from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from .sticker_semantics import (
    DEFAULT_STICKER_SEMANTIC_HINT,
    default_sticker_semantic_hint,
    normalize_sticker_semantic_hint,
)


ChatIntent = Literal["banter", "explanation", "lookup", "plugin_question", "image_generation"]
PluginQuestionIntent = Literal["capability", "implementation", "latest"]
AmbiguityLevel = Literal["low", "medium", "high"]
EmotionIntensity = Literal["low", "medium", "high"]


@dataclass
class IntentDecision:
    chat_intent: ChatIntent
    plugin_question_intent: PluginQuestionIntent = "capability"
    ambiguity_level: AmbiguityLevel = "low"
    recommend_silence: bool = False
    confidence: float = 0.0
    reason: str = ""


@dataclass
class TurnSemanticFrame:
    chat_intent: ChatIntent = "banter"
    plugin_question_intent: PluginQuestionIntent = "capability"
    ambiguity_level: AmbiguityLevel = "low"
    recommend_silence: bool = False
    requires_emotional_care: bool = False
    sticker_appropriate: bool = True
    meta_question: bool = False
    domain_focus: str = "general"
    user_attitude: str = "日常交流"
    bot_emotion: str = "平静"
    emotion_intensity: EmotionIntensity = "medium"
    expression_style: str = "自然简短"
    tts_style_hint: str = "自然"
    sticker_mood_hint: str = DEFAULT_STICKER_SEMANTIC_HINT
    confidence: float = 0.0
    reason: str = ""

    def to_intent_decision(self) -> IntentDecision:
        return IntentDecision(
            chat_intent=self.chat_intent,
            plugin_question_intent=self.plugin_question_intent,
            ambiguity_level=self.ambiguity_level,
            recommend_silence=self.recommend_silence,
            confidence=self.confidence,
            reason=self.reason,
        )


_EXPLANATORY_REPLY_PATTERNS = (
    "像是把 ",
    "这梗是",
    "意思就是",
    "相当于",
    "应该是在说",
    "可以理解成",
)


