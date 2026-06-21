from __future__ import annotations

import contextvars
import json
import time
import uuid
from typing import Any

from .db import connect_sync
from .plugin_runtime_logs import sanitize_text


_CURRENT_TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "personification_reply_trace_id",
    default="",
)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    return str(_CURRENT_TRACE_ID.get("") or "")


def set_current_trace_id(trace_id: str) -> contextvars.Token[str]:
    return _CURRENT_TRACE_ID.set(str(trace_id or ""))


def reset_current_trace_id(token: contextvars.Token[str]) -> None:
    _CURRENT_TRACE_ID.reset(token)


def _safe_json(value: Any, *, limit: int = 8000) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        payload = json.dumps({"value": sanitize_text(value)}, ensure_ascii=False, separators=(",", ":"))
    return payload[:limit]


def _load_stages(conn: Any, trace_id: str) -> list[dict[str, Any]]:
    row = conn.execute(
        "SELECT stages FROM reply_turn_traces WHERE trace_id=?",
        (trace_id,),
    ).fetchone()
    if not row:
        return []
    try:
        loaded = json.loads(row["stages"] or "[]")
    except Exception:
        loaded = []
    return loaded if isinstance(loaded, list) else []


def start_trace(
    *,
    trace_id: str = "",
    session_type: str = "",
    group_id: str = "",
    user_id: str = "",
    detail: dict[str, Any] | None = None,
) -> str:
    trace = str(trace_id or "").strip() or new_trace_id()
    payload = dict(detail or {})
    try:
        with connect_sync() as conn:
            conn.execute(
                """
                INSERT INTO reply_turn_traces(
                    trace_id, ts, session_type, group_id, user_id, stages,
                    outcome, diagnosis_code, detail
                )
                VALUES (?, ?, ?, ?, ?, '[]', '', '', ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    ts=excluded.ts,
                    session_type=excluded.session_type,
                    group_id=excluded.group_id,
                    user_id=excluded.user_id,
                    detail=excluded.detail
                """,
                (
                    trace,
                    time.time(),
                    str(session_type or "")[:24],
                    str(group_id or "")[:32],
                    str(user_id or "")[:32],
                    _safe_json(payload, limit=4000),
                ),
            )
            conn.commit()
    except Exception:
        pass
    return trace


def record_stage(
    *,
    trace_id: str = "",
    key: str,
    label: str = "",
    status: str = "info",
    detail: Any = "",
    hint: str = "",
) -> None:
    trace = str(trace_id or current_trace_id() or "").strip()
    if not trace:
        return
    stage = {
        "ts": time.time(),
        "key": str(key or "")[:64],
        "label": str(label or key or "")[:80],
        "status": str(status or "info")[:16],
        "detail": sanitize_text(detail)[:1000],
        "hint": sanitize_text(hint)[:500],
    }
    try:
        with connect_sync() as conn:
            stages = _load_stages(conn, trace)
            stages.append(stage)
            if len(stages) > 80:
                stages = stages[-80:]
            conn.execute(
                """
                UPDATE reply_turn_traces
                SET ts=?, stages=?
                WHERE trace_id=?
                """,
                (time.time(), _safe_json(stages), trace),
            )
            conn.commit()
    except Exception:
        pass


def finish_trace(
    *,
    trace_id: str = "",
    outcome: str,
    diagnosis_code: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    trace = str(trace_id or current_trace_id() or "").strip()
    if not trace:
        return
    try:
        with connect_sync() as conn:
            conn.execute(
                """
                UPDATE reply_turn_traces
                SET ts=?, outcome=?, diagnosis_code=?, detail=?
                WHERE trace_id=?
                """,
                (
                    time.time(),
                    str(outcome or "")[:32],
                    str(diagnosis_code or "")[:64],
                    _safe_json(detail or {}, limit=4000),
                    trace,
                ),
            )
            conn.commit()
    except Exception:
        pass


def get_trace(trace_id: str) -> dict[str, Any] | None:
    trace = str(trace_id or "").strip()
    if not trace:
        return None
    with connect_sync() as conn:
        row = conn.execute(
            """
            SELECT trace_id, ts, session_type, group_id, user_id, stages,
                   outcome, diagnosis_code, detail
            FROM reply_turn_traces
            WHERE trace_id=?
            """,
            (trace,),
        ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def query_recent(
    *,
    limit: int = 50,
    session_type: str = "",
    group_id: str = "",
    user_id: str = "",
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if session_type:
        clauses.append("session_type = ?")
        params.append(str(session_type)[:24])
    if group_id:
        clauses.append("group_id = ?")
        params.append(str(group_id)[:32])
    if user_id:
        clauses.append("user_id = ?")
        params.append(str(user_id)[:32])
    params.append(max(1, min(int(limit or 50), 200)))
    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT trace_id, ts, session_type, group_id, user_id, stages,
                   outcome, diagnosis_code, detail
            FROM reply_turn_traces
            WHERE {' AND '.join(clauses)}
            ORDER BY ts DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def prune_old_entries(*, retention_days: int = 7, max_entries: int = 2000) -> int:
    cutoff = time.time() - max(1, int(retention_days or 7)) * 86400
    max_keep = max(100, int(max_entries or 2000))
    deleted = 0
    with connect_sync() as conn:
        cursor = conn.execute("DELETE FROM reply_turn_traces WHERE ts < ?", (cutoff,))
        deleted += int(cursor.rowcount or 0)
        cursor = conn.execute(
            """
            DELETE FROM reply_turn_traces
            WHERE trace_id NOT IN (
                SELECT trace_id FROM reply_turn_traces ORDER BY ts DESC LIMIT ?
            )
            """,
            (max_keep,),
        )
        deleted += int(cursor.rowcount or 0)
        conn.commit()
    return deleted


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        stages = json.loads(row["stages"] or "[]")
    except Exception:
        stages = []
    try:
        detail = json.loads(row["detail"] or "{}")
    except Exception:
        detail = {}
    return {
        "trace_id": str(row["trace_id"] or ""),
        "ts": float(row["ts"] or 0),
        "session_type": str(row["session_type"] or ""),
        "group_id": str(row["group_id"] or ""),
        "user_id": str(row["user_id"] or ""),
        "stages": stages if isinstance(stages, list) else [],
        "outcome": str(row["outcome"] or ""),
        "diagnosis_code": str(row["diagnosis_code"] or ""),
        "detail": detail if isinstance(detail, dict) else {},
    }


__all__ = [
    "current_trace_id",
    "finish_trace",
    "get_trace",
    "new_trace_id",
    "prune_old_entries",
    "query_recent",
    "record_stage",
    "reset_current_trace_id",
    "set_current_trace_id",
    "start_trace",
]
