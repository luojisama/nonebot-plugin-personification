from __future__ import annotations

from typing import Any

from plugin.personification.agent.tool_registry import AgentTool

from .impl import parallel_research


PARALLEL_RESEARCH_DESCRIPTION = """动态并发研究工具。
当用户的绘图或复杂查询需要同时查图片参考、设定、百科、联网资料时调用。
工具内部会由 LLM 自行规划 0 到多个只读子Agent，并发使用搜索/百科/视觉工具，最后返回 JSON+中文摘要。
不要在普通闲聊里调用；不要把它用于发送消息、修改配置、记忆写入等有副作用任务。"""


def build_tools(runtime: Any) -> list[AgentTool]:
    plugin_config = getattr(runtime, "plugin_config", None)

    async def _handler(
        query: str,
        purpose: str = "image_generation",
        context: str = "",
        focus: list[str] | None = None,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
        max_workers: int | None = None,
    ) -> str:
        if str(purpose or "").strip().lower() == "lookup" and not bool(
            getattr(plugin_config, "personification_parallel_research_lookup_enabled", True)
        ):
            return (
                "<parallel_research_json>\n"
                '{"summary":"查询场景并行研究已关闭。","purpose":"lookup","research_plan":[],'
                '"facts":[],"visual_refs":[],"prompt_hints":[],"must_include":[],"must_avoid":[],'
                '"source_notes":["lookup_disabled_by_config"],"confidence":"low"}\n'
                "</parallel_research_json>\n摘要：查询场景并行研究已关闭。"
            )
        return await parallel_research(
            runtime=runtime,
            query=query,
            purpose=purpose,
            context=context,
            focus=focus,
            images=images,
            image_urls=image_urls,
            max_workers=max_workers,
        )

    return [
        AgentTool(
            name="parallel_research",
            description=PARALLEL_RESEARCH_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "原始需求或研究目标，必须保留用户要画/要查的主体和限制",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "用途：image_generation 或 lookup；默认 image_generation",
                        "default": "image_generation",
                    },
                    "context": {
                        "type": "string",
                        "description": "可选上下文，如对话背景、用途、已有结论、需要避免的歧义",
                    },
                    "focus": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选研究重点，如 character、brand、visual_style、canon_setting",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选参考图片引用",
                    },
                    "image_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选参考图片 URL；等同 images",
                    },
                    "max_workers": {
                        "type": "integer",
                        "description": "允许 planner 启动的最大子Agent数，上限由配置和代码限制保护",
                        "default": 6,
                    },
                },
                "required": ["query"],
            },
            handler=_handler,
            enabled=lambda: bool(
                getattr(plugin_config, "personification_parallel_research_enabled", True)
            ),
        )
    ]
