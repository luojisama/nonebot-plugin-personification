from __future__ import annotations

import re
import time
from typing import Any


TIME_HINT_RE = re.compile(r"(今天|昨天|前天|刚才|最近|上次|之前|前几天|本周|上周|本月|去年|\d+天前|\d+个月前)")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def now_ts() -> float:
    return time.time()


def build_time_hint(created_at: float) -> str:
    if not created_at:
        return ""
    delta = max(0.0, now_ts() - created_at)
    if delta < 3600:
        return "刚刚"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}天前"
    return f"{int(delta // (86400 * 30))}个月前"


def query_looks_latest(query: str) -> bool:
    lowered = str(query or "").strip().lower()
    return any(token in lowered for token in ("最新", "现在", "当前", "今天", "刚刚", "最近"))


def query_looks_ambiguous(query: str) -> bool:
    normalized = str(query or "").strip()
    if not normalized:
        return False
    if len(normalized) <= 4:
        return True
    return any(token in normalized for token in ("是谁", "哪个", "哪部", "什么番", "这个", "上次"))


def time_hint_score(query: str, created_at: float, expires_at: float, time_sensitivity: str) -> float:
    if not query or not TIME_HINT_RE.search(str(query or "")):
        return 0.0
    if expires_at and expires_at <= now_ts():
        return -0.35
    age_hours = max(0.0, (now_ts() - float(created_at or 0.0)) / 3600.0)
    if time_sensitivity in {"high", "strong", "hot"}:
        return max(0.0, 0.45 - min(age_hours, 72.0) / 160.0)
    return max(0.0, 0.28 - min(age_hours, 168.0) / 600.0)


def group_scope_delta(candidate_group_id: str, requested_group_id: str, cross_group_allowed: bool) -> float:
    if not requested_group_id:
        return 0.0
    if candidate_group_id == requested_group_id:
        return 0.18
    if not candidate_group_id:
        return -0.04
    return -0.32 if not cross_group_allowed else -0.14


def rank_memory_payload(
    payload: dict[str, Any],
    *,
    query: str,
    base_score: float,
    requested_group_id: str,
    requested_user_id: str,
) -> float:
    confidence = safe_float(payload.get("confidence", 0.5), 0.5)
    stability = safe_float(payload.get("stability", 0.4), 0.4)
    salience = safe_float(payload.get("salience", 0.4), 0.4)
    reinforcement_count = float(int(payload.get("reinforcement_count", 0) or 0))
    access_count = float(int(payload.get("access_count", 0) or 0))
    revision = float(int(payload.get("revision", 1) or 1))
    tone_risk = safe_float(payload.get("tone_risk", 0.0), 0.0)
    irony_risk = safe_float(payload.get("irony_risk", 0.0), 0.0)
    superseded_by = str(payload.get("superseded_by", "") or "")
    expires_at = safe_float(payload.get("expires_at", 0.0), 0.0)
    created_at = safe_float(payload.get("time_created", 0.0), 0.0)
    last_accessed_at = safe_float(payload.get("last_accessed_at", 0.0), 0.0)
    time_sensitivity = str(payload.get("time_sensitivity", "normal") or "normal")
    source_kind = str(payload.get("source_kind", "") or "")
    crystal_status = str(payload.get("status", "") or payload.get("crystal_status", "") or "")
    cross_group_allowed = bool(payload.get("cross_group_allowed", False))
    payload_group_id = str(payload.get("group_id", "") or "")
    payload_user_id = str(payload.get("user_id", "") or "")

    score = float(base_score)
    score += confidence * 0.28
    score += stability * 0.22
    score += salience * 0.18
    score += min(reinforcement_count, 8.0) * 0.018
    score += min(access_count, 12.0) * 0.012
    score += min(revision, 10.0) * 0.004
    score -= tone_risk * 0.12
    score -= irony_risk * 0.12
    score += group_scope_delta(payload_group_id, requested_group_id, cross_group_allowed)
    if requested_user_id and payload_user_id and payload_user_id == requested_user_id:
        score += 0.10
    if source_kind == "crystal":
        score -= 0.12
        if crystal_status in {"unreviewed", "candidate"}:
            score -= 0.08
        if crystal_status in {"stale", "staled"}:
            score -= 0.18
    if superseded_by:
        score -= 0.42
    if expires_at and expires_at <= now_ts():
        score -= 0.50
    if time_sensitivity in {"high", "strong", "hot"}:
        age_hours = max(0.0, (now_ts() - created_at) / 3600.0) if created_at else 0.0
        score -= min(age_hours / 240.0, 0.24)
    if last_accessed_at:
        score += max(0.0, 0.08 - ((now_ts() - last_accessed_at) / 86400.0) * 0.01)
    if query_looks_latest(query) and time_sensitivity in {"high", "strong", "hot"}:
        score -= 0.10
    return round(score, 6)
