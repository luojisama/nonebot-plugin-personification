"""WebUI 功能体检：对各模块做**实际调用探测**，而非仅检查配置。

与单纯"配置是否填了"不同，这里尽量真实地跑一次：
- 模型：对每个 provider 真发一次最小调用，看是否返回/被拦截/超时；
- 存储/记忆/画像：执行一次真实读操作；
- TTS/QQ 空间/联网搜索：对目标地址做真实连通探测；
- 协议端：真实调用 get_version_info。

status：ok 绿 / warn 黄 / error 红 / disabled 灰 / info 中性。
所有探测 never-raise、带超时、可并发；单项异常收敛为 error。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

_OK = "ok"
_WARN = "warn"
_ERROR = "error"
_DISABLED = "disabled"
_INFO = "info"

_PROVIDER_PROBE_TIMEOUT = 20
_HTTP_PROBE_TIMEOUT = 8
_PROBE_MESSAGES = [
    {"role": "system", "content": "回复一个字：好"},
    {"role": "user", "content": "在吗"},
]


def _check(key: str, label: str, status: str, detail: str = "", hint: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "detail": detail, "hint": hint}


def _get(cfg: Any, name: str, default: Any = None) -> Any:
    return getattr(cfg, name, default)


def _truthy_str(cfg: Any, name: str) -> bool:
    return bool(str(_get(cfg, name, "") or "").strip())


def _safe_list(fn) -> list[dict[str, Any]]:
    try:
        return list(fn())
    except Exception as exc:  # pragma: no cover
        return [_check("internal_error", "检查异常", _ERROR, detail=str(exc)[:200])]


async def _http_reachable(url: str, *, timeout: int = _HTTP_PROBE_TIMEOUT) -> tuple[bool, str]:
    """对 url 做一次真实 HTTP 探测；任何 HTTP 响应都算连通，连接异常算不通。"""
    url = str(url or "").strip()
    if not url:
        return False, "地址为空"
    if not url.startswith("http"):
        url = "https://" + url
    try:
        from .runtime_state import get_shared_http_client

        client = get_shared_http_client()
        resp = await asyncio.wait_for(client.get(url), timeout=timeout)
        return True, f"HTTP {resp.status_code}"
    except asyncio.TimeoutError:
        return False, f"超时（>{timeout}s）"
    except Exception as exc:
        return False, str(exc)[:120]


# ──────────────────────── 实时探测各模块 ────────────────────────

def _core_checks(cfg: Any, superusers: set[str]) -> list[dict[str, Any]]:
    checks = []
    enabled = bool(_get(cfg, "personification_global_enabled", True))
    checks.append(_check("global_enabled", "插件总开关", _OK if enabled else _WARN,
                         detail="已开启" if enabled else "已关闭：插件不响应任何消息"))
    n = len([s for s in (superusers or set()) if str(s).strip()])
    checks.append(_check("superusers", "管理员 (SUPERUSERS)", _OK if n else _ERROR,
                         detail=f"已配置 {n} 个" if n else "未配置：无人能登录 WebUI",
                         hint="" if n else "在 .env.prod 配置 SUPERUSERS"))
    return checks


async def _probe_provider(cfg: Any, provider: dict, *, key: str, label: str) -> dict:
    """对单个 provider 真发一次最小调用，返回带详细原因的检查项。"""
    from .ai_routes import build_single_provider_caller
    from .safety_filter import detect_api_block

    started = time.monotonic()
    try:
        caller = build_single_provider_caller(cfg, provider)
        resp = await asyncio.wait_for(
            caller.chat_with_tools(messages=_PROBE_MESSAGES, tools=[], use_builtin_search=False),
            timeout=_PROVIDER_PROBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return _check(key, label, _ERROR, detail=f"调用超时（>{_PROVIDER_PROBE_TIMEOUT}s）",
                      hint="检查该 provider 的网络/代理或换源")
    except Exception as exc:
        return _check(key, label, _ERROR, detail=f"调用失败：{str(exc)[:200]}",
                      hint="多为 api_key 错误、地址不对、额度耗尽或网络不通")
    ms = int((time.monotonic() - started) * 1000)
    blocked = detect_api_block(resp)
    content = str(getattr(resp, "content", "") or "").strip()
    model = str(provider.get("model", "") or "")
    if blocked:
        return _check(key, label, _ERROR, detail=f"被安全策略拦截：{blocked}（{model}，{ms}ms）",
                      hint="该 provider 对内容审查严格，建议换 provider 或软化提示词")
    if content:
        return _check(key, label, _OK, detail=f"调用正常（{model}，{ms}ms，返回 {len(content)} 字）")
    return _check(key, label, _WARN, detail=f"返回空内容（{model}，{ms}ms）",
                  hint="模型可能拒答或上下文为空，建议人工用「模型测试」复核")


async def _probe_provider_vision(cfg: Any, provider: dict, *, key: str, label: str, route_name: str = "") -> dict:
    """对单个 provider 真发一次图片理解探测。"""
    from .ai_routes import build_single_provider_caller
    from .message_parts import build_user_message_content
    from .visual_capabilities import (
        _PROBE_IMAGE_DATA_URL,
        error_indicates_vision_unavailable,
        heuristic_supports_vision,
        _probe_response_matches_expected,
        set_visual_capability,
    )

    api_type = str(provider.get("api_type", "") or "")
    model = str(provider.get("model", "") or "")
    if not heuristic_supports_vision(api_type, model):
        return _check(
            key,
            label,
            _WARN,
            detail=f"当前路由未判定为可直传图片（{api_type}:{model or 'unset'}）",
            hint="若该模型实际支持视觉，请确认模型名、路由配置，或运行启动/体检视觉探测刷新缓存",
        )
    messages = [
        {
            "role": "user",
            "content": build_user_message_content(
                text="请只按左上、右上、左下、右下顺序回答这张四宫格图片的颜色，只输出四个汉字。",
                image_urls=[_PROBE_IMAGE_DATA_URL],
                image_detail="low",
            ),
        }
    ]
    started = time.monotonic()
    try:
        caller = build_single_provider_caller(cfg, provider)
        resp = await asyncio.wait_for(
            caller.chat_with_tools(messages=messages, tools=[], use_builtin_search=False),
            timeout=_PROVIDER_PROBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return _check(key, label, _ERROR, detail=f"视觉调用超时（>{_PROVIDER_PROBE_TIMEOUT}s）",
                      hint="检查该 provider 的视觉模型、网络、代理或额度")
    except Exception as exc:
        if route_name and error_indicates_vision_unavailable(exc):
            set_visual_capability(route_name, api_type, model, False, source="diagnostics", detail=str(exc))
        return _check(key, label, _ERROR, detail=f"视觉调用失败：{str(exc)[:200]}",
                      hint="多为模型不支持图片、api_type/model 不匹配、凭证或网络问题")
    ms = int((time.monotonic() - started) * 1000)
    content = str(getattr(resp, "content", "") or "").strip()
    supported = not bool(getattr(resp, "vision_unavailable", False)) and _probe_response_matches_expected(content)
    if route_name:
        set_visual_capability(
            route_name,
            api_type,
            model,
            supported,
            source="diagnostics",
            detail=content[:80] or str(getattr(resp, "finish_reason", "") or ""),
        )
    if supported:
        return _check(key, label, _OK, detail=f"图片理解正常（{model}，{ms}ms）")
    if bool(getattr(resp, "vision_unavailable", False)):
        return _check(key, label, _ERROR, detail=f"模型拒绝图片输入（{model}，{ms}ms）",
                      hint="改用支持视觉的模型，或启用视觉摘要 fallback")
    return _check(key, label, _ERROR, detail=f"图片理解结果不符合预期：{content[:80] or '空'}（{model}，{ms}ms）",
                  hint="该模型可能只支持文本，或上游没有正确接收 image_url")


async def _model_checks(cfg: Any, bundle: Any, logger: Any) -> list[dict[str, Any]]:
    from .ai_routes import list_primary_providers

    providers = list_primary_providers(cfg, logger) or []
    if not providers:
        return [_check("api_pools", "模型 Provider 池", _ERROR,
                       detail="未配置任何 provider，主回复模型不可用",
                       hint="在「模型路由」添加至少一个 provider")]

    async def _one(provider: dict) -> dict:
        name = str(provider.get("name") or f"{provider.get('api_type')}:{provider.get('model')}")
        return await _probe_provider(cfg, provider, key=f"model_{name}", label=f"模型调用 · {name}")

    return list(await asyncio.gather(*[_one(p) for p in providers]))


# 各 LLM 子角色（独立 api_* 字段）：(展示名, key, 字段前缀, model 字段名)
_LLM_SUBROLES: tuple[tuple[str, str, str, str], ...] = (
    ("画像模型", "persona", "personification_persona_api", "personification_persona_model"),
    ("群风格模型", "style", "personification_style_api", "personification_style_api_model"),
    ("视觉打标模型", "labeler", "personification_labeler_api", "personification_labeler_model"),
    ("上下文压缩模型", "compress", "personification_compress_api", "personification_compress_model"),
)


async def _llm_subconfig_checks(cfg: Any) -> list[dict[str, Any]]:
    """对画像/风格/视觉打标/压缩等独立子模型做真实调用探测；未单独配置则标注复用主模型。"""
    def _subrole_is_explicitly_configured(key: str, api_type: str, api_url: str, api_key: str, model: str) -> bool:
        if api_url or api_key or model:
            return True
        if not api_type:
            return False
        if key == "labeler" and api_type == "openai":
            return False
        return True

    async def _one(name: str, key: str, prefix: str, model_field: str) -> dict:
        api_type = str(_get(cfg, f"{prefix}_type", "") or "").strip()
        api_url = str(_get(cfg, f"{prefix}_url", "") or "").strip()
        api_key = str(_get(cfg, f"{prefix}_key", "") or "").strip()
        model = str(_get(cfg, model_field, "") or "").strip()
        if not _subrole_is_explicitly_configured(key, api_type, api_url, api_key, model):
            return _check(f"sub_{key}", name, _INFO, detail="未单独配置，复用主模型（随主模型一并验证）")
        provider = {
            "name": key, "api_type": api_type or "openai", "api_url": api_url,
            "api_key": api_key, "model": model, "enabled": True, "priority": 1,
        }
        if key == "labeler":
            return await _probe_provider_vision(cfg, provider, key=f"sub_{key}", label=f"{name}视觉调用", route_name="sub_labeler")
        return await _probe_provider(cfg, provider, key=f"sub_{key}", label=f"{name}调用")

    return list(await asyncio.gather(*[_one(*r) for r in _LLM_SUBROLES]))


async def _vision_checks(cfg: Any, bundle: Any, logger: Any) -> list[dict[str, Any]]:
    from .ai_routes import list_primary_providers
    from .visual_capabilities import VISUAL_ROUTE_AGENT, VISUAL_ROUTE_REPLY_PLAIN, VISUAL_ROUTE_REPLY_YAML, VISUAL_ROUTE_VISION

    checks: list[dict[str, Any]] = []
    providers = list_primary_providers(cfg, logger) or []
    if not providers:
        return [_check("vision_primary", "主路由视觉", _ERROR, detail="未配置主 provider，无法探测视觉路径")]
    primary = dict(providers[0])
    for route_name, label in (
        (VISUAL_ROUTE_REPLY_PLAIN, "普通回复直传图片"),
        (VISUAL_ROUTE_REPLY_YAML, "YAML 回复直传图片"),
        (VISUAL_ROUTE_AGENT, "Agent 工具循环直传图片"),
    ):
        checks.append(
            await _probe_provider_vision(
                cfg,
                primary,
                key=f"vision_{route_name}",
                label=label,
                route_name=route_name,
            )
        )
    runtime_inner = None
    deps = getattr(bundle, "reply_processor_deps", None) if bundle is not None else None
    if deps is not None:
        runtime_inner = getattr(deps, "runtime", None)
    fallback = getattr(runtime_inner, "vision_caller", None) if runtime_inner is not None else None
    if fallback is None:
        checks.append(_check("vision_fallback", "视觉摘要 fallback", _WARN,
                             detail="vision_caller 未就绪；不支持直传图片的路由只能看到 [图片] 占位",
                             hint="配置全局 fallback 或视觉 fallback provider/model"))
    else:
        from .visual_capabilities import _PROBE_IMAGE_DATA_URL, _probe_response_matches_expected

        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                fallback.describe("请只回答四宫格颜色顺序，输出四个汉字。", _PROBE_IMAGE_DATA_URL),
                timeout=_PROVIDER_PROBE_TIMEOUT,
            )
            ms = int((time.monotonic() - started) * 1000)
            ok = _probe_response_matches_expected(str(result or ""))
            checks.append(_check(
                "vision_fallback",
                "视觉摘要 fallback",
                _OK if ok else _WARN,
                detail=(f"fallback 图片理解正常（{ms}ms）" if ok else f"fallback 返回异常：{str(result or '')[:80]}（{ms}ms）"),
                hint="" if ok else "若主路由不支持图片，视觉摘要质量可能受影响",
            ))
        except Exception as exc:
            checks.append(_check("vision_fallback", "视觉摘要 fallback", _ERROR,
                                 detail=f"fallback 调用失败：{str(exc)[:160]}",
                                 hint="检查 fallback/vision provider、模型和网络"))
    if checks:
        error_count = sum(1 for c in checks if c.get("status") == _ERROR)
        if error_count and any(c.get("status") == _OK for c in checks):
            checks.append(_check("vision_mode", "视觉降级模式", _WARN,
                                 detail="部分直传路径不可用，但仍有可用视觉路径；运行时会尝试摘要 fallback"))
    return checks


def _db_checks(cfg: Any) -> list[dict[str, Any]]:
    started = time.monotonic()
    try:
        from .db import connect_sync

        with connect_sync() as conn:
            conn.execute("SELECT 1").fetchone()
        ms = int((time.monotonic() - started) * 1000)
        return [_check("db", "主数据库读写", _OK, detail=f"查询正常（{ms}ms）")]
    except Exception as exc:
        return [_check("db", "主数据库读写", _ERROR, detail=f"查询失败：{str(exc)[:160]}")]


def _memory_checks(cfg: Any, bundle: Any) -> list[dict[str, Any]]:
    if not bool(_get(cfg, "personification_memory_enabled", True)):
        return [_check("memory", "记忆系统", _DISABLED, detail="已关闭")]
    store = getattr(bundle, "memory_store", None) if bundle is not None else None
    if store is None:
        return [_check("memory", "记忆系统", _WARN, detail="已启用但 memory_store 未就绪")]
    try:
        groups = store.list_groups()  # 真实读
        return [_check("memory", "记忆系统", _OK, detail=f"读操作正常，已知 {len(groups)} 个群空间")]
    except Exception as exc:
        return [_check("memory", "记忆系统", _ERROR, detail=f"读操作失败：{str(exc)[:140]}")]


def _persona_checks(cfg: Any, bundle: Any) -> list[dict[str, Any]]:
    if not bool(_get(cfg, "personification_persona_enabled", True)):
        return [_check("persona", "用户画像", _DISABLED, detail="已关闭")]
    store = getattr(bundle, "persona_store", None) if bundle is not None else None
    if store is None:
        return [_check("persona", "用户画像", _WARN, detail="已启用但 persona_store 未就绪")]
    try:
        store.get_persona("__healthcheck__")  # 真实读（不存在返回 None，不报错即通路正常）
        return [_check("persona", "用户画像", _OK, detail="画像存储读操作正常")]
    except Exception as exc:
        return [_check("persona", "用户画像", _ERROR, detail=f"读操作失败：{str(exc)[:140]}")]


def _group_checks(cfg: Any) -> list[dict[str, Any]]:
    from ..utils import is_group_whitelisted, load_group_configs, load_whitelist

    config_wl = [str(g) for g in (_get(cfg, "personification_whitelist", []) or [])]
    dynamic_wl = [str(g) for g in (load_whitelist() or [])]
    gconfigs = load_group_configs() or {}
    if not isinstance(gconfigs, dict):
        gconfigs = {}
    all_ids = set(config_wl) | set(dynamic_wl) | {str(k) for k in gconfigs}
    enabled = [g for g in all_ids if is_group_whitelisted(g, config_wl)]
    checks = [_check(
        "group_whitelist", "群聊白名单",
        _OK if enabled else _WARN,
        detail=f"{len(enabled)} 个群已启用拟人回复" if enabled else "没有任何已启用的群：bot 在群里不会说话",
        hint="" if enabled else "在「群开关」打开目标群",
    )]
    prob = float(_get(cfg, "personification_probability", 0.0) or 0.0)
    checks.append(_check("probability", "随机插话概率", _OK if prob > 0 else _WARN, detail=f"probability={prob}"))
    return checks


def _sticker_checks(cfg: Any) -> list[dict[str, Any]]:
    try:
        from .sticker_cache import get_sticker_files

        files = get_sticker_files(_get(cfg, "personification_sticker_path", None)) or []
        n = len(files)
        return [_check("stickers", "表情包库", _OK if n else _WARN,
                       detail=f"扫描到 {n} 张表情" if n else "目录为空或不存在",
                       hint="" if n else "在「表情包」上传或检查 sticker_path")]
    except Exception as exc:
        return [_check("stickers", "表情包库", _ERROR, detail=str(exc)[:140])]


async def _tts_checks(cfg: Any) -> list[dict[str, Any]]:
    if not bool(_get(cfg, "personification_tts_enabled", False)):
        return [_check("tts", "TTS 语音", _DISABLED, detail="已关闭")]
    url = str(_get(cfg, "personification_tts_api_url", "") or "")
    if not url or not _truthy_str(cfg, "personification_tts_api_key"):
        return [_check("tts", "TTS 语音", _ERROR, detail="已启用但缺少 api_url / api_key")]
    ok, info = await _http_reachable(url)
    return [_check("tts", "TTS 语音", _OK if ok else _ERROR,
                   detail=f"接口连通（{info}）" if ok else f"接口不通：{info}",
                   hint="" if ok else "检查 tts_api_url 与网络/代理")]


async def _qzone_checks(cfg: Any) -> list[dict[str, Any]]:
    if not bool(_get(cfg, "personification_qzone_enabled", False)):
        return [_check("qzone", "QQ 空间", _DISABLED, detail="已关闭")]
    if not _truthy_str(cfg, "personification_qzone_cookie"):
        return [_check("qzone", "QQ 空间", _ERROR, detail="已启用但缺少 Cookie")]
    ok, info = await _http_reachable("https://user.qzone.qq.com")
    return [_check("qzone", "QQ 空间", _OK if ok else _WARN,
                   detail=f"已配置 Cookie，空间服务可达（{info}）" if ok else f"空间服务探测失败：{info}",
                   hint="" if ok else "Cookie 失效需重新抓取；或网络受限")]


async def _search_checks(cfg: Any) -> list[dict[str, Any]]:
    tool = bool(_get(cfg, "personification_tool_web_search_enabled", True))
    builtin = bool(_get(cfg, "personification_model_builtin_search_enabled", True))
    if not (tool or builtin):
        return [_check("web_search", "联网搜索", _DISABLED, detail="外部与内置搜索均关闭")]
    if builtin and not tool:
        return [_check("web_search", "联网搜索", _OK, detail="使用模型内置搜索（随模型调用验证）")]

    # 优先探测国内可达的源（Bing 国内站），再带上已配置的免配置搜索引擎代表站点；
    # 任意一个可达即视为联网搜索可用，避免被墙的 duckduckgo/wikipedia 误报失败。
    engines = [str(e).strip().lower() for e in (_get(cfg, "personification_free_search_engines", []) or [])]
    engine_hosts = {
        "wikipedia": "https://zh.wikipedia.org",
        "duckduckgo": "https://duckduckgo.com",
        "searxng": (list(_get(cfg, "personification_searxng_instances", []) or []) or ["https://searx.be"])[0],
    }
    targets = [("Bing 国内", "https://cn.bing.com")]
    for e in engines:
        if e in engine_hosts:
            targets.append((e, engine_hosts[e]))
    if _truthy_str(cfg, "personification_web_proxy"):
        return [_check("web_search", "联网搜索", _INFO,
                       detail=f"已配置 web_proxy，外部搜索经代理访问；候选源：{', '.join(t[0] for t in targets)}")]

    results = await asyncio.gather(*[_http_reachable(url) for _, url in targets])
    reachable = [targets[i][0] for i, (ok, _info) in enumerate(results) if ok]
    if reachable:
        return [_check("web_search", "联网搜索", _OK, detail=f"可用源：{', '.join(reachable)}")]
    return [_check("web_search", "联网搜索", _WARN,
                   detail="所有候选搜索源均不可达（可能被墙或断网）",
                   hint="配置 web_proxy，或在 free_search_engines 改用国内可达源（如自建 searxng）")]


def _skill_checks(cfg: Any, bundle: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    registry = getattr(bundle, "tool_registry", None) if bundle is not None else None
    if registry is None:
        checks.append(_check("tools", "已加载工具", _WARN, detail="工具注册表未就绪"))
    else:
        try:
            n = len(list(registry.all()))
        except Exception:
            n = 0
        checks.append(_check("tools", "已加载工具", _OK if n else _WARN, detail=f"{n} 个工具已注册就绪"))
    # MCP（仅 stdio）
    try:
        from ..skill_runtime.mcp_compat import _REGISTERED_MCP_TOOLS

        mcp_n = len(_REGISTERED_MCP_TOOLS)
        checks.append(_check("mcp", "MCP 工具", _OK if mcp_n else _INFO,
                             detail=(f"已接入 {mcp_n} 个 MCP 工具（stdio）" if mcp_n
                                     else "未接入 MCP 工具；当前仅支持 stdio transport")))
    except Exception as exc:
        checks.append(_check("mcp", "MCP 工具", _INFO, detail=f"MCP 模块不可用：{str(exc)[:80]}"))
    # 远程 skill
    remote_on = bool(_get(cfg, "personification_skill_remote_enabled", False))
    sources = _get(cfg, "personification_skill_sources", None)
    src_n = len(sources) if isinstance(sources, (list, tuple)) else (1 if sources else 0)
    checks.append(_check("remote_skill", "远程 Skill", _OK if remote_on else _DISABLED,
                         detail=(f"已启用，配置 {src_n} 个来源（支持目录/zip/GitHub，不可信来源默认隔离）"
                                 if remote_on else "已关闭")))
    return checks


def _social_checks(cfg: Any) -> list[dict[str, Any]]:
    si = bool(_get(cfg, "personification_social_intelligence_enabled", False))
    pa = bool(_get(cfg, "personification_proactive_enabled", True))
    return [
        _check("social", "主动社交框架", _OK if si else _DISABLED, detail="已启用" if si else "已关闭"),
        _check("proactive", "主动私聊", _OK if pa else _DISABLED, detail="已启用" if pa else "已关闭"),
    ]


def _persona_prompt_checks(cfg: Any) -> list[dict[str, Any]]:
    try:
        from .prompt_loader import _resolve_candidate_path

        path = str(_get(cfg, "personification_prompt_path", "") or _get(cfg, "personification_system_path", "") or "").strip().strip('"').strip("'")
        if path:
            try:
                ok = _resolve_candidate_path(path).is_file()
            except OSError:
                ok = False
            if ok:
                return [_check("persona_prompt", "人设文件", _OK, detail=f"文件可读：{path}")]
            return [_check("persona_prompt", "人设文件", _ERROR, detail=f"路径不存在：{path}")]
        sp = str(_get(cfg, "personification_system_prompt", "") or "").strip()
        if sp:
            return [_check("persona_prompt", "人设设定", _OK, detail=f"使用内联 system_prompt（{len(sp)} 字）")]
        return [_check("persona_prompt", "人设设定", _WARN, detail="未配置人设，用内置默认")]
    except Exception as exc:
        return [_check("persona_prompt", "人设设定", _ERROR, detail=str(exc)[:140])]


async def _protocol_checks(get_bots: Any, cfg: Any, logger: Any) -> list[dict[str, Any]]:
    mode = str(_get(cfg, "personification_protocol_extensions", "auto") or "auto").strip().lower()
    if mode == "none":
        return [_check("protocol", "协议端扩展", _DISABLED, detail="已禁用扩展 API")]
    try:
        bots = get_bots() if callable(get_bots) else {}
        bot = next(iter(bots.values())) if bots else None
        if bot is None:
            return [_check("protocol", "协议端", _WARN, detail="Bot 未连接")]
        from .protocol_capabilities import detect_flavor

        flavor = await detect_flavor(bot, logger)  # 真实 get_version_info 调用
        if flavor == "gocq":
            return [_check("protocol", "协议端", _WARN, detail="go-cqhttp：不支持贴表情/拍一拍")]
        return [_check("protocol", "协议端", _OK, detail=f"调用正常，识别为 {flavor}")]
    except Exception as exc:
        return [_check("protocol", "协议端", _WARN, detail=str(exc)[:140])]


def _webui_checks(cfg: Any) -> list[dict[str, Any]]:
    approval = bool(_get(cfg, "personification_webui_require_device_approval", True))
    return [_check("device_approval", "新设备审批", _OK if approval else _WARN,
                   detail="已开启" if approval else "已关闭：新设备直接放行")]


async def _run_category(name: str, *, cfg, bundle, su, get_bots, logger) -> list[dict[str, Any]]:
    if name == "核心":
        return _safe_list(lambda: _core_checks(cfg, su))
    if name == "模型调用":
        return await _model_checks(cfg, bundle, logger)
    if name == "LLM 子模型":
        return await _llm_subconfig_checks(cfg)
    if name == "视觉能力":
        return await _vision_checks(cfg, bundle, logger)
    if name == "存储":
        return _safe_list(lambda: _db_checks(cfg))
    if name == "记忆":
        return _safe_list(lambda: _memory_checks(cfg, bundle))
    if name == "用户画像":
        return _safe_list(lambda: _persona_checks(cfg, bundle))
    if name == "群聊":
        return _safe_list(lambda: _group_checks(cfg))
    if name == "表情包":
        return _safe_list(lambda: _sticker_checks(cfg))
    if name == "TTS 语音":
        return await _tts_checks(cfg)
    if name == "QQ 空间":
        return await _qzone_checks(cfg)
    if name == "联网搜索":
        return await _search_checks(cfg)
    if name == "Skill 扩展":
        return _safe_list(lambda: _skill_checks(cfg, bundle))
    if name == "主动社交":
        return _safe_list(lambda: _social_checks(cfg))
    if name == "人设":
        return _safe_list(lambda: _persona_prompt_checks(cfg))
    if name == "协议端":
        return await _protocol_checks(get_bots, cfg, logger)
    if name == "WebUI 安全":
        return _safe_list(lambda: _webui_checks(cfg))
    return []


CATEGORY_NAMES: tuple[str, ...] = (
    "核心", "模型调用", "LLM 子模型", "视觉能力", "存储", "记忆", "用户画像", "群聊", "表情包",
    "TTS 语音", "QQ 空间", "联网搜索", "Skill 扩展", "主动社交", "人设",
    "协议端", "WebUI 安全",
)

# 全量体检结果缓存：WebUI 默认读缓存（秒开），仅启动 / 配置变更 / 手动刷新时重算
_CACHE: dict[str, Any] = {"result": None}


def get_cached_diagnostics() -> dict[str, Any] | None:
    return _CACHE.get("result")


async def run_diagnostics(
    *,
    plugin_config: Any,
    bundle: Any = None,
    superusers: set[str] | None = None,
    get_bots: Any = None,
    logger: Any = None,
    only: str = "",
) -> dict[str, Any]:
    """运行全部体检；only 指定单个分类名时只跑该项（逐项自检用）。"""
    cfg = plugin_config
    su = superusers or set()
    names = [only] if (only and only in CATEGORY_NAMES) else list(CATEGORY_NAMES)

    kwargs = dict(cfg=cfg, bundle=bundle, su=su, get_bots=get_bots, logger=logger)
    results = await asyncio.gather(*[_run_category(n, **kwargs) for n in names])
    categories = [{"name": n, "checks": r} for n, r in zip(names, results)]

    summary = {_OK: 0, _WARN: 0, _ERROR: 0, _DISABLED: 0, _INFO: 0}
    for cat in categories:
        for chk in cat["checks"]:
            summary[chk.get("status", _INFO)] = summary.get(chk.get("status", _INFO), 0) + 1
    overall = _ERROR if summary[_ERROR] else (_WARN if summary[_WARN] else _OK)
    result = {
        "generated_at": time.time(),
        "overall": overall,
        "summary": summary,
        "categories": categories,
        "live": True,
        "partial": bool(only),
    }
    if not only:
        _CACHE["result"] = result  # 仅缓存全量结果
    return result


async def warm_diagnostics(
    *, plugin_config: Any, bundle: Any = None, superusers: set[str] | None = None,
    get_bots: Any = None, logger: Any = None,
) -> None:
    """后台运行一次全量体检并写入缓存（启动 / 配置变更时调用），异常不外抛。"""
    try:
        await run_diagnostics(
            plugin_config=plugin_config, bundle=bundle, superusers=superusers,
            get_bots=get_bots, logger=logger,
        )
    except Exception as exc:  # pragma: no cover
        if logger is not None:
            try:
                logger.warning(f"[diagnostics] warm 失败: {exc}")
            except Exception:
                pass


__all__ = ["run_diagnostics", "warm_diagnostics", "get_cached_diagnostics", "CATEGORY_NAMES"]
