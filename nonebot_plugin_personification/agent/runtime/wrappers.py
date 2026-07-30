from __future__ import annotations

import asyncio
import json
import re
from typing import Any, List

from ...core.reply_text_policy import normalize_visible_reply_text
from .fallbacks import _parse_json_tool_result
from .intent import _clean_user_query_text, _render_message_text


_IMAGE_B64_TOOL_RESULT_RE = re.compile(r"\[IMAGE_B64\]([A-Za-z0-9+/=\r\n]+)\[/IMAGE_B64\]")
_IMAGE_GENERATION_TOOL_NAME = "generate_image"
_IMAGE_FAILURE_DIAGNOSTIC_HINTS = (
    "empty image response",
    "raw_keys=",
    "output_items=",
    "output_types=",
    "content_types=",
    "result_keys=",
)


def _looks_like_raw_tool_dump(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if _parse_json_tool_result(raw) is not None:
        return True
    if re.search(r"https?://\S+", raw):
        return True
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if sum(1 for line in lines if re.match(r"^\d+[.)]\s+", line) or re.match(r"^[-*+]\s+", line)) >= 2:
        return True
    return False


def _render_tool_result_for_user(tool_name: str, result_text: str, query: str) -> str:
    raw = str(result_text or "").strip()
    if not raw:
        return json.dumps({"status": "no_result", "query": _clean_user_query_text(query)}, ensure_ascii=False)

    if tool_name == "collect_resources":
        return raw

    payload = _parse_json_tool_result(raw)
    if not isinstance(payload, dict):
        return raw

    results = payload.get("results", [])
    if not isinstance(results, list) or not results:
        subject = _clean_user_query_text(query) or str(payload.get("query", "") or "这个主题").strip()
        return json.dumps({"status": "no_result", "query": subject}, ensure_ascii=False)

    lines: list[str] = []
    subject = _clean_user_query_text(query) or str(payload.get("query", "") or "这个主题").strip()
    lines.append(f"先给你整理 {min(len(results), 3)} 条「{subject}」参考：")
    for index, item in enumerate(results[:3], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or item.get("full_name", "") or item.get("url", "")).strip()
        snippet = str(item.get("snippet", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        source = str(item.get("source", "") or "").strip()
        line = f"{index}. {title}"
        if source:
            line += f" [{source}]"
        lines.append(line)
        if snippet:
            lines.append(snippet[:120])
        if url:
            lines.append(url)
    return "\n".join(lines).strip()


def _is_direct_media_tool_result(tool_name: str, result_text: str) -> bool:
    return str(tool_name or "").strip() == _IMAGE_GENERATION_TOOL_NAME and bool(
        _IMAGE_B64_TOOL_RESULT_RE.search(str(result_text or ""))
    )


def _format_image_generation_failure(result_text: str) -> str:
    text = str(result_text or "").strip()
    if not text:
        return "图片生成失败：工具没有返回图片数据"
    detail = text
    if detail.startswith("图片生成失败"):
        detail = detail.removeprefix("图片生成失败").lstrip("：: \t\r\n")
    detail = re.sub(r"\s+", " ", detail).strip()
    lowered = detail.lower()
    if not detail:
        return "图片生成失败：工具没有返回图片数据"
    if any(hint in lowered for hint in _IMAGE_FAILURE_DIAGNOSTIC_HINTS):
        return "图片生成失败：图片服务没有返回图片数据"
    if len(detail) > 80:
        detail = detail[:77].rstrip() + "..."
    return f"图片生成失败：{detail}"


def _extract_persona_system_prompt(messages: List[dict]) -> str:
    for message in messages:
        if str(message.get("role", "") or "").strip() != "system":
            continue
        content = _render_message_text(message.get("content", ""))
        if len(content) >= 200:
            return content[:1200]
    return ""


async def _wrap_tool_result_in_persona(
    *,
    tool_caller: Any,
    rendered_tool_result: str,
    user_query_text: str,
    persona_system: str = "",
    turn_plan: Any = None,
) -> str:
    fallback_text = str(rendered_tool_result or "").strip()
    if not fallback_text:
        return ""
    length_hint = "控制在短句范围内"
    if turn_plan is not None:
        try:
            bounds = turn_plan.length_bounds
            length_hint = f"控制在 {bounds[0]}-{bounds[1]} 字以内"
        except Exception:
            pass
    wrap_messages: list[dict[str, Any]] = []
    if persona_system:
        wrap_messages.append({"role": "system", "content": persona_system[:1200]})
    wrap_messages.append(
        {
            "role": "system",
            "content": (
                "把下面的搜索/工具结果用你自己的口吻自然说给对方。"
                "像群友顺手接话，不要暴露搜索、查询、工具、来源、链接这些中间过程。"
                "不要列 URL，不要说“根据搜索结果”“我查了一下”。"
                "最终只输出纯文本，不要 markdown、标题、项目符号列表或编号列表。"
                f"{length_hint}。"
            ),
        }
    )
    wrap_messages.append(
        {
            "role": "user",
            "content": f"查询：{str(user_query_text or '').strip()[:200]}\n工具结果：{fallback_text[:1000]}",
        }
    )
    try:
        response = await asyncio.wait_for(
            tool_caller.chat_with_tools(
                wrap_messages,
                [],
                False,
            ),
            timeout=10.0,
        )
    except Exception:
        cleaned = normalize_visible_reply_text(fallback_text)
        if _looks_like_raw_tool_dump(fallback_text):
            return "这块我没拿到稳的结果，先别按我说死。"
        return cleaned
    wrapped_text = str(getattr(response, "content", "") or "").strip()
    if wrapped_text:
        return normalize_visible_reply_text(wrapped_text)
    cleaned = normalize_visible_reply_text(fallback_text)
    if _looks_like_raw_tool_dump(fallback_text):
        return "这块我没拿到稳的结果，先别按我说死。"
    return cleaned


__all__ = [
    "_IMAGE_B64_TOOL_RESULT_RE",
    "_IMAGE_GENERATION_TOOL_NAME",
    "_extract_persona_system_prompt",
    "_format_image_generation_failure",
    "_is_direct_media_tool_result",
    "_render_tool_result_for_user",
    "_wrap_tool_result_in_persona",
]
