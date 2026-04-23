from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from nonebot_plugin_personification.core.image_refs import normalize_image_ref
from nonebot_plugin_personification.skills.skillpacks.tool_caller.scripts.impl import (
    OpenAICodexToolCaller,
    _convert_openai_tool_to_gemini,
    _extract_gemini_text,
    _extract_gemini_tool_calls,
    _normalize_api_type,
    _normalize_codex_model_name,
    _normalize_openai_base_url,
    _obj_get,
    _split_data_url,
)


logger = logging.getLogger(__name__)


def _provider_model_is_compatible(api_type: str, model: str) -> bool:
    normalized_type = _normalize_api_type(api_type)
    normalized_model = str(model or "").strip().lower()
    if not normalized_model:
        return False
    if normalized_type == "openai_codex":
        return not normalized_model.startswith(("gemini", "claude"))
    if normalized_type == "gemini_official":
        return not normalized_model.startswith(("gpt-", "claude"))
    if normalized_type == "anthropic":
        return not normalized_model.startswith(("gpt-", "gemini"))
    return True


class VisionCaller(ABC):
    @abstractmethod
    async def describe(self, prompt: str, image_url: str) -> str:
        raise NotImplementedError

    async def describe_with_tools(
        self,
        prompt: str,
        image_url: str,
        tools: List[Dict[str, Any]],
        tool_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]] = None,
        max_steps: int = 4,
    ) -> str:
        raise NotImplementedError


class OpenAIVisionCaller(VisionCaller):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = _normalize_openai_base_url(base_url)
        self.model = model
        self.timeout = timeout

    async def describe(self, prompt: str, image_url: str) -> str:
        from openai import AsyncOpenAI
        normalized_image_url, problem = normalize_image_ref(image_url)
        if not normalized_image_url:
            raise ValueError(f"invalid_image_ref:{problem or 'unknown'}")

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as http_client:
            client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                http_client=http_client,
            )
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": normalized_image_url}},
                        ],
                    }
                ],
            )

        message = response.choices[0].message
        return str(_obj_get(message, "content", "") or "").strip()

    async def describe_with_tools(
        self,
        prompt: str,
        image_url: str,
        tools: List[Dict[str, Any]],
        tool_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]] = None,
        max_steps: int = 4,
    ) -> str:
        from openai import AsyncOpenAI
        normalized_image_url, problem = normalize_image_ref(image_url)
        if not normalized_image_url:
            raise ValueError(f"invalid_image_ref:{problem or 'unknown'}")

        messages: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": normalized_image_url}},
                ],
            }
        ]
        latest_text = ""

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as http_client:
            client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                http_client=http_client,
            )

            for _ in range(max(1, int(max_steps))):
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools or None,
                )
                message = response.choices[0].message
                content = str(_obj_get(message, "content", "") or "").strip()
                if content:
                    latest_text = content
                raw_tool_calls = list(_obj_get(message, "tool_calls", []) or [])
                if not raw_tool_calls:
                    return latest_text

                assistant_tool_calls: List[Dict[str, Any]] = []
                for tc in raw_tool_calls:
                    call_id = str(_obj_get(tc, "id", "") or "")
                    func_obj = _obj_get(tc, "function", {}) or {}
                    func_name = str(_obj_get(func_obj, "name", "") or "")
                    func_args = str(_obj_get(func_obj, "arguments", "") or "")
                    assistant_tool_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": func_name,
                                "arguments": func_args,
                            },
                        }
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": content or "",
                        "tool_calls": assistant_tool_calls,
                    }
                )

                for tc in assistant_tool_calls:
                    call_id = str(tc.get("id", "") or "")
                    func_obj = tc.get("function", {}) or {}
                    func_name = str(func_obj.get("name", "") or "")
                    raw_args = str(func_obj.get("arguments", "") or "")
                    try:
                        parsed_args = json.loads(raw_args) if raw_args else {}
                    except Exception:
                        parsed_args = {}
                    tool_result = ""
                    if callable(tool_handler) and func_name:
                        try:
                            tool_result = await tool_handler(func_name, parsed_args)
                        except Exception:
                            tool_result = ""
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": func_name,
                            "content": str(tool_result or ""),
                        }
                    )

        return latest_text


