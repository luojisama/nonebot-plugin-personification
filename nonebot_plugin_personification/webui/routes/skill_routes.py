from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...core import skill_overrides, webui_audit_log
from ..deps import AdminIdentity, require_admin


def _tool_registry(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "tool_registry", None)


def build_skill_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    async def list_skills(_: AdminIdentity = Depends(require_admin)) -> dict:
        registry = _tool_registry(runtime)
        if registry is None:
            return {"skills": [], "available": False}
        overrides = skill_overrides.list_overrides()
        skills = []
        for tool in registry.all():
            override = overrides.get(tool.name, {})
            try:
                enabled_by_config = bool(tool.enabled())
            except Exception:
                enabled_by_config = False
            skills.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "category": (tool.metadata or {}).get("category", ""),
                    "enabled_by_config": enabled_by_config,
                    "user_disabled": bool(override.get("disabled", False)),
                    "reason": override.get("reason", ""),
                }
            )
        skills.sort(key=lambda x: (x["category"], x["name"]))
        return {"skills": skills, "available": True}

    @router.post("/{name}/toggle")
    async def toggle(
        name: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        registry = _tool_registry(runtime)
        if registry is None:
            raise HTTPException(status_code=503, detail="tool_registry 未就绪")
        tool = registry.get(name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"无此 skill：{name}")
        disabled = bool(body.get("disabled", False))
        reason = str(body.get("reason", "") or "")
        skill_overrides.set_disabled(name, disabled, reason=reason)
        webui_audit_log.record(
            action="skill_toggle",
            qq=admin.qq,
            device_id=admin.device_id,
            target=name,
            detail={"disabled": disabled, "reason": reason},
        )
        return {"success": True, "name": name, "user_disabled": disabled}

    return router
