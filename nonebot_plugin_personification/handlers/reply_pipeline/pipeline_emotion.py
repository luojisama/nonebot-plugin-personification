from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

from ...agent.inner_state import DEFAULT_STATE as DEFAULT_INNER_STATE, get_personification_data_dir, load_inner_state
from ...agent.runtime.planner import (
    metadata_fallback_turn_plan,
    plan_turn_with_llm,
    turn_plan_from_semantic_frame,
    turn_plan_to_semantic_frame,
)
from ...agent.runtime.tool_catalog import registry_planner_metadata
from ...core.chat_intent import (
    infer_turn_semantic_frame_with_llm,
    metadata_fallback_turn_semantic_frame_for_session,
)
from ...core.emotion_state import (
    build_turn_emotion_prompt_block,
    load_emotion_state,
    render_emotion_memory_hint,
    render_inner_state_hint,
    update_emotion_state_after_turn,
)
from ...core.metrics import record_counter, record_timing
from ...core.prompts import load_prompt
from ...core.response_review import (
    arbitrate_reply_mode,
    extract_recent_bot_reply_texts,
)
from ...core.target_inference import normalize_message_target_for_plan, normalize_message_target_for_review
from .pipeline_context import batch_has_newer_messages

_EMOTIONAL_SUPPORT_HINT = load_prompt("emotional_support_hint")
_DEFAULT_SEMANTIC_FRAME_TIMEOUT_SECONDS = 8.0
_MIN_SEMANTIC_FRAME_TIMEOUT_SECONDS = 1.0
_MAX_SEMANTIC_FRAME_TIMEOUT_SECONDS = 60.0


def _record_reply_trace_stage(
    *,
    key: str,
    label: str,
    status: str = "info",
    detail: Any = "",
    hint: str = "",
) -> None:
    try:
        from ...core import reply_turn_trace

        reply_turn_trace.record_stage(
            key=key,
            label=label,
            status=status,
            detail=detail,
            hint=hint,
        )
    except Exception:
        pass


def semantic_frame_timeout_seconds(plugin_config: Any) -> float:
    raw = getattr(plugin_config, "personification_semantic_frame_timeout", _DEFAULT_SEMANTIC_FRAME_TIMEOUT_SECONDS)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = _DEFAULT_SEMANTIC_FRAME_TIMEOUT_SECONDS
    if not math.isfinite(seconds):
        seconds = _DEFAULT_SEMANTIC_FRAME_TIMEOUT_SECONDS
    return max(_MIN_SEMANTIC_FRAME_TIMEOUT_SECONDS, min(_MAX_SEMANTIC_FRAME_TIMEOUT_SECONDS, seconds))


def semantic_frame_timeout_hint(timeout_s: float) -> str:
    return (
        f"前置语义阶段在 {timeout_s:g}s 总预算内没有拿到可用 LLM 判断，最终才使用 metadata fallback；"
        "若频繁出现，优先检查 personification_lite_model / personification_model_overrides.intent "
        "和主模型路由健康度，再调整 personification_semantic_frame_timeout。"
    )


def attach_turn_plan_to_semantic_frame(semantic_frame: Any, turn_plan: Any) -> None:
    try:
        semantic_frame.turn_plan = turn_plan
        semantic_frame.output_mode = turn_plan.output_mode
        semantic_frame.speech_act = turn_plan.speech_act
        semantic_frame.session_goal = turn_plan.session_goal
    except Exception:
        pass


def _mark_fallback_reason(value: Any, reason: str) -> None:
    try:
        value.fallback_reason = reason
    except Exception:
        pass


def _mark_llm_source(value: Any, source: str) -> None:
    try:
        value.llm_source = source
    except Exception:
        pass


def _is_metadata_fallback(value: Any) -> bool:
    return str(getattr(value, "reason", "") or "").strip().startswith("metadata_fallback")


def _effective_callers(primary: Any, secondary: Any = None) -> tuple[Any, Any | None]:
    if primary is None:
        return secondary, None
    if secondary is None or secondary is primary:
        return primary, None
    return primary, secondary


def _primary_attempt_timeout(total_timeout_s: float, has_secondary: bool) -> float:
    if not has_secondary:
        return total_timeout_s
    return max(0.25, min(total_timeout_s, total_timeout_s * 0.5))


