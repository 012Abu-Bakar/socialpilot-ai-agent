"""Browser (Playwright) tests. Runs the app on a temp DB in mock mode; no AI provider is called."""
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

playwright_sync = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, 'SOCIALPILOT_DB': str(tmp_path_factory.mktemp('db') / 'ui.db'), 'SOCIALPILOT_MOCK_MODE': '1'}
    env.pop('ANTHROPIC_API_KEY', None)
    p = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'main:app', '--port', str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            if httpx.get(url + '/api/health').status_code == 200: break
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
    ctx = browser.new_context(viewport={'width': 1366, 'height': 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on('pageerror', lambda e: pg.errors.append(str(e)))
    pg.on('console', lambda m: pg.errors.append(m.text) if m.type == 'error' and 'fonts.g' not in m.text and 'ERR_' not in m.text and 'Failed to load resource: the server responded with a status of 4' not in m.text else None)
    pg.base = server
    yield pg
    ctx.close()


def toast(pg): return pg.locator('#toast').inner_text()


def test_full_user_journey(page):
    pg = page
    pg.goto(pg.base)
    assert 'SocialPilot' in pg.title()
    assert 'Demo mode' in pg.locator('header .mode').inner_text()
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
    assert pg.locator('#creditBalance').inner_text() == '97'
    assert pg.locator('#creditUsed').inner_text() == '3'
    assert pg.locator('#lastCost').inner_text() == '3 credits'
    assert pg.locator('#workflowRuns .run').count() == 1
    assert 'Instagram' in pg.locator('#workflowRuns .run').inner_text()
    assert pg.locator('#analyticsCards .metric-card').first.inner_text().startswith('1')

    # draft lifecycle
    pg.click('#save'); pg.wait_for_selector('#drafts .draft')
    assert pg.locator('#draftCount').inner_text() == '1'
    pg.answers = []
    def on_dialog(d):
        a = pg.answers.pop(0) if pg.answers else None
        d.accept('EDITED ' + d.default_value if a == 'EDIT' else (a or ''))
    pg.on('dialog', on_dialog)
    pg.answers = ['EDIT']
    pg.click('#drafts >> text=Edit'); pg.wait_for_function("document.querySelector('#drafts').innerText.includes('EDITED')")
    pg.click('#drafts >> text=Submit for approval')
    pg.wait_for_selector('#approvalQueue .draft')
    pg.click('#approvalQueue >> text=Approve')
    pg.wait_for_selector('#drafts >> text=Schedule approved')

    # schedule via prompts: past date rejected, future accepted
    pg.answers = ['2020-01-01', '09:00']
    pg.click('#drafts >> text=Schedule approved')
    pg.wait_for_function("document.querySelector('#toast').textContent.toLowerCase().includes('future')")
    assert pg.locator('#scheduledPosts .scheduled-item').count() == 0

    fut = datetime.now() + timedelta(days=2)
    pg.answers = [fut.strftime('%Y-%m-%d'), '10:30']
    pg.click('#drafts >> text=Schedule approved')
    pg.wait_for_selector('#scheduledPosts .scheduled-item')
    assert '10:30' in pg.locator('#scheduledPosts').inner_text()

    # calendar month navigation shows the event only in its month
    if fut.month == datetime.now().month:
        assert pg.locator('.calendar-event').count() == 1
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
    assert pg.locator('#draftCount').inner_text() == '2'
    assert 'AUTOMATION MOCK' in pg.locator('#drafts').inner_text()
    pg.fill('#automationName', 'weekly TIP'); pg.fill('#automationTopic', 'dup')
    pg.click('#createAutomation'); pg.wait_for_function("document.querySelector('#toast').textContent.includes('already exists')")

    # integrations labelled honestly
    items = pg.locator('.integration-item .status-pill').all_inner_texts()
    assert len(items) == 4 and all('not connected' in t for t in items)

    # credit reset
    pg.click('#resetCredits'); pg.wait_for_function("document.querySelector('#creditBalance').textContent==='100'")

    assert pg.errors == [], pg.errors


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
