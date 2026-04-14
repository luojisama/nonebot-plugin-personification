from __future__ import annotations

from typing import Any

from ..utils import get_group_config
from .data_store import get_data_store


_PLUGIN_ADMIN_STORE = "plugin_admins"


def load_plugin_admins() -> list[str]:
    data = get_data_store().load_sync(_PLUGIN_ADMIN_STORE)
    if not isinstance(data, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in data:
        user_id = str(item or "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        result.append(user_id)
    return result


def save_plugin_admins(user_ids: list[str]) -> None:
    normalized = []
    seen: set[str] = set()
    for item in user_ids:
        user_id = str(item or "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        normalized.append(user_id)
    get_data_store().save_sync(_PLUGIN_ADMIN_STORE, normalized)


def add_plugin_admin(user_id: str) -> bool:
    target = str(user_id or "").strip()
    if not target:
        return False
    changed = False

    def _mutate(current: object) -> list[str]:
        nonlocal changed
        admins = current if isinstance(current, list) else []
        normalized = [str(item or "").strip() for item in admins if str(item or "").strip()]
        if target in normalized:
            return normalized
        normalized.append(target)
        changed = True
        return normalized

    get_data_store().mutate_sync(_PLUGIN_ADMIN_STORE, _mutate)
    return changed


def remove_plugin_admin(user_id: str) -> bool:
    target = str(user_id or "").strip()
    if not target:
        return False
    changed = False

    def _mutate(current: object) -> list[str]:
        nonlocal changed
        admins = current if isinstance(current, list) else []
        normalized = [str(item or "").strip() for item in admins if str(item or "").strip()]
        if target not in normalized:
            return normalized
        normalized.remove(target)
        changed = True
        return normalized

    get_data_store().mutate_sync(_PLUGIN_ADMIN_STORE, _mutate)
    return changed


def is_superuser(user_id: str, superusers: set[str] | None) -> bool:
    return str(user_id or "").strip() in {str(item) for item in (superusers or set())}


def is_plugin_admin(user_id: str) -> bool:
    return str(user_id or "").strip() in set(load_plugin_admins())


def is_group_admin_event(event: Any) -> bool:
    sender = getattr(event, "sender", None)
    role = ""
    if isinstance(sender, dict):
        role = str(sender.get("role", "") or "").strip().lower()
    elif sender is not None:
        role = str(getattr(sender, "role", "") or "").strip().lower()
    return role in {"admin", "owner"}


def can_manage_group_scope(
    *,
    event: Any,
    target_group_id: str,
    superusers: set[str] | None,
) -> bool:
    user_id = str(getattr(event, "user_id", "") or "").strip()
    if is_superuser(user_id, superusers) or is_plugin_admin(user_id):
        return True
    current_group_id = str(getattr(event, "group_id", "") or "").strip()
    if not current_group_id or current_group_id != str(target_group_id or "").strip():
        return False
    group_config = get_group_config(current_group_id)
    allow_group_admin = bool(group_config.get("allow_group_admin_config", True))
    return allow_group_admin and is_group_admin_event(event)


def can_manage_sensitive_action(
    *,
    event: Any,
    superusers: set[str] | None,
    allow_group_admin: bool = False,
    target_group_id: str = "",
) -> bool:
    user_id = str(getattr(event, "user_id", "") or "").strip()
    if is_superuser(user_id, superusers) or is_plugin_admin(user_id):
        return True
    if allow_group_admin and target_group_id:
        return can_manage_group_scope(
            event=event,
            target_group_id=target_group_id,
            superusers=superusers,
        )
    return False
