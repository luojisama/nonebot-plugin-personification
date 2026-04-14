import json
import re
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from ..core.remote_skill_review import (
    get_remote_skill_review_stats,
    list_remote_skill_reviews,
    review_remote_skill_sources,
)

from ..core.data_store import get_data_store
from ..core.image_result_cache import clear_image_result_cache
from ..core.session_store import clear_all_session_histories
from ..skill_runtime.source_resolver import parse_skill_sources
import asyncio


def _entry_dedupe_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("source") or "").strip(),
        str(entry.get("ref") or "").strip(),
        str(entry.get("subdir") or "").strip(),
    )


def _find_review_item_key(
    raw_sources: Any,
    logger: Any,
    entry: dict[str, Any],
) -> str:
    target_key = _entry_dedupe_key(entry)
    for item in list_remote_skill_reviews(raw_sources, logger):
        if not isinstance(item, dict):
            continue
        item_key = (
            str(item.get("source") or "").strip(),
            str(item.get("ref") or "").strip(),
            str(item.get("subdir") or "").strip(),
        )
        if item_key == target_key:
            return str(item.get("key") or "").strip()
    return ""


def install_remote_skill_source(
    *,
    entry: dict[str, Any],
    plugin_config: Any,
    save_plugin_runtime_config: Callable[[], None],
    logger: Any,
    operator_user_id: str,
    prefer_first: bool = False,
    auto_approve: bool = False,
    install_note: str = "",
) -> tuple[bool, str]:
    current_sources = parse_skill_sources(
        getattr(plugin_config, "personification_skill_sources", None),
        logger,
    )
    dedupe_key = _entry_dedupe_key(entry)
    existing_index = -1
    for index, item in enumerate(current_sources):
        if _entry_dedupe_key(item) == dedupe_key:
            existing_index = index
            break

    changed = False
    if existing_index >= 0:
        existing_entry = dict(current_sources[existing_index])
        if prefer_first and existing_index != 0:
            current_sources.pop(existing_index)
            current_sources.insert(0, existing_entry)
            changed = True
        entry_for_reply = existing_entry
        exists_text = "该远程 skill 源已存在。"
        if changed:
            exists_text += " 已调整为优先加载源。"
    else:
        if prefer_first:
            current_sources.insert(0, entry)
        else:
            current_sources.append(entry)
        entry_for_reply = entry
        changed = True
        exists_text = "已登记远程 skill 源。"

    plugin_config.personification_skill_sources = current_sources
    plugin_config.personification_skill_remote_enabled = True
    plugin_config.personification_skill_allow_unsafe_external = True
    plugin_config.personification_skill_require_admin_review = True
    if changed:
        save_plugin_runtime_config()

    approval_text = (
        "仍需执行 `远程技能审批 同意 ...`，并在下次重载或重启后才会实际加载。"
    )
    if auto_approve:
        review_key = _find_review_item_key(
            getattr(plugin_config, "personification_skill_sources", None),
            logger,
            entry_for_reply,
        )
        if review_key:
            matched_count, _matched_items = review_remote_skill_sources(
                getattr(plugin_config, "personification_skill_sources", None),
                logger,
                selector=review_key,
                status="approved",
                operator=operator_user_id,
            )
            if matched_count > 0:
                approval_text = "已自动审批通过，并会在下次重载插件或重启后实际加载。"
            else:
                approval_text = "自动审批未命中，请执行 `远程技能审批 同意 pending` 后再重载。"
        else:
            approval_text = "自动审批未找到对应记录，请执行 `远程技能审批 同意 pending` 后再重载。"

    logger.info(
        f"[remote_skill_install] operator={operator_user_id} "
        f"source={entry_for_reply.get('source')} ref={entry_for_reply.get('ref', '')} "
        f"subdir={entry_for_reply.get('subdir', '')} prefer_first={prefer_first} auto_approve={auto_approve}"
    )
    note = str(install_note or "").strip()
    if note:
        note = f"\n说明: {note}"
    return True, (
        f"{exists_text}\n"
        f"名称: {entry_for_reply.get('name')}\n"
        f"来源: {entry_for_reply.get('source')}\n"
        f"分支/标签: {entry_for_reply.get('ref', '默认')}\n"
        f"子目录: {entry_for_reply.get('subdir', '根目录')}\n"
        f"优先级: {'优先加载' if prefer_first else '普通'}\n"
        f"{approval_text}{note}"
    )


