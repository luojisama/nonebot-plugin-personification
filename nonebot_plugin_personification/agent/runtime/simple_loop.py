"""轻量工具调用循环。

与 `runner.py:run_agent` 不同，这里只做最纯粹的"多步工具调用 → 纯文本输出"，
不携带群聊回复的语义与副作用（意图推断、query 改写、ack 发送、persona 包装、
pending_actions / ActionExecutor、按 output_mode 截断等）。

用于发空间说说这类"主动创作一段短文本"的场景：让生成过程也能像群聊那样按需
调用 web_search 等真实工具查证内容，但不会误触发往群里发消息/贴表情等副作用。
"""

from __future__ import annotations

import json
from typing import Any

from ..tool_registry import ToolRegistry
from .executor import _execute_tool_with_retries
from .tool_catalog import select_tool_schemas


async def run_tool_loop_text(
    messages: list[dict],
    *,
    registry: ToolRegistry,
    tool_caller: Any,
    logger: Any,
    max_steps: int = 4,
    use_builtin_search: bool = False,
    has_images: bool = False,
    chat_intent: str = "",
) -> str:
    """驱动一个最小工具调用循环，返回模型最终的纯文本内容。

    每一步把可用工具 schema 交给 ``tool_caller.chat_with_tools``；若模型返回了
    ``tool_calls`` 就逐个执行并把结果回填进 ``messages`` 继续下一步，直到模型不再
    调用工具或步数耗尽。``messages`` 会被原地追加（assistant tool_calls 消息与
    tool 结果消息），调用方应传入一次性构造的列表。
    """
    last_content = ""
    for _step in range(max(1, int(max_steps))):
        active_schemas = select_tool_schemas(
            registry,
            has_images=has_images,
            chat_intent=chat_intent,
        )
        response = await tool_caller.chat_with_tools(
            messages,
            active_schemas,
            use_builtin_search,
        )
        content = str(getattr(response, "content", "") or "").strip()
        if content:
            last_content = content
        tool_calls = list(getattr(response, "tool_calls", []) or [])
        if not tool_calls:
            return content or last_content

        # 回填 assistant tool_calls 消息：content 必须用空字符串而非 None，
        # 部分严格 provider（Rust 反序列化）会把 null 当作缺失字段直接 400。
        messages.append(
            {
                "role": "assistant",
                "content": response.content if response.content else "",
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.name,
                            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                        },
                    }
                    for tool_call in tool_calls
                ],
            }
        )

        for tool_call in tool_calls:
            logger.info(f"[qzone-tool] tool_call name={tool_call.name}")
            tool = registry.get(tool_call.name)
            if tool is None:
                result = f"工具 {tool_call.name} 不存在"
            else:
                _tool_args, result = await _execute_tool_with_retries(
                    registry=registry,
                    tool_name=tool_call.name,
                    tool_args=dict(tool_call.arguments or {}),
                    rewritten_query=None,
                    user_images=[],
                    logger=logger,
                )
            messages.append(
                tool_caller.build_tool_result_message(
                    tool_call.id,
                    tool_call.name,
                    result,
                )
            )

    return last_content
