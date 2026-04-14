import asyncio
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

from ..core.message_relations import extract_send_message_id
from ..core.message_parts import build_user_message_content
from ..core.tts_service import extract_persona_tts_config

from ..agent.action_executor import ActionExecutor
from ..agent.loop import run_agent
from ..skills.skillpacks.sticker_tool.scripts.impl import (
    reset_current_image_context,
    set_current_image_context,
)
from ..utils import get_group_topic_summary


_TRANSLATION_LINE_SUFFIX = r"\s*\d*(?:\s*[（(][^）)]*[）)])*\s*[：:]"
_TRANSLATION_SOURCE_RE = re.compile(
    rf"^原文{_TRANSLATION_LINE_SUFFIX}",
    re.IGNORECASE | re.MULTILINE,
)
_TRANSLATION_TARGET_RE = re.compile(
    rf"^译文{_TRANSLATION_LINE_SUFFIX}",
    re.IGNORECASE | re.MULTILINE,
)
_STATUS_MAX_AGE_SECONDS = 30 * 60


def _status_period_key(target_time: Any) -> str:
    hour = int(getattr(target_time, "hour", 0) or 0)
    if 0 <= hour < 6:
        return "late_night"
    if 6 <= hour < 9:
        return "morning"
    if 9 <= hour < 12:
        return "forenoon"
    if 12 <= hour < 14:
        return "noon"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"


def _build_time_anchored_default_status(now: Any) -> str:
    hour = int(getattr(now, "hour", 0) or 0)
    if 0 <= hour < 6:
        mood, state, action = "困", "深夜了，应该快睡着了", "揉眼睛"
    elif 6 <= hour < 9:
        mood, state, action = "懵", "刚起床，还没完全清醒", "伸懒腰"
    elif 9 <= hour < 12:
        mood, state, action = "平静", "上午时段，正慢慢进入状态", "发呆"
    elif 12 <= hour < 14:
        mood, state, action = "放松", "中午休息时间", "吃饭"
    elif 14 <= hour < 18:
        mood, state, action = "平静", "下午时段", "摸鱼"
    elif 18 <= hour < 22:
        mood, state, action = "悠闲", "晚上在家，比较放松", "休息"
    else:
        mood, state, action = "困", "夜深了，准备休息", "打哈欠"
    return f'心情: "{mood}"\n状态: "{state}"\n记忆: ""\n动作: "{action}"'


def _get_current_status(
    group_id: str,
    bot_statuses: Dict[str, Any],
    prompt_config: Dict[str, Any],
    now: Any,
) -> str:
    now_ts = time.time()
    current_period = _status_period_key(now)
    entry = bot_statuses.get(group_id)
    if isinstance(entry, dict):
        status_text = str(entry.get("status", "") or "").strip()
        updated_at = float(entry.get("updated_at", 0) or 0)
        previous_period = str(entry.get("period_key", "") or "")
        if (
            status_text
            and now_ts - updated_at <= _STATUS_MAX_AGE_SECONDS
            and (not previous_period or previous_period == current_period)
        ):
            return status_text
    elif isinstance(entry, str):
        status_text = entry.strip()
        if status_text:
            bot_statuses[group_id] = {
                "status": status_text,
                "updated_at": now_ts,
                "period_key": current_period,
            }
            return status_text

    base_status = str(prompt_config.get("status", "") or "").strip()
    current_status = base_status or _build_time_anchored_default_status(now)
    bot_statuses[group_id] = {
        "status": current_status,
        "updated_at": now_ts,
        "period_key": current_period,
    }
    return current_status


