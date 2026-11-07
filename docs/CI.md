# Continuous Integration

Generic CI workflow docs for a self-hosted VPN portal. Documents the `portal-smoke.yml` GitHub Actions workflow that runs a headless-browser smoke test against a staging portal instance.

> **Hand-curated 2026-10-17 for generic use.** All databyte-specific references (paths, passwords, bug class numbers, hostnames) replaced with `<placeholders>`. Verify all values on your own deploy before running.

---

## portal-smoke

The `.github/workflows/portal-smoke.yml` workflow runs the headless-browser smoke test on every push to `main` and on every PR. It catches the class of bug where the API works but the UI renders an empty DOM — something API-only tests cannot detect.

### Required GitHub Secrets

| Secret | Value | Example |
|---|---|---|
| `PORTAL_URL` | URL of a staging portal instance | `http://staging.<your-domain>:8080` |
| `PORTAL_ADMIN_USER` | Admin username | `<your-admin-username>` |
| `PORTAL_ADMIN_PASS` | Admin password | `<your-admin-password>` (see your secrets manager) |

### Optional Inputs

| Input | Default | Notes |
|---|---|---|
| `PORTAL_SMOKE_TIMEOUT_MS` | 20000 | Per-check selector wait. Lower for fast staging, higher for prod. |

### Where it runs

`ubuntu-latest` runner. Installs Chromium + puppeteer-core, then runs the smoke test. Screenshots upload as artifacts (3-day retention on success, 14-day on failure).

### Triggering a manual run

1. GitHub → Actions → portal-smoke → Run workflow
2. Optionally override `portal_url` and pass `extra_args`

### Concurrency

Same-branch pushes cancel in-flight runs (latest is the one that matters).

### Wiring up the staging portal

**The smoke test needs a real portal running on a URL the runner can reach.** Options:

1. **Public staging** (easiest): open a port on your network's edge, set up a free Cloudflare Tunnel (or any tunnel service) from `staging.<your-domain>`, point the secret there. Runner hits the public URL.
2. **Self-hosted runner on your network** (cleanest): spin up a dedicated CI runner VM/container inside your network. Register a GitHub Actions runner there. Runner hits the portal directly via internal IP (e.g. `http://<internal-ip>:8080`). No port forwarding.

### What the smoke test checks

The script (`tools/portal-smoke.js`) launches a headless Chromium against `PORTAL_URL` and verifies:
- Login page renders with form elements (not empty DOM)
- Admin login with `PORTAL_ADMIN_USER`/`PORTAL_ADMIN_PASS` succeeds
- Dashboard page renders customer list / quota table / etc.
- API endpoints (`/api/health`, `/api/customers`) return 200
- Logout succeeds and session clears

### Screenshots and artifacts

Screenshots are uploaded as workflow artifacts on every run. If a screenshot shows an empty white page or error message, that's the bug class this test catches — inspect the DOM and console for clues.

### Local testing (before pushing)

```bash
# In tools/:
node portal-smoke.js http://localhost:8080 admin <your-admin-password>
```

Expect: green output, exit 0, no errors. If red, fix before pushing.

---

## Other workflows (in `.github/workflows/`)

This repo may have additional CI workflows for:
- **ci.yml** — unit tests, linting
- **drift-detect.yml** — detects manual LIVE edits to server config
- **release.yml** — tagged releases

Check `.github/workflows/` for the current list. Each is generic in structure — secrets as inputs, runner info, concurrency rules. Adapt to your own deploy.

---

## References

| Source | Purpose |
|---|---|
| [GitHub Actions docs](https://docs.github.com/en/actions) | Workflow syntax, secrets, runners |
| [puppeteer-core](https://pptr.dev/) | Headless Chrome automation API |
| [Chromium](https://www.chromium.org/) | The browser puppeteer drives |
| Your portal's `tools/README.md` | How to add new smoke tests |

---

**Hand-curated 2026-10-17 for generic use.** This doc is the CI workflow guide for the self-hosted VPN portal. For the master build manual, see [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md). For the architecture overview, see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).
