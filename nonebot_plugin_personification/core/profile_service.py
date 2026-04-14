from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .memory_store import MemoryStore


@dataclass
class ProfileSnapshot:
    profile_text: str
    profile_json: dict[str, Any]
    updated_at: float

    def snippet(self, max_chars: int = 150) -> str:
        text = str(self.profile_text or "").strip()
        if max_chars <= 0 or not text:
            return ""
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars]}..."


class ProfileService:
    def __init__(self, memory_store: MemoryStore) -> None:
        self.memory_store = memory_store

    def get_core_profile(self, user_id: str) -> ProfileSnapshot | None:
        data = self.memory_store.get_core_profile(user_id)
        if not data:
            return None
        return ProfileSnapshot(
            profile_text=str(data.get("profile_text", "") or ""),
            profile_json=dict(data.get("profile_json", {}) or {}),
            updated_at=float(data.get("updated_at", 0) or 0),
        )

    def upsert_core_profile(
        self,
        *,
        user_id: str,
        profile_text: str,
        profile_json: dict[str, Any] | None = None,
        source: str = "profile_service",
    ) -> None:
        self.memory_store.upsert_core_profile(
            user_id=user_id,
            profile_text=profile_text,
            profile_json=profile_json,
            source=source,
        )

    def get_local_profile(self, *, group_id: str, user_id: str) -> ProfileSnapshot | None:
        data = self.memory_store.get_local_profile(group_id=group_id, user_id=user_id)
        if not data:
            return None
        return ProfileSnapshot(
            profile_text=str(data.get("profile_text", "") or ""),
            profile_json=dict(data.get("profile_json", {}) or {}),
            updated_at=float(data.get("updated_at", 0) or 0),
        )

    def upsert_local_profile(
        self,
        *,
        group_id: str,
        user_id: str,
        profile_text: str,
        profile_json: dict[str, Any] | None = None,
    ) -> None:
        self.memory_store.upsert_local_profile(
            group_id=group_id,
            user_id=user_id,
            profile_text=profile_text,
            profile_json=profile_json,
        )