class GeminiVisionCaller(VisionCaller):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").strip()
        self.model = model
        self.timeout = 60.0

    def _request_url(self) -> str:
        raw = (self.base_url or "").strip().rstrip("/")
        if not raw:
            raw = "https://generativelanguage.googleapis.com"
        lower = raw.lower()
        if lower.endswith("/v1beta"):
            base = raw
        elif lower.endswith("/v1"):
            base = f"{raw[:-3]}/v1beta"
        else:
            base = f"{raw}/v1beta"
        return f"{base}/models/{self.model}:generateContent"

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "x-goog-api-key": self.api_key,
        }

    def _gemini_image_part(self, image_url: str) -> Dict[str, Any]:
        normalized_image_url, problem = normalize_image_ref(image_url)
        if not normalized_image_url:
            raise ValueError(f"invalid_image_ref:{problem or 'unknown'}")
        parsed = _split_data_url(normalized_image_url)
        if parsed:
            mime_type, base64_data = parsed
            return {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64_data,
                }
            }
        return {
            "fileData": {
                "mimeType": "image/*",
                "fileUri": normalized_image_url,
            }
        }

    def _request_parts(self, prompt: str, image_url: str) -> List[Dict[str, Any]]:
        return [
            {"text": prompt},
            self._gemini_image_part(image_url),
        ]

    async def _generate_content(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self._request_url()
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
            response = await client.post(
                url,
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return dict(response.json() or {})

    async def describe(self, prompt: str, image_url: str) -> str:
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": self._request_parts(prompt, image_url),
                }
            ]
        }
        response = await self._generate_content(payload)
        candidates = list(_obj_get(response, "candidates", []) or [])
        if not candidates:
            return ""
        content = _obj_get(candidates[0], "content", {})
        parts = list(_obj_get(content, "parts", []) or [])
        return _extract_gemini_text(parts)

    async def describe_with_tools(
        self,
        prompt: str,
        image_url: str,
        tools: List[Dict[str, Any]],
        tool_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]] = None,
        max_steps: int = 4,
    ) -> str:
        tool_payload: List[dict] = []
        if tools:
            tool_payload.append(
                {
                    "functionDeclarations": [
                        _convert_openai_tool_to_gemini(tool)
                        for tool in tools
                    ]
                }
            )
        contents: List[dict] = [
            {
                "role": "user",
                "parts": self._request_parts(prompt, image_url),
            }
        ]
        latest_text = ""
        for _ in range(max(1, int(max_steps))):
            payload: Dict[str, Any] = {"contents": contents}
            if tool_payload:
                payload["tools"] = tool_payload
            response = await self._generate_content(payload)
            candidates = list(_obj_get(response, "candidates", []) or [])
            if not candidates:
                return latest_text
            content = _obj_get(candidates[0], "content", {})
            parts = list(_obj_get(content, "parts", []) or [])
            text = _extract_gemini_text(parts)
            if text:
                latest_text = text
            tool_calls = _extract_gemini_tool_calls(parts)
            if not tool_calls:
                return latest_text
            contents.append(
                {
                    "role": "model",
                    "parts": parts,
                }
            )
            if not callable(tool_handler):
                return latest_text
            for call in tool_calls:
                tool_result = ""
                try:
                    tool_result = await tool_handler(call.name, dict(call.arguments or {}))
                except Exception:
                    tool_result = ""
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": call.name,
                                    "response": {"result": str(tool_result or "")},
                                }
                            }
                        ],
                    }
                )
        return latest_text


