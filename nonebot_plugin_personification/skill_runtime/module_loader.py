from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
import threading
from pathlib import Path
from types import ModuleType


_MODULE_LOAD_LOCK = threading.RLock()


def load_skill_module(
    *,
    skill_dir: Path,
    script_path: Path,
    extra_sys_paths: list[Path] | None = None,
    module_prefix: str = "personification_skill",
):
    rel_parts = script_path.relative_to(skill_dir).with_suffix("").parts
    resolved_skill_dir = skill_dir.resolve()
    path_digest = hashlib.sha256(str(resolved_skill_dir).casefold().encode("utf-8")).hexdigest()[:16]
    content_digest = _skill_content_digest(resolved_skill_dir)
    safe_skill_name = re.sub(r"\W+", "_", skill_dir.name).strip("_") or "skill"
    root_prefix = f"{module_prefix}_{safe_skill_name}_{path_digest}_"
    root_name = f"{root_prefix}{content_digest}"
    module_name = ".".join([root_name, *rel_parts])

    inject_paths: list[str] = []
    for path in [
        script_path.parent,
        skill_dir,
        *(extra_sys_paths or []),
    ]:
        candidate = str(path.resolve())
        if candidate not in inject_paths:
            inject_paths.append(candidate)

    with _MODULE_LOAD_LOCK:
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing

        original_sys_path = list(sys.path)
        try:
            sys.path = inject_paths + original_sys_path
            _clear_skill_bytecode(resolved_skill_dir)
            importlib.invalidate_caches()
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
        except BaseException:
            _remove_module_tree(root_name)
            raise
        finally:
            sys.path = original_sys_path

        for loaded_name in list(sys.modules):
            loaded_root = loaded_name.split(".", 1)[0]
            if loaded_root.startswith(root_prefix) and loaded_root != root_name:
                _remove_module_tree(loaded_root)
        return module


def _skill_content_digest(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(skill_dir.rglob("*.py"), key=lambda item: item.relative_to(skill_dir).as_posix()):
        relative_path = path.relative_to(skill_dir).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _remove_module_tree(root_name: str) -> None:
    for name in list(sys.modules):
        if name == root_name or name.startswith(f"{root_name}."):
            sys.modules.pop(name, None)


def _clear_skill_bytecode(skill_dir: Path) -> None:
    for cache_dir in skill_dir.rglob("__pycache__"):
        if cache_dir.is_symlink() or not cache_dir.is_dir():
            continue
        for cache_path in cache_dir.glob("*.pyc"):
            if cache_path.is_symlink():
                continue
            cache_path.unlink(missing_ok=True)


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
