import json
from pathlib import Path
from typing import Any


RUNTIME_CONFIG_PATH = Path("data/user_persona/runtime_config.json")


def save_plugin_runtime_config(plugin_config: Any, logger: Any, path: Path = RUNTIME_CONFIG_PATH) -> None:
    """保存运行时配置（联网、作息全局开关、主动消息开关）。"""
    data = {
        "web_search": plugin_config.personification_web_search,
        "schedule_global": plugin_config.personification_schedule_global,
        "proactive_enabled": plugin_config.personification_proactive_enabled,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"保存运行时配置失败: {e}")


def load_plugin_runtime_config(plugin_config: Any, logger: Any, path: Path = RUNTIME_CONFIG_PATH) -> None:
    """加载运行时配置并回填到插件配置对象。"""
    if not path.exists():
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"加载运行时配置失败: {e}")
        return

    plugin_config.personification_web_search = data.get(
        "web_search",
        True,
    )
    plugin_config.personification_schedule_global = data.get(
        "schedule_global",
        plugin_config.personification_schedule_global,
    )
    plugin_config.personification_proactive_enabled = data.get(
        "proactive_enabled",
        plugin_config.personification_proactive_enabled,
    )