async def handle_global_switch_command(
    matcher: Any,
    *,
    action: str,
    plugin_config: Any,
    apply_global_switch: Callable[[str, Any], tuple[bool, str]],
    save_plugin_runtime_config: Callable[[], None],
) -> None:
    changed, msg = apply_global_switch(action, plugin_config)
    if changed:
        save_plugin_runtime_config()
    await matcher.finish(msg)


async def handle_tts_global_switch_command(
    matcher: Any,
    *,
    action: str,
    plugin_config: Any,
    apply_tts_global_switch: Callable[[str, Any], tuple[bool, str]],
    save_plugin_runtime_config: Callable[[], None],
) -> None:
    changed, msg = apply_tts_global_switch(action, plugin_config)
    if changed:
        save_plugin_runtime_config()
    await matcher.finish(msg)


async def handle_web_search_switch_command(
    matcher: Any,
    *,
    action: str,
    plugin_config: Any,
    apply_web_search_switch: Callable[[str, Any], tuple[bool, str]],
    save_plugin_runtime_config: Callable[[], None],
) -> None:
    changed, msg = apply_web_search_switch(action, plugin_config)
    if changed:
        save_plugin_runtime_config()
    await matcher.finish(msg)


async def handle_proactive_switch_command(
    matcher: Any,
    *,
    action: str,
    plugin_config: Any,
    apply_proactive_switch: Callable[[str, Any], tuple[bool, str]],
    save_plugin_runtime_config: Callable[[], None],
) -> None:
    changed, msg = apply_proactive_switch(action, plugin_config)
    if changed:
        save_plugin_runtime_config()
    await matcher.finish(msg)


async def handle_personification_help_command(
    matcher: Any,
    *,
    build_plugin_usage_text: Callable[[], str],
) -> None:
    await matcher.finish(build_plugin_usage_text())


def _extract_github_source_name(url: str) -> str:
    parts = [part for part in urlparse(url).path.strip("/").split("/") if part]
    if len(parts) >= 2:
        return f"github_{parts[0]}_{parts[1]}".replace("-", "_")
    return "github_skill"


