from __future__ import annotations

from typing import Any


_ROLE_LABELS = {
    "owner": "群主",
    "admin": "管理员",
    "member": "成员",
}


def extract_sender_role(event_or_sender: Any) -> str:
    sender = getattr(event_or_sender, "sender", event_or_sender)
    if isinstance(sender, dict):
        return normalize_group_role(sender.get("role"))
    return normalize_group_role(getattr(sender, "role", ""))


def normalize_group_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in _ROLE_LABELS:
        return role
    return ""


def render_group_role_label(value: Any) -> str:
    role = normalize_group_role(value)
    return _ROLE_LABELS.get(role, "")


__all__ = [
    "extract_sender_role",
    "normalize_group_role",
    "render_group_role_label",
]
