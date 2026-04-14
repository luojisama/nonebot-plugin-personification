from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandHelpEntry:
    path: tuple[str, ...]
    category: str
    summary: str
    usage: str
    examples: tuple[str, ...]
    permission: str
    scope: str
    hot_reload: str = "立即生效"


_COMMANDS: list[CommandHelpEntry] = [
    CommandHelpEntry(
        path=("help",),
        category="help",
        summary="看帮助。",
        usage="拟人 帮助 [分类/命令/配置项]",
        examples=("拟人 帮助", "拟人 帮助 配置", "拟人 帮助 记忆宫殿"),
        permission="所有人可看。",
        scope="global",
    ),
    CommandHelpEntry(
        path=("status",),
        category="status",
        summary="看当前状态。",
        usage="拟人 状态",
        examples=("拟人 状态", "人格 状态"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("config", "list"),
        category="config",
        summary="看可改配置。",
        usage="拟人 配置列表 [全局/群]",
        examples=("拟人 配置列表", "拟人 配置列表 群"),
        permission="管理员",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("config", "get"),
        category="config",
        summary="看某个配置。",
        usage="拟人 配置 查看 <配置项> [当前群/群号]",
        examples=("拟人 配置 查看 记忆宫殿", "拟人 配置 查看 本群拟人"),
        permission="管理员；部分群配置可放行群管理员。",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("config", "set"),
        category="config",
        summary="修改配置。",
        usage="拟人 配置 设置 <配置项> <值> [当前群/群号]",
        examples=(
            "拟人 配置 设置 记忆宫殿 开",
            "拟人 配置 设置 联网模式 实时",
            "拟人 配置 设置 本群语音回复 关 当前群",
        ),
        permission="管理员；部分群配置可放行群管理员。",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("config", "reset"),
        category="config",
        summary="恢复默认值。",
        usage="拟人 配置 重置 <配置项> [当前群/群号]",
        examples=("拟人 配置 重置 记忆宫殿", "拟人 配置 重置 本群拟人"),
        permission="管理员；部分群配置可放行群管理员。",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("admin", "list"),
        category="admin",
        summary="看管理员列表。",
        usage="拟人 管理员 列表",
        examples=("拟人 管理员 列表",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("admin", "add"),
        category="admin",
        summary="添加管理员。",
        usage="拟人 管理员 添加 <QQ号>",
        examples=("拟人 管理员 添加 12345678",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("admin", "remove"),
        category="admin",
        summary="删除管理员。",
        usage="拟人 管理员 删除 <QQ号>",
        examples=("拟人 管理员 删除 12345678",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("memory", "status"),
        category="memory",
        summary="看记忆状态。",
        usage="拟人 记忆 状态",
        examples=("拟人 记忆 状态",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("memory", "bootstrap"),
        category="memory",
        summary="补建群记忆。",
        usage="拟人 记忆 补建 <当前群/群号>",
        examples=("拟人 记忆 补建 当前群",),
        permission="管理员",
        scope="group",
    ),
    CommandHelpEntry(
        path=("memory", "decay"),
        category="memory",
        summary="执行记忆衰减。",
        usage="拟人 记忆 衰减",
        examples=("拟人 记忆 衰减",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("memory", "evolves"),
        category="memory",
        summary="执行演化关系检测。",
        usage="拟人 记忆 演化 <当前群/群号>",
        examples=("拟人 记忆 演化 当前群",),
        permission="管理员",
        scope="group",
    ),
    CommandHelpEntry(
        path=("memory", "crystal", "run"),
        category="memory",
        summary="执行记忆结晶检查。",
        usage="拟人 记忆 结晶 执行 [当前群/群号]",
        examples=("拟人 记忆 结晶 执行",),
        permission="管理员",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("recall", "stats"),
        category="recall",
        summary="看 recall 统计。",
        usage="拟人 召回 统计",
        examples=("拟人 召回 统计",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("migrate", "run"),
        category="migrate",
        summary="执行旧数据迁移。",
        usage="拟人 迁移 执行",
        examples=("拟人 迁移 执行",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("migrate", "status"),
        category="migrate",
        summary="看迁移状态。",
        usage="拟人 迁移 状态",
        examples=("拟人 迁移 状态",),
        permission="管理员",
        scope="global",
    ),
]


def get_command_help_entries() -> list[CommandHelpEntry]:
    return list(_COMMANDS)


def find_command_help(path: tuple[str, ...]) -> CommandHelpEntry | None:
    normalized = tuple(str(item or "").strip().lower() for item in path if str(item or "").strip())
    for entry in _COMMANDS:
        if tuple(str(part).lower() for part in entry.path) == normalized:
            return entry
    return None


def find_entries_by_category(category: str) -> list[CommandHelpEntry]:
    normalized = str(category or "").strip().lower()
    return [entry for entry in _COMMANDS if entry.category == normalized]
