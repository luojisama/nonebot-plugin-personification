"""Agent runtime primitives for the personification plugin.

The package exports the same public names as before, but resolves them lazily
to avoid importing ``inner_state`` while submodules are still initializing.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "AgentResult",
    "AgentTool",
    "DEFAULT_STATE",
    "ToolRegistry",
    "get_personification_data_dir",
    "load_inner_state",
    "run_agent",
    "save_inner_state",
    "update_state_from_diary",
    "update_inner_state_after_chat",
]


def __getattr__(name: str) -> Any:
    if name in {
        "DEFAULT_STATE",
        "get_personification_data_dir",
        "load_inner_state",
        "save_inner_state",
        "update_state_from_diary",
        "update_inner_state_after_chat",
    }:
        from . import inner_state

        return getattr(inner_state, name)
    if name in {"AgentResult", "run_agent"}:
        from . import loop

        return getattr(loop, name)
    if name in {"AgentTool", "ToolRegistry"}:
        from . import tool_registry

        return getattr(tool_registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
