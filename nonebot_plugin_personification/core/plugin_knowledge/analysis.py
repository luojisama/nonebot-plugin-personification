from __future__ import annotations

import json
import re
from typing import Any

from ...skills.skillpacks.tool_caller.scripts.impl import ToolCaller


_BATCH_MAX_SOURCE_CHARS = 12_000
_BATCH_MAX_CHUNKS = 4


class LlmJsonObjectError(ValueError):
    def __init__(self, message: str, *, raw_preview: str = "") -> None:
        super().__init__(message)
        self.raw_preview = str(raw_preview or "").strip()


class PluginAnalysisError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        phase: str,
        raw_preview: str = "",
        failed_batch_index: int = 0,
        failed_batch_total: int = 0,
    ) -> None:
        super().__init__(message)
        self.phase = str(phase or "").strip() or "unknown"
        self.raw_preview = str(raw_preview or "").strip()
        self.failed_batch_index = int(failed_batch_index or 0)
        self.failed_batch_total = int(failed_batch_total or 0)


def _extract_json_object(raw: Any) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dedupe_texts(values: list[Any], *, limit: int = 64) -> list[str]:
    deduped: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in deduped:
            continue
        deduped.append(text)
        if len(deduped) >= limit:
            break
    return deduped


def _dedupe_dicts(values: list[Any], *, limit: int = 64) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(value))
        if len(deduped) >= limit:
            break
    return deduped


def _normalize_feature_group_key(feature: dict[str, Any], fallback_index: int) -> str:
    for candidate in (feature.get("feature_key"), feature.get("title"), feature.get("summary")):
        text = str(candidate or "").strip().lower()
        if text:
            normalized = re.sub(r"[\s\-:/]+", "_", text)
            normalized = re.sub(r"[^0-9a-z_\u4e00-\u9fff]+", "", normalized)
            return normalized or f"feature_{fallback_index}"
    return f"feature_{fallback_index}"


