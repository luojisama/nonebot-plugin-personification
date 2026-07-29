from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .llm_context import current_llm_context, use_single_attempt_retry_policy
from .safety_filter import build_safe_reframe_messages, detect_route_safety_issue


_EMITTED_ROUTE_WARNINGS: set[str] = set()
_ROUTE_WARNING_LOCK = threading.RLock()
_CANONICAL_PROVIDER_CODES = {
    "provider_auth_failed",
    "provider_call_failed",
    "provider_caller_unavailable",
    "provider_invalid_response",
    "provider_model_candidate_unavailable",
    "provider_model_unavailable",
    "provider_network_failed",
    "provider_permission_denied",
    "provider_request_rejected",
    "provider_safety_block",
    "provider_timeout",
    "providers_exhausted",
}
_MODEL_UNAVAILABLE_ERROR_CODES = {
    "invalid_model",
    "model_deprecated",
    "model_not_available",
    "model_not_found",
    "unsupported_model",
}


def _tool_caller_impl() -> Any:
    from ..skills.skillpacks.tool_caller.scripts import impl

    return impl


def _build_tool_caller(config: Any) -> Any:
    return _tool_caller_impl().build_tool_caller(config)


def _tool_caller_response_cls() -> Any:
    return _tool_caller_impl().ToolCallerResponse


def _build_vision_caller(config: Any) -> Any:
    from ..skills.skillpacks.vision_caller.scripts.impl import build_vision_caller

    return build_vision_caller(config)


@dataclass(frozen=True)
class ProviderResolution:
    provider: dict[str, str]
    source: str
    ignored_sources: tuple[str, ...] = ()


class RoutedToolCallerError(RuntimeError):
    def __init__(self, attempts: list[dict[str, Any]]) -> None:
        self.route_attempts = tuple(dict(item) for item in attempts)
        selected = self._select_attempt(attempts)
        self.code = str(selected.get("code") or "providers_exhausted")
        self.status_code = int(selected.get("status_code") or 0)
        self.retryable = bool(selected.get("retryable", True))
        self.auth_mode = str(selected.get("auth_mode") or "")[:48]
        self.request_count = sum(max(1, int(item.get("request_count") or 1)) for item in attempts)
        self.request_kind = str(selected.get("request_kind") or "")[:32]
        self.request_purpose = str(selected.get("request_purpose") or "")[:64]
        self.message_count = max(0, int(selected.get("message_count") or 0))
        self.prompt_chars = max(0, int(selected.get("prompt_chars") or 0))
        self.tools_count = max(0, int(selected.get("tools_count") or 0))
        self.wire_tools_count = max(0, int(selected.get("wire_tools_count", self.tools_count) or 0))
        self.tool_names_hash = str(selected.get("tool_names_hash") or "")[:16]
        self.tool_schema_hash = str(selected.get("tool_schema_hash") or "")[:16]
        self.builtin_search = bool(selected.get("builtin_search", False))
        super().__init__("all routed tool callers failed")

    @staticmethod
    def _select_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
        priorities = (
            lambda item: item.get("code") == "provider_model_candidate_unavailable",
            lambda item: item.get("code") == "provider_auth_failed" or item.get("status_code") == 401,
            lambda item: item.get("code") == "provider_permission_denied" or item.get("status_code") == 403,
            lambda item: item.get("code") == "provider_request_rejected" or (
                item.get("status_code") in {400, 422}
                and item.get("code") not in {"provider_model_candidate_unavailable", "provider_model_unavailable"}
            ),
            lambda item: item.get("code") == "provider_model_unavailable" or item.get("status_code") == 404,
            lambda item: bool(item.get("retryable")),
        )
        for predicate in priorities:
            matched = next((item for item in attempts if predicate(item)), None)
            if matched is not None:
                return matched
        return attempts[-1] if attempts else {"code": "providers_exhausted", "retryable": True}


