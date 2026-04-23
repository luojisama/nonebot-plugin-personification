from __future__ import annotations

import re
from typing import Any

try:
    from .....agent.tool_registry import AgentTool
    from .....core.knowledge_store import PluginKnowledgeStore
    from .....skill_runtime.runtime_api import SkillRuntime
except ImportError:  # pragma: no cover
    from nonebot_plugin_personification.agent.tool_registry import AgentTool  # type: ignore
    from nonebot_plugin_personification.core.knowledge_store import PluginKnowledgeStore  # type: ignore
    from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime  # type: ignore


LIST_PLUGINS_DESCRIPTION = """查询当前 bot 已安装的 NoneBot2 插件列表。
适合场景：
- 用户问有哪些插件、支持什么插件、装了哪些功能
- 用户问某类功能（如定时推送、天气查询、签到等）用哪个插件实现
- 用户问 bot 能不能做某件事，需要先确认本地是否有对应插件
- 不确定某插件是否已安装时，先调这个工具列出所有插件再判断
调用后，根据返回的插件列表和用户的需求，再决定是否调用 list_plugin_features 查具体功能。"""

LIST_FEATURES_DESCRIPTION = """查看某个已安装插件的功能列表和触发方式。
适合场景：
- 用户问某个插件有什么功能、支持什么命令、怎么触发
- 已通过 list_plugins 确认插件存在后，进一步了解其功能
- 用户问“XX 插件怎么用”
调用前建议先用 list_plugins 确认插件名，再传入准确的插件名。"""

FEATURE_DETAIL_DESCRIPTION = """查看某个插件某项功能的详细说明、配置项和使用示例。
适合场景：
- 用户问某个功能具体怎么配置、参数是什么
- 用户遇到配置问题需要看详细说明
- 需要了解某功能的依赖或注意事项
 - 需要顺带拿到实现文件和代码片段时
调用前先用 list_plugin_features 确认 feature_key 存在。"""

SEARCH_PLUGIN_KNOWLEDGE_DESCRIPTION = """根据用户的自然语言需求，搜索当前 bot 已安装插件及其功能。
适合场景：
- 用户只描述想做什么，不知道插件名
- 用户问 bot 自己有没有某个功能、能不能做某件事
- 用户问“怎么用/怎么触发/怎么配置某个功能”
- 用户追问插件实现方式、数据流、配置影响、技术细节，希望先定位到对应功能和实现映射
- 用户提到功能词，例如漂流瓶、活动推送、签到、塔罗、提醒等，需要反查相关插件
调用后应基于返回证据自行整合回答；若是技术细节问题，应继续结合 search_plugin_source 或 get_feature_detail 给出源码依据。"""

SEARCH_PLUGIN_SOURCE_DESCRIPTION = """根据插件实现代码片段查询当前插件的技术细节。
适合场景：
- 用户问某个插件的命令是怎么匹配、参数怎么解析、数据写到哪里、消息怎么发送
- 需要确认某功能到底由哪个文件/函数处理
- 需要源码依据，避免只凭摘要或记忆回答
调用时提供插件名和问题描述，工具会返回最相关的源码片段、文件路径和行号。
当前插件知识库已基于完整源码构建，适合回答实现方式和技术细节问题。"""

_IMPLEMENTATION_QUERY_HINTS = (
    "源码",
    "代码",
    "实现",
    "细节",
    "原理",
    "触发",
    "匹配",
    "解析",
    "handler",
    "matcher",
    "正则",
    "命令",
    "配置",
    "参数",
    "数据库",
    "存储",
    "读取",
    "写入",
    "发送",
    "调用",
    "接口",
    "hook",
)


def _resolve_store(runtime: SkillRuntime | None = None) -> PluginKnowledgeStore | None:
    store = getattr(runtime, "knowledge_store", None) if runtime is not None else None
    return store if isinstance(store, PluginKnowledgeStore) else None


