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
    get_configured_api_providers: Callable[[], list[dict[str, Any]]] | None = None
    tool_caller: Any | None = None
    knowledge_store: Any | None = None
    memory_store: Any | None = None
    profile_service: Any | None = None
    memory_curator: Any | None = None
    background_intelligence: Any | None = None

    def __post_init__(self) -> None:
        if self.get_configured_api_providers is not None:
            return

        def _read_providers() -> list[dict[str, Any]]:
            from ..core.provider_router import get_configured_api_providers

            return get_configured_api_providers(self.plugin_config, self.logger)

        self.get_configured_api_providers = _read_providers
