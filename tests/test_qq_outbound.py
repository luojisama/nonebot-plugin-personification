from __future__ import annotations

import asyncio
import hmac
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
qq_outbound = load_personification_module("plugin.personification.core.qq_outbound")
user_policy = load_personification_module("plugin.personification.core.user_policy")


def _context(
    operation_id: str,
    *,
    bot_id: str = "bot-1",
    kind: str = "group",
    conversation_id: str = "group-1",
    surface: str = "normal_reply",
) -> object:
    return qq_outbound.OutboundContext(
        operation_id=operation_id,
        bot_id=bot_id,
        conversation_kind=kind,
        conversation_id=conversation_id,
        user_target="user-1",
        surface=surface,
    )


def _ledger(tmp_path, *, key: bytes | None = None):  # noqa: ANN001, ANN202
    db_path = db.init_db_sync(tmp_path)
    return qq_outbound.QQOutboundLedger(db_path, content_hmac_key=key), db_path


def test_schema_has_constraints_and_required_indexes(tmp_path) -> None:  # noqa: ANN001
    _ledger_instance, db_path = _ledger(tmp_path)
    with sqlite3.connect(db_path) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(qq_outbound_ledger)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(qq_outbound_ledger)")}
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='qq_outbound_ledger'"
        ).fetchone()[0]

    assert {
        "id",
        "operation_id",
        "part_index",
        "bot_id",
        "conversation_kind",
        "conversation_id",
        "message_id",
        "user_target",
        "surface",
        "status",
        "preview",
        "content_hmac",
        "error_code",
        "created_at",
        "updated_at",
        "recalled_at",
    } <= set(columns)
    assert columns["message_id"][3] == 0
    assert {
        "idx_qq_outbound_operation_part",
        "idx_qq_outbound_scope",
        "idx_qq_outbound_message",
        "idx_qq_outbound_status",
    } <= indexes
    assert "'group', 'private'" in table_sql
    assert "'sent', 'failed', 'unknown'" in table_sql


def test_begin_allocates_operation_parts_atomically(tmp_path) -> None:  # noqa: ANN001
    ledger, db_path = _ledger(tmp_path)
    context = _context("operation-concurrent")

    with ThreadPoolExecutor(max_workers=12) as pool:
        receipts = list(pool.map(lambda index: ledger.begin(context, f"part {index}"), range(24)))

    assert sorted(receipt.part_index for receipt in receipts) == list(range(24))
    assert all(receipt.status == "unknown" and receipt.message_id is None for receipt in receipts)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT part_index FROM qq_outbound_ledger WHERE operation_id=? ORDER BY part_index",
            ("operation-concurrent",),
        ).fetchall()
    assert [row[0] for row in rows] == list(range(24))


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"message_id": 12}, "12"),
        ({"msg_id": "13"}, "13"),
        ({"messageId": "adapter-id"}, "adapter-id"),
        ({"status": "ok", "data": {"message_id": 14}}, "14"),
        ({"data": [{"id": 99}, {"data": {"msg_id": 15}}]}, "15"),
        ([{"data": [{"messageId": 16}]}], "16"),
        ({"id": 17}, None),
        ({"data": {"id": 18}}, None),
        (19, None),
        ({"result": {"message_id": 20}}, None),
    ],
)
def test_onebot_message_id_parser_is_strict(payload, expected) -> None:  # noqa: ANN001
    assert qq_outbound.parse_onebot_message_id(payload) == expected


def test_context_builder_uses_exact_event_scope() -> None:
    group = qq_outbound.build_outbound_context(
        bot=type("Bot", (), {"self_id": "bot-1"})(),
        event=type("Event", (), {"group_id": 20001, "user_id": 10001})(),
        surface="normal_reply",
        operation_id="trace-1",
    )
    private = qq_outbound.build_outbound_context(
        bot=type("Bot", (), {"self_id": "bot-2"})(),
        event=type("Event", (), {"user_id": 10002})(),
        surface="yaml_reply",
    )

    assert (group.bot_id, group.conversation_kind, group.conversation_id) == (
        "bot-1",
        "group",
        "20001",
    )
    assert group.user_target == "10001"
    assert group.operation_id == "trace-1"
    assert (private.bot_id, private.conversation_kind, private.conversation_id) == (
        "bot-2",
        "private",
        "10002",
    )
    assert private.operation_id.startswith("qq:")