def _normalize_query_text(text: str) -> str:
    normalized = str(text or "").strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _score_haystack_match(query: str, haystack: str) -> int:
    normalized_query = _normalize_query_text(query)
    normalized_haystack = _normalize_query_text(haystack)
    if not normalized_query or not normalized_haystack:
        return 0

    score = 0
    if normalized_query == normalized_haystack:
        score += 45
    elif normalized_query in normalized_haystack:
        score += 16

    compact_haystack = "".join(ch for ch in normalized_haystack if not ch.isspace())
    for token in PluginKnowledgeStore._to_search_tokens(normalized_query):
        if len(token) < 2:
            continue
        if token in normalized_haystack or token in compact_haystack:
            score += 4 if len(token) >= 4 else 1
    return score


def _looks_like_implementation_query(query: str) -> bool:
    normalized = _normalize_query_text(query)
    if not normalized:
        return False
    return any(token in normalized for token in _IMPLEMENTATION_QUERY_HINTS)


def _dedupe_texts(values: list[Any], *, limit: int = 16) -> list[str]:
    deduped: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in deduped:
            continue
        deduped.append(text)
        if len(deduped) >= limit:
            break
    return deduped


def _truncate_excerpt(text: str, max_chars: int = 520) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    clipped = normalized[:max_chars].rstrip()
    last_break = max(clipped.rfind("\n"), clipped.rfind(" "))
    if last_break >= max_chars // 2:
        clipped = clipped[:last_break].rstrip()
    return clipped + "\n# ..."


def _build_feature_source_query(feature_key: str, feature: dict[str, Any]) -> str:
    parts: list[str] = [str(feature_key or "")]
    for item in (
        feature.get("title", ""),
        feature.get("summary", ""),
        feature.get("detail", ""),
    ):
        text = str(item or "").strip()
        if text:
            parts.append(text)
    for item in list(feature.get("keywords") or [])[:8]:
        text = str(item or "").strip()
        if text:
            parts.append(text)
    for item in list(feature.get("config_items") or [])[:8]:
        text = str(item or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _entry_implementation_haystack(entry: dict[str, Any]) -> str:
    parts: list[str] = [
        str(entry.get("architecture_summary", "") or ""),
    ]
    for item in list(entry.get("entrypoints") or []):
        if not isinstance(item, dict):
            continue
        parts.extend(
            [
                str(item.get("kind", "") or ""),
                str(item.get("name", "") or ""),
                str(item.get("location", "") or ""),
                str(item.get("description", "") or ""),
            ]
        )
    for item in list(entry.get("implementation_map") or []):
        if not isinstance(item, dict):
            continue
        parts.extend(
            [
                str(item.get("feature_key", "") or ""),
                str(item.get("title", "") or ""),
                str(item.get("flow", "") or ""),
                str(item.get("notes", "") or ""),
                " ".join(str(value or "") for value in (item.get("files") or [])),
                " ".join(str(value or "") for value in (item.get("symbols") or [])),
            ]
        )
    for item in list(entry.get("data_access") or []):
        if not isinstance(item, dict):
            continue
        parts.extend(
            [
                str(item.get("kind", "") or ""),
                str(item.get("target", "") or ""),
                str(item.get("location", "") or ""),
                str(item.get("description", "") or ""),
            ]
        )
    return " ".join(part for part in parts if part).strip()


def _score_entry_item(query: str, item: dict[str, Any], fields: list[str]) -> int:
    haystack_parts: list[str] = []
    for field in fields:
        value = item.get(field, "")
        if isinstance(value, list):
            haystack_parts.append(" ".join(str(part or "") for part in value))
        else:
            haystack_parts.append(str(value or ""))
    return _score_haystack_match(query, " ".join(haystack_parts))


def _best_implementation_map_hits(entry: dict[str, Any], query: str, *, top_k: int = 2) -> list[dict[str, Any]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in list(entry.get("implementation_map") or []):
        if not isinstance(item, dict):
            continue
        score = _score_entry_item(query, item, ["feature_key", "title", "flow", "notes", "files", "symbols"])
        if score <= 0:
            continue
        scored.append((score, dict(item)))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("feature_key", "") or "")))
    return [item for _score, item in scored[: max(1, int(top_k or 2))]]


