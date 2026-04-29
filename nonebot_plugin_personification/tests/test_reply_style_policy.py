from __future__ import annotations

from ._loader import load_personification_module

reply_style_policy = load_personification_module("plugin.personification.core.reply_style_policy")


def test_reply_style_policy_keeps_visual_context_internal() -> None:
    prompt = reply_style_policy.build_reply_style_policy_prompt(
        has_visual_context=True,
        photo_like=False,
    )

    assert "内部上下文" in prompt
    assert "不要主动讲图里是什么" in prompt
    assert "不要堆砌互联网热词" in prompt


def test_direct_visual_guard_does_not_ask_to_describe_image() -> None:
    prompt = reply_style_policy.build_direct_visual_identity_guard()

    assert "不要主动讲解、复述或分析画面" in prompt
    assert "描述或评论图片内容" not in prompt
