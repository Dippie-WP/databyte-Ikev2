# Security Review Summary — 2026-06-27

A sub-agent code review was conducted on the portal code at v1.9.1.
The full report (with infrastructure details) is kept in workspace
memory for privacy. This file is the public-safe summary.

## Findings (10 total)

| Sev | Count | Status |
|-----|-------|--------|
| 🔴 CRITICAL | 3 | All fixed in `v1.7.0-recovered` |
| 🟠 HIGH     | 4 | 1 fixed (gitignore), 3 pending |
| 🟡 MEDIUM   | 3 | Pending |

## CRITICAL findings (all resolved)

1. **`portal_auth.require_operator_session` missing** — `app.py`
   imported a function that didn't exist in `portal_auth.py`. Portal
   would not start. Fixed by resetting to pre-replay commit that
   has the full operator-auth subsystem.
2. **`installer_tokens.py` missing** — `app.py` imported a module
   that didn't exist in git. Portal would not start. Fixed by the
   same reset (file restored from pre-replay commit).
3. **Secrets file not gitignored** — `.env.xneelo` containing live
   credentials was `git add`-able. Fixed by adding `.env.*` pattern
   to `.gitignore`.

## HIGH/MEDIUM findings (pending — see INCIDENT-2026-06-27.md)

- HIGH #4: brittle import-time table setup
- HIGH #5: deploy validator incomplete
- HIGH #7: f-string SQL defense-in-depth
- MED #8: cookie SameSite lax→strict
- MED #9: session cleanup loop missing try/except

## What the review process found that wasn't in the code

- The destructive replay commit `e4a4673` was self-contradictory:
  its message claimed "the resulting tree is identical to f4ea70c"
  but the diff was 117 files / 15,449 lines deleted. This is the
  canonical example of why commit messages should not be trusted
  over actual file diffs.
- The live production VPS was running a DIFFERENT `portal_auth.py`
  than git HEAD — git SHA `22d45630…` (deployed, working) vs
  `b3a4008c…` (HEAD, broken). This desync went unnoticed because
  the deploy SHA-verify script didn't include `portal_auth.py`.
  This is now fixed.
