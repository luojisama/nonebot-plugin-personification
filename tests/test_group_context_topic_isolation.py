"""群聊上下文话题隔离：多话题并行时，active_topics 必须带 speaker 标签防混淆。"""
from __future__ import annotations

from ._loader import load_personification_module


gc = load_personification_module("plugin.personification.core.group_context")


def test_active_topics_include_speaker_prefix() -> None:
    """每条 topic 必须带 'nickname: ' 前缀，让 LLM 看清是谁说的。"""
    messages = [
        {"user_id": "u1", "nickname": "鑫仔", "content": "这次地震有点严重吗"},
        {"user_id": "u2", "nickname": "流影", "content": "我家在浙江哦"},
    ]
    ctx = gc.build_group_conversation_context(recent_messages=messages)
    topics = ctx.active_topics
    assert any("鑫仔:" in t for t in topics)
    assert any("流影:" in t for t in topics)
    # 反向断言：没有裸内容（不带 speaker）
    assert not any(t.startswith("我家在浙江") for t in topics)


def test_render_topic_block_warns_against_cross_topic_link() -> None:
    """渲染出的 topic block 必须含警告语，提示 LLM 不要把不同人的话题串起来。"""
    messages = [
        {"user_id": "u1", "nickname": "鑫仔", "content": "这次地震严重吗"},
        {"user_id": "u2", "nickname": "流影", "content": "我家在浙江哦"},
    ]
    ctx = gc.build_group_conversation_context(recent_messages=messages)
    rendered = gc.render_group_conversation_context(ctx)
    # 警告语必须出现，明确告诉 LLM "不要把不同人说的内容关联成同一件事"
    assert "不要把不同人说的内容关联成同一件事" in rendered
    # 每条话题单独一行（- 前缀）
    assert "- 鑫仔:" in rendered
    assert "- 流影:" in rendered


def test_active_topics_dedup_by_speaker_and_content() -> None:
    """同 speaker 同内容只保留一份。"""
    messages = [
        {"user_id": "u1", "nickname": "鑫仔", "content": "地震严重吗"},
        {"user_id": "u1", "nickname": "鑫仔", "content": "地震严重吗"},  # 重复
        {"user_id": "u2", "nickname": "流影", "content": "地震严重吗"},  # 不同 speaker，保留
    ]
    ctx = gc.build_group_conversation_context(recent_messages=messages)
    topics = ctx.active_topics
    # 鑫仔的去重
    assert sum(1 for t in topics if t == "鑫仔: 地震严重吗") == 1
    # 流影的不被合并
    assert "流影: 地震严重吗" in topics


def test_anonymous_speaker_falls_back_to_uid() -> None:
    """没 nickname 时用 user_id 当 speaker label。"""
    messages = [
        {"user_id": "12345", "content": "嗯"},
        {"content": "无 id 无 nick"},
    ]
    ctx = gc.build_group_conversation_context(recent_messages=messages)
    assert any("12345:" in t for t in ctx.active_topics)
    assert any("未知:" in t for t in ctx.active_topics)


def test_no_topic_when_messages_empty() -> None:
    ctx = gc.build_group_conversation_context(recent_messages=[])
    assert ctx.active_topics == []
    rendered = gc.render_group_conversation_context(ctx)
    assert "不要把不同人说的内容关联" not in rendered


def test_topic_limit_eight() -> None:
    """active_topics 最多展示 8 条（render 层限制）。"""
    messages = [
        {"user_id": f"u{i}", "nickname": f"用户{i}", "content": f"消息{i}"}
        for i in range(15)
    ]
    ctx = gc.build_group_conversation_context(recent_messages=messages)
    rendered = gc.render_group_conversation_context(ctx)
    visible = [line for line in rendered.split("\n") if line.startswith("- 用户")]
    assert len(visible) <= 8
