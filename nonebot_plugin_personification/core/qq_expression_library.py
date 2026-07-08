from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


ASSET_PATH = Path(__file__).with_name("qq_expression_assets.json")
_MARKER_RE = re.compile(
    r"\[(QQ(?:表情|小黄脸|普通表情|超级表情|收藏表情|推荐表情))\s*(?:[:：]\s*([^\]]*?))?\]",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(r"^(.*?)(?:\s*[*xX×]\s*(\d{1,2}))?$")
_STATE_TTL_SECONDS = 12 * 60 * 60
_MAX_FACE_REPEAT = 3

_QQ_EXPRESSION_STATE: dict[str, dict[str, Any]] = {}
_QQ_MFACE_CACHE: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class QQExpressionRequest:
    raw: str
    kind: str
    query: str = ""
    count: int = 1


@dataclass(frozen=True)
class QQExpressionRender:
    message: Any
    history_text: str
    expression_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class QQExpressionCandidate:
    url: str
    summary: str = ""
    mface_data: dict[str, Any] | None = None


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = text.strip("[]()（）【】「」'\"")
    return text.replace("qq", "").replace("表情", "")


def _clean_expression_label(value: Any, default: str = "表情包") -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*\[(?:CQ:)?", "", text)
    text = text.strip("[]【】「」()（）'\"")
    text = re.sub(r"^(?:QQ)?(?:超级|收藏|推荐|普通|小黄脸)?表情\s*[:：]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or default).strip()[:40]


def qq_face_semantic_text(face_id: Any, *, super_face: bool = False) -> str:
    raw = str(face_id or "").strip()
    if not raw:
        return "[QQ表情]"
    try:
        from .qq_face_names import QQ_FACE_NAMES

        name = QQ_FACE_NAMES.get(int(raw))
    except Exception:
        name = None
    if name:
        return f"[QQ{'超级' if super_face else ''}表情:{name}]"
    return f"[QQ{'超级' if super_face else ''}表情id:{raw}]"


def _coerce_segment_data(data: Any) -> dict[str, Any]:
    return dict(data or {}) if isinstance(data, dict) else {}


def _looks_like_super_face_data(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("large", "is_super", "super", "super_face"):
        value = data.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
    sub_type = str(data.get("sub_type", data.get("subType", "")) or "").strip().lower()
    return sub_type in {"1", "super", "large"}


def _mface_send_data(raw: Any, *, fallback_summary: str = "") -> dict[str, Any] | None:
    data = _coerce_segment_data(raw)
    if not data:
        return None
    emoji_id = str(data.get("emoji_id", data.get("emojiId", "")) or "").strip()
    package_id = str(data.get("emoji_package_id", data.get("emojiPackageId", data.get("package_id", ""))) or "").strip()
    key = str(data.get("key", data.get("mface_key", "")) or "").strip()
    if not emoji_id or not package_id or not key:
        return None
    summary = _clean_expression_label(data.get("summary") or fallback_summary, default=f"emoji:{emoji_id}")
    normalized: dict[str, Any] = {
        "emoji_id": emoji_id,
        "emoji_package_id": package_id,
        "key": key,
        "summary": summary,
    }
    for optional_key in ("url", "file"):
        value = str(data.get(optional_key, "") or "").strip()
        if value:
            normalized[optional_key] = value
    return normalized


def remember_qq_mface_segment(data: Any, *, kind: str = "super", summary: str = "") -> dict[str, Any] | None:
    send_data = _mface_send_data(data, fallback_summary=summary)
    if send_data is None:
        return None
    label = _clean_expression_label(send_data.get("summary") or summary, default="表情包")
    keys = {
        _normalize_text(label),
        _normalize_text(send_data.get("emoji_id", "")),
        _normalize_text(send_data.get("summary", "")),
    }
    for key in list(keys):
        if key:
            _QQ_MFACE_CACHE[key] = {**send_data, "_kind": str(kind or "super").strip().lower()}
    return send_data


def _mface_label(data: dict[str, Any], *, fallback: str = "表情包") -> str:
    for key in ("summary", "name", "text", "emoji_name", "emojiName"):
        value = str(data.get(key, "") or "").strip()
        if value:
            return _clean_expression_label(value, default=fallback)
    emoji_id = str(data.get("emoji_id", data.get("emojiId", "")) or "").strip()
    return f"emoji:{emoji_id}" if emoji_id else fallback


def semantic_text_for_qq_expression_segment(
    segment_type: Any,
    data: Any,
    *,
    default_mface_kind: str = "favorite",
) -> str:
    seg_type = str(segment_type or "").strip().lower()
    payload = _coerce_segment_data(data)
    if seg_type == "face":
        return qq_face_semantic_text(payload.get("id", ""), super_face=_looks_like_super_face_data(payload))
    if seg_type == "mface":
        kind = str(default_mface_kind or "super").strip().lower()
        label = _mface_label(payload)
        remember_qq_mface_segment(payload, kind=kind, summary=label)
        if kind == "super":
            return f"[QQ超级表情:{label}]"
        if kind == "recommended":
            return f"[QQ推荐表情:{label}]"
        return f"[QQ收藏表情:{label}]"
    if seg_type == "image" and any(key in payload for key in ("emoji_id", "emojiId", "emoji_package_id", "emojiPackageId", "key")):
        label = _mface_label(payload)
        remember_qq_mface_segment(payload, kind=default_mface_kind or "favorite", summary=label)
        return f"[QQ收藏表情:{label}]"
    return ""


def _super_mface_data_for_query(query: Any, asset: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if asset:
        label = str(asset.get("official_name") or query or "").strip()
        for key in ("super_mface", "mface", "mface_data"):
            data = _mface_send_data(asset.get(key), fallback_summary=label)
            if data is not None:
                return data
    normalized_keys = [
        _normalize_text(query),
        _normalize_text(asset.get("official_name", "") if isinstance(asset, dict) else ""),
    ]
    for key in normalized_keys:
        if key and key in _QQ_MFACE_CACHE:
            return dict(_QQ_MFACE_CACHE[key])
    return None


def _bounded_float(value: Any, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _extensions_disabled(plugin_config: Any) -> bool:
    mode = str(getattr(plugin_config, "personification_protocol_extensions", "auto") or "auto").strip().lower()
    return mode == "none"


def qq_expression_enabled(plugin_config: Any) -> bool:
    return bool(getattr(plugin_config, "personification_qq_expression_enabled", True))


@lru_cache(maxsize=1)
def load_qq_expression_assets() -> dict[str, Any]:
    try:
        loaded = json.loads(ASSET_PATH.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    faces = loaded.get("faces") if isinstance(loaded, dict) else []
    if not isinstance(faces, list):
        faces = []
    normalized_faces: list[dict[str, Any]] = []
    for item in faces:
        if not isinstance(item, dict):
            continue
        try:
            face_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(item.get("official_name") or "").strip()
        if not name:
            continue
        aliases = [str(alias).strip() for alias in list(item.get("aliases") or []) if str(alias).strip()]
        normalized_faces.append(
            {
                **item,
                "id": face_id,
                "official_name": name,
                "aliases": aliases,
                "mood_tags": [str(tag).strip() for tag in list(item.get("mood_tags") or []) if str(tag).strip()],
                "scene_tags": [str(tag).strip() for tag in list(item.get("scene_tags") or []) if str(tag).strip()],
                "auto_safe": bool(item.get("auto_safe", False)),
                "super_supported": bool(item.get("super_supported", False)),
                "weight": float(item.get("weight", 1.0) or 1.0),
            }
        )
    return {"schema_version": loaded.get("schema_version", 1) if isinstance(loaded, dict) else 1, "faces": normalized_faces}


def _face_lookup_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in load_qq_expression_assets().get("faces", []):
        names = [item.get("official_name", ""), str(item.get("id", "")), *list(item.get("aliases") or [])]
        for name in names:
            normalized = _normalize_text(name)
            if normalized and normalized not in index:
                index[normalized] = item
    return index


def find_qq_face_asset(query: Any, *, auto_safe_only: bool = False) -> dict[str, Any] | None:
    normalized = _normalize_text(query)
    if not normalized:
        return None
    item = _face_lookup_index().get(normalized)
    if item and (not auto_safe_only or item.get("auto_safe")):
        return item
    return None


def _semantic_text(
    *,
    mood_hint: str = "",
    context: str = "",
    semantic_frame: Any = None,
) -> str:
    parts = [
        mood_hint,
        context,
        getattr(semantic_frame, "sticker_mood_hint", ""),
        getattr(semantic_frame, "bot_emotion", ""),
        getattr(semantic_frame, "expression_style", ""),
        getattr(semantic_frame, "conversation_scenario", ""),
    ]
    return " ".join(str(part or "") for part in parts if str(part or "").strip())


def choose_qq_face_asset(
    *,
    mood_hint: str = "",
    context: str = "",
    semantic_frame: Any = None,
    super_only: bool = False,
    excluded_ids: set[int] | None = None,
    rng: Any = random,
) -> dict[str, Any] | None:
    text = _semantic_text(mood_hint=mood_hint, context=context, semantic_frame=semantic_frame)
    excluded = set(excluded_ids or set())
    candidates: list[tuple[float, dict[str, Any]]] = []
    for item in load_qq_expression_assets().get("faces", []):
        if int(item.get("id", -1)) in excluded:
            continue
        if not item.get("auto_safe"):
            continue
        if super_only and not item.get("super_supported"):
            continue
        score = max(0.05, float(item.get("weight", 1.0) or 1.0))
        for tag in list(item.get("mood_tags") or []):
            if tag and tag in text:
                score += 1.3
        for tag in list(item.get("scene_tags") or []):
            if tag and tag in text:
                score += 1.5
        candidates.append((score, item))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    top_score = candidates[0][0]
    pool = [item for score, item in candidates if score >= max(0.1, top_score - 1.0)][:8]
    return rng.choice(pool) if len(pool) > 1 else pool[0]


def build_qq_expression_prompt(*, max_items: int = 16) -> str:
    faces = [
        item
        for item in load_qq_expression_assets().get("faces", [])
        if item.get("auto_safe")
    ][:max(1, max_items)]
    safe_names = "、".join(str(item.get("official_name")) for item in faces)
    avoid = [
        item
        for item in load_qq_expression_assets().get("faces", [])
        if not item.get("auto_safe")
    ]
    avoid_names = "、".join(str(item.get("official_name")) for item in avoid[:8])
    return (
        "## QQ 表情输出规则\n"
        "你可以在普通回复里插入 QQ 表情标记，发送层会把标记转成真实 QQ 表情，用户不会看到标记文本。\n"
        "- 普通小黄脸/系统表情：用 [QQ表情:笑哭]，也可嵌在短句中，例如 好好好[QQ表情:捂脸]。\n"
        "- 强烈接梗或水群时可以三连普通表情：用 [QQ表情:笑哭*3]，只在气氛适合时偶尔用。\n"
        "- 超级表情：用 [QQ超级表情:笑哭]，只在水群、庆祝、强接梗时偶尔用；只有协议端已有完整 mface 元数据时才会发成真正超级表情，否则会降级为普通 QQ 表情。\n"
        "- QQ 收藏表情：用 [QQ收藏表情:随机]，只在像发图表情包比文字更自然时用。\n"
        "- QQ 推荐表情：用 [QQ推荐表情:开心]，按 2-6 字短词获取协议端推荐图片表情。\n"
        f"- 默认安全可用：{safe_names or '笑哭、捂脸、赞、OK、吃瓜'}。\n"
        f"- 不要自动使用这些容易有歧义或攻击性的表情：{avoid_names or '微笑、呵呵哒、鄙视、踩、嘲讽'}；"
        "其中“微笑”在年轻群聊里可能像冷淡/敷衍/阴阳怪气，除非用户点名。\n"
        "- 小黄脸不是每句话都要用；除非用户明确要表情，普通闲聊里只在语气需要时低频使用。"
    )


def contains_qq_expression_marker(text: Any) -> bool:
    return bool(_MARKER_RE.search(str(text or "")))


def _parse_marker(kind_text: str, payload: str) -> QQExpressionRequest:
    kind_raw = _normalize_text(kind_text)
    raw_payload = str(payload or "").strip()
    count = 1
    query = raw_payload
    match = _COUNT_RE.match(raw_payload)
    if match:
        query = str(match.group(1) or "").strip()
        if match.group(2):
            count = _bounded_int(match.group(2), default=1, minimum=1, maximum=_MAX_FACE_REPEAT)
    if "收藏" in kind_raw:
        return QQExpressionRequest(raw=f"[{kind_text}:{raw_payload}]", kind="favorite", query=query or "随机", count=1)
    if "推荐" in kind_raw:
        return QQExpressionRequest(raw=f"[{kind_text}:{raw_payload}]", kind="recommended", query=query or "开心", count=1)
    if "超级" in kind_raw:
        return QQExpressionRequest(raw=f"[{kind_text}:{raw_payload}]", kind="super_face", query=query or "笑哭", count=1)
    return QQExpressionRequest(raw=f"[{kind_text}:{raw_payload}]", kind="face", query=query or "随机", count=count)


def split_qq_expression_markers(text: Any) -> list[str | QQExpressionRequest]:
    raw = str(text or "")
    if not raw:
        return []
    parts: list[str | QQExpressionRequest] = []
    last = 0
    for match in _MARKER_RE.finditer(raw):
        if match.start() > last:
            parts.append(raw[last:match.start()])
        parts.append(_parse_marker(match.group(1), match.group(2) or ""))
        last = match.end()
    if last < len(raw):
        parts.append(raw[last:])
    return parts


def strip_qq_expression_markers(text: Any) -> str:
    return "".join(part for part in split_qq_expression_markers(text) if isinstance(part, str)).strip()


def history_text_for_qq_expression(text: Any) -> str:
    rendered: list[str] = []
    for part in split_qq_expression_markers(text):
        if isinstance(part, str):
            rendered.append(part)
            continue
        if part.kind == "super_face":
            item = _choose_explicit_or_safe_face(part.query, super_only=True)
            label = str((item or {}).get("official_name") or part.query or "随机")
            if _super_mface_data_for_query(part.query or label, item):
                rendered.append(f"[QQ超级表情:{label}]")
            else:
                rendered.append(f"[QQ表情:{label}]")
        elif part.kind == "favorite":
            if _normalize_text(part.query) in {"", "随机", "随便"}:
                rendered.append("[QQ收藏表情]")
            else:
                rendered.append(f"[QQ收藏表情:{_clean_expression_label(part.query)}]")
        elif part.kind == "recommended":
            rendered.append(f"[QQ推荐表情:{part.query or '开心'}]")
        elif part.count > 1:
            item = _choose_explicit_or_safe_face(part.query, super_only=False)
            label = str((item or {}).get("official_name") or part.query or "随机")
            rendered.append(f"[QQ表情:{label}x{part.count}]")
        else:
            item = _choose_explicit_or_safe_face(part.query, super_only=False)
            label = str((item or {}).get("official_name") or part.query or "随机")
            rendered.append(f"[QQ表情:{label}]")
    return re.sub(r"\s+", " ", "".join(rendered)).strip()


def _extract_url_candidates(raw: Any) -> list[QQExpressionCandidate]:
    if raw is None:
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [QQExpressionCandidate(value)] if value.startswith(("http://", "https://")) else []
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
    candidates: list[QQExpressionCandidate] = []
    seen: set[str] = set()
    for item in values:
        summary = ""
        mface_data: dict[str, Any] | None = None
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("file") or "").strip()
            summary = _clean_expression_label(item.get("summary") or item.get("name") or "", default="")
            mface_data = _mface_send_data(item, fallback_summary=summary)
        else:
            url = str(item or "").strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            candidates.append(QQExpressionCandidate(url=url, summary=summary, mface_data=mface_data))
    return candidates


def _extract_url_list(raw: Any) -> list[str]:
    return [item.url for item in _extract_url_candidates(raw)]


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


def _choose_explicit_or_safe_face(query: str, *, super_only: bool = False) -> dict[str, Any] | None:
    normalized = _normalize_text(query)
    if normalized in {"", "随机", "随便", "小黄脸", "黄脸"}:
        return choose_qq_face_asset(mood_hint="搞笑|接梗", super_only=super_only)
    item = find_qq_face_asset(query, auto_safe_only=False)
    if item and (not super_only or item.get("super_supported")):
        return item
    return choose_qq_face_asset(mood_hint=query, context=query, super_only=super_only)


def _face_segment(message_segment_cls: Any, face_id: int) -> Any:
    return message_segment_cls.face(int(face_id))


def _cq_escape(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def _face_cq(face_id: int, *, super_face: bool = False) -> str:
    return f"[CQ:face,id={int(face_id)}]"


def _mface_cq(data: dict[str, Any]) -> str:
    params = []
    for key in ("emoji_id", "emoji_package_id", "key", "summary", "url", "file"):
        value = str(data.get(key, "") or "").strip()
        if value:
            params.append(f"{key}={_cq_escape(value)}")
    return f"[CQ:mface,{','.join(params)}]" if params else ""


async def _resolve_request_to_segment(
    request: QQExpressionRequest,
    *,
    message_segment_cls: Any,
    bot: Any = None,
    plugin_config: Any = None,
    logger: Any = None,
) -> tuple[list[Any], str, str]:
    try:
        if request.kind in {"face", "super_face"}:
            super_face = request.kind == "super_face"
            item = _choose_explicit_or_safe_face(request.query, super_only=super_face)
            if item is None:
                return [], "", ""
            label = str(item.get("official_name") or item.get("id"))
            if super_face:
                mface_data = _super_mface_data_for_query(request.query or label, item)
                if mface_data is not None:
                    return [message_segment_cls("mface", mface_data)], _clean_expression_label(mface_data.get("summary") or label), "super_face"
            repeat = 1 if super_face else max(1, min(_MAX_FACE_REPEAT, request.count))
            return (
                [_face_segment(message_segment_cls, int(item["id"])) for _ in range(repeat)],
                label,
                "face",
            )
        if _extensions_disabled(plugin_config) or bot is None:
            return [], "", ""
        if request.kind == "favorite":
            raw = await _call_optional_onebot_api(bot, "fetch_custom_face", count=20)
            candidates = _extract_url_candidates(raw)
            if not candidates:
                return [], "", ""
            candidate = random.choice(candidates)
            if candidate.mface_data is not None:
                remember_qq_mface_segment(candidate.mface_data, kind="favorite", summary=candidate.summary)
            return [message_segment_cls.image(candidate.url)], candidate.summary or "收藏表情", "favorite"
        if request.kind == "recommended":
            query = str(request.query or "开心").strip()[:40] or "开心"
            raw = await _call_recommend_face_api(bot, query)
            candidates = _extract_url_candidates(raw)
            if not candidates:
                return [], "", ""
            candidate = random.choice(candidates)
            return [message_segment_cls.image(candidate.url)], candidate.summary or query, "recommended"
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug(f"[qq_expression] render marker failed: {exc}")
            except Exception:
                pass
    return [], "", ""


async def render_qq_expression_message(
    text: Any,
    *,
    message_segment_cls: Any,
    bot: Any = None,
    plugin_config: Any = None,
    logger: Any = None,
) -> QQExpressionRender:
    parts = split_qq_expression_markers(text)
    if not any(isinstance(part, QQExpressionRequest) for part in parts):
        plain = str(text or "").strip()
        return QQExpressionRender(message=plain, history_text=plain, expression_names=())
    if not qq_expression_enabled(plugin_config):
        plain = strip_qq_expression_markers(text)
        return QQExpressionRender(message=plain, history_text=plain, expression_names=())
    message = None
    history_parts: list[str] = []
    names: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                text_seg = message_segment_cls.text(part)
                message = text_seg if message is None else message + text_seg
                history_parts.append(part)
            continue
        segments, label, resolved_kind = await _resolve_request_to_segment(
            part,
            message_segment_cls=message_segment_cls,
            bot=bot,
            plugin_config=plugin_config,
            logger=logger,
        )
        if not segments:
            continue
        for segment in segments:
            message = segment if message is None else message + segment
        names.append(label)
        if resolved_kind == "super_face":
            history_parts.append(f"[QQ超级表情:{label}]")
        elif resolved_kind == "favorite":
            history_parts.append(f"[QQ收藏表情:{label}]" if label and label != "收藏表情" else "[QQ收藏表情]")
        elif resolved_kind == "recommended":
            history_parts.append(f"[QQ推荐表情:{part.query or label}]")
        elif part.count > 1:
            history_parts.append(f"[QQ表情:{label}x{part.count}]")
        else:
            history_parts.append(f"[QQ表情:{label}]")
    history = re.sub(r"\s+", " ", "".join(history_parts)).strip()
    return QQExpressionRender(message=message or "", history_text=history, expression_names=tuple(names))


async def render_qq_expression_cq_text(
    text: Any,
    *,
    bot: Any = None,
    plugin_config: Any = None,
    logger: Any = None,
) -> QQExpressionRender:
    parts = split_qq_expression_markers(text)
    if not any(isinstance(part, QQExpressionRequest) for part in parts):
        plain = str(text or "").strip()
        return QQExpressionRender(message=plain, history_text=plain, expression_names=())
    if not qq_expression_enabled(plugin_config):
        plain = strip_qq_expression_markers(text)
        return QQExpressionRender(message=plain, history_text=plain, expression_names=())
    rendered: list[str] = []
    history_parts: list[str] = []
    names: list[str] = []
    for part in parts:
        if isinstance(part, str):
            rendered.append(_cq_escape(part))
            history_parts.append(part)
            continue
        try:
            if part.kind in {"face", "super_face"}:
                super_face = part.kind == "super_face"
                item = _choose_explicit_or_safe_face(part.query, super_only=super_face)
                if item is None:
                    continue
                repeat = 1 if super_face else max(1, min(_MAX_FACE_REPEAT, part.count))
                label = str(item.get("official_name") or item.get("id"))
                if super_face:
                    mface_data = _super_mface_data_for_query(part.query or label, item)
                    if mface_data is not None:
                        mface_text = _mface_cq(mface_data)
                        if mface_text:
                            rendered.append(mface_text)
                            label = _clean_expression_label(mface_data.get("summary") or label)
                            names.append(label)
                            history_parts.append(f"[QQ超级表情:{label}]")
                            continue
                rendered.extend(_face_cq(int(item["id"])) for _ in range(repeat))
                names.append(label)
                history_parts.append(f"[QQ表情:{label}{'x' + str(repeat) if repeat > 1 else ''}]")
            elif not _extensions_disabled(plugin_config) and bot is not None:
                if part.kind == "favorite":
                    raw = await _call_optional_onebot_api(bot, "fetch_custom_face", count=20)
                    candidates = _extract_url_candidates(raw)
                    if candidates:
                        candidate = random.choice(candidates)
                        rendered.append(f"[CQ:image,file={_cq_escape(candidate.url)}]")
                        label = candidate.summary or "收藏表情"
                        names.append(label)
                        history_parts.append(f"[QQ收藏表情:{label}]" if candidate.summary else "[QQ收藏表情]")
                elif part.kind == "recommended":
                    query = str(part.query or "开心").strip()[:40] or "开心"
                    raw = await _call_recommend_face_api(bot, query)
                    candidates = _extract_url_candidates(raw)
                    if candidates:
                        candidate = random.choice(candidates)
                        rendered.append(f"[CQ:image,file={_cq_escape(candidate.url)}]")
                        names.append(candidate.summary or f"推荐表情:{query}")
                        history_parts.append(f"[QQ推荐表情:{query}]")
        except Exception as exc:
            if logger is not None:
                try:
                    logger.debug(f"[qq_expression] render cq marker failed: {exc}")
                except Exception:
                    pass
    message = "".join(rendered).strip()
    history = re.sub(r"\s+", " ", "".join(history_parts)).strip()
    return QQExpressionRender(message=message, history_text=history, expression_names=tuple(names))


def _state_key(group_id: str, user_id: str = "", *, is_private: bool = False) -> str:
    return f"private:{user_id}" if is_private else f"group:{group_id}"


def _today(now_ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if now_ts is None else now_ts))


def _auto_allowed(
    *,
    plugin_config: Any,
    key: str,
    explicit: bool,
    rng: Any = random,
) -> bool:
    if not qq_expression_enabled(plugin_config):
        return False
    if explicit:
        return True
    probability = _bounded_float(getattr(plugin_config, "personification_qq_expression_probability", 0.08), 0.08)
    if probability <= 0.0 or rng.random() >= probability:
        return False
    now_ts = time.time()
    state = _QQ_EXPRESSION_STATE.get(key)
    if state and now_ts - float(state.get("updated_at", 0) or 0) > _STATE_TTL_SECONDS:
        _QQ_EXPRESSION_STATE.pop(key, None)
        state = None
    state = state or {"last_sent": 0.0, "date": _today(now_ts), "count": 0, "recent": []}
    cooldown = float(getattr(plugin_config, "personification_qq_expression_cooldown_seconds", 180) or 0)
    if cooldown > 0 and now_ts - float(state.get("last_sent", 0) or 0) < cooldown:
        return False
    daily_limit = int(getattr(plugin_config, "personification_qq_expression_daily_limit", 30) or 0)
    date = _today(now_ts)
    if state.get("date") != date:
        state["date"] = date
        state["count"] = 0
    if daily_limit > 0 and int(state.get("count", 0) or 0) >= daily_limit:
        return False
    return True


def _mark_auto_sent(key: str, face_id: int | None = None) -> None:
    now_ts = time.time()
    state = _QQ_EXPRESSION_STATE.setdefault(key, {"last_sent": 0.0, "date": _today(now_ts), "count": 0, "recent": []})
    if state.get("date") != _today(now_ts):
        state["date"] = _today(now_ts)
        state["count"] = 0
    state["last_sent"] = now_ts
    state["updated_at"] = now_ts
    state["count"] = int(state.get("count", 0) or 0) + 1
    recent = list(state.get("recent") or [])
    if face_id is not None:
        recent.append(int(face_id))
    state["recent"] = recent[-6:]


def choose_qq_expression_marker_for_context(
    *,
    mood_hint: str = "",
    context: str = "",
    semantic_frame: Any = None,
    mode: str = "face",
    rng: Any = random,
) -> str:
    normalized_mode = str(mode or "face").strip().lower()
    super_face = normalized_mode in {"super", "qq_super", "super_face", "text_qq_super"}
    triple = normalized_mode in {"triple", "qq_face_combo", "face_combo", "triple_face"}
    if normalized_mode in {"favorite", "qq_favorite"}:
        return "[QQ收藏表情:随机]"
    if normalized_mode in {"recommended", "qq_recommended"}:
        query = str(mood_hint or getattr(semantic_frame, "bot_emotion", "") or "开心").split("|", 1)[0].strip()[:12] or "开心"
        return f"[QQ推荐表情:{query}]"
    item = choose_qq_face_asset(
        mood_hint=mood_hint,
        context=context,
        semantic_frame=semantic_frame,
        super_only=super_face,
        rng=rng,
    )
    if item is None:
        return ""
    name = str(item.get("official_name") or item.get("id"))
    if super_face:
        if _super_mface_data_for_query(name, item) is None:
            return f"[QQ表情:{name}]"
        return f"[QQ超级表情:{name}]"
    if triple:
        return f"[QQ表情:{name}*3]"
    return f"[QQ表情:{name}]"


def maybe_choose_auto_qq_expression_marker(
    *,
    plugin_config: Any,
    semantic_frame: Any,
    reply_text: str,
    raw_message_text: str = "",
    message_intent: str = "",
    group_id: str = "",
    user_id: str = "",
    is_private: bool = False,
    is_random_chat: bool = False,
    force_mode: str | None = None,
    has_rich_sticker: bool = False,
    rng: Any = random,
) -> str:
    if contains_qq_expression_marker(reply_text):
        return ""
    intent = str(message_intent or "").strip()
    explicit = intent == "expression"
    if has_rich_sticker and not explicit:
        return ""
    if not explicit and not bool(getattr(semantic_frame, "sticker_appropriate", True)):
        return ""
    key = _state_key(str(group_id or ""), str(user_id or ""), is_private=is_private)
    if not _auto_allowed(plugin_config=plugin_config, key=key, explicit=explicit, rng=rng):
        return ""
    favorite_probability = _bounded_float(
        getattr(plugin_config, "personification_qq_favorite_expression_probability", 0.02),
        0.02,
    )
    super_probability = _bounded_float(
        getattr(plugin_config, "personification_qq_expression_super_probability", 0.02),
        0.02,
    )
    triple_probability = _bounded_float(
        getattr(plugin_config, "personification_qq_expression_triple_probability", 0.02),
        0.02,
    )
    mode = "face"
    if not explicit and not is_private:
        if is_random_chat and rng.random() < super_probability:
            mode = "super"
        elif is_random_chat and rng.random() < triple_probability:
            mode = "triple"
        elif favorite_probability > 0 and rng.random() < favorite_probability:
            mode = "recommended"
    if force_mode in {"qq_super", "text_qq_super"}:
        mode = "super"
    elif force_mode in {"qq_face_combo", "triple"}:
        mode = "triple"
    elif force_mode in {"qq_favorite", "qq_recommended"}:
        mode = "recommended"
    marker = choose_qq_expression_marker_for_context(
        mood_hint=str(getattr(semantic_frame, "sticker_mood_hint", "") or ""),
        context=f"{raw_message_text}\n{reply_text}",
        semantic_frame=semantic_frame,
        mode=mode,
        rng=rng,
    )
    if marker:
        item = None
        for part in split_qq_expression_markers(marker):
            if isinstance(part, QQExpressionRequest):
                item = _choose_explicit_or_safe_face(part.query, super_only=part.kind == "super_face")
                break
        _mark_auto_sent(key, int(item["id"]) if item else None)
    return marker


def reset_qq_expression_state() -> None:
    _QQ_EXPRESSION_STATE.clear()
    _QQ_MFACE_CACHE.clear()


__all__ = [
    "QQExpressionRender",
    "QQExpressionRequest",
    "build_qq_expression_prompt",
    "choose_qq_expression_marker_for_context",
    "contains_qq_expression_marker",
    "find_qq_face_asset",
    "history_text_for_qq_expression",
    "load_qq_expression_assets",
    "maybe_choose_auto_qq_expression_marker",
    "qq_expression_enabled",
    "qq_face_semantic_text",
    "remember_qq_mface_segment",
    "render_qq_expression_cq_text",
    "render_qq_expression_message",
    "reset_qq_expression_state",
    "semantic_text_for_qq_expression_segment",
    "split_qq_expression_markers",
    "strip_qq_expression_markers",
]