class AnthropicVisionCaller(VisionCaller):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").strip()
        self.model = model
        self.timeout = timeout

    async def describe(self, prompt: str, image_url: str) -> str:
        from anthropic import AsyncAnthropic
        normalized_image_url, problem = normalize_image_ref(image_url)
        if not normalized_image_url:
            raise ValueError(f"invalid_image_ref:{problem or 'unknown'}")

        client_kwargs: Dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": self.timeout,
        }
        if self.base_url:
            client_kwargs["base_url"] = self.base_url.rstrip("/")
        client = AsyncAnthropic(**client_kwargs)

        response = await client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        _anthropic_image_block(normalized_image_url),
                    ],
                }
            ],
        )
        texts = [str(_obj_get(block, "text", "")) for block in _obj_get(response, "content", []) if _obj_get(block, "type", "") == "text"]
        return "".join(texts).strip()

    async def describe_with_tools(
        self,
        prompt: str,
        image_url: str,
        tools: List[Dict[str, Any]],
        tool_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]] = None,
        max_steps: int = 4,
    ) -> str:
        logger.warning(
            "AnthropicVisionCaller does not support describe_with_tools; "
            "tool calls will be ignored and falling back to plain describe."
        )
        return await self.describe(prompt, image_url)


class CodexVisionCaller(VisionCaller):
    def __init__(
        self,
        *,
        model: str,
        auth_path: str = "",
        timeout: float = 120.0,
    ) -> None:
        self.model = _normalize_codex_model_name(model)
        self.auth_path = str(auth_path or "").strip()
        self.timeout = timeout
        self._caller = OpenAICodexToolCaller(
            model=self.model,
            auth_path=self.auth_path,
            timeout=self.timeout,
        )

    async def describe(self, prompt: str, image_url: str) -> str:
        normalized_image_url, problem = normalize_image_ref(image_url)
        if not normalized_image_url:
            raise ValueError(f"invalid_image_ref:{problem or 'unknown'}")
        response = await self._caller.chat_with_tools(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": normalized_image_url}},
                    ],
                }
            ],
            [],
            False,
        )
        if response.vision_unavailable:
            raise RuntimeError("codex_vision_unavailable")
        return str(response.content or "").strip()

    async def describe_with_tools(
        self,
        prompt: str,
        image_url: str,
        tools: List[Dict[str, Any]],
        tool_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]] = None,
        max_steps: int = 4,
    ) -> str:
        normalized_image_url, problem = normalize_image_ref(image_url)
        if not normalized_image_url:
            raise ValueError(f"invalid_image_ref:{problem or 'unknown'}")
        messages: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": normalized_image_url}},
                ],
            }
        ]
        latest_text = ""
        for _ in range(max(1, int(max_steps))):
            response = await self._caller.chat_with_tools(messages, tools, False)
            if response.vision_unavailable:
                raise RuntimeError("codex_vision_unavailable")
            text = str(response.content or "").strip()
            if text:
                latest_text = text
            if not response.tool_calls:
                return latest_text
            messages.append(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments or {}, ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            if not callable(tool_handler):
                return latest_text
            for call in response.tool_calls:
                tool_result = ""
                try:
                    tool_result = await tool_handler(call.name, dict(call.arguments or {}))
                except Exception:
                    tool_result = ""
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": str(tool_result or ""),
                    }
                )
        return latest_text


class FallbackVisionCaller(VisionCaller):
    def __init__(self, primary: VisionCaller, fallback: VisionCaller) -> None:
        self.primary = primary
        self.fallback = fallback
        self.model = getattr(primary, "model", "") or getattr(fallback, "model", "")

    async def describe(self, prompt: str, image_url: str) -> str:
        try:
            result = await self.primary.describe(prompt, image_url)
            if str(result or "").strip():
                return str(result).strip()
        except Exception:
            pass
        return await self.fallback.describe(prompt, image_url)

    async def describe_with_tools(
        self,
        prompt: str,
        image_url: str,
        tools: List[Dict[str, Any]],
        tool_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]] = None,
        max_steps: int = 4,
    ) -> str:
        try:
            result = await self.primary.describe_with_tools(
                prompt=prompt,
                image_url=image_url,
                tools=tools,
                tool_handler=tool_handler,
                max_steps=max_steps,
            )
            if str(result or "").strip():
                return str(result).strip()
        except Exception:
            pass
        return await self.fallback.describe_with_tools(
            prompt=prompt,
            image_url=image_url,
            tools=tools,
            tool_handler=tool_handler,
            max_steps=max_steps,
        )


