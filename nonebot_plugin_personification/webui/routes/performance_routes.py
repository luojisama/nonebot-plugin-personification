from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...core import runtime_performance
from ..deps import AdminIdentity, require_admin


def build_performance_router(*, runtime: Any) -> APIRouter:  # noqa: ARG001
    router = APIRouter(prefix="/api/performance", tags=["performance"])

    @router.get("/runtime")
    async def runtime_snapshot(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
        return runtime_performance.snapshot()

    return router


__all__ = ["build_performance_router"]
