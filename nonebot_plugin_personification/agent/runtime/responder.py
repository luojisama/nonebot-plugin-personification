from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .planner import OUTPUT_MODE_LENGTHS, extract_json_payload


ALLOWED_INFO_ADDED = {"tone_only", "new_fact", "continue_topic", "redirect", "refuse"}


@dataclass(frozen=True)
class PersonaResponse:
    reply_text: str
    info_added: str = "continue_topic"
    echoed_user_phrase: bool = False
    user_attitude: str = ""
    bot_emotion: str = ""
    expression_style: str = ""
    tts_style_hint: str = ""
    sticker_mood_hint: str = ""
    sticker_appropriate: bool = True


def parse_persona_response(raw: str) -> PersonaResponse | None:
    payload = extract_json_payload(str(raw or ""))
    if not isinstance(payload, dict):
        return None
    reply_text = str(payload.get("reply_text", "") or payload.get("text", "") or "").strip()
    if not reply_text:
        return None
    info_added = str(payload.get("info_added", "") or "continue_topic").strip()
    if info_added not in ALLOWED_INFO_ADDED:
        info_added = "continue_topic"
    return PersonaResponse(
        reply_text=reply_text,
        info_added=info_added,
        echoed_user_phrase=_coerce_bool(payload.get("echoed_user_phrase"), False),
        user_attitude=str(payload.get("user_attitude", "") or "").strip()[:80],
        bot_emotion=str(payload.get("bot_emotion", "") or "").strip()[:80],
        expression_style=str(payload.get("expression_style", "") or "").strip()[:80],
        tts_style_hint=str(payload.get("tts_style_hint", "") or "").strip()[:80],
        sticker_mood_hint=str(payload.get("sticker_mood_hint", "") or "").strip()[:80],
        sticker_appropriate=_coerce_bool(payload.get("sticker_appropriate"), True),
    )


def apply_persona_response_to_semantic_frame(response: PersonaResponse, semantic_frame: Any) -> None:
    if semantic_frame is None:
        return
    updates = {
        "persona_response_info_added": response.info_added,
        "persona_response_echoed_user_phrase": response.echoed_user_phrase,
        "user_attitude": response.user_attitude,
        "bot_emotion": response.bot_emotion,
        "expression_style": response.expression_style,
        "tts_style_hint": response.tts_style_hint,
        "sticker_mood_hint": response.sticker_mood_hint,
        "sticker_appropriate": response.sticker_appropriate,
    }
    for name, value in updates.items():
        if value == "" and name not in {"persona_response_info_added", "persona_response_echoed_user_phrase"}:
            continue
        try:
            setattr(semantic_frame, name, value)
        except Exception:
            continue


def with_persona_responder_instruction(
    messages: list[dict[str, Any]],
    *,
    semantic_frame: Any = None,
    is_direct_mention: bool = False,
    relationship_hint: str = "",
    recent_bot_replies: list[str] | None = None,
    emotional_climate: str = "",
    message_text: str = "",
    lorebook_enabled: bool = False,
    memory_store: Any = None,
) -> list[dict[str, Any]]:
    lorebook_section = ""
    if lorebook_enabled and message_text and memory_store:
        try:
            from ...core.persona_knowledge import match_lorebook_triggers, format_lorebook_injection
            entries = memory_store.list_recent_memories(memory_type="persona_knowledge", limit=200)
            matched = match_lorebook_triggers(message_text, entries)
            lorebook_section = format_lorebook_injection(matched)
        except Exception:
            lorebook_section = ""
    instruction = _build_persona_responder_instruction(
        semantic_frame=semantic_frame,
        is_direct_mention=is_direct_mention,
        relationship_hint=relationship_hint,
        recent_bot_replies=recent_bot_replies,
        emotional_climate=emotional_climate,
        lorebook_section=lorebook_section,
    )
    copied = [dict(item) for item in list(messages or [])]
    if copied and copied[0].get("role") == "system":
        copied[0]["content"] = f"{copied[0].get('content', '')}\n\n{instruction}"
    else:
        copied.insert(0, {"role": "system", "content": instruction})
    return copied


