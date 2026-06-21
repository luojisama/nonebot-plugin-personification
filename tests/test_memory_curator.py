from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

memory_curator_mod = load_personification_module("plugin.personification.core.memory_curator")


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def palace_enabled(self) -> bool:
        return True

    def write_memory_item(self, item: dict[str, object]) -> str:
        self.items.append(dict(item))
        return "m1"


def test_capture_turn_writes_episodic_turn_memory() -> None:
    store = _FakeMemoryStore()
    curator = memory_curator_mod.MemoryCurator(store)

    asyncio.run(
        curator.capture_turn(
            user_utterance="帮我查一下 A",
            bot_response="A 现在比较热。",
            user_id="u1",
            group_id="g1",
            evidence_refs=["tool:web_search"],
            semantic_frame=SimpleNamespace(
                chat_intent="lookup",
                ambiguity_level="low",
                output_mode="source_summary",
            ),
        )
    )

    assert len(store.items) == 1
    item = store.items[0]
    assert item["memory_type"] == "episodic_turn"
    assert item["user_utterance"] == "帮我查一下 A"
    assert item["bot_response"] == "A 现在比较热。"
    assert item["evidence_refs"] == ["tool:web_search"]
    assert item["semantic_frame"] == {
        "chat_intent": "lookup",
        "ambiguity_level": "low",
        "output_mode": "source_summary",
    }
    assert item["permission_type"] == "public_preference"


def test_capture_turn_emotional_care_is_private_fact() -> None:
    store = _FakeMemoryStore()
    curator = memory_curator_mod.MemoryCurator(store)

    asyncio.run(
        curator.capture_turn(
            user_utterance="今天有点难受",
            bot_response="先缓一下。",
            user_id="u1",
            group_id="g1",
            semantic_frame=SimpleNamespace(
                chat_intent="banter",
                domain_focus="emotion",
                requires_emotional_care=True,
            ),
        )
    )

    assert store.items[0]["permission_type"] == "private_fact"
