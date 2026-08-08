from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx

from .ai_routes import resolve_video_fallback_provider
from .audio_transcription import resolve_transcription_settings, transcribe_audio_file
from .gemini_transport import raise_for_gemini_status, request_with_gemini_auth
from .image_input import is_image_input_unsupported_error, provider_supports_vision
from .sensitive_data import sanitize_text
from .media_refs import normalize_audio_ref, normalize_video_ref
from .media_provider_adapters import (
    MEDIA_PROTOCOL_ANTIGRAVITY,
    MEDIA_PROTOCOL_GEMINI,
    MEDIA_PROTOCOL_MIMO,
    MEDIA_PROTOCOL_QWEN,
    resolve_media_provider_adapter,
)
from .message_parts import build_user_message_content
from .model_router import MODEL_ROLE_STICKER, get_model_override_for_role
from .llm_context import use_single_attempt_retry_policy
from .visual_capabilities import VISUAL_ROUTE_AGENT, error_indicates_vision_unavailable
from .video_understanding import prepare_video_storyboard


def _record_media_attempt(
    attempts: list[dict[str, Any]] | None,
    *,
    route: str,
    status: str,
    started_at: float,
    diagnostic_code: str = "",
    diagnostic_stage: str = "",
) -> None:
    if attempts is None:
        return
    attempts.append(
        {
            "route": str(route or ""),
            "status": str(status or ""),
            "elapsed_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            "diagnostic_code": str(diagnostic_code or ""),
            "diagnostic_stage": str(diagnostic_stage or ""),
        }
    )


def _gemini_web_automatic_enabled(config: Any) -> bool:
    return bool(getattr(config, "personification_gemini_web_enabled", False)) and bool(
        getattr(config, "personification_gemini_web_risk_acknowledged", False)
    )


def _mimo_web_asr_automatic_enabled(config: Any) -> bool:
    return bool(getattr(config, "personification_mimo_web_asr_enabled", False)) and bool(
        getattr(config, "personification_mimo_web_asr_risk_acknowledged", False)
    )


def build_tool_caller(config: Any) -> Any:
    from ..skills.skillpacks.tool_caller.scripts.impl import build_tool_caller

    return build_tool_caller(config)


def _build_tool_caller(config: Any) -> Any:
    return build_tool_caller(config)


