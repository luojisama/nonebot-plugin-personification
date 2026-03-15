import asyncio
from dataclasses import dataclass
from typing import Any, Dict

from .admin_commands import (
    handle_group_fav_query_command,
    handle_group_feature_switch_command,
    handle_reset_persona_command,
    handle_set_group_fav_command,
    handle_set_persona_command,
    handle_view_config_command,
    handle_view_persona_command,
)
from .admin_helpers import (
    build_group_fav_markdown,
    build_group_fav_text,
    build_view_config_nodes,
    parse_group_fav_update_args,
    parse_persona_update_args,
)
from .admin_matchers import register_admin_matchers
from .chat_matchers import register_chat_matchers
from .diary_matchers import register_diary_matchers
from .event_rules import (
    personification_rule,
    poke_notice_rule,
    poke_rule,
    record_msg_rule,
    resolve_record_message,
    split_text_into_segments,
    sticker_chat_rule,
)
from .moderation_handlers import (
    extract_target_user_id,
    handle_perm_blacklist_set_command,
    handle_schedule_switch_command,
)
from .perm_blacklist_matchers import register_perm_blacklist_matchers
from .record_message_handler import handle_record_message_event
from .reply_buffer import handle_reply_event, run_buffer_timer
from .reply_matchers import register_reply_matchers
from .reply_processor import (
    PersonaDeps,
    ReplyProcessorDeps,
    RuntimeDeps,
    SessionDeps,
    TypeDeps,
    process_response_logic,
)
from .rule_builders import (
    build_personification_rule,
    build_poke_notice_rule,
    build_poke_rule,
)
from .runtime_commands import (
    handle_clear_context_command,
    handle_proactive_switch_command,
    handle_web_search_switch_command,
)
from .runtime_matchers import register_runtime_switch_matchers
from .sticker_chat_handler import handle_sticker_chat_event
from .style_context_matchers import register_style_context_matchers
from .style_diary_handlers import (
    handle_background_style_analysis,
    handle_learn_style_command,
    handle_manual_diary_command,
    handle_view_style_command,
)
from .whitelist_handlers import (
    handle_add_whitelist_command,
    handle_agree_whitelist_command,
    handle_apply_whitelist_command,
    handle_reject_whitelist_command,
    handle_remove_whitelist_command,
)
from .whitelist_matchers import register_whitelist_matchers
from .yaml_response_handler import build_yaml_response_processor, process_yaml_response_logic


@dataclass
class MatcherSetupDeps:
    personification_rule: Any
    poke_notice_rule: Any
    record_msg_rule_core: Any
    sticker_chat_rule_core: Any
    process_response_logic_core: Any
    reply_processor_deps: ReplyProcessorDeps
    handle_reply_event_core: Any
    run_buffer_timer_core: Any
    msg_buffer: Dict[str, Dict[str, Any]]
    poke_event_cls: Any
    message_event_cls: Any
    group_message_event_cls: Any
    private_message_event_cls: Any
    message_cls: Any
    message_segment_cls: Any
    logger: Any
    is_group_whitelisted: Any
    plugin_config: Any
    superuser_permission: Any
    superusers: set[str]
    sign_in_available: bool
    md_to_pic: Any
    finished_exception_cls: Any
    register_private_command_keywords: Any
    clear_private_command_keywords: Any
    add_request: Any
    add_group_to_whitelist: Any
    update_request_status: Any
    remove_group_from_whitelist: Any
    handle_apply_whitelist_command: Any
    handle_agree_whitelist_command: Any
    handle_reject_whitelist_command: Any
    handle_add_whitelist_command: Any
    handle_remove_whitelist_command: Any
    handle_group_fav_query_command: Any
    get_user_data: Any
    get_level_name: Any
    build_group_fav_markdown: Any
    build_group_fav_text: Any
    handle_set_group_fav_command: Any
    parse_group_fav_update_args: Any
    update_user_data: Any
    handle_set_persona_command: Any
    parse_persona_update_args: Any
    set_group_prompt: Any
    handle_view_persona_command: Any
    load_prompt: Any
    handle_reset_persona_command: Any
    handle_group_feature_switch_command: Any
    set_group_enabled: Any
    set_group_sticker_enabled: Any
    handle_schedule_switch_command: Any
    save_plugin_runtime_config: Any
    set_group_schedule_enabled: Any
    bot_statuses: Dict[str, str]
    handle_view_config_command: Any
    get_group_config: Any
    get_configured_api_providers: Any
    build_view_config_nodes: Any
    session_history_limit: int
    handle_record_message_event: Any
    resolve_record_message: Any
    get_custom_title: Any
    record_group_msg: Any
    handle_background_style_analysis: Any
    analyze_group_style_flow: Any
    set_group_style: Any
    clear_group_msgs: Any
    handle_sticker_chat_event: Any
    handle_perm_blacklist_set_command: Any
    collect_perm_blacklist_items: Any
    build_perm_blacklist_card_markdown: Any
    build_perm_blacklist_text: Any
    load_data: Any
    handle_manual_diary_command: Any
    qzone_publish_available: bool
    generate_ai_diary: Any
    publish_qzone_shuo: Any
    handle_web_search_switch_command: Any
    handle_proactive_switch_command: Any
    apply_web_search_switch: Any
    apply_proactive_switch: Any
    handle_learn_style_command: Any
    handle_view_style_command: Any
    handle_clear_context_command: Any
    get_recent_group_msgs: Any
    call_ai_api: Any
    get_group_style: Any
    chat_histories: Any
    save_session_histories: Any
    get_driver: Any
    build_private_session_id: Any
    build_group_session_id: Any
    is_global_clear_command: Any
    clear_all_context: Any
    resolve_clear_target: Any
    clear_message_buffer: Any
    clear_session_context: Any


