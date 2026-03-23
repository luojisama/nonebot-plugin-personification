import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from ..skills.tool_caller import (
    AnthropicToolCaller,
    GeminiToolCaller,
    OpenAICodexToolCaller,
    OpenAIToolCaller,
    ToolCallerResponse,
)


PROVIDER_FAILURE_STATE: Dict[str, Dict[str, Any]] = {}
PROVIDER_ROTATION_CURSOR = 0


def normalize_api_type(api_type: Optional[str]) -> str:
    value = (api_type or "openai").strip().lower()
    if value == "gemini_official":
        return "gemini"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    if value not in {"openai", "gemini", "anthropic", "openai_codex"}:
        return "openai"
    return value


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


def load_api_pool_config(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    raw_config = getattr(plugin_config, "personification_api_pools", None)
    if not raw_config:
        return []

    parsed: Any = raw_config
    if isinstance(raw_config, str):
        try:
            parsed = json.loads(raw_config)
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
                item.get("supports_native_search", api_type in {"gemini", "anthropic"}),
                api_type in {"gemini", "anthropic"},
            ),
        }
        if provider["enabled"]:
            providers.append(provider)

    providers.sort(key=lambda p: (p["priority"], p["name"]))
    return providers


def get_configured_api_providers(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    providers = load_api_pool_config(plugin_config, logger)
    if providers:
        return providers

    legacy_type = normalize_api_type(getattr(plugin_config, "personification_api_type", "openai"))
    if legacy_type == "openai_codex":
        return [
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

    api_key = str(getattr(plugin_config, "personification_api_key", "") or "").strip()
    if not api_key:
        return []

    return [
        {
            "name": "legacy_primary",
            "api_type": legacy_type,
            "api_url": str(getattr(plugin_config, "personification_api_url", "") or "").strip(),
            "api_key": api_key,
            "model": getattr(plugin_config, "personification_model", ""),
            "auth_path": "",
            "enabled": True,
            "priority": 0,
            "timeout": 120 if legacy_type in {"gemini", "anthropic"} else 60,
            "max_retries": 2,
            "supports_native_search": legacy_type in {"gemini", "anthropic"},
        }
    ]


def get_provider_candidates(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    global PROVIDER_ROTATION_CURSOR

    providers = get_configured_api_providers(plugin_config, logger)
    if not providers:
        return []

    now_ts = time.time()
    available: List[Dict[str, Any]] = []
    cooling: List[Dict[str, Any]] = []
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

    cursor = PROVIDER_ROTATION_CURSOR % len(available)
    PROVIDER_ROTATION_CURSOR = (PROVIDER_ROTATION_CURSOR + 1) % len(available)
    return available[cursor:] + available[:cursor]


def _mark_provider_success(provider_name: str) -> None:
    PROVIDER_FAILURE_STATE.pop(provider_name, None)


def _mark_provider_failure(provider_name: str, error: Exception) -> None:
    now_ts = time.time()
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
    )


def _should_use_builtin_search(provider: Dict[str, Any], use_builtin_search: bool) -> bool:
    if not use_builtin_search:
        return False
    if provider["api_type"] not in {"gemini", "anthropic", "openai_codex"}:
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


def _error_text(error: Exception) -> str:
    text = str(error).strip()
    if text:
        return text
    return f"{type(error).__name__}"


async def call_ai_api(
    messages: List[Dict[str, Any]],
    *,
    plugin_config: Any,
    logger: Any,
    tools: Optional[List[Dict[str, Any]]] = None,
    use_builtin_search: bool = False,
) -> ToolCallerResponse:
    providers = get_provider_candidates(plugin_config, logger)
    if not providers:
        logger.warning("personification: no configured API provider available")
        return _empty_response()

    errors: List[str] = []
    for provider in providers:
        logger.info(
            f"personification: try provider={provider['name']} "
            f"type={provider['api_type']} model={provider['model']}"
        )
        retries = _provider_max_retries(provider)
        for attempt in range(retries):
            try:
                response = await _call_provider_once(
                    provider,
                    messages,
                    plugin_config=plugin_config,
                    tools=tools,
                    use_builtin_search=use_builtin_search,
                )
                if _is_generic_refusal_response(response):
                    raise RuntimeError(
                        f"generic refusal response: {response.content.strip()}"
                    )
                _mark_provider_success(provider["name"])
                return response
            except Exception as e:
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
        logger.warning(
            f"personification: switching to next provider after failure: {provider['name']}"
        )

    if errors:
        logger.error("personification: all providers failed: " + " | ".join(errors))
    return _empty_response()
