from __future__ import annotations

from ._loader import load_personification_module

processor = load_personification_module("plugin.personification.handlers.reply_pipeline.processor")


def test_image_only_random_chat_placeholder_prefers_silence_over_image_commentary() -> None:
    text = processor._build_image_only_context_message(
        sender_name="东方",
        is_private_context=False,
        is_active_followup=False,
        followup_topic="",
        is_solo_speaker_follow=False,
        solo_follow_topic="",
        is_random_chat=True,
    )

    assert "路过看到" in text
    assert "保持安静" in text
    assert "不要评论图片或表情内容" in text
    assert "自然评价" not in text


def test_image_only_followup_placeholder_uses_topic_without_claiming_vision() -> None:
    text = processor._build_image_only_context_message(
        sender_name="东方",
        is_private_context=False,
        is_active_followup=True,
        followup_topic="刚才说雷落在眼前",
        is_solo_speaker_follow=False,
        solo_follow_topic="",
        is_random_chat=False,
    )

    assert "刚才说雷落在眼前" in text
    assert "没有清楚的视觉摘要" in text
    assert "不要评价图片内容" in text
    assert "短句回应" in text
