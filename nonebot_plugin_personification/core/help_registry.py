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
        summary="查看命令总览、分类帮助或单个配置项说明。",
        usage="拟人 帮助 [分类/命令/配置项]",
        examples=("拟人 帮助", "拟人 帮助 配置", "拟人 帮助 记忆召回条数"),
        permission="所有人可看。",
        scope="global",
    ),
    CommandHelpEntry(
        path=("status",),
        category="status",
        summary="查看运行状态、记忆状态和后台任务状态。",
        usage="拟人 状态",
        examples=("拟人 状态", "人格 状态"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("config", "list"),
        category="config",
        summary="查看所有可配置项、当前值、默认值和用途。",
        usage="拟人 配置 列表 [全局/群]",
        examples=("拟人 配置 列表", "拟人 配置列表 群", "拟人 配置项列表 全局"),
        permission="管理员",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("config", "get"),
        category="config",
        summary="查看单个配置项的当前值、默认值和可选范围。",
        usage="拟人 配置 查看 <配置项> [当前群/群号]",
        examples=("拟人 配置 查看 记忆宫殿", "拟人 配置查看 记忆召回条数", "拟人 配置 查看 本群拟人 当前群"),
        permission="管理员；部分群配置可放行群管理员。",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("config", "set"),
        category="config",
        summary="修改配置项；适合调整开关、上限和记忆策略。",
        usage="拟人 配置 设置 <配置项> <值> [当前群/群号]",
        examples=(
            "拟人 配置 设置 记忆宫殿 开",
            "拟人 配置 设置 记忆召回条数 10",
            "拟人 配置 设置 本群语音回复 关 当前群",
        ),
        permission="管理员；部分群配置可放行群管理员。",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("config", "reset"),
        category="config",
        summary="把配置项恢复到默认值。",
        usage="拟人 配置 重置 <配置项> [当前群/群号]",
        examples=("拟人 配置 重置 记忆宫殿", "拟人 配置重置 记忆召回条数", "拟人 配置 重置 本群拟人 当前群"),
        permission="管理员；部分群配置可放行群管理员。",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("admin", "list"),
        category="admin",
        summary="查看插件管理员列表。",
        usage="拟人 管理员 列表",
        examples=("拟人 管理员 列表",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("admin", "add"),
        category="admin",
        summary="添加插件管理员。",
        usage="拟人 管理员 添加 <QQ号>",
        examples=("拟人 管理员 添加 12345678",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("admin", "remove"),
        category="admin",
        summary="删除插件管理员。",
        usage="拟人 管理员 删除 <QQ号>",
        examples=("拟人 管理员 删除 12345678",),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("memory", "status"),
        category="memory",
        summary="查看记忆系统状态和目录信息。",
        usage="拟人 记忆 状态",
        examples=("拟人 记忆 状态", "拟人 记忆状态"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("memory", "bootstrap"),
        category="memory",
        summary="为群聊补建历史记忆。",
        usage="拟人 记忆 补建 <当前群/群号>",
        examples=("拟人 记忆 补建 当前群", "拟人 记忆补建 123456789"),
        permission="管理员",
        scope="group",
    ),
    CommandHelpEntry(
        path=("memory", "decay"),
        category="memory",
        summary="执行全局记忆衰减维护。",
        usage="拟人 记忆 衰减",
        examples=("拟人 记忆 衰减", "拟人 记忆衰减"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("memory", "evolves"),
        category="memory",
        summary="执行群聊关系演化检测。",
        usage="拟人 记忆 演化 <当前群/群号>",
        examples=("拟人 记忆 演化 当前群", "拟人 记忆演化 123456789"),
        permission="管理员",
        scope="group",
    ),
    CommandHelpEntry(
        path=("memory", "crystal", "run"),
        category="memory",
        summary="执行记忆结晶检查或生成。",
        usage="拟人 记忆 结晶 执行 [当前群/群号]",
        examples=("拟人 记忆 结晶 执行", "拟人 记忆结晶执行 当前群"),
        permission="管理员",
        scope="global/group",
    ),
    CommandHelpEntry(
        path=("recall", "stats"),
        category="recall",
        summary="查看长期记忆召回统计。",
        usage="拟人 召回 统计",
        examples=("拟人 召回 统计", "拟人 召回统计"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("model", "list"),
        category="model",
        summary="查看当前模型路由、覆盖项和已配置模型。",
        usage="拟人 模型 列表",
        examples=("拟人 模型 列表", "拟人 模型 状态"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("model", "use"),
        category="model",
        summary="将 intent/review/agent/sticker 四类 LLM 调用统一切换到指定模型。",
        usage="拟人 模型 使用 <model>",
        examples=("拟人 模型 使用 gpt-5.4", "拟人 模型 使用 gpt-5.4-mini"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("model", "set"),
        category="model",
        summary="只覆盖某一类 LLM 调用的模型。",
        usage="拟人 模型 设置 <intent|review|agent|sticker> <model>",
        examples=("拟人 模型 设置 review gpt-5.4-mini", "拟人 模型 设置 agent gpt-5.4"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("model", "reset"),
        category="model",
        summary="重置模型覆盖，恢复配置文件默认路由。",
        usage="拟人 模型 重置 [intent|review|agent|sticker|全部]",
        examples=("拟人 模型 重置", "拟人 模型 重置 review"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("config", "qzone"),
        category="config",
        summary="启用 QQ 空间说说发布（短说说 + 主动发说说）。",
        usage="需在 .env.prod 中配置以下项目：",
        examples=(
            "personification_qzone_enabled=true",
            "personification_qzone_cookie=<手动填写或用'发个说说'命令自动刷新>",
            "personification_qzone_proactive_enabled=true  # 可选，允许主动发说说",
            "personification_qzone_daily_limit=2           # 每日主动发说说上限",
        ),
        permission="管理员配置",
        scope="global",
    ),
    CommandHelpEntry(
        path=("migrate", "run"),
        category="migrate",
        summary="执行旧数据迁移任务。",
        usage="拟人 迁移 执行",
        examples=("拟人 迁移 执行", "拟人 迁移执行"),
        permission="管理员",
        scope="global",
    ),
    CommandHelpEntry(
        path=("migrate", "status"),
        category="migrate",
        summary="查看旧数据迁移状态。",
        usage="拟人 迁移 状态",
        examples=("拟人 迁移 状态", "拟人 迁移状态"),
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