_VIDEO_INLINE_MAX_BYTES = 20 * 1024 * 1024
_QWEN_BASE64_MAX_BYTES = 10 * 1024 * 1024
_QWEN_RAW_INLINE_MAX_BYTES = (_QWEN_BASE64_MAX_BYTES * 3 // 4) - 4
# Kept as an internal compatibility alias for focused tests and older imports.
_QWEN_INLINE_MAX_BYTES = _QWEN_RAW_INLINE_MAX_BYTES
_MIMO_BASE64_MAX_BYTES = 50 * 1024 * 1024
_MIMO_RAW_INLINE_MAX_BYTES = (_MIMO_BASE64_MAX_BYTES * 3 // 4) - 4
_GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
_QWEN_OMNI_DEFAULT_MODEL = "qwen3.5-omni-plus"
_MIMO_DEFAULT_MODEL = "mimo-v2.5"
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
    if value in {"gemini_cli", "geminicli", "antigravity_cli", "antigravity", "agy", "agy_cli"}:
        return "antigravity_cli"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    if value in {"claude_code", "claudecode", "claude_cli"}:
        return "claude_code"
    if value == "anthropic":
        return "anthropic"
    return "openai"


def normalize_video_route_mode(value: Any) -> str:
    normalized = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "direct": "primary",
        "native": "primary",
        "hybrid": "auto",
        "frames": "storyboard",
        "frame": "storyboard",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"auto", "primary", "external", "storyboard"} else "auto"


def _is_provider_usable(provider: dict[str, Any]) -> bool:
    api_type = _normalize_media_api_type(str(provider.get("api_type", "") or "openai"))
    model = str(provider.get("model", "") or "").strip()
    if not model:
        return False
    if api_type in {"openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}:
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
        if name == "personification_gemini_auth_mode":
            return self._provider.get("gemini_auth_mode", "auto")
        if name == "personification_codex_auth_path":
            return self._provider.get("auth_path", "")
        if name == "personification_gemini_cli_auth_path":
            return self._provider.get("auth_path", "")
        if name == "personification_gemini_cli_project":
            return self._provider.get("project", "")
        if name == "personification_antigravity_cli_auth_path":
            return self._provider.get("auth_path", "")
        if name == "personification_antigravity_cli_project":
            return self._provider.get("project", "")
        if name == "personification_media_protocol":
            return self._provider.get("media_protocol", "auto")
        if name == "personification_claude_code_auth_path":
            return self._provider.get("auth_path", "")
        if name == "personification_thinking_mode":
            return getattr(self._original, name, "none")
        return getattr(self._original, name)


def _invalid_media_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    return " ".join(value.lower().split()) in _GENERIC_REFUSAL_TEXTS


_MEDIA_EVIDENCE_LIST_KEYS = (
    "visual_evidence",
    "ocr_text",
    "characters_or_entities",
    "franchise_candidates",
)
_MEDIA_EVIDENCE_SCALAR_KEYS = ("scene_summary", "analysis", "safe_summary")
_MEDIA_UNAVAILABLE_MARKERS = (
    "missing_media",
    "vision_unavailable",
    "video unavailable",
    "can't view",
    "cannot view",
    "unable to view",
    "couldn't load",
    "unable to load",
    "无法查看视频",
    "无法查看",
    "看不了视频",
    "看不了",
    "看不到视频",
    "看不到",
    "视频加载不出来",
    "加载不出来",
    "无法加载视频",
    "无法加载",
    "无法分析视频",
    "无法分析",
)


def _looks_like_media_unavailable_text(value: Any) -> bool:
    normalized = " ".join(str(value or "").strip().lower().split())
    return bool(normalized) and any(marker in normalized for marker in _MEDIA_UNAVAILABLE_MARKERS)


def _media_result_has_evidence(text: str) -> bool:
    """Reject structured empty/refusal results before marking a video route usable.

    Vision providers are allowed to return plain text, but the built-in vision
    prompt asks for a structured object.  A JSON object containing only
    ``ambiguity_notes`` (for example ``vision_unavailable``) is an operational
    failure, not evidence.  Treating it as success prevents storyboard or
    another configured route from getting a chance to inspect the media.
    """

    raw = str(text or "").strip()
    if _invalid_media_text(raw):
        return False
    try:
        payload = json.loads(raw)
    except Exception:
        return not _looks_like_media_unavailable_text(raw)
    if not isinstance(payload, dict):
        return True

    notes = payload.get("ambiguity_notes")
    if isinstance(notes, list):
        note_items = notes
    elif notes:
        note_items = [notes]
    else:
        note_items = []
    note_text = " ".join(str(item or "").strip().lower() for item in note_items)
    notes_indicate_unavailable = any(marker in note_text for marker in _MEDIA_UNAVAILABLE_MARKERS)
    known_keys = set(_MEDIA_EVIDENCE_LIST_KEYS) | set(_MEDIA_EVIDENCE_SCALAR_KEYS)
    if not known_keys.intersection(payload):
        return not notes_indicate_unavailable
    for key in _MEDIA_EVIDENCE_LIST_KEYS:
        items = payload.get(key)
        if isinstance(items, list) and any(
            (isinstance(item, dict) and bool(item))
            or (str(item or "").strip() and not _looks_like_media_unavailable_text(item))
            for item in items
        ):
            return True
    for key in _MEDIA_EVIDENCE_SCALAR_KEYS:
        value = payload.get(key)
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
        if isinstance(value, str) and value.strip() and not _looks_like_media_unavailable_text(value):
            return True
    return False


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
            "project": str(primary.get("project", "") or ""),
            "gemini_auth_mode": str(primary.get("gemini_auth_mode", "auto") or "auto"),
            "media_protocol": str(primary.get("media_protocol", "auto") or "auto"),
        }
    plugin_config = getattr(runtime, "plugin_config", None)
    api_type = str(getattr(plugin_config, "personification_api_type", "") or "")
    normalized_type = _normalize_media_api_type(api_type)
    if normalized_type == "openai_codex":
        auth_path = str(getattr(plugin_config, "personification_codex_auth_path", "") or "")
    elif normalized_type == "gemini_cli":
        auth_path = str(getattr(plugin_config, "personification_gemini_cli_auth_path", "") or "")
    elif normalized_type == "antigravity_cli":
        auth_path = str(getattr(plugin_config, "personification_antigravity_cli_auth_path", "") or "")
    elif normalized_type == "claude_code":
        auth_path = str(getattr(plugin_config, "personification_claude_code_auth_path", "") or "")
    else:
        auth_path = ""
    return {
        "api_type": api_type,
        "api_url": str(getattr(plugin_config, "personification_api_url", "") or ""),
        "api_key": str(getattr(plugin_config, "personification_api_key", "") or ""),
        "model": str(getattr(plugin_config, "personification_model", "") or ""),
        "auth_path": auth_path,
        "project": str(
            getattr(
                plugin_config,
                "personification_antigravity_cli_project"
                if normalized_type == "antigravity_cli"
                else "personification_gemini_cli_project",
                "",
            )
            or ""
        ),
        "gemini_auth_mode": str(
            getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto"
        ),
        "media_protocol": str(
            getattr(plugin_config, "personification_media_protocol", "auto") or "auto"
        ),
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
            _log_warning(
                runtime,
                f"[vision] provider={provider_name} is not known to support image input; trying anyway",
            )
        try:
            caller = _build_tool_caller(_ProviderConfigProxy(plugin_config, provider))
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
                _log_warning(
                    runtime,
                    f"[vision] primary image route failed provider={provider_name}: {sanitize_text(exc)}",
                )
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
    attempted_routes: list[str] | None = None,
) -> str:
    for provider in _primary_provider_candidates(runtime):
        api_type = str(provider.get("api_type", "") or "")
        model = str(provider.get("model", "") or "")
        provider_name = str(provider.get("name", "") or model or api_type or "primary")
        adapter = resolve_media_provider_adapter(provider)
        if not adapter.supports_video:
            continue
        if adapter.protocol != MEDIA_PROTOCOL_ANTIGRAVITY and not str(provider.get("api_key", "") or "").strip():
            continue
        attempt_route = {
            MEDIA_PROTOCOL_GEMINI: "video_primary_gemini",
            MEDIA_PROTOCOL_ANTIGRAVITY: "video_primary_agy",
            MEDIA_PROTOCOL_QWEN: "video_primary_qwen_omni",
            MEDIA_PROTOCOL_MIMO: "video_primary_mimo",
        }.get(adapter.protocol, "")
        if attempt_route and attempted_routes is not None:
            attempted_routes.append(attempt_route)
        try:
            if adapter.protocol == MEDIA_PROTOCOL_GEMINI:
                result = await _call_gemini_media(
                    api_key=str(provider.get("api_key", "") or ""),
                    base_url=str(provider.get("api_url", "") or ""),
                    model=model or _GEMINI_DEFAULT_MODEL,
                    auth_mode=str(provider.get("gemini_auth_mode", "auto") or "auto"),
                    prompt=prompt,
                    video_refs=refs,
                )
            elif adapter.protocol == MEDIA_PROTOCOL_QWEN:
                result = await _call_qwen_omni_media(
                    api_key=str(provider.get("api_key", "") or ""),
                    base_url=str(provider.get("api_url", "") or ""),
                    workspace_id=str(provider.get("workspace_id", "") or ""),
                    model=model or _QWEN_OMNI_DEFAULT_MODEL,
                    prompt=prompt,
                    video_refs=refs,
                )
            elif adapter.protocol == MEDIA_PROTOCOL_MIMO:
                result = await _call_mimo_media(
                    api_key=str(provider.get("api_key", "") or ""),
                    base_url=str(provider.get("api_url", "") or ""),
                    model=model or _MIMO_DEFAULT_MODEL,
                    prompt=prompt,
                    video_refs=refs,
                    fps=float(provider.get("video_fps", 2.0) or 2.0),
                    media_resolution=str(provider.get("media_resolution", "default") or "default"),
                )
            elif adapter.protocol == MEDIA_PROTOCOL_ANTIGRAVITY:
                caller = _build_tool_caller(_ProviderConfigProxy(runtime.plugin_config, provider))
                remote_refs = [ref for ref in refs if str(ref).startswith(("http://", "https://"))]
                local_refs = [ref for ref in refs if ref not in remote_refs]
                result_response = await caller.chat_with_tools(
                    messages=[
                        {
                            "role": "user",
                            "content": build_user_message_content(
                                text=prompt,
                                video_urls=remote_refs,
                                video_files=local_refs,
                            ),
                        }
                    ],
                    tools=[],
                    use_builtin_search=False,
                )
                result = str(getattr(result_response, "content", "") or "").strip()
            else:
                continue
        except Exception as exc:
            if not error_indicates_vision_unavailable(exc):
                _log_warning(
                    runtime,
                    f"[video] primary route failed provider={provider_name}: {sanitize_text(exc)}",
                )
            continue
        if _media_result_has_evidence(result):
            return str(result or "").strip()
    return ""


async def _try_primary_audio_routes(
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
        adapter = resolve_media_provider_adapter(provider)
        if not adapter.supports_audio:
            continue
        if adapter.protocol != MEDIA_PROTOCOL_ANTIGRAVITY and not str(provider.get("api_key", "") or "").strip():
            continue
        try:
            if adapter.protocol == MEDIA_PROTOCOL_GEMINI:
                result = await _call_gemini_media(
                    api_key=str(provider.get("api_key", "") or ""),
                    base_url=str(provider.get("api_url", "") or ""),
                    model=model or _GEMINI_DEFAULT_MODEL,
                    auth_mode=str(provider.get("gemini_auth_mode", "auto") or "auto"),
                    prompt=prompt,
                    audio_refs=refs,
                )
            elif adapter.protocol == MEDIA_PROTOCOL_QWEN:
                result = await _call_qwen_omni_media(
                    api_key=str(provider.get("api_key", "") or ""),
                    base_url=str(provider.get("api_url", "") or ""),
                    workspace_id=str(provider.get("workspace_id", "") or ""),
                    model=model or _QWEN_OMNI_DEFAULT_MODEL,
                    prompt=prompt,
                    audio_refs=refs,
                )
            elif adapter.protocol == MEDIA_PROTOCOL_MIMO:
                result = await _call_mimo_media(
                    api_key=str(provider.get("api_key", "") or ""),
                    base_url=str(provider.get("api_url", "") or ""),
                    model=model or _MIMO_DEFAULT_MODEL,
                    prompt=prompt,
                    audio_refs=refs,
                )
            elif adapter.protocol == MEDIA_PROTOCOL_ANTIGRAVITY:
                caller = _build_tool_caller(_ProviderConfigProxy(runtime.plugin_config, provider))
                remote_refs = [ref for ref in refs if str(ref).startswith(("http://", "https://"))]
                local_refs = [ref for ref in refs if ref not in remote_refs]
                response = await caller.chat_with_tools(
                    messages=[
                        {
                            "role": "user",
                            "content": build_user_message_content(
                                text=prompt,
                                audio_urls=remote_refs,
                                audio_files=local_refs,
                            ),
                        }
                    ],
                    tools=[],
                    use_builtin_search=False,
                )
                result = str(getattr(response, "content", "") or "").strip()
            else:
                continue
        except Exception as exc:
            if not error_indicates_vision_unavailable(exc):
                _log_warning(
                    runtime,
                    f"[audio] primary route failed provider={provider_name}: {sanitize_text(exc)}",
                )
            continue
        if not _invalid_media_text(result):
            return str(result or "").strip()
    return ""


def primary_route_supports_native_video(
    runtime: Any,
    *,
    route_name: str = VISUAL_ROUTE_AGENT,
) -> bool:
    for provider in _primary_provider_candidates(runtime):
        adapter = resolve_media_provider_adapter(provider)
        if adapter.protocol != MEDIA_PROTOCOL_ANTIGRAVITY and not str(provider.get("api_key", "") or "").strip():
            continue
        if adapter.supports_video:
            return True
    return False


def primary_route_supports_native_audio(
    runtime: Any,
    *,
    route_name: str = VISUAL_ROUTE_AGENT,
) -> bool:
    for provider in _primary_provider_candidates(runtime):
        adapter = resolve_media_provider_adapter(provider)
        if adapter.protocol != MEDIA_PROTOCOL_ANTIGRAVITY and not str(provider.get("api_key", "") or "").strip():
            continue
        if adapter.supports_audio:
            return True
    return False


def get_primary_provider_signature(runtime: Any) -> tuple[str, str]:
    primary = get_primary_provider_config(runtime)
    return primary["api_type"], primary["model"]


def get_primary_image_route_fingerprint(
    runtime: Any,
    *,
    route_name: str = VISUAL_ROUTE_AGENT,
) -> str:
    routes: list[dict[str, str]] = []
    for provider in _primary_provider_candidates(runtime):
        api_key = str(provider.get("api_key", "") or "")
        auth_path = str(provider.get("auth_path", "") or "")
        routes.append(
            {
                "name": str(provider.get("name", "") or ""),
                "api_type": _normalize_media_api_type(str(provider.get("api_type", "") or "")),
                "api_url": str(provider.get("api_url", "") or "").strip().rstrip("/"),
                "model": str(provider.get("model", "") or "").strip(),
                "auth": hashlib.sha256(f"{api_key}\0{auth_path}".encode("utf-8")).hexdigest(),
                "project": str(provider.get("project", "") or "").strip(),
            }
        )
    payload = json.dumps(
        {"route_name": str(route_name or ""), "routes": routes},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_video_fallback_provider_config(runtime: Any) -> dict[str, str] | None:
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return None
    if bool(getattr(plugin_config, "personification_fullmodal_provider_enabled", False)):
        protocol = str(
            getattr(plugin_config, "personification_fullmodal_provider_protocol", "gemini_native")
            or "gemini_native"
        ).strip().lower().replace("-", "_")
        if protocol not in {
            "gemini_native",
            "openai_qwen_omni",
            "openai_mimo_v25",
            "openai_custom_video_url",
        }:
            return None
        return {
            "api_type": protocol,
            "api_url": str(
                getattr(plugin_config, "personification_fullmodal_provider_api_url", "") or ""
            ).strip(),
            "api_key": str(
                getattr(plugin_config, "personification_fullmodal_provider_api_key", "") or ""
            ).strip(),
            "model": str(
                getattr(plugin_config, "personification_fullmodal_provider_model", "") or ""
            ).strip(),
            "workspace_id": str(
                getattr(plugin_config, "personification_fullmodal_provider_workspace_id", "") or ""
            ).strip(),
            "auth_mode": str(
                getattr(plugin_config, "personification_fullmodal_provider_auth_mode", "auto") or "auto"
            ).strip().lower(),
            "gemini_auth_mode": str(
                getattr(plugin_config, "personification_fullmodal_provider_auth_mode", "auto") or "auto"
            ).strip().lower(),
            "video_fps": str(
                getattr(plugin_config, "personification_fullmodal_provider_video_fps", 2.0) or 2.0
            ),
            "media_resolution": str(
                getattr(plugin_config, "personification_fullmodal_provider_media_resolution", "default")
                or "default"
            ).strip().lower(),
            "timeout": str(
                getattr(plugin_config, "personification_fullmodal_provider_timeout", 600.0) or 600.0
            ),
            "max_bytes": str(
                getattr(plugin_config, "personification_fullmodal_provider_max_bytes", 536870912)
                or 536870912
            ),
            "stream": "true"
            if bool(getattr(plugin_config, "personification_fullmodal_provider_stream", False))
            else "false",
            "source": "fullmodal_provider",
        }
    resolution = resolve_video_fallback_provider(plugin_config, getattr(runtime, "logger", None), warn=True)
    if resolution is None:
        return None
    payload = dict(resolution.provider)
    raw_type = str(payload.get("api_type", "") or "").strip().lower().replace("-", "_")
    if raw_type == "qwen_omni":
        return {
            "api_type": "qwen_omni",
            "api_url": str(payload.get("api_url", "") or "").strip(),
            "api_key": str(payload.get("api_key", "") or "").strip(),
            "model": str(payload.get("model", "") or "").strip() or _QWEN_OMNI_DEFAULT_MODEL,
            "workspace_id": str(payload.get("workspace_id", "") or "").strip(),
            "auth_path": "",
            "gemini_auth_mode": "auto",
            "source": "legacy_video_fallback",
        }
    normalized_type = _normalize_media_api_type(str(payload.get("api_type", "") or ""))
    if normalized_type not in {"gemini_official"}:
        return None
    return {
        "api_type": normalized_type,
        "api_url": str(payload.get("api_url", "") or "").strip(),
        "api_key": str(payload.get("api_key", "") or "").strip(),
        "model": str(payload.get("model", "") or "").strip() or _GEMINI_DEFAULT_MODEL,
        "auth_path": str(payload.get("auth_path", "") or "").strip(),
        "gemini_auth_mode": str(payload.get("gemini_auth_mode", "auto") or "auto").strip(),
        "source": "legacy_video_fallback",
    }


def _qwen_omni_endpoint(base_url: str, workspace_id: str = "") -> str:
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        workspace = str(workspace_id or "").strip()
        if not workspace:
            raise ValueError("qwen_omni_endpoint_missing")
        if not all(character.isalnum() or character == "-" for character in workspace):
            raise ValueError("qwen_omni_workspace_id_invalid")
        raw = f"https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("qwen_omni_endpoint_invalid")
    if parsed.query or parsed.fragment:
        raise ValueError("qwen_omni_endpoint_invalid")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return raw
    return f"{raw}/chat/completions"


def _qwen_video_part(video_ref: str) -> dict[str, Any]:
    normalized, problem = normalize_video_ref(video_ref)
    if not normalized:
        raise ValueError(f"invalid_video_ref:{problem or 'unknown'}")
    if normalized.startswith(("http://", "https://")):
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("qwen_omni_video_url_invalid")
        return {"type": "video_url", "video_url": {"url": normalized}}
    path = Path(normalized)
    if path.stat().st_size > _QWEN_RAW_INLINE_MAX_BYTES:
        raise ValueError("qwen_omni_local_video_too_large")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "video_url", "video_url": {"url": f"data:;base64,{payload}"}}


def _audio_format(ref: str, *, default: str = "wav") -> str:
    suffix = Path(urlsplit(str(ref or "")).path).suffix.lower().lstrip(".")
    aliases = {"wave": "wav", "oga": "ogg", "opus": "ogg", "m4a": "mp4"}
    normalized = aliases.get(suffix, suffix)
    return normalized if normalized in {"wav", "mp3", "mp4", "aac", "ogg", "flac", "amr"} else default


def _qwen_audio_part(audio_ref: str) -> dict[str, Any]:
    normalized, problem = normalize_audio_ref(audio_ref)
    if not normalized:
        raise ValueError(f"invalid_audio_ref:{problem or 'unknown'}")
    audio_format = _audio_format(normalized)
    if normalized.startswith(("http://", "https://")):
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("qwen_omni_audio_url_invalid")
        return {
            "type": "input_audio",
            "input_audio": {"data": normalized, "format": audio_format},
        }
    path = Path(normalized)
    if path.stat().st_size > _QWEN_RAW_INLINE_MAX_BYTES:
        raise ValueError("qwen_omni_local_audio_too_large")
    return {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "format": audio_format,
        },
    }


def _qwen_text_delta(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        message = choices[0].get("message")
        delta = message if isinstance(message, dict) else {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    return ""


def _parse_qwen_omni_response(response: httpx.Response) -> str:
    chunks: list[str] = []
    for raw_line in str(response.text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            continue
        text = _qwen_text_delta(payload)
        if text:
            chunks.append(text)
    if chunks:
        return "".join(chunks).strip()
    try:
        return _qwen_text_delta(response.json()).strip()
    except Exception:
        return ""


async def _call_qwen_omni_media(
    *,
    api_key: str,
    base_url: str,
    workspace_id: str,
    model: str,
    prompt: str,
    video_refs: Sequence[str] = (),
    audio_refs: Sequence[str] = (),
    timeout: float = 180.0,
) -> str:
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("qwen_omni_api_key_missing")
    endpoint = _qwen_omni_endpoint(base_url, workspace_id)
    selected_model = str(model or "").strip() or _QWEN_OMNI_DEFAULT_MODEL
    content = [_qwen_video_part(str(ref or "").strip()) for ref in video_refs]
    content.extend(_qwen_audio_part(str(ref or "").strip()) for ref in audio_refs)
    if not content:
        raise ValueError("qwen_omni_media_missing")
    content.append({"type": "text", "text": str(prompt or "").strip() or "请分析这段音视频内容"})
    payload: dict[str, Any] = {
        "model": selected_model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["text"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if selected_model.startswith("qwen3-omni-flash"):
        payload["enable_thinking"] = False
    bounded_timeout = max(20.0, min(300.0, float(timeout or 180.0)))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(bounded_timeout, connect=15.0),
        follow_redirects=False,
    ) as client:
        response = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
    return _parse_qwen_omni_response(response)


def _openai_compatible_endpoint(base_url: str, *, default_root: str, error_prefix: str) -> str:
    raw = str(base_url or "").strip().rstrip("/") or default_root.rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{error_prefix}_endpoint_invalid")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return raw
    return f"{raw}/chat/completions"


def _mimo_video_part(video_ref: str, *, fps: float, media_resolution: str) -> dict[str, Any]:
    normalized, problem = normalize_video_ref(video_ref)
    if not normalized:
        raise ValueError(f"invalid_video_ref:{problem or 'unknown'}")
    if normalized.startswith(("http://", "https://")):
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("mimo_video_url_invalid")
        url = normalized
    else:
        path = Path(normalized)
        if path.stat().st_size > _MIMO_RAW_INLINE_MAX_BYTES:
            raise ValueError("mimo_local_video_too_large")
        mime_type, _ = mimetypes.guess_type(str(path))
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        url = f"data:{mime_type or 'video/mp4'};base64,{payload}"
    video_url: dict[str, Any] = {
        "url": url,
        "fps": max(0.1, min(10.0, float(fps or 2.0))),
    }
    resolution = str(media_resolution or "default").strip().lower()
    if resolution in {"default", "low", "medium", "high"}:
        video_url["media_resolution"] = resolution
    return {"type": "video_url", "video_url": video_url}


def _mimo_audio_part(audio_ref: str) -> dict[str, Any]:
    normalized, problem = normalize_audio_ref(audio_ref)
    if not normalized:
        raise ValueError(f"invalid_audio_ref:{problem or 'unknown'}")
    audio_format = _audio_format(normalized)
    if normalized.startswith(("http://", "https://")):
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("mimo_audio_url_invalid")
        data = normalized
    else:
        path = Path(normalized)
        if path.stat().st_size > _MIMO_RAW_INLINE_MAX_BYTES:
            raise ValueError("mimo_local_audio_too_large")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "input_audio", "input_audio": {"data": data, "format": audio_format}}


async def _call_mimo_media(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    video_refs: Sequence[str] = (),
    audio_refs: Sequence[str] = (),
    fps: float = 2.0,
    media_resolution: str = "default",
    timeout: float = 300.0,
) -> str:
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("mimo_api_key_missing")
    endpoint = _openai_compatible_endpoint(
        base_url,
        default_root="https://api.xiaomimimo.com/v1",
        error_prefix="mimo",
    )
    content = [
        _mimo_video_part(
            str(ref or "").strip(),
            fps=fps,
            media_resolution=media_resolution,
        )
        for ref in video_refs
    ]
    content.extend(_mimo_audio_part(str(ref or "").strip()) for ref in audio_refs)
    if not content:
        raise ValueError("mimo_media_missing")
    content.append({"type": "text", "text": str(prompt or "").strip() or "请分析这段音视频内容"})
    bounded_timeout = max(20.0, min(600.0, float(timeout or 300.0)))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(bounded_timeout, connect=15.0),
        follow_redirects=False,
    ) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": str(model or "").strip() or _MIMO_DEFAULT_MODEL,
                "messages": [{"role": "user", "content": content}],
            },
        )
        response.raise_for_status()
    return _qwen_text_delta(response.json()).strip()


def _custom_video_url_part(video_ref: str) -> dict[str, Any]:
    normalized, problem = normalize_video_ref(video_ref)
    if not normalized:
        raise ValueError(f"invalid_video_ref:{problem or 'unknown'}")
    if normalized.startswith(("http://", "https://")):
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("custom_video_url_invalid")
        url = normalized
    else:
        path = Path(normalized)
        if path.stat().st_size > _MIMO_RAW_INLINE_MAX_BYTES:
            raise ValueError("custom_local_video_too_large")
        mime_type, _ = mimetypes.guess_type(str(path))
        url = (
            f"data:{mime_type or 'video/mp4'};base64,"
            f"{base64.b64encode(path.read_bytes()).decode('ascii')}"
        )
    return {"type": "video_url", "video_url": {"url": url}}


def _custom_openai_auth_headers(api_key: str, auth_mode: str) -> dict[str, str]:
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("custom_fullmodal_api_key_missing")
    normalized = str(auth_mode or "auto").strip().lower().replace("_", "-")
    if normalized == "api-key":
        return {"API-Key": key, "Content-Type": "application/json"}
    if normalized not in {"auto", "bearer"}:
        raise ValueError("custom_fullmodal_auth_mode_invalid")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _call_custom_video_url_media(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    video_refs: Sequence[str],
    auth_mode: str = "auto",
    stream: bool = False,
    timeout: float = 600.0,
) -> str:
    endpoint = _openai_compatible_endpoint(
        base_url,
        default_root="",
        error_prefix="custom_fullmodal",
    )
    selected_model = str(model or "").strip()
    if not selected_model:
        raise ValueError("custom_fullmodal_model_missing")
    content = [_custom_video_url_part(str(ref or "").strip()) for ref in video_refs]
    if not content:
        raise ValueError("custom_fullmodal_media_missing")
    content.append({"type": "text", "text": str(prompt or "").strip() or "请分析这段视频内容"})
    use_stream = bool(stream)
    bounded_timeout = max(20.0, min(900.0, float(timeout or 600.0)))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(bounded_timeout, connect=15.0),
        follow_redirects=False,
    ) as client:
        response = await client.post(
            endpoint,
            headers=_custom_openai_auth_headers(api_key, auth_mode),
            json={
                "model": selected_model,
                "messages": [{"role": "user", "content": content}],
                "stream": use_stream,
            },
        )
        response.raise_for_status()
    if use_stream:
        return _parse_qwen_omni_response(response)
    return _qwen_text_delta(response.json()).strip()


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


