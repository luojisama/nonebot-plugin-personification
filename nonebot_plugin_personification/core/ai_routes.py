from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ..skills.skillpacks.tool_caller.scripts.impl import (
    ToolCaller,
    ToolCallerResponse,
    build_tool_caller,
)
from ..skills.skillpacks.vision_caller.scripts.impl import build_vision_caller


_EMITTED_ROUTE_WARNINGS: set[str] = set()
_ROUTE_WARNING_LOCK = threading.RLock()


@dataclass(frozen=True)
class ProviderResolution:
    provider: dict[str, str]
    source: str
    ignored_sources: tuple[str, ...] = ()


def _normalize_api_type(api_type: str) -> str:
    value = str(api_type or "").strip().lower().replace("-", "_")
    if value in {"gemini", "gemini_official"}:
        return "gemini_official"
    if value == "anthropic":
        return "anthropic"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    return "openai"


def _provider_signature(provider: dict[str, Any] | None) -> tuple[str, str, str, str, str]:
    payload = provider or {}
    return (
        _normalize_api_type(str(payload.get("api_type", "") or "")),
        str(payload.get("api_url", "") or "").strip(),
        str(payload.get("api_key", "") or "").strip(),
        str(payload.get("model", "") or "").strip(),
        str(payload.get("auth_path", "") or "").strip(),
    )


def _provider_model_is_compatible(api_type: str, model: str) -> bool:
    normalized_type = _normalize_api_type(api_type)
    normalized_model = str(model or "").strip().lower()
    if not normalized_model:
        return False
    if normalized_type == "openai_codex":
        return not normalized_model.startswith(("gemini", "claude"))
    if normalized_type == "gemini_official":
        return not normalized_model.startswith(("gpt-", "claude"))
    if normalized_type == "anthropic":
        return not normalized_model.startswith(("gpt-", "gemini"))
    return True


def _provider_is_usable(provider: dict[str, Any] | None) -> bool:
    payload = provider or {}
    api_type = _normalize_api_type(str(payload.get("api_type", "") or "openai"))
    model = str(payload.get("model", "") or "").strip()
    if not model:
        return False
    if not _provider_model_is_compatible(api_type, model):
        return False
    if api_type == "openai_codex":
        return True
    return bool(str(payload.get("api_key", "") or "").strip())


def _warn_once(logger: Any, key: str, message: str) -> None:
    if logger is None:
        return
    with _ROUTE_WARNING_LOCK:
        if key in _EMITTED_ROUTE_WARNINGS:
            return
        _EMITTED_ROUTE_WARNINGS.add(key)
    try:
        logger.warning(message)
    except Exception:
        pass


def _provider_summary(provider: dict[str, Any] | None) -> str:
    payload = provider or {}
    api_type = str(payload.get("api_type", "") or "").strip() or "unset"
    model = str(payload.get("model", "") or "").strip() or "unset"
    return f"{api_type}:{model}"


def _legacy_source_field_names(source: str) -> tuple[str, ...]:
    mapping = {
        "legacy_vision_fallback": (
            "personification_vision_fallback_provider",
            "personification_vision_fallback_model",
            "personification_labeler_api_type",
            "personification_labeler_api_url",
            "personification_labeler_api_key",
            "personification_labeler_model",
        ),
        "legacy_labeler": (
            "personification_labeler_api_type",
            "personification_labeler_api_url",
            "personification_labeler_api_key",
            "personification_labeler_model",
        ),
        "legacy_style": (
            "personification_style_api_type",
            "personification_style_api_url",
            "personification_style_api_key",
            "personification_style_api_model",
        ),
        "legacy_persona": (
            "personification_persona_api_type",
            "personification_persona_api_url",
            "personification_persona_api_key",
            "personification_persona_model",
        ),
        "legacy_compress": (
            "personification_compress_api_type",
            "personification_compress_api_url",
            "personification_compress_api_key",
            "personification_compress_model",
        ),
    }
    return mapping.get(str(source or "").strip(), ())


def _field_is_configured(plugin_config: Any, field_name: str) -> bool:
    value = getattr(plugin_config, field_name, "")
    if isinstance(value, bool):
        return bool(value)
    return bool(str(value or "").strip())


def _describe_legacy_source(plugin_config: Any, source: str, provider: dict[str, Any] | None) -> str:
    field_names = [
        field_name
        for field_name in _legacy_source_field_names(source)
        if _field_is_configured(plugin_config, field_name)
    ]
    fields_text = ", ".join(field_names) if field_names else "unknown_fields"
    return f"{source} ({_provider_summary(provider)}; configured fields: {fields_text})"


