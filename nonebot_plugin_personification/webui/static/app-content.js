function renderStickers() {
  const data = state.stickers;
  if (!data) return `<div class="card muted">加载中…</div>`;
  const items = data.stickers || [];
  const search = (state.stickerSearch || "").trim().toLowerCase();
  const filtered = search
    ? items.filter(s => s.filename.toLowerCase().includes(search)
        || (s.description||"").toLowerCase().includes(search)
        || (s.mood_tags||[]).join(",").includes(search)
        || (s.scene_tags||[]).join(",").includes(search))
    : items;
  const grid = filtered.map(s => {
    const tags = [...(s.mood_tags||[]), ...(s.scene_tags||[])].slice(0, 5).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join("");
    const labelTag = s.labeled
      ? '<span class="tag" style="background:rgba(52,211,153,0.18);color:var(--ok)">已标</span>'
      : '<span class="tag" style="background:rgba(245,158,11,0.18);color:var(--warn)">待标</span>';
    return `<div class="sticker-card" onclick="openStickerEdit('${escapeAttr(s.filename)}')">
      <button class="sticker-delete-btn" title="移到回收" onclick="event.stopPropagation();deleteStickerByName('${escapeAttr(s.filename)}')">×</button>
      <img src="${escapeAttr(s.thumbnail_url)}" loading="lazy" alt="${escapeAttr(s.filename)}">
      <div class="sticker-meta">
        <div class="sticker-name" title="${escapeAttr(s.filename)}">${escapeHtml(s.filename)}</div>
        <div class="sticker-desc">${escapeHtml((s.description||'').slice(0,40))}</div>
        <div>${labelTag} ${tags}</div>
      </div>
    </div>`;
  }).join("");
  return `<div class="toolbar">
      <input id="sticker-search-input" type="search" placeholder="按文件名/描述/标签搜索…" value="${escapeAttr(state.stickerSearch)}" oninput="state.stickerSearch=this.value;render()" style="flex:1;max-width:340px">
      <span class="muted">共 ${data.total} 张，已标 ${data.labeled_count}</span>
      <button class="btn" onclick="document.getElementById('sticker-upload-input').click()">上传</button>
      <input id="sticker-upload-input" type="file" accept="image/jpeg,image/png,image/webp,image/gif" style="display:none" onchange="uploadStickerFromInput(this)">
      <button class="btn" onclick="rescanStickers('missing_only')">扫描未打标</button>
      <button class="btn" onclick="rescanStickers('force_all')" style="color:var(--warn)">全部重打标</button>
    </div>
    <p class="muted" style="font-size:12px;margin:0 0 12px">表情包目录：<code>${escapeHtml(data.sticker_dir)}</code>。删除会移到 trash/YYYYMMDD/ 子目录，可手动恢复。</p>
    <div class="sticker-grid">${grid || '<p class="muted">暂无表情包</p>'}</div>
    ${state.selectedSticker ? renderStickerEdit() : ''}`;
}

async function uploadStickerFromInput(input) {
  if (!input.files || !input.files.length) return;
  const file = input.files[0];
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch(API + "/stickers/upload", { method: "POST", credentials: "include", body: form });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    const out = await res.json();
    alertFlash("ok", `上传成功：${out.filename}${out.needs_labeling?'（待打标）':''}`);
    await loadView(); render();
  } catch (e) { alertFlash("err", "上传失败：" + e.message); }
  input.value = "";
}

async function rescanStickers(mode) {
  const label = mode === "force_all" ? "全部重打标" : "扫描未打标";
  if (!confirm(`${label}：将清空对应表情包的标签元数据，等待下次启动或后台 labeler 扫描时重打。继续？`)) return;
  try {
    const out = await api("/stickers/rescan", { method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({mode}) });
    alertFlash("ok", `${label}：已清空 ${out.scheduled} 个条目`);
    await loadView(); render();
  } catch (e) { alertFlash("err", "操作失败：" + e.message); }
}

function openStickerEdit(name) {
  const item = (state.stickers?.stickers || []).find(x => x.filename === name);
  if (!item) return;
  state.selectedSticker = JSON.parse(JSON.stringify(item));
  render();
}

