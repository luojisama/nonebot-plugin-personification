from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .sticker_library import (
    ALLOWED_SCENE_TAGS,
    ALLOWED_MOOD_TAGS,
    DEFAULT_STICKER_DIR,
    SUPPORTED_STICKER_SUFFIXES,
    list_local_sticker_files,
    load_sticker_metadata,
    resolve_sticker_dir,
    save_sticker_metadata_sync,
)
from .metrics import record_counter

_CURATOR_BATCH_SIZE = 200
_TRASH_DIR_NAME = "trash"
_TRASH_RETENTION_DAYS = 7
_CURATION_LOG_NAME = "curation_log.jsonl"

CURATOR_SYSTEM_PROMPT = (
    "你是表情包库整理器。你会收到一批表情包的描述与标签信息（仅文本，无图片）。"
    "对每张表情包，你需要决定：保留、合并、重标、删除。输出严格 JSON 数组，不要 markdown。"
    "\nJSON 数组每项格式："
    '{"file_name":"xxx.png","action":"keep|merge_into|retag|remove","target_file":"","new_tags":{"mood_tags":["..."],"scene_tags":["..."]},"reason":"15字中文理由"}\n'
    "action 含义：\n"
    "- keep：保留，不做改动；\n"
    "- merge_into：与另一张高度重复，把当前文件的标签合并进 target_file（填重复文件的文件名），当前文件将被移入 trash 并在元数据中删除；\n"
    "- retag：标签错误或不准确，用 new_tags 给出修正后的 mood_tags 和 scene_tags；\n"
    "- remove：低质量/模糊/无复用价值，移入 trash 并删除元数据条目。\n"
    "要求：合并操作 target_file 必须是同批出现的其他文件名；不要对同一批中多个文件选择 merge_into 指向同一 target（即一个目标最多被合并一次）；"
    "保留和合并的数量应占总数的 70% 以上，不要过度清除。"
)


@dataclass
class CurationResult:
    keep_count: int = 0
    merge_count: int = 0
    retag_count: int = 0
    remove_count: int = 0
    error_count: int = 0
    details: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = []


def _trash_path(sticker_dir: Path, purge_date: str | None = None) -> Path:
    if purge_date is None:
        purge_date = date.today().isoformat()
    return sticker_dir / _TRASH_DIR_NAME / str(purge_date)


def _log_curation_action(sticker_dir: Path, log_entry: dict[str, Any]) -> None:
    log_path = sticker_dir / _CURATION_LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")


def _load_curation_log(sticker_dir: Path) -> list[dict[str, Any]]:
    log_path = sticker_dir / _CURATION_LOG_NAME
    if not log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict):
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries


def _cluster_signature(mood_tags: list[str], scene_tags: list[str]) -> str:
    mood_key = ",".join(sorted(set(t for t in mood_tags if str(t).strip()))) or "无"
    scene_key = ",".join(sorted(set(t for t in scene_tags if str(t).strip()))) or "无"
    return f"{mood_key}|{scene_key}"


def _cluster_stickers(metadata: dict[str, Any]) -> list[list[dict[str, Any]]]:
    clusters: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for file_name, entry in metadata.items():
        if file_name == "_meta" or not isinstance(entry, dict):
            continue
        mood_tags = list(entry.get("mood_tags") or [])
        scene_tags = list(entry.get("scene_tags") or [])
        sig = _cluster_signature(mood_tags, scene_tags)
        clusters[sig].append({"file_name": file_name, **entry})
    return [batch for batch in clusters.values() if batch]


