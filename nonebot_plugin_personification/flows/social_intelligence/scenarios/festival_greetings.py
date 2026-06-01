"""节日祝福 scenario。

每天指定时点（默认 9 点）扫一次：
- 命中内置公历节日表 → 给所有候选用户发节日祝福
- 命中某用户生日（从 persona 文本抽 "生日 = MM-DD" 模式）→ 给该用户发生日祝福

不引入农历依赖（lunardate 等），保持纯公历，避免环境依赖问题。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..framework import SocialContext
from ..gate import gate_should_send
from ..quota import is_quota_exceeded, mark_sent

_SCENARIO = "festival_greeting"

# (month, day, name) — 公历常见节日
_FESTIVAL_TABLE: tuple[tuple[int, int, str], ...] = (
    (1, 1, "元旦"),
    (2, 14, "情人节"),
    (3, 8, "妇女节"),
    (3, 12, "植树节"),
    (4, 1, "愚人节"),
    (5, 1, "劳动节"),
    (5, 4, "青年节"),
    (6, 1, "儿童节"),
    (9, 10, "教师节"),
    (10, 1, "国庆节"),
    (10, 31, "万圣节"),
    (12, 24, "平安夜"),
    (12, 25, "圣诞节"),
    (12, 31, "跨年夜"),
)

# 从 persona 文本里抽生日：兼容"生日：3月8日""生日是 03-08""出生于 1995-03-08"等
_BIRTHDAY_PATTERNS = (
    re.compile(r"生日[:：是为]?\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})"),
    re.compile(r"出生于\s*\d{4}[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"birthday[:：]?\s*(\d{1,2})[-/.](\d{1,2})", re.IGNORECASE),
)

_GREETING_PROMPT = """你是一个性格自然的聊天 bot，今天是{occasion_label}，给一位朋友
发一句祝福。

[对方画像摘要]
{persona_snippet}

