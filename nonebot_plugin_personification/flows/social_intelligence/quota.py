"""主动社交配额：每用户每日上限 + 单场景冷却。

数据存 kv_store 命名空间 `social_outbound_log`，结构：
{
  "<YYYY-MM-DD>": {
    "<user_id>": {"<scenario>": [ts, ts, ...], "total": int}
  }
}

设计要点：
- 配额 / 冷却故意保守，避免 bot 被当成骚扰。
- 失败也安全：load_sync 出错时返回空 dict，下次重新累计。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

_NAMESPACE = "social_outbound_log"


def _today_str(now: float | None = None) -> str:
    ts = float(now if now is not None else time.time())
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _load_log() -> dict[str, Any]:
    try:
        from ...core.data_store import get_data_store

        store = get_data_store()
        data = store.load_sync(_NAMESPACE) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_log(data: dict[str, Any]) -> None:
    try:
        from ...core.data_store import get_data_store

        get_data_store().save_sync(_NAMESPACE, data)
    except Exception:
        return


def is_quota_exceeded(
    user_id: str,
    *,
    scenario: str,
    daily_quota_per_user: int,
    cooldown_seconds: int,
    now: float | None = None,
) -> bool:
    """检查给该用户发该场景的消息是否会超额。"""
    uid = str(user_id).strip()
    if not uid:
        return True
    quota = max(0, int(daily_quota_per_user or 0))
    if quota <= 0:
        return True
    cd = max(0, int(cooldown_seconds or 0))
    log = _load_log()
    today_log = log.get(_today_str(now), {}) if isinstance(log, dict) else {}
    user_log = today_log.get(uid, {}) if isinstance(today_log, dict) else {}
    total = int(user_log.get("total", 0) or 0)
    if total >= quota:
        return True
    if cd > 0:
        timestamps = user_log.get(scenario, []) or []
        if isinstance(timestamps, list) and timestamps:
            try:
                last = float(timestamps[-1])
            except Exception:
                last = 0.0
            now_ts = float(now if now is not None else time.time())
            if now_ts - last < cd:
                return True
    return False


def mark_sent(user_id: str, *, scenario: str, now: float | None = None) -> None:
    uid = str(user_id).strip()
    if not uid:
        return
    now_ts = float(now if now is not None else time.time())
    today = _today_str(now_ts)
    log = _load_log()
    if not isinstance(log, dict):
        log = {}
    today_log = log.setdefault(today, {})
    if not isinstance(today_log, dict):
        today_log = {}
        log[today] = today_log
    user_log = today_log.setdefault(uid, {})
    if not isinstance(user_log, dict):
        user_log = {}
        today_log[uid] = user_log
    timestamps = user_log.setdefault(scenario, [])
    if not isinstance(timestamps, list):
        timestamps = []
        user_log[scenario] = timestamps
    timestamps.append(now_ts)
    user_log["total"] = int(user_log.get("total", 0) or 0) + 1
    _prune_old_days(log)
    _save_log(log)


def _prune_old_days(log: dict[str, Any], keep_days: int = 14) -> None:
    """保留最近 keep_days 天的日志，超出的删掉（防止 kv 越来越大）。"""
    if not isinstance(log, dict):
        return
    keys = sorted(log.keys())
    if len(keys) <= keep_days:
        return
    for k in keys[: len(keys) - keep_days]:
        log.pop(k, None)


def _clear_log_for_testing() -> None:
    _save_log({})


__all__ = ["is_quota_exceeded", "mark_sent"]
