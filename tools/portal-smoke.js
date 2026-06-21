#!/usr/bin/env node
/* v1.2.8 — databyte VPN portal headless-browser smoke test.
 *
 * Drives a real Chromium against the portal UI and verifies eight DOM-layer
 * invariants that API-only testing misses. Specifically designed to catch
 * the failure mode that bit v1.2.7.1 (the el() flatten bug — API worked, UI
 * rendered empty DOM, sat broken for ~36h).
 *
 * Usage:
 *   node tools/portal-smoke.js                                  # default config (127.0.0.1:8080)
 *   PORTAL_URL=http://192.168.10.98:8080 node tools/portal-smoke.js
 *   npm run smoke --prefix tools                                # via package.json
 *   npm run smoke:live --prefix tools                           # hits the live LXC 903 portal
 *
 * Exit codes: 0 = all 8 checks passed, 1 = at least one failed.
 * On failure, screenshots are written to tools/screenshots/smoke-NN-*.png.
 * On success, screenshots are written too (for visual confirmation in CI artifacts).
 */

// v1.2.11: package.json has BOTH puppeteer (devDep, downloads chromium)
// and puppeteer-core (dep, uses system chromium). Prefer puppeteer when
// both are installed (full CI setup). Fall back to puppeteer-core (local dev).
// Skip gracefully if neither loads.
let puppeteer;
let isFullPuppeteer = false;
try {
  puppeteer = require('puppeteer');
  isFullPuppeteer = true;
} catch {
  try {
    puppeteer = require('puppeteer-core');
  } catch {
    console.error('FATAL: neither puppeteer nor puppeteer-core installed.');
    console.error('Run: npm install (in tools/)');
    process.exit(2);
  }
}
const fs        = require('fs');
const path      = require('path');

const VERBOSE = !!process.env.VERBOSE;
const CFG_PATH = path.join(__dirname, 'portal-smoke.config.json');
const CFG      = JSON.parse(fs.readFileSync(CFG_PATH, 'utf8'));

// Allow env override for URL (CI uses this to point at staging/LXC)
if (process.env.PORTAL_URL) CFG.base_url = process.env.PORTAL_URL;

const log = (...a) => { if (VERBOSE) console.log(...a); };

