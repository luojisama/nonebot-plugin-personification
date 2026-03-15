import asyncio
from typing import Any, Callable, Dict

from nonebot import on_message, on_notice
from nonebot.rule import Rule

try:
    from nonebot.typing import T_State
    from nonebot.adapters.onebot.v11 import Bot, Event
except Exception:  # pragma: no cover - fallback for lightweight unit-test stubs
    Bot = Any
    Event = Any
    T_State = Dict[str, Any]


def register_reply_matchers(
    *,
    personification_rule: Callable[[Event, T_State], Any],
    poke_notice_rule: Callable[[Event], Any],
    handle_reply_event: Callable[..., Any],
    process_response_logic: Callable[[Bot, Event, T_State], Any],
    msg_buffer: Dict[str, Dict[str, Any]],
    run_buffer_timer: Callable[..., Any],
    poke_event_cls: Any,
    message_event_cls: Any,
    group_message_event_cls: Any,
    message_cls: Any,
    message_segment_cls: Any,
    logger: Any,
) -> Dict[str, Any]:
    reply_matcher = on_message(rule=Rule(personification_rule), priority=100, block=True)
    poke_notice_matcher = on_notice(rule=Rule(poke_notice_rule), priority=10, block=False)

    async def _buffer_timer(key: str, bot: Bot):
        await run_buffer_timer(
            key,
            bot,
            msg_buffer=msg_buffer,
            process_response_logic=process_response_logic,
            message_event_cls=message_event_cls,
            message_cls=message_cls,
            message_segment_cls=message_segment_cls,
            logger=logger,
        )

    @reply_matcher.handle()
    @poke_notice_matcher.handle()
    async def _handle_reply(bot: Bot, event: Event, state: T_State):
        await handle_reply_event(
            bot,
            event,
            state,
            poke_event_cls=poke_event_cls,
            message_event_cls=message_event_cls,
            group_message_event_cls=group_message_event_cls,
            process_response_logic=process_response_logic,
            msg_buffer=msg_buffer,
            start_buffer_timer=lambda key, _bot: asyncio.create_task(_buffer_timer(key, _bot)),
            logger=logger,
        )

    return {
        "reply_matcher": reply_matcher,
        "poke_notice_matcher": poke_notice_matcher,
        "handle_reply": _handle_reply,
    }
