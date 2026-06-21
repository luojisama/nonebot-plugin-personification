from __future__ import annotations

from ._loader import load_personification_module

planner = load_personification_module("plugin.personification.agent.runtime.planner")


def test_metadata_fallback_turn_plan_silences_uncertain_random_group() -> None:
    plan = planner.metadata_fallback_turn_plan(
        is_group=True,
        is_random_chat=True,
        is_direct_mention=False,
        has_images=False,
        message_target="uncertain",
    )

    assert plan.reply_action == "silence"
    assert plan.ambiguity_level == "high"
    assert plan.output_mode == "chat_short"
    assert plan.tool_intent == ["none"]


def test_parse_turn_plan_payload_clamps_and_normalizes() -> None:
    plan = planner.parse_turn_plan_payload(
        {
            "reply_action": "reply",
            "memory_need": "deep",
            "research_need": "high",
            "vision_need": "native",
            "qzone_continue": "true",
            "output_mode": "source_summary",
            "tool_intent": ["none", "lookup_web", "memory"],
            "ambiguity_level": "medium",
            "message_target": "bot",
            "session_goal": "查证后回答",
            "confidence": 1.8,
            "reason": "test",
        }
    )

    assert plan is not None
    assert plan.memory_need == "deep"
    assert plan.research_need == "high"
    assert plan.qzone_continue is True
    assert plan.tool_intent == ["lookup_web", "memory"]
    assert plan.confidence == 1.0
    assert plan.length_bounds == (80, 240)


def test_turn_plan_to_semantic_frame_maps_lookup_plugin() -> None:
    plan = planner.TurnPlan(
        reply_action="reply",
        research_need="low",
        output_mode="structured_help",
        tool_intent=["lookup_plugin", "lookup_web"],
        ambiguity_level="low",
        confidence=0.7,
        reason="plugin latest",
    )

    frame = planner.turn_plan_to_semantic_frame(plan)

    assert frame.chat_intent == "plugin_question"
    assert frame.plugin_question_intent == "latest"
    assert frame.recommend_silence is False
    assert frame.output_mode == "structured_help"
