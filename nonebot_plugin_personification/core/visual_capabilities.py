from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

VISUAL_ROUTE_REPLY_PLAIN = "reply_plain"
VISUAL_ROUTE_REPLY_YAML = "reply_yaml"
VISUAL_ROUTE_AGENT = "agent"
VISUAL_ROUTE_VISION = "vision"

_VISION_ERROR_HINTS = (
    "unsupported image",
    "image input not supported",
    "invalid image_url",
    "content type image_url",
    "vision not supported",
    "multimodal not supported",
    "image input",
    "input_image",
)
_PROBE_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAIAAAADnC86AAAAWUlEQVR4nO3WIQ4AMQwDQcf//3MOpzAnbUG93Brqau2rH2PrUg5MZUw6CoxljpoFxjJHzQJjmaNeh0vaH6fuWm+tSzkwlTHpKDCWOWoWGMscNQuMZY7S4/AHSyEFTD7iBgAAAAAASUVORK5CYII="
)
_PROBE_TIMEOUT_SECONDS = 12.0
_PROBE_EXPECTED_COLOR_ORDER = "红绿蓝黄"
_VIDEO_CAPABILITY_CACHE: Dict[tuple[str, str, str], VisualCapabilityRecord] = {}


@dataclass
class VisualCapabilityRecord:
    supported: bool
    source: str
    checked_at: float
    detail: str = ""


_VISUAL_CAPABILITY_CACHE: Dict[tuple[str, str, str], VisualCapabilityRecord] = {}
_VISUAL_CAPABILITY_LOCK = threading.RLock()


def _normalize_signature(route_name: str, api_type: str, model: str | None = None) -> tuple[str, str, str]:
    return (
        str(route_name or "").strip().lower(),
        str(api_type or "").strip().lower().replace("-", "_"),
        str(model or "").strip().lower(),
    )


def set_visual_capability(
    route_name: str,
    api_type: str,
    model: str | None,
    supported: bool,
    *,
    source: str,
    detail: str = "",
) -> None:
    with _VISUAL_CAPABILITY_LOCK:
        _VISUAL_CAPABILITY_CACHE[_normalize_signature(route_name, api_type, model)] = VisualCapabilityRecord(
            supported=bool(supported),
            source=str(source or "").strip() or "unknown",
            checked_at=time.time(),
            detail=str(detail or "").strip(),
        )


def get_visual_capability(route_name: str, api_type: str, model: str | None = None) -> bool | None:
    with _VISUAL_CAPABILITY_LOCK:
        record = _VISUAL_CAPABILITY_CACHE.get(_normalize_signature(route_name, api_type, model))
    if record is None:
        return None
    return bool(record.supported)


def get_visual_capability_record(
    route_name: str,
    api_type: str,
    model: str | None = None,
) -> VisualCapabilityRecord | None:
    with _VISUAL_CAPABILITY_LOCK:
        return _VISUAL_CAPABILITY_CACHE.get(_normalize_signature(route_name, api_type, model))


def set_video_capability(
    route_name: str,
    api_type: str,
    model: str | None,
    supported: bool,
    *,
    source: str,
    detail: str = "",
) -> None:
    with _VISUAL_CAPABILITY_LOCK:
        _VIDEO_CAPABILITY_CACHE[_normalize_signature(route_name, api_type, model)] = VisualCapabilityRecord(
            supported=bool(supported),
            source=str(source or "").strip() or "unknown",
            checked_at=time.time(),
            detail=str(detail or "").strip(),
        )


def get_video_capability(route_name: str, api_type: str, model: str | None = None) -> bool | None:
    with _VISUAL_CAPABILITY_LOCK:
        record = _VIDEO_CAPABILITY_CACHE.get(_normalize_signature(route_name, api_type, model))
    if record is None:
        return None
    return bool(record.supported)


def error_indicates_vision_unavailable(error: Any) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    return any(hint in text for hint in _VISION_ERROR_HINTS)


def heuristic_supports_vision(api_type: str, model: str | None = None) -> bool:
    api = str(api_type or "").strip().lower().replace("-", "_")
    model_text = str(model or "").strip().lower()
    if api == "openai_codex":
        return "codex" in model_text
    if api in {"openai", "openai_like", "openai_compatible", "anthropic", "gemini", "gemini_official"}:
        return True
    if "vision" in model_text:
        return True
    if any(token in model_text for token in ("gpt-4o", "gpt-5", "gemini", "claude")):
        return True
    return False


def _probe_response_matches_expected(content: str) -> bool:
    normalized = "".join(re.findall(r"[红绿蓝黄]", str(content or "").strip()))
    return normalized == _PROBE_EXPECTED_COLOR_ORDER


