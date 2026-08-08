from __future__ import annotations

import asyncio
import inspect
import json
import os
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
from .source_resolver import (
    discover_skill_dirs,
    get_skill_cache_dir,
    resolve_skill_sources,
    skill_source_content_digest,
)


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


def _is_path_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath([str(root.resolve()), str(candidate.resolve())]) == str(root.resolve())
    except Exception:
        return False


def _resolve_script_path(
    skill_dir: Path,
    frontmatter: dict[str, Any],
    *,
    allowed_root: Path,
) -> Path | None:
    scripts_dir = skill_dir / "scripts"
    entry = str(frontmatter.get("entrypoint") or frontmatter.get("script") or "").strip()
    if entry:
        candidate = (skill_dir / entry).resolve()
        if _is_path_within(allowed_root, candidate) and candidate.is_file() and not candidate.is_symlink():
            return candidate
        candidate = (scripts_dir / entry).resolve()
        return (
            candidate
            if _is_path_within(allowed_root, candidate) and candidate.is_file() and not candidate.is_symlink()
            else None
        )
    if not scripts_dir.exists():
        return None
    for name in ("main.py", "run.py", "skill.py"):
        candidate = scripts_dir / name
        if candidate.exists():
            return candidate.resolve() if _is_path_within(allowed_root, candidate) else None
    py_files = sorted(scripts_dir.glob("*.py"))
    return py_files[0].resolve() if py_files and _is_path_within(allowed_root, py_files[0]) else None


def _normalize_parameters(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"type": "object", "properties": {}, "required": []}


def _stamp_tool_source(tool: AgentTool, source_kind: str) -> AgentTool:
    metadata = dict(tool.metadata or {})
    metadata["source_kind"] = str(source_kind or "local").strip().lower() or "local"
    tool.metadata = metadata
    return tool


def _stable_fingerprint_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def _tool_registration_fingerprint(tool: AgentTool) -> tuple[Any, ...]:
    return (
        id(tool),
        str(tool.name or ""),
        str(tool.description or ""),
        _stable_fingerprint_value(tool.parameters or {}),
        id(tool.handler),
        bool(tool.local),
        id(tool.enabled),
        _stable_fingerprint_value(tool.metadata or {}),
        int(tool.per_session_quota or 0),
    )


def _registry_tool_fingerprints(registry: ToolRegistry) -> dict[str, tuple[Any, ...]]:
    _, current = registry.snapshot()
    return {name: _tool_registration_fingerprint(tool) for name, tool in current.items()}


