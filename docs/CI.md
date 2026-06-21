# Continuous Integration

## portal-smoke (v1.2.8+)

The `.github/workflows/portal-smoke.yml` workflow runs the
[headless-browser smoke test](tools/README.md) on every push to `main`
and on every PR. It catches the v1.2.7.1 class of bug (API works but
the UI renders empty DOM) that API-only tests cannot detect.

### Required GitHub Secrets

| Secret | Value | Example |
|---|---|---|
| `PORTAL_URL` | URL of a staging portal instance | `http://staging.vpn.homelab.local:8080` |
| `PORTAL_ADMIN_USER` | Admin username (currently `admin`) | `admin` |
| `PORTAL_ADMIN_PASS` | Admin password (currently `totalconnect`) | (see TOOLS.md) |

### Optional Inputs

| Input | Default | Notes |
|---|---|---|
| `PORTAL_SMOKE_TIMEOUT_MS` | 20000 | Per-check selector wait. Lower for fast staging, higher for prod. |

### Where it runs

`ubuntu-latest` runner. Installs Chromium + puppeteer-core, then
`node tools/portal-smoke.js`. Screenshots upload as artifacts (3-day
retention on success, 14-day on failure).

### Triggering a manual run

1. GitHub → Actions → portal-smoke → Run workflow
2. Optionally override `portal_url` and pass `extra_args`

### Concurrency

Same-branch pushes cancel in-flight runs (latest is the one that
matters).

### Wiring up the staging portal

**The smoke test needs a real portal running on a URL the runner can
reach.** Options:

1. **Public staging** (easiest): open a port on the homelab router, set
   up a free Cloudflare Tunnel from `vpn-staging.homelab.local`, point
   the secret there. Runner hits the public URL.
2. **Self-hosted runner on LXC 902** (cleanest): register a GitHub
   Actions runner inside the homelab network. Runner hits
   `http://192.168.10.98:8080` directly. No port forwarding.
3. **WireGuard tunnel to the runner** (most secure): spin up a
   WireGuard endpoint on the homelab, the runner connects in, hits the
   portal over the tunnel.

**Currently: option 1 or 2 required before the workflow can be turned
on.** Without one of these, the secrets exist but the workflow will
fail at the `node portal-smoke.js` step with `ENOTFOUND` / `ECONNREFUSED`.

### Running locally

Same as in production:

```bash
cd tools
npm install
PORTAL_URL=http://192.168.10.98:8080 node portal-smoke.js
# or: PORTAL_URL=http://192.168.10.98:8080 npm run smoke:live
```

Exit 0 = ship, exit 1 = read which check failed + look at
`tools/screenshots/`.

### History

- **v1.2.8** (2026-06-21) — workflow file created. Workflow not yet
  enabled (no staging portal). Manual local runs against LXC 903
  (192.168.10.98) are the current verification path.