def _gemini_api_root(base_url: str) -> str:
    raw = str(base_url or "").strip().rstrip("/") or "https://generativelanguage.googleapis.com"
    lower = raw.lower()
    if lower.endswith("/v1beta"):
        return raw
    if lower.endswith("/v1"):
        return f"{raw[:-3]}/v1beta"
    return f"{raw}/v1beta"


def _gemini_headers() -> dict[str, str]:
    return {"Content-Type": "application/json"}


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


def _gemini_audio_part(audio_ref: str) -> dict[str, Any]:
    normalized_audio_ref, problem = normalize_audio_ref(audio_ref)
    if not normalized_audio_ref:
        raise ValueError(f"invalid_audio_ref:{problem or 'unknown'}")
    if normalized_audio_ref.startswith(("http://", "https://")):
        mime_type, _ = mimetypes.guess_type(urlsplit(normalized_audio_ref).path)
        return {
            "fileData": {
                "mimeType": mime_type or "audio/mpeg",
                "fileUri": normalized_audio_ref,
            }
        }

    path = Path(normalized_audio_ref)
    payload = path.read_bytes()
    if len(payload) > _VIDEO_INLINE_MAX_BYTES:
        raise ValueError("audio_file_too_large_for_inline_data")
    mime_type, _ = mimetypes.guess_type(str(path))
    return {
        "inlineData": {
            "mimeType": mime_type or "audio/wav",
            "data": base64.b64encode(payload).decode("ascii"),
        }
    }


