import random
from pathlib import Path
from typing import Any, Callable, Dict

from ..config import Config
from ..flows import FlowSetupDeps, parse_yaml_response
from ..handlers import (
    MatcherSetupDeps,
    PersonaDeps,
    ReplyProcessorDeps,
    RuntimeDeps,
    SessionDeps,
    TypeDeps,
    build_group_fav_markdown,
    build_group_fav_text,
    build_personification_rule,
    build_poke_notice_rule,
    build_poke_rule,
    build_view_config_nodes,
    build_yaml_response_processor,
    extract_forward_message_content,
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
    handle_personification_help_command,
    handle_learn_style_command,
    handle_manual_diary_command,
    handle_perm_blacklist_set_command,
    handle_proactive_switch_command,
    handle_rebuild_plugin_knowledge_command,
    handle_remote_skill_review_command,
    handle_record_message_event,
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
    handle_view_plugin_knowledge_status_command,
    handle_view_persona_command,
    handle_view_style_command,
    handle_web_search_switch_command,
    parse_group_fav_update_args,
    parse_persona_update_args,
    personification_rule as personification_rule_core,
    poke_notice_rule as poke_notice_rule_core,
    poke_rule as poke_rule_core,
    process_response_logic as process_response_logic_core,
    record_msg_rule as record_msg_rule_core,
    resolve_record_message,
    run_buffer_timer as run_buffer_timer_core,
    split_text_into_segments as split_text_into_segments_core,
    sticker_chat_rule as sticker_chat_rule_core,
)
from ..jobs import JobSetupDeps
from ..schedule import (
    format_time_context,
    get_activity_status,
    get_current_local_time,
    get_schedule_prompt_injection,
    init_schedule_config,
    is_rest_time,
)
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
from .context_cleanup import (
    clear_all_context,
    clear_message_buffer,
    clear_session_context,
    is_global_clear_command,
    resolve_clear_target,
)
from .context_policy import (
    build_private_anti_loop_hint,
    clear_private_command_keywords,
    looks_like_private_command,
    register_private_command_keywords,
    sanitize_history_text,
    sanitize_session_messages,
)
from .data_store import init_data_store
from .legacy_memory_migrator import LegacyMemoryMigrator
from .knowledge_store import PluginKnowledgeStore
from .background_intelligence import BackgroundIntelligence
from .ai_routes import (
    build_fallback_vision_caller,
    build_routed_tool_caller,
    summarize_route_state,
)
from .memory_curator import MemoryCurator
from .memory_decay import MemoryDecayScheduler
from .memory_store import init_memory_store
from .time_ctx import init_time_context
from .builtin_hooks import register_all_builtin_hooks
from .plugin_meta import build_plugin_metadata, build_plugin_usage_text
from .proactive_store import load_proactive_state, save_proactive_state, update_private_interaction_time
from .profile_service import ProfileService
from .qzone_service import build_qzone_services
from .runtime_assembly import PluginRuntimeBundle
from .memory_defaults import DEFAULT_PERSONA_HISTORY_MAX
from .model_router import (
    MODEL_ROLE_AGENT,
    MODEL_ROLE_INTENT,
    MODEL_ROLE_REVIEW,
    MODEL_ROLE_STICKER,
    get_model_override_for_role,
)
from .runtime_integrations import (
    build_sign_in_fallbacks,
    extract_default_bot_nickname,
    get_scheduler,
)
from .runtime_config import get_runtime_load_info
from .runtime_state import get_shared_http_client, schedule_disabled_override_prompt
from .service_factory import (
    build_agent_runtime_deps,
    build_ai_api_caller,
    build_custom_title_getter,
    build_grounding_context_builder,
    build_interrupt_guard,
    build_load_prompt,
    build_msg_processed_checker,
    build_provider_reader,
    build_runtime_config_io,
    build_sticker_cache,
    build_web_search_executor,
)
from .tts_service import TtsService
from ..agent.inner_state import get_personification_data_dir
from .persona_service import PersonaStore
from .session_store import (
    GROUP_SESSION_PREFIX,
    PRIVATE_SESSION_PREFIX,
    SESSION_HISTORY_LIMIT,
    append_session_message,
    build_group_session_id,
    build_private_session_id,
    chat_histories,
    ensure_session_history,
    get_session_messages,
    save_session_histories,
)


