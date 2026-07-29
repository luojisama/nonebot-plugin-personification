from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ...core import webui_audit_log
from ...core.user_policy import PolicyRevisionConflict
from ...core.user_profile_purge import purge_user_profile_data
from ..deps import AdminIdentity, require_admin


_USER_ID_RE = re.compile(r"[1-9][0-9]{4,19}\Z")
_TIERS = frozenset(
    {
        "allow",
        "blocked",
        "level_1",
        "level_2",
        "permanent",
        "manual_block",
        "manual_allow",
    }
)


class OverrideBody(BaseModel):
    mode: Literal["block", "allow", "inherit"]
    expected_revision: int = Field(..., ge=0)
    expires_at: float = Field(default=0.0, ge=0.0)
    reason_code: str = Field(default="webui_manual_override", max_length=64)


class ClearAutoBody(BaseModel):
    expected_revision: int = Field(..., ge=0)


class PurgeProfileBody(BaseModel):
    expected_revision: int = Field(..., ge=0)
    confirmation: str = Field(..., min_length=1, max_length=80)


def _bundle(runtime: Any) -> Any:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None or getattr(bundle, "user_policy_service", None) is None:
        raise HTTPException(status_code=503, detail="user policy service 未就绪")
    return bundle


def _user_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _USER_ID_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="user_id 无效")
    return normalized


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


def _conflict(exc: PolicyRevisionConflict, service: Any, user_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "policy_revision_conflict",
            "message": str(exc),
            "current_state": service.get_state(user_id).to_dict(),
        },
    )


def _audit(
    action: str,
    admin: AdminIdentity,
    user_id: str,
    detail: dict[str, Any],
    *,
    outcome: str = "ok",
) -> None:
    webui_audit_log.record(
        action=action,
        qq=admin.qq,
        device_id=admin.device_id,
        target=user_id,
        detail=detail,
        outcome=outcome,
    )


def build_user_policy_router(*, runtime: Any) -> APIRouter:
    router = APIRouter(prefix="/api/user-policy", tags=["user-policy"])

    @router.get("/states")
    async def states(
        response: Response,
        tier: str = Query(default="", max_length=32),
        limit: int = Query(default=200, ge=1, le=500),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _no_store(response)
        normalized_tier = str(tier or "").strip().lower()
        if normalized_tier and normalized_tier not in _TIERS:
            raise HTTPException(status_code=400, detail="tier 无效")
        service = _bundle(runtime).user_policy_service
        fetch_limit = 1000 if normalized_tier else limit
        rows = await asyncio.to_thread(service.list_states, limit=fetch_limit)
        if normalized_tier:
            if normalized_tier == "blocked":
                rows = [item for item in rows if bool(item.get("blocked"))][:limit]
            else:
                rows = [
                    item
                    for item in rows
                    if str(item.get("effective_tier", "") or "") == normalized_tier
                ][:limit]
        return {"states": rows, "tier": normalized_tier, "available": True}

    @router.get("/{user_id}/events")
    async def events(
        user_id: str,
        response: Response,
        include_evidence: bool = Query(default=True),
        limit: int = Query(default=100, ge=1, le=500),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _no_store(response)
        uid = _user_id(user_id)
        service = _bundle(runtime).user_policy_service
        state, rows = await asyncio.gather(
            asyncio.to_thread(service.get_state, uid),
            asyncio.to_thread(
                service.list_events,
                uid,
                limit=limit,
                include_evidence=bool(include_evidence),
            ),
        )
        return {
            "user_id": uid,
            "state": state.to_dict(),
            "events": rows,
            "evidence_retention_seconds": 7 * 24 * 60 * 60,
        }

    @router.post("/{user_id}/override")
    async def override(
        user_id: str,
        body: OverrideBody,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _no_store(response)
        uid = _user_id(user_id)
        bundle = _bundle(runtime)
        service = bundle.user_policy_service
        try:
            state = await asyncio.to_thread(
                service.set_manual_override,
                user_id=uid,
                mode=body.mode,
                actor=f"webui:{admin.qq}",
                reason_code=body.reason_code or "webui_manual_override",
                expires_at=0.0 if body.mode == "inherit" else body.expires_at,
                expected_revision=body.expected_revision,
            )
        except PolicyRevisionConflict as exc:
            _audit(
                "user_policy_override",
                admin,
                uid,
                {"mode": body.mode, "expected_revision": body.expected_revision},
                outcome="conflict",
            )
            raise _conflict(exc, service, uid) from exc
        gate = getattr(bundle, "qq_user_policy_gate", None)
        clear_cache = getattr(gate, "clear_cache", None)
        if callable(clear_cache):
            await clear_cache()
        _audit(
            "user_policy_override",
            admin,
            uid,
            {
                "mode": body.mode,
                "expires_at": body.expires_at,
                "revision": state.revision,
            },
        )
        return {"state": state.to_dict(), "updated": True}

    @router.post("/{user_id}/clear-auto")
    async def clear_auto(
        user_id: str,
        body: ClearAutoBody,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _no_store(response)
        uid = _user_id(user_id)
        bundle = _bundle(runtime)
        service = bundle.user_policy_service
        try:
            state = await asyncio.to_thread(
                service.clear_auto,
                user_id=uid,
                actor=f"webui:{admin.qq}",
                expected_revision=body.expected_revision,
            )
        except PolicyRevisionConflict as exc:
            _audit(
                "user_policy_clear_auto",
                admin,
                uid,
                {"expected_revision": body.expected_revision},
                outcome="conflict",
            )
            raise _conflict(exc, service, uid) from exc
        _audit(
            "user_policy_clear_auto",
            admin,
            uid,
            {"revision": state.revision},
        )
        return {"state": state.to_dict(), "updated": True}

    @router.delete("/{user_id}/profile")
    async def purge_profile(
        user_id: str,
        body: PurgeProfileBody,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _no_store(response)
        uid = _user_id(user_id)
        bundle = _bundle(runtime)
        service = bundle.user_policy_service
        state = await asyncio.to_thread(service.get_state, uid)
        if state.revision != body.expected_revision:
            exc = PolicyRevisionConflict(
                f"expected revision {body.expected_revision}, got {state.revision}"
            )
            _audit(
                "user_profile_purge",
                admin,
                uid,
                {"expected_revision": body.expected_revision},
                outcome="conflict",
            )
            raise _conflict(exc, service, uid)
        expected_confirmation = f"PURGE PROFILE {uid}"
        if body.confirmation != expected_confirmation:
            _audit(
                "user_profile_purge",
                admin,
                uid,
                {"revision": state.revision},
                outcome="denied",
            )
            raise HTTPException(
                status_code=400,
                detail=f"确认串必须精确等于 {expected_confirmation}",
            )
        started_at = time.time()
        counts = await purge_user_profile_data(bundle, uid)
        current_state = await asyncio.to_thread(service.get_state, uid)
        _audit(
            "user_profile_purge",
            admin,
            uid,
            {
                "revision": current_state.revision,
                "counts": counts,
                "elapsed_ms": int((time.time() - started_at) * 1000),
                "policy_state_retained": True,
            },
        )
        return {
            "purged": True,
            "counts": counts,
            "state": current_state.to_dict(),
            "policy_state_retained": True,
        }

    return router


__all__ = ["build_user_policy_router"]
