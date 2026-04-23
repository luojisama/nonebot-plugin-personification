from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any

import yaml

from ..agent.tool_registry import AgentTool, ToolRegistry
from ..core.remote_skill_review import filter_approved_remote_sources
from .compat_adapters import build_compat_tools
from .mcp_compat import normalize_mcp_config, register_mcp_tools
from .module_loader import load_skill_module
from .runtime_api import SkillRuntime
from .skill_isolation import (
    normalize_isolation_config,
    run_skill_in_subprocess,
    script_supports_function,
)
from .source_resolver import discover_skill_dirs, get_skill_cache_dir, resolve_skill_source_dirs


def _load_skill_yaml(path: Path) -> dict | None:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_handler_module(skill_dir: Path, handler_path: Path, extra_sys_paths: list[Path] | None = None):
    return load_skill_module(
        skill_dir=skill_dir,
        script_path=handler_path,
        extra_sys_paths=extra_sys_paths,
        module_prefix="personification_custom_skill",
    )


async def _run_custom_handler(handler, kwargs: dict, timeout: int = 10) -> str:
    try:
        result = handler(**kwargs)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=timeout)
        return str(result)
    except asyncio.TimeoutError:
        return f"custom skill timeout after {timeout} seconds"
    except Exception as e:
        return f"custom skill error: {e}"


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
    return data if isinstance(data, dict) else {}, match.group(2)


def _load_skill_md(skill_dir: Path) -> tuple[dict[str, Any], str] | None:
    md_path = skill_dir / "SKILL.md"
    if not md_path.exists():
        return None
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    return _extract_frontmatter(content)


def _load_openai_meta(skill_dir: Path) -> dict[str, Any]:
    meta_path = skill_dir / "agents" / "openai.yaml"
    if not meta_path.exists():
        return {}
    try:
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_script_path(skill_dir: Path, frontmatter: dict[str, Any]) -> Path | None:
    scripts_dir = skill_dir / "scripts"
    entry = str(frontmatter.get("entrypoint") or frontmatter.get("script") or "").strip()
    if entry:
        candidate = skill_dir / entry
        if candidate.exists():
            return candidate
        candidate = scripts_dir / entry
        return candidate if candidate.exists() else None
    if not scripts_dir.exists():
        return None
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


def _build_skill_config(
    skill_dir: Path,
    frontmatter: dict[str, Any],
    markdown_body: str,
    openai_meta: dict[str, Any],
    script_path: Path | None,
) -> dict[str, Any]:
    display_name = str(
        frontmatter.get("name")
        or openai_meta.get("display_name")
        or openai_meta.get("name")
        or skill_dir.name
    ).strip()
    description = str(
        frontmatter.get("description")
        or openai_meta.get("description")
        or openai_meta.get("summary")
        or ""
    ).strip()
    if not description:
        lines = [line.strip() for line in markdown_body.splitlines() if line.strip()]
        description = lines[0][:120] if lines else ""
    return {
        "name": display_name,
        "description": description,
        "parameters": _normalize_parameters(frontmatter.get("parameters")),
        "local": bool(frontmatter.get("local", True)),
        "enabled": bool(frontmatter.get("enabled", True)),
        "script_path": script_path,
    }


