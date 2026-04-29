from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from plugin.personification.agent.tool_registry import AgentTool, ToolRegistry
from plugin.personification.core.web_grounding import do_web_search
from plugin.personification.skills.skillpacks.acg_resolver.scripts import impl as acg_impl
from plugin.personification.skills.skillpacks.resource_collector.scripts import impl as resource_impl
from plugin.personification.skills.skillpacks.vision_analyze.scripts import impl as vision_impl
from plugin.personification.skills.skillpacks.wiki_search.scripts import impl as wiki_impl
from plugin.personification.skills.skillpacks.wiki_search.scripts.main import resolve_wiki_runtime_config


_HARD_MAX_WORKERS = 6
_DEFAULT_WORKER_TIMEOUT_SECONDS = 35.0
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 90.0
_DEFAULT_MAX_TOOL_ROUNDS = 2
_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "web_search",
        "search_web",
        "search_images",
        "collect_resources",
        "wiki_lookup",
        "resolve_acg_entity",
        "vision_analyze",
    }
)


@dataclass(slots=True)
class ResearchWorkerPlan:
    role: str
    goal: str
    focus: list[str]
    preferred_tools: list[str]


class _SilentLogger:
    def debug(self, _msg: str) -> None:
        return None

    def info(self, _msg: str) -> None:
        return None

    def warning(self, _msg: str) -> None:
        return None


def _logger(runtime: Any) -> Any:
    return getattr(runtime, "logger", None) or _SilentLogger()


def _normalize_int(value: Any, *, default: int, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))


def _normalize_float(value: Any, *, default: float, lower: float, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))


def _merge_image_refs(images: list[str] | None = None, image_urls: list[str] | None = None) -> list[str]:
    refs: list[str] = []
    for item in list(images or []) + list(image_urls or []):
        value = str(item or "").strip()
        if value and value not in refs:
            refs.append(value)
    return refs[:3]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _tool_schema(tool: AgentTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters if isinstance(tool.parameters, dict) else {"type": "object", "properties": {}},
        },
    }


