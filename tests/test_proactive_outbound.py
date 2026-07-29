from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
qq_outbound = load_personification_module("plugin.personification.core.qq_outbound")
proactive_flow = load_personification_module("plugin.personification.flows.proactive_flow")


def _logger() -> SimpleNamespace:
    return SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )


def _ledger(tmp_path: Path):  # noqa: ANN202
    return qq_outbound.QQOutboundLedger(db.init_db_sync(tmp_path))


def _private_config() -> SimpleNamespace:
    return SimpleNamespace(
        personification_agent_enabled=False,
        personification_persona_snippet_max_chars=150,
        personification_proactive_daily_limit=3,
        personification_proactive_enabled=True,
        personification_proactive_idle_hours=24.0,
        personification_proactive_interval=30,
        personification_proactive_probability=1.0,
        personification_proactive_require_user_profile=False,
        personification_proactive_unsuitable_prob=0.0,
        personification_proactive_without_signin=True,
        personification_timezone="Asia/Shanghai",
    )


def _group_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        personification_agent_enabled=False,
        personification_group_idle_check_interval=15,
        personification_group_idle_daily_limit=3,
        personification_group_idle_minutes=60,
        personification_group_idle_mode_decision_prob=1.0,
        personification_group_quiet_hour_end=7,
        personification_group_quiet_hour_start=0,
        personification_proactive_probability=1.0,
        personification_sticker_path=str(tmp_path),
        personification_timezone="Asia/Shanghai",
    )