function renderStickerEdit() {
  const s = state.selectedSticker;
  return `<div class="card" style="margin-top:14px">
    <div class="between"><h2 style="margin:0">编辑 ${escapeHtml(s.filename)}</h2>
      <button class="btn small" onclick="state.selectedSticker=null;render()">关闭</button></div>
    <div style="display:flex;gap:20px;margin-top:14px;flex-wrap:wrap">
      <img src="${escapeAttr(s.thumbnail_url)}" style="max-width:200px;max-height:200px;border-radius:6px;object-fit:contain;background:#0b0d12">
      <div style="flex:1;min-width:280px">
        <label class="muted">描述</label>
        <textarea oninput="state.selectedSticker.description=this.value" style="width:100%;min-height:50px;margin:4px 0 10px">${escapeHtml(s.description)}</textarea>
        <label class="muted">心情标签（逗号分隔）</label>
        <input type="text" value="${escapeAttr((s.mood_tags||[]).join(','))}" oninput="state.selectedSticker.mood_tags=this.value.split(',').map(x=>x.trim()).filter(Boolean)" style="width:100%;margin:4px 0 10px">
        <label class="muted">场景标签（逗号分隔）</label>
        <input type="text" value="${escapeAttr((s.scene_tags||[]).join(','))}" oninput="state.selectedSticker.scene_tags=this.value.split(',').map(x=>x.trim()).filter(Boolean)" style="width:100%;margin:4px 0 10px">
        <label class="muted">使用建议</label>
        <input type="text" value="${escapeAttr(s.use_hint||'')}" oninput="state.selectedSticker.use_hint=this.value" style="width:100%;margin:4px 0 10px">
        <label class="muted">避免使用</label>
        <input type="text" value="${escapeAttr(s.avoid_hint||'')}" oninput="state.selectedSticker.avoid_hint=this.value" style="width:100%;margin:4px 0 10px">
        <label class="muted" style="display:flex;align-items:center;gap:6px"><input type="checkbox" ${s.proactive_send?'checked':''} onchange="state.selectedSticker.proactive_send=this.checked" style="width:auto">允许在主动场景发送</label>
        <label class="muted" style="display:block;margin-top:10px">权重（0-3）</label>
        <input type="number" step="0.1" min="0" max="3" value="${s.weight}" oninput="state.selectedSticker.weight=parseFloat(this.value)" style="width:100px">
        <div class="row" style="margin-top:14px">
          <button class="btn primary" onclick="saveSticker()">保存</button>
          <button class="btn danger" onclick="deleteSticker()">移到回收</button>
        </div>
      </div>
    </div>
  </div>`;
}

async function saveSticker() {
  const s = state.selectedSticker;
  try {
    await api("/stickers/" + encodeURIComponent(s.filename), {
      method:"PATCH",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({
        description: s.description, mood_tags: s.mood_tags, scene_tags: s.scene_tags,
        proactive_send: s.proactive_send, use_hint: s.use_hint, avoid_hint: s.avoid_hint,
        weight: s.weight,
      }),
    });
    alertFlash("ok", "已保存");
    state.selectedSticker = null;
    await loadView(); render();
  } catch (e) { alertFlash("err", "保存失败：" + e.message); }
}

async function deleteSticker() {
  const s = state.selectedSticker;
  if (!s) return;
  await deleteStickerByName(s.filename);
  state.selectedSticker = null;
}

async function deleteStickerByName(name) {
  if (!confirm(`将 ${name} 移到 trash 目录？可手动恢复。`)) return;
  try {
    await api("/stickers/" + encodeURIComponent(name), { method:"DELETE" });
    alertFlash("ok", `已移到回收：${name}`);
    if (state.selectedSticker && state.selectedSticker.filename === name) {
      state.selectedSticker = null;
    }
    await loadView(); render();
  } catch (e) { alertFlash("err", "删除失败：" + e.message); }
}

function formatInnerPendingThoughts(value) {
  if (!value || (Array.isArray(value) && !value.length)) return "-";
  const items = Array.isArray(value) ? value : [value];
  const texts = items.map(item => {
    if (item == null) return "";
    if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") {
      return String(item).trim();
    }
    if (typeof item === "object") {
      const primary = item.thought || item.text || item.summary || item.content || item.title;
      if (primary) return String(primary).trim();
      try { return JSON.stringify(item); } catch (_e) { return ""; }
    }
    return String(item || "").trim();
  }).filter(Boolean);
  return texts.slice(-3).join(" / ") || "-";
}