def _warn_semantic_timeout(logger: Any, label: str, timeout_s: float) -> None:
    if logger is None:
        return
    try:
        logger.warning(f"[emotion] {label} exceeded {timeout_s:g}s; using metadata fallback.")
    except Exception:
        pass


async def infer_turn_semantic_frame_with_timeout(
    text: str,
    *,
    plugin_config: Any,
    is_group: bool = False,
    is_random_chat: bool = False,
    tool_caller: Any = None,
    recent_context: str = "",
    relationship_hint: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
    current_inner_state: str = "",
    current_emotion_state: str = "",
    fallback_tool_caller: Any = None,
    logger: Any = None,
    metric_scene: str = "group",
) -> tuple[Any, float, str, float, str]:
    timeout_s = semantic_frame_timeout_seconds(plugin_config)
    started_at = time.monotonic()
    primary_caller, secondary_caller = _effective_callers(tool_caller, fallback_tool_caller)
    if primary_caller is None:
        semantic_frame = metadata_fallback_turn_semantic_frame_for_session(
            is_group=is_group,
            is_random_chat=is_random_chat,
        )
        _mark_fallback_reason(semantic_frame, "semantic_frame_no_caller")
        return semantic_frame, (time.monotonic() - started_at) * 1000.0, "semantic_frame_no_caller", timeout_s, "metadata"

    async def _call(caller: Any, timeout: float) -> tuple[Any | None, bool]:
        try:
            semantic_frame = await asyncio.wait_for(
                infer_turn_semantic_frame_with_llm(
                    text,
                    is_group=is_group,
                    is_random_chat=is_random_chat,
                    tool_caller=caller,
                    recent_context=recent_context,
                    relationship_hint=relationship_hint,
                    repeat_clusters=repeat_clusters,
                    current_inner_state=current_inner_state,
                    current_emotion_state=current_emotion_state,
                ),
                timeout=max(0.1, timeout),
            )
            return semantic_frame, False
        except asyncio.TimeoutError:
            return None, True

    primary_timeout_s = _primary_attempt_timeout(timeout_s, secondary_caller is not None)
    primary_frame, primary_timed_out = await _call(primary_caller, primary_timeout_s)
    if primary_frame is not None and not _is_metadata_fallback(primary_frame):
        _mark_llm_source(primary_frame, "primary")
        return primary_frame, (time.monotonic() - started_at) * 1000.0, "", timeout_s, "primary"

    secondary_timed_out = False
    secondary_frame = None
    if secondary_caller is not None:
        elapsed_s = time.monotonic() - started_at
        remaining_s = max(0.0, timeout_s - elapsed_s)
        if remaining_s > 0.05:
            secondary_frame, secondary_timed_out = await _call(secondary_caller, remaining_s)
            if secondary_frame is not None and not _is_metadata_fallback(secondary_frame):
                _mark_llm_source(secondary_frame, "secondary")
                record_counter("reply.semantic_frame_secondary_llm_total", scene=metric_scene)
                return secondary_frame, (time.monotonic() - started_at) * 1000.0, "", timeout_s, "secondary"

    elapsed_ms = (time.monotonic() - started_at) * 1000.0
    fallback_reason = "semantic_frame_timeout" if primary_timed_out or secondary_timed_out else "semantic_frame_invalid"
    semantic_frame = metadata_fallback_turn_semantic_frame_for_session(
        is_group=is_group,
        is_random_chat=is_random_chat,
    )
    _mark_fallback_reason(semantic_frame, fallback_reason)
    record_counter("reply.semantic_frame_fallback_total", scene=metric_scene, reason=fallback_reason)
    if fallback_reason == "semantic_frame_timeout":
        _warn_semantic_timeout(logger, "semantic frame LLM", timeout_s)
    return semantic_frame, elapsed_ms, fallback_reason, timeout_s, "metadata"


