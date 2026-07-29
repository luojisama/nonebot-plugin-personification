"""早晚问候 scenario。

每天早 / 晚两个 cron 时点触发；handler 从 persona_store 拿最近活跃用户
（按 core profile 的 updated_at 排序）作为候选，给每位生成一段自然语气
的问候（优先走完整 Agent 包装而不是写死模板），再走 gate 二次决策
是否发送，最后调 bot.send_private_msg。

quota.py 管"每用户每天最多 N 条主动消息"+"同场景冷却"，避免骚扰。
"""
from __future__ import annotations

from typing import Any

from ..framework import SocialContext, dispatch_social_outbound, run_social_text_agent
from ..gate import gate_should_send
from ..quota import is_quota_exceeded, mark_sent
from ....core.visible_output import guard_visible_text

_SCENARIO_MORNING = "morning_greeting"
_SCENARIO_EVENING = "evening_greeting"

_GREETING_PROMPT = """延续你既定的人设，现在要主动给一位熟悉的朋友发"{time_label}"。

[对方画像摘要]
{persona_snippet}

[要求]
- 8-30 字，口语化，像突然想到发的一句话
- 不要"在吗""你好""最近怎么样"这类没内容的开场
- 不要工整结尾，可以省略主语
- 不要每次套同样模板；让对方画像里的细节决定语气
- 不要透露"这是定时任务""自动消息"

直接输出消息内容，不加引号、不加解释。
"""


async def morning_greetings_handler(ctx: SocialContext) -> None:
    await _run_greetings(ctx, time_of_day="morning")


async def evening_greetings_handler(ctx: SocialContext) -> None:
    await _run_greetings(ctx, time_of_day="evening")


async def _run_greetings(ctx: SocialContext, *, time_of_day: str) -> None:
    if not _social_enabled(ctx.plugin_config):
        return
    scenario = _SCENARIO_MORNING if time_of_day == "morning" else _SCENARIO_EVENING
    daily_quota = max(
        1,
        int(getattr(ctx.plugin_config, "personification_social_daily_quota_per_user", 2) or 2),
    )
    cooldown = max(
        0,
        int(getattr(ctx.plugin_config, "personification_social_greeting_cooldown_seconds", 18 * 3600) or 0),
    )
    max_recipients = max(
        1,
        int(getattr(ctx.plugin_config, "personification_social_greeting_max_recipients", 8) or 8),
    )

    candidates = _list_candidates(ctx, limit=max_recipients * 3)
    bot = _get_first_bot(ctx)
    if bot is None:
        ctx.logger.warning("[social/greetings] no bot online, skip")
        return

    persona_max = max(
        20,
        int(getattr(ctx.plugin_config, "personification_persona_snippet_max_chars", 150) or 150),
    )
    gate_enabled = bool(getattr(ctx.plugin_config, "personification_social_gate_enabled", True))
    sent = 0
    for user_id, persona_snippet in candidates:
        if sent >= max_recipients:
            break
        if is_quota_exceeded(
            user_id,
            scenario=scenario,
            daily_quota_per_user=daily_quota,
            cooldown_seconds=cooldown,
        ):
            continue
        draft = await _generate_greeting(ctx, persona_snippet[:persona_max], time_of_day)
        if not draft:
            continue
        if gate_enabled:
            allow, _rewritten, reason = await gate_should_send(
                tool_caller=ctx.tool_caller,
                plugin_config=ctx.plugin_config,
                tool_registry=ctx.tool_registry,
                agent_max_steps=ctx.agent_max_steps,
                logger=ctx.logger,
                scenario=scenario,
                user_id=user_id,
                draft=draft,
                persona_snippet=persona_snippet[:persona_max],
                now_str=_now_str(ctx),
            )
            if not allow:
                ctx.logger.info(f"[social/greetings] gate denied user={user_id}: {reason}")
                continue
        final_text = draft
        final_text = guard_visible_text(
            final_text, logger=ctx.logger, surface="social_greeting", allow_direct_media=False
        )
        if not final_text:
            continue
        try:
            confirmed = await dispatch_social_outbound(
                ctx,
                bot=bot,
                conversation_kind="private",
                conversation_id=user_id,
                surface="social_greeting",
                content=final_text,
                user_target=user_id,
            )
        except Exception as exc:
            ctx.logger.warning(f"[social/greetings] send to {user_id} failed: {exc}")
            continue
        if not confirmed:
            ctx.logger.warning(f"[social/greetings] send to {user_id} outcome unknown")
            continue
        mark_sent(user_id, scenario=scenario)
        sent += 1
        ctx.logger.info(f"[social/greetings] sent {time_of_day} to {user_id}: {final_text[:30]}")
    ctx.logger.info(f"[social/greetings] {time_of_day} done; sent={sent}/{max_recipients}")


def _social_enabled(plugin_config: Any) -> bool:
    return bool(getattr(plugin_config, "personification_social_intelligence_enabled", False))


def _list_candidates(ctx: SocialContext, *, limit: int) -> list[tuple[str, str]]:
    """拿候选用户：从 persona_store.list_core_profiles 按更新时间倒序。
    返回 [(user_id, persona_snippet), ...]。
    """
    if not ctx.persona_store:
        return []
    try:
        profiles = ctx.persona_store.list_core_profiles() or []
    except Exception as exc:
        ctx.logger.debug(f"[social/greetings] list_core_profiles failed: {exc}")
        return []
    profiles = sorted(
        profiles,
        key=lambda p: float(p.get("updated_at", 0) or 0),
        reverse=True,
    )[:limit]
    out: list[tuple[str, str]] = []
    for p in profiles:
        uid = str(p.get("user_id", "") or "").strip()
        if not uid:
            continue
        snippet = str(p.get("profile_text", "") or "")[:240]
        out.append((uid, snippet))
    return out


def _get_first_bot(ctx: SocialContext) -> Any | None:
    try:
        bots = ctx.get_bots() or {}
    except Exception:
        return None
    return next(iter(bots.values()), None) if bots else None


async def _generate_greeting(ctx: SocialContext, persona_snippet: str, time_of_day: str) -> str:
    label = "早安问候" if time_of_day == "morning" else "晚安问候"
    prompt = _GREETING_PROMPT.format(time_label=label, persona_snippet=persona_snippet or "<无画像>")
    messages = [{"role": "user", "content": prompt}]
    if not (
        getattr(ctx.plugin_config, "personification_agent_enabled", True)
        and ctx.tool_caller
        and ctx.tool_registry
    ):
        return ""
    try:
        text = await run_social_text_agent(
            ctx,
            messages=messages,
            trigger_reason=f"social_{time_of_day}_greeting",
            chat_intent_hint="social_greeting",
        )
    except Exception as exc:
        ctx.logger.debug(f"[social/greetings] Agent generate failed: {exc}")
        return ""
    text = text.strip('"').strip("'").strip("「」").strip()
    return text[:60] if text else ""


def _now_str(ctx: SocialContext) -> str:
    try:
        return ctx.get_now().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


__all__ = ["morning_greetings_handler", "evening_greetings_handler"]