[要求]
- 15-40 字，口语化，像突然想起来发的，不像群发模板
- 不要"祝...""愿..."这种官方开头，可以用自己的话表达祝福
- 不要"哈哈""嘿嘿"等廉价表情；不要 emoji 堆砌
- 如果是生日，可以提一句对方画像里相关的小细节让祝福贴心
- 直接输出消息，不加引号
"""


async def festival_greetings_handler(ctx: SocialContext) -> None:
    if not bool(getattr(ctx.plugin_config, "personification_social_intelligence_enabled", False)):
        return
    if not bool(getattr(ctx.plugin_config, "personification_social_festival_enabled", True)):
        return

    now = _get_now(ctx)
    today_festival = _today_festival(now)
    candidates = _list_candidates(ctx)
    birthday_users = _select_birthday_users(candidates, now)

    bot = _get_first_bot(ctx)
    if bot is None:
        ctx.logger.warning("[social/festival] no bot online, skip")
        return

    daily_quota = max(
        1, int(getattr(ctx.plugin_config, "personification_social_daily_quota_per_user", 2) or 2)
    )
    cooldown = max(
        0, int(getattr(ctx.plugin_config, "personification_social_festival_cooldown_seconds", 23 * 3600) or 0)
    )
    gate_enabled = bool(getattr(ctx.plugin_config, "personification_social_gate_enabled", True))
    max_recipients = max(
        1, int(getattr(ctx.plugin_config, "personification_social_festival_max_recipients", 20) or 20)
    )

    # 生日祝福先发（最贴心，应该优先消耗 quota）
    sent_birthday, sent_festival = 0, 0
    for uid, snippet in birthday_users:
        if await _try_send(
            ctx, bot, uid, snippet, occasion_label="你的生日",
            scenario=_SCENARIO, daily_quota=daily_quota, cooldown=cooldown,
            gate_enabled=gate_enabled,
        ):
            sent_birthday += 1

    if today_festival:
        for uid, snippet in candidates[:max_recipients]:
            if uid in {b[0] for b in birthday_users}:
                continue  # 生日已发过，不重复打扰
            if await _try_send(
                ctx, bot, uid, snippet, occasion_label=today_festival,
                scenario=_SCENARIO, daily_quota=daily_quota, cooldown=cooldown,
                gate_enabled=gate_enabled,
            ):
                sent_festival += 1
    ctx.logger.info(
        f"[social/festival] done; birthday={sent_birthday} festival={sent_festival} "
        f"today={today_festival or '<无节日>'}"
    )


def _today_festival(now: datetime) -> str | None:
    for month, day, name in _FESTIVAL_TABLE:
        if now.month == month and now.day == day:
            return name
    return None


def _list_candidates(ctx: SocialContext) -> list[tuple[str, str]]:
    if not ctx.persona_store:
        return []
    try:
        profiles = ctx.persona_store.list_core_profiles() or []
    except Exception:
        return []
    profiles = sorted(
        profiles, key=lambda p: float(p.get("updated_at", 0) or 0), reverse=True
    )[:50]
    out: list[tuple[str, str]] = []
    for p in profiles:
        uid = str(p.get("user_id", "") or "").strip()
        if not uid:
            continue
        snippet = str(p.get("profile_text", "") or "")[:240]
        out.append((uid, snippet))
    return out


def _select_birthday_users(
    candidates: list[tuple[str, str]], now: datetime
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for uid, snippet in candidates:
        birthday = _extract_birthday(snippet)
        if birthday and birthday == (now.month, now.day):
            out.append((uid, snippet))
    return out


def _extract_birthday(text: str) -> tuple[int, int] | None:
    if not text:
        return None
    for pat in _BIRTHDAY_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            month = int(m.group(1))
            day = int(m.group(2))
        except (TypeError, ValueError):
            continue
        if 1 <= month <= 12 and 1 <= day <= 31:
            return (month, day)
    return None


async def _try_send(
    ctx: SocialContext,
    bot: Any,
    uid: str,
    snippet: str,
    *,
    occasion_label: str,
    scenario: str,
    daily_quota: int,
    cooldown: int,
    gate_enabled: bool,
) -> bool:
    if is_quota_exceeded(
        uid, scenario=scenario, daily_quota_per_user=daily_quota, cooldown_seconds=cooldown
    ):
        return False
    persona_max = max(
        20, int(getattr(ctx.plugin_config, "personification_persona_snippet_max_chars", 150) or 150)
    )
    draft = await _generate(ctx, snippet[:persona_max], occasion_label)
    if not draft:
        return False
    final_text = draft
    if gate_enabled:
        allow, rewritten, reason = await gate_should_send(
            tool_caller=ctx.tool_caller,
            logger=ctx.logger,
            scenario=scenario,
            user_id=uid,
            draft=draft,
            persona_snippet=snippet[:persona_max],
            now_str=_now_str(ctx),
        )
        if not allow:
            ctx.logger.info(f"[social/festival] gate denied uid={uid}: {reason}")
            return False
        if rewritten:
            final_text = rewritten
    try:
        await bot.send_private_msg(user_id=int(uid), message=final_text)
    except Exception as exc:
        ctx.logger.warning(f"[social/festival] send {uid} failed: {exc}")
        return False
    mark_sent(uid, scenario=scenario)
    ctx.logger.info(f"[social/festival] sent uid={uid} occasion={occasion_label}: {final_text[:30]}")
    return True


async def _generate(ctx: SocialContext, snippet: str, occasion_label: str) -> str:
    if not ctx.tool_caller:
        return f"{occasion_label}快乐～"
    prompt = _GREETING_PROMPT.format(occasion_label=occasion_label, persona_snippet=snippet or "<无画像>")
    try:
        response = await ctx.tool_caller.chat_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            use_builtin_search=False,
        )
    except Exception as exc:
        ctx.logger.debug(f"[social/festival] generate failed: {exc}")
        return f"{occasion_label}快乐～"
    text = str(getattr(response, "content", "") or "").strip().strip('"').strip("'")
    return text[:120] if text else f"{occasion_label}快乐～"


def _get_first_bot(ctx: SocialContext) -> Any | None:
    try:
        bots = ctx.get_bots() or {}
    except Exception:
        return None
    return next(iter(bots.values()), None) if bots else None


def _get_now(ctx: SocialContext) -> datetime:
    try:
        return ctx.get_now()
    except Exception:
        return datetime.now()


def _now_str(ctx: SocialContext) -> str:
    try:
        return ctx.get_now().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


__all__ = ["festival_greetings_handler"]
