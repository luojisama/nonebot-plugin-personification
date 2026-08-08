function _fmtTs(ts) {
  ts = Number(ts) || 0;
  return ts > 0 ? new Date(ts * 1000).toLocaleString() : "—";
}
function _fmtDuration(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  if (sec === 0) return "现在可发";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h > 0) return `约 ${h} 小时 ${m} 分后`;
  if (m > 0) return `约 ${m} 分后`;
  return `约 ${sec} 秒后`;
}

let _qzoneLoginPollTimer = 0;
let _qzoneLoginPollController = null;
let _qzoneSnapshotTimer = 0;
let _qzoneSnapshotController = null;
let _qzoneSnapshotPromise = null;
let _qzoneRenderedFingerprint = "";

function setQzoneOperationId(operationId) {
  const value = String(operationId || "").trim();
  state.qzoneOperationId = value;
  try {
    if (value) sessionStorage.setItem(QZONE_OPERATION_STORAGE_KEY, value);
    else sessionStorage.removeItem(QZONE_OPERATION_STORAGE_KEY);
  } catch {}
}

function qzoneOperationIsUnresolved(value) {
  const source = value && value.operation && typeof value.operation === "object" ? value.operation : (value || {});
  const diagnostic = value && value.diagnostic && typeof value.diagnostic === "object" ? value.diagnostic : source;
  const status = String(source.status || "").toLowerCase();
  const code = String(diagnostic.code || source.code || "").toLowerCase();
  return ["reserved", "dispatching", "unknown"].includes(status)
    || Boolean(diagnostic.outcome_unknown || source.outcome_unknown)
    || code.endsWith("_in_progress")
    || code === "qzone_reconcile_pending";
}

function settleQzoneOperationId(value) {
  if (!state.qzoneOperationId || !value || typeof value !== "object") return;
  const source = value.operation && typeof value.operation === "object" ? value.operation : value;
  const returnedId = String(source.operation_id || value.operation_id || value.diagnostic?.operation_id || "");
  if (returnedId && returnedId !== state.qzoneOperationId) return;
  const status = String(source.status || "").toLowerCase();
  if (["succeeded", "definite_failure"].includes(status) || !qzoneOperationIsUnresolved(value)) {
    state.qzoneRecoveredOperation = null;
    setQzoneOperationId("");
  } else {
    setQzoneOperationId(state.qzoneOperationId);
  }
}

function qzoneSnapshotFingerprint(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return "";
  const stable = {...snapshot, server_now:0, diagnostic:null};
  try { return JSON.stringify(stable); } catch { return String(Date.now()); }
}

function qzoneSnapshotNeedsFastRefresh(snapshot=state.qzone) {
  const reconciliation = snapshot && snapshot.reconciliation || {};
  const scan = snapshot && snapshot.scan || {};
  return Boolean(reconciliation.blocking || reconciliation.state === "in_progress" || reconciliation.state === "unknown" || scan.running || state.qzoneBusy || state.qzoneActionBusy || state.qzoneCandidateBusy || state.qzoneReconcileBusy || state.qzoneHistoryBusy);
}

function _scheduleQzoneSnapshot() {
  if (_qzoneSnapshotTimer) clearTimeout(_qzoneSnapshotTimer);
  _qzoneSnapshotTimer = 0;
  if (state.view !== "qzone" || document.hidden || _qzoneSnapshotPromise) return;
  _qzoneSnapshotTimer = setTimeout(() => refreshQzoneSnapshot(), qzoneSnapshotNeedsFastRefresh() ? 3000 : 15000);
}

function stopQzoneSnapshotScheduler() {
  if (_qzoneSnapshotTimer) clearTimeout(_qzoneSnapshotTimer);
  _qzoneSnapshotTimer = 0;
  if (_qzoneSnapshotController) _qzoneSnapshotController.abort();
  _qzoneSnapshotController = null;
}

function startQzoneSnapshotScheduler(immediate=true) {
  if (state.view !== "qzone" || document.hidden) return;
  if (immediate) refreshQzoneSnapshot({forceRender:true});
  else _scheduleQzoneSnapshot();
}

async function syncPersistedQzoneOperation(snapshot=state.qzone, signal=null) {
  const operationId = String(state.qzoneOperationId || "");
  if (!operationId || !snapshot) return;
  const reconciliation = snapshot.reconciliation && typeof snapshot.reconciliation === "object"
    ? snapshot.reconciliation : {state:"clear",blocking:false,operations:[]};
  const operations = Array.isArray(reconciliation.operations) ? reconciliation.operations : [];
  const listed = operations.find(item => String(item && item.operation_id || "") === operationId);
  if (listed) {
    state.qzoneRecoveredOperation = listed;
    settleQzoneOperationId({operation:listed});
    return;
  }
  try {
    const response = await api(`/qzone/operations/${encodeURIComponent(operationId)}`, {cache:"no-store",signal:signal || undefined});
    const operation = response && response.operation;
    if (!operation) return;
    state.qzoneRecoveredOperation = operation;
    settleQzoneOperationId(response);
    if (state.qzoneOperationId && qzoneOperationIsUnresolved(operation)) {
      reconciliation.operations = [operation, ...operations];
      reconciliation.blocking = true;
      reconciliation.state = String(operation.status) === "unknown" ? "unknown" : "in_progress";
      snapshot.reconciliation = reconciliation;
    }
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    // 404 也可能处于生成完成、reservation 写入之前；没有明确 terminal 前继续保留 ID。
  }
}

