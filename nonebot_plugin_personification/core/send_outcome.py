from __future__ import annotations

from typing import Any


def is_likely_delivered_send_timeout(exc: BaseException) -> bool:
    info: Any = getattr(exc, "info", None)
    if not isinstance(info, dict):
        return False
    try:
        if int(info.get("retcode") or 0) == 1200:
            return True
    except (TypeError, ValueError):
        pass
    haystack = f"{info.get('message', '')} {info.get('wording', '')}".lower()
    return "invoke timeout" in haystack


__all__ = ["is_likely_delivered_send_timeout"]
