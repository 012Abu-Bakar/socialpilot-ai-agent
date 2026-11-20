"""End-to-end portfolio demo: drives the real SocialPilot UI against a running server and records a video.

Opt-in only (it writes to the server's database):
    SOCIALPILOT_DEMO_URL=http://127.0.0.1:8000 pytest tests/e2e -v

Safety: aborts unless /api/health reports demo-mock mode, so no AI provider is ever called; fails if the browser
requests any host other than the app (and Google Fonts). Data it creates is removed at the end unless DEMO_KEEP_DATA=1.
Artifacts: artifacts/socialpilot-demo/ (video, screenshots, results.json, report.md).
"""
import json
import os
import re
import shutil
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

BASE = os.getenv('SOCIALPILOT_DEMO_URL', '').rstrip('/')
pytestmark = pytest.mark.skipif(not BASE, reason='set SOCIALPILOT_DEMO_URL to run the live demo')
sync_api = pytest.importorskip('playwright.sync_api')
expect = sync_api.expect

OUT = Path(__file__).resolve().parents[2] / 'artifacts' / 'socialpilot-demo'
SHOTS = OUT / 'screenshots'
VIEWPORT = {'width': 1440, 'height': 900}
ALLOWED_HOSTS = {'fonts.googleapis.com', 'fonts.gstatic.com'}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


