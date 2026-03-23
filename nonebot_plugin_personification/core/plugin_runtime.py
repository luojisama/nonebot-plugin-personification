import random
from dataclasses import dataclass
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
    handle_add_whitelist_command,
    handle_agree_whitelist_command,
    handle_apply_whitelist_command,
    handle_background_style_analysis,
    handle_clear_context_command,
    handle_group_fav_query_command,
    handle_group_feature_switch_command,
    handle_learn_style_command,
    handle_manual_diary_command,
    handle_perm_blacklist_set_command,
    handle_proactive_switch_command,
    handle_record_message_event,
    handle_reject_whitelist_command,
    handle_remove_whitelist_command,
    handle_reply_event,
    handle_reset_persona_command,
    handle_schedule_switch_command,
    handle_set_group_fav_command,
    handle_set_persona_command,
    handle_sticker_chat_event,
    handle_view_config_command,
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
from ..schedule import get_activity_status, get_beijing_time, get_schedule_prompt_injection, is_rest_time
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
    set_group_style,
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
from .builtin_hooks import register_all_builtin_hooks
from .plugin_meta import build_plugin_metadata
from .proactive_store import load_proactive_state, save_proactive_state, update_private_interaction_time
from .qzone_service import build_qzone_services
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
from ..agent.inner_state import get_personification_data_dir
from ..skills.tool_caller import build_tool_caller
from ..skills.user_persona import PersonaStore
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


def _build_sign_in_fallbacks() -> tuple[bool, Any, Any, Any, Any]:
    try:
        try:
            from plugin.sign_in.utils import get_user_data, load_data, update_user_data  # type: ignore
            from plugin.sign_in.config import get_level_name  # type: ignore
        except ImportError:
            from ...sign_in.utils import get_user_data, load_data, update_user_data  # type: ignore
            from ...sign_in.config import get_level_name  # type: ignore
        return True, get_user_data, update_user_data, load_data, get_level_name
    except ImportError:
        return False, (lambda _uid: {}), (lambda *_a, **_k: None), (lambda: {}), (lambda _v: "普通")


def _get_scheduler() -> Any:
    """安全获取 APScheduler 实例，不可用时返回 None，不中断插件加载。"""
    try:
        from nonebot_plugin_apscheduler import scheduler

        return scheduler
    except Exception:
        return None


