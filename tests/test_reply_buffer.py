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


class _Bot:
    self_id = "999"


class _Logger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None


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