def _sanitize_tool_args(tool: AgentTool, args: dict[str, Any]) -> dict[str, Any]:
    params = tool.parameters if isinstance(tool.parameters, dict) else {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    if not isinstance(properties, dict) or not properties:
        return {}
    return {key: value for key, value in dict(args or {}).items() if key in properties}


def _resolve_github_token(runtime: Any) -> str:
    plugin_config = getattr(runtime, "plugin_config", None)
    token = str(getattr(plugin_config, "personification_github_token", "") or "").strip()
    if token:
        return token
    for env_name in ("PERSONIFICATION_GITHUB_TOKEN", "GITHUB_TOKEN", "GITHUB_API_TOKEN"):
        token = str(os.getenv(env_name, "") or "").strip()
        if token:
            return token
    return ""


def _web_search_enabled(plugin_config: Any) -> bool:
    return bool(getattr(plugin_config, "personification_tool_web_search_enabled", True)) and str(
        getattr(plugin_config, "personification_tool_web_search_mode", "enabled") or "enabled"
    ).strip().lower() != "disabled"


async def _with_http_client(runtime: Any, callback):
    shared_client = getattr(runtime, "http_client", None)
    if shared_client is not None:
        return await callback(shared_client)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_client:
        return await callback(http_client)


def _build_readonly_registry(runtime: Any) -> ToolRegistry:
    logger = _logger(runtime)
    plugin_config = getattr(runtime, "plugin_config", None)
    github_token = _resolve_github_token(runtime)
    tool_caller = getattr(runtime, "tool_caller", None)
    wiki_enabled, _fandom_enabled, extra_fandom_wikis = resolve_wiki_runtime_config(plugin_config)
    registry = ToolRegistry()

    async def _augment_query_with_images(
        query: str,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        final_query = str(query or "").strip()
        refs = _merge_image_refs(images, image_urls)
        if not refs:
            return final_query
        try:
            visual = await vision_impl.analyze_images(
                runtime=runtime,
                query="为联网搜索提取图片中的主体、文字、人物、作品名和关键视觉线索。",
                images=refs,
            )
        except Exception as exc:
            logger.debug(f"[parallel_research] visual query augmentation skipped: {exc}")
            return final_query
        if visual:
            final_query = f"{final_query} 图像线索：{str(visual)[:300]}".strip()
        return final_query

    async def _web_search_handler(query: str, images: list[str] | None = None, image_urls: list[str] | None = None) -> str:
        final_query = await _augment_query_with_images(query, images, image_urls)
        return await do_web_search(final_query, get_now=getattr(runtime, "get_now", lambda: None), logger=logger)

    async def _search_web_handler(
        query: str,
        limit: int = 5,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        augmented_query = await _augment_query_with_images(query, images, image_urls)

        async def _call(http_client: httpx.AsyncClient) -> str:
            return await resource_impl.search_web(
                augmented_query,
                limit=_normalize_int(limit, default=5, lower=1, upper=10),
                http_client=http_client,
                logger=logger,
            )

        return await _with_http_client(runtime, _call)

    async def _search_images_handler(
        query: str,
        limit: int = 5,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        augmented_query = await _augment_query_with_images(query, images, image_urls)

        async def _call(http_client: httpx.AsyncClient) -> str:
            return await resource_impl.search_images(
                augmented_query,
                limit=_normalize_int(limit, default=5, lower=1, upper=10),
                http_client=http_client,
                logger=logger,
            )

        return await _with_http_client(runtime, _call)

    async def _collect_resources_handler(
        query: str,
        resource_type: str = "通用资源",
        max_count: int = 5,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        augmented_query = await _augment_query_with_images(query, images, image_urls)

        async def _call(http_client: httpx.AsyncClient) -> str:
            return await resource_impl.collect_resources(
                augmented_query,
                resource_type=resource_type,
                max_count=_normalize_int(max_count, default=5, lower=1, upper=10),
                http_client=http_client,
                logger=logger,
                github_token=github_token,
                tool_caller=tool_caller,
            )

        return await _with_http_client(runtime, _call)

    async def _wiki_lookup_handler(query: str) -> str:
        async def _call(http_client: httpx.AsyncClient) -> str:
            return await wiki_impl.wiki_lookup(
                str(query or ""),
                extra_fandom_wikis=extra_fandom_wikis,
                http_client=http_client,
                logger=logger,
            )

        return await _with_http_client(runtime, _call)

    async def _resolve_acg_entity_handler(
        query: str,
        image_context: bool = False,
        images: list[str] | None = None,
        visual_hints: dict[str, Any] | None = None,
    ) -> str:
        return await acg_impl.resolve_acg_entity(
            runtime=runtime,
            query=query,
            image_context=image_context,
            images=images,
            visual_hints=visual_hints,
        )

    async def _vision_analyze_handler(
        query: str,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        return await vision_impl.analyze_images(
            runtime=runtime,
            query=query,
            images=images,
            image_urls=image_urls,
        )

    def _register(tool: AgentTool) -> None:
        if tool.name in _READ_ONLY_TOOL_NAMES:
            registry.register(tool)

    _register(
        AgentTool(
            name="web_search",
            description="快速联网搜索并返回摘要，适合事实查证、背景资料和实时信息。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "image_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
            handler=_web_search_handler,
            enabled=lambda: _web_search_enabled(plugin_config),
        )
    )
    _register(
        AgentTool(
            name="search_web",
            description="结构化网页搜索，返回 JSON 结果列表。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "image_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
            handler=_search_web_handler,
        )
    )
    _register(
        AgentTool(
            name="search_images",
            description="搜索图片/视觉参考/海报构图等图像资料，返回 JSON 结果列表。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "image_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
            handler=_search_images_handler,
        )
    )
    _register(
        AgentTool(
            name="collect_resources",
            description="按资源需求搜集并整理网页、图片、官方资料或社区资源。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "resource_type": {"type": "string"},
                    "max_count": {"type": "integer"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "image_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
            handler=_collect_resources_handler,
        )
    )
    _register(
        AgentTool(
            name="wiki_lookup",
            description="查询维基百科、萌娘百科和可选 Fandom Wiki，适合角色、作品、设定和术语资料。",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            handler=_wiki_lookup_handler,
            enabled=lambda: wiki_enabled,
        )
    )
    _register(
        AgentTool(
            name="resolve_acg_entity",
            description="对动漫、游戏、角色、作品名、术语等高歧义实体做证据式消解。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "image_context": {"type": "boolean"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "visual_hints": {"type": "object"},
                },
                "required": ["query"],
            },
            handler=_resolve_acg_entity_handler,
        )
    )
    _register(
        AgentTool(
            name="vision_analyze",
            description="分析用户给出的参考图，提取主体、文字、人物、视觉线索和不确定性。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "image_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
            handler=_vision_analyze_handler,
        )
    )
    return registry


async def _call_llm_json(
    *,
    tool_caller: Any,
    messages: list[dict[str, Any]],
    timeout: float,
) -> dict[str, Any] | None:
    if tool_caller is None:
        return None
    try:
        response = await asyncio.wait_for(
            tool_caller.chat_with_tools(messages, [], False),
            timeout=timeout,
        )
    except Exception:
        return None
    if getattr(response, "tool_calls", None):
        return None
    return _extract_json_object(str(getattr(response, "content", "") or ""))


def _fallback_plan(query: str, purpose: str, focus: list[str], max_workers: int) -> list[ResearchWorkerPlan]:
    plans: list[ResearchWorkerPlan] = []
    normalized_purpose = str(purpose or "").strip().lower()
    if normalized_purpose == "image_generation":
        plans.append(
            ResearchWorkerPlan(
                role="visual_reference",
                goal=f"为绘图需求搜集视觉参考、构图、颜色、服装、物件和画面风格：{query}",
                focus=["visual_style", *focus],
                preferred_tools=["search_images", "vision_analyze", "web_search"],
            )
        )
    plans.append(
        ResearchWorkerPlan(
            role="facts_and_setting",
            goal=f"核对主体、人物、品牌、作品、设定和不能画错的事实：{query}",
            focus=["facts", "canon_setting", *focus],
            preferred_tools=["wiki_lookup", "resolve_acg_entity", "web_search", "search_web"],
        )
    )
    return plans[:max_workers]


def _normalize_worker_plans(data: dict[str, Any] | None, *, query: str, purpose: str, focus: list[str], max_workers: int) -> list[ResearchWorkerPlan]:
    raw_workers = data.get("workers") if isinstance(data, dict) else None
    plans: list[ResearchWorkerPlan] = []
    if isinstance(raw_workers, list):
        for item in raw_workers:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "") or "").strip()[:48]
            goal = str(item.get("goal", "") or item.get("query", "") or "").strip()
            if not role or not goal:
                continue
            raw_focus = item.get("focus", [])
            worker_focus = [str(value).strip() for value in raw_focus if str(value).strip()] if isinstance(raw_focus, list) else []
            raw_tools = item.get("preferred_tools", [])
            tools = []
            if isinstance(raw_tools, list):
                for value in raw_tools:
                    name = str(value or "").strip()
                    if name in _READ_ONLY_TOOL_NAMES and name not in tools:
                        tools.append(name)
            plans.append(
                ResearchWorkerPlan(
                    role=role,
                    goal=goal[:600],
                    focus=worker_focus[:8],
                    preferred_tools=tools[:5],
                )
            )
            if len(plans) >= max_workers:
                break
    if plans or (isinstance(data, dict) and data.get("workers") == []):
        return plans
    return _fallback_plan(query, purpose, focus, max_workers)


async def _plan_workers(
    *,
    query: str,
    purpose: str,
    context: str,
    focus: list[str],
    images: list[str],
    tool_caller: Any,
    max_workers: int,
    timeout: float,
) -> list[ResearchWorkerPlan]:
    data = await _call_llm_json(
        tool_caller=tool_caller,
        timeout=timeout,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是并行研究任务规划器。根据用户需求决定是否需要启动研究子Agent，"
                    f"最多 {max_workers} 个。数量、角色和目标完全由你按需求决定；如果不需要研究，workers=[]。"
                    "只能规划只读研究任务，不要规划生成图片、发消息、写配置或记忆。"
                    "严格输出 JSON："
                    '{"workers":[{"role":"短英文或拼音角色名","goal":"具体研究目标","focus":["重点"],'
                    '"preferred_tools":["web_search|search_web|search_images|collect_resources|wiki_lookup|resolve_acg_entity|vision_analyze"]}],'
                    '"reason":"极短原因"}'
                ),
            },
            {
                "role": "user",
                "content": _json_dumps(
                    {
                        "query": query,
                        "purpose": purpose,
                        "context": context,
                        "focus": focus,
                        "has_images": bool(images),
                    }
                ),
            },
        ],
    )
    return _normalize_worker_plans(data, query=query, purpose=purpose, focus=focus, max_workers=max_workers)


