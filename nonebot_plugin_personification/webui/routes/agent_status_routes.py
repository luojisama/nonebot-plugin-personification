from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from ...agent.inner_state import get_personification_data_dir, load_inner_state
from ...core import metrics, reply_turn_trace
from ..deps import AdminIdentity, require_admin


def _bots(runtime: Any) -> list[Any]:
    try:
        items = runtime.get_bots() if callable(getattr(runtime, "get_bots", None)) else {}
    except Exception:
        items = {}
    return list(items.values()) if isinstance(items, dict) else []


def build_agent_status_router(*, runtime: Any) -> APIRouter:
    router = APIRouter(prefix="/api/agent-status", tags=["agent-status"])

    @router.get("")
    async def status(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
        now = time.time()
        timeout = max(10.0, float(getattr(runtime.plugin_config, "personification_response_timeout", 120) or 120))
        traces = reply_turn_trace.query_recent(limit=80)
        recent: list[dict[str, Any]] = []
        running = 0
        stale = 0
        relevant_stale = 0
        stale_relevance_window = max(timeout * 3, 300.0)
        outcomes: dict[str, int] = {}
        for trace in traces:
            updated_at = float(trace.get("ts", 0) or 0)
            outcome = str(trace.get("outcome", "") or "")
            age = max(0.0, now - updated_at)
            state = "finished"
            if not outcome and age <= timeout + 15:
                state = "running"
                running += 1
            elif not outcome:
                state = "stale"
                stale += 1
                if age <= stale_relevance_window:
                    relevant_stale += 1
            else:
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            stages = trace.get("stages") if isinstance(trace.get("stages"), list) else []
            last_stage = stages[-1] if stages and isinstance(stages[-1], dict) else {}
            recent.append({
                "trace_id": str(trace.get("trace_id", "") or ""),
                "session_type": str(trace.get("session_type", "") or ""),
                "group_id": str(trace.get("group_id", "") or ""),
                "state": state,
                "outcome": outcome,
                "diagnosis_code": str(trace.get("diagnosis_code", "") or ""),
                "updated_at": updated_at,
                "age_seconds": round(age, 1),
                "stage": str(last_stage.get("label") or last_stage.get("key") or ""),
                "stage_status": str(last_stage.get("status", "") or ""),
            })
        try:
            inner = await load_inner_state(get_personification_data_dir(runtime.plugin_config))
        except Exception:
            inner = {}
        snapshot = metrics.snapshot_metrics()
        bots = _bots(runtime)
        enabled = bool(getattr(runtime.plugin_config, "personification_agent_enabled", True))
        overall = "offline" if not bots else "degraded" if not enabled or relevant_stale else "online"
        return {
            "overall": overall,
            "updated_at": now,
            "bots": {"connected": len(bots), "ids": [str(getattr(bot, "self_id", "") or "") for bot in bots]},
            "agent_enabled": enabled,
            "running": running,
            "stale": stale,
            "outcomes": outcomes,
            "inner_state": {
                "mood": str(inner.get("mood", "") or ""),
                "energy": str(inner.get("energy", "") or ""),
                "pending_count": len(inner.get("pending_thoughts", []) or []),
                "updated_at": str(inner.get("updated_at", "") or ""),
            },
            "metrics": {"counters": list(snapshot.get("counters", []))[:12], "timings": list(snapshot.get("timings", []))[:12]},
            "recent": recent[:30],
        }

    return router


__all__ = ["build_agent_status_router"]
