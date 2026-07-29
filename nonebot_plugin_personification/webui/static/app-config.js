function normalizeConfigSearchText(value) {
  return String(value || "").normalize("NFKC").trim().toLowerCase();
}

function compactConfigSearchText(value) {
  return normalizeConfigSearchText(value).replace(/[^\w\u4e00-\u9fff]+/g, "");
}

function configSearchHaystack(entry) {
  if (!entry) return "";
  if (!entry._searchText) {
    const aliases = Array.isArray(entry.aliases) ? entry.aliases.join(" ") : "";
    const searchIndex = Array.isArray(entry.search_index) ? entry.search_index.join(" ") : "";
    entry._searchText = normalizeConfigSearchText([
      entry.key,
      entry.field_name,
      entry.label,
      entry.description,
      entry.group,
      aliases,
      searchIndex,
    ].join(" "));
    entry._searchCompact = compactConfigSearchText(entry._searchText);
    entry._searchParts = entry._searchText.split(/[\s,，;；/|]+/).map(compactConfigSearchText).filter(Boolean);
  }
  return entry._searchText;
}

function configSearchCompactHaystack(entry) {
  configSearchHaystack(entry);
  return entry && entry._searchCompact || "";
}

function configSearchNeedleVariants(token) {
  const raw = normalizeConfigSearchText(token);
  const compact = compactConfigSearchText(raw);
  return Array.from(new Set([raw, compact].filter(Boolean)));
}

function isConfigSubsequence(needle, haystack) {
  if (!needle || !haystack || needle.length < 2) return false;
  let idx = 0;
  for (const ch of haystack) {
    if (ch === needle[idx]) idx += 1;
    if (idx >= needle.length) return true;
  }
  return false;
}

function configEditDistanceWithin(a, b, maxDistance) {
  if (!a || !b) return false;
  if (Math.abs(a.length - b.length) > maxDistance) return false;
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  const curr = new Array(b.length + 1);
  for (let i = 1; i <= a.length; i++) {
    curr[0] = i;
    let rowMin = curr[0];
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost);
      rowMin = Math.min(rowMin, curr[j]);
    }
    if (rowMin > maxDistance) return false;
    for (let j = 0; j <= b.length; j++) prev[j] = curr[j];
  }
  return prev[b.length] <= maxDistance;
}

function configSearchTokenScore(entry, token) {
  const haystack = configSearchHaystack(entry);
  const compactHaystack = configSearchCompactHaystack(entry);
  const variants = configSearchNeedleVariants(token);
  if (!variants.length) return 0;
  for (const variant of variants) {
    if (haystack.includes(variant)) return 120 - Math.min(40, variant.length);
    if (compactHaystack.includes(variant)) return 110 - Math.min(40, variant.length);
  }
  const compactNeedle = variants[variants.length - 1];
  if (isConfigSubsequence(compactNeedle, compactHaystack)) return 56;
  if (compactNeedle.length >= 3) {
    const maxDistance = compactNeedle.length <= 5 ? 1 : 2;
    const parts = entry._searchParts || [];
    for (const part of parts) {
      if (part.length < 2) continue;
      if (configEditDistanceWithin(compactNeedle, part, maxDistance)) return 48;
      if (part.length > compactNeedle.length && configEditDistanceWithin(compactNeedle, part.slice(0, compactNeedle.length), maxDistance)) return 44;
    }
  }
  return -1;
}

function configSearchEntryScore(entry, tokens) {
  let score = 0;
  for (const token of tokens) {
    const tokenScore = configSearchTokenScore(entry, token);
    if (tokenScore < 0) return -1;
    score += tokenScore;
  }
  if (entry && entry.advanced) score -= 1;
  return score;
}

function configRememberDiagnostic(value, fallbackTitle="配置操作未完成") {
  const operation = value && value.diagnostic && typeof value.diagnostic === "object"
    ? value.diagnostic
    : (value instanceof Error ? operationDiagnosticFromError(value, fallbackTitle) : value);
  if (!operation || typeof operation !== "object") return null;
  state.configDiagnostics = [operation, ...(Array.isArray(state.configDiagnostics) ? state.configDiagnostics : [])].slice(0, 8);
  return operation;
}

function configClearDiagnostics() {
  state.configDiagnostics = [];
  render();
}

function configDraft(field) {
  const drafts = state.configDrafts && typeof state.configDrafts === "object" ? state.configDrafts : {};
  return Object.prototype.hasOwnProperty.call(drafts, field) ? drafts[field] : null;
}

function setConfigValueDraft(field, value, kind="value") {
  if (!state.configDrafts || typeof state.configDrafts !== "object") state.configDrafts = {};
  state.configDrafts[field] = {kind, value};
  return state.configDrafts[field];
}

function clearConfigDraft(field) {
  if (state.configDrafts && typeof state.configDrafts === "object") delete state.configDrafts[field];
}

