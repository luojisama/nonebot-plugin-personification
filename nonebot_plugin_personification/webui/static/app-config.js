function normalizeConfigSearchText(value) {
  return String(value || "").normalize("NFKC").trim().toLowerCase();
}

function compactConfigSearchText(value) {
  return normalizeConfigSearchText(value).replace(/[^\w\u4e00-\u9fff]+/g, "");
}

function configSearchHaystack(entry) {
  if (!entry) return "";
  if (!entry._searchText) {
    const aliases = Array.isArray(entry.aliases) ? entry.aliases.join(" ") : "";
    const searchIndex = Array.isArray(entry.search_index) ? entry.search_index.join(" ") : "";
    entry._searchText = normalizeConfigSearchText([
      entry.key,
      entry.field_name,
      entry.label,
      entry.description,
      entry.group,
      aliases,
      searchIndex,
    ].join(" "));
    entry._searchCompact = compactConfigSearchText(entry._searchText);
    entry._searchParts = entry._searchText.split(/[\s,，;；/|]+/).map(compactConfigSearchText).filter(Boolean);
  }
  return entry._searchText;
}

function configSearchCompactHaystack(entry) {
  configSearchHaystack(entry);
  return entry && entry._searchCompact || "";
}

function configSearchNeedleVariants(token) {
  const raw = normalizeConfigSearchText(token);
  const compact = compactConfigSearchText(raw);
  return Array.from(new Set([raw, compact].filter(Boolean)));
}

function isConfigSubsequence(needle, haystack) {
  if (!needle || !haystack || needle.length < 2) return false;
  let idx = 0;
  for (const ch of haystack) {
    if (ch === needle[idx]) idx += 1;
    if (idx >= needle.length) return true;
  }
  return false;
}

function configEditDistanceWithin(a, b, maxDistance) {
  if (!a || !b) return false;
  if (Math.abs(a.length - b.length) > maxDistance) return false;
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  const curr = new Array(b.length + 1);
  for (let i = 1; i <= a.length; i++) {
    curr[0] = i;
    let rowMin = curr[0];
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost);
      rowMin = Math.min(rowMin, curr[j]);
    }
    if (rowMin > maxDistance) return false;
    for (let j = 0; j <= b.length; j++) prev[j] = curr[j];
  }
  return prev[b.length] <= maxDistance;
}

function configSearchTokenScore(entry, token) {
  const haystack = configSearchHaystack(entry);
  const compactHaystack = configSearchCompactHaystack(entry);
  const variants = configSearchNeedleVariants(token);
  if (!variants.length) return 0;
  for (const variant of variants) {
    if (haystack.includes(variant)) return 120 - Math.min(40, variant.length);
    if (compactHaystack.includes(variant)) return 110 - Math.min(40, variant.length);
  }
  const compactNeedle = variants[variants.length - 1];
  if (isConfigSubsequence(compactNeedle, compactHaystack)) return 56;
  if (compactNeedle.length >= 3) {
    const maxDistance = compactNeedle.length <= 5 ? 1 : 2;
    const parts = entry._searchParts || [];
    for (const part of parts) {
      if (part.length < 2) continue;
      if (configEditDistanceWithin(compactNeedle, part, maxDistance)) return 48;
      if (part.length > compactNeedle.length && configEditDistanceWithin(compactNeedle, part.slice(0, compactNeedle.length), maxDistance)) return 44;
    }
  }
  return -1;
}

function configSearchEntryScore(entry, tokens) {
  let score = 0;
  for (const token of tokens) {
    const tokenScore = configSearchTokenScore(entry, token);
    if (tokenScore < 0) return -1;
    score += tokenScore;
  }
  if (entry && entry.advanced) score -= 1;
  return score;
}

function configRememberDiagnostic(value, fallbackTitle="配置操作未完成") {
  const operation = value && value.diagnostic && typeof value.diagnostic === "object"
    ? value.diagnostic
    : (value instanceof Error ? operationDiagnosticFromError(value, fallbackTitle) : value);
  if (!operation || typeof operation !== "object") return null;
  state.configDiagnostics = [operation, ...(Array.isArray(state.configDiagnostics) ? state.configDiagnostics : [])].slice(0, 8);
  return operation;
}

function configClearDiagnostics() {
  state.configDiagnostics = [];
  render();
}

function configDraft(field) {
  const drafts = state.configDrafts && typeof state.configDrafts === "object" ? state.configDrafts : {};
  return Object.prototype.hasOwnProperty.call(drafts, field) ? drafts[field] : null;
}

function setConfigValueDraft(field, value, kind="value") {
  if (!state.configDrafts || typeof state.configDrafts !== "object") state.configDrafts = {};
  state.configDrafts[field] = {kind, value};
  return state.configDrafts[field];
}

function clearConfigDraft(field) {
  if (state.configDrafts && typeof state.configDrafts === "object") delete state.configDrafts[field];
}

function configDraftValue(entry) {
  const draft = configDraft(entry.field_name);
  return draft && draft.kind !== "api_pool" ? draft.value : entry.current;
}

function updateConfigDraft(field, input) {
  if (!input) return;
  setConfigValueDraft(field, input.value);
  markDirty(input);
}

const VIDEO_CONFIG_FIELDS = [
  "personification_video_understanding_enabled",
  "personification_video_route_mode",
  "personification_video_frame_preset",
  "personification_video_custom_frame_budgets",
  "personification_video_custom_scan_fps",
  "personification_video_visual_soft_limit",
  "personification_video_visual_hard_limit",
  "personification_video_max_scan_samples",
  "personification_video_contact_sheet_frames",
  "personification_video_payload_max_bytes",
  "personification_video_max_bytes",
  "personification_video_download_timeout",
  "personification_video_analysis_timeout",
  "personification_video_storyboard_fallback_enabled",
  "personification_fullmodal_provider_enabled",
  "personification_fullmodal_provider_protocol",
  "personification_fullmodal_provider_workspace_id",
  "personification_fullmodal_provider_api_url",
  "personification_fullmodal_provider_api_key",
  "personification_fullmodal_provider_model",
  "personification_fullmodal_provider_auth_mode",
  "personification_fullmodal_provider_video_fps",
  "personification_fullmodal_provider_media_resolution",
  "personification_fullmodal_provider_timeout",
  "personification_fullmodal_provider_max_bytes",
  "personification_fullmodal_provider_stream",
  "personification_gemini_web_enabled",
  "personification_gemini_web_risk_acknowledged",
  "personification_gemini_web_job_timeout",
  "personification_gemini_web_idle_timeout",
  "personification_gemini_web_video_max_bytes",
  "personification_gemini_web_audio_max_bytes",
  "personification_gemini_web_output_max_chars",
  "personification_mimo_web_asr_enabled",
  "personification_mimo_web_asr_risk_acknowledged",
  "personification_mimo_web_asr_job_timeout",
  "personification_mimo_web_asr_idle_timeout",
  "personification_mimo_web_asr_audio_max_bytes",
  "personification_mimo_web_asr_output_max_chars",
  "personification_audio_transcription_enabled",
  "personification_audio_transcription_provider",
  "personification_audio_transcription_workspace_id",
  "personification_audio_transcription_api_url",
  "personification_audio_transcription_api_key",
  "personification_audio_transcription_model",
  "personification_audio_transcription_custom_protocol",
  "personification_audio_transcription_language",
  "personification_audio_transcription_prompt",
  "personification_audio_transcription_hotwords",
  "personification_audio_transcription_diarization_enabled",
  "personification_audio_transcription_speaker_count",
  "personification_audio_transcription_timeout",
  "personification_audio_transcription_poll_seconds",
  "personification_audio_transcription_max_bytes",
  "personification_audio_transcription_max_chars",
];

function videoConfigEntries(items=state.entries) {
  const map = {};
  (items || []).forEach(entry => { if (entry && VIDEO_CONFIG_FIELDS.includes(entry.field_name)) map[entry.field_name] = entry; });
  return map;
}

function videoConfigValue(entries, field, fallback="") {
  const entry = entries[field];
  if (!entry) return fallback;
  const value = configDraftValue(entry);
  return value == null ? fallback : value;
}

function videoConfigSelect(field, value, options, label, description="", extra="") {
  const rendered = options.map(option => {
    const optionValue = String(option.value == null ? "" : option.value);
    return `<option value="${escapeAttr(optionValue)}" ${String(value)===optionValue?'selected':''}>${escapeHtml(option.label)}</option>`;
  }).join("");
  return `<label class="video-config-control"><span>${escapeHtml(label)}</span><select data-video-field="${escapeAttr(field)}" data-video-kind="text" onchange="updateVideoConfigDraft(this);refreshVideoConfigVisibility()" ${extra}>${rendered}</select>${description?`<small>${escapeHtml(description)}</small>`:''}</label>`;
}

function videoConfigInput(field, value, label, options={}) {
  const kind = options.kind || "text";
  const type = options.type || (kind === "secret" ? "password" : kind === "int" || kind === "float" || kind === "mib" ? "number" : "text");
  const current = kind === "secret" ? "" : value;
  const attrs = [
    options.min != null ? `min="${escapeAttr(options.min)}"` : "",
    options.max != null ? `max="${escapeAttr(options.max)}"` : "",
    options.step != null ? `step="${escapeAttr(options.step)}"` : "",
    options.list ? `list="${escapeAttr(options.list)}"` : "",
  ].filter(Boolean).join(" ");
  const placeholder = options.placeholder || (kind === "secret" && value ? "已设置（留空保持不变）" : "");
  return `<label class="video-config-control"><span>${escapeHtml(label)}</span><input type="${escapeAttr(type)}" data-video-field="${escapeAttr(field)}" data-video-kind="${escapeAttr(kind)}" value="${escapeAttr(current)}" placeholder="${escapeAttr(placeholder)}" ${attrs} oninput="updateVideoConfigDraft(this)">${options.description?`<small>${escapeHtml(options.description)}</small>`:''}</label>`;
}

function videoConfigToggle(field, value, label, description="") {
  const checked = value === true || value === "true" || value === 1;
  return `<label class="video-config-toggle"><input type="checkbox" data-video-field="${escapeAttr(field)}" data-video-kind="bool" ${checked?'checked':''} onchange="updateVideoConfigDraft(this);refreshVideoConfigVisibility()"><span><strong>${escapeHtml(label)}</strong>${description?`<small>${escapeHtml(description)}</small>`:''}</span></label>`;
}

function normalizeVideoFrameBudgets(value) {
  const defaults = {"15":24,"60":60,"180":120,"600":160};
  if (value && typeof value === "object" && !Array.isArray(value)) {
    Object.keys(defaults).forEach(key => {
      const number = Number(value[key]);
      if (Number.isFinite(number)) defaults[key] = Math.round(number);
    });
  }
  return defaults;
}

function renderVideoBudgetEditor(entry, compact=false) {
  const budgets = normalizeVideoFrameBudgets(entry ? configDraftValue(entry) : null);
  const field = entry ? entry.field_name : "personification_video_custom_frame_budgets";
  const labels = {"15":"15 秒", "60":"1 分钟", "180":"3 分钟", "600":"10 分钟"};
  const controls = Object.keys(labels).map(key => `<label class="video-config-control"><span>${labels[key]}目标帧数</span><input type="number" min="8" max="256" step="1" data-video-budget-key="${key}" value="${escapeAttr(budgets[key])}" oninput="updateVideoBudgetDraft('${escapeAttr(field)}',this)"></label>`).join("");
  return `<div class="video-budget-editor ${compact?'compact':''}" data-video-budget-field="${escapeAttr(field)}"><div class="video-config-grid">${controls}</div>${compact?`<button class="btn small primary" onclick="saveVideoBudgetField('${escapeAttr(field)}')">保存抽帧预算</button>`:''}</div>`;
}

