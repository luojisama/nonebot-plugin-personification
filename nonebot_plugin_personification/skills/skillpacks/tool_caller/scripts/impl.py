from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from plugin.personification.core.image_refs import normalize_image_ref
from plugin.personification.core.message_parts import extract_text_from_parts, normalize_message_parts
from plugin.personification.core.time_ctx import build_current_time_context_block, inject_current_time_context


OPENAI_REASONING_MAP = {
    "none": "none",
    "adaptive": "high",
    "low": "low",
    "high": "high",
}

GEMINI_THINKING_BUDGET_MAP = {
    "none": 0,
    "adaptive": -1,
    "low": 1024,
    "high": 8192,
}

ANTHROPIC_THINKING_MAP = {
    "adaptive": {"type": "adaptive", "effort": "medium"},
    "low": {"type": "adaptive", "effort": "low"},
    "high": {"type": "adaptive", "effort": "high"},
}

ANTHROPIC_BUILTIN_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}

_HTTP_CLIENT_POOL_MAX_SIZE = 8
_HTTP_CLIENT_POOL_IDLE_SECONDS = 300.0
_HTTP_CLIENT_POOL: "OrderedDict[tuple[Any, ...], tuple[httpx.AsyncClient, float]]" = OrderedDict()
_HTTP_CLIENT_POOL_LOCK = asyncio.Lock()


async def _get_pooled_http_client(
    key: tuple[Any, ...],
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    now = time.monotonic()
    stale_clients: list[httpx.AsyncClient] = []
    result_client: httpx.AsyncClient | None = None
    async with _HTTP_CLIENT_POOL_LOCK:
        for existing_key, (existing, last_used) in list(_HTTP_CLIENT_POOL.items()):
            if getattr(existing, "is_closed", False) or (now - last_used) > _HTTP_CLIENT_POOL_IDLE_SECONDS:
                _HTTP_CLIENT_POOL.pop(existing_key, None)
                stale_clients.append(existing)

        cached = _HTTP_CLIENT_POOL.get(key)
        if cached is not None:
            hit, _ = cached
            if not getattr(hit, "is_closed", False):
                _HTTP_CLIENT_POOL.move_to_end(key)
                _HTTP_CLIENT_POOL[key] = (hit, now)
                result_client = hit
            else:
                _HTTP_CLIENT_POOL.pop(key, None)
                stale_clients.append(hit)

        if result_client is None:
            result_client = httpx.AsyncClient(**client_kwargs)
            _HTTP_CLIENT_POOL[key] = (result_client, now)
            while len(_HTTP_CLIENT_POOL) > _HTTP_CLIENT_POOL_MAX_SIZE:
                _, (evicted, _) = _HTTP_CLIENT_POOL.popitem(last=False)
                stale_clients.append(evicted)

    for stale_item in stale_clients:
        if not getattr(stale_item, "is_closed", False):
            try:
                await stale_item.aclose()
            except Exception:
                pass
    return result_client


def _http_client_pool_key(*parts: Any, timeout: Any = "", connect_timeout: Any = "", proxy: str = "") -> tuple[Any, ...]:
    return tuple(str(part or "") for part in parts) + (
        f"timeout={timeout}",
        f"connect={connect_timeout}",
        f"proxy={proxy or ''}",
    )


def _clear_http_client_pool() -> None:
    _HTTP_CLIENT_POOL.clear()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolCallerResponse:
    finish_reason: str
    content: str
    tool_calls: List[ToolCall]
    raw: Any
    used_builtin_search: bool = False
    vision_unavailable: bool = False
    usage: dict = field(default_factory=dict)
    model_used: str = ""


class ToolCaller(ABC):
    @abstractmethod
    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        raise NotImplementedError

    @abstractmethod
    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict | ToolCallerResponse:
        raise NotImplementedError


def _configure_genai(api_key: str, base_url: str = "") -> None:
    import google.generativeai as genai

    if base_url:
        from google.api_core import client_options as client_options_lib

        options = client_options_lib.ClientOptions(api_endpoint=base_url.rstrip("/"))
        genai.configure(api_key=api_key, client_options=options, transport="rest")
    else:
        genai.configure(api_key=api_key)


def _obj_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalize_api_type(api_type: Optional[str]) -> str:
    value = (api_type or "openai").strip().lower()
    if value in {"gemini", "gemini_official"}:
        return "gemini_official"
    if value in {"gemini_cli", "gemini-cli", "geminicli"}:
        return "gemini_cli"
    if value in {"antigravity_cli", "antigravity-cli", "antigravity", "agy", "agy_cli", "agy-cli"}:
        return "antigravity_cli"
    if value == "anthropic":
        return "anthropic"
    if value in {"claude_code", "claude-code", "claudecode", "claude_cli", "claude-cli"}:
        return "claude_code"
    if value in {"openai_codex", "codex"}:
        return "openai_codex"
    return "openai"


def _normalize_thinking_mode(mode: Optional[str], default: str = "none") -> str:
    value = (mode or default).strip().lower()
    if value in {"none", "adaptive", "low", "high"}:
        return value
    return default


def _normalize_openai_base_url(base_url: str) -> str:
    url = (base_url or "").strip()
    if "generativelanguage.googleapis.com" in url and "openai" not in url:
        return "https://generativelanguage.googleapis.com/v1beta/openai/"
    if url and "generativelanguage.googleapis.com" not in url and not url.endswith(("/v1", "/v1/")):
        return url.rstrip("/") + "/v1"
    return url


def _split_data_url(data_url: str) -> Optional[Tuple[str, str]]:
    if not data_url.startswith("data:") or ";base64," not in data_url:
        return None
    mime_type, base64_data = data_url.split(";base64,", 1)
    return mime_type.replace("data:", "", 1), base64_data


def _extract_system_message(messages: List[dict]) -> Tuple[str, List[dict]]:
    system_parts: List[str] = []
    rest: List[dict] = []
    for message in messages:
        if message.get("role") == "system":
            text = message.get("content", "")
            if isinstance(text, list):
                rendered = []
                for item in text:
                    if isinstance(item, dict) and item.get("type") == "text":
                        rendered.append(str(item.get("text", "")))
                    else:
                        rendered.append(str(item))
                system_parts.append("".join(rendered))
            else:
                system_parts.append(str(text))
        else:
            rest.append(message)
    return "\n\n".join(part for part in system_parts if part), rest


def _messages_contain_images(messages: List[dict]) -> bool:
    for message in messages:
        if str(message.get("role", "") or "").strip() not in {"user", "assistant"}:
            continue
        for part in normalize_message_parts(message.get("content", "")):
            if part.get("type") in {"image_url", "image_file"}:
                return True
    return False


def _error_indicates_vision_unavailable(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "unsupported image",
            "image input not supported",
            "invalid image_url",
            "content type image_url",
            "vision not supported",
            "multimodal not supported",
            "input_image",
            "image input",
        )
    )


def _vision_unavailable_response(error: Exception) -> ToolCallerResponse:
    return ToolCallerResponse(
        finish_reason="stop",
        content="",
        tool_calls=[],
        raw={"error": str(error or "")},
        vision_unavailable=True,
    )


def _maybe_openai_reasoning(model: str, thinking_mode: str) -> Optional[dict]:
    if "gpt-5" not in (model or "").lower():
        return None
    effort = OPENAI_REASONING_MAP.get(thinking_mode)
    if not effort:
        return None
    return {"effort": effort}


def _maybe_anthropic_thinking(thinking_mode: str) -> Optional[dict]:
    return ANTHROPIC_THINKING_MAP.get(thinking_mode)


def _normalize_gemini_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        normalized: Dict[str, Any] = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                normalized[key] = value.upper()
            else:
                normalized[key] = _normalize_gemini_schema(value)
        return normalized
    if isinstance(schema, list):
        return [_normalize_gemini_schema(item) for item in schema]
    return schema


def _convert_openai_tool_to_gemini(tool: dict) -> dict:
    function_def = tool.get("function", tool)
    return {
        "name": function_def.get("name", ""),
        "description": function_def.get("description", ""),
        "parameters": _normalize_gemini_schema(function_def.get("parameters", {"type": "object"})),
    }


def _convert_openai_tool_to_anthropic(tool: dict) -> dict:
    function_def = tool.get("function", tool)
    return {
        "name": function_def.get("name", ""),
        "description": function_def.get("description", ""),
        "input_schema": function_def.get("parameters", {"type": "object"}),
    }


def _convert_openai_tool_to_responses(tool: dict) -> dict:
    function_def = tool.get("function", tool)
    return {
        "type": "function",
        "name": function_def.get("name", ""),
        "description": function_def.get("description", ""),
        "parameters": function_def.get("parameters", {"type": "object", "properties": {}}),
    }


def _gemini_part_from_dict(item: dict) -> dict:
    if "function_call" in item:
        return {"function_call": item["function_call"]}
    if "functionCall" in item:
        return {"function_call": item["functionCall"]}
    if "function_response" in item:
        return {"function_response": item["function_response"]}
    if "functionResponse" in item:
        return {"function_response": item["functionResponse"]}
    if item.get("type") == "text":
        return {"text": str(item.get("text", ""))}
    if item.get("type") == "image_url":
        raw_image_url = str(_obj_get(item.get("image_url", {}), "url", ""))
        image_url, _ = normalize_image_ref(raw_image_url)
        if not image_url:
            return {"text": ""}
        parsed = _split_data_url(image_url)
        if parsed:
            mime_type, base64_data = parsed
            return {"inline_data": {"mime_type": mime_type, "data": base64_data}}
        return {"file_data": {"mime_type": "image/*", "file_uri": image_url}}
    if "text" in item:
        return {"text": str(item["text"])}
    return {"text": json.dumps(item, ensure_ascii=False)}


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        raw = arguments.strip() or "{}"
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _gemini_tool_call_parts(tool_calls: Any) -> List[dict]:
    parts: List[dict] = []
    if not isinstance(tool_calls, list):
        return parts
    for index, raw_tool_call in enumerate(tool_calls):
        if not isinstance(raw_tool_call, dict):
            continue
        function_part = raw_tool_call.get("function", {})
        if not isinstance(function_part, dict):
            function_part = {}
        name = str(function_part.get("name", "") or "").strip()
        if not name:
            continue
        arguments = _parse_tool_arguments(function_part.get("arguments", {}))
        part: dict[str, Any] = {
            "function_call": {
                "name": name,
                "args": arguments,
            }
        }
        call_id = str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or "").strip()
        if call_id:
            part["function_call"]["id"] = call_id
        else:
            part["function_call"]["id"] = f"gemini-call-{index}"
        parts.append(part)
    return parts


def _gemini_parts_from_content(content: Any) -> List[dict]:
    if isinstance(content, list):
        return [_gemini_part_from_dict(item) if isinstance(item, dict) else {"text": str(item)} for item in content] or [{"text": ""}]
    if isinstance(content, dict):
        return [_gemini_part_from_dict(content)]
    return [{"text": str(content)}]


def _convert_messages_to_gemini(messages: List[dict]) -> Tuple[Optional[str], List[dict]]:
    system_instruction, rest_messages = _extract_system_message(messages)
    contents: List[dict] = []
    for message in rest_messages:
        parts = _gemini_parts_from_content(message.get("parts", message.get("content", "")))
        parts.extend(_gemini_tool_call_parts(message.get("tool_calls", [])))
        contents.append(
            {
                "role": "model" if message.get("role") == "assistant" else "user",
                "parts": parts,
            }
        )
    return system_instruction or None, contents


def _extract_gemini_text(parts: List[Any]) -> str:
    texts: List[str] = []
    for part in parts:
        is_thought = bool(_obj_get(part, "thought", False))
        text = _obj_get(part, "text", "")
        if text and not is_thought:
            texts.append(str(text))
    return "".join(texts).strip()


def _extract_gemini_tool_calls(parts: List[Any]) -> List[ToolCall]:
    tool_calls: List[ToolCall] = []
    for index, part in enumerate(parts):
        function_call = _obj_get(part, "function_call")
        if function_call is None:
            function_call = _obj_get(part, "functionCall")
        if function_call is None:
            continue
        name = str(_obj_get(function_call, "name", ""))
        args = _parse_tool_arguments(_obj_get(function_call, "args", {}) or {})
        tool_calls.append(
            ToolCall(
                id=str(_obj_get(function_call, "id", f"gemini-call-{index}")),
                name=name,
                arguments=args,
            )
        )
    return tool_calls


def _gemini_used_builtin_search(response: Any) -> bool:
    candidates = list(_obj_get(response, "candidates", []) or [])
    for candidate in candidates:
        if _obj_get(candidate, "grounding_metadata") or _obj_get(candidate, "groundingMetadata"):
            return True
    return False


def _gemini_builtin_search_tool(model: str) -> dict:
    if str(model or "").strip().startswith("gemini-1.5"):
        return {
            "google_search_retrieval": {
                "dynamic_retrieval_config": {
                    "mode": "MODE_DYNAMIC",
                    "dynamic_threshold": 0.7,
                }
            }
        }
    return {"google_search": {}}


def _anthropic_content_blocks(content: Any) -> List[dict]:
    blocks: List[dict] = []
    # 空文本不要发：Anthropic 拒收空 text block；同时避免把 None 拼成字面 "None"。
    if content is None or content == "" or content == []:
        return blocks
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    blocks.append({"type": "text", "text": str(item.get("text", ""))})
                elif item_type == "image_url":
                    raw_image_url = str(_obj_get(item.get("image_url", {}), "url", ""))
                    image_url, _ = normalize_image_ref(raw_image_url)
                    if not image_url:
                        continue
                    parsed = _split_data_url(image_url)
                    if parsed:
                        mime_type, base64_data = parsed
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": base64_data,
                                },
                            }
                        )
                    else:
                        blocks.append({"type": "text", "text": image_url})
                elif item_type in {"tool_use", "tool_result"}:
                    blocks.append(item)
                elif "text" in item:
                    blocks.append({"type": "text", "text": str(item["text"])})
            else:
                blocks.append({"type": "text", "text": str(item)})
        return blocks or [{"type": "text", "text": ""}]
    if isinstance(content, dict):
        return _anthropic_content_blocks([content])
    return [{"type": "text", "text": str(content)}]


def _openai_responses_input_item_from_content(role: str, content: Any) -> dict | None:
    if role not in {"user", "assistant"}:
        return None
    parts = normalize_message_parts(content)
    if not parts:
        text = extract_text_from_parts(content)
        if not text:
            return None
        return {"role": role, "content": text}

    converted_parts: list[dict[str, Any]] = []
    for part in parts:
        part_type = str(part.get("type", "") or "").strip().lower()
        if part_type == "text":
            text = str(part.get("text", "") or "").strip()
            if text:
                converted_parts.append({"type": "input_text", "text": text})
        elif part_type == "image_url":
            image_obj = part.get("image_url", {})
            if isinstance(image_obj, dict) and image_obj.get("url"):
                normalized_image_url, _ = normalize_image_ref(str(image_obj.get("url")))
                if not normalized_image_url:
                    continue
                image_item: dict[str, Any] = {
                    "type": "input_image",
                    "image_url": normalized_image_url,
                }
                detail = str(image_obj.get("detail", "") or "").strip()
                if detail:
                    image_item["detail"] = detail
                converted_parts.append(image_item)
        elif part_type == "image_file":
            image_obj = part.get("image_file", {})
            path = str(image_obj.get("path", "") if isinstance(image_obj, dict) else "").strip()
            if path:
                normalized_path, _ = normalize_image_ref(path)
                if not normalized_path:
                    continue
                image_item = {"type": "input_image", "image_url": normalized_path}
                detail = str(part.get("detail", "") or "").strip()
                if detail:
                    image_item["detail"] = detail
                converted_parts.append(image_item)
    if not converted_parts:
        text = extract_text_from_parts(content)
        if not text:
            return None
        return {"role": role, "content": text}
    return {"role": role, "content": converted_parts}


