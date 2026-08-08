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

    claims = asyncio.run(slang.SlangLearningPipeline(tool_caller=Caller()).extract_claims(packet))
    assert {claim["term"] for claim in claims} == {"刘涛", "牢大", "大红"}
    assert len({claim["source_cluster_id"] for claim in claims}) == 1


def test_target_extraction_keeps_only_target_and_grounds_explicit_game_context() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    packet = _packet(
        _item(
            "bilibili",
            "BV1",
            [("c1", "花来在三角洲行动里指一种夺取装备的玩法"), ("c2", "好事成双是另一个成就")],
            title="三角洲行动花来解释",
            source_group_id="source-explicit",
        )
    )
    target = _claim("花来", "一种夺取装备的玩法", "BV1", "c1", "花来在三角洲行动里指一种夺取装备的玩法")
    target["game_context"] = {"canonical_name": "未确定游戏", "aliases": []}
    unrelated = _claim("好事成双", "另一个成就", "BV1", "c2", "好事成双是另一个成就")
    payload = {"claims": [unrelated, target]}

    class Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            prompt = messages[1]["content"]
            assert "当前目标词（若非空需优先提取）：花来" in prompt
            assert "当前目标游戏（仅在证据明确支持时填写）：三角洲行动" in prompt
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    pipeline = slang.SlangLearningPipeline(tool_caller=Caller())
    claims = asyncio.run(
        pipeline.extract_claims(
            packet,
            target_term="花来",
            target_game="三角洲行动",
        )
    )

    assert [claim["term"] for claim in claims] == ["花来"]
    assert claims[0]["game_context"]["canonical_name"] == "三角洲行动"
    assert claims[0]["source_cluster_id"] == "source-explicit"
    assert 0 < pipeline.last_extraction_diagnostics["prompt_packet_chars"] <= 6000
    assert pipeline.last_extraction_diagnostics["target_claim_limit"] == 3


def test_target_extraction_timeout_returns_empty_evidence_status() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")

    class Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            await asyncio.sleep(0.2)
            return SimpleNamespace(content='{"claims":[]}')

    pipeline = slang.SlangLearningPipeline(tool_caller=Caller(), extraction_timeout=0.05)
    claims = asyncio.run(
        pipeline.extract_claims(
            _packet(_item("bilibili", "BV1", [("c1", "花来解释")], title="花来")),
            target_term="花来",
            target_game="三角洲行动",
        )
    )

    assert claims == []
    assert pipeline.last_extraction_status == "timeout"


def test_default_target_extraction_budget_covers_observed_routed_model_latency() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")

    assert slang.DEFAULT_EXTRACTION_TIMEOUT_SECONDS == 27.0


def test_extraction_diagnostics_explain_items_removed_before_model_call() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    packet = _packet(_item("bilibili", "BV-filtered", []))
    packet["items"][0]["retained"] = False

    class Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            raise AssertionError("filtered evidence must not invoke the model")

    pipeline = slang.SlangLearningPipeline(tool_caller=Caller())
    claims = asyncio.run(
        pipeline.extract_claims(packet, target_term="花来", target_game="三角洲行动")
    )

    assert claims == []
    assert pipeline.last_extraction_status == "empty"
    assert pipeline.last_extraction_diagnostics == {
        "input_item_count": 1,
        "validated_item_count": 0,
        "selected_item_count": 0,
        "caller_available": True,
        "model_invoked": False,
        "model_claim_count": 0,
        "validated_claim_count": 0,
        "clustered_claim_count": 0,
        "grounded_claim_count": 0,
        "target_claim_count": 0,
        "prompt_packet_chars": 0,
        "target_claim_limit": 0,
    }


