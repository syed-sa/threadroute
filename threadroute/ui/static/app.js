const $ = s => document.querySelector(s);
// Access token: passed once in the URL fragment (never sent to the server or logged), kept for this tab only.
(function () {
  const m = location.hash.match(/token=([^&]+)/);
  try {
    if (m) { sessionStorage.setItem('tr_token', decodeURIComponent(m[1])); history.replaceState(null, '', location.pathname); }
  } catch (e) { /* storage unavailable: token stays in memory only */ window.__tr_token = m ? decodeURIComponent(m[1]) : ''; }
})();
function authHeaders() {
  let t = window.__tr_token || '';
  try { t = sessionStorage.getItem('tr_token') || t; } catch (e) { /* ignore */ }
  return t ? { Authorization: 'Bearer ' + t } : {};
}
const sessionId = 'web-' + Math.random().toString(36).slice(2, 10);
let attachedFile = null;
let activeTab = 'approvals';

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v; else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) n.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ''));
  return n;
}
function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function md(text) {  // tiny, safe markdown: **bold**, `code`, bullets
  const lines = esc(text).split('\n'); let html = '', list = false;
  for (const l of lines) {
    const m = l.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (m) { if (!list) { html += '<ul>'; list = true; } html += '<li>' + inline(m[1]) + '</li>'; }
    else { if (list) { html += '</ul>'; list = false; } html += inline(l) + '\n'; }
  }
  return html + (list ? '</ul>' : '');
}
function inline(s) { return s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`(.+?)`/g, '<code>$1</code>'); }
async function api(path, opts = {}) {
  opts.headers = { ...(opts.headers || {}), ...authHeaders() };
  const r = await fetch(path, opts);
  if (r.status === 401) throw new Error('Not authorised - open the URL printed by the server (it contains #token=...).');
  if (r.status === 429) throw new Error('Too many requests - please wait a moment.');
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
const jsonOpts = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

// ---------------------------------------------------------------- chat
function addMsg(kind, html, extra) {
  const m = el('div', { class: 'msg ' + kind });
  const body = el('div'); body.innerHTML = html;
  m.append(body); if (extra) m.append(extra);
  $('#log').append(m); $('#log').scrollTop = 1e9; return m;
}
function botExtras(res) {
  const wrap = el('div');
  if (res.trace && res.trace.length) {
    const calls = res.trace.filter(t => t.type !== 'tool_result');
    wrap.append(el('details', { class: 'trace' }, el('summary', {}, `Agents and tools used (${calls.length})`),
      ...calls.map(t => el('div', {}, el('code', {}, `[${t.agent}] ${t.name} ${t.args || ''}`)))));
  }
  (res.notices || []).forEach(n => wrap.append(el('div', { class: 'notice' }, 'Privacy: ' + n)));
  if (res.verification) {
    const v = res.verification;
    wrap.append(el('div', { class: 'notice ' + (v.grounded ? 'badge-ok' : 'badge-bad') },
      v.grounded ? 'Checked against tool data: no unsupported claims' + (v.retried ? ' (answer was corrected)' : '')
                 : 'Could not fully verify: ' + (v.issues || []).slice(0, 2).join('; ')));
  }
  (res.degraded || []).slice(-2).forEach(d => wrap.append(el('div', { class: 'degraded' }, 'Fallback data: ' + d)));
  const fb = el('div', { class: 'fb' }, el('span', { class: 'muted' }, 'Was this helpful?'));
  const up = el('button', { class: 'small', onclick: async () => {
    await api('/api/feedback', jsonOpts('POST', { session_id: sessionId, message_id: res.message_id, rating: 1, agent: res.agent }));
    fb.replaceChildren(el('span', { class: 'muted' }, 'Thanks - recorded.')); } }, 'Yes');
  const down = el('button', { class: 'small', onclick: () => {
    const box = el('input', { class: 'cmt grow', placeholder: 'What should it do differently? (becomes a rule)' });
    const go = el('button', { class: 'small primary', onclick: async () => {
      const r = await api('/api/feedback', jsonOpts('POST', { session_id: sessionId, message_id: res.message_id, rating: -1, comment: box.value, agent: res.agent }));
      fb.replaceChildren(el('span', { class: 'muted' }, r.learned && r.learned.created ? 'Thanks - learned a new rule.' : 'Thanks - recorded.')); refresh(); } }, 'Send');
    fb.replaceChildren(box, go); box.focus(); } }, 'No');
  fb.append(up, down); wrap.append(fb); return wrap;
}
// Streams progress (which agent / tool is running) while the answer is prepared; the final answer arrives only after
// the guardrails and grounding check. Falls back to the plain endpoint if streaming is unavailable.
async function chatStream(form, onStep) {
  const r = await fetch('/api/chat/stream', { method: 'POST', body: form, headers: authHeaders() });
  if (r.status === 401) throw new Error('Not authorised - open the URL printed by the server (it contains #token=...).');
  if (r.status === 429) throw new Error('Too many requests - please wait a moment.');
  if (!r.ok || !r.body) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || r.statusText); }
  const reader = r.body.getReader(), dec = new TextDecoder(); let buf = '', final = null;
  for (;;) {
    const { done, value } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2);
      const ev = (block.match(/^event: (.*)$/m) || [])[1], data = (block.match(/^data: (.*)$/m) || [])[1];
      if (!ev || !data) continue;
      const obj = JSON.parse(data);
      if (ev === 'step') onStep(obj); else if (ev === 'final') final = obj; else if (ev === 'error') throw new Error(obj.detail);
    }
  }
  if (!final) throw new Error('The connection closed before an answer arrived.');
  return final;
}
async function send(text) {
  text = (text ?? $('#text').value).trim();
  if (!text && !attachedFile) return;
  const userHtml = (attachedFile ? `<img src="${URL.createObjectURL(attachedFile)}">` : '') + esc(text || '(photo)');
  addMsg('user', userHtml);
  const form = new FormData(); form.append('message', text); form.append('session_id', sessionId);
  if (attachedFile) form.append('image', attachedFile);
  $('#text').value = ''; setAttached(null);
  $('#send').disabled = true;
  const thinking = addMsg('bot', '<span class="muted">Working... (agents are calling tools)</span>');
  try {
    const label = thinking.querySelector('span');
    const res = await chatStream(form, st => {
      if (label) label.textContent = st.type === 'transfer' ? 'Handing over to ' + (st.name === 'transfer_to_agent' ? 'a specialist agent' : st.name) + '...'
        : 'Working: ' + (st.agent || 'agent') + ' is using ' + st.name + '...';
    });
    thinking.remove();
    const m = addMsg('bot', `<div class="who">${esc(res.agent)}</div>` + md(res.reply), botExtras(res));
    if (res.new_actions.length) activate('approvals');
  } catch (e) { thinking.remove(); addMsg('bot', '<b>Error:</b> ' + esc(e.message)); }
  $('#send').disabled = false; refresh();
}
function setAttached(file) {
  attachedFile = file; const a = $('#attached');
  a.style.display = file ? 'block' : 'none'; a.textContent = file ? 'Attached: ' + file.name : '';
}
$('#attach').onclick = () => $('#file').click();
$('#file').onchange = e => setAttached(e.target.files[0] || null);
$('#composer').onsubmit = e => { e.preventDefault(); send(); };
$('#text').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