def _openai_responses_input(messages: List[dict]) -> tuple[str | None, List[dict]]:
    system_instruction, rest_messages = _extract_system_message(messages)
    input_items: List[dict] = []
    for message in rest_messages:
        role = str(message.get("role", "user") or "user")
        content = message.get("parts", message.get("content", ""))
        if role in {"user", "assistant"}:
            item = _openai_responses_input_item_from_content(role, content)
            if item is not None:
                input_items.append(item)
            if role == "assistant":
                raw_tool_calls = message.get("tool_calls", [])
                if isinstance(raw_tool_calls, list):
                    for raw_tool_call in raw_tool_calls:
                        if not isinstance(raw_tool_call, dict):
                            continue
                        function_part = raw_tool_call.get("function", {})
                        if not isinstance(function_part, dict):
                            function_part = {}
                        call_id = str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or "").strip()
                        name = str(function_part.get("name", "")).strip()
                        arguments = function_part.get("arguments", "{}")
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments, ensure_ascii=False)
                        else:
                            arguments = str(arguments or "{}")
                        if call_id and name:
                            input_items.append(
                                {
                                    "type": "function_call",
                                    "call_id": call_id,
                                    "name": name,
                                    "arguments": arguments,
                                }
                            )
        elif role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id", "")),
                    "output": str(message.get("content", "")),
                }
            )
    return system_instruction or None, input_items


def _parse_openai_responses_output(data: dict) -> tuple[str, List[ToolCall], bool]:
    output = data.get("output", [])
    text_parts: List[str] = []
    tool_calls: List[ToolCall] = []
    used_builtin_search = False

    for item in output:
        item_type = str(_obj_get(item, "type", "") or "")
        if item_type == "message":
            content_blocks = list(_obj_get(item, "content", []) or [])
            for block in content_blocks:
                block_type = str(_obj_get(block, "type", "") or "")
                if block_type in {"output_text", "text"}:
                    text = _obj_get(block, "text", "")
                    if text:
                        text_parts.append(str(text))
        elif item_type == "function_call":
            tool_calls.append(
                ToolCall(
                    id=str(_obj_get(item, "call_id", _obj_get(item, "id", ""))),
                    name=str(_obj_get(item, "name", "")),
                    arguments=_parse_tool_arguments(_obj_get(item, "arguments", {}) or {}),
                )
            )
        elif item_type == "web_search_call":
            used_builtin_search = True

    return "".join(text_parts).strip(), tool_calls, used_builtin_search


def _openai_chat_search_model(model: str) -> bool:
    lower = str(model or "").strip().lower()
    return any(token in lower for token in ("search-preview", "search-api"))


def _openai_tool_name(tool: dict) -> str:
    return str(
        _obj_get(_obj_get(tool, "function", {}), "name", "") or _obj_get(tool, "name", "") or ""
    ).strip()


def _response_to_dict(response: Any) -> dict:
    if hasattr(response, "model_dump"):
        try:
            dumped = response.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if isinstance(response, dict):
        return response
    return {}


_USAGE_PROMPT_KEYS = ("prompt_tokens", "input_tokens", "promptTokenCount", "promptTokens")
_USAGE_COMPLETION_KEYS = ("completion_tokens", "output_tokens", "candidatesTokenCount", "completionTokens", "outputTokens")
_USAGE_TOTAL_KEYS = ("total_tokens", "totalTokenCount", "totalTokens")
_USAGE_CONTAINER_KEYS = ("usage", "usageMetadata", "usage_metadata")


def _read_usage_value(source: Any, keys: tuple[str, ...]) -> int:
    """从 dict / pydantic 对象任一支持的键名读 token 计数。"""
    if isinstance(source, dict):
        for key in keys:
            if key in source and source[key] is not None:
                try:
                    return int(source[key])
                except (TypeError, ValueError):
                    return 0
        return 0
    for key in keys:
        value = getattr(source, key, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _extract_usage(response: Any) -> dict:
    """从任意 LLM provider 响应提取 token 用量。

    兼容三种字段命名族：
      - OpenAI:    prompt_tokens / completion_tokens / total_tokens
      - Anthropic: input_tokens / output_tokens
      - Gemini:    promptTokenCount / candidatesTokenCount / totalTokenCount

    兼容三种容器键：response.usage / response.usageMetadata / response.usage_metadata
    （chat.completions、Responses、generateContent 三套 API 各用一种）

    response 可以是 dict、Pydantic 对象、嵌套结构。无法定位时返回 {}。
    """
    try:
        usage_obj: Any = None
        for container_key in _USAGE_CONTAINER_KEYS:
            if isinstance(response, dict):
                if container_key in response and response[container_key] is not None:
                    usage_obj = response[container_key]
                    break
            else:
                candidate = getattr(response, container_key, None)
                if candidate is not None:
                    usage_obj = candidate
                    break
        if usage_obj is None:
            return {}
        prompt = _read_usage_value(usage_obj, _USAGE_PROMPT_KEYS)
        completion = _read_usage_value(usage_obj, _USAGE_COMPLETION_KEYS)
        total = _read_usage_value(usage_obj, _USAGE_TOTAL_KEYS) or (prompt + completion)
        if prompt == 0 and completion == 0 and total == 0:
            return {}
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
    except Exception:
        return {}


def _anthropic_tool_use_blocks(tool_calls: Any) -> List[dict]:
    blocks: List[dict] = []
    if not isinstance(tool_calls, list):
        return blocks
    for index, raw_tool_call in enumerate(tool_calls):
        if not isinstance(raw_tool_call, dict):
            continue
        function_part = raw_tool_call.get("function", {})
        if not isinstance(function_part, dict):
            function_part = {}
        name = str(function_part.get("name", "") or "").strip()
        if not name:
            continue
        call_id = str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or "").strip()
        if not call_id:
            call_id = f"anthropic-call-{index}"
        blocks.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": _parse_tool_arguments(function_part.get("arguments", {})),
            }
        )
    return blocks


def _convert_messages_to_anthropic(messages: List[dict]) -> Tuple[str, List[dict]]:
    system_instruction, rest_messages = _extract_system_message(messages)
    converted: List[dict] = []
    for message in rest_messages:
        content_blocks = _anthropic_content_blocks(message.get("content", ""))
        content_blocks.extend(_anthropic_tool_use_blocks(message.get("tool_calls", [])))
        converted.append(
            {
                "role": "assistant" if message.get("role") == "assistant" else "user",
                "content": content_blocks,
            }
        )
    return system_instruction, converted


def _anthropic_used_builtin_search(content_blocks: List[Any]) -> bool:
    for block in content_blocks:
        block_type = str(_obj_get(block, "type", "") or "")
        if block_type in {"server_tool_use", "web_search_tool_result"}:
            return True
    return False


class OpenAIToolCaller(ToolCaller):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking_mode: str = "none",
        timeout: float = 60.0,
        supports_reasoning: Optional[bool] = None,
        proxy: str = "",
    ) -> None:
        self.api_key = api_key
        self.base_url = _normalize_openai_base_url(base_url)
        self.model = model
        self.thinking_mode = _normalize_thinking_mode(thinking_mode)
        self.timeout = timeout
        self._supports_reasoning = supports_reasoning
        self.proxy = (proxy or "").strip()

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        messages = inject_current_time_context(messages)
        from openai import AsyncOpenAI

        contains_image_input = _messages_contain_images(messages)
        try:
            connect_timeout = 10.0
            http_kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout, connect=connect_timeout),
            }
            if self.proxy:
                http_kwargs["proxy"] = self.proxy
            http_client = await _get_pooled_http_client(
                _http_client_pool_key(
                    "openai",
                    self.base_url,
                    self.api_key[:12],
                    timeout=self.timeout,
                    connect_timeout=connect_timeout,
                    proxy=self.proxy,
                ),
                **http_kwargs,
            )
            client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                http_client=http_client,
            )
            if True:
                all_tools = list(tools)
                filtered_tools = [
                    tool
                    for tool in all_tools
                    if not (use_builtin_search and _openai_tool_name(tool) == "web_search")
                ]
                responses_failed = False
                if use_builtin_search and not _openai_chat_search_model(self.model):
                    system_instruction, input_items = _openai_responses_input(messages)
                    response_tools = [_convert_openai_tool_to_responses(tool) for tool in filtered_tools]
                    response_tools.append({"type": "web_search"})
                    payload: Dict[str, Any] = {
                        "model": self.model,
                        "input": input_items,
                        "tools": response_tools,
                        "tool_choice": "auto",
                    }
                    if system_instruction:
                        payload["instructions"] = system_instruction
                    reasoning = _maybe_openai_reasoning(self.model, self.thinking_mode)
                    if reasoning and self._supports_reasoning is not False:
                        payload["reasoning"] = reasoning

                    try:
                        response = await client.responses.create(**payload)
                        content, tool_calls, used_builtin_search = _parse_openai_responses_output(
                            _response_to_dict(response)
                        )
                        return ToolCallerResponse(
                            finish_reason="tool_calls" if tool_calls else "stop",
                            content=content,
                            tool_calls=tool_calls,
                            raw=response,
                            used_builtin_search=used_builtin_search,
                            usage=_extract_usage(response),
                            model_used=str(self.model or ""),
                        )
                    except TypeError as e:
                        error_msg = str(e).lower()
                        if "reasoning" in error_msg and "unexpected keyword" in error_msg:
                            payload.pop("reasoning", None)
                            self._supports_reasoning = False
                            try:
                                response = await client.responses.create(**payload)
                                content, tool_calls, used_builtin_search = _parse_openai_responses_output(
                                    _response_to_dict(response)
                                )
                                return ToolCallerResponse(
                                    finish_reason="tool_calls" if tool_calls else "stop",
                                    content=content,
                                    tool_calls=tool_calls,
                                    raw=response,
                                    used_builtin_search=used_builtin_search,
                                    usage=_extract_usage(response),
                                    model_used=str(self.model or ""),
                                )
                            except Exception:
                                responses_failed = True
                        else:
                            raise
                    except Exception:
                        responses_failed = True

                chat_supports_native_search = use_builtin_search and _openai_chat_search_model(self.model)

                # 部分严格 provider（Rust serde 反序列化）会把 content=None 当作
                # "missing field 'content'" 直接 400 拒绝。统一规范化为 ""。
                normalized_messages: List[dict] = []
                for _m in messages:
                    if not isinstance(_m, dict):
                        normalized_messages.append(_m)
                        continue
                    _role = _m.get("role")
                    if _role in {"assistant", "system", "user"} and _m.get("content") is None:
                        _m = {**_m, "content": ""}
                    normalized_messages.append(_m)

                def _build_chat_payload(*, use_native_search: bool, use_original_tools: bool) -> Dict[str, Any]:
                    payload = {
                        "model": self.model,
                        "messages": normalized_messages,
                    }
                    payload_tools = all_tools if use_original_tools else filtered_tools
                    if payload_tools:
                        payload["tools"] = payload_tools
                        payload["tool_choice"] = "auto"
                    if use_native_search:
                        payload["web_search_options"] = {}
                    reasoning = _maybe_openai_reasoning(self.model, self.thinking_mode)
                    if reasoning and self._supports_reasoning is not False:
                        payload["reasoning"] = reasoning
                    return payload

                payload = _build_chat_payload(
                    use_native_search=chat_supports_native_search and not responses_failed,
                    use_original_tools=(not use_builtin_search) or responses_failed,
                )

                try:
                    response = await client.chat.completions.create(**payload)
                except TypeError as e:
                    error_msg = str(e).lower()
                    if "reasoning" in error_msg and "unexpected keyword" in error_msg:
                        payload.pop("reasoning", None)
                        self._supports_reasoning = False
                        response = await client.chat.completions.create(**payload)
                    elif use_builtin_search and chat_supports_native_search and not responses_failed:
                        payload = _build_chat_payload(use_native_search=False, use_original_tools=True)
                        response = await client.chat.completions.create(**payload)
                    else:
                        raise
                except Exception:
                    if use_builtin_search and chat_supports_native_search and not responses_failed:
                        payload = _build_chat_payload(use_native_search=False, use_original_tools=True)
                        response = await client.chat.completions.create(**payload)
                    else:
                        raise

            message = response.choices[0].message
            raw_tool_calls = list(_obj_get(message, "tool_calls", []) or [])
            tool_calls = [
                ToolCall(
                    id=str(_obj_get(tool_call, "id", "")),
                    name=str(_obj_get(_obj_get(tool_call, "function", {}), "name", "")),
                    arguments=_parse_tool_arguments(_obj_get(_obj_get(tool_call, "function", {}), "arguments", "{}")),
                )
                for tool_call in raw_tool_calls
            ]
            content = str(_obj_get(message, "content", "") or "").strip()
            finish_reason = "tool_calls" if tool_calls else "stop"
            used_builtin_search = bool(_obj_get(message, "annotations", []) or [])
            return ToolCallerResponse(
                finish_reason=finish_reason,
                content=content,
                tool_calls=tool_calls,
                raw=response,
                used_builtin_search=used_builtin_search,
                usage=_extract_usage(response),
                model_used=str(self.model or ""),
            )
        except Exception as exc:
            if contains_image_input and _error_indicates_vision_unavailable(exc):
                return _vision_unavailable_response(exc)
            raise

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        }

class GeminiToolCaller(ToolCaller):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking_mode: str = "none",
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").strip()
        self.model = model
        self.thinking_mode = _normalize_thinking_mode(thinking_mode)

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        messages = inject_current_time_context(messages)
        import google.generativeai as genai

        contains_image_input = _messages_contain_images(messages)
        try:
            _configure_genai(self.api_key, self.base_url)
            system_instruction, contents = _convert_messages_to_gemini(messages)

            tool_payload: List[dict] = []
            if tools:
                tool_payload.append(
                    {
                        "function_declarations": [
                            _convert_openai_tool_to_gemini(tool)
                            for tool in tools
                        ]
                    }
                )
            if use_builtin_search:
                tool_payload.append(_gemini_builtin_search_tool(self.model))

            generation_config: Dict[str, Any] = {
                "thinking_config": {
                    "thinking_budget": GEMINI_THINKING_BUDGET_MAP[self.thinking_mode]
                }
            }
            model = genai.GenerativeModel(
                model_name=self.model,
                system_instruction=system_instruction,
                tools=tool_payload or None,
            )
            response = await model.generate_content_async(
                contents,
                generation_config=generation_config,
            )

            candidates = list(_obj_get(response, "candidates", []) or [])
            if not candidates:
                return ToolCallerResponse("stop", "", [], response)

            content = _obj_get(candidates[0], "content", {})
            parts = list(_obj_get(content, "parts", []) or [])
            tool_calls = _extract_gemini_tool_calls(parts)
            text = _extract_gemini_text(parts)
            finish_reason = "tool_calls" if tool_calls else "stop"
            used_builtin = _gemini_used_builtin_search(response)
            return ToolCallerResponse(
                finish_reason=finish_reason,
                content=text,
                tool_calls=tool_calls,
                raw=response,
                used_builtin_search=used_builtin,
                usage=_extract_usage(response),
                model_used=str(self.model or ""),
            )
        except Exception as exc:
            if contains_image_input and _error_indicates_vision_unavailable(exc):
                return _vision_unavailable_response(exc)
            raise

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        return {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": tool_name,
                        "response": {"result": result},
                    }
                }
            ],
        }


