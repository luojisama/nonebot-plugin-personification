import random
from pathlib import Path
from typing import Any, Callable

from ..skills.skillpacks.sticker_tool.scripts.impl import select_sticker


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
        mode = "text_only"
    else:
        mode = random.choice(["text_only", "mixed", "sticker_only"])

    sticker_dir = Path(sticker_path)
    available_stickers = []
    if sticker_dir.exists() and sticker_dir.is_dir():
        available_stickers = [
            f for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]
        ]

    plain_getter = getattr(event, "get_plaintext", None)
    plain_text = str(plain_getter() if callable(plain_getter) else "").strip()
    proactive_context = plain_text or "群里有人刚说了一句，适合用表情包轻轻接话"
    if mode == "mixed" and available_stickers:
        runtime_config = plugin_config or type("Cfg", (), {"personification_sticker_semantic": True})()
        selected = select_sticker(
            sticker_dir,
            mood="想接梗，轻松回应",
            context=proactive_context,
            proactive=False,
            plugin_config=runtime_config,
            allow_fallback=False,
            minimum_score=2,
        )
        if selected and random.random() < 0.45:
            chosen = Path(selected)
            logger.info(f"拟人插件：触发水群 [单独表情包] {chosen.name}")
            await finish(message_segment_cls.image(f"file:///{chosen.absolute()}"))
            return
    elif mode == "sticker_only" and available_stickers:
        chosen = Path(random.choice(available_stickers))
        logger.info(f"拟人插件：触发水群 [单独表情包] {chosen.name}")
        await finish(message_segment_cls.image(f"file:///{chosen.absolute()}"))
        return

    if mode in ["text_only", "mixed"]:
        state["is_random_chat"] = True
        state["force_mode"] = mode
        await handle_reply(bot, event, state)
