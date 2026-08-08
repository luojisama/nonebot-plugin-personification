from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
data_store = load_personification_module("plugin.personification.core.data_store")
dictionary = load_personification_module("plugin.personification.core.meme_dictionary")
learning = load_personification_module("plugin.personification.core.meme_learning_store")
slang = load_personification_module("plugin.personification.core.slang_learning")


def _init(tmp_path):
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)
    db.init_db_sync(tmp_path)
    return learning.MemeLearningStore()


def _claim(
    *,
    term: str = "刘涛",
    meaning: str = "六级防具",
    game: str = "三角洲行动",
    version: str = "",
    platform: str,
    content_id: str,
    cluster: str | None = None,
    confidence: float = 0.92,
) -> dict:
    source_cluster = cluster or f"source-{platform}-{content_id}"
    return {
        "term": term,
        "aliases": [],
        "meaning": meaning,
        "game_context": {"canonical_name": game, "aliases": ["三角洲"] if game == "三角洲行动" else []},
        "version_context": version,
        "usage_context": "讨论装备时",
        "safe_usage": f"仅在{game}语境中使用",
        "risk_level": "low",
        "extractor_confidence": confidence,
        "content_key": f"{platform}:{content_id}",
        "source_cluster_id": source_cluster,
        "source": {
            "canonical_url": f"https://example.invalid/{content_id}",
            "content_type": "video" if platform in {"bilibili", "douyin"} else "article",
            "content_fingerprint": f"fp-{content_id}",
            "quality_score": 0.85,
        },
        "evidence_refs": [{
            "packet_id": f"packet-{content_id}",
            "platform": platform,
            "content_id": content_id,
            "discussion_id": f"comment-{content_id}",
            "quote": f"{term}指{meaning}",
        }],
    }


class _CompatiblePipeline:
    async def compare_senses(self, left, right):
        return {"relation": "compatible", "confidence": 0.91, "reason": "同一装备含义"}


class _ConflictPipeline:
    async def compare_senses(self, left, right):
        return {"relation": "conflict", "confidence": 0.94, "reason": "同语境下含义互斥"}


def _ingest(store, claims, pipeline=None, now=1000):
    return asyncio.run(store.ingest_claims(
        claims,
        semantic_pipeline=pipeline or _CompatiblePipeline(),
        now=now,
        model_route="test-route",
    ))


def test_two_independent_contents_understand_and_three_cross_platform_verify(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [
        _claim(platform="bilibili", content_id="B1"),
        _claim(platform="bilibili", content_id="B2"),
    ])
    sense = store.list_senses(term="刘涛")[0]
    assert sense["status"] == "understand_only"
    assert sense["source_count"] == 2
    assert sense["platform_count"] == 1

    _ingest(store, [_claim(platform="douyin", content_id="D1")])
    sense = store.list_senses(term="刘涛")[0]
    assert sense["status"] == "verified"
    assert sense["source_count"] == 3
    assert sense["platform_count"] == 2


def test_same_content_cluster_never_counts_twice(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [
        _claim(platform="bilibili", content_id="B1", cluster="copied-one"),
        _claim(platform="bilibili", content_id="B1", cluster="copied-one"),
    ])
    sense = store.list_senses(term="刘涛")[0]
    assert sense["source_count"] == 1
    assert sense["status"] == "observed"


def test_different_games_and_versions_create_distinct_senses(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [
        _claim(term="牢大", meaning="角色外号", game="三角洲行动", version="S7", platform="bilibili", content_id="B1"),
        _claim(term="牢大", meaning="另一游戏人物梗", game="篮球游戏", version="", platform="douyin", content_id="D1"),
        _claim(term="牢大", meaning="旧赛季装备称呼", game="三角洲行动", version="S6", platform="xiaoheihe", content_id="X1"),
    ])
    senses = store.list_senses(term="牢大")
    assert len(senses) == 3
    assert {(item["game_context"]["canonical_name"], item["version_context"]) for item in senses} == {
        ("三角洲行动", "S7"), ("三角洲行动", "S6"), ("篮球游戏", "")
    }