function updateQzoneLiveIsland(force=false) {
  const island = document.getElementById("qzone-live-island");
  if (!island || state.view !== "qzone") return;
  const fingerprint = qzoneSnapshotFingerprint(state.qzone);
  if (!force && fingerprint === _qzoneRenderedFingerprint) return;
  const main = island.closest("main");
  const scrollTop = main ? main.scrollTop : 0;
  const openDetails = new Set(Array.from(island.querySelectorAll("details[data-qzone-detail-key][open]")).map(item => item.dataset.qzoneDetailKey));
  const active = document.activeElement && island.contains(document.activeElement) ? document.activeElement : null;
  const focus = active && active.id ? {id:active.id,value:"value" in active?active.value:null,start:active.selectionStart,end:active.selectionEnd} : null;
  island.innerHTML = renderQzoneLive();
  _qzoneRenderedFingerprint = fingerprint;
  island.querySelectorAll("details[data-qzone-detail-key]").forEach(item => { item.open = openDetails.has(item.dataset.qzoneDetailKey); });
  if (focus) {
    const next = document.getElementById(focus.id);
    if (next) {
      if (focus.value !== null && "value" in next) next.value = focus.value;
      next.focus();
      try { if (focus.start !== null) next.setSelectionRange(focus.start, focus.end); } catch {}
    }
  }
  if (main) main.scrollTop = scrollTop;
}

async function refreshQzoneSnapshot({forceRender=false,explicit=false}={}) {
  if (state.view !== "qzone" || document.hidden) return null;
  if (_qzoneSnapshotPromise) {
    await _qzoneSnapshotPromise;
    if (forceRender && state.view === "qzone" && !document.hidden) updateQzoneLiveIsland(true);
    return state.qzone;
  }
  if (_qzoneSnapshotTimer) clearTimeout(_qzoneSnapshotTimer);
  _qzoneSnapshotTimer = 0;
  const controller = new AbortController();
  _qzoneSnapshotController = controller;
  const task = (async () => {
    try {
      const next = await api("/qzone/status", {cache:"no-store",signal:controller.signal});
      await syncPersistedQzoneOperation(next, controller.signal);
      const changed = qzoneSnapshotFingerprint(next) !== qzoneSnapshotFingerprint(state.qzone);
      state.qzone = next;
      if (changed || forceRender) updateQzoneLiveIsland(true);
      return next;
    } catch (error) {
      if (error && error.name === "AbortError") return null;
      if (explicit) {
        const diagnostic = operationDiagnosticFromError(error, "QZone 状态刷新未完成");
        rememberAdminOperation("qzone", diagnostic);
        alertFlash("err", diagnostic.title || "QZone 状态刷新未完成");
      }
      return null;
    }
  })();
  _qzoneSnapshotPromise = task;
  try { return await task; }
  finally {
    if (_qzoneSnapshotPromise === task) _qzoneSnapshotPromise = null;
    if (_qzoneSnapshotController === controller) _qzoneSnapshotController = null;
    _scheduleQzoneSnapshot();
  }
}

function stopQzoneViewLifecycle() {
  stopQzoneSnapshotScheduler();
  _stopQzoneLoginPolling();
}

function _stopQzoneLoginPolling() {
  if (_qzoneLoginPollTimer) clearTimeout(_qzoneLoginPollTimer);
  _qzoneLoginPollTimer = 0;
  if (_qzoneLoginPollController) _qzoneLoginPollController.abort();
  _qzoneLoginPollController = null;
}

function _scheduleQzoneLoginPolling() {
  _stopQzoneLoginPolling();
  if (!state.qzoneLogin || state.qzoneLogin.terminal || state.view !== 'qzone') return;
  _qzoneLoginPollTimer = setTimeout(pollQzoneLogin, 1800);
}

async function pollQzoneLogin() {
  _qzoneLoginPollTimer = 0;
  const login = state.qzoneLogin;
  if (!login || login.terminal || state.view !== 'qzone') return;
  const controller = new AbortController();
  _qzoneLoginPollController = controller;
  try {
    state.qzoneLogin = await api(`/qzone/auth/login/${encodeURIComponent(login.session_id)}/status`, {cache:"no-store",signal:controller.signal});
    if (state.qzoneLogin.status === 'success') {
      state.qzoneAuthResult = state.qzoneLogin;
      await refreshQzoneSnapshot({forceRender:true});
    }
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    state.qzoneAuthResult = operationDiagnosticFromError(e,"QZone 登录状态读取未完成");
    state.qzoneLogin = null;
  }
  if (_qzoneLoginPollController === controller) _qzoneLoginPollController = null;
  updateQzoneLiveIsland(true);
  _scheduleQzoneLoginPolling();
}

