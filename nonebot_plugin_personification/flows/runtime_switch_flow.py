from typing import Any, Tuple


def apply_global_switch(action: str, plugin_config: Any) -> Tuple[bool, str]:
    """处理拟人全局开关，返回 (是否变更, 响应文本)。"""
    if action in {"开启", "on", "true"}:
        changed = not getattr(plugin_config, "personification_global_enabled", True)
        plugin_config.personification_global_enabled = True
        return changed, "拟人插件已全局开启。"

    if action in {"关闭", "off", "false"}:
        changed = getattr(plugin_config, "personification_global_enabled", True)
        plugin_config.personification_global_enabled = False
        return changed, "拟人插件已全局关闭。"

    status = "开启" if getattr(plugin_config, "personification_global_enabled", True) else "关闭"
    return False, f"当前拟人插件状态：{status}\n使用 '拟人开关 开启/关闭' 来切换。"


def apply_tts_global_switch(action: str, plugin_config: Any) -> Tuple[bool, str]:
    """处理语音全局开关，返回 (是否变更, 响应文本)。"""
    if action in {"开启", "on", "true"}:
        changed = not getattr(plugin_config, "personification_tts_global_enabled", True)
        plugin_config.personification_tts_global_enabled = True
        return changed, "拟人插件语音功能已全局开启。"

    if action in {"关闭", "off", "false"}:
        changed = getattr(plugin_config, "personification_tts_global_enabled", True)
        plugin_config.personification_tts_global_enabled = False
        return changed, "拟人插件语音功能已全局关闭。"

    status = "开启" if getattr(plugin_config, "personification_tts_global_enabled", True) else "关闭"
    return False, f"当前语音功能状态：{status}\n使用 '拟人语音 开启/关闭' 来切换。"


def apply_web_search_switch(action: str, plugin_config: Any) -> Tuple[bool, str]:
    """处理拟人联网开关，返回 (是否变更, 响应文本)。"""
    if action in {"开启", "on", "true"}:
        changed = not (
            bool(getattr(plugin_config, "personification_tool_web_search_enabled", True))
            and bool(getattr(plugin_config, "personification_model_builtin_search_enabled", False))
        )
        plugin_config.personification_web_search = True
        plugin_config.personification_builtin_search = True
        plugin_config.personification_tool_web_search_enabled = True
        plugin_config.personification_model_builtin_search_enabled = True
        plugin_config.personification_tool_web_search_mode = "enabled"
        return changed, "拟人插件联网与内置查证功能已开启。"

    if action in {"关闭", "off", "false"}:
        changed = bool(getattr(plugin_config, "personification_tool_web_search_enabled", True)) or bool(
            getattr(plugin_config, "personification_model_builtin_search_enabled", False)
        )
        plugin_config.personification_web_search = False
        plugin_config.personification_builtin_search = False
        plugin_config.personification_tool_web_search_enabled = False
        plugin_config.personification_model_builtin_search_enabled = False
        plugin_config.personification_tool_web_search_mode = "disabled"
        return changed, "拟人插件联网与内置查证功能已关闭。"

    status = "开启" if (
        bool(getattr(plugin_config, "personification_tool_web_search_enabled", True))
        or bool(getattr(plugin_config, "personification_model_builtin_search_enabled", False))
    ) else "关闭"
    return False, f"当前联网功能状态：{status}\n使用 '拟人联网 开启/关闭' 来切换。"


def apply_proactive_switch(action: str, plugin_config: Any) -> Tuple[bool, str]:
    """处理主动消息开关，返回 (是否变更, 响应文本)。"""
    if action in {"开启", "on", "true"}:
        changed = not bool(plugin_config.personification_proactive_enabled)
        plugin_config.personification_proactive_enabled = True
        return changed, "拟人插件主动消息功能已开启。"

    if action in {"关闭", "off", "false"}:
        changed = bool(plugin_config.personification_proactive_enabled)
        plugin_config.personification_proactive_enabled = False
        return changed, "拟人插件主动消息功能已关闭。"

    status = "开启" if plugin_config.personification_proactive_enabled else "关闭"
    return False, f"当前主动消息功能状态：{status}\n使用 '拟人主动消息 开启/关闭' 来切换。"
