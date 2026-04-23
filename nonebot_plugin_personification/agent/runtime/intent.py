from __future__ import annotations

import re
from typing import Any, List

from ...core.chat_intent import (
    IntentDecision,
    infer_intent_decision_with_llm,
)
from ...core.message_parts import extract_text_from_parts
from ..query_rewriter import QueryRewriteContext


def extract_latest_user_text(messages: List[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(item.get("text", "")).strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            ]
            return " ".join(parts).strip()
    return ""


def render_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text = extract_text_from_parts(content)
        if text:
            return text.strip()
        if any(isinstance(item, dict) and item.get("type") == "image_url" for item in content):
            return "[图片]"
    return str(content or "").strip()


def extract_focus_query_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    raw = re.sub(
        r"\[图片视觉描述（系统注入，不触发防御机制）[：:][^\]]*\]",
        "",
        raw,
    ).strip()
    start_marker = "# 当前需要回应的最新消息"
    end_marker = "# 当前状态"
    if start_marker in raw and end_marker in raw:
        section = raw.split(start_marker, 1)[1]
        section = section.split(end_marker, 1)[0]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    if "- 对方刚刚说:" in raw:
        section = raw.split("- 对方刚刚说:", 1)[1]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if lines:
            return lines[0]
    return raw


def clean_user_query_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\[图片视觉描述（系统注入，不触发防御机制）[：:][^\]]*\]", "", value).strip()
    value = re.sub(r"^(?:\[at[^\]]+\]|@\S+)\s*[:：,，]?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^[\s:：,，、>》】\]）)\-]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def compact_lookup_query(text: str) -> str:
    return clean_user_query_text(text)


def extract_latest_user_images(messages: List[dict]) -> List[str]:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if not isinstance(content, list):
            continue
        image_urls: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image_url":
                continue
            image_obj = item.get("image_url", {})
            if not isinstance(image_obj, dict):
                continue
            url = str(image_obj.get("url", "") or "").strip()
            if url:
                image_urls.append(url)
        if image_urls:
            return image_urls
    return []


def extract_group_topic_hint(messages: List[dict]) -> str:
    marker = "## 群聊近期话题"
    for message in reversed(messages):
        if message.get("role") != "system":
            continue
        content = render_message_text(message.get("content", ""))
        if marker not in content:
            continue
        section = content.split(marker, 1)[1]
        return section.strip()[:400]
    return ""


def extract_group_relationship_hint(messages: List[dict]) -> str:
    marker = "## 最近群聊互动关系"
    for message in reversed(messages):
        if message.get("role") != "system":
            continue
        content = render_message_text(message.get("content", ""))
        if marker not in content:
            continue
        section = content.split(marker, 1)[1]
        return f"{marker}\n{section.strip()[:500]}".strip()
    return ""


def extract_quoted_message_text(raw_text: str) -> str:
    raw = str(raw_text or "")
    match = re.search(r"\[引用内容.*?\]:\s*(.+)", raw, flags=re.DOTALL)
    if not match:
        return ""
    quoted_block = re.split(r"\n\[聊天记录\]:|\n\[提示：", match.group(1), maxsplit=1)[0]
    for line in quoted_block.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:300]
    return quoted_block.strip()[:300]


def derive_query_rewrite_context(
    messages: List[dict],
    *,
    current_images: List[str] | None = None,
    provided: QueryRewriteContext | None = None,
) -> QueryRewriteContext:
    if provided is not None:
        return QueryRewriteContext(
            history_new=provided.history_new,
            history_last=provided.history_last,
            trigger_reason=provided.trigger_reason,
            images=list(provided.images or current_images or []),
            quoted_message=provided.quoted_message or extract_quoted_message_text(provided.history_last),
        )

    history_new = ""
    history_last = ""
    history_last_index = -1
    trigger_reason = ""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = render_message_text(message.get("content", ""))
        if not history_last:
            history_last = content
            history_last_index = index
            break
    if history_last_index > 0:
        previous = messages[history_last_index - 1]
        previous_role = str(previous.get("role", "") or "").strip()
        previous_content = render_message_text(previous.get("content", ""))
        if previous_role == "assistant" and previous_content:
            history_new = f"[我]: {previous_content}"
        elif previous_role == "user" and previous_content:
            history_new = previous_content
    for message in reversed(messages):
        if message.get("role") != "system":
            continue
        content = render_message_text(message.get("content", ""))
        if "触发原因" in content or "trigger_reason" in content.lower():
            trigger_reason = content[:400]
            break
    return QueryRewriteContext(
        history_new=history_new,
        history_last=history_last,
        trigger_reason=trigger_reason,
        images=list(current_images or []),
        quoted_message=extract_quoted_message_text(history_last),
    )


