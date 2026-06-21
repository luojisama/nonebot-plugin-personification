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

const HEALTH_STATUS = {
  ok: {label:"正常", cls:"hs-ok"}, warn: {label:"注意", cls:"hs-warn"},
  error: {label:"异常", cls:"hs-error"}, disabled: {label:"未启用", cls:"hs-disabled"},
  info: {label:"信息", cls:"hs-info"},
};

function renderInteractionResult(ir) {
  if (!ir) return "";
  const alertCls = ir.replied ? "ok" : "err";
  const meta = [];
  if (ir.diagnosis_code) meta.push(`诊断码：${ir.diagnosis_code}`);
  if (ir.trace_id) meta.push(`trace：${ir.trace_id}`);
  if (ir.target_detail) {
    const targetParts = [];
    if (ir.target_detail.group_id) targetParts.push(`group=${ir.target_detail.group_id}`);
    if (ir.target_detail.user_id) targetParts.push(`user=${ir.target_detail.user_id}`);
    if (targetParts.length) meta.push(`目标：${targetParts.join(" ")}`);
  }
  if (ir.duration_ms != null) meta.push(`耗时：${ir.duration_ms}ms`);
  const reply = ir.reply ? `\n\n回复内容：\n${String(ir.reply)}` : "";
  const traceBtn = ir.trace_id
    ? `<button class="btn small" onclick="openLogsForTrace('${escapeAttr(ir.trace_id)}')">查看同 trace 日志</button>`
    : "";
  const stages = (ir.stages || []).map(st => {
    const status = HEALTH_STATUS[st.status] || HEALTH_STATUS.info;
    return `<tr>
      <td><span class="dot ${status.cls}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px"></span>${escapeHtml(st.label || st.key || "-")}</td>
      <td><code style="font-size:11px">${escapeHtml(st.status || "info")}</code></td>
      <td style="white-space:pre-wrap">${escapeHtml(st.detail || "")}</td>
      <td style="white-space:pre-wrap">${escapeHtml(st.hint || "")}</td>
    </tr>`;
  }).join("");
  const last = ir.last_trace || {};
  const traceSummary = last && (last.outcome || last.diagnosis_code)
    ? `<p class="muted" style="font-size:12px;margin:8px 0 0">链路收口：${escapeHtml(last.outcome || "-")} / ${escapeHtml(last.diagnosis_code || "-")}</p>`
    : "";
  return `<div style="margin-top:10px">
    <div class="alert ${alertCls}" style="white-space:pre-wrap">${escapeHtml(ir.detail || "")}${escapeHtml(reply)}</div>
    <div class="row" style="margin:6px 0 10px">
      ${meta.map(x => `<span class="tag">${escapeHtml(x)}</span>`).join("")}
      ${traceBtn}
    </div>
    ${traceSummary}
    <table style="margin-top:10px"><thead><tr><th>阶段</th><th>状态</th><th>详情</th><th>建议</th></tr></thead>
      <tbody>${stages || '<tr><td colspan="4" class="muted">无分层诊断信息</td></tr>'}</tbody></table>
  </div>`;
}

