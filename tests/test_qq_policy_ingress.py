from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


processor = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.processor"
)
record_handler = load_personification_module(
    "plugin.personification.handlers.record_message_handler"
)


class _Gate:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0

    async def allows_current(self, _event) -> bool:  # noqa: ANN001
        self.calls += 1
        return self.allowed


def test_reply_processor_sends_dedicated_closure_without_entering_lifecycle() -> None:
    gate = _Gate(True)
    sent: list[str] = []

    class _Bot:
        async def send(self, _event, payload) -> None:  # noqa: ANN001
            sent.append(str(payload))

    deps = SimpleNamespace(
        runtime=SimpleNamespace(
            user_policy_gate=gate,
            plugin_config=SimpleNamespace(personification_turn_trace_enabled=False),
            qq_outbound_ledger=None,
        )
    )
    state = {
        "user_policy_decision": {
            "disposition": "direct_closure",
            "reason_code": "classifier_unavailable",
        }
    }

    asyncio.run(processor.process_response_logic(_Bot(), object(), state, deps))

    assert gate.calls == 1
    assert sent == ["我这边暂时没判断好，这条先不接了。你可以稍后再发一次。"]
    assert "_reply_lifecycle_started_at" not in state


def test_reply_processor_rechecks_active_blacklist_before_lifecycle() -> None:
    gate = _Gate(False)
    deps = SimpleNamespace(runtime=SimpleNamespace(user_policy_gate=gate))
    state: dict = {}

    asyncio.run(processor.process_response_logic(None, object(), state, deps))

    assert gate.calls == 1
    assert "_reply_lifecycle_started_at" not in state


def test_record_handler_rechecks_policy_before_any_write() -> None:
    gate = _Gate(False)
    touched: list[str] = []

    asyncio.run(
        record_handler.handle_record_message_event(
            SimpleNamespace(user_id="10001"),
            resolve_record_message=lambda *_args, **_kwargs: touched.append("record") or ("", False),
            record_group_msg=lambda *_args, **_kwargs: touched.append("history"),
            logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
            create_background_task=lambda _group_id: touched.append("style"),
            create_summary_task=lambda _group_id: touched.append("summary"),
            create_scoped_profile_task=lambda _group_id, _user_id: touched.append("scoped"),
            user_policy_gate=gate,
        )
    )

    assert touched == []
    assert gate.calls == 1


def test_record_handler_schedules_scoped_profile_after_allowed_group_write() -> None:
    gate = _Gate(True)
    touched: list[object] = []

    asyncio.run(
        record_handler.handle_record_message_event(
            SimpleNamespace(user_id="10001"),
            resolve_record_message=lambda *_args, **_kwargs: ("20001", False),
            record_group_msg=lambda *_args, **_kwargs: None,
            logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
            create_background_task=lambda _group_id: touched.append("style"),
            create_summary_task=lambda group_id: touched.append(("summary", group_id)),
            create_scoped_profile_task=lambda group_id, user_id: touched.append(
                ("scoped", group_id, user_id)
            ),
            user_policy_gate=gate,
        )
    )

    assert touched == [
        ("summary", "20001"),
        ("scoped", "20001", "10001"),
    ]