def _best_data_access_hits(entry: dict[str, Any], query: str, *, top_k: int = 2) -> list[dict[str, Any]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in list(entry.get("data_access") or []):
        if not isinstance(item, dict):
            continue
        score = _score_entry_item(query, item, ["kind", "target", "location", "description"])
        if score <= 0:
            continue
        scored.append((score, dict(item)))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("target", "") or "")))
    return [item for _score, item in scored[: max(1, int(top_k or 2))]]


def _search_source_hits(
    plugin_name: str,
    query: str,
    knowledge_store: PluginKnowledgeStore,
    *,
    top_k: int = 3,
) -> tuple[str | None, list[dict[str, Any]]]:
    matched = knowledge_store.search_plugins(plugin_name, top_k=1)
    if not matched:
        return None, []
    actual_name = matched[0]
    snapshot = knowledge_store.load_source_snapshot_sync(actual_name)
    if not isinstance(snapshot, dict):
        return actual_name, []

    chunks = snapshot.get("chunks", [])
    if not isinstance(chunks, list) or not chunks:
        return actual_name, []

    scored: list[tuple[int, dict[str, Any]]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text", "") or "")
        if not text:
            continue
        haystack = " ".join(
            [
                str(chunk.get("file", "") or ""),
                str(chunk.get("preview", "") or ""),
                " ".join(str(item or "") for item in (chunk.get("symbols") or [])),
                text,
            ]
        )
        score = _score_haystack_match(query, haystack)
        if score <= 0:
            continue
        file_name = str(chunk.get("file", "") or "").lower()
        for token in PluginKnowledgeStore._to_search_tokens(_normalize_query_text(query)):
            if token and token in file_name:
                score += 3
        scored.append((score, chunk))

    scored.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("file", "") or ""),
            int(item[1].get("start_line", 0) or 0),
        )
    )
    return actual_name, [dict(chunk) for _score, chunk in scored[: max(1, int(top_k))]]


def _append_source_hits(lines: list[str], hits: list[dict[str, Any]], *, heading: str) -> None:
    if not hits:
        return
    lines.append(heading)
    for item in hits:
        file_path = str(item.get("file", "") or "unknown.py")
        start_line = int(item.get("start_line", 1) or 1)
        end_line = int(item.get("end_line", start_line) or start_line)
        excerpt = _truncate_excerpt(str(item.get("text", "") or ""))
        lines.append(f"- {file_path}:{start_line}-{end_line}")
        if excerpt:
            lines.append(excerpt)


def _find_loaded_plugin_metadata(plugin_name: str) -> Any | None:
    normalized = _normalize_query_text(plugin_name)
    if not normalized:
        return None
    try:
        import nonebot

        loaded_plugins = list(nonebot.get_loaded_plugins() or [])
    except Exception:
        return None

    for plugin in loaded_plugins:
        module = getattr(plugin, "module", None)
        module_name = str(getattr(module, "__name__", "") or "").strip().lower()
        runtime_name = str(getattr(plugin, "name", "") or "").strip().lower()
        candidates = {runtime_name, module_name, module_name.split(".")[-1] if module_name else ""}
        if normalized not in candidates:
            continue
        metadata = getattr(plugin, "metadata", None)
        if metadata is None and module is not None:
            metadata = getattr(module, "__plugin_meta__", None)
        if metadata is not None:
            return metadata
    return None


def _build_usage_feature(usage_text: str) -> dict | None:
    usage = str(usage_text or "").strip()
    if not usage:
        return None

    normalized = usage.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw_line in normalized.split("\n"):
        line = re.sub(r"^[\-\*\u2022]\s*", "", str(raw_line or "").strip())
        if line:
            lines.append(line)
    if not lines:
        return None

    summary = lines[0]
    return {
        "title": "使用方法",
        "summary": summary,
        "detail": "\n".join(lines),
        "keywords": ["使用", "用法", "命令", "指令", "触发", "配置"],
        "config_items": [],
    }


