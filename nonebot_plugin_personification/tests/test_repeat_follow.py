from __future__ import annotations

from ._loader import load_personification_module

repeat_follow = load_personification_module("nonebot_plugin_personification.core.repeat_follow")


def test_is_repeat_text_allowed_rejects_long_command_url_and_numbers() -> None:
    assert repeat_follow._is_repeat_text_allowed("复读机上线") is True
    assert repeat_follow._is_repeat_text_allowed("x" * 30) is False
    assert repeat_follow._is_repeat_text_allowed("/help") is False
    assert repeat_follow._is_repeat_text_allowed("https://example.com") is False
    assert repeat_follow._is_repeat_text_allowed("123456") is False


def test_looks_like_existing_repeat_matches_similar_text() -> None:
    assert repeat_follow._looks_like_existing_repeat("这也太典了吧", "这也太典了")
    assert not repeat_follow._looks_like_existing_repeat("完全不同", "这也太典了")


def test_can_follow_repeat_respects_ttl(monkeypatch) -> None:
    repeat_follow._RECENT_REPEAT_FOLLOW.clear()
    now = 1_000.0
    monkeypatch.setattr(repeat_follow.time, "time", lambda: now)

    assert repeat_follow._can_follow_repeat("123", "复读内容") is True
    repeat_follow._mark_repeat_follow("123", "复读内容")
    assert repeat_follow._can_follow_repeat("123", "复读内容") is False

    now += repeat_follow._REPEAT_FOLLOW_TTL_SECONDS + 1
    assert repeat_follow._can_follow_repeat("123", "复读内容") is True

