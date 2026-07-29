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
      <td class="dashboard-model-cell col-model" title="${escapeAttr(row.model || "unknown")}">${escapeHtml(row.model || "unknown")}</td>
      <td class="col-number u-atomic u-tabular">${Number(row.call_count || 0).toLocaleString()}</td>
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
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="模型用量"><table class="dashboard-model-table data-table compact">
      <thead><tr><th scope="col" class="col-model">模型名</th><th scope="col" class="col-number">请求次数</th><th scope="col" class="col-number">令牌消耗</th></tr></thead>
      <tbody>${body || '<tr><td colspan="3" class="muted">暂无模型用量。</td></tr>'}</tbody>
    </table></div>
  </div>`;
}

function renderDashboardPurposeUsage(rows) {
  const data = (rows || []).slice(0, 16);
  const body = data.map(row => {
    const width = Math.max(1.5, Math.min(100, Number(row.relative_width || 0) * 100));
    const label = row.purpose_label || row.purpose || "unknown";
    const title = row.purpose || label;
    return `<tr>
      <td class="dashboard-model-cell col-model" title="${escapeAttr(title)}">${escapeHtml(label)}</td>
      <td class="col-number u-atomic u-tabular">${Number(row.call_count || 0).toLocaleString()}</td>
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
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="功能用量"><table class="dashboard-model-table data-table compact">
      <thead><tr><th scope="col" class="col-model">功能</th><th scope="col" class="col-number">请求次数</th><th scope="col" class="col-number">令牌消耗</th></tr></thead>
      <tbody>${body || '<tr><td colspan="3" class="muted">暂无功能用量。</td></tr>'}</tbody>
    </table></div>
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
      <td class="col-time u-atomic u-tabular">${escapeHtml(bucket)}</td>
      <td class="col-time u-atomic">${escapeHtml(row.label || "")}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.call_count || 0)}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.prompt_tokens || 0)}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.completion_tokens || 0)}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.total_tokens || 0)}</td>
      ${valueKey !== "total_tokens" ? `<td class="col-number u-atomic u-tabular">${dashboardFullNumber(row[valueKey] || 0)}</td>` : ""}
    </tr>`;
  }).join("");
  return `<div class="table-wrap table-scroll dashboard-detail-table" tabindex="0" role="region" aria-label="令牌曲线明细">
    <table class="data-table wide">
      <thead><tr><th scope="col" class="col-time">时间桶</th><th scope="col" class="col-time">标签</th><th scope="col" class="col-number">请求</th><th scope="col" class="col-number">提示词</th><th scope="col" class="col-number">回复</th><th scope="col" class="col-number">总计</th>${valueKey !== "total_tokens" ? '<th scope="col" class="col-number">曲线值</th>' : ""}</tr></thead>
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
      <td class="col-model"><span class="u-ellipsis" title="${escapeAttr(dashboardGroupLabel(row))}">${escapeHtml(dashboardGroupLabel(row))}</span></td>
      <td class="col-id u-atomic u-tabular">${escapeHtml(groupId || "-")}</td>
      <td class="col-number u-atomic u-tabular">${escapeHtml(dashboardPercent(pct))}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.call_count || 0)}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.prompt_tokens || 0)}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.completion_tokens || 0)}</td>
      <td class="col-number u-atomic u-tabular">${dashboardFullNumber(row.total_tokens || 0)}</td>
    </tr>`;
  }).join("");
  return `<div class="table-wrap table-scroll dashboard-detail-table" tabindex="0" role="region" aria-label="群令牌消耗明细">
    <table class="data-table wide">
      <thead><tr><th scope="col" class="col-model">群</th><th scope="col" class="col-id">群号</th><th scope="col" class="col-number">占比</th><th scope="col" class="col-number">请求</th><th scope="col" class="col-number">提示词</th><th scope="col" class="col-number">回复</th><th scope="col" class="col-number">总计</th></tr></thead>
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
      <a href="#logs" onclick="navigateToView('logs');return false">查看日志 →</a>
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
      <td class="col-model"><span class="dot ${status.cls}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px"></span><span class="u-clamp-2">${escapeHtml(st.label || st.key || "-")}</span></td>
      <td class="col-status"><code class="u-atomic" style="font-size:11px">${escapeHtml(st.status || "info")}</code></td>
      <td class="col-description u-pre-wrap">${escapeHtml(st.detail || "")}</td>
      <td class="col-description u-pre-wrap">${escapeHtml(st.hint || "")}</td>
    </tr>`;
  }).join("");
  const last = ir.last_trace || {};
  const traceSummary = last && (last.outcome || last.diagnosis_code)
    ? `<p class="muted" style="font-size:12px;margin:8px 0 0">链路收口：${escapeHtml(last.outcome || "-")} / ${escapeHtml(last.diagnosis_code || "-")}</p>`
    : "";
  return `<div style="margin-top:10px">
    <div class="alert ${alertCls}" style="white-space:pre-wrap">${escapeHtml(ir.detail || "")}${escapeHtml(reply)}</div>
    <div class="row" style="margin:6px 0 10px">
      ${meta.map(x => `<span class="tag tag--ellipsis" title="${escapeAttr(x)}">${escapeHtml(x)}</span>`).join("")}
      ${traceBtn}
    </div>
    ${traceSummary}
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="实际交互测试阶段"><table class="data-table wide" style="margin-top:10px"><thead><tr><th scope="col" class="col-model">阶段</th><th scope="col" class="col-status">状态</th><th scope="col" class="col-description">详情</th><th scope="col" class="col-description">建议</th></tr></thead>
      <tbody>${stages || '<tr><td colspan="4" class="muted">无分层诊断信息</td></tr>'}</tbody></table></div>
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
      ${feed.owner_uin ? `<span class="tag tag--status u-tabular">owner=${escapeHtml(feed.owner_uin)}</span>` : ""}
      ${feed.feed_id ? `<span class="tag tag--ellipsis" title="feed=${escapeAttr(feed.feed_id)}">feed=${escapeHtml(feed.feed_id)}</span>` : ""}
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
  ${renderAdminOperations("health","功能体检操作诊断")}
  ${interactionCard}
  ${qzoneForwardCard}
  <div class="health-grid">${cats}</div>`;
}

async function refreshHealth() {
  state.loading = true; render();
  try {
    state.health = await api("/health/check?refresh=true");
    const diagnostic = rememberAdminOperation("health", state.health, "功能体检刷新未完成");
    alertFlash("ok", diagnostic?.title || "功能体检已刷新");
  } catch (e) {
    const diagnostic = rememberAdminOperation("health", e, "功能体检刷新未完成");
    alertFlash("err", diagnostic?.title || "功能体检刷新未完成");
  }
  state.loading = false; render();
}

function qqRememberDiagnostic(value, fallbackTitle="QQ 操作未完成") {
  const diagnostic = value && value.diagnostic && typeof value.diagnostic === "object"
    ? value.diagnostic
    : (value instanceof Error ? operationDiagnosticFromError(value, fallbackTitle) : value);
  if (!diagnostic || typeof diagnostic !== "object") return null;
  state.qqDiagnostics = [diagnostic, ...(Array.isArray(state.qqDiagnostics) ? state.qqDiagnostics : [])].slice(0, 6);
  return diagnostic;
}

function qqSelectedBotId() {
  return String(state.qqBotId || document.getElementById("qq-bot-id")?.value || "").trim();
}

function qqClearDiagnostics() {
  state.qqDiagnostics = [];
  render();
}

function renderQQ() {
  const info = state.qqInfo || {};
  const groups = state.qqGroups || [];
  const friends = state.qqFriends || [];
  const bots = (info.bots || []).map(item => String(item.bot_id || "")).filter(Boolean);
  const selectedBotId = bots.includes(String(state.qqBotId || "")) ? String(state.qqBotId) : (bots[0] || "");
  state.qqBotId = selectedBotId;
  const botOptions = bots.map(id => `<option value="${escapeAttr(id)}" ${id===selectedBotId?'selected':''}>${escapeHtml(id)}</option>`).join("");
  const infoCard = info.error
    ? `<div class="card"><div class="alert err">获取账号信息失败：${escapeHtml(info.error)}</div></div>`
    : `<div class="card">
        <h2>当前账号</h2>
        <div class="row"><span class="muted">QQ</span> <code class="u-atomic u-tabular">${escapeHtml(info.user_id||'')}</code>
          <span class="muted">昵称</span> <b class="u-clamp-2" title="${escapeAttr(info.nickname || '')}">${escapeHtml(info.nickname||'')}</b></div>
        <label class="field-input" style="margin-top:12px"><span>目标 Bot</span><select id="qq-bot-id" onchange="state.qqBotId=this.value;render()">${botOptions}</select></label>
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
  const groupRows = groups.map(g => {
    const memberships = Array.isArray(g.bot_self_ids) ? g.bot_self_ids.map(String) : [];
    const canLeave = Boolean(selectedBotId) && memberships.includes(selectedBotId);
    return `<tr>
      <td class="col-model"><span class="u-clamp-2" title="${escapeAttr(g.group_name || '')}">${escapeHtml(g.group_name||'')}</span><code class="u-atomic u-tabular">${escapeHtml(g.group_id)}</code></td>
      <td class="col-number u-atomic u-tabular">${g.member_count}/${g.max_member_count||'-'}</td>
      <td class="col-actions"><button class="btn small danger qq-leave-group" aria-label="退出群 ${escapeAttr(g.group_name || g.group_id)}" data-group-id="${escapeAttr(g.group_id)}" data-group-name="${escapeAttr(g.group_name||'')}" ${canLeave?'':'disabled title="所选 Bot 不在该群的已确认 membership 中"'}>退群</button></td>
    </tr>`;
  }).join("");
  const friendRows = friends.map(f => `<tr>
      <td class="col-model"><span class="u-clamp-2" title="${escapeAttr(f.remark || f.nickname || '')}">${escapeHtml(f.remark||f.nickname||'')}</span><code class="u-atomic u-tabular">${escapeHtml(f.user_id)}</code></td>
      <td class="col-actions"><button class="btn small danger" aria-label="删除好友 ${escapeAttr(f.remark || f.nickname || f.user_id)}" onclick="qqDeleteFriend('${escapeAttr(f.user_id)}','${escapeAttr(f.remark||f.nickname||'')}')">删好友</button></td>
    </tr>`).join("");
  const diagnostics = renderOperationHistory(Array.isArray(state.qqDiagnostics) ? state.qqDiagnostics : [], {group:`view-${state.view}`});
  const diagnosticCard = diagnostics ? `<div class="card"><div class="between"><h2>QQ 操作诊断</h2><button class="btn small" onclick="qqClearDiagnostics()">清空</button></div>${diagnostics}</div>` : "";
  return `${infoCard}
    ${diagnosticCard}
    <div class="card"><h2>群列表（${groups.length}）</h2>
      <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="QQ 群列表"><table class="data-table compact"><thead><tr><th scope="col" class="col-model">群</th><th scope="col" class="col-number">人数</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${groupRows||'<tr><td colspan="3" class="muted">无</td></tr>'}</tbody></table></div>
    </div>
    <div class="card"><h2>好友列表（${friends.length}）</h2>
      <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="QQ 好友列表"><table class="data-table compact"><thead><tr><th scope="col" class="col-model">好友</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${friendRows||'<tr><td colspan="2" class="muted">无</td></tr>'}</tbody></table></div>
    </div>`;
}

