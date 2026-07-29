from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


avatar_evidence = load_personification_module(
    "plugin.personification.core.avatar_relation_evidence"
)
db = load_personification_module("plugin.personification.core.db")
group_context = load_personification_module(
    "plugin.personification.core.group_context"
)
qq_user_policy = load_personification_module(
    "plugin.personification.core.qq_user_policy"
)


class _PolicyService:
    classifier = None

    def authorize(self, user_id: str):  # noqa: ANN201
        allowed = user_id not in {"10002", "10003"}
        return SimpleNamespace(
            blocked=not allowed,
            allow_context_read=allowed,
        )


def test_policy_gate_filters_blocked_context_and_reference_metadata() -> None:
    gate = qq_user_policy.QQUserPolicyGate(_PolicyService())
    messages = [
        {
            "message_id": "m1",
            "user_id": "10002",
            "nickname": "被隔离者",
            "content": "不能进入上下文",
        },
        {
            "message_id": "m2",
            "user_id": "10001",
            "nickname": "允许者",
            "content": "保留这句",
            "reply_to_msg_id": "m1",
            "reply_to_user_id": "10002",
            "mentioned_ids": ["10002", "10004"],
        },
        {
            "message_id": "m3",
            "user_id": "10003",
            "nickname": "拒绝上下文者",
            "content": "也不能进入上下文",
        },
        {
            "message_id": "m4",
            "user_id": "90000",
            "nickname": "bot",
            "content": "机器人消息保留",
            "source_kind": "bot_reply",
        },
        {
            "message_id": "m5",
            "content": "系统消息保留",
            "source_kind": "system",
        },
    ]

    filtered, excluded = asyncio.run(
        gate.filter_context_messages(
            messages,
            bot_self_id="90000",
        )
    )

    assert [item["message_id"] for item in filtered] == ["m2", "m4", "m5"]
    assert excluded == {"10002", "10003"}
    assert filtered[0]["reply_to_msg_id"] == ""
    assert filtered[0]["reply_to_user_id"] == ""
    assert filtered[0]["mentioned_ids"] == ["10004"]


def test_group_context_defensively_excludes_blocked_messages_and_links(monkeypatch) -> None:
    monkeypatch.setattr(
        group_context,
        "summarize_group_relationships",
        lambda messages, **kwargs: "",
    )
    context = group_context.build_group_conversation_context(
        recent_messages=[
            {
                "message_id": "m1",
                "group_id": "20001",
                "user_id": "10002",
                "nickname": "被隔离者",
                "content": "敏感旧内容",
            },
            {
                "message_id": "m2",
                "group_id": "20001",
                "user_id": "10001",
                "nickname": "允许者",
                "content": "普通内容",
                "reply_to_msg_id": "m1",
                "reply_to_user_id": "10002",
                "mentioned_ids": ["10002"],
            },
        ],
        trigger_msg_id="m2",
        trigger_user_id="10001",
        excluded_user_ids={"10002"},
    )

    rendered = group_context.render_group_conversation_context(context)
    assert "敏感旧内容" not in rendered
    assert "被隔离者" not in rendered
    assert "10002" not in rendered
    assert [item["message_id"] for item in context.recent_messages] == ["m2"]


def test_avatar_relation_hypothesis_is_visual_only_and_policy_filtered(tmp_path) -> None:
    db_path = db.init_db_sync(tmp_path)
    assert avatar_evidence.record_avatar_relation_evidence(
        group_id="20001",
        left_user_id="10001",
        right_user_id="10002",
        relation="coordinated_pair",
        confidence=0.91,
        evidence_tags=["matching_palette"],
        asset_kinds=["illustration", "illustration"],
        avatar_hashes={"10001": "a" * 64, "10002": "b" * 64},
        observed_at=100,
        ttl_seconds=1000,
        db_path=db_path,
    )
    assert avatar_evidence.record_avatar_relation_evidence(
        group_id="20001",
        left_user_id="10003",
        right_user_id="10004",
        relation="unrelated",
        confidence=0.99,
        evidence_tags=["no_clear_link"],
        asset_kinds=["illustration", "illustration"],
        avatar_hashes={"10003": "c" * 64, "10004": "d" * 64},
        observed_at=101,
        ttl_seconds=1000,
        db_path=db_path,
    )

    rendered = avatar_evidence.render_avatar_relation_hypotheses(
        "20001",
        recent_messages=[
            {"user_id": "10001", "nickname": "甲"},
            {"user_id": "10002", "nickname": "乙"},
            {"user_id": "10003", "nickname": "丙"},
            {"user_id": "10004", "nickname": "丁"},
        ],
        now=101,
        db_path=db_path,
    )

    assert "甲 / 乙" in rendered
    assert "仅头像图像先验" in rendered
    assert "不表示现实关系" in rendered
    assert "丙" not in rendered
    assert "情侣" not in rendered

    assert avatar_evidence.render_avatar_relation_hypotheses(
        "20001",
        recent_messages=[
            {"user_id": "10001", "nickname": "甲"},
            {"user_id": "10002", "nickname": "乙"},
        ],
        excluded_user_ids={"10002"},
        now=101,
        db_path=db_path,
    ) == ""
