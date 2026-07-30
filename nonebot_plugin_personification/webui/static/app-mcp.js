const _MCP_OPERATION_RESULT_STORAGE_KEY = "personification_mcp_operation_result_v1";
let _mcpPendingInstall = null;

function clearMcpSensitiveState() {
  _mcpPendingInstall = null;
}

function persistMcpOperationResult(input) {
  const result = input && input.diagnostic && typeof input.diagnostic === "object" ? input.diagnostic : input;
  state.mcpOperationResult = result && typeof result === "object" ? result : null;
  try {
    if (state.mcpOperationResult) sessionStorage.setItem(_MCP_OPERATION_RESULT_STORAGE_KEY, JSON.stringify(state.mcpOperationResult));
    else sessionStorage.removeItem(_MCP_OPERATION_RESULT_STORAGE_KEY);
  } catch {}
}

try {
  const savedMcpOperationResult = JSON.parse(sessionStorage.getItem(_MCP_OPERATION_RESULT_STORAGE_KEY) || "null");
  if (savedMcpOperationResult && typeof savedMcpOperationResult === "object") state.mcpOperationResult = savedMcpOperationResult;
} catch {
  try { sessionStorage.removeItem(_MCP_OPERATION_RESULT_STORAGE_KEY); } catch {}
}

function stopMcpViewLifecycle() {
  // Pending confirmation may contain Secret input values and must stay page-local.
  _mcpPendingInstall = null;
  if (_mcpAuthTimer) { clearInterval(_mcpAuthTimer); _mcpAuthTimer = null; }
}

function mcpSourceById(sourceId) {
  return (state.mcpSources || []).find(source => source.id === sourceId) || {
    id: sourceId || "unknown",
    name: sourceId || "Unknown Registry",
    preview: false,
  };
}

function renderMcpSourceBadges(sourceId) {
  const source = mcpSourceById(sourceId);
  const authority = source.id === "official"
    ? '<span class="mcp-source-badge official">Official</span>'
    : '<span class="mcp-source-badge compatible">compatible</span>';
  const preview = source.preview ? '<span class="mcp-source-badge preview">Preview</span>' : "";
  return `${authority}${preview}`;
}

