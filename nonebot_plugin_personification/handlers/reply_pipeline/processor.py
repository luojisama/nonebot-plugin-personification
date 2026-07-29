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

from ...core.ai_routes import summarize_provider_route_attempts
from ...core.chat_intent import looks_like_explanatory_output
from ...core.error_utils import log_exception
from ...core.favorability_turn import (
    build_favorability_context_block,
    build_favorability_turn_id,
    commit_favorability_turn,
    extract_legacy_favorability_markers,
    signals_from_semantic_frame,
)
from ...core.image_input import (
    is_image_input_unsupported_error,
    normalize_image_detail,
    normalize_image_input_mode,
)
from ...core.metrics import record_counter, record_timing
from ...core.meme_reply_policy import format_meme_turn_prompt, prepare_meme_turn_context
from ...core.message_parts import build_user_message_content, clone_messages_with_text_suffix
from ...core.message_relations import extract_send_message_id
from ...core.context_policy import (
    compress_context_if_needed,
    has_silence_control_marker,
    strip_response_control_markers,
)
from ...core.gemini_profile import (
    context_keep_recent_for_route,
    context_token_budget_for_route,
    should_enable_default_builtin_search,
)
from ...core import protocol_capabilities as _protocol_caps
from ...flows.yaml_parser import parse_yaml_response
from . import humanize as _humanize
from .reaction import maybe_poke_back, maybe_react_on_silence
from ...agent.runtime.responder import (
    apply_persona_response_to_semantic_frame,
    parse_persona_response,
    with_persona_responder_instruction,
)
from ...core.prompt_hooks import HookContext, get_hook_registry
from ...core.group_context import (
    build_group_conversation_context,
    render_group_conversation_context,
    render_plugin_episode_trace_detail,
    render_topic_state_trace_detail,
)
from ...core.group_mute import refresh_bot_group_mute_state
from ...core.group_roles import extract_sender_role
from ...core.target_inference import (
    MessageTargetDecision,
    TARGET_OTHERS,
    TARGET_UNCLEAR,
    infer_message_target,
)
from ...core.tts_service import extract_persona_tts_config
from ...core.turn_media import (
    attach_safe_visual_summary,
    coerce_turn_media,
    extract_turn_media_from_event,
    media_from_batched_events,
    media_summary_timeout_seconds,
    normalize_safe_visual_summary,
    render_turn_media_grounding,
    serialize_turn_media,
)
from ...core.user_avatar_insight import schedule_user_avatar_analysis
from ...core.user_avatar_pair_insight import (
    build_avatar_pair_candidates,
    filter_avatar_candidates_by_policy,
)
from ...core.repeat_follow import maybe_follow_repeat_cluster
from ...core.reply_style_policy import (
    build_direct_visual_identity_guard,
    build_directed_exchange_policy_prompt,
    build_plugin_interaction_policy_prompt,
    build_speech_act_policy_prompt,
)
from ...core.role_integrity import detect_persona_identity_leak
from ...core.response_review import (
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
from ...core.reply_text_policy import normalize_visible_reply_text
from ...core.reply_completion_contract import resolve_sent_reply_completion
from ...core.visual_capabilities import VISUAL_ROUTE_AGENT, VISUAL_ROUTE_REPLY_PLAIN
from ...skills.skillpacks.sticker_tool.scripts.impl import (
    reset_current_image_context,
    set_current_image_context,
)
from ...core.proactive_store import update_group_chat_active
from ...core.qq_expression_library import (
    build_qq_expression_prompt,
    contains_qq_expression_marker,
    history_text_for_qq_expression,
    maybe_choose_auto_qq_expression_marker,
    qq_expression_enabled,
    render_qq_expression_message,
    semantic_text_for_qq_expression_segment,
)
from ...core.qq_user_policy import QQ_POLICY_DIRECT_CLOSURE, QQPolicyBlockedDuringTurn
from ...core.qq_outbound import QQOutboundLedger, SendReceipt
from ...core.sticker_feedback import (
    build_sticker_feedback_scene_key,
    mark_pending_sticker_reaction,
    record_sticker_sent,
    review_pending_sticker_reaction,
)
from ...core.web_grounding import extract_forward_message_content
from ...utils import build_group_context_window, get_recent_group_msgs
from ..event_rules import (
    _extract_recordable_group_message,
    _looks_like_plugin_command_interaction,
    _render_plugin_command_interaction,
    split_segment_if_long,
)
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
from .pipeline_context import (
    batch_has_newer_messages as _batch_has_newer_messages,
    build_base_system_prompt as _build_base_system_prompt,
    build_confidence_style_instruction as _build_confidence_style_instruction,
    build_scenario_instruction as _build_scenario_instruction,
    build_final_visible_reply_text as _build_final_visible_reply_text,
    build_group_session_relation_metadata as _build_group_session_relation_metadata,
    build_tts_user_hint as _build_tts_user_hint,
    count_user_interactions as _count_user_interactions,
    dispatch_reply_part as _dispatch_reply_part,
    extract_reply_sender_meta as _extract_reply_sender_meta,
    fold_consecutive_sticker_placeholders as _fold_consecutive_sticker_placeholders,
    get_primary_provider_signature as _get_primary_provider_signature,
    looks_like_photo_message as _looks_like_photo_message,
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
    schedule_inner_state_update_after_reply,
    should_speak_in_random_chat,
)
from .pipeline_sticker import (
    IncomingStickerCandidate,
    build_image_summary_suffix as _build_image_summary_suffix,
    extract_gif_from_segment as _extract_gif_from_segment,
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


_IMAGE_B64_RE = re.compile(r"\[IMAGE_B64\]([A-Za-z0-9+/=\r\n]+)\[/IMAGE_B64\]")
_SAFE_PROVIDER_DIAGNOSIS_CODES = {
    "provider_auth_failed",
    "provider_call_failed",
    "provider_caller_unavailable",
    "provider_invalid_response",
    "provider_model_candidate_unavailable",
    "provider_model_unavailable",
    "provider_network_failed",
    "provider_permission_denied",
    "provider_request_rejected",
    "provider_safety_block",
    "provider_timeout",
    "providers_exhausted",
}


def _provider_diagnosis_code(exc: BaseException) -> str:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        code = str(getattr(current, "code", "") or "").strip().lower()
        if code in _SAFE_PROVIDER_DIAGNOSIS_CODES:
            return code
        current = current.__cause__ or current.__context__
    return ""


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
    if not raw_msg or len(raw_msg) >= 500:
        return
    is_command_interaction = _looks_like_plugin_command_interaction(raw_msg)
    record_content = _render_plugin_command_interaction(raw_msg) if is_command_interaction else raw_msg
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

    source_kind = (
        "plugin"
        if bot_self_id and user_id == bot_self_id
        else ("plugin_command" if is_command_interaction else "user")
    )
    runtime.record_group_msg(
        str(getattr(event, "group_id", "") or ""),
        str(nickname or user_id),
        record_content,
        is_bot=bool(bot_self_id and user_id == bot_self_id),
        user_id=user_id,
        sender_role=extract_sender_role(event),
        image_count=image_count,
        visual_summary=visual_summary,
        **build_event_relation_metadata(
            event,
            bot_self_id=bot_self_id,
            source_kind=source_kind,
        ),
    )
    try:
        setattr(event, "_personification_muted_recorded", True)
    except Exception:
        pass


def _consume_pending_action_history_text(event: Any) -> str:
    text = str(getattr(event, "_personification_pending_action_history_text", "") or "").strip()
    if text:
        try:
            setattr(event, "_personification_pending_action_history_text", "")
        except Exception:
            pass
    return re.sub(r"\s+", " ", text).strip()


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
    favorability_service: Any = None


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
    review_call_ai_api: Any = None
    persona_store: Any = None
    vision_caller: Any = None
    knowledge_store: Any = None
    memory_store: Any = None
    profile_service: Any = None
    memory_curator: Any = None
    background_intelligence: Any = None
    user_policy_gate: Any = None
    qq_outbound_ledger: QQOutboundLedger | None = None


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


def _policy_direct_closure_text(policy_decision: dict[str, Any]) -> str:
    if str(policy_decision.get("reason_code", "") or "") == "classifier_unavailable":
        return "我这边暂时没判断好，这条先不接了。你可以稍后再发一次。"
    return "这个话题我不参与，我们换个话题吧。"


async def _send_policy_direct_closure(
    bot: Any,
    event: Any,
    state: Dict[str, Any],
    deps: ReplyProcessorDeps,
    policy_decision: dict[str, Any],
) -> None:
    """Send the one-shot policy closure without entering Agent/history/memory."""
    runtime = deps.runtime
    trace_mod = None
    trace_id = ""
    reason_code = str(policy_decision.get("reason_code", "") or "policy_boundary")
    try:
        plugin_config = getattr(runtime, "plugin_config", None)
        if bool(getattr(plugin_config, "personification_turn_trace_enabled", True)):
            from ...core import reply_turn_trace as trace_mod  # type: ignore[assignment]

            group_id = str(getattr(event, "group_id", "") or "")
            user_id = str(getattr(event, "user_id", "") or "")
            trace_id = trace_mod.start_trace(
                session_type="group" if group_id else "private",
                group_id=group_id,
                user_id=user_id,
                detail={
                    "source": "user_policy_direct_closure",
                    "message_id": str(getattr(event, "message_id", "") or ""),
                },
            )
            state["reply_trace_id"] = trace_id
            trace_mod.record_stage(
                trace_id=trace_id,
                key="policy_ingress",
                label="用户策略入口",
                status="warn",
                detail=f"disposition=direct_closure reason={reason_code}",
            )
    except Exception:
        trace_mod = None
        trace_id = ""

    try:
        result = await _dispatch_reply_part(
            bot=bot,
            event=event,
            payload=_policy_direct_closure_text(policy_decision),
            ledger=getattr(runtime, "qq_outbound_ledger", None),
            surface="policy_direct_closure",
            reply_trace_id=trace_id,
        )
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(
                f"[user_policy] direct closure send failed: {type(exc).__name__}"
            )
        if trace_mod is not None and trace_id:
            trace_mod.record_stage(
                trace_id=trace_id,
                key="policy_closure_send",
                label="策略提示发送",
                status="error",
                detail=f"type={type(exc).__name__}",
            )
            trace_mod.finish_trace(
                trace_id=trace_id,
                outcome="failed",
                diagnosis_code="policy_closure_send_failed",
            )
        return

    sent = not isinstance(result, SendReceipt) or result.status == "sent"
    if trace_mod is not None and trace_id:
        trace_mod.record_stage(
            trace_id=trace_id,
            key="policy_closure_send",
            label="策略提示发送",
            status="ok" if sent else "warn",
            detail="固定非模型提示已发送" if sent else f"ledger_status={result.status}",
        )
        trace_mod.finish_trace(
            trace_id=trace_id,
            outcome="ok" if sent else "outcome_unknown",
            diagnosis_code=(
                "policy_classifier_unavailable"
                if reason_code == "classifier_unavailable"
                else "policy_direct_closure"
            ),
        )


async def process_response_logic(bot: Any, event: Any, state: Dict[str, Any], deps: ReplyProcessorDeps) -> None:
    # plugin_invoker 代为执行其它插件命令时会用 handle_event 重新分发合成事件，
    # 这里直接短路，确保合成事件永远不会再次进入拟人回复/Agent 流程（防递归）。
    if getattr(event, "_personification_synthetic", False):
        return
    user_policy_gate = getattr(deps.runtime, "user_policy_gate", None)
    policy_decision = state.get("user_policy_decision")
    if isinstance(policy_decision, dict) and policy_decision.get("disposition") != "allow":
        if policy_decision.get("disposition") == QQ_POLICY_DIRECT_CLOSURE:
            if user_policy_gate is None or await user_policy_gate.allows_current(event):
                await _send_policy_direct_closure(bot, event, state, deps, policy_decision)
        return
    if user_policy_gate is not None and not await user_policy_gate.allows_current(event):
        return
    begin_reply_lifecycle(state)

    token = None
    reset_llm_context = None
    trace_id = ""
    trace_token = None
    trace_mod = None
    cancelled = False
    try:
        from ...core.llm_context import reset_llm_context, set_llm_context

        token = set_llm_context(
            group_id=str(getattr(event, "group_id", "") or ""),
            user_id=str(getattr(event, "user_id", "") or ""),
            purpose="reply",
        )
    except Exception:
        token = None
    try:
        from ...core import reply_turn_trace as trace_mod  # type: ignore[assignment]

        runtime = deps.runtime
        if bool(getattr(runtime.plugin_config, "personification_turn_trace_enabled", True)):
            trace_id = trace_mod.current_trace_id()
            session_type = "group" if hasattr(event, "group_id") else "private"
            group_id = str(getattr(event, "group_id", "") or "")
            user_id = str(getattr(event, "user_id", "") or "")
            trace_id = trace_mod.start_trace(
                trace_id=trace_id,
                session_type=session_type,
                group_id=group_id,
                user_id=user_id,
                detail={"source": "reply_pipeline", "message_id": str(getattr(event, "message_id", "") or "")},
            )
            state["reply_trace_id"] = trace_id
            trace_token = trace_mod.set_current_trace_id(trace_id)
            trace_mod.record_stage(
                trace_id=trace_id,
                key="ingress",
                label="进入回复链路",
                status="info",
                detail=f"session={session_type} user={user_id} group={group_id or '-'}",
            )
    except Exception:
        trace_id = ""
        trace_token = None
        trace_mod = None
    try:
        await _process_response_logic_impl(bot, event, state, deps)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except FinishedException:
        if trace_mod is not None and trace_id and not cancelled:
            trace_mod.finish_trace(trace_id=trace_id, outcome="finished", diagnosis_code="finished_exception")
        raise
    except Exception as exc:
        if trace_mod is not None and trace_id and not cancelled:
            provider_code = _provider_diagnosis_code(exc)
            is_provider_failure = bool(provider_code)
            route_summary = summarize_provider_route_attempts(exc) if is_provider_failure else ""
            trace_mod.record_stage(
                trace_id=trace_id,
                key="provider_failure" if is_provider_failure else "unhandled_exception",
                label="Provider 调用失败" if is_provider_failure else "链路异常",
                status="error",
                detail=(
                    " ".join(
                        part
                        for part in (
                            f"code={provider_code}",
                            f"type={type(exc).__name__}",
                            route_summary,
                        )
                        if part
                    )
                    if is_provider_failure
                    else f"type={type(exc).__name__}"
                ),
            )
            trace_mod.finish_trace(
                trace_id=trace_id,
                outcome="failed",
                diagnosis_code=provider_code if is_provider_failure else "internal_exception",
            )
        raise
    finally:
        release_reply_commit(state)
        if trace_mod is not None and trace_id and not cancelled:
            try:
                last_trace = trace_mod.get_trace(trace_id) or {}
                if not str(last_trace.get("outcome", "") or ""):
                    trace_mod.finish_trace(
                        trace_id=trace_id,
                        outcome="no_reply",
                        diagnosis_code="no_reply",
                        detail={"reason": "reply_pipeline_returned_without_send"},
                    )
            except Exception:
                pass
        if trace_token is not None and trace_mod is not None:
            try:
                trace_mod.reset_current_trace_id(trace_token)
            except Exception:
                pass
        if token is not None and reset_llm_context is not None:
            reset_llm_context(token)


def _build_image_only_context_message(
    *,
    sender_name: str,
    is_private_context: bool,
    is_active_followup: bool,
    followup_topic: str,
    is_solo_speaker_follow: bool,
    solo_follow_topic: str,
    is_random_chat: bool,
) -> str:
    if is_private_context:
        return (
            "[对方发送了一张图片。若没有直接看到图片或可见摘要，不要假装看懂；"
            "先结合最近对话短句回应，必要时请对方补一句]"
        )
    if is_active_followup:
        return (
            f"[对方正在顺着你刚才的话题继续聊，并发来了一条图片/表情消息。"
            f"刚才的话题：{followup_topic or '上一轮对话'}。"
            "若没有清楚的视觉摘要，不要评价图片内容；只有能从前文确定是在接话时才短句回应，否则保持安静]"
        )
    if is_solo_speaker_follow:
        return (
            f"[群里 {sender_name} 已经连续说了一阵，并发来了一条图片/表情消息。"
            f"当前延续的话题：{solo_follow_topic or '刚才这串内容'}。"
            "若没有清楚的视觉摘要，不要假装看懂图片；只有能从前文确定是在接话时才短句回应，否则保持安静]"
        )
    if is_random_chat:
        return (
            f"[群里 {sender_name} 发了一条图片/表情消息，你只是路过看到。"
            "没人 cue 你且没有明确文字意图时保持安静，不要评论图片或表情内容]"
        )
    return (
        "[对方发送了一张图片，是在对你说话。"
        "如果看不清内容，先接文字或最近上下文；信息不足时给一句保守短反应或保持安静，不要追问图里是什么]"
    )


def _batch_media_owner_matches_selected_user(
    batched_events: List[Dict[str, Any]],
    selected_user_id: str,
) -> bool:
    media_owners = {
        str(media.get("owner_user_id", "") or "").strip()
        for item in batched_events
        if isinstance(item, dict)
        for media in list(item.get("media") or [])
        if isinstance(media, dict) and str(media.get("owner_user_id", "") or "").strip()
    }
    return not media_owners or media_owners == {str(selected_user_id or "").strip()}


async def _capture_user_protocol_profile(
    *,
    runtime: Any,
    bot: Any,
    event: Any,
    user_id: str,
    source: str,
) -> None:
    profile_service = getattr(runtime, "profile_service", None)
    persona_enabled = lambda: bool(
        getattr(
            getattr(runtime, "plugin_config", None),
            "personification_persona_enabled",
            True,
        )
    )
    if (
        profile_service is None
        or not str(user_id or "").strip()
        or not persona_enabled()
    ):
        return
    memory_store = getattr(profile_service, "memory_store", None)
    generation_getter = getattr(memory_store, "get_profile_generation", None)
    profile_generation = int(generation_getter()) if callable(generation_getter) else None
    try:
        from ...core.onebot_cache import get_user_profile
        from ...core.user_profile_meta import build_user_profile_meta

        protocol_profile = await get_user_profile(bot, str(user_id))
        meta = build_user_profile_meta(
            str(user_id),
            sender=getattr(event, "sender", None),
            stranger_info=protocol_profile,
            source=source,
        )
        if meta and persona_enabled():
            saved = profile_service.upsert_user_profile_meta(
                user_id=str(user_id),
                meta=meta,
                source=source,
                expected_generation=profile_generation,
            )
            if saved is not None:
                schedule_user_avatar_analysis(runtime, str(user_id))
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            try:
                logger.debug(f"[user_profile_meta] capture failed uid={user_id}: {exc}")
            except Exception:
                pass


async def _process_response_logic_impl(bot: Any, event: Any, state: Dict[str, Any], deps: ReplyProcessorDeps) -> None:
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
    sticker_image_urls: List[str] = []
    sticker_candidates: List[IncomingStickerCandidate] = []
    stop_reply_due_to_gif = [False]
    gif_understanding_counter = [0]
    is_direct_mention = False
    http_client = runtime.get_http_client()
    disable_network_hooks = bool(state.get("disable_network_hooks", False))
    batched_events = list(state.get("batched_events") or [])
    batch_trigger = dict(state.get("batch_trigger") or {})
    repeat_clusters = list(state.get("repeat_clusters") or [])
    batch_event_count = int(state.get("batch_event_count", 1) or 1)
    turn_media_context = coerce_turn_media(state.get("turn_media_context") or [])
    if not turn_media_context:
        turn_media_context = media_from_batched_events(batched_events)
    if not turn_media_context and isinstance(event, types.message_event_cls):
        turn_media_context = extract_turn_media_from_event(event, current_origin="current")
    state["turn_media_context"] = serialize_turn_media(turn_media_context)

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
        async def _poke_back_after_commit_gate() -> None:
            if (
                getattr(runtime, "user_policy_gate", None) is not None
                and not await runtime.user_policy_gate.allows_current(event)
            ):
                return
            commit_lock = state.get("reply_commit_lock")
            if isinstance(commit_lock, asyncio.Lock):
                async with commit_lock:
                    await maybe_poke_back(bot, runtime, group_id=group_id, user_id=user_id)
                return
            await maybe_poke_back(bot, runtime, group_id=group_id, user_id=user_id)

        poke_back_task = asyncio.create_task(_poke_back_after_commit_gate())
        poke_back_task.add_done_callback(_task_exc_logger("humanize_poke_back", runtime.logger))
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
                message_text_parts.append(semantic_text_for_qq_expression_segment("face", seg.data))
            elif seg.type == "mface":
                await _extract_mface_from_segment(
                    seg,
                    runtime=runtime,
                    http_client=http_client,
                    message_text_ref=message_text_parts,
                    image_urls=image_urls,
                    sticker_candidates_ref=sticker_candidates,
                    logger=runtime.logger,
                    stop_reply_ref=stop_reply_due_to_gif,
                    sticker_image_urls=sticker_image_urls,
                    gif_understanding_counter_ref=gif_understanding_counter,
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
                    sticker_image_urls=sticker_image_urls,
                    gif_understanding_counter_ref=gif_understanding_counter,
                )
            elif seg.type == "gif":
                await _extract_gif_from_segment(
                    seg,
                    runtime=runtime,
                    http_client=http_client,
                    message_text_ref=message_text_parts,
                    logger=runtime.logger,
                    stop_reply_ref=stop_reply_due_to_gif,
                    gif_understanding_counter_ref=gif_understanding_counter,
                )

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
                            sticker_image_urls=sticker_image_urls,
                            gif_understanding_counter_ref=gif_understanding_counter,
                        )
                    elif getattr(seg, "type", None) == "gif":
                        await _extract_gif_from_segment(
                            seg,
                            runtime=runtime,
                            http_client=http_client,
                            message_text_ref=message_text_parts,
                            logger=runtime.logger,
                            stop_reply_ref=stop_reply_due_to_gif,
                            gif_understanding_counter_ref=gif_understanding_counter,
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
                            elif seg_type == "face":
                                message_text_parts.append(semantic_text_for_qq_expression_segment("face", data))
                            elif seg_type == "mface":
                                message_text_parts.append(
                                    semantic_text_for_qq_expression_segment(
                                        "mface",
                                        data,
                                        default_mface_kind="super",
                                    )
                                )
                            elif seg_type == "image":
                                await _extract_reply_images(
                                    seg_type,
                                    data,
                                    http_client=http_client,
                                    message_text_ref=message_text_parts,
                                    image_urls=image_urls,
                                    logger=runtime.logger,
                                    stop_reply_ref=stop_reply_due_to_gif,
                                    runtime=runtime,
                                    gif_understanding_counter_ref=gif_understanding_counter,
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
        # 多人/连续刷表情时，把一连串表情占位符折叠成单个中性标记，
        # 避免模型看到"一张接一张"的信号后吐槽刷屏（配合 base prompt 的告诫）。
        message_text = _fold_consecutive_sticker_placeholders(message_text)
        raw_message_text = message_text
        message_content = message_text.strip()
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="incoming_message",
                label="收到消息",
                status="info",
                detail=(raw_message_text or message_content or "")[:500],
            )
        except Exception:
            pass
        is_private_context = str(group_id).startswith(session.private_session_prefix)
        if isinstance(event, types.private_message_event_cls) and session.looks_like_private_command(message_content):
            runtime.logger.debug(f"拟人插件：私聊命令消息已跳过，用户 {user_id}")
            return
        # P7：识别其他机器人 / Q 群管家，避免 bot 互相对话
        try:
            from ...core.peer_awareness import detect_other_bot

            extra_bot_ids = list(getattr(runtime.plugin_config, "personification_peer_bot_ids", []) or [])
            peer_decision = detect_other_bot(
                user_id=user_id,
                text=message_content,
                extra_bot_ids=extra_bot_ids,
            )
            if peer_decision.is_other_bot and peer_decision.suggest_silence:
                runtime.logger.info(
                    f"拟人插件：检测到来自其他机器人/管家的消息，跳过本轮 "
                    f"user={user_id} reason={peer_decision.reason}"
                )
                return
        except Exception:
            pass
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
                message_content = _build_image_only_context_message(
                    sender_name=sender_name,
                    is_private_context=is_private_context,
                    is_active_followup=is_active_followup,
                    followup_topic=followup_topic,
                    is_solo_speaker_follow=is_solo_speaker_follow,
                    solo_follow_topic=solo_follow_topic,
                    is_random_chat=is_random_chat,
                )
        else:
            if is_private_context:
                if image_urls and not message_content:
                    message_content = _build_image_only_context_message(
                        sender_name=sender_name,
                        is_private_context=True,
                        is_active_followup=False,
                        followup_topic="",
                        is_solo_speaker_follow=False,
                        solo_follow_topic="",
                        is_random_chat=False,
                    )
            else:
                if image_urls and not message_content:
                    message_content = _build_image_only_context_message(
                        sender_name=sender_name,
                        is_private_context=False,
                        is_active_followup=is_active_followup,
                        followup_topic=followup_topic,
                        is_solo_speaker_follow=is_solo_speaker_follow,
                        solo_follow_topic=solo_follow_topic,
                        is_random_chat=is_random_chat,
                    )
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
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="provider_failure",
                label="Provider 不可用",
                status="error",
                detail="code=provider_caller_unavailable delivery_state=not_started silent=true",
            )
            reply_turn_trace.finish_trace(
                outcome="failed",
                diagnosis_code="provider_caller_unavailable",
                detail={
                    "silent": True,
                    "delivery_state": "not_started",
                    "reply_required": bool(state.get("reply_required", False)),
                },
            )
        except Exception:
            pass
        return

    user_name = sender_name
    if not message_content and not is_poke and not image_urls:
        return

    if (
        isinstance(event, types.group_message_event_cls)
        and (not is_direct_mention)
        and (not is_active_followup)
        and (not is_solo_speaker_follow)
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
    reply_required = bool(state.get("reply_required", False) or is_private_session or is_direct_mention)
    response_deadline = state.get("response_deadline")

    async def _maybe_silence_reaction() -> None:
        """NO_REPLY 沉默前的轻量回应（贴表情/拍一拍），never-raise。"""
        try:
            if getattr(runtime, "user_policy_gate", None) is not None:
                await runtime.user_policy_gate.ensure_current(event)
            await acquire_reply_commit(state)
            favorability = 0.0
            try:
                favorability = float(persona.get_user_data(user_id).get("favorability", 0.0) or 0.0)
            except Exception:
                favorability = 0.0
            await maybe_react_on_silence(
                bot,
                runtime,
                event=event,
                mood_hint=str(
                    getattr(semantic_frame, "sticker_mood_hint", "")
                    or getattr(semantic_frame, "bot_emotion", "")
                    or ""
                ),
                group_id=str(group_id),
                user_id=user_id,
                is_private=is_private_session,
                favorability=favorability,
            )
        except Exception as exc:
            runtime.logger.debug(f"[humanize] silence reaction failed: {exc}")

    record_counter(
        "reply_processor.requests_total",
        scene="private" if is_private_session else "group",
        random_chat=bool(is_random_chat),
    )
    recent_group_msgs: List[Dict[str, Any]] = []
    if isinstance(event, types.group_message_event_cls) and not state.get("message_target"):
        recent_group_msgs = get_recent_group_msgs(str(group_id), limit=8, expire_hours=0)
        if getattr(runtime, "user_policy_gate", None) is not None:
            recent_group_msgs, _ = await runtime.user_policy_gate.filter_context_messages(
                recent_group_msgs,
                bot_self_id=str(getattr(bot, "self_id", "") or ""),
            )
        target_decision = infer_message_target(
            event,
            bot_self_id=str(getattr(bot, "self_id", "") or ""),
            recent_group_msgs=recent_group_msgs,
        )
        if isinstance(target_decision, MessageTargetDecision):
            state.update(target_decision.trace_fields())
        else:
            state["message_target"] = str(target_decision or "")
    try:
        from ...core import reply_turn_trace

        reply_turn_trace.record_stage(
            key="target_inferred",
            label="目标推断",
            status="info",
            detail=(
                f"private={is_private_session} random={bool(is_random_chat)} "
                f"direct={bool(is_direct_mention)} target={state.get('message_target') or '-'} "
                f"reason={state.get('message_target_reason') or '-'} "
                f"anchor={state.get('message_target_anchor_id') or '-'} "
                f"participants={len(state.get('message_target_participants') or [])}"
            ),
        )
    except Exception:
        pass
    session_id = session.build_private_session_id(user_id) if is_private_session else session.build_group_session_id(str(group_id))
    legacy_session_id = None if is_private_session else str(group_id)
    session.ensure_session_history(session_id, legacy_session_id=legacy_session_id)

    attitude_desc = "态度普通，像平常一样交流。"
    level_name = "未知"
    group_attitude = ""

    if persona.sign_in_available:
        try:
            current_attitudes = getattr(
                runtime.plugin_config,
                "personification_favorability_attitudes",
                None,
            ) or persona.favorability_attitudes
            user_data = persona.get_user_data(user_id)
            favorability = user_data.get("favorability", 0.0)
            level_name = persona.get_level_name(favorability)
            attitude_desc = current_attitudes.get(level_name, attitude_desc)

            group_key = f"group_{group_id}"
            group_data = persona.get_user_data(group_key)
            group_favorability = group_data.get("favorability", 35.0)
            group_level = persona.get_level_name(group_favorability)
            group_attitude = current_attitudes.get(group_level, "")
        except Exception as e:
            runtime.logger.error(f"获取好感度数据失败: {e}")

    favorability_context_block = build_favorability_context_block(
        user_level=level_name,
        user_attitude=attitude_desc,
        group_attitude=group_attitude,
        is_private=is_private_session,
    )

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
    if isinstance(event, types.message_event_cls):
        await _capture_user_protocol_profile(
            runtime=runtime,
            bot=bot,
            event=event,
            user_id=user_id,
            source="reply_pipeline",
        )

    user_profile_block = ""
    try:
        profile_service = getattr(runtime, "profile_service", None)
        if profile_service is not None:
            user_profile_block = profile_service.build_prompt_block(
                user_id=user_id,
                group_id="" if is_private_session else str(group_id),
            )
    except Exception:
        user_profile_block = ""

    tool_image_urls = list(image_urls)
    # 分类为真实照片的 refs 继续用于 provider 图片输入不兼容时的旧 fallback。
    photo_image_urls = list(image_urls)
    image_input_mode = normalize_image_input_mode(
        getattr(runtime.plugin_config, "personification_image_input_mode", "auto")
    )
    image_detail = normalize_image_detail(
        getattr(runtime.plugin_config, "personification_image_detail", "auto")
    )
    has_photo_input = _looks_like_photo_message(raw_message_text or message_content)
    plain_route_vision = image_input_mode == "direct" or _primary_route_supports_vision(
        runtime, VISUAL_ROUTE_REPLY_PLAIN
    )
    agent_route_vision = image_input_mode == "direct" or _primary_route_supports_vision(
        runtime, VISUAL_ROUTE_AGENT
    )
    direct_image_input = bool(image_urls) and image_input_mode in {"auto", "direct"} and plain_route_vision
    agent_direct_image_input = bool(image_urls) and image_input_mode in {"auto", "direct"} and agent_route_vision
    if image_urls or sticker_image_urls:
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="vision_mode",
                label="视觉路径",
                status="ok" if (direct_image_input or agent_direct_image_input) else "warn",
                detail=(
                    f"mode={image_input_mode} plain_direct={direct_image_input} "
                    f"agent_direct={agent_direct_image_input} images={len(image_urls)} "
                    f"stickers={len(sticker_image_urls)} elapsed_ms=0"
                ),
                hint="" if (direct_image_input or agent_direct_image_input) else "将尝试视觉摘要 fallback 或文本占位",
            )
        except Exception:
            pass
    image_summary_suffix = ""
    image_urls_for_text_model = list(image_urls)
    if image_urls:
        if image_input_mode == "disabled":
            image_urls_for_text_model = []
        else:
            if not direct_image_input:
                image_urls_for_text_model = []

    # 非 gif 表情包：去重 + 限量后，仅在视觉可用时直传模型理解（多模态直传 / 非多模态降级为文本占位符，不打标）。
    # summary / disabled 模式不直传：前者明确要走文本，后者关图；两种情况下表情都只留文本占位符。
    if sticker_image_urls and image_input_mode in {"auto", "direct"}:
        sticker_vision_max = int(
            getattr(runtime.plugin_config, "personification_sticker_vision_max", 3) or 0
        )
        _seen_sticker: set[str] = set()
        capped_stickers: List[str] = []
        for _su in sticker_image_urls:
            if _su and _su not in _seen_sticker:
                _seen_sticker.add(_su)
                capped_stickers.append(_su)
            if sticker_vision_max > 0 and len(capped_stickers) >= sticker_vision_max:
                break
        if capped_stickers and (plain_route_vision or agent_route_vision):
            for _su in capped_stickers:
                if _su not in tool_image_urls:
                    tool_image_urls.append(_su)
            if plain_route_vision:
                for _su in capped_stickers:
                    if _su not in image_urls_for_text_model:
                        image_urls_for_text_model.append(_su)
                direct_image_input = True
            if agent_route_vision:
                agent_direct_image_input = True

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
        user_profile_block=user_profile_block,
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
    conversation_context = None
    if not is_private_session and recent_group_msgs:
        recent_window = build_group_context_window(
            str(group_id),
            limit=8,
            include_message_ids=[incoming_relation_metadata.get("reply_to_msg_id")],
        )
        excluded_context_user_ids: set[str] = set()
        if getattr(runtime, "user_policy_gate", None) is not None:
            recent_window, excluded_context_user_ids = (
                await runtime.user_policy_gate.filter_context_messages(
                    recent_window,
                    bot_self_id=bot_self_id,
                )
            )
        conversation_context = build_group_conversation_context(
            recent_messages=recent_window,
            trigger_msg_id=str(incoming_relation_metadata.get("message_id", "") or ""),
            trigger_user_id=user_id,
            bot_self_id=bot_self_id,
            repeat_clusters=repeat_clusters,
            excluded_user_ids=excluded_context_user_ids,
        )
        recent_context_hint = render_group_conversation_context(conversation_context)
        relationship_hint = conversation_context.relationship_hint
        try:
            from ...core import reply_turn_trace

            topic_detail = render_topic_state_trace_detail(conversation_context.topic_state)
            if topic_detail:
                reply_turn_trace.record_stage(
                    key="topic_state",
                    label="短期话题状态",
                    status="info",
                    detail=topic_detail,
                    hint="结构化线索用于判断当前消息接谁的话，不替代 LLM 语义判断",
                )
            plugin_detail = render_plugin_episode_trace_detail(conversation_context.plugin_episode)
            if plugin_detail:
                reply_turn_trace.record_stage(
                    key="plugin_episode",
                    label="其它插件交互",
                    status="info",
                    detail=plugin_detail,
                    hint="其它插件输出仅作为带来源的群聊上下文，不等同于人格回复",
                )
        except Exception:
            pass
    avatar_pair_candidates = build_avatar_pair_candidates(
        event=event,
        current_user_id=user_id,
        current_user_label=user_name,
        bot_self_id=bot_self_id,
        batched_events=batched_events,
        recent_messages=recent_window,
    )
    avatar_pair_candidates = await filter_avatar_candidates_by_policy(
        avatar_pair_candidates,
        (
            runtime.user_policy_gate.current_authorization
            if getattr(runtime, "user_policy_gate", None) is not None
            else None
        ),
    )
    if not is_private_session and sticker_candidates:
        if _batch_media_owner_matches_selected_user(batched_events, user_id):
            _spawn_auto_collect_stickers(
                runtime=runtime,
                group_id=str(group_id),
                user_id=user_id,
                candidates=sticker_candidates,
                task_exc_logger=_task_exc_logger,
            )
        else:
            record_counter("sticker.collect_skipped", reason="ambiguous_batch_owner")
            runtime.logger.debug("拟人插件：多用户 batch 的贴图候选无法可靠回绑 owner，本轮跳过自动收藏。")
    async def _image_summary_task() -> str:
        if image_input_mode == "disabled" or not tool_image_urls:
            return ""
        return await _build_image_summary_suffix(
            runtime=runtime,
            image_urls=tool_image_urls,
            sticker_like=False,
        )

    async def _prepare_semantics_timed() -> tuple[Any, int]:
        semantic_started_at = time.monotonic()
        prepared = await prepare_reply_semantics(
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
            has_images=bool(tool_image_urls),
            media_grounding=media_grounding,
        )
        return prepared, int((time.monotonic() - semantic_started_at) * 1000)

    summary_timeout = media_summary_timeout_seconds(
        response_deadline if isinstance(response_deadline, (int, float)) else None,
        now=time.monotonic(),
    )
    image_summary_suffix = ""
    if summary_timeout > 0.05:
        try:
            image_summary_suffix = await asyncio.wait_for(
                _image_summary_task(),
                timeout=summary_timeout,
            )
        except asyncio.TimeoutError:
            runtime.logger.warning(
                f"拟人插件：视觉摘要超过本轮前置预算 {summary_timeout:.1f}s，继续使用 provenance 进入语义判断。"
            )
    safe_visual_summary = normalize_safe_visual_summary(image_summary_suffix)
    turn_media_context = attach_safe_visual_summary(
        turn_media_context,
        safe_visual_summary,
        confidence=0.65,
    )
    state["turn_media_context"] = serialize_turn_media(turn_media_context)
    media_grounding = render_turn_media_grounding(
        turn_media_context,
        summary=safe_visual_summary,
    )
    prepared_semantics, semantic_prepare_elapsed_ms = await _prepare_semantics_timed()
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
    favorability_signals = signals_from_semantic_frame(
        semantic_frame,
        is_private=is_private_session,
    )
    favorability_turn_id = build_favorability_turn_id(
        trace_id=state.get("reply_trace_id", ""),
        message_id=getattr(event, "message_id", ""),
        group_id=group_id,
        user_id=user_id,
    )
    intent_decision = prepared_semantics.intent_decision
    message_intent = prepared_semantics.message_intent
    arbitration = prepared_semantics.arbitration
    try:
        from ...core import reply_turn_trace

        reply_turn_trace.record_stage(
            key="semantic_frame",
            label="语义帧",
            status="ok",
            detail=(
                f"intent={message_intent} ambiguity={getattr(intent_decision, 'ambiguity_level', '')} "
                f"silence={getattr(intent_decision, 'recommend_silence', False)} "
                f"speech_act={getattr(semantic_frame, 'speech_act', '-') or '-'} "
                f"address_mode={getattr(semantic_frame, 'address_mode', '-') or '-'} "
                f"emotion={getattr(semantic_frame, 'bot_emotion', '')} "
                f"output={getattr(semantic_frame, 'output_mode', '') or '-'} "
                f"elapsed_ms={semantic_prepare_elapsed_ms} "
                f"turn_age_ms={int((time.monotonic() - started_at) * 1000)}"
            ),
        )
    except Exception:
        pass
    if arbitration == "no_reply":
        runtime.logger.info(
            f"拟人插件：LLM 意图判别认为本轮高歧义且不宜插话，group={group_id} user={user_id}"
        )
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="no_reply",
                label="静默",
                status="warn",
                detail="arbitration=no_reply",
                hint="LLM 判定高歧义或不宜插话",
            )
            reply_turn_trace.finish_trace(outcome="no_reply", diagnosis_code="no_reply", detail={"reason": "arbitration_no_reply"})
        except Exception:
            pass
        return
    if is_random_chat:
        should_speak = should_speak_in_random_chat(
            state=state,
            message_target=str(state.get("message_target", "") or ""),
            solo_speaker_follow=is_solo_speaker_follow,
        )
        if not should_speak:
            runtime.logger.info(f"拟人插件：随机插话场景被 LLM 否决，group={group_id} user={user_id}")
            try:
                from ...core import reply_turn_trace

                reply_turn_trace.record_stage(
                    key="no_reply",
                    label="静默",
                    status="warn",
                    detail="random_chat denied by semantic frame",
                    hint="随机插话场景被判定不适合接话",
                )
                reply_turn_trace.finish_trace(outcome="no_reply", diagnosis_code="no_reply", detail={"reason": "random_chat_denied"})
            except Exception:
                pass
            return

    meme_turn_prompt = ""
    try:
        meme_turn_context = prepare_meme_turn_context(
            group_id="" if is_private_session else str(group_id),
            message_text=raw_message_text or current_agent_message_content,
            recent_context=recent_context_hint,
            probability=float(getattr(runtime.plugin_config, "personification_meme_reply_probability", 0.18) or 0.0),
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
        runtime.logger.debug(f"拟人插件：本轮黑话上下文不可用，按无词典提示继续: {type(exc).__name__}")

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

    def _record_pending_action_history_if_any() -> bool:
        action_history_text = _consume_pending_action_history_text(event)
        if not action_history_text:
            return False
        max_chars = getattr(runtime.plugin_config, "personification_max_output_chars", 0)
        final_history_text = _build_final_visible_reply_text(
            action_history_text,
            max_chars=max_chars,
            sanitize_history_text=session.sanitize_history_text,
        )
        if not final_history_text:
            return False
        bot_nickname = persona.default_bot_nickname or str(getattr(bot, "self_id", "") or "bot")
        assistant_metadata = {
            "scene": "reply",
            "sticker_sent": None,
            "speaker": bot_nickname,
            "user_id": bot_self_id or None,
            "source_kind": "bot_reply",
        }
        if isinstance(event, types.group_message_event_cls):
            assistant_metadata.update(
                {
                    "group_id": str(event.group_id),
                    "message_id": None,
                    "reply_to_msg_id": incoming_relation_metadata.get("message_id"),
                    "reply_to_user_id": user_id,
                    "mentioned_ids": [],
                    "is_at_bot": False,
                }
            )
        session.append_session_message(
            session_id,
            "assistant",
            final_history_text,
            legacy_session_id=legacy_session_id,
            **assistant_metadata,
        )
        if isinstance(event, types.group_message_event_cls):
            runtime.record_group_msg(
                str(event.group_id),
                bot_nickname,
                final_history_text,
                is_bot=True,
                user_id=bot_self_id,
                reply_to_msg_id=incoming_relation_metadata.get("message_id"),
                reply_to_user_id=user_id,
                mentioned_ids=[],
                source_kind="bot_reply",
            )
        return True

    base_prompt = persona.load_prompt(str(group_id))
    if isinstance(base_prompt, dict):
        from ...core.builtin_hooks import schedule_pending_topic_extraction

        hook_ctx.session_messages = session_messages_for_model
        hook_ctx.semantic_frame = semantic_frame
        await schedule_pending_topic_extraction(hook_ctx)
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="yaml_route",
                label="YAML 回复路径",
                status="info",
                detail="当前人设 prompt 使用 YAML 模式",
            )
        except Exception:
            pass
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
            plugin_episode=(
                conversation_context.plugin_episode
                if conversation_context is not None
                else None
            ),
            semantic_frame=semantic_frame,
            has_newer_batch=_batch_has_newer_messages(state),
            batch_runtime_ref=state.get("batch_runtime_ref"),
            reply_commit_state=state,
            solo_speaker_follow=is_solo_speaker_follow,
            favorability_service=persona.favorability_service,
            reply_required=reply_required,
            response_deadline=response_deadline,
            prepared_inner_state=prepared_semantics.inner_state,
            prepared_emotion_state=prepared_semantics.emotion_state,
            turn_media_context=serialize_turn_media(turn_media_context),
            media_grounding=media_grounding,
            precomputed_image_summary_suffix=image_summary_suffix,
            user_profile_block=user_profile_block,
            profile_service=getattr(runtime, "profile_service", None),
            favorability_context_block=favorability_context_block,
            favorability_turn_id=favorability_turn_id,
            avatar_pair_candidates=avatar_pair_candidates,
            avatar_pair_runtime=runtime,
            qq_outbound_ledger=getattr(runtime, "qq_outbound_ledger", None),
            reply_trace_id=str(state.get("reply_trace_id", "") or ""),
        )
        return

    combined_attitude = favorability_context_block
    emotion_block = prepared_semantics.emotion_block

    hook_ctx.session_messages = session_messages_for_model
    hook_ctx.semantic_frame = semantic_frame
    prelude_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_prelude")
    context_chunks = await get_hook_registry().run_all(hook_ctx, phase="system_context")
    primary_api_type, primary_model = _get_primary_provider_signature(runtime)
    context_chunks = await compress_context_if_needed(
        context_chunks,
        max_tokens=context_token_budget_for_route(primary_api_type, primary_model),
        keep_recent=context_keep_recent_for_route(primary_api_type, primary_model),
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
        primary_api_type=primary_api_type,
        primary_model=primary_model,
        native_search_enabled=should_enable_default_builtin_search(
            runtime.plugin_config,
            get_configured_api_providers=runtime.get_configured_api_providers,
        ),
    )
    if user_profile_block:
        system_prompt += f"\n\n{user_profile_block}"
    if media_grounding:
        system_prompt += f"\n\n{media_grounding}"
    if meme_turn_prompt:
        system_prompt += f"\n\n{meme_turn_prompt}"
    turn_plan = getattr(semantic_frame, "turn_plan", None)
    system_prompt += "\n\n" + build_speech_act_policy_prompt(
        speech_act=str(getattr(turn_plan, "speech_act", getattr(semantic_frame, "speech_act", "")) or ""),
        output_mode=str(getattr(turn_plan, "output_mode", getattr(semantic_frame, "output_mode", "")) or ""),
        session_goal=str(getattr(turn_plan, "session_goal", getattr(semantic_frame, "session_goal", "")) or ""),
        is_group=not is_private_session,
    )
    directed_exchange_prompt = build_directed_exchange_policy_prompt(
        is_direct_mention=is_direct_mention,
        is_group=not is_private_session,
        speech_act=str(getattr(turn_plan, "speech_act", getattr(semantic_frame, "speech_act", "")) or ""),
        output_mode=str(getattr(turn_plan, "output_mode", getattr(semantic_frame, "output_mode", "")) or ""),
    )
    if directed_exchange_prompt:
        system_prompt += "\n\n" + directed_exchange_prompt
    if conversation_context is not None and conversation_context.plugin_episode is not None:
        system_prompt += "\n\n" + build_plugin_interaction_policy_prompt(
            is_direct_mention=is_direct_mention,
        )
    _msg_target = state.get("message_target")
    if _msg_target in (TARGET_OTHERS, TARGET_UNCLEAR):
        system_prompt += (
            "\n[系统提示] 这是多人群聊，当前这句不一定是对你说的。"
            "群友简短的感叹/评价（如『你牛大了/绝了/真的假的/笑死/好家伙』）若是紧跟在别人刚发的"
            "图片/视频/链接/内容之后，通常是在评价那条内容或那个发的人，不是在说你——"
            "不要自作多情当成在夸你或说你，更不要回『谢谢夸奖/突然这么夸我』之类。"
            "只有当对方明确 @你、引用回复你发的消息、或叫你名字/昵称时，才默认是对你说。"
        )
        if _msg_target == TARGET_OTHERS:
            system_prompt += (
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
            "如果上下文和现有证据不足，非强交互直接输出 [NO_REPLY]；"
            "强交互只索取一个明确且对方能提供的必要条件，不要播报无法确认或查证无结果。"
        )
    system_prompt += _build_confidence_style_instruction(
        float(getattr(semantic_frame, "confidence", 0.0) or 0.0),
        is_group=not is_private_session,
    )
    system_prompt += _build_scenario_instruction(
        str(getattr(semantic_frame, "conversation_scenario", "") or ""),
    )
    if qq_expression_enabled(runtime.plugin_config):
        system_prompt += "\n\n" + build_qq_expression_prompt()
    if arbitration == "clarify":
        if is_private_session:
            system_prompt += (
                "\n[系统提示] 这轮高歧义但对方像是在直接问你。"
                "优先用一句短澄清问句确认对象或范围，不要硬猜。"
            )
        else:
            system_prompt += (
                "\n[系统提示] 这轮高歧义但对方像是在直接问你。"
                "群聊里不要用澄清问句追问；能判断就给一句保守短反应，不能判断就输出 [NO_REPLY]。"
            )
    if has_photo_input:
        system_prompt += (
            "\n[系统提示] 当前消息包含真实照片。照片只作为内部语境帮助你理解对方的情绪、关系和意图；"
            "除非对方明确要求说明/识别/翻译图片，最终回复不要讲解、复述或总结画面细节。"
        )
    if tool_image_urls:
        system_prompt += build_direct_visual_identity_guard()
    if batch_event_count > 1 and not is_private_session:
        system_prompt += (
            f"\n[系统提示] 当前是同一时间窗内合并的 {batch_event_count} 条群消息。"
            "先理解这一小批消息之间的承接关系，再决定接哪一句。"
        )
    if (
        str(getattr(runtime.plugin_config, "personification_humanize_fragment_style", "prompt") or "off").strip().lower()
        == "prompt"
    ):
        if is_direct_mention:
            system_prompt += (
                "\n[输出风格] 这是明确叫到你的群聊回合。普通回答保持 1-2 条；"
                "只有调侃、自辩或情绪确实在递进时才拆成 2-4 条短消息。"
                "条与条之间用空行分隔，单条尽量不超过 40 字，每条都要有独立作用。"
            )
        else:
            system_prompt += (
                "\n[输出风格] 像 QQ 群友聊天那样说话：需要多句时拆成 1-3 条短消息，"
                "条与条之间用空行分隔；单条尽量不超过 40 字，口语化，可以只接半句，"
                "不要写成完整段落或书面文。"
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
        call_text_model = runtime.call_ai_api
        route_label = "main"
        if (
            str(message_intent or "").strip() == "banter"
            and not tool_image_urls
            and runtime.lite_call_ai_api is not None
        ):
            call_text_model = runtime.lite_call_ai_api
            route_label = "lite"
        try:
            result = await call_text_model(messages_to_use)
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
                image_urls=photo_image_urls,
                sticker_like=False,
            )
            retry_messages = clone_messages_with_text_suffix(messages_to_use, retry_suffix)
            result = await runtime.call_ai_api(retry_messages)
        if not result and tool_image_urls and direct_image_input and image_input_mode in {"auto", "direct"}:
            runtime.logger.warning("拟人插件：图片输入可能不被支持，改用视觉摘要重试...")
            retry_suffix = image_summary_suffix or await _build_image_summary_suffix(
                runtime=runtime,
                image_urls=photo_image_urls,
                sticker_like=False,
            )
            retry_messages = clone_messages_with_text_suffix(messages_to_use, retry_suffix)
            result = await runtime.call_ai_api(retry_messages)
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="text_model_route",
                label="文本模型路由",
                status="ok",
                detail=f"route={route_label} intent={message_intent}",
            )
        except Exception:
            pass
        return result

    async def _call_persona_responder_model(messages_to_use: List[Dict[str, Any]]) -> str:
        if not bool(getattr(runtime.plugin_config, "personification_persona_responder_json_enabled", False)):
            return await _call_text_model_with_retry(messages_to_use)
        json_messages = with_persona_responder_instruction(
            messages_to_use,
            semantic_frame=semantic_frame,
            is_direct_mention=is_direct_mention,
            reply_required=reply_required,
            relationship_hint=relationship_hint,
            recent_bot_replies=recent_bot_replies,
            message_text=raw_message_text or message_text or message_content,
            lorebook_enabled=bool(getattr(runtime.plugin_config, "personification_lorebook_enabled", False)),
            memory_store=getattr(runtime, "memory_store", None),
        )
        raw = await _call_text_model_with_retry(json_messages)
        parsed = parse_persona_response(raw)
        if parsed is None:
            runtime.logger.warning("拟人插件：PersonaResponder JSON 解析失败，按普通文本处理。")
            return raw
        apply_persona_response_to_semantic_frame(parsed, semantic_frame)
        return parsed.reply_text

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
        pending_action_executor = None
        pending_actions: list[dict[str, Any]] = []
        agent_failure_code = ""
        agent_suppress_reply_recovery = False
        agent_direct_output = False
        agent_quality_context = ""
        favorability_committed = False

        def _commit_favorability_if_confirmed() -> None:
            nonlocal favorability_committed
            if favorability_committed or not bool(state.get("reply_delivery_confirmed", False)):
                return
            favorability_committed = True
            try:
                commit_favorability_turn(
                    service=getattr(persona, "favorability_service", None),
                    user_id=user_id,
                    group_id=str(group_id),
                    is_private=is_private_session,
                    is_direct=bool(is_direct_mention or not is_random_chat or is_active_followup),
                    is_random_chat=bool(is_random_chat),
                    signals=favorability_signals,
                    turn_id=favorability_turn_id,
                    now=runtime.get_current_time(),
                )
            except Exception as exc:
                runtime.logger.debug(f"拟人插件：提交回复好感事件失败: {exc}")

        def _confirm_reply_delivery() -> None:
            mark_reply_delivery_confirmed(state)
            _commit_favorability_if_confirmed()

        async def _send_reply(payload: Any) -> Any:
            if getattr(runtime, "user_policy_gate", None) is not None:
                await runtime.user_policy_gate.ensure_current(event)
            mark_reply_delivery_started(state)
            result = await _dispatch_reply_part(
                bot=bot,
                event=event,
                payload=payload,
                ledger=getattr(runtime, "qq_outbound_ledger", None),
                surface="normal_reply",
                reply_trace_id=str(state.get("reply_trace_id", "") or ""),
            )
            if not isinstance(result, SendReceipt) or result.status == "sent":
                _confirm_reply_delivery()
            return result

        def _message_id_from_send_result(send_result: Any) -> str:
            if isinstance(send_result, SendReceipt):
                return str(send_result.message_id or "")
            return extract_send_message_id(send_result)

        def _finish_action_only_trace() -> None:
            try:
                from ...core import reply_turn_trace

                reply_turn_trace.finish_trace(
                    outcome="ok",
                    diagnosis_code="ok",
                    detail={"action_only": True},
                )
            except Exception:
                pass

        def _finish_suppressed_reply_trace() -> None:
            try:
                from ...core import reply_turn_trace

                if agent_quality_context == "evidence_unavailable":
                    reply_turn_trace.finish_trace(
                        outcome="no_reply",
                        diagnosis_code="evidence_unavailable",
                        detail={"silent": True, "evidence_unavailable": True},
                    )
                    return
                if agent_quality_context == "uncertain_reply":
                    reply_turn_trace.finish_trace(
                        outcome="no_reply",
                        diagnosis_code="uncertain_reply_silenced",
                        detail={"silent": True, "uncertain_reply": True},
                    )
                    return
                reply_turn_trace.finish_trace(
                    outcome="no_reply",
                    diagnosis_code="background_action_pending",
                    detail={"silent": True, "background_action": True},
                )
            except Exception:
                pass

        async def _commit_pending_actions() -> None:
            if not pending_actions:
                return
            if getattr(runtime, "user_policy_gate", None) is not None:
                await runtime.user_policy_gate.ensure_current(event)
            mark_reply_phase(state, "delivery_commit_wait")
            await acquire_reply_commit(state)
            mark_reply_phase(state, "delivery")
            stale_reason = _stale_reply_abort_reason(state)
            if stale_reason:
                runtime.logger.info(f"拟人插件：{stale_reason}")
                pending_actions.clear()
                return
            history_parts = await execute_pending_actions(
                pending_action_executor,
                pending_actions,
                state=state,
            )
            _commit_favorability_if_confirmed()
            if history_parts:
                setattr(
                    event,
                    "_personification_pending_action_history_text",
                    " ".join(history_parts),
                )
        if _should_use_agent_for_reply(
            plugin_config=runtime.plugin_config,
            tool_registry=runtime.tool_registry,
            agent_tool_caller=runtime.agent_tool_caller,
            message_intent=message_intent,
            ambiguity_level=intent_decision.ambiguity_level,
            is_direct_mention=is_direct_mention,
            has_image_input=bool(tool_image_urls),
        ):
            if getattr(runtime, "user_policy_gate", None) is not None:
                await runtime.user_policy_gate.ensure_current(event)
            image_ctx_token = set_current_image_context(tool_image_urls, message_content)
            try:
                try:
                    try:
                        from ...core import reply_turn_trace

                        reply_turn_trace.record_stage(
                            key="agent_start",
                            label="Agent 主循环",
                            status="info",
                            detail=(
                                f"intent={message_intent} images={len(tool_image_urls)} "
                                f"direct_image={agent_direct_image_input} "
                                f"elapsed_ms=0 turn_age_ms={int((time.monotonic() - started_at) * 1000)}"
                            ),
                        )
                    except Exception:
                        pass
                    agent_started_at = time.monotonic()
                    (
                        reply_content,
                        used_agent,
                        bypass_length_limits,
                        pending_action_executor,
                        pending_actions,
                        agent_failure_code,
                        agent_suppress_reply_recovery,
                        agent_direct_output,
                        agent_quality_context,
                    ) = await _run_agent_if_enabled(
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
                        turn_plan=getattr(semantic_frame, "turn_plan", None),
                        started_at=started_at,
                        is_direct_mention=is_direct_mention,
                        reply_required=reply_required,
                        response_timeout_seconds=float(
                            getattr(runtime.plugin_config, "personification_response_timeout", 180) or 180
                        ),
                        response_deadline=response_deadline,
                        task_exc_logger=_task_exc_logger,
                        reply_commit_state=state,
                        turn_media_context=turn_media_context,
                        avatar_pair_candidates=avatar_pair_candidates,
                    )
                    try:
                        from ...core import reply_turn_trace

                        reply_turn_trace.record_stage(
                            key="agent_result",
                            label="Agent 结果",
                            status="ok" if reply_content else "warn",
                            detail=(
                                f"used={used_agent} chars={len(str(reply_content or ''))} "
                                f"agent_elapsed_ms={int((time.monotonic() - agent_started_at) * 1000)} "
                                f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
                            ),
                        )
                    except Exception:
                        pass
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
        if used_agent and agent_failure_code:
            pending_actions.clear()
            delivery_started = bool(state.get("reply_delivery_started", False))
            delivery_confirmed = bool(state.get("reply_delivery_confirmed", False))
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
            runtime.logger.warning(
                f"拟人插件：Agent 基础设施失败，保持静默: code={agent_failure_code} "
                f"delivery_state={delivery_state}"
            )
            try:
                from ...core import reply_turn_trace

                reply_turn_trace.record_stage(
                    key="agent_operational_failure",
                    label="Agent 基础设施失败",
                    status="warn" if delivery_confirmed else "error",
                    detail=f"code={agent_failure_code} delivery_state={delivery_state} silent=true",
                )
                reply_turn_trace.finish_trace(
                    outcome=trace_outcome,
                    diagnosis_code=diagnosis_code,
                    detail={
                        "silent": True,
                        "delivery_state": delivery_state,
                        "failure_code": agent_failure_code,
                    },
                )
            except Exception:
                pass
            return
        if used_agent and required_reply_needs_recovery(
            reply_content,
            reply_required=reply_required,
            pending_actions=pending_actions,
            direct_output=bool(agent_direct_output or agent_suppress_reply_recovery),
        ):
            runtime.logger.warning("拟人插件：强交互 Agent 返回静默，改走基础模型恢复。")
            reply_content = ""
            used_agent = False
        if used_agent and reply_content in ("[NO_REPLY]", "<NO_REPLY>"):
            runtime.logger.info("拟人插件：Agent 返回 NO_REPLY，保持沉默。")
            try:
                from ...core import reply_turn_trace

                reply_turn_trace.record_stage(
                    key="no_reply",
                    label="静默",
                    status="warn",
                    detail="agent returned NO_REPLY",
                )
                reply_turn_trace.finish_trace(outcome="no_reply", diagnosis_code="no_reply", detail={"reason": "agent_no_reply"})
            except Exception:
                pass
            await _maybe_silence_reaction()
            return
        if not used_agent:
            fallback_started_at = time.monotonic()
            try:
                from ...core import reply_turn_trace

                reply_turn_trace.record_stage(
                    key="fallback_model_start",
                    label="基础模型",
                    status="info",
                    detail=f"intent={message_intent} elapsed_ms={int((fallback_started_at - started_at) * 1000)}",
                )
            except Exception:
                pass
            reply_content = await _call_persona_responder_model(fallback_model_messages)
            try:
                from ...core import reply_turn_trace

                reply_turn_trace.record_stage(
                    key="fallback_model_result",
                    label="基础模型结果",
                    status="ok" if reply_content else "warn",
                    detail=(
                        f"chars={len(str(reply_content or ''))} "
                        f"model_elapsed_ms={int((time.monotonic() - fallback_started_at) * 1000)} "
                        f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
                    ),
                )
            except Exception:
                pass
            bypass_length_limits = False
            if not reply_content:
                runtime.logger.warning("拟人插件：未能获取到 AI 回复内容")
                if reply_required:
                    reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
                else:
                    try:
                        from ...core import reply_turn_trace

                        reply_turn_trace.record_stage(
                            key="no_reply",
                            label="静默",
                            status="error",
                            detail="empty model reply",
                            hint="模型返回空内容或 provider 链路失败",
                        )
                        reply_turn_trace.finish_trace(outcome="no_reply", diagnosis_code="model_empty", detail={"reason": "empty_reply"})
                    except Exception:
                        pass
                    return
            if needs_uncertain_visible_reply_review(
                ambiguity_level=getattr(intent_decision, "ambiguity_level", ""),
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
                    runtime.review_call_ai_api or runtime.lite_call_ai_api or runtime.call_ai_api,
                    candidate_text=reply_content,
                    raw_message_text=raw_message_text or message_text or message_content,
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
                    agent_suppress_reply_recovery = True
                    agent_quality_context = "uncertain_reply"
                try:
                    from ...core import reply_turn_trace

                    reply_turn_trace.record_stage(
                        key="uncertain_reply_review",
                        label="高歧义回复收口",
                        status="ok" if uncertain_decision.action in {"accept", "request_context"} else "warn",
                        detail=(
                            f"action={uncertain_decision.action} "
                            f"flags={','.join(uncertain_decision.flags) or '-'} "
                            f"elapsed_ms={int((time.monotonic() - uncertain_started_at) * 1000)}"
                        ),
                    )
                except Exception:
                    pass
        elif not agent_direct_output and is_agent_reply_ooc(reply_content):
            rewritten_ooc = await rewrite_agent_reply_ooc(
                tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
                original_text=reply_content,
                persona_system=system_prompt,
                output_mode=str(getattr(semantic_frame, "output_mode", "chat_short") or "chat_short"),
                avoid_questions=not is_private_session,
                allow_rhetorical_banter=bool(
                    is_direct_mention
                    and str(getattr(turn_plan, "speech_act", "") or "") in {"", "participate", "tease"}
                ),
            )
            if rewritten_ooc:
                reply_content = rewritten_ooc
            else:
                reply_content = "[SILENCE]"

        stale_reason = _stale_reply_abort_reason(state)
        if stale_reason:
            runtime.logger.info(f"拟人插件：{stale_reason}")
            try:
                from ...core import reply_turn_trace

                reply_turn_trace.record_stage(
                    key="stale_abort",
                    label="旧批次丢弃",
                    status="warn",
                    detail=stale_reason,
                )
                reply_turn_trace.finish_trace(outcome="no_reply", diagnosis_code="stale_reply", detail={"reason": stale_reason})
            except Exception:
                pass
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
        ) and not used_agent:
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
        if required_reply_needs_recovery(
            reply_content,
            reply_required=reply_required,
            pending_actions=pending_actions,
            direct_output=bool(agent_direct_output or agent_suppress_reply_recovery),
        ):
            reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
        reply_content, legacy_favorability_signals = extract_legacy_favorability_markers(reply_content)
        favorability_signals.merge(legacy_favorability_signals)
        has_block_marker = "[BLOCK]" in reply_content or "<BLOCK>" in reply_content
        if has_block_marker:
            reply_content = reply_content.replace("[BLOCK]", "").replace("<BLOCK>", "").strip()

        has_silence_marker = has_silence_control_marker(reply_content)
        if has_silence_marker:
            await _commit_pending_actions()
            if _record_pending_action_history_if_any():
                runtime.logger.info("拟人插件：Agent 静默动作已写入会话历史。")
            runtime.logger.info(f"AI 决定结束与群 {group_id} 中 {user_name}({user_id}) 的对话 (SILENCE)")
            if bool(state.get("reply_delivery_confirmed", False)):
                mark_reply_delivery_complete(state)
                release_reply_commit(state)
                mark_reply_phase(state, "reply_complete")
                _finish_action_only_trace()
                return
            if agent_suppress_reply_recovery:
                _finish_suppressed_reply_trace()
                return
            if reply_required:
                reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
            else:
                return

        if used_agent and has_silence_control_marker(reply_content):
            runtime.logger.info("拟人插件：Agent 文本含 NO_REPLY 标记，保持沉默。")
            await _maybe_silence_reaction()
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
            if reply_required:
                reply_content = "这个我不能接。"
                has_block_marker = False
            else:
                return

        if not used_agent and ("[NO_REPLY]" in reply_content or "<NO_REPLY>" in reply_content):
            runtime.logger.info(
                f"AI 选择不回复群 {group_id} 中 {user_name}({user_id}) 的消息 (NO_REPLY)"
            )
            await _maybe_silence_reaction()
            return

        if not agent_direct_output and not is_private_session and message_intent == "banter":
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

        care_review_required = bool(
            getattr(semantic_frame, "requires_emotional_care", False)
            or getattr(getattr(semantic_frame, "emotional_support", None), "needed", False)
        )
        should_review_visual_reply = bool(turn_media_context and not _IMAGE_B64_RE.search(reply_content or ""))
        should_review_agent_reply = bool(used_agent and should_review_visual_reply)
        plugin_episode = conversation_context.plugin_episode if conversation_context is not None else None
        protected_review_required = bool(
            plugin_episode is not None or detect_persona_identity_leak(reply_content)
        )
        if agent_direct_output and not protected_review_required:
            review_decision = make_passthrough_review_decision(
                reply_content,
                reason="safe_direct_output",
            )
        elif used_agent and not should_review_agent_reply and not care_review_required and not protected_review_required:
            review_decision = make_passthrough_review_decision(
                reply_content,
                reason="agent_passthrough",
            )
        elif (
            not care_review_required
            and not should_review_visual_reply
            and not protected_review_required
            and not bool(getattr(runtime.plugin_config, "personification_response_review_enabled", False))
        ):
            review_decision = make_passthrough_review_decision(
                reply_content,
                reason="review_disabled",
            )
        else:
            review_decision = await review_response_text(
                runtime.review_call_ai_api or runtime.lite_call_ai_api or runtime.call_ai_api,
                candidate_text=reply_content,
                raw_message_text=raw_message_text or message_text or message_content,
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
                turn_media_context=turn_media_context,
                plugin_episode=plugin_episode,
            )
        if review_decision.action == "no_reply":
            runtime.logger.info(f"拟人插件：回复审阅后选择沉默，group={group_id} user={user_id}")
            return
        if review_decision.action == "rewrite" and review_decision.text:
            reply_content = review_decision.text.strip()

        reply_content, reviewed_favorability_signals = extract_legacy_favorability_markers(reply_content)
        favorability_signals.merge(reviewed_favorability_signals)

        if (
            has_silence_control_marker(reply_content)
            and reply_required
            and not pending_actions
            and not agent_suppress_reply_recovery
        ):
            reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
        if has_silence_control_marker(reply_content):
            await _commit_pending_actions()
            if _record_pending_action_history_if_any():
                runtime.logger.info("拟人插件：静默动作已写入会话历史。")
            runtime.logger.info(
                f"拟人插件：最终回复含沉默控制标记，group={group_id} user={user_id}"
            )
            if bool(state.get("reply_delivery_confirmed", False)):
                mark_reply_delivery_complete(state)
                release_reply_commit(state)
                mark_reply_phase(state, "reply_complete")
                _finish_action_only_trace()
                return
            if agent_suppress_reply_recovery:
                _finish_suppressed_reply_trace()
                return
            if reply_required:
                reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))
            else:
                return
        # 兼容 yaml_pipeline prompt 的 <output><message>...</message></output> 思维链结构：
        # 若 LLM 把回复包在 <message> 里（多条），用 \n\n 串接保留分段，下游 _split_segments 会再拆。
        try:
            parsed_yaml = parse_yaml_response(reply_content)
        except Exception:
            parsed_yaml = {"messages": []}
        if parsed_yaml.get("messages"):
            joined = "\n\n".join(
                str(item.get("text", "")).strip()
                for item in parsed_yaml["messages"]
                if str(item.get("text", "")).strip()
            )
            if joined:
                reply_content = joined
        reply_content = strip_response_control_markers(reply_content)
        reply_content = normalize_visible_reply_text(reply_content)
        if not reply_content and not _IMAGE_B64_RE.search(str(reply_content or "")):
            if agent_suppress_reply_recovery:
                _finish_suppressed_reply_trace()
                return
            if not reply_required:
                return
            reply_content = required_reply_fallback_text(has_images=bool(tool_image_urls))

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
        final_reply = normalize_visible_reply_text(reply_content)
        from ...core.visible_output import guard_visible_text

        final_reply = guard_visible_text(final_reply, logger=runtime.logger, surface="normal_reply")
        if not final_reply and not _IMAGE_B64_RE.search(str(reply_content or "")):
            return
        qq_auto_marker = maybe_choose_auto_qq_expression_marker(
            plugin_config=runtime.plugin_config,
            semantic_frame=semantic_frame,
            reply_text=final_reply,
            raw_message_text=raw_message_text or message_text or message_content,
            message_intent=message_intent,
            group_id=str(group_id),
            user_id=user_id,
            is_private=is_private_session,
            is_random_chat=is_random_chat,
            force_mode=force_mode,
            has_rich_sticker=bool(sticker_segment),
        )
        if qq_auto_marker:
            if message_intent == "expression" and not contains_qq_expression_marker(final_reply):
                final_reply = qq_auto_marker
            else:
                final_reply = f"{final_reply}{qq_auto_marker}".strip()
        max_chars = 0 if bypass_length_limits else getattr(runtime.plugin_config, "personification_max_output_chars", 0)
        final_reply, image_b64_payloads = _extract_image_b64_markers(final_reply)
        if max_chars and max_chars > 0 and len(final_reply) > max_chars:
            final_reply = _truncate_at_punctuation(final_reply, max_chars)
        # session/history 只记录最终对用户生效的文本，避免原始长回复与实际可见内容漂移。
        final_visible_reply_text = _build_final_visible_reply_text(
            history_text_for_qq_expression(final_reply) or ("[发送了一张图片]" if image_b64_payloads else ""),
            max_chars=max_chars,
            sanitize_history_text=session.sanitize_history_text,
        )
        sent_message_id = ""
        sent_as_tts = False
        delivery_partial = False
        delivery_unknown = False

        def _mark_tts_delivery_unknown() -> None:
            nonlocal delivery_unknown
            delivery_unknown = True
        tts_service = getattr(runtime, "tts_service", None)
        stale_reason = _stale_reply_abort_reason(state)
        if stale_reason:
            runtime.logger.info(f"拟人插件：{stale_reason}")
            return
        mark_reply_phase(state, "delivery_commit_wait")
        await acquire_reply_commit(state)
        delivery_started_at = time.monotonic()
        mark_reply_phase(state, "delivery")
        stale_reason = _stale_reply_abort_reason(state)
        if stale_reason:
            runtime.logger.info(f"拟人插件：{stale_reason}")
            return
        if getattr(runtime, "user_policy_gate", None) is not None:
            await runtime.user_policy_gate.ensure_current(event)
        await _commit_pending_actions()
        if (
            final_reply
            and not sticker_segment
            and not contains_qq_expression_marker(final_reply)
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
                        on_delivery_started=lambda: mark_reply_delivery_started(state),
                        on_delivery_confirmed=_confirm_reply_delivery,
                        on_delivery_unknown=_mark_tts_delivery_unknown,
                        operation_id=str(state.get("reply_trace_id", "") or ""),
                        user_target=user_id,
                    )
            except Exception as e:
                likely_delivered = is_likely_delivered_send_timeout(e)
                if bool(state.get("reply_delivery_confirmed", False)) or likely_delivered:
                    sent_as_tts = True
                    delivery_unknown = likely_delivered
                    delivery_partial = not likely_delivered
                    runtime.logger.warning(f"[tts] 自动语音发送结果不完整，不重复发送完整文字: {e}")
                else:
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

                typo_correction: str | None = None
                if message_intent == "banter" and not looks_like_explanatory_output(final_reply):
                    typo_prob = float(
                        getattr(runtime.plugin_config, "personification_humanize_typo_probability", 0.0) or 0.0
                    )
                    if typo_prob > 0 and segments:
                        typo_idx = random.randrange(len(segments))
                        mutated, typo_correction = _humanize.maybe_inject_typo(
                            segments[typo_idx], probability=typo_prob
                        )
                        segments[typo_idx] = mutated

                address_plan = _humanize.decide_addressing(
                    plugin_config=runtime.plugin_config,
                    state=state,
                    event=event,
                    group_id=str(group_id),
                    user_id=user_id,
                    is_private=is_private_session,
                    has_newer_batch=_batch_has_newer_messages(state),
                    address_mode=getattr(semantic_frame, "address_mode", "auto"),
                )
                quote_message_id = address_plan.get("quote_message_id")
                at_target = address_plan.get("at_target")
                try:
                    from ...core import reply_turn_trace

                    reply_turn_trace.record_stage(
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
                except Exception:
                    pass

                humanize_typing = _humanize.typing_enabled(runtime.plugin_config)
                typing_cps = float(
                    getattr(runtime.plugin_config, "personification_humanize_typing_cps", 7.0) or 7.0
                )
                typing_max_delay = float(
                    getattr(runtime.plugin_config, "personification_humanize_typing_max_delay", 5.0) or 0.0
                )
                if humanize_typing and segments:
                    try:
                        current_hour = runtime.get_current_time().hour
                        is_night = current_hour >= 23 or current_hour < 7
                    except Exception:
                        is_night = False
                    first_delay = _humanize.compute_typing_delay(
                        segments[0],
                        cps=typing_cps,
                        max_delay=typing_max_delay,
                        already_elapsed=time.monotonic() - started_at,
                        night=is_night,
                    )
                    if first_delay > 0.05:
                        try:
                            from ...core import reply_turn_trace

                            reply_turn_trace.record_stage(
                                key="humanize_delay",
                                label="拟人化延迟",
                                status="info",
                                detail=(
                                    f"typing_delay_ms={int(first_delay * 1000)} "
                                    f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
                                ),
                            )
                        except Exception:
                            pass
                        if (
                            is_private_session
                            and first_delay > 1.5
                            and bool(
                                getattr(
                                    runtime.plugin_config,
                                    "personification_humanize_input_status_enabled",
                                    True,
                                )
                            )
                        ):
                            await _protocol_caps.set_typing(
                                bot, runtime.plugin_config, user_id=user_id, logger=runtime.logger
                            )
                        await asyncio.sleep(first_delay)

                for i, seg in enumerate(segments):
                    if not seg.strip():
                        continue
                    stale_reason = _stale_reply_abort_reason(state)
                    if stale_reason:
                        runtime.logger.info(f"拟人插件：{stale_reason}")
                        return
                    rendered_seg = await render_qq_expression_message(
                        seg,
                        message_segment_cls=runtime.message_segment_cls,
                        bot=bot,
                        plugin_config=runtime.plugin_config,
                        logger=runtime.logger,
                    )
                    outgoing: Any = rendered_seg.message
                    if not outgoing:
                        continue
                    if i == 0:
                        try:
                            outgoing = _humanize.prepend_addressing_segments(
                                message_segment_cls=runtime.message_segment_cls,
                                outgoing=outgoing,
                                quote_message_id=quote_message_id,
                                at_target=at_target,
                            )
                        except Exception:
                            outgoing = rendered_seg.message
                    send_result = await _send_reply(outgoing)
                    if not sent_message_id:
                        sent_message_id = _message_id_from_send_result(send_result)
                    if i < len(segments) - 1 or sticker_segment:
                        if humanize_typing and i < len(segments) - 1:
                            await asyncio.sleep(
                                _humanize.compute_gap_delay(
                                    segments[i + 1], cps=typing_cps, max_delay=typing_max_delay
                                )
                            )
                        else:
                            await asyncio.sleep(random.uniform(0.8, 1.6))

                if typo_correction and not _stale_reply_abort_reason(state):
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    try:
                        await _send_reply(typo_correction)
                    except Exception as exc:
                        runtime.logger.debug(f"[humanize] 修正消息发送失败: {exc}")

        for image_b64 in image_b64_payloads:
            stale_reason = _stale_reply_abort_reason(state)
            if stale_reason:
                runtime.logger.info(f"拟人插件：{stale_reason}")
                return
            send_result = await _send_reply(runtime.message_segment_cls.image(f"base64://{image_b64}"))
            if not sent_message_id:
                sent_message_id = _message_id_from_send_result(send_result)
            if sticker_segment:
                await asyncio.sleep(random.uniform(0.8, 1.6))

        if sticker_segment:
            stale_reason = _stale_reply_abort_reason(state)
            if stale_reason:
                runtime.logger.info(f"拟人插件：{stale_reason}")
                return
            send_result = await _send_reply(sticker_segment)
            if not sent_message_id:
                sent_message_id = _message_id_from_send_result(send_result)
            if sticker_name:
                mark_pending_sticker_reaction(
                    build_sticker_feedback_scene_key(
                        group_id=str(group_id),
                        user_id=user_id,
                        is_private=is_private_session,
                    ),
                    sticker_name,
                )

        if not delivery_partial and not delivery_unknown:
            mark_reply_delivery_complete(state)
        if getattr(runtime, "user_policy_gate", None) is not None:
            await runtime.user_policy_gate.ensure_current(event)
        mark_reply_phase(state, "delivery_history_commit")
        assistant_metadata = {
            "scene": "reply",
            "sticker_sent": sticker_name if sticker_name else None,
            "speaker": bot_nickname,
            "user_id": bot_self_id or None,
            "source_kind": "bot_reply",
        }
        if isinstance(event, types.group_message_event_cls):
            assistant_metadata.update(
                {
                    "group_id": str(event.group_id),
                    "message_id": sent_message_id or None,
                    "reply_to_msg_id": incoming_relation_metadata.get("message_id"),
                    "reply_to_user_id": user_id,
                    "mentioned_ids": [str(at_target)] if at_target else [],
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
        release_reply_commit(state)
        delivery_elapsed_ms = int((time.monotonic() - delivery_started_at) * 1000)
        mark_reply_phase(state, "post_send_bookkeeping")
        bookkeeping_started_at = time.monotonic()
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="delivery_complete",
                label="交付完成",
                status="warn" if delivery_partial or delivery_unknown else "ok",
                detail=(
                    f"elapsed_ms={delivery_elapsed_ms} "
                    f"confirmed={bool(state.get('reply_delivery_confirmed', False))} "
                    f"complete={bool(state.get('reply_delivery_complete', False))}"
                ),
            )
            reply_turn_trace.record_stage(
                key="outgoing_message",
                label="发送消息",
                status="ok",
                detail=str(final_visible_reply_text or "")[:500],
                elapsed_ms=0,
            )
        except Exception:
            pass
        if sticker_name:
            await record_sticker_sent(sticker_name)
        await persist_reply_emotion_state(
            runtime=runtime,
            data_dir=data_dir,
            user_id=user_id,
            group_id=str(group_id),
            semantic_frame=semantic_frame,
            assistant_text=final_visible_reply_text,
            is_private=is_private_session,
        )
        schedule_inner_state_update_after_reply(
            runtime=runtime,
            user_text=raw_message_text or message_text or message_content,
            assistant_text=final_visible_reply_text,
            user_id=user_id,
            group_id=str(group_id),
            is_private=is_private_session,
            semantic_frame=semantic_frame,
            task_exc_logger=_task_exc_logger,
        )

        if not is_private_session and bool(getattr(runtime.plugin_config, "personification_relation_evolution_enabled", False)):
            async def _spawn_relation_evolution() -> None:
                try:
                    from ...core.evolve_group_relations import evolve_group_relations, list_group_relations
                    current_relations = list_group_relations(runtime.memory_store, str(group_id))
                    current_tags = list(set(
                        str(r.get("tag", "")).strip()
                        for r in current_relations
                        if str(r.get("tag", "")).strip()
                    ))
                    turn_summary = f"回复: {str(final_visible_reply_text)[:200]} | 意图: {message_intent} | 原话: {str(raw_message_text or message_text or message_content)[:200]}"
                    await evolve_group_relations(
                        tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
                        memory_store=runtime.memory_store,
                        group_id=str(group_id),
                        user_id=user_id,
                        turn_summary=turn_summary,
                        current_tags=current_tags,
                        plugin_config=runtime.plugin_config,
                    )
                except Exception:
                    pass
            asyncio.create_task(_spawn_relation_evolution())

        if getattr(runtime, "memory_curator", None) is not None:
            memory_group_id = "" if is_private_session else str(group_id)
            if hasattr(runtime.memory_curator, "schedule_turn_capture"):
                runtime.memory_curator.schedule_turn_capture(
                    user_utterance=raw_message_text or message_text or message_content,
                    bot_response=final_visible_reply_text,
                    user_id=user_id,
                    group_id=memory_group_id,
                    vision_summary=image_summary_suffix,
                    semantic_frame=semantic_frame,
                    scope=f"group:{memory_group_id}" if memory_group_id else f"user:{user_id}",
                )
            else:
                runtime.memory_curator.schedule_capture(
                    summary=final_visible_reply_text,
                    user_id=user_id,
                    group_id=memory_group_id,
                    topic_tags=[str(group_id)] if not is_private_session else [],
                )

        if isinstance(event, types.group_message_event_cls):
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
        bookkeeping_elapsed_ms = int((time.monotonic() - bookkeeping_started_at) * 1000)
        mark_reply_phase(state, "reply_complete")
        record_timing(
            "reply_processor.total_ms",
            (time.monotonic() - started_at) * 1000.0,
            scene="private" if is_private_session else "group",
        )
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="post_send_bookkeeping",
                label="发送后状态写入",
                status="ok",
                detail=f"elapsed_ms={bookkeeping_elapsed_ms}",
            )
            completion = resolve_sent_reply_completion(
                state=state,
                visible_text=final_visible_reply_text,
                delivery_partial=delivery_partial,
                delivery_unknown=delivery_unknown,
            )
            reply_turn_trace.record_stage(
                key="reply_success",
                label="回复完成",
                status="ok" if completion["outcome"] == "ok" else "warn",
                detail=(
                    f"chars={len(final_visible_reply_text)} tts={bool(sent_as_tts)} "
                    f"sticker={bool(sticker_name)} tool_execution={completion['tool_execution']} "
                    f"evidence_delivery={completion['evidence_delivery']} "
                    f"outbound_delivery={completion['outbound_delivery']}"
                ),
            )
            reply_turn_trace.finish_trace(
                outcome=completion["outcome"],
                diagnosis_code=completion["diagnosis_code"],
                detail={
                    "reply_chars": len(final_visible_reply_text),
                    "tts": bool(sent_as_tts),
                    "sticker": bool(sticker_name),
                    "delivery_partial": delivery_partial,
                    "delivery_unknown": delivery_unknown,
                    "tool_execution": completion["tool_execution"],
                    "evidence_delivery": completion["evidence_delivery"],
                    "outbound_delivery": completion["outbound_delivery"],
                    "social_coverage_status": completion["coverage_status"],
                    "evidence_recovered": completion["evidence_recovered"],
                    "incoming_text": str(raw_message_text or message_text or message_content or "")[:500],
                    "outgoing_text": str(final_visible_reply_text or "")[:500],
                },
            )
        except Exception:
            pass
    except QQPolicyBlockedDuringTurn:
        runtime.logger.info(f"拟人插件：用户 {user_id or '-'} policy 状态已变化，本轮立即静默终止。")
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.finish_trace(
                outcome="no_reply",
                diagnosis_code="user_policy_blocked",
                detail={"silent": True},
            )
        except Exception:
            pass
    except FinishedException:
        raise
    except Exception as e:
        record_counter("reply_processor.error_total")
        provider_code = _provider_diagnosis_code(e)
        delivery_started = bool(state.get("reply_delivery_started", False))
        delivery_confirmed = bool(state.get("reply_delivery_confirmed", False))
        delivery_complete = bool(state.get("reply_delivery_complete", False))
        if delivery_complete:
            delivery_state = "complete"
            trace_outcome = "ok"
            diagnosis_code = "post_send_provider_failure" if provider_code else "post_send_internal_exception"
        elif delivery_confirmed:
            delivery_state = "partial"
            trace_outcome = "partial"
            diagnosis_code = "partial_provider_failure" if provider_code else "partial_internal_exception"
        elif delivery_started:
            delivery_state = "dispatching"
            trace_outcome = "failed"
            diagnosis_code = "outbound_send_failed"
        else:
            delivery_state = "not_started"
            trace_outcome = "failed"
            diagnosis_code = provider_code or "internal_exception"
        error_summary = (
            " ".join(
                part
                for part in (
                    f"code={provider_code}",
                    f"type={type(e).__name__}",
                    summarize_provider_route_attempts(e),
                )
                if part
            )
            if provider_code
            else f"type={type(e).__name__}"
        )
        runtime.logger.error(f"拟人插件 API 调用失败: {error_summary}")
        try:
            from ...core import reply_turn_trace

            reply_turn_trace.record_stage(
                key="provider_failure" if provider_code else "reply_failed",
                label="Provider 调用失败" if provider_code else "回复异常",
                status="warn" if delivery_confirmed else "error",
                detail=f"{error_summary} delivery_state={delivery_state} silent=true",
            )
            reply_turn_trace.finish_trace(
                outcome=trace_outcome,
                diagnosis_code=diagnosis_code,
                detail={
                    "error": error_summary,
                    "silent": True,
                    "delivery_state": delivery_state,
                    "delivery_started": delivery_started,
                    "delivery_confirmed": delivery_confirmed,
                    "delivery_complete": delivery_complete,
                },
            )
        except Exception:
            pass