def _extract_skill_page_source_name(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    slug = parts[-1] if parts else "skill"
    prefix = "clawhub"
    if "skillhub.tencent.com" in host or "skillhub.cn" in host:
        prefix = "tencent_skillhub"
    return f"{prefix}_{slug}".replace("-", "_")


def _parse_remote_skill_install_args(arg_text: str) -> tuple[dict[str, Any] | None, str | None]:
    text = str(arg_text or "").strip()
    if not text:
        return None, "用法: 安装远程技能 <GitHub/ClawHub/SkillHub地址> [ref=分支] [subdir=目录] [name=名称]"

    parts = [part for part in text.split() if part]
    url = parts[0].strip()
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return None, "远程 skill 地址必须是 http/https URL"

    is_github = "github.com" in host
    is_skill_page = "clawhub.ai" in host or "skillhub.tencent.com" in host or "skillhub.cn" in host
    if not is_github and not is_skill_page:
        return None, "目前支持 GitHub 仓库地址、ClawHub 技能页地址或 SkillHub 技能页地址"

    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if is_github and len(path_parts) < 2:
        return None, "GitHub 地址格式不正确，应至少包含 owner/repo"
    if is_skill_page and not path_parts:
        return None, "SkillHub/ClawHub 技能页地址格式不正确"

    if is_github:
        entry: dict[str, Any] = {
            "name": _extract_github_source_name(url),
            "source": f"https://github.com/{path_parts[0]}/{path_parts[1]}",
            "enabled": True,
        }

        if len(path_parts) >= 4 and path_parts[2] == "tree":
            entry["ref"] = path_parts[3]
            if len(path_parts) > 4:
                entry["subdir"] = "/".join(path_parts[4:])
    else:
        entry = {
            "name": _extract_skill_page_source_name(url),
            "source": url,
            "enabled": True,
        }

    for token in parts[1:]:
        if "=" not in token:
            return None, f"无法识别参数: {token}"
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not value:
            return None, f"参数 {key} 不能为空"
        if key == "ref":
            entry["ref"] = value
        elif key == "subdir":
            entry["subdir"] = value
        elif key == "name":
            entry["name"] = value
        else:
            return None, f"不支持的参数: {key}"
    return entry, None


async def handle_install_remote_skill_command(
    matcher: Any,
    *,
    arg_text: str,
    plugin_config: Any,
    save_plugin_runtime_config: Callable[[], None],
    logger: Any,
    operator_user_id: str,
) -> None:
    entry, error = _parse_remote_skill_install_args(arg_text)
    if error:
        await matcher.finish(error)
    if entry is None:
        await matcher.finish("远程 skill 源参数无效。")
    _changed, message = install_remote_skill_source(
        entry=entry,
        plugin_config=plugin_config,
        save_plugin_runtime_config=save_plugin_runtime_config,
        logger=logger,
        operator_user_id=operator_user_id,
    )
    await matcher.finish(message)


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    fenced = raw.strip("`").strip()
    match = re.search(r"\{[\s\S]*\}", fenced)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_source_hint(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"skillhub", "tencent", "tencent_skillhub", "腾讯", "腾讯skillhub"}:
        return "skillhub"
    if lowered in {"clawhub", "claw"}:
        return "clawhub"
    if lowered in {"github", "git"}:
        return "github"
    return "auto"


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    lowered = str(value or "").strip().lower()
    if lowered in {"true", "1", "yes", "y", "是", "开启", "需要"}:
        return True
    if lowered in {"false", "0", "no", "n", "否", "关闭", "不要"}:
        return False
    return default


async def _parse_natural_language_install_request(
    *,
    text: str,
    tool_caller: Any,
) -> dict[str, Any]:
    if tool_caller is None:
        return {}
    prompt = (
        "你是远程技能安装请求解析器。"
        "请判断用户是否在要求安装远程 skill。"
        "只返回 JSON，不要解释。"
        'JSON 格式: {"action":"INSTALL_REMOTE_SKILL|NO_ACTION","skill_name":"","source_url":"","source_hint":"skillhub|clawhub|github|auto","prefer_source":false,"name":"","ref":"","subdir":"","auto_approve":true}'
        "如果用户只是普通聊天、问答、抱怨、评测，action=NO_ACTION。"
        "如果用户要求从 SkillHub/ClawHub/GitHub 安装某个 skill，但没给 URL，要尽量提取 skill_name 和 source_hint，不要编造 URL。"
        "如果用户说设为优先源，prefer_source=true。"
        "如果用户以管理员身份要求直接安装，auto_approve=true。"
    )
    try:
        response = await tool_caller.chat_with_tools(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": str(text or "").strip()},
            ],
            tools=[],
            use_builtin_search=False,
        )
    except Exception:
        return {}
    parsed = _extract_json_object(getattr(response, "content", "") or "")
    if not parsed:
        return {}
    parsed["action"] = str(parsed.get("action") or "").strip().upper()
    parsed["source_hint"] = _normalize_source_hint(str(parsed.get("source_hint") or "auto"))
    parsed["skill_name"] = str(parsed.get("skill_name") or "").strip()
    parsed["source_url"] = str(parsed.get("source_url") or "").strip()
    parsed["name"] = str(parsed.get("name") or "").strip()
    parsed["ref"] = str(parsed.get("ref") or "").strip()
    parsed["subdir"] = str(parsed.get("subdir") or "").strip()
    parsed["prefer_source"] = _coerce_bool(parsed.get("prefer_source"), False)
    parsed["auto_approve"] = _coerce_bool(parsed.get("auto_approve"), True)
    return parsed


