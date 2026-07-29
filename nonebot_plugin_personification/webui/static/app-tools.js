const _SKILL_OPERATION_RESULT_STORAGE_KEY = "personification_skill_operation_result_v1";

function persistSkillOperationResult(input) {
  const result = input && input.diagnostic && typeof input.diagnostic === "object" ? input.diagnostic : input;
  state.skillOperationResult = result && typeof result === "object" ? result : null;
  try {
    if (state.skillOperationResult) sessionStorage.setItem(_SKILL_OPERATION_RESULT_STORAGE_KEY, JSON.stringify(state.skillOperationResult));
    else sessionStorage.removeItem(_SKILL_OPERATION_RESULT_STORAGE_KEY);
  } catch {}
}

function clearSkillOperationResult() {
  persistSkillOperationResult(null);
  render();
}

try {
  const savedSkillOperationResult = JSON.parse(sessionStorage.getItem(_SKILL_OPERATION_RESULT_STORAGE_KEY) || "null");
  if (savedSkillOperationResult && typeof savedSkillOperationResult === "object") state.skillOperationResult = savedSkillOperationResult;
} catch {
  try { sessionStorage.removeItem(_SKILL_OPERATION_RESULT_STORAGE_KEY); } catch {}
}

function renderSkillOperationResult() {
  const result = state.skillOperationResult;
  if (!result) return "";
  return `<div class="card">
    <div class="between" style="gap:12px;align-items:flex-start">
      <div><h2 style="margin:0">最近一次 Skill 操作</h2><p class="muted" style="font-size:12px;margin:6px 0 0">刷新页面后仍保留；这里只保存服务端返回的脱敏 diagnostic。</p></div>
      <button class="btn small" onclick="clearSkillOperationResult()">清除</button>
    </div>
    <div style="margin-top:12px">${renderOperationDiagnostic(result)}</div>
  </div>`;
}

function renderSkills() {
  const operationResult = renderSkillOperationResult();
  if (state.skillsAvailable === false) return `${operationResult}<div class="card muted">tool_registry 未就绪</div>${renderRemoteSkillSources()}${renderLegacyMcpTools()}`;
  const search = (state.skillFilter || "").trim().toLowerCase();
  const items = search ? state.skills.filter(s => {
    const hay = [s.name, s.description, s.category, s.source_kind, s.mcp ? "mcp" : ""].join(" ").toLowerCase();
    return hay.includes(search);
  }) : state.skills;
  const rows = items.map(s => {
    const active = s.enabled_by_config && !s.user_disabled && !s.health_disabled;
    const health = s.health || {};
    const healthChecked = Number(health.last_checked_at || 0);
    const healthTime = healthChecked ? new Date(healthChecked * 1000).toLocaleString() : "";
    const healthDetail = healthChecked
      ? `<div class="muted" style="font-size:11.5px;margin-top:4px">巡检 ${escapeHtml(healthTime)} · ${Number(health.latency_ms || 0)}ms${health.last_error ? ` · ${escapeHtml(String(health.last_error).slice(0,120))}` : ""}</div>`
      : "";
    const tags = [
      s.category ? `<span class="tag tag--ellipsis" title="${escapeAttr(s.category)}">${escapeHtml(s.category)}</span>` : "",
      s.source_kind ? `<span class="tag tag--ellipsis" title="${escapeAttr(s.source_kind)}">${escapeHtml(s.source_kind)}</span>` : "",
      s.mcp ? '<span class="tag source-runtime_config">MCP</span>' : "",
      s.local === false && !s.mcp ? '<span class="tag">remote</span>' : "",
      s.health_disabled ? '<span class="tag required">巡检屏蔽</span>' : "",
      healthChecked && !s.health_disabled ? '<span class="tag">巡检可用</span>' : "",
    ].filter(Boolean).join("");
    const status = active
      ? '<span class="tag tag--status" style="background:rgba(52,211,153,0.18);color:var(--ok)">启用</span>'
      : s.health_disabled
        ? '<span class="tag required">临时屏蔽</span>'
        : '<span class="tag tag--status" style="background:rgba(248,113,113,0.18);color:var(--danger)">禁用</span>';
    return `<tr>
      <td class="col-id"><strong class="u-ellipsis" title="${escapeAttr(s.name)}">${escapeHtml(s.name)}</strong><div style="margin-top:4px">${tags}</div></td>
      <td class="col-description muted u-wrap" style="font-size:12.5px">${escapeHtml((s.description||"").slice(0,140))}${healthDetail}</td>
      <td class="col-status">${status}</td>
      <td class="col-actions">
        <div class="toggle">
          <button class="${!s.user_disabled?'on':''}" aria-label="启用 Skill ${escapeAttr(s.name)}" onclick="toggleSkill('${escapeAttr(s.name)}', false)">开</button>
          <button class="${s.user_disabled?'on':''}" aria-label="禁用 Skill ${escapeAttr(s.name)}" onclick="toggleSkill('${escapeAttr(s.name)}', true)">关</button>
        </div>
      </td>
    </tr>`;
  }).join("");
  return `${operationResult}${renderSkillSummary()}
    <div class="toolbar">
      <input id="skill-filter-input" type="search" placeholder="搜索 skill 名称…" value="${escapeAttr(state.skillFilter)}" oninput="state.skillFilter=this.value;render()" style="flex:1;max-width:340px">
      <span class="muted">共 ${state.skills.length} 个 skill</span>
      <button class="btn" onclick="reloadSkillRuntime()" ${state.skillSummary && state.skillSummary.reload_available ? "" : "disabled"}>重载 Skill</button>
    </div>
    ${renderRemoteSkillSources()}
    ${renderLegacyMcpTools()}
    <div class="card"><h2>Skill 启停</h2>
      <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="Skill 启停列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-id">名称</th><th scope="col" class="col-description">说明</th><th scope="col" class="col-status">状态</th><th scope="col" class="col-actions">开关</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="muted">无 skill</td></tr>'}</tbody></table></div>
    </div>`;
}

