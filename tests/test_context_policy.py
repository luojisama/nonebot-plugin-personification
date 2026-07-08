from __future__ import annotations

import asyncio

from ._loader import load_personification_module

context_policy = load_personification_module("plugin.personification.core.context_policy")


def test_compress_context_if_needed_keeps_short_context_without_llm_call() -> None:
    chunks = ["第一句", "第二句"]

    async def _run() -> tuple[list[str], bool]:
        called = False

        async def _fake_call(_messages):
            nonlocal called
            called = True
            return "不会被调用"

        result = await context_policy.compress_context_if_needed(
            chunks,
            max_tokens=200,
            call_ai_api=_fake_call,
        )
        return result, called

    result, called = asyncio.run(_run())
    assert result == chunks
    assert called is False


def test_compress_context_if_needed_summarizes_long_context() -> None:
    async def _run() -> tuple[list[str], bool]:
        called = False

        async def _fake_call(_messages):
            nonlocal called
            called = True
            return "前文主要在聊上线排期和用户反馈，结论是先保稳定。"

        chunks = [
            "早前上下文" * 40,
            "中间讨论" * 30,
            "最近一句：今晚先发修复版",
            "最近二句：明天再补体验优化",
        ]
        result = await context_policy.compress_context_if_needed(
            chunks,
            max_tokens=80,
            keep_recent=2,
            call_ai_api=_fake_call,
        )
        return result, called

    result, called = asyncio.run(_run())
    assert called is True
    assert result[0].startswith("## 较早上下文摘要")
    assert context_policy._estimate_chunks_tokens(result) <= 80


def test_response_control_markers_are_stripped_from_history() -> None:
    assert context_policy.has_silence_control_marker("[SILENCE]") is True
    assert context_policy.strip_response_control_markers("<think>内部推理</think><message>能看到吗</message>") == "能看到吗"
    assert context_policy.sanitize_history_text("前缀 [NO_REPLY] <status>休息</status> 后缀") == "前缀 后缀"


def test_silence_marker_detects_bare_words_as_full_message() -> None:
    has = context_policy.has_silence_control_marker
    assert has("SILENCE") is True
    assert has("  silence  ") is True
    assert has("Silent") is True
    assert has("沉默") is True
    assert has("无回复") is True
    assert has("不回复") is True
    assert has("NO_REPLY") is True
    assert has("no-reply") is True
    assert has("保持沉默") is True


def test_silence_marker_ignores_words_in_natural_sentences() -> None:
    has = context_policy.has_silence_control_marker
    assert has("今天我选择沉默不语") is False
    assert has("silent night, holy night") is False
    assert has("我不回复你了，气死") is False
    assert has("") is False
    assert has("    ") is False


def test_silence_marker_detects_prefix_only_messages() -> None:
    has = context_policy.has_silence_control_marker
    assert has("SILENCE:") is True
    assert has("沉默：") is True
    assert has("SILENCE\n\n") is True
    assert has("silence:    ") is True
    # 前缀后有内容时，整体不算静默（应该让 strip 砍前缀后正常发送）
    assert has("SILENCE: 实际回复") is False
    assert has("沉默：今天有事先撤") is False
    assert has("SILENCE\n今天先这样") is False
    assert has("[SILENCE]: 实际回复") is False
    assert has("[NO_REPLY]\n今天先这样") is False


def test_strip_response_control_markers_removes_bare_silence() -> None:
    strip = context_policy.strip_response_control_markers
    assert strip("SILENCE") == ""
    assert strip("沉默") == ""
    assert strip("NO_REPLY") == ""
    # 前缀+冒号砍掉，保留正文
    assert strip("SILENCE: 实际回复") == "实际回复"
    assert strip("沉默：今天有事先撤") == "今天有事先撤"
    assert strip("[SILENCE]: 实际回复") == "实际回复"
    assert strip("[NO_REPLY]\n今天先这样") == "今天先这样"
    # 前缀+换行砍掉，保留正文
    assert strip("SILENCE\n今天先这样") == "今天先这样"
    assert strip("silence\n\n下午再聊") == "下午再聊"
    # 中间出现的不动
    assert strip("今天我选择沉默不语") == "今天我选择沉默不语"
