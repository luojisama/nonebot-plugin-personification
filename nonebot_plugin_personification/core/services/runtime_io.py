from pathlib import Path
from typing import Any, Callable, List, Tuple

from ..config_manager import ConfigManager
from ..runtime_config import (
    get_runtime_config_path,
    load_plugin_runtime_config as load_plugin_runtime_config_core,
    save_plugin_runtime_config as save_plugin_runtime_config_core,
)
from ..sticker_cache import get_sticker_files as get_sticker_files_core
from ..sticker_library import resolve_sticker_dir


def build_runtime_config_io(
    *,
    plugin_config: Any,
    logger: Any,
) -> Tuple[Callable[[], None], Callable[[], None]]:
    runtime_path = get_runtime_config_path(plugin_config)
    config_manager = ConfigManager(plugin_config=plugin_config, logger=logger)

    def _save_plugin_runtime_config() -> None:
        config_manager.save()
        save_plugin_runtime_config_core(plugin_config, logger, path=runtime_path)

    def _load_plugin_runtime_config() -> None:
        config_manager.load()
        load_plugin_runtime_config_core(plugin_config, logger, path=runtime_path)

    return _save_plugin_runtime_config, _load_plugin_runtime_config


def build_sticker_cache(
    *,
    sticker_path: str | Path | None,
    ttl_seconds: int = 300,
) -> Callable[[], List[Path]]:
    resolved_dir = resolve_sticker_dir(sticker_path)

    def _get_sticker_files() -> List[Path]:
        return get_sticker_files_core(resolved_dir, ttl_seconds=ttl_seconds)

    return _get_sticker_files
