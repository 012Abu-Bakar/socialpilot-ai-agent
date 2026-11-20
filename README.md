# SocialPilot AI — local prototype

AI-style social content studio with drafts, an approval flow, a local scheduling calendar, demo usage credits, manual automation rules, integration scaffolds, and local analytics.

## Safe demo mode

**Mock generation is enabled by default.** The app generates deterministic demo content and does not call an AI provider. Each generation consumes local demo credits (1 base + 1 per variation).

To use the real provider, set `SOCIALPILOT_MOCK_MODE=0` **and** `ANTHROPIC_API_KEY` (see `.env.example`). Calls go through the official `anthropic` SDK; the model defaults to `claude-haiku-5-5` and can be changed with `ANTHROPIC_MODEL`. In AI mode, credits are reserved *before* the provider call (an empty balance never reaches the provider) and released if the call fails; the demo credit reset is disabled.

## Features
- Content brief and variation generation (mock or Anthropic)
- Credit ledger: append-only, so analytics and history stay consistent after a reset
- SQLite-backed drafts with edit, delete, submit, approve and reject (with reason)
- Approval is enforced by the backend: only approved drafts can be scheduled, and editing an approved draft sends it back for approval
- Scheduling approved drafts into a local calendar (stored in UTC, shown in local time; not published)
- Manual automation rules; running a rule creates a clearly marked draft that still needs approval
- Integration registry for n8n, LinkedIn, Instagram, and analytics (not connected)
- Analytics based on local database records only
- Docker configuration

## Run locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
Open `http://127.0.0.1:8000`.

## Run with Docker
```bash
docker compose up --build
```
The container listens on `127.0.0.1:8000` only and keeps the database in the `socialpilot_db` volume.

## Tests
```bash
pip install -r requirements-dev.txt
playwright install chromium
pytest
```
API tests use a temporary database and a faked SDK client; UI tests start the app in mock mode. No test calls an AI provider or reads `.env`.

### Recorded end-to-end demo
With the app running (`uvicorn main:app`), drive the real UI and record a video:
```bash
SOCIALPILOT_DEMO_URL=http://127.0.0.1:8000 pytest tests/e2e -v --junitxml=artifacts/socialpilot-demo/junit.xml
```
Output goes to `artifacts/socialpilot-demo/`: `socialpilot-demo.webm`, `screenshots/`, `report.md`, `results.json`. The demo refuses to run unless the server is in demo mode, fails if the browser contacts any outside host, and deletes the drafts, schedule and rule it created (set `DEMO_KEEP_DATA=1` to keep them). `DEMO_HEADED=1` shows the browser; `DEMO_SLOWMO=500` slows it down. Skipped in a plain `pytest` run.

## API endpoints
- `GET /api/health`
- `GET /api/credits`, `POST /api/credits/reset` (demo mode only)
- `POST /api/generate`
- `GET/POST /api/drafts`, `PUT/DELETE /api/drafts/{id}`
- `POST /api/drafts/{id}/submit`, `/approve`, `/reject`
- `GET /api/approvals`
- `GET/POST /api/schedule` (body: `draft_id`, `scheduled_at` with a timezone offset), `DELETE /api/schedule/{id}`
- `GET /api/workflows`
- `GET/POST /api/automations`, `PUT/DELETE /api/automations/{id}`
- `POST /api/automations/{id}/run`
- `GET /api/automation-executions`
- `GET /api/integrations`
- `GET /api/analytics`

## Built-in protections
- Input length limits and allow-lists (platform, tone, objective) on every write endpoint
- Status transitions and credit checks run in `BEGIN IMMEDIATE` transactions; unique indexes back up duplicate checks
- Cross-site write requests are rejected (Origin / `Sec-Fetch-Site`), hosts are allow-listed (`SOCIALPILOT_ALLOWED_HOSTS`), and a strict Content-Security-Policy is sent
- Per-client rate limit on generation and automation runs (`SOCIALPILOT_RATE_LIMIT`, default 30/min)
- Provider errors are logged by status and request id and returned to the browser as a generic 502

## Honest limitations
- No real AI provider calls in default mock mode.
- No social account OAuth or live publishing; "Published posts" is always 0.
- Scheduling is stored locally; no background scheduler publishes posts.
- Automations only run when you press "Run now".
- Analytics are derived from local demo data, not social network metrics.
- **No authentication or multi-user authorization.** Anyone who can reach the server can use every endpoint, so keep it on localhost.

See `DEPLOYMENT_CHECKLIST.md` before deployment.
