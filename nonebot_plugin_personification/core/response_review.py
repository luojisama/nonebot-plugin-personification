from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from ..agent.runtime.planner import OUTPUT_MODE_LENGTHS
from .reply_text_policy import (
    looks_like_formulaic_reply_tic,
    looks_like_markdown_reply,
    looks_like_visible_reasoning_trace,
    normalize_visible_reply_text,
)


@dataclass(frozen=True)
class ResponseReviewDecision:
    action: str
    text: str
    reason: str = ""


@dataclass(frozen=True)
class ReplyArbitrationIntent:
    ambiguity_level: str = ""
    recommend_silence: bool = False


def make_passthrough_review_decision(
    candidate_text: str,
    *,
    reason: str = "passthrough",
) -> ResponseReviewDecision:
    return ResponseReviewDecision(
        action="accept",
        text=str(candidate_text or "").strip(),
        reason=reason,
    )


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
            "persona_info_added": str(getattr(semantic_frame, "persona_response_info_added", "") or ""),
            "persona_echoed_user_phrase": bool(getattr(semantic_frame, "persona_response_echoed_user_phrase", False)),
        },
        ensure_ascii=False,
    )


def _output_mode_hint(semantic_frame: Any) -> str:
    output_mode = str(getattr(semantic_frame, "output_mode", "") or "").strip() or "chat_short"
    min_chars, max_chars = OUTPUT_MODE_LENGTHS.get(output_mode, OUTPUT_MODE_LENGTHS["chat_short"])
    return f"output_mode={output_mode}, 建议长度={min_chars}-{max_chars}字"


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
_AGENT_REPLY_OOC_PATTERNS = re.compile(
    r"根据(搜索|查询|检索|找到的)结果|"
    r"(查|搜|搜索|检索|查询)了一下|"
    r"我需要确认一下|"
    r"以下是(相关|查到的|找到的)|"
    r"(参考链接|相关链接|来源)[：:]|"
    r"(http|https)://\S{15,}|"
    r"(?:我)?先(?:潜水|围观|看看情况|看下情况|蹲一下|路过|观望)|"
    r"(?:等(?:一会儿?|会儿?|下)?|晚点|回头)再(?:说|看|聊)|"
    r"这(图|张图|个表情|表情包)(也太|真的|好|太)|"
    r"哈哈(这个|这张|这图)|"
    r"(图|表情包)(发的|选的|真的|也太)",
    re.IGNORECASE,
)


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
    random_chat_structural_silence = (
        not is_private
        and is_random_chat
        and not is_direct_mention
        and not solo_speaker_follow
        and recommend_silence
        and ambiguity == "high"
        and message_target in {"", "others", "uncertain"}
    )
    if hard_silence or random_chat_structural_silence:
        return "no_reply"
    if ambiguity == "high" and (is_private or is_direct_mention or message_target == "bot"):
        return "clarify"
    return "reply"


def is_agent_reply_ooc(text: str) -> bool:
    return bool(
        _AGENT_REPLY_OOC_PATTERNS.search(str(text or ""))
        or looks_like_formulaic_reply_tic(text)
        or looks_like_visible_reasoning_trace(text)
        or looks_like_markdown_reply(text)
    )


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


def _parse_bool_payload(raw: str) -> bool | None:
    text = str(raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"true", "yes", "speak", "reply"}:
        return True
    if lowered in {"false", "no", "silent", "no_reply"}:
        return False
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
    if isinstance(payload, bool):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("speak", "reply", "should_reply"):
        if key in payload:
            return bool(payload.get(key))
    action = str(payload.get("action", "") or "").strip().lower()
    if action in {"speak", "reply", "accept"}:
        return True
    if action in {"silent", "no_reply", "skip"}:
        return False
    return None


async def decide_random_chat_speak(
    call_ai_api: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    *,
    raw_message_text: str,
    recent_context: str = "",
    relationship_hint: str = "",
    message_target: str = "",
    solo_speaker_follow: bool = False,
    semantic_frame: Any = None,
) -> bool:
    if solo_speaker_follow:
        return True
    if str(message_target or "").strip() == "bot":
        return True
    semantic_hint = _render_semantic_frame_hint(semantic_frame)
    messages = [
        {
            "role": "system",
            "content": (
                "你是群聊随机插话判定器。判断这轮 bot 是否应该开口。"
                "只输出 JSON：{\"speak\":true/false,\"reason\":\"...\"}。"
                "如果不是明确需要 bot 参与，优先保持沉默。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前原话：{str(raw_message_text or '').strip()[:240] or '[EMPTY]'}\n"
                f"最近上下文：{str(recent_context or '').strip()[:500] or '[EMPTY]'}\n"
                f"互动关系：{str(relationship_hint or '').strip()[:320] or '[EMPTY]'}\n"
                f"语义情绪帧：{semantic_hint or '[EMPTY]'}"
            ),
        },
    ]
    try:
        raw = await call_ai_api(messages)
    except Exception:
        return False
    parsed = _parse_bool_payload(str(raw or ""))
    return bool(parsed) if parsed is not None else False