async def _search_remote_skill_candidates(
    *,
    registry: Any,
    skill_name: str,
    source_hint: str,
) -> list[dict[str, Any]]:
    tool = registry.get("search_web") if registry is not None else None
    if tool is None:
        return []

    queries: list[str] = []
    if source_hint == "skillhub":
        queries = [
            f"site:skillhub.tencent.com {skill_name} skill",
            f"site:skillhub.cn {skill_name} skill",
        ]
    elif source_hint == "clawhub":
        queries = [f"site:clawhub.ai/skills {skill_name} skill"]
    elif source_hint == "github":
        queries = [f"site:github.com {skill_name} skill"]
    else:
        queries = [
            f"site:skillhub.tencent.com {skill_name} skill",
            f"site:skillhub.cn {skill_name} skill",
            f"site:clawhub.ai/skills {skill_name} skill",
            f"site:github.com {skill_name} skill",
        ]

    merged: list[dict[str, Any]] = []
    for query in queries[:3]:
        try:
            raw = await tool.handler(query=query, limit=5)
            payload = json.loads(raw)
        except Exception:
            continue
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list):
            continue
        for item in results[:5]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            host = (urlparse(url).netloc or "").lower()
            if not url or not host:
                continue
            merged.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": url,
                    "snippet": str(item.get("snippet") or "").strip(),
                    "host": host,
                }
            )
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in merged:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        deduped.append(item)
    return deduped[:10]


async def _select_remote_skill_candidate_url(
    *,
    tool_caller: Any,
    source_hint: str,
    skill_name: str,
    candidates: list[dict[str, Any]],
) -> str:
    if not candidates:
        return ""
    if tool_caller is None:
        return str(candidates[0].get("url") or "").strip()
    prompt = (
        "你是远程技能页选择器。"
        "请从候选链接中选出最像目标 skill 页面、最适合直接安装的那个 URL。"
        "优先选择 ClawHub / 腾讯 SkillHub 的技能详情页；如果用户指定 GitHub，则选最像仓库首页的 URL。"
        "只返回一个 URL；如果都不合适，返回 NO_MATCH。"
    )
    user_text = json.dumps(
        {
            "source_hint": source_hint,
            "skill_name": skill_name,
            "candidates": candidates[:8],
        },
        ensure_ascii=False,
    )
    try:
        response = await tool_caller.chat_with_tools(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            tools=[],
            use_builtin_search=False,
        )
        text = str(getattr(response, "content", "") or "").strip()
    except Exception:
        text = ""
    if text and text != "NO_MATCH":
        for item in candidates:
            url = str(item.get("url") or "").strip()
            if url and url in text:
                return url
    return str(candidates[0].get("url") or "").strip()


async def maybe_handle_superuser_natural_language_skill_install(
    *,
    text: str,
    plugin_config: Any,
    save_plugin_runtime_config: Callable[[], None] | None,
    logger: Any,
    operator_user_id: str,
    tool_caller: Any,
    tool_registry: Any,
) -> str | None:
    parsed = await _parse_natural_language_install_request(
        text=text,
        tool_caller=tool_caller,
    )
    if str(parsed.get("action") or "") != "INSTALL_REMOTE_SKILL":
        return None
    if save_plugin_runtime_config is None:
        return "当前运行时未提供配置保存器，无法安装远程 skill。"

    source_url = str(parsed.get("source_url") or "").strip()
    skill_name = str(parsed.get("skill_name") or "").strip()
    source_hint = _normalize_source_hint(str(parsed.get("source_hint") or "auto"))

    if not source_url and skill_name:
        candidates = await _search_remote_skill_candidates(
            registry=tool_registry,
            skill_name=skill_name,
            source_hint=source_hint,
        )
        source_url = await _select_remote_skill_candidate_url(
            tool_caller=tool_caller,
            source_hint=source_hint,
            skill_name=skill_name,
            candidates=candidates,
        )
    if not source_url:
        return (
            "我识别到你是在让超管安装远程 skill，但当前没拿到可安装的技能页地址。\n"
            "请直接发 SkillHub/ClawHub/GitHub 的技能页 URL，或者把技能名说得更完整一点。"
        )

    parts = [source_url]
    if parsed.get("ref"):
        parts.append(f"ref={parsed['ref']}")
    if parsed.get("subdir"):
        parts.append(f"subdir={parsed['subdir']}")
    if parsed.get("name"):
        parts.append(f"name={parsed['name']}")
    entry, error = _parse_remote_skill_install_args(" ".join(parts))
    if error or entry is None:
        return error or "远程 skill 地址解析失败。"

    note = "本次通过超管自然语言触发安装，不依赖 SkillHub CLI。"
    _changed, message = install_remote_skill_source(
        entry=entry,
        plugin_config=plugin_config,
        save_plugin_runtime_config=save_plugin_runtime_config,
        logger=logger,
        operator_user_id=operator_user_id,
        prefer_first=bool(parsed.get("prefer_source", False)),
        auto_approve=bool(parsed.get("auto_approve", True)),
        install_note=note,
    )
    return message


