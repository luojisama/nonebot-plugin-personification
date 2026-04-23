from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

@dataclass(frozen=True)
class ResponseReviewDecision:
    action: str
    text: str
    reason: str = ""


@dataclass(frozen=True)
class ReplyArbitrationIntent:
    ambiguity_level: str = ""
    recommend_silence: bool = False


def _to_text_list(values: Iterable[Any], *, limit: int = 4) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in items:
            continue
        items.append(text[:160])
        if len(items) >= limit:
            break
    return items


def extract_recent_bot_reply_texts(messages: Iterable[dict[str, Any]], *, limit: int = 3) -> list[str]:
    collected: list[str] = []
    for message in list(messages or [])[-12:]:
        if not isinstance(message, dict):
            continue
        if not (bool(message.get("is_bot")) or str(message.get("source_kind", "") or "").strip() == "bot_reply"):
            continue
        text = str(message.get("content", "") or "").strip()
        if not text or text in collected:
            continue
        collected.append(text[:160])
    return collected[-limit:]


def _render_semantic_frame_hint(semantic_frame: Any) -> str:
    if semantic_frame is None:
        return ""
    return json.dumps(
        {
            "user_attitude": str(getattr(semantic_frame, "user_attitude", "") or ""),
            "bot_emotion": str(getattr(semantic_frame, "bot_emotion", "") or ""),
            "expression_style": str(getattr(semantic_frame, "expression_style", "") or ""),
            "emotion_intensity": str(getattr(semantic_frame, "emotion_intensity", "") or ""),
            "domain_focus": str(getattr(semantic_frame, "domain_focus", "") or ""),
        },
        ensure_ascii=False,
    )


