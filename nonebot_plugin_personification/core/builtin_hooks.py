from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..skills.skillpacks.friend_request_tool.scripts.impl import check_friend_request_gate
from ..utils import build_group_context_window, get_group_topic_summary
from .chat_intent import infer_turn_semantic_frame_with_llm
from .context_policy import sanitize_history_text
from .group_context import render_group_context_structured
from .group_relations import summarize_group_relationships
from .message_relations import extract_event_message_id, extract_reply_message_id
from .prompt_hooks import HookContext, register_prompt_hook


_FRIEND_IDS_CACHE: Dict[str, tuple[float, set[str]]] = {}
_REGISTERED = False


def _stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                parts.append(str(item.get("text")))
        return "".join(parts)
    return str(content or "")


def _count_user_interactions(messages: List[Dict[str, Any]], user_id: str) -> int:
    marker = f"({user_id})"
    count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content_text = _stringify_message_content(message.get("content", ""))
        if marker in content_text:
            count += 1
    return count


def _normalized_topic_key(text: Any) -> str:
    normalized = sanitize_history_text(text).lower()
    normalized = "".join(ch for ch in normalized if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return normalized[:80]


async def _ensure_semantic_frame(ctx: HookContext, *, recent_context: str = "") -> Any:
    if getattr(ctx, "semantic_frame", None) is not None:
        return ctx.semantic_frame
    tool_caller = getattr(ctx.runtime, "lite_tool_caller", None) or getattr(ctx.runtime, "agent_tool_caller", None)
    if tool_caller is None:
        return None
    frame = await infer_turn_semantic_frame_with_llm(
        ctx.message_text or ctx.message_content or "",
        is_group=not ctx.is_private,
        is_random_chat=ctx.is_random_chat,
        tool_caller=tool_caller,
        recent_context=recent_context,
    )
    ctx.semantic_frame = frame
    return frame


def _format_recent_group_context(
    group_id: str,
    *,
    limit: int = 6,
    trigger_msg_id: str = "",
    reply_to_msg_id: str = "",
) -> str:
    recent = build_group_context_window(
        group_id,
        limit=limit,
        include_message_ids=[reply_to_msg_id],
    )
    if not recent:
        return ""
    return render_group_context_structured(recent, trigger_msg_id=trigger_msg_id)


def _is_domain_sensitive_frame(frame: Any) -> bool:
    if frame is None:
        return False
    focus = str(getattr(frame, "domain_focus", "") or "").strip().lower()
    return focus in {"game_anime", "realtime", "knowledge", "plugin"}


async def _get_cached_friend_ids(bot: Any, logger: Any, ttl_seconds: int = 300) -> set[str]:
    cache_key = str(getattr(bot, "self_id", "") or "default")
    now_ts = time.time()
    cached = _FRIEND_IDS_CACHE.get(cache_key)
    if cached and now_ts - cached[0] < ttl_seconds:
        return set(cached[1])

    friend_ids: set[str] = set()
    try:
        friends = await bot.get_friend_list()
        if isinstance(friends, list):
            for item in friends:
                if isinstance(item, dict) and item.get("user_id") is not None:
                    friend_ids.add(str(item.get("user_id")))
    except Exception as e:
        logger.debug(f"[prompt_hook] get_friend_list failed: {e}")

    _FRIEND_IDS_CACHE[cache_key] = (now_ts, set(friend_ids))
    return friend_ids


async def _schedule_hook(ctx: HookContext) -> Optional[str]:
    group_config = ctx.persona.get_group_config(ctx.group_id)
    schedule_active = (
        group_config.get("schedule_enabled", False)
        or getattr(ctx.plugin_config, "personification_schedule_global", False)
    )
    if not schedule_active:
        return (
            "## 当前时间信息\n"
            f"- 当前时间：{ctx.current_time_str}\n"
            "仅用于保持时间语义自然，比如早晚问候、作息口吻，不强制限制是否回复。"
        )

    return (
        "## 当前时间与状态参考\n"
        f"- 当前时间：{ctx.current_time_str}\n"
        f"{ctx.runtime.get_schedule_prompt_injection()}"
    )


async def _user_persona_hook(ctx: HookContext) -> Optional[str]:
    persona_store = getattr(ctx.runtime, "persona_store", None)
    if not persona_store:
        return None
    max_chars = max(
        0,
        int(getattr(ctx.plugin_config, "personification_persona_prompt_max_chars", 120) or 120),
    )
    if max_chars <= 0:
        return None
    user_persona = persona_store.get_persona_snippet(ctx.user_id, max_chars=max_chars)
    if not user_persona:
        return None
    return (
        "## 互动印象参考\n"
        "仅用于判断亲疏、话题偏好和语气轻重，不得当作确定事实，也不要主动复述成档案式描述：\n"
        f"{user_persona}"
    )


async def _group_style_hook(ctx: HookContext) -> Optional[str]:
    if ctx.is_private:
        return None

    style = ctx.persona.get_group_style(ctx.group_id)
    summary = get_group_topic_summary(ctx.group_id)
    parts: list[str] = []
    if style:
        parts.append(
            "## 当前群聊风格参考\n"
            f"{style}\n"
            "请在回复时适当融入上述群聊风格，使对话更自然。"
        )
    if summary:
        parts.append(
            "## 群聊近期话题\n"
            f"{summary}\n"
            "（供背景感知用，不必强行提及）"
        )
    return "\n\n".join(parts) if parts else None


async def _recent_group_context_hook(ctx: HookContext) -> Optional[str]:
    if ctx.is_private:
        return None
    base_recent = _format_recent_group_context(
        ctx.group_id,
        limit=6,
        trigger_msg_id=extract_event_message_id(ctx.event),
        reply_to_msg_id=extract_reply_message_id(ctx.event),
    )
    frame = await _ensure_semantic_frame(ctx, recent_context=base_recent)
    is_meta_question = bool(getattr(frame, "meta_question", False))

    recent_block = _format_recent_group_context(
        ctx.group_id,
        limit=8 if is_meta_question else 6,
        trigger_msg_id=extract_event_message_id(ctx.event),
        reply_to_msg_id=extract_reply_message_id(ctx.event),
    )
    if not recent_block:
        return None

    block = (
        "## 最近群聊原始上下文\n"
        f"{recent_block}\n"
        "理解规则：\n"
        "- 优先根据回复关系、@提及和“当前消息”标记判断谁在和谁说话。\n"
        "- 先分清最近几句里哪些是群成员自然发言，哪些是机器人播报、插件输出或系统口吻消息。\n"
        "- 如果上一条明显是机器人播报，不要自动脑补成某个群成员本人在认真表达观点。"
    )
    if is_meta_question:
        block += (
            "\n- 当用户只说“什么意思 / 何意味 / 什么情况 / 为什么 / 谁发的 / 怎么成机器人了”这类短句时，"
            "优先判断他是在问上一条消息的来源、触发原因、说话对象或上下文，不一定是在问正文内容本身。"
        )
    return block


async def _group_relationship_hook(ctx: HookContext) -> Optional[str]:
    if ctx.is_private:
        return None
    recent = build_group_context_window(
        ctx.group_id,
        limit=8,
        include_message_ids=[extract_reply_message_id(ctx.event)],
    )
    summary = summarize_group_relationships(
        recent,
        trigger_msg_id=extract_event_message_id(ctx.event),
        trigger_user_id=ctx.user_id,
        bot_self_id=str(getattr(ctx.bot, "self_id", "") or ""),
    )
    return summary or None


async def _web_search_hook(ctx: HookContext) -> Optional[str]:
    if ctx.disable_network_hooks:
        return None
    if not bool(
        getattr(
            ctx.plugin_config,
            "personification_tool_web_search_enabled",
            getattr(ctx.plugin_config, "personification_web_search", False),
        )
    ):
        return None
    if ctx.is_random_chat:
        return None
    return (
        "当对方明确在问实时信息、新闻、版本变动、近期事件、真假求证，或明确在找资料、壁纸、原画、设定图、官网、下载页、图片合集、教程、资源链接时，可以考虑调用联网或资源工具；"
        "如果用户这句话很短、主语省略，但当前群里最近的话题很明确，可以把“最新一句 + 群里近期话题”一起理解后再查。"
        "闲聊时不要主动表现出查资料的感觉。"
    )


async def _grounding_hook(ctx: HookContext) -> Optional[str]:
    if ctx.disable_network_hooks:
        return None
    message_text = (ctx.message_text or ctx.message_content or "").strip()
    if ctx.is_random_chat and len(message_text) < 18:
        return None
    topic_hint = ""
    if not ctx.is_private:
        topic_hint = get_group_topic_summary(ctx.group_id)
    grounding_context = await ctx.runtime.build_grounding_context(
        message_text,
        topic_hint,
    )
    if not grounding_context:
        return None
    return (
        "## 联网事实校验（自动注入）\n"
        f"{grounding_context}\n"
        "回答时优先使用该事实，禁止无依据脑补。"
    )


async def _domain_focus_hook(ctx: HookContext) -> Optional[str]:
    frame = getattr(ctx, "semantic_frame", None)
    if frame is None:
        frame = await _ensure_semantic_frame(ctx)
    if not _is_domain_sensitive_frame(frame):
        return None
    return (
        "## 话题理解约束\n"
        "- 当前话题更偏向热点、游戏、动漫或版本内容，先准确理解对方具体在问什么，再回答。\n"
        "- 优先回应最新一句里的具体对象、角色、版本、剧情点或玩法问题，不要自顾自换成泛泛而谈。\n"
        "- 如果你不确定作品归属、版本信息或剧情细节，先基于已知上下文谨慎回答；需要时优先联网或工具核实。\n"
        "- 群聊里尽量像熟人顺口接一句，不要写成长分析，不要突然变成教程腔。"
    )


async def _anti_loop_hook(ctx: HookContext) -> Optional[str]:
    if not ctx.is_private:
        return None
    anti_loop_hint = ctx.session.build_private_anti_loop_hint(ctx.session_messages)
    return anti_loop_hint or None


async def _group_anti_loop_hook(ctx: HookContext) -> Optional[str]:
    if ctx.is_private:
        return None
    recent = [msg for msg in ctx.session_messages[-18:] if isinstance(msg, dict)]
    if not recent:
        return None

    user_keys = [
        _normalized_topic_key(msg.get("content", ""))
        for msg in recent
        if msg.get("role") == "user"
    ]
    assistant_keys = [
        _normalized_topic_key(msg.get("content", ""))
        for msg in recent
        if msg.get("role") == "assistant"
    ]
    user_keys = [key for key in user_keys if key]
    assistant_keys = [key for key in assistant_keys if key]
    if not user_keys or not assistant_keys:
        return None

    latest_user = user_keys[-1]
    repeated_user_topic = sum(1 for key in user_keys[-4:-1] if key == latest_user) >= 1
    repeated_assistant_topic = len(assistant_keys) >= 2 and assistant_keys[-1] == assistant_keys[-2]
    if not repeated_user_topic and not repeated_assistant_topic:
        return None

    return (
        "## Group Anti-loop Guard\n"
        "- 群聊里不要长期抓着同一个点反复展开。\n"
        "- 如果同一话题你已经接过两轮，而对方没有明确追问新信息，这一轮只保留一句短反应，或者直接 [SILENCE]。\n"
        "- 优先跟随最新一句的重心，不要继续延伸你上一轮自己的话。"
    )


async def _repeat_cluster_hook(ctx: HookContext) -> Optional[str]:
    if ctx.is_private or not ctx.repeat_clusters:
        return None
    lines = ["## 当前批次里的复读/接龙线索"]
    for cluster in ctx.repeat_clusters[:3]:
        text = str(cluster.get("text", "") or "").strip()
        count = int(cluster.get("count", 0) or 0)
        speakers = ", ".join(str(item or "") for item in (cluster.get("speakers") or [])[:4])
        if not text or count <= 0:
            continue
        lines.append(f"- 原句：{text}")
        lines.append(f"  次数：{count}；参与者：{speakers or '未知'}")
    if len(lines) == 1:
        return None
    lines.append("理解规则：先把它当成群友在复读、接龙或玩同一个梗，优先顺着气氛接，不要先解释笑点。")
    return "\n".join(lines)


async def _group_idle_hook(ctx: HookContext) -> Optional[str]:
    if not ctx.is_group_idle_active or not ctx.is_random_chat:
        return None

    topic_hint = ctx.group_idle_topic or "你刚刚主动起的话头"
    if ctx.is_yaml_mode:
        ctx.trigger_reason = (
            "你刚刚在群里主动说过话，现在处于短暂活跃期。"
            f"刚才的话头是：{topic_hint}。"
            f"发言者是 {ctx.user_name}({ctx.user_id})，这条消息虽然未必直接对你说，"
            "但如果是在接你的话茬、顺着刚才的话题延伸，或你自然有一句想接，可以更积极地回复。"
            "只有在明显无关或没必要接话时，才输出 [SILENCE]。"
        )
        return None

    if ctx.has_image_input and not ctx.message_content:
        ctx.message_content = (
            "[提示：你刚刚在群里主动起了个头，当前处于短暂活跃期。"
            f"刚才的话题：{topic_hint}。"
            f"现在你观察到群里 {ctx.user_name} 发送了一张图片，若是在接前面的话茬，可以更自然地评价一下；"
            "若明显无关，回复 [SILENCE]]"
        )
        return None

    if ctx.message_content:
        ctx.message_content = (
            "[提示：你刚刚在群里起过这个话头。"
            f"刚才的话题：{topic_hint}。"
            f"{ctx.user_name} 现在在接着说：{ctx.message_content}。"
            "如果明显是在顺着这个话题聊，就像普通群员一样自然接一句；若无关，就回复 [SILENCE]]"
        )
    return None


async def _friend_request_hook(ctx: HookContext) -> Optional[str]:
    if (
        ctx.is_private
        or ctx.is_random_chat
        or not ctx.persona.sign_in_available
        or not getattr(ctx.plugin_config, "personification_friend_request_enabled", False)
        or not getattr(ctx.plugin_config, "personification_agent_enabled", True)
        or not getattr(ctx.runtime, "tool_registry", None)
        or not getattr(ctx.runtime, "agent_tool_caller", None)
    ):
        return None

    friend_ids = await _get_cached_friend_ids(ctx.bot, ctx.runtime.logger)
    if ctx.user_id in friend_ids:
        return None

    user_data = ctx.persona.get_user_data(ctx.user_id)
    if user_data.get("is_perm_blacklisted"):
        return None

    fav = float(user_data.get("favorability", 0.0) or 0.0)
    min_fav = float(getattr(ctx.plugin_config, "personification_friend_request_min_fav", 85.0))
    if fav < min_fav:
        return None

    gate_ok, _gate_reason = check_friend_request_gate(
        plugin_config=ctx.plugin_config,
        user_id=ctx.user_id,
    )
    if not gate_ok:
        return None

    interaction_count = _count_user_interactions(ctx.messages, ctx.user_id)
    if interaction_count < 3:
        return None

    hint = (
        f"[系统提示，对用户不可见] 你和 {ctx.user_id} 在群 {ctx.group_id} 已经聊了 {interaction_count} 轮，"
        f"对方当前好感度是 {fav:.1f}，而且对方现在还不是你的好友。"
        "只有当你本轮本来也准备正常回复，并且真的觉得和对方很投缘、想在群外继续认识时，"
        f"才可以调用 send_friend_request 发起好友申请，附言用你自己的口吻写，并把 interaction_count 填成 {interaction_count}。"
        "如果你觉得还不够熟，或者时机不对，就不要申请。"
    )
    ctx.messages.append({"role": "system", "content": hint})
    return None


def register_all_builtin_hooks() -> None:
    global _REGISTERED
    if _REGISTERED:
        return

    register_prompt_hook("group_idle_active", _group_idle_hook, priority=45, phase="preprocess")
    register_prompt_hook("schedule", _schedule_hook, priority=10, phase="system_prelude")
    register_prompt_hook("anti_loop", _anti_loop_hook, priority=40, phase="system_context")
    register_prompt_hook("group_anti_loop", _group_anti_loop_hook, priority=41, phase="system_context")
    register_prompt_hook("user_persona", _user_persona_hook, priority=20, phase="system_context")
    register_prompt_hook("group_style", _group_style_hook, priority=25, phase="system_context")
    register_prompt_hook("recent_group_context", _recent_group_context_hook, priority=26, phase="system_context")
    register_prompt_hook("group_relationship", _group_relationship_hook, priority=27, phase="system_context")
    register_prompt_hook("repeat_cluster", _repeat_cluster_hook, priority=28, phase="system_context")
    register_prompt_hook("domain_focus", _domain_focus_hook, priority=29, phase="system_context")
    register_prompt_hook("web_search", _web_search_hook, priority=30, phase="system_context")
    register_prompt_hook("grounding", _grounding_hook, priority=35, phase="system_postlude")
    register_prompt_hook("friend_request", _friend_request_hook, priority=50, phase="message")
    _REGISTERED = True
