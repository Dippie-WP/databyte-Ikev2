// databyte VPN Portal — vanilla JS client
// Talks to /api/* on same origin. No build step, no external deps.

(function() {
  'use strict';

  // ─── Theme ─────────────────────────────────────────────
  const THEMES = ['dark', 'light'];
  let themeIdx = 0; // 0=dark, 1=light

  function applyTheme(idx) {
    themeIdx = idx;
    document.body.setAttribute('data-theme', THEMES[idx]);
    try { localStorage.setItem('vp-theme', idx); } catch {}
    // update toggle icon
    const btn = document.getElementById('theme-btn');
    if (btn) btn.textContent = idx === 0 ? '☀' : '☽';
  }

  function toggleTheme() {
    applyTheme((themeIdx + 1) % 2);
  }

  function loadTheme() {
    try {
      const saved = localStorage.getItem('vp-theme');
      if (saved !== null) applyTheme(parseInt(saved, 10) || 0);
      else applyTheme(0);
    } catch { applyTheme(0); }
  }

  // ─── State ─────────────────────────────────────────────
  const S = {
    user: null,
    page: 'dashboard',
    customers: [],
    selectedId: null,
    detail: null,
    tiers: [],
    pools: [],
    sessions: '',
    leases: [],
    bans: [],
    whitelist: [],
    deadman: null,
    health: null,
    // Loading flags per page (true while data is fetching)
    loading: { dashboard: false, customers: false, tiers: false, sessions: false, security: false },
    // Per-page errors (set if load failed; cleared on next successful load)
    loadError: { dashboard: null, customers: null, tiers: null, sessions: null, security: null },
    // Background "refreshing" flags — same as loading but suppresses skeleton flash
    // so auto-refresh doesn't blink the page. Used for auto-refresh on Sessions.
    refreshing: { sessions: false },
    // Auto-refresh timer for Sessions page (only active when there's a live lease)
    _sessionsTimer: null,
  };

  // ─── Skeleton + empty helpers ──────────────────────────
  // Visual placeholders for loading state. `width` / `height` optional.
  function skel(cls, w, h) {
    const c = 'vp-skel ' + (cls || 'vp-skel-line');
    return el('span', { cls: c, style: (w ? 'width:' + w + ';' : '') + (h ? 'height:' + h + ';' : '') }, '\u00a0');
  }
  function skelBlock(w, h) { return skel('vp-skel-block', w, h); }
  function skelNum(w)     { return skel('vp-skel-num', w || '70%'); }
  function skelLine(w)    { return skel('vp-skel-line', w); }

  // Empty state — used when a list returns [] or a page has no data to show.
  function emptyState(icon, title, sub) {
    return el('div', { cls: 'vp-empty' },
      el('div', { cls: 'vp-empty-icon' }, icon || '—'),
      el('div', { cls: 'vp-empty-title' }, title || 'Nothing here yet'),
      sub ? el('div', { cls: 'vp-empty-sub' }, sub) : null,
    );
  }

  // Spinner row — small "loading..." text
  function spinnerRow(msg) {
    return el('div', { cls: 'vp-spinner-row' },
      el('span', { cls: 'vp-spin' }),
      msg || 'Loading…',
    );
  }

  // ─── API ───────────────────────────────────────────────
  async function api(path, opts) {
    opts = opts || {};
    opts.credentials = 'same-origin';
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    const r = await fetch(path, opts);
    if (r.status === 401) { S.user = null; render(); throw new Error('Not authenticated'); }
    const text = await r.text();
    let data;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
    return data;
  }
  const get  = p  => api(p);
  const post = (p, b) => api(p, { method: 'POST', body: b });
  const del  = p  => api(p, { method: 'DELETE' });

  // ─── Format ────────────────────────────────────────────
  function fmtBytes(n) {
    if (n == null) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
    return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  }
  function fmtPct(p) { return p != null ? p.toFixed(1) + '%' : '—'; }

  // Visual usage bar used in the leases table.
  // For operators (no limit) shows "—" instead of a bar.
  function usageBar(used, limit, pct, over_quota, is_operator) {
    if (is_operator || !limit) {
      return el('span', { cls: 'vp-usage-text vp-mono', title: 'Operator account — bypasses quota' }, 'unlimited');
    }
    const barColor = over_quota ? 'var(--red)' : (pct >= 80 ? 'var(--amber)' : 'var(--green)');
    return el('div', { cls: 'vp-usage' },
      el('div', { cls: 'vp-usage-track' },
        el('div', {
          cls: 'vp-usage-fill',
          style: 'width: ' + Math.min(100, Math.max(0, pct)) + '%; background: ' + barColor,
        }),
      ),
      el('div', { cls: 'vp-usage-text vp-mono', style: 'color: ' + barColor },
        fmtBytes(used) + ' / ' + fmtBytes(limit) + ' (' + pct.toFixed(1) + '%)'),
    );
  }
  function fmtTime(e) {
    if (!e) return '—';
    return new Date(e * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC');
  }

  // ─── DOM helpers ───────────────────────────────────────
  // el('div', {cls, attrs}, children...)
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'cls') e.className = v;
      else if (k === 'html') e.innerHTML = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v != null) e.setAttribute(k, v);
    }
    for (const c of children) {
      if (c == null) continue;
      if (Array.isArray(c)) continue;  // placeholder for absent children (e.g. [] when sub is missing)
      if (c instanceof Node) { e.appendChild(c); continue; }
      e.appendChild(document.createTextNode(String(c)));
    }
    return e;
  }

  // Banner: fixed top-center error/info message
  let bannerTimer = null;
  function showBanner(msg, kind) {
    // kind: 'err' | 'ok'
    let b = document.getElementById('vp-banner');
    if (!b) {
      b = el('div', { id: 'vp-banner' });
      document.body.appendChild(b);
    }
    b.className = 'vp-banner vp-banner-' + kind;
    b.textContent = msg;
    b.style.display = 'block';
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(() => { b.style.display = 'none'; }, 4500);
  }

  // ─── Render (entry point) ───────────────────────────────
  function render() {
    try {
      const root = document.getElementById('app');
      root.innerHTML = '';
      if (!S.user) {
        root.appendChild(renderLogin());
      } else {
        root.appendChild(el('div', { cls: 'vp-app' },
          renderNav(),
          el('main', { cls: 'vp-main' },
            S.page === 'dashboard'  ? renderDashboard()  :
            S.page === 'customers'  ? renderCustomers()  :
            S.page === 'tiers'     ? renderTiers()      :
            S.page === 'sessions'  ? renderSessions()   :
                                     renderSecurity()
          )
        ));
      }
    } catch(err) {
      // Last-resort: if render throws, show raw error so we know what happened
      const root = document.getElementById('app');
      root.innerHTML = '';
      root.appendChild(el('pre', {
        cls: 'vp-errdump',
        html: 'RENDER ERROR:\n' + err.message + '\n\n' + (err.stack || '')
      }));
    }
  }

  // ─── Login ─────────────────────────────────────────────
  function renderLogin() {
    const wrap = el('div', { cls: 'vp-login-wrap' });
    const card = el('div', { cls: 'vp-login-card' });
    card.appendChild(el('div', { cls: 'vp-login-logo' }, 'databyte'));
    card.appendChild(el('div', { cls: 'vp-login-sub' }, 'VPN Portal · admin'));
    const errEl = el('div', { id: 'vp-login-err', cls: 'vp-login-err', style: 'display:none' });
    card.appendChild(errEl);
    const form = el('form', { onsubmit: onLoginSubmit });
    form.appendChild(el('div', { cls: 'vp-field' }, [
      el('label', { cls: 'vp-label' }, 'Username'),
      el('input', { id: 'vp-user', cls: 'vp-inp', type: 'text', autocomplete: 'username', required: true }),
    ]));
    form.appendChild(el('div', { cls: 'vp-field' }, [
      el('label', { cls: 'vp-label' }, 'Password'),
      el('input', { id: 'vp-pass', cls: 'vp-inp', type: 'password', autocomplete: 'current-password', required: true }),
    ]));
    form.appendChild(el('button', { cls: 'vp-btn vp-btn-primary', type: 'submit' }, 'Sign in'));
    card.appendChild(form);
    wrap.appendChild(card);
    return wrap;
  }

  async function onLoginSubmit(e) {
    e.preventDefault();
    const errEl = document.getElementById('vp-login-err');
    errEl.style.display = 'none';
    errEl.textContent = '';
    const user = document.getElementById('vp-user').value;
    const pass = document.getElementById('vp-pass').value;
    try {
      const r = await post('/api/login', { username: user, password: pass });
      S.user = r.user || user;
      await loadDashboard();
      render();
    } catch(err) {
      errEl.textContent = err.message || 'Login failed';
      errEl.style.display = 'block';
    }
  }

  async function doLogout() {
    try { await post('/api/logout'); } catch {}
    S.user = null;
    render();
  }

  // ─── Nav ───────────────────────────────────────────────
  function renderNav() {
    const wrap = el('div', { cls: 'vp-nav-wrap' });
    const nav = el('div', { cls: 'vp-nav' });
    nav.appendChild(el('div', { cls: 'vp-nav-logo' }, 'databyte'));
    nav.appendChild(el('div', { cls: 'vp-nav-sep' }));
    const tabs = ['dashboard','customers','tiers','sessions','security'];
    for (const t of tabs) {
      nav.appendChild(el('button', {
        cls: 'vp-nav-tab' + (S.page === t ? ' vp-nav-tab-on' : ''),
        onclick: () => switchPage(t),
      }, capitalize(t)));
    }
    const right = el('div', { cls: 'vp-nav-right' });
    right.appendChild(el('span', { cls: 'vp-nav-user' }, S.user || ''));
    right.appendChild(el('button', { id: 'theme-btn', cls: 'vp-theme-btn', onclick: toggleTheme, title: 'Toggle theme' }, '☀'));
    right.appendChild(el('button', { cls: 'vp-btn vp-btn-ghost vp-btn-sm', onclick: doLogout }, 'Logout'));
    nav.appendChild(right);
    wrap.appendChild(nav);
    return wrap;
  }

  function capitalize(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

  // Single source of truth for which page-load function maps to which page name.
  const LOADERS = {
    dashboard: loadDashboard,
    customers: loadCustomers,
    tiers:     loadTiers,
    sessions:  loadSessions,
    security:  loadSecurity,
  };

  async function switchPage(p) {
    if (!LOADERS[p]) return;
    if (S.loading[p]) return;  // already in flight
    // Stop Sessions auto-refresh if we're leaving it
    if (S.page === 'sessions' && p !== 'sessions') stopSessionsAutoRefresh();
    S.page = p;
    // Re-render first so the user sees the skeleton for the new page immediately.
    render();
    // Fire the load. The load function sets S.loading[p]=true synchronously
    // before any await, so the second render() (inside switchPage) and the
    // post-load render() both see the correct loading state.
    await LOADERS[p]();
    if (S.page === p) render();
  }

  // ─── Load functions ────────────────────────────────────
  // Each loadX() sets S.loading.x=true, attempts the fetch, on success clears
  // S.loadError.x and updates state. On failure, S.loadError.x holds the
  // message and S.loading.x=false. The render functions check both flags and
  // show skeletons / error chips accordingly.
  async function loadDashboard() {
    S.loading.dashboard = true;
    S.loadError.dashboard = null;
    try {
      const [h, cust, tiers, pools, dm] = await Promise.all([
        get('/api/health'),
        get('/api/customers'),
        get('/api/tiers'),
        get('/api/vpn/pools'),
        get('/api/security/deadman'),
      ]);
      S.health   = h;
      S.customers = cust;
      S.tiers     = tiers;
      S.pools     = pools;
      S.deadman   = dm;
    } catch (e) {
      S.loadError.dashboard = e.message || 'Failed to load dashboard';
    } finally {
      S.loading.dashboard = false;
    }
  }

  async function loadCustomers() {
    S.loading.customers = true;
    S.loadError.customers = null;
    try {
      const [cust, tiers] = await Promise.all([
        get('/api/customers'),
        get('/api/tiers'),
      ]);
      S.customers = cust;
      S.tiers     = tiers;
    } catch (e) {
      S.loadError.customers = e.message || 'Failed to load customers';
    } finally {
      S.loading.customers = false;
    }
  }

  async function loadTiers() {
    S.loading.tiers = true;
    S.loadError.tiers = null;
    try {
      S.tiers = await get('/api/tiers');
    } catch (e) {
      S.loadError.tiers = e.message || 'Failed to load tiers';
    } finally {
      S.loading.tiers = false;
    }
  }

  async function loadSessions() {
    S.loading.sessions = true;
    S.loadError.sessions = null;
    try {
      const [s, p, l] = await Promise.all([
        get('/api/vpn/sessions'),
        get('/api/vpn/pools'),
        get('/api/vpn/leases'),
      ]);
      S.sessions = s.raw || s.sessions || '';
      S.pools    = p;
      S.leases   = l;
    } catch (e) {
      S.loadError.sessions = e.message || 'Failed to load sessions';
    } finally {
      S.loading.sessions = false;
    }
    // Auto-refresh: if there's at least one active lease, poll /api/vpn/leases
    // every 10s so the usage bar moves in real time. Stop when no leases left
    // or when the user navigates away (handled in switchPage).
    if (S.leases && S.leases.length > 0) {
      if (!S._sessionsTimer) startSessionsAutoRefresh();
    } else {
      stopSessionsAutoRefresh();
    }
  }

  function startSessionsAutoRefresh() {
    if (S._sessionsTimer) return;
    S._sessionsTimer = setInterval(async () => {
      // Bail if user navigated away
      if (S.page !== 'sessions') { stopSessionsAutoRefresh(); return; }
      S.refreshing.sessions = true;
      try {
        // Fetch only what we need for the live bar; skip the heavy
        // sessions/pools calls — the raw <pre> is not what needs live updates.
        const l = await get('/api/vpn/leases');
        S.leases = l;
        if (S.page === 'sessions') render();
      } catch (e) {
        // Soft-fail — keep the bar visible. Surface the error if it persists.
        S.loadError.sessions = e.message || 'Auto-refresh failed';
      } finally {
        S.refreshing.sessions = false;
      }
      // If the cut fired and SA is gone, stop the timer
      if (!S.leases || S.leases.length === 0) {
        stopSessionsAutoRefresh();
        // Do one full reload so the SA <pre> and Pools reflect reality
        try { await loadSessions(); } catch {}
        if (S.page === 'sessions') render();
      }
    }, 10000);
  }

  function stopSessionsAutoRefresh() {
    if (S._sessionsTimer) {
      clearInterval(S._sessionsTimer);
      S._sessionsTimer = null;
    }
  }

  async function loadSecurity() {
    S.loading.security = true;
    S.loadError.security = null;
    try {
      const [b, w, d] = await Promise.all([
        get('/api/security/bans'),
        get('/api/security/whitelist'),
        get('/api/security/deadman'),
      ]);
      S.bans      = b;
      S.whitelist = w;
      S.deadman   = d;
    } catch (e) {
      S.loadError.security = e.message || 'Failed to load security data';
    } finally {
      S.loading.security = false;
    }
  }

  // ─── Dashboard render ──────────────────────────────────
  function renderDashboard() {
    const h = S.health || {};
    const dm = S.deadman || {};
    const loading = S.loading.dashboard;
    const err     = S.loadError.dashboard;

    // ── Skeleton placeholders for metric cards (match shape of mCard) ──
    function skelMetric() {
      return el('div', { cls: 'vp-card vp-card-sm' },
        skelNum('70%'),
        skelLine('55%'),
        skelLine('40%'),
      );
    }

    return el('div', { cls: 'vp-page' },
      el('div', { cls: 'vp-page-head' },
        el('div', { cls: 'vp-page-title' }, 'Dashboard'),
        el('div', { cls: 'vp-page-sub' }, 'System health, customer rollup, VPN topology.'),
      ),
      err ? el('div', { cls: 'vp-empty', style: 'margin-bottom:14px; border-color: var(--red); color: var(--red)' },
              '⚠ ' + err) : null,
      // 4 metric cards (skeletons while loading)
      el('div', { cls: 'vp-row' },
        loading
          ? [skelMetric(), skelMetric(), skelMetric(), skelMetric()]
          : [
              mCard('Service', h.status === 'ok' ? 'OK' : (h.status || '—'), h.status === 'ok' ? 'green' : 'red'),
              mCard('Database', h.db_ok ? 'connected' : 'DOWN', h.db_ok ? 'green' : 'red',
                    h.db_customers != null ? h.db_customers + ' customers' : ''),
              mCard('charon', h.charon_ok ? 'reachable' : 'DOWN', h.charon_ok ? 'green' : 'red', 'vici @ .98'),
              mCard('ipBan', dm.service === 'active' ? 'active' : '—', dm.service === 'active' ? 'green' : 'amber',
                    dm.active_bans != null ? dm.active_bans + ' bans' : ''),
            ]
      ),
      // Pools card
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' }, 'VPN Pools'),
        loading
          ? skelBlock()
          : (S.pools && S.pools.length
              ? el('dl', { cls: 'vp-kv' }, ...S.pools.flatMap(p => {
                  const active = (S.leases || []).length;
                  return [
                    el('dt', {}, p.name),
                    el('dd', { cls: 'vp-mono' },
                      p.base + ' · ' + active + ' active lease' + (active === 1 ? '' : 's')),
                  ];
                }))
              : emptyState('⊘', 'No pools loaded', 'swanctl returned no virtual-IP pools. Check strongswan is running on .98.'))
      ),
      // Customer rollup
      el('div', { cls: 'vp-row' },
        loading
          ? [skelMetric(), skelMetric(), skelMetric(), skelMetric()]
          : [
              mCard('Customers', S.customers.length,
                S.customers.filter(c => c.is_operator).length + ' operator · ' +
                S.customers.filter(c => !c.is_operator).length + ' paid'),
              mCard('Over quota', S.customers.filter(c => c.over_quota).length, '', 'red'),
              mCard('Total data', fmtBytes(S.customers.reduce((a,c) => a + (c.used_bytes||0), 0)), 'across all'),
              mCard('Tiers', S.tiers.length, 'active tiers'),
            ]
      ),
      // Refresh
      el('div', { cls: 'vp-btn-row' },
        el('button', { cls: 'vp-btn vp-btn-primary', onclick: () => switchPage('customers') }, 'View customers →'),
        el('button', {
          cls: 'vp-btn vp-btn-ghost',
          onclick: () => { loadDashboard().then(render).catch(()=>{}); },
        }, loading ? spinnerRow('Refreshing…') : '↻ Refresh'),
      ),
    );
  }

  // ─── Customers render ──────────────────────────────────
  function renderCustomers() {
    const loading = S.loading.customers;
    const err     = S.loadError.customers;

    function skelRow() {
      return el('tr', {},
        el('td', {}, skelLine('80%')),
        el('td', {}, skelLine('50%')),
        el('td', {}, skelLine('90%')),
        el('td', {}, skelLine('40%')),
        el('td', {}, skelLine('40%')),
      );
    }

    return el('div', { cls: 'vp-page' },
      el('div', { cls: 'vp-page-head' },
        el('div', { cls: 'vp-page-title' }, 'Customers'),
        el('div', { cls: 'vp-page-sub' }, 'Click a row for full detail. ↺ Reset zeroes usage.'),
      ),
      err ? el('div', { cls: 'vp-empty', style: 'margin-bottom:14px; border-color: var(--red); color: var(--red)' },
              '⚠ ' + err) : null,
      el('div', { cls: 'vp-row-2' },
        // Left: table
        el('div', { cls: 'vp-left-col' },
          el('div', { cls: 'vp-tbl-wrap' },
            el('table', {},
              el('thead', {}, el('tr', {},
                el('th', {}, 'Name'), el('th', {}, 'Tier'),
                el('th', {}, 'Usage'), el('th', {}, '%'), el('th', {}, 'State'),
                el('th', {}, ''),
              )),
              el('tbody', {},
                ...(
                  loading
                    ? [skelRow(), skelRow(), skelRow()]
                    : S.customers.length
                      ? S.customers.map(c => {
                          const pct = c.pct || 0;
                          const state = c.over_quota ? ['CUT','red'] : pct >= 80 ? ['NEAR','amber'] : ['OK','green'];
                          return el('tr', {
                            cls: 'vp-tr' + (S.selectedId === c.id ? ' vp-tr-sel' : ''),
                            onclick: () => selectCustomer(c.id),
                          },
                            el('td', { cls: 'vp-mono', 'data-label': 'Name' }, c.display_name || c.name),
                            el('td', { 'data-label': 'Tier' }, spanBadge(c.is_operator ? 'operator' : (c.tier_display || '—'), 'dim')),
                            el('td', { cls: 'vp-mono', 'data-label': 'Usage' }, fmtBytes(c.used_bytes) + ' / ' + fmtBytes(c.quota_bytes)),
                            el('td', { cls: 'vp-mono', 'data-label': '%' }, fmtPct(pct)),
                            el('td', { 'data-label': 'State' }, spanBadge(state[0], state[1])),
                            el('td', { 'data-label': 'Actions', cls: 'vp-row-actions' },
                              el('button', {
                                cls: 'vp-btn-icon vp-btn-warn',
                                title: 'Reset usage + restore KILLED secrets + zero iptables',
                                onclick: (ev) => { ev.stopPropagation(); doReset(c.id, c.display_name || c.name); },
                              }, '↺'),
                            ),
                          );
                        })
                      : [el('tr', {}, el('td', { colspan: 6, style: 'padding:0' },
                            emptyState('∅', 'No customers yet', 'Add a customer via SSH + sqlite, or via the admin API.')))]
                )
              ),
            ),
          ),
          el('div', { cls: 'vp-btn-row' },
            el('button', {
              cls: 'vp-btn vp-btn-ghost',
              onclick: () => { loadCustomers().then(render).catch(()=>{}); },
            }, loading ? spinnerRow('Refreshing…') : '↻ Refresh'),
          ),
        ),
        // Right: detail
        el('div', { cls: 'vp-right-col' },
          loading
            ? skelBlock()
            : (S.selectedId ? renderCustomerDetail() : el('div', { cls: 'vp-ph' }, '← Select a customer')),
        ),
      ),
    );
  }

  async function selectCustomer(id) {
    S.selectedId = id;
    try {
      S.detail = await get('/api/customers/' + id);
    } catch(e) { showBanner(e.message, 'err'); S.detail = null; }
    render();
  }

  function renderCustomerDetail() {
    const c = S.detail;
    if (!c) return el('div', {});
    const pct = c.pct || 0;
    const barColor = c.over_quota ? 'red' : pct >= 80 ? 'amber' : 'green';

    return el('div', { cls: 'vp-card' },
      el('div', { cls: 'vp-card-title' }, (c.display_name || c.name) + '  ·  ' + (c.tier_display || 'no tier')),
      el('div', { cls: 'vp-row' },
        mCard('Used', fmtBytes(c.used_bytes), fmtPct(pct), c.over_quota ? 'red' : pct >= 80 ? 'amber' : 'green'),
        mCard('Quota', fmtBytes(c.quota_bytes), c.is_operator ? 'operator (bypass)' : 'effective limit'),
      ),
      el('div', { cls: 'vp-bar-wrap' },
        el('div', { cls: 'vp-bar-fill vp-bar-' + barColor, style: 'width:' + Math.min(100, pct) + '%' }),
      ),
      el('div', { cls: 'vp-btn-row' },
        el('button', { cls: 'vp-btn vp-btn-warn', onclick: () => doReset(c.id, c.display_name || c.name) }, '↺ Reset usage'),
      ),
      el('dl', { cls: 'vp-kv' },
        el('dt', {}, 'Status'),  el('dd', {}, c.status + (c.is_active ? ' · active' : ' · INACTIVE')),
        el('dt', {}, 'Operator'), el('dd', {}, c.is_operator ? 'yes (bypass quota)' : 'no'),
        el('dt', {}, 'Telegram'), el('dd', {}, c.telegram_username || '—'),
        el('dt', {}, 'Created'),  el('dd', { cls: 'vp-mono' }, fmtTime(c.created_at)),
        el('dt', {}, 'Updated'),  el('dd', { cls: 'vp-mono' }, fmtTime(c.updated_at)),
        c.notes ? [el('dt', {}, 'Notes'), el('dd', {}, c.notes)] : [],
      ),
      // Devices
      c.devices && c.devices.length ? [
        el('div', { cls: 'vp-card-title', style: 'margin-top:20px' }, 'Devices (' + c.devices.length + ')'),
        el('div', { cls: 'vp-tbl-wrap' },
          el('table', {},
            el('thead', {}, el('tr', {},
              el('th', {}, 'Name'), el('th', {}, 'VIP'), el('th', {}, 'Last seen'), el('th', {}, ''),
            )),
            el('tbody', {},
              ...c.devices.map(d => el('tr', {},
                el('td', { cls: 'vp-mono', 'data-label': 'Name' }, d.device_name),
                el('td', { cls: 'vp-mono', 'data-label': 'VIP' }, d.last_seen_v4 || '—'),
                el('td', { cls: 'vp-mono', 'data-label': 'Last seen' }, fmtTime(d.last_seen_at)),
                el('td', { 'data-label': 'Status' }, d.is_active ? spanBadge('active','green') : spanBadge('disabled','red')),
              ))
            ),
          ),
        ),
      ] : [],
      // Alerts
      c.alerts && c.alerts.length ? [
        el('div', { cls: 'vp-card-title', style: 'margin-top:20px' }, 'Alerts (' + c.alerts.length + ')'),
        el('div', { cls: 'vp-tbl-wrap' },
          el('table', {},
            el('thead', {}, el('tr', {},
              el('th', {}, 'Threshold'), el('th', {}, 'At'), el('th', {}, 'Bytes at alert'),
            )),
            el('tbody', {},
              ...c.alerts.map(a => el('tr', {},
                el('td', { 'data-label': 'Threshold' }, spanBadge(a.threshold + '%', a.threshold >= 100 ? 'red' : 'amber')),
                el('td', { cls: 'vp-mono', 'data-label': 'At' }, fmtTime(a.sent_at)),
                el('td', { cls: 'vp-mono', 'data-label': 'Bytes at alert' }, fmtBytes(a.data_used_bytes_at_alert)),
              ))
            ),
          ),
        ),
      ] : [],
      // Audit log
      renderAuditLog(c.audit_log || []),
    );
  }

  // ─── Audit log renderer ────────────────────────────────
  // payload is a JSON-encoded STRING. Parse + extract the meaningful bits per action.
  function parseAuditPayload(action, raw) {
    if (!raw) return { detail: '—' };
    let p;
    try { p = JSON.parse(raw); } catch { return { detail: String(raw).slice(0, 120) }; }
    switch (action) {
      case 'cut_100pct':
        return {
          icon: '🛑',
          detail: 'Hard cut: ' + fmtBytes(p.data_used) + ' / ' + fmtBytes(p.data_limit) +
                  ' (' + ((p.data_used / p.data_limit) * 100).toFixed(1) + '%)' +
                  (p.sas_terminated ? ' · ' + p.sas_terminated + ' SA(s) terminated' : ' · no active SAs'),
          kind: 'red',
        };
      case 'warn_80pct':
        return {
          icon: '⚠',
          detail: '80% warning: ' + fmtBytes(p.data_used) + ' / ' + fmtBytes(p.data_limit) +
                  ' (' + (p.pct || 0).toFixed(1) + '%)',
          kind: 'amber',
        };
      case 'reset_quota':
        return {
          icon: '↺',
          detail: 'Portal reset: cleared ' + fmtBytes(p.reset_from) + ' of usage',
          kind: 'green',
        };
      case 'reset_demo':
        return {
          icon: '↺',
          detail: 'Demo reset: cleared ' + fmtBytes(p.data_used_before) +
                  (p.data_limit_bytes_unchanged ? ' · quota unchanged' : ''),
          kind: 'green',
        };
      default:
        return {
          icon: '·',
          detail: JSON.stringify(p).slice(0, 140),
          kind: 'dim',
        };
    }
  }

  function renderAuditLog(entries) {
    if (!entries.length) {
      return el('div', { cls: 'vp-muted', style: 'margin-top:20px; font-size:12px' }, 'No audit log entries.');
    }
    // Show newest first
    const sorted = entries.slice().sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    return el('div', {},
      el('div', { cls: 'vp-card-title', style: 'margin-top:20px' }, 'Audit log (' + entries.length + ')'),
      el('div', { cls: 'vp-tbl-wrap' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, 'When'),
            el('th', {}, 'Actor'),
            el('th', {}, 'Action'),
            el('th', {}, 'Detail'),
          )),
          el('tbody', {},
            ...sorted.map(e => {
              const parsed = parseAuditPayload(e.action, e.payload);
              return el('tr', {},
                el('td', { cls: 'vp-mono', 'data-label': 'When' }, fmtTime(e.created_at)),
                el('td', { cls: 'vp-mono', 'data-label': 'Actor' }, e.actor || '—'),
                el('td', { 'data-label': 'Action' }, spanBadge(e.action, parsed.kind)),
                el('td', { 'data-label': 'Detail', style: 'font-size:12px' }, parsed.icon + ' ' + parsed.detail),
              );
            })
          ),
        ),
      ),
    );
  }

  async function doReset(id, name) {
    if (!confirm('Full reset for ' + name + '?\n\n' +
                 '• data_used_bytes → 0\n' +
                 '• over_quota → 0\n' +
                 '• Restore KILLED EAP secret (if any)\n' +
                 '• Zero iptables FORWARD counters\n' +
                 '• Clear daemon session sidecar\n\n' +
                 'iOS may need a manual VPN toggle to reconnect.')) return;
    try {
      const r = await post('/api/quota/' + id + '/reset', {});
      let msg = 'Reset ' + name + ': ' + fmtBytes(r.reset_from_bytes) + ' → 0';
      if (r.secret_restored) {
        msg += ' · EAP secret restored for ' + (r.secret_devices || []).join(', ');
      }
      showBanner(msg, 'ok');
      await loadCustomers();
      if (S.selectedId === id) S.detail = await get('/api/customers/' + id);
      render();
    } catch(e) { showBanner(e.message, 'err'); }
  }

  // ─── Tiers render ──────────────────────────────────────
  function renderTiers() {
    const loading = S.loading.tiers;
    const err     = S.loadError.tiers;
    return el('div', { cls: 'vp-page' },
      el('div', { cls: 'vp-page-head' },
        el('div', { cls: 'vp-page-title' }, 'Tiers'),
        el('div', { cls: 'vp-page-sub' }, 'Quota tiers. Schema changes go through the admin layer.'),
      ),
      err ? el('div', { cls: 'vp-empty', style: 'margin-bottom:14px; border-color: var(--red); color: var(--red)' },
              '⚠ ' + err) : null,
      loading
        ? el('div', { cls: 'vp-row' }, [skelBlock('90%', '120px'), skelBlock('90%', '120px'), skelBlock('90%', '120px'), skelBlock('90%', '120px')])
        : (S.tiers.length
            ? el('div', { cls: 'vp-row' },
                ...S.tiers.map(t => el('div', { cls: 'vp-card' },
                  el('div', { cls: 'vp-card-title' }, t.display_name),
                  el('div', { cls: 'vp-big-num' }, fmtBytes(t.quota_bytes)),
                  el('div', { cls: 'vp-muted' }, t.is_active ? 'active' : 'archived'),
                  t.notes ? el('div', { cls: 'vp-note' }, t.notes) : [],
                )),
              )
            : emptyState('∅', 'No tiers defined', 'Add tiers via SSH + sqlite, or via the admin API.'))
    );
  }

  // ─── Sessions render ───────────────────────────────────
  function renderSessions() {
    const loading    = S.loading.sessions;
    const refreshing = S.refreshing.sessions;
    const err        = S.loadError.sessions;
    const live       = !!S._sessionsTimer;
    // "loading && !refreshing" = show skeleton only on first load
    const showSkeleton = loading && !refreshing;
    return el('div', { cls: 'vp-page' },
      el('div', { cls: 'vp-page-head' },
        el('div', { cls: 'vp-page-title' },
          'Sessions',
          live ? el('span', {
            cls: 'vp-live-badge',
            title: 'Auto-refreshing every 10s while a client is connected',
          }, '● LIVE') : null),
        el('div', { cls: 'vp-page-sub' },
          live
            ? 'Auto-refreshing every 10s while a client is connected.'
            : 'Active IKE SAs and virtual-IP pool state.'),
      ),
      err ? el('div', { cls: 'vp-empty', style: 'margin-bottom:14px; border-color: var(--red); color: var(--red)' },
              '⚠ ' + err) : null,
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' }, 'Pools'),
        showSkeleton
          ? skelBlock()
          : (S.pools && S.pools.length
              ? el('dl', { cls: 'vp-kv' }, ...S.pools.flatMap(p => {
                  const active = (S.leases || []).length;
                  return [
                    el('dt', {}, p.name),
                    el('dd', { cls: 'vp-mono' },
                      p.base + ' · ' + active + ' active lease' + (active === 1 ? '' : 's')),
                  ];
                }))
              : emptyState('⊘', 'No pools', 'swanctl returned no virtual-IP pools.'))
      ),
      // Active leases with customer + device
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' },
          'Active leases (' + (S.leases && S.leases.length || 0) + ')'),
        showSkeleton
          ? skelBlock()
          : (S.leases && S.leases.length
              ? el('div', { cls: 'vp-tbl-wrap' },
                  el('table', {},
                    el('thead', {}, el('tr', {},
                      el('th', {}, 'VIP'),
                      el('th', {}, 'Customer'),
                      el('th', {}, 'Device'),
                      el('th', { cls: 'vp-tbl-lease-hide-sm' }, 'Identity'),
                      el('th', {}, 'Used'),
                      el('th', {}, 'Acquired'),
                    )),
                    el('tbody', {},
                      ...S.leases.map(lease =>
                        el('tr', {},
                          el('td', { cls: 'vp-mono', 'data-label': 'VIP' }, lease.address),
                          el('td', { 'data-label': 'Customer' },
                            lease.customer_name
                              ? el('a', {
                                  href: '#',
                                  onclick: (e) => { e.preventDefault(); switchPage('customers'); setTimeout(() => selectCustomer(lease.customer_id), 100); },
                                  style: 'color: var(--cyan); text-decoration: none',
                                }, lease.customer_name)
                              : el('span', { cls: 'dim' }, '—')),
                          el('td', { cls: 'vp-mono vp-tbl-lease-hide-sm', 'data-label': 'Device' }, lease.device_name || '—'),
                          el('td', { cls: 'vp-mono vp-tbl-lease-hide-sm', 'data-label': 'Identity' }, lease.identity_name || '—'),
                          el('td', { 'data-label': 'Used' }, usageBar(lease.data_used_bytes, lease.data_limit_bytes, lease.data_pct, lease.over_quota, lease.is_operator)),
                          el('td', { cls: 'vp-mono', 'data-label': 'Acquired' }, fmtTime(lease.acquired_at)),
                        )
                      )
                    ),
                  ),
                )
              : emptyState('○', 'No active leases', 'No clients are currently connected. They will appear here as soon as someone connects.'))
      ),
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' }, 'Active SAs (swanctl --list-sas)'),
        showSkeleton
          ? skelBlock('100%', '180px')
          : el('pre', { cls: 'vp-raw' },
              S.sessions && S.sessions.trim()
                ? S.sessions
                : '(no active SAs)')
      ),
      el('div', { cls: 'vp-btn-row' },
        el('button', {
          cls: 'vp-btn vp-btn-ghost',
          onclick: () => { loadSessions().then(render).catch(()=>{}); },
        }, (loading && !refreshing) ? spinnerRow('Refreshing…') : '↻ Refresh'),
      ),
    );
  }

  // ─── Security render ───────────────────────────────────
  function renderSecurity() {
    const dm = S.deadman || {};
    const loading = S.loading.security;
    const err     = S.loadError.security;
    return el('div', { cls: 'vp-page' },
      el('div', { cls: 'vp-page-head' },
        el('div', { cls: 'vp-page-title' }, 'Security'),
        el('div', { cls: 'vp-page-sub' }, 'ipBan bans, firewalld trusted zone, deadman status.'),
      ),
      err ? el('div', { cls: 'vp-empty', style: 'margin-bottom:14px; border-color: var(--red); color: var(--red)' },
              '⚠ ' + err) : null,
      // ipBan status
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' }, 'ipBan service'),
        loading
          ? el('div', { cls: 'vp-row' }, [skelMetric(), skelMetric()])
          : el('div', { cls: 'vp-row' },
              mCard('Service', dm.service === 'active' ? 'running' : (dm.service || '—'), '', dm.service === 'active' ? 'green' : 'red'),
              mCard('Active bans', dm.active_bans != null ? dm.active_bans : '—', '', dm.active_bans > 0 ? 'amber' : 'green'),
            ),
        !loading && dm.log_tail ? [
          el('div', { cls: 'vp-card-title', style: 'margin-top:14px' }, 'Recent log (last 8 lines)'),
          el('pre', { cls: 'vp-raw' }, dm.log_tail.split('\n').slice(-8).join('\n')),
        ] : [],
      ),
      // Whitelist
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' }, 'Firewalld trusted zone (whitelist)'),
        loading
          ? skelBlock()
          : (S.whitelist && S.whitelist.length
              ? el('div', { cls: 'vp-tbl-wrap' },
                  el('table', {},
                    el('thead', {}, el('tr', {}, el('th', {}, 'CIDR'))),
                    el('tbody', {},
                      ...S.whitelist.map(w => el('tr', {}, el('td', { cls: 'vp-mono', 'data-label': 'CIDR' }, w.cidr || w))),
                    ),
                  ),
                )
              : emptyState('∅', 'Whitelist is empty', 'Add a CIDR below to trust an entire subnet.')),
        el('div', { cls: 'vp-card-title', style: 'margin-top:14px' }, 'Add CIDR'),
        el('form', { cls: 'vp-inline-form', onsubmit: onAddWhitelist },
          el('input', { id: 'vp-cidr', cls: 'vp-inp vp-inp-mono', placeholder: '192.168.1.0/24', required: true }),
          el('button', { cls: 'vp-btn vp-btn-primary', type: 'submit' }, '+ Add'),
        ),
      ),
      // Bans
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' }, 'Banned IPs (' + (S.bans&&S.bans.length||0) + ')'),
        loading
          ? skelBlock()
          : (S.bans && S.bans.length
              ? el('div', { cls: 'vp-tbl-wrap' },
                  el('table', {},
                    el('thead', {}, el('tr', {},
                      el('th', {}, 'IP'), el('th', {}, 'Source'),
                      el('th', {}, 'Count'), el('th', {}, 'Banned at'), el('th', {}, ''),
                    )),
                    el('tbody', {},
                      ...S.bans.map(b => el('tr', {},
                        el('td', { cls: 'vp-mono', 'data-label': 'IP' }, b.ip),
                        el('td', { cls: 'vp-mono', 'data-label': 'Source' }, b.source || '—'),
                        el('td', { cls: 'vp-mono', 'data-label': 'Count' }, b.count != null ? b.count : '—'),
                        el('td', { cls: 'vp-mono', 'data-label': 'Banned at' }, fmtTime(b.ban_date)),
                        el('td', { 'data-label': '' }, el('button', { cls: 'vp-btn vp-btn-ok', onclick: () => doUnban(b.ip) }, 'Unban')),
                      ))
                    ),
                  ),
                )
              : emptyState('✓', 'No active bans', 'ipBan is not blocking anyone right now.'))
      ),
      el('div', { cls: 'vp-btn-row' },
        el('button', {
          cls: 'vp-btn vp-btn-ghost',
          onclick: () => { loadSecurity().then(render).catch(()=>{}); },
        }, loading ? spinnerRow('Refreshing…') : '↻ Refresh'),
      ),
    );
  }

  async function onAddWhitelist(e) {
    e.preventDefault();
    const cidr = document.getElementById('vp-cidr').value.trim();
    try {
      await post('/api/security/whitelist/add', { cidr });
      showBanner('Whitelisted: ' + cidr, 'ok');
      document.getElementById('vp-cidr').value = '';
      await loadSecurity();
      render();
    } catch(err) { showBanner(err.message, 'err'); }
  }

  async function doUnban(ip) {
    if (!confirm('Unban ' + ip + '?')) return;
    try {
      await post('/api/security/unban', { ip });
      showBanner('Unbanned: ' + ip, 'ok');
      await loadSecurity();
      render();
    } catch(err) { showBanner(err.message, 'err'); }
  }

  // ─── Shared UI components ──────────────────────────────
  function mCard(label, value, sub, color) {
    return el('div', { cls: 'vp-card vp-card-sm' },
      el('div', { cls: 'vp-metric', style: color ? 'color:var(--' + color + ')' : '' }, value),
      el('div', { cls: 'vp-metric-label' }, label),
      sub ? el('div', { cls: 'vp-metric-sub' }, sub) : [],
    );
  }

  function spanBadge(text, kind) {
    return el('span', { cls: 'vp-badge vp-badge-' + kind }, text);
  }

  // ─── Boot ──────────────────────────────────────────────
  async function init() {
    loadTheme();
    try {
      // Session check — if this succeeds, user is already logged in
      await get('/api/health');
      try {
        await loadDashboard();
        S.user = 'admin';
      } catch {
        S.user = null;
      }
    } catch {
      S.user = null;
    }
    render();
  }

  // Catch any unhandled render errors at window level
  window.addEventListener('error', function(e) {
    try {
      showBanner('JS Error: ' + (e.message || 'unknown'), 'err');
    } catch {}
  });

  init();

})();