const MEMORY_TYPE_LABELS = {
  semantic: "长期语义", fact: "事实记忆", group_knowledge: "群知识", group_meme: "群梗词典",
  concept_anchor: "概念锚点", user_persona: "用户画像", persona_knowledge: "人设知识",
  episodic: "事件片段", episodic_turn: "对话回合", conflict_memory: "冲突记忆",
};
const MEMORY_TIER_LABELS = { working: "工作记忆", short: "短期记忆", long: "长期记忆", core: "核心记忆", archive: "归档记忆" };
const MEMORY_KIND_LABELS = { memory: "记忆条目", entity: "实体/标签", user: "群成员" };
const MEMORY_RELATION_LABELS = {
  tag: "标签关联", related: "相关", similar: "相似", supports: "支持", contradicts: "冲突",
  same_topic: "同一话题", same_user: "同一用户", reply: "回复关系", mention: "提及",
  co_occurs: "共同出现", talks_to: "对话", reacts_to: "回应", quotes: "引用",
};

function memoryTypeLabel(value, fallback) {
  if (value && typeof value === "object") return value.memory_type_label || MEMORY_TYPE_LABELS[value.memory_type] || fallback || "其他记忆";
  return MEMORY_TYPE_LABELS[value] || fallback || "其他记忆";
}

function memoryTierLabel(value, fallback) {
  if (value && typeof value === "object") return value.tier_label || MEMORY_TIER_LABELS[value.tier] || fallback || "未分层";
  return MEMORY_TIER_LABELS[value] || fallback || "未分层";
}

function memoryRelationLabel(value, fallback) {
  if (value && typeof value === "object") return value.kind_label || MEMORY_RELATION_LABELS[value.kind] || fallback || "其他关联";
  return MEMORY_RELATION_LABELS[value] || fallback || "其他关联";
}

function memoryNodeKindLabel(value, fallback) {
  if (value && typeof value === "object") return value.kind_label || MEMORY_KIND_LABELS[value.kind] || fallback || "节点";
  return MEMORY_KIND_LABELS[value] || fallback || "节点";
}

