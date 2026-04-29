from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.admin_acl import (
    add_plugin_admin,
    can_manage_sensitive_action,
    is_plugin_admin,
    is_superuser,
    load_plugin_admins,
    remove_plugin_admin,
)
from ..core.config_registry import (
    GLOBAL_SCOPE,
    GROUP_SCOPE,
    ConfigEntry,
    config_entry_matches_scope,
    describe_choices,
    format_config_value,
    get_config_entries,
    get_entry_default_value,
    get_entry_label,
    read_config_value,
    resolve_config_entry,
)
from ..core.ai_routes import summarize_route_state
from ..core.help_registry import find_command_help, find_entries_by_category, get_command_help_entries
from ..core.knowledge_builder import (
    maybe_start_plugin_knowledge_builder,
    stop_plugin_knowledge_builder,
)
from ..core.legacy_memory_migrator import LegacyMemoryMigrator
from ..core.model_router import (
    MODEL_OVERRIDE_ROLES,
    MODEL_ROLE_AGENT,
    MODEL_ROLE_INTENT,
    MODEL_ROLE_LABELS,
    MODEL_ROLE_REVIEW,
    MODEL_ROLE_STICKER,
    collect_available_models,
    format_model_overrides,
    get_model_for_role,
    normalize_model_overrides,
    resolve_model_role,
)
from ..core.runtime_config import get_runtime_load_info
from ..utils import get_group_config, is_group_whitelisted


_GROUP_CONFIG_NAMESPACE = "group_config"
_COMMAND_ALIASES = {
    "help": "help",
    "帮助": "help",
    "命令": "help",
    "config": "config",
    "配置": "config",
    "配置项": "config",
    "status": "status",
    "状态": "status",
    "admin": "admin",
    "管理员": "admin",
    "memory": "memory",
    "记忆": "memory",
    "migrate": "migrate",
    "迁移": "migrate",
    "recall": "recall",
    "召回": "recall",
    "model": "model",
    "模型": "model",
}
_SUBCOMMAND_ALIASES = {
    "list": "list",
    "列表": "list",
    "get": "get",
    "查看": "get",
    "set": "set",
    "设置": "set",
    "reset": "reset",
    "重置": "reset",
    "add": "add",
    "添加": "add",
    "remove": "remove",
    "删除": "remove",
    "run": "run",
    "执行": "run",
    "status": "status",
    "状态": "status",
    "stats": "stats",
    "统计": "stats",
    "bootstrap": "bootstrap",
    "补建": "bootstrap",
    "decay": "decay",
    "衰减": "decay",
    "evolves": "evolves",
    "演化": "evolves",
    "crystal": "crystal",
    "结晶": "crystal",
    "运行": "run",
}
_COMPOUND_TOKEN_ALIASES = {
    "配置列表": ("配置", "列表"),
    "配置项列表": ("配置", "列表"),
    "配置项查看": ("配置", "查看"),
    "配置项设置": ("配置", "设置"),
    "配置项重置": ("配置", "重置"),
    "配置查看": ("配置", "查看"),
    "配置设置": ("配置", "设置"),
    "配置重置": ("配置", "重置"),
    "命令列表": ("帮助",),
    "管理员列表": ("管理员", "列表"),
    "记忆状态": ("记忆", "状态"),
    "记忆补建": ("记忆", "补建"),
    "记忆衰减": ("记忆", "衰减"),
    "记忆演化": ("记忆", "演化"),
    "记忆结晶": ("记忆", "结晶"),
    "记忆结晶执行": ("记忆", "结晶", "执行"),
    "结晶执行": ("结晶", "执行"),
    "迁移状态": ("迁移", "状态"),
    "迁移执行": ("迁移", "执行"),
    "召回统计": ("召回", "统计"),
}


