from __future__ import annotations

import re
import time
from typing import Any

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg


def setup_persona_matchers(
    *,
    persona_store: Any,
    whitelist_rule: Any,
    superusers: set[str] | None,
    logger: Any,
) -> dict[str, Any]:
    view_cmd = on_command("查看画像", rule=whitelist_rule, priority=5, block=True)
    refresh_cmd = on_command("刷新画像", rule=whitelist_rule, priority=5, block=True)
    msg_recorder = on_message(rule=whitelist_rule, priority=99, block=False)

    driver_config = get_driver().config

    @msg_recorder.handle()
    async def _record(event: MessageEvent) -> None:
        text = event.get_plaintext().strip()
        if not text:
            return
        command_starts = getattr(driver_config, "command_start", {"/", ""})
        if any(text.startswith(start) for start in command_starts if start):
            return
        await persona_store.record_message(str(event.user_id), text)

    @view_cmd.handle()
    async def _view(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
        target_id = _extract_target_id(event, args)
        if not _can_access_target_persona(str(event.user_id), target_id, superusers):
            await view_cmd.finish("只能查看自己的画像。")
        entry = persona_store.get_persona(target_id)
        if not entry:
            count = persona_store.get_history_count(target_id)
            await view_cmd.finish(
                f"该用户暂无画像。当前已记录 {count}/{persona_store.history_max} 条消息。"
            )
        update_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.time))
        await _send_persona_forward(
            bot,
            event,
            target_id,
            entry.data,
            update_time,
            logger=logger,
        )
        await view_cmd.finish()

    @refresh_cmd.handle()
    async def _refresh(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
        target_id = _extract_target_id(event, args)
        if not _can_access_target_persona(str(event.user_id), target_id, superusers):
            await refresh_cmd.finish("只能刷新自己的画像。")
        history_count = persona_store.get_history_count(target_id)
        if history_count == 0:
            await refresh_cmd.finish("当前没有任何聊天记录，无法刷新画像。")
        await refresh_cmd.send(f"正在根据当前 {history_count} 条记录生成画像，请稍候...")
        entry = await persona_store.force_refresh(target_id)
        if not entry:
            await refresh_cmd.finish("画像刷新失败，请检查 API 配置或网络。")
        update_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.time))
        await _send_persona_forward(
            bot,
            event,
            target_id,
            entry.data,
            update_time,
            logger=logger,
        )
        await refresh_cmd.finish()

    return {
        "persona_view_matcher": view_cmd,
        "persona_refresh_matcher": refresh_cmd,
        "persona_recorder": msg_recorder,
    }


def _extract_target_id(event: MessageEvent, args: Message) -> str:
    for seg in event.get_message():
        if seg.type == "at":
            return str(seg.data["qq"])
    arg_text = args.extract_plain_text().strip()
    if arg_text.isdigit():
        return arg_text
    return str(event.user_id)


def _can_access_target_persona(
    operator_user_id: str,
    target_user_id: str,
    superusers: set[str] | None,
) -> bool:
    if operator_user_id == target_user_id:
        return True
    if not superusers:
        return False
    return operator_user_id in {str(item) for item in superusers}


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{1,3}(.+?)_{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"`{1,3}[^`\n]*`{1,3}", "", text)
    return text.strip()


async def _send_persona_forward(
    bot: Bot,
    event: MessageEvent,
    target_id: str,
    persona_text: str,
    update_time: str,
    *,
    logger: Any,
) -> None:
    clean_text = _strip_markdown(persona_text)
    lines = [line.strip() for line in clean_text.splitlines() if line.strip()]
    grouped: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("【") and current:
            grouped.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        grouped.append("\n".join(current))

    bot_id = str(bot.self_id)
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "画像系统",
                "uin": bot_id,
                "content": f"用户 {target_id} 的画像分析\n更新时间：{update_time}",
            },
        }
    ]
    for section in grouped:
        if section.strip():
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": "画像系统",
                        "uin": bot_id,
                        "content": section,
                    },
                }
            )

    try:
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "send_group_forward_msg",
                group_id=event.group_id,
                messages=nodes,
            )
        else:
            await bot.call_api(
                "send_private_forward_msg",
                user_id=event.user_id,
                messages=nodes,
            )
    except Exception as e:
        logger.error(f"[user_persona] 转发消息发送失败，回退到普通消息: {e}")
        await bot.send(event, f"用户 {target_id} 的画像分析（{update_time}）：\n\n{clean_text}")
