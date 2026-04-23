from __future__ import annotations

import json
from pathlib import Path


def scan_runtime_data(plugin_name: str, data_base_dir: Path) -> dict | None:
    root = data_base_dir / plugin_name
    if not root.exists() or not root.is_dir():
        return None

    files: list[dict[str, object]] = []
    notable_files: dict[str, dict[str, object]] = {}

    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(root).parts
            if len(rel_parts) > 3:
                continue
            rel_path = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            entry = {"path": rel_path, "size": size}
            files.append(entry)
            if path.suffix.lower() != ".json" or size >= 50 * 1024:
                continue
            try:
                raw_text = path.read_text(encoding="utf-8")
                parsed = json.loads(raw_text)
                notable_files[rel_path] = {
                    "size": size,
                    "preview": json.dumps(parsed, ensure_ascii=False)[:500],
                }
            except Exception:
                continue
    except Exception:
        return None

    return {
        "data_dir": f"data/{plugin_name}",
        "files": files,
        "notable_files": notable_files,
    }


__all__ = ["scan_runtime_data"]
