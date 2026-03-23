from typing import Any, Callable, Dict, Optional


def extract_target_user_id(args_text: str, message: Any) -> str:
    """从参数或艾特消息中提取目标用户 ID。"""
    target_id = args_text.strip()
    for seg in message:
        seg_type = getattr(seg, "type", "")
        seg_data = getattr(seg, "data", {})
        if seg_type == "at" and isinstance(seg_data, dict):
            qq = seg_data.get("qq")
            if qq is not None:
                target_id = str(qq)
                break
    return target_id


async def handle_perm_blacklist_set_command(
    matcher: Any,
    *,
    sign_in_available: bool,
    args_text: str,
    message: Any,
    update_user_data: Callable[..., None],
    set_blacklisted: bool,
) -> None:
    """处理永久拉黑/取消永久拉黑命令。"""
    if not sign_in_available:
        await matcher.finish("签到插件未就绪，无法操作。")

    target_id = extract_target_user_id(args_text, message)
    if not target_id:
        usage = "永久拉黑 [用户ID/@用户]" if set_blacklisted else "取消永久拉黑 [用户ID/@用户]"
        await matcher.finish(f"用法: {usage}")

    update_user_data(target_id, is_perm_blacklisted=set_blacklisted)
    if set_blacklisted:
        await matcher.finish(f"✅ 已将用户 {target_id} 加入永久黑名单。")
    await matcher.finish(f"✅ 已将用户 {target_id} 从永久黑名单中移除。")


async def handle_schedule_switch_command(
    matcher: Any,
    *,
    status: str,
    group_id: Optional[str],
    plugin_config: Any,
    save_plugin_runtime_config: Callable[[], None],
    set_group_schedule_enabled: Callable[[str, bool], None],
    bot_statuses: Dict[str, str],
) -> None:
    """处理拟人作息开关命令。"""
    if status in {"全局开启", "全局on", "全局true"}:
        plugin_config.personification_schedule_global = True
        save_plugin_runtime_config()
        await matcher.finish("拟人作息模拟已全局开启（所有群默认生效，除非单独关闭）。")

    if status in {"全局关闭", "全局off", "全局false"}:
        plugin_config.personification_schedule_global = False
        bot_statuses.clear()
        save_plugin_runtime_config()
        await matcher.finish("拟人作息模拟全局开关已关闭（仅在单独开启的群生效，且已清空状态缓存）。")

    if status not in {"开启", "关闭"}:
        global_status = "开启" if plugin_config.personification_schedule_global else "关闭"
        await matcher.finish(f"用法: 拟人作息 [开启/关闭/全局开启/全局关闭]\n当前全局状态：{global_status}")

    if not group_id:
        await matcher.finish("请在群聊中使用此命令开启/关闭单群功能，或使用 '全局开启/全局关闭'。")

    is_enabled = status == "开启"
    set_group_schedule_enabled(group_id, is_enabled)
    if not is_enabled:
        bot_statuses.pop(group_id, None)
    await matcher.finish(f"本群作息模拟功能已{status}。")