function renderMcpExternalLink(url, label) {
  const raw = String(url || "").trim();
  if (!raw) return "";
  const safe = safeHttpUrl(raw);
  return safe
    ? `<a href="${escapeAttr(safe)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
    : "";
}

function renderMcpOperationResult() {
  const result = state.mcpOperationResult;
  if (!result) return "";
  return `<section class="card mcp-diagnostic-card">
    <div class="mcp-section-heading">
      <div><span class="eyebrow">MCP OPERATION</span><h2>最近一次 MCP 操作</h2><p>刷新后仍保留；只存储服务端返回或基于明确成功响应生成的脱敏 diagnostic。</p></div>
      <button class="btn small" data-mcp-operation-clear>清除</button>
    </div>
    ${renderOperationDiagnostic(result, {group:"mcp-operation"})}
  </section>`;
}

function mcpStatusTone(status) {
  const value = String(status || "unknown").toLowerCase();
  if (["active", "running", "ready"].includes(value)) return "ok";
  if (["deprecated", "stopped", "unknown"].includes(value)) return "warn";
  return "error";
}

function renderMcpRegistryServer(server) {
  const repository = server.repository && typeof server.repository === "object" ? server.repository : {};
  const sourceId = server.source_id || state.mcpSourceId;
  const links = [
    renderMcpExternalLink(repository.url, "Repository"),
    renderMcpExternalLink(server.website || server.website_url, "Website"),
    renderMcpExternalLink(server.schema, "Schema"),
  ].filter(Boolean).join("");
  const resources = [
    ["repository", repository.url || repository.source || "未声明"],
    ["website", server.website || server.website_url || "未声明"],
    ["schema", server.schema || "未声明"],
  ].map(([label, value]) => `<span><b>${label}</b><code title="${escapeAttr(value)}">${escapeHtml(value)}</code></span>`).join("");
  const status = String(server.status || "unknown");
  return `<article class="mcp-registry-record">
    <header>
      <div class="mcp-record-source">${renderMcpSourceBadges(sourceId)}</div>
      <span class="mcp-state ${mcpStatusTone(status)}"><i></i>${escapeHtml(status)}</span>
    </header>
    <div class="mcp-record-body">
      <div class="mcp-record-title"><h3 title="${escapeAttr(server.title || server.name || "Untitled Server")}">${escapeHtml(server.title || server.name || "Untitled Server")}</h3><code title="${escapeAttr(server.name || "")}">${escapeHtml(server.name || "")}</code></div>
      <p>${escapeHtml(server.description || "未提供 description")}</p>
      ${server.status_message ? `<div class="mcp-status-message">${escapeHtml(server.status_message)}</div>` : ""}
      <div class="mcp-resource-facts">${resources}</div>
    </div>
    <footer>
      <div class="mcp-transport-counts"><span><strong>${Number(server.stdio_packages || 0)}</strong> stdio</span><span><strong>${Number(server.remote_count || 0)}</strong> remote</span><span><strong>${escapeHtml(server.version || "-")}</strong> version</span></div>
      <div class="mcp-record-links">${links}</div>
      <button class="btn small" aria-label="查看 MCP Server ${escapeAttr(server.name || "")}" data-mcp-detail="${escapeAttr(server.name || "")}" data-mcp-source="${escapeAttr(sourceId)}">查看详情</button>
    </footer>
  </article>`;
}

function renderMcpRegistryList() {
  const records = (state.mcpResults || []).map(renderMcpRegistryServer).join("");
  let empty = "";
  if (!records) {
    empty = state.mcpSearchLoaded
      ? '<div class="mcp-empty"><strong>没有匹配的 Server 名称</strong><span>Registry search 仅按 Server 名称查询，不执行能力全文搜索。</span></div>'
      : '<div class="mcp-empty"><strong>按 Server 名称开始 discovery</strong><span>输入 canonical name 或名称片段；这里不声称支持能力全文搜索。</span></div>';
  }
  const more = state.mcpNextCursor
    ? `<div class="mcp-load-more"><button class="btn" data-mcp-load-more ${state.mcpBusy ? "disabled" : ""}>${state.mcpLoadingMore ? '<span class="spinner"></span> 加载中' : '加载更多'}</button><span>opaque next cursor 将原样发送，结果追加并按 canonical name 去重。</span></div>`
    : (records && state.mcpSearchLoaded ? '<div class="mcp-stream-end">Registry result set complete</div>' : "");
  return `<div class="mcp-registry-stream">${records || empty}</div>${more}`;
}

function mcpPackageIdentity(item) {
  const type = String(item.registry_type || "").toLowerCase();
  const separator = type === "pypi" ? "==" : "@";
  return `${item.identifier || "unknown"}${separator}${item.version || "unknown"}`;
}

function renderMcpPackageMatrix(packages) {
  return `<div class="mcp-package-matrix">${packages.map(item => {
    const supported = item.supported === true;
    return `<article class="mcp-package-row ${supported ? "supported" : "unsupported"}">
      <div><span>${escapeHtml(item.registry_type || "unknown")} / ${escapeHtml(item.transport || "unknown")}</span><code title="${escapeAttr(mcpPackageIdentity(item))}">${escapeHtml(mcpPackageIdentity(item))}</code></div>
      <span class="mcp-state ${supported ? "ok" : "error"}"><i></i>${supported ? "supported" : "unsupported"}</span>
      ${item.fileSha256 ? `<small class="u-ellipsis" title="fileSha256 ${escapeAttr(item.fileSha256)}">fileSha256 ${escapeHtml(item.fileSha256)}</small>` : ""}
      ${!supported ? `<p>${escapeHtml(item.unsupported_reason || "该 package 不支持快捷安装。")}</p>` : ""}
    </article>`;
  }).join("") || '<div class="mcp-empty"><strong>没有 package metadata</strong><span>该 Server 当前没有可展示的安装包。</span></div>'}</div>`;
}

function selectedMcpPackage(packages) {
  const supported = packages.filter(item => item.supported === true);
  let selected = supported.find(item => Number(item.index) === Number(state.mcpPackageIndex));
  if (!selected && supported.length) {
    selected = supported[0];
    state.mcpPackageIndex = selected.index;
  }
  return selected || null;
}

function renderMcpDetail() {
  const detail = state.mcpDetail || {};
  const server = detail.server || {};
  const packages = Array.isArray(detail.packages) ? detail.packages : [];
  const selected = selectedMcpPackage(packages);
  const source = detail.source || mcpSourceById(state.mcpSourceId);
  const repository = server.repository && typeof server.repository === "object" ? server.repository : {};
  const externalLinks = [
    renderMcpExternalLink(repository.url, "Repository"),
    renderMcpExternalLink(server.website || server.website_url, "Website"),
    renderMcpExternalLink(server.schema, "Schema"),
  ].filter(Boolean).join("");
  const packageOptions = packages.filter(item => item.supported === true).map(item => `<option value="${Number(item.index)}" ${Number(item.index) === Number(state.mcpPackageIndex) ? "selected" : ""}>${escapeHtml(item.registry_type)} · ${escapeHtml(mcpPackageIdentity(item))}</option>`).join("");
  const inputs = selected ? (selected.inputs || []).map(input => {
    const choices = Array.isArray(input.choices) ? input.choices : [];
    const registryDefault = String(input.default == null ? "" : input.default);
    const defaultIsChoice = choices.some(choice => String(choice) === registryDefault);
    const control = choices.length
      ? `<select data-mcp-install-input="${escapeAttr(input.key)}" data-mcp-secret="${input.secret ? "true" : "false"}">${registryDefault && !defaultIsChoice ? `<option value="" selected>使用 Registry default · ${escapeHtml(registryDefault)}</option>` : ""}${choices.map(choice => `<option value="${escapeAttr(choice)}" ${String(choice) === registryDefault ? "selected" : ""}>${escapeHtml(choice)}</option>`).join("")}</select>`
      : `<input data-mcp-install-input="${escapeAttr(input.key)}" data-mcp-secret="${input.secret ? "true" : "false"}" type="${input.secret ? "password" : "text"}" placeholder="${escapeAttr(input.default || "")}">`;
    return `<label class="mcp-input"><span>${escapeHtml(input.key)}${input.required ? " *" : ""}<em>${escapeHtml(input.location || "input")}${input.secret ? " / Secret" : ""}</em></span><small>${escapeHtml(input.description || "Registry 未提供说明")}</small>${control}</label>`;
  }).join("") : "";
  const status = String(server.status || "unknown");
  return `<div class="mcp-detail">
    <div class="mcp-detail-nav"><button class="btn small" data-mcp-detail-close>返回 Registry 结果</button><div>${renderMcpSourceBadges(source.id || state.mcpSourceId)}</div></div>
    <header class="mcp-detail-head"><div><span class="eyebrow">CANONICAL SERVER</span><h3 title="${escapeAttr(server.title || server.name || "")}">${escapeHtml(server.title || server.name || "")}</h3><code title="${escapeAttr(server.name || "")}">${escapeHtml(server.name || "")}</code></div><span class="mcp-state ${mcpStatusTone(status)}"><i></i>${escapeHtml(status)}</span></header>
    <p class="mcp-detail-description">${escapeHtml(server.description || "未提供 description")}</p>
    ${server.status_message ? `<div class="mcp-status-message">${escapeHtml(server.status_message)}</div>` : ""}
    <div class="mcp-detail-meta"><span>version<strong title="${escapeAttr(server.version || "-")}">${escapeHtml(server.version || "-")}</strong></span><span>schema<strong title="${escapeAttr(server.schema || "未声明")}">${escapeHtml(server.schema || "未声明")}</strong></span></div>
    ${externalLinks ? `<div class="mcp-record-links">${externalLinks}</div>` : ""}
    <div><span class="eyebrow">PACKAGE SUPPORT</span>${renderMcpPackageMatrix(packages)}</div>
    ${selected ? `<div class="mcp-install-preflight">
      <div class="mcp-section-heading"><div><span class="eyebrow">EXECUTION PREFLIGHT</span><h3>安装预检</h3><p>执行第三方 package 前先核对精确 identity。安装后所有 tool 默认未授权。</p></div><span class="tag required">publisher metadata untrusted</span></div>
      <label class="mcp-install-target">安装目标<select id="mcp-package-select">${packageOptions}</select></label>
      <div class="mcp-input-grid">${inputs || '<p class="muted">该 package 不需要额外输入。</p>'}</div>
      <label class="mcp-install-target">工具名前缀（可选）<input id="mcp-prefix-input" placeholder="留空由服务端安全生成" value="${escapeAttr(state.mcpPrefix || "")}"></label>
      <div class="alert info">Registry 只提供 publisher metadata，不代表安全审计。Secret 只提交至服务器受限文件；确认页和 diagnostic 均不显示值。</div>
      <button class="btn primary" data-mcp-install-plan ${state.mcpBusy ? "disabled" : ""}>审阅 command plan</button>
    </div>` : '<div class="alert info">没有 supported 的 npm/PyPI stdio package；unsupported_reason 已逐项列出。</div>'}
  </div>`;
}

function renderMcpRegistryDiscovery() {
  const sources = state.mcpSources || [];
  const options = sources.map(source => `<option value="${escapeAttr(source.id)}" ${state.mcpSourceId === source.id ? "selected" : ""}>${escapeHtml(source.name)}</option>`).join("");
  return `<section class="card mcp-zone mcp-registry-zone">
    <div class="mcp-section-heading">
      <div><span class="eyebrow">01 / REGISTRY DISCOVERY</span><h2>Registry discovery</h2><p>Official Registry 优先，也可选择 configured compatible HTTPS source。搜索语义严格限定为 Server 名称。</p></div>
      <div class="mcp-source-legend"><span>来源</span>${renderMcpSourceBadges(state.mcpSourceId)}</div>
    </div>
    <div class="mcp-search-row"><select id="mcp-source-select" aria-label="Registry source" ${state.mcpBusy ? "disabled" : ""}>${options}</select><input id="mcp-search-input" type="search" placeholder="按 Server 名称搜索" value="${escapeAttr(state.mcpQuery || "")}" aria-label="按 Server 名称搜索" ${state.mcpBusy ? "disabled" : ""}><button class="btn primary" data-mcp-search ${state.mcpBusy ? "disabled" : ""}>${state.mcpBusy && !state.mcpLoadingMore ? '<span class="spinner"></span>' : ''} 搜索</button></div>
    <p class="mcp-search-scope">SEARCH SCOPE / canonical Server name only · 不声称支持能力全文搜索</p>
    ${state.mcpDetail ? renderMcpDetail() : renderMcpRegistryList()}
  </section>`;
}

function mcpSchemaType(spec) {
  if (!spec || typeof spec !== "object") return "unknown";
  const raw = Array.isArray(spec.type) ? spec.type.join(" | ") : String(spec.type || "unknown");
  const enumValues = Array.isArray(spec.enum) ? ` · enum(${spec.enum.length})` : "";
  return raw + enumValues;
}

function renderMcpInputSchema(schema) {
  const value = schema && typeof schema === "object" ? schema : {};
  const properties = value.properties && typeof value.properties === "object" ? value.properties : {};
  const required = new Set(Array.isArray(value.required) ? value.required.map(String) : []);
  const rows = Object.entries(properties).map(([name, spec]) => `<li><code title="${escapeAttr(name)}">${escapeHtml(name)}</code><span>${escapeHtml(mcpSchemaType(spec))}${required.has(name) ? ' <b>required</b>' : ""}</span><small>${escapeHtml(spec && spec.description || "未提供参数说明")}</small></li>`).join("");
  return `<div class="mcp-schema-summary"><div class="mcp-schema-head"><span>Input schema</span><strong>${Object.keys(properties).length} parameters / ${required.size} required</strong></div><ul>${rows || '<li class="empty">未声明 input parameters</li>'}</ul></div>`;
}

function renderMcpSchemaDetails(label, schema, identity) {
  const value = schema && typeof schema === "object" ? schema : {};
  const properties = value.properties && typeof value.properties === "object" ? Object.keys(value.properties) : [];
  const summary = Object.keys(value).length
    ? `${String(value.type || "schema")} · ${properties.length} properties`
    : "未声明";
  return `<details class="mcp-json-details" data-detail-key="${escapeAttr(stableDetailKey("mcp-schema", identity, label))}"><summary><span>${escapeHtml(label)}</span><strong>${escapeHtml(summary)}</strong></summary><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></details>`;
}

function renderManagedMcpTool(tool, installation) {
  const authorized = tool.authorized === true;
  const registered = tool.registered === true;
  const effective = tool.effective === true;
  const processRunning = installation.process_state === "running" && installation.desired_enabled === true;
  const availability = effective
    ? "当前可调用"
    : authorized && !processRunning
      ? "已授权；Server 未运行，当前不可调用"
      : authorized
        ? "已授权；尚未有效注册，当前不可调用"
        : "未授权，当前不可调用";
  const annotations = tool.annotations && typeof tool.annotations === "object" ? tool.annotations : {};
  return `<article class="mcp-tool-card ${effective ? "effective" : ""}">
    <header><div><span class="eyebrow u-ellipsis" title="${escapeAttr(tool.title || "MCP TOOL")}">${escapeHtml(tool.title || "MCP TOOL")}</span><h4 title="${escapeAttr(tool.remote_name || "")}">${escapeHtml(tool.remote_name || "")}</h4><code title="${escapeAttr(tool.registered_name || "")}">${escapeHtml(tool.registered_name || "")}</code></div><button class="btn small ${authorized ? "danger" : "primary"}" aria-label="${authorized ? "撤销授权" : "授权"} MCP tool ${escapeAttr(tool.remote_name || "")}" data-mcp-tool-toggle="${escapeAttr(tool.remote_name || "")}" data-mcp-installation="${escapeAttr(installation.installation_id || "")}" data-mcp-enabled="${authorized ? "false" : "true"}" data-mcp-risk="${tool.publisher_read_only ? "read" : "unknown"}" ${state.mcpBusy ? "disabled" : ""}>${authorized ? "撤销授权" : "授权"}</button></header>
    <p>${escapeHtml(tool.description || "未提供 description")}</p>
    <div class="mcp-state-strip"><span class="${authorized ? "on" : "off"}">authorized / ${authorized ? "yes" : "no"}</span><span class="${registered ? "on" : "off"}">registered / ${registered ? "yes" : "no"}</span><span class="${effective ? "on" : "off"}">effective / ${effective ? "yes" : "no"}</span></div>
    <div class="mcp-availability ${effective ? "ok" : "muted"}">${escapeHtml(availability)}</div>
    ${renderMcpInputSchema(tool.inputSchema || tool.parameters)}
    <div class="mcp-schema-pair">${renderMcpSchemaDetails("Output schema", tool.outputSchema || tool.output_schema, tool.registered_name)}${renderMcpSchemaDetails("Annotations · publisher 声明，untrusted", annotations, tool.registered_name)}</div>
    <div class="mcp-publisher-warning">${tool.publisher_read_only ? "publisher 声明 readOnlyHint；该声明未受信任，也不会自动授权。" : "publisher 未声明可信只读语义；副作用按 unknown 处理。"}</div>
  </article>`;
}

function renderMcpInstallation(item) {
  const tools = Array.isArray(item.tools) ? item.tools : [];
  const metadata = item.metadata && typeof item.metadata === "object" ? item.metadata : {};
  const processState = String(item.process_state || item.observed_status || "stopped");
  const desired = item.desired_enabled === true;
  const sourceBadges = renderMcpSourceBadges(item.source_id || "unknown");
  const repository = metadata.repository && typeof metadata.repository === "object" ? metadata.repository : {};
  const links = [renderMcpExternalLink(repository.url, "Repository"), renderMcpExternalLink(metadata.website, "Website")].filter(Boolean).join("");
  return `<article class="mcp-runtime-card">
    <header class="mcp-runtime-head">
      <div><div class="mcp-record-source">${sourceBadges}</div><span class="eyebrow">RUNTIME INSTALLATION</span><h3 title="${escapeAttr(item.server_title || item.server_name || "")}">${escapeHtml(item.server_title || item.server_name || "")}</h3><code title="${escapeAttr((item.server_name || "") + "@" + (item.server_version || ""))}">${escapeHtml(item.server_name || "")}@${escapeHtml(item.server_version || "")}</code><small class="mcp-installation-id u-ellipsis" title="installation ${escapeAttr(item.installation_id || "")}">installation ${escapeHtml(item.installation_id || "")}</small></div>
      <div class="mcp-runtime-actions"><span class="mcp-state ${mcpStatusTone(processState)}"><i></i>${escapeHtml(processState)}</span><button class="btn small" aria-label="${desired ? "停止" : "启动"} MCP Server ${escapeAttr(item.server_name || "")}" data-mcp-installation-toggle="${escapeAttr(item.installation_id || "")}" data-mcp-enabled="${desired ? "false" : "true"}" ${state.mcpBusy ? "disabled" : ""}>${desired ? "停止运行" : "允许启动"}</button><button class="btn small danger" aria-label="删除 MCP installation ${escapeAttr(item.installation_id || "")}" data-mcp-delete="${escapeAttr(item.installation_id || "")}" ${state.mcpBusy ? "disabled" : ""}>删除</button></div>
    </header>
    <div class="mcp-runtime-identity"><span>package<strong title="${escapeAttr((item.package_type || "") + " / " + (item.package_identifier || ""))}">${escapeHtml(item.package_type || "")} / ${escapeHtml(item.package_identifier || "")}</strong></span><span>prefix<strong title="${escapeAttr(item.name_prefix || "-")}">${escapeHtml(item.name_prefix || "-")}</strong></span><span>protocol<strong title="${escapeAttr(metadata.protocol_version || "未记录")}">${escapeHtml(metadata.protocol_version || "未记录")}</strong></span><span>Secret<strong>${item.secrets_required ? (item.secrets_configured ? "已配置" : "缺失") : "无需"}</strong></span></div>
    <div class="mcp-runtime-state-grid"><div><span>PROCESS ALLOW</span><strong>${desired ? "允许启动" : "停止运行"}</strong><small>desired_enabled=${desired ? "true" : "false"}</small></div><div><span>RUN ALLOWED</span><strong>${item.run_allowed ? "启动条件满足" : "启动条件未满足"}</strong><small>run_allowed=${item.run_allowed ? "true" : "false"}</small></div><div><span>PROCESS STATE</span><strong>${escapeHtml(processState)}</strong><small>授权与 process 生命周期分离</small></div><div><span>TOOL CATALOG</span><strong>${Number(item.tool_count || tools.length)}</strong><small>preflight / reload catalog</small></div></div>
    <div class="mcp-runtime-counts"><span>authorized<strong>${Number(item.authorized_count || 0)}</strong></span><span>registered<strong>${Number(item.registered_count || 0)}</strong></span><span>effective<strong>${Number(item.effective_count || 0)}</strong></span><span>total<strong>${Number(item.tool_count || tools.length)}</strong></span></div>
    <p class="mcp-runtime-note">Server 停止只终止 process 并撤下注册，不会清除 tool 授权；再次允许启动后按 catalog 恢复。</p>
    ${links ? `<div class="mcp-record-links">${links}</div>` : ""}
    ${item.last_error ? `<div class="alert err">runtime diagnostic: ${escapeHtml(item.last_error)}</div>` : ""}
    <div class="mcp-tool-grid">${tools.map(tool => renderManagedMcpTool(tool, item)).join("") || '<div class="mcp-empty"><strong>没有 tool catalog</strong><span>执行 reload 或检查 Server 的 tools capability。</span></div>'}</div>
  </article>`;
}

function renderMcpRuntimeInstallations() {
  const installations = (state.mcpInstallations || []).filter(item => item.installation_id !== MCP_BUILTIN_ID);
  const totals = installations.reduce((acc, item) => {
    acc.authorized += Number(item.authorized_count || 0);
    acc.registered += Number(item.registered_count || 0);
    acc.effective += Number(item.effective_count || 0);
    return acc;
  }, {authorized:0, registered:0, effective:0});
  return `<section class="card mcp-zone mcp-runtime-zone">
    <div class="mcp-section-heading"><div><span class="eyebrow">02 / RUNTIME INSTALLATIONS</span><h2>Runtime installations</h2><p>process allow、persistent authorization、runtime registration 与 effective availability 分层展示。</p></div><button class="btn" data-mcp-reload ${state.mcpBusy ? "disabled" : ""}>重载 MCP runtime</button></div>
    <div class="mcp-runtime-overview"><span>installations<strong>${installations.length}</strong></span><span>authorized<strong>${totals.authorized}</strong></span><span>registered<strong>${totals.registered}</strong></span><span>effective<strong>${totals.effective}</strong></span></div>
    <div class="mcp-runtime-list">${installations.map(renderMcpInstallation).join("") || '<div class="mcp-empty"><strong>暂无 Runtime installation</strong><span>从 Registry detail 完成 execution preflight 后，安装会出现在这里。</span></div>'}</div>
  </section>`;
}

function mcpCommandPlan(item) {
  const type = String(item.registry_type || "").toLowerCase();
  const identity = mcpPackageIdentity(item);
  const hasArguments = (item.inputs || []).some(input => input.location === "argument");
  const tokens = type === "pypi"
    ? ["uvx", "--from", identity, item.identifier || "unknown"]
    : ["npx", "--yes", identity];
  if (hasArguments) tokens.push("[validated Registry package arguments]");
  return {launcher:type === "pypi" ? "uvx" : "npx", identity, tokens};
}

function renderMcpInstallConfirmation() {
  const pending = _mcpPendingInstall;
  if (!pending) return "";
  const plan = pending.plan;
  const inputs = pending.inputSummary.map(item => `<li><span>${escapeHtml(item.location)} / <code class="u-atomic" title="${escapeAttr(item.key)}">${escapeHtml(item.key)}</code></span><strong>${item.secret ? (item.provided ? "已提供，值不显示" : "未提供") : (item.provided ? "已提供" : "使用 Registry default / 空值")}</strong></li>`).join("");
  return `<div class="mcp-confirm-backdrop" role="presentation"><section class="mcp-confirm-panel" role="dialog" aria-modal="true" aria-labelledby="mcp-confirm-title">
    <div class="mcp-section-heading"><div><span class="eyebrow">EXECUTION CONFIRMATION</span><h2 id="mcp-confirm-title">确认第三方 package 执行计划</h2><p>后端 detail 当前不返回解析后的绝对 command path；这里展示精确 package identity 与将由 launcher 执行的安全摘要，不伪造本机路径。</p></div><button class="btn small" data-mcp-install-cancel>取消</button></div>
    <div class="mcp-command-plan"><span>launcher</span><strong class="u-atomic">${escapeHtml(plan.launcher)}</strong><span>exact package identity</span><code title="${escapeAttr(plan.identity)}">${escapeHtml(plan.identity)}</code><span>command token plan</span><div>${plan.tokens.map(token => `<code title="${escapeAttr(token)}">${escapeHtml(token)}</code>`).join('<b aria-hidden="true">→</b>')}</div><span>metadata guard</span><code title="fresh_fetch=true · digest=${escapeAttr(pending.package.digest || "")}">fresh_fetch=true · digest=${escapeHtml(pending.package.digest || "")}</code></div>
    <ul class="mcp-confirm-inputs">${inputs || '<li><span>inputs</span><strong>无额外参数</strong></li>'}</ul>
    <div class="alert err">Registry publisher metadata、description 与 annotations 均视为 untrusted。确认后会以 Bot 系统用户权限执行 package 并调用 initialize / tools/list 预检。</div>
    <div class="mcp-confirm-actions"><button class="btn" data-mcp-install-cancel>返回修改</button><button class="btn primary" data-mcp-install-confirm ${state.mcpBusy ? "disabled" : ""}>${state.mcpBusy ? '<span class="spinner"></span> 正在预检' : '确认执行、预检并安装'}</button></div>
  </section></div>`;
}

const MCP_BUILTIN_ID = "builtin_social_platform_research";
const MCP_PLATFORM_LABELS = {bilibili:"B站", douyin:"抖音", tieba:"贴吧", xiaoheihe:"小黑盒"};
const MCP_PLATFORM_CONFIG_KEYS = new Set([
  "quality_mode", "marketing_threshold", "min_play_count", "min_comment_count",
  "min_reply_count", "max_results", "comment_limit", "danmaku_limit",
  "cache_ttl_seconds", "request_timeout_seconds",
]);
const MCP_STATE_LABELS = {
  disabled:"已关闭", service_disabled:"服务未启动", ready:"可用", login_required:"需要登录",
  manual_verification_required:"需要人工验证", risk_controlled:"平台风控暂停", unavailable:"不可用",
  waiting_scan:"等待扫码", qr_expired:"二维码已过期", success:"登录成功", expired:"会话已过期", cancelled:"已取消", error:"登录失败",
  verified:"自动收录", understand_only:"只理解", observed:"待确认", disputed:"冲突", stale:"已过期",
  rejected:"已拒绝", manual_locked:"人工锁定",
};
let _mcpAuthTimer = null;
let _mcpAuthPollInFlight = false;

function mcpChineseState(value) {
  const raw = String(value || "unknown");
  return MCP_STATE_LABELS[raw] || "状态未知";
}

function builtinMcpInstallation() {
  return (state.mcpInstallations || []).find(item => item.installation_id === MCP_BUILTIN_ID)
    || (state.mcpBuiltin || {}).installation || null;
}

function formatBuiltinAuthRemaining(auth) {
  const seconds = Math.max(0, Number(auth && auth.remaining_seconds || 0));
  if (!Number.isFinite(seconds)) return "等待服务端计时";
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${minutes} 分 ${String(rest).padStart(2, "0")} 秒`;
}

