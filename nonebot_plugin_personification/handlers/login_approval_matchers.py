"""WebUI 登录请求的 QQ 私聊批准命令。

bot 在管理员私聊里发出登录请求后，管理员可直接回复：
- 「同意登录 <4位码>」或「同意登录」（取最近一条）→ 批准
- 「拒绝登录 <4位码>」或「拒绝登录」→ 拒绝

仅在私聊生效（handler 形参标注 PrivateMessageEvent）。批准/拒绝只会作用于
该 QQ 自己创建的待处理登录请求，因此无需额外权限校验——能创建请求的本就是
通过 /login 管理员校验的账号。
"""

from __future__ import annotations

from typing import Any

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.params import CommandArg

from ..core import webui_auth_store


def register_login_approval_matchers(*, logger: Any = None) -> dict[str, Any]:
    approve = on_command("同意登录", aliases={"登录同意", "批准登录"}, priority=4, block=True)
    deny = on_command("拒绝登录", aliases={"登录拒绝", "驳回登录"}, priority=4, block=True)

    @approve.handle()
    async def _approve(event: PrivateMessageEvent, args: Message = CommandArg()) -> None:
        qq = str(event.user_id)
        code = args.extract_plain_text().strip() or None
        req = webui_auth_store.approve_login_request(qq, code)
        if req:
            if logger is not None:
                logger.info(f"拟人插件：WebUI 登录请求已由 {qq} 在私聊批准。")
            await approve.finish("✅ 已同意本次 WebUI 登录，网页将自动进入。")
        await approve.finish("没有待确认的 WebUI 登录请求（可能已过期或已处理）。")

    @deny.handle()
    async def _deny(event: PrivateMessageEvent, args: Message = CommandArg()) -> None:
        qq = str(event.user_id)
        code = args.extract_plain_text().strip() or None
        req = webui_auth_store.deny_login_request(qq, code)
        if req:
            if logger is not None:
                logger.info(f"拟人插件：WebUI 登录请求已由 {qq} 在私聊拒绝。")
            await deny.finish("已拒绝本次 WebUI 登录请求。")
        await deny.finish("没有待确认的 WebUI 登录请求。")

    return {"approve_login": approve, "deny_login": deny}


__all__ = ["register_login_approval_matchers"]
