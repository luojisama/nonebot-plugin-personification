from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from ._loader import load_personification_module


scoped_profile = load_personification_module("plugin.personification.core.scoped_profile")
data_transfer_service = load_personification_module(
    "plugin.personification.core.data_transfer.service"
)


def _claims_by_key(document: dict) -> dict[str, dict]:
    return {claim["key"]: claim for claim in document["claims"]}


def _create_group_messages_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                is_bot INTEGER NOT NULL DEFAULT 0,
                reply_to_msg_id TEXT DEFAULT NULL,
                reply_to_user_id TEXT DEFAULT NULL,
                mentioned_ids TEXT NOT NULL DEFAULT '[]',
                message_id TEXT DEFAULT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'user',
                timestamp REAL NOT NULL
            )
            """
        )


def _insert_message(
    path: Path,
    *,
    group_id: str = "g1",
    user_id: str,
    content: str,
    timestamp: float,
    message_id: str,
    is_bot: int = 0,
    reply_to_msg_id: str | None = None,
    reply_to_user_id: str | None = None,
    mentioned_ids: str = "[]",
    thread_id: str = "",
    source_kind: str = "user",
) -> int:
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO group_messages(
                group_id, user_id, content, is_bot, reply_to_msg_id,
                reply_to_user_id, mentioned_ids, message_id, thread_id,
                source_kind, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                user_id,
                content,
                is_bot,
                reply_to_msg_id,
                reply_to_user_id,
                mentioned_ids,
                message_id,
                thread_id,
                source_kind,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)


def test_global_legacy_upgrade_prefers_confirmed_and_bounds_claims() -> None:
    refs = [
        {
            "row_id": index + 1,
            "message_id": f"m{index}",
            "relation": "same_thread",
            "timestamp": float(index),
            "content": f"raw-{index}",
        }
        for index in range(40)
    ]
    document = scoped_profile.build_global_profile_document(
        {
            "structured": {
                "occupation": "学生",
                "communication_style": "简短",
                "relationship": "旧关系",
                "recent_focus": "x" * 250,
            },
            "user_corrections": {"职业": "设计师", "称呼与昵称": "阿明"},
        },
        claims=[
            {
                "key": "interests",
                "value": "绘画",
                "source": "global_generated",
                "confidence": 2,
                "evidence_refs": refs,
            },
            {"key": "relationship", "value": "新关系", "confidence": 0.9},
            {"key": "bad", "value": "discarded", "source": "not_allowed"},
        ],
        revision=7,
        generation={
            "last_processed_group_message_row_id": 99,
            "status": "completed",
            "generated_at": 123.5,
        },
    )

    claims = _claims_by_key(document)
    assert document["schema_version"] == 2
    assert document["revision"] == 7
    assert document["scope"] == {"kind": "global"}
    assert document["base"] == {"global_revision": 0, "digest": ""}
    assert document["generation"] == {
        "last_processed_group_message_row_id": 99,
        "status": "completed",
        "generated_at": 123.5,
    }
    assert claims["occupation"]["value"] == "设计师"
    assert claims["occupation"]["source"] == "user_confirmed"
    assert claims["occupation"]["confidence"] == 1.0
    assert claims["nickname_pref"]["source"] == "user_confirmed"
    assert claims["relationship"]["value"] == "新关系"
    assert claims["relationship"]["source"] == "global_generated"
    assert len(claims["recent_focus"]["value"]) == 200
    assert claims["interests"]["confidence"] == 1.0
    assert len(claims["interests"]["evidence_refs"]) == 32
    assert "bad" not in claims
    assert all(claim["class"] == "stable" for claim in claims.values())
    assert all("content" not in ref for ref in claims["interests"]["evidence_refs"])


def test_group_claim_allowlist_scope_and_global_base() -> None:
    global_document = scoped_profile.build_global_profile_document(
        claims={"occupation": {"value": "开发者", "source": "global_generated"}},
        revision=3,
    )
    group_document = scoped_profile.build_group_profile_document(
        "  group-1  ",
        claims=[
            {"key": "nickname_pref", "value": "小明", "source": "group_generated"},
            {"key": "group_role", "value": "气氛组", "source": "evidence_derived"},
            {"key": "occupation", "value": "老师", "source": "group_generated"},
            {"key": "age_group", "value": "20代", "source": "group_generated"},
            {"key": "gender", "value": "女", "source": "group_generated"},
        ],
        global_document=global_document,
        revision=4,
    )

    assert group_document["scope"] == {"kind": "group", "group_id": "group-1"}
    assert group_document["base"] == {
        "global_revision": 3,
        "digest": scoped_profile.profile_document_digest(global_document),
    }
    assert set(_claims_by_key(group_document)) == {"nickname_pref", "group_role"}
    assert all(claim["class"] == "contextual" for claim in group_document["claims"])
    with pytest.raises(ValueError):
        scoped_profile.build_group_profile_document("bad group id", claims=[])


def test_effective_merge_keeps_confirmed_global_and_overlays_contextual_keys() -> None:
    global_document = scoped_profile.build_global_profile_document(
        {
            "structured": {
                "relationship": "普通群友",
                "occupation": "设计师",
            },
            "user_corrections": {"沟通风格": "简短直说"},
        }
    )
    group_document = scoped_profile.build_group_profile_document(
        "g1",
        claims=[
            {"key": "communication_style", "value": "长篇解释", "source": "group_generated"},
            {"key": "relationship", "value": "熟悉群友", "source": "evidence_derived"},
            {"key": "recent_focus", "value": "准备比赛", "source": "group_generated"},
            {"key": "occupation", "value": "学生", "source": "group_generated"},
        ],
        global_document=global_document,
    )

    effective = {claim["key"]: claim for claim in scoped_profile.effective(global_document, group_document)}
    assert effective["communication_style"]["value"] == "简短直说"
    assert effective["communication_style"]["source"] == "user_confirmed"
    assert effective["relationship"]["value"] == "熟悉群友"
    assert effective["relationship"]["class"] == "contextual"
    assert effective["recent_focus"]["value"] == "准备比赛"
    assert effective["occupation"]["value"] == "设计师"


def test_normalize_and_render_are_deterministic() -> None:
    claims = [
        {"key": "social_mode", "value": "被动", "source": "global_generated", "confidence": 0.8},
        {"key": "nickname_pref", "value": "小明", "source": "user_confirmed", "confidence": 0.1},
        {"key": "social_mode", "value": "主动", "source": "global_generated", "confidence": 0.2},
    ]
    first = scoped_profile.build_global_profile_document(claims=claims, revision=2)
    second = scoped_profile.build_global_profile_document(claims=list(reversed(claims)), revision=2)

    assert first == second
    assert scoped_profile.normalize(first) == first
    assert scoped_profile.render(first) == scoped_profile.render(second)
    assert scoped_profile.render(first).splitlines()[1].startswith("- nickname_pref")
    assert scoped_profile.render(first).splitlines()[2].startswith("- social_mode")


def test_evidence_selector_filters_scope_sources_and_uses_relation_priority(tmp_path: Path) -> None:
    db_path = tmp_path / "messages.db"
    _create_group_messages_db(db_path)
    same_thread = _insert_message(
        db_path,
        user_id="u-thread",
        content="thread context",
        timestamp=10,
        message_id="m-thread",
        thread_id="thread-1",
    )
    mention = _insert_message(
        db_path,
        user_id="u-mention",
        content="mention context",
        timestamp=20,
        message_id="m-mention",
        mentioned_ids='["u-self"]',
    )
    reply = _insert_message(
        db_path,
        user_id="u-reply",
        content="reply context",
        timestamp=30,
        message_id="m-reply",
        reply_to_user_id="u-self",
        mentioned_ids='["u-self"]',
        thread_id="thread-1",
    )
    replied_to_by_anchor = _insert_message(
        db_path,
        user_id="u-anchor-reply-target",
        content="anchor replies to this message",
        timestamp=35,
        message_id="m-anchor-reply-target",
    )
    _insert_message(
        db_path,
        group_id="other-group",
        user_id="u-cross",
        content="cross group",
        timestamp=40,
        message_id="m-cross",
        reply_to_user_id="u-self",
        thread_id="thread-1",
    )
    _insert_message(
        db_path,
        user_id="bot-1",
        content="bot context",
        timestamp=41,
        message_id="m-bot",
        is_bot=1,
        thread_id="thread-1",
        source_kind="bot",
    )
    _insert_message(
        db_path,
        user_id="plugin-1",
        content="plugin context",
        timestamp=42,
        message_id="m-plugin",
        thread_id="thread-1",
        source_kind="plugin",
    )
    _insert_message(
        db_path,
        user_id="u-self",
        content="same user context",
        timestamp=43,
        message_id="m-self-old",
        thread_id="thread-1",
    )
    _insert_message(
        db_path,
        user_id="u-invalid-json",
        content="invalid mentions",
        timestamp=44,
        message_id="m-invalid",
        mentioned_ids="not-json",
    )
    anchor = _insert_message(
        db_path,
        user_id="u-self",
        content="anchor raw",
        timestamp=50,
        message_id="m-anchor",
        reply_to_msg_id="m-anchor-reply-target",
        reply_to_user_id="u-anchor-reply-target",
        mentioned_ids='["u-anchor-mention"]',
        thread_id="thread-1",
    )
    anchor_mention = _insert_message(
        db_path,
        user_id="u-anchor-mention",
        content="mentioned by anchor",
        timestamp=60,
        message_id="m-anchor-mention",
    )
    after_reply = _insert_message(
        db_path,
        user_id="u-after-reply",
        content="RAW_SECRET_REPLY",
        timestamp=70,
        message_id="m-after-reply",
        reply_to_msg_id="m-anchor",
        mentioned_ids='["u-self"]',
        thread_id="thread-1",
    )

    window = scoped_profile.select_profile_evidence(anchor, db_path=db_path)

    assert [message.row_id for message in window.before] == [same_thread, mention, reply, replied_to_by_anchor]
    assert [message.relation for message in window.before] == [
        "same_thread",
        "mention",
        "mention",
        "reply",
    ]
    assert [message.row_id for message in window.after] == [anchor_mention, after_reply]
    assert [message.relation for message in window.after] == ["mention", "reply"]
    assert window.anchor.actor == "self"
    assert all(message.actor == "context" for message in (*window.before, *window.after))
    assert window.anchor.content == "anchor raw"
    assert next(message for message in window.after if message.row_id == after_reply).content == "RAW_SECRET_REPLY"
    ref = next(message for message in window.after if message.row_id == after_reply).to_ref()
    assert set(ref) == {"row_id", "message_id", "relation", "timestamp", "content_sha256"}
    assert ref["content_sha256"] == hashlib.sha256(b"RAW_SECRET_REPLY").hexdigest()


def test_evidence_limits_are_capped_and_output_is_stable_time_order(tmp_path: Path) -> None:
    db_path = tmp_path / "limits.db"
    _create_group_messages_db(db_path)
    for index in range(7):
        _insert_message(
            db_path,
            user_id=f"before-{index}",
            content=f"before raw {index}",
            timestamp=10 + index,
            message_id=f"before-message-{index}",
            thread_id="shared",
        )
    anchor = _insert_message(
        db_path,
        user_id="self",
        content="anchor",
        timestamp=20,
        message_id="anchor-message",
        thread_id="shared",
    )
    for index in range(7):
        _insert_message(
            db_path,
            user_id=f"after-{index}",
            content=f"after raw {index}",
            timestamp=30 + index,
            message_id=f"after-message-{index}",
            thread_id="shared",
        )

    window = scoped_profile.select_profile_evidence(
        anchor,
        db_path=db_path,
        before_limit=99,
        after_limit=99,
    )

    assert len(window.before) == 5
    assert len(window.after) == 5
    assert [message.timestamp for message in window.before] == sorted(message.timestamp for message in window.before)
    assert [message.timestamp for message in window.after] == sorted(message.timestamp for message in window.after)
    assert [message.timestamp for message in window.before] == [12, 13, 14, 15, 16]
    assert [message.timestamp for message in window.after] == [30, 31, 32, 33, 34]


def test_document_refs_never_persist_raw_evidence_content(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.db"
    _create_group_messages_db(db_path)
    context = _insert_message(
        db_path,
        user_id="other",
        content="RAW_CONTEXT_MUST_NOT_PERSIST",
        timestamp=1,
        message_id="context",
        thread_id="thread",
    )
    anchor = _insert_message(
        db_path,
        user_id="self",
        content="RAW_ANCHOR_MUST_NOT_PERSIST",
        timestamp=2,
        message_id="anchor",
        thread_id="thread",
    )
    window = scoped_profile.select_profile_evidence(anchor, db_path=db_path)
    context_message = next(message for message in window.before if message.row_id == context)
    document = scoped_profile.build_group_profile_document(
        "g1",
        claims=[
            {
                "key": "recent_focus",
                "value": "测试 evidence refs",
                "source": "evidence_derived",
                "evidence_refs": [context_message],
            }
        ],
        evidence_windows=[window],
    )

    serialized = json.dumps(document, ensure_ascii=False)
    assert "RAW_CONTEXT_MUST_NOT_PERSIST" not in serialized
    assert "RAW_ANCHOR_MUST_NOT_PERSIST" not in serialized
    assert "content_sha256" in serialized
    assert "\"content\"" not in serialized


def test_selector_rejects_bot_and_plugin_anchors(tmp_path: Path) -> None:
    db_path = tmp_path / "anchors.db"
    _create_group_messages_db(db_path)
    bot_anchor = _insert_message(
        db_path,
        user_id="bot",
        content="bot",
        timestamp=1,
        message_id="bot",
        is_bot=1,
        source_kind="bot",
    )
    plugin_anchor = _insert_message(
        db_path,
        user_id="plugin",
        content="plugin",
        timestamp=2,
        message_id="plugin",
        source_kind="plugin",
    )

    with pytest.raises(scoped_profile.ProfileEvidenceError):
        scoped_profile.select_profile_evidence(bot_anchor, db_path=db_path)
    with pytest.raises(scoped_profile.ProfileEvidenceError):
        scoped_profile.select_profile_evidence(plugin_anchor, db_path=db_path)


def test_group_transfer_normalizes_nested_scoped_document_and_rejects_cross_group() -> None:
    document = scoped_profile.build_group_profile_document(
        "g1",
        claims=[
            {
                "key": "recent_focus",
                "value": "准备比赛",
                "source": "evidence_derived",
                "evidence_refs": [
                    {
                        "row_id": 1,
                        "message_id": "m1",
                        "relation": "reply",
                        "timestamp": 1,
                        "content": "RAW_TRANSFER_CONTENT",
                    }
                ],
            }
        ],
    )

    normalized = data_transfer_service.DataTransferService._safe_profile_json(
        document,
        expected_group_id="g1",
    )
    serialized = json.dumps(normalized, ensure_ascii=False)
    assert "RAW_TRANSFER_CONTENT" not in serialized
    assert "content_sha256" in serialized
    with pytest.raises(ValueError, match="group mismatch"):
        data_transfer_service.DataTransferService._safe_profile_json(
            document,
            expected_group_id="g2",
        )


def test_selector_bounds_related_history_to_anchor_neighborhood(tmp_path: Path) -> None:
    db_path = tmp_path / "bounded.db"
    _create_group_messages_db(db_path)
    _insert_message(
        db_path,
        user_id="old-context",
        content="old thread context",
        timestamp=1,
        message_id="old-context",
        thread_id="shared",
    )
    for index in range(70):
        _insert_message(
            db_path,
            user_id=f"filler-{index}",
            content="filler",
            timestamp=100 + index,
            message_id=f"filler-{index}",
        )
    anchor = _insert_message(
        db_path,
        user_id="self",
        content="anchor",
        timestamp=200,
        message_id="anchor",
        thread_id="shared",
    )

    window = scoped_profile.select_profile_evidence(anchor, db_path=db_path)

    assert all(message.user_id != "old-context" for message in window.before)


def test_transfer_validation_rejects_cross_group_scoped_document() -> None:
    service = object.__new__(data_transfer_service.DataTransferService)
    document = scoped_profile.build_group_profile_document("g1")

    with pytest.raises(
        data_transfer_service.DataTransferError,
        match="cross-group local profile",
    ):
        service._validate_values(  # noqa: SLF001
            {"scope": {"group_id": "g2"}},
            {
                "local_user_profiles": [
                    {
                        "user_id": "u1",
                        "profile_text": "",
                        "profile_json": document,
                        "updated_at": 0,
                    }
                ]
            },
        )


def test_selector_neighborhood_is_counted_within_group(tmp_path: Path) -> None:
    db_path = tmp_path / "cross-group-fillers.db"
    _create_group_messages_db(db_path)
    related = _insert_message(
        db_path,
        group_id="g1",
        user_id="context",
        content="same group context",
        timestamp=100,
        message_id="related",
        thread_id="shared",
    )
    for index in range(70):
        _insert_message(
            db_path,
            group_id="g2",
            user_id=f"other-{index}",
            content="other group filler",
            timestamp=101 + index,
            message_id=f"other-{index}",
        )
    anchor = _insert_message(
        db_path,
        group_id="g1",
        user_id="self",
        content="anchor",
        timestamp=200,
        message_id="anchor",
        thread_id="shared",
    )

    window = scoped_profile.select_profile_evidence(anchor, db_path=db_path)

    assert [message.row_id for message in window.before] == [related]


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "2", "scope": {"kind": "group", "group_id": "g1"}},
        {"schema_version": 3, "scope": {"kind": "group", "group_id": "g1"}},
        {"scope": {"kind": "group", "group_id": "g1"}, "claims": []},
    ],
)
def test_transfer_rejects_malformed_scoped_profile_schema(payload: dict) -> None:
    with pytest.raises(ValueError, match="unsupported local scoped profile schema"):
        data_transfer_service.DataTransferService._safe_profile_json(
            payload,
            expected_group_id="g1",
        )


def test_selector_keeps_bounded_reply_to_user_fallback_outside_neighborhood(tmp_path: Path) -> None:
    db_path = tmp_path / "reply-user-fallback.db"
    _create_group_messages_db(db_path)
    anchor = _insert_message(
        db_path,
        group_id="g1",
        user_id="self",
        content="anchor",
        timestamp=100,
        message_id="anchor",
    )
    for index in range(70):
        _insert_message(
            db_path,
            group_id="g1",
            user_id=f"filler-{index}",
            content="filler",
            timestamp=101 + index,
            message_id=f"filler-{index}",
        )
    reply = _insert_message(
        db_path,
        group_id="g1",
        user_id="context",
        content="direct reply",
        timestamp=180,
        message_id="reply",
        reply_to_user_id="self",
    )

    window = scoped_profile.select_profile_evidence(anchor, db_path=db_path)

    assert any(message.row_id == reply and message.relation == "reply" for message in window.after)
