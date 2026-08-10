# Internal operator docs

**⚠️ Internal-only.** Production deployment details, personal exposure items,
and operational runbooks that shouldn't be on the public front page.

This directory exists to keep the public repo front page (README.md)
clean for community consumption while preserving the operational
artefacts that operators and collaborators need.

## What's here

| File | What's in it |
|---|---|
| `CHANGELOG.md` | Full change log with CORR-IDs, deployment notes, post-mortems. Verbose. |
| `TODO.md` | Internal roadmap + outstanding work items. |
| `SECURITY-TOKENS-REVOKED.md` | History of leaked tokens + remediation timeline. |
| `PRODUCTION-DEPLOY.md` | Production VPS deploy guide (Xneelo hosting — databyte.co.za domains, 10.99.0.0/24 pool, prod endpoints). |
| `EXPOSED-INFO.md` | The exact items that were on the public front page before 2026-08-10 + why they were moved here. |

## What is public-safe

The public front page (`README.md` at repo root) is curated for:
- New users evaluating the stack
- Contributors looking for the repo layout
- CI badges + project status

Public-safe docs (architecture, deployment guides for self-hosters,
DR runbook for any operator) stay in `docs/` at the repo root.
