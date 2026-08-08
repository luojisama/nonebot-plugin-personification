from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module

stop_flow = load_personification_module("plugin.personification.agent.runtime.stop_flow")


class _Registry:
    def __init__(self, tool=None):
        self.tool = tool

    def get(self, _name: str):  # noqa: ANN001
        return self.tool


class _LookupTool:
    local = True

    def __init__(self, result, *, properties=None, retryable=True):
        self._result = result
        self.metadata = {
            "category": "retrieval",
            "intent_tags": ["lookup"],
            "evidence_kind": "web",
            "side_effect": "none",
            "retryable": retryable,
        }
        self.parameters = {
            "type": "object",
            "properties": properties or {},
            "required": [],
        }
        self.handler = lambda: None
        self.handler.__tool_metadata__ = self.metadata
        self.__tool_schema__ = {
            "function": {
                "name": "lookup_tool",
                "description": "Lookup information",
                "parameters": self.parameters,
            }
        }

    async def call(self, **_kwargs):
        return self._result


async def _no_lookup(**_kwargs):  # noqa: ANN001
    return None


async def _unused_classifier(**_kwargs):  # noqa: ANN001
    raise AssertionError("classifier callback should not run")


async def _append_evidence_guidance(**_kwargs):  # noqa: ANN001
    return None


def _stop_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        finish_reason="stop",
        content=content,
        tool_calls=[],
        vision_unavailable=False,
    )


def _run_stop_handler(
    *,
    state,
    response,
    content_len: int,
    runtime_chat_intent: str = "lookup",
    registry=None,
    select_semantic_fallback_tool=_no_lookup,
):
    traces: list[dict] = []
    decision = asyncio.run(
        stop_flow.handle_model_stop(
            state=state,
            response=response,
            content_len=content_len,
            active_schemas=[],
            runtime_chat_intent=runtime_chat_intent,
            intent_decision=SimpleNamespace(ambiguity_level="low"),
            registry=registry or _Registry(),
            tool_caller=SimpleNamespace(),
            logger=SimpleNamespace(info=lambda _msg: None, warning=lambda _msg: None),
            messages=[],
            pending_actions=[],
            plugin_config=SimpleNamespace(personification_fallback_enabled=False),
            user_query_text="问题",
            user_text="问题",
            user_images=[],
            rewritten_query=None,
            context_hint="",
            plugin_query_intent="",
            budget_deadline=None,
            step=1,
            record_trace=lambda **kwargs: traces.append(kwargs),
            append_evidence_guidance=_append_evidence_guidance,
            classify_deferred_lookup_reply=_unused_classifier,
            select_semantic_fallback_tool=select_semantic_fallback_tool,
        )
    )
    return decision, traces


def _select_fallback(
    *,
    state,
    registry,
    selection,
    budget_deadline=None,
    semantic_research_target_deadline=None,
):
    async def _selector(**_kwargs):  # noqa: ANN001
        return selection

    return asyncio.run(
        stop_flow._select_stop_fallback_lookup(
            state=state,
            response=_stop_response("没查到结果"),
            content_len=5,
            runtime_chat_intent="lookup",
            banter_requires_lookup_retry=False,
            user_query_text="问题",
            rewritten_query=None,
            context_hint="",
            user_images=[],
            plugin_query_intent="",
            tool_caller=SimpleNamespace(),
            registry=registry,
            record_trace=lambda **_kwargs: None,
            logger=SimpleNamespace(info=lambda _msg: None),
            select_semantic_fallback_tool=_selector,
            budget_deadline=budget_deadline,
            semantic_research_target_deadline=semantic_research_target_deadline,
        )
    )


def test_should_review_banter_lookup_draft_uses_structural_signals() -> None:
    assert stop_flow._should_review_banter_lookup_draft(ambiguity_level="high", draft_answer_text="知道了")
    assert stop_flow._should_review_banter_lookup_draft(ambiguity_level="low", draft_answer_text="这是啥？")
    assert not stop_flow._should_review_banter_lookup_draft(ambiguity_level="low", draft_answer_text="接一句")


