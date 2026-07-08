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
    {key:"remote_skill_source_add", label:"远程添加"},
    {key:"remote_skill_review", label:"远程审核"},
    {key:"skill_runtime_reload", label:"Skill 重载"},
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

function traceSignalLabel(key) {
  return ({
    action: "动作",
    speech_act: "说话",
    output: "输出",
    intent: "意图",
    ambiguity: "歧义",
    tool: "工具",
    budget: "预算",
    suggested_steps: "建议步数",
    actual_steps: "实际步数",
    suggested_seconds: "建议秒数",
    actual_seconds: "实际秒数",
    topic_thread: "话题线程",
    topic_speaker: "当前发言",
    reply_to_bot: "回复bot",
    bot_in_thread: "bot在线程",
    parallel_threads: "并行线程",
    participants: "参与者",
    reason: "原因",
    source: "来源",
    flags: "质量标记",
    revision: "修订",
    chars: "字数",
    address_mode: "指向",
    quote: "引用",
    at: "@",
    target: "目标",
    query: "查询",
    finish: "结束",
  })[key] || key;
}

function renderTraceSignalTags(signals) {
  const entries = Object.entries(signals || {}).filter(([key, value]) => key && value);
  if (!entries.length) return "";
  return `<div class="trace-step-signals">${
    entries.map(([key, value]) =>
      `<span class="tag" title="${escapeAttr(key)}">${escapeHtml(traceSignalLabel(key))}: ${escapeHtml(String(value))}</span>`
    ).join("")
  }</div>`;
}

