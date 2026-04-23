from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from ..flows import FlowSetupDeps
from ..handlers import (
    MatcherSetupDeps,
    build_view_config_nodes,
    build_group_fav_markdown,
    build_group_fav_text,
    handle_add_whitelist_command,
    handle_agree_whitelist_command,
    handle_apply_whitelist_command,
    handle_background_style_analysis,
    handle_clear_plugin_knowledge_command,
    handle_clear_context_command,
    handle_delete_plugin_knowledge_command,
    handle_full_reset_memory_command,
    handle_global_switch_command,
    handle_group_fav_query_command,
    handle_group_feature_switch_command,
    handle_install_remote_skill_command,
    handle_learn_style_command,
    handle_manual_diary_command,
    handle_perm_blacklist_set_command,
    handle_personification_help_command,
    handle_proactive_switch_command,
    handle_reload_config_command,
    handle_rebuild_plugin_knowledge_command,
    handle_record_message_event,
    handle_remote_skill_review_command,
    handle_stats_command,
    handle_reject_whitelist_command,
    handle_remove_whitelist_command,
    handle_reply_event,
    handle_reset_persona_command,
    handle_schedule_switch_command,
    handle_set_group_fav_command,
    handle_set_persona_command,
    handle_sticker_chat_event,
    handle_tts_global_switch_command,
    handle_view_config_command,
    handle_view_plugin_knowledge_error_command,
    handle_view_persona_command,
    handle_view_plugin_knowledge_status_command,
    handle_view_style_command,
    handle_web_search_switch_command,
    parse_group_fav_update_args,
    parse_persona_update_args,
    process_response_logic as process_response_logic_core,
    record_msg_rule as record_msg_rule_core,
    resolve_record_message,
    run_buffer_timer as run_buffer_timer_core,
    sticker_chat_rule as sticker_chat_rule_core,
)
from ..jobs import JobSetupDeps
from ..schedule import get_activity_status, get_current_local_time, is_rest_time
from ..utils import (
    add_group_to_whitelist,
    add_request,
    clear_group_msgs,
    get_group_config,
    get_group_style,
    get_recent_group_msgs,
    is_group_whitelisted,
    load_whitelist,
    record_group_msg,
    remove_group_from_whitelist,
    set_group_enabled,
    set_group_prompt,
    set_group_schedule_enabled,
    set_group_sticker_enabled,
    set_group_tts_enabled,
    set_group_style,
    should_trigger_group_style_analysis,
    update_request_status,
)
from ..agent.inner_state import get_personification_data_dir
from .context_cleanup import (
    clear_all_context,
    clear_message_buffer,
    clear_session_context,
    is_global_clear_command,
    resolve_clear_target,
)
from .context_policy import clear_private_command_keywords, register_private_command_keywords
from .plugin_meta import build_plugin_usage_text
from .proactive_store import load_proactive_state, save_proactive_state
from .session_store import (
    SESSION_HISTORY_LIMIT,
    build_group_session_id,
    build_private_session_id,
    chat_histories,
    save_session_histories,
)