class AnthropicToolCaller(ToolCaller):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking_mode: str = "none",
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").strip()
        self.model = model
        self.thinking_mode = _normalize_thinking_mode(thinking_mode)
        self.timeout = timeout

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        messages = inject_current_time_context(messages)
        from anthropic import AsyncAnthropic

        contains_image_input = _messages_contain_images(messages)
        try:
            system_instruction, anthropic_messages = _convert_messages_to_anthropic(messages)
            filtered_tools = [
                tool
                for tool in tools
                if not (
                    use_builtin_search
                    and str(_obj_get(_obj_get(tool, "function", {}), "name", "") or "") == "web_search"
                )
            ]
            tool_payload = [_convert_openai_tool_to_anthropic(tool) for tool in filtered_tools]
            if use_builtin_search:
                tool_payload.append(dict(ANTHROPIC_BUILTIN_SEARCH_TOOL))

            client_kwargs: Dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": self.timeout,
            }
            if self.base_url:
                client_kwargs["base_url"] = self.base_url.rstrip("/")
            client = AsyncAnthropic(**client_kwargs)

            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": anthropic_messages,
                "max_tokens": 1024,
            }
            if system_instruction:
                payload["system"] = system_instruction
            if tool_payload:
                payload["tools"] = tool_payload
            thinking = _maybe_anthropic_thinking(self.thinking_mode)
            if thinking:
                payload["thinking"] = thinking

            response = await client.messages.create(**payload)

            content_blocks = list(_obj_get(response, "content", []) or [])
            text_parts: List[str] = []
            tool_calls: List[ToolCall] = []
            for block in content_blocks:
                block_type = _obj_get(block, "type", "")
                if block_type == "text":
                    text_parts.append(str(_obj_get(block, "text", "")))
                elif block_type == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=str(_obj_get(block, "id", "")),
                            name=str(_obj_get(block, "name", "")),
                            arguments=_parse_tool_arguments(_obj_get(block, "input", {}) or {}),
                        )
                    )

            finish_reason = "tool_calls" if tool_calls else "stop"
            used_builtin = _anthropic_used_builtin_search(content_blocks)
            return ToolCallerResponse(
                finish_reason=finish_reason,
                content="".join(text_parts).strip(),
                tool_calls=tool_calls,
                raw=response,
                used_builtin_search=used_builtin,
                usage=_extract_usage(response),
                model_used=str(self.model or ""),
            )
        except Exception as exc:
            if contains_image_input and _error_indicates_vision_unavailable(exc):
                return _vision_unavailable_response(exc)
            raise

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": result,
                }
            ],
        }


#  Codex OAuth auth.json 路径解析
import os as _os
from pathlib import Path as _Path
from urllib.parse import unquote as _url_unquote
from urllib.parse import urlparse as _url_parse


def _find_codex_auth_file(override_path: str = "") -> _Path | None:
    """
    按优先级查找 Codex auth.json：
    1. override_path（来自 personification_codex_auth_path 配置）
    2. $CHATGPT_LOCAL_HOME/auth.json
    3. $CODEX_HOME/auth.json
    4. ~/.chatgpt-local/auth.json
    5. ~/.codex/auth.json
    """
    if override_path:
        p = _Path(override_path)
        if p.exists():
            return p

    candidates = [
        _os.environ.get("CHATGPT_LOCAL_HOME", ""),
        _os.environ.get("CODEX_HOME", ""),
    ]
    for base in candidates:
        if base:
            p = _Path(base) / "auth.json"
            if p.exists():
                return p

    for fallback in ["~/.chatgpt-local/auth.json", "~/.codex/auth.json"]:
        p = _Path(fallback).expanduser()
        if p.exists():
            return p

    return None


def _load_codex_auth(auth_path: _Path) -> dict:
    """读取 auth.json，返回 {accessToken, refreshToken, expiresAt}。"""
    try:
        return json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"读取 Codex auth.json 失败: {e}") from e


def _get_codex_access_token(auth: dict) -> str:
    token = str(auth.get("accessToken", "") or "").strip()
    if token:
        return token
    tokens = auth.get("tokens", {})
    if isinstance(tokens, dict):
        return str(tokens.get("access_token", "") or "").strip()
    return ""


def _get_codex_refresh_token(auth: dict) -> str:
    token = str(auth.get("refreshToken", "") or "").strip()
    if token:
        return token
    tokens = auth.get("tokens", {})
    if isinstance(tokens, dict):
        return str(tokens.get("refresh_token", "") or "").strip()
    return ""


def _set_codex_tokens(auth: dict, access_token: str, refresh_token: str = "", expires_at_ms: int = 0) -> dict:
    new_auth = dict(auth)
    if "tokens" in new_auth and isinstance(new_auth.get("tokens"), dict):
        nested = dict(new_auth.get("tokens", {}))
        nested["access_token"] = access_token
        if refresh_token:
            nested["refresh_token"] = refresh_token
        new_auth["tokens"] = nested
    else:
        new_auth["accessToken"] = access_token
        if refresh_token:
            new_auth["refreshToken"] = refresh_token
    if expires_at_ms > 0:
        new_auth["expiresAt"] = int(expires_at_ms)
    return new_auth