const QUICK = [
  ['Why is SH-1001 late?', 'Why is shipment SH-1001 late and what should we do about it?'],
  ['Bengaluru out of Oxford L', 'Bengaluru Indiranagar is out of white Oxford shirts in size L. Fix it.'],
  ['Delayed shipments', 'List all delayed or held shipments and which stores they affect.'],
  ['Carrier feed down?', 'Is anything wrong with our carrier or weather data feeds right now?'],
];
const SAMPLES = [
  ['Photo: damaged cartons', 'damaged_carton_SH-1002.png', 'These cartons just arrived at Delhi CP. What do we do?'],
  ['Photo: packing list', 'packing_list_SH-1003.png', 'Check this packing list against what we expected for this shipment.'],
  ['Photo: returned jacket', 'returned_jacket_RT-9001.png', 'Grade this returned jacket.'],
];
QUICK.forEach(([label, text]) => $('#chips').append(el('button', { class: 'chip', onclick: () => send(text) }, label)));
SAMPLES.forEach(([label, file, text]) => $('#chips').append(el('button', { class: 'chip', onclick: async () => {
  const blob = await (await fetch('/samples/' + file, { headers: authHeaders() })).blob();
  setAttached(new File([blob], file, { type: 'image/png' })); $('#text').value = text; $('#text').focus(); } }, label)));

