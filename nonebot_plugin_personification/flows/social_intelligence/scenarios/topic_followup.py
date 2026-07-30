"""话题延续 scenario：cron 周期扫 pending_topics 表，给到了承诺时间附近
的话题触发 follow-up（"上次你说要去上海，怎么样了？"）。

抽取链路（消息 → pending_topics 表）在 builtin_hooks 的
_pending_topic_extract_hook 里完成（fire-and-forget asyncio task），
不在本模块。
"""
from __future__ import annotations

from typing import Any

from .. import pending_topics as pt
from ..framework import SocialContext, run_social_text_agent
from ..gate import gate_should_send
from ..quota import is_quota_exceeded, mark_sent

_SCENARIO = "topic_followup"

_FOLLOWUP_PROMPT = """你是一个性格自然的聊天 bot，主动给一位朋友发一条消息，
关心他/她之前提过的事情。

[对方原话]
{raw_quote}

[你之前理解到的事]
{topic}

[要求]
- 25-60 字，像突然想起来发的一句，不像查岗
- 不要复读对方原话；用自己的话引一句
- 末尾可以问一个温和的具体问题，但不要逼问
- 不要"在吗""你好"等没内容的开场
- 直接输出消息内容，不加引号
"""


async def topic_followup_handler(ctx: SocialContext) -> None:
    if not bool(getattr(ctx.plugin_config, "personification_social_intelligence_enabled", False)):
        return
    if not bool(getattr(ctx.plugin_config, "personification_social_topic_followup_enabled", True)):
        return

    window_hours = max(
        1, int(getattr(ctx.plugin_config, "personification_social_topic_followup_window_hours", 24) or 24)
    )
    window_seconds = window_hours * 3600.0
    due = pt.find_due_topics(window_seconds=window_seconds)
    if not due:
        ctx.logger.debug("[social/topic_followup] no due topics")
        return

    bot = _get_first_bot(ctx)
    if bot is None:
        ctx.logger.warning("[social/topic_followup] no bot online, skip")
        return

    daily_quota = max(
        1, int(getattr(ctx.plugin_config, "personification_social_daily_quota_per_user", 2) or 2)
    )
    cooldown = max(
        0, int(getattr(ctx.plugin_config, "personification_social_topic_followup_cooldown_seconds", 12 * 3600) or 0)
    )
    gate_enabled = bool(getattr(ctx.plugin_config, "personification_social_gate_enabled", True))

    sent = 0
    for item in due:
        uid = str(item.get("user_id", "") or "")
        if not uid:
            continue
        topic_id = str(item.get("topic_id", "") or "")
        if is_quota_exceeded(
            uid, scenario=_SCENARIO, daily_quota_per_user=daily_quota, cooldown_seconds=cooldown
        ):
            continue
        draft = await _generate_followup(ctx, item)
        if not draft:
            pt.mark_skipped(topic_id)
            continue
        final_text = draft
        if gate_enabled:
            allow, rewritten, reason = await gate_should_send(
                tool_caller=ctx.tool_caller,
                plugin_config=ctx.plugin_config,
                tool_registry=ctx.tool_registry,
                agent_max_steps=ctx.agent_max_steps,
                logger=ctx.logger,
                scenario=_SCENARIO,
                user_id=uid,
                draft=draft,
                persona_snippet=str(item.get("topic", "") or "")[:120],
                now_str=_now_str(ctx),
            )
            if not allow:
                ctx.logger.info(f"[social/topic_followup] gate denied uid={uid} tid={topic_id}: {reason}")
                pt.mark_skipped(topic_id)
                continue
            if rewritten:
                final_text = rewritten
        try:
            await bot.send_private_msg(user_id=int(uid), message=final_text)
        except Exception as exc:
            ctx.logger.warning(f"[social/topic_followup] send {uid} failed: {exc}")
            continue
        mark_sent(uid, scenario=_SCENARIO)
        pt.mark_followed_up(topic_id)
        sent += 1
        ctx.logger.info(f"[social/topic_followup] sent uid={uid} tid={topic_id}: {final_text[:30]}")
    ctx.logger.info(f"[social/topic_followup] done; sent={sent}/{len(due)}")


def _get_first_bot(ctx: SocialContext) -> Any | None:
    try:
        bots = ctx.get_bots() or {}
    except Exception:
        return None
    return next(iter(bots.values()), None) if bots else None


async def _generate_followup(ctx: SocialContext, topic_entry: dict) -> str:
    if not ctx.tool_caller:
        topic = str(topic_entry.get("topic", "") or "")
        return f"上次你说的{topic}怎么样了？" if topic else ""
    prompt = _FOLLOWUP_PROMPT.format(
        raw_quote=str(topic_entry.get("raw_quote", "") or "")[:200],
        topic=str(topic_entry.get("topic", "") or "")[:120],
    )
    messages = [{"role": "user", "content": prompt}]
    if ctx.tool_caller and ctx.tool_registry:
        try:
            text = await run_social_text_agent(
                ctx,
                messages=messages,
                trigger_reason="social_topic_followup",
                chat_intent_hint="social_topic_followup",
            )
        except Exception as exc:
            ctx.logger.debug(f"[social/topic_followup] Agent gen failed: {exc}")
            return ""
        text = text.strip('"').strip("'").strip("「」").strip()
        return text[:120] if text else ""
    try:
        response = await ctx.tool_caller.chat_with_tools(
            messages=messages,
            tools=[],
            use_builtin_search=False,
        )
    except Exception as exc:
        ctx.logger.debug(f"[social/topic_followup] gen failed: {exc}")
        return ""
    text = str(getattr(response, "content", "") or "").strip().strip('"').strip("'")
    return text[:120] if text else ""


def _now_str(ctx: SocialContext) -> str:
    try:
        return ctx.get_now().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


__all__ = ["topic_followup_handler"]
