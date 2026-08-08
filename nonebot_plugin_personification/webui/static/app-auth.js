const SMALL_OPERATION_STORAGE_KEY = "personification_small_operation_diagnostics_v1";

function smallOperationEntries() {
  if (Array.isArray(state.smallOperationDiagnostics)) return state.smallOperationDiagnostics;
  try {
    const saved = JSON.parse(sessionStorage.getItem(SMALL_OPERATION_STORAGE_KEY) || "[]");
    state.smallOperationDiagnostics = Array.isArray(saved) ? saved.slice(0, 12) : [];
  } catch { state.smallOperationDiagnostics = []; }
  return state.smallOperationDiagnostics;
}

function rememberSmallOperation(scope, value, fallbackTitle="操作未完成") {
  const diagnostic = value && value.diagnostic && typeof value.diagnostic === "object"
    ? value.diagnostic
    : (value instanceof Error ? operationDiagnosticFromError(value, fallbackTitle) : value);
  if (!diagnostic || typeof diagnostic !== "object" || !diagnostic.code) return null;
  state.smallOperationDiagnostics = [{scope, diagnostic}, ...smallOperationEntries()].slice(0, 12);
  try { sessionStorage.setItem(SMALL_OPERATION_STORAGE_KEY, JSON.stringify(state.smallOperationDiagnostics)); } catch {}
  return diagnostic;
}

function clearSmallOperations(scope) {
  state.smallOperationDiagnostics = smallOperationEntries().filter(item => item.scope !== scope);
  try { sessionStorage.setItem(SMALL_OPERATION_STORAGE_KEY, JSON.stringify(state.smallOperationDiagnostics)); } catch {}
  render();
}

function renderSmallOperations(scope, title) {
  const items = renderOperationHistory(
    smallOperationEntries().filter(item => item.scope === scope).map(item => item.diagnostic),
    {group:`view-${state.view}`},
  );
  return items ? `<div class="card"><div class="between"><h2>${escapeHtml(title)}</h2><button class="btn small" onclick="clearSmallOperations('${escapeAttr(scope)}')">清空</button></div>${items}</div>` : "";
}

function renderDevices() {
  const rows = state.devices.map(d => {
    const isCurrent = d.id === state.currentDeviceId;
    return `<tr>
      <td class="col-model"><span class="u-clamp-2" title="${escapeAttr(d.label)} · device ${escapeAttr(d.id)}">${escapeHtml(d.label)}</span> ${isCurrent ? '<span class="tag tag--status">当前</span>' : ''}</td>
      <td class="col-description muted u-wrap" title="${escapeAttr(d.ua)}">${escapeHtml(d.ua.slice(0, 60))}</td>
      <td class="col-time u-atomic u-tabular">${new Date(d.last_seen * 1000).toLocaleString()}</td>
      <td class="col-actions">
        ${isCurrent ? '' : `<button class="btn small danger" aria-label="撤销设备 ${escapeAttr(d.label)}" onclick="revokeDevice('${escapeAttr(d.id)}')">撤销</button>`}
      </td>
    </tr>`;
  }).join("");
  return `${renderSmallOperations("device", "设备操作诊断")}<div class="card">
    <h2>已登录设备</h2>
    <p class="muted">有效 session cookie 会保持登录；退出、撤销或过期后需重新接收管理员验证码。</p>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="已登录设备列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-model">设备</th><th scope="col" class="col-description">UA</th><th scope="col" class="col-time">最后活跃</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows}</tbody></table></div>
  </div>`;
}

async function approveDevice(id) {
  try {
    const result = await api("/auth/devices/" + encodeURIComponent(id) + "/approve", { method:"POST" });
    const diagnostic = rememberSmallOperation("device", result, "设备审批未完成");
    alertFlash(diagnostic?.ok === false || diagnostic?.partial ? "info" : "ok", diagnostic?.title || "已批准");
    await loadView(); render();
  } catch (e) { const diagnostic = rememberSmallOperation("device", e, "设备审批未完成"); alertFlash("err", diagnostic?.title || "设备审批未完成"); render(); }
}

