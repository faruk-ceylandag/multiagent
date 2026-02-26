/* app.js — Multi-Agent Dashboard */
const HUB = location.origin;
const $ = id => document.getElementById(id);

let data = {agents:{},agent_names:[]}, tab = 'logs', sel = '', inbox = [], reviewData = [];
let reviewSelections = new Map(); // Map<changeId, Set<filePath>>
let planSelections = new Map(); // Map<planId, Set<stepIndex>> — tracks which plan steps are selected
let logLines = {}, logSearch = '', soundOn = false, notifOn = false;
let mergedMode = false, _inboxAgent = '';
let activeWorkspace = 'all';
let _prevTaskStatuses = {};
let _ws = null, _wsRetries = 0, _wsMaxRetries = 10, _wsConnected = false;
let _httpPollTimer = null;
const _isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
const _mod = _isMac ? '⌘' : 'Ctrl';

// ══════════════════════════════════
//  TOAST NOTIFICATIONS
// ══════════════════════════════════
function toast(msg, type='info', duration=4000) {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    document.body.appendChild(container);
  }
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  const icon = type === 'success' ? '✓' : type === 'error' ? '✗' : type === 'warn' ? '⚠' : 'ℹ';
  el.innerHTML = `<span class="toast-icon">${icon}</span><span class="toast-msg">${esc(msg)}</span><button class="toast-close" onclick="this.parentElement.remove()">×</button>`;
  container.appendChild(el);
  // Trigger animation
  requestAnimationFrame(() => el.classList.add('toast-show'));
  setTimeout(() => {
    el.classList.remove('toast-show');
    el.classList.add('toast-hide');
    setTimeout(() => el.remove(), 300);
  }, duration);
}

function detectTaskChanges(newData) {
  const newTasks = {};
  (newData.tasks || []).forEach(t => { newTasks[t.id] = t.status; });
  for (const [id, status] of Object.entries(newTasks)) {
    const prev = _prevTaskStatuses[id];
    if (prev && prev !== status) {
      const task = (newData.tasks || []).find(t => t.id == id);
      const desc = task ? task.description?.substring(0, 60) : '';
      const agent = task?.assigned_to || '?';
      if (status === 'done') {
        const elapsed = task?.elapsed_seconds;
        const elapsedStr = elapsed ? (elapsed >= 60 ? `${Math.floor(elapsed/60)}m ${elapsed%60}s` : `${elapsed}s`) : '';
        const tokens = task?.tokens_used ? formatTokens(task.tokens_used) + ' tok' : '';
        const metrics = [elapsedStr, tokens].filter(Boolean).join(', ');
        toast(`Task #${id} done by ${agent}${metrics ? ' — ' + metrics : ''}: ${desc}`, 'success', 5000);
        playSound();
      } else if (status === 'failed') {
        toast(`Task #${id} failed (${agent}): ${desc}`, 'error', 6000);
      } else if (status === 'in_progress' && prev === 'created') {
        toast(`${agent} started task #${id}`, 'info', 3000);
      }
    }
  }
  _prevTaskStatuses = newTasks;
}

// ══════════════════════════════════
//  CONNECTION STATUS
// ══════════════════════════════════
let _connectionStatus = 'connecting';
let _lastUpdateTime = Date.now();

function updateConnectionStatus(status) {
  _connectionStatus = status;
  if (status === 'connected') _lastUpdateTime = Date.now();
  const el = document.getElementById('connectionStatus');
  if (!el) return;
  const colors = { connected: '#22c55e', reconnecting: '#eab308', error: '#ef4444', offline: '#ef4444', connecting: '#eab308' };
  const labels = { connected: 'Connected', reconnecting: 'Reconnecting...', error: 'Connection Error', offline: 'Offline', connecting: 'Connecting...' };
  el.innerHTML = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${colors[status]||'#888'};margin-right:4px"></span>${labels[status]||status}`;
  el.style.cursor = status === 'offline' ? 'pointer' : 'default';
  el.onclick = status === 'offline' ? () => { _wsRetries = 0; connectWebSocket(); } : null;
}

// Update "last updated" timer
setInterval(() => {
  if (_connectionStatus === 'connected') _lastUpdateTime = Date.now();
}, 1000);

// ══════════════════════════════════
//  WEBSOCKET (Real-time updates)
// ══════════════════════════════════
function connectWebSocket() {
  if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // Read hub_token from cookie for WS auth
  const _hubToken = (document.cookie.match(/(?:^|;\s*)hub_token=([^;]*)/) || [])[1] || '';
  const wsUrl = proto + '//' + location.host + '/ws' + (_hubToken ? '?token=' + encodeURIComponent(_hubToken) : '');
  try {
    _ws = new WebSocket(wsUrl);
  } catch(e) {
    _startHttpFallback();
    return;
  }
  _ws.onopen = () => {
    _wsConnected = true;
    _wsRetries = 0;
    updateConnectionStatus('connected');
    _stopHttpFallback();
    // Follow selected agent's logs via WebSocket
    if(sel && tab==='logs'){
      _followingAgent='';
      followAgent(sel);
    }
  };
  _ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.type === 'dashboard') {
        _applyDashboardData(msg.data);
      } else if (msg.type === 'logs' && msg.agent && msg.lines) {
        const agent = msg.agent;
        const lines = msg.lines.filter(l => l != null && typeof l === 'string');
        if (!lines.length) return;
        if (!logLines[agent]) logLines[agent] = [];
        logLines[agent].push(...lines);
        if (logLines[agent].length > 3000) logLines[agent] = logLines[agent].slice(-2000);
        // Total log line limit across all agents: 50K
        let totalLines = Object.values(logLines).reduce((sum, arr) => sum + arr.length, 0);
        if (totalLines > 50000) {
          // Trim oldest logs from agents not currently selected
          for (const [a, lines] of Object.entries(logLines)) {
            if (a !== sel && lines.length > 500) {
              logLines[a] = lines.slice(-500);
            }
          }
        }
        if (tab === 'logs' && sel === agent) appendLogLines(lines);
      }
    } catch {}
  };
  _ws.onclose = () => {
    _wsConnected = false;
    _ws = null;
    _wsRetries++;
    updateConnectionStatus('reconnecting');
    if (_wsRetries <= _wsMaxRetries) {
      const delay = Math.min(10000, 1000 * Math.pow(1.5, _wsRetries));
      setTimeout(connectWebSocket, delay);
    } else {
      updateConnectionStatus('offline');
      toast('Connection lost. Click status indicator to reconnect.', 'error', 8000);
    }
    _startHttpFallback();
  };
  _ws.onerror = () => {
    toast('WebSocket connection error', 'warn', 3000);
    updateConnectionStatus('error');
  };
}

function _startHttpFallback() {
  if (_httpPollTimer) return;
  // First load
  poll();
  // Then poll at reasonable interval
  _httpPollTimer = setInterval(() => { poll(); }, 5000);
}

function _stopHttpFallback() {
  if (_httpPollTimer) {
    clearInterval(_httpPollTimer);
    _httpPollTimer = null;
  }
}

// Live timer: update elapsed time every second without full re-render
setInterval(()=>{
  document.querySelectorAll('.log-live-stats[data-server-time]').forEach(el=>{
    const sTime=parseInt(el.dataset.serverTime)||0;
    const sElapsed=parseInt(el.dataset.serverElapsed)||0;
    if(!sTime)return;
    const elapsed=sElapsed+Math.floor((Date.now()-sTime)/1000);
    const elStr=elapsed>=60?Math.floor(elapsed/60)+'m '+Math.round(elapsed%60)+'s':elapsed+'s';
    el.textContent=el.textContent.replace(/⏱\s*\d+m?\s*\d*s?/,'⏱ '+elStr);
  });
},1000);

let _lastVersion = 0, _lastAgentHash = '', _lastTaskHash = '';
function _applyDashboardData(d) {
  detectTaskChanges(d);
  const taskHash = (d.tasks||[]).map(t=>`${t.id}:${t.status}`).join('|');
  const panelNeedsUpdate = (tab === 'tasks' && taskHash !== _lastTaskHash) ||
    (d.version && d.version !== _lastVersion && tab !== 'logs');
  _lastTaskHash = taskHash;
  if (d.version) _lastVersion = d.version;
  data = d;
  if (!data._config) fetchConfig();
  // Always render workspace bar (show primary + add button even when empty)
  renderWorkspaceBar(d.workspaces || {});
  const names = d.agent_names || [];
  if (d.inbox && Array.isArray(d.inbox)) {
    const prev = inbox.length;
    inbox = d.inbox;
    if (inbox.length > prev && prev > 0) {
      const newMsg = inbox[inbox.length - 1];
      notify(`${newMsg.sender}: ${newMsg.content?.substring(0, 80)}`); playSound();
    }
    if (tab === 'inbox') renderPanel();
  }
  if (d.changes && Array.isArray(d.changes)) {
    reviewData = [...d.changes].sort((a, b) => (b.id || 0) - (a.id || 0));
    if (tab === 'review') renderPanel();
  }
  if (tab === 'analytics' && d.analytics) renderPanel();
  if (d.agents) {
    for (const [name, info] of Object.entries(d.agents)) {
      if (info.status === 'offline' && name !== sel && logLines[name] && logLines[name].length > 200) {
        logLines[name] = logLines[name].slice(-200);
      }
    }
  }
  renderSidebar(names);
  updateBadges();
  if (panelNeedsUpdate) renderPanel();
  if (tab === 'logs') updateLogHeader();
}

// ══════════════════════════════════
//  POLLING (HTTP fallback)
// ══════════════════════════════════
async function poll() {
  try {
    const d = await (await fetch(HUB + '/dashboard')).json();
    _applyDashboardData(d);
  } catch {}
}

// ══════════════════════════════════
//  SIDEBAR
// ══════════════════════════════════
function selectAgent(name) {
  sel = name;
  renderSidebar(data.agent_names || []);
  if (tab === 'logs') followAgent(name);
  renderPanel();
}

function renderSidebar(names) {
  const el = $('agentList');
  if (!el) return;
  const agentsMap = data.agents||{};
  const visibleNames = names.filter(n => !(agentsMap[n]||{}).hidden);
  const hiddenNames = names.filter(n => (agentsMap[n]||{}).hidden);
  function renderCard(n) {
    const a = agentsMap[n]||{};
    const isHidden = !!a.hidden;
    const ps = a.pipeline||'offline';
    const dot = ps==='working'?'working':ps==='booting'?'booting':ps==='verifying'?'verifying':
                a.status==='rate_limited'?'rate-limited':a.status==='unresponsive'?'unresponsive':
                ps==='idle'?'idle':'offline';
    const rlB = a.rate_limited_sec>0?`<span class="rl-badge">${a.rate_limited_sec}s</span>`:'';
    const prog = buildAgentProgress(a);
    const cost = a.cost?`<span class="agent-cost">${formatCost(a.cost)}</span>`:'';
    const tokTotal = getAgentTokens(n);
    const tokCost = tokTotal?`${formatTokens(tokTotal)} · `:'';
    const exp = a.expertise?`<span class="agent-exp" title="Expertise">★${a.expertise}</span>`:'';
    const queueCount = (data.tasks||[]).filter(t=>t.assigned_to===n&&['created','assigned','in_progress'].includes(t.status)).length;
    const qBadge = queueCount?`<span class="queue-badge" title="${queueCount} task${queueCount>1?'s':''} in queue">${queueCount}</span>`:'';
    const actions = isHidden ? '' : `<div class="agent-actions">
        <button onclick="event.stopPropagation();editAgent('${escAttr(n)}')" title="Edit">✎</button>
        <button onclick="event.stopPropagation();stopAgent('${escAttr(n)}')" title="Stop">■</button>
        <button onclick="event.stopPropagation();removeAgent('${escAttr(n)}')" title="Remove">✕</button>
      </div>`;
    return `<div class="agent-card${n===sel?' sel':''}${isHidden?' agent-hidden':''}" onclick="selectAgent('${escAttr(n)}')">
      <div class="agent-dot dot-${dot}"></div>
      <div style="flex:1;min-width:0">
        <div class="agent-name">${esc(n)} ${qBadge} ${rlB} ${exp} ${cost}</div>
        <div class="agent-meta">${a.calls||0} calls · ${tokCost}${formatCost(a.cost||0)}${a.silent_sec>60?' · '+formatAgo(a.silent_sec):''}</div>
        ${prog}
      </div>
      ${actions}
    </div>`;
  }
  let html = visibleNames.map(renderCard).join('');
  html += `<button class="add-agent-trigger" onclick="showAddAgentModal()">+ Add Agent</button>`;
  if (hiddenNames.length) {
    html += '<div class="hidden-divider"></div>';
    html += hiddenNames.map(renderCard).join('');
  }
  el.innerHTML = html;
}

function updateBadges() {
  const ib = $('inboxBadge');
  if (ib) { ib.style.display = inbox.length ? 'inline' : 'none'; ib.textContent = inbox.length; }
  const tb = $('tasksBadge');
  const pending = (data.tasks||[]).filter(t=>['created','in_progress'].includes(t.status)).length;
  if (tb) { tb.style.display = pending ? 'inline' : 'none'; tb.textContent = pending; }
  const rb = $('reviewBadge');
  const pendingR = (reviewData||[]).filter(c=>c.status==='pending').length;
  if (rb) { rb.style.display = pendingR ? 'inline' : 'none'; rb.textContent = pendingR; }
  // Alerts badge
  const ab = $('alertsBadge');
  if (ab) {
    let alertCount = 0;
    const agents = data.agents || {};
    for (const a of Object.values(agents)) {
      if (a.status === 'unresponsive' || a.pipeline === 'error') alertCount++;
      if (a.status === 'rate_limited') alertCount++;
    }
    alertCount += (data.tasks||[]).filter(t=>t.status==='failed').length;
    ab.style.display = alertCount ? 'inline' : 'none';
    ab.textContent = alertCount;
    ab.style.background = alertCount ? 'var(--red)' : '';
  }
  // Budget warning
  const bw = $('budgetWarn');
  if (bw) {
    const budget = data.budget || {};
    if (budget.limit && budget.total_spent > budget.limit * 0.8) {
      const pct = Math.round(budget.total_spent / budget.limit * 100);
      bw.textContent = '⚠ Budget ' + pct + '%';
      bw.style.display = 'inline';
      bw.style.color = pct >= 100 ? 'var(--red)' : 'var(--yellow)';
    } else { bw.style.display = 'none'; }
  }
  _syncReviewSelections();
}

function updateTargetDropdown(names) {
  const dd = $('targetAgent');
  if (!dd) return;
  const cur = dd.value;
  const agentsMap = data.agents||{};
  const visible = names.filter(n => !(agentsMap[n]||{}).hidden);
  dd.innerHTML = visible.map(n => `<option value="${escAttr(n)}"${n===cur?' selected':''}>${esc(n)}</option>`).join('');
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ma-theme', next);
  toast('Theme: ' + next, 'info', 2000);
}

function toggleNotif() {
  notifOn = !notifOn;
  if (notifOn && Notification.permission !== 'granted') {
    Notification.requestPermission().then(p => {
      if (p !== 'granted') { notifOn = false; toast('Notifications blocked by browser', 'warn'); }
    });
  }
  const btn = $('notifBtn');
  if (btn) btn.textContent = notifOn ? '🔔' : '🔕';
  toast('Notifications ' + (notifOn ? 'on' : 'off'), 'info', 2000);
}

function toggleSound() {
  soundOn = !soundOn;
  const btn = $('soundBtn');
  if (btn) btn.textContent = soundOn ? '🔊' : '🔇';
  toast('Sound ' + (soundOn ? 'on' : 'off'), 'info', 2000);
}

function cleanTitle(raw) {
  let s = (raw || '').trim();
  while (s.startsWith('[')) {
    const end = s.indexOf(']');
    if (end === -1) break;
    s = s.substring(end + 1).trim();
  }
  s = s.replace(/^[-*]\s+/, '').trim();
  return s || raw.trim();
}

function formatDiff(text) {
  if (!text) return '';
  return text.split('\n').map(line => {
    if (line.startsWith('diff ') || line.startsWith('--- ') || line.startsWith('+++ '))
      return '<div class="diff-file">' + esc(line) + '</div>';
    if (line.startsWith('@@'))
      return '<div class="diff-hunk">' + esc(line) + '</div>';
    if (line.startsWith('+'))
      return '<div class="diff-add">' + esc(line) + '</div>';
    if (line.startsWith('-'))
      return '<div class="diff-del">' + esc(line) + '</div>';
    return '<div>' + esc(line) + '</div>';
  }).join('');
}

function notify(msg) {
  if (notifOn && Notification.permission === 'granted') {
    new Notification('Multi-Agent', { body: msg });
  }
}

function playSound() {
  if (!soundOn) return;
  try { new Audio('data:audio/wav;base64,UklGRl9vT19XQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YU'+Array(300).join('0')).play(); } catch {}
}