function renderHealth() {
  const h = state.health;
  if (!h) return `<div class="card muted">体检中…</div>`;
  const s = h.summary || {};
  const overall = HEALTH_STATUS[h.overall] || HEALTH_STATUS.info;
  const pill = (k) => `<div class="health-pill"><span class="health-badge ${HEALTH_STATUS[k].cls}"></span><span class="num">${s[k]||0}</span><span class="muted">${HEALTH_STATUS[k].label}</span></div>`;
  const cats = (h.categories || []).map(cat => {
    const items = (cat.checks || []).map(it => {
      const st = HEALTH_STATUS[it.status] || HEALTH_STATUS.info;
      return `<div class="health-item">
        <span class="dot ${st.cls}" title="${st.label}"></span>
        <div class="body">
          <div class="lbl">${escapeHtml(it.label)} <span class="muted" style="font-size:11px">${st.label}</span></div>
          ${it.detail ? `<div class="det">${escapeHtml(it.detail)}</div>` : ''}
          ${it.hint ? `<div class="hint">→ ${escapeHtml(it.hint)}</div>` : ''}
        </div>
      </div>`;
    }).join("");
    const busy = state.healthBusyCat === cat.name;
    return `<div class="health-cat">
      <h3>${escapeHtml(cat.name)}
        <button class="btn small" style="margin-left:auto" onclick="recheckCategory('${escapeAttr(cat.name)}')">${busy?'检测中…':'重测'}</button>
      </h3>${items||'<div class="muted">无</div>'}</div>`;
  }).join("");
  const ir = state.interactionResult;
  const interactionCard = `<div class="card">
    <h2>实际交互测试</h2>
    <p class="muted" style="font-size:12px">向「配置中心 → 运维」里设置的<b>测试群 / 测试私聊用户</b>真实注入一条消息，走完整回复链路（规则→缓冲→模型→发送），并回显 bot 实际回复。等待时间按回复超时配置加少量余量；会真的在 QQ 里发消息。</p>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" onclick="runInteraction('group')">测试群交互</button>
      <button class="btn primary" onclick="runInteraction('private')">测试私聊交互</button>
      ${state.interactionBusy?'<span class="muted">交互中（按回复超时配置）…</span>':''}
    </div>
    ${renderInteractionResult(ir)}
  </div>`;
  return `<div class="card">
    <div class="between">
      <h2 style="margin:0">功能体检 <span class="health-badge ${overall.cls}" title="${overall.label}"></span> <span class="muted" style="font-size:13px">${overall.label}</span></h2>
      <button class="btn small" onclick="refreshHealth()">${state.loading?'检测中…':'全部重新检测'}</button>
    </div>
    <p class="muted" style="font-size:12px;margin:8px 0 0">对各模块做<b>真实调用探测</b>（含画像/风格/视觉打标等子模型）。结果缓存展示、秒开；启动与配置变更后自动重跑，也可点「全部重新检测」或单项「重测」。红=异常，黄=会影响行为，灰=未启用。</p>
    <p class="muted" style="font-size:11px;margin:4px 0 0">${h.generated_at?('上次检测：'+new Date(h.generated_at*1000).toLocaleString()+(h.cached?'（缓存）':'')):''}</p>
    <div class="health-summary" style="margin-top:14px">
      ${pill('error')}${pill('warn')}${pill('ok')}${pill('disabled')}
    </div>
  </div>
  ${interactionCard}
  <div class="health-grid">${cats}</div>`;
}

async function refreshHealth() {
  state.loading = true; render();
  try { state.health = await api("/health/check?refresh=true"); } catch (e) { alertFlash("err", "检测失败：" + e.message); }
  state.loading = false; render();
}

function renderQQ() {
  const info = state.qqInfo || {};
  const groups = state.qqGroups || [];
  const friends = state.qqFriends || [];
  const infoCard = info.error
    ? `<div class="card"><div class="alert err">获取账号信息失败：${escapeHtml(info.error)}</div></div>`
    : `<div class="card">
        <h2>当前账号</h2>
        <div class="row"><span class="muted">QQ</span> <code>${escapeHtml(info.user_id||'')}</code>
          <span class="muted">昵称</span> <b>${escapeHtml(info.nickname||'')}</b></div>
        <div class="field-input" style="margin-top:12px">
          <input id="qq-nick" type="text" placeholder="新昵称" value="${escapeAttr(info.nickname||'')}">
          <button class="btn small primary" onclick="qqSetNickname()">改昵称</button>
        </div>
        <div class="field-input" style="margin-top:8px">
          <input id="qq-sign" type="text" placeholder="新签名">
          <button class="btn small" onclick="qqSetSignature()">改签名</button>
        </div>
        <div class="field-input" style="margin-top:8px">
          <input id="qq-avatar" type="text" placeholder="头像图片 URL 或 base64://...">
          <button class="btn small" onclick="qqSetAvatar()">改头像</button>
        </div>
        <p class="muted" style="font-size:11px;margin-top:8px">部分操作依赖协议端扩展（NapCat 支持较全）；不支持时会提示失败。</p>
      </div>`;
  const groupRows = groups.map(g => `<tr>
      <td>${escapeHtml(g.group_name||'')} <code>${escapeHtml(g.group_id)}</code></td>
      <td>${g.member_count}/${g.max_member_count||'-'}</td>
      <td><button class="btn small danger" onclick="qqLeaveGroup('${escapeAttr(g.group_id)}','${escapeAttr(g.group_name||'')}')">退群</button></td>
    </tr>`).join("");
  const friendRows = friends.map(f => `<tr>
      <td>${escapeHtml(f.remark||f.nickname||'')} <code>${escapeHtml(f.user_id)}</code></td>
      <td><button class="btn small danger" onclick="qqDeleteFriend('${escapeAttr(f.user_id)}','${escapeAttr(f.remark||f.nickname||'')}')">删好友</button></td>
    </tr>`).join("");
  return `${infoCard}
    <div class="card"><h2>群列表（${groups.length}）</h2>
      <table><thead><tr><th>群</th><th>人数</th><th></th></tr></thead><tbody>${groupRows||'<tr><td colspan="3" class="muted">无</td></tr>'}</tbody></table>
    </div>
    <div class="card"><h2>好友列表（${friends.length}）</h2>
      <table><thead><tr><th>好友</th><th></th></tr></thead><tbody>${friendRows||'<tr><td colspan="2" class="muted">无</td></tr>'}</tbody></table>
    </div>`;
}