def _vision_signature(api_type: str, model: str, auth_path: str = "", api_url: str = "") -> tuple[str, str, str, str]:
    return (
        str(api_type or "").strip().lower(),
        str(model or "").strip().lower(),
        str(auth_path or "").strip(),
        str(api_url or "").strip(),
    )


def _anthropic_image_block(image_url: str) -> dict:
    parsed = _split_data_url(image_url)
    if parsed:
        mime_type, base64_data = parsed
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": base64_data,
            },
        }
    return {
        "type": "text",
        "text": image_url,
    }


def build_vision_caller(config: Any) -> Optional[VisionCaller]:
    api_type = _normalize_api_type(
        getattr(
            config,
            "personification_labeler_api_type",
            getattr(config, "personification_api_type", "openai"),
        )
    )
    model = str(
        getattr(
            config,
            "personification_labeler_model",
            getattr(config, "personification_model", ""),
        )
        or getattr(config, "personification_model", "")
        or ""
    ).strip()
    auth_path = str(getattr(config, "personification_codex_auth_path", "") or "").strip()

    def _build_single(
        *,
        selected_api_type: str,
        selected_model: str,
        key_attr: str,
        url_attr: str,
    ) -> Optional[VisionCaller]:
        resolved_model = str(selected_model or "").strip()
        if selected_api_type == "openai_codex":
            if resolved_model and not _provider_model_is_compatible(selected_api_type, resolved_model):
                return None
            if not resolved_model or "codex" not in resolved_model.lower():
                resolved_model = _normalize_codex_model_name(
                    str(getattr(config, "personification_model", "") or "").strip() or "gpt-5.3-codex"
                )
            return CodexVisionCaller(
                model=resolved_model,
                auth_path=auth_path,
            )
        if not _provider_model_is_compatible(selected_api_type, resolved_model):
            return None

        api_key = str(getattr(config, key_attr, getattr(config, "personification_api_key", "")) or "").strip()
        if not api_key:
            return None
        api_url = str(getattr(config, url_attr, getattr(config, "personification_api_url", "")) or "").strip()
        if selected_api_type == "anthropic":
            return AnthropicVisionCaller(
                api_key=api_key,
                base_url=api_url,
                model=resolved_model,
            )
        if selected_api_type == "gemini_official":
            return GeminiVisionCaller(
                api_key=api_key,
                base_url=api_url,
                model=resolved_model,
            )
        return OpenAIVisionCaller(
            api_key=api_key,
            base_url=api_url,
            model=resolved_model,
        )

    primary = _build_single(
        selected_api_type=api_type,
        selected_model=model,
        key_attr="personification_labeler_api_key",
        url_attr="personification_labeler_api_url",
    )
    if primary is None:
        return None

    fallback_enabled = bool(getattr(config, "personification_vision_fallback_enabled", True))
    fallback_provider = str(getattr(config, "personification_vision_fallback_provider", "") or "").strip()
    fallback_type = _normalize_api_type(fallback_provider)
    fallback_model = str(
        getattr(config, "personification_vision_fallback_model", "") or ""
    ).strip()
    fallback = None
    primary_signature = _vision_signature(
        api_type,
        model,
        auth_path=auth_path,
        api_url=str(getattr(config, "personification_labeler_api_url", getattr(config, "personification_api_url", "")) or "").strip(),
    )
    fallback_signature = _vision_signature(
        fallback_type,
        fallback_model,
        auth_path=auth_path,
        api_url=str(getattr(config, "personification_labeler_api_url", getattr(config, "personification_api_url", "")) or "").strip(),
    )
    if fallback_enabled and fallback_provider and fallback_model and fallback_signature != primary_signature:
        fallback = _build_single(
            selected_api_type=fallback_type,
            selected_model=fallback_model,
            key_attr="personification_labeler_api_key",
            url_attr="personification_labeler_api_url",
        )
    if fallback is None:
        return primary
    return FallbackVisionCaller(primary, fallback)

