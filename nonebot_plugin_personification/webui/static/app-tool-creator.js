let _toolCreatorTimer = 0;

const TOOL_CREATOR_ACTIVE = new Set(["queued", "planning", "researching", "generating", "validating", "publishing"]);

function toolCreatorStatusLabel(status) {
  return ({queued:"排队中",planning:"规划中",researching:"搜索中",generating:"整合中",validating:"校验中",awaiting_admin:"等待回答",ready_for_approval:"等待批准",publishing:"发布中",completed:"已完成",failed:"失败",cancelled:"已取消"})[String(status||"")] || String(status||"未知");
}

function toolCreatorStatusTag(task) {
  const status=String(task&&task.status||"");
  const cls=status==="completed"?"approved":status==="failed"?"rejected":status==="awaiting_admin"||status==="ready_for_approval"?"pending":"";
  return `<span class="device-status tag--status ${escapeAttr(cls)}">${escapeHtml(toolCreatorStatusLabel(status))}</span>`;
}

function renderToolCreatorTaskList() {
  const rows=(state.toolCreatorTasks||[]).map(task=>`<button class="tool-creator-task ${state.toolCreatorSelectedId===task.task_id?'selected':''}" data-tool-creator-task="${escapeAttr(task.task_id)}">
    <span><strong>${escapeHtml((task.context&&task.context.manifest&&task.context.manifest.name)||task.suggested_name||"未命名工具")}</strong><small>${escapeHtml(new Date(Number(task.created_at||0)*1000).toLocaleString())}</small></span>
    ${toolCreatorStatusTag(task)}
  </button>`).join("");
  return `<div class="card tool-creator-history"><div class="between"><h2>创建记录</h2><button class="btn small" data-tool-creator-refresh>刷新</button></div><div class="tool-creator-task-list">${rows||'<p class="muted">还没有创建任务。</p>'}</div></div>`;
}

function renderToolCreatorEvents(events) {
  const rows=(events||[]).map(item=>`<li><span>${String(Number(item.seq||0)).padStart(2,"0")}</span><div><strong>${escapeHtml(toolCreatorStatusLabel(item.phase||item.event_type))}</strong><p>${escapeHtml(item.payload&&item.payload.message||item.event_type||"")}</p></div><time>${escapeHtml(new Date(Number(item.created_at||0)*1000).toLocaleTimeString())}</time></li>`).join("");
  return `<ol class="tool-creator-events">${rows||'<li class="muted">暂无阶段事件</li>'}</ol>`;
}

function renderToolCreatorQuestion(task) {
  const q=task.question||{};
  if(task.status!=="awaiting_admin"||!q.question_id)return "";
  const options=(q.options||[]).map(option=>`<button class="btn btn--wrap small" data-tool-creator-answer-option="${escapeAttr(option)}">${escapeHtml(option)}</button>`).join("");
  const creator=String(task.created_by||"")===String(state.qq||"");
  return `<section class="tool-creator-question"><span class="eyebrow">ADMIN DECISION REQUIRED</span><h3>${escapeHtml(q.prompt||"需要补充信息")}</h3><p>${escapeHtml(q.reason||"")}</p>${options?`<div class="row">${options}</div>`:""}<textarea id="tool-creator-answer" placeholder="输入你的决定；提交后 LLM 会从当前任务继续" oninput="state.toolCreatorAnswer=this.value" ${creator?'':'disabled'}>${escapeHtml(state.toolCreatorAnswer||"")}</textarea><button class="btn primary" data-tool-creator-answer ${creator?'':'disabled'}>提交回答</button>${creator?'':'<small class="muted">只有任务创建者可以回答。</small>'}</section>`;
}

