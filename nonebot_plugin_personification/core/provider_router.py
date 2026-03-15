import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from openai import AsyncOpenAI

from .web_grounding import (
    WEB_SEARCH_TOOL_ANTHROPIC,
    WEB_SEARCH_TOOL_ANTHROPIC_NATIVE,
    WEB_SEARCH_TOOL_GEMINI,
    WEB_SEARCH_TOOL_OPENAI,
)


PROVIDER_FAILURE_STATE: Dict[str, Dict[str, Any]] = {}
PROVIDER_ROTATION_CURSOR = 0


def normalize_api_type(api_type: Optional[str]) -> str:
    value = (api_type or "openai").strip().lower()
    if value == "gemini_official":
        return "gemini"
    if value not in {"openai", "gemini", "anthropic"}:
        return "openai"
    return value


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _provider_timeout(provider: Dict[str, Any]) -> float:
    default_timeout = 120 if provider["api_type"] in {"gemini", "anthropic"} else 60
    try:
        return float(provider.get("timeout", default_timeout))
    except (TypeError, ValueError):
        return float(default_timeout)


def _provider_max_retries(provider: Dict[str, Any]) -> int:
    return max(1, _to_int(provider.get("max_retries", 2), 2))


def load_api_pool_config(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    raw_config = getattr(plugin_config, "personification_api_pools", None)
    if not raw_config:
        return []

    parsed: Any = raw_config
    if isinstance(raw_config, str):
        try:
            parsed = json.loads(raw_config)
        except json.JSONDecodeError as e:
            logger.error(f"拟人插件：解析 personification_api_pools 失败: {e}")
            return []

    if not isinstance(parsed, list):
        logger.error("拟人插件：personification_api_pools 必须是 JSON 数组")
        return []

    providers: List[Dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        api_key = str(item.get("api_key", "")).strip()
        api_url = str(item.get("api_url", "")).strip()
        model = str(item.get("model", "")).strip()
        if not api_key or not api_url or not model:
            continue

        api_type = normalize_api_type(item.get("api_type"))
        provider = {
            "name": str(item.get("name") or f"pool_{index + 1}").strip() or f"pool_{index + 1}",
            "api_type": api_type,
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
            "enabled": _to_bool(item.get("enabled", True), True),
            "priority": _to_int(item.get("priority", index), index),
            "timeout": _provider_timeout({"api_type": api_type, **item}),
            "max_retries": max(1, _to_int(item.get("max_retries", 2), 2)),
            "supports_native_search": _to_bool(
                item.get("supports_native_search", api_type in {"gemini", "anthropic"}),
                api_type in {"gemini", "anthropic"},
            ),
        }
        if provider["enabled"]:
            providers.append(provider)

    providers.sort(key=lambda p: (p["priority"], p["name"]))
    return providers


def get_configured_api_providers(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    providers = load_api_pool_config(plugin_config, logger)
    if providers:
        return providers

    api_key = plugin_config.personification_api_key.strip()
    if not api_key:
        return []

    legacy_type = normalize_api_type(plugin_config.personification_api_type)
    return [
        {
            "name": "legacy_primary",
            "api_type": legacy_type,
            "api_url": plugin_config.personification_api_url.strip(),
            "api_key": api_key,
            "model": plugin_config.personification_model,
            "enabled": True,
            "priority": 0,
            "timeout": 120 if legacy_type in {"gemini", "anthropic"} else 60,
            "max_retries": 2,
            "supports_native_search": legacy_type in {"gemini", "anthropic"},
        }
    ]


def get_provider_candidates(plugin_config: Any, logger: Any) -> List[Dict[str, Any]]:
    global PROVIDER_ROTATION_CURSOR

    providers = get_configured_api_providers(plugin_config, logger)
    if not providers:
        return []

    now_ts = time.time()
    available: List[Dict[str, Any]] = []
    cooling: List[Dict[str, Any]] = []
    for provider in providers:
        state = PROVIDER_FAILURE_STATE.get(provider["name"], {})
        if state.get("cooldown_until", 0) > now_ts:
            cooling.append(provider)
        else:
            available.append(provider)

    if not available:
        available = cooling

    if len(available) <= 1:
        return available

    cursor = PROVIDER_ROTATION_CURSOR % len(available)
    PROVIDER_ROTATION_CURSOR = (PROVIDER_ROTATION_CURSOR + 1) % len(available)
    return available[cursor:] + available[:cursor]


def _mark_provider_success(provider_name: str) -> None:
    PROVIDER_FAILURE_STATE.pop(provider_name, None)


def _mark_provider_failure(provider_name: str, error: Exception) -> None:
    now_ts = time.time()
    state = PROVIDER_FAILURE_STATE.get(provider_name, {})
    failures = int(state.get("failures", 0)) + 1
    PROVIDER_FAILURE_STATE[provider_name] = {
        "failures": failures,
        "cooldown_until": now_ts + min(300, 30 * failures),
        "last_error": str(error),
        "last_failed_at": now_ts,
    }


def _sanitize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    allowed_keys = {"role", "content", "name", "tool_calls", "tool_call_id"}
    return [{k: v for k, v in msg.items() if k in allowed_keys} for msg in messages]


def _normalize_openai_base_url(provider: Dict[str, Any]) -> str:
    api_url = provider["api_url"].strip()
    if provider["api_type"] == "gemini" and "api.openai.com" in api_url:
        return "https://generativelanguage.googleapis.com/v1beta/openai/"
    if "generativelanguage.googleapis.com" not in api_url and not api_url.endswith(("/v1", "/v1/")):
        return api_url.rstrip("/") + "/v1"
    return api_url


def _split_data_url(data_url: str) -> Optional[Tuple[str, str]]:
    if not data_url.startswith("data:") or ";base64," not in data_url:
        return None
    mime_type, base64_data = data_url.split(";base64,", 1)
    return mime_type.replace("data:", "", 1), base64_data


def _gemini_parts_from_content(content: Any) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                parts.append({"text": str(item)})
                continue
            if item.get("type") == "text":
                parts.append({"text": item.get("text", "")})
            elif item.get("type") == "image_url":
                image_url = item.get("image_url", {}).get("url", "")
                parsed = _split_data_url(image_url)
                if parsed:
                    mime_type, base64_data = parsed
                    parts.append({"inline_data": {"mime_type": mime_type, "data": base64_data}})
                else:
                    parts.append({"text": "[image omitted: remote URL unsupported by Gemini]"})
            elif "functionCall" in item or "functionResponse" in item:
                parts.append(item)
            elif "text" in item:
                parts.append({"text": str(item["text"])})
    elif isinstance(content, dict):
        if "functionCall" in content or "functionResponse" in content:
            parts.append(content)
        elif "text" in content:
            parts.append({"text": str(content["text"])})
        else:
            parts.append({"text": json.dumps(content, ensure_ascii=False)})
    else:
        parts.append({"text": str(content)})
    return parts or [{"text": ""}]


def _convert_messages_to_gemini(messages: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    system_instruction = None
    gemini_contents: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        parts = _gemini_parts_from_content(msg.get("content", ""))
        if role == "system":
            system_instruction = {"parts": parts}
        elif role == "assistant":
            gemini_contents.append({"role": "model", "parts": parts})
        else:
            gemini_contents.append({"role": "user", "parts": parts})
    return system_instruction, gemini_contents


def _extract_gemini_text(parts: List[Dict[str, Any]]) -> str:
    texts: List[str] = []
    for part in parts:
        if part.get("thought", False):
            continue
        if part.get("text"):
            texts.append(part["text"])
    content = "".join(texts)
    content = re.sub(r"<thought>.*?</thought>", "", content, flags=re.DOTALL)
    content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
    content = re.sub(r"```thinking\s*.*?```", "", content, flags=re.DOTALL)
    return content.strip()


def _anthropic_blocks_from_content(content: Any) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                blocks.append({"type": "text", "text": str(item)})
                continue
            item_type = item.get("type")
            if item_type == "text":
                blocks.append({"type": "text", "text": item.get("text", "")})
            elif item_type == "image_url":
                image_url = item.get("image_url", {}).get("url", "")
                parsed = _split_data_url(image_url)
                if parsed:
                    mime_type, base64_data = parsed
                    blocks.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime_type, "data": base64_data},
                        }
                    )
                else:
                    blocks.append({"type": "text", "text": "[image omitted: remote URL unsupported by Anthropic]"})
            elif item_type in {"tool_use", "tool_result", "text"}:
                blocks.append(item)
            elif "text" in item:
                blocks.append({"type": "text", "text": str(item["text"])})
    elif isinstance(content, dict):
        if content.get("type") in {"tool_use", "tool_result", "text"}:
            blocks.append(content)
        elif "text" in content:
            blocks.append({"type": "text", "text": str(content["text"])})
        else:
            blocks.append({"type": "text", "text": json.dumps(content, ensure_ascii=False)})
    else:
        blocks.append({"type": "text", "text": str(content)})
    return blocks or [{"type": "text", "text": ""}]


def _convert_messages_to_anthropic(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    system_parts: List[str] = []
    anthropic_messages: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.append(str(msg.get("content", "")))
            continue
        anthropic_messages.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": _anthropic_blocks_from_content(msg.get("content", "")),
            }
        )
    return "\n\n".join(part for part in system_parts if part), anthropic_messages


