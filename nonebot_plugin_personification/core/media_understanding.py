from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Sequence

import httpx

from .ai_routes import resolve_video_fallback_provider
from .image_input import is_image_input_unsupported_error, provider_supports_vision
from .media_refs import normalize_video_ref
from .message_parts import build_user_message_content
from .model_router import MODEL_ROLE_STICKER, get_model_override_for_role
from .visual_capabilities import VISUAL_ROUTE_AGENT, error_indicates_vision_unavailable, provider_supports_video
from ..skills.skillpacks.tool_caller.scripts.impl import build_tool_caller


_VIDEO_INLINE_MAX_BYTES = 20 * 1024 * 1024
_GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
_GENERIC_REFUSAL_TEXTS = {
    "i can't discuss that.",
    "i cant discuss that.",
    "i cannot discuss that.",
    "i'm sorry, but i can't discuss that.",
    "抱歉，我不能讨论这个。",
    "抱歉，我无法讨论这个。",
}


def _normalize_media_api_type(api_type: str) -> str:
    value = str(api_type or "").strip().lower().replace("-", "_")
    if value in {"gemini", "gemini_official"}:
        return "gemini_official"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    if value == "anthropic":
        return "anthropic"
    return "openai"


def _is_provider_usable(provider: dict[str, Any]) -> bool:
    api_type = _normalize_media_api_type(str(provider.get("api_type", "") or "openai"))
    model = str(provider.get("model", "") or "").strip()
    if not model:
        return False
    if api_type == "openai_codex":
        return True
    return bool(str(provider.get("api_key", "") or "").strip())


def _primary_provider_candidates(runtime: Any) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    getter = getattr(runtime, "get_configured_api_providers", None)
    if callable(getter):
        try:
            providers = [dict(item) for item in list(getter() or []) if isinstance(item, dict)]
        except Exception:
            providers = []
    if providers:
        return _apply_sticker_model_override(runtime, providers)

    primary = get_primary_provider_config(runtime)
    if _is_provider_usable(primary):
        payload = dict(primary)
        payload.setdefault("name", "legacy_primary")
        return _apply_sticker_model_override(runtime, [payload])
    return []