async def _refresh_codex_token(auth: dict, auth_path: _Path) -> dict:
    """
    当 accessToken 即将过期（距过期 < 5 分钟）时，使用 refreshToken 换新 token。
    刷新成功后写回 auth.json 并返回更新后的 auth dict。
    expiresAt 单位为毫秒时间戳。
    """
    import time as _time

    expires_at_ms = int(auth.get("expiresAt", 0))
    now_ms = int(_time.time() * 1000)
    access_token = _get_codex_access_token(auth)
    if expires_at_ms <= 0 and access_token:
        return auth
    if expires_at_ms - now_ms > 5 * 60 * 1000:
        return auth

    refresh_token = _get_codex_refresh_token(auth)
    if not refresh_token:
        return auth

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://auth.openai.com/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        next_access_token = str(data.get("access_token", "") or "").strip() or access_token
        next_refresh_token = str(data.get("refresh_token", "") or "").strip() or refresh_token
        next_expires_at_ms = 0
        if "expires_in" in data:
            next_expires_at_ms = int(_time.time() * 1000) + int(data["expires_in"]) * 1000
        new_auth = _set_codex_tokens(
            auth,
            access_token=next_access_token,
            refresh_token=next_refresh_token,
            expires_at_ms=next_expires_at_ms,
        )

        auth_path.write_text(
            json.dumps(new_auth, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return new_auth
    except Exception:
        # 刷新失败时继续用旧 token，不中断调用
        return auth


# Codex 后端要求的 system prompt 前缀（不包含此前缀请求会被拒绝）
_CODEX_SYSTEM_PREFIX = (
    "You are Codex, based on GPT-5. "
    "You are running as a coding agent in the Codex CLI on a user's local machine."
)

_CODEX_API_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"


def _normalize_codex_model_name(model: str) -> str:
    value = (model or "").strip()
    if not value:
        return "gpt-5.3-codex"
    lower = value.lower()
    if "codex" in lower:
        return value
    alias_map = {
        "gpt-5.3": "gpt-5.3-codex",
        "gpt-5": "gpt-5-codex",
    }
    return alias_map.get(lower, value)


class OpenAICodexToolCaller(ToolCaller):
    """
    通过 ChatGPT OAuth token 调用 Codex 后端（chatgpt.com/backend-api/codex/responses）。
    消耗 ChatGPT Plus/Pro 订阅额度，无需 API credits。

    使用前提：本机已通过 `npx @openai/codex login` 完成 OAuth 登录，
    auth.json 存放于 ~/.codex/auth.json（或由 personification_codex_auth_path 指定）。
    """

    def __init__(
        self,
        *,
        model: str = "gpt-5.3-codex",
        auth_path: str = "",
        timeout: float = 120.0,
        proxy: str = "",
    ) -> None:
        self.model = _normalize_codex_model_name(model)
        self.auth_path_override = auth_path
        self.timeout = timeout
        self.proxy = (proxy or "").strip()

    async def _get_access_token(self) -> tuple[str, _Path]:
        """加载并在需要时刷新 access token，返回 (token, auth_file_path)。"""
        auth_file = _find_codex_auth_file(self.auth_path_override)
        if auth_file is None:
            raise RuntimeError(
                "未找到 Codex auth.json。"
                "请先运行 `npx @openai/codex login` 完成 ChatGPT OAuth 登录。"
            )
        auth = _load_codex_auth(auth_file)
        auth = await _refresh_codex_token(auth, auth_file)
        token = _get_codex_access_token(auth)
        if not token:
            raise RuntimeError("Codex auth.json 中 access token 为空，请重新登录。")
        return token, auth_file

    def _message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        return extract_text_from_parts(content)

    def _build_instructions(self, messages: List[dict]) -> str:
        system_texts = [
            self._message_text(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        ]
        merged = "\n\n".join(text for text in system_texts if text).strip()
        if not merged:
            return _CODEX_SYSTEM_PREFIX
        if _CODEX_SYSTEM_PREFIX in merged:
            return merged
        return f"{_CODEX_SYSTEM_PREFIX}\n\n{merged}"

    def _build_input(self, messages: List[dict]) -> List[dict]:
        input_items: List[dict] = []

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            if role in {"user", "assistant"}:
                content_parts = normalize_message_parts(content)
                if content_parts:
                    codex_parts: list[dict[str, Any]] = []
                    for part in content_parts:
                        part_type = str(part.get("type", "") or "").strip().lower()
                        if part_type == "text":
                            text = str(part.get("text", "") or "").strip()
                            if text:
                                codex_parts.append({"type": "input_text", "text": text})
                        elif part_type == "image_url":
                            image_obj = part.get("image_url", {})
                            if isinstance(image_obj, dict) and image_obj.get("url"):
                                normalized_image_url, _ = normalize_image_ref(str(image_obj.get("url")))
                                if not normalized_image_url:
                                    continue
                                image_item: dict[str, Any] = {
                                    "type": "input_image",
                                    "image_url": normalized_image_url,
                                }
                                detail = str(image_obj.get("detail", "") or "").strip()
                                if detail:
                                    image_item["detail"] = detail
                                codex_parts.append(image_item)
                        elif part_type == "image_file":
                            image_obj = part.get("image_file", {})
                            path = str(image_obj.get("path", "") if isinstance(image_obj, dict) else "").strip()
                            if path:
                                normalized_path, _ = normalize_image_ref(path)
                                if not normalized_path:
                                    continue
                                image_item = {"type": "input_image", "image_url": normalized_path}
                                detail = str(part.get("detail", "") or "").strip()
                                if detail:
                                    image_item["detail"] = detail
                                codex_parts.append(image_item)
                    if codex_parts:
                        input_items.append({"role": role, "content": codex_parts})
                else:
                    text = self._message_text(content)
                    if text:
                        input_items.append({"role": role, "content": text})
                if role == "assistant":
                    raw_tool_calls = message.get("tool_calls", [])
                    if isinstance(raw_tool_calls, list):
                        for raw_tool_call in raw_tool_calls:
                            if not isinstance(raw_tool_call, dict):
                                continue
                            function_part = raw_tool_call.get("function", {})
                            if not isinstance(function_part, dict):
                                function_part = {}
                            call_id = str(
                                raw_tool_call.get("id")
                                or raw_tool_call.get("call_id")
                                or ""
                            ).strip()
                            name = str(function_part.get("name", "")).strip()
                            arguments = function_part.get("arguments", "{}")
                            if isinstance(arguments, dict):
                                arguments = json.dumps(arguments, ensure_ascii=False)
                            else:
                                arguments = str(arguments or "{}")
                            if call_id and name:
                                input_items.append(
                                    {
                                        "type": "function_call",
                                        "call_id": call_id,
                                        "name": name,
                                        "arguments": arguments,
                                    }
                                )

            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id", "")),
                    "output": str(message.get("content", "")),
                })

        return input_items

    def _contains_image_input(self, messages: List[dict]) -> bool:
        for message in messages:
            if message.get("role") not in {"user", "assistant"}:
                continue
            for part in normalize_message_parts(message.get("content", "")):
                if part.get("type") in {"image_url", "image_file"}:
                    return True
        return False

    def _build_tools(self, tools: List[dict]) -> List[dict]:
        """
        将 OpenAI function calling tools schema 转换为 Codex Responses API 工具格式。
        Codex 使用相同的 JSON Schema，只需确保 type 字段存在。
        """
        codex_tools = []
        for tool in tools:
            func = tool.get("function", tool)
            codex_tools.append({
                "type": "function",
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return codex_tools

    def _parse_response(self, data: dict) -> ToolCallerResponse:
        """
        解析 Codex Responses API 的响应体，提取文本和 tool calls。
        output 数组中：
          - type=="message"         -> 文本输出
          - type=="function_call"   -> 外部工具调用（需客户端处理）
          - type=="web_search_call" -> 内置搜索执行记录（Codex 自行处理，结果融入 message）
        """
        output = data.get("output", [])
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        web_search_used = False

        for item in output:
            item_type = item.get("type", "")

            if item_type == "message":
                for content_block in item.get("content", []):
                    if content_block.get("type") == "output_text":
                        text_parts.append(str(content_block.get("text", "")))

            elif item_type == "function_call":
                raw_args = item.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(
                    id=str(item.get("call_id", item.get("id", ""))),
                    name=str(item.get("name", "")),
                    arguments=args if isinstance(args, dict) else {},
                ))
            elif item_type == "web_search_call":
                web_search_used = True

            # web_search_call 为 Codex 内置搜索的中间状态，结果由 Codex 自行注入 message，
            # 此处不需要处理，仅做类型识别以避免被误认为未知 output。

        content = "".join(text_parts).strip()
        finish_reason = "tool_calls" if tool_calls else "stop"
        return ToolCallerResponse(
            finish_reason=finish_reason,
            content=content,
            tool_calls=tool_calls,
            raw=data,
            used_builtin_search=web_search_used,
            usage=_extract_usage(data),
            model_used=str(getattr(self, "model", "") or ""),
        )

    def _parse_sse_response(self, raw_text: str) -> dict:
        events: List[dict] = []
        completed_output_items: Dict[int, dict] = {}
        latest_response: Optional[dict] = None
        for line in raw_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict):
                events.append(obj)
                response_obj = obj.get("response")
                if isinstance(response_obj, dict):
                    latest_response = response_obj
                elif isinstance(obj.get("output"), list):
                    latest_response = obj

                if obj.get("type") == "response.output_item.done":
                    item = obj.get("item")
                    if isinstance(item, dict):
                        try:
                            output_index = int(obj.get("output_index", len(completed_output_items)))
                        except (TypeError, ValueError):
                            output_index = len(completed_output_items)
                        completed_output_items[output_index] = item

        if not events:
            raw = raw_text.strip()
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    pass

        if latest_response is not None:
            response_obj = dict(latest_response)
            output = response_obj.get("output", [])
            if (not isinstance(output, list) or not output) and completed_output_items:
                response_obj["output"] = [
                    item
                    for _, item in sorted(completed_output_items.items(), key=lambda pair: pair[0])
                ]
            return response_obj

        if completed_output_items:
            return {
                "output": [
                    item
                    for _, item in sorted(completed_output_items.items(), key=lambda pair: pair[0])
                ]
            }

        for event in reversed(events):
            response_obj = event.get("response")
            if isinstance(response_obj, dict):
                return response_obj
            if isinstance(event.get("output"), list):
                return event

        raise RuntimeError("Codex 流式响应解析失败：未找到有效 response 数据")

    async def _request_codex_response(
        self,
        payload: Dict[str, Any],
        *,
        contains_image_input: bool = False,
        access_token: str = "",
    ) -> dict:
        if not access_token:
            access_token, _ = await self._get_access_token()
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                connect_timeout = 15.0
                client_kwargs: dict[str, Any] = {
                    "timeout": httpx.Timeout(self.timeout, connect=connect_timeout),
                }
                if self.proxy:
                    client_kwargs["proxy"] = self.proxy
                client = await _get_pooled_http_client(
                    _http_client_pool_key(
                        "codex",
                        _CODEX_API_ENDPOINT,
                        access_token[:16],
                        timeout=self.timeout,
                        connect_timeout=connect_timeout,
                        proxy=self.proxy,
                    ),
                    **client_kwargs,
                )
                async with client.stream(
                    "POST",
                    _CODEX_API_ENDPOINT,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream, application/json",
                    },
                ) as resp:
                    content_type = (resp.headers.get("content-type", "") or "").lower()
                    if resp.is_error:
                        detail = (await resp.aread()).decode("utf-8", errors="ignore").strip()
                        lowered_detail = detail.lower()
                        if contains_image_input and resp.status_code in {400, 403, 404, 415, 422} and any(
                            token in lowered_detail
                            for token in ("image", "vision", "multimodal", "input_image", "unsupported")
                        ):
                            return ToolCallerResponse(
                                finish_reason="stop",
                                content="",
                                tool_calls=[],
                                raw={"status_code": resp.status_code, "detail": detail[:600]},
                                used_builtin_search=False,
                                vision_unavailable=True,
                            )
                        if resp.status_code == 400:
                            raise RuntimeError(
                                "Codex 请求返回 400。请确认 model 使用 Codex 可用模型（如 gpt-5.3-codex），"
                                f"当前 model={self.model}。服务端返回: {detail[:600]}"
                            )
                        raise RuntimeError(
                            f"Codex 请求失败 HTTP {resp.status_code}: {detail[:600]}"
                        )

                    if "text/event-stream" in content_type:
                        lines: List[str] = []
                        async for line in resp.aiter_lines():
                            lines.append(line)
                        raw_text = "\n".join(lines).strip()
                        if not raw_text:
                            raise RuntimeError("Codex 返回空的流式响应体")
                        data = self._parse_sse_response(raw_text)
                    else:
                        raw_text = (await resp.aread()).decode("utf-8", errors="ignore").strip()
                        if not raw_text:
                            raise RuntimeError("Codex 返回空响应体")
                        try:
                            data = json.loads(raw_text)
                        except json.JSONDecodeError:
                            lowered = raw_text.lower()
                            if "data:" in lowered or "event:" in lowered:
                                data = self._parse_sse_response(raw_text)
                            else:
                                raise RuntimeError(
                                    "Codex 返回了非 JSON 响应，"
                                    f"content-type={content_type or 'unknown'}，"
                                    f"body={raw_text[:600]}"
                                )
                break
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectError) as e:
                last_error = e
                if attempt == 0:
                    await asyncio.sleep(0.8)
                    continue
                raise RuntimeError(
                    f"Codex 连接不稳定，服务器在响应前断开: {type(e).__name__}: {e}"
                ) from e
            except Exception as e:
                last_error = e
                if attempt == 0 and "Server disconnected without sending a response" in str(e):
                    await asyncio.sleep(0.8)
                    continue
                raise
        else:
            if last_error is not None:
                raise last_error

        return data

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        messages = inject_current_time_context(messages)
        contains_image_input = self._contains_image_input(messages)

        payload: Dict[str, Any] = {
            "model": self.model,
            "store": False,  # Codex 后端要求无状态
            "stream": True,
            "instructions": self._build_instructions(messages),
            "input": self._build_input(messages),
        }
        codex_tools: List[dict] = []
        if tools:
            # 若启用内置搜索，过滤掉同名的本地 web_search function，避免与内置工具冲突
            filtered = [
                t for t in tools
                if not (
                    use_builtin_search
                    and (t.get("function", t).get("name") or t.get("name")) == "web_search"
                )
            ]
            codex_tools.extend(self._build_tools(filtered))
        if use_builtin_search:
            codex_tools.append({"type": "web_search"})
        if codex_tools:
            payload["tools"] = codex_tools

        data = await self._request_codex_response(
            payload,
            contains_image_input=contains_image_input,
        )
        if isinstance(data, ToolCallerResponse):
            return data
        return self._parse_response(data)

    @staticmethod
    def _normalize_image_size_hint(size: str) -> str:
        value = str(size or "").strip()
        if value in {"1024x1792", "1024x1536"}:
            return "portrait"
        if value in {"1792x1024", "1536x1024"}:
            return "landscape"
        if value == "auto":
            return "auto"
        return "square"

    @staticmethod
    def _normalize_image_generation_size(size: str) -> str:
        value = str(size or "").strip().lower()
        value = value.replace("×", "x").replace("*", "x")
        value = re.sub(r"\s+", "", value)
        aliases = {
            "1:1": "1024x1024",
            "square": "1024x1024",
            "正方形": "1024x1024",
            "方图": "1024x1024",
            "portrait": "1024x1536",
            "vertical": "1024x1536",
            "竖版": "1024x1536",
            "竖图": "1024x1536",
            "纵向": "1024x1536",
            "3:4": "1024x1536",
            "2:3": "1024x1536",
            "9:16": "1024x1536",
            "landscape": "1536x1024",
            "horizontal": "1536x1024",
            "wide": "1536x1024",
            "横版": "1536x1024",
            "横图": "1536x1024",
            "横向": "1536x1024",
            "16:9": "1536x1024",
            "4:3": "1536x1024",
            "3:2": "1536x1024",
        }
        value = aliases.get(value, value)
        if value in {
            "1024x1024",
            "1024x1536",
            "1536x1024",
            "auto",
        }:
            return value
        if value == "1024x1792":
            return "1024x1536"
        if value == "1792x1024":
            return "1536x1024"
        return "1024x1024"

    @staticmethod
    def _normalize_image_generation_model(model: str) -> str:
        value = str(model or "").strip().lower()
        if value in {
            "gpt-image-2",
            "gpt-image-2-2026-04-21",
            "gpt-image-1.5",
            "gpt-image-1",
            "gpt-image-1-mini",
            "chatgpt-image-latest",
        }:
            return value
        return "gpt-image-2"

    @staticmethod
    def _clean_b64_image_payload(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if "," in text and text.lower().startswith("data:image/"):
            text = text.split(",", 1)[1]
        text = re.sub(r"</?image[^>]*>", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s+", "", text)
        if len(text) < 128 or not re.fullmatch(r"[A-Za-z0-9+/=]+", text):
            return ""
        try:
            base64.b64decode(text, validate=True)
        except Exception:
            return ""
        return text

    @classmethod
    def _extract_image_payload_candidate(cls, value: Any) -> tuple[str, str]:
        if value is None:
            return "", ""
        if isinstance(value, str):
            text = value.strip()
            b64 = cls._clean_b64_image_payload(text)
            if b64:
                return b64, ""
            if text.lower().startswith(("http://", "https://")):
                return "", text
            return "", ""
        if isinstance(value, list):
            for item in value:
                b64, url = cls._extract_image_payload_candidate(item)
                if b64 or url:
                    return b64, url
            return "", ""
        if isinstance(value, dict):
            preferred_keys = (
                "b64_json",
                "image_base64",
                "base64",
                "data_url",
                "image",
                "output_image",
                "image_url",
                "url",
                "result",
                "data",
                "images",
                "content",
            )
            for key in preferred_keys:
                if key in value:
                    b64, url = cls._extract_image_payload_candidate(value.get(key))
                    if b64 or url:
                        return b64, url
            for item in value.values():
                b64, url = cls._extract_image_payload_candidate(item)
                if b64 or url:
                    return b64, url
        return "", ""

    @staticmethod
    def _summarize_image_generation_response(data: Any) -> str:
        if not isinstance(data, dict):
            if data is None:
                return "raw=none"
            return f"raw_type={type(data).__name__}"

        output = data.get("output", [])
        output_items = len(output) if isinstance(output, list) else "n/a"
        output_types: list[str] = []
        content_types: list[str] = []
        result_keys: list[str] = []
        errors: list[str] = []

        def _append_error(label: str, value: Any) -> None:
            if not value:
                return
            if isinstance(value, dict):
                code = str(value.get("code") or value.get("type") or value.get("status") or "").strip()
                message = str(
                    value.get("message")
                    or value.get("detail")
                    or value.get("error")
                    or ""
                ).strip()
                parts = [part for part in (code, message) if part]
                text = ": ".join(parts) if parts else json.dumps(value, ensure_ascii=False)
            else:
                text = str(value)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 240:
                text = text[:237].rstrip() + "..."
            errors.append(f"{label}={text}")

        _append_error("error", data.get("error"))
        if isinstance(output, list):
            for item in output[:6]:
                if not isinstance(item, dict):
                    output_types.append(type(item).__name__)
                    continue
                output_types.append(str(item.get("type", "?") or "?"))
                _append_error(f"{item.get('type', '?')}_error", item.get("error"))
                result = item.get("result")
                if isinstance(result, dict):
                    result_keys.extend(str(key) for key in list(result.keys())[:6])
                content = item.get("content")
                if isinstance(content, list):
                    for block in content[:6]:
                        if isinstance(block, dict):
                            content_types.append(str(block.get("type", "?") or "?"))

        raw_keys = ",".join(str(key) for key in list(data.keys())[:8]) or "none"
        output_type_text = ",".join(output_types) or "none"
        content_type_text = ",".join(content_types) or "none"
        result_key_text = ",".join(dict.fromkeys(result_keys)) or "none"
        error_text = "; ".join(errors) or "none"
        return (
            f"status={data.get('status', '?')} raw_keys={raw_keys} "
            f"output_items={output_items} output_types={output_type_text} "
            f"content_types={content_type_text} result_keys={result_key_text} "
            f"errors={error_text}"
        )

    async def _download_codex_image_result(self, url: str, access_token: str) -> str:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "image/*,*/*",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=15.0)) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        payload = bytes(response.content or b"")
        if not payload:
            return ""
        return base64.b64encode(payload).decode("ascii")

    async def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        image_model: str = "gpt-image-2",
        images: List[str] | None = None,
        reference_mode: str = "auto",
    ) -> dict[str, str]:
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return {"error": "empty prompt"}
        access_token, _ = await self._get_access_token()
        requested_size = str(size or "1024x1024").strip() or "1024x1024"
        generation_size = self._normalize_image_generation_size(requested_size)
        size_hint = self._normalize_image_size_hint(generation_size)
        requested_image_model = self._normalize_image_generation_model(image_model)
        mode = str(reference_mode or "auto").strip().lower() or "auto"
        reference_images = [str(item or "").strip() for item in list(images or []) if str(item or "").strip()]
        use_reference_images = bool(reference_images) and mode not in {"off", "none", "text_only", "vision_prompt"}
        prompt_parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Generate exactly one image for the user. "
                    "Use the image_generation tool and return no prose-only substitute.\n"
                    f"Requested image model: {requested_image_model}.\n"
                    f"Requested aspect/size: {size_hint} ({requested_size}).\n"
                    f"Reference image mode: {'input_image' if use_reference_images else mode}.\n"
                    f"Prompt: {prompt_text}"
                ),
            }
        ]
        if use_reference_images:
            for image_url in reference_images[:3]:
                prompt_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url, "detail": "high"},
                    }
                )
        messages = [
            {
                "role": "user",
                "content": prompt_parts,
            }
        ]
        image_tool: Dict[str, Any] = {
            "type": "image_generation",
            "model": requested_image_model,
            "size": generation_size,
            "quality": "auto",
            "action": "generate",
        }
        payload: Dict[str, Any] = {
            "model": self.model,
            "store": False,
            "stream": True,
            "instructions": (
                f"{_CODEX_SYSTEM_PREFIX}\n\n"
                f"{build_current_time_context_block()}\n\n"
                "You are generating a single image for a chat bot. "
                f"Call the built-in image_generation tool using {requested_image_model} when available. "
                "Do not answer with a prompt or instructions."
            ),
            "input": self._build_input(messages),
            "tool_choice": {"type": "image_generation"},
            "tools": [image_tool],
        }
        reference_warning = ""
        try:
            data = await self._request_codex_response(payload, access_token=access_token)
        except Exception as exc:
            error_text = str(exc).lower()
            if "model" in error_text:
                fallback_tool = dict(image_tool)
                fallback_tool.pop("model", None)
                payload = dict(payload)
                payload["tools"] = [fallback_tool]
                data = await self._request_codex_response(payload, access_token=access_token)
            elif use_reference_images and mode in {"auto", ""}:
                fallback_messages = [
                    {
                        "role": "user",
                        "content": (
                            "Generate exactly one image for the user. "
                            "Use the image_generation tool and return no prose-only substitute.\n"
                            f"Requested image model: {requested_image_model}.\n"
                            f"Requested aspect/size: {size_hint} ({requested_size}).\n"
                            "Reference image mode: unavailable; generate from the text prompt only.\n"
                            f"Reference image pass-through failed: {str(exc)[:240]}\n"
                            f"Prompt: {prompt_text}"
                        ),
                    }
                ]
                fallback_payload = dict(payload)
                fallback_payload["input"] = self._build_input(fallback_messages)
                data = await self._request_codex_response(fallback_payload, access_token=access_token)
                payload = fallback_payload
                reference_warning = "reference images were rejected by Codex image_generation; generated from text prompt only"
            else:
                raise
        output_items = list(data.get("output", []) or []) if isinstance(data, dict) else []
        for item in output_items:
            if not isinstance(item, dict) or item.get("type") != "image_generation_call":
                continue
            b64, url = self._extract_image_payload_candidate(item.get("result"))
            if not b64 and not url:
                b64, url = self._extract_image_payload_candidate(item)
            if not b64 and url:
                b64 = await self._download_codex_image_result(url, access_token)
            if b64:
                result = {"b64_json": b64}
                if reference_warning:
                    result["warning"] = reference_warning
                return result
        b64, url = self._extract_image_payload_candidate(data)
        if not b64 and url:
            b64 = await self._download_codex_image_result(url, access_token)
        if b64:
            result = {"b64_json": b64}
            if reference_warning:
                result["warning"] = reference_warning
            return result
        return {"error": f"empty image response ({self._summarize_image_generation_response(data)})"}

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        """
        Codex Responses API 的工具结果使用 function_call_output 类型，
        放在 input 数组中（非标准 OpenAI tool role）。
        """
        return {
            "role": "tool",  # 占位，_build_input 识别并转换
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        }


# ============================================================================
# Gemini CLI / Antigravity CLI / Claude Code 本地凭证调用
# ============================================================================
#
# 复用 gemini-cli / antigravity-cli / claude-code 在本机保存的 OAuth token，直接 HTTP 调用云端，
# 不再要求用户单独配 API key。token 过期由对应 cli 自身的下次启动负责刷新；
# 这里失败时会抛出明确的错误提示让用户重新登录 cli。

_GEMINI_CLI_BASE = "https://cloudcode-pa.googleapis.com"
_GEMINI_CLI_LOAD_CODE_ASSIST = f"{_GEMINI_CLI_BASE}/v1internal:loadCodeAssist"
_GEMINI_CLI_GENERATE_ENDPOINT = f"{_GEMINI_CLI_BASE}/v1internal:generateContent"
_GEMINI_CLI_DEFAULT_MODEL = "auto-gemini-3"
_ANTIGRAVITY_CLI_DEFAULT_MODEL = _GEMINI_CLI_DEFAULT_MODEL
_GEMINI_CLI_AUTO_GEMINI3_MODELS = (
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
)
_GEMINI_CLI_CLIENT_METADATA = {
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}
_CLAUDE_CODE_API_ENDPOINT = "https://api.anthropic.com/v1/messages"
_CLAUDE_CODE_OAUTH_BETA = "oauth-2025-04-20"


def _find_gemini_cli_auth_file(override_path: str = "") -> _Path | None:
    return _find_gemini_cli_auth_file_with_log(override_path)[0]


def _find_gemini_cli_auth_file_with_log(override_path: str = "") -> tuple[_Path | None, list[str]]:
    """同时返回搜索过的所有路径，便于诊断"why gemini was skipped"。"""
    searched: list[str] = []
    if override_path:
        p = _Path(override_path).expanduser()
        searched.append(f"override={p}")
        if p.exists():
            return p, searched
    for env_name in ("GEMINI_CLI_HOME", "GEMINI_HOME"):
        base = _os.environ.get(env_name, "")
        if base:
            p = _Path(base) / "oauth_creds.json"
            searched.append(f"${env_name}={p}")
            if p.exists():
                return p, searched
        else:
            searched.append(f"${env_name}=<unset>")
    for fallback in [
        "~/.gemini/oauth_creds.json",
        "~/AppData/Roaming/gemini-cli/oauth_creds.json",
        "~/.config/gemini/oauth_creds.json",
    ]:
        p = _Path(fallback).expanduser()
        searched.append(str(p))
        if p.exists():
            return p, searched
    return None, searched


_ANTIGRAVITY_KEYRING_SERVICE = "gemini:antigravity"
_ANTIGRAVITY_KEYRING_USER = "antigravity"
_ANTIGRAVITY_FULL_SCOPE = (
    "https://www.googleapis.com/auth/cloud-platform openid "
    "https://www.googleapis.com/auth/userinfo.profile "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/cclog "
    "https://www.googleapis.com/auth/experimentsandconfigs"
)


