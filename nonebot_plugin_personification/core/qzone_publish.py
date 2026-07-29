from __future__ import annotations

import asyncio
import base64
import calendar
import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Iterable

from .db import connect_sync


QZONE_OPERATION_STATUSES = frozenset(
    {"reserved", "dispatching", "succeeded", "definite_failure", "unknown"}
)
_UNRESOLVED_STATUSES = ("reserved", "dispatching", "unknown")
_IMAGE_B64_RE = re.compile(r"\[IMAGE_B64\]([A-Za-z0-9+/=\r\n]+)\[/IMAGE_B64\]")
_VISIBLE_IMAGE_RE = re.compile(r"\[图片(?:·[^\]]+)?\]|\[表情\]|\[动画表情\]")
_UNSAFE_KEY_RE = re.compile(r"cookie|raw|response|base64|b64|credential|token|secret", re.IGNORECASE)


def _now_ts(now: Any) -> float:
    try:
        return float(now.timestamp())
    except Exception:
        try:
            return float(now)
        except Exception:
            return time.time()


def _period(now: Any) -> str:
    try:
        return str(now.strftime("%Y-%m"))
    except Exception:
        return time.strftime("%Y-%m", time.localtime(_now_ts(now)))


def _normalize_content(content: Any) -> tuple[str, list[str]]:
    image_hashes: list[str] = []

    def _remove_image(match: re.Match[str]) -> str:
        encoded = re.sub(r"\s+", "", match.group(1) or "")
        try:
            image = base64.b64decode(encoded, validate=True)
        except Exception:
            image = encoded.encode("ascii", errors="ignore")
        image_hashes.append(hashlib.sha256(image).hexdigest())
        return ""

    text = _IMAGE_B64_RE.sub(_remove_image, str(content or ""))
    text = _VISIBLE_IMAGE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text, image_hashes


def normalized_qzone_content_hash(content: Any) -> str:
    normalized, _ = _normalize_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) > 500:
            text = text[:500]
        text = re.sub(r"(?i)(p_skey|skey|cookie|token|secret)\s*[=:]\s*[^\s;,]+", r"\1=***", text)
        return text
    if isinstance(value, dict):
        return {
            str(key)[:80]: _safe_value(item, depth=depth + 1)
            for key, item in value.items()
            if not _UNSAFE_KEY_RE.search(str(key))
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:30]]
    return str(value)[:200]


