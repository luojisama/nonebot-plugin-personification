from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest

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


@dataclass
class _FileSeg:
    data: dict[str, str]
    type: str = "file"


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


def test_private_file_video_is_carried_into_same_sender_followup() -> None:
    async def run() -> None:
        reply_buffer._clear_recent_media_for_test()
        controller = reply_buffer.ReplyConcurrencyController(session_limit=2, global_limit=2)
        states: list[dict[str, Any]] = []

        async def process_response_logic(_bot: Any, _event: Any, state: dict[str, Any]) -> None:
            states.append(dict(state))

        file_event = _PrivateEvent(1, "")
        file_event.message = _Message(
            [_FileSeg({"file": "opaque-video-token", "name": "gameplay.mp4"})]
        )
        prompt_event = _PrivateEvent(2, "概括刚才的视频")
        msg_buffer: dict[str, dict[str, Any]] = {}
        common = {
            "poke_event_cls": type("PokeEvent", (), {}),
            "message_event_cls": _PrivateEvent,
            "group_message_event_cls": _GroupEvent,
            "process_response_logic": process_response_logic,
            "msg_buffer": msg_buffer,
            "start_buffer_timer": lambda *_args: None,
            "logger": _Logger(),
            "concurrency_controller": controller,
            "response_timeout_seconds": 30,
        }
        try:
            await reply_buffer.handle_reply_event(_Bot(), file_event, {}, **common)
            await reply_buffer.handle_reply_event(_Bot(), prompt_event, {}, **common)
        finally:
            reply_buffer._clear_recent_media_for_test()

        assert len(states) == 2
        first_media = states[0]["turn_media_context"]
        followup_media = states[1]["turn_media_context"]
        assert len(first_media) == 1
        assert len(followup_media) == 1
        assert states[1]["batch_event_count"] == 2
        assert followup_media[0]["kind"] == "video"
        assert followup_media[0]["origin"] == "batch"
        assert followup_media[0]["owner_user_id"] == "123"
        assert followup_media[0]["message_id"] == "1"
        assert followup_media[0]["file_id"] == "opaque-video-token"
        assert reply_buffer._recent_media_for_followup(
            session_key="999:private_123",
            user_id="123",
            now=time.monotonic(),
        ) == []

    asyncio.run(run())


def test_recent_file_media_is_not_shared_with_another_group_sender() -> None:
    reply_buffer._clear_recent_media_for_test()
    now = time.monotonic()
    refs = [
        {
            "media_id": "media-owner-a",
            "ref": "opaque-video-token",
            "origin": "current",
            "owner_user_id": "owner-a",
            "message_id": "file-message",
            "kind": "video",
            "file_id": "opaque-video-token",
        }
    ]
    try:
        reply_buffer._remember_recent_media(
            session_key="bot:group-1",
            user_id="owner-a",
            values=refs,
            now=now,
        )
        assert reply_buffer._recent_media_for_followup(
            session_key="bot:group-1",
            user_id="owner-b",
            now=now + 1,
        ) == []
    finally:
        reply_buffer._clear_recent_media_for_test()


def test_unresolved_quoted_media_marks_completion_evidence_unavailable() -> None:
    async def run() -> None:
        reply_buffer._clear_recent_media_for_test()
        controller = reply_buffer.ReplyConcurrencyController(session_limit=2, global_limit=2)
        captured: dict[str, Any] = {}

        async def process_response_logic(_bot: Any, _event: Any, state: dict[str, Any]) -> None:
            captured.update(state)

        event = _PrivateEvent(9, "概括引用的视频")
        event.reply = type(
            "Reply",
            (),
            {
                "message_id": "404",
                "sender": type("Sender", (), {"user_id": "123"})(),
                "message": [],
            },
        )()
        try:
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
        finally:
            reply_buffer._clear_recent_media_for_test()

        assert captured["turn_media_context"] == []
        assert captured["media_reference_unavailable"] is True

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


