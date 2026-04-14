from collections.abc import Callable, Iterable
from typing import Any, Dict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg


def register_runtime_switch_matchers(
    *,
    superuser_permission: Any,
    logger: Any,
    handle_personification_help_command: Any,
    build_plugin_usage_text: Any,
    handle_install_remote_skill_command: Any,
    handle_global_switch_command: Any,
    handle_tts_global_switch_command: Any,
    handle_web_search_switch_command: Any,
    handle_proactive_switch_command: Any,
    handle_remote_skill_review_command: Any,
    handle_rebuild_plugin_knowledge_command: Any,
    handle_view_plugin_knowledge_status_command: Any,
    plugin_config: Any,
    knowledge_store: Any,
    agent_tool_caller: Any,
    start_knowledge_builder: Any,
    get_knowledge_build_task: Any,
    set_knowledge_build_task: Any,
    apply_global_switch: Any,
    apply_tts_global_switch: Any,
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

    personification_help_cmd = _register_command(
        "拟人帮助",
        aliases={"拟人命令", "拟人管理命令"},
        priority=5,
        block=True,
    )

    @personification_help_cmd.handle()
    async def _handle_personification_help(_bot: Bot, _event: MessageEvent):
        await handle_personification_help_command(
            personification_help_cmd,
            build_plugin_usage_text=build_plugin_usage_text,
        )

    install_remote_skill_cmd = _register_command(
        "安装远程技能",
        aliases={"添加远程技能"},
        permission=superuser_permission,
        priority=5,
        block=True,
    )

    @install_remote_skill_cmd.handle()
    async def _handle_install_remote_skill(_bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
        await handle_install_remote_skill_command(
            install_remote_skill_cmd,
            arg_text=arg.extract_plain_text().strip(),
            plugin_config=plugin_config,
            save_plugin_runtime_config=save_plugin_runtime_config,
            logger=logger,
            operator_user_id=event.get_user_id(),
        )

    global_switch_cmd = _register_command("拟人开关", permission=superuser_permission, priority=5, block=True)

    @global_switch_cmd.handle()
    async def _handle_global_switch(_bot: Bot, _event: MessageEvent, arg: Message = CommandArg()):
        await handle_global_switch_command(
            global_switch_cmd,
            action=arg.extract_plain_text().strip(),
            plugin_config=plugin_config,
            apply_global_switch=apply_global_switch,
            save_plugin_runtime_config=save_plugin_runtime_config,
        )

    tts_global_switch_cmd = _register_command("拟人语音", permission=superuser_permission, priority=5, block=True)

    @tts_global_switch_cmd.handle()
    async def _handle_tts_global_switch(_bot: Bot, _event: MessageEvent, arg: Message = CommandArg()):
        await handle_tts_global_switch_command(
            tts_global_switch_cmd,
            action=arg.extract_plain_text().strip(),
            plugin_config=plugin_config,
            apply_tts_global_switch=apply_tts_global_switch,
            save_plugin_runtime_config=save_plugin_runtime_config,
        )

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

    remote_skill_review_cmd = _register_command(
        "远程技能审批",
        permission=superuser_permission,
        priority=5,
        block=True,
    )

    @remote_skill_review_cmd.handle()
    async def _handle_remote_skill_review(_bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
        await handle_remote_skill_review_command(
            remote_skill_review_cmd,
            action_text=arg.extract_plain_text().strip(),
            plugin_config=plugin_config,
            logger=logger,
            operator_user_id=event.get_user_id(),
        )

    rebuild_plugin_knowledge_cmd = _register_command(
        "重建插件知识库",
        aliases={"刷新插件知识库"},
        permission=superuser_permission,
        priority=5,
        block=True,
    )

    @rebuild_plugin_knowledge_cmd.handle()
    async def _handle_rebuild_plugin_knowledge(_bot: Bot, _event: MessageEvent):
        await handle_rebuild_plugin_knowledge_command(
            rebuild_plugin_knowledge_cmd,
            plugin_config=plugin_config,
            knowledge_store=knowledge_store,
            tool_caller=agent_tool_caller,
            logger=logger,
            start_knowledge_builder=start_knowledge_builder,
            get_knowledge_build_task=get_knowledge_build_task,
            set_knowledge_build_task=set_knowledge_build_task,
        )

    plugin_knowledge_status_cmd = _register_command(
        "插件知识库状态",
        aliases={"查看插件知识库状态"},
        permission=superuser_permission,
        priority=5,
        block=True,
    )

    @plugin_knowledge_status_cmd.handle()
    async def _handle_plugin_knowledge_status(_bot: Bot, _event: MessageEvent):
        await handle_view_plugin_knowledge_status_command(
            plugin_knowledge_status_cmd,
            knowledge_store=knowledge_store,
            get_knowledge_build_task=get_knowledge_build_task,
        )

    return {
        "personification_help_cmd": personification_help_cmd,
        "install_remote_skill_cmd": install_remote_skill_cmd,
        "global_switch_cmd": global_switch_cmd,
        "tts_global_switch_cmd": tts_global_switch_cmd,
        "web_search_cmd": web_search_cmd,
        "proactive_msg_switch_cmd": proactive_msg_switch_cmd,
        "remote_skill_review_cmd": remote_skill_review_cmd,
        "rebuild_plugin_knowledge_cmd": rebuild_plugin_knowledge_cmd,
        "plugin_knowledge_status_cmd": plugin_knowledge_status_cmd,
    }