def setup_all_matchers(*, deps: MatcherSetupDeps) -> Dict[str, Any]:
    async def _process_response_logic(bot: Any, event: Any, state: Dict[str, Any]) -> None:
        await deps.process_response_logic_core(bot, event, state, deps.reply_processor_deps)

    deps.clear_private_command_keywords()

    reply_matchers = register_reply_matchers(
        personification_rule=deps.personification_rule,
        poke_notice_rule=deps.poke_notice_rule,
        handle_reply_event=deps.handle_reply_event_core,
        process_response_logic=_process_response_logic,
        msg_buffer=deps.msg_buffer,
        run_buffer_timer=deps.run_buffer_timer_core,
        poke_event_cls=deps.poke_event_cls,
        message_event_cls=deps.message_event_cls,
        group_message_event_cls=deps.group_message_event_cls,
        message_cls=deps.message_cls,
        message_segment_cls=deps.message_segment_cls,
        logger=deps.logger,
    )
    handle_reply = reply_matchers["handle_reply"]

    async def _record_msg_rule(event: Any) -> bool:
        return await deps.record_msg_rule_core(event)

    async def _sticker_chat_rule(event: Any) -> bool:
        return await deps.sticker_chat_rule_core(
            event,
            is_group_whitelisted=deps.is_group_whitelisted,
            plugin_whitelist=deps.plugin_config.personification_whitelist,
            probability=deps.plugin_config.personification_sticker_probability,
        )

    chat_matchers = register_chat_matchers(
        record_msg_rule=_record_msg_rule,
        sticker_chat_rule=_sticker_chat_rule,
        handle_record_message_event=deps.handle_record_message_event,
        resolve_record_message=deps.resolve_record_message,
        get_custom_title=deps.get_custom_title,
        record_group_msg=deps.record_group_msg,
        logger=deps.logger,
        create_background_task=lambda group_id: asyncio.create_task(
            deps.handle_background_style_analysis(
                group_id=group_id,
                analyze_group_style=deps.analyze_group_style_flow,
                set_group_style=deps.set_group_style,
                clear_group_msgs=deps.clear_group_msgs,
                logger=deps.logger,
            )
        ),
        handle_sticker_chat_event=deps.handle_sticker_chat_event,
        get_group_config=deps.get_group_config,
        sticker_path=deps.plugin_config.personification_sticker_path,
        message_segment_cls=deps.message_segment_cls,
        handle_reply=handle_reply,
    )

    whitelist_matchers = register_whitelist_matchers(
        superuser_permission=deps.superuser_permission,
        plugin_whitelist=deps.plugin_config.personification_whitelist,
        is_group_whitelisted=deps.is_group_whitelisted,
        add_request=deps.add_request,
        superusers=deps.superusers,
        add_group_to_whitelist=deps.add_group_to_whitelist,
        update_request_status=deps.update_request_status,
        remove_group_from_whitelist=deps.remove_group_from_whitelist,
        handle_apply_whitelist_command=deps.handle_apply_whitelist_command,
        handle_agree_whitelist_command=deps.handle_agree_whitelist_command,
        handle_reject_whitelist_command=deps.handle_reject_whitelist_command,
        handle_add_whitelist_command=deps.handle_add_whitelist_command,
        handle_remove_whitelist_command=deps.handle_remove_whitelist_command,
        logger=deps.logger,
        track_command_keywords=deps.register_private_command_keywords,
    )

    admin_matchers = register_admin_matchers(
        superuser_permission=deps.superuser_permission,
        sign_in_available=deps.sign_in_available,
        handle_group_fav_query_command=deps.handle_group_fav_query_command,
        get_user_data=deps.get_user_data,
        get_level_name=deps.get_level_name,
        build_group_fav_markdown=deps.build_group_fav_markdown,
        build_group_fav_text=deps.build_group_fav_text,
        md_to_pic=deps.md_to_pic,
        message_segment_cls=deps.message_segment_cls,
        finished_exception_cls=deps.finished_exception_cls,
        logger=deps.logger,
        handle_set_group_fav_command=deps.handle_set_group_fav_command,
        parse_group_fav_update_args=deps.parse_group_fav_update_args,
        update_user_data=deps.update_user_data,
        group_message_event_cls=deps.group_message_event_cls,
        handle_set_persona_command=deps.handle_set_persona_command,
        parse_persona_update_args=deps.parse_persona_update_args,
        set_group_prompt=deps.set_group_prompt,
        handle_view_persona_command=deps.handle_view_persona_command,
        load_prompt=deps.load_prompt,
        handle_reset_persona_command=deps.handle_reset_persona_command,
        handle_group_feature_switch_command=deps.handle_group_feature_switch_command,
        set_group_enabled=deps.set_group_enabled,
        set_group_sticker_enabled=deps.set_group_sticker_enabled,
        handle_schedule_switch_command=deps.handle_schedule_switch_command,
        plugin_config=deps.plugin_config,
        save_plugin_runtime_config=deps.save_plugin_runtime_config,
        set_group_schedule_enabled=deps.set_group_schedule_enabled,
        bot_statuses=deps.bot_statuses,
        handle_view_config_command=deps.handle_view_config_command,
        get_group_config=deps.get_group_config,
        get_configured_api_providers=deps.get_configured_api_providers,
        build_view_config_nodes=deps.build_view_config_nodes,
        session_history_limit=deps.session_history_limit,
        track_command_keywords=deps.register_private_command_keywords,
    )

    perm_blacklist_matchers = register_perm_blacklist_matchers(
        superuser_permission=deps.superuser_permission,
        sign_in_available=deps.sign_in_available,
        handle_perm_blacklist_set_command=deps.handle_perm_blacklist_set_command,
        update_user_data=deps.update_user_data,
        collect_perm_blacklist_items=deps.collect_perm_blacklist_items,
        build_perm_blacklist_card_markdown=deps.build_perm_blacklist_card_markdown,
        build_perm_blacklist_text=deps.build_perm_blacklist_text,
        load_data=deps.load_data,
        md_to_pic=deps.md_to_pic,
        message_segment_cls=deps.message_segment_cls,
        finished_exception_cls=deps.finished_exception_cls,
        logger=deps.logger,
        track_command_keywords=deps.register_private_command_keywords,
    )

    diary_matchers = register_diary_matchers(
        superuser_permission=deps.superuser_permission,
        handle_manual_diary_command=deps.handle_manual_diary_command,
        qzone_publish_available=deps.qzone_publish_available,
        generate_ai_diary=deps.generate_ai_diary,
        publish_qzone_shuo=deps.publish_qzone_shuo,
        track_command_keywords=deps.register_private_command_keywords,
    )

    runtime_switch_matchers = register_runtime_switch_matchers(
        superuser_permission=deps.superuser_permission,
        handle_web_search_switch_command=deps.handle_web_search_switch_command,
        handle_proactive_switch_command=deps.handle_proactive_switch_command,
        plugin_config=deps.plugin_config,
        apply_web_search_switch=deps.apply_web_search_switch,
        apply_proactive_switch=deps.apply_proactive_switch,
        save_plugin_runtime_config=deps.save_plugin_runtime_config,
        track_command_keywords=deps.register_private_command_keywords,
    )

    style_context_matchers = register_style_context_matchers(
        superuser_permission=deps.superuser_permission,
        handle_learn_style_command=deps.handle_learn_style_command,
        handle_view_style_command=deps.handle_view_style_command,
        handle_clear_context_command=deps.handle_clear_context_command,
        analyze_group_style_flow=deps.analyze_group_style_flow,
        get_recent_group_msgs=deps.get_recent_group_msgs,
        call_ai_api=deps.call_ai_api,
        set_group_style=deps.set_group_style,
        clear_group_msgs=deps.clear_group_msgs,
        logger=deps.logger,
        finished_exception_cls=deps.finished_exception_cls,
        get_group_style=deps.get_group_style,
        chat_histories=deps.chat_histories,
        msg_buffer=deps.msg_buffer,
        save_session_histories=deps.save_session_histories,
        get_driver=deps.get_driver,
        build_private_session_id=deps.build_private_session_id,
        build_group_session_id=deps.build_group_session_id,
        is_global_clear_command=deps.is_global_clear_command,
        clear_all_context=deps.clear_all_context,
        resolve_clear_target=deps.resolve_clear_target,
        clear_message_buffer=deps.clear_message_buffer,
        clear_session_context=deps.clear_session_context,
        group_message_event_cls=deps.group_message_event_cls,
        private_message_event_cls=deps.private_message_event_cls,
        track_command_keywords=deps.register_private_command_keywords,
    )

    return {
        "reply_matcher": reply_matchers["reply_matcher"],
        "poke_notice_matcher": reply_matchers["poke_notice_matcher"],
        "handle_reply": reply_matchers["handle_reply"],
        "record_msg_matcher": chat_matchers["record_msg_matcher"],
        "sticker_chat_matcher": chat_matchers["sticker_chat_matcher"],
        "apply_whitelist": whitelist_matchers["apply_whitelist"],
        "agree_whitelist": whitelist_matchers["agree_whitelist"],
        "reject_whitelist": whitelist_matchers["reject_whitelist"],
        "add_whitelist": whitelist_matchers["add_whitelist"],
        "remove_whitelist": whitelist_matchers["remove_whitelist"],
        "group_fav_query": admin_matchers["group_fav_query"],
        "set_group_fav": admin_matchers["set_group_fav"],
        "set_persona": admin_matchers["set_persona"],
        "view_persona": admin_matchers["view_persona"],
        "reset_persona": admin_matchers["reset_persona"],
        "enable_personification": admin_matchers["enable_personification"],
        "disable_personification": admin_matchers["disable_personification"],
        "enable_stickers": admin_matchers["enable_stickers"],
        "disable_stickers": admin_matchers["disable_stickers"],
        "enable_schedule": admin_matchers["enable_schedule"],
        "view_config": admin_matchers["view_config"],
        "perm_blacklist_add": perm_blacklist_matchers["perm_blacklist_add"],
        "perm_blacklist_del": perm_blacklist_matchers["perm_blacklist_del"],
        "perm_blacklist_list": perm_blacklist_matchers["perm_blacklist_list"],
        "manual_diary_cmd": diary_matchers["manual_diary_cmd"],
        "web_search_cmd": runtime_switch_matchers["web_search_cmd"],
        "proactive_msg_switch_cmd": runtime_switch_matchers["proactive_msg_switch_cmd"],
        "clear_context_cmd": style_context_matchers["clear_context_cmd"],
        "learn_style_cmd": style_context_matchers["learn_style_cmd"],
        "view_style_cmd": style_context_matchers["view_style_cmd"],
    }


