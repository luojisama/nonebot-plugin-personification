from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ...core import config_registry, env_writer, webui_audit_log
from ...core.config_search import build_config_search_index
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, step
from ...core.sensitive_data import sanitize_object
from ..deps import AdminIdentity, require_admin


def _schedule_diagnostics_warm(runtime: Any) -> None:
    """配置变更后后台刷新功能体检缓存；never-raise。"""
    import asyncio

    from ...core.diagnostics import warm_diagnostics

    async def _run() -> None:
        await warm_diagnostics(
            plugin_config=getattr(runtime, "plugin_config", None),
            bundle=getattr(runtime, "runtime_bundle", None),
            superusers=getattr(runtime, "superusers", set()),
            get_bots=getattr(runtime, "get_bots", None),
            logger=getattr(runtime, "logger", None),
        )

    try:
        asyncio.create_task(_run())
    except Exception:
        pass
from ..schemas import (
    ConfigEntriesResponse,
    ConfigEntryView,
    ConfigUpdateRequest,
)


_RECOMMENDED_DEFAULTS: dict[str, Any] = {
    "personification_tts_global_enabled": True,
    "personification_tts_mode": "clone",
    "personification_tts_model": "mimo-v2.5-tts-voiceclone",
    "personification_persona_history_max": 100,
    "personification_persona_snippet_max_chars": 200,
    "personification_persona_enabled": True,
    "personification_sticker_probability": 0.1,
    "personification_qzone_enabled": True,
    "personification_qzone_proactive_enabled": True,
    "personification_qzone_check_interval": 60,
    "personification_qzone_monthly_limit": 30,
    "personification_qzone_probability": 0.20,
    "personification_qzone_min_interval_hours": 12,
    "personification_qzone_forward_enabled": True,
    "personification_qzone_forward_limit": 1,
    "personification_qzone_forward_max_per_scan": 1,
    "personification_labeler_enabled": True,
    "personification_labeler_concurrency": 3,
    "personification_proactive_enabled": True,
    "personification_proactive_threshold": 50,
    "personification_proactive_daily_limit": 5,
    "personification_proactive_interval": 10,
    "personification_proactive_probability": 0.30,
    "personification_proactive_idle_hours": 12.0,
    # 主动水群（默认 enabled=False 完全不触发；推荐打开 + 上调频率）
    "personification_group_idle_enabled": True,
    "personification_group_idle_minutes": 40,
    "personification_group_idle_daily_limit": 3,
    "personification_group_idle_check_interval": 15,
    "personification_probability": 0.35,
    "personification_poke_probability": 0.5,
    "personification_agent_enabled": True,
    "personification_agent_max_steps": 5,
    "personification_memory_palace_enabled": True,
    "personification_memory_recall_top_k": 12,
    "personification_memory_search_scan_limit": 800,
    "personification_memory_capture_policy": "balanced",
    "personification_agent_memory_write_enabled": True,
    "personification_builtin_search": True,
    "personification_thinking_mode": "none",
    "personification_state_thinking_mode": "adaptive",
    "personification_60s_enabled": True,
    "personification_60s_api_base": "https://60s.viki.moe",
    "personification_group_knowledge_autobuild_enabled": True,
    "personification_group_knowledge_interval_hours": 4,
    "personification_group_knowledge_daily_limit": 6,
    "personification_group_knowledge_min_messages": 50,
}

_DIAGNOSTIC_FIELDS = (
    "ok",
    "code",
    "phase",
    "title",
    "message",
    "details",
    "steps",
    "warnings",
    "suggestion",
    "retryable",
    "partial",
    "outcome_unknown",
    "operation_id",
    "trace_id",
)
_MASKED_CONFIG_VALUE = "***"
_CONFIG_SECRET_REF_KEY = os.urandom(32)


