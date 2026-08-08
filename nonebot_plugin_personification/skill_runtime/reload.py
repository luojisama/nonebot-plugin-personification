from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from ..agent.tool_registry import ToolRegistry
from ..agent.runtime.tool_catalog import apply_tool_metadata_defaults
from ..core.file_sender import build_file_sender
from ..schedule import get_current_local_time
from .custom_loader import load_custom_skills
from .runtime_api import SkillRuntime


_RELOAD_LOCKS: dict[int, asyncio.Lock] = {}
_CUSTOM_SOURCE_KINDS = {"generated", "local", "mcp", "remote"}


def _reload_lock(registry: ToolRegistry) -> asyncio.Lock:
    key = id(registry)
    lock = _RELOAD_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _RELOAD_LOCKS[key] = lock
    return lock


async def reload_custom_skills_for_runtime(runtime: Any) -> int:
    bundle = getattr(runtime, "runtime_bundle", None)
    registry = getattr(bundle, "tool_registry", None) if bundle is not None else None
    if registry is None:
        return 0
    version, snapshot = registry.snapshot()
    candidate = ToolRegistry()
    for tool in snapshot.values():
        source_kind = str((tool.metadata or {}).get("source_kind") or "").strip().lower()
        if source_kind in _CUSTOM_SOURCE_KINDS:
            continue
        candidate.register(tool)
    plugin_config = runtime.plugin_config
    logger = runtime.logger
    deps = getattr(bundle, "reply_processor_deps", None)
    inner = getattr(deps, "runtime", None) if deps is not None else None
    tool_caller = getattr(inner, "agent_tool_caller", None)
    skills_path = str(getattr(plugin_config, "personification_skills_path", "") or "").strip()
    custom_root = Path(skills_path) if skills_path and Path(skills_path).is_dir() else None
    skill_runtime = SkillRuntime(
        plugin_config=plugin_config,
        logger=logger,
        get_now=get_current_local_time,
        scheduler=getattr(bundle, "scheduler", None),
        data_dir=getattr(plugin_config, "personification_data_dir", None),
        persona_store=getattr(bundle, "persona_store", None),
        vision_caller=getattr(inner, "vision_caller", None),
        file_sender=build_file_sender(get_bots=runtime.get_bots, logger=logger),
        get_bots=runtime.get_bots,
        get_whitelisted_groups=getattr(bundle, "_get_whitelisted_groups", None),
        tool_caller=tool_caller,
        knowledge_store=getattr(inner, "knowledge_store", None),
        memory_store=getattr(bundle, "memory_store", None),
        profile_service=getattr(bundle, "profile_service", None),
        memory_curator=getattr(bundle, "memory_curator", None),
        background_intelligence=getattr(bundle, "background_intelligence", None),
    )
    before = len(candidate.all())
    await load_custom_skills(
        custom_root,
        candidate,
        logger,
        tool_caller=tool_caller,
        plugin_config=plugin_config,
        runtime=skill_runtime,
    )
    apply_tool_metadata_defaults(candidate)
    registry.replace_all(candidate.all(), expected_version=version)
    return max(0, len(candidate.all()) - before)


async def reload_all_runtime_services(runtime: Any) -> dict[str, Any]:
    bundle = getattr(runtime, "runtime_bundle", None)
    registry = getattr(bundle, "tool_registry", None) if bundle is not None else None
    reload_base = getattr(bundle, "reload_runtime_services", None) if bundle is not None else None
    if registry is None or not callable(reload_base):
        raise RuntimeError("runtime reload is unavailable")
    async with _reload_lock(registry):
        result = reload_base()
        if inspect.isawaitable(result):
            await result
        custom_count = await reload_custom_skills_for_runtime(runtime)
        from ..core.mcp_management import get_mcp_manager

        mcp_result = await get_mcp_manager(runtime).reload()
        if int(mcp_result.get("failed") or 0) > 0:
            raise RuntimeError(f"managed MCP restore failed for {int(mcp_result['failed'])} installation(s)")
        return {"custom_tools": custom_count, "mcp": mcp_result}


__all__ = ["reload_all_runtime_services", "reload_custom_skills_for_runtime"]