def _stamp_registered_tool_sources(
    registry: ToolRegistry,
    before: dict[str, tuple[Any, ...]],
    source_kind: str,
) -> None:
    _, current = registry.snapshot()
    for name, tool in current.items():
        if before.get(name) == _tool_registration_fingerprint(tool):
            continue
        registry.register(_stamp_tool_source(tool, source_kind))


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
    expected_content_digest = str(config.get("content_digest") or "").strip().lower()
    source_kind = str(config.get("source_kind") or "local").strip().lower() or "local"

    if not trusted and not allow_unsafe_external and (is_mcp_isolated or not is_process_isolated):
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
            approved_root=container_root if isinstance(container_root, Path) else skill_dir,
            content_digest=expected_content_digest,
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
            if expected_content_digest and isinstance(container_root, Path):
                try:
                    current_digest = skill_source_content_digest(container_root)
                except Exception:
                    current_digest = ""
                if current_digest != expected_content_digest:
                    return "isolated skill blocked: approved content digest changed"
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
                metadata={
                    "category": "skillpack",
                    "source_kind": source_kind,
                    "skill_dir": skill_dir.name,
                    "isolation": str(isolation.get("mode") or "process"),
                },
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
        before = _registry_tool_fingerprints(registry)
        try:
            result = register_handler(runtime, registry)
            if inspect.isawaitable(result):
                await result
            return
        except Exception as e:
            logger.warning(f"[custom skill] register failed {skill_dir.name}: {e}")
            return
        finally:
            _stamp_registered_tool_sources(registry, before, source_kind)

    build_tools_handler = getattr(module, "build_tools", None)
    if callable(build_tools_handler) and runtime is not None:
        try:
            result = build_tools_handler(runtime)
            if inspect.isawaitable(result):
                result = await result
            tools = result if isinstance(result, list) else []
            for tool in tools:
                if isinstance(tool, AgentTool):
                    registry.register(_stamp_tool_source(tool, source_kind))
            if isinstance(result, list):
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
            metadata={
                "category": "skillpack",
                "source_kind": source_kind,
                "skill_dir": skill_dir.name,
                "isolation": str(isolation.get("mode") or "inprocess"),
            },
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
    content_digest: str = "",
) -> bool:
    parsed = _load_standard_skill_metadata(skill_dir)
    if not parsed:
        return False
    frontmatter, markdown_body = parsed
    allowed_root = container_root if isinstance(container_root, Path) else skill_dir
    raw_mcp_config = frontmatter.get("mcp")
    mcp_config = normalize_mcp_config(
        skill_dir=skill_dir,
        raw=raw_mcp_config,
        default_timeout=max(
            1,
            int(getattr(plugin_config, "personification_skill_mcp_timeout", 20) or 20),
        ) if plugin_config is not None else 20,
        allowed_root=allowed_root,
        restrict_paths=not trusted,
    )
    if isinstance(raw_mcp_config, dict) and raw_mcp_config and mcp_config is None:
        logger.warning(f"[custom skill] rejected invalid MCP config: {skill_dir.name}")
        return False
    script_path = _resolve_script_path(skill_dir, frontmatter, allowed_root=allowed_root)
    if script_path is None and mcp_config is None:
        compat_tools = build_compat_tools(
            skill_dir=skill_dir,
            frontmatter=frontmatter,
            runtime=runtime,
        )
        if compat_tools:
            for tool in compat_tools:
                if isinstance(tool, AgentTool):
                    registry.register(_stamp_tool_source(tool, source_kind))
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
        resolved_python_paths: list[str] = []
        for item in python_paths:
            value = str(item).strip()
            if not value:
                continue
            candidate = (skill_dir / value).resolve()
            if not _is_path_within(allowed_root, candidate) or not candidate.is_dir() or candidate.is_symlink():
                logger.warning(f"[custom skill] rejected python_path outside approved root: {skill_dir.name}")
                return False
            resolved_python_paths.append(str(candidate))
        config["python_paths"] = resolved_python_paths
    config["runtime"] = runtime
    config["container_root"] = container_root
    config["trusted"] = trusted
    config["source_kind"] = source_kind
    config["plugin_config"] = plugin_config
    config["content_digest"] = str(content_digest or "")
    config["isolation"] = normalize_isolation_config(
        frontmatter.get("isolation"),
        trusted=trusted,
        default_timeout=max(
            1,
            int(getattr(plugin_config, "personification_skill_default_timeout", 15) or 15),
        ) if plugin_config is not None else 15,
    )
    cwd_raw = str(config["isolation"].get("cwd") or "").strip()
    if cwd_raw:
        cwd_path = (skill_dir / cwd_raw).resolve()
        if not _is_path_within(allowed_root, cwd_path) or not cwd_path.is_dir() or cwd_path.is_symlink():
            logger.warning(f"[custom skill] rejected cwd outside approved root: {skill_dir.name}")
            return False
        config["isolation"]["cwd"] = str(cwd_path)
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
    content_digest: str = "",
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
            content_digest=content_digest,
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
            cache_dir = get_skill_cache_dir(plugin_config, Path(runtime.data_dir or "data/personification"))
            resolved_sources = await resolve_skill_sources(
                plugin_config=plugin_config,
                logger=logger,
                cache_dir=cache_dir,
            )
            prepared_sources = [
                {**item.source, "content_digest": item.content_digest}
                for item in resolved_sources
            ]
            require_admin_review = bool(
                getattr(plugin_config, "personification_skill_require_admin_review", True)
            )
            approved_sources, pending_reviews = filter_approved_remote_sources(
                prepared_sources,
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
                approved_bindings = {
                    (
                        str(source.get("source") or "").strip(),
                        str(source.get("ref") or "").strip(),
                        str(source.get("subdir") or "").strip(),
                        str(source.get("kind") or "auto").strip().lower() or "auto",
                        str(source.get("content_digest") or "").strip().lower(),
                    )
                    for source in approved_sources
                }
                for resolved_source in resolved_sources:
                    binding = (
                        str(resolved_source.source.get("source") or "").strip(),
                        str(resolved_source.source.get("ref") or "").strip(),
                        str(resolved_source.source.get("subdir") or "").strip(),
                        str(resolved_source.source.get("kind") or "auto").strip().lower() or "auto",
                        resolved_source.content_digest,
                    )
                    if binding not in approved_bindings:
                        continue
                    source_loaded += await _load_discovered_skill_dirs(
                        resolved_source.root,
                        registry,
                        logger,
                        runtime=runtime,
                        trusted=False,
                        source_kind="remote",
                        plugin_config=plugin_config,
                        content_digest=resolved_source.content_digest,
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