function renderSkillSummary() {
  const s = state.skillSummary || {};
  const remoteStatus = s.remote_enabled
    ? '<span class="device-status approved">已开</span>'
    : '<span class="device-status pending">关闭</span>';
  const unsafe = s.allow_unsafe_external
    ? '<span class="tag required">允许非隔离</span>'
    : '<span class="tag">隔离优先</span>';
  const review = s.require_admin_review
    ? '<span class="tag source-runtime_config">需审核</span>'
    : '<span class="tag required">免审核</span>';
  return `<div class="skill-summary">
    <div class="skill-stat"><span class="muted">可用工具</span><strong>${Number(s.active || 0)}</strong><small>/ ${Number(s.total || 0)}</small></div>
    <div class="skill-stat"><span class="muted">用户禁用</span><strong>${Number(s.user_disabled || 0)}</strong></div>
    <div class="skill-stat"><span class="muted">巡检屏蔽</span><strong>${Number(s.health_disabled || 0)}</strong><small>5h 复测</small></div>
    <div class="skill-stat"><span class="muted">远程源</span><strong>${Number(s.remote_sources_enabled || 0)}</strong><small>${remoteStatus}</small></div>
    <div class="skill-stat"><span class="muted">待审核</span><strong>${Number(s.remote_pending || 0)}</strong><small>${review}</small></div>
    <div class="skill-stat"><span class="muted">MCP</span><strong>${Number(s.mcp_tools || 0)}</strong><small>stdio</small></div>
    <div class="skill-stat"><span class="muted">外部执行</span><strong style="font-size:13px">${unsafe}</strong></div>
  </div>`;
}

function _remoteStatusTag(status) {
  const st = String(status || "pending").toLowerCase();
  if (st === "approved") return '<span class="device-status approved">通过</span>';
  if (st === "rejected") return '<span class="device-status pending" style="background:rgba(248,113,113,0.18);color:var(--danger)">拒绝</span>';
  if (st === "disabled") return '<span class="tag">未启用</span>';
  return '<span class="device-status pending">待审</span>';
}

function renderRemoteSkillSources() {
  const sources = state.skillRemoteSources || [];
  const s = state.skillSummary || {};
  const rows = sources.map(item => {
    const selector = item.key || item.name || item.source;
    return `<tr>
      <td class="col-id"><strong class="u-ellipsis" title="${escapeAttr(item.name || ("source_" + (item.index + 1)))}">${escapeHtml(item.name || ("source_" + (item.index + 1)))}</strong><code class="u-ellipsis" title="${escapeAttr(item.key || "")}" style="font-size:11px">${escapeHtml(item.key || "")}</code></td>
      <td class="col-description u-wrap">${escapeHtml(item.source || "")}${item.ref ? `<br><span class="muted u-wrap">ref=${escapeHtml(item.ref)}</span>` : ""}${item.subdir ? `<br><span class="muted u-wrap">subdir=${escapeHtml(item.subdir)}</span>` : ""}<br><span class="muted u-atomic">digest=${escapeHtml((item.content_digest || "未准备").slice(0,16))}</span></td>
      <td class="col-status">${_remoteStatusTag(item.status)}</td>
      <td class="col-actions">
        <div class="row" style="gap:6px">
          <button class="btn small primary" aria-label="批准远程 Skill ${escapeAttr(item.name || selector)}" onclick="reviewRemoteSkill('${escapeAttr(selector)}','approved')" ${item.status==="approved"?"disabled":""}>批准</button>
          <button class="btn small" aria-label="将远程 Skill ${escapeAttr(item.name || selector)} 标为待审" onclick="reviewRemoteSkill('${escapeAttr(selector)}','pending')">待审</button>
          <button class="btn small danger" aria-label="拒绝远程 Skill ${escapeAttr(item.name || selector)}" onclick="reviewRemoteSkill('${escapeAttr(selector)}','rejected')">拒绝</button>
        </div>
      </td>
    </tr>`;
  }).join("");
  return `<div class="card">
    <div class="between" style="gap:12px;align-items:flex-start">
      <div><h2>远程 Skill 源</h2>
        <p class="muted" style="margin:4px 0 8px">批准前会先下载但不执行，并绑定当前完整内容 SHA-256；内容变化会自动回到待审。</p>
        <div class="row" style="gap:6px">
          ${s.remote_enabled ? '<span class="device-status approved">远程加载已开</span>' : '<span class="device-status pending">远程加载关闭</span>'}
          ${s.require_admin_review ? '<span class="tag source-runtime_config">管理员审核</span>' : '<span class="tag required">免审核</span>'}
          ${s.allow_unsafe_external ? '<span class="tag required">允许非隔离外部代码</span>' : '<span class="tag">非隔离外部代码关闭</span>'}
        </div>
      </div>
      <button class="btn small" onclick="setSkillRemoteEnabled(${s.remote_enabled ? "false" : "true"})">${s.remote_enabled ? "关闭远程" : "开启远程"}</button>
    </div>
    <div class="remote-source-form">
      <input id="skill-source-url" placeholder="GitHub / zip / 本地目录" value="${escapeAttr(state.skillSourceForm.source || "")}" oninput="state.skillSourceForm.source=this.value">
      <input id="skill-source-name" placeholder="名称" value="${escapeAttr(state.skillSourceForm.name || "")}" oninput="state.skillSourceForm.name=this.value">
      <input id="skill-source-ref" placeholder="ref" value="${escapeAttr(state.skillSourceForm.ref || "")}" oninput="state.skillSourceForm.ref=this.value">
      <input id="skill-source-subdir" placeholder="subdir" value="${escapeAttr(state.skillSourceForm.subdir || "")}" oninput="state.skillSourceForm.subdir=this.value">
      <label><input type="checkbox" ${state.skillSourceForm.preferFirst ? "checked" : ""} onchange="state.skillSourceForm.preferFirst=this.checked"> 优先</label>
      <label><input type="checkbox" ${state.skillSourceForm.autoApprove ? "checked" : ""} onchange="state.skillSourceForm.autoApprove=this.checked"> 添加后批准</label>
      <button class="btn primary" onclick="addRemoteSkillSource()">添加源</button>
    </div>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="远程 Skill 来源列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-id">名称</th><th scope="col" class="col-description">来源</th><th scope="col" class="col-status">审核</th><th scope="col" class="col-actions">操作</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="muted">暂无远程源</td></tr>'}</tbody></table></div>
  </div>`;
}

