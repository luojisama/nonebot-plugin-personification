from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


ToolHandler = Callable[..., Awaitable[str]]
EnabledChecker = Callable[[], bool]


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    local: bool = True
    enabled: EnabledChecker = field(default=lambda: True)
    metadata: dict[str, Any] = field(default_factory=dict)
    per_session_quota: int = 0  # 0 表示无上限；M6 用


def _override_is_disabled(name: str) -> bool:
    """延迟 import 以避免循环依赖（agent → core 早期阶段未就绪时容错）。"""
    try:
        from ..core.skill_overrides import is_disabled
    except Exception:
        return False
    try:
        return bool(is_disabled(name))
    except Exception:
        return False


def _health_is_disabled(name: str) -> bool:
    """临时健康屏蔽：联网工具探测失败时不暴露给模型。"""
    try:
        from ..core.tool_health import is_tool_temporarily_disabled
    except Exception:
        return False
    try:
        return bool(is_tool_temporarily_disabled(name))
    except Exception:
        return False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[AgentTool]:
        return self._tools.get(name)

    def all(self) -> List[AgentTool]:
        return list(self._tools.values())

    def active(self) -> List[AgentTool]:
        active_tools: List[AgentTool] = []
        for tool in self._tools.values():
            if _override_is_disabled(tool.name):
                continue
            if _health_is_disabled(tool.name):
                continue
            try:
                if tool.enabled():
                    active_tools.append(tool)
            except Exception:
                continue
        return active_tools

    def openai_schemas(self) -> List[dict]:
        schemas: List[dict] = []
        for tool in self.active():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": _normalize_parameters(tool.parameters),
                    },
                }
            )
        return schemas


def _normalize_parameters(parameters: Any) -> dict:
    if isinstance(parameters, dict) and parameters:
        return parameters
    return {
        "type": "object",
        "properties": {},
        "required": [],
    }