def _get_primary_provider_list(plugin_config: Any, logger: Any) -> list[dict[str, Any]]:
    from .provider_router import get_configured_api_providers as get_configured_api_providers_core

    try:
        providers = list(get_configured_api_providers_core(plugin_config, logger) or [])
    except Exception:
        providers = []
    return [dict(provider) for provider in providers if isinstance(provider, dict)]


def get_primary_provider_config(plugin_config: Any, logger: Any) -> dict[str, str]:
    providers = _get_primary_provider_list(plugin_config, logger)
    if providers:
        primary = providers[0]
        return {
            "api_type": str(primary.get("api_type", "") or ""),
            "api_url": str(primary.get("api_url", "") or ""),
            "api_key": str(primary.get("api_key", "") or ""),
            "model": str(primary.get("model", "") or ""),
            "auth_path": str(primary.get("auth_path", "") or ""),
        }
    return {
        "api_type": str(getattr(plugin_config, "personification_api_type", "") or ""),
        "api_url": str(getattr(plugin_config, "personification_api_url", "") or ""),
        "api_key": str(getattr(plugin_config, "personification_api_key", "") or ""),
        "model": str(getattr(plugin_config, "personification_model", "") or ""),
        "auth_path": str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
    }


def _any_values(values: Iterable[Any]) -> bool:
    return any(str(value or "").strip() for value in values)


def _build_provider(
    *,
    api_type: str = "",
    api_url: str = "",
    api_key: str = "",
    model: str = "",
    auth_path: str = "",
    inherit: dict[str, str] | None = None,
) -> dict[str, str] | None:
    base = dict(inherit or {})
    resolved_api_type = _normalize_api_type(api_type or base.get("api_type", "") or "openai")
    resolved_api_url = str(api_url or base.get("api_url", "") or "").strip()
    resolved_api_key = str(api_key or base.get("api_key", "") or "").strip()
    resolved_model = str(model or base.get("model", "") or "").strip()
    resolved_auth_path = str(auth_path or base.get("auth_path", "") or "").strip()
    provider = {
        "api_type": resolved_api_type,
        "api_url": resolved_api_url,
        "api_key": resolved_api_key,
        "model": resolved_model,
        "auth_path": resolved_auth_path,
    }
    return provider if _provider_is_usable(provider) else None


def _resolve_explicit_global_fallback(
    plugin_config: Any,
    *,
    primary_provider: dict[str, str],
) -> ProviderResolution | None:
    del primary_provider
    explicit_values = (
        getattr(plugin_config, "personification_fallback_api_type", ""),
        getattr(plugin_config, "personification_fallback_api_url", ""),
        getattr(plugin_config, "personification_fallback_api_key", ""),
        getattr(plugin_config, "personification_fallback_model", ""),
        getattr(plugin_config, "personification_fallback_auth_path", ""),
    )
    if not _any_values(explicit_values):
        return None
    provider = _build_provider(
        api_type=str(getattr(plugin_config, "personification_fallback_api_type", "") or ""),
        api_url=str(getattr(plugin_config, "personification_fallback_api_url", "") or ""),
        api_key=str(getattr(plugin_config, "personification_fallback_api_key", "") or ""),
        model=str(getattr(plugin_config, "personification_fallback_model", "") or ""),
        auth_path=str(getattr(plugin_config, "personification_fallback_auth_path", "") or ""),
    )
    if provider is None:
        return None
    return ProviderResolution(provider=provider, source="fallback")


