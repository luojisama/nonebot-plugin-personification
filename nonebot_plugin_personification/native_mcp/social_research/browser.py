from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
_INTERACTIVE_AUTH_TTL_SECONDS = 10 * 60
_QR_REFRESH_COOLDOWN_SECONDS = 5.0
_MAX_QR_REFRESHES = 8
_PROTOCOL_POLL_INTERVAL_SECONDS = 1.5
_INTERACTIVE_FRAME_CACHE_SECONDS = 0.9
_INTERACTIVE_FRAME_MAX_BYTES = 2 * 1024 * 1024
_INTERACTIVE_TEXT_MAX_CHARS = 200
_INTERACTIVE_DRAG_MAX_POINTS = 32
_INTERACTIVE_DRAG_REPLAY_MAX_SECONDS = 0.8
_INTERACTIVE_POINTER_MOVE_MAX_POINTS = 6
_INTERACTIVE_POINTER_TIMEOUT_SECONDS = 5.0
_INTERACTIVE_POINTER_GESTURE = re.compile(r"^gesture_[A-Za-z0-9_-]{8,80}$")
_INTERACTIVE_KEYS = frozenset(
    {
        "Tab",
        "Enter",
        "Escape",
        "Backspace",
        "Delete",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)
_BROWSER_CONTEXT_IDLE_SECONDS = 5 * 60.0
_BROWSER_DIAGNOSTIC_LIMIT = 64


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
    qr_missing_since: float = field(default=0.0, repr=False, compare=False)
    protocol_secret: str = field(default="", repr=False, compare=False)
    last_protocol_poll_at: float = field(default=0.0, repr=False, compare=False)
    native_process: Any = field(default=None, repr=False, compare=False)
    interactive_allowed_hosts: tuple[str, ...] = field(default=(), repr=False, compare=False)
    interactive_frame: bytes = field(default=b"", repr=False, compare=False)
    interactive_frame_mime: str = field(default="image/jpeg", repr=False, compare=False)
    interactive_frame_revision: int = 0
    interactive_frame_captured_at: float = field(default=0.0, repr=False, compare=False)
    interactive_last_action_at: float = field(default=0.0, repr=False, compare=False)
    interactive_display_url: str = ""
    interactive_viewport_width: int = 1280
    interactive_viewport_height: int = 900
    interactive_lock: Any = field(default_factory=asyncio.Lock, repr=False, compare=False)
    interactive_start_task: Any = field(default=None, repr=False, compare=False)
    interactive_pointer_task: Any = field(default=None, repr=False, compare=False)
    interactive_pointer_down: bool = field(default=False, repr=False, compare=False)
    interactive_pointer_gesture_id: str = field(default="", repr=False, compare=False)
    interactive_pointer_seq: int = field(default=-1, repr=False, compare=False)
    interactive_pointer_last_at: float = field(default=0.0, repr=False, compare=False)
    interactive_pointer_last_gesture_id: str = field(default="", repr=False, compare=False)
    interactive_pointer_last_seq: int = field(default=-1, repr=False, compare=False)
    interactive_pointer_error_code: str = field(default="", repr=False, compare=False)


class BrowserPool:
    def __init__(
        self,
        root: Path,
        *,
        idle_timeout_seconds: float = _BROWSER_CONTEXT_IDLE_SECONDS,
        platforms: tuple[str, ...] | None = None,
        task_name_prefix: str = "social-browser",
    ) -> None:
        self.root = root.resolve()
        self.profiles_root = (self.root / "profiles").resolve()
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        _restrict_private_directory(self.profiles_root)
        normalized_platforms = tuple(
            dict.fromkeys(
                str(platform or "").strip().lower()
                for platform in (platforms or tuple(PLATFORMS))
                if str(platform or "").strip()
            )
        )
        if not normalized_platforms:
            raise ValueError("browser pool requires at least one platform")
        self.platforms = normalized_platforms
        self._task_name_prefix = str(task_name_prefix or "browser").strip() or "browser"
        self._playwright: Any = None
        self._contexts: dict[str, Any] = {}
        self._context_headless: dict[str, bool] = {}
        self._browser_channel: dict[str, str] = {}
        self._locks = {platform: asyncio.Lock() for platform in self.platforms}
        self._auth: dict[str, AuthSession] = {}
        self._idle_timeout_seconds = max(0.01, float(idle_timeout_seconds))
        self._activity_counts = {platform: 0 for platform in self.platforms}
        self._last_activity = {platform: time.monotonic() for platform in self.platforms}
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._diagnostics: list[dict[str, Any]] = []

    def _record_diagnostic(self, code: str, platform: str) -> None:
        self._diagnostics.append(
            {
                "code": str(code),
                "platform": str(platform),
                "created_at": time.time(),
            }
        )
        if len(self._diagnostics) > _BROWSER_DIAGNOSTIC_LIMIT:
            del self._diagnostics[: len(self._diagnostics) - _BROWSER_DIAGNOSTIC_LIMIT]

    def runtime_status(self) -> dict[str, Any]:
        return {
            "idle_timeout_seconds": self._idle_timeout_seconds,
            "open_contexts": sorted(self._contexts),
            "activity": {
                platform: int(self._activity_counts.get(platform, 0))
                for platform in self.platforms
            },
            "diagnostics": [dict(item) for item in self._diagnostics[-10:]],
        }

    def set_idle_timeout_seconds(self, value: float) -> None:
        self._idle_timeout_seconds = max(0.01, float(value))
        for platform in self.platforms:
            if platform in self._contexts and self._activity_counts.get(platform, 0) == 0:
                self._schedule_idle_eviction(platform)

    def _cancel_idle_task(self, platform: str) -> None:
        task = self._idle_tasks.pop(platform, None)
        if task is not None and not task.done():
            task.cancel()

    def _platform_session_protected(self, platform: str) -> bool:
        now = time.time()
        for session in self._auth.values():
            if session.platform != platform:
                continue
            if self.manual_browser_running(session):
                return True
            task = session.interactive_start_task
            if task is not None and not task.done():
                return True
            if session.interactive_lock.locked():
                return True
            if (
                session.expires_at > now
                and session.status not in {"success", "expired", "cancelled", "error"}
            ):
                return True
        return False

    def _schedule_idle_eviction(self, platform: str) -> None:
        self._cancel_idle_task(platform)
        if platform not in self._contexts or self._activity_counts.get(platform, 0) > 0:
            return
        task = asyncio.create_task(
            self._idle_evict_after(platform),
            name=f"{self._task_name_prefix}-idle:{platform}",
        )
        self._idle_tasks[platform] = task

        def consume(completed: asyncio.Task[None]) -> None:
            if self._idle_tasks.get(platform) is completed:
                self._idle_tasks.pop(platform, None)
            if completed.cancelled():
                return
            try:
                completed.exception()
            except Exception:
                return

        task.add_done_callback(consume)

    async def _idle_evict_after(self, platform: str) -> None:
        while True:
            elapsed = time.monotonic() - self._last_activity.get(platform, 0.0)
            await asyncio.sleep(max(0.01, self._idle_timeout_seconds - elapsed))
            async with self._locks[platform]:
                if self._activity_counts.get(platform, 0) > 0:
                    return
                if self._platform_session_protected(platform):
                    self._last_activity[platform] = time.monotonic()
                    continue
                context = self._contexts.pop(platform, None)
                self._context_headless.pop(platform, None)
                if context is None:
                    return
                await context.close()
                self._record_diagnostic("browser_context_idle_evicted", platform)
                return

    @asynccontextmanager
    async def activity(self, platform: str):
        if platform not in self.platforms:
            raise ValueError("unsupported platform")
        self._cancel_idle_task(platform)
        self._activity_counts[platform] = self._activity_counts.get(platform, 0) + 1
        self._last_activity[platform] = time.monotonic()
        try:
            yield
        finally:
            self._activity_counts[platform] = max(
                0,
                self._activity_counts.get(platform, 0) - 1,
            )
            self._last_activity[platform] = time.monotonic()
            if self._activity_counts[platform] == 0:
                self._schedule_idle_eviction(platform)

    async def _ensure_runtime(self) -> None:
        if self._playwright is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError("playwright_unavailable") from exc
        self._playwright = await async_playwright().start()

    def profile_dir(self, platform: str) -> Path:
        if platform not in self.platforms:
            raise ValueError("unsupported platform")
        return (self.profiles_root / platform).resolve()

    async def context(self, platform: str, *, headless: bool = True) -> Any:
        if platform not in self.platforms:
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

    async def fresh_page(self, platform: str, *, headless: bool = True) -> Any:
        """Return an isolated page for one concurrent read-only operation."""
        context = await self.context(platform, headless=headless)
        return await context.new_page()

    async def close_platform(self, platform: str) -> None:
        if platform not in self.platforms:
            raise ValueError("unsupported platform")
        self._cancel_idle_task(platform)
        for session in self._auth.values():
            if session.platform == platform and (
                session.interactive_pointer_down or session.interactive_pointer_task is not None
            ):
                await self._release_interactive_pointer(session)
        async with self._locks[platform]:
            context = self._contexts.pop(platform, None)
            self._context_headless.pop(platform, None)
            if context is not None:
                await context.close()

    async def close(self) -> None:
        idle_tasks = list(self._idle_tasks.values())
        self._idle_tasks.clear()
        for task in idle_tasks:
            task.cancel()
        if idle_tasks:
            await asyncio.gather(*idle_tasks, return_exceptions=True)
        for session in self._auth.values():
            await self._cancel_interactive_start(session)
            await self._stop_manual_browser(session)
            if session.interactive_pointer_down or session.interactive_pointer_task is not None:
                await self._release_interactive_pointer(session)
            session.protocol_secret = ""
            session.qr_png = b""
            session.interactive_frame = b""
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

    @staticmethod
    def _interactive_url_allowed(session: AuthSession, value: str) -> bool:
        try:
            parsed = urlparse(str(value or ""))
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        return bool(
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and any(host == suffix or host.endswith("." + suffix) for suffix in session.interactive_allowed_hosts)
        )

    @staticmethod
    def _interactive_display_url(value: str) -> str:
        try:
            parsed = urlparse(str(value or ""))
        except ValueError:
            return ""
        if parsed.scheme != "https" or not parsed.hostname:
            return ""
        port = f":{parsed.port}" if parsed.port else ""
        # Paths may contain opaque challenge identifiers; the WebUI only needs
        # to show which official origin currently owns the page.
        return f"https://{parsed.hostname.lower()}{port}/"[:500]

    def platform_auth_active(self, platform: str) -> bool:
        now = time.time()
        return any(
            session.platform == platform
            and session.expires_at > now
            and session.status not in {"success", "expired", "cancelled", "error"}
            for session in self._auth.values()
        )

    async def _interactive_page(self, session: AuthSession) -> Any:
        if session.login_mode != "webui_interactive":
            raise RuntimeError("interactive_auth_unavailable")
        if session.status in {"success", "expired", "cancelled", "error"}:
            raise RuntimeError("interactive_auth_unavailable")
        context = self._contexts.get(session.platform)
        try:
            pages = list(context.pages) if context is not None else []
        except Exception:
            pages = []
        if not pages:
            session.status = "error"
            session.error_code = "interactive_page_unavailable"
            session.official_window_open = False
            raise RuntimeError("interactive_page_unavailable")
        page = pages[-1]
        page_url = str(getattr(page, "url", "") or "")
        if not self._interactive_url_allowed(session, page_url):
            session.status = "error"
            session.error_code = "interactive_page_outside_platform"
            session.official_window_open = False
            raise RuntimeError("interactive_page_outside_platform")
        session.interactive_display_url = self._interactive_display_url(page_url)
        viewport = getattr(page, "viewport_size", None)
        if isinstance(viewport, dict):
            session.interactive_viewport_width = min(1920, max(320, int(viewport.get("width") or 1280)))
            session.interactive_viewport_height = min(1440, max(240, int(viewport.get("height") or 900)))
        return page

    @staticmethod
    def _cancel_interactive_pointer_task(session: AuthSession) -> None:
        task = session.interactive_pointer_task
        session.interactive_pointer_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _interactive_pointer_page(self, session: AuthSession) -> Any | None:
        context = self._contexts.get(session.platform)
        try:
            pages = list(context.pages) if context is not None else []
        except Exception:
            pages = []
        if not pages:
            return None
        page = pages[-1]
        page_url = str(getattr(page, "url", "") or "")
        return page if self._interactive_url_allowed(session, page_url) else None

    async def _release_interactive_pointer_unlocked(
        self,
        session: AuthSession,
        *,
        error_code: str = "",
    ) -> None:
        gesture_id = session.interactive_pointer_gesture_id
        sequence = session.interactive_pointer_seq
        self._cancel_interactive_pointer_task(session)
        if session.interactive_pointer_down:
            page = self._interactive_pointer_page(session)
            if page is not None:
                with suppress(Exception):
                    await page.mouse.up()
        session.interactive_pointer_down = False
        session.interactive_pointer_gesture_id = ""
        session.interactive_pointer_seq = -1
        session.interactive_pointer_last_at = 0.0
        if gesture_id:
            session.interactive_pointer_last_gesture_id = gesture_id
            session.interactive_pointer_last_seq = sequence
        session.interactive_pointer_error_code = str(error_code or "")
        session.interactive_last_action_at = time.time()
        session.interactive_frame_captured_at = 0.0

    async def _release_interactive_pointer(
        self,
        session: AuthSession,
        *,
        error_code: str = "",
    ) -> None:
        async with session.interactive_lock:
            await self._release_interactive_pointer_unlocked(session, error_code=error_code)

    async def _interactive_pointer_watchdog(self, session: AuthSession, gesture_id: str) -> None:
        try:
            delay = _INTERACTIVE_POINTER_TIMEOUT_SECONDS
            while True:
                await asyncio.sleep(delay)
                async with session.interactive_lock:
                    if (
                        not session.interactive_pointer_down
                        or session.interactive_pointer_gesture_id != gesture_id
                    ):
                        return
                    elapsed = time.monotonic() - session.interactive_pointer_last_at
                    if elapsed < _INTERACTIVE_POINTER_TIMEOUT_SECONDS:
                        delay = max(0.01, _INTERACTIVE_POINTER_TIMEOUT_SECONDS - elapsed)
                        continue
                    await self._release_interactive_pointer_unlocked(
                        session,
                        error_code="interactive_pointer_timeout",
                    )
                    return
        except asyncio.CancelledError:
            return

    async def _cancel_interactive_start(self, session: AuthSession) -> None:
        task = session.interactive_start_task
        session.interactive_start_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _prepare_interactive_auth(self, session: AuthSession) -> None:
        try:
            async with session.interactive_lock:
                if session.status == "cancelled":
                    return
                page = await self.page(session.platform, headless=True)
                await page.goto(session.login_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(350)
                if await self._click_login_trigger(session, page):
                    await page.wait_for_timeout(350)
                await self._wait_for_auth_surface(session, page)
                current_url = str(getattr(page, "url", "") or "")
                if not self._interactive_url_allowed(session, current_url):
                    raise RuntimeError("interactive_page_outside_platform")
                if session.status == "starting":
                    session.status = "manual_verification_required"
                    session.verification_kind = "official_page"
                session.interactive_display_url = self._interactive_display_url(current_url)
                session.official_window_open = True
        except asyncio.CancelledError:
            return
        except RuntimeError as exc:
            session.status = "error"
            session.error_code = str(exc)
            session.official_window_open = False
        except Exception:
            session.status = "error"
            session.error_code = "interactive_page_unavailable"
            session.official_window_open = False

    async def start_interactive_auth(
        self,
        platform: str,
        owner: str,
        login_url: str,
        allowed_hosts: tuple[str, ...],
        qr_selectors: tuple[str, ...],
        login_trigger_selectors: tuple[str, ...],
    ) -> dict[str, Any]:
        await self._supersede_auth(platform, owner)
        session = AuthSession(
            session_id=uuid.uuid4().hex,
            platform=platform,
            owner=owner,
            expires_at=time.time() + _INTERACTIVE_AUTH_TTL_SECONDS,
            qr_selectors=tuple(qr_selectors),
            login_trigger_selectors=tuple(login_trigger_selectors),
            login_url=str(login_url),
            login_mode="webui_interactive",
            interactive_allowed_hosts=tuple(str(item).lower() for item in allowed_hosts if str(item).strip()),
        )
        self._auth[session.session_id] = session
        if not self._interactive_url_allowed(session, login_url):
            session.status = "error"
            session.error_code = "interactive_page_outside_platform"
            return self.public_auth(session)
        session.interactive_start_task = asyncio.create_task(self._prepare_interactive_auth(session))
        return self.public_auth(session)

    def _interactive_frame_result(
        self,
        session: AuthSession,
        *,
        after_revision: int,
        stale: bool = False,
    ) -> dict[str, Any]:
        changed = bool(
            session.interactive_frame
            and session.interactive_frame_revision > after_revision
        )
        result = {
            **self.public_auth(session),
            "changed": changed,
            "stale": bool(stale),
            "mime_type": session.interactive_frame_mime,
            "captured_at": float(session.interactive_frame_captured_at or 0.0),
        }
        if changed:
            result["data_base64"] = base64.b64encode(session.interactive_frame).decode("ascii")
        return result

    async def interactive_frame(
        self,
        session_id: str,
        owner: str,
        *,
        after_revision: int = 0,
    ) -> dict[str, Any]:
        session = self.get_auth(session_id, owner)
        if isinstance(after_revision, bool) or not isinstance(after_revision, int) or after_revision < 0:
            raise ValueError("interactive frame revision is invalid")
        # A frame refresh is expendable. When a direct caller races an input
        # operation, keep serving the last safe frame instead of making the
        # human interaction wait behind another screenshot.
        if session.interactive_lock.locked() and session.interactive_frame:
            return self._interactive_frame_result(
                session,
                after_revision=after_revision,
                stale=True,
            )
        async with session.interactive_lock:
            page = await self._interactive_page(session)
            now = time.time()
            if (
                not session.interactive_frame
                or now - session.interactive_frame_captured_at >= _INTERACTIVE_FRAME_CACHE_SECONDS
            ):
                image = bytes(await page.screenshot(type="jpeg", quality=60))
                if not image or len(image) > _INTERACTIVE_FRAME_MAX_BYTES:
                    raise RuntimeError("interactive_frame_unavailable")
                if image != session.interactive_frame:
                    session.interactive_frame = image
                    session.interactive_frame_mime = "image/jpeg"
                    session.interactive_frame_revision += 1
                session.interactive_frame_captured_at = now
            return self._interactive_frame_result(session, after_revision=after_revision)

    @staticmethod
    def _interactive_coordinate(value: Any, maximum: int) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("interactive coordinates must be numbers")
        number = float(value)
        if not math.isfinite(number) or number < 0 or number > maximum:
            raise ValueError("interactive coordinates are outside the viewport")
        return number

    @staticmethod
    def _interactive_pointer_gesture(action: dict[str, Any]) -> str:
        gesture_id = str(action.get("gesture_id") or "")
        if not _INTERACTIVE_POINTER_GESTURE.fullmatch(gesture_id):
            raise ValueError("interactive pointer gesture is invalid")
        return gesture_id

    @staticmethod
    def _interactive_pointer_sequence(action: dict[str, Any]) -> int:
        sequence = action.get("seq")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 1_000_000_000:
            raise ValueError("interactive pointer sequence is invalid")
        return sequence

    def _interactive_pointer_points(
        self,
        value: Any,
        width: int,
        height: int,
    ) -> list[tuple[float, float]]:
        if not isinstance(value, list) or not 1 <= len(value) <= _INTERACTIVE_POINTER_MOVE_MAX_POINTS:
            raise ValueError("interactive pointer points are invalid")
        points: list[tuple[float, float]] = []
        for point in value:
            if not isinstance(point, dict):
                raise ValueError("interactive pointer points are invalid")
            points.append(
                (
                    self._interactive_coordinate(point.get("x"), width),
                    self._interactive_coordinate(point.get("y"), height),
                )
            )
        return points

    def _interactive_action_result(
        self,
        session: AuthSession,
        *,
        applied: bool,
        duplicate: bool = False,
        pointer_error_code: str = "",
    ) -> dict[str, Any]:
        return {
            **self.public_auth(session),
            "action_applied": bool(applied),
            "action_duplicate": bool(duplicate),
            "interactive_pointer_active": bool(session.interactive_pointer_down),
            "interactive_pointer_error_code": str(
                pointer_error_code or session.interactive_pointer_error_code or ""
            ),
        }

    async def interactive_action(
        self,
        session_id: str,
        owner: str,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        session = self.get_auth(session_id, owner)
        async with session.interactive_lock:
            action_type = str(action.get("type") or "")
            width = session.interactive_viewport_width
            height = session.interactive_viewport_height
            pointer_action = action_type in {
                "pointer_start",
                "pointer_move",
                "pointer_end",
                "pointer_cancel",
            }
            gesture_id = self._interactive_pointer_gesture(action) if pointer_action else ""
            sequence = self._interactive_pointer_sequence(action) if pointer_action else -1

            if action_type == "pointer_move":
                points = self._interactive_pointer_points(action.get("points"), width, height)
            else:
                points = []
            if action_type in {"pointer_start", "pointer_end"}:
                pointer_x = self._interactive_coordinate(action.get("x"), width)
                pointer_y = self._interactive_coordinate(action.get("y"), height)
            else:
                pointer_x = pointer_y = 0.0

            if pointer_action and not session.interactive_pointer_down:
                if (
                    gesture_id == session.interactive_pointer_last_gesture_id
                    and session.interactive_pointer_error_code == "interactive_pointer_timeout"
                ):
                    return self._interactive_action_result(
                        session,
                        applied=False,
                        pointer_error_code="interactive_pointer_timeout",
                    )
                if (
                    gesture_id == session.interactive_pointer_last_gesture_id
                    and sequence <= session.interactive_pointer_last_seq
                ):
                    return self._interactive_action_result(session, applied=False, duplicate=True)
                if action_type != "pointer_start":
                    return self._interactive_action_result(
                        session,
                        applied=False,
                        pointer_error_code="interactive_pointer_not_active",
                    )

            if pointer_action and session.interactive_pointer_down:
                if gesture_id != session.interactive_pointer_gesture_id:
                    if action_type != "pointer_start":
                        return self._interactive_action_result(
                            session,
                            applied=False,
                            pointer_error_code="interactive_pointer_gesture_mismatch",
                        )
                    await self._release_interactive_pointer_unlocked(session)
                elif action_type == "pointer_start" and sequence == 0:
                    return self._interactive_action_result(session, applied=False, duplicate=True)
                elif sequence <= session.interactive_pointer_seq:
                    return self._interactive_action_result(
                        session,
                        applied=False,
                        duplicate=sequence == session.interactive_pointer_seq,
                        pointer_error_code=(
                            "" if sequence == session.interactive_pointer_seq else "interactive_pointer_sequence_invalid"
                        ),
                    )

            try:
                page = await self._interactive_page(session)
            except Exception:
                if pointer_action and session.interactive_pointer_down:
                    await self._release_interactive_pointer_unlocked(session)
                raise

            if action_type == "pointer_start":
                if sequence != 0:
                    raise ValueError("interactive pointer start sequence is invalid")
                await page.mouse.move(pointer_x, pointer_y)
                await page.mouse.down()
                session.interactive_pointer_down = True
                session.interactive_pointer_gesture_id = gesture_id
                session.interactive_pointer_seq = sequence
                session.interactive_pointer_last_at = time.monotonic()
                session.interactive_pointer_error_code = ""
                session.interactive_pointer_task = asyncio.create_task(
                    self._interactive_pointer_watchdog(session, gesture_id),
                    name=f"social-pointer-timeout:{session.platform}:{session.session_id[:8]}",
                )
            elif action_type == "pointer_move":
                for x, y in points:
                    await page.mouse.move(x, y)
                session.interactive_pointer_seq = sequence
                session.interactive_pointer_last_at = time.monotonic()
            elif action_type == "pointer_end":
                session.interactive_pointer_seq = sequence
                session.interactive_pointer_last_at = time.monotonic()
                try:
                    await page.mouse.move(pointer_x, pointer_y)
                finally:
                    await self._release_interactive_pointer_unlocked(session)
            elif action_type == "pointer_cancel":
                session.interactive_pointer_seq = sequence
                await self._release_interactive_pointer_unlocked(session)
            if action_type == "click":
                x = self._interactive_coordinate(action.get("x"), width)
                y = self._interactive_coordinate(action.get("y"), height)
                await page.mouse.click(x, y, delay=60)
            elif action_type == "drag":
                points = action.get("points")
                if not isinstance(points, list) or not 2 <= len(points) <= _INTERACTIVE_DRAG_MAX_POINTS:
                    raise ValueError("interactive drag points are invalid")
                normalized: list[tuple[float, float, int]] = []
                last_elapsed = 0
                for point in points:
                    if not isinstance(point, dict):
                        raise ValueError("interactive drag points are invalid")
                    x = self._interactive_coordinate(point.get("x"), width)
                    y = self._interactive_coordinate(point.get("y"), height)
                    elapsed = point.get("t", last_elapsed)
                    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
                        raise ValueError("interactive drag timing is invalid")
                    elapsed_int = int(elapsed)
                    if elapsed_int < last_elapsed or elapsed_int > 5000:
                        raise ValueError("interactive drag timing is invalid")
                    normalized.append((x, y, elapsed_int))
                    last_elapsed = elapsed_int
                await page.mouse.move(normalized[0][0], normalized[0][1])
                await page.mouse.down()
                previous = normalized[0][2]
                total_seconds = max(0.001, (normalized[-1][2] - previous) / 1000)
                timing_scale = min(1.0, _INTERACTIVE_DRAG_REPLAY_MAX_SECONDS / total_seconds)
                try:
                    for x, y, elapsed in normalized[1:]:
                        await asyncio.sleep(max(0, elapsed - previous) / 1000 * timing_scale)
                        await page.mouse.move(x, y)
                        previous = elapsed
                finally:
                    await page.mouse.up()
            elif action_type == "type":
                text = action.get("text")
                if not isinstance(text, str) or not text or len(text) > _INTERACTIVE_TEXT_MAX_CHARS:
                    raise ValueError("interactive text is invalid")
                if any(character in text for character in ("\x00", "\r", "\n")):
                    raise ValueError("interactive text contains unsupported characters")
                await page.keyboard.insert_text(text)
            elif action_type == "key":
                key = str(action.get("key") or "")
                if key not in _INTERACTIVE_KEYS:
                    raise ValueError("interactive key is not allowed")
                await page.keyboard.press(key)
            elif action_type == "scroll":
                delta = action.get("delta_y")
                if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(float(delta)):
                    raise ValueError("interactive scroll delta is invalid")
                await page.mouse.wheel(0, max(-1200, min(1200, float(delta))))
            elif not pointer_action:
                raise ValueError("interactive action is not allowed")
            session.interactive_last_action_at = time.time()
            # Keep the last frame visible while the next screenshot is being
            # produced. A zero capture time marks it dirty without flashing the
            # WebUI back to an empty canvas.
            session.interactive_frame_captured_at = 0.0
            if not pointer_action:
                try:
                    await page.wait_for_timeout(60)
                except Exception:
                    pass
            return self._interactive_action_result(session, applied=True)

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
            from PIL import Image, ImageOps

            with Image.open(BytesIO(image)) as source:
                width, height = source.size
                if width < 80 or height < 80 or width > 1024 or height > 1024:
                    return False
                if not 0.75 <= width / max(1, height) <= 1.33:
                    return False
                grayscale = ImageOps.autocontrast(source.convert("L")).resize((160, 160), Image.Resampling.NEAREST)
                flattened = getattr(grayscale, "get_flattened_data", None)
                pixels = list(flattened() if callable(flattened) else grayscale.getdata())
        except Exception:
            return False
        dark = [value < 128 for value in pixels]
        dark_ratio = sum(dark) / len(dark)
        extreme_ratio = sum(value < 32 or value > 223 for value in pixels) / len(pixels)
        transitions = 0
        for y in range(160):
            row = dark[y * 160 : (y + 1) * 160]
            transitions += sum(left != right for left, right in zip(row, row[1:]))
        for x in range(160):
            column = [dark[y * 160 + x] for y in range(160)]
            transitions += sum(top != bottom for top, bottom in zip(column, column[1:]))
        if not (0.10 <= dark_ratio <= 0.72 and extreme_ratio >= 0.35 and transitions >= 1000):
            return False

        def finder_hits(*, horizontal: bool) -> list[tuple[float, float, float]]:
            hits: list[tuple[float, float, float]] = []
            for fixed in range(160):
                values = (
                    dark[fixed * 160 : (fixed + 1) * 160]
                    if horizontal
                    else [dark[index * 160 + fixed] for index in range(160)]
                )
                runs: list[tuple[bool, int, int]] = []
                start = 0
                current = values[0]
                for index, value in enumerate(values[1:], start=1):
                    if value == current:
                        continue
                    runs.append((current, start, index - start))
                    current = value
                    start = index
                runs.append((current, start, len(values) - start))
                for index in range(len(runs) - 4):
                    window = runs[index : index + 5]
                    if [item[0] for item in window] != [True, False, True, False, True]:
                        continue
                    lengths = [item[2] for item in window]
                    module = sum(lengths) / 7.0
                    if module < 1.0:
                        continue
                    if any(abs(length - module) > module * 0.9 for length in (*lengths[:2], *lengths[3:])):
                        continue
                    if abs(lengths[2] - 3 * module) > 1.35 * module:
                        continue
                    center = window[2][1] + window[2][2] / 2
                    hits.append((center, float(fixed), module) if horizontal else (float(fixed), center, module))
            return hits

        horizontal_hits = finder_hits(horizontal=True)
        vertical_hits = finder_hits(horizontal=False)
        candidates: list[tuple[float, float, float]] = []
        for horizontal in horizontal_hits:
            for vertical in vertical_hits:
                module = min(horizontal[2], vertical[2])
                if not 0.5 <= horizontal[2] / max(0.01, vertical[2]) <= 2.0:
                    continue
                if math.hypot(horizontal[0] - vertical[0], horizontal[1] - vertical[1]) <= max(3.0, 2.5 * module):
                    candidates.append(((horizontal[0] + vertical[0]) / 2, (horizontal[1] + vertical[1]) / 2, module))

        centers: list[list[float]] = []
        for x, y, module in candidates:
            match = next(
                (
                    center
                    for center in centers
                    if math.hypot(center[0] - x, center[1] - y) <= max(6.0, 2.5 * max(center[2], module))
                ),
                None,
            )
            if match is None:
                centers.append([x, y, module, 1.0])
            else:
                count = match[3] + 1.0
                match[0] = (match[0] * match[3] + x) / count
                match[1] = (match[1] * match[3] + y) / count
                match[2] = max(match[2], module)
                match[3] = count

        reliable = [center for center in centers if center[3] >= 2]
        for corner in reliable:
            others = [center for center in reliable if center is not corner]
            for left_index, first in enumerate(others):
                for second in others[left_index + 1 :]:
                    ax, ay = first[0] - corner[0], first[1] - corner[1]
                    bx, by = second[0] - corner[0], second[1] - corner[1]
                    a_length = math.hypot(ax, ay)
                    b_length = math.hypot(bx, by)
                    if min(a_length, b_length) < 40 or not 0.45 <= a_length / max(0.01, b_length) <= 2.2:
                        continue
                    if abs(ax * bx + ay * by) / max(0.01, a_length * b_length) <= 0.42:
                        return True
        return False

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
            session.qr_missing_since = 0.0
            old_digest = hashlib.sha256(session.qr_png).digest() if session.qr_png else b""
            new_digest = hashlib.sha256(qr_png).digest()
            if new_digest != old_digest:
                session.qr_revision += 1
            session.qr_png = qr_png
            session.status = "waiting_scan"
            session.verification_kind = ""
            return
        if session.status == "waiting_scan" and session.qr_png:
            now = time.time()
            if session.qr_missing_since <= 0:
                session.qr_missing_since = now
                return
            if now - session.qr_missing_since >= 0.8:
                # QR login panels commonly replace the QR with the account
                # avatar immediately after a successful scan. That image is not
                # a new QR: it is the device-confirmation stage.
                self._clear_page_error(session)
                session.status = "manual_verification_required"
                session.verification_kind = "device_confirmation"
                session.qr_png = b""
                session.qr_missing_since = 0.0

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
        session.qr_missing_since = 0.0
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
            # A platform owns exactly one persistent profile. A second admin
            # session must not race the first session for that same profile.
            if current.platform == platform and current.status not in {
                "success", "expired", "cancelled", "error"
            }:
                await self._cancel_interactive_start(current)
                await self._stop_manual_browser(current)
                current.status = "cancelled"
                current.qr_png = b""
                current.protocol_secret = ""
                current.interactive_frame = b""
                current.official_window_open = False
                if current.login_mode == "webui_interactive":
                    await self.close_platform(current.platform)

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
            session.interactive_frame = b""
            session.official_window_open = False
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
            "interactive_available": bool(
                session.login_mode == "webui_interactive"
                and session.status not in {"starting", "success", "expired", "cancelled", "error"}
                and session.official_window_open
            ),
            "interactive_frame_revision": int(session.interactive_frame_revision),
            "interactive_display_url": session.interactive_display_url,
            "interactive_pointer_active": bool(session.interactive_pointer_down),
            "interactive_pointer_error_code": session.interactive_pointer_error_code,
            "interactive_viewport": {
                "width": int(session.interactive_viewport_width),
                "height": int(session.interactive_viewport_height),
            },
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
        await self._cancel_interactive_start(session)
        await self._stop_manual_browser(session)
        if session.login_mode == "webui_interactive":
            await self.close_platform(session.platform)
        session.status = "cancelled"
        session.qr_png = b""
        session.protocol_secret = ""
        session.interactive_frame = b""
        session.official_window_open = False
        return self.public_auth(session)

    async def expire_auth(self, session: AuthSession) -> None:
        await self._cancel_interactive_start(session)
        await self._stop_manual_browser(session)
        await self.close_platform(session.platform)
        session.status = "expired"
        session.qr_png = b""
        session.protocol_secret = ""
        session.interactive_frame = b""
        session.official_window_open = False

    async def logout(self, platform: str) -> None:
        for session in self._auth.values():
            if session.platform == platform:
                await self._cancel_interactive_start(session)
                await self._stop_manual_browser(session)
                session.status = "cancelled"
                session.qr_png = b""
                session.protocol_secret = ""
                session.interactive_frame = b""
                session.official_window_open = False
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