def _extract_recovered_message(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    match = re.search(r"<message>(.*?)</message>", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return normalize_visible_reply_text(match.group(1))
    parsed = _parse_review_payload(text)
    if parsed is not None and parsed.text:
        return normalize_visible_reply_text(parsed.text)
    try:
        payload = json.loads(text)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "text", "reply"):
            value = str(payload.get(key, "") or "").strip()
            if value:
                return normalize_visible_reply_text(value)
    return normalize_visible_reply_text(text)


async def recover_direct_mention_reply(
    call_ai_api: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    *,
    raw_message_text: str,
    recent_context: str = "",
    relationship_hint: str = "",
    recent_bot_replies: list[str] | None = None,
    is_direct_mention: bool = False,
    semantic_frame: Any = None,
) -> str:
    if not is_direct_mention:
        return ""
    semantic_hint = _render_semantic_frame_hint(semantic_frame)
    messages = [
        {
            "role": "system",
            "content": (
                "当前用户是在直呼或提及 bot，候选链路可能没有给出可用回复。"
                "请补一条自然、短、符合人设的最终回复。"
                "只输出 <output><message>最终回复</message></output>。"
                "最终回复必须是纯文本，不要 markdown、标题、列表、链接，也不要解释过程。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前原话：{str(raw_message_text or '').strip()[:240] or '[EMPTY]'}\n"
                f"最近上下文：{str(recent_context or '').strip()[:500] or '[EMPTY]'}\n"
                f"互动关系：{str(relationship_hint or '').strip()[:320] or '[EMPTY]'}\n"
                f"语义情绪帧：{semantic_hint or '[EMPTY]'}\n"
                f"最近 bot 发言：{json.dumps(_to_text_list(recent_bot_replies or []), ensure_ascii=False)}"
            ),
        },
    ]
    try:
        raw = await call_ai_api(messages)
    except Exception:
        return ""
    return _extract_recovered_message(str(raw or ""))


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
    is_direct_mention: bool = False,
    semantic_frame: Any = None,
) -> ResponseReviewDecision:
    candidate = str(candidate_text or "").strip()
    if not candidate:
        return ResponseReviewDecision(action="no_reply", text="", reason="empty_candidate")
    if recent_bot_replies and _looks_like_recent_duplicate(candidate, recent_bot_replies):
        return ResponseReviewDecision(action="no_reply", text="", reason="recent_duplicate")
    semantic_hint = _render_semantic_frame_hint(semantic_frame)
    output_mode_hint = _output_mode_hint(semantic_frame)
    review_messages = [
        {
            "role": "system",
            "content": (
                "你是回复审阅器。检查候选回复是否自然、贴题、像正常群友/私聊对象会说的话。"
                "如果合理，输出 JSON：{\"action\":\"accept\",\"text\":\"\",\"reason\":\"...\"}。"
                "如果内容偏解释腔、AI味重、误解对象、和当前话题不贴，输出 "
                "{\"action\":\"rewrite\",\"text\":\"改写后的最终回复\",\"reason\":\"...\"}。"
                "如果这轮更适合沉默，输出 {\"action\":\"no_reply\",\"text\":\"\",\"reason\":\"...\"}。"
                f"{'当前是直呼/提及 bot 的消息，禁止输出 no_reply。' if is_direct_mention else ''}"
                f"{'当前是群聊，改写时不要用问句、反问句、澄清问句或征询式结尾；信息不足就给保守短反应或 no_reply。' if not is_private else ''}"
                "普通短句 banter、顺着上一句接话、轻量吐槽，优先 accept 或 rewrite，不要轻易 no_reply。"
                "只输出 JSON，不要解释。"
                "\n## 必须 rewrite 的 AI 味回复模式（重点检查）\n1. 「回声评论」：把用户说的话原样重复后加“太真实了/太直球了/太 X 了吧/真的假的”等感叹——必须改写为不重复原话的短句接话。\n2. 候选回复中超过 3 个连续字与用户原话重叠，且没有新增信息或立场——必须 rewrite。\n3. 候选只是在用感叹词复述用户语义，没有新事实、延续话题、转向或明确态度——必须 rewrite。\n4. 「安抚式客服腔」：以“别这么说/已经很够用了/不要这样想/你很棒的”开头——改写为自然接话。\n5. 「旁白式观察」：类似“真去做了啊/真的行动了/居然真的 XX 了”的旁白——改写为参与式短句。\n6. 「梗分析腔」：用“像是把 X 玩成 Y 了/意思就是/可以理解成”解释梗结构——改写为直接接梗。\n7. 「营业感叹腔」：用“(也)太……了吧/……爆了/绝了/谁懂啊/笑死/绷不住了/yyds”这类口号式感叹收尾或起势——改写成平铺直叙的接话，去掉感叹营业腔和网络流行语，不喊口号。\n8. 「固定起手口癖」：用“等下，/等一下，”开头，或反复用“这也/这也太/你这也/这听着也”评价用户、图片、表情、剧情——必须换一种自然说法，不要保留这个开头或句式。\n改写原则：去掉对用户发言的复述和分析，按 output_mode 的长度要求输出；改写后不得引入新的回声模式、营业感叹腔或固定起手口癖。"
                "\n9. 出现 markdown 格式、标题、项目符号列表、编号列表、代码块、链接列表时，必须改成纯文本短句。"
                "\n10. 出现 Step 1/Step 2、步骤 1/步骤 2 这类内部推理、审查清单或草稿过程时，必须 rewrite，只保留最终要对用户说的一句。"
                "\n11. 「自我行动宣告」：类似“我先潜水/围观/看看情况/先看看情况/等会再说/蹲一下/路过”的句子是在宣告 bot 自己的观察姿态，"
                "不是在参与当前话题；如果不是直呼 bot 的消息，优先 no_reply，必须回应时改成一句具体的参与式反应。"
                "\n12. 「附和感叹/转述聊天」：候选只是说“确实/太真实了/真的假的/有点东西”这类空泛反应，"
                "或只是把当前原话、最近上下文换一种说法复述，没有自己的态度、具体追问或话题推进——必须 rewrite。"
                f"{'改写时以讨论、闲聊为主基调：给一个具体看法、接住一个点或顺着话题推进半步，不要改成问题句。' if not is_private else '改写时以讨论、闲聊为主基调：给一个具体看法、接住一个点，或抛一个贴着当前话题的小问题。'}"
                "\n如果语义情绪帧里 persona_info_added=tone_only 且 persona_echoed_user_phrase=true，也必须 rewrite。"
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
                f"输出模式：{output_mode_hint}\n"
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
        if is_direct_mention:
            return ResponseReviewDecision(action="accept", text=candidate, reason=parsed.reason or "direct_mention_no_reply_blocked")
        return ResponseReviewDecision(action="no_reply", text="", reason=parsed.reason)
    return ResponseReviewDecision(action="accept", text=candidate, reason=parsed.reason)


async def rewrite_agent_reply_ooc(
    *,
    tool_caller: Any,
    original_text: str,
    persona_system: str = "",
    timeout: float = 8.0,
    output_mode: str = "chat_short",
    avoid_questions: bool = False,
) -> str:
    if tool_caller is None:
        return ""
    min_chars, max_chars = OUTPUT_MODE_LENGTHS.get(output_mode, OUTPUT_MODE_LENGTHS["chat_short"])
    messages: list[dict[str, Any]] = []
    persona_hint = str(persona_system or "").strip()
    if persona_hint:
        messages.append({"role": "system", "content": persona_hint[:1200]})
    messages.append(
        {
            "role": "system",
            "content": (
                "下面这句话听起来像 AI 助手而不像普通群友。"
                f"把它用你自己的口吻重说一次，{min_chars}-{max_chars} 字以内。"
                "去掉【搜索/查询/结果/链接/来源】类表述和 URL，也去掉“我先看看情况/等会再说/先围观/蹲一下”这类观望或延后宣告。"
                f"{'当前是群聊，不要改成问句、反问句或澄清问句；改成参与讨论、闲聊推进、保守短反应，或没有可说的新东西时输出 [SILENCE]。' if avoid_questions else '改成参与讨论、闲聊推进或一个具体追问；没有可说的新东西时输出 [SILENCE]。'}"
                "只输出纯文本，不要 markdown、标题、项目符号列表、编号列表，也不要解释改写过程。"
            ),
        }
    )
    messages.append({"role": "user", "content": str(original_text or "").strip()[:600]})
    try:
        response = await asyncio.wait_for(
            tool_caller.chat_with_tools(messages, [], False),
            timeout=timeout,
        )
    except Exception:
        return ""
    rewritten = normalize_visible_reply_text(getattr(response, "content", "") or "")
    if not rewritten or is_agent_reply_ooc(rewritten):
        return ""
    return rewritten


__all__ = [
    "ResponseReviewDecision",
    "arbitrate_reply_mode",
    "decide_random_chat_speak",
    "extract_recent_bot_reply_texts",
    "is_agent_reply_ooc",
    "make_passthrough_review_decision",
    "recover_direct_mention_reply",
    "rewrite_agent_reply_ooc",
    "review_response_text",
]
