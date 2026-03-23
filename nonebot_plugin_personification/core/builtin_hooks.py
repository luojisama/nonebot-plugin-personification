from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..skills.friend_request_tool import check_friend_request_gate
from ..utils import get_group_topic_summary
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
    if schedule_active:
        return (
            "## 当前绝对时空（强制遵循）\n"
            f"- 当前时间：{ctx.current_time_str}\n"
            f"{ctx.runtime.get_schedule_prompt_injection()}"
        )

    override = ctx.runtime.schedule_disabled_override_prompt()
    parts = []
    if override:
        parts.append(override)
    parts.append(
        "## 当前时间信息（非作息约束）\n"
        f"- 当前时间：{ctx.current_time_str}"
    )
    return "\n\n".join(parts)


async def _user_persona_hook(ctx: HookContext) -> Optional[str]:
    persona_store = getattr(ctx.runtime, "persona_store", None)
    if not persona_store:
        return None
    user_persona = persona_store.get_persona_text(ctx.user_id)
    if not user_persona:
        return None
    return (
        "## 对方的用户画像（由分析插件提供）\n"
        "以下是对该用户的专业分析，请你根据这些特征（如职业、性格、兴趣）来调整你的语气和话题侧重点：\n"
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


async def _web_search_hook(ctx: HookContext) -> Optional[str]:
    if not getattr(ctx.plugin_config, "personification_web_search", False):
        return None
    return "你现在拥有联网搜索能力，可以获取最新的实时信息、新闻和知识来回答用户。"


async def _grounding_hook(ctx: HookContext) -> Optional[str]:
    grounding_context = await ctx.runtime.build_grounding_context(
        ctx.message_text or ctx.message_content
    )
    if not grounding_context:
        return None
    return (
        "## 联网事实校验（自动注入）\n"
        f"{grounding_context}\n"
        "回答时优先使用该事实，禁止无依据脑补。"
    )


async def _anti_loop_hook(ctx: HookContext) -> Optional[str]:
    if not ctx.is_private:
        return None
    anti_loop_hint = ctx.session.build_private_anti_loop_hint(ctx.session_messages)
    return anti_loop_hint or None


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
            "[提示：当前为【随机插话模式】。你刚刚在群里主动说过一句，当前仍处于短暂活跃期。"
            f"刚才的话题：{topic_hint}。"
            f"群员 {ctx.user_name} 现在接着聊：{ctx.message_content}。"
            "如果是在顺着你的话题聊，或你自然能接上一句，可以回复；若明显无关，请回复 [SILENCE]]"
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
    register_prompt_hook("user_persona", _user_persona_hook, priority=20, phase="system_context")
    register_prompt_hook("group_style", _group_style_hook, priority=25, phase="system_context")
    register_prompt_hook("web_search", _web_search_hook, priority=30, phase="system_context")
    register_prompt_hook("grounding", _grounding_hook, priority=35, phase="system_postlude")
    register_prompt_hook("friend_request", _friend_request_hook, priority=50, phase="message")
    _REGISTERED = True
