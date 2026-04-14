from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import nonebot

from .knowledge_store import PluginKnowledgeStore
from .plugin_inspector import (
    analyze_plugin_with_llm,
    compute_skeleton_hash,
    extract_plugin_skeleton,
    get_plugin_root,
    scan_runtime_data,
)
from ..agent.inner_state import get_personification_data_dir
from ..skills.skillpacks.tool_caller.scripts.impl import ToolCaller


async def build_plugin_knowledge_async(
    plugin_config: Any,
    tool_caller: ToolCaller,
    knowledge_store: PluginKnowledgeStore,
    logger: Any,
) -> None:
    try:
        if tool_caller is None:
            logger.warning("[knowledge_builder] tool_caller 不可用，跳过知识库构建")
            return

        plugins = list(nonebot.get_loaded_plugins() or [])
        build_state = await knowledge_store.load_build_state()
        state_plugins = build_state.get("plugins", {})
        if not isinstance(state_plugins, dict):
            state_plugins = {}
            build_state["plugins"] = state_plugins
        data_dir = get_personification_data_dir(plugin_config)

        for plugin in plugins:
            plugin_name = ""
            skeleton_hash = ""
            try:
                module = getattr(plugin, "module", None)
                if module is None:
                    continue
                module_name = str(getattr(module, "__name__", "") or "")
                plugin_name = str(getattr(plugin, "name", "") or module_name.split(".")[-1]).strip()
                if not plugin_name:
                    continue
                if module_name.startswith("nonebot.plugins"):
                    continue
                if plugin_name == "personification" or module_name.endswith("personification"):
                    continue

                plugin_root = get_plugin_root(plugin)
                if plugin_root is None:
                    logger.warning(f"[knowledge_builder] 无法定位插件根目录: {plugin_name}")
                    continue

                skeleton = extract_plugin_skeleton(plugin_root)
                if not skeleton:
                    logger.warning(f"[knowledge_builder] 插件骨架提取为空: {plugin_name}")
                    continue

                skeleton_hash = compute_skeleton_hash(skeleton)
                previous = state_plugins.get(plugin_name, {}) if isinstance(state_plugins, dict) else {}
                retry_count = int(previous.get("retry_count", 0) or 0) if isinstance(previous, dict) else 0
                if (
                    isinstance(previous, dict)
                    and previous.get("hash") == skeleton_hash
                    and previous.get("status") == "success"
                ):
                    continue
                if isinstance(previous, dict) and previous.get("status") == "failed" and retry_count >= 3:
                    continue

                state_plugins[plugin_name] = {
                    **previous,
                    "hash": skeleton_hash,
                    "status": "pending",
                    "error": "",
                    "retry_count": retry_count,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "root_path": str(plugin_root),
                }
                await knowledge_store.save_build_state(build_state)

                category = "store" if "site-packages" in str(plugin_root).lower() else "local"
                analyzed = await analyze_plugin_with_llm(
                    skeleton=skeleton,
                    plugin_name=plugin_name,
                    tool_caller=tool_caller,
                )
                analyzed["plugin_name"] = plugin_name
                analyzed["module_name"] = module_name
                analyzed["root_path"] = str(plugin_root)
                analyzed["skeleton_hash"] = skeleton_hash
                analyzed["updated_at"] = datetime.now().isoformat(timespec="seconds")

                runtime_snapshot = scan_runtime_data(plugin_name, data_dir)
                await knowledge_store.save_plugin_entry(plugin_name, category, analyzed)
                if runtime_snapshot:
                    await knowledge_store.save_runtime_snapshot(plugin_name, runtime_snapshot)

                state_plugins[plugin_name] = {
                    "hash": skeleton_hash,
                    "status": "success",
                    "error": "",
                    "retry_count": 0,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "category": category,
                    "root_path": str(plugin_root),
                }
                await knowledge_store.save_build_state(build_state)
                await asyncio.sleep(2.0)
            except Exception as exc:
                previous = state_plugins.get(plugin_name, {}) if isinstance(state_plugins, dict) else {}
                state_plugins[plugin_name] = {
                    **(previous if isinstance(previous, dict) else {}),
                    "hash": skeleton_hash,
                    "status": "failed",
                    "error": str(exc),
                    "retry_count": int((previous or {}).get("retry_count", 0) or 0) + 1 if isinstance(previous, dict) else 1,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                await knowledge_store.save_build_state(build_state)
                logger.warning(f"[knowledge_builder] 插件处理失败: {getattr(plugin, 'name', '?')} error={exc}")

        logger.info("[knowledge_builder] 知识库构建完成")
    except Exception as exc:
        logger.warning(f"[knowledge_builder] 未预期异常: {exc}")


def start_knowledge_builder(
    plugin_config: Any,
    tool_caller: ToolCaller,
    knowledge_store: PluginKnowledgeStore,
    logger: Any,
) -> asyncio.Task:
    return asyncio.create_task(
        build_plugin_knowledge_async(
            plugin_config=plugin_config,
            tool_caller=tool_caller,
            knowledge_store=knowledge_store,
            logger=logger,
        )
    )
