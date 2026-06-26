// v1.3.0 — Customer portal SPA. Vanilla JS, no framework.
// Login → /api/portal/login → cookie → /api/portal/usage → render.
// On 401, show login. On logout, clear and show login.

(function () {
  'use strict';

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
      return;
    }
    if (r.status === 401) {
      showLogin();
      return;
    }
    if (!r.ok) {
      showError('Failed to load usage (HTTP ' + r.status + ')');
      showLogin();
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
      fill.style.width = pct + '%';
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
  loadUsage();
})();
