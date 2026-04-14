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
调用前先用 list_plugin_features 确认 feature_key 存在。"""

SEARCH_PLUGIN_KNOWLEDGE_DESCRIPTION = """根据用户的自然语言需求，搜索当前 bot 已安装插件及其功能。
适合场景：
- 用户只描述想做什么，不知道插件名
- 用户问 bot 自己有没有某个功能、能不能做某件事
- 用户问“怎么用/怎么触发/怎么配置某个功能”
- 用户提到功能词，例如漂流瓶、活动推送、签到、塔罗、提醒等，需要反查相关插件
调用后可直接据结果回答；若需要更细的插件级细节，再继续调用 list_plugin_features 或 get_feature_detail。"""


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
) -> tuple[dict | None, bool]:
    entry = knowledge_store.load_plugin_entry_sync(plugin_name)
    if isinstance(entry, dict):
        return entry, False
    fallback = _build_plugin_fallback_entry(plugin_name, knowledge_store)
    if isinstance(fallback, dict):
        return fallback, True
    return None, False


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
    scored_results: list[tuple[int, str, dict, dict | None, int]] = []

    for plugin_name, meta in plugins.items():
        if not isinstance(meta, dict):
            continue
        entry, _degraded = _load_plugin_entry_with_fallback(str(plugin_name), knowledge_store)
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
        scored_results.append((total_score, str(plugin_name), entry, best_feature, best_feature_score))

    scored_results.sort(key=lambda item: (-item[0], item[1]))
    if not scored_results:
        return f"本地插件知识库里没有找到和「{query}」明显相关的插件或功能。"

    lines = [f"插件知识库匹配结果（query={query}）："]
    for total_score, plugin_name, entry, best_feature, _feature_score in scored_results[: max(1, int(top_k))]:
        display_name = str(entry.get("display_name", "") or plugin_name)
        summary = str(entry.get("summary", "") or "暂无摘要")
        lines.append(f"- {plugin_name}[{display_name}]：{summary}")
        if best_feature is not None:
            title = str(best_feature.get("title", "") or best_feature.get("feature_key", "") or "未命名功能")
            feature_key = str(best_feature.get("feature_key", "") or "")
            feature_summary = str(best_feature.get("summary", "") or "暂无功能简介")
            lines.append(f"  相关功能：{feature_key} / {title} - {feature_summary}")
            if wants_usage:
                detail = str(best_feature.get("detail", "") or "").strip()
                if detail:
                    lines.append(f"  用法说明：{detail[:220]}")
        elif total_score > 0:
            lines.append("  相关功能：未命中具体功能，先参考该插件整体说明。")
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
    entry, degraded = _load_plugin_entry_with_fallback(actual_name, knowledge_store)
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
        lines.append("提示：当前详情文件缺失，以下内容来自索引或插件元数据。")
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
    return "\n".join(lines)


def get_feature_detail(
    plugin_name: str,
    feature_key: str,
    knowledge_store: PluginKnowledgeStore,
    include_runtime: bool = False,
) -> str:
    matched = knowledge_store.search_plugins(plugin_name, top_k=1)
    if not matched:
        return f"未找到插件：{plugin_name}"
    actual_name = matched[0]
    entry, degraded = _load_plugin_entry_with_fallback(actual_name, knowledge_store)
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
    config_items = target.get("config_items", [])
    lines = [f"{actual_name} / {feature_key} / {title}"]
    if degraded:
        lines.append("提示：当前详情文件缺失，以下内容来自索引或插件元数据。")
    if summary:
        lines.append(f"简介：{summary}")
    if config_items:
        lines.append("配置项：" + ", ".join(str(item) for item in config_items))
    lines.append(detail)

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
    ) -> str:
        store = _resolve_store(runtime)
        if store is None:
            return "插件知识库未初始化。"
        return get_feature_detail(plugin_name, feature_key, store, include_runtime=include_runtime)

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
                },
                "required": ["plugin_name", "feature_key"],
            },
            handler=_get_detail_handler,
        ),
    ]