def _build_tool_result_message(tool_caller: Any, tool_call_id: str, tool_name: str, result: str) -> dict[str, Any]:
    builder = getattr(tool_caller, "build_tool_result_message", None)
    if callable(builder):
        try:
            return builder(tool_call_id, tool_name, result)
        except Exception:
            pass
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": str(result or ""),
    }


async def _execute_tool_call(
    *,
    registry: ToolRegistry,
    tool_call: Any,
    default_images: list[str],
) -> tuple[str, str, str]:
    tool_name = str(getattr(tool_call, "name", "") or "").strip()
    tool_id = str(getattr(tool_call, "id", "") or tool_name or "tool-call").strip()
    tool = registry.get(tool_name)
    if tool is None or tool_name not in _READ_ONLY_TOOL_NAMES:
        return tool_id, tool_name, f"工具 {tool_name} 不在 parallel_research 只读白名单内"
    try:
        if not tool.enabled():
            return tool_id, tool_name, f"工具 {tool_name} 当前未启用"
    except Exception:
        return tool_id, tool_name, f"工具 {tool_name} 启用状态检查失败"
    args = _sanitize_tool_args(tool, dict(getattr(tool_call, "arguments", {}) or {}))
    if default_images:
        params = tool.parameters if isinstance(tool.parameters, dict) else {}
        properties = params.get("properties", {}) if isinstance(params, dict) else {}
        if isinstance(properties, dict):
            if "images" in properties and "images" not in args:
                args["images"] = list(default_images)
            if "image_urls" in properties and "image_urls" not in args:
                args["image_urls"] = list(default_images)
    try:
        result = await tool.handler(**args)
    except Exception as exc:
        result = f"工具调用失败：{exc}"
    return tool_id, tool_name, str(result or "")


