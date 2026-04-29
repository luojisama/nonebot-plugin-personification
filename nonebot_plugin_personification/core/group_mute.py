from __future__ import annotations

import time
from typing import Any

from .data_store import get_data_store


_STORE_NAME = "group_mute_state"
_LOCAL_CACHE: dict[str, dict[str, float]] = {}
_DEFAULT_CHECK_TTL_SECONDS = 30.0


def _now() -> float:
    return time.time()


def _load_state() -> dict[str, Any]:
    try:
        data = get_data_store().load_sync(_STORE_NAME)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_group_state(group_id: str, payload: dict[str, Any]) -> None:
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        return

    def _mutate(current: Any) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        data[normalized_group_id] = payload
        return data

    try:
        get_data_store().mutate_sync(_STORE_NAME, _mutate)
    except Exception:
        return


def set_group_mute_until(group_id: str, muted_until: float, *, source: str = "") -> None:
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        return
    until = max(0.0, float(muted_until or 0.0))
    payload = {
        "muted_until": until,
        "checked_at": _now(),
        "source": str(source or "").strip(),
    }
    _LOCAL_CACHE[normalized_group_id] = {
        "muted_until": until,
        "checked_at": payload["checked_at"],
    }
    _save_group_state(normalized_group_id, payload)


def get_group_mute_until(group_id: str) -> float:
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        return 0.0
    cached = _LOCAL_CACHE.get(normalized_group_id)
    if isinstance(cached, dict):
        return float(cached.get("muted_until", 0.0) or 0.0)
    state = _load_state().get(normalized_group_id)
    if not isinstance(state, dict):
        return 0.0
    muted_until = float(state.get("muted_until", 0.0) or 0.0)
    checked_at = float(state.get("checked_at", 0.0) or 0.0)
    _LOCAL_CACHE[normalized_group_id] = {
        "muted_until": muted_until,
        "checked_at": checked_at,
    }
    return muted_until


def is_group_muted(group_id: str, *, now_ts: float | None = None) -> bool:
    now_value = _now() if now_ts is None else float(now_ts)
    muted_until = get_group_mute_until(group_id)
    if muted_until <= now_value:
        return False
    return True


def update_group_mute_from_notice(event: Any, *, bot_self_id: str = "", logger: Any = None) -> bool:
    notice_type = str(getattr(event, "notice_type", "") or "").strip()
    if notice_type != "group_ban":
        return False
    target_user_id = str(getattr(event, "user_id", "") or "").strip()
    self_id = str(bot_self_id or getattr(event, "self_id", "") or "").strip()
    if not self_id or target_user_id != self_id:
        return False
    group_id = str(getattr(event, "group_id", "") or "").strip()
    if not group_id:
        return False
    try:
        duration = max(0, int(getattr(event, "duration", 0) or 0))
    except (TypeError, ValueError):
        duration = 0
    sub_type = str(getattr(event, "sub_type", "") or "").strip().lower()
    muted_until = _now() + duration if duration > 0 and sub_type != "lift_ban" else 0.0
    set_group_mute_until(group_id, muted_until, source="notice")
    if logger is not None:
        if muted_until > _now():
            logger.info(f"拟人插件：检测到 bot 在群 {group_id} 被禁言，禁言到 {int(muted_until)}。")
        else:
            logger.info(f"拟人插件：检测到 bot 在群 {group_id} 禁言解除。")
    return True


async def refresh_bot_group_mute_state(
    bot: Any,
    group_id: str,
    *,
    logger: Any = None,
    ttl_seconds: float = _DEFAULT_CHECK_TTL_SECONDS,
) -> bool:
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        return False
    now_value = _now()
    cached = _LOCAL_CACHE.get(normalized_group_id)
    if isinstance(cached, dict):
        checked_at = float(cached.get("checked_at", 0.0) or 0.0)
        if now_value - checked_at <= max(0.0, float(ttl_seconds)):
            return float(cached.get("muted_until", 0.0) or 0.0) > now_value
    try:
        info = await bot.get_group_member_info(
            group_id=int(normalized_group_id),
            user_id=int(getattr(bot, "self_id", "") or 0),
            no_cache=True,
        )
    except TypeError:
        try:
            info = await bot.get_group_member_info(
                group_id=int(normalized_group_id),
                user_id=int(getattr(bot, "self_id", "") or 0),
            )
        except Exception as exc:
            if logger is not None:
                logger.debug(f"[group_mute] get_group_member_info failed: {exc}")
            return is_group_muted(normalized_group_id, now_ts=now_value)
    except Exception as exc:
        if logger is not None:
            logger.debug(f"[group_mute] get_group_member_info failed: {exc}")
        return is_group_muted(normalized_group_id, now_ts=now_value)
    try:
        muted_until = float((info or {}).get("shut_up_timestamp", 0) or 0.0)
    except (TypeError, ValueError):
        muted_until = 0.0
    set_group_mute_until(normalized_group_id, muted_until, source="member_info")
    return muted_until > now_value


__all__ = [
    "get_group_mute_until",
    "is_group_muted",
    "refresh_bot_group_mute_state",
    "set_group_mute_until",
    "update_group_mute_from_notice",
]
