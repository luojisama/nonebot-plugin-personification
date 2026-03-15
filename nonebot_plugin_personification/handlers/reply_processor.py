import asyncio
import base64
import json
import random
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List

import aiofiles
import httpx
from nonebot.exception import FinishedException
from PIL import Image


@dataclass
class SessionDeps:
    private_session_prefix: str
    looks_like_private_command: Callable[[str], bool]
    ensure_session_history: Callable[..., None]
    build_private_session_id: Callable[[str], str]
    build_group_session_id: Callable[[str], str]
    sanitize_session_messages: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]
    get_session_messages: Callable[[str], List[Dict[str, Any]]]
    append_session_message: Callable[..., None]
    sanitize_history_text: Callable[[str], str]
    build_private_anti_loop_hint: Callable[[List[Dict[str, Any]]], str]


@dataclass
class PersonaDeps:
    load_prompt: Callable[[str], Any]
    sign_in_available: bool
    get_user_data: Callable[[str], Dict[str, Any]]
    get_level_name: Callable[[float], str]
    update_user_data: Callable[..., None]
    get_group_config: Callable[[str], Dict[str, Any]]
    get_group_style: Callable[[str], str]
    favorability_attitudes: Dict[str, str]
    get_custom_title: Callable[[str], str]
    default_bot_nickname: str


@dataclass
class RuntimeDeps:
    is_msg_processed: Callable[[int], bool]
    logger: Any
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]]
    should_avoid_interrupting: Callable[[str, bool], bool]
    module_instance_id: int
    process_yaml_response_logic: Callable[..., Any]
    plugin_config: Any
    get_beijing_time: Callable[[], Any]
    schedule_disabled_override_prompt: Callable[[], str]
    get_schedule_prompt_injection: Callable[[], str]
    build_grounding_context: Callable[[str], Any]
    update_private_interaction_time: Callable[[str], None]
    call_ai_api: Callable[..., Any]
    user_blacklist: Dict[str, float]
    record_group_msg: Callable[..., None]
    split_text_into_segments: Callable[[str], List[str]]
    message_segment_cls: Any
    get_sticker_files: Callable[[], List[Path]]
    get_http_client: Callable[[], httpx.AsyncClient]


@dataclass
class TypeDeps:
    poke_event_cls: Any
    message_event_cls: Any
    group_message_event_cls: Any
    private_message_event_cls: Any
    message_cls: Any


@dataclass
class ReplyProcessorDeps:
    session: SessionDeps
    persona: PersonaDeps
    runtime: RuntimeDeps
    types: TypeDeps


async def _extract_images_from_segment(
    seg: Any,
    *,
    http_client: httpx.AsyncClient,
    message_text_ref: List[str],
    image_urls: List[str],
    logger: Any,
) -> None:
    if getattr(seg, "type", None) != "image":
        return
    data = getattr(seg, "data", {})
    url = data.get("url")
    file_name = str(data.get("file", "")).lower()
    if not url:
        return

    try:
        resp = await http_client.get(url, timeout=10)
        if resp.status_code == 200:
            mime_type = resp.headers.get("Content-Type", "image/jpeg")
            if "image/gif" in mime_type or file_name.endswith(".gif"):
                logger.info("拟人插件：检测到 GIF 图片，忽略并不予回复")
                return
            try:
                img_obj = Image.open(BytesIO(resp.content))
                w, h = img_obj.size
                if w <= 1280 and h <= 1280:
                    message_text_ref.append("[图片·表情包]")
                else:
                    message_text_ref.append("[图片·照片]")
            except Exception as e:
                logger.warning(f"识别图片尺寸失败: {e}")
                message_text_ref.append("[图片·照片]")

            base64_data = base64.b64encode(resp.content).decode("utf-8")
            image_urls.append(f"data:{mime_type};base64,{base64_data}")
        elif not file_name.endswith(".gif"):
            message_text_ref.append("[图片·照片]")
            image_urls.append(url)
    except Exception as e:
        logger.warning(f"下载图片失败，保留原 URL: {e}")
        if not file_name.endswith(".gif"):
            message_text_ref.append("[图片·照片]")
            image_urls.append(url)


