# Security: Leaked GitHub PATs — Status & Revocation Required

**Date of incident:** 2026-06-23 16:38 UTC
**Detected during:** Multi-device VPN test (Windows PC connection issue triage)
**Severity:** 🔴 **CRITICAL** — leaked token had `delete_repo` scope

---

## TL;DR

A GitHub Personal Access Token (PAT) was committed in plaintext to `.git/config` files and referenced in agent memory files. The token was exposed to:

1. **The workspace backup** (daily 04:00 UTC cron to RustFS — `rustfs:open-claw-push/workspace-backups/`)
2. **Anyone with read access to the homelab backup destination**
3. **Any agent process that ran against the workspace**

**Mitigation taken immediately (2026-06-23 16:40 UTC):**
- Switched all 3 affected repos from `https://oauth2:<PAT>@github.com/...` to `git@github.com:...` (SSH key auth)
- The local SSH key (`/root/.ssh/id_ed25519`, comment `misha@openclaw`) was already deployed to GitHub as the `Dippie-WP` account key
- Sanitized memory files (replaced plaintext tokens with `[REDACTED-PAT-...: see SECURITY-TOKENS-REVOKED.md]`)
- Removed dead PATs from `.git/config` files (`http.extraHeader`, `credential.helper`)
- Removed dead remotes (`r2` S3, `dippie-wp/ops-tracker-fresh` which 404s)

**⚠️ MUST DO NOW (cannot be done by agent):**
Zun must **revoke the active PAT** in the GitHub web UI:
- URL: https://github.com/settings/tokens
- Find token starting with `ghp_FZWP7...`
- Click **Revoke**

The PAT is **still active** until you revoke it. Switching to SSH means we don't USE it anymore, but a malicious actor with the plaintext token still has admin access to both repos including the ability to `delete_repo`.

---

## Tokens discovered (verbatim, with status as of 2026-06-23 16:40 UTC)

### 1. 🔴 ACTIVE — `ghp_FZWP7…1y4I` (REQUIRES MANUAL REVOCATION)

| Property | Value |
|---|---|
| Token (fingerprint) | `ghp_FZWP7…1y4I` |
| Type | Classic PAT (personal access token, classic) |
| Account | `Dippie-WP` (verified via `GET /user`) |
| Scopes | `delete:packages, delete_repo, notifications, read:audit_log, read:user, repo, workflow, write:packages` |
| Permissions on `Dippie-WP/databyte-Ikev2` | `admin: True, maintain: True, push: True, triage: True, pull: True` |
| Permissions on `Dippie-WP/ops-tracker` | `admin: True, maintain: True, push: True, triage: True, pull: True` |
| Leaked in | `/root/projects/strongswan-vpn-gateway/.git/config` (now cleaned), `MEMORY.md`, `memory/2026-06-18.md`, `memory/2026-06-23.md` (2 places), **workspace daily RustFS backup from 2026-06-23** |
| Used to be in URL | `https://oauth2:<REDACTED>@github.com/Dippie-WP/databyte-Ikev2.git` |
| Action | **Revoke NOW**: https://github.com/settings/tokens (search for `ghp_FZWP7…1y4I`) |

### 2. ⚪ DEAD — `ghp_gEQj…nk7` (already revoked by GitHub, but was in plaintext)

| Property | Value |
|---|---|
| Status | HTTP 401 on `GET /user` — **already revoked by GitHub** |
| Was in | `/root/.openclaw/workspace/.git/config` (`[http] extraHeader = Authorization: Bearer ...`) |
| Also in | `memory/.dreams/short-term-recall.json.migrated` (old agent memory, now `.dreams/` excluded from backup) |
| Origin | From old `ops-tracker-fresh` repo push attempts — that repo is 404 (never existed as `dippie-wp/ops-tracker-fresh`) |
| Action | Optional: revoke from web UI if it still appears in your list; it's already dead |

### 3. ⚪ DEAD — `ghp_tt3Z…uza` (already revoked by GitHub, but was in plaintext)

