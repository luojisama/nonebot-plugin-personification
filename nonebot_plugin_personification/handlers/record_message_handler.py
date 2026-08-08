from typing import Any, Callable, Optional


async def handle_record_message_event(
    event: Any,
    *,
    resolve_record_message: Callable[..., Any],
    get_custom_title: Optional[Callable[[str], Optional[str]]] = None,
    record_group_msg: Callable[..., Any],
    should_trigger_auto_analyze: Optional[Callable[[str, int], bool]] = None,
    logger: Any,
    create_background_task: Callable[[str], None],
    create_summary_task: Optional[Callable[[str], None]] = None,
    user_policy_gate: Any = None,
    create_scoped_profile_task: Optional[Callable[[str, str], None]] = None,
) -> None:
    if user_policy_gate is not None and not await user_policy_gate.allows_current(event):
        return
    custom_title_getter = get_custom_title or (lambda _user_id: None)
    group_id, should_auto_analyze = resolve_record_message(
        event,
        get_custom_title=custom_title_getter,
        record_group_msg=record_group_msg,
        should_trigger_auto_analyze=should_trigger_auto_analyze,
    )
    if group_id:
        try:
            from ..core.group_directory import record_observed_group

            record_observed_group(
                getattr(event, "self_id", "unknown"),
                group_id,
                source="group_message_event",
            )
        except Exception:
            pass
    if group_id and create_summary_task is not None:
        create_summary_task(group_id)
    if group_id and create_scoped_profile_task is not None:
        create_scoped_profile_task(group_id, str(getattr(event, "user_id", "") or ""))
    if group_id and should_auto_analyze:
        logger.info(f"拟人插件：群 {group_id} 消息已满 200 条，已创建后台任务进行风格分析...")
        create_background_task(group_id)