async def _call_openai_provider(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    plugin_config: Any,
    do_web_search: Callable[[str], Awaitable[str]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: Optional[int] = None,
    temperature: float = 0.7,
) -> Optional[str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(_provider_timeout(provider), connect=10.0)) as http_client:
        client = AsyncOpenAI(
            api_key=provider["api_key"],
            base_url=_normalize_openai_base_url(provider),
            http_client=http_client,
        )

        current_messages = _sanitize_messages(messages)
        openai_tools = list(tools) if tools else []
        if plugin_config.personification_web_search:
            if provider.get("supports_native_search", False):
                openai_tools.append({"type": "web_search_preview"})
            else:
                openai_tools.append(WEB_SEARCH_TOOL_OPENAI)

        for _ in range(5):
            call_params: Dict[str, Any] = {
                "model": provider["model"],
                "messages": current_messages,
                "temperature": temperature,
            }
            if max_tokens:
                call_params["max_tokens"] = max_tokens
            if openai_tools:
                call_params["tools"] = openai_tools
                call_params["tool_choice"] = "auto"

            response = await client.chat.completions.create(**call_params)
            if isinstance(response, str):
                return response.strip()

            message = response.choices[0].message
            if message.tool_calls:
                current_messages.append(message.model_dump(exclude_none=True))
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments or "{}")
                    result = (
                        await do_web_search(tool_args.get("query", ""))
                        if tool_name == "web_search"
                        else f"Error: tool {tool_name} not found."
                    )
                    current_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": result,
                        }
                    )
                continue

            return (message.content or "").strip()

    return None


