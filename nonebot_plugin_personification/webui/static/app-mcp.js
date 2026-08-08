const _MCP_OPERATION_RESULT_STORAGE_KEY = "personification_mcp_operation_result_v1";
let _mcpPendingInstall = null;
let _mcpPendingInteractiveAuth = null;

function clearMcpSensitiveState() {
  _mcpPendingInstall = null;
  _mcpPendingInteractiveAuth = null;
  resetBuiltinInteractiveClientState();
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
  _mcpPendingInteractiveAuth = null;
  cancelActiveBuiltinInteractivePointer({keepalive:true});
  if (_mcpAuthTimer) { clearInterval(_mcpAuthTimer); _mcpAuthTimer = null; }
  stopBuiltinInteractiveFramePolling();
  clearBuiltinInteractiveActionQueue();
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
let _mcpInteractivePointer = null;
let _mcpInteractiveFrameTimer = null;
let _mcpInteractiveFrameInFlight = false;
let _mcpInteractiveFrameAbort = null;
let _mcpInteractiveFrameGeneration = 0;
let _mcpInteractiveActionDraining = false;
let _mcpInteractiveDrainPromise = null;
let _mcpInteractiveLifecycleBusy = false;
const _mcpInteractiveActionQueue = [];
const _mcpInteractiveFrames = new Map();
const _MCP_INTERACTIVE_FRAME_INTERVAL_MS = 1200;
const _MCP_INTERACTIVE_FRAME_ACTIVE_INTERVAL_MS = 180;
const _MCP_INTERACTIVE_POINTER_MOVE_INTERVAL_MS = 80;
const _MCP_INTERACTIVE_POINTER_MOVE_MAX_POINTS = 6;
const _MCP_INTERACTIVE_ACTION_QUEUE_MAX = 32;
const _MCP_INTERACTIVE_FRAME_PLACEHOLDER_SRC = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";

function clearBuiltinInteractiveFrameCache() {
  for (const entry of _mcpInteractiveFrames.values()) {
    if (entry.objectUrl) {
      try { URL.revokeObjectURL(entry.objectUrl); } catch {}
    }
  }
  _mcpInteractiveFrames.clear();
}

function stopBuiltinInteractiveFramePolling() {
  _mcpInteractiveFrameGeneration += 1;
  if (_mcpInteractiveFrameTimer) {
    clearTimeout(_mcpInteractiveFrameTimer);
    _mcpInteractiveFrameTimer = null;
  }
  if (_mcpInteractiveFrameAbort) {
    try { _mcpInteractiveFrameAbort.abort(); } catch {}
    _mcpInteractiveFrameAbort = null;
  }
  _mcpInteractiveFrameInFlight = false;
  clearBuiltinInteractiveFrameCache();
}

function resetBuiltinInteractiveClientState() {
  cancelActiveBuiltinInteractivePointer({keepalive:true});
  clearBuiltinInteractiveActionQueue();
  _mcpInteractiveActionDraining = false;
  _mcpInteractiveDrainPromise = null;
  _mcpInteractiveLifecycleBusy = false;
  stopBuiltinInteractiveFramePolling();
}

function builtinInteractiveFrameEntry(sessionId) {
  const key = String(sessionId || "");
  let entry = _mcpInteractiveFrames.get(key);
  if (!entry) {
    // The server revision only says a frame exists remotely. Until this
    // browser owns a Blob URL it must request from revision 0, otherwise a
    // transient first-fetch failure can leave it receiving HTTP 204 forever.
    entry = {revision:0, objectUrl:"", force:true, updatedAt:0};
    _mcpInteractiveFrames.set(key, entry);
  }
  return entry;
}

function updateBuiltinInteractiveTransportStatus(sessionId, message="") {
  const panel = Array.from(document.querySelectorAll("[data-mcp-interactive-session]"))
    .find(item => item.getAttribute("data-mcp-interactive-session") === String(sessionId || ""));
  const status = panel?.querySelector("[data-mcp-interactive-status]");
  if (!status) return;
  const queued = _mcpInteractiveActionQueue.filter(item => item.sessionId === sessionId).length;
  const pointer = _mcpInteractivePointer?.sessionId === String(sessionId || "") ? _mcpInteractivePointer : null;
  if (message) status.textContent = message;
  else if (_mcpInteractiveLifecycleBusy) status.textContent = "正在检查或关闭登录会话";
  else if (pointer?.ending) status.textContent = pointer.cancelRequested ? "正在取消拖动" : "已松开，正在同步最终位置";
  else if (pointer?.requestInFlight) status.textContent = "正在同步移动";
  else if (pointer) status.textContent = "正在按住，等待移动";
  else if (_mcpInteractiveActionDraining || queued) status.textContent = `正在发送操作${queued ? ` · 待处理 ${queued}` : ""}`;
  else if (_mcpInteractiveFrameInFlight) status.textContent = "正在更新画面";
  else status.textContent = "画面与操作通道就绪";
}

function clearBuiltinInteractiveActionQueue() {
  while (_mcpInteractiveActionQueue.length) {
    const item = _mcpInteractiveActionQueue.shift();
    try { item.resolve(null); } catch {}
  }
}

function updateBuiltinAuthDom(platform, auth) {
  const card = document.querySelector(`.mcp-platform-card[data-platform="${CSS.escape(platform)}"]`);
  if (!card) return;
  const status = card.querySelector("[data-mcp-auth-status]");
  const code = card.querySelector("[data-mcp-auth-code]");
  const remaining = card.querySelector("[data-mcp-auth-remaining]");
  const displayUrl = card.querySelector("[data-mcp-interactive-url]");
  if (status) status.textContent = mcpChineseState(auth.status);
  if (code) code.textContent = String(auth.status || "");
  if (remaining) remaining.textContent = builtinAuthSessionSummary(auth);
  if (displayUrl) displayUrl.textContent = `当前官方页面：${String(auth.interactive_display_url || "正在读取")}`;
}

function builtinAuthRenderSignature(auth) {
  if (!auth) return "";
  return JSON.stringify([
    auth.status || "",
    auth.login_mode || "",
    auth.verification_kind || "",
    auth.error_code || "",
    auth.interactive_available === true,
    auth.official_window_open === true,
    Number(auth.qr_revision || 0),
  ]);
}

async function refreshVisibleBuiltinInteractiveFrames({force=false, sessionId=""}={}) {
  const interactiveViewActive = (state.view === "mcp" && state.mcpTab === "builtin") || (state.view === "config" && state.activeGroup === "视频理解");
  if (!interactiveViewActive) return;
  const images = Array.from(document.querySelectorAll("[data-mcp-interactive-frame]"));
  const visibleSessionIds = new Set(images.map(image => image.getAttribute("data-session-id") || "").filter(Boolean));
  for (const [cachedSessionId, entry] of _mcpInteractiveFrames.entries()) {
    if (visibleSessionIds.has(cachedSessionId)) continue;
    if (entry.objectUrl) {
      try { URL.revokeObjectURL(entry.objectUrl); } catch {}
    }
    _mcpInteractiveFrames.delete(cachedSessionId);
  }
  if (!images.length) return;
  if (sessionId && force) {
    const entry = builtinInteractiveFrameEntry(sessionId);
    entry.force = true;
  }
  if (_mcpInteractiveFrameInFlight || _mcpInteractiveActionDraining || _mcpInteractiveLifecycleBusy) {
    images.forEach(image => updateBuiltinInteractiveTransportStatus(image.getAttribute("data-session-id") || ""));
    return;
  }
  _mcpInteractiveFrameInFlight = true;
  const controller = new AbortController();
  _mcpInteractiveFrameAbort = controller;
  try {
    for (const image of images) {
      const currentSessionId = image.getAttribute("data-session-id") || "";
      if (!currentSessionId || (sessionId && currentSessionId !== sessionId)) continue;
      const platform = image.getAttribute("data-platform") || "";
      const entry = builtinInteractiveFrameEntry(currentSessionId);
      const forceCurrent = force || entry.force;
      entry.force = false;
      updateBuiltinInteractiveTransportStatus(currentSessionId);
      const revision = forceCurrent || !entry.objectUrl ? 0 : Math.max(0, Number(entry.revision || 0));
      const consumerService = platform === "gemini_web" ? "gemini" : platform === "mimo_asr_web" ? "mimo_asr" : "";
      const path = consumerService
        ? `/media/web/${consumerService}/auth/${encodeURIComponent(currentSessionId)}/frame?revision=${encodeURIComponent(String(revision))}`
        : `/mcp/builtin/social-research/auth/${encodeURIComponent(currentSessionId)}/frame?platform=${encodeURIComponent(platform)}&revision=${encodeURIComponent(String(revision))}`;
      const response = await fetch(API + path, {
        method:"GET",
        credentials:"include",
        cache:"no-store",
        signal:controller.signal,
      });
      if (response.status === 401) {
        clearInMemorySensitiveState();
        state.logged = false;
        render();
        return;
      }
      if (!response.ok && response.status !== 204) {
        throw new Error(`人工验证画面请求失败（HTTP ${response.status}）`);
      }
      const responseRevision = Number(response.headers.get("X-Interactive-Revision") || 0);
      if (response.status === 204) {
        entry.updatedAt = Date.now();
        if (!entry.objectUrl) {
          entry.revision = 0;
          entry.force = true;
          updateBuiltinInteractiveTransportStatus(currentSessionId, "正在获取首帧画面");
        } else {
          entry.revision = Math.max(entry.revision, responseRevision);
          updateBuiltinInteractiveTransportStatus(currentSessionId, "官方页面画面无变化");
        }
        continue;
      }
      const blob = await response.blob();
      if (!blob.size || !["image/jpeg", "image/png"].includes(blob.type)) throw new Error("人工验证画面格式无效");
      const objectUrl = URL.createObjectURL(blob);
      const liveImage = Array.from(document.querySelectorAll("[data-mcp-interactive-frame]"))
        .find(item => item.getAttribute("data-session-id") === currentSessionId);
      if (!liveImage) {
        URL.revokeObjectURL(objectUrl);
        continue;
      }
      const previousUrl = entry.objectUrl;
      const previousRevision = entry.revision;
      entry.revision = Math.max(entry.revision, responseRevision);
      entry.objectUrl = objectUrl;
      entry.updatedAt = Date.now();
      liveImage.addEventListener("load", () => {
        if (entry.objectUrl !== objectUrl) return;
        liveImage.classList.remove("is-loading");
        if (previousUrl && previousUrl !== objectUrl) {
          try { URL.revokeObjectURL(previousUrl); } catch {}
        }
        updateBuiltinInteractiveTransportStatus(currentSessionId, response.headers.get("X-Interactive-Stale") === "1" ? "操作处理中，暂时保留上一帧" : "画面已更新");
      }, {once:true});
      liveImage.addEventListener("error", () => {
        if (entry.objectUrl !== objectUrl) return;
        try { URL.revokeObjectURL(objectUrl); } catch {}
        entry.objectUrl = previousUrl;
        entry.revision = previousRevision;
        entry.updatedAt = Date.now();
        entry.force = !previousUrl;
        liveImage.src = previousUrl || _MCP_INTERACTIVE_FRAME_PLACEHOLDER_SRC;
        liveImage.classList.toggle("is-loading", !previousUrl);
        updateBuiltinInteractiveTransportStatus(currentSessionId, previousUrl ? "新画面无效，已保留上一帧" : "画面解码失败，正在重新获取");
      }, {once:true});
      liveImage.src = objectUrl;
      updateBuiltinInteractiveTransportStatus(currentSessionId, "正在解码官方页面画面");
    }
  } catch (error) {
    if (!(error && error.name === "AbortError")) {
      images.forEach(image => {
        const entry = builtinInteractiveFrameEntry(image.getAttribute("data-session-id") || "");
        if (!entry.objectUrl) {
          entry.revision = 0;
          entry.force = true;
        }
      });
      images.forEach(image => updateBuiltinInteractiveTransportStatus(image.getAttribute("data-session-id") || "", "画面暂时未更新，可继续操作或手动刷新"));
      if (force) alertFlash("err", error?.message || "人工验证画面未更新");
    }
  } finally {
    if (_mcpInteractiveFrameAbort === controller) _mcpInteractiveFrameAbort = null;
    _mcpInteractiveFrameInFlight = false;
  }
}

function startBuiltinInteractiveFramePolling() {
  if (_mcpInteractiveFrameTimer) clearTimeout(_mcpInteractiveFrameTimer);
  const generation = ++_mcpInteractiveFrameGeneration;
  const schedule = delay => {
    _mcpInteractiveFrameTimer = setTimeout(async () => {
      if (generation !== _mcpInteractiveFrameGeneration) return;
      _mcpInteractiveFrameTimer = null;
      await refreshVisibleBuiltinInteractiveFrames();
      const stillActive = (state.view === "mcp" && state.mcpTab === "builtin") || (state.view === "config" && state.activeGroup === "视频理解");
      if (generation === _mcpInteractiveFrameGeneration && stillActive) {
        schedule(_mcpInteractivePointer ? _MCP_INTERACTIVE_FRAME_ACTIVE_INTERVAL_MS : _MCP_INTERACTIVE_FRAME_INTERVAL_MS);
      }
    }, delay);
  };
  schedule(0);
}

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
  if (kind === "device_confirmation") return ["protocol_qr","headless_page_qr"].includes(auth.login_mode)
    ? '<p class="muted">二维码已扫描，请在对应平台官方 App 中确认登录；本页会继续轮询，不需要保持任何浏览器窗口。</p>'
    : '<p class="muted">二维码已扫描，账号头像不是新二维码。请在对应平台官方 App 中确认登录；确认成功后本页会自动保存登录态并关闭接管页面。</p>';
  if (auth.login_mode === "webui_interactive" && auth.interactive_available) return '<p class="muted">官方登录页面正在通过当前管理员会话转交到 WebUI。点击、拖动和输入只发送到该平台的隔离浏览器；系统不会自动识别或破解验证码。</p>';
  if (kind === "robot_verification") return '<p class="muted">官方页面已要求机器人验证，但本机没有可用的普通系统浏览器兜底。请在当前官方窗口手动完成；MCP 不会绕过验证。</p>';
  if (kind === "official_browser_login") return '<p class="muted">已切换到不受 Playwright 控制的普通系统浏览器。请在该窗口完成扫码、验证码或机器人验证，登录成功后关闭该窗口；MCP 会在窗口关闭后检测登录态。</p>';
  if (kind === "manual_login_incomplete") return '<p class="muted">尚未检测到有效登录态。请确认普通浏览器窗口已经完成登录并完全关闭；需要时可重新打开普通浏览器继续。</p>';
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
  const browserRuntime=(state.mcpBuiltin||{}).browser_runtime||{};
  const openContexts=(browserRuntime.open_contexts||[]).map(platform=>MCP_PLATFORM_LABELS[platform]||platform);
  const activeCount=Object.values(browserRuntime.activity||{}).reduce((sum,value)=>sum+Number(value||0),0);
  const evictions=(browserRuntime.diagnostics||[]).filter(item=>item.code==="browser_context_idle_evicted").length;
  return `<section class="card mcp-native-service">
    <div class="mcp-section-heading"><div><span class="eyebrow">BUILTIN SERVICE</span><h2>社交平台游戏梗查证</h2><p>登录态只保存在四个平台各自的浏览器 profile；Agent 只能调用三个固定只读工具。</p></div>
      <div class="mcp-runtime-actions"><span class="mcp-state ${mcpStatusTone(item.process_state)}"><i></i>${escapeHtml(item.process_state || "stopped")}</span><button class="btn ${desired ? "danger" : "primary"}" data-mcp-installation-toggle="${MCP_BUILTIN_ID}" data-mcp-enabled="${desired ? "false" : "true"}">${desired ? "停止服务" : "开启服务"}</button><button class="btn" data-mcp-reload>重载并诊断</button></div></div>
    <div class="mcp-native-tool-grid">${tools.map(tool => `<article><div><strong>${escapeHtml(tool.title || tool.remote_name)}</strong><code>${escapeHtml(tool.remote_name || "")}</code><small>source_kind=mcp_builtin · risk_level=low · side_effect=none</small></div><button class="btn small ${tool.authorized ? "danger" : "primary"}" data-mcp-tool-toggle="${escapeAttr(tool.remote_name || "")}" data-mcp-installation="${MCP_BUILTIN_ID}" data-mcp-enabled="${tool.authorized ? "false" : "true"}" data-mcp-risk="builtin">${tool.authorized ? "撤销授权" : "授权给 Agent"}</button></article>`).join("")}</div>
    <p class="muted">实际可用还要求：服务进程存活、工具已授权注册，并且至少一个已开启平台处于已登录且能力健康状态。</p>
    <p class="muted">浏览器资源：打开 ${escapeHtml(openContexts.join("、")||"无")} · 活动租约 ${activeCount} · 空闲回收 ${evictions} 次 · ${Number(browserRuntime.idle_timeout_seconds||300)} 秒无活动后关闭 context，磁盘登录态保留。</p>
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
    ? auth.login_mode === "webui_interactive"
      ? '<p class="muted">WebUI 人工验证会话正在运行；验证期间该平台的检索会暂停。</p>'
      : auth.login_mode === "manual_browser"
      ? '<p class="muted">普通系统浏览器窗口正在运行；完成登录后请关闭该窗口，本页会自动检测。</p>'
      : '<p class="muted">官方二维码窗口保持开启；扫码、手机确认完成后，本页会自动更新。</p>'
    : "";
  const authPanel = auth ? `<div class="mcp-auth-panel">${qr}<div><strong data-mcp-auth-status>${escapeHtml(mcpChineseState(auth.status))}</strong><code data-mcp-auth-code>${escapeHtml(auth.status || "")}</code><small data-mcp-auth-remaining>${escapeHtml(builtinAuthSessionSummary(auth))}</small>${authHint}${windowHint}${auth.error_code === "interactive_window_unavailable" ? '<p class="muted">当前运行环境无法打开可见浏览器；二维码仍可扫码，但手机确认/滑块需要在有桌面的运行账户下重试。</p>' : ""}</div></div>` : "";
  const interactive = auth && auth.login_mode === "webui_interactive" && auth.interactive_available
    ? renderBuiltinInteractiveAuth(platform, auth)
    : "";
  const authAction = platform === "bilibili"
    ? auth && !["success","cancelled"].includes(auth.status) ? "重新获取无窗口二维码" : "获取无窗口二维码"
    : auth && !["success","cancelled"].includes(auth.status) ? "重新尝试读取官方二维码" : "尝试读取官方二维码";
  const manualAction = auth && auth.login_mode === "manual_browser" ? "重新打开普通浏览器" : "在普通浏览器中登录";
  const interactiveAction = auth && auth.login_mode === "webui_interactive" && auth.interactive_available ? "重新创建 WebUI 接管" : "在 WebUI 中登录（推荐）";
  const danmaku = capabilities.danmaku === false ? "不支持弹幕" : "页面提供时读取弹幕";
  const platformTitle = platform === "xiaoheihe"
    ? renderMcpExternalLink("https://xiaoheihe.cn/app/bbs/home", label)
    : escapeHtml(label);
  return `<article class="card mcp-platform-card" data-platform="${escapeAttr(platform)}">
    <header><div><span class="eyebrow">${escapeHtml(platform)}</span><h3>${platformTitle}</h3></div><div><strong class="mcp-native-state ${mcpStatusTone(runtimeState)}">${escapeHtml(mcpChineseState(runtimeState))}</strong><code>${escapeHtml(runtimeState)}</code></div></header>
    <div class="mcp-capability-line"><span>搜索</span><span>封面</span><span>正文</span><span>评论/回复</span><span>${escapeHtml(danmaku)}</span></div>
    <div class="mcp-platform-actions"><button class="btn ${item.enabled ? "danger" : "primary"}" data-mcp-platform-toggle="${escapeAttr(platform)}" data-enabled="${item.enabled ? "false" : "true"}">${item.enabled ? "关闭平台" : "开启平台"}</button><button class="btn primary" data-mcp-auth-interactive="${escapeAttr(platform)}">${escapeHtml(interactiveAction)}</button><button class="btn" data-mcp-auth-start="${escapeAttr(platform)}">${escapeHtml(authAction)}</button><button class="btn" data-mcp-auth-manual="${escapeAttr(platform)}">${escapeHtml(manualAction)}</button><button class="btn danger" data-mcp-auth-logout="${escapeAttr(platform)}">注销并删除 profile</button></div>
    ${authPanel}
    ${interactive}
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

function renderBuiltinInteractiveAuth(platform, auth) {
  const sessionId = String(auth.session_id || "");
  const viewport = auth.interactive_viewport || {};
  const width = Math.max(320, Number(viewport.width || 1280));
  const height = Math.max(240, Number(viewport.height || 900));
  const frame = builtinInteractiveFrameEntry(sessionId);
  const frameSource = frame.objectUrl || _MCP_INTERACTIVE_FRAME_PLACEHOLDER_SRC;
  return `<section class="mcp-interactive-auth" data-mcp-interactive-session="${escapeAttr(sessionId)}" data-platform="${escapeAttr(platform)}">
    <div class="mcp-interactive-heading"><div><strong>WebUI 人工验证</strong><small data-mcp-interactive-url>当前官方页面：${escapeHtml(auth.interactive_display_url || "正在读取")}</small></div><span class="tag required">仅管理员</span></div>
    <div class="mcp-interactive-screen" role="application" aria-label="${escapeAttr(MCP_PLATFORM_LABELS[platform] || platform)}官方登录页面人工验证画面">
      <img draggable="false" alt="${escapeAttr(MCP_PLATFORM_LABELS[platform] || platform)}官方登录页面" src="${escapeAttr(frameSource)}" class="${frame.objectUrl ? "" : "is-loading"}" data-mcp-interactive-frame data-platform="${escapeAttr(platform)}" data-session-id="${escapeAttr(sessionId)}" data-viewport-width="${width}" data-viewport-height="${height}">
      <span class="mcp-interactive-frame-placeholder">正在读取官方页面画面…</span>
      <span class="mcp-interactive-pointer-marker" data-mcp-interactive-pointer-marker aria-hidden="true"></span>
    </div>
    <div class="mcp-interactive-transport" data-mcp-interactive-status role="status">画面与操作通道准备中</div>
    <div class="mcp-interactive-controls">
      <label>向当前焦点输入<input type="password" maxlength="200" autocomplete="off" spellcheck="false" data-mcp-interactive-text placeholder="先点击官方输入框，再在此输入验证码或账号内容"></label>
      <button class="btn" data-mcp-interactive-type="${escapeAttr(platform)}" data-session-id="${escapeAttr(sessionId)}">发送输入</button>
      ${["Tab","Enter","Backspace","Escape"].map(key => `<button class="btn small" data-mcp-interactive-key="${escapeAttr(key)}" data-platform="${escapeAttr(platform)}" data-session-id="${escapeAttr(sessionId)}">${escapeHtml(key)}</button>`).join("")}
      <button class="btn small" data-mcp-interactive-scroll="-700" data-platform="${escapeAttr(platform)}" data-session-id="${escapeAttr(sessionId)}">向上滚动</button>
      <button class="btn small" data-mcp-interactive-scroll="700" data-platform="${escapeAttr(platform)}" data-session-id="${escapeAttr(sessionId)}">向下滚动</button>
      <button class="btn" data-mcp-interactive-refresh="${escapeAttr(platform)}">刷新画面</button>
      <button class="btn primary" data-mcp-interactive-finish="${escapeAttr(platform)}" data-session-id="${escapeAttr(sessionId)}">验证完成，检查登录态</button>
      <button class="btn danger" data-mcp-interactive-cancel="${escapeAttr(platform)}" data-session-id="${escapeAttr(sessionId)}">取消接管</button>
    </div>
    <p class="muted">按下、移动与松开会实时转发；网络较慢时只合并陈旧中间点，并始终保留最终位置。画面按变化增量刷新，本地圆点只表示操作已经采集，不代表验证已经通过。输入内容不写入审计日志，但会经本机 WebUI 与 MCP 进程发送到官方页面。</p>
  </section>`;
}

function renderBuiltinPlatforms() {
  const platforms = (state.mcpBuiltin || {}).platforms || {};
  return `<section class="mcp-native-platforms"><div class="mcp-section-heading"><div><span class="eyebrow">PLATFORM ISOLATION</span><h2>平台登录与能力</h2><p>每个平台使用独立 persistent context；关闭服务保留登录态，只有显式注销会删除对应 profile。</p></div></div><div class="mcp-platform-grid">${Object.entries(MCP_PLATFORM_LABELS).map(([platform]) => renderBuiltinPlatformCard(platform, platforms[platform] || {platform, enabled:false, revision:0, config:{}})).join("")}</div></section>`;
}

function renderBuiltinInteractiveConfirmation() {
  const platform = String(_mcpPendingInteractiveAuth || "");
  if (!platform) return "";
  const label = MCP_PLATFORM_LABELS[platform] || platform;
  return `<div class="mcp-confirm-backdrop" role="presentation"><section class="mcp-confirm-panel mcp-interactive-confirm-panel" role="dialog" aria-modal="true" aria-labelledby="mcp-interactive-confirm-title" aria-describedby="mcp-interactive-confirm-description">
    <div class="mcp-section-heading"><div><span class="eyebrow">HUMAN VERIFICATION CONFIRMATION</span><h2 id="mcp-interactive-confirm-title">确认接管 ${escapeHtml(label)} 官方登录页面</h2><p id="mcp-interactive-confirm-description">验证期间会暂停该平台检索；只有你在本面板执行的点击、原始拖动轨迹、有限按键、滚动和短文本会被转发到固定官方域名。</p></div><button class="btn small" data-mcp-auth-interactive-cancel>取消</button></div>
    <div class="alert warn">插件不会自动识别或破解验证码，也不会开放任意 URL、JavaScript、登录凭据、文件或剪贴板。页面离开 ${escapeHtml(label)} 官方允许域名时会立即失败并关闭。</div>
    <div class="mcp-confirm-actions"><button class="btn" data-mcp-auth-interactive-cancel>返回</button><button class="btn primary" data-mcp-auth-interactive-confirm="${escapeAttr(platform)}" ${state.mcpBusy ? "disabled" : ""}>确认并启动人工验证</button></div>
  </section></div>`;
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
  const semantic = packet && packet.semantic_validation || {};
  const semanticProcessing = packet && packet.semantic_processing || {};
  const extractionDiagnostics = semanticProcessing.diagnostics || {};
  const delivery = result && result.delivery || {};
  const warnings = (packet && packet.warnings || []).map(item => `<code>${escapeHtml(item)}</code>`).join(" ");
  return `<div class="mcp-preview-diagnostics">
    <div class="mcp-runtime-overview"><span>请求总上限<strong>${Number(aggregation.requested_limit || 0)}</strong></span><span>实际返回<strong>${Number(aggregation.returned_count || (packet.items || []).length || 0)}</strong></span><span>候选总数<strong>${Number(aggregation.candidate_count || 0)}</strong></span><span>独立来源组<strong>${Number(aggregation.source_group_count || 0)}</strong></span></div>
    <div class="mcp-preview-summary">选中平台：${escapeHtml(selected.map(item => MCP_PLATFORM_LABELS[item] || item).join("、") || "无")} · 成功平台：${escapeHtml((aggregation.successful_platforms || []).map(item => MCP_PLATFORM_LABELS[item] || item).join("、") || "无")} · 实际覆盖：${escapeHtml((aggregation.covered_platforms || []).map(item => MCP_PLATFORM_LABELS[item] || item).join("、") || "无")}</div>
    <div class="mcp-preview-summary">coverage_status=<code>${escapeHtml(aggregation.coverage_status || "empty")}</code> · satisfies_request=<code>${String(aggregation.satisfies_request === true)}</code> · cache_hit=<code>${String(packet.cache_hit === true)}</code> · partial=<code>${String(packet.partial === true)}</code></div>
    ${semantic.status ? `<div class="mcp-preview-summary">黑话语义：<code>${escapeHtml(semantic.status)}</code> · claims=<code>${Number(semantic.claim_count || 0)}</code> · 独立来源组=<code>${Number(semantic.supporting_source_group_count || 0)}</code> · 来源渠道=<code>${escapeHtml((semantic.supporting_origins || []).map(item => MCP_PLATFORM_LABELS[item] || item).join("、") || "无")}</code> · semantic_satisfies=<code>${String(semantic.satisfies_request === true)}</code></div><div class="mcp-preview-summary">共识解释：${escapeHtml(semantic.consensus_meaning || "尚未形成")} · gap_codes=${(semantic.gap_codes || []).map(item => `<code>${escapeHtml(item)}</code>`).join(" ") || "无"}</div>` : ""}
    ${semanticProcessing.extraction_elapsed_ms !== undefined ? `<div class="mcp-preview-summary">语义提取：<code>${escapeHtml(semanticProcessing.extraction_status || "unknown")}</code> / <code>${Number(semanticProcessing.extraction_elapsed_ms || 0).toLocaleString()}ms</code> · 学习归一：<code>${escapeHtml(semanticProcessing.learning_status || "unknown")}</code> / <code>${Number(semanticProcessing.learning_elapsed_ms || 0).toLocaleString()}ms</code> · 后台队列：<code>${Number(semanticProcessing.target_learning_queued || 0)}</code> · 目标 claims：<code>${Number(semanticProcessing.target_claim_count || 0)}</code></div>${Object.keys(extractionDiagnostics).length ? `<div class="mcp-preview-summary">证据输入：<code>${Number(extractionDiagnostics.input_item_count || 0)}</code> → 校验后 <code>${Number(extractionDiagnostics.validated_item_count || 0)}</code> → 送模 <code>${Number(extractionDiagnostics.selected_item_count || 0)}</code> · 紧凑包 <code>${Number(extractionDiagnostics.prompt_packet_chars || 0).toLocaleString()} 字</code> / 最多 <code>${Number(extractionDiagnostics.target_claim_limit || 0)} claims</code> · 模型调用：<code>${String(extractionDiagnostics.model_invoked === true)}</code> · 模型 claims：<code>${Number(extractionDiagnostics.model_claim_count || 0)}</code> → 有效 <code>${Number(extractionDiagnostics.validated_claim_count || 0)}</code></div>` : ""}` : ""}
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
    return `<article class="mcp-content-card">${media}<div class="mcp-content-body"><span>${escapeHtml(MCP_PLATFORM_LABELS[item.platform] || item.platform)} · ${escapeHtml(item.content_type || "unknown")} · id=${escapeHtml(item.content_id || "-")} · source_group=${escapeHtml(item.source_group_id || "-")}</span><h4>${escapeHtml(item.title || "无标题")}</h4><p>${escapeHtml(item.caption_or_body || "")}</p>${renderMcpExternalLink(item.canonical_url, item.canonical_url || "打开规范来源")}<small>quality_score=${Number(item.quality_score || 0).toFixed(2)} · marketing_score=${Number(item.marketing_score || 0).toFixed(2)} · images=${Number(item.image_count || imageRefs.length || 0)}${item.filtered_reason ? ` · ${escapeHtml(item.filtered_reason)}` : ""}${item.detail_filtered_reason ? ` · detail_warning=${escapeHtml(item.detail_filtered_reason)}` : ""}${item.detail_status ? ` · detail=${escapeHtml(item.detail_status)}` : ""}</small><ul>${renderPreviewDiscussion(item)}</ul></div></article>`;
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
  if (!state.mcpSensesLoaded) {
    return `<section class="card mcp-learning-center"><div class="mcp-section-heading"><div><span class="eyebrow">SLANG LEARNING CENTER</span><h2>学习中心</h2><p>词条和证据只在需要时加载，避免每次打开平台状态页都预取大列表。</p></div><button class="btn primary" data-mcp-senses-load ${state.mcpBusy ? "disabled" : ""}>加载学习词条</button></div></section>`;
  }
  const statuses = ["verified","understand_only","observed","disputed","stale","rejected","manual_locked"];
  const filter = state.mcpSenseFilter || "";
  const senses = (state.mcpSenses || []).filter(item => !filter || item.status === filter);
  const cards = senses.map(sense => `<article class="mcp-sense-card"><label><input type="checkbox" data-mcp-sense-select="${escapeAttr(sense.sense_id)}" ${(state.mcpSelectedSenseIds || []).includes(sense.sense_id) ? "checked" : ""}><span>选择合并</span></label><button data-mcp-sense-open="${escapeAttr(sense.sense_id)}"><header><strong>${escapeHtml(sense.term || "")}</strong><span>${escapeHtml(mcpChineseState(sense.status))}</span></header><p>${escapeHtml(sense.meaning || "")}</p><small>${escapeHtml((sense.game_context || {}).canonical_name || "通用语境")} · sources=${Number(sense.source_count || 0)} · platforms=${Number(sense.platform_count || 0)} · confidence=${Number(sense.confidence || 0).toFixed(2)}</small></button></article>`).join("");
  const more=state.mcpSensesHasMore?`<div class="row" style="justify-content:center;margin-top:14px"><button class="btn" data-mcp-senses-more ${state.mcpBusy?'disabled':''}>再加载 50 条</button></div>`:"";
  return `<section class="card mcp-learning-center"><div class="mcp-section-heading"><div><span class="eyebrow">SLANG LEARNING CENTER</span><h2>学习中心</h2><p>自动收录不需要前置人工确认，但每次升级、冲突、降级和人工操作都有事件记录。</p></div><button class="btn" data-mcp-sense-merge ${(state.mcpSelectedSenseIds || []).length < 2 ? "disabled" : ""}>合并所选 sense</button></div><nav class="mcp-learning-tabs"><button data-mcp-sense-filter="" class="${!filter ? "active" : ""}">全部</button>${statuses.map(status => `<button data-mcp-sense-filter="${status}" class="${filter === status ? "active" : ""}">${escapeHtml(mcpChineseState(status))}</button>`).join("")}</nav><div class="mcp-sense-grid">${cards || '<p class="muted">当前分类没有 sense。</p>'}</div>${more}</section>${renderSenseDetail()}`;
}

function renderBuiltinMcp() {
  return `${renderBuiltinServiceCard()}${renderBuiltinPlatforms()}${renderBuiltinPreview()}${renderLearningCenter()}${renderBuiltinInteractiveConfirmation()}`;
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
  const builtin = await api("/mcp/builtin/social-research/status", {cache:"no-store"});
  state.mcpBuiltin = builtin;
  if(state.mcpSensesLoaded)await loadMcpSenses({append:false,renderAfter:false});
  if (builtin.installation) replaceMcpInstallation(builtin.installation);
}

async function loadMcpSenses({append=false,renderAfter=true}={}) {
  if(state.mcpBusy&&renderAfter)return;
  const nextLimit=append?Math.min(2000,Number(state.mcpSenseLimit||50)+50):Math.max(50,Number(state.mcpSenseLimit||50));
  const ownsBusy=renderAfter;
  if(ownsBusy){state.mcpBusy=true;render();}
  try{
    const result=await api(`/mcp/builtin/social-research/slang/senses?limit=${nextLimit}`,{cache:"no-store"});
    state.mcpSenses=result.senses||[];
    state.mcpSenseLimit=nextLimit;
    state.mcpSensesHasMore=result.has_more===true;
    state.mcpSensesLoaded=true;
  }catch(error){
    if(renderAfter)alertFlash("err",operationDiagnosticFromError(error,"学习词条读取失败").message||"学习词条读取失败");
  }finally{
    if(ownsBusy)state.mcpBusy=false;
    if(renderAfter)render();
  }
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
  startBuiltinInteractiveFramePolling();
  _mcpAuthTimer = setInterval(async () => {
    if (state.view !== "mcp" || state.mcpTab !== "builtin" || _mcpAuthPollInFlight) return;
    const sessions = Object.entries(state.mcpAuth || {}).filter(([, value]) => value && ["starting","waiting_scan","manual_verification_required","risk_controlled","qr_expired"].includes(value.status));
    if (!sessions.length) return;
    _mcpAuthPollInFlight = true;
    try {
      let shouldRender = false;
      for (const [platform, session] of sessions) {
        try {
          const next = await api(`/mcp/builtin/social-research/auth/${encodeURIComponent(session.session_id)}/status?platform=${encodeURIComponent(platform)}`, {cache:"no-store"});
          state.mcpAuth = {...state.mcpAuth, [platform]:next};
          if (next.status === "success" && session.status !== "success") await refreshBuiltinMcp();
          if (builtinAuthRenderSignature(session) !== builtinAuthRenderSignature(next)) shouldRender = true;
          else updateBuiltinAuthDom(platform, next);
        } catch {}
      }
      const interactiveInputFocused = document.activeElement instanceof Element && document.activeElement.hasAttribute("data-mcp-interactive-text");
      if (shouldRender && !_mcpInteractivePointer && !interactiveInputFocused && !_mcpInteractiveActionDraining) {
        render();
        refreshVisibleBuiltinInteractiveFrames();
      }
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
    const interactiveMode = session.login_mode === "webui_interactive";
    const failed = session.status === "error" || session.status === "risk_controlled";
    alertFlash(failed ? "err" : "ok", browserMode ? "请在普通系统浏览器完成登录，完成后关闭该窗口" : interactiveMode ? session.status === "starting" ? "官方登录页正在后台准备，页面就绪后会自动显示" : "WebUI 人工验证接管已启动" : failed ? "登录会话启动失败，请查看状态说明" : "请确认画面确实是官方二维码后再扫码");
  } catch (error) { alertFlash("err", operationDiagnosticFromError(error, "登录启动失败").message || "登录启动失败"); }
  finally { state.mcpBusy = false; render(); refreshVisibleBuiltinInteractiveFrames(); }
}

async function startBuiltinInteractiveAuth(platform) {
  if (!MCP_PLATFORM_LABELS[platform] || state.mcpBusy) return;
  _mcpPendingInteractiveAuth = platform;
  render();
}

async function confirmBuiltinInteractiveAuth(platform) {
  if (!MCP_PLATFORM_LABELS[platform] || _mcpPendingInteractiveAuth !== platform || state.mcpBusy) return;
  _mcpPendingInteractiveAuth = null;
  await startBuiltinAuth(platform, "webui_interactive");
}

function interactivePoint(image, event, {clamp=false}={}) {
  const rect = image.getBoundingClientRect();
  const width = Math.max(1, Number(image.getAttribute("data-viewport-width") || 1280));
  const height = Math.max(1, Number(image.getAttribute("data-viewport-height") || 900));
  const naturalWidth = Math.max(1, Number(image.naturalWidth || width));
  const naturalHeight = Math.max(1, Number(image.naturalHeight || height));
  const scale = Math.min(Math.max(1, rect.width) / naturalWidth, Math.max(1, rect.height) / naturalHeight);
  const renderedWidth = Math.max(1, naturalWidth * scale);
  const renderedHeight = Math.max(1, naturalHeight * scale);
  const left = rect.left + (rect.width - renderedWidth) / 2;
  const top = rect.top + (rect.height - renderedHeight) / 2;
  if (!clamp && (event.clientX < left || event.clientX > left + renderedWidth || event.clientY < top || event.clientY > top + renderedHeight)) return null;
  const clientX = Math.max(left, Math.min(left + renderedWidth, event.clientX));
  const clientY = Math.max(top, Math.min(top + renderedHeight, event.clientY));
  return {
    x: Math.max(0, Math.min(width, (clientX - left) * width / renderedWidth)),
    y: Math.max(0, Math.min(height, (clientY - top) * height / renderedHeight)),
    localX:clientX - rect.left,
    localY:clientY - rect.top,
  };
}

function interactivePayloadPoint(point) {
  return {x:Number(point.x), y:Number(point.y)};
}

function compactBuiltinInteractivePoints(points, limit=_MCP_INTERACTIVE_POINTER_MOVE_MAX_POINTS) {
  const unique = [];
  for (const point of points || []) {
    if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) continue;
    const previous = unique[unique.length - 1];
    if (!previous || previous.x !== point.x || previous.y !== point.y) unique.push(point);
  }
  if (unique.length <= limit) return unique;
  const compacted = [];
  for (let index = 0; index < limit; index += 1) {
    const sourceIndex = Math.round(index * (unique.length - 1) / Math.max(1, limit - 1));
    const point = unique[sourceIndex];
    if (compacted[compacted.length - 1] !== point) compacted.push(point);
  }
  const latest = unique[unique.length - 1];
  if (compacted[compacted.length - 1] !== latest) compacted[compacted.length - 1] = latest;
  return compacted;
}

function createBuiltinInteractiveGestureId() {
  if (globalThis.crypto?.randomUUID) return `gesture_${globalThis.crypto.randomUUID().replaceAll("-", "")}`;
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  return `gesture_${Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("")}`;
}

function updateBuiltinInteractivePointerMarker(current, point, visible=true) {
  if (!current?.image || !point) return;
  const screen = current.image.closest(".mcp-interactive-screen");
  const marker = screen?.querySelector("[data-mcp-interactive-pointer-marker]");
  if (!marker) return;
  marker.style.left = `${point.localX}px`;
  marker.style.top = `${point.localY}px`;
  marker.classList.toggle("is-visible", Boolean(visible));
}

function hideBuiltinInteractivePointerMarker(current) {
  const marker = current?.image?.closest(".mcp-interactive-screen")?.querySelector("[data-mcp-interactive-pointer-marker]");
  if (marker) marker.classList.remove("is-visible");
}

function builtinInteractivePointerErrorMessage(code) {
  if (code === "interactive_pointer_timeout") return "拖动已超时，请重新按住滑块";
  if (["interactive_pointer_not_active","interactive_pointer_gesture_mismatch","interactive_pointer_sequence_invalid"].includes(code)) return "拖动状态已经变化，请重新按住滑块";
  return code ? "拖动没有同步到官方页面，请重新按住滑块" : "";
}

async function postBuiltinInteractiveAction(platform, sessionId, action, {quiet=false, keepalive=false}={}) {
  const consumerService = platform === "gemini_web" ? "gemini" : platform === "mimo_asr_web" ? "mimo_asr" : "";
  const path = consumerService
    ? `/media/web/${consumerService}/auth/${encodeURIComponent(sessionId)}/input`
    : `/mcp/builtin/social-research/auth/${encodeURIComponent(sessionId)}/input?platform=${encodeURIComponent(platform)}`;
  const headers = {"content-type":"application/json"};
  if (keepalive) {
    return api(path, {
      method:"POST", headers, keepalive:true,
      body:JSON.stringify({action}),
    }).catch(() => null);
  }
  const next = await api(path, {method:"POST", headers, body:JSON.stringify({action})});
  if (platform === "gemini_web") state.geminiWebAuth = next;
  else if (platform === "mimo_asr_web") state.mimoWebAsrAuth = next;
  else state.mcpAuth = {...state.mcpAuth, [platform]:next};
  const entry = builtinInteractiveFrameEntry(sessionId);
  if (!entry.objectUrl) entry.force = true;
  if (!["gemini_web","mimo_asr_web"].includes(platform)) updateBuiltinAuthDom(platform, next);
  const pointerCode = action?.type?.startsWith("pointer_") ? String(next?.interactive_pointer_error_code || "") : "";
  if (pointerCode && next?.action_duplicate !== true) {
    const message = builtinInteractivePointerErrorMessage(pointerCode);
    updateBuiltinInteractiveTransportStatus(sessionId, message);
    if (!quiet) alertFlash("err", message);
  }
  return next;
}

function waitBuiltinInteractivePointer(delay) {
  return new Promise(resolve => setTimeout(resolve, Math.max(0, Number(delay || 0))));
}

async function refreshBuiltinInteractivePointerFrame(current, {immediate=false}={}) {
  const interactiveViewActive = (state.view === "mcp" && state.mcpTab === "builtin") || (state.view === "config" && state.activeGroup === "视频理解");
  if (!current || _mcpInteractivePointer !== current || !interactiveViewActive) return;
  const now = performance.now();
  if (!immediate && now - current.lastFrameRequestedAt < _MCP_INTERACTIVE_FRAME_ACTIVE_INTERVAL_MS) return;
  current.lastFrameRequestedAt = now;
  updateBuiltinInteractiveTransportStatus(current.sessionId, "正在等待最新画面");
  await refreshVisibleBuiltinInteractiveFrames({sessionId:current.sessionId});
}

function appendBuiltinInteractivePointerPoints(current, points) {
  if (!current || current.ending) return;
  current.pendingPoints = compactBuiltinInteractivePoints([
    ...(current.pendingPoints || []),
    ...(points || []),
  ]);
  const latest = current.pendingPoints[current.pendingPoints.length - 1];
  if (latest) {
    current.latestPoint = latest;
    updateBuiltinInteractivePointerMarker(current, latest);
  }
}

function builtinInteractiveEventPoints(current, event) {
  let samples = [event];
  try {
    const coalesced = typeof event.getCoalescedEvents === "function" ? event.getCoalescedEvents() : [];
    if (Array.isArray(coalesced) && coalesced.length) samples = [...coalesced, event];
  } catch {}
  const points = [];
  for (const sample of samples) {
    const point = interactivePoint(current.image, sample, {clamp:true});
    if (point) points.push(point);
  }
  return compactBuiltinInteractivePoints(points);
}

async function sendBuiltinInteractivePointerStep(current, action, {quiet=false}={}) {
  current.requestInFlight = true;
  updateBuiltinInteractiveTransportStatus(current.sessionId);
  try {
    return await postBuiltinInteractiveAction(current.platform, current.sessionId, action, {quiet});
  } catch (error) {
    if (!quiet) alertFlash("err", operationDiagnosticFromError(error, "拖动操作未完成").message || "拖动操作未完成");
    return null;
  } finally {
    current.requestInFlight = false;
  }
}

function finishBuiltinInteractivePointerClient(current, message="已松开，正在刷新最终画面") {
  if (!current) return;
  try {
    if (current.image?.hasPointerCapture?.(current.pointerId)) current.image.releasePointerCapture(current.pointerId);
  } catch {}
  hideBuiltinInteractivePointerMarker(current);
  if (_mcpInteractivePointer === current) _mcpInteractivePointer = null;
  updateBuiltinInteractiveTransportStatus(current.sessionId, message);
}

function drainBuiltinInteractivePointer(current) {
  if (!current || current.draining) return current?.drainPromise || Promise.resolve();
  current.draining = true;
  current.drainPromise = (async () => {
    if (!current.started) {
      const next = await sendBuiltinInteractivePointerStep(current, {
        type:"pointer_start", gesture_id:current.gestureId, seq:0,
        ...interactivePayloadPoint(current.startPoint),
      });
      current.started = true;
      const pointerError = String(next?.interactive_pointer_error_code || "");
      if (!next || (pointerError && next.action_duplicate !== true)) {
        current.cancelRequested = true;
        current.ending = true;
        current.pendingPoints = [];
      }
    }

    while (_mcpInteractivePointer === current) {
      if (current.pendingPoints.length && !current.cancelRequested) {
        const elapsed = performance.now() - current.lastMoveSentAt;
        if (elapsed < _MCP_INTERACTIVE_POINTER_MOVE_INTERVAL_MS) {
          await waitBuiltinInteractivePointer(_MCP_INTERACTIVE_POINTER_MOVE_INTERVAL_MS - elapsed);
        }
        const batch = compactBuiltinInteractivePoints(current.pendingPoints);
        current.pendingPoints = [];
        current.seq += 1;
        current.lastMoveSentAt = performance.now();
        const next = await sendBuiltinInteractivePointerStep(current, {
          type:"pointer_move", gesture_id:current.gestureId, seq:current.seq,
          points:batch.map(interactivePayloadPoint),
        });
        const pointerError = String(next?.interactive_pointer_error_code || "");
        if (!next || (pointerError && next.action_duplicate !== true)) {
          current.cancelRequested = true;
          current.ending = true;
          current.pendingPoints = [];
        }
        await refreshBuiltinInteractivePointerFrame(current);
        continue;
      }
      if (!current.ending) break;
      current.seq += 1;
      const finalPoint = current.endPoint || current.latestPoint || current.startPoint;
      const action = current.cancelRequested
        ? {type:"pointer_cancel", gesture_id:current.gestureId, seq:current.seq}
        : {type:"pointer_end", gesture_id:current.gestureId, seq:current.seq, ...interactivePayloadPoint(finalPoint)};
      await sendBuiltinInteractivePointerStep(current, action, {quiet:current.cancelRequested});
      await refreshBuiltinInteractivePointerFrame(current, {immediate:true});
      finishBuiltinInteractivePointerClient(current);
      break;
    }
  })().finally(() => {
    current.draining = false;
    current.drainPromise = null;
    if (_mcpInteractivePointer === current && (current.pendingPoints.length || current.ending)) {
      Promise.resolve().then(() => drainBuiltinInteractivePointer(current));
    } else if (_mcpInteractivePointer === current) {
      updateBuiltinInteractiveTransportStatus(current.sessionId);
    }
  });
  return current.drainPromise;
}

function beginBuiltinInteractivePointer(image, event, point) {
  const current = {
    image,
    pointerId:event.pointerId,
    platform:image.getAttribute("data-platform") || "",
    sessionId:image.getAttribute("data-session-id") || "",
    gestureId:createBuiltinInteractiveGestureId(),
    seq:0,
    startPoint:point,
    latestPoint:point,
    endPoint:null,
    pendingPoints:[],
    started:false,
    ending:false,
    cancelRequested:false,
    draining:false,
    drainPromise:null,
    requestInFlight:false,
    lastMoveSentAt:performance.now() - _MCP_INTERACTIVE_POINTER_MOVE_INTERVAL_MS,
    lastFrameRequestedAt:performance.now() - _MCP_INTERACTIVE_FRAME_ACTIVE_INTERVAL_MS,
  };
  _mcpInteractivePointer = current;
  updateBuiltinInteractivePointerMarker(current, point);
  try { image.setPointerCapture(event.pointerId); } catch {}
  drainBuiltinInteractivePointer(current);
  return current;
}

function cancelActiveBuiltinInteractivePointer({keepalive=false}={}) {
  const current = _mcpInteractivePointer;
  if (!current) return Promise.resolve();
  current.cancelRequested = true;
  current.ending = true;
  current.pendingPoints = [];
  if (!keepalive) return drainBuiltinInteractivePointer(current);
  current.seq += 1;
  const request = postBuiltinInteractiveAction(current.platform, current.sessionId, {
    type:"pointer_cancel", gesture_id:current.gestureId, seq:current.seq,
  }, {quiet:true, keepalive:true});
  finishBuiltinInteractivePointerClient(current, "拖动已取消");
  return request;
}

function drainBuiltinInteractiveActions() {
  if (_mcpInteractiveActionDraining) return _mcpInteractiveDrainPromise || Promise.resolve();
  _mcpInteractiveActionDraining = true;
  _mcpInteractiveDrainPromise = (async () => {
    while (_mcpInteractiveActionQueue.length) {
      const item = _mcpInteractiveActionQueue.shift();
      updateBuiltinInteractiveTransportStatus(item.sessionId);
      try {
        const next = await postBuiltinInteractiveAction(item.platform, item.sessionId, item.action);
        item.resolve(next);
      } catch (error) {
        alertFlash("err", operationDiagnosticFromError(error, "人工验证操作未完成").message || "人工验证操作未完成");
        item.resolve(null);
      }
    }
  })().finally(() => {
    _mcpInteractiveActionDraining = false;
    _mcpInteractiveDrainPromise = null;
    refreshVisibleBuiltinInteractiveFrames();
  });
  return _mcpInteractiveDrainPromise;
}

function sendBuiltinInteractiveAction(platform, sessionId, action) {
  if (!platform || !sessionId || _mcpInteractiveLifecycleBusy) return Promise.resolve(null);
  const last = _mcpInteractiveActionQueue[_mcpInteractiveActionQueue.length - 1];
  if (action?.type === "scroll" && last?.platform === platform && last?.sessionId === sessionId && last?.action?.type === "scroll") {
    last.action.delta_y = Math.max(-1200, Math.min(1200, Number(last.action.delta_y || 0) + Number(action.delta_y || 0)));
    updateBuiltinInteractiveTransportStatus(sessionId);
    return last.promise;
  }
  if (_mcpInteractiveActionQueue.length >= _MCP_INTERACTIVE_ACTION_QUEUE_MAX) {
    alertFlash("err", "人工验证操作排队过多，请等待当前操作完成");
    return Promise.resolve(null);
  }
  let resolveItem;
  const promise = new Promise(resolve => { resolveItem = resolve; });
  _mcpInteractiveActionQueue.push({platform, sessionId, action, resolve:resolveItem, promise});
  updateBuiltinInteractiveTransportStatus(sessionId);
  drainBuiltinInteractiveActions();
  return promise;
}

async function finishBuiltinInteractiveAuth(platform, sessionId) {
  if (_mcpInteractiveLifecycleBusy) return;
  if (_mcpInteractivePointer?.sessionId === sessionId) await cancelActiveBuiltinInteractivePointer();
  _mcpInteractiveLifecycleBusy = true;
  try {
    await drainBuiltinInteractiveActions();
    const next = await api(`/mcp/builtin/social-research/auth/${encodeURIComponent(sessionId)}/finish?platform=${encodeURIComponent(platform)}`, {method:"POST"});
    state.mcpAuth = {...state.mcpAuth, [platform]:next};
    if (next.status === "success") {
      await refreshBuiltinMcp();
      alertFlash("ok", `${MCP_PLATFORM_LABELS[platform] || platform}登录态已保存`);
    } else {
      alertFlash("err", "尚未检测到有效登录态，请继续在官方页面完成验证");
    }
  } catch (error) {
    alertFlash("err", operationDiagnosticFromError(error, "登录态检查失败").message || "登录态检查失败");
  } finally {
    _mcpInteractiveLifecycleBusy = false;
    render();
  }
}

async function cancelBuiltinInteractiveAuth(platform, sessionId) {
  if (_mcpInteractiveLifecycleBusy) return;
  if (_mcpInteractivePointer?.sessionId === sessionId) await cancelActiveBuiltinInteractivePointer();
  _mcpInteractiveLifecycleBusy = true;
  try {
    await drainBuiltinInteractiveActions();
    const next = await api(`/mcp/builtin/social-research/auth/${encodeURIComponent(sessionId)}/cancel?platform=${encodeURIComponent(platform)}`, {method:"POST"});
    state.mcpAuth = {...state.mcpAuth, [platform]:next};
    alertFlash("ok", `${MCP_PLATFORM_LABELS[platform] || platform}人工验证接管已取消`);
  } catch (error) {
    alertFlash("err", operationDiagnosticFromError(error, "取消人工验证失败").message || "取消人工验证失败");
  } finally {
    _mcpInteractiveLifecycleBusy = false;
    render();
  }
}

function sendBuiltinInteractiveText(button) {
  const platform = button.getAttribute("data-mcp-interactive-type") || "";
  const sessionId = button.getAttribute("data-session-id") || "";
  const panel = button.closest("[data-mcp-interactive-session]");
  const input = panel?.querySelector("[data-mcp-interactive-text]");
  const value = String(input?.value || "");
  if (!value) { alertFlash("err", "请输入要发送到当前官方输入框的内容"); return; }
  if (input) input.value = "";
  sendBuiltinInteractiveAction(platform, sessionId, {type:"type", text:value});
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
    const element = event.target instanceof Element ? event.target.closest("[data-mcp-operation-clear],[data-mcp-search],[data-mcp-load-more],[data-mcp-detail],[data-mcp-detail-close],[data-mcp-install-plan],[data-mcp-install-confirm],[data-mcp-install-cancel],[data-mcp-installation-toggle],[data-mcp-tool-toggle],[data-mcp-delete],[data-mcp-reload],[data-mcp-tab],[data-mcp-platform-toggle],[data-mcp-platform-save],[data-mcp-auth-start],[data-mcp-auth-interactive],[data-mcp-auth-interactive-confirm],[data-mcp-auth-interactive-cancel],[data-mcp-auth-manual],[data-mcp-auth-logout],[data-mcp-interactive-type],[data-mcp-interactive-key],[data-mcp-interactive-scroll],[data-mcp-interactive-refresh],[data-mcp-interactive-finish],[data-mcp-interactive-cancel],[data-mcp-preview-run],[data-mcp-senses-load],[data-mcp-senses-more],[data-mcp-sense-filter],[data-mcp-sense-open],[data-mcp-sense-close],[data-mcp-sense-action],[data-mcp-sense-merge],[data-mcp-sense-split]") : null;
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
    if (element.hasAttribute("data-mcp-auth-interactive")) { startBuiltinInteractiveAuth(element.getAttribute("data-mcp-auth-interactive") || ""); return; }
    if (element.hasAttribute("data-mcp-auth-interactive-confirm")) { confirmBuiltinInteractiveAuth(element.getAttribute("data-mcp-auth-interactive-confirm") || ""); return; }
    if (element.hasAttribute("data-mcp-auth-interactive-cancel")) { _mcpPendingInteractiveAuth = null; render(); return; }
    if (element.hasAttribute("data-mcp-auth-manual")) { startBuiltinAuth(element.getAttribute("data-mcp-auth-manual") || "", "manual_browser"); return; }
    if (element.hasAttribute("data-mcp-auth-logout")) { logoutBuiltinPlatform(element.getAttribute("data-mcp-auth-logout") || ""); return; }
    if (element.hasAttribute("data-mcp-interactive-type")) { sendBuiltinInteractiveText(element); return; }
    if (element.hasAttribute("data-mcp-interactive-key")) { sendBuiltinInteractiveAction(element.getAttribute("data-platform") || "", element.getAttribute("data-session-id") || "", {type:"key", key:element.getAttribute("data-mcp-interactive-key") || ""}); return; }
    if (element.hasAttribute("data-mcp-interactive-scroll")) { sendBuiltinInteractiveAction(element.getAttribute("data-platform") || "", element.getAttribute("data-session-id") || "", {type:"scroll", delta_y:Number(element.getAttribute("data-mcp-interactive-scroll") || 0)}); return; }
    if (element.hasAttribute("data-mcp-interactive-refresh")) {
      const sessionId = element.closest("[data-mcp-interactive-session]")?.getAttribute("data-mcp-interactive-session") || "";
      refreshVisibleBuiltinInteractiveFrames({force:true, sessionId});
      return;
    }
    if (element.hasAttribute("data-mcp-interactive-finish")) { finishBuiltinInteractiveAuth(element.getAttribute("data-mcp-interactive-finish") || "", element.getAttribute("data-session-id") || ""); return; }
    if (element.hasAttribute("data-mcp-interactive-cancel")) { cancelBuiltinInteractiveAuth(element.getAttribute("data-mcp-interactive-cancel") || "", element.getAttribute("data-session-id") || ""); return; }
    if (element.hasAttribute("data-mcp-preview-run")) { runBuiltinPreview(); return; }
    if (element.hasAttribute("data-mcp-senses-load")) { loadMcpSenses(); return; }
    if (element.hasAttribute("data-mcp-senses-more")) { loadMcpSenses({append:true}); return; }
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
    } else if (event.key === "Enter" && event.target instanceof Element && event.target.hasAttribute("data-mcp-interactive-text")) {
      event.preventDefault();
      const button = event.target.closest("[data-mcp-interactive-session]")?.querySelector("[data-mcp-interactive-type]");
      if (button) sendBuiltinInteractiveText(button);
    }
  });
  document.addEventListener("pointerdown", event => {
    const image = event.target instanceof Element ? event.target.closest("[data-mcp-interactive-frame]") : null;
    if (!image || _mcpInteractivePointer || _mcpInteractiveLifecycleBusy || event.button !== 0 || event.isPrimary === false) return;
    event.preventDefault();
    const point = interactivePoint(image, event);
    if (!point) return;
    beginBuiltinInteractivePointer(image, event, point);
  });
  document.addEventListener("pointermove", event => {
    const current = _mcpInteractivePointer;
    if (!current || current.pointerId !== event.pointerId || current.ending) return;
    event.preventDefault();
    appendBuiltinInteractivePointerPoints(current, builtinInteractiveEventPoints(current, event));
    drainBuiltinInteractivePointer(current);
  });
  document.addEventListener("pointerup", event => {
    const current = _mcpInteractivePointer;
    if (!current || current.pointerId !== event.pointerId || current.ending) return;
    event.preventDefault();
    const point = interactivePoint(current.image, event, {clamp:true}) || current.latestPoint;
    appendBuiltinInteractivePointerPoints(current, [point]);
    current.endPoint = point;
    current.ending = true;
    try { current.image.releasePointerCapture(event.pointerId); } catch {}
    drainBuiltinInteractivePointer(current);
  });
  document.addEventListener("pointercancel", event => {
    if (_mcpInteractivePointer?.pointerId !== event.pointerId) return;
    event.preventDefault();
    cancelActiveBuiltinInteractivePointer();
  });
  document.addEventListener("lostpointercapture", event => {
    if (_mcpInteractivePointer?.pointerId === event.pointerId && !_mcpInteractivePointer.ending) {
      cancelActiveBuiltinInteractivePointer();
    }
  });
  document.addEventListener("contextmenu", event => {
    if (event.target instanceof Element && event.target.closest("[data-mcp-interactive-frame]")) event.preventDefault();
  });
  window.addEventListener("pagehide", () => { cancelActiveBuiltinInteractivePointer({keepalive:true}); });
}
