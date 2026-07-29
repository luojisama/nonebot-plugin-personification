from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ._loader import load_personification_module


reply_buffer = load_personification_module("plugin.personification.handlers.reply_buffer")


@dataclass
class _TextSeg:
    data: dict[str, str]
    type: str = "text"


@dataclass
class _AtSeg:
    data: dict[str, str]
    type: str = "at"


class _Message(list):
    pass


class _MessageSegment:
    @staticmethod
    def text(value: str) -> _TextSeg:
        return _TextSeg({"text": value})


class _Sender:
    card = ""
    nickname = "tester"


class _PrivateEvent:
    def __init__(self, message_id: int, text: str) -> None:
        self.message_id = message_id
        self.user_id = 123
        self.sender = _Sender()
        self.message = _Message([_TextSeg({"text": text})])


class _GroupEvent(_PrivateEvent):
    group_id = 456


class _MentionEvent(_GroupEvent):
    def __init__(self, message_id: int, text: str) -> None:
        super().__init__(message_id, text)
        self.message.insert(0, _AtSeg({"qq": "999"}))


class _Bot:
    self_id = "999"


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def error(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.errors.append(str(message))


class _PolicyGate:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls: list[int] = []

    async def allows_current(self, event: Any) -> bool:
        self.calls.append(int(event.message_id))
        return self.allowed


def test_private_message_preempts_processing_batch() -> None:
    asyncio.run(_run_private_message_preempts_processing_batch())


async def _run_private_message_preempts_processing_batch() -> None:
    msg_buffer: dict[str, dict[str, Any]] = {}
    tasks: list[asyncio.Task[Any]] = []
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    second_processed = asyncio.Event()
    processed_ids: list[int] = []

    async def process_response_logic(_bot: Any, event: Any, _state: dict[str, Any]) -> None:
        processed_ids.append(int(event.message_id))
        if int(event.message_id) == 1:
            first_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
        if int(event.message_id) == 2:
            second_processed.set()

    def start_buffer_timer(key: str, bot: Any, wait_seconds: float) -> asyncio.Task[Any]:
        task = asyncio.create_task(
            reply_buffer.run_buffer_timer(
                key,
                bot,
                msg_buffer=msg_buffer,
                process_response_logic=process_response_logic,
                message_event_cls=_PrivateEvent,
                message_cls=_Message,
                message_segment_cls=_MessageSegment,
                logger=_Logger(),
                delay=wait_seconds,
                response_timeout_seconds=30,
            )
        )
        tasks.append(task)
        return task

    try:
        await reply_buffer.handle_reply_event(
            _Bot(),
            _PrivateEvent(1, "first"),
            {},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=start_buffer_timer,
            logger=_Logger(),
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)

        await reply_buffer.handle_reply_event(
            _Bot(),
            _PrivateEvent(2, "second"),
            {},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=start_buffer_timer,
            logger=_Logger(),
        )

        await asyncio.wait_for(first_cancelled.wait(), timeout=1)
        await asyncio.wait_for(second_processed.wait(), timeout=1)

        assert processed_ids == [1, 2]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_direct_turns_run_concurrently_and_commit_without_interleaving() -> None:
    asyncio.run(_run_direct_turns_run_concurrently_and_commit_without_interleaving())


async def _run_direct_turns_run_concurrently_and_commit_without_interleaving() -> None:
    controller = reply_buffer.ReplyConcurrencyController(session_limit=3, global_limit=3)
    msg_buffer: dict[str, dict[str, Any]] = {}
    release_generation = asyncio.Event()
    all_started = asyncio.Event()
    active = 0
    max_active = 0
    commit_order: list[int] = []

    async def process_response_logic(_bot: Any, event: Any, state: dict[str, Any]) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 3:
            all_started.set()
        await release_generation.wait()
        active -= 1
        lock = state["reply_commit_lock"]
        async with lock:
            commit_order.append(int(event.message_id))
            await asyncio.sleep(0.01)

    async def dispatch(message_id: int) -> None:
        await reply_buffer.handle_reply_event(
            _Bot(),
            _PrivateEvent(message_id, f"message-{message_id}"),
            {},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=lambda *_args: None,
            logger=_Logger(),
            concurrency_controller=controller,
            response_timeout_seconds=30,
        )

    tasks = [asyncio.create_task(dispatch(index)) for index in range(1, 4)]
    await asyncio.wait_for(all_started.wait(), timeout=1)
    release_generation.set()
    await asyncio.gather(*tasks)

    assert max_active == 3
    assert sorted(commit_order) == [1, 2, 3]


def test_direct_turn_concurrency_limit_queues_without_dropping() -> None:
    asyncio.run(_run_direct_turn_concurrency_limit_queues_without_dropping())


async def _run_direct_turn_concurrency_limit_queues_without_dropping() -> None:
    controller = reply_buffer.ReplyConcurrencyController(session_limit=2, global_limit=2)
    active = 0
    max_active = 0
    processed: list[int] = []

    async def process_response_logic(_bot: Any, event: Any, _state: dict[str, Any]) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        processed.append(int(event.message_id))
        active -= 1

    async def dispatch(message_id: int) -> None:
        await reply_buffer.handle_reply_event(
            _Bot(),
            _PrivateEvent(message_id, f"message-{message_id}"),
            {},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer={},
            start_buffer_timer=lambda *_args: None,
            logger=_Logger(),
            concurrency_controller=controller,
            response_timeout_seconds=30,
        )

    await asyncio.gather(*(dispatch(index) for index in range(1, 6)))

    assert max_active == 2
    assert sorted(processed) == [1, 2, 3, 4, 5]


def test_session_key_isolated_by_bot_id() -> None:
    event = _GroupEvent(1, "hello")

    first = reply_buffer._session_key(
        event,
        group_message_event_cls=_GroupEvent,
        bot_self_id="10001",
    )
    second = reply_buffer._session_key(
        event,
        group_message_event_cls=_GroupEvent,
        bot_self_id="10002",
    )

    assert first == "10001:456"
    assert second == "10002:456"
    assert first != second


def test_batched_failure_stays_silent_without_replaying_delivery() -> None:
    async def run() -> None:
        msg_buffer: dict[str, dict[str, Any]] = {}
        logger = _Logger()
        calls = 0

        async def process_response_logic(_bot: Any, _event: Any, state: dict[str, Any]) -> None:
            nonlocal calls
            calls += 1
            state["reply_delivery_started"] = True
            raise RuntimeError("https://private.example/?api_key=top-secret")

        event = _GroupEvent(1, "hello")
        await reply_buffer.handle_reply_event(
            _Bot(),
            event,
            {},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=lambda *_args: None,
            logger=logger,
        )
        key = reply_buffer._session_key(event, group_message_event_cls=_GroupEvent, bot_self_id="999")
        await reply_buffer.run_buffer_timer(
            key,
            _Bot(),
            msg_buffer=msg_buffer,
            process_response_logic=process_response_logic,
            message_event_cls=_PrivateEvent,
            message_cls=_Message,
            message_segment_cls=_MessageSegment,
            logger=logger,
            delay=0,
            response_timeout_seconds=30,
        )

        assert calls == 1
        assert logger.errors
        assert "delivery_state=dispatching" in logger.errors[-1]
        assert "top-secret" not in logger.errors[-1]
        assert "private.example" not in logger.errors[-1]

    asyncio.run(run())


def test_group_mentions_each_start_an_independent_direct_turn() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController(session_limit=3, global_limit=3)
        started: list[int] = []
        release = asyncio.Event()

        async def process_response_logic(_bot: Any, event: Any, _state: dict[str, Any]) -> None:
            started.append(int(event.message_id))
            if len(started) == 3:
                release.set()
            await release.wait()

        async def dispatch(message_id: int) -> None:
            await reply_buffer.handle_reply_event(
                _Bot(),
                _MentionEvent(message_id, f"mention-{message_id}"),
                {},
                poke_event_cls=type("PokeEvent", (), {}),
                message_event_cls=_PrivateEvent,
                group_message_event_cls=_GroupEvent,
                process_response_logic=process_response_logic,
                msg_buffer={},
                start_buffer_timer=lambda *_args: None,
                logger=_Logger(),
                concurrency_controller=controller,
                response_timeout_seconds=30,
            )

        await asyncio.wait_for(
            asyncio.gather(*(dispatch(index) for index in range(1, 4))),
            timeout=1,
        )
        assert sorted(started) == [1, 2, 3]

    asyncio.run(run())


def test_private_and_group_mention_are_marked_reply_required() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController(session_limit=2, global_limit=2)
        captured: list[dict[str, Any]] = []

        async def process_response_logic(_bot: Any, _event: Any, state: dict[str, Any]) -> None:
            captured.append(dict(state))

        for event in (_PrivateEvent(1, "private"), _MentionEvent(2, "mention")):
            await reply_buffer.handle_reply_event(
                _Bot(),
                event,
                {},
                poke_event_cls=type("PokeEvent", (), {}),
                message_event_cls=_PrivateEvent,
                group_message_event_cls=_GroupEvent,
                process_response_logic=process_response_logic,
                msg_buffer={},
                start_buffer_timer=lambda *_args: None,
                logger=_Logger(),
                concurrency_controller=controller,
                response_timeout_seconds=30,
            )

        assert len(captured) == 2
        assert all(state["reply_required"] is True for state in captured)
        assert all(float(state["response_deadline"]) > 0 for state in captured)

    asyncio.run(run())


def test_random_bot_target_is_not_upgraded_to_required_reply() -> None:
    async def run() -> None:
        msg_buffer: dict[str, dict[str, Any]] = {}
        state = {"is_random_chat": True, "message_target": "bot"}
        await reply_buffer.handle_reply_event(
            _Bot(),
            _GroupEvent(1, "random"),
            state,
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=lambda *_args: None,
            msg_buffer=msg_buffer,
            start_buffer_timer=lambda *_args: None,
            logger=_Logger(),
            concurrency_controller=reply_buffer.ReplyConcurrencyController(),
        )

        assert state["reply_required"] is False
        assert msg_buffer

    asyncio.run(run())


def test_reply_to_same_account_external_plugin_is_not_persona_direct_turn() -> None:
    event = _GroupEvent(1, "plugin followup")
    event.reply = type(
        "Reply",
        (),
        {"sender": type("Sender", (), {"user_id": "999"})()},
    )()

    assert reply_buffer._is_reply_to_bot(
        event,
        "999",
        message_target="external_plugin",
    ) is False


def test_blocked_event_never_enters_direct_or_buffer_processing() -> None:
    async def run() -> None:
        processed: list[int] = []
        msg_buffer: dict[str, dict[str, Any]] = {}
        gate = _PolicyGate(False)

        async def process(_bot: Any, event: Any, _state: dict[str, Any]) -> None:
            processed.append(int(event.message_id))

        await reply_buffer.handle_reply_event(
            _Bot(),
            _PrivateEvent(1, "blocked"),
            {},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process,
            msg_buffer=msg_buffer,
            start_buffer_timer=lambda *_args: None,
            logger=_Logger(),
            user_policy_gate=gate,
        )

        assert processed == []
        assert msg_buffer == {}
        assert gate.calls == [1]

    asyncio.run(run())


def test_buffer_dequeue_drops_user_blocked_after_enqueue() -> None:
    async def run() -> None:
        processed: list[int] = []
        msg_buffer: dict[str, dict[str, Any]] = {}
        gate = _PolicyGate(True)
        event = _GroupEvent(1, "later blocked")

        async def process(_bot: Any, current: Any, _state: dict[str, Any]) -> None:
            processed.append(int(current.message_id))

        await reply_buffer.handle_reply_event(
            _Bot(),
            event,
            {"is_random_chat": True},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process,
            msg_buffer=msg_buffer,
            start_buffer_timer=lambda *_args: None,
            logger=_Logger(),
            user_policy_gate=gate,
        )
        gate.allowed = False
        key = reply_buffer._session_key(event, group_message_event_cls=_GroupEvent, bot_self_id="999")

        await reply_buffer.run_buffer_timer(
            key,
            _Bot(),
            msg_buffer=msg_buffer,
            process_response_logic=process,
            message_event_cls=_PrivateEvent,
            message_cls=_Message,
            message_segment_cls=_MessageSegment,
            logger=_Logger(),
            user_policy_gate=gate,
        )

        assert processed == []
        assert msg_buffer == {}

    asyncio.run(run())



def test_direct_turn_cancels_active_random_turn_only() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController(session_limit=3, global_limit=3)
        msg_buffer: dict[str, dict[str, Any]] = {}
        timer_tasks: list[asyncio.Task[Any]] = []
        random_started = asyncio.Event()
        random_cancelled = asyncio.Event()
        pending_finished = asyncio.Event()
        direct_finished = asyncio.Event()

        async def process_response_logic(_bot: Any, event: Any, _state: dict[str, Any]) -> None:
            if int(event.message_id) == 1:
                random_started.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    random_cancelled.set()
                    raise
            elif int(event.message_id) == 2:
                pending_finished.set()
            else:
                direct_finished.set()

        def start_buffer_timer(key: str, bot: Any, _wait_seconds: float) -> asyncio.Task[Any]:
            task = asyncio.create_task(
                reply_buffer.run_buffer_timer(
                    key,
                    bot,
                    msg_buffer=msg_buffer,
                    process_response_logic=process_response_logic,
                    message_event_cls=_PrivateEvent,
                    message_cls=_Message,
                    message_segment_cls=_MessageSegment,
                    logger=_Logger(),
                    delay=0,
                    response_timeout_seconds=30,
                    concurrency_controller=controller,
                )
            )
            timer_tasks.append(task)
            return task

        await reply_buffer.handle_reply_event(
            _Bot(),
            _GroupEvent(1, "random"),
            {"is_random_chat": True},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=start_buffer_timer,
            logger=_Logger(),
            concurrency_controller=controller,
            response_timeout_seconds=30,
        )
        await asyncio.wait_for(random_started.wait(), timeout=1)

        await reply_buffer.handle_reply_event(
            _Bot(),
            _GroupEvent(2, "pending-random"),
            {"is_random_chat": True},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=start_buffer_timer,
            logger=_Logger(),
            concurrency_controller=controller,
            response_timeout_seconds=30,
        )

        await reply_buffer.handle_reply_event(
            _Bot(),
            _MentionEvent(3, "direct"),
            {},
            poke_event_cls=type("PokeEvent", (), {}),
            message_event_cls=_PrivateEvent,
            group_message_event_cls=_GroupEvent,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=start_buffer_timer,
            logger=_Logger(),
            concurrency_controller=controller,
            response_timeout_seconds=30,
        )

        await asyncio.wait_for(random_cancelled.wait(), timeout=1)
        await asyncio.wait_for(direct_finished.wait(), timeout=1)
        await asyncio.wait_for(pending_finished.wait(), timeout=2)
        await asyncio.gather(*timer_tasks, return_exceptions=True)

    asyncio.run(run())


def test_session_queue_does_not_consume_global_slots() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController(session_limit=1, global_limit=1)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def occupy_first() -> None:
            async with controller.direct_turn("bot:group-a"):
                order.append("a1")
                first_started.set()
                await release_first.wait()

        async def queued_same_session() -> None:
            async with controller.direct_turn("bot:group-a"):
                order.append("a2")

        async def other_session() -> None:
            async with controller.direct_turn("bot:group-b"):
                order.append("b1")

        first = asyncio.create_task(occupy_first())
        await first_started.wait()
        queued = asyncio.create_task(queued_same_session())
        other = asyncio.create_task(other_session())
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first, queued, other)

        assert order == ["a1", "b1", "a2"]

    asyncio.run(run())
