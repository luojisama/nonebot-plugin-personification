from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ...agent.inner_state import DEFAULT_STATE as DEFAULT_INNER_STATE, get_personification_data_dir, load_inner_state
from ...agent.runtime.planner import (
    plan_turn_with_llm,
    turn_plan_from_semantic_frame,
    turn_plan_to_semantic_frame,
)
from ...agent.runtime.tool_catalog import registry_planner_metadata
from ...core.chat_intent import infer_turn_semantic_frame_with_llm
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
from .pipeline_context import batch_has_newer_messages

_EMOTIONAL_SUPPORT_HINT = load_prompt("emotional_support_hint")


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
        started_at = time.monotonic()
        turn_plan = await plan_turn_with_llm(
            raw_message_text or current_agent_message_content,
            is_group=not is_private_session,
            is_random_chat=is_random_chat,
            is_direct_mention=is_direct_mention,
            has_images=has_images,
            message_target=message_target,
            tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
            recent_context=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            current_inner_state=render_inner_state_hint(inner_state),
            current_emotion_state=emotion_memory_hint,
            available_tools=planner_available_tools,
            group_knowledge_hint=group_knowledge_hint,
        )
        record_counter(
            "turn_planner.plan_total",
            mode="enabled",
            action=turn_plan.reply_action,
            output_mode=turn_plan.output_mode,
        )
        plan_elapsed_ms = (time.monotonic() - started_at) * 1000.0
        record_timing("turn_planner.plan_ms", plan_elapsed_ms, mode="enabled")
        semantic_frame = turn_plan_to_semantic_frame(turn_plan)
        _record_reply_trace_stage(
            key="turn_plan_llm",
            label="回合规划 LLM",
            status="ok",
            detail=(
                f"action={getattr(turn_plan, 'reply_action', '')} "
                f"output={getattr(turn_plan, 'output_mode', '')} "
                f"elapsed_ms={int(plan_elapsed_ms)}"
            ),
        )
    else:
        semantic_started_at = time.monotonic()
        semantic_frame = await infer_turn_semantic_frame_with_llm(
            raw_message_text or current_agent_message_content,
            is_group=not is_private_session,
            is_random_chat=is_random_chat,
            tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
            recent_context=recent_context_hint,
            relationship_hint=relationship_hint,
            repeat_clusters=repeat_clusters,
            current_inner_state=render_inner_state_hint(inner_state),
            current_emotion_state=emotion_memory_hint,
        )
        semantic_elapsed_ms = (time.monotonic() - semantic_started_at) * 1000.0
        record_timing(
            "reply.semantic_frame_ms",
            semantic_elapsed_ms,
            scene="private" if is_private_session else "group",
        )
        turn_plan = turn_plan_from_semantic_frame(
            semantic_frame,
            has_images=has_images,
            message_target=message_target,
        )
        try:
            semantic_frame.turn_plan = turn_plan
            semantic_frame.output_mode = turn_plan.output_mode
            semantic_frame.session_goal = turn_plan.session_goal
        except Exception:
            pass
        _record_reply_trace_stage(
            key="semantic_frame_llm",
            label="语义帧 LLM",
            status="ok",
            detail=(
                f"intent={getattr(semantic_frame, 'chat_intent', '')} "
                f"ambiguity={getattr(semantic_frame, 'ambiguity_level', '')} "
                f"emotion={getattr(semantic_frame, 'bot_emotion', '')} "
                f"elapsed_ms={int(semantic_elapsed_ms)}"
            ),
            hint="若此阶段经常较慢，配置 lite_model 并保持 strict_main_model 关闭",
        )
        if planner_shadow_enabled:
            started_at = time.monotonic()
            shadow_plan = await plan_turn_with_llm(
                raw_message_text or current_agent_message_content,
                is_group=not is_private_session,
                is_random_chat=is_random_chat,
                is_direct_mention=is_direct_mention,
                has_images=has_images,
                message_target=message_target,
                tool_caller=runtime.lite_tool_caller or runtime.agent_tool_caller,
                recent_context=recent_context_hint,
                relationship_hint=relationship_hint,
                repeat_clusters=repeat_clusters,
                current_inner_state=render_inner_state_hint(inner_state),
                current_emotion_state=emotion_memory_hint,
                available_tools=planner_available_tools,
                group_knowledge_hint=group_knowledge_hint,
            )
            record_counter(
                "turn_planner.plan_total",
                mode="shadow",
                action=shadow_plan.reply_action,
                output_mode=shadow_plan.output_mode,
            )
            if shadow_plan.reply_action != turn_plan.reply_action:
                record_counter("turn_planner.diff_total", field="reply_action")
            if shadow_plan.output_mode != turn_plan.output_mode:
                record_counter("turn_planner.diff_total", field="output_mode")
            record_timing("turn_planner.plan_ms", (time.monotonic() - started_at) * 1000.0, mode="shadow")
    intent_decision = semantic_frame.to_intent_decision()
    message_intent = intent_decision.chat_intent
    arbitration = arbitrate_reply_mode(
        intent_decision=intent_decision,
        is_private=is_private_session,
        is_direct_mention=is_direct_mention,
        is_random_chat=is_random_chat,
        message_target=str(message_target or ""),
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
    if str(message_target or "").strip() == "bot":
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
    "compose_reply_emotion_block",
    "persist_reply_emotion_state",
    "prepare_reply_semantics",
    "schedule_inner_state_update_after_reply",
    "should_speak_in_random_chat",
]
