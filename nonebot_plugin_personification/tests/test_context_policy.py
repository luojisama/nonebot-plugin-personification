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