def _route_trace_atom(value: Any, *, limit: int) -> str:
    text = "_".join(str(value or "").split())
    text = text.replace("|", "_").replace("=", "_")
    return text[:limit] or "-"


def summarize_provider_route_attempts(exc: BaseException, *, limit: int = 3) -> str:
    current: BaseException | None = exc
    seen: set[int] = set()
    attempts: list[dict[str, Any]] = []
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        candidate = getattr(current, "route_attempts", None)
        if isinstance(candidate, (list, tuple)):
            attempts = [dict(item) for item in candidate if isinstance(item, dict)]
            break
        current = current.__cause__ or current.__context__
    if not attempts:
        return ""
    rendered: list[str] = [f"route_count={len(attempts)}"]
    for index, attempt in enumerate(attempts[: max(1, int(limit or 1))], start=1):
        code = str(attempt.get("code") or "provider_call_failed").strip().lower()
        if code not in _CANONICAL_PROVIDER_CODES:
            code = "provider_call_failed"
        try:
            status = max(0, int(attempt.get("status_code") or 0))
        except (TypeError, ValueError):
            status = 0
        try:
            tools_count = max(0, int(attempt.get("wire_tools_count") or 0))
        except (TypeError, ValueError):
            tools_count = 0
        try:
            request_count = max(1, int(attempt.get("request_count") or 1))
        except (TypeError, ValueError):
            request_count = 1
        route = "|".join(
            (
                f"provider:{_route_trace_atom(attempt.get('provider'), limit=48)}",
                f"api:{_route_trace_atom(attempt.get('api_type'), limit=32)}",
                f"model:{_route_trace_atom(attempt.get('concrete_model') or attempt.get('model'), limit=80)}",
                f"http:{status or '-'}",
                f"tools:{tools_count}",
                f"schema:{_route_trace_atom(attempt.get('tool_schema_hash') or attempt.get('tool_names_hash'), limit=16)}",
                f"requests:{request_count}",
                f"code:{code}",
            )
        )
        rendered.append(f"route_{index}={route}")
    return " ".join(rendered)


def _exception_route_metadata(exc: BaseException) -> tuple[int, str, bool, str, str, str, int]:
    current: BaseException | None = exc
    seen: set[int] = set()
    status_code = 0
    code = ""
    retryable = False
    concrete_model = ""
    next_model = ""
    auth_mode = ""
    request_count = 0
    saw_model_unavailable_text = False
    class_names: list[str] = []
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        class_names.append(type(current).__name__.lower())
        candidate_code = str(getattr(current, "code", "") or "").strip().lower()
        current_text = str(current or "").strip().lower()
        saw_model_unavailable_text = saw_model_unavailable_text or any(
            marker in current_text
            for marker in (
                "model does not exist",
                "model is invalid",
                "model is not available",
                "model not found",
                "model unavailable",
                "model was not found",
            )
        )
        if (
            candidate_code in _MODEL_UNAVAILABLE_ERROR_CODES
            and code != "provider_model_candidate_unavailable"
        ):
            code = "provider_model_unavailable"
        elif candidate_code == "provider_model_candidate_unavailable":
            code = candidate_code
        elif candidate_code == "provider_model_unavailable" and code != "provider_model_candidate_unavailable":
            code = candidate_code
        elif not code and candidate_code in _CANONICAL_PROVIDER_CODES:
            code = candidate_code
        if not status_code:
            values = (
                getattr(current, "status_code", None),
                getattr(getattr(current, "response", None), "status_code", None),
            )
            for value in values:
                try:
                    status_code = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if status_code:
                    break
        retryable = retryable or getattr(current, "retryable", False) is True
        if not concrete_model:
            concrete_model = str(getattr(current, "concrete_model", "") or "").strip()
        if not next_model:
            next_model = str(getattr(current, "next_model", "") or "").strip()
        if not auth_mode:
            auth_mode = str(getattr(current, "auth_mode", "") or "").strip().lower()
        if not request_count:
            try:
                request_count = int(getattr(current, "request_count", 0) or 0)
            except (TypeError, ValueError):
                request_count = 0
        current = current.__cause__ or current.__context__
    if status_code == 401:
        code = "provider_auth_failed"
    elif status_code == 403:
        code = "provider_permission_denied"
    elif saw_model_unavailable_text and code != "provider_model_candidate_unavailable":
        code = "provider_model_unavailable"
    elif status_code in {400, 422}:
        if code not in {"provider_model_candidate_unavailable", "provider_model_unavailable"}:
            code = "provider_request_rejected"
    elif status_code == 404:
        code = (
            "provider_model_candidate_unavailable"
            if code == "provider_model_candidate_unavailable"
            else "provider_model_unavailable"
        )
    elif any("timeout" in name for name in class_names):
        code = "provider_timeout"
        retryable = True
    elif any(
        marker in name
        for name in class_names
        for marker in ("connect", "network", "protocol", "readerror", "writeerror", "dnserror")
    ):
        code = "provider_network_failed"
        retryable = True
    elif not code:
        code = "provider_call_failed"
    if status_code in {400, 401, 403, 422} or (
        status_code == 404 and code != "provider_model_candidate_unavailable"
    ):
        retryable = False
    elif status_code in {408, 409, 425, 429} or status_code >= 500:
        retryable = True
    return (
        status_code,
        code,
        retryable,
        concrete_model[:120],
        next_model[:120],
        auth_mode[:48],
        max(1, request_count),
    )


