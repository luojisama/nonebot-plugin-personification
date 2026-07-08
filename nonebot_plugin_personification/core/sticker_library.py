from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import mimetypes
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from filelock import FileLock

from .media_understanding import analyze_images_with_route_or_fallback
from .sticker_semantics import ALLOWED_STICKER_MOOD_TAGS, ALLOWED_STICKER_SCENE_TAGS


DEFAULT_STICKER_DIR = Path("data/stickers")
STICKER_SCHEMA_VERSION = 3
SUPPORTED_STICKER_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
RESOLVABLE_STICKER_SUFFIXES = SUPPORTED_STICKER_SUFFIXES | {".gif"}
ALLOWED_MOOD_TAGS = set(ALLOWED_STICKER_MOOD_TAGS)
ALLOWED_SCENE_TAGS = set(ALLOWED_STICKER_SCENE_TAGS)

STICKER_VISION_PROMPT = """你是表情包语义分析器。请完整理解这张表情包（静态图、GIF关键帧拼图或截图），并严格返回 JSON。
不要输出 markdown，不要输出解释，只返回一个 JSON 对象。

{
  "summary": "25字内概括这张图最核心的意思",
  "description": "20-60字，说明主体、动作、表情、画面关系和文字信息",
  "ocr_text": "图中文字，没有就填空字符串",
  "use_hint": "适合在什么场景发，15-40字",
  "avoid_hint": "明显不适合在哪些场景发，15-40字",
  "mood_tags": ["情绪标签"],
  "scene_tags": ["场景标签"],
  "proactive_send": true,
  "should_collect": true,
  "collect_confidence": 0.0,
  "collect_reason": "为什么值得收藏，15字内",
  "is_sticker": true,
  "style": "anime"
}

要求：
1. 必须结合画面主体、动作/表情、人物关系、图中文字一起理解；ocr_text 要尽量抄准图中文字，不确定也要保守摘录。
1b. description 必须写出“谁/什么主体 + 正在做什么动作 + 表情/情绪 + 文字和动作合起来造成的表达效果”。
1c. 如果这是 GIF 关键帧拼图，请按时间顺序理解动作变化，不要当作多张无关图片；description/use_hint 要体现短动画的动作递进和最终想表达的效果。
2. mood_tags 只允许从以下标签里选 1-4 个：搞笑、开心、感动、尴尬、无语、惊讶、委屈、生气、害羞、得意、困惑、赞同、拒绝、期待、失落、撒娇、淡定、震惊。
3. scene_tags 只允许从以下标签里选 1-4 个：回应笑点、接梗、表达赞同、化解尴尬、自嘲、反驳、表达惊讶、安慰对方、撒娇、表示无奈、冷场时、表达期待、庆祝、拒绝请求、结束对话、打招呼、表达关心、吐槽、卖萌、表达疑惑。
4. should_collect 为 true 只在这张图语义明确、可复用、不是普通照片时给出。
5. 如果内容不清晰，也要保守填写，并把 should_collect 设为 false。
6. is_sticker 为 false 当且仅当这张图是截图、实拍照片、UI界面截图等非表情包内容；卡通、二次元、meme、梗图等创作图给 true。
7. style 从 ["anime", "meme", "other"] 中选一个：
   anime = 二次元/卡通/手绘风格（包括 Q 版、chibi、日系插画等）；
   meme = 梗图/真人改图/表情包但含真实人物；
   other = 其他（截图、实拍等）。
8. collect_confidence 是 0.0-1.0 的小数，表示对 should_collect 判断的置信度：
   语义明确、复用价值高、画面清晰、独特性强 → 0.8-1.0；
   一般可收藏但不出彩 → 0.5-0.8；
   模糊/普通/可能重复 → 0.0-0.5；
    should_collect=false 时填 0.0。"""