def _parse_curation_response(raw: str, file_names: set[str]) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    results: list[dict[str, Any]] = []
    allowed_actions = {"keep", "merge_into", "retag", "remove"}
    for item in data:
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file_name", "") or "").strip()
        if file_name not in file_names:
            continue
        action = str(item.get("action", "") or "").strip().lower()
        if action not in allowed_actions:
            action = "keep"
        target = str(item.get("target_file", "") or "").strip()
        new_tags = item.get("new_tags", {})
        if not isinstance(new_tags, dict):
            new_tags = {}
        new_mood = [
            str(t).strip() for t in list(new_tags.get("mood_tags") or [])
            if str(t).strip() in ALLOWED_MOOD_TAGS
        ][:4]
        new_scene = [
            str(t).strip() for t in list(new_tags.get("scene_tags") or [])
            if str(t).strip() in ALLOWED_SCENE_TAGS
        ][:4]
        reason = str(item.get("reason", "") or "").strip()[:40]
        results.append({
            "file_name": file_name,
            "action": action,
            "target_file": target,
            "new_tags": {"mood_tags": new_mood, "scene_tags": new_scene},
            "reason": reason,
        })
    return results


async def _curate_batch(
    *,
    tool_caller: Any,
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    file_names = {item["file_name"] for item in batch}
    lines: list[str] = []
    for item in batch:
        fn = item["file_name"]
        desc = str(item.get("description", "") or "")[:100]
        use_hint = str(item.get("use_hint", "") or "")[:60]
        proactive = "是" if item.get("proactive_send") else "否"
        mood = ",".join(item.get("mood_tags", []) or [])
        scene = ",".join(item.get("scene_tags", []) or [])
        lines.append(f"- {fn}: {desc} | use_hint={use_hint} | 情绪={mood} | 场景={scene} | 主动发送={proactive}")

    user_content = f"本批 {len(batch)} 张表情包：\n" + "\n".join(lines)
    try:
        response = await asyncio.wait_for(
            tool_caller.chat_with_tools(
                messages=[
                    {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                tools=[],
                use_builtin_search=False,
            ),
            timeout=30.0,
        )
    except Exception:
        return [{"file_name": fn, "action": "keep", "target_file": "", "new_tags": {"mood_tags": [], "scene_tags": []}, "reason": "馆长调用失败"} for fn in file_names]

    content = str(getattr(response, "content", "") or "").strip()
    return _parse_curation_response(content, file_names)


def _apply_curation_action(
    action: dict[str, Any],
    *,
    sticker_dir: Path,
    metadata: dict[str, Any],
    result: CurationResult,
    log_target: list[dict[str, Any]],
) -> None:
    file_name = action["file_name"]
    action_type = action["action"]
    target_file = action.get("target_file", "")
    new_tags = action.get("new_tags", {})
    reason = action.get("reason", "")

    # 在变更前快照原始元数据，便于回滚
    original_entry = metadata.get(file_name, {})
    original_snapshot: dict[str, Any] = {}
    if isinstance(original_entry, dict):
        original_snapshot = {
            "description": original_entry.get("description", ""),
            "mood_tags": list(original_entry.get("mood_tags", []) or []),
            "scene_tags": list(original_entry.get("scene_tags", []) or []),
            "proactive_send": bool(original_entry.get("proactive_send", False)),
            "style": str(original_entry.get("style", "anime") or "anime"),
            "use_hint": original_entry.get("use_hint", ""),
            "avoid_hint": original_entry.get("avoid_hint", ""),
            "ocr_text": original_entry.get("ocr_text", ""),
            "weight": original_entry.get("weight", 1.0),
        }

    log_entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file_name": file_name,
        "action": action_type,
        "target_file": target_file,
        "new_tags": new_tags,
        "reason": reason,
        "original_entry": original_snapshot,
    }

    if action_type == "keep":
        result.keep_count += 1
        log_target.append(log_entry)
        return

    if action_type == "remove":
        result.remove_count += 1
        purge_date = date.today().isoformat()
        src = sticker_dir / file_name
        dst_dir = _trash_path(sticker_dir, purge_date)
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.move(str(src), str(dst_dir / file_name))
        metadata.pop(file_name, None)
        _log_curation_action(sticker_dir, log_entry)
        log_target.append(log_entry)
        record_counter("sticker.curator_remove")
        return

    if action_type == "retag":
        result.retag_count += 1
        entry = metadata.get(file_name, {})
        if isinstance(entry, dict):
            if new_tags.get("mood_tags"):
                entry["mood_tags"] = new_tags["mood_tags"]
            if new_tags.get("scene_tags"):
                entry["scene_tags"] = new_tags["scene_tags"]
        _log_curation_action(sticker_dir, log_entry)
        log_target.append(log_entry)
        record_counter("sticker.curator_retag")
        return

    if action_type == "merge_into":
        if not target_file or target_file not in metadata:
            result.error_count += 1
            return
        result.merge_count += 1
        target_entry = metadata.get(target_file, {})
        source_entry = metadata.get(file_name, {})
        if isinstance(target_entry, dict) and isinstance(source_entry, dict):
            for key in ("mood_tags", "scene_tags"):
                existing = set(target_entry.get(key, []) or [])
                extra = [t for t in (source_entry.get(key, []) or []) if t not in existing]
                if extra:
                    target_entry[key] = (target_entry.get(key, []) or []) + extra
        purge_date = date.today().isoformat()
        src = sticker_dir / file_name
        dst_dir = _trash_path(sticker_dir, purge_date)
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.move(str(src), str(dst_dir / file_name))
        metadata.pop(file_name, None)
        _log_curation_action(sticker_dir, log_entry)
        log_target.append(log_entry)
        record_counter("sticker.curator_merge")


async def _run_curation(
    *,
    runtime: Any,
    sticker_dir: Path,
    tool_caller: Any,
    max_stickers: int = _CURATOR_BATCH_SIZE,
) -> CurationResult:
    result = CurationResult()
    metadata = load_sticker_metadata(sticker_dir)
    clusters = _cluster_stickers(metadata)
    if not clusters:
        result.details.append("表情包库中没有可整理的条目。")
        return result

    log_entries: list[dict[str, Any]] = []
    total_processed = 0

    for cluster in clusters:
        if total_processed >= max_stickers:
            result.details.append(f"已达单次处理上限 {max_stickers} 张，剩余批次跳过。")
            break
        batch = cluster[:max(1, max_stickers - total_processed)]
        if len(batch) <= 1:
            continue

        actions = await _curate_batch(tool_caller=tool_caller, batch=batch)
        target_seen: set[str] = set()
        for action in actions:
            action_type = action.get("action", "keep")
            target_file = action.get("target_file", "")
            if action_type == "merge_into":
                if target_file in target_seen or target_file not in metadata:
                    action["action"] = "keep"
                    action["reason"] = action.get("reason", "") + "|merge_blocked"
                else:
                    target_seen.add(target_file)
            _apply_curation_action(
                action,
                sticker_dir=sticker_dir,
                metadata=metadata,
                result=result,
                log_target=log_entries,
            )
        total_processed += len(batch)

    if any(a["action"] != "keep" for a in log_entries):
        save_sticker_metadata_sync(sticker_dir, metadata)
        result.details.append(
            f"整理完成：保留 {result.keep_count}、合并 {result.merge_count}、"
            f"重标 {result.retag_count}、移除 {result.remove_count}、"
            f"错误 {result.error_count}"
        )
    else:
        result.details.append("整理结果：无需变动。")
    return result


async def run_sticker_curation(
    *,
    runtime: Any,
    tool_caller: Any = None,
    max_stickers: int = _CURATOR_BATCH_SIZE,
) -> CurationResult:
    if not bool(getattr(runtime.plugin_config, "personification_sticker_curator_enabled", False)):
        return CurationResult(details=["表情包馆长功能未启用。"])

    sticker_dir = resolve_sticker_dir(
        getattr(runtime.plugin_config, "personification_sticker_path", None),
        create=True,
    )
    if not sticker_dir.exists() or not sticker_dir.is_dir():
        return CurationResult(details=["表情包库目录不存在。"])

    effective_tool_caller = tool_caller or getattr(runtime, "lite_tool_caller", None) or getattr(runtime, "agent_tool_caller", None)
    if effective_tool_caller is None:
        return CurationResult(details=["无法获取 LLM 调用器。"])

    return await _run_curation(
        runtime=runtime,
        sticker_dir=sticker_dir,
        tool_caller=effective_tool_caller,
        max_stickers=max_stickers,
    )


def clean_trash_expired(sticker_dir: str | Path | None, *, retention_days: int = _TRASH_RETENTION_DAYS) -> int:
    base_dir = resolve_sticker_dir(sticker_dir)
    trash_root = base_dir / _TRASH_DIR_NAME
    if not trash_root.exists():
        return 0
    cutoff_date = date.today() - timedelta(days=max(1, int(retention_days)))
    removed = 0
    for subdir in sorted(trash_root.iterdir()):
        if not subdir.is_dir():
            continue
        try:
            dir_date = date.fromisoformat(subdir.name)
        except (ValueError, TypeError):
            continue
        if dir_date < cutoff_date:
            shutil.rmtree(str(subdir), ignore_errors=True)
            removed += 1
    return removed


def rollback_last_curation(sticker_dir: str | Path | None) -> CurationResult:
    base_dir = resolve_sticker_dir(sticker_dir)
    result = CurationResult(details=[])
    entries = _load_curation_log(base_dir)
    if not entries:
        result.details.append("没有可回滚的整理记录。")
        return result

    # 每次 run_curation 写多条 log，按时间分组回滚最近一次
    entries.reverse()
    session_start = entries[0]["ts"]
    session_entries: list[dict[str, Any]] = []
    for entry in entries:
        if entry["ts"] == session_start:
            session_entries.append(entry)
        elif abs(_parse_ts_seconds(entry["ts"]) - _parse_ts_seconds(session_start)) < 10:
            session_entries.append(entry)
        else:
            break

    metadata = load_sticker_metadata(base_dir)
    rolled = 0
    for entry in session_entries:
        action_type = entry.get("action", "")
        file_name = entry.get("file_name", "")
        original_entry = entry.get("original_entry", {})
        if not isinstance(original_entry, dict):
            original_entry = {}
        if action_type == "remove" or action_type == "merge_into":
            # 从 trash 恢复文件
            restored = False
            trash_root = base_dir / _TRASH_DIR_NAME
            if trash_root.exists():
                for subdir in sorted(trash_root.iterdir(), reverse=True):
                    trashed_file = subdir / file_name
                    if trashed_file.exists():
                        try:
                            shutil.move(str(trashed_file), str(base_dir / file_name))
                            restored = True
                        except Exception:
                            pass
                        break
            if restored:
                rolled += 1
                # 用 log 内的快照恢复元数据；若无则退化到极简条目
                if original_entry:
                    metadata[file_name] = {
                        "description": original_entry.get("description", file_name),
                        "mood_tags": list(original_entry.get("mood_tags", []) or []),
                        "scene_tags": list(original_entry.get("scene_tags", []) or []),
                        "proactive_send": bool(original_entry.get("proactive_send", False)),
                        "style": str(original_entry.get("style", "anime") or "anime"),
                        "use_hint": original_entry.get("use_hint", ""),
                        "avoid_hint": original_entry.get("avoid_hint", ""),
                        "ocr_text": original_entry.get("ocr_text", ""),
                        "weight": original_entry.get("weight", 1.0),
                    }
                else:
                    metadata[file_name] = {
                        "description": file_name,
                        "mood_tags": [],
                        "scene_tags": [],
                        "proactive_send": False,
                        "style": "anime",
                    }
        elif action_type == "retag":
            entry_data = metadata.get(file_name, {})
            if isinstance(entry_data, dict) and original_entry:
                entry_data["mood_tags"] = list(original_entry.get("mood_tags", []) or [])
                entry_data["scene_tags"] = list(original_entry.get("scene_tags", []) or [])
                rolled += 1

    save_sticker_metadata_sync(base_dir, metadata)
    result.details.append(f"已回滚 {rolled} 个操作。")
    return result


def _parse_ts_seconds(ts: str) -> float:
    try:
        import time as _time
        return _time.mktime(_time.strptime(str(ts), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


__all__ = [
    "CURATOR_SYSTEM_PROMPT",
    "CurationResult",
    "clean_trash_expired",
    "rollback_last_curation",
    "run_sticker_curation",
]