def test_two_supported_conflicting_senses_become_disputed(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [
        _claim(term="同词", meaning="解释甲", platform="bilibili", content_id="B1"),
        _claim(term="同词", meaning="解释甲", platform="xiaoheihe", content_id="X1"),
    ], _ConflictPipeline())
    _ingest(store, [
        _claim(term="同词", meaning="解释乙", platform="douyin", content_id="D1"),
        _claim(term="同词", meaning="解释乙", platform="tieba", content_id="T1"),
    ], _ConflictPipeline())
    senses = store.list_senses(term="同词")
    assert len(senses) == 2
    assert {item["status"] for item in senses} == {"disputed"}
    assert {item["source_count"] for item in senses} == {2}


def test_supported_consensus_marks_single_source_conflict_disputed(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(
        store,
        [_claim(term="旧词", meaning="旧的不完整解释", platform="bilibili", content_id="B1")],
        _ConflictPipeline(),
    )
    _ingest(
        store,
        [
            _claim(term="旧词", meaning="新的完整解释", platform="xiaoheihe", content_id="X1"),
            _claim(term="旧词", meaning="新的完整解释", platform="tieba", content_id="T1"),
        ],
        _ConflictPipeline(),
    )

    senses = store.list_senses(term="旧词")
    old = next(item for item in senses if item["meaning"] == "旧的不完整解释")
    current = next(item for item in senses if item["meaning"] == "新的完整解释")
    assert old["status"] == "disputed"
    assert old["source_count"] == 1
    assert current["status"] == "understand_only"
    assert current["source_count"] == 2


def test_structured_web_fact_evidence_can_raise_existing_sense_without_search_summaries(tmp_path) -> None:
    _init(tmp_path)

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            packet = json.loads(messages[1]["content"].split("content_packet=", 1)[1])
            claims = []
            for item in packet["items"]:
                quote = item["caption_or_body"].split("原文摘录：", 1)[1]
                claims.append(
                    {
                        "term": "测试黑话",
                        "aliases": [],
                        "meaning": "一种经多个网页正文支持的玩法",
                        "game_context": {"canonical_name": "测试游戏", "aliases": []},
                        "version_context": "",
                        "usage_context": "玩家讨论玩法时",
                        "safe_usage": "仅在测试游戏语境使用",
                        "risk_level": "low",
                        "extractor_confidence": 0.95,
                        "evidence_refs": [
                            {
                                "packet_id": packet["packet_id"],
                                "platform": item["platform"],
                                "content_id": item["content_id"],
                                "discussion_id": "",
                                "quote": quote,
                            }
                        ],
                    }
                )
            return SimpleNamespace(content=json.dumps({"claims": claims}, ensure_ascii=False))

    result = asyncio.run(
        slang.ingest_web_fact_evidence(
            fact_evidence=[
                {
                    "claim": "测试黑话是一种玩法",
                    "support": [
                        {
                            "canonical_url": "https://one.example/article",
                            "title": "来源一",
                            "quote": "测试黑话描述这种玩法的具体执行方式。",
                            "content_fingerprint": "1" * 64,
                            "evidence_origin": "web:one.example",
                            "source_group_id": "web-source-one",
                        },
                        {
                            "canonical_url": "https://two.example/guide",
                            "title": "来源二",
                            "quote": "测试黑话就是该玩法在社区里的称呼。",
                            "content_fingerprint": "2" * 64,
                            "evidence_origin": "web:two.example",
                            "source_group_id": "web-source-two",
                        },
                    ],
                }
            ],
            target_term="测试黑话",
            target_game="测试游戏",
            tool_caller=_Caller(),
        )
    )

    assert result["ingested_claim_count"] == 2
    assert result["semantic_validation"]["status"] == "confirmed"
    assert result["semantic_validation"]["satisfies_request"] is True
    sense = learning.MemeLearningStore().list_senses(term="测试黑话")[0]
    assert sense["status"] == "understand_only"
    assert sense["source_count"] == 2
    assert sense["platform_count"] == 2


def test_manual_locked_sense_is_not_overwritten_by_conflict(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [_claim(term="锁定词", meaning="人工含义", platform="bilibili", content_id="B1")])
    original = store.list_senses(term="锁定词")[0]
    locked = store.set_manual_status(original["sense_id"], status="manual_locked", actor="admin")
    _ingest(store, [_claim(term="锁定词", meaning="冲突含义", platform="douyin", content_id="D1")], _ConflictPipeline())
    unchanged = store.get_sense(locked["sense_id"])
    assert unchanged["status"] == "manual_locked"
    assert unchanged["meaning"] == "人工含义"


def test_verified_sense_becomes_stale_without_deletion(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [
        _claim(platform="bilibili", content_id="B1"),
        _claim(platform="xiaoheihe", content_id="X1"),
        _claim(platform="douyin", content_id="D1"),
    ], now=1000)
    sense = store.list_senses(term="刘涛")[0]
    assert sense["status"] == "verified"
    changed = store.run_maintenance(now=1000 + 91 * 86400)
    stale = store.get_sense(sense["sense_id"])
    assert changed == 1
    assert stale["status"] == "stale"
    assert stale["source_count"] == 3


def test_liutao_three_platform_example_is_context_scoped(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [
        _claim(meaning="六级甲", platform="bilibili", content_id="A"),
        _claim(meaning="六级防具", platform="xiaoheihe", content_id="B"),
        _claim(meaning="六套或六级护甲", platform="douyin", content_id="C"),
    ], _CompatiblePipeline())
    senses = store.list_senses(term="刘涛")
    assert len(senses) == 1
    assert senses[0]["status"] == "verified"
    assert senses[0]["source_count"] == 3
    assert senses[0]["platform_count"] == 3

    game_hits = dictionary.query_meme_dictionary("", "刘涛是什么", game_context="三角洲行动")
    person_hits = dictionary.query_meme_dictionary("", "演员刘涛的新剧")
    assert game_hits and game_hits[0]["sense_id"] == senses[0]["sense_id"]
    assert person_hits == []

    # Re-running idempotent DB migrations must not turn an auto-managed root into a manual lock.
    db.init_db_sync(tmp_path)
    after_restart = store.list_senses(term="刘涛")
    assert len(after_restart) == 1
    assert after_restart[0]["status"] == "verified"


def test_low_confidence_claim_is_not_recorded(tmp_path) -> None:
    store = _init(tmp_path)
    result = _ingest(store, [_claim(platform="bilibili", content_id="B1", confidence=0.5)])
    assert result == []
    assert store.list_senses(term="刘涛") == []


def test_existing_dictionary_entry_migrates_to_manual_locked_sense(tmp_path) -> None:
    store = _init(tmp_path)
    dictionary.upsert_meme_entry({
        "term": "人工词条",
        "meaning": "管理员写入的含义",
        "scope": "public",
        "confidence": 0.9,
    })
    db.init_db_sync(tmp_path)
    senses = store.list_senses(term="人工词条")
    assert len(senses) == 1
    assert senses[0]["status"] == "manual_locked"
    assert senses[0]["manual_locked"] is True


def test_admin_merge_and_split_preserve_evidence_with_revision_guards(tmp_path) -> None:
    store = _init(tmp_path)
    _ingest(store, [
        _claim(term="合并词", meaning="解释甲", platform="bilibili", content_id="B1"),
        _claim(term="合并词", meaning="解释乙", platform="douyin", content_id="D1"),
    ], _ConflictPipeline())
    senses = store.list_senses(term="合并词")
    assert len(senses) == 2
    target, source = senses
    merged = store.merge_senses(
        target_sense_id=target["sense_id"],
        source_sense_ids=[source["sense_id"]],
        expected_revisions={target["sense_id"]: target["revision"], source["sense_id"]: source["revision"]},
        actor="admin",
    )
    assert merged["status"] == "manual_locked"
    assert len(merged["evidence"]) == 2
    rejected = store.get_sense(source["sense_id"])
    assert rejected["status"] == "rejected"

    claim_id = merged["evidence"][0]["claim_id"]
    split = store.split_sense(
        merged["sense_id"],
        claim_ids=[claim_id],
        patch={"meaning": "拆分后的独立解释"},
        expected_revision=merged["revision"],
        actor="admin",
    )
    assert split["status"] == "manual_locked"
    assert split["meaning"] == "拆分后的独立解释"
    assert len(split["evidence"]) == 1
