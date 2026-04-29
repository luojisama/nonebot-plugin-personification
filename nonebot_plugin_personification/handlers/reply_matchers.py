import asyncio
import time
from typing import Any, Callable, Dict

from nonebot import on_message, on_notice
from nonebot.rule import Rule

from ..core.group_mute import update_group_mute_from_notice

try:
    from nonebot.typing import T_State
    from nonebot.adapters.onebot.v11 import Bot, Event
except Exception:  # pragma: no cover - fallback for lightweight unit-test stubs
    Bot = Any
    Event = Any
    T_State = Dict[str, Any]


_RULE_EVAL_CACHE: Dict[str, dict[str, Any]] = {}
_RULE_EVAL_CACHE_TTL_SECONDS = 30.0
_RULE_EVAL_CACHE_MAX_SIZE = 128


def _build_rule_cache_key(event: Event) -> str:
    message_id = str(getattr(event, "message_id", "") or "").strip()
    if message_id:
        return f"{event.__class__.__name__}:{message_id}"

    user_id = str(getattr(event, "user_id", "") or "").strip()
    group_id = str(getattr(event, "group_id", "") or "").strip()
    notice_type = str(getattr(event, "notice_type", "") or "").strip()
    return f"{event.__class__.__name__}:{group_id}:{user_id}:{notice_type}:{id(event)}"


def _prune_rule_eval_cache(now_ts: float) -> None:
    expired_keys = [
        key
        for key, cached in _RULE_EVAL_CACHE.items()
        if now_ts - float(cached.get("saved_at", 0) or 0) > _RULE_EVAL_CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        _RULE_EVAL_CACHE.pop(key, None)

    if len(_RULE_EVAL_CACHE) <= _RULE_EVAL_CACHE_MAX_SIZE:
        return

    overflow = sorted(
        _RULE_EVAL_CACHE.items(),
        key=lambda item: float(item[1].get("saved_at", 0) or 0),
    )[: len(_RULE_EVAL_CACHE) - _RULE_EVAL_CACHE_MAX_SIZE]
    for key, _ in overflow:
        _RULE_EVAL_CACHE.pop(key, None)


async def _evaluate_personification_rule(
    *,
    personification_rule: Callable[[Event, T_State], Any],
    event: Event,
    state: T_State,
) -> dict[str, Any]:
    cache_key = _build_rule_cache_key(event)
    now_ts = time.time()
    cached = _RULE_EVAL_CACHE.get(cache_key)
    if isinstance(cached, dict):
        saved_at = float(cached.get("saved_at", 0) or 0)
        if now_ts - saved_at <= _RULE_EVAL_CACHE_TTL_SECONDS:
            cached_state = cached.get("state")
            if isinstance(cached_state, dict):
                state.update(cached_state)
            return {
                "matched": bool(cached.get("matched")),
                "is_random_chat": bool(cached.get("is_random_chat")),
            }

    matched = bool(await personification_rule(event, state))
    result = {
        "matched": matched,
        "is_random_chat": bool(state.get("is_random_chat", False)),
    }
    _RULE_EVAL_CACHE[cache_key] = {
        "saved_at": now_ts,
        "matched": result["matched"],
        "is_random_chat": result["is_random_chat"],
        "state": dict(state),
    }
    _prune_rule_eval_cache(now_ts)
    return result


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
    plugin_config: Any,
    finished_exception_cls: Any = None,
) -> Dict[str, Any]:
    response_timeout_seconds = max(
        30.0,
        float(getattr(plugin_config, "personification_response_timeout", 180) or 180),
    )

    async def _direct_reply_rule(event: Event, state: T_State) -> bool:
        result = await _evaluate_personification_rule(
            personification_rule=personification_rule,
            event=event,
            state=state,
        )
        return bool(result.get("matched")) and not bool(result.get("is_random_chat"))

    async def _random_reply_rule(event: Event, state: T_State) -> bool:
        result = await _evaluate_personification_rule(
            personification_rule=personification_rule,
            event=event,
            state=state,
        )
        return bool(result.get("matched")) and bool(result.get("is_random_chat"))

    direct_reply_matcher = on_message(rule=Rule(_direct_reply_rule), priority=100, block=True)
    random_reply_matcher = on_message(rule=Rule(_random_reply_rule), priority=100, block=False)
    poke_notice_matcher = on_notice(rule=Rule(poke_notice_rule), priority=10, block=False)

    async def _group_mute_notice_rule(event: Event) -> bool:
        return str(getattr(event, "notice_type", "") or "").strip() == "group_ban"

    group_mute_notice_matcher = on_notice(rule=Rule(_group_mute_notice_rule), priority=9, block=False)

    @group_mute_notice_matcher.handle()
    async def _handle_group_mute_notice(bot: Bot, event: Event):
        update_group_mute_from_notice(
            event,
            bot_self_id=str(getattr(bot, "self_id", "") or ""),
            logger=logger,
        )

    async def _buffer_timer(key: str, bot: Bot, wait_seconds: float):
        await run_buffer_timer(
            key,
            bot,
            msg_buffer=msg_buffer,
            process_response_logic=process_response_logic,
            message_event_cls=message_event_cls,
            message_cls=message_cls,
            message_segment_cls=message_segment_cls,
            logger=logger,
            finished_exception_cls=finished_exception_cls,
            delay=wait_seconds,
            response_timeout_seconds=response_timeout_seconds,
        )

    @direct_reply_matcher.handle()
    @random_reply_matcher.handle()
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
            start_buffer_timer=lambda key, _bot, wait_seconds: asyncio.create_task(
                _buffer_timer(key, _bot, wait_seconds)
            ),
            logger=logger,
        )

    return {
        "reply_matcher": direct_reply_matcher,
        "random_reply_matcher": random_reply_matcher,
        "poke_notice_matcher": poke_notice_matcher,
        "group_mute_notice_matcher": group_mute_notice_matcher,
        "handle_reply": _handle_reply,
    }