def post_status(path):
    """Raw POST without a browser; returns the HTTP status (used for negative API checks only)."""
    req = urllib.request.Request(BASE + path, method='POST', data=b'', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status
    except urllib.error.HTTPError as e: return e.code


class Demo:
    """Records each step (status, checks, timing, screenshot) and shows a caption in the video."""

    def __init__(self, page):
        self.page, self.steps = page, []

    def caption(self, text):
        self.page.evaluate("""text => {
            let el = document.getElementById('demo-caption');
            if (!el) {
                el = document.createElement('div');
                el.id = 'demo-caption';
                el.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:99999;pointer-events:none;max-width:60vw;' +
                  'background:rgba(15,20,30,.92);color:#fff;border:1px solid #9a83ff;border-radius:10px;padding:10px 14px;' +
                  'font:600 15px system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.5)';
                document.body.appendChild(el);
            }
            el.textContent = '▶ Automated demo · ' + text;
        }""", text)

    @contextmanager
    def step(self, title, kind='functional', focus=None):
        rec = {'n': len(self.steps) + 1, 'title': title, 'kind': kind, 'status': 'running', 'checks': []}
        self.steps.append(rec)
        self.caption(f'Step {rec["n"]}: {title}')
        start = time.monotonic()
        try:
            yield rec
            self.snapshot(rec, focus)  # part of the step: a step only passes once its checkpoint is captured
            rec['status'] = 'passed'
        except BaseException as e:
            rec['status'] = 'failed'
            rec['error'] = (str(e).strip().splitlines() or [type(e).__name__])[0][:400]
            if 'screenshot' not in rec:
                try: self.snapshot(rec, None)  # best effort: capture the failure state
                except Exception: pass
            raise
        finally:
            rec['seconds'] = round(time.monotonic() - start, 2)

    def snapshot(self, rec, focus):
        if focus:
            target = self.page.locator(focus).first
            if target.is_visible(): target.scroll_into_view_if_needed()
        self.page.wait_for_timeout(500)  # let the viewer see the result
        name = f'{rec["n"]:02d}-{re.sub(r"[^a-z0-9]+", "-", rec["title"].lower()).strip("-")[:50]}.png'
        self.page.screenshot(path=SHOTS / name)
        rec['screenshot'] = f'screenshots/{name}'

    @staticmethod
    def check(rec, description, condition=True):
        rec['checks'].append(description)
        assert condition, description


def write_report(demo, meta):
    (OUT / 'results.json').write_text(json.dumps({**meta, 'steps': demo.steps}, indent=2))
    icon = {'passed': '✅', 'failed': '❌', 'running': '⏸'}
    kinds = {'functional': 'Working feature', 'negative': 'Validation / guard', 'placeholder': 'Placeholder (scaffold)', 'cleanup': 'Cleanup'}
    lines = [f'# SocialPilot AI — automated demo report', '',
             f'- Run: {meta["started"]} → {meta["finished"]}', f'- Target: {meta["base_url"]} (mode: {meta["mode"]})',
             f'- Result: **{meta["result"]}** — {sum(s["status"] == "passed" for s in demo.steps)}/{len(demo.steps)} steps passed',
             f'- Video: `{meta["video"]}`', f'- Browser requests: {meta["requests"]} total, external hosts: {meta["external_hosts"] or "none"}', '']
    if meta.get('error'): lines += [f'> Stopped at first failure: {meta["error"]}', '']
    lines += ['| # | Step | Type | Result | Time | Screenshot |', '|---|---|---|---|---|---|']
    for s in demo.steps:
        lines.append(f'| {s["n"]} | {s["title"]} | {kinds[s["kind"]]} | {icon[s["status"]]} {s["status"]} | {s.get("seconds", "")}s | '
                     f'[{s.get("screenshot", "")}]({s.get("screenshot", "")}) |')
    lines += ['', '## Checks performed', '']
    for s in demo.steps:
        lines.append(f'**{s["n"]}. {s["title"]}**')
        lines += [f'- {c}' for c in s['checks']] or ['- (none recorded)']
        if s.get('error'): lines.append(f'- ❌ {s["error"]}')
        lines.append('')
    (OUT / 'report.md').write_text('\n'.join(lines))


def test_portfolio_demo():
    health = get('/api/health')
    if health['mode'] != 'demo-mock':
        pytest.fail(f'Refusing to run: server is in {health["mode"]!r} mode; the demo must not call a real AI provider.')

    shutil.rmtree(OUT, ignore_errors=True)
    SHOTS.mkdir(parents=True)
    stamp = datetime.now().strftime('%H%M%S')
    rule_name = f'Weekly tips (demo {stamp})'
    topic = '5 ways small teams can plan a week of social content in one hour'
    when = (datetime.now() + timedelta(days=3)).replace(hour=10, minute=30, second=0, microsecond=0)
    meta = {'base_url': BASE, 'mode': health['mode'], 'started': datetime.now().isoformat(timespec='seconds'), 'result': 'FAILED'}
    created = {'drafts': [], 'schedule': None, 'rule': None}
    requests_seen = []

    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=os.getenv('DEMO_HEADED') != '1', slow_mo=int(os.getenv('DEMO_SLOWMO', '250')))
        ctx = browser.new_context(viewport=VIEWPORT, locale='en-US', record_video_dir=str(OUT / 'video-tmp'), record_video_size=VIEWPORT)
        ctx.set_default_timeout(10_000)
        page = ctx.new_page()
        page.on('request', lambda r: requests_seen.append(r.url))
        console_errors = []
        page.on('pageerror', lambda e: console_errors.append(str(e)))
        page.on('console', lambda m: console_errors.append(m.text) if m.type == 'error' and 'fonts.g' not in m.text else None)
        demo = Demo(page)
        try:
            page.goto(BASE)
            base_analytics, base_credits = get('/api/analytics'), get('/api/credits')

            with demo.step('Dashboard loads in demo mode', focus='header') as s:
                expect(page).to_have_title(re.compile('SocialPilot'))
                expect(page.locator('#modeBadge')).to_contain_text('Demo mode')
                demo.check(s, 'Header shows "Demo mode (mock generation)"; /api/health reports demo-mock')
                for sel in ['#studioSection', '#approvalSection', '#calendarSection', '#draftsSection', '#automationSection', '#analyticsSection']:
                    expect(page.locator(sel)).to_be_attached()
                demo.check(s, 'All dashboard sections are present')
                expect(page.locator('#creditBalance')).to_have_text(str(base_credits['balance']))
                demo.check(s, f'Credit balance in UI matches API ({base_credits["balance"]})')
                if base_credits['balance'] < 10:  # top up demo credits through the UI so generation can run
                    page.click('#resetCredits')
                    expect(page.locator('#creditBalance')).to_have_text('100')
                    base_credits = get('/api/credits')
                    demo.check(s, 'Balance was low, so demo credits were reset through the UI')

            with demo.step('Empty topic is rejected without charging credits', kind='negative', focus='#studioSection') as s:
                page.fill('#topic', '')
                page.click('#generate')
                expect(page.locator('#toast')).to_contain_text('Enter a topic')
                demo.check(s, 'Toast asks for a topic')
                demo.check(s, 'Balance unchanged in API', get('/api/credits')['balance'] == base_credits['balance'])

            with demo.step('Fill in the content brief', focus='#studioSection') as s:
                page.fill('#brand', 'Northstar Studio')
                page.fill('#topic', topic)
                page.select_option('#platform', 'Instagram')
                page.select_option('#tone', 'Friendly')
                page.select_option('#objective', 'Brand awareness')
                page.fill('#variations', '3')
                expect(page.locator('#varLabel')).to_have_text('3')
                demo.check(s, 'Brand, topic, platform=Instagram, tone=Friendly, objective=Brand awareness, variations=3')

            with demo.step('Generate content (mock provider)', focus='#studioSection') as s:
                with page.expect_response(lambda r: r.url.endswith('/api/generate')) as resp_info:
                    page.click('#generate')
                resp = resp_info.value
                body = resp.json()
                demo.check(s, 'POST /api/generate returned 200', resp.status == 200)
                demo.check(s, 'Response is flagged mock=true (no AI provider called)', body['mock'] is True)
                demo.check(s, 'Charged 1 base + 3 variations = 4 credits', body['credits_used'] == 4)
                expect(page.locator('#generated')).to_be_visible()
                generated = page.locator('#generated').inner_text()
                demo.check(s, 'Three captions rendered in the results panel', generated.count('Caption:') == 3)
                demo.check(s, 'Rendered text equals the API response', generated.strip() == body['content'].strip())
                expect(page.locator('#resultTag')).to_have_text('Generated (mock)')
                expect(page.locator('#creditBalance')).to_have_text(str(base_credits['balance'] - 4))
                credits = get('/api/credits')
                demo.check(s, f'Balance {base_credits["balance"]} → {credits["balance"]} in both UI and API', credits['balance'] == base_credits['balance'] - 4)
                demo.check(s, 'Ledger entry recorded as "Mock generation"', credits['transactions'][0]['reason'].startswith('Mock generation'))
                expect(page.locator('#workflowRuns .run').first).to_contain_text(topic)
                demo.check(s, 'New workflow run is listed with the topic')

            with demo.step('Save the generated content as a draft', focus='#draftsSection') as s:
                before = len(get('/api/drafts'))
                page.click('#save')
                expect(page.locator('#toast')).to_contain_text('Draft saved')
                drafts = get('/api/drafts')
                demo.check(s, 'One new draft in the database', len(drafts) == before + 1)
                draft = drafts[0]
                created['drafts'].append(draft['id'])
                demo.check(s, 'Stored draft content equals the generated content', draft['content'].strip() == body['content'].strip())
                demo.check(s, 'Stored platform is Instagram and status is draft', (draft['platform'], draft['status']) == ('Instagram', 'draft'))
                card = page.locator(f'#drafts .draft[data-id="{draft["id"]}"]')
                expect(card).to_contain_text('Draft')
                expect(page.locator('#draftCount')).to_have_text(str(before + 1))
                demo.check(s, 'Draft card and sidebar counter updated')

            with demo.step('Submit for approval (draft becomes locked)', focus='#approvalSection') as s:
                card.locator('button', has_text='Submit for approval').click()
                queue_item = page.locator('#approvalQueue .draft', has_text=draft['title'])
                expect(queue_item).to_be_visible()
                demo.check(s, 'Draft appears in the approval queue')
                demo.check(s, 'API status is pending_approval', get('/api/drafts')[0]['status'] == 'pending_approval')
                expect(card.locator('button', has_text='Edit')).to_have_count(0)
                demo.check(s, 'Edit/Submit buttons are no longer offered while pending')

            with demo.step('Reviewer rejects with a reason', focus=f'#drafts .draft[data-id="{draft["id"]}"]') as s:
                queue_item.locator('button', has_text='Reject').click()
                expect(page.locator('#modal')).to_have_attribute('open', '')
                page.fill('#modal textarea[name=reason]', 'Needs a stronger opening hook')
                page.click('#modalConfirm')
                expect(card).to_contain_text('Reason: Needs a stronger opening hook')
                d = next(x for x in get('/api/drafts') if x['id'] == draft['id'])
                demo.check(s, 'API status is rejected with the reason stored', (d['status'], d['rejection_reason']) == ('rejected', 'Needs a stronger opening hook'))

            with demo.step('Author edits the rejected draft', focus=f'#drafts .draft[data-id="{draft["id"]}"]') as s:
                card.locator('button', has_text='Edit').click()
                content_box = page.locator('#modal textarea[name=content]')
                new_content = 'Stop scrolling: here is how to plan a full week of posts in one hour.\n\n' + content_box.input_value()
                content_box.fill(new_content)
                page.click('#modalConfirm')
                expect(card).to_contain_text('Stop scrolling')
                d = next(x for x in get('/api/drafts') if x['id'] == draft['id'])
                demo.check(s, 'Edited content persisted to the database', d['content'] == new_content)
                demo.check(s, 'Status returned to draft and the rejection reason was cleared', (d['status'], d['rejection_reason']) == ('draft', ''))

            with demo.step('Resubmit and approve', focus='#draftsSection') as s:
                card.locator('button', has_text='Submit for approval').click()
                page.locator('#approvalQueue .draft', has_text=draft['title']).locator('button', has_text='Approve').click()
                expect(card.locator('button', has_text='Schedule approved')).to_be_visible()
                d = next(x for x in get('/api/drafts') if x['id'] == draft['id'])
                demo.check(s, 'API status is approved', d['status'] == 'approved')
                demo.check(s, '"Schedule approved" is now offered')

            with demo.step('A past schedule time is rejected', kind='negative', focus='#modal') as s:
                card.locator('button', has_text='Schedule approved').click()
                page.fill('#modal input[name=when]', '2020-01-01T09:00')
                page.click('#modalConfirm')
                expect(page.locator('#modalError')).to_contain_text('future')
                demo.check(s, 'Dialog shows "Schedule time must be in the future"')
                demo.check(s, 'Nothing was scheduled', not any(p['draft_id'] == draft['id'] for p in get('/api/schedule')))

            with demo.step(f'Schedule the approved draft for {when:%a %d %b, %H:%M}', focus='#calendarSection') as s:
                page.click('#modalCancel')  # close the rejected attempt, then start again
                card.locator('button', has_text='Schedule approved').click()
                page.fill('#modal input[name=when]', when.strftime('%Y-%m-%dT%H:%M'))
                page.click('#modalConfirm')
                expect(page.locator('#toast')).to_contain_text('scheduled')
                post = next(p for p in get('/api/schedule') if p['draft_id'] == draft['id'])
                created['schedule'] = post['id']
                demo.check(s, 'Scheduled post stored with the approved content and platform',
                           (post['content'], post['platform'], post['status']) == (new_content, 'Instagram', 'scheduled'))
                stored = datetime.fromisoformat(post['scheduled_at'])
                demo.check(s, f'Stored time {post["scheduled_at"]} (UTC) is the chosen local time', stored == when.astimezone())
                demo.check(s, 'Draft status is scheduled', next(x for x in get('/api/drafts') if x['id'] == draft['id'])['status'] == 'scheduled')
                expect(page.locator('#scheduledPosts')).to_contain_text(draft['title'])
                expect(card.locator('button', has_text='Delete')).to_have_count(0)
                demo.check(s, 'Scheduled draft can no longer be deleted from the UI')

            with demo.step('Scheduled post appears on the calendar', focus='#calendarSection') as s:
                for _ in range(3):  # move to the target month if it is not the current one
                    if page.locator('#calendarMonth').inner_text() == when.strftime('%B %Y'): break
                    page.click('#nextMonth')
                expect(page.locator('#calendarMonth')).to_have_text(when.strftime('%B %Y'))
                day_cell = page.locator('.calendar-day', has=page.locator('.day-number', has_text=re.compile(f'^{when.day}$')))
                expect(day_cell.locator('.calendar-event')).to_contain_text('10:30')
                expect(day_cell.locator('.calendar-event')).to_contain_text('Instagram')
                demo.check(s, f'Event "10:30 · Instagram" is in the {when.day} {when:%B} cell')

            with demo.step('Create an automation rule', focus='#automationSection') as s:
                page.fill('#automationName', rule_name)
                page.fill('#automationTopic', 'Share one practical content-planning tip for small teams')
                page.select_option('#automationPlatform', 'LinkedIn')
                page.click('#createAutomation')
                rule_card = page.locator('.automation-rule', has_text=rule_name)
                expect(rule_card).to_contain_text('Enabled')
                rule = next(r for r in get('/api/automations') if r['name'] == rule_name)
                created['rule'] = rule['id']
                demo.check(s, 'Rule stored as manual trigger → create_draft, enabled', (rule['trigger_type'], rule['action_type'], rule['enabled']) == ('manual', 'create_draft', 1))
                page.fill('#automationName', rule_name.upper())
                page.fill('#automationTopic', 'duplicate')
                page.click('#createAutomation')
                expect(page.locator('#toast')).to_contain_text('already exists')
                demo.check(s, 'A duplicate name (different case) is rejected')
                page.fill('#automationName', ''); page.fill('#automationTopic', '')

            with demo.step('Run the automation manually', focus='#automationSection') as s:
                before = len(get('/api/automation-executions'))
                rule_card.locator('button', has_text='Run now').click()
                expect(page.locator('#toast')).to_contain_text('Automation ran')
                execs = get('/api/automation-executions')
                demo.check(s, 'One execution logged as success', len(execs) == before + 1 and execs[0]['status'] == 'success')
                auto_draft = next(d for d in get('/api/drafts') if d['title'].startswith(f'Automation: {rule_name}'))
                created['drafts'].append(auto_draft['id'])
                demo.check(s, 'It created a draft marked [AUTOMATION MOCK] that still needs approval',
                           auto_draft['content'].startswith('[AUTOMATION MOCK') and auto_draft['status'] == 'draft')
                expect(page.locator('#automationExecutions .scheduled-item').first).to_contain_text(rule_name)
                demo.check(s, 'Execution history shows the run')

            with demo.step('Disabled rules cannot run', kind='negative', focus='#automationSection') as s:
                rule_card.locator('button', has_text='Disable').click()
                expect(rule_card.locator('button', has_text='Run now')).to_be_disabled()
                demo.check(s, '"Run now" is disabled in the UI')
                demo.check(s, 'API also refuses the run (409)', post_status(f'/api/automations/{rule["id"]}/run') == 409)

            with demo.step('Analytics reflect every action', focus='#analyticsSection') as s:
                page.locator('#analyticsSection').scroll_into_view_if_needed()
                a = get('/api/analytics')
                expected = {'generation_runs': 1, 'credits_consumed': 4, 'scheduled_posts': 1, 'automation_rules': 1, 'automation_executions': 1, 'published_posts': 0}
                for key, delta in expected.items():
                    demo.check(s, f'{key}: {base_analytics[key]} → {a[key]} (expected +{delta})', a[key] == base_analytics[key] + delta)
                cards = dict(zip(page.locator('#analyticsCards .metric-card small').all_inner_texts(),
                                 map(int, page.locator('#analyticsCards .metric-card strong').all_inner_texts())))
                labels = {'Generation runs': 'generation_runs', 'Credits consumed': 'credits_consumed', 'Scheduled posts': 'scheduled_posts',
                          'Automation rules': 'automation_rules', 'Automation executions': 'automation_executions', 'Published posts': 'published_posts'}
                demo.check(s, 'Every analytics card in the UI matches the API', all(cards[l] == a[k] for l, k in labels.items()))

            with demo.step('Integrations are honestly labelled as not connected', kind='placeholder', focus='#integrationsSection') as s:
                pills = page.locator('.integration-item .status-pill').all_inner_texts()
                demo.check(s, 'n8n, LinkedIn, Instagram and Analytics provider all show "not connected"', len(pills) == 4 and all(p == 'not connected' for p in pills))

            if os.getenv('DEMO_KEEP_DATA') != '1':
                with demo.step('Clean up: cancel schedule, delete demo drafts and rule', kind='cleanup', focus='#draftsSection') as s:
                    page.locator('#scheduledPosts .scheduled-item', has_text=draft['title']).locator('button', has_text='Remove').click()
                    expect(card.locator('button', has_text='Schedule approved')).to_be_visible()
                    demo.check(s, 'Cancelling the schedule returns the draft to approved')
                    for draft_id in created['drafts']:
                        page.locator(f'#drafts .draft[data-id="{draft_id}"]').locator('button', has_text='Delete').click()
                        page.click('#modalConfirm')
                        expect(page.locator(f'#drafts .draft[data-id="{draft_id}"]')).to_have_count(0)
                    rule_card.locator('button', has_text='Delete').click()
                    page.click('#modalConfirm')
                    expect(rule_card).to_have_count(0)
                    left = {d['id'] for d in get('/api/drafts')} & set(created['drafts'])
                    demo.check(s, 'Demo drafts and rule are gone from the database', not left and not any(r['id'] == rule['id'] for r in get('/api/automations')))
                    demo.check(s, 'Credit ledger and workflow runs are append-only and intentionally kept')

            with demo.step('No external calls and no browser errors', kind='negative') as s:
                external = sorted({urlsplit(u).hostname for u in requests_seen} - {urlsplit(BASE).hostname} - ALLOWED_HOSTS)
                meta['external_hosts'] = external
                demo.check(s, f'All {len(requests_seen)} browser requests went to the app (or Google Fonts); no AI/social hosts', not external)
                # The deliberate negative steps (duplicate rule name) make Chrome log their 4xx responses; anything else fails.
                real_errors = [e for e in console_errors if 'the server responded with a status of 4' not in e]
                demo.check(s, f'No JavaScript or CSP errors in the console {real_errors or ""}'.strip(), not real_errors)
            demo.caption('Demo complete — all steps passed')
            page.wait_for_timeout(1500)
            meta['result'] = 'PASSED'
        except BaseException as e:
            meta['error'] = (str(e).strip().splitlines() or [type(e).__name__])[0][:400]
            raise
        finally:
            video = page.video
            ctx.close()
            browser.close()
            final = OUT / 'socialpilot-demo.webm'
            shutil.move(video.path(), final)
            shutil.rmtree(OUT / 'video-tmp', ignore_errors=True)
            meta.update(finished=datetime.now().isoformat(timespec='seconds'), video=str(final.relative_to(OUT.parents[1])),
                        requests=len(requests_seen), external_hosts=meta.get('external_hosts', []))
            write_report(demo, meta)