def _build_plugin_fallback_entry(
    plugin_name: str,
    knowledge_store: PluginKnowledgeStore,
) -> dict | None:
    index = knowledge_store.load_index_sync()
    plugins = index.get("plugins", {})
    meta = plugins.get(plugin_name) if isinstance(plugins, dict) else None
    if not isinstance(meta, dict):
        return None

    entry = {
        "display_name": str(meta.get("display_name", "") or plugin_name),
        "summary": str(meta.get("summary", "") or ""),
        "keywords": list(meta.get("keywords") or []),
        "features": {},
    }

    metadata = _find_loaded_plugin_metadata(plugin_name)
    if metadata is None:
        return entry

    display_name = str(getattr(metadata, "name", "") or "").strip()
    description = str(getattr(metadata, "description", "") or "").strip()
    if display_name:
        entry["display_name"] = display_name
    if description:
        entry["summary"] = description

    usage_feature = _build_usage_feature(str(getattr(metadata, "usage", "") or ""))
    if usage_feature is not None:
        entry["features"] = {"usage": usage_feature}
    return entry


def _load_plugin_entry_with_fallback(
    plugin_name: str,
    knowledge_store: PluginKnowledgeStore,
) -> tuple[dict | None, bool, dict[str, Any]]:
    build_state = knowledge_store.load_build_state_sync()
    state_plugins = build_state.get("plugins", {}) if isinstance(build_state, dict) else {}
    state_meta = state_plugins.get(plugin_name, {}) if isinstance(state_plugins, dict) else {}
    degraded_status = str(state_meta.get("status", "") or "").strip().lower() in {"failed", "degraded"}
    entry = knowledge_store.load_plugin_entry_sync(plugin_name)
    if isinstance(entry, dict):
        return entry, degraded_status, state_meta if isinstance(state_meta, dict) else {}
    fallback = _build_plugin_fallback_entry(plugin_name, knowledge_store)
    if isinstance(fallback, dict):
        return fallback, True, state_meta if isinstance(state_meta, dict) else {}
    return None, degraded_status, state_meta if isinstance(state_meta, dict) else {}


