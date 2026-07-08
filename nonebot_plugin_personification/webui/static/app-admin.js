function dashboardCompactNumber(value, digits = 1) {
  const n = Number(value || 0);
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(digits).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(digits).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(digits).replace(/\.0$/, "") + "K";
  return String(Math.round(n));
}

function dashboardMoney(value) {
  return "$" + Number(value || 0).toFixed(2);
}

function dashboardPercent(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return "0%";
  if (n > 0 && n < 0.1) return "<0.1%";
  return n.toFixed(n >= 10 ? 1 : 2).replace(/\.0+$/, "") + "%";
}

function dashboardFullNumber(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n.toLocaleString() : "0";
}

function dashboardSeriesPointTitle(row, valueKey) {
  const item = row || {};
  const label = String(item.bucket || item.bucket_hour || item.bucket_day || item.label || "-");
  const metric = Number(item[valueKey] || 0);
  const metricLine = valueKey && valueKey !== "total_tokens"
    ? `当前值：${dashboardFullNumber(metric)}`
    : "";
  return [
    label,
    metricLine,
    `总计令牌：${dashboardFullNumber(item.total_tokens || 0)}`,
    `提示词令牌：${dashboardFullNumber(item.prompt_tokens || 0)}`,
    `回复令牌：${dashboardFullNumber(item.completion_tokens || 0)}`,
    `请求次数：${dashboardFullNumber(item.call_count || 0)}`,
  ].filter(Boolean).join("\n");
}

let dashboardTooltipEventsBound = false;

function dashboardTooltipHtml(text) {
  return String(text || "")
    .split("\n")
    .filter(line => line.trim())
    .map((line, index) => `<div class="${index === 0 ? "title" : ""}">${escapeHtml(line)}</div>`)
    .join("");
}

function dashboardTooltipAttr(text) {
  const value = String(text || "").trim();
  return value ? ` data-dashboard-tooltip="${escapeAttr(value)}"` : "";
}

function dashboardTooltipElement() {
  let el = document.getElementById("dashboard-tooltip");
  if (!el) {
    el = document.createElement("div");
    el.id = "dashboard-tooltip";
    el.className = "dashboard-tooltip";
    el.setAttribute("role", "tooltip");
    document.body.appendChild(el);
  }
  return el;
}

function positionDashboardTooltip(event) {
  const el = document.getElementById("dashboard-tooltip");
  if (!el || !el.classList.contains("visible")) return;
  const source = event && typeof event.clientX === "number"
    ? { x: event.clientX, y: event.clientY }
    : null;
  if (!source) return;
  const pad = 12;
  const gap = 14;
  const rect = el.getBoundingClientRect();
  let left = source.x + gap;
  let top = source.y + gap;
  if (left + rect.width + pad > window.innerWidth) {
    left = Math.max(pad, source.x - rect.width - gap);
  }
  if (top + rect.height + pad > window.innerHeight) {
    top = Math.max(pad, source.y - rect.height - gap);
  }
  el.style.left = `${Math.max(pad, left)}px`;
  el.style.top = `${Math.max(pad, top)}px`;
}

function showDashboardTooltip(target, event) {
  const text = target && target.getAttribute("data-dashboard-tooltip");
  if (!text) return;
  const el = dashboardTooltipElement();
  el.innerHTML = dashboardTooltipHtml(text);
  el.classList.add("visible");
  positionDashboardTooltip(event);
}

function hideDashboardTooltip() {
  const el = document.getElementById("dashboard-tooltip");
  if (el) el.classList.remove("visible");
}

