from __future__ import annotations

import json
import re
from typing import Any


def _normalize_trigger_words(text: str) -> set[str]:
    words = re.split(r"[，,、\s]+", str(text or ""))
    return {w.strip().lower() for w in words if len(w.strip()) >= 1}


def match_lorebook_triggers(message_text: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = str(message_text or "").strip()
    if not text or not entries:
        return []
    matched: list[tuple[int, dict[str, Any]]] = []
    text_lower = text.lower()
    for entry in entries:
        triggers = entry.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        if not isinstance(triggers, list):
            continue
        score = 0
        for trigger in triggers:
            if str(trigger or "").strip().lower() in text_lower:
                score += 1
        if score > 0:
            matched.append((score, entry))
    matched.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in matched[:5]]


def format_lorebook_injection(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = ["## 角色立场知识库（lorebook）"]
    for entry in entries:
        topic = str(entry.get("topic", "") or "").strip()
        stance = str(entry.get("stance", "") or "").strip()
        if topic and stance:
            lines.append(f"- 关于「{topic}」：{stance}")
    if len(lines) <= 1:
        return ""
    lines.append("以上为已知的角色立场，如当前话题涉及这些主题，按此立场自然回应，不要扮演成普通 AI 助手。")
    return "\n".join(lines)


async def list_lorebook_entries(memory_store: Any) -> list[dict[str, Any]]:
    return (memory_store or None) and memory_store.list_recent_memories(
        memory_type="persona_knowledge",
        limit=200,
    ) or []


async def add_lorebook_entry(
    memory_store: Any,
    *,
    topic: str,
    stance: str,
    triggers: str = "",
    weight: float = 1.0,
) -> str:
    trigger_words = list(_normalize_trigger_words(triggers or topic))[:8]
    item = {
        "memory_id": f"lore_{_safe_id(topic)}",
        "memory_type": "persona_knowledge",
        "summary": f"topic={topic}; stance={stance}",
        "topic": topic,
        "stance": stance,
        "triggers": trigger_words,
        "weight": max(0.1, min(3.0, float(weight or 1.0))),
        "confidence": 1.0,
        "salience": max(0.0, min(1.0, float(weight or 1.0) / 3.0)),
        "source_kind": "admin",
    }
    if not memory_store:
        return ""
    return memory_store.write_memory_item(item)


async def remove_lorebook_entry(memory_store: Any, topic: str) -> bool:
    if not memory_store:
        return False
    memory_id = f"lore_{_safe_id(topic)}"
    item = memory_store.get_memory_item(memory_id)
    if item is None:
        return False
    memory_store.report_memory_feedback(memory_id=memory_id, outcome="wrong", superseded_by="")
    return True


def _safe_id(text: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(text or "").strip().lower())
    return safe.strip("_")[:60] or "unknown"


__all__ = [
    "add_lorebook_entry",
    "format_lorebook_injection",
    "list_lorebook_entries",
    "match_lorebook_triggers",
    "remove_lorebook_entry",
]