def normalize_command_word(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    if raw in _COMMAND_ALIASES:
        return _COMMAND_ALIASES[raw]
    if raw in _SUBCOMMAND_ALIASES:
        return _SUBCOMMAND_ALIASES[raw]
    return raw


def tokenize_command_args(arg_text: str) -> list[str]:
    tokens = [token for token in str(arg_text or "").strip().split() if token]
    expanded: list[str] = []
    for token in tokens:
        normalized = str(token or "").strip()
        compound = _COMPOUND_TOKEN_ALIASES.get(normalized) or _COMPOUND_TOKEN_ALIASES.get(normalized.lower())
        if compound:
            expanded.extend(compound)
        else:
            expanded.append(normalized)
    return expanded


def format_timestamp(ts: float) -> str:
    if not ts:
        return "未记录"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


def _scope_label(scope: str) -> str:
    mapping = {
        GLOBAL_SCOPE: "全局",
        GROUP_SCOPE: "群",
        "global/group": "全局 / 群",
        "group/global": "全局 / 群",
    }
    return mapping.get(str(scope or "").strip().lower(), str(scope or ""))


def _normalize_scope_token(token: str) -> str:
    mapping = {
        "global": GLOBAL_SCOPE,
        "全局": GLOBAL_SCOPE,
        "group": GROUP_SCOPE,
        "群": GROUP_SCOPE,
        "本群": GROUP_SCOPE,
    }
    return mapping.get(str(token or "").strip().lower(), str(token or "").strip().lower())


def _root_help_text() -> str:
    lines = [
        "拟人命令总览",
        "统一入口（推荐优先记这一组）：",
        "- 拟人 帮助：查看命令总览、分类帮助或某个配置项说明。",
        "- 拟人 状态：查看当前运行、记忆、联网和后台任务状态。",
        "- 拟人 配置 列表：查看所有可配置项、当前值、范围和用途。",
        "- 拟人 配置 查看 <配置项> [当前群/群号]：查看单个配置项详情。",
        "- 拟人 配置 设置 <配置项> <值> [当前群/群号]：修改配置。",
        "- 拟人 配置 重置 <配置项> [当前群/群号]：恢复默认值。",
        "- 拟人 记忆 状态|补建|衰减|演化|结晶 执行：管理记忆系统。",
        "- 拟人 管理员 列表|添加|删除：管理插件管理员。",
        "- 拟人 迁移 状态|执行：查看或执行旧数据迁移。",
        "- 拟人 召回 统计：查看长期记忆召回统计。",
        "- 拟人 模型 列表|使用|设置|重置：QQ 内热更新 intent/review/agent/sticker 四类 LLM 模型。",
        "",
        "旧命令兼容（仍可用，下面直接说明用途）：",
        "- 拟人配置：发送聊天记录形式的配置总览。",
        "- 拟人开关 / 拟人语音 / 拟人联网 / 拟人主动消息：快速切全局开关。",
        "- 开启拟人 / 关闭拟人 / 开启表情包 / 关闭表情包 / 开启语音回复 / 关闭语音回复：快速切当前群功能。",
        "- 拟人作息：切换当前群或全局作息模拟。",
        "- 设置人设 / 查看人设 / 重置人设：管理群人设。",
        "- 学习群聊风格 / 查看群聊风格：管理群风格。",
        "- 群好感 / 设置群好感：查看或修改群好感度。",
        "- 查看画像 / 刷新画像：查看或重建用户画像。",
        "- 清除记忆 / 完全清除记忆：清理会话与记忆数据。",
        "- 申请白名单 / 同意白名单 / 拒绝白名单 / 添加白名单 / 移除白名单：管理群准入。",
        "",
        "继续查看：拟人 帮助 配置 / 记忆 / 管理员 / 迁移 / 召回 / 某个配置项名",
        "兼容写法：拟人 配置列表 / 配置查看 / 配置设置 / 配置重置 / 记忆状态 / 召回统计",
        "前缀：拟人 / 人格 / /persona",
    ]
    return "\n".join(lines)


def _config_usage_text() -> str:
    return (
        "用法：\n"
        "- 拟人 配置 列表 [全局/群]\n"
        "- 拟人 配置 查看 <配置项> [当前群/群号]\n"
        "- 拟人 配置 设置 <配置项> <值> [当前群/群号]\n"
        "- 拟人 配置 重置 <配置项> [当前群/群号]\n"
        "兼容简写：拟人 配置列表 / 配置查看 / 配置设置 / 配置重置"
    )


def _format_command_help(path: tuple[str, ...]) -> str:
    entry = find_command_help(path)
    if entry is None:
        return "没找到这条命令。可先用“拟人 帮助”查看总览。"
    lines = [
        f"命令：{' '.join(entry.path)}",
        f"说明：{entry.summary}",
        f"用法：{entry.usage}",
        f"权限：{entry.permission}",
        f"范围：{_scope_label(entry.scope)}",
        f"生效：{entry.hot_reload}",
        "示例：",
    ]
    for example in entry.examples:
        lines.append(f"- {example}")
    return "\n".join(lines)


def _format_category_help(category: str) -> str:
    entries = find_entries_by_category(category)
    if not entries:
        return "未找到该帮助分类。"
    category_names = {
        "config": "配置",
        "admin": "管理员",
        "memory": "记忆",
        "migrate": "迁移",
        "help": "帮助",
        "status": "状态",
        "recall": "召回",
        "model": "模型",
    }
    lines = [f"{category_names.get(category, category)}："]
    for entry in entries:
        lines.append(f"- {' '.join(entry.path)}：{entry.summary}")
    lines.append("继续输入完整命令可看详细帮助。")
    return "\n".join(lines)


def _format_config_help(bundle: Any, entry: ConfigEntry, *, group_id: str = "") -> str:
    current = _read_entry_value(bundle, entry, group_id=group_id)
    default = get_entry_default_value(entry, bundle.plugin_config)
    lines = [
        f"配置：{get_entry_label(entry)}",
        f"内部键：{entry.key}",
        f"说明：{entry.description}",
        f"当前值：{format_config_value(current)}",
        f"默认值：{format_config_value(default)}",
        f"范围：{_scope_label(entry.scope)}",
        f"可选值：{describe_choices(entry)}",
        f"生效：{'立即生效' if entry.hot_reloadable else '重启后生效'}",
    ]
    if entry.risk_note:
        lines.append(f"风险提示：{entry.risk_note}")
    return "\n".join(lines)


def _read_entry_value(bundle: Any, entry: ConfigEntry, *, group_id: str = "") -> Any:
    group_payload = get_group_config(group_id) if entry.scope == GROUP_SCOPE and group_id else {}
    if entry.scope == GROUP_SCOPE and entry.field_name == "enabled" and group_id:
        return is_group_whitelisted(str(group_id), bundle.plugin_config.personification_whitelist)
    return read_config_value(entry, plugin_config=bundle.plugin_config, group_config=group_payload)


def _resolve_group_target(token: str, event: Any) -> str:
    raw = str(token or "").strip()
    if raw in {"当前群", "本群", "current", "current_group"}:
        return str(getattr(event, "group_id", "") or "")
    if raw.isdigit():
        return raw
    return ""


def _mutate_group_config(group_id: str, mutator: Any) -> dict[str, Any]:
    from ..core.data_store import get_data_store

    normalized = str(group_id or "").strip()
    if not normalized:
        raise ValueError("需要群号")

    def _apply(current: object) -> dict[str, Any]:
        payload = current if isinstance(current, dict) else {}
        group_payload = payload.get(normalized)
        if not isinstance(group_payload, dict):
            group_payload = {}
            payload[normalized] = group_payload
        mutator(group_payload)
        return payload

    updated = get_data_store().mutate_sync(_GROUP_CONFIG_NAMESPACE, _apply)
    return updated if isinstance(updated, dict) else {}


def _apply_global_side_effects(bundle: Any, entry: ConfigEntry, value: Any) -> None:
    config = bundle.plugin_config
    if entry.field_name == "personification_model_builtin_search_enabled":
        config.personification_builtin_search = bool(value)
    if entry.field_name == "personification_tool_web_search_enabled":
        config.personification_web_search = bool(value)
    if entry.field_name == "personification_tool_web_search_mode":
        mode = str(value or "").strip().lower()
        config.personification_tool_web_search_enabled = mode != "disabled"
        config.personification_web_search = mode != "disabled"
    if entry.field_name == "personification_plugin_knowledge_build_enabled":
        knowledge_store = getattr(bundle.reply_processor_deps.runtime, "knowledge_store", None)
        tool_caller = getattr(bundle.reply_processor_deps.runtime, "agent_tool_caller", None)
        get_task = getattr(bundle, "get_knowledge_build_task", None)
        set_task = getattr(bundle, "set_knowledge_build_task", None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or knowledge_store is None or get_task is None or set_task is None:
            return
        if bool(value):
            loop.create_task(
                maybe_start_plugin_knowledge_builder(
                    plugin_config=config,
                    tool_caller=tool_caller,
                    knowledge_store=knowledge_store,
                    logger=bundle.logger,
                    get_knowledge_build_task=get_task,
                    set_knowledge_build_task=set_task,
                    trigger="config_toggle",
                    force=True,
                )
            )
        else:
            loop.create_task(
                stop_plugin_knowledge_builder(
                    logger=bundle.logger,
                    knowledge_store=knowledge_store,
                    get_knowledge_build_task=get_task,
                    set_knowledge_build_task=set_task,
                    enabled=False,
                    trigger="config_toggle",
                    result="runtime_disabled",
                    reasons=["config_disabled"],
                )
            )


def _set_config_value(
    bundle: Any,
    entry: ConfigEntry,
    *,
    value: Any,
    group_id: str = "",
) -> tuple[str, str]:
    if entry.scope == GLOBAL_SCOPE:
        setattr(bundle.plugin_config, entry.field_name, value)
        _apply_global_side_effects(bundle, entry, value)
        bundle.save_plugin_runtime_config()
        return "global", format_config_value(getattr(bundle.plugin_config, entry.field_name))

    _mutate_group_config(
        group_id,
        lambda group_payload: group_payload.__setitem__(entry.field_name, value),
    )
    return str(group_id), format_config_value(_read_entry_value(bundle, entry, group_id=str(group_id)))


def _reset_config_value(bundle: Any, entry: ConfigEntry, *, group_id: str = "") -> tuple[str, str]:
    if entry.scope == GLOBAL_SCOPE:
        default = get_entry_default_value(entry, bundle.plugin_config)
        setattr(bundle.plugin_config, entry.field_name, default)
        _apply_global_side_effects(bundle, entry, default)
        bundle.save_plugin_runtime_config()
        return "global", format_config_value(default)

    _mutate_group_config(
        group_id,
        lambda group_payload: group_payload.pop(entry.field_name, None),
    )
    return str(group_id), format_config_value(_read_entry_value(bundle, entry, group_id=str(group_id)))


def _resolve_group_config_target(entry: ConfigEntry, event: Any, extra_tokens: list[str]) -> str:
    if entry.scope != GROUP_SCOPE:
        return ""
    if extra_tokens:
        group_id = _resolve_group_target(extra_tokens[-1], event)
        if group_id:
            return group_id
    return str(getattr(event, "group_id", "") or "")


def _admin_error() -> str:
    return "权限不足：仅超级管理员、插件管理员可执行；群级配置可选放行当前群管理员。"


def _normalize_help_tokens(tokens: list[str]) -> list[str]:
    expanded = tokenize_command_args(" ".join(str(token or "") for token in tokens))
    return [normalize_command_word(token) for token in expanded if str(token or "").strip()]


async def dispatch_persona_admin_command(
    matcher: Any,
    *,
    bundle: Any,
    event: Any,
    arg_text: str,
) -> None:
    tokens = tokenize_command_args(arg_text)
    if not tokens:
        await matcher.finish(_root_help_text())

    command = normalize_command_word(tokens[0])
    rest = tokens[1:]

    if command == "help":
        await matcher.finish(render_help(bundle, event=event, tokens=rest))

    if command == "status":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(render_status(bundle))

    if command == "config":
        await matcher.finish(handle_config_command(bundle, event=event, tokens=rest))

    if command == "admin":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(handle_admin_command(bundle, event=event, tokens=rest))

    if command == "memory":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(await handle_memory_command(bundle, event=event, tokens=rest))

    if command == "migrate":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(await handle_migrate_command(bundle, tokens=rest))

    if command == "recall":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(handle_recall_command(bundle, tokens=rest))

    if command == "model":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(handle_model_command(bundle, tokens=rest))

    await matcher.finish("未识别的子命令。可用“拟人 帮助”或“/persona help”查看帮助。")


def render_help(bundle: Any, *, event: Any, tokens: list[str]) -> str:
    _ = event
    if not tokens:
        return _root_help_text()
    normalized = _normalize_help_tokens(tokens)
    if not normalized:
        return _root_help_text()
    if len(normalized) >= 2:
        entry = find_command_help(tuple(normalized[:3]))
        if entry is not None:
            return _format_command_help(entry.path)
        entry = find_command_help(tuple(normalized[:2]))
        if entry is not None:
            return _format_command_help(entry.path)
    if normalized[0] in {"config", "admin", "memory", "migrate", "help", "status", "recall", "model"}:
        return _format_category_help(normalized[0])
    config_entry = resolve_config_entry(" ".join(tokens))
    if config_entry is None:
        config_entry = resolve_config_entry(tokens[0])
    if config_entry is not None:
        group_id = str(getattr(event, "group_id", "") or "")
        return _format_config_help(bundle, config_entry, group_id=group_id)
    command_entry = find_command_help(tuple(normalized[:3])) or find_command_help(tuple(normalized[:2])) or find_command_help(tuple(normalized[:1]))
    if command_entry is not None:
        return _format_command_help(command_entry.path)
    return "未找到对应帮助。可用“拟人 帮助”查看总览。"


def render_status(bundle: Any) -> str:
    memory_stats = bundle.memory_store.get_memory_stats() if bundle.memory_store is not None else {}
    background_status = (
        bundle.background_intelligence.get_status() if bundle.background_intelligence is not None else {}
    )
    route_state = summarize_route_state(bundle.plugin_config)
    runtime_load_info = get_runtime_load_info(bundle.plugin_config)
    runtime_skipped = ", ".join(runtime_load_info.get("skipped_runtime_keys", [])[:12]) or "无"
    lines = [
        "运行状态",
        f"记忆总开关：{format_config_value(getattr(bundle.plugin_config, 'personification_memory_enabled', True))}",
        f"记忆宫殿：{format_config_value(getattr(bundle.plugin_config, 'personification_memory_palace_enabled', False))}",
        f"群聊记忆目录：{memory_stats.get('grouped_memory_dir', '未初始化')}",
        f"记忆宫殿目录：{memory_stats.get('memory_palace_dir', '未初始化')}",
        f"后台智能：{'开' if background_status.get('enabled') else '关'}",
        f"本小时后台任务：{background_status.get('llm_tasks_used_this_hour', 0)}/{background_status.get('max_llm_tasks_per_hour', 0)}",
        f"本日后台任务：{background_status.get('llm_tasks_used_today', 0)}/{background_status.get('max_llm_tasks_per_day', 0)}",
        f"主路由：{route_state['primary']}",
        f"全局兜底：{route_state['fallback']}",
        f"全局兜底来源：{route_state['fallback_source'] or '无'}",
        f"忽略的旧来源：{route_state['fallback_ignored'] or '无'}",
        f"视频兜底例外：{route_state['video_fallback']}",
        f"视频兜底来源：{route_state['video_fallback_source'] or '无'}",
        f"运行时配置文件：{runtime_load_info.get('path') or '未记录'}",
        f"env 覆盖的 runtime 键：{runtime_skipped}",
        f"模型内置搜索：{format_config_value(getattr(bundle.plugin_config, 'personification_model_builtin_search_enabled', False))}",
        "模型覆盖：" + "；".join(format_model_overrides(bundle.plugin_config)).replace("- ", ""),
        f"联网模式：{getattr(bundle.plugin_config, 'personification_tool_web_search_mode', 'enabled')}",
        f"最近后台维护：{format_timestamp(background_status.get('last_periodic_at', 0))}",
    ]
    return "\n".join(lines)


def _default_model_for_role(bundle: Any, role: str) -> str:
    config = bundle.plugin_config
    primary = ""
    provider_getter = getattr(bundle, "get_configured_api_providers", None)
    if callable(provider_getter):
        try:
            providers = [item for item in list(provider_getter() or []) if isinstance(item, dict)]
        except Exception:
            providers = []
        if providers:
            primary = str(providers[0].get("model", "") or "").strip()
    if not primary:
        primary = str(getattr(config, "personification_model", "") or "").strip()
    lite = str(getattr(config, "personification_lite_model", "") or "").strip()
    if role in {MODEL_ROLE_INTENT, MODEL_ROLE_REVIEW}:
        return lite or primary
    if role == MODEL_ROLE_STICKER:
        return (
            str(getattr(config, "personification_vision_fallback_model", "") or "").strip()
            or str(getattr(config, "personification_labeler_model", "") or "").strip()
            or primary
        )
    return primary


def _format_model_status(bundle: Any) -> str:
    lines = ["模型路由"]
    lines.append("当前生效：")
    for role in MODEL_OVERRIDE_ROLES:
        effective = get_model_for_role(
            bundle.plugin_config,
            role,
            _default_model_for_role(bundle, role),
        )
        lines.append(f"- {role}（{MODEL_ROLE_LABELS[role]}）：{effective or '未配置'}")
    lines.append("")
    lines.append("当前覆盖：")
    lines.extend(format_model_overrides(bundle.plugin_config))
    available = collect_available_models(
        bundle.plugin_config,
        get_configured_api_providers=getattr(bundle, "get_configured_api_providers", None),
    )
    lines.append("")
    lines.append("可用模型（按当前调用方式枚举；Codex 来源优先使用本机 /model 缓存）：")
    if available:
        for item in available:
            lines.append(f"- {item['model']}（{item['source']}）")
    else:
        lines.append("- 未发现已配置模型")
    lines.append("")
    lines.append("用法：拟人 模型 使用 <model>；拟人 模型 设置 <intent|review|agent|sticker> <model>；拟人 模型 重置 [role|全部]")
    return "\n".join(lines)


def _save_model_overrides(bundle: Any, overrides: dict[str, str]) -> None:
    normalized = normalize_model_overrides(overrides)
    setattr(bundle.plugin_config, "personification_model_overrides", normalized)
    if callable(getattr(bundle, "save_plugin_runtime_config", None)):
        bundle.save_plugin_runtime_config()
    reload_services = getattr(bundle, "reload_runtime_services", None)
    if callable(reload_services):
        reload_services()


def handle_model_command(bundle: Any, *, tokens: list[str]) -> str:
    action = normalize_command_word(tokens[0]) if tokens else "list"
    raw_action = str(tokens[0] if tokens else "").strip().lower()
    if action in {"list", "get", "status"} or raw_action in {"列表", "查看", "状态", ""}:
        return _format_model_status(bundle)

    current = normalize_model_overrides(
        getattr(bundle.plugin_config, "personification_model_overrides", {}) or {}
    )

    if raw_action in {"use", "使用", "切换", "全部", "all"}:
        if len(tokens) < 2:
            return "用法：拟人 模型 使用 <model>"
        model = " ".join(str(token) for token in tokens[1:]).strip()
        if not model:
            return "模型名不能为空。"
        for role in MODEL_OVERRIDE_ROLES:
            current[role] = model
        _save_model_overrides(bundle, current)
        return f"已将四类 LLM 调用统一切换到：{model}\n生效：立即生效"

    if action == "set":
        if len(tokens) < 3:
            return "用法：拟人 模型 设置 <intent|review|agent|sticker> <model>"
        role = resolve_model_role(tokens[1])
        if role not in MODEL_OVERRIDE_ROLES:
            return "未知模型阶段。可选：intent、review、agent、sticker。"
        model = " ".join(str(token) for token in tokens[2:]).strip()
        if not model:
            return "模型名不能为空。"
        current[role] = model
        _save_model_overrides(bundle, current)
        return f"已设置：{role}（{MODEL_ROLE_LABELS[role]}）= {model}\n生效：立即生效"

    if action == "reset":
        target = str(tokens[1] if len(tokens) >= 2 else "全部").strip().lower()
        if target in {"", "全部", "all", "所有"}:
            current.clear()
            _save_model_overrides(bundle, current)
            return "已清空全部模型覆盖。\n生效：立即生效"
        role = resolve_model_role(target)
        if role not in MODEL_OVERRIDE_ROLES:
            return "未知模型阶段。可选：intent、review、agent、sticker、全部。"
        current.pop(role, None)
        _save_model_overrides(bundle, current)
        return f"已重置：{role}（{MODEL_ROLE_LABELS[role]}）\n生效：立即生效"

    return "用法：拟人 模型 列表｜使用 <model>｜设置 <intent|review|agent|sticker> <model>｜重置 [role|全部]"


def handle_config_command(bundle: Any, *, event: Any, tokens: list[str]) -> str:
    action = normalize_command_word(tokens[0]) if tokens else "list"
    if action not in {"list", "get", "set", "reset"}:
        return _config_usage_text()

    if action == "list":
        requested_scope = _normalize_scope_token(tokens[1]) if len(tokens) >= 2 else ""
        current_group_id = str(getattr(event, "group_id", "") or "")
        allow_group_admin = requested_scope == GROUP_SCOPE and bool(current_group_id)
        if not can_manage_sensitive_action(
            event=event,
            superusers=bundle.superusers,
            allow_group_admin=allow_group_admin,
            target_group_id=current_group_id if allow_group_admin else "",
        ):
            return _admin_error()
        entries = get_config_entries()
        if requested_scope in {GLOBAL_SCOPE, GROUP_SCOPE}:
            entries = [entry for entry in entries if config_entry_matches_scope(entry, requested_scope)]
        lines = [f"配置列表（共 {len(entries)} 项）"]
        if requested_scope in {GLOBAL_SCOPE, GROUP_SCOPE}:
            lines.append(f"当前筛选：{_scope_label(requested_scope)}")
        else:
            lines.append("当前筛选：全局 + 群")
        if current_group_id:
            lines.append(f"当前群：{current_group_id}")
        else:
            lines.append("当前群：未指定；群配置项显示默认值，可用“拟人 配置 查看 <配置项> <群号>”查看指定群。")
        lines.append("查看单项：拟人 配置 查看 <配置项>")
        lines.append("修改配置：拟人 配置 设置 <配置项> <值>")
        current_scope = ""
        for entry in entries:
            if entry.scope != current_scope:
                current_scope = entry.scope
                lines.append("")
                lines.append(f"{_scope_label(current_scope)}配置：")
            current = _read_entry_value(bundle, entry, group_id=current_group_id)
            lines.append(
                f"- {get_entry_label(entry)}：当前 {format_config_value(current)}，默认 {format_config_value(get_entry_default_value(entry, bundle.plugin_config))}，可选 {describe_choices(entry)}。作用：{entry.description}"
            )
        return "\n".join(lines)

    if len(tokens) < 2:
        return f"请写明配置项名。\n{_config_usage_text()}"
    entry = resolve_config_entry(tokens[1])
    if entry is None:
        return "没找到这个配置项。可先用“拟人 配置 列表”查看所有配置，或用“拟人 帮助 配置”查看命令说明。"
    target_group_id = _resolve_group_config_target(entry, event, tokens[2:] if len(tokens) > 2 else [])
    if entry.scope == GROUP_SCOPE and not target_group_id:
        return "这是群配置。请在群里使用，或写上群号/当前群。"

    if action == "get":
        allow_group_admin = entry.scope == GROUP_SCOPE and bool(target_group_id)
        if not can_manage_sensitive_action(
            event=event,
            superusers=bundle.superusers,
            allow_group_admin=allow_group_admin,
            target_group_id=target_group_id,
        ):
            return _admin_error()
        return _format_config_help(bundle, entry, group_id=target_group_id) if entry.scope == GROUP_SCOPE else _format_config_help(bundle, entry)

    if action == "set":
        if len(tokens) < 3:
            return f"请提供要设置的值。\n{_config_usage_text()}"
        allow_group_admin = entry.scope == GROUP_SCOPE and bool(target_group_id)
        if not can_manage_sensitive_action(
            event=event,
            superusers=bundle.superusers,
            allow_group_admin=allow_group_admin,
            target_group_id=target_group_id,
        ):
            return _admin_error()
        value_token_index = 2
        raw_value = tokens[value_token_index]
        try:
            value = entry.normalize_value(raw_value)
        except ValueError as exc:
            return f"设置失败：{exc}"
        scope_text, current_value = _set_config_value(bundle, entry, value=value, group_id=target_group_id)
        scope_name = "全局" if scope_text == "global" else f"群 {scope_text}"
        hot_text = "立即生效" if entry.hot_reloadable else "重启后生效"
        return f"已设置：{get_entry_label(entry)} = {current_value}\n范围：{scope_name}\n生效：{hot_text}"

    allow_group_admin = entry.scope == GROUP_SCOPE and bool(target_group_id)
    if not can_manage_sensitive_action(
        event=event,
        superusers=bundle.superusers,
        allow_group_admin=allow_group_admin,
        target_group_id=target_group_id,
    ):
        return _admin_error()
    scope_text, current_value = _reset_config_value(bundle, entry, group_id=target_group_id)
    scope_name = "全局" if scope_text == "global" else f"群 {scope_text}"
    hot_text = "立即生效" if entry.hot_reloadable else "重启后生效"
    return f"已重置：{get_entry_label(entry)} = {current_value}\n范围：{scope_name}\n生效：{hot_text}"


def handle_admin_command(bundle: Any, *, event: Any, tokens: list[str]) -> str:
    action = normalize_command_word(tokens[0]) if tokens else "list"
    user_id = str(getattr(event, "user_id", "") or "")
    if action == "list":
        admins = load_plugin_admins()
        lines = ["插件管理员"]
        lines.append(f"- 超级管理员：{', '.join(sorted(str(item) for item in bundle.superusers)) or '无'}")
        lines.append(f"- 插件管理员：{', '.join(admins) or '无'}")
        lines.append(f"- 你的身份：{'超级管理员' if is_superuser(user_id, bundle.superusers) else ('插件管理员' if is_plugin_admin(user_id) else '普通用户')}")
        return "\n".join(lines)
    if len(tokens) < 2 or not str(tokens[1]).isdigit():
        return "用法：拟人 管理员 添加｜删除 <QQ号>"
    target_user_id = str(tokens[1]).strip()
    if action == "add":
        changed = add_plugin_admin(target_user_id)
        return "已添加管理员。" if changed else "该用户已是管理员。"
    if action == "remove":
        changed = remove_plugin_admin(target_user_id)
        return "已移除管理员。" if changed else "该用户不在管理员列表中。"
    return "用法：拟人 管理员 列表｜添加｜删除"


async def handle_memory_command(bundle: Any, *, event: Any, tokens: list[str]) -> str:
    if not tokens:
        return "用法：拟人 记忆 状态｜补建｜衰减｜演化｜结晶 执行"
    action = normalize_command_word(tokens[0])
    if action == "status":
        stats = bundle.memory_store.get_memory_stats()
        background_status = (
            bundle.background_intelligence.get_status() if bundle.background_intelligence is not None else {}
        )
        lines = [
            "记忆状态",
            f"群聊记忆群数：{stats.get('group_count', 0)}",
            f"记忆宫殿条目：{stats.get('palace_count', 0)}",
            f"结晶条目：{stats.get('crystal_count', 0)}",
            f"检索统计条数：{stats.get('search_stats_count', 0)}",
            f"最近衰减时间：{format_timestamp(background_status.get('last_decay_at', 0))}",
            f"最近补建群数：{stats.get('bootstrap_count', 0)}",
        ]
        recent_bootstraps = stats.get("recent_bootstraps", [])
        if recent_bootstraps:
            lines.append("最近补建：")
            for item in recent_bootstraps:
                lines.append(f"- {item['group_id']} @ {format_timestamp(item['bootstrapped_at'])}")
        return "\n".join(lines)

    if action == "bootstrap":
        target_group = _resolve_group_target(tokens[1] if len(tokens) >= 2 else "", event)
        if not target_group:
            return "请指定目标群号，或在群内使用“当前群”。"
        await asyncio.to_thread(bundle.memory_store.bootstrap_group_memories, target_group)
        return f"已触发群 {target_group} 的记忆补建。"

    if action == "decay":
        purged = await asyncio.to_thread(bundle.memory_decay_scheduler.run_once)
        return f"已执行记忆衰减，处理条目数：{purged}"

    if action == "evolves":
        if bundle.background_intelligence is None:
            return "后台智能未初始化。"
        target_group = _resolve_group_target(tokens[1] if len(tokens) >= 2 else "", event)
        if not target_group:
            return "请指定目标群号，或在群内使用“当前群”。"
        result = await bundle.background_intelligence.run_evolves_for_group(target_group)
        return f"已执行演化检测：扫描 {result.get('processed', 0)} 条，建立关系 {result.get('relations', 0)} 条。"

    if action == "crystal":
        crystal_action = normalize_command_word(tokens[1]) if len(tokens) >= 2 else ""
        if crystal_action not in {"run", ""}:
            return "用法：拟人 记忆 结晶 执行 [当前群|群号]"
        if bundle.background_intelligence is None:
            return "后台智能未初始化。"
        target_group = _resolve_group_target(tokens[2] if len(tokens) >= 3 else "", event)
        result = await bundle.background_intelligence.run_crystal_now(target_group)
        return f"已执行结晶检查，更新 {result.get('updated', 0)} 个候选。"

    return "用法：拟人 记忆 状态｜补建｜衰减｜演化｜结晶 执行"


async def handle_migrate_command(bundle: Any, *, tokens: list[str]) -> str:
    action = normalize_command_word(tokens[0]) if tokens else "status"
    if action == "run":
        await asyncio.to_thread(LegacyMemoryMigrator(bundle.memory_store, logger=bundle.logger).migrate_once)
        return "已执行迁移任务。"
    status = bundle.memory_store.get_migration_status()
    lines = ["迁移状态"]
    for entry in status.get("entries", []):
        lines.append(f"- {entry['migration_key']}: {entry['status']} @ {format_timestamp(entry['updated_at'])}")
    recent_files = status.get("recent_files", [])
    if recent_files:
        lines.append("最近文件:")
        for item in recent_files[:5]:
            lines.append(
                f"- {item['source_path']} => success {item['success_count']} / skip {item['skipped_count']} / error {item['error_count']} ({item['recycle_status']})"
            )
    return "\n".join(lines)


def handle_recall_command(bundle: Any, *, tokens: list[str]) -> str:
    action = normalize_command_word(tokens[0]) if tokens else "stats"
    if action != "stats":
        return "用法：拟人 召回 统计"
    stats = bundle.memory_store.get_recall_stats(limit=8)
    lines = [
        "召回统计",
        f"总检索次数：{stats.get('total', 0)}",
        f"正常命中：{stats.get('ok_count', 0)}",
        f"兜底次数：{stats.get('fallback_count', 0)}",
    ]
    for item in stats.get("recent", [])[:8]:
        lines.append(
            f"- {item['query']} | {item['status']} | 命中 {item['hit_count']} | {format_timestamp(item['created_at'])}"
        )
    return "\n".join(lines)
