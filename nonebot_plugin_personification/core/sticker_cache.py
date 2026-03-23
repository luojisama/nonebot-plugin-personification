import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

_STICKER_SUFFIXES = {".jpg", ".png", ".gif", ".webp", ".jpeg"}
_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, List[Path]]] = {}


def get_sticker_files(path: str | Path | None, *, ttl_seconds: int = 300) -> List[Path]:
    if not path:
        return []

    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_dir():
        return []

    cache_key = str(path_obj.resolve())
    now = time.time()

    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] > now:
            return list(cached[1])

        scanned = [
            f
            for f in path_obj.iterdir()
            if f.is_file() and f.suffix.lower() in _STICKER_SUFFIXES
        ]
        _cache[cache_key] = (now + max(1, int(ttl_seconds)), scanned)
        return list(scanned)