function downloadLog(agent) {
  if (!agent) return;
  const text = (logLines[agent] || []).join('\n');
  const blob = new Blob([text], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${agent}-logs.txt`;
  a.click();
}

async function fetchInbox() {
  try {
    inbox = await (await fetch(HUB + '/messages/user?consume=false')).json();
    updateBadges();
    if (tab === 'inbox') renderPanel();
  } catch {}
}

function editAgent(name) {
  const a = (data.agents||{})[name]||{};
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  overlay.innerHTML = `<div class="modal-box" style="width:380px">
    <h3 style="margin-bottom:12px;font-size:14px">Edit ${esc(name)}</h3>
    <label class="text-sm text-dim">Status</label>
    <select id="_editStatus" class="modal-input"><option ${a.status==='active'?'selected':''}>active</option><option ${a.status==='paused'?'selected':''}>paused</option></select>
    <div class="modal-actions">
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
      <button class="modal-btn-ok" onclick="fetch(HUB+'/agents/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_name:'${escAttr(name)}',status:document.getElementById('_editStatus').value})}).then(()=>{this.closest('.modal-overlay').remove();poll();})">Save</button>
    </div></div>`;
  document.body.appendChild(overlay);
}

function showAddAgentModal() {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  overlay.innerHTML = `<div class="modal-box" style="width:380px">
    <h3 style="margin-bottom:12px;font-size:14px">Add Agent</h3>
    <label class="text-sm text-dim">Name</label>
    <input id="_addName" class="modal-input" placeholder="e.g. devops">
    <label class="text-sm text-dim">Role (optional)</label>
    <input id="_addRole" class="modal-input" placeholder="e.g. DevOps specialist">
    <div class="modal-actions">
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
      <button class="modal-btn-ok" onclick="const n=document.getElementById('_addName').value.trim();if(!n)return;fetch(HUB+'/agents/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,role:document.getElementById('_addRole').value.trim()})}).then(()=>{this.closest('.modal-overlay').remove();poll();toast('Agent '+n+' added','success');})">Add</button>
    </div></div>`;
  document.body.appendChild(overlay);
  setTimeout(() => $('_addName')?.focus(), 50);
}

let _hideHubNoise = true;
let _compactLogs = false;

function humanizeHubLog(line) {
  const low = line.toLowerCase();
  const hidden = low.includes('/poll/') || low.includes('/logs/') || low.includes('/heartbeat');
  const text = line.replace(/📡\s*(?:POST|GET|PUT|DELETE)\s+\S+/, match => {
    if (match.includes('/poll/')) return '📡 polling hub';
    if (match.includes('/status')) return '📡 status update';
    if (match.includes('/tasks')) return '📡 task sync';
    if (match.includes('/messages')) return '📡 message sync';
    if (match.includes('/changes')) return '📡 submit changes';
    if (match.includes('/patterns')) return '📡 pattern sync';
    return match;
  });
  return { text, hidden };
}

function relativeTime(ts) {
  if (!ts) return '';
  try {
    const diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
    if (diff < 60) return 'now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return formatTime(ts);
  } catch { return formatTime(ts); }
}


const _thinkVerbs=['Pondering','Ruminating','Spelunking','Cogitating','Deliberating','Musing','Contemplating','Noodling','Mulling','Brainstorming','Percolating','Synthesizing'];
let _thinkIdx=0;
function buildAgentProgress(a){
  const p=a.progress;if(!p||!p.event)return'';
  const ps=a.pipeline||'offline';
  if(ps!=='working'&&ps!=='verifying'&&p.event!=='ecosystem')return'';
  if(p.event==='ecosystem')return`<div class="agent-progress eco-badge">${esc(p.detail||'').substring(0,50)}</div>`;
  const elapsed=p.elapsed||0;const toks=p.task_tokens||0;const calls=p.task_calls||0;
  const elStr=elapsed>=60?Math.floor(elapsed/60)+'m '+elapsed%60+'s':elapsed+'s';
  const tokStr=formatTokens(toks);
  let activity='',icon='⚡';
  if(p.event==='tool_use'){
    activity=p.detail||'working';icon='🔧';
  }else if(p.event==='call_start'){
    activity=_thinkVerbs[_thinkIdx%_thinkVerbs.length]+'…';
    _thinkIdx++;icon='✶';
  }else if(p.event==='call_done'){
    activity='processing';icon='⚡';
  }else if(p.event==='hub_call'){
    activity=p.detail||'syncing';icon='📡';
  }
  return`<div class="agent-progress agent-live">${icon} ${esc(activity).substring(0,55)}</div>`;
}
function getAgentTokens(n){const u=(data.usage||{})[n];return u?(u.tokens_in||0)+(u.tokens_out||0):0;}
function formatTokens(n){return n>1e6?(n/1e6).toFixed(1)+'M':n>1e3?(n/1e3).toFixed(1)+'K':n+'';}
function formatCost(c){if(c==null||c===undefined)return'$0.00';c=parseFloat(c)||0;if(c===0)return'$0.00';if(c>=100)return'$'+c.toFixed(0);if(c>=1)return'$'+c.toFixed(2);if(c>=0.01)return'$'+c.toFixed(3);if(c>=0.001)return'$'+c.toFixed(4);if(c>0)return'$'+c.toFixed(5);return'$0.00';}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function escAttr(s){return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}  // safe for HTML attribute values
function formatTime(ts){if(!ts)return'';try{return new Date(ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}catch{return ts;}}
function formatAgo(sec){if(sec<60)return'';if(sec<120)return'1m ago';if(sec<3600)return Math.floor(sec/60)+'m ago';if(sec<7200)return'1h ago';return Math.floor(sec/3600)+'h ago';}
// ══════════════════════════════════
//  SSE + LOG HISTORY
// ══════════════════════════════════
async function fetchLogHistory(agent){
  if(!agent)return;
  try{
    const res=await fetch(HUB+'/logs/'+encodeURIComponent(agent)+'?lines=1000');
    const data=await res.json();
    const lines=(data.lines||[]).filter(l=>l!=null&&typeof l==='string');
    if(lines.length){
      logLines[agent]=lines;
      if(tab==='logs'&&sel===agent)renderPanel();
    }
  }catch{}
}

let _followingAgent='';
function followAgent(agent){
  if(_followingAgent===agent)return;
  _followingAgent=agent||'';
  // Load history via HTTP, then tell WebSocket to stream new lines
  if(agent)fetchLogHistory(agent);
  if(_ws&&_ws.readyState===WebSocket.OPEN){
    _ws.send(JSON.stringify({type:'follow',agent:agent||''}));
  }
}

function appendLogLines(lines){
  const el=document.querySelector('.log-area');if(!el)return;
  const atBot=el.scrollHeight-el.scrollTop-el.clientHeight<60;
  const valid=lines.filter(l=>l!=null&&typeof l==='string');
  const f=logSearch?valid.filter(l=>l.toLowerCase().includes(logSearch.toLowerCase())):valid;
  // Remove typing indicator before appending new lines
  const typing=el.querySelector('.log-typing');
  if(typing)typing.remove();
  for(const l of f){
    if(_reCallStart.test(l)){
      // Close previous open call block if any
      const prev=el.querySelector('details.call-block[open]:last-of-type');
      if(prev&&!prev.querySelector('[data-closed]')){
        prev.removeAttribute('open');
        const sm=prev.querySelector('summary .call-block-meta');
        if(sm){const tc=prev.querySelectorAll('.call-block-body .tool').length;sm.textContent=`✓ ${tc} tool${tc!==1?'s':''}`;}
      }
      const det=document.createElement('details');det.className='call-block';det.open=true;
      const hdrHtml=colorize(l).replace(/<hr[^>]*>/g,'').replace(/<\/?div[^>]*>/g,'');
      const sm=document.createElement('summary');sm.innerHTML=`${hdrHtml}<span class="call-block-meta">⋯</span>`;
      const body=document.createElement('div');body.className='call-block-body';
      const loader=document.createElement('div');loader.className='call-block-loading';loader.textContent='Processing...';body.appendChild(loader);
      det.appendChild(sm);det.appendChild(body);el.appendChild(det);
    } else if(_reExitCode.test(l)){
      const open=el.querySelector('details.call-block[open]:last-of-type');
      if(open){
        const body=open.querySelector('.call-block-body');
        const d=document.createElement('div');d.innerHTML=colorize(l);d.setAttribute('data-closed','1');body.appendChild(d);
        open.removeAttribute('open');
        const sm=open.querySelector('summary .call-block-meta');
        if(sm){const tc=body.querySelectorAll('.tool').length;const ok=l.match(/exit=0/);
          const tokLine=[...body.children].map(c=>c.textContent).find(t=>t.includes('tokens:')&&t.includes('in/'));
          const tokBit=tokLine?` · ${tokLine.replace(/.*?([\d,]+in\/[\d,]+out).*/,'$1')}`:'';
          sm.textContent=`${ok?'✓':'✗'} ${tc} tool${tc!==1?'s':''}${tokBit}`;}
      } else {const d=document.createElement('div');d.innerHTML=colorize(l);el.appendChild(d);}
    } else {
      const open=el.querySelector('details.call-block[open]:last-of-type');
      if(open){const body=open.querySelector('.call-block-body');const loader=body.querySelector('.call-block-loading');if(loader)loader.remove();const d=document.createElement('div');d.innerHTML=colorize(l);body.appendChild(d);}
      else{const d=document.createElement('div');d.innerHTML=colorize(l);el.appendChild(d);}
    }
  }
  // Re-add typing indicator if agent is working
  const agentInfo=(data.agents||{})[sel]||{};
  if(agentInfo.pipeline==='working'){
    const ti=document.createElement('div');ti.className='log-typing';ti.textContent='● agent is working...';el.appendChild(ti);
  }
  while(el.children.length>3000)el.removeChild(el.firstChild);
  if(atBot)el.scrollTop=el.scrollHeight;
}
// ── Call grouping: collapse ▶...◼ blocks ──
function buildGroupedHtml(lines){
  let html='',buf=[],hdr='';
  for(const l of lines){
    if(_reCallStart.test(l)){
      if(buf.length) html+=_flushCallBlock(hdr,buf,true);
      hdr=l; buf=[];
    } else if(hdr && _reExitCode.test(l)){
      buf.push(l); html+=_flushCallBlock(hdr,buf,false); hdr=''; buf=[];
    } else if(hdr){
      buf.push(l);
    } else {
      html+=`<div>${colorize(l)}</div>`;
    }
  }
  if(buf.length||hdr) html+=_flushCallBlock(hdr,buf,true);
  return html;
}
function _flushCallBlock(hdr,lines,isOpen){
  const exitLine=lines.find(l=>_reExitCode.test(l));
  const tools=lines.filter(l=>l.includes('🔧')).length;
  const tokLine=lines.find(l=>l.includes('tokens:')&&l.includes('in/'));
  const tokBit=tokLine?` · ${tokLine.replace(/.*?([\d,]+in\/[\d,]+out).*/,'$1')}`:'';
  const exitBit=exitLine?(exitLine.match(/exit=0/)?'✓':'✗'):'⋯';
  // Strip hr/div wrapper from colorize for clean summary
  const hdrHtml=colorize(hdr).replace(/<hr[^>]*>/g,'').replace(/<\/?div[^>]*>/g,'');
  const summary=`${hdrHtml}<span class="call-block-meta">${exitBit} ${tools} tool${tools!==1?'s':''}${tokBit}</span>`;
  const inner=lines.map(l=>`<div>${colorize(l)}</div>`).join('');
  return`<details class="call-block"${isOpen?' open':''}><summary>${summary}</summary><div class="call-block-body">${inner}</div></details>`;
}
// Cached regex patterns for colorize() — avoids re-creating on every call
const _reUserPrompt = /^[\s]*📨\s*/;
const _reCallStart = /▶\s*claude\s*#(\d+)\s*\[(\w+)\]\s*\((\d+)/;
const _reExitCode = /◼\s*exit=(\d+)/;
const _reToolUse = /🔧\s*(\w+)(?::?\s*(.*))?$/;
const _reTextStream = /^\s*💬\s*/;

function _ts(line){
  // Extract and format timestamp: [HH:MM:SS] → subtle prefix
  const m=line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/);
  if(!m)return{ts:'',rest:line};
  return{ts:`<span class="log-ts">${m[1]}</span>`,rest:line.slice(m[0].length)};
}
function colorize(line){
  if(!line||typeof line!=='string')return'';
  const{ts,rest}=_ts(line);
  // User prompt
  if(rest.startsWith('📨')||line.startsWith('📨'))
    return`<div class="user-prompt">${esc(rest.replace(_reUserPrompt,''))}</div>`;
  // Claude call start — ▶ call #N [model] (chars)
  const callMatch=rest.match(_reCallStart)||line.match(_reCallStart);
  if(callMatch){
    const num=callMatch[1],model=callMatch[2],chars=callMatch[3];
    const tagClass=model==='opus'?'model-tag-opus':model==='haiku'?'model-tag-haiku':'model-tag-sonnet';
    return`<div class="log-call-header">${ts}<span>call #${esc(num)}</span><span class="model-tag ${tagClass}">${esc(model)}</span><span class="log-dim">${esc(chars)} chars</span></div>`;
  }
  // Call done — ◼ exit=0, N lines, N tools
  const exitMatch=rest.match(_reExitCode)||line.match(_reExitCode);
  if(exitMatch){
    const code=exitMatch[1];
    const cls=code==='0'?'log-exit-ok':'log-exit-err';
    return`<span class="${cls}">${ts}${esc(rest)}</span>`;
  }
  // Token counts
  if(rest.includes('tokens:')&&rest.includes('in/')&&rest.includes('out'))
    return`<span class="log-tokens">${ts}${esc(rest)}</span>`;
  // Boot / system lines — dim them
  if(rest.match(/^===\s*\w+\s*===$|^  (thinking|coding|hub ok|CLI:|🔗 MCP)/))
    return`<span class="log-boot">${ts}${esc(rest)}</span>`;
  // Agent joined — compact
  if(rest.includes('👋 New agent joined'))
    return`<span class="log-boot">${ts}${esc(rest)}</span>`;
  // Task received — ← N msg(s)
  if(rest.match(/^←\s*\d+\s*msg/))
    return`<span class="log-dim">${ts}${esc(rest)}</span>`;
  // Task content line [user] #N ...
  if(rest.match(/^\s*\[user\]\s*#\d+/))
    return`<div class="log-task-line">${ts}${esc(rest)}</div>`;
  // Chat reply — collapsible
  if(rest.includes('💬') && rest.includes('Replied: ')){
    const parts=rest.split('Replied: ');
    const body=parts.slice(1).join('Replied: ');
    const preview=body.substring(0,80)+(body.length>80?'…':'');
    return`<details class="chat-reply-block"><summary class="text-stream">${ts}${esc(preview)}</summary><div class="chat-reply-body">${esc(body)}</div></details>`;
  }
  // Chat incoming — collapsible
  if(rest.includes('💬') && rest.includes('Chat from ')){
    const parts=rest.split(': ');
    const header=parts[0];
    const body=parts.slice(1).join(': ');
    const preview=body.substring(0,80)+(body.length>80?'…':'');
    return`<details class="chat-reply-block"><summary class="text-stream">${ts}${esc(header)}: ${esc(preview)}</summary><div class="chat-reply-body">${esc(body)}</div></details>`;
  }
  // Text streaming (Claude output)
  if(rest.includes('💬'))
    return`<span class="text-stream">${esc(rest.replace(_reTextStream,''))}</span>`;
  // Hub calls — humanized
  if(rest.includes('📡')){
    const h=humanizeHubLog(rest);
    if(h.hidden&&_hideHubNoise)return`<span class="hub-call-hidden"></span>`;
    if(h.text!==rest)return`<span class="hub-call-human">${ts}${esc(h.text)}</span>`;
    return`<span class="hub-call">${ts}${esc(rest)}</span>`;
  }
  // Tool use with badges
  if(rest.includes('🔧')){
    const toolMatch=rest.match(_reToolUse);
    if(toolMatch){
      const tool=toolMatch[1],detail=toolMatch[2]||'';
      const tl=tool.toLowerCase();
      const badgeClass=tl==='bash'?'tool-badge-bash':tl==='edit'||tl==='write'?'tool-badge-edit':
        tl==='read'||tl==='view'?'tool-badge-read':tl==='webfetch'||tl==='websearch'?'tool-badge-web':
        tl==='task'?'tool-badge-task':'tool-badge-default';
      return`<span class="tool">${ts}<span class="tool-badge ${badgeClass}">${esc(tool)}</span>${detail?` <span class="log-dim">${esc(detail)}</span>`:''}</span>`;
    }
    return`<span class="tool">${ts}${esc(rest)}</span>`;
  }
  // Errors
  if(rest.includes('✗')||rest.includes('ERROR')||rest.includes('FAIL')||rest.includes('⛔'))
    return`<span class="error">${ts}${esc(rest)}</span>`;
  // Success
  if(rest.includes('✓')||rest.includes('PASS'))
    return`<span class="ok">${ts}${esc(rest)}</span>`;
  // Online
  if(rest.includes('ONLINE'))
    return`<span class="log-online">${ts}${esc(rest)}</span>`;
  // Done summary
  if(rest.match(/^✓ done/))
    return`<span class="log-done">${ts}${esc(rest)}</span>`;
  // Warnings
  if(rest.includes('⚠')||rest.includes('rate limit'))
    return`<span class="text-yellow">${ts}${esc(rest)}</span>`;
  // Learning
  if(rest.includes('🧠'))
    return`<span class="text-cyan">${ts}${esc(rest)}</span>`;
  // Cost
  if(rest.includes('💰'))
    return`<span class="text-yellow">${ts}${esc(rest)}</span>`;
  // Git
  if(rest.includes('📌')||rest.includes('🌿'))
    return`<span class="text-green">${ts}${esc(rest)}</span>`;
  // Cache hit
  if(rest.includes('📦'))
    return`<span class="log-dim">${ts}${esc(rest)}</span>`;
  return`<span>${ts}${esc(rest)}</span>`;
}

// ══════════════════════════════════
//  TABS
// ══════════════════════════════════
function switchTab(t){
  tab=t;
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===t));
  if(t==='logs')followAgent(sel);else followAgent('');
  renderPanel();
}