async function qqSetNickname() {
  const v = (document.getElementById("qq-nick")?.value||"").trim();
  if (!v || !confirm("确认修改 bot 昵称为：" + v + " ？")) return;
  try { await api("/qq/nickname", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({nickname:v})}); alertFlash("ok","已修改"); await loadView(); render(); }
  catch (e) { alertFlash("err", e.message); }
}
async function qqSetSignature() {
  const v = (document.getElementById("qq-sign")?.value||"").trim();
  if (!confirm("确认修改签名？")) return;
  try { await api("/qq/signature", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({signature:v})}); alertFlash("ok","已修改"); }
  catch (e) { alertFlash("err", e.message); }
}
async function qqSetAvatar() {
  const v = (document.getElementById("qq-avatar")?.value||"").trim();
  if (!v || !confirm("确认修改 bot 头像？")) return;
  try { await api("/qq/avatar", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({file:v})}); alertFlash("ok","已修改"); }
  catch (e) { alertFlash("err", e.message); }
}
async function qqLeaveGroup(gid, name) {
  if (!confirm("确认让 bot 退出群「" + (name||gid) + "」？此操作不可撤销。")) return;
  try { await api("/qq/groups/"+encodeURIComponent(gid)+"/leave", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({confirm:String(gid)})}); alertFlash("ok","已退群"); await loadView(); render(); }
  catch (e) { alertFlash("err", e.message); }
}
async function qqDeleteFriend(uid, name) {
  if (!confirm("确认删除好友「" + (name||uid) + "」？")) return;
  try { await api("/qq/friends/"+encodeURIComponent(uid), {method:"DELETE",headers:{"content-type":"application/json"},body:JSON.stringify({confirm:String(uid)})}); alertFlash("ok","已删除"); await loadView(); render(); }
  catch (e) { alertFlash("err", e.message); }
}

async function recheckCategory(name) {
  state.healthBusyCat = name; render();
  try {
    const r = await api("/health/check?only=" + encodeURIComponent(name));
    const fresh = (r.categories || [])[0];
    if (fresh && state.health) {
      state.health.categories = state.health.categories.map(c => c.name === name ? fresh : c);
      // 重算汇总
      const sum = {ok:0,warn:0,error:0,disabled:0,info:0};
      state.health.categories.forEach(c => (c.checks||[]).forEach(it => { sum[it.status] = (sum[it.status]||0)+1; }));
      state.health.summary = sum;
      state.health.overall = sum.error ? 'error' : (sum.warn ? 'warn' : 'ok');
    }
  } catch (e) { alertFlash("err", "重测失败：" + e.message); }
  state.healthBusyCat = ""; render();
}

async function runInteraction(target) {
  state.interactionBusy = true; state.interactionResult = null; render();
  try {
    state.interactionResult = await api("/health/interaction-test", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ target }) });
  } catch (e) { alertFlash("err", "交互测试失败：" + e.message); }
  state.interactionBusy = false; render();
}

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

function renderQzone() {
  const q = state.qzone;
  if (!q) return `<div class="card muted">加载中…</div>`;
  const quota = q.quota || {};
  const used = Number(quota.used || 0), limit = Number(quota.limit || 0);
  const remaining = Number(quota.remaining != null ? quota.remaining : Math.max(0, limit - used));
  const pct = limit > 0 ? Math.min(100, Math.round(used / limit * 100)) : 0;
  const barColor = pct >= 90 ? "var(--danger)" : (pct >= 70 ? "var(--warn)" : "var(--ok)");
  const enabledPill = (on, label) => `<span class="device-status ${on?'approved':'pending'}">${label}：${on?'开':'关'}</span>`;
  const recent = (q.recent_contents || []).slice().reverse();
  const recentRows = recent.length
    ? recent.map(c => `<li style="padding:5px 0;border-top:1px solid var(--line)">${escapeHtml(c)}</li>`).join("")
    : '<li class="muted" style="padding:6px 0">暂无记录</li>';
  return `<div class="card">
    <div class="between" style="margin-bottom:4px">
      <h2 style="margin:0">本月发空间额度</h2>
      <div class="row">${enabledPill(q.enabled,'空间总开关')}${enabledPill(q.proactive_enabled,'主动发说说')}</div>
    </div>
    <p class="muted" style="margin:2px 0 14px">agent 会参考这份额度自己把控发不发、发的节奏；下面是当前快照。</p>
    <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:8px">
      <span style="font-size:30px;font-weight:700">${used}</span>
      <span class="muted">/ ${limit} 条（本月 ${escapeHtml(quota.month||'')}）</span>
      <span style="margin-left:auto" class="muted">剩余 <strong style="color:${barColor}">${remaining}</strong> 条 · 还剩 ${Number(quota.days_left||0)} 天</span>
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
      <div>${_fmtTs(q.last_post_at)}${q.last_content?'：'+escapeHtml(q.last_content):''}</div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="btn primary" onclick="triggerQzonePost()" ${state.qzoneBusy?'disabled':''}>${state.qzoneBusy?'<span class="spinner"></span> 发布中…':'立即发一条'}</button>
      <button class="btn small" onclick="loadView().then(render)">刷新</button>
      <span class="muted" style="font-size:12px">「立即发一条」会强制生成并发布（绕过额度/间隔判断），但仍计入本月额度。</span>
    </div>
    ${state.qzonePostResult ? `<div class="alert ${state.qzonePostResult.ok?'ok':'err'}" style="margin-top:12px">${escapeHtml(state.qzonePostResult.ok ? ('已发布：'+(state.qzonePostResult.content||'')) : (state.qzonePostResult.error||'发布失败'))}</div>` : ''}
  </div>
  <div class="card">
    <h2>最近发过的说说（去重记忆）</h2>
    <ul style="list-style:none;margin:0;padding:0">${recentRows}</ul>
  </div>`;
}

