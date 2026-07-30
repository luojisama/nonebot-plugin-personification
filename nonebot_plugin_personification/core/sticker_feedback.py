from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from .data_store import get_data_store
from .error_utils import log_exception


_STORE_NAME = "sticker_feedback_v2"
_PENDING_TTL_SECONDS = 30
_PENDING_STICKER_REACTIONS: dict[str, dict[str, Any]] = {}
_DEFAULT_STATE: dict[str, Any] = {
    "items": {},
    "updated_at": "",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    legacy_send_count = int(value.get("send_count", 0) or 0)
    sent_count = int(value.get("sent_count", legacy_send_count) or 0)
    positive_count = int(value.get("positive_count", legacy_send_count) or 0)
    return {
        "sent_count": max(0, sent_count),
        "positive_count": max(0, min(positive_count, sent_count)) if sent_count > 0 else 0,
        "last_reason": str(value.get("last_reason", "") or "").strip(),
        "last_sent_at": str(value.get("last_sent_at", value.get("updated_at", "")) or "").strip(),
        "last_positive_at": str(value.get("last_positive_at", "") or "").strip(),
    }


def _normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(_DEFAULT_STATE)
    items = raw.get("items", {})
    normalized_items: dict[str, dict[str, Any]] = {}
    if isinstance(items, dict):
        for key, value in items.items():
            sticker_name = str(key or "").strip()
            if not sticker_name:
                continue
            normalized = _normalize_item(value)
            if normalized is not None:
                normalized_items[sticker_name] = normalized
    return {
        "items": normalized_items,
        "updated_at": str(raw.get("updated_at", "") or "").strip(),
    }


async def load_sticker_feedback() -> dict[str, Any]:
    raw = await get_data_store().load(_STORE_NAME)
    return _normalize_state(raw)


def build_sticker_feedback_scene_key(
    *,
    group_id: str = "",
    user_id: str = "",
    is_private: bool = False,
) -> str:
    if is_private or not str(group_id or "").strip():
        return f"private:{str(user_id or '').strip()}"
    return f"group:{str(group_id or '').strip()}"


def mark_pending_sticker_reaction(
    scene_key: str,
    sticker_name: str,
    *,
    ttl_seconds: int = _PENDING_TTL_SECONDS,
) -> None:
    key = str(scene_key or "").strip()
    name = str(sticker_name or "").strip()
    if not key or not name:
        return
    _PENDING_STICKER_REACTIONS[key] = {
        "sticker_name": name,
        "expires_at": time.time() + max(1, int(ttl_seconds or _PENDING_TTL_SECONDS)),
    }


def _pop_pending_sticker_reaction(scene_key: str) -> str:
    key = str(scene_key or "").strip()
    if not key:
        return ""
    payload = _PENDING_STICKER_REACTIONS.pop(key, None)
    if not isinstance(payload, dict):
        return ""
    if float(payload.get("expires_at", 0) or 0) < time.time():
        return ""
    return str(payload.get("sticker_name", "") or "").strip()


async def _mutate_sticker_feedback(
    sticker_name: str,
    *,
    on_update: Any,
) -> dict[str, Any]:
    name = str(sticker_name or "").strip()
    if not name:
        return dict(_DEFAULT_STATE)

    store = get_data_store()
    async with store._alock(_STORE_NAME):
        current = _normalize_state(await store.load(_STORE_NAME))
        items = dict(current.get("items", {}) or {})
        entry = dict(items.get(name, {}) or {})
        updated_entry = on_update(entry)
        items[name] = _normalize_item(updated_entry) or _normalize_item(entry) or {
            "sent_count": 0,
            "positive_count": 0,
            "last_reason": "",
            "last_sent_at": "",
            "last_positive_at": "",
        }
        updated = {
            "items": items,
            "updated_at": _now_text(),
        }
        await store.save(_STORE_NAME, updated)
        return updated


async def record_sticker_sent(sticker_name: str) -> dict[str, Any]:
    def _update(entry: dict[str, Any]) -> dict[str, Any]:
        updated = dict(entry)
        updated["sent_count"] = int(updated.get("sent_count", 0) or 0) + 1
        updated["last_sent_at"] = _now_text()
        return updated

    return await _mutate_sticker_feedback(sticker_name, on_update=_update)


async def record_positive_reaction(sticker_name: str) -> dict[str, Any]:
    def _update(entry: dict[str, Any]) -> dict[str, Any]:
        updated = dict(entry)
        sent_count = int(updated.get("sent_count", 0) or 0)
        positive_count = int(updated.get("positive_count", 0) or 0)
        if sent_count <= 0:
            sent_count = 1
        updated["sent_count"] = sent_count
        updated["positive_count"] = min(sent_count, positive_count + 1)
        updated["last_positive_at"] = _now_text()
        return updated

    return await _mutate_sticker_feedback(sticker_name, on_update=_update)


def get_sticker_score(sticker_name: str, state: dict[str, Any] | None = None) -> float:
    payload = _normalize_state(state)
    entry = dict(payload.get("items", {}) or {}).get(str(sticker_name or "").strip())
    if not isinstance(entry, dict):
        return 1.0
    sent_count = int(entry.get("sent_count", 0) or 0)
    positive_count = int(entry.get("positive_count", 0) or 0)
    if sent_count <= 0:
        return 1.0
    return max(0.0, min(1.0, positive_count / sent_count))


def get_sticker_feedback_bonus(sticker_name: str, state: dict[str, Any] | None = None) -> float:
    score = get_sticker_score(sticker_name, state)
    return round((score - 0.5) * 4.0, 3)


# 衰减权重：发过的表情包短期内被压制，避免高 weight 反复中签。
# - 1 小时内：multiplier = 0.3（强烈压制）
# - 1-24 小时：线性从 0.3 恢复到 1.0
# - ≥24 小时：1.0（完全恢复）
_DECAY_FLOOR = 0.3
_DECAY_FULL_RECOVERY_HOURS = 24.0
_DECAY_GRACE_HOURS = 1.0


def _parse_last_sent_at(raw: str) -> float:
    """把 '2026-05-18 14:23:00' 解析为 epoch 秒；失败返 0。"""
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def get_sticker_decay_multiplier(
    sticker_name: str,
    state: dict[str, Any] | None = None,
    *,
    now_ts: float | None = None,
) -> float:
    """根据上次发送时间返回 0.3~1.0 的乘子，用于抑制刚刚发过的表情包再次被选。

    用法：在表情包选择器打分阶段乘以本系数：
        final_score = base_weight * feedback_score * get_sticker_decay_multiplier(name)
    """
    payload = _normalize_state(state)
    entry = dict(payload.get("items", {}) or {}).get(str(sticker_name or "").strip())
    if not isinstance(entry, dict):
        return 1.0
    last_sent_ts = _parse_last_sent_at(entry.get("last_sent_at", ""))
    if last_sent_ts <= 0:
        return 1.0
    current_ts = float(now_ts) if now_ts is not None else time.time()
    hours_since = max(0.0, (current_ts - last_sent_ts) / 3600.0)
    if hours_since < _DECAY_GRACE_HOURS:
        return _DECAY_FLOOR
    if hours_since >= _DECAY_FULL_RECOVERY_HOURS:
        return 1.0
    # 线性插值 0.3 → 1.0
    progress = (hours_since - _DECAY_GRACE_HOURS) / (_DECAY_FULL_RECOVERY_HOURS - _DECAY_GRACE_HOURS)
    return round(_DECAY_FLOOR + (1.0 - _DECAY_FLOOR) * progress, 3)


def _parse_reaction_verdict(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if re.search(r"\bpositive\b", text):
        return "positive"
    if re.search(r"\bnegative\b", text):
        return "negative"
    return ""


async def review_pending_sticker_reaction(
    scene_key: str,
    message_text: str,
    *,
    tool_caller: Any,
    logger: Any,
) -> bool:
    sticker_name = _pop_pending_sticker_reaction(scene_key)
    if not sticker_name:
        return False
    normalized_text = str(message_text or "").strip()
    if not normalized_text or tool_caller is None:
        return False
    try:
        response = await tool_caller.chat_with_tools(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "上一条是你发出的表情包。"
                        "判断下面这条新消息是在积极接这个表情包，还是无视/吐槽/转开话题。"
                        "只回答 positive 或 negative。"
                    ),
                },
                {"role": "user", "content": normalized_text[:200]},
            ],
            tools=[],
            use_builtin_search=False,
        )
    except Exception as exc:
        log_exception(
            logger,
            f"[sticker_feedback] reaction review failed scene={scene_key}",
            exc,
            level="debug",
        )
        return False
    if _parse_reaction_verdict(getattr(response, "content", "") or "") != "positive":
        return False
    await record_positive_reaction(sticker_name)
    return True


async def record_sticker_feedback(
    sticker_name: str,
    *,
    score: float = 1.0,
    reason: str = "",
) -> dict[str, Any]:
    del score, reason
    return await record_sticker_sent(sticker_name)


__all__ = [
    "build_sticker_feedback_scene_key",
    "get_sticker_decay_multiplier",
    "get_sticker_feedback_bonus",
    "get_sticker_score",
    "load_sticker_feedback",
    "mark_pending_sticker_reaction",
    "record_positive_reaction",
    "record_sticker_feedback",
    "record_sticker_sent",
    "review_pending_sticker_reaction",
]
