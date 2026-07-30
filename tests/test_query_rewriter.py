from __future__ import annotations

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