def test_satisfied_social_packet_blocks_semantic_fallback_search() -> None:
    state = stop_flow.StopFlowState(has_tool_call=True)
    packet = {
        "aggregation": {
            "source_group_count": 2,
            "covered_platforms": ["xiaoheihe", "bilibili"],
            "satisfies_request": True,
        },
        "items": [
            {
                "platform": "xiaoheihe",
                "content_id": "179364001",
                "canonical_url": "https://xiaoheihe.cn/app/bbs/link/179364001",
            }
        ],
    }
    stop_flow.update_stop_flow_tool_result(
        state=state,
        registry=_Registry(),
        tool_name="social_content_search",
        tool_args={"query": "花来"},
        result=json.dumps(packet),
    )

    async def _unexpected_selector(**_kwargs):  # noqa: ANN001
        raise AssertionError("satisfied structured social evidence must stop fallback lookup")

    selected = asyncio.run(
        stop_flow._select_stop_fallback_lookup(
            state=state,
            response=_stop_response("查到了"),
            content_len=3,
            runtime_chat_intent="lookup",
            banter_requires_lookup_retry=False,
            user_query_text="花来是什么意思",
            rewritten_query=None,
            context_hint="",
            user_images=[],
            plugin_query_intent="",
            tool_caller=SimpleNamespace(),
            registry=_Registry(),
            record_trace=lambda **_kwargs: None,
            logger=SimpleNamespace(info=lambda _msg: None),
            select_semantic_fallback_tool=_unexpected_selector,
        )
    )

    assert state.social_evidence_satisfied is True
    assert state.pending_evidence_followup_query == ""
    assert selected is None


def test_research_slang_coverage_does_not_stop_when_semantic_consensus_is_incomplete() -> None:
    state = stop_flow.StopFlowState(has_tool_call=True)
    packet = {
        "aggregation": {"source_group_count": 3, "covered_platforms": ["bilibili", "tieba"], "satisfies_request": True},
        "semantic_validation": {
            "target_term": "花来",
            "target_game": "三角洲行动",
            "status": "insufficient",
            "satisfies_request": False,
            "gap_codes": ["detail_evidence_missing"],
        },
        "items": [],
    }
    stop_flow.update_stop_flow_tool_result(
        state=state,
        registry=_Registry(),
        tool_name="research_game_slang",
        tool_args={"term": "花来"},
        result=json.dumps(packet, ensure_ascii=False),
    )
    parallel_tool = SimpleNamespace(enabled=lambda: True)

    selected = _select_fallback(state=state, registry=_Registry(parallel_tool), selection=None)

    assert state.social_evidence_satisfied is False
    assert state.semantic_web_fallback_attempted is True
    assert selected is not None
    assert selected[0] == "parallel_research"
    assert selected[1]["purpose"] == "lookup"
    assert selected[1]["max_workers"] == 3
    assert selected[1]["research_level"] == "low"
    assert selected[1]["time_budget_seconds"] == 30.0
    assert "三角洲行动 花来" in selected[1]["query"]
    assert selected[1]["focus"] == [
        "定义、称呼来源和梗的出处",
        "实际玩法、武器、角色、机制和使用语境",
        "独立梗百科、攻略或社区文章的反证与交叉验证",
    ]


def test_semantic_web_fallback_budget_reserves_execution_and_finalization_time() -> None:
    budget = stop_flow._semantic_web_fallback_budget(
        budget_deadline=130.0,
        semantic_research_target_deadline=145.0,
        now=100.0,
    )

    assert budget == (20.0, 122.0)
    assert stop_flow._semantic_web_fallback_budget(
        budget_deadline=110.0,
        semantic_research_target_deadline=145.0,
        now=100.0,
    ) is None


def test_research_slang_confirmed_semantics_blocks_web_fallback() -> None:
    state = stop_flow.StopFlowState(has_tool_call=True)
    packet = {
        "aggregation": {"satisfies_request": True},
        "semantic_validation": {
            "target_term": "花来",
            "target_game": "三角洲行动",
            "status": "confirmed",
            "satisfies_request": True,
            "gap_codes": [],
        },
        "items": [],
    }
    stop_flow.update_stop_flow_tool_result(
        state=state,
        registry=_Registry(),
        tool_name="research_game_slang",
        tool_args={"term": "花来"},
        result=json.dumps(packet, ensure_ascii=False),
    )

    selected = _select_fallback(
        state=state,
        registry=_Registry(SimpleNamespace(enabled=lambda: True)),
        selection=("web_search", {"query": "不应执行"}),
    )

    assert state.social_evidence_satisfied is True
    assert state.semantic_web_fallback_needed is False
    assert selected is None


def test_semantic_web_fallback_is_allowed_only_once() -> None:
    state = stop_flow.StopFlowState(
        semantic_web_fallback_needed=True,
        semantic_web_fallback_attempted=True,
        pending_evidence_followup_query="继续查证",
    )

    selected = _select_fallback(
        state=state,
        registry=_Registry(SimpleNamespace(enabled=lambda: True)),
        selection=("web_search", {"query": "重复"}),
    )

    assert selected is None
    assert state.pending_evidence_followup_query == ""


