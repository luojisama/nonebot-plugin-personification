from __future__ import annotations

from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def run() -> str:
    return "plugin_knowledge skillpack 需要通过 build_tools 注册后使用。"


async def run_list_plugins(runtime: SkillRuntime) -> str:
    store = getattr(runtime, "knowledge_store", None)
    if store is None:
        return "插件知识库未初始化。"
    return impl.list_plugins(store)


async def run_search_plugin_knowledge(runtime: SkillRuntime, query: str, top_k: int = 3) -> str:
    store = getattr(runtime, "knowledge_store", None)
    if store is None:
        return "插件知识库未初始化。"
    return impl.search_plugin_knowledge(query, store, top_k=top_k)


async def run_search_plugin_source(
    runtime: SkillRuntime,
    plugin_name: str,
    query: str,
    top_k: int = 3,
) -> str:
    store = getattr(runtime, "knowledge_store", None)
    if store is None:
        return "插件知识库未初始化。"
    return impl.search_plugin_source(plugin_name, query, store, top_k=top_k)


async def run_list_plugin_features(runtime: SkillRuntime, plugin_name: str) -> str:
    store = getattr(runtime, "knowledge_store", None)
    if store is None:
        return "插件知识库未初始化。"
    return impl.list_plugin_features(plugin_name, store)


async def run_get_feature_detail(
    runtime: SkillRuntime,
    plugin_name: str,
    feature_key: str,
    include_runtime: bool = False,
    include_source: bool = True,
) -> str:
    store = getattr(runtime, "knowledge_store", None)
    if store is None:
        return "插件知识库未初始化。"
    return impl.get_feature_detail(
        plugin_name,
        feature_key,
        store,
        include_runtime=include_runtime,
        include_source=include_source,
    )


def build_tools(runtime: SkillRuntime):
    return impl.build_plugin_knowledge_tools(runtime)

