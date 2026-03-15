from dataclasses import dataclass
from typing import Any, Dict

from .blacklist_flow import (
    build_perm_blacklist_card_markdown,
    build_perm_blacklist_text,
    collect_perm_blacklist_items,
)
from .diary_flow import clean_generated_text, filter_sensitive_content, generate_ai_diary, get_recent_chat_context
from .proactive_flow import build_proactive_checker, run_proactive_messaging
from .runtime_switch_flow import apply_proactive_switch, apply_web_search_switch
from .style_flow import analyze_group_style
from .yaml_parser import extract_xml_content, parse_yaml_response


@dataclass
class FlowSetupDeps:
    plugin_config: Any
    sign_in_available: bool
    is_rest_time: Any
    get_bots: Any
    load_data: Any
    load_proactive_state: Any
    save_proactive_state: Any
    get_user_data: Any
    get_level_name: Any
    get_now: Any
    get_activity_status: Any
    load_prompt: Any
    call_ai_api: Any
    parse_yaml_response: Any
    logger: Any


def setup_flows(*, deps: FlowSetupDeps) -> Dict[str, Any]:
    check_proactive_messaging = build_proactive_checker(
        plugin_config=deps.plugin_config,
        sign_in_available=deps.sign_in_available,
        is_rest_time=deps.is_rest_time,
        get_bots=deps.get_bots,
        load_data=deps.load_data,
        load_proactive_state=deps.load_proactive_state,
        save_proactive_state=deps.save_proactive_state,
        get_user_data=deps.get_user_data,
        get_level_name=deps.get_level_name,
        get_now=deps.get_now,
        get_activity_status=deps.get_activity_status,
        load_prompt=deps.load_prompt,
        call_ai_api=deps.call_ai_api,
        parse_yaml_response=deps.parse_yaml_response,
        logger=deps.logger,
    )
    return {
        "check_proactive_messaging": check_proactive_messaging,
        "apply_web_search_switch": apply_web_search_switch,
        "apply_proactive_switch": apply_proactive_switch,
    }


__all__ = [
    "analyze_group_style",
    "apply_proactive_switch",
    "apply_web_search_switch",
    "build_perm_blacklist_card_markdown",
    "build_perm_blacklist_text",
    "build_proactive_checker",
    "clean_generated_text",
    "collect_perm_blacklist_items",
    "extract_xml_content",
    "filter_sensitive_content",
    "generate_ai_diary",
    "get_recent_chat_context",
    "parse_yaml_response",
    "run_proactive_messaging",
    "FlowSetupDeps",
    "setup_flows",
]
