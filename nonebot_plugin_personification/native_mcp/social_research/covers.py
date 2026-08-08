from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_COVER_HOSTS = {
    "bilibili": ("hdslb.com", "biliimg.com"),
    "douyin": ("douyinpic.com", "byteimg.com", "pstatp.com"),
    "tieba": ("tiebapic.baidu.com", "imgsa.baidu.com", "hiphotos.baidu.com"),
    "xiaoheihe": ("xiaoheihe.cn", "heybox.cn", "max-c.com"),
}


class CoverRegistry:
    def __init__(self, root: Path, *, ttl_seconds: int = 21600) -> None:
        self.path = root / "cover_refs.json"
        self.ttl_seconds = max(300, int(ttl_seconds))
        self._items = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        now = time.time()
        return {
            str(key): dict(item)
            for key, item in value.items()
            if isinstance(item, dict) and float(item.get("expires_at", 0) or 0) > now
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def _allowed(platform: str, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and any(host == suffix or host.endswith("." + suffix) for suffix in _COVER_HOSTS.get(platform, ()))
        )

    def register(self, platform: str, url: str) -> str:
        value = str(url or "").strip()
        if not self._allowed(platform, value):
            return ""
        now = time.time()
        token = "cover_" + hashlib.sha256(f"{platform}\0{value}".encode("utf-8")).hexdigest()[:40]
        self._items[token] = {"platform": platform, "url": value, "expires_at": now + self.ttl_seconds}
        if len(self._items) > 2000:
            self._items = {
                key: item for key, item in self._items.items()
                if float(item.get("expires_at", 0) or 0) > now
            }
        self._save()
        return token

    def resolve(self, cover_ref: str) -> dict[str, Any]:
        token = str(cover_ref or "").strip()
        item = self._items.get(token)
        if item is None or float(item.get("expires_at", 0) or 0) <= time.time():
            raise KeyError("cover_ref_not_found")
        platform = str(item.get("platform") or "")
        url = str(item.get("url") or "")
        if not self._allowed(platform, url):
            raise KeyError("cover_ref_not_found")
        return {"platform": platform, "url": url, "expires_at": float(item["expires_at"])}


__all__ = ["CoverRegistry"]