function updateVideoBudgetDraft(field, input) {
  const root = input ? input.closest("[data-video-budget-field]") : document.querySelector(`[data-video-budget-field="${CSS.escape(field)}"]`);
  if (!root) return;
  const budgets = {};
  root.querySelectorAll("[data-video-budget-key]").forEach(control => {
    budgets[control.dataset.videoBudgetKey] = Math.round(Number(control.value || 0));
  });
  setConfigValueDraft(field, budgets, "video_budget");
  if (input) markDirty(input);
}

function saveVideoBudgetField(field) {
  const entry = state.entries.find(item => item.field_name === field);
  if (!entry) return;
  const root = document.querySelector(`[data-video-budget-field="${CSS.escape(field)}"]`);
  if (!root) return;
  updateVideoBudgetDraft(field, root.querySelector("[data-video-budget-key]"));
  saveField(field, configDraftValue(entry), {preserveDraft:true});
}

function updateVideoConfigDraft(input) {
  if (!input) return;
  const field = input.dataset.videoField;
  const kind = input.dataset.videoKind || "text";
  if (!field) return;
  let value = input.value;
  if (kind === "bool") value = Boolean(input.checked);
  else if (kind === "int") value = input.value === "" ? "" : Math.round(Number(input.value));
  else if (kind === "float") value = input.value === "" ? "" : Number(input.value);
  else if (kind === "mib") value = input.value === "" ? "" : Math.round(Number(input.value) * 1024 * 1024);
  else if (kind === "strlist") value = input.value.split(/[，,\n]/).map(item => item.trim()).filter(Boolean);
  if (kind === "secret" && input.value === "") clearConfigDraft(field);
  else setConfigValueDraft(field, value, `video_${kind}`);
  markDirty(input);
}

function videoConfigMiB(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.round(number / 1024 / 1024 * 100) / 100 : fallback;
}

function videoProviderNote(provider) {
  if (provider === "openai_qwen_omni") return "Qwen 使用官方 video_url / input_audio 扩展并要求流式返回；本地 Base64 编码后必须小于 10 MiB。";
  if (provider === "openai_mimo_v25") return "MiMo 使用官方 video_url、fps 与媒体分辨率参数；远程 URL 与本地 Base64 受各自官方上限约束。";
  if (provider === "gemini_native") return "Gemini 使用原生 Files API 与 generateContent；不会把视频伪装成 OpenAI 图片内容块。";
  if (provider === "openai_custom_video_url") return "自定义协议只发送固定 video_url 内容块，不支持原始 JSON 模板或任意脚本。";
  return "外部全模态 Provider 已关闭；主模型失败后将继续网页路线或最终分镜兜底。";
}

let _cliDiscoveryState = null;
let _cliDiscoveryInFlight = false;

function cliDiscoveryHtml() {
  const items = Array.isArray(_cliDiscoveryState && _cliDiscoveryState.items) ? _cliDiscoveryState.items : [];
  if (!items.length) return '<div class="muted">尚未探测。只读取本地安装状态、版本与凭证来源，不执行登录或升级。</div>';
  const labels = {ready:"可用",missing:"缺少凭证",expired:"已过期",cli_only:"CLI 管理",unreadable:"不可读取"};
  return '<div class="video-config-grid">' + items.map(item => {
    const stateLabel = labels[item.credential_state] || item.credential_state || "未知";
    return '<div class="card" style="padding:10px"><div class="between"><strong>'
      + escapeHtml(item.provider_type || "CLI") + '</strong><span class="tag">'
      + (item.installed ? "已安装" : "未安装") + '</span></div><div class="muted" style="font-size:12px">版本：'
      + escapeHtml(item.version || "-") + ' · 凭证：' + escapeHtml(stateLabel) + ' · 来源：'
      + escapeHtml(item.credential_source || "none") + '</div>'
      + (item.diagnostic_code ? '<div class="muted" style="font-size:11px">' + escapeHtml(item.diagnostic_code) + '</div>' : '')
      + '</div>';
  }).join("") + '</div>';
}

async function refreshCliDiscovery() {
  const island = document.querySelector("[data-cli-discovery-island]");
  if (!island || _cliDiscoveryInFlight) return;
  _cliDiscoveryInFlight = true;
  try {
    _cliDiscoveryState = await api("/config/cli-discovery");
  } catch (error) {
    _cliDiscoveryState = {items:[], error:String(error && error.message || error || "cli_discovery_failed")};
  } finally {
    _cliDiscoveryInFlight = false;
  }
  const current = document.querySelector("[data-cli-discovery-island]");
  if (current) current.innerHTML = cliDiscoveryHtml();
}

const GEMINI_WEB_STATE_LABELS = {
  disabled:"已关闭", starting:"正在启动", ready:"可用", login_required:"需要登录",
  manual_verification_required:"需要人工验证", busy:"任务占用", dom_changed:"页面结构变化",
  unavailable:"不可用",
};
let _geminiWebStatusTimer = null;
let _geminiWebStatusInFlight = false;

function geminiWebStatusSignature(status, auth) {
  return JSON.stringify([
    status?.state || "", status?.profile_present === true, status?.browser_running === true,
    status?.active_job === true, Number(status?.waiting_jobs || 0), status?.last_diagnostic_code || "",
    Number(status?.risk_cooldown_seconds || 0), auth?.session_id || "", auth?.status || "",
    auth?.interactive_available === true,
  ]);
}

function renderGeminiWebStatus(status=state.geminiWebStatus) {
  const current = status || {};
  const code = String(current.last_diagnostic_code || "");
  const cooldown = Math.max(0, Number(current.risk_cooldown_seconds || 0));
  return `<div class="gemini-web-status-island" data-gemini-web-status-island>
    <div class="mcp-runtime-overview"><span>状态<strong>${escapeHtml(GEMINI_WEB_STATE_LABELS[current.state] || current.state || "待检查")}</strong></span><span>页面契约<strong>${escapeHtml(current.page_contract_version || "gemini_web_v1")}</strong></span><span>本地 profile<strong>${current.profile_present?"存在":"无"}</strong></span><span>浏览器<strong>${current.browser_running?"运行中":"已回收"}</strong></span><span>任务<strong>${current.active_job?"占用":"空闲"}</strong></span></div>
    ${code?`<div class="alert ${code.includes("network_risk")?"warn":""}"><code>${escapeHtml(code)}</code>${cooldown?` · 冷却剩余 ${Math.ceil(cooldown/60)} 分钟`:""}</div>`:""}
  </div>`;
}

function renderGeminiWebInteractiveAuth(auth) {
  if (!auth?.session_id || auth.interactive_available !== true) return "";
  const sessionId = String(auth.session_id || "");
  const viewport = auth.interactive_viewport || {};
  const width = Math.max(320, Number(viewport.width || 1280));
  const height = Math.max(240, Number(viewport.height || 900));
  const frame = builtinInteractiveFrameEntry(sessionId);
  const frameSource = frame.objectUrl || _MCP_INTERACTIVE_FRAME_PLACEHOLDER_SRC;
  return `<section class="mcp-interactive-auth" data-mcp-interactive-session="${escapeAttr(sessionId)}" data-platform="gemini_web">
    <div class="mcp-interactive-heading"><div><strong>Gemini Web 人工登录 / 验证</strong><small>当前官方页面：${escapeHtml(auth.interactive_display_url || "https://gemini.google.com/app")}</small></div><span class="tag required">仅管理员</span></div>
    <div class="mcp-interactive-screen" role="application" aria-label="Gemini 官方页面人工接管画面">
      <img draggable="false" alt="Gemini 官方页面" src="${escapeAttr(frameSource)}" class="${frame.objectUrl?"":"is-loading"}" data-mcp-interactive-frame data-platform="gemini_web" data-session-id="${escapeAttr(sessionId)}" data-viewport-width="${width}" data-viewport-height="${height}">
      <span class="mcp-interactive-frame-placeholder">正在读取 Gemini 官方页面画面…</span>
      <span class="mcp-interactive-pointer-marker" data-mcp-interactive-pointer-marker aria-hidden="true"></span>
    </div>
    <div class="mcp-interactive-transport" data-mcp-interactive-status role="status">画面与操作通道准备中</div>
    <div class="mcp-interactive-controls">
      <label>向当前焦点输入<input type="password" maxlength="200" autocomplete="off" spellcheck="false" data-mcp-interactive-text placeholder="先点击官方输入框，再在此输入"></label>
      <button class="btn" data-mcp-interactive-type="gemini_web" data-session-id="${escapeAttr(sessionId)}">发送输入</button>
      ${["Tab","Enter","Backspace","Escape"].map(key=>`<button class="btn small" data-mcp-interactive-key="${key}" data-platform="gemini_web" data-session-id="${escapeAttr(sessionId)}">${key}</button>`).join("")}
      <button class="btn small" data-mcp-interactive-scroll="-700" data-platform="gemini_web" data-session-id="${escapeAttr(sessionId)}">向上滚动</button>
      <button class="btn small" data-mcp-interactive-scroll="700" data-platform="gemini_web" data-session-id="${escapeAttr(sessionId)}">向下滚动</button>
      <button class="btn" data-mcp-interactive-refresh="gemini_web">刷新画面</button>
      <button class="btn primary" onclick="geminiWebFinishAuth('${escapeAttr(sessionId)}')">验证完成，检查登录态</button>
      <button class="btn danger" onclick="geminiWebCancelAuth('${escapeAttr(sessionId)}')">取消接管</button>
    </div>
    <p class="muted">操作通过实时指针协议转发到固定的 Gemini 与 Google 登录域名。插件不会识别或破解验证码；出现网络安全风险、账号风控或验证页时自动操作立即停止，只保留管理员本人接管。</p>
  </section>`;
}

function renderGeminiWebCard(entries) {
  const value = (field, fallback="") => videoConfigValue(entries, field, fallback);
  const enabled = value("personification_gemini_web_enabled", false);
  const acknowledged = value("personification_gemini_web_risk_acknowledged", false);
  const canOperate = enabled && acknowledged;
  return `<section class="card video-config-card" data-gemini-web-card>
    <div class="between"><div><h2>Gemini Web（实验）</h2><p class="muted">通过 Gemini 消费者网页公开的文件选择器上传音视频并读取本轮最新模型回复。该路径不依赖社交 MCP。</p></div><span class="tag required">实验能力</span></div>
    <div class="alert warn">媒体会离开 Bot 服务器，并可能保留在 Google 账号历史中。插件使用标准 Playwright 和正常页面控件；不隐藏自动化、不导出 Cookie、不重放内部接口、不绕过验证码或网络安全风险。<a href="https://policies.google.com/terms" target="_blank" rel="noopener noreferrer">查看 Google 服务条款</a></div>
    <div class="video-config-grid">
      ${videoConfigToggle("personification_gemini_web_enabled", enabled, "启用 Gemini Web", "未确认风险时服务端拒绝启用。")}
      ${videoConfigToggle("personification_gemini_web_risk_acknowledged", acknowledged, "我已确认第三方上传与网页自动化风险")}
      ${videoConfigInput("personification_gemini_web_video_max_bytes", videoConfigMiB(value("personification_gemini_web_video_max_bytes",536870912),512), "Web 视频上限（MiB）", {kind:"mib",min:8,max:2048,step:1})}
      ${videoConfigInput("personification_gemini_web_audio_max_bytes", videoConfigMiB(value("personification_gemini_web_audio_max_bytes",104857600),100), "Web 音频上限（MiB）", {kind:"mib",min:0.0625,max:512,step:1})}
      ${videoConfigInput("personification_gemini_web_job_timeout", value("personification_gemini_web_job_timeout",600), "任务超时（秒）", {kind:"float",min:20,max:900,step:1})}
      ${videoConfigInput("personification_gemini_web_idle_timeout", value("personification_gemini_web_idle_timeout",300), "空闲回收（秒）", {kind:"float",min:60,max:1800,step:10})}
      ${videoConfigInput("personification_gemini_web_output_max_chars", value("personification_gemini_web_output_max_chars",20000), "输出上限（字符）", {kind:"int",min:1000,max:50000,step:100})}
    </div>
    ${renderGeminiWebStatus()}
    <div class="row"><button class="btn" data-gemini-web-operation onclick="geminiWebProbe()" ${state.geminiWebBusy||!canOperate?"disabled":""}>检查页面兼容性</button><button class="btn primary" data-gemini-web-operation onclick="geminiWebStartAuth()" ${state.geminiWebBusy||!canOperate?"disabled":""}>打开登录 / 人工验证</button><button class="btn" data-gemini-web-operation onclick="geminiWebProbe()" ${state.geminiWebBusy||!canOperate?"disabled":""}>重新检查登录状态</button><button class="btn danger" onclick="geminiWebLogout()" ${state.geminiWebBusy?"disabled":""}>注销并删除本地 profile</button></div>
    <div data-gemini-web-auth-host>${renderGeminiWebInteractiveAuth(state.geminiWebAuth)}</div>
  </section>`;
}