STICKER_SECOND_JUDGE_PROMPT = """你是表情包库二次审核器。已有一张新表情包的视觉描述与标签，以及库中 N 张已有表情包的描述对照。
你需要判断这张新图是否值得收集入库，还是重复/低价值/饱和。
严格返回 JSON，不要 markdown。

{
  "decision": "collect",
  "redundant_with": [],
  "tag_correction": {"mood_tags": [], "scene_tags": []},
  "reason": "..."
}

decision 四选一：
- collect：新图不重复、有价值，建议入库；
- skip_duplicate：与冗余候选高度重复（构图/文字/情绪几乎一致），把重复候选文件名写入 redundant_with；
- skip_low_value：画面不清晰、文字无关、信息量低；
- skip_oversaturated：同类情绪/场景在库中已很多。

tag_correction 仅在一次标注有误时更正 mood_tags 和 scene_tags，否则填空数组。
reason 20字内中文。"""

STICKER_RESEARCH_QUERY_PROMPT = """你是表情包打标检索规划器。根据视觉初稿判断是否需要联网查证角色、作品、梗出处或图中文字含义。
只输出 JSON，不要 markdown。

{
  "queries": ["检索词1", "检索词2"],
  "reason": "为什么需要或不需要联网"
}

要求：
1. 只有出现可识别角色名、作品名、梗句、截图文字、网络流行梗或不确定出处时才给 queries。
2. 普通表情、无明确文字、纯表情动作、无法识别主体时返回空数组。
3. 每个 query 必须短而具体，优先中文；最多 2 个。
4. 不要编造角色名或出处。"""

STICKER_RESEARCH_REFINE_PROMPT = STICKER_VISION_PROMPT + """

额外上下文：下面会给出第一轮视觉初稿和联网检索摘要。请重新检查图片语义、OCR、角色/作品/梗信息，输出最终 JSON。
约束：
1. 联网资料只用于消除角色、作品、梗句或出处的不确定性；不要把搜索结果里无关内容塞进描述。
2. 如果联网资料与图片不匹配，优先相信图片和 OCR，并在 use_hint/avoid_hint 上保持保守。
3. description/use_hint/avoid_hint 要面向“什么时候适合发这张表情包”，不是写百科介绍。
4. 仍然只能返回 JSON 对象。"""


@dataclass(frozen=True)
class StickerVisionResult:
    summary: str
    description: str
    ocr_text: str
    use_hint: str
    avoid_hint: str
    mood_tags: list[str]
    scene_tags: list[str]
    proactive_send: bool
    should_collect: bool
    collect_reason: str
    is_sticker: bool = True
    style: str = "anime"
    vision_route: str = ""
    collect_confidence: float = 0.0


def resolve_sticker_dir(raw_path: str | Path | None, *, create: bool = False) -> Path:
    text = str(raw_path or "").strip().strip('"').strip("'")
    path = Path(text).expanduser() if text else DEFAULT_STICKER_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def list_local_sticker_files(sticker_dir: str | Path | None, *, include_gif: bool = False) -> list[Path]:
    base_dir = resolve_sticker_dir(sticker_dir)
    if not base_dir.exists() or not base_dir.is_dir():
        return []
    allowed_suffixes = RESOLVABLE_STICKER_SUFFIXES if include_gif else SUPPORTED_STICKER_SUFFIXES
    return sorted(
        file
        for file in base_dir.iterdir()
        if file.is_file() and file.suffix.lower() in allowed_suffixes
    )


def sticker_metadata_path(sticker_dir: str | Path | None) -> Path:
    return resolve_sticker_dir(sticker_dir) / "stickers.json"


def sticker_metadata_lock_path(sticker_dir: str | Path | None) -> Path:
    return resolve_sticker_dir(sticker_dir) / "stickers.json.lock"


def compute_file_hash(payload: bytes) -> str:
    return hashlib.sha256(bytes(payload or b"")).hexdigest()


def compute_folder_hash(files: Iterable[Path]) -> str:
    hasher = hashlib.sha256()
    for file in files:
        stat = file.stat()
        hasher.update(file.name.encode("utf-8"))
        hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
        hasher.update(str(stat.st_size).encode("utf-8"))
    return hasher.hexdigest()