def search_plugin_knowledge(
    query: str,
    knowledge_store: PluginKnowledgeStore,
    top_k: int = 3,
) -> str:
    normalized_query = _normalize_query_text(query)
    if not normalized_query:
        return "插件知识库查询为空。"

    index = knowledge_store.load_index_sync()
    plugins = index.get("plugins", {})
    if not isinstance(plugins, dict) or not plugins:
        return "插件知识库暂无可搜索数据。"

    wants_usage = any(
        token in normalized_query
        for token in ("怎么", "如何", "用法", "使用", "命令", "指令", "触发", "配置", "设置")
    )
    wants_implementation = _looks_like_implementation_query(normalized_query)
    scored_results: list[tuple[int, str, dict, dict | None, int]] = []

    for plugin_name, meta in plugins.items():
        if not isinstance(meta, dict):
            continue
        entry, entry_degraded, state_meta = _load_plugin_entry_with_fallback(str(plugin_name), knowledge_store)
        entry = entry or {}
        display_name = str(entry.get("display_name", "") or meta.get("display_name", "") or plugin_name)
        summary = str(entry.get("summary", "") or meta.get("summary", "") or "")
        plugin_keywords = [str(item or "") for item in (entry.get("keywords") or meta.get("keywords") or [])]
        plugin_haystack = " ".join(
            [
                str(plugin_name or ""),
                display_name,
                summary,
                " ".join(plugin_keywords),
                _entry_implementation_haystack(entry),
            ]
        )
        plugin_score = _score_haystack_match(normalized_query, plugin_haystack)

        triggers = entry.get("triggers", [])
        if isinstance(triggers, list):
            trigger_haystack = " ".join(
                str(item.get("pattern", "") or "")
                for item in triggers
                if isinstance(item, dict)
            )
            plugin_score += _score_haystack_match(normalized_query, trigger_haystack)

        best_feature: dict | None = None
        best_feature_score = 0
        features = entry.get("features", {})
        if isinstance(features, dict):
            for feature_key, feature in features.items():
                if not isinstance(feature, dict):
                    continue
                title = str(feature.get("title", "") or feature_key)
                feature_haystack = " ".join(
                    [
                        str(feature_key or ""),
                        title,
                        str(feature.get("summary", "") or ""),
                        str(feature.get("detail", "") or ""),
                        " ".join(str(item or "") for item in (feature.get("keywords") or [])),
                        " ".join(str(item or "") for item in (feature.get("config_items") or [])),
                    ]
                )
                feature_score = _score_haystack_match(normalized_query, feature_haystack)
                if wants_usage and feature_score > 0:
                    feature_score += 6
                if feature_score > best_feature_score:
                    best_feature = {
                        "feature_key": str(feature_key),
                        "title": title,
                        "summary": str(feature.get("summary", "") or ""),
                        "detail": str(feature.get("detail", "") or ""),
                    }
                    best_feature_score = feature_score

        total_score = plugin_score + best_feature_score
        if total_score <= 0:
            continue
        if entry_degraded:
            total_score += 1
        scored_results.append(
            (
                total_score,
                str(plugin_name),
                {**entry, "_build_state": state_meta if isinstance(state_meta, dict) else {}},
                best_feature,
                best_feature_score,
            )
        )

    scored_results.sort(key=lambda item: (-item[0], item[1]))
    if not scored_results:
        return f"本地插件知识库里没有找到和「{query}」明显相关的插件或功能。"

    lines = [f"插件知识证据召回（query={query}）：", "请基于以下证据自行组织最终回答，不要逐行复述。"]
    for total_score, plugin_name, entry, best_feature, _feature_score in scored_results[: max(1, int(top_k))]:
        display_name = str(entry.get("display_name", "") or plugin_name)
        summary = str(entry.get("summary", "") or "暂无摘要")
        lines.append(f"- 插件：{plugin_name}[{display_name}]")
        lines.append(f"  摘要：{summary}")
        build_state_meta = entry.get("_build_state", {}) if isinstance(entry, dict) else {}
        build_status = str(build_state_meta.get("status", "") or "").strip().lower()
        if build_status in {"failed", "degraded"}:
            phase = str(build_state_meta.get("phase", "") or "unknown").strip()
            lines.append(f"  构建状态：{build_status}（{phase}），摘要分析未完成，当前应优先基于源码与索引证据回答。")
        if best_feature is not None:
            title = str(best_feature.get("title", "") or best_feature.get("feature_key", "") or "未命名功能")
            feature_key = str(best_feature.get("feature_key", "") or "")
            feature_summary = str(best_feature.get("summary", "") or "暂无功能简介")
            lines.append(f"  命中功能：{feature_key} / {title}")
            lines.append(f"  功能摘要：{feature_summary}")
            if wants_usage:
                detail = str(best_feature.get("detail", "") or "").strip()
                if detail:
                    lines.append(f"  用法证据：{detail[:220]}")
        elif total_score > 0:
            lines.append("  命中功能：未命中具体功能，先参考该插件整体说明。")
        if wants_implementation:
            architecture_summary = str(entry.get("architecture_summary", "") or "").strip()
            implementation_hits = _best_implementation_map_hits(entry, normalized_query, top_k=2)
            data_access_hits = _best_data_access_hits(entry, normalized_query, top_k=2)
            if architecture_summary:
                lines.append(f"  实现概览证据：{architecture_summary[:220]}")
            for hit in implementation_hits:
                title = str(hit.get("title", "") or hit.get("feature_key", "") or "未命名功能")
                flow = str(hit.get("flow", "") or hit.get("notes", "") or "").strip()
                files = ", ".join(_dedupe_texts(list(hit.get("files") or []), limit=4))
                symbols = ", ".join(_dedupe_texts(list(hit.get("symbols") or []), limit=4))
                detail_parts = [title]
                if flow:
                    detail_parts.append(flow[:220])
                if files:
                    detail_parts.append(f"文件: {files}")
                if symbols:
                    detail_parts.append(f"符号: {symbols}")
                lines.append(f"  关键实现证据：{'；'.join(detail_parts)}")
            for hit in data_access_hits:
                kind = str(hit.get("kind", "") or "unknown")
                target = str(hit.get("target", "") or "未标注目标")
                location = str(hit.get("location", "") or "")
                description = str(hit.get("description", "") or "").strip()
                detail = f"{kind} -> {target}"
                if location:
                    detail += f" @ {location}"
                if description:
                    detail += f"：{description[:180]}"
                lines.append(f"  数据访问证据：{detail}")
            actual_name, source_hits = _search_source_hits(plugin_name, query, knowledge_store, top_k=1)
            if actual_name and source_hits:
                top_hit = source_hits[0]
                file_path = str(top_hit.get("file", "") or "unknown.py")
                start_line = int(top_hit.get("start_line", 1) or 1)
                end_line = int(top_hit.get("end_line", start_line) or start_line)
                lines.append(f"  源码命中：{file_path}:{start_line}-{end_line}")
    return "\n".join(lines)