def _collect_legacy_global_fallback_candidates(
    plugin_config: Any,
    *,
    primary_provider: dict[str, str],
) -> list[ProviderResolution]:
    del primary_provider
    candidates: list[ProviderResolution] = []

    vision_provider = str(getattr(plugin_config, "personification_vision_fallback_provider", "") or "")
    labeler_api_type = str(getattr(plugin_config, "personification_labeler_api_type", "") or "")
    vision_model = str(getattr(plugin_config, "personification_vision_fallback_model", "") or "")
    labeler_api_url = str(getattr(plugin_config, "personification_labeler_api_url", "") or "")
    labeler_api_key = str(getattr(plugin_config, "personification_labeler_api_key", "") or "")
    labeler_model = str(getattr(plugin_config, "personification_labeler_model", "") or "")
    legacy_vision_configured = _any_values(
        (
            vision_provider,
            vision_model,
        )
    )
    if bool(getattr(plugin_config, "personification_vision_fallback_enabled", True)) and legacy_vision_configured:
        legacy_vision = _build_provider(
            api_type=vision_provider or labeler_api_type,
            api_url=labeler_api_url,
            api_key=labeler_api_key,
            model=vision_model or labeler_model,
            auth_path=str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
        )
        if legacy_vision is not None:
            candidates.append(ProviderResolution(provider=legacy_vision, source="legacy_vision_fallback"))

    labeler_configured = _any_values((labeler_api_url, labeler_api_key)) or (
        _normalize_api_type(labeler_api_type) == "openai_codex" and _any_values((labeler_model,))
    )
    if labeler_configured:
        labeler = _build_provider(
            api_type=labeler_api_type,
            api_url=labeler_api_url,
            api_key=labeler_api_key,
            model=labeler_model,
            auth_path=str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
        )
        if labeler is not None:
            candidates.append(ProviderResolution(provider=labeler, source="legacy_labeler"))

    style_values = (
        getattr(plugin_config, "personification_style_api_type", ""),
        getattr(plugin_config, "personification_style_api_url", ""),
        getattr(plugin_config, "personification_style_api_key", ""),
        getattr(plugin_config, "personification_style_api_model", ""),
    )
    if _any_values(style_values):
        style = _build_provider(
            api_type=str(getattr(plugin_config, "personification_style_api_type", "") or ""),
            api_url=str(getattr(plugin_config, "personification_style_api_url", "") or ""),
            api_key=str(getattr(plugin_config, "personification_style_api_key", "") or ""),
            model=str(getattr(plugin_config, "personification_style_api_model", "") or ""),
            auth_path=str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
        )
        if style is not None:
            candidates.append(ProviderResolution(provider=style, source="legacy_style"))

    persona_values = (
        getattr(plugin_config, "personification_persona_api_type", ""),
        getattr(plugin_config, "personification_persona_api_url", ""),
        getattr(plugin_config, "personification_persona_api_key", ""),
        getattr(plugin_config, "personification_persona_model", ""),
    )
    if _any_values(persona_values):
        persona = _build_provider(
            api_type=str(getattr(plugin_config, "personification_persona_api_type", "") or ""),
            api_url=str(getattr(plugin_config, "personification_persona_api_url", "") or ""),
            api_key=str(getattr(plugin_config, "personification_persona_api_key", "") or ""),
            model=str(getattr(plugin_config, "personification_persona_model", "") or ""),
            auth_path=str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
        )
        if persona is not None:
            candidates.append(ProviderResolution(provider=persona, source="legacy_persona"))

    compress_values = (
        getattr(plugin_config, "personification_compress_api_type", ""),
        getattr(plugin_config, "personification_compress_api_url", ""),
        getattr(plugin_config, "personification_compress_api_key", ""),
        getattr(plugin_config, "personification_compress_model", ""),
    )
    if _any_values(compress_values):
        compress = _build_provider(
            api_type=str(getattr(plugin_config, "personification_compress_api_type", "") or ""),
            api_url=str(getattr(plugin_config, "personification_compress_api_url", "") or ""),
            api_key=str(getattr(plugin_config, "personification_compress_api_key", "") or ""),
            model=str(getattr(plugin_config, "personification_compress_model", "") or ""),
            auth_path=str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
        )
        if compress is not None:
            candidates.append(ProviderResolution(provider=compress, source="legacy_compress"))
    return candidates


def resolve_global_fallback_provider(
    plugin_config: Any,
    logger: Any = None,
    *,
    warn: bool = False,
) -> ProviderResolution | None:
    if not bool(getattr(plugin_config, "personification_fallback_enabled", True)):
        return None
    primary_provider = get_primary_provider_config(plugin_config, logger)
    explicit = _resolve_explicit_global_fallback(
        plugin_config,
        primary_provider=primary_provider,
    )
    if explicit is not None:
        return explicit

    candidates = _collect_legacy_global_fallback_candidates(
        plugin_config,
        primary_provider=primary_provider,
    )
    if not candidates:
        return None

    chosen = candidates[0]
    chosen_signature = _provider_signature(chosen.provider)
    ignored_sources = tuple(
        candidate.source
        for candidate in candidates[1:]
        if _provider_signature(candidate.provider) != chosen_signature
    )
    resolved = ProviderResolution(
        provider=chosen.provider,
        source=chosen.source,
        ignored_sources=ignored_sources,
    )
    if warn:
        _warn_once(
            logger,
            f"legacy-fallback:{resolved.source}:{chosen_signature}",
            "personification: deprecated dedicated AI configs now act only as global fallback aliases; "
            f"using {_describe_legacy_source(plugin_config, resolved.source, resolved.provider)}. "
            "Please migrate to personification_fallback_*.",
        )
        if ignored_sources:
            _warn_once(
                logger,
                f"legacy-fallback-ignored:{resolved.source}:{ignored_sources}",
                "personification: multiple deprecated fallback sources were configured; "
                f"using {resolved.source}, ignoring {', '.join(ignored_sources)}.",
            )
    return resolved


