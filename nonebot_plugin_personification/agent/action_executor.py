from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment

from ..core.qq_outbound import QQOutboundLedger, SendReceipt, build_outbound_context
from ..core.qq_recall import QQRecallService
from ..core.visible_output import guard_visible_text


class ActionExecutor:
    def __init__(
        self,
        bot: Any,
        event: Any,
        config: Any,
        logger: Any,
        *,
        qq_outbound_ledger: Any | None = None,
        operation_id: str | None = None,
        user_target: str | None = None,
        qq_recall_service: QQRecallService | None = None,
        recall_cutoff: float | None = None,
    ) -> None:
        self.bot = bot
        self.event = event
        self.config = config
        self.logger = logger
        self.qq_outbound_ledger = qq_outbound_ledger
        self.operation_id = str(operation_id or "").strip()
        self.user_target = str(user_target or "").strip()
        self.qq_recall_service = qq_recall_service
        if self.qq_recall_service is None and isinstance(qq_outbound_ledger, QQOutboundLedger):
            self.qq_recall_service = QQRecallService(
                qq_outbound_ledger,
                plugin_config=config,
                logger=logger,
            )
        self.recall_cutoff = float(recall_cutoff or 0.0)
        self.pending_actions: list[dict[str, Any]] = []
        self.last_delivery_confirmed = False
        self.last_recall_result: Any = None
        self.receipts: list[SendReceipt] = []

    async def _send(self, message: Any, *, surface: str) -> None:
        if self.qq_outbound_ledger is None:
            await self.bot.send(self.event, message)
        else:
            context = build_outbound_context(
                bot=self.bot,
                event=self.event,
                surface=surface,
                operation_id=self.operation_id,
                user_target=self.user_target,
            )
            self.operation_id = context.operation_id
            receipt = await self.qq_outbound_ledger.dispatch(
                context,
                message,
                lambda: self.bot.send(self.event, message),
            )
            self.receipts.append(receipt)
            self.last_delivery_confirmed = receipt.status == "sent"
            return
        self.last_delivery_confirmed = True

    def bind_pending_actions(self, actions: list[dict[str, Any]]) -> None:
        self.pending_actions = actions

    def queue_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        item = {"type": str(action or "").strip(), "params": dict(params or {})}
        self.pending_actions.append(item)
        return item

    async def send_text(self, text: str) -> None:
        content = guard_visible_text(text, logger=self.logger, surface="agent_action_text", allow_direct_media=False)
        if content:
            await self._send(content, surface="agent_action_text")

    async def send_image_b64(self, image_b64: str) -> None:
        payload = str(image_b64 or "").strip()
        if payload:
            await self._send(
                MessageSegment.image(f"base64://{payload}"),
                surface="agent_action_image",
            )

    async def execute(self, action: str, params: dict) -> str:
        self.last_delivery_confirmed = False
        match action:
            case "recall_latest_qq_operation":
                if self.qq_recall_service is None:
                    return "撤回能力不可用"
                self.last_recall_result = await self.qq_recall_service.recall_latest(
                    bot=self.bot,
                    event=self.event,
                    requester_user_id=str(getattr(self.event, "user_id", "") or self.user_target),
                    actor_kind="user",
                    cutoff=self.recall_cutoff or None,
                    current_operation_id=self.operation_id,
                )
                return f"撤回结果：{self.last_recall_result.status}"
            case "send_sticker":
                await self._send(
                    MessageSegment.image(params["path"]),
                    surface="agent_action_sticker",
                )
                return "已发送表情包"
            case "send_qq_face":
                face_id = int(params["face_id"])
                text = guard_visible_text(params.get("text", ""), logger=self.logger, surface="qq_face_caption", allow_direct_media=False)
                message = MessageSegment.face(face_id)
                if text:
                    message += text
                await self._send(message, surface="agent_action_qq_expression")
                return "已发送 QQ 表情"
            case "send_qq_image_expression":
                url = str(params.get("url", "") or "").strip()
                if not url:
                    return "QQ 表情发送失败：缺少图片 URL"
                text = guard_visible_text(params.get("text", ""), logger=self.logger, surface="qq_image_caption", allow_direct_media=False)
                message = MessageSegment.image(url)
                if text:
                    message += text
                await self._send(message, surface="agent_action_qq_expression")
                return "已发送 QQ 图片表情"
            case "send_image_url":
                url = str(params.get("url", "") or "").strip()
                if not url:
                    return "图片发送失败：缺少图片 URL"
                text = guard_visible_text(params.get("text", ""), logger=self.logger, surface="image_caption", allow_direct_media=False)
                message = MessageSegment.image(url)
                if text:
                    message += text
                await self._send(message, surface="agent_action_image")
                return "已发送图片"
            case "send_qq_mface":
                data = params.get("data") if isinstance(params, dict) else {}
                if not isinstance(data, dict) or not data:
                    return "QQ mface 发送失败：缺少 mface 数据"
                text = guard_visible_text(params.get("text", ""), logger=self.logger, surface="mface_caption", allow_direct_media=False)
                message = MessageSegment("mface", data)
                if text:
                    message += text
                await self._send(message, surface="agent_action_qq_expression")
                return "已发送 QQ mface 表情"
            case "poke_user":
                await self._send(
                    MessageSegment("poke", {"qq": params["user_id"]}),
                    surface="agent_action_poke",
                )
                return "已戳"
            case _:
                self.logger.warning(f"[executor] unknown action: {action}")
                return f"未知 action: {action}"