async def _upload_gemini_video_file(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    base_url: str,
    auth_mode: str,
    path: Path,
) -> tuple[dict[str, Any], str]:
    size = path.stat().st_size
    mime_type, _ = mimetypes.guess_type(str(path))
    mime = mime_type or "video/mp4"
    upload_endpoint = f"{_gemini_api_root(base_url).rsplit('/v1beta', 1)[0]}/upload/v1beta/files"

    async def _start(auth):  # noqa: ANN001, ANN202
        return await client.post(
            upload_endpoint,
            headers={
                **auth.headers,
                "Content-Type": "application/json",
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime,
            },
            params=auth.params,
            json={"file": {"display_name": path.name}},
        )

    start_result = await request_with_gemini_auth(
        endpoint=upload_endpoint,
        api_key=api_key,
        auth_mode=auth_mode,
        send=_start,
        allow_negotiation=not use_single_attempt_retry_policy(),
    )
    start_response = start_result.response
    raise_for_gemini_status(
        start_response,
        auth_mode=start_result.mode,
        request_count=start_result.request_count,
    )
    upload_url = str(start_response.headers.get("x-goog-upload-url") or "").strip()
    if not upload_url:
        raise ValueError("gemini_video_upload_url_missing")
    with path.open("rb") as handle:
        upload_response = await client.post(
            upload_url,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            content=handle,
        )
    upload_response.raise_for_status()
    payload = dict(upload_response.json() or {})
    file_info = payload.get("file") if isinstance(payload.get("file"), dict) else payload
    file_name = str(file_info.get("name") or "").strip()
    file_uri = str(file_info.get("uri") or "").strip()
    if not file_name or not file_uri:
        raise ValueError("gemini_video_upload_result_invalid")

    file_endpoint = f"{_gemini_api_root(base_url)}/{file_name}"
    deadline = asyncio.get_running_loop().time() + 90.0
    while str(file_info.get("state") or {}).upper() not in {"ACTIVE", "FAILED"}:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("gemini_video_file_processing_timeout")

        async def _poll(auth):  # noqa: ANN001, ANN202
            return await client.get(file_endpoint, headers=auth.headers, params=auth.params)

        poll_result = await request_with_gemini_auth(
            endpoint=file_endpoint,
            api_key=api_key,
            auth_mode=auth_mode,
            send=_poll,
            allow_negotiation=not use_single_attempt_retry_policy(),
        )
        poll_response = poll_result.response
        raise_for_gemini_status(
            poll_response,
            auth_mode=poll_result.mode,
            request_count=poll_result.request_count,
        )
        file_info = dict(poll_response.json() or {})
        await asyncio.sleep(1.0)
    if str(file_info.get("state") or "").upper() != "ACTIVE":
        raise ValueError("gemini_video_file_processing_failed")
    return {"fileData": {"mimeType": mime, "fileUri": file_uri}}, file_name


