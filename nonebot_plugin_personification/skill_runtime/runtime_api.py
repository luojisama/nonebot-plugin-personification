from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class SkillRuntime:
    plugin_config: Any
    logger: Any
    get_now: Callable[[], Any]
    scheduler: Any | None = None
    data_dir: Any | None = None
    persona_store: Any | None = None
    vision_caller: Any | None = None
    file_sender: Any | None = None
    get_bots: Callable[[], dict[str, Any]] | None = None
    get_whitelisted_groups: Callable[[], list[str]] | None = None
    tool_caller: Any | None = None
    knowledge_store: Any | None = None
    memory_store: Any | None = None
    profile_service: Any | None = None
    memory_curator: Any | None = None
    background_intelligence: Any | None = None
