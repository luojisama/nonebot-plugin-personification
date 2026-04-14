from typing import Any, Awaitable, Callable, Dict, List, Optional


async def analyze_group_style(
    group_id: str,
    *,
    get_recent_group_msgs: Callable[[str, int], List[Dict[str, Any]]],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    limit: int = 300,
    provider_api_type: str = "",
) -> Optional[str]:
    """根据群聊历史提取群风格描述。"""
    msgs = get_recent_group_msgs(group_id, limit)
    if not msgs:
        return None

    chat_content: List[Dict[str, Any]] = []
    image_count = 0
    api_type = str(provider_api_type or "").strip().lower()
    allow_image_blocks = api_type != "openai_codex"

    for msg in msgs:
        if msg.get("is_bot"):
            continue
        nickname = str(msg.get("nickname", "未知"))
        content = str(msg.get("content", ""))
        if not content.strip():
            continue
        chat_content.append({"type": "text", "text": f"({nickname}): {content}\n"})

        images = msg.get("images") or []
        if not isinstance(images, list):
            continue

        for img_b64 in images:
            if len(chat_content) >= 50 or image_count >= 10:
                break
            if allow_image_blocks:
                chat_content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}",
                        },
                    }
                )
            else:
                chat_content.append({"type": "text", "text": f"({nickname}): [图片]\n"})
            image_count += 1

    prompt_text = (
        "你是一个群聊风格分析师。请根据以下群聊记录（包含部分图片），总结该群的聊天风格。\n"
        "请包含以下几个方面：\n"
        "1. 整体氛围（如：轻松、严肃、二次元、搞怪等）\n"
        "2. 常用梗或黑话（如果有）\n"
        "3. 成员互动方式（如：互损、互夸、复读等）\n"
        "4. 语言特色（如：口癖、表情包使用习惯等）\n\n"
        "请输出一段精简的描述（200字以内），用于指导 AI 融入该群聊。\n"
        "格式要求：直接输出描述内容，不要包含其他客套话。\n\n"
        "## 聊天记录开始\n"
    )

    chat_content.insert(0, {"type": "text", "text": prompt_text})
    chat_content.append({"type": "text", "text": "\n## 聊天记录结束"})

    messages = [{"role": "user", "content": chat_content}]
    return await call_ai_api(messages, temperature=0.7)
