# Branch Protection

`main` branch is protected on the `Dippie-WP/databyte-Ikev2` repo via the GitHub REST API.

## Active rules (applied 2026-06-20)

| Rule | Setting | Rationale |
|------|---------|-----------|
| Required PR reviews | 0 (PR required, no human approval) | Solo project — forces branch pattern without friction |
| Linear history | ✅ required | Clean changelog, no merge commits on main |
| Force pushes | ❌ blocked | Protects tagged commits like `v1.1.0` at `bb86b7d` |
| Branch deletion | ❌ blocked | `main` is sacred |
| Status checks | none yet | Add `ci` context once we confirm the workflow runs cleanly |
| Enforce admins | off | Repo admin (Zun) retains direct-push escape hatch for emergencies |

## To upgrade (when ready)

- Bump `required_approving_review_count` to 1 once we have a second reviewer (or for self-approval friction)
- Add `ci` context to `required_status_checks.contexts` after the `ci.yml` workflow runs clean

## Reproduce

```bash
curl -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  -d @docs/branch-protection/main.json \
  https://api.github.com/repos/Dippie-WP/databyte-Ikev2/branches/main/protection
```

## History

- 2026-06-20 — applied via direct REST API. `gh` CLI not installed on OpenClaw host; used curl with token from git remote.
