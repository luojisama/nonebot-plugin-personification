from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core import metrics, token_ledger
from ..deps import AdminIdentity, require_admin


def build_metrics_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/metrics", tags=["metrics"])

    @router.get("/summary")
    async def summary(
        window: str = Query(default="month", pattern="^(day|week|month)$"),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        return token_ledger.query_summary(window)

    @router.get("/group/{group_id}")
    async def group_detail(
        group_id: str,
        window: str = Query(default="month", pattern="^(day|week|month)$"),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        return token_ledger.query_group_detail(group_id, window)

    @router.get("/runtime")
    async def runtime_snapshot(_: AdminIdentity = Depends(require_admin)) -> dict:
        snap = metrics.snapshot_metrics()
        return {
            "counters": list(snap.get("counters", []))[:30],
            "timings": list(snap.get("timings", []))[:30],
        }

    return router
