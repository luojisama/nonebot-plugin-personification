from __future__ import annotations

import contextvars
import json
import re
import time
import uuid
from typing import Any

from .db import connect_sync
from .plugin_runtime_logs import sanitize_text


_CURRENT_TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "personification_reply_trace_id",
    default="",
)
_ELAPSED_RE = re.compile(r"(?:elapsed_ms=|耗时\s*)(\d{1,9})(?:\s*ms)?", re.I)
_SIGNAL_KEY_RE = re.compile(
    r"(?:^|\s)(action|speech_act|output|intent|ambiguity|tool|budget|suggested_steps|actual_steps|suggested_seconds|actual_seconds|topic_thread|topic_speaker|reply_to_bot|bot_in_thread|parallel_threads|participants|reason|source|flags|revision|chars|address_mode|quote|at|target|query|finish)=([^\s]+)"
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


def _stage_category(stage: dict[str, Any]) -> str:
    key = str(stage.get("key") or "").strip().lower()
    label = str(stage.get("label") or "").strip()
    text = f"{key} {label}"
    if "tool" in key or "工具" in label:
        return "tool"
    if key.startswith("agent_") or "agent" in text.lower():
        return "agent"
    if key.startswith("semantic") or key.startswith("turn_plan") or "语义" in label:
        return "semantic"
    if key.startswith("send") or "发送" in label:
        return "send"
    if key.startswith("capture") or key.startswith("reply_timeout") or "捕获" in label:
        return "capture"
    if key.startswith("rule") or key.startswith("buffer") or "缓冲" in label:
        return "dispatch"
    return "runtime"


def _elapsed_from_detail(detail: Any) -> int | None:
    match = _ELAPSED_RE.search(str(detail or ""))
    if not match:
        return None
    try:
        return max(0, int(match.group(1)))
    except Exception:
        return None


def _signals_from_detail(detail: Any) -> dict[str, str]:
    signals: dict[str, str] = {}
    for match in _SIGNAL_KEY_RE.finditer(str(detail or "")):
        key = str(match.group(1) or "").strip()
        value = sanitize_text(match.group(2) or "")[:80]
        if key and value and key not in signals:
            signals[key] = value
    return signals


def _compact_value(value: Any, *, limit: int = 80) -> str:
    text = sanitize_text(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _append_unique(values: list[str], value: Any, *, limit: int = 80, max_items: int = 6) -> None:
    text = _compact_value(value, limit=limit)
    if text and text not in values and len(values) < max_items:
        values.append(text)


def _build_agent_inspection(items: list[dict[str, Any]]) -> dict[str, Any]:
    understanding: dict[str, str] = {}
    addressing: dict[str, str] = {}
    tools: list[dict[str, Any]] = []
    questions: list[str] = []
    quality: list[str] = []
    budget: dict[str, str] = {}
    for item in items:
        key = str(item.get("key") or "")
        detail = str(item.get("detail") or "")
        signals = item.get("signals") if isinstance(item.get("signals"), dict) else {}
        if key in {"semantic_frame", "agent_intent", "agent_result"}:
            for name in ("intent", "ambiguity", "speech_act", "output"):
                if signals.get(name) and name not in understanding:
                    understanding[name] = str(signals[name])
        if key == "addressing_plan":
            for name in ("address_mode", "source", "quote", "at", "target"):
                if signals.get(name):
                    addressing[name] = str(signals[name])
        if key in {"agent_tool_call", "agent_tool_result"} or item.get("category") == "tool":
            tool_name = str(signals.get("tool") or "")
            if not tool_name:
                match = re.search(r"(?:tool|name)=([^\s]+)", detail)
                tool_name = str(match.group(1)) if match else ""
            entry = {
                "stage": "result" if "result" in key else "call",
                "tool": _compact_value(tool_name or item.get("label") or key, limit=48),
                "status": str(item.get("status") or ""),
                "detail": _compact_value(detail, limit=180),
                "duration_ms": item.get("duration_ms"),
            }
            if entry["tool"] or entry["detail"]:
                tools.append(entry)
        if key in {"agent_query_rewrite", "agent_budget", "semantic_frame"}:
            for name in ("query", "reason"):
                if signals.get(name):
                    _append_unique(questions, signals[name])
            if key == "agent_query_rewrite" and detail:
                _append_unique(questions, detail, limit=140)
        if key == "agent_reply_quality":
            _append_unique(quality, detail, limit=160)
        if key == "agent_budget":
            for name in ("budget", "suggested_steps", "actual_steps", "suggested_seconds", "actual_seconds", "source"):
                if signals.get(name):
                    budget[name] = str(signals[name])
    return {
        "understanding": understanding,
        "addressing": addressing,
        "tools": tools[:10],
        "questions": questions[:6],
        "quality": quality[:4],
        "budget": budget,
    }


def build_process_view(trace: dict[str, Any] | None, *, logs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a WebUI-safe process timeline.

    This intentionally summarizes observable stages only. It does not expose
    model hidden reasoning, prompts, raw tool arguments, or full tool results.
    """

    if not isinstance(trace, dict):
        trace = {}
    raw_stages = trace.get("stages") if isinstance(trace.get("stages"), list) else []
    stages = [stage for stage in raw_stages if isinstance(stage, dict)]
    base_ts = 0.0
    for stage in stages:
        try:
            ts = float(stage.get("ts") or 0)
        except Exception:
            ts = 0.0
        if ts > 0:
            base_ts = ts
            break

    items: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for index, stage in enumerate(stages):
        try:
            ts = float(stage.get("ts") or 0)
        except Exception:
            ts = 0.0
        next_ts = 0.0
        if index + 1 < len(stages):
            try:
                next_ts = float(stages[index + 1].get("ts") or 0)
            except Exception:
                next_ts = 0.0
        detail = sanitize_text(stage.get("detail", ""))[:1000]
        hint = sanitize_text(stage.get("hint", ""))[:500]
        status = str(stage.get("status") or "info")[:16]
        category = _stage_category(stage)
        elapsed_ms = _elapsed_from_detail(detail)
        if elapsed_ms is None and ts > 0 and next_ts > ts:
            elapsed_ms = int((next_ts - ts) * 1000)
        item = {
            "index": index + 1,
            "key": str(stage.get("key") or "")[:64],
            "label": str(stage.get("label") or stage.get("key") or "")[:80],
            "status": status,
            "category": category,
            "detail": detail,
            "signals": _signals_from_detail(detail),
            "hint": hint,
            "ts": ts,
            "offset_ms": int((ts - base_ts) * 1000) if ts > 0 and base_ts > 0 else 0,
            "duration_ms": elapsed_ms,
        }
        items.append(item)
        status_counts[status] = status_counts.get(status, 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1

    log_rows = logs if isinstance(logs, list) else []
    log_levels: dict[str, int] = {}
    for row in log_rows:
        if not isinstance(row, dict):
            continue
        level = str(row.get("level") or "INFO").upper()
        log_levels[level] = log_levels.get(level, 0) + 1

    slow_items = [
        {
            "index": item["index"],
            "label": item["label"],
            "key": item["key"],
            "duration_ms": item["duration_ms"],
        }
        for item in items
        if isinstance(item.get("duration_ms"), int) and int(item["duration_ms"]) >= 1000
    ]
    slow_items.sort(key=lambda item: int(item.get("duration_ms") or 0), reverse=True)

    outcome = str(trace.get("outcome") or "")
    diagnosis_code = str(trace.get("diagnosis_code") or "")
    return {
        "summary": {
            "trace_id": str(trace.get("trace_id") or ""),
            "outcome": outcome,
            "diagnosis_code": diagnosis_code,
            "stage_count": len(items),
            "error_count": sum(status_counts.get(name, 0) for name in ("error", "failed")),
            "warn_count": status_counts.get("warn", 0) + status_counts.get("warning", 0),
            "log_count": len(log_rows),
            "status_counts": status_counts,
            "category_counts": category_counts,
            "log_levels": log_levels,
            "slow_stages": slow_items[:5],
        },
        "items": items,
        "agent_inspection": _build_agent_inspection(items),
    }


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
    "build_process_view",
    "new_trace_id",
    "prune_old_entries",
    "query_recent",
    "record_stage",
    "reset_current_trace_id",
    "set_current_trace_id",
    "start_trace",
]