def _apply_sticker_model_override(runtime: Any, providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return providers
    model_override = get_model_override_for_role(plugin_config, MODEL_ROLE_STICKER)
    if not model_override:
        return providers
    patched: list[dict[str, Any]] = []
    for provider in providers:
        cloned = dict(provider)
        cloned["model"] = model_override
        patched.append(cloned)
    return patched


class _ProviderConfigProxy:
    def __init__(self, original: Any, provider: dict[str, Any]) -> None:
        self._original = original
        self._provider = dict(provider or {})

    def __getattr__(self, name: str) -> Any:
        if name == "personification_api_type":
            return self._provider.get("api_type", "openai")
        if name == "personification_api_url":
            return self._provider.get("api_url", "")
        if name == "personification_api_key":
            return self._provider.get("api_key", "")
        if name == "personification_model":
            return self._provider.get("model", "")
        if name == "personification_codex_auth_path":
            return self._provider.get("auth_path", "")
        if name == "personification_thinking_mode":
            return getattr(self._original, name, "none")
        return getattr(self._original, name)


def _invalid_media_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    return " ".join(value.lower().split()) in _GENERIC_REFUSAL_TEXTS


def _log_warning(runtime: Any, message: str) -> None:
    logger = getattr(runtime, "logger", None)
    if logger is None:
        return
    try:
        logger.warning(message)
    except Exception:
        pass


def get_primary_provider_config(runtime: Any) -> dict[str, str]:
    providers = []
    getter = getattr(runtime, "get_configured_api_providers", None)
    if callable(getter):
        try:
            providers = list(getter() or [])
        except Exception:
            providers = []
    if providers and isinstance(providers[0], dict):
        primary = providers[0]
        return {
            "api_type": str(primary.get("api_type", "") or ""),
            "api_url": str(primary.get("api_url", "") or ""),
            "api_key": str(primary.get("api_key", "") or ""),
            "model": str(primary.get("model", "") or ""),
            "auth_path": str(primary.get("auth_path", "") or ""),
        }
    plugin_config = getattr(runtime, "plugin_config", None)
    return {
        "api_type": str(getattr(plugin_config, "personification_api_type", "") or ""),
        "api_url": str(getattr(plugin_config, "personification_api_url", "") or ""),
        "api_key": str(getattr(plugin_config, "personification_api_key", "") or ""),
        "model": str(getattr(plugin_config, "personification_model", "") or ""),
        "auth_path": str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
    }


async def _try_primary_image_routes(
    *,
    runtime: Any,
    prompt: str,
    refs: Sequence[str],
    route_name: str,
    image_detail: str,
) -> str:
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return ""
    for provider in _primary_provider_candidates(runtime):
        api_type = str(provider.get("api_type", "") or "")
        model = str(provider.get("model", "") or "")
        provider_name = str(provider.get("name", "") or model or api_type or "primary")
        if not provider_supports_vision(api_type, model, route_name=route_name):
            continue
        try:
            caller = build_tool_caller(_ProviderConfigProxy(plugin_config, provider))
            response = await caller.chat_with_tools(
                messages=[
                    {
                        "role": "user",
                        "content": build_user_message_content(
                            text=prompt,
                            image_urls=list(refs),
                            image_detail=image_detail,
                        ),
                    }
                ],
                tools=[],
                use_builtin_search=False,
            )
        except Exception as exc:
            if not (is_image_input_unsupported_error(exc) or error_indicates_vision_unavailable(exc)):
                _log_warning(runtime, f"[vision] primary image route failed provider={provider_name}: {exc}")
            continue
        if bool(getattr(response, "vision_unavailable", False)):
            continue
        content = str(getattr(response, "content", "") or "").strip()
        if not _invalid_media_text(content):
            return content
    return ""


async def _try_primary_video_routes(
    *,
    runtime: Any,
    prompt: str,
    refs: Sequence[str],
    route_name: str,
) -> str:
    for provider in _primary_provider_candidates(runtime):
        api_type = str(provider.get("api_type", "") or "")
        model = str(provider.get("model", "") or "")
        provider_name = str(provider.get("name", "") or model or api_type or "primary")
        normalized_type = _normalize_media_api_type(api_type)
        if normalized_type != "gemini_official":
            continue
        if not str(provider.get("api_key", "") or "").strip():
            continue
        if not provider_supports_video(api_type, model, route_name=route_name):
            continue
        try:
            result = await _call_gemini_media(
                api_key=str(provider.get("api_key", "") or ""),
                base_url=str(provider.get("api_url", "") or ""),
                model=model or _GEMINI_DEFAULT_MODEL,
                prompt=prompt,
                video_refs=refs,
            )
        except Exception as exc:
            if not error_indicates_vision_unavailable(exc):
                _log_warning(runtime, f"[video] primary route failed provider={provider_name}: {exc}")
            continue
        if not _invalid_media_text(result):
            return str(result or "").strip()
    return ""


def get_primary_provider_signature(runtime: Any) -> tuple[str, str]:
    primary = get_primary_provider_config(runtime)
    return primary["api_type"], primary["model"]


def _build_video_fallback_provider_config(runtime: Any) -> dict[str, str] | None:
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return None
    resolution = resolve_video_fallback_provider(plugin_config, getattr(runtime, "logger", None), warn=True)
    if resolution is None:
        return None
    payload = dict(resolution.provider)
    normalized_type = _normalize_media_api_type(str(payload.get("api_type", "") or ""))
    if normalized_type not in {"gemini_official"}:
        return None
    return {
        "api_type": normalized_type,
        "api_url": str(payload.get("api_url", "") or "").strip(),
        "api_key": str(payload.get("api_key", "") or "").strip(),
        "model": str(payload.get("model", "") or "").strip() or _GEMINI_DEFAULT_MODEL,
        "auth_path": str(payload.get("auth_path", "") or "").strip(),
    }


def _gemini_endpoint(base_url: str, model: str) -> str:
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        raw = "https://generativelanguage.googleapis.com"
    lower = raw.lower()
    if lower.endswith("/v1beta"):
        root = raw
    elif lower.endswith("/v1"):
        root = f"{raw[:-3]}/v1beta"
    else:
        root = f"{raw}/v1beta"
    return f"{root}/models/{model}:generateContent"


def _gemini_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "x-goog-api-key": api_key,
    }


def _gemini_image_part(image_ref: str) -> dict[str, Any]:
    from .image_refs import normalize_image_ref
    from ..skills.skillpacks.tool_caller.scripts.impl import _split_data_url

    normalized_image_url, problem = normalize_image_ref(image_ref)
    if not normalized_image_url:
        raise ValueError(f"invalid_image_ref:{problem or 'unknown'}")
    parsed = _split_data_url(normalized_image_url)
    if parsed:
        mime_type, base64_data = parsed
        return {
            "inlineData": {
                "mimeType": mime_type,
                "data": base64_data,
            }
        }
    return {
        "fileData": {
            "mimeType": "image/*",
            "fileUri": normalized_image_url,
        }
    }


