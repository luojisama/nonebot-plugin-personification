from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal


ReplyAction = Literal["reply", "silence", "ask_clarify"]
MemoryNeed = Literal["none", "light", "deep"]
ResearchNeed = Literal["none", "low", "medium", "high"]
VisionNeed = Literal["none", "summary", "native"]
OutputMode = Literal["chat_short", "chat_answer", "structured_help", "source_summary", "qzone_reply"]
ToolIntent = Literal["lookup_web", "lookup_plugin", "vision", "image_gen", "memory", "none"]
AmbiguityLevel = Literal["low", "medium", "high"]
MessageTarget = Literal["bot", "someone_else", "broadcast", "uncertain"]


ALLOWED_REPLY_ACTIONS = {"reply", "silence", "ask_clarify"}
ALLOWED_MEMORY_NEEDS = {"none", "light", "deep"}
ALLOWED_RESEARCH_NEEDS = {"none", "low", "medium", "high"}
ALLOWED_VISION_NEEDS = {"none", "summary", "native"}
ALLOWED_OUTPUT_MODES = {"chat_short", "chat_answer", "structured_help", "source_summary", "qzone_reply"}
ALLOWED_TOOL_INTENTS = {"lookup_web", "lookup_plugin", "vision", "image_gen", "memory", "none"}
ALLOWED_AMBIGUITY_LEVELS = {"low", "medium", "high"}
ALLOWED_MESSAGE_TARGETS = {"bot", "someone_else", "broadcast", "uncertain"}


OUTPUT_MODE_LENGTHS: dict[str, tuple[int, int]] = {
    "chat_short": (8, 40),
    "chat_answer": (30, 120),
    "structured_help": (80, 300),
    "source_summary": (80, 240),
    "qzone_reply": (10, 80),
}


@dataclass
class TurnPlan:
    reply_action: ReplyAction = "reply"
    memory_need: MemoryNeed = "none"
    research_need: ResearchNeed = "none"
    vision_need: VisionNeed = "none"
    qzone_continue: bool = False
    output_mode: OutputMode = "chat_short"
    tool_intent: list[ToolIntent] = field(default_factory=lambda: ["none"])
    ambiguity_level: AmbiguityLevel = "low"
    confidence: float = 0.0
    reason: str = ""
    message_target: MessageTarget = "uncertain"
    session_goal: str = ""

    @property
    def length_bounds(self) -> tuple[int, int]:
        return OUTPUT_MODE_LENGTHS.get(self.output_mode, OUTPUT_MODE_LENGTHS["chat_short"])


