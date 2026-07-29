from __future__ import annotations

import asyncio
import json
import re
from typing import Any, List

from ...core.reply_text_policy import normalize_visible_reply_text
from .fallbacks import TOOL_RESULT_USABLE_EVIDENCE, _parse_json_tool_result, _tool_result_outcome
from .intent import _clean_user_query_text, _render_message_text


_IMAGE_B64_TOOL_RESULT_RE = re.compile(r"\[IMAGE_B64\]([A-Za-z0-9+/=\r\n]+)\[/IMAGE_B64\]")
_IMAGE_GENERATION_TOOL_NAME = "generate_image"


def _render_tool_result_for_user(tool_name: str, result_text: str, query: str) -> str:
    raw = str(result_text or "").strip()
    if not raw:
        return json.dumps({"status": "no_result", "query": _clean_user_query_text(query)}, ensure_ascii=False)

    if tool_name == "collect_resources":
        return raw

    payload = _parse_json_tool_result(raw)
    if not isinstance(payload, dict):
        return raw

    if "results" not in payload:
        if _tool_result_outcome(raw) == TOOL_RESULT_USABLE_EVIDENCE:
            return raw
        subject = _clean_user_query_text(query) or str(payload.get("query", "") or "这个主题").strip()
        return json.dumps({"status": "no_result", "query": subject}, ensure_ascii=False)
    results = payload.get("results", [])
    if not isinstance(results, list):
        subject = _clean_user_query_text(query) or str(payload.get("query", "") or "这个主题").strip()
        return json.dumps({"status": "no_result", "query": subject}, ensure_ascii=False)
    if not results:
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


def _extract_persona_system_prompt(messages: List[dict]) -> str:
    for message in messages:
        if str(message.get("role", "") or "").strip() != "system":
            continue
        content = _render_message_text(message.get("content", ""))
        if len(content) >= 200:
            return content
    return ""


async def _wrap_tool_result_in_persona(
    *,
    tool_caller: Any,
    rendered_tool_result: str,
    user_query_text: str,
    persona_system: str = "",
    turn_plan: Any = None,
    evidence_unavailable: bool = False,
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
        wrap_messages.append({"role": "system", "content": persona_system})
    wrap_messages.append(
        {
            "role": "system",
            "content": (
                "把下面的搜索/工具结果用你自己的口吻自然说给对方。"
                "像群友顺手接话，不要暴露搜索、查询、工具、来源、链接这些中间过程。"
                "不要列 URL，不要说“根据搜索结果”“我查了一下”。"
                + (
                    "当前结构化结果表示没有足够可用证据：保持这个事实边界，不要编造内容；"
                    "仍要像当前角色和群友一样自然回应，没必要接话时可只输出 [SILENCE]，不要套客服或 AI 助手免责声明。"
                    if evidence_unavailable
                    else ""
                )
                + "头像视觉配套只能复述图像层结论，禁止改写成两位用户现实中的情侣、朋友、认识或同一人关系。"
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
    response = await asyncio.wait_for(
        tool_caller.chat_with_tools(
            wrap_messages,
            [],
            False,
        ),
        timeout=10.0,
    )
    wrapped_text = str(getattr(response, "content", "") or "").strip()
    if wrapped_text:
        return normalize_visible_reply_text(wrapped_text)
    error = RuntimeError("agent persona wrapper returned an empty response")
    error.code = "provider_invalid_response"
    error.retryable = True
    raise error


__all__ = [
    "_IMAGE_B64_TOOL_RESULT_RE",
    "_IMAGE_GENERATION_TOOL_NAME",
    "_extract_persona_system_prompt",
    "_is_direct_media_tool_result",
    "_render_tool_result_for_user",
    "_wrap_tool_result_in_persona",
]
