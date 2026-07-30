from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_skill_module(
    *,
    skill_dir: Path,
    script_path: Path,
    extra_sys_paths: list[Path] | None = None,
    module_prefix: str = "personification_skill",
):
    rel_parts = script_path.relative_to(skill_dir).with_suffix("").parts
    version = str(int(script_path.stat().st_mtime_ns))
    root_name = f"{module_prefix}_{skill_dir.name}_{abs(hash(str(script_path.resolve())))}_{version}"
    module_name = ".".join([root_name, *rel_parts])

    original_sys_path = list(sys.path)
    inject_paths: list[str] = []
    for path in [
        script_path.parent,
        skill_dir,
        *(extra_sys_paths or []),
    ]:
        candidate = str(path.resolve())
        if candidate not in inject_paths:
            inject_paths.append(candidate)

    try:
        sys.path = inject_paths + original_sys_path
        _ensure_package(root_name, skill_dir)

        current_dir = skill_dir
        package_parts: list[str] = [root_name]
        for rel_part in rel_parts[:-1]:
            current_dir = current_dir / rel_part
            package_parts.append(rel_part)
            _ensure_package(".".join(package_parts), current_dir)

        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {script_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path = original_sys_path


def _ensure_package(name: str, directory: Path) -> None:
    existing = sys.modules.get(name)
    if existing is not None:
        path_list = getattr(existing, "__path__", None)
        candidate = str(directory.resolve())
        if isinstance(path_list, list) and candidate not in path_list:
            path_list.append(candidate)
        return

    module = ModuleType(name)
    module.__file__ = str((directory / "__init__.py").resolve())
    module.__path__ = [str(directory.resolve())]
    module.__package__ = name
    sys.modules[name] = module
