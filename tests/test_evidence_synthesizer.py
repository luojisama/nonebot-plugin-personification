from __future__ import annotations

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
