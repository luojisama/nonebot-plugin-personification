from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment


class ActionExecutor:
    def __init__(self, bot: Any, event: Any, config: Any, logger: Any) -> None:
        self.bot = bot
        self.event = event
        self.config = config
        self.logger = logger

    async def send_text(self, text: str) -> None:
        content = str(text or "").strip()
        if content:
            await self.bot.send(self.event, content)

    async def send_image_b64(self, image_b64: str) -> None:
        payload = str(image_b64 or "").strip()
        if payload:
            await self.bot.send(self.event, MessageSegment.image(f"base64://{payload}"))

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
