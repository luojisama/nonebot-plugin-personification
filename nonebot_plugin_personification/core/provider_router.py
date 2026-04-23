import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Optional

from .message_parts import normalize_message_parts
from .visual_capabilities import error_indicates_vision_unavailable, heuristic_supports_vision
from ..skills.skillpacks.tool_caller.scripts.impl import (
    AnthropicToolCaller,
    GeminiToolCaller,
    OpenAICodexToolCaller,
    OpenAIToolCaller,
    ToolCallerResponse,
)


PROVIDER_FAILURE_STATE: Dict[str, Dict[str, Any]] = {}
PROVIDER_ROTATION_CURSOR = 0
_PROVIDER_STATE_LOCK = threading.RLock()
_CURSOR_LOCK = _PROVIDER_STATE_LOCK
_LOGGED_PROVIDER_CONFIG_SIGNATURES: set[tuple[str, tuple[tuple[str, str, str], ...]]] = set()


def normalize_api_type(api_type: Optional[str]) -> str:
    value = (api_type or "openai").strip().lower()
    if value == "gemini_official":
        return "gemini"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    if value not in {"openai", "gemini", "anthropic", "openai_codex"}:
        return "openai"
    return value


def provider_supports_vision(api_type: Optional[str], model: Optional[str] = None) -> bool:
    return heuristic_supports_vision(str(api_type or ""), model)


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _provider_timeout(provider: Dict[str, Any]) -> float:
    default_timeout = 120 if provider["api_type"] in {"gemini", "anthropic"} else 60
    try:
        return float(provider.get("timeout", default_timeout))
    except (TypeError, ValueError):
        return float(default_timeout)


def _provider_max_retries(provider: Dict[str, Any]) -> int:
    return max(1, _to_int(provider.get("max_retries", 2), 2))


def _override_provider_model(provider: Dict[str, Any], model_override: str = "") -> Dict[str, Any]:
    cloned = dict(provider or {})
    override = str(model_override or "").strip()
    if not override:
        return cloned
    if normalize_api_type(cloned.get("api_type")) == "openai_codex":
        cloned["model"] = _normalize_codex_model(override)
    else:
        cloned["model"] = override
    return cloned


def _normalize_codex_model(model: str) -> str:
    value = (model or "").strip()
    if not value:
        return "gpt-5.3-codex"
    lower = value.lower()
    if "codex" in lower:
        return value
    alias_map = {
        "gpt-5.3": "gpt-5.3-codex",
        "gpt-5": "gpt-5-codex",
    }
    return alias_map.get(lower, value)


def _provider_config_signature(
    source: str,
    providers: List[Dict[str, Any]],
) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    return (
        source,
        tuple(
            (
                str(provider.get("name", "") or ""),
                str(provider.get("api_type", "") or ""),
                str(provider.get("model", "") or ""),
            )
            for provider in providers
        ),
    )


def _log_active_provider_config_once(
    *,
    logger: Any,
    source: str,
    providers: List[Dict[str, Any]],
) -> None:
    signature = _provider_config_signature(source, providers)
    with _PROVIDER_STATE_LOCK:
        if signature in _LOGGED_PROVIDER_CONFIG_SIGNATURES:
            return
        _LOGGED_PROVIDER_CONFIG_SIGNATURES.add(signature)
    summary = ", ".join(
        f"{provider.get('name') or 'unnamed'}({provider.get('api_type') or 'unset'}:{provider.get('model') or 'unset'})"
        for provider in providers
    ) or "none"
    try:
        logger.info(
            "personification: active primary provider route "
            f"source={source} count={len(providers)} providers={summary}"
        )
    except Exception:
        pass