function renderLegacyMcpTools() {
  const tools = state.skillMcpTools || [];
  const rows = tools.map(t => `<tr>
    <td class="col-id"><strong class="u-ellipsis" title="${escapeAttr(t.name)}">${escapeHtml(t.name)}</strong><span class="muted u-ellipsis" title="${escapeAttr(t.remote_name || "-")}">remote=${escapeHtml(t.remote_name || "-")}</span></td>
    <td class="col-description"><code class="u-wrap">${escapeHtml(t.command || "-")}</code></td>
    <td class="col-description muted u-wrap">${escapeHtml(t.cwd || "-")}</td>
    <td class="col-number u-atomic u-tabular">${Number(t.timeout || 0)}s</td>
    <td class="col-number u-atomic u-tabular">${Number(t.args_count || 0)} / ${Number(t.env_count || 0)}</td>
  </tr>`).join("");
  return `<div class="card">
    <h2>Legacy Skill MCP 工具</h2>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="Legacy Skill MCP 工具列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-id">工具</th><th scope="col" class="col-description">命令</th><th scope="col" class="col-description">cwd</th><th scope="col" class="col-number">超时</th><th scope="col" class="col-number">args/env</th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="muted">当前未注册 MCP stdio 工具</td></tr>'}</tbody></table></div>
  </div>`;
}

async function toggleSkill(name, disabled) {
  try {
    const result = await api(`/skills/${encodeURIComponent(name)}/toggle`, { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({disabled}) });
    persistSkillOperationResult(result);
    alertFlash("ok", `${name} 已${disabled?'禁用':'启用'}`);
    await loadView(); render();
  } catch (e) {
    const diagnostic=operationDiagnosticFromError(e,"Skill 开关保存失败");persistSkillOperationResult(diagnostic);
    alertFlash("err",diagnostic.title||"Skill 开关保存失败");
    render();
  }
}

async function setSkillRemoteEnabled(enabled) {
  try {
    const result = await api("/skills/remote/toggle", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({enabled: !!enabled}) });
    persistSkillOperationResult(result);
    alertFlash("ok", enabled ? "远程 Skill 已开启" : "远程 Skill 已关闭");
    await loadView(); render();
  } catch (e) {
    const diagnostic=operationDiagnosticFromError(e,"远程 Skill 开关保存失败");persistSkillOperationResult(diagnostic);
    alertFlash("err",diagnostic.title||"远程 Skill 开关保存失败");
    render();
  }
}

async function addRemoteSkillSource() {
  const f = state.skillSourceForm || {};
  if (!String(f.source || "").trim()) { alertFlash("err", "source 不能为空"); return; }
  try {
    const payload = {
      source: f.source, name: f.name, ref: f.ref, subdir: f.subdir, kind: f.kind || "auto",
      prefer_first: !!f.preferFirst, auto_approve: !!f.autoApprove, enable_remote: true,
    };
    const result = await api("/skills/remote/source", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify(payload) });
    persistSkillOperationResult(result);
    state.skillSourceForm = { source: "", name: "", ref: "", subdir: "", kind: "auto", preferFirst: false, autoApprove: false };
    alertFlash("ok", result.auto_approved ? "远程源已添加并批准，重载后生效" : "远程源已添加，审核后重载生效");
    await loadView(); render();
  } catch (e) {
    const diagnostic=operationDiagnosticFromError(e,"远程 Skill 来源添加失败");persistSkillOperationResult(diagnostic);
    alertFlash("err",diagnostic.title||"远程 Skill 来源添加失败");
    render();
  }
}

async function reviewRemoteSkill(selector, status) {
  try {
    const result = await api("/skills/remote/review", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({selector, status}) });
    persistSkillOperationResult(result);
    alertFlash("ok", `已更新 ${result.matched_count || 0} 个远程源`);
    await loadView(); render();
  } catch (e) {
    const diagnostic=operationDiagnosticFromError(e,"远程 Skill 审核失败");persistSkillOperationResult(diagnostic);
    alertFlash("err",diagnostic.title||"远程 Skill 审核失败");
    render();
  }
}

async function reloadSkillRuntime() {
  try {
    const result = await api("/skills/reload", { method:"POST" });
    persistSkillOperationResult(result);
    alertFlash("ok", "Skill 运行时已重载");
    await loadView(); render();
  } catch (e) {
    const diagnostic=operationDiagnosticFromError(e,"Skill runtime 重载失败");persistSkillOperationResult(diagnostic);
    alertFlash("err",diagnostic.title||"Skill runtime 重载失败");
    render();
  }
}

function pluginKnowledgeStrategyLabel(value) {
  const v = String(value || "").toLowerCase();
  if (v === "full_source" || v === "full_source_single_unit") return "单次全量";
  if (v === "module_bundles" || v === "module_bundle_multistage") return "模块分批";
  if (v === "chunk_batches" || v === "chunk_batch_multistage") return "chunk 分批";
  return v || "未知策略";
}

function pluginKnowledgeCoverage(item) {
  if (!item) return {};
  return item.source_coverage || {};
}

function pluginKnowledgeCoverageText(c) {
  const files = Number(c.source_file_count || 0);
  const chunks = Number(c.source_chunk_count || 0);
  const chars = Number(c.source_chars || 0);
  const units = Number(c.analysis_unit_count || 0);
  const mode = c.analysis_mode || c.analysis_strategy || "";
  const parts = [];
  if (files || chunks) parts.push(`${files} 文件 / ${chunks} chunk`);
  if (chars) parts.push(`${chars.toLocaleString()} 字符`);
  if (units) parts.push(`${units} 个分析单元`);
  if (mode) parts.push(pluginKnowledgeStrategyLabel(mode));
  return parts.join(" · ") || "暂无源码覆盖统计";
}

function renderPluginKnowledgeCoverage(c) {
  const full = c.full_input ? '<span class="tag tag--status" style="background:rgba(52,211,153,0.18);color:var(--ok)">全量输入</span>' : '<span class="tag tag--status required">覆盖未确认</span>';
  const trunc = c.source_truncated ? '<span class="tag required">存在截断</span>' : '<span class="tag">未截断</span>';
  const complete = c.source_complete === false ? '<span class="tag required">快照不完整</span>' : '<span class="tag">完整快照</span>';
  return `<div style="margin:10px 0 12px">
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">${full}${complete}${trunc}<span class="tag">${escapeHtml(pluginKnowledgeStrategyLabel(c.analysis_mode || c.analysis_strategy))}</span></div>
    <div class="muted" style="font-size:12.5px">${escapeHtml(pluginKnowledgeCoverageText(c))}</div>
    <div class="muted" style="font-size:12.5px;margin-top:4px">${escapeHtml(c.note || "大型插件会拆成多个模型调用，但每个源码 chunk 都会进入分析，不按样本抽取。")}</div>
  </div>`;
}

