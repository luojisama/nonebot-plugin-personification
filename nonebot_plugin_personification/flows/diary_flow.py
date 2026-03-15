import random
import re
from typing import Any, Awaitable, Callable, Optional


def filter_sensitive_content(text: str) -> str:
    """过滤敏感词和过短文本。"""
    sensitive_patterns = [
        r"政治",
        r"民主",
        r"政府",
        r"主席",
        r"书记",
        r"国家",
        r"色情",
        r"做爱",
        r"淫秽",
        r"成人",
        r"福利姬",
        r"裸",
    ]

    filtered_text = text
    for pattern in sensitive_patterns:
        filtered_text = re.sub(pattern, "**", filtered_text, flags=re.IGNORECASE)

    if len(filtered_text.strip()) < 2:
        return ""
    return filtered_text


def clean_generated_text(text: str) -> str:
    """清理模型输出中的结构化标签。"""
    text = re.sub(r"<status.*?>.*?</\s*status\s*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think.*?>.*?</\s*think\s*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?\s*output.*?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?\s*message.*?>", "", text, flags=re.IGNORECASE)
    return text.strip()


async def get_recent_chat_context(bot: Any, logger: Any) -> str:
    """随机获取两个群的最近聊天记录作为周记素材。"""
    try:
        group_list = await bot.get_group_list()
        if not group_list:
            return ""

        sample_size = min(2, len(group_list))
        selected_groups = random.sample(group_list, sample_size)

        context_parts = []
        for group in selected_groups:
            group_id = group["group_id"]
            group_name = group.get("group_name", str(group_id))

            try:
                messages = await bot.get_group_msg_history(group_id=group_id, count=50)
                if not messages or "messages" not in messages:
                    continue

                chat_text = ""
                for msg in messages["messages"]:
                    sender_name = msg.get("sender", {}).get("nickname", "未知")
                    raw_msg = msg.get("message", "")
                    content = ""

                    if isinstance(raw_msg, list):
                        text_parts = []
                        for seg in raw_msg:
                            if not isinstance(seg, dict):
                                continue
                            if seg.get("type") != "text":
                                continue
                            seg_data = seg.get("data") or {}
                            text_parts.append(str(seg_data.get("text", "")))
                        content = "".join(text_parts)
                    elif isinstance(raw_msg, str):
                        content = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg)

                    safe_content = filter_sensitive_content(content)
                    if safe_content.strip():
                        chat_text += f"{sender_name}: {safe_content.strip()}\n"

                if chat_text:
                    context_parts.append(f"【群聊：{group_name} 的最近记录】\n{chat_text}")
            except Exception as e:
                logger.warning(f"获取群 {group_id} 历史记录失败: {e}")
                continue

        return "\n\n".join(context_parts)
    except Exception as e:
        logger.error(f"获取聊天上下文失败: {e}")
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


async def generate_ai_diary(
    bot: Any,
    *,
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    logger: Any,
) -> str:
    """让 AI 根据最近群聊或保底提示生成周记。"""
    system_prompt = load_prompt()
    chat_context = await get_recent_chat_context(bot, logger)

    base_requirements = (
        "1. 语气必须完全符合你的人设（绪山真寻：变成女初中生的宅男，语气笨拙、弱气、容易害羞）。\n"
        "2. 字数严格限制在 200 字以内。\n"
        "3. 直接输出日记内容，不要包含日期或其他无关文字。\n"
        "4. 严禁涉及任何政治、色情、暴力等违规内容。\n"
        "5. 严禁包含任何图片描述、[图片] 占位符或多媒体标记，只能是纯文字内容。"
    )

    if chat_context:
        rich_prompt = (
            "任务：请以日记的形式写一段简短的周记，记录你这一周在群里看到的趣事。\n"
            "素材：以下是最近群里的聊天记录（已脱敏），你可以参考其中的话题：\n"
            f"{chat_context}\n\n"
            f"要求：\n{base_requirements}"
        )
        rich_result = await _generate_once(
            system_prompt,
            rich_prompt,
            call_ai_api=call_ai_api,
        )
        if rich_result:
            return rich_result

        logger.warning("拟人插件：带素材的 AI 生成失败（可能触发了安全拦截），尝试保底模式。")

    basic_prompt = (
        "任务：请以日记的形式写一段简短的周记，记录你这一周的心情。\n"
        f"要求：\n{base_requirements}"
    )
    return await _generate_once(
        system_prompt,
        basic_prompt,
        call_ai_api=call_ai_api,
    )