def _strip_recovered_reply_text(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    message_match = re.search(r"<message.*?>(.*?)</\s*message\s*>", text, flags=re.DOTALL | re.IGNORECASE)
    if message_match:
        text = message_match.group(1).strip()
    text = (
        text.replace("[NO_REPLY]", "").replace("<NO_REPLY>", "")
        .replace("[SILENCE]", "").replace("<SILENCE>", "")
        .strip()
    )
    text = re.sub(r"</?(?:output|message|status|think|action)\b.*?>", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return ""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:120]


def _normalize_reply_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized[:120]


def _looks_like_recent_duplicate(candidate_text: str, recent_bot_replies: Iterable[Any]) -> bool:
    candidate_fp = _normalize_reply_fingerprint(candidate_text)
    if not candidate_fp:
        return False
    for item in recent_bot_replies or []:
        recent_fp = _normalize_reply_fingerprint(str(item or ""))
        if not recent_fp:
            continue
        if candidate_fp == recent_fp:
            return True
        shorter, longer = sorted((candidate_fp, recent_fp), key=len)
        if len(shorter) >= 5 and shorter in longer:
            return True
    return False


_SILENCE_CONFIDENCE_THRESHOLD = 0.72


def arbitrate_reply_mode(
    *,
    intent_decision: Any,
    is_private: bool,
    is_direct_mention: bool,
    is_random_chat: bool,
    message_target: str = "",
    solo_speaker_follow: bool = False,
) -> str:
    ambiguity = str(getattr(intent_decision, "ambiguity_level", "") or "").strip().lower()
    recommend_silence = bool(getattr(intent_decision, "recommend_silence", False))
    try:
        confidence = float(getattr(intent_decision, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    hard_silence = (
        not is_private
        and not is_direct_mention
        and not solo_speaker_follow
        and recommend_silence
        and ambiguity == "high"
        and message_target == "others"
        and confidence >= _SILENCE_CONFIDENCE_THRESHOLD
    )
    if hard_silence:
        return "no_reply"
    if ambiguity == "high" and (is_private or is_direct_mention or message_target == "bot"):
        return "clarify"
    return "reply"


def _parse_review_payload(raw: str) -> ResponseReviewDecision | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action", "") or "").strip().lower()
    if action not in {"accept", "rewrite", "no_reply", "speak"}:
        return None
    revised = str(payload.get("text", "") or "").strip()
    reason = str(payload.get("reason", "") or "").strip()
    return ResponseReviewDecision(action=action, text=revised, reason=reason)


async def review_response_text(
    call_ai_api: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    *,
    candidate_text: str,
    raw_message_text: str,
    recent_context: str = "",
    relationship_hint: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
    recent_bot_replies: list[str] | None = None,
    message_intent: str = "",
    is_private: bool = False,
    is_random_chat: bool = False,
    semantic_frame: Any = None,
) -> ResponseReviewDecision:
    candidate = str(candidate_text or "").strip()
    if not candidate:
        return ResponseReviewDecision(action="no_reply", text="", reason="empty_candidate")
    if recent_bot_replies and _looks_like_recent_duplicate(candidate, recent_bot_replies):
        return ResponseReviewDecision(action="no_reply", text="", reason="recent_duplicate")
    semantic_hint = _render_semantic_frame_hint(semantic_frame)
    review_messages = [
        {
            "role": "system",
            "content": (
                "你是回复审阅器。检查候选回复是否自然、贴题、像正常群友/私聊对象会说的话。"
                "如果合理，输出 JSON：{\"action\":\"accept\",\"text\":\"\",\"reason\":\"...\"}。"
                "如果内容偏解释腔、AI味重、误解对象、和当前话题不贴，输出 "
                "{\"action\":\"rewrite\",\"text\":\"改写后的最终回复\",\"reason\":\"...\"}。"
                "如果这轮更适合沉默，输出 {\"action\":\"no_reply\",\"text\":\"\",\"reason\":\"...\"}。"
                "普通短句 banter、顺着上一句接话、轻量吐槽，优先 accept 或 rewrite，不要轻易 no_reply。"
                "只输出 JSON，不要解释。"
                "\n## 必须 rewrite 的 AI 味回复模式（重点检查）\n1. 「回声评论」：把用户说的话原样重复后加“太真实了/太直球了/太 X 了吧/真的假的”等感叹——必须改写为不重复原话的短句接话。\n2. 「安抚式客服腒」：以“别这么说/已经很够用了/不要这样想/你很棒的”开头——改写为自然接话。\n3. 「旁白式观察」：类似“真去做了啊/真的行动了/居然真的 XX 了”的旁白——改写为参与式短句。\n4. 「梗分析腒」：用“像是把 X 玩成 Y 了/意思就是/可以理解成”解释梗结构——改写为直接接梗。\n改写原则：去掉对用户发言的复述和分析，直接给一句自然接话，15字以内为佳。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"场景：{'私聊' if is_private else ('随机插话' if is_random_chat else '正常回复')}\n"
                f"意图：{str(message_intent or '').strip() or 'unknown'}\n"
                f"当前原话：{str(raw_message_text or '').strip()[:240] or '[EMPTY]'}\n"
                f"最近上下文：{str(recent_context or '').strip()[:500] or '[EMPTY]'}\n"
                f"互动关系：{str(relationship_hint or '').strip()[:320] or '[EMPTY]'}\n"
                f"语义情绪帧：{semantic_hint or '[EMPTY]'}\n"
                f"复读线索：{json.dumps(list(repeat_clusters or [])[:3], ensure_ascii=False)}\n"
                f"最近 bot 发言：{json.dumps(_to_text_list(recent_bot_replies or []), ensure_ascii=False)}\n"
                f"候选回复：{candidate}\n"
                "注意：先对照上方「必须 rewrite 的 AI 味回复模式」逐项检查候选回复，命中任意一条即输出 rewrite。"
            ),
        },
    ]
    try:
        raw = await call_ai_api(review_messages)
    except Exception:
        return ResponseReviewDecision(action="accept", text=candidate, reason="review_failed")
    parsed = _parse_review_payload(str(raw or ""))
    if parsed is None:
        return ResponseReviewDecision(action="accept", text=candidate, reason="review_unparseable")
    if parsed.action == "rewrite" and parsed.text:
        return ResponseReviewDecision(action="rewrite", text=parsed.text, reason=parsed.reason)
    if parsed.action == "no_reply":
        return ResponseReviewDecision(action="no_reply", text="", reason=parsed.reason)
    return ResponseReviewDecision(action="accept", text=candidate, reason=parsed.reason)


async def recover_direct_mention_reply(
    call_ai_api: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    *,
    raw_message_text: str,
    recent_context: str = "",
    relationship_hint: str = "",
    recent_bot_replies: list[str] | None = None,
    semantic_frame: Any = None,
    is_direct_mention: bool = False,
) -> str:
    if not is_direct_mention:
        return ""
    semantic_hint = _render_semantic_frame_hint(semantic_frame)
    repair_messages = [
        {
            "role": "system",
            "content": (
                "这是一次直呼 bot 的真实对话。上一轮错误地掉进了沉默/空回复。"
                "现在只补一条最终回复。必须回复，不要输出 [SILENCE]、[NO_REPLY]、JSON、XML、解释或道歉。"
                "语气像正常群友/私聊对象顺手回一句，不超过24字。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前原话：{str(raw_message_text or '').strip()[:240] or '[EMPTY]'}\n"
                f"最近上下文：{str(recent_context or '').strip()[:500] or '[EMPTY]'}\n"
                f"互动关系：{str(relationship_hint or '').strip()[:320] or '[EMPTY]'}\n"
                f"语义情绪帧：{semantic_hint or '[EMPTY]'}\n"
                f"最近 bot 发言：{json.dumps(_to_text_list(recent_bot_replies or []), ensure_ascii=False)}\n"
                "直接输出最终回复。"
            ),
        },
    ]
    try:
        raw = await call_ai_api(repair_messages)
    except Exception:
        return ""
    return _strip_recovered_reply_text(str(raw or ""))


async def decide_random_chat_speak(
    call_ai_api: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    *,
    raw_message_text: str,
    recent_context: str = "",
    relationship_hint: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
    recent_bot_replies: list[str] | None = None,
    has_newer_batch: bool = False,
    message_intent: str = "",
    ambiguity_level: str = "",
    message_target: str = "",
    solo_speaker_follow: bool = False,
) -> bool:
    if has_newer_batch:
        return False
    if solo_speaker_follow:
        return True
    if str(message_target or "").strip() == "bot":
        return True
    review_messages = [
        {
            "role": "system",
            "content": (
                "你是群聊随机插话判定器。"
                "判断 bot 这轮是否应该顺势说一句。"
                "如果该沉默，输出 JSON：{\"action\":\"no_reply\",\"reason\":\"...\"}。"
                "如果适合顺势接话，输出 JSON：{\"action\":\"speak\",\"reason\":\"...\"}。"
                "只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前原话：{str(raw_message_text or '').strip()[:240] or '[EMPTY]'}\n"
                f"消息意图：{str(message_intent or '').strip() or 'unknown'}\n"
                f"歧义等级：{str(ambiguity_level or '').strip() or 'unknown'}\n"
                f"消息目标：{str(message_target or '').strip() or 'unknown'}\n"
                f"最近上下文：{str(recent_context or '').strip()[:500] or '[EMPTY]'}\n"
                f"互动关系：{str(relationship_hint or '').strip()[:320] or '[EMPTY]'}\n"
                f"复读线索：{json.dumps(list(repeat_clusters or [])[:3], ensure_ascii=False)}\n"
                f"最近 bot 发言：{json.dumps(_to_text_list(recent_bot_replies or []), ensure_ascii=False)}\n"
                "要求：短句接梗、顺手问候、复读跟一句通常允许 speak；"
                "只有这轮明显像群员彼此聊天、bot 插话会显得突兀、或者 bot 刚说过类似内容时才选 no_reply。"
            ),
        },
    ]
    try:
        raw = await call_ai_api(review_messages)
    except Exception:
        return True
    parsed = _parse_review_payload(str(raw or ""))
    if parsed is None:
        return True
    return parsed.action == "speak"


__all__ = [
    "ResponseReviewDecision",
    "arbitrate_reply_mode",
    "decide_random_chat_speak",
    "extract_recent_bot_reply_texts",
    "recover_direct_mention_reply",
    "review_response_text",
]
