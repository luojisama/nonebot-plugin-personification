from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


action_executor_mod = load_personification_module("plugin.personification.agent.action_executor")
db = load_personification_module("plugin.personification.core.db")
reply_commit = load_personification_module("plugin.personification.handlers.reply_commit")
persona_admin_commands = load_personification_module(
    "plugin.personification.handlers.persona_admin_commands"
)
protocol_adapter = load_personification_module("plugin.personification.core.protocol_adapter")
qq_outbound = load_personification_module("plugin.personification.core.qq_outbound")
qq_recall = load_personification_module("plugin.personification.core.qq_recall")


class _Logger:
    def info(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    def warning(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


class _Bot:
    def __init__(self, self_id: str = "90001") -> None:
        self.self_id = self_id


class _Adapter:
    def __init__(self, results=None) -> None:  # noqa: ANN001
        self.results = list(results or [])
        self.calls: list[int] = []

    async def recall_message(self, *, message_id: int):  # noqa: ANN202
        self.calls.append(message_id)
        if self.results:
            return self.results.pop(0)
        return protocol_adapter.ProtocolResult("succeeded", "ok")


def _event(
    *,
    group_id: str = "20001",
    user_id: str = "10001",
    reply_message_id: int | None = None,
    reply_sender_id: str = "90001",
):  # noqa: ANN202
    event = SimpleNamespace(
        group_id=int(group_id),
        user_id=int(user_id),
        message_type="group",
    )
    if reply_message_id is not None:
        event.reply = SimpleNamespace(
            message_id=reply_message_id,
            sender=SimpleNamespace(user_id=int(reply_sender_id)),
        )
    return event


def _private_event(user_id: str = "10001"):  # noqa: ANN202
    return SimpleNamespace(user_id=int(user_id), message_type="private")


def _ledger(tmp_path):  # noqa: ANN001, ANN202
    return qq_outbound.QQOutboundLedger(db.init_db_sync(tmp_path))


def _seed(
    ledger,
    *,
    operation_id: str,
    message_ids: list[object],
    created_at: float,
    bot_id: str = "90001",
    conversation_kind: str = "group",
    conversation_id: str = "20001",
    user_target: str = "10001",
    surface: str = "normal_reply",
) -> None:
    async def _run() -> None:
        for index, message_id in enumerate(message_ids):
            context = qq_outbound.OutboundContext(
                operation_id=operation_id,
                bot_id=bot_id,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
                user_target=user_target,
                surface=surface,
            )
            await ledger.dispatch(
                context,
                f"part-{index}",
                lambda value=message_id: {"message_id": value},
                now=created_at + index * 0.01,
            )

    asyncio.run(_run())


def _service(ledger, adapter: _Adapter, *, now: float = 100.0):  # noqa: ANN001, ANN202
    return qq_recall.QQRecallService(
        ledger,
        logger=_Logger(),
        protocol_adapter_getter=lambda *_args, **_kwargs: adapter,
        clock=lambda: now,
    )


def test_recall_schema_and_message_id_validation(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        operation_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(qq_recall_operations)")
        }
        item_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(qq_recall_items)")
        }
    assert {
        "outbound_operation_id",
        "bot_id",
        "conversation_kind",
        "conversation_id",
        "actor_kind",
        "trigger_kind",
        "status",
        "total_count",
        "recalled_count",
    } <= operation_columns
    assert {"recall_operation_id", "ledger_id", "message_id", "status"} <= item_columns
    assert qq_recall.normalize_recall_message_id("-2147483648") == -2147483648
    assert qq_recall.normalize_recall_message_id("2147483647") == 2147483647
    for value in (True, 0, "0", "adapter-id", 2147483648, -2147483649, None):
        assert qq_recall.normalize_recall_message_id(value) is None


def test_user_recall_selects_latest_targeted_operation_and_all_parts(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="user-old", message_ids=[11, 12], created_at=90.0)
    _seed(
        ledger,
        operation_id="other-user",
        message_ids=[21],
        created_at=95.0,
        user_target="10002",
    )
    _seed(ledger, operation_id="current-turn", message_ids=[31], created_at=99.0)
    adapter = _Adapter()
    service = _service(ledger, adapter)

    result = asyncio.run(
        service.recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
            current_operation_id="current-turn",
        )
    )

    assert result.status == "succeeded"
    assert result.outbound_operation_id == "user-old"
    assert (result.total_count, result.recalled_count) == (2, 2)
    assert adapter.calls == [12, 11]
    with sqlite3.connect(ledger.db_path) as conn:
        operation = conn.execute(
            "SELECT status,total_count,recalled_count FROM qq_recall_operations"
        ).fetchone()
        recalled = conn.execute(
            "SELECT message_id,recalled_at FROM qq_outbound_ledger WHERE operation_id='user-old'"
        ).fetchall()
    assert operation == ("succeeded", 2, 2)
    assert all(timestamp > 0 for _message_id, timestamp in recalled)


