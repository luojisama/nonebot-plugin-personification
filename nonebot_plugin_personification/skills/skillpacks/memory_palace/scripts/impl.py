from __future__ import annotations

import json
from typing import Any

from plugin.personification.agent.tool_registry import AgentTool
from plugin.personification.core.memory_store import get_memory_store


async def recall_memory(
    *,
    runtime: Any,
    query: str,
    scope: str = "auto",
    user_id: str = "",
    group_id: str = "",
    mode: str = "auto",
) -> str:
    try:
        store = getattr(runtime, "memory_store", None) or get_memory_store()
    except Exception:
        return json.dumps(
            {"query": str(query or ""), "scope": str(scope or "auto"), "mode": str(mode or "auto"), "memories": []},
            ensure_ascii=False,
        )
    memories = store.recall_memories(
        query=str(query or ""),
        scope=str(scope or "auto"),
        user_id=str(user_id or ""),
        group_id=str(group_id or ""),
        mode=str(mode or "auto"),
    )
    background_intelligence = getattr(runtime, "background_intelligence", None)
    if background_intelligence is not None:
        background_intelligence.schedule_recall_reinforcement(
            [str(item.get("memory_id", "") or "") for item in memories]
        )
    payload = {
        "query": str(query or ""),
        "scope": str(scope or "auto"),
        "mode": str(mode or "auto"),
        "memories": memories,
    }
    return json.dumps(payload, ensure_ascii=False)


def build_memory_recall_tool(runtime: Any) -> AgentTool:
    async def _handler(
        query: str,
        scope: str = "auto",
        user_id: str = "",
        group_id: str = "",
        mode: str = "auto",
    ) -> str:
        return await recall_memory(
            runtime=runtime,
            query=query,
            scope=scope,
            user_id=user_id,
            group_id=group_id,
            mode=mode,
        )

    return AgentTool(
        name="memory_recall",
        description=(
            "回忆近期聊过的情节、人物印象、群聊上下文和长期主题。"
            "适合“上次聊到哪了”“你还记得吗”“这个人在这个群里平时怎么聊”这类场景。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "当前问题或回忆目标"},
                "scope": {"type": "string", "description": "auto/recent_episode/person/group/topic/self/future"},
                "user_id": {"type": "string", "description": "可选用户 ID"},
                "group_id": {"type": "string", "description": "可选群 ID"},
                "mode": {"type": "string", "description": "auto/fast/deep；其中 deep 当前是启发式深度重排，不是完整 LLM 深度策略"},
            },
            "required": ["query"],
        },
        handler=_handler,
        enabled=lambda: True,
    )