async function qqSetNickname() {
  const v = (document.getElementById("qq-nick")?.value||"").trim();
  if (!v || !confirm("确认修改 bot 昵称为：" + v + " ？")) return;
  try { const result=await api("/qq/nickname", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({nickname:v})}); const d=qqRememberDiagnostic(result); alertFlash("ok",d?.title||"已修改"); await loadView(); render(); }
  catch (e) { const d=qqRememberDiagnostic(e,"QQ 昵称修改失败"); alertFlash("err",d?.title||"QQ 昵称修改失败"); }
}
async function qqSetSignature() {
  const v = (document.getElementById("qq-sign")?.value||"").trim();
  if (!confirm("确认修改签名？")) return;
  const botId=qqSelectedBotId();
  try { const result=await api("/qq/signature", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:botId,signature:v})}); const d=qqRememberDiagnostic(result); alertFlash("ok",d?.title||"已修改"); }
  catch (e) { const d=qqRememberDiagnostic(e,"QQ 签名修改失败"); alertFlash("err",d?.title||"QQ 签名修改失败"); }
}
async function qqSetAvatar() {
  const v = (document.getElementById("qq-avatar")?.value||"").trim();
  if (!v || !confirm("确认修改 bot 头像？")) return;
  const botId=qqSelectedBotId();
  try { const result=await api("/qq/avatar", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:botId,file:v})}); const d=qqRememberDiagnostic(result); alertFlash("ok",d?.title||"已修改"); }
  catch (e) { const d=qqRememberDiagnostic(e,"QQ 头像修改失败"); alertFlash("err",d?.title||"QQ 头像修改失败"); }
}
async function qqLeaveGroup(gid, name) {
  const group=state.qqGroups.find(item=>String(item.group_id)===String(gid));
  const memberships=((group&&group.bot_self_ids)||[]).map(String);
  const botId=qqSelectedBotId();
  if(!botId||!memberships.includes(botId)){
    const d=qqRememberDiagnostic({ok:false,code:"qq_membership_unconfirmed",phase:"membership_check",title:"无法确认目标 Bot 的群 membership",message:"所选 Bot 不在该群的已确认 membership 中。",details:[{label:"目标 Bot",value:botId||"未指定",status:"error"},{label:"目标群",value:String(gid),status:"info"}],steps:[{key:"membership_check",label:"检查群 membership",status:"error",message:"未通过服务端操作前约束。",details:[]}],suggestion:"选择已确认属于该群的在线 Bot 后再试。",retryable:false});
    alertFlash("err",d.title);return;
  }
  if (!confirm("确认让 bot 退出群「" + (name||gid) + "」？此操作不可撤销。")) return;
  try { const result=await api("/qq/groups/"+encodeURIComponent(gid)+"/leave", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:botId,confirm:String(gid),is_dismiss:false})}); const d=qqRememberDiagnostic(result); alertFlash("ok",d?.title||"已退群"); await loadView(); render(); }
  catch (e) { const d=qqRememberDiagnostic(e,"退出 QQ 群失败"); alertFlash("err",d?.title||"退出 QQ 群失败"); }
}
async function qqDeleteFriend(uid, name) {
  if (!confirm("确认删除好友「" + (name||uid) + "」？")) return;
  try { const result=await api("/qq/friends/"+encodeURIComponent(uid), {method:"DELETE",headers:{"content-type":"application/json"},body:JSON.stringify({confirm:String(uid)})}); const d=qqRememberDiagnostic(result); alertFlash("ok",d?.title||"已删除"); await loadView(); render(); }
  catch (e) { const d=qqRememberDiagnostic(e,"删除 QQ 好友失败"); alertFlash("err",d?.title||"删除 QQ 好友失败"); }
}

async function recheckCategory(name) {
  state.healthBusyCat = name; render();
  try {
    const r = await api("/health/check?only=" + encodeURIComponent(name));
    const diagnostic = rememberAdminOperation("health", r, "功能分类重测未完成");
    const fresh = (r.categories || [])[0];
    if (fresh && state.health) {
      state.health.categories = state.health.categories.map(c => c.name === name ? fresh : c);
      // 重算汇总
      const sum = {ok:0,warn:0,error:0,disabled:0,info:0};
      state.health.categories.forEach(c => (c.checks||[]).forEach(it => { sum[it.status] = (sum[it.status]||0)+1; }));
      state.health.summary = sum;
      state.health.overall = sum.error ? 'error' : (sum.warn ? 'warn' : 'ok');
    }
    alertFlash("ok", diagnostic?.title || "功能分类重测已完成");
  } catch (e) {
    const diagnostic = rememberAdminOperation("health", e, "功能分类重测未完成");
    alertFlash("err", diagnostic?.title || "功能分类重测未完成");
  }
  state.healthBusyCat = ""; render();
}

async function runInteraction(target) {
  if (state.interactionBusy) return;
  state.interactionBusy = true; state.interactionResult = null; render();
  try {
    state.interactionResult = await api("/health/interaction-test", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ target }) });
    const diagnostic = rememberAdminOperation("health", state.interactionResult, "实际交互测试未完成");
    alertFlash(state.interactionResult.replied ? "ok" : (state.interactionResult.outcome_unknown ? "info" : "err"), diagnostic?.title || "实际交互测试已结束");
  } catch (e) {
    state.interactionResult = operationDiagnosticFromError(e, "实际交互测试未完成");
    const diagnostic = rememberAdminOperation("health", state.interactionResult, "实际交互测试未完成");
    alertFlash("err", diagnostic?.title || "实际交互测试未完成");
  }
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
  if (!state.qzoneForwardOperationId) state.qzoneForwardOperationId = (globalThis.crypto&&globalThis.crypto.randomUUID ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
  render();
  try {
    state.qzoneForwardResult = await api("/health/qzone-forward-test", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ target_user_id: target, forward_text: forwardText, operation_id: state.qzoneForwardOperationId }),
    });
    const diagnostic = rememberAdminOperation("health", state.qzoneForwardResult, "QZone 转发测试未完成");
    if (!state.qzoneForwardResult.outcome_unknown && state.qzoneForwardResult.code !== "qzone_forward_in_progress") state.qzoneForwardOperationId = "";
    alertFlash(state.qzoneForwardResult.ok ? "ok" : (state.qzoneForwardResult.outcome_unknown ? "info" : "err"), diagnostic?.title || "QZone 转发测试已结束");
  } catch (e) {
    const serverDiagnostic = e && e.diagnostic && typeof e.diagnostic === "object";
    state.qzoneForwardResult = operationDiagnosticFromError(e, "QZone 转发测试未完成");
    if (!serverDiagnostic) {
      state.qzoneForwardResult = {
        ...state.qzoneForwardResult,
        code:"qzone_forward_request_outcome_unknown",
        phase:"request",
        title:"QZone 转发请求结果未知",
        message:"浏览器没有收到服务器的明确结果，转发可能已经发生。",
        suggestion:"保留当前 Operation ID，先检查 Bot 的 QQ 空间；确认状态前不要重复提交。",
        retryable:false,
        outcome_unknown:true,
        operation_id:state.qzoneForwardOperationId,
      };
    }
    const diagnostic = rememberAdminOperation("health", state.qzoneForwardResult, "QZone 转发测试未完成");
    if (!state.qzoneForwardResult.outcome_unknown) state.qzoneForwardOperationId = "";
    alertFlash(state.qzoneForwardResult.outcome_unknown ? "info" : "err", diagnostic?.title || "QZone 转发测试未完成");
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

function renderPersonas() {
  if (state.personasAvailable === false) return `<div class="card muted">profile_service 未就绪</div>`;
  if (state.selectedPersona) return renderPersonaDetail();
  const rows = state.personas.map(p => `<tr>
    <td class="col-avatar"><img class="avatar" src="${escapeAttr(p.avatar_url || `https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(p.user_id)}&spec=100`)}" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
    <td class="col-id"><code class="u-atomic u-tabular">${escapeHtml(p.user_id)}</code></td>
    <td class="col-model"><span class="u-clamp-2" title="${escapeAttr(p.nickname || '')}">${escapeHtml(p.nickname || '')}</span></td>
    <td class="col-status">${renderFavorabilityBadge(p.favorability)}</td>
    <td class="col-summary u-wrap">${escapeHtml(p.snippet)}</td>
    <td class="col-date u-atomic u-tabular">${p.updated_at ? new Date(p.updated_at*1000).toLocaleDateString() : '-'}</td>
    <td class="col-actions"><button class="btn small" aria-label="查看 QQ ${escapeAttr(p.user_id)} 的用户画像" onclick="openPersona('${escapeAttr(p.user_id)}')">详情</button></td>
  </tr>`).join("");
  return `<div class="card"><h2>用户画像（${state.personas.length}）</h2>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户画像列表"><table class="data-table xwide"><thead><tr><th scope="col" class="col-avatar"><span class="sr-only">头像</span></th><th scope="col" class="col-id">QQ</th><th scope="col" class="col-model">昵称</th><th scope="col" class="col-status">好感度</th><th scope="col" class="col-summary">摘要</th><th scope="col" class="col-date">更新</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows||'<tr><td colspan="7" class="muted">暂无画像</td></tr>'}</tbody></table></div></div>`;
}

function favorabilityScoreText(fav) {
  if (!fav || fav.available === false) return "不可用";
  if (fav.enabled === false) return fav.exists === false ? "已关闭 · 默认未建档" : "已关闭";
  const score = Number(fav.score || 0);
  return `${score.toFixed(2)}${fav.level ? " · " + fav.level : ""}${fav.exists === false ? " · 默认" : ""}`;
}

function renderFavorabilityBadge(fav) {
  if (!fav || fav.available === false) return '<span class="muted">—</span>';
  const score = Number(fav.score || 0);
  let style = 'background:rgba(106,168,255,0.18);color:var(--accent)';
  if (fav.enabled === false) style = 'background:var(--tag-bg);color:var(--muted)';
  else if (score >= 85) style = 'background:rgba(52,211,153,0.18);color:var(--ok)';
  else if (score < 20) style = 'background:rgba(245,158,11,0.16);color:var(--warn)';
  if (fav.is_perm_blacklisted) style = 'background:rgba(248,113,113,0.16);color:var(--danger)';
  const text = favorabilityScoreText(fav);
  const stateHint = fav.exists === false ? "虚拟默认值，尚未创建档案" : (fav.enabled === false ? "功能已关闭" : "已持久化档案");
  return `<span class="tag favorability-badge u-tabular" style="${style}" title="好感度 ${escapeAttr(text)}；${escapeAttr(stateHint)}">${escapeHtml(text)}</span>`;
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
      <td class="col-time u-atomic u-tabular">${escapeHtml(when)}</td>
      <td class="col-summary u-wrap">${escapeHtml(e.label || "其他好感事件")}</td>
      <td class="col-number u-atomic u-tabular" style="color:${color};font-weight:600">${escapeHtml(deltaText)}</td>
      <td class="col-status"><span class="tag tag--status">${escapeHtml(e.status_label || "")}</span></td>
      <td class="col-description u-wrap">${escapeHtml(e.reason || "")}</td>
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
    ${fav.exists === false ? `<p class="muted">${fav.enabled === false ? '好感度功能当前已关闭；' : ''}下方展示配置中的虚拟默认值，尚未持久化；浏览此页面不会创建好感度档案。</p>` : (fav.enabled === false ? '<p class="muted">好感度功能当前已关闭，不会记录新的关系事件。</p>' : '')}
    <div class="row" style="gap:24px;margin-top:12px">
      <div><div class="muted">${fav.exists === false ? '默认分值（未建档）' : '当前分值'}</div><div class="u-atomic u-tabular" style="font-size:22px;font-weight:700">${Number(fav.score || 0).toFixed(2)}</div></div>
      <div><div class="muted">等级</div><div class="u-atomic" style="font-size:18px">${escapeHtml(fav.level || "—")}</div></div>
      <div><div class="muted">今日加分</div><div class="u-atomic u-tabular">${Number(fav.daily_positive_count || 0).toFixed(2)}</div></div>
      <div><div class="muted">今日扣分</div><div class="u-atomic u-tabular">${Number(fav.daily_negative_count || 0).toFixed(2)}</div></div>
      <div><div class="muted">最近事件</div><div>${escapeHtml(lastLine)}</div></div>
      ${fav.is_perm_blacklisted ? '<div><div class="muted">黑名单</div><div style="color:var(--danger)">永久黑名单</div></div>' : ''}
    </div>
    ${events.length ? `<details style="margin-top:12px"><summary class="muted" style="cursor:pointer">最近好感事件</summary>
      <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="最近好感事件"><table class="data-table wide" style="margin-top:8px"><thead><tr><th scope="col" class="col-time">时间</th><th scope="col" class="col-summary">事件</th><th scope="col" class="col-number">变化</th><th scope="col" class="col-status">状态</th><th scope="col" class="col-description">原因</th></tr></thead><tbody>${eventRows}</tbody></table></div>
    </details>` : ''}
  </div>`;
}

function personaDetailPath(uid) {
  const groupId=String(state.personaScopeGroupId||"").trim();
  const suffix=groupId?`?group_id=${encodeURIComponent(groupId)}`:"";
  return "/personas/"+encodeURIComponent(uid)+suffix;
}

async function loadPersonaDetail(uid) {
  const requestId=++state.personaScopeRequestId;
  const requestedGroup=String(state.personaScopeGroupId||"");
  const result=await api(personaDetailPath(uid));
  if(requestId!==state.personaScopeRequestId||requestedGroup!==String(state.personaScopeGroupId||""))return false;
  state.selectedPersona=result;
  return true;
}

async function openPersona(uid, preserveScope=false) {
  try {
    if(!preserveScope)state.personaScopeGroupId="";
    if(await loadPersonaDetail(uid))render();
  } catch (e) { alertFlash("err", e.message); }
}

function selectPersonaScopeBot(botId) {
  state.personaScopeRequestId+=1;
  state.personaScopeBotId=String(botId||"");
  state.personaScopeGroupId="";
  render();
}

async function selectPersonaScopeGroup(uid, groupId) {
  state.personaScopeGroupId=String(groupId||"");
  await openPersona(uid,true);
}

async function refreshScopedProfile(uid) {
  const botId=String(state.personaScopeBotId||"").trim();
  const groupId=String(state.personaScopeGroupId||"").trim();
  if(!botId||!groupId){alertFlash("err","请先选择在线 Bot 和目标群");return;}
  if(!confirm(`重新分析用户 ${uid} 在群 ${groupId} 的差异画像？`))return;
  state.scopedProfileBusy=true;render();
  try{
    const result=await api(`/personas/${encodeURIComponent(uid)}/group-refresh`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:botId,group_id:groupId})});
    alertFlash(result.partial?"info":(result.status==="succeeded"?"ok":"info"),result.status==="succeeded"?"群内画像已刷新":`群内画像未更新：${result.code||result.status}`);
    await loadPersonaDetail(uid);
  }catch(e){alertFlash("err",e.message||"群内画像刷新失败");}
  finally{state.scopedProfileBusy=false;render();}
}

function renderQqProfileCard(core, userId) {
  const meta = (core && core.qq_profile) || {};
  const safeAvatar = safeHttpUrl(meta.avatar_url);
  const avatar = safeAvatar || `https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(userId)}&spec=640`;
  const homepage = safeHttpUrl(meta.homepage_url);
  const atomicFields = new Set(["性别", "年龄", "QID", "等级", "登录天数", "群角色", "专属头衔"]);
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
   .map(([k, v]) => `<tr><th scope="row" class="muted u-atomic">${escapeHtml(k)}</th><td class="col-description"><span class="${atomicFields.has(k) ? "u-atomic" : "u-wrap"}" title="${escapeAttr(String(v))}">${escapeHtml(String(v))}</span></td></tr>`).join("");
  return `<div class="card">
    <h2>QQ 资料快照</h2>
    <div class="qq-profile-card">
      <img class="qq-profile-avatar" src="${escapeAttr(avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">
      <div class="qq-profile-body">
        ${rows ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="QQ 资料快照"><table class="data-table compact"><tbody>${rows}</tbody></table></div>` : '<p class="muted">暂无协议资料字段。</p>'}
        <div class="qq-profile-links">
          ${safeAvatar ? `<a class="btn small" href="${escapeAttr(safeAvatar)}" target="_blank" rel="noopener noreferrer">查看头像</a>` : ''}
          ${homepage ? `<a class="btn small" href="${escapeAttr(homepage)}" target="_blank" rel="noreferrer">打开主页</a>` : ''}
        </div>
      </div>
    </div>
  </div>`;
}

