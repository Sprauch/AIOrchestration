// Agent Orchestrator — Control Plane

const startTime = Date.now();
let eventCount = 0, lastSnap = null, allThreads = [], currentFilter = 'all', _firstLoad = true;

// ── Helpers ──
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function ago(ts) { if (!ts) return '-'; const s = Math.floor(Date.now()/1000-ts); return s<60?s+'s':Math.floor(s/60)+'m'; }
const _stageHelp={
  'Analyzing':'PM is analyzing the codebase for improvement opportunities',
  'Proposed':'PM submitted a proposal, waiting for technical review',
  'Developer Assigned':'Architect approved and assigned this to a developer',
  'Implementing':'Developer is working on the code changes',
  'Awaiting Review':'Developer finished, waiting for reviewer',
  'In Review':'Reviewer is checking the developer\'s work',
  'Rework':'Reviewer requested changes — developer will address them',
  'Completed':'Code review passed — ready for PR creation',
  'Rejected':'Architect rejected this proposal',
  'Sent Back to PM':'Architect sent this back for PM to revise',
  'Blocked':'Thread is stuck and needs operator intervention',
  'Abandoned':'Operator marked this thread as skipped',
};
function badge(t) { const tip=_stageHelp[t];return '<span class="badge '+t.toLowerCase().replace(/\s+/g,'_')+'"'+(tip?' title="'+esc(tip)+'"':'')+'>'+esc(t)+'</span>'; }
function role(id) { return id.replace(/-\d+$/,'').replace(/-challenger$/,''); }
function initials(r) { return {pm:'PM',product_designer:'PD',architect:'AR',developer:'DV',reviewer:'RV',orchestrator:'OR'}[r]||'??'; }
function roleTitle(r) { return {pm:'Product Manager',product_designer:'Product Designer',architect:'Architect',developer:'Developer',reviewer:'Reviewer'}[r]||r; }
function stageLabel(s) { return {analyzing:'Analyzing',proposed:'Proposed',approved:'Developer Assigned',implementing:'Implementing',awaiting_review:'Awaiting Review',in_review:'In Review',rework:'Rework',completed:'Completed',rejected:'Rejected',revision_requested:'Sent Back to PM',cycle_exhausted:'Blocked',blocked:'Blocked',abandoned:'Abandoned'}[s]||s; }
function decisionLabel(d) { return {needs_revision:'Sent back to PM',needs_clarification:'Sent back to PM',approved:'Approved',rejected:'Rejected',changes_requested:'Changes requested'}[d]||d; }
function relTime(ts) {
  if (!ts) return '';
  const ms = Date.parse(ts);
  if (Number.isNaN(ms)) return '';
  const diff = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}
function evDetail(type,p) {
  switch(type){
    case 'proposal':return esc(p.title||'');
    case 'proposal_review':{
      const concerns = Array.isArray(p.concerns) ? p.concerns.filter(Boolean) : [];
      const first = concerns.length ? ' ' + esc(concerns[0]) : '';
      return (p.decision||'').toUpperCase() + first;
    }
    case 'task_assignment':return esc(p.branch_name||'');case 'task_progress':return esc(p.status||'');
    case 'review_request':return esc(p.branch_name||'');case 'review_result':return (p.decision||'').toUpperCase();
    case 'human_gate':return esc(p.action||'');
    case 'system':{const a=p.action||'';if(a==='pr_created')return 'PR created: '+esc((p.pr_status||''));if(a==='pr_merged')return 'PR merged: '+esc((p.pr_status||''));if(a==='pr_closed')return 'PR closed: '+esc((p.pr_status||''));if(a==='pr_failed')return 'PR failed: '+esc((p.detail||''));if(a==='pr_skipped')return 'PR skipped: '+esc((p.detail||''));if(a==='cli_timeout')return esc(p.agent_id||'')+' timed out ('+p.timeout_seconds+'s)';return esc(a);}
    default:return type.replace(/_/g,' ');
  }
}

function formatProposalReview(p) {
  let html = '<strong>' + esc((p.decision || '').toUpperCase()) + '</strong>';
  const concerns = Array.isArray(p.concerns) ? p.concerns.filter(Boolean) : [];
  if (concerns.length) {
    html += '<ul class="ev-list">' + concerns.map(c => '<li>' + esc(c) + '</li>').join('') + '</ul>';
  }
  return html;
}

function formatProposal(p) {
  let html = '<strong>' + esc(p.title || '') + '</strong>';
  if (p.description) html += '<div class="ev-desc">' + esc(p.description) + '</div>';
  if (p.proposed_change) html += '<div class="ev-field"><span class="ev-label">Change:</span> ' + esc(p.proposed_change) + '</div>';
  if (p.rationale) html += '<div class="ev-field"><span class="ev-label">Rationale:</span> ' + esc(p.rationale) + '</div>';
  const files = Array.isArray(p.affected_files) ? p.affected_files.filter(Boolean) : [];
  if (files.length) html += '<div class="ev-field"><span class="ev-label">Files:</span> <code>' + files.map(f => esc(f)).join('</code>, <code>') + '</code></div>';
  if (p.priority) html += '<div class="ev-field"><span class="ev-label">Priority:</span> ' + esc(String(p.priority)) + ' · <span class="ev-label">Effort:</span> ' + esc(p.estimated_effort || '?') + '</div>';
  return html;
}

function formatTaskAssignment(p) {
  let html = '<strong>' + esc(p.branch_name || '') + '</strong>';
  if (p.approach) html += '<div class="ev-desc">' + esc(p.approach) + '</div>';
  const files = Array.isArray(p.files_to_modify) ? p.files_to_modify.filter(Boolean) : [];
  if (files.length) html += '<div class="ev-field"><span class="ev-label">Modify:</span> <code>' + files.map(f => esc(f)).join('</code>, <code>') + '</code></div>';
  const create = Array.isArray(p.files_to_create) ? p.files_to_create.filter(Boolean) : [];
  if (create.length) html += '<div class="ev-field"><span class="ev-label">Create:</span> <code>' + create.map(f => esc(f)).join('</code>, <code>') + '</code></div>';
  const criteria = Array.isArray(p.acceptance_criteria) ? p.acceptance_criteria.filter(Boolean) : [];
  if (criteria.length) html += '<ul class="ev-list">' + criteria.map(c => '<li>' + esc(c) + '</li>').join('') + '</ul>';
  if (p.testing_strategy) html += '<div class="ev-field"><span class="ev-label">Testing:</span> ' + esc(p.testing_strategy) + '</div>';
  return html;
}

function formatReviewResult(p) {
  let html = '<strong>' + esc((p.decision || '').toUpperCase()) + '</strong>';
  if (p.summary) html += '<div class="ev-desc">' + esc(p.summary) + '</div>';
  const issues = Array.isArray(p.blocking_issues) ? p.blocking_issues.filter(Boolean) : [];
  if (issues.length) html += '<ul class="ev-list">' + issues.map(i => '<li>' + esc(i) + '</li>').join('') + '</ul>';
  if (p.approval_note) html += '<div class="ev-field"><span class="ev-label">Note:</span> ' + esc(p.approval_note) + '</div>';
  return html;
}

function formatTaskProgress(p) {
  let html = '<strong>' + esc((p.status || '').toUpperCase()) + '</strong>';
  if (p.branch_name) html += ' on <code>' + esc(p.branch_name) + '</code>';
  const files = Array.isArray(p.files_changed) ? p.files_changed.filter(Boolean) : [];
  if (files.length) html += '<div class="ev-field"><span class="ev-label">Files changed:</span> <code>' + files.map(f => esc(f)).join('</code>, <code>') + '</code></div>';
  if (p.notes) html += '<div class="ev-desc">' + esc(p.notes) + '</div>';
  return html;
}

function formatReviewRequest(p) {
  let html = '<code>' + esc(p.branch_name || '') + '</code>';
  const files = Array.isArray(p.files_changed) ? p.files_changed.filter(Boolean) : [];
  if (files.length) html += '<div class="ev-field"><span class="ev-label">Files:</span> <code>' + files.map(f => esc(f)).join('</code>, <code>') + '</code></div>';
  const tests = Array.isArray(p.tests_added) ? p.tests_added.filter(Boolean) : [];
  if (tests.length) html += '<div class="ev-field"><span class="ev-label">Tests added:</span> ' + tests.map(t => esc(t)).join(', ') + '</div>';
  if (p.notes) html += '<div class="ev-desc">' + esc(p.notes) + '</div>';
  return html;
}

// ── Navigation ──
const viewTitles = {overview:'Overview',work:'Work',exceptions:'Exceptions',system:'System'};
document.querySelectorAll('.nav-item').forEach(n=>n.addEventListener('click',()=>switchView(n.dataset.view)));
function switchView(name) {
  // Auto-close thread detail overlay when switching views
  const overlay=document.getElementById('thread-overlay');
  if(overlay&&overlay.style.display!=='none')closeThreadDetail();
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.view===name));
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.getElementById('view-'+name).classList.add('active');
  const tt=document.getElementById('topbar-title');if(tt)tt.textContent=viewTitles[name]||name;
  if(name==='overview'&&lastSnap) setTimeout(()=>renderPipelineMini(lastSnap.agents,lastSnap.challengers||{},lastSnap.backpressure),100);
  if(name==='system'&&lastSnap) setTimeout(()=>renderPipelineFull(lastSnap.agents,lastSnap.challengers||{},lastSnap.backpressure),100);
  if(name==='exceptions') refreshExceptions();
  if(name==='system') refreshSystem();
}
// Sub-tabs within System
document.querySelectorAll('.sub-tab').forEach(t=>t.addEventListener('click',function(){
  document.querySelectorAll('.sub-tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.sub-view').forEach(v=>v.classList.remove('active'));
  this.classList.add('active');
  document.getElementById(this.dataset.sub).classList.add('active');
  if(this.dataset.sub==='sys-agents'&&lastSnap) setTimeout(()=>renderPipelineFull(lastSnap.agents,lastSnap.challengers||{},lastSnap.backpressure),50);
  if(this.dataset.sub==='sys-redis') refreshRedisInspector();
}));

function closePanel(id){document.getElementById(id).classList.remove('open')}
function addFeedItem(feedId,ev){
  const feed=document.getElementById(feedId);if(!feed)return;
  if(feed.querySelector('.empty'))feed.innerHTML='';
  const div=document.createElement('div');div.className='fi';
  div.innerHTML='<span class="fi-time">'+esc(ev.timestamp)+'</span><span class="fi-sender '+ev.role+'">'+esc(ev.sender)+'</span><span class="fi-type">'+ev.type.replace(/_/g,' ')+'</span><span class="fi-detail">'+evDetail(ev.type,ev.payload)+'</span>';
  feed.prepend(div);while(feed.children.length>80)feed.removeChild(feed.lastChild);
}

// ── Phase indicator ──
function renderPhaseIndicator(phaseData) {
  const container = document.getElementById('phase-indicator');
  if (!container || !phaseData) return;
  const phases = phaseData.phases || [];
  const steps = container.querySelectorAll('.phase-step');
  const connectors = container.querySelectorAll('.phase-connector');
  steps.forEach((el, i) => {
    const p = phases[i];
    if (!p) return;
    el.className = 'phase-step ' + (p.state || 'inactive');
    el.title = p.label || '';
  });
  connectors.forEach((el, i) => {
    const p = phases[i];
    el.className = 'phase-connector' + (p && (p.state === 'completed' || p.state === 'active') ? ' done' : '');
  });
}