function initDashboardTooltipEvents() {
  if (dashboardTooltipEventsBound) return;
  dashboardTooltipEventsBound = true;
  document.addEventListener("mouseover", event => {
    const target = event.target && event.target.closest && event.target.closest("[data-dashboard-tooltip]");
    if (target) showDashboardTooltip(target, event);
  });
  document.addEventListener("mousemove", event => {
    const target = event.target && event.target.closest && event.target.closest("[data-dashboard-tooltip]");
    if (target) positionDashboardTooltip(event);
  });
  document.addEventListener("mouseout", event => {
    const target = event.target && event.target.closest && event.target.closest("[data-dashboard-tooltip]");
    if (!target) return;
    const next = event.relatedTarget && event.relatedTarget.closest && event.relatedTarget.closest("[data-dashboard-tooltip]");
    if (next !== target) hideDashboardTooltip();
  });
  document.addEventListener("focusin", event => {
    const target = event.target && event.target.closest && event.target.closest("[data-dashboard-tooltip]");
    if (!target) return;
    const rect = target.getBoundingClientRect();
    showDashboardTooltip(target, { clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 });
  });
  document.addEventListener("focusout", event => {
    if (event.target && event.target.closest && event.target.closest("[data-dashboard-tooltip]")) {
      hideDashboardTooltip();
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") hideDashboardTooltip();
  });
}

function dashboardChartPath(points) {
  if (!points.length) return "";
  if (points.length === 1) {
    const p = points[0];
    return `M ${p.x.toFixed(1)} ${p.y.toFixed(1)} L ${(p.x + 0.1).toFixed(1)} ${p.y.toFixed(1)}`;
  }
  if (points.length === 2) {
    return points.map((p, index) => `${index === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  }
  let path = `M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`;
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const cur = points[i];
    const dx = (cur.x - prev.x) * 0.32;
    path += ` C ${(prev.x + dx).toFixed(1)} ${prev.y.toFixed(1)}, ${(cur.x - dx).toFixed(1)} ${cur.y.toFixed(1)}, ${cur.x.toFixed(1)} ${cur.y.toFixed(1)}`;
  }
  return path;
}

function renderDashboardLineChart(points, valueKey, tone, options = {}) {
  const rows = Array.isArray(points) ? points.filter(row => row && typeof row === "object") : [];
  const values = rows.map(row => {
    const n = Number(row[valueKey] || 0);
    return Number.isFinite(n) ? Math.max(0, n) : 0;
  });
  const large = !!options.large;
  const width = large ? 760 : 360;
  const height = large ? 260 : 128;
  const padX = large ? 34 : 18;
  const padTop = large ? 24 : 14;
  const padBottom = large ? 38 : 24;
  const plotBottom = height - padBottom;
  const plotHeight = plotBottom - padTop;
  const hasData = values.some(v => v > 0);
  const maxRaw = hasData ? Math.max(...values) : 0;
  const minRaw = hasData ? Math.min(...values) : 0;
  const rawSpan = maxRaw - minRaw;
  const chartMin = hasData && minRaw > 0 && rawSpan > 0 && rawSpan / maxRaw < 0.5
    ? Math.max(0, minRaw - rawSpan * 0.35)
    : 0;
  const chartMax = hasData
    ? (rawSpan > 0 ? maxRaw + rawSpan * 0.12 : maxRaw * 1.18)
    : 1;
  const span = Math.max(1, chartMax - chartMin);
  const coords = values.map((v, i) => {
    const x = values.length === 1
      ? width / 2
      : padX + i * (width - padX * 2) / Math.max(1, values.length - 1);
    const y = plotBottom - ((v - chartMin) / span) * plotHeight;
    return { x, y, value: v };
  });
  const path = dashboardChartPath(coords);
  const area = coords.length
    ? `${path} L ${coords[coords.length - 1].x.toFixed(1)} ${plotBottom.toFixed(1)} L ${coords[0].x.toFixed(1)} ${plotBottom.toFixed(1)} Z`
    : "";
  const grid = [0, 0.5, 1].map(ratio => {
    const y = padTop + ratio * plotHeight;
    return `<line x1="${padX}" y1="${y.toFixed(1)}" x2="${width - padX}" y2="${y.toFixed(1)}" stroke="currentColor" stroke-opacity="${ratio === 1 ? "0.22" : "0.10"}" stroke-width="1" vector-effect="non-scaling-stroke"></line>`;
  }).join("");
  const lastPoint = coords[coords.length - 1];
  const markerEvery = coords.length <= 18 ? 1 : Math.ceil(coords.length / 12);
  const markers = coords.map((point, index) => ({ point, index }))
    .filter(({ point, index }) => hasData && (index === coords.length - 1 || (point.value > 0 && index % markerEvery === 0)))
    .map(({ point, index }) => {
      const title = rows[index] ? dashboardSeriesPointTitle(rows[index], valueKey) : "";
      return `<circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="${index === coords.length - 1 ? "3.8" : "2.2"}" fill="var(--panel)" stroke="currentColor" stroke-width="${index === coords.length - 1 ? "2" : "1.4"}" opacity="${index === coords.length - 1 ? "1" : "0.72"}" vector-effect="non-scaling-stroke"${dashboardTooltipAttr(title)}><title>${escapeHtml(title)}</title></circle>`;
    }).join("");
  const slotWidth = coords.length > 1
    ? (width - padX * 2) / Math.max(1, coords.length - 1)
    : width - padX * 2;
  const hotWidth = Math.max(large ? 18 : 12, slotWidth);
  const hotspots = coords.map((point, index) => {
    const x = Math.max(0, Math.min(width - hotWidth, point.x - hotWidth / 2));
    const title = rows[index] ? dashboardSeriesPointTitle(rows[index], valueKey) : "";
    return `<rect class="dashboard-line-hotspot" x="${x.toFixed(1)}" y="${padTop}" width="${hotWidth.toFixed(1)}" height="${plotHeight.toFixed(1)}" fill="transparent" tabindex="0"${dashboardTooltipAttr(title)}><title>${escapeHtml(title)}</title></rect>`;
  }).join("");
  const firstLabel = rows.length ? String(rows[0].label || rows[0].bucket || "") : "";
  const lastLabel = rows.length ? String(rows[rows.length - 1].label || rows[rows.length - 1].bucket || "") : "";
  return `<svg class="dashboard-line-chart ${large ? "large" : ""} ${tone || ""}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="令牌消耗折线图">
    ${grid}
    ${area ? `<path d="${area}" fill="currentColor" opacity="0.10"></path>` : ""}
    ${path ? `<path d="${path}" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"></path>` : ""}
    ${lastPoint && hasData ? `<line x1="${lastPoint.x.toFixed(1)}" y1="${padTop}" x2="${lastPoint.x.toFixed(1)}" y2="${plotBottom}" stroke="currentColor" stroke-opacity="0.12" stroke-width="1" vector-effect="non-scaling-stroke"></line>` : ""}
    ${markers}
    ${hotspots}
    ${hasData ? `<text x="${padX}" y="11" fill="currentColor" opacity="0.70" font-size="10">${escapeHtml(dashboardCompactNumber(maxRaw))} 令牌</text>` : `<text x="${width / 2}" y="${height / 2}" fill="currentColor" opacity="0.55" font-size="12" text-anchor="middle">暂无数据</text>`}
    ${lastPoint && hasData ? `<text x="${width - padX}" y="11" fill="currentColor" opacity="0.72" font-size="10" text-anchor="end">${escapeHtml(dashboardCompactNumber(lastPoint.value))} 令牌</text>` : ""}
    <text x="${padX}" y="${height - 3}" fill="currentColor" opacity="0.55" font-size="10">${escapeHtml(firstLabel)}</text>
    <text x="${width - padX}" y="${height - 3}" fill="currentColor" opacity="0.55" font-size="10" text-anchor="end">${escapeHtml(lastLabel)}</text>
  </svg>`;
}

function renderDashboardLineCard(chart, tone) {
  const total = chart && chart.total || {};
  const series = chart && chart.series || [];
  const valueKey = chart && chart.value_key || "total_tokens";
  const key = chart && chart.key || "";
  const tokenText = dashboardCompactNumber(total.total_tokens || 0);
  const callText = Number(total.call_count || 0).toLocaleString();
  const promptText = dashboardCompactNumber(total.prompt_tokens || 0);
  const completionText = dashboardCompactNumber(total.completion_tokens || 0);
  return `<div class="dashboard-line-card">
    <div class="dashboard-line-head">
      <span class="muted">${escapeHtml(chart && chart.label || "")}</span>
      <strong>${escapeHtml(tokenText)} 令牌</strong>
      <button class="btn small dashboard-chart-open" onclick="openDashboardLineDetail('${escapeAttr(key)}')">放大</button>
    </div>
    <div class="dashboard-line-meta">
      <span>${escapeHtml(callText)} 次请求</span>
      <span>提示/回复 ${escapeHtml(promptText)} / ${escapeHtml(completionText)}</span>
    </div>
    <div class="dashboard-line-chart-wrap" onclick="openDashboardLineDetail('${escapeAttr(key)}')">
      ${renderDashboardLineChart(series, valueKey, tone, { chartKey: key })}
    </div>
  </div>`;
}

function renderDashboardModelUsage(rows) {
  const data = (rows || []).slice(0, 16);
  const body = data.map(row => {
    const width = Math.max(1.5, Math.min(100, Number(row.relative_width || 0) * 100));
    return `<tr>
      <td class="dashboard-model-cell" title="${escapeAttr(row.model || "unknown")}">${escapeHtml(row.model || "unknown")}</td>
      <td>${Number(row.call_count || 0).toLocaleString()}</td>
      <td>
        <div class="dashboard-token-bar">
          <div style="width:${width.toFixed(1)}%"></div>
          <span>${Number(row.total_tokens || 0).toLocaleString()}</span>
        </div>
      </td>
    </tr>`;
  }).join("");
  return `<div class="card dashboard-panel">
    <h2>模型用量（总计）</h2>
    <table class="dashboard-model-table">
      <thead><tr><th>模型名</th><th>请求次数</th><th>令牌消耗</th></tr></thead>
      <tbody>${body || '<tr><td colspan="3" class="muted">暂无模型用量。</td></tr>'}</tbody>
    </table>
  </div>`;
}

function renderDashboardPurposeUsage(rows) {
  const data = (rows || []).slice(0, 16);
  const body = data.map(row => {
    const width = Math.max(1.5, Math.min(100, Number(row.relative_width || 0) * 100));
    const label = row.purpose_label || row.purpose || "unknown";
    const title = row.purpose || label;
    return `<tr>
      <td class="dashboard-model-cell" title="${escapeAttr(title)}">${escapeHtml(label)}</td>
      <td>${Number(row.call_count || 0).toLocaleString()}</td>
      <td>
        <div class="dashboard-token-bar">
          <div style="width:${width.toFixed(1)}%"></div>
          <span>${Number(row.total_tokens || 0).toLocaleString()}</span>
        </div>
      </td>
    </tr>`;
  }).join("");
  return `<div class="card dashboard-panel">
    <h2>功能用量（总计）</h2>
    <table class="dashboard-model-table">
      <thead><tr><th>功能</th><th>请求次数</th><th>令牌消耗</th></tr></thead>
      <tbody>${body || '<tr><td colspan="3" class="muted">暂无功能用量。</td></tr>'}</tbody>
    </table>
  </div>`;
}

function dashboardGroupLabel(row) {
  const label = row && (row.group_label || row.group_name);
  if (label) return String(label);
  const groupId = row && row.group_id ? String(row.group_id) : "";
  return groupId ? `群 ${groupId}` : "群名获取失败";
}

function dashboardPieRowTitle(row, total) {
  const label = dashboardGroupLabel(row);
  const pct = total > 0 ? Number(row.total_tokens || 0) / total * 100 : 0;
  const groupId = row && row.group_id ? String(row.group_id) : "";
  return [
    label + (groupId ? `（${groupId}）` : ""),
    `占比：${dashboardPercent(pct)}`,
    `总计令牌：${dashboardFullNumber(row.total_tokens || 0)}`,
    `提示词令牌：${dashboardFullNumber(row.prompt_tokens || 0)}`,
    `回复令牌：${dashboardFullNumber(row.completion_tokens || 0)}`,
    `请求次数：${dashboardFullNumber(row.call_count || 0)}`,
  ].join("\n");
}

function dashboardPiePoint(cx, cy, radius, ratio) {
  const angle = ratio * Math.PI * 2 - Math.PI / 2;
  return {
    x: cx + radius * Math.cos(angle),
    y: cy + radius * Math.sin(angle),
  };
}

function dashboardPieSegmentPath(startRatio, endRatio) {
  const cx = 50;
  const cy = 50;
  const outer = 48;
  const inner = 27;
  const start = Number(startRatio || 0);
  let end = Number(endRatio || 0);
  if (end - start >= 0.9999) end = start + 0.9999;
  if (end <= start) end = start + 0.0001;
  const outerStart = dashboardPiePoint(cx, cy, outer, start);
  const outerEnd = dashboardPiePoint(cx, cy, outer, end);
  const innerEnd = dashboardPiePoint(cx, cy, inner, end);
  const innerStart = dashboardPiePoint(cx, cy, inner, start);
  const largeArc = end - start > 0.5 ? 1 : 0;
  return [
    `M ${outerStart.x.toFixed(3)} ${outerStart.y.toFixed(3)}`,
    `A ${outer} ${outer} 0 ${largeArc} 1 ${outerEnd.x.toFixed(3)} ${outerEnd.y.toFixed(3)}`,
    `L ${innerEnd.x.toFixed(3)} ${innerEnd.y.toFixed(3)}`,
    `A ${inner} ${inner} 0 ${largeArc} 0 ${innerStart.x.toFixed(3)} ${innerStart.y.toFixed(3)}`,
    "Z",
  ].join(" ");
}

function renderDashboardPieSvg(data, total, colors) {
  if (!data.length || total <= 0) {
    return `<svg class="dashboard-pie-svg" viewBox="0 0 100 100" role="img" aria-label="暂无群消耗占比">
      <circle class="dashboard-pie-empty" cx="50" cy="50" r="37" fill="none" stroke="currentColor" stroke-opacity="0.18" stroke-width="22"><title>暂无群用量</title></circle>
    </svg>`;
  }
  let cursor = 0;
  const slices = data.map((row, index) => {
    const pct = Number(row.total_tokens || 0) / total;
    const start = cursor;
    const end = Math.min(1, cursor + pct);
    cursor = end;
    const title = dashboardPieRowTitle(row, total);
    return `<path class="dashboard-pie-slice" d="${dashboardPieSegmentPath(start, end)}" fill="${colors[index % colors.length]}" role="listitem" tabindex="0" aria-label="${escapeAttr(title.replace(/\n/g, " · "))}" vector-effect="non-scaling-stroke"${dashboardTooltipAttr(title)}><title>${escapeHtml(title)}</title></path>`;
  }).join("");
  return `<svg class="dashboard-pie-svg" viewBox="0 0 100 100" role="img" aria-label="群消耗占比饼图">
    ${slices}
  </svg>`;
}

function renderDashboardGroupPie(rows, options = {}) {
  const modal = !!options.modal;
  const colors = ["#4f8cff", "#20c997", "#ffb020", "#ff6b6b", "#9775fa", "#38bdf8", "#f472b6", "#94d82d", "#ffa94d", "#adb5bd"];
  const source = (rows || []).filter(row => Number(row.total_tokens || 0) > 0);
  const top = source.slice(0, 9);
  const rest = source.slice(9);
  const restTokens = rest.reduce((sum, row) => sum + Number(row.total_tokens || 0), 0);
  const data = restTokens > 0
    ? [...top, {
        group_label: "其他群",
        total_tokens: restTokens,
        prompt_tokens: rest.reduce((sum, row) => sum + Number(row.prompt_tokens || 0), 0),
        completion_tokens: rest.reduce((sum, row) => sum + Number(row.completion_tokens || 0), 0),
        call_count: rest.reduce((sum, row) => sum + Number(row.call_count || 0), 0),
      }]
    : top;
  const total = data.reduce((sum, row) => sum + Number(row.total_tokens || 0), 0);
  const legend = data.map((row, index) => {
    const pct = total > 0 ? Number(row.total_tokens || 0) / total * 100 : 0;
    const label = dashboardGroupLabel(row);
    const groupId = row.group_id ? String(row.group_id) : "";
    const title = row.group_name
      ? `${row.group_name}${groupId ? ` · 群号 ${groupId}` : ""}`
      : `${row.group_name_missing ? "群名获取失败；" : ""}${groupId ? `群号 ${groupId}` : label}`;
    const detail = dashboardPieRowTitle(row, total);
    return `<div class="dashboard-pie-legend-row" title="${escapeAttr(detail)}" tabindex="0"${dashboardTooltipAttr(detail)}>
      <span class="dashboard-pie-dot" style="background:${colors[index % colors.length]}"></span>
      <span class="dashboard-pie-name" title="${escapeAttr(title)}">${escapeHtml(label)}</span>
      <span class="dashboard-pie-percent">${escapeHtml(dashboardPercent(pct))}</span>
      <span class="dashboard-pie-token">${escapeHtml(dashboardCompactNumber(row.total_tokens || 0))} 令牌</span>
    </div>`;
  }).join("");
  return `<div class="${modal ? "dashboard-modal-pie" : "card dashboard-panel"}">
    <div class="dashboard-panel-head">
      <h2>群消耗占比（总计）</h2>
      ${modal ? "" : '<button class="btn small" onclick="openDashboardPieDetail()">放大</button>'}
    </div>
    <div class="dashboard-pie-layout">
      <div class="dashboard-pie" ${modal ? "" : 'onclick="openDashboardPieDetail()"'} title="${data.length ? "" : "暂无群用量"}">
        ${renderDashboardPieSvg(data, total, colors)}
        <div class="dashboard-pie-center"><strong>${escapeHtml(dashboardCompactNumber(total))} 令牌</strong><span>总令牌</span></div>
      </div>
      <div class="dashboard-pie-legend">${legend || '<p class="muted">暂无群用量。</p>'}</div>
    </div>
  </div>`;
}

function dashboardOverviewCharts(d) {
  const overview = d && d.dashboard_overview || {};
  return overview.charts && overview.charts.length
    ? overview.charts
    : [
        { key: "day", label: "24小时", total: d && d.total || {}, series: d && d.series || [], value_key: "total_tokens" },
        { key: "week", label: "7天", total: d && d.total || {}, series: d && d.series || [], value_key: "total_tokens" },
        { key: "month", label: "30天", total: d && d.total || {}, series: d && d.series || [], value_key: "total_tokens" },
        { key: "total", label: "总消耗", total: (d && d.total_consumption || {}).total || {}, series: (d && d.total_consumption || {}).series || [], value_key: "cumulative_total_tokens" },
      ];
}

function openDashboardLineDetail(key) {
  if (!key) return;
  state.dashboardDetail = { type: "line", key };
  render();
}

function openDashboardPieDetail() {
  state.dashboardDetail = { type: "pie" };
  render();
}

function closeDashboardDetail() {
  state.dashboardDetail = null;
  render();
}

function dashboardLineDetailTable(rows, valueKey) {
  const colspan = valueKey !== "total_tokens" ? 7 : 6;
  const body = (rows || []).map(row => {
    const bucket = row.bucket || row.bucket_hour || row.bucket_day || "";
    return `<tr>
      <td>${escapeHtml(bucket)}</td>
      <td>${escapeHtml(row.label || "")}</td>
      <td>${dashboardFullNumber(row.call_count || 0)}</td>
      <td>${dashboardFullNumber(row.prompt_tokens || 0)}</td>
      <td>${dashboardFullNumber(row.completion_tokens || 0)}</td>
      <td>${dashboardFullNumber(row.total_tokens || 0)}</td>
      ${valueKey !== "total_tokens" ? `<td>${dashboardFullNumber(row[valueKey] || 0)}</td>` : ""}
    </tr>`;
  }).join("");
  return `<div class="table-wrap dashboard-detail-table">
    <table>
      <thead><tr><th>时间桶</th><th>标签</th><th>请求</th><th>提示词</th><th>回复</th><th>总计</th>${valueKey !== "total_tokens" ? "<th>曲线值</th>" : ""}</tr></thead>
      <tbody>${body || `<tr><td colspan="${colspan}" class="muted">暂无明细。</td></tr>`}</tbody>
    </table>
  </div>`;
}

function dashboardPieDetailTable(rows) {
  const data = (rows || []).filter(row => Number(row.total_tokens || 0) > 0);
  const total = data.reduce((sum, row) => sum + Number(row.total_tokens || 0), 0);
  const body = data.map(row => {
    const groupId = row.group_id ? String(row.group_id) : "";
    const pct = total > 0 ? Number(row.total_tokens || 0) / total * 100 : 0;
    return `<tr title="${escapeAttr(dashboardPieRowTitle(row, total))}">
      <td>${escapeHtml(dashboardGroupLabel(row))}</td>
      <td>${escapeHtml(groupId || "-")}</td>
      <td>${escapeHtml(dashboardPercent(pct))}</td>
      <td>${dashboardFullNumber(row.call_count || 0)}</td>
      <td>${dashboardFullNumber(row.prompt_tokens || 0)}</td>
      <td>${dashboardFullNumber(row.completion_tokens || 0)}</td>
      <td>${dashboardFullNumber(row.total_tokens || 0)}</td>
    </tr>`;
  }).join("");
  return `<div class="table-wrap dashboard-detail-table">
    <table>
      <thead><tr><th>群</th><th>群号</th><th>占比</th><th>请求</th><th>提示词</th><th>回复</th><th>总计</th></tr></thead>
      <tbody>${body || '<tr><td colspan="7" class="muted">暂无群用量。</td></tr>'}</tbody>
    </table>
  </div>`;
}

function renderDashboardDetailModal(charts, tones) {
  const detail = state.dashboardDetail;
  if (!detail) return "";
  const overview = (state.dashboard || {}).dashboard_overview || {};
  if (detail.type === "line") {
    const chart = (charts || []).find(item => String(item.key || "") === String(detail.key || ""));
    if (!chart) return "";
    const tone = tones[Math.max(0, (charts || []).indexOf(chart)) % tones.length];
    const rows = chart.series || [];
    const valueKey = chart.value_key || "total_tokens";
    const total = chart.total || {};
    return `<div class="dashboard-modal-backdrop" onclick="closeDashboardDetail()">
      <div class="dashboard-modal" onclick="event.stopPropagation()">
        <div class="dashboard-modal-head">
          <div>
            <h2>${escapeHtml(chart.label || "图表明细")}</h2>
            <p class="muted">${dashboardFullNumber(total.total_tokens || 0)} 令牌 · ${dashboardFullNumber(total.call_count || 0)} 次请求</p>
          </div>
          <button class="btn small" onclick="closeDashboardDetail()">关闭</button>
        </div>
        <div class="dashboard-modal-chart">
          ${renderDashboardLineChart(rows, valueKey, tone, { large: true })}
        </div>
        ${dashboardLineDetailTable(rows, valueKey)}
      </div>
    </div>`;
  }
  if (detail.type === "pie") {
    const rows = overview.group_usage || ((state.dashboard || {}).total_consumption || {}).by_group || [];
    const total = rows.reduce((sum, row) => sum + Number(row.total_tokens || 0), 0);
    return `<div class="dashboard-modal-backdrop" onclick="closeDashboardDetail()">
      <div class="dashboard-modal" onclick="event.stopPropagation()">
        <div class="dashboard-modal-head">
          <div>
            <h2>群消耗占比明细</h2>
            <p class="muted">${dashboardFullNumber(total)} 令牌 · ${dashboardFullNumber(rows.length)} 个群</p>
          </div>
          <button class="btn small" onclick="closeDashboardDetail()">关闭</button>
        </div>
        ${renderDashboardGroupPie(rows, { modal: true })}
        ${dashboardPieDetailTable(rows)}
      </div>
    </div>`;
  }
  return "";
}

function renderDashboard() {
  const d = state.dashboard;
  if (!d) return `<div class="card muted">加载中…</div>`;
  initDashboardTooltipEvents();
  const overview = d.dashboard_overview || {};
  const charts = dashboardOverviewCharts(d);
  const tones = ["blue", "green", "orange", "purple"];
  const totalTokens = Number(((d.total_consumption || {}).total || {}).total_tokens || 0);
  const empty = totalTokens === 0;
  return `<div class="dashboard-toolbar">
      <div>
        <h2 style="margin:0">令牌消耗统计</h2>
        <p class="muted" style="margin:4px 0 0;font-size:12px">24h、7天、30天与全量累计；模型、功能与群占比使用总计账本。</p>
      </div>
      <a href="#logs" onclick="state.view='logs'; loadView().then(render)">查看日志 →</a>
    </div>
    <div class="dashboard-line-grid">
      ${charts.slice(0, 4).map((chart, index) => renderDashboardLineCard(chart, tones[index % tones.length])).join("")}
    </div>
    ${empty ? `<div class="alert info">暂无令牌数据。LLM 调用记录写入后，这里会展示本地令牌账本统计。</div>` : ""}
    <div class="dashboard-usage-grid">
      ${renderDashboardModelUsage(overview.model_usage || ((d.total_consumption || {}).by_model || d.by_model || []))}
      ${renderDashboardPurposeUsage(overview.purpose_usage || ((d.total_consumption || {}).by_purpose || d.by_purpose || []))}
      ${renderDashboardGroupPie(overview.group_usage || ((d.total_consumption || {}).by_group || d.by_group || []))}
    </div>
    ${renderDashboardDetailModal(charts, tones)}`;
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

function renderQzoneForwardResult(result) {
  if (!result) return "";
  const ok = !!result.ok;
  const feed = result.feed || {};
  const quota = result.quota || {};
  const quotaLine = quota.month
    ? `本月额度：${Number(quota.used || 0)} / ${Number(quota.limit || 0)}，剩余 ${Number(quota.remaining || 0)}`
    : "";
  const detail = ok
    ? `已转发 ${result.target_user_id || ""} 的第一条空间动态`
    : (result.error || "转发测试失败");
  const feedText = feed.content ? `\n\n动态内容：${feed.content}` : "";
  return `<div style="margin-top:10px">
    <div class="alert ${ok?'ok':'err'}" style="white-space:pre-wrap">${escapeHtml(detail + feedText)}</div>
    <div class="row" style="margin-top:8px">
      ${result.stage ? `<span class="tag">阶段：${escapeHtml(result.stage)}</span>` : ""}
      ${feed.owner_uin ? `<span class="tag">owner=${escapeHtml(feed.owner_uin)}</span>` : ""}
      ${feed.feed_id ? `<span class="tag">feed=${escapeHtml(feed.feed_id)}</span>` : ""}
      ${quotaLine ? `<span class="tag">${escapeHtml(quotaLine)}</span>` : ""}
    </div>
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
  const qzf = state.qzoneForwardForm || {};
  const interactionCard = `<div class="card">
    <h2>实际交互测试</h2>
    <p class="muted" style="font-size:12px">向「配置中心 → 运维」里设置的<b>测试群 / 测试私聊用户</b>真实注入一条消息，走完整回复链路（规则→缓冲→模型→发送），并回显 bot 实际回复。等待时间按回复超时配置加少量余量；会真的在 QQ 里发消息。</p>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" onclick="runInteraction('group')" ${state.interactionBusy?'disabled':''}>测试群交互</button>
      <button class="btn primary" onclick="runInteraction('private')" ${state.interactionBusy?'disabled':''}>测试私聊交互</button>
      ${state.interactionBusy?'<span class="muted">交互中（按回复超时配置）…</span>':''}
    </div>
    ${renderInteractionResult(ir)}
  </div>`;
  const qzoneForwardCard = `<div class="card">
    <h2>QZone 首条转发测试</h2>
    <p class="muted" style="font-size:12px">指定一个 QQ，读取该用户空间第一条动态并真实转发到 bot 空间；成功后计入本月 QQ 空间额度。只用于管理员显式体检，不走自动转发决策。</p>
    <div class="row" style="margin-top:10px;gap:8px;align-items:center">
      <input id="qzone-forward-target" type="text" placeholder="目标 QQ 或 [CQ:at]" value="${escapeAttr(qzf.target_user_id || "")}" oninput="state.qzoneForwardForm.target_user_id=this.value" style="width:220px" ${state.qzoneForwardBusy?'disabled':''}>
      <input id="qzone-forward-text" type="text" placeholder="转发附言，可空" value="${escapeAttr(qzf.forward_text || "")}" oninput="state.qzoneForwardForm.forward_text=this.value" style="min-width:220px;flex:1" ${state.qzoneForwardBusy?'disabled':''}>
      <button class="btn primary" onclick="runQzoneForwardTest()" ${state.qzoneForwardBusy?'disabled':''}>${state.qzoneForwardBusy?'<span class="spinner"></span> 转发中…':'转发第一条'}</button>
    </div>
    ${renderQzoneForwardResult(state.qzoneForwardResult)}
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
  ${qzoneForwardCard}
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
  if (state.interactionBusy) return;
  state.interactionBusy = true; state.interactionResult = null; render();
  try {
    state.interactionResult = await api("/health/interaction-test", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ target }) });
  } catch (e) { alertFlash("err", "交互测试失败：" + e.message); }
  state.interactionBusy = false; render();
}

async function runQzoneForwardTest() {
  if (state.qzoneForwardBusy) return;
  const form = state.qzoneForwardForm || {};
  const target = String(form.target_user_id || "").trim();
  const forwardText = String(form.forward_text || "").trim();
  if (!target) { alertFlash("err", "请输入目标 QQ"); return; }
  if (!confirm("确认转发该用户空间第一条动态？这会真实发布到 bot 的 QQ 空间，并消耗本月空间额度。")) return;
  state.qzoneForwardBusy = true;
  state.qzoneForwardResult = null;
  render();
  try {
    state.qzoneForwardResult = await api("/health/qzone-forward-test", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ target_user_id: target, forward_text: forwardText }),
    });
  } catch (e) {
    state.qzoneForwardResult = { ok:false, error:e.message };
  }
  state.qzoneForwardBusy = false;
  render();
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
    ? recent.map(c => `<li class="qzone-recent-item">${escapeHtml(c)}</li>`).join("")
    : '<li class="qzone-recent-item muted">暂无记录</li>';
  return `<div class="card">
    <div class="between" style="margin-bottom:4px">
      <h2 style="margin:0">本月发空间额度</h2>
      <div class="row">${enabledPill(q.enabled,'空间总开关')}${enabledPill(q.proactive_enabled,'主动发说说')}</div>
    </div>
    <p class="muted" style="margin:2px 0 14px">agent 会参考这份额度自己把控发不发、发的节奏；下面是当前快照。</p>
    <div class="qzone-quota-line">
      <span style="font-size:30px;font-weight:700">${used}</span>
      <span class="muted">/ ${limit} 条（本月 ${escapeHtml(quota.month||'')}）</span>
      <span class="muted qzone-quota-remain">剩余 <strong style="color:${barColor}">${remaining}</strong> 条 · 还剩 ${Number(quota.days_left||0)} 天</span>
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
    <ul class="qzone-recent-list">${recentRows}</ul>
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
    <td><img class="avatar" src="${escapeAttr(p.avatar_url || `https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(p.user_id)}&spec=100`)}" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
    <td><code>${escapeHtml(p.user_id)}</code></td>
    <td>${escapeHtml(p.nickname || '')}</td>
    <td>${renderFavorabilityBadge(p.favorability)}</td>
    <td>${escapeHtml(p.snippet)}</td>
    <td>${p.updated_at ? new Date(p.updated_at*1000).toLocaleDateString() : '-'}</td>
    <td><button class="btn small" onclick="openPersona('${escapeAttr(p.user_id)}')">详情</button></td>
  </tr>`).join("");
  return `<div class="card"><h2>用户画像（${state.personas.length}）</h2>
    <table><thead><tr><th style="width:40px"></th><th>QQ</th><th>昵称</th><th>好感度</th><th>摘要</th><th>更新</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="7" class="muted">暂无画像</td></tr>'}</tbody></table></div>`;
}

function favorabilityScoreText(fav) {
  if (!fav || fav.available === false) return "不可用";
  const score = Number(fav.score || 0);
  return `${score.toFixed(2)}${fav.level ? " · " + fav.level : ""}`;
}

function renderFavorabilityBadge(fav) {
  if (!fav || fav.available === false) return '<span class="muted">—</span>';
  const score = Number(fav.score || 0);
  let style = 'background:rgba(106,168,255,0.18);color:var(--accent)';
  if (score >= 85) style = 'background:rgba(52,211,153,0.18);color:var(--ok)';
  else if (score < 20) style = 'background:rgba(245,158,11,0.16);color:var(--warn)';
  if (fav.is_perm_blacklisted) style = 'background:rgba(248,113,113,0.16);color:var(--danger)';
  return `<span class="tag" style="${style}">${escapeHtml(favorabilityScoreText(fav))}</span>`;
}

function renderFavorabilityCard(fav, title) {
  if (!fav || fav.available === false) {
    return `<div class="card"><h2>${escapeHtml(title)}</h2><p class="muted">好感度服务未就绪。</p></div>`;
  }
  const events = fav.events || [];
  const eventRows = events.map(e => {
    const delta = Number(e.delta || 0);
    const deltaText = (delta > 0 ? "+" : "") + delta.toFixed(2);
    const color = delta > 0 ? "var(--ok)" : (delta < 0 ? "var(--danger)" : "var(--muted)");
    const when = e.timestamp ? new Date(e.timestamp*1000).toLocaleString() : (e.date || "-");
    return `<tr>
      <td>${escapeHtml(when)}</td>
      <td>${escapeHtml(e.label || "其他好感事件")}</td>
      <td style="color:${color};font-weight:600">${escapeHtml(deltaText)}</td>
      <td>${escapeHtml(e.status_label || "")}</td>
      <td>${escapeHtml(e.reason || "")}</td>
    </tr>`;
  }).join("");
  const last = fav.latest_event;
  const lastLine = last
    ? `${last.label || "其他好感事件"} ${(Number(last.delta || 0) > 0 ? "+" : "")}${Number(last.delta || 0).toFixed(2)}`
    : "暂无事件";
  return `<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">${escapeHtml(title)}</h2>
      ${renderFavorabilityBadge(fav)}
    </div>
    <div class="row" style="gap:24px;margin-top:12px">
      <div><div class="muted">当前分值</div><div style="font-size:22px;font-weight:700">${Number(fav.score || 0).toFixed(2)}</div></div>
      <div><div class="muted">等级</div><div style="font-size:18px">${escapeHtml(fav.level || "—")}</div></div>
      <div><div class="muted">今日加分</div><div>${Number(fav.daily_positive_count || 0).toFixed(2)}</div></div>
      <div><div class="muted">今日扣分</div><div>${Number(fav.daily_negative_count || 0).toFixed(2)}</div></div>
      <div><div class="muted">最近事件</div><div>${escapeHtml(lastLine)}</div></div>
      ${fav.is_perm_blacklisted ? '<div><div class="muted">黑名单</div><div style="color:var(--danger)">永久黑名单</div></div>' : ''}
    </div>
    ${events.length ? `<details style="margin-top:12px"><summary class="muted" style="cursor:pointer">最近好感事件</summary>
      <table style="margin-top:8px"><thead><tr><th>时间</th><th>事件</th><th>变化</th><th>状态</th><th>原因</th></tr></thead><tbody>${eventRows}</tbody></table>
    </details>` : ''}
  </div>`;
}

async function openPersona(uid) {
  try {
    state.selectedPersona = await api("/personas/" + encodeURIComponent(uid));
    render();
  } catch (e) { alertFlash("err", e.message); }
}

function renderQqProfileCard(core, userId) {
  const meta = (core && core.qq_profile) || {};
  const avatar = meta.avatar_url || `https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(userId)}&spec=640`;
  const homepage = meta.homepage_url || "";
  const rows = [
    ["昵称", meta.nickname],
    ["群名片", meta.card],
    ["备注", meta.remark],
    ["性别", meta.sex],
    ["年龄", meta.age],
    ["QID", meta.qid],
    ["等级", meta.level],
    ["登录天数", meta.login_days],
    ["地区", meta.area],
    ["群角色", meta.role],
    ["专属头衔", meta.title],
    ["个性签名", meta.signature],
  ].filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== "")
   .map(([k, v]) => `<tr><td class="muted" style="white-space:nowrap">${escapeHtml(k)}</td><td>${escapeHtml(String(v))}</td></tr>`).join("");
  return `<div class="card">
    <h2>QQ 资料快照</h2>
    <div class="qq-profile-card">
      <img class="qq-profile-avatar" src="${escapeAttr(avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">
      <div class="qq-profile-body">
        ${rows ? `<table><tbody>${rows}</tbody></table>` : '<p class="muted">暂无协议资料字段。</p>'}
        <div class="qq-profile-links">
          ${meta.avatar_url ? `<a class="btn small" href="${escapeAttr(meta.avatar_url)}" target="_blank" rel="noreferrer">查看头像</a>` : ''}
          ${homepage ? `<a class="btn small" href="${escapeAttr(homepage)}" target="_blank" rel="noreferrer">打开主页</a>` : ''}
        </div>
      </div>
    </div>
  </div>`;
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
  const SKEY = {gender:"性别",age_group:"年龄段",occupation:"职业",portrait:"人物描述",interests:"兴趣",routine:"作息",communication_style:"沟通风格",emotion_baseline:"情绪基线",social_mode:"社交模式",knowledge:"知识结构",relationship:"关系",taboos:"雷区",memory_anchors:"记忆锚点",recent_focus:"近期关注",content_pref:"内容偏好",nickname_pref:"称呼偏好",interaction_advice:"互动建议"};
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
    ${renderFavorabilityCard(p.favorability, "用户好感度")}
    ${renderQqProfileCard(core, p.user_id)}
    <div class="card"><h2>全局印象</h2>${core && core.profile_text ? `<pre style="white-space:pre-wrap;margin:0;font-family:inherit">${escapeHtml(core.profile_text || '')}</pre>` : '<p class="muted">无全局画像</p>'}</div>
    ${structCard}
    <h3 style="margin-bottom:10px">各群印象（${(p.local_profiles||[]).length}）</h3>
    ${locals || '<p class="muted">无各群画像</p>'}`;
}

function renderPersonaBuilder() {
  const r = state.personaTemplateResult;
  const task = state.personaTemplateTask || {};
  const history = state.personaTemplateHistory || [];
  const sources = (r && r.sources) || [];
  const subagents = (r && r.subagents) || [];
  const sourceCards = sources.map((s, i) => `<div class="persona-source-card">
    <div class="between" style="gap:8px"><span class="tag">S${i + 1}</span><span class="muted">${escapeHtml(s.source || s.kind || "资料")}</span></div>
    <strong>${s.url ? `<a href="${escapeAttr(s.url)}" target="_blank" rel="noreferrer">${escapeHtml(s.title || s.query || s.url)}</a>` : escapeHtml(s.title || s.query || "")}</strong>
    <p>${escapeHtml((s.summary || "").slice(0, 260))}</p>
    ${s.url ? `<code>${escapeHtml(s.url)}</code>` : ""}
  </div>`).join("");
  const listBlock = (items) => (items || []).filter(Boolean).slice(0, 8).map(x => `<li>${escapeHtml(x)}</li>`).join("");
  const agentBlocks = subagents.map(a => {
    const report = a.report || {};
    if (!report || report.raw) {
      return `<details class="persona-agent-card" open>
        <summary>${escapeHtml(a.name || "子agent")} <span class="muted">${escapeHtml(a.focus || "")}</span></summary>
        <pre>${escapeHtml(JSON.stringify(report || a.raw || {}, null, 2))}</pre>
      </details>`;
    }
    const facts = listBlock(report.facts);
    const personality = listBlock(report.personality);
    const relations = listBlock(report.relations);
    const conflicts = listBlock([...(report.conflicts || []), ...(report.unknowns || [])]);
    return `<details class="persona-agent-card" open>
      <summary>${escapeHtml(a.name || "子agent")} <span class="muted">${escapeHtml(a.focus || "")}</span></summary>
      <div class="agent-report-grid">
        <div><h4>事实</h4><ul>${facts || '<li class="muted">无</li>'}</ul></div>
        <div><h4>性格/关系</h4><ul>${personality || relations || '<li class="muted">无</li>'}</ul></div>
        <div><h4>冲突与缺口</h4><ul>${conflicts || '<li class="muted">无</li>'}</ul></div>
      </div>
    </details>`;
  }).join("");
  const valid = r ? r.template_valid === true : false;
  const validationTag = r ? `<span class="tag" style="${valid?'background:rgba(52,211,153,0.18);color:var(--ok)':'background:rgba(248,113,113,0.18);color:var(--danger)'}">${valid?'YAML 有效':'YAML 需修复'}</span>` : "";
  const errors = r ? [...(r.template_errors || []), ...(r.template_warnings || [])] : [];
  const validationList = errors.map(x => `<li>${escapeHtml(x)}</li>`).join("");
  const ref = (r && r.template_reference) || {};
  const taskProgress = Math.max(0, Math.min(100, Number(task.progress || 0)));
  const form = state.personaTemplateForm || {};
  const buildMode = form.mode || "source";
  const modeSwitch = `<div class="toggle persona-builder-mode">
      <button class="${buildMode==='source'?'on':''}" onclick="state.personaTemplateForm.mode='source';render()">作品角色</button>
      <button class="${buildMode==='custom'?'on':''}" onclick="state.personaTemplateForm.mode='custom';render()">自定义描述</button>
    </div>`;
  const sourceForm = `<div class="persona-builder-form">
      <input id="persona-builder-work" type="text" placeholder="作品名" value="${escapeAttr(form.work_title || "")}" oninput="state.personaTemplateForm.work_title=this.value">
      <input id="persona-builder-character" type="text" placeholder="角色名" value="${escapeAttr(form.character_name || "")}" oninput="state.personaTemplateForm.character_name=this.value">
      <button class="btn primary" onclick="buildPersonaTemplate()" ${state.personaTemplateBusy?'disabled':''}>${state.personaTemplateBusy?'<span class="spinner"></span> 构建中…':'开始构建'}</button>
    </div>`;
  const customForm = `<div class="persona-builder-custom">
      <div class="persona-builder-form custom-head">
        <input type="text" placeholder="人设名称" value="${escapeAttr(form.persona_name || "")}" oninput="state.personaTemplateForm.persona_name=this.value">
        <input type="text" placeholder="性别" value="${escapeAttr(form.gender || "")}" oninput="state.personaTemplateForm.gender=this.value">
        <button class="btn primary" onclick="buildPersonaTemplate()" ${state.personaTemplateBusy?'disabled':''}>${state.personaTemplateBusy?'<span class="spinner"></span> 构建中…':'开始构建'}</button>
      </div>
      <div class="persona-builder-form custom-grid">
        <input type="text" placeholder="性格" value="${escapeAttr(form.personality || "")}" oninput="state.personaTemplateForm.personality=this.value">
        <input type="text" placeholder="特点" value="${escapeAttr(form.traits || "")}" oninput="state.personaTemplateForm.traits=this.value">
        <input type="text" placeholder="爱好" value="${escapeAttr(form.hobbies || "")}" oninput="state.personaTemplateForm.hobbies=this.value">
      </div>
      <textarea class="persona-builder-description" placeholder="长文描述：可以直接粘贴你对这个人设的完整设想、说话习惯、背景、禁忌、群聊表现…" oninput="state.personaTemplateForm.description=this.value">${escapeHtml(form.description || "")}</textarea>
    </div>`;
  const progressBlock = state.personaTemplateBusy || task.task_id
    ? `<div class="persona-progress">
        <div class="between" style="gap:10px">
          <strong>${escapeHtml(task.message || "正在准备人设构建...")}</strong>
          <span class="muted">${taskProgress}%</span>
        </div>
        <div class="persona-progress-bar"><div style="width:${taskProgress}%"></div></div>
        <div class="muted" style="font-size:12px;margin-top:6px">阶段：${escapeHtml(task.stage || "-")}</div>
      </div>`
    : "";
  const historyItems = history.map(item => {
    const when = item.created_at ? new Date(item.created_at * 1000).toLocaleString() : "-";
    const valid = item.template_valid ? "YAML 有效" : "需检查";
    return `<div class="persona-history-item">
      <div class="title">
        <strong>${escapeHtml(item.work_title || "")} / ${escapeHtml(item.character_name || "")}</strong>
        <div class="muted" style="font-size:12px">${escapeHtml(when)} · ${escapeHtml(valid)} · ${Number(item.source_count || 0)} 个来源</div>
      </div>
      <button class="btn small" onclick="openPersonaTemplateHistory('${escapeAttr(item.record_id || "")}')">查看</button>
    </div>`;
  }).join("");
  return `<div class="card">
    <h2>自动构建人设模板</h2>
    ${modeSwitch}
    ${buildMode === "custom" ? customForm : sourceForm}
    ${progressBlock}
  </div>
  <div class="card">
    <div class="between" style="gap:10px;flex-wrap:wrap">
      <h2 style="margin:0">构建历史</h2>
      <button class="btn small" onclick="refreshPersonaTemplateHistory()">刷新</button>
    </div>
    <div class="persona-history-list" style="margin-top:12px">${historyItems || '<p class="muted">暂无历史记录。</p>'}</div>
  </div>
  ${r ? `<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">${escapeHtml(r.work_title || "")} / ${escapeHtml(r.character_name || "")}</h2>
      <div>${validationTag}<span class="tag">主模型</span><span class="muted">${Number(r.duration_ms || 0)} ms</span></div>
    </div>
    <div class="row" style="margin-top:10px">
      ${ref.path ? `<span class="muted">参考模板：<code>${escapeHtml(ref.path)}</code></span>` : '<span class="muted">未读取到当前模板参考</span>'}
      ${(r.template_keys || []).map(k => `<span class="tag">${escapeHtml(k)}</span>`).join("")}
    </div>
    ${validationList ? `<div class="alert ${valid?'info':'err'}" style="margin-top:12px"><ul class="validation-list">${validationList}</ul></div>` : ""}
    <div class="between" style="margin:16px 0 8px">
      <h3 style="margin:0">插件 YAML 模板</h3>
      <div class="row">
        <button class="btn small primary" onclick="applyPersonaTemplate()">应用</button>
        <button class="btn small" onclick="copyPersonaTemplate()">复制</button>
      </div>
    </div>
    <pre class="persona-template-code">${escapeHtml(r.template || "")}</pre>
    <h3 style="margin:16px 0 8px">资料来源（${sources.length}）</h3>
    <div class="persona-source-grid">${sourceCards || '<p class="muted">未抓取到资料来源。</p>'}</div>
    <h3 style="margin:16px 0 8px">子agent交叉验证（${subagents.length}）</h3>
    ${agentBlocks || '<p class="muted">暂无子agent报告。</p>'}
  </div>` : ''}`;
}

async function copyPersonaTemplate() {
  const text = state.personaTemplateResult && state.personaTemplateResult.template;
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const el = document.createElement("textarea");
      el.value = text;
      el.setAttribute("readonly", "");
      el.style.position = "fixed";
      el.style.left = "-9999px";
      document.body.appendChild(el);
      el.select();
      document.execCommand("copy");
      document.body.removeChild(el);
    }
    alertFlash("ok", "已复制 YAML 模板");
  } catch (e) {
    alertFlash("err", "复制失败：" + e.message);
  }
}

async function applyPersonaTemplate() {
  const result = state.personaTemplateResult;
  if (!result || !result.template) return;
  if (!confirm("应用后会写入当前人设 YAML 文件，并刷新运行时服务。继续吗？")) return;
  try {
    const recordId = result.history_record && result.history_record.record_id;
    const body = recordId ? { record_id: recordId } : { result };
    const applied = await api("/persona-template/apply", {
      method: "POST",
      headers: {"content-type":"application/json"},
      body: JSON.stringify(body),
    });
    alertFlash("ok", "已应用人设：" + (applied.path || "当前配置"));
  } catch (e) {
    alertFlash("err", "应用失败：" + e.message);
  }
}

async function refreshPersonaTemplateHistory() {
  try {
    const r = await api("/persona-template/history?limit=8");
    state.personaTemplateHistory = r.records || [];
    render();
  } catch (e) {
    alertFlash("err", "读取历史失败：" + e.message);
  }
}

async function openPersonaTemplateHistory(recordId) {
  if (!recordId) return;
  try {
    const record = await api("/persona-template/history/" + encodeURIComponent(recordId));
    state.personaTemplateResult = record.result || null;
    state.personaTemplateTask = null;
    render();
  } catch (e) {
    alertFlash("err", "读取历史失败：" + e.message);
  }
}

async function buildPersonaTemplate() {
  if (state.personaTemplateBusy) return;
  const form = state.personaTemplateForm || {};
  const mode = form.mode || "source";
  const work = (form.work_title || "").trim();
  const character = (form.character_name || "").trim();
  const personaName = (form.persona_name || "").trim();
  if (mode === "custom") {
    const hasDetail = [form.gender, form.personality, form.traits, form.hobbies, form.description].some(v => String(v || "").trim());
    if (!personaName || !hasDetail) { alertFlash("err", "请填写人设名称，并至少补充一项描述"); return; }
  } else if (!work || !character) {
    alertFlash("err", "请填写作品名和角色名"); return;
  }
  state.personaTemplateBusy = true;
  state.personaTemplateResult = null;
  state.personaTemplateTask = { status:"queued", stage:"queued", message:"已加入构建队列...", progress:1 };
  render();
  try {
    const started = await api("/persona-template/build-task", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify(mode === "custom" ? {
        mode: "custom",
        persona_name: personaName,
        gender: form.gender || "",
        personality: form.personality || "",
        traits: form.traits || "",
        hobbies: form.hobbies || "",
        description: form.description || "",
      } : {work_title: work, character_name: character}),
    });
    state.personaTemplateTask = started;
    render();
    let last = started;
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 1200));
      last = await api("/persona-template/tasks/" + encodeURIComponent(started.task_id));
      state.personaTemplateTask = last;
      if (last.status === "done") {
        state.personaTemplateResult = last.result || null;
        alertFlash("ok", "人设模板已生成");
        await refreshPersonaTemplateHistory();
        break;
      }
      if (last.status === "error") {
        throw new Error(last.error || last.message || "构建失败");
      }
      render();
    }
  } catch (e) {
    alertFlash("err", "构建失败：" + e.message);
  }
  state.personaTemplateBusy = false; render();
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
      <td>${renderFavorabilityBadge(g.favorability)}</td>
      <td><button class="btn small" onclick="openGroup('${escapeAttr(g.group_id)}')">查看</button></td>
    </tr>`;
  }).join("");
  return `<div class="card"><h2>群列表（${state.groupList.length}）</h2>
    <p class="muted" style="font-size:12px;margin-top:0">同时显示已建立记忆的群和白名单中的群（包括关闭搜索可找到的群）。</p>
    <table><thead><tr><th style="width:40px"></th><th>群号</th><th>群名</th><th>群好感</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="muted">暂无群数据</td></tr>'}</tbody></table></div>`;
}

