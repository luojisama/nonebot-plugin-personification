"""Tests for the cleanup path that prevents thinking-chain XML from leaking to users."""
from __future__ import annotations

from plugin.personification.core.context_policy import (
    has_silence_control_marker,
    strip_response_control_markers,
)
from plugin.personification.flows.yaml_parser import parse_yaml_response


def test_strip_full_status_think_action_block() -> None:
    raw = (
        "<status>心情: \"平静\" 状态: \"旁听中\" 记忆: \"\" 动作: \"\"</status>\n"
        "<think>Step 1: 安全性 OK\nStep 2: 跳过</think>\n"
        "<action></action>\n"
        "<output>\n<message>晚上好呀</message>\n</output>"
    )
    cleaned = strip_response_control_markers(raw)
    assert "心情" not in cleaned, cleaned
    assert "Step 1" not in cleaned, cleaned
    assert "晚上好呀" in cleaned


def test_strip_unclosed_think_block_does_not_leak_inner_text() -> None:
    # 截断/未闭合 <think>，内部"心情..."不应漏出
    raw = "<think>心情: \"困\" 状态: \"在偷玩手机\" 记忆: \"\" 动作: \"打哈欠\"\n"
    cleaned = strip_response_control_markers(raw)
    assert "心情" not in cleaned, cleaned
    assert "状态" not in cleaned, cleaned


def test_strip_keeps_plain_text() -> None:
    raw = "晚上好呀"
    assert strip_response_control_markers(raw) == "晚上好呀"


def test_strip_keeps_inner_message_when_only_message_tag_present() -> None:
    raw = "<output><message>就这样</message></output>"
    cleaned = strip_response_control_markers(raw)
    assert "就这样" in cleaned
    assert "<message>" not in cleaned
    assert "<output>" not in cleaned


def test_strip_drops_silence_marker() -> None:
    raw = "<output><message>[SILENCE]</message></output>"
    assert has_silence_control_marker(raw) is True
    cleaned = strip_response_control_markers(raw)
    assert "[SILENCE]" not in cleaned
    assert cleaned == ""


def test_parse_yaml_response_extracts_multiple_messages() -> None:
    raw = (
        "<status>...</status>"
        "<output>"
        "<message>第一条</message>"
        "<message>第二条</message>"
        "</output>"
    )
    parsed = parse_yaml_response(raw)
    texts = [m["text"] for m in parsed["messages"]]
    assert texts == ["第一条", "第二条"]


def test_parse_yaml_response_falls_back_when_no_output_wrapper() -> None:
    raw = "<status>x</status><message>裸 message</message>"
    parsed = parse_yaml_response(raw)
    texts = [m["text"] for m in parsed["messages"]]
    assert texts == ["裸 message"]


def test_parse_yaml_response_empty_messages_for_plain_text() -> None:
    raw = "晚上好呀"
    parsed = parse_yaml_response(raw)
    assert parsed["messages"] == []


def test_strip_extra_attributes_on_tags() -> None:
    raw = '<status type="auto" id="1">...</status><output><message quote="msg-1">嗨</message></output>'
    cleaned = strip_response_control_markers(raw)
    assert "嗨" in cleaned
    assert "type" not in cleaned
    assert "quote" not in cleaned