def list_plugins(knowledge_store: PluginKnowledgeStore) -> str:
    index = knowledge_store.load_index_sync()
    plugins = index.get("plugins", {})
    if not isinstance(plugins, dict) or not plugins:
        return "已知插件列表（共0个）：\n暂无数据"

    local_lines: list[str] = []
    store_lines: list[str] = []
    for plugin_name, meta in sorted(plugins.items()):
        if not isinstance(meta, dict):
            continue
        display_name = str(meta.get("display_name", "") or plugin_name)
        summary = str(meta.get("summary", "") or "暂无摘要")
        line = f"- {plugin_name}[{display_name}]: {summary}"
        if str(meta.get("category", "local")) == "store":
            store_lines.append(line)
        else:
            local_lines.append(line)

    lines = [f"已知插件列表（共{len(plugins)}个）：", "本地插件："]
    lines.extend(local_lines or ["- 暂无"])
    lines.append("商店插件：")
    lines.extend(store_lines or ["- 暂无"])
    return "\n".join(lines)


def list_plugin_features(
    plugin_name: str,
    knowledge_store: PluginKnowledgeStore,
) -> str:
    matched = knowledge_store.search_plugins(plugin_name, top_k=1)
    if not matched:
        return f"未找到插件：{plugin_name}"
    actual_name = matched[0]
    entry, degraded, state_meta = _load_plugin_entry_with_fallback(actual_name, knowledge_store)
    if not isinstance(entry, dict):
        return f"未找到插件详情：{actual_name}"

    features = entry.get("features", {})
    if not isinstance(features, dict) or not features:
        if degraded:
            return f"{actual_name} 暂无功能索引（当前仅拿到索引或插件元数据，可尝试重建插件知识库）。"
        return f"{actual_name} 暂无功能索引。"

    display_name = str(entry.get("display_name", "") or actual_name)
    lines = [f"{actual_name}[{display_name}] 功能列表："]
    if degraded:
        status = str(state_meta.get("status", "") or "degraded").strip().lower()
        lines.append(
            "提示：当前插件摘要分析未完成，以下内容基于"
            + ("源码直接检索与索引。" if status in {"failed", "degraded"} else "索引或插件元数据。")
        )
    for feature_key, feature in features.items():
        if not isinstance(feature, dict):
            continue
        title = str(feature.get("title", "") or feature_key)
        summary = str(feature.get("summary", "") or "暂无简介")
        lines.append(f"- {feature_key}: {title} - {summary}")

    index = knowledge_store.load_index_sync()
    meta = (index.get("plugins", {}) or {}).get(actual_name, {})
    if isinstance(meta, dict) and meta.get("has_runtime_data"):
        lines.append("该插件有运行时数据可查询")
    if isinstance(meta, dict) and meta.get("has_source_data"):
        lines.append(
            f"该插件已索引源码（文件 {int(meta.get('source_file_count', 0) or 0)} 个，"
            f"代码块 {int(meta.get('source_chunk_count', 0) or 0)} 段）"
        )
    return "\n".join(lines)


