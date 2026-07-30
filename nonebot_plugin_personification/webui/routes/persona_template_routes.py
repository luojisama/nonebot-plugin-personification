from __future__ import annotations

import asyncio
import html
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urljoin

import httpx
import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ...core import webui_audit_log
from ...core.paths import get_data_dir
from ...core.llm_context import reset_llm_context, set_llm_context
from ...core.persona_template_history import (
    list_persona_template_records,
    record_persona_template_result,
    summarize_persona_template_record,
    write_persona_template_export_file,
)
from ..deps import AdminIdentity, get_client_ip, require_admin


_SOURCE_SUMMARY_LIMIT = 1400
_SOURCE_CORPUS_LIMIT = 18000
_RESEARCH_TIMEOUT_SECONDS = 90
_SYNTHESIS_TIMEOUT_SECONDS = 240
_TEMPLATE_REFERENCE_LIMIT = 24000
_FETCHED_PAGE_LIMIT = 3
_SEARCH_ALIAS_LIMIT = 6
_SEARCH_QUERY_LIMIT = 30
_CHARACTER_SOURCE_THRESHOLD = 50
_TASK_RETENTION_SECONDS = 6 * 60 * 60
ProgressCallback = Callable[[str, str, int], Awaitable[None]]

_ROLE_PAGE_HINTS = (
    "人物列表",
    "角色列表",
    "角色介绍",
    "登场人物",
    "登場人物",
    "登场角色",
    "登場角色",
    "角色",
    "character",
    "characters",
)

_BIOGRAPHY_NOISE_HINTS = (
    "声优",
    "聲優",
    "配音",
    "演员",
    "演員",
    "歌手",
    "艺人",
    "藝人",
    "个人资料",
    "个人经历",
)

_RELATED_CHARACTER_TITLE_HINTS = (
    "的母亲",
    "的母親",
    "的父亲",
    "的父親",
    "的姐姐",
    "的姊姊",
    "的妹妹",
    "的哥哥",
    "的弟弟",
    "的朋友",
    "的同学",
    "的同學",
    "的老师",
    "的老師",
)

_MEDIAWIKI_API_HEADERS = {
    "User-Agent": "PersonificationBot/1.0 (https://github.com/luojisama/nonebot-plugin-shiro-personification) httpx/python",
    "Accept": "application/json",
}

_BAIKE_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

_REQUIRED_TEMPLATE_KEYS = (
    "name",
    "tts",
    "status",
    "nick_name",
    "ack_phrases",
    "initial_message",
    "mute_keyword",
    "input",
    "system",
)

_REQUIRED_INPUT_PLACEHOLDERS = (
    "{time}",
    "{trigger_reason}",
    "{history_new}",
    "{history_last}",
    "{status}",
    "{schedule_instruction}",
)
_RECOMMENDED_SYSTEM_SECTIONS = (
    ("角色身份", ("身份", "角色")),
    ("性格与行为模式", ("性格", "行为")),
    ("群聊说话方式", ("说话", "口吻", "群聊")),
    ("角色关系", ("关系", "称呼")),
    ("资料冲突与缺口", ("资料冲突", "缺口", "待确认")),
)
_ASSISTANT_ROLE_PATTERNS = (
    r"我是\s*(?:AI|人工智能)",
    r"作为\s*(?:AI|人工智能|助手|客服)",
    r"(?:AI|智能)\s*助手",
    r"客服",
    r"有什么可以帮",
)


@dataclass
class _PersonaTemplateTask:
    task_id: str
    work_title: str
    character_name: str
    created_at: float
    status: str = "queued"
    stage: str = "queued"
    message: str = "已加入构建队列..."
    progress: int = 1
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "work_title": self.work_title,
            "character_name": self.character_name,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "progress": max(0, min(100, int(self.progress or 0))),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }


_TASKS: dict[str, _PersonaTemplateTask] = {}
_TASK_LOCK = asyncio.Lock()


def _clip_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _clip_multiline(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 32].rstrip() + "\n...<truncated>"


def _runtime_bundle(runtime: Any) -> Any | None:
    return getattr(runtime, "runtime_bundle", None)


def _main_ai_caller(runtime: Any) -> Any | None:
    bundle = _runtime_bundle(runtime)
    if bundle is None:
        return None
    caller = getattr(bundle, "call_ai_api", None)
    if callable(caller):
        return caller
    deps = getattr(bundle, "reply_processor_deps", None)
    runtime_inner = getattr(deps, "runtime", None) if deps is not None else None
    caller = getattr(runtime_inner, "call_ai_api", None) if runtime_inner is not None else None
    return caller if callable(caller) else None


