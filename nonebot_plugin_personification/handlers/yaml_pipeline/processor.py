import asyncio
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List

from ...agent.inner_state import get_personification_data_dir
from ...agent.runtime.planner import (
    turn_plan_from_semantic_frame,
    turn_plan_to_semantic_frame,
)
from ...agent.runtime.tool_catalog import registry_planner_metadata
from ...core.chat_intent import (
    looks_like_explanatory_output,
)
from ...core.current_group_context_tool import register_current_group_context_tool
from ...core.group_member_avatar_insight import register_group_member_avatar_insight_tool
from ...core.emotion_state import (
    build_turn_emotion_prompt_block,
    render_emotion_memory_hint,
    render_inner_state_hint,
    update_emotion_state_after_turn,
)
from ...core.favorability_turn import (
    build_favorability_turn_id,
    commit_favorability_turn,
    extract_legacy_favorability_markers,
    signals_from_semantic_frame,
)
from ...core.group_context import (
    build_group_conversation_context,
    render_group_conversation_context,
    render_plugin_episode_trace_detail,
    render_topic_state_trace_detail,
)
from ...core.metrics import record_counter, record_timing
from ...core.meme_reply_policy import format_meme_turn_prompt, prepare_meme_turn_context
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
from ...core.context_policy import (
    ensure_prompt_injection_guard,
    has_silence_control_marker,
    strip_response_control_markers,
)
from ...core.gemini_profile import (
    build_gemini_route_policy_prompt,
    primary_route_signature,
    should_enable_default_builtin_search,
)
from ...core.repeat_follow import maybe_follow_repeat_cluster
from ...core.reply_style_policy import (
    build_direct_visual_identity_guard,
    build_directed_exchange_policy_prompt,
    build_plugin_interaction_policy_prompt,
    build_reply_style_policy_prompt,
    build_speech_act_policy_prompt,
)
from ...core.role_integrity import detect_persona_identity_leak
from ...core.response_review import (
    ReplyArbitrationIntent,
    arbitrate_reply_mode,
    extract_recent_bot_reply_texts,
    is_agent_reply_ooc,
    make_passthrough_review_decision,
    needs_uncertain_visible_reply_review,
    required_reply_fallback_text,
    required_reply_needs_recovery,
    resolve_uncertain_visible_reply,
    rewrite_agent_reply_ooc,
    review_response_text,
)
from ...core.send_outcome import is_likely_delivered_send_timeout
from ...core.target_inference import normalize_message_target_for_plan, normalize_message_target_for_review
from ...core.reply_text_policy import normalize_visible_reply_text
from ...core.reply_completion_contract import resolve_sent_reply_completion
from ...core.prompt_loader import pick_ack_phrase
from ...core.qq_outbound import QQOutboundLedger, SendReceipt, build_outbound_context
from ...core.qq_recall import register_qq_recall_tool
from ...core.qq_expression_library import (
    build_qq_expression_prompt,
    contains_qq_expression_marker,
    history_text_for_qq_expression,
    maybe_choose_auto_qq_expression_marker,
    qq_expression_enabled,
    render_qq_expression_message,
)
from ...core.qq_expression_tools import register_send_qq_expression_tools
from ...core.sticker_feedback import (
    build_sticker_feedback_scene_key,
    load_sticker_feedback,
    mark_pending_sticker_reaction,
    record_sticker_sent,
)
from ...core.target_inference import TARGET_OTHERS
from ...core.tts_service import extract_persona_tts_config
from ...core.visible_output import guard_visible_text
from ...core.turn_media import (
    attach_safe_visual_summary,
    coerce_turn_media,
    extract_media_from_message,
    extract_turn_media_from_event,
    media_summary_timeout_seconds,
    normalize_safe_visual_summary,
    render_turn_media_grounding,
)
from ...core.visual_capabilities import VISUAL_ROUTE_AGENT, VISUAL_ROUTE_REPLY_YAML
from ...core.user_avatar_insight import (
    add_current_user_avatar_planner_metadata,
    register_current_user_avatar_tool,
)
from ...core.user_avatar_pair_insight import (
    build_avatar_pair_candidates,
    filter_avatar_candidates_by_policy,
    register_group_user_avatar_pair_insight_tool,
)
from ...skill_runtime.runtime_api import SkillRuntime

from ...agent.action_executor import ActionExecutor
from ...agent.loop import run_agent
from ...agent.query_rewriter import QueryRewriteContext
from ..reply_pipeline.pipeline_emotion import (
    attach_turn_plan_to_semantic_frame,
    compose_reply_emotion_block,
    infer_turn_semantic_frame_with_timeout,
    load_reply_states_with_timeout,
    plan_turn_with_timeout,
    schedule_inner_state_update_after_reply,
    semantic_frame_timeout_hint,
)
from ..reply_pipeline import humanize as _humanize
from ..reply_commit import (
    acquire_reply_commit,
    begin_reply_lifecycle,
    execute_pending_actions,
    mark_reply_phase,
    mark_reply_delivery_complete,
    mark_reply_delivery_confirmed,
    mark_reply_delivery_started,
    release_reply_commit,
)
from ..reply_pipeline.pipeline_context import (
    batch_has_newer_messages as _shared_batch_has_newer_messages,
    clone_tool_registry as _clone_tool_registry,
    compute_agent_time_budget as _compute_agent_time_budget,
    dispatch_reply_part as _dispatch_reply_part,
    guard_reply_ack_text as _guard_reply_ack_text,
    build_reply_operation_id as _build_reply_operation_id,
    primary_route_supports_vision as _runtime_primary_route_supports_vision,
    should_use_agent_for_reply as _should_use_agent_for_reply,
    strip_injected_visual_summary as _strip_injected_visual_summary,
)
from ..reply_pipeline.pipeline_sticker import build_image_summary_suffix as _shared_build_image_summary_suffix
from ...skills.skillpacks.sticker_tool.scripts.impl import (
    build_send_sticker_tool,
    choose_sticker_for_context,
    reset_current_image_context,
    set_current_image_context,
)
from ...skills.skillpacks.resource_collector.scripts.main import build_send_image_tools
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


async def _send_translation_forward(
    bot: Any,
    event: Any,
    text: str,
    *,
    qq_outbound_ledger: QQOutboundLedger | None = None,
    operation_id: str = "",
    user_target: str = "",
    logger: Any = None,
) -> bool | SendReceipt:
    guarded_text = guard_visible_text(
        text,
        logger=logger,
        surface="reply_translation_forward",
        allow_direct_media=False,
        allow_control=False,
    )
    visible_text = normalize_visible_reply_text(strip_response_control_markers(guarded_text))
    if not visible_text:
        return False
    grouped = _group_translation_result(visible_text)
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

    async def _send() -> Any:
        if hasattr(event, "group_id"):
            return await bot.call_api(
                "send_group_forward_msg",
                group_id=event.group_id,
                messages=nodes,
            )
        return await bot.call_api(
            "send_private_forward_msg",
            user_id=event.user_id,
            messages=nodes,
        )

    if qq_outbound_ledger is not None:
        outbound_context = build_outbound_context(
            bot=bot,
            event=event,
            surface="reply_translation_forward",
            operation_id=operation_id,
            user_target=user_target,
        )
        return await qq_outbound_ledger.dispatch(outbound_context, nodes, _send)
    await _send()
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


def _consume_pending_action_history_text(event: Any) -> str:
    text = str(getattr(event, "_personification_pending_action_history_text", "") or "").strip()
    if text:
        try:
            setattr(event, "_personification_pending_action_history_text", "")
        except Exception:
            pass
    return re.sub(r"\s+", " ", text).strip()