async def _register_skill(registry: ToolRegistry, logger: Any, skill_dir: Path, config: dict[str, Any]) -> None:
    script_path: Path | None = config.get("script_path")
    runtime: SkillRuntime | None = config.get("runtime")
    container_root = config.get("container_root")
    trusted = bool(config.get("trusted", True))
    plugin_config = config.get("plugin_config")
    isolation = config.get("isolation") or {}
    mcp = config.get("mcp")
    is_process_isolated = str(isolation.get("mode") or "inprocess").strip().lower() == "process"
    is_mcp_isolated = isinstance(mcp, dict)
    allow_unsafe_external = bool(
        getattr(plugin_config, "personification_skill_allow_unsafe_external", False)
    ) if plugin_config is not None else False

    if not trusted and not allow_unsafe_external and not is_process_isolated and not is_mcp_isolated:
        logger.warning(
            f"[custom skill] skip untrusted external skill without explicit opt-in: {skill_dir.name}"
        )
        return

    if is_mcp_isolated:
        loaded = await register_mcp_tools(
            registry=registry,
            logger=logger,
            skill_dir=skill_dir,
            base_name=str(config.get("name") or skill_dir.name),
            config=mcp,
        )
        if loaded <= 0:
            logger.warning(f"[custom skill] MCP tool registration failed: {skill_dir.name}")
        return

    if script_path is None:
        logger.warning(f"[custom skill] missing script path: {skill_dir.name}")
        return

    if str(isolation.get("mode") or "inprocess").lower() == "process":
        if not script_supports_function(script_path, "run"):
            logger.warning(
                f"[custom skill] isolated mode requires run(...): {script_path}"
            )
            return

        async def _handler(**kwargs) -> str:
            return await run_skill_in_subprocess(
                script_path=script_path,
                function_name="run",
                kwargs=kwargs,
                skill_dir=skill_dir,
                container_root=container_root if isinstance(container_root, Path) else None,
                python_paths=list(config.get("python_paths") or []),
                isolation=isolation,
            )

        registry.register(
            AgentTool(
                name=str(config.get("name") or skill_dir.name),
                description=str(config.get("description") or ""),
                parameters=config.get("parameters") or {"type": "object", "properties": {}, "required": []},
                handler=_handler,
                local=bool(config.get("local", True)),
                enabled=lambda cfg=config: bool(cfg.get("enabled", True)),
            )
        )
        return

    extra_sys_paths = []
    if isinstance(container_root, Path):
        extra_sys_paths.append(container_root)
    for rel in config.get("python_paths") or []:
        candidate = skill_dir / str(rel)
        if candidate.exists():
            extra_sys_paths.append(candidate)
    try:
        module = _load_handler_module(skill_dir, script_path, extra_sys_paths=extra_sys_paths)
    except Exception as e:
        logger.warning(f"[custom skill] load failed for {skill_dir.name}: {e}")
        return

    register_handler = getattr(module, "register", None)
    if callable(register_handler) and runtime is not None:
        try:
            result = register_handler(runtime, registry)
            if inspect.isawaitable(result):
                await result
            return
        except Exception as e:
            logger.warning(f"[custom skill] register failed {skill_dir.name}: {e}")
            return

    build_tools_handler = getattr(module, "build_tools", None)
    if callable(build_tools_handler) and runtime is not None:
        try:
            result = build_tools_handler(runtime)
            if inspect.isawaitable(result):
                result = await result
            tools = result if isinstance(result, list) else []
            for tool in tools:
                if isinstance(tool, AgentTool):
                    registry.register(tool)
            if tools:
                return
        except Exception as e:
            logger.warning(f"[custom skill] build_tools failed {skill_dir.name}: {e}")
            return

    handler = getattr(module, "run", None)
    if not callable(handler):
        logger.warning(f"[custom skill] run/register/build_tools missing: {script_path}")
        return

    async def _handler(_handler_ref=handler, **kwargs) -> str:
        timeout = int(isolation.get("timeout", 10) or 10)
        return await _run_custom_handler(_handler_ref, kwargs, timeout=timeout)

    registry.register(
        AgentTool(
            name=str(config.get("name") or skill_dir.name),
            description=str(config.get("description") or ""),
            parameters=config.get("parameters") or {"type": "object", "properties": {}, "required": []},
            handler=_handler,
            local=bool(config.get("local", True)),
            enabled=lambda cfg=config: bool(cfg.get("enabled", True)),
        )
    )


def _load_standard_skill_metadata(skill_dir: Path) -> tuple[dict[str, Any], str] | None:
    skill_yaml = skill_dir / "skill.yaml"
    if skill_yaml.exists():
        loaded = _load_skill_yaml(skill_yaml)
        if loaded is not None:
            return loaded, ""
    return _load_skill_md(skill_dir)