def normalize_intent_text(text: str) -> str:
    normalized = str(text or "").replace("\r", " ").replace("\n", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized



def looks_like_explanatory_output(text: str) -> bool:
    normalized = normalize_intent_text(text)
    if not normalized:
        return False
    return any(pattern in normalized for pattern in _EXPLANATORY_REPLY_PATTERNS)


def _metadata_fallback_turn_semantic_frame(
    *,
    is_group: bool,
    is_random_chat: bool,
) -> TurnSemanticFrame:
    if is_group and is_random_chat:
        return TurnSemanticFrame(
            chat_intent="banter",
            plugin_question_intent="capability",
            ambiguity_level="high",
            recommend_silence=True,
            requires_emotional_care=False,
            sticker_appropriate=False,
            meta_question=False,
            domain_focus="social",
            user_attitude="群里随口聊天",
            bot_emotion="轻松观察",
            emotion_intensity="low",
            expression_style="自然短句，不要硬插",
            tts_style_hint="自然",
            sticker_mood_hint=default_sticker_semantic_hint("banter", is_random_chat=True),
            confidence=0.18,
            reason="metadata_fallback_random_group",
        )
    return TurnSemanticFrame(
        chat_intent="banter",
        plugin_question_intent="capability",
        ambiguity_level="low",
        recommend_silence=False,
        requires_emotional_care=False,
        sticker_appropriate=True,
        meta_question=False,
        domain_focus="general" if not is_group else "social",
        user_attitude="日常交流",
        bot_emotion="平静",
        emotion_intensity="medium",
        expression_style="自然简短",
        tts_style_hint="自然",
        sticker_mood_hint=default_sticker_semantic_hint("banter", is_random_chat=is_random_chat),
        confidence=0.18,
        reason="metadata_fallback",
    )


def metadata_fallback_turn_semantic_frame_for_session(
    *,
    is_group: bool = False,
    is_random_chat: bool = False,
) -> TurnSemanticFrame:
    return _metadata_fallback_turn_semantic_frame(
        is_group=is_group,
        is_random_chat=is_random_chat,
    )


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if value is None:
        return default
    return bool(value)


def _parse_turn_semantic_frame_payload(payload: Any) -> TurnSemanticFrame | None:
    if not isinstance(payload, dict):
        return None
    chat_intent = str(payload.get("chat_intent", "") or "").strip()
    if chat_intent not in {"banter", "explanation", "lookup", "plugin_question", "image_generation"}:
        return None
    plugin_question_intent = str(payload.get("plugin_question_intent", "capability") or "capability").strip()
    if plugin_question_intent not in {"capability", "implementation", "latest"}:
        plugin_question_intent = "capability"
    ambiguity_level = str(payload.get("ambiguity_level", "low") or "low").strip()
    if ambiguity_level not in {"low", "medium", "high"}:
        ambiguity_level = "low"
    emotion_intensity = str(payload.get("emotion_intensity", "medium") or "medium").strip().lower()
    if emotion_intensity not in {"low", "medium", "high"}:
        emotion_intensity = "medium"
    try:
        confidence = float(payload.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    domain_focus = str(payload.get("domain_focus", "general") or "general").strip() or "general"
    return TurnSemanticFrame(
        chat_intent=chat_intent,  # type: ignore[arg-type]
        plugin_question_intent=plugin_question_intent,  # type: ignore[arg-type]
        ambiguity_level=ambiguity_level,  # type: ignore[arg-type]
        recommend_silence=_coerce_bool(payload.get("recommend_silence", False)),
        requires_emotional_care=_coerce_bool(payload.get("requires_emotional_care", False)),
        sticker_appropriate=_coerce_bool(payload.get("sticker_appropriate", True), True),
        meta_question=_coerce_bool(payload.get("meta_question", False)),
        domain_focus=domain_focus[:32],
        user_attitude=str(payload.get("user_attitude", "") or "").strip() or "日常交流",
        bot_emotion=str(payload.get("bot_emotion", "") or "").strip() or "平静",
        emotion_intensity=emotion_intensity,  # type: ignore[arg-type]
        expression_style=str(payload.get("expression_style", "") or "").strip() or "自然简短",
        tts_style_hint=str(payload.get("tts_style_hint", "") or "").strip() or "自然",
        sticker_mood_hint=normalize_sticker_semantic_hint(payload.get("sticker_mood_hint")),
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(payload.get("reason", "") or "").strip(),
    )


def _extract_json_payload(raw: str) -> dict[str, Any] | None:
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


async def infer_turn_semantic_frame_with_llm(
    text: str,
    *,
    is_group: bool = False,
    is_random_chat: bool = False,
    tool_caller: Any = None,
    recent_context: str = "",
    relationship_hint: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
    current_inner_state: str = "",
    current_emotion_state: str = "",
) -> TurnSemanticFrame:
    fallback = _metadata_fallback_turn_semantic_frame(
        is_group=is_group,
        is_random_chat=is_random_chat,
    )
    normalized = normalize_intent_text(text)
    if not normalized or tool_caller is None:
        return fallback

    repeat_lines: list[str] = []
    for cluster in list(repeat_clusters or [])[:3]:
        plain = str(cluster.get("text", "") or "").strip()
        count = int(cluster.get("count", 0) or 0)
        if plain and count > 0:
            repeat_lines.append(f"- {plain} x{count}")

    prompt = (
        "你是群聊/私聊的语义与情绪判别器。"
        "请根据最新一句和上下文，输出严格 JSON，不要输出 markdown 或解释。\n"
        "JSON 结构："
        '{"chat_intent":"banter|explanation|lookup|plugin_question|image_generation",'
        '"plugin_question_intent":"capability|implementation|latest",'
        '"ambiguity_level":"low|medium|high",'
        '"recommend_silence":false,'
        '"requires_emotional_care":false,'
        '"sticker_appropriate":true,'
        '"meta_question":false,'
        '"domain_focus":"general|social|realtime|game_anime|plugin|knowledge|emotion",'
        '"user_attitude":"一句短中文，描述用户这轮对 bot 的态度",'
        '"bot_emotion":"一句短中文，描述 bot 当前自然产生的情绪",'
        '"emotion_intensity":"low|medium|high",'
        '"expression_style":"一句短中文，描述本轮该如何表达",'
        '"tts_style_hint":"给 TTS 的简短风格词",'
        '"sticker_mood_hint":"给表情包选择的结构化标签，格式固定为 情绪标签|场景标签",'
        '"confidence":0.0,'
        '"reason":"一句极短中文原因"}\n'
        "判别要求：\n"
        "1. chat_intent 关注用户这一轮真正想让 bot 做什么，而不是句子里表面词汇。\n"
        "2. plugin_question 只在对方确实在问 bot/插件/本地实现/配置/能力时使用。"
        "用户让 bot 直接生成、绘制、制作图片/海报/头像/梗图时，chat_intent=image_generation，不要标成 plugin_question。\n"
        "3. meta_question=true 表示这句更像在问上一条消息是谁发的、为什么触发、接的是谁的话，而不是问正文知识点。\n"
        "4. requires_emotional_care=true 只在用户明显需要被安抚、接住情绪、失落/委屈/脆弱时使用；普通吐槽、调侃、玩梗不要滥开。"
        "群聊里如果不确定对方是不是在对你倾诉，优先保持 false。\n"
        "5. sticker_appropriate=false 表示这轮不适合发表情包，例如严肃澄清、情绪安抚、风险话题、直接答疑、明显冷淡/敌意或容易显得轻浮的场景。\n"
        "6. user_attitude 要体现用户这一轮对 bot 的态度，如调侃、求助、冷淡、试探、亲近、挑衅、认真追问。\n"
        "7. bot_emotion 要结合当前内心状态、关系线索和最近互动，自然给出，不要机械复制用户情绪。\n"
        "8. expression_style 要指导本轮说话方式，偏行为策略，不要写长句。\n"
        "9. recommend_silence 只有在群聊且明显高歧义、bot 插话风险高时才为 true。\n"
        "10. sticker_mood_hint 只能从这类标签中组合："
        "情绪标签=[搞笑,开心,感动,尴尬,无语,惊讶,委屈,生气,害羞,得意,困惑,赞同,拒绝,期待,失落,撒娇,淡定,震惊]；"
        "场景标签=[回应笑点,接梗,表达赞同,化解尴尬,自嘲,反驳,表达惊讶,安慰对方,撒娇,表示无奈,冷场时,表达期待,庆祝,拒绝请求,结束对话,打招呼,表达关心,吐槽,卖萌,表达疑惑]；"
        "严格输出为“情绪标签|场景标签”。\n"
        "## 中文互联网/游戏黑话歧义说明\n以下词语在中文群聊中有常见的非字面含义，判别时需结合上下文：\n- 粥/三角洲：腾讯游戏《三角洲行动》的别称（不是食物的粥）\n- 鸡：《绝地求生》PUBG；吃鸡=赢了PUBG\n- 原神/崩铁/ZZZ：米哈游系列游戏简称\n- dd/DD=关注/粉丝（虚拟主播圈）；典=嘘讽某行为；蚁埠住了=绷不住了；破防=情绪崩了\n11. 当最新消息包含上述歧义词且上下文不足以确认含义时，将 ambiguity_level 设为 high；若是随机插话场景还应将 recommend_silence 设为 true，等待更多上下文再参与。\n"
    )
    user_content = (
        f"场景：{'群聊' if is_group else '私聊'}\n"
        f"是否随机插话：{'是' if is_random_chat else '否'}\n"
        f"最新消息：{normalized}\n"
        f"最近上下文：{str(recent_context or '').strip()[:700] or '无'}\n"
        f"互动关系：{str(relationship_hint or '').strip()[:500] or '无'}\n"
        f"全局内心状态：{str(current_inner_state or '').strip()[:260] or '无'}\n"
        f"近期情绪记忆：{str(current_emotion_state or '').strip()[:260] or '无'}\n"
        f"复读线索：{'; '.join(repeat_lines) if repeat_lines else '无'}\n"
        "场景 fallback："
        f"intent={fallback.chat_intent}, plugin={fallback.plugin_question_intent}, "
        f"ambiguity={fallback.ambiguity_level}, silence={fallback.recommend_silence}, "
        f"user_attitude={fallback.user_attitude}, bot_emotion={fallback.bot_emotion}"
    )
    try:
        response = await tool_caller.chat_with_tools(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            tools=[],
            use_builtin_search=False,
        )
        payload = _extract_json_payload(str(getattr(response, "content", "") or ""))
        frame = _parse_turn_semantic_frame_payload(payload) if payload is not None else None
        return frame or fallback
    except Exception:
        return fallback


async def infer_intent_decision_with_llm(
    text: str,
    *,
    is_group: bool = False,
    is_random_chat: bool = False,
    tool_caller: Any = None,
    recent_context: str = "",
    relationship_hint: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
) -> IntentDecision:
    frame = await infer_turn_semantic_frame_with_llm(
        text,
        is_group=is_group,
        is_random_chat=is_random_chat,
        tool_caller=tool_caller,
        recent_context=recent_context,
        relationship_hint=relationship_hint,
        repeat_clusters=repeat_clusters,
    )
    return frame.to_intent_decision()


__all__ = [
    "AmbiguityLevel",
    "ChatIntent",
    "EmotionIntensity",
    "IntentDecision",
    "PluginQuestionIntent",
    "TurnSemanticFrame",
    "infer_intent_decision_with_llm",
    "infer_turn_semantic_frame_with_llm",
    "looks_like_explanatory_output",
    "metadata_fallback_turn_semantic_frame_for_session",
    "normalize_intent_text",
]
