from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import nonebot

from .knowledge_store import PluginKnowledgeStore
from .plugin_inspector import (
    analyze_plugin_with_llm,
    compute_source_hash,
    extract_plugin_source_snapshot,
    get_plugin_root,
    scan_runtime_data,
)
from ..agent.inner_state import get_personification_data_dir
from ..skills.skillpacks.tool_caller.scripts.impl import ToolCaller


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_recent_error(previous: dict[str, Any], error_entry: dict[str, Any]) -> list[dict[str, Any]]:
    recent = list(previous.get("recent_errors") or []) if isinstance(previous, dict) else []
    normalized = {
        "phase": str(error_entry.get("phase", "") or "").strip(),
        "error_type": str(error_entry.get("error_type", "") or "").strip(),
        "error_message": str(error_entry.get("error_message", "") or "").strip(),
        "raw_preview": str(error_entry.get("raw_preview", "") or "").strip()[:600],
        "updated_at": str(error_entry.get("updated_at", "") or _now_iso()),
        "failed_batch_index": int(error_entry.get("failed_batch_index", 0) or 0),
        "failed_batch_total": int(error_entry.get("failed_batch_total", 0) or 0),
    }
    recent.append(normalized)
    return recent[-3:]


def _extract_failure_details(exc: Exception) -> dict[str, Any]:
    return {
        "phase": str(getattr(exc, "phase", "") or "unknown"),
        "error_type": exc.__class__.__name__,
        "error_message": str(exc),
        "raw_preview": str(getattr(exc, "raw_preview", "") or "").strip()[:600],
        "failed_batch_index": int(getattr(exc, "failed_batch_index", 0) or 0),
        "failed_batch_total": int(getattr(exc, "failed_batch_total", 0) or 0),
    }


def _iter_target_loaded_plugin_names() -> set[str]:
    plugin_names: set[str] = set()
    for plugin in list(nonebot.get_loaded_plugins() or []):
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
        plugin_names.add(plugin_name)
    return plugin_names


async def _save_build_control_state(
    knowledge_store: PluginKnowledgeStore,
    *,
    enabled: bool,
    trigger: str,
    result: str,
    action: str,
    reasons: list[str] | None = None,
    clear_current: bool = False,
) -> None:
    build_state = await knowledge_store.load_build_state()
    plugins = build_state.get("plugins", {})
    if not isinstance(plugins, dict):
        build_state["plugins"] = {}
    if clear_current:
        build_state["current"] = {}
    control = build_state.get("control", {})
    if not isinstance(control, dict):
        control = {}
    control.update(
        {
            "enabled": bool(enabled),
            "last_check_trigger": str(trigger or "").strip(),
            "last_check_result": str(result or "").strip(),
            "last_check_action": str(action or "").strip(),
            "last_check_reasons": list(reasons or []),
            "updated_at": _now_iso(),
        }
    )
    build_state["control"] = control
    await knowledge_store.save_build_state(build_state)


async def inspect_plugin_knowledge_health(
    knowledge_store: PluginKnowledgeStore,
) -> dict[str, Any]:
    index = await knowledge_store.load_index()
    build_state = await knowledge_store.load_build_state()
    plugins = index.get("plugins", {}) if isinstance(index, dict) else {}
    state_plugins = build_state.get("plugins", {}) if isinstance(build_state, dict) else {}
    current = build_state.get("current", {}) if isinstance(build_state, dict) else {}
    if not isinstance(plugins, dict):
        plugins = {}
    if not isinstance(state_plugins, dict):
        state_plugins = {}
    if not isinstance(current, dict):
        current = {}

    reasons: list[str] = []
    problematic_plugins: list[str] = []
    degraded_plugins: list[str] = []

    current_plugin = str(current.get("plugin_name", "") or "").strip()
    if current_plugin:
        reasons.append("stale_current_progress")

    if not plugins:
        reasons.append("empty_index")

    for plugin_name, meta in state_plugins.items():
        if not isinstance(meta, dict):
            continue
        status = str(meta.get("status", "") or "").strip().lower()
        if status in {"pending", "failed"}:
            problematic_plugins.append(str(plugin_name))
        elif status == "degraded":
            degraded_plugins.append(str(plugin_name))
    if problematic_plugins:
        reasons.append("problematic_build_state")

    loaded_plugins = _iter_target_loaded_plugin_names()
    indexed_plugins = {str(name).strip() for name in plugins.keys() if str(name).strip()}
    missing_plugins = sorted(loaded_plugins - indexed_plugins)
    if missing_plugins:
        reasons.append("loaded_plugin_mismatch")

    return {
        "needs_build": bool(reasons),
        "reasons": reasons,
        "missing_plugins": missing_plugins,
        "problematic_plugins": problematic_plugins,
        "degraded_plugins": degraded_plugins,
        "indexed_plugin_count": len(indexed_plugins),
        "loaded_plugin_count": len(loaded_plugins),
        "current_plugin": current_plugin,
    }