def test_preview_redacts_sensitive_material_and_hmac_requires_key(tmp_path) -> None:  # noqa: ANN001
    key = b"k" * 32
    content = (
        "hello https://example.com/path?token=url-secret "
        "file=file:///D:/secret/voice.wav path=C:\\secret\\sticker.png "
        "data:image/png;base64,QUJDREVGR0g= "
        "iVBORw0KGgo= "
        "base64=VGhpcy1tdXN0LW5vdC1iZS1wZXJzaXN0ZWQ= "
        "api_key=credential-secret Bearer bearer-secret tail"
    )
    ledger, db_path = _ledger(tmp_path, key=key)
    receipt = ledger.begin(_context("redaction"), content)
    without_key = qq_outbound.QQOutboundLedger(db_path).begin(_context("no-hmac"), content)

    assert len(receipt.preview) <= 120
    assert "https://" not in receipt.preview
    assert "D:/secret" not in receipt.preview
    assert "C:\\secret" not in receipt.preview
    assert "QUJDREVGR0g" not in receipt.preview
    assert "iVBORw0KGgo" not in receipt.preview
    assert "credential-secret" not in receipt.preview
    assert "bearer-secret" not in receipt.preview
    assert receipt.content_hmac == hmac.new(key, content.encode("utf-8"), "sha256").hexdigest()
    assert without_key.content_hmac == ""
    with pytest.raises(ValueError, match="32 bytes"):
        qq_outbound.QQOutboundLedger(db_path, content_hmac_key=b"short")


def test_recent_admin_view_is_filterable_and_never_exposes_hmac(tmp_path) -> None:
    ledger, _db_path = _ledger(tmp_path, key=b"k" * 32)
    sent = asyncio.run(
        ledger.dispatch(
            _context("recent-sent"),
            "visible https://secret.example token=hidden",
            lambda: {"message_id": 123},
            now=100,
        )
    )
    ledger.begin(
        _context("recent-private", kind="private", conversation_id="10001"),
        "private",
        now=101,
    )

    rows = ledger.list_recent(
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        status="sent",
        recalled=False,
        limit=10,
    )

    assert len(rows) == 1
    assert rows[0]["operation_id"] == "recent-sent"
    assert rows[0]["message_id"] == "123"
    assert "secret.example" not in rows[0]["preview"]
    assert set(rows[0]) == {
        "operation_id",
        "part_index",
        "bot_id",
        "conversation_kind",
        "conversation_id",
        "surface",
        "message_id",
        "status",
        "preview",
        "created_at",
        "updated_at",
        "recalled_at",
        "recall_status",
    }
    assert sent.content_hmac
    assert "content_hmac" not in rows[0]
    assert "user_target" not in rows[0]

    with pytest.raises(ValueError, match="invalid qq outbound status"):
        ledger.list_recent(status="dispatching")


def test_shared_policy_key_remains_available_for_hmac_without_aes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(user_policy, "AESGCM", None)

    key = user_policy.load_or_create_policy_evidence_key(tmp_path)

    assert isinstance(key, bytes)
    assert len(key) == 32
    assert user_policy.PolicyEvidenceCipher(key).available is False


def test_dispatch_persists_unknown_before_send_and_marks_strict_receipt_sent(tmp_path) -> None:  # noqa: ANN001
    ledger, db_path = _ledger(tmp_path)
    observed: dict[str, object] = {}

    async def send():  # noqa: ANN202
        with sqlite3.connect(db_path) as conn:
            observed["row"] = conn.execute(
                "SELECT status,message_id FROM qq_outbound_ledger WHERE operation_id='dispatch-success'"
            ).fetchone()
        return {"status": "ok", "data": [{"msg_id": "message-42"}]}

    receipt = asyncio.run(
        ledger.dispatch(_context("dispatch-success"), "visible content", send, now=100.0)
    )

    assert observed["row"] == ("unknown", None)
    assert receipt.status == "sent"
    assert receipt.message_id == "message-42"
    assert receipt.error_code == ""


def test_dispatch_exception_stays_unknown_saves_no_raw_exception_and_reraises(tmp_path) -> None:  # noqa: ANN001
    ledger, db_path = _ledger(tmp_path)

    class SecretSendError(RuntimeError):
        pass

    async def send():  # noqa: ANN202
        raise SecretSendError("raw-exception-secret-must-not-persist")

    with pytest.raises(SecretSendError, match="raw-exception-secret"):
        asyncio.run(ledger.dispatch(_context("dispatch-error"), "safe preview", send))

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM qq_outbound_ledger WHERE operation_id='dispatch-error'"
        ).fetchone()
    assert row["status"] == "unknown"
    assert row["message_id"] is None
    assert row["error_code"] == "SecretSendError"
    assert "raw-exception-secret" not in "\n".join(str(value) for value in row)


def test_invalid_normal_send_result_remains_unknown(tmp_path) -> None:  # noqa: ANN001
    ledger, _db_path = _ledger(tmp_path)

    receipt = asyncio.run(
        ledger.dispatch(
            _context("invalid-result"),
            "content",
            lambda: {"status": "ok", "data": {"id": 999}},
        )
    )

    assert receipt.status == "unknown"
    assert receipt.message_id is None
    assert receipt.error_code == "message_id_missing"