def test_admin_exact_operation_recall_revalidates_scope_and_all_parts(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="older", message_ids=[11], created_at=90.0)
    _seed(ledger, operation_id="chosen", message_ids=[21, 22], created_at=95.0)
    adapter = _Adapter()
    service = _service(ledger, adapter)

    wrong_scope = asyncio.run(
        service.recall_operation(
            bot=_Bot(),
            operation_id="chosen",
            conversation_kind="group",
            conversation_id="20002",
            requester_user_id="90002",
            cutoff=100.0,
        )
    )
    assert wrong_scope.status == "no_candidate"
    assert adapter.calls == []

    result = asyncio.run(
        service.recall_operation(
            bot=_Bot(),
            operation_id="chosen",
            conversation_kind="group",
            conversation_id="20001",
            requester_user_id="90002",
            cutoff=100.0,
        )
    )
    assert result.status == "succeeded"
    assert result.outbound_operation_id == "chosen"
    assert (result.total_count, result.recalled_count) == (2, 2)
    assert adapter.calls == [22, 21]

    repeated = asyncio.run(
        service.recall_operation(
            bot=_Bot(),
            operation_id="chosen",
            conversation_kind="group",
            conversation_id="20001",
            requester_user_id="90002",
            cutoff=100.0,
        )
    )
    assert repeated.code == "already_attempted"
    assert adapter.calls == [22, 21]


def test_admin_exact_operation_never_partially_recalls_incomplete_operation(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="incomplete-admin", message_ids=[31, 32], created_at=95.0)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE qq_outbound_ledger SET status='unknown',message_id=NULL "
            "WHERE operation_id='incomplete-admin' AND part_index=1"
        )
        conn.commit()
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter).recall_operation(
            bot=_Bot(),
            operation_id="incomplete-admin",
            conversation_kind="group",
            conversation_id="20001",
            requester_user_id="90002",
            cutoff=100.0,
        )
    )

    assert result.status == "no_candidate"
    assert result.code == "operation_incomplete"
    assert adapter.calls == []


def test_admin_exact_operation_rejects_cross_scope_parts(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="cross-scope-admin", message_ids=[31], created_at=95.0)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            """
            INSERT INTO qq_outbound_ledger(
                operation_id,part_index,bot_id,conversation_kind,conversation_id,
                message_id,user_target,surface,status,preview,content_hmac,error_code,
                created_at,updated_at,recalled_at
            )
            SELECT operation_id,1,bot_id,conversation_kind,'20002',
                   '32',user_target,surface,status,preview,content_hmac,error_code,
                   95.1,95.1,0
            FROM qq_outbound_ledger
            WHERE operation_id='cross-scope-admin' AND part_index=0
            """
        )
        conn.commit()
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter).recall_operation(
            bot=_Bot(),
            operation_id="cross-scope-admin",
            conversation_kind="group",
            conversation_id="20001",
            requester_user_id="90002",
            cutoff=100.0,
        )
    )

    assert result.status == "no_candidate"
    assert result.code == "operation_scope_mismatch"
    assert adapter.calls == []


def test_admin_exact_operation_rejects_mixed_or_partly_recalled_parts(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="mixed-surface", message_ids=[41], created_at=95.0)
    _seed(
        ledger,
        operation_id="mixed-surface",
        message_ids=[42],
        created_at=95.1,
        surface="webui_diagnostic",
    )
    _seed(ledger, operation_id="partly-recalled", message_ids=[51, 52], created_at=96.0)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE qq_outbound_ledger SET recalled_at=99 "
            "WHERE operation_id='partly-recalled' AND part_index=1"
        )
        conn.commit()
    adapter = _Adapter()
    service = _service(ledger, adapter)

    mixed = asyncio.run(
        service.recall_operation(
            bot=_Bot(),
            operation_id="mixed-surface",
            conversation_kind="group",
            conversation_id="20001",
            requester_user_id="90002",
            cutoff=100.0,
        )
    )
    partial = asyncio.run(
        service.recall_operation(
            bot=_Bot(),
            operation_id="partly-recalled",
            conversation_kind="group",
            conversation_id="20001",
            requester_user_id="90002",
            cutoff=100.0,
        )
    )

    assert mixed.code == "operation_surface_not_allowed"
    assert partial.code == "already_recalled"
    assert adapter.calls == []