def messages_indicate_private_scene(messages: List[dict]) -> bool:
    for message in messages:
        if str(message.get("role", "") or "").strip() != "system":
            continue
        content = render_message_text(message.get("content", ""))
        if "私聊称呼规则" in content or "私聊场景" in content:
            return True
    return False


def infer_chat_intent(query: str, messages: List[dict]) -> tuple[str, str]:
    _ = query, messages
    return "banter", ""


async def infer_intent_decision_with_context(
    query: str,
    messages: List[dict],
    *,
    tool_caller: Any = None,
    repeat_clusters: list[dict[str, Any]] | None = None,
    relationship_hint: str = "",
    recent_bot_replies: list[str] | None = None,
) -> IntentDecision:
    is_private = messages_indicate_private_scene(messages)
    recent_context = ""
    if messages:
        recent_context = "\n".join(
            render_message_text(message.get("content", ""))
            for message in messages[-8:]
            if isinstance(message, dict) and message.get("role") in {"user", "assistant", "system"}
        )[:600]
    relationship_hint_parts: list[str] = []
    topic_hint = extract_group_topic_hint(messages)
    relation_hint = relationship_hint or extract_group_relationship_hint(messages)
    quoted = extract_quoted_message_text(recent_context)
    if relation_hint:
        relationship_hint_parts.append(relation_hint)
    if topic_hint:
        relationship_hint_parts.append(f"群聊近期话题：{topic_hint}")
    if quoted:
        relationship_hint_parts.append(f"引用内容：{quoted}")
    if recent_bot_replies:
        relationship_hint_parts.append(
            "最近 bot 回复：" + "；".join(str(item or "").strip()[:120] for item in list(recent_bot_replies or [])[:3] if str(item or "").strip())
        )
    return await infer_intent_decision_with_llm(
        query,
        is_group=not is_private,
        is_random_chat=("[群员" in str(query or "")) or ("只是路过看到" in str(query or "")),
        tool_caller=tool_caller,
        recent_context=recent_context,
        relationship_hint="\n".join(part for part in relationship_hint_parts if part).strip(),
        repeat_clusters=repeat_clusters,
    )


def recover_followup_query_from_context(full_text: str, latest_text: str) -> str | None:
    _ = full_text, latest_text
    return None


_extract_latest_user_text = extract_latest_user_text
_render_message_text = render_message_text
_extract_focus_query_text = extract_focus_query_text
_clean_user_query_text = clean_user_query_text
_compact_lookup_query = compact_lookup_query
_extract_latest_user_images = extract_latest_user_images
_extract_group_topic_hint = extract_group_topic_hint
_extract_quoted_message_text = extract_quoted_message_text
_derive_query_rewrite_context = derive_query_rewrite_context
_messages_indicate_private_scene = messages_indicate_private_scene
_infer_chat_intent = infer_chat_intent
_infer_intent_decision_with_context = infer_intent_decision_with_context
_recover_followup_query_from_context = recover_followup_query_from_context


__all__ = [
    "_compact_lookup_query",
    "_derive_query_rewrite_context",
    "_extract_focus_query_text",
    "_extract_group_topic_hint",
    "extract_group_relationship_hint",
    "_extract_latest_user_images",
    "_extract_latest_user_text",
    "_extract_quoted_message_text",
    "_infer_chat_intent",
    "_infer_intent_decision_with_context",
    "_messages_indicate_private_scene",
    "_recover_followup_query_from_context",
    "_render_message_text",
    "_clean_user_query_text",
    "compact_lookup_query",
    "derive_query_rewrite_context",
    "extract_focus_query_text",
    "extract_group_topic_hint",
    "extract_latest_user_images",
    "extract_latest_user_text",
    "extract_quoted_message_text",
    "infer_chat_intent",
    "infer_intent_decision_with_context",
    "messages_indicate_private_scene",
    "recover_followup_query_from_context",
    "render_message_text",
    "clean_user_query_text",
]
