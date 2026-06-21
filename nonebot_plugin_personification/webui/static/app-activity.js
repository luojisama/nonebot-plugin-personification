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

function renderLogs() {
  const data = state.logs;
  if (!data) return `<div class="card muted">加载中…</div>`;
  const levels = [
    {key:"", label:"全部"},
    {key:"DEBUG", label:"DEBUG+"},
    {key:"INFO", label:"INFO+"},
    {key:"WARNING", label:"WARNING+"},
    {key:"ERROR", label:"ERROR+"},
  ];
  const levelBar = levels.map(f => `<button class="${state.logLevel===f.key?'active':''}" onclick="pickLogLevel('${f.key}')">${escapeHtml(f.label)}</button>`).join("");
  const rows = (data.entries || []).map(e => {
    const time = new Date(e.ts * 1000).toLocaleString();
    const level = String(e.level || "INFO");
    const cls = level === "ERROR" || level === "CRITICAL" ? "hs-error" : (level === "WARNING" ? "hs-warn" : (level === "DEBUG" ? "hs-info" : "hs-ok"));
    const trace = e.trace_id ? `<button class="btn small" onclick="filterLogsByTrace('${escapeAttr(e.trace_id)}')">${escapeHtml(e.trace_id)}</button>` : '<span class="muted">-</span>';
    return `<tr>
      <td class="muted" style="font-size:12px;white-space:nowrap">${escapeHtml(time)}</td>
      <td><span class="dot ${cls}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px"></span><code style="font-size:11px">${escapeHtml(level)}</code></td>
      <td>${escapeHtml(e.source||'-')}</td>
      <td style="white-space:pre-wrap;word-break:break-word">${escapeHtml(e.message||'')}</td>
      <td>${trace}</td>
    </tr>`;
  }).join("");
  return `<div class="group-bar">${levelBar}</div>
    <div class="card">
      <div class="between">
        <h2 style="margin:0">插件日志（最近 ${(data.entries||[]).length} 条）</h2>
        <button class="btn small danger" onclick="clearPluginLogs()">清空</button>
      </div>
      <p class="muted" style="font-size:12px;margin:8px 0">只显示拟人插件 runtime logger 捕获到的日志；默认保留 ${Number(data.retention_days||7)} 天，每日自动清理。敏感 token、Cookie、API Key 会在写入前脱敏。</p>
      <div class="field-input" style="margin-bottom:10px">
        <input id="log-query" type="text" placeholder="搜索消息 / source / trace_id" value="${escapeAttr(state.logQuery||'')}" onkeydown="if(event.key==='Enter') applyLogQuery()">
        <button class="btn small" onclick="applyLogQuery()">搜索</button>
        <button class="btn small" onclick="state.logQuery=''; loadView().then(render)">重置</button>
      </div>
      <table><thead><tr><th>时间</th><th>级别</th><th>来源</th><th>消息</th><th>Trace</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="muted">暂无</td></tr>'}</tbody></table>
    </div>`;
}

async function pickLogLevel(level) {
  state.logLevel = level;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function applyLogQuery() {
  state.logQuery = (document.getElementById("log-query")?.value || "").trim();
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function filterLogsByTrace(traceId) {
  state.logQuery = traceId || "";
  state.logLevel = "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function openLogsForTrace(traceId) {
  state.view = "logs";
  state.logQuery = traceId || "";
  state.logLevel = "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function clearPluginLogs() {
  if (!confirm("确认清空拟人插件持久日志？")) return;
  try {
    const res = await api("/logs/clear", {method:"DELETE"});
    alertFlash("ok", "已清空 " + (res.deleted || 0) + " 条日志");
    await loadView(); render();
  } catch (e) { alertFlash("err", e.message); }
}
