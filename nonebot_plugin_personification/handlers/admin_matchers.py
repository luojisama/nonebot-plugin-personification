from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg

from ..core.remote_skill_review import get_remote_skill_review_stats


def register_admin_matchers(
    *,
    superuser_permission: Any,
    sign_in_available: bool,
    handle_group_fav_query_command: Any,
    get_user_data: Any,
    get_level_name: Any,
    build_group_fav_markdown: Any,
    build_group_fav_text: Any,
    md_to_pic: Any,
    message_segment_cls: Any,
    finished_exception_cls: Any,
    logger: Any,
    handle_set_group_fav_command: Any,
    parse_group_fav_update_args: Any,
    update_user_data: Any,
    group_message_event_cls: Any,
    handle_set_persona_command: Any,
    parse_persona_update_args: Any,
    set_group_prompt: Any,
    handle_view_persona_command: Any,
    load_prompt: Any,
    handle_reset_persona_command: Any,
    handle_group_feature_switch_command: Any,
    set_group_enabled: Any,
    set_group_sticker_enabled: Any,
    set_group_tts_enabled: Any,
    handle_schedule_switch_command: Any,
    plugin_config: Any,
    save_plugin_runtime_config: Any,
    set_group_schedule_enabled: Any,
    bot_statuses: Any,
    handle_view_config_command: Any,
    get_group_config: Any,
    get_configured_api_providers: Any,
    build_view_config_nodes: Any,
    session_history_limit: int,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> Dict[str, Any]:
    def _register_command(command: str, *, aliases: set[str] | None = None, **kwargs: Any) -> Any:
        if track_command_keywords:
            track_command_keywords(command, aliases)
        if aliases is None:
            return on_command(command, **kwargs)
        return on_command(command, aliases=aliases, **kwargs)

    group_fav_query = _register_command("群好感", aliases={"群好感度"}, priority=5, block=True)

    @group_fav_query.handle()
    async def _handle_group_fav_query(_bot: Bot, event: GroupMessageEvent):
        await handle_group_fav_query_command(
            group_fav_query,
            sign_in_available=sign_in_available,
            group_id=str(event.group_id),
            get_user_data=get_user_data,
            get_level_name=get_level_name,
            build_group_fav_markdown=build_group_fav_markdown,
            build_group_fav_text=build_group_fav_text,
            md_to_pic=md_to_pic,
            message_segment_cls=message_segment_cls,
            finished_exception_cls=finished_exception_cls,
            logger=logger,
        )

    set_group_fav = _register_command("设置群好感", permission=superuser_permission, priority=5, block=True)

    @set_group_fav.handle()
    async def _handle_set_group_fav(_bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await handle_set_group_fav_command(
            set_group_fav,
            sign_in_available=sign_in_available,
            arg_str=args.extract_plain_text().strip(),
            event_group_id=str(event.group_id) if isinstance(event, group_message_event_cls) else None,
            operator_user_id=event.get_user_id(),
            parse_group_fav_update_args=parse_group_fav_update_args,
            update_user_data=update_user_data,
            logger=logger,
        )

    set_persona = _register_command("设置人设", permission=superuser_permission, priority=5, block=True)

    @set_persona.handle()
    async def _handle_set_persona(_bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await handle_set_persona_command(
            set_persona,
            raw_text=args.extract_plain_text().strip(),
            event_group_id=str(event.group_id) if isinstance(event, group_message_event_cls) else None,
            parse_persona_update_args=parse_persona_update_args,
            set_group_prompt=set_group_prompt,
        )

    view_persona = _register_command("查看人设", permission=superuser_permission, priority=5, block=True)

    @view_persona.handle()
    async def _handle_view_persona(bot: Bot, event: GroupMessageEvent):
        await handle_view_persona_command(
            view_persona,
            bot=bot,
            group_id=str(event.group_id),
            load_prompt=load_prompt,
            logger=logger,
        )

    reset_persona = _register_command("重置人设", permission=superuser_permission, priority=5, block=True)

    @reset_persona.handle()
    async def _handle_reset_persona(_bot: Bot, event: GroupMessageEvent):
        await handle_reset_persona_command(
            reset_persona,
            group_id=str(event.group_id),
            set_group_prompt=set_group_prompt,
        )

    enable_personification = _register_command("开启拟人", permission=superuser_permission, priority=5, block=True)
    disable_personification = _register_command("关闭拟人", permission=superuser_permission, priority=5, block=True)

    @enable_personification.handle()
    async def _handle_enable_personification(_bot: Bot, event: GroupMessageEvent):
        await handle_group_feature_switch_command(
            enable_personification,
            group_id=str(event.group_id),
            setter=set_group_enabled,
            enabled=True,
            feature_name="拟人",
        )

    @disable_personification.handle()
    async def _handle_disable_personification(_bot: Bot, event: GroupMessageEvent):
        await handle_group_feature_switch_command(
            disable_personification,
            group_id=str(event.group_id),
            setter=set_group_enabled,
            enabled=False,
            feature_name="拟人",
        )

    enable_stickers = _register_command("开启表情包", permission=superuser_permission, priority=5, block=True)
    disable_stickers = _register_command("关闭表情包", permission=superuser_permission, priority=5, block=True)
    enable_tts = _register_command("开启语音回复", permission=superuser_permission, priority=5, block=True)
    disable_tts = _register_command("关闭语音回复", permission=superuser_permission, priority=5, block=True)

    @enable_stickers.handle()
    async def _handle_enable_stickers(_bot: Bot, event: GroupMessageEvent):
        await handle_group_feature_switch_command(
            enable_stickers,
            group_id=str(event.group_id),
            setter=set_group_sticker_enabled,
            enabled=True,
            feature_name="表情包",
        )

    @disable_stickers.handle()
    async def _handle_disable_stickers(_bot: Bot, event: GroupMessageEvent):
        await handle_group_feature_switch_command(
            disable_stickers,
            group_id=str(event.group_id),
            setter=set_group_sticker_enabled,
            enabled=False,
            feature_name="表情包",
        )

    @enable_tts.handle()
    async def _handle_enable_tts(_bot: Bot, event: GroupMessageEvent):
        await handle_group_feature_switch_command(
            enable_tts,
            group_id=str(event.group_id),
            setter=set_group_tts_enabled,
            enabled=True,
            feature_name="语音回复",
        )

    @disable_tts.handle()
    async def _handle_disable_tts(_bot: Bot, event: GroupMessageEvent):
        await handle_group_feature_switch_command(
            disable_tts,
            group_id=str(event.group_id),
            setter=set_group_tts_enabled,
            enabled=False,
            feature_name="语音回复",
        )

    enable_schedule = _register_command("拟人作息", permission=superuser_permission, priority=5, block=True)

    @enable_schedule.handle()
    async def _handle_schedule(_bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        status = args.extract_plain_text().strip()
        group_id = str(event.group_id) if isinstance(event, group_message_event_cls) else None
        await handle_schedule_switch_command(
            enable_schedule,
            status=status,
            group_id=group_id,
            plugin_config=plugin_config,
            save_plugin_runtime_config=save_plugin_runtime_config,
            set_group_schedule_enabled=set_group_schedule_enabled,
            bot_statuses=bot_statuses,
        )

    view_config = _register_command("拟人配置", permission=superuser_permission, priority=5, block=True)

    @view_config.handle()
    async def _handle_view_config(bot: Bot, event: GroupMessageEvent):
        await handle_view_config_command(
            view_config,
            bot=bot,
            group_id=str(event.group_id),
            get_group_config=get_group_config,
            get_configured_api_providers=get_configured_api_providers,
            build_view_config_nodes=build_view_config_nodes,
            plugin_config=plugin_config,
            session_history_limit=session_history_limit,
            get_remote_skill_review_stats=get_remote_skill_review_stats,
            logger=logger,
        )

    return {
        "group_fav_query": group_fav_query,
        "set_group_fav": set_group_fav,
        "set_persona": set_persona,
        "view_persona": view_persona,
        "reset_persona": reset_persona,
        "enable_personification": enable_personification,
        "disable_personification": disable_personification,
        "enable_stickers": enable_stickers,
        "disable_stickers": disable_stickers,
        "enable_tts": enable_tts,
        "disable_tts": disable_tts,
        "enable_schedule": enable_schedule,
        "view_config": view_config,
    }