def search_plugin_source(
    plugin_name: str,
    query: str,
    knowledge_store: PluginKnowledgeStore,
    top_k: int = 3,
) -> str:
    actual_name, source_hits = _search_source_hits(
        plugin_name,
        query,
        knowledge_store,
        top_k=max(1, min(int(top_k or 3), 5)),
    )
    if not actual_name:
        return f"未找到插件：{plugin_name}"
    snapshot = knowledge_store.load_source_snapshot_sync(actual_name)
    if not isinstance(snapshot, dict):
        return f"{actual_name} 暂无源码索引，可尝试重建插件知识库。"
    if not source_hits:
        return f"{actual_name} 的源码索引里没有找到和「{query}」明显相关的实现片段。"

    lines = [f"{actual_name} 源码命中（query={query}）："]
    build_state = knowledge_store.load_build_state_sync()
    state_plugins = build_state.get("plugins", {}) if isinstance(build_state, dict) else {}
    state_meta = state_plugins.get(actual_name, {}) if isinstance(state_plugins, dict) else {}
    if isinstance(state_meta, dict):
        status = str(state_meta.get("status", "") or "").strip().lower()
        if status in {"failed", "degraded"}:
            lines.append(
                f"提示：该插件的摘要分析状态为 {status}，当前结果直接基于源码索引。"
            )
    _append_source_hits(lines, source_hits, heading="实现片段：")
    return "\n".join(lines)


def get_feature_detail(
    plugin_name: str,
    feature_key: str,
    knowledge_store: PluginKnowledgeStore,
    include_runtime: bool = False,
    include_source: bool = True,
) -> str:
    matched = knowledge_store.search_plugins(plugin_name, top_k=1)
    if not matched:
        return f"未找到插件：{plugin_name}"
    actual_name = matched[0]
    entry, degraded, state_meta = _load_plugin_entry_with_fallback(actual_name, knowledge_store)
    if not isinstance(entry, dict):
        return f"未找到插件详情：{actual_name}"

    features = entry.get("features", {})
    if not isinstance(features, dict):
        features = {}
    target = features.get(feature_key)
    if not isinstance(target, dict):
        all_keys = ", ".join(sorted(features.keys())) if features else "无"
        return f"未找到功能 {feature_key}。可用 feature_key：{all_keys}"

    title = str(target.get("title", "") or feature_key)
    summary = str(target.get("summary", "") or "")
    detail = str(target.get("detail", "") or "暂无详细说明")
    implementation = str(target.get("implementation", "") or "").strip()
    files = _dedupe_texts(list(target.get("files") or []), limit=8)
    symbols = _dedupe_texts(list(target.get("symbols") or []), limit=8)
    config_items = target.get("config_items", [])
    lines = [f"{actual_name} / {feature_key} / {title}"]
    if degraded:
        status = str(state_meta.get("status", "") or "degraded").strip().lower()
        lines.append(
            "提示：当前摘要分析未完成，以下内容基于"
            + ("源码直接检索与索引。" if status in {"failed", "degraded"} else "索引或插件元数据。")
        )
    if summary:
        lines.append(f"简介：{summary}")
    if config_items:
        lines.append("配置项：" + ", ".join(str(item) for item in config_items))
    if implementation:
        lines.append(f"实现方式：{implementation}")
    if files:
        lines.append("关键文件：" + ", ".join(files))
    if symbols:
        lines.append("关键符号：" + ", ".join(symbols))
    lines.append(detail)

    implementation_map_hits = _best_implementation_map_hits(
        entry,
        f"{feature_key} {title} {summary} {implementation}",
        top_k=2,
    )
    if implementation_map_hits:
        lines.append("实现映射：")
        for item in implementation_map_hits:
            flow = str(item.get("flow", "") or item.get("notes", "") or "").strip()
            item_files = ", ".join(_dedupe_texts(list(item.get("files") or []), limit=4))
            item_symbols = ", ".join(_dedupe_texts(list(item.get("symbols") or []), limit=4))
            detail_parts = []
            if flow:
                detail_parts.append(flow[:220])
            if item_files:
                detail_parts.append(f"文件: {item_files}")
            if item_symbols:
                detail_parts.append(f"符号: {item_symbols}")
            if detail_parts:
                lines.append("- " + "；".join(detail_parts))

    if include_source:
        source_query = _build_feature_source_query(feature_key, target)
        _actual_name, source_hits = _search_source_hits(actual_name, source_query, knowledge_store, top_k=2)
        _append_source_hits(lines, source_hits, heading="实现参考：")

    if include_runtime:
        runtime_snapshot = knowledge_store.load_runtime_snapshot_sync(actual_name)
        if runtime_snapshot:
            files = runtime_snapshot.get("files", [])
            notable = runtime_snapshot.get("notable_files", {})
            lines.append("运行时数据：")
            lines.append(f"- 文件数：{len(files) if isinstance(files, list) else 0}")
            if isinstance(notable, dict) and notable:
                for filename, meta in list(notable.items())[:5]:
                    if not isinstance(meta, dict):
                        continue
                    preview = str(meta.get("preview", "") or "")
                    lines.append(f"- {filename}: {preview[:160]}")
            else:
                lines.append("- 无可展示快照")
    return "\n".join(lines)