function renderPanel(){
  const p=$('panel');if(!p)return;
  ({logs:renderLogs,tasks:renderTasks,inbox:renderInbox,review:renderReview,
    analytics:renderAnalytics,activity:renderActivity,locks:renderLocks,
    git:renderGit,tests:renderTests,alerts:renderAlerts})[tab]?.(p);
}
function updateLogHeader() {
  // Update header elements in-place without full re-render (preserves open <details> blocks)
  if (!sel) return;
  const agentInfo = (data.agents || {})[sel] || {};
  const isWorking = agentInfo.pipeline === 'working';
  const statusDot = isWorking ? 'dot-working' : agentInfo.pipeline === 'idle' ? 'dot-idle' : agentInfo.pipeline === 'booting' ? 'dot-booting' : 'dot-offline';
  const prog = agentInfo.progress || {};
  const liveToks = prog.task_tokens || 0;
  const liveCalls = prog.task_calls || 0;
  const liveCost = agentInfo.cost || 0;
  const _rawProject = prog.project || '';
  const liveProject = _rawProject && _rawProject !== '.' ? _rawProject : '';
  const serverElapsed = prog.elapsed || 0;
  const serverTime = prog.time ? new Date(prog.time).getTime() : 0;
  const liveElapsed = serverTime ? (serverElapsed + Math.floor((Date.now() - serverTime) / 1000)) : serverElapsed;
  const liveElStr = liveElapsed >= 60 ? Math.floor(liveElapsed / 60) + 'm ' + Math.round(liveElapsed % 60) + 's' : liveElapsed + 's';
  const hasProgress = isWorking || (serverTime > 0 && liveCalls > 0);
  const projectTag = liveProject ? ` · 📁 ${esc(liveProject)}` : '';

  // Update status dot
  const dotEl = document.querySelector('.log-header .agent-dot');
  if (dotEl) { dotEl.className = 'agent-dot ' + statusDot + ' dot-inline'; }

  // Update progress detail
  const progDetail = agentInfo.progress?.detail || '';
  const progEvent = agentInfo.progress?.event || '';
  const detailEl = document.querySelector('.log-header .text-cyan');
  if (isWorking && progDetail) {
    if (detailEl) {
      detailEl.innerHTML = `${progEvent === 'tool_use' ? '🔧' : '⚡'} ${esc(progDetail.substring(0, 60))}`;
      detailEl.style.display = '';
    }
  } else if (detailEl) {
    detailEl.style.display = 'none';
  }

  // Update live stats bar
  const statsEl = document.querySelector('.log-live-stats');
  if (hasProgress) {
    const statsHtml = `⏱ ${liveElStr} · ${formatTokens(liveToks)} tok · ${liveCalls} call${liveCalls !== 1 ? 's' : ''}${projectTag} · ${formatCost(liveCost)}`;
    if (statsEl) {
      statsEl.textContent = statsHtml;
      statsEl.dataset.serverElapsed = serverElapsed;
      statsEl.dataset.serverTime = serverTime;
      statsEl.dataset.agent = sel;
    } else {
      // Create stats bar if it doesn't exist yet
      const logHeader = document.querySelector('.log-header');
      if (logHeader) {
        const newStats = document.createElement('div');
        newStats.className = 'log-live-stats';
        newStats.dataset.agent = sel;
        newStats.dataset.serverElapsed = serverElapsed;
        newStats.dataset.serverTime = serverTime;
        newStats.textContent = statsHtml;
        logHeader.parentElement.insertBefore(newStats, logHeader.nextSibling);
      }
    }
  } else if (statsEl) {
    statsEl.remove();
  }

  // Update line count
  const totalLines = (logLines[sel] || []).filter(l => l != null && typeof l === 'string').length;
  const lineCountEl = document.querySelector('.log-header .text-dim');
  if (lineCountEl) lineCountEl.textContent = totalLines + ' lines';
}
function renderLogs(p){
  if(mergedMode){renderMergedLogs(p);return;}
  if(!sel){
    const names=data.agent_names||[];
    p.innerHTML=`<div class="empty-state" style="padding:60px 20px">
      <div style="font-size:28px;margin-bottom:12px">📋</div>
      <div style="font-size:14px;margin-bottom:8px">Select an agent to view logs</div>
      <div style="font-size:11px;color:var(--fg3);margin-bottom:16px">Click an agent in the sidebar or press <kbd style="color:var(--ac)">1-${names.length||9}</kbd></div>
      <div style="display:flex;gap:6px;justify-content:center;flex-wrap:wrap">${names.map((n,i)=>{
        const a=(data.agents||{})[n]||{};const st=a.pipeline||'offline';
        return`<button onclick="selectAgent('${escAttr(n)}')" style="padding:6px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--fg);cursor:pointer;font-size:11px"><span class="agent-dot dot-${st} dot-inline" style="margin-right:4px"></span>${esc(n)}</button>`;
      }).join('')}</div></div>`;
    return;
  }
  const all=(logLines[sel]||[]).filter(l=>l!=null&&typeof l==='string');
  const f=logSearch?all.filter(l=>l.toLowerCase().includes(logSearch.toLowerCase())):all;
  // Check if agent is currently working (for typing indicator)
  const agentInfo=(data.agents||{})[sel]||{};
  const isWorking=agentInfo.pipeline==='working';
  const statusDot=isWorking?'dot-working':agentInfo.pipeline==='idle'?'dot-idle':agentInfo.pipeline==='booting'?'dot-booting':'dot-offline';
  const progDetail=agentInfo.progress?.detail||'';
  const progEvent=agentInfo.progress?.event||'';
  const LOG_VISIBLE = 200;
  const totalLines = f.length;
  const visibleLines = f.slice(-LOG_VISIBLE);
  const hiddenCount = totalLines - visibleLines.length;
  const prog=agentInfo.progress||{};
  const liveToks=prog.task_tokens||0;const liveCalls=prog.task_calls||0;
  const liveCost=agentInfo.cost||0;
  const _rawProject=prog.project||'';
  const liveProject=_rawProject&&_rawProject!=='.'?_rawProject:'';
  // Calculate elapsed client-side from server timestamp for real-time updates
  const serverElapsed=prog.elapsed||0;
  const serverTime=prog.time?new Date(prog.time).getTime():0;
  const liveElapsed=serverTime?(serverElapsed+Math.floor((Date.now()-serverTime)/1000)):serverElapsed;
  const liveElStr=liveElapsed>=60?Math.floor(liveElapsed/60)+'m '+Math.round(liveElapsed%60)+'s':liveElapsed+'s';
  // Show stats bar when working OR when there's recent progress data (task just finished)
  const hasProgress=isWorking||(serverTime>0&&liveCalls>0);
  const projectTag=liveProject?` · 📁 ${esc(liveProject)}`:'';
  const liveStats=hasProgress?`<div class="log-live-stats" data-agent="${esc(sel)}" data-server-elapsed="${serverElapsed}" data-server-time="${serverTime}">⏱ ${liveElStr} · ${formatTokens(liveToks)} tok · ${liveCalls} call${liveCalls!==1?'s':''}${projectTag} · ${formatCost(liveCost)}</div>`:'';
  p.innerHTML=`<div class="log-header">
    <span class="agent-dot ${statusDot} dot-inline"></span>
    <strong>${sel||'Select agent'}</strong>
    ${isWorking&&progDetail?`<span class="text-sm text-cyan truncate" style="max-width:250px">${progEvent==='tool_use'?'🔧':'⚡'} ${esc(progDetail.substring(0,60))}</span>`:''}
    <span class="text-xs text-dim">${totalLines} lines</span>
    <input class="log-search" placeholder="🔍 Search..." value="${esc(logSearch)}" oninput="logSearch=this.value;renderPanel()">
    <button class="log-merge-btn" onclick="_compactLogs=!_compactLogs;renderPanel()" title="Toggle compact">${_compactLogs?'◱':'◳'}</button>
    <button class="log-merge-btn" onclick="mergedMode=!mergedMode;renderPanel()" title="Toggle merged view">${mergedMode?'👤':'🌐'}</button>
    <button class="log-merge-btn" onclick="_hideHubNoise=!_hideHubNoise;renderPanel()" title="Toggle hub noise filter">${_hideHubNoise?'🔇':'📡'}</button>
    <button class="log-merge-btn" onclick="downloadLog('${escAttr(sel)}')" title="Download log">💾</button></div>
  ${liveStats}
  <div class="log-area${_compactLogs?' compact':''}"></div>`;
  const la=p.querySelector('.log-area');
  if(la){
    const frag=document.createDocumentFragment();
    if(hiddenCount>0){
      const btn=document.createElement('div');
      btn.className='text-xs text-dim';btn.style.cssText='text-align:center;padding:4px;cursor:pointer';
      btn.textContent=`⬆ ${hiddenCount} older lines — click to load`;
      btn.onclick=()=>{btn.remove();const older=f.slice(0,-LOG_VISIBLE);older.forEach(l=>{const d=document.createElement('div');d.innerHTML=colorize(l);la.prepend(d);});};
      frag.appendChild(btn);
    }
    const grouped=document.createElement('div');grouped.innerHTML=buildGroupedHtml(visibleLines);
    while(grouped.firstChild)frag.appendChild(grouped.firstChild);
    if(isWorking){const d=document.createElement('div');d.className='log-typing';d.textContent='● agent is working...';frag.appendChild(d);}
    la.appendChild(frag);
    la.scrollTop=la.scrollHeight;
  }
}

async function renderMergedLogs(p){
  p.innerHTML=`<div class="log-header"><strong>🌐 All Agents</strong>
    <input class="log-search" placeholder="🔍 Search..." value="${esc(logSearch)}" oninput="logSearch=this.value;renderPanel()">
    <button class="log-merge-btn" onclick="_compactLogs=!_compactLogs;renderPanel()" title="Toggle compact">${_compactLogs?'◱':'◳'}</button>
    <button class="log-merge-btn" onclick="mergedMode=!mergedMode;renderPanel()">👤</button></div>
  <div class="log-area${_compactLogs?' compact':''}">Loading...</div>`;
  try{
    const m=await(await fetch(HUB+'/logs/merged?lines=300&search='+encodeURIComponent(logSearch))).json();
    const la=p.querySelector('.log-area');
    if(la){la.innerHTML=m.filter(x=>x&&x.line).map(x=>`<div><span class="log-agent-tag">${esc(x.agent||'?')}</span> ${colorize(x.line)}</div>`).join('');la.scrollTop=la.scrollHeight;}
  }catch{}
}
// ── Git ──
let gitFilterProject='';
async function renderGit(p){
  p.innerHTML='<div class="empty-state">Loading git data...</div>';
  try{
    const [branches,status]=await Promise.all([
      (await fetch(HUB+'/git/branches')).json(),
      (await fetch(HUB+'/git/status')).json()
    ]);
    const allProjects=Object.keys({...branches,...status}).sort();
    if(!allProjects.length){p.innerHTML='<div class="empty-state">🌿 No git repositories found</div>';return;}
    
    const shown=gitFilterProject?allProjects.filter(p=>p===gitFilterProject):allProjects;
    // Fetch per-project logs only for shown projects
    const logPromises=shown.map(proj=>
      fetch(HUB+'/git/log?project='+encodeURIComponent(proj)+'&n=10').then(r=>r.json()).catch(()=>[])
    );
    const logs=await Promise.all(logPromises);
    let html=`<div class="flex-center gap-8 mb-10">
      <select class="select-sm" onchange="gitFilterProject=this.value;renderGit($('panel'))">
        <option value="">All projects</option>
        ${allProjects.map(p=>`<option value="${escAttr(p)}"${gitFilterProject===p?' selected':''}>${esc(p)}</option>`).join('')}
      </select></div>`;
    shown.forEach((proj, i)=>{
      const s=status[proj]||{}, b=branches[proj]||[], log=logs[i]||[];
      html+=`<div class="card mb-8" style="padding:10px">
        <div class="flex-between mb-6"><strong class="text-ac">${esc(proj)}</strong>
          <span class="text-sm">${esc(s.branch||'?')} · ${s.changes||0} changes</span></div>
        ${s.status?`<pre class="text-xs bg-base border" style="padding:6px;border-radius:4px;max-height:100px;overflow:auto">${esc(s.status)}</pre>`:''}
        ${log.length?`<div class="mt-6"><div class="text-sm text-dim mb-4">Recent commits</div>
          ${log.map(c=>`<div class="text-xs" style="padding:2px 0"><code class="text-cyan">${esc(c.hash)}</code> ${esc(c.message)} <span class="text-dim">${esc(c.when)}</span></div>`).join('')}</div>`:''}
      </div>`;
    });
    p.innerHTML=html;
  }catch(e){p.innerHTML=`<div class="empty-state">🌿 Error loading git data</div>`;}
}

// ── Tests ──
async function renderTests(p) {
  let results=[];
  try { results = await (await fetch(HUB + '/tests/results')).json(); } catch {}
  if (!results.length) { p.innerHTML = '<div class="empty-state">🧪 No test data available</div>'; return; }
  p.innerHTML = `<div class="test-results">${results.map(r => {
    const total = (r.tests_passed||0) + (r.tests_failed||0);
    const pct = total ? Math.round((r.tests_passed||0)/total*100) : 0;
    const hasFailed = (r.tests_failed||0) > 0;
    return `<div class="card mb-8" style="padding:8px 10px;border-left:3px solid ${hasFailed?'var(--red)':'var(--green)'}">
      <div class="flex-between"><strong class="text-ac">${esc(r.project||'?')}</strong>
        <span class="text-sm">${esc(r.agent_name||'?')} · ${formatTime(r.timestamp)}</span></div>
      <div class="flex-center gap-8 mt-4">
        <span class="text-green">✓ ${r.tests_passed||0}</span>
        <span class="text-red">✗ ${r.tests_failed||0}</span>
        <span class="text-dim">${r.tests_skipped||0} skipped</span>
        <span class="text-yellow">lint: ${r.lint_errors||0}</span>
        <span class="text-dim">${pct}% pass</span></div>
    </div>`;
  }).join('')}</div>`;
}

// ── Alerts ──
let _alertFilter = 'all';
function renderAlerts(p) {
  const alerts = [];
  // Build alerts from current data
  const agents = data.agents || {};
  for (const [n, a] of Object.entries(agents)) {
    if (a.status === 'unresponsive') alerts.push({type:'unresponsive',severity:'error',agent:n,msg:`Agent unresponsive`,detail:'',time:a.last_seen||''});
    if (a.pipeline === 'error') alerts.push({type:'error',severity:'error',agent:n,msg:a.error||'Agent error',detail:'',time:''});
    if (a.status === 'rate_limited') alerts.push({type:'rate_limit',severity:'warn',agent:n,msg:`Rate limited (${a.rate_limited_sec||0}s)`,detail:'',time:''});
  }
  (data.tasks||[]).filter(t=>t.status==='failed').forEach(t=>{
    alerts.push({type:'task_fail',severity:'error',agent:t.assigned_to||'?',msg:`Task #${t.id} failed`,detail:t.error_message||'',time:t.completed_at||''});
  });
  const budget = data.budget || {};
  if (budget.limit && budget.total_spent > budget.limit * 0.8)
    alerts.push({type:'budget',severity:'warn',agent:'system',msg:`Budget ${Math.round(budget.total_spent/budget.limit*100)}% used`,detail:'',time:''});
  (data.tasks||[]).filter(t=>t.status==='created'&&t.depends_on?.length).forEach(t=>{
    const blockers=t.depends_on.filter(d=>{const bt=(data.tasks||[]).find(x=>x.id===d);return bt&&!['done','cancelled'].includes(bt.status);});
    if(blockers.length) alerts.push({type:'blocked',severity:'info',agent:t.assigned_to||'?',msg:`Task #${t.id} blocked by #${blockers.join(', #')}`,detail:'',time:t.created_at||''});
  });
  const sevOrder={error:0,warn:1,info:2};
  alerts.sort((a,b)=>(sevOrder[a.severity]||3)-(sevOrder[b.severity]||3));
  const filtered=_alertFilter==='all'?alerts:alerts.filter(a=>a.severity===_alertFilter);
  const errCount=alerts.filter(a=>a.severity==='error').length;
  const warnCount=alerts.filter(a=>a.severity==='warn').length;
  const badge=$('alertsBadge');
  if(badge){
    const total=errCount+warnCount;
    badge.style.display=total?'inline':'none';
    badge.textContent=total;
    badge.style.background=errCount?'var(--red)':'var(--yellow)';
  }
  if(!alerts.length){p.innerHTML='<div class="empty-state">All systems normal</div>';return;}
  const sevIcon={error:'🔴',warn:'🟡',info:'🔵'};
  const sevColor={error:'var(--red)',warn:'var(--yellow)',info:'var(--fg3)'};
  p.innerHTML=`<div class="flex-center flex-wrap gap-8 mb-12">
    <h3 class="text-lg" style="margin:0">Alerts</h3>
    <button class="tab-btn${_alertFilter==='all'?' active':''}" onclick="_alertFilter='all';renderAlerts($('panel'))">All (${alerts.length})</button>
    <button class="tab-btn${_alertFilter==='error'?' active':''}" onclick="_alertFilter='error';renderAlerts($('panel'))">Errors (${errCount})</button>
    <button class="tab-btn${_alertFilter==='warn'?' active':''}" onclick="_alertFilter='warn';renderAlerts($('panel'))">Warnings (${warnCount})</button>
  </div>
  <div style="display:flex;flex-direction:column;gap:6px">${filtered.map(a=>`
    <div class="card" style="padding:8px 12px;border-left:3px solid ${sevColor[a.severity]}">
      <div class="flex-between"><div class="flex-center gap-6">
        <span>${sevIcon[a.severity]}</span><strong class="text-ac">${esc(a.agent)}</strong>
        <span>${esc(a.msg)}</span></div>
        ${a.time?`<span class="text-xs text-dim">${relativeTime(a.time)}</span>`:''}</div>
      ${a.detail?`<div class="text-sm text-muted mt-4">${esc(a.detail)}</div>`:''}
    </div>`).join('')}
  </div>`;
}


// ── Tasks with Dependency Graph ──
let taskSearch='', taskFilterAgent='', taskFilterPriority='';

