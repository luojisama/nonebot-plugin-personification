"""定时新闻推送 scenario。

每天指定时点（默认 9 点）触发，调 news skillpack 拿原始日报/AI 资讯/
历史上的今天，用 lite tool_caller 包装成更自然的口语化推送，再发送给
配置的用户与群。

与 greetings 共享 quota / gate / 发送基础设施。quota key：用户用
user_id 字面值，群用 "group:{gid}" 命名空间，互不干扰。
"""
from __future__ import annotations

from typing import Any

from ..framework import SocialContext
from ..gate import gate_should_send
from ..quota import is_quota_exceeded, mark_sent

_SCENARIO = "news_push"

_PACK_PROMPT = """你是一个性格自然的聊天 bot，要把今天的{source_label}用你自己的话
转述给一位朋友/群友，不要列点、不要工整结尾，像随手分享，30-90 字。

[原始内容]
{raw_news}

[要求]
- 选最有意思的 1-3 条转述，不要全说
- 用你自己的语气，不像新闻播报
- 不要"今日早报""根据..."这种官方腔
- 不要开头加"哇""嗯""哎呀"
- 直接输出消息内容，不要引号
"""

_SOURCE_LABELS = {
    "daily": "早报",
    "ai": "AI 资讯",
    "history": "历史上的今天",
}


async def news_push_handler(ctx: SocialContext) -> None:
    if not _social_enabled(ctx.plugin_config):
        return
    if not bool(getattr(ctx.plugin_config, "personification_social_news_enabled", False)):
        ctx.logger.info("[social/news] disabled, skip")
        return
    source = str(getattr(ctx.plugin_config, "personification_social_news_source", "daily") or "daily").strip().lower()
    if source not in _SOURCE_LABELS:
        source = "daily"
    raw_news = await _fetch_raw_news(ctx, source)
    if not raw_news:
        ctx.logger.warning(f"[social/news] no raw news for source={source}, skip")
        return
    draft = await _pack_news(ctx, source, raw_news)
    if not draft:
        ctx.logger.warning("[social/news] LLM pack returned empty, skip")
        return

    users = _string_list(getattr(ctx.plugin_config, "personification_social_news_users", []))
    groups = _string_list(getattr(ctx.plugin_config, "personification_social_news_groups", []))
    if not users and not groups:
        ctx.logger.info("[social/news] no targets configured, skip")
        return

    bot = _get_first_bot(ctx)
    if bot is None:
        ctx.logger.warning("[social/news] no bot online, skip")
        return

    daily_quota = max(
        1,
        int(getattr(ctx.plugin_config, "personification_social_daily_quota_per_user", 2) or 2),
    )
    cooldown = max(
        0,
        int(getattr(ctx.plugin_config, "personification_social_news_cooldown_seconds", 20 * 3600) or 0),
    )
    gate_enabled = bool(getattr(ctx.plugin_config, "personification_social_gate_enabled", True))

    sent_user, sent_group = 0, 0
    for uid in users:
        if is_quota_exceeded(uid, scenario=_SCENARIO, daily_quota_per_user=daily_quota, cooldown_seconds=cooldown):
            continue
        final_text = await _gate_or_pass(ctx, uid, draft, gate_enabled)
        if final_text is None:
            continue
        try:
            await bot.send_private_msg(user_id=int(uid), message=final_text)
        except Exception as exc:
            ctx.logger.warning(f"[social/news] send user {uid} failed: {exc}")
            continue
        mark_sent(uid, scenario=_SCENARIO)
        sent_user += 1
        ctx.logger.info(f"[social/news] sent to user {uid}: {final_text[:30]}")

    for gid in groups:
        # 群用独立 quota namespace 避免和 user_id 撞
        gid_key = f"group:{gid}"
        if is_quota_exceeded(
            gid_key, scenario=_SCENARIO, daily_quota_per_user=daily_quota, cooldown_seconds=cooldown
        ):
            continue
        final_text = await _gate_or_pass(ctx, gid_key, draft, gate_enabled)
        if final_text is None:
            continue
        try:
            await bot.send_group_msg(group_id=int(gid), message=final_text)
        except Exception as exc:
            ctx.logger.warning(f"[social/news] send group {gid} failed: {exc}")
            continue
        mark_sent(gid_key, scenario=_SCENARIO)
        sent_group += 1
        ctx.logger.info(f"[social/news] sent to group {gid}: {final_text[:30]}")

    ctx.logger.info(f"[social/news] done; users={sent_user}/{len(users)} groups={sent_group}/{len(groups)}")


def _social_enabled(plugin_config: Any) -> bool:
    return bool(getattr(plugin_config, "personification_social_intelligence_enabled", False))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _get_first_bot(ctx: SocialContext) -> Any | None:
    try:
        bots = ctx.get_bots() or {}
    except Exception:
        return None
    return next(iter(bots.values()), None) if bots else None


async def _fetch_raw_news(ctx: SocialContext, source: str) -> str:
    """调 news skillpack 的工具 handler 拿原文。"""
    try:
        from ....skills.skillpacks.news.scripts.impl import (
            build_ai_news_tool,
            build_daily_news_tool,
            build_history_today_tool,
        )
    except Exception as exc:
        ctx.logger.warning(f"[social/news] news skillpack import failed: {exc}")
        return ""

    base = str(
        getattr(ctx.plugin_config, "personification_60s_api_base", "https://60s.viki.moe") or ""
    ).strip().rstrip("/") or "https://60s.viki.moe"
    local_base = str(
        getattr(ctx.plugin_config, "personification_60s_local_api_base", "http://127.0.0.1:4399") or ""
    ).strip().rstrip("/") or "http://127.0.0.1:4399"

    builder = {
        "daily": build_daily_news_tool,
        "ai": build_ai_news_tool,
        "history": build_history_today_tool,
    }[source]
    try:
        tool = builder(base, ctx.logger, local_base)
        return str(await tool.handler() or "").strip()
    except Exception as exc:
        ctx.logger.warning(f"[social/news] fetch source={source} failed: {exc}")
        return ""


async def _pack_news(ctx: SocialContext, source: str, raw_news: str) -> str:
    label = _SOURCE_LABELS.get(source, "新闻")
    if not ctx.tool_caller:
        # 无 tool_caller 时退化为原文截断（避免完全发不出去）
        return raw_news[:200]
    prompt = _PACK_PROMPT.format(source_label=label, raw_news=raw_news[:1500])
    try:
        response = await ctx.tool_caller.chat_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            use_builtin_search=False,
        )
    except Exception as exc:
        ctx.logger.debug(f"[social/news] LLM pack failed: {exc}")
        return raw_news[:200]
    text = str(getattr(response, "content", "") or "").strip().strip('"').strip("'")
    return text[:300] if text else raw_news[:200]


async def _gate_or_pass(
    ctx: SocialContext, target_key: str, draft: str, gate_enabled: bool
) -> str | None:
    if not gate_enabled:
        return draft
    allow, rewritten, reason = await gate_should_send(
        tool_caller=ctx.tool_caller,
        logger=ctx.logger,
        scenario=_SCENARIO,
        user_id=target_key,
        draft=draft,
        persona_snippet="",
        now_str=_now_str(ctx),
    )
    if not allow:
        ctx.logger.info(f"[social/news] gate denied {target_key}: {reason}")
        return None
    return rewritten or draft


def _now_str(ctx: SocialContext) -> str:
    try:
        return ctx.get_now().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


__all__ = ["news_push_handler"]
