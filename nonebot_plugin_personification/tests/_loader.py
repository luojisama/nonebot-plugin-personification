from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PERSONIFICATION_DIR = _TESTS_DIR.parent
_PLUGIN_DIR = _PERSONIFICATION_DIR.parent


def _ensure_namespace_package(name: str, path: Path) -> None:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = module
        return
    if not hasattr(module, "__path__"):
        module.__path__ = [str(path)]  # type: ignore[attr-defined]


def load_personification_module(module_name: str):
    if module_name in sys.modules:
        return sys.modules[module_name]

    parts = module_name.split(".")
    if parts[:2] != ["plugin", "personification"]:
        raise ValueError(f"unsupported module namespace: {module_name}")

    _ensure_namespace_package("plugin", _PLUGIN_DIR)
    _ensure_namespace_package("plugin.personification", _PERSONIFICATION_DIR)

    current_path = _PERSONIFICATION_DIR
    for index in range(2, len(parts) - 1):
        current_path = current_path / parts[index]
        _ensure_namespace_package(".".join(parts[: index + 1]), current_path)

    file_path = _PERSONIFICATION_DIR.joinpath(*parts[2:]).with_suffix(".py")
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