def _gemini_video_part(video_ref: str) -> dict[str, Any]:
    normalized_video_ref, problem = normalize_video_ref(video_ref)
    if not normalized_video_ref:
        raise ValueError(f"invalid_video_ref:{problem or 'unknown'}")
    if normalized_video_ref.startswith(("http://", "https://")):
        mime_type, _ = mimetypes.guess_type(normalized_video_ref)
        return {
            "fileData": {
                "mimeType": mime_type or "video/mp4",
                "fileUri": normalized_video_ref,
            }
        }

    path = Path(normalized_video_ref)
    payload = path.read_bytes()
    if len(payload) > _VIDEO_INLINE_MAX_BYTES:
        raise ValueError("video_file_too_large_for_inline_data")
    mime_type, _ = mimetypes.guess_type(str(path))
    return {
        "inlineData": {
            "mimeType": mime_type or "video/mp4",
            "data": base64.b64encode(payload).decode("ascii"),
        }
    }


async def _call_gemini_media(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    image_refs: Sequence[str] = (),
    video_refs: Sequence[str] = (),
) -> str:
    parts: list[dict[str, Any]] = [{"text": str(prompt or "").strip() or "请分析这段媒体内容"}]
    for ref in image_refs:
        parts.append(_gemini_image_part(str(ref or "").strip()))
    for ref in video_refs:
        parts.append(_gemini_video_part(str(ref or "").strip()))
    payload = {"contents": [{"role": "user", "parts": parts}]}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
        response = await client.post(
            _gemini_endpoint(base_url, model or _GEMINI_DEFAULT_MODEL),
            headers=_gemini_headers(api_key),
            json=payload,
        )
        response.raise_for_status()
        data = dict(response.json() or {})
    candidates = list((data.get("candidates") or []))
    if not candidates:
        return ""
    content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
    parts = list(content.get("parts") or []) if isinstance(content, dict) else []
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            texts.append(str(part.get("text", "")))
    return "".join(texts).strip()


async def analyze_images_with_route_or_fallback(
    *,
    runtime: Any,
    prompt: str,
    image_refs: Sequence[str],
    route_name: str = VISUAL_ROUTE_AGENT,
    image_detail: str = "low",
    fallback_vision_caller: Any = None,
) -> tuple[str, str]:
    refs = [str(item or "").strip() for item in image_refs if str(item or "").strip()]
    if not refs:
        return "", "missing_images"

    primary_result = await _try_primary_image_routes(
        runtime=runtime,
        prompt=prompt,
        refs=refs,
        route_name=route_name,
        image_detail=image_detail,
    )
    if primary_result:
        return primary_result, "route_direct"

    fallback = fallback_vision_caller or getattr(runtime, "vision_caller", None)
    if fallback is None:
        return "", "vision_unavailable"
    outputs: list[str] = []
    for ref in refs:
        try:
            output = await fallback.describe(prompt, ref)
        except Exception as exc:
            _log_warning(runtime, f"[vision] fallback image route failed: {exc}")
            continue
        if not _invalid_media_text(output):
            outputs.append(str(output or "").strip())
    result = "\n".join(part for part in outputs if str(part or "").strip()).strip()
    return (result, "vision_fallback" if result else "vision_unavailable")


async def analyze_videos_with_route_or_fallback(
    *,
    runtime: Any,
    prompt: str,
    video_refs: Sequence[str],
    route_name: str = VISUAL_ROUTE_AGENT,
) -> tuple[str, str]:
    refs = [str(item or "").strip() for item in video_refs if str(item or "").strip()]
    if not refs:
        return "", "missing_videos"
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None or not bool(getattr(plugin_config, "personification_video_understanding_enabled", False)):
        return "", "video_disabled"

    primary_result = await _try_primary_video_routes(
        runtime=runtime,
        prompt=prompt,
        refs=refs,
        route_name=route_name,
    )
    if primary_result:
        return primary_result, "video_route_direct"

    fallback = _build_video_fallback_provider_config(runtime)
    if not fallback or not fallback.get("api_key"):
        return "", "video_unavailable"
    try:
        result = await _call_gemini_media(
            api_key=fallback["api_key"],
            base_url=fallback.get("api_url", ""),
            model=fallback.get("model", "") or _GEMINI_DEFAULT_MODEL,
            prompt=prompt,
            video_refs=refs,
        )
    except Exception:
        return "", "video_unavailable"
    result_text = str(result or "").strip()
    if _invalid_media_text(result_text):
        return "", "video_unavailable"
    return result_text, "video_fallback"


__all__ = [
    "analyze_images_with_route_or_fallback",
    "analyze_videos_with_route_or_fallback",
    "get_primary_provider_config",
    "get_primary_provider_signature",
]