// ── Error Summary Panel ──
function toggleErrorPanel(){
  document.getElementById('error-summary-panel').classList.toggle('collapsed');
}
function renderErrorSummary(exceptions, metrics, threads) {
  const el = document.getElementById('error-summary-content');
  if (!el) return;
  const errors = [];

  // Failed PRs — critical severity
  (exceptions.failed_prs || []).forEach(function(pr) {
    errors.push({
      time: pr.last_time || '',
      source: 'orchestrator',
      sourceRole: 'orchestrator',
      desc: 'PR failed: ' + (pr.branch || pr.thread_id.slice(0, 8)) + (pr.detail ? ' — ' + pr.detail : ''),
      severity: 'critical'
    });
  });

  // Blocked threads — critical severity
  (exceptions.blocked_threads || []).forEach(function(t) {
    const reason = t.blocked_reason || t.why || 'Unknown';
    errors.push({
      time: t.last_time || '',
      source: stageLabel(t.stage) || 'pipeline',
      sourceRole: _stageToRole(t.stage),
      desc: (t.label || t.thread_id.slice(0, 8)) + ' blocked: ' + reason,
      severity: 'critical'
    });
  });

  // Stale agents — warning severity
  (exceptions.stale_agents || []).forEach(function(a) {
    errors.push({
      time: '',
      source: role(a.agent_id),
      sourceRole: role(a.agent_id),
      desc: a.agent_id + ' idle for ' + a.heartbeat_age + 's (threshold: ' + (a.threshold || '?') + 's)',
      severity: 'warning'
    });
  });

  // Pending approvals — warning severity
  (exceptions.approvals || []).forEach(function(g) {
    errors.push({
      time: '',
      source: role(g.sender || 'system'),
      sourceRole: role(g.sender || 'system'),
      desc: 'Awaiting approval: ' + (g.action || 'unknown action') + (g.reason ? ' — ' + g.reason : ''),
      severity: 'warning'
    });
  });

  // Error metrics from snapshot — warning severity
  var mt = metrics || {};
  Object.keys(mt).forEach(function(k) {
    if (k.startsWith('errors:') && parseInt(mt[k]) > 0) {
      errors.push({
        time: '',
        source: 'system',
        sourceRole: 'system',
        desc: k.replace(/_/g, ' ') + ': ' + mt[k],
        severity: 'warning'
      });
    }
  });

  // Threads with failed status — critical
  (threads || []).forEach(function(t) {
    if (t.status === 'failed') {
      errors.push({
        time: t.last_time || '',
        source: stageLabel(t.stage) || 'pipeline',
        sourceRole: _stageToRole(t.stage),
        desc: (t.label || t.thread_id.slice(0, 8)) + ' failed' + (t.why ? ': ' + t.why : ''),
        severity: 'critical'
      });
    }
  });

  // Sort: critical first, then warning
  errors.sort(function(a, b) { return (a.severity === 'critical' ? 0 : 1) - (b.severity === 'critical' ? 0 : 1); });

  // Update header count
  var head = document.querySelector('#error-summary-panel .card-head h3');
  if (head) head.textContent = 'Error Summary' + (errors.length ? ' (' + errors.length + ')' : '');

  if (!errors.length) {
    el.innerHTML = '<div class="error-summary-empty">&#10003; No recent errors — system healthy</div>';
    return;
  }

  el.innerHTML = errors.map(function(e) {
    return '<div class="error-entry severity-' + e.severity + '">' +
      '<span class="ee-time">' + esc(e.time) + '</span>' +
      '<span class="ee-source ' + e.sourceRole + '">' + esc(e.source) + '</span>' +
      '<span class="ee-desc">' + esc(e.desc) + '</span>' +
      '</div>';
  }).join('');
}
function _stageToRole(stage) {
  var map = {analyzing:'pm',proposed:'pm',approved:'architect',implementing:'developer',awaiting_review:'developer',in_review:'reviewer',rework:'developer',completed:'system',rejected:'architect',revision_requested:'architect',cycle_exhausted:'system',blocked:'system',abandoned:'system'};
  return map[stage] || 'system';
}

// ── Work filters ──
document.querySelectorAll('.filter-btn').forEach(b=>b.addEventListener('click',function(){
  document.querySelectorAll('.filter-btn').forEach(x=>x.classList.remove('active'));
  this.classList.add('active');currentFilter=this.dataset.filter;renderWorkTable();
}));

// ── Overview ──
async function refreshOverview() {
  try {
    const [snap,threads,exceptions,prs] = await Promise.all([
      fetch('/api/snapshot').then(r=>r.json()),
      fetch('/api/threads').then(r=>r.json()),
      fetch('/api/exceptions').then(r=>r.json()),
      fetch('/api/prs').then(r=>r.json()),
    ]);
    if(snap.error)return;
    lastSnap=snap; allThreads=threads;

    // Connection status
    const hb=snap.orchestrator_heartbeat;
    const dot=document.getElementById('sb-dot'),stat=document.getElementById('sb-status');
    const now=Date.now()/1000;
    if(hb&&(now-hb)<30){dot.className='status-dot ok';stat.textContent='Connected';}
    else{dot.className='status-dot bad';stat.textContent='Disconnected';}
    // Show dashboard session time, labeled honestly
    const up=Math.floor((Date.now()-startTime)/1000);
    document.getElementById('sb-uptime').textContent='session '+Math.floor(up/60)+'m';
    document.getElementById('topbar-time').textContent=new Date().toLocaleTimeString();

    // Hide first-run once connected
    const fr=document.getElementById('first-run');if(fr)fr.style.display='none';

    // Counts
    const mt=snap.metrics;
    const completed=threads.filter(t=>t.status==='completed').length;
    const healthy=threads.filter(t=>!['completed','failed','rejected','blocked','rework'].includes(t.status)).length;
    const blocked=threads.filter(t=>['blocked','rework'].includes(t.status)).length;
    const nA=exceptions.approvals.length,nP=exceptions.failed_prs.length,nB=exceptions.blocked_threads.length;
    const totalExc=nA+nP+nB;
    const totalCostMc=parseInt(mt['cost_mc:total'])||0;
    const costStr=totalCostMc?fmtCost(totalCostMc):'';

    // Nav badges
    const excNb=document.getElementById('nav-exc-count');
    if(totalExc){excNb.textContent=totalExc;excNb.style.display='';}else{excNb.style.display='none';}
    const wNb=document.getElementById('nav-work-count');if(healthy){wNb.textContent=healthy;wNb.style.display='';}else{wNb.style.display='none';}
    document.getElementById('thread-count').textContent=threads.length;

    // 1. Status brief — one sentence
    const sb=document.getElementById('status-brief');
    if(sb){
      let statusClass='ok',statusText='System is working normally.';
      const noHeartbeat=!snap.orchestrator_heartbeat||(Date.now()/1000-snap.orchestrator_heartbeat)>30;
      // The server knows why a start died even if this tab never clicked the button, so a
      // reload on a phone still gets the reason rather than a bare "disconnected".
      if(snap.orchestrator_start_error)window.orchStartError=snap.orchestrator_start_error;
      else if(!noHeartbeat)window.orchStartError='';
      // A start takes several seconds - preflight, stale-worktree cleanup, every agent
      // connecting - and the 5s refresh kept repainting "disconnected" throughout, which
      // contradicted the button's own "waiting for heartbeat" a few pixels above it. Two
      // truths on one screen read as a failure. While a start is in flight the state is
      // CONNECTING: neither a lie nor an alarm.
      if(noHeartbeat&&window.orchStarting){statusClass='connecting';statusText='Orchestrator is starting...';}
      // A start that DIED is not a disconnection, and saying "disconnected" hides the one
      // fact that fixes it. First line only: the full output stays under the button.
      else if(noHeartbeat&&window.orchStartError){
        statusClass='bad';
        statusText='Orchestrator failed to start: '+String(window.orchStartError).split('\n')[0];
      }
      else if(noHeartbeat){statusClass='bad';statusText='Orchestrator is disconnected.';}
      else if(totalExc){statusClass='warn';statusText=totalExc+' item'+(totalExc>1?'s':'')+' need'+(totalExc===1?'s':'')+' attention.';}
      const parts=[statusText];
      if(healthy)parts.push(healthy+' in progress.');
      if(completed)parts.push(completed+' shipped.');
      if(costStr)parts.push(costStr+' spent.');
      // When the orchestrator is down, offer to start it rather than only reporting it.
      // Rendered ABOVE the brief so it is the first thing read, and ONLY when
      // disconnected: an always-present start button on a running system is an invitation
      // to a second orchestrator against one Redis.
      // Only when genuinely down. Not while connecting: the start is already in flight,
      // and a second click would be refused by the server anyway - offering it invites a
      // person to believe the first one failed.
      const startBtn=(statusClass==='bad')
        ? '<div class="brief-action"><button id="btn-start-orch" onclick="startOrchestrator()">Start orchestrator</button>'
          +'<span id="start-orch-msg"></span></div>'
        : '';
      sb.innerHTML=startBtn+'<div class="brief brief-'+statusClass+'"><div class="brief-dot"></div><div class="brief-text">'+parts.join(' ')+'</div></div>';
    }

    // 2. Attention brief — max 5 items, problem threads + approvals
    const ab=document.getElementById('attention-brief');
    if(ab){
      const items=[];
      // Approvals first — operator must decide
      exceptions.approvals.slice(0,2).forEach(g=>{
        items.push({label:g.reason||'Approval needed',badge:'amber',action:'exceptions',detail:'Waiting for your decision'});
      });
      // Blocked threads only — not rework/sent-back (those are pipeline-internal)
      threads.filter(t=>t.status==='blocked'||t.stage==='cycle_exhausted').slice(0,3).forEach(t=>{
        items.push({label:t.label||t.thread_id.slice(0,8),badge:'red',action:'thread:'+t.thread_id,detail:'Blocked — needs operator intervention'});
      });
      // Failed PRs
      exceptions.failed_prs.slice(0,1).forEach(p=>{
        items.push({label:'PR failed: '+(p.branch||p.thread_id.slice(0,8)),badge:'red',action:'exceptions',detail:'Retry or investigate'});
      });
      if(!items.length){
        ab.innerHTML='';
      }else{
        ab.innerHTML='<div class="attention-brief"><div class="ab-head">Needs Attention</div>'+items.slice(0,5).map(it=>{
          const onclick=it.action==='exceptions'?'switchView(\'exceptions\')':'goToThread(\''+esc(it.action.replace('thread:',''))+'\')';
          return '<div class="ab-item ab-'+it.badge+'" onclick="'+onclick+'"><div class="ab-label">'+esc(it.label)+'</div><div class="ab-detail">'+esc(it.detail)+'</div></div>';
        }).join('')+'</div>';
      }
    }

    // 3. Outcome tiles
    document.getElementById('outcome-tiles').innerHTML=[
      '<div class="otile c-green"><div class="ot-label">Completed</div><div class="ot-value">'+completed+'</div><div class="ot-sub">'+(mt['prs:created']||0)+' PRs created</div></div>',
      '<div class="otile c-amber"><div class="ot-label">In Progress</div><div class="ot-value">'+healthy+'</div><div class="ot-sub">'+threads.length+' total threads</div></div>',
      '<div class="otile '+(blocked?'c-red':'c-green')+'"><div class="ot-label">Blocked</div><div class="ot-value">'+blocked+'</div><div class="ot-sub">'+(blocked?blocked+' need attention':'All clear')+'</div></div>',
      // Both figures, because they answer different questions. The dollar amount is the
      // API-rate equivalent and is real but abstract on a subscription; the percentage
      // of the weekly allowance is the one you act on. Percentage leads.
      (function(){
        const w=snap.weekly||{};
        const fmtT=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n||0);
        const rows=[];
        rows.push('<div class="ot-row"><span>API rate</span><b>'+(costStr||'—')+'</b></div>');
        rows.push('<div class="ot-row"><span>Tokens</span><b>'+fmtT(w.used||0)+'</b></div>');
        if(w.budget){
          rows.push('<div class="ot-row"><span>Weekly allowance</span><b>'+fmtT(w.budget)+'</b></div>');
        }
        if(w.budget){
          const pct=w.pct||0;
          const tone=pct>=90?'c-red':pct>=70?'c-amber':'c-blue';
          return '<div class="otile '+tone+'"><div class="ot-label">Consumption this week</div>'
            +'<div class="ot-value">'+pct+'%</div>'
            +'<div class="ot-bar"><div class="ot-bar-fill" style="width:'+Math.min(pct,100)+'%"></div></div>'
            +'<div class="ot-rows">'+rows.join('')+'</div></div>';
        }
        // No allowance configured: show what is known rather than a percentage of nothing.
        return '<div class="otile c-blue"><div class="ot-label">Usage this week</div>'
          +'<div class="ot-value">'+fmtT(w.used||0)+'</div>'
          +'<div class="ot-rows">'+rows.join('')
          +'<div class="ot-row ot-row-hint"><span>Set weekly_token_budget for %</span></div></div></div>';
      })(),
    ].join('');

    // 4. Agent status strip — compact row, not full pipeline diagram
    const as=document.getElementById('agent-strip');
    if(as){
      const byR={};snap.agents.forEach(a=>{const r=role(a.agent_id);if(!byR[r])byR[r]=[];byR[r].push(a);});
      const allAgents=snap.agents||[];
      const anyActive=allAgents.some(a=>!a.paused);
      const pauseAllBtn='<button class="as-pause-all" onclick="togglePauseAll()" title="'+(anyActive?'Pause all agents':'Resume all agents')+'">'+(anyActive?'Pause All':'Resume All')+'</button>';
      as.innerHTML=['pm','product_designer','architect','developer','reviewer'].map(r=>{
        const agents=byR[r]||[];
        if(!agents.length)return '<div class="as-agent as-'+r+'"><div class="as-dot off"></div><div class="as-label">'+roleTitle(r)+'</div><div class="as-status">—</div></div>';
        return agents.map(a=>{
          const st=a.paused?'paused':a.status;
          const dotCls=st==='busy'?'busy':st==='active'?'ok':st==='paused'?'paused':'off';
          const displaySt=st==='active'?'ready':st;
          return '<div class="as-agent as-'+r+'" onclick="switchView(\'system\');showAgent(\''+esc(a.agent_id)+'\')"><div class="as-dot '+dotCls+'"></div><div class="as-label">'+roleTitle(r)+'</div><div class="as-status">'+esc(displaySt)+'</div></div>';
        }).join('');
      }).join('')+pauseAllBtn;
    }

    renderPRs(prs);
    renderPhaseIndicator(snap.pipeline_phase);
    renderRecentChanges(threads);
    renderErrorSummary(exceptions, snap.metrics, threads);
    renderAudit();
    renderWorkTable();
    _firstLoad=false;
  } catch(e){
    console.error('refreshOverview error:',e);
    const dot=document.getElementById('sb-dot'),stat=document.getElementById('sb-status');
    if(dot)dot.className='status-dot bad';if(stat)stat.textContent='Error';
  }
}

