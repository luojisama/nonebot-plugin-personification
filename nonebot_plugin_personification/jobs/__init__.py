from dataclasses import dataclass
from typing import Any, Dict

from .periodic_jobs import (
    run_auto_post_diary,
    run_daily_group_fav_report,
    run_proactive_qzone_post,
    run_qzone_inbound_poll,
    run_qzone_social_scan,
)
from .scheduler_registration import (
    register_background_intelligence_job,
    register_daily_group_fav_report_job,
    register_group_idle_topic_job,
    register_proactive_messaging_job,
    register_proactive_qzone_job,
    register_qzone_inbound_poll_job,
    register_qzone_permission_recheck_job,
    register_qzone_social_scan_job,
    register_sticker_curator_job,
    register_sticker_trash_cleanup_job,
    register_weekly_diary_job,
)
from .task_builders import (
    build_auto_post_diary_task,
    build_daily_group_fav_report_task,
    build_generate_ai_diary_task,
    build_group_idle_topic_task,
    build_maybe_generate_qzone_post_task,
    build_proactive_qzone_post_task,
    build_qzone_inbound_poll_task,
    build_qzone_social_scan_task,
)


@dataclass
class JobSetupDeps:
    plugin_config: Any
    sign_in_available: bool
    load_data: Any
    load_proactive_state: Any
    get_now: Any
    get_bots: Any
    superusers: set[str]
    logger: Any
    generate_ai_diary_flow: Any
    load_prompt: Any
    call_ai_api: Any
    qzone_publish_available: bool
    update_qzone_cookie: Any
    publish_qzone_shuo: Any
    maybe_generate_proactive_qzone_post_flow: Any
    check_proactive_messaging: Any
    proactive_interval_minutes: int
    proactive_enabled: bool = True
    check_group_idle_topic: Any = None
    group_idle_enabled: bool = True
    group_idle_check_interval_minutes: int = 15
    qzone_proactive_enabled: bool = False
    qzone_check_interval_minutes: int = 90
    qzone_daily_limit: int = 3
    qzone_probability: float = 0.35
    qzone_min_interval_hours: float = 6.0
    qzone_quiet_hour_start: int = 0
    qzone_quiet_hour_end: int = 7
    qzone_social_enabled: bool = False
    qzone_social_check_interval_minutes: int = 120
    qzone_social_scan_flow: Any = None
    qzone_social_service: Any = None
    qzone_inbound_enabled: bool = True
    qzone_inbound_check_interval_minutes: int = 3
    qzone_inbound_poll_flow: Any = None
    persona_store: Any = None
    vision_caller: Any = None
    agent_tool_caller: Any = None
    agent_data_dir: Any = None
    background_intelligence: Any = None