function renderToolCreatorArtifact(task) {
  const context=task.context||{};
  const manifest=context.manifest;
  if(!manifest)return "";
  const files=(context.artifact_files||[]).map(item=>`<tr><td class="col-description"><code class="u-wrap">${escapeHtml(item.path||"")}</code></td><td class="col-number u-atomic u-tabular">${Number(item.size||0).toLocaleString()} B</td></tr>`).join("");
  return `<section class="tool-creator-artifact"><div class="between"><div><span class="eyebrow">REUSABLE SKILL</span><h3 class="u-ellipsis" title="${escapeAttr(manifest.name || "")}">${escapeHtml(manifest.name||"")}</h3></div><code class="u-atomic" title="${escapeAttr(task.artifact_digest || "")}">${escapeHtml(String(task.artifact_digest||"").slice(0,16))}</code></div><p class="u-wrap">${escapeHtml(manifest.description||"")}</p><div class="row">${(manifest.execution&&manifest.execution.allowed_tools||[]).map(name=>`<span class="tag tag--ellipsis" title="${escapeAttr(name)}">${escapeHtml(name)}</span>`).join("")}</div><details><summary>查看 manifest</summary><pre class="u-pre-wrap code-scroll">${escapeHtml(JSON.stringify(manifest,null,2))}</pre></details><div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="工具创建产物列表"><table class="data-table compact"><thead><tr><th scope="col" class="col-description">产物</th><th scope="col" class="col-number">大小</th></tr></thead><tbody>${files}</tbody></table></div></section>`;
}

function renderToolCreatorDetail() {
  const detail=state.toolCreatorDetail;
  if(!detail||!detail.task)return `<div class="card muted">选择一条任务查看构建过程。</div>`;
  const task=detail.task;
  const creator=String(task.created_by||"")===String(state.qq||"");
  const canCancel=creator&&!new Set(["completed","failed","cancelled","publishing"]).has(task.status);
  const canRetry=creator&&task.status==="failed"&&task.phase!=="publish_outcome_unknown";
  const canApprove=creator&&task.status==="ready_for_approval";
  return `<div class="card tool-creator-detail">
    <div class="between"><div><span class="eyebrow">BUILD SESSION</span><h2>${escapeHtml((task.context&&task.context.manifest&&task.context.manifest.name)||task.suggested_name||"创建工具")}</h2><p>${escapeHtml(task.request_text||"")}</p></div>${toolCreatorStatusTag(task)}</div>
    <div class="tool-creator-progress"><span style="width:${Math.max(0,Math.min(100,Number(task.progress||0)))}%"></span></div>
    <div class="row muted"><span>阶段 ${escapeHtml(task.phase||"")}</span><span>进度 ${Number(task.progress||0)}%</span><span>创建者 ${escapeHtml(task.created_by||"")}</span><span>版本 ${Number(task.version||0)}</span></div>
    ${task.error?`<div class="alert err">构建错误：${escapeHtml(task.error)}</div>`:""}${renderToolCreatorQuestion(task)}${renderToolCreatorArtifact(task)}
    <h3>构建过程</h3>${renderToolCreatorEvents(detail.events)}
    <div class="row tool-creator-actions">${canApprove?'<button class="btn primary" data-tool-creator-approve>批准并启用</button>':''}${canCancel?'<button class="btn danger" data-tool-creator-cancel>取消任务</button>':''}${canRetry?'<button class="btn" data-tool-creator-retry>重新构建</button>':''}</div>
    ${state.toolCreatorDiagnostic?`<div style="margin-top:12px">${renderOperationDiagnostic(state.toolCreatorDiagnostic)}</div>`:""}
  </div>`;
}

function renderToolCreator() {
  return `<div class="tool-creator-intro"><div><span class="eyebrow">NATURAL LANGUAGE → SKILL</span><h1>创建工具</h1><p>描述想要的能力。LLM 会规划、搜索、整合并生成受限的声明式 Skill；关键内容无法决定时会暂停询问，发布前始终需要创建者批准。</p></div></div>
  <div class="card tool-creator-compose"><label for="tool-creator-request">工具需求</label><textarea id="tool-creator-request" placeholder="例如：创建一个查询某游戏近期官方更新并附来源的工具" oninput="state.toolCreatorRequest=this.value">${escapeHtml(state.toolCreatorRequest||"")}</textarea><div class="row"><input id="tool-creator-name" placeholder="可选英文名称，如 game_updates" value="${escapeAttr(state.toolCreatorSuggestedName||"")}" oninput="state.toolCreatorSuggestedName=this.value"><button class="btn primary" data-tool-creator-create ${state.toolCreatorBusy?'disabled':''}>${state.toolCreatorBusy?'创建中…':'开始构建'}</button></div><small class="muted">首版只组合现有只读能力，不生成或执行任意 Python。</small></div>
  <div class="tool-creator-workspace">${renderToolCreatorTaskList()}${renderToolCreatorDetail()}</div>`;
}

