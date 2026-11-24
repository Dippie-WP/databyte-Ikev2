// v2.0.0 — Customer portal SPA. Vanilla JS, no framework.
// Login → /api/portal/login → cookie → /api/portal/usage → render.
// On 401, show login. On logout, clear and show login.
//
// v2.0.0 (2026-11-24) — account revamp: dashboard now shows the customer
//   name, tier badge, time-left (text + period bar), sign-up date and expiry
//   date, alongside the existing data-usage meter. All values come from
//   /api/portal/usage (server-computed; the client does no date math, so
//   browser timezone cannot drift the result).
// v1.4.1: 30s auto-refresh on the dashboard. Pauses on 401 / logout / hidden tab.
// v1.4.0: fill.style.setProperty('--pct', ...) for strict-CSP compliance.

(function () {
  'use strict';

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

  function show(id) { document.getElementById(id).classList.remove('vp-hidden'); }
  function hide(id) { document.getElementById(id).classList.add('vp-hidden'); }
  function showLogin() { hide('vp-dashboard-view'); show('vp-login-view'); }
  function showDashboard() { hide('vp-login-view'); show('vp-dashboard-view'); }

  function showError(msg) {
    const el = document.getElementById('vp-login-error');
    el.textContent = msg;
    show('vp-login-error');
  }
  function hideError() { hide('vp-login-error'); }

  // Strict-CSP safe: set the meter fill via CSS custom property through CSSOM.
  function setFill(el, pct, cls) {
    el.style.setProperty('--pct', pct + '%');
    el.classList.remove('vp-warn', 'vp-over');
    if (cls) el.classList.add(cls);
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
      await fetch('/api/portal/logout', { method: 'POST', credentials: 'include' });
    } catch (e) { /* best effort */ }
    stopAutoRefresh();
    showLogin();
    document.getElementById('vp-identity').value = '';
    document.getElementById('vp-password').value = '';
  }

  async function loadUsage() {
    let r;
    try {
      r = await fetch('/api/portal/usage', { credentials: 'include' });
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

  function renderAccount(u) {
    // Name (display name preferred) + tier badge
    document.getElementById('vp-name').textContent =
      u.customer_display_name || u.customer_name || 'Account';
    document.getElementById('vp-tier').textContent =
      u.tier_display || u.tier_name || 'Unknown tier';

    // Time left — text + period bar
    const label = document.getElementById('vp-time-label');
    const fill = document.getElementById('vp-time-fill');
    const expired = u.expired || (u.has_expiry && (u.seconds_left || 0) <= 0);
    if (!u.has_expiry) {
      label.textContent = 'No expiry set';
      setFill(fill, 0, null);
    } else if (expired) {
      label.textContent = 'Expired';
      setFill(fill, 100, 'vp-over');
    } else {
      const d = u.days_left;
      if (u.seconds_left != null && u.seconds_left < 86400) {
        label.textContent = 'Less than a day left';
      } else {
        label.textContent = (d === 1) ? '1 day left' : d + ' days left';
      }
      let p = 0;
      if (u.account_days && u.account_days > 0) {
        p = Math.min(100, Math.max(0, ((u.account_days - d) / u.account_days) * 100));
      }
      setFill(fill, p.toFixed(1), d <= 3 ? 'vp-warn' : null);
    }

    // Dates
    document.getElementById('vp-signup').textContent = u.signup_date || '\u2014';
    document.getElementById('vp-expiry').textContent = u.expiry_date || '\u2014';
  }

  function renderUsage(u) {
    renderAccount(u);

    // Data usage meter
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
      setFill(fill, pct.toFixed(1), (u.over_quota || pct >= 100) ? 'vp-over' : (pct >= 80 ? 'vp-warn' : null));
      document.getElementById('vp-meter-text').textContent = pct.toFixed(1) + '%';

      const used = fmtBytes(u.data_used_bytes);
      const limit = fmtBytes(u.data_limit_bytes);
      const remaining = fmtBytes(Math.max(0, (u.data_limit_bytes || 0) - (u.data_used_bytes || 0)));
      document.getElementById('vp-stats').innerHTML =
        '<span><strong>' + used + '</strong> used</span>' +
        '<span><strong>' + remaining + '</strong> remaining</span>' +
        '<span><strong>' + limit + '</strong> cap</span>';
    }

    // Footer
    const me = document.getElementById('vp-meta');
    me.textContent = 'Signed in as ' + (u.customer_name || 'your account') +
      '. Auto-refreshes every 30s. Idle expiry: 30 days.';
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

  loadUsage().then(() => {
    const dashVisible = !document.getElementById('vp-dashboard-view').classList.contains('vp-hidden');
    if (dashVisible) startAutoRefresh();
  });

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      stopAutoRefresh();
    } else {
      const dashVisible = !document.getElementById('vp-dashboard-view').classList.contains('vp-hidden');
      if (dashVisible) { loadUsage(); startAutoRefresh(); }
    }
  });
})();
