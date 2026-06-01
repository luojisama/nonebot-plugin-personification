from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .routes.audit_routes import build_audit_router
from .routes.auth_routes import build_auth_router
from .routes.proactive_routes import build_proactive_router
from .routes.config_routes import build_config_router
from .routes.group_routes import build_group_router
from .routes.memory_routes import build_memory_router
from .routes.metrics_routes import build_metrics_router
from .routes.persona_routes import build_persona_router
from .routes.plugin_knowledge_routes import build_plugin_knowledge_router
from .routes.quota_routes import build_quota_router
from .routes.skill_routes import build_skill_router
from .routes.sticker_routes import build_sticker_router
from .routes.test_routes import build_test_router


@dataclass
class _RuntimeContext:
    plugin_config: Any
    superusers: set[str]
    get_bots: Callable[[], dict[str, Any]]
    logger: Any
    runtime_bundle: Any = None


_RUNTIME: _RuntimeContext | None = None


def set_runtime_context(
    *,
    plugin_config: Any,
    superusers: set[str],
    get_bots: Callable[[], dict[str, Any]],
    logger: Any,
    runtime_bundle: Any = None,
) -> None:
    global _RUNTIME
    _RUNTIME = _RuntimeContext(
        plugin_config=plugin_config,
        superusers=set(superusers or set()),
        get_bots=get_bots,
        logger=logger,
        runtime_bundle=runtime_bundle,
    )


def get_runtime_context() -> _RuntimeContext:
    if _RUNTIME is None:
        raise RuntimeError("WebUI runtime context 未初始化")
    return _RUNTIME


def build_router() -> APIRouter:
    runtime = get_runtime_context()
    router = APIRouter(prefix="/personification")
    router.include_router(build_auth_router(runtime=runtime))
    router.include_router(build_config_router(runtime=runtime))
    router.include_router(build_metrics_router(runtime=runtime))
    router.include_router(build_persona_router(runtime=runtime))
    router.include_router(build_group_router(runtime=runtime))
    router.include_router(build_skill_router(runtime=runtime))
    router.include_router(build_test_router(runtime=runtime))
    router.include_router(build_memory_router(runtime=runtime))
    router.include_router(build_sticker_router(runtime=runtime))
    router.include_router(build_audit_router(runtime=runtime))
    router.include_router(build_proactive_router(runtime=runtime))
    router.include_router(build_quota_router(runtime=runtime))
    router.include_router(build_plugin_knowledge_router(runtime=runtime))

    @router.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return router