function normalizePluginKnowledgeFeatures(features) {
  if (Array.isArray(features)) return features.map((item, index) => {
    if (typeof item === "string") return { key: `feature_${index + 1}`, title: item, summary: "", detail: "" };
    return Object.assign({ key: item.feature_key || item.key || `feature_${index + 1}` }, item || {});
  });
  if (features && typeof features === "object") {
    return Object.entries(features).map(([key, value]) => {
      if (value && typeof value === "object") return Object.assign({ key }, value);
      return { key, title: key, summary: String(value || ""), detail: "" };
    });
  }
  return [];
}

const _PLUGIN_KNOWLEDGE_DIAGNOSTIC_KEY="personification_plugin_knowledge_diagnostic_v1";
function rememberPluginKnowledgeDiagnostic(value) {
  const diagnostic=value&&value.diagnostic&&typeof value.diagnostic==="object"?value.diagnostic:value;
  if(!diagnostic||typeof diagnostic!=="object"||!diagnostic.code)return null;
  state.pluginKnowledgeDiagnostic=diagnostic;
  try{sessionStorage.setItem(_PLUGIN_KNOWLEDGE_DIAGNOSTIC_KEY,JSON.stringify(diagnostic));}catch{}
  return diagnostic;
}
function pluginKnowledgeDiagnosticCard() {
  let diagnostic=state.pluginKnowledgeDiagnostic;
  if(!diagnostic){try{diagnostic=JSON.parse(sessionStorage.getItem(_PLUGIN_KNOWLEDGE_DIAGNOSTIC_KEY)||"null");}catch{diagnostic=null;}}
  return diagnostic?`<div class="card"><div class="between"><h2>插件知识库诊断</h2><button class="btn small" onclick="state.pluginKnowledgeDiagnostic=null;try{sessionStorage.removeItem(_PLUGIN_KNOWLEDGE_DIAGNOSTIC_KEY)}catch{};render()">清空</button></div>${renderOperationDiagnostic(diagnostic)}</div>`:"";
}

function renderPluginKnowledgeSourceFiles(snapshot) {
  const files = snapshot && Array.isArray(snapshot.files) ? snapshot.files : [];
  if (!files.length) return "";
  const rows = files.slice(0, 120).map(f => `<tr>
    <td class="col-description"><code class="u-wrap">${escapeHtml(f.path || "")}</code></td>
    <td class="col-number muted u-atomic u-tabular">${Number(f.line_count || 0)}</td>
    <td class="col-number muted u-atomic u-tabular">${Number(f.size || 0).toLocaleString()}</td>
    <td class="col-description muted u-wrap">${escapeHtml((f.symbols || []).slice(0, 8).join(", "))}</td>
  </tr>`).join("");
  const more = files.length > 120 ? `<div class="muted" style="margin-top:6px">还有 ${files.length - 120} 个源码文件未在表格中展开，完整数据见 JSON。</div>` : "";
  return `<h3>源码文件</h3>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="插件知识源码文件"><table class="data-table wide"><thead><tr><th scope="col" class="col-description">文件</th><th scope="col" class="col-number">行数</th><th scope="col" class="col-number">字符</th><th scope="col" class="col-description">符号</th></tr></thead><tbody>${rows}</tbody></table></div>${more}`;
}

function renderPluginKnowledge() {
  const diagnosticCard=pluginKnowledgeDiagnosticCard();
  if (state.pluginKnowledgeAvailable === false) return `${diagnosticCard}<div class="card muted">knowledge_store 未就绪</div>`;
  if (state.selectedPluginKnowledge) return `${diagnosticCard}${renderPluginKnowledgeDetail()}`;
  const list = state.pluginKnowledgeList || [];
  const searchResults = state.pluginKnowledgeSearchResults;
  const matchedSet = searchResults ? new Set(searchResults.results || []) : null;
  const displayList = matchedSet ? list.filter(p => matchedSet.has(p.plugin_name)) : list;
  const rows = displayList.map(p => {
    const coverage = pluginKnowledgeCoverage(p);
    return `<tr>
    <td class="col-model"><strong class="u-clamp-2" title="${escapeAttr(p.display_name || p.plugin_name)}">${escapeHtml(p.display_name || p.plugin_name)}</strong>${p.category ? ` <span class="tag tag--ellipsis" title="${escapeAttr(p.category)}">${escapeHtml(p.category)}</span>` : ''}</td>
    <td class="col-id"><code class="u-ellipsis" title="${escapeAttr(p.plugin_name)}">${escapeHtml(p.plugin_name)}</code></td>
    <td class="col-summary u-wrap">${escapeHtml(p.summary || '')}</td>
    <td class="col-description muted" style="font-size:12px">
      ${p.has_runtime_data ? '<span class="tag">runtime</span>' : ''}
      ${p.has_source_data ? `<span class="tag">source(${p.source_file_count}f/${p.source_chunk_count}c)</span>` : ''}
      ${coverage.full_input ? '<span class="tag tag--status" style="background:rgba(52,211,153,0.18);color:var(--ok)">全量</span>' : ''}
      ${(p.analysis_mode || p.analysis_strategy) ? `<span class="tag">${escapeHtml(pluginKnowledgeStrategyLabel(p.analysis_mode || p.analysis_strategy))}</span>` : ''}
      ${p.source_chars ? `<span class="tag">${Number(p.source_chars || 0).toLocaleString()} chars</span>` : ''}
    </td>
    <td class="col-actions"><button class="btn small" aria-label="查看插件 ${escapeAttr(p.display_name || p.plugin_name)} 的知识详情" onclick="openPluginKnowledge('${escapeAttr(p.plugin_name)}')">详情</button></td>
  </tr>`;
  }).join("");
  const searchInfo = matchedSet ? `<div class="muted" style="margin-bottom:8px">搜索 "${escapeHtml(state.pluginKnowledgeSearchQ || '')}" 命中 ${matchedSet.size} 条 <button class="btn small" onclick="clearPluginKnowledgeSearch()">清除</button></div>` : '';
  return `${diagnosticCard}<div class="card">
    <div class="row" style="margin-bottom:12px;gap:8px;align-items:center">
      <input id="pk-search-input" placeholder="按插件名/关键词/摘要搜索" value="${escapeAttr(state.pluginKnowledgeSearchQ || '')}" onkeydown="if(event.key==='Enter')triggerPluginKnowledgeSearch()" style="flex:1">
      <button class="btn" onclick="triggerPluginKnowledgeSearch()">搜索</button>
    </div>
    <div class="muted" style="margin-bottom:10px;font-size:12.5px">构建说明：插件知识库读取每个插件根目录下完整可读 Python 源码。小插件可一次传入模型；大插件会按模块或 chunk 拆成多次分析，所有 chunk 都会参与，不做抽样。skeleton 预览可能截断，但源码快照与分析输入不截断。</div>
    ${searchInfo}
    <h2>插件知识库（${displayList.length} / ${list.length}）</h2>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="插件知识库列表"><table class="data-table xwide"><thead><tr><th scope="col" class="col-model">名称</th><th scope="col" class="col-id">plugin_name</th><th scope="col" class="col-summary">摘要</th><th scope="col" class="col-description">数据</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="muted">暂无插件知识，等待自动构建或手动触发。</td></tr>'}</tbody></table></div>
  </div>`;
}

