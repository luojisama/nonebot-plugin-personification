function opsStatus(value) {
  const map = {online:["ok","在线"],degraded:["warn","降级"],offline:["error","离线"],running:["info","运行中"],stale:["warn","状态陈旧"],finished:["ok","已完成"],completed:["ok","已完成"],failed:["error","失败"],cancelled:["disabled","已取消"]};
  const item = map[value] || ["disabled",value||"未知"];
  return `<span class="ops-status ${item[0]}"><span></span>${escapeHtml(item[1])}</span>`;
}

function opsMegabytes(value) {
  if(value===null||value===undefined||value==="")return "暂不可用";
  const bytes=Number(value);
  return Number.isFinite(bytes)&&bytes>=0?`${(bytes/1024/1024).toFixed(1)} MB`:"暂不可用";
}

function renderRuntimePerformance() {
  const data=state.runtimePerformance;
  if(!data)return `<div class="card"><h2>运行性能</h2><p class="muted">正在读取进程和事件循环指标…</p></div>`;
  const process=data.process||{},loop=data.event_loop||{},reply=data.reply||{},tasks=data.tasks||{},queue=(data.queues||{}).runtime_logs||{};
  const cacheRows=(data.caches||[]).map(item=>`<tr><td>${escapeHtml(item.name||"-")}</td><td class="u-tabular">${Number(item.entries||0)} / ${Number(item.limit||0)}</td><td class="u-tabular">${Number(item.evictions||0)}</td></tr>`).join("");
  return `<div class="card"><div class="between"><h2>运行性能</h2><span class="muted u-atomic">进程内即时采样</span></div><div class="ops-stat-grid"><div class="ops-stat"><span>当前内存</span><strong>${escapeHtml(opsMegabytes(process.rss_bytes))}</strong><small>峰值 ${escapeHtml(opsMegabytes(process.peak_rss_bytes))}</small></div><div class="ops-stat"><span>事件循环 p95</span><strong>${Number(loop.p95_ms||0).toFixed(1)} ms</strong><small>最近 ${Number(loop.samples||0)} 个样本</small></div><div class="ops-stat"><span>回复排队</span><strong>${Number(reply.waiting||0)}</strong><small>${Number(reply.active||0)} 活动 · ${Number(reply.session_gates||0)} 会话 gate</small></div><div class="ops-stat"><span>后台任务</span><strong>${Number(tasks.failed_total||0)} 次失败</strong><small>${Number(tasks.total||0)} 个受监管任务</small></div></div><div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="运行资源使用情况"><table class="data-table"><thead><tr><th scope="col">缓存/容器</th><th scope="col">使用量</th><th scope="col">溢出/淘汰</th></tr></thead><tbody>${cacheRows||'<tr><td colspan="3" class="muted">暂无缓存统计</td></tr>'}<tr><td>运行日志队列</td><td class="u-tabular">${Number(queue.depth||0)} / ${Number(queue.capacity||0)}</td><td class="u-tabular">${Number(queue.dropped||0)}</td></tr></tbody></table></div></div>`;
}

function renderBrowserPerformance(){
  const data=typeof browserPerformanceSnapshot==="function"?browserPerformanceSnapshot():null;
  if(!data)return "";
  const slowest=(data.api||[])[0]||{};
  const lcp=data.lcp_ms==null?"暂不可用":`${Number(data.lcp_ms||0).toFixed(0)} ms`;
  const inp=Number((data.interaction||{}).count||0)?`${Number((data.interaction||{}).p75_ms||0).toFixed(0)} ms`:"暂不可用";
  return `<div class="card"><div class="between"><h2>浏览器当前会话</h2><span class="muted">仅保存在本标签页，不上传路径参数或用户内容</span></div><div class="ops-stat-grid"><div class="ops-stat"><span>活动视图渲染 p95</span><strong>${Number((data.render||{}).p95_ms||0).toFixed(1)} ms</strong><small>${Number((data.render||{}).count||0)} 次采样</small></div><div class="ops-stat"><span>最慢 API p95</span><strong>${Number(slowest.p95_ms||0).toFixed(1)} ms</strong><small>${escapeHtml(slowest.key||"暂无请求")}</small></div><div class="ops-stat"><span>Long Task</span><strong>${Number((data.long_tasks||{}).count||0)}</strong><small>最大 ${Number((data.long_tasks||{}).max_ms||0).toFixed(1)} ms</small></div><div class="ops-stat"><span>Web Vitals</span><strong>LCP ${escapeHtml(lcp)}</strong><small>INP ${escapeHtml(inp)} · CLS ${Number(data.layout_shift||0).toFixed(3)}</small></div></div></div>`;
}

function opsAgo(seconds) {
  const value=Number(seconds||0);
  if(value<60)return `${Math.round(value)} 秒前`;
  if(value<3600)return `${Math.round(value/60)} 分钟前`;
  return `${Math.round(value/3600)} 小时前`;
}

