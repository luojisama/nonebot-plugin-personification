import asyncio
import random
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List


async def process_yaml_response_logic(
    bot: Any,
    event: Any,
    *,
    group_id: str,
    user_id: str,
    user_name: str,
    level_name: str,
    prompt_config: Dict[str, Any],
    chat_history: List[Dict[str, Any]],
    trigger_reason: str,
    get_beijing_time: Callable[[], Any],
    bot_statuses: Dict[str, str],
    get_group_config: Callable[[str], dict],
    plugin_config: Any,
    get_schedule_prompt_injection: Callable[[], str],
    schedule_disabled_override_prompt: Callable[[], str],
    build_grounding_context: Callable[[str], Any],
    call_ai_api: Callable[..., Any],
    parse_yaml_response: Callable[[str], Dict[str, Any]],
    message_segment_cls: Any,
    sanitize_history_text: Callable[[str], str],
    private_session_prefix: str,
    build_private_session_id: Callable[[str], str],
    build_group_session_id: Callable[[str], str],
    append_session_message: Callable[..., None],
    logger: Any,
) -> None:
    """处理基于 YAML 模板的新版响应逻辑。"""
    now = get_beijing_time()
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = (
        f"{now.year}年{now.month:02d}月{now.day:02d}日 "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) [东京时间 JST/UTC+9]"
    )

    current_status = bot_statuses.get(group_id)
    if not current_status:
        current_status = prompt_config.get("status", "").strip()
        if not current_status:
            current_status = '心情: "平静"\n状态: "正在潜水"\n记忆: ""\n动作: "发呆"'
        bot_statuses[group_id] = current_status

    history_new_text = ""
    recent_msgs = chat_history[:-1] if len(chat_history) > 1 else []
    for msg in recent_msgs:
        role = msg["role"]
        content = msg["content"]
        text_content = ""
        if isinstance(content, list):
            for item in content:
                if item["type"] == "text":
                    text_content += item["text"]
                elif item["type"] == "image_url":
                    if "[图片" not in text_content:
                        text_content += "[图片]"
        else:
            text_content = str(content)

        if role == "user":
            is_direct = msg.get("is_direct", True)
            if is_direct:
                history_new_text += f"{text_content}\n"
            else:
                history_new_text += f"{text_content}（群员间对话，非对你说）\n"
        elif role == "assistant":
            clean_content = re.sub(r" \[发送了表情包:.*?\]", "", text_content)
            history_new_text += f"[我]: {clean_content}\n"

    if not history_new_text:
        history_new_text = "(无最近消息)"

    last_msg = chat_history[-1]
    history_last_text = ""
    if isinstance(last_msg["content"], list):
        for item in last_msg["content"]:
            if item["type"] == "text":
                history_last_text += item["text"]
            elif item["type"] == "image_url":
                if "[图片" not in history_last_text:
                    history_last_text += "[图片]"
    else:
        history_last_text = str(last_msg["content"])

    system_prompt = prompt_config.get("system", "")
    is_private_session = str(group_id).startswith(private_session_prefix)
    if is_private_session:
        system_prompt += (
            "\n\n## 私聊称呼规则（高优先级）\n"
            "- 你在和单个用户对话，必须使用第二人称“你”。\n"
            "- 禁止使用“他/她/对方/这位用户”指代当前聊天对象。\n"
            "- 禁止出现“大家/你们/各位”这类群聊称呼。\n"
        )

    group_config = get_group_config(group_id)
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = plugin_config.personification_schedule_global
    schedule_active = schedule_enabled or global_schedule_enabled

    schedule_instruction = "2. **时间锚定**：参考【当前时间】保持时间语义正确（例如早晚问候、是否还在熬夜），但不受上课/睡觉等作息硬约束。"
    system_schedule_instruction = ""

    if schedule_active:
        system_schedule_instruction = get_schedule_prompt_injection()
        schedule_instruction = "2. **时间锚定**：参考【当前时间】判断作息状态。**作息状态仅作为回复的背景设定（占比约20%），主要精力应放在回应对方的内容上。**如果当前是上课或深夜（非休息时间），你回复了消息说明你正在“偷偷玩手机”或“熬夜”，请表现出这种紧张感或困意。"
    else:
        system_prompt = f"{schedule_disabled_override_prompt()}\n\n{system_prompt}"
        system_schedule_instruction = (
            "（⚠️ 作息模拟当前已关闭：此处及以下所有涉及时间、作息、上课、深夜的约束规则"
            "在本次对话中均不生效，请忽略并以正常方式对话。）"
        )

    system_prompt = system_prompt.replace("{system_schedule_instruction}", system_schedule_instruction)

    input_template = prompt_config.get("input", "")
    input_text = input_template.replace("{trigger_reason}", trigger_reason)
    input_text = input_text.replace("{time}", current_time_str)
    input_text = input_text.replace("{history_new}", history_new_text)
    input_text = input_text.replace("{history_last}", history_last_text)
    input_text = input_text.replace("{status}", current_status)
    input_text = input_text.replace("{schedule_instruction}", schedule_instruction)
    input_text = input_text.replace("{long_memory('guild')}", "(暂无长期记忆)")

    grounding_context = await build_grounding_context(history_last_text)
    if grounding_context:
        input_text = f"{input_text}\n\n## 联网事实校验（自动注入）\n{grounding_context}\n"

    if "{history_new}" not in input_template and "{history_last}" not in input_template:
        input_text = (
            f"{input_text}\n\n"
            f"## 最近对话上下文(自动注入)\n"
            f"- 最近历史:\n{history_new_text}\n"
            f"- 对方刚刚说:\n{history_last_text}\n"
        )

    user_content: Any = input_text
    last_images = []
    if isinstance(last_msg["content"], list):
        for item in last_msg["content"]:
            if item["type"] == "image_url":
                img_url_obj = item.get("image_url", {})
                if isinstance(img_url_obj, dict):
                    url = img_url_obj.get("url")
                    if url:
                        last_images.append(url)
                elif isinstance(img_url_obj, str):
                    last_images.append(img_url_obj)

    if last_images:
        user_content = [{"type": "text", "text": input_text}]
        for img_url in last_images:
            user_content.append({"type": "image_url", "image_url": {"url": img_url}})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    reply_content = await call_ai_api(messages)
    if not reply_content:
        logger.warning("拟人插件 (YAML): 未能获取到 AI 回复内容")
        return

    parsed = parse_yaml_response(reply_content)
    if "[SILENCE]" in reply_content:
        logger.info("AI (YAML) 决定保持沉默 (SILENCE)")
        return

    if parsed["status"]:
        bot_statuses[group_id] = parsed["status"]
        logger.info(f"拟人插件: 更新状态为: {parsed['status']}")
    if parsed["think"]:
        logger.debug(f"拟人插件: 思考过程: {parsed['think']}")

    if parsed["action"]:
        action_text = parsed["action"]
        logger.info(f"拟人插件: 执行动作: {action_text}")
        if "戳一戳" in action_text:
            try:
                await bot.send(event, message_segment_cls.poke(int(user_id)))
            except Exception as e:
                logger.warning(f"拟人插件: 发送戳一戳失败: {e}")

    clean_reply = ""
    if parsed["messages"]:
        for msg in parsed["messages"]:
            text = msg["text"]
            sticker_url = msg["sticker"]
            if text:
                text = text.replace("[SILENCE]", "").replace("[氛围好]", "").strip()

            if text:
                segments = re.split(r"([。！？\n])", text)
                merged_segments = []
                current_seg = ""
                for s in segments:
                    if s in "。！？\n":
                        current_seg += s
                        if current_seg.strip():
                            merged_segments.append(current_seg)
                        current_seg = ""
                    else:
                        current_seg += s
                if current_seg.strip():
                    merged_segments.append(current_seg)
                if not merged_segments and text.strip():
                    merged_segments = [text]

                for seg in merged_segments:
                    if seg.strip():
                        await bot.send(event, seg)
                        await asyncio.sleep(random.uniform(0.5, 2.0))

            if sticker_url:
                try:
                    if sticker_url.startswith("http"):
                        await bot.send(event, message_segment_cls.image(sticker_url))
                    else:
                        sticker_dir = Path(plugin_config.personification_sticker_path)
                        target_file = None
                        if sticker_dir.exists():
                            possible = sticker_dir / sticker_url
                            if possible.exists():
                                target_file = possible
                            else:
                                for f in sticker_dir.iterdir():
                                    if f.stem == sticker_url:
                                        target_file = f
                                        break
                        if target_file:
                            await bot.send(event, message_segment_cls.image(f"file:///{target_file.absolute()}"))
                        else:
                            logger.warning(f"拟人插件: 找不到表情包 {sticker_url}")
                except Exception as e:
                    logger.error(f"发送表情包失败: {e}")
    else:
        clean_reply = reply_content
        for tag in ["status", "think", "action", "output", "message"]:
            clean_reply = re.sub(rf"<{tag}.*?>.*?</\s*{tag}\s*>", "", clean_reply, flags=re.DOTALL | re.IGNORECASE)
            clean_reply = re.sub(rf"</?\s*{tag}.*?>", "", clean_reply, flags=re.IGNORECASE)
        clean_reply = clean_reply.replace("[SILENCE]", "").replace("[氛围好]", "").strip()
        if clean_reply:
            await bot.send(event, clean_reply)

    assistant_text = ""
    stickers_sent: List[str] = []
    if parsed["messages"]:
        text_parts = [m["text"] for m in parsed["messages"] if m["text"]]
        stickers_sent = [str(m["sticker"]) for m in parsed["messages"] if m.get("sticker")]
        assistant_text = sanitize_history_text(" ".join(text_parts).strip())
    else:
        assistant_text = sanitize_history_text(clean_reply)

    is_private_session = str(group_id).startswith(private_session_prefix)
    session_id = build_private_session_id(user_id) if is_private_session else build_group_session_id(group_id)
    legacy_session_id = None if is_private_session else group_id
    append_session_message(
        session_id,
        "assistant",
        assistant_text,
        legacy_session_id=legacy_session_id,
        scene="reply",
        sticker_sent=", ".join(stickers_sent) if stickers_sent else None,
    )


