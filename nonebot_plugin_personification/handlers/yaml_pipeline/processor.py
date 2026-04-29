import asyncio
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

from ...agent.inner_state import DEFAULT_STATE as DEFAULT_INNER_STATE, get_personification_data_dir, load_inner_state
from ...core.chat_intent import (
    infer_turn_semantic_frame_with_llm,
    looks_like_explanatory_output,
)
from ...core.emotion_state import (
    build_turn_emotion_prompt_block,
    load_emotion_state,
    render_emotion_memory_hint,
    render_inner_state_hint,
    update_emotion_state_after_turn,
)
from ...core.group_context import render_group_context_structured
from ...core.group_relations import summarize_group_relationships
from ...core.metrics import record_counter, record_timing
from ...core.message_relations import extract_event_message_id, extract_reply_message_id, extract_send_message_id
from ...core.image_input import (
    is_image_input_unsupported_error,
    normalize_image_detail,
    normalize_image_input_mode,
)
from ...core.sticker_library import (
    resolve_sticker_dir,
)
from ...core.message_parts import build_user_message_content, clone_messages_with_text_suffix
from ...core.context_policy import build_prompt_injection_guard
from ...core.repeat_follow import maybe_follow_repeat_cluster
from ...core.reply_style_policy import (
    build_direct_visual_identity_guard,
    build_reply_style_policy_prompt,
)
from ...core.response_review import (
    ReplyArbitrationIntent,
    arbitrate_reply_mode,
    decide_random_chat_speak,
    extract_recent_bot_reply_texts,
    is_agent_reply_ooc,
    make_passthrough_review_decision,
    recover_direct_mention_reply,
    rewrite_agent_reply_ooc,
    review_response_text,
)
from ...core.prompt_loader import pick_ack_phrase
from ...core.sticker_feedback import (
    build_sticker_feedback_scene_key,
    load_sticker_feedback,
    mark_pending_sticker_reaction,
    record_sticker_sent,
)
from ...core.target_inference import TARGET_OTHERS
from ...core.tts_service import extract_persona_tts_config
from ...core.visual_capabilities import VISUAL_ROUTE_AGENT, VISUAL_ROUTE_REPLY_YAML

from ...agent.action_executor import ActionExecutor
from ...agent.loop import run_agent
from ...agent.query_rewriter import QueryRewriteContext
from ..reply_pipeline.pipeline_emotion import compose_reply_emotion_block
from ..reply_pipeline.pipeline_context import (
    batch_has_newer_messages as _shared_batch_has_newer_messages,
    compute_agent_time_budget as _compute_agent_time_budget,
    primary_route_supports_vision as _runtime_primary_route_supports_vision,
    should_use_agent_for_reply as _should_use_agent_for_reply,
    strip_injected_visual_summary as _strip_injected_visual_summary,
)
from ..reply_pipeline.pipeline_sticker import build_image_summary_suffix as _shared_build_image_summary_suffix
from ...skills.skillpacks.sticker_tool.scripts.impl import (
    choose_sticker_for_context,
    reset_current_image_context,
    set_current_image_context,
)
from ...utils import build_group_context_window, get_group_topic_summary, get_recent_group_msgs


_IMAGE_B64_RE = re.compile(r"\[IMAGE_B64\]([A-Za-z0-9+/=\r\n]+)\[/IMAGE_B64\]")


def _extract_image_b64_markers(text: str) -> tuple[str, list[str]]:
    payloads: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        payload = re.sub(r"\s+", "", match.group(1) or "")
        if payload:
            payloads.append(payload)
        return ""

    cleaned = _IMAGE_B64_RE.sub(_replace, str(text or "")).strip()
    return cleaned, payloads


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
    *,
    allow_schedule_status: bool = True,
) -> str:
    if not allow_schedule_status:
        bot_statuses.pop(group_id, None)
        return ""

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


def _event_mentions_bot(event: Any, bot: Any) -> bool:
    bot_self_id = str(getattr(bot, "self_id", "") or "").strip()
    if not bot_self_id:
        return False
    try:
        for seg in getattr(event, "message", []) or []:
            if getattr(seg, "type", None) != "at":
                continue
            qq = str((getattr(seg, "data", {}) or {}).get("qq", "") or "").strip()
            if qq == bot_self_id:
                return True
    except Exception:
        return False
    return False


def _primary_route_supports_vision(
    *,
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]] | None,
    plugin_config: Any,
    route_name: str,
) -> bool:
    return _runtime_primary_route_supports_vision(
        _build_yaml_runtime_proxy(
            plugin_config=plugin_config,
            agent_tool_caller=None,
            get_configured_api_providers=get_configured_api_providers,
            vision_caller=None,
            logger=None,
        ),
        route_name,
    )


def _batch_ref_has_newer_messages(runtime_ref: Any) -> bool:
    return _shared_batch_has_newer_messages({"batch_runtime_ref": runtime_ref})


def _build_yaml_runtime_proxy(
    *,
    plugin_config: Any,
    agent_tool_caller: Any,
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]] | None,
    vision_caller: Any,
    logger: Any,
) -> Any:
    return type(
        "_YamlReplyRuntime",
        (),
        {
            "plugin_config": plugin_config,
            "agent_tool_caller": agent_tool_caller,
            "vision_caller": vision_caller,
            "logger": logger,
            "get_configured_api_providers": staticmethod(get_configured_api_providers or (lambda: [])),
        },
    )()


