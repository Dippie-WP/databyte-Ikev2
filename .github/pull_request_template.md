## What does this PR do?

<!-- One-line summary of the change. -->

## Why?

<!-- What problem does this solve? What was the trigger? -->

## Type of change

- [ ] Bug fix (non-breaking, fixes an issue)
- [ ] New feature (non-breaking, adds capability)
- [ ] Breaking change (would break existing setup, requires user action)
- [ ] Documentation / runbook update
- [ ] CI / pipeline change
- [ ] Refactor (no behavior change)

## Testing

<!-- What did you test? How? -->

- [ ] Built image locally: `bash scripts/build-image.sh` (or specify tag)
- [ ] Ran smoke tests against the new image (charon version, plugins, strongswan.conf, start.sh)
- [ ] Tested on a real client (Android / iPhone / Windows / Linux)
- [ ] Updated README / docs to match

If N/A for any of the above, explain why:

<!-- 
For example: "This is a docs-only change, no image rebuild needed"
-->

## Checklist

- [ ] `git log` is clean (no debug commits, no half-done work)
- [ ] Commit messages explain **why**, not just **what**
- [ ] No secrets, certs, or `.pem` files in the diff (check `git status` + diff carefully)
- [ ] No changes to `docker/swanctl/x509ca/`, `docker/swanctl/x509/`, or `docker/swanctl/private/` (those are runtime certs, not source)
- [ ] README.md updated if behavior or ops changed
- [ ] No unrelated changes bundled in

## Risk assessment

<!-- One of: Low / Medium / High — explain why -->

- **Risk:** <!-- Low / Medium / High -->
- **Reasoning:** <!-- What could break? Who is affected? What's the rollback? -->

## Backout plan

<!-- How do we revert this if it goes wrong? -->

- [ ] Revert the merge commit
- [ ] Roll back to previous image tag: `docker tag <previous-tag> zun/strongswan:6.0.7-mschapv2-attrsql`
- [ ] Other: <!-- specify -->

## Related

<!-- Link related issues, docs, sessions, or other PRs -->

- Docs:
- Memory:
- Phase:
