from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Optional

from .llm_context import use_single_attempt_retry_policy
from .message_parts import normalize_message_parts
from .safety_filter import build_safe_reframe_messages, detect_route_safety_issue
from .visual_capabilities import error_indicates_vision_unavailable, heuristic_supports_vision


PROVIDER_FAILURE_STATE: Dict[str, Dict[str, Any]] = {}
PROVIDER_ROTATION_CURSOR = 0
_PROVIDER_STATE_LOCK = threading.RLock()
_CURSOR_LOCK = _PROVIDER_STATE_LOCK
_LOGGED_PROVIDER_CONFIG_SIGNATURES: set[tuple[str, tuple[tuple[str, str, str], ...]]] = set()
_RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS = 10 * 60
_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 30 * 60
_DEFAULT_PROVIDER_TIMEOUT_SECONDS = 200.0
_DEFAULT_PROVIDER_MAX_ATTEMPTS = 5
_MAX_PROVIDER_ATTEMPTS = 10
_RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429}
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


class _InvalidProviderResponse(RuntimeError):
    pass


class ProviderRouteError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 0,
        retryable: bool = False,
        route_attempts: List[Dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "provider_route_failed")
        self.status_code = int(status_code or 0)
        self.retryable = bool(retryable)
        self.route_attempts = tuple(dict(item) for item in list(route_attempts or []))
        selected = _select_provider_attempt(list(self.route_attempts)) if self.route_attempts else {}
        self.auth_mode = str(selected.get("auth_mode") or "")[:48]
        self.request_count = sum(
            max(1, int(item.get("request_count") or 1)) for item in self.route_attempts
        )


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