async function triggerPluginKnowledgeSearch() {
  const input = document.getElementById("pk-search-input");
  const q = (input ? input.value : "").trim();
  state.pluginKnowledgeSearchQ = q;
  if (!q) { state.pluginKnowledgeSearchResults = null; render(); return; }
  try {
    state.pluginKnowledgeSearchResults = await api("/plugin-knowledge/search?" + new URLSearchParams({q, top_k: "30"}).toString());
    rememberPluginKnowledgeDiagnostic(state.pluginKnowledgeSearchResults);
    render();
  } catch (e) { const diagnostic=rememberPluginKnowledgeDiagnostic(operationDiagnosticFromError(e,"插件知识搜索未完成"));alertFlash("err",diagnostic?.title||"插件知识搜索未完成");render(); }
}

function clearPluginKnowledgeSearch() {
  state.pluginKnowledgeSearchQ = "";
  state.pluginKnowledgeSearchResults = null;
  render();
}

async function openPluginKnowledge(name) {
  try {
    state.selectedPluginKnowledge = await api("/plugin-knowledge/detail/" + encodeURIComponent(name));
    rememberPluginKnowledgeDiagnostic(state.selectedPluginKnowledge);
    render();
  } catch (e) { const diagnostic=rememberPluginKnowledgeDiagnostic(operationDiagnosticFromError(e,"插件知识详情读取未完成"));alertFlash("err",diagnostic?.title||"插件知识详情读取未完成");render(); }
}

function renderPluginKnowledgeDetail() {
  const d = state.selectedPluginKnowledge;
  const e = d.entry || {};
  const coverage = d.source_coverage || e.source_coverage || (d.source_snapshot && d.source_snapshot.source_coverage) || {};
  const features = normalizePluginKnowledgeFeatures(e.features);
  const featureRows = features.map(f => {
    const name = f.title || f.name || f.feature || f.key || "";
    const desc = f.summary || f.description || f.desc || f.detail || "";
    const meta = [
      f.key ? `<code class="u-ellipsis" title="${escapeAttr(f.key)}">${escapeHtml(f.key)}</code>` : "",
      f.files && f.files.length ? `<span class="muted">${escapeHtml(f.files.slice(0, 4).join(", "))}</span>` : "",
    ].filter(Boolean).join(" ");
    return `<li><strong>${escapeHtml(name)}</strong>${desc ? `：${escapeHtml(desc)}` : ''}${meta ? `<div style="font-size:12px;margin-top:3px">${meta}</div>` : ''}</li>`;
  }).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedPluginKnowledge=null;render()">返回列表</button><span class="muted">插件 ${escapeHtml(d.plugin_name)}</span></div>
    <div class="card">
      <h2>${escapeHtml(e.display_name || d.plugin_name)} <code style="font-size:13px;color:var(--muted)">${escapeHtml(d.plugin_name)}</code></h2>
      ${e.summary ? `<p>${escapeHtml(e.summary)}</p>` : ''}
      ${renderPluginKnowledgeCoverage(coverage)}
      ${(e.keywords && e.keywords.length) ? `<div style="margin:6px 0">${e.keywords.map(k => `<span class="tag">${escapeHtml(k)}</span>`).join("")}</div>` : ''}
      ${e.architecture_summary ? `<h3>架构摘要</h3><pre class="u-pre-wrap code-scroll" style="margin:0;font-family:inherit">${escapeHtml(e.architecture_summary)}</pre>` : ''}
      ${features.length ? `<h3>功能列表</h3><ul>${featureRows}</ul>` : ''}
      ${renderPluginKnowledgeSourceFiles(d.source_snapshot)}
      <details style="margin-top:12px"><summary class="muted">完整 JSON</summary><pre class="u-pre-wrap code-scroll" style="font-size:12px;background:#0b0d12;padding:10px;border-radius:6px">${escapeHtml(JSON.stringify(e, null, 2))}</pre></details>
    </div>`;
}

function pluginCommitRows(items, emptyText) {
  const rows = (items || []).map(item => {
    const ts = Number(item.timestamp || 0);
    const time = ts ? new Date(ts * 1000).toLocaleString() : "-";
    return `<tr>
      <td class="col-id"><code class="u-atomic" title="${escapeAttr(item.short_hash || "")}">${escapeHtml(item.short_hash || "")}</code></td>
      <td class="col-summary u-wrap">${escapeHtml(item.subject || "")}</td>
      <td class="col-model muted"><span class="u-ellipsis" title="${escapeAttr(item.author || "")}">${escapeHtml(item.author || "")}</span></td>
      <td class="col-time muted u-atomic u-tabular">${escapeHtml(time)}</td>
    </tr>`;
  }).join("");
  return `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="插件版本提交记录"><table class="data-table wide"><thead><tr><th scope="col" class="col-id">版本</th><th scope="col" class="col-summary">内容</th><th scope="col" class="col-model">作者</th><th scope="col" class="col-time">时间</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="4" class="muted">${escapeHtml(emptyText || "暂无记录")}</td></tr>`}</tbody></table></div>`;
}

const _PLUGIN_UPDATE_RESULT_STORAGE_KEY = "personification_plugin_update_result_v1";

function persistPluginUpdateResult(input) {
  const result = input && input.diagnostic && typeof input.diagnostic === "object" ? input.diagnostic : input;
  state.pluginUpdateResult = result && typeof result === "object" ? result : null;
  try {
    if (state.pluginUpdateResult) sessionStorage.setItem(_PLUGIN_UPDATE_RESULT_STORAGE_KEY, JSON.stringify(state.pluginUpdateResult));
    else sessionStorage.removeItem(_PLUGIN_UPDATE_RESULT_STORAGE_KEY);
  } catch {}
}