// ── Work ──
function _threadExplanation(t){
  const rounds=t.proposal_submissions||0;
  const state=t.current_state||stageLabel(t.stage);
  if(rounds>1) return 'Round '+rounds+' — '+state;
  return state;
}

function renderRecentChanges(threads){
  const el=document.getElementById('recent-changes');
  if(!el)return;
  const items=(threads||[])
    .filter(t=>t.latest_change&&t.latest_change.timestamp)
    .sort((a,b)=>Date.parse(b.latest_change.timestamp||0)-Date.parse(a.latest_change.timestamp||0))
    .slice(0,6);
  if(!items.length){
    el.innerHTML='<div class="empty">No recent workflow changes.</div>';
    return;
  }
  el.innerHTML=items.map(t=>{
    const ch=t.latest_change||{};
    return '<div class="recent-change rc-'+esc(ch.kind||'neutral')+'" onclick="goToThread(\''+t.thread_id+'\')">'
      +'<div class="rc-main"><div class="rc-summary">'+esc(ch.summary||'Workflow changed')+'</div><div class="rc-thread">'+esc(t.label||t.thread_id.slice(0,8))+'</div><div class="rc-detail">'+esc(ch.detail||'')+'</div></div>'
      +'<div class="rc-time">'+esc(relTime(ch.timestamp)||ch.time||'')+'</div>'
      +'</div>';
  }).join('');
}

function renderWorkTable() {
  const filtered=currentFilter==='all'?allThreads:allThreads.filter(t=>{
    if(currentFilter==='active')return !['completed','failed','rejected','blocked'].includes(t.status);
    if(currentFilter==='rework')return t.status==='rework';
    if(currentFilter==='completed')return t.status==='completed';
    if(currentFilter==='failed')return ['failed','blocked'].includes(t.status);
    return true;
  });
  const _statusPriority={blocked:0,rework:1,active:2,completed:3,failed:4,rejected:4};
  filtered.sort((a,b)=>(_statusPriority[a.status]??3)-(_statusPriority[b.status]??3));
  const el=document.getElementById('work-list');
  if(!filtered.length){
    const connected=document.getElementById('sb-status')?.textContent==='Connected';
    const msg=_firstLoad?'<span class="loading-pulse">Loading threads...</span>'
      :currentFilter!=='all'?'No threads match this filter.'
      :(connected?'No threads yet. The orchestrator is connected and waiting to generate work.'
      :'No work started yet. Run <code>agent-orchestrator run</code> to begin.');
    el.innerHTML='<div class="empty">'+msg+'</div>';return;
  }
  el.innerHTML=filtered.map(t=>{
    const cls=t.status==='blocked'||t.stage==='cycle_exhausted'?' wc-blocked'
      :t.status==='rework'||t.stage==='revision_requested'?' wc-rework'
      :t.status==='completed'?' wc-completed'
      :['failed','rejected'].includes(t.status)||t.stage==='abandoned'?' wc-abandoned':'';
    const explanation=_threadExplanation(t);
    const decision=t.last_decision?'Last decision: '+esc(decisionLabel(t.last_decision)):'';
    const ch=t.latest_change||null;
    const fresh=ch&&ch.timestamp&&((Date.now()-Date.parse(ch.timestamp))/1000)<900;
    return '<div class="work-case'+cls+'" data-tid="'+t.thread_id+'" onclick="showThread(\''+t.thread_id+'\')">'
      +'<div class="wc-top"><div class="wc-title">'+esc(t.label||t.thread_id.slice(0,8))+'</div><div class="wc-badges">'+badge(stageLabel(t.stage))+'</div></div>'
      +'<div class="wc-explain">'+esc(explanation)+'</div>'
      +(fresh?'<div class="wc-change wc-'+esc(ch.kind||'neutral')+'">'+esc(relTime(ch.timestamp)||'')+(relTime(ch.timestamp)?' · ':'')+esc(ch.summary||'')+'</div>':'')
      +(decision?'<div class="wc-decision">'+decision+'</div>':'')
      +'<div class="wc-meta">'+(t.branch?'<span>'+esc(t.branch)+'</span>':'')+(t.last_time?'<span>'+esc(t.last_time)+'</span>':'')+'</div>'
      +'</div>';
  }).join('');
}
function goToThread(tid){switchView('work');showThread(tid);}

function closeThreadDetail(){
  document.getElementById('thread-overlay').style.display='none';
  document.getElementById('thread-detail-mode').innerHTML='';
  document.getElementById('thread-narrative').innerHTML='';
  document.getElementById('thread-latest-change').innerHTML='';
  document.getElementById('thread-challenger').innerHTML='';
  document.getElementById('thread-summary').innerHTML='';
  document.getElementById('thread-transcript').innerHTML='';
  document.getElementById('thread-detail-body').innerHTML='';
  document.querySelectorAll('.work-case').forEach(c=>c.classList.remove('selected'));
}

function renderThreadEvent(e){
  const ts=e.timestamp.length>19?e.timestamp.slice(11,19):e.timestamp;
  const p=e.payload;let c='';
  if(e.type==='proposal')c=formatProposal(p);
  else if(e.type==='proposal_review')c=formatProposalReview(p);
  else if(e.type==='task_assignment')c=formatTaskAssignment(p);
  else if(e.type==='task_progress')c=formatTaskProgress(p);
  else if(e.type==='review_request')c=formatReviewRequest(p);
  else if(e.type==='review_result')c=formatReviewResult(p);
  else if(e.type==='system'){const a=p.action||'';c='<strong>'+esc(a)+'</strong>';if(p.detail)c+=' — '+esc(String(p.detail));}
  else c=esc(JSON.stringify(p));
  return '<div class="tl-item"><div class="tl-dot '+e.type+'"></div><div class="tl-head">'+e.type.replace(/_/g,' ')+'</div><div class="tl-meta">'+ts+' · '+esc(e.sender)+'</div><div class="tl-body">'+c+'</div></div>';
}

function renderProposalGroups(detail){
  const groups=detail.proposal_groups||[];
  const threadEvents=detail.thread_events||[];
  let html='';
  if(!groups.length&&!threadEvents.length){
    return '<div class="empty">No proposal history yet</div>';
  }
  html+=groups.map(g=>{
    const open=g.is_current?' open':'';
    const statusTag=g.is_current?'<span class="proposal-current-tag">CURRENT</span>':'<span class="proposal-historical-tag">HISTORICAL</span>';
    const decision=g.last_decision?'<span class="proposal-meta">'+esc(g.last_decision)+'</span>':'';
    const count='<span class="proposal-meta">'+g.events.length+' events</span>';
    const events=(g.events||[]).map(renderThreadEvent).join('');
    const cls='proposal-group'+(g.is_current?' proposal-current':' proposal-historical');
    return '<details class="'+cls+'"'+open+'><summary class="proposal-summary"><div class="proposal-main"><div class="proposal-kicker">Proposal '+g.proposal_index+' '+statusTag+'</div><div class="proposal-title">'+esc(g.title||('Proposal '+g.proposal_index))+'</div></div><div class="proposal-side">'+badge(stageLabel(g.stage))+' '+badge(g.status)+' '+decision+' '+count+'</div></summary><div class="proposal-events">'+events+'</div></details>';
  }).join('');
  // Thread-level events shown separately below proposals
  if(threadEvents.length){
    html+='<div class="thread-level-events"><div class="thread-level-heading">Thread Events</div>';
    html+=threadEvents.map(renderThreadEvent).join('');
    html+='</div>';
  }
  return html;
}

function parseTraceJson(content){
  if(!content||typeof content!=='string')return null;
  const trimmed=content.trim();
  if(!trimmed||(!trimmed.startsWith('{')&&!trimmed.startsWith('[')))return null;
  try{return JSON.parse(trimmed);}catch(_e){return null;}
}

function renderTraceText(content){
  return esc(content||'').replace(/\n/g,'<br>');
}

function renderTraceValue(value){
  if(value===null||value===undefined||value==='')return '<span class="trace-empty">—</span>';
  if(Array.isArray(value)){
    if(!value.length)return '<span class="trace-empty">—</span>';
    return '<div class="trace-list">'+value.map(item=>'<div class="trace-list-item">'+renderTraceValue(item)+'</div>').join('')+'</div>';
  }
  if(typeof value==='object'){
    const entries=Object.entries(value).filter(([,v])=>v!==undefined&&v!==null&&v!=='');
    if(!entries.length)return '<span class="trace-empty">—</span>';
    return '<div class="trace-fields">'+entries.map(([k,v])=>'<div class="trace-field"><div class="trace-key">'+esc(k.replace(/_/g,' '))+'</div><div class="trace-value">'+renderTraceValue(v)+'</div></div>').join('')+'</div>';
  }
  return '<span>'+renderTraceText(String(value))+'</span>';
}

function renderTraceMessage(msg,index){
  const payload=msg.payload&&typeof msg.payload==='object'?msg.payload:{};
  const recipient=msg.recipient_role?'<span class="trace-message-recipient">to '+esc(msg.recipient_role)+'</span>':'';
  return '<div class="trace-message-card">'
    +'<div class="trace-message-head"><div class="trace-message-type">'+esc((msg.message_type||('message '+(index+1))).replace(/_/g,' '))+'</div>'+recipient+'</div>'
    +renderTraceValue(payload)
    +'</div>';
}

function renderStructuredTrace(data){
  if(data&&typeof data==='object'&&Array.isArray(data.messages)){
    const version=data.schema_version!==undefined?'<div class="trace-structured-meta">Schema v'+esc(String(data.schema_version))+'</div>':'';
    return '<div class="trace-structured">'
      +version
      +data.messages.map((msg,idx)=>renderTraceMessage(msg,idx)).join('')
      +'</div>';
  }
  return '<div class="trace-structured generic-trace">'+renderTraceValue(data)+'</div>';
}

function renderTraceContent(content){
  const parsed=parseTraceJson(content);
  if(parsed!==null)return renderStructuredTrace(parsed);
  return renderTraceText(content);
}

function renderTranscriptTurn(turn){
  const ts=turn.timestamp&&turn.timestamp.length>19?turn.timestamp.slice(11,19):turn.timestamp||'';
  const prompt=turn.prompt||{};
  const response=turn.response||null;
  let meta=ts+' · '+esc(turn.agent||'');
  if(turn.role)meta+=' · '+esc(turn.role);
  let html='<div class="transcript-turn">';
  html+='<div class="transcript-turn-head"><div class="transcript-turn-title">'+meta+'</div></div>';
  html+='<div class="transcript-block"><div class="transcript-block-head">Prompt</div><div class="transcript-content">'+renderTraceContent(prompt.content||prompt.content_preview||'')+'</div></div>';
  if(response){
    const modelBits=[response.cli_backend,response.cli_model||response.model].filter(Boolean).join(' · ');
    const usageBits=[];
    if(response.duration_ms)usageBits.push(response.duration_ms+'ms');
    if(response.input_tokens||response.output_tokens)usageBits.push((response.input_tokens||0)+' in / '+(response.output_tokens||0)+' out');
    if(response.cost_usd)usageBits.push('$'+Number(response.cost_usd).toFixed(4));
    html+='<div class="transcript-block"><div class="transcript-block-head">Response'+(modelBits?'<span class="transcript-meta"> '+esc(modelBits)+'</span>':'')+(usageBits.length?'<span class="transcript-meta"> · '+esc(usageBits.join(' · '))+'</span>':'')+'</div><div class="transcript-content">'+renderTraceContent(response.content||response.content_preview||'')+'</div></div>';
  } else {
    html+='<div class="transcript-empty">No model response was recorded for this turn.</div>';
  }
  if(turn.hidden_trace_count){
    html+='<div class="transcript-note">'+turn.hidden_trace_count+' internal trace'+(turn.hidden_trace_count===1?'':'s')+' hidden. Use Advanced to inspect deliberation rounds and raw trace entries.</div>';
  }
  html+='</div>';
  return html;
}