async function openGroup(gid) {
  try {
    state.selectedGroup = gid;
    state.groupRawChat = null;
    state.groupAliasDrafts = {};
    const [personas, style, knowledge, memes, agentState, schedule] = await Promise.all([
      api("/groups/" + encodeURIComponent(gid) + "/personas"),
      api("/groups/" + encodeURIComponent(gid) + "/style"),
      api("/groups/" + encodeURIComponent(gid) + "/knowledge").catch(() => ({knowledge: [], autobuild_status: null})),
      api("/groups/" + encodeURIComponent(gid) + "/memes").catch(() => ({memes: []})),
      api("/groups/" + encodeURIComponent(gid) + "/agent-state").catch(() => null),
      api("/groups/" + encodeURIComponent(gid) + "/schedule").catch(() => null),
    ]);
    state.groupPersonas = personas.profiles;
    state.groupFavorability = personas.group_favorability || null;
    state.groupStyle = style;
    state.groupKnowledge = knowledge.knowledge || [];
    state.groupKnowledgeAutobuild = knowledge.autobuild_status || null;
    state.groupMemes = memes.memes || [];
    state.groupAgentState = agentState;
    state.groupSchedule = schedule;
    render();
  } catch (e) { alertFlash("err", e.message); }
}

function splitAliasInput(raw) {
  return String(raw || "").split(/[\n,，、;；|/]+/).map(x => x.trim()).filter(Boolean);
}

