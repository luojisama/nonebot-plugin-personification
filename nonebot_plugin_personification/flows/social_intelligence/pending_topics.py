"""pending_topics 持久化层。

存 kv_store 命名空间 `social_pending_topics`，结构：
{
  "<topic_id>": {
    "user_id": "...",
    "topic": "下周去上海出差",
    "raw_quote": "我下周三去上海一趟",
    "time_hint_ts": 1716000000.0,
    "created_at": 1715000000.0,
    "followed_up_at": 0.0,
    "skipped": false
  }
}

topic_id 用 sha1(user_id + raw_quote)[:12]，避免同用户同句话重复抽取。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

_NAMESPACE = "social_pending_topics"


def _load() -> dict[str, Any]:
    try:
        from ...core.data_store import get_data_store

        data = get_data_store().load_sync(_NAMESPACE) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        from ...core.data_store import get_data_store

        get_data_store().save_sync(_NAMESPACE, data)
    except Exception:
        return


def _topic_id(user_id: str, raw_quote: str) -> str:
    h = hashlib.sha1(f"{user_id}|{raw_quote}".encode("utf-8")).hexdigest()
    return h[:12]


def add_pending_topic(
    *,
    user_id: str,
    topic: str,
    raw_quote: str,
    time_hint_ts: float,
    now: float | None = None,
) -> str | None:
    """添加一条 pending topic；已存在的同 topic_id 不覆盖。返回 topic_id。"""
    uid = str(user_id).strip()
    quote = str(raw_quote).strip()
    if not uid or not quote:
        return None
    tid = _topic_id(uid, quote)
    data = _load()
    if tid in data:
        return tid  # 幂等：已经存在不动
    data[tid] = {
        "user_id": uid,
        "topic": str(topic).strip(),
        "raw_quote": quote,
        "time_hint_ts": float(time_hint_ts or 0),
        "created_at": float(now if now is not None else time.time()),
        "followed_up_at": 0.0,
        "skipped": False,
    }
    _prune_old(data)
    _save(data)
    return tid


def list_pending_topics() -> list[dict[str, Any]]:
    data = _load()
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for tid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        item["topic_id"] = tid
        out.append(item)
    return out


def find_due_topics(
    *, now: float | None = None, window_seconds: float = 86400.0
) -> list[dict[str, Any]]:
    """找出"该跟进"的 topic：time_hint_ts 在 [now - window, now + window] 内
    且未 follow_up / 未 skip。"""
    now_ts = float(now if now is not None else time.time())
    out: list[dict[str, Any]] = []
    for item in list_pending_topics():
        if item.get("followed_up_at") or item.get("skipped"):
            continue
        ts = float(item.get("time_hint_ts", 0) or 0)
        if ts <= 0:
            continue
        if abs(ts - now_ts) <= window_seconds:
            out.append(item)
    return out


def mark_followed_up(topic_id: str, *, now: float | None = None) -> None:
    data = _load()
    if topic_id not in data or not isinstance(data[topic_id], dict):
        return
    data[topic_id]["followed_up_at"] = float(now if now is not None else time.time())
    _save(data)


def mark_skipped(topic_id: str) -> None:
    data = _load()
    if topic_id not in data or not isinstance(data[topic_id], dict):
        return
    data[topic_id]["skipped"] = True
    _save(data)


def _prune_old(data: dict[str, Any], *, max_age_days: int = 30) -> None:
    cutoff = time.time() - max_age_days * 86400
    expired = [tid for tid, e in data.items()
               if isinstance(e, dict) and float(e.get("created_at", 0) or 0) < cutoff]
    for tid in expired:
        data.pop(tid, None)


__all__ = [
    "add_pending_topic",
    "list_pending_topics",
    "find_due_topics",
    "mark_followed_up",
    "mark_skipped",
]
