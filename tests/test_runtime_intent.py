from __future__ import annotations

from ._loader import load_personification_module

intent = load_personification_module("plugin.personification.agent.runtime.intent")


def test_extract_focus_query_text_prefers_text_before_forward_block() -> None:
    text = "你觉得这个怎么样\n[聊天记录]: A:哈哈\nB:对啊"

    assert intent.extract_focus_query_text(text) == "你觉得这个怎么样"


def test_extract_focus_query_text_returns_empty_when_message_is_only_forward_block() -> None:
    text = "[聊天记录]: A:哈哈\nB:对啊"

    assert intent.extract_focus_query_text(text) == ""


def test_extract_quoted_message_text_supports_forward_block() -> None:
    text = "你觉得这个怎么样\n[聊天记录]: A:哈哈\nB:对啊"

    quoted = intent.extract_quoted_message_text(text)

    assert quoted.startswith("[转发聊天记录]:")
    assert "A:哈哈" in quoted