def heuristic_supports_video(api_type: str, model: str | None = None) -> bool:
    api = str(api_type or "").strip().lower().replace("-", "_")
    model_text = str(model or "").strip().lower()
    if api in {"gemini", "gemini_official"}:
        return True
    return "gemini" in model_text


def provider_supports_vision(
    api_type: str,
    model: str | None = None,
    *,
    route_name: str = "",
) -> bool:
    if route_name:
        cached = get_visual_capability(route_name, api_type, model)
        if cached is not None:
            return cached
    return heuristic_supports_vision(api_type, model)


def provider_supports_video(
    api_type: str,
    model: str | None = None,
    *,
    route_name: str = "",
) -> bool:
    if route_name:
        cached = get_video_capability(route_name, api_type, model)
        if cached is not None:
            return cached
    return heuristic_supports_video(api_type, model)


def _clone_provider_config(original: Any, provider: Dict[str, Any]) -> Any:
    class _ConfigProxy:
        def __init__(self, base: Any, selected: Dict[str, Any]) -> None:
            self._base = base
            self._selected = selected

        def __getattr__(self, name: str) -> Any:
            if name == "personification_api_type":
                return self._selected.get("api_type", "openai")
            if name == "personification_api_url":
                return self._selected.get("api_url", "")
            if name == "personification_api_key":
                return self._selected.get("api_key", "")
            if name == "personification_model":
                return self._selected.get("model", "")
            if name == "personification_codex_auth_path":
                return self._selected.get("auth_path", "")
            return getattr(self._base, name)

    return _ConfigProxy(original, provider)


def build_primary_route_probe_caller(
    *,
    plugin_config: Any,
    get_configured_api_providers: Callable[[], list[dict[str, Any]]],
) -> tuple[Any | None, str, str]:
    from ..skills.skillpacks.tool_caller.scripts.impl import build_tool_caller

    providers = list(get_configured_api_providers() or [])
    if providers:
        primary = providers[0]
        caller = build_tool_caller(_clone_provider_config(plugin_config, primary))
        return caller, str(primary.get("api_type", "") or ""), str(primary.get("model", "") or "")

    api_type = str(getattr(plugin_config, "personification_api_type", "") or "")
    model = str(getattr(plugin_config, "personification_model", "") or "")
    if not api_type and not model:
        return None, "", ""
    return build_tool_caller(plugin_config), api_type, model


async def probe_tool_caller_vision(
    *,
    route_name: str,
    caller: Any,
    api_type: str,
    model: str,
    logger: Any,
) -> bool | None:
    if caller is None or not api_type:
        return None

    from .message_parts import build_user_message_content

    messages = [
        {
            "role": "user",
            "content": build_user_message_content(
                text=(
                    "这是一张 4 宫格纯色图片。"
                    "请只按“左上、右上、左下、右下”的顺序回答颜色。"
                    "只能输出四个汉字，不要解释，不要标点。"
                ),
                image_urls=[_PROBE_IMAGE_DATA_URL],
                image_detail="low",
            ),
        }
    ]
    try:
        response = await asyncio.wait_for(
            caller.chat_with_tools(messages, [], False),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if error_indicates_vision_unavailable(exc):
            set_visual_capability(
                route_name,
                api_type,
                model,
                False,
                source="startup_probe",
                detail=str(exc),
            )
            return False
        logger.warning(f"[vision] startup probe failed route={route_name} type={api_type} model={model}: {exc}")
        return None

    content = str(getattr(response, "content", "") or "").strip()
    supported = (
        not bool(getattr(response, "vision_unavailable", False))
        and _probe_response_matches_expected(content)
    )
    set_visual_capability(
        route_name,
        api_type,
        model,
        supported,
        source="startup_probe",
        detail=content[:80] or str(getattr(response, "finish_reason", "") or ""),
    )
    return supported


async def probe_vision_caller(
    *,
    route_name: str,
    caller: Any,
    api_type: str,
    model: str,
    logger: Any,
) -> bool | None:
    if caller is None or not api_type:
        return None
    try:
        result = await asyncio.wait_for(
            caller.describe("请直接回答 ok", _PROBE_IMAGE_DATA_URL),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if error_indicates_vision_unavailable(exc):
            set_visual_capability(
                route_name,
                api_type,
                model,
                False,
                source="startup_probe",
                detail=str(exc),
            )
            return False
        logger.warning(f"[vision] startup probe failed route={route_name} type={api_type} model={model}: {exc}")
        return None

    set_visual_capability(
        route_name,
        api_type,
        model,
        True,
        source="startup_probe",
        detail=str(result or "").strip()[:80],
    )
    return True