async function shot(page, name) {
  fs.mkdirSync(CFG.screenshots_dir, { recursive: true });
  const p = path.join(CFG.screenshots_dir, `smoke-${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  log('   📸', p);
}

async function check(name, fn) {
  process.stdout.write(`  ${name.padEnd(60)} `);
  try {
    await fn();
    console.log('✅');
    return { name, pass: true };
  } catch (e) {
    console.log('❌');
    console.log(`     ${e.message}`);
    if (e.stack && VERBOSE) console.log(e.stack.split('\n').slice(1, 4).join('\n'));
    return { name, pass: false, err: e.message };
  }
}

// v1.2.13 — login via API and return session cookie value
async function loginApi(url) {
  const r = await fetch(new URL('/api/login', url).toString(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: CFG.username, password: CFG.password }),
  });
  if (!r.ok) throw new Error(`login API failed: HTTP ${r.status}`);
  const rawHeaders = r.headers.getSetCookie ? r.headers.getSetCookie() : (r.headers.get('set-cookie') || '').split(/,(?=[^ ])/);
  for (const h of rawHeaders) {
    // Cookie name is `session` (FastAPI SessionMiddleware default), not vpn_session
    const m = h.match(/(?:^|;\s*)session=([^;]+)/);
    if (m) return m[1];
  }
  throw new Error(`login did not return session cookie. status=${r.status}, set-cookie=${rawHeaders[0] || '(none)'}`);
}

// v1.2.13 — login via UI (for tests that need the DOM)
async function loginUi(page) {
  await page.waitForSelector('#vp-user', { timeout: CFG.timeouts.selector_ms });
  await page.type('#vp-user', CFG.username, { delay: 20 });
  await page.type('#vp-pass', CFG.password, { delay: 20 });
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle0', timeout: CFG.timeouts.page_load_ms }).catch(() => {}),
    page.click('button[type="submit"]'),
  ]);
  await new Promise(r => setTimeout(r, 600));
}

async function waitMs(ms) { return new Promise(r => setTimeout(r, ms)); }

(async () => {
  const t0 = Date.now();
  console.log(`🧪 v1.2.8 portal-smoke — ${CFG.base_url}`);
  console.log('');

  // Build launch options. With puppeteer-core we MUST specify executablePath.
  // With full puppeteer we can omit it and let puppeteer find its bundled browser.
  const launchOpts = {
    headless: CFG.browser.headless,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  };
  if (isFullPuppeteer) {
    // Full puppeteer: use bundled chromium (downloaded by CI workflow).
    // Skip executablePath entirely so it uses puppeteer's browserFetcher cache.
  } else {
    // puppeteer-core: must point at system chromium.
    const exe = CFG.browser.executable_path;
    if (!exe || !require('fs').existsSync(exe)) {
      throw new Error(
        `puppeteer-core requires executablePath but '${exe}' is missing.\n` +
        `Either install system chromium at that path, or 'npm install puppeteer' (downloads bundled chromium).`
      );
    }
    launchOpts.executablePath = exe;
  }

  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  await page.setViewport(CFG.browser.viewport);

  // Surface page errors loudly
  page.on('pageerror', (err) => console.log('   ⚠ pageerror:', err.message));
  page.on('console', (msg) => {
    if (msg.type() === 'error') log('   ⚠ console.error:', msg.text());
  });

  // Log API responses when VERBOSE (helps debug timing)
  if (VERBOSE) {
    page.on('response', (resp) => {
      const u = resp.url();
      if (u.includes('192.168.10.98') && u.includes('/api/')) {
        console.log(`   📡 ${resp.status()} ${resp.request().method()} ${u.replace('http://192.168.10.98:8080', '')}`);
      }
    });
    page.on('request', (req) => {
      if (req.url().includes('/api/login') && req.method() === 'POST') {
        console.log(`   📤 POST /api/login body: ${req.postData()}`);
      }
    });
  }

  const results = [];

  // ─── 1. Login page renders ───────────────────────────────
  results.push(await check('1. / renders login form (username + password inputs)', async () => {
    await page.goto(CFG.base_url, { waitUntil: 'networkidle0', timeout: CFG.timeouts.page_load_ms });
    await waitMs(300);  // let render() complete
    await shot(page, '01-login');
    const u = await page.$('#vp-user');
    const p = await page.$('#vp-pass');
    if (!u) throw new Error('#vp-user not found in DOM');
    if (!p) throw new Error('#vp-pass not found in DOM');
    const submit = await page.$('button[type="submit"]');
    if (!submit) throw new Error('no submit button on login form');
  }));

  // ─── 2. Login submits + dashboard renders ────────────────
  results.push(await check('2. Login submits + dashboard cards appear', async () => {
    // Use page.type + page.click — same approach that works in manual probes.
    // (Setting .value directly + dispatching events races with Chrome autofill
    //  and React-style controlled inputs; page.type sends real keystrokes.)
    await page.type('#vp-user', CFG.username, { delay: 20 });
    await page.type('#vp-pass', CFG.password, { delay: 20 });
    // Sanity check values were typed correctly
    const vals = await page.evaluate(() => ({
      u: document.getElementById('vp-user').value,
      p: document.getElementById('vp-pass').value,
    }));
    if (vals.u !== CFG.username || vals.p !== CFG.password) {
      throw new Error(`field values wrong: u="${vals.u}" p="${vals.p}"`);
    }
    await page.click('button[type="submit"]');
    // The SPA does POST /api/login → on success → render() swaps in dashboard.
    await page.waitForFunction(() => {
      return document.body.innerText.includes('Dashboard') &&
             document.body.innerText.includes('System health');
    }, { timeout: CFG.timeouts.selector_ms, polling: 300 });
    await waitMs(1500);  // let loadDashboard() finish (parallel API calls)
    await shot(page, '02-dashboard');
    const metrics = await page.$$('.vp-metric');
    if (metrics.length === 0) {
      const errText = await page.$eval('#vp-login-err', el => el.textContent).catch(() => '');
      throw new Error('no metric cards rendered' + (errText ? ' (login error: ' + errText + ')' : ''));
    }
  }));

  // ─── 3. Dashboard cards have real values (not just skeletons) ─────
  results.push(await check('3. Dashboard metric cards have non-empty values', async () => {
    const cards = await page.$$eval('.vp-card.vp-card-sm', els => els.map(card => ({
      label: card.querySelector('.vp-metric-label')?.textContent?.trim(),
      value: card.querySelector('.vp-metric')?.textContent?.trim(),
      sub:   card.querySelector('.vp-metric-sub')?.textContent?.trim(),
    })));
    log('   cards:', JSON.stringify(cards));
    if (cards.length < 4) throw new Error(`expected ≥4 cards, got ${cards.length}`);
    const filled = cards.filter(c => c.value && c.value !== '—' && c.value !== '');
    if (filled.length < 3) throw new Error(`only ${filled.length}/${cards.length} cards have values: ${JSON.stringify(cards)}`);
  }));

  // ─── 4. Customers list has ≥1 row ────────────────────────
  results.push(await check('4. Customers page has ≥1 row in table', async () => {
    await page.goto(CFG.base_url + '/', { waitUntil: 'networkidle0' }).catch(() => {});
    // Click the Customers nav link (text-based; survives copy changes)
    await page.evaluate(() => {
      const links = Array.from(document.querySelectorAll('a, button, [role="tab"]'));
      const t = links.find(el => el.textContent.trim().match(/^Customers$/i));
      if (t) t.click();
    });
    await page.waitForFunction(() => {
      const rows = document.querySelectorAll('table tbody tr');
      return rows.length > 0;
    }, { timeout: CFG.timeouts.selector_ms });
    await waitMs(500);
    const rowCount = await page.$$eval('table tbody tr', els => els.length);
    await shot(page, '04-customers');
    if (rowCount < 1) throw new Error('no customer rows');
  }));

  // ─── 5. + New client modal opens with all fields ─────────
  results.push(await check('5. + New client modal opens with 11 fields', async () => {
    // Close any open modal first
    await page.evaluate(() => {
      document.querySelectorAll('.vp-modal-bg').forEach(m => m.remove());
    });
    // Click the + New client button (text-based)
    const clicked = await page.evaluate(() => {
      const btns = Array.from(document.querySelectorAll('button'));
      const target = btns.find(b => /\+\s*New\s*client/i.test(b.textContent));
      if (target) { target.click(); return true; }
      return false;
    });
    if (!clicked) throw new Error('+ New client button not found');
    await page.waitForFunction(() => {
      return document.querySelectorAll('#vp-new-client-form input, #vp-new-client-form select').length >= 11;
    }, { timeout: CFG.timeouts.selector_ms });
    await waitMs(400);
    await shot(page, '05-modal');
    const fieldCount = await page.$$eval(
      '#vp-new-client-form input, #vp-new-client-form select',
      els => els.length,
    );
    if (fieldCount < 11) throw new Error(`only ${fieldCount} fields visible in modal`);
  }));

  // ─── 6. Collision warning fires for Zayd/Zayd-iphone ─────
  results.push(await check('6. Collision warning shows for Zayd / Zayd-iphone', async () => {
    await page.click('#vp-nc-name',  { clickCount: 3 }); await page.keyboard.press('Backspace');
    await page.click('#vp-nc-device', { clickCount: 3 }); await page.keyboard.press('Backspace');
    await page.type('#vp-nc-name',   'Zayd');
    await page.type('#vp-nc-device', 'Zayd-iphone');
    await waitMs(400);
    const warn = await page.$('#vp-nc-device-warn');
    if (!warn) throw new Error('#vp-nc-device-warn not in DOM');
    const visible = await page.evaluate(el => el.style.display !== 'none' && el.textContent.trim().length > 0, warn);
    if (!visible) throw new Error('warning element exists but is empty/hidden');
    const txt = await page.$eval('#vp-nc-device-warn', el => el.textContent);
    if (!/rejected|will be|duplicates|starts with/i.test(txt)) {
      throw new Error('warning text unexpected: ' + txt);
    }
    // Submit button must be disabled
    const disabled = await page.$eval('#vp-nc-submit', el => el.disabled);
    if (!disabled) throw new Error('submit button NOT disabled despite collision warning');
    await shot(page, '06-collision');
  }));

  // ─── 7. Operator row shows "no cap" pill (v1.2.7.3) ──────
  results.push(await check('7. Operator row shows "no cap" pill (not "unlimited")', async () => {
    // Close modal
    await page.evaluate(() => {
      document.querySelectorAll('.vp-modal-bg').forEach(m => m.remove());
    });
    await waitMs(300);
    // Re-navigate to customers
    await page.goto(CFG.base_url + '/', { waitUntil: 'networkidle0' });
    await page.evaluate(() => {
      const links = Array.from(document.querySelectorAll('a, button, [role="tab"]'));
      const t = links.find(el => el.textContent.trim().match(/^Customers$/i));
      if (t) t.click();
    });
    await page.waitForFunction(() => document.querySelectorAll('table tbody tr').length > 0,
                              { timeout: CFG.timeouts.selector_ms });
    await waitMs(400);
    const text = await page.evaluate(() => document.body.innerText);
    // innerText respects CSS text-transform: uppercase (the vp-usage-tag pill),
    // so rendered "NO CAP" must be matched case-insensitively against "no cap".
    const textLower = text.toLowerCase();
    if (!textLower.includes('no cap')) {
      throw new Error('"no cap" string not visible anywhere on customers page');
    }
    if (/\bunlimited\b/i.test(text)) {
      throw new Error('literal "unlimited" still rendered (v1.2.7.3 regression)');
    }
    await shot(page, '07-nocap');
  }));

  // ─── 8. Customer detail shows real bytes for operator ────
  results.push(await check('8. Operator customer detail shows used bytes (not 0.0%)', async () => {
    // Find the operator row by looking for the 'operator' tier badge.
    // Display name "Zun (operator)" doesn't contain slug "zun-operator" — the
    // badge in the Tier cell is the stable selector.
    const clicked = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('table tbody tr'));
      const opRow = rows.find(r => {
        const tierCell = r.querySelector('td[data-label="Tier"]');
        return tierCell && /operator/i.test(tierCell.textContent);
      });
      if (!opRow) return false;
      opRow.click();
      return true;
    });
    if (!clicked) throw new Error('operator row not found in customers table');
    await page.waitForFunction(() => {
      return Array.from(document.querySelectorAll('.vp-metric-label'))
        .some(el => el.textContent.trim() === 'Used');
    }, { timeout: CFG.timeouts.selector_ms });
    await waitMs(500);
    const cards = await page.$$eval('.vp-card.vp-card-sm', els => els.map(card => ({
      label: card.querySelector('.vp-metric-label')?.textContent?.trim(),
      value: card.querySelector('.vp-metric')?.textContent?.trim(),
      sub:   card.querySelector('.vp-metric-sub')?.textContent?.trim(),
    })));
    log('   detail cards:', JSON.stringify(cards));
    const usedCard = cards.find(c => c.label === 'Used');
    if (!usedCard) throw new Error('no "Used" card on customer detail');
    // value should be a byte string (e.g. "0 B", "1.4 GB") NOT "0.0%"
    if (/^[\d.]+%$/.test(usedCard.value)) throw new Error(`Used card shows pct string "${usedCard.value}" (regression — v1.2.7.3 fix missing)`);
    // sub should mention "no cap" or "tracking" for operator (case-insensitive)
    if (!/no cap|tracking/i.test(usedCard.sub || '')) {
      throw new Error(`Used card sub doesn't say "no cap / tracking": "${usedCard.sub}"`);
    }
    await shot(page, '08-operator-detail');
  }));

  // === v1.2.13 — bulk operations ===
  results.push(await check('9. Bulk action bar appears when rows selected (checkboxes present)', async () => {
    // We may already be logged in from a prior test — clear cookies + storage so we see the login form
    const client = await page.target().createCDPSession();
    await client.send('Network.clearBrowserCookies');
    await client.send('Network.clearBrowserStorage').catch(() => {});
    await page.goto(CFG.base_url + '/', { waitUntil: 'networkidle0' });
    await shot(page, '09a-pre-login');
    await loginUi(page);
    await shot(page, '09b-after-login');
    await page.waitForSelector('.vp-metric', { timeout: CFG.timeouts.selector_ms });
    // Go to Customers
    await page.waitForSelector('.vp-nav-tab', { timeout: CFG.timeouts.selector_ms });
    const navItems = await page.$$('.vp-nav-tab');
    let custNav = null;
    for (const it of navItems) {
      const txt = await page.evaluate(el => el.textContent || '', it);
      if (/customers/i.test(txt)) { custNav = it; break; }
    }
    if (!custNav) throw new Error('Customers nav not found');
    await custNav.click();
    await page.waitForSelector('.vp-tbl-wrap tbody tr', { timeout: CFG.timeouts.selector_ms });
    // Verify checkboxes exist in thead
    const thCheck = await page.$('.vp-th-check .vp-check');
    if (!thCheck) throw new Error('header checkbox missing');
    // Verify checkboxes exist in tbody (non-operator rows have them, operator has disabled)
    const checks = await page.$$('.vp-td-check .vp-check');
    const disabled = await page.$$('.vp-td-check .vp-check-disabled');
    log('   row checkboxes:', checks.length, 'operator disabled icons:', disabled.length);
    if (checks.length < 1) throw new Error('no row checkboxes found');
    // Tick the first 2 row checkboxes via direct property manipulation + dispatchEvent
    // (puppeteer .click() on a checkbox flips DOM .checked but may not fire onchange
    //  reliably in puppeteer's synthetic event flow). Use evaluate() instead.
    await page.evaluate(() => {
      const cbs = document.querySelectorAll('.vp-td-check .vp-check');
      if (cbs[0]) {
        cbs[0].checked = true;
        cbs[0].dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
    await new Promise(r => setTimeout(r, 400));
    await page.evaluate(() => {
      const cbs = document.querySelectorAll('.vp-td-check .vp-check');
      if (cbs[1]) {
        cbs[1].checked = true;
        cbs[1].dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
    await new Promise(r => setTimeout(r, 400));
    // Bulk action bar should appear
    await shot(page, '09c-after-checkbox');
    const bar = await page.$('.vp-bulk-bar');
    if (!bar) throw new Error('bulk action bar did not appear after selecting rows');
    const count = await page.evaluate(el => el.textContent || '', await page.$('.vp-bulk-count'));
    if (!/2 selected/.test(count)) throw new Error(`bulk count wrong: "${count}"`);
    const buttons = await page.$$('.vp-bulk-bar button');
    const btnTexts = await Promise.all(buttons.map(b => page.evaluate(el => el.textContent || '', b)));
    log('   bulk bar buttons:', btnTexts);
    if (btnTexts.length !== 5) throw new Error(`expected 5 bulk buttons (Clear, Archive, Unarchive, Change tier, Delete), got ${btnTexts.length}`);
    await shot(page, '09-bulk-bar');
  }));

  results.push(await check('10. Bulk archive + delete via API (full lifecycle, EAP cleanup)', async () => {
    const token = await loginApi(CFG.base_url);
    const created = [];
    for (let i = 1; i <= 2; i++) {
      const r = await fetch(new URL('/api/customers', CFG.base_url).toString(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Cookie: `session=${token}` },
        body: JSON.stringify({ name: `smoke-bulk-${i}`, display_name: `Smoke Bulk ${i}`, tier_name: 'tier_3gb', device_name: `dev${i}`, device_type: 'Linux' }),
      });
      const j = await r.json();
      if (!j.customer || !j.customer.id) throw new Error(`create #${i} failed: ${JSON.stringify(j)}`);
      created.push(j.customer.id);
    }
    log('   created temp customers:', created);
    const arch = await fetch(new URL('/api/customers/bulk-action', CFG.base_url).toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Cookie: `session=${token}` },
      body: JSON.stringify({ action: 'archive', customer_ids: created }),
    });
    const archJ = await arch.json();
    if (!archJ.ok) throw new Error(`bulk archive failed: ${JSON.stringify(archJ)}`);
    if (archJ.affected.length !== 2) throw new Error(`expected 2 affected, got ${archJ.affected.length}`);
    log('   archived:', archJ.affected.map(a => a.name));
    const def = await (await fetch(new URL('/api/customers', CFG.base_url).toString(), { headers: { Cookie: `session=${token}` } })).json();
    const arc = await (await fetch(new URL('/api/customers?include_archived=1', CFG.base_url).toString(), { headers: { Cookie: `session=${token}` } })).json();
    if (def.some(c => created.includes(c.id))) throw new Error('archived customers still in default list');
    if (!arc.some(c => created.includes(c.id) && c.status === 'archived')) throw new Error('archived customers not in archived list');
    const del = await fetch(new URL('/api/customers/bulk-action', CFG.base_url).toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Cookie: `session=${token}` },
      body: JSON.stringify({ action: 'delete', customer_ids: created, confirm: 'DELETE 2 CUSTOMERS' }),
    });
    const delJ = await del.json();
    if (!delJ.ok) throw new Error(`bulk delete failed: ${JSON.stringify(delJ)}`);
    if (delJ.affected.length !== 2) throw new Error(`expected 2 affected, got ${delJ.affected.length}`);
    if (delJ.eap_blocks_removed !== 2) throw new Error(`expected 2 EAP blocks removed, got ${delJ.eap_blocks_removed}`);
    log('   cleanup ok: deleted', delJ.affected.length, 'and removed', delJ.eap_blocks_removed, 'EAP blocks');
  }));

  await browser.close();

  const passed = results.filter(r => r.pass).length;
  const failed = results.length - passed;
  const dt = ((Date.now() - t0) / 1000).toFixed(1);

  console.log('');
  console.log('━'.repeat(62));
  console.log(`Result: ${passed} passed, ${failed} failed (${dt}s)`);
  console.log('━'.repeat(62));

  if (failed > 0) {
    console.log('');
    console.log('Failures:');
    results.filter(r => !r.pass).forEach(r => console.log(`  ❌ ${r.name}\n     ${r.err}`));
    process.exit(1);
  }
  console.log(`✅ All ${passed} checks passed.`);
  console.log(`Screenshots: ${path.resolve(CFG.screenshots_dir)}/`);
  process.exit(0);
})().catch(e => {
  console.error('FATAL:', e.message);
  if (VERBOSE) console.error(e.stack);
  process.exit(2);
});
