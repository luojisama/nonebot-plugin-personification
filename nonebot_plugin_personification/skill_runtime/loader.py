from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import yaml

from ..agent.tool_registry import AgentTool, ToolRegistry
from .compat_adapters import build_compat_tools
from .module_loader import load_skill_module
from .runtime_api import SkillRuntime


def _extract_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    match = re.match(r"^---\n([\s\S]*?)\n---\n?([\s\S]*)$", normalized)
    if not match:
        return {}, normalized
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except Exception:
        data = {}
    return (data if isinstance(data, dict) else {}), match.group(2)


def _resolve_entrypoint(skill_dir: Path, frontmatter: dict[str, Any]) -> Path | None:
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.exists():
        return None
    entry = str(frontmatter.get("entrypoint") or frontmatter.get("script") or "").strip()
    if entry:
        candidate = skill_dir / entry
        if candidate.exists():
            return candidate
        candidate = scripts_dir / entry
        if candidate.exists():
            return candidate
    for name in ("main.py", "run.py", "skill.py"):
        candidate = scripts_dir / name
        if candidate.exists():
            return candidate
    py_files = sorted(scripts_dir.glob("*.py"))
    return py_files[0] if py_files else None


def _normalize_parameters(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"type": "object", "properties": {}, "required": []}


def _load_module(skill_dir: Path, script_path: Path, extra_sys_paths: list[Path] | None = None):
    return load_skill_module(
        skill_dir=skill_dir,
        script_path=script_path,
        extra_sys_paths=extra_sys_paths,
        module_prefix="personification_skillpack",
    )


def _load_skill_metadata(skill_dir: Path) -> tuple[dict[str, Any], str] | None:
    yaml_path = skill_dir / "skill.yaml"
    if yaml_path.exists():
        try:
            parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            return parsed, ""
    md_path = skill_dir / "SKILL.md"
    if md_path.exists():
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            return None
        frontmatter, markdown_body = _extract_frontmatter(text)
        return frontmatter, markdown_body
    return None


def _validate_run_signature(
    *,
    skill_name: str,
    frontmatter: dict[str, Any],
    run_handler: Any,
    logger: Any,
) -> None:
    parameters = _normalize_parameters(frontmatter.get("parameters"))
    required_list = parameters.get("required", [])
    required = {str(item) for item in required_list if str(item).strip()}
    if not required:
        return
    try:
        sig = inspect.signature(run_handler)
    except Exception:
        logger.warning(f"[skillpack] cannot inspect run signature: {skill_name}")
        return
    sig_required: set[str] = set()
    for name, param in sig.parameters.items():
        if param.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            continue
        if param.default is inspect.Parameter.empty:
            sig_required.add(str(name))
    missing_in_sig = sorted(required - sig_required)
    missing_in_meta = sorted(sig_required - required)
    if missing_in_sig or missing_in_meta:
        logger.warning(
            f"[skillpack] metadata/signature mismatch {skill_name}: "
            f"required_in_meta_only={missing_in_sig} required_in_run_only={missing_in_meta}"
        )


