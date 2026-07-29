from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

query_rewriter = load_personification_module("plugin.personification.agent.query_rewriter")


def test_fallback_rewrite_resolves_deictic_acg_context() -> None:
    rewrite = query_rewriter._fallback_rewrite(
        history_new=(
            "白咲真寻机: @liang 大鸟居明日香 [图片]\n"
            "仅本次抽卡人可回复本消息并发送“签订契约”"
        ),
        history_last="@白咲真寻机 限时复活 这个动画牛逼",
        trigger_reason="direct mention",
    )

    assert "大鸟居明日香" in rewrite.primary_query
    assert any("角色 作品" in item for item in rewrite.query_candidates)
    assert any("动画 剧情" in item for item in rewrite.query_candidates)
    assert "resolve_acg_entity" in rewrite.recommended_tools


def test_empty_json_rewrite_is_structural_fallback() -> None:
    class _Caller:
        async def chat_with_tools(self, *_args, **_kwargs):  # noqa: ANN001
            return SimpleNamespace(content="{}")

    rewrite = asyncio.run(
        query_rewriter.contextual_query_rewriter(
            tool_caller=_Caller(),
            history_new="刚才在说测试图",
            history_last="解释一下",
            trigger_reason="direct",
        )
    )

    assert rewrite.source == "structural"
    assert rewrite.fallback_reason == "empty_payload"