async def _extract_reply_images(
    seg_type: str,
    data: Dict[str, Any],
    *,
    http_client: httpx.AsyncClient,
    message_text_ref: List[str],
    image_urls: List[str],
    logger: Any,
) -> None:
    if seg_type != "image":
        return
    url = data.get("url")
    file_name = str(data.get("file", "")).lower()
    if not url:
        return
    try:
        resp = await http_client.get(url, timeout=10)
        if resp.status_code == 200:
            mime_type = resp.headers.get("Content-Type", "image/jpeg")
            if "image/gif" in mime_type or file_name.endswith(".gif"):
                return
            message_text_ref.append("[图片]")
            base64_data = base64.b64encode(resp.content).decode("utf-8")
            image_urls.append(f"data:{mime_type};base64,{base64_data}")
        elif not file_name.endswith(".gif"):
            message_text_ref.append("[图片]")
            image_urls.append(url)
    except Exception as e:
        logger.warning(f"下载引用图片失败: {e}")
        if not file_name.endswith(".gif"):
            message_text_ref.append("[图片]")
            image_urls.append(url)


async def process_response_logic(bot: Any, event: Any, state: Dict[str, Any], deps: ReplyProcessorDeps) -> None:
    session = deps.session
    persona = deps.persona
    runtime = deps.runtime
    types = deps.types

    if hasattr(event, "message_id") and runtime.is_msg_processed(event.message_id):
        return

    is_poke = False
    user_id = ""
    group_id: Any = 0
    message_content = ""
    message_text = ""
    sender_name = ""
    trigger_reason = ""
    image_urls: List[str] = []
    http_client = runtime.get_http_client()

    is_random_chat = state.get("is_random_chat", False)
    force_mode = state.get("force_mode", None)

    if isinstance(event, types.poke_event_cls):
        is_poke = True
        user_id = str(event.user_id)
        group_id = str(event.group_id)
        message_content = "[你被对方戳了戳，你感到有点疑惑和好奇，想知道对方要做什么]"
        sender_name = "戳戳怪"
        runtime.logger.info(f"拟人插件：检测到来自 {user_id} 的戳一戳")
    elif isinstance(event, types.message_event_cls):
        user_id = str(event.user_id)

        if isinstance(event, types.group_message_event_cls):
            group_id = str(event.group_id)
            sender_name = event.sender.nickname or event.sender.card or user_id
            custom_title = persona.get_custom_title(user_id)
            if custom_title:
                sender_name = custom_title
        else:
            group_id = f"private_{user_id}"
            sender_name = event.sender.nickname or user_id
            custom_title = persona.get_custom_title(user_id)
            if custom_title:
                sender_name = custom_title

        message_text_parts: List[str] = []
        source_message = state.get("concatenated_message", event.message)
        for seg in source_message:
            if seg.type == "text":
                message_text_parts.append(seg.data.get("text", ""))
            elif seg.type == "face":
                face_id = seg.data.get("id", "")
                message_text_parts.append(f"[表情id:{face_id}]")
            elif seg.type == "mface":
                summary = seg.data.get("summary", "表情包")
                message_text_parts.append(f"[{summary}]")
            elif seg.type == "image":
                await _extract_images_from_segment(
                    seg,
                    http_client=http_client,
                    message_text_ref=message_text_parts,
                    image_urls=image_urls,
                    logger=runtime.logger,
                )

        reply = getattr(event, "reply", None)
        if reply:
            reply_msg = getattr(reply, "message", None) or (reply.get("message") if isinstance(reply, dict) else None)
            if reply_msg:
                message_text_parts.append("\n[引用内容]: ")
                try:
                    if isinstance(reply_msg, (list, tuple, types.message_cls)):
                        for seg in reply_msg:
                            seg_type = getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)
                            data = getattr(seg, "data", None) or (seg.get("data") if isinstance(seg, dict) else {})
                            if seg_type == "text":
                                message_text_parts.append(data.get("text", ""))
                            elif seg_type == "image":
                                await _extract_reply_images(
                                    seg_type,
                                    data,
                                    http_client=http_client,
                                    message_text_ref=message_text_parts,
                                    image_urls=image_urls,
                                    logger=runtime.logger,
                                )
                except Exception as e:
                    runtime.logger.warning(f"处理引用消息失败: {e}")

        message_text = "".join(message_text_parts)
        message_content = message_text.strip()
        is_private_context = str(group_id).startswith(session.private_session_prefix)
        if isinstance(event, types.private_message_event_cls) and session.looks_like_private_command(message_content):
            runtime.logger.debug(f"拟人插件：私聊命令消息已跳过，用户 {user_id}")
            return

        base_prompt = persona.load_prompt(group_id)
        is_yaml_mode = isinstance(base_prompt, dict)

        if is_yaml_mode:
            if is_poke:
                trigger_reason = "对方戳了戳你。"
            elif is_random_chat:
                trigger_reason = (
                    f"你正在【潜水】观察群聊。"
                    f"发言者是 {sender_name}({user_id})，这条消息并非对你说的，是群员间的普通对话。"
                    f"请保持沉默，默认输出 [SILENCE]。"
                    f"仅当你的名字被直接提及，或话题让你极度感兴趣时，才考虑开口搭话。"
                )
            else:
                trigger_reason = f"对方（{sender_name}）正在【主动】与你搭话，请认真回复。"

            if image_urls and not message_content:
                message_content = "[发送了一张图片]"
        else:
            if is_private_context:
                if image_urls and not message_content:
                    message_content = "[发送了一张图片]"
            else:
                if image_urls and not message_content:
                    if is_random_chat:
                        message_content = f"[你观察到群里 {sender_name} 发送了一张图片，这只是群员间的交流，你决定是否要评价一下]"
                    else:
                        message_content = "[对方发送了一张图片，是在对你说话]"
                elif is_random_chat:
                    message_content = f"[提示：当前为【随机插话模式】。群员 {sender_name} 正在和别人聊天，内容是: {message_content}。如果话题与你无关，请务必回复 [SILENCE]]"
                else:
                    message_content = f"[提示：对方正在【直接】对你说话：{message_content}]"
    else:
        return

    if not runtime.get_configured_api_providers():
        runtime.logger.warning("拟人插件：未配置可用的 API provider，跳过回复")
        return

    user_name = sender_name
    if not message_content and not is_poke and not image_urls:
        return

    if isinstance(event, types.group_message_event_cls) and runtime.should_avoid_interrupting(str(group_id), is_random_chat):
        runtime.logger.info(f"拟人插件：群 {group_id} 讨论热度高，触发 KY 规避，本轮保持沉默。")
        return

    if not is_poke:
        runtime.logger.info(
            f"拟人插件：[Bot {bot.self_id}] [Inst {runtime.module_instance_id}] 正在处理来自 {user_name} ({user_id}) 的消息..."
        )
    else:
        runtime.logger.info(
            f"拟人插件：[Bot {bot.self_id}] [Inst {runtime.module_instance_id}] 正在处理来自 {user_name} ({user_id}) 的戳一戳..."
        )

    is_private_session = str(group_id).startswith(session.private_session_prefix)
    session_id = session.build_private_session_id(user_id) if is_private_session else session.build_group_session_id(str(group_id))
    legacy_session_id = None if is_private_session else str(group_id)
    session.ensure_session_history(session_id, legacy_session_id=legacy_session_id)

    user_persona = ""
    try:
        persona_data_path = Path("data/user_persona/data.json")
        if persona_data_path.exists():
            async with aiofiles.open(persona_data_path, mode="r", encoding="utf-8") as f:
                persona_json = json.loads(await f.read())
                personas = persona_json.get("personas", {})
                if user_id in personas:
                    user_persona = personas[user_id].get("data", "")
                    runtime.logger.info(f"拟人插件：成功为用户 {user_id} 加载画像信息")
    except Exception as e:
        runtime.logger.error(f"拟人插件：读取用户画像数据失败: {e}")

    attitude_desc = "态度普通，像平常一样交流。"
    level_name = "未知"
    group_attitude = ""

    if persona.sign_in_available:
        try:
            user_data = persona.get_user_data(user_id)
            favorability = user_data.get("favorability", 0.0)
            level_name = persona.get_level_name(favorability)
            attitude_desc = persona.favorability_attitudes.get(level_name, attitude_desc)

            group_key = f"group_{group_id}"
            group_data = persona.get_user_data(group_key)
            group_favorability = group_data.get("favorability", 100.0)
            group_level = persona.get_level_name(group_favorability)
            group_attitude = persona.favorability_attitudes.get(group_level, "")
        except Exception as e:
            runtime.logger.error(f"获取好感度数据失败: {e}")

    safe_user_name = user_name.replace(":", "：").replace("\n", " ").strip()
    safe_user_name = f"{safe_user_name}({user_id})"
    msg_prefix = f"[{safe_user_name}]: "

    if image_urls:
        current_user_content: Any = [{"type": "text", "text": f"{msg_prefix}{message_content}"}]
        for url in image_urls:
            current_user_content.append({"type": "image_url", "image_url": {"url": url}})
        session.append_session_message(
            session_id,
            "user",
            current_user_content,
            legacy_session_id=legacy_session_id,
            is_direct=not is_random_chat,
            scene="private" if is_private_session else ("direct" if not is_random_chat else "observe"),
        )
    else:
        session.append_session_message(
            session_id,
            "user",
            f"{msg_prefix}{message_content}",
            legacy_session_id=legacy_session_id,
            is_direct=not is_random_chat,
            scene="private" if is_private_session else ("direct" if not is_random_chat else "observe"),
        )

    session_messages = session.sanitize_session_messages(session.get_session_messages(session_id))
    if is_private_session:
        session_messages = session_messages[-24:]

    base_prompt = persona.load_prompt(str(group_id))
    if isinstance(base_prompt, dict):
        if not trigger_reason and is_poke:
            trigger_reason = "对方戳了戳你。"
        await runtime.process_yaml_response_logic(
            bot,
            event,
            str(group_id),
            user_id,
            user_name,
            level_name,
            base_prompt,
            session_messages,
            trigger_reason=trigger_reason,
        )
        return

    attitude_desc = attitude_desc or "态度普通，像平常一样交流。"
    combined_attitude = f"你对该用户的个人态度是：{attitude_desc}"
    if group_attitude:
        combined_attitude += f"\n当前群聊整体氛围带给你的感受是：{group_attitude}"

    web_search_hint = ""
    if runtime.plugin_config.personification_web_search:
        web_search_hint = "你现在拥有联网搜索能力，可以获取最新的实时信息、新闻和知识来回答用户。"
    grounding_context = await runtime.build_grounding_context(message_text or message_content)

    now = runtime.get_beijing_time()
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = (
        f"{now.year}年{now.month:02d}月{now.day:02d}日 "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) [东京时间 JST/UTC+9]"
    )

    group_config = persona.get_group_config(str(group_id))
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = runtime.plugin_config.personification_schedule_global
    schedule_active = schedule_enabled or global_schedule_enabled

    schedule_prompt_part = ""
    schedule_disabled_override = ""
    time_section_title = "## 当前绝对时空（强制遵循）"
    if schedule_active:
        schedule_prompt_part = runtime.get_schedule_prompt_injection()
    else:
        schedule_disabled_override = runtime.schedule_disabled_override_prompt()
        time_section_title = "## 当前时间信息（非作息约束）"

    system_prompt = (
        f"{schedule_disabled_override}\n\n"
        f"{base_prompt}\n\n"
        f"{time_section_title}\n"
        f"- 当前时间：{current_time_str}\n"
        f"{schedule_prompt_part}\n"
        f"## 当前对话环境\n"
        f"- 对方昵称：{user_name}\n"
        f"- 对方好感等级：{level_name}\n"
        f"- 你的心理状态：{combined_attitude}\n"
    )

    if is_private_session:
        system_prompt += (
            f"\n## 私聊特殊规则（高优先级）\n"
            f"1. **自主回复决策**：私聊中你拥有完全的自主权。如果对方发送的内容重复、无意义（如连续的“在吗”、“你好”、“哈哈”等）或你不想理会，请直接输出 **[SILENCE]** 结束话题，不要强行回复。\n"
            f"2. **极简回复模式**：对于高频、低信息量的私聊内容，必须使用极简回复（1-2句以内），切勿长篇大论。\n"
            f"3. **第二人称约束**：必须用“你”称呼当前对象，禁止使用“他/她/对方/这位用户”。\n"
            f"4. **称呼约束**：私聊中禁止使用“大家/你们/各位”。\n"
        )
        anti_loop_hint = session.build_private_anti_loop_hint(session_messages)
        if anti_loop_hint:
            system_prompt += f"\n{anti_loop_hint}\n"

    if user_persona:
        system_prompt += (
            f"\n## 对方的用户画像（由分析插件提供）\n"
            f"以下是对该用户的专业分析，请你根据这些特征（如职业、性格、兴趣）来调整你的语气和话题侧重点：\n"
            f"{user_persona}\n"
        )

    group_style = persona.get_group_style(str(group_id))
    if group_style and not is_private_session:
        system_prompt += f"\n\n## 当前群聊风格参考\n{group_style}\n请在回复时适当融入上述群聊风格，使对话更自然。\n"

    system_prompt += (
        f"\n{web_search_hint}\n"
        f"## 核心行动准则（绝对优先）\n"
        f"1. **身份一致性**：你必须完全沉浸在上述角色设定中。严禁提及你是 AI、机器人、程序 or 语言模型。严禁以 AI 助手的语气说话。\n"
        f"2. **回复风格**：回复必须精简、口语化，严禁使用长篇大论的列表或过于正式的陈述。回复长度限制在 30 个汉字左右。\n"
        f"3. **社交距离感**：你必须分清【直接对话】与【潜水观察】。在群员之间互相聊天（未艾特你或提及你名字）时，你只是个旁观者，绝不能认为对方是在对你说话。此时应尽量保持沉默，除非你确信有必要插话。\n"
        f"4. **互动决策**：\n"
        f"   - **决定是否回复**：仔细判断对话是否已经自然结束，或者对方只是发送了无意义的感慨/语气词。如果你认为**没有必要回复**，请直接输出 **[SILENCE]**。\n"
        f"   - **氛围反馈**：若氛围极好或对方让你开心，末尾加 [氛围好]。\n"
        f"   - **防御机制**：当检测到对方发送**恶毒语言**（如“杀了你全家”、“去死吧”等诅咒或严重人身攻击）或**黄赌毒恐暴**（色情、赌博、毒品、恐怖主义、暴力）相关内容时，**必须**输出 [BLOCK] 以触发自动拉黑机制。这是为了保护你和维护群聊环境。\n"
        f"4. **视觉感知**：\n"
        f"   - 若用户发送内容标记为 **[发送了一个表情包]**，请将其视为**梗图/表情包**。这通常是幽默、夸张或流行文化引用，**严禁**将其解读为真实发生的严重事件（如受伤、灾难）。请以轻松、调侃、配合玩梗或“看来你很喜欢这个表情”的态度回复。\n"
        f"   - 若标记为 **[发送了一张图片]**，则正常结合图片内容进行符合人设的评价。\n"
    )
    if grounding_context:
        system_prompt += (
            "\n## 联网事实校验（自动注入）\n"
            f"{grounding_context}\n"
            "回答时优先使用该事实，禁止无依据脑补。\n"
        )

    available_stickers: List[str] = []
    group_config = persona.get_group_config(str(group_id))
    if group_config.get("sticker_enabled", True):
        available_stickers = [f.stem for f in runtime.get_sticker_files()]

    messages = [
        {
            "role": "system",
            "content": (
                f"{system_prompt}\n\n当前可用表情包参考: "
                f"{', '.join(available_stickers[:15]) if available_stickers else '暂无'}"
            ),
        }
    ]
    messages.extend(session_messages)

    try:
        if is_private_session:
            try:
                runtime.update_private_interaction_time(user_id)
            except Exception as e:
                runtime.logger.error(f"更新最后交互时间失败: {e}")

        reply_content = await runtime.call_ai_api(messages)
        if not reply_content:
            if image_urls:
                runtime.logger.warning("拟人插件：视觉模型调用可能失败，正在尝试降级至纯文本模式...")
                fallback_messages = []
                for msg in messages:
                    if isinstance(msg.get("content"), list):
                        text_content = "".join([item["text"] for item in msg["content"] if item["type"] == "text"])
                        fallback_messages.append({"role": msg["role"], "content": text_content})
                    else:
                        fallback_messages.append(msg)
                reply_content = await runtime.call_ai_api(fallback_messages)
            if not reply_content:
                runtime.logger.warning("拟人插件：未能获取到 AI 回复内容")
                return

        reply_content = re.sub(r"\[表情:[^\]]*\]", "", reply_content)
        reply_content = re.sub(r"\[发送了表情包:[^\]]*\]", "", reply_content).strip()
        reply_content = re.sub(r"[A-F0-9]{16,}", "", reply_content).strip()

        if "[SILENCE]" in reply_content:
            runtime.logger.info(f"AI 决定结束与群 {group_id} 中 {user_name}({user_id}) 的对话 (SILENCE)")
            return

        if "[BLOCK]" in reply_content or "[NO_REPLY]" in reply_content:
            duration = runtime.plugin_config.personification_blacklist_duration
            runtime.user_blacklist[user_id] = time.time() + duration
            runtime.logger.info(f"AI 决定拉黑群 {group_id} 中 {user_name}({user_id})，时长 {duration} 秒")

            if persona.sign_in_available:
                try:
                    is_private_context = str(group_id).startswith("private_")
                    penalty = round(random.uniform(0, 0.3), 2)
                    user_data = persona.get_user_data(user_id)
                    current_fav = float(user_data.get("favorability", 0.0))
                    new_fav = round(max(0.0, current_fav - penalty), 2)

                    current_blacklist_count = int(user_data.get("blacklist_count", 0)) + 1
                    is_perm = current_blacklist_count >= 25
                    persona.update_user_data(
                        user_id,
                        favorability=new_fav,
                        blacklist_count=current_blacklist_count,
                        is_perm_blacklisted=is_perm,
                    )

                    if not is_private_context:
                        group_key = f"group_{group_id}"
                        group_data = persona.get_user_data(group_key)
                        g_current_fav = float(group_data.get("favorability", 100.0))
                        g_new_fav = round(max(0.0, g_current_fav - 0.5), 2)
                        persona.update_user_data(group_key, favorability=g_new_fav)

                    if is_perm:
                        runtime.logger.info(f"用户 {user_id} 拉黑累计达到 {current_blacklist_count} 次，已自动加入永久黑名单。")
                    else:
                        runtime.logger.info(f"用户 {user_id} 拉黑累计 {current_blacklist_count} 次。")
                except Exception as e:
                    runtime.logger.error(f"扣除好感度或更新黑名单失败: {e}")
            return

        has_good_atmosphere = "[氛围好]" in reply_content
        if has_good_atmosphere:
            reply_content = reply_content.replace("[氛围好]", "").strip()
            if persona.sign_in_available:
                try:
                    is_private_context = str(group_id).startswith("private_")
                    if not is_private_context:
                        group_key = f"group_{group_id}"
                        group_data = persona.get_user_data(group_key)

                        today = time.strftime("%Y-%m-%d")
                        last_update = group_data.get("last_update", "")
                        daily_count = group_data.get("daily_fav_count", 0.0)

                        if last_update != today:
                            daily_count = 0.0

                        if daily_count < 10.0:
                            g_current_fav = float(group_data.get("favorability", 100.0))
                            g_new_fav = round(g_current_fav + 0.1, 2)
                            daily_count = round(float(daily_count) + 0.1, 2)
                            persona.update_user_data(
                                group_key,
                                favorability=g_new_fav,
                                daily_fav_count=daily_count,
                                last_update=today,
                            )
                            runtime.logger.info(
                                f"AI 觉得群 {group_id} 氛围良好，好感度 +0.10 (今日已加: {daily_count:.2f}/10.00)"
                            )
                except Exception as e:
                    runtime.logger.error(f"增加群聊好感度失败: {e}")

        sticker_segment = None
        sticker_name = ""
        should_get_sticker = False

        group_config = persona.get_group_config(str(group_id))
        is_sticker_enabled = group_config.get("sticker_enabled", True)
        if is_sticker_enabled:
            if force_mode == "mixed":
                should_get_sticker = True
            elif force_mode == "text_only":
                should_get_sticker = False
            elif random.random() < runtime.plugin_config.personification_sticker_probability:
                should_get_sticker = True

        if should_get_sticker:
            stickers = runtime.get_sticker_files()
            if stickers:
                random_sticker = random.choice(stickers)
                sticker_name = random_sticker.stem
                sticker_segment = runtime.message_segment_cls.image(f"file:///{random_sticker.absolute()}")
                runtime.logger.info(f"拟人插件：随机挑选了表情包 {random_sticker.name}")

        assistant_content = session.sanitize_history_text(reply_content)
        session.append_session_message(
            session_id,
            "assistant",
            assistant_content,
            legacy_session_id=legacy_session_id,
            scene="reply",
            sticker_sent=sticker_name if sticker_name else None,
        )

        if isinstance(event, types.group_message_event_cls):
            bot_nickname = persona.default_bot_nickname or str(bot.self_id)
            try:
                bot_member_info = await bot.get_group_member_info(
                    group_id=event.group_id,
                    user_id=int(bot.self_id),
                )
                bot_nickname = bot_member_info.get("card") or bot_member_info.get("nickname") or bot_nickname
            except Exception:
                pass
            runtime.record_group_msg(str(event.group_id), bot_nickname, assistant_content, is_bot=True)

        final_reply = reply_content.strip()
        if final_reply:
            segments = runtime.split_text_into_segments(final_reply)
            if not segments:
                segments = [final_reply]
            for i, seg in enumerate(segments):
                if not seg.strip():
                    continue
                await bot.send(event, seg)
                if i < len(segments) - 1 or sticker_segment:
                    await asyncio.sleep(random.uniform(3.0, 5.0))

        if sticker_segment:
            await bot.send(event, sticker_segment)
    except FinishedException:
        raise
    except Exception as e:
        runtime.logger.error(f"拟人插件 API 调用失败: {e}")