async function triggerQzonePost() {
  if (state.qzoneBusy) return;
  if (!confirm("确定现在强制发一条空间说说？会真实发布到 QQ 空间，并计入本月额度。")) return;
  state.qzoneBusy = true; state.qzonePostResult = null; render();
  try {
    const r = await api("/qzone/post-now", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({}) });
    state.qzonePostResult = r;
    if (r && r.quota && state.qzone) state.qzone.quota = r.quota;
    if (r && r.ok) { try { await loadView(); } catch {} }
  } catch (e) {
    state.qzonePostResult = { ok:false, error: e.message };
  }
  state.qzoneBusy = false; render();
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
  const structured = (core && core.structured) || {};
  const corr = (core && core.user_corrections) || {};
  const SKEY = {gender:"性别",age_group:"年龄段",occupation:"职业",interests:"兴趣",routine:"作息",communication_style:"沟通风格",emotion_baseline:"情绪基线",social_mode:"社交模式",knowledge:"知识结构",relationship:"关系",taboos:"雷区",memory_anchors:"记忆锚点",recent_focus:"近期关注",content_pref:"内容偏好",nickname_pref:"称呼偏好"};
  const structRows = Object.keys(structured).map(k => `<tr>
      <td style="white-space:nowrap">${escapeHtml(SKEY[k]||k)}${corr[SKEY[k]]||corr[k]?' <span class="device-status approved">已更正</span>':''}</td>
      <td>${escapeHtml(String(structured[k]))}</td>
    </tr>`).join("");
  const structCard = `<div class="card"><h2>结构化字段（持久保存）</h2>
    ${structRows?`<table><tbody>${structRows}</tbody></table>`:'<p class="muted">暂无结构化字段</p>'}
    <div class="field-input" style="margin-top:12px">
      <input id="corr-field" type="text" placeholder="字段（如 性别/职业）" style="max-width:160px">
      <input id="corr-value" type="text" placeholder="更正为…" style="max-width:220px">
      <button class="btn small primary" onclick="submitCorrection('${escapeAttr(p.user_id)}')">提交更正</button>
    </div>
    <p class="muted" style="font-size:11px;margin-top:6px">用户更正以最高优先级保留，后续画像重生成不会被覆盖。</p>
  </div>`;
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedPersona=null;render()">返回列表</button><span class="muted">用户 ${escapeHtml(p.user_id)}</span></div>
    <div class="card"><h2>全局印象</h2>${core ? `<pre style="white-space:pre-wrap;margin:0;font-family:inherit">${escapeHtml(core.profile_text || '')}</pre>` : '<p class="muted">无全局画像</p>'}</div>
    ${structCard}
    <h3 style="margin-bottom:10px">各群印象（${(p.local_profiles||[]).length}）</h3>
    ${locals || '<p class="muted">无各群画像</p>'}`;
}

async function submitCorrection(uid) {
  const field = (document.getElementById("corr-field")?.value||"").trim();
  const value = (document.getElementById("corr-value")?.value||"").trim();
  if (!field || !value) { alertFlash("err", "请填写字段与更正值"); return; }
  try {
    await api("/personas/"+encodeURIComponent(uid)+"/correction", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({corrections:{[field]:value}})});
    alertFlash("ok", "已提交更正");
    state.selectedPersona = await api("/personas/"+encodeURIComponent(uid));
    render();
  } catch (e) { alertFlash("err", e.message); }
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
