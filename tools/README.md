# tools/ — VPN Portal Tooling

## portal-smoke.js — headless-browser smoke test

Drives a real Chromium against the portal UI and verifies 8 DOM-layer invariants that API-only testing misses. Specifically designed to catch the failure mode that bit **v1.2.7.1** (the `el()` flatten bug — API worked, UI rendered empty DOM, sat broken for ~36h).

### The 8 checks

| # | Check | Would catch |
|---|---|---|
| 1 | `/` renders login form (username + password inputs present) | el() bug, JS syntax error, network failure |
| 2 | POST `/api/login` with admin/totalconnect → 200 + dashboard renders | auth regression |
| 3 | Dashboard metric cards have non-empty values (not just skeletons) | render-side data wiring bugs |
| 4 | Customers page has ≥1 row in table | list render regression |
| 5 | `+ New client` modal opens with ≥11 fields | modal render regression |
| 6 | Type `Zayd` customer slug + `Zayd-iphone` device name → red warning shows + submit disabled | v1.2.7.2 collision-guard regression |
| 7 | Operator row shows `no cap` pill (not literal `unlimited`) | v1.2.7.3 operator-visibility regression |
| 8 | Customer detail `Used` card shows byte string (not `0.0%`), sub says "no cap / tracking" | v1.2.7.3 detail-card regression |

Each check takes a screenshot on success AND failure: `tools/screenshots/smoke-NN-*.png`.

### Setup

```bash
cd tools
npm install                  # ~30s, downloads puppeteer-core only (no Chromium)
# uses system /usr/bin/chromium — no 280MB Chromium download
```

### Run

```bash
# Default (localhost:8080 — useful when vpn-portal runs on the same host)
node tools/portal-smoke.js

# Live LXC 903 portal (what CI / pre-tag should use)
PORTAL_URL=http://192.168.10.98:8080 node tools/portal-smoke.js

# Via npm scripts
npm run smoke --prefix tools
npm run smoke:live --prefix tools      # live LXC 903

# Verbose (logs each pageerror + console.error + screenshot path)
VERBOSE=1 node tools/portal-smoke.js
```

### Exit codes

- `0` — all 8 checks passed
- `1` — at least one check failed (screenshots written for diagnosis)
- `2` — fatal (browser failed to launch, config missing, etc.)

### CI integration (future)

Suggested GitHub Actions step (not wired up yet — flag if you want it):

```yaml
- name: Portal smoke test
  run: PORTAL_URL=http://192.168.10.98:8080 node tools/portal-smoke.js
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: portal-smoke-screenshots
    path: tools/screenshots/
```

### Adding a new check

1. Add a `results.push(await check('N. description', async () => { ... }))` block in `portal-smoke.js`
2. Use stable selectors: `#vp-*` IDs are best, text-based fallback for nav links
3. Always take a screenshot: `await shot(page, 'NN-short-name')`
4. Throw an `Error('reason')` on failure — message becomes the test output
5. Update this README's check table

### Config

`portal-smoke.config.json` — base URL, credentials, Chromium path, timeouts. **Credentials are plaintext** because this is an internal admin test account; if you rotate the password, update this file. Do NOT commit credentials for any non-admin account.

`.gitignore` excludes `tools/node_modules/` and `tools/screenshots/` — Chromium cache and screenshot artifacts aren't source.
