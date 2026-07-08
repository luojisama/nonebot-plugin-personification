from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

response_review = load_personification_module("plugin.personification.core.response_review")


def test_looks_like_recent_duplicate_detects_same_and_similar_text() -> None:
    assert response_review._looks_like_recent_duplicate("太真实了", ["太真实了", "别的"])
    assert response_review._looks_like_recent_duplicate("这也太典了吧", ["这也太典了"])
    assert not response_review._looks_like_recent_duplicate("完全不同", ["这也太典了"])


def test_arbitrate_reply_mode_handles_key_combinations() -> None:
    no_reply = response_review.arbitrate_reply_mode(
        intent_decision=SimpleNamespace(ambiguity_level="high", recommend_silence=True, confidence=0.9),
        is_private=False,
        is_direct_mention=False,
        is_random_chat=True,
        message_target="others",
        solo_speaker_follow=False,
    )
    clarify = response_review.arbitrate_reply_mode(
        intent_decision=SimpleNamespace(ambiguity_level="high", recommend_silence=True, confidence=0.5),
        is_private=True,
        is_direct_mention=False,
        is_random_chat=False,
        message_target="bot",
        solo_speaker_follow=False,
    )
    reply = response_review.arbitrate_reply_mode(
        intent_decision=SimpleNamespace(ambiguity_level="low", recommend_silence=False, confidence=0.2),
        is_private=False,
        is_direct_mention=False,
        is_random_chat=False,
        message_target="others",
        solo_speaker_follow=False,
    )

    assert no_reply == "no_reply"
    assert clarify == "clarify"
    assert reply == "reply"


def test_arbitrate_reply_mode_silences_uncertain_random_chat_even_with_low_confidence() -> None:
    decision = response_review.arbitrate_reply_mode(
        intent_decision=SimpleNamespace(ambiguity_level="high", recommend_silence=True, confidence=0.18),
        is_private=False,
        is_direct_mention=False,
        is_random_chat=True,
        message_target="uncertain",
        solo_speaker_follow=False,
    )

    assert decision == "no_reply"


def test_arbitrate_reply_mode_keeps_direct_random_chat_replyable() -> None:
    decision = response_review.arbitrate_reply_mode(
        intent_decision=SimpleNamespace(ambiguity_level="high", recommend_silence=True, confidence=0.18),
        is_private=False,
        is_direct_mention=True,
        is_random_chat=True,
        message_target="bot",
        solo_speaker_follow=False,
    )

    assert decision == "clarify"


def test_is_agent_reply_ooc_detects_search_style_phrasing_and_urls() -> None:
    assert response_review.is_agent_reply_ooc("根据搜索结果，先给你两条相关链接：https://example.com/very/long/path")
    assert response_review.is_agent_reply_ooc("我查了一下，这个设定后来改过")
    assert response_review.is_agent_reply_ooc("我需要确认一下广州的天气。")
    assert response_review.is_agent_reply_ooc("**广州** 接下来雨不少")
    assert response_review.is_agent_reply_ooc("Step 1: 检查\nStep 2: 输出\n轻松")
    assert response_review.is_agent_reply_ooc("等下，这什么表情")
    assert response_review.is_agent_reply_ooc("被雷炸了，这也太刺激了吧")
    assert response_review.is_agent_reply_ooc("我先看看情况，等会再说")
    assert response_review.is_agent_reply_ooc("先围观一下，回头再聊")
    assert not response_review.is_agent_reply_ooc("这事儿大概就是后来改设定了")


def test_output_mode_hint_uses_turn_plan_lengths() -> None:
    hint = response_review._output_mode_hint(SimpleNamespace(output_mode="structured_help"))

    assert "structured_help" in hint
    assert "80-300" in hint


def test_review_blocks_no_reply_for_direct_mention() -> None:
    async def _fake_call(messages):  # noqa: ANN001
        assert "禁止输出 no_reply" in messages[0]["content"]
        return '{"action":"no_reply","text":"","reason":"bad"}'

    decision = asyncio.run(
        response_review.review_response_text(
            _fake_call,
            candidate_text="我在",
            raw_message_text="@bot 在吗",
            is_direct_mention=True,
        )
    )

    assert decision.action == "accept"
    assert decision.text == "我在"


def test_review_prompt_rejects_empty_affirmation_and_status_announcement() -> None:
    async def _fake_call(messages):  # noqa: ANN001
        content = messages[0]["content"]
        assert "附和感叹/转述聊天" in content
        assert "等会再说" in content
        return '{"action":"rewrite","text":"那就先卡这个点聊，别绕远。","reason":"empty_affirmation"}'

    decision = asyncio.run(
        response_review.review_response_text(
            _fake_call,
            candidate_text="确实，这也太真实了吧",
            raw_message_text="这题写不动了",
        )
    )

    assert decision.action == "rewrite"
    assert decision.text == "那就先卡这个点聊，别绕远。"
