from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

stop_flow = load_personification_module("plugin.personification.agent.runtime.stop_flow")


class _Registry:
    def get(self, _name: str):  # noqa: ANN001
        return None


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


def _run_stop_handler(*, state, response, content_len: int, runtime_chat_intent: str = "lookup"):
    traces: list[dict] = []
    decision = asyncio.run(
        stop_flow.handle_model_stop(
            state=state,
            response=response,
            content_len=content_len,
            active_schemas=[],
            runtime_chat_intent=runtime_chat_intent,
            intent_decision=SimpleNamespace(ambiguity_level="low"),
            registry=_Registry(),
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
            select_semantic_fallback_tool=_no_lookup,
        )
    )
    return decision, traces


def test_should_review_banter_lookup_draft_uses_structural_signals() -> None:
    assert stop_flow._should_review_banter_lookup_draft(ambiguity_level="high", draft_answer_text="知道了")
    assert stop_flow._should_review_banter_lookup_draft(ambiguity_level="low", draft_answer_text="这是啥？")
    assert not stop_flow._should_review_banter_lookup_draft(ambiguity_level="low", draft_answer_text="接一句")


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