function builtinAuthSessionSummary(auth) {
  if (auth && auth.status === "success") return "登录会话已完成；profile 登录态不会按此倒计时清除";
  return `服务端会话剩余 ${formatBuiltinAuthRemaining(auth)}`;
}

function renderBuiltinAuthHint(auth, platformEnabled=false) {
  if (!auth) return "";
  const kind = String(auth.verification_kind || "");
  if (auth.status === "success" && !platformEnabled) return '<p class="muted">登录态已保存，但平台开关仍关闭。点击“开启平台”后才会参与只读检索；登录和平台开关彼此独立。</p>';
  if (auth.status === "success") return '<p class="muted">登录态已保存且平台已开启。Agent 是否可调用还取决于上方工具授权。</p>';
  if (auth.login_mode === "protocol_qr" && auth.status === "waiting_scan") return '<p class="muted">B站二维码由官方登录接口生成并在本机编码；轮询直接写入隔离的 persistent profile，不依赖可见窗口，页面关闭或刷新不会让二维码立即失效。</p>';
  if (auth.login_mode === "headless_page_qr" && auth.status === "waiting_scan") return '<p class="muted">官方登录页由服务端后台保持；只有通过二维码像素结构校验后才会显示，加载中占位图不会再被当成二维码。无需保持可见窗口。</p>';
  if (kind === "robot_verification") return '<p class="muted">官方页面已要求机器人验证，但本机没有可用的普通系统浏览器兜底。请在当前官方窗口手动完成；MCP 不会绕过验证。</p>';
  if (kind === "official_browser_login") return '<p class="muted">已切换到不受 Playwright 控制的普通系统浏览器。请在该窗口完成扫码、验证码或机器人验证，登录成功后关闭该窗口；MCP 会在窗口关闭后检测登录态。</p>';
  if (kind === "manual_login_incomplete") return '<p class="muted">尚未检测到有效登录态。请确认普通浏览器窗口已经完成登录并完全关闭；需要时可重新打开普通浏览器继续。</p>';
  if (kind === "device_confirmation") return ["protocol_qr","headless_page_qr"].includes(auth.login_mode)
    ? '<p class="muted">二维码已扫描，请在对应平台官方 App 中确认登录；本页会继续轮询，不需要保持任何浏览器窗口。</p>'
    : '<p class="muted">二维码已扫描，请在对应平台官方 App 中确认登录；确认前请不要关闭官方窗口。</p>';
  if (kind === "qr_expired" || auth.status === "qr_expired") return '<p class="muted">官方二维码已经过期，MCP 正在自动刷新官方页面获取新二维码；旧二维码不会继续使用。</p>';
  if (kind === "qr_generation_blocked") return '<p class="muted">官方登录面板已打开，但平台没有生成真实二维码。请在官方窗口完成人工验证，或使用平台提供的验证码登录；不要扫描 Logo 占位图。</p>';
  if (kind === "official_page" && auth.status === "manual_verification_required") return '<p class="muted">官方页面没有提供可转发的二维码，请在自动打开的官方窗口中完成登录。</p>';
  if (auth.error_code === "official_window_closed") return '<p class="muted">官方登录窗口已关闭。请重新获取二维码并保持窗口打开，直到登录状态变为成功。</p>';
  if (auth.error_code === "bilibili_login_state_missing") return '<p class="muted">B站已确认扫码，但没有把有效登录态写入隔离 profile。请重新获取二维码；若仍失败，改用普通浏览器登录并在成功后关闭窗口。</p>';
  if (auth.error_code === "qrcode_encoder_unavailable") return '<p class="muted">服务端缺少本地二维码编码依赖 qrcode，安装依赖并重载原生 MCP 后重试。</p>';
  if (["bilibili_qr_generate_failed","bilibili_qr_poll_failed","bilibili_qr_unknown_state"].includes(auth.error_code)) return '<p class="muted">B站官方二维码事务未完成，未使用未知响应或回退到非官方接口。请稍后重新获取二维码。</p>';
  if (auth.status === "manual_verification_required") return '<p class="muted">请在自动打开的官方页面窗口完成人工验证；系统不会绕过滑块或验证码。</p>';
  if (auth.status === "starting" && !auth.qr_available) return '<p class="muted">正在等待官方页面生成二维码，页面延迟渲染时会自动刷新此状态。</p>';
  return "";
}

