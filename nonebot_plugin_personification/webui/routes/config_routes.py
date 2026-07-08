from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ...core import config_registry, env_writer, webui_audit_log
from ...core.config_search import build_config_search_index
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
    ConfigUpdateResponse,
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
    return "generativelanguage.googleapis.com" in str(raw or "").lower()


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
        if not _is_google_gemini_base(base):
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            return f"{base}/models", headers, {}, "gemini"
        params = {"key": api_key} if api_key else {}
        return f"{base}/models", {}, params, "gemini"
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
    client_kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
    models = _parse_model_list(parser_api_type, payload)
    _add_configured_model(models, provider)
    return models, url + (("?" + urlencode({"key": "***"})) if params.get("key") else "")


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


def build_config_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/config", tags=["config"])

    @router.get("/entries", response_model=ConfigEntriesResponse)
    async def list_entries(_: AdminIdentity = Depends(require_admin)) -> ConfigEntriesResponse:
        entries = [
            _entry_to_view(entry, plugin_config=runtime.plugin_config)
            for entry in config_registry.get_config_entries("global")
        ]
        # 对 secret 字段返回时遮码，前端不应看到明文
        for view in entries:
            if view.secret:
                if isinstance(view.current, str) and view.current:
                    view.current = "***"
                if isinstance(view.sources.get("env_file"), str) and view.sources["env_file"]:
                    view.sources["env_file"] = "***"
                if isinstance(view.sources.get("env_json"), str) and view.sources["env_json"]:
                    view.sources["env_json"] = "***"
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
        for field_name, value in _RECOMMENDED_DEFAULTS.items():
            if isinstance(fields, list) and field_name not in fields:
                continue
            entry = None
            for candidate in config_registry.get_config_entries("global"):
                if candidate.field_name == field_name:
                    entry = candidate
                    break
            if entry is None:
                skipped.append({"field_name": field_name, "reason": "未注册到 ConfigEntry"})
                continue
            try:
                normalized = entry.normalize_value(value)
            except ValueError as exc:
                skipped.append({"field_name": field_name, "reason": str(exc)})
                continue
            result = env_writer.write_both(field_name, normalized, runtime.plugin_config)
            if result["errors"]:
                skipped.append({"field_name": field_name, "reason": "；".join(result["errors"])})
                continue
            try:
                setattr(runtime.plugin_config, field_name, normalized)
            except Exception:
                pass
            applied.append(field_name)
        webui_audit_log.record(
            action="config_apply_recommended",
            qq=admin.qq,
            device_id=admin.device_id,
            detail={"applied": applied, "skipped_count": len(skipped)},
        )
        return {"applied": applied, "skipped": skipped}

    @router.post("/value", response_model=ConfigUpdateResponse)
    async def update_value(
        payload: ConfigUpdateRequest,
        admin: AdminIdentity = Depends(require_admin),
    ) -> ConfigUpdateResponse:
        field_name = payload.field_name.strip()
        if not field_name:
            raise HTTPException(status_code=400, detail="field_name 不能为空")
        entry = None
        for candidate in config_registry.get_config_entries("global"):
            if candidate.field_name == field_name:
                entry = candidate
                break
        if entry is None:
            raise HTTPException(status_code=404, detail=f"未知字段 {field_name}")
        try:
            normalized = entry.normalize_value(payload.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"取值非法：{exc}") from exc
        result = env_writer.write_both(field_name, normalized, runtime.plugin_config)
        # 同步当前进程中的 plugin_config 实例（不依赖重启）
        try:
            setattr(runtime.plugin_config, field_name, normalized)
        except Exception as exc:
            result["errors"].append(f"运行时同步失败：{exc}")
        webui_audit_log.record(
            action="config_update",
            qq=admin.qq,
            device_id=admin.device_id,
            target=field_name,
            detail={"errors": result.get("errors", []), "secret": bool(entry.secret)},
            outcome="ok" if not result["errors"] else "partial",
        )
        # 配置变更后后台重跑一次功能体检，刷新缓存（不阻塞本次响应）
        if not result["errors"]:
            _schedule_diagnostics_warm(runtime)
        return ConfigUpdateResponse(
            success=not result["errors"],
            errors=list(result["errors"]),
            dotenv_path=result.get("dotenv_path"),
            env_json_path=result.get("env_json_path"),
            new_value=normalized if not entry.secret else "***",
        )

    @router.post("/provider-models")
    async def provider_models(
        body: dict | None = None,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.provider_router import normalize_api_type

        provider = (body or {}).get("provider", body or {})
        if not isinstance(provider, dict):
            raise HTTPException(status_code=400, detail="provider 必须是对象")
        api_type = normalize_api_type(provider.get("api_type"))
        cli_types = {"openai_codex", "gemini_cli", "antigravity_cli", "claude_code"}
        if api_type in cli_types:
            cached = _cached_cli_models(runtime, provider)
            return {
                "api_type": api_type,
                "source": "local_cache",
                "manual_allowed": True,
                "models": cached,
            }
        if api_type not in {"openai", "anthropic", "gemini"}:
            raise HTTPException(status_code=400, detail=f"暂不支持探测 {api_type} 的模型列表")
        try:
            models, source_url = await asyncio.wait_for(_probe_http_models(provider), timeout=_provider_timeout(provider) + 2.0)
        except httpx.HTTPStatusError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            raise HTTPException(status_code=502, detail=f"模型列表探测失败：HTTP {status}") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"模型列表探测失败：{str(exc)[:160]}") from exc
        return {
            "api_type": api_type,
            "source": source_url,
            "manual_allowed": True,
            "models": sorted(models, key=lambda item: item.get("id", "")),
        }

    return router