function configDraftValue(entry) {
  const draft = configDraft(entry.field_name);
  return draft && draft.kind !== "api_pool" ? draft.value : entry.current;
}

function updateConfigDraft(field, input) {
  if (!input) return;
  setConfigValueDraft(field, input.value);
  markDirty(input);
}

function renderConfig() {
  const search = normalizeConfigSearchText(state.configSearch || "");
  const searchTokens = search ? search.split(/\s+/).filter(Boolean) : [];
  let items = state.entries;
  let activeGroup = state.activeGroup;
  if (searchTokens.length) {
    items = items
      .map(e => ({ entry: e, score: configSearchEntryScore(e, searchTokens) }))
      .filter(item => item.score >= 0)
      .sort((a, b) => b.score - a.score || String(a.entry.group).localeCompare(String(b.entry.group), "zh-CN") || String(a.entry.label).localeCompare(String(b.entry.label), "zh-CN"))
      .map(item => item.entry);
    activeGroup = null;
  } else if (activeGroup) {
    items = items.filter(e => e.group === activeGroup);
  }
  // advanced 折叠：默认隐藏 advanced=true 字段
  const totalBeforeAdvanced = items.length;
  if (!state.showAdvancedConfig) {
    items = items.filter(e => !e.advanced);
  }
  const hiddenAdvanced = totalBeforeAdvanced - items.length;
  const groupBar = !search ? state.groups.map(g => {
    const groupEntries = state.entries.filter(e => e.group === g);
    const visibleCount = state.showAdvancedConfig ? groupEntries.length : groupEntries.filter(e => !e.advanced).length;
    return `<button class="${g===activeGroup?'active':''}" onclick="pickGroup('${escapeAttr(g)}')">${escapeHtml(g)} <span class="muted" style="font-size:11px">${visibleCount}/${groupEntries.length}</span></button>`;
  }).join("") : "";
  const heading = search ? `搜索结果（${items.length}）` : (activeGroup || '配置');
  const diagnostics = renderOperationHistory(
    Array.isArray(state.configDiagnostics) ? state.configDiagnostics : [],
    {group:`view-${state.view}`},
  );
  const diagnosticCard = diagnostics
    ? `<div class="card"><div class="between"><h2>配置操作诊断</h2><button class="btn small" onclick="configClearDiagnostics()">清空</button></div>${diagnostics}</div>`
    : "";
  return `<div class="toolbar">
      <input id="config-search-input" type="search" placeholder="搜索字段名 / 标签 / 描述…" value="${escapeAttr(state.configSearch)}" oncompositionstart="onConfigSearchCompositionStart(this)" oncompositionend="onConfigSearchCompositionEnd(this)" oninput="onConfigSearchInput(this,event)" style="flex:1;max-width:340px">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
        <input type="checkbox" ${state.showAdvancedConfig?'checked':''} onchange="state.showAdvancedConfig=this.checked;render()" style="width:auto">
        显示高级配置
      </label>
      <button class="btn" onclick="applyRecommended()">应用推荐默认值</button>
    </div>
    <div class="alert" style="margin-bottom:10px">
      插件配置由数据目录下的 <code>env.json</code> 持久化；<code>.env.prod</code> 仅在首次启用时导入插件字段，后续 WebUI 保存不会改写它。<code>SUPERUSERS</code> 等 NoneBot 基础配置仍放在 <code>.env.prod</code>。
    </div>
    ${diagnosticCard}
    ${groupBar ? `<div class="group-bar">${groupBar}</div>` : ''}
    <div class="card">
      <h2>${escapeHtml(heading)} ${hiddenAdvanced ? `<span class="muted" style="font-size:12px;font-weight:normal">（已折叠 ${hiddenAdvanced} 项高级配置）</span>` : ''}</h2>
      ${items.length ? items.map(renderField).join("") : '<p class="muted">无匹配字段</p>'}
    </div>`;
}

async function applyRecommended() {
  if (!confirm("将一组推荐配置写入插件 env.json，覆盖现有插件配置；不会改写 .env.prod。继续？")) return;
  try {
    const result = await api("/config/apply-recommended", { method:"POST", headers:{"content-type":"application/json"}, body: "{}" });
    const operation = configRememberDiagnostic(result, "推荐默认值应用未完成");
    const lines = [`已应用 ${result.applied.length} 项`];
    if (result.skipped.length) lines.push(`跳过 ${result.skipped.length}：` + result.skipped.map(s=>`${s.field_name}（${s.reason}）`).slice(0,3).join("、"));
    alertFlash(operation?.ok === false ? "err" : "ok", operation?.title || lines.join("；"));
    await loadView(); render();
  } catch (e) { const operation = configRememberDiagnostic(e, "推荐默认值应用未完成"); alertFlash("err", operation?.title || "推荐默认值应用未完成"); }
}

