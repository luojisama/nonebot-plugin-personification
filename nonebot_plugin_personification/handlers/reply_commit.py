from __future__ import annotations

import asyncio
import time
from typing import Any


_LOCK_KEY = "reply_commit_lock"
_ACQUIRED_KEY = "_reply_commit_lock_acquired"
_DELIVERY_STARTED_KEY = "reply_delivery_started"
_DELIVERY_CONFIRMED_KEY = "reply_delivery_confirmed"
_DELIVERY_COMPLETE_KEY = "reply_delivery_complete"
_STARTED_AT_KEY = "_reply_lifecycle_started_at"
_PHASE_KEY = "reply_last_phase"
_PHASE_STARTED_AT_KEY = "_reply_phase_started_at"


def begin_reply_lifecycle(state: dict[str, Any], phase: str = "reply_pipeline") -> None:
    now = time.monotonic()
    state.setdefault(_STARTED_AT_KEY, now)
    state[_PHASE_KEY] = str(phase or "reply_pipeline").strip()[:64]
    state[_PHASE_STARTED_AT_KEY] = now


def mark_reply_phase(state: dict[str, Any], phase: str) -> None:
    if _STARTED_AT_KEY not in state:
        begin_reply_lifecycle(state, phase)
        return
    state[_PHASE_KEY] = str(phase or "unknown").strip()[:64]
    state[_PHASE_STARTED_AT_KEY] = time.monotonic()


def reply_lifecycle_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    now = time.monotonic()
    try:
        started_at = float(state.get(_STARTED_AT_KEY, now) or now)
    except (TypeError, ValueError):
        started_at = now
    try:
        phase_started_at = float(state.get(_PHASE_STARTED_AT_KEY, started_at) or started_at)
    except (TypeError, ValueError):
        phase_started_at = started_at
    return {
        "last_phase": str(state.get(_PHASE_KEY, "unknown") or "unknown")[:64],
        "elapsed_ms": max(0, int((now - started_at) * 1000)),
        "phase_age_ms": max(0, int((now - phase_started_at) * 1000)),
    }


async def acquire_reply_commit(state: dict[str, Any]) -> None:
    """Acquire the per-session delivery gate once for the current turn."""
    if bool(state.get(_ACQUIRED_KEY)):
        return
    lock = state.get(_LOCK_KEY)
    if not isinstance(lock, asyncio.Lock):
        return
    await lock.acquire()
    state[_ACQUIRED_KEY] = True


def release_reply_commit(state: dict[str, Any]) -> None:
    if not bool(state.pop(_ACQUIRED_KEY, False)):
        return
    lock = state.get(_LOCK_KEY)
    if isinstance(lock, asyncio.Lock) and lock.locked():
        lock.release()


def mark_reply_delivery_started(state: dict[str, Any]) -> None:
    state[_DELIVERY_STARTED_KEY] = True


def mark_reply_delivery_confirmed(state: dict[str, Any]) -> None:
    state[_DELIVERY_STARTED_KEY] = True
    state[_DELIVERY_CONFIRMED_KEY] = True


def mark_reply_delivery_complete(state: dict[str, Any]) -> None:
    if bool(state.get(_DELIVERY_CONFIRMED_KEY, False)):
        state[_DELIVERY_COMPLETE_KEY] = True


async def execute_pending_actions(
    executor: Any,
    actions: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
) -> list[str]:
    """Execute staged Agent actions and return their visible history projections."""
    if executor is None or not actions:
        return []

    from ..core.qq_expression_tools import qq_action_history_text

    history_parts: list[str] = []
    for action in actions:
        if state is not None:
            mark_reply_delivery_started(state)
        await executor.execute(action["type"], action["params"])
        confirmed = bool(getattr(executor, "last_delivery_confirmed", True))
        if state is not None and confirmed:
            mark_reply_delivery_confirmed(state)
        if confirmed:
            history_text = qq_action_history_text(action)
            if history_text:
                history_parts.append(history_text)
        else:
            break
    actions.clear()
    return history_parts


__all__ = [
    "acquire_reply_commit",
    "begin_reply_lifecycle",
    "execute_pending_actions",
    "mark_reply_phase",
    "mark_reply_delivery_complete",
    "mark_reply_delivery_confirmed",
    "mark_reply_delivery_started",
    "release_reply_commit",
    "reply_lifecycle_snapshot",
]
