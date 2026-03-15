from typing import Any, Tuple


def apply_web_search_switch(action: str, plugin_config: Any) -> Tuple[bool, str]:
    """处理拟人联网开关，返回 (是否变更, 响应文本)。"""
    if action in {"开启", "on", "true"}:
        changed = not bool(plugin_config.personification_web_search)
        plugin_config.personification_web_search = True
        return changed, "拟人插件模型联网功能已开启（将对所有消息启用搜索功能）。"

    if action in {"关闭", "off", "false"}:
        changed = bool(plugin_config.personification_web_search)
        plugin_config.personification_web_search = False
        return changed, "拟人插件模型联网功能已关闭。"

    status = "开启" if plugin_config.personification_web_search else "关闭"
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
