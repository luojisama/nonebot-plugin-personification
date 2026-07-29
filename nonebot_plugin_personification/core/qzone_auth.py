from __future__ import annotations

import asyncio
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx


_QR_SHOW_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
_QR_LOGIN_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
_LOGIN_SUCCESS_URL = "https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone"
_QZONE_APP_ID = "549000912"
_QZONE_DAID = "5"
_POLL_INTERVAL_SECONDS = 2.0
_SESSION_TTL_SECONDS = 150.0
_TERMINAL_RETENTION_SECONDS = 300.0
_START_COOLDOWN_SECONDS = 5.0
_MAX_REDIRECTS = 8
_TERMINAL_STATES = {
    "success",
    "expired",
    "cancelled",
    "risk_controlled",
    "account_mismatch",
    "failed",
}
_CALLBACK_ARG_RE = re.compile(r"'((?:\\.|[^'\\])*)'")

CookieInstaller = Callable[[str, str, str], Awaitable[tuple[bool, str]]]
ClientFactory = Callable[[], httpx.AsyncClient]


def _ptqrtoken(qrsig: str) -> int:
    value = 0
    for char in str(qrsig or ""):
        value = value * 33 + ord(char)
    return value & 0x7FFFFFFF


def _parse_ptui_callback(raw_text: str) -> tuple[str, str, str]:
    text = str(raw_text or "").strip()
    if not text.startswith("ptuiCB("):
        raise ValueError("腾讯登录响应格式已变化")
    args = [match.group(1).replace("\\'", "'").replace("\\\\", "\\") for match in _CALLBACK_ARG_RE.finditer(text)]
    if len(args) < 5:
        raise ValueError("腾讯登录响应字段不完整")
    return args[0], args[2], args[4]


def _validate_login_url(url: str, *, initial: bool = False) -> str:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in {
        "ptlogin2.qzone.qq.com",
        "ssl.ptlogin2.qq.com",
        "qzs.qq.com",
        "qzs.qzone.qq.com",
    }:
        raise ValueError("腾讯登录跳转地址不受信任")
    if initial and parsed.path.rstrip("/") != "/check_sig":
        raise ValueError("腾讯登录回调路径不受信任")
    return parsed.geturl()


def _cookie_header(client: httpx.AsyncClient) -> str:
    selected: dict[str, str] = {}
    for cookie in client.cookies.jar:
        value = str(cookie.value or "").strip()
        if not value or cookie.name in {"qrsig", "pt_login_sig"}:
            continue
        if cookie.is_expired():
            continue
        selected[str(cookie.name)] = value
    return "; ".join(f"{name}={value}" for name, value in selected.items())


@dataclass(slots=True)
class QzoneLoginSession:
    session_id: str
    bot_id: str
    owner_key: str
    created_at: float
    expires_at: float
    status: str = "preparing"
    message: str = "正在生成二维码"
    qr_png: bytes = b""
    qrsig: str = ""
    client: httpx.AsyncClient | None = None
    task: asyncio.Task[None] | None = None
    updated_at: float = field(default_factory=time.time)

    def public_status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "session_id": self.session_id,
            "bot_id": self.bot_id,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "expires_in_seconds": max(0, int(self.expires_at - now)),
            "terminal": self.status in _TERMINAL_STATES,
            "qr_ready": bool(self.qr_png) and self.status not in _TERMINAL_STATES,
        }


