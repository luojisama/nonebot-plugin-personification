from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ...core import plugin_update_manager, webui_audit_log
from ..deps import AdminIdentity, require_admin


def _audit(
    *,
    action: str,
    admin: AdminIdentity,
    outcome: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    webui_audit_log.record(
        action=action,
        qq=admin.qq,
        device_id=admin.device_id,
        target=target,
        detail=detail or {},
        outcome=outcome,
    )


def build_plugin_manager_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/plugin-manager", tags=["plugin-manager"])

    @router.get("/status")
    async def status(
        refresh: bool = Query(default=False),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        return await plugin_update_manager.get_plugin_update_status(
            plugin_config=getattr(runtime, "plugin_config", None),
            refresh=refresh,
        )

    @router.get("/history")
    async def history(
        limit: int = Query(default=30, ge=1, le=100),
        refresh: bool = Query(default=False),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        return await plugin_update_manager.get_plugin_update_history(
            plugin_config=getattr(runtime, "plugin_config", None),
            limit=limit,
            refresh=refresh,
        )

    @router.post("/check")
    async def check(admin: AdminIdentity = Depends(require_admin)) -> dict:
        result = await plugin_update_manager.get_plugin_update_status(
            plugin_config=getattr(runtime, "plugin_config", None),
            refresh=True,
        )
        outcome = "ok" if result.get("fetch", {}).get("ok", True) else "error"
        _audit(
            action="plugin_update_check",
            admin=admin,
            outcome=outcome,
            target=str((result.get("source") or {}).get("upstream") or ""),
            detail={
                "source_type": result.get("source_type"),
                "update_available": result.get("update_available"),
                "message": result.get("message"),
            },
        )
        return result

    @router.post("/update")
    async def update(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        if str(body.get("confirm", "") or "").strip().lower() != "update":
            raise HTTPException(status_code=400, detail="缺少确认参数")
        result = await plugin_update_manager.perform_plugin_update(
            plugin_config=getattr(runtime, "plugin_config", None),
        )
        status_body = result.get("status") if isinstance(result.get("status"), dict) else {}
        target = str(((status_body or {}).get("source") or {}).get("upstream") or "")
        _audit(
            action="plugin_update_apply",
            admin=admin,
            outcome="ok" if result.get("ok") else "error",
            target=target,
            detail={
                "updated": result.get("updated"),
                "message": result.get("message") or result.get("error"),
                "source_type": (status_body or {}).get("source_type"),
            },
        )
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            if result.get("ok"):
                logger.info(f"[webui] 管理员 {admin.qq} 执行插件更新：updated={bool(result.get('updated'))}")
            else:
                logger.warning(f"[webui] 管理员 {admin.qq} 执行插件更新失败：{result.get('error')}")
        return result

    return router
