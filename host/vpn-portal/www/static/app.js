// databyte VPN Portal — vanilla JS client
// Talks to /api/* on the same origin (FastAPI backend).
// State: page + selected customer. No router lib, no build step.

const API = '';  // same origin

// ─── State ──────────────────────────────────────────────
const state = {
  user: null,           // 'admin' on login
  page: 'dashboard',    // 'dashboard' | 'customers' | 'tiers' | 'sessions' | 'security'
  customers: [],
  selectedCustomerId: null,
  customerDetail: null,
  tiers: [],
  pools: [],
  sessionsRaw: '',
  bans: [],
  whitelist: [],
  deadman: null,
  health: null,
  loading: false,
};

// ─── API wrapper ────────────────────────────────────────
async function api(path, opts = {}) {
  opts.credentials = 'same-origin';
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
  const r = await fetch(API + path, opts);
  if (r.status === 401) { state.user = null; render(); throw new Error('Not authenticated'); }
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) throw new Error(data?.detail || `HTTP ${r.status}`);
  return data;
}

const get = (p) => api(p);
const post = (p, body) => api(p, { method: 'POST', body });
const del  = (p) => api(p, { method: 'DELETE' });

// ─── Format helpers ──────────────────────────────────────
const fmtBytes = (n) => {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 ** 2) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + ' MB';
  return (n / 1024 ** 3).toFixed(2) + ' GB';
};
const fmtTime = (epoch) => {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC');
};
const fmtPct = (pct) => {
  if (pct == null) return '—';
  return pct.toFixed(1) + '%';
};
const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

// ─── Toast ──────────────────────────────────────────────
let toastTimer = null;
function toast(msg, kind = 'ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast toast-' + kind;
  el.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.display = 'none'; }, 3500);
}

// ─── Render ─────────────────────────────────────────────
function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v != null) e.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
}

function render() {
  const root = document.getElementById('app');
  root.innerHTML = '';
  if (!state.user) {
    root.appendChild(renderLogin());
    return;
  }
  const wrap = el('div', { class: 'app' });
  wrap.appendChild(renderNav());
  wrap.appendChild(renderMain());
  root.appendChild(wrap);
}

function renderLogin() {
  const wrap = el('div', { class: 'login-wrap' });
  const card = el('div', { class: 'login-card' });
  card.appendChild(el('div', { class: 'login-title' }, 'databyte VPN Portal'));
  card.appendChild(el('div', { class: 'login-sub' }, '5C.1 · admin auth'));
  const errBox = el('div', { id: 'login-err', class: 'login-err', style: 'display:none' });
  card.appendChild(errBox);
  const form = el('form', { onsubmit: handleLogin });
  form.appendChild(el('div', { class: 'form-group' }, [
    el('label', { class: 'form-label' }, 'Username'),
    el('input', { id: 'login-user', class: 'inp', type: 'text', value: 'admin', autocomplete: 'username', required: true }),
  ]));
  form.appendChild(el('div', { class: 'form-group', style: 'margin-top:14px' }, [
    el('label', { class: 'form-label' }, 'Password'),
    el('input', { id: 'login-pass', class: 'inp', type: 'password', autocomplete: 'current-password', required: true }),
  ]));
  form.appendChild(el('button', { class: 'btn btn-cyan', type: 'submit', style: 'width:100%; margin-top:18px; padding:9px;' }, 'Sign in'));
  card.appendChild(form);
  wrap.appendChild(card);
  return wrap;
}

async function handleLogin(e) {
  e.preventDefault();
  const errBox = document.getElementById('login-err');
  errBox.style.display = 'none';
  try {
    const r = await post('/api/login', {
      username: document.getElementById('login-user').value,
      password: document.getElementById('login-pass').value,
    });
    state.user = r.user;
    await loadDashboard();
    render();
  } catch (err) {
    errBox.textContent = err.message || 'Login failed';
    errBox.style.display = 'block';
  }
}

