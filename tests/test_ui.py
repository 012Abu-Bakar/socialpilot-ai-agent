"""Browser (Playwright) tests. Runs the app on a temp DB in mock mode; no AI provider is called."""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, 'SOCIALPILOT_DB': str(tmp_path_factory.mktemp('db') / 'ui.db'), 'SOCIALPILOT_MOCK_MODE': '1',
           'SOCIALPILOT_TESTING': '1', 'SOCIALPILOT_ALLOWED_HOSTS': '127.0.0.1,localhost'}
    env.pop('ANTHROPIC_API_KEY', None)
    p = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'main:app', '--port', str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            urllib.request.urlopen(url + '/api/health'); break
        except Exception: time.sleep(0.2)
    yield url
    p.terminate(); p.wait(5)


@pytest.fixture(scope='module')
def browser():
    with playwright_sync.sync_playwright() as pw:
        try: b = pw.chromium.launch()
        except Exception: b = pw.chromium.launch(channel='chrome')
        yield b
        b.close()


@pytest.fixture()
def page(browser, server):
    # bypass_csp lets Playwright's string-eval helpers run; test_page_runs_under_csp covers the real policy.
    ctx = browser.new_context(viewport={'width': 1366, 'height': 900}, locale='en-US', bypass_csp=True)
    pg = ctx.new_page()
    pg.errors = []
    pg.on('pageerror', lambda e: pg.errors.append(str(e)))
    # Console errors fail the test (this also catches CSP violations); network/4xx noise is excluded.
    pg.on('console', lambda m: pg.errors.append(m.text) if m.type == 'error' and 'fonts.g' not in m.text and 'ERR_' not in m.text and 'Failed to load resource: the server responded with a status of 4' not in m.text else None)
    pg.base = server
    yield pg
    ctx.close()


def toast(pg): return pg.locator('#toast').inner_text()
def wait_toast(pg, text): pg.wait_for_function(f"document.querySelector('#toast').textContent.includes({text!r})")


def modal(pg, fill=None, confirm=True):
    pg.wait_for_selector('#modal[open]')
    for selector, value in (fill or {}).items():
        pg.fill(f'#modal {selector}', value)
    pg.click('#modalConfirm' if confirm else '#modalCancel')


def test_full_user_journey(page):
    pg = page
    pg.goto(pg.base)
    assert 'SocialPilot' in pg.title()
    pg.wait_for_function("document.querySelector('#modeBadge').textContent.includes('Demo mode')")
    assert pg.locator('#creditBalance').inner_text() == '100'

    # empty topic is rejected client-side, nothing charged
    pg.click('#generate'); assert 'topic' in toast(pg).lower()
    assert pg.locator('#creditBalance').inner_text() == '100'

    # generate (mock) with special characters
    pg.fill('#topic', '<b>Q4 launch</b> & "offers"')
    pg.select_option('#platform', 'Instagram'); pg.select_option('#tone', 'Witty')
    pg.select_option('#objective', 'Lead generation'); pg.fill('#variations', '2')
    pg.click('#generate')
    pg.wait_for_selector('#generated')
    out = pg.locator('#generated').inner_text()
    assert out.count('Caption:') == 2 and '<b>Q4 launch</b>' in out
    assert pg.locator('#output b').count() == 0  # not rendered as HTML
    assert pg.locator('#resultTag').inner_text() == 'Generated (mock)'
    pg.wait_for_function("document.querySelector('#creditBalance').textContent==='97'")
    assert pg.locator('#creditUsed').inner_text() == '3'
    assert pg.locator('#lastCost').inner_text() == '3 credits'
    assert pg.locator('#workflowRuns .run').count() == 1
    assert 'Instagram' in pg.locator('#workflowRuns .run').inner_text()
    pg.wait_for_function("document.querySelector('#analyticsCards .metric-card strong')?.textContent==='1'")

    # draft lifecycle: only actions valid for the current status are offered
    pg.click('#save'); pg.wait_for_selector('#drafts .draft')
    assert pg.locator('#draftCount').inner_text() == '1'
    pg.click('#drafts >> text=Edit')
    original = pg.locator('#modal textarea[name=content]').input_value()
    modal(pg, {'textarea[name=content]': 'EDITED ' + original})
    pg.wait_for_function("document.querySelector('#drafts').innerText.includes('EDITED')")
    pg.click('#drafts >> text=Submit for approval')
    pg.wait_for_selector('#approvalQueue .draft')
    assert pg.locator('#drafts button', has_text='Edit').count() == 0  # pending drafts are locked
    pg.click('#approvalQueue >> text=Approve')
    pg.wait_for_selector('#drafts >> text=Schedule approved')

    # schedule dialog: past date rejected in the dialog, future accepted
    pg.click('#drafts >> text=Schedule approved')
    modal(pg, {'input[name=when]': '2020-01-01T09:00'})
    assert 'future' in pg.locator('#modalError').inner_text().lower()
    pg.click('#modalCancel')
    assert pg.locator('#scheduledPosts .scheduled-item').count() == 0

    fut = datetime.now() + timedelta(days=2)
    pg.click('#drafts >> text=Schedule approved')
    modal(pg, {'input[name=when]': fut.strftime('%Y-%m-%dT10:30')})
    pg.wait_for_selector('#scheduledPosts .scheduled-item')
    assert '10:30' in pg.locator('#scheduledPosts').inner_text()
    assert pg.locator('#drafts button', has_text='Delete').count() == 0  # scheduled drafts can't be deleted

    # calendar shows the event on its local day; month navigation round-trips
    if fut.month == datetime.now().month:
        assert pg.locator('.calendar-event').count() == 1
        assert '10:30' in pg.locator('.calendar-event').inner_text()
    month = pg.locator('#calendarMonth').inner_text()
    pg.click('#nextMonth'); assert pg.locator('#calendarMonth').inner_text() != month
    pg.click('#prevMonth'); assert pg.locator('#calendarMonth').inner_text() == month

    # persistence after reload
    pg.reload(); pg.wait_for_selector('#scheduledPosts .scheduled-item')
    assert pg.locator('#creditBalance').inner_text() == '97'
    assert pg.locator('#workflowRuns .run').count() == 1
    assert pg.locator('#draftCount').inner_text() == '1'

    # remove schedule -> draft returns to approved
    pg.click('#scheduledPosts >> text=Remove')
    pg.wait_for_function("document.querySelector('#scheduledPosts').innerText.includes('No scheduled')")
    pg.wait_for_selector('#drafts >> text=Schedule approved')

    # automation
    pg.click('#createAutomation'); assert 'rule name' in toast(pg).lower()
    pg.fill('#automationName', 'Weekly tip'); pg.fill('#automationTopic', 'Share a weekly tip')
    pg.click('#createAutomation'); pg.wait_for_selector('.automation-rule')
    pg.click('.automation-rule >> text=Run now')
    pg.wait_for_selector('#automationExecutions .scheduled-item')
    pg.wait_for_function("document.querySelector('#draftCount').textContent==='2'")
    assert 'AUTOMATION MOCK' in pg.locator('#drafts').inner_text()
    pg.fill('#automationName', 'weekly TIP'); pg.fill('#automationTopic', 'dup')
    pg.click('#createAutomation'); wait_toast(pg, 'already exists')
    pg.click('.automation-rule >> text=Disable')
    pg.wait_for_selector('.automation-rule button:has-text("Run now"):disabled')

    # integrations labelled honestly
    items = pg.locator('.integration-item .status-pill').all_inner_texts()
    assert len(items) == 4 and all('not connected' in t for t in items)

    # delete a draft through the confirmation dialog (cancel first, then confirm)
    pg.locator('#drafts .draft', has_text='AUTOMATION MOCK').locator('button', has_text='Delete').click()
    modal(pg, confirm=False)
    assert pg.locator('#draftCount').inner_text() == '2'
    pg.locator('#drafts .draft', has_text='AUTOMATION MOCK').locator('button', has_text='Delete').click()
    modal(pg)
    pg.wait_for_function("document.querySelector('#draftCount').textContent==='1'")

    # credit reset keeps history
    pg.click('#resetCredits'); pg.wait_for_function("document.querySelector('#creditBalance').textContent==='100'")
    assert pg.locator('#creditUsed').inner_text() == '3'

    assert pg.errors == [], pg.errors