def _looks_like_translation_result(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if "未识别到可翻译文字" in raw:
        return True
    return bool(_TRANSLATION_SOURCE_RE.search(raw) and _TRANSLATION_TARGET_RE.search(raw))


def _group_translation_result(text: str) -> List[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if "未识别到可翻译文字" in raw:
        return ["未识别到可翻译文字"]

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return []

    grouped: List[str] = []
    current: List[str] = []
    for line in lines:
        if _TRANSLATION_SOURCE_RE.match(line):
            if current:
                grouped.append("\n".join(current))
            current = [line]
            continue
        if _TRANSLATION_TARGET_RE.match(line):
            if not current:
                current = [line]
            else:
                current.append(line)
            continue
        if current:
            current.append(line)
        else:
            grouped.append(line)
    if current:
        grouped.append("\n".join(current))

    if grouped:
        return grouped
    return [raw]


async def _send_translation_forward(bot: Any, event: Any, text: str) -> bool:
    grouped = _group_translation_result(text)
    if not grouped:
        return False

    bot_id = str(getattr(bot, "self_id", "") or "0")
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "漫画翻译",
                "uin": bot_id,
                "content": f"漫画翻译结果（共 {len(grouped)} 条）",
            },
        }
    ]
    for index, content in enumerate(grouped, start=1):
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": f"第{index}条",
                    "uin": bot_id,
                    "content": content,
                },
            }
        )

    if hasattr(event, "group_id"):
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    else:
        await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
    return True


def _strip_control_markers(text: str) -> str:
    cleaned = str(text or "")
    cleaned = (
        cleaned.replace("[SILENCE]", "").replace("<SILENCE>", "")
        .replace("[氛围好]", "").replace("<氛围好>", "")
        .replace("[BLOCK]", "").replace("<BLOCK>", "")
        .replace("[NO_REPLY]", "").replace("<NO_REPLY>", "")
        .strip()
    )
    cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)
    cleaned = re.sub(r'<[^>]*>', '', cleaned)
    return cleaned.strip()