async function revokeDevice(id) {
  if (!confirm("撤销该设备？该设备下次访问将被踢出。")) return;
  try {
    const result = await api("/auth/devices/" + encodeURIComponent(id), { method:"DELETE" });
    const diagnostic = rememberSmallOperation("device", result, "设备撤销未完成");
    alertFlash(diagnostic?.ok === false || diagnostic?.partial ? "info" : "ok", diagnostic?.title || "已撤销");
    await loadView(); render();
  }
  catch (e) { const diagnostic = rememberSmallOperation("device", e, "设备撤销未完成"); alertFlash("err", diagnostic?.title || "设备撤销未完成"); render(); }
}

async function doLogout() {
  try {
    await api("/auth/logout", { method:"POST" });
    leaveViewLifecycle(state.view,"");
    clearInMemorySensitiveState();
    state.logged = false;
    await refreshEligibleAdmins();
    render();
  } catch (e) {
    if (!state.logged) {
      clearInMemorySensitiveState();
      await refreshEligibleAdmins();
      render();
      return;
    }
    alertFlash("err", "退出失败：" + e.message);
  }
}

let _layoutDelegationAttached = false;

function attachLayout() {
  if (!_layoutDelegationAttached) {
    _layoutDelegationAttached = true;
    document.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const nav = target?.closest("#console-sidebar nav a[href^='#']");
      if (nav) {
        event.preventDefault();
        navigateToView(nav.getAttribute("href").slice(1));
        return;
      }
      const leave = target?.closest(".qq-leave-group");
      if (leave && typeof qqLeaveGroup === "function") {
        qqLeaveGroup(leave.dataset.groupId, leave.dataset.groupName);
      }
    });
  }
  const main=document.querySelector(".layout > main");
  if(main&&!main.dataset.scrollListenerBound){
    main.dataset.scrollListenerBound="true";
    main.addEventListener("scroll",queueScrollStateCapture,{passive:true});
  }
  const nav=document.querySelector("#console-sidebar nav");
  if(nav&&!nav.dataset.scrollListenerBound){
    nav.dataset.scrollListenerBound="true";
    nav.addEventListener("scroll",queueScrollStateCapture,{passive:true});
  }
}

