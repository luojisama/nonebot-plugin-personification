from collections.abc import Callable, Iterable
from typing import Any, Dict, Optional

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, PrivateMessageEvent
from nonebot.params import CommandArg


def register_style_context_matchers(
    *,
    superuser_permission: Any,
    handle_learn_style_command: Any,
    handle_view_style_command: Any,
    handle_clear_context_command: Any,
    analyze_group_style_flow: Any,
    get_recent_group_msgs: Any,
    get_configured_api_providers: Any,
    call_ai_api: Any,
    plugin_config: Any,
    set_group_style: Any,
    clear_group_msgs: Any,
    logger: Any,
    finished_exception_cls: Any,
    get_group_style: Any,
    chat_histories: Any,
    msg_buffer: Any,
    save_session_histories: Any,
    get_driver: Any,
    build_private_session_id: Any,
    build_group_session_id: Any,
    is_global_clear_command: Any,
    clear_all_context: Any,
    resolve_clear_target: Any,
    clear_message_buffer: Any,
    clear_session_context: Any,
    group_message_event_cls: Any,
    private_message_event_cls: Any,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> Dict[str, Any]:
    def _register_command(command: str, *, aliases: set[str] | None = None, **kwargs: Any) -> Any:
        if track_command_keywords:
            track_command_keywords(command, aliases)
        if aliases is None:
            return on_command(command, **kwargs)
        return on_command(command, aliases=aliases, **kwargs)

    clear_context_cmd = _register_command(
        "清除记忆",
        aliases={"清除上下文", "重置记忆"},
        permission=superuser_permission,
        priority=5,
        block=True,
    )
    learn_style_cmd = _register_command(
        "学习群聊风格",
        aliases={"分析群聊风格"},
        permission=superuser_permission,
        priority=5,
        block=True,
    )
    view_style_cmd = _register_command("查看群聊风格", aliases={"群聊风格"}, priority=5, block=True)

    async def _analyze_group_style(group_id: str) -> Optional[str]:
        provider_api_type = ""
        try:
            style_api_key = str(
                getattr(plugin_config, "personification_style_api_key", "") or ""
            ).strip()
            if style_api_key:
                provider_api_type = str(
                    getattr(plugin_config, "personification_style_api_type", "")
                    or getattr(plugin_config, "personification_api_type", "openai")
                ).strip().lower()
            else:
                providers = get_configured_api_providers()
                if providers:
                    provider_api_type = str(providers[0].get("api_type", "") or "").strip().lower()
        except Exception:
            provider_api_type = ""
        return await analyze_group_style_flow(
            group_id,
            get_recent_group_msgs=get_recent_group_msgs,
            call_ai_api=call_ai_api,
            limit=300,
            provider_api_type=provider_api_type,
        )

    @learn_style_cmd.handle()
    async def _handle_learn_style(_bot: Bot, event: GroupMessageEvent):
        await handle_learn_style_command(
            learn_style_cmd,
            group_id=str(event.group_id),
            get_recent_group_msgs=get_recent_group_msgs,
            analyze_group_style=_analyze_group_style,
            set_group_style=set_group_style,
            clear_group_msgs=clear_group_msgs,
            logger=logger,
            finished_exception_cls=finished_exception_cls,
        )

    @view_style_cmd.handle()
    async def _handle_view_style(_bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await handle_view_style_command(
            view_style_cmd,
            args_text=args.extract_plain_text().strip(),
            event_group_id=str(event.group_id) if isinstance(event, group_message_event_cls) else None,
            get_group_style=get_group_style,
        )

    @clear_context_cmd.handle()
    async def _handle_clear_context(_bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
        await handle_clear_context_command(
            clear_context_cmd,
            args_text=arg.extract_plain_text().strip(),
            event_group_id=str(event.group_id) if isinstance(event, group_message_event_cls) else None,
            event_private_user_id=str(event.user_id) if isinstance(event, private_message_event_cls) else None,
            chat_histories=chat_histories,
            msg_buffer=msg_buffer,
            save_session_histories=save_session_histories,
            get_driver=get_driver,
            build_private_session_id=build_private_session_id,
            build_group_session_id=build_group_session_id,
            is_global_clear_command=is_global_clear_command,
            clear_all_context=clear_all_context,
            resolve_clear_target=resolve_clear_target,
            clear_message_buffer=clear_message_buffer,
            clear_session_context=clear_session_context,
        )

    return {
        "clear_context_cmd": clear_context_cmd,
        "learn_style_cmd": learn_style_cmd,
        "view_style_cmd": view_style_cmd,
    }
