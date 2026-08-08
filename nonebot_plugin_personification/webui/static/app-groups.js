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