def build_plugin_knowledge_tools(runtime: SkillRuntime) -> list[AgentTool]:
    async def _search_plugin_knowledge_handler(query: str, top_k: int = 3) -> str:
        store = _resolve_store(runtime)
        if store is None:
            return "插件知识库未初始化。"
        return search_plugin_knowledge(query, store, top_k=max(1, min(int(top_k or 3), 5)))

    async def _search_plugin_source_handler(
        plugin_name: str,
        query: str,
        top_k: int = 3,
    ) -> str:
        store = _resolve_store(runtime)
        if store is None:
            return "插件知识库未初始化。"
        return search_plugin_source(plugin_name, query, store, top_k=max(1, min(int(top_k or 3), 5)))

    async def _list_plugins_handler() -> str:
        store = _resolve_store(runtime)
        if store is None:
            return "插件知识库未初始化。"
        return list_plugins(store)

    async def _list_features_handler(plugin_name: str) -> str:
        store = _resolve_store(runtime)
        if store is None:
            return "插件知识库未初始化。"
        return list_plugin_features(plugin_name, store)

    async def _get_detail_handler(
        plugin_name: str,
        feature_key: str,
        include_runtime: bool = False,
        include_source: bool = True,
    ) -> str:
        store = _resolve_store(runtime)
        if store is None:
            return "插件知识库未初始化。"
        return get_feature_detail(
            plugin_name,
            feature_key,
            store,
            include_runtime=include_runtime,
            include_source=include_source,
        )

    return [
        AgentTool(
            name="search_plugin_knowledge",
            description=SEARCH_PLUGIN_KNOWLEDGE_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户描述的功能需求或问题"},
                    "top_k": {"type": "integer", "description": "返回候选插件数量，默认 3"},
                },
                "required": ["query"],
            },
            handler=_search_plugin_knowledge_handler,
        ),
        AgentTool(
            name="list_plugins",
            description=LIST_PLUGINS_DESCRIPTION,
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_list_plugins_handler,
        ),
        AgentTool(
            name="search_plugin_source",
            description=SEARCH_PLUGIN_SOURCE_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件名或模糊关键词"},
                    "query": {"type": "string", "description": "要确认的实现问题或源码细节"},
                    "top_k": {"type": "integer", "description": "返回的源码片段数量，默认 3"},
                },
                "required": ["plugin_name", "query"],
            },
            handler=_search_plugin_source_handler,
        ),
        AgentTool(
            name="list_plugin_features",
            description=LIST_FEATURES_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件名或模糊关键词"}
                },
                "required": ["plugin_name"],
            },
            handler=_list_features_handler,
        ),
        AgentTool(
            name="get_feature_detail",
            description=FEATURE_DETAIL_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件名或模糊关键词"},
                    "feature_key": {"type": "string", "description": "功能键名"},
                    "include_runtime": {"type": "boolean", "description": "是否附加运行时数据"},
                    "include_source": {"type": "boolean", "description": "是否附加实现代码片段"},
                },
                "required": ["plugin_name", "feature_key"],
            },
            handler=_get_detail_handler,
        ),
    ]

