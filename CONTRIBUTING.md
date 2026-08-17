# Contributing / Deploy Policy — databyte-Ikev2

> **Policy in effect from 2026-08-17** (per Zun msg #36714, option (a)):
> **NO LIVE-only edits.** Every change to `/opt/vpn-portal/`, `/home/zunaid/strongswan/`,
> or any other LIVE path MUST go through git before it hits production.

## Why this rule exists

The original 5-day drift (Phase 4B RADIUS migration deployed directly to
`/opt/vpn-portal/app.py` without a commit) triggered the email cascade that
started this session's work. The follow-on ci workflow failure was caused by
the same gap (LIVE changes ≠ tests). Root cause in both cases: **production
state can diverge from git HEAD**.

We fix this at the workflow level, not by adding more drift-detect alarms.

## The rule

1. **Edit in your working tree first.** All code changes start in
   `/root/projects/strongswan-vpn-gateway/` (or whatever local clone).
2. **Commit + push** before the change is deployed to LIVE.
3. **Deploy FROM git** — never from a hand-edited LIVE file. Use `git pull`
   on the LIVE box (or your normal deploy mechanism) to pull the new HEAD.
4. **If a LIVE emergency needs an immediate fix**, commit it to a hotfix
   branch first, push, then apply. The branch exists before the LIVE edit.

## Enforcement (3 layers, in order of speed)

### Layer 1 — Pre-push git hook (instant, local)

`.githooks/pre-push` runs `./tools/ci-drift-detect.sh` + `pytest tests/` before
every `git push`. If either fails, the push is blocked.

Set up on a fresh clone:
```bash
git config core.hooksPath .githooks
```

Bypass (use sparingly + document why in the commit body):
```bash
git push --no-verify
```

### Layer 2 — Drift-detect CI on push (seconds)

`.github/workflows/drift-detect.yml` runs on every push + every 5min cron.
If LIVE files differ from HEAD, the workflow fails and Zun gets an email.
The push-trigger catches drift within seconds of pushing.

### Layer 3 — Manual deploy discipline

If you MUST deploy outside of git (true emergency, CI broken, etc.):
- Log the change in `#deploy-log` channel with timestamp + reason
- Commit + push the same change within the same work block
- If the change isn't committed within the work block, the next drift
  detect run will catch it and the deploy log provides the audit trail.

## When this rule doesn't apply

- VPS-side infra changes that aren't in this repo (e.g., `/etc/freeradius/`,
  systemd units). Those are managed separately.
- One-shot scripts that don't touch a watched file.

## What happens if you break the rule

1. Pre-push hook blocks you (if you tried to push).
2. If you bypass with `--no-verify` and the LIVE edit isn't committed
   within the same session, the next drift-detect run catches it.
3. Zun gets a notification. We review together.

## Files in scope (watched by drift-detect)

See `tools/ci-drift-detect.sh` `FILES` array. Currently:
- `host/vpn-portal/app.py`
- `host/vpn-portal/www/portal/index.html`
- `quota/quota-monitor.py`
- `quota/bandwidth-monitor.py`
- `host/backup/backup-vpn-portal-config.sh` (deploy to add)
- `ops/rotate-vpn-credentials.py` (deploy to add)
- `quota/update_rw_eap_conf.py`

If you add a new LIVE-deployable file, add it to `FILES` + `LIVE_PATHS`
in the same commit. This is the only way drift-detect will catch it.

## History

- **2026-08-17**: Initial policy. Triggered by 5-day drift + ci test failure
  caused by LIVE-only deploys not synced to git.