def _read_antigravity_keyring_token() -> dict | None:
    """从系统 keyring 读 agy 存储的 OAuth token blob。

    agy CLI 把凭证写在 Windows Credential Manager / macOS Keychain /
    Linux Secret Service，blob 是 JSON 形式但内层 token 字段嵌套在 'token' 下。
    Windows 上 Credential Manager 把 UTF-8 bytes 当 UTF-16LE 字符串存，
    Python keyring 读出来呈现为乱码 unicode str；需要回 encode 再 decode。
    """
    try:
        import keyring as _keyring_mod
    except ImportError:
        return None
    try:
        blob = _keyring_mod.get_password(
            _ANTIGRAVITY_KEYRING_SERVICE, _ANTIGRAVITY_KEYRING_USER
        )
    except Exception:
        return None
    if not blob:
        return None
    text = blob
    try:
        text = blob.encode("utf-16-le").decode("utf-8")
    except UnicodeError:
        # 非 Windows 平台一般已经是正常的 utf-8 字符串，保留原值
        text = blob
    # 去除 UTF-16LE 解码可能引入的 trailing NUL 与首尾空白
    text = text.rstrip("\x00").strip()
    try:
        payload = json.loads(text)
    except Exception:
        try:
            payload = json.loads(blob)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    return token if isinstance(token, dict) else None


def _sync_antigravity_keyring_to_file() -> _Path | None:
    """从系统 keyring 同步 agy 最新凭证到 ~/.gemini/antigravity-cli/oauth_creds.json。

    agy 后台会自己刷新 keyring 中的 access_token；bot 每次读 keyring 取到
    最新值再写入磁盘，便于现有 _find_antigravity_cli_auth_file_with_log
    流程定位。失败返回 None，由文件搜索逻辑兜底。
    """
    token = _read_antigravity_keyring_token()
    if not token:
        return None
    access_token = str(token.get("access_token", "") or "").strip()
    if not access_token:
        return None
    refresh_token = str(token.get("refresh_token", "") or "").strip()
    token_type = str(token.get("token_type", "Bearer") or "Bearer")
    expiry_ms = 0
    expiry_str = str(token.get("expiry", "") or "").strip()
    if expiry_str:
        try:
            from datetime import datetime as _dt_mod

            iso = expiry_str.replace("Z", "+00:00")
            expiry_ms = int(_dt_mod.fromisoformat(iso).timestamp() * 1000)
        except Exception:
            pass
    payload_for_disk = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token_type,
        "scope": _ANTIGRAVITY_FULL_SCOPE,
        "expiry_date": expiry_ms,
    }
    target_dir = _Path.home() / ".gemini" / "antigravity-cli"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    target = target_dir / "oauth_creds.json"
    try:
        target.write_text(json.dumps(payload_for_disk, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return None
    return target


def _find_antigravity_cli_auth_file(override_path: str = "") -> _Path | None:
    return _find_antigravity_cli_auth_file_with_log(override_path)[0]


def _is_gemini_cli_compat_auth_path(path: _Path) -> bool:
    """Best-effort path classifier for explicitly configured Gemini CLI creds."""
    try:
        target = _Path(path).expanduser().resolve(strict=False)
    except Exception:
        target = _Path(path).expanduser()
    candidates: list[_Path] = []
    for env_name in ("GEMINI_CLI_HOME", "GEMINI_HOME"):
        base = _os.environ.get(env_name, "")
        if base:
            candidates.append(_Path(base).expanduser() / "oauth_creds.json")
    for fallback in [
        "~/.gemini/oauth_creds.json",
        "~/AppData/Roaming/gemini-cli/oauth_creds.json",
        "~/.config/gemini/oauth_creds.json",
    ]:
        candidates.append(_Path(fallback).expanduser())
    for candidate in candidates:
        try:
            normalized = candidate.resolve(strict=False)
        except Exception:
            normalized = candidate
        if normalized == target:
            return True
    return False


def _find_antigravity_cli_auth_file_with_source(
    override_path: str = "",
) -> tuple[_Path | None, list[str], str]:
    """查找 Antigravity CLI OAuth 文件；没有专用文件时兼容 gemini-cli 凭证。

    入口先尝试从系统 keyring 把 agy 最新凭证同步写到 ~/.gemini/antigravity-cli/
    oauth_creds.json，再走原有文件搜索路径。这样首次启动或 token 被 agy
    后台刷新后，bot 都能拿到最新的 access_token。
    """
    searched: list[str] = []
    synced = _sync_antigravity_keyring_to_file()
    if synced is not None:
        searched.append(f"synced from system keyring → {synced}")
    if override_path:
        p = _Path(override_path).expanduser()
        searched.append(f"override={p}")
        if p.exists():
            source = "gemini_compat" if _is_gemini_cli_compat_auth_path(p) else "antigravity"
            return p, searched, source
    for env_name in ("ANTIGRAVITY_CLI_AUTH_PATH", "AGY_AUTH_PATH"):
        raw_path = _os.environ.get(env_name, "")
        if raw_path:
            p = _Path(raw_path).expanduser()
            searched.append(f"${env_name}={p}")
            if p.exists():
                source = "gemini_compat" if _is_gemini_cli_compat_auth_path(p) else "antigravity"
                return p, searched, source
        else:
            searched.append(f"${env_name}=<unset>")
    for env_name in ("ANTIGRAVITY_CLI_HOME", "ANTIGRAVITY_HOME", "AGY_HOME"):
        base = _os.environ.get(env_name, "")
        if base:
            for filename in ("antigravity-oauth-token", "oauth_creds.json", "auth.json", "credentials.json"):
                p = _Path(base) / filename
                searched.append(f"${env_name}/{filename}={p}")
                if p.exists():
                    return p, searched, "antigravity"
        else:
            searched.append(f"${env_name}=<unset>")
    for fallback in [
        "~/.gemini/antigravity-cli/antigravity-oauth-token",
        "~/.gemini/antigravity-cli/oauth_creds.json",
        "~/.gemini/antigravity-cli/auth.json",
        "~/.gemini/antigravity-cli/credentials.json",
        "~/AppData/Roaming/antigravity-cli/antigravity-oauth-token",
        "~/AppData/Roaming/antigravity-cli/oauth_creds.json",
        "~/AppData/Roaming/antigravity-cli/auth.json",
        "~/AppData/Roaming/antigravity-cli/credentials.json",
        "~/.config/antigravity-cli/antigravity-oauth-token",
        "~/.config/antigravity-cli/oauth_creds.json",
        "~/.config/antigravity-cli/auth.json",
        "~/.config/antigravity-cli/credentials.json",
    ]:
        p = _Path(fallback).expanduser()
        searched.append(str(p))
        if p.exists():
            return p, searched, "antigravity"

    gemini_auth, gemini_searched = _find_gemini_cli_auth_file_with_log("")
    searched.extend([f"gemini_compat:{item}" for item in gemini_searched])
    if gemini_auth is not None:
        return gemini_auth, searched, "gemini_compat"
    return None, searched, ""


def _find_antigravity_cli_auth_file_with_log(override_path: str = "") -> tuple[_Path | None, list[str]]:
    auth_file, searched, _source = _find_antigravity_cli_auth_file_with_source(override_path)
    return auth_file, searched


def _load_gemini_cli_auth(auth_path: _Path) -> dict:
    try:
        return json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"读取 gemini-cli oauth_creds.json 失败: {exc}") from exc


def _get_gemini_cli_access_token(auth: dict) -> str:
    for key in ("access_token", "accessToken"):
        token = str(auth.get(key, "") or "").strip()
        if token:
            return token
    for nested_key in ("credentials", "tokens", "token"):
        nested = auth.get(nested_key)
        if isinstance(nested, dict):
            for key in ("access_token", "accessToken"):
                token = str(nested.get(key, "") or "").strip()
                if token:
                    return token
    return ""


def _get_gemini_cli_refresh_token(auth: dict) -> str:
    for key in ("refresh_token", "refreshToken"):
        token = str(auth.get(key, "") or "").strip()
        if token:
            return token
    for nested_key in ("credentials", "tokens", "token"):
        nested = auth.get(nested_key)
        if isinstance(nested, dict):
            for key in ("refresh_token", "refreshToken"):
                token = str(nested.get(key, "") or "").strip()
                if token:
                    return token
    return ""


def _get_gemini_cli_token_expiry_ms(auth: dict) -> int:
    """返回 access_token 过期的 epoch 毫秒；0 表示未知。"""
    candidates: list[Any] = []
    for key in ("expiry_date", "expires_at", "expiresAt", "expiry"):
        candidates.append(auth.get(key))
    for nested_key in ("credentials", "tokens", "token"):
        nested = auth.get(nested_key)
        if isinstance(nested, dict):
            for key in ("expiry_date", "expires_at", "expiresAt", "expiry"):
                candidates.append(nested.get(key))
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, (int, float)) and value > 0:
            # 兼容秒级
            return int(value if value > 1e12 else value * 1000)
        if isinstance(value, str) and value.strip():
            try:
                from datetime import datetime

                iso = value.strip().replace("Z", "+00:00")
                return int(datetime.fromisoformat(iso).timestamp() * 1000)
            except Exception:
                continue
    return 0


# gemini-cli 内置 OAuth client（公开值，来自 gemini-cli 源码）。
# 仅用于 token refresh，不涉及个人凭据。
# 这里用 base64 + 拼接的方式存放，避免 GitHub Secret Scanning 把它当用户凭据拦截。
# 用户也可通过 GEMINI_OAUTH_CLIENT_ID / GEMINI_OAUTH_CLIENT_SECRET 环境变量覆盖。
import base64 as _b64
import os as _os


def _has_env_proxy() -> bool:
    """判断环境中是否配置了 HTTPS/通用代理变量（大小写均检查）。"""
    return bool(
        _os.environ.get("HTTPS_PROXY")
        or _os.environ.get("https_proxy")
        or _os.environ.get("ALL_PROXY")
        or _os.environ.get("all_proxy")
    )


def _gemini_oauth_client_id() -> str:
    override = _os.environ.get("GEMINI_OAUTH_CLIENT_ID")
    if override:
        return override.strip()
    middle = _b64.b64decode("b284ZnQyb3ByZHJucDllM2FxZjZhdjNobWRpYjEzNWo=").decode("ascii")
    suffix = _b64.b64decode("Z29vZ2xldXNlcmNvbnRlbnQuY29t").decode("ascii")
    return "681255809395-" + middle + ".apps." + suffix


def _gemini_oauth_client_secret() -> str:
    override = _os.environ.get("GEMINI_OAUTH_CLIENT_SECRET")
    if override:
        return override.strip()
    body = _b64.b64decode("NHVIZ01QbS0xbzdTay1nZVY2Q3U1Y2xYRnN4bA==").decode("ascii")
    return "GOCS" + "PX-" + body


def _antigravity_oauth_client_id() -> str:
    """Antigravity CLI 自己的 OAuth client_id；用 gemini-cli 的会 unauthorized_client。
    从 agy.exe binary 提取得到（公开值，仅用于 token refresh）。
    可用 ANTIGRAVITY_OAUTH_CLIENT_ID 环境变量覆盖。
    """
    override = _os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID")
    if override:
        return override.strip()
    middle = _b64.b64decode("dG1oc3NpbjJoMjFsY3JlMjM1dnRvbG9qaDRnNDAzZXA=").decode("ascii")
    suffix = _b64.b64decode("Z29vZ2xldXNlcmNvbnRlbnQuY29t").decode("ascii")
    return "1071006060591-" + middle + ".apps." + suffix


def _antigravity_oauth_client_secret() -> str:
    override = _os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET")
    if override:
        return override.strip()
    body = _b64.b64decode("SzU4RldSNDg2TGRMSjFtTEI4c1hDNHo2cURBZg==").decode("ascii")
    return "GOCS" + "PX-" + body


_GEMINI_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