def test_target_research_packet_prioritizes_three_compact_cross_platform_items() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    long_body = "前置无关材料" * 500 + "花来指夺取装备后快速撤离的玩法" + "后置无关材料" * 500
    packet = slang.validate_content_packet(
        _packet(
            *[
                _item(
                    "bilibili" if index % 2 == 0 else "xiaoheihe",
                    f"item-{index}",
                    [
                        (f"target-{index}", "花来会保留头甲并拿走装备"),
                        *[(f"other-{index}-{row}", "与目标无关的长评论" * 80) for row in range(12)],
                    ],
                    title=f"三角洲行动花来解释 {index}",
                    caption_or_body=long_body,
                )
                for index in range(8)
            ]
        )
    )

    targeted = slang._target_research_packet(packet, target_term="花来")
    rendered = slang._bounded_packet_json(targeted, max_chars=slang.MAX_TARGET_PACKET_CHARS)

    assert len(targeted["items"]) == 3
    assert {item["platform"] for item in targeted["items"]} == {"bilibili", "xiaoheihe"}
    assert all(len(item["caption_or_body"]) <= slang.MAX_TARGET_BODY_CHARS for item in targeted["items"])
    assert all(
        len(item["discussion"]) <= slang.MAX_TARGET_DISCUSSIONS
        for item in targeted["items"]
    )
    assert all("花来" in item["caption_or_body"] for item in targeted["items"])
    assert len(rendered) <= slang.MAX_TARGET_PACKET_CHARS


def test_target_research_packet_prefers_explanatory_detail_over_repeated_search_noise() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    packet = slang.validate_content_packet(
        _packet(
            *[
                _item(
                    "bilibili",
                    f"noise-{index}",
                    [],
                    title=f"花来 花来 热门视频 {index}",
                    caption_or_body="花来相关视频推荐，点击查看更多花来内容",
                )
                for index in range(6)
            ],
            _item(
                "bilibili",
                "explained",
                [("c-explained", "玩家把这种保留护甲、拿走装备再撤离的玩法称为花来")],
                title="三角洲行动玩法解释",
                caption_or_body="花来指使用低级肉伤弹攻击腿部，击败后夺取装备并快速撤离的玩法。",
            ),
        )
    )

    targeted = slang._target_research_packet(packet, target_term="花来")

    assert targeted["items"][0]["content_id"] == "explained"
    assert len(targeted["items"]) == slang.MAX_TARGET_PACKET_ITEMS


def test_invalid_quote_is_rejected_and_cross_content_evidence_is_split() -> None:
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
    repaired = slang.validate_extracted_claims({"claims": [invalid_quote, cross]}, validated)
    assert [claim["content_key"] for claim in repaired] == ["bilibili:BV1", "douyin:D1"]
    assert all(claim["meaning"] == "六级甲" for claim in repaired)


def test_extractor_schema_normalization_splits_cross_content_claims() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    validated = slang.validate_content_packet(_packet(
        _item("bilibili", "BV1", [("c1", "花来就是完整夺取对方装备")]),
        _item("xiaoheihe", "X1", [("c2", "花来指换上对方装备后撤离")]),
    ))
    payload = {
        "claims": [{
            "term": "花来",
            "aliases": [],
            "meaning": "击败对手后换上其装备并撤离",
            "game_context": "三角洲行动",
            "version_context": "",
            "usage_context": "讨论夺取装备时",
            "safe_usage": "仅在游戏语境使用",
            "risk_level": "低",
            "extractor_confidence": 0.91,
            "evidence_refs": [
                {
                    "packet_id": "packet-one",
                    "platform": "bilibili",
                    "content_id": "BV1",
                    "discussion_id": "c1",
                    "quote": "花来就是完整夺取对方装备",
                },
                {
                    "packet_id": "packet-one",
                    "platform": "xiaoheihe",
                    "content_id": "X1",
                    "discussion_id": "c2",
                    "quote": "花来指换上对方装备后撤离",
                },
            ],
        }],
    }

    claims = slang.validate_extracted_claims(payload, validated)

    assert [claim["content_key"] for claim in claims] == [
        "bilibili:BV1",
        "xiaoheihe:X1",
    ]
    assert {claim["risk_level"] for claim in claims} == {"low"}
    assert {claim["game_context"]["canonical_name"] for claim in claims} == {
        "三角洲行动"
    }
    assert len({claim["meaning"] for claim in claims}) == 1


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


