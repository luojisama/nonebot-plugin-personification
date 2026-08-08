from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urlparse


_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://passport.bilibili.com/login",
}
_QR_HOST = "account.bilibili.com"
_QR_PATH = "/h5/account-h5/auth/scan-web"
_KNOWN_POLL_CODES = frozenset({0, 86038, 86090, 86101})


class BilibiliQrProtocolError(RuntimeError):
    """A safe, non-secret protocol failure code."""


@dataclass(frozen=True, repr=False)
class BilibiliQrChallenge:
    key: str
    qr_url: str


async def _json_response(response: Any, *, failure_code: str) -> dict[str, Any]:
    try:
        status = int(response.status)
    except Exception as exc:
        raise BilibiliQrProtocolError(failure_code) from exc
    if status != 200:
        raise BilibiliQrProtocolError(failure_code)
    try:
        payload = await response.json()
    except Exception as exc:
        raise BilibiliQrProtocolError(failure_code) from exc
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise BilibiliQrProtocolError(failure_code)
    return payload


def _validate_challenge(data: dict[str, Any]) -> BilibiliQrChallenge:
    key = data.get("qrcode_key")
    qr_url = data.get("url")
    if not isinstance(key, str) or len(key) != 32 or not key.isascii() or not key.isalnum():
        raise BilibiliQrProtocolError("bilibili_qr_generate_failed")
    if not isinstance(qr_url, str) or len(qr_url) > 2048:
        raise BilibiliQrProtocolError("bilibili_qr_generate_failed")
    try:
        parsed = urlparse(qr_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise BilibiliQrProtocolError("bilibili_qr_generate_failed") from exc
    if (
        parsed.scheme != "https"
        or hostname != _QR_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != _QR_PATH
        or query.get("qrcode_key") != [key]
    ):
        raise BilibiliQrProtocolError("bilibili_qr_generate_failed")
    return BilibiliQrChallenge(key=key, qr_url=qr_url)


async def generate_challenge(request: Any) -> BilibiliQrChallenge:
    try:
        response = await request.get(
            _GENERATE_URL,
            params={"source": "main_web"},
            headers=_REQUEST_HEADERS,
            timeout=15_000,
            max_redirects=0,
        )
    except Exception as exc:
        raise BilibiliQrProtocolError("bilibili_qr_generate_failed") from exc
    payload = await _json_response(response, failure_code="bilibili_qr_generate_failed")
    return _validate_challenge(payload["data"])


async def poll_challenge(request: Any, key: str) -> int:
    if not isinstance(key, str) or len(key) != 32 or not key.isascii() or not key.isalnum():
        raise BilibiliQrProtocolError("bilibili_qr_session_invalid")
    try:
        response = await request.get(
            _POLL_URL,
            params={"qrcode_key": key, "source": "main_web"},
            headers=_REQUEST_HEADERS,
            timeout=15_000,
            max_redirects=0,
        )
    except Exception as exc:
        raise BilibiliQrProtocolError("bilibili_qr_poll_failed") from exc
    payload = await _json_response(response, failure_code="bilibili_qr_poll_failed")
    code = payload["data"].get("code")
    if type(code) is not int or code not in _KNOWN_POLL_CODES:
        raise BilibiliQrProtocolError("bilibili_qr_unknown_state")
    return code


def render_qr_png(qr_url: str) -> bytes:
    # The official transaction URL is encoded locally. It never crosses a
    # third-party QR service and is never returned in JSON or written to disk.
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except Exception as exc:
        raise BilibiliQrProtocolError("qrcode_encoder_unavailable") from exc
    try:
        image = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        image.add_data(qr_url)
        image.make(fit=True)
        rendered = image.make_image(fill_color="black", back_color="white")
        output = BytesIO()
        rendered.save(output, format="PNG")
        data = output.getvalue()
    except Exception as exc:
        raise BilibiliQrProtocolError("qrcode_encoder_failed") from exc
    if not data or len(data) > 1024 * 1024:
        raise BilibiliQrProtocolError("qrcode_encoder_failed")
    return data


__all__ = [
    "BilibiliQrChallenge",
    "BilibiliQrProtocolError",
    "generate_challenge",
    "poll_challenge",
    "render_qr_png",
]