function renderNav() {
  const nav = el('div', { class: 'nav' });
  nav.appendChild(el('div', { class: 'nav-logo' }, 'databyte · vpn'));
  nav.appendChild(el('div', { class: 'nav-sep' }));
  for (const [p, label] of [
    ['dashboard', 'Dashboard'],
    ['customers', 'Customers'],
    ['tiers', 'Tiers'],
    ['sessions', 'Sessions'],
    ['security', 'Security'],
  ]) {
    nav.appendChild(el('button', {
      class: 'nav-tab' + (state.page === p ? ' active' : ''),
      onclick: () => switchPage(p),
    }, label));
  }
  const right = el('div', { class: 'nav-right' });
  right.appendChild(el('span', { class: 'nav-user' }, state.user));
  right.appendChild(el('span', { class: 'nav-badge badge-admin' }, 'admin'));
  right.appendChild(el('button', { class: 'btn btn-ghost', onclick: handleLogout }, 'Logout'));
  nav.appendChild(right);
  return nav;
}

function renderMain() {
  const main = el('main', { class: 'main' });
  const fn = { dashboard: renderDashboard, customers: renderCustomers, tiers: renderTiers, sessions: renderSessions, security: renderSecurity }[state.page];
  if (fn) fn(main);
  return main;
}

async function switchPage(p) {
  state.page = p;
  if (p === 'dashboard') await loadDashboard();
  if (p === 'customers') await loadCustomers();
  if (p === 'tiers') await loadTiers();
  if (p === 'sessions') await loadSessions();
  if (p === 'security') await loadSecurity();
  render();
}

async function handleLogout() {
  try { await post('/api/logout'); } catch {}
  state.user = null;
  render();
}

// ─── Dashboard ──────────────────────────────────────────
async function loadDashboard() {
  state.loading = true;
  try {
    const [health, customers, tiers, pools, dm] = await Promise.all([
      get('/api/health'),
      get('/api/customers'),
      get('/api/tiers'),
      get('/api/vpn/pools'),
      get('/api/security/deadman'),
    ]);
    state.health = health;
    state.customers = customers;
    state.tiers = tiers;
    state.pools = pools;
    state.deadman = dm;
  } catch (e) { toast(e.message, 'err'); }
  state.loading = false;
}

function renderDashboard(main) {
  main.appendChild(el('div', { class: 'page-title' }, 'Dashboard'));
  main.appendChild(el('div', { class: 'page-sub' }, 'System health, customer rollup, VPN topology.'));

  // Health row
  const h = state.health || {};
  const healthGrid = el('div', { class: 'grid-4' });
  healthGrid.appendChild(metricCard('Service', h.status || '—', null, h.status === 'ok' ? 'green' : 'red'));
  healthGrid.appendChild(metricCard('DB', h.db_ok ? 'connected' : 'down', h.db_customers != null ? `${h.db_customers} customers` : '', h.db_ok ? 'green' : 'red'));
  healthGrid.appendChild(metricCard('charon', h.charon_ok ? 'reachable' : 'down', 'vici @ .98', h.charon_ok ? 'green' : 'red'));
  healthGrid.appendChild(metricCard('ipBan', state.deadman?.service || '—', state.deadman?.active_bans != null ? `${state.deadman.active_bans} active bans` : '', state.deadman?.service === 'active' ? 'green' : 'amber'));
  main.appendChild(healthGrid);

  // Pools
  const poolsCard = el('div', { class: 'card' });
  poolsCard.appendChild(el('div', { class: 'card-title' }, 'Pools'));
  if (state.pools.length) {
    const list = el('div', { class: 'kv' });
    for (const p of state.pools) {
      const used = (parseInt(p.size) || 0).toLocaleString();
      list.appendChild(el('dt', {}, p.name));
      list.appendChild(el('dd', {}, `${p.base} · ${used} leases`));
    }
    poolsCard.appendChild(list);
  } else {
    poolsCard.appendChild(el('div', { class: 'dim' }, 'No pools loaded.'));
  }
  main.appendChild(poolsCard);

  // Customer rollup
  const cust = state.customers || [];
  const cutCount = cust.filter(c => c.over_quota).length;
  const opCount = cust.filter(c => c.is_operator).length;
  const rollup = el('div', { class: 'grid-4' });
  rollup.appendChild(metricCard('Customers', cust.length, `${opCount} operator · ${cust.length - opCount} paid`));
  rollup.appendChild(metricCard('Over quota', cutCount, 'hard cut in effect', cutCount ? 'red' : 'green'));
  const totalUsed = cust.reduce((a, c) => a + (c.used_bytes || 0), 0);
  rollup.appendChild(metricCard('Total data used', fmtBytes(totalUsed), 'across all customers'));
  const tierCount = state.tiers.length;
  rollup.appendChild(metricCard('Tiers', tierCount, '3 GB · 10 GB · 15 GB · demo'));
  main.appendChild(rollup);

  // Quick action — view customers
  const action = el('div', { class: 'btn-row' });
  action.appendChild(el('button', { class: 'btn btn-cyan', onclick: () => switchPage('customers') }, 'View customers →'));
  action.appendChild(el('button', { class: 'btn btn-ghost', onclick: () => loadDashboard().then(render) }, '↻ Refresh'));
  main.appendChild(action);
}

