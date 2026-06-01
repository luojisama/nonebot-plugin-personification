from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core import config_registry, env_writer, webui_audit_log
from ..deps import AdminIdentity, require_admin
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
    "personification_qzone_check_interval": 90,
    "personification_qzone_daily_limit": 3,
    "personification_qzone_min_interval_hours": 6,
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
        return ConfigUpdateResponse(
            success=not result["errors"],
            errors=list(result["errors"]),
            dotenv_path=result.get("dotenv_path"),
            env_json_path=result.get("env_json_path"),
            new_value=normalized if not entry.secret else "***",
        )

    return router