def _peer_plugins_section() -> str:
    """枚举其他 NoneBot 插件清单，用于让 bot 不要把别人的功能说成自己。"""
    try:
        from ...core.peer_awareness import render_other_plugins_hint

        return render_other_plugins_hint(max_plugins=10)
    except Exception:
        return ""


def _build_persona_responder_instruction(
    *,
    semantic_frame: Any = None,
    is_direct_mention: bool = False,
    relationship_hint: str = "",
    recent_bot_replies: list[str] | None = None,
    emotional_climate: str = "",
    lorebook_section: str = "",
) -> str:
    output_mode = str(getattr(semantic_frame, "output_mode", "") or "").strip() or "chat_short"
    min_chars, max_chars = OUTPUT_MODE_LENGTHS.get(output_mode, OUTPUT_MODE_LENGTHS["chat_short"])
    no_reply_rule = "直呼/提及时禁止输出 [NO_REPLY]。" if is_direct_mention else "只有明显不该回复时才可把 reply_text 设为 [NO_REPLY]。"
    session_goal = str(getattr(semantic_frame, "session_goal", "") or "").strip()[:100]
    user_attitude = str(getattr(semantic_frame, "user_attitude", "") or "").strip()[:100]
    bot_emotion = str(getattr(semantic_frame, "bot_emotion", "") or "").strip()[:100]
    expression_style = str(getattr(semantic_frame, "expression_style", "") or "").strip()[:100]
    recent_replies = [
        str(item or "").strip()[:80]
        for item in list(recent_bot_replies or [])[:3]
        if str(item or "").strip()
    ]
    direction = {
        "session_goal": session_goal or "自然回应当前轮次",
        "relationship": str(relationship_hint or "").strip()[:240] or "无特别关系摘要",
        "emotional_climate": str(emotional_climate or "").strip()[:80] or "未单独判断",
        "current_attitude": user_attitude or "按当前语境自然判断",
        "current_emotion": bot_emotion or "平静",
        "expression_style": expression_style or "自然口语",
        "recent_bot_replies": recent_replies,
    }
    instruction = (
        "## PersonaResponder JSON 输出要求\n"
        "作者旁白/角色方向："
        + json.dumps(direction, ensure_ascii=False, separators=(",", ":"))
        + "\n"
        "本轮不要直接输出普通文本，必须只输出严格 JSON，不要 markdown。\n"
        "结构："
        + json.dumps(
            {
                "reply_text": "最终要发出的回复",
                "info_added": "tone_only|new_fact|continue_topic|redirect|refuse",
                "echoed_user_phrase": False,
                "user_attitude": "你对用户的即时态度",
                "bot_emotion": "你的即时情绪",
                "expression_style": "回复表达风格",
                "tts_style_hint": "TTS风格提示",
                "sticker_mood_hint": "表情包情绪提示",
                "sticker_appropriate": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        f"reply_text 按 output_mode={output_mode} 控制在 {min_chars}-{max_chars} 字附近。"
        "如果只是在复述用户语义，把 info_added 标为 tone_only；如果复用了用户原话连续片段，把 echoed_user_phrase 标为 true。"
        f"{no_reply_rule}\n"
        "## 不确定性硬约束（拟人优先于装懂）\n"
        "- 涉及具体事实、数字、时间、人名、新闻、产品参数等的问题，如果你没有调用工具（如 web_search）且对答案不完全确定，"
        "  reply_text 应当用口语自然地承认不知道（例如：『这个我不太清楚诶』『不太确定，我去查查再告诉你』），"
        "  把 info_added 设为 'refuse'。**禁止凭印象编造具体数字、链接、日期、官方说法。**\n"
        "- 如果工具结果明显为空或与问题无关，也要承认信息不足，而不是绕开。\n"
        "- 但闲聊、共情、表达情绪、复述用户观点这些**不需要外部事实**的话题，依然要正常回答，不要滥用『不知道』。"
    )
    if lorebook_section:
        instruction = f"{lorebook_section}\n\n{instruction}"
    peer_section = _peer_plugins_section()
    if peer_section:
        instruction = f"{instruction}\n\n{peer_section}"
    return instruction


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


__all__ = [
    "PersonaResponse",
    "apply_persona_response_to_semantic_frame",
    "parse_persona_response",
    "with_persona_responder_instruction",
]