function renderMemory() {
  const mem = state.memory;
  const inner = state.memoryInnerState;
  if (state.selectedMemory) return renderMemoryDetail();
  if (!mem) return `<div class="card muted">加载中…</div>`;
  if (!mem.palace_enabled) {
    return `<div class="card"><h2>Agent 记忆</h2>
      <p class="muted">memory palace 未启用。要查看长期记忆，需在配置中开启 <code>personification_memory_palace_enabled</code>。</p></div>`;
  }
  const filters = ["", "group_knowledge", "user_persona", "fact"].map(t =>
    `<button class="${state.memoryFilter===t?'active':''}" onclick="pickMemoryFilter('${t}')">${t ? memoryTypeLabel(t) : '全部类型'}</button>`
  ).join("");
  const zoneOptions = ['<option value="">全部分区</option>'].concat(
    (state.memoryPalaceZones || []).map(z => `<option value="${escapeAttr(z)}" ${state.memoryPalaceZone===z?'selected':''}>${escapeHtml(z)}</option>`)
  ).join("");
  const rows = (mem.items || []).map(it => `<tr>
    <td><span class="tag">${escapeHtml(memoryTypeLabel(it))}</span>${it.tier ? `<br><span class="muted" style="font-size:11px">${escapeHtml(memoryTierLabel(it))}</span>` : ''}</td>
    <td><code style="font-size:11px">${escapeHtml(it.group_id||'')}${it.user_id ? '/'+escapeHtml(it.user_id) : ''}</code></td>
    <td>${escapeHtml(it.summary)}</td>
    <td class="muted" style="font-size:12px">置信 ${it.confidence.toFixed(2)}<br>显著 ${it.salience.toFixed(2)}</td>
    <td class="muted" style="font-size:12px">${it.updated_at?new Date(it.updated_at*1000).toLocaleString():'-'}</td>
    <td><button class="btn small" onclick="openMemoryDetail('${escapeAttr(it.memory_id)}')">详情</button></td>
  </tr>`).join("");
  const hiddenNote = mem.hidden_self_count
    ? `<span class="muted" style="font-size:12px;margin-left:10px">已默认隐藏 ${mem.hidden_self_count} 条 bot 自言条目</span>`
    : '';
  let innerBlock = '';
  if (inner && inner.available) {
    const s = inner.state || {};
    const warm = s.relation_warmth || {};
    const warmRows = Object.keys(warm).slice(0,12).map(k => `<tr><td><code>${escapeHtml(k)}</code></td><td>${typeof warm[k]==='number'?warm[k].toFixed(2):escapeHtml(String(warm[k]))}</td></tr>`).join("");
    innerBlock = `<div class="card"><h2>Inner State（情绪/能量/关系）</h2>
      <div class="row" style="gap:30px;flex-wrap:wrap">
        <div><div class="muted">mood</div><div style="font-size:18px;margin-top:4px">${escapeHtml(String(s.mood||'-'))}</div></div>
        <div><div class="muted">energy</div><div style="font-size:18px;margin-top:4px">${escapeHtml(String(s.energy||'-'))}</div></div>
        <div><div class="muted">pending</div><div style="font-size:13px;margin-top:4px">${escapeHtml(formatInnerPendingThoughts(s.pending_thoughts)).slice(0,120)||'-'}</div></div>
      </div>
      ${warmRows ? `<h3 style="margin-top:14px;margin-bottom:6px;font-size:13px">用户好感度</h3><table style="max-width:420px"><thead><tr><th>用户</th><th>好感</th></tr></thead><tbody>${warmRows}</tbody></table>`:''}</div>`;
  }
  const vectorPanel = renderMemoryVectorPanel();
  return `${innerBlock}
    ${vectorPanel}
    <div class="toolbar">
      <div class="group-bar" style="margin-bottom:0">${filters}</div>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
        <input type="checkbox" ${state.memoryIncludeSelf?'checked':''} onchange="toggleMemoryIncludeSelf(this.checked)" style="width:auto">
        包含 bot 自己的发言
      </label>
      ${hiddenNote}
    </div>
    <div class="card">
      <div class="row" style="gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <input id="mem-user-input" placeholder="按 user_id 过滤" value="${escapeAttr(state.memoryUserId || '')}" onkeydown="if(event.key==='Enter')applyMemoryFilters()" style="width:160px">
        <input id="mem-group-input" placeholder="按 group_id 过滤" value="${escapeAttr(state.memoryGroupId || '')}" onkeydown="if(event.key==='Enter')applyMemoryFilters()" style="width:160px">
        <select id="mem-zone-select" onchange="pickPalaceZone(this.value)">${zoneOptions}</select>
        <select id="mem-limit-select" onchange="pickMemoryLimit(this.value)">
          ${[100,200,500].map(n => `<option value="${n}" ${Number(state.memoryLimit||200)===n?'selected':''}>显示 ${n} 条</option>`).join('')}
        </select>
        <button class="btn" onclick="applyMemoryFilters()">应用</button>
        ${(state.memoryUserId || state.memoryGroupId || state.memoryPalaceZone) ? '<button class="btn small" onclick="clearMemoryFilters()">清除过滤</button>' : ''}
      </div>
      <h2>长期记忆（${(mem.items||[]).length}）</h2>
      <p class="muted" style="font-size:12px;margin:-6px 0 10px">
        从 memory_palace 数据库蒸馏后的记忆条目（用户画像、群知识、事实等）；
        ${state.memoryIncludeSelf ? '当前显示 bot 自言条目。' : 'bot 自己的发言默认隐藏，勾选上方复选框可显示。'}
        要看群里的原始对话历史，请进入「群信息」→ 选择群 → 切「对话原文」tab。
      </p>
      <table><thead><tr><th>类型</th><th>作用域</th><th>摘要</th><th>分数</th><th>更新</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="muted">暂无记忆条目</td></tr>'}</tbody></table>
    </div>`;
}

