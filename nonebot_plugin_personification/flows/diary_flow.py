from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..agent.inner_state import update_state_from_diary


def filter_sensitive_content(text: str) -> str:
    """Filter obviously unsafe fragments from sampled group history."""
    sensitive_patterns = [
        r"自杀",
        r"跳楼",
        r"毒品",
        r"开盒",
        r"爆照",
        r"约炮",
        r"政治敏感",
        r"血腥",
        r"色情",
    ]

    filtered_text = text
    for pattern in sensitive_patterns:
        filtered_text = re.sub(pattern, "**", filtered_text, flags=re.IGNORECASE)

    if len(filtered_text.strip()) < 2:
        return ""
    return filtered_text


def clean_generated_text(text: str) -> str:
    """Strip model-side thinking/status wrappers."""
    cleaned = re.sub(r"<status.*?>.*?</\s*status\s*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think.*?>.*?</\s*think\s*>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"</?\s*output.*?>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?\s*message.*?>", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


async def get_recent_chat_context(bot: Any, logger: Any) -> str:
    """Sample recent group messages as diary context."""
    try:
        group_list = await bot.get_group_list()
        if not group_list:
            return ""

        selected_groups = random.sample(group_list, min(2, len(group_list)))
        context_parts = []
        for group in selected_groups:
            group_id = group["group_id"]
            group_name = group.get("group_name", str(group_id))

            try:
                messages = await bot.get_group_msg_history(group_id=group_id, count=50)
            except Exception as e:
                logger.warning(f"[diary] get group history failed: {group_id}: {e}")
                continue

            if not messages or "messages" not in messages:
                continue

            lines = []
            for msg in messages["messages"]:
                sender_name = msg.get("sender", {}).get("nickname", "未知")
                raw_msg = msg.get("message", "")
                content = ""

                if isinstance(raw_msg, list):
                    text_parts = []
                    for seg in raw_msg:
                        if isinstance(seg, dict) and seg.get("type") == "text":
                            text_parts.append(str((seg.get("data") or {}).get("text", "")))
                    content = "".join(text_parts)
                elif isinstance(raw_msg, str):
                    content = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg)

                safe_content = filter_sensitive_content(content)
                if safe_content.strip():
                    lines.append(f"{sender_name}: {safe_content.strip()}")

            if lines:
                context_parts.append(f"群聊 {group_name} 的最近聊天：\n" + "\n".join(lines))

        return "\n\n".join(context_parts)
    except Exception as e:
        logger.error(f"[diary] get recent chat context failed: {e}")
        return ""


async def _generate_once(
    system_prompt: Any,
    user_prompt: str,
    *,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    result = await call_ai_api(messages)
    if not result:
        return ""
    return clean_generated_text(result)


def _schedule_diary_state_update(
    *,
    diary_text: str,
    tool_caller: Any,
    data_dir: Optional[Path],
    logger: Any,
) -> None:
    if not diary_text or tool_caller is None or data_dir is None:
        return
    asyncio.create_task(
        update_state_from_diary(
            diary_text,
            Path(data_dir),
            tool_caller,
            logger,
        )
    )


async def generate_ai_diary(
    bot: Any,
    *,
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    logger: Any,
    tool_caller: Any = None,
    data_dir: Optional[Path] = None,
) -> str:
    """Generate a weekly diary entry from recent chat context."""
    system_prompt = load_prompt()
    chat_context = await get_recent_chat_context(bot, logger)

    base_requirements = (
        "请写一篇自然、像真人发动态一样的周记。\n"
        "1. 语气要符合当前角色设定，不要像总结报告。\n"
        "2. 长度控制在 80 到 200 字之间。\n"
        "3. 可以写聊天里看到的趣事、自己的心情、最近在意的小事。\n"
        "4. 不要暴露这是 AI 生成，也不要列条目。\n"
        "5. 直接输出正文，不要加标题、标签或额外说明。"
    )

    if chat_context:
        rich_prompt = (
            "请结合下面这些最近聊天内容，写一篇带一点生活感的周记。\n"
            "不要逐条复述聊天记录，而是把它们消化成自己的感受、吐槽或碎碎念。\n\n"
            f"{chat_context}\n\n"
            f"{base_requirements}"
        )
        rich_result = await _generate_once(
            system_prompt,
            rich_prompt,
            call_ai_api=call_ai_api,
        )
        if rich_result:
            _schedule_diary_state_update(
                diary_text=rich_result,
                tool_caller=tool_caller,
                data_dir=data_dir,
                logger=logger,
            )
            return rich_result

        logger.warning("[diary] rich prompt generation failed, fallback to basic prompt")

    basic_prompt = (
        "请直接写一篇自然的短周记，像是角色自己随手发的碎碎念。\n\n"
        f"{base_requirements}"
    )
    result = await _generate_once(
        system_prompt,
        basic_prompt,
        call_ai_api=call_ai_api,
    )
    if result:
        _schedule_diary_state_update(
            diary_text=result,
            tool_caller=tool_caller,
            data_dir=data_dir,
            logger=logger,
        )
    return result
