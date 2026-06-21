#!/usr/bin/env node
/* v1.3.0 — databyte customer portal headless-browser smoke test.
 *
 * Drives a real Chromium against the customer portal at /portal/ and verifies
 * the customer-only flow end-to-end:
 *   1. /portal/ loads with login form
 *   2. Login with demo-phone credentials succeeds
 *   3. Dashboard renders with tier + usage
 *   4. Logout clears session
 *   5. /api/portal/usage with operator cookie returns 401
 *   6. Customer scope: can't see other customer data
 *
 * Usage:
 *   node tools/portal-customer-smoke.js
 *   PORTAL_URL=http://192.168.10.98:8080 node tools/portal-customer-smoke.js
 *
 * Test credentials (lab only, in the strongSwan DB):
 *   identity = demo-phone
 *   password = E6fkfBK6DvUHkG1jcipJrQ
 *
 * Exit codes: 0 = all checks passed, 1 = at least one failed.
 */

let puppeteer;
let isFullPuppeteer = false;
try {
  puppeteer = require('puppeteer');
  isFullPuppeteer = true;
} catch {
  try {
    puppeteer = require('puppeteer-core');
  } catch {
    console.error('Neither puppeteer nor puppeteer-core is installed.');
    console.error('Run: cd tools && npm install');
    process.exit(2);
  }
}

const PORTAL_URL = process.env.PORTAL_URL || 'http://192.168.0.98:8080';
const IDENTITY = process.env.PORTAL_TEST_IDENTITY || 'demo-phone';
const PASSWORD = process.env.PORTAL_TEST_PASSWORD || 'E6fkfBK6DvUHkG1jcipJrQ';
const BROWSER_PATH = process.env.PUPPETEER_BROWSER || '/usr/bin/chromium';

const results = [];
let failed = 0;

function check(name, ok, detail) {
  const symbol = ok ? '\u2713' : '\u2717';
  console.log(`  ${symbol} ${name}${detail ? ' \u2014 ' + detail : ''}`);
  results.push({ name, ok, detail });
  if (!ok) failed++;
}

async function main() {
  console.log(`\n  Portal: ${PORTAL_URL}`);
  console.log(`  Identity: ${IDENTITY}`);
  console.log(`  Puppeteer: ${isFullPuppeteer ? 'full' : 'core'}`);

  const launchOpts = {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  };
  if (!isFullPuppeteer) {
    launchOpts.executablePath = BROWSER_PATH;
  }

  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  await page.setViewport({ width: 414, height: 896 }); // iPhone XR-ish

  let exitCode = 0;
  try {
    // Check 1: /portal/ loads with login form
    console.log('\n[1] /portal/ loads with login form');
    await page.goto(`${PORTAL_URL}/portal/`, { waitUntil: 'networkidle0' });
    const loginVisible = await page.$('#vp-login-form') !== null;
    const dashHidden = await page.evaluate(() => {
      const d = document.getElementById('vp-dashboard-view');
      return d ? d.classList.contains('vp-hidden') : true;
    });
    check('login form rendered', loginVisible);
    check('dashboard hidden initially', dashHidden);
    if (loginVisible) {
      await page.screenshot({ path: '/root/projects/strongswan-vpn-gateway/tools/screenshots/smoke-portal-01-login.png' });
    }

    // Check 2: login submit
    console.log('\n[2] Login submit');
    await page.type('#vp-identity', IDENTITY);
    await page.type('#vp-password', PASSWORD);
    await page.click('#vp-login-btn');
    // Wait for dashboard
    await page.waitForFunction(
      () => {
        const d = document.getElementById('vp-dashboard-view');
        return d && !d.classList.contains('vp-hidden');
      },
      { timeout: 5000 }
    ).catch(() => {});
    const tierText = await page.$eval('#vp-tier', el => el.textContent.trim());
    const dashVisible = await page.evaluate(() => {
      const d = document.getElementById('vp-dashboard-view');
      return d && !d.classList.contains('vp-hidden');
    });
    check('dashboard visible after login', dashVisible);
    check('tier rendered (non-empty)', tierText && tierText !== 'Loading...', `tier: ${tierText}`);
    await page.screenshot({ path: '/root/projects/strongswan-vpn-gateway/tools/screenshots/smoke-portal-02-dashboard.png' });

    // Check 3: usage meter
    console.log('\n[3] Usage meter renders');
    const meterWidth = await page.$eval('#vp-meter-fill', el => el.style.width || '0%');
    const statsText = await page.$eval('#vp-stats', el => el.textContent.replace(/\s+/g, ' ').trim());
    check('meter width set', meterWidth && meterWidth !== '0%', `width: ${meterWidth}`);
    check('stats show "used/remaining/cap"', statsText.includes('used') && statsText.includes('remaining') && statsText.includes('cap'),
      `stats: ${statsText}`);

    // Check 4: logout
    console.log('\n[4] Logout');
    await page.click('#vp-logout-btn');
    await page.waitForFunction(
      () => {
        const d = document.getElementById('vp-login-view');
        return d && !d.classList.contains('vp-hidden');
      },
      { timeout: 5000 }
    ).catch(() => {});
    const loginVisibleAfterLogout = await page.evaluate(() => {
      const l = document.getElementById('vp-login-view');
      return l && !l.classList.contains('vp-hidden');
    });
    check('login visible after logout', loginVisibleAfterLogout);

    // Check 5: API-level test — operator endpoint with portal cookie
    console.log('\n[5] API isolation: portal cookie cannot hit operator endpoints');
    // Re-login first to get a portal cookie
    await page.type('#vp-identity', IDENTITY);
    await page.type('#vp-password', PASSWORD);
    await page.click('#vp-login-btn');
    await page.waitForFunction(
      () => {
        const d = document.getElementById('vp-dashboard-view');
        return d && !d.classList.contains('vp-hidden');
      },
      { timeout: 5000 }
    ).catch(() => {});

    const operatorResponse = await page.evaluate(async (base) => {
      const r = await fetch(base + '/api/customers', { credentials: 'include' });
      return { status: r.status, body: await r.text() };
    }, PORTAL_URL);
    check('portal cookie -> /api/customers returns 401', operatorResponse.status === 401,
      `got ${operatorResponse.status}`);

    // Check 6: scope test — verify customer_id in usage matches logged-in customer
    console.log('\n[6] Scope: customer_id in /api/portal/usage = demo-customer');
    const usageResp = await page.evaluate(async (base) => {
      const r = await fetch(base + '/api/portal/usage', { credentials: 'include' });
      return { status: r.status, body: await r.json() };
    }, PORTAL_URL);
    check('usage returns 200', usageResp.status === 200);
    check('usage customer_id = 2 (demo-customer)', usageResp.body && usageResp.body.customer_id === 2,
      `got ${usageResp.body && usageResp.body.customer_id}`);

  } catch (err) {
    console.error('  \u2717 ERROR:', err.message);
    failed++;
    exitCode = 1;
    try { await page.screenshot({ path: '/root/projects/strongswan-vpn-gateway/tools/screenshots/smoke-portal-ERR.png' }); } catch {}
  } finally {
    await browser.close();
  }

  console.log(`\n  ${results.length} checks: ${results.length - failed} passed, ${failed} failed`);
  console.log(exitCode === 0 && failed === 0 ? '  PASS' : '  FAIL');
  process.exit(failed === 0 ? 0 : 1);
}

main().catch(err => {
  console.error('Fatal:', err);
  process.exit(2);
});
