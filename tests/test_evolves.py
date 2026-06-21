from __future__ import annotations

import asyncio
import json
from typing import Any

from ._loader import load_personification_module


evolves = load_personification_module("plugin.personification.core.evolves")


class _MemoryStore:
    def __init__(self, items: dict[str, dict[str, Any]]) -> None:
        self.items = items
        self.links: list[dict[str, Any]] = []
        self.feedback: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []

    def get_memory_item(self, memory_id: str) -> dict[str, Any] | None:
        item = self.items.get(memory_id)
        return dict(item) if isinstance(item, dict) else None

    def list_related_memory_candidates(self, *, memory_id: str, group_id: str = "", limit: int = 12):  # noqa: ANN001, ANN201
        _ = group_id
        return [dict(item) for key, item in self.items.items() if key != memory_id][:limit]

    def link_memories(self, **kwargs: Any) -> None:
        self.links.append(dict(kwargs))

    def write_memory_item(self, item: dict[str, Any]) -> str:
        payload = dict(item)
        self.writes.append(payload)
        memory_id = str(payload.get("memory_id", "") or "")
        if memory_id:
            self.items[memory_id] = payload
        return memory_id

    def report_memory_feedback(self, **kwargs: Any) -> None:
        self.feedback.append(dict(kwargs))


def test_evolves_uses_single_llm_call_for_candidate_batch() -> None:
    calls: list[list[dict[str, Any]]] = []

    async def _call_ai(messages: list[dict[str, Any]], **_kwargs: Any) -> str:
        calls.append(messages)
        return json.dumps(
            {
                "relations": [
                    {
                        "target_memory_id": "old",
                        "relation_type": "replaces",
                        "confidence": 0.91,
                        "reason": "当前记忆给出了更新后的偏好",
                    },
                    {
                        "target_memory_id": "other",
                        "relation_type": "none",
                        "confidence": 0.2,
                        "reason": "无关",
                    },
                ]
            },
            ensure_ascii=False,
        )

    store = _MemoryStore(
        {
            "new": {
                "memory_id": "new",
                "summary": "用户现在喜欢喝热拿铁。",
                "entity_tags": ["用户", "拿铁"],
                "topic_tags": ["饮品偏好"],
                "group_id": "1",
                "time_created": 20,
            },
            "old": {
                "memory_id": "old",
                "summary": "用户喜欢喝冰拿铁。",
                "entity_tags": ["用户", "拿铁"],
                "topic_tags": ["饮品偏好"],
                "group_id": "1",
                "time_created": 10,
            },
            "other": {
                "memory_id": "other",
                "summary": "群里昨天聊过游戏更新。",
                "entity_tags": ["游戏"],
                "topic_tags": ["游戏"],
                "group_id": "1",
                "time_created": 9,
            },
        }
    )
    engine = evolves.EvolvesEngine(store, call_ai_api=_call_ai)

    result = asyncio.run(engine.process_memory_async("new", group_id="1"))

    assert len(calls) == 1
    assert [item["relation_type"] for item in result] == ["replaces"]
    assert store.items["old"]["superseded_by"] == "new"
    assert any(link["relation_type"] == "replaces" for link in store.links)


def test_evolves_sync_fallback_is_similarity_only() -> None:
    store = _MemoryStore(
        {
            "new": {
                "memory_id": "new",
                "summary": "用户喜欢安静的咖啡馆和热拿铁。",
                "entity_tags": ["用户", "咖啡馆"],
                "topic_tags": ["偏好"],
                "group_id": "1",
            },
            "old": {
                "memory_id": "old",
                "summary": "用户喜欢安静的咖啡馆。",
                "entity_tags": ["用户", "咖啡馆"],
                "topic_tags": ["偏好"],
                "group_id": "1",
            },
        }
    )
    engine = evolves.EvolvesEngine(store)

    result = engine.process_memory("new", group_id="1")

    assert result
    assert result[0]["relation_type"] in {"confirms", "enriches"}
    assert not any(item["relation_type"] in {"replaces", "challenges"} for item in result)
