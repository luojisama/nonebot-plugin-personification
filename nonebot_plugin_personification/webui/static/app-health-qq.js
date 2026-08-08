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
    const isVideo = cat.name === "视频理解";
    return `<div class="health-cat">
      <h3>${escapeHtml(cat.name)}
        <button class="btn small" style="margin-left:auto" onclick="recheckCategory('${escapeAttr(cat.name)}')">${busy?'检测中…':(isVideo?'上传视频并重测':'重测')}</button>
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
    <p class="muted" style="font-size:12px;margin:8px 0 0">对各模块做<b>真实调用探测</b>（含画像/风格/视觉打标等子模型）。结果缓存展示、秒开；启动与配置变更后自动重跑，也可点「全部重新检测」或单项「重测」。视频理解需要点击“上传视频并重测”选择本机视频，单项重测会真实调用一次视频模型，不会向 QQ 发消息；视频只临时写入插件/配置数据目录，完成后自动清理。红=异常，黄=会影响行为，灰=未启用。</p>
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
    let r;
    if (name === "视频理解") {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "video/*,.mp4,.mov,.m4v,.webm,.mkv,.avi";
      input.style.display = "none";
      document.body.appendChild(input);
      const file = await new Promise(resolve => {
        const finish = value => resolve(value);
        input.addEventListener("change", () => finish(input.files && input.files[0] ? input.files[0] : null), { once:true });
        input.addEventListener("cancel", () => finish(null), { once:true });
        input.click();
      });
      input.remove();
      if (!file) {
        state.healthBusyCat = "";
        render();
        return;
      }
      r = await api("/health/video-probe", {
        method: "POST",
        headers: {
          "content-type": file.type || "application/octet-stream",
          "X-Personification-Video-Filename": file.name || "video.mp4",
        },
        body: file,
      });
    } else {
      r = await api("/health/check?only=" + encodeURIComponent(name));
    }
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
