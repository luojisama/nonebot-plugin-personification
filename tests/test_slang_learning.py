from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from ._loader import load_personification_module


def _packet(*items: dict) -> dict:
    return {
        "schema_version": 1,
        "packet_id": "packet-one",
        "trust": "untrusted_data_only",
        "retrieved_at": time.time(),
        "expires_at": time.time() + 3600,
        "partial": False,
        "platform_statuses": {},
        "items": list(items),
        "filtered_counts": {},
        "warnings": [],
    }


def _item(platform: str, content_id: str, discussions: list[tuple[str, str]], **extra) -> dict:
    return {
        "platform": platform,
        "content_type": "video" if platform in {"bilibili", "douyin"} else "article",
        "content_id": content_id,
        "canonical_url": f"https://example.invalid/{content_id}",
        "title": "三角洲黑话集中解释",
        "caption_or_body": "本期讨论玩家社区黑话。",
        "retained": True,
        "quality_score": 0.8,
        "discussion": [
            {"discussion_id": discussion_id, "type": "comment", "text": text}
            for discussion_id, text in discussions
        ],
        **extra,
    }


def _claim(term: str, meaning: str, content_id: str, discussion_id: str, quote: str, platform: str = "bilibili") -> dict:
    return {
        "term": term,
        "aliases": [],
        "meaning": meaning,
        "game_context": {"canonical_name": "三角洲行动", "aliases": ["三角洲"]},
        "version_context": "",
        "usage_context": "玩家讨论装备时",
        "safe_usage": "仅在三角洲行动语境使用",
        "risk_level": "low",
        "extractor_confidence": 0.92,
        "evidence_refs": [{
            "packet_id": "packet-one",
            "platform": platform,
            "content_id": content_id,
            "discussion_id": discussion_id,
            "quote": quote,
        }],
    }


def test_one_content_extracts_multiple_slang_claims() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    packet = _packet(_item("bilibili", "BV1", [
        ("c1", "刘涛就是六级甲"),
        ("c2", "牢大在这段话里指威龙"),
        ("c3", "大红说的是高价值红色物资"),
    ]))
    payload = {"claims": [
        _claim("刘涛", "六级防具或六级护甲", "BV1", "c1", "刘涛就是六级甲"),
        _claim("牢大", "威龙干员的玩家外号", "BV1", "c2", "牢大在这段话里指威龙"),
        _claim("大红", "高价值红色物资", "BV1", "c3", "大红说的是高价值红色物资"),
    ]}

    class Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            assert "untrusted_data_only" in messages[1]["content"]
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    claims = asyncio.run(slang.SlangLearningPipeline(tool_caller=Caller()).extract_claims(packet, target_term="刘涛"))
    assert {claim["term"] for claim in claims} == {"刘涛", "牢大", "大红"}
    assert len({claim["source_cluster_id"] for claim in claims}) == 1


def test_invalid_or_cross_content_evidence_is_rejected() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    validated = slang.validate_content_packet(_packet(
        _item("bilibili", "BV1", [("c1", "刘涛就是六级甲")]),
        _item("douyin", "D1", [("c2", "刘涛就是六套")]),
    ))
    invalid_quote = _claim("刘涛", "六级甲", "BV1", "c1", "并不存在的原文")
    cross = _claim("刘涛", "六级甲", "BV1", "c1", "刘涛就是六级甲")
    cross["evidence_refs"].append({
        "packet_id": "packet-one", "platform": "douyin", "content_id": "D1", "discussion_id": "c2", "quote": "刘涛就是六套"
    })
    assert slang.validate_extracted_claims({"claims": [invalid_quote, cross]}, validated) == []


def test_comments_in_one_video_count_once_and_reposts_merge_across_platforms() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    repeated = [(f"c{index}", "刘涛就是六级甲") for index in range(10)]
    packet = slang.validate_content_packet(_packet(
        _item("bilibili", "BV1", repeated, media_fingerprint="same-media"),
        _item("douyin", "D1", [("d1", "刘涛就是六套")], media_fingerprint="same-media"),
        _item("xiaoheihe", "X1", [("x1", "刘涛指六级防具")], content_fingerprint="independent"),
    ))
    claims = []
    for discussion_id, quote in repeated:
        claims.append(_claim("刘涛", "六级甲", "BV1", discussion_id, quote))
    claims.append(_claim("刘涛", "六级甲", "D1", "d1", "刘涛就是六套", "douyin"))
    claims.append(_claim("刘涛", "六级防具", "X1", "x1", "刘涛指六级防具", "xiaoheihe"))
    validated = slang.validate_extracted_claims({"claims": claims}, packet, max_claims=20)
    clustered = slang.attach_source_clusters(validated, packet)
    assert slang.independent_source_count(clustered) == 2
    assert {claim["source_cluster_id"] for claim in clustered if claim["content_key"] in {"bilibili:BV1", "douyin:D1"}}.__len__() == 1


def test_semantic_comparison_requires_fixed_enum() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")

    class Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            return SimpleNamespace(content='{"relation":"compatible","confidence":0.88,"reason":"六级甲与六级防具兼容"}')

    result = asyncio.run(slang.SlangLearningPipeline(tool_caller=Caller()).compare_senses(
        {"term": "刘涛", "meaning": "六级甲"}, {"term": "刘涛", "meaning": "六级防具"}
    ))
    assert result == {"relation": "compatible", "confidence": 0.88, "reason": "六级甲与六级防具兼容"}


def test_discovery_queue_is_bounded_per_content_and_skips_target() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    handled = []

    async def run() -> tuple[int, list]:
        async def handler(task):
            handled.append(task)

        queue = slang.BoundedSlangDiscoveryQueue(handler, max_global=10, max_per_content=2, concurrency=1)
        claims = []
        for term in ("刘涛", "牢大", "大红", "哈基米"):
            claim = _claim(term, "解释", "BV1", "c1", "原文")
            claim["content_key"] = "bilibili:BV1"
            claims.append(claim)
        added = queue.schedule_claims(claims, target_term="刘涛")
        await queue.join()
        await queue.close()
        return added, handled

    added, tasks = asyncio.run(run())
    assert added == 2
    assert [task.term for task in tasks] == ["牢大", "大红"]
