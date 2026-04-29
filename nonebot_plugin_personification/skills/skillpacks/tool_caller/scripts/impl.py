from __future__ import annotations

import asyncio
import base64
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from plugin.personification.core.image_refs import normalize_image_ref
from plugin.personification.core.message_parts import extract_text_from_parts, normalize_message_parts


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
    if value == "anthropic":
        return "anthropic"
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
    ) -> None:
        self.api_key = api_key
        self.base_url = _normalize_openai_base_url(base_url)
        self.model = model
        self.thinking_mode = _normalize_thinking_mode(thinking_mode)
        self.timeout = timeout
        self._supports_reasoning = supports_reasoning

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        use_builtin_search: bool,
    ) -> ToolCallerResponse:
        from openai import AsyncOpenAI

        contains_image_input = _messages_contain_images(messages)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as http_client:
                client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    http_client=http_client,
                )
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
                                )
                            except Exception:
                                responses_failed = True
                        else:
                            raise
                    except Exception:
                        responses_failed = True

                chat_supports_native_search = use_builtin_search and _openai_chat_search_model(self.model)

                def _build_chat_payload(*, use_native_search: bool, use_original_tools: bool) -> Dict[str, Any]:
                    payload = {
                        "model": self.model,
                        "messages": messages,
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
    ) -> None:
        self.model = _normalize_codex_model_name(model)
        self.auth_path_override = auth_path
        self.timeout = timeout

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
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout, connect=15.0)
                ) as client:
                    async with client.stream(
                        "POST",
                        _CODEX_API_ENDPOINT,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream, application/json",
                            "Connection": "close",
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
    ) -> dict[str, str]:
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return {"error": "empty prompt"}
        access_token, _ = await self._get_access_token()
        requested_size = str(size or "1024x1024").strip() or "1024x1024"
        generation_size = self._normalize_image_generation_size(requested_size)
        size_hint = self._normalize_image_size_hint(generation_size)
        requested_image_model = self._normalize_image_generation_model(image_model)
        messages = [
            {
                "role": "user",
                "content": (
                    "Generate exactly one image for the user. "
                    "Use the image_generation tool and return no prose-only substitute.\n"
                    f"Requested image model: {requested_image_model}.\n"
                    f"Requested aspect/size: {size_hint} ({requested_size}).\n"
                    f"Prompt: {prompt_text}"
                ),
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
                "You are generating a single image for a chat bot. "
                f"Call the built-in image_generation tool using {requested_image_model} when available. "
                "Do not answer with a prompt or instructions."
            ),
            "input": self._build_input(messages),
            "tool_choice": {"type": "image_generation"},
            "tools": [image_tool],
        }
        try:
            data = await self._request_codex_response(payload, access_token=access_token)
        except Exception as exc:
            error_text = str(exc).lower()
            if "model" not in error_text:
                raise
            fallback_tool = dict(image_tool)
            fallback_tool.pop("model", None)
            payload = dict(payload)
            payload["tools"] = [fallback_tool]
            data = await self._request_codex_response(payload, access_token=access_token)
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
                return {"b64_json": b64}
        b64, url = self._extract_image_payload_candidate(data)
        if not b64 and url:
            b64 = await self._download_codex_image_result(url, access_token)
        if b64:
            return {"b64_json": b64}
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


def build_tool_caller(config: Any, supports_reasoning: Optional[bool] = None) -> ToolCaller:
    api_type = _normalize_api_type(getattr(config, "personification_api_type", "openai"))
    api_key = str(getattr(config, "personification_api_key", "") or "").strip()
    api_url = str(getattr(config, "personification_api_url", "") or "").strip()
    model = str(getattr(config, "personification_model", "") or "").strip()
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
    if api_type == "anthropic":
        return AnthropicToolCaller(
            api_key=api_key,
            base_url=api_url,
            model=model,
            thinking_mode=thinking_mode,
        )
    if api_type == "openai_codex":
        auth_path = str(getattr(config, "personification_codex_auth_path", "") or "").strip()
        return OpenAICodexToolCaller(
            model=model or "gpt-5.3-codex",
            auth_path=auth_path,
        )
    return OpenAIToolCaller(
        api_key=api_key,
        base_url=api_url,
        model=model,
        thinking_mode=thinking_mode,
        supports_reasoning=supports_reasoning,
    )
