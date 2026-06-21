from __future__ import annotations

import asyncio
import json
import re
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
from ..core.provider_router import get_provider_failure_snapshot, normalize_api_type, parse_api_pool_config
from ..core.runtime_config import get_runtime_load_info
from ..utils import get_group_config, is_group_whitelisted
from .runtime_commands import handle_git_update_command as _handle_git_update_command


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
    "scheduler": "scheduler",
    "schedule": "scheduler",
    "定时": "scheduler",
    "任务": "scheduler",
    "qzone": "qzone",
    "空间": "qzone",
    "说说": "qzone",
    "update": "update",
    "更新": "update",
    "升级": "update",
    "sticker": "sticker",
    "表情包": "sticker",
    "lore": "lore",
    "设定": "lore",
    "lorebook": "lore",
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
    "scan": "run",
    "扫描": "run",
    "test": "test",
    "测试": "test",
    "message": "message",
    "messages": "message",
    "inbox": "message",
    "消息": "message",
    "留言": "message",
    "收消息": "message",
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
    "curate": "curate",
    "整理": "curate",
    "clean": "clean",
    "清理": "clean",
    "rollback": "rollback",
    "回滚": "rollback",
    "设定": "set",
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
    "定时状态": ("定时", "状态"),
    "任务状态": ("定时", "状态"),
    "空间状态": ("空间", "状态"),
    "空间扫描": ("空间", "扫描"),
    "空间测试": ("空间", "测试"),
    "空间消息": ("空间", "消息"),
    "空间留言": ("空间", "消息"),
    "空间收消息": ("空间", "消息"),
    "模型路由": ("模型", "路由"),
    "模型调用": ("模型", "路由"),
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


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(float(seconds or 0))))
    if total <= 0:
        return "0秒"
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}时{minutes}分"
    if minutes:
        return f"{minutes}分{sec}秒"
    return f"{sec}秒"


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
        "- 拟人 模型 列表|路由|使用|设置|重置：QQ 内热更新主 provider 和各阶段模型。",
        "- 拟人 定时 状态：查看定时任务注册状态、下次运行时间和功能开关。",
        "- 拟人 空间 状态|测试 <QQ号/@用户>：查看空间任务状态，或对指定好友空间执行一次测试互动扫描。",
        "- 拟人 表情包 整理|清理|回滚：整理表情包库（去重/去低质量）、清理过期 trash、回滚最近一次整理。",
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
        "scheduler": "定时任务",
        "qzone": "QQ 空间",
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


async def handle_sticker_command(bundle: Any, *, event: Any, tokens: list[str]) -> str:
    subcommand = normalize_command_word(tokens[0]) if tokens else ""
    if subcommand not in {"curate", "clean", "rollback"}:
        return "用法：拟人 表情包 整理｜清理｜回滚"

    if subcommand == "curate":
        try:
            from ..core.sticker_curator import run_sticker_curation
        except ImportError:
            return "表情包馆长模块加载失败。"
        result = await run_sticker_curation(runtime=bundle.runtime)
        return "\n".join(result.details) if result.details else "整理完成。"

    if subcommand == "clean":
        try:
            from ..core.sticker_curator import clean_trash_expired
            from ..core.sticker_library import resolve_sticker_dir
        except ImportError:
            return "表情包馆长模块加载失败。"
        sticker_dir = resolve_sticker_dir(getattr(bundle.runtime.plugin_config, "personification_sticker_path", None))
        removed = clean_trash_expired(sticker_dir)
        return f"已清理过期 trash 目录，共删除 {removed} 个过期批次。"

    if subcommand in {"rollback", "回滚"}:
        try:
            from ..core.sticker_curator import rollback_last_curation
            from ..core.sticker_library import resolve_sticker_dir
        except ImportError:
            return "表情包馆长模块加载失败。"
        sticker_dir = resolve_sticker_dir(getattr(bundle.runtime.plugin_config, "personification_sticker_path", None))
        result = rollback_last_curation(sticker_dir)
        return "\n".join(result.details) if result.details else "回滚完成。"

    return "未知子命令。"


