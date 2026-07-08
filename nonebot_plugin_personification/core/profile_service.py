from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .memory_store import MemoryStore
from .user_profile_meta import (
    compact_user_profile_meta_summary,
    merge_profile_json_with_meta,
    render_user_profile_meta,
)


@dataclass
class ProfileSnapshot:
    profile_text: str
    profile_json: dict[str, Any]
    updated_at: float
    source: str = ""

    def snippet(self, max_chars: int = 150) -> str:
        text = str(self.profile_text or "").strip()
        if not text:
            text = compact_user_profile_meta_summary(self.profile_json.get("qq_profile", {}))
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
            source=str(data.get("source", "") or ""),
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

    def upsert_user_profile_meta(
        self,
        *,
        user_id: str,
        meta: dict[str, Any],
        source: str = "profile_meta",
    ) -> ProfileSnapshot | None:
        uid = str(user_id or "").strip()
        if not uid or not isinstance(meta, dict):
            return self.get_core_profile(uid)
        current = self.get_core_profile(uid)
        profile_text = current.profile_text if current is not None else ""
        profile_json = dict(current.profile_json if current is not None else {})
        merged_json = merge_profile_json_with_meta(profile_json, {**meta, "source": source})
        self.upsert_core_profile(
            user_id=uid,
            profile_text=profile_text,
            profile_json=merged_json,
            source=(current.source if current is not None and current.source else source),
        )
        return self.get_core_profile(uid)

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

    @staticmethod
    def _render_structured_profile_card(profile_json: dict[str, Any]) -> str:
        structured = profile_json.get("structured") if isinstance(profile_json, dict) else {}
        if not isinstance(structured, dict) or not structured:
            return ""
        rows: list[str] = []
        portrait = str(structured.get("portrait", "") or "").strip()
        if portrait:
            rows.append(f"[人物轮廓] {portrait[:260]}")
        compact_fields = [
            ("称呼偏好", "nickname_pref"),
            ("兴趣", "interests"),
            ("作息", "routine"),
            ("说话风格", "communication_style"),
            ("情绪基线", "emotion_baseline"),
            ("社交模式", "social_mode"),
            ("关系", "relationship"),
            ("近期关注", "recent_focus"),
            ("雷区", "taboos"),
            ("互动建议", "interaction_advice"),
        ]
        facts: list[str] = []
        for label, key in compact_fields:
            value = str(structured.get(key, "") or "").strip()
            if value:
                facts.append(f"{label}: {value[:120]}")
        if facts:
            rows.append("[立体人物卡] " + "；".join(facts[:8]))
        return "\n".join(rows)

    def build_prompt_block(self, *, user_id: str, group_id: str = "") -> str:
        """生成注入到 system prompt 的"用户档案"段落。

        全局印象 + 本群角色双层展示；任何一层缺失都容错处理。
        """
        lines: list[str] = []
        core = self.get_core_profile(str(user_id or ""))
        if core and core.profile_json:
            meta_block = render_user_profile_meta(core.profile_json.get("qq_profile", {}))
            if meta_block:
                lines.append(meta_block)
            structured_block = self._render_structured_profile_card(core.profile_json)
            if structured_block:
                lines.append(structured_block)
        if core and core.profile_text:
            lines.append(f"[全局印象] {core.snippet(220)}")
        if str(group_id or "").strip():
            local = self.get_local_profile(group_id=group_id, user_id=user_id)
            if local and local.profile_text:
                lines.append(f"[在本群] {local.snippet(220)}")
        if not lines:
            return ""
        return "## 用户档案\n" + "\n".join(lines)
