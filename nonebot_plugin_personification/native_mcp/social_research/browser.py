from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from .bilibili_auth import (
    BilibiliQrProtocolError,
    generate_challenge,
    poll_challenge,
    render_qr_png,
)
from .models import PLATFORMS


_ROBOT_VERIFICATION_MARKERS = (
    "机器人验证",
    "人机验证",
    "请完成下列验证",
    "请完成验证",
    "安全验证",
    "拖动滑块",
    "拖动完成拼图",
    "captcha challenge",
)
_DEVICE_CONFIRMATION_MARKERS = (
    "请在手机上确认",
    "请在客户端确认",
    "请在app确认",
    "扫描成功",
    "扫码成功",
    "确认登录",
)
_RISK_CONTROL_MARKERS = (
    "访问过于频繁",
    "操作过于频繁",
    "请求过于频繁",
    "账号存在风险",
    "risk control",
    "risk_control",
)
_QR_EXPIRED_MARKERS = (
    "二维码已失效",
    "二维码已过期",
    "扫码已过期",
)
_QR_AUTH_TTL_SECONDS = 15 * 60
_MANUAL_AUTH_TTL_SECONDS = 30 * 60
_QR_REFRESH_COOLDOWN_SECONDS = 5.0
_MAX_QR_REFRESHES = 8
_PROTOCOL_POLL_INTERVAL_SECONDS = 1.5


def _restrict_private_directory(path: Path) -> None:
    if os.name == "nt":
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        identity = subprocess.run(
            ["whoami"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creation_flags,
        ).stdout.strip()
        if not identity:
            raise RuntimeError("profile_permission_hardening_failed")
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:(OI)(CI)F",
            ],
            check=False,
            capture_output=True,
            timeout=10,
            creationflags=creation_flags,
        )
        if result.returncode != 0:
            raise RuntimeError("profile_permission_hardening_failed")
        return
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError as exc:
        raise RuntimeError("profile_permission_hardening_failed") from exc


@dataclass
class AuthSession:
    session_id: str
    platform: str
    owner: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + _QR_AUTH_TTL_SECONDS)
    status: str = "starting"
    qr_png: bytes = b""
    qr_revision: int = 0
    error_code: str = ""
    verification_kind: str = ""
    official_window_open: bool = False
    qr_selectors: tuple[str, ...] = ()
    login_trigger_selectors: tuple[str, ...] = ()
    login_triggered: bool = False
    login_url: str = ""
    login_mode: str = "embedded_qr"
    qr_refresh_count: int = 0
    last_qr_refresh_at: float = 0.0
    protocol_secret: str = field(default="", repr=False, compare=False)
    last_protocol_poll_at: float = field(default=0.0, repr=False, compare=False)
    native_process: Any = field(default=None, repr=False, compare=False)


