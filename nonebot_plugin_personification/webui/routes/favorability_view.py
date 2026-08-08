from __future__ import annotations

import math
from typing import Any


_EVENT_LABELS: dict[str, str] = {
    "group_good_atmosphere": "群聊氛围良好",
    "user_interesting_chat": "有趣互动",
    "user_reply_interaction": "回复互动",
    "user_perm_blacklist": "加入永久黑名单",
    "user_perm_blacklist_removed": "移出永久黑名单",
    "manual_adjust": "管理员手动调整",
    "daily_decay": "每日关系衰减",
    "baseline_migration": "默认基线迁移",
}

_STATUS_LABELS: dict[str, str] = {
    "applied": "已生效",
    "capped": "已达每日上限",
    "clamped": "已触及分值边界",
    "disabled": "功能关闭",
    "invalid": "无效事件",
    "duplicate": "重复事件已忽略",
}


def _favorability_service(runtime: Any) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "favorability_service", None)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type", "") or "").strip()
    status = str(event.get("status", "") or "").strip()
    delta = round(_safe_float(event.get("delta", 0.0), 0.0), 2)
    requested_delta = round(_safe_float(event.get("requested_delta", delta), delta), 2)
    return {
        "type": event_type,
        "label": _EVENT_LABELS.get(event_type, "其他好感事件"),
        "status": status,
        "status_label": _STATUS_LABELS.get(status, "未知状态"),
        "delta": delta,
        "requested_delta": requested_delta,
        "old": round(_safe_float(event.get("old", 0.0), 0.0), 2),
        "new": round(_safe_float(event.get("new", 0.0), 0.0), 2),
        "timestamp": _safe_int(event.get("timestamp", 0), 0),
        "date": str(event.get("date", "") or ""),
        "reason": str(event.get("reason", "") or ""),
        "actor": str(event.get("actor", "") or ""),
        "group_id": str(event.get("group_id", "") or ""),
        "capped": bool(event.get("capped", False)),
    }


def serialize_favorability(
    runtime: Any,
    key: str,
    *,
    scope: str,
    include_events: bool = True,
) -> dict[str, Any]:
    service = _favorability_service(runtime)
    profile_key = str(key or "").strip()
    if not profile_key:
        return {
            "available": False,
            "enabled": False,
            "exists": False,
            "key": "",
            "scope": scope,
            "reason": "empty_key",
        }
    if service is None or not hasattr(service, "peek_user_data"):
        return {
            "available": False,
            "enabled": False,
            "exists": False,
            "key": profile_key,
            "scope": scope,
            "reason": "favorability_service_missing",
        }
    try:
        enabled = bool(service.enabled)
    except Exception:
        enabled = False
    try:
        stored_profile = service.peek_user_data(profile_key)
    except Exception as exc:
        return {
            "available": False,
            "enabled": enabled,
            "exists": False,
            "key": profile_key,
            "scope": scope,
            "reason": str(exc),
        }
    exists = isinstance(stored_profile, dict)
    profile = dict(stored_profile) if exists else {}
    if exists:
        default_score = 0.0
    else:
        try:
            default_score = _safe_float(service.default_score(profile_key), 0.0)
        except Exception:
            default_score = 0.0
    score = round(_safe_float(profile.get("favorability", default_score), default_score), 2)
    try:
        level = str(service.get_level_name(score) or "")
    except Exception:
        level = ""
    events_raw = profile.get("favorability_events")
    events = [_event_view(e) for e in events_raw if isinstance(e, dict)] if isinstance(events_raw, list) else []
    latest_event = events[-1] if events else None
    try:
        today = str(service.current_date() or "")
    except Exception:
        today = ""
    positive_date = str(profile.get("daily_positive_date", "") or "")
    negative_date = str(profile.get("daily_negative_date", "") or "")
    group_daily_date = str(profile.get("last_update", "") or "")
    interesting_date = str(profile.get("last_interesting_date", "") or "")
    return {
        "available": True,
        "enabled": enabled,
        "exists": exists,
        "key": profile_key,
        "scope": scope,
        "score": score,
        "level": level,
        "is_perm_blacklisted": bool(profile.get("is_perm_blacklisted", False)),
        "blacklist_count": _safe_int(profile.get("blacklist_count", 0), 0),
        "daily_positive_count": round(
            _safe_float(profile.get("daily_positive_count", 0.0), 0.0) if positive_date == today else 0.0,
            2,
        ),
        "daily_negative_count": round(
            _safe_float(profile.get("daily_negative_count", 0.0), 0.0) if negative_date == today else 0.0,
            2,
        ),
        "daily_fav_count": round(
            _safe_float(profile.get("daily_fav_count", 0.0), 0.0) if group_daily_date == today else 0.0,
            2,
        ),
        "daily_interesting_count": round(
            _safe_float(profile.get("daily_interesting_count", 0.0), 0.0)
            if interesting_date == today
            else 0.0,
            2,
        ),
        "daily_positive_date": positive_date,
        "daily_negative_date": negative_date,
        "today": today,
        "last_event_at": _safe_int(profile.get("last_favorability_event_at", 0), 0),
        "last_event_date": str(profile.get("last_favorability_event_date", "") or ""),
        "last_relationship_activity_at": _safe_int(profile.get("last_relationship_activity_at", 0), 0),
        "revision": _safe_int(profile.get("revision", 0), 0),
        "updated_at": _safe_int(profile.get("updated_at", 0), 0),
        "source": str(profile.get("source", "") or ("personification" if exists else "virtual_default")),
        "latest_event": latest_event,
        "events": list(reversed(events[-12:])) if include_events else [],
    }