function renderTraceProcess() {
  const detail = state.traceDetail;
  const activeTraceId = state.selectedTraceId || state.logTraceId || "";
  if (!activeTraceId) return "";
  if (!detail) return `<div class="card muted">正在读取 trace 过程…</div>`;
  if (detail.error) return `<div class="card"><div class="alert err">读取 trace 失败：${escapeHtml(detail.error)}</div></div>`;
  const trace = detail.trace || {};
  const process = detail.process || {};
  const summary = process.summary || {};
  const items = process.items || [];
  const inspection = process.agent_inspection || {};
  const categoryLabel = {
    agent: "Agent",
    tool: "工具",
    semantic: "语义",
    send: "发送",
    capture: "捕获",
    dispatch: "分发",
    runtime: "运行时",
  };
  const statusMeta = {
    ok: { label: "正常", cls: "hs-ok" },
    info: { label: "信息", cls: "hs-info" },
    warn: { label: "注意", cls: "hs-warn" },
    warning: { label: "注意", cls: "hs-warn" },
    error: { label: "异常", cls: "hs-error" },
    failed: { label: "异常", cls: "hs-error" },
  };
  const slow = (summary.slow_stages || []).map(item =>
    `<span class="tag">${escapeHtml(item.label || item.key || "-")} · ${Number(item.duration_ms || 0).toLocaleString()}ms</span>`
  ).join("");
  const statCards = [
    ["阶段", summary.stage_count || 0],
    ["警告", summary.warn_count || 0],
    ["错误", summary.error_count || 0],
    ["日志", summary.log_count || 0],
  ].map(([label, value]) => `<div class="trace-stat"><strong>${escapeHtml(String(value))}</strong><span>${escapeHtml(label)}</span></div>`).join("");
  const understanding = inspection.understanding || {};
  const addressing = inspection.addressing || {};
  const budget = inspection.budget || {};
  const readableKeys = {
    intent: "理解",
    ambiguity: "歧义",
    speech_act: "说话动作",
    output: "输出模式",
    address_mode: "发送方式",
    source: "来源",
    quote: "引用",
    at: "@",
    target: "目标",
    budget: "预算",
    suggested_steps: "建议步数",
    actual_steps: "实际步数",
    suggested_seconds: "建议秒数",
    actual_seconds: "实际秒数",
  };
  const kvTags = (obj) => Object.entries(obj || {})
    .filter(([_, value]) => value)
    .map(([key, value]) => `<span class="tag" title="${escapeAttr(key)}">${escapeHtml(readableKeys[key] || key)}: ${escapeHtml(String(value))}</span>`)
    .join("");
  const toolRows = (inspection.tools || []).map(tool => `<tr>
    <td><span class="tag">${escapeHtml(tool.stage === "result" ? "结果" : "调用")}</span></td>
    <td><code>${escapeHtml(tool.tool || "-")}</code></td>
    <td>${escapeHtml(tool.status || "-")}</td>
    <td>${tool.duration_ms != null ? `${Number(tool.duration_ms || 0).toLocaleString()}ms` : "-"}</td>
    <td>${escapeHtml(tool.detail || "")}</td>
  </tr>`).join("");
  const questionTags = (inspection.questions || []).map(q => `<span class="tag">${escapeHtml(q)}</span>`).join("");
  const qualityTags = (inspection.quality || []).map(q => `<div class="trace-step-detail">${escapeHtml(q)}</div>`).join("");
  const inspectionBlock = `<div class="trace-inspection-grid">
    <div class="trace-inspection-card">
      <h3>怎么理解发言</h3>
      <div class="trace-step-signals">${kvTags(understanding) || '<span class="muted">暂无语义信号</span>'}</div>
    </div>
    <div class="trace-inspection-card">
      <h3>怎么发送</h3>
      <div class="trace-step-signals">${kvTags(addressing) || '<span class="muted">默认直发</span>'}</div>
    </div>
    <div class="trace-inspection-card">
      <h3>想查什么</h3>
      <div class="trace-step-signals">${questionTags || '<span class="muted">本轮未记录检索计划</span>'}</div>
    </div>
    <div class="trace-inspection-card">
      <h3>预算</h3>
      <div class="trace-step-signals">${kvTags(budget) || '<span class="muted">暂无预算信号</span>'}</div>
    </div>
  </div>
  <details class="trace-tool-detail" ${toolRows ? "open" : ""}>
    <summary>工具调用明细（${Number((inspection.tools || []).length || 0)}）</summary>
    ${toolRows ? `<table><thead><tr><th>阶段</th><th>工具</th><th>状态</th><th>耗时</th><th>脱敏摘要</th></tr></thead><tbody>${toolRows}</tbody></table>` : '<p class="muted">本轮未调用工具。</p>'}
  </details>
  ${qualityTags ? `<details class="trace-tool-detail"><summary>回复质量闭环</summary>${qualityTags}</details>` : ""}`;
  const timeline = items.map(item => {
    const meta = statusMeta[String(item.status || "info").toLowerCase()] || statusMeta.info;
    const duration = item.duration_ms != null ? `${Number(item.duration_ms || 0).toLocaleString()}ms` : "";
    const offset = item.offset_ms ? `+${Number(item.offset_ms).toLocaleString()}ms` : "+0ms";
    return `<div class="trace-step ${escapeAttr(item.category || "runtime")}">
      <div class="trace-step-dot ${meta.cls}"></div>
      <div class="trace-step-body">
        <div class="trace-step-head">
          <strong>${escapeHtml(item.label || item.key || "-")}</strong>
          <span class="tag">${escapeHtml(categoryLabel[item.category] || item.category || "阶段")}</span>
          <code>${escapeHtml(item.key || "")}</code>
          <span class="muted">${escapeHtml(offset)}${duration ? " · " + escapeHtml(duration) : ""}</span>
        </div>
        ${renderTraceSignalTags(item.signals)}
        ${item.detail ? `<div class="trace-step-detail">${escapeHtml(item.detail)}</div>` : ""}
        ${item.hint ? `<div class="trace-step-hint">${escapeHtml(item.hint)}</div>` : ""}
      </div>
    </div>`;
  }).join("");
  return `<div class="card trace-panel">
    <div class="between" style="gap:10px;flex-wrap:wrap">
      <h2 style="margin:0">Agent 过程可视化</h2>
      <div class="row" style="gap:6px">
        <span class="tag">trace ${escapeHtml(summary.trace_id || activeTraceId)}</span>
        ${summary.outcome ? `<span class="tag">outcome ${escapeHtml(summary.outcome)}</span>` : ""}
        ${summary.diagnosis_code ? `<span class="tag">诊断 ${escapeHtml(summary.diagnosis_code)}</span>` : ""}
      </div>
    </div>
    <p class="muted" style="font-size:12px;margin:8px 0 12px">展示的是可审计的运行阶段、耗时、工具名和脱敏摘要，不包含模型隐藏推理或完整工具结果。</p>
    <div class="trace-stat-grid">${statCards}</div>
    ${inspectionBlock}
    ${slow ? `<div class="trace-slow"><span class="muted">较慢阶段</span>${slow}</div>` : ""}
    <div class="trace-timeline">${timeline || '<p class="muted">该 trace 暂无阶段记录。</p>'}</div>
  </div>`;
}

