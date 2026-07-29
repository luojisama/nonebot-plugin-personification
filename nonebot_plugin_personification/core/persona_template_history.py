from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .data_store import get_data_store
from .paths import get_data_dir


_NAMESPACE = "persona_template_history"
_MAX_RECORDS = 80
_MAX_PER_CHARACTER = 10


def _normalize_key_part(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def build_persona_template_key(work_title: str, character_name: str) -> str:
    return f"{_normalize_key_part(work_title)}::{_normalize_key_part(character_name)}"


def make_persona_template_record(
    result: dict[str, Any],
    *,
    actor: str = "",
    source: str = "webui",
) -> dict[str, Any]:
    work_title = str(result.get("work_title", "") or "").strip()
    character_name = str(result.get("character_name", "") or "").strip()
    created_at = time.time()
    digest_source = f"{work_title}\0{character_name}\0{created_at}\0{source}"
    record_id = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:16]
    return {
        "record_id": record_id,
        "key": build_persona_template_key(work_title, character_name),
        "work_title": work_title,
        "character_name": character_name,
        "created_at": created_at,
        "actor": str(actor or ""),
        "source": str(source or "webui"),
        "template_valid": bool(result.get("template_valid")),
        "duration_ms": int(result.get("duration_ms") or 0),
        "source_count": len(result.get("sources") or []),
        "subagent_count": len(result.get("subagents") or []),
        "result": result,
    }


def save_persona_template_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized_record = dict(record)
    normalized_record["key"] = normalized_record.get("key") or build_persona_template_key(
        str(normalized_record.get("work_title", "") or ""),
        str(normalized_record.get("character_name", "") or ""),
    )

    def _mutate(current: Any) -> dict[str, Any]:
        payload = current if isinstance(current, dict) else {}
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
        records = [item for item in records if item.get("record_id") != normalized_record.get("record_id")]
        records.append(normalized_record)
        records.sort(key=lambda item: float(item.get("created_at", 0) or 0), reverse=True)

        per_key_count: dict[str, int] = {}
        trimmed: list[dict[str, Any]] = []
        for item in records:
            key = str(item.get("key", "") or "")
            per_key_count[key] = per_key_count.get(key, 0) + 1
            if per_key_count[key] > _MAX_PER_CHARACTER:
                continue
            trimmed.append(item)
            if len(trimmed) >= _MAX_RECORDS:
                break

        payload["records"] = trimmed
        payload["updated_at"] = time.time()
        return payload

    get_data_store().mutate_sync(_NAMESPACE, _mutate)
    return normalized_record


def record_persona_template_result(
    result: dict[str, Any],
    *,
    actor: str = "",
    source: str = "webui",
) -> dict[str, Any]:
    return save_persona_template_record(
        make_persona_template_record(result, actor=actor, source=source)
    )


def list_persona_template_records(
    *,
    limit: int = 20,
    work_title: str = "",
    character_name: str = "",
) -> list[dict[str, Any]]:
    payload = get_data_store().load_sync(_NAMESPACE)
    records = [item for item in payload.get("records", []) if isinstance(item, dict)] if isinstance(payload, dict) else []
    work_key = _normalize_key_part(work_title)
    character_key = _normalize_key_part(character_name)
    if work_key or character_key:
        records = [
            item
            for item in records
            if (not work_key or _normalize_key_part(item.get("work_title")) == work_key)
            and (not character_key or _normalize_key_part(item.get("character_name")) == character_key)
        ]
    records.sort(key=lambda item: float(item.get("created_at", 0) or 0), reverse=True)
    return records[: max(1, min(int(limit or 20), 100))]


def get_latest_persona_template_record(work_title: str, character_name: str) -> dict[str, Any] | None:
    records = list_persona_template_records(
        limit=1,
        work_title=work_title,
        character_name=character_name,
    )
    return records[0] if records else None


def get_persona_template_record(record_id: str) -> dict[str, Any] | None:
    wanted = str(record_id or "")
    return next(
        (record for record in list_persona_template_records(limit=100) if str(record.get("record_id") or "") == wanted),
        None,
    )


def delete_persona_template_record(record_id: str) -> dict[str, Any] | None:
    deleted: dict[str, Any] | None = None

    def _mutate(current: Any) -> dict[str, Any]:
        nonlocal deleted
        payload = current if isinstance(current, dict) else {}
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
        kept: list[dict[str, Any]] = []
        for record in records:
            if deleted is None and str(record.get("record_id") or "") == str(record_id or ""):
                deleted = record
            else:
                kept.append(record)
        payload["records"] = kept
        payload["updated_at"] = time.time()
        return payload

    get_data_store().mutate_sync(_NAMESPACE, _mutate)
    return deleted


