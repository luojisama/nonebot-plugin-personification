from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ._loader import load_personification_module


reply_commit = load_personification_module("plugin.personification.handlers.reply_commit")


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, action: str, params: dict[str, Any]) -> None:
        self.calls.append((action, params))


def test_commit_gate_is_reentrant_for_same_turn_and_released() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        state: dict[str, Any] = {"reply_commit_lock": lock}

        await reply_commit.acquire_reply_commit(state)
        await reply_commit.acquire_reply_commit(state)

        assert lock.locked() is True
        reply_commit.release_reply_commit(state)
        assert lock.locked() is False

    asyncio.run(run())


def test_reply_lifecycle_snapshot_tracks_post_send_phase() -> None:
    state: dict[str, Any] = {}

    reply_commit.begin_reply_lifecycle(state)
    reply_commit.mark_reply_phase(state, "post_send_bookkeeping")
    snapshot = reply_commit.reply_lifecycle_snapshot(state)

    assert snapshot["last_phase"] == "post_send_bookkeeping"
    assert snapshot["elapsed_ms"] >= 0
    assert snapshot["phase_age_ms"] >= 0


def test_reply_paths_commit_ordered_history_before_releasing_delivery_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "processor.py").read_text(encoding="utf-8")
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")

    normal_delivery = normal.index("mark_reply_phase(state, \"delivery_history_commit\")")
    normal_history = normal.index("session.append_session_message(", normal_delivery)
    normal_release = normal.index("release_reply_commit(state)", normal_history)
    normal_emotion = normal.index("await persist_reply_emotion_state(", normal_release)
    assert normal_delivery < normal_history < normal_release < normal_emotion

    yaml_delivery = yaml.index("mark_reply_phase(reply_commit_state, \"delivery_history_commit\")")
    yaml_history = yaml.index("append_session_message(", yaml_delivery)
    yaml_release = yaml.index("release_reply_commit(reply_commit_state)", yaml_history)
    yaml_emotion = yaml.index("await update_emotion_state_after_turn(", yaml_release)
    assert yaml_delivery < yaml_history < yaml_release < yaml_emotion


def test_pending_action_paths_mark_delivery_phase_before_execution() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "processor.py").read_text(encoding="utf-8")
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")

    normal_action = normal.index("async def _commit_pending_actions()")
    normal_phase = normal.index("mark_reply_phase(state, \"delivery\")", normal_action)
    normal_execute = normal.index("await execute_pending_actions(", normal_action)
    assert normal_action < normal_phase < normal_execute

    yaml_action = yaml.index("async def _commit_pending_actions()")
    yaml_phase = yaml.index("mark_reply_phase(reply_commit_state, \"delivery\")", yaml_action)
    yaml_execute = yaml.index("await execute_pending_actions(", yaml_action)
    assert yaml_action < yaml_phase < yaml_execute


def test_ack_paths_mark_delivery_and_restore_agent_phase() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "pipeline_context.py").read_text(encoding="utf-8")
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")

    normal_ack = normal.index("async def _ack_sender(")
    assert normal.index('mark_reply_phase(commit_state, "delivery")', normal_ack) < normal.index(
        "await bot.send(", normal_ack
    )
    assert normal.index('mark_reply_phase(commit_state, "agent_after_ack")', normal_ack) > normal.index(
        "release_reply_commit(commit_state)", normal_ack
    )

    yaml_ack = yaml.index("async def _ack_sender(")
    assert yaml.index('mark_reply_phase(reply_commit_state, "delivery")', yaml_ack) < yaml.index(
        "await bot.send(", yaml_ack
    )
    assert yaml.index('mark_reply_phase(reply_commit_state, "yaml_agent_after_ack")', yaml_ack) > yaml.index(
        "release_reply_commit(reply_commit_state)", yaml_ack
    )


def test_pending_actions_execute_once_and_are_consumed(monkeypatch) -> None:  # noqa: ANN001
    async def run() -> None:
        executor = _Executor()
        actions = [{"type": "send_sticker", "params": {"path": "x.png"}}]
        expression_tools = load_personification_module("plugin.personification.core.qq_expression_tools")
        monkeypatch.setattr(expression_tools, "qq_action_history_text", lambda _action: "[发送了表情包]")

        history = await reply_commit.execute_pending_actions(executor, actions)
        second = await reply_commit.execute_pending_actions(executor, actions)

        assert executor.calls == [("send_sticker", {"path": "x.png"})]
        assert history == ["[发送了表情包]"]
        assert second == []
        assert actions == []

    asyncio.run(run())


def test_pending_actions_record_confirmed_delivery() -> None:
    async def run() -> None:
        executor = _Executor()
        actions = [{"type": "send", "params": {"text": "hello"}}]
        state: dict[str, Any] = {}

        await reply_commit.execute_pending_actions(executor, actions, state=state)

        assert state["reply_delivery_started"] is True
        assert state["reply_delivery_confirmed"] is True

    asyncio.run(run())


def test_pending_action_without_external_send_is_not_confirmed() -> None:
    class _FailedExecutor:
        last_delivery_confirmed = False

        async def execute(self, _action: str, _params: dict[str, Any]) -> str:
            return "发送失败"

    async def run() -> None:
        actions = [{"type": "send_image_url", "params": {"url": ""}}]
        state: dict[str, Any] = {}

        await reply_commit.execute_pending_actions(_FailedExecutor(), actions, state=state)

        assert state["reply_delivery_started"] is True
        assert "reply_delivery_confirmed" not in state

    asyncio.run(run())


def test_unknown_pending_action_stops_following_actions_and_history(monkeypatch) -> None:  # noqa: ANN001
    class _UnknownExecutor:
        last_delivery_confirmed = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, action: str, _params: dict[str, Any]) -> str:
            self.calls.append(action)
            self.last_delivery_confirmed = False
            return "发送结果未知"

    async def run() -> None:
        executor = _UnknownExecutor()
        actions = [
            {"type": "send_image_url", "params": {"url": "first"}},
            {"type": "send_sticker", "params": {"path": "second"}},
        ]
        expression_tools = load_personification_module(
            "plugin.personification.core.qq_expression_tools"
        )
        monkeypatch.setattr(expression_tools, "qq_action_history_text", lambda _action: "sent")

        history = await reply_commit.execute_pending_actions(executor, actions, state={})

        assert executor.calls == ["send_image_url"]
        assert history == []
        assert actions == []

    asyncio.run(run())