function renderBuiltinServiceCard() {
  const item = builtinMcpInstallation();
  if (!item) return '<section class="card"><p class="muted">原生 MCP 清单尚未初始化，请重载插件后重试。</p></section>';
  const desired = item.desired_enabled === true;
  const tools = item.tools || [];
  return `<section class="card mcp-native-service">
    <div class="mcp-section-heading"><div><span class="eyebrow">BUILTIN SERVICE</span><h2>社交平台游戏梗查证</h2><p>登录态只保存在四个平台各自的浏览器 profile；Agent 只能调用三个固定只读工具。</p></div>
      <div class="mcp-runtime-actions"><span class="mcp-state ${mcpStatusTone(item.process_state)}"><i></i>${escapeHtml(item.process_state || "stopped")}</span><button class="btn ${desired ? "danger" : "primary"}" data-mcp-installation-toggle="${MCP_BUILTIN_ID}" data-mcp-enabled="${desired ? "false" : "true"}">${desired ? "停止服务" : "开启服务"}</button><button class="btn" data-mcp-reload>重载并诊断</button></div></div>
    <div class="mcp-native-tool-grid">${tools.map(tool => `<article><div><strong>${escapeHtml(tool.title || tool.remote_name)}</strong><code>${escapeHtml(tool.remote_name || "")}</code><small>source_kind=mcp_builtin · risk_level=low · side_effect=none</small></div><button class="btn small ${tool.authorized ? "danger" : "primary"}" data-mcp-tool-toggle="${escapeAttr(tool.remote_name || "")}" data-mcp-installation="${MCP_BUILTIN_ID}" data-mcp-enabled="${tool.authorized ? "false" : "true"}" data-mcp-risk="builtin">${tool.authorized ? "撤销授权" : "授权给 Agent"}</button></article>`).join("")}</div>
    <p class="muted">实际可用还要求：服务进程存活、工具已授权注册，并且至少一个已开启平台处于已登录且能力健康状态。</p>
  </section>`;
}

function renderBuiltinPlatformCard(platform, item) {
  const label = MCP_PLATFORM_LABELS[platform] || platform;
  const config = item.config || {};
  const auth = (state.mcpAuth || {})[platform] || null;
  const runtimeState = item.runtime_state || item.state || "service_disabled";
  const capabilities = item.capabilities || {};
  const qr = auth && auth.session_id && auth.qr_available
    ? `<img class="mcp-login-qr" alt="${escapeAttr(label)}登录二维码" src="${API}/mcp/builtin/social-research/auth/${encodeURIComponent(auth.session_id)}/qrcode?platform=${encodeURIComponent(platform)}&revision=${encodeURIComponent(String(auth.qr_revision || 0))}">`
    : "";
  const authHint = renderBuiltinAuthHint(auth, item.enabled === true);
  const windowHint = auth && auth.official_window_open
    ? auth.login_mode === "manual_browser"
      ? '<p class="muted">普通系统浏览器窗口正在运行；完成登录后请关闭该窗口，本页会自动检测。</p>'
      : '<p class="muted">官方二维码窗口保持开启；扫码、手机确认完成后，本页会自动更新。</p>'
    : "";
  const authPanel = auth ? `<div class="mcp-auth-panel">${qr}<div><strong>${escapeHtml(mcpChineseState(auth.status))}</strong><code>${escapeHtml(auth.status || "")}</code><small>${escapeHtml(builtinAuthSessionSummary(auth))}</small>${authHint}${windowHint}${auth.error_code === "interactive_window_unavailable" ? '<p class="muted">当前运行环境无法打开可见浏览器；二维码仍可扫码，但手机确认/滑块需要在有桌面的运行账户下重试。</p>' : ""}</div></div>` : "";
  const authAction = platform === "bilibili"
    ? auth && !["success","cancelled"].includes(auth.status) ? "重新获取无窗口二维码" : "获取无窗口二维码"
    : auth && !["success","cancelled"].includes(auth.status) ? "重新获取 WebUI 二维码" : "获取 WebUI 二维码";
  const manualAction = auth && auth.login_mode === "manual_browser" ? "重新打开普通浏览器" : "在普通浏览器中登录";
  const danmaku = capabilities.danmaku === false ? "不支持弹幕" : "页面提供时读取弹幕";
  const platformTitle = platform === "xiaoheihe"
    ? renderMcpExternalLink("https://xiaoheihe.cn/app/bbs/home", label)
    : escapeHtml(label);
  return `<article class="card mcp-platform-card" data-platform="${escapeAttr(platform)}">
    <header><div><span class="eyebrow">${escapeHtml(platform)}</span><h3>${platformTitle}</h3></div><div><strong class="mcp-native-state ${mcpStatusTone(runtimeState)}">${escapeHtml(mcpChineseState(runtimeState))}</strong><code>${escapeHtml(runtimeState)}</code></div></header>
    <div class="mcp-capability-line"><span>搜索</span><span>封面</span><span>正文</span><span>评论/回复</span><span>${escapeHtml(danmaku)}</span></div>
    <div class="mcp-platform-actions"><button class="btn ${item.enabled ? "danger" : "primary"}" data-mcp-platform-toggle="${escapeAttr(platform)}" data-enabled="${item.enabled ? "false" : "true"}">${item.enabled ? "关闭平台" : "开启平台"}</button><button class="btn" data-mcp-auth-start="${escapeAttr(platform)}">${escapeHtml(authAction)}</button><button class="btn" data-mcp-auth-manual="${escapeAttr(platform)}">${escapeHtml(manualAction)}</button><button class="btn danger" data-mcp-auth-logout="${escapeAttr(platform)}">注销并删除 profile</button></div>
    ${authPanel}
    <details><summary>过滤、采样与缓存设置</summary><div class="mcp-platform-config">
      <label>质量模式<select data-mcp-config="quality_mode"><option value="balanced" ${config.quality_mode === "balanced" ? "selected" : ""}>平衡</option><option value="strict" ${config.quality_mode === "strict" ? "selected" : ""}>严格</option><option value="ranking_only" ${config.quality_mode === "ranking_only" ? "selected" : ""}>仅排序不排除</option></select></label>
      <label>营销阈值<input data-mcp-config="marketing_threshold" type="number" min="0" max="1" step="0.05" value="${escapeAttr(config.marketing_threshold ?? 0.75)}"></label>
      <label>最低播放<input data-mcp-config="min_play_count" type="number" min="0" value="${escapeAttr(config.min_play_count ?? 3000)}"></label>
      <label>最低评论<input data-mcp-config="min_comment_count" type="number" min="0" value="${escapeAttr(config.min_comment_count ?? 5)}"></label>
      <label>最低回复<input data-mcp-config="min_reply_count" type="number" min="0" value="${escapeAttr(config.min_reply_count ?? 3)}"></label>
      <label>平台候选上限<input data-mcp-config="max_results" type="number" min="1" max="50" value="${escapeAttr(config.max_results ?? 10)}"></label>
      <label>评论采样<input data-mcp-config="comment_limit" type="number" min="0" max="200" value="${escapeAttr(config.comment_limit ?? 50)}"></label>
      <label>弹幕采样<input data-mcp-config="danmaku_limit" type="number" min="0" max="500" value="${escapeAttr(config.danmaku_limit ?? 200)}"></label>
      <label>缓存秒数<input data-mcp-config="cache_ttl_seconds" type="number" min="60" max="86400" value="${escapeAttr(config.cache_ttl_seconds ?? 21600)}"></label>
      <label>请求超时<input data-mcp-config="request_timeout_seconds" type="number" min="3" max="60" value="${escapeAttr(config.request_timeout_seconds ?? 20)}"></label>
      <button class="btn primary" data-mcp-platform-save="${escapeAttr(platform)}">保存设置</button>
    </div></details><small>revision=${Number(item.revision || 0)} · 登录态不会出现在此 JSON、日志或导出中。</small>
  </article>`;
}