function updateGeminiWebCardDom() {
  const island = document.querySelector("[data-gemini-web-status-island]");
  const authHost = document.querySelector("[data-gemini-web-auth-host]");
  if (!island || !authHost) return false;
  island.outerHTML = renderGeminiWebStatus();
  if (!_mcpInteractivePointer) authHost.innerHTML = renderGeminiWebInteractiveAuth(state.geminiWebAuth);
  if (state.geminiWebAuth?.interactive_available) startBuiltinInteractiveFramePolling();
  return true;
}

async function refreshGeminiWebStatus({refresh=false, renderAfter=true}={}) {
  if (_geminiWebStatusInFlight || state.view !== "config" || state.activeGroup !== "视频理解") return;
  _geminiWebStatusInFlight = true;
  const before = geminiWebStatusSignature(state.geminiWebStatus, state.geminiWebAuth);
  try {
    const next = await api(`/media/web/gemini/status${refresh?"?refresh=true":""}`, {cache:"no-store"});
    state.geminiWebStatus = next;
    if (!state.geminiWebAuth && next?.interactive_session) state.geminiWebAuth = next.interactive_session;
    if (state.geminiWebAuth?.session_id && !document.hidden) {
      try { state.geminiWebAuth = await api(`/media/web/gemini/auth/${encodeURIComponent(state.geminiWebAuth.session_id)}`, {cache:"no-store"}); } catch {}
    }
  } finally {
    _geminiWebStatusInFlight = false;
  }
  const after = geminiWebStatusSignature(state.geminiWebStatus, state.geminiWebAuth);
  if (renderAfter && before !== after && !_mcpInteractivePointer && !updateGeminiWebCardDom()) render();
  if (state.geminiWebAuth?.interactive_available) startBuiltinInteractiveFramePolling();
}

function scheduleGeminiWebStatusPoll(delay=3000) {
  if (_geminiWebStatusTimer) clearTimeout(_geminiWebStatusTimer);
  _geminiWebStatusTimer = setTimeout(async()=>{
    _geminiWebStatusTimer = null;
    if (state.view !== "config") return;
    if (!document.hidden && state.activeGroup === "视频理解") await refreshGeminiWebStatus();
    scheduleGeminiWebStatusPoll(3000);
  }, Math.max(0, Number(delay||0)));
}

function startGeminiWebConfigLifecycle() {
  scheduleGeminiWebStatusPoll(0);
  scheduleMimoWebAsrStatusPoll(0);
}

function stopGeminiWebConfigLifecycle() {
  if (_geminiWebStatusTimer) clearTimeout(_geminiWebStatusTimer);
  _geminiWebStatusTimer = null;
  if (_mimoWebAsrStatusTimer) clearTimeout(_mimoWebAsrStatusTimer);
  _mimoWebAsrStatusTimer = null;
  if (["gemini_web","mimo_asr_web"].includes(_mcpInteractivePointer?.platform)) cancelActiveBuiltinInteractivePointer({keepalive:true});
  stopBuiltinInteractiveFramePolling();
}

async function geminiWebProbe() {
  if (state.geminiWebBusy) return;
  state.geminiWebBusy = true; render();
  try { state.geminiWebStatus = await api("/media/web/gemini/probe", {method:"POST"}); const ready=state.geminiWebStatus?.state==="ready"; const login=state.geminiWebStatus?.state==="login_required"; alertFlash(ready||login?"ok":"err", ready?"Gemini 页面与登录态可用":login?"Gemini 官方页面已识别，请先完成登录":"Gemini 页面检查未达到可用状态"); }
  catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Gemini 页面检查失败").message || "Gemini 页面检查失败"); }
  finally { state.geminiWebBusy = false; render(); }
}

async function geminiWebStartAuth() {
  if (state.geminiWebBusy) return;
  if (!confirm("将打开 Gemini 官方页面供管理员本人登录或验证。插件不会绕过验证码、风控或网络安全风险。确认继续？")) return;
  state.geminiWebBusy = true; render();
  try {
    state.geminiWebAuth = await api("/media/web/gemini/auth/start", {method:"POST"});
    const blocked = state.geminiWebAuth?.status === "risk_controlled" || !state.geminiWebAuth?.session_id;
    if (!blocked) startBuiltinInteractiveFramePolling();
    alertFlash(blocked?"err":"ok", blocked?"网络安全风险冷却尚未结束，未打开官方页面":"Gemini 人工登录会话已创建");
  }
  catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Gemini 登录启动失败").message || "Gemini 登录启动失败"); }
  finally { state.geminiWebBusy = false; render(); }
}

async function geminiWebFinishAuth(sessionId) {
  if (state.geminiWebBusy) return;
  if (_mcpInteractivePointer?.sessionId === sessionId) await cancelActiveBuiltinInteractivePointer();
  state.geminiWebBusy = true;
  try { state.geminiWebAuth = await api(`/media/web/gemini/auth/${encodeURIComponent(sessionId)}/finish`, {method:"POST"}); await refreshGeminiWebStatus({refresh:false,renderAfter:false}); alertFlash(state.geminiWebAuth?.status==="success"?"ok":"err", state.geminiWebAuth?.status==="success"?"Gemini 登录态已保存":"尚未检测到有效登录态"); }
  catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Gemini 登录状态检查失败").message || "Gemini 登录状态检查失败"); }
  finally { state.geminiWebBusy = false; render(); }
}

async function geminiWebCancelAuth(sessionId) {
  if (_mcpInteractivePointer?.sessionId === sessionId) await cancelActiveBuiltinInteractivePointer();
  try { await api(`/media/web/gemini/auth/${encodeURIComponent(sessionId)}/cancel`, {method:"POST"}); state.geminiWebAuth = null; alertFlash("ok", "Gemini 人工接管已取消"); }
  catch (error) { alertFlash("err", operationDiagnosticFromError(error, "取消 Gemini 接管失败").message || "取消 Gemini 接管失败"); }
  finally { render(); }
}

async function geminiWebLogout() {
  const exact = "确认注销GeminiWeb";
  if ((prompt(`注销会关闭浏览器并删除本地 Gemini profile，不会删除 Google 账号中的云端历史。\n请输入：${exact}`)||"") !== exact) return;
  state.geminiWebBusy = true; render();
  try { state.geminiWebStatus = await api("/media/web/gemini/logout", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({confirm:exact})}); state.geminiWebAuth=null; stopBuiltinInteractiveFramePolling(); alertFlash("ok", "Gemini 本地 profile 已删除"); }
  catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Gemini 注销失败").message || "Gemini 注销失败"); }
  finally { state.geminiWebBusy=false; render(); }
}

let _mimoWebAsrStatusTimer = null;
let _mimoWebAsrStatusInFlight = false;

function renderMimoWebAsrStatus(status=state.mimoWebAsrStatus) {
  const current = status || {};
  const code = String(current.last_diagnostic_code || "");
  const cooldown = Math.max(0, Number(current.risk_cooldown_seconds || 0));
  return `<div class="mimo-web-asr-status-island" data-mimo-web-asr-status-island>
    <div class="mcp-runtime-overview"><span>状态<strong>${escapeHtml(GEMINI_WEB_STATE_LABELS[current.state] || current.state || "待检查")}</strong></span><span>页面契约<strong>${escapeHtml(current.page_contract_version || "mimo_studio_asr_v1")}</strong></span><span>本地 profile<strong>${current.profile_present?"存在":"无"}</strong></span><span>浏览器<strong>${current.browser_running?"运行中":"已回收"}</strong></span><span>任务<strong>${current.active_job?"占用":"空闲"}</strong></span></div>
    ${code?`<div class="alert ${code.includes("network_risk")?"warn":""}"><code>${escapeHtml(code)}</code>${cooldown?` · 冷却剩余 ${Math.ceil(cooldown/60)} 分钟`:""}</div>`:""}
  </div>`;
}

function renderMimoWebAsrInteractiveAuth(auth) {
  if (!auth?.session_id || auth.interactive_available !== true) return "";
  const sessionId = String(auth.session_id || "");
  const viewport = auth.interactive_viewport || {};
  const width = Math.max(320, Number(viewport.width || 1280));
  const height = Math.max(240, Number(viewport.height || 900));
  const frame = builtinInteractiveFrameEntry(sessionId);
  const frameSource = frame.objectUrl || _MCP_INTERACTIVE_FRAME_PLACEHOLDER_SRC;
  return `<section class="mcp-interactive-auth" data-mcp-interactive-session="${escapeAttr(sessionId)}" data-platform="mimo_asr_web">
    <div class="mcp-interactive-heading"><div><strong>MiMo Web ASR 人工登录 / 验证</strong><small>当前官方页面：${escapeHtml(auth.interactive_display_url || "https://aistudio.xiaomimimo.com/#/c")}</small></div><span class="tag required">仅管理员</span></div>
    <div class="mcp-interactive-screen" role="application" aria-label="MiMo Studio 官方页面人工接管画面">
      <img draggable="false" alt="MiMo Studio 官方页面" src="${escapeAttr(frameSource)}" class="${frame.objectUrl?"":"is-loading"}" data-mcp-interactive-frame data-platform="mimo_asr_web" data-session-id="${escapeAttr(sessionId)}" data-viewport-width="${width}" data-viewport-height="${height}">
      <span class="mcp-interactive-frame-placeholder">正在读取 MiMo Studio 官方页面画面…</span>
      <span class="mcp-interactive-pointer-marker" data-mcp-interactive-pointer-marker aria-hidden="true"></span>
    </div>
    <div class="mcp-interactive-transport" data-mcp-interactive-status role="status">画面与操作通道准备中</div>
    <div class="mcp-interactive-controls">
      <label>向当前焦点输入<input type="password" maxlength="200" autocomplete="off" spellcheck="false" data-mcp-interactive-text placeholder="先点击官方输入框，再在此输入"></label>
      <button class="btn" data-mcp-interactive-type="mimo_asr_web" data-session-id="${escapeAttr(sessionId)}">发送输入</button>
      ${["Tab","Enter","Backspace","Escape"].map(key=>`<button class="btn small" data-mcp-interactive-key="${key}" data-platform="mimo_asr_web" data-session-id="${escapeAttr(sessionId)}">${key}</button>`).join("")}
      <button class="btn small" data-mcp-interactive-scroll="-700" data-platform="mimo_asr_web" data-session-id="${escapeAttr(sessionId)}">向上滚动</button>
      <button class="btn small" data-mcp-interactive-scroll="700" data-platform="mimo_asr_web" data-session-id="${escapeAttr(sessionId)}">向下滚动</button>
      <button class="btn" data-mcp-interactive-refresh="mimo_asr_web">刷新画面</button>
      <button class="btn primary" onclick="mimoWebAsrFinishAuth('${escapeAttr(sessionId)}')">验证完成，检查登录态</button>
      <button class="btn danger" onclick="mimoWebAsrCancelAuth('${escapeAttr(sessionId)}')">取消接管</button>
    </div>
    <p class="muted">操作只转发到 MiMo Studio 官方域名。插件不会识别或破解验证码，不隐藏自动化特征，不调用网页内部接口；遇到风控立即停止。</p>
  </section>`;
}