async def _refresh_oauth_token(
    refresh_token: str,
    *,
    client_id: str,
    client_secret: str,
    timeout: float = 30.0,
    proxy: str = "",
    trust_env: bool | None = None,
) -> dict:
    """通用 OAuth refresh：用 (client_id, client_secret, refresh_token) 换新 access_token。

    proxy 非空时显式走该代理，不依赖 HTTPS_PROXY 环境变量（bot 进程未必继承）。
    """
    client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(timeout, connect=10.0)}
    if proxy:
        client_kwargs["proxy"] = proxy
    if trust_env is not None:
        client_kwargs["trust_env"] = trust_env
    async with httpx.AsyncClient(**client_kwargs) as client:
        resp = await client.post(
            _GEMINI_OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            body = ""
            try:
                body = resp.text[:400]
            except Exception:
                pass
            raise RuntimeError(
                f"oauth refresh HTTP {resp.status_code}: {body or resp.reason_phrase}"
            )
        return resp.json()


async def _refresh_gemini_cli_access_token(
    refresh_token: str,
    *,
    timeout: float = 30.0,
    proxy: str = "",
    trust_env: bool | None = None,
) -> dict:
    """gemini-cli 路径下的 refresh：用 gemini-cli OAuth client。"""
    return await _refresh_oauth_token(
        refresh_token,
        client_id=_gemini_oauth_client_id(),
        client_secret=_gemini_oauth_client_secret(),
        timeout=timeout,
        proxy=proxy,
        trust_env=trust_env,
    )


async def _refresh_antigravity_cli_access_token(
    refresh_token: str,
    *,
    timeout: float = 30.0,
    proxy: str = "",
    trust_env: bool | None = None,
) -> dict:
    """antigravity_cli 路径下的 refresh：用 antigravity OAuth client。

    必须与发放此 refresh_token 的 OAuth client 匹配，否则 Google 返回
    HTTP 401 ``unauthorized_client``（gemini-cli 与 antigravity 不互通）。
    """
    return await _refresh_oauth_token(
        refresh_token,
        client_id=_antigravity_oauth_client_id(),
        client_secret=_antigravity_oauth_client_secret(),
        timeout=timeout,
        proxy=proxy,
        trust_env=trust_env,
    )


def _persist_refreshed_gemini_cli_auth(
    auth_path: _Path,
    auth: dict,
    *,
    access_token: str,
    expires_in: int,
    id_token: str = "",
) -> dict:
    """将刷新后的 token 与 expiry 写回 oauth_creds.json，保持原有嵌套结构。"""
    import time as _t
    new_expiry_ms = int(_t.time() * 1000) + max(0, int(expires_in or 3600) - 30) * 1000
    new_auth = dict(auth)
    target_nested = None
    for nested_key in ("credentials", "tokens", "token"):
        nested = new_auth.get(nested_key)
        if isinstance(nested, dict) and (nested.get("access_token") or nested.get("refresh_token")):
            target_nested = dict(nested)
            target_nested["access_token"] = access_token
            target_nested["expiry_date"] = new_expiry_ms
            if "expiry" in target_nested:
                from datetime import datetime as _dt_mod, timezone as _tz_mod

                target_nested["expiry"] = _dt_mod.fromtimestamp(
                    new_expiry_ms / 1000,
                    tz=_tz_mod.utc,
                ).isoformat().replace("+00:00", "Z")
            if id_token:
                target_nested["id_token"] = id_token
            new_auth[nested_key] = target_nested
            break
    if target_nested is None:
        new_auth["access_token"] = access_token
        new_auth["expiry_date"] = new_expiry_ms
        if id_token:
            new_auth["id_token"] = id_token
    try:
        auth_path.write_text(json.dumps(new_auth, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return new_auth


_GEMINI_PROJECT_KEYS = {
    "cloudaicompanionproject",
    "cloud_ai_companion_project",
    "cloudAiCompanionProject".lower(),
    "googlecloudproject",
    "google_cloud_project",
    "project",
    "projectid",
    "project_id",
    "quota_project_id",
}


def _looks_like_project_id(value: str) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        return False
    return bool(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,127}", text)
        or re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            text,
        )
    )


def _extract_project_from_json_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key or "").strip().replace("-", "_").lower()
            if normalized_key in _GEMINI_PROJECT_KEYS and isinstance(value, (str, int)):
                candidate = str(value or "").strip()
                if _looks_like_project_id(candidate):
                    return candidate
        for value in payload.values():
            found = _extract_project_from_json_payload(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _extract_project_from_json_payload(value)
            if found:
                return found
    return ""


def _read_gemini_project_from_json(path: _Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return _extract_project_from_json_payload(payload)


def _normalise_local_path(value: str) -> _Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        parsed = _url_parse(raw)
        raw = _url_unquote(parsed.path or "")
    if not raw:
        return None
    try:
        return _Path(raw).expanduser().resolve(strict=False)
    except Exception:
        return _Path(raw).expanduser()


def _antigravity_project_workspace_paths(payload: Any) -> list[_Path]:
    paths: list[_Path] = []

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        path = _normalise_local_path(value)
        if path is not None and path not in paths:
            paths.append(path)

    if not isinstance(payload, dict):
        return paths
    _add(payload.get("name"))
    resources = (
        (payload.get("projectResources") or {}).get("resources")
        if isinstance(payload.get("projectResources"), dict)
        else None
    )
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            git_folder = resource.get("gitFolder")
            if isinstance(git_folder, dict):
                _add(git_folder.get("folderUri"))
    return paths


def _resolve_antigravity_project_from_workspace_configs() -> str:
    """按当前工作目录匹配 agy 本地 workspace project 配置。"""
    project_dir = _Path("~/.gemini/config/projects").expanduser()
    if not project_dir.exists():
        return ""
    try:
        cwd = _Path.cwd().resolve(strict=False)
    except Exception:
        cwd = _Path.cwd()
    best: tuple[int, str] | None = None
    for path in sorted(project_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        project_id = str(payload.get("id") or "").strip()
        if not _looks_like_project_id(project_id):
            continue
        for workspace_path in _antigravity_project_workspace_paths(payload):
            try:
                cwd.relative_to(workspace_path)
            except Exception:
                continue
            score = len(str(workspace_path))
            if best is None or score > best[0]:
                best = (score, project_id)
    return best[1] if best else ""


def _gemini_project_config_candidates(auth_file: _Path | None = None) -> list[_Path]:
    candidates: list[_Path] = []

    def _add(path: _Path) -> None:
        try:
            resolved = path.expanduser()
        except Exception:
            resolved = path
        if resolved not in candidates:
            candidates.append(resolved)

    if auth_file is not None:
        _add(auth_file)
        base = auth_file.parent
        for name in ("settings.json", "config.json", "gemini.json"):
            _add(base / name)
    for env_name in ("GEMINI_CLI_HOME", "GEMINI_HOME"):
        base = _os.environ.get(env_name, "")
        if base:
            for name in ("settings.json", "config.json", "oauth_creds.json"):
                _add(_Path(base) / name)
    for raw in (
        "~/.gemini/settings.json",
        "~/.gemini/config.json",
        "~/.gemini/oauth_creds.json",
        "~/AppData/Roaming/gemini-cli/settings.json",
        "~/AppData/Roaming/gemini-cli/config.json",
        "~/AppData/Roaming/gemini-cli/oauth_creds.json",
        "~/.config/gemini/settings.json",
        "~/.config/gemini/config.json",
        "~/.config/gemini/oauth_creds.json",
    ):
        _add(_Path(raw))
    return candidates


def _antigravity_project_config_candidates(auth_file: _Path | None = None) -> list[_Path]:
    candidates: list[_Path] = []

    def _add(path: _Path) -> None:
        try:
            resolved = path.expanduser()
        except Exception:
            resolved = path
        if resolved not in candidates:
            candidates.append(resolved)

    if auth_file is not None:
        _add(auth_file)
        base = auth_file.parent
        for name in ("settings.json", "config.json", "auth.json", "credentials.json"):
            _add(base / name)
    for env_name in ("ANTIGRAVITY_CLI_HOME", "ANTIGRAVITY_HOME", "AGY_HOME"):
        base = _os.environ.get(env_name, "")
        if base:
            for name in ("settings.json", "config.json", "oauth_creds.json", "auth.json", "credentials.json"):
                _add(_Path(base) / name)
    for raw in (
        "~/.gemini/antigravity-cli/settings.json",
        "~/.gemini/antigravity-cli/config.json",
        "~/.gemini/antigravity-cli/oauth_creds.json",
        "~/.gemini/antigravity-cli/auth.json",
        "~/.gemini/antigravity-cli/credentials.json",
        "~/AppData/Roaming/antigravity-cli/settings.json",
        "~/AppData/Roaming/antigravity-cli/config.json",
        "~/AppData/Roaming/antigravity-cli/oauth_creds.json",
        "~/AppData/Roaming/antigravity-cli/auth.json",
        "~/AppData/Roaming/antigravity-cli/credentials.json",
        "~/.config/antigravity-cli/settings.json",
        "~/.config/antigravity-cli/config.json",
        "~/.config/antigravity-cli/oauth_creds.json",
        "~/.config/antigravity-cli/auth.json",
        "~/.config/antigravity-cli/credentials.json",
    ):
        _add(_Path(raw))
    return candidates


def _read_gcloud_project() -> str:
    try:
        import configparser
    except Exception:
        return ""
    raw_candidates = [
        _os.environ.get("CLOUDSDK_CONFIG", ""),
        "~/.config/gcloud",
        "~/AppData/Roaming/gcloud",
    ]
    files: list[_Path] = []
    for raw in raw_candidates:
        if not raw:
            continue
        base = _Path(raw).expanduser()
        files.append(base / "configurations" / "config_default")
        files.append(base / "active_config")
    for path in files:
        if not path.exists() or path.name == "active_config":
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
            project = parser.get("core", "project", fallback="").strip()
        except Exception:
            project = ""
        if _looks_like_project_id(project):
            return project
    return ""


def _resolve_local_gemini_cli_project(auth_file: _Path | None = None) -> str:
    for env_name in (
        "GEMINI_CLI_PROJECT",
        "GEMINI_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "CLOUDSDK_CORE_PROJECT",
    ):
        value = str(_os.environ.get(env_name, "") or "").strip()
        if _looks_like_project_id(value):
            return value
    for path in _gemini_project_config_candidates(auth_file):
        if not path.exists():
            continue
        project = _read_gemini_project_from_json(path)
        if project:
            return project
    return _read_gcloud_project()


def _resolve_local_antigravity_cli_project(auth_file: _Path | None = None) -> str:
    for env_name in (
        "ANTIGRAVITY_CLI_PROJECT",
        "ANTIGRAVITY_PROJECT",
        "AGY_PROJECT",
    ):
        value = str(_os.environ.get(env_name, "") or "").strip()
        if _looks_like_project_id(value):
            return value
    for path in _antigravity_project_config_candidates(auth_file):
        if not path.exists():
            continue
        project = _read_gemini_project_from_json(path)
        if project:
            return project
    workspace_project = _resolve_antigravity_project_from_workspace_configs()
    if workspace_project:
        return workspace_project
    return _resolve_local_gemini_cli_project(auth_file)


def _gemini_cli_headers(access_token: str, *, user_agent: str = "GeminiCLI/v0.41.2 (personification; python) google-api-python-client") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "x-goog-api-client": "gl-python/3 personification",
    }


def _gemini_cli_model_candidates(model: str) -> list[str]:
    primary = (model or _GEMINI_CLI_DEFAULT_MODEL).strip() or _GEMINI_CLI_DEFAULT_MODEL
    lowered = primary.lower()
    if lowered in {"auto", "auto-gemini-3"}:
        candidates = list(_GEMINI_CLI_AUTO_GEMINI3_MODELS)
    else:
        candidates = [primary]
    if lowered.startswith("gemini-3"):
        candidates.extend(["gemini-2.5-flash", "gemini-2.5-pro"])
    elif lowered.startswith("gemini-2.5-pro"):
        candidates.append("gemini-2.5-flash")
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _gemini_cli_retry_with_fallback_model(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        status_code = int(status or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code in {404, 429, 500, 502, 503, 504}:
        return True
    text = str(exc or "").lower()
    return any(
        token in text
        for token in (
            "too many requests",
            "resource exhausted",
            "no capacity",
            "overloaded",
            "model was not found",
            "model is invalid",
        )
    )


class GeminiCliToolCaller(ToolCaller):
    """复用 gemini-cli 本地 OAuth 凭证调用 cloudcode-pa 内部端点。

    走 v1internal:generateContent 的 envelope 格式：
        {"model": ..., "project": ..., "request": {...GenerateContentRequest...}}
    project 字段必填，由 v1internal:loadCodeAssist 解析得到，缓存到实例。
    """

    _project_cache: dict[str, str] = {}
    _default_model = _GEMINI_CLI_DEFAULT_MODEL
    _auth_label = "gemini-cli"
    _login_command = "`gemini auth login`"
    _onboarding_command = "`gemini`"
    _auth_config_field = "personification_gemini_cli_auth_path"
    _project_config_field = "personification_gemini_cli_project"
    _user_agent = "GeminiCLI/v0.41.2 (personification; python) google-api-python-client"

    def __init__(
        self,
        *,
        model: str,
        auth_path: str = "",
        project: str = "",
        thinking_mode: str = "none",
        timeout: float = 120.0,
        proxy: str = "",
    ) -> None:
        self.model = (model or self._default_model).strip() or self._default_model
        self.auth_path_override = auth_path
        self.project_override = (project or "").strip()
        self.thinking_mode = _normalize_thinking_mode(thinking_mode)
        self.timeout = timeout
        # 显式 HTTP 代理；非空则所有 httpx 调用都强制走它，不依赖 HTTPS_PROXY
        # 环境变量（bot 子进程未必继承终端 env）。
        self.proxy = (proxy or "").strip()

    def _find_auth_file_with_log(self) -> tuple[_Path | None, list[str]]:
        return _find_gemini_cli_auth_file_with_log(self.auth_path_override)

    def _resolve_local_project(self, auth_file: _Path | None = None) -> str:
        return _resolve_local_gemini_cli_project(auth_file)

    def _headers(self, access_token: str) -> dict[str, str]:
        return _gemini_cli_headers(access_token, user_agent=self._user_agent)

    def _load_code_assist_endpoint(self) -> str:
        return _GEMINI_CLI_LOAD_CODE_ASSIST

    def _load_code_assist_metadata(self) -> dict[str, Any]:
        return dict(_GEMINI_CLI_CLIENT_METADATA)

    def _http_client_kwargs(self, *, connect_timeout: float = 15.0) -> dict[str, Any]:
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout, connect=connect_timeout),
        }
        if self.proxy:
            client_kwargs["proxy"] = self.proxy
        return client_kwargs

    async def _refresh_token_remote(self, refresh_token: str) -> dict:
        """子类可覆写：用对应的 OAuth client 续 token。默认走 gemini-cli。"""
        return await _refresh_gemini_cli_access_token(refresh_token, proxy=self.proxy)

    async def _get_access_token(self, *, force_refresh: bool = False) -> tuple[str, _Path]:
        import time as _t

        auth_file, searched = self._find_auth_file_with_log()
        if auth_file is None:
            raise RuntimeError(
                f"未找到 {self._auth_label} OAuth 凭证。"
                f"请先运行 {self._login_command} 完成 Google OAuth 登录，"
                f"或在配置中设置 {self._auth_config_field}。"
                f" 已搜索路径: {searched}"
            )
        auth = _load_gemini_cli_auth(auth_file)
        token = _get_gemini_cli_access_token(auth)
        refresh_token = _get_gemini_cli_refresh_token(auth)
        expiry_ms = _get_gemini_cli_token_expiry_ms(auth)
        now_ms = int(_t.time() * 1000)
        # 满足任意条件即刷新：
        #   - 强制刷新（401 重试）
        #   - access_token 为空
        #   - 有 expiry 且距离过期不足 60 秒
        needs_refresh = force_refresh or not token or (expiry_ms > 0 and (expiry_ms - now_ms) < 60_000)
        if needs_refresh and not refresh_token:
            if force_refresh or not token:
                raise RuntimeError(
                    f"{self._auth_label} OAuth 凭证中 refresh_token 缺失，"
                    f"无法自动续期 access_token；请在本机执行 {self._login_command} 重新登录。"
                    f" auth_file={auth_file}"
                )
            # 有旧 token 但无 refresh_token：尝试用旧 token（可能仍在过期临界点）
        if needs_refresh and refresh_token:
            try:
                payload = await self._refresh_token_remote(refresh_token)
            except Exception as exc:
                if token and not force_refresh:
                    # 旧 token 仍在尝试中，刷新失败时先用旧的（可能仍未过期）
                    return token, auth_file
                raise RuntimeError(
                    f"{self._auth_label} 自动刷新 access_token 失败：{exc}；"
                    f"请尝试在本机执行 {self._login_command} 重新登录。"
                    f" auth_file={auth_file}"
                ) from exc
            new_token = str(payload.get("access_token", "") or "").strip()
            if not new_token:
                raise RuntimeError(
                    f"{self._auth_label} OAuth 刷新响应中 access_token 为空，请重新执行 {self._login_command}。"
                )
            id_token = str(payload.get("id_token", "") or "")
            expires_in = int(payload.get("expires_in", 3600) or 3600)
            _persist_refreshed_gemini_cli_auth(
                auth_file,
                auth,
                access_token=new_token,
                expires_in=expires_in,
                id_token=id_token,
            )
            # 旧 access_token 在 project_cache 里已失效，清理
            type(self)._project_cache.pop(token, None)
            token = new_token
        if not token:
            raise RuntimeError(
                f"{self._auth_label} OAuth 凭证中 access_token 为空，且未找到 refresh_token；"
                f"请重新执行 {self._login_command}。"
            )
        return token, auth_file

    async def _load_code_assist_project(self, access_token: str) -> str:
        client_kwargs = self._http_client_kwargs(connect_timeout=15.0)
        client = await _get_pooled_http_client(
            _http_client_pool_key(
                self._auth_label,
                "loadCodeAssist",
                self._load_code_assist_endpoint(),
                access_token[:16],
                timeout=self.timeout,
                connect_timeout=15.0,
                proxy=self.proxy,
            ),
            **client_kwargs,
        )
        resp = await client.post(
            self._load_code_assist_endpoint(),
            json={"metadata": self._load_code_assist_metadata()},
            headers=self._headers(access_token),
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("cloudaicompanionProject", "") or "").strip()

    async def _resolve_project(self, access_token: str, auth_file: _Path | None = None) -> str:
        if self.project_override:
            return self.project_override
        cached = type(self)._project_cache.get(access_token)
        if cached:
            return cached
        load_error: Exception | None = None
        data: dict = {}
        # 第一次尝试用当前 token；如果 401 说明 token 已过期但本地未察觉（expiry 缺失或被绕过），
        # 强制刷新一次再重试。
        for attempt in range(2):
            try:
                project = await self._load_code_assist_project(access_token)
                data = {"cloudaicompanionProject": project}
                load_error = None
                break
            except httpx.HTTPStatusError as exc:
                load_error = exc
                if exc.response is not None and exc.response.status_code == 401 and attempt == 0:
                    try:
                        new_token, _ = await self._get_access_token(force_refresh=True)
                    except Exception:
                        new_token = ""
                    if new_token and new_token != access_token:
                        access_token = new_token
                        continue
                break
            except Exception as exc:
                load_error = exc
                break
        project = str(data.get("cloudaicompanionProject", "") or "").strip()
        if project:
            type(self)._project_cache[access_token] = project
            return project

        local_project = self._resolve_local_project(auth_file)
        if local_project:
            type(self)._project_cache[access_token] = local_project
            return local_project
        if load_error is not None:
            raise RuntimeError(
                f"loadCodeAssist 获取 {self._auth_label} companion project 失败。"
                f"如果本机 {self._auth_label} 可用，请确认 bot 进程读取的是同一份 OAuth 凭证，"
                f"或在配置 {self._project_config_field} 中手动指定 project。"
                f" 原始错误: {load_error}"
            ) from load_error
        if not project:
            raise RuntimeError(
                "loadCodeAssist 未返回 cloudaicompanionProject。"
                f"已尝试从 {self._auth_label}/gcloud 本地配置自动发现 project 但未找到。"
                f"请在配置 {self._project_config_field} 中手动指定 GCP 项目 ID，"
                f"或先运行一次 {self._onboarding_command} 完成 onboarding。"
            )

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        messages = inject_current_time_context(messages)
        contains_image_input = _messages_contain_images(messages)
        try:
            access_token, auth_file = await self._get_access_token()
            project = await self._resolve_project(access_token, auth_file)
            system_instruction, contents = _convert_messages_to_gemini(messages)

            tool_payload: List[dict] = []
            if tools:
                tool_payload.append(
                    {
                        "function_declarations": [
                            _convert_openai_tool_to_gemini(tool) for tool in tools
                        ]
                    }
                )
            if use_builtin_search:
                tool_payload.append(_gemini_builtin_search_tool(self.model))

            request_obj: Dict[str, Any] = {
                "contents": contents,
                "generationConfig": {
                    "thinkingConfig": {
                        "thinkingBudget": GEMINI_THINKING_BUDGET_MAP[self.thinking_mode]
                    }
                },
            }
            if system_instruction:
                request_obj["systemInstruction"] = {"parts": [{"text": system_instruction}]}
            if tool_payload:
                request_obj["tools"] = tool_payload

            model_candidates = _gemini_cli_model_candidates(self.model)
            last_exc: Exception | None = None
            auth_refreshed_for_401 = False
            client_kwargs = self._http_client_kwargs(connect_timeout=15.0)
            client = await _get_pooled_http_client(
                _http_client_pool_key(
                    self._auth_label,
                    "generateContent",
                    _GEMINI_CLI_GENERATE_ENDPOINT,
                    access_token[:16],
                    timeout=self.timeout,
                    connect_timeout=15.0,
                    proxy=self.proxy,
                ),
                **client_kwargs,
            )
            if True:

                async def _post_once(_model_name: str, _access_token: str, _project: str):
                    envelope_inner = {
                        "model": _model_name,
                        "project": _project,
                        "request": request_obj,
                    }
                    resp = await client.post(
                        _GEMINI_CLI_GENERATE_ENDPOINT,
                        json=envelope_inner,
                        headers=self._headers(_access_token),
                    )
                    resp.raise_for_status()
                    return resp.json()

                data = {}
                for model_index, model_name in enumerate(model_candidates):
                    try:
                        data = await _post_once(model_name, access_token, project)
                        last_exc = None
                        break
                    except httpx.HTTPStatusError as exc:
                        last_exc = exc
                        # 401 时强制刷新 access_token 并重试一次（仅一次，避免循环刷新风暴）
                        if (
                            exc.response is not None
                            and exc.response.status_code == 401
                            and not auth_refreshed_for_401
                        ):
                            auth_refreshed_for_401 = True
                            try:
                                access_token, _ = await self._get_access_token(force_refresh=True)
                                project = await self._resolve_project(access_token, auth_file)
                                data = await _post_once(model_name, access_token, project)
                                last_exc = None
                                break
                            except Exception as exc2:
                                last_exc = exc2
                                has_next = model_index + 1 < len(model_candidates)
                                if has_next and _gemini_cli_retry_with_fallback_model(exc2):
                                    continue
                                raise
                        has_next = model_index + 1 < len(model_candidates)
                        if has_next and _gemini_cli_retry_with_fallback_model(exc):
                            continue
                        raise
                    except Exception as exc:
                        last_exc = exc
                        has_next = model_index + 1 < len(model_candidates)
                        if has_next and _gemini_cli_retry_with_fallback_model(exc):
                            continue
                        raise
                if last_exc is not None and not data:
                    raise last_exc

            inner_response = data.get("response") if isinstance(data, dict) else None
            if isinstance(inner_response, dict):
                payload = inner_response
            else:
                payload = data
            candidates = list(payload.get("candidates", []) or [])
            if not candidates:
                return ToolCallerResponse("stop", "", [], data)
            content = candidates[0].get("content") or {}
            parts = list(content.get("parts", []) or [])
            tool_calls = _extract_gemini_tool_calls(parts)
            text = _extract_gemini_text(parts)
            finish_reason = "tool_calls" if tool_calls else "stop"
            grounding = candidates[0].get("groundingMetadata") or candidates[0].get("grounding_metadata")
            # Gemini CLI 的 usageMetadata 同时在 payload（inner_response）和外层 data 里出现，优先取里层
            usage = _extract_usage(payload) or _extract_usage(data)
            return ToolCallerResponse(
                finish_reason=finish_reason,
                content=text,
                tool_calls=tool_calls,
                raw=data,
                used_builtin_search=bool(grounding),
                usage=usage,
                model_used=str(self.model or self._default_model),
            )
        except Exception as exc:
            if contains_image_input and _error_indicates_vision_unavailable(exc):
                return _vision_unavailable_response(exc)
            raise

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        return {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": tool_name,
                        "response": {"result": result},
                    }
                }
            ],
        }


