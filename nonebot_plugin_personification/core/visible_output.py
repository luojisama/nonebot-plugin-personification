from __future__ import annotations

import hashlib
import re
from base64 import b64decode
from binascii import Error as BinasciiError
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .role_integrity import detect_persona_identity_leak

_CONTROL_OUTPUTS = frozenset({"[NO_REPLY]", "<NO_REPLY>", "[SILENCE]", "<SILENCE>"})
_CONTROL_OUTPUT_RE = re.compile(r"(?:\[(?:NO_REPLY|SILENCE)\]|<(?:NO_REPLY|SILENCE)>)", re.IGNORECASE)
_MEDIA_TAGS = {"IMAGE_B64", "IMAGE_URL", "AUDIO_B64"}
_MEDIA_BLOCK_RE = re.compile(
    r"\[(IMAGE_B64|IMAGE_URL|AUDIO_B64)\](.*?)\[/\1\]",
    re.DOTALL,
)
_ANY_MEDIA_MARKER_RE = re.compile(r"\[/?(?:IMAGE_B64|IMAGE_URL|AUDIO_B64)\]")
_INTERNAL_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal_tag", re.compile(r"</?(?:think|output|message|status|action)>", re.IGNORECASE)),
    ("provider_policy_text", re.compile(r"(?:严重|高危)?违规安全限制(?:已)?被触发")),
    (
        "provider_policy_text",
        re.compile(r"(?:服务器|服务端|供应商|provider).{0,16}(?:拦截|风控|安全策略)", re.IGNORECASE),
    ),
    (
        "provider_policy_text",
        re.compile(r"(?:finish[_ ]?reason|block[_ ]?reason|content[_ ]?filter)\s*[:=]", re.IGNORECASE),
    ),
    ("internal_tag", re.compile(r"(?:system|developer)\s+(?:prompt|message)\s*[:=]", re.IGNORECASE)),
    ("internal_error", re.compile(r"(?:traceback|stack trace|internal server error)\b", re.IGNORECASE)),
    (
        "credential_like_text",
        re.compile(r"(?:api[_ -]?key|authorization)\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9_.-]{8,}", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class VisibleOutputDecision:
    allowed: bool
    text: str
    reason: str = ""
    pattern_id: str = ""
    summary_hash: str = ""


def _blocked_decision(candidate: str, reason: str, pattern_id: str) -> VisibleOutputDecision:
    return VisibleOutputDecision(
        False,
        "",
        reason,
        pattern_id,
        hashlib.sha256(str(candidate or "").encode("utf-8", errors="replace")).hexdigest()[:12],
    )


def _valid_media_payload(kind: str, payload: str) -> bool:
    value = str(payload or "").strip()
    if not value:
        return False
    if kind == "IMAGE_URL":
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    try:
        decoded = b64decode(value, validate=True)
    except (ValueError, BinasciiError):
        return False
    return 0 < len(decoded) <= 20 * 1024 * 1024


def _visible_residual(candidate: str, *, allow_direct_media: bool) -> tuple[str, str]:
    blocks = list(_MEDIA_BLOCK_RE.finditer(candidate))
    if not blocks:
        if _ANY_MEDIA_MARKER_RE.search(candidate):
            return "", "invalid_media_block"
        return candidate, ""
    if not allow_direct_media:
        return "", "media_not_allowed"
    for block in blocks:
        if block.group(1) not in _MEDIA_TAGS or not _valid_media_payload(block.group(1), block.group(2)):
            return "", "invalid_media_payload"
    residual = _MEDIA_BLOCK_RE.sub(" ", candidate)
    if _ANY_MEDIA_MARKER_RE.search(residual):
        return "", "invalid_media_block"
    return residual.strip(), ""


def assess_visible_text(
    text: Any,
    *,
    allow_control: bool = True,
    allow_direct_media: bool = True,
    enforce_role_integrity: bool = True,
    identity_context: Any = None,
) -> VisibleOutputDecision:
    candidate = str(text or "").strip()
    if not candidate:
        return _blocked_decision(candidate, "empty", "empty_output")
    if candidate in _CONTROL_OUTPUTS:
        if allow_control:
            return VisibleOutputDecision(True, candidate)
        return _blocked_decision(candidate, "control_not_allowed", "control_output")
    if not allow_control and _CONTROL_OUTPUT_RE.search(candidate):
        return _blocked_decision(candidate, "control_not_allowed", "control_output")
    residual, media_error = _visible_residual(candidate, allow_direct_media=allow_direct_media)
    if media_error:
        return _blocked_decision(candidate, media_error, media_error)
    for pattern_id, pattern in _INTERNAL_OUTPUT_PATTERNS:
        if pattern.search(residual):
            return _blocked_decision(candidate, "internal_or_policy_output", pattern_id)
    if enforce_role_integrity and detect_persona_identity_leak(residual, identity_context):
        return _blocked_decision(candidate, "persona_identity_leak", "persona_identity_leak")
    return VisibleOutputDecision(True, candidate)


def guard_visible_text(
    text: Any,
    *,
    logger: Any = None,
    surface: str = "",
    allow_direct_media: bool = True,
    allow_control: bool = False,
    enforce_role_integrity: bool = True,
    identity_context: Any = None,
) -> str:
    decision = assess_visible_text(
        text,
        allow_control=allow_control,
        allow_direct_media=allow_direct_media,
        enforce_role_integrity=enforce_role_integrity,
        identity_context=identity_context,
    )
    if decision.allowed:
        return decision.text
    if logger is not None:
        try:
            logger.warning(
                f"[visible_output] blocked surface={surface or 'unknown'} "
                f"reason={decision.reason} pattern_id={decision.pattern_id or '-'} "
                f"chars={len(str(text or ''))} summary_hash={decision.summary_hash or '-'}"
            )
        except Exception:
            pass
    return ""


__all__ = [
    "VisibleOutputDecision",
    "assess_visible_text",
    "detect_persona_identity_leak",
    "guard_visible_text",
]
