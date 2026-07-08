from __future__ import annotations

from typing import Any

from plugin.personification.agent.tool_registry import AgentTool

from .impl import _first_codex_caller, _first_gemini_cli_caller, generate_image
from .impl import normalize_image_generation_size


IMAGE_GEN_DESCRIPTION = """通过当前绘图路由生成一张图片并发送给用户。
仅在用户明确请求“画一张/生成一张/帮我画/P一张”时调用，不要主动使用。
参数：
- prompt：由 LLM 理解需求、参考上下文/检索结果后构建的完整生图提示词，详细说明画面内容、构图、风格、氛围、文字和限制
- size：图片尺寸，由 LLM 根据构图需求选择；支持 1024x1024、1024x1536、1536x1024，也支持 square/portrait/landscape、1:1/3:4/16:9、方图/竖版/横版等别名
- images/image_urls：可选参考图；当用户带图要求“按这张/参考这张/改成”时保留原图引用
- reference_mode：auto 默认尝试直传参考图；vision_prompt 表示只使用视觉理解后的文字提示；off 表示不使用参考图
路由：同一个 generate_image 工具会按配置自动切换 Codex image_generation、Gemini/Antigravity CLI、OpenAI-compatible Images API 或 Gemini generateContent 图片接口。"""


def _image_route_available(runtime: Any) -> bool:
    plugin_config = getattr(runtime, "plugin_config", None)
    tool_caller = getattr(runtime, "tool_caller", None) or getattr(runtime, "agent_tool_caller", None)
    if _first_codex_caller(tool_caller) is not None or _first_gemini_cli_caller(tool_caller) is not None:
        return True
    api_key = str(getattr(plugin_config, "personification_image_gen_api_key", "") or "").strip()
    api_url = str(getattr(plugin_config, "personification_image_gen_api_url", "") or "").strip()
    main_key = str(getattr(plugin_config, "personification_api_key", "") or "").strip()
    main_url = str(getattr(plugin_config, "personification_api_url", "") or "").strip()
    api_type = str(getattr(plugin_config, "personification_image_gen_api_type", "auto") or "auto").strip().lower()
    main_type = str(getattr(plugin_config, "personification_api_type", "") or "").strip().lower()
    if api_type in {"openai", "gemini"} and (api_key or main_key):
        return True
    if api_key and (api_url or main_url):
        return True
    return bool(main_key and main_type in {"openai", "gemini", "gemini_official"})


def build_image_gen_tool(runtime: Any) -> AgentTool | None:
    plugin_config = getattr(runtime, "plugin_config", None)
    if not bool(getattr(plugin_config, "personification_image_gen_enabled", True)):
        return None
    if not _image_route_available(runtime):
        return None
    tool_caller = getattr(runtime, "tool_caller", None) or getattr(runtime, "agent_tool_caller", None)
    image_model = str(
        getattr(plugin_config, "personification_image_gen_model", "gpt-image-2")
        or "gpt-image-2"
    ).strip() or "gpt-image-2"
    image_timeout = float(getattr(plugin_config, "personification_image_gen_timeout", 180) or 180)

    async def _handler(
        prompt: str,
        size: str = "1024x1024",
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
        reference_mode: str = "auto",
    ) -> str:
        result = await generate_image(
            prompt,
            tool_caller=tool_caller,
            size=normalize_image_generation_size(size),
            image_model=image_model,
            timeout=image_timeout,
            images=images,
            image_urls=image_urls,
            reference_mode=reference_mode,
            plugin_config=plugin_config,
        )
        if "error" in result:
            return f"图片生成失败：{str(result['error']).strip() or '工具没有返回图片数据'}"
        b64 = str(result.get("b64_json", "") or "").strip()
        if not b64:
            return "图片生成失败"
        warning = str(result.get("warning", "") or "").strip()
        prefix = f"{warning}\n" if warning else ""
        return f"{prefix}[IMAGE_B64]{b64}[/IMAGE_B64]"

    return AgentTool(
        name="generate_image",
        description=IMAGE_GEN_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "完整生图提示词，包含画面内容、构图、风格、参考信息和限制"},
                "size": {
                    "type": "string",
                    "description": "尺寸或别名：1024x1024/square/1:1/方图，1024x1536/portrait/3:4/竖版，1536x1024/landscape/16:9/横版",
                    "default": "1024x1024",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选参考图片引用，支持 URL、data URL 或绝对本地路径",
                },
                "image_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选参考图片 URL；等同 images",
                },
                "reference_mode": {
                    "type": "string",
                    "description": "参考图模式：auto/input_image 尝试直传，vision_prompt 仅使用视觉摘要，off 不使用参考图",
                    "default": "auto",
                },
            },
            "required": ["prompt"],
        },
        handler=_handler,
    )


def build_image_gen_nanobanan_tool(runtime: Any) -> AgentTool | None:
    """Compatibility shim for old imports; the public tool is now generate_image."""
    return build_image_gen_tool(runtime)


def build_tools(runtime: Any) -> list[AgentTool]:
    tool = build_image_gen_tool(runtime)
    return [tool] if tool is not None else []
