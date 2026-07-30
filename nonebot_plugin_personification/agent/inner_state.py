from __future__ import annotations

import asyncio
import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

DEFAULT_STATE = {
    "mood": "平静",
    "energy": "正常",
    "pending_thoughts": [],
    "relation_warmth": {},
    "updated_at": "",
}

_MOOD_LABELS = ("平静", "开心", "疲惫", "困倦", "烦躁", "低落", "期待", "紧张", "放松", "无语", "好奇")
_ENERGY_LABELS = ("高", "中", "低", "正常")
_MOOD_ALIASES = {
    "平淡": "平静",
    "平稳": "平静",
    "普通": "平静",
    "正常": "平静",
    "高兴": "开心",
    "愉快": "开心",
    "累": "疲惫",
    "疲劳": "疲惫",
    "想睡": "困倦",
    "困": "困倦",
    "生气": "烦躁",
    "烦": "烦躁",
    "难过": "低落",
    "伤心": "低落",
    "期待": "期待",
    "紧张": "紧张",
    "放松": "放松",
    "无语": "无语",
    "好奇": "好奇",
}

def _get_data_store() -> Any:
    from ..core.data_store import get_data_store

    return get_data_store()


def _load_prompt_text(name: str) -> str:
    from ..core.prompts import load_prompt

    return load_prompt(name)


def get_personification_data_dir(plugin_config: Any | None = None) -> Path:
    from ..core.paths import get_data_dir

    return get_data_dir(plugin_config)

_STORE_NAME = "inner_state"


def _compact_text(value: Any, *, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit].strip()


def _normalize_mood(value: Any, *, fallback: str = DEFAULT_STATE["mood"]) -> str:
    text = _compact_text(value, limit=80)
    if not text:
        return fallback
    if text in _MOOD_LABELS:
        return text
    for label in _MOOD_LABELS:
        if label in text:
            return label
    compact = re.sub(r"[\s，。！？、,.!?;；:：]+", "", text)
    for needle, label in _MOOD_ALIASES.items():
        if needle and needle in compact:
            return label
    if fallback == "":
        return ""
    return fallback if fallback in _MOOD_LABELS else DEFAULT_STATE["mood"]


def _normalize_energy(value: Any, *, fallback: str = DEFAULT_STATE["energy"]) -> str:
    text = _compact_text(value, limit=20)
    if not text:
        return fallback
    if text in _ENERGY_LABELS:
        return text
    if "高" in text:
        return "高"
    if "低" in text:
        return "低"
    if "中" in text:
        return "中"
    if "正常" in text or "普通" in text:
        return "正常"
    if fallback == "":
        return ""
    return fallback if fallback in _ENERGY_LABELS else DEFAULT_STATE["energy"]


def _normalize_pending_thoughts(value: Any, *, limit: int = 8) -> list[dict[str, str]]:
    if value in (None, "", False):
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]
    items: list[dict[str, str]] = []
    for item in raw_items:
        if isinstance(item, dict):
            thought = ""
            for key in ("thought", "text", "summary", "content", "title"):
                thought = _compact_text(item.get(key), limit=120)
                if thought:
                    break
            if not thought:
                thought = _compact_text(json.dumps(item, ensure_ascii=False), limit=120)
            if thought:
                normalized = {"thought": thought}
                for key in ("status", "due", "user_id", "group_id"):
                    val = _compact_text(item.get(key), limit=40)
                    if val:
                        normalized[key] = val
                items.append(normalized)
            continue
        thought = _compact_text(item, limit=120)
        if thought:
            items.append({"thought": thought})
    return items[-max(1, int(limit)):]


def normalize_inner_state(state: dict | None) -> dict:
    normalized = copy.deepcopy(DEFAULT_STATE)
    if isinstance(state, dict):
        normalized.update(state)
    normalized["mood"] = _normalize_mood(normalized.get("mood"), fallback=DEFAULT_STATE["mood"])
    normalized["energy"] = _normalize_energy(normalized.get("energy"), fallback=DEFAULT_STATE["energy"])
    normalized["pending_thoughts"] = _normalize_pending_thoughts(normalized.get("pending_thoughts"))

    relation = normalized.get("relation_warmth")
    if not isinstance(relation, dict):
        relation = {}
    clipped_relation: Dict[str, Any] = {}
    for uid, score in relation.items():
        try:
            clipped_relation[str(uid)] = max(-1.0, min(1.0, float(score)))
        except Exception:
            continue
    normalized["relation_warmth"] = clipped_relation
    normalized["updated_at"] = str(normalized.get("updated_at") or "")
    return normalized


async def load_inner_state(data_dir: Path) -> dict:
    _ = data_dir
    loaded = await _get_data_store().load(_STORE_NAME)
    if not isinstance(loaded, dict):
        return copy.deepcopy(DEFAULT_STATE)
    return normalize_inner_state(loaded)


async def save_inner_state(data_dir: Path, state: dict) -> None:
    _ = data_dir
    await _get_data_store().save(_STORE_NAME, normalize_inner_state(state))


