from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


action_executor_mod = load_personification_module("plugin.personification.agent.action_executor")
db = load_personification_module("plugin.personification.core.db")
qq_outbound = load_personification_module("plugin.personification.core.qq_outbound")


class _Logger:
    def warning(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


class _Bot:
    self_id = "bot-1"

    def __init__(self, results=None) -> None:  # noqa: ANN001
        self.results = list(results or [])
        self.sent: list[str] = []

    async def send(self, _event, message):  # noqa: ANN001
        self.sent.append(str(message))
        if not self.results:
            return None
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _event():  # noqa: ANN202
    return SimpleNamespace(group_id=20001, user_id=10001)


def test_action_executor_fake_ledger_wraps_every_send_surface() -> None:
    class _Ledger:
        def __init__(self) -> None:
            self.calls: list[tuple[object, str, str]] = []

        async def dispatch(self, context, content, send):  # noqa: ANN001, ANN202
            result = await send()
            part_index = len(self.calls)
            receipt = SimpleNamespace(
                operation_id=context.operation_id,
                part_index=part_index,
                surface=context.surface,
                message_id=result["message_id"],
                status="sent",
            )
            self.calls.append((context, str(content), receipt.message_id))
            return receipt

    async def _run():  # noqa: ANN202
        ledger = _Ledger()
        bot = _Bot({"message_id": f"message-{index}"} for index in range(1, 9))
        executor = action_executor_mod.ActionExecutor(
            bot,
            _event(),
            SimpleNamespace(),
            _Logger(),
            qq_outbound_ledger=ledger,
            operation_id="agent-operation",
            user_target="10002",
        )
        await executor.send_text("文本")
        await executor.send_image_b64("QUJD")
        await executor.execute("send_sticker", {"path": "sticker.png"})
        await executor.execute("send_qq_face", {"face_id": 182})
        await executor.execute("send_qq_image_expression", {"url": "https://example.test/a.png"})
        await executor.execute("send_image_url", {"url": "https://example.test/b.png"})
        await executor.execute("send_qq_mface", {"data": {"emoji_id": "face-1"}})
        await executor.execute("poke_user", {"user_id": "10002"})
        return executor, ledger

    executor, ledger = asyncio.run(_run())

    assert [call[0].surface for call in ledger.calls] == [
        "agent_action_text",
        "agent_action_image",
        "agent_action_sticker",
        "agent_action_qq_expression",
        "agent_action_qq_expression",
        "agent_action_image",
        "agent_action_qq_expression",
        "agent_action_poke",
    ]
    assert [call[0].operation_id for call in ledger.calls] == ["agent-operation"] * 8
    assert [call[0].user_target for call in ledger.calls] == ["10002"] * 8
    assert [receipt.message_id for receipt in executor.receipts] == [
        f"message-{index}" for index in range(1, 9)
    ]
    assert executor.last_delivery_confirmed is True


def test_action_executor_real_ledger_records_distinct_parts_and_message_ids(tmp_path) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    ledger = qq_outbound.QQOutboundLedger(db_path)

    async def _run():  # noqa: ANN202
        executor = action_executor_mod.ActionExecutor(
            _Bot(
                [
                    {"data": {"message_id": "message-1"}},
                    {"msg_id": "message-2"},
                    {"messageId": "message-3"},
                ]
            ),
            _event(),
            SimpleNamespace(),
            _Logger(),
            qq_outbound_ledger=ledger,
            operation_id="real-agent-operation",
        )
        await executor.send_text("第一段")
        await executor.execute("send_image_url", {"url": "https://example.test/image.png"})
        await executor.execute("poke_user", {"user_id": "10001"})
        return executor

    executor = asyncio.run(_run())

    assert [receipt.part_index for receipt in executor.receipts] == [0, 1, 2]
    assert [receipt.message_id for receipt in executor.receipts] == [
        "message-1",
        "message-2",
        "message-3",
    ]
    assert [receipt.surface for receipt in executor.receipts] == [
        "agent_action_text",
        "agent_action_image",
        "agent_action_poke",
    ]
    candidates = ledger.list_recall_candidates(
        bot_id="bot-1",
        conversation_kind="group",
        conversation_id="20001",
    )
    assert "message-3" not in {candidate.message_id for candidate in candidates}


def test_action_executor_send_exception_stays_unknown(tmp_path) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    ledger = qq_outbound.QQOutboundLedger(db_path)
    executor = action_executor_mod.ActionExecutor(
        _Bot([RuntimeError("send failed")]),
        _event(),
        SimpleNamespace(),
        _Logger(),
        qq_outbound_ledger=ledger,
        operation_id="agent-send-error",
    )

    with pytest.raises(RuntimeError, match="send failed"):
        asyncio.run(executor.execute("send_sticker", {"path": "sticker.png"}))

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, message_id, surface, error_code FROM qq_outbound_ledger WHERE operation_id=?",
            ("agent-send-error",),
        ).fetchone()
    assert executor.last_delivery_confirmed is False
    assert executor.receipts == []
    assert row == ("unknown", None, "agent_action_sticker", "RuntimeError")


def test_action_executor_missing_message_id_is_not_confirmed(tmp_path) -> None:  # noqa: ANN001
    db_path = db.init_db_sync(tmp_path)
    ledger = qq_outbound.QQOutboundLedger(db_path)
    executor = action_executor_mod.ActionExecutor(
        _Bot([{"status": "ok"}]),
        _event(),
        SimpleNamespace(),
        _Logger(),
        qq_outbound_ledger=ledger,
        operation_id="agent-missing-message-id",
    )

    asyncio.run(executor.execute("send_sticker", {"path": "sticker.png"}))

    assert executor.last_delivery_confirmed is False
    assert len(executor.receipts) == 1
    assert executor.receipts[0].status == "unknown"


def test_action_executor_without_ledger_keeps_legacy_send_behavior() -> None:
    bot = _Bot()
    executor = action_executor_mod.ActionExecutor(
        bot,
        object(),
        SimpleNamespace(),
        _Logger(),
    )

    result = asyncio.run(executor.execute("poke_user", {"user_id": "10001"}))

    assert result == "已戳"
    assert bot.sent == ["[CQ:poke,qq=10001]"]
    assert executor.last_delivery_confirmed is True
    assert executor.receipts == []