async function startQzoneLogin() {
  if (state.qzoneAuthBusy) return;
  state.qzoneAuthBusy = 'start'; state.qzoneAuthResult = null; _stopQzoneLoginPolling(); render();
  try {
    state.qzoneLogin = await api('/qzone/auth/login/start', {
      method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({bot_id:state.qzoneBotId})
    });
    rememberAdminOperation("qzone",state.qzoneLogin,"QZone 登录会话创建未完成");
    _scheduleQzoneLoginPolling();
  } catch (e) {
    state.qzoneAuthResult = operationDiagnosticFromError(e,"QZone 登录会话创建未完成");rememberAdminOperation("qzone",state.qzoneAuthResult);
  }
  state.qzoneAuthBusy = ''; render();
}

async function cancelQzoneLogin() {
  const login = state.qzoneLogin;
  if (!login || state.qzoneAuthBusy) return;
  state.qzoneAuthBusy = 'cancel'; _stopQzoneLoginPolling(); render();
  try {
    state.qzoneLogin = await api(`/qzone/auth/login/${encodeURIComponent(login.session_id)}/cancel`, {method:'POST'});
    state.qzoneAuthResult = state.qzoneLogin;rememberAdminOperation("qzone",state.qzoneLogin,"QZone 登录会话取消未完成");
  } catch (e) { state.qzoneAuthResult=operationDiagnosticFromError(e,"QZone 登录会话取消未完成");rememberAdminOperation("qzone",state.qzoneAuthResult); }
  state.qzoneAuthBusy = ''; render();
}

async function importQzoneCookie() {
  if (state.qzoneAuthBusy) return;
  const input = document.getElementById('qzone-cookie-import');
  const cookie = input ? input.value.trim() : '';
  if (!cookie) { state.qzoneAuthResult = {ok:false,message:'请粘贴完整 Cookie'}; render(); return; }
  if (input) input.value = '';
  state.qzoneAuthBusy = 'import'; state.qzoneAuthResult = null; render();
  try {
    const result = await api('/qzone/auth/cookie', {
      method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({bot_id:state.qzoneBotId,cookie})
    });
    state.qzoneAuthResult = result;
    rememberAdminOperation("qzone",result,"QZone Cookie 导入未完成");
    if (result.ok) { state.qzoneLogin = null; _stopQzoneLoginPolling(); await refreshQzoneSnapshot({forceRender:true}); }
  } catch (e) { state.qzoneAuthResult=operationDiagnosticFromError(e,"QZone Cookie 导入未完成");rememberAdminOperation("qzone",state.qzoneAuthResult); }
  state.qzoneAuthBusy = ''; render();
}