async def _build_image_summary_suffix(
    *,
    plugin_config: Any,
    agent_tool_caller: Any,
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]] | None,
    vision_caller: Any,
    image_urls: List[str],
    sticker_like: bool,
    logger: Any,
) -> str:
    return await _shared_build_image_summary_suffix(
        runtime=_build_yaml_runtime_proxy(
            plugin_config=plugin_config,
            agent_tool_caller=agent_tool_caller,
            get_configured_api_providers=get_configured_api_providers,
            vision_caller=vision_caller,
            logger=logger,
        ),
        image_urls=image_urls,
        sticker_like=sticker_like,
    )


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
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]] | None = None,
    tool_registry: Any = None,
    agent_tool_caller: Any = None,
    lite_tool_caller: Any = None,
    lite_call_ai_api: Callable[..., Awaitable[Any]] | None = None,
    current_image_urls: List[str] | None = None,
    vision_caller: Any = None,
    tts_service: Any = None,
    extract_forward_content: Callable[..., Any] = None,
    memory_curator: Any = None,
    knowledge_store: Any = None,
    disable_network_hooks: bool = False,
    batched_events: List[Dict[str, Any]] | None = None,
    repeat_clusters: List[Dict[str, Any]] | None = None,
    batch_event_count: int = 1,
    message_intent: str = "",
    raw_message_text: str = "",
    is_random_chat: bool = False,
    message_target: str = "",
    intent_ambiguity_level: str = "",
    intent_recommend_silence: bool | None = None,
    recent_context_hint: str = "",
    relationship_hint: str = "",
    semantic_frame: Any = None,
    has_newer_batch: bool = False,
    batch_runtime_ref: Dict[str, Any] | None = None,
    solo_speaker_follow: bool = False,
) -> None:
    """处理基于 YAML 模板的新版响应逻辑。"""
    started_at = time.monotonic()
    lite_tool_caller = lite_tool_caller or agent_tool_caller
    lite_call_ai_api = lite_call_ai_api or call_ai_api

    def _has_newer_batch_now() -> bool:
        return bool(has_newer_batch or _batch_ref_has_newer_messages(batch_runtime_ref))

    now = get_current_time()
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = (
        f"{now.year}年{now.month:02d}月{now.day:02d}日 "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) "
        f"[{format_time_context(now)}]"
    )

    is_private_session = str(group_id).startswith(private_session_prefix)
    record_counter(
        "yaml_reply.requests_total",
        scene="private" if is_private_session else "group",
        random_chat=bool(is_random_chat),
    )
    is_direct_mention = _event_mentions_bot(event, bot)

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

    last_msg = chat_history[-1] if chat_history else {"content": raw_message_text or ""}
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
    photo_like = "[图片·照片]" in (history_last_text or raw_message_text or trigger_reason)

    if not is_private_session and not recent_context_hint:
        recent_window = build_group_context_window(
            group_id,
            limit=8,
            include_message_ids=[extract_reply_message_id(event)],
        )
        recent_context_hint = render_group_context_structured(
            recent_window,
            trigger_msg_id=extract_event_message_id(event),
        )
        relationship_hint = relationship_hint or summarize_group_relationships(
            recent_window,
            trigger_msg_id=extract_event_message_id(event),
            trigger_user_id=user_id,
            bot_self_id=str(getattr(bot, "self_id", "") or ""),
        )
    else:
        recent_window = []
    recent_bot_replies = extract_recent_bot_reply_texts(
        recent_window if recent_window else get_recent_group_msgs(group_id, limit=8, expire_hours=0) if not is_private_session else []
    )
    data_dir = get_personification_data_dir(plugin_config)
    inner_state = dict(DEFAULT_INNER_STATE)
    try:
        inner_state.update(await load_inner_state(data_dir))
    except Exception as e:
        logger.debug(f"[emotion] YAML load inner_state failed: {e}")
    emotion_state = {}
    try:
        emotion_state = await load_emotion_state(data_dir)
    except Exception as e:
        logger.debug(f"[emotion] YAML load emotion_state failed: {e}")
    emotion_memory_hint = render_emotion_memory_hint(
        emotion_state,
        user_id=user_id,
        group_id="" if is_private_session else group_id,
    )

    if semantic_frame is None:
        semantic_frame = await infer_turn_semantic_frame_with_llm(
            raw_message_text or history_last_text or trigger_reason,
            is_group=not is_private_session,
            is_random_chat=is_random_chat,
            tool_caller=lite_tool_caller,
            recent_context=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            current_inner_state=render_inner_state_hint(inner_state),
            current_emotion_state=emotion_memory_hint,
        )
    intent_decision = semantic_frame.to_intent_decision()
    if not message_intent:
        message_intent = intent_decision.chat_intent
    if not str(intent_ambiguity_level or "").strip():
        intent_ambiguity_level = intent_decision.ambiguity_level
    if intent_recommend_silence is None:
        intent_recommend_silence = intent_decision.recommend_silence

    arbitration = arbitrate_reply_mode(
        intent_decision=ReplyArbitrationIntent(
            ambiguity_level=str(intent_ambiguity_level or "").strip(),
            recommend_silence=bool(intent_recommend_silence),
        ),
        is_private=is_private_session,
        is_direct_mention=is_direct_mention,
        is_random_chat=is_random_chat,
        message_target=str(message_target or ""),
        solo_speaker_follow=solo_speaker_follow,
    )
    if arbitration == "no_reply":
        logger.info(
            f"拟人插件 (YAML)：LLM 意图判别认为本轮高歧义且不宜插话，group={group_id} user={user_id}"
        )
        return
    if is_random_chat:
        should_speak = await decide_random_chat_speak(
            lite_call_ai_api,
            raw_message_text=raw_message_text or history_last_text or trigger_reason,
            recent_context=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            recent_bot_replies=recent_bot_replies,
            has_newer_batch=_has_newer_batch_now(),
            message_intent=message_intent,
            ambiguity_level=str(intent_ambiguity_level or "").strip().lower(),
            message_target=str(message_target or ""),
            solo_speaker_follow=solo_speaker_follow,
        )
        if not should_speak:
            logger.info(f"拟人插件 (YAML)：随机插话场景被 LLM 否决，group={group_id} user={user_id}")
            return

    system_prompt = prompt_config.get("system", "")
    if is_private_session:
        system_prompt += (
            "\n\n## 私聊称呼规则（高优先级）\n"
            "- 你在和单个用户对话，必须使用第二人称“你”。\n"
            "- 禁止使用“他/她/对方/这位用户”指代当前聊天对象。\n"
            "- 禁止出现“大家/你们/各位”这类群聊称呼。\n"
            "- 如果最新消息只是“在吗/还在吗/有人吗”这类心跳，只回应当前问候，不要延续旧话题补答。\n"
        )
    else:
        system_prompt += (
            "\n\n## 群聊接话规则（高优先级）\n"
            "- 回复先像群友顺手接一句，优先短句、口语、吐槽、接梗。\n"
            "- 遇到梗、复读、空耳、调侃时，默认先顺着气氛接话，不要把笑点翻译成说明文。\n"
            "- 除非对方明确在问出处、意思或为什么好笑，否则不要用“像是把 X 玩成 Y 了”这种解释梗结构的句式。\n"
            "- 只有明显无关、会打断别人、刚说过类似内容，或高歧义且没人 cue 你时才保持沉默。\n"
            "- 如果最新消息只是“在吗/还在吗/有人吗”这类心跳，只回应当前问候，不要延续旧话题补答。"
        )
        if message_intent == "banter":
            system_prompt += (
                "\n- 当前更像接梗/吐槽场景，优先反应、补半句、复读或短吐槽，不要写成解释文。"
            )
        if solo_speaker_follow:
            system_prompt += "\n- 对方已经连续说了一阵，这轮更适合像群友顺手接一句。"
        if str(intent_ambiguity_level or "").strip().lower() == "high":
            system_prompt += (
                "\n- 当前最新名词/对象存在较高歧义。"
                "如果上下文和现有证据不足，请优先承认不确定；群聊里若没人明确在 cue 你，且这轮明显会打断别人时，也可以保持沉默。"
            )
        if arbitration == "clarify":
            system_prompt += "\n- 这轮高歧义但对方像是在直接问你，优先用一句短澄清问句确认对象。"
    system_prompt += (
        "\n\n## 基础输出规则\n"
        "- 输出纯文本，禁止使用 markdown 格式（不要用 **加粗**、*斜体*、# 标题、- 列表符号、`代码块`等）。\n"
        "- 收到贴图/表情包时绝对不要对图片内容发表任何评论（包括“这图也太X了”“哈哈这个”等），当作没看见，按对话语境继续；收到真实照片时可以像群友看朋友圈一样自然回应。\n"
        "- 表情包/梗图/截图可以当作语气线索理解，但没人问图里是什么时，不要主动做图片讲解。"
    )
    system_prompt += "\n\n" + build_reply_style_policy_prompt(
        has_visual_context=bool(last_images),
        photo_like=photo_like,
    )
    if knowledge_store is not None:
        try:
            plugin_summary = knowledge_store.get_plugin_summary_for_prompt()
        except Exception as exc:
            logger.debug(f"[plugin_knowledge] YAML prompt summary unavailable: {exc}")
            plugin_summary = ""
        if plugin_summary:
            system_prompt += f"\n\n[已安装插件摘要（仅供参考）]\n{plugin_summary}"
    system_prompt += f"\n\n{build_prompt_injection_guard()}"

    group_config = get_group_config(group_id)
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = plugin_config.personification_schedule_global
    schedule_active = schedule_enabled or global_schedule_enabled
    current_status = _get_current_status(
        group_id,
        bot_statuses,
        prompt_config,
        now,
        allow_schedule_status=schedule_active,
    )

    schedule_instruction = "2. **时间锚定**：参考【当前时间】保持时间语义正确（例如早晚问候、是否还在熬夜），但不受上课/睡觉等作息硬约束。"
    system_schedule_instruction = ""

    if schedule_active:
        system_schedule_instruction = get_schedule_prompt_injection()
        schedule_instruction = "2. **时间锚定**：参考【当前时间】判断作息状态。**作息状态仅作为回复的背景设定（占比约20%），主要精力应放在回应对方的内容上。**如果当前是上课或深夜（非休息时间），你回复了消息说明你正在“偷偷玩手机”或“熬夜”，请表现出这种紧张感或困意。"
    else:
        system_prompt = f"{schedule_disabled_override_prompt()}\n\n{system_prompt}"
        system_schedule_instruction = (
            "（⚠️ 作息模拟当前已关闭：此处及以下所有涉及时间、作息、上课、深夜的约束规则"
            "在本次对话中均不生效，请忽略并以正常方式对话。"
            "本轮不要根据时间生成或沿用状态/动作，<status> 与 <action> 优先留空。）"
        )
        schedule_instruction = (
            "2. **时间锚定**：参考【当前时间】保持时间语义正确，但不要根据时间生成作息状态或动作。"
            "作息已关闭时，<status> 与 <action> 应优先留空，只专注回复内容。"
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
    grounding_context = ""
    if not disable_network_hooks:
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
    if recent_context_hint and not is_private_session:
        input_text = (
            f"{input_text}\n\n"
            f"## 最近群聊原始上下文\n"
            f"{recent_context_hint}\n"
        )
    if relationship_hint and not is_private_session:
        input_text = f"{input_text}\n\n{relationship_hint}\n"
    emotion_block = compose_reply_emotion_block(
        semantic_frame=semantic_frame,
        inner_state=inner_state,
        emotion_state=emotion_state,
        user_id=user_id,
        group_id="" if is_private_session else group_id,
        is_private=is_private_session,
    )
    if emotion_block:
        input_text = f"{input_text}\n\n{emotion_block}\n"
    if batch_event_count > 1:
        input_text = (
            f"{input_text}\n\n"
            f"## 当前批次消息\n"
            f"- 本轮合并消息数：{int(batch_event_count or 1)}\n"
        )
        for item in list(batched_events or [])[:8]:
            if not isinstance(item, dict):
                continue
            sender = str(item.get('sender_name', '') or item.get('user_id', '') or '未知').strip()
            text = str(item.get("text", "") or "").strip() or "[图片/非文本消息]"
            marker = []
            if item.get("is_direct_mention"):
                marker.append("@你")
            if item.get("is_reply_to_bot"):
                marker.append("回复你")
            suffix = f"（{'、'.join(marker)}）" if marker else ""
            input_text += f"- {sender}{suffix}: {text}\n"
    if repeat_clusters:
        input_text += "\n## 复读/接龙线索\n"
        for cluster in list(repeat_clusters or [])[:3]:
            if not isinstance(cluster, dict):
                continue
            text = str(cluster.get("text", "") or "").strip()
            count = int(cluster.get("count", 0) or 0)
            speakers = "、".join(str(item or "") for item in (cluster.get("speakers") or [])[:4])
            if text and count > 0:
                input_text += f"- {text}（{count}次，参与者：{speakers or '未知'}）\n"

    user_content: Any = input_text
    tool_image_urls = list(last_images)
    image_input_mode = normalize_image_input_mode(
        getattr(plugin_config, "personification_image_input_mode", "auto")
    )
    image_detail = normalize_image_detail(
        getattr(plugin_config, "personification_image_detail", "auto")
    )
    sticker_like = "[图片·表情包]" in input_text or "[表情id:" in input_text or "[表情包]" in input_text
    photo_like = photo_like or "[图片·照片]" in input_text
    direct_image_input = bool(last_images) and image_input_mode in {"auto", "direct"} and (
        image_input_mode == "direct"
        or _primary_route_supports_vision(
            get_configured_api_providers=get_configured_api_providers,
            plugin_config=plugin_config,
            route_name=VISUAL_ROUTE_REPLY_YAML,
        )
    )
    agent_direct_image_input = bool(last_images) and image_input_mode in {"auto", "direct"} and (
        image_input_mode == "direct"
        or _primary_route_supports_vision(
            get_configured_api_providers=get_configured_api_providers,
            plugin_config=plugin_config,
            route_name=VISUAL_ROUTE_AGENT,
        )
    )
    image_summary_suffix = ""
    text_model_images = list(last_images)
    if last_images:
        if image_input_mode == "disabled":
            text_model_images = []
        else:
            if image_input_mode in {"auto", "summary"} and (not direct_image_input or not agent_direct_image_input):
                image_summary_suffix = await _build_image_summary_suffix(
                    plugin_config=plugin_config,
                    agent_tool_caller=agent_tool_caller,
                    get_configured_api_providers=get_configured_api_providers,
                    vision_caller=vision_caller,
                    image_urls=tool_image_urls,
                    sticker_like=sticker_like,
                    logger=logger,
                )
            if not direct_image_input:
                text_model_images = []

    text_model_input = input_text
    agent_input_text = input_text
    if image_summary_suffix and tool_image_urls:
        if not direct_image_input:
            text_model_input = f"{text_model_input} {image_summary_suffix}".strip()
        if not agent_direct_image_input:
            agent_input_text = f"{agent_input_text} {image_summary_suffix}".strip()
    if photo_like:
        system_prompt += (
            "\n[系统提示] 当前消息包含真实照片，可以像群友看到朋友圈一样自然回应图片内容，"
            "不需要等对方先提问。"
        )

    image_guard_prompt = build_direct_visual_identity_guard()
    if text_model_images:
        user_content = build_user_message_content(
            text=text_model_input,
            image_urls=text_model_images,
            image_detail=image_detail,
        )
        system_prompt += image_guard_prompt
    else:
        user_content = text_model_input

    agent_user_content: Any = agent_input_text
    agent_system_prompt = system_prompt
    if agent_direct_image_input and tool_image_urls:
        agent_user_content = build_user_message_content(
            text=agent_input_text,
            image_urls=tool_image_urls,
            image_detail=image_detail,
        )
        if not text_model_images:
            agent_system_prompt += image_guard_prompt

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    agent_messages = [
        {"role": "system", "content": agent_system_prompt},
        {"role": "user", "content": agent_user_content},
    ]
    used_agent = False
    reply_content = ""
    async def _call_text_model_with_retry(messages_to_use: List[Dict[str, Any]]) -> str:
        try:
            result = await call_ai_api(messages_to_use)
        except Exception as exc:
            if not (
                tool_image_urls
                and direct_image_input
                and image_input_mode in {"auto", "direct"}
                and is_image_input_unsupported_error(exc)
            ):
                raise
            logger.warning("拟人插件 (YAML)：模型不支持图片输入，改用视觉摘要重试...")
            retry_suffix = image_summary_suffix or await _build_image_summary_suffix(
                plugin_config=plugin_config,
                agent_tool_caller=agent_tool_caller,
                get_configured_api_providers=get_configured_api_providers,
                vision_caller=vision_caller,
                image_urls=tool_image_urls,
                sticker_like=sticker_like,
                logger=logger,
            )
            retry_messages = clone_messages_with_text_suffix(messages_to_use, retry_suffix)
            result = await call_ai_api(retry_messages)
        if not result and tool_image_urls and direct_image_input and image_input_mode in {"auto", "direct"}:
            logger.warning("拟人插件 (YAML)：图片输入可能不被支持，改用视觉摘要重试...")
            retry_suffix = image_summary_suffix or await _build_image_summary_suffix(
                plugin_config=plugin_config,
                agent_tool_caller=agent_tool_caller,
                get_configured_api_providers=get_configured_api_providers,
                vision_caller=vision_caller,
                image_urls=tool_image_urls,
                sticker_like=sticker_like,
                logger=logger,
            )
            retry_messages = clone_messages_with_text_suffix(messages_to_use, retry_suffix)
            result = await call_ai_api(retry_messages)
        return result

    recovered_direct_mention_reply: str | None = None

    async def _recover_direct_mention_reply_now() -> str:
        nonlocal recovered_direct_mention_reply
        if recovered_direct_mention_reply is None:
            recovered_direct_mention_reply = await recover_direct_mention_reply(
                call_ai_api,
                raw_message_text=raw_message_text or history_last_text or trigger_reason,
                recent_context=recent_context_hint,
                relationship_hint=relationship_hint,
                recent_bot_replies=recent_bot_replies,
                semantic_frame=semantic_frame,
                is_direct_mention=is_direct_mention,
            )
        return recovered_direct_mention_reply

    if _should_use_agent_for_reply(
        plugin_config=plugin_config,
        tool_registry=tool_registry,
        agent_tool_caller=agent_tool_caller,
        message_intent=message_intent,
        ambiguity_level=str(intent_ambiguity_level or ""),
        is_direct_mention=is_direct_mention,
        has_image_input=bool(tool_image_urls),
    ):
        executor = ActionExecutor(bot, event, plugin_config, logger)
        image_ctx_token = set_current_image_context(tool_image_urls, input_text)
        ack_phrase = ""
        if is_direct_mention:
            ack_phrase = pick_ack_phrase(
                plugin_config,
                get_group_config,
                logger,
                group_id=None if is_private_session else group_id,
            )
        ack_sender = None
        if ack_phrase:
            async def _ack_sender(text: str, *, _phrase: str = ack_phrase) -> None:
                await bot.send(event, str(text or "").strip() or _phrase)
            ack_sender = _ack_sender
        try:
            try:
                agent_result = await run_agent(
                    messages=agent_messages,
                    registry=tool_registry,
                    tool_caller=agent_tool_caller,
                    executor=executor,
                    plugin_config=plugin_config,
                    logger=logger,
                    max_steps=getattr(plugin_config, "personification_agent_max_steps", 10),
                    current_image_urls=tool_image_urls,
                    direct_image_input=agent_direct_image_input,
                    query_rewrite_context=QueryRewriteContext(
                        history_new=history_new_text,
                        history_last=history_last_text,
                        trigger_reason=trigger_reason,
                        images=tool_image_urls,
                    ),
                    repeat_clusters=repeat_clusters,
                    relationship_hint=relationship_hint,
                    recent_bot_replies=recent_bot_replies,
                    precomputed_intent=intent_decision,
                    time_budget_seconds=_compute_agent_time_budget(
                        started_at=started_at,
                        total_timeout_seconds=float(
                            getattr(plugin_config, "personification_response_timeout", 180) or 180
                        ),
                    ),
                    ack_sender=ack_sender,
                )
            except Exception as exc:
                if not (
                    tool_image_urls
                    and agent_direct_image_input
                    and image_input_mode in {"auto", "direct"}
                    and is_image_input_unsupported_error(exc)
                ):
                    raise
                logger.warning("拟人插件 (YAML)：Agent 处理图片输入失败，改用基础模型摘要重试...")
                agent_result = None
        finally:
            reset_current_image_context(image_ctx_token)
        if agent_result is not None:
            reply_content = agent_result.text
            used_agent = True
            if not agent_result.direct_output and is_agent_reply_ooc(reply_content):
                rewritten_ooc = await rewrite_agent_reply_ooc(
                    tool_caller=lite_tool_caller or agent_tool_caller,
                    original_text=reply_content,
                    persona_system=system_prompt,
                )
                if rewritten_ooc:
                    reply_content = rewritten_ooc
                else:
                    reply_content = "[SILENCE]"
            if _has_newer_batch_now():
                logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                return
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
                        if _has_newer_batch_now():
                            logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                            return
                        await bot.send(event, text)
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                return
    if not used_agent:
        reply_content = await _call_text_model_with_retry(messages)
    if not reply_content:
        recovered_reply = await _recover_direct_mention_reply_now()
        if used_agent:
            logger.warning("拟人插件 (YAML): Agent 执行完成但返回空文本，请检查上方 [agent] provider 日志")
        else:
            logger.warning("拟人插件 (YAML): 未能获取到 AI 回复内容")
        if recovered_reply:
            reply_content = recovered_reply
        else:
            return
    if _has_newer_batch_now():
        logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
        return
    if used_agent and reply_content in ("[NO_REPLY]", "<NO_REPLY>"):
        recovered_reply = await _recover_direct_mention_reply_now()
        if recovered_reply:
            logger.info("拟人插件 (YAML)：Agent 对直呼消息返回 NO_REPLY，改用 LLM 补答。")
            reply_content = recovered_reply
        else:
            return

    parsed = parse_yaml_response(reply_content)
    has_block_marker = "[BLOCK]" in reply_content or "<BLOCK>" in reply_content
    frame_domain_focus = str(getattr(semantic_frame, "domain_focus", "") or "").strip().lower()
    is_fact_like_scene = bool(
        message_intent in {"lookup", "plugin_question"}
        or frame_domain_focus in {"realtime", "knowledge", "plugin"}
    )

    if has_block_marker and not is_fact_like_scene:
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
        recovered_reply = await _recover_direct_mention_reply_now()
        logger.info("AI (YAML) 决定保持沉默 (SILENCE)")
        if recovered_reply:
            reply_content = recovered_reply
            parsed = {"messages": [{"text": recovered_reply, "sticker": ""}], "think": "", "status": "", "action": ""}
        else:
            return

    status_text = str(parsed.get("status") or "").strip()
    action_text = str(parsed.get("action") or "").strip()

    if schedule_active and status_text:
        bot_statuses[group_id] = {
            "status": status_text,
            "updated_at": time.time(),
            "period_key": _status_period_key(get_current_time()),
        }
        logger.info(f"拟人插件: 更新状态为: {status_text}")
    elif not schedule_active:
        bot_statuses.pop(group_id, None)
    if parsed["think"]:
        logger.debug(f"拟人插件: 思考过程: {parsed['think']}")

    if schedule_active and action_text:
        logger.info(f"拟人插件: 执行动作: {action_text}")
        if "戳一戳" in action_text:
            try:
                await bot.send(event, message_segment_cls.poke(int(user_id)))
            except Exception as e:
                logger.warning(f"拟人插件: 发送戳一戳失败: {e}")
    elif not schedule_active:
        action_text = ""

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
    if (
        message_intent == "banter"
        and looks_like_explanatory_output(assistant_text)
    ):
        try:
            regenerated = await call_ai_api(
                [
                    {
                        "role": "system",
                        "content": (
                            "把下面这句群聊回复改写成更像群友顺嘴接话的一句。"
                            "不要解释梗结构，不要用“像是把X玩成Y了”“意思就是”这类句式。"
                            "优先吐槽、补半句、顺着气氛接。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"原句：{assistant_text}\n当前群聊原话：{raw_message_text or history_last_text}",
                    },
                ]
            )
            regenerated_text = str(regenerated or "").strip()
            if regenerated_text and not looks_like_explanatory_output(regenerated_text):
                assistant_text = sanitized_regenerated = sanitize_history_text(regenerated_text)
                parsed = {"messages": [{"text": sanitized_regenerated, "sticker": ""}], "think": "", "status": "", "action": ""}
        except Exception as e:
            logger.debug(f"[yaml_response_handler] banter regenerate skipped: {e}")

    if not is_private_session and message_intent == "banter":
        async def _rewrite_for_repeat(cluster_text: str, original_reply: str) -> str:
            return str(
                await call_ai_api(
                    [
                        {
                            "role": "system",
                            "content": (
                                "当前是群聊多人复读/接龙场景。"
                                "请输出一句不超过24字、像群友顺势跟一句的话。"
                                "优先：原句轻微口语化复读；其次：半复读+半句吐槽。"
                                "不要解释梗，不要写分析，不要用问句。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"当前群里反复在说：{cluster_text}\n"
                                f"你原本想回：{original_reply}\n"
                                f"群聊原话：{raw_message_text or history_last_text}"
                            ),
                        },
                    ]
                )
                or ""
            ).strip()

        assistant_text, _repeat_follow_used = await maybe_follow_repeat_cluster(
            reply_text=assistant_text,
            repeat_clusters=repeat_clusters,
            group_id=group_id,
            raw_message_text=raw_message_text or history_last_text,
            message_intent=message_intent,
            is_private_session=is_private_session,
            is_random_chat=False,
            is_direct_mention=is_direct_mention,
            has_newer_batch=_has_newer_batch_now(),
            rewrite_reply=_rewrite_for_repeat,
        )
        if assistant_text:
            parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}

    should_review_agent_reply = bool(used_agent and tool_image_urls and not _IMAGE_B64_RE.search(assistant_text or ""))
    if used_agent and not should_review_agent_reply:
        review_decision = make_passthrough_review_decision(
            assistant_text,
            reason="agent_passthrough",
        )
    else:
        review_decision = await review_response_text(
            lite_call_ai_api,
            candidate_text=assistant_text,
            raw_message_text=raw_message_text or history_last_text or trigger_reason,
            recent_context=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            recent_bot_replies=recent_bot_replies,
            message_intent=message_intent,
            is_private=is_private_session,
            is_random_chat=is_random_chat,
            semantic_frame=semantic_frame,
        )
    if review_decision.action == "no_reply":
        recovered_reply = await _recover_direct_mention_reply_now()
        if recovered_reply:
            logger.info(
                f"拟人插件 (YAML)：回复审阅对直呼消息选择沉默，改用 LLM 补答，group={group_id} user={user_id}"
            )
            assistant_text = sanitize_history_text(recovered_reply)
            parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}
        else:
            logger.info(f"拟人插件 (YAML)：回复审阅后选择沉默，group={group_id} user={user_id}")
            return
    if review_decision.action == "rewrite" and review_decision.text:
        assistant_text = sanitize_history_text(review_decision.text.strip())
        parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}

    assistant_text, history_image_payloads = _extract_image_b64_markers(assistant_text)
    has_generated_image = bool(history_image_payloads)
    if history_image_payloads and not assistant_text:
        assistant_text = "[发送了一张图片]"

    sticker_dir = resolve_sticker_dir(getattr(plugin_config, "personification_sticker_path", None))
    chosen_sticker_paths: list[Path | None] = []
    if (
        parsed["messages"]
        and bool(getattr(semantic_frame, "sticker_appropriate", True))
        and group_config.get("sticker_enabled", True)
        and sticker_dir.exists()
        and sticker_dir.is_dir()
    ):
        feedback_state = await load_sticker_feedback()
        for msg in parsed["messages"]:
            requested_sticker = str(msg.get("sticker", "") or "").strip()
            should_try_sticker = bool(requested_sticker)
            if not should_try_sticker and random.random() < float(getattr(plugin_config, "personification_sticker_probability", 0.0) or 0.0):
                should_try_sticker = True
            if not should_try_sticker:
                chosen_sticker_paths.append(None)
                continue
            chosen_sticker = await choose_sticker_for_context(
                sticker_dir,
                mood=str(getattr(semantic_frame, "sticker_mood_hint", "") or assistant_text or "淡定|表达疑惑"),
                context=f"用户刚说：{raw_message_text or history_last_text or trigger_reason}\n你准备回：{assistant_text or msg.get('text', '')}",
                draft_reply=str(msg.get("text", "") or assistant_text or "").strip()[:120],
                current_visual_summary=_strip_injected_visual_summary(image_summary_suffix),
                proactive=bool(is_random_chat),
                plugin_config=plugin_config,
                call_ai_api=call_ai_api,
                preferred_sticker=requested_sticker,
                minimum_score=1 if requested_sticker else 2,
                feedback_state=feedback_state,
            )
            chosen_sticker_paths.append(chosen_sticker)
        stickers_sent = [path.stem for path in chosen_sticker_paths if path is not None]
    elif parsed["messages"]:
        chosen_sticker_paths = [None for _ in parsed["messages"]]

    if _has_newer_batch_now():
        logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
        return

    sent_as_tts = False
    sent_message_id = ""
    if (
        assistant_text
        and not has_generated_image
        and not stickers_sent
        and tts_service is not None
    ):
        try:
            tts_user_hint = _build_tts_user_hint(is_private=is_private_session)
            persona_tts = extract_persona_tts_config(prompt_config)
            tts_decision = await tts_service.decide_tts_delivery(
                text=assistant_text,
                is_private=is_private_session,
                group_config=group_config,
                has_rich_content=has_generated_image,
                command_triggered=False,
                raw_message_text=raw_message_text or history_last_text or trigger_reason,
                recent_context=recent_context_hint,
                relationship_hint=relationship_hint,
                semantic_frame=semantic_frame,
                fallback_style_hint=str(getattr(semantic_frame, "tts_style_hint", "") or ""),
                persona_tts=persona_tts,
            )
            if tts_decision.action == "voice":
                sent_as_tts = await tts_service.send_tts(
                    bot=bot,
                    event=event,
                    message_segment_cls=message_segment_cls,
                    text=assistant_text,
                    style_hint=tts_decision.style_hint,
                    user_hint=tts_user_hint,
                    is_private=is_private_session,
                    persona_tts=persona_tts,
                    pause_range=(0.8, 1.5),
                )
        except Exception as e:
            logger.warning(f"[tts] YAML 自动语音发送失败，回退文字: {e}")

    if not sent_as_tts:
        clean_reply = ""
        if parsed["messages"]:
            for msg in parsed["messages"]:
                text, image_b64_payloads = _extract_image_b64_markers(msg["text"])
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
                            if _has_newer_batch_now():
                                logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                                return
                            send_result = await bot.send(event, seg)
                            if not sent_message_id:
                                sent_message_id = extract_send_message_id(send_result)
                            await asyncio.sleep(random.uniform(0.4, 1.0))

                for image_b64 in image_b64_payloads:
                    if _has_newer_batch_now():
                        logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                        return
                    send_result = await bot.send(event, message_segment_cls.image(f"base64://{image_b64}"))
                    if not sent_message_id:
                        sent_message_id = extract_send_message_id(send_result)
                    await asyncio.sleep(random.uniform(0.4, 1.0))

                chosen_sticker_path = chosen_sticker_paths.pop(0) if chosen_sticker_paths else None
                if chosen_sticker_path is not None:
                    try:
                        if _has_newer_batch_now():
                            logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                            return
                        send_result = await bot.send(event, message_segment_cls.image(f"file:///{chosen_sticker_path.absolute()}"))
                        if not sent_message_id:
                            sent_message_id = extract_send_message_id(send_result)
                        await record_sticker_sent(chosen_sticker_path.stem)
                        mark_pending_sticker_reaction(
                            build_sticker_feedback_scene_key(
                                group_id=group_id,
                                user_id=user_id,
                                is_private=is_private_session,
                            ),
                            chosen_sticker_path.stem,
                        )
                    except Exception as e:
                        logger.error(f"发送表情包失败: {e}")
        else:
            clean_reply = reply_content
            for tag in ["status", "think", "action", "output", "message"]:
                clean_reply = re.sub(rf"<{tag}.*?>.*?</\s*{tag}\s*>", "", clean_reply, flags=re.DOTALL | re.IGNORECASE)
                clean_reply = re.sub(rf"</?\s*{tag}.*?>", "", clean_reply, flags=re.IGNORECASE)
            clean_reply = _strip_control_markers(clean_reply)
            clean_reply, image_b64_payloads = _extract_image_b64_markers(clean_reply)
            if clean_reply:
                if _has_newer_batch_now():
                    logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                    return
                send_result = await bot.send(event, clean_reply)
                if not sent_message_id:
                    sent_message_id = extract_send_message_id(send_result)
            for image_b64 in image_b64_payloads:
                if _has_newer_batch_now():
                    logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                    return
                send_result = await bot.send(event, message_segment_cls.image(f"base64://{image_b64}"))
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
    try:
        await update_emotion_state_after_turn(
            data_dir,
            user_id=user_id,
            group_id="" if is_private_session else group_id,
            semantic_frame=semantic_frame,
            assistant_text=assistant_text,
            is_private=is_private_session,
        )
    except Exception as e:
        logger.debug(f"[emotion] YAML update after reply failed: {e}")
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
    record_counter(
        "yaml_reply.success_total",
        scene="private" if is_private_session else "group",
        via="tts" if sent_as_tts else "text",
        sticker=bool(stickers_sent),
    )
    record_timing(
        "yaml_reply.total_ms",
        (time.monotonic() - started_at) * 1000.0,
        scene="private" if is_private_session else "group",
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
    lite_call_ai_api: Callable[..., Awaitable[Any]] | None = None,
    superusers: set[str] | None = None,
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]] | None = None,
    tool_registry: Any = None,
    agent_tool_caller: Any = None,
    lite_tool_caller: Any = None,
    vision_caller: Any = None,
    tts_service: Any = None,
    extract_forward_content: Callable[..., Any] = None,
    memory_curator: Any = None,
    knowledge_store: Any = None,
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
        **runtime_overrides: Any,
    ) -> None:
        try:
            from .. import yaml_response_handler as _yaml_response_handler
        except Exception:
            process_fn = process_yaml_response_logic
        else:
            process_fn = getattr(_yaml_response_handler, "process_yaml_response_logic", process_yaml_response_logic)

        return await process_fn(
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
            lite_call_ai_api=lite_call_ai_api,
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
            get_configured_api_providers=runtime_overrides.get(
                "get_configured_api_providers",
                get_configured_api_providers,
            ),
            tool_registry=runtime_overrides.get("tool_registry", tool_registry),
            agent_tool_caller=runtime_overrides.get("agent_tool_caller", agent_tool_caller),
            lite_tool_caller=runtime_overrides.get("lite_tool_caller", lite_tool_caller),
            current_image_urls=current_image_urls,
            vision_caller=runtime_overrides.get("vision_caller", vision_caller),
            tts_service=runtime_overrides.get("tts_service", tts_service),
            extract_forward_content=runtime_overrides.get(
                "extract_forward_content",
                extract_forward_content,
            ),
            memory_curator=runtime_overrides.get("memory_curator", memory_curator),
            knowledge_store=runtime_overrides.get("knowledge_store", knowledge_store),
            disable_network_hooks=bool(runtime_overrides.get("disable_network_hooks", False)),
            batched_events=list(runtime_overrides.get("batched_events") or []),
            repeat_clusters=list(runtime_overrides.get("repeat_clusters") or []),
            batch_event_count=int(runtime_overrides.get("batch_event_count", 1) or 1),
            message_intent=str(runtime_overrides.get("message_intent", "") or ""),
            raw_message_text=str(runtime_overrides.get("raw_message_text", "") or ""),
            is_random_chat=bool(runtime_overrides.get("is_random_chat", False)),
            message_target=str(runtime_overrides.get("message_target", "") or ""),
            intent_ambiguity_level=str(runtime_overrides.get("intent_ambiguity_level", "") or ""),
            intent_recommend_silence=runtime_overrides.get("intent_recommend_silence"),
            recent_context_hint=str(runtime_overrides.get("recent_context_hint", "") or ""),
            relationship_hint=str(runtime_overrides.get("relationship_hint", "") or ""),
            has_newer_batch=bool(runtime_overrides.get("has_newer_batch", False)),
            batch_runtime_ref=runtime_overrides.get("batch_runtime_ref"),
            solo_speaker_follow=bool(runtime_overrides.get("solo_speaker_follow", False)),
        )

    return _processor