def build_plugin_runtime(
    *,
    plugin_config: Any,
    superusers: set[str],
    logger: Any,
    get_driver: Callable[[], Any],
    get_bots: Callable[[], dict[str, Any]],
    superuser_permission: Any,
    finished_exception_cls: Any,
    group_message_event_cls: Any,
    private_message_event_cls: Any,
    message_event_cls: Any,
    poke_event_cls: Any,
    message_cls: Any,
    message_segment_cls: Any,
    md_to_pic: Any,
) -> PluginRuntimeBundle:
    init_data_store(plugin_config, logger=logger)
    init_schedule_config(plugin_config)
    init_time_context(str(getattr(plugin_config, "personification_timezone", "Asia/Shanghai") or "Asia/Shanghai"))
    register_all_builtin_hooks()

    sign_in_available, get_user_data, update_user_data, load_data, get_level_name = build_sign_in_fallbacks()
    qzone_publish_available, publish_qzone_shuo, update_qzone_cookie = build_qzone_services(
        plugin_config=plugin_config,
        logger=logger,
    )
    data_dir = get_personification_data_dir(plugin_config)
    knowledge_store = PluginKnowledgeStore(data_dir)

    if sign_in_available:
        logger.info("拟人插件：已加载签到插件，启用好感度与黑名单联动。")
    else:
        logger.warning("拟人插件：未加载签到插件，部分联动功能不可用。")

    module_instance_id = random.randint(1000, 9999)
    logger.info(f"拟人插件：模块加载中 (Instance ID: {module_instance_id})")

    bot_statuses: Dict[str, str] = {}
    user_blacklist: Dict[str, float] = {}
    msg_buffer: Dict[str, Dict[str, Any]] = {}
    persona_store = None
    memory_store = init_memory_store(plugin_config, logger=logger)
    profile_service = ProfileService(memory_store)
    memory_decay_scheduler = MemoryDecayScheduler(memory_store, logger=logger)
    background_intelligence = BackgroundIntelligence(
        plugin_config=plugin_config,
        memory_store=memory_store,
        memory_decay_scheduler=memory_decay_scheduler,
        logger=logger,
        migrator_factory=lambda: LegacyMemoryMigrator(memory_store, logger=logger),
    )
    memory_curator = MemoryCurator(
        memory_store,
        logger=logger,
        background_intelligence=background_intelligence,
    )
    LegacyMemoryMigrator(memory_store, logger=logger).migrate_once()

    load_prompt = build_load_prompt(
        plugin_config=plugin_config,
        get_group_config=get_group_config,
        logger=logger,
    )
    is_msg_processed = build_msg_processed_checker(
        get_driver=get_driver,
        logger=logger,
        module_instance_id=module_instance_id,
    )
    build_grounding_context = build_grounding_context_builder(
        plugin_config=plugin_config,
        get_now=get_current_local_time,
        logger=logger,
    )
    should_avoid_interrupting = build_interrupt_guard(
        get_recent_group_msgs=get_recent_group_msgs,
        hot_chat_min_pass_rate=getattr(
            plugin_config, "personification_hot_chat_min_pass_rate", 0.2
        ),
    )
    do_web_search = build_web_search_executor(
        get_now=get_current_local_time,
        logger=logger,
    )
    get_configured_api_providers = build_provider_reader(
        plugin_config=plugin_config,
        logger=logger,
    )
    call_ai_api = build_ai_api_caller(
        plugin_config=plugin_config,
        logger=logger,
        model_role=MODEL_ROLE_AGENT,
    )
    lite_call_ai_api = build_ai_api_caller(
        plugin_config=plugin_config,
        logger=logger,
        model_override_field_name="personification_lite_model",
        model_role=MODEL_ROLE_REVIEW,
    )
    call_style_ai_api = call_ai_api

    vision_caller = build_fallback_vision_caller(
        plugin_config,
        logger,
        warn=True,
        model_override=get_model_override_for_role(plugin_config, MODEL_ROLE_STICKER),
    )
    route_state = summarize_route_state(plugin_config, logger)
    logger.info(
        "personification: routing "
        f"primary={route_state['primary']} "
        f"fallback={route_state['fallback']} "
        f"fallback_source={route_state['fallback_source'] or 'none'} "
        f"video_fallback={route_state['video_fallback']} "
        f"video_fallback_source={route_state['video_fallback_source'] or 'none'}"
    )
    if getattr(plugin_config, "personification_persona_enabled", True):
        persona_tool_caller = build_routed_tool_caller(
            plugin_config=plugin_config,
            logger=logger,
        )
        persona_data_path_raw = str(
            getattr(plugin_config, "personification_persona_data_path", "") or ""
        ).strip()
        persona_data_file = (
            Path(persona_data_path_raw)
            if persona_data_path_raw
            else get_personification_data_dir(plugin_config) / "user_personas.json"
        )
        persona_store = PersonaStore(
            data_dir=persona_data_file.parent,
            data_file=persona_data_file,
            tool_caller=persona_tool_caller,
            history_max=int(
                getattr(plugin_config, "personification_persona_history_max", DEFAULT_PERSONA_HISTORY_MAX)
            ),
            logger=logger,
            profile_service=profile_service,
        )
    tool_registry, inner_state_updater, agent_tool_caller, lite_tool_caller = build_agent_runtime_deps(
        plugin_config=plugin_config,
        logger=logger,
        get_now=get_current_local_time,
        persona_store=persona_store,
        vision_caller=vision_caller,
        scheduler=get_scheduler(),
        data_dir=data_dir,
        get_bots=get_bots,
        knowledge_store=knowledge_store,
        memory_store=memory_store,
        profile_service=profile_service,
        memory_curator=memory_curator,
        background_intelligence=background_intelligence,
    )
    tts_service = TtsService(
        plugin_config=plugin_config,
        logger=logger,
        get_http_client=lambda: get_shared_http_client(max_connections=20),
        data_dir=data_dir,
        style_planner=lambda messages: call_style_ai_api(messages),
    )
    save_plugin_runtime_config, load_plugin_runtime_config = build_runtime_config_io(
        plugin_config=plugin_config,
        logger=logger,
    )
    load_plugin_runtime_config()
    runtime_load_info = get_runtime_load_info(plugin_config)
    skipped_runtime_keys = list(runtime_load_info.get("skipped_runtime_keys") or [])
    if skipped_runtime_keys:
        logger.info(
            "personification: startup env overrides runtime_config keys="
            + ", ".join(skipped_runtime_keys[:24])
        )

    personification_rule = build_personification_rule(
        personification_rule_core=personification_rule_core,
        sign_in_available=sign_in_available,
        get_user_data=get_user_data,
        user_blacklist=user_blacklist,
        logger=logger,
        group_event_cls=group_message_event_cls,
        private_event_cls=private_message_event_cls,
        is_group_whitelisted=is_group_whitelisted,
        plugin_whitelist=plugin_config.personification_whitelist,
        load_prompt=load_prompt,
        load_proactive_state=load_proactive_state,
        is_rest_time=is_rest_time,
        probability=plugin_config.personification_probability,
        group_chat_follow_probability=getattr(
            plugin_config,
            "personification_group_chat_follow_probability",
            0.85,
        ),
        looks_like_private_command=looks_like_private_command,
        get_recent_group_msgs=get_recent_group_msgs,
    )
    poke_rule = build_poke_rule(
        poke_rule_core=poke_rule_core,
        is_group_whitelisted=is_group_whitelisted,
        plugin_whitelist=plugin_config.personification_whitelist,
        probability=plugin_config.personification_poke_probability,
    )
    poke_notice_rule = build_poke_notice_rule(
        poke_notice_rule_core=poke_notice_rule_core,
        is_group_whitelisted=is_group_whitelisted,
        plugin_whitelist=plugin_config.personification_whitelist,
        probability=plugin_config.personification_poke_probability,
        logger=logger,
    )

    yaml_response_processor = build_yaml_response_processor(
        get_current_time=get_current_local_time,
        format_time_context=format_time_context,
        bot_statuses=bot_statuses,
        get_group_config=get_group_config,
        plugin_config=plugin_config,
        get_schedule_prompt_injection=get_schedule_prompt_injection,
        schedule_disabled_override_prompt=schedule_disabled_override_prompt,
        build_grounding_context=build_grounding_context,
        call_ai_api=call_ai_api,
        lite_call_ai_api=lite_call_ai_api,
        parse_yaml_response=parse_yaml_response,
        message_segment_cls=message_segment_cls,
        sanitize_history_text=sanitize_history_text,
        private_session_prefix=PRIVATE_SESSION_PREFIX,
        build_private_session_id=build_private_session_id,
        build_group_session_id=build_group_session_id,
        append_session_message=append_session_message,
        record_group_msg=record_group_msg,
        logger=logger,
        user_blacklist=user_blacklist,
        superusers=superusers,
        get_configured_api_providers=get_configured_api_providers,
        tool_registry=tool_registry,
        agent_tool_caller=agent_tool_caller,
        lite_tool_caller=lite_tool_caller,
        vision_caller=vision_caller,
        tts_service=tts_service,
        extract_forward_content=extract_forward_message_content,
        memory_curator=memory_curator,
        knowledge_store=knowledge_store,
    )

    get_custom_title = build_custom_title_getter(logger=logger)
    get_sticker_files = build_sticker_cache(
        sticker_path=plugin_config.personification_sticker_path,
        ttl_seconds=300,
    )
    default_bot_nickname = extract_default_bot_nickname(load_prompt, logger=logger)

    def _get_whitelisted_groups() -> list[str]:
        static = list(plugin_config.personification_whitelist or [])
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

    reply_processor_deps = ReplyProcessorDeps(
        session=SessionDeps(
            private_session_prefix=PRIVATE_SESSION_PREFIX,
            looks_like_private_command=looks_like_private_command,
            ensure_session_history=ensure_session_history,
            build_private_session_id=build_private_session_id,
            build_group_session_id=build_group_session_id,
            sanitize_session_messages=sanitize_session_messages,
            get_session_messages=get_session_messages,
            append_session_message=append_session_message,
            sanitize_history_text=sanitize_history_text,
            build_private_anti_loop_hint=build_private_anti_loop_hint,
        ),
        persona=PersonaDeps(
            load_prompt=load_prompt,
            sign_in_available=sign_in_available,
            get_user_data=get_user_data,
            get_level_name=get_level_name,
            update_user_data=update_user_data,
            get_group_config=get_group_config,
            get_group_style=get_group_style,
            favorability_attitudes=plugin_config.personification_favorability_attitudes,
            get_custom_title=get_custom_title,
            default_bot_nickname=default_bot_nickname,
        ),
        runtime=RuntimeDeps(
            is_msg_processed=is_msg_processed,
            logger=logger,
            superusers=superusers,
            get_configured_api_providers=get_configured_api_providers,
            should_avoid_interrupting=should_avoid_interrupting,
            module_instance_id=module_instance_id,
            process_yaml_response_logic=yaml_response_processor,
            plugin_config=plugin_config,
            get_current_time=get_current_local_time,
            format_time_context=format_time_context,
            schedule_disabled_override_prompt=schedule_disabled_override_prompt,
            get_schedule_prompt_injection=get_schedule_prompt_injection,
            build_grounding_context=build_grounding_context,
            update_private_interaction_time=update_private_interaction_time,
            call_ai_api=call_ai_api,
            lite_call_ai_api=lite_call_ai_api,
            save_plugin_runtime_config=save_plugin_runtime_config,
            user_blacklist=user_blacklist,
            record_group_msg=record_group_msg,
            split_text_into_segments=split_text_into_segments_core,
            message_segment_cls=message_segment_cls,
            get_sticker_files=get_sticker_files,
            get_http_client=lambda: get_shared_http_client(max_connections=20),
            get_whitelisted_groups=_get_whitelisted_groups,
            tts_service=tts_service,
            tool_registry=tool_registry,
            inner_state_updater=inner_state_updater,
            agent_tool_caller=agent_tool_caller,
            lite_tool_caller=lite_tool_caller,
            persona_store=persona_store,
            vision_caller=vision_caller,
            knowledge_store=knowledge_store,
            memory_store=memory_store,
            profile_service=profile_service,
            memory_curator=memory_curator,
            background_intelligence=background_intelligence,
        ),
        types=TypeDeps(
            poke_event_cls=poke_event_cls,
            message_event_cls=message_event_cls,
            group_message_event_cls=group_message_event_cls,
            private_message_event_cls=private_message_event_cls,
            message_cls=message_cls,
        ),
    )

    def _build_dynamic_lite_tool_caller(default_caller: Any) -> Any:
        lite_model = (
            get_model_override_for_role(plugin_config, MODEL_ROLE_INTENT)
            or str(getattr(plugin_config, "personification_lite_model", "") or "").strip()
        )
        if not lite_model:
            return default_caller
        try:
            return build_routed_tool_caller(
                plugin_config=plugin_config,
                logger=logger,
                model_override=lite_model,
            )
        except Exception as exc:
            logger.warning(f"personification: rebuild lite tool caller failed, fallback to primary caller: {exc}")
            return default_caller

    def _reload_runtime_services() -> None:
        nonlocal yaml_response_processor
        new_agent_tool_caller = build_routed_tool_caller(
            plugin_config=plugin_config,
            logger=logger,
            model_override=get_model_override_for_role(plugin_config, MODEL_ROLE_AGENT),
        )
        new_lite_tool_caller = _build_dynamic_lite_tool_caller(new_agent_tool_caller)
        new_vision_caller = build_fallback_vision_caller(
            plugin_config,
            logger,
            warn=True,
            model_override=get_model_override_for_role(plugin_config, MODEL_ROLE_STICKER),
        )
        yaml_response_processor = build_yaml_response_processor(
            get_current_time=get_current_local_time,
            format_time_context=format_time_context,
            bot_statuses=bot_statuses,
            get_group_config=get_group_config,
            plugin_config=plugin_config,
            get_schedule_prompt_injection=get_schedule_prompt_injection,
            schedule_disabled_override_prompt=schedule_disabled_override_prompt,
            build_grounding_context=build_grounding_context,
            call_ai_api=call_ai_api,
            lite_call_ai_api=lite_call_ai_api,
            parse_yaml_response=parse_yaml_response,
            message_segment_cls=message_segment_cls,
            sanitize_history_text=sanitize_history_text,
            private_session_prefix=PRIVATE_SESSION_PREFIX,
            build_private_session_id=build_private_session_id,
            build_group_session_id=build_group_session_id,
            append_session_message=append_session_message,
            record_group_msg=record_group_msg,
            logger=logger,
            user_blacklist=user_blacklist,
            superusers=superusers,
            get_configured_api_providers=get_configured_api_providers,
            tool_registry=tool_registry,
            agent_tool_caller=new_agent_tool_caller,
            lite_tool_caller=new_lite_tool_caller,
            vision_caller=new_vision_caller,
            tts_service=tts_service,
            extract_forward_content=extract_forward_message_content,
            memory_curator=memory_curator,
            knowledge_store=knowledge_store,
        )
        reply_processor_deps.runtime.process_yaml_response_logic = yaml_response_processor
        reply_processor_deps.runtime.agent_tool_caller = new_agent_tool_caller
        reply_processor_deps.runtime.lite_tool_caller = new_lite_tool_caller
        reply_processor_deps.runtime.vision_caller = new_vision_caller
        if persona_store is not None:
            try:
                persona_store.tool_caller = build_routed_tool_caller(
                    plugin_config=plugin_config,
                    logger=logger,
                )
            except Exception as exc:
                logger.warning(f"personification: rebuild persona tool caller failed: {exc}")
        logger.info("personification: runtime services reloaded from current config")

    _reload_runtime_services()

    return PluginRuntimeBundle(
        plugin_meta=build_plugin_metadata(Config),
        plugin_config=plugin_config,
        superusers=superusers,
        logger=logger,
        get_driver=get_driver,
        get_bots=get_bots,
        superuser_permission=superuser_permission,
        finished_exception_cls=finished_exception_cls,
        group_message_event_cls=group_message_event_cls,
        private_message_event_cls=private_message_event_cls,
        message_event_cls=message_event_cls,
        poke_event_cls=poke_event_cls,
        message_cls=message_cls,
        message_segment_cls=message_segment_cls,
        md_to_pic=md_to_pic,
        sign_in_available=sign_in_available,
        qzone_publish_available=qzone_publish_available,
        publish_qzone_shuo=publish_qzone_shuo,
        update_qzone_cookie=update_qzone_cookie,
        get_user_data=get_user_data,
        update_user_data=update_user_data,
        load_data=load_data,
        get_level_name=get_level_name,
        bot_statuses=bot_statuses,
        user_blacklist=user_blacklist,
        msg_buffer=msg_buffer,
        load_prompt=load_prompt,
        call_ai_api=call_ai_api,
        call_style_ai_api=call_style_ai_api,
        get_configured_api_providers=get_configured_api_providers,
        save_plugin_runtime_config=save_plugin_runtime_config,
        load_plugin_runtime_config=load_plugin_runtime_config,
        reload_runtime_services=_reload_runtime_services,
        reply_processor_deps=reply_processor_deps,
        personification_rule=personification_rule,
        poke_rule=poke_rule,
        poke_notice_rule=poke_notice_rule,
        tts_service=tts_service,
        tool_registry=tool_registry,
        persona_store=persona_store,
        memory_store=memory_store,
        profile_service=profile_service,
        memory_curator=memory_curator,
        memory_decay_scheduler=memory_decay_scheduler,
        background_intelligence=background_intelligence,
    )
