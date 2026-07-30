from typing import Any, Callable, Dict, Optional, Tuple


GLOBAL_CLEAR_ARGS = {"全局", "all", "所有"}


def is_global_clear_command(args_text: str) -> bool:
    return args_text in GLOBAL_CLEAR_ARGS


def resolve_clear_target(
    args_text: str,
    *,
    group_id: Optional[str],
    private_user_id: Optional[str],
    build_private_session_id: Callable[[str], str],
) -> Tuple[Optional[str], bool]:
    """解析清除目标，返回 (target_id, is_group)。"""
    if args_text and args_text.isdigit():
        return args_text, True
    if group_id:
        return group_id, True
    if private_user_id:
        return build_private_session_id(private_user_id), False
    return None, False


def clear_all_context(
    chat_histories: Dict[str, Any],
    *,
    save_session_histories: Callable[[], None],
    driver: Any,
) -> int:
    """清空全部上下文并返回清空数量。"""
    count = len(chat_histories)
    chat_histories.clear()
    save_session_histories()
    if hasattr(driver, "_personification_msg_cache"):
        driver._personification_msg_cache.clear()
    return count


def clear_message_buffer(msg_buffer: Dict[str, Dict[str, Any]], target_id: str) -> int:
    """按目标会话前缀清理消息缓冲。"""
    keys_to_remove = []
    for key in list(msg_buffer.keys()):
        if key == target_id:
            keys_to_remove.append(key)

    for key in keys_to_remove:
        timer_task = msg_buffer[key].get("timer_task")
        if timer_task:
            timer_task.cancel()
        del msg_buffer[key]
    return len(keys_to_remove)


def clear_session_context(
    chat_histories: Dict[str, Any],
    *,
    target_id: str,
    is_group: bool,
    build_group_session_id: Callable[[str], str],
    save_session_histories: Callable[[], None],
) -> Optional[str]:
    """清理目标会话上下文，成功则返回提示文本。"""
    if is_group and target_id in chat_histories:
        del chat_histories[target_id]
        save_session_histories()

    target_session_id = build_group_session_id(target_id) if is_group else target_id
    if target_session_id not in chat_histories:
        return None

    del chat_histories[target_session_id]
    save_session_histories()
    if is_group and not target_id.startswith("private_"):
        return f"已清除群 {target_id} 的短期对话上下文记忆。"
    return "已清除当前私聊的短期对话上下文记忆。"
