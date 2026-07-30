from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .data_store import get_data_store

if TYPE_CHECKING:
    from .chat_intent import TurnSemanticFrame


_STORE_NAME = "emotion_state_v1"
_MAX_USER_EMOTION_ENTRIES = 256
_MAX_GROUP_EMOTION_ENTRIES = 128
_EMOTION_STATE_TTL_DAYS = 30

DEFAULT_EMOTION_STATE: dict[str, Any] = {
    "per_user": {},
    "per_group": {},
    "updated_at": "",
}


def _parse_updated_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _prune_bucket(raw: Any, *, max_entries: int, ttl_days: int) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    cutoff = datetime.now() - timedelta(days=max(1, int(ttl_days or 1)))
    kept: list[tuple[datetime, str, dict[str, Any]]] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        normalized_key = str(key)
        entry = dict(value)
        updated_at = _parse_updated_at(entry.get("updated_at"))
        if updated_at is not None and updated_at < cutoff:
            continue
        kept.append((updated_at or cutoff, normalized_key, entry))
    kept.sort(key=lambda item: (item[0], item[1]), reverse=True)
    trimmed = kept[: max(1, int(max_entries or 1))]
    trimmed.sort(key=lambda item: item[1])
    return {key: entry for _updated_at, key, entry in trimmed}


def _normalize_state(raw: Any) -> dict[str, Any]:
    state = copy.deepcopy(DEFAULT_EMOTION_STATE)
    if not isinstance(raw, dict):
        return state
    state["per_user"] = _prune_bucket(
        raw.get("per_user"),
        max_entries=_MAX_USER_EMOTION_ENTRIES,
        ttl_days=_EMOTION_STATE_TTL_DAYS,
    )
    state["per_group"] = _prune_bucket(
        raw.get("per_group"),
        max_entries=_MAX_GROUP_EMOTION_ENTRIES,
        ttl_days=_EMOTION_STATE_TTL_DAYS,
    )
    updated_at = str(raw.get("updated_at", "") or "").strip()
    if updated_at:
        state["updated_at"] = updated_at
    return state


async def load_emotion_state(data_dir: Path | None = None) -> dict[str, Any]:
    _ = data_dir
    loaded = await get_data_store().load(_STORE_NAME)
    return _normalize_state(loaded)


def _trim_text(value: Any, *, limit: int = 80) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def describe_user_emotion_memory(state: dict[str, Any], user_id: str) -> str:
    entry = dict((state or {}).get("per_user", {}) or {}).get(str(user_id))
    if not isinstance(entry, dict):
        return ""
    user_attitude = _trim_text(entry.get("user_attitude", ""), limit=40)
    bot_emotion = _trim_text(entry.get("bot_emotion", ""), limit=40)
    expression_style = _trim_text(entry.get("expression_style", ""), limit=40)
    parts: list[str] = []
    if user_attitude:
        parts.append(f"对方最近对你：{user_attitude}")
    if bot_emotion:
        parts.append(f"你上次对TA：{bot_emotion}")
    if expression_style:
        parts.append(f"表达方式：{expression_style}")
    return "；".join(parts)


def describe_group_emotion_memory(state: dict[str, Any], group_id: str) -> str:
    entry = dict((state or {}).get("per_group", {}) or {}).get(str(group_id))
    if not isinstance(entry, dict):
        return ""
    group_climate = _trim_text(entry.get("group_climate", ""), limit=50)
    social_posture = _trim_text(entry.get("bot_social_posture", ""), limit=50)
    bot_emotion = _trim_text(entry.get("bot_emotion", ""), limit=40)
    parts: list[str] = []
    if group_climate:
        parts.append(f"群氛围：{group_climate}")
    if social_posture:
        parts.append(f"你的社交姿态：{social_posture}")
    if bot_emotion:
        parts.append(f"最近情绪：{bot_emotion}")
    return "；".join(parts)


def render_inner_state_hint(inner_state: dict[str, Any]) -> str:
    state = dict(inner_state or {})
    mood = _trim_text(state.get("mood", "平静"), limit=40) or "平静"
    energy = _trim_text(state.get("energy", "正常"), limit=20) or "正常"
    pending = state.get("pending_thoughts", [])
    pending_lines: list[str] = []
    if isinstance(pending, list):
        for item in pending[-3:]:
            if not isinstance(item, dict):
                continue
            thought = _trim_text(item.get("thought", ""), limit=40)
            if thought:
                pending_lines.append(thought)
    pending_text = "；".join(pending_lines) if pending_lines else "无明显挂念"
    return f"全局心情：{mood}；精力：{energy}；最近挂念：{pending_text}"