class QzoneLoginManager:
    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        session_ttl: float = _SESSION_TTL_SECONDS,
    ) -> None:
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=10.0),
                follow_redirects=False,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                    ),
                },
            )
        )
        self._poll_interval = max(0.01, float(poll_interval))
        self._session_ttl = max(30.0, float(session_ttl))
        self._lock = threading.RLock()
        self._sessions: dict[str, QzoneLoginSession] = {}
        self._active_by_bot: dict[str, str] = {}
        self._last_start_by_owner: dict[str, float] = {}

    async def start(
        self,
        *,
        bot_id: str,
        owner_key: str,
        install_cookie: CookieInstaller,
    ) -> dict[str, Any]:
        bot = str(bot_id or "").strip()
        owner = str(owner_key or "").strip()
        if not bot.isdigit() or not owner:
            raise ValueError("登录目标无效")
        now = time.time()
        with self._lock:
            active_id = self._active_by_bot.get(bot, "")
            active_session = self._sessions.get(active_id)
            if (
                active_session is not None
                and active_session.status not in _TERMINAL_STATES
                and not secrets.compare_digest(active_session.owner_key, owner)
            ):
                raise RuntimeError("该 Bot 正由另一位管理员执行登录恢复")
            last_start = float(self._last_start_by_owner.get(owner, 0.0) or 0.0)
            if now - last_start < _START_COOLDOWN_SECONDS:
                raise RuntimeError("登录请求过于频繁，请稍后重试")
            self._last_start_by_owner[owner] = now
        await self.cancel_bot(bot)
        self._prune()

        session = QzoneLoginSession(
            session_id=secrets.token_urlsafe(24),
            bot_id=bot,
            owner_key=owner,
            created_at=now,
            expires_at=now + self._session_ttl,
        )
        client = self._client_factory()
        session.client = client
        with self._lock:
            self._sessions[session.session_id] = session
            self._active_by_bot[bot] = session.session_id

        try:
            response = await client.get(
                _QR_SHOW_URL,
                params={
                    "appid": _QZONE_APP_ID,
                    "e": "2",
                    "l": "M",
                    "s": "3",
                    "d": "72",
                    "v": "4",
                    "t": str(time.time()),
                    "daid": _QZONE_DAID,
                    "pt_3rd_aid": "0",
                    "u1": _LOGIN_SUCCESS_URL,
                },
            )
            response.raise_for_status()
            qrsig = str(response.cookies.get("qrsig") or client.cookies.get("qrsig") or "")
            if not qrsig or not response.content:
                raise ValueError("腾讯未返回有效登录二维码")
            session.qrsig = qrsig
            session.qr_png = bytes(response.content)
            self._set_state(session, "waiting_scan", "请使用手机 QQ 扫描二维码")
            session.task = asyncio.create_task(
                self._poll(session, install_cookie),
                name=f"personification-qzone-login-{bot}",
            )
            return session.public_status()
        except Exception:
            self._set_state(session, "failed", "生成登录二维码失败，请稍后重试")
            session.qr_png = b""
            session.qrsig = ""
            await self._close_client(session)
            with self._lock:
                if self._active_by_bot.get(bot) == session.session_id:
                    self._active_by_bot.pop(bot, None)
            raise

    def status(self, session_id: str, *, owner_key: str) -> dict[str, Any]:
        session = self._owned_session(session_id, owner_key)
        return session.public_status()

    def qrcode(self, session_id: str, *, owner_key: str) -> bytes:
        session = self._owned_session(session_id, owner_key)
        if not session.qr_png or session.status in _TERMINAL_STATES:
            raise LookupError("二维码不可用")
        return bytes(session.qr_png)

    async def cancel(self, session_id: str, *, owner_key: str) -> dict[str, Any]:
        session = self._owned_session(session_id, owner_key)
        await self._cancel_session(session)
        return session.public_status()

    async def cancel_bot(self, bot_id: str) -> None:
        with self._lock:
            session_id = self._active_by_bot.get(str(bot_id or ""))
            session = self._sessions.get(session_id or "")
        if session is not None:
            await self._cancel_session(session)

    async def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.status not in _TERMINAL_STATES:
                await self._cancel_session(session)

    async def _poll(self, session: QzoneLoginSession, install_cookie: CookieInstaller) -> None:
        try:
            while time.time() < session.expires_at:
                await asyncio.sleep(self._poll_interval)
                if session.status in _TERMINAL_STATES or session.client is None:
                    return
                response = await session.client.get(
                    _QR_LOGIN_URL,
                    params={
                        "u1": _LOGIN_SUCCESS_URL,
                        "ptqrtoken": str(_ptqrtoken(session.qrsig)),
                        "ptredirect": "0",
                        "h": "1",
                        "t": "1",
                        "g": "1",
                        "from_ui": "1",
                        "ptlang": "2052",
                        "action": f"0-0-{int(time.time() * 1000)}",
                        "js_ver": "22070111",
                        "js_type": "1",
                        "login_sig": "",
                        "pt_uistyle": "40",
                        "aid": _QZONE_APP_ID,
                        "daid": _QZONE_DAID,
                        "has_onekey": "1",
                    },
                )
                response.raise_for_status()
                code, callback_url, _message = _parse_ptui_callback(response.text)
                if code == "66":
                    self._set_state(session, "waiting_scan", "请使用手机 QQ 扫描二维码")
                    continue
                if code == "67":
                    self._set_state(session, "waiting_confirm", "已扫码，请在手机 QQ 中确认登录")
                    continue
                if code == "65":
                    self._set_state(session, "expired", "二维码已过期，请重新生成")
                    return
                if code == "68":
                    self._set_state(session, "cancelled", "本次登录已在手机端取消")
                    return
                if code != "0":
                    self._set_state(session, "risk_controlled", "腾讯拒绝了本次登录，请稍后重试或使用人工兜底")
                    return

                self._set_state(session, "verifying", "登录已确认，正在验证 QZone 凭证")
                await self._follow_login_redirects(session.client, callback_url)
                cookie = _cookie_header(session.client)
                ok, message = await install_cookie(cookie, session.bot_id, "ptlogin")
                if ok:
                    self._set_state(session, "success", "QZone 登录已恢复")
                elif message == "account_mismatch":
                    self._set_state(session, "account_mismatch", "扫码 QQ 与当前 Bot QQ 不一致")
                else:
                    self._set_state(session, "failed", "登录凭证验证失败，请重新扫码")
                return
            self._set_state(session, "expired", "二维码已过期，请重新生成")
        except asyncio.CancelledError:
            if session.status not in _TERMINAL_STATES:
                self._set_state(session, "cancelled", "登录已取消")
            raise
        except Exception:
            self._set_state(session, "failed", "登录会话异常，请稍后重试")
        finally:
            session.qr_png = b""
            session.qrsig = ""
            await self._close_client(session)
            with self._lock:
                if self._active_by_bot.get(session.bot_id) == session.session_id:
                    self._active_by_bot.pop(session.bot_id, None)

    async def _follow_login_redirects(self, client: httpx.AsyncClient, callback_url: str) -> None:
        current = _validate_login_url(callback_url, initial=True)
        for _ in range(_MAX_REDIRECTS + 1):
            response = await client.get(current, follow_redirects=False)
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.raise_for_status()
                return
            location = str(response.headers.get("location") or "").strip()
            if not location:
                raise ValueError("腾讯登录跳转缺少目标地址")
            current = _validate_login_url(urljoin(current, location))
        raise ValueError("腾讯登录跳转次数过多")

    async def _cancel_session(self, session: QzoneLoginSession) -> None:
        if session.status in _TERMINAL_STATES:
            return
        task = session.task
        self._set_state(session, "cancelled", "登录已取消")
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            await self._close_client(session)
        session.qr_png = b""
        session.qrsig = ""
        with self._lock:
            if self._active_by_bot.get(session.bot_id) == session.session_id:
                self._active_by_bot.pop(session.bot_id, None)

    async def _close_client(self, session: QzoneLoginSession) -> None:
        client = session.client
        session.client = None
        if client is not None:
            await client.aclose()

    def _owned_session(self, session_id: str, owner_key: str) -> QzoneLoginSession:
        self._prune()
        with self._lock:
            session = self._sessions.get(str(session_id or ""))
        if session is None or not secrets.compare_digest(session.owner_key, str(owner_key or "")):
            raise LookupError("登录会话不存在")
        return session

    def _set_state(self, session: QzoneLoginSession, status: str, message: str) -> None:
        with self._lock:
            session.status = status
            session.message = message
            session.updated_at = time.time()

    def _prune(self) -> None:
        cutoff = time.time() - _TERMINAL_RETENTION_SECONDS
        with self._lock:
            stale = [
                session_id
                for session_id, session in self._sessions.items()
                if session.status in _TERMINAL_STATES and session.updated_at < cutoff
            ]
            for session_id in stale:
                self._sessions.pop(session_id, None)


qzone_login_manager = QzoneLoginManager()


__all__ = [
    "QzoneLoginManager",
    "_parse_ptui_callback",
    "_ptqrtoken",
    "_validate_login_url",
    "qzone_login_manager",
]
