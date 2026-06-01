from __future__ import annotations

import json
import re
from typing import Any


_GROUP_KNOWLEDGE_PROMPT = (
    "你是群聊知识抽取器。下面是一段群聊对话摘要。"
    "请从中抽取 3-10 个高频话题、专有名词、群内绰号、内部梗或概念锚点。"
    "每个条目一句话说明含义（20字内）。只输出 JSON 数组，不要 markdown。"
    "is_meme=true 表示这是梗/戏称/口头禅；scope=concept 表示这是群内抽象说法对应的具体含义。"
    "\n格式：[{\"term\":\"专有名词\",\"definition\":\"一句话说明\",\"aliases\":[\"别名\"],"
    "\"is_meme\":false,\"scope\":\"group|concept\",\"tone\":[\"吐槽\"],\"risk_level\":\"low|medium|high\","
    "\"safe_usage\":\"使用边界\"}]"
)


async def build_group_knowledge(
    *,
    tool_caller: Any,
    memory_store: Any,
    group_id: str,
    chat_summary: str,
) -> int:
    if not tool_caller or not memory_store or not chat_summary:
        return 0
    token = None
    try:
        from .llm_context import reset_llm_context, set_llm_context

        token = set_llm_context(purpose="group_knowledge", group_id=str(group_id or ""))
    except Exception:
        token = None
    try:
        from .safety_filter import SafetyRefusalError, sanitize_or_retry
        from .token_ledger import record_response_usage

        async def _first() -> Any:
            return await tool_caller.chat_with_tools(
                messages=[
                    {"role": "system", "content": _GROUP_KNOWLEDGE_PROMPT},
                    {"role": "user", "content": str(chat_summary)[:3000]},
                ],
                tools=[],
                use_builtin_search=False,
            )

        async def _retry() -> Any:
            return await tool_caller.chat_with_tools(
                messages=[
                    {
                        "role": "system",
                        "content": _GROUP_KNOWLEDGE_PROMPT
                        + "\n注意：请只输出 JSON 数组，不要任何拒绝模板。",
                    },
                    {"role": "user", "content": str(chat_summary)[:3000]},
                ],
                tools=[],
                use_builtin_search=False,
            )

        try:
            response = await sanitize_or_retry(
                call=_first,
                retry_call=_retry,
                on_response=record_response_usage,
                purpose="group_knowledge",
            )
        except SafetyRefusalError:
            return 0
    except Exception:
        return 0
    finally:
        if token is not None:
            try:
                reset_llm_context(token)
            except Exception:
                pass

    content = str(getattr(response, "content", "") or "").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", content)
        if not match:
            return 0
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return 0
    if not isinstance(data, list):
        return 0

    saved = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "") or "").strip()
        definition = str(item.get("definition", "") or "").strip()
        if not term or len(term) < 2:
            continue
        aliases = item.get("aliases", [])
        tone = item.get("tone", [])
        risk_level = str(item.get("risk_level", "low") or "low").strip().lower()
        safe_usage = str(item.get("safe_usage", "") or "").strip()
        scope = str(item.get("scope", "group") or "group").strip().lower()
        is_meme = bool(item.get("is_meme")) or scope == "concept"
        memory_id = f"gk_{_safe_id(group_id)}_{_safe_id(term)}"
        memory_store.write_memory_item({
            "memory_id": memory_id,
            "memory_type": "concept_anchor" if scope == "concept" else ("group_meme" if is_meme else "group_knowledge"),
            "summary": f"{term}: {definition}",
            "term": term,
            "definition": definition,
            "aliases": aliases if isinstance(aliases, list) else [],
            "tone": tone if isinstance(tone, list) else [],
            "risk_level": risk_level,
            "safe_usage": safe_usage,
            "is_meme": is_meme,
            "group_id": str(group_id),
            "confidence": 0.75 if is_meme else 0.7,
            "salience": 0.5,
            "source_kind": "auto_extract",
        })
        if is_meme:
            try:
                from .meme_dictionary import upsert_meme_entry

                upsert_meme_entry(
                    {
                        "term": term,
                        "aliases": aliases,
                        "meaning": definition,
                        "tone": tone,
                        "risk_level": risk_level,
                        "scope": "concept" if scope == "concept" else "group",
                        "group_id": str(group_id),
                        "confidence": 0.75,
                        "safe_usage": safe_usage,
                    }
                )
            except Exception:
                pass
        saved += 1
    return saved


