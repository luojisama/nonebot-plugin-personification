from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Optional

from .message_parts import normalize_message_parts
from .visual_capabilities import error_indicates_vision_unavailable, heuristic_supports_vision


PROVIDER_FAILURE_STATE: Dict[str, Dict[str, Any]] = {}
PROVIDER_ROTATION_CURSOR = 0
_PROVIDER_STATE_LOCK = threading.RLock()
_CURSOR_LOCK = _PROVIDER_STATE_LOCK
_LOGGED_PROVIDER_CONFIG_SIGNATURES: set[tuple[str, tuple[tuple[str, str, str], ...]]] = set()
_RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS = 10 * 60
_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 30 * 60


def _tool_caller_impl() -> Any:
    from ..skills.skillpacks.tool_caller.scripts import impl

    return impl


def normalize_api_type(api_type: Optional[str]) -> str:
    value = (api_type or "openai").strip().lower().replace("-", "_")
    if value in {"gemini", "gemini_official"}:
        return "gemini"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    if value in {"gemini_cli", "geminicli"}:
        return "gemini_cli"
    if value in {"antigravity_cli", "antigravity", "agy", "agy_cli"}:
        return "antigravity_cli"
    if value in {"claude_code", "claudecode", "claude_cli"}:
        return "claude_code"
    if value not in {"openai", "gemini", "anthropic", "openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}:
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
    default_timeout = 120 if provider["api_type"] in {"gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code"} else 60
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


def _default_model_for_api_type(api_type: str, model: str = "") -> str:
    value = str(model or "").strip()
    if value:
        return _normalize_codex_model(value) if api_type == "openai_codex" else value
    if api_type == "openai_codex":
        return "gpt-5.3-codex"
    if api_type == "gemini_cli":
        return "auto-gemini-3"
    if api_type == "antigravity_cli":
        return "auto-gemini-3"
    if api_type == "claude_code":
        return "claude-opus-4-7"
    return ""


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


def parse_api_pool_config(raw_config: Any, logger: Any = None) -> List[Dict[str, Any]]:
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
            if logger is not None:
                logger.error(f"personification: failed to parse personification_api_pools: {e}")
            return []

    if not isinstance(parsed, list):
        if logger is not None:
            logger.error("personification: personification_api_pools must be a JSON array")
        return []

    providers: List[Dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        api_type = normalize_api_type(item.get("api_type"))
        api_key = str(item.get("api_key", "")).strip()
        api_url = str(item.get("api_url", "")).strip()
        model = _default_model_for_api_type(api_type, str(item.get("model", "")).strip())
        auth_path = str(
            item.get(
                "auth_path",
                item.get(
                    "antigravity_cli_auth_path",
                    item.get("gemini_cli_auth_path", item.get("codex_auth_path", "")),
                ),
            )
            or ""
        ).strip()
        project = str(
            item.get(
                "project",
                item.get("antigravity_cli_project", item.get("gemini_cli_project", "")),
            )
            or ""
        ).strip()

        if api_type in {"openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}:
            if not model:
                continue
        elif not api_key or not api_url or not model:
            continue

        provider = {
            "name": str(item.get("name") or f"pool_{index + 1}").strip() or f"pool_{index + 1}",
            "api_type": api_type,
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
            "auth_path": auth_path,
            "project": project,
            "proxy": str(item.get("proxy", "") or "").strip(),
            "enabled": _to_bool(item.get("enabled", True), True),
            "priority": _to_int(item.get("priority", index), index),
            "timeout": _provider_timeout({**item, "api_type": api_type}),
            "max_retries": max(1, _to_int(item.get("max_retries", 2), 2)),
            "supports_native_search": _to_bool(
                item.get(
                    "supports_native_search",
                    api_type in {"gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code", "openai", "openai_codex"},
                ),
                api_type in {"gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code", "openai", "openai_codex"},
            ),
            "supports_reasoning": item.get("supports_reasoning"),
        }
        if provider["enabled"]:
            providers.append(provider)

    providers.sort(key=lambda p: (p["priority"], p["name"]))
    return providers


def _read_env_api_pool_raw() -> str:
    try:
        from .runtime_config import read_env_file_value
    except Exception:
        return ""
    return read_env_file_value("personification_api_pools") or ""


def _load_env_api_pool_config(logger: Any) -> List[Dict[str, Any]]:
    raw_env = _read_env_api_pool_raw()
    if not raw_env:
        return []
    return parse_api_pool_config(raw_env, logger)


def _has_usable_legacy_primary_config(plugin_config: Any) -> bool:
    legacy_type = normalize_api_type(getattr(plugin_config, "personification_api_type", "openai"))
    model = _default_model_for_api_type(
        legacy_type,
        str(getattr(plugin_config, "personification_model", "") or "").strip(),
    )
    if legacy_type in {"openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}:
        return bool(model)
    api_url = str(getattr(plugin_config, "personification_api_url", "") or "").strip()
    api_key = str(getattr(plugin_config, "personification_api_key", "") or "").strip()
    return bool(api_url and api_key and model)


def load_api_pool_config(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    raw_config = getattr(plugin_config, "personification_api_pools", None)
    providers = parse_api_pool_config(raw_config, logger)

    env_raw = _read_env_api_pool_raw()
    env_providers = _load_env_api_pool_config(logger)

    # 运行时为空（未配置或被异常清空）：直接用 .env 兜底救援。
    if not providers:
        if env_providers:
            if _has_usable_legacy_primary_config(plugin_config):
                logger.info(
                    "personification: runtime api_pools is empty but legacy primary provider "
                    "is explicitly configured; ignoring .env api_pools fallback."
                )
                return providers
            logger.warning(
                "personification: 运行时 api_pools 为空，回退到 .env 文件中的配置 "
                f"({len(env_providers)} 个 provider)。"
            )
            return env_providers
        return providers

    # 运行时与 .env 都非空但数量不一致时，需要区分两种情况：
    # 1) .env 是手写的多行 pretty JSON —— NoneBot/pydantic 的 dotenv 解析器只读
    #    首行，会把内存值截断成更少的 provider；此时应以 .env 文件的完整值为准。
    # 2) .env 是工具（WebUI/命令）写入的单行紧凑 JSON —— pydantic 能完整解析，
    #    内存值才是用户的真实意图。用户主动删减 provider 属于此类，必须信任内存，
    #    否则会出现"保存后被 .env 旧配置顶回"的回退问题。
    if env_providers and len(env_providers) > len(providers):
        if "\n" in env_raw.strip():
            logger.warning(
                "personification: 运行时 api_pools 疑似被 dotenv 多行解析截断 "
                f"(runtime={len(providers)} < env={len(env_providers)})，改用 .env 文件的完整配置。"
            )
            return env_providers
        logger.info(
            "personification: api_pools 运行时值少于 .env 文件 "
            f"(runtime={len(providers)}, env={len(env_providers)})；.env 为单行可解析，"
            "以运行时值为准（视为用户主动删减）。"
        )
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
    if legacy_type in {"openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}:
        if legacy_type == "openai_codex":
            auth_path = str(getattr(plugin_config, "personification_codex_auth_path", "") or "").strip()
        elif legacy_type == "gemini_cli":
            auth_path = str(getattr(plugin_config, "personification_gemini_cli_auth_path", "") or "").strip()
        elif legacy_type == "antigravity_cli":
            auth_path = str(getattr(plugin_config, "personification_antigravity_cli_auth_path", "") or "").strip()
            if not auth_path:
                auth_path = str(getattr(plugin_config, "personification_gemini_cli_auth_path", "") or "").strip()
        else:
            auth_path = str(getattr(plugin_config, "personification_claude_code_auth_path", "") or "").strip()
        project = ""
        if legacy_type == "gemini_cli":
            project = str(getattr(plugin_config, "personification_gemini_cli_project", "") or "").strip()
        elif legacy_type == "antigravity_cli":
            project = str(getattr(plugin_config, "personification_antigravity_cli_project", "") or "").strip()
            if not project:
                project = str(getattr(plugin_config, "personification_gemini_cli_project", "") or "").strip()
        providers = [
            {
                "name": "legacy_primary",
                "api_type": legacy_type,
                "api_url": "",
                "api_key": "",
                "model": _default_model_for_api_type(
                    legacy_type,
                    str(getattr(plugin_config, "personification_model", "") or "").strip(),
                ),
                "auth_path": auth_path,
                "project": project,
                "enabled": True,
                "priority": 0,
                "timeout": 120 if legacy_type in {"gemini_cli", "antigravity_cli", "claude_code"} else 60,
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
            "project": "",
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

    # cooldown 期间不再直接过滤，而是降级到最后（所有其它 provider 全挂时还能兜底）
    # 通过给 effective_priority 加大 offset 实现
    cooling_set = {p["name"] for p in cooling}
    merged = available + cooling

    # 计算 effective_priority：base + latency_penalty + failure_penalty + (cooling ? +100)
    dynamic_enabled = bool(
        getattr(plugin_config, "personification_provider_dynamic_priority_enabled", True)
    )
    min_samples = max(
        1,
        int(getattr(plugin_config, "personification_provider_health_min_samples", 3) or 3),
    )
    all_stats: Dict[str, Dict[str, Any]] = {}
    if dynamic_enabled:
        try:
            from . import provider_health

            all_stats = provider_health.get_all_stats()
        except Exception:
            all_stats = {}

    def _eff(provider: Dict[str, Any]) -> float:
        try:
            from . import provider_health

            base_eff = provider_health.compute_effective_priority(
                provider,
                all_stats.get(provider["name"]),
                min_samples=min_samples,
                enabled=dynamic_enabled,
            )
        except Exception:
            base_eff = float(provider.get("priority", 0) or 0)
        if provider["name"] in cooling_set:
            base_eff += 100.0  # 降级到最末位但不剔除
        return round(base_eff, 1)

    decorated = [(provider, _eff(provider)) for provider in merged]
    decorated.sort(key=lambda item: (item[1], item[0]["name"]))

    if len(decorated) <= 1:
        return [item[0] for item in decorated]

    # 同 effective_priority 的若干 provider 走轮询
    top_eff = decorated[0][1]
    top_tier = [item[0] for item in decorated if item[1] == top_eff]
    lower_tiers = [item[0] for item in decorated if item[1] != top_eff]
    if len(top_tier) <= 1:
        return top_tier + lower_tiers
    with _PROVIDER_STATE_LOCK:
        cursor = PROVIDER_ROTATION_CURSOR % len(top_tier)
        PROVIDER_ROTATION_CURSOR = (PROVIDER_ROTATION_CURSOR + 1) % len(top_tier)
    return top_tier[cursor:] + top_tier[:cursor] + lower_tiers


def get_provider_failure_snapshot(now_ts: float | None = None) -> Dict[str, Dict[str, Any]]:
    now_value = time.time() if now_ts is None else float(now_ts)
    with _PROVIDER_STATE_LOCK:
        snapshot: Dict[str, Dict[str, Any]] = {}
        for name, state in PROVIDER_FAILURE_STATE.items():
            item = dict(state)
            cooldown_until = float(item.get("cooldown_until", 0) or 0)
            item["cooldown_remaining"] = max(0.0, cooldown_until - now_value)
            snapshot[str(name)] = item
        return snapshot


def _mark_provider_success(provider_name: str) -> None:
    with _PROVIDER_STATE_LOCK:
        PROVIDER_FAILURE_STATE.pop(provider_name, None)


def _error_http_status(error: Exception) -> int:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    try:
        return int(status or 0)
    except (TypeError, ValueError):
        return 0


def _retry_after_seconds(error: Exception) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return 0.0
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After") or ""
    except Exception:
        raw = ""
    try:
        return max(0.0, float(str(raw or "").strip()))
    except (TypeError, ValueError):
        return 0.0


def _is_rate_limit_error(error: Exception) -> bool:
    if _error_http_status(error) == 429:
        return True
    text = str(error or "").lower()
    return "429" in text and ("too many requests" in text or "rate" in text)


def _mark_provider_failure(provider_name: str, error: Exception) -> float:
    """记一次失败：失败计数 + 指数退避（带 ±20% jitter）。

    非-429：60s, 120s, 240s, 480s, 960s（封顶 1800s = 30 分钟）
    429：基础 600s（或 retry-after，取较大者），封顶 1800s
    全部加 ±20% jitter 避免多 provider 同时复活时挤上游。
    """
    now_ts = time.time()
    is_rate_limit = _is_rate_limit_error(error)
    retry_after = _retry_after_seconds(error) if is_rate_limit else 0.0
    with _PROVIDER_STATE_LOCK:
        state = PROVIDER_FAILURE_STATE.get(provider_name, {})
        failures = int(state.get("failures", 0)) + 1
        try:
            from . import provider_health

            cooldown = provider_health.compute_cooldown_seconds(
                failures, is_rate_limit=is_rate_limit, retry_after=retry_after
            )
        except Exception:
            # 守底逻辑（与旧实现一致），防 provider_health 模块加载失败
            if is_rate_limit:
                cooldown = max(_RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS, retry_after)
                cooldown = min(_RATE_LIMIT_MAX_COOLDOWN_SECONDS, cooldown)
            else:
                cooldown = min(300, 30 * failures)
        PROVIDER_FAILURE_STATE[provider_name] = {
            "failures": failures,
            "cooldown_until": now_ts + cooldown,
            "last_error": str(error),
            "last_failed_at": now_ts,
            "last_status": _error_http_status(error) or None,
            "rate_limited": is_rate_limit,
        }
    return cooldown


def _get_thinking_mode(plugin_config: Any) -> str:
    value = str(getattr(plugin_config, "personification_thinking_mode", "none") or "none")
    return value.strip().lower()


def _build_provider_caller(provider: Dict[str, Any], plugin_config: Any):
    tool_impl = _tool_caller_impl()
    if provider["api_type"] == "openai_codex":
        return tool_impl.OpenAICodexToolCaller(
            model=provider["model"],
            auth_path=str(provider.get("auth_path", "") or "").strip(),
            timeout=_provider_timeout(provider),
            proxy=str(provider.get("proxy", "") or "").strip(),
        )
    if provider["api_type"] == "gemini_cli":
        return tool_impl.GeminiCliToolCaller(
            model=provider["model"] or "auto-gemini-3",
            auth_path=str(provider.get("auth_path", "") or "").strip(),
            project=str(provider.get("project", "") or "").strip(),
            thinking_mode=_get_thinking_mode(plugin_config),
            timeout=_provider_timeout(provider),
            proxy=str(provider.get("proxy", "") or "").strip(),
        )
    if provider["api_type"] == "antigravity_cli":
        # 先看 provider 自带 proxy 字段；为空再 fallback 到全局
        # personification_antigravity_cli_proxy 配置（便于 .env 集中配代理）。
        explicit_proxy = str(provider.get("proxy", "") or "").strip()
        if not explicit_proxy:
            explicit_proxy = str(
                getattr(plugin_config, "personification_antigravity_cli_proxy", "") or ""
            ).strip()
        return tool_impl.AntigravityCliToolCaller(
            model=provider["model"] or "auto-gemini-3",
            auth_path=str(provider.get("auth_path", "") or "").strip(),
            project=str(provider.get("project", "") or "").strip(),
            thinking_mode=_get_thinking_mode(plugin_config),
            timeout=_provider_timeout(provider),
            proxy=explicit_proxy,
        )
    if provider["api_type"] == "claude_code":
        return tool_impl.ClaudeCodeToolCaller(
            model=provider["model"] or "claude-opus-4-7",
            auth_path=str(provider.get("auth_path", "") or "").strip(),
            thinking_mode=_get_thinking_mode(plugin_config),
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
        return tool_impl.GeminiToolCaller(**common_kwargs)
    if provider["api_type"] == "anthropic":
        return tool_impl.AnthropicToolCaller(
            **common_kwargs,
            timeout=_provider_timeout(provider),
        )
    return tool_impl.OpenAIToolCaller(
        **common_kwargs,
        timeout=_provider_timeout(provider),
        supports_reasoning=supports_reasoning,
        proxy=str(provider.get("proxy", "") or "").strip(),
    )


def _should_use_builtin_search(provider: Dict[str, Any], use_builtin_search: bool) -> bool:
    if not use_builtin_search:
        return False
    if provider["api_type"] not in {
        "gemini",
        "gemini_cli",
        "antigravity_cli",
        "anthropic",
        "claude_code",
        "openai",
        "openai_codex",
    }:
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
    start_ts = time.monotonic()
    success = False
    error_kind = ""
    try:
        response = await caller.chat_with_tools(
            messages=messages,
            tools=list(tools or []),
            use_builtin_search=_should_use_builtin_search(provider, use_builtin_search),
        )
        # vision_unavailable 算业务失败（影响 success_rate），让后续真正能识图的
        # provider 自然排前面；error_kind 标 vision_unavailable 便于诊断
        success = not bool(getattr(response, "vision_unavailable", False))
        if not success:
            error_kind = "vision_unavailable"
    except Exception as exc:
        try:
            from . import provider_health

            error_kind = provider_health.classify_error(exc)
        except Exception:
            error_kind = "other"
        raise
    finally:
        try:
            from . import provider_health

            latency_ms = (time.monotonic() - start_ts) * 1000.0
            provider_health.record_request_result(
                provider_name=str(provider.get("name", "") or ""),
                latency_ms=latency_ms,
                success=success,
                error_kind=error_kind,
            )
        except Exception:
            pass
    # 中央 token 拦截：所有走 call_ai_api → _call_provider_once 的调用统一在这里
    # 记账，覆盖 user_persona / group_style / group_knowledge / proactive / qzone /
    # inner_state / review / intent / planner / vision 等所有非-runner 路径。
    # runner.py 走 runtime.agent_tool_caller，独立拦截。
    try:
        usage = getattr(response, "usage", None) or {}
        if isinstance(usage, dict) and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            from . import llm_context as _llm_ctx
            from . import token_ledger as _ledger

            ctx = _llm_ctx.current_llm_context()
            _ledger.record_llm_call(
                model=str(getattr(response, "model_used", "") or provider.get("model", "") or ""),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                group_id=str(ctx.get("group_id", "") or ""),
                user_id=str(ctx.get("user_id", "") or ""),
                purpose=str(ctx.get("purpose", "") or "ai_route"),
            )
    except Exception:
        pass
    return response


def _empty_response() -> ToolCallerResponse:
    return _tool_caller_impl().ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw=None,
    )


def _empty_vision_unavailable_response() -> ToolCallerResponse:
    return _tool_caller_impl().ToolCallerResponse(
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


def _strip_image_parts_for_text_only_provider(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把含 image_url / image_file 的多模态 content 降级成纯文本。

    用于将消息转发给不支持视觉的 provider（如第三方网关下的 DeepSeek / Qwen / Kimi）。
    保留 text 类 part，把图像 part 替换成 [图片] 占位符，避免上游 400
    'unknown variant image_url, expected text'。视觉摘要文字通常已经由
    pipeline_context 以 system message 形式注入，所以信息几乎不损失。
    """
    converted: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            converted.append(message)
            continue
        content = message.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            image_count = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = str(part.get("type", "") or "")
                if ptype == "text":
                    text = str(part.get("text", "") or "")
                    if text:
                        text_parts.append(text)
                elif ptype in {"image_url", "image_file"}:
                    image_count += 1
            new_text = " ".join(text_parts).strip()
            if image_count:
                placeholder = f"[图片×{image_count}]" if image_count > 1 else "[图片]"
                new_text = (new_text + " " + placeholder).strip() if new_text else placeholder
            new_message = dict(message)
            new_message["content"] = new_text
            converted.append(new_message)
        else:
            converted.append(message)
    return converted


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


def _errors_include_rate_limit(errors: List[str]) -> bool:
    for error in errors:
        lowered = str(error or "").lower()
        if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
            return True
    return False


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
    text_only_messages_cache: List[Dict[str, Any]] | None = None
    for provider in providers:
        logger.info(
            f"personification: try provider={provider['name']} "
            f"type={provider['api_type']} model={provider['model']}"
        )
        # 对不支持视觉的 provider 自动 strip image_url，避免 400
        # （视觉摘要文字已在 system message 中，信息不丢失）
        provider_messages = messages
        if contains_image_input and not heuristic_supports_vision(
            provider.get("api_type"), provider.get("model")
        ):
            if text_only_messages_cache is None:
                text_only_messages_cache = _strip_image_parts_for_text_only_provider(messages)
            provider_messages = text_only_messages_cache
            logger.info(
                f"personification: provider {provider['name']} not vision-capable; "
                "stripped image parts to text placeholders"
            )
        retries = _provider_max_retries(provider)
        skip_provider = False
        for attempt in range(retries):
            try:
                response = await _call_provider_once(
                    provider,
                    provider_messages,
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
                if _is_rate_limit_error(e):
                    cooldown = _mark_provider_failure(provider["name"], e)
                    logger.warning(
                        f"personification: provider {provider['name']} rate limited "
                        f"({attempt + 1}/{retries}); cooldown {int(cooldown)}s and switch: {error_text}"
                    )
                    break
                logger.warning(
                    f"personification: provider {provider['name']} failed "
                    f"({attempt + 1}/{retries}): {error_text}"
                )
                try:
                    from . import provider_health

                    error_kind = provider_health.classify_error(e)
                except Exception:
                    error_kind = ""
                if error_kind in {"timeout", "connect"}:
                    cooldown = _mark_provider_failure(provider["name"], e)
                    logger.warning(
                        f"personification: provider {provider['name']} {error_kind}，"
                        f"跳过剩余重试并 cooldown {int(cooldown)}s"
                    )
                    break
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
            fallback_provider.setdefault(
                "timeout",
                120 if fallback_provider["api_type"] in {"gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code"} else 60,
            )
            fallback_provider.setdefault("max_retries", 2)
            fallback_provider.setdefault(
                "supports_native_search",
                fallback_provider["api_type"]
                in {"gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code", "openai", "openai_codex"},
            )
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
        if providers and len(providers) == 1 and _errors_include_rate_limit(errors):
            logger.error(
                "personification: only one primary provider is configured and it is rate limited; "
                "configure a secondary provider or wait for the cooldown to expire"
            )
        logger.error("personification: all providers failed: " + " | ".join(errors))
    if saw_vision_unavailable:
        return _empty_vision_unavailable_response()
    return _empty_response()
