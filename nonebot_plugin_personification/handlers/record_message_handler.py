from typing import Any, Callable, Optional


def handle_record_message_event(
    event: Any,
    *,
    resolve_record_message: Callable[..., Any],
    get_custom_title: Optional[Callable[[str], Optional[str]]] = None,
    record_group_msg: Callable[..., Any],
    logger: Any,
    create_background_task: Callable[[str], None],
    create_summary_task: Optional[Callable[[str], None]] = None,
) -> None:
    custom_title_getter = get_custom_title or (lambda _user_id: None)
    group_id, should_auto_analyze = resolve_record_message(
        event,
        get_custom_title=custom_title_getter,
        record_group_msg=record_group_msg,
    )
    if group_id and create_summary_task is not None:
        create_summary_task(group_id)
    if group_id and should_auto_analyze:
        logger.info(f"拟人插件：群 {group_id} 消息已满 200 条，已创建后台任务进行风格分析...")
        create_background_task(group_id)