async def plan_turn_with_timeout(
    text: str,
    *,
    plugin_config: Any,
    is_group: bool = False,
    is_random_chat: bool = False,
    is_direct_mention: bool = False,
    has_images: bool = False,
    message_target: str = "",
    qzone_event_type: str = "",
    tool_caller: Any = None,
    recent_context: str = "",
    relationship_hint: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
    current_inner_state: str = "",
    current_emotion_state: str = "",
    available_tools: list[dict[str, Any]] | None = None,
    group_knowledge_hint: str = "",
    fallback_tool_caller: Any = None,
    logger: Any = None,
    metric_mode: str = "enabled",
) -> tuple[Any, float, str, float, str]:
    timeout_s = semantic_frame_timeout_seconds(plugin_config)
    started_at = time.monotonic()
    planner_message_target = normalize_message_target_for_plan(message_target)
    primary_caller, secondary_caller = _effective_callers(tool_caller, fallback_tool_caller)
    if primary_caller is None:
        turn_plan = metadata_fallback_turn_plan(
            is_group=is_group,
            is_random_chat=is_random_chat,
            is_direct_mention=is_direct_mention,
            has_images=has_images,
            message_target=planner_message_target,
            qzone_event_type=qzone_event_type,
        )
        _mark_fallback_reason(turn_plan, "turn_plan_no_caller")
        return turn_plan, (time.monotonic() - started_at) * 1000.0, "turn_plan_no_caller", timeout_s, "metadata"

    async def _call(caller: Any, timeout: float) -> tuple[Any | None, bool]:
        try:
            turn_plan = await asyncio.wait_for(
                plan_turn_with_llm(
                    text,
                    is_group=is_group,
                    is_random_chat=is_random_chat,
                    is_direct_mention=is_direct_mention,
                    has_images=has_images,
                    message_target=planner_message_target,
                    qzone_event_type=qzone_event_type,
                    tool_caller=caller,
                    recent_context=recent_context,
                    relationship_hint=relationship_hint,
                    repeat_clusters=repeat_clusters,
                    current_inner_state=current_inner_state,
                    current_emotion_state=current_emotion_state,
                    available_tools=available_tools,
                    group_knowledge_hint=group_knowledge_hint,
                ),
                timeout=max(0.1, timeout),
            )
            return turn_plan, False
        except asyncio.TimeoutError:
            return None, True

    primary_timeout_s = _primary_attempt_timeout(timeout_s, secondary_caller is not None)
    primary_plan, primary_timed_out = await _call(primary_caller, primary_timeout_s)
    if primary_plan is not None and not _is_metadata_fallback(primary_plan):
        _mark_llm_source(primary_plan, "primary")
        return primary_plan, (time.monotonic() - started_at) * 1000.0, "", timeout_s, "primary"

    secondary_timed_out = False
    secondary_plan = None
    if secondary_caller is not None:
        elapsed_s = time.monotonic() - started_at
        remaining_s = max(0.0, timeout_s - elapsed_s)
        if remaining_s > 0.05:
            secondary_plan, secondary_timed_out = await _call(secondary_caller, remaining_s)
            if secondary_plan is not None and not _is_metadata_fallback(secondary_plan):
                _mark_llm_source(secondary_plan, "secondary")
                record_counter("turn_planner.secondary_llm_total", mode=metric_mode)
                return secondary_plan, (time.monotonic() - started_at) * 1000.0, "", timeout_s, "secondary"

    elapsed_ms = (time.monotonic() - started_at) * 1000.0
    fallback_reason = "turn_plan_timeout" if primary_timed_out or secondary_timed_out else "turn_plan_invalid"
    turn_plan = metadata_fallback_turn_plan(
        is_group=is_group,
        is_random_chat=is_random_chat,
        is_direct_mention=is_direct_mention,
        has_images=has_images,
        message_target=planner_message_target,
        qzone_event_type=qzone_event_type,
    )
    _mark_fallback_reason(turn_plan, fallback_reason)
    record_counter("turn_planner.fallback_total", mode=metric_mode, reason=fallback_reason)
    if fallback_reason == "turn_plan_timeout":
        _warn_semantic_timeout(logger, "TurnPlan LLM", timeout_s)
    return turn_plan, elapsed_ms, fallback_reason, timeout_s, "metadata"


@dataclass
class PreparedReplySemantics:
    data_dir: Any
    recent_bot_replies: list[str]
    inner_state: dict[str, Any]
    emotion_state: dict[str, Any]
    semantic_frame: Any
    intent_decision: Any
    message_intent: str
    arbitration: str
    emotion_block: str


def compose_reply_emotion_block(
    *,
    semantic_frame: Any,
    inner_state: dict[str, Any],
    emotion_state: dict[str, Any],
    user_id: str,
    group_id: str = "",
    is_private: bool = False,
) -> str:
    block = build_turn_emotion_prompt_block(
        semantic_frame=semantic_frame,
        inner_state=inner_state,
        emotion_state=emotion_state,
        user_id=user_id,
        group_id=group_id,
        is_private=is_private,
    )
    if bool(getattr(semantic_frame, "requires_emotional_care", False)):
        support_hint = str(_EMOTIONAL_SUPPORT_HINT or "").strip()
        if support_hint:
            block = f"{block}\n{support_hint}".strip() if block else support_hint
    return block