def _normalize_parsed_message_texts(parsed: Dict[str, Any]) -> Dict[str, Any]:
    copied = dict(parsed or {})
    messages: list[dict[str, Any]] = []
    for item in list(copied.get("messages") or []):
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized["text"] = normalize_visible_reply_text(strip_response_control_markers(normalized.get("text", "")))
        messages.append(normalized)
    copied["messages"] = messages
    return copied


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
    review_call_ai_api: Callable[..., Awaitable[Any]] | None = None,
    current_image_urls: List[str] | None = None,
    vision_caller: Any = None,
    tts_service: Any = None,
    extract_forward_content: Callable[..., Any] = None,
    memory_curator: Any = None,
    knowledge_store: Any = None,
    inner_state_updater: Any = None,
    favorability_service: Any = None,
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
    plugin_episode: Any = None,
    semantic_frame: Any = None,
    has_newer_batch: bool = False,
    batch_runtime_ref: Dict[str, Any] | None = None,
    reply_commit_state: Dict[str, Any] | None = None,
    solo_speaker_follow: bool = False,
    reply_required: bool = False,
    response_deadline: float | None = None,
    prepared_inner_state: dict[str, Any] | None = None,
    prepared_emotion_state: dict[str, Any] | None = None,
    turn_media_context: List[Dict[str, Any]] | None = None,
    media_grounding: str = "",
    precomputed_image_summary_suffix: str = "",
    user_profile_block: str = "",
    profile_service: Any = None,
    favorability_context_block: str = "",
    favorability_turn_id: str = "",
    avatar_pair_candidates: List[Dict[str, str]] | None = None,
    avatar_pair_runtime: Any = None,
    user_policy_gate: Any = None,
    qq_outbound_ledger: QQOutboundLedger | None = None,
    reply_trace_id: str = "",
) -> None:
    """处理基于 YAML 模板的新版响应逻辑。"""
    if user_policy_gate is not None and not await user_policy_gate.allows_current(event):
        return
    reply_commit_state = reply_commit_state if isinstance(reply_commit_state, dict) else {}
    outbound_reply_trace_id = str(
        reply_trace_id or reply_commit_state.get("reply_trace_id", "") or ""
    ).strip()
    started_at = time.monotonic()
    begin_reply_lifecycle(reply_commit_state, "yaml_pipeline")
    lite_tool_caller = lite_tool_caller or agent_tool_caller
    lite_call_ai_api = lite_call_ai_api or call_ai_api
    review_call_ai_api = review_call_ai_api or lite_call_ai_api or call_ai_api
    planner_message_target = normalize_message_target_for_plan(message_target)
    review_message_target = normalize_message_target_for_review(message_target)
    turn_media_refs = coerce_turn_media(turn_media_context or [])
    if not turn_media_refs:
        turn_media_refs = extract_turn_media_from_event(event, current_origin="current")

    def _has_newer_batch_now() -> bool:
        return bool(has_newer_batch or _batch_ref_has_newer_messages(batch_runtime_ref))

    def _trace_stage(
        key: str,
        label: str,
        status: str,
        detail: str = "",
        hint: str = "",
        elapsed_ms: int | None = None,
    ) -> None:
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key=key,
                label=label,
                status=status,
                detail=detail,
                hint=hint,
                elapsed_ms=elapsed_ms,
            )
        except Exception:
            pass

    def _trace_finish(outcome: str, diagnosis_code: str, detail: Dict[str, Any] | None = None) -> None:
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.finish_trace(
                outcome=outcome,
                diagnosis_code=diagnosis_code,
                detail=detail or {},
            )
        except Exception:
            pass

    def _trace_no_reply(reason: str, *, diagnosis_code: str = "no_reply", detail: str = "") -> None:
        _trace_stage(
            key="yaml_no_reply",
            label="YAML 未发送",
            status="warn",
            detail=detail or reason,
        )
        _trace_finish(
            outcome="no_reply",
            diagnosis_code=diagnosis_code,
            detail={"reason": reason},
        )

    _trace_stage(
        key="incoming_message",
        label="收到消息",
        status="info",
        detail=str(raw_message_text or "")[:500],
    )

    now = get_current_time()
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = (
        f"{now.year}年{now.month:02d}月{now.day:02d}日 "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) "
        f"[{format_time_context(now)}]"
    )

    is_private_session = str(group_id).startswith(private_session_prefix)
    favorability_signals = signals_from_semantic_frame(
        semantic_frame,
        is_private=is_private_session,
    )
    resolved_favorability_turn_id = str(favorability_turn_id or "").strip() or build_favorability_turn_id(
        message_id=getattr(event, "message_id", ""),
        group_id=group_id,
        user_id=user_id,
    )
    record_counter(
        "yaml_reply.requests_total",
        scene="private" if is_private_session else "group",
        random_chat=bool(is_random_chat),
    )
    _trace_stage(
        key="yaml_start",
        label="YAML 回复开始",
        status="info",
        detail=(
            f"scene={'private' if is_private_session else 'group'} "
            f"user={user_id} group={group_id or '-'} elapsed_ms=0"
        ),
    )
    is_direct_mention = _event_mentions_bot(event, bot)
    pending_action_executor: Any = None
    pending_actions: list[dict[str, Any]] = []
    favorability_committed = False

    def _commit_favorability_if_confirmed() -> None:
        nonlocal favorability_committed
        if favorability_committed or not bool(reply_commit_state.get("reply_delivery_confirmed", False)):
            return
        favorability_committed = True
        try:
            commit_favorability_turn(
                service=favorability_service,
                user_id=user_id,
                group_id=group_id,
                is_private=is_private_session,
                is_direct=bool(is_direct_mention or not is_random_chat),
                is_random_chat=bool(is_random_chat),
                signals=favorability_signals,
                turn_id=resolved_favorability_turn_id,
                now=get_current_time(),
            )
        except Exception as exc:
            logger.debug(f"拟人插件 (YAML)：提交回复好感事件失败: {exc}")

    def _confirm_reply_delivery() -> None:
        mark_reply_delivery_confirmed(reply_commit_state)
        _commit_favorability_if_confirmed()

    async def _send_reply(payload: Any, *, surface: str = "yaml_reply") -> Any:
        if user_policy_gate is not None:
            await user_policy_gate.ensure_current(event)
        mark_reply_delivery_started(reply_commit_state)
        result = await _dispatch_reply_part(
            bot=bot,
            event=event,
            payload=payload,
            ledger=qq_outbound_ledger,
            surface=surface,
            reply_trace_id=outbound_reply_trace_id,
        )
        if not isinstance(result, SendReceipt) or result.status == "sent":
            _confirm_reply_delivery()
        return result

    def _message_id_from_send_result(send_result: Any) -> str:
        if isinstance(send_result, SendReceipt):
            return str(send_result.message_id or "")
        return extract_send_message_id(send_result)

    async def _commit_pending_actions() -> None:
        if not pending_actions:
            return
        if user_policy_gate is not None:
            await user_policy_gate.ensure_current(event)
        mark_reply_phase(reply_commit_state, "delivery_commit_wait")
        await acquire_reply_commit(reply_commit_state)
        mark_reply_phase(reply_commit_state, "delivery")
        if _has_newer_batch_now():
            pending_actions.clear()
            return
        history_parts = await execute_pending_actions(
            pending_action_executor,
            pending_actions,
            state=reply_commit_state,
        )
        _commit_favorability_if_confirmed()
        if history_parts:
            setattr(
                event,
                "_personification_pending_action_history_text",
                " ".join(history_parts),
            )

    def _record_pending_action_history_if_any() -> bool:
        action_history_text = _consume_pending_action_history_text(event)
        if not action_history_text:
            return False
        assistant_history = sanitize_history_text(action_history_text)
        if not assistant_history:
            return False
        session_id = build_private_session_id(user_id) if is_private_session else build_group_session_id(group_id)
        legacy_session_id = None if is_private_session else group_id
        bot_self_id = str(getattr(bot, "self_id", "") or "")
        append_session_message(
            session_id,
            "assistant",
            assistant_history,
            legacy_session_id=legacy_session_id,
            scene="reply",
            sticker_sent=None,
            speaker=bot_self_id or "bot",
            user_id=bot_self_id or None,
            source_kind="bot_reply",
            group_id=None if is_private_session else group_id,
            message_id=None,
            reply_to_msg_id=str(getattr(event, "message_id", "") or "") or None,
            reply_to_user_id=None if is_private_session else user_id,
            mentioned_ids=[],
            is_at_bot=False,
        )
        if not is_private_session and record_group_msg is not None:
            record_group_msg(
                group_id,
                bot_self_id or "bot",
                assistant_history,
                is_bot=True,
                user_id=bot_self_id,
                reply_to_msg_id=str(getattr(event, "message_id", "") or "") or None,
                reply_to_user_id=user_id,
                source_kind="bot_reply",
            )
        return True

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
    if not turn_media_refs and last_images:
        turn_media_refs = extract_media_from_message(
            [
                {"type": "image", "data": {"url": image_ref}}
                for image_ref in last_images
            ],
            origin="current",
            owner_user_id=user_id,
            message_id=str(getattr(event, "message_id", "") or ""),
        )
    image_input_mode = normalize_image_input_mode(
        getattr(plugin_config, "personification_image_input_mode", "auto")
    )
    image_summary_suffix = str(precomputed_image_summary_suffix or "").strip()
    if not image_summary_suffix and last_images and image_input_mode != "disabled":
        summary_timeout = media_summary_timeout_seconds(
            response_deadline if isinstance(response_deadline, (int, float)) else None,
            now=time.monotonic(),
        )
        if summary_timeout > 0.05:
            try:
                image_summary_suffix = await asyncio.wait_for(
                    _build_image_summary_suffix(
                        plugin_config=plugin_config,
                        agent_tool_caller=agent_tool_caller,
                        get_configured_api_providers=get_configured_api_providers,
                        vision_caller=vision_caller,
                        image_urls=last_images,
                        sticker_like=False,
                        logger=logger,
                    ),
                    timeout=summary_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"拟人插件 (YAML)：视觉摘要超过本轮前置预算 {summary_timeout:.1f}s，继续使用 provenance 进入语义判断。"
                )
    safe_visual_summary = normalize_safe_visual_summary(image_summary_suffix)
    turn_media_refs = attach_safe_visual_summary(
        turn_media_refs,
        safe_visual_summary,
        confidence=0.65,
    )
    media_grounding = render_turn_media_grounding(
        turn_media_refs,
        summary=safe_visual_summary,
    ) or str(media_grounding or "").strip()
    photo_like = "[图片·照片]" in (history_last_text or raw_message_text or trigger_reason)

    conversation_context = None
    resolved_plugin_episode = plugin_episode
    if not is_private_session and not recent_context_hint:
        recent_window = build_group_context_window(
            group_id,
            limit=8,
            include_message_ids=[extract_reply_message_id(event)],
        )
        excluded_context_user_ids: set[str] = set()
        if user_policy_gate is not None:
            recent_window, excluded_context_user_ids = (
                await user_policy_gate.filter_context_messages(
                    recent_window,
                    bot_self_id=str(getattr(bot, "self_id", "") or ""),
                )
            )
        conversation_context = build_group_conversation_context(
            recent_messages=recent_window,
            trigger_msg_id=extract_event_message_id(event),
            trigger_user_id=user_id,
            bot_self_id=str(getattr(bot, "self_id", "") or ""),
            repeat_clusters=list(repeat_clusters or []),
            excluded_user_ids=excluded_context_user_ids,
        )
        recent_context_hint = render_group_conversation_context(conversation_context)
        relationship_hint = relationship_hint or conversation_context.relationship_hint
        resolved_plugin_episode = conversation_context.plugin_episode
        topic_detail = render_topic_state_trace_detail(conversation_context.topic_state)
        if topic_detail:
            _trace_stage(
                key="yaml_topic_state",
                label="YAML 短期话题状态",
                status="info",
                detail=topic_detail,
                hint="结构化线索用于判断当前消息接谁的话，不替代 LLM 语义判断",
            )
        plugin_detail = render_plugin_episode_trace_detail(conversation_context.plugin_episode)
        if plugin_detail:
            _trace_stage(
                key="yaml_plugin_episode",
                label="YAML 其它插件交互",
                status="info",
                detail=plugin_detail,
                hint="其它插件输出仅作为带来源的群聊上下文，不等同于人格回复",
            )
    else:
        recent_window = []
    avatar_pair_recent_messages = (
        recent_window
        if recent_window
        else get_recent_group_msgs(group_id, limit=8, expire_hours=0)
        if not is_private_session
        else []
    )
    recent_bot_replies = extract_recent_bot_reply_texts(avatar_pair_recent_messages)
    resolved_avatar_pair_candidates = list(avatar_pair_candidates or []) or build_avatar_pair_candidates(
        event=event,
        current_user_id=user_id,
        current_user_label=user_name,
        bot_self_id=getattr(bot, "self_id", ""),
        batched_events=list(batched_events or []),
        recent_messages=avatar_pair_recent_messages,
    )
    resolved_avatar_pair_candidates = await filter_avatar_candidates_by_policy(
        resolved_avatar_pair_candidates,
        (
            user_policy_gate.current_authorization
            if user_policy_gate is not None
            else None
        ),
    )
    data_dir = get_personification_data_dir(plugin_config)
    if isinstance(prepared_inner_state, dict) and isinstance(prepared_emotion_state, dict):
        inner_state = dict(prepared_inner_state)
        emotion_state = dict(prepared_emotion_state)
        _trace_stage(
            key="yaml_state_reuse",
            label="YAML 复用回复状态",
            status="ok",
            detail="source=prepared_semantics elapsed_ms=0",
        )
    else:
        inner_state, emotion_state = await load_reply_states_with_timeout(
            data_dir,
            logger,
            trace_key="yaml_state_load",
            trace_label="YAML 状态加载",
        )
    emotion_memory_hint = render_emotion_memory_hint(
        emotion_state,
        user_id=user_id,
        group_id="" if is_private_session else group_id,
    )

    turn_plan = getattr(semantic_frame, "turn_plan", None)
    if semantic_frame is None:
        planner_enabled = bool(getattr(plugin_config, "personification_turn_planner_enabled", False))
        planner_shadow_enabled = bool(getattr(plugin_config, "personification_turn_planner_shadow_enabled", False))
        planner_available_tools: list[dict[str, Any]] = []
        if planner_enabled or planner_shadow_enabled:
            try:
                planner_available_tools = registry_planner_metadata(tool_registry)
                planner_available_tools = add_current_user_avatar_planner_metadata(
                    planner_available_tools,
                    profile_service,
                    user_id,
                )
            except Exception:
                planner_available_tools = []
        plan_source_text = raw_message_text or history_last_text or trigger_reason
        if planner_enabled:
            turn_plan, plan_elapsed_ms, plan_fallback_reason, plan_timeout_s, plan_source = await plan_turn_with_timeout(
                plan_source_text,
                plugin_config=plugin_config,
                is_group=not is_private_session,
                is_random_chat=is_random_chat,
                is_direct_mention=is_direct_mention,
                has_images=bool(last_images),
                message_target=planner_message_target,
                tool_caller=lite_tool_caller,
                fallback_tool_caller=agent_tool_caller,
                recent_context=recent_context_hint,
                relationship_hint=relationship_hint,
                repeat_clusters=repeat_clusters,
                current_inner_state=render_inner_state_hint(inner_state),
                current_emotion_state=emotion_memory_hint,
                available_tools=planner_available_tools,
                media_grounding=media_grounding,
                logger=logger,
                metric_mode="yaml_enabled",
            )
            record_counter(
                "turn_planner.plan_total",
                mode="yaml_enabled",
                action=turn_plan.reply_action,
                output_mode=turn_plan.output_mode,
            )
            record_timing("turn_planner.plan_ms", plan_elapsed_ms, mode="yaml_enabled")
            semantic_frame = turn_plan_to_semantic_frame(turn_plan)
            if plan_fallback_reason:
                is_timeout = plan_fallback_reason.endswith("_timeout")
                _trace_stage(
                    key="yaml_turn_plan_timeout" if is_timeout else "yaml_turn_plan_fallback",
                    label="YAML 回合规划超时" if is_timeout else "YAML 回合规划降级",
                    status="warn",
                    detail=(
                        f"timeout_s={plan_timeout_s:g} "
                        f"elapsed_ms={int(plan_elapsed_ms)} "
                        f"reason={plan_fallback_reason} "
                        f"fallback=metadata "
                        f"source={plan_source} "
                        f"action={getattr(turn_plan, 'reply_action', '')} "
                        f"speech_act={getattr(turn_plan, 'speech_act', '')} "
                        f"output={getattr(turn_plan, 'output_mode', '')}"
                    ),
                    hint=semantic_frame_timeout_hint(plan_timeout_s),
                )
        else:
            semantic_frame, semantic_elapsed_ms, semantic_fallback_reason, semantic_timeout_s, semantic_source = (
                await infer_turn_semantic_frame_with_timeout(
                    plan_source_text,
                    plugin_config=plugin_config,
                    is_group=not is_private_session,
                    is_random_chat=is_random_chat,
                    is_direct_mention=is_direct_mention,
                    message_target=planner_message_target,
                    tool_caller=lite_tool_caller,
                    fallback_tool_caller=agent_tool_caller,
                    recent_context=recent_context_hint,
                    relationship_hint=relationship_hint,
                    repeat_clusters=repeat_clusters,
                    current_inner_state=render_inner_state_hint(inner_state),
                    current_emotion_state=emotion_memory_hint,
                    media_grounding=media_grounding,
                    logger=logger,
                    metric_scene="yaml_private" if is_private_session else "yaml_group",
                )
            )
            record_timing(
                "reply.semantic_frame_ms",
                semantic_elapsed_ms,
                scene="yaml_private" if is_private_session else "yaml_group",
            )
            turn_plan = turn_plan_from_semantic_frame(
                semantic_frame,
                has_images=bool(last_images),
                message_target=planner_message_target,
            )
            attach_turn_plan_to_semantic_frame(semantic_frame, turn_plan)
            if semantic_fallback_reason:
                is_timeout = semantic_fallback_reason.endswith("_timeout")
                _trace_stage(
                    key="yaml_semantic_frame_timeout" if is_timeout else "yaml_semantic_frame_fallback",
                    label="YAML 语义帧超时" if is_timeout else "YAML 语义帧降级",
                    status="warn",
                    detail=(
                        f"timeout_s={semantic_timeout_s:g} "
                        f"elapsed_ms={int(semantic_elapsed_ms)} "
                        f"reason={semantic_fallback_reason} "
                        f"fallback=metadata "
                        f"source={semantic_source} "
                        f"intent={getattr(semantic_frame, 'chat_intent', '')} "
                        f"speech_act={getattr(turn_plan, 'speech_act', '')} "
                        f"ambiguity={getattr(semantic_frame, 'ambiguity_level', '')}"
                    ),
                    hint=semantic_frame_timeout_hint(semantic_timeout_s),
                )
            if planner_shadow_enabled:
                shadow_plan, shadow_elapsed_ms, shadow_fallback_reason, shadow_timeout_s, shadow_source = await plan_turn_with_timeout(
                    plan_source_text,
                    plugin_config=plugin_config,
                    is_group=not is_private_session,
                    is_random_chat=is_random_chat,
                    is_direct_mention=is_direct_mention,
                    has_images=bool(last_images),
                    message_target=planner_message_target,
                    tool_caller=lite_tool_caller,
                    fallback_tool_caller=agent_tool_caller,
                    recent_context=recent_context_hint,
                    relationship_hint=relationship_hint,
                    repeat_clusters=repeat_clusters,
                    current_inner_state=render_inner_state_hint(inner_state),
                    current_emotion_state=emotion_memory_hint,
                    available_tools=planner_available_tools,
                    media_grounding=media_grounding,
                    logger=logger,
                    metric_mode="yaml_shadow",
                )
                record_timing("turn_planner.plan_ms", shadow_elapsed_ms, mode="yaml_shadow")
                if shadow_fallback_reason:
                    is_timeout = shadow_fallback_reason.endswith("_timeout")
                    _trace_stage(
                        key="yaml_turn_plan_shadow_timeout" if is_timeout else "yaml_turn_plan_shadow_fallback",
                        label="YAML TurnPlan 影子超时" if is_timeout else "YAML TurnPlan 影子降级",
                        status="warn",
                        detail=(
                            f"timeout_s={shadow_timeout_s:g} "
                            f"elapsed_ms={int(shadow_elapsed_ms)} "
                            f"reason={shadow_fallback_reason} "
                            f"fallback=metadata source={shadow_source}"
                        ),
                        hint=semantic_frame_timeout_hint(shadow_timeout_s),
                    )
                else:
                    record_counter(
                        "turn_planner.plan_total",
                        mode="yaml_shadow",
                        action=shadow_plan.reply_action,
                        output_mode=shadow_plan.output_mode,
                    )
                    if shadow_plan.reply_action != turn_plan.reply_action:
                        record_counter("turn_planner.diff_total", field="yaml_reply_action")
                    if getattr(shadow_plan, "speech_act", "") != getattr(turn_plan, "speech_act", ""):
                        record_counter("turn_planner.diff_total", field="yaml_speech_act")
                    if shadow_plan.output_mode != turn_plan.output_mode:
                        record_counter("turn_planner.diff_total", field="yaml_output_mode")
    if turn_plan is None:
        turn_plan = turn_plan_from_semantic_frame(
            semantic_frame,
            has_images=bool(last_images),
            message_target=planner_message_target,
        )
        attach_turn_plan_to_semantic_frame(semantic_frame, turn_plan)
    intent_decision = semantic_frame.to_intent_decision()
    if not message_intent:
        message_intent = intent_decision.chat_intent
    if not str(intent_ambiguity_level or "").strip():
        intent_ambiguity_level = intent_decision.ambiguity_level
    if intent_recommend_silence is None:
        intent_recommend_silence = intent_decision.recommend_silence
    favorability_signals.merge(
        signals_from_semantic_frame(
            semantic_frame,
            is_private=is_private_session,
        )
    )
    _trace_stage(
        key="yaml_semantic_frame",
        label="YAML 语义帧",
        status="info",
        detail=(
            f"intent={message_intent or '-'} "
            f"speech_act={getattr(turn_plan, 'speech_act', getattr(semantic_frame, 'speech_act', '-')) or '-'} "
            f"ambiguity={intent_ambiguity_level or '-'} "
            f"recommend_silence={bool(intent_recommend_silence)}"
        ),
    )

    arbitration = arbitrate_reply_mode(
        intent_decision=ReplyArbitrationIntent(
            ambiguity_level=str(intent_ambiguity_level or "").strip(),
            recommend_silence=bool(intent_recommend_silence),
        ),
        is_private=is_private_session,
        is_direct_mention=is_direct_mention,
        is_random_chat=is_random_chat,
        message_target=review_message_target,
        solo_speaker_follow=solo_speaker_follow,
    )
    if arbitration == "no_reply":
        logger.info(
            f"拟人插件 (YAML)：LLM 意图判别认为本轮高歧义且不宜插话，group={group_id} user={user_id}"
        )
        _trace_no_reply(
            "arbitration_no_reply",
            detail="LLM 语义判别认为高歧义且不宜插话",
        )
        return
    if is_random_chat and _has_newer_batch_now():
        logger.info(f"拟人插件 (YAML)：随机插话场景较新批次到达，跳过，group={group_id} user={user_id}")
        _trace_no_reply("stale_random_chat", diagnosis_code="stale_reply", detail="随机插话期间出现更新批次")
        return

    meme_turn_prompt = ""
    try:
        meme_turn_context = prepare_meme_turn_context(
            group_id="" if is_private_session else str(group_id),
            message_text=plan_source_text,
            recent_context=recent_context_hint,
            probability=float(getattr(plugin_config, "personification_meme_reply_probability", 0.18) or 0.0),
            semantic_frame=semantic_frame,
            rng=random.random,
        )
        meme_turn_prompt = format_meme_turn_prompt(meme_turn_context)
        record_counter(
            "reply.meme_turn_total",
            allowed=str(bool(meme_turn_context.get("active_use_allowed"))).lower(),
            understood=len(meme_turn_context.get("understanding_senses") or []),
        )
    except Exception as exc:
        logger.debug(f"拟人插件 (YAML)：本轮黑话上下文不可用，按无词典提示继续: {type(exc).__name__}")

    system_prompt = prompt_config.get("system", "")
    if user_profile_block:
        system_prompt += f"\n\n{user_profile_block}"
    if favorability_context_block:
        system_prompt += f"\n\n{favorability_context_block}"
    if meme_turn_prompt:
        system_prompt += f"\n\n{meme_turn_prompt}"
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
                "如果上下文和现有证据不足，非强交互直接保持沉默；"
                "强交互只索取一个明确且对方能提供的必要条件，不要播报无法确认或查证无结果。"
            )
        if arbitration == "clarify":
            system_prompt += "\n- 这轮高歧义但对方像是在直接问你；群聊里不要用澄清问句追问，能判断就给保守短反应，不能判断就保持沉默。"
    system_prompt += (
        "\n\n## 基础输出规则\n"
        "- 输出纯文本，禁止使用 markdown 格式（不要用 **加粗**、*斜体*、# 标题、- 列表符号、`代码块`等）。\n"
        "- 收到贴图、表情包、GIF、截图或真实照片时，先把视觉信息当作内部语境理解；没人明确要求识别、翻译或说明时，不要主动评论、讲解、复述或总结图片/动图内容。\n"
        "- 表情包/梗图/GIF 只当作语气线索；真实照片也只用于判断情绪、关系和意图，最终回复接人和话题，不写成图片说明。如果只看到图片占位或没有视觉摘要，不要假装看懂，也不要泛泛问对方看到什么。\n"
        "- 有人让你“写一段/来一段/AI 一段/帮我写”对白、剧本、小作文、段子、歌词或角色扮演内容时，不要切换成写作工具去交付任务：不写前言铺垫、不写“角色：台词”式多角色剧本、不写结尾点评总结、不堆营业腔和网络黑话。可以用人设口吻即兴接两三句、玩梗式带过，但绝不展开成长篇命题作文，也不要为此出戏或扮演成别的角色。"
    )
    if media_grounding:
        system_prompt += f"\n\n{media_grounding}"
    if qq_expression_enabled(plugin_config):
        system_prompt += "\n\n" + build_qq_expression_prompt()
    system_prompt += "\n\n" + build_reply_style_policy_prompt(
        has_visual_context=bool(last_images),
        photo_like=photo_like,
        is_group=not is_private_session,
    )
    turn_plan_for_prompt = turn_plan
    system_prompt += "\n\n" + build_speech_act_policy_prompt(
        speech_act=str(getattr(turn_plan_for_prompt, "speech_act", getattr(semantic_frame, "speech_act", "")) or ""),
        output_mode=str(getattr(turn_plan_for_prompt, "output_mode", getattr(semantic_frame, "output_mode", "")) or ""),
        session_goal=str(getattr(turn_plan_for_prompt, "session_goal", getattr(semantic_frame, "session_goal", "")) or ""),
        is_group=not is_private_session,
    )
    directed_exchange_prompt = build_directed_exchange_policy_prompt(
        is_direct_mention=is_direct_mention,
        is_group=not is_private_session,
        speech_act=str(getattr(turn_plan_for_prompt, "speech_act", getattr(semantic_frame, "speech_act", "")) or ""),
        output_mode=str(getattr(turn_plan_for_prompt, "output_mode", getattr(semantic_frame, "output_mode", "")) or ""),
    )
    if directed_exchange_prompt:
        system_prompt += "\n\n" + directed_exchange_prompt
    if resolved_plugin_episode is not None:
        system_prompt += "\n\n" + build_plugin_interaction_policy_prompt(
            is_direct_mention=is_direct_mention,
        )
    primary_api_type, primary_model = primary_route_signature(
        plugin_config,
        get_configured_api_providers=get_configured_api_providers,
    )
    gemini_policy = build_gemini_route_policy_prompt(
        api_type=primary_api_type,
        model=primary_model,
        has_visual_context=bool(last_images),
        native_search_enabled=should_enable_default_builtin_search(
            plugin_config,
            get_configured_api_providers=get_configured_api_providers,
        ),
    )
    if gemini_policy:
        system_prompt += "\n\n" + gemini_policy
    if knowledge_store is not None:
        try:
            plugin_summary = knowledge_store.get_plugin_summary_for_prompt()
        except Exception as exc:
            logger.debug(f"[plugin_knowledge] YAML prompt summary unavailable: {exc}")
            plugin_summary = ""
        if plugin_summary:
            system_prompt += f"\n\n[已安装插件摘要（仅供参考）]\n{plugin_summary}"
    system_prompt = ensure_prompt_injection_guard(system_prompt)

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
        schedule_prompt_text = str(group_config.get("schedule_prompt", "") or "").strip()
        system_schedule_instruction = get_schedule_prompt_injection(schedule_prompt_text)
        if schedule_prompt_text:
            schedule_instruction = "2. **时间锚定**：参考【当前时间】和本群自定义作息表判断轻量状态。作息只占背景，不要压过正在聊的内容。"
        else:
            schedule_instruction = "2. **时间锚定**：参考【当前时间】保持时间语义正确；当前未配置具体作息表，不要自动推断上课/上班/睡觉等状态。"
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
    image_detail = normalize_image_detail(
        getattr(plugin_config, "personification_image_detail", "auto")
    )
    sticker_like = (
        "[图片·表情包]" in input_text
        or "[表情id:" in input_text
        or "[表情:" in input_text
        or "[QQ表情" in input_text
        or "[QQ超级表情" in input_text
        or "[QQ收藏表情" in input_text
        or "[QQ推荐表情" in input_text
        or "[表情包]" in input_text
        or "[多张表情]" in input_text
    )
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
    text_model_images = list(last_images)
    if last_images:
        _trace_stage(
            key="yaml_vision_mode",
            label="YAML 视觉输入",
            status="info",
            detail=(
                f"images={len(last_images)} mode={image_input_mode} "
                f"text_direct={bool(direct_image_input)} "
                f"agent_direct={bool(agent_direct_image_input)} elapsed_ms=0"
            ),
        )
        if image_input_mode == "disabled":
            text_model_images = []
        else:
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
            "\n[系统提示] 当前消息包含真实照片。照片只作为内部语境帮助你理解对方的情绪、关系和意图；"
            "除非对方明确要求说明/识别/翻译图片，最终回复不要讲解、复述或总结画面细节。"
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
    agent_result: Any = None
    reply_content = ""
    uncertain_reply_suppress_recovery = False

    def _trace_suppressed_agent_reply(background_detail: str, evidence_detail: str) -> None:
        if str(getattr(agent_result, "quality_context", "") or "") == "evidence_unavailable":
            _trace_no_reply(
                "evidence_unavailable",
                diagnosis_code="evidence_unavailable",
                detail=evidence_detail,
            )
            return
        _trace_no_reply(
            "background_action_pending",
            diagnosis_code="background_action_pending",
            detail=background_detail,
        )

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

    if _should_use_agent_for_reply(
        plugin_config=plugin_config,
        tool_registry=tool_registry,
        agent_tool_caller=agent_tool_caller,
        message_intent=message_intent,
        ambiguity_level=str(intent_ambiguity_level or ""),
        is_direct_mention=is_direct_mention,
        has_image_input=bool(tool_image_urls),
    ):
        executor = ActionExecutor(
            bot,
            event,
            plugin_config,
            logger,
            qq_outbound_ledger=qq_outbound_ledger,
            operation_id=_build_reply_operation_id(
                bot=bot,
                event=event,
                reply_trace_id=outbound_reply_trace_id,
            ),
            user_target=user_id,
            recall_cutoff=float(
                reply_commit_state.get("received_wall_at", 0.0) or time.time()
            ),
        )
        agent_tool_registry = _clone_tool_registry(tool_registry)
        register_qq_recall_tool(
            agent_tool_registry,
            executor=executor,
            bot=bot,
            event=event,
            cutoff=float(
                reply_commit_state.get("received_wall_at", 0.0) or time.time()
            ),
        )
        register_current_user_avatar_tool(agent_tool_registry, profile_service, user_id)
        register_current_group_context_tool(
            agent_tool_registry,
            bot=bot,
            event=event,
            plugin_config=plugin_config,
            logger=logger,
            policy_authorizer=(
                user_policy_gate.current_authorization
                if user_policy_gate is not None
                else None
            ),
        )
        register_group_user_avatar_pair_insight_tool(
            agent_tool_registry,
            runtime=avatar_pair_runtime
            or SimpleNamespace(
                plugin_config=plugin_config,
                get_configured_api_providers=get_configured_api_providers,
            ),
            bot=bot,
            event=event,
            candidates=resolved_avatar_pair_candidates,
            policy_authorizer=(
                user_policy_gate.current_authorization
                if user_policy_gate is not None
                else None
            ),
        )
        register_group_member_avatar_insight_tool(
            agent_tool_registry,
            runtime=avatar_pair_runtime
            or SimpleNamespace(
                plugin_config=plugin_config,
                get_configured_api_providers=get_configured_api_providers,
            ),
            bot=bot,
            event=event,
            candidates=resolved_avatar_pair_candidates,
            policy_authorizer=(
                user_policy_gate.current_authorization
                if user_policy_gate is not None
                else None
            ),
        )
        register_send_qq_expression_tools(
            agent_tool_registry,
            executor=executor,
            bot=bot,
            plugin_config=plugin_config,
        )
        try:
            skill_runtime_for_images = SkillRuntime(
                plugin_config=plugin_config,
                logger=logger,
                get_now=lambda: int(time.time()),
                vision_caller=vision_caller,
                tool_caller=agent_tool_caller,
            )
            for tool in build_send_image_tools(skill_runtime_for_images, executor):
                agent_tool_registry.register(tool)
        except Exception as exc:
            logger.debug(f"拟人插件 (YAML)：注册联网搜图发送工具失败: {exc}")
        try:
            sticker_dir = resolve_sticker_dir(getattr(plugin_config, "personification_sticker_path", None))
            if sticker_dir.exists() and sticker_dir.is_dir():
                agent_tool_registry.register(
                    build_send_sticker_tool(
                        sticker_dir,
                        plugin_config,
                        executor,
                    )
                )
        except Exception as exc:
            logger.debug(f"拟人插件 (YAML)：注册本地表情包发送工具失败: {exc}")
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
                visible_ack = _guard_reply_ack_text(
                    text,
                    fallback=_phrase,
                    logger=logger,
                )
                if not visible_ack:
                    return
                mark_reply_phase(reply_commit_state, "delivery_commit_wait")
                await acquire_reply_commit(reply_commit_state)
                mark_reply_phase(reply_commit_state, "delivery")
                try:
                    # _send_reply keeps the legacy await bot.send(...) fallback without a ledger.
                    if user_policy_gate is not None:
                        await user_policy_gate.ensure_current(event)
                    await _send_reply(
                        visible_ack,
                        surface="reply_ack",
                    )
                finally:
                    release_reply_commit(reply_commit_state)
                    mark_reply_phase(reply_commit_state, "yaml_agent_after_ack")
            ack_sender = _ack_sender
        _trace_stage(
            key="yaml_agent_start",
            label="YAML Agent 开始",
            status="info",
            detail=(
                f"images={len(tool_image_urls)} direct_image={bool(agent_direct_image_input)} "
                "elapsed_ms=0"
            ),
        )
        try:
            try:
                agent_result = await run_agent(
                    messages=agent_messages,
                    registry=agent_tool_registry,
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
                    turn_plan=turn_plan,
                    time_budget_seconds=_compute_agent_time_budget(
                        started_at=started_at,
                        total_timeout_seconds=float(
                            getattr(plugin_config, "personification_response_timeout", 180) or 180
                        ),
                        response_deadline=response_deadline,
                    ),
                    ack_sender=ack_sender,
                    is_group=not is_private_session,
                    is_direct_mention=is_direct_mention,
                    reply_required=reply_required,
                    turn_media_context=turn_media_refs,
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
            social_coverage = dict(getattr(agent_result, "social_coverage", {}) or {})
            reply_commit_state["agent_evidence_delivery_required"] = bool(
                getattr(agent_result, "evidence_delivery_required", False)
            )
            reply_commit_state["agent_evidence_delivery_status"] = str(
                getattr(agent_result, "evidence_delivery_status", "not_required") or "not_required"
            )
            reply_commit_state["agent_evidence_recovered"] = bool(
                getattr(agent_result, "evidence_recovered", False)
            )
            reply_commit_state["agent_social_coverage_status"] = str(
                social_coverage.get("coverage_status", "") or ""
            )
            reply_commit_state["agent_social_tool_execution"] = (
                "partial" if bool(social_coverage.get("partial", False)) else "ok"
                if bool(getattr(agent_result, "social_evidence", None))
                else "not_used"
            )
            agent_failure_code = str(getattr(agent_result, "failure_code", "") or "").strip()
            if agent_failure_code:
                delivery_started = bool(reply_commit_state.get("reply_delivery_started", False))
                delivery_confirmed = bool(reply_commit_state.get("reply_delivery_confirmed", False))
                if delivery_confirmed:
                    delivery_state = "partial"
                    trace_outcome = "partial"
                    diagnosis_code = f"partial_{agent_failure_code}"
                elif delivery_started:
                    delivery_state = "dispatching"
                    trace_outcome = "failed"
                    diagnosis_code = "outbound_send_failed"
                else:
                    delivery_state = "not_started"
                    trace_outcome = "failed"
                    diagnosis_code = agent_failure_code
                logger.warning(
                    f"拟人插件 (YAML)：Agent 基础设施失败，保持静默: code={agent_failure_code} "
                    f"delivery_state={delivery_state}"
                )
                _trace_stage(
                    key="yaml_agent_operational_failure",
                    label="YAML Agent 基础设施失败",
                    status="warn" if delivery_confirmed else "error",
                    detail=f"code={agent_failure_code} delivery_state={delivery_state} silent=true",
                )
                _trace_finish(
                    outcome=trace_outcome,
                    diagnosis_code=diagnosis_code,
                    detail={
                        "silent": True,
                        "delivery_state": delivery_state,
                        "failure_code": agent_failure_code,
                    },
                )
                return
            reply_content = agent_result.text
            used_agent = True
            _trace_stage(
                key="yaml_agent_result",
                label="YAML Agent 结果",
                status="ok" if reply_content else "warn",
                detail=(
                    f"chars={len(str(reply_content or ''))} "
                    f"direct_output={bool(getattr(agent_result, 'direct_output', False))} "
                    f"actions={len(getattr(agent_result, 'pending_actions', []) or [])}"
                ),
                )
            if required_reply_needs_recovery(
                reply_content,
                reply_required=reply_required,
                pending_actions=list(getattr(agent_result, "pending_actions", []) or []),
                direct_output=bool(
                    getattr(agent_result, "direct_output", False)
                    or getattr(agent_result, "suppress_reply_recovery", False)
                ),
            ):
                logger.warning("拟人插件 (YAML)：强交互 Agent 返回静默，改走基础模型恢复。")
                reply_content = ""
                used_agent = False
                agent_result = None
        if agent_result is not None:
            reply_content, legacy_favorability_signals = extract_legacy_favorability_markers(reply_content)
            favorability_signals.merge(legacy_favorability_signals)
            if not agent_result.direct_output and is_agent_reply_ooc(reply_content):
                rewritten_ooc = await rewrite_agent_reply_ooc(
                    tool_caller=lite_tool_caller or agent_tool_caller,
                    original_text=reply_content,
                    persona_system=system_prompt,
                    output_mode=str(getattr(semantic_frame, "output_mode", "chat_short") or "chat_short"),
                    avoid_questions=not is_private_session,
                    allow_rhetorical_banter=bool(
                        is_direct_mention
                        and str(getattr(turn_plan_for_prompt, "speech_act", "") or "") in {"", "participate", "tease"}
                    ),
                )
                if rewritten_ooc:
                    reply_content = rewritten_ooc
                else:
                    reply_content = "[SILENCE]"
            if _has_newer_batch_now():
                logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="Agent 结果生成后出现更新批次")
                return
            pending_action_executor = executor
            pending_actions = list(agent_result.pending_actions)
            if agent_result.direct_output:
                mark_reply_phase(reply_commit_state, "delivery_commit_wait")
                await acquire_reply_commit(reply_commit_state)
                if user_policy_gate is not None:
                    await user_policy_gate.ensure_current(event)
                mark_reply_phase(reply_commit_state, "delivery")
                if _has_newer_batch_now():
                    logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                    _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="直出消息提交前出现更新批次")
                    return
                await _commit_pending_actions()
                raw_direct_output = str(reply_content or "").strip()
                if _looks_like_translation_result(raw_direct_output):
                    try:
                        mark_reply_delivery_started(reply_commit_state)
                        translation_result = await _send_translation_forward(
                            bot,
                            event,
                            raw_direct_output,
                            qq_outbound_ledger=qq_outbound_ledger,
                            operation_id=outbound_reply_trace_id,
                            user_target=user_id,
                            logger=logger,
                        )
                        if translation_result is False:
                            release_reply_commit(reply_commit_state)
                            mark_reply_phase(reply_commit_state, "reply_complete")
                            _trace_no_reply(
                                "unsafe_visible_output",
                                diagnosis_code="blocked",
                                detail="翻译转发内容被安全门拦截",
                            )
                            return
                        translation_confirmed = (
                            not isinstance(translation_result, SendReceipt)
                            or translation_result.status == "sent"
                        )
                        if translation_confirmed:
                            _confirm_reply_delivery()
                            mark_reply_delivery_complete(reply_commit_state)
                            release_reply_commit(reply_commit_state)
                            mark_reply_phase(reply_commit_state, "reply_complete")
                            _trace_stage(
                                key="yaml_direct_output_success",
                                label="YAML 直出完成",
                                status="ok",
                                detail="translation_forward",
                            )
                            _trace_finish(
                                outcome="ok",
                                diagnosis_code="ok",
                                detail={"direct_output": True, "kind": "translation_forward"},
                            )
                            return
                        delivery_unknown = True
                        release_reply_commit(reply_commit_state)
                        mark_reply_phase(reply_commit_state, "reply_complete")
                        _trace_finish(
                            outcome="failed",
                            diagnosis_code="outbound_send_failed",
                            detail={"direct_output": True, "kind": "translation_forward"},
                        )
                        return
                    except Exception as e:
                        if qq_outbound_ledger is not None:
                            delivery_partial = bool(
                                reply_commit_state.get("reply_delivery_confirmed", False)
                            )
                            delivery_unknown = not delivery_partial
                            logger.warning(
                                f"拟人插件: 翻译结果转发发送结果未知，禁止自动回退重发: {e}"
                            )
                            release_reply_commit(reply_commit_state)
                            mark_reply_phase(reply_commit_state, "reply_complete")
                            _trace_finish(
                                outcome="partial" if delivery_partial else "failed",
                                diagnosis_code=(
                                    "partial_reply_timeout"
                                    if delivery_partial
                                    else "outbound_send_failed"
                                ),
                                detail={"direct_output": True, "kind": "translation_forward"},
                            )
                            return
                        logger.warning(f"拟人插件: 翻译结果转发发送失败，回退到普通消息: {e}")
                raw_direct_output = normalize_visible_reply_text(strip_response_control_markers(raw_direct_output))
                if raw_direct_output:
                    raw_direct_output = guard_visible_text(
                        raw_direct_output,
                        logger=logger,
                        surface="yaml_direct_output",
                        allow_direct_media=False,
                        allow_control=False,
                    )
                if not raw_direct_output:
                    action_confirmed = bool(reply_commit_state.get("reply_delivery_confirmed", False))
                    if action_confirmed:
                        mark_reply_delivery_complete(reply_commit_state)
                    release_reply_commit(reply_commit_state)
                    mark_reply_phase(reply_commit_state, "reply_complete")
                    if action_confirmed:
                        _trace_finish(
                            outcome="ok",
                            diagnosis_code="blocked",
                            detail={"direct_output": True, "visible_text_blocked": True, "actions_sent": True},
                        )
                    else:
                        _trace_no_reply(
                            "unsafe_visible_output",
                            diagnosis_code="blocked",
                            detail="YAML 直出内容为空或被安全门拦截",
                        )
                    return
                direct_segments_sent = 0
                for seg in re.split(r"(?:\r?\n){2,}", raw_direct_output):
                    text = seg.strip()
                    if text:
                        if _has_newer_batch_now():
                            logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                            _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="直出消息发送前出现更新批次")
                            return
                        await _send_reply(text)
                        direct_segments_sent += 1
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                _trace_stage(
                    key="yaml_direct_output_success",
                    label="YAML 直出完成",
                    status="ok",
                    detail=f"segments={direct_segments_sent}",
                )
                mark_reply_delivery_complete(reply_commit_state)
                release_reply_commit(reply_commit_state)
                mark_reply_phase(reply_commit_state, "reply_complete")
                _trace_finish(
                    outcome="ok",
                    diagnosis_code="ok",
                    detail={"direct_output": True, "segments": direct_segments_sent},
                )
                return
    if not used_agent:
        reply_content = await _call_text_model_with_retry(messages)
        _trace_stage(
            key="yaml_model_result",
            label="YAML 模型结果",
            status="ok" if reply_content else "warn",
            detail=f"chars={len(str(reply_content or ''))} images={len(text_model_images)}",
        )
    if not reply_content:
        logger.warning("拟人插件 (YAML): 未能获取到 AI 回复内容")
        if reply_required:
            reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
        else:
            _trace_no_reply("empty_model_reply", diagnosis_code="model_empty", detail="模型返回空内容")
            return
    if not used_agent and needs_uncertain_visible_reply_review(
        ambiguity_level=intent_ambiguity_level,
        persona_response_info_added=getattr(
            semantic_frame,
            "persona_response_info_added",
            "",
        ),
    ):
        uncertain_started_at = time.monotonic()
        uncertain_timeout = 8.0
        if isinstance(response_deadline, (int, float)):
            uncertain_timeout = min(
                uncertain_timeout,
                max(0.0, float(response_deadline) - time.monotonic()),
            )
        uncertain_decision = await resolve_uncertain_visible_reply(
            review_call_ai_api or call_ai_api,
            candidate_text=reply_content,
            raw_message_text=raw_message_text or history_last_text or trigger_reason,
            persona_system=system_prompt,
            turn_plan=turn_plan,
            reply_required=reply_required,
            is_private=is_private_session,
            evidence_unavailable=False,
            timeout=uncertain_timeout,
        )
        if uncertain_decision.action == "request_context" and uncertain_decision.text:
            reply_content = uncertain_decision.text.strip()
        elif uncertain_decision.action == "silence":
            reply_content = "[SILENCE]"
            uncertain_reply_suppress_recovery = True
        _trace_stage(
            key="yaml_uncertain_reply_review",
            label="YAML 高歧义回复收口",
            status="ok" if uncertain_decision.action in {"accept", "request_context"} else "warn",
            detail=(
                f"action={uncertain_decision.action} "
                f"flags={','.join(uncertain_decision.flags) or '-'} "
                f"elapsed_ms={int((time.monotonic() - uncertain_started_at) * 1000)}"
            ),
        )
    if _has_newer_batch_now():
        logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
        _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="模型回复生成后出现更新批次")
        return
    if required_reply_needs_recovery(
        reply_content,
        reply_required=reply_required,
        pending_actions=pending_actions,
        direct_output=bool(
            uncertain_reply_suppress_recovery
            or (
                agent_result is not None
                and (
                    getattr(agent_result, "direct_output", False)
                    or getattr(agent_result, "suppress_reply_recovery", False)
                )
            )
        ),
    ):
        reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
    if used_agent and reply_content in ("[NO_REPLY]", "<NO_REPLY>"):
        logger.info("拟人插件 (YAML)：Agent 返回 NO_REPLY，保持沉默。")
        _trace_no_reply("agent_no_reply", detail="Agent 返回 NO_REPLY")
        return

    reply_content, legacy_favorability_signals = extract_legacy_favorability_markers(reply_content)
    favorability_signals.merge(legacy_favorability_signals)
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
        if reply_required:
            reply_content = "这个我不能接。"
            parsed = parse_yaml_response(reply_content)
        else:
            _trace_no_reply("block_marker", diagnosis_code="blocked", detail="模型返回 BLOCK 控制标记")
            return
    if has_silence_control_marker(reply_content):
        await _commit_pending_actions()
        if _record_pending_action_history_if_any():
            logger.info("拟人插件 (YAML)：Agent 静默动作已写入会话历史。")
        logger.info("AI (YAML) 决定保持沉默 (SILENCE)")
        if bool(reply_commit_state.get("reply_delivery_confirmed", False)):
            mark_reply_delivery_complete(reply_commit_state)
            release_reply_commit(reply_commit_state)
            mark_reply_phase(reply_commit_state, "reply_complete")
            _trace_finish(outcome="ok", diagnosis_code="ok", detail={"action_only": True})
            return
        if uncertain_reply_suppress_recovery:
            _trace_no_reply(
                "uncertain_reply_silenced",
                diagnosis_code="uncertain_reply_silenced",
                detail="高歧义候选没有实质内容或具体补充请求",
            )
            return
        if bool(getattr(agent_result, "suppress_reply_recovery", False)):
            _trace_suppressed_agent_reply(
                "后台动作已启动，状态回复保持静默",
                "当前没有足够可用证据，状态回复保持静默",
            )
            return
        if reply_required:
            reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
            parsed = parse_yaml_response(reply_content)
        else:
            _trace_no_reply("silence_marker", detail="模型返回 SILENCE 控制标记")
            return

    status_text = str(parsed.get("status") or "").strip()
    action_text = str(parsed.get("action") or "").strip()
    pending_yaml_poke = bool(schedule_active and "戳一戳" in action_text)

    async def _commit_yaml_poke() -> bool:
        nonlocal pending_yaml_poke
        if not pending_yaml_poke:
            return True
        await acquire_reply_commit(reply_commit_state)
        if user_policy_gate is not None:
            await user_policy_gate.ensure_current(event)
        if _has_newer_batch_now():
            _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="YAML 动作提交前出现更新批次")
            return False
        try:
            await _send_reply(message_segment_cls.poke(int(user_id)))
            pending_yaml_poke = False
        except Exception as exc:
            logger.warning(f"拟人插件: 发送戳一戳失败: {exc}")
        return True

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
        not used_agent
        and message_intent == "banter"
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
            is_random_chat=is_random_chat,
            is_direct_mention=is_direct_mention,
            has_newer_batch=_has_newer_batch_now(),
            rewrite_reply=_rewrite_for_repeat,
        )
        if assistant_text:
            parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}

    care_review_required = bool(
        getattr(semantic_frame, "requires_emotional_care", False)
        or getattr(getattr(semantic_frame, "emotional_support", None), "needed", False)
    )
    should_review_visual_reply = bool(turn_media_refs and not _IMAGE_B64_RE.search(assistant_text or ""))
    should_review_agent_reply = bool(used_agent and should_review_visual_reply)
    protected_review_required = bool(
        resolved_plugin_episode is not None
        or detect_persona_identity_leak(assistant_text)
    )
    if used_agent and not should_review_agent_reply and not care_review_required and not protected_review_required:
        review_decision = make_passthrough_review_decision(
            assistant_text,
            reason="agent_passthrough",
        )
    elif (
        not care_review_required
        and not should_review_visual_reply
        and not protected_review_required
        and not bool(getattr(plugin_config, "personification_response_review_enabled", False))
    ):
        review_decision = make_passthrough_review_decision(
            assistant_text,
            reason="review_disabled",
        )
    else:
        review_decision = await review_response_text(
            review_call_ai_api,
            candidate_text=assistant_text,
            raw_message_text=raw_message_text or history_last_text or trigger_reason,
            recent_context=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            recent_bot_replies=recent_bot_replies,
            message_intent=message_intent,
            is_private=is_private_session,
            is_random_chat=is_random_chat,
            is_direct_mention=is_direct_mention,
            reply_required=reply_required,
            semantic_frame=semantic_frame,
            turn_media_context=turn_media_refs,
            plugin_episode=resolved_plugin_episode,
        )
    if review_decision.action == "no_reply":
        logger.info(f"拟人插件 (YAML)：回复审阅后选择沉默，group={group_id} user={user_id}")
        _trace_no_reply("review_no_reply", detail="回复审阅选择沉默")
        return
    if review_decision.action == "rewrite" and review_decision.text:
        assistant_text = sanitize_history_text(review_decision.text.strip())
        parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}

    assistant_text, reviewed_favorability_signals = extract_legacy_favorability_markers(assistant_text)
    favorability_signals.merge(reviewed_favorability_signals)

    suppress_reply_recovery = bool(
        agent_result is not None and getattr(agent_result, "suppress_reply_recovery", False)
    ) or uncertain_reply_suppress_recovery
    if (
        has_silence_control_marker(assistant_text)
        and reply_required
        and not pending_actions
        and not suppress_reply_recovery
    ):
        assistant_text = required_reply_fallback_text(has_images=bool(tool_image_urls))
        parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}
    if has_silence_control_marker(assistant_text):
        logger.info(f"拟人插件 (YAML)：最终回复含沉默控制标记，group={group_id} user={user_id}")
        await _commit_pending_actions()
        if bool(reply_commit_state.get("reply_delivery_confirmed", False)):
            mark_reply_delivery_complete(reply_commit_state)
            release_reply_commit(reply_commit_state)
            mark_reply_phase(reply_commit_state, "reply_complete")
            _trace_finish(outcome="ok", diagnosis_code="ok", detail={"action_only": True})
            return
        if suppress_reply_recovery:
            _trace_suppressed_agent_reply(
                "后台动作已启动，最终状态回复保持静默",
                "当前没有足够可用证据，最终回复保持静默",
            )
            return
        if reply_required:
            assistant_text = required_reply_fallback_text(has_images=bool(tool_image_urls))
            parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}
        else:
            _trace_no_reply("final_silence_marker", detail="最终回复含沉默控制标记")
            return

    cleaned_assistant_text = strip_response_control_markers(assistant_text)
    cleaned_assistant_text = normalize_visible_reply_text(cleaned_assistant_text)
    if not cleaned_assistant_text:
        if suppress_reply_recovery:
            _trace_suppressed_agent_reply(
                "后台动作已启动，清理后状态回复保持静默",
                "当前没有足够可用证据，清理后保持静默",
            )
            return
        if reply_required:
            cleaned_assistant_text = required_reply_fallback_text(has_images=bool(tool_image_urls))
            parsed = {
                "messages": [{"text": cleaned_assistant_text, "sticker": ""}],
                "think": "",
                "status": "",
                "action": "",
            }
        else:
            if not await _commit_yaml_poke():
                return
            _trace_no_reply("empty_visible_reply", diagnosis_code="model_empty", detail="清理控制标记后没有可见文本")
            return
    if parsed.get("messages"):
        parsed = _normalize_parsed_message_texts(parsed)
        if not any(str(item.get("text", "") or "").strip() for item in parsed.get("messages", [])):
            parsed = {"messages": [{"text": cleaned_assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}
    elif cleaned_assistant_text != assistant_text:
        parsed = {"messages": [{"text": cleaned_assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}
    assistant_text = cleaned_assistant_text
    assistant_text = guard_visible_text(assistant_text, logger=logger, surface="yaml_reply")
    if not assistant_text:
        _trace_no_reply("unsafe_visible_output", diagnosis_code="blocked", detail="最终可见输出被安全门拦截")
        return
    qq_auto_marker = maybe_choose_auto_qq_expression_marker(
        plugin_config=plugin_config,
        semantic_frame=semantic_frame,
        reply_text=assistant_text,
        raw_message_text=raw_message_text or history_last_text or trigger_reason,
        message_intent=message_intent,
        group_id=group_id,
        user_id=user_id,
        is_private=is_private_session,
        is_random_chat=is_random_chat,
        has_rich_sticker=bool(stickers_sent),
    )
    if qq_auto_marker:
        if message_intent == "expression" and not contains_qq_expression_marker(assistant_text):
            assistant_text = qq_auto_marker
            parsed = {"messages": [{"text": qq_auto_marker, "sticker": ""}], "think": "", "status": "", "action": ""}
        elif parsed.get("messages"):
            parsed["messages"][-1]["text"] = f"{str(parsed['messages'][-1].get('text', '') or '').strip()}{qq_auto_marker}"
            assistant_text = f"{assistant_text}{qq_auto_marker}".strip()
        else:
            assistant_text = f"{assistant_text}{qq_auto_marker}".strip()
            parsed = {"messages": [{"text": assistant_text, "sticker": ""}], "think": "", "status": "", "action": ""}

    assistant_text, history_image_payloads = _extract_image_b64_markers(assistant_text)
    has_generated_image = bool(history_image_payloads)
    assistant_history_text = history_text_for_qq_expression(assistant_text)
    if history_image_payloads and not assistant_history_text:
        assistant_history_text = "[发送了一张图片]"

    sticker_dir = resolve_sticker_dir(getattr(plugin_config, "personification_sticker_path", None))
    chosen_sticker_paths: list[Path | None] = []
    if (
        parsed["messages"]
        and bool(getattr(semantic_frame, "sticker_appropriate", True))
        and not contains_qq_expression_marker(assistant_text)
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
    delivered_sticker_names: list[str] = []

    if _has_newer_batch_now():
        logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
        _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="发送前出现更新批次")
        return
    mark_reply_phase(reply_commit_state, "delivery_commit_wait")
    await acquire_reply_commit(reply_commit_state)
    if user_policy_gate is not None:
        await user_policy_gate.ensure_current(event)
    delivery_started_at = time.monotonic()
    mark_reply_phase(reply_commit_state, "delivery")
    if _has_newer_batch_now():
        logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
        _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="获得提交锁后出现更新批次")
        return
    await _commit_pending_actions()
    if not await _commit_yaml_poke():
        return

    sent_as_tts = False
    delivery_partial = False
    delivery_unknown = False

    def _mark_tts_delivery_unknown() -> None:
        nonlocal delivery_unknown
        delivery_unknown = True
    sent_message_id = ""
    address_plan = _humanize.decide_addressing(
        plugin_config=plugin_config,
        state={"batched_events": list(batched_events or [])},
        event=event,
        group_id=group_id,
        user_id=user_id,
        is_private=is_private_session,
        has_newer_batch=_has_newer_batch_now(),
        address_mode=getattr(semantic_frame, "address_mode", "auto"),
    )
    quote_message_id = address_plan.get("quote_message_id")
    at_target = address_plan.get("at_target")
    _trace_stage(
        key="addressing_plan",
        label="发送指向",
        status="info",
        detail=(
            f"address_mode={address_plan.get('mode') or 'none'} "
            f"source={address_plan.get('source') or '-'} "
            f"quote={bool(quote_message_id)} at={bool(at_target)} "
            f"target={str(at_target or '-')} elapsed_ms=0"
        ),
    )
    if (
        assistant_text
        and not has_generated_image
        and not stickers_sent
        and not contains_qq_expression_marker(assistant_text)
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
                    on_delivery_started=lambda: mark_reply_delivery_started(reply_commit_state),
                    on_delivery_confirmed=_confirm_reply_delivery,
                    on_delivery_unknown=_mark_tts_delivery_unknown,
                    operation_id=outbound_reply_trace_id,
                    user_target=user_id,
                )
        except Exception as e:
            likely_delivered = is_likely_delivered_send_timeout(e)
            if bool(reply_commit_state.get("reply_delivery_confirmed", False)) or likely_delivered:
                sent_as_tts = True
                delivery_unknown = likely_delivered
                delivery_partial = not likely_delivered
                logger.warning(f"[tts] YAML 自动语音发送结果不完整，不重复发送完整文字: {e}")
            else:
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

                    for seg_index, seg in enumerate(merged_segments):
                        if seg.strip():
                            if _has_newer_batch_now():
                                logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                                _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="分段文本发送前出现更新批次")
                                return
                            rendered_seg = await render_qq_expression_message(
                                seg,
                                message_segment_cls=message_segment_cls,
                                bot=bot,
                                plugin_config=plugin_config,
                                logger=logger,
                            )
                            if not rendered_seg.message:
                                continue
                            outgoing = rendered_seg.message
                            if not sent_message_id and seg_index == 0:
                                try:
                                    outgoing = _humanize.prepend_addressing_segments(
                                        message_segment_cls=message_segment_cls,
                                        outgoing=outgoing,
                                        quote_message_id=quote_message_id,
                                        at_target=at_target,
                                    )
                                except Exception:
                                    outgoing = rendered_seg.message
                            send_result = await _send_reply(outgoing)
                            if not sent_message_id:
                                sent_message_id = _message_id_from_send_result(send_result)
                            await asyncio.sleep(random.uniform(0.4, 1.0))

                for image_b64 in image_b64_payloads:
                    if _has_newer_batch_now():
                        logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                        _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="图片发送前出现更新批次")
                        return
                    send_result = await _send_reply(message_segment_cls.image(f"base64://{image_b64}"))
                    if not sent_message_id:
                        sent_message_id = _message_id_from_send_result(send_result)
                    await asyncio.sleep(random.uniform(0.4, 1.0))

                chosen_sticker_path = chosen_sticker_paths.pop(0) if chosen_sticker_paths else None
                if chosen_sticker_path is not None:
                    try:
                        if _has_newer_batch_now():
                            logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                            _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="表情发送前出现更新批次")
                            return
                        send_result = await _send_reply(
                            message_segment_cls.image(f"file:///{chosen_sticker_path.absolute()}")
                        )
                        if not sent_message_id:
                            sent_message_id = _message_id_from_send_result(send_result)
                        delivered_sticker_names.append(chosen_sticker_path.stem)
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
                    _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="普通文本发送前出现更新批次")
                    return
                rendered_reply = await render_qq_expression_message(
                    clean_reply,
                    message_segment_cls=message_segment_cls,
                    bot=bot,
                    plugin_config=plugin_config,
                    logger=logger,
                )
                if rendered_reply.message:
                    outgoing = rendered_reply.message
                    try:
                        outgoing = _humanize.prepend_addressing_segments(
                            message_segment_cls=message_segment_cls,
                            outgoing=outgoing,
                            quote_message_id=quote_message_id,
                            at_target=at_target,
                        )
                    except Exception:
                        outgoing = rendered_reply.message
                    send_result = await _send_reply(outgoing)
                else:
                    send_result = None
                if send_result is not None and not sent_message_id:
                    sent_message_id = _message_id_from_send_result(send_result)
            for image_b64 in image_b64_payloads:
                if _has_newer_batch_now():
                    logger.info(f"拟人插件 (YAML)：会话 {group_id} 已出现更新批次，本轮旧回复丢弃。")
                    _trace_no_reply("stale_reply", diagnosis_code="stale_reply", detail="生成图片发送前出现更新批次")
                    return
                send_result = await _send_reply(message_segment_cls.image(f"base64://{image_b64}"))
                if not sent_message_id:
                    sent_message_id = _message_id_from_send_result(send_result)

    if not delivery_partial and not delivery_unknown:
        mark_reply_delivery_complete(reply_commit_state)
    if user_policy_gate is not None:
        await user_policy_gate.ensure_current(event)
    mark_reply_phase(reply_commit_state, "delivery_history_commit")
    session_id = build_private_session_id(user_id) if is_private_session else build_group_session_id(group_id)
    legacy_session_id = None if is_private_session else group_id
    # 可见发送与有序历史投影共用同一个提交门，避免并发直达轮次写回顺序反转。
    append_session_message(
        session_id,
        "assistant",
        assistant_history_text,
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
        mentioned_ids=[str(at_target)] if at_target else [],
        is_at_bot=False,
    )
    if not is_private_session and record_group_msg is not None:
        record_group_msg(
            group_id,
            str(getattr(bot, "self_id", "") or "bot"),
            assistant_history_text,
            is_bot=True,
            user_id=str(getattr(bot, "self_id", "") or ""),
            message_id=sent_message_id or None,
            reply_to_msg_id=str(getattr(event, "message_id", "") or "") or None,
            reply_to_user_id=user_id,
            mentioned_ids=[str(at_target)] if at_target else [],
            source_kind="bot_reply",
        )
    release_reply_commit(reply_commit_state)
    delivery_elapsed_ms = int((time.monotonic() - delivery_started_at) * 1000)
    mark_reply_phase(reply_commit_state, "post_send_bookkeeping")
    bookkeeping_started_at = time.monotonic()
    _trace_stage(
        key="delivery_complete",
        label="交付完成",
        status="warn" if delivery_partial or delivery_unknown else "ok",
        detail=(
            f"elapsed_ms={delivery_elapsed_ms} "
            f"confirmed={bool(reply_commit_state.get('reply_delivery_confirmed', False))} "
            f"complete={bool(reply_commit_state.get('reply_delivery_complete', False))}"
        ),
    )
    _trace_stage(
        key="outgoing_message",
        label="发送消息",
        status="ok",
        detail=str(assistant_history_text or "")[:500],
        elapsed_ms=0,
    )
    for sticker_name in delivered_sticker_names:
        try:
            await record_sticker_sent(sticker_name)
        except Exception as e:
            logger.debug(f"[sticker] YAML sent feedback update failed: {e}")
    try:
        await update_emotion_state_after_turn(
            data_dir,
            user_id=user_id,
            group_id="" if is_private_session else group_id,
            semantic_frame=semantic_frame,
            assistant_text=assistant_history_text,
            is_private=is_private_session,
        )
    except Exception as e:
        logger.debug(f"[emotion] YAML update after reply failed: {e}")
    schedule_inner_state_update_after_reply(
        inner_state_updater=inner_state_updater,
        logger=logger,
        user_text=raw_message_text or history_last_text or trigger_reason,
        assistant_text=assistant_history_text,
        user_id=user_id,
        group_id=group_id,
        is_private=is_private_session,
        semantic_frame=semantic_frame,
    )
    if memory_curator is not None:
        memory_group_id = "" if is_private_session else group_id
        if hasattr(memory_curator, "schedule_turn_capture"):
            memory_curator.schedule_turn_capture(
                user_utterance=raw_message_text or history_last_text or trigger_reason,
                bot_response=assistant_history_text,
                user_id=user_id,
                group_id=memory_group_id,
                vision_summary=image_summary_suffix,
                semantic_frame=semantic_frame,
                scope=f"group:{memory_group_id}" if memory_group_id else f"user:{user_id}",
            )
        else:
            memory_curator.schedule_capture(
                summary=assistant_history_text,
                user_id=user_id,
                group_id=memory_group_id,
                topic_tags=[group_id] if not is_private_session else [],
            )
    record_counter(
        "yaml_reply.success_total",
        scene="private" if is_private_session else "group",
        via="tts" if sent_as_tts else "text",
        sticker=bool(stickers_sent),
    )
    bookkeeping_elapsed_ms = int((time.monotonic() - bookkeeping_started_at) * 1000)
    mark_reply_phase(reply_commit_state, "reply_complete")
    record_timing(
        "yaml_reply.total_ms",
        (time.monotonic() - started_at) * 1000.0,
        scene="private" if is_private_session else "group",
    )
    _trace_stage(
        key="post_send_bookkeeping",
        label="发送后状态写入",
        status="ok",
        detail=f"elapsed_ms={bookkeeping_elapsed_ms}",
    )
    completion = resolve_sent_reply_completion(
        state=reply_commit_state,
        visible_text=assistant_history_text,
        delivery_partial=delivery_partial,
        delivery_unknown=delivery_unknown,
    )
    _trace_stage(
        key="yaml_reply_success",
        label="YAML 回复完成",
        status="ok" if completion["outcome"] == "ok" else "warn",
        detail=(
            f"chars={len(assistant_history_text)} tts={bool(sent_as_tts)} "
            f"sticker={bool(stickers_sent)} tool_execution={completion['tool_execution']} "
            f"evidence_delivery={completion['evidence_delivery']} "
            f"outbound_delivery={completion['outbound_delivery']}"
        ),
    )
    _trace_finish(
        outcome=completion["outcome"],
        diagnosis_code=completion["diagnosis_code"],
        detail={
            "reply_chars": len(assistant_history_text),
            "tts": bool(sent_as_tts),
            "sticker": bool(stickers_sent),
            "delivery_partial": delivery_partial,
            "delivery_unknown": delivery_unknown,
            "tool_execution": completion["tool_execution"],
            "evidence_delivery": completion["evidence_delivery"],
            "outbound_delivery": completion["outbound_delivery"],
            "social_coverage_status": completion["coverage_status"],
            "evidence_recovered": completion["evidence_recovered"],
            "incoming_text": str(raw_message_text or history_last_text or trigger_reason or "")[:500],
            "outgoing_text": str(assistant_history_text or "")[:500],
        },
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
    review_call_ai_api: Callable[..., Awaitable[Any]] | None = None,
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
    inner_state_updater: Any = None,
    favorability_service: Any = None,
    user_policy_gate: Any = None,
    qq_outbound_ledger: QQOutboundLedger | None = None,
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
            review_call_ai_api=runtime_overrides.get("review_call_ai_api", review_call_ai_api),
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
            inner_state_updater=runtime_overrides.get("inner_state_updater", inner_state_updater),
            favorability_service=runtime_overrides.get("favorability_service", favorability_service),
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
            semantic_frame=runtime_overrides.get("semantic_frame"),
            has_newer_batch=bool(runtime_overrides.get("has_newer_batch", False)),
            batch_runtime_ref=runtime_overrides.get("batch_runtime_ref"),
            reply_commit_state=runtime_overrides.get("reply_commit_state"),
            solo_speaker_follow=bool(runtime_overrides.get("solo_speaker_follow", False)),
            reply_required=bool(runtime_overrides.get("reply_required", False)),
            response_deadline=runtime_overrides.get("response_deadline"),
            prepared_inner_state=runtime_overrides.get("prepared_inner_state"),
            prepared_emotion_state=runtime_overrides.get("prepared_emotion_state"),
            turn_media_context=list(runtime_overrides.get("turn_media_context") or []),
            media_grounding=str(runtime_overrides.get("media_grounding", "") or ""),
            precomputed_image_summary_suffix=str(
                runtime_overrides.get("precomputed_image_summary_suffix", "") or ""
            ),
            user_profile_block=str(runtime_overrides.get("user_profile_block", "") or ""),
            profile_service=runtime_overrides.get("profile_service"),
            favorability_context_block=str(
                runtime_overrides.get("favorability_context_block", "") or ""
            ),
            favorability_turn_id=str(runtime_overrides.get("favorability_turn_id", "") or ""),
            avatar_pair_candidates=list(runtime_overrides.get("avatar_pair_candidates") or []),
            avatar_pair_runtime=runtime_overrides.get("avatar_pair_runtime"),
            user_policy_gate=runtime_overrides.get("user_policy_gate", user_policy_gate),
            qq_outbound_ledger=runtime_overrides.get(
                "qq_outbound_ledger",
                qq_outbound_ledger,
            ),
            reply_trace_id=str(runtime_overrides.get("reply_trace_id", "") or ""),
        )

    return _processor
