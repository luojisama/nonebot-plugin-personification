from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
data_store = load_personification_module("plugin.personification.core.data_store")
memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
meme_dictionary = load_personification_module("plugin.personification.core.meme_dictionary")
group_knowledge = load_personification_module("plugin.personification.core.group_knowledge")


def _init_store(tmp_path):
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=True,
    )
    data_store.init_data_store(cfg)
    db.init_db_sync(tmp_path)
    store = memory_store_mod.MemoryStore(plugin_config=cfg, logger=SimpleNamespace(warning=lambda *_a, **_k: None))
    store.initialize()
    return cfg, store


def test_public_meme_seed_query(tmp_path) -> None:
    _init_store(tmp_path)
    meme_dictionary.ensure_public_meme_seeds()

    entries = meme_dictionary.query_meme_dictionary("", "这个操作太典了，有点绷不住")

    terms = {entry["term"] for entry in entries}
    assert "典" in terms
    assert "绷" in terms


def test_group_knowledge_extracts_group_meme_and_hint(tmp_path) -> None:
    _cfg, store = _init_store(tmp_path)

    class _Caller:
        async def chat_with_tools(self, **_kwargs):
            return SimpleNamespace(
                content='[{"term":"猫车","definition":"群里指测试车翻车","aliases":["上猫车"],'
                        '"is_meme":true,"scope":"group","tone":["吐槽"],"risk_level":"low","safe_usage":"只在测试上下文用"}]',
                usage={},
            )

    saved = asyncio.run(
        group_knowledge.build_group_knowledge(
            tool_caller=_Caller(),
            memory_store=store,
            group_id="g1",
            chat_summary="u1: 今天又上猫车了",
        )
    )

    assert saved == 1
    entries = meme_dictionary.query_meme_dictionary("g1", "刚才是不是又上猫车了")
    assert entries[0]["term"] == "猫车"
    assert entries[0]["safe_usage"] == "只在测试上下文用"

    hint = group_knowledge.format_group_knowledge_hint(
        group_knowledge.query_group_knowledge(store, "g1", "又上猫车了")
    )
    assert "群聊梗/概念锚点参考" in hint
    assert "可轻量试探使用" in hint


def test_meme_hint_confidence_rules(tmp_path) -> None:
    _init_store(tmp_path)
    meme_dictionary.upsert_meme_entry(
        {
            "term": "低置信词",
            "meaning": "不确定的群内说法",
            "scope": "group",
            "group_id": "g2",
            "confidence": 0.4,
        }
    )

    entries = meme_dictionary.query_meme_dictionary("g2", "低置信词是啥")
    hint = meme_dictionary.format_meme_hint(entries)

    assert "只理解不主动使用" in hint