def image_file_to_data_url(file_path: Path) -> str:
    mime_type = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
    payload = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def image_bytes_to_data_url(payload: bytes, mime_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(bytes(payload or b"")).decode("ascii")
    safe_mime = str(mime_type or "").strip() or "image/jpeg"
    return f"data:{safe_mime};base64,{encoded}"


def normalize_sticker_entry(
    value: Any,
    *,
    file_name: str = "",
    model_name: str = "",
    vision_route: str = "",
    file_hash: str = "",
    source_kind: str = "",
    source_group_id: str = "",
    source_user_id: str = "",
    collected_at: str = "",
) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"description": value}
    if not isinstance(value, dict):
        value = {}

    mood_tags = [
        str(tag).strip()
        for tag in list(value.get("mood_tags") or [])
        if str(tag).strip() in ALLOWED_MOOD_TAGS
    ][:4]
    scene_tags = [
        str(tag).strip()
        for tag in list(value.get("scene_tags") or [])
        if str(tag).strip() in ALLOWED_SCENE_TAGS
    ][:4]
    raw_weight = value.get("weight", 1.0)
    try:
        weight = max(0.0, min(3.0, float(raw_weight if raw_weight is not None else 1.0)))
    except (TypeError, ValueError):
        weight = 1.0
    entry = {
        "description": str(value.get("description", "") or file_name or "图片内容不清晰").strip() or "图片内容不清晰",
        "mood_tags": mood_tags or ["淡定"],
        "scene_tags": scene_tags or ["表达疑惑"],
        "proactive_send": bool(value.get("proactive_send", False)),
        "ocr_text": str(value.get("ocr_text", "") or "").strip(),
        "use_hint": str(value.get("use_hint", "") or "").strip(),
        "avoid_hint": str(value.get("avoid_hint", "") or "").strip(),
        "style": str(value.get("style", "") or "anime").strip().lower(),
        "weight": round(weight, 2),
        "file_hash": str(value.get("file_hash", "") or file_hash).strip(),
        "collected_at": str(value.get("collected_at", "") or collected_at).strip(),
        "source_kind": str(value.get("source_kind", "") or source_kind).strip(),
        "source_group_id": str(value.get("source_group_id", "") or source_group_id).strip(),
        "source_user_id": str(value.get("source_user_id", "") or source_user_id).strip(),
        "vision_route": str(value.get("vision_route", "") or vision_route).strip(),
        "labeled_at": str(value.get("labeled_at", "") or time.strftime("%Y-%m-%d %H:%M")).strip(),
        "model": str(value.get("model", "") or model_name).strip(),
    }
    return entry


def normalize_sticker_metadata(data: Any, *, files: Iterable[Path] | None = None) -> dict[str, Any]:
    normalized_files = list(files or [])
    existing_names = {file.name for file in normalized_files}
    loaded = data if isinstance(data, dict) else {}
    metadata: dict[str, Any] = {}
    for key, value in loaded.items():
        if key == "_meta":
            continue
        file_name = str(key or "").strip()
        if not file_name:
            continue
        if existing_names and file_name not in existing_names:
            continue
        metadata[file_name] = normalize_sticker_entry(value, file_name=Path(file_name).stem)

    for file in normalized_files:
        metadata.setdefault(file.name, normalize_sticker_entry({}, file_name=file.stem))

    meta_value = loaded.get("_meta", {}) if isinstance(loaded, dict) else {}
    folder_hash = str(meta_value.get("folder_hash", "") or "").strip()
    metadata["_meta"] = {
        "folder_hash": folder_hash or compute_folder_hash(normalized_files),
        "schema_version": STICKER_SCHEMA_VERSION,
    }
    return metadata


