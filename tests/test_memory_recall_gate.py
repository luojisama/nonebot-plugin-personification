from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


gate = load_personification_module("plugin.personification.core.memory_recall_gate")


def test_gate_filters_expired_unverified_social_and_limits_three() -> None:
    class _Caller:
        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003
            return SimpleNamespace(content='{"keep_memory_ids":["m0","m1","m2","m3"],"drop_memory_ids":[],"reason":"相关"}')

    result = asyncio.run(
        gate.gate_memory_candidates(
            candidates=[
                {"memory_id": "expired", "summary": "当前问题", "score": 0.99, "expires_at": 1},
                {
                    "memory_id": "social_candidate",
                    "summary": "当前问题的社交摘要",
                    "score": 0.99,
                    "source_kind": "social_mcp_summary",
                    "summary_status": "candidate",
                    "auto_context_eligible": False,
                },
                *[
                    {"memory_id": f"m{i}", "summary": f"当前问题 资料 {i}", "score": 0.98, "confidence": 0.95}
                    for i in range(5)
                ],
            ],
            query="当前问题",
            tool_caller=_Caller(),
            maximum=3,
            minimum_score=0.2,
        )
    )
    assert [item["memory_id"] for item in result] == ["m0", "m1", "m2"]
    assert all(item["memory_trust"] == "untrusted_data_only" for item in result)

    assert asyncio.run(
        gate.gate_memory_candidates(
            candidates=[{"memory_id": "m1", "summary": "当前问题", "score": 0.99}],
            query="当前问题",
            tool_caller=None,
            minimum_score=0.2,
        )
    ) == []


def test_gate_uses_strict_json_second_stage_and_fail_closes() -> None:
    class _Caller:
        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003
            return SimpleNamespace(content='{"keep_memory_ids":["m2"],"drop_memory_ids":["m1"],"reason":"仅 m2 相关"}')

    result = asyncio.run(
        gate.gate_memory_candidates(
            candidates=[
                {"memory_id": "m1", "summary": "不相关资料", "score": 0.98, "confidence": 0.95},
                {"memory_id": "m2", "summary": "当前问题相关资料", "score": 0.98, "confidence": 0.95},
            ],
            query="当前问题",
            turn_plan=SimpleNamespace(session_goal="回答当前问题"),
            tool_caller=_Caller(),
            maximum=3,
            minimum_score=0.2,
        )
    )
    assert [item["memory_id"] for item in result] == ["m2"]

    class _Broken:
        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003
            raise TimeoutError

    assert asyncio.run(
        gate.gate_memory_candidates(
            candidates=[{"memory_id": "m1", "summary": "当前问题", "score": 0.99, "confidence": 0.99}],
            query="当前问题",
            tool_caller=_Broken(),
            minimum_score=0.2,
        )
    ) == []
