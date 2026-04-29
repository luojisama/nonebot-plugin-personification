from __future__ import annotations

from typing import Any

from plugin.personification.agent.tool_registry import AgentTool

from .impl import generate_image
from .impl import _first_codex_caller
from .impl import normalize_image_generation_size


IMAGE_GEN_DESCRIPTION = """通过 Codex 的 image_generation 托管工具生成一张图片并发送给用户。
仅在用户明确请求“画一张/生成一张/帮我画/P一张”时调用，不要主动使用。
参数：
- prompt：由 LLM 理解需求、参考上下文/检索结果后构建的完整生图提示词，详细说明画面内容、构图、风格、氛围、文字和限制
- size：图片尺寸，由 LLM 根据构图需求选择；支持 1024x1024、1024x1536、1536x1024，也支持 square/portrait/landscape、1:1/3:4/16:9、方图/竖版/横版等别名
要求：只能走 OpenAICodexToolCaller，不使用 OpenAI API 的 images.generate。"""


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

    async def _handler(prompt: str, size: str = "1024x1024") -> str:
        result = await generate_image(
            prompt,
            tool_caller=tool_caller,
            size=normalize_image_generation_size(size),
            image_model=image_model,
            timeout=image_timeout,
        )
        if "error" in result:
            return f"图片生成失败：{str(result['error']).strip() or '工具没有返回图片数据'}"
        b64 = str(result.get("b64_json", "") or "").strip()
        if not b64:
            return "图片生成失败"
        return f"[IMAGE_B64]{b64}[/IMAGE_B64]"

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
            },
            "required": ["prompt"],
        },
        handler=_handler,
    )


def build_tools(runtime: Any) -> list[AgentTool]:
    tool = build_image_gen_tool(runtime)
    return [tool] if tool is not None else []
