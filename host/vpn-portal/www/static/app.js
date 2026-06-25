// 2026-06-25 live-pool-leases-integration-v2

// 2026-06-25 live-pool-leases-integration v1.4.6

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
    // v1.2.13 — bulk selection (Set of customer IDs)
    bulkSelected: new Set(),
    // v1.2.13 — when filter/search changes, drop selection of vanished rows
    bulkAnchor: null,  // shift-click anchor for range select
    // v1.2.14 — column sort {by, dir}. Default: name asc.
    custSort: { by: 'name', dir: 'asc' },
    // v1.2.14 — live active-session counts {customer_id: count} from swanctl
    activeSessions: {},
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
  // v1.4.0 — width/height are passed via CSS custom properties (CSSOM-set, CSP-safe).
  function skel(cls, w, h) {
    const c = 'vp-skel ' + (cls || 'vp-skel-line');
    const cssVars = {};
    if (w) cssVars.skelW = w;
    if (h) cssVars.skelH = h;
    return el('span', { cls: c, cssVars }, '\u00a0');
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
  const patch = (p, b) => api(p, { method: 'PATCH', body: b });
  const del  = p  => api(p, { method: 'DELETE' });

  // v1.2.12 — toast (top-right floating notification)
  function toast(msg, kind) {
    const t = el('div', { cls: 'vp-toast vp-toast-' + (kind || 'ok') }, msg);
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add('vp-toast-show'));
    setTimeout(() => {
      t.classList.remove('vp-toast-show');
      setTimeout(() => t.remove(), 250);
    }, 3000);
  }

  // v1.2.12 — $ shorthand for getElementById
  const $ = id => document.getElementById(id);

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
    const clampedPct = Math.min(100, Math.max(0, pct));
    return el('div', { cls: 'vp-usage' },
      el('div', { cls: 'vp-usage-track' },
        el('div', {
          cls: 'vp-usage-fill',
          cssVars: { pct: clampedPct + '%', 'bar-color': barColor },
        }),
      ),
      el('div', {
        cls: 'vp-usage-text vp-mono',
        cssVars: { 'bar-color': barColor },
      }, fmtBytes(used) + ' / ' + fmtBytes(limit) + ' (' + pct.toFixed(1) + '%)'),
    );
  }
  function fmtTime(e) {
    if (!e) return '—';
    return new Date(e * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC');
  }

  // ─── DOM helpers ───────────────────────────────────────
  // el('div', {cls, cssVars, ...attrs}, children...)
  // v1.4.0: `style:` is REJECTED — strict CSP blocks inline style attributes.
  //   Use either:
  //     - a className:  el('div', { cls: 'vp-mt-20 vp-empty-err' }, 'oops')
  //     - cssVars:      el('div', { cls: 'vp-bar-fill', cssVars: { pct: 50 } })
  //       which calls el.style.setProperty('--pct', '50') (CSSOM API,
  //       allowed by strict CSP per W3C CSP3 — only inline-style *attributes*
  //       are blocked, not CSSOM custom-property sets).
  //   Trying to pass style: throws to catch regressions early.
  //
  // Note on `el.style.setProperty`: it is the CSSOM API, distinct from a CSS
  // `style` attribute. CSP `style-src` (per W3C CSP3 §6.7.3.1) only blocks
  // inline style *attributes* and `<style>` elements; CSSOM property writes
  // — including custom property writes — are explicitly allowed. Verified
  // against Chrome 119+ behavior.
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'cls') e.className = v;
      else if (k === 'html') e.innerHTML = v;
      else if (k === 'cssVars' && v) {
        // Set CSS custom properties (CSP-safe via CSSOM)
        for (const [prop, val] of Object.entries(v)) {
          e.style.setProperty('--' + prop, val);
        }
      }
      else if (k === 'style') {
        // HARD REJECTION: inline style attribute violates strict CSP.
        // Throws to fail loud in dev — silent fallback would hide a security regression.
        throw new Error('el(): style: key forbidden by strict CSP — use cls: or cssVars: instead');
      }
      else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v === false || v == null) {
        // skip — null/undefined means absent; false means absent for boolean attrs
        // (e.g. `selected: false` should NOT set selected="false" on an option)
        continue;
      } else if (k === 'selected' || k === 'disabled' || k === 'checked' || k === 'readonly' || k === 'required' || k === 'multiple' || k === 'hidden' || k === 'autofocus') {
        // Boolean attributes: set the IDL property to ensure correct behavior on
        // form controls (option.selected, input.checked, etc.). The HTML attribute
        // is also set so the markup round-trips.
        try { e[k] = true; } catch {}
        e.setAttribute(k, '');
      } else {
        e.setAttribute(k, v);
      }
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
    // v1.4.0 — classList toggle replaces .style.display to keep strict CSP clean.
    b.classList.remove('vp-hidden');
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(() => { b.classList.add('vp-hidden'); }, 4500);
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
    const errEl = el('div', { id: 'vp-login-err', cls: 'vp-login-err vp-hidden' });
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
    errEl.classList.add('vp-hidden');
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
      errEl.classList.remove('vp-hidden');
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
    // On VPS (VPN_HOST=127.0.0.1), the security tab is not relevant — we use OS iptables + fail2ban,
    // not ipBan/firewalld. Hide the tab rather than show ipban-only endpoints that always fail.
    const isVps = S.health && S.health.vpn_host === '127.0.0.1';
    const tabs = isVps
      ? ['dashboard','customers','tiers','sessions']
      : ['dashboard','customers','tiers','sessions','security'];
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
    customers: async () => { await loadCustomers(); await loadActiveSessions(); },
    tiers:     loadTiers,
    sessions:  loadSessions,
    security:  loadSecurity,
  };

  // v1.2.14 — periodic active-sessions refresh (30s) while on customers page.
  // Started/stopped with the page so we don't hammer the API when on other tabs.
  let _activeSessTimer = null;
  function startActiveSessAutoRefresh() {
    stopActiveSessAutoRefresh();
    _activeSessTimer = setInterval(() => {
      if (S.page !== 'customers') return;
      loadActiveSessions().then(render).catch(() => {});
    }, 30000);
  }
  function stopActiveSessAutoRefresh() {
    if (_activeSessTimer) { clearInterval(_activeSessTimer); _activeSessTimer = null; }
  }

  // v1.6.3 — Dashboard auto-refresh (30s) so live data (Total data, Over quota
  // counts, Pools active leases) actually moves while operator is on the page.
  // Bug: dashboard loaded once on tab-open and never polled again, leaving
  // operators looking at a frozen view even when customers were actively
  // burning bandwidth. Hidden for weeks behind a JS SyntaxError that broke
  // the portal entirely — caught 2026-06-25 by Zun after the SyntaxError fix.
  let _dashboardTimer = null;
  function startDashboardAutoRefresh() {
    stopDashboardAutoRefresh();
    _dashboardTimer = setInterval(() => {
      if (S.page !== 'dashboard') return;
      loadDashboard().then(render).catch(() => {});
    }, 30000);
  }
  function stopDashboardAutoRefresh() {
    if (_dashboardTimer) { clearInterval(_dashboardTimer); _dashboardTimer = null; }
  }

  async function switchPage(p) {
    if (!LOADERS[p]) return;
    if (S.loading[p]) return;  // already in flight
    // Stop Sessions auto-refresh if we're leaving it
    if (S.page === 'sessions' && p !== 'sessions') stopSessionsAutoRefresh();
    // Stop customer-detail auto-refresh if leaving customers
    if (S.page === 'customers' && p !== 'customers') stopCustDetailAutoRefresh();
    // v1.6.3 — start/stop dashboard poll when entering/leaving dashboard tab
    if (p === 'dashboard' && S.page !== 'dashboard') startDashboardAutoRefresh();
    else if (p !== 'dashboard' && S.page === 'dashboard') stopDashboardAutoRefresh();
    // v1.2.14 — start/stop active-sessions poll when entering/leaving customers
    if (p === 'customers' && S.page !== 'customers') startActiveSessAutoRefresh();
    else if (p !== 'customers' && S.page === 'customers') stopActiveSessAutoRefresh();
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
      const params = new URLSearchParams();
      if (S.custSort && S.custSort.by) {
        params.set('sort_by', S.custSort.by);
        params.set('sort_dir', S.custSort.dir || 'asc');
      }
      const url = '/api/customers' + (params.toString() ? '?' + params.toString() : '');
      const [cust, tiers] = await Promise.all([
        get(url),
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

  // v1.2.14 — load active session counts from live swanctl --list-sas
  async function loadActiveSessions() {
    try {
      const r = await get('/api/customers/active-sessions');
      S.activeSessions = r.counts || {};
    } catch (e) {
      // Don't fail the whole page on this — just leave stale data
      log('active-sessions load failed:', e.message);
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
      err ? el('div', { cls: 'vp-empty vp-empty-err' },
              '⚠ ' + err) : null,
      // 4 metric cards (skeletons while loading)
      el('div', { cls: 'vp-row' },
        loading
          ? [skelMetric(), skelMetric(), skelMetric(), skelMetric()]
          : [
              mCard('Service', h.status === 'ok' ? 'OK' : (h.status || '—'), h.status === 'ok' ? 'green' : 'red'),
              mCard('Database', h.db_ok ? 'connected' : 'DOWN',
                    h.db_customers != null ? h.db_customers + ' customers' : '',
                    h.db_ok ? 'green' : 'red'),
              mCard('charon', h.charon_ok ? 'reachable' : 'DOWN', h.charon_ok ? 'green' : 'red', 'vici @ ' + (h.vpn_host || 'gateway')),
              // On VPS (VPN_HOST=127.0.0.1) we use OS iptables + fail2ban instead of ipban.
              // Show 'OS firewall' with status from /api/health-derived info rather than ipban.
              h.vpn_host === '127.0.0.1'
                ? mCard('OS firewall', 'active', 'green', 'iptables + fail2ban')
                : mCard('ipBan', dm.service === 'active' ? 'active' : '—', dm.service === 'active' ? 'green' : 'amber',
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
                  const active = (S.leases || []).filter(l => l.online).length;
                  return [
                    el('dt', {}, p.name),
                    el('dd', { cls: 'vp-mono' },
                      p.base + ' · ' + active + ' active lease' + (active === 1 ? '' : 's')),
                  ];
                }))
              : emptyState('⊘', 'No pools loaded', 'swanctl returned no virtual-IP pools. Check strongswan is running on ' + (h.vpn_host || 'gateway') + '.'))
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
    S.custSearch = S.custSearch || '';
    S.custFilter = S.custFilter || 'all';  // all | over | near | operator | archived

    // v1.2.13 — drop bulk-selected IDs that no longer exist (e.g., filter changed, deleted)
    const validIds = new Set((S.customers || []).map(c => c.id));
    for (const id of [...S.bulkSelected]) {
      if (!validIds.has(id)) S.bulkSelected.delete(id);
    }

    function skelRow() {
      return el('tr', {},
        el('td', {}, skelLine('20%')),
        el('td', {}, skelLine('80%')),
        el('td', {}, skelLine('50%')),
        el('td', {}, skelLine('90%')),
        el('td', {}, skelLine('40%')),
        el('td', {}, skelLine('20%')),
        el('td', {}, skelLine('40%')),
        el('td', {}, skelLine('20%')),
      );
    }

    function applyFilter(rows) {
      const q = S.custSearch.trim().toLowerCase();
      return rows.filter(c => {
        if (q) {
          const hay = (c.display_name || '') + ' ' + (c.name || '') + ' ' + (c.email || '') + ' ' + (c.billing_id || '');
          if (!hay.toLowerCase().includes(q)) return false;
        }
        if (S.custFilter === 'over' && !c.over_quota) return false;
        if (S.custFilter === 'near' && !(c.pct >= 80 && !c.over_quota)) return false;
        if (S.custFilter === 'operator' && !c.is_operator) return false;
        if (S.custFilter === 'archived' && c.status !== 'archived') return false;
        return true;
      });
    }

    const allRows = S.customers || [];
    const filteredRows = applyFilter(allRows);

    return el('div', { cls: 'vp-page' },
      el('div', { cls: 'vp-page-head' },
        el('div', { cls: 'vp-page-head-l' },
          el('div', { cls: 'vp-page-title' }, 'Customers'),
          el('div', { cls: 'vp-page-sub' },
            'Click a row for full detail. ↺ Reset zeroes usage. '
            + (S.custFilter === 'archived' ? 'Showing archived only — click "Active" to restore view.' : '')
          ),
        ),
        el('div', { cls: 'vp-page-head-r' },
          // v1.6.6 — Refresh button moved to top next to +New client.
          // Was at the bottom of the table; Zun asked 2026-06-25 to put it
          // on top so it's visible without scrolling past the full list.
          el('button', {
            cls: 'vp-btn vp-btn-ghost',
            onclick: () => { loadCustomers().then(render).catch(()=>{}); },
          }, loading ? spinnerRow('Refreshing…') : '↻ Refresh'),
          el('button', {
            cls: 'vp-btn vp-btn-primary',
            onclick: () => openNewClientModal(),
          }, '+ New client'),
        ),
      ),
      err ? el('div', { cls: 'vp-empty vp-empty-err' },
              '⚠ ' + err) : null,
      // v1.2.12 — search + filter bar
      el('div', { cls: 'vp-toolbar' },
        el('input', {
          type: 'search',
          cls: 'vp-search',
          placeholder: 'Search name / email / billing ID…',
          value: S.custSearch,
          'data-label': 'Search customers',
          oninput: (ev) => { S.custSearch = ev.target.value; render(); },
        }),
        el('div', { cls: 'vp-pills' },
          pillBtn('all', 'All', S.custFilter === 'all', () => { S.custFilter = 'all'; render(); }),
          pillBtn('over', 'Over quota', S.custFilter === 'over', () => { S.custFilter = 'over'; render(); }),
          pillBtn('near', 'Near cap (≥80%)', S.custFilter === 'near', () => { S.custFilter = 'near'; render(); }),
          pillBtn('operator', 'Operators', S.custFilter === 'operator', () => { S.custFilter = 'operator'; render(); }),
          pillBtn('archived', 'Archived', S.custFilter === 'archived', () => { S.custFilter = 'archived'; render(); }),
        ),
        el('div', { cls: 'vp-toolbar-meta' },
          filteredRows.length === allRows.length
            ? `${allRows.length} customer${allRows.length === 1 ? '' : 's'}`
            : `${filteredRows.length} of ${allRows.length} shown`,
        ),
      ),
      // v1.2.13 — bulk action bar (only when ≥1 row selected)
      S.bulkSelected.size > 0 ? el('div', { cls: 'vp-bulk-bar' },
        el('div', { cls: 'vp-bulk-bar-l' },
          el('span', { cls: 'vp-bulk-count' }, `${S.bulkSelected.size} selected`),
          el('button', { cls: 'vp-btn-link', onclick: () => { S.bulkSelected.clear(); render(); } }, 'Clear'),
        ),
        el('div', { cls: 'vp-bulk-bar-r' },
          el('button', {
            cls: 'vp-btn vp-btn-warn',
            onclick: () => doBulkArchive(),
          }, '↥ Archive'),
          el('button', {
            cls: 'vp-btn vp-btn-ok',
            onclick: () => doBulkUnarchive(),
          }, '↧ Unarchive'),
          el('div', { cls: 'vp-bulk-tier-wrap' },
            el('button', {
              cls: 'vp-btn vp-btn-primary',
              onclick: () => doBulkChangeTier(),
            }, '⇄ Change tier'),
          ),
          el('button', {
            cls: 'vp-btn vp-btn-danger',
            onclick: () => doBulkDelete(),
          }, '🗑 Delete'),
        ),
      ) : null,
      el('div', { cls: 'vp-row-2' },
        // Left: table
        el('div', { cls: 'vp-left-col' },
          el('div', { cls: 'vp-tbl-wrap' },
            el('table', {},
              el('thead', {}, el('tr', {},
                el('th', { cls: 'vp-th-check' },
                  (() => {
                    const visibleIds = filteredRows.filter(c => !c.is_operator).map(c => c.id);
                    const allSelected = visibleIds.length > 0 && visibleIds.every(id => S.bulkSelected.has(id));
                    return el('input', {
                      type: 'checkbox',
                      cls: 'vp-check',
                      title: 'Select all visible (non-operator)',
                      checked: allSelected,
                      onchange: (ev) => {
                        if (ev.target.checked) {
                          visibleIds.forEach(id => S.bulkSelected.add(id));
                        } else {
                          visibleIds.forEach(id => S.bulkSelected.delete(id));
                        }
                        render();
                      },
                    });
                  })()
                ),
                sortHeader('Name',    'name',    S.custSort),
                sortHeader('Tier',    'tier',    S.custSort),
                sortHeader('Usage',   'usage',   S.custSort),
                el('th', {}, '%'),
                el('th', {}, 'Active'),
                el('th', {}, 'State'),
                el('th', {}, ''),
              )),
              el('tbody', {},
                ...(
                  loading
                    ? [skelRow(), skelRow(), skelRow()]
                    : filteredRows.length
                      ? filteredRows.map(c => {
                          const pct = c.pct || 0;
                          const archived = c.status === 'archived';
                          const state = archived ? ['ARCH','dim'] : c.over_quota ? ['CUT','red'] : pct >= 80 ? ['NEAR','amber'] : ['OK','green'];
                          const isOp = c.is_operator || !c.quota_bytes;
                          const selected = S.bulkSelected.has(c.id);
                          return el('tr', {
                            cls: 'vp-tr' + (S.selectedId === c.id ? ' vp-tr-sel' : '') + (archived ? ' vp-tr-archived' : '') + (selected ? ' vp-tr-bulk' : ''),
                            onclick: () => selectCustomer(c.id),
                          },
                            el('td', { cls: 'vp-td-check', onclick: (ev) => ev.stopPropagation() },
                              isOp
                                ? el('span', { cls: 'vp-check-disabled', title: 'Operators cannot be bulk-edited' }, '⊘')
                                : el('input', {
                                    type: 'checkbox',
                                    cls: 'vp-check',
                                    checked: selected,
                                    onchange: (ev) => {
                                      if (ev.target.checked) S.bulkSelected.add(c.id);
                                      else S.bulkSelected.delete(c.id);
                                      render();
                                    },
                                  })
                            ),
                            el('td', { cls: 'vp-mono', 'data-label': 'Name' }, c.display_name || c.name),
                            el('td', { 'data-label': 'Tier' }, spanBadge(c.is_operator ? 'operator' : (c.tier_display || '—'), 'dim')),
                            el('td', { 'data-label': 'Usage' },
                              usageBar(c.used_bytes, c.quota_bytes, pct, c.over_quota, c.is_operator)),
                            el('td', { cls: 'vp-mono', 'data-label': '%' }, isOp ? '—' : fmtPct(pct)),
                            // v1.2.14 — Active column: live green dot when ≥1 SA is connected
                            (() => {
                              const count = S.activeSessions[c.id] || 0;
                              return el('td', { 'data-label': 'Active', cls: 'vp-td-active' },
                                count > 0
                                  ? el('span', { cls: 'vp-active-dot', title: `${count} active session${count === 1 ? '' : 's'}` }, count)
                                  : el('span', { cls: 'vp-active-dot-off', title: 'No active sessions' }, '·'),
                              );
                            })(),
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
                      : [el('tr', {}, el('td', { colspan: 8, cls: 'vp-no-pad' },
                            emptyState('∅',
                              S.custSearch || S.custFilter !== 'all'
                                ? 'No customers match this filter'
                                : 'No customers yet',
                              S.custSearch || S.custFilter !== 'all'
                                ? 'Clear the search box or pick another pill.'
                                : 'Add a customer via SSH + sqlite, or via the admin API.')))]
                )
              ),
            ),
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
        el('div', {
          cls: 'vp-bar-fill vp-bar-' + barColor,
          cssVars: { pct: Math.min(100, Math.max(0, pct)) + '%' },
        }),
      ),
      el('div', { cls: 'vp-btn-row' },
        el('button', { cls: 'vp-btn vp-btn-warn', onclick: () => doReset(c.id, c.display_name || c.name) }, '↺ Reset usage'),
        c.is_operator ? null : el('button', {
          cls: 'vp-btn vp-btn-ghost',
          onclick: () => openEditCustomerModal(c),
          'data-label': 'Edit customer',
        }, '✎ Edit'),
        c.is_operator ? null : el('button', {
          cls: 'vp-btn vp-btn-primary',
          onclick: () => generateInstallerLink(c),
          'data-label': 'Generate one-time installer link (7-day expiry)',
        }, '🔗 Installer'),
        c.is_operator ? null : (c.status === 'archived'
          ? el('button', { cls: 'vp-btn vp-btn-ghost', onclick: () => doUnarchive(c.id) }, '↩ Unarchive')
          : el('button', { cls: 'vp-btn vp-btn-ghost', onclick: () => doArchive(c.id, c.display_name || c.name) }, '🗄 Archive')),
        c.is_operator ? null : el('button', { cls: 'vp-btn vp-btn-danger', onclick: () => doDelete(c.id, c.name) }, '✕ Delete'),
      ),
      el('dl', { cls: 'vp-kv' },
        el('dt', {}, 'Status'),  el('dd', {}, c.status + (c.is_active ? ' · active' : ' · INACTIVE')),
        el('dt', {}, 'Operator'), el('dd', {}, c.is_operator ? 'yes (bypass quota)' : 'no'),
        // v1.6.4 — show allocated bandwidth. Was being applied by tc but invisible
        // to operators in the detail view. Zun noticed 2026-06-25 while watching
        // zade's session and asked "i dont see the allocated speed for this user".
        el('dt', {}, 'Bandwidth'), el('dd', { cls: 'vp-mono' },
          (c.bandwidth_down_mbps || 0) + ' Mbit/s down · ' +
          (c.bandwidth_up_mbps || 0) + ' Mbit/s up'),
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
        el('div', { cls: 'vp-card-title vp-mt-20' }, 'Devices (' + c.devices.length + ')'),
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
        el('div', { cls: 'vp-card-title vp-mt-20' }, 'Alerts (' + c.alerts.length + ')'),
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
        el('div', { cls: 'vp-btn-row vp-mt-14' },
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
      return el('div', { cls: 'vp-muted vp-mt-20 vp-fs-12' }, 'No audit log entries.');
    }
    // Show newest first
    const sorted = entries.slice().sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    return el('div', {},
      el('div', { cls: 'vp-card-title vp-mt-20' }, 'Audit log (' + entries.length + ')'),
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
                el('td', { 'data-label': 'Detail', cls: 'vp-fs-12' }, parsed.icon + ' ' + parsed.detail),
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
      err ? el('div', { cls: 'vp-empty vp-empty-err' },
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
    // v1.6.2 — Filter to online-only. Charon keeps offline leases sticky
    // for reconnection stickiness, but the dashboard's job is "who is
    // connected right now". Total pool usage still visible in the Pools card.
    // MUST be declared before the return el(...) call — declaring a `const`
    // inside a function-argument list is a syntax error (was bug shipped in
    // commit 1cc2855, fixed in followup).
    const onlineLeases = (S.leases || []).filter(l => l.online);
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
      err ? el('div', { cls: 'vp-empty vp-empty-err' },
              '⚠ ' + err) : null,
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' }, 'Pools'),
        showSkeleton
          ? skelBlock()
          : (S.pools && S.pools.length
              ? el('dl', { cls: 'vp-kv' }, ...S.pools.flatMap(p => {
                  const active = (S.leases || []).filter(l => l.online).length;
                  return [
                    el('dt', {}, p.name),
                    el('dd', { cls: 'vp-mono' },
                      p.base + ' · ' + active + ' active lease' + (active === 1 ? '' : 's')),
                  ];
                }))
              : emptyState('⊘', 'No pools', 'swanctl returned no virtual-IP pools.'))
      ),
      // Active leases with customer + device + live SA enrichment.
      el('div', { cls: 'vp-card' },
        el('div', { cls: 'vp-card-title' },
          'Active leases (' + onlineLeases.length + ')'),
        showSkeleton
          ? skelBlock()
          : (onlineLeases.length
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
                      ...onlineLeases.map(lease => {
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
                                  cls: 'vp-link-cyan',
                                  onclick: (e) => { e.preventDefault(); switchPage('customers'); setTimeout(() => selectCustomer(lease.customer_id), 100); },
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
              : emptyState('○', 'No clients connected', 'No active SAs right now. Offline leases (sticky pool entries) are not shown here — check the Pools card above for total usage.'))
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
      err ? el('div', { cls: 'vp-empty vp-empty-err' },
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
          el('div', { cls: 'vp-card-title vp-mt-14' }, 'Recent log (last 8 lines)'),
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
        el('div', { cls: 'vp-card-title vp-mt-14' }, 'Add CIDR'),
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
  // v1.4.0 — `color` is a CSS variable name (e.g. 'green', 'red', 'amber') or null.
  //   Set via CSS custom property `metric-color: var(--<name>)` (CSSOM-set, CSP-safe).
  function mCard(label, value, sub, color) {
    const cssVars = color ? { 'metric-color': 'var(--' + color + ')' } : null;
    return el('div', { cls: 'vp-card vp-card-sm' },
      el('div', { cls: 'vp-metric', cssVars }, value),
      el('div', { cls: 'vp-metric-label' }, label),
      sub ? el('div', { cls: 'vp-metric-sub' }, sub) : [],
    );
  }

  function spanBadge(text, kind) {
    return el('span', { cls: 'vp-badge vp-badge-' + kind }, text);
  }

  // v1.2.12 — pill button (filter chip)
  function pillBtn(value, label, active, onClick) {
    return el('button', {
      cls: 'vp-pill' + (active ? ' vp-pill-active' : ''),
      'data-pill': value,
      onclick: onClick,
    }, label);
  }

  // v1.2.14 — Clickable column header with ▲▼ indicator. Click cycles asc → desc → no-sort.
  function sortHeader(label, field, currentSort) {
    const isActive = currentSort.by === field;
    const indicator = isActive ? (currentSort.dir === 'asc' ? ' ▲' : ' ▼') : '';
    return el('th', {
      cls: 'vp-th-sort' + (isActive ? ' vp-th-sort-on' : ''),
      'data-sort-field': field,
      onclick: () => {
        if (!isActive) {
          S.custSort = { by: field, dir: 'asc' };
        } else if (currentSort.dir === 'asc') {
          S.custSort = { by: field, dir: 'desc' };
        } else {
          // Toggle off — return to name asc
          S.custSort = { by: 'name', dir: 'asc' };
        }
        loadCustomers().then(render).catch(() => {});
      },
    }, label + indicator);
  }

  // v1.2.12 — Archive / Unarchive / Delete / Edit handlers
  async function doArchive(customerId, displayName) {
    if (!confirm(`Archive "${displayName}"?\n\n• Hides from default list\n• All data, devices, audit history preserved\n• Reversible via "Unarchive"`)) return;
    try {
      await post(`/api/customers/${customerId}/archive`);
      await loadCustomers();
      if (S.selectedId === customerId) await selectCustomer(customerId);
      render();
      toast('Archived.');
    } catch (e) {
      toast('Archive failed: ' + (e.message || e), 'err');
    }
  }

  // v1.5.0 — Generate one-time installer link for customer onboarding
  async function generateInstallerLink(c) {
    try {
      const r = await post(`/api/customers/${c.id}/installer-token`);
      showInstallerLinkModal(c, r);
    } catch (e) {
      toast('Failed to generate installer link: ' + (e.message || e), 'err');
    }
  }

  function showInstallerLinkModal(c, data) {
    // Remove any existing modal
    document.querySelectorAll('.vp-modal-bg').forEach(m => m.remove());

    const psCmd = data.powershell_cmd;
    const url = data.installer_url;
    const expires = data.expires_in_days + ' days';

    const modal = el('div', {
      cls: 'vp-modal-bg',
      onclick: (e) => { if (e.target.classList.contains('vp-modal-bg')) closeModal(); },
    },
      el('div', { cls: 'vp-modal vp-modal-wide' },
        el('div', { cls: 'vp-modal-title' }, '🔗 Installer link — ' + (c.display_name || c.name)),
        el('div', { cls: 'vp-modal-body' },
          el('p', {},
            'Send this PowerShell one-liner to the customer. They run it in ',
            el('code', {}, 'Windows PowerShell (Admin)'),
            ' and the script will fetch their credentials, bind to the VPN profile, ',
            'and connect. The link expires in ', el('strong', {}, expires),
            ' and is single-use (burned on first fetch).',
          ),
          el('div', { cls: 'vp-field' },
            el('label', {}, 'PowerShell command (copy + send):'),
            el('textarea', {
              readonly: true,
              rows: 3,
              cls: 'vp-installer-cmd',
              onclick: (e) => e.target.select(),
              id: 'vp-installer-cmd',
            }, psCmd),
          ),
          el('div', { cls: 'vp-row vp-mt-12' },
            el('button', {
              cls: 'vp-btn vp-btn-primary',
              onclick: () => copyToClipboard(psCmd, 'PowerShell command'),
            }, '📋 Copy PS command'),
            el('button', {
              cls: 'vp-btn vp-btn-ghost',
              onclick: () => copyToClipboard(url, 'Installer URL'),
            }, '📋 Copy URL'),
            el('button', {
              cls: 'vp-btn vp-btn-ghost',
              onclick: () => window.open(url, '_blank').close(),  // test fetch (burns token!)
              title: 'WARNING: this consumes the token!',
            }, '⚠ Test fetch (burns token)'),
          ),
          el('div', { cls: 'vp-info vp-mt-16 vp-fs-12 vp-fg-muted' },
            'Details: device=', el('code', {}, data.device_name),
            ' (' + data.device_type + '), tier=', el('code', {}, data.tier || 'none'),
            ', token prefix=', el('code', {}, data.token_prefix),
            ', expires ', new Date(data.expires_at * 1000).toISOString(),
          ),
        ),
        el('div', { cls: 'vp-modal-foot' },
          el('button', { cls: 'vp-btn vp-btn-ghost', onclick: () => closeModal() }, 'Close'),
        ),
      ),
    );
    document.body.appendChild(modal);
    // Auto-select the textarea content for easy keyboard copy
    const ta = modal.querySelector('#vp-installer-cmd');
    if (ta) { ta.focus(); ta.select(); }
  }

  async function copyToClipboard(text, label) {
    try {
      await navigator.clipboard.writeText(text);
      toast('Copied ' + (label || 'to') + ' clipboard', 'ok');
    } catch (e) {
      // Fallback: select the textarea
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); toast('Copied (fallback)', 'ok'); }
      catch { toast('Copy failed — select text manually', 'err'); }
      document.body.removeChild(ta);
    }
  }

  async function doUnarchive(customerId) {
    try {
      await post(`/api/customers/${customerId}/unarchive`);
      S.custFilter = 'all';  // switch back to active view
      await loadCustomers();
      await selectCustomer(customerId);
      render();
      toast('Unarchived.');
    } catch (e) {
      toast('Unarchive failed: ' + (e.message || e), 'err');
    }
  }

  async function doDelete(customerId, customerName) {
    // 1st gate: confirm
    if (!confirm(`DELETE "${customerName}"?\n\n• Removes customer row\n• Cascades: devices, alerts, purchases, audit\n• Removes EAP secret from rw-eap.conf (reloads charon)\n• IRREVERSIBLE — there is no undo`)) return;
    // 2nd gate: type the name
    const typed = prompt(`Type the customer name exactly to confirm:\n\n${customerName}`);
    if (typed !== customerName) {
      toast('Delete cancelled — name did not match.', 'err');
      return;
    }
    try {
      const url = `/api/customers/${customerId}?confirm=${encodeURIComponent(customerName)}`;
      await del(url);
      S.selectedId = null;
      S.detail = null;
      await loadCustomers();
      render();
      toast('Deleted.');
    } catch (e) {
      toast('Delete failed: ' + (e.message || e), 'err');
    }
  }

  // v1.2.13 — bulk action handlers
  function _bulkSelectedList() {
    // Resolve names for the confirm dialog
    const list = [];
    for (const c of (S.customers || [])) {
      if (S.bulkSelected.has(c.id)) list.push(c);
    }
    return list;
  }

  function _bulkConfirm(action, names, extraNote) {
    const n = names.length;
    const preview = names.slice(0, 8).map(n => `• ${n}`).join('\n');
    const more = n > 8 ? `\n… and ${n - 8} more` : '';
    const summary = {
      archive: `↥ Archive ${n} customer${n === 1 ? '' : 's'}?\n\nThey will be hidden from the default list, all data preserved. Reversible via Unarchive.`,
      unarchive: `↧ Unarchive ${n} customer${n === 1 ? '' : 's'}?\n\nThey will return to the default list. Reversible via Archive.`,
      change_tier: `⇄ Change tier for ${n} customer${n === 1 ? '' : 's'}?\n${extraNote || ''}\nTheir data_limit_bytes updates to the new tier's cap. Reversible.`,
      delete: `🗑 HARD delete ${n} customer${n === 1 ? '' : 's'}?\n\nCascades: removes devices, alerts, purchases, audit log rows. Removes EAP blocks from rw-eap.conf and reloads charon ONCE.\n\nTHIS CANNOT BE UNDONE.`,
    }[action];
    return confirm(summary + '\n\nAffected:\n' + preview + more);
  }

  async function doBulkArchive() {
    const list = _bulkSelectedList();
    if (!list.length) return;
    if (!_bulkConfirm('archive', list.map(c => c.display_name || c.name))) return;
    const ids = list.map(c => c.id);
    try {
      const res = await post('/api/customers/bulk-action', { action: 'archive', customer_ids: ids });
      S.bulkSelected.clear();
      await loadCustomers();
      render();
      const skipped = (res.skipped || []).length;
      const msg = `Archived ${res.affected.length}` + (skipped ? ` (${skipped} skipped)` : '');
      toast(msg, skipped ? 'err' : 'ok');
    } catch (e) {
      toast('Bulk archive failed: ' + (e.message || e), 'err');
    }
  }

  async function doBulkUnarchive() {
    const list = _bulkSelectedList();
    if (!list.length) return;
    if (!_bulkConfirm('unarchive', list.map(c => c.display_name || c.name))) return;
    const ids = list.map(c => c.id);
    try {
      const res = await post('/api/customers/bulk-action', { action: 'unarchive', customer_ids: ids });
      S.bulkSelected.clear();
      await loadCustomers();
      render();
      const skipped = (res.skipped || []).length;
      toast(`Unarchived ${res.affected.length}` + (skipped ? ` (${skipped} skipped)` : ''), skipped ? 'err' : 'ok');
    } catch (e) {
      toast('Bulk unarchive failed: ' + (e.message || e), 'err');
    }
  }

  async function doBulkChangeTier() {
    const list = _bulkSelectedList();
    if (!list.length) return;
    const tierList = (S.tiers || []).map((t, i) =>
      `${i + 1}) ${t.display_name || t.name} (${fmtBytes(t.data_limit_bytes)})  [name: ${t.name}]`
    ).join('\n');
    const picked = prompt(
      `⇄ Change tier for ${list.length} customer${list.length === 1 ? '' : 's'}.\n\nAvailable tiers:\n${tierList}\n\nEnter the tier name (exact match):`
    );
    if (!picked) return;
    const tierName = picked.trim();
    const tierExists = (S.tiers || []).some(t => t.name === tierName);
    if (!tierExists) {
      toast(`Tier '${tierName}' not found. Use exact tier name from the list.`, 'err');
      return;
    }
    if (!_bulkConfirm('change_tier', list.map(c => c.display_name || c.name), `\n→ New tier: ${tierName}\n`)) return;
    const ids = list.map(c => c.id);
    try {
      const res = await post('/api/customers/bulk-action', {
        action: 'change_tier', customer_ids: ids, tier_name: tierName
      });
      S.bulkSelected.clear();
      await loadCustomers();
      render();
      const skipped = (res.skipped || []).length;
      toast(`Changed tier for ${res.affected.length}` + (skipped ? ` (${skipped} skipped)` : ''), skipped ? 'err' : 'ok');
    } catch (e) {
      toast('Bulk change tier failed: ' + (e.message || e), 'err');
    }
  }

  async function doBulkDelete() {
    const list = _bulkSelectedList();
    if (!list.length) return;
    const expected = `DELETE ${list.length} CUSTOMERS`;
    if (!_bulkConfirm('delete', list.map(c => c.display_name || c.name))) return;
    const typed = prompt(`This is irreversible. Type ${expected} to confirm:`);
    if (typed !== expected) {
      toast('Delete cancelled (confirmation text did not match).', 'err');
      return;
    }
    const ids = list.map(c => c.id);
    try {
      const res = await post('/api/customers/bulk-action', {
        action: 'delete', customer_ids: ids, confirm: typed
      });
      S.bulkSelected.clear();
      S.selectedId = null;
      S.detail = null;
      await loadCustomers();
      render();
      toast(`Deleted ${res.affected.length} (${res.eap_blocks_removed} EAP blocks removed).`, 'ok');
    } catch (e) {
      toast('Bulk delete failed: ' + (e.message || e), 'err');
    }
  }

  function openEditCustomerModal(c) {
    // v1.2.12 — edit: display_name, telegram, email, billing_id, notes, tier (incl. custom), max_devices
    (async () => {
      const tiers = await loadTiers();
      const isCustom = !tiers.find(t => t.name === c.tier);
      const allTiers = [...tiers];
      if (isCustom && c.tier) {
        allTiers.push({ name: c.tier, display_name: c.tier_display || c.tier });
      }
      // Helper: render a labeled form field.
      // Signature: labeledField(label, inputEl, fullWidth?)
      // Used in the Edit modal below.
      const labeledField = (label, inputEl, full) => el('div',
        { cls: 'vp-field' + (full ? ' vp-field-full' : '') },
        el('label', { cls: 'vp-label' }, label),
        inputEl,
      );
      const modal = el('div', {
        cls: 'vp-modal-bg',
        onclick: (e) => { if (e.target.classList.contains('vp-modal-bg')) closeModal(); },
      },
        el('div', { cls: 'vp-modal vp-modal-lg' },
          el('div', { cls: 'vp-modal-h' },
            el('div', { cls: 'vp-modal-title' }, 'Edit customer'),
            el('div', { cls: 'vp-modal-sub' }, 'Changes apply on Save. Reloading charon not required for these fields.'),
          ),
          el('div', { cls: 'vp-modal-b' },
            el('div', { cls: 'vp-form-grid' },
              labeledField('Display name', el('input', { type: 'text', cls: 'vp-input', id: 'ed-disp', value: c.display_name || '' })),
              labeledField('Telegram username', el('input', { type: 'text', cls: 'vp-input', id: 'ed-tg', value: c.telegram_username || '' })),
              labeledField('Email', el('input', { type: 'email', cls: 'vp-input', id: 'ed-email', value: c.email || '' })),
              labeledField('Billing ID', el('input', { type: 'text', cls: 'vp-input', id: 'ed-bill', value: c.billing_id || '' })),
              labeledField('Max devices (1–10)', el('input', { type: 'number', min: 1, max: 10, cls: 'vp-input', id: 'ed-mdev', value: c.max_devices || 1 })),
              labeledField('Bandwidth down (Mbps, 1–1000)', el('input', { type: 'number', min: 1, max: 1000, cls: 'vp-input', id: 'ed-bw-down', value: c.bandwidth_down_mbps || 20 })),
              labeledField('Bandwidth up (Mbps, 1–1000)', el('input', { type: 'number', min: 1, max: 1000, cls: 'vp-input', id: 'ed-bw-up', value: c.bandwidth_up_mbps || 20 })),
              labeledField('Tier',
                el('select', { cls: 'vp-input', id: 'ed-tier' },
                  ...allTiers.map(t => el('option', { value: t.name, selected: t.name === c.tier }, t.display_name || t.name)),
                  el('option', { value: 'custom' }, '+ Custom cap (MiB)…'),
                ),
              ),
              labeledField('Custom cap (MiB, only if tier=custom)', el('input', { type: 'number', min: 1, cls: 'vp-input', id: 'ed-custom-mb' })),
              labeledField('Notes', el('textarea', { cls: 'vp-input', rows: 3, id: 'ed-notes' }, c.notes || ''), true),
            ),
          ),
          el('div', { cls: 'vp-modal-f' },
            el('button', { cls: 'vp-btn vp-btn-ghost', onclick: () => closeModal() }, 'Cancel'),
            el('button', {
              cls: 'vp-btn vp-btn-primary',
              onclick: async () => {
                // Use document.getElementById directly to avoid any
                // `$` closure-capture quirks in nested modal contexts.
                const $ = id => document.getElementById(id);
                const $d = $('ed-disp'), $tg = $('ed-tg'), $em = $('ed-email'),
                      $bi = $('ed-bill'), $md = $('ed-mdev'), $ti = $('ed-tier'),
                      $cm = $('ed-custom-mb'), $nt = $('ed-notes'),
                      $bwd = $('ed-bw-down'), $bwu = $('ed-bw-up');
                if (!$d || !$tg || !$em || !$bi || !$md || !$ti || !$bwd || !$bwu) {
                  toast('Edit form is broken — fields missing. Reload the page.', 'err');
                  return;
                }
                const body = {
                  display_name: $d.value.trim() || null,
                  telegram_username: $tg.value.trim() || null,
                  email: $em.value.trim() || null,
                  billing_id: $bi.value.trim() || null,
                  max_devices: parseInt($md.value || '1', 10),
                  bandwidth_down_mbps: parseInt($bwd.value || '20', 10),
                  bandwidth_up_mbps: parseInt($bwu.value || '20', 10),
                  tier_name: $ti.value,
                  notes: $nt.value.trim() || null,
                };
                // v1.6.5 — defense in depth: backend (db_query) now converts
                // sqlite3 -json's "None" string to real null, but if a stale
                // browser cache still has the old API shape (or if any other
                // field ever gets the literal string "None" pre-filled), the
                // email regex validation would 400. Strip "None"/"null"/
                // whitespace-only strings before sending.
                for (const k of ['telegram_username', 'email', 'billing_id', 'notes', 'display_name']) {
                  if (body[k] && (body[k].toLowerCase() === 'none' || body[k].toLowerCase() === 'null')) {
                    body[k] = null;
                  }
                }
                if (body.tier_name === 'custom') {
                  const mb = parseInt($cm.value || '0', 10);
                  if (mb < 1) { toast('Custom cap must be ≥ 1 MiB', 'err'); return; }
                  body.custom_cap_mb = mb;
                }
                try {
                  await patch(`/api/customers/${c.id}`, body);
                  closeModal();
                  await loadCustomers();
                  await selectCustomer(c.id);
                  render();
                  toast('Updated.');
                } catch (e) {
                  toast('Update failed: ' + (e.message || e), 'err');
                }
              },
            }, 'Save'),
          ),
        ),
      );
      openModal(modal);
    })();
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

  // v1.3.0.1 — openModal helper (paired with closeModal). Was called by the Edit
  // customer modal but never defined. Same shape as the inline code in
  // openNewClientModal: append to body, register ESC listener.
  function openModal(modalEl) {
    document.body.appendChild(modalEl);
    document.addEventListener('keydown', _modalEscListener);
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
          el('div', { cls: 'vp-field vp-hidden', id: 'vp-nc-custom-wrap' },
            el('label', { cls: 'vp-label' }, 'Custom cap (MiB)'),
            el('input', { id: 'vp-nc-custom-mb', cls: 'vp-inp', type: 'number',
                           min: 1, max: 1048576, placeholder: 'e.g. 1500 for 1.5 GB' }),
            el('div', { cls: 'vp-hint' }, 'Binary MiB (× 1,048,576 bytes). Tier auto-created.'),
          ),
          // v1.5.0 — Speed plan (per-customer, NOT tier-driven). Two presets:
          //   'standard'         → 20/20 mbps symmetric
          //   'asymmetric_40_20' → 40/20 mbps (asymmetric, typical home broadband)
          // Tiers drive data quota (5/10/20 GB); speed_plan drives bandwidth.
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Speed plan'),
            el('select', { id: 'vp-nc-speed-plan', cls: 'vp-inp' },
              el('option', { value: 'standard' }, 'Standard — 20 Mbps down / 20 Mbps up (symmetric)'),
              el('option', { value: 'asymmetric_40_20' }, 'Asymmetric — 40 Mbps down / 20 Mbps up'),
            ),
            el('div', { cls: 'vp-hint' },
              'Per-customer bandwidth. Independent of tier (tier controls data quota only).'),
          ),
          el('div', { cls: 'vp-field vp-hidden', id: 'vp-nc-bw-override-wrap' },
            el('label', { cls: 'vp-label' },
              'Custom bandwidth (Mbps) ',
              el('span', { cls: 'vp-optional' }, '(advanced override — wins over speed plan)')),
            el('div', { cls: 'vp-row' },
              el('input', { id: 'vp-nc-bw-down', cls: 'vp-inp vp-flex-1 vp-mr-6', type: 'number',
                             min: 1, max: 1000, placeholder: 'down' }),
              el('input', { id: 'vp-nc-bw-up',   cls: 'vp-inp vp-flex-1', type: 'number',
                             min: 1, max: 1000, placeholder: 'up' }),
            ),
            el('div', { cls: 'vp-hint' },
              'Both fields required. Bypasses the speed-plan preset above.'),
          ),
          // Device
          el('div', { cls: 'vp-field' },
            el('label', { cls: 'vp-label' }, 'Device name'),
            el('input', { id: 'vp-nc-device', cls: 'vp-inp', type: 'text', required: true,
                           placeholder: 'laptop', maxlength: 32,
                           
                           'aria-describedby': 'vp-nc-device-hint vp-nc-device-warn',
                           value: 'laptop' }),
            el('div', { cls: 'vp-hint', id: 'vp-nc-device-hint' },
              'Friendly name. EAP identity will be \u201c{customer-name}-{device-name}\u201d.'),
            el('div', { cls: 'vp-field-warn vp-hidden', id: 'vp-nc-device-warn' }),
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
        el('div', { id: 'vp-nc-custom-preview', cls: 'vp-custom-preview vp-hidden' }),
        el('div', { id: 'vp-nc-form-err', cls: 'vp-form-err vp-hidden' }),
        el('div', { cls: 'vp-btn-row vp-mt-18 vp-justify-end' },
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
      customWrap.classList.toggle('vp-hidden', !isCustom);
      if (isCustom) {
        const mb = parseInt(customMb.value || '0', 10);
        if (mb > 0) {
          const bytes = mb * 1048576;
          preview.classList.remove('vp-hidden');
          preview.innerHTML = '';
          preview.appendChild(el('strong', {}, '→ New tier: '));
          preview.appendChild(document.createTextNode(
            `custom_${mb}mb_<ts> · ${fmtBytes(bytes)} (${mb} MiB)`));
        } else {
          preview.classList.add('vp-hidden');
        }
      } else {
        preview.classList.add('vp-hidden');
      }
    }
    tierSel.addEventListener('change', refresh);
    customMb.addEventListener('input', refresh);

    // v1.5.0 — speed plan + bandwidth override wiring
    const speedPlanSel = body.querySelector('#vp-nc-speed-plan');
    const bwOverrideWrap = body.querySelector('#vp-nc-bw-override-wrap');
    const bwDown = body.querySelector('#vp-nc-bw-down');
    const bwUp   = body.querySelector('#vp-nc-bw-up');
    speedPlanSel.addEventListener('change', () => {
      // Custom override is always available; we just show it after user picks.
      // (Not auto-shown because most operators will use the preset.)
    });

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
        devWarn.classList.remove('vp-hidden');
        devInp.classList.add('vp-inp-bad');
        if (submitBtn) submitBtn.disabled = true;
      } else {
        devWarn.classList.add('vp-hidden');
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
    errEl.classList.add('vp-hidden');
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
      speed_plan:         document.getElementById('vp-nc-speed-plan').value,
      bandwidth_down_mbps: parseInt(document.getElementById('vp-nc-bw-down').value || '', 10) || null,
      bandwidth_up_mbps:   parseInt(document.getElementById('vp-nc-bw-up').value   || '', 10) || null,
      device_name:        document.getElementById('vp-nc-device').value.trim(),
      device_type:        document.getElementById('vp-nc-devtype').value,
      os_version:         document.getElementById('vp-nc-osver').value.trim() || null,
    };
    if (body.tier_name !== 'custom') body.custom_cap_mb = null;
    // If neither explicit bandwidth field is filled, drop both so the server
    // applies the speed_plan preset (no partial-state confusion).
    if (body.bandwidth_down_mbps == null && body.bandwidth_up_mbps == null) {
      body.bandwidth_down_mbps = null;
      body.bandwidth_up_mbps = null;
    }

    try {
      const r = await post('/api/customers', body);
      // Refresh customers list in the background
      try { loadCustomers().then(render).catch(()=>{}); } catch {}
      // v1.6.0 — For Windows devices, also auto-generate the installer
      // one-liner so the operator can immediately send it to the customer.
      // Token is one-shot, 7-day expiry, burned on first customer fetch.
      let installerData = null;
      if (body.device_type === 'Windows') {
        try {
          installerData = await post(`/api/customers/${r.customer.id}/installer-token`);
        } catch (e) {
          // Non-fatal — Windows card will fall back to manual steps.
          console.warn('installer-token failed:', e.message || e);
        }
      }
      renderOneshotPanel(r, installerData);
    } catch (e) {
      errEl.textContent = e.message || String(e);
      errEl.classList.remove('vp-hidden');
      submitBtn.disabled = false;
      submitBtn.textContent = 'Create client';
    }
  }

  // ─── One-shot password panel (shown after successful create) ─────────────
  // v1.6.0 — installerData (optional): PowerShell one-liner data for Windows.
  // When the customer was just created with device_type=Windows, we auto-generate
  // the installer token so the operator can copy-paste-send immediately.
  function renderOneshotPanel(r, installerData) {
    const c = r.customer, d = r.device;
    const eapId = r.eap_identity, pw = r.password;
    const server    = 'myvpn.databyte.co.za';
    const remoteId  = 'myvpn.databyte.co.za';
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

    // v1.6.0 — Windows: prefer PowerShell installer one-liner over manual steps.
    // The one-liner downloads the CA cert + EAP profile + connects. Falls back
    // to manual steps if installer-token generation failed.
    const Windows = setupCard('Windows',
      installerData && installerData.powershell_cmd
        ? el('div', {},
            el('div', { cls: 'vp-setup-steps' },
              el('div', {},
                el('strong', {}, 'Send these 3 lines to the customer. They paste them into '),
                el('code', {}, 'Windows PowerShell (Admin)'),
                el('strong', {}, '. The script downloads the CA cert, installs the EAP profile, and connects.'),
              ),
              el('div', { cls: 'vp-row vp-mt-12' },
                el('button', {
                  type: 'button',
                  cls: 'vp-btn vp-btn-primary',
                  onclick: () => {
                    navigator.clipboard.writeText(installerData.powershell_cmd).then(() => {
                      showBanner('Copied PowerShell one-liner', 'ok');
                    }).catch(() => { showBanner('Copy failed', 'err'); });
                  },
                }, '⧉ Copy PowerShell one-liner'),
                el('button', {
                  type: 'button',
                  cls: 'vp-btn vp-btn-ghost',
                  onclick: () => {
                    window.open(installerData.installer_url, '_blank').close();
                    showBanner('⚠ Test fetch consumed the token', 'err');
                  },
                  title: 'WARNING: this consumes the token!',
                }, '⚠ Test fetch (burns token)'),
              ),
              // v2.5.2 — Render the multi-line powershell_cmd in a <pre> so the
              // operator can SEE the 3 lines before clicking Copy. Each line
              // starts on its own row.
              el('pre', { cls: 'vp-cmd vp-mt-12' },
                installerData.powershell_cmd),
              el('div', { cls: 'vp-info vp-mt-16 vp-fs-12 vp-fg-muted' },
                'Token: ', el('code', {}, installerData.token_prefix),
                ' — expires in ', String(installerData.expires_in_days), ' day(s). ',
                'Single-use: burns when the customer runs the one-liner.',
              ),
            ),
          )
        : el('ol', { cls: 'vp-setup-steps' },
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
          )
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
        el('div', { cls: 'vp-btn-row vp-mt-20 vp-justify-end' },
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
      cls: 'vp-btn-row vp-mt-18 vp-gap-8',
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
          // v1.4.0 — use .vp-offscreen class instead of inline style (strict CSP).
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.className = 'vp-offscreen';
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
