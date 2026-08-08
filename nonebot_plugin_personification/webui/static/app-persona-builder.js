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
