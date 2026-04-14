from __future__ import annotations

from importlib import import_module
from pathlib import Path

from nonebot import require


def get_plugin_data_dir() -> Path:
    require("nonebot_plugin_localstore")
    localstore = import_module("nonebot_plugin_localstore")
    return Path(localstore.get_plugin_data_dir())