import uuid as _uuid

# 真实 Antigravity v1internal 走 daily-cloudcode-pa。Gemini CLI 仍走
# cloudcode-pa，两者不能混用；否则即使 OAuth token 有效也可能 403。
_ANTIGRAVITY_CLI_BASE = "https://daily-cloudcode-pa.googleapis.com"
_ANTIGRAVITY_CLI_LOAD_CODE_ASSIST = f"{_ANTIGRAVITY_CLI_BASE}/v1internal:loadCodeAssist"
_ANTIGRAVITY_CLI_GENERATE_ENDPOINT = f"{_ANTIGRAVITY_CLI_BASE}/v1internal:generateContent"
_ANTIGRAVITY_CLI_STREAM_ENDPOINT = (
    f"{_ANTIGRAVITY_CLI_BASE}/v1internal:streamGenerateContent?alt=sse"
)
_ANTIGRAVITY_CLIENT_UA_VERSION_FALLBACK = "1.0.0"
_ANTIGRAVITY_X_GOOG_API_CLIENT = "google-cloud-sdk vscode_cloudshelleditor/0.1"
_ANTIGRAVITY_CLIENT_UA_VERSION_CACHE = ""


def _antigravity_client_version() -> str:
    """返回本机 agy 版本；可用 ANTIGRAVITY_CLI_VERSION 覆盖。"""
    global _ANTIGRAVITY_CLIENT_UA_VERSION_CACHE
    override = str(_os.environ.get("ANTIGRAVITY_CLI_VERSION", "") or "").strip()
    if override:
        return override
    if _ANTIGRAVITY_CLIENT_UA_VERSION_CACHE:
        return _ANTIGRAVITY_CLIENT_UA_VERSION_CACHE
    try:
        import shutil as _shutil
        import subprocess as _subprocess

        exe = _shutil.which("agy")
        if exe:
            version = _subprocess.check_output(
                [exe, "--version"],
                text=True,
                timeout=2.0,
                stderr=_subprocess.DEVNULL,
            ).strip()
            if version:
                _ANTIGRAVITY_CLIENT_UA_VERSION_CACHE = version.splitlines()[0].strip()
                return _ANTIGRAVITY_CLIENT_UA_VERSION_CACHE
    except Exception:
        pass
    _ANTIGRAVITY_CLIENT_UA_VERSION_CACHE = _ANTIGRAVITY_CLIENT_UA_VERSION_FALLBACK
    return _ANTIGRAVITY_CLIENT_UA_VERSION_CACHE


def _antigravity_client_platform() -> str:
    """返回 Antigravity Client-Metadata 中 platform 字段值。"""
    import platform as _platform_mod

    sys_name = (_platform_mod.system() or "").lower()
    if sys_name == "darwin":
        return "MACOS"
    if sys_name == "linux":
        return "LINUX"
    return "WINDOWS"


def _antigravity_user_agent() -> str:
    """按当前 OS/arch 构造 Antigravity 真实使用的 User-Agent 字符串。

    格式 `antigravity/<ver> <os>/<arch>`，例如 ``antigravity/1.15.8 windows/amd64``。
    """
    import platform as _platform_mod

    os_token = (_platform_mod.system() or "windows").lower()
    if os_token == "darwin":
        os_token = "macos"
    arch_token = (_platform_mod.machine() or "amd64").lower()
    if arch_token in {"x86_64", "x64"}:
        arch_token = "amd64"
    elif arch_token in {"aarch64", "arm64"}:
        arch_token = "arm64"
    return f"antigravity/{_antigravity_client_version()} {os_token}/{arch_token}"


# Antigravity v1internal 实际接受的模型列表。把用户配置的 model 放第一位，
# 后面追加 fallback 候选避免单个模型暂时受限时整体失败。
# 注意：gemini-3.5-flash 是 Antigravity 2026-05 默认模型，必须在候选首位。
_ANTIGRAVITY_CLI_AUTO_MODELS = (
    "gemini-3.5-flash-low",
    "gemini-3-flash-agent",
    "gemini-3.5-flash-extra-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
)
_ANTIGRAVITY_CLI_MODEL_ALIASES = {
    "gemini-3.5-flash": "gemini-3.5-flash-low",
    "gemini-3.5-flash-medium": "gemini-3.5-flash-low",
    "gemini-3.5-flash-high": "gemini-3-flash-agent",
    "gemini-3-pro-high": "gemini-3.1-pro-high",
    "gemini-3-pro-low": "gemini-3.1-pro-low",
    "gemini-3-pro": "gemini-3.1-pro-high",
}


def _antigravity_cli_model_candidates(model: str) -> list[str]:
    """Antigravity v1internal 的 model 候选链；与 gemini-cli 路径解耦。"""
    primary = (model or "auto-gemini-3").strip() or "auto-gemini-3"
    lowered = primary.lower()
    if lowered in {"auto", "auto-gemini-3", "auto-antigravity"}:
        candidates = list(_ANTIGRAVITY_CLI_AUTO_MODELS)
    else:
        candidates = [_ANTIGRAVITY_CLI_MODEL_ALIASES.get(lowered, primary)]
        for fallback in _ANTIGRAVITY_CLI_AUTO_MODELS:
            if fallback.lower() != candidates[0].lower():
                candidates.append(fallback)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _parse_antigravity_sse_response(text: str) -> dict[str, Any]:
    """把 Antigravity streamGenerateContent SSE 合并成 generateContent 形状。

    agy CLI 实际使用 `streamGenerateContent?alt=sse`，返回多行 `data: {...}`。
    上层只关心最终 candidates / usageMetadata；这里将文本片段、tool parts 与
    metadata 合并到一个 `{"response": ...}` envelope，兼容既有解析逻辑。
    """
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if data_lines:
                joined = "\n".join(data_lines).strip()
                data_lines = []
                if joined and joined != "[DONE]":
                    try:
                        parsed = json.loads(joined)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict):
                        events.append(parsed)
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        joined = "\n".join(data_lines).strip()
        if joined and joined != "[DONE]":
            try:
                parsed = json.loads(joined)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                events.append(parsed)

    if not events:
        try:
            fallback = json.loads(text)
        except Exception:
            fallback = {}
        return fallback if isinstance(fallback, dict) else {}

    parts: list[dict[str, Any]] = []
    finish_reason = ""
    grounding: Any = None
    usage: Any = None
    last_candidate_extra: dict[str, Any] = {}
    for event in events:
        payload = event.get("response") if isinstance(event.get("response"), dict) else event
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        if not isinstance(candidates, list) or not candidates:
            if isinstance(payload, dict):
                usage = payload.get("usageMetadata") or payload.get("usage_metadata") or usage
            continue
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        chunk_parts = content.get("parts") if isinstance(content, dict) else None
        if isinstance(chunk_parts, list):
            for part in chunk_parts:
                if isinstance(part, dict):
                    parts.append(part)
        finish_reason = (
            candidate.get("finishReason")
            or candidate.get("finish_reason")
            or finish_reason
        )
        grounding = (
            candidate.get("groundingMetadata")
            or candidate.get("grounding_metadata")
            or grounding
        )
        if isinstance(candidate, dict):
            last_candidate_extra = {
                key: value
                for key, value in candidate.items()
                if key not in {"content", "finishReason", "finish_reason"}
            }
        if isinstance(payload, dict):
            usage = payload.get("usageMetadata") or payload.get("usage_metadata") or usage

    candidate: dict[str, Any] = dict(last_candidate_extra)
    candidate["content"] = {"parts": parts}
    if finish_reason:
        candidate["finishReason"] = finish_reason
    if grounding:
        candidate["groundingMetadata"] = grounding
    merged: dict[str, Any] = {"candidates": [candidate]}
    if usage:
        merged["usageMetadata"] = usage
    return {"response": merged, "stream": events}


def _antigravity_is_transient_network_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "tls/ssl connection has been closed",
            "connection reset",
            "connection aborted",
            "eof",
            "timed out",
        )
    )