function getAliasDraft(uid, p) {
  const key = String(uid || "");
  const current = state.groupAliasDrafts && state.groupAliasDrafts[key];
  if (current) return current;
  return {
    aliasesText: (p.aliases || []).join("、"),
    note: p.alias_note || "",
  };
}

function setGroupAliasDraft(uid, field, value) {
  const key = String(uid || "");
  state.groupAliasDrafts = state.groupAliasDrafts || {};
  const current = state.groupAliasDrafts[key] || { aliasesText: "", note: "" };
  state.groupAliasDrafts[key] = { ...current, [field]: value };
}

async function refreshGroupDetailLight() {
  const gid = state.selectedGroup;
  if (!gid) return;
  const [personas, agentState] = await Promise.all([
    api("/groups/" + encodeURIComponent(gid) + "/personas"),
    api("/groups/" + encodeURIComponent(gid) + "/agent-state").catch(() => state.groupAgentState),
  ]);
  state.groupPersonas = personas.profiles || [];
  state.groupFavorability = personas.group_favorability || state.groupFavorability;
  state.groupAgentState = agentState;
}

async function saveGroupMemberAliases(uid) {
  const gid = state.selectedGroup;
  if (!gid || !uid) return;
  const draft = getAliasDraft(uid, {});
  try {
    await api("/groups/" + encodeURIComponent(gid) + "/aliases/" + encodeURIComponent(uid), {
      method: "PUT",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({ aliases: splitAliasInput(draft.aliasesText), note: draft.note || "" }),
    });
    if (state.groupAliasDrafts) delete state.groupAliasDrafts[String(uid)];
    await refreshGroupDetailLight();
    alertFlash("ok", "已保存群成员外号");
    render();
  } catch (e) { alertFlash("err", "保存外号失败：" + e.message); }
}

