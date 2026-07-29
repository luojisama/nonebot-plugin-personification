from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
qq_outbound = load_personification_module("plugin.personification.core.qq_outbound")
pipeline_context = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.pipeline_context"
)
yaml_processor = load_personification_module(
    "plugin.personification.handlers.yaml_pipeline.processor"
)


class _Bot:
    self_id = "bot-42"

    def __init__(self) -> None:
        self.sends: list[tuple[object, object]] = []

    async def send(self, event: object, payload: object) -> dict[str, object]:
        self.sends.append((event, payload))
        return {"status": "ok", "data": {"message_id": f"message-{len(self.sends)}"}}


def _event() -> SimpleNamespace:
    return SimpleNamespace(group_id=100, user_id=200, message_id=300)


def _ledger(tmp_path):  # noqa: ANN001, ANN202
    db_path = db.init_db_sync(tmp_path)
    return qq_outbound.QQOutboundLedger(db_path), db_path


@pytest.mark.parametrize("surface", ["normal_reply", "yaml_reply", "reply_ack"])
def test_shared_reply_dispatch_records_surface_and_calls_bot_send(tmp_path, surface: str) -> None:  # noqa: ANN001
    ledger, _db_path = _ledger(tmp_path)
    bot = _Bot()
    event = _event()

    receipt = asyncio.run(
        pipeline_context.dispatch_reply_part(
            bot=bot,
            event=event,
            payload=f"payload-{surface}",
            ledger=ledger,
            surface=surface,
            reply_trace_id="trace-outbound",
        )
    )

    assert isinstance(receipt, qq_outbound.SendReceipt)
    assert receipt.operation_id == "trace-outbound"
    assert receipt.surface == surface
    assert receipt.bot_id == "bot-42"
    assert receipt.conversation_kind == "group"
    assert receipt.conversation_id == "100"
    assert receipt.user_target == "200"
    assert receipt.message_id == "message-1"
    assert bot.sends == [(event, f"payload-{surface}")]


def test_reply_parts_are_independent_and_fallback_operation_id_is_stable(tmp_path) -> None:  # noqa: ANN001
    ledger, _db_path = _ledger(tmp_path)
    bot = _Bot()
    event = _event()
    payloads = [
        "segment",
        {"type": "image", "data": "image-ref"},
        {"type": "sticker", "data": "sticker-ref"},
        "typo-correction",
    ]

    async def _dispatch_all():  # noqa: ANN202
        return [
            await pipeline_context.dispatch_reply_part(
                bot=bot,
                event=event,
                payload=payload,
                ledger=ledger,
                surface="normal_reply",
            )
            for payload in payloads
        ]

    receipts = asyncio.run(_dispatch_all())

    assert [receipt.operation_id for receipt in receipts] == ["qq-reply:bot-42:300"] * 4
    assert [receipt.part_index for receipt in receipts] == [0, 1, 2, 3]
    assert [receipt.message_id for receipt in receipts] == [
        "message-1",
        "message-2",
        "message-3",
        "message-4",
    ]
    assert [payload for _event_value, payload in bot.sends] == payloads


def test_reply_dispatch_without_ledger_preserves_raw_bot_send_result() -> None:
    bot = _Bot()
    event = _event()

    result = asyncio.run(
        pipeline_context.dispatch_reply_part(
            bot=bot,
            event=event,
            payload="legacy",
            ledger=None,
            surface="normal_reply",
            reply_trace_id="trace-legacy",
        )
    )

    assert result == {"status": "ok", "data": {"message_id": "message-1"}}
    assert bot.sends == [(event, "legacy")]


def test_reply_dispatch_reraises_send_exception_and_records_unknown(tmp_path) -> None:  # noqa: ANN001
    ledger, db_path = _ledger(tmp_path)
    event = _event()

    class SendFailure(RuntimeError):
        pass

    class FailingBot(_Bot):
        async def send(self, event: object, payload: object) -> dict[str, object]:
            raise SendFailure("send failed")

    with pytest.raises(SendFailure, match="send failed"):
        asyncio.run(
            pipeline_context.dispatch_reply_part(
                bot=FailingBot(),
                event=event,
                payload="failure",
                ledger=ledger,
                surface="yaml_reply",
                reply_trace_id="trace-failure",
            )
        )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, surface, user_target FROM qq_outbound_ledger WHERE operation_id=?",
            ("trace-failure",),
        ).fetchone()
    assert row == ("unknown", "yaml_reply", "200")