function renderMimoWebAsrCard(entries) {
  const value = (field, fallback="") => videoConfigValue(entries, field, fallback);
  const enabled = value("personification_mimo_web_asr_enabled", false);
  const acknowledged = value("personification_mimo_web_asr_risk_acknowledged", false);
  const canOperate = enabled && acknowledged;
  return `<section class="card video-config-card" data-mimo-web-asr-card>
    <div class="between"><div><h2>MiMo Web ASR（实验）</h2><p class="muted">仅在没有平台字幕、且全模态模型没有成功听取音轨时，通过 MiMo Studio 公开页面选择 MiMo-V2.5-ASR 做忠实转写。</p></div><span class="tag required">声音兜底</span></div>
    <div class="alert warn">音频会离开 Bot 服务器并可能保留在 MiMo 账号历史中。页面没有 MiMo-V2.5-ASR、没有音频上传入口或出现验证时会立即降级到已配置的 ASR API。</div>
    <div class="video-config-grid">
      ${videoConfigToggle("personification_mimo_web_asr_enabled", enabled, "启用 MiMo Web ASR", "未确认风险时服务端拒绝启用。")}
      ${videoConfigToggle("personification_mimo_web_asr_risk_acknowledged", acknowledged, "我已确认音频上传与网页自动化风险")}
      ${videoConfigInput("personification_mimo_web_asr_audio_max_bytes", videoConfigMiB(value("personification_mimo_web_asr_audio_max_bytes",67108864),64), "Web 音频上限（MiB）", {kind:"mib",min:0.0625,max:512,step:1})}
      ${videoConfigInput("personification_mimo_web_asr_job_timeout", value("personification_mimo_web_asr_job_timeout",300), "任务超时（秒）", {kind:"float",min:20,max:600,step:1})}
      ${videoConfigInput("personification_mimo_web_asr_idle_timeout", value("personification_mimo_web_asr_idle_timeout",300), "空闲回收（秒）", {kind:"float",min:60,max:1800,step:10})}
      ${videoConfigInput("personification_mimo_web_asr_output_max_chars", value("personification_mimo_web_asr_output_max_chars",20000), "输出上限（字符）", {kind:"int",min:1000,max:50000,step:100})}
    </div>
    ${renderMimoWebAsrStatus()}
    <div class="row"><button class="btn" onclick="mimoWebAsrProbe()" ${state.mimoWebAsrBusy||!canOperate?"disabled":""}>检查页面与模型</button><button class="btn primary" onclick="mimoWebAsrStartAuth()" ${state.mimoWebAsrBusy||!canOperate?"disabled":""}>打开登录 / 人工验证</button><button class="btn danger" onclick="mimoWebAsrLogout()" ${state.mimoWebAsrBusy?"disabled":""}>注销并删除本地 profile</button></div>
    <div data-mimo-web-asr-auth-host>${renderMimoWebAsrInteractiveAuth(state.mimoWebAsrAuth)}</div>
  </section>`;
}

function updateMimoWebAsrCardDom() {
  const island = document.querySelector("[data-mimo-web-asr-status-island]");
  const authHost = document.querySelector("[data-mimo-web-asr-auth-host]");
  if (!island || !authHost) return false;
  island.outerHTML = renderMimoWebAsrStatus();
  if (!_mcpInteractivePointer) authHost.innerHTML = renderMimoWebAsrInteractiveAuth(state.mimoWebAsrAuth);
  if (state.mimoWebAsrAuth?.interactive_available) startBuiltinInteractiveFramePolling();
  return true;
}

async function refreshMimoWebAsrStatus({refresh=false, renderAfter=true}={}) {
  if (_mimoWebAsrStatusInFlight || state.view !== "config" || state.activeGroup !== "视频理解") return;
  _mimoWebAsrStatusInFlight = true;
  const before = geminiWebStatusSignature(state.mimoWebAsrStatus, state.mimoWebAsrAuth);
  try {
    const next = await api(`/media/web/mimo_asr/status${refresh?"?refresh=true":""}`, {cache:"no-store"});
    state.mimoWebAsrStatus = next;
    if (!state.mimoWebAsrAuth && next?.interactive_session) state.mimoWebAsrAuth = next.interactive_session;
    if (state.mimoWebAsrAuth?.session_id && !document.hidden) {
      try { state.mimoWebAsrAuth = await api(`/media/web/mimo_asr/auth/${encodeURIComponent(state.mimoWebAsrAuth.session_id)}`, {cache:"no-store"}); } catch {}
    }
  } finally { _mimoWebAsrStatusInFlight = false; }
  const after = geminiWebStatusSignature(state.mimoWebAsrStatus, state.mimoWebAsrAuth);
  if (renderAfter && before !== after && !_mcpInteractivePointer && !updateMimoWebAsrCardDom()) render();
  if (state.mimoWebAsrAuth?.interactive_available) startBuiltinInteractiveFramePolling();
}

function scheduleMimoWebAsrStatusPoll(delay=3000) {
  if (_mimoWebAsrStatusTimer) clearTimeout(_mimoWebAsrStatusTimer);
  _mimoWebAsrStatusTimer = setTimeout(async()=>{
    _mimoWebAsrStatusTimer = null;
    if (state.view !== "config") return;
    if (!document.hidden && state.activeGroup === "视频理解") await refreshMimoWebAsrStatus();
    scheduleMimoWebAsrStatusPoll(3000);
  }, Math.max(0, Number(delay||0)));
}

async function mimoWebAsrProbe() {
  if (state.mimoWebAsrBusy) return;
  state.mimoWebAsrBusy=true; render();
  try { state.mimoWebAsrStatus=await api("/media/web/mimo_asr/probe",{method:"POST"}); alertFlash(state.mimoWebAsrStatus?.state==="ready"?"ok":"err",state.mimoWebAsrStatus?.state==="ready"?"MiMo-V2.5-ASR 页面契约可用":"MiMo Web ASR 页面或模型尚不可用"); }
  catch(error){alertFlash("err",operationDiagnosticFromError(error,"MiMo Web ASR 检查失败").message||"MiMo Web ASR 检查失败");}
  finally{state.mimoWebAsrBusy=false;render();}
}

async function mimoWebAsrStartAuth() {
  if (state.mimoWebAsrBusy || !confirm("将打开 MiMo Studio 官方页面供管理员本人登录或验证。插件不会绕过验证码、风控或网络安全风险。确认继续？")) return;
  state.mimoWebAsrBusy=true;render();
  try { state.mimoWebAsrAuth=await api("/media/web/mimo_asr/auth/start",{method:"POST"}); if(state.mimoWebAsrAuth?.session_id)startBuiltinInteractiveFramePolling(); alertFlash(state.mimoWebAsrAuth?.session_id?"ok":"err",state.mimoWebAsrAuth?.session_id?"MiMo Web ASR 人工登录会话已创建":"未能打开 MiMo Studio 官方页面"); }
  catch(error){alertFlash("err",operationDiagnosticFromError(error,"MiMo Web ASR 登录启动失败").message||"MiMo Web ASR 登录启动失败");}
  finally{state.mimoWebAsrBusy=false;render();}
}

async function mimoWebAsrFinishAuth(sessionId) {
  if (_mcpInteractivePointer?.sessionId===sessionId) await cancelActiveBuiltinInteractivePointer();
  state.mimoWebAsrBusy=true;
  try { state.mimoWebAsrAuth=await api(`/media/web/mimo_asr/auth/${encodeURIComponent(sessionId)}/finish`,{method:"POST"}); await refreshMimoWebAsrStatus({renderAfter:false}); alertFlash(state.mimoWebAsrAuth?.status==="success"?"ok":"err",state.mimoWebAsrAuth?.status==="success"?"MiMo Studio 登录态已保存":"尚未检测到可用登录态和 ASR 页面"); }
  catch(error){alertFlash("err",operationDiagnosticFromError(error,"MiMo Web ASR 登录检查失败").message||"MiMo Web ASR 登录检查失败");}
  finally{state.mimoWebAsrBusy=false;render();}
}

async function mimoWebAsrCancelAuth(sessionId) {
  if (_mcpInteractivePointer?.sessionId===sessionId) await cancelActiveBuiltinInteractivePointer();
  try { await api(`/media/web/mimo_asr/auth/${encodeURIComponent(sessionId)}/cancel`,{method:"POST"}); state.mimoWebAsrAuth=null; alertFlash("ok","MiMo Web ASR 人工接管已取消"); }
  catch(error){alertFlash("err",operationDiagnosticFromError(error,"取消 MiMo Web ASR 接管失败").message||"取消 MiMo Web ASR 接管失败");}
  finally{render();}
}

async function mimoWebAsrLogout() {
  const exact="确认注销MiMoWebASR";
  if((prompt(`注销会删除本地 MiMo Web ASR profile，不会删除账号云端历史。\n请输入：${exact}`)||"")!==exact)return;
  state.mimoWebAsrBusy=true;render();
  try { state.mimoWebAsrStatus=await api("/media/web/mimo_asr/logout",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({confirm:exact})}); state.mimoWebAsrAuth=null; stopBuiltinInteractiveFramePolling(); alertFlash("ok","MiMo Web ASR 本地 profile 已删除"); }
  catch(error){alertFlash("err",operationDiagnosticFromError(error,"MiMo Web ASR 注销失败").message||"MiMo Web ASR 注销失败");}
  finally{state.mimoWebAsrBusy=false;render();}
}

