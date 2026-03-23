from typing import Any, Callable, Optional


async def handle_group_fav_query_command(
    matcher: Any,
    *,
    sign_in_available: bool,
    group_id: str,
    get_user_data: Callable[[str], dict],
    get_level_name: Callable[[float], str],
    build_group_fav_markdown: Callable[[str, float, float, str], str],
    build_group_fav_text: Callable[[str, float, float, str], str],
    md_to_pic: Optional[Callable[..., Any]],
    message_segment_cls: Any,
    finished_exception_cls: Optional[type[BaseException]],
    logger: Any,
) -> None:
    if not sign_in_available:
        await matcher.finish("签到插件未就绪，无法查询好感度。")

    group_key = f"group_{group_id}"
    data = get_user_data(group_key)
    favorability = float(data.get("favorability", 100.0))
    daily_count = float(data.get("daily_fav_count", 0.0))
    status = get_level_name(favorability) if sign_in_available else "普通"
    md = build_group_fav_markdown(group_id, favorability, daily_count, status)

    pic = None
    if md_to_pic:
        try:
            pic = await md_to_pic(md, width=450)
        except Exception as e:
            if finished_exception_cls and isinstance(e, finished_exception_cls):
                raise
            logger.error(f"渲染群好感图片失败: {e}")

    if pic:
        await matcher.finish(message_segment_cls.image(pic))
    await matcher.finish(build_group_fav_text(group_id, favorability, daily_count, status))


async def handle_set_group_fav_command(
    matcher: Any,
    *,
    sign_in_available: bool,
    arg_str: str,
    event_group_id: Optional[str],
    operator_user_id: str,
    parse_group_fav_update_args: Callable[[str, Optional[str]], tuple[Optional[str], Optional[float], Optional[str]]],
    update_user_data: Callable[..., None],
    logger: Any,
) -> None:
    if not sign_in_available:
        await matcher.finish("签到插件未就绪，无法设置好感度。")

    target_group, new_fav, error_msg = parse_group_fav_update_args(arg_str, event_group_id)
    if error_msg:
        await matcher.finish(error_msg)
    if not target_group or new_fav is None:
        await matcher.finish("未指定目标群号。")

    update_user_data(f"group_{target_group}", favorability=new_fav)
    logger.info(f"管理员 {operator_user_id} 将群 {target_group} 的好感度设置为 {new_fav}")
    await matcher.finish(f"✅ 已将群 {target_group} 的好感度设置为 {new_fav:.2f}")


async def handle_set_persona_command(
    matcher: Any,
    *,
    raw_text: str,
    event_group_id: Optional[str],
    parse_persona_update_args: Callable[[str, Optional[str]], tuple[Optional[str], Optional[str], Optional[str]]],
    set_group_prompt: Callable[[str, Optional[str]], None],
) -> None:
    target_group_id, prompt, error_msg = parse_persona_update_args(raw_text, event_group_id)
    if error_msg:
        await matcher.finish(error_msg)
    if not target_group_id or not prompt:
        await matcher.finish("请提供提示词！")

    set_group_prompt(target_group_id, prompt)
    await matcher.finish(f"已更新群 {target_group_id} 的人设。")


async def handle_view_persona_command(
    matcher: Any,
    *,
    bot: Any,
    group_id: str,
    load_prompt: Callable[[str], Any],
    logger: Any,
) -> None:
    prompt = load_prompt(group_id)
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "当前生效人设",
                "uin": str(bot.self_id),
                "content": prompt,
            },
        }
    ]

    try:
        await bot.call_api("send_group_forward_msg", group_id=int(group_id), messages=nodes)
    except Exception as e:
        logger.error(f"发送人设转发消息失败: {e}")
        await matcher.finish(f"当前生效人设（转发失败，转文本发送）：\n{prompt}")


async def handle_reset_persona_command(
    matcher: Any,
    *,
    group_id: str,
    set_group_prompt: Callable[[str, Optional[str]], None],
) -> None:
    set_group_prompt(group_id, None)
    await matcher.finish("已重置本群人设为默认配置。")


async def handle_group_feature_switch_command(
    matcher: Any,
    *,
    group_id: str,
    setter: Callable[[str, bool], None],
    enabled: bool,
    feature_name: str,
) -> None:
    setter(group_id, enabled)
    status = "开启" if enabled else "关闭"
    await matcher.finish(f"本群{feature_name}功能已{status}。")


async def handle_view_config_command(
    matcher: Any,
    *,
    bot: Any,
    group_id: str,
    get_group_config: Callable[[str], dict],
    get_configured_api_providers: Callable[[], list[dict]],
    build_view_config_nodes: Callable[..., list[dict]],
    plugin_config: Any,
    session_history_limit: int,
    logger: Any,
) -> None:
    group_config = get_group_config(group_id)
    provider_names = ", ".join(provider["name"] for provider in get_configured_api_providers()) or "未配置"
    nodes = build_view_config_nodes(
        bot_self_id=str(bot.self_id),
        group_id=group_id,
        group_config=group_config,
        provider_names=provider_names,
        plugin_config=plugin_config,
        session_history_limit=session_history_limit,
    )

    try:
        await bot.call_api("send_group_forward_msg", group_id=int(group_id), messages=nodes)
    except Exception as e:
        logger.error(f"发送配置详情失败: {e}")
        await matcher.finish(f"配置详情发送失败: {e}")