function renderRawTrace(trace){
  const ts=trace.timestamp&&trace.timestamp.length>19?trace.timestamp.slice(11,19):trace.timestamp||'';
  const dir=(trace.direction||'').toUpperCase();
  const tags=[trace.agent,trace.role,dir].filter(Boolean);
  if(trace.model)tags.push(trace.model);
  if(trace.deliberation_round)tags.push('R'+trace.deliberation_round);
  if(trace.cli_backend||trace.cli_model)tags.push([trace.cli_backend,trace.cli_model].filter(Boolean).join(' · '));
  const metrics=[];
  if(trace.duration_ms)metrics.push(trace.duration_ms+'ms');
  if(trace.input_tokens||trace.output_tokens)metrics.push((trace.input_tokens||0)+' in / '+(trace.output_tokens||0)+' out');
  if(trace.cost_usd)metrics.push('$'+Number(trace.cost_usd).toFixed(4));
  return '<div class="raw-trace'+(trace.is_error?' is-error':'')+'"><div class="raw-trace-head"><div class="raw-trace-title">'+esc(tags.join(' · '))+'</div><div class="raw-trace-time">'+esc(ts)+'</div></div>'+(metrics.length?'<div class="raw-trace-meta">'+esc(metrics.join(' · '))+'</div>':'')+'<div class="transcript-content">'+renderTraceContent(trace.content||trace.content_preview||'')+'</div></div>';
}

function renderThreadTranscript(detail,advanced){
  const turns=detail.transcript_turns||[];
  const raw=detail.raw_traces||[];
  if(!turns.length&&!raw.length)return '';
  let html='<div class="transcript-card"><div class="transcript-head"><div><div class="transcript-title">Model Transcript</div><div class="transcript-sub">Shows the prompt sent to the model and the answer that drove the workflow. Advanced mode includes internal deliberation and raw trace entries.</div></div><label class="transcript-toggle"><input type="checkbox" id="thread-transcript-advanced"'+(advanced?' checked':'')+'> Advanced</label></div>';
  if(!advanced){
    if(!turns.length)html+='<div class="empty">No model interactions recorded for this thread yet. Traces appear as agents process work.</div>';
    else html+=turns.map(renderTranscriptTurn).join('');
  }else{
    if(!raw.length)html+='<div class="empty">No raw traces recorded for this thread yet.</div>';
    else html+=raw.map(renderRawTrace).join('');
  }
  html+='</div>';
  return html;
}

function renderThreadModeSwitch(mode,hasTranscript){
  if(!hasTranscript)return '';
  return '<div class="thread-mode-switch" role="tablist" aria-label="Thread detail mode">'
    +'<button class="thread-mode-btn'+(mode==='workflow'?' active':'')+'" data-mode="workflow" type="button">Workflow</button>'
    +'<button class="thread-mode-btn'+(mode==='trace'?' active':'')+'" data-mode="trace" type="button">Model Trace</button>'
    +'</div>';
}

function applyThreadMode(detail){
  const modeRoot=document.getElementById('thread-detail-mode');
  const workflow=document.getElementById('thread-workflow-panel');
  const transcript=document.getElementById('thread-transcript');
  if(!modeRoot||!workflow||!transcript)return;
  const hasTranscript=(detail.transcript_turns||[]).length||(detail.raw_traces||[]).length;
  const mode=(modeRoot.dataset.mode||'workflow');
  modeRoot.innerHTML=renderThreadModeSwitch(mode,!!hasTranscript);
  workflow.style.display=mode==='trace'?'none':'';
  transcript.style.display=mode==='trace'?'':'none';
  modeRoot.querySelectorAll('.thread-mode-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{
      modeRoot.dataset.mode=btn.dataset.mode||'workflow';
      applyThreadMode(detail);
    });
  });
}

function mountThreadTranscript(detail){
  const root=document.getElementById('thread-transcript');
  if(!root)return;
  const advanced=root.dataset.advanced==='1';
  root.innerHTML=renderThreadTranscript(detail,advanced);
  const toggle=document.getElementById('thread-transcript-advanced');
  if(toggle){
    toggle.addEventListener('change',()=>{
      root.dataset.advanced=toggle.checked?'1':'0';
      mountThreadTranscript(detail);
      applyThreadMode(detail);
    });
  }
}

async function showThread(tid) {
  document.getElementById('thread-detail-id').textContent=tid.slice(0,8);
  document.getElementById('thread-overlay').style.display='';
  document.querySelectorAll('.work-case').forEach(c=>c.classList.remove('selected'));
  document.querySelectorAll('.work-case').forEach(c=>{
    if(c.getAttribute('data-tid')===tid) c.classList.add('selected');
  });
  const t=allThreads.find(x=>x.thread_id===tid);
  if(t){
    // Narrative: Goal → Current State → Why → Next Step (explanation only, no duplicated facts)
    let n='<div class="thread-narrative">';
    n+='<div class="tn-title">'+esc(t.goal||t.label||t.thread_id.slice(0,8))+'</div>';
    n+='<div class="tn-badges">'+badge(stageLabel(t.stage))+' '+badge(t.status)+'</div>';

    n+='<div class="tn-fields">';
    n+='<div class="tn-field"><span class="tn-field-label">Current State</span><span class="tn-field-value">'+esc(t.current_state||'—')+'</span></div>';
    if(t.why)n+='<div class="tn-field"><span class="tn-field-label">Why</span><span class="tn-field-value tn-why">'+esc(t.why)+'</span></div>';
    if(t.blocked_reason)n+='<div class="tn-blocker">'+esc(t.blocked_reason)+'</div>';
    if(t.next_step)n+='<div class="tn-field"><span class="tn-field-label">Next Step</span><span class="tn-field-value tn-next">'+esc(t.next_step)+'</span></div>';
    if(t.needs_human)n+='<div class="tn-human">Human decision needed</div>';
    n+='</div></div>';
    document.getElementById('thread-narrative').innerHTML=n;

    const latest=t.latest_change;
    if(latest){
      document.getElementById('thread-latest-change').innerHTML=
        '<div class="latest-change latest-'+esc(latest.kind||'neutral')+'">'
        +'<div class="lc-kicker">Latest Change</div>'
        +'<div class="lc-summary">'+esc(latest.summary||'Workflow changed')+'</div>'
        +(latest.detail?'<div class="lc-detail">'+esc(latest.detail)+'</div>':'')
        +'<div class="lc-time">'+esc(relTime(latest.timestamp)||latest.time||'')+'</div>'
        +'</div>';
    } else {
      document.getElementById('thread-latest-change').innerHTML='';
    }

    // Challenger impact — show before/after, not just that it happened
    const cs=t.challenger_summary;
    if(cs&&cs.challenged){
      let ch='<div class="challenger-card">';
      ch+='<div class="ch-head">'+(cs.outcome_changed?'Revised after challenge':'Challenged but unchanged')+'</div>';
      if(cs.challenger_concern)ch+='<div class="ch-section"><div class="ch-label">Challenger concern</div><div class="ch-concern">"'+esc(cs.challenger_concern)+'"</div></div>';
      if(cs.outcome_changed&&cs.primary_initial&&cs.primary_final){
        ch+='<div class="ch-compare">';
        ch+='<div class="ch-before"><div class="ch-label">Original position</div><div class="ch-text">'+esc(cs.primary_initial)+'</div></div>';
        ch+='<div class="ch-after"><div class="ch-label">After challenge</div><div class="ch-text">'+esc(cs.primary_final)+'</div></div>';
        ch+='</div>';
      }
      ch+='<div class="ch-outcome">'+(cs.rounds_used||'?')+' deliberation rounds</div>';
      ch+='</div>';
      document.getElementById('thread-challenger').innerHTML=ch;
    } else {
      document.getElementById('thread-challenger').innerHTML='';
    }

    // Summary: compact facts only (no duplication with narrative)
    let s='<div class="thread-summary">';
    if(t.branch)s+='<div class="ts-row"><span class="ts-label">Branch</span><span class="ts-value">'+esc(t.branch)+'</span></div>';
    if(t.proposal_summary)s+='<div class="ts-row"><span class="ts-label">Proposals</span><span class="ts-value">'+esc(t.proposal_summary)+'</span></div>';
    if(t.thread_scope)s+='<div class="ts-row"><span class="ts-label">Scope</span><span class="ts-value">'+esc(t.thread_scope)+'</span></div>';
    if(t.pr)s+='<div class="ts-row"><span class="ts-label">PR</span><span class="ts-value">'+(t.pr.url?'<a href="'+esc(t.pr.url)+'" target="_blank">'+esc(t.pr.url)+'</a>':esc(t.pr.status))+'</span></div>';
    if(t.pr_scope)s+='<div class="ts-row"><span class="ts-label">PR Rule</span><span class="ts-value">'+esc(t.pr_scope)+'</span></div>';
    if(t.review_cycles)s+='<div class="ts-row"><span class="ts-label">Review cycles</span><span class="ts-value">'+t.review_cycles+'</span></div>';
    if(t.blocking_issues&&t.blocking_issues.length)s+='<div class="ts-row"><span class="ts-label">Blocking</span><span class="ts-value" style="color:var(--red)">'+t.blocking_issues.map(b=>esc(b)).join('; ')+'</span></div>';
    // Deduplicate concerns: skip any that match the Why text or blocker text
    const whyText=(t.why||'').toLowerCase();
    const blockerText=(t.blocked_reason||'').toLowerCase();
    const uniqueConcerns=(t.concerns||[]).filter(c=>{const cl=c.toLowerCase();return cl!==whyText&&cl!==blockerText&&!whyText.includes(cl.slice(0,50));});
    if(uniqueConcerns.length){
      const visible=uniqueConcerns;
      const hidden=uniqueConcerns.slice(2);
      s+='<div class="ts-row"><span class="ts-label">Concerns</span><span class="ts-value"><ul style="margin:4px 0;padding-left:18px">'+visible.map(c=>'<li style="margin:2px 0">'+esc(c)+'</li>').join('')+'</ul>';
      if(hidden.length)s+='<details style="margin-top:2px"><summary style="font-size:10px;color:var(--accent);cursor:pointer">'+hidden.length+' more</summary><ul style="margin:4px 0;padding-left:18px">'+hidden.map(c=>'<li style="margin:2px 0">'+esc(c)+'</li>').join('')+'</ul></details>';
      s+='</span></div>';
    }
    // Actions — keep abandon available for threads that are clearly stuck, not as a default action on every live thread.
    const canReset=t.stage==='cycle_exhausted';
    const canAbandon=['blocked','rework'].includes(t.status)||t.stage==='cycle_exhausted';
    if(canReset||canAbandon){
      s+='<div class="ts-row" style="padding-top:8px">';
      if(canReset)s+='<button class="gbtn" onclick="resetCycles(\''+t.thread_id+'\')" title="Clear cycle count so the thread retries">Reset Cycles</button> ';
      if(canAbandon)s+='<button class="gbtn no" onclick="abandonThread(\''+t.thread_id+'\')" title="Mark as skipped permanently">Abandon</button>';
      s+='</div>';
    }
    s+='</div>';
    document.getElementById('thread-summary').innerHTML=s;
  }
  try{
    const detail=await(await fetch('/api/threads/'+tid)).json();
    mountThreadTranscript(detail);
    document.getElementById('thread-detail-body').innerHTML=renderProposalGroups(detail);
    const modeRoot=document.getElementById('thread-detail-mode');
    if(modeRoot&&!modeRoot.dataset.mode)modeRoot.dataset.mode='workflow';
    applyThreadMode(detail);
  }catch(e){}
}