async def _load_standard_skill_dir(
    *,
    skill_dir: Path,
    registry: ToolRegistry,
    logger: Any,
    runtime: SkillRuntime | None,
    container_root: Path | None = None,
    trusted: bool = True,
    source_kind: str = "local",
    plugin_config: Any = None,
) -> bool:
    parsed = _load_standard_skill_metadata(skill_dir)
    if not parsed:
        return False
    frontmatter, markdown_body = parsed
    mcp_config = normalize_mcp_config(
        skill_dir=skill_dir,
        raw=frontmatter.get("mcp"),
        default_timeout=max(
            1,
            int(getattr(plugin_config, "personification_skill_mcp_timeout", 20) or 20),
        ) if plugin_config is not None else 20,
    )
    script_path = _resolve_script_path(skill_dir, frontmatter)
    if script_path is None and mcp_config is None:
        compat_tools = build_compat_tools(
            skill_dir=skill_dir,
            frontmatter=frontmatter,
            runtime=runtime,
        )
        if compat_tools:
            for tool in compat_tools:
                if isinstance(tool, AgentTool):
                    registry.register(tool)
            logger.info(
                f"[custom skill] registered compatibility adapter tools for {skill_dir.name}: "
                f"{', '.join(tool.name for tool in compat_tools)}"
            )
            return True
        logger.warning(f"[custom skill] missing scripts entrypoint in {skill_dir}")
        return False
    config = _build_skill_config(
        skill_dir=skill_dir,
        frontmatter=frontmatter,
        markdown_body=markdown_body,
        openai_meta=_load_openai_meta(skill_dir),
        script_path=script_path,
    )
    python_paths = frontmatter.get("python_paths")
    if isinstance(python_paths, list):
        config["python_paths"] = [str(item).strip() for item in python_paths if str(item).strip()]
    config["runtime"] = runtime
    config["container_root"] = container_root
    config["trusted"] = trusted
    config["source_kind"] = source_kind
    config["plugin_config"] = plugin_config
    config["isolation"] = normalize_isolation_config(
        frontmatter.get("isolation"),
        trusted=trusted,
        default_timeout=max(
            1,
            int(getattr(plugin_config, "personification_skill_default_timeout", 15) or 15),
        ) if plugin_config is not None else 15,
    )
    config["mcp"] = mcp_config
    await _register_skill(registry, logger, skill_dir, config)
    return True


async def _load_new_layout_skills(
    skills_root: Path,
    registry: ToolRegistry,
    logger: Any,
    runtime: SkillRuntime | None = None,
    trusted: bool = True,
    source_kind: str = "local",
    plugin_config: Any = None,
) -> int:
    loaded = 0
    if not skills_root.exists() or not skills_root.is_dir():
        return loaded
    for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        if await _load_standard_skill_dir(
            skill_dir=skill_dir,
            registry=registry,
            logger=logger,
            runtime=runtime,
            container_root=skills_root,
            trusted=trusted,
            source_kind=source_kind,
            plugin_config=plugin_config,
        ):
            loaded += 1
    return loaded


async def _load_discovered_skill_dirs(
    root: Path,
    registry: ToolRegistry,
    logger: Any,
    runtime: SkillRuntime | None = None,
    trusted: bool = False,
    source_kind: str = "remote",
    plugin_config: Any = None,
) -> int:
    loaded = 0
    for skill_dir in discover_skill_dirs(root):
        if await _load_standard_skill_dir(
            skill_dir=skill_dir,
            registry=registry,
            logger=logger,
            runtime=runtime,
            container_root=root,
            trusted=trusted,
            source_kind=source_kind,
            plugin_config=plugin_config,
        ):
            loaded += 1
    return loaded


async def _load_legacy_layout_skills(
    skills_root: Path,
    registry: ToolRegistry,
    logger: Any,
    tool_caller: Any = None,
) -> int:
    loaded = 0
    custom_root = Path(skills_root) / "custom"
    if not custom_root.exists() or not custom_root.is_dir():
        return loaded

    for skill_dir in sorted(path for path in custom_root.iterdir() if path.is_dir()):
        skill_yaml = skill_dir / "skill.yaml"
        config = None
        if skill_yaml.exists():
            config = _load_skill_yaml(skill_yaml)
            if not config:
                logger.warning(f"[custom skill] invalid skill config: {skill_yaml}")
                continue
        else:
            handler_path_check = skill_dir / "handler.py"
            if handler_path_check.exists() and tool_caller is not None:
                config = await _auto_describe_handler(handler_path_check, tool_caller, logger)
                if not config:
                    logger.warning(f"[custom skill] auto-describe failed for {skill_dir.name}, skipping")
                    continue
            else:
                continue

        handler_script = str(config.get("handler_script") or "handler.py")
        handler_path = skill_dir / handler_script
        if not handler_path.exists():
            logger.warning(f"[custom skill] missing handler: {handler_path}")
            continue
        config = dict(config)
        config["script_path"] = handler_path
        await _register_skill(registry, logger, skill_dir, config)
        loaded += 1
    return loaded


