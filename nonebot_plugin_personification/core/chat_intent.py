from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .reply_style_policy import build_context_continuity_policy_prompt
from .sticker_semantics import (
    DEFAULT_STICKER_SEMANTIC_HINT,
    default_sticker_semantic_hint,
    normalize_sticker_semantic_hint,
)


ChatIntent = Literal["banter", "explanation", "lookup", "plugin_question", "image_generation", "expression"]
PluginQuestionIntent = Literal["capability", "runtime_capability", "implementation", "latest"]
AmbiguityLevel = Literal["low", "medium", "high"]
EmotionIntensity = Literal["low", "medium", "high"]
DomainFocus = Literal["general", "social", "technology", "science", "game_anime", "plugin", "realtime", "emotion"]
EvidencePolicy = Literal["none", "light", "standard", "strict"]
AdvicePermission = Literal["not_needed", "ask_first", "allowed"]
CareRiskLevel = Literal["none", "concern", "high"]
ConversationScenario = Literal[
    "normal",
    "casual_banter",
    "sarcasm_irony",
    "argument",
    "inside_joke",
    "multi_thread",
    "private_topic",
]
_VALID_SCENARIOS: set[str] = {
    "normal", "casual_banter", "sarcasm_irony", "argument",
    "inside_joke", "multi_thread", "private_topic",
}


@dataclass
class IntentDecision:
    chat_intent: ChatIntent
    plugin_question_intent: PluginQuestionIntent = "capability"
    ambiguity_level: AmbiguityLevel = "low"
    recommend_silence: bool = False
    confidence: float = 0.0
    reason: str = ""


@dataclass
class EmotionalSupport:
    needed: bool = False
    listen: bool = False
    validate: bool = False
    advice_permission: AdvicePermission = "not_needed"
    risk_level: CareRiskLevel = "none"


@dataclass
class TurnSemanticFrame:
    chat_intent: ChatIntent = "banter"
    plugin_question_intent: PluginQuestionIntent = "capability"
    ambiguity_level: AmbiguityLevel = "low"
    recommend_silence: bool = False
    requires_emotional_care: bool = False
    sticker_appropriate: bool = True
    meta_question: bool = False
    domain_focus: DomainFocus = "general"
    evidence_policy: EvidencePolicy = "none"
    emotional_support: EmotionalSupport = field(default_factory=EmotionalSupport)
    user_attitude: str = "日常交流"
    bot_emotion: str = "平静"
    emotion_intensity: EmotionIntensity = "medium"
    expression_style: str = "自然简短"
    tts_style_hint: str = "自然"
    sticker_mood_hint: str = DEFAULT_STICKER_SEMANTIC_HINT
    group_atmosphere_positive: bool = False
    interaction_interesting: bool = False
    future_commitment_candidate: bool = False
    conversation_scenario: ConversationScenario = "normal"
    # 由 LLM 决定回复时要不要 @ 对方 / 引用对方消息：auto=交给默认启发式；
    # none=直接接话；at=@对方；quote=引用回复；at_quote=两者都用。
    address_mode: str = "auto"
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


_VALID_DOMAIN_FOCUS = {"general", "social", "technology", "science", "game_anime", "plugin", "realtime", "emotion"}
_VALID_EVIDENCE_POLICIES = {"none", "light", "standard", "strict"}
_VALID_ADVICE_PERMISSIONS = {"not_needed", "ask_first", "allowed"}
_VALID_CARE_RISKS = {"none", "concern", "high"}


def _parse_emotional_support(value: Any, *, legacy_needed: bool = False) -> EmotionalSupport:
    if isinstance(value, Mapping):
        data = value
    elif value is not None:
        data = {
            key: getattr(value, key)
            for key in ("needed", "listen", "validate", "advice_permission", "risk_level")
            if hasattr(value, key)
        }
    else:
        data = {}
    if not data:
        return EmotionalSupport(needed=legacy_needed, listen=legacy_needed, validate=legacy_needed)
    needed = _coerce_bool(data.get("needed"), legacy_needed)
    advice_permission = str(data.get("advice_permission", "not_needed") or "not_needed").strip().lower()
    risk_level = str(data.get("risk_level", "none") or "none").strip().lower()
    return EmotionalSupport(
        needed=needed,
        listen=_coerce_bool(data.get("listen"), needed),
        validate=_coerce_bool(data.get("validate"), needed),
        advice_permission=advice_permission if advice_permission in _VALID_ADVICE_PERMISSIONS else "not_needed",  # type: ignore[arg-type]
        risk_level=risk_level if risk_level in _VALID_CARE_RISKS else "none",  # type: ignore[arg-type]
    )


