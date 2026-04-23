from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ...agent.inner_state import DEFAULT_STATE as DEFAULT_INNER_STATE, get_personification_data_dir, load_inner_state
from ...core.chat_intent import infer_turn_semantic_frame_with_llm
from ...core.emotion_state import (
    build_turn_emotion_prompt_block,
    load_emotion_state,
    render_emotion_memory_hint,
    render_inner_state_hint,
    update_emotion_state_after_turn,
)
from ...core.prompts import load_prompt
from ...core.response_review import (
    arbitrate_reply_mode,
    decide_random_chat_speak,
    extract_recent_bot_reply_texts,
)
from .pipeline_context import batch_has_newer_messages

_EMOTIONAL_SUPPORT_HINT = load_prompt("emotional_support_hint")


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


async def should_speak_in_random_chat(
    *,
    runtime: Any,
    state: dict[str, Any],
    raw_message_text: str,
    message_text: str,
    message_content: str,
    recent_context_hint: str,
    relationship_hint: str,
    repeat_clusters: list[dict[str, Any]] | None,
    recent_bot_replies: list[str],
    message_intent: str,
    ambiguity_level: str,
    message_target: str,
    solo_speaker_follow: bool,
) -> bool:
    return await decide_random_chat_speak(
        runtime.lite_call_ai_api or runtime.call_ai_api,
        raw_message_text=raw_message_text or message_text or message_content,
        recent_context=recent_context_hint,
        relationship_hint=relationship_hint,
        repeat_clusters=repeat_clusters,
        recent_bot_replies=recent_bot_replies,
        has_newer_batch=batch_has_newer_messages(state),
        message_intent=message_intent,
        ambiguity_level=ambiguity_level,
        message_target=str(message_target or ""),
        solo_speaker_follow=solo_speaker_follow,
    )


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


__all__ = [
    "PreparedReplySemantics",
    "compose_reply_emotion_block",
    "persist_reply_emotion_state",
    "prepare_reply_semantics",
    "should_speak_in_random_chat",
]
