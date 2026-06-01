from __future__ import annotations

from typing import Any

from plugin.personification.agent.tool_registry import AgentTool

from .impl import generate_image
from .impl import _first_codex_caller
from .impl import normalize_image_generation_size
from .nanobanan import _first_gemini_cli_caller, generate_image_nanobanan


IMAGE_GEN_DESCRIPTION = """通过 Codex 的 image_generation 托管工具生成一张图片并发送给用户。
仅在用户明确请求“画一张/生成一张/帮我画/P一张”时调用，不要主动使用。
参数：
- prompt：由 LLM 理解需求、参考上下文/检索结果后构建的完整生图提示词，详细说明画面内容、构图、风格、氛围、文字和限制
- size：图片尺寸，由 LLM 根据构图需求选择；支持 1024x1024、1024x1536、1536x1024，也支持 square/portrait/landscape、1:1/3:4/16:9、方图/竖版/横版等别名
- images/image_urls：可选参考图；当用户带图要求“按这张/参考这张/改成”时保留原图引用
- reference_mode：auto 默认尝试把参考图直传给 Codex image_generation；vision_prompt 表示只使用视觉理解后的文字提示；off 表示不使用参考图
要求：只能走 OpenAICodexToolCaller，不使用 OpenAI API 的 images.generate。"""


NANOBANAN_DESCRIPTION = """通过 Gemini/Antigravity CLI 的 Nano Banana（Gemini 3 Pro Image）生成一张图片并发送给用户。
仅在用户明确请求“画一张/生成一张/帮我画/P一张”时调用，不要主动使用。
参数：
- prompt：完整生图提示词，详细说明画面内容、构图、风格、氛围、文字和限制
- size：图片尺寸；与 Codex 的 generate_image 保持同样别名
- images/image_urls：可选参考图；URL 与 data URL 都支持
要求：仅在 personification_api_type=gemini_cli 或 antigravity_cli 时可用。"""


def build_image_gen_tool(runtime: Any) -> AgentTool | None:
    if not bool(getattr(getattr(runtime, "plugin_config", None), "personification_image_gen_enabled", True)):
        return None
    tool_caller = getattr(runtime, "tool_caller", None) or getattr(runtime, "agent_tool_caller", None)
    if _first_codex_caller(tool_caller) is None:
        return None
    image_model = str(
        getattr(getattr(runtime, "plugin_config", None), "personification_image_gen_model", "gpt-image-2")
        or "gpt-image-2"
    ).strip() or "gpt-image-2"
    image_timeout = float(
        getattr(getattr(runtime, "plugin_config", None), "personification_image_gen_timeout", 180)
        or 180
    )

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
    if not bool(getattr(getattr(runtime, "plugin_config", None), "personification_image_gen_enabled", True)):
        return None
    tool_caller = getattr(runtime, "tool_caller", None) or getattr(runtime, "agent_tool_caller", None)
    if _first_gemini_cli_caller(tool_caller) is None:
        return None
    image_model = str(
        getattr(
            getattr(runtime, "plugin_config", None),
            "personification_image_gen_nanobanan_model",
            "gemini-3-pro-image-preview",
        )
        or "gemini-3-pro-image-preview"
    ).strip() or "gemini-3-pro-image-preview"
    image_timeout = float(
        getattr(getattr(runtime, "plugin_config", None), "personification_image_gen_timeout", 180)
        or 180
    )

    async def _handler(
        prompt: str,
        size: str = "1024x1024",
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        result = await generate_image_nanobanan(
            prompt,
            tool_caller=tool_caller,
            size=normalize_image_generation_size(size),
            image_model=image_model,
            timeout=image_timeout,
            images=images,
            image_urls=image_urls,
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
        name="generate_image_nanobanan",
        description=NANOBANAN_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "完整生图提示词，包含画面内容、构图、风格、参考信息和限制"},
                "size": {
                    "type": "string",
                    "description": "尺寸或别名，同 generate_image",
                    "default": "1024x1024",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选参考图片，支持 URL、data URL 或绝对本地路径",
                },
                "image_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选参考图片 URL；等同 images",
                },
            },
            "required": ["prompt"],
        },
        handler=_handler,
    )


def build_tools(runtime: Any) -> list[AgentTool]:
    out: list[AgentTool] = []
    codex_tool = build_image_gen_tool(runtime)
    if codex_tool is not None:
        out.append(codex_tool)
    nanobanan_tool = build_image_gen_nanobanan_tool(runtime)
    if nanobanan_tool is not None:
        out.append(nanobanan_tool)
    return out