async def handle_lore_command(bundle: Any, *, tokens: list[str]) -> str:
    subcommand = normalize_command_word(tokens[0]) if tokens else ""
    if subcommand not in {"set", "remove", "list"}:
        return "用法：拟人 设定 <主题> <立场>｜列出设定｜删除设定 <主题>"

    try:
        from ..core.persona_knowledge import add_lorebook_entry, list_lorebook_entries, remove_lorebook_entry
    except ImportError:
        return "人格知识库模块加载失败。"

    memory_store = getattr(bundle.runtime, "memory_store", None)
    if not memory_store:
        return "记忆存储不可用。"

    if subcommand == "list":
        entries = await list_lorebook_entries(memory_store)
        if not entries:
            return "当前没有人格设定。"
        lines = ["当前人格设定："]
        for entry in entries:
            topic = str(entry.get("topic", "") or entry.get("summary", "")).strip()[:80]
            stance = str(entry.get("stance", "") or "").strip()[:120]
            lines.append(f"- {topic}: {stance}")
        return "\n".join(lines)

    if subcommand == "remove":
        topic = " ".join(str(t) for t in tokens[1:]).strip() if len(tokens) > 1 else ""
        if not topic:
            return "请指定要删除的设定主题。"
        success = await remove_lorebook_entry(memory_store, topic)
        return f"已删除设定「{topic}」。" if success else f"未找到设定「{topic}」。"

    if subcommand == "set":
        if len(tokens) < 3:
            return "用法：拟人 设定 <主题> <立场>"
        topic = str(tokens[1] or "").strip()
        stance = " ".join(str(t) for t in tokens[2:]).strip()
        if not topic or not stance:
            return "主题和立场均不能为空。"
        triggers = " ".join(tokens[1:]).strip()
        await add_lorebook_entry(memory_store, topic=topic, stance=stance, triggers=triggers)
        return f"已设定「{topic}」：{stance}"

    return "未知子命令。"


