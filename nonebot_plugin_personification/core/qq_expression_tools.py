from __future__ import annotations

import json
import random
import re
from typing import Any

from ..agent.tool_registry import AgentTool, ToolRegistry
from .qq_expression_library import semantic_text_for_qq_expression_segment
from .qq_face_names import QQ_FACE_NAMES


QQ_EXPRESSION_TOOL_NAMES = frozenset(
    {
        "send_qq_face",
        "send_qq_favorite_expression",
        "send_qq_recommended_expression",
    }
)

_FACE_ALIASES: dict[str, int] = {
    "小黄脸": 14,
    "黄脸": 14,
    "笑": 14,
    "笑脸": 14,
    "微笑": 14,
    "ok": 124,
    "okay": 124,
    "赞": 76,
    "强": 76,
    "点赞": 201,
    "笑哭": 182,
    "捂脸": 264,
    "吃瓜": 271,
    "疑问": 32,
    "问号": 268,
    "问号脸": 268,
    "委屈": 106,
    "可怜": 111,
    "流泪": 5,
    "大哭": 9,
    "害羞": 6,
    "亲亲": 109,
    "爱心": 66,
    "鼓掌": 99,
}


def _normalize_face_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = text.strip("[]()（）【】「」'\"")
    text = text.replace("qq", "").replace("表情", "")
    return text


def resolve_qq_face_id(face_id: Any = None, face_name: Any = "") -> tuple[int | None, str]:
    raw_id = str(face_id if face_id is not None else "").strip()
    if raw_id:
        try:
            resolved = int(raw_id)
        except (TypeError, ValueError):
            return None, f"face_id 不是有效数字：{raw_id}"
        if resolved < 0:
            return None, "face_id 不能为负数"
        return resolved, QQ_FACE_NAMES.get(resolved, f"id:{resolved}")

    normalized = _normalize_face_name(face_name)
    if not normalized:
        return None, "需要提供 face_id 或 face_name"
    if normalized in _FACE_ALIASES:
        resolved = _FACE_ALIASES[normalized]
        return resolved, QQ_FACE_NAMES.get(resolved, normalized)
    for candidate_id, candidate_name in QQ_FACE_NAMES.items():
        if _normalize_face_name(candidate_name) == normalized:
            return candidate_id, candidate_name
    return None, f"未识别的 QQ 表情名称：{face_name}"


def _action_result(*, ok: bool, queued: bool = False, kind: str = "", reason: str = "", **extra: Any) -> str:
    payload: dict[str, Any] = {
        "ok": ok,
        "queued": queued,
    }
    if kind:
        payload["kind"] = kind
    if reason:
        payload["reason"] = reason
    payload.update(extra)
    if queued:
        payload["final_reply_instruction"] = "表情已经安排发送；最终回复请只输出 [SILENCE]，不要再解释工具或重复说已发送。"
    return json.dumps(payload, ensure_ascii=False)


def expression_tool_result_queued(result_text: Any) -> bool:
    try:
        payload = json.loads(str(result_text or "").strip())
    except Exception:
        return False
    return isinstance(payload, dict) and bool(payload.get("ok")) and bool(payload.get("queued"))


def _queue_action(executor: Any, action_type: str, params: dict[str, Any]) -> bool:
    queue = getattr(executor, "queue_action", None)
    if callable(queue):
        queue(action_type, params)
        return True
    actions = getattr(executor, "pending_actions", None)
    if isinstance(actions, list):
        actions.append({"type": action_type, "params": dict(params or {})})
        return True
    return False


def _compact_text(value: Any, *, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[: max(0, int(limit))]


def _extensions_disabled(plugin_config: Any) -> bool:
    mode = str(getattr(plugin_config, "personification_protocol_extensions", "auto") or "auto").strip().lower()
    return mode == "none"


def _extract_url_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, dict):
        values = None
        for key in ("url", "urls", "data", "result"):
            candidate = raw.get(key)
            if candidate:
                values = candidate
                break
        if isinstance(values, dict):
            return _extract_url_list(values)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return []
    else:
        return []
    urls: list[str] = []
    for item in values:
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("file") or "").strip()
        else:
            url = str(item or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls


def _extract_url_candidates(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [{"url": value, "summary": ""}] if value.startswith(("http://", "https://")) else []
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, dict):
        values = None
        for key in ("url", "urls", "data", "result"):
            candidate = raw.get(key)
            if candidate:
                values = candidate
                break
        if isinstance(values, dict):
            return _extract_url_candidates(values)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return []
    else:
        return []
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in values:
        summary = ""
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("file") or "").strip()
            summary = re.sub(r"\s+", " ", str(item.get("summary") or item.get("name") or "").strip()).strip("[]")[:40]
        else:
            url = str(item or "").strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            candidates.append({"url": url, "summary": summary})
    return candidates


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def _pick_url(urls: list[str], *, index: Any = 1, random_pick: bool = False) -> tuple[str, int]:
    if not urls:
        return "", 0
    if random_pick:
        picked_index = random.randrange(0, len(urls))
        return urls[picked_index], picked_index + 1
    one_based = _bounded_int(index, default=1, minimum=1, maximum=len(urls))
    return urls[one_based - 1], one_based