def _attach_diagnostic(payload: dict[str, Any], operation_diagnostic: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["diagnostic"] = operation_diagnostic
    for field in _DIAGNOSTIC_FIELDS:
        result.setdefault(field, operation_diagnostic[field])
    return result


def _record_audit(**kwargs: Any) -> None:
    try:
        webui_audit_log.record(**kwargs)
    except Exception:
        pass


def _log_operation_exception(runtime: Any, exc: BaseException, failure: dict[str, Any], operation: str) -> None:
    logger = getattr(runtime, "logger", None)
    if logger is None:
        return
    try:
        logger.warning(
            f"[webui] config {operation} failed: {type(exc).__name__} "
            f"trace={failure.get('trace_id', '')}"
        )
    except Exception:
        pass


def _unexpected_failure(
    runtime: Any,
    exc: BaseException,
    *,
    operation: str,
    code: str,
    phase: str,
    title: str,
) -> dict[str, Any]:
    failure = exception_diagnostic(
        exc,
        phase=phase,
        title=title,
        message="配置操作发生内部异常。",
        suggestion="请根据 Trace ID 查看脱敏日志，确认当前配置状态后再重试。",
        retryable=True,
    )
    failure["code"] = code
    failure["steps"] = [
        step(
            phase,
            "Execute configuration operation",
            "error",
            "操作异常中断，未向客户端返回 exception text。",
        ).to_dict()
    ]
    _log_operation_exception(runtime, exc, failure, operation)
    return failure


async def _reload_runtime_step(runtime: Any, *, enabled: bool = True) -> tuple[Any, str]:
    if not enabled:
        return step(
            "runtime_reload",
            "Reload runtime services",
            "skipped",
            "当前进程配置未同步，因此未重载 runtime services。",
        ), ""
    bundle = getattr(runtime, "runtime_bundle", None)
    reload_services = getattr(bundle, "reload_all_runtime_services", None) if bundle is not None else None
    if not callable(reload_services) and bundle is not None:
        reload_services = getattr(bundle, "reload_runtime_services", None)
    if not callable(reload_services):
        return step(
            "runtime_reload",
            "Reload runtime services",
            "skipped",
            "Runtime reload hook 不可用；plugin_config 已直接同步。",
        ), ""
    try:
        result = reload_services()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        return step(
            "runtime_reload",
            "Reload runtime services",
            "error",
            "Runtime services 重载失败。",
            details=(detail("异常类型", type(exc).__name__, "error"),),
        ), "运行时服务重载失败"
    return step(
        "runtime_reload",
        "Reload runtime services",
        "ok",
        "Runtime services 已使用新配置重建。",
    ), ""


def _persistence_step(result: dict[str, Any], *, count: int = 1):
    errors = list(result.get("errors") or [])
    persisted = bool(result.get("env_json_path")) and not errors
    return step(
        "persist_config",
        "Persist configuration",
        "ok" if persisted else "error",
        f"已写入 {count} 个字段到 env.json。" if persisted else "env.json 持久化未完成。",
        details=(
            detail("env.json", "已写入" if persisted else "写入失败", "ok" if persisted else "error"),
            detail(".env.prod / .env", "未改写（仅作为首次导入来源）", "info"),
        ),
    )


def _provider_timeout(provider: dict[str, Any]) -> float:
    try:
        return max(3.0, min(30.0, float(provider.get("timeout", 12) or 12)))
    except (TypeError, ValueError):
        return 12.0


def _normalize_base_url(raw: str, default: str) -> str:
    value = str(raw or "").strip().rstrip("/") or default.rstrip("/")
    for suffix in ("/chat/completions", "/messages", "/responses", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip("/")


def _append_version_path(base: str, version: str = "v1") -> str:
    base = base.rstrip("/")
    return base if base.endswith(f"/{version}") else f"{base}/{version}"


def _normalize_gemini_models_base(raw: str) -> str:
    base = _normalize_base_url(raw, "https://generativelanguage.googleapis.com")
    match = re.match(r"^(?P<root>.+/(?:v1beta|v1))(?:/.*)?$", base)
    if match:
        return match.group("root").rstrip("/")
    return f"{base.rstrip('/')}/v1beta"


def _is_google_gemini_base(raw: str) -> bool:
    from ...core.gemini_transport import is_google_gemini_endpoint

    return is_google_gemini_endpoint(raw)


def _has_openai_path(raw: str) -> bool:
    return re.search(r"(^|/)openai(/|$)", str(raw or "").strip().lower().rstrip("/")) is not None


def _normalize_openai_models_base(raw: str) -> str:
    base = _normalize_base_url(raw, "https://api.openai.com/v1")
    if _is_google_gemini_base(base) and not _has_openai_path(base):
        return f"{_normalize_gemini_models_base(base)}/openai"
    if not re.search(r"/v\d+(?:beta)?(?:/|$)", base):
        return f"{base.rstrip('/')}/v1"
    return base.rstrip("/")


def _models_endpoint(provider: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, str], str]:
    from ...core.provider_router import normalize_api_type

    api_type = normalize_api_type(provider.get("api_type"))
    api_key = str(provider.get("api_key", "") or "").strip()
    if api_type == "anthropic":
        base = _normalize_base_url(str(provider.get("api_url", "") or ""), "https://api.anthropic.com")
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return f"{_append_version_path(base)}/models", headers, {}, "anthropic"
    if api_type == "gemini":
        raw_base = str(provider.get("api_url", "") or "")
        if _has_openai_path(raw_base):
            base = _normalize_openai_models_base(raw_base)
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            return f"{base}/models", headers, {}, "gemini_openai"
        base = _normalize_gemini_models_base(raw_base)
        return f"{base}/models", {}, {}, "gemini"
    base = _normalize_openai_models_base(str(provider.get("api_url", "") or ""))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return f"{base}/models", headers, {}, "openai"


def _parse_model_list(api_type: str, payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    models: list[dict[str, str]] = []
    if api_type == "gemini":
        raw_items = payload.get("models") or []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            raw_name = str(item.get("name", "") or "").strip()
            model_id = raw_name.removeprefix("models/")
            if not model_id:
                continue
            methods = item.get("supportedGenerationMethods") or []
            if isinstance(methods, list) and methods and "generateContent" not in methods:
                continue
            models.append(
                {
                    "id": model_id,
                    "label": str(item.get("displayName", "") or model_id),
                    "source": "gemini_models",
                }
            )
        return models
    raw_items = payload.get("data") or payload.get("models") or []
    for item in raw_items if isinstance(raw_items, list) else []:
        if isinstance(item, str):
            model_id = item.strip()
            label = model_id
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
            label = str(item.get("display_name") or item.get("displayName") or model_id).strip()
        else:
            continue
        if model_id:
            models.append({"id": model_id, "label": label or model_id, "source": f"{api_type}_models"})
    return models


def _add_configured_model(models: list[dict[str, str]], provider: dict[str, Any]) -> None:
    model_id = str(provider.get("model", "") or "").strip()
    if not model_id:
        return
    if any(str(item.get("id", "") or "").lower() == model_id.lower() for item in models):
        return
    models.append({"id": model_id, "label": model_id, "source": "current_config"})


async def _probe_http_models(provider: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    url, headers, params, parser_api_type = _models_endpoint(provider)
    proxy = str(provider.get("proxy", "") or "").strip()
    timeout = httpx.Timeout(_provider_timeout(provider), connect=5.0)
    client_kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        if parser_api_type == "gemini":
            from ...core.gemini_transport import raise_for_gemini_status, request_with_gemini_auth

            async def _send(auth):  # noqa: ANN001, ANN202
                return await client.get(
                    url,
                    headers={**headers, **auth.headers},
                    params={**params, **auth.params},
                )

            auth_result = await request_with_gemini_auth(
                endpoint=_normalize_gemini_models_base(str(provider.get("api_url", "") or "")),
                api_key=str(provider.get("api_key", "") or ""),
                auth_mode=str(provider.get("gemini_auth_mode", "auto") or "auto"),
                send=_send,
            )
            response = auth_result.response
            raise_for_gemini_status(
                response,
                auth_mode=auth_result.mode,
                request_count=auth_result.request_count,
            )
        else:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("model list response must be an object")
    raw_items = payload.get("models") if parser_api_type == "gemini" else payload.get("data", payload.get("models"))
    if not isinstance(raw_items, list):
        raise ValueError("model list response does not contain an array")
    models = _parse_model_list(parser_api_type, payload)
    _add_configured_model(models, provider)
    return models, url


def _provider_probe_diagnostic(
    *,
    api_type: str,
    models: list[dict[str, str]],
    source: str,
) -> dict[str, Any]:
    return diagnostic(
        ok=True,
        code="provider_model_probe_complete",
        phase="model_list_parse",
        title="Provider 模型探测完成",
        message=f"已取得 {len(models)} 个可选模型。" if models else "Provider 返回了有效响应，但模型列表为空。",
        details=(
            detail("API type", api_type),
            detail("模型数量", len(models), "ok" if models else "warn"),
            detail("来源类型", "local_cache" if source == "local_cache" else "remote_models"),
        ),
        steps=(
            step("request_validation", "Validate provider probe request", "ok", "Provider type 与请求结构有效。"),
            step(
                "model_list_request",
                "Request model list",
                "ok",
                "已读取本地候选模型。" if source == "local_cache" else "Provider 已返回模型列表响应。",
            ),
            step(
                "model_list_parse",
                "Parse model list",
                "ok" if models else "warn",
                f"已规范化并去重 {len(models)} 个模型。",
            ),
        ),
        suggestion="可从列表选择模型，也可以继续手动填写模型 ID。",
        retryable=False,
    )


def _provider_probe_failure(exc: BaseException, *, api_type: str) -> tuple[int, dict[str, Any]]:
    status_code = 502
    status = None
    auth_mode = str(getattr(exc, "auth_mode", "") or "").strip().lower()
    if auth_mode not in {"x-goog-api-key", "bearer", "query_legacy"}:
        auth_mode = ""
    try:
        request_count = max(1, int(getattr(exc, "request_count", 1) or 1))
    except (TypeError, ValueError):
        request_count = 1
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(exc.response, "status_code", None)
        if status == 401:
            code = "provider_model_probe_auth_failed"
            phase = "provider_auth"
            title = "Provider 认证未通过"
            message = "Provider 拒绝了模型列表认证。"
            suggestion = "检查 API Key 与认证方式后再试。"
            retryable = False
        elif status == 403:
            code = "provider_model_probe_permission_denied"
            phase = "provider_auth"
            title = "Provider 模型列表权限不足"
            message = "Provider 已识别认证信息，但拒绝访问模型列表。"
            suggestion = "检查账号套餐、项目权限、模型白名单与区域限制后再试。"
            retryable = False
        else:
            code = "provider_model_probe_http_failed"
            phase = "model_list_request"
            title = "Provider 模型列表请求失败"
            message = f"Provider 返回 HTTP {status if status is not None else 'unknown'}。"
            suggestion = "检查 Provider 服务状态、模型列表 endpoint 与访问权限。"
            retryable = bool(status == 429 or (isinstance(status, int) and status >= 500))
    elif isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        code = "provider_model_probe_timeout"
        phase = "model_list_request"
        title = "Provider 模型探测超时"
        message = "等待 Provider 模型列表响应超时，未取得探测结果。"
        suggestion = "检查网络与 Provider 状态，或提高该 Provider 的 timeout 后重试。"
        retryable = True
    elif isinstance(exc, httpx.RequestError):
        code = "provider_model_probe_network_failed"
        phase = "model_list_request"
        title = "Provider 网络连接失败"
        message = "无法连接 Provider 的模型列表服务。"
        suggestion = "检查 DNS、代理、TLS 与服务器出站网络后重试。"
        retryable = True
    elif isinstance(exc, ValueError):
        code = "provider_model_probe_parse_failed"
        phase = "model_list_parse"
        title = "Provider 模型列表无法解析"
        message = "Provider 返回内容不是受支持的模型列表格式。"
        suggestion = "确认 API type 与 endpoint 协议匹配；仍可手动填写模型 ID。"
        retryable = False
    else:
        failure = exception_diagnostic(
            exc,
            phase="provider_probe",
            title="Provider 模型探测异常中断",
            message="Provider 模型探测发生内部异常。",
            suggestion="请根据 Trace ID 查看脱敏日志；不要在客户端粘贴完整 URL 或凭证。",
            retryable=True,
        )
        failure["code"] = "provider_model_probe_exception"
        failure["details"].append(detail("API type", api_type).to_dict())
        failure["steps"] = [
            step(
                "provider_probe",
                "Probe provider models",
                "error",
                "探测异常中断，raw URL、secret 与 exception text 均未返回。",
            ).to_dict()
        ]
        return 502, failure
    failure = diagnostic(
        ok=False,
        code=code,
        phase=phase,
        title=title,
        message=message,
        details=(
            detail("API type", api_type),
            *((detail("HTTP status", status, "error"),) if status is not None else ()),
            *(
                (detail("Auth mode", auth_mode, "error"),)
                if auth_mode
                else ()
            ),
            detail("Request count", request_count),
            detail("异常类型", type(exc).__name__, "error"),
        ),
        steps=(
            step("request_validation", "Validate provider probe request", "ok", "Provider type 与请求结构有效。"),
            step(
                "model_list_request",
                "Request model list",
                "error" if phase != "model_list_parse" else "ok",
                message if phase != "model_list_parse" else "Provider 已返回响应。",
            ),
            step(
                "model_list_parse",
                "Parse model list",
                "error" if phase == "model_list_parse" else "skipped",
                message if phase == "model_list_parse" else "请求阶段未完成，未执行解析。",
            ),
        ),
        suggestion=suggestion,
        retryable=retryable,
    )
    return status_code, failure


def _cached_cli_models(runtime: Any, provider: dict[str, Any]) -> list[dict[str, str]]:
    from ...core.provider_router import normalize_api_type
    from ...core.model_router import collect_available_models

    def _add_model(
        bucket: list[dict[str, str]],
        seen: set[str],
        model: Any,
        label: str = "",
        source: str = "local_cache",
    ) -> None:
        model_id = str(model or "").strip()
        if not model_id:
            return
        key = model_id.lower()
        if key in seen:
            return
        seen.add(key)
        bucket.append({"id": model_id, "label": label or model_id, "source": source})

    api_type = normalize_api_type(provider.get("api_type"))
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        items = collect_available_models(
            getattr(runtime, "plugin_config", None),
            get_configured_api_providers=lambda: [provider],
        )
        for item in items:
            model_id = str(item.get("model", "") or "").strip()
            _add_model(models, seen, model_id, model_id, str(item.get("source", "") or "local_cache"))
    except Exception:
        pass

    # CLI/OAuth providers do not expose a remote /models endpoint. Mirror the
    # same fallback chain the runtime caller uses so WebUI probing still yields
    # a real selectable list even before any local cache exists.
    configured_model = str(provider.get("model", "") or "").strip()
    try:
        from ...skills.skillpacks.tool_caller.scripts import impl as tool_caller_impl

        if api_type == "gemini_cli":
            for model in tool_caller_impl._gemini_cli_model_candidates(configured_model):
                _add_model(models, seen, model, model, "gemini_cli_candidates")
        elif api_type == "antigravity_cli":
            for model in tool_caller_impl._antigravity_cli_model_candidates(configured_model):
                _add_model(models, seen, model, model, "antigravity_cli_candidates")
    except Exception:
        pass

    if api_type == "openai_codex":
        _add_model(models, seen, configured_model or "gpt-5.3-codex", source="codex_default")
    elif api_type == "gemini_cli":
        _add_model(models, seen, configured_model or "auto-gemini-3", source="gemini_cli_default")
    elif api_type == "antigravity_cli":
        _add_model(models, seen, configured_model or "auto-gemini-3", source="antigravity_cli_default")
    elif api_type == "claude_code":
        _add_model(models, seen, configured_model or "claude-opus-4-7", source="claude_code_default")
    return models


def _entry_to_view(entry: Any, *, plugin_config: Any) -> ConfigEntryView:
    sources = env_writer.resolve_value_sources(entry.field_name, plugin_config)
    return ConfigEntryView(
        key=entry.key,
        field_name=entry.field_name,
        label=entry.display_name or entry.key,
        description=entry.description or "",
        group=entry.group or "其他",
        kind=entry.kind or "text",
        value_type=entry.value_type,
        required=bool(entry.required),
        secret=bool(entry.secret),
        advanced=bool(getattr(entry, "advanced", False)),
        example=str(getattr(entry, "example", "") or ""),
        aliases=list(getattr(entry, "help_aliases", ()) or ()),
        search_index=build_config_search_index(
            entry.key,
            entry.field_name,
            entry.display_name,
            entry.description,
            entry.group,
            getattr(entry, "help_aliases", ()) or (),
            getattr(entry, "example", "") or "",
        ),
        default=getattr(type(plugin_config), entry.field_name, entry.default),
        current=sources.get("current"),
        active_source=sources.get("active_source", "default"),
        sources={
            "env_file": sources.get("env_file"),
            "env_json": sources.get("env_json"),
            "runtime_config": sources.get("runtime_config"),
            "default": sources.get("default"),
        },
        choices=list(entry.choices or []),
        min_value=entry.min_value,
        max_value=entry.max_value,
        scope=entry.scope,
    )


def _provider_secret_ref(provider: dict[str, Any], index: int) -> str:
    payload = json.dumps(
        {"index": index, "provider": provider},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hmac.new(_CONFIG_SECRET_REF_KEY, payload, hashlib.sha256).hexdigest()


def _provider_transport_identity(provider: dict[str, Any]) -> tuple[str, ...]:
    fields = ("api_type", "api_url", "auth_path", "project", "proxy")
    return (
        *(str(provider.get(field) or "").strip() for field in fields),
        str(provider.get("gemini_auth_mode", "auto") or "auto").strip().lower(),
    )


def _contains_masked_value(value: Any) -> bool:
    if value == _MASKED_CONFIG_VALUE:
        return True
    if isinstance(value, dict):
        return any(_contains_masked_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_masked_value(item) for item in value)
    return False


def _mask_api_pool_config(value: Any) -> Any:
    sanitized = sanitize_object(value)
    if not isinstance(value, list) or not isinstance(sanitized, list):
        return sanitized
    for index, (original, masked) in enumerate(zip(value, sanitized)):
        if isinstance(original, dict) and isinstance(masked, dict):
            masked["_secret_ref"] = _provider_secret_ref(original, index)
    return sanitized


def _restore_masked_value(value: Any, existing: Any) -> Any:
    if value == _MASKED_CONFIG_VALUE:
        return existing
    if isinstance(value, dict):
        current = existing if isinstance(existing, dict) else {}
        return {
            key: _restore_masked_value(item, current.get(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        current_items = existing if isinstance(existing, list) else []
        return [
            _restore_masked_value(item, current_items[index] if index < len(current_items) else None)
            for index, item in enumerate(value)
        ]
    return value


def _restore_masked_config_secrets(field_name: str, value: Any, plugin_config: Any) -> Any:
    if field_name != "personification_api_pools" or not isinstance(value, list):
        return value
    existing = getattr(plugin_config, field_name, None)
    existing_items = existing if isinstance(existing, list) else []
    existing_by_ref = {
        _provider_secret_ref(item, index): item
        for index, item in enumerate(existing_items)
        if isinstance(item, dict)
    }
    restored: list[Any] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            restored.append(item)
            continue
        candidate = dict(item)
        secret_ref = str(candidate.pop("_secret_ref", "") or "").strip()
        if not _contains_masked_value(candidate):
            restored.append(candidate)
            continue
        current = existing_by_ref.get(secret_ref)
        if current is None:
            raise ValueError("masked provider secret reference is invalid")
        for field, old_value in zip(
            ("api_type", "api_url", "auth_path", "project", "proxy", "gemini_auth_mode"),
            _provider_transport_identity(current),
        ):
            new_value = candidate.get(field, "auto" if field == "gemini_auth_mode" else None)
            if new_value != _MASKED_CONFIG_VALUE and str(new_value or "").strip() != old_value:
                raise ValueError("provider transport changed while secret remained masked")
        restored.append(_restore_masked_value(candidate, current))
    return restored


def build_config_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/config", tags=["config"])

    @router.get("/entries", response_model=ConfigEntriesResponse)
    async def list_entries(_: AdminIdentity = Depends(require_admin)) -> ConfigEntriesResponse:
        entries = [
            _entry_to_view(entry, plugin_config=runtime.plugin_config)
            for entry in config_registry.get_config_entries("global")
        ]
        # Whole-field secrets and nested credentials must never reach the browser.
        for view in entries:
            if view.secret:
                if view.current is not None and view.current != "":
                    view.current = _MASKED_CONFIG_VALUE
                view.default = (
                    _MASKED_CONFIG_VALUE
                    if view.default is not None and view.default != ""
                    else view.default
                )
                for source_name, source_value in list(view.sources.items()):
                    if source_value is not None and source_value != "":
                        view.sources[source_name] = _MASKED_CONFIG_VALUE
                continue
            raw_current = view.current
            view.current = sanitize_object(raw_current)
            if view.field_name == "personification_api_pools":
                view.current = _mask_api_pool_config(raw_current)
            view.default = sanitize_object(view.default)
            view.sources = {
                source_name: sanitize_object(source_value)
                for source_name, source_value in view.sources.items()
            }
        groups = sorted({view.group for view in entries})
        return ConfigEntriesResponse(entries=entries, groups=groups)

    @router.get("/recommended-defaults")
    async def recommended_defaults(_: AdminIdentity = Depends(require_admin)) -> dict:
        return {"defaults": _RECOMMENDED_DEFAULTS}

    @router.post("/apply-recommended")
    async def apply_recommended(
        body: dict | None = None,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        fields = (body or {}).get("fields")
        applied: list[str] = []
        skipped: list[dict] = []
        persisted_count = 0
        entries = {
            candidate.field_name: candidate
            for candidate in config_registry.get_config_entries("global")
        }
        if isinstance(fields, list):
            requested_fields = [str(item or "").strip() for item in fields if str(item or "").strip()]
            for field_name in requested_fields:
                if field_name not in _RECOMMENDED_DEFAULTS:
                    skipped.append({"field_name": field_name, "reason": "不在推荐默认值列表"})
        else:
            requested_fields = list(_RECOMMENDED_DEFAULTS)
        for field_name, value in _RECOMMENDED_DEFAULTS.items():
            if isinstance(fields, list) and field_name not in requested_fields:
                continue
            entry = entries.get(field_name)
            if entry is None:
                skipped.append({"field_name": field_name, "reason": "未注册到 ConfigEntry"})
                continue
            try:
                normalized = entry.normalize_value(value)
            except ValueError:
                skipped.append({"field_name": field_name, "reason": "推荐值未通过字段校验"})
                continue
            try:
                result = env_writer.write_both(field_name, normalized, runtime.plugin_config)
            except Exception:
                skipped.append({"field_name": field_name, "reason": "env.json 持久化发生内部异常"})
                continue
            if result.get("errors") or not result.get("env_json_path"):
                skipped.append({"field_name": field_name, "reason": "env.json 持久化失败"})
                continue
            persisted_count += 1
            try:
                setattr(runtime.plugin_config, field_name, normalized)
            except Exception:
                skipped.append({"field_name": field_name, "reason": "当前进程配置同步失败"})
                continue
            applied.append(field_name)
        reload_step, reload_error = await _reload_runtime_step(runtime, enabled=bool(applied))
        operation_ok = not skipped and not reload_error
        if operation_ok and applied:
            code, phase, title = "config_recommended_applied", "runtime_reload", "推荐默认值已应用"
        elif operation_ok:
            code, phase, title = "config_recommended_noop", "selection", "没有需要应用的推荐默认值"
        elif applied:
            code, phase, title = "config_recommended_partial", "runtime_reload" if reload_error else "persist_config", "推荐默认值仅部分应用"
        else:
            code, phase, title = "config_recommended_failed", "persist_config", "推荐默认值未能应用"
        skipped_details = tuple(
            detail(f"跳过 {item['field_name']}", item["reason"], "warn")
            for item in skipped
        )
        operation_diagnostic = diagnostic(
            ok=operation_ok,
            code=code,
            phase=phase,
            title=title,
            message=f"已应用 {len(applied)} 项，跳过 {len(skipped)} 项。",
            details=(
                detail("请求字段数", len(requested_fields)),
                detail("成功应用", len(applied), "ok" if applied else "info"),
                detail("跳过", len(skipped), "warn" if skipped else "ok"),
                *skipped_details,
            ),
            steps=(
                step(
                    "selection",
                    "Select recommended defaults",
                    "warn" if skipped else "ok",
                    "已逐项校验推荐字段；跳过原因保留在详情中。",
                ),
                step(
                    "persist_config",
                    "Persist configuration",
                    "ok" if persisted_count and persisted_count == len(requested_fields) else "warn" if persisted_count else "skipped",
                    f"已将 {persisted_count} 个字段写入 env.json；未改写 .env.prod / .env。",
                    details=(
                        detail("env.json", f"{persisted_count} 项已写入", "ok" if persisted_count else "info"),
                        detail(".env.prod / .env", "未改写（仅作为首次导入来源）"),
                    ),
                ),
                step(
                    "runtime_config_sync",
                    "Synchronize plugin_config",
                    "ok" if applied else "skipped",
                    f"当前进程已同步 {len(applied)} 个字段。" if applied else "没有可同步的字段。",
                ),
                reload_step,
            ),
            suggestion=(
                "查看每个 skipped 字段的原因；已成功字段无需重复提交。"
                if skipped
                else "新配置已持久化并同步到当前进程。"
            ),
            retryable=bool(skipped or reload_error),
            partial=bool(applied and not operation_ok),
        )
        _record_audit(
            action="config_apply_recommended",
            qq=admin.qq,
            device_id=admin.device_id,
            detail={
                "applied": applied,
                "skipped_count": len(skipped),
                "code": operation_diagnostic["code"],
                "phase": operation_diagnostic["phase"],
            },
            outcome="ok" if operation_ok else "partial" if applied else "error",
        )
        if applied and not reload_error:
            _schedule_diagnostics_warm(runtime)
        return _attach_diagnostic({"applied": applied, "skipped": skipped}, operation_diagnostic)

    @router.post("/value")
    async def update_value(
        payload: ConfigUpdateRequest,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        field_name = payload.field_name.strip()
        if not field_name:
            raise HTTPException(
                status_code=400,
                detail=diagnostic(
                    ok=False,
                    code="config_field_required",
                    phase="request_validation",
                    title="缺少配置字段名",
                    message="field_name 不能为空。",
                    suggestion="选择一个已注册配置字段后再保存。",
                    retryable=True,
                ),
            )
        entry = None
        for candidate in config_registry.get_config_entries("global"):
            if candidate.field_name == field_name:
                entry = candidate
                break
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=diagnostic(
                    ok=False,
                    code="config_field_not_found",
                    phase="request_validation",
                    title="配置字段不存在",
                    message="请求的字段未注册到配置中心。",
                    details=(detail("字段", field_name, "error"),),
                    suggestion="刷新配置页面后从现有字段中选择。",
                    retryable=False,
                ),
            )
        try:
            normalized = entry.normalize_value(payload.value)
            normalized = _restore_masked_config_secrets(
                field_name,
                normalized,
                runtime.plugin_config,
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=diagnostic(
                    ok=False,
                    code="config_value_invalid",
                    phase="value_normalization",
                    title="配置值无效",
                    message="提交的值未通过该字段的类型或范围校验。",
                    details=(detail("字段", field_name, "error"),),
                    suggestion="按字段说明、类型和范围修改后重试。",
                    retryable=True,
                ),
            )
        try:
            result = env_writer.write_both(field_name, normalized, runtime.plugin_config)
            if not isinstance(result, dict):
                raise TypeError("config writer returned invalid result")
        except Exception as exc:
            failure = _unexpected_failure(
                runtime,
                exc,
                operation="value persistence",
                code="config_value_persist_exception",
                phase="persist_config",
                title="配置持久化异常中断",
            )
            _record_audit(
                action="config_update",
                qq=admin.qq,
                device_id=admin.device_id,
                target=field_name,
                detail={"code": failure["code"], "phase": failure["phase"], "trace_id": failure["trace_id"]},
                outcome="error",
            )
            raise HTTPException(status_code=500, detail=failure) from exc
        errors = ["env.json 持久化失败"] if result.get("errors") or not result.get("env_json_path") else []
        persistence = _persistence_step(result)
        # 同步当前进程中的 plugin_config 实例（不依赖重启）
        try:
            setattr(runtime.plugin_config, field_name, normalized)
            runtime_sync = step(
                "runtime_config_sync",
                "Synchronize plugin_config",
                "ok",
                "当前进程中的 plugin_config 已更新。",
            )
            runtime_synced = True
        except Exception as exc:
            errors.append("当前进程配置同步失败")
            runtime_sync = step(
                "runtime_config_sync",
                "Synchronize plugin_config",
                "error",
                "当前进程配置同步失败。",
                details=(detail("异常类型", type(exc).__name__, "error"),),
            )
            runtime_synced = False
        reload_step, reload_error = await _reload_runtime_step(runtime, enabled=runtime_synced)
        if reload_error:
            errors.append(reload_error)
        success = not errors
        if success:
            code, phase, title = "config_value_updated", "runtime_reload", "配置已保存并在当前进程生效"
        elif result.get("env_json_path"):
            code, phase, title = "config_value_runtime_partial", "runtime_reload", "配置已持久化，但 runtime 更新不完整"
        else:
            code, phase, title = "config_value_persist_failed", "persist_config", "配置未能持久化"
        operation_diagnostic = diagnostic(
            ok=success,
            code=code,
            phase=phase,
            title=title,
            message=(
                "配置已写入 env.json，plugin_config 与 runtime services 已处理。"
                if success
                else "配置操作仅完成了一部分，请按步骤结果确认当前状态。"
            ),
            details=(
                detail("字段", field_name),
                detail("持久化目标", "env.json"),
                detail("敏感字段", bool(entry.secret)),
            ),
            steps=(
                step("value_normalization", "Normalize configuration value", "ok", "字段值已通过校验。"),
                persistence,
                runtime_sync,
                reload_step,
            ),
            warnings=("env.json 未写入；当前进程与重启后的值可能不一致。",) if not result.get("env_json_path") and runtime_synced else (),
            suggestion="无需重启。" if success else "根据失败步骤修复后刷新配置页面，确认 active source 与当前值。",
            retryable=not success,
            partial=bool(not success and (result.get("env_json_path") or runtime_synced)),
        )
        _record_audit(
            action="config_update",
            qq=admin.qq,
            device_id=admin.device_id,
            target=field_name,
            detail={
                "errors": errors,
                "secret": bool(entry.secret),
                "code": operation_diagnostic["code"],
                "phase": operation_diagnostic["phase"],
            },
            outcome="ok" if success else "partial" if operation_diagnostic["partial"] else "error",
        )
        # 配置变更后后台重跑一次功能体检，刷新缓存（不阻塞本次响应）
        if success:
            _schedule_diagnostics_warm(runtime)
        return _attach_diagnostic(
            {
                "success": success,
                "errors": errors,
                "dotenv_path": result.get("dotenv_path"),
                "env_json_path": result.get("env_json_path"),
                "new_value": (
                    _MASKED_CONFIG_VALUE
                    if entry.secret
                    else _mask_api_pool_config(normalized)
                    if field_name == "personification_api_pools"
                    else sanitize_object(normalized)
                ),
            },
            operation_diagnostic,
        )

    @router.post("/provider-models")
    async def provider_models(
        body: dict | None = None,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.provider_router import normalize_api_type

        provider = (body or {}).get("provider", body or {})
        if not isinstance(provider, dict):
            raise HTTPException(
                status_code=400,
                detail=diagnostic(
                    ok=False,
                    code="provider_model_probe_invalid_request",
                    phase="request_validation",
                    title="Provider 探测参数无效",
                    message="provider 必须是对象。",
                    suggestion="提交一个 Provider 配置对象后重试。",
                    retryable=True,
                ),
            )
        try:
            provider = _restore_masked_config_secrets(
                "personification_api_pools",
                [provider],
                runtime.plugin_config,
            )[0]
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail=diagnostic(
                    ok=False,
                    code="provider_model_probe_secret_refresh_required",
                    phase="request_validation",
                    title="Provider 凭据需要重新输入",
                    message="Provider transport 已变化或 secret reference 已失效，未发起模型探测。",
                    suggestion="重新输入该 Provider 的 API Key 后再探测模型。",
                    retryable=True,
                ),
            )
        api_type = normalize_api_type(provider.get("api_type"))
        cli_types = {"openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}
        if api_type in cli_types:
            cached = _cached_cli_models(runtime, provider)
            result = {
                "api_type": api_type,
                "source": "local_cache",
                "manual_allowed": True,
                "models": cached,
            }
            return _attach_diagnostic(
                result,
                _provider_probe_diagnostic(api_type=api_type, models=cached, source="local_cache"),
            )
        if api_type not in {"openai", "anthropic", "gemini"}:
            raise HTTPException(
                status_code=400,
                detail=diagnostic(
                    ok=False,
                    code="provider_model_probe_unsupported",
                    phase="request_validation",
                    title="Provider 类型不支持模型探测",
                    message="该 API type 暂不支持自动获取模型列表。",
                    details=(detail("API type", api_type, "error"),),
                    suggestion="手动填写模型 ID，或改用支持模型列表探测的 Provider 类型。",
                    retryable=False,
                ),
            )
        try:
            models, _source_url = await asyncio.wait_for(_probe_http_models(provider), timeout=_provider_timeout(provider) + 2.0)
        except Exception as exc:
            response_status, failure = _provider_probe_failure(exc, api_type=api_type)
            if failure.get("code") == "provider_model_probe_exception":
                _log_operation_exception(runtime, exc, failure, "provider model probe")
            raise HTTPException(status_code=response_status, detail=failure) from exc
        sorted_models = sorted(models, key=lambda item: item.get("id", ""))
        result = {
            "api_type": api_type,
            "source": "remote_models",
            "manual_allowed": True,
            "models": sorted_models,
        }
        return _attach_diagnostic(
            result,
            _provider_probe_diagnostic(api_type=api_type, models=sorted_models, source="remote_models"),
        )

    return router