try {
  const savedPluginUpdateResult = JSON.parse(sessionStorage.getItem(_PLUGIN_UPDATE_RESULT_STORAGE_KEY) || "null");
  if (savedPluginUpdateResult && typeof savedPluginUpdateResult === "object") state.pluginUpdateResult = savedPluginUpdateResult;
} catch {
  try { sessionStorage.removeItem(_PLUGIN_UPDATE_RESULT_STORAGE_KEY); } catch {}
}

function renderPluginManager() {
  const st = state.pluginUpdateStatus;
  if (!st) return `<div class="card muted">加载中…</div>`;
  const source = st.source || {};
  const local = st.local || {};
  const remote = st.remote || {};
  const history = state.pluginUpdateHistory || {};
  const pending = st.pending_history || history.pending_history || [];
  const localHistory = history.history || st.history || [];
  const fetch = st.fetch || {};
  const sourceType = st.source_type === "git" ? "Git" : (st.source_type || "unknown");
  const updateAvailable = !!st.update_available;
  const canUpdate = !!st.update_supported && updateAvailable && !st.dirty && !state.pluginUpdateBusy;
  const statusTag = st.update_supported === false
    ? '<span class="tag required">不支持自动更新</span>'
    : updateAvailable
      ? '<span class="tag tag--status" style="background:rgba(245,158,11,0.18);color:var(--warn)">有更新</span>'
      : '<span class="tag tag--status" style="background:rgba(52,211,153,0.18);color:var(--ok)">已是最新</span>';
  const fetchAlert = fetch && fetch.ok === false
    ? `<div class="alert err" style="margin-top:10px">远端检查失败：${escapeHtml(fetch.error || "未知错误")}</div>`
    : "";
  const dirtyAlert = st.dirty
    ? `<div class="alert err" style="margin-top:10px">本地有 ${Number(st.dirty_count || 0)} 项未提交改动，自动更新已禁用。<pre class="u-pre-wrap code-scroll" style="margin:8px 0 0">${escapeHtml((st.dirty_preview || []).join("\n"))}</pre></div>`
    : "";
  const result = state.pluginUpdateResult;
  const resultDiagnostic = result ? `<div style="margin-top:12px">${renderOperationDiagnostic(result)}</div>` : "";
  return `<div class="card">
    <div class="between" style="gap:12px;align-items:flex-start">
      <div>
        <h2 style="margin:0">拟人插件更新</h2>
        <p class="muted" style="font-size:12px;margin:6px 0 0">按当前安装源检查更新；当前实现识别 Git 安装，后续商店版可接入新的 source provider。</p>
      </div>
      <div class="row">
        ${statusTag}
        <button class="btn small" onclick="reloadPluginManager()" ${state.loading?'disabled':''}>刷新</button>
      </div>
    </div>
    <div class="health-summary" style="margin-top:14px">
      <div class="health-pill"><div><div class="muted" style="font-size:12px">安装源</div><div>${escapeHtml(sourceType)}</div></div></div>
      <div class="health-pill"><div><div class="muted" style="font-size:12px">本地版本</div><div><code class="u-atomic" title="${escapeAttr(local.short_hash || "-")}">${escapeHtml(local.short_hash || "-")}</code></div></div></div>
      <div class="health-pill"><div><div class="muted" style="font-size:12px">远端版本</div><div><code class="u-atomic" title="${escapeAttr(remote.short_hash || "-")}">${escapeHtml(remote.short_hash || "-")}</code></div></div></div>
      <div class="health-pill"><div><div class="muted" style="font-size:12px">差异</div><div>领先 ${Number(st.ahead || 0)} / 落后 ${Number(st.behind || 0)}</div></div></div>
    </div>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="插件安装源详情"><table class="data-table compact" style="margin-top:10px"><tbody>
      <tr><td class="muted u-atomic" style="width:120px">仓库根目录</td><td class="col-description"><code class="u-wrap">${escapeHtml(st.repo_root || st.plugin_root || "-")}</code></td></tr>
      <tr><td class="muted u-atomic">插件目录</td><td class="col-description"><code class="u-wrap">${escapeHtml(st.plugin_subdir || ".")}</code></td></tr>
      <tr><td class="muted u-atomic">分支</td><td class="col-description u-wrap">${escapeHtml(source.branch || local.branch || "-")} ${source.upstream ? `<span class="muted">→ ${escapeHtml(source.upstream)}</span>` : ""}</td></tr>
      <tr><td class="muted u-atomic">远端</td><td class="col-description">${source.remote_url ? `<code class="u-wrap">${escapeHtml(source.remote_url)}</code>` : '<span class="muted">未配置</span>'}</td></tr>
      <tr><td class="muted u-atomic">状态</td><td class="col-description u-wrap">${escapeHtml(st.message || "-")}</td></tr>
    </tbody></table></div>
    ${fetchAlert}${dirtyAlert}${resultDiagnostic}
    <div class="row" style="margin-top:14px">
      <button class="btn primary" onclick="checkPluginUpdates()" ${state.pluginUpdateChecking?'disabled':''}>${state.pluginUpdateChecking?'<span class="spinner"></span> 检查中…':'检查更新'}</button>
      <button class="btn danger" onclick="applyPluginUpdate()" ${canUpdate?'':'disabled'}>${state.pluginUpdateBusy?'<span class="spinner"></span> 更新中…':'应用更新'}</button>
      <span class="muted" style="font-size:12px">应用更新只执行 fast-forward；成功后需要重启 bot 才会加载新代码。</span>
    </div>
  </div>
  <div class="card">
    <h2>待更新内容</h2>
    ${pluginCommitRows(pending, updateAvailable ? "暂无可展示的待更新提交" : "当前没有待更新提交")}
  </div>
  <div class="card">
    <h2>历史更新内容</h2>
    ${pluginCommitRows(localHistory, "暂无历史提交记录")}
  </div>`;
}

async function reloadPluginManager() {
  try { await loadView(); render(); } catch (e) { alertFlash("err", "刷新失败：" + e.message); }
}

async function checkPluginUpdates() {
  if (state.pluginUpdateChecking) return;
  state.pluginUpdateChecking = true;
  render();
  try {
    const status = await api("/plugin-manager/check", { method:"POST", headers:{"content-type":"application/json"}, body:"{}" });
    state.pluginUpdateStatus = status;
    persistPluginUpdateResult(status);
    state.pluginUpdateHistory = await api("/plugin-manager/history?limit=30").catch(() => state.pluginUpdateHistory);
  } catch (e) {
    const diagnostic=operationDiagnosticFromError(e,"插件更新检查未完成");persistPluginUpdateResult(diagnostic);
    alertFlash("err",diagnostic.title||"插件更新检查未完成");
  }
  state.pluginUpdateChecking = false;
  render();
}

