"""WebUI 主动行为诊断路由：暴露 proactive_diagnostics 表给前端查看。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core import proactive_diagnostics
from ..deps import AdminIdentity, require_admin


def build_proactive_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/proactive", tags=["proactive"])

    @router.get("/recent")
    async def recent(
        scope: str = Query(default=""),
        target: str = Query(default=""),
        limit: int = Query(default=100, ge=1, le=500),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        rows = proactive_diagnostics.query_recent(
            scope=scope,
            target=target,
            limit=limit,
        )
        return {"entries": rows}

    @router.get("/stats")
    async def stats(
        scope: str = Query(default=""),
        since_hours: float = Query(default=72.0, ge=1.0, le=720.0),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        counts = proactive_diagnostics.query_skip_reason_stats(
            scope=scope,
            since_seconds=since_hours * 3600,
        )
        # 区分 sent vs skip_*
        sent_count = 0
        skip_count = 0
        for outcome, n in counts.items():
            if outcome == "sent":
                sent_count += n
            elif outcome.startswith("skip_"):
                skip_count += n
        total = sum(counts.values())
        return {
            "scope": scope or "all",
            "since_hours": since_hours,
            "counts": counts,
            "sent": sent_count,
            "skip": skip_count,
            "total": total,
        }

    @router.get("/next-eligible")
    async def next_eligible(
        scope: str = Query(default=""),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        rows = proactive_diagnostics.query_next_eligible(scope=scope)
        return {"entries": rows}

    return router