def _patch_common(monkeypatch) -> None:  # noqa: ANN001
    diagnostics = load_personification_module(
        "plugin.personification.core.proactive_diagnostics"
    )
    schedule = load_personification_module("plugin.personification.schedule")
    monkeypatch.setattr(diagnostics, "record", lambda **_kwargs: None)
    monkeypatch.setattr(schedule, "is_group_active_hour", lambda *_args: True)
    monkeypatch.setattr(proactive_flow, "append_session_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(proactive_flow, "get_group_topic_summary", lambda _group_id: "")
    monkeypatch.setattr(proactive_flow.random, "random", lambda: 0.0)
    monkeypatch.setattr(proactive_flow.random, "uniform", lambda *_args: 0.0)


def _patch_sticker(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    sticker_library = load_personification_module(
        "plugin.personification.core.sticker_library"
    )
    sticker_feedback = load_personification_module(
        "plugin.personification.core.sticker_feedback"
    )
    sticker_path = tmp_path / "idle.png"
    sticker_path.write_bytes(b"test-sticker")

    async def _load_feedback():  # noqa: ANN202
        return {}

    async def _record_sent(_name: str) -> None:
        return None

    monkeypatch.setattr(sticker_library, "resolve_sticker_dir", lambda _path: tmp_path)
    monkeypatch.setattr(
        sticker_library,
        "list_local_sticker_files",
        lambda _path, include_gif=True: [sticker_path],
    )
    monkeypatch.setattr(
        sticker_library,
        "load_sticker_metadata",
        lambda _path: {"idle.png": {"description": "idle", "weight": 1.0}},
    )
    monkeypatch.setattr(sticker_feedback, "load_sticker_feedback", _load_feedback)
    monkeypatch.setattr(
        sticker_feedback,
        "get_sticker_decay_multiplier",
        lambda _name, _state: 1.0,
    )
    monkeypatch.setattr(sticker_feedback, "record_sticker_sent", _record_sent)


def test_private_proactive_send_records_ledger_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_common(monkeypatch)
    ledger = _ledger(tmp_path)
    now = datetime(2026, 7, 18, 10, 0, 0)
    proactive_state = {
        "10001": {
            "last_interaction": now.timestamp() - 48 * 60 * 60,
        }
    }
    sent: list[dict] = []

    class Bot:
        self_id = "90001"

        async def get_friend_list(self):  # noqa: ANN202
            return [{"user_id": 10001, "nickname": "friend"}]

        async def send_private_msg(self, **kwargs):  # noqa: ANN003, ANN202
            sent.append(kwargs)
            return {"status": "ok", "data": {"message_id": "private-message-1"}}

    async def _call_ai(_messages, **_kwargs):  # noqa: ANN001, ANN202
        return "SEND|10001|suddenly remembered that episode"

    result = asyncio.run(
        proactive_flow.run_proactive_messaging(
            plugin_config=_private_config(),
            sign_in_available=False,
            is_rest_time=lambda **_kwargs: True,
            get_bots=lambda: {"90001": Bot()},
            load_data=lambda: {},
            load_proactive_state=lambda: proactive_state,
            save_proactive_state=lambda state: proactive_state.update(state),
            get_user_data=lambda _user_id: {},
            get_level_name=lambda _value: "",
            get_now=lambda: now,
            get_activity_status=lambda: "active",
            load_prompt=lambda: "persona",
            call_ai_api=_call_ai,
            parse_yaml_response=lambda _text: None,
            logger=_logger(),
            qq_outbound_ledger=ledger,
        )
    )

    with sqlite3.connect(ledger.db_path) as conn:
        row = conn.execute(
            """
            SELECT operation_id, part_index, bot_id, conversation_kind,
                   conversation_id, user_target, surface, status, message_id
            FROM qq_outbound_ledger
            """
        ).fetchone()

    assert result is True
    assert sent == [{"user_id": 10001, "message": "suddenly remembered that episode"}]
    assert row is not None
    assert row[0].startswith("qq:")
    assert row[1:] == (
        0,
        "90001",
        "private",
        "10001",
        "10001",
        "proactive_private",
        "sent",
        "private-message-1",
    )
    assert proactive_state["10001"]["count"] == 1
    assert proactive_state["10001"]["proactive_history"][-1]["message"] == (
        "suddenly remembered that episode"
    )


def test_private_proactive_missing_message_id_does_not_commit_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_common(monkeypatch)
    ledger = _ledger(tmp_path)
    now = datetime(2026, 7, 18, 10, 0, 0)
    proactive_state = {
        "10001": {"last_interaction": now.timestamp() - 48 * 60 * 60}
    }

    class Bot:
        self_id = "90001"

        async def get_friend_list(self):  # noqa: ANN202
            return [{"user_id": 10001, "nickname": "friend"}]

        async def send_private_msg(self, **_kwargs):  # noqa: ANN003, ANN202
            return {"status": "ok"}

    async def _call_ai(_messages, **_kwargs):  # noqa: ANN001, ANN202
        return "SEND|10001|unknown delivery"

    result = asyncio.run(
        proactive_flow.run_proactive_messaging(
            plugin_config=_private_config(),
            sign_in_available=False,
            is_rest_time=lambda **_kwargs: True,
            get_bots=lambda: {"90001": Bot()},
            load_data=lambda: {},
            load_proactive_state=lambda: proactive_state,
            save_proactive_state=lambda state: proactive_state.update(state),
            get_user_data=lambda _user_id: {},
            get_level_name=lambda _value: "",
            get_now=lambda: now,
            get_activity_status=lambda: "active",
            load_prompt=lambda: "persona",
            call_ai_api=_call_ai,
            parse_yaml_response=lambda _text: None,
            logger=_logger(),
            qq_outbound_ledger=ledger,
        )
    )

    assert result is False
    assert proactive_state["10001"]["count"] == 0
    assert proactive_state["10001"]["last_proactive_at"] == 0
    assert proactive_state["10001"].get("proactive_history", []) == []


def test_private_proactive_agent_send_envelope_is_structured(monkeypatch) -> None:  # noqa: ANN001
    _patch_common(monkeypatch)
    config = _private_config()
    config.personification_agent_enabled = True
    now = datetime(2026, 7, 18, 10, 0, 0)
    proactive_state = {
        "10001": {"last_interaction": now.timestamp() - 48 * 60 * 60}
    }
    captured: dict = {}

    class Bot:
        async def get_friend_list(self):  # noqa: ANN202
            return [{"user_id": 10001, "nickname": "friend"}]

    async def _agent(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return "SKIP|现在没什么想说的"

    async def _direct(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Agent path must not use the direct Provider fallback")

    monkeypatch.setattr(proactive_flow, "run_text_agent", _agent)
    result = asyncio.run(
        proactive_flow.run_proactive_messaging(
            plugin_config=config,
            sign_in_available=False,
            is_rest_time=lambda **_kwargs: True,
            get_bots=lambda: {"90001": Bot()},
            load_data=lambda: {},
            load_proactive_state=lambda: proactive_state,
            save_proactive_state=lambda state: proactive_state.update(state),
            get_user_data=lambda _user_id: {},
            get_level_name=lambda _value: "",
            get_now=lambda: now,
            get_activity_status=lambda: "active",
            load_prompt=lambda: "persona",
            call_ai_api=_direct,
            parse_yaml_response=lambda _text: None,
            logger=_logger(),
            agent_tool_caller=object(),
            agent_tool_registry=object(),
        )
    )

    assert result is False
    assert captured["trigger_reason"] == "proactive_private"
    assert captured["structured_output"] is True


def _run_group_idle(
    *,
    config: SimpleNamespace,
    ledger,
    bot,
    responses: list[str],
    groups: list[str],
    proactive_state: dict,
    records: list[tuple],
    agent_tool_caller=None,  # noqa: ANN001
    agent_tool_registry=None,  # noqa: ANN001
) -> int:
    now = datetime(2026, 7, 18, 10, 0, 0)
    response_iter = iter(responses)

    async def _call_ai(_messages, **_kwargs):  # noqa: ANN001, ANN202
        return next(response_iter)

    return asyncio.run(
        proactive_flow.run_group_idle_topic(
            plugin_config=config,
            get_bots=lambda: {str(bot.self_id): bot},
            get_whitelisted_groups=lambda: groups,
            get_recent_group_msgs=lambda _group_id, limit: [],
            get_group_style=lambda _group_id: "casual",
            load_proactive_state=lambda: proactive_state,
            save_proactive_state=lambda state: proactive_state.update(state),
            load_prompt=lambda _group_id: "persona",
            call_ai_api=_call_ai,
            get_now=lambda: now,
            record_group_msg=lambda *args, **kwargs: records.append((*args, kwargs)),
            logger=_logger(),
            agent_tool_caller=agent_tool_caller,
            agent_tool_registry=agent_tool_registry,
            qq_outbound_ledger=ledger,
        )
    )


def test_group_idle_combo_uses_one_operation_with_two_parts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_common(monkeypatch)
    _patch_sticker(monkeypatch, tmp_path)
    proactive_flow._last_group_idle_sent_at = 0.0
    ledger = _ledger(tmp_path)
    sent: list[dict] = []
    state: dict = {}
    records: list[tuple] = []

    class Bot:
        self_id = "90001"

        async def get_group_info(self, **_kwargs):  # noqa: ANN202
            return {"group_name": "test group"}

        async def get_group_member_info(self, **_kwargs):  # noqa: ANN202
            return {"card": "bot card"}

        async def send_group_msg(self, **kwargs):  # noqa: ANN003, ANN202
            sent.append(kwargs)
            return {"message_id": f"group-message-{len(sent)}"}

    result = _run_group_idle(
        config=_group_config(tmp_path),
        ledger=ledger,
        bot=Bot(),
        responses=["a new topic", '{"mode":"combo","mood":"excited"}'],
        groups=["20001"],
        proactive_state=state,
        records=records,
    )

    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            """
            SELECT operation_id, part_index, surface, status, message_id, preview
            FROM qq_outbound_ledger
            ORDER BY part_index
            """
        ).fetchall()

    assert result == 1
    assert len(sent) == 2
    assert len(rows) == 2
    assert rows[0][0] == rows[1][0]
    assert rows[0][0].startswith("qq:")
    assert [row[1] for row in rows] == [0, 1]
    assert [row[2] for row in rows] == ["proactive_group_idle"] * 2
    assert [row[3] for row in rows] == ["sent", "sent"]
    assert [row[4] for row in rows] == ["group-message-1", "group-message-2"]
    assert rows[0][5] == "a new topic"
    assert "CQ:image" in rows[1][5]
    assert state["group_idle_20001"]["count"] == 1
    assert records[0][2] == "a new topic"


def test_group_idle_send_exception_stays_unknown_without_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_common(monkeypatch)
    _patch_sticker(monkeypatch, tmp_path)
    proactive_flow._last_group_idle_sent_at = 0.0
    ledger = _ledger(tmp_path)
    send_calls: list[str] = []
    state: dict = {}
    records: list[tuple] = []

    class Bot:
        self_id = "90001"

        async def get_group_info(self, *, group_id):  # noqa: ANN001, ANN202
            return {"group_name": f"group-{group_id}"}

        async def send_group_msg(self, *, group_id, message):  # noqa: ANN001, ANN202
            _ = message
            send_calls.append(str(group_id))
            raise RuntimeError("outcome is unknown")

    result = _run_group_idle(
        config=_group_config(tmp_path),
        ledger=ledger,
        bot=Bot(),
        responses=["first topic", '{"mode":"sticker","mood":"sleepy"}'],
        groups=["20001", "20002"],
        proactive_state=state,
        records=records,
    )

    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            """
            SELECT part_index, conversation_id, surface, status, message_id, error_code
            FROM qq_outbound_ledger
            """
        ).fetchall()

    assert result == 0
    assert send_calls == ["20001"]
    assert rows == [
        (
            0,
            "20001",
            "proactive_group_idle",
            "unknown",
            None,
            "RuntimeError",
        )
    ]
    assert "group_idle_20001" not in state
    assert "group_idle_20002" not in state
    assert records == []


def test_group_idle_agent_keeps_visible_topic_quality_and_structures_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_common(monkeypatch)
    proactive_flow._last_group_idle_sent_at = 0.0
    config = _group_config(tmp_path)
    config.personification_agent_enabled = True
    calls: list[dict] = []
    sent: list[str] = []

    class Bot:
        self_id = "90001"

        async def get_group_info(self, **_kwargs):  # noqa: ANN202
            return {"group_name": "test group"}

        async def send_group_msg(self, *, group_id, message):  # noqa: ANN001, ANN202
            sent.append(str(message))
            return {"message_id": f"group-{group_id}-1"}

    async def _agent(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if kwargs["trigger_reason"] == "proactive_group_idle_mode":
            return '{"mode":"text","mood":"日常"}'
        return "突然有点想吃冰淇淋"

    monkeypatch.setattr(proactive_flow, "run_text_agent", _agent)
    result = _run_group_idle(
        config=config,
        ledger=None,
        bot=Bot(),
        responses=[],
        groups=["20001"],
        proactive_state={},
        records=[],
        agent_tool_caller=object(),
        agent_tool_registry=object(),
    )

    assert result == 1
    assert sent == ["突然有点想吃冰淇淋"]
    kwargs_by_trigger = {str(item["trigger_reason"]): item for item in calls}
    assert kwargs_by_trigger["proactive_group_idle"]["structured_output"] is False
    assert kwargs_by_trigger["proactive_group_idle_mode"]["structured_output"] is True


def test_checker_builders_forward_optional_ledger(monkeypatch) -> None:  # noqa: ANN001
    marker = object()
    private_kwargs: dict = {}
    group_kwargs: dict = {}

    async def _run_private(**kwargs):  # noqa: ANN003, ANN202
        private_kwargs.update(kwargs)
        return True

    async def _run_group(**kwargs):  # noqa: ANN003, ANN202
        group_kwargs.update(kwargs)
        return 1

    monkeypatch.setattr(proactive_flow, "run_proactive_messaging", _run_private)
    monkeypatch.setattr(proactive_flow, "run_group_idle_topic", _run_group)

    private_checker = proactive_flow.build_proactive_checker(
        plugin_config=object(),
        sign_in_available=False,
        is_rest_time=lambda: True,
        get_bots=lambda: {},
        load_data=lambda: {},
        load_proactive_state=lambda: {},
        save_proactive_state=lambda _state: None,
        get_user_data=lambda _user_id: {},
        get_level_name=lambda _value: "",
        get_now=datetime.now,
        get_activity_status=lambda: "active",
        load_prompt=lambda: "",
        call_ai_api=lambda _messages: None,
        parse_yaml_response=lambda _text: None,
        logger=_logger(),
        qq_outbound_ledger=marker,
    )
    group_checker = proactive_flow.build_group_idle_checker(
        plugin_config=object(),
        get_bots=lambda: {},
        get_whitelisted_groups=lambda: [],
        get_recent_group_msgs=lambda _group_id, _limit: [],
        get_group_style=lambda _group_id: "",
        load_proactive_state=lambda: {},
        save_proactive_state=lambda _state: None,
        load_prompt=lambda _group_id: "",
        call_ai_api=lambda _messages: None,
        get_now=datetime.now,
        record_group_msg=lambda *_args, **_kwargs: 0,
        logger=_logger(),
        qq_outbound_ledger=marker,
    )

    assert asyncio.run(private_checker()) is True
    assert asyncio.run(group_checker()) == 1
    assert private_kwargs["qq_outbound_ledger"] is marker
    assert group_kwargs["qq_outbound_ledger"] is marker