function renderTasks(p){
  // Preserve new-task input values across re-renders
  const _savedDesc=$('newTaskDesc')?.value||'';
  const _savedAgent=$('newTaskAgent')?.value||'';
  const _savedDeps=$('newTaskDeps')?.value||'';
  const _focusedInTask=document.activeElement?.id;

  let taskList=data.tasks||[];
  if(activeWorkspace!=='all'){
    taskList=taskList.filter(t=>{
      if(activeWorkspace==='primary')return !t.workspace||t.workspace==='';
      return t.workspace===activeWorkspace;
    });
  }
  const allTasks=taskList;const names=data.agent_names||[];
  // Apply filters
  let tasks=allTasks;
  if(taskSearch){const q=taskSearch.toLowerCase();tasks=tasks.filter(t=>(t.description||'').toLowerCase().includes(q)||('#'+t.id).includes(q));}
  if(taskFilterAgent)tasks=tasks.filter(t=>t.assigned_to===taskFilterAgent);
  if(taskFilterPriority)tasks=tasks.filter(t=>t.priority==parseInt(taskFilterPriority));
  const cols={to_do:[],in_progress:[],code_review:[],in_testing:[],uat:[],done:[],failed:[]};
  tasks.forEach(t=>{const s=t.status||'to_do';if(cols[s])cols[s].push(t);else if(s==='created'||s==='assigned')cols.to_do.push(t);else if(s==='in_review')cols.code_review.push(t);else if(s==='cancelled')cols.failed.push(t);});
  p.innerHTML=`<div class="new-task">
    <input id="newTaskDesc" placeholder="New task...">
    <select id="newTaskAgent">${names.map(n=>`<option value="${n}">${n}</option>`).join('')}</select>
    <select id="newTaskPriority"><option value="1">P1 Critical</option><option value="3">P3 High</option>
      <option value="5" selected>P5 Normal</option><option value="8">P8 Low</option></select>
    <input id="newTaskDeps" placeholder="Deps #" style="width:60px">
    <label style="font-size:10px;color:var(--fg2);display:flex;align-items:center;gap:3px;cursor:pointer"><input type="checkbox" id="newTaskSkipReview" style="accent-color:var(--ac)"> Skip review</label>
    <label style="font-size:10px;color:var(--fg2);display:flex;align-items:center;gap:3px;cursor:pointer"><input type="checkbox" id="newTaskSkipQa" style="accent-color:var(--ac)"> Skip QA</label>
    <button onclick="createTask()">+ Add</button>
  </div>
  <div class="new-task" style="margin-bottom:8px">
    <input placeholder="🔍 Search tasks..." value="${esc(taskSearch)}" oninput="taskSearch=this.value;renderPanel()" style="flex:1">
    <select onchange="taskFilterAgent=this.value;renderPanel()">
      <option value="">All agents</option>${names.map(n=>`<option value="${n}"${taskFilterAgent===n?' selected':''}>${n}</option>`).join('')}</select>
    <select onchange="taskFilterPriority=this.value;renderPanel()">
      <option value="">All priority</option>${[1,2,3,5,8,10].map(p=>`<option value="${p}"${taskFilterPriority==p?' selected':''}>P${p}</option>`).join('')}</select>
    <span style="font-size:10px;color:var(--fg3)">${tasks.length}/${allTasks.length}</span>
  </div>
  <div class="kanban">${Object.entries(cols).map(([status,items])=>`
    <div class="kanban-col"><div class="kanban-title">${statusIcon(status)} ${status.replace('_',' ')} <span class="kanban-count">${items.length}</span></div>
      ${items.map(t=>`<div class="task-card task-${t.status}" onclick="showTaskDetail(${t.id})" style="cursor:pointer">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span><span class="task-id">#${t.id}</span> <span class="task-agent">${t.assigned_to||'?'}</span></span>
          <span class="task-priority p${t.priority||5}">P${t.priority||5}</span>
        </div>
        ${t.depends_on?.length?`<span class="task-deps">← #${t.depends_on.join(',#')}</span>`:''}
        ${t.created_by&&t.created_by!=='user'?`<span class="task-origin">via ${t.created_by}</span>`:''}
        ${t.branch?`<span class="task-branch">⎇ ${esc(t.branch)}</span>`:''}
        <div class="task-desc">${esc(t.description||'').substring(0,100)}</div>
        ${t.elapsed_seconds||t.tokens_used?`<div style="font-size:9px;color:var(--fg3);margin-top:3px">${t.elapsed_seconds?(t.elapsed_seconds>=60?Math.floor(t.elapsed_seconds/60)+'m '+t.elapsed_seconds%60+'s':t.elapsed_seconds+'s'):''}${t.elapsed_seconds&&t.tokens_used?' · ':''}${t.tokens_used?formatTokens(t.tokens_used)+' tok':''}</div>`:''}
        <div class="task-actions" onclick="event.stopPropagation()">
          ${t.status==='created'?`<button class="task-btn task-start" onclick="assignTask(${t.id})">▶ Start</button>`:''}
          ${t.status==='in_progress'?`<button class="task-btn task-cancel" onclick="cancelTask(${t.id})">✗ Cancel</button>`:''}
          ${['done','failed','cancelled'].includes(t.status)?`<button class="task-btn" style="background:var(--cyan);color:#000" onclick="retryTask(${t.id},'${escAttr(t.assigned_to)}')">↻ Retry</button>`:''}
        </div></div>`).join('')}
    </div>`).join('')}</div>
  ${allTasks.some(t=>t.depends_on?.length)?`<div class="dep-graph"><h3>🔗 Dependency Graph</h3><canvas id="depCanvas" width="600" height="200"></canvas></div>`:''}`;
  // Draw dependency graph
  if(allTasks.some(t=>t.depends_on?.length))setTimeout(()=>drawDepGraph(allTasks),50);
  // Restore saved input values
  if(_savedDesc){const el=$('newTaskDesc');if(el)el.value=_savedDesc;}
  if(_savedAgent){const el=$('newTaskAgent');if(el)el.value=_savedAgent;}
  if(_savedDeps){const el=$('newTaskDeps');if(el)el.value=_savedDeps;}
  if(_focusedInTask&&_focusedInTask.startsWith('newTask')){const el=$(_focusedInTask);if(el)el.focus();}
}

function statusIcon(s){return{created:'📋',assigned:'👤',in_progress:'⚙️',in_review:'🔍',done:'✅',failed:'❌',cancelled:'🚫'}[s]||'•';}

function drawDepGraph(tasks){
  const canvas=$('depCanvas');if(!canvas)return;
  const ctx=canvas.getContext('2d');
  // Polyfill roundRect for older browsers
  if(!ctx.roundRect){ctx.roundRect=function(x,y,w,h,r){r=Math.min(r,w/2,h/2);this.beginPath();this.moveTo(x+r,y);this.lineTo(x+w-r,y);this.arcTo(x+w,y,x+w,y+r,r);this.lineTo(x+w,y+h-r);this.arcTo(x+w,y+h,x+w-r,y+h,r);this.lineTo(x+r,y+h);this.arcTo(x,y+h,x,y+h-r,r);this.lineTo(x,y+r);this.arcTo(x,y,x+r,y,r);this.closePath();};}
  const dpr=window.devicePixelRatio||1;
  const w=canvas.parentElement.clientWidth-24;
  canvas.width=w*dpr;canvas.height=200*dpr;
  canvas.style.width=w+'px';canvas.style.height='200px';
  ctx.scale(dpr,dpr);

  const nodes={};let col=0;
  // Layout: group by status
  const statusOrder=['created','assigned','in_progress','in_review','done','failed','cancelled'];
  const groups={};
  tasks.forEach(t=>{const s=t.status||'created';if(!groups[s])groups[s]=[];groups[s].push(t);});
  let x=40;
  statusOrder.forEach(s=>{
    const g=groups[s]||[];
    g.forEach((t,i)=>{nodes[t.id]={x,y:30+i*36,task:t};});
    if(g.length)x+=w/(statusOrder.filter(s2=>(groups[s2]||[]).length).length||1);
  });

  const isDark=document.documentElement.getAttribute('data-theme')!=='light';
  const style=getComputedStyle(document.documentElement);
  const cssVar=(v)=>style.getPropertyValue(v).trim()||v;
  // Draw edges
  ctx.strokeStyle=cssVar('--fg3');ctx.lineWidth=1.5;ctx.fillStyle=cssVar('--fg3');
  tasks.forEach(t=>{
    const to=nodes[t.id];if(!to)return;
    (t.depends_on||[]).forEach(did=>{
      const from=nodes[did];if(!from)return;
      ctx.beginPath();ctx.moveTo(from.x+20,from.y+8);
      ctx.bezierCurveTo(from.x+50,from.y+8,to.x-30,to.y+8,to.x-4,to.y+8);
      ctx.stroke();
      // Arrow
      ctx.beginPath();ctx.moveTo(to.x-4,to.y+8);ctx.lineTo(to.x-10,to.y+4);ctx.lineTo(to.x-10,to.y+12);ctx.fill();
    });
  });
  // Draw nodes
  const colors={created:cssVar('--fg3'),in_progress:cssVar('--blue'),done:cssVar('--green'),failed:cssVar('--red'),cancelled:cssVar('--fg3')};
  Object.values(nodes).forEach(n=>{
    const c=colors[n.task.status]||'#8b949e';
    ctx.fillStyle=c;ctx.beginPath();ctx.roundRect(n.x-4,n.y-4,48,24,4);ctx.fill();
    ctx.fillStyle='#fff';ctx.font='bold 10px sans-serif';ctx.textAlign='center';
    ctx.fillText('#'+n.task.id,n.x+20,n.y+12);
  });
}

async function createTask(){
  const desc=$('newTaskDesc')?.value?.trim(),agent=$('newTaskAgent')?.value;
  const pri=parseInt($('newTaskPriority')?.value||'5');
  const depsStr=$('newTaskDeps')?.value?.trim();
  if(!desc)return;
  const deps=depsStr?depsStr.split(',').map(d=>parseInt(d.replace('#','').trim())).filter(n=>n>0):[];
  const skipReview=$('newTaskSkipReview')?.checked||false;
  const skipQa=$('newTaskSkipQa')?.checked||false;
  await fetch(HUB+'/tasks',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({description:desc,assigned_to:agent,status:'created',depends_on:deps,priority:pri,created_by:'user',skip_review:skipReview,skip_qa:skipQa})});
  $('newTaskDesc').value='';$('newTaskDeps').value='';
  setTimeout(()=>{poll();renderPanel();},300);
}

async function assignTask(id){
  const task=(data.tasks||[]).find(t=>t.id===id);if(!task)return;
  const ready=await(await fetch(HUB+'/tasks/'+id+'/ready')).json();
  if(!ready.ready){alert('Deps not met: '+ready.reason);return;}
  await fetch(HUB+'/tasks/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'in_progress'})});
  await fetch(HUB+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sender:'user',receiver:task.assigned_to,content:`#${id} ${task.description}`,msg_type:'task'})});
  setTimeout(()=>{poll();renderPanel();},300);
}

async function cancelTask(id){
  await fetch(HUB+'/tasks/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'cancelled'})});
  const task=(data.tasks||[]).find(t=>t.id===id);
  if(task?.assigned_to)stopAgent(task.assigned_to);
  setTimeout(()=>{poll();renderPanel();},300);
}

async function showTaskDetail(id){
  let task=(data.tasks||[]).find(t=>t.id===id);
  // Fetch fresh data
  try{const t=await(await fetch(HUB+'/tasks/'+id)).json();if(t&&t.id)task=t;}catch{}
  if(!task)return;
  
  // Fetch related sub-tasks
  const subTasks=(data.tasks||[]).filter(t=>t.created_by===task.assigned_to&&t.id!==id);
  const parentRef=task.description?.match(/#(\d+)/)?.[1];
  
  const overlay=document.createElement('div');
  overlay.className='modal-overlay';
  overlay.addEventListener('click',e=>{if(e.target===overlay)overlay.remove();});
  
  const statusColors={created:'var(--fg3)',in_progress:'var(--blue)',done:'var(--green)',failed:'var(--red)',cancelled:'var(--fg3)'};
  const statusBg=statusColors[task.status]||'var(--fg3)';
  const names=data.agent_names||[];
  
  overlay.innerHTML=`<div class="modal-box" style="width:560px;max-height:85vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="task-id" style="font-size:18px">#${task.id}</span>
        <span class="task-priority p${task.priority||5}" style="font-size:11px">P${task.priority||5}</span>
        <span style="padding:3px 10px;border-radius:12px;font-size:11px;font-weight:500;background:${statusBg};color:#fff">
          ${statusIcon(task.status||'created')} ${(task.status||'created').replace('_',' ')}
        </span>
      </div>
      <button style="background:none;border:none;color:var(--fg3);cursor:pointer;font-size:18px;padding:4px" onclick="this.closest('.modal-overlay').remove()">✕</button>
    </div>
    
    <div style="font-size:13px;color:var(--fg);line-height:1.6;margin-bottom:16px;white-space:pre-wrap;word-break:break-word;padding:10px;background:var(--bg);border-radius:8px;border:1px solid var(--border)">${esc(task.description||'No description')}</div>
    
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:11px;margin-bottom:16px">
      <div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">Assigned to</span><br><strong style="color:var(--ac)">${esc(task.assigned_to||'unassigned')}</strong></div>
      <div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">Created by</span><br><strong>${esc(task.created_by||'user')}</strong></div>
      ${task.project?`<div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">Project</span><br><strong>${esc(task.project)}</strong></div>`:''}
      ${task.branch?`<div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">Branch</span><br><code style="color:var(--cyan);font-size:10px">${esc(task.branch)}</code></div>`:''}
      ${task.task_external_id?`<div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">External ID</span><br><strong style="color:var(--cyan)">${esc(task.task_external_id)}</strong></div>`:''}
      ${task.depends_on?.length?`<div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">Depends on</span><br><strong style="color:var(--yellow)">#${task.depends_on.join(', #')}</strong></div>`:''}
      ${task.created_at?`<div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">Created</span><br>${formatTime(task.created_at)}</div>`:''}
      ${task.updated_at?`<div style="padding:6px 8px;background:var(--bg);border-radius:6px"><span style="color:var(--fg3)">Updated</span><br>${formatTime(task.updated_at)}</div>`:''}
    </div>
    
    ${task.result?`<div style="margin-bottom:14px"><div style="font-size:10px;color:var(--fg3);margin-bottom:4px;font-weight:500">Result</div>
      <div style="padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;font-size:11px;white-space:pre-wrap;max-height:200px;overflow-y:auto">${esc(task.result)}</div>
    </div>`:''}
    
    ${subTasks.length?`<div style="margin-bottom:14px"><div style="font-size:10px;color:var(--fg3);margin-bottom:4px;font-weight:500">Related Sub-tasks</div>
      ${subTasks.slice(0,10).map(st=>`<div style="display:flex;gap:6px;align-items:center;padding:3px 0;font-size:11px">
        <span style="color:var(--fg3)">#${st.id}</span>
        <span>${statusIcon(st.status)} ${esc(st.assigned_to||'?')}</span>
        <span style="color:var(--fg2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc((st.description||'').substring(0,80))}</span>
      </div>`).join('')}
    </div>`:''}

    ${task.project?`<div style="display:flex;gap:6px;margin:8px 0">
      <button class="modal-btn-ok" style="font-size:11px;padding:4px 10px" onclick="showTaskDiff(${task.id})">View Diff</button>
      ${task.branch?`<button class="modal-btn-ok" style="font-size:11px;padding:4px 10px;background:var(--blue)" onclick="createPR('${escAttr(task.project)}','${escAttr(task.branch)}')">Create PR</button>
      <button class="modal-btn-ok" style="font-size:11px;padding:4px 10px;background:var(--green)" onclick="pushBranch('${escAttr(task.project)}')">Push</button>`:''}
    </div>`:''}

    <div class="modal-actions" style="flex-wrap:wrap;gap:6px">
      ${task.status==='created'?`<select id="_reassign" style="padding:5px 8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:11px">
        ${names.map(n=>`<option value="${esc(n)}"${n===task.assigned_to?' selected':''}>${esc(n)}</option>`).join('')}
      </select>
      <button class="modal-btn-ok" onclick="reassignTask(${task.id},$('_reassign').value);this.closest('.modal-overlay').remove()">▶ Assign & Start</button>`:''}
      <select onchange="changePriority(${task.id},this.value)" style="padding:5px 8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:11px">
        ${[1,3,5,8,10].map(pr=>`<option value="${pr}"${(task.priority||5)==pr?' selected':''}>P${pr}${pr===1?' Critical':pr===3?' High':pr===5?' Normal':pr===8?' Low':' Lowest'}</option>`).join('')}
      </select>
      ${task.status==='in_progress'?`<button class="modal-btn-cancel" style="background:var(--red);color:#fff;border:none" onclick="cancelTask(${task.id});this.closest('.modal-overlay').remove()">✗ Cancel</button>`:''}
      ${task.status==='in_progress'?`<button style="padding:5px 10px;background:var(--yellow);border:none;border-radius:6px;color:#000;cursor:pointer;font-size:11px" onclick="restartAgent('${escAttr(task.assigned_to)}');this.closest('.modal-overlay').remove()" title="Restart the agent working on this">⟲ Restart Agent</button>`:''}
      ${['done','failed','cancelled'].includes(task.status)?`<button style="padding:5px 10px;background:var(--cyan);border:none;border-radius:6px;color:#000;cursor:pointer;font-size:11px;font-weight:500" onclick="retryTask(${task.id},'${escAttr(task.assigned_to)}');this.closest('.modal-overlay').remove()">↻ Retry</button>`:''}
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Close</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
}

async function reassignTask(id,agent){
  await fetch(HUB+'/tasks/'+id,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({assigned_to:agent,status:'created'})});
  notify(`Reassigned #${id} → ${agent}`);
  setTimeout(poll,300);
}

async function changePriority(id,priority){
  await fetch(HUB+'/tasks/'+id,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({priority:parseInt(priority)})});
  notify(`Priority #${id} → P${priority}`);
  setTimeout(poll,300);
}

async function restartAgent(name){
  if(!confirm(`Restart agent "${name}"? This stops current work and restarts.`))return;
  await fetch(HUB+'/agents/'+name+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  setTimeout(async()=>{
    await fetch(HUB+'/agents/'+name+'/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    notify('⟲ Restarting '+name);
    poll();
  },1000);
}

async function retryTask(id,agent){
  // Reset task to created — preserve branch/project/external_id
  await fetch(HUB+'/tasks/'+id,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({status:'created',result:'',started_at:'',completed_at:''})});
  // Fetch task details including branch info
  let desc='',branch='',project='',extId='';
  try{const t=await(await fetch(HUB+'/tasks/'+id)).json();
    desc=t?.description||'';branch=t?.branch||'';project=t?.project||'';extId=t?.task_external_id||'';
  }catch{}
  // Send task message to agent with branch/project context
  await fetch(HUB+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sender:'user',receiver:agent,content:`#${id} ${desc}`,msg_type:'task',
      task_id:String(id),task_external_id:extId,project:project,branch:branch})});
  notify(`↻ Retrying #${id} → ${agent}`);
  setTimeout(poll,300);
}

async function showTaskDiff(tid) {
  const task = (data.tasks || []).find(t => t.id == tid);
  if (!task) return;
  const project = task.project || '';
  const branch = task.branch || '';
  if (!project) { toast('No project associated', 'warn'); return; }

  try {
    const r = await (await fetch(HUB + '/git/diff?project=' + encodeURIComponent(project) + '&branch=' + encodeURIComponent(branch))).json();
    if (r.status === 'error') { toast(r.message, 'error'); return; }

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

    const filesHtml = (r.files || []).map(f => {
      const icon = f.status === 'A' ? '+' : f.status === 'D' ? '-' : '~';
      const cls = f.status === 'A' ? 'diff-add' : f.status === 'D' ? 'diff-del' : '';
      return '<div class="' + cls + '" style="padding:2px 0">' + icon + ' ' + esc(f.path) + '</div>';
    }).join('');

    overlay.innerHTML = '<div class="modal-box" style="width:90vw;max-width:900px;max-height:85vh;display:flex;flex-direction:column">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">' +
      '<h3 style="font-size:14px">Diff — Task #' + tid + ' (' + esc(branch || 'working tree') + ')</h3>' +
      '<button onclick="this.closest(\'.modal-overlay\').remove()" style="background:none;border:none;color:var(--fg2);cursor:pointer;font-size:16px">✕</button>' +
      '</div>' +
      '<div style="display:flex;gap:12px;flex:1;overflow:hidden">' +
      '<div style="width:200px;flex-shrink:0;overflow-y:auto;border-right:1px solid var(--border);padding-right:8px">' +
      '<div style="font-size:11px;font-weight:600;margin-bottom:6px">Files (' + (r.files||[]).length + ')</div>' +
      '<div style="font-size:11px;font-family:monospace">' + filesHtml + '</div>' +
      '</div>' +
      '<div style="flex:1;overflow:auto">' +
      '<pre style="font-size:11px;font-family:monospace;white-space:pre-wrap;margin:0">' + formatDiff(r.diff || 'No changes') + '</pre>' +
      '</div></div></div>';

    document.body.appendChild(overlay);
  } catch (e) { toast('Error loading diff: ' + e, 'error'); }
}

async function createPR(project, branch) {
  const title = prompt('PR Title:', branch ? branch.replace('feature/', '').replace(/[-_]/g, ' ') : '');
  if (!title) return;
  try {
    const r = await (await fetch(HUB + '/git/pr', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project, branch, title})
    })).json();
    if (r.status === 'ok') {
      toast('PR created: ' + r.url, 'success', 6000);
    } else toast(r.message || 'Failed', 'error');
  } catch (e) { toast('Error: ' + e, 'error'); }
}

async function pushBranch(project) {
  try {
    const r = await (await fetch(HUB + '/git/push', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project})
    })).json();
    if (r.status === 'ok') toast('Pushed to origin/' + r.branch, 'success');
    else toast(r.message || 'Push failed', 'error');
  } catch (e) { toast('Error: ' + e, 'error'); }
}

