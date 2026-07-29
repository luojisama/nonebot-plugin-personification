from __future__ import annotations

import asyncio
from typing import Any

from .db import connect_sync, get_db_path


async def purge_user_profile_data(
    bundle: Any,
    user_id: Any,
) -> dict[str, int]:
    """Purge derived user/profile data while intentionally retaining policy state."""

    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    counts: dict[str, int] = {
        "cancelled_scoped_tasks": 0,
        "personas": 0,
        "persona_histories": 0,
        "core_profiles": 0,
        "local_profiles": 0,
        "memory_items": 0,
        "relation_edges": 0,
        "avatar_relation_evidence": 0,
    }

    scoped_service = getattr(bundle, "scoped_profile_service", None)
    cancel_user_tasks = getattr(scoped_service, "cancel_user_tasks", None)
    if callable(cancel_user_tasks):
        counts["cancelled_scoped_tasks"] = int(await cancel_user_tasks(uid))

    persona_store = getattr(bundle, "persona_store", None)
    purge_persona = getattr(persona_store, "purge_user", None)
    if callable(purge_persona):
        result = await purge_persona(uid)
        for key in ("personas", "persona_histories"):
            counts[key] += max(0, int((result or {}).get(key, 0) or 0))

    memory_store = getattr(bundle, "memory_store", None)
    if memory_store is None:
        profile_service = getattr(bundle, "profile_service", None)
        memory_store = getattr(profile_service, "memory_store", None)
    purge_memory = getattr(memory_store, "purge_user_profile_data", None)
    if callable(purge_memory):
        result = await asyncio.to_thread(purge_memory, uid)
        for key in ("core_profiles", "local_profiles", "memory_items"):
            counts[key] += max(0, int((result or {}).get(key, 0) or 0))

    policy_service = getattr(bundle, "user_policy_service", None)
    db_path = getattr(policy_service, "db_path", None) or get_db_path()
    with connect_sync(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # These legacy rows are deleted here as a safe fallback when PersonaStore is
        # disabled or unavailable in a partially initialized runtime.
        persona_cursor = conn.execute("DELETE FROM user_personas WHERE user_id=?", (uid,))
        history_cursor = conn.execute("DELETE FROM persona_histories WHERE user_id=?", (uid,))
        edge_cursor = conn.execute(
            "DELETE FROM group_relation_edges WHERE src_user_id=? OR dst_user_id=?",
            (uid, uid),
        )
        avatar_cursor = conn.execute(
            "DELETE FROM avatar_relation_evidence WHERE left_user_id=? OR right_user_id=?",
            (uid, uid),
        )
        conn.commit()
    counts["personas"] += max(0, int(persona_cursor.rowcount or 0))
    counts["persona_histories"] += max(0, int(history_cursor.rowcount or 0))
    counts["relation_edges"] = max(0, int(edge_cursor.rowcount or 0))
    counts["avatar_relation_evidence"] = max(0, int(avatar_cursor.rowcount or 0))
    return counts


__all__ = ["purge_user_profile_data"]