class BrowserPool:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.profiles_root = (self.root / "profiles").resolve()
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        _restrict_private_directory(self.profiles_root)
        self._playwright: Any = None
        self._contexts: dict[str, Any] = {}
        self._context_headless: dict[str, bool] = {}
        self._browser_channel: dict[str, str] = {}
        self._locks = {platform: asyncio.Lock() for platform in PLATFORMS}
        self._auth: dict[str, AuthSession] = {}

    async def _ensure_runtime(self) -> None:
        if self._playwright is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError("playwright_unavailable") from exc
        self._playwright = await async_playwright().start()

    def profile_dir(self, platform: str) -> Path:
        if platform not in PLATFORMS:
            raise ValueError("unsupported platform")
        return (self.profiles_root / platform).resolve()

    async def context(self, platform: str, *, headless: bool = True) -> Any:
        if platform not in PLATFORMS:
            raise ValueError("unsupported platform")
        async with self._locks[platform]:
            existing = self._contexts.get(platform)
            if existing is not None and self._context_headless.get(platform, True) == bool(headless):
                return existing
            if existing is not None:
                await existing.close()
                self._contexts.pop(platform, None)
                self._context_headless.pop(platform, None)
            await self._ensure_runtime()
            profile = self.profile_dir(platform)
            profile.mkdir(parents=True, exist_ok=True)
            _restrict_private_directory(profile)
            preferred = self._browser_channel.get(platform)
            if not headless:
                system_channels = ("chrome", "msedge") if os.name == "nt" else ("chrome",)
                preferred_channels = (preferred,) if preferred and preferred != "bundled" else ()
                channels = (*preferred_channels, *system_channels, "")
            elif preferred:
                channels = ("",) if preferred == "bundled" else (preferred, "")
            else:
                channels = ("chrome", "msedge", "") if os.name == "nt" else ("chrome", "")
            context = None
            last_error: BaseException | None = None
            for channel in dict.fromkeys(channels):
                options: dict[str, Any] = {
                    "headless": bool(headless),
                    "viewport": {"width": 1280, "height": 900},
                    "locale": "zh-CN",
                }
                if channel:
                    options["channel"] = channel
                try:
                    context = await self._playwright.chromium.launch_persistent_context(
                        str(profile),
                        **options,
                    )
                    self._browser_channel[platform] = str(channel or "bundled")
                    break
                except Exception as exc:
                    last_error = exc
            if context is None:
                raise RuntimeError("chromium_unavailable") from last_error
            self._contexts[platform] = context
            self._context_headless[platform] = bool(headless)
            return context

    async def page(self, platform: str, *, headless: bool = True) -> Any:
        context = await self.context(platform, headless=headless)
        pages = list(context.pages)
        return pages[0] if pages else await context.new_page()

    async def close_platform(self, platform: str) -> None:
        async with self._locks[platform]:
            context = self._contexts.pop(platform, None)
            self._context_headless.pop(platform, None)
            if context is not None:
                await context.close()

    async def close(self) -> None:
        for session in self._auth.values():
            await self._stop_manual_browser(session)
            session.protocol_secret = ""
            session.qr_png = b""
        for platform in list(self._contexts):
            await self.close_platform(platform)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def cookies(self, platform: str, *, headless: bool | None = None) -> list[dict[str, Any]]:
        resolved_headless = self._context_headless.get(platform, True) if headless is None else bool(headless)
        context = await self.context(platform, headless=resolved_headless)
        return [dict(item) for item in await context.cookies()]

    async def authenticated(self, platform: str, cookie_names: set[str], *, headless: bool | None = None) -> bool:
        names = {str(item.get("name") or "") for item in await self.cookies(platform, headless=headless)}
        return bool(names & cookie_names)

    @staticmethod
    def _system_browser() -> tuple[Path, str] | None:
        candidates: list[tuple[Path, str]] = []
        if os.name == "nt":
            local = Path(os.environ.get("LOCALAPPDATA") or "")
            program_files = Path(os.environ.get("PROGRAMFILES") or "")
            program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)") or "")
            candidates.extend(
                (
                    (local / "Google/Chrome/Application/chrome.exe", "chrome"),
                    (program_files / "Google/Chrome/Application/chrome.exe", "chrome"),
                    (program_files_x86 / "Google/Chrome/Application/chrome.exe", "chrome"),
                    (program_files / "Microsoft/Edge/Application/msedge.exe", "msedge"),
                    (program_files_x86 / "Microsoft/Edge/Application/msedge.exe", "msedge"),
                )
            )
        else:
            for name, channel in (("google-chrome", "chrome"), ("chromium", "bundled")):
                resolved = shutil.which(name)
                if resolved:
                    candidates.append((Path(resolved), channel))
        for executable, channel in candidates:
            if executable.is_file():
                return executable.resolve(), channel
        return None

    @staticmethod
    def manual_browser_running(session: AuthSession) -> bool:
        process = session.native_process
        return process is not None and process.poll() is None

    async def _stop_manual_browser(self, session: AuthSession) -> None:
        process = session.native_process
        session.native_process = None
        if process is None or process.poll() is not None:
            session.official_window_open = False
            return

        def stop() -> None:
            try:
                process.terminate()
                process.wait(timeout=3)
                return
            except Exception:
                pass
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

        await asyncio.to_thread(stop)
        session.official_window_open = False

    async def _launch_manual_browser(self, session: AuthSession) -> None:
        resolved = self._system_browser()
        if resolved is None:
            raise RuntimeError("system_browser_unavailable")
        executable, channel = resolved
        await self.close_platform(session.platform)
        profile = self.profile_dir(session.platform)
        profile.mkdir(parents=True, exist_ok=True)
        _restrict_private_directory(profile)
        await self._stop_manual_browser(session)
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            options["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(
            [
                str(executable),
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-mode",
                "--new-window",
                session.login_url,
            ],
            **options,
        )
        session.native_process = process
        session.login_mode = "manual_browser"
        session.status = "manual_verification_required"
        session.verification_kind = "official_browser_login"
        session.error_code = ""
        session.official_window_open = True
        session.qr_png = b""
        session.expires_at = time.time() + _MANUAL_AUTH_TTL_SECONDS
        self._browser_channel[session.platform] = channel

    @staticmethod
    async def _page_text(page: Any) -> str:
        parts: list[str] = []
        frames = list(getattr(page, "frames", ()) or ()) or [page]
        for frame in frames:
            try:
                text = await frame.locator("body").inner_text(timeout=1000)
            except Exception:
                continue
            if text:
                parts.append(str(text)[:5000])
        return " ".join(parts).casefold()

    @staticmethod
    def _looks_like_qr_png(image: bytes) -> bool:
        try:
            from PIL import Image

            with Image.open(BytesIO(image)) as source:
                width, height = source.size
                if width < 80 or height < 80 or width > 1024 or height > 1024:
                    return False
                grayscale = source.convert("L").resize((128, 128), Image.Resampling.NEAREST)
                flattened = getattr(grayscale, "get_flattened_data", None)
                pixels = list(flattened() if callable(flattened) else grayscale.getdata())
        except Exception:
            return False
        dark = [value < 128 for value in pixels]
        dark_ratio = sum(dark) / len(dark)
        extreme_ratio = sum(value < 32 or value > 223 for value in pixels) / len(pixels)
        transitions = 0
        for y in range(128):
            row = dark[y * 128 : (y + 1) * 128]
            transitions += sum(left != right for left, right in zip(row, row[1:]))
        for x in range(128):
            column = [dark[y * 128 + x] for y in range(128)]
            transitions += sum(top != bottom for top, bottom in zip(column, column[1:]))
        return 0.10 <= dark_ratio <= 0.80 and extreme_ratio >= 0.25 and transitions >= 800

    @staticmethod
    async def _capture_qr(page: Any, selectors: tuple[str, ...]) -> bytes:
        frames = list(getattr(page, "frames", ()) or ()) or [page]
        for frame in frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if not await locator.count() or not await locator.is_visible():
                        continue
                    if hasattr(locator, "bounding_box"):
                        box = await locator.bounding_box()
                        if box:
                            width = float(box.get("width") or 0)
                            height = float(box.get("height") or 0)
                            if width < 80 or height < 80 or width > 800 or height > 800:
                                continue
                    image = await locator.screenshot(type="png")
                    if image and BrowserPool._looks_like_qr_png(bytes(image)):
                        return bytes(image)
                except Exception:
                    continue
        return b""

    @staticmethod
    def _marker_present(text: str, markers: tuple[str, ...]) -> bool:
        return any(marker.casefold() in text for marker in markers)

    @staticmethod
    def _clear_page_error(session: AuthSession) -> None:
        if session.error_code != "interactive_window_unavailable":
            session.error_code = ""

    async def _inspect_auth_page(self, session: AuthSession, page: Any) -> None:
        text = await self._page_text(page)
        if self._marker_present(text, _ROBOT_VERIFICATION_MARKERS):
            self._clear_page_error(session)
            session.status = "manual_verification_required"
            session.verification_kind = "robot_verification"
            session.qr_png = b""
            return
        if self._marker_present(text, _RISK_CONTROL_MARKERS):
            session.status = "risk_controlled"
            session.verification_kind = "risk_control"
            session.error_code = "risk_controlled"
            session.qr_png = b""
            return
        if self._marker_present(text, _DEVICE_CONFIRMATION_MARKERS):
            self._clear_page_error(session)
            session.status = "manual_verification_required"
            session.verification_kind = "device_confirmation"
            session.qr_png = b""
            return
        if self._marker_present(text, _QR_EXPIRED_MARKERS):
            self._clear_page_error(session)
            session.status = "qr_expired"
            session.verification_kind = "qr_expired"
            session.qr_png = b""
            return
        qr_png = await self._capture_qr(page, session.qr_selectors)
        if qr_png:
            self._clear_page_error(session)
            old_digest = hashlib.sha256(session.qr_png).digest() if session.qr_png else b""
            new_digest = hashlib.sha256(qr_png).digest()
            if new_digest != old_digest:
                session.qr_revision += 1
            session.qr_png = qr_png
            session.status = "waiting_scan"
            session.verification_kind = ""

    @staticmethod
    async def _click_login_trigger(session: AuthSession, page: Any) -> bool:
        if session.login_triggered or not session.login_trigger_selectors:
            return session.login_triggered
        for selector in session.login_trigger_selectors:
            try:
                trigger = page.locator(selector).first
                if await trigger.count() and await trigger.is_visible():
                    await trigger.click(timeout=3000)
                    session.login_triggered = True
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_auth_surface(self, session: AuthSession, page: Any, timeout_seconds: float = 10.0) -> None:
        deadline = time.monotonic() + max(0.5, float(timeout_seconds))
        while time.monotonic() < deadline:
            await self._inspect_auth_page(session, page)
            if session.status != "starting":
                return
            await self._click_login_trigger(session, page)
            try:
                await page.wait_for_timeout(400)
            except Exception:
                break
        session.status = "manual_verification_required"
        text = await self._page_text(page)
        session.verification_kind = (
            "qr_generation_blocked"
            if "扫码登录" in text or "扫码快捷登录" in text
            else "official_page"
        )

    async def _renew_expired_qr(self, session: AuthSession, page: Any) -> None:
        now = time.time()
        if session.qr_refresh_count >= _MAX_QR_REFRESHES:
            return
        if now - session.last_qr_refresh_at < _QR_REFRESH_COOLDOWN_SECONDS:
            return
        session.qr_refresh_count += 1
        session.last_qr_refresh_at = now
        session.status = "starting"
        session.verification_kind = ""
        session.error_code = ""
        session.qr_png = b""
        session.login_triggered = False
        try:
            await page.reload(wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(500)
            if await self._click_login_trigger(session, page):
                await page.wait_for_timeout(500)
            await self._wait_for_auth_surface(session, page, timeout_seconds=8.0)
        except Exception:
            session.status = "qr_expired"
            session.verification_kind = "qr_expired"
            session.error_code = "qr_refresh_failed"

    async def _supersede_auth(self, platform: str, owner: str) -> None:
        for current in self._auth.values():
            if current.platform == platform and current.owner == owner and current.status not in {
                "success", "expired", "cancelled", "error"
            }:
                await self._stop_manual_browser(current)
                current.status = "cancelled"
                current.qr_png = b""
                current.protocol_secret = ""
                current.official_window_open = False

    @staticmethod
    async def _context_authenticated(context: Any, cookie_names: set[str]) -> bool:
        names = {str(item.get("name") or "") for item in await context.cookies()}
        return bool(names & cookie_names)

    async def _replace_bilibili_challenge(self, session: AuthSession, context: Any) -> None:
        challenge = await generate_challenge(context.request)
        qr_png = render_qr_png(challenge.qr_url)
        old_digest = hashlib.sha256(session.qr_png).digest() if session.qr_png else b""
        new_digest = hashlib.sha256(qr_png).digest()
        if new_digest != old_digest:
            session.qr_revision += 1
        session.protocol_secret = challenge.key
        session.qr_png = qr_png
        session.status = "waiting_scan"
        session.error_code = ""
        session.verification_kind = ""
        session.official_window_open = False

    async def start_bilibili_qr_auth(self, owner: str) -> dict[str, Any]:
        await self._supersede_auth("bilibili", owner)
        session = AuthSession(
            session_id=uuid.uuid4().hex,
            platform="bilibili",
            owner=owner,
            login_mode="protocol_qr",
        )
        self._auth[session.session_id] = session
        try:
            context = await self.context("bilibili", headless=True)
            await self._replace_bilibili_challenge(session, context)
        except BilibiliQrProtocolError as exc:
            session.status = "error"
            session.error_code = str(exc)
            session.protocol_secret = ""
            session.qr_png = b""
        except RuntimeError as exc:
            session.status = "error"
            session.error_code = str(exc)
            session.protocol_secret = ""
            session.qr_png = b""
        except Exception:
            session.status = "error"
            session.error_code = "bilibili_qr_generate_failed"
            session.protocol_secret = ""
            session.qr_png = b""
        return self.public_auth(session)

    async def refresh_bilibili_qr_auth(
        self,
        session: AuthSession,
        cookie_names: set[str],
    ) -> dict[str, Any]:
        if session.platform != "bilibili" or session.login_mode != "protocol_qr":
            raise ValueError("not a Bilibili protocol QR session")
        if session.status in {"success", "expired", "cancelled", "error"}:
            return self.public_auth(session)
        try:
            context = await self.context("bilibili", headless=True)
            if await self._context_authenticated(context, cookie_names):
                session.status = "success"
                session.error_code = ""
                session.verification_kind = ""
                session.protocol_secret = ""
                session.qr_png = b""
                await self.close_platform("bilibili")
                return self.public_auth(session)
            now = time.monotonic()
            if now - session.last_protocol_poll_at < _PROTOCOL_POLL_INTERVAL_SECONDS:
                return self.public_auth(session)
            session.last_protocol_poll_at = now
            code = await poll_challenge(context.request, session.protocol_secret)
            if code == 86101:
                session.status = "waiting_scan"
                session.verification_kind = ""
                session.error_code = ""
            elif code == 86090:
                session.status = "manual_verification_required"
                session.verification_kind = "device_confirmation"
                session.error_code = ""
                session.qr_png = b""
            elif code == 86038:
                if session.qr_refresh_count >= _MAX_QR_REFRESHES:
                    session.status = "expired"
                    session.verification_kind = "qr_expired"
                    session.error_code = "bilibili_qr_refresh_limit"
                    session.protocol_secret = ""
                    session.qr_png = b""
                elif time.time() - session.last_qr_refresh_at >= _QR_REFRESH_COOLDOWN_SECONDS:
                    session.qr_refresh_count += 1
                    session.last_qr_refresh_at = time.time()
                    await self._replace_bilibili_challenge(session, context)
                else:
                    session.status = "qr_expired"
                    session.verification_kind = "qr_expired"
                    session.qr_png = b""
            else:
                # A successful poll writes Set-Cookie into BrowserContext.request's
                # shared cookie jar. No token-bearing response field is consumed.
                await asyncio.sleep(0)
                if not await self._context_authenticated(context, cookie_names):
                    raise BilibiliQrProtocolError("bilibili_login_state_missing")
                session.status = "success"
                session.error_code = ""
                session.verification_kind = ""
                session.protocol_secret = ""
                session.qr_png = b""
                await self.close_platform("bilibili")
        except BilibiliQrProtocolError as exc:
            session.status = "error"
            session.error_code = str(exc)
            session.verification_kind = ""
            session.protocol_secret = ""
            session.qr_png = b""
        except RuntimeError as exc:
            session.status = "error"
            session.error_code = str(exc)
            session.verification_kind = ""
            session.protocol_secret = ""
            session.qr_png = b""
        except Exception:
            session.status = "error"
            session.error_code = "bilibili_qr_poll_failed"
            session.verification_kind = ""
            session.protocol_secret = ""
            session.qr_png = b""
        return self.public_auth(session)

    async def refresh_auth(self, session: AuthSession) -> dict[str, Any]:
        if session.status in {"success", "expired", "cancelled", "error"}:
            return self.public_auth(session)
        context = self._contexts.get(session.platform)
        try:
            pages = list(context.pages) if context is not None else []
        except Exception:
            pages = []
        if not pages:
            session.status = "error"
            session.error_code = "official_window_closed"
            session.verification_kind = ""
            session.official_window_open = False
            session.qr_png = b""
            return self.public_auth(session)
        try:
            if session.verification_kind == "official_page":
                await self._click_login_trigger(session, pages[-1])
                try:
                    await pages[-1].wait_for_timeout(400)
                except Exception:
                    pass
            await self._inspect_auth_page(session, pages[-1])
            if session.status == "qr_expired":
                await self._renew_expired_qr(session, pages[-1])
            if (
                session.verification_kind in {"robot_verification", "qr_generation_blocked"}
                and session.login_mode in {"embedded_qr", "headless_page_qr"}
            ):
                try:
                    await self._launch_manual_browser(session)
                except RuntimeError as exc:
                    session.error_code = str(exc)
        except Exception:
            session.status = "error"
            session.error_code = "login_page_unavailable"
            session.verification_kind = ""
            session.qr_png = b""
        return self.public_auth(session)

    async def start_auth(
        self,
        platform: str,
        owner: str,
        login_url: str,
        qr_selectors: tuple[str, ...],
        login_trigger_selectors: tuple[str, ...],
        *,
        prefer_headless: bool = False,
    ) -> dict[str, Any]:
        await self._supersede_auth(platform, owner)
        session = AuthSession(
            session_id=uuid.uuid4().hex,
            platform=platform,
            owner=owner,
            qr_selectors=tuple(qr_selectors),
            login_trigger_selectors=tuple(login_trigger_selectors),
            login_url=str(login_url),
            login_mode="headless_page_qr" if prefer_headless else "embedded_qr",
        )
        self._auth[session.session_id] = session
        try:
            # Page-based login uses the same persistent profile as later read-only
            # requests. Platforms that need a slider can still hand off to the
            # fixed ordinary-browser path.
            interactive_window = not prefer_headless
            interactive_fallback = False
            if prefer_headless:
                page = await self.page(platform, headless=True)
            else:
                try:
                    page = await self.page(platform, headless=False)
                except RuntimeError:
                    # A headless server can still provide the official QR flow. If
                    # the platform later asks for a slider, the status clearly
                    # reports that a desktop window is required.
                    interactive_window = False
                    interactive_fallback = True
                    session.login_mode = "headless_page_qr"
                    page = await self.page(platform, headless=True)
            session.official_window_open = interactive_window
            await page.goto(login_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(500)
            if await self._click_login_trigger(session, page):
                await page.wait_for_timeout(500)
            await self._wait_for_auth_surface(session, page)
            if interactive_fallback:
                session.error_code = "interactive_window_unavailable"
            if session.verification_kind in {"robot_verification", "qr_generation_blocked"}:
                try:
                    await self._launch_manual_browser(session)
                except RuntimeError as exc:
                    session.error_code = str(exc)
        except RuntimeError as exc:
            session.status = "error"
            session.error_code = str(exc)
        except Exception:
            session.status = "error"
            session.error_code = "login_page_unavailable"
        return self.public_auth(session)

    async def start_manual_auth(self, platform: str, owner: str, login_url: str) -> dict[str, Any]:
        await self._supersede_auth(platform, owner)
        session = AuthSession(
            session_id=uuid.uuid4().hex,
            platform=platform,
            owner=owner,
            login_url=str(login_url),
            login_mode="manual_browser",
        )
        self._auth[session.session_id] = session
        try:
            await self._launch_manual_browser(session)
        except RuntimeError as exc:
            session.status = "error"
            session.error_code = str(exc)
            session.verification_kind = ""
        except Exception:
            session.status = "error"
            session.error_code = "manual_browser_start_failed"
            session.verification_kind = ""
        return self.public_auth(session)

    def get_auth(self, session_id: str, owner: str) -> AuthSession:
        session = self._auth.get(str(session_id or ""))
        if session is None or session.owner != str(owner or ""):
            raise KeyError("auth_session_not_found")
        if session.expires_at <= time.time() and session.status not in {"success", "cancelled"}:
            session.status = "expired"
            session.qr_png = b""
            session.protocol_secret = ""
        return session

    def public_auth(self, session: AuthSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "platform": session.platform,
            "status": session.status,
            "created_at": session.created_at,
            "expires_at": session.expires_at,
            "remaining_seconds": max(0, int(session.expires_at - time.time())),
            "qr_available": bool(session.qr_png),
            "qr_revision": int(session.qr_revision),
            "error_code": session.error_code,
            "verification_kind": session.verification_kind,
            "official_window_open": bool(session.official_window_open),
            "login_mode": session.login_mode,
            "qr_refresh_count": int(session.qr_refresh_count),
        }

    def auth_qrcode(self, session_id: str, owner: str) -> dict[str, Any]:
        session = self.get_auth(session_id, owner)
        if not session.qr_png:
            raise KeyError("qrcode_unavailable")
        return {
            **self.public_auth(session),
            "mime_type": "image/png",
            "data_base64": base64.b64encode(session.qr_png).decode("ascii"),
        }

    async def cancel_auth(self, session_id: str, owner: str) -> dict[str, Any]:
        session = self.get_auth(session_id, owner)
        await self._stop_manual_browser(session)
        session.status = "cancelled"
        session.qr_png = b""
        session.protocol_secret = ""
        session.official_window_open = False
        return self.public_auth(session)

    async def expire_auth(self, session: AuthSession) -> None:
        await self._stop_manual_browser(session)
        await self.close_platform(session.platform)
        session.status = "expired"
        session.qr_png = b""
        session.protocol_secret = ""
        session.official_window_open = False

    async def logout(self, platform: str) -> None:
        for session in self._auth.values():
            if session.platform == platform:
                await self._stop_manual_browser(session)
        await self.close_platform(platform)
        profile = self.profile_dir(platform)
        if profile == self.profiles_root or not profile.is_relative_to(self.profiles_root):
            raise RuntimeError("unsafe_profile_path")
        if profile.exists():
            await asyncio.to_thread(shutil.rmtree, profile)
        for session in self._auth.values():
            if session.platform == platform:
                session.status = "cancelled"
                session.qr_png = b""
                session.protocol_secret = ""
                session.official_window_open = False
        self._browser_channel.pop(platform, None)


__all__ = ["AuthSession", "BrowserPool"]
