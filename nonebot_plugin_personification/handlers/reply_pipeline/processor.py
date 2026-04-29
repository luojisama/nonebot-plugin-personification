import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

import httpx
from nonebot.exception import FinishedException

from ...core.chat_intent import looks_like_explanatory_output
from ...core.error_utils import log_exception
from ...core.image_input import (
    is_image_input_unsupported_error,
    normalize_image_detail,
    normalize_image_input_mode,
)
from ...core.metrics import record_counter, record_timing
from ...core.message_parts import build_user_message_content, clone_messages_with_text_suffix
from ...core.message_relations import extract_send_message_id
from ...core.context_policy import compress_context_if_needed
from ...core.prompt_hooks import HookContext, get_hook_registry
from ...core.group_relations import summarize_group_relationships
from ...core.group_context import render_group_context_structured
from ...core.group_mute import refresh_bot_group_mute_state
from ...core.group_roles import extract_sender_role
from ...core.target_inference import TARGET_OTHERS, infer_message_target
from ...core.tts_service import extract_persona_tts_config
from ...core.repeat_follow import maybe_follow_repeat_cluster
from ...core.reply_style_policy import build_direct_visual_identity_guard
from ...core.response_review import (
    is_agent_reply_ooc,
    make_passthrough_review_decision,
    recover_direct_mention_reply,
    rewrite_agent_reply_ooc,
    review_response_text,
)
from ...core.visual_capabilities import VISUAL_ROUTE_AGENT, VISUAL_ROUTE_REPLY_PLAIN
from ...skills.skillpacks.sticker_tool.scripts.impl import (
    reset_current_image_context,
    set_current_image_context,
)
from ...core.proactive_store import update_group_chat_active
from ...core.sticker_feedback import (
    build_sticker_feedback_scene_key,
    mark_pending_sticker_reaction,
    record_sticker_sent,
    review_pending_sticker_reaction,
)
from ...core.web_grounding import extract_forward_message_content
from ...utils import build_group_context_window, get_recent_group_msgs
from ..event_rules import _extract_recordable_group_message, split_segment_if_long
from .pipeline_context import (
    batch_has_newer_messages as _batch_has_newer_messages,
    build_base_system_prompt as _build_base_system_prompt,
    build_final_visible_reply_text as _build_final_visible_reply_text,
    build_group_session_relation_metadata as _build_group_session_relation_metadata,
    build_tts_user_hint as _build_tts_user_hint,
    count_user_interactions as _count_user_interactions,
    extract_reply_sender_meta as _extract_reply_sender_meta,
    get_primary_provider_signature as _get_primary_provider_signature,
    looks_like_photo_message as _looks_like_photo_message,
    looks_like_sticker_message as _looks_like_sticker_message,
    primary_route_supports_vision as _primary_route_supports_vision,
    private_history_window_limit as _private_history_window_limit,
    restore_current_user_message_content as _restore_current_user_message_content,
    run_agent_if_enabled as _run_agent_if_enabled,
    should_suppress_group_topic_loop as _should_suppress_group_topic_loop,
    should_use_agent_for_reply as _should_use_agent_for_reply,
    stale_reply_abort_reason as _stale_reply_abort_reason,
    strip_injected_visual_summary as _strip_injected_visual_summary,
    truncate_at_punctuation as _truncate_at_punctuation,
)
from .pipeline_emotion import (
    persist_reply_emotion_state,
    prepare_reply_semantics,
    should_speak_in_random_chat,
)
from .pipeline_sticker import (
    IncomingStickerCandidate,
    build_image_summary_suffix as _build_image_summary_suffix,
    extract_images_from_segment as _extract_images_from_segment,
    extract_mface_from_segment as _extract_mface_from_segment,
    extract_reply_images as _extract_reply_images,
    maybe_choose_reply_sticker,
    spawn_auto_collect_stickers as _spawn_auto_collect_stickers,
)

def _task_exc_logger(label: str, logger: Any) -> Any:
    def _cb(task: Any) -> None:
        if not task.cancelled():
            try:
                exc = task.exception()
                if exc is not None:
                    logger.warning(f"[bg_task:{label}] unhandled exception: {exc!r}")
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass
    return _cb


_FALLBACK_REPLIES = [
    "啊，我突然脑子有点空白...等一下再问我？",
    "这个问题我需要想想，稍后回你",
    "哦，刚才走神了，你刚说什么来着",
]

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


def _record_muted_group_message(
    *,
    event: Any,
    runtime: Any,
    persona: Any,
    bot_self_id: str,
) -> None:
    if bool(getattr(event, "_personification_muted_recorded", False)):
        return
    raw_msg, image_count, visual_summary = _extract_recordable_group_message(event)
    if not raw_msg or raw_msg.startswith("/") or len(raw_msg) >= 500:
        return
    user_id = str(getattr(event, "user_id", "") or "")
    sender = getattr(event, "sender", None)
    nickname = (
        getattr(sender, "card", None)
        or getattr(sender, "nickname", None)
        or user_id
    )
    custom_title = persona.get_custom_title(user_id)
    if custom_title:
        nickname = custom_title
    from ...core.message_relations import build_event_relation_metadata

    runtime.record_group_msg(
        str(getattr(event, "group_id", "") or ""),
        str(nickname or user_id),
        raw_msg,
        is_bot=bool(bot_self_id and user_id == bot_self_id),
        user_id=user_id,
        sender_role=extract_sender_role(event),
        image_count=image_count,
        visual_summary=visual_summary,
        **build_event_relation_metadata(
            event,
            bot_self_id=bot_self_id,
            source_kind="bot" if bot_self_id and user_id == bot_self_id else "user",
        ),
    )
    try:
        setattr(event, "_personification_muted_recorded", True)
    except Exception:
        pass


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
    superusers: set[str]
    get_configured_api_providers: Callable[[], List[Dict[str, Any]]]
    should_avoid_interrupting: Callable[[str, bool], bool]
    module_instance_id: int
    process_yaml_response_logic: Callable[..., Any]
    plugin_config: Any
    get_current_time: Callable[[], Any]
    format_time_context: Callable[[Any | None], str]
    schedule_disabled_override_prompt: Callable[[], str]
    get_schedule_prompt_injection: Callable[[], str]
    build_grounding_context: Callable[[str], Any]
    update_private_interaction_time: Callable[[str], None]
    call_ai_api: Callable[..., Any]
    save_plugin_runtime_config: Callable[[], None] | None
    user_blacklist: Dict[str, float]
    record_group_msg: Callable[..., None]
    split_text_into_segments: Callable[[str], List[str]]
    message_segment_cls: Any
    get_sticker_files: Callable[[], List[Path]]
    get_http_client: Callable[[], httpx.AsyncClient]
    get_whitelisted_groups: Callable[[], List[str]]
    tts_service: Any = None
    tool_registry: Any = None
    inner_state_updater: Any = None
    agent_tool_caller: Any = None
    lite_tool_caller: Any = None
    lite_call_ai_api: Any = None
    persona_store: Any = None
    vision_caller: Any = None
    knowledge_store: Any = None
    memory_store: Any = None
    profile_service: Any = None
    memory_curator: Any = None
    background_intelligence: Any = None


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