def load_sticker_metadata(sticker_dir: str | Path | None) -> dict[str, Any]:
    base_dir = resolve_sticker_dir(sticker_dir)
    files = list_local_sticker_files(base_dir, include_gif=True)
    path = sticker_metadata_path(base_dir)
    if not path.exists():
        return normalize_sticker_metadata({}, files=files)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    return normalize_sticker_metadata(loaded, files=files)


def save_sticker_metadata_sync(sticker_dir: str | Path | None, metadata: dict[str, Any]) -> None:
    base_dir = resolve_sticker_dir(sticker_dir, create=True)
    files = list_local_sticker_files(base_dir, include_gif=True)
    normalized = normalize_sticker_metadata(metadata, files=files)
    normalized["_meta"]["folder_hash"] = compute_folder_hash(files)
    path = sticker_metadata_path(base_dir)
    lock_path = sticker_metadata_lock_path(base_dir)
    with FileLock(str(lock_path)):
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")


async def save_sticker_metadata(sticker_dir: str | Path | None, metadata: dict[str, Any]) -> None:
    await asyncio.to_thread(save_sticker_metadata_sync, sticker_dir, metadata)


def find_sticker_by_hash(metadata: dict[str, Any], file_hash: str) -> str:
    target = str(file_hash or "").strip()
    if not target:
        return ""
    for file_name, entry in (metadata or {}).items():
        if file_name == "_meta" or not isinstance(entry, dict):
            continue
        if str(entry.get("file_hash", "") or "").strip() == target:
            return file_name
    return ""


def _safe_stem(text: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(text or "").strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:40] or "sticker"


def save_collected_sticker_sync(
    sticker_dir: str | Path | None,
    *,
    payload: bytes,
    mime_type: str,
    file_name_hint: str = "",
) -> tuple[Path, bool, str]:
    base_dir = resolve_sticker_dir(sticker_dir, create=True)
    metadata = load_sticker_metadata(base_dir)
    file_hash = compute_file_hash(payload)
    existing_name = find_sticker_by_hash(metadata, file_hash)
    if existing_name:
        return base_dir / existing_name, False, file_hash

    suffix = mimetypes.guess_extension(str(mime_type or "").split(";")[0].strip()) or ".png"
    suffix = suffix.lower()
    if suffix not in RESOLVABLE_STICKER_SUFFIXES:
        suffix = ".png"
    file_name = f"{_safe_stem(file_name_hint)}_{file_hash[:12]}{suffix}"
    target = base_dir / file_name
    target.write_bytes(bytes(payload or b""))
    return target, True, file_hash


async def save_collected_sticker(
    sticker_dir: str | Path | None,
    *,
    payload: bytes,
    mime_type: str,
    file_name_hint: str = "",
) -> tuple[Path, bool, str]:
    return await asyncio.to_thread(
        save_collected_sticker_sync,
        sticker_dir,
        payload=payload,
        mime_type=mime_type,
        file_name_hint=file_name_hint,
    )


def _parse_json_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return bool(default)


