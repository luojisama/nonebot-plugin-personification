function renderPersonas() {
  if (state.personasAvailable === false) return `<div class="card muted">profile_service 未就绪</div>`;
  if (state.selectedPersona) return renderPersonaDetail();
  const rows = state.personas.map(p => `<tr>
    <td class="col-avatar"><img class="avatar" src="${escapeAttr(p.avatar_url || `https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(p.user_id)}&spec=100`)}" alt="" loading="lazy" referrerpolicy="no-referrer"></td>
    <td class="col-id"><code class="u-atomic u-tabular">${escapeHtml(p.user_id)}</code></td>
    <td class="col-model"><span class="u-clamp-2" title="${escapeAttr(p.nickname || '')}">${escapeHtml(p.nickname || '')}</span></td>
    <td class="col-status">${renderFavorabilityBadge(p.favorability)}</td>
    <td class="col-summary u-wrap">${escapeHtml(p.snippet)}</td>
    <td class="col-date u-atomic u-tabular">${p.updated_at ? new Date(p.updated_at*1000).toLocaleDateString() : '-'}</td>
    <td class="col-actions"><button class="btn small" aria-label="查看 QQ ${escapeAttr(p.user_id)} 的用户画像" onclick="openPersona('${escapeAttr(p.user_id)}')">详情</button></td>
  </tr>`).join("");
  return `<div class="card"><h2>用户画像（${state.personas.length}）</h2>
    <div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户画像列表"><table class="data-table xwide"><thead><tr><th scope="col" class="col-avatar"><span class="sr-only">头像</span></th><th scope="col" class="col-id">QQ</th><th scope="col" class="col-model">昵称</th><th scope="col" class="col-status">好感度</th><th scope="col" class="col-summary">摘要</th><th scope="col" class="col-date">更新</th><th scope="col" class="col-actions"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows||'<tr><td colspan="7" class="muted">暂无画像</td></tr>'}</tbody></table></div></div>`;
}


function personaDetailPath(uid) {
  const groupId=String(state.personaScopeGroupId||"").trim();
  const suffix=groupId?`?group_id=${encodeURIComponent(groupId)}`:"";
  return "/personas/"+encodeURIComponent(uid)+suffix;
}

async function loadPersonaDetail(uid) {
  const requestId=++state.personaScopeRequestId;
  const requestedGroup=String(state.personaScopeGroupId||"");
  const result=await api(personaDetailPath(uid));
  if(requestId!==state.personaScopeRequestId||requestedGroup!==String(state.personaScopeGroupId||""))return false;
  state.selectedPersona=result;
  return true;
}

async function openPersona(uid, preserveScope=false) {
  try {
    if(!preserveScope)state.personaScopeGroupId="";
    if(await loadPersonaDetail(uid))render();
  } catch (e) { alertFlash("err", e.message); }
}

function selectPersonaScopeBot(botId) {
  state.personaScopeRequestId+=1;
  state.personaScopeBotId=String(botId||"");
  state.personaScopeGroupId="";
  render();
}

async function selectPersonaScopeGroup(uid, groupId) {
  state.personaScopeGroupId=String(groupId||"");
  await openPersona(uid,true);
}

async function refreshScopedProfile(uid) {
  const botId=String(state.personaScopeBotId||"").trim();
  const groupId=String(state.personaScopeGroupId||"").trim();
  if(!botId||!groupId){alertFlash("err","请先选择在线 Bot 和目标群");return;}
  if(!confirm(`重新分析用户 ${uid} 在群 ${groupId} 的差异画像？`))return;
  state.scopedProfileBusy=true;render();
  try{
    const result=await api(`/personas/${encodeURIComponent(uid)}/group-refresh`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({bot_id:botId,group_id:groupId})});
    alertFlash(result.partial?"info":(result.status==="succeeded"?"ok":"info"),result.status==="succeeded"?"群内画像已刷新":`群内画像未更新：${result.code||result.status}`);
    await loadPersonaDetail(uid);
  }catch(e){alertFlash("err",e.message||"群内画像刷新失败");}
  finally{state.scopedProfileBusy=false;render();}
}