def resolve_video_fallback_provider(
    plugin_config: Any,
    logger: Any = None,
    *,
    warn: bool = False,
) -> ProviderResolution | None:
    if not bool(getattr(plugin_config, "personification_video_fallback_enabled", True)):
        return None
    global_fallback = resolve_global_fallback_provider(plugin_config, logger, warn=warn)
    base_provider = dict(global_fallback.provider) if global_fallback is not None else {}
    explicit_values = (
        getattr(plugin_config, "personification_video_fallback_provider", ""),
        getattr(plugin_config, "personification_video_fallback_api_url", ""),
        getattr(plugin_config, "personification_video_fallback_api_key", ""),
        getattr(plugin_config, "personification_video_fallback_model", ""),
        getattr(plugin_config, "personification_video_fallback_auth_path", ""),
    )
    if _any_values(explicit_values):
        provider = _build_provider(
            api_type=str(getattr(plugin_config, "personification_video_fallback_provider", "") or ""),
            api_url=str(getattr(plugin_config, "personification_video_fallback_api_url", "") or ""),
            api_key=str(getattr(plugin_config, "personification_video_fallback_api_key", "") or ""),
            model=str(getattr(plugin_config, "personification_video_fallback_model", "") or ""),
            auth_path=str(getattr(plugin_config, "personification_video_fallback_auth_path", "") or ""),
            inherit=base_provider,
        )
        if provider is None:
            return None
        return ProviderResolution(provider=provider, source="video_fallback")
    if global_fallback is None:
        return None
    return ProviderResolution(provider=dict(global_fallback.provider), source="global_fallback")


def format_provider_summary(provider: dict[str, Any] | None) -> str:
    return _provider_summary(provider)


def summarize_route_state(plugin_config: Any, logger: Any = None) -> dict[str, str]:
    providers = _get_primary_provider_list(plugin_config, logger)
    primary_summary = ", ".join(
        format_provider_summary(provider) for provider in providers
    ) or format_provider_summary(get_primary_provider_config(plugin_config, logger))
    global_fallback = resolve_global_fallback_provider(plugin_config, logger, warn=False)
    video_fallback = resolve_video_fallback_provider(plugin_config, logger, warn=False)
    return {
        "primary": primary_summary,
        "fallback": format_provider_summary(global_fallback.provider) if global_fallback is not None else "关闭/未配置",
        "fallback_source": global_fallback.source if global_fallback is not None else "",
        "fallback_ignored": ", ".join(global_fallback.ignored_sources) if global_fallback is not None else "",
        "video_fallback": format_provider_summary(video_fallback.provider) if video_fallback is not None else "关闭/未配置",
        "video_fallback_source": video_fallback.source if video_fallback is not None else "",
    }


class _ProviderConfigProxy:
    def __init__(
        self,
        original: Any,
        provider: dict[str, Any],
        *,
        thinking_mode_override: str = "",
        model_override: str = "",
    ) -> None:
        self._original = original
        self._provider = dict(provider or {})
        self._thinking_mode_override = str(thinking_mode_override or "").strip()
        self._model_override = str(model_override or "").strip()

    def __getattr__(self, name: str) -> Any:
        if name == "personification_api_type":
            return self._provider.get("api_type", "openai")
        if name == "personification_api_url":
            return self._provider.get("api_url", "")
        if name == "personification_api_key":
            return self._provider.get("api_key", "")
        if name == "personification_model":
            if self._model_override:
                return self._model_override
            return self._provider.get("model", "")
        if name == "personification_codex_auth_path":
            return self._provider.get("auth_path", "")
        if name == "personification_thinking_mode" and self._thinking_mode_override:
            return self._thinking_mode_override
        return getattr(self._original, name)


def _is_invalid_tool_response(response: ToolCallerResponse) -> bool:
    if response.vision_unavailable:
        return True
    if response.tool_calls:
        return False
    if str(response.content or "").strip():
        normalized = " ".join(str(response.content or "").strip().lower().split())
        return normalized in {
            "i can't discuss that.",
            "i cant discuss that.",
            "i cannot discuss that.",
            "i'm sorry, but i can't discuss that.",
            "抱歉，我不能讨论这个。",
            "抱歉，我无法讨论这个。",
        }
    return True


