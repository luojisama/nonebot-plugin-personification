from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from ..plugin_data import get_plugin_data_dir
from ..core.data_store import get_data_store
from ..schedule import format_time_context, get_activity_status, get_current_local_time
from ..skills.skillpacks.tool_caller.scripts.impl import ToolCaller


DEFAULT_STATE = {
    "mood": "平静",
    "energy": "正常",
    "pending_thoughts": [],
    "relation_warmth": {},
    "updated_at": "",
}

INNER_STATE_UPDATE_PROMPT = """你刚结束了一段对话，请根据对话内容简短更新你的内心状态。
只输出 JSON，不要任何解释。

当前时间：{current_time}
当前时段：{time_period}

当前状态：
{current_state_json}

本次对话摘要：
{conversation_summary}

请输出更新后的状态（只修改需要变化的字段，不变的字段原样保留）：
{{
  "mood": "用一句话描述当前心情（如变化不大可保持原样）",
  "energy": "高/中/低",
  "pending_thoughts": [
    保留原有未解决的念头，
    如果本次对话产生了新的念头则追加，
    如果某个念头已经解决了则删除
  ],
  "relation_warmth": {
    只更新本次对话涉及的人，其他人原样保留
  }
}}

注意：
- 对话愉快时 mood 变积极，被催促或冷场时变平淡
- pending_thoughts 里的念头不要无限累积，超过5条时删除最旧的
- 只输出 JSON，不要解释，不要 markdown"""

DIARY_STATE_UPDATE_PROMPT = """今天的日记如下：
{diary_text}

请根据日记内容，用 JSON 格式更新以下两个字段（只输出这两个字段的更新值）：
{
  "mood": "根据今天整体感受，用一句话描述明天开始的基础心情",
  "pending_thoughts": [
    如果日记中提到了还没做完的事、想跟进的人/话题，加入这里
    格式：{"thought": "...", "born_at": "{today}"}
  ]
}
只输出 JSON。"""


def get_personification_data_dir(plugin_config: Any | None = None) -> Path:
    _ = plugin_config
    return get_plugin_data_dir()

_STORE_NAME = "inner_state"


async def load_inner_state(data_dir: Path) -> dict:
    _ = data_dir
    loaded = await get_data_store().load(_STORE_NAME)
    if not isinstance(loaded, dict):
        return copy.deepcopy(DEFAULT_STATE)
    state = copy.deepcopy(DEFAULT_STATE)
    state.update(loaded)
    return state


async def save_inner_state(data_dir: Path, state: dict) -> None:
    _ = data_dir
    await get_data_store().save(_STORE_NAME, state)


def _merge_state(current_state: Dict[str, Any], new_state: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_STATE)
    merged.update(current_state or {})
    now = datetime.now()
    current_mood = str(merged.get("mood") or DEFAULT_STATE["mood"]).strip()
    current_energy = str(merged.get("energy") or DEFAULT_STATE["energy"]).strip()
    incoming_mood = str((new_state or {}).get("mood") or "").strip()
    incoming_energy = str((new_state or {}).get("energy") or "").strip()

    updated_at_raw = str(merged.get("updated_at") or "").strip()
    hours_since_update = 0.0
    if updated_at_raw:
        try:
            prev = datetime.strptime(updated_at_raw, "%Y-%m-%d %H:%M:%S")
            hours_since_update = max(0.0, (now - prev).total_seconds() / 3600)
        except Exception:
            hours_since_update = 0.0

    if incoming_mood and incoming_mood != current_mood and hours_since_update < 0.5:
        new_state = dict(new_state or {})
        new_state["mood"] = current_mood
    elif incoming_mood and incoming_mood != current_mood and hours_since_update < 2:
        new_state = dict(new_state or {})
        new_state["mood"] = f"{current_mood}，但有些{incoming_mood}"

    if incoming_energy:
        if current_energy == "高" and incoming_energy == "低" and hours_since_update < 1:
            new_state = dict(new_state or {})
            new_state["energy"] = "中"
        if current_energy == "低" and incoming_energy == "高" and hours_since_update < 1:
            new_state = dict(new_state or {})
            new_state["energy"] = "中"

    for key, value in (new_state or {}).items():
        merged[key] = value

    relation = merged.get("relation_warmth")
    if not isinstance(relation, dict):
        relation = {}
    clipped_relation: Dict[str, Any] = {}
    for uid, score in relation.items():
        try:
            clipped_relation[str(uid)] = max(-1.0, min(1.0, float(score)))
        except Exception:
            continue
    merged["relation_warmth"] = clipped_relation

    pending = merged.get("pending_thoughts")
    if isinstance(pending, list) and len(pending) > 8:
        merged["pending_thoughts"] = pending[-8:]

    hour = now.hour
    if hour >= 23 or hour < 6:
        if merged.get("energy") == "高":
            merged["energy"] = "中"
    elif 7 <= hour <= 11:
        if merged.get("energy") == "低":
            merged["energy"] = "中"

    merged["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    return merged


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
    store = get_data_store()
    async with store._alock(_STORE_NAME):
        fresh_state = await asyncio.to_thread(store._read, _STORE_NAME)
        if not isinstance(fresh_state, dict):
            fresh_state = copy.deepcopy(DEFAULT_STATE)
        else:
            merged_state = copy.deepcopy(DEFAULT_STATE)
            merged_state.update(fresh_state)
            fresh_state = merged_state

        persona_context = ""
        if persona_snippet:
            persona_context = (
                "\n对话用户画像参考（用于更新 relation_warmth，不必照搬）：\n"
                f"{persona_snippet}\n"
            )
        datetime_now = get_current_local_time()
        prompt = INNER_STATE_UPDATE_PROMPT.replace(
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
        try:
            response = await tool_caller.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                use_builtin_search=False,
            )
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


async def update_state_from_diary(
    diary_text: str,
    data_dir: Path,
    tool_caller: ToolCaller,
    logger: Any,
) -> None:
    _ = data_dir
    store = get_data_store()
    async with store._alock(_STORE_NAME):
        fresh_state = await asyncio.to_thread(store._read, _STORE_NAME)
        if not isinstance(fresh_state, dict):
            fresh_state = copy.deepcopy(DEFAULT_STATE)
        else:
            base = copy.deepcopy(DEFAULT_STATE)
            base.update(fresh_state)
            fresh_state = base

        prompt = DIARY_STATE_UPDATE_PROMPT.replace(
            "{diary_text}",
            diary_text,
        ).replace(
            "{today}",
            datetime.now().strftime("%Y-%m-%d"),
        )
        try:
            response = await tool_caller.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                use_builtin_search=False,
            )
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