function renderField(e) {
  const tags = [];
  if (e.required) tags.push(`<span class="tag required">必填</span>`);
  if (e.secret) tags.push(`<span class="tag secret">敏感</span>`);
  if (e.advanced) tags.push(`<span class="tag">高级</span>`);
  tags.push(`<span class="tag source-${escapeAttr(e.active_source)}">当前来源：${activeSourceLabel(e.active_source)}</span>`);
  const inputHtml = renderInput(e);
  const defaultLine = e.default !== null && e.default !== "" && !e.secret ? `<div class="muted" style="font-size:12px;margin-top:6px">默认值：<code>${escapeHtml(JSON.stringify(e.default))}</code></div>` : '';
  const exampleLine = e.example ? `<div class="muted" style="font-size:12px;margin-top:4px">示例：<code>${escapeHtml(e.example)}</code></div>` : '';
  return `<div class="field" data-field="${escapeAttr(e.field_name)}">
    <div class="field-head"><strong>${escapeHtml(e.label)}</strong><code>${escapeHtml(e.field_name)}</code>${tags.join("")}</div>
    <div class="field-desc">${escapeHtml(e.description)}</div>
    <div class="field-input">${inputHtml}</div>
    ${defaultLine}
    ${exampleLine}
  </div>`;
}

function renderInput(e) {
  const cur = configDraftValue(e);
  if (e.field_name === "personification_api_pools") {
    return renderApiPoolEditor(e);
  }
  if (e.kind === "toggle") {
    const on = cur === true || cur === "true" || cur === 1;
    return `<div class="toggle">
      <button class="${on?'on':''}" onclick="saveField('${escapeAttr(e.field_name)}', true)">开</button>
      <button class="${!on?'on':''}" onclick="saveField('${escapeAttr(e.field_name)}', false)">关</button>
    </div>`;
  }
  if (e.kind === "select") {
    const opts = e.choices.map(c => `<option value="${escapeAttr(c)}" ${cur===c?'selected':''}>${escapeHtml(c)}</option>`).join("");
    return `<select onchange="saveField('${escapeAttr(e.field_name)}', this.value)">${opts}</select>`;
  }
  if (e.kind === "strlist") {
    return renderStrListEditor(e);
  }
  if (e.kind === "json") {
    const text = cur == null ? "" : (typeof cur === "string" ? cur : JSON.stringify(cur, null, 2));
    return `<textarea data-raw="json" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">${escapeHtml(text)}</textarea>
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'json')">保存</button>`;
  }
  if (e.kind === "int") {
    return `<input type="number" step="1" value="${escapeAttr(cur==null?'':cur)}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'int')">保存</button>`;
  }
  if (e.kind === "float") {
    return `<input type="number" step="0.01" value="${escapeAttr(cur==null?'':cur)}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'float')">保存</button>`;
  }
  if (e.kind === "secret") {
    const secretValue = configDraft(e.field_name) ? cur : "";
    return `<input type="password" value="${escapeAttr(secretValue||'')}" placeholder="${e.current ? '已设置（输入新值覆盖）' : '未设置'}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
      <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'secret')">保存</button>`;
  }
  return `<input type="text" value="${escapeAttr(cur==null?'':cur)}" oninput="updateConfigDraft('${escapeAttr(e.field_name)}',this)">
    <button class="btn small primary" onclick="commitTextField('${escapeAttr(e.field_name)}', this, 'text')">保存</button>`;
}

function strListValue(cur) {
  if (Array.isArray(cur)) return cur.map(x => String(x));
  if (typeof cur === "string" && cur.trim()) {
    try { const p = JSON.parse(cur); if (Array.isArray(p)) return p.map(x => String(x)); } catch {}
    return cur.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  }
  return [];
}

function renderStrListEditor(e) {
  const items = strListValue(configDraftValue(e));
  const field = escapeAttr(e.field_name);
  const rows = items.map(v => `<div class="strlist-row" data-strlist-row>
      <input type="text" value="${escapeAttr(v)}" oninput="syncStrListDraft('${field}')">
      <button class="btn small danger" onclick="this.closest('[data-strlist-row]').remove();syncStrListDraft('${field}')">删</button>
    </div>`).join("");
  return `<div class="strlist-editor" data-strlist-field="${field}">
    <div class="strlist-rows">${rows || '<div class="muted" style="font-size:12px">（空）</div>'}</div>
    <div class="row" style="margin-top:6px">
      <button class="btn small" onclick="addStrListRow('${field}')">+ 添加一项</button>
      <button class="btn small primary" onclick="saveStrList('${field}')">保存</button>
    </div>
  </div>`;
}

function addStrListRow(field) {
  const root = document.querySelector(`[data-strlist-field="${CSS.escape(field)}"] .strlist-rows`);
  if (!root) return;
  const empty = root.querySelector(".muted"); if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "strlist-row"; div.setAttribute("data-strlist-row", "");
  div.innerHTML = `<input type="text" value="" oninput="syncStrListDraft('${escapeAttr(field)}')"><button class="btn small danger" onclick="this.closest('[data-strlist-row]').remove();syncStrListDraft('${escapeAttr(field)}')">删</button>`;
  root.appendChild(div);
  syncStrListDraft(field);
  div.querySelector("input").focus();
}