def build_yaml_response_processor(
    *,
    get_beijing_time: Callable[[], Any],
    bot_statuses: Dict[str, str],
    get_group_config: Callable[[str], dict],
    plugin_config: Any,
    get_schedule_prompt_injection: Callable[[], str],
    schedule_disabled_override_prompt: Callable[[], str],
    build_grounding_context: Callable[[str], Awaitable[str]],
    call_ai_api: Callable[..., Awaitable[Any]],
    parse_yaml_response: Callable[[str], Dict[str, Any]],
    message_segment_cls: Any,
    sanitize_history_text: Callable[[str], str],
    private_session_prefix: str,
    build_private_session_id: Callable[[str], str],
    build_group_session_id: Callable[[str], str],
    append_session_message: Callable[..., None],
    logger: Any,
) -> Callable[..., Awaitable[None]]:
    async def _processor(
        bot: Any,
        event: Any,
        group_id: str,
        user_id: str,
        user_name: str,
        level_name: str,
        prompt_config: Dict[str, Any],
        chat_history: List[Dict[str, Any]],
        trigger_reason: str = "",
    ) -> None:
        return await process_yaml_response_logic(
            bot,
            event,
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            level_name=level_name,
            prompt_config=prompt_config,
            chat_history=chat_history,
            trigger_reason=trigger_reason,
            get_beijing_time=get_beijing_time,
            bot_statuses=bot_statuses,
            get_group_config=get_group_config,
            plugin_config=plugin_config,
            get_schedule_prompt_injection=get_schedule_prompt_injection,
            schedule_disabled_override_prompt=schedule_disabled_override_prompt,
            build_grounding_context=build_grounding_context,
            call_ai_api=call_ai_api,
            parse_yaml_response=parse_yaml_response,
            message_segment_cls=message_segment_cls,
            sanitize_history_text=sanitize_history_text,
            private_session_prefix=private_session_prefix,
            build_private_session_id=build_private_session_id,
            build_group_session_id=build_group_session_id,
            append_session_message=append_session_message,
            logger=logger,
        )

    return _processor