def _pick_candidate(candidates: list[dict[str, str]], *, index: Any = 1, random_pick: bool = False) -> tuple[dict[str, str], int]:
    if not candidates:
        return {}, 0
    if random_pick:
        picked_index = random.randrange(0, len(candidates))
        return candidates[picked_index], picked_index + 1
    one_based = _bounded_int(index, default=1, minimum=1, maximum=len(candidates))
    return candidates[one_based - 1], one_based


def qq_action_history_text(action: Any) -> str:
    if not isinstance(action, dict):
        return ""
    action_type = str(action.get("type", "") or "").strip()
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    explicit = str(params.get("history_text", "") or "").strip()
    if explicit:
        return explicit
    if action_type == "send_qq_face":
        return semantic_text_for_qq_expression_segment("face", {"id": params.get("face_id", "")})
    if action_type == "send_qq_image_expression":
        summary = str(params.get("summary", "") or "").strip()
        return f"[QQ收藏表情:{summary}]" if summary else "[QQ收藏表情]"
    if action_type == "send_image_url":
        summary = str(params.get("summary", "") or "").strip()
        return str(params.get("history_text", "") or "").strip() or (f"[联网图片:{summary}]" if summary else "[联网图片]")
    if action_type == "send_qq_mface":
        data = params.get("data") if isinstance(params, dict) else {}
        return semantic_text_for_qq_expression_segment("mface", data, default_mface_kind=str(params.get("kind", "super") or "super"))
    return ""


async def _call_optional_onebot_api(bot: Any, api: str, **kwargs: Any) -> Any:
    call_api = getattr(bot, "call_api", None)
    if not callable(call_api):
        raise RuntimeError("当前 bot 不支持 call_api")
    return await call_api(api, **kwargs)


async def _call_recommend_face_api(bot: Any, query: str) -> Any:
    try:
        return await _call_optional_onebot_api(bot, "get_recommend_face", word=query)
    except Exception:
        return await _call_optional_onebot_api(bot, "get_recommend_face", message=query)


