function opsStatus(value) {
  const map = {online:["ok","在线"],degraded:["warn","降级"],offline:["error","离线"],running:["info","运行中"],stale:["warn","状态陈旧"],finished:["ok","已完成"]};
  const item = map[value] || ["disabled",value||"未知"];
  return `<span class="ops-status ${item[0]}"><span></span>${escapeHtml(item[1])}</span>`;
}

function opsAgo(seconds) {
  const value=Number(seconds||0);
  if(value<60)return `${Math.round(value)} 秒前`;
  if(value<3600)return `${Math.round(value/60)} 分钟前`;
  return `${Math.round(value/3600)} 小时前`;
}

function renderAgentStatus() {
  const data=state.agentStatus;
  if(!data)return `<div class="ops-hero skeleton-card"></div>`;
  const inner=data.inner_state||{};
  const rows=(data.recent||[]).map(row=>`<tr><td class="col-status">${opsStatus(row.state)}</td><td class="col-id"><code class="u-ellipsis" title="${escapeAttr(row.trace_id)}">${escapeHtml(row.trace_id)}</code></td><td class="col-id"><span class="u-ellipsis" title="${escapeAttr(row.stage || "-")}">${escapeHtml(row.stage||"-")}</span></td><td class="col-status"><span class="u-ellipsis" title="${escapeAttr(row.outcome || row.diagnosis_code || "-")}">${escapeHtml(row.outcome||row.diagnosis_code||"-")}</span></td><td class="col-time u-atomic u-tabular">${escapeHtml(opsAgo(row.age_seconds))}</td><td class="col-actions"><button class="btn small" aria-label="查看 Trace ${escapeAttr(row.trace_id)}" onclick="openAgentTrace('${escapeAttr(row.trace_id)}')">Trace</button></td></tr>`).join("");
  return `<section class="ops-hero"><div><span class="eyebrow">LIVE RUNTIME</span><h2>Agent 运行脉搏</h2><p>只展示可审计状态，不暴露隐藏推理、画像正文或工具参数。</p></div><div class="ops-hero-state">${opsStatus(data.overall)}<button class="btn small" onclick="refreshAgentStatus()">立即刷新</button></div></section>
  <div class="ops-stat-grid"><div class="ops-stat"><span>连接 Bot</span><strong>${Number((data.bots||{}).connected||0)}</strong></div><div class="ops-stat"><span>正在执行</span><strong>${Number(data.running||0)}</strong></div><div class="ops-stat"><span>陈旧任务</span><strong>${Number(data.stale||0)}</strong></div><div class="ops-stat"><span>内心状态</span><strong>${escapeHtml(inner.mood||"-")} · ${escapeHtml(inner.energy||"-")}</strong><small class="u-atomic u-tabular">${escapeHtml(inner.updated_at||"尚未更新")}</small></div></div>
  <div class="card"><div class="between"><h2>最近运行</h2><span class="muted u-atomic">5 秒自动刷新</span></div><div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="Agent 最近运行列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-status">状态</th><th scope="col" class="col-id">Trace</th><th scope="col" class="col-id">当前/末阶段</th><th scope="col" class="col-status">结果</th><th scope="col" class="col-time">最后活动</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows||'<tr><td colspan="6" class="muted">暂无运行记录</td></tr>'}</tbody></table></div></div>`;
}

async function refreshAgentStatus(){try{state.agentStatus=await api("/agent-status");render();}catch(e){alertFlash("err","状态刷新失败："+e.message);}}
async function openAgentTrace(traceId){await ensureViewAsset("trace_detail");return openTraceDetail(traceId);}
setInterval(()=>{if(state.logged&&state.view==="agent_status"&&!state.loading)refreshAgentStatus();},5000);

const TRANSFER_DIAGNOSTIC_FIELDS=new Set(["ok","code","phase","title","message","details","steps","warnings","suggestion","retryable","partial","outcome_unknown","operation_id","trace_id","error"]);
function transferDiagnostic(value){return value&&value.code?value:null;}
function renderTransferDiagnostics(values){const items=values.map(transferDiagnostic).filter(Boolean);const diagnostics=renderOperationHistory(items,{group:"view-data_transfer"});return diagnostics?`<div class="card"><h2>数据迁移操作诊断</h2>${diagnostics}</div>`:"";}
function transferPreview(value){const result={};for(const [key,item] of Object.entries(value||{})){if(!TRANSFER_DIAGNOSTIC_FIELDS.has(key))result[key]=key==="plan_token"?"[已绑定当前参数]":item;}return result;}