function renderQzoneAuthRecovery(q, auth) {
  const bots = Array.isArray(q.bots) ? q.bots : [];
  const botIds = bots.map(item => String(item.bot_id || '')).filter(Boolean);
  if (!botIds.includes(state.qzoneBotId)) state.qzoneBotId = botIds[0] || '';
  const login = state.qzoneLogin;
  const active = login && !login.terminal;
  const selectorDisabled = active || state.qzoneCandidateBusy || state.qzoneHistoryBusy || state.qzoneReconcileBusy || state.qzoneActionBusy;
  const phase = !login ? 0 : (login.status === 'waiting_scan' ? 1 : (login.status === 'waiting_confirm' ? 2 : (['verifying','success'].includes(login.status) ? 3 : 1)));
  const botOptions = bots.map(item => `<option value="${escapeAttr(item.bot_id)}" ${String(item.bot_id)===state.qzoneBotId?'selected':''}>QQ ${escapeHtml(item.bot_id)}</option>`).join('');
  const qrUrl = login && login.qr_ready ? `${API}/qzone/auth/login/${encodeURIComponent(login.session_id)}/qrcode?v=${encodeURIComponent(login.updated_at||0)}` : '';
  const terminalHint = login && login.terminal && login.status !== 'success'
    ? `<div class="alert err">${escapeHtml(login.message || '本次登录未完成')}</div>` : '';
  const insecure = location.protocol !== 'https:' && !['localhost','127.0.0.1','::1'].includes(location.hostname);
  return `<div class="card qzone-auth-card">
    <div class="qzone-auth-head">
      <div><span class="eyebrow">ACCOUNT RECOVERY</span><h2>QZone 认证恢复</h2><p>使用手机 QQ 扫码并一键确认，凭证只在服务端验证和保存。</p></div>
       <label class="qzone-bot-select"><small>目标 Bot</small><select id="qzone-bot-select" onchange="selectQzoneBot(this.value)" ${selectorDisabled?'disabled':''}>${botOptions||'<option value="">无已连接 Bot</option>'}</select></label>
    </div>
    <div class="qzone-auth-track" aria-label="认证恢复进度">
      <div class="${phase>=1?'active':''}"><span>01</span><strong>扫码</strong><small>手机 QQ 相机</small></div>
      <div class="${phase>=2?'active':''}"><span>02</span><strong>确认</strong><small>核对登录账号</small></div>
      <div class="${phase>=3?'active':''}"><span>03</span><strong>验证</strong><small>安装 p_skey</small></div>
    </div>
    ${active ? `<div class="qzone-login-stage">
      <div class="qzone-qr-frame">${qrUrl?`<img src="${escapeAttr(qrUrl)}" alt="QQ 扫码登录二维码">`:'<span class="spinner"></span>'}</div>
      <div class="qzone-login-copy"><span class="ops-status info"><span></span>${escapeHtml(login.status||'preparing')}</span><h3>${escapeHtml(login.message||'等待腾讯登录')}</h3><p>二维码剩余 ${Number(login.expires_in_seconds||0)} 秒。请用手机 QQ 的扫一扫，不要使用图片识别或第三方扫码工具。</p><div class="row"><button class="btn small" onclick="cancelQzoneLogin()" ${state.qzoneAuthBusy?'disabled':''}>取消登录</button></div></div>
    </div>` : `<div class="qzone-auth-idle"><p>${auth.status==='healthy'?'当前凭证可用。需要切换或重新授权时，也可以主动生成新二维码。':'LLOneBot 无法提供有效 p_skey 时，从这里发起独立服务端登录。'}</p><button class="btn primary" onclick="startQzoneLogin()" ${state.qzoneAuthBusy||!state.qzoneBotId?'disabled':''}>${state.qzoneAuthBusy==='start'?'<span class="spinner"></span> 生成中…':'QQ 扫码恢复登录'}</button></div>`}
    ${terminalHint}
    <details class="qzone-cookie-fallback" data-qzone-detail-key="cookie-import"><summary>高级兜底：手动导入 Cookie</summary><p>仅在扫码受腾讯风控影响时使用。Cookie 不会回显或进入审计详情。${insecure?' 当前页面不是 HTTPS，请勿在公网传输凭证。':''}</p><textarea id="qzone-cookie-import" autocomplete="off" spellcheck="false" placeholder="uin=o...; p_uin=o...; skey=...; p_skey=...;"></textarea><div class="row"><button class="btn small" onclick="importQzoneCookie()" ${state.qzoneAuthBusy||!state.qzoneBotId?'disabled':''}>${state.qzoneAuthBusy==='import'?'<span class="spinner"></span> 验证中…':'验证并安装'}</button></div></details>
  </div>`;
}

function selectQzoneBot(botId) {
  state.qzoneBotId = String(botId || "");
  state.qzoneCandidates = null;
  updateQzoneLiveIsland(true);
}

function qzonePendingOperations(q) {
  const listed = q && q.reconciliation && Array.isArray(q.reconciliation.operations) ? q.reconciliation.operations : [];
  const items = [...listed];
  const storedId = String(state.qzoneOperationId || "");
  if (storedId && !items.some(item => String(item && item.operation_id || "") === storedId)) {
    items.unshift(state.qzoneRecoveredOperation && String(state.qzoneRecoveredOperation.operation_id || "") === storedId
      ? state.qzoneRecoveredOperation
      : {operation_id:storedId,bot_id:state.qzoneBotId,status:state.qzoneBusy?"reserved":"unknown",created_at:0,dispatch_started_at:0,result_code:"browser_recovery",content:"",action_required:state.qzoneBusy?"wait":"verify_remote"});
  }
  const seen = new Set();
  return items.filter(item => {
    const id = String(item && item.operation_id || "");
    if (!id || seen.has(id)) return false;
    seen.add(id);
    return ["reserved", "dispatching", "unknown"].includes(String(item.status || ""));
  });
}

