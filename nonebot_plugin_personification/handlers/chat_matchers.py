from typing import Any, Callable, Dict

from nonebot import on_message
from nonebot.rule import Rule

try:
    from nonebot.typing import T_State
    from nonebot.adapters.onebot.v11 import Bot, MessageEvent
except Exception:  # pragma: no cover - fallback for lightweight unit-test stubs
    Bot = Any
    MessageEvent = Any
    T_State = Dict[str, Any]


def register_chat_matchers(
    *,
    record_msg_rule: Callable[[Any], Any],
    sticker_chat_rule: Callable[[Any], Any],
    handle_record_message_event: Callable[..., None],
    resolve_record_message: Any,
    get_custom_title: Callable[[str], str] | None = None,
    record_group_msg: Any,
    should_trigger_auto_analyze: Callable[[str, int], bool] | None = None,
    logger: Any,
    create_background_task: Callable[[str], None],
    create_summary_task: Callable[[str], None] | None,
    handle_sticker_chat_event: Callable[..., Any],
    get_group_config: Callable[[str], dict],
    sticker_path: str,
    plugin_config: Any,
    message_segment_cls: Any,
    handle_reply: Callable[[Bot, MessageEvent, T_State], Any],
) -> Dict[str, Any]:
    record_msg_matcher = on_message(rule=Rule(record_msg_rule), priority=999, block=False)

    @record_msg_matcher.handle()
    async def _handle_record_msg(_bot: Bot, event: MessageEvent):
        handle_record_message_event(
            event,
            resolve_record_message=resolve_record_message,
            get_custom_title=get_custom_title,
            record_group_msg=record_group_msg,
            should_trigger_auto_analyze=should_trigger_auto_analyze,
            logger=logger,
            create_background_task=create_background_task,
            create_summary_task=create_summary_task,
        )

    sticker_chat_matcher = on_message(rule=Rule(sticker_chat_rule), priority=101, block=True)

    @sticker_chat_matcher.handle()
    async def _handle_sticker_chat(bot: Bot, event: MessageEvent, state: T_State):
        await handle_sticker_chat_event(
            bot,
            event,
            state,
            get_group_config=get_group_config,
            sticker_path=sticker_path,
            plugin_config=plugin_config,
            logger=logger,
            message_segment_cls=message_segment_cls,
            finish=sticker_chat_matcher.finish,
            handle_reply=handle_reply,
        )

    return {
        "record_msg_matcher": record_msg_matcher,
        "sticker_chat_matcher": sticker_chat_matcher,
    }