function renderDataTransfer(){
  const exp=state.transferExport||{},imp=state.transferImport||{},inspect=imp.inspect||{},dry=imp.dryRun||null;
  const connectedBot=(state.transferBotInfo||{}).user_id||"";
  const rollbackBlocked=Boolean(imp.rollbackDiagnostic&&(imp.rollbackDiagnostic.outcome_unknown||!imp.rollbackDiagnostic.retryable));
  const diagnostics=renderTransferDiagnostics([imp.rollbackDiagnostic,imp.applyDiagnostic,dry||imp.dryRunDiagnostic,imp.inspectDiagnostic,imp.uploadDiagnostic,exp]);
  return `<section class="ops-hero"><div><span class="eyebrow">PORTABLE PERSONA</span><h2>拟人数据迁移舱</h2><p>默认创建群安全包。凭证、设备令牌、日志、审计和 Provider 信息永不进入压缩包。</p></div><div class="transfer-seal">ZIP<br><small>v1</small></div></section>
  ${diagnostics}
  <div class="ops-grid"><div class="card transfer-card"><span class="step-no">01</span><h2>打包与下载</h2><label>Bot QQ</label><input id="transfer-bot" value="${escapeAttr(exp.botId||connectedBot)}" placeholder="Bot QQ"><label>目标群</label><input id="transfer-group" value="${escapeAttr(exp.groupId||"")}" placeholder="群号"><label class="row"><input id="transfer-raw-history" type="checkbox">额外包含原始群消息与会话历史（高隐私）</label><p class="muted">默认包含群内画像、关系、风格、知识与长期记忆，不包含原始聊天。</p><div class="row"><button class="btn primary" onclick="createTransferExport()">创建群安全包</button>${exp.task_id?`<a class="btn" href="${API}/data-transfer/exports/${encodeURIComponent(exp.task_id)}/download">下载 ZIP</a>`:""}</div>${exp.task_id?`<div class="transfer-result"><code class="u-ellipsis" title="${escapeAttr(exp.task_id)}">${escapeHtml(exp.task_id)}</code><span class="tag tag--status">${escapeHtml(exp.status||"completed")}</span></div>`:""}</div>
  <div class="card transfer-card"><span class="step-no">02</span><h2>上传与验包</h2><input id="transfer-file" type="file" accept=".zip,application/zip"><button class="btn primary" onclick="uploadTransferPackage()">上传并检查</button>${inspect.manifest?`<div class="transfer-manifest"><strong class="u-ellipsis" title="${escapeAttr(inspect.manifest.package_id || "")}">${escapeHtml(inspect.manifest.package_id||"")}</strong><span class="u-atomic u-tabular">来源 Bot ${escapeHtml((inspect.manifest.source||{}).bot_id||"")}</span><span class="u-atomic u-tabular">群 ${escapeHtml((inspect.manifest.source||{}).group_id||"")}</span></div>`:""}</div></div>
  ${imp.task_id&&inspect.valid?`<div class="card transfer-card"><span class="step-no">03</span><h2>预演、导入与回滚</h2><div class="transfer-plan-grid"><input id="transfer-target-bot" value="${escapeAttr(imp.targetBotId||connectedBot)}" placeholder="目标 Bot QQ" oninput="invalidateTransferPlan()"><input id="transfer-target-group" value="${escapeAttr(imp.targetGroupId||"")}" placeholder="目标群号" oninput="invalidateTransferPlan()"><select id="transfer-mode" onchange="invalidateTransferPlan()"><option value="merge" ${imp.mode==='merge'?'selected':''}>安全合并</option><option value="scope-replace" ${imp.mode==='scope-replace'?'selected':''}>替换目标群数据</option></select></div><div class="row"><button class="btn" onclick="dryRunTransferImport()">先做 Dry-run</button><button id="transfer-apply" class="btn primary" onclick="applyTransferImport()" ${dry?'':'disabled'}>确认导入</button>${imp.journalId?`<button class="btn danger" onclick="rollbackTransferImport()" ${rollbackBlocked?'disabled':''}>回滚本次导入</button>`:""}</div>${dry?`<pre id="transfer-preview" class="transfer-preview">${escapeHtml(JSON.stringify(transferPreview(dry),null,2))}</pre>`:'<p id="transfer-preview" class="muted">必须先完成 Dry-run，确认影响范围后才能导入。</p>'}</div>`:""}`;
}

