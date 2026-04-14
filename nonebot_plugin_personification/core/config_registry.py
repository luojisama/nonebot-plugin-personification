from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


GLOBAL_SCOPE = "global"
GROUP_SCOPE = "group"


def _bool_parser(raw: str) -> bool:
    text = str(raw or "").strip().lower()
    mapping = {
        "on": True,
        "off": False,
        "true": True,
        "false": False,
        "1": True,
        "0": False,
        "yes": True,
        "no": False,
        "开": True,
        "关": False,
        "开启": True,
        "关闭": False,
        "启用": True,
        "禁用": False,
    }
    if text not in mapping:
        raise ValueError("布尔值仅支持 on/off、开/关、true/false、1/0")
    return mapping[text]


def _int_parser(raw: str) -> int:
    try:
        return int(str(raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("需要整数值") from exc


def _str_parser(raw: str) -> str:
    return str(raw or "").strip()


def _web_search_mode_parser(raw: str) -> str:
    text = str(raw or "").strip().lower()
    mapping = {
        "enabled": "enabled",
        "开启": "enabled",
        "启用": "enabled",
        "默认": "enabled",
        "live": "live",
        "实时": "live",
        "即时": "live",
        "cached": "cached",
        "缓存": "cached",
        "disabled": "disabled",
        "关闭": "disabled",
        "禁用": "disabled",
    }
    return mapping.get(text, text)


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    field_name: str
    display_name: str
    value_type: str
    default: Any
    scope: str
    description: str
    category: str
    admin_only: bool = True
    hot_reloadable: bool = True
    choices: tuple[str, ...] = ()
    min_value: int | None = None
    max_value: int | None = None
    help_aliases: tuple[str, ...] = ()
    risk_note: str = ""
    parser: Callable[[str], Any] | None = None

    def normalize_value(self, raw: Any) -> Any:
        if isinstance(raw, bool) and self.value_type == "bool":
            value = raw
        else:
            parser = self.parser or _str_parser
            value = parser(str(raw or ""))
        if self.choices:
            normalized = str(value).strip().lower()
            allowed = {choice.lower(): choice for choice in self.choices}
            if normalized not in allowed:
                raise ValueError(f"可选值: {', '.join(self.choices)}")
            value = allowed[normalized]
        if self.value_type == "int":
            number = int(value)
            if self.min_value is not None and number < self.min_value:
                raise ValueError(f"不能小于 {self.min_value}")
            if self.max_value is not None and number > self.max_value:
                raise ValueError(f"不能大于 {self.max_value}")
            return number
        return value


def _build_entries() -> list[ConfigEntry]:
    entries = [
        ConfigEntry(
            key="model_builtin_search_enabled",
            field_name="personification_model_builtin_search_enabled",
            display_name="模型内置搜索",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="允许主模型直接使用 provider 原生 builtin search。",
            category="config",
            help_aliases=("builtin_search", "内置搜索", "模型搜索"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tool_web_search_enabled",
            field_name="personification_tool_web_search_enabled",
            display_name="工具联网搜索",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许工具层执行联网搜索。",
            category="config",
            help_aliases=("web_search_enabled", "联网搜索", "工具联网"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="tool_web_search_mode",
            field_name="personification_tool_web_search_mode",
            display_name="联网模式",
            value_type="str",
            default="enabled",
            scope=GLOBAL_SCOPE,
            description="工具联网模式。",
            category="config",
            choices=("enabled", "live", "cached", "disabled"),
            help_aliases=("web_search_mode", "搜索模式", "联网方式"),
            parser=_web_search_mode_parser,
        ),
        ConfigEntry(
            key="vision_fallback_enabled",
            field_name="personification_vision_fallback_enabled",
            display_name="视觉兜底",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="主模型看图不稳定时允许兜底视觉流程。",
            category="config",
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="vision_fallback_model",
            field_name="personification_vision_fallback_model",
            display_name="视觉兜底模型",
            value_type="str",
            default="gpt-5.4",
            scope=GLOBAL_SCOPE,
            description="视觉兜底模型名。",
            category="config",
            parser=_str_parser,
        ),
        ConfigEntry(
            key="memory_enabled",
            field_name="personification_memory_enabled",
            display_name="记忆总开关",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="总开关，控制记忆体系是否运行。",
            category="config",
            help_aliases=("记忆", "记忆开关"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_palace_enabled",
            field_name="personification_memory_palace_enabled",
            display_name="记忆宫殿",
            value_type="bool",
            default=False,
            scope=GLOBAL_SCOPE,
            description="启用长期记忆宫殿存储与 recall。",
            category="config",
            help_aliases=("记忆宫殿", "长期记忆"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_decay_enabled",
            field_name="personification_memory_decay_enabled",
            display_name="记忆衰减",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许后台执行记忆衰减。",
            category="config",
            help_aliases=("衰减", "自动衰减"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_consolidation_enabled",
            field_name="personification_memory_consolidation_enabled",
            display_name="记忆整合",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许后台执行记忆聚合与 crystal 检查。",
            category="config",
            help_aliases=("整合", "记忆整合", "结晶检查"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="memory_recall_top_k",
            field_name="personification_memory_recall_top_k",
            display_name="记忆召回条数",
            value_type="int",
            default=6,
            scope=GLOBAL_SCOPE,
            description="单次 recall 默认返回记忆条数。",
            category="config",
            min_value=1,
            max_value=10,
            help_aliases=("召回条数", "recall条数"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="background_intelligence_enabled",
            field_name="personification_background_intelligence_enabled",
            display_name="后台智能",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用统一后台智能调度层。",
            category="config",
            help_aliases=("后台智能",),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="background_evolves_enabled",
            field_name="personification_background_evolves_enabled",
            display_name="后台演化关系",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用后台 EVOLVES 关系检测。",
            category="config",
            help_aliases=("演化关系", "evolves"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="background_crystals_enabled",
            field_name="personification_background_crystals_enabled",
            display_name="后台结晶",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="启用后台 crystal 候选生成。",
            category="config",
            help_aliases=("结晶", "crystal", "记忆结晶"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="background_max_llm_tasks_per_hour",
            field_name="personification_background_max_llm_tasks_per_hour",
            display_name="每小时后台任务上限",
            value_type="int",
            default=6,
            scope=GLOBAL_SCOPE,
            description="后台每小时最多 LLM 任务数。",
            category="config",
            min_value=0,
            max_value=120,
            help_aliases=("每小时任务上限", "小时预算"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="background_max_llm_tasks_per_day",
            field_name="personification_background_max_llm_tasks_per_day",
            display_name="每日后台任务上限",
            value_type="int",
            default=24,
            scope=GLOBAL_SCOPE,
            description="后台每日最多 LLM 任务数。",
            category="config",
            min_value=0,
            max_value=500,
            help_aliases=("每日任务上限", "每日预算"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="background_debounce_seconds",
            field_name="personification_background_debounce_seconds",
            display_name="后台防抖秒数",
            value_type="int",
            default=90,
            scope=GLOBAL_SCOPE,
            description="同类后台任务的防抖时间。",
            category="config",
            min_value=5,
            max_value=3600,
            help_aliases=("防抖秒数", "后台防抖"),
            parser=_int_parser,
        ),
        ConfigEntry(
            key="wiki_enabled",
            field_name="personification_wiki_enabled",
            display_name="Wiki 查询",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许使用 wiki 能力。",
            category="config",
            help_aliases=("wiki", "百科查询"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="wiki_fandom_enabled",
            field_name="personification_wiki_fandom_enabled",
            display_name="Fandom Wiki",
            value_type="bool",
            default=True,
            scope=GLOBAL_SCOPE,
            description="允许 Fandom wiki 作为补充来源。",
            category="config",
            help_aliases=("fandom", "fandom wiki", "粉丝百科"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_enabled",
            field_name="enabled",
            display_name="本群拟人回复",
            value_type="bool",
            default=True,
            scope=GROUP_SCOPE,
            description="当前群是否启用拟人回复。",
            category="config",
            help_aliases=("personification_enabled", "本群拟人", "群回复"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_sticker_enabled",
            field_name="sticker_enabled",
            display_name="本群表情包",
            value_type="bool",
            default=True,
            scope=GROUP_SCOPE,
            description="当前群是否允许发表情包。",
            category="config",
            help_aliases=("本群表情包", "群表情包"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_tts_enabled",
            field_name="tts_enabled",
            display_name="本群语音回复",
            value_type="bool",
            default=True,
            scope=GROUP_SCOPE,
            description="当前群是否允许自动或手动语音回复。",
            category="config",
            help_aliases=("本群语音", "群语音回复"),
            parser=_bool_parser,
        ),
        ConfigEntry(
            key="group_schedule_enabled",
            field_name="schedule_enabled",
            display_name="本群作息模拟",
            value_type="bool",
            default=False,
            scope=GROUP_SCOPE,
            description="当前群是否启用作息模拟。",
            category="config",
            help_aliases=("作息模拟", "群作息"),
            parser=_bool_parser,
        ),
    ]
    return entries


_ENTRIES = _build_entries()
_ENTRY_BY_KEY: dict[str, ConfigEntry] = {entry.key: entry for entry in _ENTRIES}
_ALIASES: dict[str, ConfigEntry] = {}
for _entry in _ENTRIES:
    _ALIASES[_entry.key.lower()] = _entry
    _ALIASES[_entry.field_name.lower()] = _entry
    for _alias in _entry.help_aliases:
        _ALIASES[str(_alias or "").strip().lower()] = _entry


def get_config_entries(scope: str | None = None) -> list[ConfigEntry]:
    if scope is None:
        return list(_ENTRIES)
    normalized = str(scope or "").strip().lower()
    return [entry for entry in _ENTRIES if entry.scope == normalized]


def get_global_runtime_config_keys() -> list[str]:
    return [entry.key for entry in _ENTRIES if entry.scope == GLOBAL_SCOPE]


def resolve_config_entry(key: str) -> ConfigEntry | None:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return None
    return _ALIASES.get(normalized)


def get_entry_default_value(entry: ConfigEntry, plugin_config: Any) -> Any:
    if entry.scope == GLOBAL_SCOPE:
        return getattr(type(plugin_config), entry.field_name, entry.default)
    return entry.default


def read_config_value(
    entry: ConfigEntry,
    *,
    plugin_config: Any,
    group_config: dict[str, Any] | None = None,
) -> Any:
    if entry.scope == GLOBAL_SCOPE:
        return getattr(plugin_config, entry.field_name, entry.default)
    group_payload = group_config if isinstance(group_config, dict) else {}
    return group_payload.get(entry.field_name, entry.default)


def describe_choices(entry: ConfigEntry) -> str:
    if entry.value_type == "bool":
        return "开 / 关"
    if entry.key == "tool_web_search_mode":
        return "开启 / 实时 / 缓存 / 关闭"
    if entry.choices:
        return ", ".join(entry.choices)
    if entry.value_type == "int":
        lower = entry.min_value if entry.min_value is not None else "-"
        upper = entry.max_value if entry.max_value is not None else "-"
        return f"整数 ({lower}..{upper})"
    return "自由文本"


def format_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "开" if value else "关"
    if value is None:
        return "未设置"
    return str(value)


def get_entry_label(entry: ConfigEntry) -> str:
    return str(entry.display_name or entry.key)


def config_entry_matches_scope(entry: ConfigEntry, scope: str) -> bool:
    return entry.scope == str(scope or "").strip().lower()


def iter_config_aliases(entry: ConfigEntry) -> Iterable[str]:
    yield entry.key
    yield entry.field_name
    for alias in entry.help_aliases:
        yield alias