def test_operation_crossing_request_cutoff_is_never_partially_recalled(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="complete", message_ids=[13], created_at=90.0)
    _seed(
        ledger,
        operation_id="crossing",
        message_ids=[14, 15],
        created_at=99.995,
    )
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter).recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )

    assert result.status == "no_candidate"
    assert result.code == "operation_outside_window"
    assert result.outbound_operation_id == "crossing"
    assert adapter.calls == []


def test_operation_confirmed_after_request_cutoff_is_not_recalled(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="confirmed", message_ids=[16], created_at=90.0)
    _seed(ledger, operation_id="late-confirmation", message_ids=[17], created_at=99.0)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE qq_outbound_ledger SET updated_at=101 WHERE operation_id='late-confirmation'"
        )
        conn.commit()
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter).recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )

    assert result.outbound_operation_id == "confirmed"
    assert adapter.calls == [16]


def test_operation_crossing_window_start_is_never_partially_recalled(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="complete", message_ids=[18], created_at=95.0)
    _seed(
        ledger,
        operation_id="crossing-start",
        message_ids=[19, 20],
        created_at=94.995,
    )
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter).recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
            window_seconds=5.0,
        )
    )

    assert result.status == "no_candidate"
    assert result.code == "operation_outside_window"
    assert result.outbound_operation_id == "crossing-start"
    assert adapter.calls == []


def test_preexisting_inflight_part_blocks_partial_operation_recall(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="complete", message_ids=[24], created_at=90.0)
    _seed(ledger, operation_id="inflight", message_ids=[25, 26], created_at=95.0)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            """
            UPDATE qq_outbound_ledger
            SET status='unknown', message_id=NULL
            WHERE operation_id='inflight' AND part_index=1
            """
        )
        conn.commit()
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter).recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )

    assert result.status == "no_candidate"
    assert result.code == "operation_incomplete"
    assert result.outbound_operation_id == "inflight"
    assert adapter.calls == []


def test_current_turn_ack_is_recalled_with_previous_operation(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="previous", message_ids=[22], created_at=95.0)
    _seed(
        ledger,
        operation_id="current-turn",
        message_ids=[23],
        created_at=101.0,
        surface="reply_ack",
    )
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter, now=105.0).recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
            current_operation_id="current-turn",
        )
    )

    assert result.outbound_operation_id == "previous"
    assert (result.total_count, result.recalled_count) == (2, 2)
    assert adapter.calls == [23, 22]


