from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.message_parts import build_user_message_content
from ..skills.skillpacks.tool_caller.scripts.impl import ToolCaller


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
_DEFAULT_RECOMMENDED_TOOLS = ["web_search"]
_QUERY_WRAPPER_LEADING_RE = re.compile(
    r"^(?:我问的是|我想问的是|我想问下|我想问一下|我想知道|我问下|我问一下|"
    r"请问|想问下|想问一下|帮我查下|帮我查一下|帮我搜下|帮我搜一下|"
    r"查一下|搜一下|问下|问一下)\s*"
)
_QUERY_WRAPPER_TRAILING_RE = re.compile(
    r"(?:到底)?(?:算|指)?(?:是)?(?:什么(?:东西|意思|玩意儿?|来着)?|啥(?:意思|东西)?|"
    r"指什么|是谁(?:啊|来着)?|谁来着|哪来的|什么梗|什么黑话|出处(?:是)?(?:哪里)?|"
    r"来源(?:是)?(?:哪里)?|怎么回事)\s*[?？!！.。]*$"
)
_CHINESE_WEB_SLANG_HINT_RE = re.compile(
    r"(梗|黑话|外号|别称|绰号|简称|缩写|谐音|空耳|打错|错字|玩梗|出处|来源|什么意思|"
    r"什么东西|是什么|是谁|谁啊|谁来着|哪来的|怎么回事)"
)


@dataclass(slots=True)
class QueryRewriteContext:
    history_new: str = ""
    history_last: str = ""
    trigger_reason: str = ""
    images: list[str] = field(default_factory=list)
    quoted_message: str = ""


@dataclass(slots=True)
class ContextualQueryRewrite:
    primary_query: str
    query_candidates: list[str]
    context_clues: list[str]
    need_image_understanding: bool
    recommended_tools: list[str]
    search_plan: list[str] = field(default_factory=list)


def _normalize_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\r", " ")).strip()


def _compact_query_text(text: Any) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    normalized = _QUERY_WRAPPER_LEADING_RE.sub("", normalized).strip()
    normalized = _QUERY_WRAPPER_TRAILING_RE.sub("", normalized).strip()
    normalized = re.sub(r"[?？!！。]+$", "", normalized).strip()
    return normalized or _normalize_text(text)


def _normalized_anchor_text(text: Any) -> str:
    normalized = _normalize_text(text)
    normalized = re.sub(r"^\[我\]:\s*", "", normalized)
    normalized = re.sub(r"\[[^\]]+\]", " ", normalized)
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized).strip().lower()
    return normalized[:48]


def _has_anchor_overlap(left: str, right: str) -> bool:
    if len(left) < 2 or len(right) < 2:
        return False
    if left in right or right in left:
        return True
    max_span = min(6, len(left), len(right))
    for span in range(max_span, 1, -1):
        for index in range(0, len(left) - span + 1):
            if left[index : index + span] in right:
                return True
    return False


def _pick_recent_entity_anchor(
    *,
    history_new: str,
    history_last: str,
    quoted_message: str = "",
) -> str:
    latest = _normalized_anchor_text(history_last)
    if not latest:
        return ""

    candidates: list[str] = []
    if _normalize_text(quoted_message):
        candidates.append(_normalize_text(quoted_message))
    for line in reversed(str(history_new or "").splitlines()):
        cleaned = _normalize_text(line)
        if not cleaned:
            continue
        if cleaned not in candidates:
            candidates.append(cleaned)
        if len(candidates) >= 8:
            break

    for candidate in candidates:
        normalized_candidate = _normalized_anchor_text(candidate)
        if not normalized_candidate or normalized_candidate == latest:
            continue
        if _has_anchor_overlap(normalized_candidate, latest):
            return candidate
    return ""


def _dedupe_candidates(values: list[str], *, limit: int = 6) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _normalize_text(value)
        if not cleaned:
            continue
        key = _normalized_anchor_text(cleaned)
        if not key:
            key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(cleaned)
        if len(items) >= limit:
            break
    return items


def _looks_like_colloquial_query(text: Any) -> bool:
    normalized = _normalize_text(text)
    compacted = _compact_query_text(normalized)
    if not compacted:
        return False
    if _CHINESE_WEB_SLANG_HINT_RE.search(normalized):
        return True
    if compacted != normalized:
        return True
    anchor_text = _normalized_anchor_text(compacted)
    if (
        2 <= len(anchor_text) <= 10
        and " " not in compacted
        and not any(
            token in normalized
            for token in ("最新", "今天", "现在", "多少", "怎么", "为什么", "教程", "攻略", "配置", "角色", "剧情", "版本", "活动")
        )
    ):
        return True
    return False


