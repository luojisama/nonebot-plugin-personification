from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_HEADER_PATTERN = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]*"
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[\s_-]?key|access[\s_-]?token|refresh[\s_-]?token|token|csrf|"
    r"client[_-]?secret|password|passwd|session(?:[_-]?id)?|p_skey|skey)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}&]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_BASIC_PATTERN = re.compile(r"(?i)\bbasic\s+([a-z0-9+/=_-]+)")
_OPENAI_KEY_PATTERN = re.compile(r"(?i)\b(sk-[a-z0-9_-]{8})[a-z0-9_-]+")
_URL_PATTERN = re.compile(r"(?i)https?://[^\s\"'<>]+")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"key|token|secret|cookie|authorization|csrf|password|passwd|session|p_skey|skey|"
    r"credential|headers?|proxy|signature|sig|oauth[_-]?code)(?![a-z0-9])"
)
_SENSITIVE_QUERY_KEY_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"key|token|secret|signature|sig|credential|oauth[_-]?code|code)(?![a-z0-9])"
)
_SENSITIVE_PATH_MARKER = re.compile(
    r"(?i)^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|key|token|"
    r"secret|signature|sig|credential|oauth[_-]?code|code)$"
)
_OPAQUE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~+/=-]{32,}$")


def _sanitize_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if not host:
            return "***" + trailing
        rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ""
        netloc = f"{rendered_host}{port}"
        if parsed.username is not None or parsed.password is not None:
            netloc = f"***@{netloc}"
        query = urlencode([
            (key, "***" if _SENSITIVE_QUERY_KEY_PATTERN.search(key) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        path_parts = parsed.path.split("/")
        sanitized_parts: list[str] = []
        redact_next = False
        for part in path_parts:
            if redact_next or _OPAQUE_PATH_SEGMENT.fullmatch(part):
                sanitized_parts.append("***")
                redact_next = False
                continue
            key, separator, _value = part.partition("=")
            if separator and _SENSITIVE_PATH_MARKER.fullmatch(key):
                sanitized_parts.append(f"{key}=***")
                continue
            sanitized_parts.append(part)
            redact_next = bool(_SENSITIVE_PATH_MARKER.fullmatch(part))
        path = "/".join(sanitized_parts)
        fragment = "***" if _SENSITIVE_KEY_PATTERN.search(parsed.fragment) else parsed.fragment
        return urlunsplit((parsed.scheme, netloc, path, query, fragment)) + trailing
    except Exception:
        return "***" + trailing


def _sanitize_basic_auth(match: re.Match[str]) -> str:
    token = match.group(1)
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return match.group(0)
    return "Basic ***" if b":" in decoded else match.group(0)


def sanitize_text(value: Any, *, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) > limit:
        text = text[: max(0, limit - 12)] + "...<truncated>"
    text = _URL_PATTERN.sub(_sanitize_url, text)
    text = _HEADER_PATTERN.sub(lambda match: f"{match.group(1)}: ***", text)
    text = _KEY_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}***", text)
    text = _BEARER_PATTERN.sub("Bearer ***", text)
    text = _BASIC_PATTERN.sub(_sanitize_basic_auth, text)
    text = _OPENAI_KEY_PATTERN.sub(r"\1***", text)
    return text


def sanitize_object(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<nested>"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            key_text = str(key or "")[:120]
            output[key_text] = "***" if _SENSITIVE_KEY_PATTERN.search(key_text) else sanitize_object(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set)):
        return [sanitize_object(item, depth=depth + 1) for item in list(value)[:80]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(value)


def contains_sensitive_value(value: Any) -> bool:
    text = str(value or "")
    return bool(
        _HEADER_PATTERN.search(text)
        or _KEY_VALUE_PATTERN.search(text)
        or _BEARER_PATTERN.search(text)
        or _BASIC_PATTERN.sub(_sanitize_basic_auth, text) != text
    )


__all__ = ["contains_sensitive_value", "sanitize_object", "sanitize_text"]