function renderAvatarInsightCard(core, userId) {
  const analysis = (core && core.avatar_analysis) || {};
  const insight = (core && core.avatar_insight) || {};
  const status = String(analysis.status || "");
  const statusMap = {success:"分析成功", unchanged:"头像未变化", failed:"分析失败"};
  const kindMap = {real_person:"真人头像", illustration:"插画", acg_character:"ACG 角色", logo:"Logo", other:"其他", unknown:"无法判断"};
  const analyzedAt = Number(analysis.analyzed_at || 0);
  const checkedAt = Number(analysis.checked_at || 0);
  const atomicFields = new Set(["状态", "内容 hash", "最近检查", "最近成功分析", "视觉 route", "头像类型", "主体数", "包含文字", "置信度"]);
  const rows = [
    ["状态", statusMap[status] || "尚未分析"],
    ["内容 hash", analysis.content_hash_short || "—"],
    ["最近检查", checkedAt ? new Date(checkedAt * 1000).toLocaleString() : "—"],
    ["最近成功分析", analyzedAt ? new Date(analyzedAt * 1000).toLocaleString() : "—"],
    ["视觉 route", analysis.route || "—"],
    ["头像类型", kindMap[insight.asset_kind] || insight.asset_kind || "—"],
    ["主体数", insight.subject_count],
    ["中性摘要", insight.neutral_summary],
    ["ACG 候选", Array.isArray(insight.acg_candidates) ? insight.acg_candidates.join("、") : ""],
    ["包含文字", insight.contains_text === true ? "是" : (insight.contains_text === false ? "否" : "")],
    ["置信度", insight.confidence === undefined ? "" : Number(insight.confidence).toFixed(2)],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "")
   .map(([key, value]) => `<tr><th scope="row" class="muted u-atomic">${escapeHtml(key)}</th><td class="col-description"><span class="${atomicFields.has(key) ? "u-ellipsis" : "u-wrap"}" title="${escapeAttr(String(value))}">${escapeHtml(String(value))}</span></td></tr>`).join("");
  const busy = String(state.avatarAnalysisBusy || "").endsWith(`:${userId}`);
  return `<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">头像长期画像</h2>
      <div class="row" style="gap:8px">
        <button class="btn small primary" onclick="refreshAvatarAnalysis('${escapeAttr(userId)}')" ${busy?'disabled':''}>${state.avatarAnalysisBusy===`refresh:${userId}`?'<span class="spinner"></span> 排队中…':'重新分析'}</button>
        <button class="btn small danger" onclick="clearAvatarAnalysis('${escapeAttr(userId)}')" ${busy?'disabled':''}>${state.avatarAnalysisBusy===`clear:${userId}`?'<span class="spinner"></span> 删除中…':'删除分析'}</button>
      </div>
    </div>
    ${rows ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="头像长期画像"><table class="data-table compact" style="margin-top:12px"><tbody>${rows}</tbody></table></div>` : '<p class="muted">暂无持久化头像分析。</p>'}
    <p class="muted" style="font-size:11px;margin-top:8px">真人头像只保留“真人头像”类型；不会保存头像 bytes、data URL 或模型 raw response。</p>
  </div>`;
}

function renderPersonaDetail() {
  const p = state.selectedPersona;
  const core = p.core_profile;
  const claimLabels={gender:"性别",age_group:"年龄段",occupation:"职业",portrait:"人物描述",interests:"兴趣",routine:"作息",communication_style:"沟通风格",emotion_baseline:"情绪基线",social_mode:"社交模式",knowledge:"知识结构",relationship:"关系",taboos:"雷区",memory_anchors:"记忆锚点",recent_focus:"近期关注",content_pref:"内容偏好",nickname_pref:"称呼偏好",interaction_advice:"互动建议",group_role:"本群角色"};
  const renderClaims=claims=>(claims||[]).map(claim=>`<tr><th scope="row" class="u-atomic">${escapeHtml(claimLabels[claim.key]||claim.key||"")}</th><td class="col-description u-wrap">${escapeHtml(claim.value||"")}</td><td class="col-status u-atomic">${escapeHtml(claim.source||"")}</td><td class="col-number u-atomic u-tabular">${Number(claim.confidence||0).toFixed(2)}</td></tr>`).join("");
  const locals = (p.local_profiles || []).map(lp => {const claimRows=renderClaims(lp.effective_claims);return `<div class="card" style="background:#0e1117">
    <div class="between"><strong>群 ${escapeHtml(lp.group_id)}</strong><span class="muted" style="font-size:12px">${new Date(lp.updated_at*1000).toLocaleString()}</span></div>
    ${claimRows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群内 effective claims"><table class="data-table compact" style="margin-top:8px"><thead><tr><th scope="col">字段</th><th scope="col">值</th><th scope="col">来源</th><th scope="col">置信度</th></tr></thead><tbody>${claimRows}</tbody></table></div>`:`<pre class="u-pre-wrap code-scroll" style="margin:6px 0 0;font-family:inherit">${escapeHtml(lp.profile_text||"")}</pre>`}
  </div>`;}).join("");
  const structured = (core && core.structured) || {};
  const corr = (core && core.user_corrections) || {};
  const structRows = Object.keys(structured).map(k => `<tr>
      <th scope="row" class="u-atomic">${escapeHtml(claimLabels[k]||k)}${corr[claimLabels[k]]||corr[k]?' <span class="device-status approved">已更正</span>':''}</th>
      <td class="col-description u-wrap">${escapeHtml(String(structured[k]))}</td>
    </tr>`).join("");
  const structCard = `<div class="card"><h2>结构化字段（持久保存）</h2>
    ${structRows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户画像结构化字段"><table class="data-table compact"><tbody>${structRows}</tbody></table></div>`:'<p class="muted">暂无结构化字段</p>'}
    <div class="field-input" style="margin-top:12px">
      <input id="corr-field" type="text" placeholder="字段（如 性别/职业）" style="max-width:160px">
      <input id="corr-value" type="text" placeholder="更正为…" style="max-width:220px">
      <button class="btn small primary" onclick="submitCorrection('${escapeAttr(p.user_id)}')">提交更正</button>
    </div>
    <p class="muted" style="font-size:11px;margin-top:6px">用户确认的画像事实会保留到后续重生成，但只作为背景数据，不构成模型指令。</p>
  </div>`;
  const bots=(state.qqInfo&&state.qqInfo.bots)||[];
  const botOptions=bots.map(item=>{const id=String(item.bot_id||"");return `<option value="${escapeAttr(id)}" ${id===state.personaScopeBotId?"selected":""}>QQ ${escapeHtml(id)}</option>`}).join("");
  const scopedGroups=(state.qqGroups||[]).filter(item=>(item.bot_self_ids||[]).map(String).includes(String(state.personaScopeBotId||"")));
  const groupOptions=scopedGroups.map(item=>{const id=String(item.group_id||"");const label=item.group_name?`${item.group_name} (${id})`:id;return `<option value="${escapeAttr(id)}" ${id===state.personaScopeGroupId?"selected":""}>${escapeHtml(label)}</option>`}).join("");
  const effectiveRows=state.personaScopeGroupId?renderClaims(p.effective_claims):"";
  const scopedCard=`<div class="card"><div class="between" style="gap:12px;flex-wrap:wrap"><h2 style="margin:0">当前群差异画像</h2><button class="btn small primary" onclick="refreshScopedProfile('${escapeAttr(p.user_id)}')" ${!state.personaScopeBotId||!state.personaScopeGroupId||state.scopedProfileBusy?"disabled":""}>${state.scopedProfileBusy?'<span class="spinner"></span> 刷新中…':'重新分析'}</button></div>
    <div class="field-input" style="margin-top:12px"><label>Bot <select onchange="selectPersonaScopeBot(this.value)">${botOptions||'<option value="">无在线 Bot</option>'}</select></label><label>群 <select onchange="selectPersonaScopeGroup('${escapeAttr(p.user_id)}',this.value)"><option value="">选择目标群</option>${groupOptions}</select></label></div>
    ${state.personaScopeGroupId?(effectiveRows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="当前群 effective claims"><table class="data-table compact" style="margin-top:12px"><thead><tr><th scope="col">字段</th><th scope="col">值</th><th scope="col">来源</th><th scope="col">置信度</th></tr></thead><tbody>${effectiveRows}</tbody></table></div>`:'<p class="muted">当前群暂无差异画像，可手动重新分析。</p>'):'<p class="muted">选择明确的在线 Bot 与目标群后，可查看 effective claims 或触发安全刷新。</p>'}</div>`;
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedPersona=null;render()">返回列表</button><span class="muted">用户 ${escapeHtml(p.user_id)}</span></div>
    ${renderAdminOperations("persona","画像操作诊断")}
    ${renderFavorabilityCard(p.favorability, "用户好感度")}
    ${renderQqProfileCard(core, p.user_id)}
    ${renderAvatarInsightCard(core, p.user_id)}
    <div class="card"><h2>全局印象</h2>${core && core.profile_text ? `<pre class="u-pre-wrap code-scroll" style="margin:0;font-family:inherit">${escapeHtml(core.profile_text || '')}</pre>` : '<p class="muted">无全局画像</p>'}</div>
    ${structCard}
    ${scopedCard}
    <h3 style="margin-bottom:10px">各群印象（${(p.local_profiles||[]).length}）</h3>
    ${locals || '<p class="muted">无各群画像</p>'}`;
}

function userPolicyTierLabel(value) {
  return ({allow:"允许",level_1:"Level 1 · 12 小时",level_2:"Level 2 · 24 小时",permanent:"自动永久 Blacklist",manual_block:"管理员 Blacklist",manual_allow:"管理员允许"})[String(value||"")] || String(value||"未知");
}

function userPolicyExpiry(stateValue) {
  const item=stateValue||{};
  const manual=String(item.manual_mode||"");
  const raw=(manual==="block"||manual==="allow")?Number(item.manual_expires_at||0):Number(item.auto_expires_at||0);
  return raw>0?new Date(raw*1000).toLocaleString():"永久 / 无到期";
}

function userPolicyFriendName(friend) {
  const item=friend||{};
  return String(item.remark||item.nickname||"未命名好友");
}

async function setUserPolicyBot(value) {
  state.userPolicyBotId=String(value||"");
  state.userPolicyFriends=[];
  state.userPolicyFriendError="";
  if(!state.userPolicyBotId){render();return;}
  state.userPolicyBusy=true;render();
  try{
    const result=await api("/qq/friends?bot_id="+encodeURIComponent(state.userPolicyBotId),{cache:"no-store"});
    state.userPolicyFriends=result.friends||[];
  }catch(e){
    state.userPolicyFriendError="好友列表读取失败，仍可手工输入 QQ。";
    alertFlash("err",e.message||"好友列表读取失败");
  }finally{state.userPolicyBusy=false;render();}
}

function selectUserPolicyFriend(value) {
  state.userPolicyDraftUserId=String(value||"");
  render();
}

function updateUserPolicyDraftUserId(input) {
  const value=String(input&&input.value||"").replace(/\D/g,"").slice(0,20);
  if(input)input.value=value;
  state.userPolicyDraftUserId=value;
}

async function addUserPolicyBlacklist() {
  const uid=String(state.userPolicyDraftUserId||"").trim();
  const hours=Math.max(0,Math.min(8760,Number(document.getElementById("user-policy-add-hours")?.value||0)));
  if(!/^[1-9][0-9]{4,19}$/.test(uid)){alertFlash("err","请输入 5～20 位、且不以 0 开头的 QQ 号");return;}
  const duration=hours>0?`${hours} 小时`:"永久";
  if(!confirm(`确认将用户 ${uid} 加入 ${duration} Blacklist？`))return;
  state.userPolicyBusy=true;render();
  try{
    const detail=await api(`/user-policy/${encodeURIComponent(uid)}/events?include_evidence=false&limit=1`,{cache:"no-store"});
    await api(`/user-policy/${encodeURIComponent(uid)}/override`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({
        mode:"block",
        expected_revision:Number((detail.state||{}).revision||0),
        expires_at:hours>0?Date.now()/1000+hours*3600:0,
        reason_code:"webui_blacklist_add",
      }),
    });
    state.userPolicyDraftUserId="";
    state.userPolicyDurationHours=0;
    state.userPolicyTier="blocked";
    await reloadUserPolicyList();
    alertFlash("ok",`用户 ${uid} 已加入 Blacklist`);
  }catch(e){alertFlash("err",e.message||"加入 Blacklist 失败");}
  finally{state.userPolicyBusy=false;render();}
}

function currentUserPolicyState(userId) {
  const uid=String(userId||"");
  const selected=state.selectedUserPolicy&&state.selectedUserPolicy.state;
  if(selected&&String(selected.user_id||state.selectedUserPolicy.user_id||"")===uid)return selected;
  return ((state.userPolicy&&state.userPolicy.states)||[]).find(item=>String(item.user_id||"")===uid)||null;
}

async function unblockUserPolicy(userId) {
  const uid=String(userId||"");
  let current=currentUserPolicyState(uid);
  if(!current)return;
  if(!confirm(`确认解除用户 ${uid} 的当前 Blacklist？管理员 block 与自动 strikes 都会清除。`))return;
  state.userPolicyBusy=true;render();
  try{
    if(String(current.manual_mode||"")==="block"){
      const result=await api(`/user-policy/${encodeURIComponent(uid)}/override`,{
        method:"POST",headers:{"content-type":"application/json"},
        body:JSON.stringify({mode:"inherit",expected_revision:Number(current.revision||0),expires_at:0,reason_code:"webui_blacklist_unblock"}),
      });
      current=result.state||current;
    }
    if(Boolean(current.blocked)){
      const result=await api(`/user-policy/${encodeURIComponent(uid)}/clear-auto`,{
        method:"POST",headers:{"content-type":"application/json"},
        body:JSON.stringify({expected_revision:Number(current.revision||0)}),
      });
      current=result.state||current;
    }
    await reloadUserPolicyList();
    if(state.selectedUserPolicy&&String(state.selectedUserPolicy.user_id||"")===uid){
      state.selectedUserPolicy=await api(`/user-policy/${encodeURIComponent(uid)}/events?include_evidence=true&limit=150`,{cache:"no-store"});
    }
    alertFlash("ok",`用户 ${uid} 已解除 Blacklist`);
  }catch(e){alertFlash("err",e.message||"解除 Blacklist 失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function reloadUserPolicyList() {
  const qs=new URLSearchParams({limit:"300"});
  if(state.userPolicyTier)qs.set("tier",state.userPolicyTier);
  state.userPolicy=await api("/user-policy/states?"+qs.toString(),{cache:"no-store"});
}

async function setUserPolicyTier(value) {
  state.userPolicyTier=String(value||"");
  state.selectedUserPolicy=null;
  try{await reloadUserPolicyList();render();}catch(e){alertFlash("err",e.message||"用户策略读取失败");}
}

async function openUserPolicy(userId) {
  const uid=String(userId||"");
  state.userPolicyBusy=true;render();
  try{
    state.selectedUserPolicy=await api(`/user-policy/${encodeURIComponent(uid)}/events?include_evidence=true&limit=150`,{cache:"no-store"});
  }catch(e){alertFlash("err",e.message||"策略详情读取失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function applyUserPolicyOverride(userId) {
  const uid=String(userId||"");
  const current=(state.selectedUserPolicy&&state.selectedUserPolicy.state)||{};
  const mode=String(document.getElementById("user-policy-mode")?.value||"inherit");
  const durationHours=Math.max(0,Number(document.getElementById("user-policy-hours")?.value||0));
  const expiresAt=(mode==="inherit"||durationHours<=0)?0:(Date.now()/1000+durationHours*3600);
  if(mode==="block"&&expiresAt===0&&!confirm(`永久 Blacklist 用户 ${uid}？`))return;
  state.userPolicyBusy=true;render();
  try{
    const result=await api(`/user-policy/${encodeURIComponent(uid)}/override`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({mode,expected_revision:Number(current.revision||0),expires_at:expiresAt,reason_code:"webui_manual_override"}),
    });
    state.selectedUserPolicy={...(state.selectedUserPolicy||{}),state:result.state};
    await reloadUserPolicyList();
    alertFlash("ok","用户策略已更新");
  }catch(e){alertFlash("err",e.message||"用户策略更新失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function clearUserPolicyAuto(userId) {
  const uid=String(userId||"");
  const current=(state.selectedUserPolicy&&state.selectedUserPolicy.state)||{};
  if(!confirm(`清除用户 ${uid} 的自动 strikes 与自动 tier history？管理员 override 不受影响。`))return;
  state.userPolicyBusy=true;render();
  try{
    const result=await api(`/user-policy/${encodeURIComponent(uid)}/clear-auto`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({expected_revision:Number(current.revision||0)}),
    });
    state.selectedUserPolicy={...(state.selectedUserPolicy||{}),state:result.state};
    await reloadUserPolicyList();
    alertFlash("ok","自动 strikes 已清除");
  }catch(e){alertFlash("err",e.message||"自动状态清除失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function purgeUserPolicyProfile(userId) {
  const uid=String(userId||"");
  const current=(state.selectedUserPolicy&&state.selectedUserPolicy.state)||{};
  const expected=`PURGE PROFILE ${uid}`;
  const typed=window.prompt(`这是不可逆操作，将删除该用户所有全局/群内画像、Persona history、Memory、关系边与头像 visual evidence。\n\n请输入：${expected}`)||"";
  if(typed!==expected){if(typed)alertFlash("err","确认串不匹配，未执行清除");return;}
  state.userPolicyBusy=true;render();
  try{
    const result=await api(`/user-policy/${encodeURIComponent(uid)}/profile`,{
      method:"DELETE",headers:{"content-type":"application/json"},
      body:JSON.stringify({expected_revision:Number(current.revision||0),confirmation:typed}),
    });
    state.selectedUserPolicy={...(state.selectedUserPolicy||{}),state:result.state,purge_counts:result.counts};
    const total=Object.values(result.counts||{}).reduce((sum,value)=>sum+Number(value||0),0);
    alertFlash("ok",`用户画像数据已不可逆清除，共移除 ${total} 项；Policy state 保留。`);
  }catch(e){alertFlash("err",e.message||"用户画像清除失败");}
  finally{state.userPolicyBusy=false;render();}
}

function renderUserPolicyDetail() {
  const detail=state.selectedUserPolicy;
  if(!detail)return "";
  const item=detail.state||{};
  const uid=String(detail.user_id||item.user_id||"");
  const events=(detail.events||[]).map(event=>{
    const evidence=String(event.evidence_excerpt||"").trim();
    const when=event.created_at?new Date(Number(event.created_at)*1000).toLocaleString():"-";
    return `<tr>
      <td class="col-time u-atomic u-tabular">${escapeHtml(when)}</td>
      <td class="col-status u-atomic">${escapeHtml(event.event_kind||"")}</td>
      <td class="col-status u-atomic">${escapeHtml(event.verdict||event.reason_code||"")}</td>
      <td class="col-description u-wrap">${escapeHtml([event.category,event.intent,event.severity].filter(Boolean).join(" / "))}</td>
      <td class="col-number u-tabular">${Number(event.confidence||0).toFixed(2)}</td>
      <td class="col-description u-wrap">${evidence?escapeHtml(evidence):'<span class="muted">无 / 已到期</span>'}</td>
    </tr>`;
  }).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedUserPolicy=null;render()">返回列表</button><span class="muted">用户 ${escapeHtml(uid)}</span></div>
    <div class="card">
      <div class="between" style="gap:12px;flex-wrap:wrap"><h2 style="margin:0">策略状态</h2><span class="tag tag--status">revision ${Number(item.revision||0)}</span></div>
      <div class="row" style="gap:24px;margin-top:12px">
        <div><div class="muted">effective tier</div><strong>${escapeHtml(userPolicyTierLabel(item.effective_tier))}</strong></div>
        <div><div class="muted">到期</div><span class="u-tabular">${escapeHtml(userPolicyExpiry(item))}</span></div>
        <div><div class="muted">auto stage / strikes</div><span class="u-tabular">${Number(item.auto_stage||0)} / ${Number(item.violation_count||0)}</span></div>
        <div><div class="muted">最近更新</div><span class="u-tabular">${item.updated_at?escapeHtml(new Date(Number(item.updated_at)*1000).toLocaleString()):"-"}</span></div>
      </div>
      <div class="field-input" style="margin-top:14px">
        <label>管理员策略 <select id="user-policy-mode"><option value="inherit" ${item.manual_mode==="inherit"?'selected':''}>inherit（沿用自动状态）</option><option value="block" ${item.manual_mode==="block"?'selected':''}>block</option><option value="allow" ${item.manual_mode==="allow"?'selected':''}>allow</option></select></label>
        <label>临时小时数 <input id="user-policy-hours" type="number" min="0" max="8760" step="1" value="0" style="max-width:120px"></label>
        <button class="btn small primary" onclick="applyUserPolicyOverride('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>保存 override</button>
        ${item.blocked?`<button class="btn small danger" onclick="unblockUserPolicy('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>解除 Blacklist</button>`:""}
        <button class="btn small" onclick="clearUserPolicyAuto('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>清除自动 strikes</button>
        <button class="btn small danger" onclick="purgeUserPolicyProfile('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>彻底清除画像</button>
      </div>
      <p class="muted" style="font-size:11px">小时数为 0 时 block/allow 为永久 override；彻底清除画像不会删除 Policy state 或事件。所有写操作均使用当前 revision 防并发覆盖。</p>
    </div>
    ${detail.purge_counts?`<div class="card"><h2>最近清除结果</h2><pre class="u-pre-wrap">${escapeHtml(JSON.stringify(detail.purge_counts,null,2))}</pre></div>`:""}
    <div class="card"><h2>事件时间线</h2>
      <p class="muted">evidence 短摘只在此管理员详情请求中解密，页面不会写入浏览器持久存储；服务端 ciphertext 到期后自动删除。</p>
      ${events?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户策略事件"><table class="data-table wide"><thead><tr><th scope="col">时间</th><th scope="col">类型</th><th scope="col">判定</th><th scope="col">分类</th><th scope="col">置信度</th><th scope="col">evidence 短摘</th></tr></thead><tbody>${events}</tbody></table></div>`:'<p class="muted">暂无策略事件。</p>'}
    </div>`;
}

function renderUserPolicyAdd() {
  const bots=(state.userPolicyBotInfo&&state.userPolicyBotInfo.bots)||[];
  const botOptions=bots.map(item=>{const id=String(item.bot_id||"");return `<option value="${escapeAttr(id)}" ${id===state.userPolicyBotId?'selected':''}>QQ ${escapeHtml(id)}</option>`;}).join("");
  const friends=(state.userPolicyFriends||[]).filter(item=>/^[1-9][0-9]{4,19}$/.test(String(item.user_id||"")));
  const friendOptions=friends.map(item=>{const uid=String(item.user_id||"");return `<option value="${escapeAttr(uid)}" ${uid===state.userPolicyDraftUserId?'selected':''}>${escapeHtml(userPolicyFriendName(item))} · ${escapeHtml(uid)}</option>`;}).join("");
  return `<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap"><div><h2 style="margin:0">添加 Blacklist</h2><p class="muted" style="margin:6px 0 0">可从指定 Bot 的好友中选取，也可直接输入 QQ 号；QQ 只作为全局策略标识保存。</p></div><span class="tag tag--status">管理员操作</span></div>
    <div class="field-input" style="margin-top:14px">
      <label>好友来源 Bot <select onchange="setUserPolicyBot(this.value)" ${state.userPolicyBusy||!botOptions?'disabled':''}>${botOptions||'<option value="">无已连接 Bot</option>'}</select></label>
      <label>选择好友 <select onchange="selectUserPolicyFriend(this.value)" ${state.userPolicyBusy||!friendOptions?'disabled':''}><option value="">请选择好友</option>${friendOptions}</select></label>
      <label>QQ 号 <input inputmode="numeric" autocomplete="off" maxlength="20" value="${escapeAttr(state.userPolicyDraftUserId)}" placeholder="手工输入 5～20 位 QQ" oninput="updateUserPolicyDraftUserId(this)" ${state.userPolicyBusy?'disabled':''}></label>
      <label>临时小时数 <input id="user-policy-add-hours" type="number" min="0" max="8760" step="1" value="${escapeAttr(state.userPolicyDurationHours)}" oninput="state.userPolicyDurationHours=Math.max(0,Math.min(8760,Number(this.value||0)))" style="max-width:120px" ${state.userPolicyBusy?'disabled':''}></label>
      <button class="btn danger" onclick="addUserPolicyBlacklist()" ${state.userPolicyBusy?'disabled':''}>加入 Blacklist</button>
    </div>
    <p class="muted" style="font-size:11px">小时数为 0 表示永久管理员 Blacklist；好友列表不可用时不影响手工添加。加入、解除和详情修改均使用 revision 防止并发覆盖。</p>
    ${state.userPolicyFriendError?`<div class="alert err">${escapeHtml(state.userPolicyFriendError)}</div>`:""}
  </div>`;
}

function renderUserPolicy() {
  if(state.selectedUserPolicy)return renderUserPolicyDetail();
  const friendMap=new Map((state.userPolicyFriends||[]).map(item=>[String(item.user_id||""),userPolicyFriendName(item)]));
  const rows=((state.userPolicy&&state.userPolicy.states)||[]).map(item=>`<tr>
    <td class="col-id"><button class="btn small u-atomic u-tabular" onclick="openUserPolicy('${escapeAttr(item.user_id)}')">${escapeHtml(item.user_id)}</button>${friendMap.has(String(item.user_id||""))?`<div class="muted u-wrap" style="margin-top:4px">${escapeHtml(friendMap.get(String(item.user_id||"")))}</div>`:""}</td>
    <td class="col-status"><span class="tag tag--status">${escapeHtml(userPolicyTierLabel(item.effective_tier))}</span></td>
    <td class="col-time u-atomic u-tabular">${escapeHtml(userPolicyExpiry(item))}</td>
    <td class="col-number u-tabular">${Number(item.auto_stage||0)} / ${Number(item.violation_count||0)}</td>
    <td class="col-status u-atomic">${escapeHtml(item.manual_mode||"inherit")}</td>
    <td class="col-number u-tabular">${Number(item.revision||0)}</td>
    <td class="col-time u-atomic u-tabular">${item.updated_at?escapeHtml(new Date(Number(item.updated_at)*1000).toLocaleString()):"-"}</td>
    <td class="col-actions">${item.blocked?`<button class="btn small danger" onclick="unblockUserPolicy('${escapeAttr(item.user_id)}')" ${state.userPolicyBusy?'disabled':''}>解除</button>`:`<button class="btn small" onclick="openUserPolicy('${escapeAttr(item.user_id)}')">查看</button>`}</td>
  </tr>`).join("");
  const options=[["blocked","当前阻止"],["","全部策略记录"],["allow","允许"],["level_1","Level 1"],["level_2","Level 2"],["permanent","自动永久"],["manual_block","管理员 Blacklist"],["manual_allow","管理员允许"]].map(([value,label])=>`<option value="${escapeAttr(value)}" ${state.userPolicyTier===value?'selected':''}>${escapeHtml(label)}</option>`).join("");
  return `${renderUserPolicyAdd()}<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap"><div><h2 style="margin:0">用户策略 / Blacklist</h2><p class="muted" style="margin:6px 0 0">全局 QQ 用户策略，跨群、私聊、Bot 身份与 QZone 生效。</p></div><label>tier <select onchange="setUserPolicyTier(this.value)">${options}</select></label></div>
    ${rows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户策略列表"><table class="data-table wide" style="margin-top:12px"><thead><tr><th scope="col">用户</th><th scope="col">effective tier</th><th scope="col">到期</th><th scope="col">stage / strikes</th><th scope="col">manual</th><th scope="col">revision</th><th scope="col">更新时间</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p class="muted">当前筛选下没有已持久化策略状态。</p>'}
  </div>`;
}

async function reloadOutbound() {
  const qs=new URLSearchParams({limit:"300"});
  if(state.outboundBotId)qs.set("bot_id",state.outboundBotId);
  if(state.outboundKind)qs.set("conversation_kind",state.outboundKind);
  if(state.outboundConversationId)qs.set("conversation_id",state.outboundConversationId);
  if(state.outboundStatus)qs.set("status",state.outboundStatus);
  if(state.outboundRecalled)qs.set("recalled",state.outboundRecalled);
  state.outbound=await api("/outbound/recent?"+qs.toString(),{cache:"no-store"});
}

async function applyOutboundFilters() {
  state.outboundBotId=String(document.getElementById("outbound-bot")?.value||"").trim();
  state.outboundKind=String(document.getElementById("outbound-kind")?.value||"").trim();
  state.outboundConversationId=String(document.getElementById("outbound-conversation")?.value||"").trim();
  state.outboundStatus=String(document.getElementById("outbound-status")?.value||"").trim();
  state.outboundRecalled=String(document.getElementById("outbound-recalled")?.value||"").trim();
  state.outboundBusy=true;render();
  try{await reloadOutbound();}catch(e){alertFlash("err",e.message||"出站账本读取失败");}
  finally{state.outboundBusy=false;render();}
}

async function recallOutboundOperation(button) {
  const operationId=String(button?.dataset?.operationId||"");
  const botId=String(button?.dataset?.botId||"");
  const kind=String(button?.dataset?.conversationKind||"");
  const conversationId=String(button?.dataset?.conversationId||"");
  const expected=`RECALL ${operationId}`;
  const typed=window.prompt(`将撤回该 operation 的全部平台消息；服务端会重新验证 Bot、会话、窗口、完整性与未撤回状态。\n\n请输入：${expected}`)||"";
  if(typed!==expected){if(typed)alertFlash("err","确认串不匹配，未执行撤回");return;}
  state.outboundBusy=true;render();
  try{
    const result=await api(`/outbound/${encodeURIComponent(operationId)}/recall`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({bot_id:botId,conversation_kind:kind,conversation_id:conversationId,confirmation:typed}),
    });
    const diagnostic=rememberAdminOperation("outbound",result,"Bot 消息撤回未完成");
    if(result.status==="succeeded")alertFlash("ok",diagnostic?.title||"Bot 消息撤回完成");
    else if(result.status==="unknown"||result.status==="partial")alertFlash("info",diagnostic?.title||"撤回结果需要人工核对");
    else alertFlash("err",diagnostic?.title||"Bot 消息撤回失败");
    await reloadOutbound();
  }catch(e){const diagnostic=rememberAdminOperation("outbound",e,"Bot 消息撤回未完成");alertFlash("err",diagnostic?.title||e.message||"Bot 消息撤回失败");}
  finally{state.outboundBusy=false;render();}
}

function renderOutbound() {
  const messages=(state.outbound&&state.outbound.messages)||[];
  const seenOperations=new Set();
  const rows=messages.map(item=>{
    const operationId=String(item.operation_id||"");
    const first=!seenOperations.has(operationId);
    seenOperations.add(operationId);
    const recalled=Number(item.recalled_at||0)>0;
    const attempted=Boolean(String(item.recall_status||""));
    const canRecall=first&&!recalled&&!attempted&&item.status==="sent";
    const action=canRecall?`<button class="btn small danger" data-operation-id="${escapeAttr(operationId)}" data-bot-id="${escapeAttr(item.bot_id||'')}" data-conversation-kind="${escapeAttr(item.conversation_kind||'')}" data-conversation-id="${escapeAttr(item.conversation_id||'')}" onclick="recallOutboundOperation(this)" ${state.outboundBusy?'disabled':''}>撤回 operation</button>`:(first?'<span class="muted">不可撤回 / 已尝试</span>':'<span class="muted">同 operation</span>');
    return `<tr>
      <td class="col-time u-atomic u-tabular">${item.created_at?escapeHtml(new Date(Number(item.created_at)*1000).toLocaleString()):"-"}</td>
      <td class="col-model"><code class="u-ellipsis" title="${escapeAttr(operationId)}">${escapeHtml(operationId)}</code><div class="muted u-tabular">part ${Number(item.part_index||0)}</div></td>
      <td class="col-status u-atomic">${escapeHtml(item.bot_id||"")}</td>
      <td class="col-model u-atomic">${escapeHtml(item.conversation_kind||"")} ${escapeHtml(item.conversation_id||"")}</td>
      <td class="col-status u-atomic">${escapeHtml(item.surface||"")}</td>
      <td class="col-model u-atomic u-tabular">${escapeHtml(item.message_id||"-")}</td>
      <td class="col-status"><span class="tag tag--status">${escapeHtml(recalled?"recalled":(item.recall_status||item.status||""))}</span></td>
      <td class="col-description u-wrap">${escapeHtml(item.preview||"")}</td>
      <td class="col-actions">${action}</td>
    </tr>`;
  }).join("");
  const diagnostics=renderAdminOperations("outbound","Bot 消息撤回诊断");
  return `${diagnostics}
    <div class="card">
      <div class="between" style="gap:12px;flex-wrap:wrap"><div><h2 style="margin:0">近期 Bot 消息</h2><p class="muted" style="margin:6px 0 0">只展示持久账本中的脱敏 preview；不会返回消息 payload、HMAC key 或 Secret。</p></div><button class="btn small" onclick="applyOutboundFilters()" ${state.outboundBusy?'disabled':''}>刷新</button></div>
      <div class="field-input" style="margin-top:12px">
        <input id="outbound-bot" type="text" placeholder="Bot ID" value="${escapeAttr(state.outboundBotId)}">
        <select id="outbound-kind"><option value="" ${!state.outboundKind?'selected':''}>全部会话</option><option value="group" ${state.outboundKind==='group'?'selected':''}>group</option><option value="private" ${state.outboundKind==='private'?'selected':''}>private</option></select>
        <input id="outbound-conversation" type="text" placeholder="群号 / QQ 号" value="${escapeAttr(state.outboundConversationId)}">
        <select id="outbound-status"><option value="" ${!state.outboundStatus?'selected':''}>全部发送状态</option><option value="sent" ${state.outboundStatus==='sent'?'selected':''}>sent</option><option value="unknown" ${state.outboundStatus==='unknown'?'selected':''}>unknown</option><option value="failed" ${state.outboundStatus==='failed'?'selected':''}>failed</option></select>
        <select id="outbound-recalled"><option value="" ${!state.outboundRecalled?'selected':''}>全部撤回状态</option><option value="false" ${state.outboundRecalled==='false'?'selected':''}>未撤回</option><option value="true" ${state.outboundRecalled==='true'?'selected':''}>已撤回</option></select>
        <button class="btn small primary" onclick="applyOutboundFilters()" ${state.outboundBusy?'disabled':''}>应用筛选</button>
      </div>
      ${rows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="近期 Bot 出站消息"><table class="data-table xwide"><thead><tr><th scope="col">时间</th><th scope="col">operation / part</th><th scope="col">Bot</th><th scope="col">会话</th><th scope="col">surface</th><th scope="col">message_id</th><th scope="col">状态</th><th scope="col">脱敏 preview</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p class="muted">当前筛选下没有出站账本记录。</p>'}
      <p class="muted" style="font-size:11px">管理员只能选择完整 operation；前端不会提交任意 message_id。unknown/partial/已尝试 operation 会被服务端封存，禁止自动重试。</p>
    </div>`;
}

const ADMIN_OPERATION_STORAGE_KEY = "personification_admin_operation_diagnostics_v1";

function adminOperationEntries() {
  if (Array.isArray(state.adminOperationDiagnostics)) return state.adminOperationDiagnostics;
  try {
    const saved=JSON.parse(sessionStorage.getItem(ADMIN_OPERATION_STORAGE_KEY)||"[]");
    state.adminOperationDiagnostics=Array.isArray(saved)?saved.slice(0,16):[];
  } catch { state.adminOperationDiagnostics=[]; }
  return state.adminOperationDiagnostics;
}

function rememberAdminOperation(scope, value, fallbackTitle="管理操作未完成") {
  const diagnostic=value&&value.diagnostic&&typeof value.diagnostic==="object"
    ? value.diagnostic
    : (value instanceof Error ? operationDiagnosticFromError(value,fallbackTitle) : value);
  if(!diagnostic||typeof diagnostic!=="object"||!diagnostic.code)return null;
  state.adminOperationDiagnostics=[{scope,diagnostic},...adminOperationEntries()].slice(0,16);
  try{sessionStorage.setItem(ADMIN_OPERATION_STORAGE_KEY,JSON.stringify(state.adminOperationDiagnostics));}catch{}
  return diagnostic;
}

function clearAdminOperations(scope) {
  state.adminOperationDiagnostics=adminOperationEntries().filter(item=>item.scope!==scope);
  try{sessionStorage.setItem(ADMIN_OPERATION_STORAGE_KEY,JSON.stringify(state.adminOperationDiagnostics));}catch{}
  render();
}

function renderAdminOperations(scope,title) {
  const items=renderOperationHistory(adminOperationEntries().filter(item=>item.scope===scope).map(item=>item.diagnostic),{group:`view-${state.view}`});
  return items?`<div class="card"><div class="between"><h2>${escapeHtml(title)}</h2><button class="btn small" onclick="clearAdminOperations('${escapeAttr(scope)}')">清空</button></div>${items}</div>`:"";
}

function renderPersonaBuilder() {
  const r = state.personaTemplateResult;
  const task = state.personaTemplateTask || {};
  const history = state.personaTemplateHistory || [];
  const sources = (r && r.sources) || [];
  const subagents = (r && r.subagents) || [];
  const sourceCards = sources.map((s, i) => `<div class="persona-source-card">
    <div class="between" style="gap:8px"><span class="tag">S${i + 1}</span><span class="muted">${escapeHtml(s.source || s.kind || "资料")}</span></div>
    <strong>${safeHttpUrl(s.url) ? `<a href="${escapeAttr(safeHttpUrl(s.url))}" target="_blank" rel="noreferrer">${escapeHtml(s.title || s.query || s.url)}</a>` : escapeHtml(s.title || s.query || "")}</strong>
    <p>${escapeHtml((s.summary || "").slice(0, 260))}</p>
    ${s.url ? `<code class="u-wrap">${escapeHtml(s.url)}</code>` : ""}
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
  const recordId = r && r.history_record && r.history_record.record_id || "";
  const revision = r && r.revision || "";
  const allAvatarCandidates = r && r.avatar_candidates || [];
  const avatarCandidates = allAvatarCandidates.filter(item => item.safety_status==="pass"&&item.vision_status==="verified");
  const avatarReview = r && r.avatar_review_summary || {};
  const reviewCounts = avatarReview.status_counts || {};
  const searchDiag = avatarReview.search_diagnostics || {};
  const downloadDiag = avatarReview.download_diagnostics || {};
  const downloadFailures = downloadDiag.failure_counts || {};
  const signatureCandidates = r && r.signature_candidates || [];
  if (avatarCandidates.length && !avatarCandidates.some(x => x.candidate_id === state.personaAvatarCandidateId)) state.personaAvatarCandidateId = avatarCandidates[0].candidate_id;
  if (signatureCandidates.length && !signatureCandidates.some(x => x.candidate_id === state.personaSignatureCandidateId)) state.personaSignatureCandidateId = signatureCandidates[0].candidate_id;
  const avatarCards = avatarCandidates.map(item => `<label class="avatar-candidate ${state.personaAvatarCandidateId===item.candidate_id?'selected':''}"><input type="radio" name="persona-avatar" value="${escapeAttr(item.candidate_id)}" ${state.personaAvatarCandidateId===item.candidate_id?'checked':''} onchange="state.personaAvatarCandidateId=this.value;render()"><img src="${API}/persona-template/avatar-candidates/${encodeURIComponent(revision)}/${encodeURIComponent(item.candidate_id)}/thumbnail" alt="已验证的${escapeAttr(r.character_name||'角色')}头像候选"><span><strong>匹配 ${Math.round(Number(item.character_confidence||0)*100)}%</strong><small>头像质量 ${Math.round(Number(item.portrait_quality||0)*100)}% · 综合 ${Math.round(Number(item.fit_score||0)*100)}</small><small>${escapeHtml(item.source||'图片来源')} · ${Number(item.width||0)}×${Number(item.height||0)}</small>${item.review_reason?`<small title="${escapeAttr(item.review_reason)}">${escapeHtml(item.review_reason)}</small>`:''}</span></label>`).join("");
  const signatureRows = signatureCandidates.map(item => `<label class="signature-candidate ${state.personaSignatureCandidateId===item.candidate_id?'selected':''}"><input type="radio" name="persona-signature" value="${escapeAttr(item.candidate_id)}" ${state.personaSignatureCandidateId===item.candidate_id?'checked':''} onchange="state.personaSignatureCandidateId=this.value;render()"><span>${escapeHtml(item.text||'')}</span><small>${Number(item.length||String(item.text||'').length)} 字 · ${escapeHtml(item.safety_status||'')}</small></label>`).join("");
  const profileBotOptions=((state.qqInfo&&state.qqInfo.bots)||[]).map(item=>{const id=String(item.bot_id||"");return `<option value="${escapeAttr(id)}" ${state.personaProfileBotId===id?'selected':''}>${escapeHtml(id)}</option>`}).join("");
  const avatarStats = `<div class="avatar-review-stats"><span>安全下载 <strong>${Number(avatarReview.safe_count||allAvatarCandidates.length)}</strong></span><span>已审核 <strong>${Number(avatarReview.reviewed_count||0)}</strong></span><span>角色验证 <strong>${Number(avatarReview.verified_count||avatarCandidates.length)}</strong></span><span>不匹配 <strong>${Number(reviewCounts.rejected||0)}</strong></span><span>不确定/异常 <strong>${Number(reviewCounts.uncertain||0)+Number(reviewCounts.unavailable||0)+Number(reviewCounts.invalid_response||0)+Number(reviewCounts.error||0)}</strong></span></div>`;
  const failureLabels = {dependency_missing:'服务器缺少 Pillow',dns_or_address:'图片域名解析或地址被拒绝',not_an_image:'返回内容不是图片',http_error:'图片服务器返回错误',too_large:'图片体积超限',decode_rejected:'图片解码或尺寸不合格',download_error:'图片下载失败',duplicate:'重复图片'};
  const failureParts = Object.entries(downloadFailures).filter(([, count]) => Number(count)>0).map(([key, count]) => `${failureLabels[key]||key} ${Number(count)} 张`);
  let avatarDiagnostic = '';
  if (!Number(avatarReview.safe_count||0)) {
    if (Number(downloadDiag.extracted_url_count||0)>0 && failureParts.length) avatarDiagnostic = `已找到 ${Number(downloadDiag.extracted_url_count||0)} 条图片地址，但全部处理失败：${failureParts.join('；')}。`;
    else if (!Number(searchDiag.direct_image_count||0)) avatarDiagnostic = Number(searchDiag.web_fallback_row_count||0)>0 ? '图片搜索已降级为普通网页结果，没有获得可安全下载的图片直链。' : '图片搜索没有返回可用的图片直链。';
  }
  const diagnosticBlock = avatarDiagnostic ? `<p class="muted" style="color:var(--warn)">${escapeHtml(avatarDiagnostic)}</p>` : '';
  const profileAssets = r ? `<div class="persona-assets"><div class="between"><h3>已验证头像（${avatarCandidates.length}）</h3><span class="tag ${r.profile_status==='complete'?'':'required'}">${escapeHtml(r.profile_status==='complete'?'候选完整':'候选未完整')}</span></div>${avatarStats}${diagnosticBlock}<div class="avatar-candidate-grid">${avatarCards||'<p class="muted">没有通过目标角色视觉审核的头像。视觉不可用或不足 10 张时不会用未验证图片补位。</p>'}</div><div class="between"><h3>人设签名（${signatureCandidates.length}）</h3></div><div class="signature-candidate-list">${signatureRows||'<p class="muted">暂未生成可用签名。</p>'}</div><div class="row"><label>目标 Bot <select onchange="state.personaProfileBotId=this.value">${profileBotOptions}</select></label><button class="btn primary" onclick="applyPersonaProfileAssets('${escapeAttr(recordId)}','${escapeAttr(revision)}')" ${recordId&&revision&&state.personaProfileBotId&&(state.personaAvatarCandidateId||state.personaSignatureCandidateId)?'':'disabled'}>应用选中的头像与签名</button>${state.personaAvatarCandidateId?`<a class="btn" href="${API}/persona-template/avatar-candidates/${encodeURIComponent(revision)}/${encodeURIComponent(state.personaAvatarCandidateId)}/original" download>下载头像</a>`:''}</div></div>` : "";
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
      <div class="row"><button class="btn small" onclick="openPersonaTemplateHistory('${escapeAttr(item.record_id || "")}')">管理</button><button class="btn small danger" onclick="deletePersonaTemplateHistory('${escapeAttr(item.record_id || "")}', '${escapeAttr(item.character_name || "")}' )">删除</button></div>
    </div>`;
  }).join("");
  return `${renderAdminOperations("persona-template","人设构建与应用诊断")}<div class="card">
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
      ${ref.path ? `<span class="muted u-wrap">参考模板：<code class="u-wrap">${escapeHtml(ref.path)}</code></span>` : '<span class="muted">未读取到当前模板参考</span>'}
      ${(r.template_keys || []).map(k => `<span class="tag">${escapeHtml(k)}</span>`).join("")}
    </div>
    ${validationList ? `<div class="alert ${valid?'info':'err'}" style="margin-top:12px"><ul class="validation-list">${validationList}</ul></div>` : ""}
    ${profileAssets}
    <div class="between" style="margin:16px 0 8px">
      <h3 style="margin:0">插件 YAML 模板</h3>
      <div class="row">
        ${state.personaTemplateEditing?'<button class="btn small primary" onclick="savePersonaTemplateEdit()">保存修改</button><button class="btn small" onclick="state.personaTemplateEditing=false;render()">取消</button>':'<button class="btn small" onclick="state.personaTemplateEditing=true;render()">编辑</button>'}
        <button class="btn small primary" onclick="applyPersonaTemplate()">应用</button>
        <button class="btn small" onclick="copyPersonaTemplate()">复制</button>
      </div>
    </div>
    ${state.personaTemplateEditing?`<textarea id="persona-template-editor" class="persona-builder-description" style="min-height:520px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace">${escapeHtml(r.template || "")}</textarea>`:`<pre class="persona-template-code">${escapeHtml(r.template || "")}</pre>`}
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
    const diagnostic=rememberAdminOperation("persona-template",applied,"人设应用未完成");
    alertFlash("ok", diagnostic?.title||"人设已应用");render();
  } catch (e) {
    const diagnostic=rememberAdminOperation("persona-template",e,"人设应用未完成");alertFlash("err",diagnostic?.title||"人设应用未完成");render();
  }
}

async function applyPersonaProfileAssets(recordId, revision) {
  if (!recordId || !revision) return;
  const avatarId=state.personaAvatarCandidateId||"",signatureId=state.personaSignatureCandidateId||"";
  if(!avatarId&&!signatureId)return;
  if(!confirm("将选中的头像和签名应用到当前 QQ？两个动作会分别记录结果。"))return;
  try {
    const result=await api("/persona-template/profile-apply",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:state.personaProfileBotId,record_id:recordId,revision,avatar_candidate_id:avatarId,signature_candidate_id:signatureId,confirm_avatar:Boolean(avatarId),confirm_signature:Boolean(signatureId)})});
    state.personaProfileApplyResult=result;const diagnostic=rememberAdminOperation("persona-template",result,"QQ 资料应用失败");alertFlash(result.status==="applied"?"ok":"info",diagnostic?.title||"QQ 资料应用完成");render();
  } catch(e){state.personaProfileApplyResult=operationDiagnosticFromError(e,"QQ 资料应用失败");rememberAdminOperation("persona-template",state.personaProfileApplyResult);alertFlash("err",state.personaProfileApplyResult.title);render();}
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
    if (state.personaTemplateResult) state.personaTemplateResult.history_record = {record_id: record.record_id};
    state.personaTemplateTask = null;
    state.personaTemplateEditing = false;
    render();
  } catch (e) {
    alertFlash("err", "读取历史失败：" + e.message);
  }
}

async function savePersonaTemplateEdit() {
  const result = state.personaTemplateResult;
  const recordId = result && result.history_record && result.history_record.record_id;
  const editor = document.getElementById("persona-template-editor");
  if (!recordId || !editor) return;
  try {
    const record = await api("/persona-template/history/" + encodeURIComponent(recordId), {method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify({template:editor.value})});
    const diagnostic=rememberAdminOperation("persona-template",record,"人设 YAML 保存未完成");
    state.personaTemplateResult = record.result || null;
    if (state.personaTemplateResult) state.personaTemplateResult.history_record = {record_id:record.record_id};
    state.personaTemplateEditing = false;
    await refreshPersonaTemplateHistory();
    alertFlash("ok", diagnostic?.title||"人设 YAML 已保存");
  } catch (e) { const diagnostic=rememberAdminOperation("persona-template",e,"人设 YAML 保存未完成");alertFlash("err",diagnostic?.title||"人设 YAML 保存未完成");render(); }
}

async function deletePersonaTemplateHistory(recordId, name) {
  if (!recordId || !confirm(`确认删除已构建人设「${name||recordId}」？相关头像候选也会清理。`)) return;
  try {
    const result=await api("/persona-template/history/" + encodeURIComponent(recordId), {method:"DELETE"});
    const diagnostic=rememberAdminOperation("persona-template",result,"人设记录删除未完成");
    const current = state.personaTemplateResult && state.personaTemplateResult.history_record;
    if (current && current.record_id === recordId) state.personaTemplateResult = null;
    state.personaTemplateEditing = false;
    await refreshPersonaTemplateHistory();
    alertFlash(diagnostic?.partial?"info":"ok",diagnostic?.title||"已删除人设记录");
  } catch (e) { const diagnostic=rememberAdminOperation("persona-template",e,"人设记录删除未完成");alertFlash("err",diagnostic?.title||"人设记录删除未完成");render(); }
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
        const diagnostic=rememberAdminOperation("persona-template",last,"人设模板构建未完成");
        alertFlash("ok", diagnostic?.title||"人设模板已生成");
        await refreshPersonaTemplateHistory();
        break;
      }
      if (last.status === "error") {
        rememberAdminOperation("persona-template",last,"人设模板构建未完成");
        alertFlash("err",last.title||last.message||"人设模板构建未完成");
        break;
      }
      render();
    }
  } catch (e) {
    const diagnostic=rememberAdminOperation("persona-template",e,"人设模板构建未完成");alertFlash("err",diagnostic?.title||"人设模板构建未完成");
  }
  state.personaTemplateBusy = false; render();
}

