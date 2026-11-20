# SocialPilot AI — Social Media Automation Dashboard (Demo)

A portfolio prototype for a centralized AI-assisted social media workflow across multiple brands and channels.

## Demo features
- Multi-brand workspace selector (UI demonstration)
- LinkedIn / Instagram content format selection
- Topic-to-caption demo generator with hashtags
- Content list with draft and scheduled states
- Human approval queue
- Demo scheduling queue (no actual platform publishing)
- KPI cards and automation pipeline overview

## Run locally
Open `index.html` in a browser. No build step or dependencies required.

## Important demo limitations
- Caption generation is template-based and runs locally. It is **not connected to an LLM API**.
- Approval, draft saving, and queue actions are in-memory demonstration interactions.
- No social accounts are connected and nothing is published or scheduled on LinkedIn/Instagram.
- KPI values are illustrative demo data.

## Planned architecture for a production version
1. Frontend dashboard (React)
2. Backend API (FastAPI)
3. LLM provider integration for caption, variations, and hashtags
4. Database for brands, content, approvals, schedules, and analytics
5. Workflow orchestrator (n8n or backend worker)
6. OAuth-based social platform connections and platform-compliant publishing
7. Scheduled jobs, retries, audit logs, and analytics ingestion

## Product workflow
Brand/topic input → AI content generation → human approval → scheduling → publishing integration → analytics.