def test_reject_with_reason(page):
    pg = page; pg.goto(pg.base)
    pg.fill('#topic', 'Reject me'); pg.click('#generate'); pg.wait_for_selector('#generated')
    pg.click('#saveSubmit'); wait_toast(pg, 'submitted')
    pg.locator('#approvalQueue .draft').last.locator('button', has_text='Reject').click()
    modal(pg, {'textarea[name=reason]': 'Off brand'})
    pg.wait_for_function("document.querySelector('#drafts').innerText.includes('Reason: Off brand')")
    assert pg.errors == [], pg.errors


def test_navigation_links_have_targets(page):
    pg = page; pg.goto(pg.base)
    hrefs = pg.locator('.side .nav').evaluate_all('els => els.map(e => e.getAttribute("href"))')
    assert len(hrefs) == 7
    for h in hrefs:
        assert pg.locator(h).count() == 1, h
    pg.click('.nav[href="#analyticsSection"]')
    assert 'active' in pg.locator('.nav[href="#analyticsSection"]').get_attribute('class')


def test_page_runs_under_csp(browser, server):
    """Exercise the app with the real Content-Security-Policy active (selector waits only, no eval)."""
    ctx = browser.new_context()
    pg = ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.on('console', lambda m: errors.append(m.text) if m.type == 'error' and 'fonts.g' not in m.text else None)
    pg.goto(server)
    pg.fill('#topic', 'CSP check'); pg.click('#generate'); pg.wait_for_selector('#generated')
    pg.click('#save'); pg.wait_for_selector('#drafts .draft button:has-text("Edit")')
    pg.locator('#drafts .draft button', has_text='Edit').first.click()
    pg.wait_for_selector('#modal[open]'); pg.click('#modalCancel'); pg.wait_for_selector('#modal:not([open])', state='attached')
    pg.click('#nextMonth')
    ctx.close()
    assert not [e for e in errors if 'Content Security Policy' in e or 'Refused' in e], errors
    assert errors == [], errors


def test_no_horizontal_overflow_mobile(browser, server):
    ctx = browser.new_context(viewport={'width': 390, 'height': 800})
    pg = ctx.new_page(); pg.goto(server); pg.wait_for_load_state('networkidle')
    sw, cw = pg.evaluate('[document.documentElement.scrollWidth, document.documentElement.clientWidth]')
    ctx.close()
    assert sw <= cw + 1, f'horizontal overflow: scrollWidth={sw} clientWidth={cw}'


def test_generate_error_shown_not_object_object(page):
    pg = page; pg.goto(pg.base)
    pg.fill('#topic', 'x' * 2001); pg.click('#generate')
    pg.wait_for_function("document.querySelector('#resultTag').textContent==='Error'")
    assert '[object' not in pg.locator('#output').inner_text()
    assert 'topic' in pg.locator('#output').inner_text()  # names the offending field