async def _run_worker(
    *,
    plan: ResearchWorkerPlan,
    query: str,
    purpose: str,
    context: str,
    images: list[str],
    tool_caller: Any,
    registry: ToolRegistry,
    max_tool_rounds: int,
) -> dict[str, Any]:
    active_tools = [
        tool
        for tool in registry.active()
        if tool.name in _READ_ONLY_TOOL_NAMES
        and (not plan.preferred_tools or tool.name in plan.preferred_tools)
    ]
    if not active_tools:
        active_tools = [tool for tool in registry.active() if tool.name in _READ_ONLY_TOOL_NAMES]
    schemas = [_tool_schema(tool) for tool in active_tools]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是 parallel_research 的一个只读研究子Agent。"
                "你只能围绕自己的角色和目标做资料查询，不要生成图片，不要聊天，不要写执行过程。"
                "工具调用结束后严格输出 JSON："
                '{"role":"","goal":"","findings":["..."],"facts":["..."],"visual_refs":["..."],'
                '"prompt_hints":["..."],"must_include":["..."],"must_avoid":["..."],'
                '"source_notes":["..."],"confidence":"low|medium|high"}'
            ),
        },
        {
            "role": "user",
            "content": _json_dumps(
                {
                    "role": plan.role,
                    "goal": plan.goal,
                    "focus": plan.focus,
                    "preferred_tools": plan.preferred_tools,
                    "user_query": query,
                    "purpose": purpose,
                    "context": context,
                    "has_images": bool(images),
                }
            ),
        },
    ]
    last_content = ""
    for _round in range(max_tool_rounds + 1):
        response = await tool_caller.chat_with_tools(messages, schemas, False)
        content = str(getattr(response, "content", "") or "").strip()
        tool_calls = list(getattr(response, "tool_calls", []) or [])
        if not tool_calls:
            payload = _extract_json_object(content)
            if payload is not None:
                payload.setdefault("role", plan.role)
                payload.setdefault("goal", plan.goal)
                return payload
            return {
                "role": plan.role,
                "goal": plan.goal,
                "findings": [content] if content else [],
                "facts": [],
                "visual_refs": [],
                "prompt_hints": [],
                "must_include": [],
                "must_avoid": [],
                "source_notes": ["worker_returned_plain_text"] if content else ["worker_returned_empty"],
                "confidence": "low" if not content else "medium",
            }
        if _round >= max_tool_rounds:
            break
        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": str(getattr(call, "id", "") or ""),
                        "type": "function",
                        "function": {
                            "name": str(getattr(call, "name", "") or ""),
                            "arguments": _json_dumps(dict(getattr(call, "arguments", {}) or {})),
                        },
                    }
                    for call in tool_calls
                ],
            }
        )
        executed = await asyncio.gather(
            *[
                _execute_tool_call(
                    registry=registry,
                    tool_call=tool_call,
                    default_images=images,
                )
                for tool_call in tool_calls
            ],
            return_exceptions=True,
        )
        for item in executed:
            if isinstance(item, Exception):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": "failed-tool-call",
                        "name": "unknown",
                        "content": f"工具调用失败：{item}",
                    }
                )
                continue
            tool_id, tool_name, result = item
            last_content = result
            messages.append(_build_tool_result_message(tool_caller, tool_id, tool_name, result[:4000]))
    return {
        "role": plan.role,
        "goal": plan.goal,
        "findings": [last_content[:800]] if last_content else [],
        "facts": [],
        "visual_refs": [],
        "prompt_hints": [],
        "must_include": [],
        "must_avoid": [],
        "source_notes": ["worker_reached_tool_round_limit"],
        "confidence": "low",
    }


