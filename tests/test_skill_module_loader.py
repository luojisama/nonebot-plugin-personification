from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ._loader import load_personification_module


module_loader = load_personification_module("plugin.personification.skill_runtime.module_loader")


def _write_skill(skill_dir: Path, *, value: str, fail: bool = False) -> Path:
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "impl.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
    main_path = scripts_dir / "main.py"
    source = "from . import impl\nVALUE = impl.VALUE\n"
    if fail:
        source += "raise RuntimeError('load failed')\n"
    main_path.write_text(source, encoding="utf-8")
    return main_path


def test_production_skillpack_entries_load_with_shared_dependency() -> None:
    skillpacks_root = Path(__file__).resolve().parents[1] / "skills" / "skillpacks"

    for skill_name in ("resource_collector", "web_search"):
        skill_dir = skillpacks_root / skill_name
        module = module_loader.load_skill_module(
            skill_dir=skill_dir,
            script_path=skill_dir / "scripts" / "main.py",
            extra_sys_paths=[skillpacks_root],
            module_prefix="personification_skillpack_test",
        )
        assert callable(module.build_tools)


def test_dependency_change_loads_new_version_and_removes_old_tree(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo"
    main_path = _write_skill(skill_dir, value="first")

    first = module_loader.load_skill_module(
        skill_dir=skill_dir,
        script_path=main_path,
        module_prefix="test_skill_reload",
    )
    old_root = first.__name__.split(".", 1)[0]
    _write_skill(skill_dir, value="other")
    second = module_loader.load_skill_module(
        skill_dir=skill_dir,
        script_path=main_path,
        module_prefix="test_skill_reload",
    )

    assert first.VALUE == "first"
    assert second.VALUE == "other"
    assert second.__name__ != first.__name__
    assert not any(name == old_root or name.startswith(f"{old_root}.") for name in sys.modules)


def test_failed_load_removes_partial_module_tree(tmp_path: Path) -> None:
    skill_dir = tmp_path / "broken"
    main_path = _write_skill(skill_dir, value="unused", fail=True)

    with pytest.raises(RuntimeError, match="load failed"):
        module_loader.load_skill_module(
            skill_dir=skill_dir,
            script_path=main_path,
            module_prefix="test_skill_failure",
        )

    assert not any(name.startswith("test_skill_failure_") for name in sys.modules)


def test_concurrent_load_is_singleton_and_restores_sys_path(tmp_path: Path) -> None:
    skill_dir = tmp_path / "concurrent"
    main_path = _write_skill(skill_dir, value="ready")
    original_sys_path = list(sys.path)

    def _load():
        return module_loader.load_skill_module(
            skill_dir=skill_dir,
            script_path=main_path,
            module_prefix="test_skill_concurrent",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        modules = list(executor.map(lambda _: _load(), range(16)))

    assert all(module is modules[0] for module in modules)
    assert sys.path == original_sys_path
