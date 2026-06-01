from __future__ import annotations

import asyncio
import json
import re
from typing import Any, List

from ..query_rewriter import ContextualQueryRewrite
from ..tool_registry import ToolRegistry
from ...core.error_utils import log_exception
from .executor import _execute_tool_with_retries
from .tool_selection import _select_tool_schemas
from .wrappers import (
    _IMAGE_B64_TOOL_RESULT_RE,
    _IMAGE_GENERATION_TOOL_NAME,
    _format_image_generation_failure,
)


_IMAGE_GENERATION_BACKGROUND_MAX_STEPS = 5
_IMAGE_GENERATION_STATUS_TIMEOUT_SECONDS = 8.0


def _extract_image_b64_tool_result(result_text: str) -> str:
    match = _IMAGE_B64_TOOL_RESULT_RE.search(str(result_text or ""))
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(1) or "").strip()


def _can_send_background_image(executor: Any) -> bool:
    return bool(
        callable(getattr(executor, "send_image_b64", None))
        and callable(getattr(executor, "send_text", None))
    )


def _is_likely_delivered_send_timeout(exc: Exception) -> bool:
    # NapCat 在底层 QQ NT sendMsg 超过内部 5s 阈值时会抛 retcode=1200 invoke timeout，
    # 但消息通常已经实际下发，再补一句"发送失败"会误导用户。
    info = getattr(exc, "info", None)
    if not isinstance(info, dict):
        return False
    if int(info.get("retcode") or 0) == 1200:
        return True
    haystack = f"{info.get('message', '')} {info.get('wording', '')}".lower()
    return "invoke timeout" in haystack


def _can_start_background_image_generation(
    *,
    registry: ToolRegistry,
    executor: Any,
    user_request: str,
) -> bool:
    return bool(
        _can_send_background_image(executor)
        and registry.get(_IMAGE_GENERATION_TOOL_NAME) is not None
        and str(user_request or "").strip()
    )


def _clone_messages_for_background(messages: List[dict]) -> list[dict]:
    return [dict(message) for message in list(messages or []) if isinstance(message, dict)]


def _clean_image_generation_status_reply(text: str) -> str:
    reply = _IMAGE_B64_TOOL_RESULT_RE.sub("", str(text or "")).strip()
    if reply in {"[NO_REPLY]", "<NO_REPLY>", "[SILENCE]", "<SILENCE>"}:
        return reply
    return reply


