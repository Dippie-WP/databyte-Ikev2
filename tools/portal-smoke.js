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