async def _call_main_model(
    caller: Any,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    use_builtin_search: bool = False,
    timeout: int = _RESEARCH_TIMEOUT_SECONDS,
    purpose: str = "",
    stage_label: str = "主模型调用",
) -> str:
    async def _invoke() -> Any:
        try:
            return await caller(
                messages,
                temperature=temperature,
                use_builtin_search=use_builtin_search,
            )
        except TypeError:
            try:
                return await caller(messages, None, None, temperature)
            except TypeError:
                return await caller(messages)

    token = set_llm_context(purpose=purpose) if purpose else None
    try:
        raw = await asyncio.wait_for(_invoke(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"{stage_label}超时（>{timeout}秒）") from exc
    finally:
        if token is not None:
            reset_llm_context(token)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    content = getattr(raw, "content", None)
    if content is not None:
        return str(content or "").strip()
    return str(raw or "").strip()


def _source_key(source: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(source.get("source", "") or ""),
        str(source.get("title", "") or ""),
        str(source.get("url", "") or ""),
    )


def _dedupe_keep_order(values: list[str], limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _compact_alias_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or len(text) > 80:
        return ""
    if re.search(r"https?://|[\r\n{}<>]", text, flags=re.I):
        return ""
    return text


def _cjk_spaced_variants(value: Any) -> list[str]:
    compact = re.sub(r"\s+", "", str(value or "").strip())
    if not compact or len(compact) < 3:
        return []
    if not re.fullmatch(r"[\u3400-\u9fff\u3040-\u30ff\uff00-\uffef]+", compact):
        return []
    variants: list[str] = []
    # Many ACG character pages are indexed as "surname given-name" even when
    # users input the no-space Chinese spelling. Try generic split points only.
    for index in (2, 1, 3):
        if 0 < index < len(compact):
            variants.append(f"{compact[:index]} {compact[index:]}")
    if len(compact) <= 6:
        variants.append(" ".join(compact))
    return _dedupe_keep_order(variants, limit=4)


def _clean_alias_list(values: list[Any], primary: str, *, limit: int = _SEARCH_ALIAS_LIMIT) -> list[str]:
    cleaned = [_compact_alias_text(primary)]
    cleaned.extend(_compact_alias_text(value) for value in values)
    return _dedupe_keep_order([value for value in cleaned if value], limit=limit)


def _normalize_search_aliases(
    raw: dict[str, Any] | None,
    *,
    work_title: str,
    character_name: str,
) -> dict[str, list[str]]:
    raw = raw if isinstance(raw, dict) else {}
    work_aliases_raw = raw.get("work_aliases", [])
    character_aliases_raw = raw.get("character_aliases", [])
    extra_queries_raw = raw.get("queries", [])
    if not isinstance(work_aliases_raw, list):
        work_aliases_raw = []
    if not isinstance(character_aliases_raw, list):
        character_aliases_raw = []
    if not isinstance(extra_queries_raw, list):
        extra_queries_raw = []
    character_values: list[Any] = [*character_aliases_raw, *_cjk_spaced_variants(character_name)]
    return {
        "work_aliases": _clean_alias_list(work_aliases_raw, work_title),
        "character_aliases": _clean_alias_list(character_values, character_name),
        "queries": _dedupe_keep_order(
            [_compact_alias_text(value) for value in extra_queries_raw if _compact_alias_text(value)],
            limit=6,
        ),
    }


def _alias_values(search_aliases: dict[str, list[str]] | None, key: str, primary: str) -> list[str]:
    if not search_aliases:
        return _clean_alias_list([], primary)
    values = search_aliases.get(key, []) if isinstance(search_aliases, dict) else []
    return _clean_alias_list(list(values or []), primary)


def _normalized_for_match(value: Any) -> str:
    return re.sub(r"[\s\-_/|:：,，。！？!?（）()【】\[\]<>]+", "", str(value or "").lower())


def _source_relevance_score(
    *,
    work_title: str,
    character_name: str,
    title: str,
    summary: str,
    search_aliases: dict[str, list[str]] | None = None,
) -> int:
    title_text = str(title or "")
    summary_text = str(summary or "")
    title_norm = _normalized_for_match(title_text)
    summary_norm = _normalized_for_match(summary_text)
    haystack = f"{title_norm}{summary_norm}"
    work_keys = [
        _normalized_for_match(value)
        for value in _alias_values(search_aliases, "work_aliases", work_title)
    ]
    character_keys = [
        _normalized_for_match(value)
        for value in _alias_values(search_aliases, "character_aliases", character_name)
    ]
    role_hints = [_normalized_for_match(value) for value in _ROLE_PAGE_HINTS]
    noise_hints = [_normalized_for_match(value) for value in _BIOGRAPHY_NOISE_HINTS]

    title_has_character = any(key and key in title_norm for key in character_keys)
    title_exact_character = any(key and title_norm == key for key in character_keys)
    summary_has_character = any(key and key in summary_norm for key in character_keys)
    title_has_work = any(key and key in title_norm for key in work_keys)
    has_work = any(key and key in haystack for key in work_keys)
    title_has_role_hint = any(hint and hint in title_norm for hint in role_hints)
    has_role_hint = any(hint and hint in haystack for hint in role_hints)
    title_has_noise = any(hint and hint in title_norm for hint in noise_hints)
    title_is_related_character = title_has_character and not title_exact_character and any(
        _normalized_for_match(hint) in title_norm for hint in _RELATED_CHARACTER_TITLE_HINTS
    )

    if title_has_character and has_work:
        score = 100
    elif title_has_character:
        score = 92
    elif summary_has_character and has_work and has_role_hint:
        score = 72
    elif title_has_work and title_has_role_hint and summary_has_character:
        score = 66
    elif title_has_work and title_has_role_hint:
        score = 54
    elif summary_has_character and has_work:
        score = 44
    elif has_work and has_role_hint:
        score = 38
    else:
        score = 0

    if title_has_noise and not title_has_character:
        score -= 35
    if title_is_related_character:
        score = min(score, 42)
    return max(0, score)


def _source_relevant(
    *,
    work_title: str,
    character_name: str,
    title: str,
    summary: str,
    search_aliases: dict[str, list[str]] | None = None,
) -> bool:
    return (
        _source_relevance_score(
            work_title=work_title,
            character_name=character_name,
            title=title,
            summary=summary,
            search_aliases=search_aliases,
        )
        >= _CHARACTER_SOURCE_THRESHOLD
    )


def _source_rank(
    source: dict[str, Any],
    *,
    work_title: str,
    character_name: str,
    search_aliases: dict[str, list[str]] | None = None,
) -> tuple[float, float]:
    relevance = _source_relevance_score(
        work_title=work_title,
        character_name=character_name,
        title=str(source.get("title") or ""),
        summary=str(source.get("summary") or ""),
        search_aliases=search_aliases,
    )
    kind_weight = {
        "wiki_api": 0.18,
        "baike_api": 0.17,
        "web_page": 0.12,
        "wiki": 0.10,
        "web_search": 0.03,
    }.get(str(source.get("kind") or ""), 0.0)
    try:
        confidence = float(source.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (relevance + confidence * 10 + kind_weight, confidence)


def _persona_search_queries(
    work_title: str,
    character_name: str,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[str]:
    work_aliases = _alias_values(search_aliases, "work_aliases", work_title)[:3]
    character_aliases = _alias_values(search_aliases, "character_aliases", character_name)[:4]
    queries = [
        f"{work_aliases[0]} {character_aliases[0]}",
        f"{character_aliases[0]} {work_aliases[0]} 角色",
        f"{work_aliases[0]} {character_aliases[0]} 人物设定 口癖 关系",
        f"{work_aliases[0]} {character_aliases[0]} 官方 角色介绍",
        f"{work_aliases[0]} {character_aliases[0]} wiki",
        f"{work_aliases[0]} {character_aliases[0]} character profile",
        f"{work_aliases[0]} {character_aliases[0]} official character",
    ]
    for character in character_aliases:
        queries.extend(
            [
                f"{work_aliases[0]} {character}",
                f"{character} {work_aliases[0]}",
                f"{character} {work_aliases[0]} wiki",
            ]
        )
    for work in work_aliases[1:]:
        for character in character_aliases[:3]:
            queries.extend(
                [
                    f"{work} {character}",
                    f"{character} {work}",
                    f"{character} {work} character profile",
                ]
            )
    for character in character_aliases:
        queries.extend(
            [
                character,
                f"{character} wiki",
                f"{character} 角色",
            ]
        )
    if search_aliases:
        queries.extend(search_aliases.get("queries") or [])
    return _dedupe_keep_order(queries, limit=_SEARCH_QUERY_LIMIT)


def _persona_site_search_queries(
    work_title: str,
    character_name: str,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[str]:
    work_aliases = _alias_values(search_aliases, "work_aliases", work_title)[:2]
    character_aliases = _alias_values(search_aliases, "character_aliases", character_name)[:4]
    domains = (
        "moegirl.org.cn",
        "mzh.moegirl.org.cn",
        "moegirl.uk",
        "baike.baidu.com",
        "fandom.com",
    )
    queries: list[str] = []
    for character in character_aliases:
        for domain in domains[:4]:
            queries.append(f"site:{domain} {character}")
    for work in work_aliases:
        for character in character_aliases[:3]:
            queries.extend(
                [
                    f"site:fandom.com {character} {work}",
                    f'"{character}" "{work}" 角色',
                ]
            )
    return _dedupe_keep_order(queries, limit=10)


def _resolve_prompt_path(raw_path: str) -> Path:
    cleaned = str(raw_path or "").strip().strip('"').strip("'")
    path = Path(cleaned).expanduser()
    if path.is_file():
        return path
    return Path(cleaned.replace("\\", "/")).expanduser()


def _configured_template_reference(runtime: Any) -> dict[str, Any]:
    plugin_config = getattr(runtime, "plugin_config", None)
    candidates = [
        getattr(plugin_config, "personification_prompt_path", "") if plugin_config is not None else "",
        getattr(plugin_config, "personification_system_path", "") if plugin_config is not None else "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            path = _resolve_prompt_path(str(candidate))
            if not path.is_file() or path.suffix.lower() not in {".yml", ".yaml"}:
                continue
            content = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
            keys = list(parsed.keys()) if isinstance(parsed, dict) else []
            return {
                "path": str(path),
                "exists": True,
                "keys": keys,
                "content": _clip_multiline(content, _TEMPLATE_REFERENCE_LIMIT),
            }
        except Exception as exc:
            return {
                "path": str(candidate),
                "exists": False,
                "keys": [],
                "content": "",
                "error": _clip_text(exc, 240),
            }
    return {
        "path": "",
        "exists": False,
        "keys": [],
        "content": "",
    }


def _template_apply_target(runtime: Any, result: dict[str, Any]) -> Path:
    plugin_config = getattr(runtime, "plugin_config", None)
    configured = ""
    if plugin_config is not None:
        configured = str(getattr(plugin_config, "personification_prompt_path", "") or "").strip()
        if not configured:
            configured = str(getattr(plugin_config, "personification_system_path", "") or "").strip()
    configured = configured.strip('"').strip("'")
    if configured:
        path = _resolve_prompt_path(configured)
        if path.suffix.lower() not in {".yml", ".yaml"}:
            raise RuntimeError("当前人设路径不是 YAML 文件，请先在配置中心设置 personification_prompt_path 为 .yaml/.yml")
        return path
    work = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", str(result.get("work_title") or "persona")).strip("_")[:64] or "persona"
    character = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", str(result.get("character_name") or "template")).strip("_")[:64] or "template"
    return Path(get_data_dir(plugin_config)) / "persona_templates" / f"{work}_{character}_applied.yaml"


def _apply_persona_template(runtime: Any, result: dict[str, Any]) -> dict[str, Any]:
    template = str(result.get("template") or "").strip()
    if not template:
        raise RuntimeError("没有可应用的 YAML 模板")
    validation = _validate_template_yaml(template)
    if not validation.get("valid"):
        errors = "；".join(str(item) for item in list(validation.get("errors") or [])[:3])
        raise RuntimeError(f"YAML 校验未通过，不能应用：{errors or '结构不完整'}")
    target = _template_apply_target(runtime, result)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(validation.get("template") or template, encoding="utf-8")
    plugin_config = getattr(runtime, "plugin_config", None)
    if plugin_config is not None and not str(getattr(plugin_config, "personification_prompt_path", "") or "").strip():
        setattr(plugin_config, "personification_prompt_path", str(target))
    bundle = _runtime_bundle(runtime)
    save_config = getattr(bundle, "save_plugin_runtime_config", None) if bundle is not None else None
    if callable(save_config):
        save_config()
    reload_services = getattr(bundle, "reload_runtime_services", None) if bundle is not None else None
    if callable(reload_services):
        reload_services()
    return {
        "applied": True,
        "path": str(target.resolve()),
        "template_keys": list(validation.get("keys") or []),
    }


async def _set_task_progress(
    task: _PersonaTemplateTask | None,
    *,
    stage: str,
    message: str,
    progress: int,
    status: str | None = None,
) -> None:
    if task is None:
        return
    task.stage = stage
    task.message = message
    task.progress = max(0, min(100, int(progress)))
    task.updated_at = time.time()
    if status:
        task.status = status


async def _default_progress(_: str, __: str, ___: int) -> None:
    return None


def _cleanup_finished_tasks() -> None:
    now = time.time()
    expired = [
        task_id
        for task_id, task in _TASKS.items()
        if task.status in {"done", "error"} and now - float(task.updated_at or task.created_at) > _TASK_RETENTION_SECONDS
    ]
    for task_id in expired:
        _TASKS.pop(task_id, None)


def _get_task(task_id: str) -> _PersonaTemplateTask | None:
    _cleanup_finished_tasks()
    return _TASKS.get(str(task_id or ""))


async def _run_persona_template_build(
    *,
    runtime: Any,
    work_title: str,
    character_name: str,
    actor: str = "",
    source: str = "webui",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    caller = _main_ai_caller(runtime)
    if caller is None:
        raise RuntimeError("主模型调用器未就绪")
    progress = progress or _default_progress

    started = time.time()
    await progress("alias_planning", "正在梳理检索别名...", 5)
    await progress("source_gathering", "正在收集百科与联网资料...", 12)
    sources = await _gather_persona_sources(
        runtime=runtime,
        work_title=work_title,
        character_name=character_name,
    )
    source_text = _source_corpus(sources)
    await progress("subagent_research", f"已收集 {len(sources)} 个资料源，正在运行 3 个研究子agent...", 44)
    focus_items = [
        ("基础设定子agent", "官方身份、百科摘要、基础人设、剧情定位、资料可信度"),
        ("性格台词子agent", "性格模式、口癖、说话节奏、萌点、群聊可迁移表达"),
        ("关系视觉子agent", "角色关系、立绘/服装/视觉锚点、冲突资料与缺口"),
    ]
    subagents = await asyncio.gather(
        *[
            _run_subagent(
                caller=caller,
                agent_name=name,
                focus=focus,
                work_title=work_title,
                character_name=character_name,
                source_text=source_text,
            )
            for name, focus in focus_items
        ]
    )
    reference_template = _configured_template_reference(runtime)
    await progress("template_synthesis", "正在按插件 YAML 结构生成人设模板...", 78)
    template = await _synthesize_template(
        caller=caller,
        work_title=work_title,
        character_name=character_name,
        source_text=source_text,
        subagents=list(subagents),
        reference_template=reference_template,
    )
    validation = _validate_template_yaml(template)
    if not validation.get("valid"):
        await progress("template_repair", "正在修复模板结构与占位符...", 88)
        repaired = await _repair_template_yaml(
            caller=caller,
            work_title=work_title,
            character_name=character_name,
            template=template,
            validation=validation,
            reference_template=reference_template,
        )
        repaired_validation = _validate_template_yaml(repaired)
        if repaired_validation.get("valid"):
            template = repaired_validation["template"]
            validation = repaired_validation
        else:
            template = _strip_yaml_fence(template)
    await progress("finalize", "正在整理输出结果...", 95)
    conflicts: list[str] = []
    for item in subagents:
        report = item.get("report") if isinstance(item, dict) else {}
        raw_conflicts = report.get("conflicts", []) if isinstance(report, dict) else []
        if isinstance(raw_conflicts, list):
            conflicts.extend(str(x) for x in raw_conflicts if str(x).strip())
    result = {
        "success": True,
        "work_title": work_title,
        "character_name": character_name,
        "model_role": "configured_main",
        "duration_ms": int((time.time() - started) * 1000),
        "sources": sources,
        "subagents": list(subagents),
        "conflicts": conflicts[:20],
        "template": validation.get("template") or _strip_yaml_fence(template),
        "template_valid": bool(validation.get("valid")),
        "template_errors": list(validation.get("errors") or [])[:20],
        "template_warnings": list(validation.get("warnings") or [])[:20],
        "template_keys": list(validation.get("keys") or []),
        "template_reference": reference_template,
    }
    record = record_persona_template_result(result, actor=actor, source=source)
    result["history_record"] = summarize_persona_template_record(record)
    export_path = write_persona_template_export_file(record, plugin_config=getattr(runtime, "plugin_config", None))
    result["export_path"] = str(export_path)
    await progress("done", "人设模板已完成。", 100)
    return result


async def _gather_wiki_sources(
    *,
    work_title: str,
    character_name: str,
    plugin_config: Any,
    logger: Any,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    try:
        from ...skills.skillpacks.wiki_search.scripts.impl import wiki_lookup_candidates
        from ...skills.skillpacks.wiki_search.scripts.main import resolve_wiki_runtime_config
    except Exception:
        return []

    wiki_enabled, _fandom_enabled, extra_fandom_wikis = resolve_wiki_runtime_config(plugin_config)
    if not wiki_enabled:
        return []
    queries = _persona_search_queries(work_title, character_name, search_aliases)[:6]
    sources: list[dict[str, Any]] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def _lookup_one(query: str) -> list[dict[str, Any]]:
            try:
                payload = await asyncio.wait_for(
                    wiki_lookup_candidates(
                        query,
                        extra_fandom_wikis=extra_fandom_wikis,
                        http_client=client,
                        logger=logger,
                    ),
                    timeout=10,
                )
            except Exception:
                return []
            items: list[dict[str, Any]] = []
            for item in payload.get("top_candidates", []) or []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "") or "")
                summary = _clip_text(item.get("summary", ""), _SOURCE_SUMMARY_LIMIT)
                if not _source_relevant(
                    work_title=work_title,
                    character_name=character_name,
                    title=title,
                    summary=summary,
                    search_aliases=search_aliases,
                ):
                    continue
                items.append(
                    {
                        "kind": "wiki",
                        "query": query,
                        "source": str(item.get("source", "") or "Wiki"),
                        "title": title,
                        "url": str(item.get("url", "") or ""),
                        "summary": summary,
                        "confidence": item.get("confidence", 0),
                    }
                )
            return items

        results = await asyncio.gather(*(_lookup_one(query) for query in queries))
        for items in results:
            sources.extend(items)
    return sources


def _html_text_excerpt(raw_html: str, limit: int = 520) -> str:
    text = str(raw_html or "")
    meta = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']',
        text,
        flags=re.I,
    )
    meta_text = _clip_text(html.unescape(meta.group(1)), limit) if meta else ""
    body = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", text)
    body = re.sub(r"(?is)<table.*?</table>|<nav.*?</nav>|<footer.*?</footer>", " ", body)
    paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)
    for paragraph in paragraphs[:12]:
        cleaned = html.unescape(re.sub(r"<[^>]+>", " ", paragraph))
        cleaned = re.sub(r"\[[^\]]{1,12}\]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) >= 40 and cleaned != meta_text:
            return _clip_text(cleaned, limit)
    cleaned = html.unescape(re.sub(r"<[^>]+>", " ", body))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) >= 80:
        return _clip_text(cleaned, limit)
    return meta_text or _clip_text(cleaned, limit)


def _html_title(raw_html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", str(raw_html or ""))
    if not match:
        return ""
    return _clip_text(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))), 120)


def _direct_profile_urls(work_title: str, character_name: str) -> list[tuple[str, str]]:
    char = quote(re.sub(r"\s+", "", str(character_name or "").strip()))
    return [
        ("萌娘百科镜像", f"https://moegirl.icu/{char}"),
        ("百度百科", f"https://baike.baidu.com/item/{char}"),
    ]


async def _plan_search_aliases(
    *,
    runtime: Any,
    work_title: str,
    character_name: str,
) -> dict[str, list[str]]:
    aliases = _normalize_search_aliases(
        None,
        work_title=work_title,
        character_name=character_name,
    )
    caller = _main_ai_caller(runtime)
    if caller is None:
        return aliases
    logger = getattr(runtime, "logger", None)
    system = (
        "你是 Web 检索查询规划器，只负责为 ACG/作品角色资料检索生成别名。"
        "不要编造角色事实，不要输出资料正文，不要输出 URL。"
        "如果不确定某个别名就省略。输出必须是 JSON。"
    )
    user = f"""
作品名：{work_title}
角色名：{character_name}

请输出 JSON：
{{
  "work_aliases": ["作品的其他常见中文名、日文名、英文名或罗马字名，最多 5 个"],
  "character_aliases": ["角色的其他常见中文名、日文名、英文名、罗马字名或带空格写法，最多 5 个"],
  "queries": ["额外推荐的通用搜索查询，最多 5 个"]
}}

规则：
- 必须适用于任意作品和任意角色，不能写固定站点专用脚本。
- work_aliases 和 character_aliases 不要包含 URL。
- queries 应是普通搜索词，例如“作品别名 角色别名 wiki/character profile/官方角色介绍”。
- 不确定就少写，禁止把猜测当事实。
""".strip()
    try:
        text = await _call_main_model(
            caller,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            use_builtin_search=False,
            timeout=25,
            purpose="persona_template_alias_planning",
            stage_label="检索别名规划",
        )
        parsed = _extract_json_object(text)
        if parsed:
            aliases = _normalize_search_aliases(
                parsed,
                work_title=work_title,
                character_name=character_name,
            )
    except Exception as exc:
        if logger:
            try:
                logger.debug(f"persona builder alias planning skipped: {exc}")
            except Exception:
                pass
    return aliases


async def _fetch_mediawiki_extract(
    *,
    client: httpx.AsyncClient,
    api_url: str,
    title: str,
    source_name: str,
    query: str,
) -> dict[str, Any] | None:
    params = {
        "action": "query",
        "prop": "extracts|info",
        "titles": title,
        "inprop": "url",
        "explaintext": "1",
        "exintro": "1",
        "format": "json",
        "utf8": "1",
        "redirects": "1",
    }
    try:
        resp = await client.get(api_url, params=params, headers=_MEDIAWIKI_API_HEADERS)
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None
    pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
    if not isinstance(pages, dict):
        return None
    for page in pages.values():
        if not isinstance(page, dict) or "missing" in page:
            continue
        extract = _clip_text(page.get("extract", ""), _SOURCE_SUMMARY_LIMIT)
        page_title = str(page.get("title") or title)
        url = str(page.get("fullurl") or "")
        if extract:
            return {
                "kind": "wiki_api",
                "query": query,
                "source": source_name,
                "title": page_title,
                "url": url,
                "summary": extract,
                "confidence": 0.82,
            }
    return None


async def _fetch_mediawiki_search_sources(
    *,
    client: httpx.AsyncClient,
    api_url: str,
    source_name: str,
    queries: list[str],
    work_title: str,
    character_name: str,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    titles: list[tuple[str, str]] = []
    seen_titles: set[str] = set()
    for query in queries:
        params = {
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": query,
            "srlimit": 3,
            "srprop": "snippet|titlesnippet",
            "utf8": "1",
            "formatversion": "2",
        }
        try:
            resp = await client.get(api_url, params=params, headers=_MEDIAWIKI_API_HEADERS)
            if resp.status_code != 200:
                continue
            payload = resp.json()
        except Exception:
            continue
        hits = payload.get("query", {}).get("search", []) if isinstance(payload, dict) else []
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            title = str(hit.get("title") or "").strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            titles.append((title, query))
            if len(titles) >= 8:
                break
        if len(titles) >= 8:
            break

    if not titles:
        return []

    async def _fetch_title(title: str, query: str) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(
                _fetch_mediawiki_extract(
                    client=client,
                    api_url=api_url,
                    title=title,
                    source_name=source_name,
                    query=query,
                ),
                timeout=5,
            )
        except Exception:
            return None

    results = await asyncio.gather(*(_fetch_title(title, query) for title, query in titles))
    sources: list[dict[str, Any]] = []
    for item in results:
        if item is None:
            continue
        if not _source_relevant(
            work_title=work_title,
            character_name=character_name,
            title=str(item.get("title") or ""),
            summary=str(item.get("summary") or ""),
            search_aliases=search_aliases,
        ):
            continue
        item["confidence"] = max(float(item.get("confidence") or 0), 0.78)
        sources.append(item)
    return sources


async def _fetch_baike_open_source(
    *,
    client: httpx.AsyncClient,
    keyword: str,
    work_title: str,
    character_name: str,
    search_aliases: dict[str, list[str]] | None = None,
) -> dict[str, Any] | None:
    keyword = re.sub(r"\s+", "", str(keyword or "").strip())
    if not keyword:
        return None
    params = {
        "scope": "103",
        "format": "json",
        "appid": "379020",
        "bk_key": keyword,
        "bk_length": "800",
    }
    try:
        resp = await client.get(
            "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi",
            params=params,
            headers=_BAIKE_API_HEADERS,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("errno"):
        return None
    title = str(payload.get("key") or payload.get("title") or keyword).strip()
    desc = _clean_html_fragment(payload.get("desc") or "")
    abstract = _clean_html_fragment(payload.get("abstract") or "")
    facts: list[str] = []
    for card in payload.get("card") or []:
        if not isinstance(card, dict):
            continue
        name = _clean_html_fragment(card.get("name") or "")
        values = card.get("value") or card.get("format") or []
        if isinstance(values, str):
            values = [values]
        if not name or not isinstance(values, list):
            continue
        value_text = "、".join(_clean_html_fragment(value) for value in values[:4] if value)
        if value_text:
            facts.append(f"{name}: {value_text}")
        if len(facts) >= 8:
            break
    summary = _clip_text(" ".join([part for part in [desc, abstract, *facts] if part]), _SOURCE_SUMMARY_LIMIT)
    if not summary:
        return None
    if not _source_relevant(
        work_title=work_title,
        character_name=character_name,
        title=title,
        summary=summary,
        search_aliases=search_aliases,
    ):
        return None
    return {
        "kind": "baike_api",
        "query": keyword,
        "source": "百度百科开放接口",
        "title": title,
        "url": str(payload.get("url") or payload.get("wapUrl") or ""),
        "summary": summary,
        "confidence": 0.8,
    }


async def _gather_special_api_sources(
    *,
    work_title: str,
    character_name: str,
    client: httpx.AsyncClient,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    api_targets = (
        ("https://zh.wikipedia.org/w/api.php", "维基百科"),
        ("https://zh.moegirl.icu/api.php", "萌娘百科镜像"),
    )
    character_aliases = _alias_values(search_aliases, "character_aliases", character_name)[:3]
    search_queries = _dedupe_keep_order(
        [
            *character_aliases,
            *(f"{character_alias} {work_title}" for character_alias in character_aliases),
        ],
        limit=8,
    )

    async def _fetch_one(api_url: str, source_name: str, character_alias: str) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(
                _fetch_mediawiki_extract(
                    client=client,
                    api_url=api_url,
                    title=character_alias,
                    source_name=source_name,
                    query=f"{work_title} {character_alias}",
                ),
                timeout=5,
            )
        except Exception:
            return None

    results = await asyncio.gather(
        *(
            _fetch_one(api_url, source_name, character_alias)
            for api_url, source_name in api_targets
            for character_alias in character_aliases
        )
    )
    for item in results:
        if item is not None and _source_relevant(
            work_title=work_title,
            character_name=character_name,
            title=str(item.get("title") or ""),
            summary=str(item.get("summary") or ""),
            search_aliases=search_aliases,
        ):
            sources.append(item)
    search_results = await asyncio.gather(
        *(
            _fetch_mediawiki_search_sources(
                client=client,
                api_url=api_url,
                source_name=source_name,
                queries=search_queries,
                work_title=work_title,
                character_name=character_name,
                search_aliases=search_aliases,
            )
            for api_url, source_name in api_targets
        )
    )
    for items in search_results:
        sources.extend(items)
    return sources


async def _gather_direct_page_sources(
    *,
    work_title: str,
    character_name: str,
    logger: Any,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    sources: list[dict[str, Any]] = []
    timeout = httpx.Timeout(10.0, connect=4.0)
    async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=timeout) as client:
        sources.extend(
            await _gather_special_api_sources(
                work_title=work_title,
                character_name=character_name,
                client=client,
                search_aliases=search_aliases,
            )
        )
        baike_keywords: list[str] = []
        seen_baike_keywords: set[str] = set()
        for character_alias in _alias_values(search_aliases, "character_aliases", character_name)[:5]:
            keyword = re.sub(r"\s+", "", str(character_alias or "").strip())
            if not keyword or keyword.lower() in seen_baike_keywords:
                continue
            seen_baike_keywords.add(keyword.lower())
            baike_keywords.append(keyword)
            if len(baike_keywords) >= 3:
                break
        baike_results = await asyncio.gather(
            *(
                _fetch_baike_open_source(
                    client=client,
                    keyword=keyword,
                    work_title=work_title,
                    character_name=character_name,
                    search_aliases=search_aliases,
                )
                for keyword in baike_keywords
            )
        )
        sources.extend(item for item in baike_results if item is not None)
        direct_url_pairs: list[tuple[str, str]] = []
        for character_alias in _alias_values(search_aliases, "character_aliases", character_name)[:3]:
            if re.search(r"[\u3400-\u9fff]", character_alias):
                direct_url_pairs.extend(_direct_profile_urls(work_title, character_alias))
        seen_direct_urls: set[str] = set()
        for source_name, url in direct_url_pairs:
            if url in seen_direct_urls:
                continue
            seen_direct_urls.add(url)
            try:
                resp = await client.get(url)
                if resp.status_code >= 500 or not resp.text:
                    continue
                title = _html_title(resp.text) or character_name
                summary = _html_text_excerpt(resp.text)
                normalized_page = re.sub(r"\s+", "", f"{title} {summary}").lower()
                if "justamoment" in normalized_page or "checkingyourconnection" in normalized_page:
                    continue
                title_key = re.sub(r"\s+", "", title)
                if not _source_relevant(
                    work_title=work_title,
                    character_name=character_name,
                    title=title,
                    summary=summary,
                    search_aliases=search_aliases,
                ):
                    continue
                sources.append(
                    {
                        "kind": "web_page",
                        "query": f"{work_title} {character_name}",
                        "source": source_name,
                        "title": title,
                        "url": str(resp.url),
                        "summary": summary,
                        "confidence": 0.7,
                    }
                )
            except Exception as exc:
                if logger:
                    try:
                        logger.debug(f"persona builder direct page failed: {url}: {exc}")
                    except Exception:
                        pass
    return sources


def _parse_web_search_sources(query: str, rendered: str) -> list[dict[str, Any]]:
    text = str(rendered or "").strip()
    if not text:
        return []
    header = text.splitlines()[0] if text.splitlines() else ""
    source_name = "联网搜索"
    source_match = re.search(r"来源=([^\]\s]+)", header)
    if source_match:
        source_name = f"联网搜索/{source_match.group(1)}"
    if "命中=0" in header:
        return [
            {
                "kind": "web_search_empty",
                "query": query,
                "source": source_name,
                "title": query,
                "url": "",
                "summary": _clip_text(text, _SOURCE_SUMMARY_LIMIT),
                "confidence": 0,
            }
        ]

    sources: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    snippet_lines: list[str] = []
    item_re = re.compile(r"^\s*\d+\.\s+\*\*(.*?)\*\*\s+—\s*(.*?)\s*$")
    for raw_line in text.splitlines()[1:]:
        line = raw_line.strip()
        if not line:
            continue
        match = item_re.match(line)
        if match:
            if current is not None:
                current["summary"] = _clip_text(" ".join(snippet_lines), _SOURCE_SUMMARY_LIMIT)
                sources.append(current)
            title = re.sub(r"\s+", " ", match.group(1)).strip()
            domain = re.sub(r"\s+", " ", match.group(2)).strip()
            current = {
                "kind": "web_search",
                "query": query,
                "source": source_name,
                "title": title,
                "url": "",
                "domain": domain,
                "summary": "",
                "confidence": 0.55,
            }
            snippet_lines = []
            continue
        if current is None:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            current["url"] = line
            continue
        snippet_lines.append(re.sub(r"^\s*(摘要\(综合\):\s*)?", "", line).strip())
    if current is not None:
        current["summary"] = _clip_text(" ".join(snippet_lines), _SOURCE_SUMMARY_LIMIT)
        sources.append(current)
    return sources


def _clean_html_fragment(value: Any) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


async def _bing_search_sources(
    *,
    query: str,
    work_title: str,
    character_name: str,
    logger: Any,
    search_aliases: dict[str, list[str]] | None = None,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=10.0) as client:
            resp = await client.get(
                "https://www.bing.com/search",
                params={"q": query, "mkt": "zh-CN", "setlang": "zh-CN"},
            )
            if resp.status_code != 200:
                return []
            html_text = resp.text
    except Exception as exc:
        if logger:
            try:
                logger.debug(f"persona builder bing search failed: {query}: {exc}")
            except Exception:
                pass
        return []

    out: list[dict[str, Any]] = []
    blocks = re.findall(r'<li class="b_algo".*?</li>', html_text, flags=re.S | re.I)
    for block in blocks:
        match = re.search(r'<h2.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S | re.I)
        if not match:
            continue
        url = html.unescape(match.group(1)).strip()
        title = _clean_html_fragment(match.group(2))
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.S | re.I)
        snippet = _clean_html_fragment(snippet_match.group(1)) if snippet_match else ""
        if not url.startswith(("http://", "https://")) or not title:
            continue
        if not _source_relevant(
            work_title=work_title,
            character_name=character_name,
            title=title,
            summary=snippet,
            search_aliases=search_aliases,
        ):
            continue
        out.append(
            {
                "kind": "web_search",
                "query": query,
                "source": "Bing",
                "title": title,
                "url": url,
                "summary": _clip_text(snippet, _SOURCE_SUMMARY_LIMIT),
                "confidence": 0.58,
            }
        )
        if len(out) >= max_results:
            break
    return out


async def _sogou_search_sources(
    *,
    query: str,
    work_title: str,
    character_name: str,
    logger: Any,
    search_aliases: dict[str, list[str]] | None = None,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=10.0) as client:
            resp = await client.get("https://www.sogou.com/web", params={"query": query})
            if resp.status_code != 200:
                return []
            html_text = resp.text
    except Exception as exc:
        if logger:
            try:
                logger.debug(f"persona builder sogou search failed: {query}: {exc}")
            except Exception:
                pass
        return []

    if "安全验证" in _html_title(html_text) or "请输入验证码" in html_text:
        return []

    out: list[dict[str, Any]] = []
    title_re = re.compile(
        r'<h3\b[^>]*class="[^"]*vr-title[^"]*"[^>]*>.*?'
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?</h3>',
        flags=re.S | re.I,
    )
    matches = list(title_re.finditer(html_text))
    for index, match in enumerate(matches):
        url = urljoin("https://www.sogou.com/", html.unescape(match.group(1)).strip())
        title = _clean_html_fragment(match.group(2))
        if not url.startswith(("http://", "https://")) or not title:
            continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else min(
            len(html_text),
            match.end() + 2200,
        )
        block = html_text[match.end() : next_start]
        snippet_match = re.search(
            r'<div[^>]*class="[^"]*(?:fz-mid|text-layout|str_info)[^"]*"[^>]*>(.*?)</div>',
            block,
            flags=re.S | re.I,
        )
        snippet_source = snippet_match.group(1) if snippet_match else block[:1800]
        snippet = _clean_html_fragment(snippet_source)
        if not _source_relevant(
            work_title=work_title,
            character_name=character_name,
            title=title,
            summary=snippet,
            search_aliases=search_aliases,
        ):
            continue
        out.append(
            {
                "kind": "web_search",
                "query": query,
                "source": "搜狗",
                "title": title,
                "url": url,
                "summary": _clip_text(snippet, _SOURCE_SUMMARY_LIMIT),
                "confidence": 0.56,
            }
        )
        if len(out) >= max_results:
            break
    return out


async def _enrich_sources_with_pages(
    *,
    sources: list[dict[str, Any]],
    work_title: str,
    character_name: str,
    logger: Any,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    page_sources: list[dict[str, Any]] = []
    urls: list[tuple[str, str, str]] = []
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        title = str(source.get("title") or "")
        source_name = str(source.get("source") or "网页")
        if "just a moment" in title.lower() or "checking your connection" in title.lower():
            continue
        urls.append((url, title, source_name))
        if len(urls) >= _FETCHED_PAGE_LIMIT:
            break
    if not urls:
        return []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=10.0) as client:
        for url, fallback_title, source_name in urls:
            try:
                resp = await client.get(url)
                if resp.status_code >= 500 or not resp.text:
                    continue
                title = _html_title(resp.text) or fallback_title
                summary = _html_text_excerpt(resp.text, limit=_SOURCE_SUMMARY_LIMIT)
                normalized = _normalized_for_match(f"{title} {summary}")
                if "justamoment" in normalized or "checkingyourconnection" in normalized:
                    continue
                if not _source_relevant(
                    work_title=work_title,
                    character_name=character_name,
                    title=title,
                    summary=summary,
                    search_aliases=search_aliases,
                ):
                    continue
                page_sources.append(
                    {
                        "kind": "web_page",
                        "query": f"{work_title} {character_name}",
                        "source": f"{source_name}/正文",
                        "title": title,
                        "url": str(resp.url),
                        "summary": summary,
                        "confidence": 0.72,
                    }
                )
            except Exception as exc:
                if logger:
                    try:
                        logger.debug(f"persona builder page enrich failed: {url}: {exc}")
                    except Exception:
                        pass
    return page_sources


async def _gather_web_sources(
    *,
    work_title: str,
    character_name: str,
    logger: Any,
    search_aliases: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    try:
        from ...core.web_grounding import do_web_search
    except Exception:
        do_web_search = None

    base_queries = _persona_search_queries(work_title, character_name, search_aliases)
    queries = _dedupe_keep_order(
        [
            f"{base_queries[0]} 官方 角色介绍",
            f"{base_queries[0]} 设定 立绘 台词 萌点",
            *base_queries[1:4],
        ],
        limit=5,
    )
    direct_sources = await _gather_direct_page_sources(
        work_title=work_title,
        character_name=character_name,
        logger=logger,
        search_aliases=search_aliases,
    )
    sources: list[dict[str, Any]] = list(direct_sources)
    search_queries = queries[:2] if direct_sources else queries[:4]
    character_queries: list[str] = []
    work_aliases = _alias_values(search_aliases, "work_aliases", work_title)[:2]
    character_aliases = _alias_values(search_aliases, "character_aliases", character_name)[:5]
    for character in character_aliases:
        character_queries.append(character)
        for work in work_aliases:
            character_queries.append(f"{character} {work}")
    bing_queries = _dedupe_keep_order(
        [
            *character_queries,
            *base_queries,
            *_persona_site_search_queries(work_title, character_name, search_aliases),
        ],
        limit=10,
    )

    async def _search_one(query: str) -> list[dict[str, Any]]:
        if do_web_search is None:
            return []
        try:
            result = await asyncio.wait_for(
                do_web_search(
                    query,
                    context_hint="",
                    get_now=lambda: datetime.now(),
                    logger=logger,
                ),
                timeout=12,
            )
        except Exception as exc:
            return [
                {
                    "kind": "web_search_error",
                    "query": query,
                    "source": "联网搜索",
                    "title": query,
                    "url": "",
                    "summary": f"联网搜索失败：{_clip_text(exc, 300)}",
                    "confidence": 0,
                }
            ]
        text = _clip_text(result, _SOURCE_SUMMARY_LIMIT)
        parsed_sources = _parse_web_search_sources(query, result)
        if parsed_sources:
            return [
                source
                for source in parsed_sources
                if _source_relevant(
                    work_title=work_title,
                    character_name=character_name,
                    title=str(source.get("title") or ""),
                    summary=str(source.get("summary") or ""),
                    search_aliases=search_aliases,
                )
            ]
        if text:
            return [
                {
                    "kind": "web_search",
                    "query": query,
                    "source": "联网搜索",
                    "title": query,
                    "url": "",
                    "summary": text,
                    "confidence": 0.5,
                }
            ]
        return []

    if search_queries:
        generic_search_queries = bing_queries[:6]
        results = await asyncio.gather(
            *(_bing_search_sources(
                query=query,
                work_title=work_title,
                character_name=character_name,
                logger=logger,
                search_aliases=search_aliases,
            ) for query in bing_queries),
            *(_sogou_search_sources(
                query=query,
                work_title=work_title,
                character_name=character_name,
                logger=logger,
                search_aliases=search_aliases,
            ) for query in generic_search_queries),
            *(_search_one(query) for query in search_queries),
        )
        for items in results:
            sources.extend(items)
    sources.extend(
        await _enrich_sources_with_pages(
            sources=sources,
            work_title=work_title,
            character_name=character_name,
            logger=logger,
            search_aliases=search_aliases,
        )
    )
    useful = [s for s in sources if s.get("kind") not in {"web_search_error", "web_search_empty"}]
    return useful if useful else sources


async def _gather_persona_sources(
    *,
    runtime: Any,
    work_title: str,
    character_name: str,
) -> list[dict[str, Any]]:
    plugin_config = getattr(runtime, "plugin_config", None)
    logger = getattr(runtime, "logger", None)
    search_aliases = await _plan_search_aliases(
        runtime=runtime,
        work_title=work_title,
        character_name=character_name,
    )
    wiki_sources, web_sources = await asyncio.gather(
        _gather_wiki_sources(
            work_title=work_title,
            character_name=character_name,
            plugin_config=plugin_config,
            logger=logger,
            search_aliases=search_aliases,
        ),
        _gather_web_sources(
            work_title=work_title,
            character_name=character_name,
            logger=logger,
            search_aliases=search_aliases,
        ),
    )
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for source in [*wiki_sources, *web_sources]:
        key = _source_key(source)
        if key in seen:
            continue
        seen.add(key)
        merged.append(source)
    merged.sort(
        key=lambda source: _source_rank(
            source,
            work_title=work_title,
            character_name=character_name,
            search_aliases=search_aliases,
        ),
        reverse=True,
    )
    useful = [s for s in merged if s.get("kind") not in {"web_search_error", "web_search_empty"}]
    return (useful if useful else merged)[:12]


def _source_corpus(sources: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, source in enumerate(sources, start=1):
        title = source.get("title") or source.get("query") or f"资料 {index}"
        lines.append(
            "\n".join(
                [
                    f"[S{index}] 来源：{source.get('source') or '未知来源'}",
                    f"标题：{title}",
                    f"链接：{source.get('url') or '无直接链接'}",
                    f"查询：{source.get('query') or ''}",
                    f"摘要：{source.get('summary') or ''}",
                ]
            )
        )
    corpus = "\n\n".join(lines)
    return corpus[:_SOURCE_CORPUS_LIMIT]


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strip_yaml_fence(text: str) -> str:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:yaml|yml)?\s*(.*?)\s*```", raw, flags=re.S | re.I)
    if fenced:
        raw = fenced.group(1).strip()
    if raw.startswith("---"):
        raw = raw[3:].lstrip()
    return raw


def _validate_template_yaml(text: str) -> dict[str, Any]:
    raw = _strip_yaml_fence(text)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        parsed = yaml.safe_load(raw)
    except Exception as exc:
        return {
            "valid": False,
            "errors": [f"YAML 解析失败：{exc}"],
            "warnings": [],
            "keys": [],
            "data": None,
            "template": raw,
        }
    if not isinstance(parsed, dict):
        errors.append("YAML 顶层必须是对象/dict。")
        parsed = {}
    keys = list(parsed.keys()) if isinstance(parsed, dict) else []
    missing = [key for key in _REQUIRED_TEMPLATE_KEYS if key not in parsed]
    if missing:
        errors.append("缺少必需顶层字段：" + "、".join(missing))
    system_text = str(parsed.get("system") or "")
    if not isinstance(parsed.get("system"), str) or not system_text.strip():
        errors.append("system 必须是非空字符串。")
    else:
        if len(system_text.strip()) < 900:
            warnings.append("system 偏短：建议补足身份锚点、性格、群聊口吻、关系、边界与资料缺口。")
        missing_sections = [
            label
            for label, hints in _RECOMMENDED_SYSTEM_SECTIONS
            if not any(hint in system_text for hint in hints)
        ]
        if missing_sections:
            warnings.append("system 建议补充小节：" + "、".join(missing_sections))
        if any(re.search(pattern, system_text, flags=re.I) for pattern in _ASSISTANT_ROLE_PATTERNS):
            warnings.append("system 含助手/客服式身份措辞，建议改成角色本人在群里说话的边界。")
    input_text = parsed.get("input")
    if not isinstance(input_text, str) or not input_text.strip():
        errors.append("input 必须是非空字符串。")
    else:
        missing_placeholders = [p for p in _REQUIRED_INPUT_PLACEHOLDERS if p not in input_text]
        if missing_placeholders:
            errors.append("input 缺少插件运行占位符：" + "、".join(missing_placeholders))
        if "<output>" not in input_text or "<message>" not in input_text:
            warnings.append("input 未显式包含 <output>/<message> 输出格式，YAML 回复路径可能无法解析多消息。")
    if not isinstance(parsed.get("tts"), dict):
        warnings.append("tts 建议使用 voice/style/user_hint 对象。")
    for list_key in ("nick_name", "ack_phrases", "mute_keyword"):
        if list_key in parsed and not isinstance(parsed.get(list_key), list):
            errors.append(f"{list_key} 必须是列表。")
    ack_phrases = parsed.get("ack_phrases")
    if isinstance(ack_phrases, list) and len([item for item in ack_phrases if str(item or "").strip()]) < 3:
        warnings.append("ack_phrases 建议至少 3 条短促自然的接话短句，避免固定口癖反复出现。")
    nick_names = parsed.get("nick_name")
    if isinstance(nick_names, list) and not [item for item in nick_names if str(item or "").strip()]:
        warnings.append("nick_name 列表为空，建议加入角色常用称呼/别名。")
    status_text = parsed.get("status")
    if isinstance(status_text, str) and "{time}" in status_text:
        warnings.append("status 不建议直接放运行占位符；时间事实应由 input 注入。")
    if parsed.get("name") and not str(parsed.get("name")).strip():
        errors.append("name 不能为空。")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "keys": keys,
        "data": parsed,
        "template": raw,
    }


async def _repair_template_yaml(
    *,
    caller: Any,
    work_title: str,
    character_name: str,
    template: str,
    validation: dict[str, Any],
    reference_template: dict[str, Any],
) -> str:
    errors = "\n".join(f"- {item}" for item in validation.get("errors", []) or [])
    warnings = "\n".join(f"- {item}" for item in validation.get("warnings", []) or [])
    system = (
        "你是 NoneBot 拟人插件 YAML 模板修复器。"
        "优先修复 YAML 结构、字段、占位符和转义问题；如果存在质量提醒，也顺手补齐角色群聊底座。"
        "输出必须是完整 YAML，不能有代码围栏、解释或额外文本。"
    )
    reference = reference_template.get("content") or "（无当前模板参考）"
    user = f"""
作品：《{work_title}》
角色：{character_name}

当前 YAML 校验错误：
{errors or "- 未知错误"}

当前 YAML 质量提醒：
{warnings or "- 无"}

当前插件正在使用的人设 YAML 参考：
{reference}

待修复文本：
{template}

修复要求：
- 输出完整 YAML。
- 必须包含顶层字段：{", ".join(_REQUIRED_TEMPLATE_KEYS)}。
- input 必须保留占位符：{", ".join(_REQUIRED_INPUT_PLACEHOLDERS)}。
- input/system/status 必须适合 YAML 块标量。
- system 建议包含：角色身份、性格与行为模式、群聊说话方式、角色关系、资料冲突与缺口。
- ack_phrases 至少给 3 条短促自然的接话短句；不要写成“我来帮你查询”。
- 不能输出 Markdown、解释、代码围栏。
""".strip()
    return await _call_main_model(
        caller,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.05,
        use_builtin_search=False,
        timeout=60,
        purpose="persona_template_repair",
        stage_label="人设模板修复",
    )


async def _run_subagent(
    *,
    caller: Any,
    agent_name: str,
    focus: str,
    work_title: str,
    character_name: str,
    source_text: str,
    use_builtin_search: bool = True,
    purpose: str = "persona_template_research",
) -> dict[str, Any]:
    system = (
        "你是拟人插件 WebUI 的只读资料研究子agent。"
        + (
            "只能基于给出的资料和你能通过主模型内置搜索查到的可靠信息做交叉验证。"
            if use_builtin_search
            else "只能基于用户给出的描述做交叉验证；不要联网补设定，不要把描述外的内容编造成事实。"
        )
        + "不要编造设定；不确定就写入 conflicts 或 unknowns。"
        + "重点提取能迁移到群聊拟人 bot 的身份锚点、说话节奏、边界和关系称呼。"
        + "输出 JSON，不要输出寒暄。"
    )
    user = f"""
任务：为《{work_title}》的角色「{character_name}」构建人设模板前置研究。
你的研究重点：{focus}

资料：
{source_text or "（资料抓取为空，可以使用主模型内置搜索补充，但必须标注不确定性。）"}

请输出 JSON：
{{
  "agent": "{agent_name}",
  "focus": "{focus}",
  "facts": ["可验证事实，含来源编号如 S1"],
  "personality": ["性格/行为模式"],
  "visual_references": ["立绘、服装、外观、表情、代表性物件"],
  "relations": ["角色关系"],
  "catchphrases": ["口癖/常用表达"],
  "speech_style": ["句长、语气、称呼、吐槽方式、群聊可用的表达习惯"],
  "conversation_boundaries": ["哪些内容不应扮演、不能自称 AI/助手、证据不足时怎么说"],
  "moe_points": ["萌点/记忆点"],
  "story_setting": ["剧情定位/背景"],
  "conflicts": ["互相冲突或来源不足之处"],
  "unknowns": ["仍缺资料的点"]
}}
""".strip()
    text = await _call_main_model(
        caller,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.1,
        use_builtin_search=use_builtin_search,
        timeout=_RESEARCH_TIMEOUT_SECONDS,
        purpose=purpose,
        stage_label=f"{agent_name}研究",
    )
    parsed = _extract_json_object(text)
    return {
        "name": agent_name,
        "focus": focus,
        "report": parsed if parsed else {"raw": text},
        "raw": text,
    }


async def _synthesize_template(
    *,
    caller: Any,
    work_title: str,
    character_name: str,
    source_text: str,
    subagents: list[dict[str, Any]],
    reference_template: dict[str, Any],
) -> str:
    reports = json.dumps(
        [
            {
                "name": item.get("name"),
                "focus": item.get("focus"),
                "report": item.get("report"),
            }
            for item in subagents
        ],
        ensure_ascii=False,
        indent=2,
    )
    system = (
        "你是 NoneBot 拟人插件的人设 YAML 构建器，必须使用主模型做最终合成。"
        "目标是生成可直接写入 personification_prompt_path 的完整 YAML 文件。"
        "保留角色核心，不要把 bot 写成客服或助手；口吻应像群友。"
        "模板要控制角色扮演边界，但不要把正常聊天语义写成关键词触发规则。"
        "不要用关键词规则或触发词设计对话语义。"
        "输出必须只有 YAML 本体，不能有 Markdown 代码围栏、标题、解释、注释之外的额外文本。"
    )
    reference = reference_template.get("content") or "（未读取到当前模板；请按字段规范生成）"
    user = f"""
请基于资料和三个子agent交叉验证报告，为《{work_title}》的「{character_name}」生成插件内可直接使用的人设 YAML。

硬性要求：
- 输出完整 YAML，顶层字段必须包含：{", ".join(_REQUIRED_TEMPLATE_KEYS)}。
- 顶层结构、input 输出格式和占位符请参考“当前插件正在使用的人设 YAML”。
- input 必须保留占位符：{", ".join(_REQUIRED_INPUT_PLACEHOLDERS)}。
- system 内必须使用清晰小节，至少包含：
  - ## 角色身份与不可替换锚点
  - ## 性格与行为模式
  - ## 群聊说话方式
  - ## 角色关系与称呼
  - ## 视觉/立绘参考
  - ## 剧情定位与可用背景
  - ## 安全边界与扮演边界
  - ## 资料冲突与缺口
- 模板要适合作为群聊拟人 bot 的角色底座：自然、有边界、有群友感，不像在朗读百科。
- 不要写“我是 AI/助手/客服”。
- 不要让角色解释自己的运行机制、工具、检索、子agent、审查清单或思考过程。
- ack_phrases 给 4-8 条短促自然的接话短句，避免每句都像“收到/我来查”。
- status 写当前心情、动作和状态基线，不要塞长篇设定。
- 对证据不足的设定标注“待确认”，不要编造成事实。
- 资料冲突与缺口必须写入 system 的“## 资料冲突与缺口”小节，不要在 YAML 外附文字。
- status、input、system 使用 YAML 块标量；nick_name、ack_phrases、mute_keyword 使用列表。
- 最终输出只能是 YAML，不要代码围栏，不要额外说明。

当前插件正在使用的人设 YAML 参考：
{reference}

资料：
{source_text or "（资料抓取为空）"}

三个子agent报告：
{reports}
""".strip()
    return await _call_main_model(
        caller,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.25,
        use_builtin_search=False,
        timeout=_SYNTHESIS_TIMEOUT_SECONDS,
        purpose="persona_template_synthesis",
        stage_label="人设模板生成",
    )


def _custom_persona_source_text(spec: dict[str, str]) -> str:
    rows = [
        ("人设名称", spec.get("persona_name", "")),
        ("性别", spec.get("gender", "")),
        ("性格", spec.get("personality", "")),
        ("特点", spec.get("traits", "")),
        ("爱好", spec.get("hobbies", "")),
        ("长文描述", spec.get("description", "")),
    ]
    return "\n".join(f"{label}：{str(value or '').strip() or '未填写'}" for label, value in rows)


async def _synthesize_custom_template(
    *,
    caller: Any,
    spec: dict[str, str],
    source_text: str,
    subagents: list[dict[str, Any]],
    reference_template: dict[str, Any],
) -> str:
    reports = json.dumps(
        [{"name": item.get("name"), "focus": item.get("focus"), "report": item.get("report")} for item in subagents],
        ensure_ascii=False,
        indent=2,
    )
    persona_name = spec["persona_name"]
    system = (
        "你是 NoneBot 拟人插件的人设 YAML 构建器。"
        "这次不是从已有作品角色检索资料，而是根据用户自定义描述创建一个原创人设。"
        "用户描述是最高优先级来源；不得联网补设定，不得把没写的经历、作品出处、真实身份编造成事实。"
        "输出必须只有 YAML 本体，不能有 Markdown 代码围栏、标题或解释。"
    )
    reference = reference_template.get("content") or "（未读取到当前模板；请按字段规范生成）"
    user = f"""
请根据用户给出的原创人设描述，生成插件内可直接使用的人设 YAML：{persona_name}

硬性要求：
- 输出完整 YAML，顶层字段必须包含：{", ".join(_REQUIRED_TEMPLATE_KEYS)}。
- 顶层结构、input 输出格式和占位符请参考“当前插件正在使用的人设 YAML”。
- input 必须保留占位符：{", ".join(_REQUIRED_INPUT_PLACEHOLDERS)}。
- system 内必须使用清晰小节，至少包含：
  - ## 角色身份与不可替换锚点
  - ## 性格与行为模式
  - ## 群聊说话方式
  - ## 关系与称呼
  - ## 兴趣爱好与日常锚点
  - ## 安全边界与扮演边界
  - ## 资料冲突与缺口
- 自定义人设要鲜活、能在群聊里自然插话；不要写成客服、助手、设定说明书或关键词触发规则。
- 性别、性格、特点、爱好和长文描述都要进入 system，但要转成角色本人可执行的说话/行为约束。
- ack_phrases 给 4-8 条短促自然的接话短句，避免“收到/我来查”。
- status 写当前心情、动作和状态基线，不要塞长篇设定。
- 不确定、用户没写、互相冲突的点写入“## 资料冲突与缺口”，不要补成事实。
- 最终输出只能是 YAML，不要代码围栏，不要额外说明。

当前插件正在使用的人设 YAML 参考：
{reference}

用户描述资料：
{source_text}

三个子agent交叉验证报告：
{reports}
""".strip()
    return await _call_main_model(
        caller,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.25,
        use_builtin_search=False,
        timeout=_SYNTHESIS_TIMEOUT_SECONDS,
        purpose="persona_template_custom_synthesis",
        stage_label="自定义人设模板生成",
    )


async def _run_custom_persona_template_build(
    *,
    runtime: Any,
    spec: dict[str, str],
    actor: str = "",
    source: str = "webui",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    caller = _main_ai_caller(runtime)
    if caller is None:
        raise RuntimeError("主模型调用器未就绪")
    progress = progress or _default_progress
    started = time.time()
    persona_name = spec["persona_name"]
    source_text = _custom_persona_source_text(spec)
    await progress("custom_research", "正在运行 3 个自定义人设子agent...", 35)
    focus_items = [
        ("身份性格子agent", "身份锚点、性别表达、性格底色、不可替换特征"),
        ("说话关系子agent", "群聊说话节奏、称呼方式、插话边界、亲疏表达"),
        ("日常校验子agent", "爱好、作息/日常锚点、资料冲突、可执行边界"),
    ]
    subagents = await asyncio.gather(
        *[
            _run_subagent(
                caller=caller,
                agent_name=name,
                focus=focus,
                work_title="自定义人设",
                character_name=persona_name,
                source_text=source_text,
                use_builtin_search=False,
                purpose="persona_template_custom_research",
            )
            for name, focus in focus_items
        ]
    )
    reference_template = _configured_template_reference(runtime)
    await progress("template_synthesis", "正在按插件 YAML 结构合成原创人设...", 78)
    template = await _synthesize_custom_template(
        caller=caller,
        spec=spec,
        source_text=source_text,
        subagents=list(subagents),
        reference_template=reference_template,
    )
    validation = _validate_template_yaml(template)
    if not validation.get("valid"):
        await progress("template_repair", "正在修复模板结构与占位符...", 88)
        repaired = await _repair_template_yaml(
            caller=caller,
            work_title="自定义人设",
            character_name=persona_name,
            template=template,
            validation=validation,
            reference_template=reference_template,
        )
        repaired_validation = _validate_template_yaml(repaired)
        if repaired_validation.get("valid"):
            template = repaired_validation["template"]
            validation = repaired_validation
        else:
            template = _strip_yaml_fence(template)
    await progress("finalize", "正在整理输出结果...", 95)
    conflicts: list[str] = []
    for item in subagents:
        report = item.get("report") if isinstance(item, dict) else {}
        raw_conflicts = report.get("conflicts", []) if isinstance(report, dict) else []
        if isinstance(raw_conflicts, list):
            conflicts.extend(str(x) for x in raw_conflicts if str(x).strip())
    sources = [
        {
            "kind": "custom_description",
            "source": "用户描述",
            "title": persona_name,
            "summary": _clip_text(source_text, _SOURCE_SUMMARY_LIMIT),
            "confidence": 1.0,
        }
    ]
    result = {
        "success": True,
        "mode": "custom",
        "work_title": "自定义人设",
        "character_name": persona_name,
        "model_role": "configured_main",
        "duration_ms": int((time.time() - started) * 1000),
        "sources": sources,
        "subagents": list(subagents),
        "conflicts": conflicts[:20],
        "template": validation.get("template") or _strip_yaml_fence(template),
        "template_valid": bool(validation.get("valid")),
        "template_errors": list(validation.get("errors") or [])[:20],
        "template_warnings": list(validation.get("warnings") or [])[:20],
        "template_keys": list(validation.get("keys") or []),
        "template_reference": reference_template,
    }
    record = record_persona_template_result(result, actor=actor, source=source)
    result["history_record"] = summarize_persona_template_record(record)
    export_path = write_persona_template_export_file(record, plugin_config=getattr(runtime, "plugin_config", None))
    result["export_path"] = str(export_path)
    await progress("done", "自定义人设模板已完成。", 100)
    return result


def build_persona_template_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/persona-template", tags=["persona-template"])

    def _parse_build_body(body: dict) -> tuple[str, str]:
        work_title = str(body.get("work_title", "") or "").strip()
        character_name = str(body.get("character_name", "") or "").strip()
        if not work_title or not character_name:
            raise HTTPException(status_code=400, detail="作品名和角色名都不能为空")
        if len(work_title) > 120 or len(character_name) > 80:
            raise HTTPException(status_code=400, detail="作品名或角色名过长")
        if _main_ai_caller(runtime) is None:
            raise HTTPException(status_code=503, detail="主模型调用器未就绪")
        return work_title, character_name

    def _parse_custom_build_body(body: dict) -> dict[str, str]:
        persona_name = str(body.get("persona_name") or body.get("character_name") or "").strip()
        gender = str(body.get("gender", "") or "").strip()
        personality = str(body.get("personality", "") or "").strip()
        traits = str(body.get("traits", "") or "").strip()
        hobbies = str(body.get("hobbies", "") or "").strip()
        description = str(body.get("description", "") or "").strip()
        if not persona_name:
            raise HTTPException(status_code=400, detail="人设名称不能为空")
        if len(persona_name) > 80:
            raise HTTPException(status_code=400, detail="人设名称过长")
        if not any([gender, personality, traits, hobbies, description]):
            raise HTTPException(status_code=400, detail="请至少填写性别、性格、特点、爱好或长文描述中的一项")
        if len(description) > 6000:
            raise HTTPException(status_code=400, detail="长文描述过长，最多 6000 字")
        if _main_ai_caller(runtime) is None:
            raise HTTPException(status_code=503, detail="主模型调用器未就绪")
        return {
            "persona_name": persona_name,
            "gender": gender[:120],
            "personality": personality[:600],
            "traits": traits[:800],
            "hobbies": hobbies[:800],
            "description": description,
        }

    def _is_custom_build(body: dict) -> bool:
        return str(body.get("mode", "") or "").strip().lower() in {"custom", "description", "原创", "自定义"}

    @router.get("/history")
    async def history(
        limit: int = 20,
        work_title: str = "",
        character_name: str = "",
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        records = list_persona_template_records(
            limit=limit,
            work_title=work_title,
            character_name=character_name,
        )
        return {
            "records": [summarize_persona_template_record(record) for record in records],
            "total": len(records),
        }

    @router.get("/history/{record_id}")
    async def history_detail(
        record_id: str,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        records = list_persona_template_records(limit=100)
        for record in records:
            if str(record.get("record_id", "")) == str(record_id):
                return record
        raise HTTPException(status_code=404, detail="未找到该人设构建历史记录")

    @router.post("/apply")
    async def apply_template(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        record_id = str(body.get("record_id", "") or "").strip()
        result = body.get("result") if isinstance(body.get("result"), dict) else None
        if record_id:
            records = list_persona_template_records(limit=100)
            for record in records:
                if str(record.get("record_id", "")) == record_id:
                    result = record.get("result") if isinstance(record.get("result"), dict) else None
                    break
        if not isinstance(result, dict):
            raise HTTPException(status_code=400, detail="缺少可应用的人设构建结果")
        try:
            applied = _apply_persona_template(runtime, result)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        webui_audit_log.record(
            action="persona_template_apply",
            qq=admin.qq,
            device_id=admin.device_id,
            target=f"{result.get('work_title', '')}/{result.get('character_name', '')}",
            ip_hash=get_client_ip(request),
            detail={"path": applied.get("path", ""), "record_id": record_id},
            outcome="ok",
        )
        return applied

    @router.post("/build-task")
    async def build_template_task(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        is_custom = _is_custom_build(body)
        custom_spec = _parse_custom_build_body(body) if is_custom else {}
        work_title, character_name = (
            ("自定义人设", custom_spec["persona_name"]) if is_custom else _parse_build_body(body)
        )
        task = _PersonaTemplateTask(
            task_id=uuid.uuid4().hex,
            work_title=work_title,
            character_name=character_name,
            created_at=time.time(),
        )
        async with _TASK_LOCK:
            _cleanup_finished_tasks()
            _TASKS[task.task_id] = task

        async def _progress(stage: str, message: str, percent: int) -> None:
            await _set_task_progress(
                task,
                stage=stage,
                message=message,
                progress=percent,
                status="running",
            )

        async def _runner() -> None:
            try:
                await _set_task_progress(
                    task,
                    stage="starting",
                    message="正在启动人设构建...",
                    progress=3,
                    status="running",
                )
                if is_custom:
                    task.result = await _run_custom_persona_template_build(
                        runtime=runtime,
                        spec=custom_spec,
                        actor=admin.qq,
                        source="webui",
                        progress=_progress,
                    )
                else:
                    task.result = await _run_persona_template_build(
                        runtime=runtime,
                        work_title=work_title,
                        character_name=character_name,
                        actor=admin.qq,
                        source="webui",
                        progress=_progress,
                    )
                await _set_task_progress(
                    task,
                    stage="done",
                    message="人设模板已完成。",
                    progress=100,
                    status="done",
                )
                webui_audit_log.record(
                    action="persona_template_build",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=f"{work_title}/{character_name}",
                    ip_hash=get_client_ip(request),
                    detail={
                        "source_count": len((task.result or {}).get("sources") or []),
                        "subagent_count": len((task.result or {}).get("subagents") or []),
                        "mode": "task",
                        "build_mode": "custom" if is_custom else "source",
                    },
                    outcome="ok",
                )
            except Exception as exc:
                task.error = _clip_text(exc, 500)
                await _set_task_progress(
                    task,
                    stage="error",
                    message=f"构建失败：{task.error}",
                    progress=max(1, task.progress),
                    status="error",
                )
                webui_audit_log.record(
                    action="persona_template_build",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=f"{work_title}/{character_name}",
                    ip_hash=get_client_ip(request),
                    detail={"error": task.error, "mode": "task", "build_mode": "custom" if is_custom else "source"},
                    outcome="error",
                )

        asyncio.create_task(_runner())
        return task.public()

    @router.get("/tasks/{task_id}")
    async def build_task_status(
        task_id: str,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        task = _get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="人设构建任务不存在或已过期")
        return task.public()

    @router.post("/build")
    async def build_template(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        is_custom = _is_custom_build(body)
        custom_spec = _parse_custom_build_body(body) if is_custom else {}
        work_title, character_name = (
            ("自定义人设", custom_spec["persona_name"]) if is_custom else _parse_build_body(body)
        )
        try:
            if is_custom:
                result = await _run_custom_persona_template_build(
                    runtime=runtime,
                    spec=custom_spec,
                    actor=admin.qq,
                    source="webui",
                )
            else:
                result = await _run_persona_template_build(
                    runtime=runtime,
                    work_title=work_title,
                    character_name=character_name,
                    actor=admin.qq,
                    source="webui",
                )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        webui_audit_log.record(
            action="persona_template_build",
            qq=admin.qq,
            device_id=admin.device_id,
            target=f"{work_title}/{character_name}",
            ip_hash=get_client_ip(request),
            detail={
                "source_count": len(result.get("sources") or []),
                "subagent_count": len(result.get("subagents") or []),
                "mode": "sync",
                "build_mode": "custom" if is_custom else "source",
            },
            outcome="ok",
        )
        return result

    return router