_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>拟人插件 控制台</title>
<style>
:root {
  --bg:#0f1115; --panel:#171a21; --line:#262a33;
  --text:#e6e8ef; --muted:#8a91a3; --input-bg:#0b0d12;
  --accent:#6aa8ff; --danger:#f87171; --warn:#f59e0b; --ok:#34d399;
  --hover-bg:#1f242c; --zebra:#13161c;
}
[data-theme="light"] {
  --bg:#f6f8fb; --panel:#ffffff; --line:#e3e8ef;
  --text:#1c2230; --muted:#6b7280; --input-bg:#ffffff;
  --accent:#2563eb; --danger:#dc2626; --warn:#d97706; --ok:#059669;
  --hover-bg:#eef2f7; --zebra:#f9fafc;
}
* { box-sizing:border-box; }
html,body { margin:0; padding:0; background:var(--bg); color:var(--text); font:13.5px/1.55 -apple-system,Segoe UI,Roboto,"Noto Sans CJK SC","Microsoft YaHei",sans-serif; transition:background .2s, color .2s; }
a { color:var(--accent); text-decoration:none; }
button { font:inherit; cursor:pointer; }
input,select,textarea { font:inherit; background:var(--input-bg); color:var(--text); border:1px solid var(--line); border-radius:6px; padding:6px 10px; transition:border-color .15s; }
input:focus, textarea:focus, select:focus { outline:none; border-color:var(--accent); }
.layout { display:grid; grid-template-columns:220px 1fr; min-height:100vh; }
aside { background:var(--panel); border-right:1px solid var(--line); padding:18px 0; }
aside h1 { font-size:13px; padding:0 18px 14px; color:var(--muted); margin:0; letter-spacing:1px; border-bottom:1px solid var(--line); }
aside nav { display:flex; flex-direction:column; padding:10px 0; }
aside nav a { padding:9px 18px; color:var(--text); border-left:3px solid transparent; transition:background .12s, border-color .12s; }
aside nav a:hover { background:var(--hover-bg); }
aside nav a.active { background:var(--hover-bg); border-left-color:var(--accent); }
main { padding:20px 26px; max-width:1400px; }
.mobile-nav-toggle { display:none; }
.progress-bar { position:fixed; top:0; left:0; right:0; height:2px; background:linear-gradient(90deg, transparent, var(--accent), transparent); background-size:200% 100%; animation:progress 1.2s linear infinite; z-index:100; }
@keyframes progress { 0% { background-position:200% 0; } 100% { background-position:-200% 0; } }
.breadcrumb { color:var(--muted); font-size:12.5px; margin-bottom:4px; }
.breadcrumb a { color:var(--muted); }
.breadcrumb a:hover { color:var(--accent); }
.breadcrumb span.sep { margin:0 6px; opacity:.5; }
.row { display:flex; gap:14px; align-items:center; }
.between { display:flex; justify-content:space-between; align-items:center; }
.muted { color:var(--muted); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin-bottom:16px; }
.card h2 { margin:0 0 14px; font-size:16px; font-weight:600; }
.tag { display:inline-block; padding:1px 8px; border-radius:99px; font-size:12px; background:#222; color:var(--muted); margin-right:6px; }
.tag.required { background:rgba(248,113,113,0.18); color:var(--danger); }
.tag.secret { background:rgba(245,158,11,0.18); color:var(--warn); }
.tag.source-env_file { background:rgba(106,168,255,0.18); color:var(--accent); }
.tag.source-env_json { background:rgba(52,211,153,0.18); color:var(--ok); }
.tag.source-runtime_config { background:rgba(245,158,11,0.18); color:var(--warn); }
.tag.source-default { background:#222; color:var(--muted); }
.field { padding:14px 0; border-top:1px solid var(--line); }
.field:first-child { border-top:none; }
.field-head { display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin-bottom:4px; }
.field-head strong { font-size:14px; }
.field-head code { font-size:11px; color:var(--muted); background:#0b0d12; padding:2px 6px; border-radius:4px; }
.field-desc { color:var(--muted); margin:4px 0 8px; font-size:12.5px; }
.field-input { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.field-input input[type=text], .field-input input[type=number], .field-input textarea { min-width:260px; flex:1; max-width:560px; }
.field-input textarea { min-height:60px; font-family:ui-monospace,Consolas,monospace; }
.api-pool-editor { width:100%; max-width:980px; display:flex; flex-direction:column; gap:10px; }
.api-provider-card { border:1px solid var(--line); border-radius:8px; padding:12px; background:var(--bg); }
.api-provider-head { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:10px; }
.api-provider-title { font-weight:600; }
.api-provider-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; }
.api-provider-field { display:flex; flex-direction:column; gap:4px; min-width:0; }
.api-provider-field label { color:var(--muted); font-size:12px; }
.api-provider-field input, .api-provider-field select { width:100%; min-width:0; }
.api-provider-actions { display:flex; gap:8px; flex-wrap:wrap; }
.api-pool-empty { border:1px dashed var(--line); border-radius:8px; padding:14px; color:var(--muted); }
.btn { padding:7px 14px; border-radius:6px; border:1px solid var(--line); background:var(--panel); color:var(--text); transition:border-color .15s, background .15s, transform .05s; }
.btn:hover { border-color:var(--accent); background:var(--hover-bg); }
.btn:active { transform:translateY(1px); }
.btn.primary { background:var(--accent); color:#ffffff; border-color:transparent; }
[data-theme="light"] .btn.primary { color:#ffffff; }
.btn.primary:hover { background:var(--accent); opacity:.9; }
.btn.danger { background:transparent; color:var(--danger); border-color:rgba(248,113,113,0.4); }
.btn.small { padding:3px 9px; font-size:12px; min-height:0; }
.btn:disabled { opacity:.5; cursor:not-allowed; }
.toggle { display:inline-flex; gap:4px; padding:3px; background:#0b0d12; border:1px solid var(--line); border-radius:99px; }
.toggle button { padding:4px 14px; border:none; border-radius:99px; background:transparent; color:var(--muted); }
.toggle button.on { background:var(--accent); color:#0b0d12; }
.alert { padding:10px 14px; border-radius:6px; margin-bottom:12px; }
.alert.ok { background:rgba(52,211,153,0.14); color:var(--ok); }
.alert.err { background:rgba(248,113,113,0.14); color:var(--danger); }
.alert.info { background:rgba(106,168,255,0.14); color:var(--accent); }
.group-bar { display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
.group-bar button { padding:5px 12px; border-radius:99px; border:1px solid var(--line); background:transparent; color:var(--muted); cursor:pointer; }
.group-bar button:hover { color:var(--text); border-color:var(--accent); }
.group-bar button.active { background:var(--accent); color:#0b0d12; border-color:transparent; }
.toolbar { display:flex; gap:10px; margin-bottom:14px; flex-wrap:wrap; align-items:center; }
.sticker-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:14px; }
.sticker-card { position:relative; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:8px; cursor:pointer; transition:border-color .15s; }
.sticker-card:hover { border-color:var(--accent); }
.sticker-card img { width:100%; aspect-ratio:1; object-fit:contain; background:#0b0d12; border-radius:4px; }
[data-theme="light"] .sticker-card img { background:#f3f5f8; }
.sticker-meta { padding:6px 2px 0; }
.sticker-name { font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); }
.sticker-desc { font-size:13px; margin:4px 0; min-height:20px; }
.sticker-delete-btn { position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,0.55); color:#fff; font-size:14px; line-height:1; display:flex; align-items:center; justify-content:center; cursor:pointer; opacity:0; transition:opacity .15s, background .15s; }
.sticker-card:hover .sticker-delete-btn, .sticker-delete-btn:focus { opacity:1; }
.sticker-delete-btn:hover { background:var(--danger); }
@media (hover:none) { .sticker-delete-btn { opacity:0.85; } }
.spinner { display:inline-block; width:14px; height:14px; border:2px solid var(--line); border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; vertical-align:middle; }
@keyframes spin { to { transform:rotate(360deg); } }
.avatar { width:28px; height:28px; border-radius:50%; vertical-align:middle; background:#0b0d12; object-fit:cover; display:inline-block; }
[data-theme="light"] .avatar { background:#f3f5f8; }
.topbar { position:sticky; top:0; z-index:5; background:var(--bg); padding-bottom:10px; margin-bottom:14px; }
.login-wrap { max-width:380px; margin:80px auto 0; }
.login-wrap .card { padding:28px; }
.login-wrap h2 { margin:0 0 16px; }
.login-wrap label { display:block; margin:10px 0 6px; color:var(--muted); }
.login-wrap input { width:100%; }
table { width:100%; border-collapse:collapse; }
th, td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; }
th { color:var(--muted); font-weight:500; font-size:12px; background:var(--bg); position:sticky; top:0; }
tbody tr:nth-child(even) td { background:var(--zebra); }
tbody tr:hover td { background:var(--hover-bg); }

/* Mobile 响应式 */
@media (max-width: 768px) {
  .layout { grid-template-columns:1fr; }
  aside { position:fixed; top:0; left:-100%; bottom:0; width:240px; z-index:50; transition:left .2s; padding-top:60px; }
  aside.open { left:0; box-shadow:2px 0 12px rgba(0,0,0,.3); }
  main { padding:14px 14px 60px; }
  .mobile-nav-toggle { display:inline-flex; align-items:center; justify-content:center; width:36px; height:36px; border-radius:6px; border:1px solid var(--line); background:var(--panel); color:var(--text); margin-right:10px; }
  .topbar { padding:8px 0 10px; }
  .topbar > div:first-child { flex:1; min-width:0; }
  .topbar strong { font-size:15px !important; }
  table { font-size:12.5px; }
  th, td { padding:6px 8px; }
  .field-input input[type=text], .field-input input[type=number], .field-input textarea { min-width:0; max-width:100%; width:100%; }
  .api-provider-grid { grid-template-columns:1fr; }
  .sticker-grid { grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); }
  .scrim { position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:40; }
}
@media (min-width: 769px) {
  .scrim { display:none !important; }
}
</style>
</head>
<body>
<div id="app"></div>
<script>
const API = "/personification/api";
let state = {
  logged: false, qq: "", view: "dashboard",
  entries: [], groups: [], activeGroup: null, configSearch: "",
  devices: [], alert: null, loading: false,
  dashboard: null, dashboardWindow: "month",
  personas: [], selectedPersona: null, personaSearch: "",
  groupList: [], selectedGroup: null, groupPersonas: [], groupStyle: null, groupKnowledge: [],
  groupSwitches: [], newGroupId: "",
  skills: [], skillFilter: "",
  testPrompt: "你好，自我介绍一下", testSystem: "你是测试助手，简洁回复。", testResult: null,
  memory: null, memoryFilter: "", memoryInnerState: null, memoryIncludeSelf: false,
  memoryGraph: null, memoryGraphGroupId: "", memoryGraphLimit: 100, memoryGraphMinSalience: 0,
  groupRawChat: null, groupStyleSnapIdx: 0, groupStyleRebuilding: false,
  showAdvancedConfig: false,
  stickers: null, stickerSearch: "", selectedSticker: null,
  theme: "dark", mobileNavOpen: false, eligibleAdmins: [],
  audit: null, auditFilter: "",
  proactiveStats: null, proactiveRecent: null, proactiveScope: "",
};

function readCookie(name) {
  const items = (document.cookie || "").split("; ");
  for (const it of items) {
    if (it.startsWith(name + "=")) {
      return decodeURIComponent(it.slice(name.length + 1));
    }
  }
  return "";
}

const _apiInflight = new Map();

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = { ...(opts.headers || {}) };
  // 非 safe method：自动从 cookie 读 CSRF token 并注入 header
  if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
    const csrf = readCookie("personification_webui_csrf");
    if (csrf) headers["X-Personification-CSRF"] = csrf;
  }
  // 仅对 GET 做 in-flight 去重；同一 path 在响应回来之前的重复请求复用同一 Promise
  const dedupKey = method === "GET" ? path : null;
  if (dedupKey && _apiInflight.has(dedupKey)) {
    return _apiInflight.get(dedupKey);
  }
  const promise = (async () => {
    const res = await fetch(API + path, { credentials: "include", ...opts, headers });
    if (res.status === 401) { state.logged = false; render(); throw new Error("未登录"); }
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || JSON.stringify(j); } catch {}
      throw new Error(detail);
    }
    return res.status === 204 ? null : await res.json();
  })();
  if (dedupKey) {
    _apiInflight.set(dedupKey, promise);
    promise.finally(() => { _apiInflight.delete(dedupKey); });
  }
  return promise;
}

function alertFlash(kind, text) { state.alert = { kind, text }; render(); setTimeout(() => { state.alert = null; render(); }, 4000); }

async function bootstrap() {
  // 主题
  const savedTheme = localStorage.getItem("personification_theme") || "dark";
  state.theme = savedTheme;
  document.documentElement.setAttribute("data-theme", savedTheme);
  try { const me = await api("/auth/me"); state.logged = true; state.qq = me.qq; await loadView(); }
  catch { state.logged = false; }
  if (!state.logged) {
    try { const ea = await fetch(API + "/auth/eligible-admins").then(r=>r.json()); state.eligibleAdmins = ea.admins||[]; } catch {}
  }
  render();
}

function toggleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", state.theme);
  localStorage.setItem("personification_theme", state.theme);
  render();
}

function toggleMobileNav() {
  state.mobileNavOpen = !state.mobileNavOpen;
  render();
}

async function loadView() {
  state.loading = true;
  try {
    if (state.view === "config") {
      const data = await api("/config/entries");
      state.entries = data.entries; state.groups = data.groups;
      if (!state.activeGroup || !state.groups.includes(state.activeGroup)) state.activeGroup = state.groups[0] || null;
    } else if (state.view === "devices") {
      const data = await api("/auth/devices");
      state.devices = data.devices; state.currentDeviceId = data.current_device_id;
    } else if (state.view === "dashboard") {
      state.dashboard = await api("/metrics/summary?window=" + encodeURIComponent(state.dashboardWindow));
    } else if (state.view === "personas") {
      const data = await api("/personas");
      state.personas = data.profiles; state.personasAvailable = data.available;
    } else if (state.view === "groups") {
      const data = await api("/groups");
      state.groupList = data.groups; state.groupsAvailable = data.available;
    } else if (state.view === "group_switch") {
      const data = await api("/groups/whitelist");
      state.groupSwitches = data.groups;
    } else if (state.view === "skills") {
      const data = await api("/skills");
      state.skills = data.skills; state.skillsAvailable = data.available;
    } else if (state.view === "test") {
      /* nothing to preload */
    } else if (state.view === "proactive") {
      const qs = new URLSearchParams({ since_hours: "72" });
      if (state.proactiveScope) qs.set("scope", state.proactiveScope);
      const [stats, recent] = await Promise.all([
        api("/proactive/stats?" + qs.toString()),
        api("/proactive/recent?limit=80" + (state.proactiveScope?`&scope=${encodeURIComponent(state.proactiveScope)}`:"")),
      ]);
      state.proactiveStats = stats;
      state.proactiveRecent = recent;
    } else if (state.view === "audit") {
      const qs = new URLSearchParams({ limit: "150" });
      if (state.auditFilter) qs.set("action", state.auditFilter);
      state.audit = await api("/audit/recent?" + qs.toString());
    } else if (state.view === "stickers") {
      state.stickers = await api("/stickers");
    } else if (state.view === "memory") {
      const qs = new URLSearchParams({ limit: "80" });
      if (state.memoryFilter) qs.set("memory_type", state.memoryFilter);
      if (state.memoryIncludeSelf) qs.set("include_self", "true");
      if (state.memoryUserId) qs.set("user_id", state.memoryUserId);
      if (state.memoryGroupId) qs.set("group_id", state.memoryGroupId);
      if (state.memoryPalaceZone) qs.set("palace_zone", state.memoryPalaceZone);
      const [mem, inner, zones] = await Promise.all([
        api("/memory/recent?" + qs.toString()),
        api("/memory/inner-state").catch(() => ({available: false})),
        api("/memory/palace-zones").catch(() => ({zones: []})),
      ]);
      state.memory = mem;
      state.memoryInnerState = inner;
      state.memoryPalaceZones = zones.zones || [];
    } else if (state.view === "plugin_knowledge") {
      const data = await api("/plugin-knowledge/list");
      state.pluginKnowledgeList = data.plugins || [];
      state.pluginKnowledgeAvailable = data.available;
      state.pluginKnowledgeTotal = data.total || 0;
    } else if (state.view === "memory_graph") {
      const qs = new URLSearchParams({ limit: String(state.memoryGraphLimit || 100) });
      if (state.memoryGraphGroupId) qs.set("group_id", state.memoryGraphGroupId);
      if (state.memoryGraphMinSalience) qs.set("min_salience", String(state.memoryGraphMinSalience));
      // 群下拉依赖 groupList；如果还没加载过，顺手加载一次
      const tasks = [api("/memory/graph?" + qs.toString()).catch(e => ({available:false, error:e.message}))];
      if (!state.groupList.length) tasks.push(api("/groups").catch(() => ({groups:[]})));
      const [graph, groupsResp] = await Promise.all(tasks);
      state.memoryGraph = graph;
      if (groupsResp && groupsResp.groups) {
        state.groupList = groupsResp.groups;
        state.groupsAvailable = groupsResp.available;
      }
    }
  } finally { state.loading = false; }
}

function render() {
  const root = document.getElementById("app");
  if (!state.logged) { root.innerHTML = renderLogin(); attachLogin(); return; }
  // 全量 innerHTML 重绘会让正在输入的搜索框失焦；记下焦点 + 光标位置，重绘后还原。
  const active = document.activeElement;
  let focusSnap = null;
  if (active && active.id && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
    focusSnap = {
      id: active.id,
      start: active.selectionStart,
      end: active.selectionEnd,
      scrollTop: active.scrollTop,
    };
  }
  root.innerHTML = renderLayout();
  attachLayout();
  if (focusSnap) {
    const next = document.getElementById(focusSnap.id);
    if (next && (next.tagName === "INPUT" || next.tagName === "TEXTAREA")) {
      next.focus();
      try {
        if (focusSnap.start !== null && focusSnap.end !== null) {
          next.setSelectionRange(focusSnap.start, focusSnap.end);
        }
        next.scrollTop = focusSnap.scrollTop || 0;
      } catch (_) { /* number/email inputs 不支持 setSelectionRange，忽略 */ }
    }
  }
}

function renderLayout() {
  const navItem = (v, label) => `<a href="#${v}" class="${state.view===v?'active':''}" onclick="state.mobileNavOpen=false">${label}</a>`;
  const themeIcon = state.theme === "dark" ? "🌙" : "☀";
  return `${state.loading ? '<div class="progress-bar"></div>' : ''}
    <div class="layout">
    ${state.mobileNavOpen ? '<div class="scrim" onclick="toggleMobileNav()"></div>' : ''}
    <aside class="${state.mobileNavOpen?'open':''}">
      <h1>拟人插件控制台</h1>
      <nav>
        ${navItem('dashboard','仪表盘')}
        ${navItem('config','配置中心')}
        ${navItem('personas','用户画像')}
        ${navItem('groups','群信息')}
        ${navItem('group_switch','群开关')}
        ${navItem('memory','Agent 记忆')}
        ${navItem('memory_graph','记忆宫殿')}
        ${navItem('stickers','表情包')}
        ${navItem('skills','Skill 管理')}
        ${navItem('plugin_knowledge','插件知识库')}
        ${navItem('test','模型测试')}
        ${navItem('proactive','主动诊断')}
        ${navItem('audit','审计日志')}
        ${navItem('devices','设备管理')}
      </nav>
    </aside>
    <main>
      <div class="topbar between">
        <div style="display:flex;align-items:center;min-width:0;flex:1">
          <button class="mobile-nav-toggle" onclick="toggleMobileNav()" aria-label="菜单">≡</button>
          <div style="min-width:0">
            <div class="breadcrumb">控制台 <span class="sep">›</span> ${escapeHtml(viewTitle())}</div>
            <strong style="font-size:17px">${escapeHtml(viewTitle())}</strong>
          </div>
        </div>
        <div class="row">
          <button class="btn small" onclick="toggleTheme()" title="切换主题">${themeIcon}</button>
          <span class="muted" title="登录 QQ">${escapeHtml(state.qq)}</span>
          <button class="btn small" onclick="doLogout()">退出</button>
        </div>
      </div>
      ${state.alert ? `<div class="alert ${state.alert.kind}">${escapeHtml(state.alert.text)}</div>` : ''}
      ${renderView()}
    </main>
  </div>`;
}

function renderView() {
  if (state.view === "config") return renderConfig();
  if (state.view === "devices") return renderDevices();
  if (state.view === "dashboard") return renderDashboard();
  if (state.view === "personas") return renderPersonas();
  if (state.view === "groups") return renderGroups();
  if (state.view === "group_switch") return renderGroupSwitch();
  if (state.view === "skills") return renderSkills();
  if (state.view === "plugin_knowledge") return renderPluginKnowledge();
  if (state.view === "test") return renderTest();
  if (state.view === "memory") return renderMemory();
  if (state.view === "memory_graph") return renderMemoryGraph();
  if (state.view === "stickers") return renderStickers();
  if (state.view === "audit") return renderAudit();
  if (state.view === "proactive") return renderProactive();
  return `<div class="card"><h2>${escapeHtml(viewTitle())}</h2><p class="muted">该视图暂未实现。</p></div>`;
}

function renderProactive() {
  const stats = state.proactiveStats;
  const recent = state.proactiveRecent;
  if (!stats || !recent) return `<div class="card muted">加载中…</div>`;
  const counts = stats.counts || {};
  const total = stats.total || 0;
  const reasonLabels = {
    sent: "已发送",
    skip_daily_limit: "日上限",
    skip_cooldown: "冷却中",
    skip_idle_not_reached: "用户未空闲",
    skip_probability: "概率未中",
    skip_quiet_hour: "深夜禁言",
    skip_no_candidate: "无候选人",
    skip_llm_failed: "LLM 调用失败",
    skip_llm_decided: "LLM 决定跳过",
    skip_unread: "上条未读",
    skip_disabled: "功能禁用",
    skip_no_profile: "缺画像",
    skip_other: "其他",
  };
  const scopeFilter = [
    {k: "", label: "全部"},
    {k: "private", label: "主动私聊"},
    {k: "group_idle", label: "群主动接话"},
    {k: "qzone", label: "QQ 空间"},
  ];
  const scopeBar = scopeFilter.map(s =>
    `<button class="${state.proactiveScope===s.k?'active':''}" onclick="pickProactiveScope('${s.k}')">${escapeHtml(s.label)}</button>`
  ).join("");

  // 统计卡片
  const reasonRows = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([reason, cnt]) => {
      const pct = total > 0 ? Math.round(cnt / total * 100) : 0;
      const label = reasonLabels[reason] || reason;
      const barColor = reason === "sent" ? "var(--ok)" : "var(--muted)";
      return `<tr>
        <td>${escapeHtml(label)} <code style="font-size:11px;opacity:.6">${escapeHtml(reason)}</code></td>
        <td style="width:60%"><div style="background:${barColor};height:6px;border-radius:3px;width:${pct}%;min-width:2px"></div></td>
        <td style="text-align:right">${cnt} <span class="muted">/ ${pct}%</span></td>
      </tr>`;
    }).join("");
  const summary = total === 0
    ? `<p class="muted">最近 72 小时没有主动触发尝试记录。可能 bot 刚启动，或 personification_proactive_enabled / personification_group_idle_topic_enabled 都关闭了。</p>`
    : `<table style="margin-top:8px"><thead><tr><th>结果 / Reason</th><th style="width:60%">占比</th><th style="text-align:right">次数</th></tr></thead><tbody>${reasonRows}</tbody></table>`;

  // 最近事件流
  const eventRows = (recent.entries || []).map(e => {
    const time = new Date(e.ts * 1000).toLocaleString();
    const outcomeColor = e.outcome === "sent"
      ? `<span class="tag" style="background:rgba(52,211,153,0.18);color:var(--ok)">${escapeHtml(reasonLabels[e.outcome]||e.outcome)}</span>`
      : `<span class="tag" style="background:rgba(248,113,113,0.12);color:var(--danger)">${escapeHtml(reasonLabels[e.outcome]||e.outcome)}</span>`;
    const next = e.next_eligible_at
      ? `下次可触发：${new Date(e.next_eligible_at * 1000).toLocaleString()}`
      : "";
    const detailParts = Object.entries(e.detail || {}).map(([k, v]) => `${escapeHtml(k)}=${escapeHtml(String(v).slice(0,40))}`);
    return `<tr>
      <td class="muted" style="font-size:11px;white-space:nowrap">${escapeHtml(time)}</td>
      <td><code style="font-size:11px">${escapeHtml(e.scope)}</code></td>
      <td>${outcomeColor}</td>
      <td>${escapeHtml(e.target || "-")}</td>
      <td class="muted" style="font-size:11px">${detailParts.slice(0,3).join(" · ")}${next ? "<br>"+escapeHtml(next):""}</td>
    </tr>`;
  }).join("");

  return `<div class="group-bar">${scopeBar}</div>
    <div class="card"><h2>主动行为统计（最近 72 小时）</h2>
      <p class="muted" style="font-size:12px;margin:-4px 0 8px">
        记录每次主动私聊 / 群接话 / QQ 空间发表的触发尝试。如果 "sent" 占比偏低或某 skip 原因频繁出现，
        参考下方配置中心调整：proactive_probability / proactive_daily_limit / proactive_idle_hours 等。
      </p>
      ${summary}
    </div>
    <div class="card"><h2>最近 ${(recent.entries||[]).length} 条触发记录</h2>
      <table>
        <thead><tr><th>时间</th><th>类型</th><th>结果</th><th>对象</th><th>详情</th></tr></thead>
        <tbody>${eventRows || '<tr><td colspan="5" class="muted">无</td></tr>'}</tbody>
      </table>
    </div>`;
}

async function pickProactiveScope(scope) {
  state.proactiveScope = scope;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

function renderAudit() {
  const data = state.audit;
  if (!data) return `<div class="card muted">加载中…</div>`;
  const actionFilters = [
    {key:"", label:"全部"},
    {key:"login_verify", label:"登录"},
    {key:"config_update", label:"配置修改"},
    {key:"device_revoke", label:"设备撤销"},
    {key:"sticker_delete", label:"表情删除"},
    {key:"sticker_upload", label:"表情上传"},
    {key:"skill_toggle", label:"Skill 启停"},
    {key:"style_rebuild", label:"风格重建"},
  ];
  const filterBar = actionFilters.map(f => `<button class="${state.auditFilter===f.key?'active':''}" onclick="pickAuditFilter('${f.key}')">${escapeHtml(f.label)}</button>`).join("");
  const rows = (data.entries || []).map(e => {
    const time = new Date(e.ts * 1000).toLocaleString();
    const outcome = e.outcome === "ok"
      ? '<span class="tag" style="background:rgba(52,211,153,0.18);color:var(--ok)">成功</span>'
      : `<span class="tag" style="background:rgba(248,113,113,0.18);color:var(--danger)">${escapeHtml(e.outcome)}</span>`;
    return `<tr>
      <td class="muted" style="font-size:12px;white-space:nowrap">${escapeHtml(time)}</td>
      <td><code style="font-size:11px">${escapeHtml(e.action)}</code></td>
      <td>${escapeHtml(e.qq||'-')}</td>
      <td>${escapeHtml(e.target||'-')}</td>
      <td>${outcome}</td>
    </tr>`;
  }).join("");
  return `<div class="group-bar">${filterBar}</div>
    <div class="card">
      <h2>审计日志（最近 ${(data.entries||[]).length} 条）</h2>
      <p class="muted" style="font-size:12px;margin:-6px 0 10px">记录登录、配置修改、表情包/Skill/风格等敏感动作；保留 90 天。</p>
      <table><thead><tr><th>时间</th><th>动作</th><th>QQ</th><th>对象</th><th>结果</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="muted">暂无</td></tr>'}</tbody></table>
    </div>`;
}

async function pickAuditFilter(action) {
  state.auditFilter = action;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

function renderStickers() {
  const data = state.stickers;
  if (!data) return `<div class="card muted">加载中…</div>`;
  const items = data.stickers || [];
  const search = (state.stickerSearch || "").trim().toLowerCase();
  const filtered = search
    ? items.filter(s => s.filename.toLowerCase().includes(search)
        || (s.description||"").toLowerCase().includes(search)
        || (s.mood_tags||[]).join(",").includes(search)
        || (s.scene_tags||[]).join(",").includes(search))
    : items;
  const grid = filtered.map(s => {
    const tags = [...(s.mood_tags||[]), ...(s.scene_tags||[])].slice(0, 5).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join("");
    const labelTag = s.labeled
      ? '<span class="tag" style="background:rgba(52,211,153,0.18);color:var(--ok)">已标</span>'
      : '<span class="tag" style="background:rgba(245,158,11,0.18);color:var(--warn)">待标</span>';
    return `<div class="sticker-card" onclick="openStickerEdit('${escapeAttr(s.filename)}')">
      <button class="sticker-delete-btn" title="移到回收" onclick="event.stopPropagation();deleteStickerByName('${escapeAttr(s.filename)}')">×</button>
      <img src="${escapeAttr(s.thumbnail_url)}" loading="lazy" alt="${escapeAttr(s.filename)}">
      <div class="sticker-meta">
        <div class="sticker-name" title="${escapeAttr(s.filename)}">${escapeHtml(s.filename)}</div>
        <div class="sticker-desc">${escapeHtml((s.description||'').slice(0,40))}</div>
        <div>${labelTag} ${tags}</div>
      </div>
    </div>`;
  }).join("");
  return `<div class="toolbar">
      <input id="sticker-search-input" type="search" placeholder="按文件名/描述/标签搜索…" value="${escapeAttr(state.stickerSearch)}" oninput="state.stickerSearch=this.value;render()" style="flex:1;max-width:340px">
      <span class="muted">共 ${data.total} 张，已标 ${data.labeled_count}</span>
      <button class="btn" onclick="document.getElementById('sticker-upload-input').click()">上传</button>
      <input id="sticker-upload-input" type="file" accept="image/jpeg,image/png,image/webp,image/gif" style="display:none" onchange="uploadStickerFromInput(this)">
      <button class="btn" onclick="rescanStickers('missing_only')">扫描未打标</button>
      <button class="btn" onclick="rescanStickers('force_all')" style="color:var(--warn)">全部重打标</button>
    </div>
    <p class="muted" style="font-size:12px;margin:0 0 12px">表情包目录：<code>${escapeHtml(data.sticker_dir)}</code>。删除会移到 trash/YYYYMMDD/ 子目录，可手动恢复。</p>
    <div class="sticker-grid">${grid || '<p class="muted">暂无表情包</p>'}</div>
    ${state.selectedSticker ? renderStickerEdit() : ''}`;
}

async function uploadStickerFromInput(input) {
  if (!input.files || !input.files.length) return;
  const file = input.files[0];
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch(API + "/stickers/upload", { method: "POST", credentials: "include", body: form });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    const out = await res.json();
    alertFlash("ok", `上传成功：${out.filename}${out.needs_labeling?'（待打标）':''}`);
    await loadView(); render();
  } catch (e) { alertFlash("err", "上传失败：" + e.message); }
  input.value = "";
}

async function rescanStickers(mode) {
  const label = mode === "force_all" ? "全部重打标" : "扫描未打标";
  if (!confirm(`${label}：将清空对应表情包的标签元数据，等待下次启动或后台 labeler 扫描时重打。继续？`)) return;
  try {
    const out = await api("/stickers/rescan", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({mode}) });
    alertFlash("ok", `${label}：已清空 ${out.scheduled} 个条目`);
    await loadView(); render();
  } catch (e) { alertFlash("err", "操作失败：" + e.message); }
}

function openStickerEdit(name) {
  const item = (state.stickers?.stickers || []).find(x => x.filename === name);
  if (!item) return;
  state.selectedSticker = JSON.parse(JSON.stringify(item));
  render();
}

function renderStickerEdit() {
  const s = state.selectedSticker;
  return `<div class="card" style="margin-top:14px">
    <div class="between"><h2 style="margin:0">编辑 ${escapeHtml(s.filename)}</h2>
      <button class="btn small" onclick="state.selectedSticker=null;render()">关闭</button></div>
    <div style="display:flex;gap:20px;margin-top:14px;flex-wrap:wrap">
      <img src="${escapeAttr(s.thumbnail_url)}" style="max-width:200px;max-height:200px;border-radius:6px;object-fit:contain;background:#0b0d12">
      <div style="flex:1;min-width:280px">
        <label class="muted">描述</label>
        <textarea oninput="state.selectedSticker.description=this.value" style="width:100%;min-height:50px;margin:4px 0 10px">${escapeHtml(s.description)}</textarea>
        <label class="muted">心情标签（逗号分隔）</label>
        <input type="text" value="${escapeAttr((s.mood_tags||[]).join(','))}" oninput="state.selectedSticker.mood_tags=this.value.split(',').map(x=>x.trim()).filter(Boolean)" style="width:100%;margin:4px 0 10px">
        <label class="muted">场景标签（逗号分隔）</label>
        <input type="text" value="${escapeAttr((s.scene_tags||[]).join(','))}" oninput="state.selectedSticker.scene_tags=this.value.split(',').map(x=>x.trim()).filter(Boolean)" style="width:100%;margin:4px 0 10px">
        <label class="muted">使用建议</label>
        <input type="text" value="${escapeAttr(s.use_hint||'')}" oninput="state.selectedSticker.use_hint=this.value" style="width:100%;margin:4px 0 10px">
        <label class="muted">避免使用</label>
        <input type="text" value="${escapeAttr(s.avoid_hint||'')}" oninput="state.selectedSticker.avoid_hint=this.value" style="width:100%;margin:4px 0 10px">
        <label class="muted" style="display:flex;align-items:center;gap:6px"><input type="checkbox" ${s.proactive_send?'checked':''} onchange="state.selectedSticker.proactive_send=this.checked" style="width:auto">允许在主动场景发送</label>
        <label class="muted" style="display:block;margin-top:10px">权重（0-3）</label>
        <input type="number" step="0.1" min="0" max="3" value="${s.weight}" oninput="state.selectedSticker.weight=parseFloat(this.value)" style="width:100px">
        <div class="row" style="margin-top:14px">
          <button class="btn primary" onclick="saveSticker()">保存</button>
          <button class="btn danger" onclick="deleteSticker()">移到回收</button>
        </div>
      </div>
    </div>
  </div>`;
}

async function saveSticker() {
  const s = state.selectedSticker;
  try {
    await api("/stickers/" + encodeURIComponent(s.filename), {
      method:"PATCH",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({
        description: s.description, mood_tags: s.mood_tags, scene_tags: s.scene_tags,
        proactive_send: s.proactive_send, use_hint: s.use_hint, avoid_hint: s.avoid_hint,
        weight: s.weight,
      }),
    });
    alertFlash("ok", "已保存");
    state.selectedSticker = null;
    await loadView(); render();
  } catch (e) { alertFlash("err", "保存失败：" + e.message); }
}

async function deleteSticker() {
  const s = state.selectedSticker;
  if (!s) return;
  await deleteStickerByName(s.filename);
  state.selectedSticker = null;
}

async function deleteStickerByName(name) {
  if (!confirm(`将 ${name} 移到 trash 目录？可手动恢复。`)) return;
  try {
    await api("/stickers/" + encodeURIComponent(name), { method:"DELETE" });
    alertFlash("ok", `已移到回收：${name}`);
    if (state.selectedSticker && state.selectedSticker.filename === name) {
      state.selectedSticker = null;
    }
    await loadView(); render();
  } catch (e) { alertFlash("err", "删除失败：" + e.message); }
}

function renderMemory() {
  const mem = state.memory;
  const inner = state.memoryInnerState;
  if (state.selectedMemory) return renderMemoryDetail();
  if (!mem) return `<div class="card muted">加载中…</div>`;
  if (!mem.palace_enabled) {
    return `<div class="card"><h2>Agent 记忆</h2>
      <p class="muted">memory palace 未启用。要查看长期记忆，需在配置中开启 <code>personification_memory_palace_enabled</code>。</p></div>`;
  }
  const filters = ["", "group_knowledge", "user_persona", "fact"].map(t =>
    `<button class="${state.memoryFilter===t?'active':''}" onclick="pickMemoryFilter('${t}')">${t || '全部类型'}</button>`
  ).join("");
  const zoneOptions = ['<option value="">全部分区</option>'].concat(
    (state.memoryPalaceZones || []).map(z => `<option value="${escapeAttr(z)}" ${state.memoryPalaceZone===z?'selected':''}>${escapeHtml(z)}</option>`)
  ).join("");
  const rows = (mem.items || []).map(it => `<tr>
    <td><span class="tag">${escapeHtml(it.memory_type||'-')}</span></td>
    <td><code style="font-size:11px">${escapeHtml(it.group_id||'')}${it.user_id ? '/'+escapeHtml(it.user_id) : ''}</code></td>
    <td>${escapeHtml(it.summary)}</td>
    <td class="muted" style="font-size:12px">conf=${it.confidence.toFixed(2)}<br>sal=${it.salience.toFixed(2)}</td>
    <td class="muted" style="font-size:12px">${it.updated_at?new Date(it.updated_at*1000).toLocaleString():'-'}</td>
    <td><button class="btn small" onclick="openMemoryDetail('${escapeAttr(it.memory_id)}')">详情</button></td>
  </tr>`).join("");
  const hiddenNote = mem.hidden_self_count
    ? `<span class="muted" style="font-size:12px;margin-left:10px">已默认隐藏 ${mem.hidden_self_count} 条 bot 自言条目</span>`
    : '';
  let innerBlock = '';
  if (inner && inner.available) {
    const s = inner.state || {};
    const warm = s.relation_warmth || {};
    const warmRows = Object.keys(warm).slice(0,12).map(k => `<tr><td><code>${escapeHtml(k)}</code></td><td>${typeof warm[k]==='number'?warm[k].toFixed(2):escapeHtml(String(warm[k]))}</td></tr>`).join("");
    innerBlock = `<div class="card"><h2>Inner State（情绪/能量/关系）</h2>
      <div class="row" style="gap:30px;flex-wrap:wrap">
        <div><div class="muted">mood</div><div style="font-size:18px;margin-top:4px">${escapeHtml(String(s.mood||'-'))}</div></div>
        <div><div class="muted">energy</div><div style="font-size:18px;margin-top:4px">${escapeHtml(String(s.energy||'-'))}</div></div>
        <div><div class="muted">pending</div><div style="font-size:13px;margin-top:4px">${escapeHtml(String(s.pending_thoughts||'-')).slice(0,80)||'-'}</div></div>
      </div>
      ${warmRows ? `<h3 style="margin-top:14px;margin-bottom:6px;font-size:13px">用户好感度</h3><table style="max-width:420px"><thead><tr><th>用户</th><th>好感</th></tr></thead><tbody>${warmRows}</tbody></table>`:''}</div>`;
  }
  return `${innerBlock}
    <div class="toolbar">
      <div class="group-bar" style="margin-bottom:0">${filters}</div>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
        <input type="checkbox" ${state.memoryIncludeSelf?'checked':''} onchange="toggleMemoryIncludeSelf(this.checked)" style="width:auto">
        包含 bot 自己的发言
      </label>
      ${hiddenNote}
    </div>
    <div class="card">
      <div class="row" style="gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <input id="mem-user-input" placeholder="按 user_id 过滤" value="${escapeAttr(state.memoryUserId || '')}" onkeydown="if(event.key==='Enter')applyMemoryFilters()" style="width:160px">
        <input id="mem-group-input" placeholder="按 group_id 过滤" value="${escapeAttr(state.memoryGroupId || '')}" onkeydown="if(event.key==='Enter')applyMemoryFilters()" style="width:160px">
        <select id="mem-zone-select" onchange="pickPalaceZone(this.value)">${zoneOptions}</select>
        <button class="btn" onclick="applyMemoryFilters()">应用</button>
        ${(state.memoryUserId || state.memoryGroupId || state.memoryPalaceZone) ? '<button class="btn small" onclick="clearMemoryFilters()">清除过滤</button>' : ''}
      </div>
      <h2>长期记忆（${(mem.items||[]).length}）</h2>
      <p class="muted" style="font-size:12px;margin:-6px 0 10px">
        从 memory_palace 数据库蒸馏后的记忆条目（用户画像、群知识、事实等）；
        ${state.memoryIncludeSelf ? '当前显示 bot 自言条目。' : 'bot 自己的发言默认隐藏，勾选上方复选框可显示。'}
        要看群里的原始对话历史，请进入「群信息」→ 选择群 → 切「对话原文」tab。
      </p>
      <table><thead><tr><th>类型</th><th>作用域</th><th>摘要</th><th>分数</th><th>更新</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="muted">暂无记忆条目</td></tr>'}</tbody></table>
    </div>`;
}

async function pickMemoryFilter(t) {
  state.memoryFilter = t;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function toggleMemoryIncludeSelf(checked) {
  state.memoryIncludeSelf = !!checked;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function applyMemoryFilters() {
  state.memoryUserId = (document.getElementById("mem-user-input")?.value || "").trim();
  state.memoryGroupId = (document.getElementById("mem-group-input")?.value || "").trim();
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function pickPalaceZone(zone) {
  state.memoryPalaceZone = zone || "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function clearMemoryFilters() {
  state.memoryUserId = "";
  state.memoryGroupId = "";
  state.memoryPalaceZone = "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function openMemoryDetail(memoryId) {
  if (!memoryId) return;
  try {
    state.selectedMemory = await api("/memory/detail/" + encodeURIComponent(memoryId));
    render();
  } catch (e) { alertFlash("err", e.message); }
}

function renderMemoryDetail() {
  const d = state.selectedMemory;
  const it = d.item || {};
  const related = d.related || [];
  const tagLine = (label, arr) => arr && arr.length ? `<div style="margin:4px 0"><span class="muted" style="font-size:12px">${escapeHtml(label)}：</span>${arr.map(v => `<span class="tag">${escapeHtml(String(v))}</span>`).join("")}</div>` : '';
  const relatedRows = related.map(r => `<tr>
    <td><span class="tag">${escapeHtml(r.memory_type||'-')}</span></td>
    <td>${escapeHtml((r.summary||'').slice(0,120))}</td>
    <td><button class="btn small" onclick="openMemoryDetail('${escapeAttr(r.memory_id||'')}')">查看</button></td>
  </tr>`).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedMemory=null;render()">返回列表</button><span class="muted">记忆 ${escapeHtml(d.memory_id)}</span></div>
    <div class="card">
      <h2>${escapeHtml(it.memory_type || '-')} <code style="font-size:13px;color:var(--muted)">${escapeHtml(d.memory_id)}</code></h2>
      <div class="row" style="gap:20px;flex-wrap:wrap;font-size:13px">
        ${it.palace_zone ? `<div><span class="muted">palace_zone：</span><strong>${escapeHtml(it.palace_zone)}</strong></div>` : ''}
        ${it.group_id ? `<div><span class="muted">group_id：</span><code>${escapeHtml(it.group_id)}</code></div>` : ''}
        ${it.user_id ? `<div><span class="muted">user_id：</span><code>${escapeHtml(it.user_id)}</code></div>` : ''}
        <div><span class="muted">confidence：</span>${(it.confidence||0).toFixed(2)}</div>
        <div><span class="muted">salience：</span>${(it.salience||0).toFixed(2)}</div>
        ${typeof it.stability === 'number' ? `<div><span class="muted">stability：</span>${it.stability.toFixed(2)}</div>` : ''}
      </div>
      <h3 style="margin-top:14px">摘要</h3>
      <pre style="white-space:pre-wrap;margin:0;font-family:inherit">${escapeHtml(it.summary || '')}</pre>
      ${tagLine('topic_tags', it.topic_tags)}
      ${tagLine('entity_tags', it.entity_tags)}
      ${tagLine('aliases', it.aliases)}
      ${it.why_relevant ? `<h3>关联说明</h3><p>${escapeHtml(it.why_relevant)}</p>` : ''}
      ${it.time_hint ? `<p class="muted" style="font-size:12px">时间提示：${escapeHtml(it.time_hint)}</p>` : ''}
      <details style="margin-top:12px"><summary class="muted">完整 payload</summary><pre style="white-space:pre-wrap;font-size:12px;background:#0b0d12;padding:10px;border-radius:6px;overflow-x:auto">${escapeHtml(JSON.stringify(it, null, 2))}</pre></details>
    </div>
    ${related.length ? `<div class="card"><h3>关联记忆（${related.length}）</h3><table><tbody>${relatedRows}</tbody></table></div>` : ''}`;
}

function viewTitle() {
  return ({dashboard:"仪表盘",config:"配置中心",personas:"用户画像",groups:"群信息",group_switch:"群开关",memory:"Agent 记忆",memory_graph:"记忆宫殿",stickers:"表情包",skills:"Skill 管理",plugin_knowledge:"插件知识库",test:"模型测试",audit:"审计日志",proactive:"主动诊断",devices:"设备管理"})[state.view] || state.view;
}

// ---------------------------------------------------------------------------
// 记忆宫殿（Cytoscape 力导向关系图）
// ---------------------------------------------------------------------------
let _cytoscapeLoaded = false;
let _cytoscapeInstance = null;

function ensureCytoscapeLoaded() {
  if (_cytoscapeLoaded) return Promise.resolve();
  if (window.cytoscape) { _cytoscapeLoaded = true; return Promise.resolve(); }
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js';
    s.onload = () => { _cytoscapeLoaded = true; resolve(); };
    s.onerror = () => reject(new Error('cytoscape 加载失败（检查网络）'));
    document.head.appendChild(s);
  });
}

function _memoryGraphColor(kind) {
  if (kind === 'memory') return '#6aa8ff';
  if (kind === 'entity') return '#f59e0b';
  if (kind === 'user') return '#34d399';
  return '#9ca3af';
}

function renderMemoryGraph() {
  const data = state.memoryGraph;
  const nodeCount = data ? (data.nodes||[]).length : 0;
  const edgeCount = data ? (data.edges||[]).length : 0;
  const groups = state.groupList || [];
  const opts = groups.map(g => `<option value="${escapeAttr(g.group_id)}" ${state.memoryGraphGroupId===g.group_id?'selected':''}>${escapeHtml(g.group_name||g.group_id)}</option>`).join('');
  const unavailable = data && data.available === false;
  const errorBox = unavailable
    ? `<div class="alert err" style="margin-bottom:10px">记忆宫殿不可用（${escapeHtml(data.reason||data.error||'未知原因')}）。检查 personification_memory_palace_enabled。</div>`
    : '';
  setTimeout(() => { try { renderMemoryGraphCanvas(); } catch(e) { console.warn('cytoscape', e); } }, 60);
  return `<div class="card">
    <div class="between" style="margin-bottom:10px">
      <h2 style="margin:0">记忆宫殿 力导向图</h2>
      <div class="row">
        <button class="btn small" onclick="resetMemoryGraphZoom()">复位视图</button>
        <button class="btn small" onclick="exportMemoryGraphPNG()">导出PNG</button>
      </div>
    </div>
    ${errorBox}
    <div class="row" style="gap:10px;flex-wrap:wrap;margin-bottom:10px;align-items:center">
      <label class="muted" style="font-size:12px">群：</label>
      <select onchange="state.memoryGraphGroupId=this.value; loadView().then(render)" style="min-width:180px">
        <option value="">（全局）</option>
        ${opts}
      </select>
      <label class="muted" style="font-size:12px">条目上限：</label>
      <input type="number" value="${state.memoryGraphLimit||100}" min="10" max="300" step="10" onchange="state.memoryGraphLimit=Number(this.value); loadView().then(render)" style="width:80px">
      <label class="muted" style="font-size:12px">最小 salience：</label>
      <input type="number" value="${state.memoryGraphMinSalience||0}" min="0" max="1" step="0.05" onchange="state.memoryGraphMinSalience=Number(this.value); loadView().then(render)" style="width:80px">
      <span class="muted" style="font-size:12px;margin-left:auto">节点 ${nodeCount} · 边 ${edgeCount}</span>
    </div>
    <div class="row" style="gap:14px;font-size:12px;margin-bottom:8px">
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#6aa8ff;margin-right:4px"></span>记忆条目</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-right:4px"></span>实体/标签</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#34d399;margin-right:4px"></span>群成员</span>
    </div>
    <div id="memory-graph-canvas" style="width:100%;height:540px;background:var(--input-bg);border:1px solid var(--line);border-radius:6px"></div>
    <div id="memory-graph-detail" class="muted" style="font-size:12px;margin-top:8px">点击节点查看详情。</div>
  </div>`;
}

async function renderMemoryGraphCanvas() {
  const data = state.memoryGraph;
  if (!data || !data.available) return;
  const el = document.getElementById('memory-graph-canvas');
  if (!el) return;
  try { await ensureCytoscapeLoaded(); } catch (e) {
    el.innerHTML = '<p class="muted" style="padding:20px">' + escapeHtml(e.message) + '</p>';
    return;
  }
  const nodes = (data.nodes || []).map(n => {
    const w = Number(n.salience || n.weight || 0.3) || 0.3;
    return {
      data: {
        id: n.id, label: n.label || n.id, kind: n.kind, color: _memoryGraphColor(n.kind),
        size: 16 + Math.min(30, w * 40),
        raw: n,
      }
    };
  });
  const edges = (data.edges || []).map((e, i) => ({
    data: {
      id: 'e' + i, source: e.src, target: e.dst,
      kind: e.kind, weight: e.weight,
      thickness: Math.max(1, Math.min(6, Number(e.weight || 1))),
    }
  }));
  if (_cytoscapeInstance) { try { _cytoscapeInstance.destroy(); } catch {} _cytoscapeInstance = null; }
  const theme = document.documentElement.getAttribute('data-theme') || 'dark';
  const labelColor = theme === 'light' ? '#1f2937' : '#e6e8ef';
  const labelOutline = theme === 'light' ? '#ffffff' : '#0f1115';
  const edgeColor = theme === 'light' ? '#9ca3af' : '#3b4252';
  const selectedBorder = theme === 'light' ? '#1f2937' : '#ffffff';
  _cytoscapeInstance = window.cytoscape({
    container: el,
    elements: { nodes, edges },
    style: [
      { selector: 'node', style: {
        'background-color': 'data(color)', 'label': 'data(label)',
        'color': labelColor, 'font-size': '10px', 'width': 'data(size)', 'height': 'data(size)',
        'text-valign': 'bottom', 'text-margin-y': 4, 'text-outline-width': 1, 'text-outline-color': labelOutline,
      }},
      { selector: 'edge', style: {
        'width': 'data(thickness)', 'line-color': edgeColor, 'curve-style': 'bezier',
        'opacity': 0.65, 'target-arrow-shape': 'none',
      }},
      { selector: 'node:selected', style: { 'border-width': 2, 'border-color': selectedBorder }},
    ],
    layout: { name: 'cose', animate: false, idealEdgeLength: 90, nodeRepulsion: 6000, padding: 30 },
    wheelSensitivity: 0.2,
  });
  _cytoscapeInstance.on('tap', 'node', evt => {
    const raw = evt.target.data('raw') || {};
    const detail = document.getElementById('memory-graph-detail');
    if (!detail) return;
    const lines = [
      `<strong>${escapeHtml(raw.label || raw.id)}</strong>`,
      raw.kind ? `<span class="tag">${escapeHtml(raw.kind)}</span>` : '',
      raw.memory_type ? `<span class="muted">type=${escapeHtml(raw.memory_type)}</span>` : '',
      raw.palace_zone ? `<span class="muted">zone=${escapeHtml(raw.palace_zone)}</span>` : '',
      typeof raw.salience === 'number' ? `<span class="muted">salience=${raw.salience.toFixed(2)}</span>` : '',
      raw.group_id ? `<span class="muted">group=${escapeHtml(raw.group_id)}</span>` : '',
    ].filter(Boolean);
    detail.innerHTML = lines.join(' · ');
  });
}

function resetMemoryGraphZoom() {
  if (_cytoscapeInstance) { _cytoscapeInstance.fit(); _cytoscapeInstance.center(); }
}

function exportMemoryGraphPNG() {
  if (!_cytoscapeInstance) return;
  try {
    const theme = document.documentElement.getAttribute('data-theme') || 'dark';
    const bg = theme === 'light' ? '#f6f8fb' : '#0f1115';
    const blob = _cytoscapeInstance.png({ output: 'blob', bg, full: true, scale: 2 });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'memory-palace.png';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (e) { alertFlash('err', '导出失败：' + e.message); }
}

function renderDashboard() {
  const d = state.dashboard;
  if (!d) return `<div class="card muted">加载中…</div>`;
  const total = d.total || {};
  const tabs = ["day","week","month"].map(w => `<button class="${state.dashboardWindow===w?'active':''}" onclick="switchDashboard('${w}')">${({day:'今日',week:'本周',month:'本月'})[w]}</button>`).join("");
  const byDay = (d.by_day || []).map(row => {
    const max = Math.max(...d.by_day.map(r => r.total_tokens || 1), 1);
    const w = ((row.total_tokens || 0) / max * 100).toFixed(1);
    return `<tr><td>${escapeHtml(row.bucket_day)}</td><td><div style="background:linear-gradient(90deg,var(--accent) ${w}%,transparent ${w}%);padding:4px 8px;border-radius:4px">${row.total_tokens.toLocaleString()}</div></td><td>${row.call_count}</td></tr>`;
  }).join("");
  const byModel = (d.by_model || []).map(row => `<tr><td>${escapeHtml(row.model)}</td><td>${row.total_tokens.toLocaleString()}</td><td>${row.call_count}</td></tr>`).join("");
  const byGroup = (d.by_group || []).map(row => `<tr><td>${escapeHtml(row.group_id)}</td><td>${row.total_tokens.toLocaleString()}</td><td>${row.call_count}</td></tr>`).join("");
  const empty = (total.total_tokens || 0) === 0;
  return `<div class="group-bar">${tabs}</div>
    <div class="card">
      <h2>总览（${escapeHtml(({day:'今日',week:'本周',month:'本月'})[state.dashboardWindow])}）</h2>
      <div class="row" style="gap:30px">
        <div><div class="muted">总 token</div><div style="font-size:24px;margin-top:4px">${(total.total_tokens||0).toLocaleString()}</div></div>
        <div><div class="muted">prompt</div><div style="font-size:18px;margin-top:4px">${(total.prompt_tokens||0).toLocaleString()}</div></div>
        <div><div class="muted">completion</div><div style="font-size:18px;margin-top:4px">${(total.completion_tokens||0).toLocaleString()}</div></div>
        <div><div class="muted">调用次数</div><div style="font-size:18px;margin-top:4px">${total.call_count||0}</div></div>
      </div>
      ${empty ? `<p class="muted" style="margin-top:14px">暂无数据。token 计量将在 LLM 调用拦截上线后开始记录（M6）。</p>` : ''}
    </div>
    <div class="card"><h2>按日期</h2><table><thead><tr><th>日期</th><th>总 token</th><th>调用</th></tr></thead><tbody>${byDay || `<tr><td colspan="3" class="muted">无</td></tr>`}</tbody></table></div>
    <div class="card"><h2>按模型</h2><table><thead><tr><th>模型</th><th>总 token</th><th>调用</th></tr></thead><tbody>${byModel || `<tr><td colspan="3" class="muted">无</td></tr>`}</tbody></table></div>
    <div class="card"><h2>按群</h2><table><thead><tr><th>群号</th><th>总 token</th><th>调用</th></tr></thead><tbody>${byGroup || `<tr><td colspan="3" class="muted">无</td></tr>`}</tbody></table></div>`;
}

async function switchDashboard(window) {
  state.dashboardWindow = window;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

function renderPersonas() {
  if (state.personasAvailable === false) return `<div class="card muted">profile_service 未就绪</div>`;
  if (state.selectedPersona) return renderPersonaDetail();
  const rows = state.personas.map(p => `<tr>
    <td><img class="avatar" src="https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(p.user_id)}&spec=100" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
    <td><code>${escapeHtml(p.user_id)}</code></td>
    <td>${escapeHtml(p.nickname || '')}</td>
    <td>${escapeHtml(p.snippet)}</td>
    <td>${p.updated_at ? new Date(p.updated_at*1000).toLocaleDateString() : '-'}</td>
    <td><button class="btn small" onclick="openPersona('${escapeAttr(p.user_id)}')">详情</button></td>
  </tr>`).join("");
  return `<div class="card"><h2>用户画像（${state.personas.length}）</h2>
    <table><thead><tr><th style="width:40px"></th><th>QQ</th><th>昵称</th><th>摘要</th><th>更新</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="6" class="muted">暂无画像</td></tr>'}</tbody></table></div>`;
}

async function openPersona(uid) {
  try {
    state.selectedPersona = await api("/personas/" + encodeURIComponent(uid));
    render();
  } catch (e) { alertFlash("err", e.message); }
}

function renderPersonaDetail() {
  const p = state.selectedPersona;
  const core = p.core_profile;
  const locals = (p.local_profiles || []).map(lp => `<div class="card" style="background:#0e1117">
    <div class="between"><strong>群 ${escapeHtml(lp.group_id)}</strong><span class="muted" style="font-size:12px">${new Date(lp.updated_at*1000).toLocaleString()}</span></div>
    <pre style="white-space:pre-wrap;margin:6px 0 0;font-family:inherit">${escapeHtml(lp.profile_text)}</pre>
  </div>`).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedPersona=null;render()">返回列表</button><span class="muted">用户 ${escapeHtml(p.user_id)}</span></div>
    <div class="card"><h2>全局印象</h2>${core ? `<pre style="white-space:pre-wrap;margin:0;font-family:inherit">${escapeHtml(core.profile_text || '')}</pre>` : '<p class="muted">无全局画像</p>'}</div>
    <h3 style="margin-bottom:10px">各群印象（${(p.local_profiles||[]).length}）</h3>
    ${locals || '<p class="muted">无各群画像</p>'}`;
}

function renderGroupSwitch() {
  const list = state.groupSwitches || [];
  const sourceLabel = {config_file:"配置文件", dynamic:"动态", group_config:"群配置", none:""};
  const rows = list.map(g => {
    const statusBadge = g.enabled
      ? `<span class="tag" style="background:rgba(52,211,153,0.18);color:var(--ok)">启用</span>`
      : `<span class="tag" style="background:rgba(248,113,113,0.12);color:var(--danger)">禁用</span>`;
    const srcTag = sourceLabel[g.source]
      ? `<span class="tag">${escapeHtml(sourceLabel[g.source])}</span>`
      : '';
    let actionBtn;
    if (g.readonly) {
      actionBtn = `<button class="btn small" disabled title="由配置文件固定，无法在此修改">固定启用</button>`;
    } else if (g.enabled) {
      actionBtn = `<button class="btn small danger" onclick="disableGroup('${escapeAttr(g.group_id)}')">禁用</button>`;
    } else {
      actionBtn = `<button class="btn small primary" onclick="enableGroup('${escapeAttr(g.group_id)}')">启用</button>`;
    }
    return `<tr>
      <td><img class="avatar" src="https://p.qlogo.cn/gh/${encodeURIComponent(g.group_id)}/${encodeURIComponent(g.group_id)}/100/" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
      <td><code>${escapeHtml(g.group_id)}</code></td>
      <td>${escapeHtml(g.group_name || '')}</td>
      <td>${statusBadge}${srcTag}</td>
      <td>${actionBtn}</td>
    </tr>`;
  }).join("");
  const enabledCount = list.filter(g => g.enabled).length;
  return `<div class="card">
    <div class="between" style="margin-bottom:14px">
      <h2 style="margin:0">群开关（${enabledCount} / ${list.length} 启用）</h2>
    </div>
    <table><thead><tr><th style="width:40px"></th><th>群号</th><th>群名</th><th>状态</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="muted">暂无群数据</td></tr>'}</tbody></table>
  </div>
  <div class="card">
    <h2>手动添加群到白名单</h2>
    <p class="muted" style="margin-bottom:10px">输入群号直接启用，适用于机器人还未在该群发言的情况。</p>
    <div class="row">
      <input type="text" id="newGroupIdInput" placeholder="群号" value="${escapeHtml(state.newGroupId)}" oninput="state.newGroupId=this.value" style="width:180px">
      <button class="btn primary" onclick="enableGroupNew()">添加并启用</button>
    </div>
  </div>`;
}

async function enableGroup(gid) {
  try {
    await api("/groups/" + encodeURIComponent(gid) + "/whitelist", { method: "POST" });
    alertFlash("ok", "已启用群 " + gid);
    const data = await api("/groups/whitelist");
    state.groupSwitches = data.groups;
    render();
  } catch (e) { alertFlash("err", e.message); }
}

async function disableGroup(gid) {
  try {
    await api("/groups/" + encodeURIComponent(gid) + "/whitelist", { method: "DELETE" });
    alertFlash("ok", "已禁用群 " + gid);
    const data = await api("/groups/whitelist");
    state.groupSwitches = data.groups;
    render();
  } catch (e) { alertFlash("err", e.message); }
}

async function enableGroupNew() {
  const gid = (state.newGroupId || "").trim();
  if (!gid) { alertFlash("err", "请输入群号"); return; }
  await enableGroup(gid);
  state.newGroupId = "";
}

function renderGroups() {
  if (state.groupsAvailable === false) return `<div class="card muted">profile_service 未就绪</div>`;
  if (state.selectedGroup) return renderGroupDetail();
  const sourceLabel = {memory:"已积累", group_config:"群配置", config_file:"配置白名单", dynamic:"动态白名单", unknown:""};
  const rows = state.groupList.map(g => {
    const srcKey = g.source || (g.has_memory ? 'memory' : '');
    const srcTag = sourceLabel[srcKey]
      ? `<span class="tag" style="font-size:11px">${escapeHtml(sourceLabel[srcKey])}</span>`
      : '';
    const memTag = g.has_memory === false
      ? `<span class="tag" style="background:rgba(245,158,11,0.12);color:var(--warn);font-size:11px">无数据</span>`
      : '';
    return `<tr>
      <td><img class="avatar" src="https://p.qlogo.cn/gh/${encodeURIComponent(g.group_id)}/${encodeURIComponent(g.group_id)}/100/" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
      <td><code>${escapeHtml(g.group_id)}</code></td>
      <td>${escapeHtml(g.group_name || '')} ${srcTag} ${memTag}</td>
      <td><button class="btn small" onclick="openGroup('${escapeAttr(g.group_id)}')">查看</button></td>
    </tr>`;
  }).join("");
  return `<div class="card"><h2>群列表（${state.groupList.length}）</h2>
    <p class="muted" style="font-size:12px;margin-top:0">同时显示已建立记忆的群和白名单中的群（包括关闭搜索可找到的群）。</p>
    <table><thead><tr><th style="width:40px"></th><th>群号</th><th>群名</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="4" class="muted">暂无群数据</td></tr>'}</tbody></table></div>`;
}

async function openGroup(gid) {
  try {
    state.selectedGroup = gid;
    state.groupRawChat = null;
    const [personas, style, knowledge, memes, agentState] = await Promise.all([
      api("/groups/" + encodeURIComponent(gid) + "/personas"),
      api("/groups/" + encodeURIComponent(gid) + "/style"),
      api("/groups/" + encodeURIComponent(gid) + "/knowledge").catch(() => ({knowledge: [], autobuild_status: null})),
      api("/groups/" + encodeURIComponent(gid) + "/memes").catch(() => ({memes: []})),
      api("/groups/" + encodeURIComponent(gid) + "/agent-state").catch(() => null),
    ]);
    state.groupPersonas = personas.profiles;
    state.groupStyle = style;
    state.groupKnowledge = knowledge.knowledge || [];
    state.groupKnowledgeAutobuild = knowledge.autobuild_status || null;
    state.groupMemes = memes.memes || [];
    state.groupAgentState = agentState;
    render();
  } catch (e) { alertFlash("err", e.message); }
}

async function rebuildGroupKnowledge() {
  const gid = state.selectedGroup;
  if (!gid) return;
  if (state.groupKnowledgeRebuilding) return;
  state.groupKnowledgeRebuilding = true; render();
  try {
    const out = await api("/groups/" + encodeURIComponent(gid) + "/knowledge/rebuild", { method:"POST", headers:{"content-type":"application/json"}, body: "{}" });
    alertFlash("ok", "已重建群知识库，新增 " + (out.saved || 0) + " 条");
    const knowledge = await api("/groups/" + encodeURIComponent(gid) + "/knowledge");
    state.groupKnowledge = knowledge.knowledge || [];
    state.groupKnowledgeAutobuild = knowledge.autobuild_status || null;
  } catch (e) { alertFlash("err", "重建失败：" + e.message); }
  state.groupKnowledgeRebuilding = false; render();
}

async function loadGroupRawChat() {
  const gid = state.selectedGroup;
  if (!gid) return;
  try {
    const data = await api("/memory/raw-chat?group_id=" + encodeURIComponent(gid) + "&limit=80");
    state.groupRawChat = data;
    render();
  } catch (e) { alertFlash("err", "加载对话原文失败：" + e.message); }
}

function renderGroupAgentState() {
  const s = state.groupAgentState;
  if (!s) return '';
  const emo = s.emotion || {};
  const stats = s.stats || {};
  const memories = s.recent_memories || [];
  const edges = s.top_edges || [];
  const lastAct = stats.last_activity_at ? new Date(stats.last_activity_at*1000).toLocaleString() : '-';
  const emoSummary = emo.summary || '（暂无群情绪记忆）';
  const inner = emo.global_inner_state || '';
  const memBlock = memories.length
    ? `<table style="font-size:12.5px"><thead><tr><th>类型</th><th>摘要</th><th>显著度</th><th>更新</th></tr></thead><tbody>${
        memories.map(m => `<tr>
          <td><span class="tag">${escapeHtml(m.memory_type || '')}</span></td>
          <td>${escapeHtml(m.summary || '')}</td>
          <td class="muted">${Number(m.salience||0).toFixed(2)}</td>
          <td class="muted">${m.updated_at ? new Date(m.updated_at*1000).toLocaleDateString() : '-'}</td>
        </tr>`).join('')
      }</tbody></table>`
    : '<p class="muted" style="margin:6px 0 0">暂无显著记忆条目</p>';
  const edgeBlock = edges.length
    ? `<table style="font-size:12.5px"><thead><tr><th>关系</th><th>类型</th><th>权重</th><th>最近</th></tr></thead><tbody>${
        edges.map(e => `<tr>
          <td><code>${escapeHtml(e.src)}</code> → <code>${escapeHtml(e.dst)}</code></td>
          <td><span class="tag">${escapeHtml(e.kind)}</span></td>
          <td>${Number(e.weight||0).toFixed(2)}</td>
          <td class="muted">${e.last_seen_at ? new Date(e.last_seen_at*1000).toLocaleDateString() : '-'}</td>
        </tr>`).join('')
      }</tbody></table>`
    : '<p class="muted" style="margin:6px 0 0">暂无显著关系边</p>';
  return `<div class="card"><h2>Agent 状态</h2>
    <div class="row" style="gap:14px;flex-wrap:wrap;margin-bottom:12px">
      <div style="flex:1;min-width:260px"><div class="muted" style="font-size:12px">群情绪</div><div>${escapeHtml(emoSummary)}</div></div>
      <div style="flex:1;min-width:260px"><div class="muted" style="font-size:12px">Bot 内心基线</div><div>${escapeHtml(inner || '—')}</div></div>
      <div style="min-width:160px"><div class="muted" style="font-size:12px">消息总数</div><div>${stats.message_count || 0}</div></div>
      <div style="min-width:200px"><div class="muted" style="font-size:12px">最近活跃</div><div>${escapeHtml(lastAct)}</div></div>
    </div>
    <details><summary class="muted" style="cursor:pointer">显著记忆 Top-${memories.length}</summary>${memBlock}</details>
    <details style="margin-top:8px"><summary class="muted" style="cursor:pointer">群内关系 Top-${edges.length}</summary>${edgeBlock}</details>
  </div>`;
}

function renderGroupKnowledgeCard() {
  const knowledge = state.groupKnowledge || [];
  const auto = state.groupKnowledgeAutobuild || null;
  const rebuilding = state.groupKnowledgeRebuilding;
  const knowledgeRows = knowledge.map(k => `<tr>
    <td><strong>${escapeHtml(k.term)}</strong></td>
    <td>${escapeHtml(k.definition)}</td>
    <td><span class="tag">${escapeHtml(k.memory_type || k.source_kind || '')}</span></td>
    <td class="muted" style="font-size:12px">${k.updated_at ? new Date(k.updated_at*1000).toLocaleDateString() : '-'}</td>
  </tr>`).join("");
  let autoLine = '';
  if (auto) {
    const lastRun = auto.last_run_at ? new Date(auto.last_run_at*1000).toLocaleString() : '从未运行';
    const flag = auto.enabled ? '已启用' : '已禁用';
    autoLine = `<p class="muted" style="font-size:12px;margin:4px 0 10px">
      自动构建：${flag} · 上次运行 ${escapeHtml(lastRun)} · 今日 ${auto.daily_count||0}/${auto.daily_limit||0} 次 · 每 ${auto.interval_hours||0}h · 阈值 ${auto.min_messages_threshold||0} 条
      ${auto.daily_limit_hit ? '<span class="tag" style="background:rgba(245,158,11,0.18);color:var(--warn)">今日已满</span>' : ''}
    </p>`;
  }
  return `<div class="card">
    <div class="between"><h2 style="margin:0">群知识库（${knowledge.length}）</h2>
      <button class="btn small ${rebuilding?'':'primary'}" onclick="rebuildGroupKnowledge()" ${rebuilding?'disabled':''}>${rebuilding?'重建中…':'立即重建'}</button>
    </div>
    ${autoLine}
    ${knowledgeRows ? `<table><thead><tr><th>术语</th><th>解释</th><th>类型</th><th>更新</th></tr></thead><tbody>${knowledgeRows}</tbody></table>` : '<p class="muted">暂无群知识。可点击「立即重建」手动触发分析，或开启「群知识库自动构建」后等待定时扫描。</p>'}
  </div>`;
}

function renderGroupDetail() {
  const gid = state.selectedGroup;
  const rows = state.groupPersonas.map(p => {
    const em = p.latest_emotion || {};
    const emoCol = em.user_attitude || em.bot_emotion
      ? `<div style="font-size:11.5px;line-height:1.5">
          ${em.user_attitude ? `<div class="muted">态度: ${escapeHtml(em.user_attitude)}</div>` : ''}
          ${em.bot_emotion ? `<div class="muted">回应: ${escapeHtml(em.bot_emotion)}</div>` : ''}
        </div>`
      : '<span class="muted">—</span>';
    return `<tr>
      <td><img class="avatar" src="https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(p.user_id)}&spec=100" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
      <td><code>${escapeHtml(p.user_id)}</code></td>
      <td>${escapeHtml(p.nickname || '')}</td>
      <td>${escapeHtml(p.snippet)}</td>
      <td>${emoCol}</td>
      <td>${p.updated_at ? new Date(p.updated_at*1000).toLocaleDateString() : '-'}</td>
    </tr>`;
  }).join("");
  const style = state.groupStyle || {};
  const memeRows = (state.groupMemes || []).map(m => `<tr>
    <td><strong>${escapeHtml(m.term)}</strong></td>
    <td>${escapeHtml(m.meaning)}</td>
    <td>${escapeHtml((m.aliases||[]).join("、"))}</td>
    <td class="muted" style="font-size:12px">${escapeHtml(m.scope || '')}/${escapeHtml(m.risk_level || '')}/${Number(m.confidence||0).toFixed(2)}</td>
  </tr>`).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedGroup=null;state.groupRawChat=null;state.groupStyleSnapIdx=0;render()">返回列表</button><span class="muted">群 ${escapeHtml(gid)}</span></div>
    ${renderGroupAgentState()}
    ${renderGroupStyle(style)}
    ${renderGroupKnowledgeCard()}
    <div class="card"><h2>梗词典 / 概念锚点（${(state.groupMemes||[]).length}）</h2>
      ${memeRows ? `<table><thead><tr><th>词条</th><th>含义</th><th>别名</th><th>范围/风险/置信度</th></tr></thead><tbody>${memeRows}</tbody></table>` : '<p class="muted">暂无匹配词条，公共热梗种子会在首次查询后自动初始化。</p>'}</div>
    <div class="card"><h2>群内成员画像（${state.groupPersonas.length}）</h2>
      <table><thead><tr><th style="width:40px"></th><th>QQ</th><th>昵称</th><th>摘要</th><th>近期情绪</th><th>更新</th></tr></thead><tbody>${rows||'<tr><td colspan="6" class="muted">无</td></tr>'}</tbody></table></div>
    ${renderGroupRawChat()}`;
}

function renderGroupStyle(style) {
  const snapshots = (style && style.snapshots) || [];
  const idx = Math.min(state.groupStyleSnapIdx || 0, Math.max(0, snapshots.length - 1));
  const active = snapshots[idx];
  const rebuilding = state.groupStyleRebuilding;
  if (!snapshots.length) {
    return `<div class="card"><h2>群风格</h2>
      <p class="muted">暂无群风格快照。可手动触发分析（需该群至少有 20 条对话历史）。</p>
      <button class="btn ${rebuilding?'':'primary'}" onclick="rebuildGroupStyle()" ${rebuilding?'disabled':''}>${rebuilding?'分析中…':'立即分析风格'}</button></div>`;
  }
  const tabs = snapshots.map((s, i) => {
    const dt = new Date(s.created_at * 1000).toLocaleString();
    return `<button class="${i===idx?'active':''}" onclick="state.groupStyleSnapIdx=${i};render()">${i===0?'最新':'#'+(i+1)} <span class="muted" style="font-size:11px">${dt}</span></button>`;
  }).join("");
  const styleJson = active.style_json || {};
  const detailRows = ["tone","pace","catchphrases","taboos","typical_length"].map(k => {
    const label = ({tone:"语气",pace:"节奏",catchphrases:"口头禅",taboos:"禁忌",typical_length:"典型句长"})[k];
    let value = styleJson[k];
    if (Array.isArray(value)) value = value.join("、") || "—";
    if (!value) value = "—";
    return `<tr><td class="muted" style="width:80px">${escapeHtml(label)}</td><td>${escapeHtml(String(value))}</td></tr>`;
  }).join("");
  return `<div class="card"><div class="between"><h2 style="margin:0">群风格（${snapshots.length} 个快照）</h2>
    <button class="btn small ${rebuilding?'':'primary'}" onclick="rebuildGroupStyle()" ${rebuilding?'disabled':''}>${rebuilding?'分析中…':'立即重新分析'}</button></div>
    <div class="group-bar" style="margin-top:10px">${tabs}</div>
    <table style="margin-top:8px"><tbody>${detailRows}</tbody></table>
    ${active.style_text ? `<details style="margin-top:8px"><summary class="muted" style="cursor:pointer;font-size:12px">展示原始 prompt 段</summary>
      <pre style="white-space:pre-wrap;margin:8px 0 0;font-family:inherit;font-size:12.5px">${escapeHtml(active.style_text)}</pre></details>` : ''}
  </div>`;
}

async function rebuildGroupStyle() {
  const gid = state.selectedGroup;
  if (!gid) return;
  state.groupStyleRebuilding = true; render();
  try {
    const out = await api("/groups/" + encodeURIComponent(gid) + "/style/rebuild", { method:"POST", headers:{"content-type":"application/json"}, body: "{}" });
    state.groupStyle = { ...state.groupStyle, snapshots: out.snapshots };
    state.groupStyleSnapIdx = 0;
    alertFlash("ok", "已生成新群风格快照");
  } catch (e) { alertFlash("err", "分析失败：" + e.message); }
  state.groupStyleRebuilding = false; render();
}

function renderGroupRawChat() {
  const chat = state.groupRawChat;
  if (!chat) {
    return `<div class="card"><h2>对话原文</h2>
      <p class="muted" style="margin:0 0 10px">本群在 chat_history.db 里的原始消息流（未经蒸馏）。点击下方按钮按需加载。</p>
      <button class="btn" onclick="loadGroupRawChat()">加载最近 80 条</button></div>`;
  }
  if (!chat.available) {
    return `<div class="card muted"><h2>对话原文</h2>memory_store 未就绪</div>`;
  }
  if (!chat.messages.length) {
    return `<div class="card"><h2>对话原文</h2><p class="muted">该群没有任何消息记录（chat_history.db 不存在或为空）</p></div>`;
  }
  // 反转为时间正序，看着更自然
  const ordered = [...chat.messages].reverse();
  const rows = ordered.map(m => {
    const isBot = m.role === "assistant";
    const tag = isBot ? '<span class="tag" style="background:rgba(106,168,255,0.18);color:var(--accent)">bot</span>' : '<span class="tag">user</span>';
    const sender = m.sender_name || m.user_id || '匿名';
    const time = m.created_at ? new Date(m.created_at*1000).toLocaleString() : '-';
    return `<tr><td style="white-space:nowrap">${tag}</td>
      <td class="muted" style="font-size:12px;white-space:nowrap">${escapeHtml(sender)}</td>
      <td>${escapeHtml(m.text)}</td>
      <td class="muted" style="font-size:11px;white-space:nowrap">${escapeHtml(time)}</td></tr>`;
  }).join("");
  return `<div class="card"><h2>对话原文（${chat.messages.length}）</h2>
    <p class="muted" style="font-size:12px;margin:-6px 0 10px">按时间正序显示；不参与 LLM 上下文，仅供管理员查看。</p>
    <table><thead><tr><th></th><th>发送者</th><th>内容</th><th>时间</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div style="margin-top:10px">
      <button class="btn small" onclick="state.groupRawChat=null;render()">收起</button>
      <button class="btn small" onclick="loadGroupRawChat()">刷新</button>
    </div>
  </div>`;
}

function renderSkills() {
  if (state.skillsAvailable === false) return `<div class="card muted">tool_registry 未就绪</div>`;
  const search = (state.skillFilter || "").trim().toLowerCase();
  const items = search ? state.skills.filter(s => s.name.toLowerCase().includes(search) || (s.description||"").toLowerCase().includes(search)) : state.skills;
  const rows = items.map(s => {
    const active = s.enabled_by_config && !s.user_disabled;
    return `<tr>
      <td><strong>${escapeHtml(s.name)}</strong>${s.category ? ` <span class="tag">${escapeHtml(s.category)}</span>` : ''}</td>
      <td class="muted" style="font-size:12.5px">${escapeHtml((s.description||"").slice(0,140))}</td>
      <td>${active ? '<span class="tag" style="background:rgba(52,211,153,0.18);color:var(--ok)">启用</span>' : '<span class="tag" style="background:rgba(248,113,113,0.18);color:var(--danger)">禁用</span>'}</td>
      <td>
        <div class="toggle">
          <button class="${!s.user_disabled?'on':''}" onclick="toggleSkill('${escapeAttr(s.name)}', false)">开</button>
          <button class="${s.user_disabled?'on':''}" onclick="toggleSkill('${escapeAttr(s.name)}', true)">关</button>
        </div>
      </td>
    </tr>`;
  }).join("");
  return `<div class="toolbar">
      <input id="skill-filter-input" type="search" placeholder="搜索 skill 名称…" value="${escapeAttr(state.skillFilter)}" oninput="state.skillFilter=this.value;render()" style="flex:1;max-width:340px">
      <span class="muted">共 ${state.skills.length} 个 skill</span>
    </div>
    <div class="card"><h2>Skill 启停</h2>
      <table><thead><tr><th>名称</th><th>说明</th><th>状态</th><th>开关</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="muted">无 skill</td></tr>'}</tbody></table>
      <p class="muted" style="margin-top:10px;font-size:12px">仅启停内置 skillpack；新增 skillpack 仍需在文件系统放入 plugin/personification/skills/skillpacks/ 后重启。</p>
    </div>`;
}

async function toggleSkill(name, disabled) {
  try {
    await api(`/skills/${encodeURIComponent(name)}/toggle`, { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({disabled}) });
    alertFlash("ok", `${name} 已${disabled?'禁用':'启用'}`);
    await loadView(); render();
  } catch (e) { alertFlash("err", "切换失败：" + e.message); }
}

function renderPluginKnowledge() {
  if (state.pluginKnowledgeAvailable === false) return `<div class="card muted">knowledge_store 未就绪</div>`;
  if (state.selectedPluginKnowledge) return renderPluginKnowledgeDetail();
  const list = state.pluginKnowledgeList || [];
  const searchResults = state.pluginKnowledgeSearchResults;
  const matchedSet = searchResults ? new Set(searchResults.results || []) : null;
  const displayList = matchedSet ? list.filter(p => matchedSet.has(p.plugin_name)) : list;
  const rows = displayList.map(p => `<tr>
    <td><strong>${escapeHtml(p.display_name || p.plugin_name)}</strong>${p.category ? ` <span class="tag">${escapeHtml(p.category)}</span>` : ''}</td>
    <td><code>${escapeHtml(p.plugin_name)}</code></td>
    <td>${escapeHtml(p.summary || '')}</td>
    <td class="muted" style="font-size:12px">
      ${p.has_runtime_data ? '<span class="tag">runtime</span>' : ''}
      ${p.has_source_data ? `<span class="tag">source(${p.source_file_count}f/${p.source_chunk_count}c)</span>` : ''}
    </td>
    <td><button class="btn small" onclick="openPluginKnowledge('${escapeAttr(p.plugin_name)}')">详情</button></td>
  </tr>`).join("");
  const searchInfo = matchedSet ? `<div class="muted" style="margin-bottom:8px">搜索 "${escapeHtml(state.pluginKnowledgeSearchQ || '')}" 命中 ${matchedSet.size} 条 <button class="btn small" onclick="clearPluginKnowledgeSearch()">清除</button></div>` : '';
  return `<div class="card">
    <div class="row" style="margin-bottom:12px;gap:8px;align-items:center">
      <input id="pk-search-input" placeholder="按插件名/关键词/摘要搜索" value="${escapeAttr(state.pluginKnowledgeSearchQ || '')}" onkeydown="if(event.key==='Enter')triggerPluginKnowledgeSearch()" style="flex:1">
      <button class="btn" onclick="triggerPluginKnowledgeSearch()">搜索</button>
    </div>
    ${searchInfo}
    <h2>插件知识库（${displayList.length} / ${list.length}）</h2>
    <table><thead><tr><th>名称</th><th>plugin_name</th><th>摘要</th><th>数据</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="muted">暂无插件知识，等待自动构建或手动触发。</td></tr>'}</tbody></table>
  </div>`;
}

async function triggerPluginKnowledgeSearch() {
  const input = document.getElementById("pk-search-input");
  const q = (input ? input.value : "").trim();
  state.pluginKnowledgeSearchQ = q;
  if (!q) { state.pluginKnowledgeSearchResults = null; render(); return; }
  try {
    state.pluginKnowledgeSearchResults = await api("/plugin-knowledge/search?" + new URLSearchParams({q, top_k: "30"}).toString());
    render();
  } catch (e) { alertFlash("err", e.message); }
}

function clearPluginKnowledgeSearch() {
  state.pluginKnowledgeSearchQ = "";
  state.pluginKnowledgeSearchResults = null;
  render();
}

async function openPluginKnowledge(name) {
  try {
    state.selectedPluginKnowledge = await api("/plugin-knowledge/detail/" + encodeURIComponent(name));
    render();
  } catch (e) { alertFlash("err", e.message); }
}

function renderPluginKnowledgeDetail() {
  const d = state.selectedPluginKnowledge;
  const e = d.entry || {};
  const features = Array.isArray(e.features) ? e.features : [];
  const featureRows = features.map(f => {
    if (typeof f === "string") return `<li>${escapeHtml(f)}</li>`;
    const name = f.name || f.feature || "";
    const desc = f.description || f.desc || "";
    return `<li><strong>${escapeHtml(name)}</strong>${desc ? `：${escapeHtml(desc)}` : ''}</li>`;
  }).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedPluginKnowledge=null;render()">返回列表</button><span class="muted">插件 ${escapeHtml(d.plugin_name)}</span></div>
    <div class="card">
      <h2>${escapeHtml(e.display_name || d.plugin_name)} <code style="font-size:13px;color:var(--muted)">${escapeHtml(d.plugin_name)}</code></h2>
      ${e.summary ? `<p>${escapeHtml(e.summary)}</p>` : ''}
      ${(e.keywords && e.keywords.length) ? `<div style="margin:6px 0">${e.keywords.map(k => `<span class="tag">${escapeHtml(k)}</span>`).join("")}</div>` : ''}
      ${e.architecture_summary ? `<h3>架构摘要</h3><pre style="white-space:pre-wrap;margin:0;font-family:inherit">${escapeHtml(e.architecture_summary)}</pre>` : ''}
      ${features.length ? `<h3>功能列表</h3><ul>${featureRows}</ul>` : ''}
      <details style="margin-top:12px"><summary class="muted">完整 JSON</summary><pre style="white-space:pre-wrap;font-size:12px;background:#0b0d12;padding:10px;border-radius:6px;overflow-x:auto">${escapeHtml(JSON.stringify(e, null, 2))}</pre></details>
    </div>`;
}

function renderTest() {
  const r = state.testResult;
  return `<div class="card">
    <h2>模型调用测试</h2>
    <label class="muted">system prompt</label>
    <textarea oninput="state.testSystem=this.value" style="width:100%;min-height:60px;margin:6px 0">${escapeHtml(state.testSystem)}</textarea>
    <label class="muted">用户消息</label>
    <textarea oninput="state.testPrompt=this.value" style="width:100%;min-height:80px;margin:6px 0">${escapeHtml(state.testPrompt)}</textarea>
    <div class="row" style="margin-top:10px"><button class="btn primary" onclick="runTest()">发送</button>${state.testLoading?'<span class="muted">调用中…</span>':''}</div>
  </div>
  ${r ? `<div class="card"><h2>响应</h2>
    <div class="row muted" style="font-size:12px;margin-bottom:8px">
      <span>模型 <code>${escapeHtml(r.model_used||'未知')}</code></span>
      <span>finish=${escapeHtml(r.finish_reason||'')}</span>
      <span>${r.duration_ms}ms</span>
      <span>tokens prompt=${r.usage?.prompt_tokens||0} completion=${r.usage?.completion_tokens||0}</span>
    </div>
    <pre style="white-space:pre-wrap;margin:0;font-family:inherit">${escapeHtml(r.content||'(无内容)')}</pre>
  </div>` : ''}`;
}

async function runTest() {
  state.testLoading = true; render();
  try {
    state.testResult = await api("/test/chat", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({prompt: state.testPrompt, system: state.testSystem}) });
  } catch (e) { alertFlash("err", "调用失败：" + e.message); }
  state.testLoading = false; render();
}

function renderConfig() {
  const search = (state.configSearch || "").trim().toLowerCase();
  let items = state.entries;
  let activeGroup = state.activeGroup;
  if (search) {
    items = items.filter(e =>
      e.field_name.toLowerCase().includes(search)
      || (e.label || "").toLowerCase().includes(search)
      || (e.description || "").toLowerCase().includes(search)
    );
    activeGroup = null;
  } else if (activeGroup) {
    items = items.filter(e => e.group === activeGroup);
  }
  // advanced 折叠：默认隐藏 advanced=true 字段
  const totalBeforeAdvanced = items.length;
  if (!state.showAdvancedConfig) {
    items = items.filter(e => !e.advanced);
  }
  const hiddenAdvanced = totalBeforeAdvanced - items.length;
  const groupBar = !search ? state.groups.map(g => {
    const groupEntries = state.entries.filter(e => e.group === g);
    const visibleCount = state.showAdvancedConfig ? groupEntries.length : groupEntries.filter(e => !e.advanced).length;
    return `<button class="${g===activeGroup?'active':''}" onclick="pickGroup('${escapeAttr(g)}')">${escapeHtml(g)} <span class="muted" style="font-size:11px">${visibleCount}/${groupEntries.length}</span></button>`;
  }).join("") : "";
  const heading = search ? `搜索结果（${items.length}）` : (activeGroup || '配置');
  return `<div class="toolbar">
      <input id="config-search-input" type="search" placeholder="搜索字段名 / 标签 / 描述…" value="${escapeAttr(state.configSearch)}" oninput="state.configSearch=this.value;render()" style="flex:1;max-width:340px">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
        <input type="checkbox" ${state.showAdvancedConfig?'checked':''} onchange="state.showAdvancedConfig=this.checked;render()" style="width:auto">
        显示高级配置
      </label>
      <button class="btn" onclick="applyRecommended()">应用推荐默认值</button>
    </div>
    ${groupBar ? `<div class="group-bar">${groupBar}</div>` : ''}
    <div class="card">
      <h2>${escapeHtml(heading)} ${hiddenAdvanced ? `<span class="muted" style="font-size:12px;font-weight:normal">（已折叠 ${hiddenAdvanced} 项高级配置）</span>` : ''}</h2>
      ${items.length ? items.map(renderField).join("") : '<p class="muted">无匹配字段</p>'}
    </div>`;
}

async function applyRecommended() {
  if (!confirm("将一组推荐配置写入 .env.prod 与 env.json，覆盖现有值。继续？")) return;
  try {
    const result = await api("/config/apply-recommended", { method:"POST", headers:{"content-type":"application/json"}, body: "{}" });
    const lines = [`已应用 ${result.applied.length} 项`];
    if (result.skipped.length) lines.push(`跳过 ${result.skipped.length}：` + result.skipped.map(s=>s.field_name).slice(0,3).join("、"));
    alertFlash("ok", lines.join("；"));
    await loadView(); render();
  } catch (e) { alertFlash("err", "应用失败：" + e.message); }
}

function renderField(e) {
  const tags = [];
  if (e.required) tags.push(`<span class="tag required">必填</span>`);
  if (e.secret) tags.push(`<span class="tag secret">敏感</span>`);
  if (e.advanced) tags.push(`<span class="tag">高级</span>`);
  tags.push(`<span class="tag source-${escapeAttr(e.active_source)}">当前来源：${activeSourceLabel(e.active_source)}</span>`);
  const inputHtml = renderInput(e);
  const defaultLine = e.default !== null && e.default !== "" && !e.secret ? `<div class="muted" style="font-size:12px;margin-top:6px">默认值：<code>${escapeHtml(JSON.stringify(e.default))}</code></div>` : '';
  const exampleLine = e.example ? `<div class="muted" style="font-size:12px;margin-top:4px">示例：<code>${escapeHtml(e.example)}</code></div>` : '';
  return `<div class="field" data-field="${escapeAttr(e.field_name)}">
    <div class="field-head"><strong>${escapeHtml(e.label)}</strong><code>${escapeHtml(e.field_name)}</code>${tags.join("")}</div>
    <div class="field-desc">${escapeHtml(e.description)}</div>
    <div class="field-input">${inputHtml}</div>
    ${defaultLine}
    ${exampleLine}
  </div>`;
}

function renderInput(e) {
  const cur = e.current;
  if (e.field_name === "personification_api_pools") {
    return renderApiPoolEditor(e);
  }
  if (e.kind === "toggle") {
    const on = cur === true || cur === "true" || cur === 1;
    return `<div class="toggle">
      <button class="${on?'on':''}" onclick="saveField('${escapeAttr(e.field_name)}', true)">开</button>
      <button class="${!on?'on':''}" onclick="saveField('${escapeAttr(e.field_name)}', false)">关</button>
    </div>`;
  }
  if (e.kind === "select") {
    const opts = e.choices.map(c => `<option value="${escapeAttr(c)}" ${cur===c?'selected':''}>${escapeHtml(c)}</option>`).join("");
    return `<select onchange="saveField('${escapeAttr(e.field_name)}', this.value)">${opts}</select>`;
  }
  if (e.kind === "json") {
    const text = cur == null ? "" : (typeof cur === "string" ? cur : JSON.stringify(cur, null, 2));
    return `<textarea data-raw="json" oninput="markDirty(this)">${escapeHtml(text)}</textarea>
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'json')">保存</button>`;
  }
  if (e.kind === "int") {
    return `<input type="number" step="1" value="${escapeAttr(cur==null?'':cur)}" oninput="markDirty(this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'int')">保存</button>`;
  }
  if (e.kind === "float") {
    return `<input type="number" step="0.01" value="${escapeAttr(cur==null?'':cur)}" oninput="markDirty(this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'float')">保存</button>`;
  }
  if (e.kind === "secret") {
    return `<input type="password" placeholder="${cur ? '已设置（输入新值覆盖）' : '未设置'}" oninput="markDirty(this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'secret')">保存</button>`;
  }
  return `<input type="text" value="${escapeAttr(cur==null?'':cur)}" oninput="markDirty(this)">
    <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'text')">保存</button>`;
}

function normalizeApiPoolValue(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value.trim());
      return Array.isArray(parsed) ? parsed : [];
    } catch { return []; }
  }
  return [];
}

function defaultApiProvider(index) {
  return {
    name: `provider_${index + 1}`,
    api_type: "openai",
    api_url: "",
    api_key: "",
    model: "",
    auth_path: "",
    project: "",
    proxy: "",
    timeout: 60,
    priority: index,
    enabled: true,
  };
}

function apiProviderFieldVisible(apiType, field) {
  const type = (apiType || "openai").replaceAll("-", "_");
  if (["openai_codex", "codex", "claude_code", "claude_cli"].includes(type)) {
    return !["api_url", "api_key", "project"].includes(field);
  }
  if (["gemini_cli", "antigravity_cli", "agy", "agy_cli"].includes(type)) {
    return !["api_url", "api_key"].includes(field);
  }
  return !["auth_path", "project"].includes(field);
}

function renderApiPoolEditor(e) {
  const providers = normalizeApiPoolValue(e.current);
  const cards = providers.map((provider, index) => renderApiProviderCard(e.field_name, provider || {}, index)).join("");
  return `<div class="api-pool-editor" data-api-pool-field="${escapeAttr(e.field_name)}">
    <div class="api-provider-actions">
      <button class="btn small" onclick="addApiProvider('${escapeAttr(e.field_name)}')">+ 添加 Provider</button>
      <button class="btn small primary" onclick="saveApiPool('${escapeAttr(e.field_name)}')">保存全部</button>
      <button class="btn small" onclick="toggleApiPoolRaw(this)">查看 JSON</button>
    </div>
    <div class="api-provider-list">${cards || '<div class="api-pool-empty">暂无 provider，点击“添加 Provider”创建。</div>'}</div>
    <textarea data-api-pool-raw style="display:none;min-height:120px" oninput="markDirty(this)">${escapeHtml(JSON.stringify(providers, null, 2))}</textarea>
  </div>`;
}

function renderApiProviderCard(field, provider, index) {
  const apiType = provider.api_type || "openai";
  const choices = ["openai", "openai_codex", "gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code"];
  const typeOptions = choices.map(c => `<option value="${escapeAttr(c)}" ${apiType===c?'selected':''}>${escapeHtml(c)}</option>`).join("");
  const fieldHtml = (name, label, type = "text", extra = "") => {
    if (!apiProviderFieldVisible(apiType, name)) return "";
    const value = provider[name] == null ? "" : provider[name];
    return `<div class="api-provider-field" data-provider-field="${escapeAttr(name)}">
      <label>${escapeHtml(label)}</label>
      <input type="${escapeAttr(type)}" value="${escapeAttr(value)}" ${extra}>
    </div>`;
  };
  return `<div class="api-provider-card" data-provider-index="${index}">
    <div class="api-provider-head">
      <div class="api-provider-title">Provider ${index + 1}</div>
      <button class="btn small danger" onclick="removeApiProvider('${escapeAttr(field)}', ${index})">删除</button>
    </div>
    <div class="api-provider-grid">
      ${fieldHtml("name", "名称")}
      <div class="api-provider-field" data-provider-field="priority">
        <label>优先级</label>
        <input type="number" step="1" value="${escapeAttr(provider.priority ?? index)}">
      </div>
      <div class="api-provider-field" data-provider-field="api_type">
        <label>类型</label>
        <select onchange="refreshApiPoolEditor('${escapeAttr(field)}')">${typeOptions}</select>
      </div>
      ${fieldHtml("api_url", "API URL")}
      ${fieldHtml("api_key", "API Key", "password")}
      ${fieldHtml("model", "模型")}
      ${fieldHtml("auth_path", "Auth Path")}
      ${fieldHtml("project", "Project")}
      ${fieldHtml("proxy", "代理")}
      ${fieldHtml("timeout", "超时（秒）", "number", 'step="1"')}
      <div class="api-provider-field" data-provider-field="enabled">
        <label>启用</label>
        <select>
          <option value="true" ${provider.enabled !== false ? 'selected' : ''}>是</option>
          <option value="false" ${provider.enabled === false ? 'selected' : ''}>否</option>
        </select>
      </div>
    </div>
  </div>`;
}

function readApiPoolEditor(field) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return [];
  const raw = root.querySelector("[data-api-pool-raw]");
  if (raw && raw.style.display !== "none" && raw.value.trim()) {
    try {
      const parsed = JSON.parse(raw.value);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      throw new Error("API Pool JSON 格式错误");
    }
  }
  return Array.from(root.querySelectorAll(".api-provider-card")).map((card, index) => {
    const provider = defaultApiProvider(index);
    card.querySelectorAll("[data-provider-field]").forEach(wrap => {
      const name = wrap.dataset.providerField;
      const input = wrap.querySelector("input, select");
      if (!input) return;
      let value = input.value;
      if (name === "enabled") value = value === "true";
      if (name === "priority" || name === "timeout") value = value === "" ? undefined : parseInt(value, 10);
      if (value !== "" && value !== undefined) provider[name] = value;
      else delete provider[name];
    });
    return provider;
  });
}

function writeApiPoolEditor(field, providers) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return;
  const list = root.querySelector(".api-provider-list");
  list.innerHTML = providers.map((provider, index) => renderApiProviderCard(field, provider, index)).join("") || '<div class="api-pool-empty">暂无 provider，点击“添加 Provider”创建。</div>';
  const raw = root.querySelector("[data-api-pool-raw]");
  if (raw) raw.value = JSON.stringify(providers, null, 2);
}