async function submitCorrection(uid) {
  const field = (document.getElementById("corr-field")?.value||"").trim();
  const value = (document.getElementById("corr-value")?.value||"").trim();
  if (!field || !value) { alertFlash("err", "请填写字段与更正值"); return; }
  try {
    const result=await api("/personas/"+encodeURIComponent(uid)+"/correction", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({corrections:{[field]:value}})});
    const diagnostic=rememberAdminOperation("persona",result,"画像更正未完成");alertFlash(diagnostic?.partial?"info":"ok",diagnostic?.title||"已提交更正");
    await loadPersonaDetail(uid);
    render();
  } catch (e) { const diagnostic=rememberAdminOperation("persona",e,"画像更正未完成");alertFlash("err",diagnostic?.title||"画像更正未完成");render(); }
}

async function refreshAvatarAnalysis(uid) {
  if (!confirm("重新下载并分析该用户当前 QQ 头像？这会调用一次可用的 vision route。")) return;
  state.avatarAnalysisBusy = `refresh:${uid}`;
  render();
  try {
    const result = await api(`/personas/${encodeURIComponent(uid)}/avatar-analysis/refresh`, {method:"POST"});
    const diagnostic = rememberAdminOperation("persona", result, "头像重新分析未排队");
    alertFlash("ok", diagnostic?.title || "头像重新分析已排队");
    await loadPersonaDetail(uid);
  } catch (e) {
    const diagnostic = rememberAdminOperation("persona", e, "头像重新分析未排队");
    alertFlash("err", diagnostic?.title || "头像重新分析未排队");
  } finally {
    state.avatarAnalysisBusy = "";
    render();
  }
}

