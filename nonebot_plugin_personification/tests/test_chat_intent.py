from __future__ import annotations

from ._loader import load_personification_module

chat_intent = load_personification_module("nonebot_plugin_personification.core.chat_intent")


def test_normalize_intent_text_strips_whitespace_and_newlines() -> None:
    assert chat_intent.normalize_intent_text("  你好\n\n 世界 \r\n ") == "你好 世界"


def test_heuristic_turn_semantic_frame_respects_group_metadata() -> None:
    group_frame = chat_intent.heuristic_turn_semantic_frame("随便说说", is_group=True, is_random_chat=True)
    private_frame = chat_intent.heuristic_turn_semantic_frame("随便说说", is_group=False, is_random_chat=False)

    assert group_frame.chat_intent == "banter"
    assert group_frame.ambiguity_level == "high"
    assert group_frame.recommend_silence is True
    assert private_frame.chat_intent == "banter"
    assert private_frame.ambiguity_level == "low"
    assert private_frame.recommend_silence is False


def test_parse_turn_semantic_frame_payload_handles_valid_and_invalid_dicts() -> None:
    valid = chat_intent._parse_turn_semantic_frame_payload(
        {
            "chat_intent": "lookup",
            "plugin_question_intent": "latest",
            "ambiguity_level": "medium",
            "recommend_silence": True,
            "requires_emotional_care": True,
            "sticker_appropriate": False,
            "meta_question": True,
            "domain_focus": "plugin",
            "user_attitude": "认真追问",
            "bot_emotion": "平静",
            "emotion_intensity": "high",
            "expression_style": "直接一点",
            "tts_style_hint": "自然",
            "sticker_mood_hint": "淡定|表达疑惑",
            "confidence": 0.8,
            "reason": "test",
        }
    )
    invalid = chat_intent._parse_turn_semantic_frame_payload({"chat_intent": "unknown"})

    assert valid is not None
    assert valid.chat_intent == "lookup"
    assert valid.plugin_question_intent == "latest"
    assert valid.ambiguity_level == "medium"
    assert valid.recommend_silence is True
    assert valid.requires_emotional_care is True
    assert valid.sticker_appropriate is False
    assert invalid is None