// ---------------------------------------------------------------- panels
function activate(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'p-' + tab));
  refresh();
}
document.querySelectorAll('.tab').forEach(t => t.onclick = () => activate(t.dataset.tab));

const EDITABLE = new Set(['qty', 'to_location', 'from_location', 'new_mode', 'new_carrier', 'grade', 'disposition', 'severity', 'affected_units', 'to', 'subject']);
function actionCard(a) {
  const card = el('div', { class: 'act' },
    el('div', { class: 'kind' }, a.kind.replace(/_/g, ' ') + ' - ' + a.id + ' - by ' + a.agent),
    el('h4', {}, a.summary), el('span', { class: 'status ' + a.status }, a.status.toUpperCase()));
  if (a.status === 'pending') {
    const kv = el('div', { class: 'kv' }); const inputs = {};
    for (const [k, v] of Object.entries(a.payload)) {
      if (v === null || v === '') continue;
      kv.append(el('label', {}, k));
      if (EDITABLE.has(k)) { inputs[k] = el('input', { value: v }); kv.append(inputs[k]); }
      else kv.append(el('div', {}, String(v)));
    }
    card.append(kv);
    if (a.guidance_ids.length) card.append(el('div', { class: 'muted' }, 'Learned rules applied: ' + a.guidance_ids.join(', ')));
    const cmt = el('input', { class: 'cmt', placeholder: 'Comment / reason (rejections teach the agents)' });
    const approve = el('button', { class: 'primary small', onclick: async () => {
      const edits = {}; for (const [k, i] of Object.entries(inputs)) { if (i.value !== String(a.payload[k])) edits[k] = isNaN(i.value) || i.value === '' ? i.value : Number(i.value); }
      try { await api(`/api/actions/${a.id}/approve`, jsonOpts('POST', { comment: cmt.value || null, edits: Object.keys(edits).length ? edits : null })); }
      catch (e) { alert(e.message); } refresh(); } }, 'Approve');
    const reject = el('button', { class: 'danger small', onclick: async () => {
      if (!cmt.value.trim()) { cmt.focus(); cmt.placeholder = 'Please give a reason - it teaches the agents'; return; }
      await api(`/api/actions/${a.id}/reject`, jsonOpts('POST', { reason: cmt.value })); refresh(); } }, 'Reject');
    card.append(cmt, el('div', { class: 'row' }, approve, reject));
  } else {
    if (a.decision_comment) card.append(el('div', { class: 'muted' }, 'Comment: ' + a.decision_comment));
    if (a.result && a.result.steps) card.append(el('ul', { class: 'steps' }, ...a.result.steps.map(s => el('li', {}, s.step + ': ' + (s.ok ? 'ok' : 'FAILED')))));
    if (a.result && a.result.status === 'failed' && a.result.result.error) card.append(el('div', { class: 'degraded' }, a.result.result.error.message));
  }
  return card;
}
async function loadApprovals() {
  const [pending, recent] = await Promise.all([api('/api/actions?status=pending'), api('/api/actions?session_id=' + sessionId)]);
  $('#badge').textContent = pending.length ? `(${pending.length})` : '';
  const seen = new Set(); const items = [];
  [...pending, ...recent].forEach(a => { if (!seen.has(a.id)) { seen.add(a.id); items.push(a); } });
  const p = $('#p-approvals');
  if (document.activeElement && p.contains(document.activeElement)) return;  // don't wipe what the user is typing
  p.replaceChildren(el('div', { class: 'muted' }, 'Agents only propose. Nothing changes until you approve.'),
    ...(items.length ? items.map(actionCard) : [el('p', { class: 'muted' }, 'No proposals yet - try "Bengaluru out of Oxford L".')]));
}
async function loadHealth() {
  const h = await api('/api/health'); const p = $('#p-health');
  if (document.activeElement && p.contains(document.activeElement)) return;
  const rows = Object.entries(h.targets).map(([t, v]) => el('tr', {}, el('td', {}, t), el('td', {}, v.calls), el('td', {}, v.failures),
    el('td', {}, v.breaker), el('td', {}, v.avg_latency_ms + ' ms')));
  const target = el('select', {}, ...['carrier:primary_api', 'carrier:secondary_api', 'maps:route_model', 'weather:seasonal_model', 'email:outbox_file', 'mcp:mcp_stdio'].map(t => el('option', {}, t)));
  const mode = el('select', {}, ...['fail', 'timeout', 'malformed', 'flaky'].map(t => el('option', {}, t)));
  p.replaceChildren(
    el('h3', {}, 'Providers (last 60 min)'),
    rows.length ? el('table', {}, el('tr', {}, ...['provider', 'calls', 'fails', 'breaker', 'latency'].map(x => el('th', {}, x))), ...rows) : el('p', { class: 'muted' }, 'No external calls yet.'),
    el('h3', {}, 'Failure drill'),
    el('div', { class: 'row' }, target, mode, el('button', { class: 'small', onclick: async () => { await api('/api/chaos', jsonOpts('POST', { target: target.value, mode: mode.value })); refresh(); } }, 'Inject'),
      el('button', { class: 'small', onclick: async () => { await api('/api/chaos', { method: 'DELETE' }); refresh(); } }, 'Clear all')),
    el('div', { class: 'muted' }, h.chaos_rules.length ? 'Active: ' + h.chaos_rules.map(r => `${r.target}=${r.mode}`).join(', ') : 'No failures injected. Inject one, then ask "Where is SH-1001?".'),
    el('h3', {}, 'Watchdog'),
    ...(h.recommendations.length ? h.recommendations.map(r => el('div', { class: 'muted' }, `${r.component}/${r.provider}: ${r.issue} -> ${r.action}`)) : [el('div', { class: 'muted' }, 'No unhealthy providers.')]),
    el('div', { class: 'row' }, el('button', { class: 'small', onclick: async () => { await api('/api/watchdog/heal', { method: 'POST' }); refresh(); } }, 'Run watchdog now')),
    el('h3', {}, 'Recent healing events'),
    ...(h.recent_events.length ? h.recent_events.slice(0, 12).map(e => el('div', { class: 'muted' }, `${e.kind} - ${e.component}/${e.provider || '-'} - ${e.detail}`)) : [el('div', { class: 'muted' }, 'None yet.')]));
}
async function loadRules() {
  const g = await api('/api/guidance'); const p = $('#p-rules');
  if (document.activeElement && p.contains(document.activeElement)) return;
  const box = el('input', { placeholder: 'e.g. Never reroute suits by road during monsoon' });
  p.replaceChildren(el('div', { class: 'muted' }, 'Rules come from your rejections, edits and thumbs-down comments. They are injected into the agents\' instructions.'),
    el('div', { class: 'row' }, box, el('button', { class: 'small primary', onclick: async () => { if (box.value.trim()) { await api('/api/teach', jsonOpts('POST', { text: box.value })); refresh(); } } }, 'Teach')),
    ...(g.length ? g.map(r => el('div', { class: 'act' }, el('div', { class: 'kind' }, `#${r.id} - ${r.scenario} - weight ${r.weight.toFixed(1)} - used ${r.uses} - helpful ${r.helpful}/${r.helpful + r.unhelpful} - ${r.active ? 'active' : 'retired'}`),
      el('div', {}, r.text), r.active ? el('button', { class: 'small', onclick: async () => { await api('/api/guidance/' + r.id, { method: 'DELETE' }); refresh(); } }, 'Retire') : ''))
      : [el('p', { class: 'muted' }, 'No rules learned yet.')]));
}
async function loadSecurity() {
  const g = await api('/api/guardrails'); const p = $('#p-security');
  const kinds = Object.entries(g.by_kind || {});
  p.replaceChildren(
    el('div', { class: 'muted' }, 'Personal data and secrets are masked on input, tool results, model output, email, sheets, stored feedback and agent-to-agent messages. Only counts are shown here - never the values.'),
    el('h3', {}, 'Caught so far: ' + g.total),
    kinds.length ? el('table', {}, el('tr', {}, el('th', {}, 'type'), el('th', {}, 'count')), ...kinds.map(([k, n]) => el('tr', {}, el('td', {}, k), el('td', {}, n)))) : el('p', { class: 'muted' }, 'Nothing caught yet. Try pasting a phone number or email into the chat.'),
    el('h3', {}, 'By stage'),
    ...Object.entries(g.by_stage || {}).map(([k, n]) => el('div', { class: 'muted' }, k + ': ' + n)));
}
async function loadPerf() {
  const d = await api('/api/perf'); const p = $('#p-perf');
  const t = d.tokens.total;
  p.replaceChildren(
    el('h3', {}, 'Tokens and cost'),
    el('div', {}, `${t.calls} model calls - ${t.prompt_tokens} prompt / ${t.output_tokens} output tokens` + (t.estimated_calls ? ` (${t.estimated_calls} estimated)` : '') + ` - ${d.tokens.currency} ${t.cost_usd}`),
    el('div', { class: 'muted' }, d.tokens.note),
    d.tokens.agents.length ? el('table', {}, el('tr', {}, ...['agent', 'calls', 'prompt', 'output'].map(x => el('th', {}, x))), ...d.tokens.agents.map(a => el('tr', {}, el('td', {}, a.agent), el('td', {}, a.calls), el('td', {}, a.prompt_tokens), el('td', {}, a.output_tokens)))) : el('p', { class: 'muted' }, 'No model calls yet.'),
    el('h3', {}, 'Latency (ms)'),
    Object.keys(d.timing.spans).length ? el('table', {}, el('tr', {}, ...['step', 'n', 'p50', 'p95'].map(x => el('th', {}, x))), ...Object.entries(d.timing.spans).map(([k, v]) => el('tr', {}, el('td', {}, k), el('td', {}, v.count), el('td', {}, v.p50_ms), el('td', {}, v.p95_ms)))) : el('p', { class: 'muted' }, 'No timings yet.'));
}
async function loadReport() { const r = await api('/api/report'); $('#p-report').replaceChildren(el('pre', {}, r.markdown)); }
function refresh() {
  loadApprovals().catch(() => {});
  if (activeTab === 'health') loadHealth().catch(() => {});
  if (activeTab === 'rules') loadRules().catch(() => {});
  if (activeTab === 'security') loadSecurity().catch(() => {});
  if (activeTab === 'perf') loadPerf().catch(() => {});
  if (activeTab === 'report') loadReport().catch(() => {});
}
async function init() {
  const s = await api('/api/status');
  $('#pills').append(el('span', { class: 'pill ' + (s.gemini_credentials ? 'ok' : 'warn') }, s.gemini_credentials ? 'Gemini: ' + s.model : 'Gemini key missing'),
    el('span', { class: 'pill' }, s.live_apis ? 'Live Google APIs' : 'Mock providers'));
  addMsg('bot', '<div class="who">orchestrator</div>' + md('Hi - I run logistics for **Aurelio Menswear**. Ask about shipments, stock-outs, returns or carrier claims, or attach a photo. Try a shortcut below.' + (s.gemini_credentials ? '' : '\n\n**Gemini credentials are missing** - add GOOGLE_API_KEY to .env and restart (see README).')));
  refresh(); setInterval(refresh, 5000);
}
init();