def test_session_gates_are_reclaimed_after_large_session_churn() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController(session_limit=3, global_limit=12)
        for index in range(10_000):
            async with controller.direct_turn(f"bot:private_{index}"):
                pass
        assert controller.snapshot() == {"active": 0, "waiting": 0, "session_gates": 0}

    asyncio.run(run())


def test_reply_admission_timeout_and_cancellation_release_all_gates() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController(session_limit=1, global_limit=1)
        occupied = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with controller.direct_turn("bot:group-a"):
                occupied.set()
                await release.wait()

        first = asyncio.create_task(holder())
        await occupied.wait()
        with pytest.raises(reply_buffer.ReplyAdmissionTimeout):
            async with controller.direct_turn(
                "bot:group-b",
                deadline=time.monotonic() + 0.02,
            ):
                raise AssertionError("timed out turn must not be admitted")

        async def queued() -> None:
            async with controller.direct_turn(
                "bot:group-c",
                deadline=time.monotonic() + 1.0,
            ):
                pass

        pending = asyncio.create_task(queued())
        await asyncio.sleep(0)
        assert controller.snapshot()["waiting"] == 1
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        await first
        assert controller.snapshot() == {"active": 0, "waiting": 0, "session_gates": 0}

    asyncio.run(run())


def test_processing_timeout_is_not_reclassified_as_admission_timeout() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController()
        with pytest.raises(asyncio.TimeoutError):
            async with controller.direct_turn("bot:group-a"):
                raise asyncio.TimeoutError
        assert controller.snapshot() == {"active": 0, "waiting": 0, "session_gates": 0}

    asyncio.run(run())


def test_reply_concurrency_limits_hold_under_fifty_turn_burst() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController(session_limit=3, global_limit=12)
        global_active = 0
        global_peak = 0
        session_active: dict[str, int] = {}
        session_peak: dict[str, int] = {}
        guard = asyncio.Lock()

        async def worker(index: int) -> None:
            nonlocal global_active, global_peak
            key = f"bot:group-{index % 10}"
            async with controller.direct_turn(key, deadline=time.monotonic() + 2.0):
                async with guard:
                    global_active += 1
                    global_peak = max(global_peak, global_active)
                    session_active[key] = session_active.get(key, 0) + 1
                    session_peak[key] = max(session_peak.get(key, 0), session_active[key])
                await asyncio.sleep(0.002)
                async with guard:
                    global_active -= 1
                    session_active[key] -= 1

        await asyncio.gather(*(worker(index) for index in range(50)))
        assert global_peak <= 12
        assert max(session_peak.values()) <= 3
        assert controller.snapshot() == {"active": 0, "waiting": 0, "session_gates": 0}

    asyncio.run(run())


def test_external_buffer_cleanup_releases_retained_session_gate() -> None:
    context_cleanup = load_personification_module("plugin.personification.core.context_cleanup")
    controller = reply_buffer.ReplyConcurrencyController()
    controller.retain_buffer_session("bot:group-1")
    entry: dict[str, Any] = {
        "timer_task": None,
        "_release_concurrency_gate": lambda: controller.release_buffer_session("bot:group-1"),
    }
    msg_buffer = {"bot:group-1": entry}
    assert controller.snapshot()["session_gates"] == 1
    assert context_cleanup.clear_message_buffer(msg_buffer, "group-1") == 1
    assert controller.snapshot() == {"active": 0, "waiting": 0, "session_gates": 0}


def test_buffer_entry_retains_gate_until_dequeue_finishes() -> None:
    async def run() -> None:
        controller = reply_buffer.ReplyConcurrencyController()
        msg_buffer: dict[str, dict[str, Any]] = {}
        event = _GroupEvent(1, "buffered")

        async def process(_bot: Any, _event: Any, _state: dict[str, Any]) -> None:
            await asyncio.sleep(0)

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
            concurrency_controller=controller,
        )
        assert controller.snapshot()["session_gates"] == 1
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
            concurrency_controller=controller,
        )
        assert msg_buffer == {}
        assert controller.snapshot() == {"active": 0, "waiting": 0, "session_gates": 0}

    asyncio.run(run())
