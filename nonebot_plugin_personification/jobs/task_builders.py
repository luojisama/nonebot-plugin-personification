from typing import Any, Awaitable, Callable


def build_generate_ai_diary_task(
    *,
    plugin_config: Any,
    generate_ai_diary_flow: Callable[..., Awaitable[str]],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[str]],
    logger: Any,
    agent_tool_caller: Any = None,
    get_agent_tool_caller: Callable[[], Any] | None = None,
    agent_tool_registry: Any = None,
    agent_max_steps: int = 4,
    agent_data_dir: Any = None,
    user_policy_authorizer: Any = None,
) -> Callable[[Any], Awaitable[str]]:
    def _current_tool_caller() -> Any:
        return get_agent_tool_caller() if callable(get_agent_tool_caller) else agent_tool_caller

    async def _generate_ai_diary(bot: Any) -> str:
        return await generate_ai_diary_flow(
            bot,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            logger=logger,
            plugin_config=plugin_config,
            tool_caller=_current_tool_caller(),
            registry=agent_tool_registry,
            agent_max_steps=agent_max_steps,
            data_dir=agent_data_dir,
            user_policy_authorizer=user_policy_authorizer,
        )

    async def _generate_ai_diary_detailed(bot: Any) -> dict[str, Any]:
        detailed = getattr(generate_ai_diary_flow, "detailed", None)
        if not callable(detailed):
            content = await _generate_ai_diary(bot)
            return {"content": content, "diagnostic": {"ok": bool(content)}}
        return await detailed(
            bot,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            logger=logger,
            plugin_config=plugin_config,
            tool_caller=_current_tool_caller(),
            registry=agent_tool_registry,
            agent_max_steps=agent_max_steps,
            data_dir=agent_data_dir,
            user_policy_authorizer=user_policy_authorizer,
        )

    def _mark_published(content: str) -> None:
        from ..flows.diary_flow import schedule_diary_state_update

        schedule_diary_state_update(
            diary_text=content,
            tool_caller=_current_tool_caller(),
            data_dir=agent_data_dir,
            logger=logger,
        )

    setattr(_generate_ai_diary, "mark_published", _mark_published)
    setattr(_generate_ai_diary, "detailed", _generate_ai_diary_detailed)

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
    qzone_monthly_limit: int,
    qzone_min_interval_hours: float,
    get_bots: Callable[[], dict[str, Any]],
    get_now: Callable[[], Any],
    update_qzone_cookie: Any,
    maybe_generate_qzone_post: Callable[[Any], Awaitable[str]],
    publish_qzone_shuo: Any,
    logger: Any,
    qzone_quiet_hour_start: int = 0,
    qzone_quiet_hour_end: int = 7,
) -> Callable[[], Awaitable[bool]]:
    async def _proactive_qzone_post() -> bool:
        return await run_proactive_qzone_post(
            qzone_publish_available=qzone_publish_available,
            qzone_proactive_enabled=qzone_proactive_enabled,
            qzone_probability=qzone_probability,
            qzone_monthly_limit=qzone_monthly_limit,
            qzone_min_interval_hours=qzone_min_interval_hours,
            get_bots=get_bots,
            get_now=get_now,
            update_qzone_cookie=update_qzone_cookie,
            maybe_generate_qzone_post=maybe_generate_qzone_post,
            publish_qzone_shuo=publish_qzone_shuo,
            logger=logger,
            quiet_hour_start=qzone_quiet_hour_start,
            quiet_hour_end=qzone_quiet_hour_end,
        )

    return _proactive_qzone_post


def build_qzone_social_scan_task(
    *,
    run_qzone_social_scan: Callable[..., Awaitable[dict[str, Any]]],
    qzone_publish_available: bool,
    qzone_social_enabled: bool,
    get_bots: Callable[[], dict[str, Any]],
    update_qzone_cookie: Any,
    scan_qzone_social_feeds: Callable[..., Awaitable[dict[str, Any]]],
    logger: Any,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def _qzone_social_scan(target_user_id: str = "", force: bool = False) -> dict[str, Any]:
        return await run_qzone_social_scan(
            qzone_publish_available=qzone_publish_available,
            qzone_social_enabled=qzone_social_enabled,
            get_bots=get_bots,
            update_qzone_cookie=update_qzone_cookie,
            scan_qzone_social_feeds=scan_qzone_social_feeds,
            logger=logger,
            target_user_id=target_user_id,
            force=force,
        )

    return _qzone_social_scan


def build_qzone_inbound_poll_task(
    *,
    run_qzone_inbound_poll: Callable[..., Awaitable[dict[str, Any]]],
    qzone_publish_available: bool,
    qzone_inbound_enabled: bool,
    get_bots: Callable[[], dict[str, Any]],
    update_qzone_cookie: Any,
    poll_qzone_inbound_messages: Callable[[Any], Awaitable[dict[str, Any]]],
    logger: Any,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def _qzone_inbound_poll(force: bool = False) -> dict[str, Any]:
        return await run_qzone_inbound_poll(
            qzone_publish_available=qzone_publish_available,
            qzone_inbound_enabled=qzone_inbound_enabled,
            get_bots=get_bots,
            update_qzone_cookie=update_qzone_cookie,
            poll_qzone_inbound_messages=poll_qzone_inbound_messages,
            logger=logger,
            force=force,
        )

    return _qzone_inbound_poll


def build_maybe_generate_qzone_post_task(
    *,
    plugin_config: Any,
    maybe_generate_proactive_qzone_post_flow: Callable[..., Awaitable[str]],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[str]],
    logger: Any,
    agent_tool_caller: Any = None,
    get_agent_tool_caller: Callable[[], Any] | None = None,
    agent_tool_registry: Any = None,
    agent_max_steps: int = 4,
    agent_data_dir: Any = None,
    user_policy_authorizer: Any = None,
) -> Callable[..., Awaitable[str]]:
    def _current_tool_caller() -> Any:
        return get_agent_tool_caller() if callable(get_agent_tool_caller) else agent_tool_caller

    async def _maybe_generate_qzone_post(bot: Any, quota: Any = None) -> str:
        return await maybe_generate_proactive_qzone_post_flow(
            bot,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            logger=logger,
            plugin_config=plugin_config,
            data_dir=agent_data_dir,
            tool_caller=_current_tool_caller(),
            registry=agent_tool_registry,
            agent_max_steps=agent_max_steps,
            quota=quota,
            user_policy_authorizer=user_policy_authorizer,
        )

    def _mark_published(content: str) -> None:
        from ..flows.diary_flow import schedule_diary_state_update

        schedule_diary_state_update(
            diary_text=content,
            tool_caller=_current_tool_caller(),
            data_dir=agent_data_dir,
            logger=logger,
        )

    setattr(_maybe_generate_qzone_post, "mark_published", _mark_published)

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


def build_favorability_maintenance_task(
    *,
    run_favorability_maintenance: Callable[..., Awaitable[dict[str, Any]]],
    sign_in_available: bool,
    favorability_service: Any,
    logger: Any,
) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def _favorability_maintenance() -> dict[str, Any]:
        return await run_favorability_maintenance(
            sign_in_available=sign_in_available,
            favorability_service=favorability_service,
            logger=logger,
        )

    return _favorability_maintenance


def build_group_idle_topic_task(
    *,
    check_group_idle_topic: Callable[[], Awaitable[int]],
) -> Callable[[], Awaitable[int]]:
    async def _group_idle_topic() -> int:
        return await check_group_idle_topic()

    return _group_idle_topic
