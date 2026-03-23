from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment


class ActionExecutor:
    def __init__(self, bot: Any, event: Any, config: Any, logger: Any) -> None:
        self.bot = bot
        self.event = event
        self.config = config
        self.logger = logger

    async def execute(self, action: str, params: dict) -> str:
        match action:
            case "send_sticker":
                await self.bot.send(self.event, MessageSegment.image(params["path"]))
                return "已发送表情包"
            case "poke_user":
                await self.bot.send(
                    self.event,
                    MessageSegment("poke", {"qq": params["user_id"]}),
                )
                return "已戳"
            case _:
                self.logger.warning(f"[executor] unknown action: {action}")
                return f"未知 action: {action}"