async def _call_gemini_provider(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    plugin_config: Any,
    do_web_search: Callable[[str], Awaitable[str]],
    max_tokens: Optional[int] = None,
    temperature: float = 0.7,
) -> Optional[str]:
    api_url = provider["api_url"].strip()
    if "generateContent" not in api_url:
        if not api_url.endswith("/"):
            api_url += "/"
        if "models/" not in api_url:
            api_url += f"v1beta/models/{provider['model']}:generateContent"
        else:
            api_url += ":generateContent"

    if "key=" not in api_url:
        connector = "&" if "?" in api_url else "?"
        api_url = f"{api_url}{connector}key={provider['api_key']}"

    headers = {"Content-Type": "application/json"}
    current_messages = _sanitize_messages(messages)

    for _ in range(5):
        system_instruction, gemini_contents = _convert_messages_to_gemini(current_messages)
        payload: Dict[str, Any] = {
            "contents": gemini_contents,
            "generationConfig": {"temperature": temperature},
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        if max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = max_tokens
        if plugin_config.personification_thinking_budget > 0:
            payload["generationConfig"]["thinkingConfig"] = {
                "includeThoughts": plugin_config.personification_include_thoughts,
                "thinkingBudget": plugin_config.personification_thinking_budget,
            }
        if plugin_config.personification_web_search:
            if provider.get("supports_native_search", True):
                payload["tools"] = [{"googleSearch": {}}]
            else:
                payload["tools"] = [{"functionDeclarations": [WEB_SEARCH_TOOL_GEMINI]}]

        async with httpx.AsyncClient(timeout=httpx.Timeout(_provider_timeout(provider), connect=20.0)) as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        function_calls = [part["functionCall"] for part in parts if "functionCall" in part]
        if function_calls:
            current_messages.append({"role": "assistant", "content": [{"functionCall": call} for call in function_calls]})
            for function_call in function_calls:
                tool_name = function_call.get("name", "")
                args = function_call.get("args", {}) or {}
                result = (
                    await do_web_search(args.get("query", ""))
                    if tool_name == "web_search"
                    else f"Error: tool {tool_name} not found."
                )
                current_messages.append(
                    {
                        "role": "user",
                        "content": [{"functionResponse": {"name": tool_name, "response": {"result": result}}}],
                    }
                )
            continue

        text = _extract_gemini_text(parts)
        if text:
            return text

    return None


async def _call_anthropic_provider(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    plugin_config: Any,
    do_web_search: Callable[[str], Awaitable[str]],
    max_tokens: Optional[int] = None,
    temperature: float = 0.7,
) -> Optional[str]:
    api_url = provider["api_url"].strip()
    if not api_url.endswith("/v1/messages"):
        api_url = api_url.rstrip("/") + "/v1/messages"

    headers = {
        "content-type": "application/json",
        "x-api-key": provider["api_key"],
        "anthropic-version": "2023-06-01",
    }
    use_native_search = provider.get("supports_native_search", True)
    if plugin_config.personification_web_search and use_native_search:
        headers["anthropic-beta"] = "web-search-2025-03-05"
    current_messages = _sanitize_messages(messages)

    for _ in range(5):
        system_text, anthropic_messages = _convert_messages_to_anthropic(current_messages)
        payload: Dict[str, Any] = {
            "model": provider["model"],
            "messages": anthropic_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 1024,
        }
        if system_text:
            payload["system"] = system_text
        if plugin_config.personification_web_search:
            if use_native_search:
                payload["tools"] = [WEB_SEARCH_TOOL_ANTHROPIC_NATIVE]
            else:
                payload["tools"] = [WEB_SEARCH_TOOL_ANTHROPIC]

        async with httpx.AsyncClient(timeout=httpx.Timeout(_provider_timeout(provider), connect=20.0)) as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        content_blocks = data.get("content", [])
        tool_uses = [block for block in content_blocks if block.get("type") == "tool_use"]
        if tool_uses and not use_native_search:
            current_messages.append({"role": "assistant", "content": tool_uses})
            tool_results: List[Dict[str, Any]] = []
            for tool_use in tool_uses:
                tool_name = tool_use.get("name", "")
                tool_input = tool_use.get("input", {}) or {}
                result = (
                    await do_web_search(tool_input.get("query", ""))
                    if tool_name == "web_search"
                    else f"Error: tool {tool_name} not found."
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.get("id", ""),
                        "content": result,
                    }
                )
            current_messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(block.get("text", "") for block in content_blocks if block.get("type") == "text").strip()
        if text:
            return text

    return None