def _sorted_source_chunks(source_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = source_snapshot.get("chunks", [])
    if not isinstance(chunks, list):
        return []
    ordered = [
        dict(chunk)
        for chunk in chunks
        if isinstance(chunk, dict) and str(chunk.get("text", "") or "").strip()
    ]
    ordered.sort(
        key=lambda item: (
            str(item.get("file", "") or ""),
            int(item.get("start_line", 0) or 0),
            str(item.get("chunk_id", "") or ""),
        )
    )
    return ordered


def _build_source_batches(source_snapshot: dict[str, Any]) -> list[list[dict[str, Any]]]:
    ordered_chunks = _sorted_source_chunks(source_snapshot)
    if not ordered_chunks:
        return []

    batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_chars = 0

    for chunk in ordered_chunks:
        chunk_text = str(chunk.get("text", "") or "")
        estimated = len(chunk_text) + 120
        if current_batch and (
            len(current_batch) >= _BATCH_MAX_CHUNKS
            or current_chars + estimated > _BATCH_MAX_SOURCE_CHARS
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(chunk)
        current_chars += estimated

    if current_batch:
        batches.append(current_batch)
    return batches


def _render_source_chunks(chunks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        rel_path = str(chunk.get("file", "") or "unknown.py")
        start_line = int(chunk.get("start_line", 1) or 1)
        end_line = int(chunk.get("end_line", start_line) or start_line)
        text = str(chunk.get("text", "") or "").rstrip()
        if not text:
            continue
        parts.append(f"# === {rel_path}:{start_line}-{end_line} ===\n{text}")
    return "\n\n".join(parts).strip()


def _analysis_strategy(source_snapshot: dict[str, Any]) -> str:
    strategy = str(source_snapshot.get("analysis_strategy", "") or "").strip().lower()
    if strategy in {"full_source", "module_bundles"}:
        return strategy
    return "chunk_batches"


def _chunk_lookup(source_snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for chunk in _sorted_source_chunks(source_snapshot):
        chunk_id = str(chunk.get("chunk_id", "") or "").strip()
        if chunk_id:
            lookup[chunk_id] = chunk
    return lookup


def _build_analysis_units(source_snapshot: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    ordered_chunks = _sorted_source_chunks(source_snapshot)
    if not ordered_chunks:
        return "chunk_batches", []
    strategy = _analysis_strategy(source_snapshot)
    if strategy == "full_source":
        return "full_source_single_unit", [
            {
                "unit_key": "full_source",
                "unit_label": "完整插件源码",
                "unit_kind": "full_source",
                "chunks": ordered_chunks,
            }
        ]
    if strategy == "module_bundles":
        chunk_map = _chunk_lookup(source_snapshot)
        units: list[dict[str, Any]] = []
        for bundle in list(source_snapshot.get("module_bundles") or []):
            if not isinstance(bundle, dict):
                continue
            chunk_ids = [str(item or "").strip() for item in list(bundle.get("chunk_ids") or []) if str(item or "").strip()]
            chunks = [chunk_map[item] for item in chunk_ids if item in chunk_map]
            if not chunks:
                continue
            split_index = int(bundle.get("split_index", 1) or 1)
            split_total = int(bundle.get("split_total", 1) or 1)
            module_label = str(bundle.get("module_label", "") or bundle.get("module_key", "") or "module").strip()
            if split_total > 1:
                unit_label = f"{module_label}（第 {split_index}/{split_total} 段）"
            else:
                unit_label = module_label
            units.append(
                {
                    "unit_key": str(bundle.get("bundle_key", "") or unit_label),
                    "unit_label": unit_label,
                    "module_key": str(bundle.get("module_key", "") or module_label),
                    "unit_kind": "module_bundle",
                    "chunks": chunks,
                }
            )
        if units:
            return "module_bundle_multistage", units
    fallback_units = []
    for index, batch in enumerate(_build_source_batches(source_snapshot), start=1):
        fallback_units.append(
            {
                "unit_key": f"batch_{index}",
                "unit_label": f"源码批次 {index}",
                "unit_kind": "chunk_batch",
                "chunks": batch,
            }
        )
    return "chunk_batch_multistage", fallback_units


async def _call_json(tool_caller: ToolCaller, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    response = await tool_caller.chat_with_tools(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        tools=[],
        use_builtin_search=False,
    )
    raw_content = str(getattr(response, "content", "") or "").strip()
    parsed = _extract_json_object(raw_content)
    if not parsed:
        raise LlmJsonObjectError(
            "llm result is not a JSON object",
            raw_preview=raw_content[:600],
        )
    return parsed


def _build_batch_fallback_summary(
    *,
    plugin_name: str,
    batch_index: int,
    total_batches: int,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    keywords: list[str] = []
    entrypoints: list[dict[str, Any]] = []
    seen_files: set[str] = set()

    for index, chunk in enumerate(chunks, start=1):
        file_path = str(chunk.get("file", "") or "").strip()
        if not file_path:
            continue
        symbols = _dedupe_texts(list(chunk.get("symbols") or []), limit=8)
        start_line = int(chunk.get("start_line", 1) or 1)
        end_line = int(chunk.get("end_line", start_line) or start_line)
        location = f"{file_path}:{start_line}-{end_line}"
        title = file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or f"源码片段 {index}"
        summary = (
            f"{plugin_name} 的第 {batch_index}/{total_batches} 批源码来自 {file_path}，"
            "本批次使用确定性回退摘要。"
        )
        implementation = summary
        if symbols:
            implementation += f" 关键符号：{', '.join(symbols[:4])}。"
        features.append(
            {
                "feature_key": f"batch_{batch_index}_chunk_{index}",
                "title": title,
                "summary": summary,
                "implementation": implementation,
                "keywords": _dedupe_texts([file_path, *symbols], limit=10),
                "config_items": [],
                "files": [file_path],
                "symbols": symbols,
            }
        )
        keywords.extend([file_path, *symbols])
        if file_path not in seen_files:
            seen_files.add(file_path)
            entrypoints.append(
                {
                    "kind": "other",
                    "name": title,
                    "location": location,
                    "description": "源码批次回退摘要入口",
                }
            )

    return {
        "keywords": _dedupe_texts(keywords, limit=24),
        "triggers": [],
        "entrypoints": entrypoints,
        "features": features,
        "config_items": [],
        "data_access": [],
        "dependencies": [],
        "architecture_notes": [
            f"{plugin_name} 的第 {batch_index}/{total_batches} 批源码使用了确定性回退摘要。"
        ],
        "implementation_notes": [
            "该批次 LLM 结构化分析失败，当前结果来自源码文件名、行号和符号信息。"
        ],
    }


async def _analyze_source_unit_with_llm(
    *,
    plugin_name: str,
    unit_index: int,
    total_units: int,
    unit_label: str,
    unit_kind: str,
    chunks: list[dict[str, Any]],
    tool_caller: ToolCaller,
) -> dict[str, Any]:
    system_prompt = (
        "你是 NoneBot2 插件源码分析器。"
        "你当前只分析一个插件源码分析单元。"
        "必须严格依据给定源码，不要猜测未出现的实现。"
        "只输出一个 JSON 对象，不要输出 markdown，不要解释。"
    )
    user_prompt = (
        "请分析以下源码分析单元，并总结这一单元涉及的功能和实现。\n"
        "输出 JSON 结构必须为：\n"
        "{\n"
        '  "keywords": ["关键词"],\n'
        '  "triggers": [{"type":"command|keyword|message|notice|regex|schedule|api|other","pattern":"触发方式","description":"说明"}],\n'
        '  "entrypoints": [{"kind":"command|matcher|event|scheduler|api|hook|other","name":"入口名","location":"文件:行号或文件","description":"说明"}],\n'
        '  "features": [\n'
        '    {\n'
        '      "feature_key":"稳定键名",\n'
        '      "title":"功能名",\n'
        '      "summary":"功能简介",\n'
        '      "keywords":["触发词"],\n'
        '      "config_items":["配置项"],\n'
        '      "implementation":"这一批源码里该功能的实现方式",\n'
        '      "files":["相关文件"],\n'
        '      "symbols":["关键函数/类"]\n'
        "    }\n"
        "  ],\n"
        '  "config_items": [{"name":"配置项","type":"str|int|bool|float|unknown","default":"默认值","description":"说明","location":"文件"}],\n'
        '  "data_access": [{"kind":"database|file|cache|network|memory|external|unknown","target":"目标","location":"文件","description":"说明"}],\n'
        '  "dependencies": ["依赖"],\n'
        '  "architecture_notes": ["结构说明"],\n'
        '  "implementation_notes": ["实现细节说明"]\n'
        "}\n\n"
        f"插件名: {plugin_name}\n"
        f"分析模式: {unit_kind}\n"
        f"当前分析单元: {unit_index}/{total_units}\n"
        f"当前单元标签: {unit_label}\n"
        f"当前单元 chunk 数: {len(chunks)}\n\n"
        f"{_render_source_chunks(chunks)}"
    )
    return await _call_json(tool_caller, system_prompt=system_prompt, user_prompt=user_prompt)


def _merge_batch_summaries(batch_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    keywords: list[Any] = []
    triggers: list[Any] = []
    entrypoints: list[Any] = []
    data_access: list[Any] = []
    dependencies: list[Any] = []
    architecture_notes: list[Any] = []
    implementation_notes: list[Any] = []
    config_items: list[Any] = []
    feature_groups: dict[str, dict[str, Any]] = {}

    for batch_summary in batch_summaries:
        if not isinstance(batch_summary, dict):
            continue
        keywords.extend(list(batch_summary.get("keywords") or []))
        triggers.extend(list(batch_summary.get("triggers") or []))
        entrypoints.extend(list(batch_summary.get("entrypoints") or []))
        data_access.extend(list(batch_summary.get("data_access") or []))
        dependencies.extend(list(batch_summary.get("dependencies") or []))
        architecture_notes.extend(list(batch_summary.get("architecture_notes") or []))
        implementation_notes.extend(list(batch_summary.get("implementation_notes") or []))
        config_items.extend(list(batch_summary.get("config_items") or []))

        for index, feature in enumerate(list(batch_summary.get("features") or []), start=1):
            if not isinstance(feature, dict):
                continue
            group_key = _normalize_feature_group_key(feature, fallback_index=index)
            current = feature_groups.setdefault(
                group_key,
                {
                    "feature_key": str(feature.get("feature_key", "") or "").strip(),
                    "title": str(feature.get("title", "") or "").strip(),
                    "summary_candidates": [],
                    "implementation_candidates": [],
                    "keywords": [],
                    "config_items": [],
                    "files": [],
                    "symbols": [],
                },
            )
            if not current["feature_key"]:
                current["feature_key"] = str(feature.get("feature_key", "") or "").strip()
            if not current["title"]:
                current["title"] = str(feature.get("title", "") or "").strip()
            summary = str(feature.get("summary", "") or "").strip()
            implementation = str(feature.get("implementation", "") or "").strip()
            if summary:
                current["summary_candidates"].append(summary)
            if implementation:
                current["implementation_candidates"].append(implementation)
            current["keywords"].extend(list(feature.get("keywords") or []))
            current["config_items"].extend(list(feature.get("config_items") or []))
            current["files"].extend(list(feature.get("files") or []))
            current["symbols"].extend(list(feature.get("symbols") or []))

    merged_features: list[dict[str, Any]] = []
    for index, feature in enumerate(feature_groups.values(), start=1):
        summary_candidates = _dedupe_texts(list(feature.get("summary_candidates") or []), limit=6)
        implementation_candidates = _dedupe_texts(list(feature.get("implementation_candidates") or []), limit=8)
        merged_features.append(
            {
                "feature_key": str(feature.get("feature_key", "") or "").strip() or f"feature_{index}",
                "title": str(feature.get("title", "") or "").strip() or f"功能 {index}",
                "summary": max(summary_candidates, key=len) if summary_candidates else "",
                "implementation": "\n".join(implementation_candidates[:4]).strip(),
                "keywords": _dedupe_texts(list(feature.get("keywords") or []), limit=12),
                "config_items": _dedupe_texts(list(feature.get("config_items") or []), limit=12),
                "files": _dedupe_texts(list(feature.get("files") or []), limit=16),
                "symbols": _dedupe_texts(list(feature.get("symbols") or []), limit=16),
            }
        )

    return {
        "keywords": _dedupe_texts(keywords, limit=32),
        "triggers": _dedupe_dicts(triggers, limit=48),
        "entrypoints": _dedupe_dicts(entrypoints, limit=64),
        "features": merged_features,
        "config_items": _dedupe_dicts(config_items, limit=64),
        "data_access": _dedupe_dicts(data_access, limit=48),
        "dependencies": _dedupe_texts(dependencies, limit=24),
        "architecture_notes": _dedupe_texts(architecture_notes, limit=24),
        "implementation_notes": _dedupe_texts(implementation_notes, limit=24),
    }


def _build_fallback_plugin_analysis(
    *,
    plugin_name: str,
    source_snapshot: dict[str, Any],
    merged_summary: dict[str, Any],
) -> dict[str, Any]:
    config_schema: dict[str, dict[str, Any]] = {}
    for item in list(merged_summary.get("config_items") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name or name in config_schema:
            continue
        config_schema[name] = {
            "type": str(item.get("type", "") or "unknown"),
            "default": str(item.get("default", "") or ""),
            "description": str(item.get("description", "") or ""),
            "location": str(item.get("location", "") or ""),
        }

    feature_map: dict[str, dict[str, Any]] = {}
    implementation_map: list[dict[str, Any]] = []
    for index, feature in enumerate(list(merged_summary.get("features") or []), start=1):
        if not isinstance(feature, dict):
            continue
        feature_key = str(feature.get("feature_key", "") or "").strip() or f"feature_{index}"
        title = str(feature.get("title", "") or "").strip() or feature_key
        summary = str(feature.get("summary", "") or "").strip()
        implementation = str(feature.get("implementation", "") or "").strip()
        files = _dedupe_texts(list(feature.get("files") or []), limit=16)
        symbols = _dedupe_texts(list(feature.get("symbols") or []), limit=16)
        config_items = _dedupe_texts(list(feature.get("config_items") or []), limit=12)
        feature_map[feature_key] = {
            "title": title,
            "keywords": _dedupe_texts(list(feature.get("keywords") or []), limit=12),
            "summary": summary,
            "config_items": config_items,
            "detail": implementation or summary,
            "implementation": implementation or summary,
            "files": files,
            "symbols": symbols,
        }
        implementation_map.append(
            {
                "feature_key": feature_key,
                "title": title,
                "flow": implementation or summary,
                "files": files,
                "symbols": symbols,
                "notes": "",
            }
        )

    files = source_snapshot.get("files", [])
    display_name = plugin_name
    summary = ""
    if merged_summary.get("architecture_notes"):
        summary = str(list(merged_summary.get("architecture_notes") or [""])[0] or "").strip()
    if not summary and feature_map:
        summary = str(next(iter(feature_map.values())).get("summary", "") or "").strip()
    if not summary and isinstance(files, list) and files:
        summary = f"{plugin_name} 的插件知识库已基于完整源码构建。"

    architecture_summary = "；".join(_dedupe_texts(list(merged_summary.get("architecture_notes") or []), limit=6)).strip()
    if not architecture_summary:
        architecture_summary = summary

    return {
        "display_name": display_name,
        "summary": summary or f"{plugin_name} 的插件知识库已基于完整源码构建。",
        "keywords": _dedupe_texts(list(merged_summary.get("keywords") or []), limit=24),
        "triggers": _dedupe_dicts(list(merged_summary.get("triggers") or []), limit=48),
        "features": feature_map,
        "config_schema": config_schema,
        "dependencies": _dedupe_texts(list(merged_summary.get("dependencies") or []), limit=24),
        "architecture_summary": architecture_summary,
        "entrypoints": _dedupe_dicts(list(merged_summary.get("entrypoints") or []), limit=64),
        "implementation_map": _dedupe_dicts(implementation_map, limit=64),
        "data_access": _dedupe_dicts(list(merged_summary.get("data_access") or []), limit=48),
    }


def _normalize_final_analysis(
    raw: dict[str, Any],
    *,
    plugin_name: str,
    source_snapshot: dict[str, Any],
    analyzed_batch_count: int,
    analysis_mode: str,
) -> dict[str, Any]:
    normalized = raw if isinstance(raw, dict) else {}
    features = normalized.get("features", {})
    if isinstance(features, list):
        feature_map: dict[str, Any] = {}
        for index, feature in enumerate(features, start=1):
            if not isinstance(feature, dict):
                continue
            feature_key = str(feature.get("feature_key", "") or "").strip() or f"feature_{index}"
            feature_map[feature_key] = feature
        features = feature_map
    if not isinstance(features, dict):
        features = {}

    for index, (feature_key, feature) in enumerate(list(features.items()), start=1):
        if not isinstance(feature, dict):
            features[feature_key] = {
                "title": str(feature_key or f"feature_{index}"),
                "keywords": [],
                "summary": "",
                "config_items": [],
                "detail": "",
                "implementation": "",
                "files": [],
                "symbols": [],
            }
            continue
        feature["title"] = str(feature.get("title", "") or feature_key).strip()
        feature["keywords"] = _dedupe_texts(list(feature.get("keywords") or []), limit=12)
        feature["summary"] = str(feature.get("summary", "") or "").strip()
        feature["config_items"] = _dedupe_texts(list(feature.get("config_items") or []), limit=12)
        feature["detail"] = str(feature.get("detail", "") or feature.get("summary", "") or "").strip()
        feature["implementation"] = str(feature.get("implementation", "") or feature["detail"]).strip()
        feature["files"] = _dedupe_texts(list(feature.get("files") or []), limit=16)
        feature["symbols"] = _dedupe_texts(list(feature.get("symbols") or []), limit=16)

    config_schema = normalized.get("config_schema", {})
    if not isinstance(config_schema, dict):
        config_schema = {}

    normalized["display_name"] = str(normalized.get("display_name", "") or plugin_name).strip() or plugin_name
    normalized["summary"] = str(normalized.get("summary", "") or "").strip()
    normalized["keywords"] = _dedupe_texts(list(normalized.get("keywords") or []), limit=24)
    normalized["triggers"] = _dedupe_dicts(list(normalized.get("triggers") or []), limit=48)
    normalized["features"] = features
    normalized["config_schema"] = config_schema
    normalized["dependencies"] = _dedupe_texts(list(normalized.get("dependencies") or []), limit=24)
    normalized["architecture_summary"] = str(normalized.get("architecture_summary", "") or normalized["summary"]).strip()
    normalized["entrypoints"] = _dedupe_dicts(list(normalized.get("entrypoints") or []), limit=64)
    normalized["implementation_map"] = _dedupe_dicts(list(normalized.get("implementation_map") or []), limit=64)
    normalized["data_access"] = _dedupe_dicts(list(normalized.get("data_access") or []), limit=48)
    normalized["analysis_mode"] = analysis_mode
    normalized["analyzed_batch_count"] = int(analyzed_batch_count)
    normalized["analyzed_unit_count"] = int(analyzed_batch_count)
    normalized["analyzed_chunk_count"] = len(_sorted_source_chunks(source_snapshot))
    normalized["analyzed_file_count"] = len(list(source_snapshot.get("files") or []))
    normalized["analyzed_module_count"] = int(source_snapshot.get("module_bundle_count", 0) or 0)
    return normalized


async def _synthesize_plugin_analysis_with_llm(
    *,
    plugin_name: str,
    source_snapshot: dict[str, Any],
    merged_summary: dict[str, Any],
    tool_caller: ToolCaller,
) -> dict[str, Any]:
    system_prompt = (
        "你是 NoneBot2 插件总分析器。"
        "你拿到的是对插件完整源码逐批分析后的中间结果。"
        "请输出最终插件知识 JSON。"
        "只输出一个 JSON 对象，不要输出 markdown，不要解释。"
    )
    user_prompt = (
        "请基于以下“完整源码分析单元结果”生成最终插件知识条目。\n"
        "重点要求：功能描述和实现方式都要完整，技术细节回答要能落到文件、函数、数据流。\n"
        "输出 JSON 结构必须为：\n"
        "{\n"
        '  "display_name":"插件名",\n'
        '  "summary":"一句话简介",\n'
        '  "keywords":["关键词"],\n'
        '  "triggers":[{"type":"command|keyword|message|notice|regex|schedule|api|other","pattern":"触发方式","description":"说明"}],\n'
        '  "features": {\n'
        '    "feature_key": {\n'
        '      "title":"功能名",\n'
        '      "keywords":["关键词"],\n'
        '      "summary":"功能简介",\n'
        '      "config_items":["配置项"],\n'
        '      "detail":"功能详细说明",\n'
        '      "implementation":"实现方式说明",\n'
        '      "files":["关键文件"],\n'
        '      "symbols":["关键函数/类"]\n'
        "    }\n"
        "  },\n"
        '  "config_schema": {"CONFIG_KEY":{"type":"str|int|bool|float|unknown","default":"默认值","description":"说明","location":"文件"}},\n'
        '  "dependencies":["依赖"],\n'
        '  "architecture_summary":"插件整体架构和主流程说明",\n'
        '  "entrypoints":[{"kind":"command|matcher|event|scheduler|api|hook|other","name":"入口名","location":"文件或文件:行号","description":"说明"}],\n'
        '  "implementation_map":[{"feature_key":"功能键","title":"功能名","flow":"执行路径/调用链","files":["关键文件"],"symbols":["关键函数/类"],"notes":"技术细节"}],\n'
        '  "data_access":[{"kind":"database|file|cache|network|memory|external|unknown","target":"目标","location":"文件","description":"说明"}]\n'
        "}\n\n"
        f"插件名: {plugin_name}\n"
        f"源码文件数: {len(list(source_snapshot.get('files') or []))}\n"
        f"源码 chunk 数: {len(_sorted_source_chunks(source_snapshot))}\n"
        f"源码文件清单: {json.dumps(list(source_snapshot.get('files') or []), ensure_ascii=False)}\n"
        f"完整源码分析单元结果: {json.dumps(merged_summary, ensure_ascii=False)}"
    )
    return await _call_json(tool_caller, system_prompt=system_prompt, user_prompt=user_prompt)


async def analyze_plugin_with_llm(
    source_snapshot: dict[str, Any],
    plugin_name: str,
    tool_caller: ToolCaller,
) -> dict[str, Any]:
    analysis_mode, analysis_units = _build_analysis_units(source_snapshot)
    if not analysis_units:
        raise PluginAnalysisError(
            "source snapshot contains no source chunks",
            phase="snapshot",
        )

    batch_summaries: list[dict[str, Any]] = []
    degraded = False
    error_history: list[dict[str, Any]] = []
    for unit_index, unit in enumerate(analysis_units, start=1):
        unit_chunks = list(unit.get("chunks") or [])
        unit_label = str(unit.get("unit_label", "") or f"单元 {unit_index}").strip()
        unit_key = str(unit.get("unit_key", "") or unit_label).strip()
        try:
            batch_summary = await _analyze_source_unit_with_llm(
                plugin_name=plugin_name,
                unit_index=unit_index,
                total_units=len(analysis_units),
                unit_label=unit_label,
                unit_kind=str(unit.get("unit_kind", "") or analysis_mode),
                chunks=unit_chunks,
                tool_caller=tool_caller,
            )
        except Exception as exc:
            degraded = True
            error_history.append(
                {
                    "phase": "module_analysis" if analysis_mode == "module_bundle_multistage" else "batch_analysis",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                    "raw_preview": str(getattr(exc, "raw_preview", "") or "").strip(),
                    "failed_batch_index": unit_index,
                    "failed_batch_total": len(analysis_units),
                    "failed_unit_key": unit_key,
                }
            )
            batch_summary = _build_batch_fallback_summary(
                plugin_name=plugin_name,
                batch_index=unit_index,
                total_batches=len(analysis_units),
                chunks=unit_chunks,
            )
        batch_summaries.append(batch_summary)

    merged_summary = _merge_batch_summaries(batch_summaries)
    try:
        final_analysis = await _synthesize_plugin_analysis_with_llm(
            plugin_name=plugin_name,
            source_snapshot=source_snapshot,
            merged_summary=merged_summary,
            tool_caller=tool_caller,
        )
    except Exception as exc:
        degraded = True
        error_history.append(
            {
                "phase": "synthesis",
                "error_type": exc.__class__.__name__,
                "error_message": str(exc),
                "raw_preview": str(getattr(exc, "raw_preview", "") or "").strip(),
                "failed_batch_index": 0,
                "failed_batch_total": len(analysis_units),
            }
        )
        final_analysis = _build_fallback_plugin_analysis(
            plugin_name=plugin_name,
            source_snapshot=source_snapshot,
            merged_summary=merged_summary,
        )
    normalized = _normalize_final_analysis(
        final_analysis,
        plugin_name=plugin_name,
        source_snapshot=source_snapshot,
        analyzed_batch_count=len(analysis_units),
        analysis_mode=analysis_mode,
    )
    normalized["_analysis_meta"] = {
        "status": "degraded" if degraded else "success",
        "phase": error_history[-1]["phase"] if error_history else "complete",
        "error_type": error_history[-1]["error_type"] if error_history else "",
        "error_message": error_history[-1]["error_message"] if error_history else "",
        "raw_preview": error_history[-1]["raw_preview"] if error_history else "",
        "failed_batch_index": int(error_history[-1]["failed_batch_index"]) if error_history else 0,
        "failed_batch_total": int(error_history[-1]["failed_batch_total"]) if error_history else len(analysis_units),
        "analysis_mode": analysis_mode,
        "module_bundle_count": int(source_snapshot.get("module_bundle_count", 0) or 0),
        "recent_errors": error_history[-3:],
    }
    return normalized


__all__ = ["analyze_plugin_with_llm"]