function renderMemoryVectorPanel() {
  const idx = state.memoryVectorIndex || {};
  const ok = idx.available !== false;
  const statusTag = ok
    ? `<span class="tag ${idx.enabled ? 'source-env_json' : ''}">${idx.enabled ? '已启用' : '未启用'}</span>`
    : `<span class="tag required">不可用</span>`;
  const searchRows = ((state.memorySearchResult || {}).items || []).map(it => `<tr>
    <td><code style="font-size:11px">${escapeHtml(it.memory_id || '')}</code><br><span class="tag">${escapeHtml(it.search_source_label || '检索结果')}</span></td>
    <td>${escapeHtml(it.summary || '')}</td>
    <td class="muted" style="font-size:12px">${escapeHtml(it.why_relevant || '')}</td>
    <td>${Number(it.score || 0).toFixed(3)}</td>
  </tr>`).join("");
  return `<div class="card">
    <div class="row" style="justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">RAG 索引</h2>
      <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
        ${statusTag}
        <span class="muted" style="font-size:12px">backend=${escapeHtml(idx.backend || '-')} model=${escapeHtml(idx.model_version || '-')}</span>
        <button class="btn small" onclick="rebuildMemoryVectorIndex()" ${state.memoryVectorBusy?'disabled':''}>重建索引</button>
      </div>
    </div>
    <div class="row" style="gap:24px;flex-wrap:wrap;margin-top:10px">
      <div><div class="muted">记忆数</div><div style="font-size:18px">${Number(idx.memory_count || 0)}</div></div>
      <div><div class="muted">chunk 数</div><div style="font-size:18px">${Number(idx.chunk_count || 0)}</div></div>
      <div><div class="muted">待补建</div><div style="font-size:18px">${Number(idx.stale_count || 0)}</div></div>
    </div>
    <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px">
      <input id="memory-search-query" placeholder="测试长期记忆召回" value="${escapeAttr(state.memorySearchQuery || '')}" onkeydown="if(event.key==='Enter')testMemoryRecall()" style="min-width:260px;flex:1">
      <button class="btn small primary" onclick="testMemoryRecall()">测试召回</button>
    </div>
    ${state.memorySearchResult ? `<table style="margin-top:10px"><thead><tr><th>记忆</th><th>摘要</th><th>相关性</th><th>分数</th></tr></thead><tbody>${searchRows || '<tr><td colspan="4" class="muted">无召回结果</td></tr>'}</tbody></table>` : ''}
  </div>`;
}

