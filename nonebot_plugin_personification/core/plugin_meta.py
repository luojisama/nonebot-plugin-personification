from nonebot.plugin import PluginMetadata


def build_plugin_metadata(config_cls: type) -> PluginMetadata:
    return PluginMetadata(
        name="拟人化聊天",
        description="基于群聊与私聊上下文的人设回复插件，支持作息模拟、联网检索、风格学习与主动私聊。",
        usage=(
            "主要能力：\n"
            "  - 群聊触发回复、随机插话与戳一戳响应\n"
            "  - 私聊自主回复与上下文记忆\n"
            "  - 主动私聊、周记生成、群聊风格学习\n"
            "  - 联网搜索、黑名单与运行时开关\n\n"
            "常用管理员命令：\n"
            "  - 查看配置\n"
            "  - 拟人开 / 拟人关\n"
            "  - 贴图开 / 贴图关\n"
            "  - 拟人作息 [开启/关闭/全局开启/全局关闭]\n"
            "  - 联网开关 [开/关]\n"
            "  - 主动私聊开关 [开/关]\n"
            "  - 清除上下文 [全局/@用户/用户ID]\n"
            "  - 学习群聊风格 / 查看群聊风格\n"
            "  - 修改群好感 [群号] [数值]\n"
            "  - 永久拉黑 [用户ID/@用户]\n"
            "  - 取消永久拉黑 [用户ID/@用户]\n"
            "  - 查看永久黑名单\n"
            "  - 手动发说说\n"
        ),
        config=config_cls,
    )
