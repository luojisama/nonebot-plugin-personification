from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

_RELATION_EVOLUTION_PROMPT = (
    "你是群友关系演化器。根据 bot 与用户的最近一次互动摘要和当前关系标签，"
    "判断关系是否需要更新。只输出 JSON。\n"
    '{"action":"tag_add|tag_remove|adjust_weight|no_change","tag":"关系标签","new_weight":0.0,"reason":"15字中文理由"}\n'
    "tag_add=新增一个关系标签；tag_remove=移除一个关系标签；adjust_weight=调整权重；no_change=无需变化。"
)


_RELATION_EVOLUTION_DAILY_QUOTA = 30


def _read_quota(memory_store: Any, key: str) -> int:
    try:
        item = memory_store.get_memory_item(key)
        if item and isinstance(item, dict):
            return int(item.get("counter", 0) or 0)
    except Exception:
        pass
    return 0


def _write_quota(memory_store: Any, key: str, value: int) -> None:
    try:
        memory_store.write_memory_item({
            "memory_id": key,
            "memory_type": "quota_tracker",
            "summary": f"relation_evolution_quota:{value}",
            "counter": value,
            "confidence": 1.0,
            "salience": 0.0,
        })
    except Exception:
        pass


async def evolve_group_relations(
    *,
    tool_caller: Any,
    memory_store: Any,
    group_id: str,
    user_id: str,
    turn_summary: str,
    current_tags: list[str],
    plugin_config: Any = None,
) -> dict[str, Any]:
    if not tool_caller or not memory_store:
        return {"action": "no_change", "reason": "缺少调用器或存储"}

    daily_quota = _RELATION_EVOLUTION_DAILY_QUOTA
    if plugin_config is not None:
        daily_quota = int(getattr(plugin_config, "personification_relation_evolution_daily_quota", _RELATION_EVOLUTION_DAILY_QUOTA) or _RELATION_EVOLUTION_DAILY_QUOTA)
    today = time.strftime("%Y-%m-%d")
    quota_key = f"relation_evolution_quota_{group_id}_{today}"
    used = _read_quota(memory_store, quota_key)
    if used >= daily_quota:
        return {"action": "no_change", "reason": "日配额已用完"}

    tags_text = "、".join(current_tags) if current_tags else "无"
    try:
        response = await asyncio.wait_for(
            tool_caller.chat_with_tools(
                messages=[
                    {"role": "system", "content": _RELATION_EVOLUTION_PROMPT},
                    {"role": "user", "content": (
                        f"群ID：{group_id}\n"
                        f"用户ID：{user_id}\n"
                        f"当前关系标签：{tags_text}\n"
                        f"本回合互动摘要：{str(turn_summary)[:600]}"
                    )},
                ],
                tools=[],
                use_builtin_search=False,
            ),
            timeout=10.0,
        )
    except Exception:
        return {"action": "no_change", "reason": "LLM 调用失败"}

    content = str(getattr(response, "content", "") or "").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            return {"action": "no_change", "reason": "解析失败"}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {"action": "no_change", "reason": "解析失败"}

    if not isinstance(data, dict):
        return {"action": "no_change", "reason": "非 JSON"}

    action = str(data.get("action", "no_change") or "no_change").strip()
    if action not in {"tag_add", "tag_remove", "adjust_weight", "no_change"}:
        action = "no_change"
    tag = str(data.get("tag", "") or "").strip()[:30]
    try:
        new_weight = max(0.0, min(3.0, float(data.get("new_weight", 1.0) or 1.0)))
    except (ValueError, TypeError):
        new_weight = 1.0
    reason = str(data.get("reason", "") or "").strip()[:40]

    if action == "no_change":
        return {"action": "no_change", "reason": reason or "无需变化"}

    memory_store.write_memory_item({
        "memory_id": f"gr_{group_id}_{user_id}_{int(time.time())}",
        "memory_type": "group_relation",
        "summary": f"{action}:{tag} weight={new_weight:.2f}",
        "group_id": str(group_id),
        "user_id": str(user_id),
        "action": action,
        "tag": tag,
        "weight": new_weight,
        "reason": reason,
        "source_kind": "relation_evolution",
        "confidence": 0.6,
        "salience": 0.5,
    })
    _write_quota(memory_store, quota_key, used + 1)
    return {"action": action, "tag": tag, "new_weight": new_weight, "reason": reason}


def list_group_relations(memory_store: Any, group_id: str) -> list[dict[str, Any]]:
    if not memory_store:
        return []
    return memory_store.list_recent_memories(
        group_id=str(group_id),
        memory_type="group_relation",
        limit=100,
    )


__all__ = ["evolve_group_relations", "list_group_relations"]
