import asyncio
from typing import Any, Callable, Dict


def _has_reply_semantics(event: Any) -> bool:
    # message_id 是消息自身 ID，不代表引用/回复关系。
    # 这里只检查真正表示“我在回复谁”的字段。
    for attr in ("reply", "quoted", "quote"):
        value = getattr(event, attr, None)
        if value:
            return True
    if getattr(event, "reply_to_message_id", None):
        return True
    return False


def _select_merged_event(events: list[Any]) -> Any:
    """
    合并连续消息时优先沿用最后一条带引用语义的事件。

    文本会被拼接，但 reply/quote 这类上下文只存在于具体事件对象上。
    这里不强行伪造 event，而是选择语义最完整的那条真实事件往下传。
    """
    for event in reversed(events):
        if _has_reply_semantics(event):
            return event
    return events[-1]


async def run_buffer_timer(
    key: str,
    bot: Any,
    *,
    msg_buffer: Dict[str, Dict[str, Any]],
    process_response_logic: Callable[[Any, Any, Dict[str, Any]], Any],
    message_event_cls: Any,
    message_cls: Any,
    message_segment_cls: Any,
    logger: Any,
    finished_exception_cls: Any = None,
    delay: float = 3.0,
) -> None:
    await asyncio.sleep(delay)

    if key not in msg_buffer:
        return

    data = msg_buffer.pop(key)
    events = data.get("events", [])
    state = data.get("state", {})
    if not events:
        return

    selected_event = _select_merged_event(events)
    combined_message = None
    try:
        combined_message = message_cls()
        for i, ev in enumerate(events):
            if isinstance(ev, message_event_cls):
                if i > 0:
                    combined_message.append(message_segment_cls.text(" "))
                combined_message.extend(getattr(ev, "message", message_cls()))
    except Exception as e:
        logger.warning(f"拟人插件：拼接消息构建失败，回退单条处理: {e}")
        combined_message = None

    if combined_message is not None:
        state["concatenated_message"] = combined_message
    state["merged_event_context"] = {
        "event_count": len(events),
        "selected_event_index": events.index(selected_event),
    }
    try:
        await process_response_logic(bot, selected_event, state)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if finished_exception_cls and isinstance(e, finished_exception_cls):
            logger.debug("拟人插件：拼接消息处理提前结束（FinishedException）")
            return
        if "concatenated_message" in state:
            retry_state = dict(state)
            retry_state.pop("concatenated_message", None)
            try:
                await process_response_logic(bot, selected_event, retry_state)
                logger.warning(f"拟人插件：拼接消息处理失败（{type(e).__name__}: {e}），已回退单条处理成功")
                return
            except Exception:
                pass
        logger.exception("拟人插件：处理拼接消息失败")


async def handle_reply_event(
    bot: Any,
    event: Any,
    state: Dict[str, Any],
    *,
    poke_event_cls: Any,
    message_event_cls: Any,
    group_message_event_cls: Any,
    process_response_logic: Callable[[Any, Any, Dict[str, Any]], Any],
    msg_buffer: Dict[str, Dict[str, Any]],
    start_buffer_timer: Callable[[str, Any], Any],
    logger: Any,
) -> None:
    if isinstance(event, poke_event_cls):
        await process_response_logic(bot, event, state)
        return

    if not isinstance(event, message_event_cls):
        return

    user_id = str(event.user_id)
    if isinstance(event, group_message_event_cls):
        group_id = str(event.group_id)
    else:
        group_id = f"private_{user_id}"
    key = f"{group_id}_{user_id}"

    bot_self_id = str(getattr(bot, "self_id", "") or "")
    is_direct_mention = False
    if bot_self_id:
        try:
            for seg in getattr(event, "message", []) or []:
                if getattr(seg, "type", None) != "at":
                    continue
                qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
                if qq == bot_self_id:
                    is_direct_mention = True
                    break
        except Exception:
            is_direct_mention = False

    if is_direct_mention:
        existing = msg_buffer.pop(key, None)
        if isinstance(existing, dict):
            timer_task = existing.get("timer_task")
            if timer_task:
                timer_task.cancel()
        await process_response_logic(bot, event, state)
        return

    if key in msg_buffer:
        timer_task = msg_buffer[key].get("timer_task")
        if timer_task:
            timer_task.cancel()
        msg_buffer[key]["events"].append(event)
    else:
        msg_buffer[key] = {"events": [event], "state": dict(state)}

    task = start_buffer_timer(key, bot)
    msg_buffer[key]["timer_task"] = task
    logger.debug(f"拟人插件：已缓冲用户 {user_id} 的消息，等待后续...")