def append_persona_template_apply_audit(record_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
    updated: dict[str, Any] | None = None

    def _mutate(current: Any) -> dict[str, Any]:
        nonlocal updated
        payload = current if isinstance(current, dict) else {}
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
        for record in records:
            if str(record.get("record_id") or "") != str(record_id or ""):
                continue
            audit = [item for item in record.get("profile_apply_audit", []) if isinstance(item, dict)]
            audit.append({**event, "created_at": time.time()})
            record["profile_apply_audit"] = audit[-50:]
            updated = record
            break
        payload["records"] = records
        payload["updated_at"] = time.time()
        return payload

    get_data_store().mutate_sync(_NAMESPACE, _mutate)
    return updated


def summarize_persona_template_record(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    avatar_review = result.get("avatar_review_summary") if isinstance(result.get("avatar_review_summary"), dict) else {}
    verified_count = int(
        avatar_review.get("verified_count")
        or len([
            item
            for item in list(result.get("avatar_candidates") or [])
            if isinstance(item, dict) and item.get("vision_status") == "verified"
        ])
    )
    return {
        "record_id": record.get("record_id", ""),
        "work_title": record.get("work_title", ""),
        "character_name": record.get("character_name", ""),
        "created_at": record.get("created_at", 0),
        "actor": record.get("actor", ""),
        "source": record.get("source", ""),
        "template_valid": bool(record.get("template_valid")),
        "duration_ms": int(record.get("duration_ms") or 0),
        "source_count": int(record.get("source_count") or len(result.get("sources") or [])),
        "subagent_count": int(record.get("subagent_count") or len(result.get("subagents") or [])),
        "template_keys": list(result.get("template_keys") or [])[:32],
        "revision": str(result.get("revision") or ""),
        "avatar_candidate_count": verified_count,
        "verified_avatar_count": verified_count,
        "avatar_reviewed_count": int(avatar_review.get("reviewed_count") or 0),
        "signature_candidate_count": len(result.get("signature_candidates") or []),
        "profile_status": str(result.get("profile_status") or ""),
    }


def render_persona_template_export(record_or_result: dict[str, Any]) -> str:
    record = record_or_result
    result = record.get("result") if isinstance(record.get("result"), dict) else record
    work_title = str(result.get("work_title") or record.get("work_title") or "")
    character_name = str(result.get("character_name") or record.get("character_name") or "")
    lines = [
        f"人设构建结果：{work_title} / {character_name}",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(record.get('created_at', time.time()) or time.time())))}",
        f"YAML 校验：{'通过' if result.get('template_valid') else '需要检查'}",
        f"耗时：{int(result.get('duration_ms') or record.get('duration_ms') or 0)} ms",
        "",
    ]
    errors = [str(x) for x in [*(result.get("template_errors") or []), *(result.get("template_warnings") or [])] if str(x).strip()]
    if errors:
        lines.append("校验提示：")
        lines.extend(f"- {item}" for item in errors[:20])
        lines.append("")

    lines.append("插件 YAML 模板：")
    lines.append(str(result.get("template") or "").strip())

    sources = result.get("sources") or []
    if sources:
        lines.extend(["", "资料来源："])
        for index, source in enumerate(sources[:20], start=1):
            if not isinstance(source, dict):
                continue
            title = source.get("title") or source.get("query") or f"资料 {index}"
            lines.append(f"[S{index}] {source.get('source') or source.get('kind') or '资料'} - {title}")
            if source.get("url"):
                lines.append(str(source.get("url")))
            if source.get("summary"):
                lines.append(str(source.get("summary")))

    subagents = result.get("subagents") or []
    if subagents:
        lines.extend(["", "子agent报告："])
        for item in subagents[:8]:
            if not isinstance(item, dict):
                continue
            report = item.get("report") if isinstance(item.get("report"), dict) else {}
            lines.append(f"## {item.get('name') or '子agent'} - {item.get('focus') or ''}")
            if report:
                lines.append(json.dumps(report, ensure_ascii=False, indent=2))
            elif item.get("raw"):
                lines.append(str(item.get("raw")))

    return "\n".join(lines).strip() + "\n"


def write_persona_template_export_file(record_or_result: dict[str, Any], *, plugin_config: Any = None) -> Path:
    result = record_or_result.get("result") if isinstance(record_or_result.get("result"), dict) else record_or_result
    work = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", str(result.get("work_title") or "work")).strip("_")[:64] or "work"
    character = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", str(result.get("character_name") or "character")).strip("_")[:64] or "character"
    record_id = str(record_or_result.get("record_id") or int(time.time()))
    target_dir = Path(get_data_dir(plugin_config)) / "persona_template_exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{work}_{character}_{record_id}.yaml.txt"
    target.write_text(render_persona_template_export(record_or_result), encoding="utf-8")
    return target.resolve()


__all__ = [
    "append_persona_template_apply_audit",
    "build_persona_template_key",
    "delete_persona_template_record",
    "get_latest_persona_template_record",
    "get_persona_template_record",
    "list_persona_template_records",
    "record_persona_template_result",
    "render_persona_template_export",
    "save_persona_template_record",
    "summarize_persona_template_record",
    "write_persona_template_export_file",
]