def normalize_plan_text(text: str) -> str:
    normalized = str(text or "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def _coerce_bool(value: Any, default: bool = False) -> bool:
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


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _enum_value(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _coerce_tool_intents(value: Any) -> list[ToolIntent]:
    raw_items: list[Any]
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    items: list[ToolIntent] = []
    for raw in raw_items:
        text = str(raw or "").strip()
        if text in ALLOWED_TOOL_INTENTS and text not in items:
            items.append(text)  # type: ignore[arg-type]
    if not items:
        return ["none"]
    if len(items) > 1 and "none" in items:
        items = [item for item in items if item != "none"]
    return items[:5]


def parse_turn_plan_payload(payload: Any) -> TurnPlan | None:
    if not isinstance(payload, dict):
        return None
    reply_action = _enum_value(payload.get("reply_action"), ALLOWED_REPLY_ACTIONS, "")
    if not reply_action:
        return None
    output_mode = _enum_value(payload.get("output_mode"), ALLOWED_OUTPUT_MODES, "chat_short")
    confidence = max(0.0, min(1.0, _coerce_float(payload.get("confidence"), 0.0)))
    return TurnPlan(
        reply_action=reply_action,  # type: ignore[arg-type]
        memory_need=_enum_value(payload.get("memory_need"), ALLOWED_MEMORY_NEEDS, "none"),  # type: ignore[arg-type]
        research_need=_enum_value(payload.get("research_need"), ALLOWED_RESEARCH_NEEDS, "none"),  # type: ignore[arg-type]
        vision_need=_enum_value(payload.get("vision_need"), ALLOWED_VISION_NEEDS, "none"),  # type: ignore[arg-type]
        qzone_continue=_coerce_bool(payload.get("qzone_continue"), False),
        output_mode=output_mode,  # type: ignore[arg-type]
        tool_intent=_coerce_tool_intents(payload.get("tool_intent")),
        ambiguity_level=_enum_value(payload.get("ambiguity_level"), ALLOWED_AMBIGUITY_LEVELS, "low"),  # type: ignore[arg-type]
        confidence=confidence,
        reason=str(payload.get("reason", "") or "").strip()[:80],
        message_target=_enum_value(payload.get("message_target"), ALLOWED_MESSAGE_TARGETS, "uncertain"),  # type: ignore[arg-type]
        session_goal=str(payload.get("session_goal", "") or "").strip()[:80],
    )


def extract_json_payload(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("`").strip()
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def metadata_fallback_turn_plan(
    *,
    is_group: bool = False,
    is_random_chat: bool = False,
    is_direct_mention: bool = False,
    has_images: bool = False,
    message_target: str = "",
    qzone_event_type: str = "",
) -> TurnPlan:
    target = str(message_target or "").strip()
    qzone_event = str(qzone_event_type or "").strip()
    if qzone_event:
        return TurnPlan(
            reply_action="reply",
            memory_need="light",
            research_need="none",
            vision_need="none",
            qzone_continue=True,
            output_mode="qzone_reply",
            tool_intent=["none"],
            ambiguity_level="low",
            confidence=0.22,
            reason="metadata_fallback_qzone",
            message_target="bot",
            session_goal="延续空间互动",
        )
    if is_group and is_random_chat and not is_direct_mention and target not in {"bot", "broadcast"}:
        return TurnPlan(
            reply_action="silence",
            memory_need="none",
            research_need="none",
            vision_need="summary" if has_images else "none",
            qzone_continue=False,
            output_mode="chat_short",
            tool_intent=["vision"] if has_images else ["none"],
            ambiguity_level="high",
            confidence=0.18,
            reason="metadata_fallback_random_group",
            message_target="uncertain",
            session_goal="避免误插话",
        )
    return TurnPlan(
        reply_action="reply",
        memory_need="light" if is_group else "none",
        research_need="none",
        vision_need="summary" if has_images else "none",
        qzone_continue=False,
        output_mode="chat_short",
        tool_intent=["vision"] if has_images else ["none"],
        ambiguity_level="low",
        confidence=0.18,
        reason="metadata_fallback",
        message_target="bot" if (not is_group or is_direct_mention or target == "bot") else "broadcast",
        session_goal="自然回应当前轮次",
    )


def _render_tool_metadata(tools: list[dict[str, Any]] | None) -> str:
    lines: list[str] = []
    for tool in list(tools or [])[:24]:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name", "") or "").strip()
        if not name:
            continue
        tags = tool.get("intent_tags", [])
        if isinstance(tags, list):
            tags_text = ",".join(str(item) for item in tags[:5])
        else:
            tags_text = str(tags or "")
        flags = []
        if tool.get("requires_network"):
            flags.append("network")
        if tool.get("requires_image"):
            flags.append("image")
        kind = str(tool.get("evidence_kind", "") or "").strip()
        latency = str(tool.get("latency_class", "") or "").strip()
        lines.append(f"- {name}: tags={tags_text or 'none'} kind={kind or 'generic'} latency={latency or 'normal'} flags={','.join(flags) or 'none'}")
    return "\n".join(lines) if lines else "无"


async def plan_turn_with_llm(
    text: str,
    *,
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
) -> TurnPlan:
    fallback = metadata_fallback_turn_plan(
        is_group=is_group,
        is_random_chat=is_random_chat,
        is_direct_mention=is_direct_mention,
        has_images=has_images,
        message_target=message_target,
        qzone_event_type=qzone_event_type,
    )
    normalized = normalize_plan_text(text)
    if not normalized or tool_caller is None:
        return fallback

    repeat_lines: list[str] = []
    for cluster in list(repeat_clusters or [])[:3]:
        plain = str(cluster.get("text", "") or "").strip()
        count = int(cluster.get("count", 0) or 0)
        if plain and count > 0:
            repeat_lines.append(f"- {plain} x{count}")

    system_prompt = (
        "你是群聊/私聊的回合规划器，只判断本轮应该做什么，不写最终回复。"
        "输出严格 JSON，不要 markdown，不要解释。\n"
        "JSON 结构："
        '{"reply_action":"reply|silence|ask_clarify",'
        '"memory_need":"none|light|deep",'
        '"research_need":"none|low|medium|high",'
        '"vision_need":"none|summary|native",'
        '"qzone_continue":false,'
        '"output_mode":"chat_short|chat_answer|structured_help|source_summary|qzone_reply",'
        '"tool_intent":["lookup_web|lookup_plugin|vision|image_gen|memory|none"],'
        '"ambiguity_level":"low|medium|high",'
        '"message_target":"bot|someone_else|broadcast|uncertain",'
        '"session_goal":"一句短中文目标",'
        '"confidence":0.0,'
        '"reason":"极短中文原因"}\n'
        "判别要求：\n"
        "1. reply_action 只决定回不回复；群聊不确定是否 cue bot 时用 silence。\n"
        "2. message_target 由 @、引用、称呼、上下文共同判断；uncertain 时通常 silence。\n"
        "3. 风格、用户态度、bot 情绪、TTS 和表情不要在这里决定。\n"
        "4. 工具意图只给候选方向，不要因为工具存在就强行使用。\n"
        "5. research_need=high 只给明显需要多源查证、时效或争议的问题。\n"
        "6. output_mode 控制最终回复长度和形态：chat_short 接梗，chat_answer 普通答，structured_help 教程，source_summary 检索摘要，qzone_reply 空间评论。\n"
        "7. fallback 只能当模型不确定时参考，不要机械照抄。\n"
    )
    user_content = (
        f"场景：{'群聊' if is_group else '私聊'}\n"
        f"是否随机插话：{'是' if is_random_chat else '否'}\n"
        f"是否直呼/提及 bot：{'是' if is_direct_mention else '否'}\n"
        f"代码侧 message_target：{str(message_target or '').strip() or '无'}\n"
        f"是否有图片：{'是' if has_images else '否'}\n"
        f"QZone事件：{str(qzone_event_type or '').strip() or '无'}\n"
        f"最新消息：{normalized}\n"
        f"最近上下文：{str(recent_context or '').strip()[:900] or '无'}\n"
        f"互动关系：{str(relationship_hint or '').strip()[:600] or '无'}\n"
        f"全局内心状态：{str(current_inner_state or '').strip()[:260] or '无'}\n"
        f"近期情绪记忆：{str(current_emotion_state or '').strip()[:260] or '无'}\n"
        f"复读线索：{'; '.join(repeat_lines) if repeat_lines else '无'}\n"
        f"可用工具元数据：\n{_render_tool_metadata(available_tools)}\n"
        "metadata fallback："
        f"reply_action={fallback.reply_action}, memory={fallback.memory_need}, research={fallback.research_need}, "
        f"vision={fallback.vision_need}, output_mode={fallback.output_mode}, target={fallback.message_target}, "
        f"ambiguity={fallback.ambiguity_level}"
        + (f"\n{group_knowledge_hint}" if group_knowledge_hint else "")
    )
    try:
        response = await tool_caller.chat_with_tools(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            tools=[],
            use_builtin_search=False,
        )
        payload = extract_json_payload(str(getattr(response, "content", "") or ""))
        plan = parse_turn_plan_payload(payload) if payload is not None else None
        return plan or fallback
    except Exception:
        return fallback


def turn_plan_from_semantic_frame(frame: Any, *, has_images: bool = False, message_target: str = "") -> TurnPlan:
    chat_intent = str(getattr(frame, "chat_intent", "banter") or "banter").strip()
    plugin_intent = str(getattr(frame, "plugin_question_intent", "capability") or "capability").strip()
    recommend_silence = bool(getattr(frame, "recommend_silence", False))
    output_mode: OutputMode = "chat_short"
    research_need: ResearchNeed = "none"
    tool_intent: list[ToolIntent] = ["none"]
    if chat_intent == "lookup":
        output_mode = "source_summary"
        research_need = "medium"
        tool_intent = ["lookup_web"]
    elif chat_intent == "plugin_question":
        output_mode = "structured_help"
        research_need = "low" if plugin_intent == "latest" else "none"
        tool_intent = ["lookup_plugin"] if plugin_intent != "latest" else ["lookup_plugin", "lookup_web"]
    elif chat_intent == "image_generation":
        output_mode = "chat_answer"
        tool_intent = ["image_gen"]
    elif chat_intent == "explanation":
        output_mode = "chat_answer"
    if has_images and "vision" not in tool_intent:
        tool_intent = [item for item in tool_intent if item != "none"] + ["vision"]
    ambiguity = str(getattr(frame, "ambiguity_level", "low") or "low").strip()
    if ambiguity not in ALLOWED_AMBIGUITY_LEVELS:
        ambiguity = "low"
    confidence = max(0.0, min(1.0, _coerce_float(getattr(frame, "confidence", 0.0), 0.0)))
    return TurnPlan(
        reply_action="silence" if recommend_silence else "reply",
        memory_need="light" if chat_intent in {"banter", "explanation"} else "none",
        research_need=research_need,
        vision_need="summary" if has_images else "none",
        qzone_continue=False,
        output_mode=output_mode,
        tool_intent=tool_intent,
        ambiguity_level=ambiguity,  # type: ignore[arg-type]
        confidence=confidence,
        reason=str(getattr(frame, "reason", "") or "semantic_frame_adapter").strip()[:80],
        message_target=_enum_value(message_target, ALLOWED_MESSAGE_TARGETS, "uncertain"),  # type: ignore[arg-type]
        session_goal="沿用旧语义帧",
    )


def turn_plan_to_semantic_frame(plan: TurnPlan) -> Any:
    from ...core.chat_intent import TurnSemanticFrame
    from ...core.sticker_semantics import default_sticker_semantic_hint

    tool_intents = set(plan.tool_intent or [])
    chat_intent = "banter"
    plugin_intent = "capability"
    if "image_gen" in tool_intents:
        chat_intent = "image_generation"
    elif "lookup_plugin" in tool_intents:
        chat_intent = "plugin_question"
        plugin_intent = "latest" if "lookup_web" in tool_intents or plan.research_need != "none" else "capability"
    elif "lookup_web" in tool_intents or plan.research_need in {"low", "medium", "high"}:
        chat_intent = "lookup"
    elif plan.output_mode in {"chat_answer", "structured_help"}:
        chat_intent = "explanation"
    frame = TurnSemanticFrame(
        chat_intent=chat_intent,  # type: ignore[arg-type]
        plugin_question_intent=plugin_intent,  # type: ignore[arg-type]
        ambiguity_level=plan.ambiguity_level,
        recommend_silence=plan.reply_action == "silence",
        requires_emotional_care=False,
        sticker_appropriate=plan.output_mode in {"chat_short", "qzone_reply"} and plan.reply_action == "reply",
        meta_question=False,
        domain_focus="plugin" if chat_intent == "plugin_question" else ("realtime" if chat_intent == "lookup" else "general"),
        user_attitude="待 responder 判断",
        bot_emotion="平静",
        emotion_intensity="medium",
        expression_style="自然短句" if plan.output_mode == "chat_short" else "直接清楚",
        tts_style_hint="自然",
        sticker_mood_hint=default_sticker_semantic_hint(chat_intent, is_random_chat=plan.reply_action == "silence"),
        confidence=plan.confidence,
        reason=plan.reason,
    )
    try:
        frame.turn_plan = plan
        frame.output_mode = plan.output_mode
        frame.session_goal = plan.session_goal
        frame.message_target = plan.message_target
    except Exception:
        pass
    return frame


__all__ = [
    "OUTPUT_MODE_LENGTHS",
    "TurnPlan",
    "extract_json_payload",
    "metadata_fallback_turn_plan",
    "normalize_plan_text",
    "parse_turn_plan_payload",
    "plan_turn_with_llm",
    "turn_plan_from_semantic_frame",
    "turn_plan_to_semantic_frame",
]