async function clearAvatarAnalysis(uid) {
  if (!confirm("删除该用户已持久化的头像分析与安全摘要？")) return;
  state.avatarAnalysisBusy = `clear:${uid}`;
  render();
  try {
    const result = await api(`/personas/${encodeURIComponent(uid)}/avatar-analysis`, {method:"DELETE"});
    const diagnostic = rememberAdminOperation("persona", result, "头像分析删除未完成");
    alertFlash(diagnostic?.partial ? "info" : "ok", diagnostic?.title || "头像分析已删除");
    await loadPersonaDetail(uid);
  } catch (e) {
    const diagnostic = rememberAdminOperation("persona", e, "头像分析删除未完成");
    alertFlash("err", diagnostic?.title || "头像分析删除未完成");
  } finally {
    state.avatarAnalysisBusy = "";
    render();
  }
}

function renderGroupSwitch() {
  const list = state.groupSwitches || [];
  const sourceLabel = {config_file:"配置文件", dynamic:"动态", group_config:"群配置", none:""};
  const rows = list.map(g => {
    const statusBadge = g.enabled
      ? `<span class="tag tag--status" style="background:rgba(52,211,153,0.18);color:var(--ok)">启用</span>`
      : `<span class="tag tag--status" style="background:rgba(248,113,113,0.12);color:var(--danger)">禁用</span>`;
    const srcTag = sourceLabel[g.source]
      ? `<span class="tag">${escapeHtml(sourceLabel[g.source])}</span>`
      : '';
    let actionBtn;
    if (g.readonly) {
      actionBtn = `<button class="btn small" disabled title="由配置文件固定，无法在此修改">固定启用</button>`;
    } else if (g.enabled) {
      actionBtn = `<button class="btn small danger" aria-label="禁用群 ${escapeAttr(g.group_name || g.group_id)}" onclick="disableGroup('${escapeAttr(g.group_id)}')">禁用</button>`;
    } else {
      actionBtn = `<button class="btn small primary" aria-label="启用群 ${escapeAttr(g.group_name || g.group_id)}" onclick="enableGroup('${escapeAttr(g.group_id)}')">启用</button>`;
    }
    return `<tr>
      <td class="col-avatar"><img class="avatar" src="https://p.qlogo.cn/gh/${encodeURIComponent(g.group_id)}/${encodeURIComponent(g.group_id)}/100/" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
      <td class="col-id"><code class="u-atomic u-tabular">${escapeHtml(g.group_id)}</code></td>
      <td class="col-model"><span class="u-clamp-2" title="${escapeAttr(g.group_name || '')}">${escapeHtml(g.group_name || '')}</span></td>
      <td class="col-status">${statusBadge}${srcTag}</td>
      <td class="col-actions">${actionBtn}</td>
    </tr>`;
  }).join("");
  const enabledCount = list.filter(g => g.enabled).length;
  return `${renderAdminOperations("group","群开关操作诊断")}<div class="card">
    <div class="between" style="margin-bottom:14px">
      <h2 style="margin:0">群开关（${enabledCount} / ${list.length} 启用）</h2>
    </div>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群开关列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-avatar"><span class="sr-only">群头像</span></th><th scope="col" class="col-id">群号</th><th scope="col" class="col-model">群名</th><th scope="col" class="col-status">状态</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="muted">暂无群数据</td></tr>'}</tbody></table></div>
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
    const result=await api("/groups/" + encodeURIComponent(gid) + "/whitelist", { method: "POST" });
    const diagnostic=rememberAdminOperation("group",result,"群启用未完成");alertFlash("ok",diagnostic?.title||("已启用群 "+gid));
    const data = await api("/groups/whitelist");
    state.groupSwitches = data.groups;
    render();
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群启用未完成");alertFlash("err",diagnostic?.title||"群启用未完成");render(); }
}

async function disableGroup(gid) {
  try {
    const result=await api("/groups/" + encodeURIComponent(gid) + "/whitelist", { method: "DELETE" });
    const diagnostic=rememberAdminOperation("group",result,"群禁用未完成");alertFlash("ok",diagnostic?.title||("已禁用群 "+gid));
    const data = await api("/groups/whitelist");
    state.groupSwitches = data.groups;
    render();
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群禁用未完成");alertFlash("err",diagnostic?.title||"群禁用未完成");render(); }
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
      ? `<span class="tag tag--status" style="background:rgba(245,158,11,0.12);color:var(--warn);font-size:11px">无数据</span>`
      : '';
    return `<tr>
      <td class="col-avatar"><img class="avatar" src="https://p.qlogo.cn/gh/${encodeURIComponent(g.group_id)}/${encodeURIComponent(g.group_id)}/100/" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
      <td class="col-id"><code class="u-atomic u-tabular">${escapeHtml(g.group_id)}</code></td>
      <td class="col-model"><span class="u-clamp-2" title="${escapeAttr(g.group_name || '')}">${escapeHtml(g.group_name || '')}</span> ${srcTag} ${memTag}</td>
      <td class="col-status">${renderFavorabilityBadge(g.favorability)}</td>
      <td class="col-actions"><button class="btn small" aria-label="查看群 ${escapeAttr(g.group_name || g.group_id)}" onclick="openGroup('${escapeAttr(g.group_id)}')">查看</button></td>
    </tr>`;
  }).join("");
  return `<div class="card"><h2>群列表（${state.groupList.length}）</h2>
    <p class="muted" style="font-size:12px;margin-top:0">同时显示已建立记忆的群和白名单中的群（包括关闭搜索可找到的群）。</p>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-avatar"><span class="sr-only">群头像</span></th><th scope="col" class="col-id">群号</th><th scope="col" class="col-model">群名</th><th scope="col" class="col-status">群好感</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="muted">暂无群数据</td></tr>'}</tbody></table></div></div>`;
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
    const result=await api("/groups/" + encodeURIComponent(gid) + "/aliases/" + encodeURIComponent(uid), {
      method: "PUT",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({ aliases: splitAliasInput(draft.aliasesText), note: draft.note || "" }),
    });
    const diagnostic=rememberAdminOperation("group",result,"群成员称呼保存未完成");
    if (state.groupAliasDrafts) delete state.groupAliasDrafts[String(uid)];
    await refreshGroupDetailLight();
    alertFlash(diagnostic?.partial?"info":"ok",diagnostic?.title||"已保存群成员外号");
    render();
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群成员称呼保存未完成");alertFlash("err",diagnostic?.title||"群成员称呼保存未完成");render(); }
}