def build_qzone_publish_payload(
    *,
    bot_id: str,
    kind: str,
    content: str,
    payload_identity: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    normalized_content, image_hashes = _normalize_content(content)
    safe_identity = _safe_value(payload_identity or {})
    content_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    payload = {
        "bot_id": str(bot_id or "").strip(),
        "kind": str(kind or "post").strip().lower()[:32] or "post",
        "content": _safe_value(normalized_content),
        "content_hash": content_hash,
        "image_hashes": image_hashes,
        "identity": safe_identity if isinstance(safe_identity, dict) else {},
    }
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    canonical = {
        **payload,
        "content": normalized_content,
    }
    canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return payload_hash, payload_json, payload


def _load_state(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value FROM kv_store WHERE namespace=? AND key=?",
        ("qzone_post_state", "__root__"),
    ).fetchone()
    if row is None:
        return {}
    try:
        state = json.loads(row["value"] or "{}")
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(conn: Any, state: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO kv_store(namespace, key, value, updated_at)
        VALUES (?, ?, ?, unixepoch('now'))
        ON CONFLICT(namespace, key)
        DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        ("qzone_post_state", "__root__", json.dumps(state, ensure_ascii=False)),
    )


def _compact_content(content: str) -> str:
    return qzone_content_preview(content, limit=200)


def qzone_content_preview(content: Any, *, limit: int = 200) -> str:
    normalized, image_hashes = _normalize_content(content)
    if image_hashes:
        normalized = f"{normalized} [配图]".strip()
    return normalized[:max(0, int(limit))]


def _remember_content(state: dict[str, Any], content: str, *, max_items: int = 12) -> None:
    compact = _compact_content(content)
    if not compact:
        return
    recent_raw = state.get("recent_contents")
    recent = list(recent_raw) if isinstance(recent_raw, list) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in recent + [compact]:
        text = str(item.get("content") if isinstance(item, dict) else item or "").strip()
        if text and text not in seen:
            seen.add(text)
            normalized.append(text)
    state["recent_contents"] = normalized[-max(1, int(max_items)) :]
    state["last_content"] = compact


def _mark_stale_dispatching_unknown(conn: Any, now_ts: float) -> None:
    conn.execute(
        """
        UPDATE qzone_publish_operations
        SET status='unknown', updated_at=?, completed_at=?, result_code='dispatch_lease_expired',
            resolution_source='lease_timeout', detail='{"reason":"dispatch_lease_expired"}'
        WHERE status='dispatching' AND lease_expires_at>0 AND lease_expires_at<=?
        """,
        (now_ts, now_ts, now_ts),
    )


def build_qzone_quota(
    *,
    state: Any,
    now: Any,
    monthly_limit: int,
    min_interval_hours: float,
) -> dict[str, Any]:
    period = _period(now)
    now_ts = _now_ts(now)
    state_dict = state if isinstance(state, dict) else {}
    state_confirmed = int(state_dict.get("count", 0) or 0) if state_dict.get("period") == period else 0
    confirmed = state_confirmed
    held = 0
    latest_held_at = 0.0
    try:
        with connect_sync() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _mark_stale_dispatching_unknown(conn, now_ts)
            usage = conn.execute(
                "SELECT confirmed_count FROM qzone_monthly_usage WHERE period=?",
                (period,),
            ).fetchone()
            if usage is not None:
                confirmed = max(confirmed, int(usage["confirmed_count"] or 0))
            active = conn.execute(
                """
                SELECT COUNT(*) AS held, MAX(CASE
                    WHEN dispatch_started_at>0 THEN dispatch_started_at ELSE reserved_at END) AS latest
                FROM qzone_publish_operations
                WHERE period=? AND (
                    status IN ('dispatching','unknown')
                    OR (status='reserved' AND lease_expires_at>?)
                )
                """,
                (period, now_ts),
            ).fetchone()
            held = int(active["held"] or 0)
            latest_held_at = float(active["latest"] or 0)
            conn.commit()
    except Exception:
        pass

    limit = max(0, int(monthly_limit if monthly_limit is not None else 0))
    available = max(0, limit - confirmed - held)
    days_in_month = calendar.monthrange(int(period[:4]), int(period[5:7]))[1]
    day_of_month = int(getattr(now, "day", time.localtime(now_ts).tm_mday) or 1)
    last_post_at = float(state_dict.get("last_post_at", 0) or 0)
    interval_seconds = max(0.0, float(min_interval_hours or 0)) * 3600
    last_effective = max(last_post_at, latest_held_at)
    next_eligible_at = last_effective + interval_seconds if last_effective and interval_seconds else 0.0
    return {
        "month": period,
        "confirmed": confirmed,
        "held": held,
        "available": available,
        "used": confirmed,
        "reserved": held,
        "limit": limit,
        "remaining": available,
        "days_in_month": days_in_month,
        "day_of_month": day_of_month,
        "days_left": max(1, days_in_month - day_of_month + 1),
        "min_interval_hours": float(min_interval_hours or 0),
        "last_post_at": last_post_at,
        "next_eligible_at": next_eligible_at,
    }


def _row_to_operation(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    operation = dict(row)
    for key in ("payload_json", "detail"):
        try:
            value = json.loads(operation.get(key) or "{}")
        except Exception:
            value = {}
        operation.pop(key, None)
        operation["payload" if key == "payload_json" else "detail"] = (
            value if isinstance(value, dict) else {}
        )
    return operation


def get_qzone_publish_operation(operation_id: str) -> dict[str, Any] | None:
    with connect_sync() as conn:
        row = conn.execute(
            "SELECT * FROM qzone_publish_operations WHERE operation_id=?",
            (str(operation_id or "").strip()[:96],),
        ).fetchone()
    return _row_to_operation(row) if row is not None else None


def list_qzone_publish_operations(
    *,
    bot_id: str = "",
    status: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if str(bot_id or "").strip():
        clauses.append("bot_id=?")
        params.append(str(bot_id).strip())
    if str(status or "").strip() in QZONE_OPERATION_STATUSES:
        clauses.append("status=?")
        params.append(str(status).strip())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(500, int(limit or 100))))
    with connect_sync() as conn:
        rows = conn.execute(
            f"SELECT * FROM qzone_publish_operations{where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_row_to_operation(row) for row in rows]


def resolve_qzone_publish_absent(
    *, operation_id: str, bot_id: str, now: Any = None
) -> dict[str, Any]:
    op_id = str(operation_id or "").strip()[:96]
    normalized_bot_id = str(bot_id or "").strip()
    resolved_at = _now_ts(now) if now is not None else time.time()
    with connect_sync() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, bot_id FROM qzone_publish_operations WHERE operation_id=?",
            (op_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return {"ok": False, "status": "not_found", "operation_id": op_id}
        if str(row["bot_id"] or "") != normalized_bot_id:
            conn.commit()
            return {"ok": False, "status": "bot_conflict", "operation_id": op_id}
        current_status = str(row["status"] or "unknown")
        if current_status != "unknown":
            conn.commit()
            return {
                "ok": current_status == "definite_failure",
                "status": current_status,
                "operation_id": op_id,
                "changed": False,
            }
        cursor = conn.execute(
            """
            UPDATE qzone_publish_operations
            SET status='definite_failure', updated_at=?, completed_at=?, lease_expires_at=0,
                result_code='admin_confirmed_absent', resolution_source='admin',
                detail='{"resolution":"confirmed_absent"}'
            WHERE operation_id=? AND status='unknown' AND bot_id=?
            """,
            (resolved_at, resolved_at, op_id, normalized_bot_id),
        )
        conn.commit()
    return {
        "ok": cursor.rowcount == 1,
        "status": "definite_failure",
        "operation_id": op_id,
        "changed": cursor.rowcount == 1,
    }


def record_historical_qzone_feed(
    *,
    bot_id: str,
    feed: dict[str, Any],
    occurred_at: Any,
    resolved_at: Any = None,
) -> dict[str, Any]:
    normalized_bot_id = str(bot_id or "").strip()
    owner_uin = str(feed.get("owner_uin") or "").strip()
    remote_id = str(feed.get("feed_id") or "").strip()[:160]
    content = str(feed.get("content") or "").strip()
    remote_time = float(feed.get("created_at") or _now_ts(occurred_at))
    if not normalized_bot_id or owner_uin != normalized_bot_id:
        return {"ok": False, "status": "bot_conflict"}
    if not remote_id or not content:
        return {"ok": False, "status": "invalid_feed"}
    payload_hash, payload_json, _ = build_qzone_publish_payload(
        bot_id=normalized_bot_id,
        kind="post",
        content=content,
        payload_identity={"remote_id": remote_id, "legacy_reconciliation": True},
    )
    operation_id = f"legacy-reconciled-{hashlib.sha256(f'{normalized_bot_id}:{remote_id}'.encode()).hexdigest()[:48]}"
    operation_period = _period(occurred_at)
    completed_at = _now_ts(resolved_at) if resolved_at is not None else time.time()
    with connect_sync() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT operation_id, status FROM qzone_publish_operations
            WHERE bot_id=? AND remote_id=? AND status='succeeded'
            LIMIT 1
            """,
            (normalized_bot_id, remote_id),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return {
                "ok": True,
                "status": "succeeded",
                "operation_id": str(existing["operation_id"]),
                "duplicate": True,
                "newly_committed": False,
            }
        conn.execute(
            """
            INSERT INTO qzone_publish_operations(
                operation_id, bot_id, period, kind, payload_hash, payload_json,
                status, fence_token, created_at, updated_at, reserved_at,
                dispatch_started_at, lease_expires_at, completed_at, remote_id,
                remote_time, result_code, resolution_source, detail
            ) VALUES (?, ?, ?, 'post', ?, ?, 'succeeded', 1, ?, ?, ?, ?, 0, ?, ?, ?,
                      'legacy_reconciled', 'admin_remote_feed', '{"resolution":"historical_feed"}')
            """,
            (
                operation_id,
                normalized_bot_id,
                operation_period,
                payload_hash,
                payload_json,
                completed_at,
                completed_at,
                completed_at,
                completed_at,
                completed_at,
                remote_id,
                remote_time,
            ),
        )
        state = _apply_success_state(
            conn,
            operation_period=operation_period,
            kind="post",
            content=content,
            event_time=remote_time,
        )
        conn.commit()
    return {
        "ok": True,
        "success": True,
        "status": "succeeded",
        "operation_id": operation_id,
        "remote_id": remote_id,
        "remote_time": remote_time,
        "newly_committed": True,
        "state": state,
    }


def reserve_qzone_publish(
    *,
    operation_id: str,
    now: Any,
    monthly_limit: int,
    min_interval_hours: float,
    kind: str = "post",
    bot_id: str = "",
    content: str = "",
    payload_identity: dict[str, Any] | None = None,
    force: bool = False,
    lease_seconds: int = 300,
) -> dict[str, Any]:
    op_id = str(operation_id or "").strip()[:96]
    if not op_id:
        raise ValueError("operation_id is required")
    normalized_bot_id = str(bot_id or "").strip()
    normalized_kind = str(kind or "post").strip().lower()[:32] or "post"
    payload_hash, payload_json, _ = build_qzone_publish_payload(
        bot_id=normalized_bot_id,
        kind=normalized_kind,
        content=content,
        payload_identity=payload_identity,
    )
    period = _period(now)
    now_ts = _now_ts(now)
    lease_until = now_ts + max(30, int(lease_seconds or 300))
    with connect_sync() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _mark_stale_dispatching_unknown(conn, now_ts)
        existing = conn.execute(
            "SELECT * FROM qzone_publish_operations WHERE operation_id=?",
            (op_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["bot_id"] or "") != normalized_bot_id
                or str(existing["kind"] or "") != normalized_kind
                or str(existing["payload_hash"] or "") != payload_hash
            ):
                conn.commit()
                return {"ok": False, "duplicate": True, "status": "payload_conflict", "operation_id": op_id}
            existing_status = str(existing["status"] or "unknown")
            if existing_status == "reserved" and float(existing["lease_expires_at"] or 0) <= now_ts:
                competing = conn.execute(
                    """
                    SELECT operation_id FROM qzone_publish_operations
                    WHERE operation_id<>? AND bot_id=? AND kind=? AND payload_hash=?
                      AND (
                        status IN ('dispatching','unknown')
                        OR (status='reserved' AND lease_expires_at>?)
                      )
                    LIMIT 1
                    """,
                    (op_id, normalized_bot_id, normalized_kind, payload_hash, now_ts),
                ).fetchone()
                if competing is not None:
                    conn.commit()
                    return {
                        "ok": False,
                        "status": "unresolved_payload",
                        "conflicting_operation_id": str(competing["operation_id"]),
                    }
                next_fence = int(existing["fence_token"] or 0) + 1
                conn.execute(
                    """
                    UPDATE qzone_publish_operations
                    SET fence_token=?, reserved_at=?, updated_at=?, lease_expires_at=?,
                        result_code='reserved_reclaimed', resolution_source='coordinator', detail='{}'
                    WHERE operation_id=? AND status='reserved' AND fence_token=?
                    """,
                    (next_fence, now_ts, now_ts, lease_until, op_id, int(existing["fence_token"] or 0)),
                )
                conn.commit()
                return {
                    "ok": True,
                    "status": "reserved",
                    "operation_id": op_id,
                    "fence_token": next_fence,
                    "reclaimed": True,
                }
            conn.commit()
            return {
                "ok": existing_status == "succeeded",
                "success": existing_status == "succeeded",
                "duplicate": True,
                "status": existing_status,
                "operation_id": op_id,
                "fence_token": int(existing["fence_token"] or 0),
                "newly_committed": False,
            }

        unresolved = conn.execute(
            """
            SELECT operation_id, status FROM qzone_publish_operations
            WHERE bot_id=? AND kind=? AND payload_hash=?
              AND (
                status IN ('dispatching','unknown')
                OR (status='reserved' AND lease_expires_at>?)
              )
            LIMIT 1
            """,
            (normalized_bot_id, normalized_kind, payload_hash, now_ts),
        ).fetchone()
        if unresolved is not None:
            conn.commit()
            return {
                "ok": False,
                "status": "unresolved_payload",
                "conflicting_operation_id": str(unresolved["operation_id"]),
                "conflicting_status": str(unresolved["status"]),
            }

        usage = conn.execute(
            "SELECT confirmed_count FROM qzone_monthly_usage WHERE period=?",
            (period,),
        ).fetchone()
        state = _load_state(conn)
        confirmed = int(usage["confirmed_count"] or 0) if usage is not None else 0
        if state.get("period") == period:
            confirmed = max(confirmed, int(state.get("count", 0) or 0))
        active = conn.execute(
            """
            SELECT COUNT(*) AS held, MAX(CASE
                WHEN dispatch_started_at>0 THEN dispatch_started_at ELSE reserved_at END) AS latest
            FROM qzone_publish_operations
            WHERE period=? AND (
                status IN ('dispatching','unknown')
                OR (status='reserved' AND lease_expires_at>?)
            )
            """,
            (period, now_ts),
        ).fetchone()
        held = int(active["held"] or 0)
        limit = max(0, int(monthly_limit if monthly_limit is not None else 0))
        if not force and confirmed + held >= limit:
            conn.commit()
            return {
                "ok": False,
                "status": "quota_blocked",
                "confirmed": confirmed,
                "held": held,
                "available": max(0, limit - confirmed - held),
                "limit": limit,
            }
        interval_seconds = max(0.0, float(min_interval_hours or 0)) * 3600
        latest = float(active["latest"] or 0)
        last_effective = max(float(state.get("last_post_at", 0) or 0), latest)
        if not force and interval_seconds and last_effective and now_ts - last_effective < interval_seconds:
            conn.commit()
            return {
                "ok": False,
                "status": "interval_blocked",
                "next_eligible_at": last_effective + interval_seconds,
            }
        conn.execute(
            """
            INSERT INTO qzone_publish_operations(
                operation_id, bot_id, period, kind, payload_hash, payload_json,
                status, fence_token, created_at, updated_at, reserved_at,
                dispatch_started_at, lease_expires_at, completed_at, remote_id,
                remote_time, result_code, resolution_source, detail
            ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?, ?, 0, ?, 0, '', 0, 'reserved', 'coordinator', '{}')
            """,
            (
                op_id,
                normalized_bot_id,
                period,
                normalized_kind,
                payload_hash,
                payload_json,
                now_ts,
                now_ts,
                now_ts,
                lease_until,
            ),
        )
        conn.commit()
    return {"ok": True, "status": "reserved", "operation_id": op_id, "fence_token": 1}


def _claim_dispatch(
    *, operation_id: str, fence_token: int, now_ts: float, lease_seconds: int
) -> bool:
    with connect_sync() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE qzone_publish_operations
            SET status='dispatching', dispatch_started_at=?, updated_at=?, lease_expires_at=?,
                result_code='dispatching', resolution_source='coordinator', detail='{}'
            WHERE operation_id=? AND status='reserved' AND fence_token=? AND lease_expires_at>?
            """,
            (
                now_ts,
                now_ts,
                now_ts + max(30, int(lease_seconds or 300)),
                operation_id,
                int(fence_token),
                now_ts,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1


def _safe_detail_json(detail: Any) -> str:
    safe = _safe_value(detail if isinstance(detail, dict) else {"message": str(detail or "")})
    return json.dumps(safe if isinstance(safe, dict) else {}, ensure_ascii=False, separators=(",", ":"))


def _apply_success_state(
    conn: Any,
    *,
    operation_period: str,
    kind: str,
    content: str,
    event_time: float,
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO qzone_monthly_usage(period, confirmed_count, forward_count, updated_at)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(period) DO UPDATE SET
            confirmed_count=qzone_monthly_usage.confirmed_count+1,
            forward_count=qzone_monthly_usage.forward_count+excluded.forward_count,
            updated_at=excluded.updated_at
        """,
        (operation_period, 1 if kind == "forward" else 0, event_time),
    )
    state = _load_state(conn)
    current_period = str(state.get("period") or "")
    if not current_period or current_period < operation_period:
        state = {
            "period": operation_period,
            "count": 0,
            "forward_count": 0,
            "last_post_at": float(state.get("last_post_at", 0) or 0),
            "last_content": str(state.get("last_content", "") or ""),
            "recent_contents": list(state.get("recent_contents", []))
            if isinstance(state.get("recent_contents"), list)
            else [],
        }
        current_period = operation_period
    if current_period == operation_period:
        state["count"] = int(state.get("count", 0) or 0) + 1
        if kind == "forward":
            state["forward_count"] = int(state.get("forward_count", 0) or 0) + 1
    previous_last_post_at = float(state.get("last_post_at", 0) or 0)
    event_time = float(event_time or 0)
    if event_time >= previous_last_post_at:
        state["last_post_at"] = event_time
        _remember_content(state, content)
    else:
        compact = _compact_content(content)
        recent = list(state.get("recent_contents", [])) if isinstance(state.get("recent_contents"), list) else []
        if compact and compact not in recent:
            state["recent_contents"] = ([compact] + recent)[-12:]
    _save_state(conn, state)
    return state


def _finalize_dispatch(
    *,
    operation_id: str,
    fence_token: int,
    content: str,
    now_ts: float,
    status: str,
    result_code: str,
    resolution_source: str,
    detail: Any,
    remote_id: str = "",
    remote_time: float = 0,
) -> dict[str, Any]:
    if status not in {"succeeded", "definite_failure", "unknown"}:
        raise ValueError("invalid qzone final status")
    with connect_sync() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM qzone_publish_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise ValueError("qzone publish operation does not exist")
        if str(row["status"]) != "dispatching" or int(row["fence_token"] or 0) != int(fence_token):
            conn.commit()
            current = _row_to_operation(row)
            return {
                "success": current.get("status") == "succeeded",
                "status": current.get("status", "unknown"),
                "duplicate": True,
                "newly_committed": False,
                "operation_id": operation_id,
                "operation": current,
            }
        if status == "succeeded" and str(remote_id or ""):
            claimed_remote = conn.execute(
                """
                SELECT operation_id FROM qzone_publish_operations
                WHERE operation_id<>? AND bot_id=? AND remote_id=? AND status='succeeded'
                LIMIT 1
                """,
                (operation_id, str(row["bot_id"] or ""), str(remote_id)[:160]),
            ).fetchone()
            if claimed_remote is not None:
                status = "unknown"
                result_code = "remote_id_conflict"
                resolution_source = "coordinator_conflict"
                detail = {"reason": "remote_id_already_claimed"}
                remote_id = ""
                remote_time = 0
        event_time = float(remote_time or now_ts)
        cursor = conn.execute(
            """
            UPDATE qzone_publish_operations
            SET status=?, updated_at=?, completed_at=?, lease_expires_at=0,
                remote_id=?, remote_time=?, result_code=?, resolution_source=?, detail=?
            WHERE operation_id=? AND status='dispatching' AND fence_token=?
            """,
            (
                status,
                now_ts,
                now_ts,
                str(remote_id or "")[:160],
                float(remote_time or 0),
                str(result_code or "")[:64],
                str(resolution_source or "coordinator")[:64],
                _safe_detail_json(detail),
                operation_id,
                int(fence_token),
            ),
        )
        state = _load_state(conn)
        newly_committed = cursor.rowcount == 1 and status == "succeeded"
        if newly_committed:
            state = _apply_success_state(
                conn,
                operation_period=str(row["period"]),
                kind=str(row["kind"]),
                content=content,
                event_time=event_time,
            )
        conn.commit()
    return {
        "success": status == "succeeded",
        "status": status,
        "newly_committed": newly_committed,
        "operation_id": operation_id,
        "state": state,
        "result_code": str(result_code or ""),
    }


def _normalize_write_result(result: Any) -> dict[str, Any]:
    if is_dataclass(result):
        data = asdict(result)
    elif isinstance(result, dict):
        data = dict(result)
    elif isinstance(result, tuple) and len(result) >= 2:
        success, message = bool(result[0]), str(result[1] or "")
        data = {
            "status": "succeeded"
            if success
            else "unknown"
            if message.startswith("outcome_unknown")
            else "definite_failure",
            "message": message,
        }
    else:
        data = {"status": "unknown", "message": "invalid_publish_result"}
    status = str(data.get("status") or "").strip().lower()
    if status not in {"succeeded", "definite_failure", "unknown"}:
        if bool(data.get("success")):
            status = "succeeded"
        else:
            status = "unknown"
    safe_detail = _safe_value(data.get("detail") if isinstance(data.get("detail"), dict) else {})
    return {
        "status": status,
        "message": str(data.get("message") or "")[:300],
        "result_code": str(data.get("result_code") or status)[:64],
        "remote_id": str(data.get("remote_id") or "")[:160],
        "remote_time": float(data.get("remote_time") or 0),
        "detail": safe_detail if isinstance(safe_detail, dict) else {},
    }


async def coordinated_qzone_publish(
    *,
    operation_id: str,
    content: str,
    now: Any,
    monthly_limit: int,
    min_interval_hours: float,
    kind: str,
    publish: Callable[[], Any],
    bot_id: str = "",
    payload_identity: dict[str, Any] | None = None,
    force: bool = False,
    lease_seconds: int = 300,
) -> dict[str, Any]:
    reservation = await asyncio.to_thread(
        reserve_qzone_publish,
        operation_id=operation_id,
        now=now,
        monthly_limit=monthly_limit,
        min_interval_hours=min_interval_hours,
        kind=kind,
        bot_id=bot_id,
        content=content,
        payload_identity=payload_identity,
        force=force,
        lease_seconds=lease_seconds,
    )
    if reservation.get("duplicate") or not reservation.get("ok"):
        return {
            **reservation,
            "success": reservation.get("status") == "succeeded",
            "newly_committed": False,
        }
    fence_token = int(reservation["fence_token"])
    dispatch_now = _now_ts(now)
    claimed = await asyncio.to_thread(
        _claim_dispatch,
        operation_id=operation_id,
        fence_token=fence_token,
        now_ts=dispatch_now,
        lease_seconds=lease_seconds,
    )
    if not claimed:
        current = await asyncio.to_thread(get_qzone_publish_operation, operation_id)
        return {
            "success": bool(current and current.get("status") == "succeeded"),
            "status": str((current or {}).get("status") or "unknown"),
            "duplicate": True,
            "newly_committed": False,
            "operation_id": operation_id,
        }
    cancelled: asyncio.CancelledError | None = None
    try:
        raw_result = await publish()
    except asyncio.CancelledError as exc:
        cancelled = exc
        normalized = {
            "status": "unknown",
            "message": type(exc).__name__,
            "result_code": "dispatch_cancelled",
            "remote_id": "",
            "remote_time": 0.0,
            "detail": {"exception_type": type(exc).__name__},
        }
    except Exception as exc:
        normalized = {
            "status": "unknown",
            "message": type(exc).__name__,
            "result_code": "dispatch_exception",
            "remote_id": "",
            "remote_time": 0.0,
            "detail": {"exception_type": type(exc).__name__},
        }
    else:
        normalized = _normalize_write_result(raw_result)
    finalized = await asyncio.to_thread(
        _finalize_dispatch,
        operation_id=operation_id,
        fence_token=fence_token,
        content=content,
        now_ts=_now_ts(now),
        status=normalized["status"],
        result_code=normalized["result_code"],
        resolution_source="provider_response",
        detail=normalized["detail"],
        remote_id=normalized["remote_id"],
        remote_time=normalized["remote_time"],
    )
    finalized["message"] = normalized["message"]
    finalized["publish_detail"] = normalized["detail"]
    finalized["fence_token"] = fence_token
    if cancelled is not None:
        raise cancelled
    return finalized


def finalize_qzone_publish(
    *,
    operation_id: str,
    content: str,
    now: Any,
    outcome: str,
    detail: str = "",
    fence_token: int | None = None,
) -> dict[str, Any]:
    operation = get_qzone_publish_operation(operation_id)
    if operation is None:
        raise ValueError("qzone publish operation does not exist")
    token = int(fence_token or operation.get("fence_token") or 0)
    if operation.get("status") == "reserved":
        _claim_dispatch(
            operation_id=operation_id,
            fence_token=token,
            now_ts=_now_ts(now),
            lease_seconds=300,
        )
    normalized = str(outcome or "").strip().lower()
    status = "succeeded" if normalized in {"success", "succeeded"} else "unknown" if normalized == "unknown" else "definite_failure"
    return _finalize_dispatch(
        operation_id=operation_id,
        fence_token=token,
        content=content,
        now_ts=_now_ts(now),
        status=status,
        result_code=normalized or status,
        resolution_source="legacy_finalize",
        detail={"message": str(detail or "")[:300]},
    )


def record_qzone_post(content: str, *, now: Any, kind: str = "post", bot_id: str = "") -> dict[str, Any]:
    operation_id = f"legacy-{uuid.uuid4().hex}"
    reserved = reserve_qzone_publish(
        operation_id=operation_id,
        now=now,
        monthly_limit=0,
        min_interval_hours=0,
        kind=kind,
        bot_id=bot_id,
        content=content,
        force=True,
    )
    return finalize_qzone_publish(
        operation_id=operation_id,
        content=content,
        now=now,
        outcome="succeeded",
        fence_token=int(reserved["fence_token"]),
    ).get("state", {})


def reconcile_qzone_publish_operation(
    *,
    operation_id: str,
    bot_id: str,
    feeds: Iterable[dict[str, Any]],
    window_seconds: float = 600.0,
    now: Any = None,
) -> dict[str, Any]:
    op_id = str(operation_id or "").strip()[:96]
    normalized_bot_id = str(bot_id or "").strip()
    operation = get_qzone_publish_operation(op_id)
    if operation is None:
        return {"ok": False, "status": "not_found", "operation_id": op_id}
    if operation.get("status") != "unknown":
        return {
            "ok": operation.get("status") == "succeeded",
            "status": operation.get("status"),
            "operation_id": op_id,
            "changed": False,
        }
    if str(operation.get("bot_id") or "") != normalized_bot_id:
        return {"ok": False, "status": "bot_conflict", "operation_id": op_id, "changed": False}
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    expected_hash = str(payload.get("content_hash") or "")
    expected_identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    operation_kind = str(operation.get("kind") or "post")
    anchor = float(operation.get("dispatch_started_at") or operation.get("reserved_at") or 0)
    window = max(0.0, float(window_seconds or 0))
    matches: list[dict[str, Any]] = []
    for feed in feeds:
        if not isinstance(feed, dict):
            continue
        owner = str(feed.get("owner_uin") or "").strip()
        try:
            created_at = float(feed.get("created_at") or 0)
        except Exception:
            created_at = 0.0
        if owner != normalized_bot_id or not created_at or abs(created_at - anchor) > window:
            continue
        if normalized_qzone_content_hash(feed.get("content") or "") != expected_hash:
            continue
        if operation_kind == "forward":
            raw = feed.get("raw") if isinstance(feed.get("raw"), dict) else {}
            source = feed.get("forward_source") if isinstance(feed.get("forward_source"), dict) else {}
            candidate_source = {
                "owner_uin": str(source.get("owner_uin") or raw.get("rt_uin") or raw.get("sourceUin") or ""),
                "feed_id": str(source.get("feed_id") or raw.get("rt_tid") or raw.get("sourceTid") or ""),
                "topic_id": str(source.get("topic_id") or raw.get("rt_topicid") or raw.get("sourceTopicId") or ""),
                "appid": str(source.get("appid") or raw.get("rt_appid") or raw.get("sourceAppid") or ""),
            }
            required = {
                key: str(expected_identity.get(key) or "")
                for key in ("owner_uin", "feed_id", "topic_id", "appid")
                if str(expected_identity.get(key) or "")
            }
            if not required or any(candidate_source.get(key) != value for key, value in required.items()):
                continue
        matches.append(feed)
    if len(matches) != 1:
        return {
            "ok": False,
            "status": "unknown",
            "operation_id": op_id,
            "changed": False,
            "match_count": len(matches),
        }

    match = matches[0]
    resolved_at = _now_ts(now) if now is not None else time.time()
    remote_time = float(match.get("created_at") or 0)
    with connect_sync() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM qzone_publish_operations WHERE operation_id=? AND status='unknown'",
            (op_id,),
        ).fetchone()
        if row is None:
            current_row = conn.execute(
                "SELECT * FROM qzone_publish_operations WHERE operation_id=?",
                (op_id,),
            ).fetchone()
            current = _row_to_operation(current_row) if current_row is not None else {}
            conn.commit()
            return {
                "ok": current.get("status") == "succeeded",
                "status": current.get("status", "unknown"),
                "operation_id": op_id,
                "changed": False,
            }
        cursor = conn.execute(
            """
            UPDATE qzone_publish_operations
            SET status='succeeded', updated_at=?, completed_at=?, lease_expires_at=0,
                remote_id=?, remote_time=?, result_code='reconciled_exact_match',
                resolution_source='remote_self_feed', detail='{"match":"exact_content_hash"}'
            WHERE operation_id=? AND status='unknown'
            """,
            (
                resolved_at,
                resolved_at,
                str(match.get("feed_id") or "")[:160],
                remote_time,
                op_id,
            ),
        )
        state = _load_state(conn)
        if cursor.rowcount == 1:
            state = _apply_success_state(
                conn,
                operation_period=str(row["period"]),
                kind=str(row["kind"]),
                content=str(payload.get("content") or ""),
                event_time=remote_time or resolved_at,
            )
        conn.commit()
    return {
        "ok": True,
        "success": True,
        "status": "succeeded",
        "operation_id": op_id,
        "changed": True,
        "newly_committed": True,
        "remote_id": str(match.get("feed_id") or ""),
        "remote_time": remote_time,
        "state": state,
    }


async def reconcile_qzone_publish_from_self_feed(
    *,
    operation_id: str,
    bot_id: str,
    qzone_social_service: Any,
    window_seconds: float = 600.0,
    feed_count: int = 40,
) -> dict[str, Any]:
    operation = await asyncio.to_thread(get_qzone_publish_operation, operation_id)
    if operation is None:
        return {"ok": False, "status": "not_found", "operation_id": str(operation_id or "")[:96]}
    if operation.get("status") != "unknown":
        return {
            "ok": operation.get("status") == "succeeded",
            "status": operation.get("status"),
            "operation_id": str(operation_id or "")[:96],
            "changed": False,
        }
    try:
        ok, _message, feeds = await qzone_social_service.fetch_user_feeds(
            target_uin=str(bot_id or ""),
            bot_id=str(bot_id or ""),
            count=max(1, min(100, int(feed_count or 40))),
            include_comments=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "unknown",
            "operation_id": str(operation_id or "")[:96],
            "changed": False,
            "result_code": "self_feed_fetch_exception",
            "exception_type": type(exc).__name__,
        }
    if not ok:
        return {
            "ok": False,
            "status": "unknown",
            "operation_id": str(operation_id or "")[:96],
            "changed": False,
            "result_code": "self_feed_fetch_failed",
        }
    return await asyncio.to_thread(
        reconcile_qzone_publish_operation,
        operation_id=operation_id,
        bot_id=bot_id,
        feeds=feeds,
        window_seconds=window_seconds,
    )