def _provider_signature(provider: Dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    api_type = normalize_api_type(provider.get("api_type"))
    return (
        api_type,
        str(provider.get("api_url", "") or "").strip(),
        str(provider.get("api_key", "") or "").strip(),
        str(provider.get("model", "") or "").strip(),
        str(provider.get("auth_path", "") or "").strip(),
        (
            str(provider.get("gemini_auth_mode", "auto") or "auto").strip().lower()
            if api_type == "gemini"
            else ""
        ),
    )


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
    try:
        value = float(provider.get("timeout", _DEFAULT_PROVIDER_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        value = _DEFAULT_PROVIDER_TIMEOUT_SECONDS
    return max(5.0, min(value, 600.0))


def _provider_max_retries(provider: Dict[str, Any]) -> int:
    value = _to_int(
        provider.get("max_retries", _DEFAULT_PROVIDER_MAX_ATTEMPTS),
        _DEFAULT_PROVIDER_MAX_ATTEMPTS,
    )
    return max(1, min(value, _MAX_PROVIDER_ATTEMPTS))


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
            "gemini_auth_mode": str(item.get("gemini_auth_mode", "auto") or "auto").strip().lower(),
            "enabled": _to_bool(item.get("enabled", True), True),
            "priority": _to_int(item.get("priority", index), index),
            "timeout": _provider_timeout({**item, "api_type": api_type}),
            "max_retries": _provider_max_retries(item),
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
                "gemini_auth_mode": str(
                    getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto"
                ).strip().lower(),
                "enabled": True,
                "priority": 0,
                "timeout": _DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                "max_retries": _DEFAULT_PROVIDER_MAX_ATTEMPTS,
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
            "gemini_auth_mode": str(
                getattr(plugin_config, "personification_gemini_auth_mode", "auto") or "auto"
            ).strip().lower(),
            "enabled": True,
            "priority": 0,
            "timeout": _DEFAULT_PROVIDER_TIMEOUT_SECONDS,
            "max_retries": _DEFAULT_PROVIDER_MAX_ATTEMPTS,
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
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        values = (
            getattr(current, "status_code", None),
            getattr(getattr(current, "response", None), "status_code", None),
        )
        for value in values:
            try:
                status = int(value or 0)
            except (TypeError, ValueError):
                continue
            if status:
                return status
        current = current.__cause__ or current.__context__
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
            "last_error": _error_text(error),
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
        return tool_impl.GeminiToolCaller(
            **common_kwargs,
            timeout=_provider_timeout(provider),
            auth_mode=str(provider.get("gemini_auth_mode", "auto") or "auto"),
        )
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
        safety_issue = detect_route_safety_issue(response)
        success = not bool(getattr(response, "vision_unavailable", False)) and not safety_issue
        if safety_issue:
            error_kind = "safety_block"
        elif not success:
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


def _is_invalid_route_response(response: ToolCallerResponse) -> bool:
    if response.tool_calls:
        return False
    if response.vision_unavailable:
        return True
    if not str(response.content or "").strip():
        return True
    return False


def _provider_error_kind(error: Exception) -> str:
    if isinstance(error, _InvalidProviderResponse):
        return "invalid_response"
    try:
        from . import provider_health

        return str(provider_health.classify_error(error) or "other")
    except Exception:
        return "other"


def _is_retryable_provider_error(error: Exception) -> bool:
    status = _error_http_status(error)
    canonical_code = ""
    current: BaseException | None = error
    seen: set[int] = set()
    class_names: list[str] = []
    explicit_retryable = False
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        class_names.append(type(current).__name__.lower())
        candidate_code = str(getattr(current, "code", "") or "").strip().lower()
        if not canonical_code and candidate_code in _CANONICAL_PROVIDER_CODES:
            canonical_code = candidate_code
        explicit_retryable = explicit_retryable or getattr(current, "retryable", False) is True
        current = current.__cause__ or current.__context__
    if status in {400, 401, 403, 422}:
        return False
    if status == 404:
        return canonical_code == "provider_model_candidate_unavailable" and explicit_retryable
    if explicit_retryable:
        return True
    if status in _RETRYABLE_HTTP_STATUSES or 500 <= status < 600:
        return True
    if any("timeout" in name for name in class_names):
        return True
    if any(
        marker in name
        for name in class_names
        for marker in ("connect", "network", "protocol", "readerror", "writeerror", "dnserror")
    ):
        return True
    return _provider_error_kind(error) in {"timeout", "connect", "invalid_response"}


def _provider_failure_code(error: Exception) -> str:
    current: BaseException | None = error
    seen: set[int] = set()
    canonical_code = ""
    class_names: list[str] = []
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        class_names.append(type(current).__name__.lower())
        code = str(getattr(current, "code", "") or "").strip().lower()
        if not canonical_code and code in _CANONICAL_PROVIDER_CODES:
            canonical_code = code
        current = current.__cause__ or current.__context__
    status = _error_http_status(error)
    if status == 401:
        return "provider_auth_failed"
    if status == 403:
        return "provider_permission_denied"
    if status in {400, 422}:
        return "provider_request_rejected"
    if status == 404:
        return (
            "provider_model_candidate_unavailable"
            if canonical_code == "provider_model_candidate_unavailable"
            else "provider_model_unavailable"
        )
    if canonical_code:
        return canonical_code
    if isinstance(error, _InvalidProviderResponse):
        return "provider_invalid_response"
    if any("timeout" in name for name in class_names):
        return "provider_timeout"
    if any(
        marker in name
        for name in class_names
        for marker in ("connect", "network", "protocol", "readerror", "writeerror", "dnserror")
    ):
        return "provider_network_failed"
    return "provider_call_failed"


def _provider_route_attempt(
    provider: Dict[str, Any],
    error: Exception,
    *,
    attempt: int,
) -> Dict[str, Any]:
    auth_mode = ""
    request_count = 0
    concrete_model = ""
    next_model = ""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        if not auth_mode:
            auth_mode = str(getattr(current, "auth_mode", "") or "").strip().lower()
        if not request_count:
            try:
                request_count = int(getattr(current, "request_count", 0) or 0)
            except (TypeError, ValueError):
                request_count = 0
        if not concrete_model:
            concrete_model = str(getattr(current, "concrete_model", "") or "").strip()
        if not next_model:
            next_model = str(getattr(current, "next_model", "") or "").strip()
        current = current.__cause__ or current.__context__
    api_type = normalize_api_type(provider.get("api_type"))
    return {
        "provider": str(provider.get("name") or "provider")[:80],
        "api_type": api_type[:48],
        "model": str(provider.get("model") or "")[:120],
        "concrete_model": concrete_model[:120],
        "next_model": next_model[:120],
        "auth_mode": (
            auth_mode or str(provider.get("gemini_auth_mode", "") or "").strip().lower()
            if api_type == "gemini"
            else ""
        )[:48],
        "request_count": max(1, request_count),
        "attempt": max(1, int(attempt or 1)),
        "status_code": _error_http_status(error),
        "code": _provider_failure_code(error),
        "retryable": _is_retryable_provider_error(error),
        "exception_type": type(error).__name__[:80],
    }


def _select_provider_attempt(attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
    priorities = (
        lambda item: item.get("code") == "provider_model_candidate_unavailable",
        lambda item: item.get("code") == "provider_auth_failed" or item.get("status_code") == 401,
        lambda item: item.get("code") == "provider_permission_denied" or item.get("status_code") == 403,
        lambda item: item.get("code") == "provider_request_rejected" or item.get("status_code") in {400, 422},
        lambda item: item.get("code") == "provider_model_unavailable" or item.get("status_code") == 404,
        lambda item: bool(item.get("retryable")),
    )
    for predicate in priorities:
        matched = next((item for item in attempts if predicate(item)), None)
        if matched is not None:
            return matched
    return attempts[-1] if attempts else {
        "code": "providers_exhausted",
        "status_code": 0,
        "retryable": True,
    }


def _provider_retry_delay(error: Exception, attempt: int) -> float:
    delay = float(min(8, 2 ** max(0, int(attempt))))
    if _error_http_status(error) == 429:
        delay = max(delay, min(30.0, _retry_after_seconds(error)))
    return delay


def _error_text(error: Exception) -> str:
    status = _error_http_status(error)
    kind = _provider_error_kind(error)
    error_type = type(error).__name__
    return f"type={error_type} kind={kind} status={status or '-'}"


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
) -> tuple[ToolCallerResponse | None, list[str], list[Dict[str, Any]], bool]:
    errors: List[str] = []
    route_attempts: List[Dict[str, Any]] = []
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
        retries = 1 if use_single_attempt_retry_policy() else _provider_max_retries(provider)
        skip_provider = False
        safety_reframed = False
        for attempt in range(retries):
            try:
                response = await _call_provider_once(
                    provider,
                    provider_messages,
                    plugin_config=plugin_config,
                    tools=tools,
                    use_builtin_search=use_builtin_search,
                )
                safety_issue = detect_route_safety_issue(response)
                if safety_issue:
                    reason = safety_issue
                    errors.append(f"{provider['name']}#{attempt + 1}: safety_block:{reason}")
                    logger.warning(
                        f"personification: provider {provider['name']} returned blocked output "
                        f"({reason}); visible text discarded"
                    )
                    if not safety_reframed and not use_single_attempt_retry_policy():
                        provider_messages = build_safe_reframe_messages(provider_messages)
                        safety_reframed = True
                        try:
                            response = await _call_provider_once(
                                provider,
                                provider_messages,
                                plugin_config=plugin_config,
                                tools=tools,
                                use_builtin_search=use_builtin_search,
                            )
                        except Exception as safety_exc:
                            errors.append(f"{provider['name']}#safety: {_error_text(safety_exc)}")
                            route_attempts.append(
                                _provider_route_attempt(provider, safety_exc, attempt=attempt + 2)
                            )
                            _mark_provider_failure(provider["name"], safety_exc)
                            skip_provider = True
                            break
                        retry_issue = detect_route_safety_issue(response)
                        invalid_response = _is_invalid_route_response(response)
                        if not retry_issue and not invalid_response:
                            _mark_provider_success(provider["name"])
                            return response, errors, route_attempts, saw_vision_unavailable
                        if retry_issue:
                            errors.append(f"{provider['name']}#safety: safety_block:{retry_issue}")
                            safety_error = RuntimeError("provider safety block")
                            safety_error.code = "provider_safety_block"
                        else:
                            errors.append(f"{provider['name']}#safety: invalid_response")
                            safety_error = _InvalidProviderResponse("empty or invalid route response")
                        route_attempts.append(
                            _provider_route_attempt(provider, safety_error, attempt=attempt + 2)
                        )
                        _mark_provider_failure(provider["name"], safety_error)
                    elif use_single_attempt_retry_policy():
                        safety_error = RuntimeError("provider safety block")
                        safety_error.code = "provider_safety_block"
                        route_attempts.append(
                            _provider_route_attempt(provider, safety_error, attempt=attempt + 1)
                        )
                    skip_provider = True
                    break
                if response.vision_unavailable:
                    saw_vision_unavailable = True
                    errors.append(f"{provider['name']}#{attempt + 1}: vision_unavailable")
                    logger.warning(
                        f"personification: provider {provider['name']} does not support current image input"
                    )
                    skip_provider = True
                    break
                if _is_invalid_route_response(response):
                    raise _InvalidProviderResponse("empty or invalid route response")
                _mark_provider_success(provider["name"])
                return response, errors, route_attempts, saw_vision_unavailable
            except asyncio.CancelledError:
                raise
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
                route_attempts.append(_provider_route_attempt(provider, e, attempt=attempt + 1))
                retryable = _is_retryable_provider_error(e)
                has_next_attempt = attempt + 1 < retries
                if retryable and has_next_attempt:
                    delay = _provider_retry_delay(e, attempt)
                    logger.warning(
                        f"personification: provider {provider['name']} transient failure "
                        f"attempt={attempt + 1}/{retries} {error_text}; retry_in={delay:g}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                cooldown = _mark_provider_failure(provider["name"], e)
                outcome = "attempts_exhausted" if retryable else "non_retryable"
                logger.warning(
                    f"personification: provider {provider['name']} failed "
                    f"attempt={attempt + 1}/{retries} outcome={outcome} {error_text}; "
                    f"cooldown={int(cooldown)}s and switch"
                )
                break
        if skip_provider:
            continue
        logger.warning(
            f"personification: switching to next provider after failure: {provider['name']}"
        )
    return None, errors, route_attempts, saw_vision_unavailable


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
    route_attempts: List[Dict[str, Any]] = []
    saw_vision_unavailable = False

    if providers:
        response, primary_errors, primary_attempts, primary_saw_vision_unavailable = await _try_provider_chain(
            providers,
            messages=messages,
            plugin_config=plugin_config,
            logger=logger,
            tools=tools,
            use_builtin_search=use_builtin_search,
        )
        errors.extend(primary_errors)
        route_attempts.extend(primary_attempts)
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
        fallback_signature = _provider_signature(fallback_provider)
        primary_signatures = {_provider_signature(provider) for provider in providers}
        if fallback_signature not in primary_signatures:
            fallback_provider.setdefault("name", "configured_fallback")
            fallback_provider.setdefault("priority", 999)
            fallback_provider.setdefault(
                "timeout",
                _DEFAULT_PROVIDER_TIMEOUT_SECONDS,
            )
            fallback_provider.setdefault("max_retries", _DEFAULT_PROVIDER_MAX_ATTEMPTS)
            fallback_provider.setdefault(
                "supports_native_search",
                fallback_provider["api_type"]
                in {"gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code", "openai", "openai_codex"},
            )
            response, fallback_errors, fallback_attempts, fallback_saw_vision_unavailable = await _try_provider_chain(
                [fallback_provider],
                messages=messages,
                plugin_config=plugin_config,
                logger=logger,
                tools=tools,
                use_builtin_search=use_builtin_search,
            )
            errors.extend(fallback_errors)
            route_attempts.extend(fallback_attempts)
            saw_vision_unavailable = saw_vision_unavailable or fallback_saw_vision_unavailable
            if response is not None:
                return response

    if saw_vision_unavailable:
        return _empty_vision_unavailable_response()
    if errors:
        if providers and len(providers) == 1 and _errors_include_rate_limit(errors):
            logger.error(
                "personification: only one primary provider is configured and it is rate limited; "
                "configure a secondary provider or wait for the cooldown to expire"
            )
        logger.error("personification: all providers failed: " + " | ".join(errors))
        selected = _select_provider_attempt(route_attempts)
        raise ProviderRouteError(
            str(selected.get("code") or "providers_exhausted"),
            "all configured providers failed",
            status_code=int(selected.get("status_code") or 0),
            retryable=bool(selected.get("retryable", True)),
            route_attempts=route_attempts,
        )
    if saw_vision_unavailable:
        return _empty_vision_unavailable_response()
    raise ProviderRouteError(
        "provider_caller_unavailable",
        "no configured provider caller is available",
        retryable=False,
        route_attempts=route_attempts,
    )