async function clearGroupMemberAliases(uid) {
  const gid = state.selectedGroup;
  if (!gid || !uid) return;
  if (!confirm("清空该成员在本群的外号映射？")) return;
  try {
    const result=await api("/groups/" + encodeURIComponent(gid) + "/aliases/" + encodeURIComponent(uid), { method: "DELETE" });
    const diagnostic=rememberAdminOperation("group",result,"群成员称呼删除未完成");
    if (state.groupAliasDrafts) delete state.groupAliasDrafts[String(uid)];
    await refreshGroupDetailLight();
    alertFlash(diagnostic?.partial?"info":"ok",diagnostic?.title||"已清空群成员外号");
    render();
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群成员称呼删除未完成");alertFlash("err",diagnostic?.title||"群成员称呼删除未完成");render(); }
}

async function rebuildGroupKnowledge() {
  const gid = state.selectedGroup;
  if (!gid) return;
  if (state.groupKnowledgeRebuilding) return;
  state.groupKnowledgeRebuilding = true; render();
  try {
    const out = await api("/groups/" + encodeURIComponent(gid) + "/knowledge/rebuild", { method:"POST", headers:{"content-type":"application/json"}, body: "{}" });
    const diagnostic=rememberAdminOperation("group",out,"群知识重建未完成");alertFlash("ok",diagnostic?.title||("已重建群知识库，新增 "+(out.saved||0)+" 条"));
    const knowledge = await api("/groups/" + encodeURIComponent(gid) + "/knowledge");
    state.groupKnowledge = knowledge.knowledge || [];
    state.groupKnowledgeAutobuild = knowledge.autobuild_status || null;
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群知识重建未完成");alertFlash("err",diagnostic?.title||"群知识重建未完成"); }
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
    ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="Agent 显著记忆"><table class="data-table wide" style="font-size:12.5px"><thead><tr><th scope="col" class="col-status">类型</th><th scope="col" class="col-summary">摘要</th><th scope="col" class="col-number">显著度</th><th scope="col" class="col-date">更新</th></tr></thead><tbody>${
        memories.map(m => `<tr>
          <td class="col-status"><span class="tag tag--ellipsis" title="${escapeAttr(m.memory_type || '')}">${escapeHtml(m.memory_type || '')}</span></td>
          <td class="col-summary u-wrap">${escapeHtml(m.summary || '')}</td>
          <td class="col-number muted u-atomic u-tabular">${Number(m.salience||0).toFixed(2)}</td>
          <td class="col-date muted u-atomic u-tabular">${m.updated_at ? new Date(m.updated_at*1000).toLocaleDateString() : '-'}</td>
        </tr>`).join('')
      }</tbody></table></div>`
    : '<p class="muted" style="margin:6px 0 0">暂无显著记忆条目</p>';
  const edgeBlock = edges.length
    ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="Agent 群内关系"><table class="data-table wide" style="font-size:12.5px"><thead><tr><th scope="col" class="col-summary">关系</th><th scope="col" class="col-status">类型</th><th scope="col" class="col-number">权重</th><th scope="col" class="col-date">最近</th></tr></thead><tbody>${
        edges.map(e => `<tr>
          <td class="col-summary">
            <code class="u-atomic u-tabular">${escapeHtml(e.src)}</code>${e.src_label && e.src_label !== e.src ? ` <span class="muted u-clamp-2">${escapeHtml(e.src_label)}</span>` : ''}
            →
            <code class="u-atomic u-tabular">${escapeHtml(e.dst)}</code>${e.dst_label && e.dst_label !== e.dst ? ` <span class="muted u-clamp-2">${escapeHtml(e.dst_label)}</span>` : ''}
          </td>
          <td class="col-status"><span class="tag tag--ellipsis" title="${escapeAttr(e.kind)}">${escapeHtml(e.kind)}</span></td>
          <td class="col-number u-atomic u-tabular">${Number(e.weight||0).toFixed(2)}</td>
          <td class="col-date muted u-atomic u-tabular">${e.last_seen_at ? new Date(e.last_seen_at*1000).toLocaleDateString() : '-'}</td>
        </tr>`).join('')
      }</tbody></table></div>`
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
    <td class="col-model"><strong class="u-clamp-2" title="${escapeAttr(k.term)}">${escapeHtml(k.term)}</strong></td>
    <td class="col-description u-wrap">${escapeHtml(k.definition)}</td>
    <td class="col-status"><span class="tag tag--ellipsis" title="${escapeAttr(k.memory_type || k.source_kind || '')}">${escapeHtml(k.memory_type || k.source_kind || '')}</span></td>
    <td class="col-date muted u-atomic u-tabular" style="font-size:12px">${k.updated_at ? new Date(k.updated_at*1000).toLocaleDateString() : '-'}</td>
  </tr>`).join("");
  let autoLine = '';
  if (auto) {
    const lastRun = auto.last_run_at ? new Date(auto.last_run_at*1000).toLocaleString() : '从未运行';
    const flag = auto.enabled ? '已启用' : '已禁用';
    autoLine = `<p class="muted" style="font-size:12px;margin:4px 0 10px">
      自动构建：${flag} · 上次运行 ${escapeHtml(lastRun)} · 今日 ${auto.daily_count||0}/${auto.daily_limit||0} 次 · 每 ${auto.interval_hours||0}h · 阈值 ${auto.min_messages_threshold||0} 条
      ${auto.daily_limit_hit ? '<span class="tag tag--status" style="background:rgba(245,158,11,0.18);color:var(--warn)">今日已满</span>' : ''}
    </p>`;
  }
  return `<div class="card">
    <div class="between"><h2 style="margin:0">群知识库（${knowledge.length}）</h2>
      <button class="btn small ${rebuilding?'':'primary'}" onclick="rebuildGroupKnowledge()" ${rebuilding?'disabled':''}>${rebuilding?'重建中…':'立即重建'}</button>
    </div>
    ${autoLine}
    ${knowledgeRows ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群知识库"><table class="data-table wide"><thead><tr><th scope="col" class="col-model">术语</th><th scope="col" class="col-description">解释</th><th scope="col" class="col-status">类型</th><th scope="col" class="col-date">更新</th></tr></thead><tbody>${knowledgeRows}</tbody></table></div>` : '<p class="muted">暂无群知识。可点击「立即重建」手动触发分析，或开启「群知识库自动构建」后等待定时扫描。</p>'}
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
    const diagnostic=rememberAdminOperation("group",out,"群作息保存未完成");alertFlash(diagnostic?.partial?"info":"ok",diagnostic?.title||"群作息已保存");
    render();
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群作息保存未完成");alertFlash("err",diagnostic?.title||"群作息保存未完成");render(); }
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
    rememberAdminOperation("group",out,"群作息生成未完成");
    const saved = await api("/groups/" + encodeURIComponent(gid) + "/schedule", {
      method:"PUT",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ enabled: true, schedule_prompt: out.schedule_prompt || "" }),
    });
    state.groupSchedule = saved;
    const diagnostic=rememberAdminOperation("group",saved,"群作息保存未完成");alertFlash("ok",diagnostic?.title||"已自动生成并启用群作息");
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群作息自动生成未完成");alertFlash("err",diagnostic?.title||"群作息自动生成未完成"); }
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
    <div class="member-alias-title" title="${escapeAttr(p.nickname || names[0] || "")}">${escapeHtml(p.nickname || names[0] || "") || '<span class="muted">无昵称</span>'}</div>
    ${nameTags}
    <input type="text" placeholder="外号，如：老王、车神" value="${escapeAttr(draft.aliasesText || "")}" oninput="setGroupAliasDraft('${escapeAttr(p.user_id)}','aliasesText',this.value)">
    <input type="text" placeholder="备注（可选）" value="${escapeAttr(draft.note || "")}" oninput="setGroupAliasDraft('${escapeAttr(p.user_id)}','note',this.value)">
    <div class="member-alias-actions">
      <button class="btn small primary" aria-label="保存 QQ ${escapeAttr(p.user_id)} 的群称呼" onclick="saveGroupMemberAliases('${escapeAttr(p.user_id)}')">保存</button>
      ${hasSaved ? `<button class="btn small" aria-label="清空 QQ ${escapeAttr(p.user_id)} 的群称呼" onclick="clearGroupMemberAliases('${escapeAttr(p.user_id)}')">清空</button>` : ''}
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
      <td class="col-avatar"><img class="avatar" src="https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(p.user_id)}&spec=100" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
      <td class="col-id"><code class="u-atomic u-tabular">${escapeHtml(p.user_id)}</code></td>
      <td>${renderMemberAliasEditor(p)}</td>
      <td class="col-status">${renderFavorabilityBadge(p.favorability)}</td>
      <td class="col-description">${renderMemberRelationDigest(p)}</td>
      <td class="col-summary u-wrap">${emoCol}</td>
      <td class="col-date u-atomic u-tabular">${p.updated_at ? new Date(p.updated_at*1000).toLocaleDateString() : '-'}</td>
    </tr>`;
  }).join("");
  const style = state.groupStyle || {};
  const memeRows = (state.groupMemes || []).map(m => `<tr>
    <td class="col-model"><strong class="u-clamp-2" title="${escapeAttr(m.term)}">${escapeHtml(m.term)}</strong></td>
    <td class="col-description u-wrap">${escapeHtml(m.meaning)}</td>
    <td class="col-summary u-wrap">${escapeHtml((m.aliases||[]).join("、"))}</td>
    <td class="col-status muted u-atomic u-tabular" style="font-size:12px">${escapeHtml(m.scope || '')}/${escapeHtml(m.risk_level || '')}/${Number(m.confidence||0).toFixed(2)}</td>
  </tr>`).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedGroup=null;state.groupRawChat=null;state.groupFavorability=null;state.groupStyleSnapIdx=0;state.groupAliasDrafts={};render()">返回列表</button><span class="muted">群 ${escapeHtml(gid)}</span></div>
    ${renderAdminOperations("group","群管理操作诊断")}
    ${renderFavorabilityCard(state.groupFavorability, "群好感度")}
    ${renderGroupAgentState()}
    ${renderGroupScheduleCard()}
    ${renderGroupStyle(style)}
    ${renderGroupKnowledgeCard()}
    <div class="card"><h2>梗词典 / 概念锚点（${(state.groupMemes||[]).length}）</h2>
      <p class="muted" style="font-size:12px;margin-top:0">词条会持久保留；列表只是当前读取视图，不会因为数量变多自动清理旧梗。</p>
      ${memeRows ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群梗词典"><table class="data-table wide"><thead><tr><th scope="col" class="col-model">词条</th><th scope="col" class="col-description">含义</th><th scope="col" class="col-summary">别名</th><th scope="col" class="col-status">范围/风险/置信度</th></tr></thead><tbody>${memeRows}</tbody></table></div>` : '<p class="muted">暂无匹配词条，公共热梗种子会在首次查询后自动初始化。</p>'}</div>
    <div class="card"><h2>群内成员理解（${state.groupPersonas.length}）</h2>
      <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群内成员理解"><table class="group-member-understanding data-table xwide"><thead><tr><th scope="col" class="col-avatar"><span class="sr-only">头像</span></th><th scope="col" class="col-id">QQ</th><th scope="col" class="col-summary">称呼 / 外号</th><th scope="col" class="col-status">好感度</th><th scope="col" class="col-description">关系与画像</th><th scope="col" class="col-summary">近期情绪</th><th scope="col" class="col-date">更新</th></tr></thead><tbody>${rows||'<tr><td colspan="7" class="muted">无</td></tr>'}</tbody></table></div></div>
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
    return `<tr><td class="muted u-atomic" style="width:80px">${escapeHtml(label)}</td><td class="col-description u-wrap">${escapeHtml(String(value))}</td></tr>`;
  }).join("");
  return `<div class="card"><div class="between"><h2 style="margin:0">群风格（${snapshots.length} 个快照）</h2>
    <button class="btn small ${rebuilding?'':'primary'}" onclick="rebuildGroupStyle()" ${rebuilding?'disabled':''}>${rebuilding?'分析中…':'立即重新分析'}</button></div>
    <div class="group-bar" style="margin-top:10px">${tabs}</div>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群风格结构化字段"><table class="data-table compact" style="margin-top:8px"><tbody>${detailRows}</tbody></table></div>
    ${active.style_text ? `<details style="margin-top:8px"><summary class="muted" style="cursor:pointer;font-size:12px">展示原始 prompt 段</summary>
      <pre class="u-pre-wrap code-scroll" style="margin:8px 0 0;font-family:inherit;font-size:12.5px">${escapeHtml(active.style_text)}</pre></details>` : ''}
  </div>`;
}

