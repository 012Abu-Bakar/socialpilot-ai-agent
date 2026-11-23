# SocialPilot AI — local prototype

AI-style social content studio for multiple business accounts: team sign-in with roles, drafts, an approval flow, a scheduling calendar with a publishing pipeline (demo channels), demo usage credits, manual automation rules, integration scaffolds, and per-business analytics.

## Safe demo mode

**Mock generation is enabled by default.** The app generates deterministic demo content and does not call an AI provider. Each generation consumes local demo credits (1 base + 1 per variation).

To use the real provider, set `SOCIALPILOT_MOCK_MODE=0` **and** `ANTHROPIC_API_KEY` (see `.env.example`). Calls go through the official `anthropic` SDK; the model defaults to `claude-haiku-5-5` and can be changed with `ANTHROPIC_MODEL`. In AI mode, credits are reserved *before* the provider call (an empty balance never reaches the provider) and released if the call fails; the demo credit reset is disabled.

## Features
- Sign-up and sign-in (scrypt-hashed passwords, HttpOnly SameSite=Strict session cookie)
- Multiple business accounts: each has its own drafts, schedule, channels, automations, analytics and credits; one person can belong to several and switch between them
- Team roles per business: **editor** (create, submit, schedule), **approver** (also approves/rejects), **owner** (also manages the team, channels and credit resets)
- Publishing pipeline: connect a channel per platform, then scheduled posts publish automatically when due (background publisher) or with "Publish now"; failures are recorded with the reason and can be retried
- Content generation (mock or Anthropic), with several variations per brief
- Credit ledger: append-only, so analytics and history stay consistent after a reset
- SQLite-backed drafts with edit, delete, submit, approve and reject (with reason)
- Approval is enforced by the backend: only approved drafts can be scheduled, and editing an approved draft sends it back for approval
- Scheduling approved drafts into a calendar (stored in UTC, shown in local time)
- Manual automation rules; running a rule creates a clearly marked draft that still needs approval
- Integration registry for n8n, LinkedIn, Instagram, and analytics
- Analytics per business, including published and failed posts
- Docker configuration

## Run locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
Open `http://127.0.0.1:8000` and create an account. The first account created on an existing database takes over the data that was there before accounts existed.

### Publishing
Owners connect a **demo channel** for LinkedIn or Instagram under Integrations. A demo channel checks each post the way the platform would (caption length: LinkedIn 3,000, Instagram 2,200 characters) and records it with a `demo-…` post id; **nothing is sent to the social network**. The background publisher checks for due posts every `SOCIALPILOT_PUBLISH_INTERVAL` seconds (default 30; `0` turns it off). A post that was mid-publish when the server stopped is marked failed, never silently re-sent.

Going live means adding a publisher for a new connection mode in `PUBLISHERS` (`main.py`): an OAuth flow to store the account's token, and the platform API call. That needs your own LinkedIn developer app (Community Management API, `w_member_social`) and, for Instagram, a Meta app with an Instagram Business account linked to a Facebook Page; Instagram also only accepts posts with an image or video.

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
Everything except `/api/health` and the auth endpoints needs a signed-in session and works on the session's current business.
- `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/me`
- `POST /api/businesses`, `POST /api/session/business` (switch)
- `GET/POST /api/members`, `PUT/DELETE /api/members/{user_id}`
- `GET/POST /api/connections`, `DELETE /api/connections/{id}`
- `POST /api/schedule/{id}/publish`, `POST /api/publishing/run-due`, `GET /api/publishing`
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
- Publishing goes to demo channels only; live LinkedIn/Instagram publishing needs developer apps (see Publishing).
- Teammates are added by the email of an existing account; there are no email invitations or password resets.
- Automations only run when you press "Run now".
- Analytics are derived from local records, not social network metrics.
- On narrow screens the sidebar (business switcher, sign out) is hidden, as the section navigation already was.

See `DEPLOYMENT_CHECKLIST.md` before deployment.