def _extract_default_bot_nickname(load_prompt: Callable[[str | None], Any], logger: Any) -> str:
    try:
        prompt_data = load_prompt(None)
        if isinstance(prompt_data, dict):
            name = str(prompt_data.get("name", "")).strip()
            if name:
                return name
    except Exception as e:
        logger.debug(f"拟人插件：提取默认昵称失败，使用回退昵称。{e}")
    return ""


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
    reply_processor_deps: ReplyProcessorDeps
    personification_rule: Any
    poke_rule: Any
    poke_notice_rule: Any
    tool_registry: Any = None
    persona_store: Any = None

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
            get_now=get_beijing_time,
            get_activity_status=get_activity_status,
            load_prompt=self.load_prompt,
            call_ai_api=self.call_ai_api,
            parse_yaml_response=self.parse_yaml_response,
            logger=self.logger,
            agent_tool_caller=self.reply_processor_deps.runtime.agent_tool_caller,
            agent_data_dir=get_personification_data_dir(self.plugin_config),
            persona_store=self.persona_store,
            get_recent_group_msgs=get_recent_group_msgs,
            get_group_style=get_group_style,
            get_whitelisted_groups=self._get_whitelisted_groups,
            record_group_msg=record_group_msg,
        )

    def _get_whitelisted_groups(self) -> list[str]:
        """返回当前静态与动态白名单中的所有群 ID。"""
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
            get_now=get_beijing_time,
            get_bots=self.get_bots,
            superusers=self.superusers,
            logger=self.logger,
            generate_ai_diary_flow=self.generate_ai_diary_flow,
            load_prompt=self.load_prompt,
            call_ai_api=self.call_ai_api,
            qzone_publish_available=self.qzone_publish_available,
            update_qzone_cookie=self.update_qzone_cookie,
            publish_qzone_shuo=self.publish_qzone_shuo,
            check_proactive_messaging=check_proactive_messaging,
            proactive_interval_minutes=self.plugin_config.personification_proactive_interval,
            check_group_idle_topic=check_group_idle_topic,
            group_idle_check_interval_minutes=getattr(
                self.plugin_config,
                "personification_group_idle_check_interval",
                15,
            ),
            agent_tool_caller=self.reply_processor_deps.runtime.agent_tool_caller,
            agent_data_dir=get_personification_data_dir(self.plugin_config),
        )

    def make_matcher_setup_deps(
        self,
        *,
        generate_ai_diary: Any,
        apply_web_search_switch: Any,
        apply_proactive_switch: Any,
    ) -> MatcherSetupDeps:
        return MatcherSetupDeps(
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
            handle_schedule_switch_command=handle_schedule_switch_command,
            save_plugin_runtime_config=self.save_plugin_runtime_config,
            set_group_schedule_enabled=set_group_schedule_enabled,
            bot_statuses=self.bot_statuses,
            handle_view_config_command=handle_view_config_command,
            get_group_config=get_group_config,
            get_configured_api_providers=self.get_configured_api_providers,
            build_view_config_nodes=build_view_config_nodes,
            session_history_limit=SESSION_HISTORY_LIMIT,
            handle_record_message_event=handle_record_message_event,
            resolve_record_message=resolve_record_message,
            get_custom_title=self.reply_processor_deps.persona.get_custom_title,
            record_group_msg=record_group_msg,
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
            apply_web_search_switch=apply_web_search_switch,
            apply_proactive_switch=apply_proactive_switch,
            handle_learn_style_command=handle_learn_style_command,
            handle_view_style_command=handle_view_style_command,
            handle_clear_context_command=handle_clear_context_command,
            get_recent_group_msgs=get_recent_group_msgs,
            call_ai_api=self.call_ai_api,
            call_style_ai_api=self.call_style_ai_api or self.call_ai_api,
            get_group_style=get_group_style,
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
    init_data_store(plugin_config)
    register_all_builtin_hooks()

    sign_in_available, get_user_data, update_user_data, load_data, get_level_name = _build_sign_in_fallbacks()
    qzone_publish_available, publish_qzone_shuo, update_qzone_cookie = build_qzone_services(
        plugin_config=plugin_config,
        logger=logger,
    )

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
        web_search_enabled=plugin_config.personification_web_search,
        get_now=get_beijing_time,
        logger=logger,
    )
    should_avoid_interrupting = build_interrupt_guard(
        get_recent_group_msgs=get_recent_group_msgs,
        hot_chat_min_pass_rate=getattr(
            plugin_config, "personification_hot_chat_min_pass_rate", 0.2
        ),
    )
    do_web_search = build_web_search_executor(
        get_now=get_beijing_time,
        logger=logger,
    )
    get_configured_api_providers = build_provider_reader(
        plugin_config=plugin_config,
        logger=logger,
    )
    call_ai_api = build_ai_api_caller(
        plugin_config=plugin_config,
        logger=logger,
    )
    style_api_key = str(getattr(plugin_config, "personification_style_api_key", "") or "").strip()
    if style_api_key:
        class _StyleConfigProxy:
            def __init__(self, original: Any) -> None:
                self._original = original

            def __getattr__(self, name: str) -> Any:
                if name == "personification_api_type":
                    return getattr(self._original, "personification_style_api_type", "") or getattr(
                        self._original,
                        "personification_api_type",
                        "openai",
                    )
                if name == "personification_api_url":
                    return getattr(self._original, "personification_style_api_url", "") or ""
                if name == "personification_api_key":
                    return style_api_key
                if name == "personification_model":
                    return getattr(self._original, "personification_style_api_model", "") or getattr(
                        self._original,
                        "personification_model",
                        "",
                    )
                if name == "personification_api_pools":
                    return None
                return getattr(self._original, name)

        call_style_ai_api = build_ai_api_caller(
            plugin_config=_StyleConfigProxy(plugin_config),
            logger=logger,
        )
        logger.info("拟人插件：群聊风格分析启用专用模型配置。")
    else:
        call_style_ai_api = call_ai_api

    from ..skills.vision_caller import build_vision_caller as _build_vision_caller

    vision_caller = _build_vision_caller(plugin_config)
    if getattr(plugin_config, "personification_persona_enabled", True):
        persona_providers = get_configured_api_providers()
        class _PersonaConfigProxy:
            def __init__(self, original: Any, provider: Dict[str, Any]) -> None:
                self._original = original
                self._provider = provider

            def __getattr__(self, name: str) -> Any:
                if name == "personification_api_type":
                    return self._provider.get("api_type", "openai")
                if name == "personification_api_url":
                    return self._provider.get("api_url", "")
                if name == "personification_api_key":
                    return self._provider.get("api_key", "")
                if name == "personification_model":
                    return self._provider.get("model", "")
                if name == "personification_codex_auth_path":
                    return self._provider.get("auth_path", "")
                return getattr(self._original, name)

        persona_api_key = str(getattr(plugin_config, "personification_persona_api_key", "") or "").strip()
        if persona_api_key:
            persona_provider = {
                "api_type": getattr(plugin_config, "personification_persona_api_type", "")
                or getattr(plugin_config, "personification_api_type", "openai"),
                "api_url": getattr(plugin_config, "personification_persona_api_url", "") or "",
                "api_key": persona_api_key,
                "model": getattr(plugin_config, "personification_persona_model", "")
                or getattr(plugin_config, "personification_model", ""),
                "auth_path": getattr(plugin_config, "personification_codex_auth_path", "") or "",
            }
            persona_tool_caller = build_tool_caller(_PersonaConfigProxy(plugin_config, persona_provider))
            logger.info("拟人插件：用户画像分析启用专用模型配置。")
        elif persona_providers:
            persona_tool_caller = build_tool_caller(_PersonaConfigProxy(plugin_config, persona_providers[0]))
        else:
            persona_tool_caller = build_tool_caller(plugin_config)
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
            history_max=int(getattr(plugin_config, "personification_persona_history_max", 30)),
            logger=logger,
        )
    tool_registry, inner_state_updater, agent_tool_caller = build_agent_runtime_deps(
        plugin_config=plugin_config,
        logger=logger,
        get_now=get_beijing_time,
        persona_store=persona_store,
        vision_caller=vision_caller,
        scheduler=_get_scheduler(),
        data_dir=get_personification_data_dir(plugin_config),
        get_bots=get_bots,
    )
    save_plugin_runtime_config, load_plugin_runtime_config = build_runtime_config_io(
        plugin_config=plugin_config,
        logger=logger,
    )
    load_plugin_runtime_config()

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
        looks_like_private_command=looks_like_private_command,
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
        get_beijing_time=get_beijing_time,
        bot_statuses=bot_statuses,
        get_group_config=get_group_config,
        plugin_config=plugin_config,
        get_schedule_prompt_injection=get_schedule_prompt_injection,
        schedule_disabled_override_prompt=schedule_disabled_override_prompt,
        build_grounding_context=build_grounding_context,
        call_ai_api=call_ai_api,
        parse_yaml_response=parse_yaml_response,
        message_segment_cls=message_segment_cls,
        sanitize_history_text=sanitize_history_text,
        private_session_prefix=PRIVATE_SESSION_PREFIX,
        build_private_session_id=build_private_session_id,
        build_group_session_id=build_group_session_id,
        append_session_message=append_session_message,
        logger=logger,
        tool_registry=tool_registry,
        agent_tool_caller=agent_tool_caller,
    )

    get_custom_title = build_custom_title_getter(logger=logger)
    get_sticker_files = build_sticker_cache(
        sticker_path=plugin_config.personification_sticker_path,
        ttl_seconds=300,
    )
    default_bot_nickname = _extract_default_bot_nickname(load_prompt, logger=logger)

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
            get_beijing_time=get_beijing_time,
            schedule_disabled_override_prompt=schedule_disabled_override_prompt,
            get_schedule_prompt_injection=get_schedule_prompt_injection,
            build_grounding_context=build_grounding_context,
            update_private_interaction_time=update_private_interaction_time,
            call_ai_api=call_ai_api,
            user_blacklist=user_blacklist,
            record_group_msg=record_group_msg,
            split_text_into_segments=split_text_into_segments_core,
            message_segment_cls=message_segment_cls,
            get_sticker_files=get_sticker_files,
            get_http_client=lambda: get_shared_http_client(max_connections=20),
            get_whitelisted_groups=_get_whitelisted_groups,
            tool_registry=tool_registry,
            inner_state_updater=inner_state_updater,
            agent_tool_caller=agent_tool_caller,
            persona_store=persona_store,
            vision_caller=vision_caller,
        ),
        types=TypeDeps(
            poke_event_cls=poke_event_cls,
            message_event_cls=message_event_cls,
            group_message_event_cls=group_message_event_cls,
            private_message_event_cls=private_message_event_cls,
            message_cls=message_cls,
        ),
    )

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
        reply_processor_deps=reply_processor_deps,
        personification_rule=personification_rule,
        poke_rule=poke_rule,
        poke_notice_rule=poke_notice_rule,
        tool_registry=tool_registry,
        persona_store=persona_store,
    )
