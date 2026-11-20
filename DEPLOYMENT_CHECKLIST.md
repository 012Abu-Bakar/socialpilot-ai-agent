# SocialPilot AI — Deployment & Security Checklist

## Included in this build
- SQLite persistence for drafts, workflows, credits, schedules, automation rules and execution history.
- Mock generation by default. No provider API calls are made while `SOCIALPILOT_MOCK_MODE=1`.
- Dockerfile and Docker Compose for a local container run.
- Health endpoint: `/api/health`.
- Local demo analytics endpoint: `/api/analytics`.

## Before exposing this app publicly
- [ ] Implement authentication and authorization (not included in this prototype).
- [ ] Add per-user/workspace data ownership and permissions.
- [ ] Replace SQLite with managed PostgreSQL for multi-user deployment.
- [ ] Add CSRF protections if cookie-based sessions are introduced.
- [ ] Add rate limits for generation and automation endpoints.
- [ ] Validate platform values against an allowlist.
- [ ] Add secrets management; never commit `.env` or API keys.
- [ ] Configure HTTPS, trusted hosts, secure headers, and CORS deliberately.
- [ ] Add migrations (e.g. Alembic) instead of relying on table creation at startup.
- [ ] Add backup/restore and retention policies for the database.
- [ ] Add structured logs and monitoring/alerting.
- [ ] Social OAuth, publishing, real scheduler worker, webhook verification, and external analytics remain unimplemented.

## Docker demo
```bash
docker compose up --build
```
Then open http://localhost:8000. Demo mode remains enabled and provider calls are disabled.

## Local run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
SOCIALPILOT_MOCK_MODE=1 uvicorn main:app --reload
```
