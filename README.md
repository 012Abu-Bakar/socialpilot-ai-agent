# SocialPilot AI — local prototype

AI-style social content studio with drafts, approval flow, a local scheduling calendar, demo usage credits, manual automation rules, integration scaffolds, and local analytics.


## Safe demo mode

**Mock generation is enabled by default.** The application generates deterministic demo content and does not call an AI provider or use an API key. Each mock generation consumes local demo credits (base + variations).

## Features
- Content brief and mock variation generation
- Local credit ledger and workflow runs
- SQLite-backed drafts, editing, deleting, approvals and rejection notes
- Schedule approved drafts into a local calendar (not published)
- Manual automation rules; running a rule creates a clearly marked draft
- Automation execution history
- Integration registry for n8n, LinkedIn, Instagram, and analytics (not connected)
- Analytics based on local database records only
- Docker demo configuration

## Run locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
SOCIALPILOT_MOCK_MODE=1 uvicorn main:app --reload
```
Open `http://127.0.0.1:8000`.

## Run with Docker
```bash
docker compose up --build
```

## API endpoints
- `GET /api/health`
- `GET/POST /api/credits`, `POST /api/credits/reset`
- `POST /api/generate`
- `GET/POST /api/drafts`, `PUT/DELETE /api/drafts/{id}`
- `POST /api/drafts/{id}/submit`, `/approve`, `/reject`
- `GET/POST /api/schedule`, `DELETE /api/schedule/{id}`
- `GET /api/workflows`
- `GET/POST/PUT/DELETE /api/automations`
- `POST /api/automations/{id}/run`
- `GET /api/automation-executions`
- `GET /api/integrations`
- `GET /api/analytics`

## Honest limitations
- No real AI provider calls in default mock mode.
- No social account OAuth or live publishing.
- Scheduling is stored locally; no background scheduler publishes posts.
- Analytics are derived from local demo data, not social network metrics.
- No authentication or multi-user authorization; do not expose this prototype publicly as-is.

See `DEPLOYMENT_CHECKLIST.md` before deployment.
