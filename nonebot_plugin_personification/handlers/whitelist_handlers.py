from typing import Any, Callable, Iterable


async def handle_apply_whitelist_command(
    matcher: Any,
    *,
    bot: Any,
    event: Any,
    plugin_whitelist: list[str],
    is_group_whitelisted: Callable[[str, list[str]], bool],
    add_request: Callable[[str, str, str], bool],
    superusers: Iterable[str],
    logger: Any,
) -> None:
    group_id = str(event.group_id)

    if is_group_whitelisted(group_id, plugin_whitelist):
        await matcher.finish("本群已经在白名单中啦！")

    group_info = await bot.get_group_info(group_id=int(group_id))
    group_name = group_info.get("group_name", "未知群聊")

    if not add_request(group_id, str(event.user_id), group_name):
        await matcher.finish("已有申请正在审核中，请勿重复提交~")

    msg = (
        f"收到白名单申请：\n"
        f"群名称：{group_name}\n"
        f"群号：{group_id}\n"
        f"申请人：{event.user_id}\n\n"
        f"请回复：\n同意白名单 {group_id}\n拒绝白名单 {group_id}"
    )

    sent_count = 0
    for superuser in superusers:
        try:
            await bot.send_private_msg(user_id=int(superuser), message=msg)
            sent_count += 1
        except Exception as e:
            logger.error(f"发送申请通知给超级用户 {superuser} 失败: {e}")

    if sent_count > 0:
        await matcher.finish("已向管理员发送申请，请耐心等待审核~")
    await matcher.finish("发送申请失败，未能联系到管理员。")


async def handle_agree_whitelist_command(
    matcher: Any,
    *,
    bot: Any,
    operator_user_id: str,
    group_id: str,
    add_group_to_whitelist: Callable[[str], bool],
    update_request_status: Callable[[str, str, str], bool],
    logger: Any,
) -> None:
    if not group_id:
        await matcher.finish("请提供群号！")

    if add_group_to_whitelist(group_id):
        update_request_status(group_id, "approved", operator_user_id)
        await matcher.send(f"已将群 {group_id} 加入白名单。")
        try:
            await bot.send_group_msg(group_id=int(group_id), message="🎉 本群申请已通过，拟人功能已激活，快来和我聊天吧~")
        except Exception as e:
            logger.error(f"发送入群通知失败: {e}")
            await matcher.finish(f"已加入白名单，但发送群通知失败: {e}")
        return

    await matcher.finish(f"群 {group_id} 已在白名单中。")


async def handle_reject_whitelist_command(
    matcher: Any,
    *,
    bot: Any,
    operator_user_id: str,
    group_id: str,
    update_request_status: Callable[[str, str, str], bool],
    logger: Any,
) -> None:
    if not group_id:
        await matcher.finish("请提供群号！")

    update_request_status(group_id, "rejected", operator_user_id)
    await matcher.send(f"已拒绝群 {group_id} 的申请。")
    try:
        await bot.send_group_msg(group_id=int(group_id), message="❌ 本群白名单申请未通过。")
    except Exception as e:
        logger.error(f"发送拒绝通知失败: {e}")


async def handle_add_whitelist_command(
    matcher: Any,
    *,
    bot: Any,
    operator_user_id: str,
    group_id: str,
    add_group_to_whitelist: Callable[[str], bool],
    update_request_status: Callable[[str, str, str], bool],
    logger: Any,
) -> None:
    if not group_id:
        await matcher.finish("请提供群号！")

    if add_group_to_whitelist(group_id):
        update_request_status(group_id, "approved", operator_user_id)
        await matcher.send(f"已将群 {group_id} 添加到白名单。")
        try:
            await bot.send_group_msg(group_id=int(group_id), message="🎉 本群已启用拟人功能，快来和我聊天吧~")
        except Exception as e:
            logger.error(f"发送入群通知失败: {e}")
            await matcher.finish(f"已加入白名单，但发送群通知失败: {e}")
        return

    await matcher.finish(f"群 {group_id} 已在白名单中。")


async def handle_remove_whitelist_command(
    matcher: Any,
    *,
    group_id: str,
    remove_group_from_whitelist: Callable[[str], bool],
) -> None:
    if not group_id:
        await matcher.finish("请提供群号！")

    if remove_group_from_whitelist(group_id):
        await matcher.finish(f"已将群 {group_id} 移出白名单。")
    await matcher.finish(f"群 {group_id} 不在白名单中（若是配置文件的白名单则无法动态移除）。")