__all__ = [
    "build_group_fav_markdown",
    "build_group_fav_text",
    "build_view_config_nodes",
    "register_admin_matchers",
    "personification_rule",
    "build_personification_rule",
    "poke_notice_rule",
    "build_poke_notice_rule",
    "poke_rule",
    "build_poke_rule",
    "extract_target_user_id",
    "record_msg_rule",
    "resolve_record_message",
    "split_text_into_segments",
    "sticker_chat_rule",
    "handle_background_style_analysis",
    "handle_clear_context_command",
    "handle_group_fav_query_command",
    "handle_group_feature_switch_command",
    "handle_add_whitelist_command",
    "handle_agree_whitelist_command",
    "handle_apply_whitelist_command",
    "register_whitelist_matchers",
    "handle_learn_style_command",
    "register_diary_matchers",
    "handle_manual_diary_command",
    "handle_reset_persona_command",
    "handle_proactive_switch_command",
    "register_runtime_switch_matchers",
    "register_perm_blacklist_matchers",
    "handle_set_group_fav_command",
    "handle_set_persona_command",
    "handle_perm_blacklist_set_command",
    "handle_reject_whitelist_command",
    "handle_remove_whitelist_command",
    "handle_schedule_switch_command",
    "handle_web_search_switch_command",
    "handle_view_config_command",
    "handle_view_persona_command",
    "handle_view_style_command",
    "register_style_context_matchers",
    "build_yaml_response_processor",
    "process_yaml_response_logic",
    "SessionDeps",
    "PersonaDeps",
    "RuntimeDeps",
    "TypeDeps",
    "ReplyProcessorDeps",
    "process_response_logic",
    "handle_reply_event",
    "run_buffer_timer",
    "register_reply_matchers",
    "handle_sticker_chat_event",
    "handle_record_message_event",
    "register_chat_matchers",
    "parse_group_fav_update_args",
    "parse_persona_update_args",
    "MatcherSetupDeps",
    "setup_all_matchers",
]