async function applyPluginUpdate() {
  const st = state.pluginUpdateStatus || {};
  if (!st.update_available) { alertFlash("err", "当前没有可应用的更新"); return; }
  if (st.dirty) { alertFlash("err", "本地有未提交改动，不能自动更新"); return; }
  if (!confirm("确认更新拟人插件？将执行当前安装源的 fast-forward 更新，成功后需要重启 bot。")) return;
  state.pluginUpdateBusy = true;
  render();
  try {
    const result = await api("/plugin-manager/update", { method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({confirm:"update"}) });
    persistPluginUpdateResult(result);
    if (result && result.status) state.pluginUpdateStatus = result.status;
    state.pluginUpdateHistory = await api("/plugin-manager/history?limit=30").catch(() => state.pluginUpdateHistory);
    alertFlash(result.ok ? "ok" : "err", result.message || result.error || (result.ok ? "更新完成" : "更新失败"));
  } catch (e) {
    const diagnostic=operationDiagnosticFromError(e,"插件更新未完成");persistPluginUpdateResult(diagnostic);
    alertFlash("err",diagnostic.title||"插件更新未完成");
  }
  state.pluginUpdateBusy = false;
  render();
}

const _TEST_OPERATION_RESULT_STORAGE_KEY = "personification_test_operation_result_v1";

function testDiagnosticSnapshot(input) {
  const d = input && input.diagnostic && typeof input.diagnostic === "object" ? input.diagnostic : input;
  if (!d || typeof d !== "object" || !d.code) return null;
  return {
    ok: d.ok === true,
    code: String(d.code || "operation_failed"),
    phase: String(d.phase || ""),
    title: String(d.title || ""),
    message: String(d.message || d.error || ""),
    details: Array.isArray(d.details) ? d.details : [],
    steps: Array.isArray(d.steps) ? d.steps : [],
    warnings: Array.isArray(d.warnings) ? d.warnings : [],
    suggestion: String(d.suggestion || ""),
    retryable: Boolean(d.retryable),
    partial: Boolean(d.partial),
    outcome_unknown: Boolean(d.outcome_unknown),
    operation_id: String(d.operation_id || ""),
    trace_id: String(d.trace_id || ""),
  };
}

function persistTestOperationResult(input) {
  const primary = testDiagnosticSnapshot(input);
  const providers = Array.isArray(input && input.results) ? input.results.map(item => ({
    name: String(item && item.name || "未命名 Provider"),
    diagnostic: testDiagnosticSnapshot(item),
  })).filter(item => item.diagnostic) : [];
  state.testOperationResult = primary ? { diagnostic: primary, providers } : null;
  try {
    if (state.testOperationResult) sessionStorage.setItem(_TEST_OPERATION_RESULT_STORAGE_KEY, JSON.stringify(state.testOperationResult));
    else sessionStorage.removeItem(_TEST_OPERATION_RESULT_STORAGE_KEY);
  } catch {}
}

function clearTestOperationResult() {
  persistTestOperationResult(null);
  render();
}

try {
  const savedTestOperationResult = JSON.parse(sessionStorage.getItem(_TEST_OPERATION_RESULT_STORAGE_KEY) || "null");
  if (savedTestOperationResult && savedTestOperationResult.diagnostic) state.testOperationResult = savedTestOperationResult;
} catch {
  try { sessionStorage.removeItem(_TEST_OPERATION_RESULT_STORAGE_KEY); } catch {}
}

function renderTestOperationResult() {
  const result = state.testOperationResult;
  if (!result || !result.diagnostic) return "";
  const providers = (result.providers || []).map(item => `<details name="provider-diagnostic" style="margin-top:10px">
    <summary>${escapeHtml(item.name)} · ${escapeHtml(item.diagnostic.code || "unknown")}</summary>
    <div style="margin-top:10px">${renderOperationDiagnostic(item.diagnostic,{group:"provider-diagnostic",expanded:false})}</div>
  </details>`).join("");
  return `<div class="card">
    <div class="between" style="gap:12px;align-items:flex-start">
      <div><h2 style="margin:0">最近一次模型 / Prompt 测试诊断</h2><p class="muted" style="font-size:12px;margin:6px 0 0">刷新页面后仍保留；仅保存服务端返回的脱敏 diagnostic，不保存模型正文。</p></div>
      <button class="btn small" onclick="clearTestOperationResult()">清除</button>
    </div>
    <div style="margin-top:12px">${renderOperationDiagnostic(result.diagnostic)}</div>
    ${providers}
  </div>`;
}

function renderTest() {
  const r = state.testResult;
  return `${renderTestOperationResult()}<div class="card">
    <h2>模型调用测试</h2>
    <label class="muted">system prompt</label>
    <textarea oninput="state.testSystem=this.value" style="width:100%;min-height:60px;margin:6px 0">${escapeHtml(state.testSystem)}</textarea>
    <label class="muted">用户消息</label>
    <textarea oninput="state.testPrompt=this.value" style="width:100%;min-height:80px;margin:6px 0">${escapeHtml(state.testPrompt)}</textarea>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" onclick="runTest()">发送（路由模型）</button>
      <button class="btn" onclick="runTest('qzone')">QZone 兼容测试</button>
      <button class="btn" onclick="runTestAll()">测试全部 provider</button>
      ${state.testLoading?'<span class="muted">调用中…</span>':''}
    </div>
    <p class="muted" style="margin-top:8px;font-size:12px">“QZone 兼容测试”使用 production single_attempt、当前 runtime 的 QZone read-only tool profile 和 JSON instruction，但不会执行工具或发布内容；profile 没有可用 schema 时会按真实 tool-free 形态探测并明确标记。“测试全部 provider”会向 api_pools 里每个 provider 各发一次。</p>
  </div>
  ${r ? `<div class="card"><h2>响应（路由模型）</h2>
    <div class="row muted" style="font-size:12px;margin-bottom:8px">
      <span class="u-ellipsis" title="${escapeAttr(r.model_used || '未知')}">模型 <code>${escapeHtml(r.model_used||'未知')}</code></span>
      ${r.profile==='qzone'?'<span>QZone-compatible</span>':''}
      <span>finish=${escapeHtml(r.finish_reason||'')}</span>
      <span>${r.duration_ms}ms</span>
      <span>tokens prompt=${r.usage?.prompt_tokens||0} completion=${r.usage?.completion_tokens||0}</span>
    </div>
    <pre class="u-pre-wrap code-scroll" style="margin:0;font-family:inherit">${escapeHtml(r.content||'(无内容)')}</pre>
  </div>` : ''}
  ${renderTestAll()}`;
}

