from typing import Any, Callable


async def handle_sticker_chat_event(
    bot: Any,
    event: Any,
    state: dict,
    *,
    get_group_config: Callable[[str], dict],
    sticker_path: str,
    plugin_config: Any = None,
    logger: Any,
    message_segment_cls: Any,
    finish: Callable[[Any], Any],
    handle_reply: Callable[[Any, Any, dict], Any],
) -> None:
    """随机水群被「表情概率」触发时的入口。

    历史实现会在选到表情后以 0.45 概率直接 finish() 把表情甩出去，
    这条路径完全绕过 LLM——既不判断此刻该不该接话，也不判断接什么。
    现在统一走完整回复 pipeline（force_mode=mixed），由 LLM 决定：
    保持沉默([NO_REPLY]) / 回一句文字 / 配一张表情。表情的挑选与
    appropriateness 门控都在 pipeline 内（choose_sticker_for_context +
    semantic_frame.sticker_appropriate）完成，不再有无 LLM 判断的直发捷径。
    """
    group_id = str(event.group_id)
    group_config = get_group_config(group_id)
    sticker_enabled = group_config.get("sticker_enabled", True)

    state["is_random_chat"] = True
    state["force_mode"] = "mixed" if sticker_enabled else "text_only"
    await handle_reply(bot, event, state)