async function inboxToTask(sender,content){
  const names=data.agent_names||[];
  let target='architect';
  try{const r=await(await fetch(HUB+'/route?msg='+encodeURIComponent(content))).json();if(r.target)target=r.target;}catch{}
  const r=await(await fetch(HUB+'/tasks',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({description:content,assigned_to:target,status:'created',priority:5,created_by:'user'})})).json();
  if(r?.id){notify(`📋 Created task #${r.id} from ${sender}'s message`);dismissSender(sender);}
}
async function renderActivity(p){
  let acts=[];try{acts=await(await fetch(HUB+'/activity?limit=100')).json();}catch{}
  // Filter out change/file review spam
  acts=acts.filter(a=>a.type!=='change');
  if(!acts.length){p.innerHTML='<div class="empty-state">📊 No activity</div>';return;}
  p.innerHTML=`<div class="activity-feed">${acts.reverse().map(a=>{
    const icon={task:'📋',message:'💬',question:'❓',info:'ℹ️',blocker:'🚫',stop:'⛔',change:'📝',
      task_create:'📋',task_update:'🔄',agent_add:'🆕',agent_remove:'🗑️',test_result:'🧪',
      git_rollback:'↩',learning:'🧠',notification:'🔔'}[a.type]||'•';
    return`<div class="activity-item"><span class="activity-time">${formatTime(a.time)}</span>
      <span class="activity-icon">${icon}</span>
      <span class="activity-actors">${esc(a.sender)}→${esc(a.receiver)}</span>
      <span class="activity-preview">${esc(a.preview).substring(0,120)}</span></div>`;
  }).join('')}</div>`;
}

// ── Locks ──
function renderLocks(p){
  const locks=data.locks||{};const entries=Object.entries(locks);
  if(!entries.length){p.innerHTML='<div class="empty-state">🔓 No active locks</div>';return;}
  p.innerHTML=`<div class="locks-list">${entries.map(([path,info])=>`<div class="lock-item">
    <span class="lock-file">${esc(path)}</span><span class="lock-agent">${esc(info.agent)}</span>
    <span class="lock-since">${formatTime(info.since)}</span></div>`).join('')}</div>`;
}

// ── Inbox ──
function renderInbox(p){
  if(!inbox.length){p.innerHTML='<div class="empty-state">💬 No messages</div>';return;}
  // Save reply input state
  const savedInputs={};
  const focusedId=document.activeElement?.id;
  document.querySelectorAll('[id^="rp_"]').forEach(el=>{
    if(el.value || el.id===focusedId) savedInputs[el.id]=el.value||'';
  });
  const threads={};inbox.forEach(m=>{
    const partner=m.sender==='user'?m.receiver:m.sender;
    if(!partner||partner==='user')return;
    if(!threads[partner])threads[partner]=[];
    threads[partner].push(m);
  });
  const agentNames=Object.keys(threads).sort();
  // Reset filter if selected agent has no messages
  if(_inboxAgent&&!threads[_inboxAgent])_inboxAgent='';
  const filtered=_inboxAgent?{[_inboxAgent]:threads[_inboxAgent]}:threads;
  const tabsHtml=`<div class="inbox-tabs"><button class="inbox-tab${!_inboxAgent?' active':''}" onclick="_inboxAgent='';renderPanel()">All <span class="inbox-tab-count">${inbox.length}</span></button>${agentNames.map(n=>{
    const cnt=threads[n].length;const ai=(data.agents||{})[n]||{};const ps=ai.pipeline||'offline';
    const dot=ps==='working'?'working':ps==='idle'?'idle':'offline';
    return`<button class="inbox-tab${_inboxAgent===n?' active':''}" onclick="_inboxAgent='${escAttr(n)}';renderPanel()"><span class="agent-dot dot-${dot} dot-inline"></span>${esc(n)} <span class="inbox-tab-count">${cnt}</span></button>`;
  }).join('')}</div>`;
  p.innerHTML=tabsHtml+`<div class="inbox-toolbar"><button class="inbox-clear-all" onclick="${_inboxAgent?`dismissSender('${escAttr(_inboxAgent)}')`:'dismissAll()'}">🗑️ ${_inboxAgent?'Clear':'Clear All'}</button>
    <span class="inbox-count">${Object.values(filtered).reduce((s,m)=>s+m.length,0)} msg${inbox.length>1?'s':''}</span></div>`+
    Object.entries(filtered).map(([agent,msgs])=>{
      const lastMsg=msgs[msgs.length-1];
      const replyId=`rp_${agent.replace(/\W/g,'_')}`;
      const agentInfo=(data.agents||{})[agent]||{};
      const ps=agentInfo.pipeline||'offline';
      const statusDot=ps==='working'?'working':ps==='idle'?'idle':ps==='booting'?'booting':'offline';
      const statusLabel=ps==='working'?'working...':ps==='idle'?'idle':ps;
      return`<div class="inbox-thread${_inboxAgent?' inbox-thread-solo':''}">
      ${_inboxAgent?'':`<div class="inbox-header">
        <span class="agent-dot dot-${statusDot} dot-inline"></span>
        <span class="inbox-sender">${esc(agent)}</span>
        <span class="chat-status-label">${statusLabel}</span>
        <span class="text-dim text-xs">${msgs.length} msg${msgs.length>1?'s':''}</span>
        <span class="inbox-time">${relativeTime(lastMsg.timestamp)}</span>
        <button class="inbox-dismiss" onclick="dismissSender('${escAttr(agent)}')">✕</button></div>`}
      <div class="chat-thread">
      ${msgs.map(m=>{
        const content=m.content||'';
        const isLong=content.length>500;
        const displayContent=isLong?esc(content.substring(0,500)):esc(content);
        const fullContent=isLong?esc(content):'';
        const timeStr=relativeTime(m.timestamp);
        if(m.sender==='user'){
          return`<div class="chat-bubble chat-bubble-user">${displayContent}${isLong?`<span class="bubble-collapsed" data-full="${fullContent.replace(/"/g,'&quot;')}">… <button class="bubble-toggle" onclick="expandBubble(this)">Show more</button></span>`:''}<div class="bubble-meta">you · ${timeStr}</div></div>`;
        }
        if(m.msg_type==='plan_proposal'){
          const planId=m.plan_id;
          const steps=m.plan_steps||[];
          // Check if plan was already approved/dismissed via pending_plans snapshot
          const planState=(data.pending_plans||{})[planId];
          const planStatus=planState?planState.status:'pending';
          const isDone=planStatus!=='pending';
          const stepsHtml=steps.map((s,i)=>{
            const dep=s.depends_on_step!=null?`<span class="plan-dep">after step ${Number(s.depends_on_step)+1}</span>`:'';
            const assignee=s.assigned_to||'';
            const desc=esc(s.description||'');
            const shortDesc=desc.length>120?desc.substring(0,120)+'…':'';
            const needsExpand=desc.length>120;
            return`<div class="plan-step" data-step="${i}">
              <div class="plan-step-header">
                <label class="plan-step-check"><input type="checkbox" ${isDone?'disabled':'checked'} data-plan="${planId}" data-idx="${i}" onchange="togglePlanStep(this)"><span class="plan-step-num">${i+1}</span></label>
                <div class="plan-step-meta">${assignee?`<span class="plan-agent-badge">${esc(assignee)}</span>`:''}${dep}</div>
              </div>
              <div class="plan-step-body">${needsExpand?`<span class="plan-desc-short">${shortDesc}</span><span class="plan-desc-full" style="display:none">${desc}</span> <button class="plan-expand-btn" onclick="this.previousElementSibling.style.display='inline';this.previousElementSibling.previousElementSibling.style.display='none';this.style.display='none'">Show more</button>`:desc}</div>
            </div>`;
          }).join('');
          const doneLabel=planStatus==='approved'?`<span style="font-size:11px;font-weight:600;color:var(--green)">Approved</span>`
            :planStatus==='dismissed'?`<span style="font-size:11px;font-weight:600;color:var(--fg3)">Dismissed</span>`:'';
          return`<div class="chat-bubble chat-bubble-agent plan-proposal-bubble"${isDone?' style="opacity:0.6"':''}>
            <div class="plan-header"><span class="plan-badge">Plan</span><span class="plan-title">${displayContent}</span></div>
            ${steps.length?`<div class="plan-steps">
              ${isDone?'':`<div class="plan-toolbar"><label class="plan-select-all"><input type="checkbox" checked onchange="toggleAllPlanSteps(this,${planId})"> Select all (${steps.length} steps)</label></div>`}
              ${stepsHtml}
            </div>
            <div class="plan-actions">
              ${isDone?doneLabel:`<button class="plan-btn plan-btn-approve" onclick="approvePlan(${planId})">Approve Selected</button>
              <button class="plan-btn plan-btn-dismiss" onclick="dismissPlan(${planId})">Dismiss</button>`}
            </div>`:''}
            <div class="bubble-meta">${m.sender||'architect'} · ${timeStr}</div></div>`;
        }
        if(m.msg_type==='review_request'){
          const proj=m.project||'';const branch=m.branch||'';const suggested=m.suggested_commit_msg||'';
          const commitId=`commit_${(proj+branch).replace(/\W/g,'_')}`;
          return`<div class="chat-bubble chat-bubble-agent review-request-bubble">
            <span class="bubble-type" style="background:var(--green);color:#000">review</span>
            ${displayContent}${isLong?`<span class="bubble-collapsed" data-full="${fullContent.replace(/"/g,'&quot;')}">… <button class="bubble-toggle" onclick="expandBubble(this)">Show more</button></span>`:''}
            <div class="commit-actions" style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">
              <input id="${commitId}" class="commit-msg-input" value="${escAttr(suggested)}" placeholder="Commit message..." style="width:100%;padding:6px 8px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--fg);font-size:12px;margin-bottom:6px">
              <div class="flex-center gap-4">
                <button class="btn-sm" style="background:var(--green);color:#000" onclick="commitChanges('${escAttr(proj)}','${commitId}')">✓ Commit</button>
                <button class="btn-sm" style="background:var(--ac);color:#fff" onclick="commitAndPush('${escAttr(proj)}','${commitId}')">🚀 Commit & Push</button>
                <button class="btn-sm btn-ghost" onclick="discardChanges('${escAttr(proj)}')">✗ Discard</button>
              </div>
            </div>
            <div class="bubble-meta">${timeStr}</div></div>`;
        }
        return`<div class="chat-bubble chat-bubble-agent">${m.msg_type&&m.msg_type!=='info'&&m.msg_type!=='message'?`<span class="bubble-type">${m.msg_type}</span>`:''
          }${displayContent}${isLong?`<span class="bubble-collapsed" data-full="${fullContent.replace(/"/g,'&quot;')}">… <button class="bubble-toggle" onclick="expandBubble(this)">Show more</button></span>`:''
          }${m.msg_type==='check_report'?`<div class="bubble-actions"><button class="btn-sm" style="background:var(--ac);color:#fff" onclick="navigator.clipboard.writeText(${JSON.stringify(content).replace(/'/g,"\\'")});toast('Copied','success',2000)">📋 Copy</button></div>`:''
          }<div class="bubble-meta">${timeStr}</div></div>`;
      }).join('')}
      </div>
      <div class="inbox-reply"><input id="${replyId}" placeholder="Reply to ${esc(agent)}..." onkeydown="if(event.key==='Enter'){event.preventDefault();replyTo('${escAttr(agent)}','${replyId}')}">
        <button onclick="replyTo('${escAttr(agent)}','${replyId}')">Reply</button>
        <button onclick="inboxToTask('${escAttr(agent)}',${JSON.stringify(lastMsg.content.substring(0,500))})" title="Convert to task" class="btn-ghost btn-sm">📋</button></div></div>`;}).join('');

  // Restore saved input values
  for(const [id,val] of Object.entries(savedInputs)){
    const el=$(id);if(el){el.value=val;}
  }
  if(focusedId&&focusedId.startsWith('rp_')){
    const el=$(focusedId);
    if(el){el.focus();el.selectionStart=el.selectionEnd=el.value.length;}
  }
}

function expandBubble(btn){
  const span=btn.parentElement;
  const full=span.dataset.full;
  if(!full)return;
  const bubble=span.closest('.chat-bubble');
  // Replace truncated text + toggle with full text
  const meta=bubble.querySelector('.bubble-meta');
  const actions=bubble.querySelector('.bubble-actions');
  const typeSpan=bubble.querySelector('.bubble-type');
  bubble.innerHTML=(typeSpan?typeSpan.outerHTML:'')+full+(actions?actions.outerHTML:'')+(meta?meta.outerHTML:'');
}

function startChatWith(agent){
  const ci=$('cmdInput');
  if(ci){ci.value=`@${agent} `;ci.focus();autoResize(ci);updateRouteHint(ci.value);}
}