async def _delete_gemini_file(
    *,
    client: httpx.AsyncClient,
    api_key: str,
    base_url: str,
    auth_mode: str,
    file_name: str,
) -> None:
    endpoint = f"{_gemini_api_root(base_url)}/{str(file_name or '')}"

    async def _delete(auth):  # noqa: ANN001, ANN202
        return await client.delete(endpoint, headers=auth.headers, params=auth.params)

    result = await request_with_gemini_auth(
        endpoint=endpoint,
        api_key=api_key,
        auth_mode=auth_mode,
        send=_delete,
        allow_negotiation=False,
    )
    if result.response.status_code not in {200, 204, 404}:
        raise_for_gemini_status(result.response, auth_mode=result.mode, request_count=result.request_count)


async def _call_gemini_media(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    image_refs: Sequence[str] = (),
    video_refs: Sequence[str] = (),
    audio_refs: Sequence[str] = (),
    auth_mode: str = "auto",
) -> str:
    parts: list[dict[str, Any]] = [{"text": str(prompt or "").strip() or "请分析这段媒体内容"}]
    for ref in image_refs:
        parts.append(_gemini_image_part(str(ref or "").strip()))
    endpoint = _gemini_endpoint(base_url, model or _GEMINI_DEFAULT_MODEL)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(90.0, connect=15.0),
        follow_redirects=False,
    ) as client:
        uploaded_files: list[str] = []
        try:
            for ref in video_refs:
                normalized, problem = normalize_video_ref(str(ref or "").strip())
                if not normalized:
                    raise ValueError(f"invalid_video_ref:{problem or 'unknown'}")
                if normalized.startswith(("http://", "https://")) or Path(normalized).stat().st_size <= _VIDEO_INLINE_MAX_BYTES:
                    parts.append(_gemini_video_part(normalized))
                else:
                    part, file_name = await _upload_gemini_video_file(
                        client=client,
                        api_key=api_key,
                        base_url=base_url,
                        auth_mode=auth_mode,
                        path=Path(normalized),
                    )
                    parts.append(part)
                    uploaded_files.append(file_name)
            for ref in audio_refs:
                normalized, problem = normalize_audio_ref(str(ref or "").strip())
                if not normalized:
                    raise ValueError(f"invalid_audio_ref:{problem or 'unknown'}")
                if normalized.startswith(("http://", "https://")) or Path(normalized).stat().st_size <= _VIDEO_INLINE_MAX_BYTES:
                    parts.append(_gemini_audio_part(normalized))
                else:
                    part, file_name = await _upload_gemini_video_file(
                        client=client,
                        api_key=api_key,
                        base_url=base_url,
                        auth_mode=auth_mode,
                        path=Path(normalized),
                    )
                    parts.append(part)
                    uploaded_files.append(file_name)
            payload = {"contents": [{"role": "user", "parts": parts}]}

            async def _send(auth):  # noqa: ANN001, ANN202
                return await client.post(
                    endpoint,
                    headers={**_gemini_headers(), **auth.headers},
                    params=auth.params,
                    json=payload,
                )

            auth_result = await request_with_gemini_auth(
                endpoint=endpoint.rsplit("/models/", 1)[0],
                api_key=api_key,
                auth_mode=auth_mode,
                send=_send,
                allow_negotiation=not use_single_attempt_retry_policy(),
            )
            response = auth_result.response
            raise_for_gemini_status(
                response,
                auth_mode=auth_result.mode,
                request_count=auth_result.request_count,
            )
            data = dict(response.json() or {})
        finally:
            for file_name in uploaded_files:
                try:
                    await _delete_gemini_file(
                        client=client,
                        api_key=api_key,
                        base_url=base_url,
                        auth_mode=auth_mode,
                        file_name=file_name,
                    )
                except Exception:
                    pass
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
            _log_warning(runtime, f"[vision] fallback image route failed: {sanitize_text(exc)}")
            continue
        if not _invalid_media_text(output):
            outputs.append(str(output or "").strip())
    result = "\n".join(part for part in outputs if str(part or "").strip()).strip()
    return (result, "vision_fallback" if result else "vision_unavailable")


async def analyze_images_with_primary_route_joint_only(
    *,
    runtime: Any,
    prompt: str,
    image_refs: Sequence[str],
    route_name: str = VISUAL_ROUTE_AGENT,
    image_detail: str = "low",
) -> tuple[str, str]:
    """Analyze exactly two images in one primary-route request.

    This API deliberately has no per-image fallback. A joint comparison loses
    its meaning if either image is analyzed in a separate request.
    """
    refs = [str(item or "").strip() for item in image_refs if str(item or "").strip()]
    if len(refs) != 2:
        return "", "missing_joint_images"
    primary_result = await _try_primary_image_routes(
        runtime=runtime,
        prompt=prompt,
        refs=refs,
        route_name=route_name,
        image_detail=image_detail,
    )
    if primary_result:
        return primary_result, "route_direct"
    return "", "joint_vision_unavailable"