function metricCard(label, value, sub, color) {
  const valStyle = color ? `color: var(--${color})` : '';
  return el('div', { class: 'card card-sm' }, [
    el('div', { class: 'metric-val', style: valStyle }, value),
    el('div', { class: 'metric-label' }, label),
    sub ? el('div', { class: 'metric-sub' }, sub) : null,
  ]);
}

// ─── Customers ──────────────────────────────────────────
async function loadCustomers() {
  try {
    const [cust, tiers] = await Promise.all([get('/api/customers'), get('/api/tiers')]);
    state.customers = cust;
    state.tiers = tiers;
  } catch (e) { toast(e.message, 'err'); }
}

function renderCustomers(main) {
  main.appendChild(el('div', { class: 'page-title' }, 'Customers'));
  main.appendChild(el('div', { class: 'page-sub' }, 'Click a row for details. Reset zeroes usage; hard cut flips over_quota back to 0.'));

  const row = el('div', { class: 'row-2col' });

  // ─── Left: table ───
  const left = el('div', {});
  const wrap = el('div', { class: 'tbl-wrap' });
  const table = el('table');
  const thead = el('thead', {}, el('tr', {}, [
    el('th', {}, 'Name'),
    el('th', {}, 'Tier'),
    el('th', {}, 'Usage'),
    el('th', {}, '%'),
    el('th', {}, 'State'),
  ]));
  table.appendChild(thead);
  const tbody = el('tbody');
  for (const c of state.customers) {
    const sel = state.selectedCustomerId === c.id ? 'selected' : '';
    const tr = el('tr', { class: sel, onclick: () => selectCustomer(c.id) });
    tr.appendChild(el('td', {}, el('span', { class: 'mono' }, c.display_name || c.name)));
    tr.appendChild(el('td', {}, el('span', { class: 'badge b-dim' }, c.tier_display || (c.is_operator ? 'operator' : '—'))));
    tr.appendChild(el('td', { class: 'mono' }, fmtBytes(c.used_bytes) + ' / ' + fmtBytes(c.quota_bytes)));
    tr.appendChild(el('td', { class: 'mono' }, fmtPct(c.pct)));
    const state = c.over_quota ? badge('CUT', 'red') : c.pct >= 80 ? badge('NEAR', 'amber') : badge('OK', 'green');
    tr.appendChild(el('td', {}, state));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  left.appendChild(wrap);
  left.appendChild(el('div', { class: 'btn-row' }, [
    el('button', { class: 'btn btn-ghost', onclick: () => loadCustomers().then(render) }, '↻ Refresh'),
  ]));
  row.appendChild(left);

  // ─── Right: detail ───
  const right = el('div', { id: 'customer-detail' });
  if (state.selectedCustomerId) {
    right.appendChild(renderCustomerDetail());
  } else {
    const ph = el('div', { class: 'card', style: 'text-align:center; color: var(--textDim); padding: 40px' });
    ph.appendChild(el('div', {}, 'Select a customer'));
    right.appendChild(ph);
  }
  row.appendChild(right);
  main.appendChild(row);
}

function badge(text, kind) { return el('span', { class: 'badge b-' + kind }, text); }

async function selectCustomer(id) {
  state.selectedCustomerId = id;
  try {
    state.customerDetail = await get(`/api/customers/${id}`);
  } catch (e) { toast(e.message, 'err'); return; }
  render();
}

function renderCustomerDetail() {
  const c = state.customerDetail;
  if (!c) return el('div', {});
  const card = el('div', { class: 'card' });
  card.appendChild(el('div', { class: 'card-title' }, `${c.display_name || c.name}  ·  ${c.tier_display || 'no tier'}`));

  // Stats grid
  const grid = el('div', { class: 'grid-2' });
  grid.appendChild(metricCard('Used', fmtBytes(c.used_bytes), c.pct.toFixed(1) + '% of quota', c.over_quota ? 'red' : (c.pct >= 80 ? 'amber' : 'green')));
  grid.appendChild(metricCard('Quota', fmtBytes(c.quota_bytes), c.is_operator ? 'operator (bypass)' : 'effective limit'));
  card.appendChild(grid);

  // Bar
  const pctClamped = Math.min(100, c.pct);
  const barColor = c.over_quota ? 'red' : (c.pct >= 80 ? 'amber' : 'green');
  const bar = el('div', { class: 'bar-wrap', style: 'margin-bottom: 16px' });
  bar.appendChild(el('div', { class: 'bar-fill bar-' + barColor, style: `width: ${pctClamped}%` }));
  card.appendChild(bar);

  // Buttons
  const actions = el('div', { class: 'btn-row' });
  actions.appendChild(el('button', {
    class: 'btn btn-amber',
    onclick: () => doReset(c.id, c.name),
  }, '↺ Reset usage'));
  card.appendChild(actions);

  // Meta
  const meta = el('dl', { class: 'kv' });
  meta.appendChild(el('dt', {}, 'Status'));
  meta.appendChild(el('dd', {}, c.status + (c.is_active ? ' · active' : ' · INACTIVE')));
  meta.appendChild(el('dt', {}, 'Operator'));
  meta.appendChild(el('dd', {}, c.is_operator ? 'yes (bypass quota)' : 'no'));
  meta.appendChild(el('dt', {}, 'Telegram'));
  meta.appendChild(el('dd', {}, c.telegram_username || '—'));
  meta.appendChild(el('dt', {}, 'Created'));
  meta.appendChild(el('dd', {}, fmtTime(c.created_at)));
  meta.appendChild(el('dt', {}, 'Updated'));
  meta.appendChild(el('dd', {}, fmtTime(c.updated_at)));
  if (c.notes) { meta.appendChild(el('dt', {}, 'Notes')); meta.appendChild(el('dd', {}, c.notes)); }
  card.appendChild(meta);

  // Devices
  if (c.devices && c.devices.length) {
    const devCard = el('div', { class: 'card' });
    devCard.appendChild(el('div', { class: 'card-title' }, `Devices (${c.devices.length})`));
    const devTable = el('table');
    devTable.appendChild(el('thead', {}, el('tr', {}, [
      el('th', {}, 'Name'), el('th', {}, 'Last VIP'),
      el('th', {}, 'Last seen'), el('th', {}, 'Active'),
    ])));
    const devBody = el('tbody');
    for (const d of c.devices) {
      const tr = el('tr', {});
      tr.appendChild(el('td', { class: 'mono' }, d.device_name));
      tr.appendChild(el('td', { class: 'mono' }, d.last_seen_v4 || '—'));
      tr.appendChild(el('td', { class: 'mono' }, fmtTime(d.last_seen_at)));
      tr.appendChild(el('td', {}, d.is_active ? badge('active', 'green') : badge('disabled', 'red')));
      devBody.appendChild(tr);
    }
    devTable.appendChild(devBody);
    devCard.appendChild(devTable);
    card.appendChild(devCard);
  }

  // Alerts
  if (c.alerts && c.alerts.length) {
    const aCard = el('div', { class: 'card' });
    aCard.appendChild(el('div', { class: 'card-title' }, `Alerts (${c.alerts.length})`));
    const aTbl = el('table');
    aTbl.appendChild(el('thead', {}, el('tr', {}, [
      el('th', {}, 'Threshold'), el('th', {}, 'At'),
      el('th', {}, 'Bytes at alert'),
    ])));
    const aBody = el('tbody');
    for (const a of c.alerts) {
      const tr = el('tr', {});
      tr.appendChild(el('td', {}, badge(a.threshold + '%', a.threshold >= 100 ? 'red' : 'amber')));
      tr.appendChild(el('td', { class: 'mono' }, fmtTime(a.sent_at)));
      tr.appendChild(el('td', { class: 'mono' }, fmtBytes(a.data_used_bytes_at_alert)));
      aBody.appendChild(tr);
    }
    aTbl.appendChild(aBody);
    aCard.appendChild(aTbl);
    card.appendChild(aCard);
  }

  return card;
}

async function doReset(id, name) {
  if (!confirm(`Reset usage for ${name}? data_used_bytes → 0, over_quota → 0. Audit-logged.`)) return;
  try {
    const r = await post(`/api/quota/${id}/reset`);
    toast(`Reset ${name}: from ${fmtBytes(r.reset_from_bytes)} → 0`, 'ok');
    await loadCustomers();
    if (state.selectedCustomerId === id) state.customerDetail = await get(`/api/customers/${id}`);
    render();
  } catch (e) { toast(e.message, 'err'); }
}

// ─── Tiers ──────────────────────────────────────────────
async function loadTiers() {
  try { state.tiers = await get('/api/tiers'); } catch (e) { toast(e.message, 'err'); }
}

function renderTiers(main) {
  main.appendChild(el('div', { class: 'page-title' }, 'Tiers'));
  main.appendChild(el('div', { class: 'page-sub' }, 'Quota tiers. Read-only here — schema changes go through 5C.3 admin page.'));

  const grid = el('div', { class: 'grid-3' });
  for (const t of state.tiers) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-title' }, t.display_name));
    card.appendChild(el('div', { class: 'metric-val' }, fmtBytes(t.quota_bytes)));
    card.appendChild(el('div', { class: 'metric-sub' }, t.is_active ? 'active' : 'archived'));
    if (t.notes) card.appendChild(el('div', { class: 'muted', style: 'margin-top: 12px; font-size: 12px' }, t.notes));
    grid.appendChild(card);
  }
  main.appendChild(grid);
}