class RoutedToolCaller(ToolCaller):
    def __init__(
        self,
        *,
        primary_callers: list[ToolCaller],
        fallback_caller: ToolCaller | None,
        logger: Any,
    ) -> None:
        self._primary_callers = list(primary_callers)
        self._fallback_caller = fallback_caller
        self._logger = logger
        self._tool_call_callers: dict[str, ToolCaller] = {}
        self._default_result_caller: ToolCaller | None = primary_callers[0] if primary_callers else fallback_caller

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        last_error: Exception | None = None
        saw_vision_unavailable = False
        call_chain = list(self._primary_callers)
        if self._fallback_caller is not None:
            call_chain.append(self._fallback_caller)
        for caller in call_chain:
            try:
                response = await caller.chat_with_tools(messages, tools, use_builtin_search)
            except Exception as exc:
                last_error = exc
                continue
            if response.vision_unavailable:
                saw_vision_unavailable = True
                continue
            if _is_invalid_tool_response(response):
                continue
            self._default_result_caller = caller
            for tool_call in list(response.tool_calls or []):
                call_id = str(getattr(tool_call, "id", "") or "").strip()
                if call_id:
                    self._tool_call_callers[call_id] = caller
            return response
        if saw_vision_unavailable:
            return ToolCallerResponse(
                finish_reason="stop",
                content="",
                tool_calls=[],
                raw=None,
                vision_unavailable=True,
            )
        if last_error is not None:
            raise last_error
        return ToolCallerResponse(
            finish_reason="stop",
            content="",
            tool_calls=[],
            raw=None,
        )

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        caller = self._tool_call_callers.pop(str(tool_call_id or "").strip(), None)
        if caller is None:
            caller = self._default_result_caller
        if caller is None:
            raise RuntimeError("no routed tool caller available")
        return caller.build_tool_result_message(tool_call_id, tool_name, result)


def build_routed_tool_caller(
    plugin_config: Any,
    logger: Any,
    *,
    thinking_mode_override: str = "",
    model_override: str = "",
) -> ToolCaller:
    providers = _get_primary_provider_list(plugin_config, logger)
    primary_callers = [
        build_tool_caller(
            _ProviderConfigProxy(
                plugin_config,
                provider,
                thinking_mode_override=thinking_mode_override,
                model_override=model_override,
            )
        )
        for provider in providers
    ]
    fallback_resolution = resolve_global_fallback_provider(plugin_config, logger, warn=True)
    fallback_caller = None
    if fallback_resolution is not None:
        fallback_signature = _provider_signature(fallback_resolution.provider)
        primary_signatures = {_provider_signature(provider) for provider in providers}
        if fallback_signature not in primary_signatures:
            fallback_caller = build_tool_caller(
                _ProviderConfigProxy(
                    plugin_config,
                    fallback_resolution.provider,
                    thinking_mode_override=thinking_mode_override,
                    model_override=model_override,
                )
            )
    if not primary_callers and fallback_caller is None:
        return build_tool_caller(plugin_config)
    return RoutedToolCaller(
        primary_callers=primary_callers,
        fallback_caller=fallback_caller,
        logger=logger,
    )


def build_fallback_vision_caller(
    plugin_config: Any,
    logger: Any = None,
    *,
    warn: bool = False,
) -> Any:
    resolution = resolve_global_fallback_provider(plugin_config, logger, warn=warn)
    if resolution is None:
        return None

    class _FallbackVisionConfig:
        def __init__(self, original: Any, provider: dict[str, Any]) -> None:
            self._original = original
            self._provider = dict(provider or {})

        def __getattr__(self, name: str) -> Any:
            if name == "personification_labeler_api_type":
                return self._provider.get("api_type", "openai")
            if name == "personification_labeler_api_url":
                return self._provider.get("api_url", "")
            if name == "personification_labeler_api_key":
                return self._provider.get("api_key", "")
            if name == "personification_labeler_model":
                return self._provider.get("model", "")
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
            if name == "personification_vision_fallback_enabled":
                return False
            return getattr(self._original, name)

    return build_vision_caller(_FallbackVisionConfig(plugin_config, resolution.provider))


__all__ = [
    "ProviderResolution",
    "RoutedToolCaller",
    "build_fallback_vision_caller",
    "build_routed_tool_caller",
    "format_provider_summary",
    "get_primary_provider_config",
    "resolve_global_fallback_provider",
    "resolve_video_fallback_provider",
    "summarize_route_state",
]
