from __future__ import annotations

import json
import time
from typing import Any

from .db import connect_sync


_RETENTION_DAYS = 90


def record(
    *,
    action: str,
    qq: str = "",
    device_id: str = "",
    target: str = "",
    ip_hash: str = "",
    detail: dict[str, Any] | None = None,
    outcome: str = "ok",
) -> None:
    """记录一条 WebUI 审计日志。即使失败也不抛异常（不要影响主流程）。"""
    try:
        payload = json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":"))
        with connect_sync() as conn:
            conn.execute(
                """
                INSERT INTO webui_audit_log
                    (ts, action, qq, device_id, target, ip_hash, detail, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    str(action or "")[:64],
                    str(qq or "")[:32],
                    str(device_id or "")[:64],
                    str(target or "")[:128],
                    str(ip_hash or "")[:32],
                    payload[:2000],
                    str(outcome or "ok")[:16],
                ),
            )
            conn.commit()
    except Exception:
        # 审计日志失败时不应影响业务，吞掉
        return


def query_recent(
    *,
    limit: int = 200,
    action: str = "",
    qq: str = "",
    before_ts: float = 0.0,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if action:
        clauses.append("action = ?")
        params.append(str(action))
    if qq:
        clauses.append("qq = ?")
        params.append(str(qq))
    if before_ts and before_ts > 0:
        clauses.append("ts < ?")
        params.append(float(before_ts))
    params.append(max(1, min(int(limit), 500)))
    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT id, ts, action, qq, device_id, target, ip_hash, detail, outcome
            FROM webui_audit_log
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
            "action": str(row["action"] or ""),
            "qq": str(row["qq"] or ""),
            "device_id": str(row["device_id"] or ""),
            "target": str(row["target"] or ""),
            "ip_hash": str(row["ip_hash"] or ""),
            "detail": detail if isinstance(detail, dict) else {},
            "outcome": str(row["outcome"] or "ok"),
        })
    return out


def prune_old_entries(*, retention_days: int = _RETENTION_DAYS) -> int:
    """删除 retention_days 之前的旧记录，返回删除条数。"""
    cutoff = time.time() - max(1, int(retention_days)) * 86400
    with connect_sync() as conn:
        cursor = conn.execute(
            "DELETE FROM webui_audit_log WHERE ts < ?",
            (cutoff,),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


__all__ = ["record", "query_recent", "prune_old_entries"]