function renderTraceDetail() {
  return `<div class="between" style="margin-bottom:10px;gap:8px;flex-wrap:wrap">
    <button class="btn small" onclick="state.view='traces'; loadView().then(render)">返回消息 Trace</button>
    ${state.selectedTraceId ? `<button class="btn small" onclick="openLogsForTrace('${escapeAttr(state.selectedTraceId)}')">查看同 Trace 日志</button>` : ""}
  </div>
  ${renderTraceProcess()}`;
}

function renderTraces() {
  const data = state.traces;
  if (!data) return `<div class="card muted">加载中…</div>`;
  const outcomeLabel = (value) => {
    const text = String(value || "-");
    const cls = text === "ok" ? "hs-ok" : (text === "failed" ? "hs-error" : (text === "no_reply" ? "hs-warn" : "hs-info"));
    return `<span class="tag"><span class="dot ${cls}" style="display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px"></span>${escapeHtml(text)}</span>`;
  };
  const rows = (data.entries || []).map(e => {
    const time = e.ts ? new Date(e.ts * 1000).toLocaleString() : "-";
    const scene = e.session_type === "group" ? `群 ${e.group_id || "-"}` : "私聊";
    const traceBtn = `<button class="btn small" onclick="openTraceDetail('${escapeAttr(e.trace_id)}')">${escapeHtml(e.trace_id || "-")}</button>`;
    return `<tr>
      <td class="muted" style="font-size:12px;white-space:nowrap">${escapeHtml(time)}</td>
      <td>${escapeHtml(scene)}<br><code style="font-size:11px">${escapeHtml(e.user_id || "-")}</code></td>
      <td style="white-space:pre-wrap;word-break:break-word">${escapeHtml(e.incoming_text || "")}</td>
      <td style="white-space:pre-wrap;word-break:break-word">${escapeHtml(e.outgoing_text || "")}</td>
      <td>${outcomeLabel(e.outcome)}<br><span class="muted" style="font-size:11px">${escapeHtml(e.diagnosis_code || "")}</span></td>
      <td>${traceBtn}<br><span class="muted" style="font-size:11px">阶段 ${Number(e.stage_count || 0)} · 警告 ${Number(e.warn_count || 0)} · 异常 ${Number(e.error_count || 0)}</span></td>
    </tr>`;
  }).join("");
  return `<div class="card">
    <div class="between" style="gap:10px;flex-wrap:wrap">
      <h2 style="margin:0">收/发消息与 Trace</h2>
      <button class="btn small" onclick="loadView().then(render)">刷新</button>
    </div>
    <p class="muted" style="font-size:12px;margin:8px 0 12px">这里按回合展示收到什么、最终发出什么和对应 Trace。点击 Trace 进入独立详情页查看可审计过程、工具调用、预算、发送指向和异常阶段。</p>
    <div class="table-wrap"><table><thead><tr><th>时间</th><th>会话</th><th>收到</th><th>发出</th><th>结果</th><th>Trace</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6" class="muted">暂无 Trace</td></tr>'}</tbody></table></div>
  </div>`;
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
    const trace = e.trace_id ? `<button class="btn small" onclick="openTraceDetail('${escapeAttr(e.trace_id)}')">${escapeHtml(e.trace_id)}</button>` : '<span class="muted">-</span>';
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
        <button class="btn small" onclick="state.logQuery=''; state.logTraceId=''; state.traceDetail=null; loadView().then(render)">重置</button>
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
  state.logTraceId = "";
  state.traceDetail = null;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function filterLogsByTrace(traceId) {
  state.logQuery = traceId || "";
  state.logTraceId = traceId || "";
  state.logLevel = "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function openTraceDetail(traceId) {
  state.view = "trace_detail";
  state.selectedTraceId = traceId || "";
  state.logTraceId = "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function openLogsForTrace(traceId) {
  state.view = "logs";
  state.logQuery = traceId || "";
  state.logTraceId = "";
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