async def load_custom_skills(
    skills_root: Path | None,
    registry: ToolRegistry,
    logger: Any,
    tool_caller: Any = None,
    plugin_config: Any = None,
    runtime: SkillRuntime | None = None,
) -> None:
    bundled_root = Path(__file__).resolve().parent.parent / "skills" / "skillpacks"
    bundled_loaded = await _load_new_layout_skills(
        bundled_root,
        registry,
        logger,
        runtime=runtime,
        trusted=True,
        source_kind="bundled",
        plugin_config=plugin_config,
    )

    external_loaded = 0
    legacy_loaded = 0
    source_loaded = 0
    if skills_root:
        root = Path(skills_root)
        external_loaded = await _load_new_layout_skills(
            root,
            registry,
            logger,
            runtime=runtime,
            trusted=True,
            source_kind="local",
            plugin_config=plugin_config,
        )
        legacy_loaded = await _load_legacy_layout_skills(root, registry, logger, tool_caller=tool_caller)
    if plugin_config is not None and runtime is not None:
        remote_enabled = bool(
            getattr(plugin_config, "personification_skill_remote_enabled", False)
        )
        raw_remote_sources = getattr(plugin_config, "personification_skill_sources", None)
        has_remote_sources = bool(raw_remote_sources)
        if has_remote_sources and not remote_enabled:
            logger.warning(
                "[custom skill] remote skill sources configured but disabled; "
                "set personification_skill_remote_enabled=true to allow fetching"
            )
        elif has_remote_sources:
            require_admin_review = bool(
                getattr(plugin_config, "personification_skill_require_admin_review", True)
            )
            approved_sources, pending_reviews = filter_approved_remote_sources(
                raw_remote_sources,
                logger,
                require_confirmation=require_admin_review,
            )
            if require_admin_review and pending_reviews:
                pending_names = ", ".join(
                    str(item.get("name") or item.get("source") or "?")
                    for item in pending_reviews[:5]
                )
                logger.warning(
                    "[custom skill] remote skill sources pending admin review; "
                    f"approved={len(approved_sources)} pending={len(pending_reviews)} names={pending_names}"
                )
            if not approved_sources:
                logger.info("[custom skill] no approved remote skill sources to load")
            else:
                class _RemoteSourceConfigProxy:
                    def __init__(self, original: Any, approved: list[dict[str, Any]]) -> None:
                        self._original = original
                        self._approved = approved

                    def __getattr__(self, name: str) -> Any:
                        if name == "personification_skill_sources":
                            return self._approved
                        return getattr(self._original, name)

                remote_config = _RemoteSourceConfigProxy(plugin_config, approved_sources)
                cache_dir = get_skill_cache_dir(remote_config, Path(runtime.data_dir or "data/personification"))
                source_roots = await resolve_skill_source_dirs(
                    plugin_config=remote_config,
                    logger=logger,
                    cache_dir=cache_dir,
                )
                for source_root in source_roots:
                    source_loaded += await _load_discovered_skill_dirs(
                        source_root,
                        registry,
                        logger,
                        runtime=runtime,
                        trusted=False,
                        source_kind="remote",
                        plugin_config=plugin_config,
                    )
    logger.info(
        f"[custom skill] loaded bundled={bundled_loaded} external={external_loaded} "
        f"source={source_loaded} legacy={legacy_loaded}"
    )


async def _auto_describe_handler(handler_path: Path, tool_caller: Any, logger: Any) -> dict | None:
    try:
        code = handler_path.read_text(encoding="utf-8")[:3000]
        prompt = f"""请分析以下 Python 函数代码，以 JSON 格式返回 skill 元数据：
{{
  "name": "snake_case英文名（最多30字符）",
  "description": "功能描述（中文，最多80字符）",
  "parameters": {{
    "type": "object",
    "properties": {{
      "param_name": {{"type": "string", "description": "参数说明"}}
    }},
    "required": ["必填参数列表"]
  }}
}}
代码：
{code}
只返回 JSON，不要其他内容。"""
        resp = await tool_caller.chat_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            use_builtin_search=False,
        )
        text = resp.content if hasattr(resp, "content") else str(resp)
        match = re.search(r"\{[\s\S]+\}", text)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:
        logger.warning(f"[custom skill] auto-describe error: {e}")
    return None
