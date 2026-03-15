from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg


def register_runtime_switch_matchers(
    *,
    superuser_permission: Any,
    handle_web_search_switch_command: Any,
    handle_proactive_switch_command: Any,
    plugin_config: Any,
    apply_web_search_switch: Any,
    apply_proactive_switch: Any,
    save_plugin_runtime_config: Any,
    track_command_keywords: Callable[[str, Iterable[str] | None], None] | None = None,
) -> Dict[str, Any]:
    def _register_command(command: str, *, aliases: set[str] | None = None, **kwargs: Any) -> Any:
        if track_command_keywords:
            track_command_keywords(command, aliases)
        if aliases is None:
            return on_command(command, **kwargs)
        return on_command(command, aliases=aliases, **kwargs)

    web_search_cmd = _register_command("拟人联网", permission=superuser_permission, priority=5, block=True)

    @web_search_cmd.handle()
    async def _handle_web_search(_bot: Bot, _event: MessageEvent, arg: Message = CommandArg()):
        await handle_web_search_switch_command(
            web_search_cmd,
            action=arg.extract_plain_text().strip(),
            plugin_config=plugin_config,
            apply_web_search_switch=apply_web_search_switch,
            save_plugin_runtime_config=save_plugin_runtime_config,
        )

    proactive_msg_switch_cmd = _register_command(
        "拟人主动消息",
        permission=superuser_permission,
        priority=5,
        block=True,
    )

    @proactive_msg_switch_cmd.handle()
    async def _handle_proactive(_bot: Bot, _event: MessageEvent, arg: Message = CommandArg()):
        await handle_proactive_switch_command(
            proactive_msg_switch_cmd,
            action=arg.extract_plain_text().strip(),
            plugin_config=plugin_config,
            apply_proactive_switch=apply_proactive_switch,
            save_plugin_runtime_config=save_plugin_runtime_config,
        )

    return {
        "web_search_cmd": web_search_cmd,
        "proactive_msg_switch_cmd": proactive_msg_switch_cmd,
    }
