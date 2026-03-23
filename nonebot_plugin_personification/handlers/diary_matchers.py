from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot


def register_diary_matchers(
    *,
    superuser_permission: Any,
    handle_manual_diary_command: Any,
    qzone_publish_available: bool,
    update_qzone_cookie: Any,
    generate_ai_diary: Any,
    publish_qzone_shuo: Any,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> Dict[str, Any]:
    def _register_command(command: str, *, aliases: set[str] | None = None, **kwargs: Any) -> Any:
        if track_command_keywords:
            track_command_keywords(command, aliases)
        if aliases is None:
            return on_command(command, **kwargs)
        return on_command(command, aliases=aliases, **kwargs)

    manual_diary_cmd = _register_command("发个说说", permission=superuser_permission, priority=5, block=True)

    @manual_diary_cmd.handle()
    async def _handle_manual_diary(bot: Bot):
        await handle_manual_diary_command(
            manual_diary_cmd,
            bot=bot,
            qzone_publish_available=qzone_publish_available,
            update_qzone_cookie=update_qzone_cookie,
            generate_ai_diary=generate_ai_diary,
            publish_qzone_shuo=publish_qzone_shuo,
        )

    return {
        "manual_diary_cmd": manual_diary_cmd,
    }
