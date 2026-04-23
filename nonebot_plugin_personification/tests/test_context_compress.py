from __future__ import annotations

import asyncio

from ._loader import load_personification_module

context_policy = load_personification_module("nonebot_plugin_personification.core.context_policy")


def test_compress_context_if_needed_falls_back_when_llm_summary_fails() -> None:
    async def _run() -> list[str]:
        async def _broken_call(_messages):  # noqa: ANN001
            raise RuntimeError("summary unavailable")

        return await context_policy.compress_context_if_needed(
            [
                "较早上下文" * 40,
                "更早上下文" * 30,
                "最近一句：今晚先发修复版",
                "最近二句：明天再补体验优化",
            ],
            max_tokens=120,
            keep_recent=2,
            call_ai_api=_broken_call,
        )

    result = asyncio.run(_run())

    assert result[0].startswith("## 较早上下文摘要")
    assert "今晚先发修复版" in result[-2]

