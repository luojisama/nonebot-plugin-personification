from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg


def register_perm_blacklist_matchers(
    *,
    superuser_permission: Any,
    sign_in_available: bool,
    handle_perm_blacklist_set_command: Any,
    update_user_data: Any,
    collect_perm_blacklist_items: Any,
    build_perm_blacklist_card_markdown: Any,
    build_perm_blacklist_text: Any,
    load_data: Any,
    md_to_pic: Any,
    message_segment_cls: Any,
    finished_exception_cls: Any,
    logger: Any,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> Dict[str, Any]:
    def _register_command(command: str, *, aliases: set[str] | None = None, **kwargs: Any) -> Any:
        if track_command_keywords:
            track_command_keywords(command, aliases)
        if aliases is None:
            return on_command(command, **kwargs)
        return on_command(command, aliases=aliases, **kwargs)

    perm_blacklist_add = _register_command("永久拉黑", permission=superuser_permission, priority=5, block=True)

    @perm_blacklist_add.handle()
    async def _handle_perm_blacklist_add(_bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await handle_perm_blacklist_set_command(
            perm_blacklist_add,
            sign_in_available=sign_in_available,
            args_text=args.extract_plain_text().strip(),
            message=event.get_message(),
            update_user_data=update_user_data,
            set_blacklisted=True,
        )

    perm_blacklist_del = _register_command("取消永久拉黑", permission=superuser_permission, priority=5, block=True)

    @perm_blacklist_del.handle()
    async def _handle_perm_blacklist_del(_bot: Bot, event: MessageEvent, args: Message = CommandArg()):
        await handle_perm_blacklist_set_command(
            perm_blacklist_del,
            sign_in_available=sign_in_available,
            args_text=args.extract_plain_text().strip(),
            message=event.get_message(),
            update_user_data=update_user_data,
            set_blacklisted=False,
        )

    perm_blacklist_list = _register_command("永久黑名单列表", permission=superuser_permission, priority=5, block=True)

    @perm_blacklist_list.handle()
    async def _handle_perm_blacklist_list(_bot: Bot, _event: MessageEvent):
        if not sign_in_available:
            await perm_blacklist_list.finish("签到插件未就绪，无法操作。")

        data = load_data()
        blacklisted_items = collect_perm_blacklist_items(data)
        if not blacklisted_items:
            await perm_blacklist_list.finish("目前没有永久黑名单用户。")

        md = build_perm_blacklist_card_markdown(blacklisted_items)
        if md_to_pic:
            try:
                pic = await md_to_pic(md, width=400)
                await perm_blacklist_list.finish(message_segment_cls.image(pic))
            except finished_exception_cls:
                raise
            except Exception as e:
                logger.error(f"渲染永久黑名单图片失败: {e}")

        await perm_blacklist_list.finish(build_perm_blacklist_text(blacklisted_items))

    return {
        "perm_blacklist_add": perm_blacklist_add,
        "perm_blacklist_del": perm_blacklist_del,
        "perm_blacklist_list": perm_blacklist_list,
    }
