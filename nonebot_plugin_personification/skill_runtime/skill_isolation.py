from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


RUNNER_PATH = Path(__file__).resolve().parent / "isolated_runner.py"
_SAFE_ENV_KEYS = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
_SAFE_ENV_KEYS_LOWER = frozenset(key.lower() for key in _SAFE_ENV_KEYS)
_PYTHON_ENV_GUARDS = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "PYTHONUTF8": "1",
}


def normalize_isolation_config(
    raw: Any,
    *,
    trusted: bool,
    default_timeout: int = 15,
) -> dict[str, Any]:
    block = raw if isinstance(raw, dict) else {}
    mode = str(block.get("mode") or "").strip().lower()
    if mode not in {"", "inprocess", "process"}:
        mode = ""
    if not mode:
        mode = "inprocess" if trusted else "process"

    timeout = int(block.get("timeout", default_timeout) or default_timeout)
    python_executable = str(block.get("python") or block.get("python_executable") or "").strip()
    cwd = str(block.get("cwd") or "").strip()
    inherit_env = bool(block.get("inherit_env", trusted))
    env_raw = block.get("env")
    env = env_raw if isinstance(env_raw, dict) else {}
    return {
        "mode": mode,
        "timeout": max(1, timeout),
        "python_executable": python_executable,
        "cwd": cwd,
        "inherit_env": inherit_env,
        "env": {str(k): str(v) for k, v in env.items()},
    }


def script_supports_function(script_path: Path, function_name: str = "run") -> bool:
    try:
        text = script_path.read_text(encoding="utf-8")
    except Exception:
        return False
    markers = [
        f"def {function_name}(",
        f"async def {function_name}(",
    ]
    return any(marker in text for marker in markers)


def _build_subprocess_env(*, inherit_env: bool, extra_env: dict[str, Any] | None = None) -> dict[str, str]:
    if inherit_env:
        env = os.environ.copy()
    else:
        env = {
            key: value
            for key, value in os.environ.items()
            if key.lower() in _SAFE_ENV_KEYS_LOWER
        }
        for canonical, aliases in {
            "SystemRoot": ("SystemRoot", "SYSTEMROOT"),
            "WINDIR": ("WINDIR", "windir"),
            "ComSpec": ("ComSpec", "COMSPEC"),
        }.items():
            if canonical in env:
                continue
            for alias in aliases:
                value = os.environ.get(alias)
                if value:
                    env[canonical] = value
                    break
    env.update({str(k): str(v) for k, v in dict(extra_env or {}).items()})
    for key, value in _PYTHON_ENV_GUARDS.items():
        env[key] = value
    return env


async def run_skill_in_subprocess(
    *,
    script_path: Path,
    function_name: str,
    kwargs: dict[str, Any],
    skill_dir: Path,
    container_root: Path | None = None,
    python_paths: list[str] | None = None,
    isolation: dict[str, Any] | None = None,
) -> str:
    isolation_cfg = isolation or {}
    python_executable = str(isolation_cfg.get("python_executable") or "").strip() or sys.executable
    timeout = max(1, int(isolation_cfg.get("timeout", 15) or 15))
    inherit_env = bool(isolation_cfg.get("inherit_env", True))
    env = _build_subprocess_env(
        inherit_env=inherit_env,
        extra_env=dict(isolation_cfg.get("env") or {}),
    )
    cwd_raw = str(isolation_cfg.get("cwd") or "").strip()
    cwd = str((skill_dir / cwd_raw).resolve()) if cwd_raw else str(skill_dir.resolve())

    sys_paths: list[str] = []
    for path in [
        script_path.parent,
        skill_dir,
        container_root,
        *[(skill_dir / rel) for rel in (python_paths or [])],
    ]:
        if path is None:
            continue
        candidate = str(Path(path).resolve())
        if candidate not in sys_paths:
            sys_paths.append(candidate)

    payload = {
        "script_path": str(script_path.resolve()),
        "function": function_name,
        "kwargs": kwargs,
        "sys_paths": sys_paths,
    }

    proc = await asyncio.create_subprocess_exec(
        python_executable,
        str(RUNNER_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        timeout=timeout,
    )
    text = stdout.decode("utf-8", errors="replace").strip()
    err_text = stderr.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text) if text else {}
    except Exception:
        parsed = {}
    if isinstance(parsed, dict) and parsed.get("ok") is True:
        return str(parsed.get("result") or "")
    if isinstance(parsed, dict) and parsed.get("error"):
        return f"isolated skill error: {parsed.get('error')}"
    if err_text:
        return f"isolated skill error: {err_text}"
    return "isolated skill error: unknown failure"
