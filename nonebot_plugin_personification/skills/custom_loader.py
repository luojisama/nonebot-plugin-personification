from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from ..agent.tool_registry import AgentTool, ToolRegistry


def _load_skill_yaml(path: Path) -> dict | None:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_handler_module(skill_dir: Path, handler_script: str):
    module_name = f"personification_custom_skill_{skill_dir.name}"
    handler_path = skill_dir / handler_script
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load handler module from {handler_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BlockedSubprocess:
    def __call__(self, *args, **kwargs):
        raise PermissionError("subprocess is not allowed in custom skill")


async def _run_custom_handler(handler, kwargs: dict) -> str:
    blocked = _BlockedSubprocess()
    originals = {
        "run": subprocess.run,
        "Popen": subprocess.Popen,
        "call": subprocess.call,
        "check_call": subprocess.check_call,
        "check_output": subprocess.check_output,
    }
    try:
        subprocess.run = blocked
        subprocess.Popen = blocked
        subprocess.call = blocked
        subprocess.check_call = blocked
        subprocess.check_output = blocked
        result = await asyncio.wait_for(handler(**kwargs), timeout=10)
        return str(result)
    except asyncio.TimeoutError:
        return "custom skill timeout after 10 seconds"
    except PermissionError as e:
        return f"PermissionError: {e}"
    except Exception as e:
        return f"custom skill error: {e}"
    finally:
        subprocess.run = originals["run"]
        subprocess.Popen = originals["Popen"]
        subprocess.call = originals["call"]
        subprocess.check_call = originals["check_call"]
        subprocess.check_output = originals["check_output"]


async def load_custom_skills(
    skills_root: Path,
    registry: ToolRegistry,
    logger: Any,
    tool_caller: Any = None,
) -> None:
    custom_root = Path(skills_root) / "custom"
    if not custom_root.exists() or not custom_root.is_dir():
        return

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

        try:
            module = _load_handler_module(skill_dir, handler_script)
            handler = getattr(module, "run")
        except Exception as e:
            logger.warning(f"[custom skill] load failed for {skill_dir.name}: {e}")
            continue

        async def _handler(_handler_ref=handler, **kwargs) -> str:
            return await _run_custom_handler(_handler_ref, kwargs)

        registry.register(
            AgentTool(
                name=str(config.get("name") or skill_dir.name),
                description=str(config.get("description") or ""),
                parameters=config.get("parameters") or {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                handler=_handler,
                local=bool(config.get("local", True)),
                enabled=lambda cfg=config: bool(cfg.get("enabled", True)),
            )
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
