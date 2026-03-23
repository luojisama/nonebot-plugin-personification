from typing import Any


def register_weekly_diary_job(
    *,
    scheduler: Any,
    auto_post_diary: Any,
    logger: Any,
) -> None:
    try:
        scheduler.add_job(
            auto_post_diary,
            "cron",
            day_of_week="fri",
            hour=19,
            minute=0,
            id="ai_weekly_diary",
            replace_existing=True,
        )
        logger.info("拟人插件：已成功注册 AI 每周说说定时任务 (周五 19:00)")
    except Exception as e:
        logger.error(f"拟人插件：注册定时任务失败: {e}")


def register_proactive_messaging_job(
    *,
    scheduler: Any,
    proactive_job: Any,
    interval_minutes: int,
    logger: Any,
) -> None:
    safe_interval = max(5, int(interval_minutes))
    try:
        scheduler.add_job(
            proactive_job,
            "interval",
            minutes=safe_interval,
            id="personification_proactive_messaging",
            replace_existing=True,
        )
        logger.info(f"拟人插件：主动消息任务已注册，间隔 {safe_interval} 分钟")
    except Exception as e:
        logger.error(f"拟人插件：注册主动消息任务失败: {e}")


def register_daily_group_fav_report_job(
    *,
    scheduler: Any,
    daily_job: Any,
    logger: Any,
) -> None:
    try:
        scheduler.add_job(
            daily_job,
            "cron",
            hour=23,
            minute=59,
            id="personification_daily_fav_report",
            replace_existing=True,
        )
        logger.info("拟人插件：已成功注册每日群好感统计任务 (23:59)")
    except Exception as e:
        logger.error(f"拟人插件：注册每日群好感统计任务失败: {e}")


def register_group_idle_topic_job(
    *,
    scheduler: Any,
    group_idle_topic_job: Any,
    interval_minutes: int,
    logger: Any,
) -> None:
    safe_interval = max(5, int(interval_minutes))
    try:
        scheduler.add_job(
            group_idle_topic_job,
            "interval",
            minutes=safe_interval,
            id="personification_group_idle_topic",
            replace_existing=True,
        )
        logger.info(
            f"拟人插件：群聊空闲发话任务已注册，检测间隔 {safe_interval} 分钟"
        )
    except Exception as e:
        logger.error(f"拟人插件：注册群聊空闲发话任务失败: {e}")