// ── Exceptions ──
// ── PRs ──
function renderPRs(prs){
  const card=document.getElementById('pr-card'),body=document.getElementById('pr-body');
  if(!prs||!prs.length){card.style.display='none';return;}card.style.display='';
  body.innerHTML=prs.map(pr=>{const f=pr.failed,u=pr.status.startsWith('http');const thr=allThreads.find(t=>t.thread_id===pr.thread_id);const title=thr?thr.label:pr.branch||pr.thread_id.slice(0,8);return '<div class="pr-item"><div class="pr-info"><div class="pr-branch">'+esc(title)+'</div><div class="pr-status '+(f?'fail':u?'ok':'pending')+'">'+(u?'<a href="'+esc(pr.status)+'" target="_blank" style="color:var(--green)">'+esc(pr.status)+'</a>':esc(f?'Failed':pr.status))+'</div>'+(pr.detail?'<div class="pr-detail">'+esc(pr.detail)+'</div>':'')+'</div>'+(f?'<div class="pr-actions"><button class="gbtn retry" onclick="retryPR(\''+pr.thread_id+'\')">Retry</button></div>':'')+'</div>';}).join('');
}
async function retryPR(tid){const r=await fetch('/api/prs/'+tid+'/retry',{method:'POST'});const d=await r.json();if(d.error)alert('Retry failed: '+d.error);refreshOverview();}

