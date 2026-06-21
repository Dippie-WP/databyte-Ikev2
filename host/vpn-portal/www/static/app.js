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
  // v1.2.7.3 — show real used_bytes for operators too, just label "no cap"
// next to it. Previously the bar hid all numbers for operators, which
// made Zun's own usage invisible. Quota monitoring still bypasses for
// operators (no cut, no over_quota), but visibility is independent of that.
  function usageBar(used, limit, pct, over_quota, is_operator) {
    if (is_operator || !limit) {
      const tag = el('span', {
        cls: 'vp-usage-tag',
        title: is_operator
          ? 'Operator account — bypasses quota (no cap, but usage is still tracked)'
          : 'No data cap set for this customer',
      }, is_operator ? 'no cap' : 'no quota');
      return el('div', { cls: 'vp-usage' },
        el('div', { cls: 'vp-usage-text vp-mono' },
          fmtBytes(used) + ' \u00b7 ',
          tag,
        ),
      );
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
    // Flatten children — arrays passed as a single child arg (e.g. el('div', {}, [a, b]))
    // are also valid; the function should render their elements inline.
    // `sub ? ... : null` ternary returns null (skipped) or an array (flattened).
    const flat = children.flat(Infinity);
    for (const c of flat) {
      if (c == null) continue;
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
    // Stop customer-detail auto-refresh if leaving customers
    if (S.page === 'customers' && p !== 'customers') stopCustDetailAutoRefresh();
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
        el('div', { cls: 'vp-page-head-l' },
          el('div', { cls: 'vp-page-title' }, 'Customers'),
          el('div', { cls: 'vp-page-sub' }, 'Click a row for full detail. ↺ Reset zeroes usage.'),
        ),
        el('div', { cls: 'vp-page-head-r' },
          el('button', {
            cls: 'vp-btn vp-btn-primary',
            onclick: () => openNewClientModal(),
          }, '+ New client'),
        ),
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
                          // v1.2.7.3 — for operators, drop the "%" column value (always 0 by
                          // definition) and surface real used_bytes + "no cap" instead of
                          // "0 B / 0 B". The Usage cell now uses usageBar() for consistency
                          // with the sessions table + customer detail.
                          const isOp = c.is_operator || !c.quota_bytes;
                          return el('tr', {
                            cls: 'vp-tr' + (S.selectedId === c.id ? ' vp-tr-sel' : ''),
                            onclick: () => selectCustomer(c.id),
                          },
                            el('td', { cls: 'vp-mono', 'data-label': 'Name' }, c.display_name || c.name),
                            el('td', { 'data-label': 'Tier' }, spanBadge(c.is_operator ? 'operator' : (c.tier_display || '—'), 'dim')),
                            el('td', { 'data-label': 'Usage' },
                              usageBar(c.used_bytes, c.quota_bytes, pct, c.over_quota, c.is_operator)),
                            el('td', { cls: 'vp-mono', 'data-label': '%' }, isOp ? '—' : fmtPct(pct)),
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
    // Start the live current_session refresh (30s) while detail is open
    if (S.detail) startCustDetailAutoRefresh();
  }

  function renderCustomerDetail() {
    const c = S.detail;
    if (!c) return el('div', {});
    const pct = c.pct || 0;
    // v1.2.7.3 — for operators, force neutral color + hide the bar.
    // Operators have no cap so pct is meaningless (would always be 0 or undefined);
    // showing a 0% bar adds nothing. The "Used" card already shows real bytes.
    const noCap = c.is_operator || !c.quota_bytes;
    const barColor = noCap ? 'dim' : (c.over_quota ? 'red' : pct >= 80 ? 'amber' : 'green');
    const usedSub = noCap
      ? (c.is_operator ? 'no cap · usage tracked' : 'no quota set')
      : fmtPct(pct);

    return el('div', { cls: 'vp-card' },
      el('div', { cls: 'vp-card-title' }, (c.display_name || c.name) + '  ·  ' + (c.tier_display || 'no tier')),
      el('div', { cls: 'vp-row' },
        mCard('Used', fmtBytes(c.used_bytes), usedSub, noCap ? 'dim' : (c.over_quota ? 'red' : pct >= 80 ? 'amber' : 'green')),
        mCard('Quota', noCap ? (c.is_operator ? 'no cap' : 'no quota') : fmtBytes(c.quota_bytes), noCap ? (c.is_operator ? 'bypass' : 'unset') : 'effective limit'),
      ),
      noCap ? null : el('div', { cls: 'vp-bar-wrap' },
        el('div', { cls: 'vp-bar-fill vp-bar-' + barColor, style: 'width:' + Math.min(100, pct) + '%' }),
      ),
      el('div', { cls: 'vp-btn-row' },
        el('button', { cls: 'vp-btn vp-btn-warn', onclick: () => doReset(c.id, c.display_name || c.name) }, '↺ Reset usage'),
      ),
      el('dl', { cls: 'vp-kv' },
        el('dt', {}, 'Status'),  el('dd', {}, c.status + (c.is_active ? ' · active' : ' · INACTIVE')),
        el('dt', {}, 'Operator'), el('dd', {}, c.is_operator ? 'yes (bypass quota)' : 'no'),
        el('dt', {}, 'Telegram'), el('dd', {}, c.telegram_username || '—'),
        el('dt', {}, 'Billing ID'), el('dd', {}, c.billing_id ? [el('span', { cls: 'vp-mono' }, c.billing_id)] : '—'),
        el('dt', {}, 'Email'),     el('dd', {}, c.email ? [el('span', { cls: 'vp-mono' }, c.email)] : '—'),
        el('dt', {}, 'Created'),  el('dd', { cls: 'vp-mono' }, fmtTime(c.created_at)),
        el('dt', {}, 'Updated'),  el('dd', { cls: 'vp-mono' }, fmtTime(c.updated_at)),
        c.notes ? [el('dt', {}, 'Notes'), el('dd', {}, c.notes)] : [],
      ),
      // v1.2.7 — current session (public IP + VIP + since)
      el('div', { cls: 'vp-current-session', id: 'vp-current-session' },
        renderCurrentSession(c.current_session),
      ),
      // Devices (with metadata + edit)
      c.devices && c.devices.length ? [
        el('div', { cls: 'vp-card-title', style: 'margin-top:20px' }, 'Devices (' + c.devices.length + ')'),
        el('div', { cls: 'vp-tbl-wrap' },
          el('table', {},
            el('thead', {}, el('tr', {},
              el('th', {}, 'Name'),
              el('th', {}, 'Type'),
              el('th', {}, 'OS'),
              el('th', {}, 'Hostname'),
              el('th', {}, 'VIP'),
              el('th', {}, 'Last seen'),
              el('th', {}, ''),
            )),
            el('tbody', {},
              ...c.devices.map(d => el('tr', {},
                el('td', { cls: 'vp-mono', 'data-label': 'Name' }, d.device_name),
                el('td', { 'data-label': 'Type' }, d.device_type ? spanBadge(d.device_type, 'cyan') : el('span', { cls: 'dim' }, '—')),
                el('td', { cls: 'vp-mono', 'data-label': 'OS' }, d.os_version || '—'),
                el('td', { cls: 'vp-mono', 'data-label': 'Hostname' }, d.hostname || '—'),
                el('td', { cls: 'vp-mono', 'data-label': 'VIP' }, d.last_seen_v4 || '—'),
                el('td', { cls: 'vp-mono', 'data-label': 'Last seen' }, fmtTime(d.last_seen_at)),
                el('td', { 'data-label': '' },
                  el('button', {
                    cls: 'vp-btn-icon',
                    title: 'Edit device metadata',
                    onclick: () => openDeviceEditor(c.id, d),
                  }, '✎'),
                ),
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

  // ─── Device metadata editor ────────────
  // Inline modal — replaces the device row with editable fields, then PUTs /api/devices/{id}.
  function openDeviceEditor(customerId, d) {
    const modal = el('div', {
      cls: 'vp-modal-bg',
      onclick: (e) => { if (e.target.classList.contains('vp-modal-bg')) closeModal(); },
    },
      el('div', { cls: 'vp-modal' },
        el('div', { cls: 'vp-modal-title' },
          'Edit device: ', el('span', { cls: 'vp-mono' }, d.device_name)),
        // device_type
        el('label', { cls: 'vp-field' },
          el('span', {}, 'Device type'),
          el('input', { id: 'dev-type', type: 'text', value: d.device_type || '',
                        placeholder: 'e.g. iPhone 15 Pro, Windows 11 laptop' }),
        ),
        // os_version
        el('label', { cls: 'vp-field' },
          el('span', {}, 'OS version'),
          el('input', { id: 'dev-os', type: 'text', value: d.os_version || '',
                        placeholder: 'e.g. iOS 18.5, Windows 11 23H2, Ubuntu 24.04' }),
        ),
        // hostname
        el('label', { cls: 'vp-field' },
          el('span', {}, 'Hostname'),
          el('input', { id: 'dev-host', type: 'text', value: d.hostname || '',
                        placeholder: 'device hostname or human label' }),
        ),
        // notes
        el('label', { cls: 'vp-field' },
          el('span', {}, 'Notes'),
          el('textarea', { id: 'dev-notes', rows: '2', placeholder: 'admin notes' },
            d.notes || ''),
        ),
        el('div', { cls: 'vp-btn-row', style: 'margin-top:14px' },
          el('button', { cls: 'vp-btn', onclick: closeModal }, 'Cancel'),
          el('button', {
            cls: 'vp-btn vp-btn-primary',
            onclick: async () => {
              const payload = {};
              const t = document.getElementById('dev-type').value.trim();
              const o = document.getElementById('dev-os').value.trim();
              const h = document.getElementById('dev-host').value.trim();
              const n = document.getElementById('dev-notes').value.trim();
              if (t) payload.device_type = t;
              if (o) payload.os_version = o;
              if (h) payload.hostname = h;
              payload.notes = n; // empty string allowed (clears notes)
              try {
                await api(`/api/devices/${d.id}`, {
                  method: 'PUT',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(payload),
                });
                closeModal();
                // refresh customer detail
                await loadCustomers();
                if (S.selectedId === customerId) {
                  const fresh = (S.customers || []).find(x => x.id === customerId);
                  if (fresh) { S.detail = fresh; render(); }
                } else {
                  selectCustomer(customerId);
                }
                showBanner('Device metadata saved', 'green');
              } catch (e) {
                showBanner('Save failed: ' + (e && e.message || e), 'red');
              }
            },
          }, '💾 Save'),
        ),
      ),
    );
    document.body.appendChild(modal);
  }

  function closeModal() {
    const m = document.querySelector('.vp-modal-bg');
    if (m) m.remove();
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
      // Active leases with customer + device + live SA enrichment
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
                      el('th', {}, 'Type'),
                      el('th', {}, 'OS'),
                      el('th', {}, 'Hostname'),
                      el('th', { cls: 'vp-tbl-lease-hide-sm' }, 'Public IP'),
                      el('th', {}, 'Used'),
                      el('th', {}, 'Acquired'),
                    )),
                    el('tbody', {},
                      ...S.leases.map(lease => {
                        const dt = lease.device_type || {};
                        const typeLabel = dt.label || '—';
                        const typeBadge = dt.source === 'inferred'
                          ? spanBadge(typeLabel, 'amber')
                          : dt.source === 'manual'
                          ? spanBadge(typeLabel, 'cyan')
                          : el('span', { cls: 'dim' }, '—');
                        return el('tr', {},
                          el('td', { cls: 'vp-mono', 'data-label': 'VIP' }, lease.address),
                          el('td', { 'data-label': 'Customer' },
                            lease.customer_name
                              ? el('a', {
                                  href: '#',
                                  onclick: (e) => { e.preventDefault(); switchPage('customers'); setTimeout(() => selectCustomer(lease.customer_id), 100); },
                                  style: 'color: var(--cyan); text-decoration: none',
                                }, lease.customer_name)
                              : el('span', { cls: 'dim' }, '—')),
                          el('td', { cls: 'vp-mono', 'data-label': 'Device' }, lease.device_name || '—'),
                          el('td', { 'data-label': 'Type' }, typeBadge),
                          el('td', { cls: 'vp-mono', 'data-label': 'OS' }, lease.os_version || '—'),
                          el('td', { cls: 'vp-mono', 'data-label': 'Hostname' }, lease.hostname || '—'),
                          el('td', { cls: 'vp-mono vp-tbl-lease-hide-sm', 'data-label': 'Public IP' }, lease.public_ip || '—'),
                          el('td', { 'data-label': 'Used' }, usageBar(lease.data_used_bytes, lease.data_limit_bytes, lease.data_pct, lease.over_quota, lease.is_operator)),
                          el('td', { cls: 'vp-mono', 'data-label': 'Acquired' }, fmtTime(lease.acquired_at)),
                        );
                      })
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

  // ─── v1.2.7 — New client modal + one-shot password panel ────────────────

  // Cached tiers (loaded on first open of the modal)
  let _tiersCache = null;

  async function loadTiers() {
    if (_tiersCache) return _tiersCache;
    try {
      const rows = await get('/api/tiers');
      _tiersCache = rows.filter(t => t.is_active);
    } catch {
      _tiersCache = [];
    }
    return _tiersCache;
  }

  function openNewClientModal() {
    const modal = el('div', {
      cls: 'vp-modal-bg',
      onclick: (e) => { if (e.target.classList.contains('vp-modal-bg')) closeModal(); },
    },
      el('div', { cls: 'vp-modal vp-modal-lg' },
        el('div', { cls: 'vp-modal-title' },
          el('span', {}, '+ New client'),
          el('button', { cls: 'vp-modal-x', onclick: closeModal, title: 'Close' }, '×'),
        ),
        el('div', { id: 'vp-new-client-body' }, 'Loading…'),
      ),
    );
    document.body.appendChild(modal);
    // Focus trap & ESC
    document.addEventListener('keydown', _modalEscListener);
    renderNewClientForm();
  }

  function _modalEscListener(e) {
    if (e.key === 'Escape') closeModal();
  }

  function closeModal() {
    document.removeEventListener('keydown', _modalEscListener);
    const m = document.querySelector('.vp-modal-bg');
    if (m) m.remove();
  }

  async function renderNewClientForm() {
    const body = document.getElementById('vp-new-client-body');
    if (!body) return;
    const tiers = await loadTiers();

    const tierOptions = [
      el('option', { value: '', disabled: true, selected: true }, '— pick a tier —'),
      ...tiers.map(t => el('option', { value: t.name },
        `${t.display_name}  (${fmtBytes(t.quota_bytes)})`)),
      el('option', { value: 'custom' }, 'Custom (MiB)…'),
    ];

    body.innerHTML = '';
    body.appendChild(
      el('form', { id: 'vp-new-client-form', onsubmit: onNewClientSubmit },
        el('div', { cls: 'vp-form-grid' },
          // Customer
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Client name (slug)'),
            el('input', { id: 'vp-nc-name', cls: 'vp-inp', type: 'text', required: true,
                           placeholder: 'acme-corp', maxlength: 32,
                           pattern: '[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}',
                           'aria-describedby': 'vp-nc-name-hint' }),
            el('div', { cls: 'vp-hint', id: 'vp-nc-name-hint' },
              'URL-safe: letters, digits, dash, underscore. 1-32 chars.'),
          ),
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Display name'),
            el('input', { id: 'vp-nc-display', cls: 'vp-inp', type: 'text', required: true,
                           placeholder: 'Acme Corp', maxlength: 128 }),
          ),
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Billing ID ', el('span', { cls: 'vp-optional' }, '(optional)')),
            el('input', { id: 'vp-nc-billing', cls: 'vp-inp', type: 'text',
                           placeholder: 'INV-2026-0042', maxlength: 128 }),
          ),
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Email ', el('span', { cls: 'vp-optional' }, '(optional)')),
            el('input', { id: 'vp-nc-email', cls: 'vp-inp', type: 'email',
                           placeholder: 'ops@acme.com', maxlength: 128 }),
          ),
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Telegram ', el('span', { cls: 'vp-optional' }, '(optional)')),
            el('input', { id: 'vp-nc-tg', cls: 'vp-inp', type: 'text',
                           placeholder: '@acme', maxlength: 64 }),
          ),
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Notes ', el('span', { cls: 'vp-optional' }, '(optional)')),
            el('input', { id: 'vp-nc-notes', cls: 'vp-inp', type: 'text',
                           placeholder: 'KYC done, MDM-enrolled', maxlength: 256 }),
          ),
          // Tier
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Tier'),
            el('select', { id: 'vp-nc-tier', cls: 'vp-inp', required: true }, tierOptions),
          ),
          el('div', { cls: 'vp-field', id: 'vp-nc-custom-wrap', style: 'display:none' },
            el('label', { cls: 'vp-label' }, 'Custom cap (MiB)'),
            el('input', { id: 'vp-nc-custom-mb', cls: 'vp-inp', type: 'number',
                           min: 1, max: 1048576, placeholder: 'e.g. 1500 for 1.5 GB' }),
            el('div', { cls: 'vp-hint' }, 'Binary MiB (× 1,048,576 bytes). Tier auto-created.'),
          ),
          // Device
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Device name'),
            el('input', { id: 'vp-nc-device', cls: 'vp-inp', type: 'text', required: true,
                           placeholder: 'laptop', maxlength: 32,
                           pattern: '[a-zA-Z0-9][a-zA-Z0-9-]{0,31}',
                           'aria-describedby': 'vp-nc-device-hint vp-nc-device-warn',
                           value: 'laptop' }),
            el('div', { cls: 'vp-hint', id: 'vp-nc-device-hint' },
              'Friendly name. EAP identity will be \u201c{customer-name}-{device-name}\u201d.'),
            el('div', { cls: 'vp-field-warn', id: 'vp-nc-device-warn', style: 'display:none' }),
          ),
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Device type'),
            el('select', { id: 'vp-nc-devtype', cls: 'vp-inp', required: true },
              el('option', { value: 'iOS'     }, 'iOS'),
              el('option', { value: 'Android' }, 'Android'),
              el('option', { value: 'Windows' }, 'Windows'),
              el('option', { value: 'macOS'   }, 'macOS'),
              el('option', { value: 'Linux'   }, 'Linux'),
              el('option', { value: 'Other'   }, 'Other'),
            ),
          ),
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'OS version ', el('span', { cls: 'vp-optional' }, '(optional)')),
            el('input', { id: 'vp-nc-osver', cls: 'vp-inp', type: 'text',
                           placeholder: 'iOS 18.3.1', maxlength: 32 }),
          ),
        ),
        // Live custom-cap preview
        el('div', { id: 'vp-nc-custom-preview', cls: 'vp-custom-preview', style: 'display:none' }),
        el('div', { id: 'vp-nc-form-err', cls: 'vp-form-err', style: 'display:none' }),
        el('div', { cls: 'vp-btn-row', style: 'margin-top:18px; justify-content: flex-end' },
          el('button', { type: 'button', cls: 'vp-btn vp-btn-ghost', onclick: closeModal }, 'Cancel'),
          el('button', { type: 'submit', cls: 'vp-btn vp-btn-primary', id: 'vp-nc-submit' }, 'Create client'),
        ),
      ),
    );

    // Show/hide custom cap input + live preview
    const tierSel = body.querySelector('#vp-nc-tier');
    const customWrap = body.querySelector('#vp-nc-custom-wrap');
    const customMb = body.querySelector('#vp-nc-custom-mb');
    const preview  = body.querySelector('#vp-nc-custom-preview');
    function refresh() {
      const isCustom = tierSel.value === 'custom';
      customWrap.style.display = isCustom ? '' : 'none';
      if (isCustom) {
        const mb = parseInt(customMb.value || '0', 10);
        if (mb > 0) {
          const bytes = mb * 1048576;
          preview.style.display = '';
          preview.innerHTML = '';
          preview.appendChild(el('strong', {}, '→ New tier: '));
          preview.appendChild(document.createTextNode(
            `custom_${mb}mb_<ts> · ${fmtBytes(bytes)} (${mb} MiB)`));
        } else {
          preview.style.display = 'none';
        }
      } else {
        preview.style.display = 'none';
      }
    }
    tierSel.addEventListener('change', refresh);
    customMb.addEventListener('input', refresh);

    // Auto-derive client name from display name if not yet typed
    const nameInp  = body.querySelector('#vp-nc-name');
    const displayInp = body.querySelector('#vp-nc-display');
    const devInp = body.querySelector('#vp-nc-device');
    const devHint = body.querySelector('#vp-nc-device-hint');
    const devWarn = body.querySelector('#vp-nc-device-warn');
    const submitBtn = body.querySelector('#vp-nc-submit');

    // v1.2.7.2 — live collision warning when device_name matches or starts with
    // the customer slug. Same rule the server enforces (POST /api/customers).
    function checkDeviceCollision() {
      const c = (nameInp.value || '').trim();
      const d = (devInp.value || '').trim();
      let msg = '';
      if (!c || !d) {
        msg = '';
      } else if (d.toLowerCase() === c.toLowerCase()) {
        msg = `Will be rejected: device_name duplicates customer name (EAP identity would be "${c}-${d}"). Use a different name (e.g. iphone, laptop, pixel9).`;
      } else if (d.toLowerCase().startsWith(c.toLowerCase() + '-')) {
        msg = `Will be rejected: device_name starts with "${c}-" (EAP identity would duplicate the customer prefix). Drop the "${c}-" prefix (e.g. "${d.slice(c.length + 1)}" instead of "${d}").`;
      }
      if (msg) {
        devWarn.textContent = '\u26a0  ' + msg;
        devWarn.style.display = '';
        devInp.classList.add('vp-inp-bad');
        if (submitBtn) submitBtn.disabled = true;
      } else {
        devWarn.style.display = 'none';
        devWarn.textContent = '';
        devInp.classList.remove('vp-inp-bad');
        if (submitBtn) submitBtn.disabled = false;
      }
    }

    displayInp.addEventListener('blur', () => {
      if (!nameInp.value) {
        const slug = (displayInp.value || '').trim().toLowerCase()
          .replace(/[^a-z0-9_-]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, 32);
        if (slug) nameInp.value = slug;
        checkDeviceCollision();
      }
    });
    nameInp.addEventListener('input', checkDeviceCollision);
    devInp.addEventListener('input', checkDeviceCollision);
    // Initial check (in case form re-rendered with stale values)
    checkDeviceCollision();
  }

  async function onNewClientSubmit(ev) {
    ev.preventDefault();
    const errEl = document.getElementById('vp-nc-form-err');
    errEl.style.display = 'none';
    errEl.textContent = '';
    const submitBtn = document.getElementById('vp-nc-submit');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Creating…';

    const body = {
      name:               document.getElementById('vp-nc-name').value.trim(),
      display_name:       document.getElementById('vp-nc-display').value.trim(),
      billing_id:         document.getElementById('vp-nc-billing').value.trim() || null,
      email:              document.getElementById('vp-nc-email').value.trim() || null,
      telegram_username:  document.getElementById('vp-nc-tg').value.trim() || null,
      notes:              document.getElementById('vp-nc-notes').value.trim() || null,
      tier_name:          document.getElementById('vp-nc-tier').value,
      custom_cap_mb:      parseInt(document.getElementById('vp-nc-custom-mb').value || '0', 10) || null,
      device_name:        document.getElementById('vp-nc-device').value.trim(),
      device_type:        document.getElementById('vp-nc-devtype').value,
      os_version:         document.getElementById('vp-nc-osver').value.trim() || null,
    };
    if (body.tier_name !== 'custom') body.custom_cap_mb = null;

    try {
      const r = await post('/api/customers', body);
      // Refresh customers list in the background
      try { loadCustomers().then(render).catch(()=>{}); } catch {}
      renderOneshotPanel(r);
    } catch (e) {
      errEl.textContent = e.message || String(e);
      errEl.style.display = '';
      submitBtn.disabled = false;
      submitBtn.textContent = 'Create client';
    }
  }

  // ─── One-shot password panel (shown after successful create) ─────────────
  function renderOneshotPanel(r) {
    const c = r.customer, d = r.device;
    const eapId = r.eap_identity, pw = r.password;
    const server    = '102.182.117.43';
    const remoteId  = 'vpn.homelab.local';
    const localId   = eapId;

    function fieldRow(label, value, copy = true) {
      const row = el('div', { cls: 'vp-os-field' },
        el('div', { cls: 'vp-os-field-l' }, label),
        el('div', { cls: 'vp-os-field-r vp-mono' }, value),
      );
      if (copy && value) {
        row.appendChild(el('button', {
          type: 'button',
          cls: 'vp-btn-icon',
          title: 'Copy',
          onclick: () => {
            navigator.clipboard.writeText(String(value)).then(() => {
              showBanner('Copied ' + label, 'ok');
            }).catch(() => {});
          },
        }, '⧉'));
      }
      return row;
    }

    // Setup cards per OS
    function setupCard(os, body) {
      return el('div', { cls: 'vp-os-card' },
        el('div', { cls: 'vp-os-card-h' }, os),
        el('div', { cls: 'vp-os-card-b' }, body));
    }

    const iOS = setupCard('iOS',
      el('ol', { cls: 'vp-setup-steps' },
        el('li', {}, 'Settings → General → VPN & Device Management → Add VPN config'),
        el('li', {}, 'Type: IKEv2'),
        el('li', {}, 'Description: any (e.g. "databyte VPN")'),
        el('li', {}, 'Server: ', el('span', { cls: 'vp-mono' }, server)),
        el('li', {}, 'Remote ID: ', el('span', { cls: 'vp-mono' }, remoteId)),
        el('li', {}, 'Local ID: ', el('span', { cls: 'vp-mono' }, localId)),
        el('li', {}, 'User Authentication → Username: ', el('span', { cls: 'vp-mono' }, eapId)),
        el('li', {}, 'User Authentication → Password: ', el('span', { cls: 'vp-mono vp-pw-shown' }, pw)),
        el('li', {}, 'Tap "Done" then toggle the VPN switch'),
      ),
    );

    const Android = setupCard('Android',
      el('ol', { cls: 'vp-setup-steps' },
        el('li', {}, 'Settings → Network & internet → VPN → +'),
        el('li', {}, 'Type: IKEv2/IPSec MSCHAPv2'),
        el('li', {}, 'Name: any (e.g. "databyte VPN")'),
        el('li', {}, 'Server: ', el('span', { cls: 'vp-mono' }, server)),
        el('li', {}, 'IPSec identifier: ', el('span', { cls: 'vp-mono' }, remoteId)),
        el('li', {}, 'Username: ', el('span', { cls: 'vp-mono' }, eapId)),
        el('li', {}, 'Password: ', el('span', { cls: 'vp-mono vp-pw-shown' }, pw)),
        el('li', {}, 'Save → tap to connect'),
      ),
    );

    const Windows = setupCard('Windows',
      el('ol', { cls: 'vp-setup-steps' },
        el('li', {}, 'Settings → Network & internet → VPN → Add a VPN connection'),
        el('li', {}, 'VPN provider: Windows (built-in)'),
        el('li', {}, 'Connection name: any (e.g. "databyte VPN")'),
        el('li', {}, 'Server name or address: ', el('span', { cls: 'vp-mono' }, server)),
        el('li', {}, 'VPN type: IKEv2'),
        el('li', {}, 'Type of sign-in info: User name and password'),
        el('li', {}, 'User name: ', el('span', { cls: 'vp-mono' }, eapId)),
        el('li', {}, 'Password: ', el('span', { cls: 'vp-mono vp-pw-shown' }, pw)),
        el('li', {}, 'Save → connect from network flyout'),
        el('li', {}, 'If asked for "Remember my sign-in info": NO'),
      ),
    );

    const macOS = setupCard('macOS',
      el('ol', { cls: 'vp-setup-steps' },
        el('li', {}, 'System Settings → Network → + → Interface: VPN, VPN Type: IKEv2'),
        el('li', {}, 'Service Name: any (e.g. "databyte VPN")'),
        el('li', {}, 'Server Address: ', el('span', { cls: 'vp-mono' }, server)),
        el('li', {}, 'Remote ID: ', el('span', { cls: 'vp-mono' }, remoteId)),
        el('li', {}, 'Local ID: ', el('span', { cls: 'vp-mono' }, localId)),
        el('li', {}, 'Authentication Settings → Username: ', el('span', { cls: 'vp-mono' }, eapId)),
        el('li', {}, 'Authentication Settings → Password: ', el('span', { cls: 'vp-mono vp-pw-shown' }, pw)),
        el('li', {}, 'Connect'),
      ),
    );

    const Linux = setupCard('Linux (NetworkManager)',
      el('ol', { cls: 'vp-setup-steps' },
        el('li', {}, 'Install strongswan / NetworkManager-strongswan / strongswan-nm'),
        el('li', {}, 'Add connection: nmcli connection add type vpn vpn-type org.freedesktop.NetworkManager.strongswan'),
        el('li', {}, 'Set: gateway = ', el('span', { cls: 'vp-mono' }, server)),
        el('li', {}, 'Set: address = 10.99.0.0/24, 0.0.0.0/0 (split tunnel optional)'),
        el('li', {}, 'Set: remote-id = ', el('span', { cls: 'vp-mono' }, remoteId)),
        el('li', {}, 'Set: local-id  = ', el('span', { cls: 'vp-mono' }, localId)),
        el('li', {}, 'Set: user = ', el('span', { cls: 'vp-mono' }, eapId)),
        el('li', {}, 'Set: user-password = ', el('span', { cls: 'vp-mono vp-pw-shown' }, pw)),
        el('li', {}, 'nmcli connection up <name>'),
      ),
    );

    const body = document.getElementById('vp-new-client-body');
    if (!body) return;
    body.innerHTML = '';

    body.appendChild(
      el('div', {},
        // Warning banner
        el('div', { cls: 'vp-oneshot-warn' },
          el('span', { cls: 'vp-oneshot-warn-icon' }, '⚠'),
          el('strong', {}, 'SAVE THESE NOW — '),
          'the password is shown ONCE. Copy or send to the client before closing.'),
        // Summary
        el('div', { cls: 'vp-oneshot-summary' },
          el('div', {}, 'Created customer ', el('strong', {}, c.display_name),
            ' (id=', String(c.id), ', slug=', el('span', { cls: 'vp-mono' }, c.name), '), tier: ',
            el('span', { cls: 'vp-mono' }, c.tier || '(unknown)')),
          c.billing_id ? el('div', {}, 'Billing ID: ', el('span', { cls: 'vp-mono' }, c.billing_id)) : null,
          c.email ? el('div', {}, 'Email: ', el('span', { cls: 'vp-mono' }, c.email)) : null,
        ),
        // Core fields
        el('div', { cls: 'vp-os-card vp-os-card-core' },
          el('div', { cls: 'vp-os-card-h' }, 'VPN connection details'),
          el('div', { cls: 'vp-os-card-b vp-os-fields' },
            fieldRow('Server', server),
            fieldRow('Remote ID', remoteId),
            fieldRow('Local ID', localId),
            fieldRow('Username (EAP identity)', eapId),
            fieldRow('Password (one-shot)', pw, true),
          ),
        ),
        // Per-OS setup cards
        el('div', { cls: 'vp-os-grid' }, iOS, Android, Windows, macOS, Linux),
        // v1.2.7.2 — share/copy buttons. Web Share API on mobile (native
        // share sheet → WhatsApp, Telegram, SMS, email, etc.), copy-all
        // fallback on desktop. Text content built below.
        renderShareControls({ c, d, eapId, pw, server, remoteId }),
        // Footer
        el('div', { cls: 'vp-btn-row', style: 'margin-top:20px; justify-content: flex-end' },
          el('button', { type: 'button', cls: 'vp-btn vp-btn-primary', onclick: closeModal }, 'Done'),
        ),
      ),
    );
  }

  // ─── v1.2.7.2 — Share / copy-all controls on the one-shot panel ──────
  // Web Share API where available (Android Chrome, iOS Safari, etc.) gives
  // the native share sheet — WhatsApp, Telegram, SMS, email, etc.
  // Desktop browsers fall back to a single "Copy all" button.
  function buildShareText({ c, d, eapId, pw, server, remoteId }) {
    const name = c.display_name || c.name;
    return [
      '\ud83d\udd10 databyte VPN \u2014 ' + name,
      '\u2501'.repeat(28),
      'Server:    ' + server,
      'Remote ID: ' + remoteId,
      'Local ID:  ' + eapId,
      'Username:  ' + eapId,
      'Password:  ' + pw,
      '',
      'Setup (iOS): Settings \u2192 General \u2192 VPN & Device Management',
      '\u2192 Add VPN config \u2192 Type: IKEv2 \u2192 paste the values above.',
      '',
      'Setup (Android): Settings \u2192 Network \u2192 VPN \u2192 +',
      '\u2192 IKEv2/IPSec MSCHAPv2 \u2192 paste the values above.',
      '',
      '\u26a0  Save this message \u2014 the password is shown ONCE.',
    ].join('\n');
  }

  function renderShareControls(ctx) {
    const text = buildShareText(ctx);
    const canShare = typeof navigator !== 'undefined'
      && navigator.share
      && typeof navigator.canShare === 'function'
      && navigator.canShare({ text });

    const btnRow = el('div', {
      cls: 'vp-btn-row',
      style: 'margin-top:18px; gap:8px; flex-wrap:wrap;',
    });

    if (canShare) {
      btnRow.appendChild(el('button', {
        type: 'button',
        cls: 'vp-btn vp-btn-primary',
        onclick: async () => {
          try {
            await navigator.share({
              title: 'databyte VPN \u2014 ' + (ctx.c.display_name || ctx.c.name),
              text,
            });
            showBanner('Shared', 'ok');
          } catch (e) {
            // User cancelled or share failed \u2014 fall back silently.
            if (e && e.name !== 'AbortError') {
              try { await navigator.clipboard.writeText(text); showBanner('Share failed \u2014 copied to clipboard', 'ok'); }
              catch (_) { showBanner('Share failed', 'err'); }
            }
          }
        },
      }, '\u2197  Share to WhatsApp / Telegram / etc.'));
    }

    // Always-available fallback: "Copy all" \u2014 copies the same text.
    btnRow.appendChild(el('button', {
      type: 'button',
      cls: canShare ? 'vp-btn vp-btn-ghost' : 'vp-btn vp-btn-primary',
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          showBanner('Copied full config to clipboard', 'ok');
        } catch (_) {
          // Last-resort fallback for very old browsers without Clipboard API
          const ta = document.createElement('textarea');
          ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.select();
          try { document.execCommand('copy'); showBanner('Copied full config to clipboard', 'ok'); }
          catch (_) { showBanner('Copy failed \u2014 select text manually', 'err'); }
          finally { document.body.removeChild(ta); }
        }
      },
    }, canShare ? '\u29c9  Copy all' : '\u29c9  Copy full config'));

    return btnRow;
  }

  // ─── v1.2.7 — renderCustomerDetail: show billing_id, email, current_session ──

  function renderCurrentSession(sess) {
    if (!sess || !sess.public_ip) {
      return el('div', { cls: 'vp-cs-empty' },
        el('span', { cls: 'vp-cs-dot vp-cs-dot-off' }),
        el('span', { cls: 'vp-cs-label' }, 'No active session'),
      );
    }
    const sinceStr = sess.since ? fmtTime(sess.since) + ' (' + relTime(sess.since) + ')' : '?';
    return el('div', { cls: 'vp-cs-on' },
      el('div', { cls: 'vp-cs-head' },
        el('span', { cls: 'vp-cs-dot vp-cs-dot-on' }),
        el('span', { cls: 'vp-cs-label' }, 'Active session'),
      ),
      el('dl', { cls: 'vp-cs-grid' },
        el('dt', {}, 'Public IP'),
        el('dd', { cls: 'vp-mono' }, sess.public_ip + (sess.remote_port ? ':' + sess.remote_port : '')),
        el('dt', {}, 'VPN IP'),
        el('dd', { cls: 'vp-mono' }, sess.vip || '—'),
        el('dt', {}, 'Device'),
        el('dd', { cls: 'vp-mono' }, sess.device || '—'),
        el('dt', {}, 'Connected'),
        el('dd', { cls: 'vp-mono' }, sinceStr),
        sess.ike ? [el('dt', {}, 'IKE proposal'), el('dd', { cls: 'vp-mono' }, sess.ike)] : [],
        sess.sa_bytes_in != null || sess.sa_bytes_out != null ? [
          el('dt', {}, 'This session'),
          el('dd', { cls: 'vp-mono' },
            '↓ ' + fmtBytes(sess.sa_bytes_in || 0) + ' · ↑ ' + fmtBytes(sess.sa_bytes_out || 0)),
        ] : [],
      ),
    );
  }

  function relTime(epoch) {
    if (!epoch) return '?';
    const secs = Math.floor(Date.now() / 1000) - epoch;
    if (secs < 60) return secs + 's ago';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
    return Math.floor(secs / 86400) + 'd ago';
  }

  // v1.2.7 — live refresh of customer detail (current_session updates)
  let _custDetailTimer = null;
  function startCustDetailAutoRefresh() {
    if (_custDetailTimer) return;
    _custDetailTimer = setInterval(async () => {
      if (S.page !== 'customers' || !S.selectedId) { stopCustDetailAutoRefresh(); return; }
      try {
        const d = await get('/api/customers/' + S.selectedId);
        S.detail = d;
        const el2 = document.getElementById('vp-current-session');
        if (el2) {
          el2.innerHTML = '';
          el2.appendChild(renderCurrentSession(d.current_session));
        }
      } catch { /* ignore */ }
    }, 30000);
  }
  function stopCustDetailAutoRefresh() {
    if (_custDetailTimer) { clearInterval(_custDetailTimer); _custDetailTimer = null; }
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