async def prepare_reply_semantics(
    *,
    runtime: Any,
    recent_window: list[dict[str, Any]],
    group_id: str,
    user_id: str,
    is_private_session: bool,
    is_random_chat: bool,
    is_direct_mention: bool,
    raw_message_text: str,
    current_agent_message_content: str,
    recent_context_hint: str,
    relationship_hint: str,
    repeat_clusters: list[dict[str, Any]] | None,
    message_target: str,
    solo_speaker_follow: bool,
    has_images: bool = False,
) -> PreparedReplySemantics:
    recent_bot_replies = extract_recent_bot_reply_texts(recent_window if not is_private_session else [])
    data_dir = get_personification_data_dir(runtime.plugin_config)
    inner_state = dict(DEFAULT_INNER_STATE)
    emotion_state = {}
    _is_result, _es_result = await asyncio.gather(
        load_inner_state(data_dir),
        load_emotion_state(data_dir),
        return_exceptions=True,
    )
    if isinstance(_is_result, BaseException):
        runtime.logger.debug(f"[emotion] load inner_state failed: {_is_result}")
    else:
        inner_state.update(_is_result)
    if isinstance(_es_result, BaseException):
        runtime.logger.debug(f"[emotion] load emotion_state failed: {_es_result}")
    else:
        emotion_state = _es_result
    emotion_memory_hint = render_emotion_memory_hint(
        emotion_state,
        user_id=user_id,
        group_id="" if is_private_session else str(group_id),
    )
    planner_enabled = bool(getattr(runtime.plugin_config, "personification_turn_planner_enabled", False))
    planner_shadow_enabled = bool(
        getattr(runtime.plugin_config, "personification_turn_planner_shadow_enabled", False)
    )
    planner_available_tools: list[dict[str, Any]] = []
    if planner_enabled or planner_shadow_enabled:
        try:
            planner_available_tools = registry_planner_metadata(runtime.tool_registry)
        except Exception:
            planner_available_tools = []

    group_knowledge_hint = ""
    if not is_private_session and bool(getattr(runtime.plugin_config, "personification_group_knowledge_enabled", False)):
        try:
            from ...core.group_knowledge import query_group_knowledge, format_group_knowledge_hint
            entries = query_group_knowledge(
                getattr(runtime, "memory_store", None),
                str(group_id),
                raw_message_text or current_agent_message_content,
                top_k=8,
            )
            group_knowledge_hint = format_group_knowledge_hint(entries)
        except Exception:
            group_knowledge_hint = ""

    turn_plan = None
    if planner_enabled:
        turn_plan, plan_elapsed_ms, plan_fallback_reason, plan_timeout_s, plan_source = await plan_turn_with_timeout(
            raw_message_text or current_agent_message_content,
            plugin_config=runtime.plugin_config,
            is_group=not is_private_session,
            is_random_chat=is_random_chat,
            is_direct_mention=is_direct_mention,
            has_images=has_images,
            message_target=message_target,
            tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
            fallback_tool_caller=runtime.agent_tool_caller,
            recent_context=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            current_inner_state=render_inner_state_hint(inner_state),
            current_emotion_state=emotion_memory_hint,
            available_tools=planner_available_tools,
            group_knowledge_hint=group_knowledge_hint,
            logger=runtime.logger,
            metric_mode="enabled",
        )
        record_counter(
            "turn_planner.plan_total",
            mode="enabled",
            action=turn_plan.reply_action,
            output_mode=turn_plan.output_mode,
        )
        record_timing("turn_planner.plan_ms", plan_elapsed_ms, mode="enabled")
        semantic_frame = turn_plan_to_semantic_frame(turn_plan)
        if plan_fallback_reason:
            is_timeout = plan_fallback_reason.endswith("_timeout")
            _record_reply_trace_stage(
                key="turn_plan_timeout" if is_timeout else "turn_plan_fallback",
                label="回合规划超时" if is_timeout else "回合规划降级",
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
            _record_reply_trace_stage(
                key="turn_plan_llm",
                label="回合规划 LLM",
                status="ok",
                detail=(
                    f"action={getattr(turn_plan, 'reply_action', '')} "
                    f"speech_act={getattr(turn_plan, 'speech_act', '')} "
                    f"output={getattr(turn_plan, 'output_mode', '')} "
                    f"source={plan_source} "
                    f"elapsed_ms={int(plan_elapsed_ms)}"
                ),
            )
    else:
        semantic_frame, semantic_elapsed_ms, semantic_fallback_reason, semantic_timeout_s, semantic_source = (
            await infer_turn_semantic_frame_with_timeout(
                raw_message_text or current_agent_message_content,
                plugin_config=runtime.plugin_config,
                is_group=not is_private_session,
                is_random_chat=is_random_chat,
                tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
                fallback_tool_caller=runtime.agent_tool_caller,
                recent_context=recent_context_hint,
                relationship_hint=relationship_hint,
                repeat_clusters=repeat_clusters,
                current_inner_state=render_inner_state_hint(inner_state),
                current_emotion_state=emotion_memory_hint,
                logger=runtime.logger,
                metric_scene="private" if is_private_session else "group",
            )
        )
        record_timing(
            "reply.semantic_frame_ms",
            semantic_elapsed_ms,
            scene="private" if is_private_session else "group",
        )
        turn_plan = turn_plan_from_semantic_frame(
            semantic_frame,
            has_images=has_images,
            message_target=normalize_message_target_for_plan(message_target),
        )
        attach_turn_plan_to_semantic_frame(semantic_frame, turn_plan)
        if semantic_fallback_reason:
            is_timeout = semantic_fallback_reason.endswith("_timeout")
            _record_reply_trace_stage(
                key="semantic_frame_timeout" if is_timeout else "semantic_frame_fallback",
                label="语义帧超时" if is_timeout else "语义帧降级",
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
        else:
            _record_reply_trace_stage(
                key="semantic_frame_llm",
                label="语义帧 LLM",
                status="ok",
                detail=(
                    f"intent={getattr(semantic_frame, 'chat_intent', '')} "
                    f"speech_act={getattr(turn_plan, 'speech_act', '')} "
                    f"ambiguity={getattr(semantic_frame, 'ambiguity_level', '')} "
                    f"emotion={getattr(semantic_frame, 'bot_emotion', '')} "
                    f"source={semantic_source} "
                    f"elapsed_ms={int(semantic_elapsed_ms)}"
                ),
                hint="若此阶段经常较慢，配置 lite_model 并保持 strict_main_model 关闭",
            )
        if planner_shadow_enabled:
            shadow_plan, shadow_elapsed_ms, shadow_fallback_reason, shadow_timeout_s, shadow_source = await plan_turn_with_timeout(
                raw_message_text or current_agent_message_content,
                plugin_config=runtime.plugin_config,
                is_group=not is_private_session,
                is_random_chat=is_random_chat,
                is_direct_mention=is_direct_mention,
                has_images=has_images,
                message_target=message_target,
                tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
                fallback_tool_caller=runtime.agent_tool_caller,
                recent_context=recent_context_hint,
                relationship_hint=relationship_hint,
                repeat_clusters=repeat_clusters,
                current_inner_state=render_inner_state_hint(inner_state),
                current_emotion_state=emotion_memory_hint,
                available_tools=planner_available_tools,
                group_knowledge_hint=group_knowledge_hint,
                logger=runtime.logger,
                metric_mode="shadow",
            )
            record_timing("turn_planner.plan_ms", shadow_elapsed_ms, mode="shadow")
            if shadow_fallback_reason:
                is_timeout = shadow_fallback_reason.endswith("_timeout")
                _record_reply_trace_stage(
                    key="turn_plan_shadow_timeout" if is_timeout else "turn_plan_shadow_fallback",
                    label="TurnPlan 影子超时" if is_timeout else "TurnPlan 影子降级",
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
                    mode="shadow",
                    action=shadow_plan.reply_action,
                    output_mode=shadow_plan.output_mode,
                )
                if shadow_plan.reply_action != turn_plan.reply_action:
                    record_counter("turn_planner.diff_total", field="reply_action")
                if getattr(shadow_plan, "speech_act", "") != getattr(turn_plan, "speech_act", ""):
                    record_counter("turn_planner.diff_total", field="speech_act")
                if shadow_plan.output_mode != turn_plan.output_mode:
                    record_counter("turn_planner.diff_total", field="output_mode")
    intent_decision = semantic_frame.to_intent_decision()
    message_intent = intent_decision.chat_intent
    arbitration = arbitrate_reply_mode(
        intent_decision=intent_decision,
        is_private=is_private_session,
        is_direct_mention=is_direct_mention,
        is_random_chat=is_random_chat,
        message_target=normalize_message_target_for_review(message_target),
        solo_speaker_follow=solo_speaker_follow,
    )
    emotion_block = compose_reply_emotion_block(
        semantic_frame=semantic_frame,
        inner_state=inner_state,
        emotion_state=emotion_state,
        user_id=user_id,
        group_id="" if is_private_session else str(group_id),
        is_private=is_private_session,
    )
    return PreparedReplySemantics(
        data_dir=data_dir,
        recent_bot_replies=recent_bot_replies,
        inner_state=inner_state,
        emotion_state=emotion_state,
        semantic_frame=semantic_frame,
        intent_decision=intent_decision,
        message_intent=message_intent,
        arbitration=arbitration,
        emotion_block=emotion_block,
    )


def should_speak_in_random_chat(
    *,
    state: dict[str, Any],
    message_target: str,
    solo_speaker_follow: bool,
) -> bool:
    if batch_has_newer_messages(state):
        return False
    if solo_speaker_follow:
        return True
    if normalize_message_target_for_review(message_target) == "bot":
        return True
    return True


async def persist_reply_emotion_state(
    *,
    runtime: Any,
    data_dir: Any,
    user_id: str,
    group_id: str,
    semantic_frame: Any,
    assistant_text: str,
    is_private: bool,
) -> None:
    try:
        await update_emotion_state_after_turn(
            data_dir,
            user_id=user_id,
            group_id="" if is_private else str(group_id),
            semantic_frame=semantic_frame,
            assistant_text=assistant_text,
            is_private=is_private,
        )
    except Exception as e:
        runtime.logger.debug(f"[emotion] update after reply failed: {e}")


def schedule_inner_state_update_after_reply(
    *,
    runtime: Any = None,
    inner_state_updater: Any = None,
    logger: Any = None,
    user_text: str,
    assistant_text: str,
    user_id: str,
    group_id: str = "",
    is_private: bool = False,
    semantic_frame: Any = None,
    task_exc_logger: Any = None,
) -> None:
    updater = inner_state_updater or getattr(runtime, "inner_state_updater", None)
    if updater is None:
        return
    runtime_logger = logger or getattr(runtime, "logger", None)
    visible_reply = str(assistant_text or "").strip()
    if not visible_reply:
        return
    frame_parts = []
    if semantic_frame is not None:
        for name in ("chat_intent", "user_attitude", "bot_emotion", "emotion_intensity", "expression_style", "session_goal"):
            value = str(getattr(semantic_frame, name, "") or "").strip()
            if value:
                frame_parts.append(f"{name}={value}")
        turn_plan = getattr(semantic_frame, "turn_plan", None)
        if turn_plan is not None:
            action = str(getattr(turn_plan, "reply_action", "") or "").strip()
            output = str(getattr(turn_plan, "output_mode", "") or "").strip()
            if action or output:
                frame_parts.append(f"turn_plan={action or '-'}:{output or '-'}")
    recent_summary = (
        f"场景：{'私聊' if is_private else '群聊'}"
        + (f" group={group_id}" if group_id and not is_private else "")
        + f" user={user_id}\n"
        f"用户：{str(user_text or '').strip()[:300]}\n"
        f"你：{visible_reply[:300]}\n"
        + (f"语义帧：{'; '.join(frame_parts)}" if frame_parts else "")
    ).strip()
    try:
        task = asyncio.create_task(updater(recent_summary, str(user_id or "")))
        if task_exc_logger is not None and runtime_logger is not None:
            task.add_done_callback(task_exc_logger("inner_state_updater", runtime_logger))
    except Exception as exc:
        try:
            if runtime_logger is not None:
                runtime_logger.debug(f"[emotion] schedule inner_state update failed: {exc}")
        except Exception:
            pass


__all__ = [
    "PreparedReplySemantics",
    "attach_turn_plan_to_semantic_frame",
    "compose_reply_emotion_block",
    "infer_turn_semantic_frame_with_timeout",
    "plan_turn_with_timeout",
    "persist_reply_emotion_state",
    "prepare_reply_semantics",
    "schedule_inner_state_update_after_reply",
    "semantic_frame_timeout_hint",
    "semantic_frame_timeout_seconds",
    "should_speak_in_random_chat",
]