def _format_remote_skill_review_items(items: list[dict[str, Any]], *, limit: int = 12) -> str:
    if not items:
        return "暂无匹配的远程 skill 源。"

    lines: list[str] = []
    for index, item in enumerate(items[: max(1, limit)], start=1):
        status = str(item.get("status") or "pending").strip().lower()
        status_text = {
            "pending": "待审批",
            "approved": "已批准",
            "rejected": "已拒绝",
        }.get(status, status or "未知")
        name = str(item.get("name") or "未命名源").strip()
        source = str(item.get("source") or "").strip()
        ref = str(item.get("ref") or "").strip()
        key = str(item.get("key") or "").strip()
        suffix = f" @ {ref}" if ref else ""
        lines.append(f"{index}. [{status_text}] {name}{suffix}")
        if source:
            lines.append(f"   来源: {source}")
        if key:
            lines.append(f"   键值: {key}")
    if len(items) > limit:
        lines.append(f"... 其余 {len(items) - limit} 项未显示")
    return "\n".join(lines)


async def handle_remote_skill_review_command(
    matcher: Any,
    *,
    action_text: str,
    plugin_config: Any,
    logger: Any,
    operator_user_id: str,
) -> None:
    raw_sources = getattr(plugin_config, "personification_skill_sources", None)
    if not raw_sources:
        await matcher.finish("当前未配置任何远程 skill 源。")

    require_review = bool(
        getattr(plugin_config, "personification_skill_require_admin_review", True)
    )
    parts = [part for part in str(action_text or "").strip().split(maxsplit=1) if part]
    action = parts[0].strip().lower() if parts else "查看"
    selector = parts[1].strip() if len(parts) > 1 else ""

    if action in {"查看", "列表", "list", "ls", "show"}:
        status_map = {
            "待审批": "pending",
            "pending": "pending",
            "已批准": "approved",
            "批准": "approved",
            "approved": "approved",
            "已拒绝": "rejected",
            "拒绝": "rejected",
            "rejected": "rejected",
        }
        status = status_map.get(selector.lower(), "") if selector else ""
        items = list_remote_skill_reviews(raw_sources, logger, status=status)
        stats = get_remote_skill_review_stats(raw_sources, logger)
        header = (
            "远程 skill 审批状态\n"
            f"审批要求: {'开启' if require_review else '关闭'}\n"
            f"总计 {stats['total']} 项, 待审批 {stats['pending']} 项, "
            f"已批准 {stats['approved']} 项, 已拒绝 {stats['rejected']} 项"
        )
        usage = (
            "\n用法:\n"
            "远程技能审批 查看 [待审批/已批准/已拒绝]\n"
            "远程技能审批 同意 [名称|key前缀|pending|全部]\n"
            "远程技能审批 拒绝 [名称|key前缀|pending|全部]\n"
            "远程技能审批 重置 [名称|key前缀|pending|全部]"
        )
        await matcher.finish(f"{header}\n\n{_format_remote_skill_review_items(items)}{usage}")

    status_alias = {
        "同意": "approved",
        "批准": "approved",
        "通过": "approved",
        "approve": "approved",
        "approved": "approved",
        "拒绝": "rejected",
        "驳回": "rejected",
        "reject": "rejected",
        "rejected": "rejected",
        "重置": "pending",
        "待审批": "pending",
        "reset": "pending",
        "pending": "pending",
    }
    target_status = status_alias.get(action)
    if not target_status:
        await matcher.finish(
            "命令格式不正确。\n"
            "可用动作: 查看 / 同意 / 拒绝 / 重置\n"
            "示例: 远程技能审批 同意 pending"
        )

    effective_selector = selector or "pending"
    matched_count, matched_items = review_remote_skill_sources(
        raw_sources,
        logger,
        selector=effective_selector,
        status=target_status,
        operator=operator_user_id,
    )
    if matched_count <= 0:
        await matcher.finish("没有找到匹配的远程 skill 源。")

    action_text_map = {
        "approved": "已批准",
        "rejected": "已拒绝",
        "pending": "已重置为待审批",
    }
    await matcher.finish(
        f"{action_text_map[target_status]} {matched_count} 个远程 skill 源。\n"
        f"{_format_remote_skill_review_items(matched_items, limit=8)}\n"
        "结果会在下次重载插件或重启后生效。"
    )


