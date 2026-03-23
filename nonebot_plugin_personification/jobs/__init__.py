from dataclasses import dataclass
from typing import Any, Dict

from .periodic_jobs import run_auto_post_diary, run_daily_group_fav_report
from .scheduler_registration import (
    register_daily_group_fav_report_job,
    register_group_idle_topic_job,
    register_proactive_messaging_job,
    register_weekly_diary_job,
)
from .task_builders import (
    build_auto_post_diary_task,
    build_daily_group_fav_report_task,
    build_generate_ai_diary_task,
    build_group_idle_topic_task,
)


@dataclass
class JobSetupDeps:
    sign_in_available: bool
    load_data: Any
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
    check_proactive_messaging: Any
    proactive_interval_minutes: int
    check_group_idle_topic: Any = None
    group_idle_check_interval_minutes: int = 15
    agent_tool_caller: Any = None
    agent_data_dir: Any = None


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
    register_proactive_messaging_job(
        scheduler=scheduler,
        proactive_job=deps.check_proactive_messaging,
        interval_minutes=deps.proactive_interval_minutes,
        logger=deps.logger,
    )
    if deps.check_group_idle_topic is not None:
        group_idle_topic = build_group_idle_topic_task(
            check_group_idle_topic=deps.check_group_idle_topic,
        )
        register_group_idle_topic_job(
            scheduler=scheduler,
            group_idle_topic_job=group_idle_topic,
            interval_minutes=deps.group_idle_check_interval_minutes,
            logger=deps.logger,
        )

    return {
        "daily_group_fav_report": daily_group_fav_report,
        "generate_ai_diary": generate_ai_diary,
        "auto_post_diary": auto_post_diary,
    }


__all__ = [
    "run_auto_post_diary",
    "run_daily_group_fav_report",
    "register_weekly_diary_job",
    "register_daily_group_fav_report_job",
    "register_group_idle_topic_job",
    "register_proactive_messaging_job",
    "build_auto_post_diary_task",
    "build_daily_group_fav_report_task",
    "build_generate_ai_diary_task",
    "build_group_idle_topic_task",
    "JobSetupDeps",
    "setup_jobs",
]
