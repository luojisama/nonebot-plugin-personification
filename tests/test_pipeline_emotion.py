from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

pipeline_emotion = load_personification_module("plugin.personification.handlers.reply_pipeline.pipeline_emotion")
target_inference = load_personification_module("plugin.personification.core.target_inference")


class _SleepingToolCaller:
    async def chat_with_tools(self, *args, **kwargs):  # noqa: ANN001, ARG002
        await asyncio.sleep(10)
        return SimpleNamespace(content="{}")


class _JsonToolCaller:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat_with_tools(self, *args, **kwargs):  # noqa: ANN001, ARG002
        return SimpleNamespace(content=self.content)


def test_should_speak_in_random_chat_solo_speaker_returns_true(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return False

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="others",
        solo_speaker_follow=True,
    )
    assert result is True


def test_should_speak_in_random_chat_newer_batch_returns_false(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return True

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="others",
        solo_speaker_follow=False,
    )
    assert result is False


def test_should_speak_in_random_chat_target_bot_returns_true(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return False

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="bot",
        solo_speaker_follow=False,
    )
    assert result is True


def test_should_speak_in_random_chat_default_returns_true(monkeypatch) -> None:  # noqa: ANN001
    def _fake_has_newer(state):  # noqa: ANN001
        return False

    monkeypatch.setattr(pipeline_emotion, "batch_has_newer_messages", _fake_has_newer)
    result = pipeline_emotion.should_speak_in_random_chat(
        state={},
        message_target="others",
        solo_speaker_follow=False,
    )
    assert result is True