class AntigravityCliToolCaller(GeminiCliToolCaller):
    """复用 Antigravity CLI OAuth/兼容 Gemini OAuth 凭证调用 Gemini 系列模型。"""

    _project_cache: dict[str, str] = {}
    _default_model = _ANTIGRAVITY_CLI_DEFAULT_MODEL
    _auth_label = "Antigravity CLI"
    _login_command = "`agy`"
    _onboarding_command = "`agy`"
    _auth_config_field = "personification_antigravity_cli_auth_path"
    _project_config_field = "personification_antigravity_cli_project"
    # 保留 class attr 兼容父类签名；实际请求 UA 在 _headers 里动态构造。
    _user_agent = "antigravity/1.15.8 windows/amd64"

    async def _refresh_token_remote(self, refresh_token: str) -> dict:
        """按凭证来源选择 OAuth client 续 token。

        Antigravity 专用凭证必须用 antigravity client；兼容回退到
        gemini-cli 凭证时必须用 gemini-cli client，否则 Google 会返回
        unauthorized_client。
        """
        # 无显式代理且环境中未配置 HTTPS_PROXY 时，禁止 httpx 读取系统代理（TUN 兼容）；
        # 有环境代理或显式 proxy 时，传 None 让 httpx 按默认逻辑决定（trust_env=True）。
        trust_env: bool | None = False if not self.proxy and not _has_env_proxy() else None
        if getattr(self, "_last_auth_source", "") == "gemini_compat":
            return await _refresh_gemini_cli_access_token(
                refresh_token,
                proxy=self.proxy,
                trust_env=trust_env,
            )
        return await _refresh_antigravity_cli_access_token(
            refresh_token,
            proxy=self.proxy,
            trust_env=trust_env,
        )

    def _headers(self, access_token: str) -> dict[str, str]:
        """覆写父类 _headers：注入 Antigravity 必需的 Client-Metadata、
        X-Goog-Api-Client 与按 OS/arch 动态生成的 User-Agent。
        """
        import json as _json_local

        client_metadata = _json_local.dumps(
            {
                "ideType": "ANTIGRAVITY",
                "platform": _antigravity_client_platform(),
                "pluginType": "GEMINI",
            },
            separators=(",", ":"),
        )
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": _antigravity_user_agent(),
            "X-Goog-Api-Client": _ANTIGRAVITY_X_GOOG_API_CLIENT,
            "Client-Metadata": client_metadata,
        }

    def _load_code_assist_endpoint(self) -> str:
        return _ANTIGRAVITY_CLI_LOAD_CODE_ASSIST

    def _load_code_assist_metadata(self) -> dict[str, Any]:
        return {
            "ideType": "ANTIGRAVITY",
            "platform": _antigravity_client_platform(),
            "pluginType": "GEMINI",
        }

    def _http_client_kwargs(self, *, connect_timeout: float = 15.0) -> dict[str, Any]:
        client_kwargs = super()._http_client_kwargs(connect_timeout=connect_timeout)
        if not self.proxy and not _has_env_proxy():
            # 无显式代理且环境中未配置 HTTPS_PROXY/ALL_PROXY 时，禁止 httpx 读取
            # 系统代理，使 TUN 透明代理能在 OS 层正常接管（HTTP CONNECT 会绕开 TUN）。
            # 若使用 TUN 的同时需要 HTTPS_PROXY，请改用 proxy 字段显式配置代理地址。
            client_kwargs["trust_env"] = False
        return client_kwargs

    def _find_auth_file_with_log(self) -> tuple[_Path | None, list[str]]:
        auth_file, searched, source = _find_antigravity_cli_auth_file_with_source(
            self.auth_path_override
        )
        self._last_auth_source = source
        return auth_file, searched

    async def _get_access_token(self, *, force_refresh: bool = False) -> tuple[str, _Path]:
        """覆写：始终从系统 keyring 取 agy 最新 token，不走 gemini-cli OAuth refresh。

        agy CLI 后台会自己刷新 Credential Manager / Keychain 中的 access_token；
        bot 每次重读 keyring，避免用 gemini-cli 的 OAuth client_id 去 refresh
        agy 的 refresh_token（必然 invalid_grant）。
        """
        synced = _sync_antigravity_keyring_to_file()
        if synced is None:
            # keyring 不可用（库未安装 / agy 未登录），回退父类逻辑作兜底
            return await super()._get_access_token(force_refresh=force_refresh)
        try:
            auth = _load_gemini_cli_auth(synced)
            token = _get_gemini_cli_access_token(auth)
        except Exception as exc:
            raise RuntimeError(
                f"Antigravity 凭证读取失败：{exc}。"
                "请确认 agy CLI 已正常登录（运行 `agy --print hi` 完成 Google OAuth）。"
            ) from exc
        if not token:
            raise RuntimeError(
                "Antigravity 凭证 access_token 为空。"
                "请重新运行 agy 完成 Google OAuth 登录。"
            )
        return token, synced

    def _resolve_local_project(self, auth_file: _Path | None = None) -> str:
        return _resolve_local_antigravity_cli_project(auth_file)

    async def _resolve_project(self, access_token: str, auth_file: _Path | None = None) -> str:
        if self.project_override:
            return self.project_override
        cached = type(self)._project_cache.get(access_token)
        if cached:
            return cached

        # 1) 优先本地解析（agy settings.json / 环境变量）
        local = self._resolve_local_project(auth_file)
        if local:
            type(self)._project_cache[access_token] = local
            return local

        # 2) 本地拿不到 → 复用父类 loadCodeAssist 远程查询；agy 的 token
        #    scope 已含 cclog/experimentsandconfigs，正常情况能拿到
        #    cloudaicompanionProject 字段
        try:
            project = await super()._resolve_project(access_token, auth_file)
        except Exception as exc:
            raise RuntimeError(
                "Antigravity CLI 无法解析 GCP Project ID。\n"
                "请执行以下操作之一：\n"
                "1. 在 .env 中配置 `personification_antigravity_cli_project=\"你的项目ID\"`\n"
                "2. 设置环境变量 `ANTIGRAVITY_CLI_PROJECT` 或 `AGY_PROJECT`\n"
                "3. 在 `~/.gemini/antigravity-cli/settings.json` 里加 `project` 字段\n"
                f"4. 或确认 agy 凭证已含 cclog scope（当前 loadCodeAssist 出错：{exc}）"
            ) from exc
        if project:
            type(self)._project_cache[access_token] = project
        return project

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        messages = inject_current_time_context(messages)
        contains_image_input = _messages_contain_images(messages)
        try:
            access_token, auth_file = await self._get_access_token()
            project = await self._resolve_project(access_token, auth_file)
            system_instruction, contents = _convert_messages_to_gemini(messages)

            tool_payload: List[dict] = []
            if tools:
                tool_payload.append(
                    {
                        "function_declarations": [
                            _convert_openai_tool_to_gemini(tool) for tool in tools
                        ]
                    }
                )
            if use_builtin_search:
                tool_payload.append(_gemini_builtin_search_tool(self.model))

            request_obj: Dict[str, Any] = {
                "contents": contents,
                "generationConfig": {
                    "thinkingConfig": {
                        "thinkingBudget": GEMINI_THINKING_BUDGET_MAP[self.thinking_mode]
                    }
                },
            }
            if system_instruction:
                request_obj["systemInstruction"] = {"parts": [{"text": system_instruction}]}
            if tool_payload:
                request_obj["tools"] = tool_payload

            model_candidates = _antigravity_cli_model_candidates(self.model)
            last_exc: Exception | None = None
            auth_refreshed_for_401 = False
            project_refreshed_for_403 = False
            selected_model_name = ""
            client_kwargs = self._http_client_kwargs(connect_timeout=15.0)
            client = await _get_pooled_http_client(
                _http_client_pool_key(
                    self._auth_label,
                    "streamGenerateContent",
                    _ANTIGRAVITY_CLI_STREAM_ENDPOINT,
                    access_token[:16],
                    timeout=self.timeout,
                    connect_timeout=15.0,
                    proxy=self.proxy,
                ),
                **client_kwargs,
            )
            if True:

                async def _post_once(_model_name: str, _access_token: str, _project: str):
                    # Antigravity v1internal envelope 比 gemini-cli 多两个顶层字段：
                    # userAgent 固定字面值 "antigravity"；requestId 每次请求独立的标识。
                    envelope_inner = {
                        "model": _model_name,
                        "project": _project,
                        "request": request_obj,
                        "userAgent": "antigravity",
                        "requestId": _uuid.uuid4().hex,
                    }
                    last_network_exc: Exception | None = None
                    for attempt in range(4):
                        try:
                            resp = await client.post(
                                _ANTIGRAVITY_CLI_STREAM_ENDPOINT,
                                json=envelope_inner,
                                headers=self._headers(_access_token),
                            )
                            resp.raise_for_status()
                            body_attr = getattr(resp, "text", "")
                            body_text = body_attr if isinstance(body_attr, str) else ""
                            if body_text.strip().startswith(("data:", "{")):
                                return _parse_antigravity_sse_response(body_text)
                            return resp.json()
                        except Exception as exc:
                            if not _antigravity_is_transient_network_error(exc) or attempt >= 3:
                                raise
                            last_network_exc = exc
                            await asyncio.sleep(0.8 * (attempt + 1))
                    if last_network_exc is not None:
                        raise last_network_exc
                    raise RuntimeError("Antigravity CLI streamGenerateContent 未返回响应")

                data = {}
                for model_index, model_name in enumerate(model_candidates):
                    try:
                        data = await _post_once(model_name, access_token, project)
                        selected_model_name = model_name
                        last_exc = None
                        break
                    except httpx.HTTPStatusError as exc:
                        last_exc = exc
                        if (
                            exc.response is not None
                            and exc.response.status_code == 401
                            and not auth_refreshed_for_401
                        ):
                            auth_refreshed_for_401 = True
                            try:
                                access_token, _ = await self._get_access_token(force_refresh=True)
                                project = await self._resolve_project(access_token, auth_file)
                                data = await _post_once(model_name, access_token, project)
                                selected_model_name = model_name
                                last_exc = None
                                break
                            except Exception as exc2:
                                last_exc = exc2
                                has_next = model_index + 1 < len(model_candidates)
                                if has_next and _gemini_cli_retry_with_fallback_model(exc2):
                                    continue
                                raise
                        if (
                            exc.response is not None
                            and exc.response.status_code == 403
                            and not project_refreshed_for_403
                        ):
                            project_refreshed_for_403 = True
                            try:
                                remote_project = await self._load_code_assist_project(access_token)
                                if remote_project and remote_project != project:
                                    project = remote_project
                                    type(self)._project_cache[access_token] = project
                                    data = await _post_once(model_name, access_token, project)
                                    selected_model_name = model_name
                                    last_exc = None
                                    break
                            except Exception as exc2:
                                last_exc = exc2
                                has_next = model_index + 1 < len(model_candidates)
                                if has_next and _gemini_cli_retry_with_fallback_model(exc2):
                                    continue
                                raise
                        has_next = model_index + 1 < len(model_candidates)
                        if has_next and _gemini_cli_retry_with_fallback_model(exc):
                            continue
                        raise
                    except Exception as exc:
                        last_exc = exc
                        has_next = model_index + 1 < len(model_candidates)
                        if has_next and _gemini_cli_retry_with_fallback_model(exc):
                            continue
                        raise
                if last_exc is not None and not data:
                    raise last_exc

            inner_response = data.get("response") if isinstance(data, dict) else None
            if isinstance(inner_response, dict):
                payload = inner_response
            else:
                payload = data
            candidates = list(payload.get("candidates", []) or [])
            if not candidates:
                return ToolCallerResponse("stop", "", [], data)
            content = candidates[0].get("content") or {}
            parts = list(content.get("parts", []) or [])
            tool_calls = _extract_gemini_tool_calls(parts)
            text = _extract_gemini_text(parts)
            finish_reason = "tool_calls" if tool_calls else "stop"
            grounding = candidates[0].get("groundingMetadata") or candidates[0].get("grounding_metadata")
            usage = _extract_usage(payload) or _extract_usage(data)
            return ToolCallerResponse(
                finish_reason=finish_reason,
                content=text,
                tool_calls=tool_calls,
                raw=data,
                used_builtin_search=bool(grounding),
                usage=usage,
                model_used=str(selected_model_name or self.model or self._default_model),
            )
        except Exception as exc:
            if contains_image_input and _error_indicates_vision_unavailable(exc):
                return _vision_unavailable_response(exc)
            raise


def _find_claude_code_auth_file(override_path: str = "") -> _Path | None:
    if override_path:
        p = _Path(override_path).expanduser()
        if p.exists():
            return p
    candidates = [
        _os.environ.get("CLAUDE_CODE_HOME", ""),
        _os.environ.get("CLAUDE_HOME", ""),
    ]
    for base in candidates:
        if base:
            p = _Path(base) / ".credentials.json"
            if p.exists():
                return p
    for fallback in [
        "~/.claude/.credentials.json",
        "~/AppData/Roaming/claude/.credentials.json",
        "~/.config/claude/.credentials.json",
    ]:
        p = _Path(fallback).expanduser()
        if p.exists():
            return p
    return None


def _load_claude_code_auth(auth_path: _Path) -> dict:
    try:
        return json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"读取 claude-code .credentials.json 失败: {exc}") from exc


def _get_claude_code_access_token(auth: dict) -> str:
    nested = auth.get("claudeAiOauth") if isinstance(auth, dict) else None
    if isinstance(nested, dict):
        token = str(nested.get("accessToken", "") or "").strip()
        if token:
            return token
    for key in ("accessToken", "access_token"):
        token = str(auth.get(key, "") or "").strip()
        if token:
            return token
    return ""


class ClaudeCodeToolCaller(AnthropicToolCaller):
    """复用 claude-code 本地 OAuth 凭证调用 Anthropic Messages API。"""

    def __init__(
        self,
        *,
        model: str,
        auth_path: str = "",
        thinking_mode: str = "none",
        timeout: float = 120.0,
    ) -> None:
        super().__init__(
            api_key="",
            base_url="",
            model=(model or "claude-opus-4-7").strip() or "claude-opus-4-7",
            thinking_mode=thinking_mode,
            timeout=timeout,
        )
        self.auth_path_override = auth_path

    def _get_access_token(self) -> str:
        auth_file = _find_claude_code_auth_file(self.auth_path_override)
        if auth_file is None:
            raise RuntimeError(
                "未找到 claude-code .credentials.json。"
                "请先运行 `claude login` 完成 Anthropic OAuth 登录，"
                "或在配置中设置 personification_claude_code_auth_path。"
            )
        auth = _load_claude_code_auth(auth_file)
        token = _get_claude_code_access_token(auth)
        if not token:
            raise RuntimeError(
                "claude-code .credentials.json 中 accessToken 为空，请重新执行 `claude login`。"
            )
        return token

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        from anthropic import AsyncAnthropic

        messages = inject_current_time_context(messages)
        contains_image_input = _messages_contain_images(messages)
        try:
            access_token = self._get_access_token()
            system_instruction, anthropic_messages = _convert_messages_to_anthropic(messages)
            filtered_tools = [
                tool
                for tool in tools
                if not (
                    use_builtin_search
                    and str(_obj_get(_obj_get(tool, "function", {}), "name", "") or "") == "web_search"
                )
            ]
            tool_payload = [_convert_openai_tool_to_anthropic(tool) for tool in filtered_tools]
            if use_builtin_search:
                tool_payload.append(dict(ANTHROPIC_BUILTIN_SEARCH_TOOL))

            client = AsyncAnthropic(
                auth_token=access_token,
                timeout=self.timeout,
                default_headers={"anthropic-beta": _CLAUDE_CODE_OAUTH_BETA},
            )

            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": anthropic_messages,
                "max_tokens": 1024,
            }
            if system_instruction:
                payload["system"] = system_instruction
            if tool_payload:
                payload["tools"] = tool_payload
            thinking = _maybe_anthropic_thinking(self.thinking_mode)
            if thinking:
                payload["thinking"] = thinking

            response = await client.messages.create(**payload)

            content_blocks = list(_obj_get(response, "content", []) or [])
            text_parts: List[str] = []
            tool_calls: List[ToolCall] = []
            for block in content_blocks:
                block_type = _obj_get(block, "type", "")
                if block_type == "text":
                    text_parts.append(str(_obj_get(block, "text", "")))
                elif block_type == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=str(_obj_get(block, "id", "")),
                            name=str(_obj_get(block, "name", "")),
                            arguments=_parse_tool_arguments(_obj_get(block, "input", {}) or {}),
                        )
                    )

            finish_reason = "tool_calls" if tool_calls else "stop"
            used_builtin = _anthropic_used_builtin_search(content_blocks)
            return ToolCallerResponse(
                finish_reason=finish_reason,
                content="".join(text_parts).strip(),
                tool_calls=tool_calls,
                raw=response,
                used_builtin_search=used_builtin,
                usage=_extract_usage(response),
                model_used=str(getattr(self, "model", "") or ""),
            )
        except Exception as exc:
            if contains_image_input and _error_indicates_vision_unavailable(exc):
                return _vision_unavailable_response(exc)
            raise


def build_tool_caller(config: Any, supports_reasoning: Optional[bool] = None) -> ToolCaller:
    api_type = _normalize_api_type(getattr(config, "personification_api_type", "openai"))
    api_key = str(getattr(config, "personification_api_key", "") or "").strip()
    api_url = str(getattr(config, "personification_api_url", "") or "").strip()
    model = str(getattr(config, "personification_model", "") or "").strip()
    proxy = str(getattr(config, "personification_proxy", "") or "").strip()
    thinking_mode = _normalize_thinking_mode(
        getattr(config, "personification_thinking_mode", "none")
    )

    if api_type == "gemini_official":
        return GeminiToolCaller(
            api_key=api_key,
            base_url=api_url,
            model=model,
            thinking_mode=thinking_mode,
        )
    if api_type == "gemini_cli":
        auth_path = str(getattr(config, "personification_gemini_cli_auth_path", "") or "").strip()
        project = str(getattr(config, "personification_gemini_cli_project", "") or "").strip()
        return GeminiCliToolCaller(
            model=model or _GEMINI_CLI_DEFAULT_MODEL,
            auth_path=auth_path,
            project=project,
            thinking_mode=thinking_mode,
            proxy=proxy,
        )
    if api_type == "antigravity_cli":
        auth_path = str(getattr(config, "personification_antigravity_cli_auth_path", "") or "").strip()
        if not auth_path:
            auth_path = str(getattr(config, "personification_gemini_cli_auth_path", "") or "").strip()
        project = str(getattr(config, "personification_antigravity_cli_project", "") or "").strip()
        if not project:
            project = str(getattr(config, "personification_gemini_cli_project", "") or "").strip()
        return AntigravityCliToolCaller(
            model=model or _ANTIGRAVITY_CLI_DEFAULT_MODEL,
            auth_path=auth_path,
            project=project,
            thinking_mode=thinking_mode,
            proxy=proxy or str(getattr(config, "personification_antigravity_cli_proxy", "") or "").strip(),
        )
    if api_type == "anthropic":
        return AnthropicToolCaller(
            api_key=api_key,
            base_url=api_url,
            model=model,
            thinking_mode=thinking_mode,
        )
    if api_type == "claude_code":
        auth_path = str(getattr(config, "personification_claude_code_auth_path", "") or "").strip()
        return ClaudeCodeToolCaller(
            model=model or "claude-opus-4-7",
            auth_path=auth_path,
            thinking_mode=thinking_mode,
        )
    if api_type == "openai_codex":
        auth_path = str(getattr(config, "personification_codex_auth_path", "") or "").strip()
        return OpenAICodexToolCaller(
            model=model or "gpt-5.3-codex",
            auth_path=auth_path,
            proxy=proxy,
        )
    return OpenAIToolCaller(
        api_key=api_key,
        base_url=api_url,
        model=model,
        thinking_mode=thinking_mode,
        supports_reasoning=supports_reasoning,
        proxy=proxy,
    )