async function refreshToolCreator() {
  const data=await api("/tool-creator/tasks?limit=40");
  state.toolCreatorTasks=data.tasks||[];
  if(state.toolCreatorSelectedId)state.toolCreatorDetail=await api("/tool-creator/tasks/"+encodeURIComponent(state.toolCreatorSelectedId)).catch(()=>null);
  render();startToolCreatorPolling();
}

function stopToolCreatorPolling(){if(_toolCreatorTimer){clearTimeout(_toolCreatorTimer);_toolCreatorTimer=0;}}
function startToolCreatorPolling(){stopToolCreatorPolling();if(state.view!=="tool_creator")return;const task=state.toolCreatorDetail&&state.toolCreatorDetail.task;if(!task||!TOOL_CREATOR_ACTIVE.has(task.status))return;_toolCreatorTimer=setTimeout(()=>refreshToolCreator().catch(()=>startToolCreatorPolling()),1500);}

async function createToolCreatorTask(){
  const request=document.getElementById("tool-creator-request")?.value.trim()||"";const suggested=document.getElementById("tool-creator-name")?.value.trim()||"";
  if(!request){alertFlash("err","请先描述工具需求");return;}state.toolCreatorBusy=true;render();
  try{const result=await api("/tool-creator/tasks",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({request,suggested_name:suggested})});state.toolCreatorRequest="";state.toolCreatorSuggestedName="";state.toolCreatorSelectedId=result.task.task_id;await refreshToolCreator();}
  catch(e){state.toolCreatorDiagnostic=operationDiagnosticFromError(e,"创建工具任务未建立");alertFlash("err",state.toolCreatorDiagnostic.title);}
  finally{state.toolCreatorBusy=false;render();startToolCreatorPolling();}
}

async function toolCreatorAction(action,payload={}){const task=state.toolCreatorDetail&&state.toolCreatorDetail.task;if(!task)return;state.toolCreatorBusy=true;render();try{const result=await api(`/tool-creator/tasks/${encodeURIComponent(task.task_id)}/${action}`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({expected_version:task.version,...payload})});state.toolCreatorDiagnostic=result.diagnostic||null;state.toolCreatorAnswer="";await refreshToolCreator();}catch(e){state.toolCreatorDiagnostic=operationDiagnosticFromError(e,"工具创建操作未完成");alertFlash("err",state.toolCreatorDiagnostic.title);}finally{state.toolCreatorBusy=false;render();startToolCreatorPolling();}}

if(!window.__personificationToolCreatorEvents){
  window.__personificationToolCreatorEvents=true;
  document.addEventListener("click",event=>{
    const el=event.target instanceof Element?event.target.closest("[data-tool-creator-task],[data-tool-creator-refresh],[data-tool-creator-create],[data-tool-creator-answer],[data-tool-creator-answer-option],[data-tool-creator-approve],[data-tool-creator-cancel],[data-tool-creator-retry]"):null;
    if(!el)return;
    if(el.hasAttribute("data-tool-creator-task")){state.toolCreatorSelectedId=el.getAttribute("data-tool-creator-task")||"";refreshToolCreator();return;}
    if(el.hasAttribute("data-tool-creator-refresh")){refreshToolCreator();return;}
    if(el.hasAttribute("data-tool-creator-create")){createToolCreatorTask();return;}
    if(el.hasAttribute("data-tool-creator-answer-option")){state.toolCreatorAnswer=el.getAttribute("data-tool-creator-answer-option")||"";render();return;}
    const task=state.toolCreatorDetail&&state.toolCreatorDetail.task;if(!task)return;
    if(el.hasAttribute("data-tool-creator-answer")){const answer=document.getElementById("tool-creator-answer")?.value.trim()||"";if(answer)toolCreatorAction("answer",{question_id:task.question.question_id,answer});return;}
    if(el.hasAttribute("data-tool-creator-approve")){if(confirm("确认发布并启用这个声明式 Skill？"))toolCreatorAction("approve",{artifact_digest:task.artifact_digest});return;}
    if(el.hasAttribute("data-tool-creator-cancel")){if(confirm("确认取消这个创建任务？"))toolCreatorAction("cancel");return;}
    if(el.hasAttribute("data-tool-creator-retry")){toolCreatorAction("retry");}
  });
}
