import random
from typing import Any, Callable

from ..core.sticker_library import resolve_sticker_dir
from ..skills.skillpacks.sticker_tool.scripts.impl import choose_sticker_for_context


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
    group_id = str(event.group_id)
    group_config = get_group_config(group_id)
    sticker_enabled = group_config.get("sticker_enabled", True)

    if not sticker_enabled:
        state["is_random_chat"] = True
        state["force_mode"] = "text_only"
        await handle_reply(bot, event, state)
        return

    sticker_dir = resolve_sticker_dir(sticker_path)

    plain_getter = getattr(event, "get_plaintext", None)
    plain_text = str(plain_getter() if callable(plain_getter) else "").strip()
    proactive_context = plain_text or "群里有人刚说了一句，适合用表情包轻轻接话"
    runtime_config = plugin_config or type("Cfg", (), {"personification_sticker_semantic": True})()
    selected = None
    if sticker_dir.exists() and sticker_dir.is_dir():
        selected = await choose_sticker_for_context(
            sticker_dir,
            mood="搞笑|接梗",
            context=proactive_context,
            draft_reply="",
            proactive=True,
            plugin_config=runtime_config,
            call_ai_api=None,
            minimum_score=2,
        )
    if selected is not None and random.random() < 0.45:
        logger.info(f"拟人插件：触发水群 [单独表情包] {selected.name}")
        await finish(message_segment_cls.image(f"file:///{selected.absolute()}"))
        return

    state["is_random_chat"] = True
    state["force_mode"] = "mixed"
    await handle_reply(bot, event, state)