function renderQqProfileCard(core, userId) {
  const meta = (core && core.qq_profile) || {};
  const safeAvatar = safeHttpUrl(meta.avatar_url);
  const avatar = safeAvatar || `https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(userId)}&spec=640`;
  const homepage = safeHttpUrl(meta.homepage_url);
  const atomicFields = new Set(["性别", "年龄", "QID", "等级", "登录天数", "群角色", "专属头衔"]);
  const rows = [
    ["昵称", meta.nickname],
    ["群名片", meta.card],
    ["备注", meta.remark],
    ["性别", meta.sex],
    ["年龄", meta.age],
    ["QID", meta.qid],
    ["等级", meta.level],
    ["登录天数", meta.login_days],
    ["地区", meta.area],
    ["群角色", meta.role],
    ["专属头衔", meta.title],
    ["个性签名", meta.signature],
  ].filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== "")
   .map(([k, v]) => `<tr><th scope="row" class="muted u-atomic">${escapeHtml(k)}</th><td class="col-description"><span class="${atomicFields.has(k) ? "u-atomic" : "u-wrap"}" title="${escapeAttr(String(v))}">${escapeHtml(String(v))}</span></td></tr>`).join("");
  return `<div class="card">
    <h2>QQ 资料快照</h2>
    <div class="qq-profile-card">
      <img class="qq-profile-avatar" src="${escapeAttr(avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">
      <div class="qq-profile-body">
        ${rows ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="QQ 资料快照"><table class="data-table compact"><tbody>${rows}</tbody></table></div>` : '<p class="muted">暂无协议资料字段。</p>'}
        <div class="qq-profile-links">
          ${safeAvatar ? `<a class="btn small" href="${escapeAttr(safeAvatar)}" target="_blank" rel="noopener noreferrer">查看头像</a>` : ''}
          ${homepage ? `<a class="btn small" href="${escapeAttr(homepage)}" target="_blank" rel="noreferrer">打开主页</a>` : ''}
        </div>
      </div>
    </div>
  </div>`;
}

function renderAvatarInsightCard(core, userId) {
  const analysis = (core && core.avatar_analysis) || {};
  const insight = (core && core.avatar_insight) || {};
  const status = String(analysis.status || "");
  const statusMap = {success:"分析成功", unchanged:"头像未变化", failed:"分析失败"};
  const kindMap = {real_person:"真人头像", illustration:"插画", acg_character:"ACG 角色", logo:"Logo", other:"其他", unknown:"无法判断"};
  const analyzedAt = Number(analysis.analyzed_at || 0);
  const checkedAt = Number(analysis.checked_at || 0);
  const atomicFields = new Set(["状态", "内容 hash", "最近检查", "最近成功分析", "视觉 route", "头像类型", "主体数", "包含文字", "置信度"]);
  const rows = [
    ["状态", statusMap[status] || "尚未分析"],
    ["内容 hash", analysis.content_hash_short || "—"],
    ["最近检查", checkedAt ? new Date(checkedAt * 1000).toLocaleString() : "—"],
    ["最近成功分析", analyzedAt ? new Date(analyzedAt * 1000).toLocaleString() : "—"],
    ["视觉 route", analysis.route || "—"],
    ["头像类型", kindMap[insight.asset_kind] || insight.asset_kind || "—"],
    ["主体数", insight.subject_count],
    ["中性摘要", insight.neutral_summary],
    ["ACG 候选", Array.isArray(insight.acg_candidates) ? insight.acg_candidates.join("、") : ""],
    ["包含文字", insight.contains_text === true ? "是" : (insight.contains_text === false ? "否" : "")],
    ["置信度", insight.confidence === undefined ? "" : Number(insight.confidence).toFixed(2)],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "")
   .map(([key, value]) => `<tr><th scope="row" class="muted u-atomic">${escapeHtml(key)}</th><td class="col-description"><span class="${atomicFields.has(key) ? "u-ellipsis" : "u-wrap"}" title="${escapeAttr(String(value))}">${escapeHtml(String(value))}</span></td></tr>`).join("");
  const busy = String(state.avatarAnalysisBusy || "").endsWith(`:${userId}`);
  return `<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap">
      <h2 style="margin:0">头像长期画像</h2>
      <div class="row" style="gap:8px">
        <button class="btn small primary" onclick="refreshAvatarAnalysis('${escapeAttr(userId)}')" ${busy?'disabled':''}>${state.avatarAnalysisBusy===`refresh:${userId}`?'<span class="spinner"></span> 排队中…':'重新分析'}</button>
        <button class="btn small danger" onclick="clearAvatarAnalysis('${escapeAttr(userId)}')" ${busy?'disabled':''}>${state.avatarAnalysisBusy===`clear:${userId}`?'<span class="spinner"></span> 删除中…':'删除分析'}</button>
      </div>
    </div>
    ${rows ? `<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="头像长期画像"><table class="data-table compact" style="margin-top:12px"><tbody>${rows}</tbody></table></div>` : '<p class="muted">暂无持久化头像分析。</p>'}
    <p class="muted" style="font-size:11px;margin-top:8px">真人头像只保留“真人头像”类型；不会保存头像 bytes、data URL 或模型 raw response。</p>
  </div>`;
}

function renderPersonaDetail() {
  const p = state.selectedPersona;
  const core = p.core_profile;
  const claimLabels={gender:"性别",age_group:"年龄段",occupation:"职业",portrait:"人物描述",interests:"兴趣",routine:"作息",communication_style:"沟通风格",emotion_baseline:"情绪基线",social_mode:"社交模式",knowledge:"知识结构",relationship:"关系",taboos:"雷区",memory_anchors:"记忆锚点",recent_focus:"近期关注",content_pref:"内容偏好",nickname_pref:"称呼偏好",interaction_advice:"互动建议",group_role:"本群角色"};
  const renderClaims=claims=>(claims||[]).map(claim=>`<tr><th scope="row" class="u-atomic">${escapeHtml(claimLabels[claim.key]||claim.key||"")}</th><td class="col-description u-wrap">${escapeHtml(claim.value||"")}</td><td class="col-status u-atomic">${escapeHtml(claim.source||"")}</td><td class="col-number u-atomic u-tabular">${Number(claim.confidence||0).toFixed(2)}</td></tr>`).join("");
  const locals = (p.local_profiles || []).map(lp => {const claimRows=renderClaims(lp.effective_claims);return `<div class="card" style="background:#0e1117">
    <div class="between"><strong>群 ${escapeHtml(lp.group_id)}</strong><span class="muted" style="font-size:12px">${new Date(lp.updated_at*1000).toLocaleString()}</span></div>
    ${claimRows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="群内 effective claims"><table class="data-table compact" style="margin-top:8px"><thead><tr><th scope="col">字段</th><th scope="col">值</th><th scope="col">来源</th><th scope="col">置信度</th></tr></thead><tbody>${claimRows}</tbody></table></div>`:`<pre class="u-pre-wrap code-scroll" style="margin:6px 0 0;font-family:inherit">${escapeHtml(lp.profile_text||"")}</pre>`}
  </div>`;}).join("");
  const structured = (core && core.structured) || {};
  const corr = (core && core.user_corrections) || {};
  const structRows = Object.keys(structured).map(k => `<tr>
      <th scope="row" class="u-atomic">${escapeHtml(claimLabels[k]||k)}${corr[claimLabels[k]]||corr[k]?' <span class="device-status approved">已更正</span>':''}</th>
      <td class="col-description u-wrap">${escapeHtml(String(structured[k]))}</td>
    </tr>`).join("");
  const structCard = `<div class="card"><h2>结构化字段（持久保存）</h2>
    ${structRows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户画像结构化字段"><table class="data-table compact"><tbody>${structRows}</tbody></table></div>`:'<p class="muted">暂无结构化字段</p>'}
    <div class="field-input" style="margin-top:12px">
      <input id="corr-field" type="text" placeholder="字段（如 性别/职业）" style="max-width:160px">
      <input id="corr-value" type="text" placeholder="更正为…" style="max-width:220px">
      <button class="btn small primary" onclick="submitCorrection('${escapeAttr(p.user_id)}')">提交更正</button>
    </div>
    <p class="muted" style="font-size:11px;margin-top:6px">用户确认的画像事实会保留到后续重生成，但只作为背景数据，不构成模型指令。</p>
  </div>`;
  const bots=(state.qqInfo&&state.qqInfo.bots)||[];
  const botOptions=bots.map(item=>{const id=String(item.bot_id||"");return `<option value="${escapeAttr(id)}" ${id===state.personaScopeBotId?"selected":""}>QQ ${escapeHtml(id)}</option>`}).join("");
  const scopedGroups=(state.qqGroups||[]).filter(item=>(item.bot_self_ids||[]).map(String).includes(String(state.personaScopeBotId||"")));
  const groupOptions=scopedGroups.map(item=>{const id=String(item.group_id||"");const label=item.group_name?`${item.group_name} (${id})`:id;return `<option value="${escapeAttr(id)}" ${id===state.personaScopeGroupId?"selected":""}>${escapeHtml(label)}</option>`}).join("");
  const effectiveRows=state.personaScopeGroupId?renderClaims(p.effective_claims):"";
  const scopedCard=`<div class="card"><div class="between" style="gap:12px;flex-wrap:wrap"><h2 style="margin:0">当前群差异画像</h2><button class="btn small primary" onclick="refreshScopedProfile('${escapeAttr(p.user_id)}')" ${!state.personaScopeBotId||!state.personaScopeGroupId||state.scopedProfileBusy?"disabled":""}>${state.scopedProfileBusy?'<span class="spinner"></span> 刷新中…':'重新分析'}</button></div>
    <div class="field-input" style="margin-top:12px"><label>Bot <select onchange="selectPersonaScopeBot(this.value)">${botOptions||'<option value="">无在线 Bot</option>'}</select></label><label>群 <select onchange="selectPersonaScopeGroup('${escapeAttr(p.user_id)}',this.value)"><option value="">选择目标群</option>${groupOptions}</select></label></div>
    ${state.personaScopeGroupId?(effectiveRows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="当前群 effective claims"><table class="data-table compact" style="margin-top:12px"><thead><tr><th scope="col">字段</th><th scope="col">值</th><th scope="col">来源</th><th scope="col">置信度</th></tr></thead><tbody>${effectiveRows}</tbody></table></div>`:'<p class="muted">当前群暂无差异画像，可手动重新分析。</p>'):'<p class="muted">选择明确的在线 Bot 与目标群后，可查看 effective claims 或触发安全刷新。</p>'}</div>`;
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedPersona=null;render()">返回列表</button><span class="muted">用户 ${escapeHtml(p.user_id)}</span></div>
    ${renderAdminOperations("persona","画像操作诊断")}
    ${renderFavorabilityCard(p.favorability, "用户好感度")}
    ${renderQqProfileCard(core, p.user_id)}
    ${renderAvatarInsightCard(core, p.user_id)}
    <div class="card"><h2>全局印象</h2>${core && core.profile_text ? `<pre class="u-pre-wrap code-scroll" style="margin:0;font-family:inherit">${escapeHtml(core.profile_text || '')}</pre>` : '<p class="muted">无全局画像</p>'}</div>
    ${structCard}
    ${scopedCard}
    <h3 style="margin-bottom:10px">各群印象（${(p.local_profiles||[]).length}）</h3>
    ${locals || '<p class="muted">无各群画像</p>'}`;
}

function userPolicyTierLabel(value) {
  return ({allow:"允许",level_1:"Level 1 · 12 小时",level_2:"Level 2 · 24 小时",permanent:"自动永久 Blacklist",manual_block:"管理员 Blacklist",manual_allow:"管理员允许"})[String(value||"")] || String(value||"未知");
}

function userPolicyExpiry(stateValue) {
  const item=stateValue||{};
  const manual=String(item.manual_mode||"");
  const raw=(manual==="block"||manual==="allow")?Number(item.manual_expires_at||0):Number(item.auto_expires_at||0);
  return raw>0?new Date(raw*1000).toLocaleString():"永久 / 无到期";
}

function userPolicyFriendName(friend) {
  const item=friend||{};
  return String(item.remark||item.nickname||"未命名好友");
}

async function setUserPolicyBot(value) {
  state.userPolicyBotId=String(value||"");
  state.userPolicyFriends=[];
  state.userPolicyFriendError="";
  if(!state.userPolicyBotId){render();return;}
  state.userPolicyBusy=true;render();
  try{
    const result=await api("/qq/friends?bot_id="+encodeURIComponent(state.userPolicyBotId),{cache:"no-store"});
    state.userPolicyFriends=result.friends||[];
  }catch(e){
    state.userPolicyFriendError="好友列表读取失败，仍可手工输入 QQ。";
    alertFlash("err",e.message||"好友列表读取失败");
  }finally{state.userPolicyBusy=false;render();}
}

function selectUserPolicyFriend(value) {
  state.userPolicyDraftUserId=String(value||"");
  render();
}

function updateUserPolicyDraftUserId(input) {
  const value=String(input&&input.value||"").replace(/\D/g,"").slice(0,20);
  if(input)input.value=value;
  state.userPolicyDraftUserId=value;
}

async function addUserPolicyBlacklist() {
  const uid=String(state.userPolicyDraftUserId||"").trim();
  const hours=Math.max(0,Math.min(8760,Number(document.getElementById("user-policy-add-hours")?.value||0)));
  if(!/^[1-9][0-9]{4,19}$/.test(uid)){alertFlash("err","请输入 5～20 位、且不以 0 开头的 QQ 号");return;}
  const duration=hours>0?`${hours} 小时`:"永久";
  if(!confirm(`确认将用户 ${uid} 加入 ${duration} Blacklist？`))return;
  state.userPolicyBusy=true;render();
  try{
    const detail=await api(`/user-policy/${encodeURIComponent(uid)}/events?include_evidence=false&limit=1`,{cache:"no-store"});
    await api(`/user-policy/${encodeURIComponent(uid)}/override`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({
        mode:"block",
        expected_revision:Number((detail.state||{}).revision||0),
        expires_at:hours>0?Date.now()/1000+hours*3600:0,
        reason_code:"webui_blacklist_add",
      }),
    });
    state.userPolicyDraftUserId="";
    state.userPolicyDurationHours=0;
    state.userPolicyTier="blocked";
    await reloadUserPolicyList();
    alertFlash("ok",`用户 ${uid} 已加入 Blacklist`);
  }catch(e){alertFlash("err",e.message||"加入 Blacklist 失败");}
  finally{state.userPolicyBusy=false;render();}
}

function currentUserPolicyState(userId) {
  const uid=String(userId||"");
  const selected=state.selectedUserPolicy&&state.selectedUserPolicy.state;
  if(selected&&String(selected.user_id||state.selectedUserPolicy.user_id||"")===uid)return selected;
  return ((state.userPolicy&&state.userPolicy.states)||[]).find(item=>String(item.user_id||"")===uid)||null;
}

async function unblockUserPolicy(userId) {
  const uid=String(userId||"");
  let current=currentUserPolicyState(uid);
  if(!current)return;
  if(!confirm(`确认解除用户 ${uid} 的当前 Blacklist？管理员 block 与自动 strikes 都会清除。`))return;
  state.userPolicyBusy=true;render();
  try{
    if(String(current.manual_mode||"")==="block"){
      const result=await api(`/user-policy/${encodeURIComponent(uid)}/override`,{
        method:"POST",headers:{"content-type":"application/json"},
        body:JSON.stringify({mode:"inherit",expected_revision:Number(current.revision||0),expires_at:0,reason_code:"webui_blacklist_unblock"}),
      });
      current=result.state||current;
    }
    if(Boolean(current.blocked)){
      const result=await api(`/user-policy/${encodeURIComponent(uid)}/clear-auto`,{
        method:"POST",headers:{"content-type":"application/json"},
        body:JSON.stringify({expected_revision:Number(current.revision||0)}),
      });
      current=result.state||current;
    }
    await reloadUserPolicyList();
    if(state.selectedUserPolicy&&String(state.selectedUserPolicy.user_id||"")===uid){
      state.selectedUserPolicy=await api(`/user-policy/${encodeURIComponent(uid)}/events?include_evidence=true&limit=150`,{cache:"no-store"});
    }
    alertFlash("ok",`用户 ${uid} 已解除 Blacklist`);
  }catch(e){alertFlash("err",e.message||"解除 Blacklist 失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function reloadUserPolicyList() {
  const qs=new URLSearchParams({limit:String(state.userPolicyLimit||50)});
  if(state.userPolicyTier)qs.set("tier",state.userPolicyTier);
  state.userPolicy=await api("/user-policy/states?"+qs.toString(),{cache:"no-store"});
}

async function setUserPolicyTier(value) {
  state.userPolicyTier=String(value||"");
  state.userPolicyLimit=50;
  state.selectedUserPolicy=null;
  try{await reloadUserPolicyList();render();}catch(e){alertFlash("err",e.message||"用户策略读取失败");}
}

async function loadMoreUserPolicies(){
  if(state.userPolicyBusy)return;
  state.userPolicyLimit=Math.min(500,Number(state.userPolicyLimit||50)+50);
  state.userPolicyBusy=true;render();
  try{await reloadUserPolicyList();}catch(e){alertFlash("err",e.message||"用户策略读取失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function openUserPolicy(userId) {
  const uid=String(userId||"");
  state.userPolicyBusy=true;render();
  try{
    state.selectedUserPolicy=await api(`/user-policy/${encodeURIComponent(uid)}/events?include_evidence=true&limit=150`,{cache:"no-store"});
  }catch(e){alertFlash("err",e.message||"策略详情读取失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function applyUserPolicyOverride(userId) {
  const uid=String(userId||"");
  const current=(state.selectedUserPolicy&&state.selectedUserPolicy.state)||{};
  const mode=String(document.getElementById("user-policy-mode")?.value||"inherit");
  const durationHours=Math.max(0,Number(document.getElementById("user-policy-hours")?.value||0));
  const expiresAt=(mode==="inherit"||durationHours<=0)?0:(Date.now()/1000+durationHours*3600);
  if(mode==="block"&&expiresAt===0&&!confirm(`永久 Blacklist 用户 ${uid}？`))return;
  state.userPolicyBusy=true;render();
  try{
    const result=await api(`/user-policy/${encodeURIComponent(uid)}/override`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({mode,expected_revision:Number(current.revision||0),expires_at:expiresAt,reason_code:"webui_manual_override"}),
    });
    state.selectedUserPolicy={...(state.selectedUserPolicy||{}),state:result.state};
    await reloadUserPolicyList();
    alertFlash("ok","用户策略已更新");
  }catch(e){alertFlash("err",e.message||"用户策略更新失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function clearUserPolicyAuto(userId) {
  const uid=String(userId||"");
  const current=(state.selectedUserPolicy&&state.selectedUserPolicy.state)||{};
  if(!confirm(`清除用户 ${uid} 的自动 strikes 与自动 tier history？管理员 override 不受影响。`))return;
  state.userPolicyBusy=true;render();
  try{
    const result=await api(`/user-policy/${encodeURIComponent(uid)}/clear-auto`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({expected_revision:Number(current.revision||0)}),
    });
    state.selectedUserPolicy={...(state.selectedUserPolicy||{}),state:result.state};
    await reloadUserPolicyList();
    alertFlash("ok","自动 strikes 已清除");
  }catch(e){alertFlash("err",e.message||"自动状态清除失败");}
  finally{state.userPolicyBusy=false;render();}
}

async function purgeUserPolicyProfile(userId) {
  const uid=String(userId||"");
  const current=(state.selectedUserPolicy&&state.selectedUserPolicy.state)||{};
  const expected=`PURGE PROFILE ${uid}`;
  const typed=window.prompt(`这是不可逆操作，将删除该用户所有全局/群内画像、Persona history、Memory、关系边与头像 visual evidence。\n\n请输入：${expected}`)||"";
  if(typed!==expected){if(typed)alertFlash("err","确认串不匹配，未执行清除");return;}
  state.userPolicyBusy=true;render();
  try{
    const result=await api(`/user-policy/${encodeURIComponent(uid)}/profile`,{
      method:"DELETE",headers:{"content-type":"application/json"},
      body:JSON.stringify({expected_revision:Number(current.revision||0),confirmation:typed}),
    });
    state.selectedUserPolicy={...(state.selectedUserPolicy||{}),state:result.state,purge_counts:result.counts};
    const total=Object.values(result.counts||{}).reduce((sum,value)=>sum+Number(value||0),0);
    alertFlash("ok",`用户画像数据已不可逆清除，共移除 ${total} 项；Policy state 保留。`);
  }catch(e){alertFlash("err",e.message||"用户画像清除失败");}
  finally{state.userPolicyBusy=false;render();}
}

function renderUserPolicyDetail() {
  const detail=state.selectedUserPolicy;
  if(!detail)return "";
  const item=detail.state||{};
  const uid=String(detail.user_id||item.user_id||"");
  const events=(detail.events||[]).map(event=>{
    const evidence=String(event.evidence_excerpt||"").trim();
    const when=event.created_at?new Date(Number(event.created_at)*1000).toLocaleString():"-";
    return `<tr>
      <td class="col-time u-atomic u-tabular">${escapeHtml(when)}</td>
      <td class="col-status u-atomic">${escapeHtml(event.event_kind||"")}</td>
      <td class="col-status u-atomic">${escapeHtml(event.verdict||event.reason_code||"")}</td>
      <td class="col-description u-wrap">${escapeHtml([event.category,event.intent,event.severity].filter(Boolean).join(" / "))}</td>
      <td class="col-number u-tabular">${Number(event.confidence||0).toFixed(2)}</td>
      <td class="col-description u-wrap">${evidence?escapeHtml(evidence):'<span class="muted">无 / 已到期</span>'}</td>
    </tr>`;
  }).join("");
  return `<div class="row" style="margin-bottom:10px"><button class="btn small" onclick="state.selectedUserPolicy=null;render()">返回列表</button><span class="muted">用户 ${escapeHtml(uid)}</span></div>
    <div class="card">
      <div class="between" style="gap:12px;flex-wrap:wrap"><h2 style="margin:0">策略状态</h2><span class="tag tag--status">revision ${Number(item.revision||0)}</span></div>
      <div class="row" style="gap:24px;margin-top:12px">
        <div><div class="muted">effective tier</div><strong>${escapeHtml(userPolicyTierLabel(item.effective_tier))}</strong></div>
        <div><div class="muted">到期</div><span class="u-tabular">${escapeHtml(userPolicyExpiry(item))}</span></div>
        <div><div class="muted">auto stage / strikes</div><span class="u-tabular">${Number(item.auto_stage||0)} / ${Number(item.violation_count||0)}</span></div>
        <div><div class="muted">最近更新</div><span class="u-tabular">${item.updated_at?escapeHtml(new Date(Number(item.updated_at)*1000).toLocaleString()):"-"}</span></div>
      </div>
      <div class="field-input" style="margin-top:14px">
        <label>管理员策略 <select id="user-policy-mode"><option value="inherit" ${item.manual_mode==="inherit"?'selected':''}>inherit（沿用自动状态）</option><option value="block" ${item.manual_mode==="block"?'selected':''}>block</option><option value="allow" ${item.manual_mode==="allow"?'selected':''}>allow</option></select></label>
        <label>临时小时数 <input id="user-policy-hours" type="number" min="0" max="8760" step="1" value="0" style="max-width:120px"></label>
        <button class="btn small primary" onclick="applyUserPolicyOverride('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>保存 override</button>
        ${item.blocked?`<button class="btn small danger" onclick="unblockUserPolicy('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>解除 Blacklist</button>`:""}
        <button class="btn small" onclick="clearUserPolicyAuto('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>清除自动 strikes</button>
        <button class="btn small danger" onclick="purgeUserPolicyProfile('${escapeAttr(uid)}')" ${state.userPolicyBusy?'disabled':''}>彻底清除画像</button>
      </div>
      <p class="muted" style="font-size:11px">小时数为 0 时 block/allow 为永久 override；彻底清除画像不会删除 Policy state 或事件。所有写操作均使用当前 revision 防并发覆盖。</p>
    </div>
    ${detail.purge_counts?`<div class="card"><h2>最近清除结果</h2><pre class="u-pre-wrap">${escapeHtml(JSON.stringify(detail.purge_counts,null,2))}</pre></div>`:""}
    <div class="card"><h2>事件时间线</h2>
      <p class="muted">evidence 短摘只在此管理员详情请求中解密，页面不会写入浏览器持久存储；服务端 ciphertext 到期后自动删除。</p>
      ${events?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户策略事件"><table class="data-table wide"><thead><tr><th scope="col">时间</th><th scope="col">类型</th><th scope="col">判定</th><th scope="col">分类</th><th scope="col">置信度</th><th scope="col">evidence 短摘</th></tr></thead><tbody>${events}</tbody></table></div>`:'<p class="muted">暂无策略事件。</p>'}
    </div>`;
}

function renderUserPolicyAdd() {
  const bots=(state.userPolicyBotInfo&&state.userPolicyBotInfo.bots)||[];
  const botOptions=bots.map(item=>{const id=String(item.bot_id||"");return `<option value="${escapeAttr(id)}" ${id===state.userPolicyBotId?'selected':''}>QQ ${escapeHtml(id)}</option>`;}).join("");
  const friends=(state.userPolicyFriends||[]).filter(item=>/^[1-9][0-9]{4,19}$/.test(String(item.user_id||"")));
  const friendOptions=friends.map(item=>{const uid=String(item.user_id||"");return `<option value="${escapeAttr(uid)}" ${uid===state.userPolicyDraftUserId?'selected':''}>${escapeHtml(userPolicyFriendName(item))} · ${escapeHtml(uid)}</option>`;}).join("");
  return `<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap"><div><h2 style="margin:0">添加 Blacklist</h2><p class="muted" style="margin:6px 0 0">可从指定 Bot 的好友中选取，也可直接输入 QQ 号；QQ 只作为全局策略标识保存。</p></div><span class="tag tag--status">管理员操作</span></div>
    <div class="field-input" style="margin-top:14px">
      <label>好友来源 Bot <select onchange="setUserPolicyBot(this.value)" ${state.userPolicyBusy||!botOptions?'disabled':''}>${botOptions||'<option value="">无已连接 Bot</option>'}</select></label>
      <label>选择好友 <select onchange="selectUserPolicyFriend(this.value)" ${state.userPolicyBusy||!friendOptions?'disabled':''}><option value="">请选择好友</option>${friendOptions}</select></label>
      <label>QQ 号 <input inputmode="numeric" autocomplete="off" maxlength="20" value="${escapeAttr(state.userPolicyDraftUserId)}" placeholder="手工输入 5～20 位 QQ" oninput="updateUserPolicyDraftUserId(this)" ${state.userPolicyBusy?'disabled':''}></label>
      <label>临时小时数 <input id="user-policy-add-hours" type="number" min="0" max="8760" step="1" value="${escapeAttr(state.userPolicyDurationHours)}" oninput="state.userPolicyDurationHours=Math.max(0,Math.min(8760,Number(this.value||0)))" style="max-width:120px" ${state.userPolicyBusy?'disabled':''}></label>
      <button class="btn danger" onclick="addUserPolicyBlacklist()" ${state.userPolicyBusy?'disabled':''}>加入 Blacklist</button>
    </div>
    <p class="muted" style="font-size:11px">小时数为 0 表示永久管理员 Blacklist；好友列表不可用时不影响手工添加。加入、解除和详情修改均使用 revision 防止并发覆盖。</p>
    ${state.userPolicyFriendError?`<div class="alert err">${escapeHtml(state.userPolicyFriendError)}</div>`:""}
  </div>`;
}

function renderUserPolicy() {
  if(state.selectedUserPolicy)return renderUserPolicyDetail();
  const friendMap=new Map((state.userPolicyFriends||[]).map(item=>[String(item.user_id||""),userPolicyFriendName(item)]));
  const rows=((state.userPolicy&&state.userPolicy.states)||[]).map(item=>`<tr>
    <td class="col-id"><button class="btn small u-atomic u-tabular" onclick="openUserPolicy('${escapeAttr(item.user_id)}')">${escapeHtml(item.user_id)}</button>${friendMap.has(String(item.user_id||""))?`<div class="muted u-wrap" style="margin-top:4px">${escapeHtml(friendMap.get(String(item.user_id||"")))}</div>`:""}</td>
    <td class="col-status"><span class="tag tag--status">${escapeHtml(userPolicyTierLabel(item.effective_tier))}</span></td>
    <td class="col-time u-atomic u-tabular">${escapeHtml(userPolicyExpiry(item))}</td>
    <td class="col-number u-tabular">${Number(item.auto_stage||0)} / ${Number(item.violation_count||0)}</td>
    <td class="col-status u-atomic">${escapeHtml(item.manual_mode||"inherit")}</td>
    <td class="col-number u-tabular">${Number(item.revision||0)}</td>
    <td class="col-time u-atomic u-tabular">${item.updated_at?escapeHtml(new Date(Number(item.updated_at)*1000).toLocaleString()):"-"}</td>
    <td class="col-actions">${item.blocked?`<button class="btn small danger" onclick="unblockUserPolicy('${escapeAttr(item.user_id)}')" ${state.userPolicyBusy?'disabled':''}>解除</button>`:`<button class="btn small" onclick="openUserPolicy('${escapeAttr(item.user_id)}')">查看</button>`}</td>
  </tr>`).join("");
  const options=[["blocked","当前阻止"],["","全部策略记录"],["allow","允许"],["level_1","Level 1"],["level_2","Level 2"],["permanent","自动永久"],["manual_block","管理员 Blacklist"],["manual_allow","管理员允许"]].map(([value,label])=>`<option value="${escapeAttr(value)}" ${state.userPolicyTier===value?'selected':''}>${escapeHtml(label)}</option>`).join("");
  return `${renderUserPolicyAdd()}<div class="card">
    <div class="between" style="gap:12px;flex-wrap:wrap"><div><h2 style="margin:0">用户策略 / Blacklist</h2><p class="muted" style="margin:6px 0 0">全局 QQ 用户策略，跨群、私聊、Bot 身份与 QZone 生效。</p></div><label>tier <select onchange="setUserPolicyTier(this.value)">${options}</select></label></div>
    ${rows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="用户策略列表"><table class="data-table wide" style="margin-top:12px"><thead><tr><th scope="col">用户</th><th scope="col">effective tier</th><th scope="col">到期</th><th scope="col">stage / strikes</th><th scope="col">manual</th><th scope="col">revision</th><th scope="col">更新时间</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p class="muted">当前筛选下没有已持久化策略状态。</p>'}
    ${state.userPolicy?.has_more?`<div class="row" style="justify-content:center;margin-top:14px"><button class="btn" onclick="loadMoreUserPolicies()" ${state.userPolicyBusy?'disabled':''}>再加载 50 条</button></div>`:""}
  </div>`;
}

async function reloadOutbound() {
  const qs=new URLSearchParams({limit:String(state.outboundLimit||50)});
  if(state.outboundBotId)qs.set("bot_id",state.outboundBotId);
  if(state.outboundKind)qs.set("conversation_kind",state.outboundKind);
  if(state.outboundConversationId)qs.set("conversation_id",state.outboundConversationId);
  if(state.outboundStatus)qs.set("status",state.outboundStatus);
  if(state.outboundRecalled)qs.set("recalled",state.outboundRecalled);
  state.outbound=await api("/outbound/recent?"+qs.toString(),{cache:"no-store"});
}

async function applyOutboundFilters() {
  state.outboundBotId=String(document.getElementById("outbound-bot")?.value||"").trim();
  state.outboundKind=String(document.getElementById("outbound-kind")?.value||"").trim();
  state.outboundConversationId=String(document.getElementById("outbound-conversation")?.value||"").trim();
  state.outboundStatus=String(document.getElementById("outbound-status")?.value||"").trim();
  state.outboundRecalled=String(document.getElementById("outbound-recalled")?.value||"").trim();
  state.outboundLimit=50;
  state.outboundBusy=true;render();
  try{await reloadOutbound();}catch(e){alertFlash("err",e.message||"出站账本读取失败");}
  finally{state.outboundBusy=false;render();}
}

async function loadMoreOutbound(){
  if(state.outboundBusy)return;
  state.outboundLimit=Math.min(500,Number(state.outboundLimit||50)+50);
  state.outboundBusy=true;render();
  try{await reloadOutbound();}catch(e){alertFlash("err",e.message||"出站账本读取失败");}
  finally{state.outboundBusy=false;render();}
}

async function recallOutboundOperation(button) {
  const operationId=String(button?.dataset?.operationId||"");
  const botId=String(button?.dataset?.botId||"");
  const kind=String(button?.dataset?.conversationKind||"");
  const conversationId=String(button?.dataset?.conversationId||"");
  const expected=`RECALL ${operationId}`;
  const typed=window.prompt(`将撤回该 operation 的全部平台消息；服务端会重新验证 Bot、会话、窗口、完整性与未撤回状态。\n\n请输入：${expected}`)||"";
  if(typed!==expected){if(typed)alertFlash("err","确认串不匹配，未执行撤回");return;}
  state.outboundBusy=true;render();
  try{
    const result=await api(`/outbound/${encodeURIComponent(operationId)}/recall`,{
      method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({bot_id:botId,conversation_kind:kind,conversation_id:conversationId,confirmation:typed}),
    });
    const diagnostic=rememberAdminOperation("outbound",result,"Bot 消息撤回未完成");
    if(result.status==="succeeded")alertFlash("ok",diagnostic?.title||"Bot 消息撤回完成");
    else if(result.status==="unknown"||result.status==="partial")alertFlash("info",diagnostic?.title||"撤回结果需要人工核对");
    else alertFlash("err",diagnostic?.title||"Bot 消息撤回失败");
    await reloadOutbound();
  }catch(e){const diagnostic=rememberAdminOperation("outbound",e,"Bot 消息撤回未完成");alertFlash("err",diagnostic?.title||e.message||"Bot 消息撤回失败");}
  finally{state.outboundBusy=false;render();}
}

function renderOutbound() {
  const messages=(state.outbound&&state.outbound.messages)||[];
  const seenOperations=new Set();
  const rows=messages.map(item=>{
    const operationId=String(item.operation_id||"");
    const first=!seenOperations.has(operationId);
    seenOperations.add(operationId);
    const recalled=Number(item.recalled_at||0)>0;
    const attempted=Boolean(String(item.recall_status||""));
    const canRecall=first&&!recalled&&!attempted&&item.status==="sent";
    const action=canRecall?`<button class="btn small danger" data-operation-id="${escapeAttr(operationId)}" data-bot-id="${escapeAttr(item.bot_id||'')}" data-conversation-kind="${escapeAttr(item.conversation_kind||'')}" data-conversation-id="${escapeAttr(item.conversation_id||'')}" onclick="recallOutboundOperation(this)" ${state.outboundBusy?'disabled':''}>撤回 operation</button>`:(first?'<span class="muted">不可撤回 / 已尝试</span>':'<span class="muted">同 operation</span>');
    return `<tr>
      <td class="col-time u-atomic u-tabular">${item.created_at?escapeHtml(new Date(Number(item.created_at)*1000).toLocaleString()):"-"}</td>
      <td class="col-model"><code class="u-ellipsis" title="${escapeAttr(operationId)}">${escapeHtml(operationId)}</code><div class="muted u-tabular">part ${Number(item.part_index||0)}</div></td>
      <td class="col-status u-atomic">${escapeHtml(item.bot_id||"")}</td>
      <td class="col-model u-atomic">${escapeHtml(item.conversation_kind||"")} ${escapeHtml(item.conversation_id||"")}</td>
      <td class="col-status u-atomic">${escapeHtml(item.surface||"")}</td>
      <td class="col-model u-atomic u-tabular">${escapeHtml(item.message_id||"-")}</td>
      <td class="col-status"><span class="tag tag--status">${escapeHtml(recalled?"recalled":(item.recall_status||item.status||""))}</span></td>
      <td class="col-description u-wrap">${escapeHtml(item.preview||"")}</td>
      <td class="col-actions">${action}</td>
    </tr>`;
  }).join("");
  const diagnostics=renderAdminOperations("outbound","Bot 消息撤回诊断");
  return `${diagnostics}
    <div class="card">
      <div class="between" style="gap:12px;flex-wrap:wrap"><div><h2 style="margin:0">近期 Bot 消息</h2><p class="muted" style="margin:6px 0 0">只展示持久账本中的脱敏 preview；不会返回消息 payload、HMAC key 或 Secret。</p></div><button class="btn small" onclick="applyOutboundFilters()" ${state.outboundBusy?'disabled':''}>刷新</button></div>
      <div class="field-input" style="margin-top:12px">
        <input id="outbound-bot" type="text" placeholder="Bot ID" value="${escapeAttr(state.outboundBotId)}">
        <select id="outbound-kind"><option value="" ${!state.outboundKind?'selected':''}>全部会话</option><option value="group" ${state.outboundKind==='group'?'selected':''}>group</option><option value="private" ${state.outboundKind==='private'?'selected':''}>private</option></select>
        <input id="outbound-conversation" type="text" placeholder="群号 / QQ 号" value="${escapeAttr(state.outboundConversationId)}">
        <select id="outbound-status"><option value="" ${!state.outboundStatus?'selected':''}>全部发送状态</option><option value="sent" ${state.outboundStatus==='sent'?'selected':''}>sent</option><option value="unknown" ${state.outboundStatus==='unknown'?'selected':''}>unknown</option><option value="failed" ${state.outboundStatus==='failed'?'selected':''}>failed</option></select>
        <select id="outbound-recalled"><option value="" ${!state.outboundRecalled?'selected':''}>全部撤回状态</option><option value="false" ${state.outboundRecalled==='false'?'selected':''}>未撤回</option><option value="true" ${state.outboundRecalled==='true'?'selected':''}>已撤回</option></select>
        <button class="btn small primary" onclick="applyOutboundFilters()" ${state.outboundBusy?'disabled':''}>应用筛选</button>
      </div>
      ${rows?`<div class="table-wrap table-scroll" tabindex="0" role="region" aria-label="近期 Bot 出站消息"><table class="data-table xwide"><thead><tr><th scope="col">时间</th><th scope="col">operation / part</th><th scope="col">Bot</th><th scope="col">会话</th><th scope="col">surface</th><th scope="col">message_id</th><th scope="col">状态</th><th scope="col">脱敏 preview</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p class="muted">当前筛选下没有出站账本记录。</p>'}
      ${state.outbound?.has_more?`<div class="row" style="justify-content:center;margin-top:14px"><button class="btn" onclick="loadMoreOutbound()" ${state.outboundBusy?'disabled':''}>再加载 50 条</button></div>`:""}
      <p class="muted" style="font-size:11px">管理员只能选择完整 operation；前端不会提交任意 message_id。unknown/partial/已尝试 operation 会被服务端封存，禁止自动重试。</p>
    </div>`;
}

async function submitCorrection(uid) {
  const field = (document.getElementById("corr-field")?.value||"").trim();
  const value = (document.getElementById("corr-value")?.value||"").trim();
  if (!field || !value) { alertFlash("err", "请填写字段与更正值"); return; }
  try {
    const result=await api("/personas/"+encodeURIComponent(uid)+"/correction", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({corrections:{[field]:value}})});
    const diagnostic=rememberAdminOperation("persona",result,"画像更正未完成");alertFlash(diagnostic?.partial?"info":"ok",diagnostic?.title||"已提交更正");
    await loadPersonaDetail(uid);
    render();
  } catch (e) { const diagnostic=rememberAdminOperation("persona",e,"画像更正未完成");alertFlash("err",diagnostic?.title||"画像更正未完成");render(); }
}

async function refreshAvatarAnalysis(uid) {
  if (!confirm("重新下载并分析该用户当前 QQ 头像？这会调用一次可用的 vision route。")) return;
  state.avatarAnalysisBusy = `refresh:${uid}`;
  render();
  try {
    const result = await api(`/personas/${encodeURIComponent(uid)}/avatar-analysis/refresh`, {method:"POST"});
    const diagnostic = rememberAdminOperation("persona", result, "头像重新分析未排队");
    alertFlash("ok", diagnostic?.title || "头像重新分析已排队");
    await loadPersonaDetail(uid);
  } catch (e) {
    const diagnostic = rememberAdminOperation("persona", e, "头像重新分析未排队");
    alertFlash("err", diagnostic?.title || "头像重新分析未排队");
  } finally {
    state.avatarAnalysisBusy = "";
    render();
  }
}

async function clearAvatarAnalysis(uid) {
  if (!confirm("删除该用户已持久化的头像分析与安全摘要？")) return;
  state.avatarAnalysisBusy = `clear:${uid}`;
  render();
  try {
    const result = await api(`/personas/${encodeURIComponent(uid)}/avatar-analysis`, {method:"DELETE"});
    const diagnostic = rememberAdminOperation("persona", result, "头像分析删除未完成");
    alertFlash(diagnostic?.partial ? "info" : "ok", diagnostic?.title || "头像分析已删除");
    await loadPersonaDetail(uid);
  } catch (e) {
    const diagnostic = rememberAdminOperation("persona", e, "头像分析删除未完成");
    alertFlash("err", diagnostic?.title || "头像分析删除未完成");
  } finally {
    state.avatarAnalysisBusy = "";
    render();
  }
}