// ── System ──
async function refreshSystem(){
  if(!lastSnap)return;const snap=lastSnap;
  const g=document.getElementById('agents-grid');
  g.innerHTML=snap.agents.length?snap.agents.map(a=>{const r=role(a.agent_id);const p=a.paused;const st=p?badge('paused'):badge(a.status);const btn=p?'<button class="gbtn ok" style="padding:3px 10px;font-size:10px;margin-top:8px" onclick="event.stopPropagation();resumeAgent(\''+a.agent_id+'\')">Resume</button>':'<button class="gbtn no" style="padding:3px 10px;font-size:10px;margin-top:8px" onclick="event.stopPropagation();pauseAgent(\''+a.agent_id+'\')">Pause</button>';return '<div class="acard" onclick="showAgent(\''+a.agent_id+'\')"><div class="acard-top"><div class="acard-avatar '+r+'">'+initials(r)+'</div><div><div class="acard-name">'+esc(a.agent_id)+'</div><div class="acard-role">'+roleTitle(r)+'</div></div><div style="margin-left:auto">'+st+'</div></div><div class="acard-stats">'+(a.heartbeat?'HB '+ago(a.heartbeat):'—')+' · '+(a.current_task?esc(a.current_task):'idle')+'</div>'+btn+'</div>';}).join(''):'<div class="empty">No agents</div>';
  // Metrics
  const mt=snap.metrics,keys=Object.keys(mt).sort(),mb=document.getElementById('metrics-body');
  if(!keys.length){mb.innerHTML='<div class="empty">'+(_firstLoad?'<span class="loading-pulse">Loading data...</span>':'No metrics yet')+'</div>';}
  else{const sec={Throughput:[],Errors:[],Gates:[],PRs:[]};keys.forEach(k=>{if(k.startsWith('messages:'))sec.Throughput.push([k,mt[k]]);else if(k.startsWith('errors:'))sec.Errors.push([k,mt[k]]);else if(k.startsWith('gates:'))sec.Gates.push([k,mt[k]]);else if(k.startsWith('prs:'))sec.PRs.push([k,mt[k]]);});let h='';for(const[n,items]of Object.entries(sec)){h+='<div class="msec"><h4>'+n+'</h4>';if(!items.length)h+='-';items.forEach(([k,v])=>{h+='<div class="mrow"><span class="mk">'+esc(k)+'</span><span class="mv"'+((k.includes('error')||k.includes('failed'))&&v>0?' style="color:var(--red)"':'')+'>'+v+'</span></div>';});h+='</div>';}mb.innerHTML=h;}
  // Streams
  const maxC=Math.max(1,...snap.streams.map(s=>s.count));
  document.getElementById('streams-body').innerHTML=snap.streams.map(s=>'<tr class="click" onclick="showStream(\''+s.name+'\')"><td><strong>'+esc(s.name)+'</strong></td><td>'+s.count+'</td><td><div class="sbar"><div class="sbar-fill" style="width:'+(s.count/maxC*100)+'%"></div></div></td></tr>').join('');
  refreshRedis();renderPipelineFull(snap.agents,snap.challengers||{},snap.backpressure);renderTimeline();
}
function closeAgentDetail(){document.getElementById('agent-overlay').style.display='none';document.getElementById('agent-detail-summary').innerHTML='';document.getElementById('agent-detail-body').innerHTML='';}
async function showAgent(aid){
  document.getElementById('agent-detail-title').textContent=aid;
  document.getElementById('agent-overlay').style.display='';
  // Find agent in snapshot for current status
  const agentInfo=lastSnap?lastSnap.agents.find(a=>a.agent_id===aid):null;
  const sm=document.getElementById('agent-detail-summary');
  if(sm&&agentInfo){
    const r=role(aid);const st=agentInfo.paused?'paused':agentInfo.status;
    const hb=agentInfo.heartbeat?ago(agentInfo.heartbeat)+' ago':'—';
    const task=agentInfo.current_task?esc(agentInfo.current_task):'idle';
    sm.innerHTML='<div class="ad-summary"><div class="ad-avatar '+r+'">'+initials(r)+'</div><div class="ad-info"><div class="ad-role">'+roleTitle(r)+'</div><div class="ad-status">'+badge(st)+' · heartbeat '+hb+'</div><div class="ad-task">'+task+'</div></div></div>';
  } else if(sm){sm.innerHTML='';}
  try{
    const events=await(await fetch('/api/agents/'+aid)).json();
    const body=document.getElementById('agent-detail-body');
    if(!events.length){body.innerHTML='<div class="empty">No activity recorded for this agent yet.</div>';return;}
    // Group events by thread
    const byThread={};const threadOrder=[];
    events.forEach(e=>{
      const tid=e.thread||'system';
      if(!byThread[tid]){byThread[tid]=[];threadOrder.push(tid);}
      byThread[tid].push(e);
    });
    // Collapse consecutive duplicate events within each thread group
    function dedup(evts){
      const result=[];
      evts.forEach(e=>{
        const prev=result[result.length-1];
        if(prev&&prev.type===e.type&&prev.thread===e.thread&&evDetail(prev.type,prev.payload)===evDetail(e.type,e.payload)){
          prev._count=(prev._count||1)+1;
        }else{
          result.push({...e,_count:1});
        }
      });
      return result;
    }
    let h='';
    threadOrder.forEach(tid=>{
      const evts=dedup(byThread[tid]);
      const threadLabel=tid==='system'?'System Events':(allThreads.find(t=>t.thread_id===tid)||{}).label||tid.slice(0,8);
      h+='<div class="ad-thread-group">';
      h+='<div class="ad-thread-head" onclick="goToThread(\''+esc(tid)+'\')">';
      h+='<span class="ad-thread-title">'+esc(threadLabel)+'</span>';
      h+='<span class="ad-thread-count">'+byThread[tid].length+' event'+(byThread[tid].length>1?'s':'')+'</span>';
      h+='</div>';
      const visible=evts.slice(-5);const hidden=evts.length-visible.length;
      if(hidden>0)h+='<div class="ad-older">'+hidden+' older event'+(hidden>1?'s':'')+' not shown</div>';
      visible.forEach(e=>{
        const ts=e.timestamp&&e.timestamp.length>19?e.timestamp.slice(11,19):e.timestamp||'';
        const count=e._count>1?' <span class="ad-repeat">&times;'+e._count+'</span>':'';
        h+='<div class="ad-event"><span class="ad-time">'+esc(ts)+'</span>'+badge(e.type.replace(/_/g,' '))+count+'<span class="ad-detail">'+evDetail(e.type,e.payload)+'</span></div>';
      });
      h+='</div>';
    });
    body.innerHTML=h;
  }catch(e){console.error(e);}
}
async function showStream(name){document.getElementById('stream-detail-name').textContent=name;document.getElementById('stream-detail-panel').classList.add('open');try{const msgs=await(await fetch('/api/streams/'+name)).json();if(!msgs.length){document.getElementById('stream-detail-body').innerHTML='<div class="empty">Empty</div>';return;}document.getElementById('stream-detail-body').innerHTML='<table><thead><tr><th>Time</th><th>Sender</th><th>Detail</th></tr></thead><tbody>'+msgs.map(m=>'<tr><td style="color:var(--subtle)">'+esc(m.timestamp)+'</td><td class="fi-sender '+m.role+'">'+esc(m.sender)+'</td><td>'+evDetail(m.type,m.payload)+'</td></tr>').join('')+'</tbody></table>';}catch(e){}}
async function refreshRedis(){try{const data=await(await fetch('/api/redis')).json();const cy=Object.entries(data.hashes['orchestrator:thread_cycles']||{}).sort();document.getElementById('redis-cycles').innerHTML=cy.length?cy.map(([k,v])=>'<tr><td>'+esc(k)+'</td><td'+(parseInt(v)>=3?' style="color:var(--red);font-weight:600"':'')+'>'+v+(parseInt(v)>=3?' (blocked)':'')+'</td><td><button class="gbtn ok" style="padding:2px 8px;font-size:10px" onclick="redisDelete(\'orchestrator:thread_cycles\',\''+esc(k)+'\')">Reset</button></td></tr>').join(''):'<tr><td colspan="3" class="empty">None</td></tr>';const pr=Object.entries(data.hashes['orchestrator:created_prs']||{}).sort();document.getElementById('redis-prs').innerHTML=pr.length?pr.map(([k,v])=>'<tr><td>'+esc(k.slice(0,12))+'</td><td'+(v.includes('failed')?' style="color:var(--red)"':'')+'>'+esc(v)+'</td><td><button class="gbtn no" style="padding:2px 8px;font-size:10px" onclick="redisDelete(\'orchestrator:created_prs\',\''+esc(k)+'\')">Del</button></td></tr>').join(''):'<tr><td colspan="3" class="empty">None</td></tr>';}catch(e){}}
async function redisDelete(key,field,opts){if(!confirm('Delete?'))return;const body=opts?Object.assign({key},opts):field?{key,field}:{key};await fetch('/api/redis/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});refreshRedis();refreshRedisInspector();}

async function refreshRedisInspector() {
  try {
    const data = await (await fetch('/api/redis')).json();

    // Streams
    const rs = document.getElementById('redis-streams');
    if (rs) rs.innerHTML = Object.entries(data.streams).map(([n,c]) => '<tr><td><strong>'+esc(n)+'</strong></td><td>'+c+'</td></tr>').join('') || '<tr><td colspan="2" class="empty">None</td></tr>';

    // Orchestrator keys — format epoch timestamps
    const ro = document.getElementById('redis-orch');
    if (ro) ro.innerHTML = Object.entries(data.keys.orchestrator||{}).map(([k,v]) => {
      let display=String(v);
      if(k.includes('heartbeat')&&parseFloat(v)>1e9)display=ago(parseFloat(v))+' ago';
      return '<tr><td><strong>'+esc(k)+'</strong></td><td style="color:var(--dim)">'+esc(display)+'</td></tr>';
    }).join('') || '<tr><td colspan="2" class="empty">None</td></tr>';

    // Agent keys
    const ra = document.getElementById('redis-agents');
    if (ra) {
      const agentKeys = Object.entries(data.keys.agents||{}).sort();
      ra.innerHTML = agentKeys.length ? agentKeys.map(([k,v]) => {
        let display=v;
        if(k.includes('heartbeat')&&parseFloat(v)>1e9)display=ago(parseFloat(v))+' ago';
        return '<tr><td><strong>'+esc(k)+'</strong></td><td style="color:var(--dim)">'+esc(display)+'</td></tr>';
      }).join('') : '<tr><td colspan="2" class="empty">None</td></tr>';
    }

    // Metrics hash
    const rm = document.getElementById('redis-metrics');
    if (rm) {
      const metrics = Object.entries(data.hashes['orchestrator:metrics']||{}).sort();
      rm.innerHTML = metrics.length ? metrics.map(([k,v]) => '<tr><td>'+esc(k)+'</td><td><strong>'+esc(v)+'</strong></td><td><button class="gbtn no" style="padding:2px 8px;font-size:10px" onclick="redisDelete(\'orchestrator:metrics\',\''+esc(k)+'\')">Del</button></td></tr>').join('') : '<tr><td colspan="3" class="empty">None</td></tr>';
    }

    // Thread cycles (already populated by refreshRedis, but refresh here too)
    const rc = document.getElementById('redis-cycles');
    if (rc) {
      const cycles = Object.entries(data.hashes['orchestrator:thread_cycles']||{}).sort();
      rc.innerHTML = cycles.length ? cycles.map(([k,v]) => {
        const blocked = parseInt(v)>=3;
        return '<tr><td>'+esc(k)+'</td><td'+(blocked?' style="color:var(--red);font-weight:600"':'')+'>'+v+(blocked?' (blocked)':'')+'</td><td><button class="gbtn ok" style="padding:2px 8px;font-size:10px" onclick="redisDelete(\'orchestrator:thread_cycles\',\''+esc(k)+'\')">Reset</button></td></tr>';
      }).join('') : '<tr><td colspan="3" class="empty">None</td></tr>';
    }

    // PR audit
    const rp = document.getElementById('redis-prs');
    if (rp) {
      const prs = Object.entries(data.hashes['orchestrator:created_prs']||{}).sort();
      rp.innerHTML = prs.length ? prs.map(([k,v]) => '<tr><td>'+esc(k.slice(0,12))+'</td><td'+(v.includes('failed')?' style="color:var(--red)"':'')+'>'+esc(v)+'</td><td><button class="gbtn no" style="padding:2px 8px;font-size:10px" onclick="redisDelete(\'orchestrator:created_prs\',\''+esc(k)+'\')">Del</button></td></tr>').join('') : '<tr><td colspan="3" class="empty">None</td></tr>';
    }
    // Active work sets
    const aw = document.getElementById('redis-active-work');
    if (aw) {
      const sets = data.active_work || {};
      const labels = {'orchestrator:active:designs':'Design','orchestrator:active:proposals':'Architect','orchestrator:active:tasks':'Tasks','orchestrator:active:reviews':'Reviews'};
      let awH = '';
      for (const [key, label] of Object.entries(labels)) {
        const members = sets[key] || [];
        const emptyCls = members.length ? '' : ' is-empty';
        awH += '<section class="wip-stage'+emptyCls+'">';
        awH += '<div class="wip-stage-head"><div><div class="wip-stage-title">'+label+'</div><div class="wip-stage-sub">'+(members.length?members.length+' active thread'+(members.length===1?'':'s'):'No active threads')+'</div></div><span class="wip-stage-count">'+members.length+'</span></div>';
        if (members.length) {
          awH += '<div class="wip-member-list">' + members.map(m => (
            '<div class="wip-member">' +
              '<button class="wip-thread" onclick="goToThread(\''+esc(m)+'\')">' +
                '<span class="wip-thread-label">Thread</span>' +
                '<span class="wip-thread-id">'+esc(m.slice(0,12))+'</span>' +
              '</button>' +
              '<button class="gbtn no wip-del" onclick="redisDelete(\''+esc(key)+'\',null,{member:\''+esc(m)+'\'})">Remove</button>' +
            '</div>'
          )).join('') + '</div>';
        } else {
          awH += '<div class="wip-empty">Nothing is currently queued in this stage.</div>';
        }
        awH += '</section>';
      }
      aw.innerHTML = awH;
    }
  } catch(e) { console.error(e); }
}

// ── Pipeline ──
const PIPELINE_ROLES=['pm','product_designer','architect','developer','reviewer'],PIPELINE_CONNECTIONS=[['pm','product_designer'],['pm','architect'],['product_designer','architect'],['architect','developer'],['developer','reviewer'],['reviewer','developer']],DELIBERATION_ROLES=['pm','architect'];
const PIPELINE_QUEUE_MAP={pm_product_designer:'designs',product_designer_architect:'proposals',architect_developer:'tasks',developer_reviewer:'reviews'};
const PIPELINE_STAGE_QUEUE={product_designer:'designs',architect:'proposals',developer:'tasks',reviewer:'reviews'};
const PIPELINE_QUEUE_LABELS={designs:'design',proposals:'proposal',tasks:'task',reviews:'review'};
function queueBadgeText(stage,q){
  if(!q||!q.active)return '';
  if(q.gated)return 'FULL';
  const noun=PIPELINE_QUEUE_LABELS[stage]||'item';
  return q.active+' '+noun+(q.active===1?'':'s');
}
function _renderPipeline(cId,sId,agents,challengers,opts){
  const compact=opts.compact||false,showLabels=opts.labels!==false,showChallengers=opts.challengers!==false,bp=opts.backpressure||{};
  const canvas=document.getElementById(cId),svg=document.getElementById(sId);if(!canvas||!svg)return;
  const w=canvas.offsetWidth,h=canvas.offsetHeight;if(w<80||h<60)return;
  const byRole={};PIPELINE_ROLES.forEach(r=>byRole[r]=[]);agents.forEach(a=>{const r=role(a.agent_id);if(byRole[r])byRole[r].push(a);});
  const nR=compact?24:34,chOff=showChallengers?(compact?24:34):0,cy=compact?(h/2+8):(h/2+18),sp=Math.min(compact?175:280,(w-180)/(PIPELINE_ROLES.length-1)),sx=w/2-(sp*(PIPELINE_ROLES.length-1))/2;
  // Per-role layout: above = labels above bar then circle; below = circle then labels below bar
  const _nodeDir={pm:'above',product_designer:'below',architect:'above',developer:'below',reviewer:'above'};
  const pos={},cpos={};let html='';
  PIPELINE_ROLES.forEach((r,i)=>{const x=sx+i*sp,ra=byRole[r]||[];const stageQueue=PIPELINE_STAGE_QUEUE[r];const q=stageQueue&&bp[stageQueue];const qBadge=q&&q.active?'<div class="node-queue '+(q.gated?'full':q.active/q.limit>=0.5?'warn':'')+'">'+queueBadgeText(stageQueue,q)+'</div>':'';
  const dir=_nodeDir[r]||'above';
  const labelH=compact?28:40;
  // For 'above': top = cy - nR - labelH (labels above, circle at cy)
  // For 'below': top = cy - nR (circle at cy, labels below)
  const nodeTop=dir==='above'?(cy-nR-labelH):(cy-nR);
  if(ra.length<=1){const a=ra[0],psd=a&&a.paused,st=psd?'paused':(a?a.status:'stopped'),cl=st==='busy'?'busy':st==='active'?'active':st==='paused'?'paused':'';const meta=a?a.agent_id+(a.status&&a.status!==st?' · '+a.status:''):(st||'—');pos[r]={x,y:cy};
  const labelHtml=showLabels?'<div class="node-text"><div class="node-label">'+roleTitle(r)+'</div><div class="node-status">'+esc(meta)+'</div>'+qBadge+'</div>':'';
  const circleHtml='<div class="node-circle '+r+' '+cl+'">'+initials(r)+'<div class="node-dot"></div></div>';
  const inner=dir==='above'?(labelHtml+circleHtml):(circleHtml+labelHtml);
  html+='<div class="pipeline-node '+dir+'" style="left:'+(x-nR)+'px;top:'+nodeTop+'px" onclick="onNodeClick(\''+(a?a.agent_id:r)+'\')">'+inner+'</div>';}
  else{const gap=compact?64:110,tH=(ra.length-1)*gap,sY=cy-tH/2;pos[r]={x,y:cy};ra.forEach((a,j)=>{const ny=sY+j*gap,psd2=a.paused,ast=psd2?'paused':a.status,cl=ast==='busy'?'busy':ast==='active'?'active':ast==='paused'?'paused':'';const badge=j===0?qBadge:'';
  const nTop=dir==='above'?(ny-nR-labelH):(ny-nR);
  const labelHtml=showLabels?'<div class="node-text"><div class="node-label">'+roleTitle(r)+'</div><div class="node-status">'+esc(a.agent_id)+' · '+esc(a.status)+'</div>'+badge+'</div>':'';
  const circleHtml='<div class="node-circle '+r+' '+cl+'">'+initials(r)+'<div class="node-dot"></div></div>';
  const inner=dir==='above'?(labelHtml+circleHtml):(circleHtml+labelHtml);
  html+='<div class="pipeline-node '+dir+'" style="left:'+(x-nR)+'px;top:'+nTop+'px" onclick="onNodeClick(\''+a.agent_id+'\')">'+inner+'</div>';});}
  if(showChallengers&&DELIBERATION_ROLES.includes(r)){const ch=(challengers||{})[r],cA=ch&&ch.active,cR=ch&&ch.recent;const chDir=_nodeDir[r]||'above';const cY=chDir==='above'?cy-(compact?110:148):cy+(compact?110:148);cpos[r]={x,y:cY};html+='<div class="challenger-node" style="left:'+(x-(compact?14:16))+'px;top:'+(cY-(compact?14:16))+'px"><div class="challenger-circle '+(cA?'active':cR?'recent':'')+'">CH</div><div class="challenger-label">challenger</div></div>';}});
  let sv='';PIPELINE_CONNECTIONS.forEach(([f,t])=>{const p1=pos[f],p2=pos[t];if(!p1||!p2)return;const act=(byRole[f]||[]).some(a=>a.status==='busy')||(byRole[t]||[]).some(a=>a.status==='busy');
  // Queue depth badge on forward connections
  const qKey=PIPELINE_QUEUE_MAP[f+'_'+t];const q=qKey&&bp[qKey];
  const isRework=f==='reviewer'&&t==='developer';
  if(q&&q.active>0){const mx=(p1.x+p2.x)/2,my=(p1.y+p2.y)/2+20;const cls=q.gated?'full':q.active/q.limit>=0.5?'warn':'';html+='<div class="pipeline-queue-badge '+cls+'" style="left:'+mx+'px;top:'+my+'px">'+queueBadgeText(qKey,q)+'</div>';}
  if(isRework){const mx=(p1.x+p2.x)/2,my=(p1.y+p2.y)/2-20;html+='<div class="pipeline-edge-label rework" style="left:'+mx+'px;top:'+my+'px">Rework loop</div>';}
  sv+='<line x1="'+p1.x+'" y1="'+p1.y+'" x2="'+p2.x+'" y2="'+p2.y+'" class="'+(act?'active':'')+((q&&q.gated)?' gated':'')+(isRework?' rework':'')+'"/>';});
  if(showChallengers)DELIBERATION_ROLES.forEach(r=>{const p=pos[r],cp=cpos[r];if(!p||!cp)return;sv+='<line x1="'+p.x+'" y1="'+p.y+'" x2="'+cp.x+'" y2="'+cp.y+'" class="challenger'+((challengers||{})[r]&&(challengers||{})[r].active?' active':'')+'"/>';});
  svg.innerHTML=sv;canvas.querySelectorAll('.pipeline-node,.challenger-node,.pipeline-queue-badge,.pipeline-edge-label,.pipeline-legend').forEach(n=>n.remove());canvas.insertAdjacentHTML('beforeend',html+'<div class="pipeline-legend"><span><i class="leg-line"></i> forward flow</span><span><i class="leg-line rework"></i> rework</span><span><i class="leg-dot full"></i> gated backlog</span></div>');
  const pl=canvas.querySelector('.pipeline-loading');if(pl)pl.style.display='none';
}
function renderPipelineMini(a,c,bp){_renderPipeline('pipeline-canvas','pipeline-svg',a,c,{labels:true,challengers:true,backpressure:bp});}
function renderPipelineFull(a,c,bp){_renderPipeline('pipeline-canvas-sys','pipeline-svg-sys',a,c,{labels:true,challengers:true,backpressure:bp});}
function onNodeClick(id){switchView('system');showAgent(id);}

// ── Exceptions ──
async function refreshExceptions(){
  try{
    const exc=await(await fetch('/api/exceptions')).json();
    // Approvals
    const ea=document.getElementById('exc-approvals');
    // Fetch full gates list for resolved display
    let allGates=[];try{allGates=await(await fetch('/api/gates')).json();}catch(e){}
    const pendingGates=allGates.filter(g=>g.outcome==='pending');
    const resolvedGates=allGates.filter(g=>g.outcome!=='pending');
    const actionLabel=a=>({'create_pr':'Create a pull request','protected_file':'Modify a protected file','large_change':'Commit a large change (>10 files)','delete_files':'Delete files','modify_database':'Modify database'}[a]||a);
    const approveEffect=a=>({'create_pr':'PR will be created on GitHub','protected_file':'Agent will proceed with the file change','large_change':'Agent will commit the changes','delete_files':'Files will be deleted','modify_database':'Database will be modified'}[a]||'Agent will proceed');
    const denyEffect='Agent will skip this action and continue';
    const outcomeBadge=o=>o==='approved'?'<span style="color:var(--green);font-weight:600">Approved</span>':o==='denied'?'<span style="color:var(--red);font-weight:600">Denied</span>':o==='timed_out'?'<span style="color:var(--amber);font-weight:600">Timed Out</span>':'<span>'+esc(o)+'</span>';
    let approvalHtml='';
    if(pendingGates.length){approvalHtml+='<h3 class="exc-title">Needs Approval <span class="exc-count">'+pendingGates.length+'</span></h3>'+pendingGates.map(g=>'<div class="exc-card urgent"><div class="ec-info"><div class="ec-title">'+esc(actionLabel(g.action))+'</div><div class="ec-detail">'+esc(g.reason||'')+'</div>'+(g.context?'<div class="ec-meta">'+esc(typeof g.context==='string'?g.context:JSON.stringify(g.context))+'</div>':'')+'<div class="ec-meta" style="font-size:10px;margin-top:4px;color:var(--subtle)">From: '+esc(g.sender)+' · Thread: '+esc(g.thread||'—')+'</div></div><div class="ec-action"><button class="gbtn ok" onclick="gateAction(\''+g.gate_id+'\',\'approve\')" title="'+esc(approveEffect(g.action))+'">Approve</button> <button class="gbtn no" onclick="gateAction(\''+g.gate_id+'\',\'deny\')" title="'+esc(denyEffect)+'">Deny</button></div></div>').join('');}
    if(resolvedGates.length){approvalHtml+='<h3 class="exc-title" style="margin-top:16px">Resolved Gates <span class="exc-count">'+resolvedGates.length+'</span></h3>'+resolvedGates.map(g=>'<div class="exc-card diagnostic"><div class="ec-info"><div class="ec-title">'+esc(actionLabel(g.action))+' '+outcomeBadge(g.outcome)+'</div><div class="ec-detail">'+esc(g.reason||'')+'</div><div class="ec-meta" style="font-size:10px;margin-top:4px;color:var(--subtle)">From: '+esc(g.sender)+' · Thread: '+esc(g.thread||'—')+(g.resolution_at?' · Resolved: '+esc(g.resolution_at):'')+'</div></div></div>').join('');}
    ea.innerHTML=approvalHtml;
    // Failed PRs
    const ep=document.getElementById('exc-failed-prs');
    ep.innerHTML=exc.failed_prs.length?'<h3 class="exc-title">Failed PRs <span class="exc-count">'+exc.failed_prs.length+'</span></h3>'+exc.failed_prs.map(p=>'<div class="exc-card urgent"><div class="ec-info"><div class="ec-title">PR failed: '+esc(p.branch||p.thread_id.slice(0,8))+'</div><div class="ec-detail">'+esc(p.detail)+'</div></div><div class="ec-action"><button class="gbtn retry" onclick="retryPR(\''+p.thread_id+'\')">Retry</button></div></div>').join(''):'';
    // Blocked threads — uses backend why/next_step/blocked_reason
    const eb=document.getElementById('exc-blocked');
    eb.innerHTML=exc.blocked_threads.length?'<h3 class="exc-title">Blocked Threads <span class="exc-count">'+exc.blocked_threads.length+'</span></h3>'+exc.blocked_threads.map(t=>{
      const reason=t.blocked_reason||t.why||'Unknown';
      const next=t.next_step||'';
      return '<div class="exc-card warn"><div class="ec-info"><div class="ec-title">'+esc(t.label||t.thread_id.slice(0,8))+'</div><div class="ec-detail" style="color:var(--red)">'+esc(reason)+'</div>'+(next?'<div class="ec-meta">'+esc(next)+'</div>':'')+'</div><div class="ec-action"><button class="gbtn" onclick="resetCycles(\''+t.thread_id+'\')" title="Clear cycle count so the thread retries">Reset</button><button class="gbtn no" onclick="abandonThread(\''+t.thread_id+'\')" title="Mark as skipped permanently">Abandon</button><button class="gbtn retry" onclick="goToThread(\''+t.thread_id+'\')">View</button></div></div>';
    }).join(''):'';
    // Stale agents
    const es=document.getElementById('exc-stale');
    es.innerHTML=exc.stale_agents.length?'<h3 class="exc-title">Idle Agents <span class="exc-count">'+exc.stale_agents.length+'</span></h3>'+exc.stale_agents.map(a=>'<div class="exc-card diagnostic"><div class="ec-info"><div class="ec-title">'+esc(a.agent_id)+'</div><div class="ec-detail">Inactive for '+a.heartbeat_age+'s (threshold: '+(a.threshold||'?')+'s)</div></div></div>').join(''):'';
    // Empty state

    if(!exc.approvals.length&&!exc.failed_prs.length&&!exc.blocked_threads.length&&!exc.stale_agents.length)
      ea.innerHTML='<div class="empty" style="padding:40px">No exceptions right now. When approvals are pending, PRs fail, or threads get blocked, they appear here.</div>';

  }catch(e){console.error(e);}
}
async function gateAction(id,action){const h={method:'POST'};const hdrs=authHeaders();if(hdrs)h.headers=hdrs;const r=await fetch('/api/gates/'+id+'/'+action,h);if(r.status===401){const t=prompt('Gate token required:');if(t){localStorage.setItem('gate_token',t);return gateAction(id,action);}}refreshExceptions();refreshOverview();}

// ── Orchestrator control ──
// Follows the same token pattern as gateAction: stored in localStorage, prompted for on
// 401, retried once. That means pairing a phone once works for approvals AND for this.
async function startOrchestrator(){
  const btn=document.getElementById('btn-start-orch');
  const msg=document.getElementById('start-orch-msg');
  const say=(t)=>{if(msg)msg.textContent=t;};
  if(btn){btn.disabled=true;btn.textContent='Starting...';}
  say('');
  try{
    const h={method:'POST'};
    const hdrs=authHeaders();
    if(hdrs)h.headers=hdrs;
    const r=await fetch('/api/orchestrator/start',h);
    if(r.status===401){
      const t=prompt('Dashboard token required. Run  .\\AIO.ps1 pair  and paste the token:');
      if(t&&isValidToken(t.trim())){localStorage.setItem('gate_token',t.trim());return startOrchestrator();}
      say(t?'that is not a valid token (expected 64 hex characters)':'cancelled');
      if(btn){btn.disabled=false;btn.textContent='Start orchestrator';}
      return;
    }
    const data=await r.json().catch(()=>({}));
    if(!r.ok||!data.started){
      window.orchStarting=false;say(data.error||('failed (HTTP '+r.status+')'));
      if(btn){btn.disabled=false;btn.textContent='Start orchestrator';}
      return;
    }
    // Startup runs preflight, cleans stale worktrees and connects every agent, so the
    // heartbeat does not appear immediately. Poll rather than claim success: saying
    // "started" before it is up is how a failed start looks like a working one.
    // Tell the rest of the dashboard a start is in flight, so the 5s refresh shows
    // "starting" rather than repainting "disconnected" over the top of this message.
    window.orchStarting=true;
    if(typeof refreshOverview==='function')refreshOverview();
    say('started (pid '+data.pid+'), waiting for it to come up...');
    for(let i=0;i<40;i++){
      await new Promise(r=>setTimeout(r,1500));
      try{
        const s=await (await fetch('/api/orchestrator')).json();
        if(s.running){window.orchStarting=false;window.orchStartError='';say('');if(typeof refreshOverview==='function')refreshOverview();return;}
        // The process DIED. Say so now, with the reason, instead of showing "starting"
        // for the rest of the 60s and then a timeout that explains nothing. Startup can
        // fail well past the spawn check: a missing CLI surfaces 5-20s into preflight.
        if(s.failed){
          window.orchStarting=false;
          window.orchStartError=s.error||'orchestrator exited during startup';
          say(window.orchStartError);
          if(btn){btn.disabled=false;btn.textContent='Start orchestrator';}
          if(typeof refreshOverview==='function')refreshOverview();
          return;
        }
      }catch(e){}
    }
    window.orchStarting=false;
    say('started, but no heartbeat after 60s - check the orchestrator window');
    if(btn){btn.disabled=false;btn.textContent='Start orchestrator';}
  }catch(e){
    window.orchStarting=false;say('failed: '+e.message);
    if(btn){btn.disabled=false;btn.textContent='Start orchestrator';}
  }
}

// ── Thread actions ──
async function resetCycles(tid){try{const h={method:'POST'};const hdrs=authHeaders();if(hdrs)h.headers=hdrs;const r=await fetch('/api/threads/'+tid+'/reset-cycles',h);if(r.status===401){const t=prompt('Gate token required:');if(t){localStorage.setItem('gate_token',t);return resetCycles(tid);}}if(!r.ok){const b=await r.text();console.error('resetCycles failed:',r.status,b);alert('Reset failed: '+r.status);}else{refreshExceptions();refreshOverview();}}catch(e){console.error('resetCycles error:',e);alert('Reset error: '+e.message);}}
async function abandonThread(tid){if(!confirm('Abandon this thread? It will be marked as skipped permanently.'))return;try{const h={method:'POST'};const hdrs=authHeaders();if(hdrs)h.headers=hdrs;const r=await fetch('/api/threads/'+tid+'/abandon',h);if(r.status===401){const t=prompt('Gate token required:');if(t){localStorage.setItem('gate_token',t);return abandonThread(tid);}}if(!r.ok){alert('Abandon failed: '+r.status);}else{refreshExceptions();refreshOverview();}}catch(e){alert('Abandon error: '+e.message);}}

// ── Agent pause/resume ──
async function pauseAgent(id){await fetch('/api/agents/'+id+'/pause',{method:'POST'});refreshOverview();}
async function resumeAgent(id){await fetch('/api/agents/'+id+'/resume',{method:'POST'});refreshOverview();}
async function togglePauseAll(){
  if(!lastSnap||!lastSnap.agents)return;
  const anyActive=lastSnap.agents.some(a=>!a.paused);
  const action=anyActive?'pause':'resume';
  await Promise.all(lastSnap.agents.map(a=>fetch('/api/agents/'+a.agent_id+'/'+action,{method:'POST'})));
  refreshOverview();
}

// ── Token usage ──
function fmtTok(n){if(!n)return '0';if(n>=1e6)return (n/1e6).toFixed(1)+'M';if(n>=1e3)return (n/1e3).toFixed(1)+'k';return String(n);}
function fmtCost(mc){if(!mc)return '';const d=mc/100000;return d>=1?'$'+d.toFixed(2):d>=0.01?'$'+d.toFixed(3):'$'+d.toFixed(4);}
const ROLE_COLORS={pm:'var(--blue)',product_designer:'var(--cyan)',architect:'var(--orange)',developer:'var(--green)',reviewer:'var(--purple)'};
async function renderAudit(){
  const el=document.getElementById('audit-container');if(!el)return;
  let findings;
  try{findings=await(await fetch('/api/audit')).json();}catch(e){return;}
  const facts=findings.filter(f=>f.kind==='fact');
  const critical=facts.filter(f=>f.severity==='critical').length;
  const warnings=facts.filter(f=>f.severity==='warning').length;

  // Overview: compact verdict card
  if(!facts.length){
    el.innerHTML='<div class="sh-verdict sh-ok"><div class="sh-icon">&#10003;</div><div class="sh-body"><div class="sh-title">All clear</div><div class="sh-sub">Checked pipeline health, blocked threads, failed PRs, and agent state. No issues found.</div></div></div>';
  } else {
    const severity=critical?'critical':'warn';
    const icon=critical?'\u26d4':'\u26a0';
    const summary=[];
    if(critical)summary.push(critical+' critical');
    if(warnings)summary.push(warnings+' warning'+(warnings>1?'s':''));
    const verdict = findings.length===1 ? '1 issue needs attention' : findings.length+' issues need attention';
    el.innerHTML='<div class="sh-verdict sh-'+severity+'"><div class="sh-icon">'+icon+'</div><div class="sh-body"><div class="sh-title">'+verdict+'</div><div class="sh-sub">Checked pipeline health, blocked threads, failed PRs, and agent state.</div><a href="#" class="sh-link" onclick="switchView(\'system\');return false">View details in System &rarr;</a></div></div>';
  }
  renderAuditFull(findings);
}
function renderAuditFull(findings){
  const el=document.getElementById('audit-full');if(!el)return;
  if(!findings||!findings.length){el.innerHTML='<div class="sh-verdict sh-ok" style="margin:0"><div class="sh-icon">&#10003;</div><div class="sh-body"><div class="sh-title">System healthy</div><div class="sh-sub">All checks passed. No issues detected.</div></div></div>';return;}
  const facts=findings.filter(f=>f.kind==='fact');
  const obs=findings.filter(f=>f.kind==='observation');
  let h='';
  if(facts.length){
    h+='<div class="sh-findings">';
    facts.forEach(f=>{
      const cls=f.severity==='critical'?'sh-finding-critical':f.severity==='warning'?'sh-finding-warn':'sh-finding-info';
      const icon=f.severity==='critical'?'\u26d4':f.severity==='warning'?'\u26a0':'\u2713';
      h+='<div class="sh-finding '+cls+'">';
      h+='<div class="sh-finding-icon">'+icon+'</div>';
      h+='<div class="sh-finding-body">';
      h+='<div class="sh-finding-title">'+esc(f.summary)+'</div>';
      if(f.detail)h+='<div class="sh-finding-detail">'+esc(f.detail)+'</div>';
      if(f.thread_id)h+='<a href="#" class="sh-finding-link" onclick="goToThread(\''+f.thread_id+'\');return false">View thread '+esc(f.thread_id.slice(0,8))+' &rarr;</a>';
      h+='</div></div>';
    });
    h+='</div>';
  }
  if(obs.length){
    h+='<div class="sh-observations">';
    obs.forEach(f=>{
      h+='<div class="sh-obs"><span class="sh-obs-icon">&#10003;</span> '+esc(f.summary)+'</div>';
    });
    h+='</div>';
  }
  el.innerHTML=h;
}

const CLI_COLORS={claude:'#5046e5',codex:'#f97316'};

async function renderTimeline(){
  const el=document.getElementById('timeline-container');if(!el)return;
  let traces;
  try{traces=await(await fetch('/api/traces?limit=100')).json();}catch(e){return;}
  if(!traces.length){el.innerHTML='<div class="empty">No AI interactions recorded yet. Traces appear once agents start processing work.</div>';return;}

  // Group by agent, find time range
  const agents={};
  let tMin=Infinity,tMax=-Infinity;
  traces.forEach(t=>{
    if(!agents[t.agent])agents[t.agent]={role:t.role,calls:[]};
    const ts=new Date(t.timestamp).getTime();
    const end=ts+(t.duration_ms||1000);
    if(ts<tMin)tMin=ts;if(end>tMax)tMax=end;
    agents[t.agent].calls.push({...t,ts,end});
  });

  const span=Math.max(tMax-tMin,1000);
  const agentNames=Object.keys(agents).sort();
  const rowH=28,padL=100,padR=12,barH=18;
  const W=el.clientWidth||600;
  const barW=W-padL-padR;
  const H=agentNames.length*rowH+8;

  let h='<div class="tl-chart" style="position:relative;height:'+H+'px;overflow:hidden">';

  agentNames.forEach((name,i)=>{
    const a=agents[name];
    const y=i*rowH+4;
    const roleColor=ROLE_COLORS[a.role]||'var(--dim)';
    h+='<div class="tl-label" style="position:absolute;left:0;top:'+y+'px;width:'+(padL-8)+'px;height:'+rowH+'px;line-height:'+rowH+'px;font-size:11px;font-weight:600;color:'+roleColor+';text-align:right;overflow:hidden;white-space:nowrap">'+esc(name)+'</div>';
    h+='<div style="position:absolute;left:'+padL+'px;top:'+(y+rowH-1)+'px;width:'+barW+'px;height:1px;background:var(--border)"></div>';

    a.calls.forEach(c=>{
      const x=padL+((c.ts-tMin)/span)*barW;
      const w=Math.max(((c.end-c.ts)/span)*barW,3);
      const bg=CLI_COLORS[c.cli_backend]||'#888';
      const opacity=c.delib_model==='challenger'?0.5:1;
      const border=c.is_error?';border:2px solid var(--red)':'';
      const tok=c.input_tokens||c.output_tokens?(fmtTok(c.input_tokens)+'/'+fmtTok(c.output_tokens)):'';
      const dur=c.duration_ms>=1000?((c.duration_ms/1000).toFixed(1)+'s'):(c.duration_ms+'ms');
      const cost=c.cost_usd?(' $'+c.cost_usd.toFixed(3)):'';
      const model=c.model||c.cli_backend||'?';
      const round=c.deliberation_round?(' R'+c.deliberation_round+(c.delib_model?' '+c.delib_model:'')):'';
      const tip=model+' '+dur+' '+tok+cost+round;
      h+='<div class="tl-bar" title="'+esc(tip)+'" style="position:absolute;left:'+x.toFixed(1)+'px;top:'+(y+((rowH-barH)/2)).toFixed(1)+'px;width:'+w.toFixed(1)+'px;height:'+barH+'px;background:'+bg+';opacity:'+opacity+';border-radius:3px;cursor:pointer'+border+'"></div>';
    });
  });

  h+='</div>';
  // Legend
  h+='<div style="display:flex;gap:16px;margin-top:4px;font-size:10px;color:var(--dim)">';
  h+='<span><span style="display:inline-block;width:10px;height:10px;background:'+CLI_COLORS.claude+';border-radius:2px;vertical-align:middle"></span> Claude</span>';
  h+='<span><span style="display:inline-block;width:10px;height:10px;background:'+CLI_COLORS.codex+';border-radius:2px;vertical-align:middle"></span> Codex</span>';
  h+='<span style="opacity:0.5"><span style="display:inline-block;width:10px;height:10px;background:#888;border-radius:2px;vertical-align:middle"></span> Challenger (faded)</span>';
  h+='</div>';
  el.innerHTML=h;
}

function renderTokenUsage(mt){
  const el=document.getElementById('token-usage');if(!el)return;
  const tIn=parseInt(mt['tokens_in:total'])||0,tOut=parseInt(mt['tokens_out:total'])||0,tCost=parseInt(mt['cost_mc:total'])||0;
  if(!tIn&&!tOut){el.innerHTML='<div class="empty">'+ (_firstLoad?'<span class="loading-pulse">Loading usage data...</span>':'No token usage recorded yet. Cost data appears once agents make CLI calls.') +'</div>';return;}
  const total=tIn+tOut;

  // Find biggest spender
  let bigRole='',bigVal=0;
  ['pm','product_designer','architect','developer','reviewer'].forEach(r=>{
    const rv=(parseInt(mt['tokens_in:'+r])||0)+(parseInt(mt['tokens_out:'+r])||0);
    if(rv>bigVal){bigVal=rv;bigRole=r;}
  });

  let h='<div class="tok-header">';
  if(tCost) h+='<div class="tok-total-val">'+fmtCost(tCost)+'</div>';
  h+='<div class="tok-total-sub">'+fmtTok(total)+' tokens · in: '+fmtTok(tIn)+' · out: '+fmtTok(tOut)+'</div>';
  if(bigRole) h+='<div class="tok-total-sub">Biggest spender: <strong>'+bigRole+'</strong> ('+fmtTok(bigVal)+' · '+Math.round(bigVal/total*100)+'%)</div>';
  h+='</div>';

  // Per-role bars
  h+='<div class="tok-bars">';
  ['pm','product_designer','architect','developer','reviewer'].forEach(r=>{
    const i=parseInt(mt['tokens_in:'+r])||0,o=parseInt(mt['tokens_out:'+r])||0,c=parseInt(mt['cost_mc:'+r])||0;
    const rt=i+o;if(!rt)return;
    const pct=Math.max(2,Math.round(rt/total*100));
    const color=ROLE_COLORS[r]||'var(--dim)';
    h+='<div class="tok-row">';
    h+='<div class="tok-row-label">'+r+'</div>';
    h+='<div class="tok-row-bar"><div class="tok-row-fill" style="width:'+pct+'%;background:'+color+'"></div></div>';
    h+='<div class="tok-row-val">'+fmtTok(rt)+(c?' <span class="tok-cost">'+fmtCost(c)+'</span>':'')+'</div>';
    h+='</div>';
  });
  h+='</div>';

  el.innerHTML=h;
}

// ── SSE with reconnect ──
let _refreshTimer=null;
function debouncedRefresh(){if(_refreshTimer)return;_refreshTimer=setTimeout(()=>{_refreshTimer=null;refreshOverview();},1000);}
let _evtSource=null;
let _sseRetryDelay=1000;
let _sseReconnectAttempts=0;
const SSE_PROLONGED_DISCONNECT_THRESHOLD=5;
function connectSSE(){
  if(_evtSource){try{_evtSource.close();}catch(e){}}
  _evtSource=new EventSource('/api/events');
  _evtSource.onopen=function(){_sseRetryDelay=1000;_sseReconnectAttempts=0;document.getElementById('sb-dot').className='status-dot ok';document.getElementById('sb-status').textContent='Connected';};
  _evtSource.onmessage=function(msg){const ev=JSON.parse(msg.data);eventCount++;document.getElementById('event-count').textContent=eventCount;addFeedItem('events-feed',ev);debouncedRefresh();};
  _evtSource.onerror=function(){_sseReconnectAttempts++;document.getElementById('sb-dot').className='status-dot bad';if(_sseReconnectAttempts>SSE_PROLONGED_DISCONNECT_THRESHOLD){document.getElementById('sb-status').textContent='Connection lost. Attempting to reconnect. If this continues, check that the server is running and refresh the page.';}else{document.getElementById('sb-status').textContent='Reconnecting...';}  _evtSource.close();setTimeout(connectSSE,_sseRetryDelay);_sseRetryDelay=Math.min(_sseRetryDelay*2,30000);};
}
connectSSE();

// ── Keyboard shortcuts ──
document.addEventListener('keydown',function(e){
  // Don't intercept when typing in inputs
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA'||e.target.isContentEditable)return;
  // Escape closes overlays and panels
  if(e.key==='Escape'){
    const threadOv=document.getElementById('thread-overlay');
    if(threadOv&&threadOv.style.display!=='none'){closeThreadDetail();return;}
    const agentOv=document.getElementById('agent-overlay');
    if(agentOv&&agentOv.style.display!=='none'){closeAgentDetail();return;}
    document.querySelectorAll('.panel.open').forEach(p=>p.classList.remove('open'));
    return;
  }
  // 1-4 switches views
  const viewKeys={'1':'overview','2':'work','3':'exceptions','4':'system'};
  if(viewKeys[e.key]&&!e.ctrlKey&&!e.metaKey&&!e.altKey){switchView(viewKeys[e.key]);return;}
});

// ── Pairing ──
// A 64-character hex token is not something anyone types, least of all on a phone. If the
// page is opened with ?token=..., store it for this device and strip it from the URL.
//
// Stripping matters: a token left in the address bar reaches browser history, travels
// when someone copies the URL, and can appear in a Referer header later. replaceState
// rather than pushState means Back does not restore it either.
// A token is 64 hex characters. Anything else is a copy-paste accident - most often a URL
// abbreviated with an ellipsis in a chat message, which stores the literal character.
// That matters more than it sounds: a header value outside Latin-1 makes fetch throw a
// bare TypeError BEFORE any request is sent, so the failure looks like a network problem
// and nothing reaches the server to explain it.
function isValidToken(t){ return typeof t==='string' && /^[0-9a-fA-F]{32,128}$/.test(t); }

// Returns auth headers, or null if the stored token is unusable. Clears a bad one so the
// next call prompts instead of failing the same way forever.
function authHeaders(){
  const t=localStorage.getItem('gate_token');
  if(!t)return null;
  if(!isValidToken(t)){localStorage.removeItem('gate_token');return null;}
  return {'Authorization':'Bearer '+t};
}

(function pairFromUrl(){
  try{
    const u=new URL(window.location.href);
    const t=u.searchParams.get('token');
    if(!t)return;
    if(!isValidToken(t)){
      console.warn('Ignoring an invalid token in the URL:',JSON.stringify(t));
      alert('That pairing link has a placeholder instead of a real token.\n\nRun  .\\AIO.ps1 pair  and use the link it copies to the clipboard.');
      return;
    }
    localStorage.setItem('gate_token',t);
    u.searchParams.delete('token');
    window.history.replaceState({},document.title,u.pathname+(u.search||'')+u.hash);
    console.info('Dashboard token stored for this device.');
  }catch(e){}
})();

// ── Init ──
refreshOverview();
setInterval(refreshOverview,5000);
window.addEventListener('resize',()=>{if(lastSnap){renderPipelineMini(lastSnap.agents,lastSnap.challengers||{},lastSnap.backpressure);renderPipelineFull(lastSnap.agents,lastSnap.challengers||{},lastSnap.backpressure);}});