async def analyze_videos_with_route_or_fallback(
    *,
    runtime: Any,
    prompt: str,
    video_refs: Sequence[str],
    route_name: str = VISUAL_ROUTE_AGENT,
    context_terms: Sequence[str] = (),
    route_attempts: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    refs = [str(item or "").strip() for item in video_refs if str(item or "").strip()]
    if not refs:
        return "", "missing_videos"
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return "", "video_disabled"
    video_enabled = bool(getattr(plugin_config, "personification_video_understanding_enabled", False))
    if (
        not video_enabled
        and not _gemini_web_automatic_enabled(plugin_config)
        and not primary_route_supports_native_video(runtime, route_name=route_name)
    ):
        return "", "video_disabled"

    route_mode = normalize_video_route_mode(
        getattr(plugin_config, "personification_video_route_mode", "auto")
    )

    async def _gemini_web_result() -> tuple[str, str]:
        started_at = time.monotonic()
        enabled = bool(getattr(plugin_config, "personification_gemini_web_enabled", False))
        acknowledged = bool(
            getattr(plugin_config, "personification_gemini_web_risk_acknowledged", False)
        )
        if not enabled or not acknowledged:
            code = (
                "gemini_web_disabled"
                if not enabled
                else "gemini_web_risk_ack_required"
            )
            _record_media_attempt(
                route_attempts,
                route="video_gemini_web",
                status="skipped",
                started_at=started_at,
                diagnostic_code=code,
            )
            return "", "video_unavailable"
        try:
            from .gemini_web_service import get_gemini_web_service

            result, detail = await get_gemini_web_service(runtime).analyze(
                config=plugin_config,
                kind="video",
                media_ref=refs[0],
                prompt=prompt,
            )
        except Exception as exc:
            _log_warning(runtime, f"[video] Gemini web route failed: {sanitize_text(exc)}")
            result = ""
            detail = {"status": "failed", "diagnostic_code": "gemini_web_process_failed"}
        result_text = str(result or "").strip()
        status = "ok" if _media_result_has_evidence(result_text) else str(
            detail.get("status") or "failed"
        )
        _record_media_attempt(
            route_attempts,
            route="video_gemini_web",
            status=status,
            started_at=started_at,
            diagnostic_code=str(detail.get("diagnostic_code") or ""),
            diagnostic_stage=str(detail.get("diagnostic_stage") or ""),
        )
        return (result_text, "video_gemini_web") if status == "ok" else ("", "video_unavailable")

    async def _formal_api_result() -> tuple[str, str]:
        started_at = time.monotonic()
        fallback = _build_video_fallback_provider_config(runtime)
        protocol = str((fallback or {}).get("api_type", "") or "").strip().lower()
        route_by_protocol = {
            "gemini_native": "video_external_gemini",
            "gemini_official": "video_external_gemini",
            "openai_qwen_omni": "video_external_qwen_omni",
            "qwen_omni": "video_external_qwen_omni",
            "openai_mimo_v25": "video_external_mimo",
            "openai_custom_video_url": "video_external_custom",
        }
        attempt_route = route_by_protocol.get(protocol, "video_external_fullmodal")
        if not fallback or not fallback.get("api_key"):
            _record_media_attempt(
                route_attempts,
                route=attempt_route,
                status="skipped",
                started_at=started_at,
                diagnostic_code="fullmodal_provider_unconfigured",
            )
            return "", "video_unavailable"
        try:
            max_bytes = max(1, int(float(fallback.get("max_bytes", 536870912) or 536870912)))
            for ref in refs:
                normalized, problem = normalize_video_ref(ref)
                if not normalized:
                    raise ValueError(f"invalid_video_ref:{problem or 'unknown'}")
                if not normalized.startswith(("http://", "https://")) and Path(normalized).stat().st_size > max_bytes:
                    raise ValueError("fullmodal_provider_media_too_large")
            timeout = float(fallback.get("timeout", 600.0) or 600.0)
            if protocol in {"qwen_omni", "openai_qwen_omni"}:
                result = await _call_qwen_omni_media(
                    api_key=fallback["api_key"],
                    base_url=fallback.get("api_url", ""),
                    workspace_id=fallback.get("workspace_id", ""),
                    model=fallback.get("model", "") or _QWEN_OMNI_DEFAULT_MODEL,
                    prompt=prompt,
                    video_refs=refs,
                    timeout=timeout,
                )
            elif protocol == "openai_mimo_v25":
                result = await _call_mimo_media(
                    api_key=fallback["api_key"],
                    base_url=fallback.get("api_url", ""),
                    model=fallback.get("model", "") or _MIMO_DEFAULT_MODEL,
                    prompt=prompt,
                    video_refs=refs,
                    fps=float(fallback.get("video_fps", 2.0) or 2.0),
                    media_resolution=fallback.get("media_resolution", "default"),
                    timeout=timeout,
                )
            elif protocol == "openai_custom_video_url":
                result = await _call_custom_video_url_media(
                    api_key=fallback["api_key"],
                    base_url=fallback.get("api_url", ""),
                    model=fallback.get("model", ""),
                    prompt=prompt,
                    video_refs=refs,
                    auth_mode=fallback.get("auth_mode", "auto"),
                    stream=str(fallback.get("stream", "false")).lower() == "true",
                    timeout=timeout,
                )
            else:
                result = await _call_gemini_media(
                    api_key=fallback["api_key"],
                    base_url=fallback.get("api_url", ""),
                    model=fallback.get("model", "") or _GEMINI_DEFAULT_MODEL,
                    auth_mode=fallback.get("gemini_auth_mode", "auto"),
                    prompt=prompt,
                    video_refs=refs,
                )
        except Exception as exc:
            _log_warning(runtime, f"[video] official API route failed: {sanitize_text(exc)}")
            raw_code = str(exc or "").split(":", 1)[0]
            diagnostic_code = (
                "media_transport_unavailable"
                if raw_code in {
                    "fullmodal_provider_media_too_large",
                    "qwen_omni_local_video_too_large",
                    "mimo_local_video_too_large",
                    "custom_local_video_too_large",
                }
                else "fullmodal_provider_request_failed"
            )
            _record_media_attempt(
                route_attempts,
                route=attempt_route,
                status="failed",
                started_at=started_at,
                diagnostic_code=diagnostic_code,
            )
            return "", "video_unavailable"
        result_text = str(result or "").strip()
        if not _media_result_has_evidence(result_text):
            _record_media_attempt(
                route_attempts,
                route=attempt_route,
                status="failed",
                started_at=started_at,
                diagnostic_code="fullmodal_provider_output_empty",
            )
            return "", "video_unavailable"
        _record_media_attempt(
            route_attempts,
            route=attempt_route,
            status="ok",
            started_at=started_at,
        )
        return result_text, attempt_route

    async def _primary_result() -> tuple[str, str]:
        started_at = time.monotonic()
        attempted_primary_routes: list[str] = []
        primary_result = await _try_primary_video_routes(
            runtime=runtime,
            prompt=prompt,
            refs=refs,
            route_name=route_name,
            attempted_routes=attempted_primary_routes,
        )
        attempt_route = attempted_primary_routes[-1] if attempted_primary_routes else "video_primary"
        _record_media_attempt(
            route_attempts,
            route=attempt_route,
            status="ok" if primary_result else "unsupported",
            started_at=started_at,
            diagnostic_code="" if primary_result else "primary_video_unsupported",
        )
        if primary_result:
            return primary_result, attempt_route

        return "", "video_unavailable"

    async def _external_result() -> tuple[str, str]:
        gemini_result = await _gemini_web_result()
        if gemini_result[0]:
            return gemini_result
        api_result = await _formal_api_result()
        if api_result[0]:
            return api_result
        return "", "video_unavailable"

    native_text = ""
    native_route = "video_unavailable"
    if route_mode in {"auto", "primary"}:
        native_text, native_route = await _primary_result()
        if native_text:
            return native_text, native_route
        if route_mode == "primary":
            return "", native_route
    if route_mode in {"auto", "external"}:
        native_text, native_route = await _external_result()
        if native_text:
            return native_text, native_route

    if route_mode != "storyboard" and not bool(
        getattr(plugin_config, "personification_video_storyboard_fallback_enabled", True)
    ):
        return "", native_route

    async def _storyboard_one(ref: str, native_summary: str) -> tuple[str, str]:
        storyboard = await prepare_video_storyboard(ref, plugin_config)
        try:
            transcript = None
            mimo_transcript = ""
            if not storyboard.subtitle_text:
                mimo_started_at = time.monotonic()
                if storyboard.audio_path is not None and _mimo_web_asr_automatic_enabled(plugin_config):
                    try:
                        from .mimo_web_asr_service import get_mimo_web_asr_service

                        mimo_transcript, detail = await get_mimo_web_asr_service(runtime).transcribe(
                            config=plugin_config,
                            media_ref=str(storyboard.audio_path),
                            prompt="请按时间顺序忠实转写这段视频音轨。" + " ".join(context_terms[:20]),
                        )
                    except Exception as exc:
                        _log_warning(runtime, f"[video] MiMo Web ASR failed: {sanitize_text(exc)}")
                        detail = {"status": "failed", "diagnostic_code": "mimo_web_asr_process_failed"}
                    _record_media_attempt(
                        route_attempts,
                        route="video_storyboard_mimo_web_asr",
                        status="ok" if mimo_transcript else str(detail.get("status") or "failed"),
                        started_at=mimo_started_at,
                        diagnostic_code=str(detail.get("diagnostic_code") or ""),
                        diagnostic_stage=str(detail.get("diagnostic_stage") or ""),
                    )
                if not mimo_transcript:
                    asr_started_at = time.monotonic()
                    transcript = await transcribe_audio_file(
                        storyboard.audio_path,
                        plugin_config,
                        source_url=storyboard.source_url,
                        context_terms=context_terms,
                    )
                    _record_media_attempt(
                        route_attempts,
                        route="video_storyboard_asr_api",
                        status=(
                            "ok"
                            if bool(getattr(transcript, "available", False))
                            else str(getattr(transcript, "status", "unavailable") or "unavailable")
                        ),
                        started_at=asr_started_at,
                        diagnostic_code=str(getattr(transcript, "error_code", "") or ""),
                    )
            metadata = storyboard.summary()
            transcript_block = storyboard.subtitle_text or mimo_transcript or (
                str(getattr(transcript, "text", "") or "")
                if transcript is not None and bool(getattr(transcript, "available", False))
                else ""
            )
            transcript_kind = (
                "BILIBILI_OR_PLATFORM_SUBTITLE"
                if storyboard.subtitle_text
                else "MIMO_WEB_ASR_TRANSCRIPT"
                if mimo_transcript
                else "AUDIO_TRANSCRIPT"
            )
            combined_prompt = (
                f"{str(prompt or '').strip()}\n\n"
                "以下是系统从同一视频按时间顺序提取的分镜拼图。请结合每格时间戳理解动作、镜头、字幕和梗的完整演变，"
                "不要把单帧静态画面当作完整事件。\n"
                f"分镜元数据：{json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))}\n"
                f"[UNTRUSTED_DATA_ONLY: {transcript_kind}]\n"
                f"{transcript_block or '转写不可用；只能依靠分镜和视频原生证据。'}\n"
                "[/UNTRUSTED_DATA_ONLY]\n"
            )
            if native_summary:
                combined_prompt += (
                    "[UNTRUSTED_DATA_ONLY: NATIVE_VIDEO_OBSERVATION]\n"
                    f"{native_summary[:12000]}\n"
                    "[/UNTRUSTED_DATA_ONLY]\n"
                    "请把原生视频观察、分镜和转写作为互相校验的证据；冲突时明确保留不确定点。\n"
                )
            if not storyboard.contact_sheet_refs:
                return native_summary, "" if native_summary else "video_storyboard_frames_empty"
            result, _route = await analyze_images_with_route_or_fallback(
                runtime=runtime,
                prompt=combined_prompt,
                image_refs=storyboard.contact_sheet_refs,
                route_name=route_name,
                image_detail="low",
            )
            result_text = str(result or native_summary or "").strip()
            return result_text, "" if result_text else "video_storyboard_vision_unavailable"
        finally:
            storyboard.cleanup()

    def _storyboard_exception_code(exc: Exception) -> str:
        raw = str(exc or "").strip()
        stable = raw.split(":", 1)[0]
        allowed = {
            "invalid_video_ref",
            "video_bilibili_download_failed",
            "video_douyin_download_failed",
            "video_download_failed",
            "video_ffmpeg_unavailable",
            "video_ffprobe_unavailable",
            "video_storyboard_frame_extract_failed",
            "video_storyboard_probe_failed",
            "video_storyboard_scan_failed",
            "video_storyboard_sheet_build_failed",
            "video_ytdlp_download_failed",
            "video_ytdlp_unavailable",
        }
        return stable if stable in allowed else "video_storyboard_extract_failed"

    timeout = max(
        20.0,
        min(
            900.0,
            float(getattr(plugin_config, "personification_video_analysis_timeout", 600.0) or 600.0),
        ),
    )
    storyboard_started_at = time.monotonic()
    outputs: list[str] = []
    storyboard_failures: list[str] = []
    for index, ref in enumerate(refs):
        try:
            output, failure_code = await asyncio.wait_for(
                _storyboard_one(ref, native_text if index == 0 else ""),
                timeout=timeout,
            )
            if failure_code:
                storyboard_failures.append(failure_code)
        except asyncio.TimeoutError:
            _log_warning(runtime, f"[video] storyboard analysis timed out after {timeout:.1f}s")
            output = native_text if index == 0 else ""
            storyboard_failures.append("video_storyboard_timeout")
        except Exception as exc:
            _log_warning(runtime, f"[video] storyboard analysis failed: {sanitize_text(exc)}")
            output = native_text if index == 0 else ""
            storyboard_failures.append(_storyboard_exception_code(exc))
        if output:
            outputs.append(output)
    result_text = "\n".join(outputs).strip()
    if not result_text or _invalid_media_text(result_text):
        _record_media_attempt(
            route_attempts,
            route="video_storyboard",
            status="failed",
            started_at=storyboard_started_at,
            diagnostic_code=(storyboard_failures[0] if storyboard_failures else "video_storyboard_unavailable"),
        )
        return "", native_route if native_text else "video_unavailable"
    _record_media_attempt(
        route_attempts,
        route="video_storyboard",
        status="ok",
        started_at=storyboard_started_at,
    )
    return result_text, "video_hybrid" if native_text else "video_storyboard"


def audio_route_available(runtime: Any) -> bool:
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return False
    if primary_route_supports_native_audio(runtime):
        return True
    if _gemini_web_automatic_enabled(plugin_config):
        return True
    if _mimo_web_asr_automatic_enabled(plugin_config):
        return True
    if bool(getattr(plugin_config, "personification_fullmodal_provider_enabled", False)) and str(
        getattr(plugin_config, "personification_fullmodal_provider_api_key", "") or ""
    ).strip():
        return True
    try:
        settings = resolve_transcription_settings(plugin_config)
    except Exception:
        return False
    return bool(settings.get("enabled") and settings.get("api_key"))


async def analyze_audios_with_route_or_fallback(
    *,
    runtime: Any,
    prompt: str,
    audio_refs: Sequence[str],
    route_name: str = VISUAL_ROUTE_AGENT,
    context_terms: Sequence[str] = (),
    route_attempts: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    refs: list[str] = []
    for raw in audio_refs:
        normalized, _problem = normalize_audio_ref(str(raw or "").strip())
        if normalized and normalized not in refs:
            refs.append(normalized)
        if refs:
            break
    if not refs:
        return "", "missing_audios"
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is None:
        return "", "audio_disabled"

    started_at = time.monotonic()
    primary_result = await _try_primary_audio_routes(
        runtime=runtime,
        prompt=prompt,
        refs=refs,
        route_name=route_name,
    )
    _record_media_attempt(
        route_attempts,
        route="audio_primary_native",
        status="ok" if primary_result else "unsupported",
        started_at=started_at,
        diagnostic_code="" if primary_result else "primary_audio_unsupported",
    )
    if primary_result:
        return primary_result, "audio_primary_native"

    async def _external_api_result() -> tuple[str, str]:
        external_started_at = time.monotonic()
        fallback = _build_video_fallback_provider_config(runtime)
        protocol = str((fallback or {}).get("api_type", "") or "").strip().lower()
        if not fallback or not fallback.get("api_key") or protocol == "openai_custom_video_url":
            _record_media_attempt(
                route_attempts,
                route="audio_external_fullmodal",
                status="skipped",
                started_at=external_started_at,
                diagnostic_code="fullmodal_provider_unconfigured",
            )
            return "", "audio_unavailable"
        try:
            max_bytes = max(1, int(float(fallback.get("max_bytes", 536870912) or 536870912)))
            normalized = refs[0]
            if not normalized.startswith(("http://", "https://")) and Path(normalized).stat().st_size > max_bytes:
                raise ValueError("fullmodal_provider_media_too_large")
            timeout = float(fallback.get("timeout", 600.0) or 600.0)
            if protocol in {"qwen_omni", "openai_qwen_omni"}:
                result = await _call_qwen_omni_media(
                    api_key=fallback["api_key"],
                    base_url=fallback.get("api_url", ""),
                    workspace_id=fallback.get("workspace_id", ""),
                    model=fallback.get("model", "") or _QWEN_OMNI_DEFAULT_MODEL,
                    prompt=prompt,
                    audio_refs=refs,
                    timeout=timeout,
                )
            elif protocol == "openai_mimo_v25":
                result = await _call_mimo_media(
                    api_key=fallback["api_key"],
                    base_url=fallback.get("api_url", ""),
                    model=fallback.get("model", "") or _MIMO_DEFAULT_MODEL,
                    prompt=prompt,
                    audio_refs=refs,
                    timeout=timeout,
                )
            else:
                result = await _call_gemini_media(
                    api_key=fallback["api_key"],
                    base_url=fallback.get("api_url", ""),
                    model=fallback.get("model", "") or _GEMINI_DEFAULT_MODEL,
                    auth_mode=fallback.get("gemini_auth_mode", "auto"),
                    prompt=prompt,
                    audio_refs=refs,
                )
        except Exception as exc:
            _log_warning(runtime, f"[audio] external fullmodal route failed: {sanitize_text(exc)}")
            code = str(exc or "").split(":", 1)[0]
            _record_media_attempt(
                route_attempts,
                route="audio_external_fullmodal",
                status="failed",
                started_at=external_started_at,
                diagnostic_code=(
                    "media_transport_unavailable"
                    if code in {
                        "fullmodal_provider_media_too_large",
                        "qwen_omni_local_audio_too_large",
                        "mimo_local_audio_too_large",
                    }
                    else "fullmodal_provider_request_failed"
                ),
            )
            return "", "audio_unavailable"
        result_text = str(result or "").strip()
        if _invalid_media_text(result_text):
            _record_media_attempt(
                route_attempts,
                route="audio_external_fullmodal",
                status="failed",
                started_at=external_started_at,
                diagnostic_code="fullmodal_provider_output_empty",
            )
            return "", "audio_unavailable"
        _record_media_attempt(
            route_attempts,
            route="audio_external_fullmodal",
            status="ok",
            started_at=external_started_at,
        )
        return result_text, "audio_external_fullmodal"

    async def _gemini_web_result() -> tuple[str, str]:
        gemini_started_at = time.monotonic()
        enabled = bool(getattr(plugin_config, "personification_gemini_web_enabled", False))
        acknowledged = bool(
            getattr(plugin_config, "personification_gemini_web_risk_acknowledged", False)
        )
        if not enabled or not acknowledged:
            code = (
                "gemini_web_disabled"
                if not enabled
                else "gemini_web_risk_ack_required"
            )
            _record_media_attempt(
                route_attempts,
                route="audio_gemini_web",
                status="skipped",
                started_at=gemini_started_at,
                diagnostic_code=code,
            )
            return "", "audio_unavailable"
        try:
            from .gemini_web_service import get_gemini_web_service

            result, detail = await get_gemini_web_service(runtime).analyze(
                config=plugin_config,
                kind="audio",
                media_ref=refs[0],
                prompt=prompt,
            )
        except Exception as exc:
            _log_warning(runtime, f"[audio] Gemini web route failed: {sanitize_text(exc)}")
            result = ""
            detail = {"status": "failed", "diagnostic_code": "gemini_web_process_failed"}
        result_text = str(result or "").strip()
        status = "ok" if result_text and not _invalid_media_text(result_text) else str(
            detail.get("status") or "failed"
        )
        _record_media_attempt(
            route_attempts,
            route="audio_gemini_web",
            status=status,
            started_at=gemini_started_at,
            diagnostic_code=str(detail.get("diagnostic_code") or ""),
            diagnostic_stage=str(detail.get("diagnostic_stage") or ""),
        )
        return (result_text, "audio_gemini_web") if status == "ok" else ("", "audio_unavailable")

    async def _asr_result() -> tuple[str, str]:
        asr_started_at = time.monotonic()
        ref = refs[0]
        source_url = ref if ref.startswith(("http://", "https://")) else ""
        local_path = None if source_url else Path(ref)
        transcript = await transcribe_audio_file(
            local_path,
            plugin_config,
            source_url=source_url,
            context_terms=context_terms,
        )
        _record_media_attempt(
            route_attempts,
            route="audio_asr_api",
            status="ok" if transcript.available else str(transcript.status or "unavailable"),
            started_at=asr_started_at,
            diagnostic_code=str(transcript.error_code or ""),
        )
        if not transcript.available:
            return "", "audio_unavailable"
        payload = {
            "transcript": transcript.text,
            "provider": transcript.provider,
            "model": transcript.model,
            "language": transcript.language,
            "confidence": transcript.confidence,
            "segments": list(transcript.segments)[:200],
        }
        wrapped = (
            "[UNTRUSTED_DATA_ONLY: AUDIO_TRANSCRIPT]\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
            "[/UNTRUSTED_DATA_ONLY]"
        )
        return wrapped, "audio_asr_api"

    async def _mimo_web_asr_result() -> tuple[str, str]:
        mimo_started_at = time.monotonic()
        if not _mimo_web_asr_automatic_enabled(plugin_config):
            code = (
                "mimo_web_asr_disabled"
                if not bool(getattr(plugin_config, "personification_mimo_web_asr_enabled", False))
                else "mimo_web_asr_risk_ack_required"
            )
            _record_media_attempt(
                route_attempts,
                route="audio_mimo_web_asr",
                status="skipped",
                started_at=mimo_started_at,
                diagnostic_code=code,
            )
            return "", "audio_unavailable"
        try:
            from .mimo_web_asr_service import get_mimo_web_asr_service

            result, detail = await get_mimo_web_asr_service(runtime).transcribe(
                config=plugin_config,
                media_ref=refs[0],
                prompt=prompt + " " + " ".join(context_terms[:20]),
            )
        except Exception as exc:
            _log_warning(runtime, f"[audio] MiMo Web ASR failed: {sanitize_text(exc)}")
            result = ""
            detail = {"status": "failed", "diagnostic_code": "mimo_web_asr_process_failed"}
        result_text = str(result or "").strip()
        status = "ok" if result_text else str(detail.get("status") or "failed")
        _record_media_attempt(
            route_attempts,
            route="audio_mimo_web_asr",
            status=status,
            started_at=mimo_started_at,
            diagnostic_code=str(detail.get("diagnostic_code") or ""),
            diagnostic_stage=str(detail.get("diagnostic_stage") or ""),
        )
        return (result_text, "audio_mimo_web_asr") if result_text else ("", "audio_unavailable")

    gemini_result = await _gemini_web_result()
    if gemini_result[0]:
        return gemini_result
    external_result = await _external_api_result()
    if external_result[0]:
        return external_result
    mimo_result = await _mimo_web_asr_result()
    if mimo_result[0]:
        return mimo_result
    asr_result = await _asr_result()
    if asr_result[0]:
        return asr_result
    return "", "audio_unavailable"


__all__ = [
    "analyze_audios_with_route_or_fallback",
    "analyze_images_with_primary_route_joint_only",
    "analyze_images_with_route_or_fallback",
    "analyze_videos_with_route_or_fallback",
    "audio_route_available",
    "get_primary_image_route_fingerprint",
    "get_primary_provider_config",
    "get_primary_provider_signature",
    "normalize_video_route_mode",
    "primary_route_supports_native_video",
    "primary_route_supports_native_audio",
]
