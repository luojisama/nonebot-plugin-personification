function renderDevices() {
  const rows = state.devices.map(d => {
    const isCurrent = d.id === state.currentDeviceId;
    const badge = d.status === "pending" ? '<span class="device-status pending">待审批</span>' : '<span class="device-status approved">已批准</span>';
    return `<tr>
      <td>${escapeHtml(d.label)} ${isCurrent ? '<span class="tag">当前</span>' : ''}</td>
      <td>${badge}</td>
      <td class="muted">${escapeHtml(d.ua.slice(0, 60))}</td>
      <td>${new Date(d.last_seen * 1000).toLocaleString()}</td>
      <td>
        <button class="btn small" onclick="trustDevice('${escapeAttr(d.id)}')" title="之后从该设备登录免验证">设为免验证</button>
        ${isCurrent ? '' : `<button class="btn small danger" onclick="revokeDevice('${escapeAttr(d.id)}')">撤销</button>`}
      </td>
    </tr>`;
  }).join("");
  const trusted = state.trustedDevices || [];
  const trustedRows = trusted.map(d => `<tr>
      <td>${escapeHtml(d.label)}</td>
      <td class="muted">${escapeHtml((d.ua||'').slice(0,60))}</td>
      <td>${new Date(d.created_at * 1000).toLocaleString()}</td>
      <td><button class="btn small danger" onclick="untrustDevice('${escapeAttr(d.id)}')">移除</button></td>
    </tr>`).join("");
  const trustedCard = `<div class="card">
    <h2>免验证设备 ${trusted.length?`<span class="device-status approved">${trusted.length}</span>`:''}</h2>
    <p class="muted">登记后，从相同浏览器（UA 指纹）+ 相同 QQ 登录将跳过验证码与审批，直接登录。请仅对你本人长期使用的设备启用。</p>
    ${trusted.length ? `<table><thead><tr><th>设备</th><th>UA</th><th>登记时间</th><th></th></tr></thead><tbody>${trustedRows}</tbody></table>` : '<p class="muted">暂无免验证设备。在上方「已登录设备」点“设为免验证”添加。</p>'}
  </div>`;
  const pending = state.pendingDevices || [];
  const pendingRows = pending.map(d => `<tr>
      <td>${escapeHtml(d.label)} <span class="muted">QQ ${escapeHtml(d.qq)}</span></td>
      <td class="muted">${escapeHtml(d.ua.slice(0, 60))}</td>
      <td>${new Date(d.created_at * 1000).toLocaleString()}</td>
      <td>
        <button class="btn small primary" onclick="approveDevice('${escapeAttr(d.id)}')">批准</button>
        <button class="btn small danger" onclick="revokeDevice('${escapeAttr(d.id)}')">拒绝</button>
      </td>
    </tr>`).join("");
  const pendingCard = pending.length ? `<div class="card">
    <h2>待审批设备 <span class="device-status pending">${pending.length}</span></h2>
    <p class="muted">以下新设备等待确认。请核对 QQ / 来源后再批准，拒绝将立即吊销其令牌。</p>
    <table><thead><tr><th>设备</th><th>UA</th><th>登记时间</th><th></th></tr></thead><tbody>${pendingRows}</tbody></table>
  </div>` : '';
  return `${pendingCard}<div class="card">
    <h2>已登录设备</h2>
    <table><thead><tr><th>设备</th><th>状态</th><th>UA</th><th>最后活跃</th><th></th></tr></thead><tbody>${rows}</tbody></table>
  </div>${trustedCard}`;
}

async function trustDevice(id) {
  if (!confirm("将该设备设为免验证？之后从相同浏览器+QQ 登录会跳过验证码与审批。")) return;
  try {
    await api("/auth/devices/" + encodeURIComponent(id) + "/trust", { method:"POST" });
    alertFlash("ok", "已设为免验证设备");
    await loadView(); render();
  } catch (e) { alertFlash("err", "操作失败：" + e.message); }
}

async function untrustDevice(id) {
  try {
    await api("/auth/trusted-devices/" + encodeURIComponent(id), { method:"DELETE" });
    alertFlash("ok", "已移除免验证");
    await loadView(); render();
  } catch (e) { alertFlash("err", "操作失败：" + e.message); }
}

