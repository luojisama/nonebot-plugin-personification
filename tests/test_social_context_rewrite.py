from __future__ import annotations

from ._loader import load_personification_module


query_rewriter = load_personification_module("plugin.personification.agent.query_rewriter")
tool_args = load_personification_module("plugin.personification.agent.runtime.tool_args")


def test_social_search_prefers_contextual_primary_query_and_bounded_context() -> None:
    rewritten = query_rewriter.ContextualQueryRewrite(
        primary_query="明日方舟 3-7 低练度 通关建议",
        query_candidates=["明日方舟 3-7"],
        context_clues=["当前讨论是 3-7 关卡", "用户询问前期阵容"],
        need_image_understanding=False,
        recommended_tools=["social_content_search"],
        search_plan=["查攻略和练度要求", "必要时读取视频"],
    )
    result = tool_args.rewrite_tool_args(
        registry=None,
        tool_name="social_content_search",
        tool_args={"query": "花来"},
        rewritten_query=rewritten,
    )

    assert result["query"] == "明日方舟 3-7 低练度 通关建议"
    assert "当前讨论是 3-7 关卡" in result["context"]
    assert "查攻略和练度要求" in result["context"]
    assert len(result["context"]) <= 1000