function renderQzoneReconciliation(q) {
  const operations = qzonePendingOperations(q);
  const stateLabel = operations.some(item => item.status === "unknown") ? "需要人工核对" : operations.length ? "发布处理中" : "当前清晰";
  const stateClass = operations.some(item => item.status === "unknown") ? "warn" : operations.length ? "info" : "ok";
  const rows = operations.map(item => {
    const id = String(item.operation_id || "");
    const botId = String(item.bot_id || state.qzoneBotId || "");
    const status = String(item.status || "unknown");
    const statusLabel = ({reserved:"已保留额度",dispatching:"正在发布",unknown:"结果未知"})[status] || "待核对";
    const created = _fmtTs(item.created_at);
    const dispatched = Number(item.dispatch_started_at || 0) > 0 ? _fmtTs(item.dispatch_started_at) : "尚未发起";
    const content = String(item.content || "").trim();
    const busy = String(state.qzoneReconcileBusy || "").startsWith(`${id}:`);
    const actions = status === "unknown" ? `<div class="qzone-operation-actions">
      <button class="btn small" aria-label="检查 operation ${escapeAttr(id)} 的远端结果" data-qzone-operation-action="reconcile" data-operation-id="${escapeAttr(id)}" data-bot-id="${escapeAttr(botId)}" ${busy?'disabled':''}>${state.qzoneReconcileBusy===`${id}:reconcile`?'<span class="spinner"></span> 检查中…':'检查远端'}</button>
      <button class="btn small danger" aria-label="确认 operation ${escapeAttr(id)} 未发布" data-qzone-operation-action="absent" data-operation-id="${escapeAttr(id)}" data-bot-id="${escapeAttr(botId)}" ${busy?'disabled':''}>${state.qzoneReconcileBusy===`${id}:absent`?'<span class="spinner"></span> 确认中…':'确认未发布'}</button>
    </div>` : '<span class="muted qzone-operation-wait">系统将继续刷新状态，请勿重复发布。</span>';
    return `<article class="qzone-operation-item ${status === 'unknown'?'unknown':'active'}">
      <header><span class="ops-status ${status === 'unknown'?'warn':'info'}"><span></span>${escapeHtml(statusLabel)}</span><code title="${escapeAttr(id)}">${escapeHtml(id)}</code></header>
      <div class="qzone-operation-meta"><span>Bot <strong class="u-tabular" title="${escapeAttr(botId || '—')}">${escapeHtml(botId || '—')}</strong></span><span>创建 <strong class="u-tabular" title="${escapeAttr(created)}">${escapeHtml(created)}</strong></span><span>发送 <strong class="u-tabular" title="${escapeAttr(dispatched)}">${escapeHtml(dispatched)}</strong></span><span>结果 <strong title="${escapeAttr(item.result_code || '—')}">${escapeHtml(item.result_code || '—')}</strong></span></div>
      <p class="qzone-operation-content">${content?escapeHtml(content.slice(0,180)):'<span class="muted">正文仍在生成或快照中未保留摘要</span>'}</p>
      ${actions}
    </article>`;
  }).join("");
  return `<div class="card qzone-reconciliation-card">
    <div class="between qzone-section-head"><div><span class="eyebrow">RECONCILIATION QUEUE</span><h2>待对账发布</h2></div><span class="ops-status ${stateClass}"><span></span>${escapeHtml(stateLabel)}</span></div>
    <p class="muted">unknown 操作必须先检查本人空间；只有确认远端不存在后，才可解除占用。</p>
    <div class="qzone-operation-list">${rows||'<div class="qzone-empty-state">没有待对账 operation</div>'}</div>
  </div>`;
}

function renderQzoneHistoryReconciliation() {
  const candidates = Array.isArray(state.qzoneCandidates) ? state.qzoneCandidates : null;
  const rows = candidates ? candidates.map(item => {
    const feedId = String(item.feed_id || "");
    const busy = state.qzoneHistoryBusy === feedId;
    return `<article class="qzone-candidate-item">
      <div><time>${escapeHtml(_fmtTs(item.created_at))}</time><code title="${escapeAttr(feedId)}">${escapeHtml(feedId)}</code></div>
      <p>${escapeHtml(String(item.content || "").slice(0,200))}</p>
      <button class="btn small" aria-label="确认并补记 feed ${escapeAttr(feedId)}" data-qzone-history-feed="${escapeAttr(feedId)}" ${state.qzoneHistoryBusy?'disabled':''}>${busy?'<span class="spinner"></span> 补记中…':'确认并补记'}</button>
    </article>`;
  }).join("") : "";
  return `<div class="card qzone-history-card">
    <div class="between qzone-section-head"><div><span class="eyebrow">LEDGER RECOVERY</span><h2>检查漏记动态</h2></div><button class="btn small" onclick="scanQzoneReconcileCandidates()" ${state.qzoneCandidateBusy||!state.qzoneBotId?'disabled':''}>${state.qzoneCandidateBusy?'<span class="spinner"></span> 检查中…':'检查漏记动态'}</button></div>
    <p class="muted">读取所选 Bot 本人最近动态。选择补记前会再次从远端验证，并更新本月额度与最近发布记录。</p>
    <div class="qzone-candidate-list">${candidates?(rows||'<div class="qzone-empty-state">最近动态中没有可补记候选</div>'):'<div class="qzone-empty-state">点击检查后显示最近候选</div>'}</div>
  </div>`;
}

function renderQzone() {
  if (!state.qzone) return `<div class="card muted">加载中…</div>`;
  _qzoneRenderedFingerprint = qzoneSnapshotFingerprint(state.qzone);
  return `${renderAdminOperations("qzone","QZone 操作诊断")}<div id="qzone-live-island">${renderQzoneLive()}</div>`;
}

