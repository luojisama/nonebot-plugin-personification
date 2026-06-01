"""主动行为诊断日志：记录每次 proactive job 触发结果与 skip 原因，便于 WebUI 排查。"""
from __future__ import annotations

import json
import time
from typing import Any

from .db import connect_sync


_RETENTION_DAYS = 30


# 标准化 skip 原因码——前端可按 code 分组渲染
SKIP_DAILY_LIMIT = "skip_daily_limit"
SKIP_COOLDOWN = "skip_cooldown"
SKIP_IDLE_NOT_REACHED = "skip_idle_not_reached"
SKIP_PROBABILITY = "skip_probability"
SKIP_QUIET_HOUR = "skip_quiet_hour"
SKIP_NO_CANDIDATE = "skip_no_candidate"
SKIP_LLM_FAILED = "skip_llm_failed"
SKIP_LLM_DECIDED = "skip_llm_decided"
SKIP_UNREAD = "skip_unread"
SKIP_DISABLED = "skip_disabled"
SKIP_NO_PROFILE = "skip_no_profile"
SKIP_OTHER = "skip_other"
OUTCOME_SENT = "sent"


def record(
    *,
    scope: str,
    outcome: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
    next_eligible_at: float | None = None,
) -> None:
    """记录一次诊断条目。失败时静默吞，不影响主流程。"""
    try:
        with connect_sync() as conn:
            conn.execute(
                """
                INSERT INTO proactive_diagnostics
                    (ts, scope, target, outcome, detail, next_eligible_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    str(scope or "")[:24],
                    str(target or "")[:64],
                    str(outcome or "")[:32],
                    json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))[:2000],
                    float(next_eligible_at) if next_eligible_at else None,
                ),
            )
            conn.commit()
    except Exception:
        return


def query_recent(
    *,
    scope: str = "",
    limit: int = 100,
    target: str = "",
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if scope:
        clauses.append("scope = ?")
        params.append(str(scope))
    if target:
        clauses.append("target = ?")
        params.append(str(target))
    params.append(max(1, min(int(limit), 500)))
    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT id, ts, scope, target, outcome, detail, next_eligible_at
            FROM proactive_diagnostics
            WHERE {' AND '.join(clauses)}
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except Exception:
            detail = {}
        out.append({
            "id": int(row["id"]),
            "ts": float(row["ts"] or 0),
            "scope": str(row["scope"] or ""),
            "target": str(row["target"] or ""),
            "outcome": str(row["outcome"] or ""),
            "detail": detail if isinstance(detail, dict) else {},
            "next_eligible_at": float(row["next_eligible_at"] or 0) or None,
        })
    return out


def query_skip_reason_stats(
    *,
    scope: str = "",
    since_seconds: float = 86400 * 3,
) -> dict[str, int]:
    """按 outcome（含 sent / skip_X）统计最近 N 秒内的次数。"""
    since = time.time() - max(0, float(since_seconds))
    clauses = ["ts >= ?"]
    params: list[Any] = [since]
    if scope:
        clauses.append("scope = ?")
        params.append(str(scope))
    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT outcome, COUNT(*) AS cnt
            FROM proactive_diagnostics
            WHERE {' AND '.join(clauses)}
            GROUP BY outcome
            ORDER BY cnt DESC
            """,
            tuple(params),
        ).fetchall()
    return {str(row["outcome"]): int(row["cnt"]) for row in rows}


def query_next_eligible(*, scope: str = "") -> list[dict[str, Any]]:
    """返回每个 target 最近一次记录里的 next_eligible_at，按时间升序。"""
    clauses = ["next_eligible_at IS NOT NULL"]
    params: list[Any] = []
    if scope:
        clauses.append("scope = ?")
        params.append(str(scope))
    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT scope, target, MAX(ts) AS latest_ts, next_eligible_at
            FROM proactive_diagnostics
            WHERE {' AND '.join(clauses)}
            GROUP BY scope, target
            ORDER BY next_eligible_at ASC
            LIMIT 100
            """,
            tuple(params),
        ).fetchall()
    return [
        {
            "scope": str(r["scope"] or ""),
            "target": str(r["target"] or ""),
            "latest_ts": float(r["latest_ts"] or 0),
            "next_eligible_at": float(r["next_eligible_at"] or 0),
        }
        for r in rows
    ]


def prune_old_entries(*, retention_days: int = _RETENTION_DAYS) -> int:
    cutoff = time.time() - max(1, int(retention_days)) * 86400
    with connect_sync() as conn:
        cursor = conn.execute(
            "DELETE FROM proactive_diagnostics WHERE ts < ?",
            (cutoff,),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


__all__ = [
    "record",
    "query_recent",
    "query_skip_reason_stats",
    "query_next_eligible",
    "prune_old_entries",
    "SKIP_DAILY_LIMIT", "SKIP_COOLDOWN", "SKIP_IDLE_NOT_REACHED",
    "SKIP_PROBABILITY", "SKIP_QUIET_HOUR", "SKIP_NO_CANDIDATE",
    "SKIP_LLM_FAILED", "SKIP_LLM_DECIDED", "SKIP_UNREAD",
    "SKIP_DISABLED", "SKIP_NO_PROFILE", "SKIP_OTHER", "OUTCOME_SENT",
]
