from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg


def register_whitelist_matchers(
    *,
    superuser_permission: Any,
    plugin_whitelist: Any,
    is_group_whitelisted: Any,
    add_request: Any,
    superusers: Any,
    add_group_to_whitelist: Any,
    update_request_status: Any,
    remove_group_from_whitelist: Any,
    handle_apply_whitelist_command: Any,
    handle_agree_whitelist_command: Any,
    handle_reject_whitelist_command: Any,
    handle_add_whitelist_command: Any,
    handle_remove_whitelist_command: Any,
    logger: Any,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> Dict[str, Any]:
    def _register_command(command: str, *, aliases: set[str] | None = None, **kwargs: Any) -> Any:
        if track_command_keywords:
            track_command_keywords(command, aliases)
        if aliases is None:
            return on_command(command, **kwargs)
        return on_command(command, aliases=aliases, **kwargs)

    apply_whitelist = _register_command("申请白名单", priority=5, block=True)

    @apply_whitelist.handle()
    async def _handle_apply(bot: Bot, event: GroupMessageEvent):
        await handle_apply_whitelist_command(
            apply_whitelist,
            bot=bot,
            event=event,
            plugin_whitelist=plugin_whitelist,
            is_group_whitelisted=is_group_whitelisted,
            add_request=add_request,
            superusers=superusers,
            logger=logger,
        )

    agree_whitelist = _register_command("同意白名单", permission=superuser_permission, priority=5, block=True)

    @agree_whitelist.handle()
    async def _handle_agree(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        group_id = args.extract_plain_text().strip()
        await handle_agree_whitelist_command(
            agree_whitelist,
            bot=bot,
            operator_user_id=str(event.user_id),
            group_id=group_id,
            add_group_to_whitelist=add_group_to_whitelist,
            update_request_status=update_request_status,
            logger=logger,
        )

    reject_whitelist = _register_command("拒绝白名单", permission=superuser_permission, priority=5, block=True)

    @reject_whitelist.handle()
    async def _handle_reject(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        group_id = args.extract_plain_text().strip()
        await handle_reject_whitelist_command(
            reject_whitelist,
            bot=bot,
            operator_user_id=str(event.user_id),
            group_id=group_id,
            update_request_status=update_request_status,
            logger=logger,
        )

    add_whitelist = _register_command("添加白名单", permission=superuser_permission, priority=5, block=True)

    @add_whitelist.handle()
    async def _handle_add(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        group_id = args.extract_plain_text().strip()
        await handle_add_whitelist_command(
            add_whitelist,
            bot=bot,
            operator_user_id=str(event.user_id),
            group_id=group_id,
            add_group_to_whitelist=add_group_to_whitelist,
            update_request_status=update_request_status,
            logger=logger,
        )

    remove_whitelist = _register_command("移除白名单", permission=superuser_permission, priority=5, block=True)

    @remove_whitelist.handle()
    async def _handle_remove(args: Message = CommandArg()):
        group_id = args.extract_plain_text().strip()
        await handle_remove_whitelist_command(
            remove_whitelist,
            group_id=group_id,
            remove_group_from_whitelist=remove_group_from_whitelist,
        )

    return {
        "apply_whitelist": apply_whitelist,
        "agree_whitelist": agree_whitelist,
        "reject_whitelist": reject_whitelist,
        "add_whitelist": add_whitelist,
        "remove_whitelist": remove_whitelist,
    }
