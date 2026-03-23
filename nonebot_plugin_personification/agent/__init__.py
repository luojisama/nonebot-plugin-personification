"""Agent runtime primitives for the personification plugin."""

from .inner_state import (
    DEFAULT_STATE,
    get_personification_data_dir,
    load_inner_state,
    save_inner_state,
    update_state_from_diary,
    update_inner_state_after_chat,
)
from .loop import AgentResult, run_agent
from .tool_registry import AgentTool, ToolRegistry

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