async function createTransferExport(){
  const botId=document.getElementById("transfer-bot").value.trim(),groupId=document.getElementById("transfer-group").value.trim(),raw=document.getElementById("transfer-raw-history").checked;
  if(!botId||!groupId)return alertFlash("err","请填写 Bot QQ 和群号");
  const datasets=["conversation_threads","group_relation_edges","group_style_snapshots","group_state","local_user_profiles","group_memories","avatar_relation_evidence","meme_dictionary"];
  if(raw)datasets.unshift("group_messages","session_messages");
  try{state.transferExport=await api("/data-transfer/exports/create",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:botId,group_id:groupId,datasets})});}
  catch(e){state.transferExport=operationDiagnosticFromError(e,"创建迁移包未完成");}
  state.transferExport.botId=botId;state.transferExport.groupId=groupId;render();
}
async function uploadTransferPackage(){
  const file=document.getElementById("transfer-file").files[0];
  if(!file)return alertFlash("err","请选择 ZIP 文件");
  const form=new FormData();form.append("file",file);
  let uploaded;
  try{uploaded=await api("/data-transfer/imports/upload",{method:"POST",body:form});}
  catch(e){state.transferImport={uploadDiagnostic:operationDiagnosticFromError(e,"上传迁移包未完成")};render();return;}
  state.transferImport={...uploaded,uploadDiagnostic:uploaded,targetBotId:(state.transferBotInfo||{}).user_id||""};
  try{
    const inspected=await api(`/data-transfer/imports/${encodeURIComponent(uploaded.task_id)}/inspect`),source=inspected.manifest?.source||{};
    state.transferImport.inspect=inspected;state.transferImport.inspectDiagnostic=inspected;state.transferImport.targetGroupId=source.group_id||((source.group_ids)||[])[0]||"";
  }catch(e){state.transferImport.inspectDiagnostic=operationDiagnosticFromError(e,"检查迁移包未完成");}
  render();
}
function transferPlanBody(){return{target_bot_id:document.getElementById("transfer-target-bot").value.trim(),target_group_id:document.getElementById("transfer-target-group").value.trim(),mode:document.getElementById("transfer-mode").value,allow_same_identity:false};}
function invalidateTransferPlan(){if(!state.transferImport)return;state.transferImport.dryRun=null;state.transferImport.dryRunDiagnostic=null;state.transferImport.applyDiagnostic=null;const button=document.getElementById("transfer-apply");if(button)button.disabled=true;const preview=document.getElementById("transfer-preview");if(preview){preview.className="muted";preview.textContent="输入已变化，请重新 Dry-run。";}}
async function dryRunTransferImport(){
  const body=transferPlanBody();state.transferImport.targetBotId=body.target_bot_id;state.transferImport.targetGroupId=body.target_group_id;state.transferImport.mode=body.mode;state.transferImport.applyDiagnostic=null;
  try{const result=await api(`/data-transfer/imports/${encodeURIComponent(state.transferImport.task_id)}/dry-run`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});state.transferImport.dryRun=result;state.transferImport.dryRunDiagnostic=result;}
  catch(e){state.transferImport.dryRun=null;state.transferImport.dryRunDiagnostic=operationDiagnosticFromError(e,"导入预演未完成");}
  render();
}
async function applyTransferImport(){
  const dry=state.transferImport?.dryRun;if(!dry?.plan_token)return alertFlash("err","预演已失效，请重新 Dry-run");
  if(!confirm("确认按 Dry-run 结果导入？服务器只保存目标群前镜像用于回滚。"))return;
  try{const body={...transferPlanBody(),plan_token:dry.plan_token};const result=await api(`/data-transfer/imports/${encodeURIComponent(state.transferImport.task_id)}/apply`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});state.transferImport.journalId=result.journal_id;state.transferImport.dryRun=null;state.transferImport.applyDiagnostic=result;state.transferImport.rollbackDiagnostic=null;}
  catch(e){const report=operationDiagnosticFromError(e,"应用数据导入未完成");state.transferImport.applyDiagnostic=report;if(report.outcome_unknown||!report.retryable)state.transferImport.dryRun=null;}
  render();
}
async function rollbackTransferImport(){
  if(!confirm("回滚本次导入？"))return;
  try{const result=await api(`/data-transfer/imports/${encodeURIComponent(state.transferImport.journalId)}/rollback`,{method:"POST"});state.transferImport.rollbackDiagnostic=result;state.transferImport.journalId="";}
  catch(e){state.transferImport.rollbackDiagnostic=operationDiagnosticFromError(e,"回滚数据导入未完成");}
  render();
}
