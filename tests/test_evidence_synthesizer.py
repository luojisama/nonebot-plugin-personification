from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module

evidence = load_personification_module("plugin.personification.agent.runtime.evidence")


def test_parse_evidence_synthesis_filters_unknown_memory_ids() -> None:
    parsed = evidence.parse_evidence_synthesis_payload(
        {
            "selected_memory_ids": ["m1", "missing"],
            "memory_inject_style": "softened",
            "tool_evidence_digest": "x" * 260,
            "uncertainty_notes": ["来源冲突"],
            "needs_more_research": True,
            "research_followup_query": "换个关键词",
        },
        candidate_memories=[{"memory_id": "m1"}],
    )

    assert parsed is not None
    assert parsed.selected_memory_ids == ["m1"]
    assert parsed.memory_inject_style == "softened"
    assert len(parsed.tool_evidence_digest) == 200
    assert parsed.needs_more_research is True


def test_fallback_evidence_synthesis_drops_risky_memories_and_digest_tools() -> None:
    result = evidence.fallback_evidence_synthesis(
        candidate_memories=[
            {"memory_id": "safe", "summary": "ok", "tone_risk": 0.1},
            {"memory_id": "risky", "summary": "bad", "tone_risk": 0.9},
        ],
        tool_results=[
            {"tool_name": "web_search", "result": "第一条结果\n第二条结果"},
        ],
    )

    assert result.selected_memory_ids == ["safe"]
    assert "web_search" in result.tool_evidence_digest
    assert result.needs_more_research is False


def test_render_evidence_guidance_includes_followup_query() -> None:
    item = evidence.EvidenceSynthesis(
        selected_memory_ids=["m1"],
        memory_inject_style="factual",
        tool_evidence_digest="已有证据",
        uncertainty_notes=["缺少二次来源"],
        needs_more_research=True,
        research_followup_query="继续查官方公告",
    )

    text = evidence.render_evidence_guidance(item)

    assert "m1" in text
    assert "已有证据" in text
    assert "继续查官方公告" in text


def test_build_tool_result_record_truncates_result_text() -> None:
    record = evidence.build_tool_result_record(
        tool_name="web_search",
        tool_args={"query": "abc"},
        result="x" * 2500,
    )

    assert record["tool_name"] == "web_search"
    assert record["args"] == {"query": "abc"}
    assert len(record["result"]) == 2400


def _social_packet() -> dict:
    return {
        "schema_version": 1,
        "partial": True,
        "warnings": ["xiaoheihe:request_timeout"],
        "aggregation": {
            "requested_limit": 10,
            "candidate_count": 14,
            "returned_count": 2,
            "source_group_count": 2,
            "selected_platforms": ["xiaoheihe", "bilibili"],
            "successful_platforms": ["xiaoheihe", "bilibili"],
            "covered_platforms": ["xiaoheihe", "bilibili"],
            "coverage_status": "degraded",
            "satisfies_request": True,
        },
        "source_groups": [
            {
                "group_id": "source_xhh",
                "members": [
                    {
                        "platform": "xiaoheihe",
                        "content_id": "179364001",
                        "canonical_url": "https://xiaoheihe.cn/app/bbs/link/179364001",
                    }
                ],
            },
            {
                "group_id": "source_bili",
                "members": [
                    {
                        "platform": "bilibili",
                        "content_id": "BV1abc",
                        "canonical_url": "https://www.bilibili.com/video/BV1abc/",
                    }
                ],
            },
        ],
        "items": [
            {
                "platform": "xiaoheihe",
                "content_id": "179364001",
                "source_group_id": "source_xhh",
                "title": "花来出处",
                "canonical_url": "https://xiaoheihe.cn/app/bbs/link/179364001",
                "caption_or_body": "x" * 5000,
            },
            {
                "platform": "bilibili",
                "content_id": "BV1abc",
                "source_group_id": "source_bili",
                "title": "另一来源",
                "canonical_url": "https://www.bilibili.com/video/BV1abc/",
            },
        ],
    }


def test_social_record_preserves_validated_sources_outside_truncated_json() -> None:
    record = evidence.build_tool_result_record(
        tool_name="social_content_search",
        tool_args={"query": "花来"},
        result=json.dumps(_social_packet(), ensure_ascii=False),
    )

    assert len(record["result"]) == 2400
    assert record["social_evidence"]["aggregation"]["source_group_count"] == 2
    assert record["social_evidence"]["aggregation"]["satisfies_request"] is True
    assert [item["source_group_id"] for item in record["social_evidence"]["sources"]] == [
        "source_xhh",
        "source_bili",
    ]
    assert evidence._independent_source_count([record]) == 2
    rendered = evidence._render_tool_results([record])
    assert "[社交证据元数据]" in rendered
    assert "https://xiaoheihe.cn/app/bbs/link/179364001" in rendered


def test_social_record_rejects_non_content_and_credential_urls() -> None:
    packet = _social_packet()
    packet["items"] = [
        {
            "platform": "xiaoheihe",
            "content_id": "bad",
            "canonical_url": "https://user:pass@xiaoheihe.cn/app/bbs/home",
        },
        {
            "platform": "tieba",
            "content_id": "bad",
            "canonical_url": "https://tieba.baidu.com/f/search/res?qw=test",
        },
    ]
    record = evidence.build_tool_result_record(
        tool_name="social_content_search",
        tool_args={},
        result=json.dumps(packet),
    )

    assert record["social_evidence"]["sources"] == []


def test_social_evidence_consolidation_keeps_satisfied_search_when_read_follows() -> None:
    search = evidence.build_tool_result_record(
        tool_name="social_content_search",
        tool_args={},
        result=json.dumps(_social_packet(), ensure_ascii=False),
    )
    read_packet = {
        "schema_version": 1,
        "items": [
            {
                "platform": "xiaoheihe",
                "content_id": "179364001",
                "canonical_url": "https://xiaoheihe.cn/app/bbs/link/179364001",
                "title": "详情",
            }
        ],
    }
    read = evidence.build_tool_result_record(
        tool_name="social_content_read",
        tool_args={},
        result=json.dumps(read_packet, ensure_ascii=False),
    )

    consolidated = evidence.social_evidence_from_records([search, read])

    assert consolidated["satisfies_request"] is True
    assert consolidated["aggregation"]["source_group_count"] == 2
    assert len(consolidated["sources"]) == 2


def test_strict_evidence_keeps_research_open_with_one_independent_source() -> None:
    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            assert '"evidence_policy": "strict"' in messages[1]["content"]
            return SimpleNamespace(
                content='{"selected_memory_ids":[],"tool_evidence_digest":"单一结果",'
                '"uncertainty_notes":[],"needs_more_research":false,"research_followup_query":""}'
            )

    result = asyncio.run(
        evidence.synthesize_evidence_with_llm(
            tool_caller=_Caller(),
            turn_plan=SimpleNamespace(evidence_policy="strict", domain_focus="science", session_goal="核验结论"),
            tool_results=[{"tool_name": "web_search", "result": "https://example.com/report"}],
        )
    )

    assert result.needs_more_research is True
    assert result.research_followup_query == "核验结论"
    assert "独立来源不足" in result.uncertainty_notes
