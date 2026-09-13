# Email Access Implementation Plan

**Goal:** Restrict SGO to an email allowlist, expire logins after 24 hours, and constrain test usage.
**Architecture:** SQLite auth store plus SES transport; ASGI security boundary on FastAPI; same-origin browser login and session cookies.
**Tech Stack:** Python, FastAPI, SQLite, boto3, vanilla browser JS, Docker/Caddy.

1. Write failing `tests/test_auth.py`: allowlist, expiration, retries, replay, revocation, persistent quotas and concurrent verification. Run unittest; implement `web/auth.py` and local administration command; re-run.
2. Write failing `tests/test_security.py`: anonymous API denial, ownership, CSRF, unsafe LLM override, bounded body/workload, admin-only datasets and fixed expiry. Implement `web/security.py` and wire `web/app.py`; re-run.
3. Add login page and authenticated fetch wrapper, logout and small test defaults. Verify existing UI tests and browser behavior.
4. Add private production container configuration and documented SES/allowlist/backup/HTTPS setup; preserve user's existing `.env.example` changes and untracked lockfile.
5. Run all Python/JS tests and compile checks; request independent security code review, fix findings, re-run affected checks. Record residual deployment validation explicitly.