function renderQzoneLive() {
  const q = state.qzone;
  if (!q) return `<div class="card muted">加载中…</div>`;
  const botIds = (Array.isArray(q.bots) ? q.bots : []).map(item => String(item.bot_id || "")).filter(Boolean);
  if (!botIds.includes(state.qzoneBotId)) state.qzoneBotId = botIds[0] || "";
  const quota = q.quota || {};
  const confirmed = Number(quota.confirmed != null ? quota.confirmed : quota.used || 0);
  const held = Number(quota.held || 0);
  const available = Number(quota.available != null ? quota.available : quota.remaining != null ? quota.remaining : 0);
  const limit = Number(quota.limit != null ? quota.limit : confirmed + held + available);
  const pct = limit > 0 ? Math.min(100, Math.round((confirmed + held) / limit * 100)) : 0;
  const barColor = pct >= 90 ? "var(--danger)" : (pct >= 70 ? "var(--warn)" : "var(--ok)");
  const enabledPill = (on, label) => `<span class="device-status ${on?'approved':'pending'}">${label}：${on?'开':'关'}</span>`;
  const authByBot = q.auth_by_bot && typeof q.auth_by_bot === 'object' ? q.auth_by_bot : {};
  const auth = authByBot[state.qzoneBotId] || q.auth || {}, scan = q.scan || {}, social = q.social || {}, inbound = q.inbound || {};
  const capabilityByBot = q.capabilities_by_bot && typeof q.capabilities_by_bot === 'object' ? q.capabilities_by_bot : {};
  const capability = capabilityByBot[state.qzoneBotId] || q.capabilities || {};
  const cookieExportState = String((capability["qzone.cookie_export"]||{}).state||"unknown");
  const webReadState = String((capability["qzone.web_read"]||{}).state||"unknown");
  const webWriteState = String((capability["qzone.web_write"]||{}).state||"unknown");
  const readOnly = Boolean(capability.read_only || q.read_only);
  const manualWriteAllowed = webWriteState === "available" || webWriteState === "unknown";
  const statusText = auth.status === 'healthy' ? '认证正常' : auth.status === 'login_required' ? '需要登录' : auth.status === 'risk_blocked' ? '安全验证' : auth.status === 'refresh_failed' ? '刷新失败' : '尚未验证';
  const statusClass = auth.status === 'healthy' ? 'ok' : auth.status === 'unknown' ? 'info' : 'warn';
  const scanText = scan.running ? `${scan.owner==='social'?'好友动态扫描':'留言轮询'}运行 ${Number(scan.running_seconds||0)} 秒` : '当前空闲';
  const resultDigest = item => {
    const result = item.last_result || {};
    if (!item.last_scan_at) return '尚未执行';
    if (result.status === 'timed_out') return '最近执行超时';
    if (result.skipped) return `最近跳过：${String(result.reason||'busy')}`;
    if (item.last_error) return `最近失败：${String(item.last_error)}`;
    return `最近完成：动态 ${Number(result.feeds_seen||0)} · 回复 ${Number(result.replied||0)} · 失败 ${Number(result.failed||0)}`;
  };
  const recent = (q.recent_contents || []).slice().reverse();
  const recentRows = recent.length
    ? recent.map(c => `<li class="qzone-recent-item">${escapeHtml(c)}</li>`).join("")
    : '<li class="qzone-recent-item muted">暂无记录</li>';
  return `<section class="qzone-ops-grid">
    <div class="card qzone-runtime-card">
      <div class="between"><div><span class="eyebrow">RUNTIME HEALTH</span><h2>空间运行状态</h2></div><span class="ops-status ${statusClass}"><span></span>${statusText}</span></div>
      <div class="qzone-runtime-grid">
        <div><small>Cookie</small><strong>${q.cookie_configured?'已配置':'未配置'}</strong><span>${auth.last_success_at?`最近刷新 ${escapeHtml(_fmtTs(auth.last_success_at))}`:'等待刷新'}</span></div>
        <div><small>扫描协调器</small><strong>${scanText}</strong><span>忙碌跳过 ${Number(scan.busy_skip_count||0)} 次</span></div>
        <div><small>好友互动</small><strong>${social.job&&social.job.registered?'已注册':'未注册'}</strong><span>${escapeHtml(resultDigest(social))}</span></div>
        <div><small>留言轮询</small><strong>${inbound.job&&inbound.job.registered?'已注册':'未注册'}</strong><span>${escapeHtml(resultDigest(inbound))}</span></div>
        <div><small>qzone.cookie_export</small><strong>${escapeHtml(cookieExportState)}</strong><span>OneBot Cookie 导出</span></div>
        <div><small>qzone.web_read</small><strong>${escapeHtml(webReadState)}</strong><span>${webReadState==='available'?'可读取空间':'读取能力未确认'}</span></div>
        <div><small>qzone.web_write</small><strong>${escapeHtml(webWriteState)}</strong><span>${readOnly?'read_only：写动作已停用':'写能力快照'}</span></div>
      </div>
      ${readOnly?'<div class="alert info">QZone 当前为 read_only：读取仍可用，发布、评论、点赞与转发已停止；普通 QQ 功能不受影响。</div>':''}
      ${auth.cooldown_remaining_seconds>0?`<div class="alert err">${auth.status==='risk_blocked'?'检测到腾讯安全验证':'检测到登录失效'}，已暂停该 Bot 的空间请求，冷却剩余 ${_fmtDuration(auth.cooldown_remaining_seconds)}。</div>`:''}
      ${auth.last_error?`<p class="muted qzone-error-line">${escapeHtml(auth.last_error)}</p>`:''}
      <div class="row">
        <button class="btn small" onclick="runQzoneAction('refresh')" ${state.qzoneActionBusy?'disabled':''}>${state.qzoneActionBusy==='refresh'?'<span class="spinner"></span> 刷新中…':'从 LLOneBot 刷新'}</button>
        <button class="btn small" onclick="runQzoneAction('social')" ${state.qzoneActionBusy||!q.social_enabled?'disabled':''}>运行好友扫描</button>
        <button class="btn small" onclick="runQzoneAction('inbound')" ${state.qzoneActionBusy||!q.inbound_enabled?'disabled':''}>运行留言轮询</button>
      </div>
    </div>
  </section>
  ${renderQzoneReconciliation(q)}
  ${renderQzoneHistoryReconciliation()}
  ${renderQzoneAuthRecovery(q, auth)}
  <div class="card">
    <div class="between" style="margin-bottom:4px">
      <h2 style="margin:0">本月发空间额度</h2>
      <div class="row">${enabledPill(q.enabled,'空间总开关')}${enabledPill(q.proactive_enabled,'主动发说说')}</div>
    </div>
    <p class="muted" style="margin:2px 0 14px">agent 会参考这份额度自己把控发不发、发的节奏；下面是当前快照。</p>
    <div class="qzone-quota-line">
      <span style="font-size:30px;font-weight:700">${confirmed}</span>
      <span class="muted">已确认 / ${limit} 条（本月 ${escapeHtml(quota.month||'')}）</span>
      <span class="muted">在途占用 <strong>${held}</strong> 条</span>
      <span class="muted qzone-quota-remain">可用 <strong style="color:${barColor}">${available}</strong> 条 · 还剩 ${Number(quota.days_left||0)} 天</span>
    </div>
    <div style="height:10px;border-radius:99px;background:var(--input-bg);border:1px solid var(--line);overflow:hidden">
      <div style="height:100%;width:${pct}%;background:${barColor};transition:width .3s"></div>
    </div>
    <div class="health-summary" style="margin-top:16px">
      <div class="health-pill"><div><div class="muted" style="font-size:12px">检查间隔</div><div>每 ${Number(q.check_interval_minutes||0)} 分钟</div></div></div>
      <div class="health-pill"><div><div class="muted" style="font-size:12px">最小间隔</div><div>${Number((quota.min_interval_hours)||0)} 小时</div></div></div>
      <div class="health-pill"><div><div class="muted" style="font-size:12px">静默时段</div><div>${Number(q.quiet_hour_start||0)}:00 - ${Number(q.quiet_hour_end||0)}:00</div></div></div>
      <div class="health-pill"><div><div class="muted" style="font-size:12px">下次最早可发</div><div>${q.next_eligible_in_seconds>0?_fmtDuration(q.next_eligible_in_seconds):'现在可发'}</div></div></div>
    </div>
    <div class="field" style="margin-top:8px">
      <div class="muted" style="font-size:12px">上次发布</div>
      <div>${escapeHtml(_fmtTs(q.last_post_at))}${q.last_content?'：'+escapeHtml(q.last_content):''}</div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="btn primary" onclick="triggerQzonePost()" ${state.qzoneBusy||!manualWriteAllowed?'disabled':''}>${state.qzoneBusy?'<span class="spinner"></span> 发布中…':webWriteState==='unknown'?'立即发一条并验证写能力':'立即发一条'}</button>
      <button class="btn small" onclick="refreshQzoneSnapshot({forceRender:true,explicit:true})">刷新</button>
      <span class="muted" style="font-size:12px">「立即发一条」会强制生成并发布（绕过额度/间隔判断），但仍计入本月额度。</span>
    </div>
  </div>
  <div class="card">
    <h2>最近发过的说说（去重记忆）</h2>
    <ul class="qzone-recent-list">${recentRows}</ul>
  </div>`;
}