function syncStrListDraft(field) {
  const root = document.querySelector(`[data-strlist-field="${CSS.escape(field)}"]`);
  if (!root) return [];
  const values = Array.from(root.querySelectorAll('[data-strlist-row] input')).map(input => input.value);
  setConfigValueDraft(field, values, "strlist");
  return values;
}

function saveStrList(field) {
  const values = syncStrListDraft(field);
  saveField(field, values.map(value => value.trim()).filter(Boolean), {preserveDraft:true});
}

function normalizeApiPoolValue(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value.trim());
      return Array.isArray(parsed) ? parsed : [];
    } catch { return []; }
  }
  return [];
}

function sanitizeApiProvider(provider) {
  const out = {...(provider || {})};
  delete out._model_options;
  delete out._model_source;
  delete out._model_probe_done;
  return out;
}

function sanitizeApiProviders(providers) {
  return (providers || []).map(p => sanitizeApiProvider(p));
}

function apiPoolDraftState(field) {
  const draft = configDraft(field);
  return draft && draft.kind === "api_pool" ? draft : null;
}

function setApiPoolDraft(field, providers, options={}) {
  if (!state.configDrafts || typeof state.configDrafts !== "object") state.configDrafts = {};
  const previous = apiPoolDraftState(field);
  const cleanProviders = Array.isArray(providers) ? providers.map(provider => ({...(provider || {})})) : [];
  state.configDrafts[field] = {
    kind: "api_pool",
    providers: cleanProviders,
    rawText: options.rawText !== undefined
      ? String(options.rawText)
      : (previous ? previous.rawText : JSON.stringify(sanitizeApiProviders(cleanProviders), null, 2)),
    rawVisible: options.rawVisible !== undefined ? Boolean(options.rawVisible) : Boolean(previous && previous.rawVisible),
  };
  return state.configDrafts[field];
}

const apiProviderModelProbeCache = new Map();

function apiProviderProbeCacheKey(field, index, provider) {
  const parts = [
    field,
    index,
    provider && provider.name,
    provider && provider.api_type,
    provider && provider.api_url,
    provider && provider.auth_path,
    provider && provider.project,
    provider && provider.gemini_auth_mode,
  ];
  return parts.map(item => String(item == null ? "" : item)).join("\u001f");
}

function cacheApiProviderModelProbe(field, index, provider) {
  const key = apiProviderProbeCacheKey(field, index, provider);
  if (provider && Array.isArray(provider._model_options)) {
    apiProviderModelProbeCache.set(key, {
      models: normalizeApiProviderModels(provider._model_options),
      source: String(provider._model_source || ""),
      done: provider._model_probe_done === true,
    });
  }
}

function hydrateApiProviderModelProbe(field, index, provider) {
  const cloned = {...(provider || {})};
  const cached = apiProviderModelProbeCache.get(apiProviderProbeCacheKey(field, index, cloned));
  if (!cached) return cloned;
  cloned._model_options = cached.models;
  cloned._model_source = cached.source;
  cloned._model_probe_done = cached.done;
  return cloned;
}

function defaultApiProvider(index) {
  return {
    name: `provider_${index + 1}`,
    api_type: "openai",
    api_url: "",
    api_key: "",
    model: "",
    auth_path: "",
    project: "",
    proxy: "",
    timeout: 200,
    max_retries: 5,
    priority: index,
    enabled: true,
  };
}

function apiProviderFieldVisible(apiType, field) {
  const type = (apiType || "openai").replaceAll("-", "_");
  if (["openai_codex", "codex", "claude_code", "claude_cli"].includes(type)) {
    return !["api_url", "api_key", "project"].includes(field);
  }
  if (["gemini_cli", "antigravity_cli", "agy", "agy_cli"].includes(type)) {
    return !["api_url", "api_key"].includes(field);
  }
  return !["auth_path", "project"].includes(field);
}

function apiProviderModelId(item) {
  if (typeof item === "string") return item.trim();
  if (!item || typeof item !== "object") return "";
  return String(item.id || item.model || item.name || item.slug || "").trim();
}

function apiProviderModelLabel(item, id) {
  if (typeof item === "string") return id;
  if (!item || typeof item !== "object") return id;
  return String(item.label || item.display_name || item.displayName || item.source || id || "").trim();
}

function normalizeApiProviderModels(items) {
  const rawItems = Array.isArray(items) ? items : [];
  const seen = new Set();
  const models = [];
  rawItems.forEach(item => {
    const id = apiProviderModelId(item);
    if (!id) return;
    const key = id.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    const label = apiProviderModelLabel(item, id);
    models.push({id, label});
  });
  return models;
}

function renderApiProviderModelDatalistOptions(models) {
  return normalizeApiProviderModels(models).map(item =>
    `<option value="${escapeAttr(item.id)}" label="${escapeAttr(item.label || item.id)}"></option>`
  ).join("");
}