async def call_ai_api(
    messages: List[Dict[str, Any]],
    *,
    plugin_config: Any,
    logger: Any,
    do_web_search: Callable[[str], Awaitable[str]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: Optional[int] = None,
    temperature: float = 0.7,
) -> Optional[str]:
    """通用 AI 调用函数，支持多 provider 轮询与工具调用。"""
    providers = get_provider_candidates(plugin_config, logger)
    if not providers:
        logger.warning("拟人插件：未配置可用的 API provider，跳过调用")
        return None

    errors: List[str] = []
    for provider in providers:
        logger.info(f"拟人插件：尝试 provider={provider['name']} type={provider['api_type']} model={provider['model']}")
        retries = _provider_max_retries(provider)
        for attempt in range(retries):
            try:
                if provider["api_type"] == "gemini":
                    reply = await _call_gemini_provider(
                        provider,
                        messages,
                        plugin_config=plugin_config,
                        do_web_search=do_web_search,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                elif provider["api_type"] == "anthropic":
                    reply = await _call_anthropic_provider(
                        provider,
                        messages,
                        plugin_config=plugin_config,
                        do_web_search=do_web_search,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                else:
                    reply = await _call_openai_provider(
                        provider,
                        messages,
                        plugin_config=plugin_config,
                        do_web_search=do_web_search,
                        tools=tools,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )

                if reply:
                    _mark_provider_success(provider["name"])
                    return reply
                raise RuntimeError("empty response")
            except Exception as e:
                errors.append(f"{provider['name']}#{attempt + 1}: {e}")
                logger.warning(f"拟人插件：provider {provider['name']} 调用失败 ({attempt + 1}/{retries}): {e}")
                if attempt + 1 >= retries:
                    _mark_provider_failure(provider["name"], e)
                else:
                    await asyncio.sleep(min(2, attempt + 1))
        logger.warning(f"拟人插件：切换到下一个 provider，当前失败 provider={provider['name']}")

    if errors:
        logger.error("拟人插件：所有 provider 调用失败: " + " | ".join(errors))
    return None