function refreshApiPoolEditor(field) {
  try { writeApiPoolEditor(field, readApiPoolEditor(field)); } catch (e) { alertFlash("err", e.message); }
}

function addApiProvider(field) {
  try {
    const providers = readApiPoolEditor(field);
    providers.push(defaultApiProvider(providers.length));
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function removeApiProvider(field, index) {
  try {
    const providers = readApiPoolEditor(field);
    providers.splice(index, 1);
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function toggleApiPoolRaw(btn) {
  const root = btn.closest(".api-pool-editor");
  const raw = root.querySelector("[data-api-pool-raw]");
  const showing = raw.style.display !== "none";
  if (!showing) raw.value = JSON.stringify(readApiPoolEditor(root.dataset.apiPoolField), null, 2);
  raw.style.display = showing ? "none" : "block";
  btn.textContent = showing ? "查看 JSON" : "隐藏 JSON";
}

async function saveApiPool(field) {
  try {
    await saveField(field, readApiPoolEditor(field));
  } catch (e) { alertFlash("err", e.message); }
}

function activeSourceLabel(src) {
  return ({env_file:".env.prod",env_json:"env.json",runtime_config:"runtime_config.json",default:"默认"})[src] || src;
}

function markDirty(el) { el.dataset.dirty = "1"; }

async function commitTextField(field, btn, kind) {
  const wrap = btn.parentElement;
  const input = wrap.querySelector("input, textarea");
  if (!input) return;
  let raw = input.value;
  let value = raw;
  if (kind === "int") value = parseInt(raw, 10);
  else if (kind === "float") value = parseFloat(raw);
  await saveField(field, value);
}

async function saveField(field, value) {
  try {
    const result = await api("/config/value", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ field_name: field, value }) });
    if (result.success) { alertFlash("ok", `已保存 ${field}（已同步 .env 与 env.json）`); await loadView(); render(); }
    else { alertFlash("err", `保存部分失败：${(result.errors||[]).join("；")}`); await loadView(); render(); }
  } catch (e) { alertFlash("err", "保存失败：" + e.message); }
}

function pickGroup(g) { state.activeGroup = g; render(); }

function renderDevices() {
  const rows = state.devices.map(d => {
    const isCurrent = d.id === state.currentDeviceId;
    return `<tr>
      <td>${escapeHtml(d.label)} ${isCurrent ? '<span class="tag">当前</span>' : ''}</td>
      <td class="muted">${escapeHtml(d.ua.slice(0, 60))}</td>
      <td>${new Date(d.last_seen * 1000).toLocaleString()}</td>
      <td>${isCurrent ? '' : `<button class="btn small danger" onclick="revokeDevice('${escapeAttr(d.id)}')">撤销</button>`}</td>
    </tr>`;
  }).join("");
  return `<div class="card">
    <h2>已登录设备</h2>
    <table><thead><tr><th>设备</th><th>UA</th><th>最后活跃</th><th></th></tr></thead><tbody>${rows}</tbody></table>
  </div>`;
}

async function revokeDevice(id) {
  if (!confirm("撤销该设备？该设备下次访问将被踢出。")) return;
  try { await api("/auth/devices/" + encodeURIComponent(id), { method:"DELETE" }); alertFlash("ok", "已撤销"); await loadView(); render(); }
  catch (e) { alertFlash("err", "撤销失败：" + e.message); }
}

async function doLogout() {
  try { await api("/auth/logout", { method:"POST" }); } catch {}
  state.logged = false; render();
}

function attachLayout() {
  document.querySelectorAll("aside nav a").forEach(a => {
    a.addEventListener("click", async (ev) => {
      ev.preventDefault();
      state.view = a.getAttribute("href").slice(1);
      try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
    });
  });
}

function renderLogin() {
  const themeIcon = state.theme === "dark" ? "🌙" : "☀";
  const eligible = (state.eligibleAdmins || []).slice(0, 8);
  const eligibleBlock = eligible.length
    ? `<div class="muted" style="font-size:12px;margin-top:12px;padding:10px;background:var(--zebra);border-radius:6px;border:1px solid var(--line)">
        <div style="margin-bottom:4px">可登录的 QQ：</div>
        ${eligible.map(e => `<div style="font-family:ui-monospace,Consolas,monospace">· ${escapeHtml(e.qq)} <span class="muted" style="font-size:11px">${escapeHtml(e.source)}</span></div>`).join("")}
        ${state.eligibleAdmins.length > 8 ? `<div style="margin-top:4px">… 还有 ${state.eligibleAdmins.length - 8} 个</div>` : ''}
      </div>`
    : `<div class="muted" style="font-size:12px;margin-top:12px">未检测到管理员 QQ。请在 .env.prod 配置 <code>SUPERUSERS=["你的QQ"]</code>。</div>`;
  return `<div class="login-wrap"><div class="card"><div class="between">
      <h2 style="margin:0">拟人插件 WebUI 登录</h2>
      <button class="btn small" onclick="toggleTheme()" title="切换主题">${themeIcon}</button>
    </div>
    <div id="login-step1">
      <label>管理员 QQ</label>
      <input id="login-qq" type="text" placeholder="例如 10001">
      <div style="margin-top:14px"><button class="btn primary" onclick="sendCode()">发送验证码</button></div>
      <p class="muted" style="margin-top:14px;font-size:12.5px">点击发送后，Bot 会向该 QQ 私聊推送 6 位数验证码，5 分钟内有效。</p>
      ${eligibleBlock}
    </div>
    <div id="login-step2" style="display:none">
      <label>验证码（来自 Bot 私聊）</label>
      <input id="login-code" type="text" inputmode="numeric" maxlength="6" placeholder="6 位数字">
      <label style="margin-top:10px">设备名称（便于识别）</label>
      <input id="login-label" type="text" placeholder="例如 公司笔记本">
      <div style="margin-top:14px"><button class="btn primary" onclick="doVerify()">验证并登录</button></div>
    </div>
    <div id="login-msg" class="muted" style="margin-top:14px"></div>
  </div></div>`;
}

function attachLogin() { /* 节点内 onclick 已绑定 */ }

async function sendCode() {
  const qq = document.getElementById("login-qq").value.trim();
  const msg = document.getElementById("login-msg");
  msg.textContent = "正在发送…";
  try {
    await api("/auth/login", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ qq }) });
    state.pendingQq = qq;
    document.getElementById("login-step1").style.display = "none";
    document.getElementById("login-step2").style.display = "block";
    msg.textContent = "已发送，请到 QQ 私聊查看 6 位数验证码。";
  } catch (e) { msg.textContent = "发送失败：" + e.message; }
}

async function doVerify() {
  const code = document.getElementById("login-code").value.trim();
  const label = document.getElementById("login-label").value.trim();
  const msg = document.getElementById("login-msg");
  msg.textContent = "正在验证…";
  try {
    await api("/auth/verify", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ qq: state.pendingQq, code, device_label: label }) });
    state.logged = true; state.qq = state.pendingQq; await loadView(); render();
  } catch (e) { msg.textContent = "验证失败：" + e.message; }
}

function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function escapeAttr(s) { return escapeHtml(s).replace(/'/g, "&#39;"); }

window.addEventListener("hashchange", async () => {
  const v = location.hash.slice(1);
  if (v) { state.view = v; try { await loadView(); } catch {} render(); }
});

if (location.hash) state.view = location.hash.slice(1);
bootstrap();
</script>
</body>
</html>
"""