function renderBuiltinPlatforms() {
  const platforms = (state.mcpBuiltin || {}).platforms || {};
  return `<section class="mcp-native-platforms"><div class="mcp-section-heading"><div><span class="eyebrow">PLATFORM ISOLATION</span><h2>平台登录与能力</h2><p>每个平台使用独立 persistent context；关闭服务保留登录态，只有显式注销会删除对应 profile。</p></div></div><div class="mcp-platform-grid">${Object.entries(MCP_PLATFORM_LABELS).map(([platform]) => renderBuiltinPlatformCard(platform, platforms[platform] || {platform, enabled:false, revision:0, config:{}})).join("")}</div></section>`;
}

function renderPreviewDiscussion(item) {
  return (item.discussion || []).slice(0, 12).map(row => `<li><span>${escapeHtml(row.type || "comment")}</span>${escapeHtml(row.text || "")}</li>`).join("");
}

function renderBuiltinPreviewDiagnostics(packet, result) {
  const aggregation = packet && packet.aggregation || {};
  const counts = aggregation.per_platform_counts || {};
  const statuses = packet && packet.platform_statuses || {};
  const selected = aggregation.selected_platforms || [];
  const platforms = [...new Set([...selected, ...Object.keys(statuses), ...Object.keys(counts)])];
  const platformRows = platforms.map(platform => {
    const count = counts[platform] || {};
    const status = statuses[platform] || {};
    return `<tr><td>${escapeHtml(MCP_PLATFORM_LABELS[platform] || platform)}</td><td><code>${escapeHtml(status.state || "unknown")}</code>${status.error_code ? ` · ${escapeHtml(status.error_code)}` : ""}</td><td class="u-tabular">${Number(count.candidates || status.candidate_count || 0)}</td><td class="u-tabular">${Number(count.filtered || 0)}</td><td class="u-tabular">${Number(count.returned || status.returned_count || 0)}</td><td class="u-tabular">${Number(status.elapsed_ms || 0).toLocaleString()}ms</td></tr>`;
  }).join("");
  const stages = aggregation.stages || {};
  const delivery = result && result.delivery || {};
  const warnings = (packet && packet.warnings || []).map(item => `<code>${escapeHtml(item)}</code>`).join(" ");
  return `<div class="mcp-preview-diagnostics">
    <div class="mcp-runtime-overview"><span>请求总上限<strong>${Number(aggregation.requested_limit || 0)}</strong></span><span>实际返回<strong>${Number(aggregation.returned_count || (packet.items || []).length || 0)}</strong></span><span>候选总数<strong>${Number(aggregation.candidate_count || 0)}</strong></span><span>独立来源组<strong>${Number(aggregation.source_group_count || 0)}</strong></span></div>
    <div class="mcp-preview-summary">选中平台：${escapeHtml(selected.map(item => MCP_PLATFORM_LABELS[item] || item).join("、") || "无")} · 成功平台：${escapeHtml((aggregation.successful_platforms || []).map(item => MCP_PLATFORM_LABELS[item] || item).join("、") || "无")} · 实际覆盖：${escapeHtml((aggregation.covered_platforms || []).map(item => MCP_PLATFORM_LABELS[item] || item).join("、") || "无")}</div>
    <div class="mcp-preview-summary">coverage_status=<code>${escapeHtml(aggregation.coverage_status || "empty")}</code> · satisfies_request=<code>${String(aggregation.satisfies_request === true)}</code> · cache_hit=<code>${String(packet.cache_hit === true)}</code> · partial=<code>${String(packet.partial === true)}</code></div>
    ${stages.search || stages.detail ? `<div class="mcp-preview-summary">搜索阶段：<code>${escapeHtml((stages.search || {}).status || "-")}</code> / ${Number((stages.search || {}).elapsed_ms || 0).toLocaleString()}ms · 详情阶段：<code>${escapeHtml((stages.detail || {}).status || "-")}</code> / ${Number((stages.detail || {}).elapsed_ms || 0).toLocaleString()}ms</div>` : ""}
    ${platformRows ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="社交平台检索统计"><table class="data-table"><thead><tr><th scope="col">平台</th><th scope="col">状态 / 错误码</th><th scope="col">候选</th><th scope="col">过滤</th><th scope="col">返回</th><th scope="col">耗时</th></tr></thead><tbody>${platformRows}</tbody></table></div>` : ""}
    ${warnings ? `<div class="alert warn">警告：${warnings}</div>` : ""}
    <div class="mcp-preview-summary">证据交付：<code>${escapeHtml(delivery.evidence_delivery || "not_applicable")}</code> · 安全降级：<code>${String(delivery.visible_output_recovered === true)}</code> · QQ 发送：<code>${escapeHtml(delivery.outbound_delivery || "not_applicable")}</code> · ${escapeHtml(delivery.note || "工具预览不生成最终回复，也不发送 QQ 消息")}</div>
  </div>`;
}

function renderBuiltinPreview() {
  const result = state.mcpPreview;
  const packet = result && result.packet || null;
  const cards = packet ? (packet.items || []).map(item => {
    const imageRefs = [...new Set([...(Array.isArray(item.image_refs) ? item.image_refs : []), item.cover_ref].filter(Boolean))].slice(0, 6);
    const media = imageRefs.length ? `<div class="mcp-media-gallery">${imageRefs.map((ref, index) => `<img src="${API}/mcp/builtin/social-research/cover/${encodeURIComponent(ref)}" alt="帖子图片 ${index + 1}" loading="lazy">`).join("")}</div>` : "";
    return `<article class="mcp-content-card">${media}<div class="mcp-content-body"><span>${escapeHtml(MCP_PLATFORM_LABELS[item.platform] || item.platform)} · source_group=${escapeHtml(item.source_group_id || "-")}</span><h4>${escapeHtml(item.title || "无标题")}</h4><p>${escapeHtml(item.caption_or_body || "")}</p>${renderMcpExternalLink(item.canonical_url, item.canonical_url || "打开规范来源")}<small>quality_score=${Number(item.quality_score || 0).toFixed(2)} · marketing_score=${Number(item.marketing_score || 0).toFixed(2)} · images=${Number(item.image_count || imageRefs.length || 0)}${item.filtered_reason ? ` · ${escapeHtml(item.filtered_reason)}` : ""}${item.detail_status ? ` · detail=${escapeHtml(item.detail_status)}` : ""}</small><ul>${renderPreviewDiscussion(item)}</ul></div></article>`;
  }).join("") : "";
  const claims = result ? (result.claims || []).map(claim => `<article class="mcp-claim-card"><header><strong>${escapeHtml(claim.term || "")}</strong><span>${escapeHtml((claim.game_context || {}).canonical_name || "未确定游戏")}</span></header><p>${escapeHtml(claim.meaning || "")}</p><small>${escapeHtml(claim.safe_usage || "")} · confidence=${Number(claim.extractor_confidence || 0).toFixed(2)}</small><blockquote>${escapeHtml(((claim.evidence_refs || [])[0] || {}).quote || "")}</blockquote></article>`).join("") : "";
  return `<section class="card mcp-native-preview"><div class="mcp-section-heading"><div><span class="eyebrow">RESEARCH PREVIEW</span><h2>检索预览与多梗提取</h2><p>跨平台检索默认总计最多 10 条；黑话研究中一次内容默认提取全部黑话，并要求每条 claim 引用可审计证据。</p></div></div>
    <div class="mcp-preview-form"><label>预览工具<select id="mcp-preview-tool"><option value="social_content_search">social_content_search</option><option value="research_game_slang">research_game_slang</option></select></label><label>游戏（可选）<input id="mcp-preview-game" placeholder="例如：三角洲行动"></label><label>查询词<input id="mcp-preview-term" placeholder="例如：花来"></label><label>结果总上限<input id="mcp-preview-limit" type="number" min="1" max="50" value="10"></label><label>深度<select id="mcp-preview-depth"><option value="auto">两阶段自动</option><option value="deep">四平台深挖</option></select></label><label>最多 claims<input id="mcp-preview-max" type="number" min="1" max="50" value="20"></label><button class="btn primary" data-mcp-preview-run ${state.mcpBusy ? "disabled" : ""}>运行原生 MCP 预览</button></div>
    ${packet ? `<div class="mcp-preview-summary">tool=<code>${escapeHtml(result.tool_name || "")}</code> · packet_id=<code>${escapeHtml(packet.packet_id || "")}</code> · trust=<code>${escapeHtml(packet.trust || "")}</code> · ${packet.partial ? "部分结果" : "完整结果"}</div>${renderBuiltinPreviewDiagnostics(packet, result)}<div class="mcp-content-grid">${cards || '<p class="muted">没有通过质量过滤的内容。</p>'}</div>${result.tool_name === "research_game_slang" ? `<h3>本次提取的 claims</h3><div class="mcp-claim-grid">${claims || '<p class="muted">没有找到带明确“词语 → 含义”关系的证据。</p>'}</div>` : ""}` : ""}
  </section>`;
}

function renderSenseDetail() {
  const sense = state.mcpSelectedSense;
  if (!sense) return "";
  const evidence = (sense.evidence || []).map(item => `<li><div><strong>${escapeHtml(MCP_PLATFORM_LABELS[item.platform] || item.platform)} · ${escapeHtml(item.content_id || "")}</strong><small>${new Date(Number(item.created_at || 0) * 1000).toLocaleString()} · cluster=${escapeHtml(item.source_cluster_id || "")}</small></div><blockquote>${escapeHtml(item.quote || "")}</blockquote></li>`).join("");
  const events = (sense.events || []).slice(0, 30).map(item => `<li><span>${escapeHtml(item.event_type || "")}</span><strong>${escapeHtml(item.old_status || "")} → ${escapeHtml(item.new_status || "")}</strong><small>${new Date(Number(item.created_at || 0) * 1000).toLocaleString()}</small></li>`).join("");
  return `<section class="card mcp-sense-detail"><div class="mcp-section-heading"><div><span class="eyebrow">SENSE DETAIL</span><h2>${escapeHtml(sense.term || "")}</h2><p>${escapeHtml(sense.meaning || "")}</p></div><button class="btn" data-mcp-sense-close>关闭详情</button></div>
    <div class="mcp-sense-facts"><span>状态<strong>${escapeHtml(mcpChineseState(sense.status))}</strong><code>${escapeHtml(sense.status || "")}</code></span><span>游戏<strong>${escapeHtml((sense.game_context || {}).canonical_name || "通用")}</strong></span><span>版本<strong>${escapeHtml(sense.version_context || "未限定")}</strong></span><span>独立内容<strong>${Number(sense.source_count || 0)}</strong></span><span>平台<strong>${Number(sense.platform_count || 0)}</strong></span><span>置信度<strong>${Number(sense.confidence || 0).toFixed(2)}</strong></span></div>
    <p>${escapeHtml(sense.usage_context || "")}</p><p class="muted">安全用法：${escapeHtml(sense.safe_usage || "未填写")}</p>
    <div class="mcp-platform-actions"><button class="btn primary" data-mcp-sense-action="accept" data-sense-id="${escapeAttr(sense.sense_id)}" data-revision="${Number(sense.revision)}">确认并锁定</button><button class="btn" data-mcp-sense-action="reverify" data-sense-id="${escapeAttr(sense.sense_id)}" data-revision="${Number(sense.revision)}">四平台重新验证</button><button class="btn" data-mcp-sense-split="${escapeAttr(sense.sense_id)}" data-revision="${Number(sense.revision)}">拆分 sense</button><button class="btn danger" data-mcp-sense-action="reject" data-sense-id="${escapeAttr(sense.sense_id)}" data-revision="${Number(sense.revision)}">拒绝</button></div>
    <h3>证据来源</h3><ul class="mcp-evidence-list">${evidence || '<li class="muted">暂无证据详情。</li>'}</ul><h3>状态历史</h3><ul class="mcp-event-list">${events || '<li class="muted">暂无事件。</li>'}</ul>
  </section>`;
}

function renderLearningCenter() {
  const statuses = ["verified","understand_only","observed","disputed","stale","rejected","manual_locked"];
  const filter = state.mcpSenseFilter || "";
  const senses = (state.mcpSenses || []).filter(item => !filter || item.status === filter);
  const cards = senses.map(sense => `<article class="mcp-sense-card"><label><input type="checkbox" data-mcp-sense-select="${escapeAttr(sense.sense_id)}" ${(state.mcpSelectedSenseIds || []).includes(sense.sense_id) ? "checked" : ""}><span>选择合并</span></label><button data-mcp-sense-open="${escapeAttr(sense.sense_id)}"><header><strong>${escapeHtml(sense.term || "")}</strong><span>${escapeHtml(mcpChineseState(sense.status))}</span></header><p>${escapeHtml(sense.meaning || "")}</p><small>${escapeHtml((sense.game_context || {}).canonical_name || "通用语境")} · sources=${Number(sense.source_count || 0)} · platforms=${Number(sense.platform_count || 0)} · confidence=${Number(sense.confidence || 0).toFixed(2)}</small></button></article>`).join("");
  return `<section class="card mcp-learning-center"><div class="mcp-section-heading"><div><span class="eyebrow">SLANG LEARNING CENTER</span><h2>学习中心</h2><p>自动收录不需要前置人工确认，但每次升级、冲突、降级和人工操作都有事件记录。</p></div><button class="btn" data-mcp-sense-merge ${(state.mcpSelectedSenseIds || []).length < 2 ? "disabled" : ""}>合并所选 sense</button></div><nav class="mcp-learning-tabs"><button data-mcp-sense-filter="" class="${!filter ? "active" : ""}">全部</button>${statuses.map(status => `<button data-mcp-sense-filter="${status}" class="${filter === status ? "active" : ""}">${escapeHtml(mcpChineseState(status))}</button>`).join("")}</nav><div class="mcp-sense-grid">${cards || '<p class="muted">当前分类没有 sense。</p>'}</div></section>${renderSenseDetail()}`;
}

function renderBuiltinMcp() {
  return `${renderBuiltinServiceCard()}${renderBuiltinPlatforms()}${renderBuiltinPreview()}${renderLearningCenter()}`;
}

function renderMcp() {
  const running = (state.mcpInstallations || []).filter(item => item.process_state === "running").length;
  const effective = (state.mcpInstallations || []).reduce((sum, item) => sum + Number(item.effective_count || 0), 0);
  return `<div class="mcp-console">
    <section class="mcp-hero"><div><span class="eyebrow">MODEL CONTEXT PROTOCOL / CONTROL PLANE</span><h1>MCP 管理</h1><p>将 Registry discovery 与 Runtime installations 分离：先核验权威 metadata，再明确控制 process 与逐 tool 授权。</p></div><div class="mcp-hero-readout"><span>sources<strong>${(state.mcpSources || []).length}</strong></span><span>installations<strong>${(state.mcpInstallations || []).length}</strong></span><span>running<strong>${running}</strong></span><span>effective<strong>${effective}</strong></span></div></section>
    <nav class="mcp-primary-tabs"><button data-mcp-tab="builtin" class="${state.mcpTab === "builtin" ? "active" : ""}">原生 MCP</button><button data-mcp-tab="extension" class="${state.mcpTab === "extension" ? "active" : ""}">扩展 MCP</button></nav>
    ${renderMcpOperationResult()}
    ${state.mcpTab === "builtin" ? renderBuiltinMcp() : `${renderMcpRegistryDiscovery()}${renderMcpRuntimeInstallations()}${renderMcpInstallConfirmation()}`}
  </div>`;
}

function mergeMcpRegistryResults(current, incoming) {
  const merged = Array.isArray(current) ? current.slice() : [];
  const positions = new Map(merged.map((item, index) => [`${item.source_id || state.mcpSourceId}\u0000${item.name || ""}`, index]));
  for (const item of Array.isArray(incoming) ? incoming : []) {
    const key = `${item.source_id || state.mcpSourceId}\u0000${item.name || ""}`;
    if (positions.has(key)) merged[positions.get(key)] = {...merged[positions.get(key)], ...item};
    else {
      positions.set(key, merged.length);
      merged.push(item);
    }
  }
  return merged;
}

function replaceMcpInstallation(item) {
  if (!item || !item.installation_id) return;
  const installations = state.mcpInstallations || [];
  const index = installations.findIndex(current => current.installation_id === item.installation_id);
  if (index < 0) state.mcpInstallations = [item, ...installations];
  else state.mcpInstallations = installations.map((current, currentIndex) => currentIndex === index ? item : current);
}

async function loadMcpInstallations() {
  const data = await api("/mcp/installations");
  state.mcpInstallations = data.installations || [];
}

async function searchMcpRegistry({append=false}={}) {
  if (state.mcpBusy) return;
  const input = document.getElementById("mcp-search-input");
  const source = document.getElementById("mcp-source-select");
  if (!append) {
    state.mcpQuery = String(input ? input.value : state.mcpQuery || "").trim();
    state.mcpSourceId = String(source ? source.value : state.mcpSourceId || "official");
    state.mcpNextCursor = "";
    state.mcpDetail = null;
  }
  if (append && !state.mcpNextCursor) return;
  const params = {source_id:state.mcpSourceId, q:state.mcpQuery, limit:"30"};
  if (append) params.cursor = state.mcpNextCursor;
  const requestSourceId = state.mcpSourceId;
  const requestQuery = state.mcpQuery;
  const requestCursor = append ? state.mcpNextCursor : "";
  state.mcpBusy = true;
  state.mcpLoadingMore = append;
  render();
  try {
    const data = await api("/mcp/search?" + new URLSearchParams(params).toString());
    if (state.mcpSourceId !== requestSourceId || state.mcpQuery !== requestQuery || (append && state.mcpNextCursor !== requestCursor)) return;
    state.mcpResults = append
      ? mergeMcpRegistryResults(state.mcpResults, data.servers)
      : mergeMcpRegistryResults([], data.servers);
    state.mcpNextCursor = typeof data.next_cursor === "string" ? data.next_cursor : "";
    state.mcpSearchLoaded = true;
  } catch (error) {
    const diagnostic = operationDiagnosticFromError(error, "MCP Registry 搜索失败");
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title || "MCP Registry 搜索失败");
  } finally {
    state.mcpBusy = false;
    state.mcpLoadingMore = false;
    render();
  }
}

async function openMcpDetail(name, sourceId) {
  if (!name || state.mcpBusy) return;
  state.mcpBusy = true;
  render();
  try {
    state.mcpDetail = await api("/mcp/detail?" + new URLSearchParams({source_id:sourceId || state.mcpSourceId, name}).toString());
    const first = (state.mcpDetail.packages || []).find(item => item.supported === true);
    state.mcpPackageIndex = first ? first.index : 0;
  } catch (error) {
    const diagnostic = operationDiagnosticFromError(error, "MCP Server 详情读取失败");
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title || "MCP Server 详情读取失败");
  } finally {
    state.mcpBusy = false;
    render();
  }
}

function prepareMcpInstall() {
  const detail = state.mcpDetail;
  if (!detail) return;
  const selected = (detail.packages || []).find(item => Number(item.index) === Number(state.mcpPackageIndex) && item.supported === true);
  if (!selected) return;
  const inputs = {};
  const inputSummary = [];
  let missing = "";
  document.querySelectorAll("[data-mcp-install-input]").forEach(element => {
    const key = element.getAttribute("data-mcp-install-input") || "";
    const spec = (selected.inputs || []).find(item => item.key === key) || {};
    const value = String(element.value || "");
    if (key && value) inputs[key] = value;
    if (!value && spec.required && !spec.default && !missing) missing = key;
    inputSummary.push({key, location:spec.location || "input", secret:spec.secret === true, provided:Boolean(value)});
  });
  if (missing) {
    const diagnostic = {ok:false, code:"mcp_install_input_missing", phase:"client_validation", title:"MCP 安装输入不完整", message:`必填输入 ${missing} 尚未提供。`, details:[{label:"缺少字段", value:missing, status:"error"}], steps:[], warnings:[], suggestion:"补齐必填字段后重新审阅 command plan。", retryable:true, partial:false, outcome_unknown:false};
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title);
    return;
  }
  state.mcpPrefix = String(document.getElementById("mcp-prefix-input")?.value || "").trim();
  _mcpPendingInstall = {
    package:selected,
    plan:mcpCommandPlan(selected),
    inputSummary,
    payload:{
      source_id:String((detail.source || {}).id || state.mcpSourceId),
      server_name:String((detail.server || {}).name || ""),
      package_index:Number(selected.index),
      package_digest:String(selected.digest || ""),
      inputs,
      name_prefix:state.mcpPrefix,
      confirm_execution:true,
      fresh_fetch:true,
    },
  };
  render();
}

async function installMcpServer() {
  if (!_mcpPendingInstall || state.mcpBusy) return;
  const pending = _mcpPendingInstall;
  state.mcpBusy = true;
  render();
  try {
    const result = await api("/mcp/install", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify(pending.payload)});
    persistMcpOperationResult(result);
    replaceMcpInstallation(result.installation);
    state.mcpDetail = null;
    state.mcpPrefix = "";
    alertFlash("ok", "MCP Server 已完成预检并安装");
  } catch (error) {
    const diagnostic = operationDiagnosticFromError(error, "MCP Server 安装失败");
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title || "MCP Server 安装失败");
  } finally {
    _mcpPendingInstall = null;
    state.mcpBusy = false;
    render();
  }
}

async function toggleMcpInstallation(installationId, enabled) {
  if (!installationId || state.mcpBusy) return;
  state.mcpBusy = true;
  render();
  try {
    const result = await api(`/mcp/installations/${encodeURIComponent(installationId)}/toggle`, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({enabled})});
    persistMcpOperationResult(result);
    replaceMcpInstallation(result.installation);
    if (installationId === MCP_BUILTIN_ID) await refreshBuiltinMcp();
    alertFlash("ok", enabled ? "Server 已允许启动" : "Server 已停止运行，工具授权保留");
  } catch (error) {
    const diagnostic = operationDiagnosticFromError(error, "MCP Server 状态切换失败");
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title || "MCP Server 状态切换失败");
  } finally {
    state.mcpBusy = false;
    render();
  }
}

async function toggleManagedMcpTool(installationId, remoteName, enabled, risk) {
  if (!installationId || !remoteName || state.mcpBusy) return;
  if (enabled) {
    const riskText = risk === "builtin"
      ? "这是宿主可信清单内的只读工具，固定 side_effect=none；平台内容仍是不可信数据。"
      : risk === "read"
      ? "publisher 的 readOnlyHint 是未受信任声明，不能作为安全保证。"
      : "publisher 未提供可信只读保证，副作用按 unknown 处理。";
    if (!confirm(`${riskText}\n授权后仅在 Server 运行且 tool 已注册时才可调用。确认授权？`)) return;
  }
  state.mcpBusy = true;
  render();
  try {
    const result = await api(`/mcp/installations/${encodeURIComponent(installationId)}/tools/${encodeURIComponent(remoteName)}/toggle`, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({enabled, confirm_side_effect:enabled})});
    persistMcpOperationResult(result);
    replaceMcpInstallation(result.installation);
    if (installationId === MCP_BUILTIN_ID) await refreshBuiltinMcp();
    alertFlash("ok", enabled ? "MCP tool 已授权" : "MCP tool 已撤销授权");
  } catch (error) {
    const diagnostic = operationDiagnosticFromError(error, "MCP 工具授权切换失败");
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title || "MCP 工具授权切换失败");
  } finally {
    state.mcpBusy = false;
    render();
  }
}

async function deleteMcpInstallation(installationId) {
  if (!installationId || state.mcpBusy) return;
  if (!confirm("确认删除这个 MCP installation、tool policy 与 Secret 文件条目？该操作不会保留授权。")) return;
  state.mcpBusy = true;
  render();
  try {
    await api(`/mcp/installations/${encodeURIComponent(installationId)}`, {method:"DELETE", headers:{"content-type":"application/json"}, body:JSON.stringify({confirm:"delete"})});
    persistMcpOperationResult({ok:true, code:"mcp_installation_deleted", phase:"delete", title:"MCP installation 已删除", message:"process、tool policy 与独立 Secret 条目已由服务端删除。", details:[{label:"installation_id", value:installationId, status:"ok"}], steps:[{key:"delete", label:"删除 installation", status:"ok", message:"服务端返回明确成功。"}], warnings:[], suggestion:"", retryable:false, partial:false, outcome_unknown:false, operation_id:installationId});
    state.mcpInstallations = (state.mcpInstallations || []).filter(item => item.installation_id !== installationId);
    alertFlash("ok", "MCP installation 已删除");
  } catch (error) {
    const diagnostic = operationDiagnosticFromError(error, "MCP Server 删除失败");
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title || "MCP Server 删除失败");
  } finally {
    state.mcpBusy = false;
    render();
  }
}

async function reloadMcpRuntime() {
  if (state.mcpBusy) return;
  state.mcpBusy = true;
  render();
  try {
    const result = await api("/mcp/reload", {method:"POST"});
    persistMcpOperationResult(result);
    let refreshFailed = false;
    try {
      await loadMcpInstallations();
      await refreshBuiltinMcp();
    } catch (refreshError) {
      refreshFailed = true;
      const refreshDiagnostic = operationDiagnosticFromError(refreshError, "MCP reload 后状态刷新失败");
      alertFlash("err", refreshDiagnostic.title || "MCP reload 后状态刷新失败");
    }
    if (!refreshFailed) alertFlash(result.diagnostic && result.diagnostic.ok === false ? "err" : "ok", result.diagnostic?.title || "MCP runtime 已重载");
  } catch (error) {
    const diagnostic = operationDiagnosticFromError(error, "MCP reload 失败");
    persistMcpOperationResult(diagnostic);
    alertFlash("err", diagnostic.title || "MCP reload 失败");
  } finally {
    state.mcpBusy = false;
    render();
  }
}

async function refreshBuiltinMcp() {
  const [builtin, senses] = await Promise.all([
    api("/mcp/builtin/social-research/status", {cache:"no-store"}),
    api("/mcp/builtin/social-research/slang/senses?limit=200", {cache:"no-store"}),
  ]);
  state.mcpBuiltin = builtin;
  state.mcpSenses = senses.senses || [];
  if (builtin.installation) replaceMcpInstallation(builtin.installation);
}

function builtinPlatformConfig(platform) {
  const card = document.querySelector(`.mcp-platform-card[data-platform="${CSS.escape(platform)}"]`);
  const config = {};
  if (!card) return config;
  card.querySelectorAll("[data-mcp-config]").forEach(input => {
    const key = input.getAttribute("data-mcp-config");
    if (!key) return;
    config[key] = key === "quality_mode" ? String(input.value || "balanced") : Number(input.value || 0);
  });
  return config;
}

function normalizedBuiltinPlatformConfig(value) {
  const input = value && typeof value === "object" ? value : {};
  return Object.fromEntries(Object.entries(input).filter(([key]) => MCP_PLATFORM_CONFIG_KEYS.has(key)));
}

async function configureBuiltinPlatform(platform, enabled, {useForm=false}={}) {
  const current = ((state.mcpBuiltin || {}).platforms || {})[platform];
  if (!current || state.mcpBusy) return;
  const desiredConfig = normalizedBuiltinPlatformConfig(useForm ? builtinPlatformConfig(platform) : current.config);
  state.mcpBusy = true; render();
  try {
    await api(`/mcp/builtin/social-research/platforms/${encodeURIComponent(platform)}/configure`, {
      method:"POST", headers:{"content-type":"application/json"},
      body:JSON.stringify({enabled, revision:Number(current.revision || 0), config:desiredConfig}),
    });
    await refreshBuiltinMcp();
    alertFlash("ok", `${MCP_PLATFORM_LABELS[platform] || platform}配置已保存`);
  } catch (error) {
    try { await refreshBuiltinMcp(); } catch {}
    alertFlash("err", operationDiagnosticFromError(error, "平台配置失败").message || "平台配置失败");
  } finally { state.mcpBusy = false; render(); }
}

function startBuiltinAuthPolling() {
  if (_mcpAuthTimer) clearInterval(_mcpAuthTimer);
  _mcpAuthTimer = setInterval(async () => {
    if (state.view !== "mcp" || state.mcpTab !== "builtin" || _mcpAuthPollInFlight) return;
    const sessions = Object.entries(state.mcpAuth || {}).filter(([, value]) => value && ["starting","waiting_scan","manual_verification_required","risk_controlled","qr_expired"].includes(value.status));
    if (!sessions.length) return;
    _mcpAuthPollInFlight = true;
    try {
      for (const [platform, session] of sessions) {
        try {
          const next = await api(`/mcp/builtin/social-research/auth/${encodeURIComponent(session.session_id)}/status?platform=${encodeURIComponent(platform)}`, {cache:"no-store"});
          state.mcpAuth = {...state.mcpAuth, [platform]:next};
          if (next.status === "success") await refreshBuiltinMcp();
        } catch {}
      }
      render();
    } finally {
      _mcpAuthPollInFlight = false;
    }
  }, 3000);
}

async function startBuiltinAuth(platform, mode="embedded_qr") {
  if (state.mcpBusy) return;
  state.mcpBusy = true; render();
  try {
    const session = await api("/mcp/builtin/social-research/auth/start", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({platform, mode})});
    state.mcpAuth = {...state.mcpAuth, [platform]:session};
    startBuiltinAuthPolling();
    const browserMode = session.login_mode === "manual_browser";
    const failed = session.status === "error" || session.status === "risk_controlled";
    alertFlash(failed ? "err" : "ok", browserMode ? "请在普通系统浏览器完成登录，完成后关闭该窗口" : failed ? "登录会话启动失败，请查看状态说明" : "请使用官方客户端扫描 WebUI 二维码");
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "登录启动失败").message || "登录启动失败"); }
  finally { state.mcpBusy = false; render(); }
}

async function logoutBuiltinPlatform(platform) {
  const exact = `确认注销${MCP_PLATFORM_LABELS[platform] || platform}`;
  const input = prompt(`注销会删除该平台的独立浏览器 profile，其他平台不受影响。\n请输入：${exact}`) || "";
  if (input !== exact) return;
  state.mcpBusy = true; render();
  try {
    await api("/mcp/builtin/social-research/auth/logout", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({platform, confirm:exact})});
    const nextAuth = {...state.mcpAuth}; delete nextAuth[platform]; state.mcpAuth = nextAuth;
    await refreshBuiltinMcp();
    alertFlash("ok", `${MCP_PLATFORM_LABELS[platform] || platform}已注销，profile 已删除`);
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "注销失败").message || "注销失败"); }
  finally { state.mcpBusy = false; render(); }
}

async function runBuiltinPreview() {
  const tool = String(document.getElementById("mcp-preview-tool")?.value || "social_content_search");
  const term = String(document.getElementById("mcp-preview-term")?.value || "").trim();
  const game = String(document.getElementById("mcp-preview-game")?.value || "").trim();
  const depth = String(document.getElementById("mcp-preview-depth")?.value || "auto");
  const maxClaims = Number(document.getElementById("mcp-preview-max")?.value || 20);
  const limit = Number(document.getElementById("mcp-preview-limit")?.value || 10);
  if (!term) { alertFlash("err", "请输入要查证的游戏黑话或梗"); return; }
  state.mcpBusy = true; render();
  try {
    state.mcpPreview = await api("/mcp/builtin/social-research/preview", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({tool, term, query:[game, term].filter(Boolean).join(" "), context:`查证 ${term} 的玩家语境含义`, game, depth, limit, max_claims:maxClaims})});
    await refreshBuiltinMcp();
    alertFlash("ok", `原生 MCP 返回 ${Number((state.mcpPreview.packet || {}).aggregation?.returned_count || (state.mcpPreview.packet || {}).items?.length || 0)} 条内容`);
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "检索预览失败").message || "检索预览失败"); }
  finally { state.mcpBusy = false; render(); }
}

async function openSlangSense(senseId) {
  try {
    const data = await api(`/mcp/builtin/social-research/slang/senses/${encodeURIComponent(senseId)}`, {cache:"no-store"});
    state.mcpSelectedSense = data.sense || null; render();
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Sense 详情读取失败").message || "Sense 详情读取失败"); }
}

async function actOnSlangSense(action, senseId, revision) {
  state.mcpBusy = true; render();
  try {
    const result = await api(`/mcp/builtin/social-research/slang/senses/${encodeURIComponent(senseId)}/${encodeURIComponent(action)}`, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({revision:Number(revision)})});
    state.mcpSelectedSense = result.sense || null;
    await refreshBuiltinMcp();
    alertFlash("ok", "Sense 状态已更新");
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Sense 操作失败").message || "Sense 操作失败"); }
  finally { state.mcpBusy = false; render(); }
}

async function mergeSelectedSenses() {
  const ids = state.mcpSelectedSenseIds || [];
  if (ids.length < 2) return;
  const selected = ids.map(id => (state.mcpSenses || []).find(item => item.sense_id === id)).filter(Boolean);
  const target = selected[0];
  if (!target || !confirm(`将其余 ${selected.length - 1} 个 sense 合并到“${target.term}：${target.meaning}”，并人工锁定目标？`)) return;
  const revisions = Object.fromEntries(selected.map(item => [item.sense_id, Number(item.revision)]));
  state.mcpBusy = true; render();
  try {
    const result = await api("/mcp/builtin/social-research/slang/senses/merge", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({target_sense_id:target.sense_id, source_sense_ids:selected.slice(1).map(item => item.sense_id), revisions})});
    state.mcpSelectedSenseIds = [];
    state.mcpSelectedSense = result.sense || null;
    await refreshBuiltinMcp();
    alertFlash("ok", "Sense 已合并并锁定");
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Sense 合并失败").message || "Sense 合并失败"); }
  finally { state.mcpBusy = false; render(); }
}

async function splitSelectedSense(senseId, revision) {
  const sense = state.mcpSelectedSense;
  if (!sense || sense.sense_id !== senseId) return;
  const claimId = prompt("输入要移到新 sense 的证据 claim_id：", (sense.evidence || [])[0]?.claim_id || "") || "";
  if (!claimId) return;
  const meaning = prompt("输入新 sense 的含义：", sense.meaning || "") || "";
  if (!meaning) return;
  state.mcpBusy = true; render();
  try {
    const result = await api(`/mcp/builtin/social-research/slang/senses/${encodeURIComponent(senseId)}/split`, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({revision:Number(revision), claim_ids:[claimId], sense:{meaning}})});
    state.mcpSelectedSense = result.sense || null;
    await refreshBuiltinMcp();
    alertFlash("ok", "新 sense 已拆分并人工锁定");
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "Sense 拆分失败").message || "Sense 拆分失败"); }
  finally { state.mcpBusy = false; render(); }
}

if (!window.__personificationMcpPageEvents) {
  window.__personificationMcpPageEvents = true;
  document.addEventListener("click", event => {
    const element = event.target instanceof Element ? event.target.closest("[data-mcp-operation-clear],[data-mcp-search],[data-mcp-load-more],[data-mcp-detail],[data-mcp-detail-close],[data-mcp-install-plan],[data-mcp-install-confirm],[data-mcp-install-cancel],[data-mcp-installation-toggle],[data-mcp-tool-toggle],[data-mcp-delete],[data-mcp-reload],[data-mcp-tab],[data-mcp-platform-toggle],[data-mcp-platform-save],[data-mcp-auth-start],[data-mcp-auth-manual],[data-mcp-auth-logout],[data-mcp-preview-run],[data-mcp-sense-filter],[data-mcp-sense-open],[data-mcp-sense-close],[data-mcp-sense-action],[data-mcp-sense-merge],[data-mcp-sense-split]") : null;
    if (!element) return;
    if (element.hasAttribute("data-mcp-operation-clear")) { persistMcpOperationResult(null); render(); return; }
    if (element.hasAttribute("data-mcp-search")) { searchMcpRegistry(); return; }
    if (element.hasAttribute("data-mcp-load-more")) { searchMcpRegistry({append:true}); return; }
    if (element.hasAttribute("data-mcp-detail")) { openMcpDetail(element.getAttribute("data-mcp-detail") || "", element.getAttribute("data-mcp-source") || state.mcpSourceId); return; }
    if (element.hasAttribute("data-mcp-detail-close")) { state.mcpDetail = null; _mcpPendingInstall = null; render(); return; }
    if (element.hasAttribute("data-mcp-install-plan")) { prepareMcpInstall(); return; }
    if (element.hasAttribute("data-mcp-install-confirm")) { installMcpServer(); return; }
    if (element.hasAttribute("data-mcp-install-cancel")) { _mcpPendingInstall = null; render(); return; }
    if (element.hasAttribute("data-mcp-installation-toggle")) { toggleMcpInstallation(element.getAttribute("data-mcp-installation-toggle") || "", element.getAttribute("data-mcp-enabled") === "true"); return; }
    if (element.hasAttribute("data-mcp-tool-toggle")) { toggleManagedMcpTool(element.getAttribute("data-mcp-installation") || "", element.getAttribute("data-mcp-tool-toggle") || "", element.getAttribute("data-mcp-enabled") === "true", element.getAttribute("data-mcp-risk") || "unknown"); return; }
    if (element.hasAttribute("data-mcp-delete")) { deleteMcpInstallation(element.getAttribute("data-mcp-delete") || ""); return; }
    if (element.hasAttribute("data-mcp-reload")) { reloadMcpRuntime(); return; }
    if (element.hasAttribute("data-mcp-tab")) { state.mcpTab = element.getAttribute("data-mcp-tab") || "builtin"; if (state.mcpTab === "builtin") startBuiltinAuthPolling(); render(); return; }
    if (element.hasAttribute("data-mcp-platform-toggle")) { configureBuiltinPlatform(element.getAttribute("data-mcp-platform-toggle") || "", element.getAttribute("data-enabled") === "true"); return; }
    if (element.hasAttribute("data-mcp-platform-save")) { const platform=element.getAttribute("data-mcp-platform-save") || ""; const current=((state.mcpBuiltin||{}).platforms||{})[platform]||{}; configureBuiltinPlatform(platform, current.enabled === true, {useForm:true}); return; }
    if (element.hasAttribute("data-mcp-auth-start")) { startBuiltinAuth(element.getAttribute("data-mcp-auth-start") || ""); return; }
    if (element.hasAttribute("data-mcp-auth-manual")) { startBuiltinAuth(element.getAttribute("data-mcp-auth-manual") || "", "manual_browser"); return; }
    if (element.hasAttribute("data-mcp-auth-logout")) { logoutBuiltinPlatform(element.getAttribute("data-mcp-auth-logout") || ""); return; }
    if (element.hasAttribute("data-mcp-preview-run")) { runBuiltinPreview(); return; }
    if (element.hasAttribute("data-mcp-sense-filter")) { state.mcpSenseFilter=element.getAttribute("data-mcp-sense-filter") || ""; render(); return; }
    if (element.hasAttribute("data-mcp-sense-open")) { openSlangSense(element.getAttribute("data-mcp-sense-open") || ""); return; }
    if (element.hasAttribute("data-mcp-sense-close")) { state.mcpSelectedSense=null; render(); return; }
    if (element.hasAttribute("data-mcp-sense-action")) { actOnSlangSense(element.getAttribute("data-mcp-sense-action") || "", element.getAttribute("data-sense-id") || "", Number(element.getAttribute("data-revision") || 0)); return; }
    if (element.hasAttribute("data-mcp-sense-merge")) { mergeSelectedSenses(); return; }
    if (element.hasAttribute("data-mcp-sense-split")) { splitSelectedSense(element.getAttribute("data-mcp-sense-split") || "", Number(element.getAttribute("data-revision") || 0)); }
  });
  document.addEventListener("change", event => {
    const element = event.target;
    if (!(element instanceof Element)) return;
    if (element.id === "mcp-source-select") {
      state.mcpSourceId = String(element.value || "official");
      state.mcpResults = [];
      state.mcpNextCursor = "";
      state.mcpSearchLoaded = false;
      state.mcpDetail = null;
      render();
    } else if (element.id === "mcp-package-select") {
      state.mcpPackageIndex = Number(element.value || 0);
      _mcpPendingInstall = null;
      render();
    } else if (element.hasAttribute("data-mcp-sense-select")) {
      const id = element.getAttribute("data-mcp-sense-select") || "";
      const selected = new Set(state.mcpSelectedSenseIds || []);
      if (element.checked) selected.add(id); else selected.delete(id);
      state.mcpSelectedSenseIds = [...selected];
      render();
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Enter" && event.target instanceof Element && event.target.id === "mcp-search-input") {
      event.preventDefault();
      searchMcpRegistry();
    }
  });
}