@dataclass
class PluginRuntimeBundle:
    plugin_meta: Any
    plugin_config: Any
    superusers: set[str]
    logger: Any
    get_driver: Callable[[], Any]
    get_bots: Callable[[], dict[str, Any]]
    superuser_permission: Any
    finished_exception_cls: Any
    group_message_event_cls: Any
    private_message_event_cls: Any
    message_event_cls: Any
    poke_event_cls: Any
    message_cls: Any
    message_segment_cls: Any
    md_to_pic: Any
    sign_in_available: bool
    qzone_publish_available: bool
    publish_qzone_shuo: Any
    update_qzone_cookie: Any
    get_user_data: Any
    update_user_data: Any
    load_data: Any
    get_level_name: Any
    bot_statuses: Dict[str, str]
    user_blacklist: Dict[str, float]
    msg_buffer: Dict[str, Dict[str, Any]]
    load_prompt: Any
    call_ai_api: Any
    call_style_ai_api: Any
    get_configured_api_providers: Any
    save_plugin_runtime_config: Any
    reply_processor_deps: Any
    personification_rule: Any
    poke_rule: Any
    poke_notice_rule: Any
    load_plugin_runtime_config: Any = None
    reload_runtime_services: Any = None
    tts_service: Any = None
    tool_registry: Any = None
    persona_store: Any = None
    memory_store: Any = None
    profile_service: Any = None
    memory_curator: Any = None
    memory_decay_scheduler: Any = None
    background_intelligence: Any = None
    get_knowledge_build_task: Any = None
    set_knowledge_build_task: Any = None

    def make_flow_setup_deps(self) -> FlowSetupDeps:
        return FlowSetupDeps(
            plugin_config=self.plugin_config,
            sign_in_available=self.sign_in_available,
            is_rest_time=is_rest_time,
            get_bots=self.get_bots,
            load_data=self.load_data,
            load_proactive_state=load_proactive_state,
            save_proactive_state=save_proactive_state,
            get_user_data=self.get_user_data,
            get_level_name=self.get_level_name,
            get_now=get_current_local_time,
            get_activity_status=get_activity_status,
            load_prompt=self.load_prompt,
            call_ai_api=self.call_ai_api,
            parse_yaml_response=self.parse_yaml_response,
            logger=self.logger,
            agent_tool_caller=self.reply_processor_deps.runtime.agent_tool_caller,
            agent_data_dir=get_personification_data_dir(self.plugin_config),
            persona_store=self.persona_store,
            superusers=self.superusers,
            get_recent_group_msgs=get_recent_group_msgs,
            get_group_style=get_group_style,
            get_whitelisted_groups=self._get_whitelisted_groups,
            record_group_msg=record_group_msg,
            build_grounding_context=self.reply_processor_deps.runtime.build_grounding_context,
        )

    def _get_whitelisted_groups(self) -> list[str]:
        static = list(self.plugin_config.personification_whitelist or [])
        try:
            dynamic = list(load_whitelist() or [])
        except Exception:
            dynamic = []

        seen: set[str] = set()
        result: list[str] = []
        for gid in static + dynamic:
            gid = str(gid)
            if gid not in seen:
                seen.add(gid)
                result.append(gid)
        return result

    @property
    def parse_yaml_response(self) -> Any:
        from ..flows import parse_yaml_response

        return parse_yaml_response

    @property
    def generate_ai_diary_flow(self) -> Any:
        from ..flows import generate_ai_diary

        return generate_ai_diary

    @property
    def maybe_generate_proactive_qzone_post_flow(self) -> Any:
        from ..flows import maybe_generate_proactive_qzone_post

        return maybe_generate_proactive_qzone_post

    @property
    def collect_perm_blacklist_items(self) -> Any:
        from ..flows import collect_perm_blacklist_items

        return collect_perm_blacklist_items

    @property
    def build_perm_blacklist_card_markdown(self) -> Any:
        from ..flows import build_perm_blacklist_card_markdown

        return build_perm_blacklist_card_markdown

    @property
    def build_perm_blacklist_text(self) -> Any:
        from ..flows import build_perm_blacklist_text

        return build_perm_blacklist_text

    @property
    def analyze_group_style_flow(self) -> Any:
        from ..flows import analyze_group_style

        return analyze_group_style

    def make_job_setup_deps(
        self,
        *,
        check_proactive_messaging: Any,
        check_group_idle_topic: Any = None,
    ) -> JobSetupDeps:
        return JobSetupDeps(
            sign_in_available=self.sign_in_available,
            load_data=self.load_data,
            get_now=get_current_local_time,
            get_bots=self.get_bots,
            superusers=self.superusers,
            logger=self.logger,
            generate_ai_diary_flow=self.generate_ai_diary_flow,
            load_prompt=self.load_prompt,
            call_ai_api=self.call_ai_api,
            qzone_publish_available=self.qzone_publish_available,
            update_qzone_cookie=self.update_qzone_cookie,
            publish_qzone_shuo=self.publish_qzone_shuo,
            maybe_generate_proactive_qzone_post_flow=self.maybe_generate_proactive_qzone_post_flow,
            check_proactive_messaging=check_proactive_messaging,
            proactive_interval_minutes=self.plugin_config.personification_proactive_interval,
            proactive_enabled=bool(getattr(self.plugin_config, "personification_proactive_enabled", True)),
            check_group_idle_topic=check_group_idle_topic,
            group_idle_enabled=bool(getattr(self.plugin_config, "personification_group_idle_enabled", False)),
            group_idle_check_interval_minutes=getattr(
                self.plugin_config,
                "personification_group_idle_check_interval",
                15,
            ),
            qzone_proactive_enabled=bool(
                getattr(self.plugin_config, "personification_qzone_proactive_enabled", False)
            ),
            qzone_check_interval_minutes=int(
                getattr(self.plugin_config, "personification_qzone_check_interval", 180)
            ),
            qzone_daily_limit=int(getattr(self.plugin_config, "personification_qzone_daily_limit", 2)),
            qzone_probability=float(getattr(self.plugin_config, "personification_qzone_probability", 0.35)),
            qzone_min_interval_hours=float(
                getattr(self.plugin_config, "personification_qzone_min_interval_hours", 8.0)
            ),
            agent_tool_caller=self.reply_processor_deps.runtime.agent_tool_caller,
            agent_data_dir=get_personification_data_dir(self.plugin_config),
            background_intelligence=self.background_intelligence,
        )

    def make_matcher_setup_deps(
        self,
        *,
        generate_ai_diary: Any,
        apply_global_switch: Any,
        apply_tts_global_switch: Any,
        apply_web_search_switch: Any,
        apply_proactive_switch: Any,
        start_knowledge_builder: Any,
        get_knowledge_build_task: Any,
        set_knowledge_build_task: Any,
    ) -> MatcherSetupDeps:
        return MatcherSetupDeps(
            runtime_bundle=self,
            personification_rule=self.personification_rule,
            poke_notice_rule=self.poke_notice_rule,
            record_msg_rule_core=record_msg_rule_core,
            sticker_chat_rule_core=sticker_chat_rule_core,
            process_response_logic_core=process_response_logic_core,
            reply_processor_deps=self.reply_processor_deps,
            handle_reply_event_core=handle_reply_event,
            run_buffer_timer_core=run_buffer_timer_core,
            msg_buffer=self.msg_buffer,
            poke_event_cls=self.poke_event_cls,
            message_event_cls=self.message_event_cls,
            group_message_event_cls=self.group_message_event_cls,
            private_message_event_cls=self.private_message_event_cls,
            message_cls=self.message_cls,
            message_segment_cls=self.message_segment_cls,
            logger=self.logger,
            is_group_whitelisted=is_group_whitelisted,
            plugin_config=self.plugin_config,
            superuser_permission=self.superuser_permission,
            superusers=self.superusers,
            sign_in_available=self.sign_in_available,
            md_to_pic=self.md_to_pic,
            finished_exception_cls=self.finished_exception_cls,
            register_private_command_keywords=register_private_command_keywords,
            clear_private_command_keywords=clear_private_command_keywords,
            add_request=add_request,
            add_group_to_whitelist=add_group_to_whitelist,
            update_request_status=update_request_status,
            remove_group_from_whitelist=remove_group_from_whitelist,
            handle_apply_whitelist_command=handle_apply_whitelist_command,
            handle_agree_whitelist_command=handle_agree_whitelist_command,
            handle_reject_whitelist_command=handle_reject_whitelist_command,
            handle_add_whitelist_command=handle_add_whitelist_command,
            handle_remove_whitelist_command=handle_remove_whitelist_command,
            handle_group_fav_query_command=handle_group_fav_query_command,
            get_user_data=self.get_user_data,
            get_level_name=self.get_level_name,
            build_group_fav_markdown=build_group_fav_markdown,
            build_group_fav_text=build_group_fav_text,
            handle_set_group_fav_command=handle_set_group_fav_command,
            parse_group_fav_update_args=parse_group_fav_update_args,
            update_user_data=self.update_user_data,
            handle_set_persona_command=handle_set_persona_command,
            parse_persona_update_args=parse_persona_update_args,
            set_group_prompt=set_group_prompt,
            handle_view_persona_command=handle_view_persona_command,
            load_prompt=self.load_prompt,
            handle_reset_persona_command=handle_reset_persona_command,
            handle_group_feature_switch_command=handle_group_feature_switch_command,
            set_group_enabled=set_group_enabled,
            set_group_sticker_enabled=set_group_sticker_enabled,
            set_group_tts_enabled=set_group_tts_enabled,
            handle_schedule_switch_command=handle_schedule_switch_command,
            save_plugin_runtime_config=self.save_plugin_runtime_config,
            set_group_schedule_enabled=set_group_schedule_enabled,
            bot_statuses=self.bot_statuses,
            handle_view_config_command=handle_view_config_command,
            get_group_config=get_group_config,
            get_configured_api_providers=self.get_configured_api_providers,
            build_view_config_nodes=build_view_config_nodes,
            session_history_limit=int(
                getattr(self.plugin_config, "personification_history_len", SESSION_HISTORY_LIMIT) or SESSION_HISTORY_LIMIT
            ),
            handle_record_message_event=handle_record_message_event,
            resolve_record_message=resolve_record_message,
            get_custom_title=self.reply_processor_deps.persona.get_custom_title,
            record_group_msg=record_group_msg,
            should_trigger_group_style_analysis=should_trigger_group_style_analysis,
            handle_background_style_analysis=handle_background_style_analysis,
            analyze_group_style_flow=self.analyze_group_style_flow,
            set_group_style=set_group_style,
            clear_group_msgs=clear_group_msgs,
            handle_sticker_chat_event=handle_sticker_chat_event,
            handle_perm_blacklist_set_command=handle_perm_blacklist_set_command,
            collect_perm_blacklist_items=self.collect_perm_blacklist_items,
            build_perm_blacklist_card_markdown=self.build_perm_blacklist_card_markdown,
            build_perm_blacklist_text=self.build_perm_blacklist_text,
            load_data=self.load_data,
            handle_manual_diary_command=handle_manual_diary_command,
            qzone_publish_available=self.qzone_publish_available,
            update_qzone_cookie=self.update_qzone_cookie,
            generate_ai_diary=generate_ai_diary,
            publish_qzone_shuo=self.publish_qzone_shuo,
            handle_web_search_switch_command=handle_web_search_switch_command,
            handle_proactive_switch_command=handle_proactive_switch_command,
            handle_remote_skill_review_command=handle_remote_skill_review_command,
            handle_rebuild_plugin_knowledge_command=handle_rebuild_plugin_knowledge_command,
            handle_delete_plugin_knowledge_command=handle_delete_plugin_knowledge_command,
            handle_clear_plugin_knowledge_command=handle_clear_plugin_knowledge_command,
            handle_view_plugin_knowledge_status_command=handle_view_plugin_knowledge_status_command,
            handle_view_plugin_knowledge_error_command=handle_view_plugin_knowledge_error_command,
            handle_personification_help_command=handle_personification_help_command,
            handle_reload_config_command=handle_reload_config_command,
            handle_stats_command=handle_stats_command,
            handle_install_remote_skill_command=handle_install_remote_skill_command,
            build_plugin_usage_text=build_plugin_usage_text,
            handle_global_switch_command=handle_global_switch_command,
            handle_tts_global_switch_command=handle_tts_global_switch_command,
            load_plugin_runtime_config=self.load_plugin_runtime_config,
            reload_runtime_services=self.reload_runtime_services,
            apply_web_search_switch=apply_web_search_switch,
            apply_proactive_switch=apply_proactive_switch,
            apply_global_switch=apply_global_switch,
            apply_tts_global_switch=apply_tts_global_switch,
            handle_learn_style_command=handle_learn_style_command,
            handle_view_style_command=handle_view_style_command,
            handle_clear_context_command=handle_clear_context_command,
            handle_full_reset_memory_command=handle_full_reset_memory_command,
            get_recent_group_msgs=get_recent_group_msgs,
            call_ai_api=self.call_ai_api,
            call_style_ai_api=self.call_style_ai_api or self.call_ai_api,
            get_group_style=get_group_style,
            tts_service=self.tts_service,
            chat_histories=chat_histories,
            save_session_histories=save_session_histories,
            get_driver=self.get_driver,
            build_private_session_id=build_private_session_id,
            build_group_session_id=build_group_session_id,
            is_global_clear_command=is_global_clear_command,
            clear_all_context=clear_all_context,
            resolve_clear_target=resolve_clear_target,
            clear_message_buffer=clear_message_buffer,
            clear_session_context=clear_session_context,
            persona_store=self.persona_store,
            knowledge_store=self.reply_processor_deps.runtime.knowledge_store,
            agent_tool_caller=self.reply_processor_deps.runtime.agent_tool_caller,
            start_knowledge_builder=start_knowledge_builder,
            get_knowledge_build_task=get_knowledge_build_task,
            set_knowledge_build_task=set_knowledge_build_task,
        )


__all__ = ["PluginRuntimeBundle"]
