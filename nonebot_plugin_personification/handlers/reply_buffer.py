import asyncio
from typing import Any, Callable, Dict


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

    combined_message = message_cls()
    first_event = events[0]

    for i, ev in enumerate(events):
        if isinstance(ev, message_event_cls):
            if i > 0:
                combined_message.append(message_segment_cls.text(" "))
            combined_message.extend(ev.message)

    state["concatenated_message"] = combined_message
    try:
        await process_response_logic(bot, first_event, state)
    except Exception as e:
        logger.error(f"拟人插件：处理拼接消息失败: {e}")


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

    if key in msg_buffer:
        timer_task = msg_buffer[key].get("timer_task")
        if timer_task:
            timer_task.cancel()
        msg_buffer[key]["events"].append(event)
    else:
        msg_buffer[key] = {"events": [event], "state": state}

    task = start_buffer_timer(key, bot)
    msg_buffer[key]["timer_task"] = task
    logger.debug(f"拟人插件：已缓冲用户 {user_id} 的消息，等待后续...")
