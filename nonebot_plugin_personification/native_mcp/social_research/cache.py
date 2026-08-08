from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any


class PacketCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or float(payload.get("expires_at", 0) or 0) <= time.time():
            return None
        value = payload.get("value")
        return dict(value) if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        path = self._path(key)
        temporary = path.with_suffix(".tmp")
        payload = {"expires_at": time.time() + max(60, int(ttl_seconds)), "value": value}
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(temporary, path)


__all__ = ["PacketCache"]