async function approveDevice(id) {
  try {
    await api("/auth/devices/" + encodeURIComponent(id) + "/approve", { method:"POST" });
    alertFlash("ok", "已批准");
    await loadView(); render();
  } catch (e) { alertFlash("err", "批准失败：" + e.message); }
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
  const eligible = state.eligibleAdmins || [];
  let picker, hint;
  if (!eligible.length) {
    picker = `<input id="login-qq" type="text" inputmode="numeric" autocomplete="username" placeholder="输入管理员 QQ" style="width:100%;margin-top:6px">`;
    hint = `<p class="muted" style="margin-top:14px;font-size:12.5px">Bot 会向该 QQ 私聊推送 6 位数验证码；未配置为管理员的 QQ 会被拒绝。</p>`;
  } else if (eligible.length === 1) {
    const e = eligible[0];
    picker = `<input id="login-qq" type="hidden" value="${escapeAttr(e.qq)}">
      <div style="margin-top:6px;padding:10px;background:var(--zebra);border-radius:6px;border:1px solid var(--line)">
        <span style="font-family:ui-monospace,Consolas,monospace">${escapeHtml(e.qq)}</span>
        <span class="muted" style="font-size:11px;margin-left:6px">${escapeHtml(e.source)}</span>
      </div>`;
    hint = `<p class="muted" style="margin-top:14px;font-size:12.5px">将向上述 QQ 私聊推送 6 位数验证码，5 分钟内有效。</p>`;
  } else {
    picker = `<select id="login-qq" style="width:100%;margin-top:6px">
        ${eligible.map(e => `<option value="${escapeAttr(e.qq)}">${escapeHtml(e.qq)}（${escapeHtml(e.source)}）</option>`).join("")}
      </select>`;
    hint = `<p class="muted" style="margin-top:14px;font-size:12.5px">选择一个管理员 QQ，Bot 会向其私聊推送 6 位数验证码，5 分钟内有效。</p>`;
  }
  return `<div class="login-wrap"><div class="card"><div class="between">
      <h2 style="margin:0">拟人插件 WebUI 登录</h2>
      <button class="btn small" onclick="toggleTheme()" title="切换主题">${themeIcon}</button>
    </div>
    <div id="login-step1">
      <label>管理员 QQ</label>
      ${picker}
      <div style="margin-top:14px"><button class="btn primary" onclick="sendCode()">发送验证码</button></div>
      ${hint}
    </div>
    <div id="login-step2" style="display:none">
      <div class="alert info" style="font-size:12.5px">已向 QQ 发送登录请求。<b>在 QQ 私聊回复『同意登录』即可直接进入</b>（本页会自动刷新）；也可手动输入验证码。</div>
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
  try { const me = await api("/auth/me"); state.logged = true; state.devicePending = false; state.qq = me.qq; await loadView(); render(); }
  catch (e) {
    if (/DEVICE_PENDING/.test(String(e && e.message || ""))) { alertFlash("info", "仍在等待管理员批准"); }
    else { state.devicePending = false; render(); }
  }
}

async function logoutPending() {
  try { await api("/auth/logout", { method:"POST" }); } catch {}
  state.devicePending = false; state.logged = false; render();
}

async function sendCode() {
  const el = document.getElementById("login-qq");
  const qq = (el && el.value || "").trim();
  const msg = document.getElementById("login-msg");
  if (!qq) { msg.textContent = "请输入管理员 QQ。"; return; }
  msg.textContent = "正在发送…";
  try {
    const r = await api("/auth/login", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ qq }) });
    state.pendingQq = qq;
    // 免验证设备：后端直接发证
    if (r && r.passwordless) {
      if (r.pending) { state.logged = false; state.devicePending = true; state.qq = qq; render(); return; }
      state.logged = true; state.devicePending = false; state.qq = qq; await loadView(); render(); return;
    }
    document.getElementById("login-step1").style.display = "none";
    document.getElementById("login-step2").style.display = "block";
    msg.textContent = "已发送。可在 QQ 私聊回复『同意登录』直接进入，或输入验证码。";
    state.loginRequestId = (r && r.request_id) || "";
    if (state.loginRequestId) startLoginPolling();
  } catch (e) { msg.textContent = "发送失败：" + e.message; }
}

async function startLoginPolling() {
  if (state.loginPolling) return;
  state.loginPolling = true;
  const myReq = state.loginRequestId;
  while (state.loginPolling && !state.logged && state.loginRequestId === myReq) {
    await new Promise(r => setTimeout(r, 2500));
    if (!state.loginRequestId || state.loginRequestId !== myReq) break;
    let s;
    try { s = await fetch(API + "/auth/login-status?request_id=" + encodeURIComponent(myReq), { credentials:"include" }).then(r=>r.json()); }
    catch { continue; }
    if (s.status === "approved" && s.success) {
      state.loginPolling = false;
      if (s.pending) { state.devicePending = true; state.logged = false; state.qq = state.pendingQq; render(); return; }
      state.logged = true; state.devicePending = false; state.qq = state.pendingQq; await loadView(); render(); return;
    }
    if (s.status === "denied") {
      state.loginPolling = false;
      const msg = document.getElementById("login-msg");
      if (msg) msg.textContent = "管理员已拒绝本次登录请求。";
      return;
    }
    if (s.status === "expired") { state.loginPolling = false; return; }
  }
  state.loginPolling = false;
}

async function doVerify() {
  const code = document.getElementById("login-code").value.trim();
  const label = document.getElementById("login-label").value.trim();
  const msg = document.getElementById("login-msg");
  msg.textContent = "正在验证…";
  try {
    const r = await api("/auth/verify", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ qq: state.pendingQq, code, device_label: label }) });
    if (r && r.pending) { state.logged = false; state.devicePending = true; state.qq = state.pendingQq; render(); return; }
    state.logged = true; state.devicePending = false; state.qq = state.pendingQq; await loadView(); render();
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