async def handle_clear_context_command(
    matcher: Any,
    *,
    args_text: str,
    event_group_id: Optional[str],
    event_private_user_id: Optional[str],
    chat_histories: Dict[str, Any],
    msg_buffer: Dict[str, Dict[str, Any]],
    save_session_histories: Callable[[], None],
    get_driver: Callable[[], Any],
    build_private_session_id: Callable[[str], str],
    build_group_session_id: Callable[[str], str],
    is_global_clear_command: Callable[[str], bool],
    clear_all_context: Callable[..., int],
    resolve_clear_target: Callable[..., tuple[Optional[str], bool]],
    clear_message_buffer: Callable[[Dict[str, Dict[str, Any]], str], int],
    clear_session_context: Callable[..., Optional[str]],
) -> None:
    if is_global_clear_command(args_text):
        count = clear_all_context(
            chat_histories,
            save_session_histories=save_session_histories,
            driver=get_driver(),
        )
        await matcher.finish(f"已清除全局所有群聊/私聊的对话上下文记忆（共 {count} 个会话）。")

    target_id, is_group = resolve_clear_target(
        args_text=args_text,
        group_id=event_group_id,
        private_user_id=event_private_user_id,
        build_private_session_id=build_private_session_id,
    )
    if not target_id:
        await matcher.finish("无法确定要清除的目标，请指定群号或在群聊/私聊中使用，或使用 '清除记忆 全局'。")

    clear_message_buffer(msg_buffer, target_id)
    msg = clear_session_context(
        chat_histories=chat_histories,
        target_id=target_id,
        is_group=is_group,
        build_group_session_id=build_group_session_id,
        save_session_histories=save_session_histories,
    )
    if msg:
        await matcher.finish(msg)
    await matcher.finish("当前没有任何缓存的对话上下文记忆。")