async def dispatch_persona_admin_command(
    matcher: Any,
    *,
    bot: Any = None,
    bundle: Any,
    event: Any,
    arg_text: str,
    arg_message: Any = None,
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

    if command == "scheduler":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(handle_scheduler_command(bundle, tokens=rest))

    if command == "qzone":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(await handle_qzone_command(bundle, event=event, tokens=rest, arg_message=arg_message))

    if command == "sticker":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(await handle_sticker_command(bundle, event=event, tokens=rest))
        return

    if command == "lore":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await matcher.finish(await handle_lore_command(bundle, tokens=rest))
        return

    if command == "update":
        if not can_manage_sensitive_action(event=event, superusers=bundle.superusers):
            await matcher.finish(_admin_error())
        await _handle_git_update_command(matcher, bot=bot, event=event, logger=bundle.logger)
        return

    await matcher.finish("未识别的子命令。可用“拟人 帮助”或”/persona help”查看帮助。")


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
    if normalized[0] in {"config", "admin", "memory", "migrate", "help", "status", "recall", "model", "scheduler", "qzone", "update"}:
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


def _load_data_store_namespace(name: str) -> dict[str, Any]:
    try:
        from ..core.data_store import get_data_store

        payload = get_data_store().load_sync(name)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _render_qzone_status_summary(bundle: Any) -> str:
    post_state = _load_data_store_namespace("qzone_post_state")
    social_state = _load_data_store_namespace("qzone_social_state")
    qzone_enabled = "开" if bool(getattr(bundle.plugin_config, "personification_qzone_enabled", False)) else "关"
    proactive_enabled = "开" if bool(getattr(bundle.plugin_config, "personification_qzone_proactive_enabled", False)) else "关"
    social_enabled = "开" if bool(getattr(bundle.plugin_config, "personification_qzone_social_enabled", False)) else "关"
    inbound_enabled = "开" if bool(getattr(bundle.plugin_config, "personification_qzone_inbound_enabled", True)) else "关"
    return (
        f"QQ空间：总开关 {qzone_enabled}，主动发说说 {proactive_enabled}"
        f"（今日 {int(post_state.get('count', 0) or 0)} 条），好友互动 {social_enabled}"
        f"（赞 {int(social_state.get('like_count', 0) or 0)} / 评 {int(social_state.get('comment_count', 0) or 0)}），"
        f"空间消息 {inbound_enabled}"
    )


def render_qzone_status(bundle: Any) -> str:
    post_state = _load_data_store_namespace("qzone_post_state")
    social_state = _load_data_store_namespace("qzone_social_state")
    last_result = social_state.get("last_result") if isinstance(social_state.get("last_result"), dict) else {}
    last_inbound_result = (
        social_state.get("last_inbound_result")
        if isinstance(social_state.get("last_inbound_result"), dict)
        else {}
    )
    lines = [
        "QQ 空间状态",
        f"总开关：{'开' if bool(getattr(bundle.plugin_config, 'personification_qzone_enabled', False)) else '关'}",
        f"主动发说说：{'开' if bool(getattr(bundle.plugin_config, 'personification_qzone_proactive_enabled', False)) else '关'}",
        f"主动发说说频率：每 {getattr(bundle.plugin_config, 'personification_qzone_check_interval', 90)} 分钟检查，"
        f"每月最多 {getattr(bundle.plugin_config, 'personification_qzone_monthly_limit', 30)} 条，"
        f"最小间隔 {getattr(bundle.plugin_config, 'personification_qzone_min_interval_hours', 6.0)} 小时",
        f"本月已发：{int(post_state.get('count', 0) or 0)} 条",
        f"上次发说说：{format_timestamp(float(post_state.get('last_post_at', 0) or 0))}",
        f"上次内容：{post_state.get('last_content') or '无'}",
        "",
        f"好友空间互动：{'开' if bool(getattr(bundle.plugin_config, 'personification_qzone_social_enabled', False)) else '关'}",
        f"扫描范围：{getattr(bundle.plugin_config, 'personification_qzone_social_scope', 'recent_interactions')}",
        f"扫描间隔：{getattr(bundle.plugin_config, 'personification_qzone_social_check_interval', 120)} 分钟",
        f"单次动态数：{getattr(bundle.plugin_config, 'personification_qzone_social_max_feeds_per_scan', 10)}",
        f"点赞上限：{getattr(bundle.plugin_config, 'personification_qzone_social_like_limit', 0)}（0=无上限）",
        f"评论上限：{getattr(bundle.plugin_config, 'personification_qzone_social_comment_limit', 0)}（0=无上限）",
        f"单好友上限：{getattr(bundle.plugin_config, 'personification_qzone_social_per_friend_limit', 0)}（0=无上限）",
        f"今日点赞/评论：{int(social_state.get('like_count', 0) or 0)} / {int(social_state.get('comment_count', 0) or 0)}",
        f"上次扫描：{format_timestamp(float(social_state.get('last_scan_at', 0) or 0))}",
        f"上次结果：用户 {last_result.get('scanned_users', 0)}，动态 {last_result.get('feeds_seen', 0)}，"
        f"点赞 {last_result.get('liked', 0)}，评论 {last_result.get('commented', 0)}，"
        f"留言 {last_result.get('inbound_comments', 0)}，回复 {last_result.get('replied', 0)}，"
        f"画像补充 {last_result.get('profile_records', 0)}，忽略 {last_result.get('ignored', 0)}，失败 {last_result.get('failed', 0)}",
        f"最近失败：{social_state.get('last_error') or '无'}",
        "",
        f"空间消息轮询：{'开' if bool(getattr(bundle.plugin_config, 'personification_qzone_inbound_enabled', True)) else '关'}",
        f"消息轮询间隔：{getattr(bundle.plugin_config, 'personification_qzone_inbound_check_interval', 3)} 分钟",
        f"单次检查说说：{getattr(bundle.plugin_config, 'personification_qzone_inbound_max_feeds_per_scan', 20)}",
        f"单说说留言：{getattr(bundle.plugin_config, 'personification_qzone_inbound_max_comments_per_feed', 20)}",
        f"上次消息轮询：{format_timestamp(float(social_state.get('last_inbound_scan_at', 0) or 0))}",
        f"上次消息结果：说说 {last_inbound_result.get('feeds_seen', 0)}，"
        f"留言 {last_inbound_result.get('inbound_comments', 0)}，"
        f"回复 {last_inbound_result.get('replied', 0)}，"
        f"画像补充 {last_inbound_result.get('profile_records', 0)}，失败 {last_inbound_result.get('failed', 0)}",
        f"最近消息失败：{social_state.get('last_inbound_error') or '无'}",
    ]
    return "\n".join(lines)


def _extract_at_user_id_from_message(arg_message: Any) -> str:
    if arg_message is None:
        return ""
    try:
        segments = list(arg_message)
    except Exception:
        segments = []
    for segment in segments:
        seg_type = str(getattr(segment, "type", "") or "")
        data = getattr(segment, "data", None)
        if data is None and isinstance(segment, dict):
            seg_type = str(segment.get("type", "") or "")
            data = segment.get("data")
        if seg_type != "at" or not isinstance(data, dict):
            continue
        qq = str(data.get("qq", "") or "").strip()
        if qq and qq.lower() != "all":
            return qq
    raw = str(arg_message or "")
    match = re.search(r"\[CQ:at,qq=(\d+)\]", raw)
    return match.group(1) if match else ""


def _extract_qzone_target_user_id(tokens: list[str], arg_message: Any = None) -> str:
    at_user_id = _extract_at_user_id_from_message(arg_message)
    if at_user_id:
        return at_user_id
    for token in tokens:
        match = re.search(r"\d{5,12}", str(token or ""))
        if match:
            return match.group(0)
    return ""


def _format_qzone_scan_result(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return "空间扫描未返回有效结果。"
    lines = [
        "空间互动扫描结果",
        f"状态：{'成功' if result.get('ok') else '跳过/失败'}",
        f"目标：{result.get('target_user_id') or '最近互动好友'}",
        f"扫描用户：{result.get('scanned_users', 0)}",
        f"读取动态：{result.get('feeds_seen', 0)}",
        f"点赞：{result.get('liked', 0)}",
        f"评论：{result.get('commented', 0)}",
        f"收到留言：{result.get('inbound_comments', 0)}",
        f"回复留言：{result.get('replied', 0)}",
        f"画像补充：{result.get('profile_records', 0)}",
        f"忽略：{result.get('ignored', 0)}",
        f"失败：{result.get('failed', 0)}",
    ]
    if result.get("last_error"):
        lines.append(f"最近错误：{result.get('last_error')}")
    return "\n".join(lines)


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
        _render_qzone_status_summary(bundle),
        _render_scheduler_status_summary(bundle),
    ]
    return "\n".join(lines)


_SCHEDULER_EXPECTED_JOBS: tuple[tuple[str, str, str], ...] = (
    ("personification_daily_fav_report", "每日群好感统计", ""),
    ("ai_weekly_diary", "每周空间说说", "personification_qzone_enabled"),
    ("personification_proactive_messaging", "主动私聊", "personification_proactive_enabled"),
    ("personification_group_idle_topic", "群空闲发话", "personification_group_idle_enabled"),
    ("personification_proactive_qzone", "主动空间动态", "personification_qzone_proactive_enabled"),
    ("personification_qzone_social_scan", "好友空间互动", "personification_qzone_social_enabled"),
    ("personification_qzone_inbound_poll", "空间消息轮询", "personification_qzone_inbound_enabled"),
    ("personification_background_intelligence", "后台智能维护", ""),
)


def _format_scheduler_time(value: Any) -> str:
    if value is None:
        return "未计划"
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
        except Exception:
            pass
    return str(value)


def _get_scheduler_jobs(bundle: Any) -> list[Any]:
    scheduler = getattr(bundle, "scheduler", None)
    if scheduler is None:
        return []
    getter = getattr(scheduler, "get_jobs", None)
    if not callable(getter):
        return []
    try:
        return list(getter() or [])
    except Exception:
        return []


def _scheduler_running_text(bundle: Any) -> str:
    scheduler = getattr(bundle, "scheduler", None)
    if scheduler is None:
        return "未初始化"
    running = getattr(scheduler, "running", None)
    if running is None:
        state = getattr(scheduler, "state", None)
        return f"未知(state={state})" if state is not None else "未知"
    return "运行中" if bool(running) else "未运行"


def _expected_job_enabled(bundle: Any, flag_field: str) -> str:
    if not flag_field:
        return "应注册"
    enabled = bool(getattr(bundle.plugin_config, flag_field, False))
    return "开" if enabled else "关"


def _render_scheduler_status_summary(bundle: Any) -> str:
    jobs = _get_scheduler_jobs(bundle)
    job_ids = {str(getattr(job, "id", "") or "") for job in jobs}
    expected_ids = {job_id for job_id, _, flag in _SCHEDULER_EXPECTED_JOBS if not flag or bool(getattr(bundle.plugin_config, flag, False))}
    missing = sorted(job_id for job_id in expected_ids if job_id not in job_ids)
    return f"定时任务：{_scheduler_running_text(bundle)}，已注册 {len(jobs)} 个，缺失 {len(missing)} 个"


def render_scheduler_status(bundle: Any) -> str:
    jobs = _get_scheduler_jobs(bundle)
    by_id = {str(getattr(job, "id", "") or ""): job for job in jobs}
    lines = [
        "定时任务状态",
        f"Scheduler：{_scheduler_running_text(bundle)}",
        f"已注册任务数：{len(jobs)}",
    ]
    for job_id, label, flag_field in _SCHEDULER_EXPECTED_JOBS:
        job = by_id.get(job_id)
        enabled_text = _expected_job_enabled(bundle, flag_field)
        if job is None:
            lines.append(f"- {label}：未注册（开关：{enabled_text}，id={job_id}）")
            continue
        trigger = str(getattr(job, "trigger", "") or "未知触发器")
        next_run = _format_scheduler_time(getattr(job, "next_run_time", None))
        lines.append(f"- {label}：已注册（开关：{enabled_text}，下次：{next_run}，触发：{trigger}，id={job_id}）")
    extra_jobs = [job for job in jobs if str(getattr(job, "id", "") or "") not in {item[0] for item in _SCHEDULER_EXPECTED_JOBS}]
    if extra_jobs:
        lines.append("其他任务：")
        for job in extra_jobs[:12]:
            lines.append(
                f"- {getattr(job, 'id', 'unknown')}：下次 {_format_scheduler_time(getattr(job, 'next_run_time', None))}"
            )
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


def _parse_api_pool_config(raw_config: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in parse_api_pool_config(raw_config) if isinstance(item, dict)]


def _current_api_pool_config(bundle: Any) -> list[dict[str, Any]]:
    pools = _parse_api_pool_config(getattr(bundle.plugin_config, "personification_api_pools", None))
    provider_getter = getattr(bundle, "get_configured_api_providers", None)
    if not callable(provider_getter):
        return pools
    try:
        providers = list(provider_getter() or [])
    except Exception:
        providers = []
    normalized = [dict(item) for item in providers if isinstance(item, dict)]
    if normalized and len(normalized) > len(pools):
        return normalized
    if pools:
        return pools
    return normalized


def _default_route_model(api_type: str, config: Any) -> str:
    normalized = normalize_api_type(api_type)
    if normalized == "openai_codex":
        return str(getattr(config, "personification_model", "") or "").strip() or "gpt-5.3-codex"
    if normalized == "gemini_cli":
        return "auto-gemini-3"
    if normalized == "antigravity_cli":
        return "auto-gemini-3"
    if normalized == "claude_code":
        return "claude-opus-4-7"
    return str(getattr(config, "personification_model", "") or "").strip()


def _default_route_auth_path(api_type: str, config: Any) -> str:
    normalized = normalize_api_type(api_type)
    if normalized == "openai_codex":
        return str(getattr(config, "personification_codex_auth_path", "") or "").strip() or "~/.codex/auth.json"
    if normalized == "gemini_cli":
        return str(getattr(config, "personification_gemini_cli_auth_path", "") or "").strip() or "~/.gemini/oauth_creds.json"
    if normalized == "antigravity_cli":
        return (
            str(getattr(config, "personification_antigravity_cli_auth_path", "") or "").strip()
            or "~/.gemini/antigravity-cli/oauth_creds.json"
        )
    if normalized == "claude_code":
        return str(getattr(config, "personification_claude_code_auth_path", "") or "").strip() or "~/.claude/.credentials.json"
    return ""


def _route_target_matches(provider: dict[str, Any], target: str, normalized_target: str) -> bool:
    name = str(provider.get("name", "") or "").strip().lower()
    api_type = normalize_api_type(provider.get("api_type"))
    raw = str(target or "").strip().lower()
    return bool(raw and name == raw) or bool(normalized_target and api_type == normalized_target)


def _new_route_provider(api_type: str, model: str, config: Any) -> dict[str, Any]:
    normalized = normalize_api_type(api_type)
    provider = {
        "name": f"{normalized}_primary" if normalized else "custom_primary",
        "api_type": normalized,
        "api_url": "",
        "api_key": "",
        "model": model or _default_route_model(normalized, config),
        "auth_path": _default_route_auth_path(normalized, config),
        "enabled": True,
        "supports_native_search": True,
        "priority": 1,
    }
    if normalized == "gemini_cli":
        provider["project"] = str(getattr(config, "personification_gemini_cli_project", "") or "").strip()
    elif normalized == "antigravity_cli":
        provider["project"] = str(getattr(config, "personification_antigravity_cli_project", "") or "").strip()
    return provider


def _format_provider_line(provider: dict[str, Any], failure_state: dict[str, Any] | None = None) -> str:
    name = str(provider.get("name", "") or "unnamed")
    api_type = str(provider.get("api_type", "") or "unset")
    model = str(provider.get("model", "") or "未配置")
    priority = provider.get("priority", "-")
    enabled = "开" if provider.get("enabled", True) else "关"
    status = ""
    if failure_state:
        remaining = float(failure_state.get("cooldown_remaining", 0) or 0)
        if remaining > 0:
            label = "限流冷却" if failure_state.get("rate_limited") else "失败冷却"
            status = f"，状态={label} {_format_duration(remaining)}"
        elif failure_state.get("last_error"):
            status = "，状态=最近失败，已可重试"
    return f"- {name}（{api_type}:{model}，priority={priority}，enabled={enabled}{status}）"


def _save_route_config(bundle: Any, providers: list[dict[str, Any]]) -> None:
    setattr(bundle.plugin_config, "personification_api_pools", providers)
    setattr(bundle.plugin_config, "personification_model_overrides", {})
    if callable(getattr(bundle, "save_plugin_runtime_config", None)):
        bundle.save_plugin_runtime_config()
    reload_services = getattr(bundle, "reload_runtime_services", None)
    if callable(reload_services):
        reload_services()


def _switch_primary_provider_route(bundle: Any, *, target: str, model: str = "") -> str:
    raw_target = str(target or "").strip()
    normalized_target = normalize_api_type(raw_target)
    if not raw_target:
        return "用法：拟人 模型 路由 <provider_name|api_type> [model]"
    if normalized_target == "openai" and raw_target.lower() not in {"openai", "openai_official"}:
        normalized_target = ""

    existing = _current_api_pool_config(bundle)
    selected_index: int | None = None
    for index, provider in enumerate(existing):
        if _route_target_matches(provider, raw_target, normalized_target):
            selected_index = index
            break

    if selected_index is None:
        if not normalized_target:
            names = [str(item.get("name", "") or "").strip() for item in existing if item.get("name")]
            hint = "、".join(names[:8]) if names else "无"
            return f"没找到这个 provider：{raw_target}\n可用 provider：{hint}\n也可以直接写 api_type：antigravity_cli、gemini_cli、claude_code、openai_codex。"
        selected = _new_route_provider(normalized_target, model, bundle.plugin_config)
        existing.insert(0, selected)
        selected_index = 0
    else:
        selected = dict(existing[selected_index])
        selected["api_type"] = normalize_api_type(selected.get("api_type"))
        if model:
            selected["model"] = model
        elif not str(selected.get("model", "") or "").strip():
            selected["model"] = _default_route_model(selected["api_type"], bundle.plugin_config)
        if not str(selected.get("auth_path", "") or "").strip():
            auth_path = _default_route_auth_path(selected["api_type"], bundle.plugin_config)
            if auth_path:
                selected["auth_path"] = auth_path
        if selected["api_type"] == "gemini_cli" and "project" not in selected:
            selected["project"] = str(getattr(bundle.plugin_config, "personification_gemini_cli_project", "") or "").strip()
        if selected["api_type"] == "antigravity_cli" and "project" not in selected:
            selected["project"] = str(getattr(bundle.plugin_config, "personification_antigravity_cli_project", "") or "").strip()
        selected["enabled"] = True
        existing[selected_index] = selected

    ordered: list[dict[str, Any]] = []
    selected = dict(existing[selected_index])
    selected["priority"] = 1
    selected["enabled"] = True
    ordered.append(selected)
    next_priority = 2
    selected_name = str(selected.get("name", "") or "")
    for index, provider in enumerate(existing):
        if index == selected_index:
            continue
        item = dict(provider)
        if selected_name and str(item.get("name", "") or "") == selected_name:
            continue
        item["api_type"] = normalize_api_type(item.get("api_type"))
        item["priority"] = next_priority
        next_priority += 1
        ordered.append(item)

    _save_route_config(bundle, ordered)
    return (
        "已热切主 provider："
        f"{selected.get('name')}（{selected.get('api_type')}:{selected.get('model') or '未配置'}）\n"
        "已清空模型覆盖，避免把旧 GPT 模型名套到新 provider。\n"
        "生效：立即生效；如果 .env.prod 显式设置 personification_api_pools，重启后仍以 .env.prod 为准。"
    )


def _format_model_status(bundle: Any) -> str:
    from ..core.runtime_config import _collect_env_file_keys
    lines = ["模型路由"]
    providers = _current_api_pool_config(bundle)
    env_file_keys = _collect_env_file_keys()
    pool_from_env = "personification_api_pools" in env_file_keys
    source_label = "来源：.env.prod/.env" if pool_from_env else "来源：runtime_config（.env.prod 未设置此项）"
    # 诊断信息：raw 配置的类型/长度/解析后数量，方便排查 .env 多行 JSON 解析失败问题
    raw_pools = getattr(bundle.plugin_config, "personification_api_pools", None)
    if raw_pools is None:
        raw_info = "raw=None"
    elif isinstance(raw_pools, str):
        raw_info = f"raw=str(len={len(raw_pools)}) head={raw_pools[:80]!r}"
    elif isinstance(raw_pools, list):
        names = []
        for item in raw_pools[:5]:
            if isinstance(item, dict):
                names.append(str(item.get("name", "?")))
        raw_info = f"raw=list(len={len(raw_pools)}) names={names}"
    else:
        raw_info = f"raw={type(raw_pools).__name__}"
    lines.append(f"诊断：{raw_info}; 解析后 provider 数={len(providers)}")
    lines.append(f"当前 provider（{source_label}）：")
    provider_failures = get_provider_failure_snapshot()
    if providers:
        for provider in providers[:8]:
            provider_name = str(provider.get("name", "") or "")
            lines.append(_format_provider_line(provider, provider_failures.get(provider_name)))
    else:
        lines.append("- 未配置")
    lines.append("")
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
    lines.append("用法：拟人 模型 路由 <provider_name|api_type> [model]；拟人 模型 使用 <model>；拟人 模型 设置 <intent|review|agent|sticker> <model>；拟人 模型 重置 [role|全部]")
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

    if raw_action in {"route", "路由", "主路由", "provider", "供应商", "调用"}:
        if len(tokens) < 2:
            return "用法：拟人 模型 路由 <provider_name|api_type> [model]"
        target = str(tokens[1] or "").strip()
        model = " ".join(str(token) for token in tokens[2:]).strip()
        return _switch_primary_provider_route(bundle, target=target, model=model)

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

    return "用法：拟人 模型 列表｜路由 <provider_name|api_type> [model]｜使用 <model>｜设置 <intent|review|agent|sticker> <model>｜重置 [role|全部]"


def handle_scheduler_command(bundle: Any, *, tokens: list[str]) -> str:
    action = normalize_command_word(tokens[0]) if tokens else "status"
    if action in {"status", "list", "get"}:
        return render_scheduler_status(bundle)
    return "用法：拟人 定时 状态"


async def handle_qzone_command(
    bundle: Any,
    *,
    event: Any,
    tokens: list[str],
    arg_message: Any = None,
) -> str:
    _ = event
    action = normalize_command_word(tokens[0]) if tokens else "status"
    if action in {"status", "list", "get"}:
        return render_qzone_status(bundle)
    if action in {"run", "test"}:
        target_user_id = _extract_qzone_target_user_id(tokens[1:] if len(tokens) > 1 else [], arg_message)
        if action == "test" and not target_user_id:
            return "用法：拟人 空间 测试 <QQ号/@用户>"
        scanner = getattr(bundle, "qzone_social_scan", None)
        if not callable(scanner):
            return "好友空间互动扫描任务未初始化。请确认 personification_qzone_enabled=true 后重启。"
        try:
            result = await scanner(target_user_id=target_user_id, force=(action == "test"))
        except TypeError:
            result = await scanner(target_user_id)
        return _format_qzone_scan_result(result if isinstance(result, dict) else {})
    if action == "message":
        poller = getattr(bundle, "qzone_inbound_poll", None)
        if not callable(poller):
            return "空间消息轮询任务未初始化。请确认 personification_qzone_enabled=true 后重启。"
        result = await poller(force=True)
        return _format_qzone_scan_result(result if isinstance(result, dict) else {})
    return "用法：拟人 空间 状态｜扫描｜测试 <QQ号/@用户>｜消息"


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