def _build_tts_user_hint(*, is_private: bool) -> str:
    scene = "私聊" if is_private else "群聊"
    return f"这是{scene}场景下的回复，请自然朗读，整体语速略快一点。"


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
    get_current_time: Callable[[], Any],
    format_time_context: Callable[[Any | None], str],
    bot_statuses: Dict[str, Any],
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
    record_group_msg: Callable[..., Any] | None,
    logger: Any,
    user_blacklist: Dict[str, float],
    superusers: set[str] | None = None,
    tool_registry: Any = None,
    agent_tool_caller: Any = None,
    current_image_urls: List[str] | None = None,
    tts_service: Any = None,
    extract_forward_content: Callable[..., Any] = None,
    memory_curator: Any = None,
) -> None:
    """处理基于 YAML 模板的新版响应逻辑。"""
    now = get_current_time()
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = (
        f"{now.year}年{now.month:02d}月{now.day:02d}日 "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) "
        f"[{format_time_context(now)}]"
    )

    current_status = _get_current_status(group_id, bot_statuses, prompt_config, now)

    forward_content = ""
    if extract_forward_content is not None:
        try:
            forward_content = await extract_forward_content(bot, event, logger=logger)
        except Exception as e:
            logger.warning(f"拟人插件：提取转发消息内容失败: {e}")

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

    topic_hint = ""
    if not is_private_session:
        topic_hint = get_group_topic_summary(group_id)
    grounding_context = await build_grounding_context(history_last_text, topic_hint)
    if grounding_context:
        input_text = f"{input_text}\n\n## 联网事实校验（自动注入）\n{grounding_context}\n"

    if forward_content:
        forward_content = forward_content[:2000] if len(forward_content) > 2000 else forward_content
        input_text = (
            f"{input_text}\n\n"
            f"## 聊天记录内容（用户转发的聊天记录）\n"
            f"{forward_content}\n"
            f"（请理解并回应转发内容中的话题，如有需要可结合联网搜索验证信息）\n"
        )

    if "{history_new}" not in input_template and "{history_last}" not in input_template:
        input_text = (
            f"{input_text}\n\n"
            f"## 最近对话上下文(自动注入)\n"
            f"- 最近历史:\n{history_new_text}\n"
            f"- 对方刚刚说:\n{history_last_text}\n"
        )

    user_content: Any = input_text
    last_images = list(current_image_urls or [])
    if isinstance(last_msg["content"], list):
        for item in last_msg["content"]:
            if item["type"] == "image_url":
                img_url_obj = item.get("image_url", {})
                if isinstance(img_url_obj, dict):
                    url = img_url_obj.get("url")
                    if url and url not in last_images:
                        last_images.append(url)
                elif isinstance(img_url_obj, str) and img_url_obj not in last_images:
                    last_images.append(img_url_obj)

    if last_images:
        user_content = build_user_message_content(
            text=input_text,
            image_urls=last_images,
        )
        system_prompt += (
            "\n\n## 图片处理规则（重要）\n"
            "1. 你正在看用户发送的图片，但你仍然是「你自己」，绝对不能代入图片中的角色。\n"
            "2. 禁止以图片中角色的口吻说话，禁止扮演图片中的任何人物或角色。\n"
            "3. 你应该以旁观者的身份描述或评论图片内容，而不是成为图片中的人。\n"
            "4. 如果图片是动漫/游戏角色，你只是「看到」了这张图片，你不是那个角色。\n"
            "5. 始终保持你自己的身份和人格，用你自己的语气来回应图片。\n"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    used_agent = False
    if (
        getattr(plugin_config, "personification_agent_enabled", True)
        and tool_registry
        and agent_tool_caller
    ):
        executor = ActionExecutor(bot, event, plugin_config, logger)
        image_ctx_token = set_current_image_context(last_images, input_text)
        try:
            agent_result = await run_agent(
                messages=messages,
                registry=tool_registry,
                tool_caller=agent_tool_caller,
                executor=executor,
                plugin_config=plugin_config,
                logger=logger,
                max_steps=getattr(plugin_config, "personification_agent_max_steps", 5),
                current_image_urls=last_images,
            )
        finally:
            reset_current_image_context(image_ctx_token)
        reply_content = agent_result.text
        used_agent = True
        for action in agent_result.pending_actions:
            await executor.execute(action["type"], action["params"])
        if agent_result.direct_output:
            raw_direct_output = str(reply_content or "").strip()
            if _looks_like_translation_result(raw_direct_output):
                try:
                    if await _send_translation_forward(bot, event, raw_direct_output):
                        return
                except Exception as e:
                    logger.warning(f"拟人插件: 翻译结果转发发送失败，回退到普通消息: {e}")
            for seg in re.split(r"(?:\r?\n){2,}", raw_direct_output):
                text = seg.strip()
                if text:
                    await bot.send(event, text)
                    await asyncio.sleep(random.uniform(0.5, 1.2))
            return
    else:
        reply_content = await call_ai_api(messages)
    if not reply_content:
        if used_agent:
            logger.warning("拟人插件 (YAML): Agent 执行完成但返回空文本，请检查上方 [agent] provider 日志")
        else:
            logger.warning("拟人插件 (YAML): 未能获取到 AI 回复内容")
        return
    if used_agent and reply_content in ("[NO_REPLY]", "<NO_REPLY>"):
        return

    parsed = parse_yaml_response(reply_content)
    has_block_marker = "[BLOCK]" in reply_content or "<BLOCK>" in reply_content

    # 增加双重校验：如果 AI 输出虽然包含 [BLOCK]，但实际输出的内容表示它是客观事实/新闻，或者没有包含违规理由，则可以忽略拦截
    think_content = parsed.get("think", "")
    is_news_or_fact = False
    if think_content:
        # 如果模型思考过程中明确认为是新闻、事实、客观分享，则放行
        if any(keyword in think_content for keyword in ["新闻", "实事", "客观", "事实", "突发事件", "分享", "报道"]):
            is_news_or_fact = True

    if has_block_marker and not is_news_or_fact:
        reply_content = reply_content.replace("[BLOCK]", "").strip()
        logger.warning(f"AI (YAML) 检测到高风险标记，当前仅跳过本轮回复: {group_id} {user_name}({user_id})")
        notify_superusers = superusers or set()
        if notify_superusers:
            notify_msg = (
                "拟人插件高风险提示\n"
                f"群：{group_id}\n"
                f"用户：{user_name}（{user_id}）\n"
                f"拦截内容：{history_last_text[:80]}\n"
                f"时间：{get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "处理：已跳过本轮回复，未自动拉黑。"
            )

            async def _notify_superusers(
                _bot: Any = bot,
                _superusers: set[str] = notify_superusers,
                _msg: str = notify_msg,
            ) -> None:
                for _su in _superusers:
                    try:
                        await _bot.send_private_msg(user_id=int(_su), message=_msg)
                    except Exception as _e:
                        logger.warning(f"[BLOCK] 通知管理员 {_su} 失败: {_e}")

            asyncio.create_task(_notify_superusers())
        return
    if "[SILENCE]" in reply_content or "<SILENCE>" in reply_content:
        logger.info("AI (YAML) 决定保持沉默 (SILENCE)")
        return

    if parsed["status"]:
        bot_statuses[group_id] = {
            "status": parsed["status"],
            "updated_at": time.time(),
            "period_key": _status_period_key(get_current_time()),
        }
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

    assistant_text = ""
    stickers_sent: List[str] = []
    if parsed["messages"]:
        text_parts = [_strip_control_markers(m["text"]) for m in parsed["messages"] if m["text"]]
        stickers_sent = [str(m["sticker"]) for m in parsed["messages"] if m.get("sticker")]
        assistant_text = sanitize_history_text(" ".join(text_parts).strip())
    else:
        clean_reply = reply_content
        for tag in ["status", "think", "action", "output", "message"]:
            clean_reply = re.sub(rf"<{tag}.*?>.*?</\s*{tag}\s*>", "", clean_reply, flags=re.DOTALL | re.IGNORECASE)
            clean_reply = re.sub(rf"</?\s*{tag}.*?>", "", clean_reply, flags=re.IGNORECASE)
        clean_reply = _strip_control_markers(clean_reply)
        assistant_text = sanitize_history_text(clean_reply)

    assistant_text = re.sub(r"^(根据你的描述|总的来说|总体来说)[，,:：\s]*", "", assistant_text).strip()
    assistant_text = re.sub(r"^(如果你需要|如果需要的话)[，,:：\s]*", "", assistant_text).strip()
    assistant_text = re.sub(r"(?:如果你需要|需要的话).*?$", "", assistant_text).strip()

    is_private_session = str(group_id).startswith(private_session_prefix)
    sent_as_tts = False
    sent_message_id = ""
    if (
        assistant_text
        and not stickers_sent
        and tts_service is not None
        and tts_service.should_auto_tts(
            is_private=is_private_session,
            group_config=group_config,
            text=assistant_text,
            has_rich_content=False,
        )
    ):
        try:
            sent_as_tts = await tts_service.send_tts(
                bot=bot,
                event=event,
                message_segment_cls=message_segment_cls,
                text=assistant_text,
                user_hint=_build_tts_user_hint(is_private=is_private_session),
                is_private=is_private_session,
                persona_tts=extract_persona_tts_config(prompt_config),
                pause_range=(0.8, 1.5),
            )
        except Exception as e:
            logger.warning(f"[tts] YAML 自动语音发送失败，回退文字: {e}")

    if not sent_as_tts:
        clean_reply = ""
        if parsed["messages"]:
            for msg in parsed["messages"]:
                text = msg["text"]
                sticker_url = msg["sticker"]
                if text:
                    text = _strip_control_markers(text)

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
                            send_result = await bot.send(event, seg)
                            if not sent_message_id:
                                sent_message_id = extract_send_message_id(send_result)
                            await asyncio.sleep(random.uniform(0.4, 1.0))

                if sticker_url:
                    try:
                        if sticker_url.startswith("http"):
                            send_result = await bot.send(event, message_segment_cls.image(sticker_url))
                            if not sent_message_id:
                                sent_message_id = extract_send_message_id(send_result)
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
                                send_result = await bot.send(event, message_segment_cls.image(f"file:///{target_file.absolute()}"))
                                if not sent_message_id:
                                    sent_message_id = extract_send_message_id(send_result)
                            else:
                                logger.warning(f"拟人插件: 找不到表情包 {sticker_url}")
                    except Exception as e:
                        logger.error(f"发送表情包失败: {e}")
        else:
            clean_reply = reply_content
            for tag in ["status", "think", "action", "output", "message"]:
                clean_reply = re.sub(rf"<{tag}.*?>.*?</\s*{tag}\s*>", "", clean_reply, flags=re.DOTALL | re.IGNORECASE)
                clean_reply = re.sub(rf"</?\s*{tag}.*?>", "", clean_reply, flags=re.IGNORECASE)
            clean_reply = _strip_control_markers(clean_reply)
            if clean_reply:
                send_result = await bot.send(event, clean_reply)
                if not sent_message_id:
                    sent_message_id = extract_send_message_id(send_result)

    session_id = build_private_session_id(user_id) if is_private_session else build_group_session_id(group_id)
    legacy_session_id = None if is_private_session else group_id
    # YAML 模式同样只写回最终用户可见文本，避免 session 中保留未发送的原始模板输出。
    append_session_message(
        session_id,
        "assistant",
        assistant_text,
        legacy_session_id=legacy_session_id,
        scene="reply",
        sticker_sent=", ".join(stickers_sent) if stickers_sent else None,
        speaker=str(getattr(bot, "self_id", "") or "bot"),
        user_id=str(getattr(bot, "self_id", "") or "") or None,
        source_kind="bot_reply",
        group_id=None if is_private_session else group_id,
        message_id=sent_message_id or None,
        reply_to_msg_id=str(getattr(event, "message_id", "") or "") or None,
        reply_to_user_id=None if is_private_session else user_id,
        mentioned_ids=[],
        is_at_bot=False,
    )
    if memory_curator is not None:
        memory_curator.schedule_capture(
            summary=assistant_text,
            user_id=user_id,
            group_id="" if is_private_session else group_id,
            topic_tags=[group_id] if not is_private_session else [],
        )
    if not is_private_session and record_group_msg is not None:
        record_group_msg(
            group_id,
            str(getattr(bot, "self_id", "") or "bot"),
            assistant_text,
            is_bot=True,
            user_id=str(getattr(bot, "self_id", "") or ""),
            message_id=sent_message_id or None,
            reply_to_msg_id=str(getattr(event, "message_id", "") or "") or None,
            reply_to_user_id=user_id,
            source_kind="bot_reply",
        )


def build_yaml_response_processor(
    *,
    get_current_time: Callable[[], Any],
    format_time_context: Callable[[Any | None], str],
    bot_statuses: Dict[str, Any],
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
    record_group_msg: Callable[..., Any] | None,
    logger: Any,
    user_blacklist: Dict[str, float],
    superusers: set[str] | None = None,
    tool_registry: Any = None,
    agent_tool_caller: Any = None,
    tts_service: Any = None,
    extract_forward_content: Callable[..., Any] = None,
    memory_curator: Any = None,
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
        current_image_urls: List[str] | None = None,
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
            get_current_time=get_current_time,
            format_time_context=format_time_context,
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
            record_group_msg=record_group_msg,
            logger=logger,
            user_blacklist=user_blacklist,
            superusers=superusers,
            tool_registry=tool_registry,
            agent_tool_caller=agent_tool_caller,
            current_image_urls=current_image_urls,
            tts_service=tts_service,
            extract_forward_content=extract_forward_content,
            memory_curator=memory_curator,
        )

    return _processor
