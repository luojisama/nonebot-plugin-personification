from __future__ import annotations

import random
from typing import Any, Callable

from .meme_dictionary import query_meme_dictionary
from .meme_learning_store import MemeLearningStore


_UNDERSTANDING_STATUSES = frozenset({"manual_locked", "verified", "understand_only", "disputed", "stale"})
_ACTIVE_STATUSES = frozenset({"manual_locked", "verified"})


def _normalized(value: Any) -> str:
    return "".join(str(value or "").casefold().split())


def _game_names(value: Any) -> set[str]:
    raw = value if isinstance(value, dict) else {}
    return {
        _normalized(item)
        for item in [raw.get("canonical_name"), *list(raw.get("aliases") or [])]
        if _normalized(item)
    }


def _term_names(value: dict[str, Any]) -> set[str]:
    return {
        _normalized(item)
        for item in [value.get("term"), *list(value.get("aliases") or [])]
        if _normalized(item)
    }


def _sense_matches_context(sense: dict[str, Any], context_text: str) -> bool:
    normalized_context = _normalized(context_text)
    games = _game_names(sense.get("game_context"))
    if games and not any(name in normalized_context for name in games):
        return False
    version = _normalized(sense.get("version_context"))
    if version and version not in normalized_context:
        return False
    if games:
        return True
    return any(name in normalized_context for name in _term_names(sense))


def _public_sense(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "sense_id",
            "term",
            "meaning",
            "aliases",
            "game_context",
            "version_context",
            "usage_context",
            "safe_usage",
            "risk_level",
            "status",
            "confidence",
            "source_count",
            "platform_count",
        )
    }


def prepare_meme_turn_context(
    *,
    group_id: str,
    message_text: str,
    recent_context: str = "",
    probability: float = 0.18,
    semantic_frame: Any = None,
    rng: Callable[[], float] = random.random,
) -> dict[str, Any]:
    """Resolve dictionary senses after reply participation has already been accepted.

    The caller owns the reply/silence decision. This function only samples whether one
    low-risk, context-matched active sense may be used as a stylistic meme.
    """

    context_text = f"{message_text}\n{recent_context}".strip()[:5000]
    understanding = query_meme_dictionary(
        str(group_id or ""),
        message_text,
        top_k=8,
        context_text=recent_context,
    )
    understood_senses: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in understanding:
        for sense in list(entry.get("senses") or [])[:8]:
            sense_id = str(sense.get("sense_id") or "")
            if not sense_id or sense_id in seen or str(sense.get("status") or "") not in _UNDERSTANDING_STATUSES:
                continue
            seen.add(sense_id)
            understood_senses.append(_public_sense(sense))

    active_candidates: list[dict[str, Any]] = []
    for sense in MemeLearningStore().list_senses(limit=2000):
        if str(sense.get("scope") or "public") not in {"public", "group", "concept"}:
            continue
        if str(sense.get("group_id") or "") not in {"", str(group_id or "")}:
            continue
        if str(sense.get("status") or "") not in _ACTIVE_STATUSES:
            continue
        if str(sense.get("risk_level") or "low") != "low":
            continue
        if not _sense_matches_context(sense, context_text):
            continue
        active_candidates.append(_public_sense(sense))
    active_candidates.sort(
        key=lambda item: (
            int(item.get("status") == "manual_locked"),
            float(item.get("confidence") or 0),
            int(item.get("source_count") or 0),
        ),
        reverse=True,
    )

    chance = max(0.0, min(1.0, float(probability or 0.0)))
    active_use_allowed = bool(active_candidates) and chance > 0.0 and float(rng()) < chance
    selected = active_candidates[0] if active_use_allowed else None
    result = {
        "schema_version": 1,
        "understanding_senses": understood_senses[:8],
        "active_use_allowed": active_use_allowed,
        "selected_active_sense": selected,
        "probability": chance,
        "max_active_memes": 1,
    }
    attach_meme_turn_context(semantic_frame, result)
    return result


def attach_meme_turn_context(semantic_frame: Any, context: dict[str, Any]) -> None:
    if semantic_frame is None:
        return
    for target in (semantic_frame, getattr(semantic_frame, "turn_plan", None)):
        if target is None:
            continue
        try:
            setattr(target, "meme_turn_context", context)
        except Exception:
            continue


def format_meme_turn_prompt(context: Any) -> str:
    if not isinstance(context, dict):
        return ""
    understood = [item for item in list(context.get("understanding_senses") or []) if isinstance(item, dict)][:8]
    selected = context.get("selected_active_sense") if isinstance(context.get("selected_active_sense"), dict) else None
    if not understood and selected is None:
        return ""
    lines = ["## 本轮多义黑话上下文（结构化词典，必须按游戏/版本语境选择）"]
    for sense in understood:
        game = sense.get("game_context") if isinstance(sense.get("game_context"), dict) else {}
        game_name = str(game.get("canonical_name") or "通用")
        lines.append(
            f"- {sense.get('term', '')}: {sense.get('meaning', '')} "
            f"（status={sense.get('status', '')}, game={game_name}, version={sense.get('version_context') or '未限定'}, "
            f"risk={sense.get('risk_level', 'low')}；{sense.get('safe_usage') or '仅按当前匹配语境理解'}）"
        )
    lines.append("understand_only 只可用于理解或被问时解释；disputed/stale 只能作为不确定背景；不得把游戏义套到真人、影视或其它游戏。")
    if selected is None:
        lines.append("本轮未通过主动玩梗抽样：不得为了风格主动塞入上述黑话；正常解释用户正在问的词义不受此限制。")
    else:
        game = selected.get("game_context") if isinstance(selected.get("game_context"), dict) else {}
        lines.append(
            "本轮已通过主动玩梗抽样，最多自然带一个梗，不解释笑点、不堆梗。唯一可主动使用的 sense："
            f"{selected.get('term', '')}={selected.get('meaning', '')}，game={game.get('canonical_name') or '通用'}，"
            f"safe_usage={selected.get('safe_usage') or '仅按当前匹配语境使用'}。"
        )
    return "\n".join(lines)


__all__ = [
    "attach_meme_turn_context",
    "format_meme_turn_prompt",
    "prepare_meme_turn_context",
]