function renderLogin() {
  const themeIcon = state.theme === "dark" ? renderIcon("sun") : renderIcon("moon");
  const themeLabel = state.theme === "dark" ? "切换到浅色主题" : "切换到深色主题";
  const eligible = state.eligibleAdmins || [];
  let picker, hint;
  if (!eligible.length) {
    picker = `<select id="login-qq" disabled style="width:100%;margin-top:6px"><option value="">未配置管理员</option></select>`;
    hint = `<p class="muted" style="margin-top:14px;font-size:12.5px">请先在 NoneBot 配置中设置 SUPERUSERS，或通过已有管理入口添加 plugin admin。</p>`;
  } else {
    picker = `<select id="login-qq" style="width:100%;margin-top:6px">
        ${eligible.map(e => `<option value="${escapeAttr(e.qq)}">${escapeHtml(e.qq)}（${escapeHtml(e.source)}）</option>`).join("")}
      </select>`;
    hint = `<p class="muted" style="margin-top:14px;font-size:12.5px">选择一个管理员 QQ，Bot 会向其私聊推送 6 位数验证码，5 分钟内有效。</p>`;
  }
  return `<div class="login-wrap"><div class="card"><div class="between">
      <h2 style="margin:0">拟人插件 WebUI 登录</h2>
       <button class="btn small icon-btn" onclick="toggleTheme()" title="${themeLabel}" aria-label="${themeLabel}">${themeIcon}</button>
    </div>
    <div id="login-step1">
      <label>管理员 QQ</label>
      ${picker}
      <div style="margin-top:14px"><button class="btn primary" onclick="sendCode()" ${eligible.length ? "" : "disabled"}>发送验证码</button></div>
      ${hint}
    </div>
    <div id="login-step2" style="display:none">
      <div class="alert info" style="font-size:12.5px">验证码已发送到所选管理员 QQ，请在 5 分钟内输入。</div>
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

function renderDevicePending() {
  return `<div class="login-wrap"><div class="card">
    <h2>设备等待审批</h2>
    <p class="muted">该设备已登记，但需由一台<strong>已批准的设备</strong>在「设备」页确认后才能使用。</p>
    <p class="muted">已通知管理员。批准后点击下方按钮刷新。</p>
    <div style="margin-top:14px;display:flex;gap:8px">
      <button class="btn primary" onclick="recheckDevice()">我已被批准，刷新</button>
      <button class="btn" onclick="logoutPending()">退出</button>
    </div>
  </div></div>`;
}

async function recheckDevice() {
  try { const me = await api("/auth/me"); state.logged = true; state.devicePending = false; state.qq = me.qq; await loadView(); render(); enterViewLifecycle(state.view); }
  catch (e) {
    if (/DEVICE_PENDING/.test(String(e && e.message || ""))) { alertFlash("info", "仍在等待管理员批准"); }
    else { state.devicePending = false; render(); }
  }
}

async function logoutPending() {
  try { await api("/auth/logout", { method:"POST" }); } catch {}
  clearInMemorySensitiveState();
  state.devicePending = false; state.logged = false; render();
}

async function sendCode() {
  const el = document.getElementById("login-qq");
  const qq = (el && el.value || "").trim();
  const msg = document.getElementById("login-msg");
  if (!qq) { msg.textContent = "请选择管理员 QQ。"; return; }
  msg.textContent = "正在发送…";
  try {
    await api("/auth/login", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ qq }) });
    state.pendingQq = qq;
    document.getElementById("login-step1").style.display = "none";
    document.getElementById("login-step2").style.display = "block";
    msg.textContent = "验证码已发送，请查收管理员 QQ 私聊。";
  } catch (e) { msg.textContent = "发送失败：" + e.message; }
}

async function doVerify() {
  const code = document.getElementById("login-code").value.trim();
  const label = document.getElementById("login-label").value.trim();
  const msg = document.getElementById("login-msg");
  msg.textContent = "正在验证…";
  let r;
  try {
    r = await api("/auth/verify", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ qq: state.pendingQq, code, device_label: label }) });
  } catch (e) { msg.textContent = "验证失败：" + e.message; }
  if (!r) return;
  clearInMemorySensitiveState();
  if (r.pending) { state.logged = false; state.devicePending = true; state.qq = state.pendingQq; render(); return; }
  state.logged = true; state.devicePending = false; state.qq = state.pendingQq;
  try {
    const loaded = await loadView();
    if (loaded) { render(); enterViewLifecycle(state.view); }
  } catch (e) {
    const failedView = state.view;
    state.view = "dashboard";
    history.replaceState({view:state.view}, "", "#dashboard");
    const loaded = await loadView().catch(() => false);
    if (loaded) {
      state.alert = {kind:"err", text:`登录成功，但“${failedView}”页面加载失败：${e.message}`};
      render();
      enterViewLifecycle(state.view);
    }
  }
}

function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function escapeAttr(s) { return escapeHtml(s).replace(/'/g, "&#39;"); }

async function navigateFromBrowserHistory() {
  const view = normalizeView(location.hash.slice(1));
  if (view !== state.view) await navigateToView(view, {fromHistory:true});
}

window.addEventListener("popstate", navigateFromBrowserHistory);
window.addEventListener("hashchange", navigateFromBrowserHistory);
window.addEventListener("beforeunload", captureScrollState);

state.view = normalizeView(location.hash.slice(1));
history.replaceState({view:state.view}, "", `#${state.view}`);
bootstrap();