function renderTestAll() {
  const ra = state.testAllResult;
  if (!ra) return '';
  const rows = (ra.results || []).slice().sort((a,b)=>(a.priority-b.priority)||(a.index-b.index)).map(x => {
    const ok = x.ok && !x.error;
    const status = ok
      ? '<span class="device-status approved">通过</span>'
      : (x.blocked_reason ? '<span class="device-status pending">被拦截</span>'
                          : '<span class="device-status pending" style="background:rgba(248,113,113,0.18);color:var(--danger)">失败</span>');
    const diagnostic = x.diagnostic || x;
    const failureText = [diagnostic.title, diagnostic.message].filter(Boolean).join("：");
    const detail = ok ? (escapeHtml((x.content||'').slice(0,200)) || '(空)') : escapeHtml(failureText || x.error || x.blocked_reason || '未知错误');
    return `<tr>
      <td class="col-model"><span class="u-ellipsis" title="${escapeAttr(x.name || '')}">${escapeHtml(x.name||'')}</span></td>
      <td class="col-model muted"><span class="u-ellipsis" title="${escapeAttr((x.api_type || '') + ' / ' + (x.model || ''))}">${escapeHtml(x.api_type||'')} / ${escapeHtml(x.model||'')}</span></td>
      <td class="col-status">${status}</td>
      <td class="col-number u-atomic u-tabular">${x.duration_ms!=null?x.duration_ms+'ms':'-'}</td>
      <td class="col-description u-pre-wrap">${detail}</td>
    </tr>`;
  }).join("");
  return `<div class="card"><h2>全部 provider 测试（${ra.count||0}）</h2>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="Provider 测试结果"><table class="data-table wide"><thead><tr><th scope="col" class="col-model">名称</th><th scope="col" class="col-model">类型 / 模型</th><th scope="col" class="col-status">状态</th><th scope="col" class="col-number">延迟</th><th scope="col" class="col-description">内容 / 错误</th></tr></thead>
    <tbody>${rows||'<tr><td colspan="5" class="muted">无</td></tr>'}</tbody></table></div>
  </div>`;
}

async function runTest(profile="chat") {
  state.testLoading = true; render();
  try {
    state.testResult = await api("/test/chat", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({prompt: state.testPrompt, system: state.testSystem, profile}) });
    persistTestOperationResult(state.testResult);
  } catch (e) {
    const report = operationDiagnosticFromError(e, "路由模型测试未完成");
    state.testResult = null;
    persistTestOperationResult(report);
    alertFlash("err", report.title || report.message || "路由模型测试未完成");
  }
  state.testLoading = false; render();
}

async function runTestAll() {
  state.testLoading = true; render();
  try {
    state.testAllResult = await api("/test/chat-all", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({prompt: state.testPrompt, system: state.testSystem}) });
    persistTestOperationResult(state.testAllResult);
  } catch (e) {
    const report = operationDiagnosticFromError(e, "全部 Provider 测试未完成");
    state.testAllResult = null;
    persistTestOperationResult(report);
    alertFlash("err", report.title || report.message || "全部 Provider 测试未完成");
  }
  state.testLoading = false; render();
}

function renderPersonaPrompt() {
  const p = state.personaPrompt;
  if (p && p.diagnostic) persistTestOperationResult(p);
  const meta = p ? `<div class="row muted" style="font-size:12px;margin-bottom:8px;gap:14px">
      <span>来源：${escapeHtml(p.source||'-')}</span>
      ${p.resolved_path ? `<span class="u-wrap">路径：<code class="u-wrap">${escapeHtml(p.resolved_path)}</code></span>` : ''}
      <span>${p.exists ? (p.is_file ? (p.size+' 字节') : '内联文本') : '<span style="color:var(--danger)">文件不存在</span>'}</span>
    </div>` : '';
  const body = p && (p.content || p.content === '')
    ? `<pre class="u-pre-wrap code-scroll" style="margin:0;font-family:ui-monospace,Consolas,monospace;max-height:60vh">${escapeHtml(p.content || '(空)')}</pre>`
    : (p && p.diagnostic ? '' : '<p class="muted">加载中…</p>');
  return `<div class="card">
    <h2>人设预览</h2>
    <p class="muted" style="font-size:12.5px">默认显示当前生效的人设文件（prompt_path / system_path / system_prompt）。也可输入任意路径查看其内容。</p>
    <div class="row" style="margin:10px 0">
      <input id="persona-path" type="text" placeholder="留空=当前配置；或输入文件路径" value="${escapeAttr(state.personaPromptPath||'')}" style="flex:1;min-width:240px" onkeydown="if(event.key==='Enter')loadPersonaPrompt()">
      <button class="btn primary" onclick="loadPersonaPrompt()">查看</button>
      ${state.personaPromptPath ? '<button class="btn" onclick="resetPersonaPrompt()">重置为当前配置</button>' : ''}
    </div>
  </div>
  <div class="card">${p && p.diagnostic ? renderOperationDiagnostic(p.diagnostic) : ''}${meta}${body}</div>`;
}

async function loadPersonaPrompt() {
  const el = document.getElementById("persona-path");
  if (el) state.personaPromptPath = el.value.trim();
  try {
    await loadView();
    persistTestOperationResult(state.personaPrompt);
    render();
  } catch (e) {
    const report = operationDiagnosticFromError(e, "人设 prompt 读取未完成");
    state.personaPrompt = { diagnostic: report };
    persistTestOperationResult(report);
    alertFlash("err", report.title || report.message || "人设 prompt 读取未完成");
    render();
  }
}

async function resetPersonaPrompt() {
  state.personaPromptPath = "";
  try {
    await loadView();
    persistTestOperationResult(state.personaPrompt);
    render();
  } catch (e) {
    const report = operationDiagnosticFromError(e, "当前人设 prompt 读取未完成");
    state.personaPrompt = { diagnostic: report };
    persistTestOperationResult(report);
    alertFlash("err", report.title || report.message || "当前人设 prompt 读取未完成");
    render();
  }
}