async def _generate_image_generation_status_reply(
    *,
    tool_caller: Any,
    messages: List[dict],
    user_request: str,
    logger: Any,
) -> str:
    status_messages = _clone_messages_for_background(messages)
    status_messages.append(
        {
            "role": "system",
            "content": (
                "当前用户的需求会进入图片生成流程。"
                "你只负责按人设和当前对话，给用户一句自然、很短的即时回应。"
                "不要固定模板，不要解释工具流程，不要暴露后台、检索、prompt、模型等实现细节。"
                "如果此刻不适合回复，可以只输出 [NO_REPLY]。"
                f"\n用户图片需求：{str(user_request or '').strip()[:500]}"
            ),
        }
    )
    try:
        response = await asyncio.wait_for(
            tool_caller.chat_with_tools(
                status_messages,
                [],
                False,
            ),
            timeout=_IMAGE_GENERATION_STATUS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log_exception(logger, "[agent] image generation status reply failed", exc, level="debug")
        return ""
    if getattr(response, "tool_calls", None):
        return ""
    return _clean_image_generation_status_reply(str(getattr(response, "content", "") or ""))


def _build_background_image_generation_messages(
    *,
    messages: List[dict],
    user_request: str,
) -> list[dict]:
    planning_messages = _clone_messages_for_background(messages)
    planning_messages.append(
        {
            "role": "system",
            "content": (
                "你现在负责准备并执行图片生成，不是在写聊天回复。"
                "先理解用户真正想要的画面、主体、风格、用途、文字和限制。"
                "如有必要，优先调用 parallel_research 并行聚合图片参考、百科/设定和联网资料；"
                "也可自行调用联网搜索、图片搜索、资源搜集、百科/实体解析、视觉理解等工具补充事实和参考图线索；"
                "如果需求已经足够明确，也可以不检索。"
                "拿到足够上下文后，必须调用 generate_image。"
                "调用 generate_image 时，由你组装完整、可执行的生图 prompt，"
                "把检索/参考图得到的关键信息消化进画面描述，不要只原样转述用户一句话。"
                "size 也由你根据构图需求从工具允许值中选择。"
                "不要向用户输出制作步骤；图片生成失败或需要澄清时，才输出一句自然说明。"
                "如果调用了 parallel_research 或其他上下文工具，请先阅读结果，"
                "把 must_include、must_avoid、prompt_hints 等关键约束吸收到最终 prompt，再调用 generate_image。"
                f"\n用户图片需求：{str(user_request or '').strip()[:1000]}"
            ),
        }
    )
    return planning_messages


async def _send_background_text(executor: Any, text: str, logger: Any) -> None:
    content = str(text or "").strip()
    if not content:
        return
    try:
        await executor.send_text(content)
    except Exception as exc:
        log_exception(logger, "[agent] background image text send failed", exc, level="warning")


async def _run_background_image_generation(
    *,
    registry: ToolRegistry,
    executor: Any,
    tool_caller: Any,
    messages: List[dict],
    user_request: str,
    rewritten_query: ContextualQueryRewrite | None,
    user_images: list[str],
    use_builtin_search: bool,
    logger: Any,
) -> None:
    if registry.get(_IMAGE_GENERATION_TOOL_NAME) is None:
        await _send_background_text(executor, "图片生成失败：工具不可用", logger)
        return
    background_messages = _build_background_image_generation_messages(
        messages=messages,
        user_request=user_request,
    )
    last_tool_name = ""
    last_tool_result_text = ""
    for step in range(_IMAGE_GENERATION_BACKGROUND_MAX_STEPS):
        active_schemas = _select_tool_schemas(
            registry,
            has_images=bool(user_images),
            chat_intent="image_generation",
            plugin_question_intent="",
        )
        try:
            response = await tool_caller.chat_with_tools(
                background_messages,
                active_schemas,
                use_builtin_search,
            )
        except Exception as exc:
            log_exception(logger, "[agent] background image planning failed", exc, level="warning")
            await _send_background_text(executor, f"图片生成失败：{exc}", logger)
            return
        content = str(getattr(response, "content", "") or "").strip()
        tool_calls = list(getattr(response, "tool_calls", []) or [])
        logger.info(
            f"[agent] background_image step={step + 1} "
            f"tool_calls={len(tool_calls)} content_len={len(content)}"
        )
        if not tool_calls:
            if content:
                await _send_background_text(executor, content, logger)
            else:
                await _send_background_text(executor, "图片生成失败：模型没有完成图片生成", logger)
            return
        background_messages.append(
            {
                "role": "assistant",
                "content": content,
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
            logger.info(f"[agent] background_image tool_call name={tool_call.name}")
            tool_args, result = await _execute_tool_with_retries(
                registry=registry,
                tool_name=tool_call.name,
                tool_args=dict(tool_call.arguments or {}),
                rewritten_query=None,
                user_images=user_images,
                previous_tool_name=last_tool_name,
                previous_tool_result_text=last_tool_result_text,
                logger=logger,
                budget_deadline=None,
            )
            _ = tool_args
            last_tool_name = str(tool_call.name or "").strip()
            if str(result or "").strip():
                last_tool_result_text = str(result).strip()
            logger.info(
                f"[agent] background_image tool_result name={tool_call.name} "
                f"preview={str(result).replace(chr(10), ' ')[:220]}"
            )
            if last_tool_name == _IMAGE_GENERATION_TOOL_NAME:
                image_b64 = _extract_image_b64_tool_result(str(result or ""))
                if image_b64:
                    try:
                        await executor.send_image_b64(image_b64)
                    except Exception as exc:
                        log_exception(logger, "[agent] background image send failed", exc, level="warning")
                        if not _is_likely_delivered_send_timeout(exc):
                            await _send_background_text(executor, "图片生成好了，但发送图片失败了", logger)
                    return
                await _send_background_text(executor, _format_image_generation_failure(str(result or "")), logger)
                return
            background_messages.append(
                tool_caller.build_tool_result_message(
                    tool_call.id,
                    tool_call.name,
                    result,
                )
            )
    if last_tool_result_text:
        await _send_background_text(executor, "图片生成失败：模型没有继续完成生图调用", logger)
        return
    await _send_background_text(executor, "图片生成失败：模型没有开始生图调用", logger)


def _start_background_image_generation(
    *,
    registry: ToolRegistry,
    executor: Any,
    tool_caller: Any,
    messages: List[dict],
    user_request: str,
    rewritten_query: ContextualQueryRewrite | None,
    user_images: list[str],
    use_builtin_search: bool,
    logger: Any,
) -> bool:
    request_text = str(user_request or "").strip()
    if not _can_start_background_image_generation(
        registry=registry,
        executor=executor,
        user_request=request_text,
    ):
        return False
    task = asyncio.create_task(
        _run_background_image_generation(
            registry=registry,
            executor=executor,
            tool_caller=tool_caller,
            messages=_clone_messages_for_background(messages),
            user_request=request_text,
            rewritten_query=rewritten_query,
            user_images=list(user_images or []),
            use_builtin_search=use_builtin_search,
            logger=logger,
        )
    )

    def _done(done_task: asyncio.Task) -> None:
        if done_task.cancelled():
            return
        try:
            exc = done_task.exception()
        except asyncio.InvalidStateError:
            return
        if exc is not None:
            log_exception(logger, "[agent] background image generation task crashed", exc, level="warning")

    task.add_done_callback(_done)
    return True


__all__ = [
    "_IMAGE_GENERATION_TOOL_NAME",
    "_can_start_background_image_generation",
    "_clone_messages_for_background",
    "_extract_image_b64_tool_result",
    "_generate_image_generation_status_reply",
    "_run_background_image_generation",
    "_start_background_image_generation",
]