async function dismissSender(s){await fetch(HUB+'/messages/user/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sender:s})});setTimeout(fetchInbox,200);}
async function dismissAll(){if(!confirm('Clear all?'))return;await fetch(HUB+'/messages/user/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})});setTimeout(fetchInbox,200);}

async function replyTo(sender,inputId){
  const input=$(inputId);const text=(input?.value||'').trim();if(!text)return;
  input.value='';
  // Add reply visually to the thread immediately
  const thread=input.closest('.inbox-thread');
  if(thread){
    const chatThread=thread.querySelector('.chat-thread');
    const replyDiv=document.createElement('div');
    replyDiv.className='chat-bubble chat-bubble-user';
    replyDiv.innerHTML=`${esc(text)}<div class="bubble-meta">you · now</div>`;
    if(chatThread)chatThread.appendChild(replyDiv);
    else thread.querySelector('.inbox-reply')?.before(replyDiv);
  }
  await fetch(HUB+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sender:'user',receiver:sender,content:text,msg_type:'message'})});
  notify(`→ ${sender}: ${text.substring(0,60)}`);
  setTimeout(fetchInbox,500);
}

// ── Review ──
let reviewFilter='pending',reviewFilterProject='',reviewFilterBranch='__current__';

function _syncReviewSelections(){
  const pendingIds=new Set(reviewData.filter(c=>c.status==='pending').map(c=>c.id));
  // Init new pending changes with all files selected
  for(const c of reviewData){
    if(c.status==='pending'&&!reviewSelections.has(c.id)){
      reviewSelections.set(c.id,new Set((c.files||[]).map(f=>f.path)));
    }
  }
  // Clean up removed/non-pending
  for(const cid of reviewSelections.keys()){
    if(!pendingIds.has(cid))reviewSelections.delete(cid);
  }
}
function toggleFile(cid,fileIndex,checked){
  const c=reviewData.find(x=>x.id===cid);if(!c)return;
  const path=(c.files||[])[fileIndex]?.path;if(!path)return;
  const sel=reviewSelections.get(cid);if(!sel)return;
  if(checked)sel.add(path);else sel.delete(path);
  if(tab==='review')renderPanel();
}
function toggleAllFiles(cid,checked){
  const c=reviewData.find(x=>x.id===cid);if(!c)return;
  const sel=reviewSelections.get(cid);if(!sel)return;
  if(checked)(c.files||[]).forEach(f=>sel.add(f.path));else sel.clear();
  if(tab==='review')renderPanel();
}
function _allSelected(cid,files){
  const sel=reviewSelections.get(cid);
  return sel&&files.length>0&&files.every(f=>sel.has(f.path));
}
function _getSelectedFiles(cid){
  const sel=reviewSelections.get(cid);
  return sel?[...sel]:[];
}
async function renderReview(p){
  const all=reviewData||[];
  // Get current branches per project from git status
  let currentBranches={};
  try{const gs=await(await fetch(HUB+'/git/status')).json();for(const[proj,info]of Object.entries(gs)){if(info.branch)currentBranches[proj]=info.branch;}}catch{}
  // Resolve __current__ filter — match any project's current branch
  const currentBranchValues=Object.values(currentBranches);
  const isCurrentFilter=reviewFilterBranch==='__current__';

  const counts={pending:0,approved:0,dismissed:0};
  all.forEach(c=>counts[c.status]=(counts[c.status]||0)+1);
  const projects=[...new Set(all.map(c=>c.project).filter(Boolean))].sort();
  const branches=[...new Set(all.map(c=>c.branch).filter(Boolean))].sort();
  let filtered=reviewFilter?all.filter(c=>c.status===reviewFilter):all;
  if(reviewFilterProject)filtered=filtered.filter(c=>c.project===reviewFilterProject);
  if(reviewFilterBranch){
    if(isCurrentFilter){
      filtered=filtered.filter(c=>{
        const projBranch=currentBranches[c.project];
        return projBranch&&c.branch===projBranch;
      });
    }else{
      filtered=filtered.filter(c=>c.branch===reviewFilterBranch);
    }
  }
  const currentBranchLabel=isCurrentFilter&&currentBranchValues.length?currentBranchValues[0]:'';
  p.innerHTML=`<div class="flex-center flex-wrap gap-6 mb-10">
    <button class="tab-btn${reviewFilter==='pending'?' active':''}" onclick="reviewFilter='pending';renderReview($('panel'))">
      ⏳ Pending <span class="badge" style="display:inline">${counts.pending}</span></button>
    <button class="tab-btn${reviewFilter==='approved'?' active':''}" onclick="reviewFilter='approved';renderReview($('panel'))">
      ✅ Approved <span class="badge" style="display:inline;background:var(--green)">${counts.approved}</span></button>
    <button class="tab-btn${reviewFilter==='dismissed'?' active':''}" onclick="reviewFilter='dismissed';renderReview($('panel'))">
      ✗ Dismissed <span class="badge" style="display:inline;background:var(--fg3)">${counts.dismissed}</span></button>
    <button class="tab-btn${!reviewFilter?' active':''}" onclick="reviewFilter='';renderReview($('panel'))">All (${all.length})</button>
    ${projects.length>1?`<select class="select-sm" style="max-width:160px" onchange="reviewFilterProject=this.value;renderReview($('panel'))">
      <option value="">All projects</option>
      ${projects.map(pr=>`<option value="${escAttr(pr)}"${reviewFilterProject===pr?' selected':''}>${esc(pr)}</option>`).join('')}
    </select>`:''}
    <select class="select-sm" style="max-width:220px" onchange="reviewFilterBranch=this.value;renderReview($('panel'))">
      <option value="__current__"${isCurrentFilter?' selected':''}>⎇ Current branch${currentBranchLabel?' ('+currentBranchLabel+')':''}</option>
      <option value=""${reviewFilterBranch===''?' selected':''}>All branches</option>
      ${branches.map(br=>`<option value="${escAttr(br)}"${reviewFilterBranch===br?' selected':''}>${esc(br)}</option>`).join('')}
    </select>
    <button class="btn-sm btn-ghost ml-auto" onclick="fetchChanges()">↻</button>
  </div>` +
  (!filtered.length?`<div class="empty-state">🔍 ${all.length?'No changes matching filters':'No code changes yet. Changes appear here after agents commit.'}</div>`:
  filtered.slice(0,40).map(c=>{
    const branch=c.branch||'';
    const taskRef=branch.startsWith('feature/')?branch.replace('feature/',''):c.agent;
    const statusColor=c.status==='approved'?'var(--green)':c.status==='dismissed'?'var(--fg3)':'var(--yellow)';
    const fileCount=(c.files||[]).length;
    return`<div class="card mb-8" style="padding:8px 10px;border-left:3px solid ${statusColor}">
    <div class="flex-center flex-wrap gap-6">
      <strong class="text-base">${esc(taskRef)}</strong>
      <span class="text-sm text-ac">${esc(c.project)}</span>
      ${branch?`<code class="text-xs text-cyan">⎇ ${esc(branch)}</code>`:''}
      <span class="text-xs text-dim">${c.status==='pending'&&reviewSelections.has(c.id)?`${reviewSelections.get(c.id).size}/${fileCount} files`:fileCount+' file'+(fileCount!==1?'s':'')}</span>
      <span class="text-xs text-dim">${formatTime(c.timestamp)}</span>
      ${c.status==='pending'?`<span class="ml-auto"></span>`:
        `<span class="text-sm font-bold ml-auto" style="color:${statusColor}">${c.status}</span>`}
    </div>
    ${c.description?`<div class="text-sm text-muted truncate" style="margin:4px 0">${esc(c.description).substring(0,200)}</div>`:''}
    ${c.status==='pending'?`<div style="margin:6px 0">
      <input id="rc_msg_${c.id}" class="commit-msg-input" value="${escAttr(taskRef+' | '+cleanTitle((c.description||'').substring(0,80)))}" placeholder="Commit message..." style="width:100%;padding:5px 8px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--fg);font-size:12px;margin-bottom:4px">
      <div class="flex-center gap-4">
        <button class="btn-sm" style="background:var(--green);color:#000" onclick="commitChanges('${escAttr(c.project)}','rc_msg_${c.id}',${c.id})">✓ Commit</button>
        <button class="btn-sm" style="background:var(--ac);color:#fff" onclick="commitAndPush('${escAttr(c.project)}','rc_msg_${c.id}',${c.id})">🚀 Commit & Push</button>
        <button class="btn-sm btn-ghost" onclick="reviewChange(${c.id},'dismissed')">✗ Dismiss</button>
      </div>
    </div>`:''}
    ${c.status==='pending'&&(c.files||[]).length>1?`<div class="mt-4 mb-4"><label style="font-size:10px;color:var(--fg2);cursor:pointer;display:inline-flex;align-items:center;gap:4px"><input type="checkbox" ${_allSelected(c.id,c.files||[])?'checked':''} onchange="toggleAllFiles(${c.id},this.checked)" style="accent-color:var(--ac);width:11px;height:11px"> Select all</label></div>`:''}
    <div class="flex-wrap gap-4${c.status==='pending'?'':' mt-4'}">${(c.files||[]).map((f,fi)=>{
      const icon=f.status==='modified'?'M':f.status==='added'?'A':f.status==='deleted'?'D':f.status[0].toUpperCase();
      const col=f.status==='added'?'var(--green)':f.status==='deleted'?'var(--red)':'var(--yellow)';
      const checked=c.status==='pending'&&reviewSelections.has(c.id)&&reviewSelections.get(c.id).has(f.path);
      const dim=c.status==='pending'&&reviewSelections.has(c.id)&&!checked;
      return`<label class="text-xs font-mono badge-sm bg-base border" style="cursor:${c.status==='pending'?'pointer':'default'};display:inline-flex;align-items:center;gap:3px;${dim?'opacity:0.4':''}">${c.status==='pending'?`<input type="checkbox" ${checked?'checked':''} onchange="toggleFile(${c.id},${fi},this.checked)" style="accent-color:var(--ac);width:11px;height:11px">`:''}<span class="font-bold" style="color:${col}">${icon}</span> ${esc(f.path)}</label>`;}).join('')}</div>
    ${c.diff?`<details class="mt-6"><summary class="text-sm text-dim" style="cursor:pointer">Diff (${c.diff.split('\\n').length} lines)</summary><div class="review-diff">${formatDiff(c.diff||'')}</div></details>`:''}
  </div>`;}).join(''));
}

// ── Config / Autonomy ──
async function fetchConfig() {
  try {
    data._config = await (await fetch(HUB + '/config')).json();
  } catch {}
}

function renderAutonomySettings() {
  const cfg = data._config || {};
  return `<div class="settings-panel">
    <h3 style="font-size:13px;margin-bottom:10px">Autonomy Settings</h3>
    <div class="settings-grid">
      <label class="setting-row">
        <input type="checkbox" id="_autoUat" ${cfg.auto_uat ? 'checked' : ''}>
        <span>Auto-UAT</span>
        <span class="text-dim text-xs">Skip manual approval, tasks go directly to done</span>
      </label>
      <label class="setting-row">
        <span>UAT Timeout (sec)</span>
        <input type="number" id="_uatTimeout" value="${cfg.auto_uat_timeout || 0}" min="0" style="width:80px" class="modal-input">
        <span class="text-dim text-xs">0 = manual, &gt;0 = auto-approve after N seconds</span>
      </label>
      <label class="setting-row">
        <input type="checkbox" id="_autoPlan" ${cfg.auto_plan_approval ? 'checked' : ''}>
        <span>Auto-Plan Approval</span>
        <span class="text-dim text-xs">Approve all plans without user confirmation</span>
      </label>
      <label class="setting-row">
        <input type="checkbox" id="_autoSinglePlan" ${cfg.auto_plan_single_step !== false ? 'checked' : ''}>
        <span>Auto-Single-Step Plan</span>
        <span class="text-dim text-xs">Auto-approve plans with a single step</span>
      </label>
      <label class="setting-row">
        <span>Escalation Threshold</span>
        <input type="number" id="_escalationThreshold" value="${cfg.escalation_threshold || 3}" min="0" style="width:80px" class="modal-input">
        <span class="text-dim text-xs">Failures before escalating to architect (0 = disabled)</span>
      </label>
    </div>
    <button class="modal-btn-ok" style="margin-top:10px" onclick="saveAutonomySettings()">Save Settings</button>
  </div>`;
}

async function saveAutonomySettings() {
  const payload = {
    auto_uat: $('_autoUat')?.checked || false,
    auto_uat_timeout: parseInt($('_uatTimeout')?.value || '0'),
    auto_plan_approval: $('_autoPlan')?.checked || false,
    auto_plan_single_step: $('_autoSinglePlan')?.checked || false,
    escalation_threshold: parseInt($('_escalationThreshold')?.value || '3'),
  };
  try {
    const r = await (await fetch(HUB + '/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    })).json();
    if (r.status === 'ok') {
      toast('Settings saved', 'success');
      data._config = {...(data._config || {}), ...payload};
    } else toast('Error: ' + (r.message || 'unknown'), 'error');
  } catch (e) { toast('Error: ' + e, 'error'); }
}

// ── Workspace Tabs ──
function renderWorkspaceBar(workspaces) {
  const bar = $('workspaceBar');
  if (!bar) return;
  const wsEntries = Object.entries(workspaces || {});
  bar.style.display = 'flex';
  const wsName = data.workspace ? data.workspace.split('/').pop() : 'primary';
  let html = '';
  if (wsEntries.length > 0) {
    html += `<button class="ws-tab${activeWorkspace === 'all' ? ' ws-active' : ''}" onclick="switchWorkspace('all')">All</button>`;
    html += `<button class="ws-tab${activeWorkspace === 'primary' ? ' ws-active' : ''}" onclick="switchWorkspace('primary')">${esc(wsName)}</button>`;
    for (const [wsId, ws] of wsEntries) {
      html += `<button class="ws-tab${activeWorkspace === wsId ? ' ws-active' : ''}" onclick="switchWorkspace('${escAttr(wsId)}')">${esc(ws.name || wsId)} <span class="ws-remove" onclick="event.stopPropagation();removeWorkspace('${escAttr(wsId)}','${escAttr(ws.name || wsId)}')" title="Remove workspace">&times;</span></button>`;
    }
  } else {
    html += `<button class="ws-tab ws-active">${esc(wsName)}</button>`;
  }
  html += `<button class="ws-tab ws-add" onclick="showAddWorkspaceModal()" title="Add workspace">+</button>`;
  bar.innerHTML = html;
}

function switchWorkspace(wsId) {
  activeWorkspace = wsId;
  renderWorkspaceBar(data.workspaces || {});
  renderPanel();
}

function showAddWorkspaceModal() {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  overlay.innerHTML = `<div class="modal-box" style="width:420px">
    <h3 style="margin-bottom:12px;font-size:14px">Add Workspace</h3>
    <label class="text-sm text-dim">Path</label>
    <input id="_wsPath" class="modal-input" placeholder="/path/to/repo">
    <label class="text-sm text-dim" style="margin-top:8px">Alias (optional)</label>
    <input id="_wsAlias" class="modal-input" placeholder="my-project">
    <div class="modal-actions">
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
      <button class="modal-btn-ok" onclick="addWorkspace()">Add</button>
    </div></div>`;
  document.body.appendChild(overlay);
  setTimeout(() => $('_wsPath')?.focus(), 50);
}

async function addWorkspace() {
  const path = $('_wsPath')?.value?.trim();
  const alias = $('_wsAlias')?.value?.trim();
  if (!path) { toast('Path required', 'warn'); return; }
  try {
    const r = await (await fetch(HUB + '/workspaces/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path, alias})
    })).json();
    if (r.status === 'ok') {
      toast('Workspace added: ' + (alias || path.split('/').pop()), 'success');
      document.querySelector('.modal-overlay')?.remove();
      switchWorkspace(r.ws_id);
    } else toast(r.message || 'Failed', 'error');
  } catch (e) { toast('Error: ' + e, 'error'); }
}

async function removeWorkspace(wsId, name) {
  if (!confirm('Remove workspace "' + name + '"?')) return;
  try {
    const r = await (await fetch(HUB + '/workspaces/' + wsId, { method: 'DELETE' })).json();
    if (r.status === 'ok') {
      toast('Workspace removed: ' + name, 'success');
      if (activeWorkspace === wsId) activeWorkspace = 'all';
      poll();
    } else toast(r.message || 'Failed', 'error');
  } catch (e) { toast('Error: ' + e, 'error'); }
}

// ── Analytics ──
async function renderAnalytics(p){
  let an={};try{an=await(await fetch(HUB+'/analytics')).json();}catch{p.innerHTML='<div class="empty-state">📈 Analytics unavailable</div>';return;}
  const ba=an.by_agent||{};const dur=an.durations||[];
  // Use keys from by_agent (includes all agents with data) + data.agent_names
  const names=[...new Set([...(data.agent_names||[]),...Object.keys(ba)])];
  const totTok=Object.values(ba).reduce((s,a)=>s+(a.tokens_in||0)+(a.tokens_out||0),0);
  const totReq=Object.values(ba).reduce((s,a)=>s+(a.requests||0),0);
  const totCost=Object.values(ba).reduce((s,a)=>s+((a.cost||0)*1),0);
  const tests=an.tests||{};

  p.innerHTML=`${renderAutonomySettings()}
  <div class="analytics-grid">
    <div class="stat-card"><div class="stat-label">Tokens</div><div class="stat-value">${formatTokens(totTok)}</div></div>
    <div class="stat-card"><div class="stat-label">API Calls</div><div class="stat-value">${totReq}</div></div>
    <div class="stat-card"><div class="stat-label">Tasks Done</div><div class="stat-value text-green">${an.tasks_done||0}</div><div class="stat-sub">${an.tasks_pending||0} pending</div></div>
    <div class="stat-card"><div class="stat-label">Cost</div><div class="stat-value">${formatCost(totCost)}</div>
      ${an.budget?.limit?`<div class="stat-sub">${an.budget.remaining!=null?formatCost(an.budget.remaining)+' left':'unlimited'}</div>`:''}</div>
  </div>
  ${modelBreakdownHTML(ba)}
  ${tests.total_passed||tests.total_failed?`<div class="test-mini">🧪 Tests: <span class="text-green">✓${tests.total_passed||0}</span> <span class="text-red">✗${tests.total_failed||0}</span> lint:${tests.lint_errors||0}</div>`:''}
  ${names.length?`<h3 class="text-lg text-muted" style="margin:16px 0 8px">Agent Usage</h3>
  <div class="agent-usage-table">${names.map(n=>{
    const a=ba[n]||{},total=(a.tokens_in||0)+(a.tokens_out||0),pct=totTok?Math.round(total/totTok*100):0;
    const ai=(data.agents||{})[n]||{};const ps=ai.pipeline||'offline';
    const dot=ps==='working'?'working':ps==='idle'?'idle':'offline';
    return`<div class="agent-usage-row">
      <span class="agent-usage-name"><span class="agent-dot dot-${dot} dot-inline"></span>${esc(n)}</span>
      <span class="agent-usage-bar-wrap"><span class="agent-usage-bar" style="width:${Math.max(2,pct)}%"></span></span>
      <span class="agent-usage-stat">${formatTokens(total)}</span>
      <span class="agent-usage-stat">${a.requests||0} calls</span>
      <span class="agent-usage-stat">${formatCost((a.cost||0)*1)}</span>
    </div>`;}).join('')}</div>`:''}
  ${dur.length?`<h3 class="text-lg text-muted" style="margin:16px 0 8px">Task Durations</h3>
    <div class="duration-list">${dur.slice(-10).reverse().map(d=>{
      const mx=Math.max(1,...dur.map(x=>x.seconds||1)),w=Math.max(5,((d.seconds||1)/mx)*100);
      const c=d.status==='done'?'var(--green)':'var(--red)';
      return`<div class="duration-item"><span style="width:80px">#${d.task} (${d.agent})</span>
        <div style="flex:1"><div class="dur-bar" style="width:${w}%;background:${c}"></div></div>
        <span>${d.seconds||0}s</span></div>`;}).join('')}</div>`:''}
  <h3 class="text-lg text-muted" style="margin:16px 0 8px">🔑 Service Connections</h3>
  <div id="servicesArea" class="text-md">Loading...</div>
  <div class="flex-wrap gap-4 mt-8">
    <button class="task-btn task-start" onclick="showServiceWizard()">+ Connect Service</button>
    <button class="btn-sm btn-ghost" style="opacity:0.7" onclick="showManualCred()">Manual Key</button>
  </div>`;
  // Load services status
  try{
    const [cr,sv]=await Promise.all([
      (await fetch(HUB+'/credentials')).json(),
      (await fetch(HUB+'/services')).json()
    ]);
    const area=$('servicesArea');
    if(area){
      const svcs=(sv.services||[]).filter(s=>s.id!=='custom');
      const connected=svcs.filter(s=>s.connected);
      const available=svcs.filter(s=>!s.connected);
      let html='';
      if(connected.length){
        html+=connected.map(s=>`<div class="flex-center gap-8 py-4">
          <span style="font-size:14px">${s.icon}</span>
          <span class="text-green font-bold">${esc(s.name)}</span>
          <span class="text-sm text-green">● Connected</span>
          <button class="btn-sm task-cancel ml-auto" onclick="disconnectService('${escAttr(s.id)}')">Disconnect</button>
        </div>`).join('');
      }
      if(available.length){
        html+=`<div class="mt-6 text-sm text-dim">${available.map(s=>
          `<span style="cursor:pointer;opacity:0.6;margin-right:8px" onclick="showServiceWizard('${escAttr(s.id)}')" title="Connect ${esc(s.name)}">${s.icon} ${esc(s.name)}</span>`
        ).join('')}</div>`;
      }
      // Also show raw credential keys
      const keys=Object.entries(cr.credentials||{});
      if(keys.length){
        html+=`<details style="margin-top:8px"><summary style="font-size:10px;color:var(--fg3);cursor:pointer">Raw credentials (${keys.length})</summary>
          ${keys.map(([k,v])=>`<div style="display:flex;align-items:center;gap:6px;padding:1px 0;font-size:10px">
            <code style="color:var(--cyan)">${esc(k)}</code> <span style="color:var(--fg3)">${esc(v)}</span>
            <button style="font-size:8px;background:none;border:1px solid var(--border);color:var(--fg3);border-radius:3px;cursor:pointer;padding:0 3px" onclick="delCred('${escAttr(k)}')">✗</button>
          </div>`).join('')}
        </details>`;
      }
      area.innerHTML=html||'<span style="color:var(--fg3)">No services connected. Click "Connect Service" to set up MCP authentication.</span>';
    }
  }catch{}
}

function modelBreakdownHTML(ba){
  let st=0,ot=0,ht=0;for(const a of Object.values(ba)){st+=(a.sonnet_in||0)+(a.sonnet_out||0);ot+=(a.opus_in||0)+(a.opus_out||0);ht+=(a.haiku_in||0)+(a.haiku_out||0);}
  if(!st&&!ot&&!ht)return'';const total=st+ot+ht||1,sp=Math.round(st/total*100),op=Math.round(ot/total*100),hp=100-sp-op;
  return`<div class="model-breakdown"><div style="font-size:11px;color:var(--fg2);margin-bottom:8px">Model Usage</div>
    <div class="model-bar"><div class="model-sonnet" style="width:${sp}%">${sp>10?'Sonnet '+sp+'%':''}</div>
      <div class="model-opus" style="width:${op}%">${op>10?'Opus '+op+'%':''}</div>${ht?`<div class="model-haiku" style="width:${hp}%;background:var(--green)">${hp>10?'Haiku '+hp+'%':''}</div>`:''}</div>
    <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:10px;color:var(--fg2)">
      <span>🔵 Sonnet: ${formatTokens(st)}</span><span>🟣 Opus: ${formatTokens(ot)}</span>${ht?`<span>🟢 Haiku: ${formatTokens(ht)}</span>`:''}</div></div>`;
}

// ── Credentials ──
async function saveCred(){
  const key=$('credKey')?.value?.trim(),val=$('credVal')?.value?.trim();
  if(!key||!val){notify('Enter both key and value');return;}
  const body={};body[key]=val;
  const r=await(await fetch(HUB+'/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(r.status==='ok'){notify('🔑 Saved: '+key);$('credKey').value='';$('credVal').value='';renderPanel();}
  else notify('Error: '+(r.message||'failed'));
}
async function delCred(key){
  if(!confirm('Delete credential: '+key+'?'))return;
  await fetch(HUB+'/credentials/'+encodeURIComponent(key),{method:'DELETE'});
  notify('Deleted: '+key);renderPanel();
}

async function disconnectService(svcId){
  let svcs=[];try{svcs=(await(await fetch(HUB+'/services')).json()).services||[];}catch{return;}
  const svc=svcs.find(s=>s.id===svcId);if(!svc)return;
  if(!confirm(`Disconnect ${svc.name}? This will delete saved credentials.`))return;
  for(const c of (svc.credentials||[])){
    await fetch(HUB+'/credentials/'+encodeURIComponent(c.key),{method:'DELETE'});
  }
  notify(`Disconnected: ${svc.name}`);renderPanel();
}

function showManualCred(){
  const overlay=document.createElement('div');
  overlay.className='modal-overlay';
  overlay.innerHTML=`<div class="modal-box" style="width:380px">
    <h3 class="mb-12" style="font-size:14px">🔑 Manual Credential</h3>
    <label class="text-base text-muted mb-4" style="display:block">Key</label>
    <input id="_mcKey" class="modal-input" placeholder="e.g. FIGMA_ACCESS_TOKEN">
    <label class="text-base text-muted" style="display:block;margin:8px 0 4px">Value</label>
    <input id="_mcVal" class="modal-input" type="password" placeholder="Token or secret value">
    <div class="modal-actions">
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
      <button class="modal-btn-ok" onclick="saveManualCred()">Save</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  setTimeout(()=>$('_mcKey')?.focus(),50);
}

async function saveManualCred(){
  const key=$('_mcKey')?.value?.trim(),val=$('_mcVal')?.value?.trim();
  if(!key||!val){notify('Both key and value required');return;}
  const body={};body[key]=val;
  const r=await(await fetch(HUB+'/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  document.querySelector('.modal-overlay')?.remove();
  if(r.status==='ok'){notify('🔑 Saved: '+key);renderPanel();}
}

async function showServiceWizard(preselect){
  let svcs=[];try{svcs=(await(await fetch(HUB+'/services')).json()).services||[];}catch{return;}
  const overlay=document.createElement('div');
  overlay.className='modal-overlay';

  if(preselect){
    // Direct connect for specific service
    const svc=svcs.find(s=>s.id===preselect);
    if(!svc){overlay.remove();return;}
    renderServiceForm(overlay,svc);
    document.body.appendChild(overlay);
    return;
  }

  // Service picker
  overlay.innerHTML=`<div class="modal-box" style="width:480px;max-height:80vh;overflow-y:auto">
    <h3 style="margin-bottom:4px;font-size:15px">🔗 Connect a Service</h3>
    <p style="font-size:11px;color:var(--fg3);margin-bottom:14px">Connect MCP tools to external services. Click a service to authenticate.</p>
    <div class="service-grid" id="_svcGrid">${svcs.map(s=>`<div class="service-card${s.connected?' connected':''}" data-svc="${escAttr(s.id)}">
      <span class="service-icon" style="background:${s.color||'#666'}">${s.icon}</span>
      <span class="service-name">${esc(s.name)}</span>
      ${s.connected?'<span class="service-status">● Connected</span>':''}
    </div>`).join('')}</div>
    <div class="modal-actions">
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Close</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  // Delegate click handling
  const grid=$('_svcGrid');
  if(grid) grid.addEventListener('click',e=>{
    const card=e.target.closest('.service-card');
    if(!card)return;
    const svcId=card.dataset.svc;
    if(svcId==='custom'){overlay.remove();showManualCred();return;}
    connectService(card,svcId);
  });
}

async function connectService(el,svcId){
  let svcs=[];try{svcs=(await(await fetch(HUB+'/services')).json()).services||[];}catch{return;}
  const svc=svcs.find(s=>s.id===svcId);if(!svc)return;
  const overlay=el.closest('.modal-overlay');
  renderServiceForm(overlay,svc);
}

function renderServiceForm(overlay,svc){
  const hasUrl=svc.auth_url&&svc.auth_url.length>5;
  overlay.innerHTML=`<div class="modal-box" style="width:440px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <span style="font-size:24px;width:40px;height:40px;display:flex;align-items:center;justify-content:center;border-radius:10px;background:${svc.color||'#666'};color:#fff">${svc.icon}</span>
      <div>
        <h3 style="margin:0;font-size:15px">${esc(svc.name)}</h3>
        <span style="font-size:10px;color:var(--fg3)">MCP Authentication</span>
      </div>
    </div>
    ${hasUrl?`<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:14px">
      <div style="font-size:11px;color:var(--fg2);margin-bottom:6px">Step 1: Open the authentication page</div>
      <a href="${esc(svc.auth_url)}" target="_blank" rel="noopener" class="modal-btn-ok" style="width:100%;display:block;text-align:center;text-decoration:none;box-sizing:border-box">
        🔐 Open ${esc(svc.name)} Auth Page
      </a>
      ${svc.docs?`<a href="${esc(svc.docs)}" target="_blank" rel="noopener" style="font-size:9px;color:var(--fg3);display:block;margin-top:6px">📖 Documentation</a>`:''}
    </div>`:''}
    ${svc.setup_note?`<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin-bottom:14px;font-size:10px;color:var(--fg2);white-space:pre-line">${esc(svc.setup_note)}</div>`:''}
    <div style="font-size:11px;color:var(--fg2);margin-bottom:8px">${hasUrl?'Step 2: Paste':'Enter'} your credentials below</div>
    ${svc.credentials.map(c=>`<div style="margin-bottom:10px">
      <label style="font-size:11px;color:var(--fg2);display:block;margin-bottom:3px">${esc(c.label)}</label>
      <input id="_svc_${escAttr(c.key)}" class="modal-input" type="${c.type||'text'}" placeholder="${esc(c.placeholder||c.help||'')}" autocomplete="off">
      ${c.help?`<div style="font-size:9px;color:var(--fg3);margin-top:2px">${esc(c.help)}</div>`:''}
    </div>`).join('')}
    <div class="modal-actions">
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
      <button class="modal-btn-ok" id="_svcSaveBtn">
        ✓ Connect ${esc(svc.name)}
      </button>
    </div>
  </div>`;
  // Bind buttons with closures
  const credKeys=svc.credentials.map(c=>c.key);
  const svcId=svc.id;
  setTimeout(()=>{
    const saveBtn=$('_svcSaveBtn');
    if(saveBtn) saveBtn.addEventListener('click',()=>saveServiceCreds(svcId,credKeys));
    const first=overlay.querySelector('.modal-input');
    if(first)first.focus();
  },50);
}

async function saveServiceCreds(svcId,keys){
  const body={};let missing=false;
  for(const key of keys){
    const el=$('_svc_'+key.replace(/[^a-zA-Z0-9_-]/g,''));
    const val=(el?.value||'').trim();
    if(!val){missing=true;if(el){el.style.borderColor='var(--red)';el.focus();}return;}
    body[key]=val;
  }
  const r=await(await fetch(HUB+'/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  document.querySelector('.modal-overlay')?.remove();
  if(r.status==='ok'){notify('🔑 Connected! Saved: '+Object.keys(body).join(', '));renderPanel();}
  else notify('Error: '+(r.message||'failed'));
}

// ══════════════════════════════════
//  ACTIONS
// ══════════════════════════════════
async function removeAgent(n){
  const overlay=document.createElement('div');overlay.className='modal-overlay';
  overlay.addEventListener('click',e=>{if(e.target===overlay)overlay.remove();});
  const tasks=(data.tasks||[]).filter(t=>t.assigned_to===n&&['created','assigned','in_progress'].includes(t.status));
  overlay.innerHTML=`<div class="modal-box" style="width:360px;text-align:center">
    <div style="font-size:28px;margin-bottom:8px">⚠️</div>
    <h3 style="margin-bottom:8px">Remove "${esc(n)}"?</h3>
    ${tasks.length?`<p style="font-size:12px;color:var(--yellow);margin-bottom:8px">${tasks.length} active task(s) will be unassigned</p>`:''}
    <div class="modal-actions" style="justify-content:center">
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
      <button class="modal-btn-ok" style="background:var(--red)" onclick="fetch(HUB+'/agents/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'${escAttr(n)}'})}).then(()=>{this.closest('.modal-overlay').remove();toast('Removed '+${JSON.stringify(n)},'info');poll();})">Remove</button>
    </div></div>`;
  document.body.appendChild(overlay);
}

async function stopAgent(n){
  if(!confirm(`Stop agent "${n}"? This will cancel any active task.`))return;
  try{
    const r=await(await fetch(HUB+'/agents/'+n+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    if(r.status==='ok'){notify('⛔ Stop signal sent to '+n);}
    else{notify('Error stopping '+n);}
  }catch{notify('Failed to stop '+n);}
  setTimeout(poll,500);
}

function _cmdContext() {
  if (!sel) { toast('Select an agent first', 'warn'); return; }
  const a = (data.agents || {})[sel] || {};
  const u = (data.usage || {})[sel] || {};
  const tasks_list = (data.tasks || []).filter(t => t.assigned_to === sel);
  const activeTasks = tasks_list.filter(t => t.status === 'in_progress');
  const doneTasks = tasks_list.filter(t => t.status === 'done');
  const overlay = document.createElement('div'); overlay.className = 'modal-overlay';
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  overlay.innerHTML = `<div class="modal-box" style="width:480px"><h3 style="margin-bottom:12px;font-size:14px">Context: ${esc(sel)}</h3>
    <div style="display:grid;grid-template-columns:120px 1fr;gap:6px;font-size:12px">
      <span style="color:var(--fg3)">Status</span><span>${esc(a.pipeline || 'unknown')}</span>
      <span style="color:var(--fg3)">Calls</span><span>${a.calls || 0}</span>
      <span style="color:var(--fg3)">Tokens (in/out)</span><span>${formatTokens(u.tokens_in || 0)} / ${formatTokens(u.tokens_out || 0)}</span>
      <span style="color:var(--fg3)">Cost</span><span>${formatCost(a.cost || 0)}</span>
      <span style="color:var(--fg3)">Active tasks</span><span>${activeTasks.length ? activeTasks.map(t => '#' + t.id).join(', ') : 'none'}</span>
      <span style="color:var(--fg3)">Completed</span><span>${doneTasks.length} tasks</span>
      <span style="color:var(--fg3)">Expertise</span><span>${a.expertise || 'general'}</span>
      <span style="color:var(--fg3)">Silent</span><span>${a.silent_sec ? formatAgo(a.silent_sec) : 'active'}</span>
    </div>
    <div class="modal-actions"><button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Close</button></div></div>`;
  document.body.appendChild(overlay);
}

async function _runCheck(type, args) {
  let tid = (args || '').trim().replace('#', '');
  if (!tid) {
    const tasks_arr = data.tasks || [];
    const recent = tasks_arr.filter(t => ['done', 'in_progress'].includes(t.status))
                           .sort((a, b) => (b.id - a.id))[0];
    if (!recent) { toast('No recent task found. Usage: /dev-check #123', 'warn'); return; }
    tid = recent.id;
  }
  const task = (data.tasks || []).find(t => t.id == tid);
  if (!task) { toast(`Task #${tid} not found`, 'error'); return; }
  const overlay = document.createElement('div'); overlay.className = 'modal-overlay';
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  overlay.innerHTML = `<div class="modal-box" style="width:560px">
    <h3>${type === 'dev' ? '🔍 Dev Check' : '🧪 QA Check'} — Task #${tid}</h3>
    <div style="margin:10px 0;font-size:12px;color:var(--fg2)">${esc((task.description || '').substring(0, 200))}</div>
    <div style="margin:10px 0;font-size:11px">
      Agent: <strong>${esc(task.assigned_to || '?')}</strong> ·
      Status: <strong>${esc(task.status)}</strong>
      ${task.branch ? ' · Branch: <code>' + esc(task.branch) + '</code>' : ''}
    </div>
    <div class="modal-actions">
      <button class="modal-btn-ok" onclick="startCheck('${type}', ${tid}); this.closest('.modal-overlay').remove()">
        ▶ Run ${type === 'dev' ? 'Dev' : 'QA'} Check
      </button>
      <button class="modal-btn-cancel" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
}

async function startCheck(type, tid) {
  toast(`Starting ${type} check on task #${tid}...`, 'info', 3000);
  const names = data.agent_names || [];
  const target = type === 'qa'
    ? (names.find(n => n === 'qa') || names[0])
    : (names.find(n => n !== 'architect' && n !== 'qa') || names[0]);
  try {
    await fetch(HUB + `/tasks/${tid}/check`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ check_type: type, agent: target })
    });
    toast(`${type} check assigned to ${target}`, 'success', 3000);
  } catch { toast('Failed to start check', 'error'); }
}

function _updateSlashHint(text) {
  let hint = $('slashHint');
  if (!text || !text.startsWith('/')) {
    if (hint) hint.style.display = 'none';
    return;
  }
  // Hide route hint when typing slash commands
  const rh = $('routeHint');
  if (rh) rh.style.display = 'none';

  const partial = text.split(/\s/)[0].toLowerCase();
  const matches = Object.entries(_slashCommands).filter(([k]) => k.startsWith(partial));
  if (!matches.length || (matches.length === 1 && matches[0][0] === partial && !text.includes(' '))) {
    // Exact match or no matches — hide hint
    if (hint) hint.style.display = 'none';
    return;
  }

  if (!hint) {
    hint = document.createElement('div');
    hint.id = 'slashHint';
    const cmdBar = document.querySelector('.cmd-bar');
    if (cmdBar) cmdBar.parentElement.insertBefore(hint, cmdBar);
    else return;
  }
  hint.style.display = 'block';
  hint.innerHTML = matches.slice(0, 8).map(([k, v]) =>
    `<div class="slash-hint-item" onmousedown="event.preventDefault();$('cmdInput').value='${k} ';$('cmdInput').focus();_updateSlashHint('${k} ');">
      <span class="slash-cmd">${esc(k)}</span>${v.args ? `<span class="slash-args">${esc(v.args)}</span>` : ''}
      <span class="slash-desc">${esc(v.desc)}</span></div>`
  ).join('');
}

// ── Expanded Editor (${_mod}+O) ──
function openEditor() {
  const ci = $('cmdInput');
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'editorOverlay';
  overlay.innerHTML = `<div class="editor-modal-box">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <span style="font-size:13px;font-weight:600">Expanded Editor</span>
      <span style="font-size:11px;color:var(--fg3)">${_mod}+Enter to send · Esc to cancel</span>
    </div>
    <textarea id="editorArea" placeholder="Type your prompt...">${ci ? esc(ci.value) : ''}</textarea>
    <div class="editor-footer">
      <span id="editorCharCount">0 chars</span>
      <div style="display:flex;gap:8px">
        <button class="modal-btn-cancel" onclick="closeEditor(true)">← Back (keep text)</button>
        <button class="modal-btn-ok" onclick="closeEditor(true, true)">Send ▶</button>
      </div>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  const ea = $('editorArea');
  ea.focus();
  ea.setSelectionRange(ea.value.length, ea.value.length);
  const cc = $('editorCharCount');
  ea.addEventListener('input', () => { cc.textContent = ea.value.length + ' chars'; });
  cc.textContent = ea.value.length + ' chars';
  ea.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); closeEditor(true, true); }
    if (e.key === 'Escape') { e.preventDefault(); closeEditor(true); }
  });
  overlay.addEventListener('click', e => { if (e.target === overlay) closeEditor(true); });
}

function closeEditor(apply, send) {
  const ea = $('editorArea');
  const ci = $('cmdInput');
  if (apply && ea && ci) {
    ci.value = ea.value;
    autoResize(ci);
  }
  const ov = $('editorOverlay');
  if (ov) ov.remove();
  if (send) sendCmd();
  else if (ci) ci.focus();
}

// ── Help ──
function showHelp(){
  const cmdRows=Object.entries(_slashCommands).map(([k,v])=>
    `<tr><td style="white-space:nowrap;font-weight:600;color:var(--accent);padding:2px 12px 2px 0">${esc(k)}${v.args?' '+esc(v.args):''}</td><td style="color:var(--fg2);padding:2px 0">${esc(v.desc)}</td></tr>`
  ).join('');
  const shortcuts=[
    [`${_mod}+C`,'Stop selected agent'],
    [`${_mod}+O`,'Open expanded editor'],
    [`${_mod}+K`,'Focus command input'],
    [`${_mod}+Enter`,'Send (in expanded editor)'],
    ['1-9','Select agent by number'],
    ['Esc','Close modal / editor'],
    ['?','Show this help'],
  ];
  const keyRows=shortcuts.map(([k,d])=>
    `<tr><td style="white-space:nowrap;padding:2px 12px 2px 0"><kbd style="background:var(--bg1);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:11px">${k}</kbd></td><td style="color:var(--fg2);padding:2px 0">${d}</td></tr>`
  ).join('');
  const overlay=document.createElement('div');
  overlay.className='modal-overlay';
  overlay.innerHTML=`<div class="modal-box" style="min-width:340px;max-width:480px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <span style="font-size:14px;font-weight:600">Help</span>
      <button onclick="this.closest('.modal-overlay').remove()" style="background:none;border:none;color:var(--fg3);cursor:pointer;font-size:16px">×</button>
    </div>
    <div style="font-size:11px;font-weight:600;color:var(--fg3);text-transform:uppercase;margin-bottom:6px">Keyboard Shortcuts</div>
    <table style="font-size:12px;margin-bottom:14px;width:100%">${keyRows}</table>
    <div style="font-size:11px;font-weight:600;color:var(--fg3);text-transform:uppercase;margin-bottom:6px">Slash Commands</div>
    <table style="font-size:12px;width:100%">${cmdRows}</table>
  </div>`;
  overlay.addEventListener('click',e=>{if(e.target===overlay)overlay.remove();});
  document.body.appendChild(overlay);
}

// ── Slash Commands ──
const _slashCommands={
  '/help':{desc:'Show available commands',args:''},
  '/status':{desc:'Show all agent statuses',args:''},
  '/tasks':{desc:'Show all tasks',args:''},
  '/model':{desc:'Change agent model',args:'<model-name>'},
  '/stop':{desc:'Stop selected agent',args:'[agent]'},
  '/restart':{desc:'Restart selected agent',args:'[agent]'},
  '/clear':{desc:'Clear log view',args:''},
  '/export':{desc:'Export session (Alt=JSON)',args:'[format]'},
  '/budget':{desc:'Set cost budget',args:'<amount>'},
  '/dev-check':{desc:'Run dev check on task',args:'[#task-id]'},
  '/qa-check':{desc:'Run QA check on task',args:'[#task-id]'},
  '/context':{desc:'Show agent context info',args:''},
  '/theme':{desc:'Toggle light/dark theme',args:''},
  '/connect':{desc:'Connect external service',args:'[service]'},
  '/add-dir':{desc:'Add directory for agents',args:'<path>'},
  '/remove-dir':{desc:'Remove added directory',args:'<path>'},
  '/dirs':{desc:'Show added directories',args:''},
};

async function _addDir(path){
  try{
    const cfg=await(await fetch(HUB+'/config')).json();
    const dirs=cfg.add_dirs||[];
    if(dirs.includes(path)){toast('Already added: '+path,'warn');return;}
    dirs.push(path);
    const r=await(await fetch(HUB+'/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({add_dirs:dirs})})).json();
    if(r.updated&&r.updated.add_dirs!==undefined){toast('Directory added: '+path,'success');}
    else toast('Failed: '+(r.message||'unknown'),'error');
  }catch(e){toast('Error: '+e,'error');}
}
async function _removeDir(path){
  try{
    const cfg=await(await fetch(HUB+'/config')).json();
    const dirs=(cfg.add_dirs||[]).filter(d=>d!==path);
    const r=await(await fetch(HUB+'/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({add_dirs:dirs})})).json();
    if(r.updated&&r.updated.add_dirs!==undefined){toast('Directory removed: '+path,'success');}
    else toast('Not found or failed','warn');
  }catch(e){toast('Error: '+e,'error');}
}
async function _showDirs(){
  try{
    const cfg=await(await fetch(HUB+'/config')).json();
    const dirs=cfg.add_dirs||[];
    if(!dirs.length){toast('No extra directories configured','info');return;}
    toast('Directories: '+dirs.join(', '),'info',5000);
  }catch(e){toast('Error: '+e,'error');}
}
function _handleSlashCommand(text){
  const parts=text.split(/\s+/);
  const cmd=parts[0].toLowerCase();
  const args=parts.slice(1).join(' ');
  switch(cmd){
    case '/help':
      toast('Commands: '+Object.keys(_slashCommands).join(', '),'info',6000);break;
    case '/status': switchTab('logs');break;
    case '/tasks': switchTab('tasks');break;
    case '/clear':
      if(sel){logLines[sel]=[];renderPanel();}break;
    case '/export': exportSession();break;
    case '/stop':
      const stopTarget=args||sel;if(stopTarget)stopAgent(stopTarget);else toast('Select an agent','warn');break;
    case '/restart':
      const restartTarget=args||sel;if(restartTarget)restartAgent(restartTarget);else toast('Select an agent','warn');break;
    case '/context': _cmdContext();break;
    case '/dev-check': _runCheck('dev',args);break;
    case '/qa-check': _runCheck('qa',args);break;
    case '/theme':
      const cur=document.documentElement.getAttribute('data-theme');
      const next=cur==='light'?'dark':'light';
      document.documentElement.setAttribute('data-theme',next);
      localStorage.setItem('ma-theme',next);
      toast('Theme: '+next,'info',2000);break;
    case '/connect': showServiceWizard(args||undefined);break;
    case '/add-dir':
      if(!args){toast('Usage: /add-dir /path/to/directory','warn');break;}
      _addDir(args.trim());break;
    case '/remove-dir':
      if(!args){toast('Usage: /remove-dir /path/to/directory','warn');break;}
      _removeDir(args.trim());break;
    case '/dirs':
      _showDirs();break;
    case '/model':
      if(!sel){toast('Select an agent first','warn');break;}
      if(!args){toast('Usage: /model <model-name>','warn');break;}
      fetch(HUB+'/agents/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel,model:args})}).then(()=>toast(`Model → ${args} for ${sel}`,'success')).catch(()=>toast('Failed','error'));break;
    case '/budget':
      if(!args){toast('Usage: /budget <amount>','warn');break;}
      fetch(HUB+'/budget',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:parseFloat(args)})}).then(()=>toast(`Budget set: $${args}`,'success')).catch(()=>toast('Failed','error'));break;
    default:
      toast('Unknown command: '+cmd,'warn');
  }
}

function _updateMentionHint(text,pos){
  // Show agent mention suggestions when typing @
  const before=text.substring(0,pos);
  const mentionMatch=before.match(/@(\w*)$/);
  let hint=$('mentionHint');
  if(!mentionMatch){if(hint)hint.style.display='none';return;}
  const partial=mentionMatch[1].toLowerCase();
  const names=(data.agent_names||[]).filter(n=>n.toLowerCase().startsWith(partial));
  if(!names.length){if(hint)hint.style.display='none';return;}
  if(!hint){
    hint=document.createElement('div');hint.id='mentionHint';
    const cmdBar=document.querySelector('.cmd-bar');
    if(cmdBar)cmdBar.parentElement.insertBefore(hint,cmdBar);else return;
  }
  hint.style.display='block';
  hint.innerHTML=names.slice(0,6).map(n=>{
    const a=(data.agents||{})[n]||{};const ps=a.pipeline||'offline';
    const dot=ps==='working'?'working':ps==='idle'?'idle':'offline';
    return`<div class="slash-hint-item" onmousedown="event.preventDefault();const ci=$('cmdInput');const v=ci.value;const p=${pos};const before=v.substring(0,p).replace(/@\\w*$/,'@${escAttr(n)} ');ci.value=before+v.substring(p);ci.focus();$('mentionHint').style.display='none';">
      <span class="agent-dot dot-${dot} dot-inline"></span><span>${esc(n)}</span></div>`;
  }).join('');
}

let _sending=false;
async function sendCmd(){
  if(_sending)return;
  const input=$('cmdInput');const text=input.value.trim();if(!text)return;
  _sending=true;
  const btn=$('sendBtn');if(btn){btn.disabled=true;btn.textContent='...';}
  try{
  // Clear input immediately to prevent double-send
  input.value='';autoResize(input);
  // Hide slash hint
  const sh=$('slashHint');if(sh)sh.style.display='none';

  // Handle slash commands
  if(text.startsWith('/')){
    _handleSlashCommand(text);
    return;
  }

  // Detect credential shortcut
  const credMatch=text.match(/^([A-Z][A-Z0-9_]{2,50})=(\S+)$/);
  if(credMatch){
    const body={};body[credMatch[1]]=credMatch[2];
    const r=await(await fetch(HUB+'/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.status==='ok'){notify('🔑 Saved: '+credMatch[1]);_sending=false;if(btn){btn.disabled=false;btn.textContent='Send';}return;}
  }

  // Determine target: @mention > sidebar selection > /route auto-detect
  let target='architect', intent='task';
  const atMatch=text.match(/^@(\w+)\s/);
  if(atMatch && (data.agent_names||[]).includes(atMatch[1])){
    target=atMatch[1];
  } else if(sel && (data.agent_names||[]).includes(sel)){
    target=sel;
  } else {
    try{
      const r=await(await fetch(HUB+'/route?msg='+encodeURIComponent(text))).json();
      if(r.target)target=r.target;
    }catch{}
  }
  // AI intent classification (with heuristic fallback)
  try{
    const ic=await(await fetch(HUB+'/classify-intent?msg='+encodeURIComponent(text))).json();
    if(ic.intent)intent=ic.intent;
  }catch{
    try{
      const r=await(await fetch(HUB+'/route?msg='+encodeURIComponent(text))).json();
      if(r.intent)intent=r.intent;
    }catch{}
  }

  if(intent==='task'){
    const taskPayload={
      description:text,
      assigned_to:target,
      status:'created',
      priority:5,
      created_by:'user',
    };
    const taskResult=await(await fetch(HUB+'/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(taskPayload)})).json();
    const taskId=taskResult?.id;
    // Extract ticket ID from URLs (Jira, Linear, GitHub, Sentry) — no branch if no real ticket
    let taskExternalId='';
    const jiraM=text.match(/atlassian\.net\/browse\/([A-Z]{2,10}-\d+)/);
    const linearM=!jiraM&&text.match(/linear\.app\/[^/]+\/issue\/([A-Z]+-\d+)/);
    const ghM=!jiraM&&!linearM&&text.match(/github\.com\/[^/]+\/[^/]+\/(?:issues|pull)\/(\d+)/);
    const sentryM=!jiraM&&!linearM&&!ghM&&text.match(/sentry\.io\/issues\/(\d+)/);
    const genericJiraM=!jiraM&&!linearM&&!ghM&&!sentryM&&text.match(/\b([A-Z]{2,10}-\d{1,6})\b/);
    if(jiraM)taskExternalId=jiraM[1];
    else if(linearM)taskExternalId=linearM[1];
    else if(ghM)taskExternalId='GH-'+ghM[1];
    else if(sentryM)taskExternalId='SENTRY-'+sentryM[1];
    else if(genericJiraM)taskExternalId=genericJiraM[1];

    // Update task with external ID only if a real ticket was found
    if(taskId&&taskExternalId) fetch(HUB+'/tasks/'+taskId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_external_id:taskExternalId})});

    const msgPayload={
      sender:'user',receiver:target,content:text,msg_type:'task',
      task_external_id:taskExternalId
    };
    if(taskId) msgPayload.task_id=String(taskId);
    await fetch(HUB+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(msgPayload)});

    const queueInfo=taskId?` (#${taskId})`:'';
    toast(`Task sent to ${target}${queueInfo}: ${text.substring(0,60)}`, 'success', 3000);
    notify(`📋 → ${target}: ${text.substring(0,50)}${queueInfo}`);
  } else {
    // Chat/question — just send message, no task creation
    const msgPayload={
      sender:'user',receiver:target,content:text,msg_type:'chat'
    };
    await fetch(HUB+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(msgPayload)});
    toast(`Chat sent to ${target}`, 'info', 2000);
    notify(`💬 → ${target}: ${text.substring(0,50)}`);
  }

  setTimeout(poll,300);
  }finally{_sending=false;const b=$('sendBtn');if(b){b.disabled=false;b.textContent='Send';}}
}