function renderVideoUnderstandingEditor(items) {
  queueMicrotask(refreshCliDiscovery);
  const entries = videoConfigEntries(items);
  const value = (field, fallback="") => videoConfigValue(entries, field, fallback);
  const provider = String(value("personification_fullmodal_provider_protocol", "gemini_native") || "gemini_native");
  const framePreset = String(value("personification_video_frame_preset", "balanced") || "balanced");
  const asrProvider = String(value("personification_audio_transcription_provider", "auto") || "auto");
  const hotwords = strListValue(value("personification_audio_transcription_hotwords", [])).join("，");
  const providerModel = value("personification_fullmodal_provider_model", "");
  const providerKeyConfigured = value("personification_fullmodal_provider_api_key", "") === "***";
  const asrKeyConfigured = value("personification_audio_transcription_api_key", "") === "***";
  return `<div class="video-config-editor">
    <section class="card video-config-card">
      <div class="between"><div><h2>本地 CLI 状态</h2><p class="muted">固定白名单只读探测；不会运行 update、install、登录或任意 WebUI 命令。</p></div><button class="btn small" onclick="refreshCliDiscovery()">重新探测</button></div>
      <div data-cli-discovery-island>${cliDiscoveryHtml()}</div>
    </section>
    <section class="card video-config-card">
      <div class="between"><div><h2>视频理解路线</h2><p class="muted">先决定原生音视频与分镜证据如何组合；这里的选择同时作用于群视频和社交平台 MCP 的视频证据。</p></div><span class="tag">结构化表单</span></div>
      <div class="video-config-grid">
        ${videoConfigToggle("personification_video_understanding_enabled", value("personification_video_understanding_enabled", false), "启用视频理解", "关闭后不下载、不抽帧，也不调用独立视频 Provider。")}
        ${videoConfigSelect("personification_video_route_mode", value("personification_video_route_mode", "auto"), [
          {value:"auto",label:"自动：主模型原生，否则第三方路线"},{value:"primary",label:"仅主模型原生音视频"},{value:"external",label:"跳过主模型，使用第三方路线"},{value:"storyboard",label:"诊断：仅分镜 + 字幕/ASR"}
        ], "理解路线", "主模型没有已确认的视频协议时，自动模式会直接进入第三方路线。")}
        ${videoConfigSelect("personification_video_frame_preset", framePreset, [
          {value:"economy",label:"经济：3 分钟约 72 帧"},{value:"balanced",label:"均衡：3 分钟约 120 帧"},{value:"quality",label:"质量：3 分钟约 168 帧"},{value:"custom",label:"自定义帧预算"}
        ], "抽帧预设", "仅分镜或混合路线使用。")}
      </div>
    </section>
    <section class="card video-config-card">
      <div><h2>外部全模态 API</h2><p class="muted">只配置一个正式的第三方全模态 Provider。协议下拉决定真实视频载荷，不能仅凭 OpenAI-compatible 或模型名推断。</p></div>
      <div class="video-config-grid">
        ${videoConfigToggle("personification_fullmodal_provider_enabled", value("personification_fullmodal_provider_enabled", false), "启用外部全模态 API", "主模型与消费者网页不可用时允许调用。")}
        ${videoConfigSelect("personification_fullmodal_provider_protocol", provider, [
          {value:"gemini_native",label:"Gemini 原生 Files API（推荐）"},{value:"openai_qwen_omni",label:"Qwen3.5-Omni 官方扩展"},{value:"openai_mimo_v25",label:"MiMo-V2.5 官方扩展"},{value:"openai_custom_video_url",label:"自定义 OpenAI video_url"},{value:"disabled",label:"禁用"}
        ], "协议")}
        ${videoConfigInput("personification_fullmodal_provider_model", providerModel, "模型", {list:"video-native-models",placeholder:"留空使用协议预设模型",description:"自定义协议必须明确填写模型 ID。"})}
        ${videoConfigInput("personification_fullmodal_provider_api_key", providerKeyConfigured?"***":"", "API Key", {kind:"secret"})}
        <div data-fullmodal-provider-only="openai_qwen_omni" style="display:${provider==='openai_qwen_omni'?'block':'none'}">${videoConfigInput("personification_fullmodal_provider_workspace_id", value("personification_fullmodal_provider_workspace_id", ""), "百炼 WorkspaceId", {description:"Base URL 留空时据此生成北京地域 compatible-mode/v1 地址。"})}</div>
        ${videoConfigInput("personification_fullmodal_provider_api_url", value("personification_fullmodal_provider_api_url", ""), "Base URL", {placeholder:provider==="openai_qwen_omni"?"可留空并填写 WorkspaceId":"HTTPS Base URL",description:"自定义地址必须为 HTTPS。"})}
        ${videoConfigSelect("personification_fullmodal_provider_auth_mode", value("personification_fullmodal_provider_auth_mode", "auto"), [{value:"auto",label:"自动"},{value:"x-goog-api-key",label:"x-goog-api-key"},{value:"bearer",label:"Bearer"},{value:"api-key",label:"API-Key"}], "鉴权方式")}
        <div data-fullmodal-provider-only="openai_mimo_v25" style="display:${provider==='openai_mimo_v25'?'block':'none'}">${videoConfigInput("personification_fullmodal_provider_video_fps", value("personification_fullmodal_provider_video_fps",2), "视频 FPS", {kind:"float",min:0.1,max:10,step:0.1})}</div>
        <div data-fullmodal-provider-only="openai_mimo_v25" style="display:${provider==='openai_mimo_v25'?'block':'none'}">${videoConfigSelect("personification_fullmodal_provider_media_resolution", value("personification_fullmodal_provider_media_resolution","default"), [{value:"default",label:"默认"},{value:"low",label:"低"},{value:"medium",label:"中"},{value:"high",label:"高"}], "媒体分辨率")}</div>
        <div data-fullmodal-provider-only="openai_custom_video_url" style="display:${provider==='openai_custom_video_url'?'block':'none'}">${videoConfigToggle("personification_fullmodal_provider_stream", value("personification_fullmodal_provider_stream",false), "要求流式返回")}</div>
        ${videoConfigInput("personification_fullmodal_provider_timeout", value("personification_fullmodal_provider_timeout",600), "调用超时（秒）", {kind:"float",min:20,max:900,step:1})}
        ${videoConfigInput("personification_fullmodal_provider_max_bytes", videoConfigMiB(value("personification_fullmodal_provider_max_bytes",536870912),512), "媒体上限（MiB）", {kind:"mib",min:1,max:2048,step:1})}
      </div>
      <datalist id="video-native-models">
        <option value="qwen3.5-omni-plus">Qwen Omni</option><option value="qwen3.5-omni-flash">Qwen Omni Flash</option><option value="mimo-v2.5">MiMo-V2.5</option><option value="gemini-2.5-flash">Gemini 2.5 Flash</option><option value="gemini-2.0-flash">Gemini 2.0 Flash</option>
      </datalist>
      <div class="alert" data-video-provider-note style="margin-top:12px">${escapeHtml(videoProviderNote(provider))}</div>
    </section>
    ${renderGeminiWebCard(entries)}
    ${renderMimoWebAsrCard(entries)}
    <section class="card video-config-card" data-video-frame-section>
      <div><h2>分镜抽帧</h2><p class="muted">场景差分与字幕差分先低清扫描，再按时间顺序拼图；不会把 24 FPS 的每一帧全部交给模型。</p></div>
      ${videoConfigToggle("personification_video_storyboard_fallback_enabled", value("personification_video_storyboard_fallback_enabled",true), "启用最终分镜兜底", "所有全模态路线失败后仍尝试抽帧与字幕/ASR。")}
      <div data-video-custom-budgets style="display:${framePreset==='custom'?'block':'none'}">${renderVideoBudgetEditor(entries["personification_video_custom_frame_budgets"])}</div>
      <details ${framePreset==='custom'?'open':''}><summary>抽帧高级参数</summary><div class="video-config-grid" style="margin-top:10px">
        ${videoConfigInput("personification_video_custom_scan_fps", value("personification_video_custom_scan_fps", 5), "自定义扫描 FPS", {kind:"float",min:0.5,max:8,step:0.1})}
        ${videoConfigInput("personification_video_visual_soft_limit", value("personification_video_visual_soft_limit", 160), "视觉软上限（帧）", {kind:"int",min:8,max:256,step:1})}
        ${videoConfigInput("personification_video_visual_hard_limit", value("personification_video_visual_hard_limit", 192), "视觉硬上限（帧）", {kind:"int",min:12,max:256,step:1})}
        ${videoConfigInput("personification_video_max_scan_samples", value("personification_video_max_scan_samples", 1800), "低清扫描样本上限", {kind:"int",min:240,max:5000,step:1})}
        ${videoConfigInput("personification_video_contact_sheet_frames", value("personification_video_contact_sheet_frames", 8), "每张拼图帧数", {kind:"int",min:4,max:9,step:1})}
      </div></details>
    </section>
    <section class="card video-config-card">
      <div><h2>体积与超时</h2><p class="muted">表单统一使用 MiB 和秒，保存时自动转换成运行时字节值，不再要求手算大整数。</p></div>
      <div class="video-config-grid">
        ${videoConfigInput("personification_video_max_bytes", videoConfigMiB(value("personification_video_max_bytes", 268435456),256), "视频下载上限（MiB）", {kind:"mib",min:8,max:512,step:1})}
        ${videoConfigInput("personification_video_payload_max_bytes", videoConfigMiB(value("personification_video_payload_max_bytes",16777216),16), "分镜载荷上限（MiB）", {kind:"mib",min:1,max:32,step:1})}
        ${videoConfigInput("personification_video_download_timeout", value("personification_video_download_timeout",90), "下载超时（秒）", {kind:"float",min:8,max:180,step:1})}
        ${videoConfigInput("personification_video_analysis_timeout", value("personification_video_analysis_timeout",600), "单视频理解总超时（秒）", {kind:"float",min:20,max:900,step:1})}
      </div>
    </section>
    <section class="card video-config-card">
      <div><h2>分镜链路音频转写</h2><p class="muted">Qwen Audio 适合游戏黑话和热词；Paraformer 更便宜。原生 Qwen-Omni 已直接听取视频音轨时，ASR 主要用于分镜回退与交叉验证。</p></div>
      <div class="video-config-grid">
        ${videoConfigToggle("personification_audio_transcription_enabled", value("personification_audio_transcription_enabled",true), "启用云端音频转写", "不在轻量服务器加载本地语音模型。")}
        ${videoConfigSelect("personification_audio_transcription_provider", asrProvider, [
          {value:"auto",label:"自动：有 Key 时使用 Qwen Audio"},{value:"qwen_audio",label:"Qwen Audio 3 ASR Flash"},{value:"paraformer",label:"Paraformer v2（低成本）"},{value:"custom",label:"自定义接口"},{value:"disabled",label:"禁用"}
        ], "转写 Provider")}
        ${videoConfigInput("personification_audio_transcription_api_key", asrKeyConfigured?"***":"", "转写 API Key", {kind:"secret"})}
        ${videoConfigInput("personification_audio_transcription_workspace_id", value("personification_audio_transcription_workspace_id", ""), "百炼 WorkspaceId")}
        ${videoConfigInput("personification_audio_transcription_api_url", value("personification_audio_transcription_api_url", ""), "转写 API 地址", {placeholder:"预设可留空；custom 必填"})}
        ${videoConfigInput("personification_audio_transcription_model", value("personification_audio_transcription_model", ""), "模型覆盖", {list:"video-asr-models",placeholder:"留空使用预设模型"})}
        ${videoConfigInput("personification_audio_transcription_language", value("personification_audio_transcription_language", "auto"), "音频语言", {placeholder:"auto / zh / en"})}
        ${videoConfigInput("personification_audio_transcription_hotwords", hotwords, "固定热词", {kind:"strlist",description:"用中文逗号、英文逗号或换行分隔；不需要 JSON。"})}
      </div>
      <datalist id="video-asr-models"><option value="qwen-audio-3.0-asr-flash-filetrans"></option><option value="paraformer-v2"></option></datalist>
      <details><summary>转写高级参数</summary><div class="video-config-grid" style="margin-top:10px">
        ${videoConfigSelect("personification_audio_transcription_custom_protocol", value("personification_audio_transcription_custom_protocol","dashscope_async_url"), [{value:"dashscope_async_url",label:"DashScope 异步公网 URL"},{value:"openai_multipart",label:"OpenAI multipart 上传"},{value:"json_base64",label:"JSON Base64"}], "自定义协议")}
        ${videoConfigInput("personification_audio_transcription_prompt", value("personification_audio_transcription_prompt", ""), "固定上下文提示")}
        ${videoConfigToggle("personification_audio_transcription_diarization_enabled", value("personification_audio_transcription_diarization_enabled",false), "说话人分离")}
        ${videoConfigInput("personification_audio_transcription_speaker_count", value("personification_audio_transcription_speaker_count",0), "预期说话人数（0 自动）", {kind:"int",min:0,max:24,step:1})}
        ${videoConfigInput("personification_audio_transcription_timeout", value("personification_audio_transcription_timeout",180), "转写超时（秒）", {kind:"float",min:15,max:600,step:1})}
        ${videoConfigInput("personification_audio_transcription_poll_seconds", value("personification_audio_transcription_poll_seconds",1.5), "轮询间隔（秒）", {kind:"float",min:0.5,max:10,step:0.1})}
        ${videoConfigInput("personification_audio_transcription_max_bytes", videoConfigMiB(value("personification_audio_transcription_max_bytes",26214400),25), "本地音频上传上限（MiB）", {kind:"mib",min:0.0625,max:64,step:1})}
        ${videoConfigInput("personification_audio_transcription_max_chars", value("personification_audio_transcription_max_chars",12000), "转写文本上限（字符）", {kind:"int",min:500,max:50000,step:100})}
      </div></details>
    </section>
    <div class="video-config-actions"><button class="btn primary" onclick="saveVideoUnderstandingConfig()">保存视频理解配置</button><button class="btn" onclick="resetVideoUnderstandingDrafts()">放弃未保存修改</button><span class="muted">一次原子写入 env.json，只触发一次运行时重载。</span></div>
  </div>`;
}

