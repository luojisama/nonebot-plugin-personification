from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

budgeting = load_personification_module("plugin.personification.agent.runtime.budgeting")


def test_budget_profile_uses_fast_silence_for_silence_plan() -> None:
    profile = budgeting.derive_agent_budget_profile(
        turn_plan=SimpleNamespace(reply_action="silence", speech_act="silence", tool_intent=["none"]),
        intent_decision=SimpleNamespace(chat_intent="banter"),
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    assert profile.mode == "silence_fast"
    assert profile.suggested_max_steps == 0
    assert profile.suggested_time_budget_seconds == 3.0


def test_budget_profile_keeps_research_room_for_lookup() -> None:
    profile = budgeting.derive_agent_budget_profile(
        turn_plan=SimpleNamespace(
            reply_action="reply",
            speech_act="source_summary",
            research_need="high",
            tool_intent=["lookup_web"],
        ),
        intent_decision=SimpleNamespace(chat_intent="lookup"),
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    assert profile.mode == "research"
    assert profile.suggested_max_steps == 10
    assert profile.suggested_time_budget_seconds == 120.0


def test_budget_profile_limits_light_chat_without_lookup_intents() -> None:
    profile = budgeting.derive_agent_budget_profile(
        turn_plan=SimpleNamespace(
            reply_action="reply",
            speech_act="participate",
            research_need="none",
            tool_intent=["none"],
        ),
        intent_decision=SimpleNamespace(chat_intent="banter"),
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    assert profile.mode == "light_chat"
    assert profile.suggested_max_steps == 2
    assert profile.suggested_time_budget_seconds == 18.0


def test_budget_profile_action_mode_for_send_tools() -> None:
    profile = budgeting.derive_agent_budget_profile(
        turn_plan=SimpleNamespace(
            reply_action="reply",
            speech_act="execute_action",
            research_need="none",
            tool_intent=["expression"],
        ),
        intent_decision=SimpleNamespace(chat_intent="expression"),
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    assert profile.mode == "action"
    assert profile.suggested_max_steps == 3
    assert profile.suggested_time_budget_seconds == 25.0


def test_budget_trace_detail_exposes_safe_signals() -> None:
    profile = budgeting.derive_agent_budget_profile(
        turn_plan=SimpleNamespace(reply_action="reply", speech_act="answer", output_mode="chat_answer"),
        intent_decision=SimpleNamespace(chat_intent="explanation"),
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    detail = budgeting.render_agent_budget_trace_detail(
        profile,
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    assert "budget=answer" in detail
    assert "suggested_steps=4" in detail
    assert "actual_steps=10" in detail
    assert "source=shadow" in detail


def test_budget_profile_shadow_keeps_runtime_limits() -> None:
    profile = budgeting.derive_agent_budget_profile(
        turn_plan=SimpleNamespace(reply_action="reply", speech_act="participate", research_need="none", tool_intent=[]),
        intent_decision=SimpleNamespace(chat_intent="banter"),
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    steps, seconds, sourced, applied = budgeting.apply_agent_budget_profile(
        profile,
        mode="shadow",
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    assert applied is False
    assert steps == 10
    assert seconds == 150
    assert sourced.source == "shadow"


def test_budget_profile_adaptive_applies_suggested_limits() -> None:
    profile = budgeting.derive_agent_budget_profile(
        turn_plan=SimpleNamespace(reply_action="reply", speech_act="participate", research_need="none", tool_intent=[]),
        intent_decision=SimpleNamespace(chat_intent="banter"),
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    steps, seconds, sourced, applied = budgeting.apply_agent_budget_profile(
        profile,
        mode="adaptive",
        actual_max_steps=10,
        actual_time_budget_seconds=150,
    )

    assert applied is True
    assert steps == 2
    assert seconds == 18.0
    assert sourced.source == "adaptive"
