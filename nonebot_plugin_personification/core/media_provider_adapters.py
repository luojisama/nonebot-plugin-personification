from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


MEDIA_PROTOCOL_AUTO = "auto"
MEDIA_PROTOCOL_NONE = "none"
MEDIA_PROTOCOL_GEMINI = "gemini_native"
MEDIA_PROTOCOL_ANTIGRAVITY = "antigravity_native"
MEDIA_PROTOCOL_QWEN = "openai_qwen_omni"
MEDIA_PROTOCOL_MIMO = "openai_mimo_v25"

_KNOWN_PROTOCOLS = {
    MEDIA_PROTOCOL_AUTO,
    MEDIA_PROTOCOL_NONE,
    MEDIA_PROTOCOL_GEMINI,
    MEDIA_PROTOCOL_ANTIGRAVITY,
    MEDIA_PROTOCOL_QWEN,
    MEDIA_PROTOCOL_MIMO,
}


@dataclass(frozen=True)
class MediaProviderAdapter:
    protocol: str
    supports_video: bool
    supports_audio: bool
    local_transport: str
    source: str


def normalize_media_protocol(value: Any) -> str:
    normalized = str(value or MEDIA_PROTOCOL_AUTO).strip().lower().replace("-", "_")
    aliases = {
        "disabled": MEDIA_PROTOCOL_NONE,
        "text_only": MEDIA_PROTOCOL_NONE,
        "gemini": MEDIA_PROTOCOL_GEMINI,
        "gemini_official": MEDIA_PROTOCOL_GEMINI,
        "antigravity": MEDIA_PROTOCOL_ANTIGRAVITY,
        "agy": MEDIA_PROTOCOL_ANTIGRAVITY,
        "agy_native": MEDIA_PROTOCOL_ANTIGRAVITY,
        "qwen": MEDIA_PROTOCOL_QWEN,
        "qwen_omni": MEDIA_PROTOCOL_QWEN,
        "mimo": MEDIA_PROTOCOL_MIMO,
        "mimo_v25": MEDIA_PROTOCOL_MIMO,
        "mimo_v2_5": MEDIA_PROTOCOL_MIMO,
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _KNOWN_PROTOCOLS else MEDIA_PROTOCOL_AUTO


def _adapter(protocol: str, *, source: str) -> MediaProviderAdapter:
    if protocol == MEDIA_PROTOCOL_GEMINI:
        return MediaProviderAdapter(protocol, True, True, "files_api", source)
    if protocol == MEDIA_PROTOCOL_ANTIGRAVITY:
        return MediaProviderAdapter(protocol, True, True, "inlineData_or_fileData", source)
    if protocol in {MEDIA_PROTOCOL_QWEN, MEDIA_PROTOCOL_MIMO}:
        return MediaProviderAdapter(protocol, True, True, "base64_or_url", source)
    return MediaProviderAdapter(MEDIA_PROTOCOL_NONE, False, False, "none", source)


def _is_google_gemini_endpoint(api_url: str) -> bool:
    raw = str(api_url or "").strip()
    if not raw:
        return True
    try:
        host = str(urlsplit(raw).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "generativelanguage.googleapis.com" or host.endswith(".googleapis.com")


def _official_protocol(api_type: str, api_url: str, model: str) -> str:
    api = str(api_type or "").strip().lower().replace("-", "_")
    model_text = str(model or "").strip().lower().replace("_", "-")
    if any(token in model_text for token in ("asr", "tts", "embedding", "embed")):
        return MEDIA_PROTOCOL_NONE
    if model_text.startswith(("qwen3.5-omni-", "qwen3-omni-flash")):
        return MEDIA_PROTOCOL_QWEN
    if model_text == "mimo-v2.5":
        return MEDIA_PROTOCOL_MIMO
    # AGY 模型目录没有稳定的远程能力探测契约；auto 模式保持 fail-closed。
    if (
        api in {"gemini", "gemini_official"}
        and model_text.startswith("gemini-")
        and _is_google_gemini_endpoint(api_url)
    ):
        return MEDIA_PROTOCOL_GEMINI
    return MEDIA_PROTOCOL_NONE


def resolve_media_provider_adapter(provider: Mapping[str, Any] | None) -> MediaProviderAdapter:
    payload = provider or {}
    configured = normalize_media_protocol(payload.get("media_protocol", MEDIA_PROTOCOL_AUTO))
    if configured != MEDIA_PROTOCOL_AUTO:
        return _adapter(configured, source="explicit")
    inferred = _official_protocol(
        str(payload.get("api_type", "") or ""),
        str(payload.get("api_url", "") or ""),
        str(payload.get("model", "") or ""),
    )
    return _adapter(
        inferred,
        source="official_preset" if inferred != MEDIA_PROTOCOL_NONE else "unsupported",
    )


__all__ = [
    "MEDIA_PROTOCOL_AUTO",
    "MEDIA_PROTOCOL_ANTIGRAVITY",
    "MEDIA_PROTOCOL_GEMINI",
    "MEDIA_PROTOCOL_MIMO",
    "MEDIA_PROTOCOL_NONE",
    "MEDIA_PROTOCOL_QWEN",
    "MediaProviderAdapter",
    "normalize_media_protocol",
    "resolve_media_provider_adapter",
]