function refreshVideoConfigVisibility() {
  const providerControl = document.querySelector('[data-video-field="personification_fullmodal_provider_protocol"]');
  const provider = providerControl ? providerControl.value : "gemini_native";
  document.querySelectorAll("[data-fullmodal-provider-only]").forEach(element => {
    element.style.display = element.dataset.fullmodalProviderOnly === provider ? "block" : "none";
  });
  const preset = document.querySelector('[data-video-field="personification_video_frame_preset"]')?.value || "balanced";
  const custom = document.querySelector("[data-video-custom-budgets]");
  if (custom) custom.style.display = preset === "custom" ? "block" : "none";
  const note = document.querySelector("[data-video-provider-note]");
  if (note) note.textContent = videoProviderNote(provider);
}

function readVideoUnderstandingForm() {
  const values = {};
  document.querySelectorAll("[data-video-field]").forEach(input => {
    const field = input.dataset.videoField;
    const kind = input.dataset.videoKind || "text";
    if (!field || (kind === "secret" && input.value === "")) return;
    let value = input.value;
    if (kind === "bool") value = Boolean(input.checked);
    else if (kind === "int") value = Math.round(Number(input.value));
    else if (kind === "float") value = Number(input.value);
    else if (kind === "mib") value = Math.round(Number(input.value) * 1024 * 1024);
    else if (kind === "strlist") value = input.value.split(/[，,\n]/).map(item => item.trim()).filter(Boolean);
    if (["int","float","mib"].includes(kind) && !Number.isFinite(value)) throw new Error(`${field} 需要有效数字`);
    values[field] = value;
  });
  const budgetRoot = document.querySelector('[data-video-budget-field="personification_video_custom_frame_budgets"]');
  if (budgetRoot) {
    const budgets = {};
    budgetRoot.querySelectorAll("[data-video-budget-key]").forEach(input => { budgets[input.dataset.videoBudgetKey] = Math.round(Number(input.value)); });
    if (Object.values(budgets).some(value => !Number.isFinite(value))) throw new Error("自定义抽帧预算需要有效整数");
    values.personification_video_custom_frame_budgets = budgets;
  }
  return values;
}

async function saveVideoUnderstandingConfig() {
  let values;
  try { values = readVideoUnderstandingForm(); }
  catch (error) { alertFlash("err", error.message || "视频理解表单包含无效值"); return; }
  try {
    const result = await api("/config/video-understanding", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({values})});
    const operation = configRememberDiagnostic(result, "视频理解配置保存未完成");
    if (result.success) {
      (result.updated || []).forEach(clearConfigDraft);
      alertFlash("ok", operation?.title || `已保存 ${result.updated.length} 项视频理解配置`);
      await loadView(); render(); queueMicrotask(refreshVideoConfigVisibility);
    } else alertFlash("err", operation?.title || "视频理解配置保存未完成");
  } catch (error) {
    const operation = configRememberDiagnostic(error, "视频理解配置保存未完成");
    alertFlash("err", operation?.title || "视频理解配置保存未完成");
  }
}

function resetVideoUnderstandingDrafts() {
  VIDEO_CONFIG_FIELDS.forEach(clearConfigDraft);
  render();
  queueMicrotask(refreshVideoConfigVisibility);
}

function renderConfig() {
  const search = normalizeConfigSearchText(state.configSearch || "");
  const searchTokens = search ? search.split(/\s+/).filter(Boolean) : [];
  let items = state.entries;
  let activeGroup = state.activeGroup;
  if (searchTokens.length) {
    items = items
      .map(e => ({ entry: e, score: configSearchEntryScore(e, searchTokens) }))
      .filter(item => item.score >= 0)
      .sort((a, b) => b.score - a.score || String(a.entry.group).localeCompare(String(b.entry.group), "zh-CN") || String(a.entry.label).localeCompare(String(b.entry.label), "zh-CN"))
      .map(item => item.entry);
    activeGroup = null;
  } else if (activeGroup) {
    items = items.filter(e => e.group === activeGroup);
  }
  const renderVideoEditor = !search && activeGroup === "视频理解";
  const videoEditorItems = renderVideoEditor ? state.entries.filter(entry => entry.group === "视频理解") : [];
  // advanced 折叠：默认隐藏 advanced=true 字段；视频理解使用自己的分区与高级折叠。
  const totalBeforeAdvanced = items.length;
  if (!state.showAdvancedConfig && !renderVideoEditor) {
    items = items.filter(e => !e.advanced);
  }
  const hiddenAdvanced = totalBeforeAdvanced - items.length;
  const groupBar = !search ? state.groups.map(g => {
    const groupEntries = state.entries.filter(e => e.group === g);
    const visibleCount = state.showAdvancedConfig ? groupEntries.length : groupEntries.filter(e => !e.advanced).length;
    return `<button class="${g===activeGroup?'active':''}" onclick="pickGroup('${escapeAttr(g)}')">${escapeHtml(g)} <span class="muted" style="font-size:11px">${visibleCount}/${groupEntries.length}</span></button>`;
  }).join("") : "";
  const heading = search ? `搜索结果（${items.length}）` : (activeGroup || '配置');
  const diagnostics = renderOperationHistory(
    Array.isArray(state.configDiagnostics) ? state.configDiagnostics : [],
    {group:`view-${state.view}`},
  );
  const diagnosticCard = diagnostics
    ? `<div class="card"><div class="between"><h2>配置操作诊断</h2><button class="btn small" onclick="configClearDiagnostics()">清空</button></div>${diagnostics}</div>`
    : "";
  return `<div class="toolbar">
      <input id="config-search-input" type="search" placeholder="搜索字段名 / 标签 / 描述…" value="${escapeAttr(state.configSearch)}" oncompositionstart="onConfigSearchCompositionStart(this)" oncompositionend="onConfigSearchCompositionEnd(this)" oninput="onConfigSearchInput(this,event)" style="flex:1;max-width:340px">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
        <input type="checkbox" ${state.showAdvancedConfig?'checked':''} onchange="state.showAdvancedConfig=this.checked;render()" style="width:auto">
        显示高级配置
      </label>
      <button class="btn" onclick="applyRecommended()">应用推荐默认值</button>
    </div>
    <div class="alert" style="margin-bottom:10px">
      插件配置由数据目录下的 <code>env.json</code> 持久化；<code>.env.prod</code> 仅在首次启用时导入插件字段，后续 WebUI 保存不会改写它。<code>SUPERUSERS</code> 等 NoneBot 基础配置仍放在 <code>.env.prod</code>。
    </div>
    ${diagnosticCard}
    ${groupBar ? `<div class="group-bar">${groupBar}</div>` : ''}
    ${renderVideoEditor ? renderVideoUnderstandingEditor(videoEditorItems) : `<div class="card">
      <h2>${escapeHtml(heading)} ${hiddenAdvanced ? `<span class="muted" style="font-size:12px;font-weight:normal">（已折叠 ${hiddenAdvanced} 项高级配置）</span>` : ''}</h2>
      ${items.length ? items.map(renderField).join("") : '<p class="muted">无匹配字段</p>'}
    </div>`}`;
}

async function applyRecommended() {
  if (!confirm("将一组推荐配置写入插件 env.json，覆盖现有插件配置；不会改写 .env.prod。继续？")) return;
  try {
    const result = await api("/config/apply-recommended", { method:"POST", headers:{"content-type":"application/json"}, body: "{}" });
    const operation = configRememberDiagnostic(result, "推荐默认值应用未完成");
    const lines = [`已应用 ${result.applied.length} 项`];
    if (result.skipped.length) lines.push(`跳过 ${result.skipped.length}：` + result.skipped.map(s=>`${s.field_name}（${s.reason}）`).slice(0,3).join("、"));
    alertFlash(operation?.ok === false ? "err" : "ok", operation?.title || lines.join("；"));
    await loadView(); render();
  } catch (e) { const operation = configRememberDiagnostic(e, "推荐默认值应用未完成"); alertFlash("err", operation?.title || "推荐默认值应用未完成"); }
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
  const cur = configDraftValue(e);
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
  if (e.kind === "strlist") {
    return renderStrListEditor(e);
  }
  if (e.field_name === "personification_video_custom_frame_budgets") {
    return renderVideoBudgetEditor(e, true);
  }
  if (e.kind === "json") {
    const text = cur == null ? "" : (typeof cur === "string" ? cur : JSON.stringify(cur, null, 2));
    return `<textarea data-raw="json" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">${escapeHtml(text)}</textarea>
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'json')">保存</button>`;
  }
  if (e.kind === "int") {
    return `<input type="number" step="1" value="${escapeAttr(cur==null?'':cur)}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'int')">保存</button>`;
  }
  if (e.kind === "float") {
    return `<input type="number" step="0.01" value="${escapeAttr(cur==null?'':cur)}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'float')">保存</button>`;
  }
  if (e.kind === "secret") {
    const secretValue = configDraft(e.field_name) ? cur : "";
    return `<input type="password" value="${escapeAttr(secretValue||'')}" placeholder="${e.current ? '已设置（输入新值覆盖）' : '未设置'}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'secret')">保存</button>`;
  }
  return `<input type="text" value="${escapeAttr(cur==null?'':cur)}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
    <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'text')">保存</button>`;
}

function strListValue(cur) {
  if (Array.isArray(cur)) return cur.map(x => String(x));
  if (typeof cur === "string" && cur.trim()) {
    try { const p = JSON.parse(cur); if (Array.isArray(p)) return p.map(x => String(x)); } catch {}
    return cur.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  }
  return [];
}

function renderStrListEditor(e) {
  const items = strListValue(configDraftValue(e));
  const field = escapeAttr(e.field_name);
  const rows = items.map(v => `<div class="strlist-row" data-strlist-row>
      <input type="text" value="${escapeAttr(v)}" oninput="syncStrListDraft('${field}')">
      <button class="btn small danger" onclick="this.closest('[data-strlist-row]').remove();syncStrListDraft('${field}')">删</button>
    </div>`).join("");
  return `<div class="strlist-editor" data-strlist-field="${field}">
    <div class="strlist-rows">${rows || '<div class="muted" style="font-size:12px">（空）</div>'}</div>
    <div class="row" style="margin-top:6px">
      <button class="btn small" onclick="addStrListRow('${field}')">+ 添加一项</button>
      <button class="btn small primary" onclick="saveStrList('${field}')">保存</button>
    </div>
  </div>`;
}

function addStrListRow(field) {
  const root = document.querySelector(`[data-strlist-field="${CSS.escape(field)}"] .strlist-rows`);
  if (!root) return;
  const empty = root.querySelector(".muted"); if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "strlist-row"; div.setAttribute("data-strlist-row", "");
  div.innerHTML = `<input type="text" value="" oninput="syncStrListDraft('${escapeAttr(field)}')"><button class="btn small danger" onclick="this.closest('[data-strlist-row]').remove();syncStrListDraft('${escapeAttr(field)}')">删</button>`;
  root.appendChild(div);
  syncStrListDraft(field);
  div.querySelector("input").focus();
}

