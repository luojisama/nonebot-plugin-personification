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

    def list_groups(self) -> list[str]:
        return list(self.memory_store.list_groups())

    def list_core_profiles(self) -> list[dict[str, Any]]:
        return self.memory_store.list_core_profiles()

    def list_local_profiles(self, group_id: str) -> list[dict[str, Any]]:
        return self.memory_store.list_local_profiles(group_id)

    def build_prompt_block(self, *, user_id: str, group_id: str = "") -> str:
        """生成注入到 system prompt 的"用户档案"段落。

        全局印象 + 本群角色双层展示；任何一层缺失都容错处理。
        """
        lines: list[str] = []
        core = self.get_core_profile(str(user_id or ""))
        if core and core.profile_text:
            lines.append(f"[全局印象] {core.snippet(220)}")
        if str(group_id or "").strip():
            local = self.get_local_profile(group_id=group_id, user_id=user_id)
            if local and local.profile_text:
                lines.append(f"[在本群] {local.snippet(220)}")
        if not lines:
            return ""
        return "## 用户档案\n" + "\n".join(lines)
