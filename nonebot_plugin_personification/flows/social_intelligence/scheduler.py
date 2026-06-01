"""把 social_intelligence 场景注册到 APScheduler。

调用方在 bot 启动时调 setup_social_intelligence_jobs；它会内部注册若干个
SocialTrigger，并按 schedule_kind / schedule_args 添加到 APScheduler。
"""
from __future__ import annotations

from typing import Any

from .framework import (
    SocialContext,
    SocialTrigger,
    list_social_triggers,
    register_social_trigger,
)
from .scenarios.greetings import (
    evening_greetings_handler,
    morning_greetings_handler,
)
from .scenarios.news_push import news_push_handler
from .scenarios.festival_greetings import festival_greetings_handler
from .scenarios.topic_followup import topic_followup_handler


def _register_builtin_scenarios(plugin_config: Any) -> None:
    morning_hour = int(getattr(plugin_config, "personification_social_morning_hour", 8) or 8)
    evening_hour = int(getattr(plugin_config, "personification_social_evening_hour", 22) or 22)

    register_social_trigger(
        SocialTrigger(
            name="morning_greeting",
            handler=morning_greetings_handler,
            schedule_kind="cron",
            schedule_args={"hour": morning_hour, "minute": 0},
            enabled=lambda cfg: bool(
                getattr(cfg, "personification_social_intelligence_enabled", False)
            ) and bool(
                getattr(cfg, "personification_social_morning_greeting_enabled", True)
            ),
        )
    )
    register_social_trigger(
        SocialTrigger(
            name="evening_greeting",
            handler=evening_greetings_handler,
            schedule_kind="cron",
            schedule_args={"hour": evening_hour, "minute": 0},
            enabled=lambda cfg: bool(
                getattr(cfg, "personification_social_intelligence_enabled", False)
            ) and bool(
                getattr(cfg, "personification_social_evening_greeting_enabled", True)
            ),
        )
    )

    news_hour = int(getattr(plugin_config, "personification_social_news_hour", 9) or 9)
    register_social_trigger(
        SocialTrigger(
            name="news_push",
            handler=news_push_handler,
            schedule_kind="cron",
            schedule_args={"hour": news_hour, "minute": 0},
            enabled=lambda cfg: bool(
                getattr(cfg, "personification_social_intelligence_enabled", False)
            ) and bool(
                getattr(cfg, "personification_social_news_enabled", False)
            ),
        )
    )

    topic_scan_minutes = max(
        15,
        int(getattr(plugin_config, "personification_social_topic_scan_interval_minutes", 60) or 60),
    )
    register_social_trigger(
        SocialTrigger(
            name="topic_followup",
            handler=topic_followup_handler,
            schedule_kind="interval",
            schedule_args={"minutes": topic_scan_minutes},
            enabled=lambda cfg: bool(
                getattr(cfg, "personification_social_intelligence_enabled", False)
            ) and bool(
                getattr(cfg, "personification_social_topic_followup_enabled", True)
            ),
        )
    )

    festival_hour = int(getattr(plugin_config, "personification_social_festival_hour", 9) or 9)
    register_social_trigger(
        SocialTrigger(
            name="festival_greeting",
            handler=festival_greetings_handler,
            schedule_kind="cron",
            schedule_args={"hour": festival_hour, "minute": 0},
            enabled=lambda cfg: bool(
                getattr(cfg, "personification_social_intelligence_enabled", False)
            ) and bool(
                getattr(cfg, "personification_social_festival_enabled", True)
            ),
        )
    )


def setup_social_intelligence_jobs(*, scheduler: Any, ctx: SocialContext) -> int:
    """注册全部 SocialTrigger 到 APScheduler。返回成功注册的数量。"""
    _register_builtin_scenarios(ctx.plugin_config)
    registered = 0
    for trigger in list_social_triggers():
        if trigger.schedule_kind == "event":
            # 事件触发器不走 scheduler，由具体 hook 调 handler。
            continue
        if not trigger.enabled(ctx.plugin_config):
            ctx.logger.info(f"[social/scheduler] trigger {trigger.name} disabled, skip")
            continue
        try:
            scheduler.add_job(
                _make_runner(trigger, ctx),
                trigger.schedule_kind,
                id=f"personification_social_{trigger.name}",
                replace_existing=True,
                **trigger.schedule_args,
            )
            registered += 1
            ctx.logger.info(
                f"[social/scheduler] registered {trigger.name} "
                f"({trigger.schedule_kind} {trigger.schedule_args})"
            )
        except Exception as exc:
            ctx.logger.error(f"[social/scheduler] register {trigger.name} failed: {exc}")
    return registered


def _make_runner(trigger: SocialTrigger, ctx: SocialContext):
    async def _runner() -> None:
        if not trigger.enabled(ctx.plugin_config):
            return
        try:
            await trigger.handler(ctx)
        except Exception as exc:
            ctx.logger.error(f"[social/{trigger.name}] handler crashed: {exc}")

    return _runner


__all__ = ["setup_social_intelligence_jobs"]
