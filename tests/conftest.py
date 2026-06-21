from __future__ import annotations

import sys
import types
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_DIR = _REPO_ROOT / "nonebot_plugin_personification"


def _ensure_namespace_package(name: str, path: Path) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = module
        return module
    current_path = getattr(module, "__path__", None)
    if current_path is None:
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
    elif str(path) not in current_path:
        try:
            current_path.append(str(path))
        except Exception:
            module.__path__ = list(current_path) + [str(path)]  # type: ignore[attr-defined]
    return module


def pytest_configure() -> None:
    _ensure_namespace_package("plugin", _REPO_ROOT)
    _ensure_namespace_package("plugin.personification", _PACKAGE_DIR)
