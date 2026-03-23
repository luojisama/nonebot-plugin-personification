from typing import Any, Callable, Dict, Optional


async def handle_web_search_switch_command(
    matcher: Any,
    *,
    action: str,
    plugin_config: Any,
    apply_web_search_switch: Callable[[str, Any], tuple[bool, str]],
    save_plugin_runtime_config: Callable[[], None],
) -> None:
    changed, msg = apply_web_search_switch(action, plugin_config)
    if changed:
        save_plugin_runtime_config()
    await matcher.finish(msg)


async def handle_proactive_switch_command(
    matcher: Any,
    *,
    action: str,
    plugin_config: Any,
    apply_proactive_switch: Callable[[str, Any], tuple[bool, str]],
    save_plugin_runtime_config: Callable[[], None],
) -> None:
    changed, msg = apply_proactive_switch(action, plugin_config)
    if changed:
        save_plugin_runtime_config()
    await matcher.finish(msg)


async def handle_clear_context_command(
    matcher: Any,
    *,
    args_text: str,
    event_group_id: Optional[str],
    event_private_user_id: Optional[str],
    chat_histories: Dict[str, Any],
    msg_buffer: Dict[str, Dict[str, Any]],
    save_session_histories: Callable[[], None],
    get_driver: Callable[[], Any],
    build_private_session_id: Callable[[str], str],
    build_group_session_id: Callable[[str], str],
    is_global_clear_command: Callable[[str], bool],
    clear_all_context: Callable[..., int],
    resolve_clear_target: Callable[..., tuple[Optional[str], bool]],
    clear_message_buffer: Callable[[Dict[str, Dict[str, Any]], str], int],
    clear_session_context: Callable[..., Optional[str]],
) -> None:
    if is_global_clear_command(args_text):
        count = clear_all_context(
            chat_histories,
            save_session_histories=save_session_histories,
            driver=get_driver(),
        )
        await matcher.finish(f"已清除全局所有群聊/私聊的对话上下文记忆（共 {count} 个会话）。")

    target_id, is_group = resolve_clear_target(
        args_text=args_text,
        group_id=event_group_id,
        private_user_id=event_private_user_id,
        build_private_session_id=build_private_session_id,
    )
    if not target_id:
        await matcher.finish("无法确定要清除的目标，请指定群号或在群聊/私聊中使用，或使用 '清除记忆 全局'。")

    clear_message_buffer(msg_buffer, target_id)
    msg = clear_session_context(
        chat_histories=chat_histories,
        target_id=target_id,
        is_group=is_group,
        build_group_session_id=build_group_session_id,
        save_session_histories=save_session_histories,
    )
    if msg:
        await matcher.finish(msg)
    await matcher.finish("当前没有任何缓存的对话上下文记忆。")