def _fallback_aggregate(*, query: str, purpose: str, plans: list[ResearchWorkerPlan], worker_results: list[dict[str, Any]], notes: list[str]) -> dict[str, Any]:
    facts: list[str] = []
    visual_refs: list[str] = []
    prompt_hints: list[str] = []
    must_include: list[str] = []
    must_avoid: list[str] = []
    source_notes = list(notes)
    for result in worker_results:
        for key, target in (
            ("facts", facts),
            ("visual_refs", visual_refs),
            ("prompt_hints", prompt_hints),
            ("must_include", must_include),
            ("must_avoid", must_avoid),
            ("source_notes", source_notes),
        ):
            values = result.get(key, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                for value in values:
                    text = str(value or "").strip()
                    if text and text not in target:
                        target.append(text)
        findings = result.get("findings", [])
        if isinstance(findings, str):
            findings = [findings]
        if isinstance(findings, list):
            for value in findings[:3]:
                text = str(value or "").strip()
                if text and text not in facts:
                    facts.append(text[:280])
    return {
        "summary": f"已围绕「{query}」完成并行研究。" if worker_results else f"「{query}」未启动额外研究。",
        "purpose": purpose,
        "research_plan": [
            {"role": plan.role, "goal": plan.goal, "focus": plan.focus, "preferred_tools": plan.preferred_tools}
            for plan in plans
        ],
        "facts": facts[:10],
        "visual_refs": visual_refs[:10],
        "prompt_hints": prompt_hints[:10],
        "must_include": must_include[:10],
        "must_avoid": must_avoid[:10],
        "source_notes": source_notes[:12],
        "confidence": "medium" if worker_results else "low",
    }


async def _aggregate_results(
    *,
    query: str,
    purpose: str,
    context: str,
    plans: list[ResearchWorkerPlan],
    worker_results: list[dict[str, Any]],
    notes: list[str],
    tool_caller: Any,
    timeout: float,
) -> dict[str, Any]:
    fallback = _fallback_aggregate(
        query=query,
        purpose=purpose,
        plans=plans,
        worker_results=worker_results,
        notes=notes,
    )
    payload = await _call_llm_json(
        tool_caller=tool_caller,
        timeout=timeout,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是并行研究结果聚合器。基于用户需求、研究计划和各子Agent结果，"
                    "合并成给外层 LLM 使用的稳定 JSON。不要编造来源，不要掩盖不确定性。"
                    "严格输出 JSON，字段：summary,purpose,research_plan,facts,visual_refs,"
                    "prompt_hints,must_include,must_avoid,source_notes,confidence。"
                ),
            },
            {
                "role": "user",
                "content": _json_dumps(
                    {
                        "query": query,
                        "purpose": purpose,
                        "context": context,
                        "fallback_shape": fallback,
                        "worker_results": worker_results,
                    }
                ),
            },
        ],
    )
    if not isinstance(payload, dict):
        return fallback
    merged = dict(fallback)
    for key in (
        "summary",
        "facts",
        "visual_refs",
        "prompt_hints",
        "must_include",
        "must_avoid",
        "source_notes",
        "confidence",
    ):
        if key in payload:
            merged[key] = payload[key]
    merged["purpose"] = purpose
    merged["research_plan"] = fallback["research_plan"]
    return merged


def _render_result(payload: dict[str, Any]) -> str:
    summary = str(payload.get("summary", "") or "").strip()
    return (
        "<parallel_research_json>\n"
        f"{_json_dumps(payload)}\n"
        "</parallel_research_json>\n"
        f"摘要：{summary or '已完成并行研究。'}"
    )