function renderAgentStatusContent() {
  const data=state.agentStatus;
  if(!data)return `<div class="ops-hero skeleton-card"></div>`;
  const inner=data.inner_state||{};
  const rows=(data.recent||[]).map(row=>`<tr><td class="col-status">${opsStatus(row.state)}</td><td class="col-id"><code class="u-ellipsis" title="${escapeAttr(row.trace_id)}">${escapeHtml(row.trace_id)}</code></td><td class="col-id"><span class="u-ellipsis" title="${escapeAttr(row.stage || "-")}">${escapeHtml(row.stage||"-")}</span></td><td class="col-status"><span class="u-ellipsis" title="${escapeAttr(row.outcome || row.diagnosis_code || "-")}">${escapeHtml(row.outcome||row.diagnosis_code||"-")}</span></td><td class="col-time u-atomic u-tabular">${escapeHtml(opsAgo(row.age_seconds))}</td><td class="col-actions"><button class="btn small" aria-label="查看 Trace ${escapeAttr(row.trace_id)}" onclick="openAgentTrace('${escapeAttr(row.trace_id)}')">Trace</button></td></tr>`).join("");
  return `<section class="ops-hero"><div><span class="eyebrow">LIVE RUNTIME</span><h2>Agent 运行脉搏</h2><p>只展示可审计状态，不暴露隐藏推理、画像正文或工具参数。</p></div><div class="ops-hero-state">${opsStatus(data.overall)}<button class="btn small" onclick="refreshAgentStatus()">立即刷新</button></div></section>
  <div class="ops-stat-grid"><div class="ops-stat"><span>连接 Bot</span><strong>${Number((data.bots||{}).connected||0)}</strong></div><div class="ops-stat"><span>正在执行</span><strong>${Number(data.running||0)}</strong></div><div class="ops-stat"><span>陈旧任务</span><strong>${Number(data.stale||0)}</strong></div><div class="ops-stat"><span>内心状态</span><strong>${escapeHtml(inner.mood||"-")} · ${escapeHtml(inner.energy||"-")}</strong><small class="u-atomic u-tabular">${escapeHtml(inner.updated_at||"尚未更新")}</small></div></div>
  ${renderRuntimePerformance()}${renderBrowserPerformance()}<div class="card"><div class="between"><h2>最近运行</h2><span class="muted u-atomic">5 秒自动刷新</span></div><div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="Agent 最近运行列表"><table class="data-table wide"><thead><tr><th scope="col" class="col-status">状态</th><th scope="col" class="col-id">Trace</th><th scope="col" class="col-id">当前/末阶段</th><th scope="col" class="col-status">结果</th><th scope="col" class="col-time">最后活动</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows||'<tr><td colspan="6" class="muted">暂无运行记录</td></tr>'}</tbody></table></div></div>`;
}

function renderAgentStatus(){return `<div id="agent-status-island">${renderAgentStatusContent()}</div>`;}

let _agentStatusTimer=null;
let _agentStatusAbort=null;
let _agentStatusRunning=false;
let _agentStatusGeneration=0;
let _agentStatusFingerprint="";

function agentStatusFingerprint(status,performanceData){
  try{return JSON.stringify([status||null,performanceData||null]);}catch{return String(Date.now());}
}

function updateAgentStatusIsland(){
  const island=document.getElementById("agent-status-island");
  if(island&&state.view==="agent_status"){
    island.innerHTML=renderAgentStatusContent();
    prepareDetailState(island);
  }
}

async function refreshAgentStatus({manual=true,generation=_agentStatusGeneration}={}){
  if(_agentStatusRunning||!state.logged||state.view!=="agent_status"||document.hidden)return false;
  _agentStatusRunning=true;
  const controller=new AbortController();
  _agentStatusAbort=controller;
  try{
    const [status,performanceData]=await Promise.all([
      api("/agent-status",{signal:controller.signal,cache:"no-store"}),
      api("/performance/runtime",{signal:controller.signal,cache:"no-store"}),
    ]);
    if(generation!==_agentStatusGeneration||state.view!=="agent_status")return false;
    const fingerprint=agentStatusFingerprint(status,performanceData);
    state.agentStatus=status;
    state.runtimePerformance=performanceData;
    if(fingerprint!==_agentStatusFingerprint){
      _agentStatusFingerprint=fingerprint;
      updateAgentStatusIsland();
    }
    return true;
  }catch(e){
    if(e?.name!=="AbortError"&&manual)alertFlash("err","状态刷新失败："+e.message);
    return false;
  }finally{
    if(_agentStatusAbort===controller)_agentStatusAbort=null;
    _agentStatusRunning=false;
  }
}

function scheduleAgentStatusPoll(generation,delay=5000){
  if(_agentStatusTimer)clearTimeout(_agentStatusTimer);
  if(generation!==_agentStatusGeneration||!state.logged||state.view!=="agent_status"||document.hidden)return;
  _agentStatusTimer=setTimeout(async()=>{
    _agentStatusTimer=null;
    await refreshAgentStatus({manual:false,generation});
    scheduleAgentStatusPoll(generation,5000);
  },delay);
}

function startAgentStatusPolling(){
  const generation=++_agentStatusGeneration;
  _agentStatusFingerprint=agentStatusFingerprint(state.agentStatus,state.runtimePerformance);
  scheduleAgentStatusPoll(generation,5000);
}

function stopAgentStatusPolling(){
  _agentStatusGeneration+=1;
  if(_agentStatusTimer){clearTimeout(_agentStatusTimer);_agentStatusTimer=null;}
  if(_agentStatusAbort){try{_agentStatusAbort.abort();}catch{}_agentStatusAbort=null;}
  _agentStatusRunning=false;
}

if(!window.__personificationAgentStatusVisibility){
  window.__personificationAgentStatusVisibility=true;
  document.addEventListener("visibilitychange",()=>{
    if(document.hidden)stopAgentStatusPolling();
    else if(state.logged&&state.view==="agent_status")startAgentStatusPolling();
  });
}
async function openAgentTrace(traceId){await ensureViewAsset("trace_detail");return openTraceDetail(traceId);}

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
