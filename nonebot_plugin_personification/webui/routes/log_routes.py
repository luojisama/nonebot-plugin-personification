from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core import plugin_runtime_logs
from ..deps import AdminIdentity, require_admin


def build_log_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/logs", tags=["logs"])

    @router.get("/recent")
    async def recent(
        limit: int = Query(default=200, ge=1, le=500),
        level: str = Query(default=""),
        q: str = Query(default=""),
        cursor: int = Query(default=0, ge=0),
        trace_id: str = Query(default=""),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        cfg = getattr(runtime, "plugin_config", None)
        pruned = plugin_runtime_logs.maybe_prune(config=cfg)
        rows = plugin_runtime_logs.query_recent(
            limit=limit,
            level=level,
            q=q,
            cursor=cursor,
            trace_id=trace_id,
        )
        return {
            "entries": rows,
            "next_cursor": rows[-1]["id"] if rows else 0,
            "retention_days": plugin_runtime_logs.retention_days_from_config(cfg),
            "pruned": pruned,
        }

    @router.delete("/clear")
    async def clear(_: AdminIdentity = Depends(require_admin)) -> dict:
        deleted = plugin_runtime_logs.clear_all()
        return {"deleted": deleted}

    return router


__all__ = ["build_log_router"]
