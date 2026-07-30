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