async function pickMemoryFilter(t) {
  state.memoryFilter = t;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function rebuildMemoryVectorIndex() {
  state.memoryVectorBusy = true;
  render();
  try {
    const result = await api("/memory/vector-index/rebuild", { method:"POST" });
    state.memoryVectorIndex = result.index || state.memoryVectorIndex;
    alertFlash("ok", `已重建 ${result.rebuilt || 0} 条记忆索引`);
    await loadView(); render();
  } catch (e) {
    alertFlash("err", "重建失败：" + e.message);
  } finally {
    state.memoryVectorBusy = false;
    render();
  }
}

async function testMemoryRecall() {
  const input = document.getElementById("memory-search-query");
  const query = (input ? input.value : state.memorySearchQuery || "").trim();
  state.memorySearchQuery = query;
  if (!query) { alertFlash("err", "请输入召回测试 query"); return; }
  const qs = new URLSearchParams({ query, limit: "8" });
  if (state.memoryUserId) qs.set("user_id", state.memoryUserId);
  if (state.memoryGroupId) qs.set("group_id", state.memoryGroupId);
  try {
    state.memorySearchResult = await api("/memory/search-test?" + qs.toString());
    render();
  } catch (e) {
    alertFlash("err", "召回测试失败：" + e.message);
  }
}

async function toggleMemoryIncludeSelf(checked) {
  state.memoryIncludeSelf = !!checked;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function applyMemoryFilters() {
  state.memoryUserId = (document.getElementById("mem-user-input")?.value || "").trim();
  state.memoryGroupId = (document.getElementById("mem-group-input")?.value || "").trim();
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function pickPalaceZone(zone) {
  state.memoryPalaceZone = zone || "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function pickMemoryLimit(value) {
  const n = Number(value || 200);
  state.memoryLimit = [100, 200, 500].includes(n) ? n : 200;
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function clearMemoryFilters() {
  state.memoryUserId = "";
  state.memoryGroupId = "";
  state.memoryPalaceZone = "";
  try { await loadView(); render(); } catch (e) { alertFlash("err", e.message); }
}

async function openMemoryDetail(memoryId) {
  if (!memoryId) return;
  try {
    state.selectedMemory = await api("/memory/detail/" + encodeURIComponent(memoryId));
    render();
  } catch (e) { alertFlash("err", e.message); }
}

function renderMemoryDetail() {
  const d = state.selectedMemory;
  const it = d.item || {};
  const related = d.related || [];
  const tagLine = (label, arr) => arr && arr.length ? `<div style="margin:4px 0"><span class="muted" style="font-size:12px">${escapeHtml(label)}：</span>${arr.map(v => `<span class="tag">${escapeHtml(String(v))}</span>`).join("")}</div>` : '';
  const relatedRows = related.map(r => `<tr>
    <td><span class="tag">${escapeHtml(memoryTypeLabel(r))}</span></td>
    <td>${escapeHtml((r.summary||'').slice(0,120))}</td>
    <td><button class="btn small" onclick="openMemoryDetail('${escapeAttr(r.memory_id||'')}')">查看</button></td>
  </tr>`).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedMemory=null;render()">返回列表</button><span class="muted">记忆 ${escapeHtml(d.memory_id)}</span></div>
    <div class="card">
      <h2>${escapeHtml(memoryTypeLabel(it))} <code style="font-size:13px;color:var(--muted)">${escapeHtml(d.memory_id)}</code></h2>
      <div class="row" style="gap:20px;flex-wrap:wrap;font-size:13px">
        ${it.palace_zone ? `<div><span class="muted">记忆分区：</span><strong>${escapeHtml(it.palace_zone_label || it.palace_zone)}</strong></div>` : ''}
        ${it.group_id ? `<div><span class="muted">群：</span><code>${escapeHtml(it.group_id)}</code></div>` : ''}
        ${it.user_id ? `<div><span class="muted">用户：</span><code>${escapeHtml(it.user_id)}</code></div>` : ''}
        <div><span class="muted">置信度：</span>${(it.confidence||0).toFixed(2)}</div>
        <div><span class="muted">显著度：</span>${(it.salience||0).toFixed(2)}</div>
        ${typeof it.stability === 'number' ? `<div><span class="muted">稳定度：</span>${it.stability.toFixed(2)}</div>` : ''}
      </div>
      <h3 style="margin-top:14px">摘要</h3>
      <pre style="white-space:pre-wrap;margin:0;font-family:inherit">${escapeHtml(it.summary || '')}</pre>
      ${tagLine('主题标签', it.topic_tags)}
      ${tagLine('实体标签', it.entity_tags)}
      ${tagLine('别名', it.aliases)}
      ${it.why_relevant ? `<h3>关联说明</h3><p>${escapeHtml(it.why_relevant)}</p>` : ''}
      ${it.time_hint ? `<p class="muted" style="font-size:12px">时间提示：${escapeHtml(it.time_hint)}</p>` : ''}
      <details style="margin-top:12px"><summary class="muted">完整数据</summary><pre style="white-space:pre-wrap;font-size:12px;background:#0b0d12;padding:10px;border-radius:6px;overflow-x:auto">${escapeHtml(JSON.stringify(it, null, 2))}</pre></details>
    </div>
    ${related.length ? `<div class="card"><h3>关联记忆（${related.length}）</h3><table><tbody>${relatedRows}</tbody></table></div>` : ''}`;
}

function viewTitle() {
  return ({dashboard:"仪表盘",config:"配置中心",personas:"用户画像",groups:"群信息",group_switch:"群开关",memory:"Agent 记忆",memory_graph:"记忆宫殿",stickers:"表情包",skills:"Skill 管理",plugin_knowledge:"插件知识库",plugin_manager:"插件管理",test:"模型测试",persona_prompt:"人设预览",persona_builder:"人设构建",audit:"审计日志",logs:"插件日志",traces:"消息 Trace",trace_detail:"Trace 详情",proactive:"主动诊断",health:"功能体检",qzone:"QQ 空间",qq:"QQ 管理",devices:"设备管理"})[state.view] || state.view;
}

// ---------------------------------------------------------------------------
// 记忆宫殿（Cytoscape 力导向关系图）
// ---------------------------------------------------------------------------
let _cytoscapeLoaded = false;
let _cytoscapeInstance = null;

function ensureCytoscapeLoaded() {
  if (_cytoscapeLoaded) return Promise.resolve();
  if (window.cytoscape) { _cytoscapeLoaded = true; return Promise.resolve(); }
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js';
    s.onload = () => { _cytoscapeLoaded = true; resolve(); };
    s.onerror = () => reject(new Error('cytoscape 加载失败（检查网络）'));
    document.head.appendChild(s);
  });
}

function _memoryGraphColor(kind) {
  if (kind === 'memory') return '#6aa8ff';
  if (kind === 'entity') return '#f59e0b';
  if (kind === 'user') return '#34d399';
  return '#9ca3af';
}

function renderMemoryGraph() {
  const data = state.memoryGraph;
  const nodeCount = data ? (data.nodes||[]).length : 0;
  const edgeCount = data ? (data.edges||[]).length : 0;
  const groups = state.groupList || [];
  const opts = groups.map(g => `<option value="${escapeAttr(g.group_id)}" ${state.memoryGraphGroupId===g.group_id?'selected':''}>${escapeHtml(g.group_name||g.group_id)}</option>`).join('');
  const unavailable = data && data.available === false;
  const errorBox = unavailable
    ? `<div class="alert err" style="margin-bottom:10px">记忆宫殿不可用（${escapeHtml(data.reason||data.error||'未知原因')}）。检查 personification_memory_palace_enabled。</div>`
    : '';
  setTimeout(() => { try { renderMemoryGraphCanvas(); } catch(e) { console.warn('cytoscape', e); } }, 60);
  return `<div class="card">
    <div class="between" style="margin-bottom:10px">
      <h2 style="margin:0">记忆宫殿 力导向图</h2>
      <div class="row">
        <button class="btn small" onclick="resetMemoryGraphZoom()">复位视图</button>
        <button class="btn small" onclick="exportMemoryGraphPNG()">导出PNG</button>
      </div>
    </div>
    ${errorBox}
    <div class="row" style="gap:10px;flex-wrap:wrap;margin-bottom:10px;align-items:center">
      <label class="muted" style="font-size:12px">群：</label>
      <select onchange="state.memoryGraphGroupId=this.value; loadView().then(render)" style="min-width:180px">
        <option value="">（全局）</option>
        ${opts}
      </select>
      <label class="muted" style="font-size:12px">条目上限：</label>
      <input type="number" value="${state.memoryGraphLimit||100}" min="10" max="300" step="10" onchange="state.memoryGraphLimit=Number(this.value); loadView().then(render)" style="width:80px">
      <label class="muted" style="font-size:12px">最小 salience：</label>
      <input type="number" value="${state.memoryGraphMinSalience||0}" min="0" max="1" step="0.05" onchange="state.memoryGraphMinSalience=Number(this.value); loadView().then(render)" style="width:80px">
      <span class="muted" style="font-size:12px;margin-left:auto">节点 ${nodeCount} · 边 ${edgeCount}</span>
    </div>
    <div class="row" style="gap:14px;font-size:12px;margin-bottom:8px">
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#6aa8ff;margin-right:4px"></span>记忆条目</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-right:4px"></span>实体/标签</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#34d399;margin-right:4px"></span>群成员</span>
    </div>
    <div id="memory-graph-canvas" style="width:100%;height:540px;background:var(--input-bg);border:1px solid var(--line);border-radius:6px"></div>
    <div id="memory-graph-detail" class="muted" style="font-size:12px;margin-top:8px">点击节点查看详情。</div>
  </div>`;
}

async function renderMemoryGraphCanvas() {
  const data = state.memoryGraph;
  if (!data || !data.available) return;
  const el = document.getElementById('memory-graph-canvas');
  if (!el) return;
  try { await ensureCytoscapeLoaded(); } catch (e) {
    el.innerHTML = '<p class="muted" style="padding:20px">' + escapeHtml(e.message) + '</p>';
    return;
  }
  const nodes = (data.nodes || []).map(n => {
    const w = Number(n.salience || n.weight || 0.3) || 0.3;
    return {
      data: {
        id: n.id, label: n.label || n.id, kind: n.kind, kindLabel: memoryNodeKindLabel(n), color: _memoryGraphColor(n.kind),
        size: 16 + Math.min(30, w * 40),
        raw: n,
      }
    };
  });
  const edges = (data.edges || []).map((e, i) => ({
    data: {
      id: 'e' + i, source: e.src, target: e.dst,
      kind: e.kind, kindLabel: memoryRelationLabel(e), weight: e.weight,
      thickness: Math.max(1, Math.min(6, Number(e.weight || 1))),
    }
  }));
  if (_cytoscapeInstance) { try { _cytoscapeInstance.destroy(); } catch {} _cytoscapeInstance = null; }
  const theme = document.documentElement.getAttribute('data-theme') || 'dark';
  const labelColor = theme === 'light' ? '#1f2937' : '#e6e8ef';
  const labelOutline = theme === 'light' ? '#ffffff' : '#0f1115';
  const edgeColor = theme === 'light' ? '#9ca3af' : '#3b4252';
  const selectedBorder = theme === 'light' ? '#1f2937' : '#ffffff';
  _cytoscapeInstance = window.cytoscape({
    container: el,
    elements: { nodes, edges },
    style: [
      { selector: 'node', style: {
        'background-color': 'data(color)', 'label': 'data(label)',
        'color': labelColor, 'font-size': '10px', 'width': 'data(size)', 'height': 'data(size)',
        'text-valign': 'bottom', 'text-margin-y': 4, 'text-outline-width': 1, 'text-outline-color': labelOutline,
      }},
      { selector: 'edge', style: {
        'width': 'data(thickness)', 'line-color': edgeColor, 'curve-style': 'bezier',
        'opacity': 0.72, 'target-arrow-shape': 'none',
        'label': 'data(kindLabel)', 'font-size': '8px', 'color': labelColor,
        'text-outline-width': 1, 'text-outline-color': labelOutline,
      }},
      { selector: 'node:selected', style: { 'border-width': 2, 'border-color': selectedBorder }},
    ],
    layout: { name: 'cose', animate: false, idealEdgeLength: 90, nodeRepulsion: 6000, padding: 30 },
    wheelSensitivity: 0.2,
  });
  _cytoscapeInstance.on('tap', 'node', evt => {
    const raw = evt.target.data('raw') || {};
    const detail = document.getElementById('memory-graph-detail');
    if (!detail) return;
    const lines = [
      `<strong>${escapeHtml(raw.label || raw.id)}</strong>`,
      raw.kind ? `<span class="tag">${escapeHtml(memoryNodeKindLabel(raw))}</span>` : '',
      raw.memory_type ? `<span class="muted">记忆类型：${escapeHtml(memoryTypeLabel(raw))}</span>` : '',
      raw.palace_zone ? `<span class="muted">分区：${escapeHtml(raw.palace_zone_label || raw.palace_zone)}</span>` : '',
      typeof raw.salience === 'number' ? `<span class="muted">显著度：${raw.salience.toFixed(2)}</span>` : '',
      raw.group_id ? `<span class="muted">群：${escapeHtml(raw.group_id)}</span>` : '',
    ].filter(Boolean);
    detail.innerHTML = lines.join(' · ');
  });
  _cytoscapeInstance.on('tap', 'edge', evt => {
    const edge = evt.target.data() || {};
    const detail = document.getElementById('memory-graph-detail');
    if (!detail) return;
    const source = edge.source || "";
    const target = edge.target || "";
    const weight = Number(edge.weight || 0);
    detail.innerHTML = [
      `<strong>${escapeHtml(edge.kindLabel || "关联")}</strong>`,
      `<span class="muted">来源：${escapeHtml(source)}</span>`,
      `<span class="muted">目标：${escapeHtml(target)}</span>`,
      `<span class="muted">权重：${weight.toFixed(2)}</span>`,
    ].join(' · ');
  });
}

function resetMemoryGraphZoom() {
  if (_cytoscapeInstance) { _cytoscapeInstance.fit(); _cytoscapeInstance.center(); }
}

function exportMemoryGraphPNG() {
  if (!_cytoscapeInstance) return;
  try {
    const theme = document.documentElement.getAttribute('data-theme') || 'dark';
    const bg = theme === 'light' ? '#f6f8fb' : '#0f1115';
    const blob = _cytoscapeInstance.png({ output: 'blob', bg, full: true, scale: 2 });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'memory-palace.png';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (e) { alertFlash('err', '导出失败：' + e.message); }
}