function autoResize(el){
  if(!el)return;
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,160)+'px';
}

let routeTimer=null;
function updateRouteHint(text){
  const hint=$('routeHint');if(!hint||!text){if(hint)hint.style.display='none';return;}
  clearTimeout(routeTimer);
  routeTimer=setTimeout(async()=>{try{
    // Determine target: @mention > sidebar > /route
    let target=null;
    const atMatch=text.match(/^@(\w+)\s/);
    if(atMatch && (data.agent_names||[]).includes(atMatch[1])){
      target=atMatch[1];
    } else if(sel && (data.agent_names||[]).includes(sel)){
      target=sel;
    }
    if(!target){
      try{const r=await(await fetch(HUB+'/route?msg='+encodeURIComponent(text))).json();target=r.target||null;}catch{}
    }
    // AI intent classification
    let intent='task';
    try{const ic=await(await fetch(HUB+'/classify-intent?msg='+encodeURIComponent(text))).json();if(ic.intent)intent=ic.intent;}catch{}
    if(target){
      const icon=intent==='chat'?'💬':'📋';
      const label=intent==='chat'?'chat':'task';
      hint.innerHTML=`${icon} ${label} → ${target}`;
      hint.style.display='inline-block';
      hint.style.background=intent==='chat'?'var(--cyan)':'var(--ac)';
    } else hint.style.display='none';
  }catch{hint.style.display='none';}},600);
}

