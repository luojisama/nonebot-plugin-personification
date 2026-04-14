from nonebot.plugin import PluginMetadata


PERSONIFICATION_USAGE = (
    "主要能力：\n"
    "  - 群聊触发回复、随机插话与戳一戳响应\n"
    "  - 私聊自主回复与上下文记忆\n"
    "  - 主动私聊、周记生成、群聊风格学习\n"
    "  - 联网搜索、黑名单与运行时开关\n\n"
    "统一管理入口：\n"
    "  - /persona help\n"
    "  - /persona status\n"
    "  - /persona config list|get|set|reset\n"
    "  - /persona admin list|add|remove\n"
    "  - /persona memory status|bootstrap|decay|evolves|crystal run\n"
    "  - /persona migrate run|status\n"
    "  - /persona recall stats\n"
    "  - 中文别名：拟人 / 人格\n\n"
    "全局开关命令（超管）：\n"
    "  - 拟人开关 [开启/关闭]\n"
    "  - 拟人语音 [开启/关闭]\n"
    "  - 拟人联网 [开启/关闭]\n"
    "  - 拟人主动消息 [开启/关闭]\n"
    "  - 远程技能审批 [查看/同意/拒绝/重置] [名称|key|pending|全部]\n\n"
    "  - 安装远程技能 <GitHub/ClawHub/SkillHub地址> [ref=分支] [subdir=目录] [name=名称]\n"
    "  - 超管也可直接用自然语言要求安装远程 skill\n\n"
    "群聊管理命令（超管）：\n"
    "  - 开启拟人 / 关闭拟人\n"
    "  - 开启表情包 / 关闭表情包\n"
    "  - 开启语音回复 / 关闭语音回复\n"
    "  - 拟人作息 [开启/关闭/全局开启/全局关闭]\n"
    "  - 拟人配置（以聊天记录形式展示配置含义）\n\n"
    "人设与风格命令：\n"
    "  - 设置人设 [群号] <提示词>\n"
    "  - 查看人设\n"
    "  - 重置人设\n"
    "  - 学习群聊风格\n"
    "  - 查看群聊风格 [群号]\n\n"
    "好感度命令：\n"
    "  - 群好感 / 群好感度\n"
    "  - 设置群好感 [群号] [数值]\n\n"
    "白名单命令：\n"
    "  - 申请白名单\n"
    "  - 同意白名单 [群号]\n"
    "  - 拒绝白名单 [群号]\n"
    "  - 添加白名单 [群号]\n"
    "  - 移除白名单 [群号]\n\n"
    "黑名单命令（超管）：\n"
    "  - 永久拉黑 [用户ID/@用户]\n"
    "  - 取消永久拉黑 [用户ID/@用户]\n"
    "  - 永久黑名单列表\n\n"
    "记忆管理命令（超管）：\n"
    "  - 清除记忆 [全局/@用户/用户ID]\n"
    "  - 完全清除记忆\n\n"
    "画像与语音命令：\n"
    "  - 查看画像\n"
    "  - 刷新画像\n"
    "  - 说/朗读/配音 [--voice 音色] [--style 风格] 文本\n\n"
    "其他命令（超管）：\n"
    "  - 发个说说\n\n"
    "帮助命令：\n"
    "  - 拟人帮助\n"
    "  - 拟人命令\n"
    "  - 拟人管理命令\n"
)


def build_plugin_usage_text() -> str:
    return PERSONIFICATION_USAGE


def build_plugin_metadata(config_cls: type) -> PluginMetadata:
    return PluginMetadata(
        name="拟人化聊天",
        description="基于群聊与私聊上下文的人设回复插件，支持作息模拟、联网检索、风格学习、主动私聊、贴图、画像与 Agent 工具调用。",
        usage=build_plugin_usage_text(),
        config=config_cls,
        type="application",
        homepage="https://github.com/luojisama/nonebot-plugin-personification",
        supported_adapters={"~onebot.v11"},
        extra={
            "help_commands": ["拟人帮助", "拟人命令", "拟人管理命令", "/persona help"],
            "command_groups": {
                "persona_admin_root": [
                    "/persona help",
                    "/persona status",
                    "/persona config",
                    "/persona admin",
                    "/persona memory",
                    "/persona migrate",
                    "/persona recall",
                ],
                "global_admin": [
                    "拟人开关",
                    "拟人语音",
                    "拟人联网",
                    "拟人主动消息",
                    "远程技能审批",
                    "安装远程技能",
                ],
                "group_admin": [
                    "开启拟人",
                    "关闭拟人",
                    "开启表情包",
                    "关闭表情包",
                    "开启语音回复",
                    "关闭语音回复",
                    "拟人作息",
                    "拟人配置",
                    "查看人设",
                    "设置人设",
                    "重置人设",
                    "学习群聊风格",
                    "查看群聊风格",
                ],
                "memory_admin": [
                    "清除记忆",
                    "完全清除记忆",
                ],
                "whitelist_admin": [
                    "申请白名单",
                    "同意白名单",
                    "拒绝白名单",
                    "添加白名单",
                    "移除白名单",
                ],
                "moderation_admin": [
                    "永久拉黑",
                    "取消永久拉黑",
                    "永久黑名单列表",
                ],
                "persona": [
                    "查看画像",
                    "刷新画像",
                ],
                "tts": [
                    "说",
                    "朗读",
                    "配音",
                ],
                "diary_admin": [
                    "发个说说",
                ],
            },
            "author": "luojisama",
            "version": "0.5.0",
            "pypi": "nonebot-plugin-shiro-personification",
        },
    )
