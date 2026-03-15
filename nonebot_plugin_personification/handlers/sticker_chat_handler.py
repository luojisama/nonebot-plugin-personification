import random
from pathlib import Path
from typing import Any, Callable


async def handle_sticker_chat_event(
    bot: Any,
    event: Any,
    state: dict,
    *,
    get_group_config: Callable[[str], dict],
    sticker_path: str,
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
        mode = random.choice(["text_only", "sticker_only", "mixed"])

    sticker_dir = Path(sticker_path)
    available_stickers = []
    if sticker_dir.exists() and sticker_dir.is_dir():
        available_stickers = [
            f for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]
        ]

    if mode == "sticker_only":
        if available_stickers:
            random_sticker = random.choice(available_stickers)
            logger.info(f"拟人插件：触发水群 [单独表情包] {random_sticker.name}")
            await finish(message_segment_cls.image(f"file:///{random_sticker.absolute()}"))
            return
        mode = "text_only"

    if mode in ["text_only", "mixed"]:
        state["is_random_chat"] = True
        state["force_mode"] = mode
        await handle_reply(bot, event, state)
