from __future__ import annotations

import os

import httpx

from plugin.personification.agent.tool_registry import AgentTool
from . import impl
from ...vision_analyze.scripts import impl as vision_impl


def _resolve_github_token(runtime) -> str:
    plugin_config = getattr(runtime, "plugin_config", None)
    config_token = str(getattr(plugin_config, "personification_github_token", "") or "").strip()
    if config_token:
        return config_token
    for env_name in ("PERSONIFICATION_GITHUB_TOKEN", "GITHUB_TOKEN", "GITHUB_API_TOKEN"):
        token = str(os.getenv(env_name, "") or "").strip()
        if token:
            return token
    return ""


def _merge_image_refs(images=None, image_urls=None) -> list[str]:  # noqa: ANN001
    refs: list[str] = []
    for item in list(images or []) + list(image_urls or []):
        value = str(item or "").strip()
        if value and value not in refs:
            refs.append(value)
    return refs[:3]


def _compact_visual_search_context(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        import json

        payload = json.loads(text)
    except Exception:
        return text[:300]
    if not isinstance(payload, dict):
        return text[:300]
    parts: list[str] = []
    summary = str(payload.get("scene_summary", "") or "").strip()
    if summary:
        parts.append(summary)
    for item in list(payload.get("ocr_text") or [])[:4]:
        value = str(item or "").strip()
        if value:
            parts.append(f"文字:{value}")
    for item in list(payload.get("characters_or_entities") or [])[:4]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        evidence = str(item.get("evidence", "") or "").strip()
        if name:
            parts.append(f"{name} {evidence}".strip())
    return "；".join(parts)[:300]


def build_tools(runtime):
    logger = getattr(runtime, "logger", None) or _SilentLogger()
    shared_client = getattr(runtime, "http_client", None)
    github_token = _resolve_github_token(runtime)
    tool_caller = getattr(runtime, "tool_caller", None)

    async def _with_client(callback):
        if shared_client is not None:
            return await callback(shared_client)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_client:
            return await callback(http_client)

    async def _augment_query_with_images(query: str, images=None, image_urls=None) -> str:  # noqa: ANN001
        refs = _merge_image_refs(images, image_urls)
        if not refs:
            return str(query or "").strip()
        try:
            visual_raw = await vision_impl.analyze_images(
                runtime=runtime,
                query="为资源/网页搜索提取图片中的主体、文字、人物、作品名和关键视觉线索。",
                images=refs,
            )
        except Exception as exc:
            logger.debug(f"[resource_collector] visual query augmentation skipped: {exc}")
            return str(query or "").strip()
        visual_context = _compact_visual_search_context(visual_raw)
        if not visual_context:
            return str(query or "").strip()
        return f"{str(query or '').strip()} 图像线索：{visual_context}".strip()

    async def _confirm_handler(raw_query: str = "", context_hint: str = "") -> str:
        result = await impl.confirm_resource_request(
            raw_query,
            context_hint,
            tool_caller=tool_caller,
            logger=logger,
        )
        return impl.dumps_confirmation_payload(result)

    async def _collect_handler(
        query: str,
        resource_type: str = "通用资源",
        max_count: int = 5,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        augmented_query = await _augment_query_with_images(query, images, image_urls)

        async def _call(http_client: httpx.AsyncClient) -> str:
            return await impl.collect_resources(
                augmented_query,
                resource_type=resource_type,
                max_count=max_count,
                http_client=http_client,
                logger=logger,
                github_token=github_token,
                tool_caller=tool_caller,
            )

        return await _with_client(_call)

    async def _search_web_handler(
        query: str,
        limit: int = 5,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        augmented_query = await _augment_query_with_images(query, images, image_urls)

        async def _call(http_client: httpx.AsyncClient) -> str:
            return await impl.search_web(
                augmented_query,
                limit=limit,
                http_client=http_client,
                logger=logger,
            )

        return await _with_client(_call)

    async def _search_official_site_handler(query: str, limit: int = 5) -> str:
        async def _call(http_client: httpx.AsyncClient) -> str:
            return await impl.search_official_site(
                query,
                limit=limit,
                http_client=http_client,
                logger=logger,
            )

        return await _with_client(_call)

    async def _search_images_handler(
        query: str,
        limit: int = 5,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        augmented_query = await _augment_query_with_images(query, images, image_urls)

        async def _call(http_client: httpx.AsyncClient) -> str:
            return await impl.search_images(
                augmented_query,
                limit=limit,
                http_client=http_client,
                logger=logger,
            )

        return await _with_client(_call)

    async def _search_github_repos_handler(query: str, limit: int = 5, sort: str = "best_match") -> str:
        async def _call(http_client: httpx.AsyncClient) -> str:
            return await impl.search_github_repos(
                query,
                limit=limit,
                sort=sort,
                http_client=http_client,
                logger=logger,
                github_token=github_token,
            )

        return await _with_client(_call)

    return [
        AgentTool(
            name="search_web",
            description="获取结构化网页搜索结果。更适合找入口页、资料页、链接列表并返回 JSON 结果。若是普通事实查证、实时信息或定义解释，优先使用 web_search/wiki_lookup；若用户明确是在找攻略、教程、资源合集，优先使用 collect_resources。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索主题或问题"},
                    "limit": {"type": "integer", "description": "最多返回结果数，1 到 10", "default": 5},
                    "images": {"type": "array", "items": {"type": "string"}, "description": "可选图片引用；用于先提取视觉线索再搜索"},
                    "image_urls": {"type": "array", "items": {"type": "string"}, "description": "可选图片 URL；等同 images"},
                },
                "required": ["query"],
            },
            handler=_search_web_handler,
        ),
        AgentTool(
            name="search_github_repos",
            description="搜索 GitHub 仓库并返回结构化 JSON。适合查仓库地址、作者项目、按 stars 排序找热门仓库等场景。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "仓库名、作者名或搜索语句"},
                    "limit": {"type": "integer", "description": "最多返回结果数，1 到 10", "default": 5},
                    "sort": {
                        "type": "string",
                        "description": "排序方式，best_match 或 stars",
                        "enum": ["best_match", "stars"],
                        "default": "best_match",
                    },
                },
                "required": ["query"],
            },
            handler=_search_github_repos_handler,
        ),
        AgentTool(
            name="search_official_site",
            description="搜索官网或官方入口并返回结构化 JSON。适合查官网、官方网站、产品主页、活动入口。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "实体名称或官网查询语句"},
                    "limit": {"type": "integer", "description": "最多返回结果数，1 到 10", "default": 5},
                },
                "required": ["query"],
            },
            handler=_search_official_site_handler,
        ),
        AgentTool(
            name="search_images",
            description="搜索图片、壁纸、插画等资源并返回结构化 JSON。适合找壁纸、原画、设定图、图集入口。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "图片或壁纸搜索主题"},
                    "limit": {"type": "integer", "description": "最多返回结果数，1 到 10", "default": 5},
                    "images": {"type": "array", "items": {"type": "string"}, "description": "可选图片引用；用于先提取视觉线索再搜索"},
                    "image_urls": {"type": "array", "items": {"type": "string"}, "description": "可选图片 URL；等同 images"},
                },
                "required": ["query"],
            },
            handler=_search_images_handler,
        ),
        AgentTool(
            name="confirm_resource_request",
            description="在你无法从上下文准确判断用户要找什么资源时，用于生成澄清信息。不是必须先调用。",
            parameters={
                "type": "object",
                "properties": {
                    "raw_query": {"type": "string", "description": "用户原始描述"},
                    "context_hint": {"type": "string", "description": "可选的上下文提示"},
                },
                "required": ["raw_query"],
            },
            handler=_confirm_handler,
        ),
        AgentTool(
            name="collect_resources",
            description="资源搜集主工具。会先理解用户意图，生成搜索计划，再执行搜索并整合结果，适合攻略、教程、资料页、资源站、链接合集等场景。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "已确认的搜索主题"},
                    "resource_type": {"type": "string", "description": "资源类型描述", "default": "通用资源"},
                    "max_count": {"type": "integer", "description": "最多返回数量，1 到 10", "default": 5},
                    "images": {"type": "array", "items": {"type": "string"}, "description": "可选图片引用；用于先提取视觉线索再搜集资源"},
                    "image_urls": {"type": "array", "items": {"type": "string"}, "description": "可选图片 URL；等同 images"},
                },
                "required": ["query"],
            },
            handler=_collect_handler,
        ),
    ]


class _SilentLogger:
    def debug(self, _msg: str) -> None:
        return None
