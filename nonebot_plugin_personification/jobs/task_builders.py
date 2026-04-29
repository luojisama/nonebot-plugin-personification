from typing import Any, Awaitable, Callable


def build_generate_ai_diary_task(
    *,
    generate_ai_diary_flow: Callable[..., Awaitable[str]],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[str]],
    logger: Any,
    agent_tool_caller: Any = None,
    agent_data_dir: Any = None,
) -> Callable[[Any], Awaitable[str]]:
    async def _generate_ai_diary(bot: Any) -> str:
        return await generate_ai_diary_flow(
            bot,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            logger=logger,
            tool_caller=agent_tool_caller,
            data_dir=agent_data_dir,
        )

    return _generate_ai_diary


def build_auto_post_diary_task(
    *,
    run_auto_post_diary: Callable[..., Awaitable[bool]],
    qzone_publish_available: bool,
    get_bots: Callable[[], dict[str, Any]],
    update_qzone_cookie: Any,
    generate_ai_diary: Callable[[Any], Awaitable[str]],
    publish_qzone_shuo: Any,
    logger: Any,
) -> Callable[[], Awaitable[bool]]:
    async def _auto_post_diary() -> bool:
        return await run_auto_post_diary(
            qzone_publish_available=qzone_publish_available,
            get_bots=get_bots,
            update_qzone_cookie=update_qzone_cookie,
            generate_ai_diary=generate_ai_diary,
            publish_qzone_shuo=publish_qzone_shuo,
            logger=logger,
        )

    return _auto_post_diary


def build_proactive_qzone_post_task(
    *,
    run_proactive_qzone_post: Callable[..., Awaitable[bool]],
    qzone_publish_available: bool,
    qzone_proactive_enabled: bool,
    qzone_probability: float,
    qzone_daily_limit: int,
    qzone_min_interval_hours: float,
    get_bots: Callable[[], dict[str, Any]],
    get_now: Callable[[], Any],
    update_qzone_cookie: Any,
    maybe_generate_qzone_post: Callable[[Any], Awaitable[str]],
    publish_qzone_shuo: Any,
    logger: Any,
) -> Callable[[], Awaitable[bool]]:
    async def _proactive_qzone_post() -> bool:
        return await run_proactive_qzone_post(
            qzone_publish_available=qzone_publish_available,
            qzone_proactive_enabled=qzone_proactive_enabled,
            qzone_probability=qzone_probability,
            qzone_daily_limit=qzone_daily_limit,
            qzone_min_interval_hours=qzone_min_interval_hours,
            get_bots=get_bots,
            get_now=get_now,
            update_qzone_cookie=update_qzone_cookie,
            maybe_generate_qzone_post=maybe_generate_qzone_post,
            publish_qzone_shuo=publish_qzone_shuo,
            logger=logger,
        )

    return _proactive_qzone_post


def build_maybe_generate_qzone_post_task(
    *,
    maybe_generate_proactive_qzone_post_flow: Callable[..., Awaitable[str]],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[str]],
    logger: Any,
    agent_tool_caller: Any = None,
    agent_data_dir: Any = None,
) -> Callable[[Any], Awaitable[str]]:
    async def _maybe_generate_qzone_post(bot: Any) -> str:
        return await maybe_generate_proactive_qzone_post_flow(
            bot,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            logger=logger,
            data_dir=agent_data_dir,
            tool_caller=agent_tool_caller,
        )

    return _maybe_generate_qzone_post


def build_daily_group_fav_report_task(
    *,
    run_daily_group_fav_report: Callable[..., Awaitable[int]],
    sign_in_available: bool,
    load_data: Callable[[], dict[str, Any]],
    get_now: Callable[[], Any],
    get_bots: Callable[[], dict[str, Any]],
    superusers: set[str],
    logger: Any,
) -> Callable[[], Awaitable[int]]:
    async def _daily_group_fav_report() -> int:
        return await run_daily_group_fav_report(
            sign_in_available=sign_in_available,
            load_data=load_data,
            get_now=get_now,
            get_bots=get_bots,
            superusers=superusers,
            logger=logger,
        )

    return _daily_group_fav_report


def build_group_idle_topic_task(
    *,
    check_group_idle_topic: Callable[[], Awaitable[int]],
) -> Callable[[], Awaitable[int]]:
    async def _group_idle_topic() -> int:
        return await check_group_idle_topic()

    return _group_idle_topic
