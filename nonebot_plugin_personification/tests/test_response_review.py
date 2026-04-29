from __future__ import annotations

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


def test_is_agent_reply_ooc_detects_search_style_phrasing_and_urls() -> None:
    assert response_review.is_agent_reply_ooc("根据搜索结果，先给你两条相关链接：https://example.com/very/long/path")
    assert response_review.is_agent_reply_ooc("我查了一下，这个设定后来改过")
    assert not response_review.is_agent_reply_ooc("这事儿大概就是后来改设定了")