async def parallel_research(
    *,
    runtime: Any,
    query: str,
    purpose: str = "image_generation",
    context: str = "",
    focus: list[str] | None = None,
    images: list[str] | None = None,
    image_urls: list[str] | None = None,
    max_workers: int | None = None,
) -> str:
    plugin_config = getattr(runtime, "plugin_config", None)
    tool_caller = getattr(runtime, "tool_caller", None)
    query_text = str(query or "").strip()
    if not query_text:
        return _render_result(
            {
                "summary": "没有收到研究目标。",
                "purpose": str(purpose or "lookup"),
                "research_plan": [],
                "facts": [],
                "visual_refs": [],
                "prompt_hints": [],
                "must_include": [],
                "must_avoid": [],
                "source_notes": ["missing_query"],
                "confidence": "low",
            }
        )
    config_max_workers = _normalize_int(
        getattr(plugin_config, "personification_parallel_research_max_workers", 6),
        default=6,
        lower=0,
        upper=_HARD_MAX_WORKERS,
    )
    effective_max_workers = _normalize_int(
        max_workers if max_workers is not None else config_max_workers,
        default=config_max_workers,
        lower=0,
        upper=min(config_max_workers, _HARD_MAX_WORKERS),
    )
    worker_timeout = _normalize_float(
        getattr(plugin_config, "personification_parallel_research_worker_timeout", _DEFAULT_WORKER_TIMEOUT_SECONDS),
        default=_DEFAULT_WORKER_TIMEOUT_SECONDS,
        lower=5.0,
        upper=180.0,
    )
    total_timeout = _normalize_float(
        getattr(plugin_config, "personification_parallel_research_total_timeout", _DEFAULT_TOTAL_TIMEOUT_SECONDS),
        default=_DEFAULT_TOTAL_TIMEOUT_SECONDS,
        lower=10.0,
        upper=300.0,
    )
    max_tool_rounds = _normalize_int(
        getattr(plugin_config, "personification_parallel_research_max_tool_rounds", _DEFAULT_MAX_TOOL_ROUNDS),
        default=_DEFAULT_MAX_TOOL_ROUNDS,
        lower=0,
        upper=4,
    )
    focus_items = [str(item or "").strip() for item in list(focus or []) if str(item or "").strip()][:12]
    image_refs = _merge_image_refs(images, image_urls)
    purpose_text = str(purpose or "image_generation").strip() or "image_generation"
    context_text = str(context or "").strip()[:1200]

    started_at = time.monotonic()
    notes: list[str] = []
    if effective_max_workers <= 0:
        plans = []
        notes.append("max_workers_zero")
    else:
        planner_timeout = min(12.0, max(4.0, total_timeout * 0.18))
        plans = await _plan_workers(
            query=query_text,
            purpose=purpose_text,
            context=context_text,
            focus=focus_items,
            images=image_refs,
            tool_caller=tool_caller,
            max_workers=effective_max_workers,
            timeout=planner_timeout,
        )
    registry = _build_readonly_registry(runtime)
    worker_results: list[dict[str, Any]] = []
    if plans and tool_caller is not None:
        remaining_total = max(1.0, total_timeout - (time.monotonic() - started_at))
        tasks = [
            asyncio.create_task(
                asyncio.wait_for(
                    _run_worker(
                        plan=plan,
                        query=query_text,
                        purpose=purpose_text,
                        context=context_text,
                        images=image_refs,
                        tool_caller=tool_caller,
                        registry=registry,
                        max_tool_rounds=max_tool_rounds,
                    ),
                    timeout=worker_timeout,
                )
            )
            for plan in plans
        ]
        try:
            raw_results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=remaining_total,
            )
        except asyncio.TimeoutError:
            raw_results = []
            notes.append("parallel_research_total_timeout")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for index, item in enumerate(raw_results):
            if isinstance(item, Exception):
                role = plans[index].role if index < len(plans) else f"worker_{index + 1}"
                notes.append(f"{role}: {type(item).__name__}: {item}")
                continue
            if isinstance(item, dict):
                worker_results.append(item)
    elif plans and tool_caller is None:
        notes.append("tool_caller_unavailable")

    if not plans:
        return _render_result(
            _fallback_aggregate(
                query=query_text,
                purpose=purpose_text,
                plans=plans,
                worker_results=worker_results,
                notes=notes,
            )
        )

    remaining_for_aggregate = max(3.0, total_timeout - (time.monotonic() - started_at))
    aggregate = await _aggregate_results(
        query=query_text,
        purpose=purpose_text,
        context=context_text,
        plans=plans,
        worker_results=worker_results,
        notes=notes,
        tool_caller=tool_caller,
        timeout=min(15.0, remaining_for_aggregate),
    )
    return _render_result(aggregate)


__all__ = [
    "ResearchWorkerPlan",
    "parallel_research",
]
