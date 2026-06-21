from __future__ import annotations

from plugin.personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


def build_tools(_runtime: SkillRuntime):
    """启动时不注册任何工具：invoke_plugin 需要当前 bot/event，必须 per-request 注册
    （见 build_invoke_plugin_tool_for_runtime）。返回空列表以避免 loader 的缺失告警。"""
    return []


def build_invoke_plugin_tool_for_runtime(*, bot, event, runtime: SkillRuntime):
    """Per-request 构造 invoke_plugin 工具。

    该工具需要当前的 bot / event 才能代为分发命令并捕获输出，因此由
    handlers/reply_pipeline/pipeline_context.py 在每次回复时调用，而不是作为
    静态 skillpack 的 build_tools（loader 拿不到 event）。
    """
    return impl.build_invoke_plugin_tool(
        bot=bot,
        event=event,
        knowledge_store=runtime.knowledge_store,
        plugin_config=runtime.plugin_config,
        logger=runtime.logger,
    )