async function triggerQzonePost() {
  if (state.qzoneBusy) return;
  if (!confirm("确定现在强制发一条空间说说？会真实发布到 QQ 空间，并计入本月额度。")) return;
  if (!state.qzoneOperationId) setQzoneOperationId(globalThis.crypto&&globalThis.crypto.randomUUID ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
  else setQzoneOperationId(state.qzoneOperationId);
  state.qzoneBusy = true; state.qzonePostResult = null; render();
  _scheduleQzoneSnapshot();
  try {
    const r = await api("/qzone/post-now", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({operation_id:state.qzoneOperationId,bot_id:state.qzoneBotId}) });
    state.qzonePostResult = r;
    rememberAdminOperation("qzone",r,"QZone 发布未完成");
    settleQzoneOperationId(r);
    if (r && r.quota && state.qzone) state.qzone.quota = r.quota;
    await refreshQzoneSnapshot({forceRender:true});
  } catch (e) {
    const serverDiagnostic=e&&e.diagnostic&&typeof e.diagnostic==="object";
    state.qzonePostResult=operationDiagnosticFromError(e,"QZone 发布未完成");
    if(!serverDiagnostic)state.qzonePostResult={...state.qzonePostResult,code:"qzone_publish_request_outcome_unknown",phase:"request",title:"QZone 发布请求结果未知",message:"浏览器没有收到服务器的明确结果，发布可能已经发生。",suggestion:"保留当前 Operation ID，先检查 Bot 的 QQ 空间；确认状态前不要重复提交。",retryable:false,outcome_unknown:true,operation_id:state.qzoneOperationId};
    rememberAdminOperation("qzone",state.qzonePostResult);
    settleQzoneOperationId(state.qzonePostResult);
  }
  state.qzoneBusy = false; render();
}

