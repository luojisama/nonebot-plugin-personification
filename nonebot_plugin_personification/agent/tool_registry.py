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


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[AgentTool]:
        return self._tools.get(name)

    def active(self) -> List[AgentTool]:
        active_tools: List[AgentTool] = []
        for tool in self._tools.values():
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
