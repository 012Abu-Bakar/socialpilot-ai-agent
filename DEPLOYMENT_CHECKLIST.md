# SocialPilot AI — Deployment & Security Checklist

## Included in this build
- SQLite persistence (WAL mode) for drafts, workflows, credits, schedules, automation rules and execution history.
- Mock generation by default. No provider calls are made while `SOCIALPILOT_MOCK_MODE=1`.
- Approval enforced server-side; scheduling only accepts approved drafts and copies their stored content and platform.
- Atomic credit checks; AI-mode credits are reserved before the provider call and released on failure.
- Input validation with length limits and allow-lists for platform, tone and objective.
- Cross-site write blocking, trusted-host allow-list, CSP and other security headers.
- In-memory per-client rate limit for generation and automation runs.
- Docker image runs as a non-root user, copies only `main.py` and `static/`, stores data in `/data`, and has a health check. `.dockerignore` keeps `.env` and local data out of the build context.
- Compose binds to `127.0.0.1` only.
- Health endpoint: `/api/health`. Local demo analytics: `/api/analytics`.

## Before exposing this app publicly
- [x] Authentication (scrypt passwords, HttpOnly SameSite=Strict session cookie), per-business data ownership, and editor / approver / owner roles.
- [ ] Set `SOCIALPILOT_SECURE_COOKIES=1` behind HTTPS so the session cookie is only sent over TLS.
- [ ] Add email verification, password reset and email invitations for teammates.
- [ ] Consider CSRF tokens in addition to the SameSite=Strict cookie and the Origin / Sec-Fetch-Site check.
- [ ] Replace the in-memory rate limiter with a shared one (e.g. Redis) if running more than one process.
- [ ] Replace SQLite with managed PostgreSQL for multi-user deployment.
- [ ] Add migrations (e.g. Alembic) instead of relying on table creation at startup.
- [ ] Use a secrets manager; never commit `.env` or API keys. Rotate any key that was ever baked into an image built from an older Dockerfile.
- [ ] Configure HTTPS, `SOCIALPILOT_ALLOWED_HOSTS` for the real domain, and CORS deliberately.
- [ ] Add backup/restore and retention policies for the database.
- [ ] Add structured logs and monitoring/alerting.
- [ ] Live social publishing (OAuth + platform API publishers; only demo channels exist), webhook verification, and external analytics remain unimplemented.
- [ ] Run the publisher as one dedicated worker; with several web processes, each one runs the background publisher (the claim step prevents double posting, but one worker is simpler to operate).

## Docker demo
```bash
docker compose up --build
```
Then open http://localhost:8000. Demo mode stays enabled and provider calls are disabled.

## Local run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