def render_emotion_memory_hint(
    state: dict[str, Any],
    *,
    user_id: str = "",
    group_id: str = "",
) -> str:
    parts: list[str] = []
    if user_id:
        user_hint = describe_user_emotion_memory(state, user_id)
        if user_hint:
            parts.append(f"与当前用户的近期情绪记忆：{user_hint}")
    if group_id:
        group_hint = describe_group_emotion_memory(state, group_id)
        if group_hint:
            parts.append(f"当前群的近期情绪记忆：{group_hint}")
    return "\n".join(parts).strip()


def build_turn_emotion_prompt_block(
    *,
    semantic_frame: TurnSemanticFrame | Any,
    inner_state: dict[str, Any],
    emotion_state: dict[str, Any],
    user_id: str,
    group_id: str = "",
    is_private: bool = False,
) -> str:
    frame = semantic_frame
    user_hint = describe_user_emotion_memory(emotion_state, user_id)
    group_hint = "" if is_private or not group_id else describe_group_emotion_memory(emotion_state, group_id)
    lines = [
        "## 当前互动情绪",
        f"- 用户此刻对你的态度：{_trim_text(getattr(frame, 'user_attitude', ''), limit=60) or '中性互动'}",
        (
            f"- 你此刻的情绪：{_trim_text(getattr(frame, 'bot_emotion', ''), limit=60) or '平静'}"
            f"（强度：{_trim_text(getattr(frame, 'emotion_intensity', ''), limit=12) or 'medium'}）"
        ),
        f"- 本轮表达方式：{_trim_text(getattr(frame, 'expression_style', ''), limit=60) or '自然简短'}",
        f"- 你的内心基线：{render_inner_state_hint(inner_state)}",
    ]
    if user_hint:
        lines.append(f"- 你和这个人的近期情绪记忆：{user_hint}")
    if group_hint:
        lines.append(f"- 这个群的近期情绪记忆：{group_hint}")
    return "\n".join(lines)


async def update_emotion_state_after_turn(
    data_dir: Path | None,
    *,
    user_id: str,
    group_id: str = "",
    semantic_frame: TurnSemanticFrame | Any,
    assistant_text: str = "",
    is_private: bool = False,
) -> dict[str, Any]:
    _ = data_dir
    store = get_data_store()
    async with store._alock(_STORE_NAME):
        loaded = await store.load(_STORE_NAME)
        state = _normalize_state(loaded)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        frame = semantic_frame

        per_user = dict(state.get("per_user", {}) or {})
        if user_id:
            per_user[str(user_id)] = {
                "user_attitude": _trim_text(getattr(frame, "user_attitude", ""), limit=80),
                "bot_emotion": _trim_text(getattr(frame, "bot_emotion", ""), limit=80),
                "emotion_intensity": _trim_text(getattr(frame, "emotion_intensity", ""), limit=16),
                "expression_style": _trim_text(getattr(frame, "expression_style", ""), limit=80),
                "tts_style_hint": _trim_text(getattr(frame, "tts_style_hint", ""), limit=60),
                "sticker_mood_hint": _trim_text(getattr(frame, "sticker_mood_hint", ""), limit=60),
                "last_group_id": "" if is_private else str(group_id or ""),
                "last_reply": _trim_text(assistant_text, limit=120),
                "updated_at": now_str,
            }
        state["per_user"] = per_user

        per_group = dict(state.get("per_group", {}) or {})
        if group_id and not is_private:
            per_group[str(group_id)] = {
                "group_climate": _trim_text(getattr(frame, "user_attitude", ""), limit=80),
                "bot_social_posture": _trim_text(getattr(frame, "expression_style", ""), limit=80),
                "bot_emotion": _trim_text(getattr(frame, "bot_emotion", ""), limit=80),
                "emotion_intensity": _trim_text(getattr(frame, "emotion_intensity", ""), limit=16),
                "last_user_id": str(user_id or ""),
                "updated_at": now_str,
            }
        state["per_user"] = _prune_bucket(
            per_user,
            max_entries=_MAX_USER_EMOTION_ENTRIES,
            ttl_days=_EMOTION_STATE_TTL_DAYS,
        )
        state["per_group"] = _prune_bucket(
            per_group,
            max_entries=_MAX_GROUP_EMOTION_ENTRIES,
            ttl_days=_EMOTION_STATE_TTL_DAYS,
        )
        state["updated_at"] = now_str
        await store.save(_STORE_NAME, state)
        return state


__all__ = [
    "DEFAULT_EMOTION_STATE",
    "build_turn_emotion_prompt_block",
    "describe_group_emotion_memory",
    "describe_user_emotion_memory",
    "load_emotion_state",
    "render_emotion_memory_hint",
    "render_inner_state_hint",
    "update_emotion_state_after_turn",
]