| Property | Value |
|---|---|
| Status | HTTP 401 on `GET /user` — **already revoked by GitHub** |
| Was in | `/root/.openclaw/workspace/ops-tracker/.git/config` (`[credential] helper = "!f() { echo \"password=...\"; }; f"`) |
| Action | Optional: revoke from web UI if it still appears in your list; it's already dead |

---

## Why this is critical

The active PAT has the **`delete_repo` scope**. This means anyone with this token could:

1. `DELETE /repos/Dippie-WP/databyte-Ikev2` — destroy the entire VPN gateway project
2. `DELETE /repos/Dippie-WP/ops-tracker` — destroy the homelab ops tracker
3. Modify any file in either repo
4. Read all private content (audit logs, secrets in any private repo)
5. Modify or delete GitHub Packages
6. Modify Actions workflows (potentially exfiltrate secrets from CI runs)

Even though we no longer USE the token (SSH is the new auth), the plaintext value is in:
- The **workspace daily RustFS backup from 2026-06-23 13:28 UTC** (`rustfs:open-claw-push/workspace-backups/2026-06-23/MEMORY.md` and `.../memory/2026-06-23.md`)
- Any **prior backup** of the strongswan-vpn-gateway `.git` directory
- The **agent's daily memory file** (replaced now, but prior versions of `memory/2026-06-23.md` are in backups)

**Once revoked**, the token returns HTTP 401 immediately, and all copies (local + backup) become harmless.

---

## Rotation procedure (post-revocation, optional)

If you want a fresh PAT for any specific use (e.g., CI):

1. Go to https://github.com/settings/tokens → **Generate new token** → **Classic**
2. **DO NOT** select `delete_repo` or `workflow` unless absolutely required
3. Recommended scopes for typical CI use: `repo`, `read:user`
4. **Save the new token ONLY in a secrets manager** (e.g., HashiCorp Vault, `pass`, GitHub Actions secrets, systemd `LoadCredential=`)
5. **NEVER** put the new token in `.git/config` — use SSH or a credential helper with a secrets-store backend

**Better alternative: stay on SSH for git, use fine-grained tokens for API access only.**

---

## Prevention (already applied)

1. **All 3 repos now use SSH key auth** — no PATs in any URL or config
2. **Dead PATs removed** from `.git/config` files (workspace, ops-tracker)
3. **Memory files sanitized** — replaced with `[REDACTED-PAT-...: see SECURITY-TOKENS-REVOKED.md]` markers
4. **`.dreams/` directory** in `workspace_files_enumerator.py` EXCLUDE_DIRS — never backed up
5. **Daily workspace backup** already runs a content scan for `ghp_` regex (added 2026-06-23 13:13 UTC) — would catch a new leak on next run

### Future hardening (suggested, not yet done)

- [ ] Add pre-commit hook: `git diff --staged | grep -E 'ghp_[A-Za-z0-9]+' && echo "PAT detected, refusing commit" && exit 1`
- [ ] Add a CI check on the strongswan-vpn-gateway repo: scan all branches for `ghp_` strings
- [ ] Add daily `git remote -v` audit to workspace backup script
- [ ] Consider migrating to GitHub App authentication (more granular permissions, no long-lived secrets)

---

## Verification (post-revocation)

After you revoke the token on GitHub, verify with:

```bash
curl -sI -H "Authorization: token <FULL_TOKEN>" https://api.github.com/user
```

(Replace `<FULL_TOKEN>` with the actual value — DO NOT type the full token in chat or commit history. The fingerprint `ghp_FZWP7…1y4I` is safe to reference; the full 40-char string is not.)

Expected response: `HTTP/2 401` (instead of the previous `HTTP/2 200`).

---

**Logged as:** 🟠 **HIGH** finding from post-test audit (item #3 in post-test queue).
**Lesson:** `#97` — Token rotation is the responsibility of the token owner. The agent's job is to surface leaks fast and switch auth methods. (Lesson to be added to `memory/2026-06-23.md` after this commit.)
