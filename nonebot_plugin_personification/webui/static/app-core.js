const API = "/personification/api";
let state = {
  logged: false, qq: "", view: "dashboard",
  entries: [], groups: [], activeGroup: null, configSearch: "", configSearchComposing: false, configSearchDraft: "",
  devices: [], pendingDevices: [], trustedDevices: [], devicePending: false, loginRequestId: "", loginPolling: false, alert: null, loading: false, loadingMessage: "",
  dashboard: null, dashboardWindow: "month", dashboardDetail: null,
  personas: [], selectedPersona: null, personaSearch: "",
  groupList: [], selectedGroup: null, groupPersonas: [], groupStyle: null, groupKnowledge: [], groupAliasDrafts: {}, groupSchedule: null, groupScheduleGenerating: false,
  groupFavorability: null,
  groupSwitches: [], newGroupId: "",
  skills: [], skillFilter: "", skillSummary: null, skillRemoteSources: [], skillMcpTools: [],
  skillSourceForm: { source: "", name: "", ref: "", subdir: "", kind: "auto", preferFirst: false, autoApprove: false },
  testPrompt: "你好，自我介绍一下", testSystem: "你是测试助手，简洁回复。", testResult: null, testAllResult: null,
  personaTemplateForm: { mode: "source", work_title: "", character_name: "", persona_name: "", gender: "", personality: "", traits: "", hobbies: "", description: "" }, personaTemplateResult: null, personaTemplateBusy: false, personaTemplateTask: null, personaTemplateHistory: [],
  personaPrompt: null, personaPromptPath: "", health: null, healthBusyCat: "", interactionResult: null, interactionBusy: false,
  qzoneForwardForm: { target_user_id: "", forward_text: "" }, qzoneForwardResult: null, qzoneForwardBusy: false,
  pluginUpdateStatus: null, pluginUpdateHistory: null, pluginUpdateBusy: false, pluginUpdateChecking: false, pluginUpdateResult: null,
  qqInfo: null, qqGroups: [], qqFriends: [],
  memory: null, memoryFilter: "", memoryInnerState: null, memoryIncludeSelf: false, memoryLimit: 200,
  memoryVectorIndex: null, memorySearchQuery: "", memorySearchResult: null, memoryVectorBusy: false,
  memoryGraph: null, memoryGraphGroupId: "", memoryGraphLimit: 100, memoryGraphMinSalience: 0,
  groupRawChat: null, groupStyleSnapIdx: 0, groupStyleRebuilding: false,
  showAdvancedConfig: false,
  stickers: null, stickerSearch: "", selectedSticker: null,
  theme: "dark", mobileNavOpen: false, eligibleAdmins: [],
  audit: null, auditFilter: "",
  logs: null, traces: null, logLevel: "", logQuery: "", logTraceId: "", traceDetail: null, selectedTraceId: "",
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

function restoreConfigSearchFocus(caret) {
  setTimeout(() => {
    const el = document.getElementById("config-search-input");
    if (!el) return;
    el.focus();
    const pos = Number.isFinite(caret) ? Math.max(0, Math.min(caret, el.value.length)) : el.value.length;
    try { el.setSelectionRange(pos, pos); } catch {}
  }, 0);
}

function onConfigSearchCompositionStart(input) {
  state.configSearchComposing = true;
  state.configSearchDraft = input ? input.value : state.configSearch;
}

function onConfigSearchCompositionEnd(input) {
  state.configSearchComposing = false;
  state.configSearch = input ? input.value : state.configSearchDraft;
  state.configSearchDraft = "";
  const caret = input ? input.selectionStart : undefined;
  render();
  restoreConfigSearchFocus(caret);
}

function onConfigSearchInput(input, event) {
  if (!input) return;
  if ((event && event.isComposing) || state.configSearchComposing) {
    state.configSearchDraft = input.value;
    return;
  }
  state.configSearch = input.value;
  const caret = input.selectionStart;
  render();
  restoreConfigSearchFocus(caret);
}

async function bootstrap() {
  // 主题
  const savedTheme = localStorage.getItem("personification_theme") || "dark";
  state.theme = savedTheme;
  document.documentElement.setAttribute("data-theme", savedTheme);
  try { const me = await api("/auth/me"); state.logged = true; state.devicePending = false; state.qq = me.qq; await loadView(); }
  catch (e) { state.logged = false; state.devicePending = /DEVICE_PENDING/.test(String(e && e.message || "")); }
  if (state.devicePending) { render(); return; }
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

function closeMobileNav() {
  // 点击导航项后关闭抽屉；即便点的是当前视图（hashchange 不触发）也立即收起。
  if (state.mobileNavOpen) { state.mobileNavOpen = false; render(); }
}

function loadingMessageForView(view) {
  return ({
    dashboard: "正在统计 Token 消耗...",
    health: "正在跑功能体检...",
    qzone: "正在读取 QQ 空间状态...",
    config: "正在加载配置中心...",
    personas: "正在读取用户画像...",
    groups: "正在整理群信息...",
    memory: "正在打开记忆宫殿...",
    memory_graph: "正在绘制记忆关系...",
    stickers: "正在加载表情包库...",
    skills: "正在扫描 Skill 和 MCP 工具...",
    plugin_knowledge: "正在读取插件知识库...",
    plugin_manager: "正在检查插件更新...",
    persona_builder: "正在准备人设构建工具...",
    logs: "正在拉取插件日志...",
    traces: "正在拉取消息 Trace...",
    trace_detail: "正在打开 Trace 详情...",
    audit: "正在读取审计记录...",
    qq: "正在读取 QQ 账号信息...",
  })[view] || "正在加载页面...";
}

async function loadView() {
  state.loading = true;
  state.loadingMessage = loadingMessageForView(state.view);
  if (state.logged) render();
  try {
    if (state.view === "config") {
      const data = await api("/config/entries");
      state.entries = data.entries; state.groups = data.groups;
      if (!state.activeGroup || !state.groups.includes(state.activeGroup)) state.activeGroup = state.groups[0] || null;
    } else if (state.view === "devices") {
      const [data, pend, trust] = await Promise.all([
        api("/auth/devices"),
        api("/auth/pending-devices").catch(() => ({ devices: [] })),
        api("/auth/trusted-devices").catch(() => ({ devices: [] })),
      ]);
      state.devices = data.devices; state.currentDeviceId = data.current_device_id;
      state.pendingDevices = pend.devices || [];
      state.trustedDevices = trust.devices || [];
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
      state.skillSummary = data.summary || null;
      state.skillRemoteSources = data.remote_sources || [];
      state.skillMcpTools = data.mcp_tools || [];
    } else if (state.view === "test") {
      /* nothing to preload */
    } else if (state.view === "persona_builder") {
      const history = await api("/persona-template/history?limit=8").catch(() => ({ records: [] }));
      state.personaTemplateHistory = history.records || [];
    } else if (state.view === "qq") {
      const [info, groups, friends] = await Promise.all([
        api("/qq/info").catch(e => ({ error: e.message })),
        api("/qq/groups").catch(() => ({ groups: [] })),
        api("/qq/friends").catch(() => ({ friends: [] })),
      ]);
      state.qqInfo = info; state.qqGroups = groups.groups || []; state.qqFriends = friends.friends || [];
    } else if (state.view === "health") {
      state.health = await api("/health/check");  // 默认读缓存，秒开
    } else if (state.view === "qzone") {
      state.qzone = await api("/qzone/status");
    } else if (state.view === "plugin_manager") {
      const [status, history] = await Promise.all([
        api("/plugin-manager/status"),
        api("/plugin-manager/history?limit=30"),
      ]);
      state.pluginUpdateStatus = status;
      state.pluginUpdateHistory = history;
    } else if (state.view === "persona_prompt") {
      const qs = state.personaPromptPath ? ("?path=" + encodeURIComponent(state.personaPromptPath)) : "";
      state.personaPrompt = await api("/test/persona-prompt" + qs);
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
    } else if (state.view === "logs") {
      const qs = new URLSearchParams({ limit: "220" });
      if (state.logLevel) qs.set("level", state.logLevel);
      if (state.logQuery) qs.set("q", state.logQuery);
      const logs = await api("/logs/recent?" + qs.toString());
      state.logs = logs;
    } else if (state.view === "traces") {
      state.traces = await api("/logs/traces?limit=120");
    } else if (state.view === "trace_detail") {
      if (!state.selectedTraceId) {
        state.traceDetail = { error: "未选择 trace" };
      } else {
        state.traceDetail = await api("/logs/trace/" + encodeURIComponent(state.selectedTraceId)).catch(e => ({ error: e.message }));
      }
    } else if (state.view === "stickers") {
      state.stickers = await api("/stickers");
    } else if (state.view === "memory") {
      const qs = new URLSearchParams({ limit: String(state.memoryLimit || 200) });
      if (state.memoryFilter) qs.set("memory_type", state.memoryFilter);
      if (state.memoryIncludeSelf) qs.set("include_self", "true");
      if (state.memoryUserId) qs.set("user_id", state.memoryUserId);
      if (state.memoryGroupId) qs.set("group_id", state.memoryGroupId);
      if (state.memoryPalaceZone) qs.set("palace_zone", state.memoryPalaceZone);
      const [mem, inner, zones, vectorIndex] = await Promise.all([
        api("/memory/recent?" + qs.toString()),
        api("/memory/inner-state").catch(() => ({available: false})),
        api("/memory/palace-zones").catch(() => ({zones: []})),
        api("/memory/vector-index").catch(e => ({available:false, error:e.message})),
      ]);
      state.memory = mem;
      state.memoryInnerState = inner;
      state.memoryPalaceZones = zones.zones || [];
      state.memoryVectorIndex = vectorIndex;
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
  } finally { state.loading = false; state.loadingMessage = ""; }
}

function render() {
  const root = document.getElementById("app");
  if (state.devicePending) { root.innerHTML = renderDevicePending(); return; }
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
  const navItem = (v, label) => `<a href="#${v}" class="${state.view===v?'active':''}" onclick="closeMobileNav()">${label}</a>`;
  const themeIcon = state.theme === "dark" ? "🌙" : "☀";
  const loadingHint = state.loading
    ? `<div class="loading-hint"><span class="spinner"></span><span>${escapeHtml(state.loadingMessage || "正在加载页面...")}</span></div>`
    : "";
  return `${state.loading ? '<div class="progress-bar"></div>' : ''}
    <div class="layout">
    ${state.mobileNavOpen ? '<div class="scrim" onclick="toggleMobileNav()"></div>' : ''}
    <aside class="${state.mobileNavOpen?'open':''}">
      <h1>拟人插件控制台</h1>
      <nav>
        ${navItem('dashboard','仪表盘')}
        ${navItem('health','功能体检')}
        ${navItem('qzone','QQ 空间')}
        ${navItem('config','配置中心')}
        ${navItem('personas','用户画像')}
        ${navItem('groups','群信息')}
        ${navItem('group_switch','群开关')}
        ${navItem('memory','Agent 记忆')}
        ${navItem('memory_graph','记忆宫殿')}
        ${navItem('stickers','表情包')}
        ${navItem('skills','Skill 管理')}
        ${navItem('plugin_knowledge','插件知识库')}
        ${navItem('plugin_manager','插件管理')}
        ${navItem('test','模型测试')}
        ${navItem('persona_prompt','人设预览')}
        ${navItem('persona_builder','人设构建')}
        ${navItem('proactive','主动诊断')}
        ${navItem('traces','消息 Trace')}
        ${navItem('audit','审计日志')}
        ${navItem('logs','插件日志')}
        ${navItem('qq','QQ 管理')}
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
      ${loadingHint}
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
  if (state.view === "plugin_manager") return renderPluginManager();
  if (state.view === "qq") return renderQQ();
  if (state.view === "health") return renderHealth();
  if (state.view === "qzone") return renderQzone();
  if (state.view === "test") return renderTest();
  if (state.view === "persona_prompt") return renderPersonaPrompt();
  if (state.view === "persona_builder") return renderPersonaBuilder();
  if (state.view === "memory") return renderMemory();
  if (state.view === "memory_graph") return renderMemoryGraph();
  if (state.view === "stickers") return renderStickers();
  if (state.view === "audit") return renderAudit();
  if (state.view === "logs") return renderLogs();
  if (state.view === "traces") return renderTraces();
  if (state.view === "trace_detail") return renderTraceDetail();
  if (state.view === "proactive") return renderProactive();
  return `<div class="card"><h2>${escapeHtml(viewTitle())}</h2><p class="muted">该视图暂未实现。</p></div>`;
}