@pytest.mark.parametrize(
    ("ack_text", "expected_sent"),
    [
        ("ack-now", True),
        ("Authorization: Bearer secret-token-value", False),
    ],
)
def test_normal_agent_ack_uses_reply_ack_ledger_surface(
    tmp_path,
    monkeypatch,
    ack_text: str,
    expected_sent: bool,
) -> None:  # noqa: ANN001
    ledger, db_path = _ledger(tmp_path)
    bot = _Bot()
    event = _event()

    class _Registry:
        def register(self, _tool: object) -> None:
            return None

    async def _friend_ids(*_args, **_kwargs):  # noqa: ANN202
        return set()

    async def _run_agent(**kwargs):  # noqa: ANN202
        await kwargs["ack_sender"](ack_text)
        return SimpleNamespace(
            text="reply",
            bypass_length_limits=False,
            pending_actions=[],
            failure_code="",
            suppress_reply_recovery=False,
            direct_output=False,
            quality_context="",
        )

    monkeypatch.setattr(pipeline_context, "clone_tool_registry", lambda _registry: _Registry())
    monkeypatch.setattr(pipeline_context, "register_current_user_avatar_tool", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pipeline_context,
        "register_group_user_avatar_pair_insight_tool",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(pipeline_context, "register_send_qq_expression_tools", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_context, "build_send_image_tools", lambda *_a, **_k: [])
    monkeypatch.setattr(pipeline_context, "get_cached_friend_ids", _friend_ids)
    monkeypatch.setattr(pipeline_context, "build_group_info_tool_for_runtime", lambda **_k: object())
    monkeypatch.setattr(pipeline_context, "build_friend_request_tool_for_runtime", lambda **_k: object())
    monkeypatch.setattr(pipeline_context, "pick_ack_phrase", lambda *_a, **_k: "ack-default")
    monkeypatch.setattr(pipeline_context, "run_agent", _run_agent)

    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_agent_enabled=True,
            personification_agent_max_steps=1,
            personification_evidence_synthesizer_enabled=False,
            personification_memory_palace_enabled=False,
            personification_plugin_invoker_enabled=False,
            personification_sticker_path=str(tmp_path / "missing-stickers"),
        ),
        tool_registry=object(),
        agent_tool_caller=object(),
        profile_service=None,
        vision_caller=None,
        knowledge_store=None,
        background_intelligence=None,
        logger=SimpleNamespace(debug=lambda *_a, **_k: None),
        get_whitelisted_groups=lambda: [],
        qq_outbound_ledger=ledger,
        user_policy_gate=None,
    )
    commit_state = {"reply_trace_id": "trace-ack"}

    asyncio.run(
        pipeline_context.run_agent_if_enabled(
            bot=bot,
            event=event,
            messages=[],
            persona=SimpleNamespace(
                get_group_config=lambda _group_id: {},
                get_user_data=lambda _user_id: {},
            ),
            runtime=runtime,
            is_direct_mention=True,
            reply_commit_state=commit_state,
        )
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT operation_id, part_index, surface, status, message_id, user_target "
            "FROM qq_outbound_ledger WHERE operation_id=?",
            ("trace-ack",),
        ).fetchone()
    if expected_sent:
        assert row == ("trace-ack", 0, "reply_ack", "sent", "message-1", "200")
        assert bot.sends == [(event, "ack-now")]
        assert commit_state["reply_delivery_confirmed"] is True
    else:
        assert row is None
        assert bot.sends == []
        assert "reply_delivery_started" not in commit_state


def test_yaml_translation_forward_records_one_ledger_receipt(tmp_path) -> None:  # noqa: ANN001
    ledger, _db_path = _ledger(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    class _ForwardBot:
        self_id = "bot-42"

        async def call_api(self, api: str, **kwargs):  # noqa: ANN003, ANN202
            calls.append((api, kwargs))
            return {"data": {"message_id": "forward-message-1"}}

    receipt = asyncio.run(
        yaml_processor._send_translation_forward(
            _ForwardBot(),
            _event(),
            "第一条翻译\n\n第二条翻译",
            qq_outbound_ledger=ledger,
            operation_id="trace-forward",
            user_target="200",
        )
    )

    assert isinstance(receipt, qq_outbound.SendReceipt)
    assert receipt.status == "sent"
    assert receipt.message_id == "forward-message-1"
    assert receipt.surface == "reply_translation_forward"
    assert receipt.operation_id == "trace-forward"
    assert calls[0][0] == "send_group_forward_msg"