def _build_contextual_query_candidates(
    *,
    primary_query: str,
    recent_anchor: str = "",
    quoted_message: str = "",
    topic_hint: str = "",
) -> list[str]:
    base = _compact_query_text(primary_query) or _normalize_text(primary_query)
    anchor = _compact_query_text(recent_anchor) or _normalize_text(recent_anchor)
    quoted = _compact_query_text(quoted_message) or _normalize_text(quoted_message)
    topic = _compact_query_text(topic_hint) or _normalize_text(topic_hint)
    colloquial = _looks_like_colloquial_query(primary_query)

    candidates: list[str] = []
    if base:
        candidates.append(base)

    if anchor and base and not _has_anchor_overlap(_normalized_anchor_text(anchor), _normalized_anchor_text(base)):
        candidates.append(f"{anchor} {base}")

    if quoted and base and not _has_anchor_overlap(_normalized_anchor_text(quoted), _normalized_anchor_text(base)):
        candidates.append(f"{quoted} {base}")

    if topic and base and not _has_anchor_overlap(_normalized_anchor_text(topic), _normalized_anchor_text(base)):
        candidates.append(f"{topic} {base}")

    if colloquial and base:
        candidates.extend(
            [
                f"{base} 梗",
                f"{base} 出处",
                f"{base} 来源",
                f"{base} 正式名称",
            ]
        )
        if anchor and not _has_anchor_overlap(_normalized_anchor_text(anchor), _normalized_anchor_text(base)):
            candidates.append(f"{anchor} {base} 梗")
        if topic and not _has_anchor_overlap(_normalized_anchor_text(topic), _normalized_anchor_text(base)):
            candidates.append(f"{topic} {base} 梗")

    return _dedupe_candidates(candidates, limit=6)