def build_send_qq_expression_tools(*, executor: Any, bot: Any = None, plugin_config: Any = None) -> list[AgentTool]:
    effective_bot = bot or getattr(executor, "bot", None)
    effective_config = plugin_config or getattr(executor, "config", None)

    async def _send_face(face_id: Any = None, face_name: str = "", text: str = "") -> str:
        resolved_id, label = resolve_qq_face_id(face_id, face_name)
        if resolved_id is None:
            return _action_result(ok=False, reason=label)
        if not _queue_action(
            executor,
            "send_qq_face",
            {
                "face_id": resolved_id,
                "text": _compact_text(text),
                "history_text": f"[QQ表情:{label}]",
            },
        ):
            return _action_result(ok=False, reason="当前发送上下文不可用")
        return _action_result(
            ok=True,
            queued=True,
            kind="qq_face",
            face_id=resolved_id,
            face_name=label,
        )

    async def _send_favorite(index: int = 1, random_pick: bool = False, count: int = 20, text: str = "") -> str:
        if _extensions_disabled(effective_config):
            return _action_result(ok=False, reason="协议扩展能力已关闭，无法调用 fetch_custom_face")
        if effective_bot is None:
            return _action_result(ok=False, reason="当前 bot 上下文不可用")
        resolved_count = _bounded_int(count, default=20, minimum=1, maximum=50)
        try:
            raw = await _call_optional_onebot_api(effective_bot, "fetch_custom_face", count=resolved_count)
        except Exception as exc:
            return _action_result(ok=False, reason=f"fetch_custom_face 调用失败：{str(exc)[:120]}")
        candidates = _extract_url_candidates(raw)
        if not candidates:
            return _action_result(ok=False, reason="没有拿到可发送的收藏表情 URL")
        picked, picked_index = _pick_candidate(candidates, index=index, random_pick=_coerce_bool(random_pick))
        picked_url = picked.get("url", "")
        summary = picked.get("summary", "")
        if not picked_url:
            return _action_result(ok=False, reason="没有选中收藏表情")
        if not _queue_action(
            executor,
            "send_qq_image_expression",
            {
                "url": picked_url,
                "text": _compact_text(text),
                "summary": summary,
                "history_text": f"[QQ收藏表情:{summary}]" if summary else "[QQ收藏表情]",
            },
        ):
            return _action_result(ok=False, reason="当前发送上下文不可用")
        return _action_result(
            ok=True,
            queued=True,
            kind="qq_favorite_expression",
            picked_index=picked_index,
            fetched=len(candidates),
            summary=summary,
        )

    async def _send_recommended(query: str, index: int = 1, random_pick: bool = False, text: str = "") -> str:
        if _extensions_disabled(effective_config):
            return _action_result(ok=False, reason="协议扩展能力已关闭，无法调用 get_recommend_face")
        if effective_bot is None:
            return _action_result(ok=False, reason="当前 bot 上下文不可用")
        q = _compact_text(query, limit=40)
        if not q:
            return _action_result(ok=False, reason="需要提供推荐表情搜索词")
        try:
            raw = await _call_recommend_face_api(effective_bot, q)
        except Exception as exc:
            return _action_result(ok=False, reason=f"get_recommend_face 调用失败：{str(exc)[:120]}")
        candidates = _extract_url_candidates(raw)
        if not candidates:
            return _action_result(ok=False, reason="没有拿到可发送的推荐表情 URL", query=q)
        picked, picked_index = _pick_candidate(candidates, index=index, random_pick=_coerce_bool(random_pick))
        picked_url = picked.get("url", "")
        summary = picked.get("summary", "")
        if not picked_url:
            return _action_result(ok=False, reason="没有选中推荐表情", query=q)
        if not _queue_action(
            executor,
            "send_qq_image_expression",
            {
                "url": picked_url,
                "text": _compact_text(text),
                "summary": summary or q,
                "history_text": f"[QQ推荐表情:{q}]",
            },
        ):
            return _action_result(ok=False, reason="当前发送上下文不可用")
        return _action_result(
            ok=True,
            queued=True,
            kind="qq_recommended_expression",
            query=q,
            picked_index=picked_index,
            fetched=len(candidates),
            summary=summary,
        )

    metadata = {
        "intent_tags": ["expression"],
        "evidence_kind": "action",
        "requires_network": False,
        "requires_image": False,
        "latency_class": "fast",
        "risk_level": "low",
    }
    return [
        AgentTool(
            name="send_qq_face",
            description=(
                "发送一个 QQ 内置小黄脸/系统 face 表情。"
                "仅当用户明确让你发表情、小黄脸、某个 QQ face，或这轮只用一个小黄脸回应更自然时调用；"
                "调用后不要再用文字解释已发送。face_id 优先，face_name 可用如 微笑、笑哭、捂脸、赞、OK、吃瓜。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "face_id": {"type": "string", "description": "QQ face 表情 id，如 14=微笑、182=笑哭、264=捂脸"},
                    "face_name": {"type": "string", "description": "QQ 表情中文名；不知道 id 时填写"},
                    "text": {"type": "string", "description": "可选，和表情同条发送的极短文字；通常留空"},
                },
                "required": [],
            },
            handler=_send_face,
            local=True,
            metadata=dict(metadata),
        ),
        AgentTool(
            name="send_qq_favorite_expression",
            description=(
                "从 LLOneBot/NapCat 的收藏表情列表拉取一个表情 URL 并发送。"
                "仅当用户明确要求发送收藏表情、随机收藏表情，或说'发个你收藏的表情'时调用；"
                "收藏列表只有 URL，没有语义标签，所以不要用它回答需要精确语义的请求。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "发送第几个收藏表情，1 起；默认 1"},
                    "random_pick": {"type": "boolean", "description": "是否随机挑一个收藏表情"},
                    "count": {"type": "integer", "description": "拉取收藏表情数量，默认 20，最大 50"},
                    "text": {"type": "string", "description": "可选，和表情同条发送的极短文字；通常留空"},
                },
                "required": [],
            },
            handler=_send_favorite,
            local=True,
            metadata=dict(metadata),
        ),
        AgentTool(
            name="send_qq_recommended_expression",
            description=(
                "调用 LLOneBot/NapCat 的推荐表情接口，按一个短词/情绪/场景拿推荐表情 URL 并发送。"
                "当用户要求按某个语气发表情，或你需要比收藏表情更贴合语义的 QQ 图片表情时调用；"
                "query 用很短的中文词，如 开心、无语、笑哭、吃瓜。调用后不要再解释已发送。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "推荐表情搜索词，短中文词最佳"},
                    "index": {"type": "integer", "description": "发送第几个推荐结果，1 起；默认 1"},
                    "random_pick": {"type": "boolean", "description": "是否随机挑一个推荐结果"},
                    "text": {"type": "string", "description": "可选，和表情同条发送的极短文字；通常留空"},
                },
                "required": ["query"],
            },
            handler=_send_recommended,
            local=True,
            metadata=dict(metadata),
        ),
    ]


def register_send_qq_expression_tools(registry: ToolRegistry, *, executor: Any, bot: Any = None, plugin_config: Any = None) -> None:
    for tool in build_send_qq_expression_tools(
        executor=executor,
        bot=bot,
        plugin_config=plugin_config,
    ):
        registry.register(tool)


__all__ = [
    "QQ_EXPRESSION_TOOL_NAMES",
    "build_send_qq_expression_tools",
    "expression_tool_result_queued",
    "qq_action_history_text",
    "register_send_qq_expression_tools",
    "resolve_qq_face_id",
]
