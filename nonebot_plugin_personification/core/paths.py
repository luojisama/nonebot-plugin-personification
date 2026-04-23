from __future__ import annotations

from pathlib import Path
from typing import Any


def get_data_dir(plugin_config: Any | None = None) -> Path:
    configured = ""
    if plugin_config is not None:
        configured = str(getattr(plugin_config, "personification_data_dir", "") or "").strip()
    if configured:
        return Path(configured)
    try:
        import nonebot_plugin_localstore as store

        return Path(store.get_plugin_data_dir())
    except Exception:
        return Path("data") / "personification"
