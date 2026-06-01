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
        logger.info("拟人插件：已成功注册 AI 空间说说定时任务 (周五 19:00)")
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


def register_proactive_qzone_job(
    *,
    scheduler: Any,
    proactive_qzone_job: Any,
    interval_minutes: int,
    logger: Any,
) -> None:
    safe_interval = max(30, int(interval_minutes))
    try:
        scheduler.add_job(
            proactive_qzone_job,
            "interval",
            minutes=safe_interval,
            id="personification_proactive_qzone",
            replace_existing=True,
        )
        logger.info(f"拟人插件：主动空间动态任务已注册，检测间隔 {safe_interval} 分钟")
    except Exception as e:
        logger.error(f"拟人插件：注册主动空间动态任务失败: {e}")


def register_qzone_social_scan_job(
    *,
    scheduler: Any,
    qzone_social_scan_job: Any,
    interval_minutes: int,
    logger: Any,
) -> None:
    safe_interval = max(30, int(interval_minutes))
    try:
        scheduler.add_job(
            qzone_social_scan_job,
            "interval",
            minutes=safe_interval,
            id="personification_qzone_social_scan",
            replace_existing=True,
        )
        logger.info(f"拟人插件：好友空间互动扫描任务已注册，检测间隔 {safe_interval} 分钟")
    except Exception as e:
        logger.error(f"拟人插件：注册好友空间互动扫描任务失败: {e}")


def register_qzone_inbound_poll_job(
    *,
    scheduler: Any,
    qzone_inbound_poll_job: Any,
    interval_minutes: int,
    logger: Any,
) -> None:
    safe_interval = max(1, int(interval_minutes))
    try:
        scheduler.add_job(
            qzone_inbound_poll_job,
            "interval",
            minutes=safe_interval,
            id="personification_qzone_inbound_poll",
            replace_existing=True,
        )
        logger.info(f"拟人插件：空间消息轮询任务已注册，检测间隔 {safe_interval} 分钟")
    except Exception as e:
        logger.error(f"拟人插件：注册空间消息轮询任务失败: {e}")


def register_qzone_permission_recheck_job(
    *,
    scheduler: Any,
    permission_recheck_job: Any,
    logger: Any,
) -> None:
    """每周重检 qzone_permission_blocked 中的 uid 是否重新可访问。"""
    try:
        scheduler.add_job(
            permission_recheck_job,
            "interval",
            days=7,
            id="personification_qzone_permission_recheck",
            replace_existing=True,
        )
        logger.info("拟人插件：空间权限重检任务已注册，间隔 7 天")
    except Exception as e:
        logger.error(f"拟人插件：注册空间权限重检任务失败: {e}")


def register_background_intelligence_job(
    *,
    scheduler: Any,
    maintenance_job: Any,
    logger: Any,
) -> None:
    try:
        scheduler.add_job(
            maintenance_job,
            "interval",
            minutes=15,
            id="personification_background_intelligence",
            replace_existing=True,
        )
        logger.info("拟人插件：后台智能维护任务已注册，间隔 15 分钟")
    except Exception as e:
        logger.error(f"拟人插件：注册后台智能维护任务失败: {e}")


def register_sticker_curator_job(
    *,
    scheduler: Any,
    curator_job: Any,
    interval_days: int,
    logger: Any,
) -> None:
    safe_days = max(1, int(interval_days or 3))
    try:
        scheduler.add_job(
            curator_job,
            "interval",
            days=safe_days,
            id="personification_sticker_curator",
            replace_existing=True,
        )
        logger.info(f"拟人插件：表情包馆长定时整理已注册，间隔 {safe_days} 天")
    except Exception as e:
        logger.error(f"拟人插件：注册表情包馆长任务失败: {e}")


def register_sticker_trash_cleanup_job(
    *,
    scheduler: Any,
    cleanup_job: Any,
    logger: Any,
) -> None:
    try:
        scheduler.add_job(
            cleanup_job,
            "cron",
            hour=4,
            minute=0,
            id="personification_sticker_trash_cleanup",
            replace_existing=True,
        )
        logger.info("拟人插件：表情包 trash 每日清理已注册，每天凌晨 4:00")
    except Exception as e:
        logger.error(f"拟人插件：注册表情包 trash 清理任务失败: {e}")