function syncStrListDraft(field) {
  const root = document.querySelector(`[data-strlist-field="${CSS.escape(field)}"]`);
  if (!root) return [];
  const values = Array.from(root.querySelectorAll('[data-strlist-row] input')).map(input => input.value);
  setConfigValueDraft(field, values, "strlist");
  return values;
}

function saveStrList(field) {
  const values = syncStrListDraft(field);
  saveField(field, values.map(value => value.trim()).filter(Boolean), {preserveDraft:true});
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

function sanitizeApiProvider(provider) {
  const out = {...(provider || {})};
  delete out._model_options;
  delete out._model_source;
  delete out._model_probe_done;
  return out;
}

function sanitizeApiProviders(providers) {
  return (providers || []).map(p => sanitizeApiProvider(p));
}

function apiPoolDraftState(field) {
  const draft = configDraft(field);
  return draft && draft.kind === "api_pool" ? draft : null;
}

function setApiPoolDraft(field, providers, options={}) {
  if (!state.configDrafts || typeof state.configDrafts !== "object") state.configDrafts = {};
  const previous = apiPoolDraftState(field);
  const cleanProviders = Array.isArray(providers) ? providers.map(provider => ({...(provider || {})})) : [];
  state.configDrafts[field] = {
    kind: "api_pool",
    providers: cleanProviders,
    rawText: options.rawText !== undefined
      ? String(options.rawText)
      : (previous ? previous.rawText : JSON.stringify(sanitizeApiProviders(cleanProviders), null, 2)),
    rawVisible: options.rawVisible !== undefined ? Boolean(options.rawVisible) : Boolean(previous && previous.rawVisible),
  };
  return state.configDrafts[field];
}

const apiProviderModelProbeCache = new Map();

function apiProviderProbeCacheKey(field, index, provider) {
  const parts = [
    field,
    index,
    provider && provider.name,
    provider && provider.api_type,
    provider && provider.api_url,
    provider && provider.auth_path,
    provider && provider.project,
    provider && provider.gemini_auth_mode,
    provider && provider.media_protocol,
  ];
  return parts.map(item => String(item == null ? "" : item)).join("\u001f");
}

function cacheApiProviderModelProbe(field, index, provider) {
  const key = apiProviderProbeCacheKey(field, index, provider);
  if (provider && Array.isArray(provider._model_options)) {
    apiProviderModelProbeCache.set(key, {
      models: normalizeApiProviderModels(provider._model_options),
      source: String(provider._model_source || ""),
      done: provider._model_probe_done === true,
    });
  }
}

function hydrateApiProviderModelProbe(field, index, provider) {
  const cloned = {...(provider || {})};
  const cached = apiProviderModelProbeCache.get(apiProviderProbeCacheKey(field, index, cloned));
  if (!cached) return cloned;
  cloned._model_options = cached.models;
  cloned._model_source = cached.source;
  cloned._model_probe_done = cached.done;
  return cloned;
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
    media_protocol: "auto",
    timeout: 200,
    max_retries: 5,
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

function apiProviderModelId(item) {
  if (typeof item === "string") return item.trim();
  if (!item || typeof item !== "object") return "";
  return String(item.id || item.model || item.name || item.slug || "").trim();
}

function apiProviderModelLabel(item, id) {
  if (typeof item === "string") return id;
  if (!item || typeof item !== "object") return id;
  return String(item.label || item.display_name || item.displayName || item.source || id || "").trim();
}

function normalizeApiProviderModels(items) {
  const rawItems = Array.isArray(items) ? items : [];
  const seen = new Set();
  const models = [];
  rawItems.forEach(item => {
    const id = apiProviderModelId(item);
    if (!id) return;
    const key = id.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    const label = apiProviderModelLabel(item, id);
    models.push({id, label});
  });
  return models;
}

function renderApiProviderModelDatalistOptions(models) {
  return normalizeApiProviderModels(models).map(item =>
    `<option value="${escapeAttr(item.id)}" label="${escapeAttr(item.label || item.id)}"></option>`
  ).join("");
}

function renderApiProviderModelSelectOptions(models, value) {
  return normalizeApiProviderModels(models).map(item => {
    const text = item.label && item.label !== item.id ? `${item.id} · ${item.label}` : item.id;
    return `<option value="${escapeAttr(item.id)}" ${value===item.id?'selected':''}>${escapeHtml(text)}</option>`;
  }).join("");
}

function updateApiProviderModelControls(card, models, source) {
  if (!card) return;
  const field = card.querySelector('[data-provider-field="model"]');
  if (!field) return;
  const input = field.querySelector("[data-provider-model-input]");
  const select = field.querySelector("[data-provider-model-select]");
  const datalistId = input ? input.getAttribute("list") : "";
  const datalist = datalistId ? document.getElementById(datalistId) : null;
  const currentValue = input ? input.value : "";
  const normalized = normalizeApiProviderModels(models);
  if (select) {
    const placeholder = normalized.length ? "选择模型" : "未探测到可选模型";
    select.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>${renderApiProviderModelSelectOptions(normalized, currentValue)}`;
    select.value = normalized.some(item => item.id === currentValue) ? currentValue : "";
  }
  if (datalist) datalist.innerHTML = renderApiProviderModelDatalistOptions(normalized);
  const oldHint = field.querySelector("[data-provider-model-hint]");
  if (oldHint) oldHint.remove();
  const hint = document.createElement("div");
  hint.className = "muted";
  hint.dataset.providerModelHint = "1";
  hint.style.fontSize = "11px";
  const modelSource = source ? `，来源：${source}` : "";
  hint.textContent = normalized.length ? `已探测 ${normalized.length} 个模型${modelSource}，可输入筛选或手填。` : "未探测到可选模型，仍可手动填写模型 ID。";
  field.appendChild(hint);
}

function renderApiPoolEditor(e) {
  const draft = apiPoolDraftState(e.field_name);
  const source = draft ? draft.providers : normalizeApiPoolValue(e.current);
  const providers = source.map((provider, index) =>
    hydrateApiProviderModelProbe(e.field_name, index, provider || {})
  );
  const rawVisible = Boolean(draft && draft.rawVisible);
  const rawText = draft ? draft.rawText : JSON.stringify(sanitizeApiProviders(providers), null, 2);
  const cards = providers.map((provider, index) => renderApiProviderCard(e.field_name, provider || {}, index)).join("");
  return `<div class="api-pool-editor" data-api-pool-field="${escapeAttr(e.field_name)}">
    <div class="api-provider-actions">
      <button class="btn small" onclick="addApiProvider('${escapeAttr(e.field_name)}')">+ 添加 Provider</button>
      <button class="btn small primary" onclick="saveApiPool('${escapeAttr(e.field_name)}')">保存全部</button>
      <button class="btn small" onclick="toggleApiPoolRaw(this)">${rawVisible?'隐藏 JSON':'查看 JSON'}</button>
    </div>
    <div class="api-provider-list">${cards || '<div class="api-pool-empty">暂无 provider，点击“添加 Provider”创建。</div>'}</div>
    <textarea data-api-pool-raw style="display:${rawVisible?'block':'none'};min-height:120px" oninput="syncApiPoolRawDraft(this)">${escapeHtml(rawText)}</textarea>
  </div>`;
}

function renderApiProviderCard(field, provider, index) {
  provider = hydrateApiProviderModelProbe(field, index, provider || {});
  const apiType = provider.api_type || "openai";
  const choices = ["openai", "openai_codex", "gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code"];
  const typeOptions = choices.map(c => `<option value="${escapeAttr(c)}" ${apiType===c?'selected':''}>${escapeHtml(c)}</option>`).join("");
  const fieldHtml = (name, label, type = "text", extra = "") => {
    if (!apiProviderFieldVisible(apiType, name)) return "";
    const value = provider[name] == null ? "" : provider[name];
    return `<div class="api-provider-field" data-provider-field="${escapeAttr(name)}">
      <label>${escapeHtml(label)}</label>
      <input type="${escapeAttr(type)}" value="${escapeAttr(value)}" ${extra} oninput="syncApiPoolDraft('${escapeAttr(field)}')">
    </div>`;
  };
  const modelFieldHtml = () => {
    if (!apiProviderFieldVisible(apiType, "model")) return "";
    const value = provider.model == null ? "" : provider.model;
    const options = Array.isArray(provider._model_options) ? provider._model_options : [];
    const listId = `api-provider-models-${field}-${index}`.replace(/[^\w-]/g, "-");
    const selectId = `${listId}-select`;
    const normalizedOptions = normalizeApiProviderModels(options);
    const optionHtml = renderApiProviderModelDatalistOptions(normalizedOptions);
    const probeDone = provider._model_probe_done === true;
    const selectPlaceholder = normalizedOptions.length ? "选择模型" : (probeDone ? "未探测到可选模型" : "先探测模型");
    const selectHtml = `<select id="${escapeAttr(selectId)}" data-provider-model-select onchange="selectApiProviderModel(this)" aria-label="选择模型">
      <option value="">${escapeHtml(selectPlaceholder)}</option>
      ${renderApiProviderModelSelectOptions(normalizedOptions, value)}
    </select>`;
    const modelSource = provider._model_source ? `，来源：${provider._model_source}` : "";
    const sourceHint = normalizedOptions.length
      ? `<div class="muted" data-provider-model-hint style="font-size:11px">已探测 ${normalizedOptions.length} 个模型${escapeHtml(modelSource)}，可输入筛选或手填。</div>`
      : (probeDone ? `<div class="muted" data-provider-model-hint style="font-size:11px">未探测到可选模型，仍可手动填写模型 ID。</div>` : "");
    return `<div class="api-provider-field api-provider-model-field" data-provider-field="model">
      <label>模型</label>
      <div class="api-provider-model-row">
        <input type="text" data-provider-model-input list="${escapeAttr(listId)}" value="${escapeAttr(value)}" placeholder="先探测或手动填写模型 ID" oninput="syncApiProviderModelSelect(this)">
        ${selectHtml}
        <button class="btn small" type="button" onclick="probeApiProviderModels('${escapeAttr(field)}', ${index}, this)">探测模型</button>
      </div>
      <datalist id="${escapeAttr(listId)}">${optionHtml}</datalist>
      ${sourceHint}
    </div>`;
  };
  const geminiAuthFieldHtml = () => {
    if (!["gemini", "gemini_official"].includes(String(apiType).replaceAll("-", "_"))) return "";
    const value = provider.gemini_auth_mode || "auto";
    const options = [
      ["auto", "自动（x-goog 优先，401 尝试 Bearer）"],
      ["x-goog-api-key", "x-goog-api-key"],
      ["bearer", "Authorization Bearer"],
      ["query_legacy", "Query key（旧兼容，不推荐）"],
    ].map(([id, label]) => `<option value="${escapeAttr(id)}" ${value===id?'selected':''}>${escapeHtml(label)}</option>`).join("");
    return `<div class="api-provider-field" data-provider-field="gemini_auth_mode">
      <label>Gemini 认证</label>
      <select onchange="syncApiPoolDraft('${escapeAttr(field)}')">${options}</select>
    </div>`;
  };
  const mediaProtocolFieldHtml = () => {
    const value = String(provider.media_protocol || "auto");
    const options = [
      ["auto", "自动（仅官方已确认模型）"],
      ["none", "无音视频输入"],
      ["gemini_native", "Gemini 原生 Files API"],
      ["antigravity_native", "AGY 原生 inlineData / fileData"],
      ["openai_qwen_omni", "Qwen3.5-Omni video_url / input_audio"],
      ["openai_mimo_v25", "MiMo-V2.5 video_url / input_audio"],
    ].map(([id, label]) => `<option value="${escapeAttr(id)}" ${value===id?'selected':''}>${escapeHtml(label)}</option>`).join("");
    return `<div class="api-provider-field" data-provider-field="media_protocol">
      <label>音视频输入协议</label>
      <select onchange="syncApiPoolDraft('${escapeAttr(field)}')">${options}</select>
      <div class="muted" style="font-size:11px">API 类型不代表支持视频；代理网关请显式选择其真实协议。</div>
    </div>`;
  };
  return `<div class="api-provider-card" data-provider-index="${index}" data-provider-secret-ref="${escapeAttr(provider._secret_ref || "")}">
    <div class="api-provider-head">
      <div class="api-provider-title">Provider ${index + 1}</div>
      <button class="btn small danger" onclick="removeApiProvider('${escapeAttr(field)}', ${index})">删除</button>
    </div>
    <div class="api-provider-grid">
      ${fieldHtml("name", "名称")}
      <div class="api-provider-field" data-provider-field="priority">
        <label>优先级</label>
        <input type="number" step="1" value="${escapeAttr(provider.priority ?? index)}" oninput="syncApiPoolDraft('${escapeAttr(field)}')">
      </div>
      <div class="api-provider-field" data-provider-field="api_type">
        <label>类型</label>
        <select onchange="refreshApiPoolEditor('${escapeAttr(field)}')">${typeOptions}</select>
      </div>
      ${fieldHtml("api_url", "API URL")}
      ${fieldHtml("api_key", "API Key", "password")}
      ${geminiAuthFieldHtml()}
      ${mediaProtocolFieldHtml()}
      ${modelFieldHtml()}
      ${fieldHtml("auth_path", "Auth Path")}
      ${fieldHtml("project", "Project")}
      ${fieldHtml("proxy", "代理")}
      ${fieldHtml("timeout", "单次超时（秒）", "number", 'min="5" max="600" step="1"')}
      ${fieldHtml("max_retries", "总尝试次数", "number", 'min="1" max="10" step="1" title="包含首次请求；5 表示首次加 4 次重试"')}
      <div class="api-provider-field" data-provider-field="enabled">
        <label>启用</label>
        <select onchange="syncApiPoolDraft('${escapeAttr(field)}')">
          <option value="true" ${provider.enabled !== false ? 'selected' : ''}>是</option>
          <option value="false" ${provider.enabled === false ? 'selected' : ''}>否</option>
        </select>
      </div>
    </div>
  </div>`;
}

function selectApiProviderModel(select) {
  const field = select.closest("[data-provider-field]");
  const input = field ? field.querySelector("[data-provider-model-input]") : null;
  if (!input) return;
  input.value = select.value || "";
  markDirty(input);
  const root = select.closest("[data-api-pool-field]");
  if (root) syncApiPoolDraft(root.dataset.apiPoolField);
}

function syncApiProviderModelSelect(input) {
  markDirty(input);
  const field = input.closest("[data-provider-field]");
  const select = field ? field.querySelector("[data-provider-model-select]") : null;
  if (!select) return;
  const hasOption = Array.from(select.options).some(option => option.value === input.value);
  select.value = hasOption ? input.value : "";
  const root = input.closest("[data-api-pool-field]");
  if (root) syncApiPoolDraft(root.dataset.apiPoolField);
}

function readApiPoolEditor(field) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return [];
  const raw = root.querySelector("[data-api-pool-raw]");
  if (raw && raw.style.display !== "none") {
    try {
      const parsed = JSON.parse(raw.value);
      if (!Array.isArray(parsed)) throw new Error("API Pool JSON 必须是数组");
      return sanitizeApiProviders(parsed);
    } catch {
      throw new Error("API Pool JSON 格式错误");
    }
  }
  return Array.from(root.querySelectorAll(".api-provider-card")).map((card, index) => {
    const provider = defaultApiProvider(index);
    if (card.dataset.providerSecretRef) provider._secret_ref = card.dataset.providerSecretRef;
    card.querySelectorAll("[data-provider-field]").forEach(wrap => {
      const name = wrap.dataset.providerField;
      const input = wrap.querySelector("input, select");
      if (!input) return;
      let value = input.value;
      if (name === "enabled") value = value === "true";
      if (name === "priority" || name === "timeout" || name === "max_retries") value = value === "" ? undefined : parseInt(value, 10);
      if (value !== "" && value !== undefined) provider[name] = value;
      else delete provider[name];
    });
    return hydrateApiProviderModelProbe(field, index, provider);
  });
}

function syncApiPoolDraft(field) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return [];
  const raw = root.querySelector("[data-api-pool-raw]");
  const rawVisible = Boolean(raw && raw.style.display !== "none");
  if (rawVisible) {
    const rawText = raw.value;
    let providers = apiPoolDraftState(field)?.providers || [];
    try {
      const parsed = JSON.parse(rawText);
      if (Array.isArray(parsed)) providers = parsed;
    } catch {}
    setApiPoolDraft(field, providers, {rawText, rawVisible:true});
    return providers;
  }
  const providers = readApiPoolEditor(field);
  setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
  return providers;
}

function syncApiPoolRawDraft(raw) {
  const root = raw ? raw.closest("[data-api-pool-field]") : null;
  if (!root) return;
  syncApiPoolDraft(root.dataset.apiPoolField);
  markDirty(raw);
}

function writeApiPoolEditor(field, providers) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return;
  const list = root.querySelector(".api-provider-list");
  list.innerHTML = providers.map((provider, index) => renderApiProviderCard(field, provider, index)).join("") || '<div class="api-pool-empty">暂无 provider，点击“添加 Provider”创建。</div>';
  const raw = root.querySelector("[data-api-pool-raw]");
  const draft = apiPoolDraftState(field);
  if (raw) {
    raw.value = draft ? draft.rawText : JSON.stringify(sanitizeApiProviders(providers), null, 2);
    raw.style.display = draft && draft.rawVisible ? "block" : "none";
  }
  const toggle = root.querySelector(".api-provider-actions .btn:last-child");
  if (toggle) toggle.textContent = draft && draft.rawVisible ? "隐藏 JSON" : "查看 JSON";
}

function refreshApiPoolEditor(field) {
  try {
    const providers = readApiPoolEditor(field);
    setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function addApiProvider(field) {
  try {
    const providers = readApiPoolEditor(field);
    providers.push(defaultApiProvider(providers.length));
    setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function removeApiProvider(field, index) {
  try {
    const providers = readApiPoolEditor(field);
    providers.splice(index, 1);
    setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function toggleApiPoolRaw(btn) {
  const root = btn.closest(".api-pool-editor");
  const raw = root.querySelector("[data-api-pool-raw]");
  const showing = raw.style.display !== "none";
  const field = root.dataset.apiPoolField;
  if (!showing) {
    const providers = readApiPoolEditor(field);
    const rawText = JSON.stringify(sanitizeApiProviders(providers), null, 2);
    setApiPoolDraft(field, providers, {rawText, rawVisible:true});
    raw.value = rawText;
    raw.style.display = "block";
    btn.textContent = "隐藏 JSON";
    return;
  }
  try {
    const parsed = JSON.parse(raw.value);
    if (!Array.isArray(parsed)) throw new Error("API Pool JSON 必须是数组");
    const providers = sanitizeApiProviders(parsed);
    setApiPoolDraft(field, providers, {rawText:raw.value, rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) {
    setApiPoolDraft(field, apiPoolDraftState(field)?.providers || [], {rawText:raw.value, rawVisible:true});
    alertFlash("err", e.message || "API Pool JSON 格式错误");
  }
}

async function saveApiPool(field) {
  try {
    const providers = readApiPoolEditor(field);
    const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
    const raw = root ? root.querySelector("[data-api-pool-raw]") : null;
    setApiPoolDraft(field, providers, {
      rawText:raw ? raw.value : JSON.stringify(sanitizeApiProviders(providers), null, 2),
      rawVisible:Boolean(raw && raw.style.display !== "none"),
    });
    await saveField(field, sanitizeApiProviders(providers), {preserveDraft:true});
  } catch (e) { const operation = configRememberDiagnostic(e, "API Pool 保存未完成"); alertFlash("err", operation?.title || "API Pool 保存未完成"); }
}

async function probeApiProviderModels(field, index, btn) {
  let providers;
  try {
    providers = readApiPoolEditor(field);
    const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
    const raw = root ? root.querySelector("[data-api-pool-raw]") : null;
    setApiPoolDraft(field, providers, {
      rawText:raw ? raw.value : JSON.stringify(sanitizeApiProviders(providers), null, 2),
      rawVisible:Boolean(raw && raw.style.display !== "none"),
    });
  } catch (e) {
    const operation = configRememberDiagnostic(e, "Provider 模型探测参数无效");
    alertFlash("err", operation?.title || "Provider 模型探测参数无效");
    return;
  }
  const provider = sanitizeApiProvider(providers[index]);
  if (!provider) return;
  const requestIdentity = apiProviderProbeCacheKey(field, index, provider);
  const oldText = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "探测中…"; }
  try {
    const result = await api("/config/provider-models", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({provider}),
    });
    const operation = configRememberDiagnostic(result, "Provider 模型探测未完成");
    const models = normalizeApiProviderModels(result.models);
    const probedProvider = {...provider, _model_options: models, _model_source: result.source || "", _model_probe_done: true};
    cacheApiProviderModelProbe(field, index, probedProvider);
    let latestProviders;
    try {
      latestProviders = readApiPoolEditor(field);
    } catch {
      alertFlash("info", "探测已完成；当前草稿仍在编辑，结果将在 Provider 参数恢复匹配时可用");
      return;
    }
    const latestProvider = sanitizeApiProvider(latestProviders[index]);
    if (!latestProvider || apiProviderProbeCacheKey(field, index, latestProvider) !== requestIdentity) {
      alertFlash("info", "探测已完成；Provider 参数已变化，未覆盖当前草稿");
      return;
    }
    latestProviders[index] = {...latestProviders[index], _model_options: models, _model_source: result.source || "", _model_probe_done: true};
    cacheApiProviderModelProbe(field, index, latestProviders[index]);
    const previousDraft = apiPoolDraftState(field);
    setApiPoolDraft(field, latestProviders, {
      rawText:JSON.stringify(sanitizeApiProviders(latestProviders), null, 2),
      rawVisible:Boolean(previousDraft && previousDraft.rawVisible),
    });
    writeApiPoolEditor(field, latestProviders);
    alertFlash(models.length ? "ok" : "err", operation?.title || (models.length ? `已探测 ${models.length} 个模型` : "未探测到模型，请手动填写"));
  } catch (e) {
    const operation = configRememberDiagnostic(e, "Provider 模型探测未完成");
    alertFlash("err", operation?.title || "Provider 模型探测未完成");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = oldText || "探测模型"; }
  }
}

function activeSourceLabel(src) {
  return ({env_file:".env.prod 首次导入",env_json:"env.json",runtime_config:"runtime_config.json",default:"默认"})[src] || src;
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
  await saveField(field, value, {preserveDraft:true});
}

async function saveField(field, value, options={}) {
  if (!options.preserveDraft) setConfigValueDraft(field, value);
  const submittedDraft = configDraft(field);
  try {
    const result = await api("/config/value", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ field_name: field, value }) });
    const operation = configRememberDiagnostic(result, "配置保存未完成");
    if (result.success) {
      const entry = state.entries.find(item => item.field_name === field);
      if (entry && Object.prototype.hasOwnProperty.call(result, "new_value")) entry.current = result.new_value;
      if (configDraft(field) === submittedDraft) clearConfigDraft(field);
      alertFlash("ok", operation?.title || `已保存 ${field} 到插件 env.json`);
      await loadView(); render();
    }
    else { alertFlash("err", operation?.title || "配置保存仅部分完成"); await loadView(); render(); }
  } catch (e) { const operation = configRememberDiagnostic(e, "配置保存未完成"); alertFlash("err", operation?.title || "配置保存未完成"); }
}

function pickGroup(g) { state.activeGroup = g; render(); }