async function rebuildGroupStyle() {
  const gid = state.selectedGroup;
  if (!gid) return;
  state.groupStyleRebuilding = true; render();
  try {
    const out = await api("/groups/" + encodeURIComponent(gid) + "/style/rebuild", { method:"POST", headers:{"content-type":"application/json"}, body: "{}" });
    const diagnostic=rememberAdminOperation("group",out,"群风格分析未完成");
    state.groupStyle = { ...state.groupStyle, snapshots: out.snapshots };
    state.groupStyleSnapIdx = 0;
    alertFlash("ok",diagnostic?.title||"已生成新群风格快照");
  } catch (e) { const diagnostic=rememberAdminOperation("group",e,"群风格分析未完成");alertFlash("err",diagnostic?.title||"群风格分析未完成"); }
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
    const tag = isBot ? '<span class="tag tag--status" style="background:rgba(106,168,255,0.18);color:var(--accent)">bot</span>' : '<span class="tag tag--status">user</span>';
    const sender = m.sender_name || m.user_id || '匿名';
    const time = m.created_at ? new Date(m.created_at*1000).toLocaleString() : '-';
    return `<tr><td class="col-status">${tag}</td>
      <td class="col-model muted"><span class="u-clamp-2" title="${escapeAttr(sender)}" style="font-size:12px">${escapeHtml(sender)}</span></td>
      <td class="col-description u-pre-wrap">${escapeHtml(m.text)}</td>
      <td class="col-time muted u-atomic u-tabular" style="font-size:11px">${escapeHtml(time)}</td></tr>`;
  }).join("");
  return `<div class="card"><h2>对话原文（${chat.messages.length}）</h2>
    <p class="muted" style="font-size:12px;margin:-6px 0 10px">按时间正序显示；不参与 LLM 上下文，仅供管理员查看。</p>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群对话原文"><table class="data-table wide"><thead><tr><th scope="col" class="col-status">角色</th><th scope="col" class="col-model">发送者</th><th scope="col" class="col-description">内容</th><th scope="col" class="col-time">时间</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
    <div style="margin-top:10px">
      <button class="btn small" onclick="state.groupRawChat=null;render()">收起</button>
      <button class="btn small" onclick="loadGroupRawChat()">刷新</button>
    </div>
  </div>`;
}