def test_trusted_quote_selects_quoted_operation_but_user_quote_is_ignored(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="quoted", message_ids=[41, 42], created_at=90.0)
    _seed(ledger, operation_id="latest", message_ids=[51], created_at=95.0)

    trusted_adapter = _Adapter()
    trusted = asyncio.run(
        _service(ledger, trusted_adapter).recall_latest(
            bot=_Bot(),
            event=_event(reply_message_id=41, reply_sender_id="90001"),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )
    assert trusted.outbound_operation_id == "quoted"
    assert trusted_adapter.calls == [42, 41]

    ledger2 = _ledger(tmp_path / "untrusted")
    _seed(ledger2, operation_id="quoted", message_ids=[61], created_at=90.0)
    _seed(ledger2, operation_id="latest", message_ids=[71], created_at=95.0)
    untrusted_adapter = _Adapter()
    untrusted = asyncio.run(
        _service(ledger2, untrusted_adapter).recall_latest(
            bot=_Bot(),
            event=_event(reply_message_id=61, reply_sender_id="10002"),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )
    assert untrusted.outbound_operation_id == "latest"
    assert untrusted_adapter.calls == [71]


@pytest.mark.parametrize(
    ("bot", "event", "requester"),
    [
        (_Bot("90002"), _event(), "10001"),
        (_Bot(), _event(group_id="20002"), "10001"),
        (_Bot(), _event(user_id="10002"), "10002"),
        (_Bot(), _private_event("10001"), "10001"),
    ],
)
def test_user_recall_cannot_cross_bot_conversation_or_target(
    tmp_path, bot, event, requester  # noqa: ANN001
) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="scoped", message_ids=[81], created_at=95.0)
    adapter = _Adapter()

    result = asyncio.run(
        _service(ledger, adapter).recall_latest(
            bot=bot,
            event=event,
            requester_user_id=requester,
            cutoff=100.0,
        )
    )

    assert result.status == "no_candidate"
    assert adapter.calls == []


def test_concurrent_recall_claim_dispatches_each_message_once(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="concurrent", message_ids=[91, 92], created_at=95.0)

    class _SlowAdapter(_Adapter):
        async def recall_message(self, *, message_id: int):  # noqa: ANN202
            self.calls.append(message_id)
            await asyncio.sleep(0.02)
            return protocol_adapter.ProtocolResult("succeeded", "ok")

    adapter = _SlowAdapter()
    service = _service(ledger, adapter)

    async def _run():  # noqa: ANN202
        kwargs = dict(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
        return await asyncio.gather(
            service.recall_latest(**kwargs),
            service.recall_latest(**kwargs),
        )

    results = asyncio.run(_run())
    assert sorted(result.status for result in results) == ["no_candidate", "succeeded"]
    assert adapter.calls == [92, 91]


def test_unknown_stops_remaining_items_and_is_never_retried(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="unknown", message_ids=[101, 102], created_at=95.0)
    adapter = _Adapter([protocol_adapter.ProtocolResult("degraded", "timeout")])
    service = _service(ledger, adapter)

    first = asyncio.run(
        service.recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )
    second = asyncio.run(
        service.recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )

    assert first.status == "unknown"
    assert second.code == "already_attempted"
    assert adapter.calls == [102]
    with sqlite3.connect(ledger.db_path) as conn:
        statuses = conn.execute(
            "SELECT status FROM qq_recall_items ORDER BY part_index DESC"
        ).fetchall()
        recalled = conn.execute(
            "SELECT recalled_at FROM qq_outbound_ledger WHERE operation_id='unknown'"
        ).fetchall()
    assert statuses == [("unknown",), ("unknown",)]
    assert recalled == [(0.0,), (0.0,)]


def test_startup_recovery_marks_interrupted_claim_unknown_without_dispatch(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="interrupted", message_ids=[103, 104], created_at=95.0)
    service = _service(ledger, _Adapter())
    claim = service._selection_sync(  # noqa: SLF001
        bot_id="90001",
        conversation_kind="group",
        conversation_id="20001",
        requester_user_id="10001",
        actor_kind="user",
        trigger_kind="agent",
        cutoff=100.0,
        current_operation_id="",
        preferred_message_id=None,
        window_seconds=300.0,
        surfaces=None,
        claim=True,
    )
    assert isinstance(claim, qq_recall.RecallClaim)

    recovered = service.recover_interrupted_dispatches()

    assert recovered == 1
    with sqlite3.connect(ledger.db_path) as conn:
        operation = conn.execute(
            "SELECT status,error_code,recalled_count FROM qq_recall_operations"
        ).fetchone()
        items = conn.execute("SELECT status,error_code FROM qq_recall_items").fetchall()
    assert operation == ("unknown", "process_interrupted", 0)
    assert items == [
        ("unknown", "process_interrupted"),
        ("unknown", "process_interrupted"),
    ]


def test_recall_claim_seals_operation_against_late_parts(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="sealed", message_ids=[106], created_at=95.0)
    service = _service(ledger, _Adapter())
    claim = service._selection_sync(  # noqa: SLF001
        bot_id="90001",
        conversation_kind="group",
        conversation_id="20001",
        requester_user_id="10001",
        actor_kind="user",
        trigger_kind="agent",
        cutoff=100.0,
        current_operation_id="",
        preferred_message_id=None,
        window_seconds=300.0,
        surfaces=None,
        claim=True,
    )
    assert isinstance(claim, qq_recall.RecallClaim)
    send_calls = 0

    async def _late_send():  # noqa: ANN202
        nonlocal send_calls
        send_calls += 1
        return {"message_id": 107}

    with pytest.raises(RuntimeError, match="sealed by a recall claim"):
        asyncio.run(
            ledger.dispatch(
                qq_outbound.OutboundContext(
                    operation_id="sealed",
                    bot_id="90001",
                    conversation_kind="group",
                    conversation_id="20001",
                    user_target="10001",
                    surface="normal_reply",
                ),
                "late part",
                _late_send,
                now=101.0,
            )
        )
    assert send_calls == 0


def test_cancelled_protocol_call_is_persisted_unknown_and_reraised(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="cancelled", message_ids=[105], created_at=95.0)

    class _CancelledAdapter:
        async def recall_message(self, *, message_id: int):  # noqa: ANN202
            _ = message_id
            raise asyncio.CancelledError

    service = qq_recall.QQRecallService(
        ledger,
        logger=_Logger(),
        protocol_adapter_getter=lambda *_args, **_kwargs: _CancelledAdapter(),
        clock=lambda: 100.0,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.recall_latest(
                bot=_Bot(),
                event=_event(),
                requester_user_id="10001",
                cutoff=100.0,
            )
        )
    with sqlite3.connect(ledger.db_path) as conn:
        operation = conn.execute(
            "SELECT status,error_code FROM qq_recall_operations"
        ).fetchone()
    assert operation == ("unknown", "cancelled")


def test_definite_failure_and_opaque_id_never_mark_recalled(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="unsupported", message_ids=[111, 112], created_at=90.0)
    adapter = _Adapter(
        [
            protocol_adapter.ProtocolResult("unavailable", "action_not_found"),
            protocol_adapter.ProtocolResult("unavailable", "action_not_found"),
        ]
    )
    result = asyncio.run(
        _service(ledger, adapter).recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )
    assert result.status == "definite_failure"
    assert adapter.calls == [112, 111]

    opaque_ledger = _ledger(tmp_path / "opaque")
    _seed(
        opaque_ledger,
        operation_id="opaque",
        message_ids=["adapter-id"],
        created_at=95.0,
    )
    opaque_adapter = _Adapter()
    opaque = asyncio.run(
        _service(opaque_ledger, opaque_adapter).recall_latest(
            bot=_Bot(),
            event=_event(),
            requester_user_id="10001",
            cutoff=100.0,
        )
    )
    assert opaque.status == "definite_failure"
    assert opaque.code == "invalid_message_id"
    assert opaque_adapter.calls == []


def test_agent_tool_is_zero_argument_and_only_queues_internal_action(tmp_path) -> None:  # noqa: ANN001
    ledger = _ledger(tmp_path)
    _seed(ledger, operation_id="tool-candidate", message_ids=[121], created_at=95.0)
    service = _service(ledger, _Adapter())

    class _Executor:
        qq_recall_service = service
        operation_id = "current-turn"

        def __init__(self) -> None:
            self.pending_actions: list[dict] = []

        def queue_action(self, action: str, params: dict) -> None:
            self.pending_actions.append({"type": action, "params": params})

    executor = _Executor()
    tool = qq_recall.build_qq_recall_tool(
        executor=executor,
        bot=_Bot(),
        event=_event(),
        cutoff=100.0,
    )
    payload = json.loads(asyncio.run(tool.handler()))

    assert tool.parameters == {"type": "object", "properties": {}, "required": []}
    assert list(inspect.signature(tool.handler).parameters) == []
    assert payload == {
        "ok": True,
        "queued": True,
        "kind": "message_recall",
        "final_reply_instruction": "撤回动作已进入最终提交；不要再发送文字或 ACK。",
    }
    assert executor.pending_actions == [
        {"type": "recall_latest_qq_operation", "params": {}}
    ]
    with pytest.raises(TypeError):
        asyncio.run(tool.handler(message_id=121))


def test_action_executor_ignores_injected_recall_params() -> None:
    captured: dict = {}

    class _Service:
        async def recall_latest(self, **kwargs):  # noqa: ANN003, ANN202
            captured.update(kwargs)
            return qq_recall.QQRecallResult("succeeded", "ok", recalled_count=1)

    executor = action_executor_mod.ActionExecutor(
        _Bot(),
        _event(),
        SimpleNamespace(),
        _Logger(),
        qq_recall_service=_Service(),
        operation_id="current-turn",
        user_target="10001",
        recall_cutoff=100.0,
    )
    result = asyncio.run(
        executor.execute(
            "recall_latest_qq_operation",
            {"message_id": 999, "group_id": 888, "user_id": 777},
        )
    )

    assert result == "撤回结果：succeeded"
    assert captured["current_operation_id"] == "current-turn"
    assert captured["requester_user_id"] == "10001"
    assert "message_id" not in captured
    assert "group_id" not in captured
    assert executor.last_delivery_confirmed is False


def test_recall_pending_action_creates_no_visible_history_or_delivery_confirmation() -> None:
    class _Service:
        async def recall_latest(self, **_kwargs):  # noqa: ANN003, ANN202
            return qq_recall.QQRecallResult("succeeded", "ok", recalled_count=1)

    executor = action_executor_mod.ActionExecutor(
        _Bot(),
        _event(),
        SimpleNamespace(),
        _Logger(),
        qq_recall_service=_Service(),
        recall_cutoff=100.0,
    )
    actions = [{"type": "recall_latest_qq_operation", "params": {}}]
    state: dict = {}

    history = asyncio.run(reply_commit.execute_pending_actions(executor, actions, state=state))

    assert history == []
    assert actions == []
    assert state["reply_delivery_started"] is True
    assert "reply_delivery_confirmed" not in state


def test_admin_withdraw_alias_rejects_all_external_identifiers() -> None:
    assert persona_admin_commands.normalize_command_word("召回") == "recall"
    assert persona_admin_commands.normalize_command_word("撤回") == "withdraw"
    assert persona_admin_commands.normalize_command_word("withdraw") == "withdraw"

    class _Finished(RuntimeError):
        pass

    class _Matcher:
        async def finish(self, message):  # noqa: ANN001, ANN202
            raise _Finished(str(message))

    bundle = SimpleNamespace(
        superusers={"10001"},
        qq_outbound_ledger=object(),
        plugin_config=SimpleNamespace(),
        logger=_Logger(),
    )
    with pytest.raises(_Finished, match="不接受消息 ID"):
        asyncio.run(
            persona_admin_commands.dispatch_persona_admin_command(
                _Matcher(),
                bot=_Bot(),
                bundle=bundle,
                event=_event(user_id="10001"),
                arg_text="撤回 123456",
            )
        )


def test_admin_withdraw_uses_current_scope_and_rejects_non_admin(monkeypatch) -> None:  # noqa: ANN001
    captured: dict = {}

    class _Finished(RuntimeError):
        pass

    class _Matcher:
        async def finish(self, message):  # noqa: ANN001, ANN202
            raise _Finished(str(message))

    class _Service:
        def __init__(self, ledger, **kwargs):  # noqa: ANN001, ANN003
            captured["ledger"] = ledger
            captured["init"] = kwargs

        async def recall_latest(self, **kwargs):  # noqa: ANN003, ANN202
            captured["call"] = kwargs
            return qq_recall.QQRecallResult(
                "succeeded",
                "ok",
                total_count=2,
                recalled_count=2,
            )

    monkeypatch.setattr(qq_recall, "QQRecallService", _Service)
    ledger_marker = object()
    bundle = SimpleNamespace(
        superusers={"10001"},
        qq_outbound_ledger=ledger_marker,
        plugin_config=SimpleNamespace(),
        logger=_Logger(),
    )
    event = _event(user_id="10001")
    bot = _Bot()

    with pytest.raises(_Finished, match="已撤回.*2 条"):
        asyncio.run(
            persona_admin_commands.dispatch_persona_admin_command(
                _Matcher(),
                bot=bot,
                bundle=bundle,
                event=event,
                arg_text="撤回",
            )
        )
    assert captured["ledger"] is ledger_marker
    assert captured["call"]["bot"] is bot
    assert captured["call"]["event"] is event
    assert captured["call"]["actor_kind"] == "admin"
    assert set(captured["call"]) == {
        "bot",
        "event",
        "requester_user_id",
        "actor_kind",
        "cutoff",
    }

    ordinary_bundle = SimpleNamespace(
        superusers=set(),
        qq_outbound_ledger=ledger_marker,
        plugin_config=SimpleNamespace(),
        logger=_Logger(),
    )
    monkeypatch.setattr(
        persona_admin_commands,
        "can_manage_sensitive_action",
        lambda **_kwargs: False,
    )
    with pytest.raises(_Finished, match="权限不足"):
        asyncio.run(
            persona_admin_commands.dispatch_persona_admin_command(
                _Matcher(),
                bot=bot,
                bundle=ordinary_bundle,
                event=_event(user_id="10002"),
                arg_text="撤回",
            )
        )