async def stop_plugin_knowledge_builder(
    *,
    logger: Any,
    knowledge_store: PluginKnowledgeStore | None,
    get_knowledge_build_task: Any,
    set_knowledge_build_task: Any,
    enabled: bool,
    trigger: str,
    result: str,
    reasons: list[str] | None = None,
) -> bool:
    current_task = get_knowledge_build_task() if callable(get_knowledge_build_task) else None
    was_running = bool(current_task is not None and not current_task.done())
    if was_running:
        current_task.cancel()
        try:
            await current_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"[plugin_knowledge] cancel build task failed: {exc}")
    if callable(set_knowledge_build_task):
        set_knowledge_build_task(None)
    if knowledge_store is not None:
        await _save_build_control_state(
            knowledge_store,
            enabled=enabled,
            trigger=trigger,
            result=result,
            action="cancel" if was_running else "idle",
            reasons=reasons,
            clear_current=True,
        )
    return was_running


async def maybe_start_plugin_knowledge_builder(
    *,
    plugin_config: Any,
    tool_caller: ToolCaller | None,
    knowledge_store: PluginKnowledgeStore | None,
    logger: Any,
    get_knowledge_build_task: Any,
    set_knowledge_build_task: Any,
    trigger: str,
    force: bool = False,
) -> dict[str, Any]:
    enabled = bool(getattr(plugin_config, "personification_plugin_knowledge_build_enabled", False))
    if not enabled:
        if knowledge_store is not None:
            await _save_build_control_state(
                knowledge_store,
                enabled=False,
                trigger=trigger,
                result="disabled_skip",
                action="skip",
                reasons=[],
                clear_current=False,
            )
        return {"started": False, "result": "disabled_skip", "reasons": []}

    if knowledge_store is None:
        logger.warning("[plugin_knowledge] knowledge_store 不可用，跳过构建检查")
        return {"started": False, "result": "store_unavailable", "reasons": ["store_unavailable"]}
    if tool_caller is None:
        logger.warning("[plugin_knowledge] tool_caller 不可用，跳过构建检查")
        await _save_build_control_state(
            knowledge_store,
            enabled=True,
            trigger=trigger,
            result="tool_caller_unavailable",
            action="skip",
            reasons=["tool_caller_unavailable"],
            clear_current=False,
        )
        return {
            "started": False,
            "result": "tool_caller_unavailable",
            "reasons": ["tool_caller_unavailable"],
        }

    current_task = get_knowledge_build_task() if callable(get_knowledge_build_task) else None
    if current_task is not None and not current_task.done():
        await _save_build_control_state(
            knowledge_store,
            enabled=True,
            trigger=trigger,
            result="already_running",
            action="skip",
            reasons=["already_running"],
            clear_current=False,
        )
        return {"started": False, "result": "already_running", "reasons": ["already_running"]}

    reasons: list[str]
    if force:
        reasons = ["force_start"]
    else:
        inspection = await inspect_plugin_knowledge_health(knowledge_store)
        reasons = list(inspection.get("reasons") or [])
        if not inspection.get("needs_build"):
            await _save_build_control_state(
                knowledge_store,
                enabled=True,
                trigger=trigger,
                result="healthy_skip",
                action="skip",
                reasons=[],
                clear_current=False,
            )
            return {"started": False, "result": "healthy_skip", "reasons": []}

    task = start_knowledge_builder(
        plugin_config=plugin_config,
        tool_caller=tool_caller,
        knowledge_store=knowledge_store,
        logger=logger,
    )
    if callable(set_knowledge_build_task):
        set_knowledge_build_task(task)
    await _save_build_control_state(
        knowledge_store,
        enabled=True,
        trigger=trigger,
        result="started",
        action="start",
        reasons=reasons,
        clear_current=False,
    )
    return {"started": True, "result": "started", "reasons": reasons}


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
        build_state.setdefault("current", {})
        data_dir = get_personification_data_dir(plugin_config)

        for plugin in plugins:
            plugin_name = ""
            source_hash = ""
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

                source_snapshot = extract_plugin_source_snapshot(plugin_root)
                if not source_snapshot:
                    logger.warning(f"[knowledge_builder] 插件源码提取为空: {plugin_name}")
                    continue

                source_hash = compute_source_hash(source_snapshot)
                analysis_strategy = str(source_snapshot.get("analysis_strategy", "") or "chunk_batches").strip()
                module_bundle_count = int(source_snapshot.get("module_bundle_count", 0) or 0)
                previous = state_plugins.get(plugin_name, {}) if isinstance(state_plugins, dict) else {}
                retry_count = int(previous.get("retry_count", 0) or 0) if isinstance(previous, dict) else 0
                if (
                    isinstance(previous, dict)
                    and previous.get("hash") == source_hash
                    and previous.get("status") == "success"
                ):
                    continue
                if isinstance(previous, dict) and previous.get("status") == "failed" and retry_count >= 3:
                    continue

                source_file_count = len(list(source_snapshot.get("files") or []))
                source_chunk_count = len(list(source_snapshot.get("chunks") or []))
                state_plugins[plugin_name] = {
                    **previous,
                    "hash": source_hash,
                    "status": "pending",
                    "phase": "snapshot",
                    "error": "",
                    "error_type": "",
                    "error_message": "",
                    "raw_preview": "",
                    "retry_count": retry_count,
                    "updated_at": _now_iso(),
                    "last_success_at": str(previous.get("last_success_at", "") or ""),
                    "root_path": str(plugin_root),
                    "source_file_count": source_file_count,
                    "source_chunk_count": source_chunk_count,
                    "analysis_strategy": analysis_strategy,
                    "module_bundle_count": module_bundle_count,
                    "failed_batch_index": 0,
                    "failed_batch_total": 0,
                    "recent_errors": list(previous.get("recent_errors") or []) if isinstance(previous, dict) else [],
                }
                build_state["current"] = {
                    "plugin_name": plugin_name,
                    "phase": "snapshot",
                    "updated_at": _now_iso(),
                }
                await knowledge_store.save_build_state(build_state)

                category = "store" if "site-packages" in str(plugin_root).lower() else "local"
                await knowledge_store.save_source_snapshot(plugin_name, source_snapshot)
                pending_meta = state_plugins.get(plugin_name, {})
                if isinstance(pending_meta, dict):
                    pending_meta["phase"] = "full_source_analysis" if analysis_strategy == "full_source" else "module_analysis"
                    pending_meta["updated_at"] = _now_iso()
                    state_plugins[plugin_name] = pending_meta
                build_state["current"] = {
                    "plugin_name": plugin_name,
                    "phase": "full_source_analysis" if analysis_strategy == "full_source" else "module_analysis",
                    "updated_at": _now_iso(),
                }
                await knowledge_store.save_build_state(build_state)
                analyzed = await analyze_plugin_with_llm(
                    source_snapshot=source_snapshot,
                    plugin_name=plugin_name,
                    tool_caller=tool_caller,
                )
                analysis_meta = analyzed.pop("_analysis_meta", {}) if isinstance(analyzed, dict) else {}
                analyzed["plugin_name"] = plugin_name
                analyzed["module_name"] = module_name
                analyzed["root_path"] = str(plugin_root)
                analyzed["source_hash"] = source_hash
                analyzed["source_file_count"] = source_file_count
                analyzed["source_chunk_count"] = source_chunk_count
                analyzed["updated_at"] = _now_iso()

                runtime_snapshot = scan_runtime_data(plugin_name, data_dir)
                build_state["current"] = {
                    "plugin_name": plugin_name,
                    "phase": "save_entry",
                    "updated_at": _now_iso(),
                }
                await knowledge_store.save_plugin_entry(plugin_name, category, analyzed)
                if runtime_snapshot:
                    await knowledge_store.save_runtime_snapshot(plugin_name, runtime_snapshot)

                status = str(analysis_meta.get("status", "") or "success").strip().lower()
                if status not in {"success", "degraded"}:
                    status = "success"
                error_message = str(analysis_meta.get("error_message", "") or "").strip()
                state_plugins[plugin_name] = {
                    "hash": source_hash,
                    "status": status,
                    "phase": "complete" if status == "success" else str(analysis_meta.get("phase", "") or "synthesis"),
                    "error": error_message,
                    "error_type": str(analysis_meta.get("error_type", "") or "").strip(),
                    "error_message": error_message,
                    "raw_preview": str(analysis_meta.get("raw_preview", "") or "").strip()[:600],
                    "retry_count": 0,
                    "updated_at": _now_iso(),
                    "last_success_at": _now_iso(),
                    "category": category,
                    "root_path": str(plugin_root),
                    "source_file_count": source_file_count,
                    "source_chunk_count": source_chunk_count,
                    "analysis_strategy": str(analysis_meta.get("analysis_mode", "") or analysis_strategy),
                    "module_bundle_count": int(analysis_meta.get("module_bundle_count", 0) or module_bundle_count),
                    "failed_batch_index": int(analysis_meta.get("failed_batch_index", 0) or 0),
                    "failed_batch_total": int(analysis_meta.get("failed_batch_total", 0) or 0),
                    "recent_errors": list(analysis_meta.get("recent_errors") or []),
                }
                build_state["current"] = {}
                await knowledge_store.save_build_state(build_state)
                await asyncio.sleep(2.0)
            except Exception as exc:
                previous = state_plugins.get(plugin_name, {}) if isinstance(state_plugins, dict) else {}
                failure = _extract_failure_details(exc)
                updated_at = _now_iso()
                state_plugins[plugin_name] = {
                    **(previous if isinstance(previous, dict) else {}),
                    "hash": source_hash,
                    "status": "failed",
                    "phase": failure["phase"],
                    "error": failure["error_message"],
                    "error_type": failure["error_type"],
                    "error_message": failure["error_message"],
                    "raw_preview": failure["raw_preview"],
                    "retry_count": int((previous or {}).get("retry_count", 0) or 0) + 1 if isinstance(previous, dict) else 1,
                    "updated_at": updated_at,
                    "last_success_at": str((previous or {}).get("last_success_at", "") or ""),
                    "analysis_strategy": str((previous or {}).get("analysis_strategy", "") or analysis_strategy) if isinstance(previous, dict) else analysis_strategy,
                    "module_bundle_count": int((previous or {}).get("module_bundle_count", 0) or module_bundle_count) if isinstance(previous, dict) else module_bundle_count,
                    "failed_batch_index": failure["failed_batch_index"],
                    "failed_batch_total": failure["failed_batch_total"],
                    "recent_errors": _append_recent_error(
                        previous if isinstance(previous, dict) else {},
                        {**failure, "updated_at": updated_at},
                    ),
                }
                build_state["current"] = {}
                await knowledge_store.save_build_state(build_state)
                logger.warning(f"[knowledge_builder] 插件处理失败: {getattr(plugin, 'name', '?')} error={exc}")

        build_state["current"] = {}
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


__all__ = [
    "build_plugin_knowledge_async",
    "inspect_plugin_knowledge_health",
    "maybe_start_plugin_knowledge_builder",
    "start_knowledge_builder",
    "stop_plugin_knowledge_builder",
]