def setup_jobs(*, scheduler: Any, deps: JobSetupDeps) -> Dict[str, Any]:
    daily_group_fav_report = build_daily_group_fav_report_task(
        run_daily_group_fav_report=run_daily_group_fav_report,
        sign_in_available=deps.sign_in_available,
        load_data=deps.load_data,
        get_now=deps.get_now,
        get_bots=deps.get_bots,
        superusers=deps.superusers,
        logger=deps.logger,
    )
    register_daily_group_fav_report_job(
        scheduler=scheduler,
        daily_job=daily_group_fav_report,
        logger=deps.logger,
    )

    generate_ai_diary = build_generate_ai_diary_task(
        generate_ai_diary_flow=deps.generate_ai_diary_flow,
        load_prompt=deps.load_prompt,
        call_ai_api=deps.call_ai_api,
        logger=deps.logger,
        agent_tool_caller=deps.agent_tool_caller,
        agent_data_dir=deps.agent_data_dir,
    )
    auto_post_diary = None
    proactive_qzone_post = None
    qzone_social_scan = None
    qzone_inbound_poll = None
    if not deps.qzone_publish_available:
        deps.logger.info(
            "拟人插件：Qzone 说说功能未启用（personification_qzone_enabled=False），跳过定时注册"
        )
    if deps.qzone_publish_available:
        auto_post_diary = build_auto_post_diary_task(
            run_auto_post_diary=run_auto_post_diary,
            qzone_publish_available=deps.qzone_publish_available,
            get_bots=deps.get_bots,
            update_qzone_cookie=deps.update_qzone_cookie,
            generate_ai_diary=generate_ai_diary,
            publish_qzone_shuo=deps.publish_qzone_shuo,
            logger=deps.logger,
        )

        register_weekly_diary_job(
            scheduler=scheduler,
            auto_post_diary=auto_post_diary,
            logger=deps.logger,
        )
        if deps.qzone_proactive_enabled and deps.maybe_generate_proactive_qzone_post_flow is not None:
            maybe_generate_qzone_post = build_maybe_generate_qzone_post_task(
                maybe_generate_proactive_qzone_post_flow=deps.maybe_generate_proactive_qzone_post_flow,
                load_prompt=deps.load_prompt,
                call_ai_api=deps.call_ai_api,
                logger=deps.logger,
                agent_tool_caller=deps.agent_tool_caller,
                agent_data_dir=deps.agent_data_dir,
            )
            proactive_qzone_post = build_proactive_qzone_post_task(
                run_proactive_qzone_post=run_proactive_qzone_post,
                qzone_publish_available=deps.qzone_publish_available,
                qzone_proactive_enabled=deps.qzone_proactive_enabled,
                qzone_probability=deps.qzone_probability,
                qzone_daily_limit=deps.qzone_daily_limit,
                qzone_min_interval_hours=deps.qzone_min_interval_hours,
                get_bots=deps.get_bots,
                get_now=deps.get_now,
                update_qzone_cookie=deps.update_qzone_cookie,
                maybe_generate_qzone_post=maybe_generate_qzone_post,
                publish_qzone_shuo=deps.publish_qzone_shuo,
                logger=deps.logger,
                qzone_quiet_hour_start=getattr(deps, "qzone_quiet_hour_start", 0),
                qzone_quiet_hour_end=getattr(deps, "qzone_quiet_hour_end", 7),
            )
            register_proactive_qzone_job(
                scheduler=scheduler,
                proactive_qzone_job=proactive_qzone_post,
                interval_minutes=deps.qzone_check_interval_minutes,
                logger=deps.logger,
            )
        if deps.qzone_social_scan_flow is not None and deps.qzone_social_service is not None:
            async def _scan_qzone_social_feeds(
                bot: Any,
                target_user_id: str = "",
                allow_open_user: bool = False,
            ) -> dict[str, Any]:
                return await deps.qzone_social_scan_flow(
                    bot=bot,
                    plugin_config=getattr(deps, "plugin_config", None),
                    qzone_social_service=deps.qzone_social_service,
                    load_prompt=deps.load_prompt,
                    call_ai_api=deps.call_ai_api,
                    load_proactive_state=getattr(deps, "load_proactive_state", lambda: {}),
                    get_now=deps.get_now,
                    logger=deps.logger,
                    persona_store=deps.persona_store,
                    vision_caller=deps.vision_caller,
                    agent_data_dir=deps.agent_data_dir,
                    target_user_id=target_user_id,
                    allow_open_user=allow_open_user,
                )

            qzone_social_scan = build_qzone_social_scan_task(
                run_qzone_social_scan=run_qzone_social_scan,
                qzone_publish_available=deps.qzone_publish_available,
                qzone_social_enabled=deps.qzone_social_enabled,
                get_bots=deps.get_bots,
                update_qzone_cookie=deps.update_qzone_cookie,
                scan_qzone_social_feeds=_scan_qzone_social_feeds,
                logger=deps.logger,
            )
            if deps.qzone_social_enabled:
                register_qzone_social_scan_job(
                    scheduler=scheduler,
                    qzone_social_scan_job=qzone_social_scan,
                    interval_minutes=deps.qzone_social_check_interval_minutes,
                    logger=deps.logger,
                )
        if deps.qzone_inbound_poll_flow is not None and deps.qzone_social_service is not None:
            async def _poll_qzone_inbound_messages(bot: Any) -> dict[str, Any]:
                return await deps.qzone_inbound_poll_flow(
                    bot=bot,
                    plugin_config=getattr(deps, "plugin_config", None),
                    qzone_social_service=deps.qzone_social_service,
                    load_prompt=deps.load_prompt,
                    call_ai_api=deps.call_ai_api,
                    load_proactive_state=getattr(deps, "load_proactive_state", lambda: {}),
                    get_now=deps.get_now,
                    logger=deps.logger,
                    persona_store=deps.persona_store,
                    agent_data_dir=deps.agent_data_dir,
                )

            qzone_inbound_poll = build_qzone_inbound_poll_task(
                run_qzone_inbound_poll=run_qzone_inbound_poll,
                qzone_publish_available=deps.qzone_publish_available,
                qzone_inbound_enabled=deps.qzone_inbound_enabled,
                get_bots=deps.get_bots,
                update_qzone_cookie=deps.update_qzone_cookie,
                poll_qzone_inbound_messages=_poll_qzone_inbound_messages,
                logger=deps.logger,
            )
            if deps.qzone_inbound_enabled:
                register_qzone_inbound_poll_job(
                    scheduler=scheduler,
                    qzone_inbound_poll_job=qzone_inbound_poll,
                    interval_minutes=deps.qzone_inbound_check_interval_minutes,
                    logger=deps.logger,
                )

        # 每周重检 qzone 权限黑名单
        if deps.qzone_social_service is not None:
            async def _recheck_qzone_permissions() -> None:
                from ..flows.qzone_social_flow import recheck_qzone_permission_blocked_users
                bots = deps.get_bots() if callable(deps.get_bots) else []
                for bot in (bots or []):
                    try:
                        await recheck_qzone_permission_blocked_users(
                            bot=bot,
                            qzone_social_service=deps.qzone_social_service,
                            logger=deps.logger,
                        )
                    except Exception as exc:
                        deps.logger.warning(f"[qzone_permission_recheck] bot {getattr(bot, 'self_id', '?')} failed: {exc}")

            register_qzone_permission_recheck_job(
                scheduler=scheduler,
                permission_recheck_job=_recheck_qzone_permissions,
                logger=deps.logger,
            )
    if getattr(deps, "proactive_enabled", True):
        register_proactive_messaging_job(
            scheduler=scheduler,
            proactive_job=deps.check_proactive_messaging,
            interval_minutes=deps.proactive_interval_minutes,
            logger=deps.logger,
        )
    if getattr(deps, "group_idle_enabled", True) and deps.check_group_idle_topic is not None:
        group_idle_topic = build_group_idle_topic_task(
            check_group_idle_topic=deps.check_group_idle_topic,
        )
        register_group_idle_topic_job(
            scheduler=scheduler,
            group_idle_topic_job=group_idle_topic,
            interval_minutes=deps.group_idle_check_interval_minutes,
            logger=deps.logger,
        )
    if getattr(deps, "background_intelligence", None) is not None:
        async def _background_maintenance() -> dict[str, Any]:
            return await deps.background_intelligence.run_periodic_maintenance()

        register_background_intelligence_job(
            scheduler=scheduler,
            maintenance_job=_background_maintenance,
            logger=deps.logger,
        )

    curator_interval_days = int(getattr(deps.plugin_config, "personification_sticker_curator_interval_days", 3) or 3)
    async def _sticker_curator_job() -> dict[str, Any]:
        from ..core.sticker_curator import run_sticker_curation
        result = await run_sticker_curation(runtime=deps.runtime)
        deps.logger.info(f"拟人插件：表情包馆长定时整理完成: {'; '.join(result.details)}")
        return {"details": result.details}

    register_sticker_curator_job(
        scheduler=scheduler,
        curator_job=_sticker_curator_job,
        interval_days=curator_interval_days,
        logger=deps.logger,
    )

    async def _sticker_trash_cleanup_job() -> dict[str, Any]:
        from ..core.sticker_curator import clean_trash_expired
        from ..core.sticker_library import resolve_sticker_dir
        sticker_dir = resolve_sticker_dir(getattr(deps.plugin_config, "personification_sticker_path", None))
        removed = clean_trash_expired(sticker_dir)
        deps.logger.info(f"拟人插件：表情包 trash 清理完成，移除 {removed} 个过期批次")
        return {"removed": removed}

    register_sticker_trash_cleanup_job(
        scheduler=scheduler,
        cleanup_job=_sticker_trash_cleanup_job,
        logger=deps.logger,
    )

    return {
        "daily_group_fav_report": daily_group_fav_report,
        "generate_ai_diary": generate_ai_diary,
        "auto_post_diary": auto_post_diary,
        "proactive_qzone_post": proactive_qzone_post,
        "qzone_social_scan": qzone_social_scan,
        "qzone_inbound_poll": qzone_inbound_poll,
        "background_intelligence": getattr(deps, "background_intelligence", None),
    }


__all__ = [
    "run_auto_post_diary",
    "run_daily_group_fav_report",
    "run_proactive_qzone_post",
    "run_qzone_inbound_poll",
    "run_qzone_social_scan",
    "register_weekly_diary_job",
    "register_daily_group_fav_report_job",
    "register_group_idle_topic_job",
    "register_proactive_messaging_job",
    "register_proactive_qzone_job",
    "register_qzone_inbound_poll_job",
    "register_qzone_social_scan_job",
    "build_auto_post_diary_task",
    "build_daily_group_fav_report_task",
    "build_generate_ai_diary_task",
    "build_group_idle_topic_task",
    "build_maybe_generate_qzone_post_task",
    "build_proactive_qzone_post_task",
    "build_qzone_inbound_poll_task",
    "build_qzone_social_scan_task",
    "JobSetupDeps",
    "setup_jobs",
]