def query_group_knowledge(
    memory_store: Any,
    group_id: str,
    message_text: str,
    *,
    top_k: int = 8,
) -> list[dict[str, Any]]:
    if not memory_store:
        return []
    entries = []
    for memory_type in ("group_knowledge", "group_meme", "concept_anchor"):
        entries.extend(
            memory_store.list_recent_memories(
                group_id=str(group_id),
                memory_type=memory_type,
                limit=200,
            )
        )
    matched: list[tuple[int, dict[str, Any]]] = []
    text_lower = str(message_text or "").strip().lower()
    for entry in entries:
        term = str(entry.get("term", "") or "").strip()
        aliases = entry.get("aliases", [])
        alias_list = aliases if isinstance(aliases, list) else []
        candidates = [term, *[str(item or "") for item in alias_list]]
        hit_len = max((len(candidate) for candidate in candidates if candidate and candidate.lower() in text_lower), default=0)
        if hit_len > 0:
            summary = str(entry.get("summary", "") or "").strip()
            matched.append((hit_len, {"term": term, "summary": summary, "memory_type": entry.get("memory_type", "")}))
    matched.sort(key=lambda item: item[0], reverse=True)
    results = [item[1] for item in matched[:top_k]]
    try:
        from .meme_dictionary import query_meme_dictionary

        for meme in query_meme_dictionary(str(group_id), message_text, top_k=top_k):
            results.append(
                {
                    "term": meme.get("term", ""),
                    "summary": f"{meme.get('term', '')}: {meme.get('meaning', '')}",
                    "memory_type": "meme_dictionary",
                    "meme": meme,
                }
            )
    except Exception:
        pass
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in results:
        term = str(item.get("term", "") or "")
        if term in seen:
            continue
        seen.add(term)
        deduped.append(item)
    return deduped[:top_k]


def format_group_knowledge_hint(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = ["群组已知专有名词/话题："]
    meme_entries: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry.get("meme"), dict):
            meme_entries.append(entry["meme"])
            continue
        if str(entry.get("memory_type", "") or "") in {"group_meme", "concept_anchor"}:
            summary = str(entry.get("summary", "") or "")
            term = str(entry.get("term", "") or "")
            meaning = summary.split(":", 1)[1].strip() if ":" in summary else summary
            meme_entries.append(
                {
                    "term": term,
                    "meaning": meaning,
                    "scope": "concept" if entry.get("memory_type") == "concept_anchor" else "group",
                    "confidence": 0.75,
                    "risk_level": "low",
                }
            )
            continue
        term = entry.get("term", "")
        summary = entry.get("summary", "")
        if term and summary:
            lines.append(f"- {term}: {summary}")
    if meme_entries:
        try:
            from .meme_dictionary import format_meme_hint

            meme_hint = format_meme_hint(meme_entries)
            if meme_hint:
                lines.append(meme_hint)
        except Exception:
            pass
    if len(lines) <= 1:
        return ""
    lines.append("上面这些是群友常聊的，你已知晓。遇到这些词不要再触发 web_search/lookup_web。")
    return "\n".join(lines)


def _safe_id(text: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(text or "").strip().lower())
    return safe.strip("_")[:40] or "unknown"


__all__ = [
    "build_group_knowledge",
    "format_group_knowledge_hint",
    "query_group_knowledge",
]
