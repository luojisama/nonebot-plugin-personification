from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment


class ActionExecutor:
    def __init__(self, bot: Any, event: Any, config: Any, logger: Any) -> None:
        self.bot = bot
        self.event = event
        self.config = config
        self.logger = logger
        self.pending_actions: list[dict[str, Any]] = []

    def bind_pending_actions(self, actions: list[dict[str, Any]]) -> None:
        self.pending_actions = actions

    def queue_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        item = {"type": str(action or "").strip(), "params": dict(params or {})}
        self.pending_actions.append(item)
        return item

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
            case "send_qq_face":
                face_id = int(params["face_id"])
                text = str(params.get("text", "") or "").strip()
                message = MessageSegment.face(face_id)
                if text:
                    message += text
                await self.bot.send(self.event, message)
                return "已发送 QQ 表情"
            case "send_qq_image_expression":
                url = str(params.get("url", "") or "").strip()
                if not url:
                    return "QQ 表情发送失败：缺少图片 URL"
                text = str(params.get("text", "") or "").strip()
                message = MessageSegment.image(url)
                if text:
                    message += text
                await self.bot.send(self.event, message)
                return "已发送 QQ 图片表情"
            case "send_image_url":
                url = str(params.get("url", "") or "").strip()
                if not url:
                    return "图片发送失败：缺少图片 URL"
                text = str(params.get("text", "") or "").strip()
                message = MessageSegment.image(url)
                if text:
                    message += text
                await self.bot.send(self.event, message)
                return "已发送图片"
            case "send_qq_mface":
                data = params.get("data") if isinstance(params, dict) else {}
                if not isinstance(data, dict) or not data:
                    return "QQ mface 发送失败：缺少 mface 数据"
                text = str(params.get("text", "") or "").strip()
                message = MessageSegment("mface", data)
                if text:
                    message += text
                await self.bot.send(self.event, message)
                return "已发送 QQ mface 表情"
            case "poke_user":
                await self.bot.send(
                    self.event,
                    MessageSegment("poke", {"qq": params["user_id"]}),
                )
                return "已戳"
            case _:
                self.logger.warning(f"[executor] unknown action: {action}")
                return f"未知 action: {action}"
