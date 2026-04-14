from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg

from .persona_admin_commands import dispatch_persona_admin_command


def register_persona_admin_matchers(
    *,
    runtime_bundle: Any,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> dict[str, Any]:
    def _register_command(command: str, *, aliases: set[str] | None = None, **kwargs: Any) -> Any:
        if track_command_keywords:
            track_command_keywords(command, aliases)
        if aliases is None:
            return on_command(command, **kwargs)
        return on_command(command, aliases=aliases, **kwargs)

    persona_admin_cmd = _register_command(
        "persona",
        aliases={"拟人", "人格"},
        priority=5,
        block=True,
    )

    @persona_admin_cmd.handle()
    async def _handle_persona_admin(_bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
        await dispatch_persona_admin_command(
            persona_admin_cmd,
            bundle=runtime_bundle,
            event=event,
            arg_text=args.extract_plain_text().strip(),
        )

    return {
        "persona_admin_cmd": persona_admin_cmd,
    }