def load_api_pool_config(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    raw_config = getattr(plugin_config, "personification_api_pools", None)
    if not raw_config:
        return []

    parsed: Any = raw_config
    if isinstance(raw_config, str):
        text = raw_config.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1].strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"personification: failed to parse personification_api_pools: {e}")
            return []

    if not isinstance(parsed, list):
        logger.error("personification: personification_api_pools must be a JSON array")
        return []

    providers: List[Dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        api_type = normalize_api_type(item.get("api_type"))
        api_key = str(item.get("api_key", "")).strip()
        api_url = str(item.get("api_url", "")).strip()
        model = str(item.get("model", "")).strip()
        auth_path = str(item.get("auth_path", item.get("codex_auth_path", "")) or "").strip()

        if api_type == "openai_codex":
            model = _normalize_codex_model(model)
        elif not api_key or not api_url or not model:
            continue

        provider = {
            "name": str(item.get("name") or f"pool_{index + 1}").strip() or f"pool_{index + 1}",
            "api_type": api_type,
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
            "auth_path": auth_path,
            "enabled": _to_bool(item.get("enabled", True), True),
            "priority": _to_int(item.get("priority", index), index),
            "timeout": _provider_timeout({"api_type": api_type, **item}),
            "max_retries": max(1, _to_int(item.get("max_retries", 2), 2)),
            "supports_native_search": _to_bool(
                item.get("supports_native_search", api_type in {"gemini", "anthropic", "openai", "openai_codex"}),
                api_type in {"gemini", "anthropic", "openai", "openai_codex"},
            ),
            "supports_reasoning": item.get("supports_reasoning"),
        }
        if provider["enabled"]:
            providers.append(provider)

    providers.sort(key=lambda p: (p["priority"], p["name"]))
    return providers


def get_configured_api_providers(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    providers = load_api_pool_config(plugin_config, logger)
    if providers:
        _log_active_provider_config_once(
            logger=logger,
            source="personification_api_pools",
            providers=providers,
        )
        return providers

    legacy_type = normalize_api_type(getattr(plugin_config, "personification_api_type", "openai"))
    if legacy_type == "openai_codex":
        providers = [
            {
                "name": "legacy_primary",
                "api_type": legacy_type,
                "api_url": "",
                "api_key": "",
                "model": str(getattr(plugin_config, "personification_model", "") or "").strip() or "gpt-5.3-codex",
                "auth_path": str(getattr(plugin_config, "personification_codex_auth_path", "") or "").strip(),
                "enabled": True,
                "priority": 0,
                "timeout": 60,
                "max_retries": 2,
                "supports_native_search": True,
            }
        ]
        _log_active_provider_config_once(
            logger=logger,
            source="personification_api_*",
            providers=providers,
        )
        return providers

    api_url = str(getattr(plugin_config, "personification_api_url", "") or "").strip()
    api_key = str(getattr(plugin_config, "personification_api_key", "") or "").strip()
    model = str(getattr(plugin_config, "personification_model", "") or "").strip()
    if not api_key or not api_url or not model:
        return []

    providers = [
        {
            "name": "legacy_primary",
            "api_type": legacy_type,
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
            "auth_path": "",
            "enabled": True,
            "priority": 0,
            "timeout": 120 if legacy_type in {"gemini", "anthropic"} else 60,
            "max_retries": 2,
            "supports_native_search": legacy_type in {"gemini", "anthropic", "openai", "openai_codex"},
        }
    ]
    _log_active_provider_config_once(
        logger=logger,
        source="personification_api_*",
        providers=providers,
    )
    return providers


def get_provider_candidates(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    global PROVIDER_ROTATION_CURSOR

    providers = get_configured_api_providers(plugin_config, logger)
    if not providers:
        return []

    now_ts = time.time()
    available: List[Dict[str, Any]] = []
    cooling: List[Dict[str, Any]] = []
    with _PROVIDER_STATE_LOCK:
        for provider in providers:
            state = PROVIDER_FAILURE_STATE.get(provider["name"], {})
            if state.get("cooldown_until", 0) > now_ts:
                cooling.append(provider)
            else:
                available.append(provider)

    if not available:
        available = cooling

    if len(available) <= 1:
        return available

    with _PROVIDER_STATE_LOCK:
        cursor = PROVIDER_ROTATION_CURSOR % len(available)
        PROVIDER_ROTATION_CURSOR = (PROVIDER_ROTATION_CURSOR + 1) % len(available)
    return available[cursor:] + available[:cursor]


def _mark_provider_success(provider_name: str) -> None:
    with _PROVIDER_STATE_LOCK:
        PROVIDER_FAILURE_STATE.pop(provider_name, None)


def _mark_provider_failure(provider_name: str, error: Exception) -> None:
    now_ts = time.time()
    with _PROVIDER_STATE_LOCK:
        state = PROVIDER_FAILURE_STATE.get(provider_name, {})
        failures = int(state.get("failures", 0)) + 1
        PROVIDER_FAILURE_STATE[provider_name] = {
            "failures": failures,
            "cooldown_until": now_ts + min(300, 30 * failures),
            "last_error": str(error),
            "last_failed_at": now_ts,
        }


def _get_thinking_mode(plugin_config: Any) -> str:
    value = str(getattr(plugin_config, "personification_thinking_mode", "none") or "none")
    return value.strip().lower()


def _build_provider_caller(provider: Dict[str, Any], plugin_config: Any):
    if provider["api_type"] == "openai_codex":
        return OpenAICodexToolCaller(
            model=provider["model"],
            auth_path=str(provider.get("auth_path", "") or "").strip(),
            timeout=_provider_timeout(provider),
        )

    thinking_mode = _get_thinking_mode(plugin_config)
    supports_reasoning_raw = provider.get("supports_reasoning")
    supports_reasoning: Optional[bool] = None
    if supports_reasoning_raw is not None:
        supports_reasoning = _to_bool(supports_reasoning_raw, None)
    common_kwargs = {
        "api_key": provider["api_key"],
        "base_url": provider["api_url"],
        "model": provider["model"],
        "thinking_mode": thinking_mode,
    }
    if provider["api_type"] == "gemini":
        return GeminiToolCaller(**common_kwargs)
    if provider["api_type"] == "anthropic":
        return AnthropicToolCaller(
            **common_kwargs,
            timeout=_provider_timeout(provider),
        )
    return OpenAIToolCaller(
        **common_kwargs,
        timeout=_provider_timeout(provider),
        supports_reasoning=supports_reasoning,
    )


def _should_use_builtin_search(provider: Dict[str, Any], use_builtin_search: bool) -> bool:
    if not use_builtin_search:
        return False
    if provider["api_type"] not in {"gemini", "anthropic", "openai", "openai_codex"}:
        return False
    return bool(provider.get("supports_native_search", True))


async def _call_provider_once(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    plugin_config: Any,
    tools: Optional[List[Dict[str, Any]]] = None,
    use_builtin_search: bool = False,
) -> ToolCallerResponse:
    caller = _build_provider_caller(provider, plugin_config)
    return await caller.chat_with_tools(
        messages=messages,
        tools=list(tools or []),
        use_builtin_search=_should_use_builtin_search(provider, use_builtin_search),
    )


def _empty_response() -> ToolCallerResponse:
    return ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw=None,
    )


def _empty_vision_unavailable_response() -> ToolCallerResponse:
    return ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw=None,
        vision_unavailable=True,
    )


def _contains_image_input(messages: List[Dict[str, Any]]) -> bool:
    for message in messages:
        if str(message.get("role", "") or "").strip() not in {"user", "assistant"}:
            continue
        for part in normalize_message_parts(message.get("content", "")):
            if part.get("type") in {"image_url", "image_file"}:
                return True
    return False


GENERIC_REFUSAL_TEXTS = {
    "i can't discuss that.",
    "i cant discuss that.",
    "i cannot discuss that.",
    "i'm sorry, but i can't discuss that.",
    "抱歉，我不能讨论这个。",
    "抱歉，我无法讨论这个。",
}


def _is_generic_refusal_response(response: ToolCallerResponse) -> bool:
    if response.tool_calls:
        return False
    content = (response.content or "").strip()
    if not content:
        return False
    normalized = " ".join(content.lower().split())
    return normalized in GENERIC_REFUSAL_TEXTS


def _is_invalid_route_response(response: ToolCallerResponse) -> bool:
    if response.tool_calls:
        return False
    if response.vision_unavailable:
        return True
    if not str(response.content or "").strip():
        return True
    return _is_generic_refusal_response(response)


def _error_text(error: Exception) -> str:
    text = str(error).strip()
    if text:
        return text
    return f"{type(error).__name__}"


async def _try_provider_chain(
    providers: List[Dict[str, Any]],
    *,
    messages: List[Dict[str, Any]],
    plugin_config: Any,
    logger: Any,
    tools: Optional[List[Dict[str, Any]]] = None,
    use_builtin_search: bool = False,
) -> tuple[ToolCallerResponse | None, list[str], bool]:
    errors: List[str] = []
    contains_image_input = _contains_image_input(messages)
    saw_vision_unavailable = False
    for provider in providers:
        logger.info(
            f"personification: try provider={provider['name']} "
            f"type={provider['api_type']} model={provider['model']}"
        )
        retries = _provider_max_retries(provider)
        skip_provider = False
        for attempt in range(retries):
            try:
                response = await _call_provider_once(
                    provider,
                    messages,
                    plugin_config=plugin_config,
                    tools=tools,
                    use_builtin_search=use_builtin_search,
                )
                if response.vision_unavailable:
                    saw_vision_unavailable = True
                    errors.append(f"{provider['name']}#{attempt + 1}: vision_unavailable")
                    logger.warning(
                        f"personification: provider {provider['name']} does not support current image input"
                    )
                    skip_provider = True
                    break
                if _is_invalid_route_response(response):
                    raise RuntimeError(
                        f"empty or invalid route response: {str(response.content or '').strip()[:120]}"
                    )
                _mark_provider_success(provider["name"])
                return response, errors, saw_vision_unavailable
            except Exception as e:
                if contains_image_input and error_indicates_vision_unavailable(e):
                    saw_vision_unavailable = True
                    errors.append(f"{provider['name']}#{attempt + 1}: vision_unavailable")
                    logger.warning(
                        f"personification: provider {provider['name']} rejected image input "
                        f"({attempt + 1}/{retries}): {_error_text(e)}"
                    )
                    skip_provider = True
                    break
                error_text = _error_text(e)
                errors.append(f"{provider['name']}#{attempt + 1}: {error_text}")
                logger.warning(
                    f"personification: provider {provider['name']} failed "
                    f"({attempt + 1}/{retries}): {error_text}"
                )
                if attempt + 1 >= retries:
                    _mark_provider_failure(provider["name"], e)
                else:
                    await asyncio.sleep(min(2, attempt + 1))
        if skip_provider:
            continue
        logger.warning(
            f"personification: switching to next provider after failure: {provider['name']}"
        )
    return None, errors, saw_vision_unavailable


async def call_ai_api(
    messages: List[Dict[str, Any]],
    *,
    plugin_config: Any,
    logger: Any,
    tools: Optional[List[Dict[str, Any]]] = None,
    use_builtin_search: bool = False,
    model_override: str = "",
) -> ToolCallerResponse:
    providers = [
        _override_provider_model(provider, model_override)
        for provider in get_provider_candidates(plugin_config, logger)
    ]
    errors: List[str] = []
    saw_vision_unavailable = False

    if providers:
        response, primary_errors, primary_saw_vision_unavailable = await _try_provider_chain(
            providers,
            messages=messages,
            plugin_config=plugin_config,
            logger=logger,
            tools=tools,
            use_builtin_search=use_builtin_search,
        )
        errors.extend(primary_errors)
        saw_vision_unavailable = saw_vision_unavailable or primary_saw_vision_unavailable
        if response is not None:
            return response
    else:
        logger.warning("personification: no configured API provider available")

    from .ai_routes import resolve_global_fallback_provider

    fallback_resolution = resolve_global_fallback_provider(plugin_config, logger, warn=True)
    if fallback_resolution is not None:
        fallback_provider = _override_provider_model(fallback_resolution.provider, model_override)
        fallback_provider["api_type"] = normalize_api_type(fallback_provider.get("api_type"))
        fallback_signature = (
            normalize_api_type(fallback_provider.get("api_type")),
            str(fallback_provider.get("api_url", "") or "").strip(),
            str(fallback_provider.get("api_key", "") or "").strip(),
            str(fallback_provider.get("model", "") or "").strip(),
            str(fallback_provider.get("auth_path", "") or "").strip(),
        )
        primary_signatures = {
            (
                normalize_api_type(provider.get("api_type")),
                str(provider.get("api_url", "") or "").strip(),
                str(provider.get("api_key", "") or "").strip(),
                str(provider.get("model", "") or "").strip(),
                str(provider.get("auth_path", "") or "").strip(),
            )
            for provider in providers
        }
        if fallback_signature not in primary_signatures:
            fallback_provider.setdefault("name", "configured_fallback")
            fallback_provider.setdefault("priority", 999)
            fallback_provider.setdefault("timeout", 120 if fallback_provider["api_type"] in {"gemini", "anthropic"} else 60)
            fallback_provider.setdefault("max_retries", 2)
            fallback_provider.setdefault("supports_native_search", fallback_provider["api_type"] in {"gemini", "anthropic", "openai", "openai_codex"})
            response, fallback_errors, fallback_saw_vision_unavailable = await _try_provider_chain(
                [fallback_provider],
                messages=messages,
                plugin_config=plugin_config,
                logger=logger,
                tools=tools,
                use_builtin_search=use_builtin_search,
            )
            errors.extend(fallback_errors)
            saw_vision_unavailable = saw_vision_unavailable or fallback_saw_vision_unavailable
            if response is not None:
                return response

    if errors:
        logger.error("personification: all providers failed: " + " | ".join(errors))
    if saw_vision_unavailable:
        return _empty_vision_unavailable_response()
    return _empty_response()