async function shutdownSystem(){
  if(!confirm('Shut down the entire multi-agent system? (hub + all agents)'))return;
  try{
    toast('Shutting down...','info',3000);
    await fetch(HUB+'/shutdown',{method:'POST'});
    setTimeout(()=>{document.body.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--fg3);font-size:14px;font-family:monospace">System shut down. Close this tab.</div>';},1000);
  }catch(e){toast('Shutdown signal sent','info',3000);}
}
async function exportSession(){try{
  const fmt=event?.altKey?'json':'md';
  const r=await fetch(HUB+'/export?fmt='+fmt);
  if(fmt==='json'){
    const j=await r.json();const t=JSON.stringify(j,null,2);
    const b=new Blob([t],{type:'application/json'}),u=URL.createObjectURL(b),a=document.createElement('a');
    a.href=u;a.download='session.json';a.click();URL.revokeObjectURL(u);
  } else {
    const t=await r.text();
    const b=new Blob([t],{type:'text/markdown'}),u=URL.createObjectURL(b),a=document.createElement('a');
    a.href=u;a.download='session.md';a.click();URL.revokeObjectURL(u);
  }
}catch{toast('Export failed','error');}}

async function fetchChanges(){try{
  const c=await(await fetch(HUB+'/changes')).json();
  if(Array.isArray(c)){reviewData=[...c].sort((a,b)=>(b.id||0)-(a.id||0));if(tab==='review')renderPanel();updateBadges();}
}catch{}}

async function reviewChange(id,s){await fetch(HUB+'/changes/'+id+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:s})});setTimeout(()=>{fetchChanges();poll();},300);}

async function commitChanges(project,inputId){
  const input=$(inputId);const msg=(input?.value||'').trim();
  if(!msg){toast('Commit message required','warn');return;}
  try{
    const r=await(await fetch(HUB+'/git/commit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project,message:msg})})).json();
    if(r.status==='ok'){toast(`✅ Committed ${r.hash} to ${r.branch}`,'success',4000);fetchChanges();fetchInbox();poll();}
    else toast('❌ '+r.message,'error',5000);
  }catch(e){toast('Commit error: '+e,'error');}
}
async function commitAndPush(project,inputId){
  const input=$(inputId);const msg=(input?.value||'').trim();
  if(!msg){toast('Commit message required','warn');return;}
  try{
    const r=await(await fetch(HUB+'/git/commit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project,message:msg})})).json();
    if(r.status!=='ok'){toast('❌ '+r.message,'error',5000);return;}
    toast(`✅ Committed ${r.hash}`,'success',2000);
    const p=await(await fetch(HUB+'/git/push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project})})).json();
    if(p.status==='ok'){toast(`🚀 Pushed to origin/${p.branch}`,'success',4000);}
    else toast('⚠️ Push failed: '+p.message,'warn',5000);
    fetchChanges();fetchInbox();poll();
  }catch(e){toast('Error: '+e,'error');}
}
async function discardChanges(project){
  if(!confirm(`Discard ALL uncommitted changes in "${project}"?`))return;
  try{
    const r=await(await fetch(HUB+'/git/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project,mode:'discard'})})).json();
    if(r.status==='ok'){toast('↩ Changes discarded: '+project,'info',3000);fetchChanges();fetchInbox();poll();}
    else toast('Error: '+r.message,'error');
  }catch(e){toast('Error: '+e,'error');}
}

// ── Plan Proposal Actions ──
function togglePlanStep(cb){
  const step=cb.closest('.plan-step');
  if(step) step.classList.toggle('plan-step-unchecked',!cb.checked);
}
function toggleAllPlanSteps(masterCb,planId){
  const bubble=masterCb.closest('.plan-proposal-bubble');
  if(!bubble)return;
  bubble.querySelectorAll(`input[data-plan="${planId}"]`).forEach(cb=>{
    cb.checked=masterCb.checked;
    togglePlanStep(cb);
  });
}
function _markPlanDone(bubble, label, color){
  const actions=bubble.querySelector('.plan-actions');
  if(actions) actions.innerHTML=`<span style="font-size:11px;font-weight:600;color:${color}">${label}</span>`;
  bubble.querySelectorAll('.plan-step-check input').forEach(cb=>{cb.disabled=true;});
  bubble.querySelector('.plan-select-all')?.remove();
  bubble.style.opacity='0.6';
}
async function approvePlan(planId){
  const bubble=document.querySelector(`.plan-proposal-bubble input[data-plan="${planId}"]`)?.closest('.plan-proposal-bubble');
  if(!bubble){toast('Plan bubble not found','error');return;}
  const selected=[];
  bubble.querySelectorAll(`input[data-plan="${planId}"]:checked`).forEach(cb=>selected.push(Number(cb.dataset.idx)));
  if(!selected.length){toast('Select at least one step','warn');return;}
  bubble.querySelectorAll('.plan-btn').forEach(b=>b.disabled=true);
  try{
    const r=await(await fetch(HUB+'/plan/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan_id:planId,selected_steps:selected})})).json();
    if(r.status==='ok'){
      toast(`Plan approved — ${r.tasks_created.length} task(s) created`,'success',4000);
      _markPlanDone(bubble, `Approved — ${r.tasks_created.length} task(s) created`, 'var(--green)');
      setTimeout(()=>{poll();fetchInbox();},500);
    } else {
      toast('Error: '+(r.message||'unknown'),'error');
      bubble.querySelectorAll('.plan-btn').forEach(b=>b.disabled=false);
    }
  }catch(e){toast('Error: '+e,'error');bubble.querySelectorAll('.plan-btn').forEach(b=>b.disabled=false);}
}
async function dismissPlan(planId){
  if(!confirm('Dismiss this plan?'))return;
  try{
    const r=await(await fetch(HUB+'/plan/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan_id:planId})})).json();
    if(r.status==='ok'){
      toast('Plan dismissed','info',3000);
      const bubble=document.querySelector(`.plan-proposal-bubble input[data-plan="${planId}"]`)?.closest('.plan-proposal-bubble');
      if(bubble) _markPlanDone(bubble, 'Dismissed', 'var(--fg3)');
      setTimeout(fetchInbox,500);
    } else toast('Error: '+(r.message||'unknown'),'error');
  }catch(e){toast('Error: '+e,'error');}
}

// ══════════════════════════════════
//  INIT
// ══════════════════════════════════
const saved=localStorage.getItem('ma-theme');
if(saved)document.documentElement.setAttribute('data-theme',saved);

// WebSocket-first: single connection handles everything
connectWebSocket();

const ci=$('cmdInput');
if(ci){
  ci.addEventListener('input',e=>{updateRouteHint(e.target.value);_updateSlashHint(e.target.value);_updateMentionHint(e.target.value,e.target.selectionStart);});
  ci.addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey&&!e.ctrlKey&&!e.metaKey){e.preventDefault();sendCmd();}
  });
  setTimeout(()=>ci.focus(),100);
}

// ── Global keyboard shortcuts ──
document.addEventListener('keydown',e=>{
  const inInput=document.activeElement&&(document.activeElement.tagName==='INPUT'||document.activeElement.tagName==='TEXTAREA'||document.activeElement.tagName==='SELECT');
  // Cmd+C / Ctrl+C (no text selected) → stop selected agent
  if(e.key==='c'&&(e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey){
    const selection=window.getSelection().toString();
    if(!selection&&sel){e.preventDefault();stopAgent(sel);}
  }
  // Ctrl/Cmd+O → Expanded editor
  if(e.key==='o'&&(e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey){e.preventDefault();openEditor();}
  // Ctrl/Cmd+K → Focus command input
  if(e.key==='k'&&(e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey){e.preventDefault();const ci=$('cmdInput');if(ci)ci.focus();}
  // Number keys 1-9 → Select agent (only when not in input)
  if(!inInput&&e.key>='1'&&e.key<='9'&&!e.metaKey&&!e.ctrlKey&&!e.altKey){
    const names=(data.agent_names||[]).filter(n=>!((data.agents||{})[n]||{}).hidden);
    const idx=parseInt(e.key)-1;
    if(idx<names.length){e.preventDefault();selectAgent(names[idx]);}
  }
  // ? → Show help (only when not in input)
  if(!inInput&&e.key==='?'&&!e.metaKey&&!e.ctrlKey&&!e.altKey){e.preventDefault();showHelp();}
  // Esc → Close modal
  if(e.key==='Escape'){const modal=document.querySelector('.modal-overlay');if(modal)modal.remove();}
});