def test_semantic_validation_confirms_compatible_detail_claims_from_two_origins() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    packet = _packet(
        _item("bilibili", "BV1", [("c1", "刘涛指六级防具")], detail_status="ready"),
        _item("tieba", "T1", [("c2", "刘涛就是六级甲")], detail_status="ready"),
    )
    claims = [
        _claim("刘涛", "六级防具", "BV1", "c1", "刘涛指六级防具"),
        _claim("刘涛", "六级甲", "T1", "c2", "刘涛就是六级甲", "tieba"),
    ]
    claims[0]["source_cluster_id"] = "source-one"
    claims[1]["source_cluster_id"] = "source-two"

    result = slang.build_semantic_validation(
        target_term="刘涛",
        target_game="三角洲行动",
        target_claims=claims,
        target_senses=[
            {"sense_id": "sense-one", "meaning": "六级防具", "status": "understand_only"},
            {"sense_id": "sense-one", "meaning": "六级防具", "status": "understand_only"},
        ],
        packet=packet,
    )

    assert result["status"] == "confirmed"
    assert result["satisfies_request"] is True
    assert result["supporting_source_group_count"] == 2
    assert result["supporting_origins"] == ["bilibili", "tieba"]
    assert result["gap_codes"] == []


def test_semantic_validation_does_not_confirm_search_cards_without_detail() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    packet = _packet(
        _item("bilibili", "BV1", [("c1", "刘涛指六级防具")], detail_status="detail_content_unavailable"),
        _item("tieba", "T1", [("c2", "刘涛就是六级甲")], detail_status="detail_content_unavailable"),
    )
    claims = [
        _claim("刘涛", "六级防具", "BV1", "c1", "刘涛指六级防具"),
        _claim("刘涛", "六级甲", "T1", "c2", "刘涛就是六级甲", "tieba"),
    ]
    claims[0]["source_cluster_id"] = "source-one"
    claims[1]["source_cluster_id"] = "source-two"

    result = slang.build_semantic_validation(
        target_term="刘涛",
        target_game="三角洲行动",
        target_claims=claims,
        target_senses=[{"sense_id": "sense-one", "meaning": "六级防具", "status": "understand_only"}],
        packet=packet,
    )

    assert result["status"] == "insufficient"
    assert result["satisfies_request"] is False
    assert "detail_evidence_missing" in result["gap_codes"]


def test_semantic_validation_reports_conflicting_senses_even_when_coverage_is_high() -> None:
    slang = load_personification_module("plugin.personification.core.slang_learning")
    packet = _packet(
        _item("bilibili", "BV1", [("c1", "刘涛指六级防具")], detail_status="ready"),
        _item("douyin", "D1", [("c2", "刘涛指另一个互斥玩法")], detail_status="ready"),
    )
    packet["aggregation"] = {"satisfies_request": True, "source_group_count": 2}
    claims = [
        _claim("刘涛", "六级防具", "BV1", "c1", "刘涛指六级防具"),
        _claim("刘涛", "另一个互斥玩法", "D1", "c2", "刘涛指另一个互斥玩法", "douyin"),
    ]
    claims[0]["source_cluster_id"] = "source-one"
    claims[1]["source_cluster_id"] = "source-two"

    result = slang.build_semantic_validation(
        target_term="刘涛",
        target_game="三角洲行动",
        target_claims=claims,
        target_senses=[
            {"sense_id": "sense-one", "meaning": "六级防具", "status": "observed"},
            {"sense_id": "sense-two", "meaning": "另一个互斥玩法", "status": "observed"},
        ],
        packet=packet,
    )

    assert result["status"] == "conflict"
    assert result["satisfies_request"] is False
    assert "semantic_conflict" in result["gap_codes"]
