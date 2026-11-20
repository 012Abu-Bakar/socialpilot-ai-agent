# SocialPilot AI — automated demo report

- Run: 2026-11-20T17:37:27 → 2026-11-20T17:38:00
- Target: http://127.0.0.1:8000 (mode: demo-mock)
- Result: **PASSED** — 19/19 steps passed
- Video: `artifacts/socialpilot-demo/socialpilot-demo.webm`
- Browser requests: 103 total, external hosts: none

| # | Step | Type | Result | Time | Screenshot |
|---|---|---|---|---|---|
| 1 | Dashboard loads in demo mode | Working feature | ✅ passed | 0.94s | [screenshots/01-dashboard-loads-in-demo-mode.png](screenshots/01-dashboard-loads-in-demo-mode.png) |
| 2 | Empty topic is rejected without charging credits | Validation / guard | ✅ passed | 1.4s | [screenshots/02-empty-topic-is-rejected-without-charging-credits.png](screenshots/02-empty-topic-is-rejected-without-charging-credits.png) |
| 3 | Fill in the content brief | Working feature | ✅ passed | 2.43s | [screenshots/03-fill-in-the-content-brief.png](screenshots/03-fill-in-the-content-brief.png) |
| 4 | Generate content (mock provider) | Working feature | ✅ passed | 1.17s | [screenshots/04-generate-content-mock-provider.png](screenshots/04-generate-content-mock-provider.png) |
| 5 | Save the generated content as a draft | Working feature | ✅ passed | 1.12s | [screenshots/05-save-the-generated-content-as-a-draft.png](screenshots/05-save-the-generated-content-as-a-draft.png) |
| 6 | Submit for approval (draft becomes locked) | Working feature | ✅ passed | 1.13s | [screenshots/06-submit-for-approval-draft-becomes-locked.png](screenshots/06-submit-for-approval-draft-becomes-locked.png) |
| 7 | Reviewer rejects with a reason | Working feature | ✅ passed | 1.68s | [screenshots/07-reviewer-rejects-with-a-reason.png](screenshots/07-reviewer-rejects-with-a-reason.png) |
| 8 | Author edits the rejected draft | Working feature | ✅ passed | 1.7s | [screenshots/08-author-edits-the-rejected-draft.png](screenshots/08-author-edits-the-rejected-draft.png) |
| 9 | Resubmit and approve | Working feature | ✅ passed | 1.39s | [screenshots/09-resubmit-and-approve.png](screenshots/09-resubmit-and-approve.png) |
| 10 | A past schedule time is rejected | Validation / guard | ✅ passed | 1.68s | [screenshots/10-a-past-schedule-time-is-rejected.png](screenshots/10-a-past-schedule-time-is-rejected.png) |
| 11 | Schedule the approved draft for Mon 23 Nov, 10:30 | Working feature | ✅ passed | 1.96s | [screenshots/11-schedule-the-approved-draft-for-mon-23-nov-10-30.png](screenshots/11-schedule-the-approved-draft-for-mon-23-nov-10-30.png) |
| 12 | Scheduled post appears on the calendar | Working feature | ✅ passed | 0.88s | [screenshots/12-scheduled-post-appears-on-the-calendar.png](screenshots/12-scheduled-post-appears-on-the-calendar.png) |
| 13 | Create an automation rule | Working feature | ✅ passed | 3.23s | [screenshots/13-create-an-automation-rule.png](screenshots/13-create-an-automation-rule.png) |
| 14 | Run the automation manually | Working feature | ✅ passed | 1.14s | [screenshots/14-run-the-automation-manually.png](screenshots/14-run-the-automation-manually.png) |
| 15 | Disabled rules cannot run | Validation / guard | ✅ passed | 1.14s | [screenshots/15-disabled-rules-cannot-run.png](screenshots/15-disabled-rules-cannot-run.png) |
| 16 | Analytics reflect every action | Working feature | ✅ passed | 1.13s | [screenshots/16-analytics-reflect-every-action.png](screenshots/16-analytics-reflect-every-action.png) |
| 17 | Integrations are honestly labelled as not connected | Placeholder (scaffold) | ✅ passed | 0.85s | [screenshots/17-integrations-are-honestly-labelled-as-not-connecte.png](screenshots/17-integrations-are-honestly-labelled-as-not-connecte.png) |
| 18 | Clean up: cancel schedule, delete demo drafts and rule | Cleanup | ✅ passed | 2.81s | [screenshots/18-clean-up-cancel-schedule-delete-demo-drafts-and-ru.png](screenshots/18-clean-up-cancel-schedule-delete-demo-drafts-and-ru.png) |
| 19 | No external calls and no browser errors | Validation / guard | ✅ passed | 0.56s | [screenshots/19-no-external-calls-and-no-browser-errors.png](screenshots/19-no-external-calls-and-no-browser-errors.png) |

