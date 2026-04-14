from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

from module_loader import load_skill_module


def _load_module(script_path: Path, sys_paths: list[str]):
    return load_skill_module(
        skill_dir=script_path.parent.parent if script_path.parent.name == "scripts" else script_path.parent,
        script_path=script_path,
        extra_sys_paths=[Path(item) for item in sys_paths if item],
        module_prefix="personification_isolated_skill",
    )


async def _run_handler(module, function_name: str, kwargs: dict) -> str:
    handler = getattr(module, function_name, None)
    if not callable(handler):
        raise AttributeError(f"Missing callable {function_name}")
    result = handler(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return str(result)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        script_path = Path(str(payload.get("script_path") or "")).resolve()
        function_name = str(payload.get("function") or "run").strip() or "run"
        kwargs = payload.get("kwargs") if isinstance(payload.get("kwargs"), dict) else {}
        sys_paths_raw = payload.get("sys_paths")
        sys_paths = [str(item) for item in sys_paths_raw] if isinstance(sys_paths_raw, list) else []
        module = _load_module(script_path, sys_paths)
        result = asyncio.run(_run_handler(module, function_name, kwargs))
        sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