// ─── Sessions ───────────────────────────────────────────
async function loadSessions() {
  try {
    const [s, p] = await Promise.all([get('/api/vpn/sessions'), get('/api/vpn/pools')]);
    state.sessionsRaw = s.raw || '';
    state.pools = p;
  } catch (e) { toast(e.message, 'err'); }
}

function renderSessions(main) {
  main.appendChild(el('div', { class: 'page-title' }, 'Sessions'));
  main.appendChild(el('div', { class: 'page-sub' }, 'Active IKE SAs + virtual-IP pool assignments.'));

  // Pools
  const pCard = el('div', { class: 'card' });
  pCard.appendChild(el('div', { class: 'card-title' }, 'Pools'));
  if (state.pools.length) {
    const list = el('dl', { class: 'kv' });
    for (const p of state.pools) {
      list.appendChild(el('dt', {}, p.name));
      list.appendChild(el('dd', {}, `${p.base}  ·  ${p.size} leases`));
    }
    pCard.appendChild(list);
  } else { pCard.appendChild(el('div', { class: 'dim' }, 'No pools.')); }
  main.appendChild(pCard);

  // Sessions raw
  const sCard = el('div', { class: 'card' });
  sCard.appendChild(el('div', { class: 'card-title' }, 'Active SAs (swanctl --list-sas)'));
  sCard.appendChild(el('pre', { class: 'raw-pre' }, state.sessionsRaw || '(no active SAs)'));
  main.appendChild(sCard);
}