async function runQzoneAction(kind) {
  if (state.qzoneActionBusy) return;
  state.qzoneActionBusy = kind; state.qzoneActionResult = null; render();
  _scheduleQzoneSnapshot();
  try {
    const path = kind === 'refresh' ? '/qzone/refresh-cookie' : '/qzone/scan-now';
    const options = kind === 'refresh' ? {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({bot_id:state.qzoneBotId})} : {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({kind})};
    const result = await api(path, options);
    state.qzoneActionResult = result;
    rememberAdminOperation("qzone",result,"QZone 管理操作未完成");
    await refreshQzoneSnapshot({forceRender:true});
  } catch (e) { state.qzoneActionResult=operationDiagnosticFromError(e,"QZone 管理操作未完成");rememberAdminOperation("qzone",state.qzoneActionResult); }
  state.qzoneActionBusy = ""; render();
}

async function reconcileQzoneOperation(operationId, botId, action) {
  const id = String(operationId || "");
  const targetBot = String(botId || state.qzoneBotId || "");
  if (!id || !targetBot || state.qzoneReconcileBusy) return;
  if (action === "absent" && !confirm("确认本人空间中确实没有这条动态？确认后会结束该 operation 并释放占用，此操作不可撤销。")) return;
  state.qzoneReconcileBusy = `${id}:${action}`;
  render();
  _scheduleQzoneSnapshot();
  try {
    const suffix = action === "absent" ? "resolve-absent" : "reconcile";
    const result = await api(`/qzone/operations/${encodeURIComponent(id)}/${suffix}`, {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:targetBot})});
    rememberAdminOperation("qzone", result, action === "absent" ? "确认未发布操作未完成" : "远端对账未完成");
    settleQzoneOperationId(result);
  } catch (error) {
    rememberAdminOperation("qzone", operationDiagnosticFromError(error, action === "absent" ? "确认未发布操作未完成" : "远端对账未完成"));
  }
  state.qzoneReconcileBusy = "";
  await refreshQzoneSnapshot({forceRender:true});
  render();
}

async function scanQzoneReconcileCandidates() {
  if (!state.qzoneBotId || state.qzoneCandidateBusy) return;
  state.qzoneCandidateBusy = true;
  state.qzoneCandidates = null;
  render();
  _scheduleQzoneSnapshot();
  try {
    const result = await api(`/qzone/reconcile-candidates?bot_id=${encodeURIComponent(state.qzoneBotId)}`, {cache:"no-store"});
    state.qzoneCandidates = Array.isArray(result.candidates) ? result.candidates : [];
  } catch (error) {
    if (error && error.name === "AbortError") {
      state.qzoneCandidateBusy = false;
      return;
    }
    rememberAdminOperation("qzone", operationDiagnosticFromError(error, "漏记动态检查未完成"));
    state.qzoneCandidates = [];
  }
  state.qzoneCandidateBusy = false;
  render();
  _scheduleQzoneSnapshot();
}

async function reconcileQzoneHistory(feedId) {
  const id = String(feedId || "");
  if (!id || !state.qzoneBotId || state.qzoneHistoryBusy) return;
  if (!confirm("确认将这条本人空间动态补入本地账本？系统会再次核对远端动态，并调整本月额度。")) return;
  state.qzoneHistoryBusy = id;
  render();
  _scheduleQzoneSnapshot();
  try {
    const result = await api("/qzone/reconcile-history", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:state.qzoneBotId,feed_id:id})});
    rememberAdminOperation("qzone", result, "漏记动态补记未完成");
    if (result.ok) state.qzoneCandidates = (state.qzoneCandidates || []).filter(item => String(item.feed_id || "") !== id);
  } catch (error) {
    rememberAdminOperation("qzone", operationDiagnosticFromError(error, "漏记动态补记未完成"));
  }
  state.qzoneHistoryBusy = "";
  await refreshQzoneSnapshot({forceRender:true});
  render();
}

document.addEventListener("click", event => {
  const operationButton = event.target instanceof Element ? event.target.closest("[data-qzone-operation-action]") : null;
  if (operationButton) {
    reconcileQzoneOperation(operationButton.dataset.operationId, operationButton.dataset.botId, operationButton.dataset.qzoneOperationAction);
    return;
  }
  const historyButton = event.target instanceof Element ? event.target.closest("[data-qzone-history-feed]") : null;
  if (historyButton) reconcileQzoneHistory(historyButton.dataset.qzoneHistoryFeed);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopQzoneViewLifecycle();
  else if (state.view === "qzone") {
    startQzoneSnapshotScheduler(true);
    _scheduleQzoneLoginPolling();
  }
});
