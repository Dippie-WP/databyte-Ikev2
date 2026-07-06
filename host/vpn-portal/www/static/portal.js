// v1.4.0 — Customer portal SPA. Vanilla JS, no framework.
// Login → /api/portal/login → cookie → /api/portal/usage → render.
// On 401, show login. On logout, clear and show login.
// v1.4.0: refactored fill.style.width → fill.style.setProperty('--pct', ...)
// for strict-CSP compliance (no inline style attributes).
// v1.4.1: add 30s auto-refresh on dashboard so data usage updates without
// page reload. Matches operator portal cadence. Pauses on 401 / logout / page
// hidden. Re-fires on visibility return.

(function () {
  'use strict';

  // v1.4.1 — auto-refresh state
  const AUTO_REFRESH_MS = 30000;
  let _refreshTimer = null;
  function startAutoRefresh() {
    stopAutoRefresh();
    _refreshTimer = setInterval(loadUsage, AUTO_REFRESH_MS);
  }
  function stopAutoRefresh() {
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  }

  function fmtBytes(n) {
    if (!n || n < 0) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KiB';
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MiB';
    return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GiB';
  }

  function show(id) {
    document.getElementById(id).classList.remove('vp-hidden');
  }
  function hide(id) {
    document.getElementById(id).classList.add('vp-hidden');
  }
  function showLogin() {
    hide('vp-dashboard-view');
    show('vp-login-view');
  }
  function showDashboard() {
    hide('vp-login-view');
    show('vp-dashboard-view');
  }

  function showError(msg) {
    const el = document.getElementById('vp-login-error');
    el.textContent = msg;
    show('vp-login-error');
  }
  function hideError() {
    hide('vp-login-error');
  }

  async function doLogin(identity, password) {
    const btn = document.getElementById('vp-login-btn');
    btn.disabled = true;
    btn.textContent = 'Signing in...';
    hideError();
    try {
      const r = await fetch('/api/portal/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity, password }),
        credentials: 'include',
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({ detail: 'Login failed' }));
        showError(body.detail || 'Invalid credentials');
        return;
      }
      await loadUsage();
      // v1.4.1 — start 30s auto-refresh once we're past auth
      startAutoRefresh();
    } catch (e) {
      showError('Network error: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  }

  async function doLogout() {
    try {
      await fetch('/api/portal/logout', {
        method: 'POST',
        credentials: 'include',
      });
    } catch (e) {
      // best effort
    }
    stopAutoRefresh();
    showLogin();
    document.getElementById('vp-identity').value = '';
    document.getElementById('vp-password').value = '';
  }

  async function loadUsage() {
    let r;
    try {
      r = await fetch('/api/portal/usage', {
        credentials: 'include',
      });
    } catch (e) {
      showLogin();
      stopAutoRefresh();
      return;
    }
    if (r.status === 401) {
      showLogin();
      stopAutoRefresh();
      return;
    }
    if (!r.ok) {
      showError('Failed to load usage (HTTP ' + r.status + ')');
      showLogin();
      stopAutoRefresh();
      return;
    }
    const u = await r.json();
    renderUsage(u);
    showDashboard();
  }

  function renderUsage(u) {
    const tierName = u.tier_display || u.tier_name || 'Unknown tier';
    document.getElementById('vp-tier').textContent = tierName;

    if (u.no_cap) {
      hide('vp-meter-fill');
      hide('vp-meter-text');
      hide('vp-stats');
      show('vp-no-cap');
    } else {
      show('vp-meter-fill');
      show('vp-meter-text');
      show('vp-stats');
      hide('vp-no-cap');

      const pct = Math.min(100, u.data_pct || 0);
      const fill = document.getElementById('vp-meter-fill');
      // v1.4.0 — strict-CSP compliant: use CSS custom property via CSSOM API.
      // element.setProperty is allowed by strict CSP per W3C CSP3 §6.7.3.1.
      // Only the `style="..."` *attribute* is blocked; CSSOM writes are not.
      fill.style.setProperty('--pct', pct + '%');
      fill.classList.remove('vp-warn', 'vp-over');
      if (u.over_quota || pct >= 100) {
        fill.classList.add('vp-over');
      } else if (pct >= 80) {
        fill.classList.add('vp-warn');
      }
      document.getElementById('vp-meter-text').textContent = pct.toFixed(1) + '%';

      const used = fmtBytes(u.data_used_bytes);
      const limit = fmtBytes(u.data_limit_bytes);
      const remaining = fmtBytes(Math.max(0, (u.data_limit_bytes || 0) - (u.data_used_bytes || 0)));
      document.getElementById('vp-stats').innerHTML =
        '<span><strong>' + used + '</strong> used</span>' +
        '<span><strong>' + remaining + '</strong> remaining</span>' +
        '<span><strong>' + limit + '</strong> cap</span>';
    }

    // Footer: who you're logged in as + session info
    const me = document.getElementById('vp-meta');
    const createdAt = u._session_created_at
      ? new Date(u._session_created_at * 1000).toLocaleString()
      : '';
    me.textContent = 'Session active. ' + (createdAt ? 'Signed in at ' + createdAt + '. ' : '') + 'Idle expiry: 30 days.';
  }

  // Wire up
  document.getElementById('vp-login-form').addEventListener('submit', function (e) {
    e.preventDefault();
    const id = document.getElementById('vp-identity').value.trim();
    const pw = document.getElementById('vp-password').value;
    if (!id || !pw) {
      showError('Please enter your EAP identity and password.');
      return;
    }
    doLogin(id, pw);
  });
  document.getElementById('vp-logout-btn').addEventListener('click', doLogout);

  // On load: try to fetch usage. If 401, show login.
  loadUsage().then(() => {
    // v1.4.1 — if we already have a valid session cookie (returning customer),
    // loadUsage() rendered the dashboard. Start the 30s auto-refresh now.
    const dashVisible = !document.getElementById('vp-dashboard-view').classList.contains('vp-hidden');
    if (dashVisible) startAutoRefresh();
  });

  // v1.4.1 — pause auto-refresh when tab is hidden (Chrome throttles setInterval
  // to 1s+ in background anyway, but stopping avoids spurious 401 storms if the
  // server-side session expires while the tab is hidden). Resume on return.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      stopAutoRefresh();
    } else {
      const dashVisible = !document.getElementById('vp-dashboard-view').classList.contains('vp-hidden');
      if (dashVisible) {
        loadUsage();
        startAutoRefresh();
      }
    }
  });
})();