async function clearGroupMemberAliases(uid) {
  const gid = state.selectedGroup;
  if (!gid || !uid) return;
  if (!confirm("清空该成员在本群的外号映射？")) return;
  try {
    await api("/groups/" + encodeURIComponent(gid) + "/aliases/" + encodeURIComponent(uid), { method: "DELETE" });
    if (state.groupAliasDrafts) delete state.groupAliasDrafts[String(uid)];
    await refreshGroupDetailLight();
    alertFlash("ok", "已清空群成员外号");
    render();
  } catch (e) { alertFlash("err", "清空外号失败：" + e.message); }
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

function renderGroupRelationGraph(edges) {
  const list = Array.isArray(edges) ? edges.slice(0, 24) : [];
  if (!list.length) return '<p class="muted" style="margin:6px 0 0">暂无可绘制的群员关系图</p>';
  const nodeMap = new Map();
  for (const e of list) {
    for (const side of ["src", "dst"]) {
      const id = String(e[side] || "");
      if (!id) continue;
      const label = String(e[side + "_label"] || id);
      const current = nodeMap.get(id) || { id, label, weight: 0 };
      current.weight += Number(e.weight || 0);
      if (label && label !== id) current.label = label;
      nodeMap.set(id, current);
    }
  }
  const nodes = Array.from(nodeMap.values()).slice(0, 16);
  const centerX = 260, centerY = 160, radius = nodes.length <= 6 ? 102 : 122;
  nodes.forEach((n, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i / Math.max(1, nodes.length));
    n.x = centerX + Math.cos(angle) * radius;
    n.y = centerY + Math.sin(angle) * radius;
  });
  const pos = new Map(nodes.map(n => [n.id, n]));
  const colorFor = (kind) => ({reply:"#6aa8ff",quote:"#9775fa",mention:"#20c997",turn:"#ffb020",repeat:"#f87171",co_topic:"#34d399"})[kind] || "#8a91a3";
  const edgeLines = list.map(e => {
    const a = pos.get(String(e.src || ""));
    const b = pos.get(String(e.dst || ""));
    if (!a || !b) return "";
    const w = Math.max(1.2, Math.min(5, 1 + Number(e.weight || 0) * 0.35));
    return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke="${colorFor(e.kind)}" stroke-width="${w.toFixed(1)}" opacity="0.58">
      <title>${escapeHtml(a.label)} → ${escapeHtml(b.label)} · ${escapeHtml(e.kind || "relation")} · ${Number(e.weight || 0).toFixed(2)}</title>
    </line>`;
  }).join("");
  const nodeSvg = nodes.map(n => {
    const r = Math.max(15, Math.min(25, 13 + Math.sqrt(Math.max(0, n.weight || 0)) * 3));
    const label = String(n.label || n.id);
    const short = label.length > 7 ? label.slice(0, 7) + "…" : label;
    return `<g class="relation-node" transform="translate(${n.x.toFixed(1)} ${n.y.toFixed(1)})">
      <circle r="${r.toFixed(1)}"></circle>
      <text text-anchor="middle" dominant-baseline="central">${escapeHtml(short)}</text>
      <title>${escapeHtml(label)} (${escapeHtml(n.id)})</title>
    </g>`;
  }).join("");
  return `<div class="relation-graph">
    <svg viewBox="0 0 520 320" role="img" aria-label="群员关系图">
      <rect x="1" y="1" width="518" height="318" rx="8"></rect>
      <g class="relation-edges">${edgeLines}</g>
      <g>${nodeSvg}</g>
    </svg>
  </div>`;
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
          <td>
            <code>${escapeHtml(e.src)}</code>${e.src_label && e.src_label !== e.src ? ` <span class="muted">${escapeHtml(e.src_label)}</span>` : ''}
            →
            <code>${escapeHtml(e.dst)}</code>${e.dst_label && e.dst_label !== e.dst ? ` <span class="muted">${escapeHtml(e.dst_label)}</span>` : ''}
          </td>
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
    <h3 style="margin:12px 0 8px">群员关系图</h3>
    ${renderGroupRelationGraph(edges)}
    <details style="margin-top:8px"><summary class="muted" style="cursor:pointer">显著记忆 Top-${memories.length}</summary>${memBlock}</details>
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

function renderGroupScheduleCard() {
  const s = state.groupSchedule || { enabled:false, schedule_prompt:"" };
  const enabled = !!s.enabled;
  const generating = !!state.groupScheduleGenerating;
  return `<div class="card">
    <div class="between" style="gap:10px;flex-wrap:wrap">
      <h2 style="margin:0">群作息表</h2>
      <div class="toggle">
        <button class="${enabled?'on':''}" onclick="saveGroupSchedule(true)">开</button>
        <button class="${!enabled?'on':''}" onclick="saveGroupSchedule(false)">关</button>
      </div>
    </div>
    <p class="muted" style="font-size:12px;margin:4px 0 10px">默认关闭且不内置硬编码作息；开启后只把下方内容作为轻量背景。</p>
    <textarea id="group-schedule-text" class="group-schedule-text" placeholder="留空则只提供当前时间，不自动推断上课/上班/睡觉。">${escapeHtml(s.schedule_prompt || "")}</textarea>
    <div class="row" style="margin-top:8px">
      <button class="btn small primary" onclick="saveGroupSchedule(${enabled ? "true" : "false"})">保存作息</button>
      <button class="btn small" onclick="autoGenerateGroupSchedule()" ${generating?'disabled':''}>${generating?'生成中…':'按人设自动生成'}</button>
    </div>
  </div>`;
}

async function saveGroupSchedule(enabled) {
  const gid = state.selectedGroup;
  if (!gid) return;
  const text = document.getElementById("group-schedule-text")?.value || "";
  try {
    const out = await api("/groups/" + encodeURIComponent(gid) + "/schedule", {
      method:"PUT",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ enabled: !!enabled, schedule_prompt: text }),
    });
    state.groupSchedule = out;
    alertFlash("ok", "群作息已保存");
    render();
  } catch (e) { alertFlash("err", "保存作息失败：" + e.message); }
}

async function autoGenerateGroupSchedule() {
  const gid = state.selectedGroup;
  if (!gid || state.groupScheduleGenerating) return;
  state.groupScheduleGenerating = true; render();
  try {
    const out = await api("/groups/" + encodeURIComponent(gid) + "/schedule/auto-generate", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body: "{}",
    });
    const saved = await api("/groups/" + encodeURIComponent(gid) + "/schedule", {
      method:"PUT",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ enabled: true, schedule_prompt: out.schedule_prompt || "" }),
    });
    state.groupSchedule = saved;
    alertFlash("ok", "已自动生成并启用群作息");
  } catch (e) { alertFlash("err", "自动生成作息失败：" + e.message); }
  state.groupScheduleGenerating = false; render();
}

function renderMemberAliasEditor(p) {
  const draft = getAliasDraft(p.user_id, p);
  const names = (p.known_names || []).filter(Boolean);
  const nameTags = names.length
    ? `<div class="member-known-names">${names.slice(0, 6).map(n => `<span class="tag">${escapeHtml(n)}</span>`).join("")}</div>`
    : '<div class="muted" style="font-size:12px">暂无称呼候选</div>';
  const hasSaved = (p.aliases || []).length || p.alias_note;
  return `<div class="member-alias-editor">
    <div class="member-alias-title">${escapeHtml(p.nickname || names[0] || "") || '<span class="muted">无昵称</span>'}</div>
    ${nameTags}
    <input type="text" placeholder="外号，如：老王、车神" value="${escapeAttr(draft.aliasesText || "")}" oninput="setGroupAliasDraft('${escapeAttr(p.user_id)}','aliasesText',this.value)">
    <input type="text" placeholder="备注（可选）" value="${escapeAttr(draft.note || "")}" oninput="setGroupAliasDraft('${escapeAttr(p.user_id)}','note',this.value)">
    <div class="member-alias-actions">
      <button class="btn small primary" onclick="saveGroupMemberAliases('${escapeAttr(p.user_id)}')">保存</button>
      ${hasSaved ? `<button class="btn small" onclick="clearGroupMemberAliases('${escapeAttr(p.user_id)}')">清空</button>` : ''}
    </div>
  </div>`;
}

function renderMemberRelationDigest(p) {
  const edges = p.relationship_edges || [];
  const edgeLines = edges.slice(0, 4).map(e => {
    const dir = e.direction === 'out' ? '常接' : '常被接';
    return `<div><span class="tag">${escapeHtml(dir)}</span> ${escapeHtml(e.peer_label || e.peer_user_id || '')} <span class="muted">${escapeHtml(e.kind || '')}/${Number(e.weight||0).toFixed(2)}</span></div>`;
  }).join("");
  const profile = p.snippet ? `<div class="member-profile-snippet">${escapeHtml(p.snippet)}</div>` : '<div class="muted">暂无画像摘要</div>';
  return `<div class="member-relation-digest">
    ${edgeLines || '<div class="muted">暂无显著关系边</div>'}
    ${profile}
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
      <td>${renderMemberAliasEditor(p)}</td>
      <td>${renderFavorabilityBadge(p.favorability)}</td>
      <td>${renderMemberRelationDigest(p)}</td>
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
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedGroup=null;state.groupRawChat=null;state.groupFavorability=null;state.groupStyleSnapIdx=0;state.groupAliasDrafts={};render()">返回列表</button><span class="muted">群 ${escapeHtml(gid)}</span></div>
    ${renderFavorabilityCard(state.groupFavorability, "群好感度")}
    ${renderGroupAgentState()}
    ${renderGroupScheduleCard()}
    ${renderGroupStyle(style)}
    ${renderGroupKnowledgeCard()}
    <div class="card"><h2>梗词典 / 概念锚点（${(state.groupMemes||[]).length}）</h2>
      <p class="muted" style="font-size:12px;margin-top:0">词条会持久保留；列表只是当前读取视图，不会因为数量变多自动清理旧梗。</p>
      ${memeRows ? `<table><thead><tr><th>词条</th><th>含义</th><th>别名</th><th>范围/风险/置信度</th></tr></thead><tbody>${memeRows}</tbody></table>` : '<p class="muted">暂无匹配词条，公共热梗种子会在首次查询后自动初始化。</p>'}</div>
    <div class="card"><h2>群内成员理解（${state.groupPersonas.length}）</h2>
      <table class="group-member-understanding"><thead><tr><th style="width:40px"></th><th>QQ</th><th>称呼 / 外号</th><th>好感度</th><th>关系与画像</th><th>近期情绪</th><th>更新</th></tr></thead><tbody>${rows||'<tr><td colspan="7" class="muted">无</td></tr>'}</tbody></table></div>
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