def test_semantic_frame_timeout_uses_metadata_fallback(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(pipeline_emotion, "semantic_frame_timeout_seconds", lambda _config: 0.01)

    async def _run():
        return await pipeline_emotion.infer_turn_semantic_frame_with_timeout(
            "看到后随便回我一句",
            plugin_config=SimpleNamespace(),
            is_group=False,
            is_random_chat=False,
            tool_caller=_SleepingToolCaller(),
            metric_scene="test",
        )

    frame, elapsed_ms, fallback_reason, timeout_s, source = asyncio.run(_run())
    assert fallback_reason == "semantic_frame_timeout"
    assert timeout_s == 0.01
    assert source == "metadata"
    assert elapsed_ms >= 0
    assert frame.reason == "metadata_fallback"
    assert getattr(frame, "fallback_reason", "") == "semantic_frame_timeout"


def test_semantic_frame_timeout_tries_secondary_llm_before_metadata(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(pipeline_emotion, "semantic_frame_timeout_seconds", lambda _config: 1.0)
    secondary = _JsonToolCaller(
        '{"chat_intent":"lookup","plugin_question_intent":"capability","ambiguity_level":"medium",'
        '"recommend_silence":false,"requires_emotional_care":false,"sticker_appropriate":false,'
        '"meta_question":false,"domain_focus":"realtime","user_attitude":"认真询问",'
        '"bot_emotion":"专注","emotion_intensity":"medium","expression_style":"先查清楚再答",'
        '"tts_style_hint":"自然","sticker_mood_hint":"困惑|表达疑惑",'
        '"conversation_scenario":"normal","address_mode":"none","confidence":0.82,'
        '"reason":"需要查证"}'
    )

    async def _run():
        return await pipeline_emotion.infer_turn_semantic_frame_with_timeout(
            "这个现在是什么情况",
            plugin_config=SimpleNamespace(),
            is_group=False,
            is_random_chat=False,
            tool_caller=_SleepingToolCaller(),
            fallback_tool_caller=secondary,
            metric_scene="test",
        )

    frame, elapsed_ms, fallback_reason, timeout_s, source = asyncio.run(_run())
    assert fallback_reason == ""
    assert timeout_s == 1.0
    assert source == "secondary"
    assert elapsed_ms >= 0
    assert frame.chat_intent == "lookup"
    assert frame.reason == "需要查证"
    assert getattr(frame, "llm_source", "") == "secondary"


def test_turn_plan_timeout_uses_metadata_fallback(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(pipeline_emotion, "semantic_frame_timeout_seconds", lambda _config: 0.01)

    async def _run():
        return await pipeline_emotion.plan_turn_with_timeout(
            "看到后随便回我一句",
            plugin_config=SimpleNamespace(),
            is_group=True,
            is_random_chat=False,
            is_direct_mention=True,
            message_target="bot",
            tool_caller=_SleepingToolCaller(),
            metric_mode="test",
        )

    plan, elapsed_ms, fallback_reason, timeout_s, source = asyncio.run(_run())
    assert fallback_reason == "turn_plan_timeout"
    assert timeout_s == 0.01
    assert source == "metadata"
    assert elapsed_ms >= 0
    assert plan.reply_action == "reply"
    assert plan.output_mode == "chat_short"
    assert getattr(plan, "fallback_reason", "") == "turn_plan_timeout"


def test_turn_plan_metadata_fallback_accepts_structural_bot_target() -> None:
    async def _run():
        return await pipeline_emotion.plan_turn_with_timeout(
            "接着问一句",
            plugin_config=SimpleNamespace(),
            is_group=True,
            is_random_chat=True,
            is_direct_mention=False,
            message_target=target_inference.TARGET_BOT,
            tool_caller=None,
            metric_mode="test",
        )

    plan, _elapsed_ms, fallback_reason, _timeout_s, source = asyncio.run(_run())
    assert fallback_reason == "turn_plan_no_caller"
    assert source == "metadata"
    assert plan.reply_action == "reply"
    assert plan.message_target == "bot"


def test_turn_plan_timeout_tries_secondary_llm_before_metadata(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(pipeline_emotion, "semantic_frame_timeout_seconds", lambda _config: 1.0)
    secondary = _JsonToolCaller(
        '{"reply_action":"ask_clarify","memory_need":"light","research_need":"low",'
        '"vision_need":"none","qzone_continue":false,"output_mode":"chat_answer",'
        '"tool_intent":["lookup_web"],"ambiguity_level":"medium","message_target":"bot",'
        '"session_goal":"先确认需求","confidence":0.78,"reason":"问题需要澄清"}'
    )

    async def _run():
        return await pipeline_emotion.plan_turn_with_timeout(
            "这个现在是什么情况",
            plugin_config=SimpleNamespace(),
            is_group=True,
            is_random_chat=False,
            is_direct_mention=True,
            message_target="bot",
            tool_caller=_SleepingToolCaller(),
            fallback_tool_caller=secondary,
            metric_mode="test",
        )

    plan, elapsed_ms, fallback_reason, timeout_s, source = asyncio.run(_run())
    assert fallback_reason == ""
    assert timeout_s == 1.0
    assert source == "secondary"
    assert elapsed_ms >= 0
    assert plan.reply_action == "reply"
    assert plan.speech_act == "answer"
    assert plan.output_mode == "chat_answer"
    assert getattr(plan, "llm_source", "") == "secondary"


def test_prepare_reply_semantics_falls_back_when_state_load_blocks(monkeypatch) -> None:  # noqa: ANN001
    stages: list[dict[str, object]] = []

    async def _blocked_state_load(_data_dir):  # noqa: ANN001
        await asyncio.sleep(10)
        return {}

    async def _emotion_state_load(_data_dir):  # noqa: ANN001
        return {}

    monkeypatch.setattr(pipeline_emotion, "_REPLY_STATE_LOAD_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(pipeline_emotion, "load_inner_state", _blocked_state_load)
    monkeypatch.setattr(pipeline_emotion, "load_emotion_state", _emotion_state_load)
    monkeypatch.setattr(pipeline_emotion, "_record_reply_trace_stage", lambda **kwargs: stages.append(kwargs))
    monkeypatch.setattr(
        pipeline_emotion,
        "get_personification_data_dir",
        lambda _config: None,
    )

    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_turn_planner_enabled=False,
            personification_turn_planner_shadow_enabled=False,
        ),
        logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        lite_tool_caller=None,
        agent_tool_caller=None,
        tool_registry=None,
        memory_store=None,
    )

    prepared = asyncio.run(
        pipeline_emotion.prepare_reply_semantics(
            runtime=runtime,
            recent_window=[],
            group_id="private_1",
            user_id="1",
            is_private_session=True,
            is_random_chat=False,
            is_direct_mention=False,
            raw_message_text="你翻译一下",
            current_agent_message_content="你翻译一下",
            recent_context_hint="",
            relationship_hint="",
            repeat_clusters=[],
            message_target="bot",
            solo_speaker_follow=False,
            has_images=True,
        )
    )

    state_stage = next(stage for stage in stages if stage["key"] == "reply_state_load")
    assert state_stage["status"] == "warn"
    assert "timeout=true" in str(state_stage["detail"])
    assert prepared.inner_state == pipeline_emotion.DEFAULT_INNER_STATE
    assert prepared.message_intent == "banter"


def test_prepare_reply_semantics_exposes_visual_grounding_before_frame_decision(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class _Caller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            del tools, use_builtin_search
            captured["messages"] = messages
            return SimpleNamespace(
                content=(
                    '{"chat_intent":"banter","plugin_question_intent":"capability",'
                    '"ambiguity_level":"low","recommend_silence":false,"confidence":0.9}'
                )
            )

    async def _load_states(*_args, **_kwargs):  # noqa: ANN001
        return {"mood": "calm"}, {}

    monkeypatch.setattr(pipeline_emotion, "load_reply_states_with_timeout", _load_states)
    monkeypatch.setattr(pipeline_emotion, "get_personification_data_dir", lambda _config: None)
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_turn_planner_enabled=False,
            personification_turn_planner_shadow_enabled=False,
            personification_semantic_frame_timeout=1.0,
            personification_group_knowledge_enabled=False,
        ),
        logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        lite_tool_caller=_Caller(),
        agent_tool_caller=None,
        tool_registry=None,
        memory_store=None,
    )

    prepared = asyncio.run(
        pipeline_emotion.prepare_reply_semantics(
            runtime=runtime,
            recent_window=[],
            group_id="1",
            user_id="u1",
            is_private_session=False,
            is_random_chat=False,
            is_direct_mention=False,
            raw_message_text="[图片]",
            current_agent_message_content="[图片]",
            recent_context_hint="",
            relationship_hint="",
            repeat_clusters=[],
            message_target="broadcast",
            solo_speaker_follow=False,
            has_images=True,
            media_grounding="安全视觉摘要：动漫图里多人注视画面中央；画中人物不是群友。",
        )
    )

    user_prompt = captured["messages"][1]["content"]  # type: ignore[index]
    assert "动漫图里多人注视画面中央" in user_prompt
    assert "画中人物不是群友" in user_prompt
    assert prepared.semantic_frame.confidence == 0.9