async def handle_full_reset_memory_command(
    matcher: Any,
    *,
    persona_store: Any,
    msg_buffer: Dict[str, Dict[str, Any]],
    get_driver: Callable[[], Any],
    logger: Any,
) -> None:
    buffer_count = len(msg_buffer)
    for key, item in list(msg_buffer.items()):
        timer_task = item.get("timer_task")
        if timer_task:
            timer_task.cancel()
        msg_buffer.pop(key, None)

    session_count = await clear_all_session_histories()

    persona_stats = {
        "personas": 0,
        "history_users": 0,
        "history_messages": 0,
        "cancelled_tasks": 0,
    }
    if persona_store is not None:
        try:
            persona_stats = await persona_store.clear_all()
        except Exception as e:
            logger.warning(f"[full_reset_memory] clear persona store failed: {e}")

    image_cache_count = 0
    try:
        image_cache_count = await clear_image_result_cache()
    except Exception as e:
        logger.warning(f"[full_reset_memory] clear image cache failed: {e}")

    driver = get_driver()
    driver_cache_count = 0
    if hasattr(driver, "_personification_msg_cache"):
        try:
            driver_cache_count = len(driver._personification_msg_cache)
        except Exception:
            driver_cache_count = 0
        driver._personification_msg_cache.clear()

    store = get_data_store()
    await store.save("chat_history", {})
    await store.save("inner_state", {})
    await store.save("proactive_state", {})

    await matcher.finish(
        "已完全清除拟人插件记忆："
        f"会话 {session_count} 个，"
        f"消息缓冲 {buffer_count} 个，"
        f"用户画像 {persona_stats['personas']} 份，"
        f"画像暂存 {persona_stats['history_users']} 人/{persona_stats['history_messages']} 条，"
        f"取消画像任务 {persona_stats['cancelled_tasks']} 个，"
        f"图片缓存 {image_cache_count} 条，"
        f"驱动缓存 {driver_cache_count} 条。"
        "已保留所有提示词与群配置。"
    )


async def handle_rebuild_plugin_knowledge_command(
    matcher: Any,
    *,
    plugin_config: Any,
    knowledge_store: Any,
    tool_caller: Any,
    logger: Any,
    start_knowledge_builder: Callable[..., Any],
    get_knowledge_build_task: Callable[[], Any],
    set_knowledge_build_task: Callable[[Any], None],
) -> None:
    if knowledge_store is None:
        await matcher.finish("插件知识库未初始化，无法重建。")
    if tool_caller is None:
        await matcher.finish("知识库分析模型未初始化，无法重建插件知识库。")

    current_task = get_knowledge_build_task()
    if current_task is not None and not current_task.done():
        current_task.cancel()
        try:
            await current_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[plugin_knowledge] cancel previous build task failed: {e}")

    new_task = start_knowledge_builder(
        plugin_config=plugin_config,
        tool_caller=tool_caller,
        knowledge_store=knowledge_store,
        logger=logger,
    )
    set_knowledge_build_task(new_task)
    await matcher.finish("已启动插件知识库后台重建。可用“插件知识库状态”查看进度。")


async def handle_view_plugin_knowledge_status_command(
    matcher: Any,
    *,
    knowledge_store: Any,
    get_knowledge_build_task: Callable[[], Any],
) -> None:
    if knowledge_store is None:
        await matcher.finish("插件知识库未初始化。")

    index = await knowledge_store.load_index()
    build_state = await knowledge_store.load_build_state()
    plugins = index.get("plugins", {}) if isinstance(index, dict) else {}
    state_plugins = build_state.get("plugins", {}) if isinstance(build_state, dict) else {}
    if not isinstance(plugins, dict):
        plugins = {}
    if not isinstance(state_plugins, dict):
        state_plugins = {}

    success = 0
    failed = 0
    pending = 0
    recent_failures: list[str] = []
    for plugin_name, meta in state_plugins.items():
        if not isinstance(meta, dict):
            continue
        status = str(meta.get("status", "") or "").strip().lower()
        if status == "success":
            success += 1
        elif status == "failed":
            failed += 1
            if len(recent_failures) < 5:
                error = str(meta.get("error", "") or "").strip()
                recent_failures.append(f"- {plugin_name}: {error or '未知错误'}")
        elif status == "pending":
            pending += 1

    task = get_knowledge_build_task()
    task_status = "空闲"
    if task is not None:
        if not task.done():
            task_status = "构建中"
        elif task.cancelled():
            task_status = "已取消"
        else:
            exc = task.exception()
            task_status = f"已结束（异常: {exc}）" if exc else "已完成"

    lines = [
        "插件知识库状态",
        f"后台任务: {task_status}",
        f"索引插件数: {len(plugins)}",
        f"构建状态: 成功 {success} / 失败 {failed} / 进行中 {pending}",
    ]
    if recent_failures:
        lines.append("最近失败项：")
        lines.extend(recent_failures)
    await matcher.finish("\n".join(lines))