def test_recall_candidates_are_scope_filtered_deduplicated_and_cas_marked(tmp_path) -> None:  # noqa: ANN001
    ledger, _db_path = _ledger(tmp_path)

    async def dispatch(
        operation_id: str,
        message_id: str,
        created_at: float,
        *,
        bot_id: str = "bot-1",
        kind: str = "group",
        conversation_id: str = "group-1",
        surface: str = "normal_reply",
    ):
        return await ledger.dispatch(
            _context(
                operation_id,
                bot_id=bot_id,
                kind=kind,
                conversation_id=conversation_id,
                surface=surface,
            ),
            operation_id,
            lambda: {"message_id": message_id},
            now=created_at,
        )

    duplicate_old = asyncio.run(dispatch("duplicate-old", "message-1", 91.0))
    duplicate_new = asyncio.run(dispatch("duplicate-new", "message-1", 95.0))
    newest = asyncio.run(dispatch("newest", "message-2", 96.0))
    blocked_surface = asyncio.run(
        dispatch("admin", "message-3", 97.0, surface="admin_command")
    )
    other_bot = asyncio.run(dispatch("other-bot", "message-4", 98.0, bot_id="bot-2"))
    other_group = asyncio.run(
        dispatch("other-group", "message-5", 99.0, conversation_id="group-2")
    )
    private = asyncio.run(
        dispatch("private", "message-6", 99.0, kind="private", conversation_id="group-1")
    )
    stale = asyncio.run(dispatch("stale", "message-7", 70.0))
    unknown = ledger.begin(_context("unknown"), "not sent", now=99.5)
    failed = ledger.begin(_context("failed"), "failed", now=99.5)
    sent_without_id = ledger.begin(_context("sent-without-id"), "missing id", now=99.5)
    sent_with_empty_id = ledger.begin(_context("sent-empty-id"), "empty id", now=99.5)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("UPDATE qq_outbound_ledger SET status='failed' WHERE id=?", (failed.id,))
        conn.execute("UPDATE qq_outbound_ledger SET status='sent' WHERE id=?", (sent_without_id.id,))
        conn.execute(
            "UPDATE qq_outbound_ledger SET status='sent', message_id='' WHERE id=?",
            (sent_with_empty_id.id,),
        )
        conn.commit()

    candidates = ledger.list_recall_candidates(
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        now=100.0,
        window_seconds=20.0,
        limit=10,
    )

    assert [candidate.id for candidate in candidates] == [newest.id, duplicate_new.id]
    assert ledger.list_recall_candidates(
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        user_target="another-user",
        now=100.0,
        window_seconds=20.0,
    ) == []
    assert duplicate_old.id not in {candidate.id for candidate in candidates}
    assert blocked_surface.id not in {candidate.id for candidate in candidates}
    assert other_bot.id not in {candidate.id for candidate in candidates}
    assert other_group.id not in {candidate.id for candidate in candidates}
    assert private.id not in {candidate.id for candidate in candidates}
    assert stale.id not in {candidate.id for candidate in candidates}
    assert unknown.id not in {candidate.id for candidate in candidates}
    assert failed.id not in {candidate.id for candidate in candidates}
    assert sent_without_id.id not in {candidate.id for candidate in candidates}
    assert sent_with_empty_id.id not in {candidate.id for candidate in candidates}
    assert ledger.list_recall_candidates(
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        since=90.0,
        until=100.0,
        limit=1,
    ) == [newest]

    assert ledger.mark_recalled(
        newest.id,
        bot_id="bot-2",
        conversation_kind="group",
        conversation_id="group-1",
        recalled_at=101.0,
    ) is False
    assert ledger.mark_recalled(
        newest.id,
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        recalled_at=101.0,
    ) is True
    assert ledger.mark_recalled(
        newest.id,
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        recalled_at=102.0,
    ) is False
    remaining = ledger.list_recall_candidates(
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        now=103.0,
        window_seconds=20.0,
        limit=10,
    )
    assert [candidate.id for candidate in remaining] == [duplicate_new.id]
    assert ledger.mark_recalled(
        duplicate_new.id,
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        recalled_at=104.0,
    ) is True
    assert ledger.list_recall_candidates(
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
        now=105.0,
        window_seconds=20.0,
        limit=10,
    ) == []


def test_mark_recalled_rejects_unknown_rows(tmp_path) -> None:  # noqa: ANN001
    ledger, _db_path = _ledger(tmp_path)
    receipt = ledger.begin(_context("unknown-recall"), "not dispatched")

    assert ledger.mark_recalled(
        receipt.id,
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
    ) is False


def test_default_recall_surfaces_cover_all_recallable_qq_outputs() -> None:
    assert {
        "normal_reply",
        "yaml_reply",
        "reply_ack",
        "reply_tts",
        "reply_translation_forward",
        "agent_action_text",
        "agent_action_image",
        "agent_action_sticker",
        "agent_action_qq_expression",
        "proactive_private",
        "proactive_group_idle",
        "scheduled_user_task",
        "social_topic_followup",
        "social_news_private",
        "social_news_group",
        "social_greeting",
        "social_festival_greeting",
    } <= qq_outbound.DEFAULT_SOCIAL_RECALL_SURFACES