def _choose_primary_query(
    *,
    history_last: str,
    recent_anchor: str = "",
    quoted_message: str = "",
    topic_hint: str = "",
) -> str:
    base = _compact_query_text(history_last) or _normalize_text(history_last)
    if not base:
        return ""
    if not _looks_like_colloquial_query(history_last):
        return base
    anchor = _compact_query_text(recent_anchor) or _normalize_text(recent_anchor)
    topic = _compact_query_text(topic_hint) or _normalize_text(topic_hint)
    quoted = _compact_query_text(quoted_message) or _normalize_text(quoted_message)
    for candidate in (anchor, quoted, topic):
        if candidate and not _has_anchor_overlap(_normalized_anchor_text(candidate), _normalized_anchor_text(base)):
            return f"{candidate} {base}".strip()
    return base


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        candidates.insert(0, match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _normalize_str_list(value: Any, limit: int = 5) -> list[str]:
    items: list[str] = []
    if isinstance(value, list):
        source = value
    elif isinstance(value, str):
        source = re.split(r"[；;\n]+", value)
    else:
        source = []
    for item in source:
        normalized = _normalize_text(item)
        if normalized and normalized not in items:
            items.append(normalized)
        if len(items) >= limit:
            break
    return items


def _fallback_rewrite(
    *,
    history_new: str,
    history_last: str,
    trigger_reason: str,
    images: list[str] | None = None,
    quoted_message: str = "",
    topic_hint: str = "",
) -> ContextualQueryRewrite:
    colloquial_hint = _looks_like_colloquial_query(history_last)
    anchor = _pick_recent_entity_anchor(
        history_new=history_new,
        history_last=history_last,
        quoted_message=quoted_message,
    )
    clues = _normalize_str_list(
        [
            clue
            for clue in [anchor, quoted_message, *str(history_new or "").splitlines()]
            if _normalize_text(clue) and not str(clue).strip().startswith("[我]:")
        ],
        limit=4,
    )
    if _normalize_text(topic_hint):
        clues = _normalize_str_list([topic_hint, *clues], limit=4)
    primary_query = _choose_primary_query(
        history_last=history_last,
        recent_anchor=anchor,
        quoted_message=quoted_message,
        topic_hint=topic_hint,
    ) or (clues[0] if clues else "")
    query_candidates = _build_contextual_query_candidates(
        primary_query=primary_query,
        recent_anchor=anchor,
        quoted_message=quoted_message,
        topic_hint=topic_hint,
    )
    if colloquial_hint and primary_query:
        query_candidates = _dedupe_candidates(
            [
                *query_candidates,
                f"{primary_query} 梗",
                f"{primary_query} 出处",
                f"{primary_query} 来源",
                f"{primary_query} 正式名称",
            ],
            limit=6,
        )
    if not query_candidates:
        query_candidates = _normalize_str_list([primary_query, *clues], limit=5)
    need_image_understanding = bool(images)
    if need_image_understanding:
        recommended_tools = ["vision_analyze", "resolve_acg_entity", "web_search"]
    elif colloquial_hint:
        recommended_tools = ["resolve_acg_entity", "web_search", "wiki_lookup"]
    else:
        recommended_tools = list(_DEFAULT_RECOMMENDED_TOOLS)
    search_plan = [
        "先结合最近对话、群聊话题和引用内容确定用户真正想找的对象",
        "若像梗、黑话、外号或谐音，先补成正式名/出处/作品线索再检索",
        "如果首轮结果不稳，再依次尝试候选检索词",
    ]
    return ContextualQueryRewrite(
        primary_query=primary_query,
        query_candidates=query_candidates or ([primary_query] if primary_query else []),
        context_clues=clues,
        need_image_understanding=need_image_understanding,
        recommended_tools=recommended_tools,
        search_plan=search_plan,
    )


def _coerce_rewrite_payload(
    payload: dict[str, Any] | None,
    *,
    history_new: str,
    history_last: str,
    trigger_reason: str,
    images: list[str] | None = None,
    quoted_message: str = "",
    topic_hint: str = "",
) -> ContextualQueryRewrite:
    fallback = _fallback_rewrite(
        history_new=history_new,
        history_last=history_last,
        trigger_reason=trigger_reason,
        images=images,
        quoted_message=quoted_message,
        topic_hint=topic_hint,
    )
    if not isinstance(payload, dict):
        return fallback

    primary_query = _normalize_text(payload.get("primary_query", "")) or fallback.primary_query
    query_candidates = _normalize_str_list(payload.get("query_candidates", []), limit=6)
    if primary_query and primary_query not in query_candidates:
        query_candidates.insert(0, primary_query)
    if not query_candidates and fallback.query_candidates:
        query_candidates = list(fallback.query_candidates)

    context_clues = _normalize_str_list(payload.get("context_clues", []), limit=5) or list(fallback.context_clues)
    recommended_tools = _normalize_str_list(payload.get("recommended_tools", []), limit=6) or list(fallback.recommended_tools)
    search_plan = _normalize_str_list(payload.get("search_plan", []), limit=4) or list(fallback.search_plan)
    need_image_understanding = bool(payload.get("need_image_understanding", fallback.need_image_understanding))

    return ContextualQueryRewrite(
        primary_query=primary_query,
        query_candidates=query_candidates,
        context_clues=context_clues,
        need_image_understanding=need_image_understanding,
        recommended_tools=recommended_tools,
        search_plan=search_plan,
    )


async def contextual_query_rewriter(
    *,
    tool_caller: ToolCaller,
    history_new: str,
    history_last: str,
    trigger_reason: str,
    images: list[str] | None = None,
    quoted_message: str = "",
    topic_hint: str = "",
) -> ContextualQueryRewrite:
    recent_anchor = _pick_recent_entity_anchor(
        history_new=history_new,
        history_last=history_last,
        quoted_message=quoted_message,
    )
    prompt = (
        "你是检索意图规划器。"
        "你的任务是在调用任何联网、资源、Wiki、攻略、视觉识别类工具之前，"
        "先基于最近对话、引用内容、最新一句和图片生成一个高质量检索计划。"
        "不要直接复读最后一句口语补充，不要把无信息尾句当搜索词。"
        "如果最新一句里的称呼、别名或实体，和最近几轮刚提过的对象高度相似，优先把它当成会话内同一对象，"
        "不要擅自跳到更常见但无上下文依据的百科实体。"
        "遇到中文互联网梗、黑话、缩写、别称、外号、谐音梗、空耳、错别字时，"
        "要主动补出更正式的实体名、出处、原句或作品线索，并把它们放进 query_candidates。"
        "若图片对识别对象很关键，请把 need_image_understanding 设为 true。"
        "只输出 JSON，不要输出解释、markdown 或代码块。\n\n"
        "JSON 字段：\n"
        "{\n"
        '  "primary_query": "最适合作为首轮检索的查询",\n'
        '  "query_candidates": ["首轮失败后可依次尝试的候选查询"],\n'
        '  "context_clues": ["影响检索的上下文线索"],\n'
        '  "need_image_understanding": true,\n'
        '  "recommended_tools": ["按优先级排序的工具名，如 vision_analyze/resolve_acg_entity/collect_resources/wiki_lookup/web_search/search_images"],\n'
        '  "search_plan": ["1-3 步中文短句，说明应该怎样检索"]\n'
        "}"
    )
    user_text = (
        f"最近对话：\n{str(history_new or '').strip() or '(无最近消息)'}\n\n"
        f"当前最新一句：\n{str(history_last or '').strip() or '(空)'}\n\n"
        f"最近会话锚点：\n{recent_anchor or '(无)'}\n\n"
        f"群聊近期话题：\n{str(topic_hint or '').strip() or '(无)'}\n\n"
        f"引用内容：\n{str(quoted_message or '').strip() or '(无)'}\n\n"
        f"触发原因：\n{str(trigger_reason or '').strip() or '(无)'}\n\n"
        f"当前是否带图：{'是' if images else '否'}"
    )
    message_content = build_user_message_content(text=user_text, image_urls=list(images or []))
    try:
        response = await tool_caller.chat_with_tools(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": message_content},
            ],
            [],
            False,
        )
    except Exception:
        response = None
    payload = _parse_json_object(getattr(response, "content", "") if response is not None else "")
    return _coerce_rewrite_payload(
        payload,
        history_new=history_new,
        history_last=history_last,
        trigger_reason=trigger_reason,
        images=images,
        quoted_message=quoted_message,
        topic_hint=topic_hint,
    )