def normalize_sticker_vision_result(raw: Any, *, vision_route: str = "", meme_policy: str = "reject") -> StickerVisionResult:
    data = _parse_json_payload(raw)
    summary = str(data.get("summary", "") or "").strip()
    description = str(data.get("description", "") or summary or "图片内容不清晰").strip() or "图片内容不清晰"
    ocr_text = str(data.get("ocr_text", "") or "").strip()
    use_hint = str(data.get("use_hint", "") or "").strip()
    avoid_hint = str(data.get("avoid_hint", "") or "").strip()
    mood_tags = [
        str(tag).strip()
        for tag in list(data.get("mood_tags") or [])
        if str(tag).strip() in ALLOWED_MOOD_TAGS
    ][:4]
    scene_tags = [
        str(tag).strip()
        for tag in list(data.get("scene_tags") or [])
        if str(tag).strip() in ALLOWED_SCENE_TAGS
    ][:4]
    if not summary:
        summary = description[:40]
    style = str(data.get("style", "anime") or "anime").strip().lower()
    if style not in {"anime", "meme", "other"}:
        style = "other"
    try:
        collect_confidence = float(data.get("collect_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        collect_confidence = 0.0
    collect_confidence = max(0.0, min(1.0, collect_confidence))
    result = StickerVisionResult(
        summary=summary,
        description=description,
        ocr_text=ocr_text,
        use_hint=use_hint,
        avoid_hint=avoid_hint,
        mood_tags=mood_tags or ["淡定"],
        scene_tags=scene_tags or ["表达疑惑"],
        proactive_send=_normalize_bool(data.get("proactive_send"), False),
        should_collect=_normalize_bool(data.get("should_collect"), False),
        collect_reason=str(data.get("collect_reason", "") or "").strip(),
        is_sticker=_normalize_bool(data.get("is_sticker"), True),
        style=style,
        vision_route=str(vision_route or data.get("vision_route", "") or "").strip(),
        collect_confidence=collect_confidence,
    )
    if result.style != "anime":
        if meme_policy == "reject":
            result = dataclasses.replace(result, should_collect=False)
        elif meme_policy == "accept" and result.style == "meme":
            pass
        elif meme_policy == "review":
            pass
        else:
            result = dataclasses.replace(result, should_collect=False)
    return result


def _sticker_labeler_research_enabled(runtime: Any) -> bool:
    cfg = getattr(runtime, "plugin_config", None)
    return bool(getattr(cfg, "personification_sticker_labeler_research_enabled", True))


def _sticker_labeler_research_max_queries(runtime: Any) -> int:
    cfg = getattr(runtime, "plugin_config", None)
    try:
        value = int(getattr(cfg, "personification_sticker_labeler_research_max_queries", 2) or 0)
    except (TypeError, ValueError):
        value = 2
    return max(0, min(4, value))


def _sticker_labeler_research_timeout(runtime: Any) -> float:
    cfg = getattr(runtime, "plugin_config", None)
    try:
        value = float(getattr(cfg, "personification_sticker_labeler_research_timeout", 12.0) or 12.0)
    except (TypeError, ValueError):
        value = 12.0
    return max(1.0, min(60.0, value))


def _parse_sticker_research_queries(raw: Any, *, limit: int) -> list[str]:
    if limit <= 0:
        return []
    payload = _parse_json_payload(raw)
    queries_raw = payload.get("queries") if isinstance(payload, dict) else []
    if not isinstance(queries_raw, list):
        return []
    queries: list[str] = []
    seen: set[str] = set()
    for item in queries_raw:
        query = re.sub(r"\s+", " ", str(item or "")).strip()
        if not query or len(query) > 80:
            continue
        lowered = query.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        queries.append(query)
        if len(queries) >= limit:
            break
    return queries


def _fallback_sticker_research_queries(result: StickerVisionResult, *, limit: int) -> list[str]:
    if limit <= 0:
        return []
    seeds: list[str] = []
    ocr_text = re.sub(r"\s+", " ", result.ocr_text).strip()
    if 2 <= len(ocr_text) <= 40:
        seeds.append(f"{ocr_text} 表情包 梗")
    if result.style == "meme" and result.summary:
        seeds.append(f"{result.summary[:30]} 表情包")
    out: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        key = seed.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(seed)
        if len(out) >= limit:
            break
    return out


async def _plan_sticker_research_queries(
    *,
    runtime: Any,
    result: StickerVisionResult,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    caller = getattr(runtime, "lite_call_ai_api", None) or getattr(runtime, "call_ai_api", None)
    if caller is None:
        return _fallback_sticker_research_queries(result, limit=limit)
    payload = {
        "summary": result.summary,
        "description": result.description,
        "ocr_text": result.ocr_text,
        "style": result.style,
        "mood_tags": result.mood_tags,
        "scene_tags": result.scene_tags,
        "use_hint": result.use_hint,
        "avoid_hint": result.avoid_hint,
    }
    messages = [
        {"role": "system", "content": STICKER_RESEARCH_QUERY_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        raw = await caller(messages, max_tokens=240, temperature=0.1, use_builtin_search=False)
    except TypeError:
        try:
            raw = await caller(messages)
        except Exception:
            return _fallback_sticker_research_queries(result, limit=limit)
    except Exception:
        return _fallback_sticker_research_queries(result, limit=limit)
    payload = _parse_json_payload(raw)
    if isinstance(payload.get("queries"), list):
        return _parse_sticker_research_queries(payload, limit=limit)
    return _fallback_sticker_research_queries(result, limit=limit)


async def _run_sticker_research(
    *,
    runtime: Any,
    queries: list[str],
    timeout_s: float,
) -> str:
    if not queries:
        return ""
    from .web_grounding import do_web_search

    logger = getattr(runtime, "logger", None)

    def _now() -> Any:
        getter = getattr(runtime, "get_current_time", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return datetime.now()

    async def _search_one(query: str) -> str:
        try:
            return await do_web_search(
                query,
                context_hint="表情包打标：只查角色、作品、梗出处或图中文字含义",
                get_now=_now,
                logger=logger,
            )
        except Exception:
            return ""

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[_search_one(query) for query in queries], return_exceptions=True),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return ""
    parts: list[str] = []
    for query, raw in zip(queries, results):
        text = "" if isinstance(raw, BaseException) else str(raw or "").strip()
        if text:
            parts.append(f"### query: {query}\n{text[:1800]}")
    return "\n\n".join(parts).strip()


async def _refine_sticker_with_research(
    *,
    runtime: Any,
    image_refs: Sequence[str],
    initial: StickerVisionResult,
    research_context: str,
    fallback_vision_caller: Any = None,
    meme_policy: str,
) -> StickerVisionResult:
    if not research_context:
        return initial
    initial_payload = {
        "summary": initial.summary,
        "description": initial.description,
        "ocr_text": initial.ocr_text,
        "use_hint": initial.use_hint,
        "avoid_hint": initial.avoid_hint,
        "mood_tags": initial.mood_tags,
        "scene_tags": initial.scene_tags,
        "proactive_send": initial.proactive_send,
        "should_collect": initial.should_collect,
        "collect_confidence": initial.collect_confidence,
        "collect_reason": initial.collect_reason,
        "is_sticker": initial.is_sticker,
        "style": initial.style,
    }
    prompt = (
        STICKER_RESEARCH_REFINE_PROMPT
        + "\n\n【第一轮视觉初稿】\n"
        + json.dumps(initial_payload, ensure_ascii=False)
        + "\n\n【联网检索摘要】\n"
        + research_context[:3600]
    )
    try:
        raw, route = await analyze_images_with_route_or_fallback(
            runtime=runtime,
            prompt=prompt,
            image_refs=image_refs,
            fallback_vision_caller=fallback_vision_caller,
        )
    except Exception:
        return initial
    refined = normalize_sticker_vision_result(raw, vision_route=route, meme_policy=meme_policy)
    route_suffix = f"{refined.vision_route}+web_research" if refined.vision_route else "web_research"
    return dataclasses.replace(refined, vision_route=route_suffix)


async def analyze_sticker_image(
    *,
    runtime: Any,
    image_refs: Sequence[str],
    prompt: str = STICKER_VISION_PROMPT,
    fallback_vision_caller: Any = None,
) -> StickerVisionResult:
    meme_policy = str(getattr(runtime.plugin_config, "personification_sticker_collect_meme_policy", "reject") or "reject").strip().lower()
    raw, route = await analyze_images_with_route_or_fallback(
        runtime=runtime,
        prompt=prompt,
        image_refs=image_refs,
        fallback_vision_caller=fallback_vision_caller,
    )
    initial = normalize_sticker_vision_result(raw, vision_route=route, meme_policy=meme_policy)
    if not _sticker_labeler_research_enabled(runtime):
        return initial
    timeout_s = _sticker_labeler_research_timeout(runtime)

    async def _research_and_refine() -> StickerVisionResult:
        queries = await _plan_sticker_research_queries(
            runtime=runtime,
            result=initial,
            limit=_sticker_labeler_research_max_queries(runtime),
        )
        research_context = await _run_sticker_research(
            runtime=runtime,
            queries=queries,
            timeout_s=timeout_s,
        )
        return await _refine_sticker_with_research(
            runtime=runtime,
            image_refs=image_refs,
            initial=initial,
            research_context=research_context,
            fallback_vision_caller=fallback_vision_caller,
            meme_policy=meme_policy,
        )

    try:
        return await asyncio.wait_for(_research_and_refine(), timeout=timeout_s + 2.0)
    except Exception:
        return initial


def recall_similar_stickers(
    metadata: dict[str, Any],
    mood_tags: list[str],
    scene_tags: list[str],
    *,
    top_k: int = 12,
) -> list[dict[str, Any]]:
    mood_set = set(tag for tag in mood_tags if str(tag).strip())
    scene_set = set(tag for tag in scene_tags if str(tag).strip())
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for file_name, entry in (metadata or {}).items():
        if file_name == "_meta" or not isinstance(entry, dict):
            continue
        entry_mood = set(tag for tag in (entry.get("mood_tags") or []) if str(tag).strip())
        entry_scene = set(tag for tag in (entry.get("scene_tags") or []) if str(tag).strip())
        overlap = len(mood_set & entry_mood) + len(scene_set & entry_scene)
        if overlap > 0:
            scored.append((overlap, file_name, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"file_name": file_name, "description": entry.get("description", ""), "use_hint": entry.get("use_hint", ""),
         "mood_tags": entry.get("mood_tags", []), "scene_tags": entry.get("scene_tags", []),
         "overlap_score": score}
        for score, file_name, entry in scored[:top_k]
    ]


async def judge_sticker_against_library(
    *,
    runtime: Any,
    sticker_data_url: str,
    sticker_summary: str,
    sticker_description: str,
    sticker_mood_tags: list[str],
    sticker_scene_tags: list[str],
    similar_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    import re as _re
    candidate_text = "\n".join(
        f"- {item['file_name']}: {item['description'][:80]} | use_hint={item.get('use_hint', '')[:60]}"
        for item in similar_candidates
    ) or "无候选"

    prompt = STICKER_SECOND_JUDGE_PROMPT + (
        f"\n---\n新表情包：summary={sticker_summary}\n"
        f"description={sticker_description}\n"
        f"mood_tags={','.join(sticker_mood_tags)}\n"
        f"scene_tags={','.join(sticker_scene_tags)}\n"
        f"库中候选：\n{candidate_text}\n"
        f"候选数量：{len(similar_candidates)} 张"
    )
    try:
        from ...core.visual_capabilities import VISUAL_ROUTE_REPLY_PLAIN
        raw, _route = await analyze_images_with_route_or_fallback(
            runtime=runtime,
            prompt=prompt,
            image_refs=[sticker_data_url],
            route_name=VISUAL_ROUTE_REPLY_PLAIN,
            fallback_vision_caller=runtime.vision_caller,
        )
    except Exception:
        return {"decision": "collect", "redundant_with": [], "tag_correction": {"mood_tags": [], "scene_tags": []}, "reason": "二次判断失败，放行"}

    text = str(raw or "").strip()
    try:
        data = json.loads(text)
    except Exception:
        match = _re.search(r"\{.*\}", text, flags=_re.DOTALL)
        if not match:
            return {"decision": "collect", "redundant_with": [], "tag_correction": {"mood_tags": [], "scene_tags": []}, "reason": "解析失败，放行"}
        try:
            data = json.loads(match.group(0))
        except Exception:
            return {"decision": "collect", "redundant_with": [], "tag_correction": {"mood_tags": [], "scene_tags": []}, "reason": "解析失败，放行"}
    if not isinstance(data, dict):
        return {"decision": "collect", "redundant_with": [], "tag_correction": {"mood_tags": [], "scene_tags": []}, "reason": "非JSON，放行"}

    decision = str(data.get("decision", "collect") or "collect").strip()
    if decision not in {"collect", "skip_duplicate", "skip_low_value", "skip_oversaturated"}:
        decision = "collect"
    redundant = data.get("redundant_with", [])
    if not isinstance(redundant, list):
        redundant = []
    tag_correction = data.get("tag_correction", {})
    if not isinstance(tag_correction, dict):
        tag_correction = {}
    reason = str(data.get("reason", "") or "").strip()
    return {
        "decision": decision,
        "redundant_with": [str(item) for item in redundant[:5]],
        "tag_correction": tag_correction,
        "reason": reason[:40],
    }


def render_sticker_semantic_summary(entry: dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return ""
    description = str(entry.get("description", "") or "").strip()
    ocr_text = str(entry.get("ocr_text", "") or "").strip()
    use_hint = str(entry.get("use_hint", "") or "").strip()
    avoid_hint = str(entry.get("avoid_hint", "") or "").strip()
    mood_tags = "、".join(str(tag) for tag in (entry.get("mood_tags") or [])[:4] if str(tag).strip())
    scene_tags = "、".join(str(tag) for tag in (entry.get("scene_tags") or [])[:4] if str(tag).strip())
    parts: list[str] = []
    if description:
        parts.append(f"主体与动作：{description}")
    if ocr_text:
        parts.append(f"图中文字：{ocr_text}")
    if use_hint:
        parts.append(f"适用：{use_hint}")
    if avoid_hint:
        parts.append(f"不适用：{avoid_hint}")
    if mood_tags:
        parts.append(f"情绪标签：{mood_tags}")
    if scene_tags:
        parts.append(f"场景标签：{scene_tags}")
    return "；".join(parts)


def normalize_label_result(
    raw: str,
    *,
    model_name: str = "",
    vision_route: str = "",
    file_hash: str = "",
    source_kind: str = "",
    source_group_id: str = "",
    source_user_id: str = "",
    collected_at: str = "",
) -> dict[str, Any]:
    normalized = normalize_sticker_vision_result(raw, vision_route=vision_route)
    return normalize_sticker_entry(
        {
            "description": normalized.description,
            "mood_tags": normalized.mood_tags,
            "scene_tags": normalized.scene_tags,
            "proactive_send": normalized.proactive_send,
            "ocr_text": normalized.ocr_text,
            "use_hint": normalized.use_hint,
            "avoid_hint": normalized.avoid_hint,
            "style": normalized.style,
        },
        model_name=model_name,
        vision_route=normalized.vision_route,
        file_hash=file_hash,
        source_kind=source_kind,
        source_group_id=source_group_id,
        source_user_id=source_user_id,
        collected_at=collected_at,
    )


__all__ = [
    "DEFAULT_STICKER_DIR",
    "RESOLVABLE_STICKER_SUFFIXES",
    "STICKER_SCHEMA_VERSION",
    "STICKER_VISION_PROMPT",
    "STICKER_SECOND_JUDGE_PROMPT",
    "STICKER_RESEARCH_QUERY_PROMPT",
    "STICKER_RESEARCH_REFINE_PROMPT",
    "SUPPORTED_STICKER_SUFFIXES",
    "StickerVisionResult",
    "analyze_sticker_image",
    "compute_file_hash",
    "compute_folder_hash",
    "find_sticker_by_hash",
    "image_bytes_to_data_url",
    "image_file_to_data_url",
    "judge_sticker_against_library",
    "list_local_sticker_files",
    "load_sticker_metadata",
    "normalize_label_result",
    "normalize_sticker_entry",
    "normalize_sticker_metadata",
    "normalize_sticker_vision_result",
    "recall_similar_stickers",
    "render_sticker_semantic_summary",
    "resolve_sticker_dir",
    "save_collected_sticker",
    "save_collected_sticker_sync",
    "save_sticker_metadata",
    "save_sticker_metadata_sync",
    "sticker_metadata_path",
]