def _should_regenerate_for_banter(
    *,
    reply_content: str,
    state: Dict[str, Any],
    is_private_session: bool,
    is_random_chat: bool,
    raw_message_text: str,
    message_intent: str = "",
) -> bool:
    if is_private_session:
        return False
    if not looks_like_explanatory_output(reply_content):
        return False
    return str(message_intent or "").strip() == "banter"


async def process_response_logic(bot: Any, event: Any, state: Dict[str, Any], deps: ReplyProcessorDeps) -> None:
    session = deps.session
    persona = deps.persona
    runtime = deps.runtime
    types = deps.types
    started_at = time.monotonic()

    if hasattr(event, "message_id") and runtime.is_msg_processed(event.message_id):
        return

    is_poke = False
    user_id = ""
    group_id: Any = 0
    message_content = ""
    message_text = ""
    raw_message_text = ""
    sender_name = ""
    trigger_reason = ""
    image_urls: List[str] = []
    sticker_candidates: List[IncomingStickerCandidate] = []
    stop_reply_due_to_gif = [False]
    is_direct_mention = False
    http_client = runtime.get_http_client()
    disable_network_hooks = bool(state.get("disable_network_hooks", False))
    batched_events = list(state.get("batched_events") or [])
    batch_trigger = dict(state.get("batch_trigger") or {})
    repeat_clusters = list(state.get("repeat_clusters") or [])
    batch_event_count = int(state.get("batch_event_count", 1) or 1)

    is_random_chat = state.get("is_random_chat", False)
    force_mode = state.get("force_mode", None)
    group_idle_active = state.get("group_idle_active")
    is_group_idle_active = False
    group_idle_topic = ""
    if isinstance(group_idle_active, dict):
        active_until = float(group_idle_active.get("until", 0) or 0)
        if active_until > time.time():
            is_group_idle_active = True
            group_idle_topic = str(group_idle_active.get("topic", "") or "").strip()
    active_followup = state.get("active_followup")
    is_active_followup = False
    followup_topic = ""
    if isinstance(active_followup, dict):
        followup_until = float(active_followup.get("until", 0) or 0)
        if followup_until > time.time():
            is_active_followup = True
            followup_topic = str(active_followup.get("topic", "") or "").strip()
    solo_speaker_follow = state.get("solo_speaker_follow")
    is_solo_speaker_follow = isinstance(solo_speaker_follow, dict) and bool(solo_speaker_follow)
    solo_follow_topic = str((solo_speaker_follow or {}).get("topic", "") or "").strip() if isinstance(solo_speaker_follow, dict) else ""

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

        bot_self_id = str(getattr(bot, "self_id", "") or "")
        if isinstance(event, types.group_message_event_cls):
            try:
                muted = await refresh_bot_group_mute_state(
                    bot,
                    str(group_id),
                    logger=runtime.logger,
                )
            except Exception as exc:
                runtime.logger.debug(f"[reply_processor] bot mute check failed: {exc}")
                muted = False
            if muted:
                if not is_random_chat:
                    _record_muted_group_message(
                        event=event,
                        runtime=runtime,
                        persona=persona,
                        bot_self_id=bot_self_id,
                    )
                runtime.logger.info(f"拟人插件：群 {group_id} 中 bot 处于禁言期，本轮跳过回复生成。")
                return

        message_text_parts: List[str] = []
        source_message = state.get("concatenated_message", event.message)
        if bot_self_id:
            try:
                for seg in source_message:
                    if getattr(seg, "type", None) != "at":
                        continue
                    qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
                    if qq == bot_self_id:
                        is_direct_mention = True
                        break
            except Exception:
                is_direct_mention = False
        for seg in source_message:
            if seg.type == "text":
                message_text_parts.append(seg.data.get("text", ""))
            elif seg.type == "face":
                face_id = seg.data.get("id", "")
                message_text_parts.append(f"[表情id:{face_id}]")
            elif seg.type == "mface":
                await _extract_mface_from_segment(
                    seg,
                    http_client=http_client,
                    message_text_ref=message_text_parts,
                    image_urls=image_urls,
                    sticker_candidates_ref=sticker_candidates,
                    logger=runtime.logger,
                    stop_reply_ref=stop_reply_due_to_gif,
                )
            elif seg.type == "image":
                await _extract_images_from_segment(
                    seg,
                    runtime=runtime,
                    http_client=http_client,
                    message_text_ref=message_text_parts,
                    image_urls=image_urls,
                    sticker_candidates_ref=sticker_candidates,
                    logger=runtime.logger,
                    stop_reply_ref=stop_reply_due_to_gif,
                )
            elif seg.type == "gif":
                # OneBot 独立 gif 消息段，直接忽略，不下载，不传给视觉模型
                runtime.logger.info("拟人插件：检测到 gif 消息段，忽略并不予回复")
                stop_reply_due_to_gif[0] = True

        if not image_urls and source_message is not event.message:
            try:
                for seg in event.message:
                    if getattr(seg, "type", None) == "image":
                        await _extract_images_from_segment(
                            seg,
                            runtime=runtime,
                            http_client=http_client,
                            message_text_ref=message_text_parts,
                            image_urls=image_urls,
                            sticker_candidates_ref=sticker_candidates,
                            logger=runtime.logger,
                            stop_reply_ref=stop_reply_due_to_gif,
                        )
            except Exception as e:
                runtime.logger.warning(f"回退解析原始消息图片失败: {e}")

        if stop_reply_due_to_gif[0]:
            runtime.logger.info("拟人插件：GIF 信号命中，整条消息跳过本轮回复。")
            return

        reply = getattr(event, "reply", None)
        if reply:
            reply_msg = getattr(reply, "message", None) or (reply.get("message") if isinstance(reply, dict) else None)
            if reply_msg:
                reply_sender_name, reply_is_bot = _extract_reply_sender_meta(reply)
                message_text_parts.append(
                    f"\n[引用内容|发送者:{reply_sender_name}|类型:{'机器人消息' if reply_is_bot else '群成员消息'}]: "
                )
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
                                    stop_reply_ref=stop_reply_due_to_gif,
                                )
                except Exception as e:
                    runtime.logger.warning(f"处理引用消息失败: {e}")

        if stop_reply_due_to_gif[0]:
            runtime.logger.info("拟人插件：引用消息中的 GIF 信号命中，整条消息跳过本轮回复。")
            return

        try:
            forward_content = await extract_forward_message_content(
                bot,
                event,
                logger=runtime.logger,
            )
        except Exception as e:
            runtime.logger.warning(f"处理聊天记录失败: {e}")
            forward_content = ""
        if forward_content:
            clipped_forward = forward_content[:2000]
            message_text_parts.append("\n[聊天记录]:\n")
            message_text_parts.append(clipped_forward)

        message_text = "".join(message_text_parts)
        raw_message_text = message_text
        message_content = message_text.strip()
        is_private_context = str(group_id).startswith(session.private_session_prefix)
        if isinstance(event, types.private_message_event_cls) and session.looks_like_private_command(message_content):
            runtime.logger.debug(f"拟人插件：私聊命令消息已跳过，用户 {user_id}")
            return
        sticker_feedback_scene = build_sticker_feedback_scene_key(
            group_id=str(group_id),
            user_id=user_id,
            is_private=is_private_context,
        )
        feedback_task = asyncio.create_task(
            review_pending_sticker_reaction(
                sticker_feedback_scene,
                raw_message_text or message_content,
                tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
                logger=runtime.logger,
            )
        )
        feedback_task.add_done_callback(_task_exc_logger("sticker_feedback_review", runtime.logger))

        base_prompt = persona.load_prompt(group_id)
        is_yaml_mode = isinstance(base_prompt, dict)

        if is_yaml_mode:
            if is_poke:
                trigger_reason = "对方戳了戳你。"
            elif is_active_followup:
                trigger_reason = (
                    f"你刚才已经和 {sender_name}({user_id}) 聊上了。"
                    f"当前是在顺着上一轮继续说话，刚才的话题是：{followup_topic or '刚才那段对话'}。"
                    "优先像真人继续接上，不要突然冷掉；只有明显跑题或没必要时才输出 [SILENCE]。"
                )
            elif is_solo_speaker_follow:
                trigger_reason = (
                    f"{sender_name}({user_id}) 已经连续说了一阵。"
                    f"当前话题大致是：{solo_follow_topic or '刚才这串内容'}。"
                    "你可以像群友顺手接一句那样回应，不用太正式；只有明显打断别人或接不上时才 [SILENCE]。"
                )
            elif is_random_chat:
                trigger_reason = (
                    f"你在群里潜水看大家聊天。"
                    f"发言者是 {sender_name}({user_id})，这句话未必是对你说的。"
                    f"只有在对方明显在 cue 你、顺着你的话题聊，或你自然能接上一句时再回复；明显无关或高歧义时才输出 [SILENCE]。"
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
                    if is_active_followup:
                        message_content = (
                            f"[对方正在顺着你刚才的话题继续聊，并发来了一张图片。"
                            f"刚才的话题：{followup_topic or '上一轮对话'}。"
                            "如果图片明显是在接前文，就自然评价一句；否则保持安静]"
                        )
                    elif is_solo_speaker_follow:
                        message_content = (
                            f"[群里 {sender_name} 已经连续说了一阵，并发来了一张图片。"
                            f"当前延续的话题：{solo_follow_topic or '刚才这串内容'}。"
                            "如果图片和前文接得上，就像群友顺手接一句；明显不合适再安静]"
                        )
                    elif is_random_chat:
                        message_content = f"[群里 {sender_name} 发了一张图片，你只是路过看到。要是自然能接一句就接，不然保持安静]"
                    else:
                        message_content = "[对方发送了一张图片，是在对你说话]"
                elif is_active_followup:
                    message_content = (
                        f"[对方正在顺着你刚才的话继续聊，刚才的话题：{followup_topic or '上一轮对话'}。"
                        f"对方现在说：{message_content}]"
                    )
                elif is_solo_speaker_follow:
                    message_content = (
                        f"[群里 {sender_name} 已经连续说了一阵，当前延续的话题大致是：{solo_follow_topic or '刚才那串内容'}。"
                        f"对方现在说：{message_content}。像群友那样顺手接一句；只有明显会打断或接不上时才回复 [SILENCE]]"
                    )
                elif is_random_chat:
                    message_content = f"[群员 {sender_name} 正在和别人聊天：{message_content}。如果这话和你没关系，或者你接不上，再回复 [SILENCE]；自然能插一句时优先短句接话]"
                else:
                    message_content = f"[对方正在直接跟你说：{message_content}]"
    else:
        return

    if not runtime.get_configured_api_providers():
        runtime.logger.warning("拟人插件：未配置可用的 API provider，跳过回复")
        if is_direct_mention:
            try:
                await bot.send(event, "在呢")
            except Exception as exc:
                log_exception(runtime.logger, "[reply_processor] fallback presence reply failed", exc, level="debug")
        return

    user_name = sender_name
    if not message_content and not is_poke and not image_urls:
        return

    if (
        isinstance(event, types.group_message_event_cls)
        and (not is_direct_mention)
        and (not is_active_followup)
        and runtime.should_avoid_interrupting(str(group_id), is_random_chat)
    ):
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
    record_counter(
        "reply_processor.requests_total",
        scene="private" if is_private_session else "group",
        random_chat=bool(is_random_chat),
    )
    recent_group_msgs: List[Dict[str, Any]] = []
    if isinstance(event, types.group_message_event_cls) and not state.get("message_target"):
        recent_group_msgs = get_recent_group_msgs(str(group_id), limit=8, expire_hours=0)
        state["message_target"] = infer_message_target(
            event,
            bot_self_id=str(getattr(bot, "self_id", "") or ""),
            recent_group_msgs=recent_group_msgs,
        )
    session_id = session.build_private_session_id(user_id) if is_private_session else session.build_group_session_id(str(group_id))
    legacy_session_id = None if is_private_session else str(group_id)
    session.ensure_session_history(session_id, legacy_session_id=legacy_session_id)

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

    now = runtime.get_current_time()
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = (
        f"{now.year}年{now.month:02d}月{now.day:02d}日 "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) "
        f"[{runtime.format_time_context(now)}]"
    )

    safe_user_name = user_name.replace(":", "：").replace("\n", " ").strip()
    safe_user_name = f"{safe_user_name}({user_id})"
    msg_prefix = f"[{safe_user_name}]: "
    bot_self_id = str(getattr(bot, "self_id", "") or "")
    incoming_relation_metadata = (
        _build_group_session_relation_metadata(
            event,
            bot_self_id=bot_self_id,
            group_id=str(group_id),
            user_id=user_id,
            source_kind="user",
        )
        if isinstance(event, types.group_message_event_cls)
        else {"user_id": user_id, "source_kind": "user"}
    )
    if state.get("message_target"):
        incoming_relation_metadata["message_target"] = state.get("message_target")
    if isinstance(event, types.group_message_event_cls):
        sender_role = extract_sender_role(event)
        if sender_role:
            incoming_relation_metadata["sender_role"] = sender_role

    tool_image_urls = list(image_urls)
    image_input_mode = normalize_image_input_mode(
        getattr(runtime.plugin_config, "personification_image_input_mode", "auto")
    )
    image_detail = normalize_image_detail(
        getattr(runtime.plugin_config, "personification_image_detail", "auto")
    )
    is_sticker_like = _looks_like_sticker_message(raw_message_text or message_content)
    has_photo_input = _looks_like_photo_message(raw_message_text or message_content)
    direct_image_input = bool(image_urls) and image_input_mode in {"auto", "direct"} and (
        image_input_mode == "direct"
        or _primary_route_supports_vision(runtime, VISUAL_ROUTE_REPLY_PLAIN)
    )
    agent_direct_image_input = bool(image_urls) and image_input_mode in {"auto", "direct"} and (
        image_input_mode == "direct"
        or _primary_route_supports_vision(runtime, VISUAL_ROUTE_AGENT)
    )
    image_summary_suffix = ""
    image_urls_for_text_model = list(image_urls)
    _needs_image_summary = False
    if image_urls:
        if image_input_mode == "disabled":
            image_urls_for_text_model = []
        else:
            if image_input_mode in {"auto", "summary"} and (not direct_image_input or not agent_direct_image_input):
                _needs_image_summary = True
            if not direct_image_input:
                image_urls_for_text_model = []

    hook_ctx = HookContext(
        user_id=user_id,
        user_name=user_name,
        group_id=str(group_id),
        is_private=is_private_session,
        is_random_chat=is_random_chat,
        is_yaml_mode=isinstance(base_prompt, dict),
        is_group_idle_active=is_group_idle_active,
        group_idle_topic=group_idle_topic,
        has_image_input=bool(tool_image_urls),
        message_text=message_text,
        message_content=message_content,
        trigger_reason=trigger_reason,
        batched_events=batched_events,
        batch_trigger=batch_trigger,
        repeat_clusters=repeat_clusters,
        batch_event_count=batch_event_count,
        disable_network_hooks=disable_network_hooks,
        current_time_str=current_time_str,
        session_messages=[],
        messages=[],
        plugin_config=runtime.plugin_config,
        session=session,
        persona=persona,
        runtime=runtime,
        bot=bot,
        event=event,
    )
    await get_hook_registry().run_all(hook_ctx, phase="preprocess")
    message_content = hook_ctx.message_content
    trigger_reason = hook_ctx.trigger_reason

    current_text_message_content = message_content
    current_agent_message_content = message_content
    if not is_private_session and not recent_group_msgs:
        recent_group_msgs = get_recent_group_msgs(str(group_id), limit=8, expire_hours=0)
    relationship_hint = ""
    recent_context_hint = ""
    recent_window: list[dict[str, Any]] = []
    if not is_private_session and recent_group_msgs:
        recent_window = build_group_context_window(
            str(group_id),
            limit=8,
            include_message_ids=[incoming_relation_metadata.get("reply_to_msg_id")],
        )
        recent_context_hint = render_group_context_structured(
            recent_window,
            trigger_msg_id=str(incoming_relation_metadata.get("message_id", "") or ""),
        )
        relationship_hint = summarize_group_relationships(
            recent_window,
            trigger_msg_id=str(incoming_relation_metadata.get("message_id", "") or ""),
            trigger_user_id=user_id,
            bot_self_id=bot_self_id,
        )
    if not is_private_session and sticker_candidates:
        _spawn_auto_collect_stickers(
            runtime=runtime,
            group_id=str(group_id),
            user_id=user_id,
            candidates=sticker_candidates,
            task_exc_logger=_task_exc_logger,
        )
    async def _image_summary_task() -> str:
        if not _needs_image_summary:
            return ""
        return await _build_image_summary_suffix(
            runtime=runtime,
            image_urls=tool_image_urls,
            sticker_like=is_sticker_like,
        )

    image_summary_suffix, prepared_semantics = await asyncio.gather(
        _image_summary_task(),
        prepare_reply_semantics(
            runtime=runtime,
            recent_window=recent_window,
            group_id=str(group_id),
            user_id=user_id,
            is_private_session=is_private_session,
            is_random_chat=is_random_chat,
            is_direct_mention=is_direct_mention,
            raw_message_text=raw_message_text,
            current_agent_message_content=current_agent_message_content,
            recent_context_hint=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            message_target=str(state.get("message_target", "") or ""),
            solo_speaker_follow=is_solo_speaker_follow,
        ),
    )
    if image_summary_suffix and tool_image_urls:
        if not direct_image_input:
            current_text_message_content = (
                f"{current_text_message_content} {image_summary_suffix}".strip()
                if current_text_message_content
                else image_summary_suffix
            )
        if not agent_direct_image_input:
            current_agent_message_content = (
                f"{current_agent_message_content} {image_summary_suffix}".strip()
                if current_agent_message_content
                else image_summary_suffix
            )
    recent_bot_replies = prepared_semantics.recent_bot_replies
    data_dir = prepared_semantics.data_dir
    inner_state = prepared_semantics.inner_state
    emotion_state = prepared_semantics.emotion_state
    semantic_frame = prepared_semantics.semantic_frame
    intent_decision = prepared_semantics.intent_decision
    message_intent = prepared_semantics.message_intent
    arbitration = prepared_semantics.arbitration
    if arbitration == "no_reply":
        runtime.logger.info(
            f"拟人插件：LLM 意图判别认为本轮高歧义且不宜插话，group={group_id} user={user_id}"
        )
        return
    if is_random_chat:
        should_speak = await should_speak_in_random_chat(
            runtime=runtime,
            state=state,
            raw_message_text=raw_message_text or message_text or message_content,
            message_text=message_text,
            message_content=message_content,
            recent_context_hint=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            recent_bot_replies=recent_bot_replies,
            message_intent=message_intent,
            ambiguity_level=intent_decision.ambiguity_level,
            message_target=str(state.get("message_target", "") or ""),
            solo_speaker_follow=is_solo_speaker_follow,
            knowledge_store=runtime.knowledge_store,
        )
        if not should_speak:
            runtime.logger.info(f"拟人插件：随机插话场景被 LLM 否决，group={group_id} user={user_id}")
            return

    current_user_content = build_user_message_content(
        text=f"{msg_prefix}{current_text_message_content}",
        image_urls=image_urls_for_text_model,
        image_detail=image_detail,
    )
    agent_current_user_content = build_user_message_content(
        text=f"{msg_prefix}{current_agent_message_content}",
        image_urls=tool_image_urls if agent_direct_image_input else [],
        image_detail=image_detail,
    )
    session.append_session_message(
        session_id,
        "user",
        current_user_content,
        legacy_session_id=legacy_session_id,
        is_direct=not is_random_chat,
        scene="private" if is_private_session else ("direct" if not is_random_chat else "observe"),
        speaker=safe_user_name,
        **incoming_relation_metadata,
    )

    session_messages = session.sanitize_session_messages(session.get_session_messages(session_id))
    if is_private_session:
        session_messages = session_messages[-_private_history_window_limit(runtime.plugin_config):]
    session_messages_for_model = _restore_current_user_message_content(
        session_messages,
        current_user_content,
    )

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
            session_messages_for_model,
            trigger_reason=trigger_reason,
            current_image_urls=tool_image_urls,
            get_configured_api_providers=runtime.get_configured_api_providers,
            vision_caller=runtime.vision_caller,
            disable_network_hooks=disable_network_hooks,
            batched_events=batched_events,
            repeat_clusters=repeat_clusters,
            batch_event_count=batch_event_count,
            message_intent=message_intent,
            raw_message_text=raw_message_text or message_text,
            is_random_chat=is_random_chat,
            message_target=state.get("message_target"),
            intent_ambiguity_level=intent_decision.ambiguity_level,
            intent_recommend_silence=intent_decision.recommend_silence,
            recent_context_hint=recent_context_hint,
            relationship_hint=relationship_hint,
            semantic_frame=semantic_frame,
            has_newer_batch=_batch_has_newer_messages(state),
            batch_runtime_ref=state.get("batch_runtime_ref"),
            solo_speaker_follow=is_solo_speaker_follow,
        )
        return

    attitude_desc = attitude_desc or "态度普通，像平常一样交流。"
    relation_style = "用自然平衡语气回应。"
    preferred_length = "默认回复 1-2 句。"
    if level_name in {"挚友", "亲密"}:
        relation_style = "适度使用更亲近的称呼或语气词，体现熟悉感。"
        preferred_length = "可以扩展到 2-4 句，增加情感反馈。"
    elif level_name in {"陌生", "路人"}:
        relation_style = "保持礼貌和边界感，避免过度亲昵。"
        preferred_length = "优先 1-2 句，直接回答重点。"
    if is_private_session:
        relation_style += " 私聊场景可更自然连续，不必强调围观感。"

    combined_attitude = f"你对该用户的个人态度是：{attitude_desc}\n关系表达策略：{relation_style}\n长度偏好：{preferred_length}"
    if group_attitude:
        combined_attitude += f"\n当前群聊整体氛围带给你的感受是：{group_attitude}"
    emotion_block = prepared_semantics.emotion_block

    hook_ctx.session_messages = session_messages_for_model
    hook_ctx.semantic_frame = semantic_frame
    prelude_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_prelude")
    context_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_context")
    context_chunks = await compress_context_if_needed(
        context_chunks,
        call_ai_api=runtime.lite_call_ai_api or runtime.call_ai_api,
    )
    postlude_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_postlude")
    plugin_summary = ""
    if runtime.knowledge_store is not None:
        try:
            plugin_summary = runtime.knowledge_store.get_plugin_summary_for_prompt()
        except Exception as exc:
            runtime.logger.debug(f"[plugin_knowledge] prompt summary unavailable: {exc}")
    system_prompt = _build_base_system_prompt(
        base_prompt=base_prompt,
        user_name=user_name,
        level_name=level_name,
        combined_attitude=combined_attitude,
        emotion_block=emotion_block,
        is_private_session=is_private_session,
        prelude_chunks=prelude_chunks,
        context_chunks=context_chunks,
        postlude_chunks=postlude_chunks,
        plugin_summary=plugin_summary,
        has_visual_context=bool(tool_image_urls),
        photo_like=has_photo_input,
    )
    if state.get("message_target") == TARGET_OTHERS:
        system_prompt += (
            "\n[系统提示] 当前消息疑似群友之间的对话，不一定是对你说话。"
            "请判断是否需要回复；只有明显无关、会打断别人或高歧义时再保持沉默（输出 [NO_REPLY]）。"
        )
    if message_intent == "banter" and not is_private_session:
        system_prompt += (
            "\n[系统提示] 当前更像群聊接梗/顺嘴吐槽场景。"
            "优先短句接话、补半句、吐槽或复读，不要把笑点翻译成解释文。"
        )
    if is_solo_speaker_follow and not is_private_session:
        system_prompt += (
            "\n[系统提示] 对方已经连续说了一阵。"
            "这轮更适合像群友顺手接一句，不要太端着；但如果明显会打断别人，仍可 [NO_REPLY]。"
        )
    if intent_decision.ambiguity_level == "high":
        system_prompt += (
            "\n[系统提示] 当前最新名词/对象存在较高歧义。"
            "如果上下文和现有证据不足，请优先承认不确定；群聊里若没人明确在 cue 你，且这轮明显会打断别人时，也可以输出 [NO_REPLY]。"
        )
    if arbitration == "clarify":
        system_prompt += (
            "\n[系统提示] 这轮高歧义但对方像是在直接问你。"
            "优先用一句短澄清问句确认对象或范围，不要硬猜。"
        )
    if has_photo_input:
        system_prompt += (
            "\n[系统提示] 当前消息包含真实照片，可以像群友看到朋友圈一样自然回应图片内容，"
            "不需要等对方先提问。"
        )
    if tool_image_urls:
        system_prompt += build_direct_visual_identity_guard()
    if batch_event_count > 1 and not is_private_session:
        system_prompt += (
            f"\n[系统提示] 当前是同一时间窗内合并的 {batch_event_count} 条群消息。"
            "先理解这一小批消息之间的承接关系，再决定接哪一句。"
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
    messages.extend(session_messages_for_model)
    hook_ctx.messages = messages
    await get_hook_registry().run_all(hook_ctx, phase="message")
    agent_messages = _restore_current_user_message_content(messages, agent_current_user_content)
    friend_request_interaction_count = (
        _count_user_interactions(messages, user_id)
        if not is_private_session and not is_random_chat
        else 0
    )

    async def _call_text_model_with_retry(messages_to_use: List[Dict[str, Any]]) -> str:
        try:
            result = await runtime.call_ai_api(messages_to_use)
        except Exception as exc:
            if not (
                tool_image_urls
                and direct_image_input
                and image_input_mode in {"auto", "direct"}
                and is_image_input_unsupported_error(exc)
            ):
                raise
            runtime.logger.warning("拟人插件：模型不支持图片输入，改用视觉摘要重试...")
            retry_suffix = image_summary_suffix or await _build_image_summary_suffix(
                runtime=runtime,
                image_urls=tool_image_urls,
                sticker_like=is_sticker_like,
            )
            retry_messages = clone_messages_with_text_suffix(messages_to_use, retry_suffix)
            result = await runtime.call_ai_api(retry_messages)
        if not result and tool_image_urls and direct_image_input and image_input_mode in {"auto", "direct"}:
            runtime.logger.warning("拟人插件：图片输入可能不被支持，改用视觉摘要重试...")
            retry_suffix = image_summary_suffix or await _build_image_summary_suffix(
                runtime=runtime,
                image_urls=tool_image_urls,
                sticker_like=is_sticker_like,
            )
            retry_messages = clone_messages_with_text_suffix(messages_to_use, retry_suffix)
            result = await runtime.call_ai_api(retry_messages)
        return result

    recovered_direct_mention_reply: str | None = None

    async def _recover_direct_mention_reply_now() -> str:
        nonlocal recovered_direct_mention_reply
        if recovered_direct_mention_reply is None:
            recovered_direct_mention_reply = await recover_direct_mention_reply(
                runtime.call_ai_api,
                raw_message_text=raw_message_text or message_text or message_content,
                recent_context=recent_context_hint,
                relationship_hint=relationship_hint,
                recent_bot_replies=recent_bot_replies,
                semantic_frame=semantic_frame,
                is_direct_mention=is_direct_mention,
            )
        return recovered_direct_mention_reply

    fallback_model_messages = (
        agent_messages
        if tool_image_urls and agent_direct_image_input and direct_image_input
        else messages
    )

    try:
        if is_private_session:
            try:
                runtime.update_private_interaction_time(user_id)
            except Exception as e:
                runtime.logger.error(f"更新最后交互时间失败: {e}")

        reply_content = None
        used_agent = False
        bypass_length_limits = False
        if _should_use_agent_for_reply(
            plugin_config=runtime.plugin_config,
            tool_registry=runtime.tool_registry,
            agent_tool_caller=runtime.agent_tool_caller,
            message_intent=message_intent,
            ambiguity_level=intent_decision.ambiguity_level,
            is_direct_mention=is_direct_mention,
            has_image_input=bool(tool_image_urls),
        ):
            image_ctx_token = set_current_image_context(tool_image_urls, message_content)
            try:
                try:
                    reply_content, used_agent, bypass_length_limits = await _run_agent_if_enabled(
                        bot=bot,
                        event=event,
                        messages=agent_messages,
                        persona=persona,
                        runtime=runtime,
                        interaction_count=friend_request_interaction_count,
                        current_image_urls=tool_image_urls,
                        trigger_reason=trigger_reason,
                        direct_image_input=agent_direct_image_input,
                        repeat_clusters=repeat_clusters,
                        relationship_hint=relationship_hint,
                        recent_bot_replies=recent_bot_replies,
                        precomputed_intent=intent_decision,
                        started_at=started_at,
                        is_direct_mention=is_direct_mention,
                        response_timeout_seconds=float(
                            getattr(runtime.plugin_config, "personification_response_timeout", 180) or 180
                        ),
                        task_exc_logger=_task_exc_logger,
                    )
                except Exception as exc:
                    if not (
                        tool_image_urls
                        and agent_direct_image_input
                        and image_input_mode in {"auto", "direct"}
                        and is_image_input_unsupported_error(exc)
                    ):
                        raise
                    runtime.logger.warning("拟人插件：Agent 处理图片输入失败，改用基础模型摘要重试...")
                    reply_content = ""
                    used_agent = False
                    bypass_length_limits = False
            finally:
                reset_current_image_context(image_ctx_token)
        if used_agent and reply_content in ("[NO_REPLY]", "<NO_REPLY>"):
            recovered_reply = await _recover_direct_mention_reply_now()
            if recovered_reply:
                runtime.logger.info("拟人插件：Agent 对直呼消息返回 NO_REPLY，改用 LLM 补答。")
                reply_content = recovered_reply
                used_agent = False
                bypass_length_limits = False
            elif is_random_chat:
                runtime.logger.info("拟人插件：Agent 在随机插话场景选择 NO_REPLY，保持沉默。")
                return
            else:
                runtime.logger.info("拟人插件：Agent 返回 NO_REPLY，回退基础模型生成文本回复。")
                used_agent = False
                reply_content = ""
                bypass_length_limits = False
        if not used_agent:
            reply_content = await _call_text_model_with_retry(fallback_model_messages)
            bypass_length_limits = False
            if not reply_content:
                recovered_reply = await _recover_direct_mention_reply_now()
                runtime.logger.warning("拟人插件：未能获取到 AI 回复内容")
                if recovered_reply:
                    reply_content = recovered_reply
                elif is_direct_mention:
                    reply_content = random.choice(_FALLBACK_REPLIES)
                else:
                    return
        elif is_agent_reply_ooc(reply_content):
            rewritten_ooc = await rewrite_agent_reply_ooc(
                tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
                original_text=reply_content,
                persona_system=system_prompt,
            )
            if rewritten_ooc:
                reply_content = rewritten_ooc
            else:
                reply_content = "[SILENCE]"

        stale_reason = _stale_reply_abort_reason(state)
        if stale_reason:
            runtime.logger.info(f"拟人插件：{stale_reason}")
            return

        reply_content = re.sub(r"\[表情:[^\]]*\]", "", reply_content)
        reply_content = re.sub(r"\[发送了表情包:[^\]]*\]", "", reply_content).strip()
        reply_content = re.sub(r"[A-F0-9]{16,}", "", reply_content).strip()
        reply_content = re.sub(r"^(根据你的描述|总的来说|总体来说)[，,:：\s]*", "", reply_content).strip()
        reply_content = re.sub(r"^(如果你需要|如果需要的话)[，,:：\s]*", "", reply_content).strip()
        reply_content = re.sub(r"(?:如果你需要|需要的话).*?$", "", reply_content).strip()
        if (
            not is_private_session
            and _should_suppress_group_topic_loop(reply_content, session_messages)
        ):
            runtime.logger.info(
                f"拟人插件：群 {group_id} 命中重复话题抑制，本轮不继续围绕旧内容展开。"
            )
            if not is_direct_mention and is_random_chat:
                return
            reply_content = "嗯，我知道啦"
        if is_random_chat and _batch_has_newer_messages(state):
            runtime.logger.info(f"拟人插件：会话 {state.get('batch_session_key', group_id)} 已出现更新批次，本轮随机插话降级为静默。")
            return
        if _should_regenerate_for_banter(
            reply_content=reply_content,
            state=state,
            is_private_session=is_private_session,
            is_random_chat=is_random_chat,
            raw_message_text=raw_message_text or message_text,
            message_intent=message_intent,
        ):
            try:
                rewrite_messages = list(messages) + [
                    {
                        "role": "system",
                        "content": (
                            "这是一段群聊接梗场景。"
                            "请只用一句更像群友顺嘴接话的回复重写刚才的回答。"
                            "不要解释梗结构，不要用“像是把X玩成Y了”“意思就是”这类句式。"
                            "优先吐槽、补半句、顺着气氛接。"
                        ),
                    }
                ]
                regenerated = await _call_text_model_with_retry(rewrite_messages)
                if regenerated and not looks_like_explanatory_output(regenerated):
                    reply_content = regenerated.strip()
            except Exception as e:
                runtime.logger.debug(f"[reply_processor] banter regenerate skipped: {e}")
        has_block_marker = "[BLOCK]" in reply_content or "<BLOCK>" in reply_content
        if has_block_marker:
            reply_content = reply_content.replace("[BLOCK]", "").replace("<BLOCK>", "").strip()

        has_silence_marker = "[SILENCE]" in reply_content or "<SILENCE>" in reply_content
        if has_silence_marker:
            recovered_reply = await _recover_direct_mention_reply_now()
            runtime.logger.info(f"AI 决定结束与群 {group_id} 中 {user_name}({user_id}) 的对话 (SILENCE)")
            if recovered_reply:
                reply_content = recovered_reply
            else:
                return

        if used_agent and ("[NO_REPLY]" in reply_content or "<NO_REPLY>" in reply_content):
            recovered_reply = await _recover_direct_mention_reply_now()
            if recovered_reply:
                runtime.logger.info("拟人插件：Agent 文本对直呼消息返回 NO_REPLY，改用 LLM 补答。")
                reply_content = recovered_reply
                used_agent = False
                bypass_length_limits = False
            elif is_random_chat:
                return
            else:
                runtime.logger.info("拟人插件：Agent 文本含 NO_REPLY 标记，回退基础模型重试。")
                reply_content = await _call_text_model_with_retry(fallback_model_messages)
                bypass_length_limits = False
                if not reply_content:
                    recovered_reply = await _recover_direct_mention_reply_now()
                    runtime.logger.warning("拟人插件：Agent 回退基础模型后仍无回复内容")
                    if recovered_reply:
                        reply_content = recovered_reply
                    elif is_direct_mention:
                        reply_content = random.choice(_FALLBACK_REPLIES)
                    else:
                        return

        if has_block_marker:
            runtime.logger.warning(
                f"[BLOCK] 检测到高风险内容标记，当前仅忽略本轮回复: group={group_id} user={user_id}"
            )
            notify_superusers = getattr(runtime, "superusers", None) or set()
            if notify_superusers:
                notify_msg = (
                    "拟人插件高风险提示\n"
                    f"群：{group_id}\n"
                    f"用户：{user_name}（{user_id}）\n"
                    f"原始文字：{(raw_message_text or message_text or '')[:60]}\n"
                    f"处理后内容：{(message_content or '')[:100]}\n"
                    f"时间：{runtime.get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
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
                            runtime.logger.warning(f"[BLOCK] 通知管理员 {_su} 失败: {_e}")

                _t = asyncio.create_task(_notify_superusers())
                _t.add_done_callback(_task_exc_logger("notify_superusers", runtime.logger))
            return

        if not used_agent and ("[NO_REPLY]" in reply_content or "<NO_REPLY>" in reply_content):
            runtime.logger.info(
                f"AI 选择不回复群 {group_id} 中 {user_name}({user_id}) 的消息 (NO_REPLY)"
            )
            return

        has_good_atmosphere = "[氛围好]" in reply_content or "<氛围好>" in reply_content
        if has_good_atmosphere:
            reply_content = reply_content.replace("[氛围好]", "").replace("<氛围好>", "").strip()
            if persona.sign_in_available:
                try:
                    is_private_context = str(group_id).startswith("private_")
                    if not is_private_context:
                        group_key = f"group_{group_id}"
                        group_data = persona.get_user_data(group_key)

                        today = runtime.get_current_time().strftime("%Y-%m-%d")
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

        has_interesting = "[有趣]" in reply_content
        if has_interesting:
            reply_content = reply_content.replace("[有趣]", "").strip()
            if persona.sign_in_available:
                try:
                    user_data = persona.get_user_data(user_id)
                    today = runtime.get_current_time().strftime("%Y-%m-%d")

                    last_fav_date = user_data.get("last_interesting_date", "")
                    daily_interesting_count = float(user_data.get("daily_interesting_count", 0.0))
                    if last_fav_date != today:
                        daily_interesting_count = 0.0

                    DAILY_LIMIT = 5.0
                    INCREMENT = 0.05

                    if daily_interesting_count < DAILY_LIMIT:
                        current_fav = float(user_data.get("favorability", 0.0))
                        new_fav = round(current_fav + INCREMENT, 2)
                        daily_interesting_count = round(daily_interesting_count + INCREMENT, 2)
                        persona.update_user_data(
                            user_id,
                            favorability=new_fav,
                            daily_interesting_count=daily_interesting_count,
                            last_interesting_date=today,
                        )
                        runtime.logger.info(
                            f"AI 觉得与 {user_name}({user_id}) 聊天有趣，"
                            f"好感度 +{INCREMENT} (今日已加: {daily_interesting_count:.2f}/{DAILY_LIMIT:.1f})"
                        )
                except Exception as e:
                    runtime.logger.error(f"增加用户好感度失败: {e}")

        if not is_private_session and message_intent == "banter":
            async def _rewrite_for_repeat(cluster_text: str, original_reply: str) -> str:
                rewrite_messages = list(messages) + [
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
                            f"群聊原话：{raw_message_text or message_text or message_content}"
                        ),
                    },
                ]
                return await _call_text_model_with_retry(rewrite_messages)

            reply_content, _repeat_follow_used = await maybe_follow_repeat_cluster(
                reply_text=reply_content,
                repeat_clusters=repeat_clusters,
                group_id=str(group_id),
                raw_message_text=raw_message_text or message_text or message_content,
                message_intent=message_intent,
                is_private_session=is_private_session,
                is_random_chat=is_random_chat,
                is_direct_mention=is_direct_mention,
                has_newer_batch=_batch_has_newer_messages(state),
                rewrite_reply=_rewrite_for_repeat,
            )

        should_review_agent_reply = bool(used_agent and tool_image_urls and not _IMAGE_B64_RE.search(reply_content or ""))
        if used_agent and not should_review_agent_reply:
            review_decision = make_passthrough_review_decision(
                reply_content,
                reason="agent_passthrough",
            )
        else:
            review_decision = await review_response_text(
                runtime.lite_call_ai_api or runtime.call_ai_api,
                candidate_text=reply_content,
                raw_message_text=raw_message_text or message_text or message_content,
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
                runtime.logger.info(
                    f"拟人插件：回复审阅对直呼消息选择沉默，改用 LLM 补答，group={group_id} user={user_id}"
                )
                reply_content = recovered_reply
            else:
                runtime.logger.info(f"拟人插件：回复审阅后选择沉默，group={group_id} user={user_id}")
                return
        if review_decision.action == "rewrite" and review_decision.text:
            reply_content = review_decision.text.strip()

        stale_reason = _stale_reply_abort_reason(state)
        if stale_reason:
            runtime.logger.info(f"拟人插件：{stale_reason}")
            return

        group_config = persona.get_group_config(str(group_id))
        sticker_segment, sticker_name = await maybe_choose_reply_sticker(
            runtime=runtime,
            group_id=str(group_id),
            group_config=group_config,
            semantic_frame=semantic_frame,
            reply_content=reply_content,
            raw_message_text=raw_message_text,
            message_text=message_text,
            message_content=message_content,
            image_summary_suffix=image_summary_suffix,
            is_private_session=is_private_session,
            is_random_chat=is_random_chat,
            is_group_idle_active=is_group_idle_active,
            force_mode=force_mode,
            strip_injected_visual_summary=_strip_injected_visual_summary,
        )

        bot_nickname = persona.default_bot_nickname or str(bot.self_id)
        if isinstance(event, types.group_message_event_cls):
            try:
                bot_member_info = await bot.get_group_member_info(
                    group_id=event.group_id,
                    user_id=int(bot.self_id),
                )
                bot_nickname = bot_member_info.get("card") or bot_member_info.get("nickname") or bot_nickname
            except Exception as exc:
                log_exception(runtime.logger, "[reply_processor] get_group_member_info failed", exc, level="debug")
        final_reply = reply_content.strip()
        max_chars = 0 if bypass_length_limits else getattr(runtime.plugin_config, "personification_max_output_chars", 0)
        final_reply, image_b64_payloads = _extract_image_b64_markers(final_reply)
        if max_chars and max_chars > 0 and len(final_reply) > max_chars:
            final_reply = _truncate_at_punctuation(final_reply, max_chars)
        # session/history 只记录最终对用户生效的文本，避免原始长回复与实际可见内容漂移。
        final_visible_reply_text = _build_final_visible_reply_text(
            final_reply or ("[发送了一张图片]" if image_b64_payloads else ""),
            max_chars=max_chars,
            sanitize_history_text=session.sanitize_history_text,
        )
        sent_message_id = ""
        sent_as_tts = False
        tts_service = getattr(runtime, "tts_service", None)
        stale_reason = _stale_reply_abort_reason(state)
        if stale_reason:
            runtime.logger.info(f"拟人插件：{stale_reason}")
            return
        if (
            final_reply
            and not sticker_segment
            and tts_service is not None
        ):
            try:
                group_style = persona.get_group_style(str(group_id))
                tts_user_hint = _build_tts_user_hint(
                    is_private=is_private_session,
                    group_style=group_style,
                )
                persona_tts = extract_persona_tts_config(base_prompt)
                tts_decision = await tts_service.decide_tts_delivery(
                    text=final_reply,
                    is_private=is_private_session,
                    group_config=group_config,
                    has_rich_content=bool(image_b64_payloads),
                    command_triggered=False,
                    raw_message_text=raw_message_text or message_text or message_content,
                    recent_context=recent_context_hint,
                    relationship_hint=relationship_hint,
                    group_style=group_style,
                    semantic_frame=semantic_frame,
                    fallback_style_hint=str(getattr(semantic_frame, "tts_style_hint", "") or ""),
                    persona_tts=persona_tts,
                )
                if tts_decision.action == "voice":
                    sent_as_tts = await tts_service.send_tts(
                        bot=bot,
                        event=event,
                        message_segment_cls=runtime.message_segment_cls,
                        text=final_reply,
                        style_hint=tts_decision.style_hint,
                        user_hint=tts_user_hint,
                        is_private=is_private_session,
                        group_style=group_style,
                        persona_tts=persona_tts,
                        pause_range=(1.2, 2.0),
                    )
            except Exception as e:
                runtime.logger.warning(f"[tts] 自动语音发送失败，回退文字: {e}")
        if final_reply:
            if not sent_as_tts:
                segments = runtime.split_text_into_segments(final_reply)
                max_seg = getattr(runtime.plugin_config, "personification_max_segment_chars", 0)
                if max_seg and max_seg > 0:
                    expanded: List[str] = []
                    for seg in segments:
                        expanded.extend(split_segment_if_long(seg, max_seg))
                    segments = expanded
                if not segments:
                    segments = [final_reply]
                for i, seg in enumerate(segments):
                    if not seg.strip():
                        continue
                    stale_reason = _stale_reply_abort_reason(state)
                    if stale_reason:
                        runtime.logger.info(f"拟人插件：{stale_reason}")
                        return
                    send_result = await bot.send(event, seg)
                    if not sent_message_id:
                        sent_message_id = extract_send_message_id(send_result)
                    if i < len(segments) - 1 or sticker_segment:
                        await asyncio.sleep(random.uniform(0.8, 1.6))

        for image_b64 in image_b64_payloads:
            stale_reason = _stale_reply_abort_reason(state)
            if stale_reason:
                runtime.logger.info(f"拟人插件：{stale_reason}")
                return
            send_result = await bot.send(event, runtime.message_segment_cls.image(f"base64://{image_b64}"))
            if not sent_message_id:
                sent_message_id = extract_send_message_id(send_result)
            if sticker_segment:
                await asyncio.sleep(random.uniform(0.8, 1.6))

        if sticker_segment:
            stale_reason = _stale_reply_abort_reason(state)
            if stale_reason:
                runtime.logger.info(f"拟人插件：{stale_reason}")
                return
            send_result = await bot.send(event, sticker_segment)
            if not sent_message_id:
                sent_message_id = extract_send_message_id(send_result)
            if sticker_name:
                await record_sticker_sent(sticker_name)
                mark_pending_sticker_reaction(
                    build_sticker_feedback_scene_key(
                        group_id=str(group_id),
                        user_id=user_id,
                        is_private=is_private_session,
                    ),
                    sticker_name,
                )

        assistant_metadata = {
            "scene": "reply",
            "sticker_sent": sticker_name if sticker_name else None,
            "speaker": bot_nickname,
            "user_id": bot_self_id or None,
            "source_kind": "bot_reply",
        }
        await persist_reply_emotion_state(
            runtime=runtime,
            data_dir=data_dir,
            user_id=user_id,
            group_id=str(group_id),
            semantic_frame=semantic_frame,
            assistant_text=final_visible_reply_text,
            is_private=is_private_session,
        )
        if isinstance(event, types.group_message_event_cls):
            assistant_metadata.update(
                {
                    "group_id": str(event.group_id),
                    "message_id": sent_message_id or None,
                    "reply_to_msg_id": incoming_relation_metadata.get("message_id"),
                    "reply_to_user_id": user_id,
                    "mentioned_ids": [],
                    "is_at_bot": False,
                }
            )
        session.append_session_message(
            session_id,
            "assistant",
            final_visible_reply_text,
            legacy_session_id=legacy_session_id,
            **assistant_metadata,
        )
        if getattr(runtime, "memory_curator", None) is not None:
            runtime.memory_curator.schedule_capture(
                summary=final_visible_reply_text,
                user_id=user_id,
                group_id="" if is_private_session else str(group_id),
                topic_tags=[str(group_id)] if not is_private_session else [],
            )

        if isinstance(event, types.group_message_event_cls):
            runtime.record_group_msg(
                str(event.group_id),
                bot_nickname,
                final_visible_reply_text,
                is_bot=True,
                user_id=bot_self_id,
                message_id=sent_message_id or None,
                reply_to_msg_id=incoming_relation_metadata.get("message_id"),
                reply_to_user_id=user_id,
                source_kind="bot_reply",
            )
            try:
                update_group_chat_active(
                    str(event.group_id),
                    user_id=user_id,
                    topic=raw_message_text or message_text or final_visible_reply_text,
                    active_minutes=int(
                        getattr(runtime.plugin_config, "personification_group_chat_active_minutes", 8)
                    ),
                )
            except Exception as e:
                runtime.logger.debug(f"[reply_processor] update_group_chat_active failed: {e}")
        record_counter(
            "reply_processor.success_total",
            scene="private" if is_private_session else "group",
            via="tts" if sent_as_tts else "text",
            sticker=bool(sticker_name),
        )
        record_timing(
            "reply_processor.total_ms",
            (time.monotonic() - started_at) * 1000.0,
            scene="private" if is_private_session else "group",
        )
    except FinishedException:
        raise
    except Exception as e:
        record_counter("reply_processor.error_total")
        runtime.logger.error(f"拟人插件 API 调用失败: {e}")
        if is_direct_mention:
            try:
                await bot.send(event, random.choice(_FALLBACK_REPLIES))
            except Exception as exc:
                log_exception(runtime.logger, "[reply_processor] fallback direct mention send failed", exc, level="debug")