async def load_builtin_skillpacks(
    *,
    runtime: SkillRuntime,
    registry: ToolRegistry,
) -> int:
    loaded = 0
    skillpacks_root = Path(__file__).resolve().parent.parent / "skills" / "skillpacks"
    if not skillpacks_root.exists():
        return loaded

    for skill_dir in sorted(path for path in skillpacks_root.iterdir() if path.is_dir()):
        metadata = _load_skill_metadata(skill_dir)
        if metadata is None:
            continue
        frontmatter, markdown_body = metadata
        script_path = _resolve_entrypoint(skill_dir, frontmatter)
        if script_path is None:
            compat_tools = build_compat_tools(
                skill_dir=skill_dir,
                frontmatter=frontmatter,
                runtime=runtime,
            )
            if compat_tools:
                for tool in compat_tools:
                    if isinstance(tool, AgentTool):
                        registry.register(tool)
                loaded += 1
                runtime.logger.info(
                    f"[skillpack] registered compatibility adapter tools for {skill_dir.name}: "
                    f"{', '.join(tool.name for tool in compat_tools)}"
                )
                continue
            runtime.logger.warning(f"[skillpack] missing scripts entrypoint: {skill_dir.name}")
            continue

        extra_sys_paths = [skillpacks_root]
        python_paths = frontmatter.get("python_paths")
        if isinstance(python_paths, list):
            for rel in python_paths:
                candidate = skill_dir / str(rel)
                if candidate.exists():
                    extra_sys_paths.append(candidate)
        try:
            module = _load_module(skill_dir, script_path, extra_sys_paths=extra_sys_paths)
        except Exception as e:
            runtime.logger.warning(f"[skillpack] load failed {skill_dir.name}: {e}")
            continue

        if hasattr(module, "register") and callable(getattr(module, "register")):
            try:
                result = module.register(runtime, registry)
                if inspect.isawaitable(result):
                    await result
                loaded += 1
                continue
            except Exception as e:
                runtime.logger.warning(f"[skillpack] register failed {skill_dir.name}: {e}")
                continue

        if hasattr(module, "build_tools") and callable(getattr(module, "build_tools")):
            try:
                result = module.build_tools(runtime)
                if inspect.isawaitable(result):
                    result = await result
                tools = result if isinstance(result, list) else []
                count = 0
                for tool in tools:
                    if isinstance(tool, AgentTool):
                        registry.register(tool)
                        count += 1
                if count > 0:
                    loaded += 1
                continue
            except Exception as e:
                runtime.logger.warning(f"[skillpack] build_tools failed {skill_dir.name}: {e}")
                continue

        run_handler = getattr(module, "run", None)
        if not callable(run_handler):
            runtime.logger.warning(f"[skillpack] missing run/build_tools/register: {skill_dir.name}")
            continue
        _validate_run_signature(
            skill_name=skill_dir.name,
            frontmatter=frontmatter,
            run_handler=run_handler,
            logger=runtime.logger,
        )

        description = str(frontmatter.get("description") or "").strip()
        if not description:
            lines = [line.strip() for line in markdown_body.splitlines() if line.strip()]
            description = lines[0][:120] if lines else ""

        async def _handler(_run_ref=run_handler, **kwargs) -> str:
            result = _run_ref(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return str(result)

        registry.register(
            AgentTool(
                name=str(frontmatter.get("name") or skill_dir.name),
                description=description,
                parameters=_normalize_parameters(frontmatter.get("parameters")),
                handler=_handler,
                local=bool(frontmatter.get("local", True)),
                enabled=lambda fm=frontmatter: bool(fm.get("enabled", True)),
            )
        )
        loaded += 1

    runtime.logger.info(f"[skillpack] loaded builtins={loaded}")
    return loaded


def load_builtin_skillpacks_sync(
    *,
    runtime: SkillRuntime,
    registry: ToolRegistry,
) -> int:
    loaded = 0
    skillpacks_root = Path(__file__).resolve().parent.parent / "skills" / "skillpacks"
    if not skillpacks_root.exists():
        return loaded

    for skill_dir in sorted(path for path in skillpacks_root.iterdir() if path.is_dir()):
        metadata = _load_skill_metadata(skill_dir)
        if metadata is None:
            continue
        frontmatter, markdown_body = metadata
        script_path = _resolve_entrypoint(skill_dir, frontmatter)
        if script_path is None:
            compat_tools = build_compat_tools(
                skill_dir=skill_dir,
                frontmatter=frontmatter,
                runtime=runtime,
            )
            if compat_tools:
                for tool in compat_tools:
                    if isinstance(tool, AgentTool):
                        registry.register(tool)
                loaded += 1
                runtime.logger.info(
                    f"[skillpack] registered compatibility adapter tools for {skill_dir.name}: "
                    f"{', '.join(tool.name for tool in compat_tools)}"
                )
                continue
            runtime.logger.warning(f"[skillpack] missing scripts entrypoint: {skill_dir.name}")
            continue

        extra_sys_paths = [skillpacks_root]
        python_paths = frontmatter.get("python_paths")
        if isinstance(python_paths, list):
            for rel in python_paths:
                candidate = skill_dir / str(rel)
                if candidate.exists():
                    extra_sys_paths.append(candidate)
        try:
            module = _load_module(skill_dir, script_path, extra_sys_paths=extra_sys_paths)
        except Exception as e:
            runtime.logger.warning(f"[skillpack] load failed {skill_dir.name}: {e}")
            continue

        if hasattr(module, "register") and callable(getattr(module, "register")):
            try:
                result = module.register(runtime, registry)
                if inspect.isawaitable(result):
                    runtime.logger.warning(f"[skillpack] register is awaitable in sync mode: {skill_dir.name}")
                    continue
                loaded += 1
                continue
            except Exception as e:
                runtime.logger.warning(f"[skillpack] register failed {skill_dir.name}: {e}")
                continue

        if hasattr(module, "build_tools") and callable(getattr(module, "build_tools")):
            try:
                result = module.build_tools(runtime)
                if inspect.isawaitable(result):
                    runtime.logger.warning(f"[skillpack] build_tools is awaitable in sync mode: {skill_dir.name}")
                    continue
                tools = result if isinstance(result, list) else []
                count = 0
                for tool in tools:
                    if isinstance(tool, AgentTool):
                        registry.register(tool)
                        count += 1
                if count > 0:
                    loaded += 1
                continue
            except Exception as e:
                runtime.logger.warning(f"[skillpack] build_tools failed {skill_dir.name}: {e}")
                continue

        run_handler = getattr(module, "run", None)
        if not callable(run_handler):
            runtime.logger.warning(f"[skillpack] missing run/build_tools/register: {skill_dir.name}")
            continue
        _validate_run_signature(
            skill_name=skill_dir.name,
            frontmatter=frontmatter,
            run_handler=run_handler,
            logger=runtime.logger,
        )

        description = str(frontmatter.get("description") or "").strip()
        if not description:
            lines = [line.strip() for line in markdown_body.splitlines() if line.strip()]
            description = lines[0][:120] if lines else ""

        async def _handler(_run_ref=run_handler, **kwargs) -> str:
            result = _run_ref(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return str(result)

        registry.register(
            AgentTool(
                name=str(frontmatter.get("name") or skill_dir.name),
                description=description,
                parameters=_normalize_parameters(frontmatter.get("parameters")),
                handler=_handler,
                local=bool(frontmatter.get("local", True)),
                enabled=lambda fm=frontmatter: bool(fm.get("enabled", True)),
            )
        )
        loaded += 1

    runtime.logger.info(f"[skillpack] loaded builtins(sync)={loaded}")
    return loaded