def _exception_wire_tools_count(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        if hasattr(current, "wire_tools_count"):
            try:
                return max(0, int(getattr(current, "wire_tools_count", 0) or 0))
            except (TypeError, ValueError):
                return 0
        current = current.__cause__ or current.__context__
    return None


def _response_wire_tools_count(response: Any, fallback: int) -> int:
    value = getattr(response, "wire_tools_count", None)
    if value is None:
        value = fallback
        try:
            response.wire_tools_count = max(0, int(value or 0))
        except Exception:
            pass
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return max(0, int(fallback or 0))


def _attach_response_wire_tools_count(exc: BaseException, response: Any, fallback: int) -> None:
    try:
        exc.wire_tools_count = _response_wire_tools_count(response, fallback)
    except Exception:
        pass


def _request_text_chars(messages: list[dict]) -> int:
    total = 0
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += len(str(part.get("text", "") or ""))
    return total


def _request_tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function", tool)
    if not isinstance(function, dict):
        return ""
    return str(function.get("name", "") or "").strip()


def _safe_request_shape(
    messages: list[dict],
    tools: list[dict],
    use_builtin_search: bool,
) -> dict[str, Any]:
    names = sorted(name for name in (_request_tool_name(tool) for tool in list(tools or [])) if name)
    names_hash = hashlib.sha256("\0".join(names).encode("utf-8")).hexdigest()[:12] if names else ""
    schema_json = json.dumps(
        list(tools or []),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    schema_hash = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()[:12] if tools else ""
    purpose = str(current_llm_context().get("purpose") or "").strip()[:64]
    return {
        "request_kind": "function_calling" if tools else "text",
        "request_purpose": purpose,
        "message_count": len(list(messages or [])),
        "prompt_chars": _request_text_chars(messages),
        "tools_count": len(list(tools or [])),
        "tool_names_hash": names_hash,
        "tool_schema_hash": schema_hash,
        "builtin_search": bool(use_builtin_search),
    }


def _normalize_api_type(api_type: str) -> str:
    value = str(api_type or "").strip().lower().replace("-", "_")
    if value in {"gemini", "gemini_official"}:
        return "gemini_official"
    if value == "anthropic":
        return "anthropic"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    if value in {"gemini_cli", "geminicli"}:
        return "gemini_cli"
    if value in {"antigravity_cli", "antigravity", "agy", "agy_cli"}:
        return "antigravity_cli"
    if value in {"claude_code", "claudecode", "claude_cli"}:
        return "claude_code"
    return "openai"


def _provider_signature(provider: dict[str, Any] | None) -> tuple[str, str, str, str, str, str]:
    payload = provider or {}
    api_type = _normalize_api_type(str(payload.get("api_type", "") or ""))
    return (
        api_type,
        str(payload.get("api_url", "") or "").strip(),
        str(payload.get("api_key", "") or "").strip(),
        str(payload.get("model", "") or "").strip(),
        str(payload.get("auth_path", "") or "").strip(),
        (
            str(payload.get("gemini_auth_mode", "auto") or "auto").strip().lower()
            if api_type == "gemini_official"
            else ""
        ),
    )


def _provider_model_is_compatible(api_type: str, model: str) -> bool:
    normalized_type = _normalize_api_type(api_type)
    normalized_model = str(model or "").strip().lower()
    if not normalized_model:
        return False
    if normalized_type == "openai_codex":
        return not normalized_model.startswith(("gemini", "claude"))
    if normalized_type in {"gemini_official", "gemini_cli", "antigravity_cli"}:
        return not normalized_model.startswith(("gpt-", "claude"))
    if normalized_type in {"anthropic", "claude_code"}:
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
    if api_type in {"openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}:
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


def list_primary_providers(plugin_config: Any, logger: Any) -> list[dict[str, Any]]:
    """返回当前配置的全部主回复 provider（api_pools 解析后），供 WebUI 逐个测试。"""
    return _get_primary_provider_list(plugin_config, logger)


def build_single_provider_caller(
    plugin_config: Any,
    provider: dict[str, Any],
    *,
    thinking_mode_override: str = "",
    model_override: str = "",
) -> ToolCaller:
    """为单个 provider 构建独立 caller（不走路由/回退），用于逐个连通性测试。"""
    return _build_tool_caller(
        _ProviderConfigProxy(
            plugin_config,
            provider,
            thinking_mode_override=thinking_mode_override,
            model_override=model_override,
        )
    )


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
            "gemini_auth_mode": str(primary.get("gemini_auth_mode", "auto") or "auto"),
        }
    return {
        "api_type": str(getattr(plugin_config, "personification_api_type", "") or ""),
        "api_url": str(getattr(plugin_config, "personification_api_url", "") or ""),
        "api_key": str(getattr(plugin_config, "personification_api_key", "") or ""),
        "model": str(getattr(plugin_config, "personification_model", "") or ""),
        "auth_path": str(getattr(plugin_config, "personification_codex_auth_path", "") or ""),
        "gemini_auth_mode": str(
            getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto"
        ),
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
    gemini_auth_mode: str = "",
    inherit: dict[str, str] | None = None,
) -> dict[str, str] | None:
    base = dict(inherit or {})
    resolved_api_type = _normalize_api_type(api_type or base.get("api_type", "") or "openai")
    resolved_api_url = str(api_url or base.get("api_url", "") or "").strip()
    resolved_api_key = str(api_key or base.get("api_key", "") or "").strip()
    resolved_model = str(model or base.get("model", "") or "").strip()
    resolved_auth_path = str(auth_path or base.get("auth_path", "") or "").strip()
    resolved_gemini_auth_mode = str(
        gemini_auth_mode or base.get("gemini_auth_mode", "auto") or "auto"
    ).strip()
    provider = {
        "api_type": resolved_api_type,
        "api_url": resolved_api_url,
        "api_key": resolved_api_key,
        "model": resolved_model,
        "auth_path": resolved_auth_path,
        "gemini_auth_mode": resolved_gemini_auth_mode,
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
        gemini_auth_mode=str(
            getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto"
        ),
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
    gemini_auth_mode = str(
        getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto"
    )

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
            gemini_auth_mode=gemini_auth_mode,
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
            gemini_auth_mode=gemini_auth_mode,
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
            gemini_auth_mode=gemini_auth_mode,
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
            gemini_auth_mode=gemini_auth_mode,
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
            gemini_auth_mode=gemini_auth_mode,
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
            gemini_auth_mode=str(
                getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto"
            ),
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
        if name == "personification_proxy":
            return self._provider.get("proxy", "")
        if name == "personification_gemini_auth_mode":
            return self._provider.get(
                "gemini_auth_mode",
                getattr(self._original, "personification_gemini_auth_mode", "auto"),
            )
        if name == "personification_provider_timeout":
            return self._provider.get("timeout", 200.0)
        if name == "personification_model":
            if self._model_override:
                return self._model_override
            return self._provider.get("model", "")
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
        if name == "personification_claude_code_auth_path":
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
        return False
    return True


class RoutedToolCaller:
    def __init__(
        self,
        *,
        primary_callers: list[ToolCaller],
        fallback_caller: ToolCaller | None,
        logger: Any,
        route_descriptors: list[dict[str, Any]] | None = None,
    ) -> None:
        self._primary_callers = list(primary_callers)
        self._fallback_caller = fallback_caller
        self._logger = logger
        self._caller_route_keys: dict[int, str] = {}
        self._caller_by_route_key: dict[str, ToolCaller] = {}
        self._caller_route_descriptors: dict[int, dict[str, Any]] = {}
        descriptors = list(route_descriptors or [])
        for index, caller in enumerate([*self._primary_callers, *([self._fallback_caller] if self._fallback_caller else [])]):
            key = f"{index}:{type(caller).__name__}"
            self._caller_route_keys[id(caller)] = key
            self._caller_by_route_key[key] = caller
            source = descriptors[index] if index < len(descriptors) else {}
            self._caller_route_descriptors[id(caller)] = {
                "provider": str(source.get("name") or f"route-{index + 1}")[:80],
                "api_type": str(source.get("api_type") or "")[:48],
                "model": str(source.get("model") or "")[:120],
                "auth_mode": str(source.get("gemini_auth_mode") or "")[:48],
                "caller": type(caller).__name__[:80],
            }

    def _route_attempt(
        self,
        caller: ToolCaller,
        exc: BaseException,
        *,
        request_shape: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status_code, code, retryable, concrete_model, next_model, auth_mode, request_count = (
            _exception_route_metadata(exc)
        )
        descriptor = self._caller_route_descriptors.get(id(caller), {"caller": type(caller).__name__[:80]})
        shape = dict(request_shape or {})
        wire_tools_count = _exception_wire_tools_count(exc)
        if wire_tools_count is None:
            wire_tools_count = max(0, int(shape.get("tools_count") or 0))
        return {
            **descriptor,
            **shape,
            "wire_tools_count": wire_tools_count,
            "concrete_model": concrete_model,
            "next_model": next_model,
            "auth_mode": auth_mode or str(descriptor.get("auth_mode") or "")[:48],
            "request_count": request_count,
            "status_code": status_code,
            "code": code,
            "retryable": retryable,
            "exception_type": type(exc).__name__[:80],
        }

    def _caller_from_tool_result_markers(self, messages: list[dict]) -> ToolCaller | None:
        for message in reversed(list(messages or [])):
            if not isinstance(message, dict):
                continue
            route_key = str(message.get("_personification_routed_caller", "") or "").strip()
            if route_key:
                caller = self._caller_by_route_key.get(route_key)
                if caller is not None:
                    return caller
        return None

    def _caller_from_response(self, response: ToolCallerResponse) -> ToolCaller | None:
        route_key = str(getattr(response, "route_key", "") or "").strip()
        if route_key:
            caller = self._caller_by_route_key.get(route_key)
            if caller is not None:
                return caller
        return None

    @staticmethod
    def _strip_route_markers(messages: list[dict]) -> list[dict]:
        cleaned: list[dict] = []
        changed = False
        for message in list(messages or []):
            if isinstance(message, dict) and (
                "_personification_routed_caller" in message
                or "_personification_untrusted" in message
            ):
                cloned = dict(message)
                cloned.pop("_personification_routed_caller", None)
                cloned.pop("_personification_untrusted", None)
                cleaned.append(cloned)
                changed = True
            else:
                cleaned.append(message)
        return cleaned if changed else messages

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        last_error: Exception | None = None
        route_attempts: list[dict[str, Any]] = []
        request_shape = _safe_request_shape(messages, tools, use_builtin_search)
        saw_vision_unavailable = False
        pinned_caller = self._caller_from_tool_result_markers(messages)
        if pinned_caller is not None:
            call_chain = [pinned_caller]
        else:
            call_chain = list(self._primary_callers)
            if self._fallback_caller is not None:
                call_chain.append(self._fallback_caller)
        for caller in call_chain:
            try:
                response = await caller.chat_with_tools(
                    self._strip_route_markers(messages),
                    tools,
                    use_builtin_search,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                route_attempts.append(self._route_attempt(caller, exc, request_shape=request_shape))
                continue
            _response_wire_tools_count(response, len(list(tools or [])))
            safety_issue = detect_route_safety_issue(response)
            if safety_issue:
                if use_single_attempt_retry_policy():
                    last_error = RuntimeError("provider safety block")
                    last_error.code = "provider_safety_block"
                    _attach_response_wire_tools_count(last_error, response, len(list(tools or [])))
                    route_attempts.append(self._route_attempt(caller, last_error, request_shape=request_shape))
                    continue
                try:
                    response = await caller.chat_with_tools(
                        self._strip_route_markers(build_safe_reframe_messages(messages)),
                        tools,
                        use_builtin_search,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    route_attempts.append(self._route_attempt(caller, exc, request_shape=request_shape))
                    continue
                _response_wire_tools_count(response, len(list(tools or [])))
                if detect_route_safety_issue(response):
                    last_error = RuntimeError("provider safety block")
                    last_error.code = "provider_safety_block"
                    _attach_response_wire_tools_count(last_error, response, len(list(tools or [])))
                    route_attempts.append(self._route_attempt(caller, last_error, request_shape=request_shape))
                    continue
            if response.vision_unavailable:
                saw_vision_unavailable = True
                continue
            if _is_invalid_tool_response(response):
                last_error = RuntimeError("provider returned an empty or invalid response")
                last_error.code = "provider_invalid_response"
                last_error.retryable = True
                _attach_response_wire_tools_count(last_error, response, len(list(tools or [])))
                route_attempts.append(self._route_attempt(caller, last_error, request_shape=request_shape))
                continue
            try:
                response.route_key = str(self._caller_route_keys.get(id(caller), "") or "")
            except Exception:
                pass
            return response
        if saw_vision_unavailable:
            return _tool_caller_response_cls()(
                finish_reason="stop",
                content="",
                tool_calls=[],
                raw=None,
                vision_unavailable=True,
            )
        if route_attempts:
            raise RoutedToolCallerError(route_attempts) from last_error
        raise RoutedToolCallerError([
            {
                **request_shape,
                "provider": "routed",
                "api_type": "",
                "model": "",
                "auth_mode": "",
                "request_count": 1,
                "status_code": 0,
                "code": "provider_caller_unavailable",
                "retryable": False,
                "exception_type": "",
            }
        ])

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        del tool_call_id, tool_name, result
        raise RuntimeError("routed tool results require the originating response")

    def build_assistant_tool_calls_message(self, response: ToolCallerResponse) -> dict[str, Any]:
        caller = self._caller_from_response(response)
        if caller is None:
            raise RuntimeError("no routed tool caller available")
        builder = getattr(caller, "build_assistant_tool_calls_message", None)
        if callable(builder):
            return builder(response)
        return _tool_caller_impl().build_assistant_tool_calls_message(response)

    def build_tool_result_messages(
        self,
        response: ToolCallerResponse,
        results: list[tuple[Any, str]],
    ) -> list[dict[str, Any]]:
        caller = self._caller_from_response(response)
        if caller is None:
            raise RuntimeError("no routed tool caller available")
        builder = getattr(caller, "build_tool_result_messages", None)
        if callable(builder):
            messages = list(builder(response, results) or [])
        else:
            messages = _tool_caller_impl().build_tool_result_messages(caller, response, results)
        route_key = self._caller_route_keys.get(id(caller), "")
        routed_messages: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            cloned = dict(message)
            if route_key:
                cloned["_personification_routed_caller"] = route_key
            routed_messages.append(cloned)
        return routed_messages

    def build_synthetic_tool_evidence_message(
        self,
        response: ToolCallerResponse | None,
        tool_name: str,
        tool_args: dict[str, Any],
        result: str,
    ) -> dict[str, Any]:
        message = _tool_caller_impl().build_synthetic_tool_evidence_message(
            tool_name,
            tool_args,
            result,
        )
        route_key = str(getattr(response, "route_key", "") or "").strip() if response is not None else ""
        if route_key and route_key in self._caller_by_route_key:
            message["_personification_routed_caller"] = route_key
        return message


def build_routed_tool_caller(
    plugin_config: Any,
    logger: Any,
    *,
    thinking_mode_override: str = "",
    model_override: str = "",
) -> ToolCaller:
    providers = _get_primary_provider_list(plugin_config, logger)
    primary_callers = [
        _build_tool_caller(
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
    route_descriptors = list(providers)
    if fallback_resolution is not None:
        fallback_signature = _provider_signature(fallback_resolution.provider)
        primary_signatures = {_provider_signature(provider) for provider in providers}
        if fallback_signature not in primary_signatures:
            fallback_caller = _build_tool_caller(
                _ProviderConfigProxy(
                    plugin_config,
                    fallback_resolution.provider,
                    thinking_mode_override=thinking_mode_override,
                    model_override=model_override,
                )
            )
            route_descriptors.append(fallback_resolution.provider)
    if not primary_callers and fallback_caller is None:
        legacy_caller = _build_tool_caller(plugin_config)
        return RoutedToolCaller(
            primary_callers=[legacy_caller],
            fallback_caller=None,
            logger=logger,
            route_descriptors=[get_primary_provider_config(plugin_config, logger)],
        )
    return RoutedToolCaller(
        primary_callers=primary_callers,
        fallback_caller=fallback_caller,
        logger=logger,
        route_descriptors=route_descriptors,
    )


def build_fallback_vision_caller(
    plugin_config: Any,
    logger: Any = None,
    *,
    warn: bool = False,
    model_override: str = "",
) -> Any:
    resolution = resolve_global_fallback_provider(plugin_config, logger, warn=warn)
    if resolution is None:
        return None

    class _FallbackVisionConfig:
        def __init__(self, original: Any, provider: dict[str, Any]) -> None:
            self._original = original
            self._provider = dict(provider or {})
            self._model_override = str(model_override or "").strip()

        def __getattr__(self, name: str) -> Any:
            if name == "personification_labeler_api_type":
                return self._provider.get("api_type", "openai")
            if name == "personification_labeler_api_url":
                return self._provider.get("api_url", "")
            if name == "personification_labeler_api_key":
                return self._provider.get("api_key", "")
            if name == "personification_labeler_model":
                if self._model_override:
                    return self._model_override
                return self._provider.get("model", "")
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
            if name == "personification_vision_fallback_enabled":
                return False
            return getattr(self._original, name)

    return _build_vision_caller(_FallbackVisionConfig(plugin_config, resolution.provider))


__all__ = [
    "ProviderResolution",
    "RoutedToolCaller",
    "build_fallback_vision_caller",
    "build_routed_tool_caller",
    "format_provider_summary",
    "get_primary_provider_config",
    "resolve_global_fallback_provider",
    "resolve_video_fallback_provider",
    "summarize_provider_route_attempts",
    "summarize_route_state",
]
