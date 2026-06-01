from __future__ import annotations

from typing import Any

from .data_store import get_data_store


_NS = "skill_overrides"


def _load() -> dict[str, Any]:
    data = get_data_store().load_sync(_NS)
    return data if isinstance(data, dict) else {}


def is_disabled(name: str) -> bool:
    key = str(name or "").strip()
    if not key:
        return False
    try:
        data = _load()
    except Exception:
        return False
    entry = data.get(key)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("disabled", False))


def set_disabled(name: str, disabled: bool, *, reason: str = "") -> None:
    key = str(name or "").strip()
    if not key:
        return

    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        if disabled:
            data[key] = {"disabled": True, "reason": str(reason or "")}
        else:
            data.pop(key, None)
        return data

    get_data_store().mutate_sync(_NS, _mutate)


def list_overrides() -> dict[str, dict[str, Any]]:
    try:
        data = _load()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, entry in data.items():
        if isinstance(entry, dict):
            out[str(key)] = {
                "disabled": bool(entry.get("disabled", False)),
                "reason": str(entry.get("reason", "")),
            }
    return out


__all__ = ["is_disabled", "set_disabled", "list_overrides"]