def _parse_turn_semantic_frame_payload(payload: Any) -> TurnSemanticFrame | None:
    if not isinstance(payload, dict):
        return None
    chat_intent = str(payload.get("chat_intent", "") or "").strip()
    if chat_intent not in {"banter", "explanation", "lookup", "plugin_question", "image_generation", "expression"}:
        return None
    plugin_question_intent = str(payload.get("plugin_question_intent", "capability") or "capability").strip()
    if plugin_question_intent not in {"capability", "runtime_capability", "implementation", "latest"}:
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
    domain_focus = str(payload.get("domain_focus", "general") or "general").strip().lower()
    if domain_focus == "knowledge":
        domain_focus = "general"
    if domain_focus not in _VALID_DOMAIN_FOCUS:
        domain_focus = "general"
    evidence_policy = str(payload.get("evidence_policy", "none") or "none").strip().lower()
    if evidence_policy not in _VALID_EVIDENCE_POLICIES:
        evidence_policy = "none"
    legacy_care = _coerce_bool(payload.get("requires_emotional_care", False))
    emotional_support = _parse_emotional_support(payload.get("emotional_support"), legacy_needed=legacy_care)
    return TurnSemanticFrame(
        chat_intent=chat_intent,  # type: ignore[arg-type]
        plugin_question_intent=plugin_question_intent,  # type: ignore[arg-type]
        ambiguity_level=ambiguity_level,  # type: ignore[arg-type]
        recommend_silence=_coerce_bool(payload.get("recommend_silence", False)),
        requires_emotional_care=emotional_support.needed,
        sticker_appropriate=_coerce_bool(payload.get("sticker_appropriate", True), True),
        meta_question=_coerce_bool(payload.get("meta_question", False)),
        domain_focus=domain_focus,  # type: ignore[arg-type]
        evidence_policy=evidence_policy,  # type: ignore[arg-type]
        emotional_support=emotional_support,
        user_attitude=str(payload.get("user_attitude", "") or "").strip() or "日常交流",
        bot_emotion=str(payload.get("bot_emotion", "") or "").strip() or "平静",
        emotion_intensity=emotion_intensity,  # type: ignore[arg-type]
        expression_style=str(payload.get("expression_style", "") or "").strip() or "自然简短",
        tts_style_hint=str(payload.get("tts_style_hint", "") or "").strip() or "自然",
        sticker_mood_hint=normalize_sticker_semantic_hint(payload.get("sticker_mood_hint")),
        group_atmosphere_positive=_coerce_bool(payload.get("group_atmosphere_positive", False)),
        interaction_interesting=_coerce_bool(payload.get("interaction_interesting", False)),
        future_commitment_candidate=_coerce_bool(
            payload.get("future_commitment_candidate", False)
        ),
        conversation_scenario=_parse_scenario(payload.get("conversation_scenario")),
        address_mode=_parse_address_mode(payload.get("address_mode")),
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(payload.get("reason", "") or "").strip(),
    )


def _parse_scenario(value: Any) -> ConversationScenario:
    normalized = str(value or "normal").strip().lower()
    return normalized if normalized in _VALID_SCENARIOS else "normal"  # type: ignore[return-value]


_VALID_ADDRESS_MODES = {"auto", "none", "at", "quote", "at_quote"}


