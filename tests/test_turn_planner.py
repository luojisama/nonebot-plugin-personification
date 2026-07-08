from __future__ import annotations

import asyncio

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
            "speech_act": "source_summary",
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
    assert plan.speech_act == "source_summary"
    assert plan.qzone_continue is True
    assert plan.tool_intent == ["lookup_web", "memory"]
    assert plan.confidence == 1.0
    assert plan.length_bounds == (80, 240)


def test_turn_plan_to_semantic_frame_maps_lookup_plugin() -> None:
    plan = planner.TurnPlan(
        reply_action="reply",
        research_need="low",
        output_mode="structured_help",
        speech_act="answer",
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
    assert frame.speech_act == "answer"


def test_turn_planner_prompt_includes_media_context_discipline() -> None:
    captured: dict[str, object] = {}

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            captured["messages"] = messages
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"reply_action":"silence","memory_need":"none","research_need":"none",'
                        '"vision_need":"summary","qzone_continue":false,"output_mode":"chat_short",'
                        '"speech_act":"silence",'
                        '"tool_intent":["vision"],"ambiguity_level":"high",'
                        '"message_target":"uncertain","session_goal":"等待更多上下文",'
                        '"confidence":0.84,"reason":"媒体占位"}'
                    )
                },
            )()

    plan = asyncio.run(
        planner.plan_turn_with_llm(
            "[图片]",
            is_group=True,
            is_random_chat=True,
            has_images=True,
            message_target="uncertain",
            recent_context="群友刚才讨论一道雷落在附近，后面只发了表情。",
            tool_caller=_Caller(),
        )
    )

    system_prompt = captured["messages"][0]["content"]  # type: ignore[index]
    assert "媒体占位纪律" in system_prompt
    assert "低信息跟帖或媒体占位" in system_prompt
    assert "speech_act" in system_prompt
    assert "优先保持沉默" in system_prompt
    assert "优先回答文字 cue 或最近同一话题" in system_prompt
    assert "群聊不追问纪律" in system_prompt
    assert plan.reply_action == "silence"
    assert plan.speech_act == "silence"


def test_group_turn_plan_converts_clarify_to_statement_policy() -> None:
    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"reply_action":"ask_clarify","memory_need":"light","research_need":"low",'
                        '"vision_need":"none","qzone_continue":false,"output_mode":"chat_answer",'
                        '"speech_act":"clarify","tool_intent":["lookup_web"],"ambiguity_level":"medium",'
                        '"message_target":"bot","session_goal":"先确认对象","confidence":0.8,"reason":"缺对象"}'
                    )
                },
            )()

    plan = asyncio.run(
        planner.plan_turn_with_llm(
            "这个现在是什么情况",
            is_group=True,
            is_direct_mention=True,
            message_target="bot",
            tool_caller=_Caller(),
        )
    )

    assert plan.reply_action == "reply"
    assert plan.speech_act == "answer"
    assert "不追问" in plan.session_goal


def test_turn_plan_defaults_speech_act_from_output_and_tools() -> None:
    lookup_plan = planner.parse_turn_plan_payload(
        {
            "reply_action": "reply",
            "output_mode": "source_summary",
            "tool_intent": ["lookup_web"],
        }
    )
    expression_plan = planner.parse_turn_plan_payload(
        {
            "reply_action": "reply",
            "output_mode": "chat_short",
            "tool_intent": ["expression"],
        }
    )

    assert lookup_plan is not None
    assert lookup_plan.speech_act == "source_summary"
    assert expression_plan is not None
    assert expression_plan.speech_act == "execute_action"