def _merge_state(current_state: Dict[str, Any], new_state: Dict[str, Any]) -> Dict[str, Any]:
    merged = normalize_inner_state(current_state or {})
    now = datetime.now()
    incoming_patch = dict(new_state or {})
    current_mood = _normalize_mood(merged.get("mood"), fallback=DEFAULT_STATE["mood"])
    current_energy = _normalize_energy(merged.get("energy"), fallback=DEFAULT_STATE["energy"])
    incoming_mood = _normalize_mood(incoming_patch.get("mood"), fallback="") if incoming_patch.get("mood") else ""
    incoming_energy = _normalize_energy(incoming_patch.get("energy"), fallback="") if incoming_patch.get("energy") else ""

    updated_at_raw = str(merged.get("updated_at") or "").strip()
    hours_since_update = 0.0
    if updated_at_raw:
        try:
            prev = datetime.strptime(updated_at_raw, "%Y-%m-%d %H:%M:%S")
            hours_since_update = max(0.0, (now - prev).total_seconds() / 3600)
        except Exception:
            hours_since_update = 0.0

    if incoming_mood and incoming_mood != current_mood and hours_since_update < 0.5:
        incoming_patch["mood"] = current_mood
    elif incoming_mood:
        incoming_patch["mood"] = incoming_mood

    if incoming_energy:
        if current_energy == "高" and incoming_energy == "低" and hours_since_update < 1:
            incoming_patch["energy"] = "中"
        elif current_energy == "低" and incoming_energy == "高" and hours_since_update < 1:
            incoming_patch["energy"] = "中"
        else:
            incoming_patch["energy"] = incoming_energy

    if "pending_thoughts" in incoming_patch:
        incoming_patch["pending_thoughts"] = _normalize_pending_thoughts(incoming_patch.get("pending_thoughts"))

    for key, value in incoming_patch.items():
        merged[key] = value

    hour = now.hour
    if hour >= 23 or hour < 6:
        if merged.get("energy") == "高":
            merged["energy"] = "中"
    elif 7 <= hour <= 11:
        if merged.get("energy") == "低":
            merged["energy"] = "中"

    merged["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    return normalize_inner_state(merged)


async def update_inner_state_after_chat(
    data_dir: Path,
    tool_caller: ToolCaller,
    recent_summary: str,
    current_state: dict,
    thinking_mode: str,
    logger: Any,
    persona_snippet: str = "",
) -> None:
    _ = data_dir, current_state
    # thinking_mode 参数保留以备将来直接在 tool_caller 层控制推理档位
    # 当前由 build_inner_state_updater 构建 state_tool_caller 时应用
    store = _get_data_store()
    async with store._alock(_STORE_NAME):
        fresh_state = await asyncio.to_thread(store._read, _STORE_NAME)
        if not isinstance(fresh_state, dict):
            fresh_state = copy.deepcopy(DEFAULT_STATE)
        else:
            fresh_state = normalize_inner_state(fresh_state)

        persona_context = ""
        if persona_snippet:
            persona_context = (
                "\n对话用户画像参考（用于更新 relation_warmth，不必照搬）：\n"
                f"{persona_snippet}\n"
            )
        from ..schedule import format_time_context, get_activity_status, get_current_local_time

        datetime_now = get_current_local_time()
        prompt = _load_prompt_text("inner_state_update").replace(
            "{current_time}",
            f"{datetime_now.strftime('%Y-%m-%d %H:%M:%S')} [{format_time_context(datetime_now)}]",
        ).replace(
            "{time_period}",
            get_activity_status(),
        ).replace(
            "{current_state_json}",
            json.dumps(fresh_state, ensure_ascii=False, indent=2),
        ).replace(
            "{conversation_summary}",
            f"{recent_summary}{persona_context}",
        )
        token = None
        try:
            from ..core.llm_context import reset_llm_context, set_llm_context

            token = set_llm_context(purpose="inner_state_chat")
        except Exception:
            token = None
        try:
            response = await tool_caller.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                use_builtin_search=False,
            )
            try:
                from ..core.token_ledger import record_response_usage
                record_response_usage(response)
            except Exception:
                pass
            updated_fields = json.loads(response.content or "{}")
            if not isinstance(updated_fields, dict):
                raise ValueError("inner state update is not a JSON object")
            await asyncio.to_thread(
                store._write,
                _STORE_NAME,
                _merge_state(fresh_state, updated_fields),
            )
        except Exception as e:
            logger.warning(f"[inner_state] update failed: {e}")
        finally:
            if token is not None:
                try:
                    reset_llm_context(token)
                except Exception:
                    pass


async def update_state_from_diary(
    diary_text: str,
    data_dir: Path,
    tool_caller: ToolCaller,
    logger: Any,
) -> None:
    _ = data_dir
    store = _get_data_store()
    async with store._alock(_STORE_NAME):
        fresh_state = await asyncio.to_thread(store._read, _STORE_NAME)
        if not isinstance(fresh_state, dict):
            fresh_state = copy.deepcopy(DEFAULT_STATE)
        else:
            fresh_state = normalize_inner_state(fresh_state)

        prompt = _load_prompt_text("diary_state_update").replace(
            "{diary_text}",
            diary_text,
        ).replace(
            "{today}",
            datetime.now().strftime("%Y-%m-%d"),
        )
        token = None
        try:
            from ..core.llm_context import reset_llm_context, set_llm_context

            token = set_llm_context(purpose="inner_state_diary")
        except Exception:
            token = None
        try:
            response = await tool_caller.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                use_builtin_search=False,
            )
            try:
                from ..core.token_ledger import record_response_usage
                record_response_usage(response)
            except Exception:
                pass
            updated_fields = json.loads(response.content or "{}")
            if not isinstance(updated_fields, dict):
                raise ValueError("diary state update is not a JSON object")
            await asyncio.to_thread(
                store._write,
                _STORE_NAME,
                _merge_state(fresh_state, updated_fields),
            )
        except Exception as e:
            logger.warning(f"[inner_state] diary update failed: {e}")
        finally:
            if token is not None:
                try:
                    reset_llm_context(token)
                except Exception:
                    pass