def test_direct_image_answer_does_not_force_an_unrelated_capability_tool() -> None:
    state = stop_flow.StopFlowState()

    async def _unexpected_selector(**_kwargs):  # noqa: ANN001
        raise AssertionError("a grounded image answer must not force a fallback tool")

    selected = asyncio.run(
        stop_flow._select_stop_fallback_lookup(
            state=state,
            response=_stop_response("主色调是蓝色，更像抽象壁纸"),
            content_len=15,
            runtime_chat_intent="plugin_question",
            banter_requires_lookup_retry=False,
            user_query_text="请看这张图并说说主色调",
            rewritten_query=None,
            context_hint="",
            user_images=["https://example.invalid/current-image.jpg"],
            plugin_query_intent="runtime_capability",
            tool_caller=SimpleNamespace(),
            registry=_Registry(),
            record_trace=lambda **_kwargs: None,
            logger=SimpleNamespace(info=lambda _msg: None),
            select_semantic_fallback_tool=_unexpected_selector,
        )
    )

    assert selected is None
    assert state.semantic_fallback_attempted is False


def test_handle_model_stop_returns_banter_text_without_bypass() -> None:
    decision, traces = _run_stop_handler(
        state=stop_flow.StopFlowState(),
        response=_stop_response("接一句"),
        content_len=3,
        runtime_chat_intent="banter",
    )

    assert decision.action == "return"
    assert decision.result.text == "接一句"
    assert decision.result.bypass_length_limits is False
    assert traces[-1]["detail"] == "reason=banter_stop content_len=3"


def test_handle_model_stop_marks_post_tool_text_as_bypass_length_limits() -> None:
    decision, traces = _run_stop_handler(
        state=stop_flow.StopFlowState(has_tool_call=True),
        response=_stop_response("查到了"),
        content_len=3,
        runtime_chat_intent="lookup",
    )

    assert decision.action == "return"
    assert decision.result.text == "查到了"
    assert decision.result.bypass_length_limits is True
    assert "has_tool_call=True" in traces[-1]["detail"]


def test_handle_model_stop_empty_stop_returns_no_reply() -> None:
    decision, traces = _run_stop_handler(
        state=stop_flow.StopFlowState(),
        response=_stop_response(""),
        content_len=0,
        runtime_chat_intent="lookup",
    )

    assert decision.action == "return"
    assert decision.result.text == "[NO_REPLY]"
    assert traces[-1]["detail"] == "reason=empty_stop text=[NO_REPLY]"


def test_failed_evidence_empty_stop_suppresses_required_reply_recovery() -> None:
    tool = _LookupTool("")
    state = stop_flow.StopFlowState(
        has_tool_call=True,
        last_tool_name="lookup_tool",
        last_tool_outcome="empty_evidence",
    )

    decision, traces = _run_stop_handler(
        state=state,
        response=_stop_response(""),
        content_len=0,
        registry=_Registry(tool),
    )

    assert decision.action == "return"
    assert decision.result.text == "[SILENCE]"
    assert decision.result.quality_context == "evidence_unavailable"
    assert decision.result.suppress_reply_recovery is True
    assert traces[-1]["detail"] == "reason=evidence_unavailable_empty text=[SILENCE]"


def test_update_stop_flow_records_canonical_no_results_as_empty_evidence() -> None:
    tool = _LookupTool("", properties={"query": {"type": "string"}})
    registry = _Registry(tool)
    state = stop_flow.StopFlowState()
    args = {"query": "旧问题"}

    stop_flow.update_stop_flow_tool_result(
        state=state,
        registry=registry,
        tool_name="lookup_tool",
        tool_args=args,
        result=json.dumps({"status": "no_results", "items": []}),
    )

    assert state.last_tool_outcome == "empty_evidence"
    assert stop_flow.tool_signature("lookup_tool", args) in state.unavailable_tool_signatures


def test_empty_evidence_allows_same_tool_with_different_args() -> None:
    tool = _LookupTool("", properties={"query": {"type": "string"}})
    registry = _Registry(tool)
    old_args = {"query": "旧问题"}
    state = stop_flow.StopFlowState(
        has_tool_call=True,
        last_tool_name="lookup_tool",
        last_tool_args=old_args,
        last_tool_outcome="empty_evidence",
        unavailable_tool_signatures={stop_flow.tool_signature("lookup_tool", old_args)},
    )

    selected = _select_fallback(
        state=state,
        registry=registry,
        selection=("lookup_tool", {"query": "新问题"}),
    )

    assert selected == ("lookup_tool", {"query": "新问题"})


