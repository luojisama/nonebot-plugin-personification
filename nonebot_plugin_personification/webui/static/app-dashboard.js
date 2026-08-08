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