## Checks performed

**1. Dashboard loads in demo mode**
- Header shows "Demo mode (mock generation)"; /api/health reports demo-mock
- All dashboard sections are present
- Credit balance in UI matches API (85)

**2. Empty topic is rejected without charging credits**
- Toast asks for a topic
- Balance unchanged in API

**3. Fill in the content brief**
- Brand, topic, platform=Instagram, tone=Friendly, objective=Brand awareness, variations=3

**4. Generate content (mock provider)**
- POST /api/generate returned 200
- Response is flagged mock=true (no AI provider called)
- Charged 1 base + 3 variations = 4 credits
- Three captions rendered in the results panel
- Rendered text equals the API response
- Balance 85 → 81 in both UI and API
- Ledger entry recorded as "Mock generation"
- New workflow run is listed with the topic

**5. Save the generated content as a draft**
- One new draft in the database
- Stored draft content equals the generated content
- Stored platform is Instagram and status is draft
- Draft card and sidebar counter updated

**6. Submit for approval (draft becomes locked)**
- Draft appears in the approval queue
- API status is pending_approval
- Edit/Submit buttons are no longer offered while pending

**7. Reviewer rejects with a reason**
- API status is rejected with the reason stored

**8. Author edits the rejected draft**
- Edited content persisted to the database
- Status returned to draft and the rejection reason was cleared

**9. Resubmit and approve**
- API status is approved
- "Schedule approved" is now offered

**10. A past schedule time is rejected**
- Dialog shows "Schedule time must be in the future"
- Nothing was scheduled

**11. Schedule the approved draft for Mon 23 Nov, 10:30**
- Scheduled post stored with the approved content and platform
- Stored time 2026-11-23T05:00:00+00:00 (UTC) is the chosen local time
- Draft status is scheduled
- Scheduled draft can no longer be deleted from the UI

**12. Scheduled post appears on the calendar**
- Event "10:30 · Instagram" is in the 23 November cell

**13. Create an automation rule**
- Rule stored as manual trigger → create_draft, enabled
- A duplicate name (different case) is rejected

**14. Run the automation manually**
- One execution logged as success
- It created a draft marked [AUTOMATION MOCK] that still needs approval
- Execution history shows the run

**15. Disabled rules cannot run**
- "Run now" is disabled in the UI
- API also refuses the run (409)

**16. Analytics reflect every action**
- generation_runs: 4 → 5 (expected +1)
- credits_consumed: 15 → 19 (expected +4)
- scheduled_posts: 0 → 1 (expected +1)
- automation_rules: 0 → 1 (expected +1)
- automation_executions: 2 → 3 (expected +1)
- published_posts: 0 → 0 (expected +0)
- Every analytics card in the UI matches the API

**17. Integrations are honestly labelled as not connected**
- n8n, LinkedIn, Instagram and Analytics provider all show "not connected"

**18. Clean up: cancel schedule, delete demo drafts and rule**
- Cancelling the schedule returns the draft to approved
- Demo drafts and rule are gone from the database
- Credit ledger and workflow runs are append-only and intentionally kept

**19. No external calls and no browser errors**
- All 103 browser requests went to the app (or Google Fonts); no AI/social hosts
- No JavaScript or CSP errors in the console