def test_empty_evidence_skips_exact_signature_only_for_current_turn() -> None:
    tool = _LookupTool("", properties={"query": {"type": "string"}})
    registry = _Registry(tool)
    args = {"query": "同一个问题"}
    signature = stop_flow.tool_signature("lookup_tool", args)
    current_turn = stop_flow.StopFlowState(
        has_tool_call=True,
        last_tool_name="lookup_tool",
        last_tool_args=args,
        last_tool_outcome="empty_evidence",
        unavailable_tool_signatures={signature},
    )

    assert _select_fallback(
        state=current_turn,
        registry=registry,
        selection=("lookup_tool", args),
    ) is None

    next_turn = stop_flow.StopFlowState()
    assert _select_fallback(
        state=next_turn,
        registry=registry,
        selection=("lookup_tool", args),
    ) == ("lookup_tool", args)


def test_failed_evidence_draft_is_marked_for_persona_review() -> None:
    tool = _LookupTool("")
    state = stop_flow.StopFlowState(
        has_tool_call=True,
        last_tool_name="lookup_tool",
        last_tool_outcome="operational_failure",
    )

    decision, traces = _run_stop_handler(
        state=state,
        response=_stop_response("我没查到，不想乱编。"),
        content_len=10,
        registry=_Registry(tool),
    )

    assert decision.action == "return"
    assert decision.result.quality_context == "evidence_unavailable"
    assert traces[-1]["status"] == "warn"


def test_banter_draft_does_not_bypass_empty_evidence_quality_context() -> None:
    tool = _LookupTool("")
    state = stop_flow.StopFlowState(
        has_tool_call=True,
        last_tool_name="lookup_tool",
        last_tool_outcome="empty_evidence",
    )

    decision, traces = _run_stop_handler(
        state=state,
        response=_stop_response("地点没拿准，我别乱猜天气。"),
        content_len=14,
        runtime_chat_intent="banter",
        registry=_Registry(tool),
    )

    assert decision.action == "return"
    assert decision.result.quality_context == "evidence_unavailable"
    assert traces[-1]["status"] == "warn"
    assert "reason=evidence_unavailable" in traces[-1]["detail"]


def test_usable_evidence_is_not_overridden_by_later_tool_failure() -> None:
    tool = _LookupTool("", retryable=False)
    registry = _Registry(tool)
    state = stop_flow.StopFlowState(has_tool_call=True)
    stop_flow.update_stop_flow_tool_result(
        state=state,
        registry=registry,
        tool_name="lookup_tool",
        tool_args={"query": "有效查询"},
        result="可用证据",
    )
    stop_flow.update_stop_flow_tool_result(
        state=state,
        registry=registry,
        tool_name="lookup_tool",
        tool_args={"query": "失败查询"},
        result=json.dumps({"ok": False, "error": "fetch_failed"}),
    )

    decision, traces = _run_stop_handler(
        state=state,
        response=_stop_response("基于已有证据作答。"),
        content_len=9,
        registry=registry,
    )

    assert state.has_usable_evidence is True
    assert state.last_usable_tool_result_text == "可用证据"
    assert decision.action == "return"
    assert decision.result.quality_context == ""
    assert traces[-1]["status"] == "ok"


def test_fallback_tool_trace_uses_structured_outcome_without_result_content(monkeypatch) -> None:  # noqa: ANN001
    results = {
        "error": json.dumps({"ok": False, "error": "provider_failed", "secret": "do-not-log"}),
        "warn": json.dumps({"status": "no_results", "items": [], "secret": "do-not-log"}),
        "ok": json.dumps({"results": [{"title": "可用证据"}], "secret": "do-not-log"}),
    }

    async def _run(result: str) -> dict:
        async def _execute(**kwargs):  # noqa: ANN001
            return dict(kwargs["tool_args"]), result

        monkeypatch.setattr(stop_flow, "_execute_tool_with_retries", _execute)
        traces: list[dict] = []
        ran = await stop_flow._run_stop_fallback_tool(
            state=stop_flow.StopFlowState(),
            fallback_name="lookup_tool",
            fallback_args={"query": "问题"},
            step=1,
            registry=_Registry(_LookupTool(result, properties={"query": {"type": "string"}})),
            rewritten_query=None,
            user_images=[],
            logger=SimpleNamespace(info=lambda _msg: None),
            budget_deadline=None,
            messages=[],
            tool_caller=SimpleNamespace(),
            origin_response=_stop_response(""),
            record_trace=lambda **kwargs: traces.append(kwargs),
            append_evidence_guidance=_append_evidence_guidance,
        )
        assert ran is True
        return traces[-1]

    for expected_status, result in results.items():
        trace = asyncio.run(_run(result))
        assert trace["status"] == expected_status
        assert f"outcome={'operational_failure' if expected_status == 'error' else 'empty_evidence' if expected_status == 'warn' else 'usable_evidence'}" in trace["detail"]
        assert "do-not-log" not in trace["detail"]