function renderApiProviderModelSelectOptions(models, value) {
  return normalizeApiProviderModels(models).map(item => {
    const text = item.label && item.label !== item.id ? `${item.id} · ${item.label}` : item.id;
    return `<option value="${escapeAttr(item.id)}" ${value===item.id?'selected':''}>${escapeHtml(text)}</option>`;
  }).join("");
}

function updateApiProviderModelControls(card, models, source) {
  if (!card) return;
  const field = card.querySelector('[data-provider-field="model"]');
  if (!field) return;
  const input = field.querySelector("[data-provider-model-input]");
  const select = field.querySelector("[data-provider-model-select]");
  const datalistId = input ? input.getAttribute("list") : "";
  const datalist = datalistId ? document.getElementById(datalistId) : null;
  const currentValue = input ? input.value : "";
  const normalized = normalizeApiProviderModels(models);
  if (select) {
    const placeholder = normalized.length ? "选择模型" : "未探测到可选模型";
    select.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>${renderApiProviderModelSelectOptions(normalized, currentValue)}`;
    select.value = normalized.some(item => item.id === currentValue) ? currentValue : "";
  }
  if (datalist) datalist.innerHTML = renderApiProviderModelDatalistOptions(normalized);
  const oldHint = field.querySelector("[data-provider-model-hint]");
  if (oldHint) oldHint.remove();
  const hint = document.createElement("div");
  hint.className = "muted";
  hint.dataset.providerModelHint = "1";
  hint.style.fontSize = "11px";
  const modelSource = source ? `，来源：${source}` : "";
  hint.textContent = normalized.length ? `已探测 ${normalized.length} 个模型${modelSource}，可输入筛选或手填。` : "未探测到可选模型，仍可手动填写模型 ID。";
  field.appendChild(hint);
}

function renderApiPoolEditor(e) {
  const draft = apiPoolDraftState(e.field_name);
  const source = draft ? draft.providers : normalizeApiPoolValue(e.current);
  const providers = source.map((provider, index) =>
    hydrateApiProviderModelProbe(e.field_name, index, provider || {})
  );
  const rawVisible = Boolean(draft && draft.rawVisible);
  const rawText = draft ? draft.rawText : JSON.stringify(sanitizeApiProviders(providers), null, 2);
  const cards = providers.map((provider, index) => renderApiProviderCard(e.field_name, provider || {}, index)).join("");
  return `<div class="api-pool-editor" data-api-pool-field="${escapeAttr(e.field_name)}">
    <div class="api-provider-actions">
      <button class="btn small" onclick="addApiProvider('${escapeAttr(e.field_name)}')">+ 添加 Provider</button>
      <button class="btn small primary" onclick="saveApiPool('${escapeAttr(e.field_name)}')">保存全部</button>
      <button class="btn small" onclick="toggleApiPoolRaw(this)">${rawVisible?'隐藏 JSON':'查看 JSON'}</button>
    </div>
    <div class="api-provider-list">${cards || '<div class="api-pool-empty">暂无 provider，点击“添加 Provider”创建。</div>'}</div>
    <textarea data-api-pool-raw style="display:${rawVisible?'block':'none'};min-height:120px" oninput="syncApiPoolRawDraft(this)">${escapeHtml(rawText)}</textarea>
  </div>`;
}

function renderApiProviderCard(field, provider, index) {
  provider = hydrateApiProviderModelProbe(field, index, provider || {});
  const apiType = provider.api_type || "openai";
  const choices = ["openai", "openai_codex", "gemini", "gemini_cli", "antigravity_cli", "anthropic", "claude_code"];
  const typeOptions = choices.map(c => `<option value="${escapeAttr(c)}" ${apiType===c?'selected':''}>${escapeHtml(c)}</option>`).join("");
  const fieldHtml = (name, label, type = "text", extra = "") => {
    if (!apiProviderFieldVisible(apiType, name)) return "";
    const value = provider[name] == null ? "" : provider[name];
    return `<div class="api-provider-field" data-provider-field="${escapeAttr(name)}">
      <label>${escapeHtml(label)}</label>
      <input type="${escapeAttr(type)}" value="${escapeAttr(value)}" ${extra} oninput="syncApiPoolDraft('${escapeAttr(field)}')">
    </div>`;
  };
  const modelFieldHtml = () => {
    if (!apiProviderFieldVisible(apiType, "model")) return "";
    const value = provider.model == null ? "" : provider.model;
    const options = Array.isArray(provider._model_options) ? provider._model_options : [];
    const listId = `api-provider-models-${field}-${index}`.replace(/[^\w-]/g, "-");
    const selectId = `${listId}-select`;
    const normalizedOptions = normalizeApiProviderModels(options);
    const optionHtml = renderApiProviderModelDatalistOptions(normalizedOptions);
    const probeDone = provider._model_probe_done === true;
    const selectPlaceholder = normalizedOptions.length ? "选择模型" : (probeDone ? "未探测到可选模型" : "先探测模型");
    const selectHtml = `<select id="${escapeAttr(selectId)}" data-provider-model-select onchange="selectApiProviderModel(this)" aria-label="选择模型">
      <option value="">${escapeHtml(selectPlaceholder)}</option>
      ${renderApiProviderModelSelectOptions(normalizedOptions, value)}
    </select>`;
    const modelSource = provider._model_source ? `，来源：${provider._model_source}` : "";
    const sourceHint = normalizedOptions.length
      ? `<div class="muted" data-provider-model-hint style="font-size:11px">已探测 ${normalizedOptions.length} 个模型${escapeHtml(modelSource)}，可输入筛选或手填。</div>`
      : (probeDone ? `<div class="muted" data-provider-model-hint style="font-size:11px">未探测到可选模型，仍可手动填写模型 ID。</div>` : "");
    return `<div class="api-provider-field api-provider-model-field" data-provider-field="model">
      <label>模型</label>
      <div class="api-provider-model-row">
        <input type="text" data-provider-model-input list="${escapeAttr(listId)}" value="${escapeAttr(value)}" placeholder="先探测或手动填写模型 ID" oninput="syncApiProviderModelSelect(this)">
        ${selectHtml}
        <button class="btn small" type="button" onclick="probeApiProviderModels('${escapeAttr(field)}', ${index}, this)">探测模型</button>
      </div>
      <datalist id="${escapeAttr(listId)}">${optionHtml}</datalist>
      ${sourceHint}
    </div>`;
  };
  const geminiAuthFieldHtml = () => {
    if (!["gemini", "gemini_official"].includes(String(apiType).replaceAll("-", "_"))) return "";
    const value = provider.gemini_auth_mode || "auto";
    const options = [
      ["auto", "自动（x-goog 优先，401 尝试 Bearer）"],
      ["x-goog-api-key", "x-goog-api-key"],
      ["bearer", "Authorization Bearer"],
      ["query_legacy", "Query key（旧兼容，不推荐）"],
    ].map(([id, label]) => `<option value="${escapeAttr(id)}" ${value===id?'selected':''}>${escapeHtml(label)}</option>`).join("");
    return `<div class="api-provider-field" data-provider-field="gemini_auth_mode">
      <label>Gemini 认证</label>
      <select onchange="syncApiPoolDraft('${escapeAttr(field)}')">${options}</select>
    </div>`;
  };
  return `<div class="api-provider-card" data-provider-index="${index}" data-provider-secret-ref="${escapeAttr(provider._secret_ref || "")}">
    <div class="api-provider-head">
      <div class="api-provider-title">Provider ${index + 1}</div>
      <button class="btn small danger" onclick="removeApiProvider('${escapeAttr(field)}', ${index})">删除</button>
    </div>
    <div class="api-provider-grid">
      ${fieldHtml("name", "名称")}
      <div class="api-provider-field" data-provider-field="priority">
        <label>优先级</label>
        <input type="number" step="1" value="${escapeAttr(provider.priority ?? index)}" oninput="syncApiPoolDraft('${escapeAttr(field)}')">
      </div>
      <div class="api-provider-field" data-provider-field="api_type">
        <label>类型</label>
        <select onchange="refreshApiPoolEditor('${escapeAttr(field)}')">${typeOptions}</select>
      </div>
      ${fieldHtml("api_url", "API URL")}
      ${fieldHtml("api_key", "API Key", "password")}
      ${geminiAuthFieldHtml()}
      ${modelFieldHtml()}
      ${fieldHtml("auth_path", "Auth Path")}
      ${fieldHtml("project", "Project")}
      ${fieldHtml("proxy", "代理")}
      ${fieldHtml("timeout", "单次超时（秒）", "number", 'min="5" max="600" step="1"')}
      ${fieldHtml("max_retries", "总尝试次数", "number", 'min="1" max="10" step="1" title="包含首次请求；5 表示首次加 4 次重试"')}
      <div class="api-provider-field" data-provider-field="enabled">
        <label>启用</label>
        <select onchange="syncApiPoolDraft('${escapeAttr(field)}')">
          <option value="true" ${provider.enabled !== false ? 'selected' : ''}>是</option>
          <option value="false" ${provider.enabled === false ? 'selected' : ''}>否</option>
        </select>
      </div>
    </div>
  </div>`;
}

function selectApiProviderModel(select) {
  const field = select.closest("[data-provider-field]");
  const input = field ? field.querySelector("[data-provider-model-input]") : null;
  if (!input) return;
  input.value = select.value || "";
  markDirty(input);
  const root = select.closest("[data-api-pool-field]");
  if (root) syncApiPoolDraft(root.dataset.apiPoolField);
}

function syncApiProviderModelSelect(input) {
  markDirty(input);
  const field = input.closest("[data-provider-field]");
  const select = field ? field.querySelector("[data-provider-model-select]") : null;
  if (!select) return;
  const hasOption = Array.from(select.options).some(option => option.value === input.value);
  select.value = hasOption ? input.value : "";
  const root = input.closest("[data-api-pool-field]");
  if (root) syncApiPoolDraft(root.dataset.apiPoolField);
}

function readApiPoolEditor(field) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return [];
  const raw = root.querySelector("[data-api-pool-raw]");
  if (raw && raw.style.display !== "none") {
    try {
      const parsed = JSON.parse(raw.value);
      if (!Array.isArray(parsed)) throw new Error("API Pool JSON 必须是数组");
      return sanitizeApiProviders(parsed);
    } catch {
      throw new Error("API Pool JSON 格式错误");
    }
  }
  return Array.from(root.querySelectorAll(".api-provider-card")).map((card, index) => {
    const provider = defaultApiProvider(index);
    if (card.dataset.providerSecretRef) provider._secret_ref = card.dataset.providerSecretRef;
    card.querySelectorAll("[data-provider-field]").forEach(wrap => {
      const name = wrap.dataset.providerField;
      const input = wrap.querySelector("input, select");
      if (!input) return;
      let value = input.value;
      if (name === "enabled") value = value === "true";
      if (name === "priority" || name === "timeout" || name === "max_retries") value = value === "" ? undefined : parseInt(value, 10);
      if (value !== "" && value !== undefined) provider[name] = value;
      else delete provider[name];
    });
    return hydrateApiProviderModelProbe(field, index, provider);
  });
}

function syncApiPoolDraft(field) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return [];
  const raw = root.querySelector("[data-api-pool-raw]");
  const rawVisible = Boolean(raw && raw.style.display !== "none");
  if (rawVisible) {
    const rawText = raw.value;
    let providers = apiPoolDraftState(field)?.providers || [];
    try {
      const parsed = JSON.parse(rawText);
      if (Array.isArray(parsed)) providers = parsed;
    } catch {}
    setApiPoolDraft(field, providers, {rawText, rawVisible:true});
    return providers;
  }
  const providers = readApiPoolEditor(field);
  setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
  return providers;
}

function syncApiPoolRawDraft(raw) {
  const root = raw ? raw.closest("[data-api-pool-field]") : null;
  if (!root) return;
  syncApiPoolDraft(root.dataset.apiPoolField);
  markDirty(raw);
}

function writeApiPoolEditor(field, providers) {
  const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
  if (!root) return;
  const list = root.querySelector(".api-provider-list");
  list.innerHTML = providers.map((provider, index) => renderApiProviderCard(field, provider, index)).join("") || '<div class="api-pool-empty">暂无 provider，点击“添加 Provider”创建。</div>';
  const raw = root.querySelector("[data-api-pool-raw]");
  const draft = apiPoolDraftState(field);
  if (raw) {
    raw.value = draft ? draft.rawText : JSON.stringify(sanitizeApiProviders(providers), null, 2);
    raw.style.display = draft && draft.rawVisible ? "block" : "none";
  }
  const toggle = root.querySelector(".api-provider-actions .btn:last-child");
  if (toggle) toggle.textContent = draft && draft.rawVisible ? "隐藏 JSON" : "查看 JSON";
}

function refreshApiPoolEditor(field) {
  try {
    const providers = readApiPoolEditor(field);
    setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function addApiProvider(field) {
  try {
    const providers = readApiPoolEditor(field);
    providers.push(defaultApiProvider(providers.length));
    setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function removeApiProvider(field, index) {
  try {
    const providers = readApiPoolEditor(field);
    providers.splice(index, 1);
    setApiPoolDraft(field, providers, {rawText:JSON.stringify(sanitizeApiProviders(providers), null, 2), rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) { alertFlash("err", e.message); }
}

function toggleApiPoolRaw(btn) {
  const root = btn.closest(".api-pool-editor");
  const raw = root.querySelector("[data-api-pool-raw]");
  const showing = raw.style.display !== "none";
  const field = root.dataset.apiPoolField;
  if (!showing) {
    const providers = readApiPoolEditor(field);
    const rawText = JSON.stringify(sanitizeApiProviders(providers), null, 2);
    setApiPoolDraft(field, providers, {rawText, rawVisible:true});
    raw.value = rawText;
    raw.style.display = "block";
    btn.textContent = "隐藏 JSON";
    return;
  }
  try {
    const parsed = JSON.parse(raw.value);
    if (!Array.isArray(parsed)) throw new Error("API Pool JSON 必须是数组");
    const providers = sanitizeApiProviders(parsed);
    setApiPoolDraft(field, providers, {rawText:raw.value, rawVisible:false});
    writeApiPoolEditor(field, providers);
  } catch (e) {
    setApiPoolDraft(field, apiPoolDraftState(field)?.providers || [], {rawText:raw.value, rawVisible:true});
    alertFlash("err", e.message || "API Pool JSON 格式错误");
  }
}

async function saveApiPool(field) {
  try {
    const providers = readApiPoolEditor(field);
    const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
    const raw = root ? root.querySelector("[data-api-pool-raw]") : null;
    setApiPoolDraft(field, providers, {
      rawText:raw ? raw.value : JSON.stringify(sanitizeApiProviders(providers), null, 2),
      rawVisible:Boolean(raw && raw.style.display !== "none"),
    });
    await saveField(field, sanitizeApiProviders(providers), {preserveDraft:true});
  } catch (e) { const operation = configRememberDiagnostic(e, "API Pool 保存未完成"); alertFlash("err", operation?.title || "API Pool 保存未完成"); }
}

async function probeApiProviderModels(field, index, btn) {
  let providers;
  try {
    providers = readApiPoolEditor(field);
    const root = document.querySelector(`[data-api-pool-field="${CSS.escape(field)}"]`);
    const raw = root ? root.querySelector("[data-api-pool-raw]") : null;
    setApiPoolDraft(field, providers, {
      rawText:raw ? raw.value : JSON.stringify(sanitizeApiProviders(providers), null, 2),
      rawVisible:Boolean(raw && raw.style.display !== "none"),
    });
  } catch (e) {
    const operation = configRememberDiagnostic(e, "Provider 模型探测参数无效");
    alertFlash("err", operation?.title || "Provider 模型探测参数无效");
    return;
  }
  const provider = sanitizeApiProvider(providers[index]);
  if (!provider) return;
  const requestIdentity = apiProviderProbeCacheKey(field, index, provider);
  const oldText = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "探测中…"; }
  try {
    const result = await api("/config/provider-models", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({provider}),
    });
    const operation = configRememberDiagnostic(result, "Provider 模型探测未完成");
    const models = normalizeApiProviderModels(result.models);
    const probedProvider = {...provider, _model_options: models, _model_source: result.source || "", _model_probe_done: true};
    cacheApiProviderModelProbe(field, index, probedProvider);
    let latestProviders;
    try {
      latestProviders = readApiPoolEditor(field);
    } catch {
      alertFlash("info", "探测已完成；当前草稿仍在编辑，结果将在 Provider 参数恢复匹配时可用");
      return;
    }
    const latestProvider = sanitizeApiProvider(latestProviders[index]);
    if (!latestProvider || apiProviderProbeCacheKey(field, index, latestProvider) !== requestIdentity) {
      alertFlash("info", "探测已完成；Provider 参数已变化，未覆盖当前草稿");
      return;
    }
    latestProviders[index] = {...latestProviders[index], _model_options: models, _model_source: result.source || "", _model_probe_done: true};
    cacheApiProviderModelProbe(field, index, latestProviders[index]);
    const previousDraft = apiPoolDraftState(field);
    setApiPoolDraft(field, latestProviders, {
      rawText:JSON.stringify(sanitizeApiProviders(latestProviders), null, 2),
      rawVisible:Boolean(previousDraft && previousDraft.rawVisible),
    });
    writeApiPoolEditor(field, latestProviders);
    alertFlash(models.length ? "ok" : "err", operation?.title || (models.length ? `已探测 ${models.length} 个模型` : "未探测到模型，请手动填写"));
  } catch (e) {
    const operation = configRememberDiagnostic(e, "Provider 模型探测未完成");
    alertFlash("err", operation?.title || "Provider 模型探测未完成");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = oldText || "探测模型"; }
  }
}

function activeSourceLabel(src) {
  return ({env_file:".env.prod 首次导入",env_json:"env.json",runtime_config:"runtime_config.json",default:"默认"})[src] || src;
}

function markDirty(el) { el.dataset.dirty = "1"; }

async function commitTextField(field, btn, kind) {
  const wrap = btn.parentElement;
  const input = wrap.querySelector("input, textarea");
  if (!input) return;
  let raw = input.value;
  let value = raw;
  if (kind === "int") value = parseInt(raw, 10);
  else if (kind === "float") value = parseFloat(raw);
  await saveField(field, value, {preserveDraft:true});
}

async function saveField(field, value, options={}) {
  if (!options.preserveDraft) setConfigValueDraft(field, value);
  const submittedDraft = configDraft(field);
  try {
    const result = await api("/config/value", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({ field_name: field, value }) });
    const operation = configRememberDiagnostic(result, "配置保存未完成");
    if (result.success) {
      const entry = state.entries.find(item => item.field_name === field);
      if (entry && Object.prototype.hasOwnProperty.call(result, "new_value")) entry.current = result.new_value;
      if (configDraft(field) === submittedDraft) clearConfigDraft(field);
      alertFlash("ok", operation?.title || `已保存 ${field} 到插件 env.json`);
      await loadView(); render();
    }
    else { alertFlash("err", operation?.title || "配置保存仅部分完成"); await loadView(); render(); }
  } catch (e) { const operation = configRememberDiagnostic(e, "配置保存未完成"); alertFlash("err", operation?.title || "配置保存未完成"); }
}

function pickGroup(g) { state.activeGroup = g; render(); }