// ─── Security ───────────────────────────────────────────
async function loadSecurity() {
  try {
    const [b, w, d] = await Promise.all([
      get('/api/security/bans'),
      get('/api/security/whitelist'),
      get('/api/security/deadman'),
    ]);
    state.bans = b;
    state.whitelist = w;
    state.deadman = d;
  } catch (e) { toast(e.message, 'err'); }
}

function renderSecurity(main) {
  main.appendChild(el('div', { class: 'page-title' }, 'Security'));
  main.appendChild(el('div', { class: 'page-sub' }, 'ipBan bans, firewalld trusted zone, deadman switch.'));

  // Deadman status
  const d = state.deadman || {};
  const dCard = el('div', { class: 'card' });
  dCard.appendChild(el('div', { class: 'card-title' }, 'ipBan status'));
  const dGrid = el('div', { class: 'grid-2' });
  dGrid.appendChild(metricCard('Service', d.service || '—', '', d.service === 'active' ? 'green' : 'red'));
  dGrid.appendChild(metricCard('Active bans', d.active_bans != null ? d.active_bans : '—', '', d.active_bans > 0 ? 'amber' : 'green'));
  dCard.appendChild(dGrid);
  if (d.log_tail) {
    dCard.appendChild(el('div', { class: 'card-title', style: 'margin-top: 16px' }, 'Recent log'));
    dCard.appendChild(el('pre', { class: 'raw-pre' }, d.log_tail.split('\n').slice(-10).join('\n')));
  }
  main.appendChild(dCard);

  // Whitelist (firewalld trusted zone)
  const wCard = el('div', { class: 'card' });
  wCard.appendChild(el('div', { class: 'card-title' }, 'Whitelist (firewalld trusted zone)'));
  if (state.whitelist.length) {
    const tbl = el('table');
    tbl.appendChild(el('thead', {}, el('tr', {}, [el('th', {}, 'CIDR')])));
    const tb = el('tbody');
    for (const w of state.whitelist) tb.appendChild(el('tr', {}, el('td', { class: 'mono' }, w.cidr)));
    tbl.appendChild(tb);
    wCard.appendChild(tbl);
  } else { wCard.appendChild(el('div', { class: 'dim' }, 'Empty.')); }

  // Add to whitelist form
  wCard.appendChild(el('div', { class: 'card-title', style: 'margin-top: 16px' }, 'Add CIDR'));
  const form = el('form', { onsubmit: handleAddWhitelist });
  form.appendChild(el('div', { class: 'form-row' }, [
    el('div', { class: 'form-group' }, [
      el('label', { class: 'form-label' }, 'CIDR'),
      el('input', { id: 'whitelist-cidr', class: 'inp inp-mono', placeholder: '192.168.1.0/24', required: true }),
    ]),
    el('button', { class: 'btn btn-cyan', type: 'submit' }, '+ Add'),
  ]));
  wCard.appendChild(form);
  main.appendChild(wCard);

  // Bans
  const bCard = el('div', { class: 'card' });
  bCard.appendChild(el('div', { class: 'card-title' }, `Bans (${state.bans.length})`));
  if (state.bans.length) {
    const tbl = el('table');
    tbl.appendChild(el('thead', {}, el('tr', {}, [
      el('th', {}, 'IP'), el('th', {}, 'Source'),
      el('th', {}, 'Count'), el('th', {}, 'Banned at'),
      el('th', {}, ''),
    ])));
    const tb = el('tbody');
    for (const b of state.bans) {
      const tr = el('tr', {});
      tr.appendChild(el('td', { class: 'mono' }, b.ip));
      tr.appendChild(el('td', { class: 'mono' }, b.source || '—'));
      tr.appendChild(el('td', { class: 'mono' }, b.count != null ? b.count : '—'));
      tr.appendChild(el('td', { class: 'mono' }, fmtTime(b.ban_date)));
      tr.appendChild(el('td', {}, el('button', {
        class: 'btn btn-green', onclick: () => doUnban(b.ip),
      }, 'Unban')));
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    bCard.appendChild(tbl);
  } else { bCard.appendChild(el('div', { class: 'muted' }, 'No active bans.')); }
  main.appendChild(bCard);
}

async function handleAddWhitelist(e) {
  e.preventDefault();
  const cidr = document.getElementById('whitelist-cidr').value.trim();
  try {
    await post('/api/security/whitelist/add', { cidr });
    toast(`Whitelisted ${cidr}`, 'ok');
    document.getElementById('whitelist-cidr').value = '';
    await loadSecurity();
    render();
  } catch (e) { toast(e.message, 'err'); }
}

async function doUnban(ip) {
  if (!confirm(`Unban ${ip}?`)) return;
  try {
    await post('/api/security/unban', { ip });
    toast(`Unbanned ${ip}`, 'ok');
    await loadSecurity();
    render();
  } catch (e) { toast(e.message, 'err'); }
}

// ─── Boot ───────────────────────────────────────────────
(async function init() {
  const toastEl = el('div', { id: 'toast', class: 'toast', style: 'display:none' });
  document.body.appendChild(toastEl);
  try {
    const r = await get('/api/health');
    if (r.status === 'ok' || r.status === 'degraded') {
      // session is cookie-based; try a cheap call
      try { const me = await get('/api/customers'); state.user = 'admin'; await loadDashboard(); }
      catch { state.user = null; }
    }
  } catch {}
  render();
})();