def _parse_address_mode(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    return normalized if normalized in _VALID_ADDRESS_MODES else "auto"


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
    is_direct_mention: bool = False,
    message_target: str = "",
    tool_caller: Any = None,
    recent_context: str = "",
    relationship_hint: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
    current_inner_state: str = "",
    current_emotion_state: str = "",
    media_grounding: str = "",
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
        '{"chat_intent":"banter|explanation|lookup|plugin_question|image_generation|expression",'
        '"plugin_question_intent":"capability|runtime_capability|implementation|latest",'
        '"ambiguity_level":"low|medium|high",'
        '"recommend_silence":false,'
        '"requires_emotional_care":false,'
        '"sticker_appropriate":true,'
        '"meta_question":false,'
        '"domain_focus":"general|social|technology|science|game_anime|plugin|realtime|emotion",'
        '"evidence_policy":"none|light|standard|strict",'
        '"emotional_support":{"needed":false,"listen":false,"validate":false,"advice_permission":"not_needed|ask_first|allowed","risk_level":"none|concern|high"},'
        '"user_attitude":"一句短中文，描述用户这轮对 bot 的态度",'
        '"bot_emotion":"一句短中文，描述 bot 当前自然产生的情绪",'
        '"emotion_intensity":"low|medium|high",'
        '"expression_style":"一句短中文，描述本轮该如何表达",'
        '"tts_style_hint":"给 TTS 的简短风格词",'
        '"sticker_mood_hint":"给表情包选择的结构化标签，格式固定为 情绪标签|场景标签",'
        '"group_atmosphere_positive":false,'
        '"interaction_interesting":false,'
        '"future_commitment_candidate":false,'
        '"conversation_scenario":"normal|casual_banter|sarcasm_irony|argument|inside_joke|multi_thread|private_topic",'
        '"address_mode":"auto|none|at|quote|at_quote",'
        '"confidence":0.0,'
        '"reason":"一句极短中文原因"}\n'
        "判别要求：\n"
        "1. chat_intent 关注用户这一轮真正想让 bot 做什么，而不是句子里表面词汇。\n"
        "1a. 对方明确 @/直呼 bot 只表示这轮在叫你回应，不等于 chat_intent=explanation。"
        "如果正文是在调侃、甩锅、轻挑衅、玩笑指控或接梗，仍应判为 banter，并让 user_attitude、bot_emotion、expression_style 体现具体互动；"
        "只有真的在索取知识解释、查证或步骤时才判 explanation/lookup。\n"
        "1b. 如果最新消息引用了一个你不确定含义的专有名词/梗/节目名/人名/外号，"
        "或群友分享了图片/视频/链接而你无法确定其内容或出处（例如配图只配了一个短词、视频/分享卡片标题里有陌生词），"
        "倾向 chat_intent=lookup（先查证再接话），不要只因为是短句、像闲聊就判成 banter——"
        "先搞懂群友在聊什么再接话才更像真人。\n"
        "1c. 如果最新消息说“这个动画/这段动画/这个角色/这张图/这个场面”等指代词，且最近上下文刚出现 ACG 角色、作品、抽卡结果、图片或卡面，"
        "要把它当成在评价那个具体对象；优先 chat_intent=lookup、domain_focus=game_anime，查清角色/作品/剧情或动画出处后再参与讨论。"
        "不要只回“确实挺强/有那味了”这类空泛附和。\n"
        "2. plugin_question 只在对方确实在问 bot/插件/本地实现/配置/能力时使用。"
        "当前 runtime_capability 只覆盖对方询问 bot 能否读取或理解当前发送者头像中的可观察画面事实；"
        "这类问题使用 plugin_question/runtime_capability，后续必须交给第一方只读工具核实。"
        "其它用户资料、记忆、群信息或会话能力问题仍使用 plugin_question/capability，除非以后有对应 Tool 明确声明 runtime_capability。"
        "用户让 bot 直接生成、绘制、制作图片/海报/头像/梗图时，chat_intent=image_generation，不要标成 plugin_question；"
        "用户让 bot 联网搜已有图片、壁纸、插画、图包或参考图时，chat_intent=lookup，不要误判成生成图片。\n"
        "2b. 用户让 bot 发送 QQ 小黄脸、系统表情、收藏表情、推荐表情，或这轮明确只需要发一个 QQ 表情回应时，"
        "chat_intent=expression；不要把这种动作请求标成普通闲聊或解释。\n"
        "3. meta_question=true 表示这句更像在问上一条消息是谁发的、为什么触发、接的是谁的话，而不是问正文知识点。\n"
        "4. emotional_support 结构化判断用户是否需要倾听和情绪确认、是否已允许建议、以及是否存在风险；普通吐槽、调侃、玩梗不要滥开。"
        "群聊里如果不确定对方是不是在对你倾诉，needed 优先 false；requires_emotional_care 与 needed 保持一致以兼容旧字段。\n"
        "4b. evidence_policy 按事实要求决定：普通闲聊 none，轻量背景 light，一般事实 standard，技术/科学/时效/高影响结论 strict。"
        "domain_focus 必须使用给定枚举，不要自造 knowledge 等值。\n"
        "5. sticker_appropriate=false 表示这轮不适合发表情包，例如严肃澄清、情绪安抚、风险话题、直接答疑、明显冷淡/敌意或容易显得轻浮的场景。\n"
        "6. user_attitude 要体现用户这一轮对 bot 的态度，如调侃、求助、冷淡、试探、亲近、挑衅、认真追问。\n"
        "7. bot_emotion 要结合当前内心状态、关系线索和最近互动，自然给出，不要机械复制用户情绪。\n"
        "8. expression_style 要指导本轮说话方式，偏行为策略，不要写长句。\n"
        "8b. group_atmosphere_positive 只在群聊整体互动明确友好、融洽或共同参与感很强时为 true；普通无冲突聊天不能机械设为 true，私聊必须 false。"
        "interaction_interesting 只在 bot 与当前用户这轮确实有明显趣味、默契或高质量互动时为 true；成功回复本身不等于有趣。\n"
        "8c. future_commitment_candidate 只在私聊用户明确表达带时间线索的未来承诺、计划或待办时为 true；"
        "已完成的事、无时间的愿望和普通闲聊必须为 false。\n"
        "9. recommend_silence 只有在群聊且明显高歧义、bot 插话风险确实较高时才为 true；"
        "不能只因为本轮是随机插话、没有 @bot 或结构化目标暂为 unclear 就机械静默。"
        "最新消息若是面向群聊的开放表达，且能沿最近同一话题自然推进半步，可以给 false；"
        "明确群友点对点对话、纯媒体/回声、私密话题或上下文明显不足时才优先 true。\n"
        "10. 群聊上下文中若标注“回复某人/提及某人/来源=其他插件输出/用户调用其它插件/命令/群聊旁观”，不要把它误认为用户正在要求 bot 解释内容；"
        "明确回复/提及其他群友时应提高插话谨慎度，必要时 recommend_silence=true；"
        "目标只是 unclear 时要继续结合最近话题、参与者和消息是否面向全群判断，不能等同于 target_others。\n"
        "10b. 若上下文明确提供其它插件 interaction episode，source_kind=plugin 的输出不是人格 bot 自己说过的话。"
        "紧随其后的短句默认先按评论插件结果、接梗或调侃理解，优先 banter + domain_focus=social + evidence_policy=none；"
        "不要因为插件结果包含专业名词就转成 explanation/lookup。明确 @bot 也只表示轮到你回应，"
        "只有最新消息清楚另起独立事实问题时才允许解释或查证。\n"
        "11. sticker_mood_hint 只能从这类标签中组合："
        "情绪标签=[搞笑,开心,感动,尴尬,无语,惊讶,委屈,生气,害羞,得意,困惑,赞同,拒绝,期待,失落,撒娇,淡定,震惊]；"
        "场景标签=[回应笑点,接梗,表达赞同,化解尴尬,自嘲,反驳,表达惊讶,安慰对方,撒娇,表示无奈,冷场时,表达期待,庆祝,拒绝请求,结束对话,打招呼,表达关心,吐槽,卖萌,表达疑惑]；"
        "严格输出为“情绪标签|场景标签”。\n"
        "12. 遇到陌生的游戏黑话、圈内说法或同词多义时，不要依赖静态词表猜义；结合当前对话、结构化词典 sense 与可用查证证据判断。"
        "语境不足时提高 ambiguity_level；只有随机插话、没有指向 bot，并且明显是别人的对话，"
        "或目标不明且现有上下文不足以自然参与时，才强制建议静默。\n"
        "13. conversation_scenario 判断当前对话场景：\n"
        "- casual_banter：群友互相调侃、插科打诨\n"
        "- sarcasm_irony：反讽/阴阳怪气\n"
        "- argument：争吵/对立\n"
        "- inside_joke：群内部梗/暗号\n"
        "- multi_thread：多个话题同时进行\n"
        "- private_topic：涉及个人隐私/敏感话题\n"
        "- normal：以上都不是\n"
        "14. address_mode 决定这条回复要不要 @ 对方或引用对方消息，让多人群聊更像真人、指向更清楚：\n"
        "- none：直接接话、不@不引用（一对一顺畅聊、或刚好接着对方最后一句时优先，别每句都@显得机械）\n"
        "- at：多人混战、你要回的人已经被后面的新消息刷上去了、或需要明确『我在回你』时，@ 对方\n"
        "- quote：需要精确指向对方那条具体消息（尤其是隔了好几条、容易认错）时，引用回复\n"
        "- at_quote：两者都用（少用，仅在既要指人又要指那条消息时）\n"
        "- auto：拿不准就给 auto，交给默认规则。私聊一律 none/auto。\n"
        "当你是在接一条 @bot 的群消息但回复内容实际需要承接前面某个群友/插件输出时，优先选择 quote 或 at_quote 指向当前触发消息，"
        "不要让后续群友误以为你在无对象地泛泛评价。\n"
        "15. 明确 @/直呼 bot 时，除非消息为空、纯动作请求已由工具完成或安全原因必须拒绝，否则 recommend_silence 应为 false；"
        "轻松调侃场景优先给出有立场的即时反应，不要只生成‘在呢/哈哈/怎么了’。\n"
        f"{build_context_continuity_policy_prompt()}\n"
    )
    user_content = (
        f"场景：{'群聊' if is_group else '私聊'}\n"
        f"是否随机插话：{'是' if is_random_chat else '否'}\n"
        f"是否明确 @/直呼 bot：{'是' if is_direct_mention else '否'}\n"
        f"结构化消息目标：{str(message_target or '').strip() or 'uncertain'}\n"
        f"最新消息：{normalized}\n"
        f"最近上下文：{str(recent_context or '').strip()[:700] or '无'}\n"
        f"互动关系：{str(relationship_hint or '').strip()[:500] or '无'}\n"
        f"全局内心状态：{str(current_inner_state or '').strip()[:260] or '无'}\n"
        f"近期情绪记忆：{str(current_emotion_state or '').strip()[:260] or '无'}\n"
        f"媒体 provenance / 视觉 grounding：\n{str(media_grounding or '').strip()[:1800] or '无'}\n"
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
        if frame is None:
            return fallback
        normalized_target = str(message_target or "").strip().lower()
        directed = is_direct_mention or normalized_target in {"target_bot", "bot", "broadcast"}
        undirected_random_target = normalized_target in {
            "",
            "target_unclear",
            "target_others",
            "target_external_plugin",
            "uncertain",
            "others",
            "someone_else",
            "external_plugin",
        }
        if is_group and frame.confidence < 0.4:
            frame.ambiguity_level = "high"
            if is_random_chat and not directed and undirected_random_target:
                frame.recommend_silence = True
            elif directed:
                frame.recommend_silence = False
        return frame
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
    "ConversationScenario",
    "EmotionIntensity",
    "EmotionalSupport",
    "EvidencePolicy",
    "DomainFocus",
    "IntentDecision",
    "PluginQuestionIntent",
    "TurnSemanticFrame",
    "infer_intent_decision_with_llm",
    "infer_turn_semantic_frame_with_llm",
    "looks_like_explanatory_output",
    "metadata_fallback_turn_semantic_frame_for_session",
    "normalize_intent_text",
]
