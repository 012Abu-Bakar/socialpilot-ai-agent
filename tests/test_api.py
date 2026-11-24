"""Local API tests. Uses a temporary SQLite DB and never calls an AI provider."""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# Must be set before importing main: skip .env (real keys) and allow the TestClient host.
os.environ['SOCIALPILOT_TESTING'] = '1'
os.environ['SOCIALPILOT_ALLOWED_HOSTS'] = 'testserver,localhost,127.0.0.1'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


class _NoNetwork:  # safety net: any provider client construction fails the test
    def __init__(self, *a, **k): raise AssertionError('external provider call attempted')


PASSWORD = 'correct-horse-battery'


def signup(client, email='owner@example.com', business='Acme', name='Olive Owner', password=PASSWORD):
    r = client.post('/api/auth/register', json={'name': name, 'email': email, 'password': password, 'business_name': business})
    assert r.status_code == 200, r.text
    return r.json()


def new_client(**signup_kw):
    """A second browser: its own cookies, optionally signed up as another person."""
    c = TestClient(main.app)
    if signup_kw: signup(c, **signup_kw)
    main._hits.clear()
    return c


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'test.db')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv('SOCIALPILOT_MOCK_MODE', '1')
    monkeypatch.setenv('SOCIALPILOT_RATE_LIMIT', '1000')
    monkeypatch.setattr(main.anthropic, 'AsyncAnthropic', _NoNetwork)
    main._hits.clear()
    main.init_db()
    return new_client(email='owner@example.com')


def business_id(client): return client.get('/api/me').json()['business']['id']


def future(days=3):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def gen(client, **kw):
    body = {'topic': 'AI for small business', 'brand': 'Acme', 'platform': 'LinkedIn', 'variations': 3}
    body.update(kw)
    return client.post('/api/generate', json=body)


def make_draft(client, **kw):
    body = {'title': 'T', 'content': 'Some content', 'platform': 'LinkedIn', 'topic': 'x'}
    body.update(kw)
    r = client.post('/api/drafts', json=body)
    assert r.status_code == 200
    return r.json()


def approved_draft(client, **kw):
    d = make_draft(client, **kw)
    assert client.post(f"/api/drafts/{d['id']}/submit").status_code == 200
    assert client.post(f"/api/drafts/{d['id']}/approve").status_code == 200
    return d


def schedule(client, draft_id, when=None):
    return client.post('/api/schedule', json={'draft_id': draft_id, 'scheduled_at': when or future()})


# ---- health / startup
def test_health_and_ui(client):
    assert client.get('/api/health').json()['mode'] == 'demo-mock'
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/missing').status_code == 404


def test_health_reports_mock_even_if_key_present(client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'dummy')
    assert client.get('/api/health').json()['mode'] == 'demo-mock'


# ---- security guards
def test_security_headers(client):
    h = client.get('/').headers
    assert h['x-content-type-options'] == 'nosniff' and h['x-frame-options'] == 'DENY'
    assert "script-src 'self'" in h['content-security-policy']


@pytest.mark.parametrize('headers', [{'origin': 'https://evil.example'}, {'sec-fetch-site': 'cross-site'}])
def test_cross_site_writes_blocked(client, headers):
    d = make_draft(client)
    client.post(f"/api/drafts/{d['id']}/submit")
    assert client.post(f"/api/drafts/{d['id']}/approve", headers=headers).status_code == 403
    assert client.post('/api/credits/reset', headers=headers).status_code == 403
    assert client.get('/api/drafts', headers=headers).status_code == 200  # reads unaffected
    assert client.get('/api/drafts').json()[0]['status'] == 'pending_approval'


def test_same_origin_write_allowed(client):
    r = client.post('/api/drafts', json={'title': 't', 'content': 'c'}, headers={'origin': 'http://testserver', 'sec-fetch-site': 'same-origin'})
    assert r.status_code == 200


def test_untrusted_host_rejected(client):
    assert client.get('/api/health', headers={'host': 'attacker.example'}).status_code == 400


def test_rate_limit(client, monkeypatch):
    monkeypatch.setenv('SOCIALPILOT_RATE_LIMIT', '2')
    assert gen(client, variations=1).status_code == 200
    assert gen(client, variations=1).status_code == 200
    assert gen(client, variations=1).status_code == 429
    assert client.get('/api/credits').json()['balance'] == 96  # the rejected call was not charged


# ---- generation + credits + workflows
def test_generate_deducts_credits_and_records_run(client):
    assert client.get('/api/credits').json()['balance'] == 100
    r = gen(client, variations=3)
    d = r.json()
    assert r.status_code == 200 and d['mock'] is True and d['credits_used'] == 4
    assert d['content'].count('Caption:') == 3
    c = client.get('/api/credits').json()
    assert (c['balance'], c['used'], c['lastCost']) == (96, 4, 4)
    runs = client.get('/api/workflows').json()
    assert len(runs) == 1 and runs[0]['credits'] == 4 and runs[0]['variations'] == 3 and runs[0]['platform'] == 'LinkedIn'


@pytest.mark.parametrize('v,cost', [(1, 2), (5, 6)])
def test_variation_boundaries(client, v, cost):
    assert gen(client, variations=v).json()['credits_used'] == cost


@pytest.mark.parametrize('bad', [{'variations': 0}, {'variations': 6}, {'variations': 'x'}, {'topic': ''},
                                 {'topic': '   '}, {'topic': 'a' * 2001}, {'platform': 'TikTok'},
                                 {'brand': 'B' * 121}, {'brand': '  '}, {'tone': 'Sarcastic'}, {'objective': 'Anything'}])
def test_invalid_generation_does_not_charge(client, bad):
    assert gen(client, **bad).status_code == 422
    assert client.get('/api/credits').json()['balance'] == 100
    assert client.get('/api/workflows').json() == []


def test_missing_topic(client):
    assert client.post('/api/generate', json={}).status_code == 422


def test_special_characters_and_long_topic(client):
    assert gen(client, topic='<script>alert(1)</script> \'; DROP TABLE drafts;-- 日本語 😀').status_code == 200
    assert gen(client, topic='a' * 2000).status_code == 200
    assert client.get('/api/drafts').status_code == 200  # tables intact


@pytest.mark.parametrize('platform', ['LinkedIn', 'Instagram'])
def test_platforms_tones_objectives(client, platform):
    for tone in ['Professional', 'Friendly', 'Witty']:
        assert gen(client, platform=platform, tone=tone, objective='Education').status_code == 200


def test_insufficient_credits(client):
    for _ in range(16):  # 16 * 6 = 96
        assert gen(client, variations=5).status_code == 200
    assert client.get('/api/credits').json()['balance'] == 4
    r = gen(client, variations=5)
    assert r.status_code == 402
    assert client.get('/api/credits').json()['balance'] == 4
    assert len(client.get('/api/workflows').json()) == 16


def test_concurrent_generation_cannot_overspend(client):
    for _ in range(15):  # leave 10 credits: room for exactly one 6-credit request
        gen(client, variations=5)
    assert client.get('/api/credits').json()['balance'] == 10
    results, barrier = [], threading.Barrier(8)

    def worker():
        c = TestClient(main.app, cookies=client.cookies)
        barrier.wait()
        results.append(gen(c, variations=5).status_code)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(results) == [200] + [402] * 7
    assert client.get('/api/credits').json()['balance'] == 4


def test_credit_reset_tops_up_without_erasing_history(client):
    gen(client)
    r = client.post('/api/credits/reset').json()
    assert r['balance'] == 100 and r['used'] == 4  # history kept, balance restored
    assert client.post('/api/credits/reset').json()['balance'] == 100  # idempotent
    a = client.get('/api/analytics').json()
    assert (a['generation_runs'], a['credits_consumed']) == (1, 4)  # analytics stay consistent


def test_credit_reset_forbidden_in_ai_mode(client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-not-real')
    monkeypatch.setenv('SOCIALPILOT_MOCK_MODE', '0')
    assert client.post('/api/credits/reset').status_code == 403


# ---- drafts
def test_draft_crud_and_validation(client):
    d = make_draft(client)
    assert d['status'] == 'draft'
    assert client.put(f"/api/drafts/{d['id']}", json={'content': 'new'}).json()['content'] == 'new'
    assert client.put(f"/api/drafts/{d['id']}", json={'content': '   '}).status_code == 422
    assert client.put('/api/drafts/9999', json={'content': 'x'}).status_code == 404
    assert client.post('/api/drafts', json={'title': '', 'content': 'x'}).status_code == 422
    assert client.post('/api/drafts', json={'title': 't', 'content': '   '}).status_code == 422
    assert client.post('/api/drafts', json={'title': 't', 'content': 'x', 'platform': 'Fax'}).status_code == 422
    assert client.post('/api/drafts', json={'title': 't', 'content': 'x' * 10001}).status_code == 422
    assert client.post('/api/drafts', json={'title': 't' * 251, 'content': 'x'}).status_code == 422
    assert client.delete(f"/api/drafts/{d['id']}").status_code == 200
    assert client.delete(f"/api/drafts/{d['id']}").status_code == 404
    assert client.get('/api/drafts').json() == []


# ---- approval
def test_approval_flow_and_invalid_transitions(client):
    d = make_draft(client)
    i = d['id']
    assert client.post(f'/api/drafts/{i}/approve').status_code == 409      # not pending
    assert client.post(f'/api/drafts/{i}/reject', json={'reason': 'x'}).status_code == 409
    assert client.post(f'/api/drafts/{i}/submit').json()['status'] == 'pending_approval'
    assert client.post(f'/api/drafts/{i}/submit').status_code == 409       # duplicate submit
    assert [x['id'] for x in client.get('/api/approvals').json()] == [i]
    assert client.post(f'/api/drafts/{i}/reject', json={'reason': 'r' * 1001}).status_code == 422
    r = client.post(f'/api/drafts/{i}/reject', json={'reason': 'too long'}).json()
    assert r['status'] == 'rejected' and r['rejection_reason'] == 'too long'
    assert client.post(f'/api/drafts/{i}/approve').status_code == 409      # rejected can't be approved
    assert client.post(f'/api/drafts/{i}/submit').json()['rejection_reason'] == ''  # resubmit
    assert client.post(f'/api/drafts/{i}/approve').json()['status'] == 'approved'
    assert client.post(f'/api/drafts/{i}/approve').status_code == 409
    assert client.post('/api/drafts/9999/submit').status_code == 404


def test_editing_approved_draft_requires_reapproval(client):
    d = approved_draft(client)
    r = client.put(f"/api/drafts/{d['id']}", json={'content': 'changed after approval'})
    assert r.status_code == 200 and r.json()['status'] == 'draft'


def test_noop_edit_keeps_approval(client):
    d = approved_draft(client)
    assert client.put(f"/api/drafts/{d['id']}", json={}).json()['status'] == 'approved'
    assert client.put(f"/api/drafts/{d['id']}", json={'content': 'Some content'}).json()['status'] == 'approved'


def test_cannot_edit_pending_draft(client):
    d = make_draft(client)
    client.post(f"/api/drafts/{d['id']}/submit")
    assert client.put(f"/api/drafts/{d['id']}", json={'content': 'sneaky'}).status_code == 409


# ---- scheduling
def test_schedule_flow(client):
    d = approved_draft(client)
    r = schedule(client, d['id'])
    assert r.status_code == 200 and r.json()['status'] == 'scheduled'
    assert schedule(client, d['id']).status_code == 409                    # duplicate
    assert client.get('/api/drafts').json()[0]['status'] == 'scheduled'
    assert len(client.get('/api/schedule').json()) == 1
    pid = r.json()['id']
    assert client.delete(f'/api/schedule/{pid}').status_code == 200
    assert client.get('/api/schedule').json() == []
    assert client.get('/api/drafts').json()[0]['status'] == 'approved'
    assert client.delete(f'/api/schedule/{pid}').status_code == 409        # already cancelled
    assert client.delete('/api/schedule/9999').status_code == 404
    assert schedule(client, d['id']).status_code == 200                    # reschedule after cancel


def test_schedule_validation(client):
    d = approved_draft(client)
    assert schedule(client, d['id'], 'garbage').status_code == 400
    assert schedule(client, d['id'], '2020-01-01T09:00:00Z').status_code == 400
    naive = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%dT09:00:00')
    assert schedule(client, d['id'], naive).status_code == 400            # timezone required
    assert schedule(client, make_draft(client)['id']).status_code == 409   # unapproved
    assert schedule(client, 9999).status_code == 404


@pytest.mark.parametrize('body', [{'title': 'x', 'content': 'unapproved', 'platform': 'LinkedIn'},
                                  {'draft_id': 0}, {'draft_id': -1}, {'draft_id': None}])
def test_schedule_requires_approved_draft(client, body):
    """Regression: scheduling without a draft (or draft_id=0) used to bypass approval."""
    r = client.post('/api/schedule', json={**body, 'scheduled_at': future()})
    assert r.status_code == 422
    assert client.get('/api/schedule').json() == []


def test_schedule_uses_stored_draft_content_and_platform(client):
    d = approved_draft(client, platform='LinkedIn')
    r = client.post('/api/schedule', json={'draft_id': d['id'], 'title': 'T', 'content': 'TAMPERED',
                                           'platform': 'Instagram', 'scheduled_at': future()})
    assert r.json()['content'] == 'Some content' and r.json()['platform'] == 'LinkedIn'


def test_schedule_time_normalised_to_utc(client):
    d = approved_draft(client)
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    local = when.astimezone(timezone(timedelta(hours=5))).isoformat()  # same instant, +05:00
    assert schedule(client, d['id'], local).json()['scheduled_at'] == when.isoformat()


def test_concurrent_schedule_creates_one_post(client):
    d = approved_draft(client)
    results, barrier = [], threading.Barrier(6)

    def worker():
        c = TestClient(main.app, cookies=client.cookies)
        barrier.wait()
        results.append(schedule(c, d['id']).status_code)
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(results) == [200] + [409] * 5
    assert len(client.get('/api/schedule').json()) == 1


def test_cannot_delete_scheduled_draft(client):
    d = approved_draft(client)
    schedule(client, d['id'])
    assert client.delete(f"/api/drafts/{d['id']}").status_code == 409
    assert len(client.get('/api/schedule').json()) == 1


# ---- automation
def rule(**kw):
    b = {'name': 'Weekly', 'topic_template': 'Topic', 'platform': 'LinkedIn'}
    b.update(kw)
    return b


def test_automation_flow(client):
    r = client.post('/api/automations', json=rule())
    assert r.status_code == 200
    rid = r.json()['id']
    run = client.post(f'/api/automations/{rid}/run').json()
    assert run['status'] == 'success'
    drafts = client.get('/api/drafts').json()
    assert len(drafts) == 1 and drafts[0]['content'].startswith('[AUTOMATION MOCK')
    assert drafts[0]['status'] == 'draft'  # automation output still goes through approval
    assert len(client.get('/api/automation-executions').json()) == 1
    assert client.put(f'/api/automations/{rid}', json={'enabled': False}).json()['enabled'] == 0
    assert client.post(f'/api/automations/{rid}/run').status_code == 409
    assert len(client.get('/api/automation-executions').json()) == 1
    assert client.delete(f'/api/automations/{rid}').status_code == 200
    assert client.delete(f'/api/automations/{rid}').status_code == 404
    assert client.post(f'/api/automations/{rid}/run').status_code == 404
    assert client.get('/api/credits').json()['balance'] == 100  # no credits used


@pytest.mark.parametrize('bad', [{'name': ''}, {'name': '   '}, {'topic_template': ''}, {'topic_template': '  '},
                                 {'platform': 'Fax'}, {'trigger_type': 'cron'}, {'action_type': 'publish'},
                                 {'name': 'n' * 121}])
def test_invalid_automation(client, bad):
    assert client.post('/api/automations', json=rule(**bad)).status_code == 422


def test_duplicate_automation_name(client):
    assert client.post('/api/automations', json=rule()).status_code == 200
    assert client.post('/api/automations', json=rule(name='weekly')).status_code == 409


def test_rename_automation_to_duplicate_name(client):
    client.post('/api/automations', json=rule())
    other = client.post('/api/automations', json=rule(name='Daily')).json()['id']
    assert client.put(f'/api/automations/{other}', json={'name': 'WEEKLY'}).status_code == 409
    assert client.put(f'/api/automations/{other}', json={'name': 'Daily'}).status_code == 200  # own name is fine


def test_db_unique_index_backs_up_name_check(client):
    client.post('/api/automations', json=rule())
    with pytest.raises(main.sqlite3.IntegrityError), main.db(write=True) as c:
        c.execute("INSERT INTO automation_rules(business_id,name,trigger_type,action_type,topic_template,platform,created_at,updated_at) "
                  "VALUES(?,'WEEKLY','manual','create_draft','t','LinkedIn','x','x')", (business_id(client),))


# ---- integrations + analytics
def test_integrations_all_not_connected(client):
    items = client.get('/api/integrations').json()
    assert {i['provider'] for i in items} == {'n8n', 'LinkedIn', 'Instagram', 'Analytics provider'}
    assert all(i['status'] == 'not_connected' for i in items)


def test_analytics_empty_and_populated(client):
    a = client.get('/api/analytics').json()
    assert (a['generation_runs'], a['credits_consumed'], a['scheduled_posts'], a['published_posts']) == (0, 0, 0, 0)
    assert a['drafts_by_status'] == [] and a['drafts_by_platform'] == []
    gen(client, variations=2)
    d = approved_draft(client)
    make_draft(client, platform='Instagram')
    schedule(client, d['id'])
    rid = client.post('/api/automations', json=rule()).json()['id']
    client.post(f'/api/automations/{rid}/run')
    a = client.get('/api/analytics').json()
    assert a['generation_runs'] == 1 and a['credits_consumed'] == 3
    assert a['scheduled_posts'] == 1 and a['published_posts'] == 0
    assert a['automation_rules'] == 1 and a['automation_executions'] == 1
    st = {x['status']: x['count'] for x in a['drafts_by_status']}
    assert st == {'scheduled': 1, 'draft': 2}
    pl = {x['platform']: x['count'] for x in a['drafts_by_platform']}
    assert pl == {'LinkedIn': 2, 'Instagram': 1}


# ---- real-provider code path, with the SDK client faked (no network, no cost)
_REQ = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')


def api_error(status, body_text):
    return main.anthropic.APIStatusError(body_text, response=httpx2.Response(status, request=_REQ, headers={'request-id': 'req_1'}), body=None)


def message(text='1. Hello AI', stop_reason='end_turn'):
    blocks = [SimpleNamespace(type='text', text=text)] if text is not None else []
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


@pytest.fixture()
def ai_client(client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-not-real')
    monkeypatch.setenv('SOCIALPILOT_MOCK_MODE', '0')
    calls = []

    def install(result=None, exc=None):
        class FakeAsyncAnthropic:
            def __init__(self, **kw): self.messages = self; self.kw = kw
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def create(self, **kw):
                calls.append(kw)
                if exc: raise exc
                return result
        monkeypatch.setattr(main.anthropic, 'AsyncAnthropic', FakeAsyncAnthropic)
    client.install, client.calls = install, calls
    return client


def test_ai_path_success_charges_and_logs(ai_client):
    ai_client.install(message())
    assert ai_client.get('/api/health').json()['mode'] == 'ai-enabled'
    r = gen(ai_client, variations=2)
    assert r.status_code == 200 and r.json() == {'content': '1. Hello AI', 'mock': False, 'credits_used': 3}
    kw = ai_client.calls[0]
    assert kw['model'] == 'claude-haiku-5-5' and kw['messages'][0]['content'].count('Acme') == 1
    c = ai_client.get('/api/credits').json()
    assert (c['balance'], c['used']) == (97, 3)
    assert len(ai_client.get('/api/workflows').json()) == 1


def test_ai_model_from_env(ai_client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_MODEL', 'claude-sonnet-5-5')
    ai_client.install(message())
    gen(ai_client)
    assert ai_client.calls[0]['model'] == 'claude-sonnet-5-5'


@pytest.mark.parametrize('result,exc,status', [
    (None, api_error(500, 'secret upstream body'), 502),
    (None, api_error(401, 'invalid x-api-key'), 502),   # provider auth errors must not look like ours
    (None, api_error(429, 'rate limited'), 502),
    (message(text=None), None, 502),
    (message(text='partial', stop_reason='refusal'), None, 502),
    (None, main.anthropic.APITimeoutError(request=_REQ), 504),
    (None, main.anthropic.APIConnectionError(request=_REQ), 502),
])
def test_ai_failures_do_not_charge_or_leak(ai_client, result, exc, status):
    ai_client.install(result, exc)
    r = gen(ai_client)
    assert r.status_code == status
    assert 'secret upstream body' not in r.text and 'test-key' not in r.text
    c = ai_client.get('/api/credits').json()
    assert (c['balance'], c['used']) == (100, 0)  # reservation released, ledger clean
    assert ai_client.get('/api/workflows').json() == []


def test_ai_insufficient_credits_never_calls_provider(ai_client):
    """Regression: the paid provider call used to happen before the balance check."""
    ai_client.install(message())
    biz = business_id(ai_client)
    with main.db(write=True) as c:
        c.execute('INSERT INTO credit_ledger(business_id,amount,reason,created_at) VALUES(?,?,?,?)', (biz, -99, 'drain', main.now()))
    assert gen(ai_client, variations=3).status_code == 402
    assert ai_client.calls == []


# ---- accounts + sessions
def test_api_requires_sign_in(client):
    anon = new_client()
    assert anon.get('/api/health').status_code == 200 and anon.get('/').status_code == 200
    for method, path in [('get', '/api/drafts'), ('get', '/api/credits'), ('get', '/api/me'), ('post', '/api/generate'),
                         ('get', '/api/analytics'), ('get', '/api/connections'), ('post', '/api/publishing/run-due')]:
        assert getattr(anon, method)(path).status_code == 401, path


def test_register_login_logout(client):
    me = client.get('/api/me').json()
    assert me['user']['email'] == 'owner@example.com' and me['business'] == {'id': me['business']['id'], 'name': 'Acme', 'role': 'owner'}
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/me').status_code == 401
    assert client.post('/api/auth/login', json={'email': 'owner@example.com', 'password': 'wrong-password'}).status_code == 401
    assert client.post('/api/auth/login', json={'email': 'nobody@example.com', 'password': PASSWORD}).status_code == 401
    r = client.post('/api/auth/login', json={'email': ' OWNER@Example.com ', 'password': PASSWORD})
    assert r.status_code == 200 and r.json()['business']['name'] == 'Acme'
    assert client.get('/api/drafts').status_code == 200


def test_session_cookie_is_http_only_and_strict(client):
    r = new_client().post('/api/auth/register', json={'name': 'A', 'email': 'a@example.com', 'password': PASSWORD, 'business_name': 'B'})
    cookie = r.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie


@pytest.mark.parametrize('bad', [{'email': 'not-an-email'}, {'password': 'short'}, {'name': '  '}, {'business_name': ''}, {'password': 'p' * 129}])
def test_register_validation(client, bad):
    body = {'name': 'N', 'email': 'new@example.com', 'password': PASSWORD, 'business_name': 'B', **bad}
    assert new_client().post('/api/auth/register', json=body).status_code == 422


def test_duplicate_email_rejected(client):
    r = new_client().post('/api/auth/register', json={'name': 'X', 'email': 'Owner@example.com', 'password': PASSWORD, 'business_name': 'Other'})
    assert r.status_code == 409


def test_passwords_are_hashed(client):
    with main.db() as c: stored = c.execute('SELECT password_hash FROM users').fetchone()[0]
    assert stored.startswith('scrypt$') and PASSWORD not in stored
    assert main.verify_password(PASSWORD, stored) and not main.verify_password('nope', stored)


def test_login_is_rate_limited(client, monkeypatch):
    monkeypatch.setenv('SOCIALPILOT_RATE_LIMIT', '3')
    anon = new_client()
    codes = [anon.post('/api/auth/login', json={'email': 'owner@example.com', 'password': f'guess-{i}'}).status_code for i in range(4)]
    assert codes == [401, 401, 401, 429]


# ---- business accounts are isolated
def test_businesses_cannot_see_or_touch_each_other(client):
    mine = make_draft(client, title='Mine')
    rid = client.post('/api/automations', json=rule()).json()['id']
    gen(client)
    other = new_client(email='other@example.com', business='Globex')
    assert other.get('/api/drafts').json() == [] and other.get('/api/workflows').json() == []
    assert other.get('/api/automations').json() == [] and other.get('/api/credits').json()['balance'] == 100
    i = mine['id']
    assert other.put(f'/api/drafts/{i}', json={'content': 'x'}).status_code == 404
    assert other.delete(f'/api/drafts/{i}').status_code == 404
    assert other.post(f'/api/drafts/{i}/submit').status_code == 404
    assert other.post(f'/api/automations/{rid}/run').status_code == 404
    client.post(f'/api/drafts/{i}/submit'); client.post(f'/api/drafts/{i}/approve')
    assert schedule(other, i).status_code == 404
    pid = schedule(client, i).json()['id']
    assert other.delete(f'/api/schedule/{pid}').status_code == 404
    assert other.post(f'/api/schedule/{pid}/publish').status_code == 404
    assert other.get('/api/analytics').json()['scheduled_posts'] == 0
    assert other.post('/api/automations', json=rule()).status_code == 200  # rule names are unique per business only
    assert client.get('/api/credits').json()['balance'] == 96


def test_one_person_many_businesses(client):
    make_draft(client, title='Acme draft')
    first = business_id(client)
    me = client.post('/api/businesses', json={'name': 'Second Brand'}).json()
    assert me['business']['name'] == 'Second Brand' and me['business']['role'] == 'owner' and len(me['businesses']) == 2
    assert client.get('/api/drafts').json() == [] and client.get('/api/credits').json()['balance'] == 100
    assert client.post('/api/session/business', json={'business_id': first}).json()['business']['id'] == first
    assert [d['title'] for d in client.get('/api/drafts').json()] == ['Acme draft']
    stranger = new_client(email='s@example.com', business='Stranger Co')
    assert client.post('/api/session/business', json={'business_id': business_id(stranger)}).status_code == 404


# ---- team roles
def team(client):
    """Adds an editor and an approver to the owner's business; returns their clients, switched into it."""
    biz = business_id(client)
    people = {}
    for who in ('editor', 'approver'):
        c = new_client(email=f'{who}@example.com', business=f'{who} own co')
        assert client.post('/api/members', json={'email': f'{who}@example.com', 'role': who}).status_code == 200
        assert c.post('/api/session/business', json={'business_id': biz}).json()['business']['role'] == who
        people[who] = c
    return people['editor'], people['approver']


def test_roles_enforced(client):
    editor, approver = team(client)
    d = make_draft(editor)
    assert editor.post(f"/api/drafts/{d['id']}/submit").status_code == 200
    assert editor.post(f"/api/drafts/{d['id']}/approve").status_code == 403   # editors cannot approve
    assert approver.post(f"/api/drafts/{d['id']}/approve").json()['status'] == 'approved'
    assert editor.post('/api/credits/reset').status_code == 403
    assert approver.post('/api/members', json={'email': 'x@example.com'}).status_code == 403
    assert editor.post('/api/connections', json={'platform': 'LinkedIn', 'account_name': 'x'}).status_code == 403
    assert {m['role'] for m in editor.get('/api/members').json()} == {'owner', 'editor', 'approver'}


def test_member_management(client):
    editor, _ = team(client)
    eid = editor.get('/api/me').json()['user']['id']
    me = client.get('/api/me').json()['user']['id']
    assert client.post('/api/members', json={'email': 'nobody@example.com'}).status_code == 404
    assert client.post('/api/members', json={'email': 'EDITOR@example.com'}).status_code == 409
    assert client.put(f'/api/members/{me}', json={'role': 'editor'}).status_code == 409   # last owner
    assert client.delete(f'/api/members/{me}').status_code == 409
    assert client.put(f'/api/members/{eid}', json={'role': 'approver'}).json()['role'] == 'approver'
    assert client.delete(f'/api/members/{eid}').status_code == 200
    assert editor.get('/api/drafts').status_code == 401   # their session into this business ends
    assert client.delete(f'/api/members/{eid}').status_code == 404


# ---- channels + publishing
def connect(client, platform='LinkedIn', name='Acme Page'):
    r = client.post('/api/connections', json={'platform': platform, 'account_name': name})
    assert r.status_code == 200, r.text
    return r.json()


def scheduled(client, **kw):
    d = approved_draft(client, **kw)
    return d, schedule(client, d['id']).json()


def make_due(post_id):
    with main.db(write=True) as c:
        c.execute('UPDATE scheduled_posts SET scheduled_at=? WHERE id=?', ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), post_id))


def test_connections(client):
    ch = connect(client)
    assert ch['mode'] == 'demo' and ch['status'] == 'connected'
    assert client.post('/api/connections', json={'platform': 'LinkedIn', 'account_name': 'again'}).status_code == 409
    items = {i['provider']: i for i in client.get('/api/integrations').json()}
    assert items['LinkedIn']['status'] == 'demo_connected' and items['Instagram']['status'] == 'not_connected'
    other = new_client(email='other@example.com', business='Globex')
    assert other.delete(f"/api/connections/{ch['id']}").status_code == 404
    assert client.delete(f"/api/connections/{ch['id']}").status_code == 200
    assert client.delete(f"/api/connections/{ch['id']}").status_code == 404
    assert client.get('/api/connections').json() == []
    connect(client)  # can reconnect after disconnecting


def test_publish_now(client):
    connect(client)
    d, post = scheduled(client)
    r = client.post(f"/api/schedule/{post['id']}/publish").json()
    assert r['status'] == 'published' and r['external_id'].startswith('demo-linkedin-') and r['published_at'] and r['attempts'] == 1
    assert client.get('/api/drafts').json()[0]['status'] == 'published'
    assert client.get('/api/schedule').json() == []
    assert [p['id'] for p in client.get('/api/publishing').json()] == [post['id']]
    assert client.post(f"/api/schedule/{post['id']}/publish").status_code == 409            # never twice
    assert client.put(f"/api/drafts/{d['id']}", json={'content': 'edit'}).status_code == 409  # published content is locked
    assert client.delete(f"/api/schedule/{post['id']}").status_code == 409
    a = client.get('/api/analytics').json()
    assert (a['published_posts'], a['failed_posts'], a['scheduled_posts']) == (1, 0, 0)
    assert a['published_by_platform'] == [{'platform': 'LinkedIn', 'count': 1}]


def test_publish_without_channel_fails_then_retry(client):
    _, post = scheduled(client)
    r = client.post(f"/api/schedule/{post['id']}/publish").json()
    assert r['status'] == 'failed' and 'No LinkedIn channel' in r['last_error']
    assert client.get('/api/drafts').json()[0]['status'] == 'scheduled'
    assert client.get('/api/analytics').json()['failed_posts'] == 1
    connect(client)
    r = client.post(f"/api/schedule/{post['id']}/publish").json()
    assert r['status'] == 'published' and r['attempts'] == 2 and r['last_error'] == ''


def test_platform_limits_enforced_and_failed_post_can_be_cancelled(client):
    connect(client, 'Instagram', 'acme.ig')
    _, post = scheduled(client, platform='Instagram', content='x' * 2201)
    r = client.post(f"/api/schedule/{post['id']}/publish").json()
    assert r['status'] == 'failed' and 'limited to 2200' in r['last_error']
    assert client.delete(f"/api/schedule/{post['id']}").status_code == 200
    assert client.get('/api/drafts').json()[0]['status'] == 'approved'   # back to approved, ready to edit
    assert client.post(f"/api/schedule/{post['id']}/publish").status_code == 409


def test_run_due_publishes_only_due_posts_of_this_business(client):
    connect(client)
    _, due = scheduled(client, title='due')
    _, later = scheduled(client, title='later')
    other = new_client(email='other@example.com', business='Globex')
    connect(other)
    _, theirs = scheduled(other)
    make_due(due['id']); make_due(theirs['id'])
    r = client.post('/api/publishing/run-due').json()
    assert (r['processed'], r['published'], r['failed']) == (1, 1, 0) and r['posts'][0]['id'] == due['id']
    assert [p['id'] for p in client.get('/api/schedule').json()] == [later['id']]
    assert other.get('/api/schedule').json()[0]['status'] == 'scheduled'   # another business's post is left alone


def test_background_publisher(client, monkeypatch):
    connect(client)
    _, post = scheduled(client)
    make_due(post['id'])
    monkeypatch.setenv('SOCIALPILOT_PUBLISH_INTERVAL', '0.05')
    with TestClient(main.app, cookies=client.cookies) as live:   # entering the client runs the app lifespan
        for _ in range(100):
            if live.get('/api/publishing').json(): break
            main.time.sleep(0.05)
    assert client.get('/api/publishing').json()[0]['status'] == 'published'


def test_concurrent_publish_posts_once(client):
    connect(client)
    _, post = scheduled(client)
    results, barrier = [], threading.Barrier(6)

    def worker():
        c = TestClient(main.app, cookies=client.cookies)
        barrier.wait()
        results.append(c.post(f"/api/schedule/{post['id']}/publish").status_code)
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(results) == [200] + [409] * 5
    assert client.get('/api/publishing').json()[0]['attempts'] == 1


def test_interrupted_publish_is_marked_failed_on_startup(client):
    _, post = scheduled(client)
    with main.db(write=True) as c: c.execute("UPDATE scheduled_posts SET status='publishing' WHERE id=?", (post['id'],))
    main.init_db()
    p = client.get('/api/publishing').json()[0]
    assert p['status'] == 'failed' and 'Interrupted' in p['last_error']


def test_publisher_crash_is_reported_without_details(client, monkeypatch):
    connect(client)
    _, post = scheduled(client)
    def boom(post, channel): raise RuntimeError('secret token abc')
    monkeypatch.setitem(main.PUBLISHERS, 'demo', boom)
    r = client.post(f"/api/schedule/{post['id']}/publish")
    assert r.json()['status'] == 'failed' and 'secret' not in r.text


# ---- shared demo login
def test_demo_account_seeded_from_env(client, monkeypatch):
    main.seed_demo_account()  # no env vars: nothing is created
    assert new_client().post('/api/auth/login', json={'email': 'demo@example.com', 'password': 'demo-password-1'}).status_code == 401
    monkeypatch.setenv('SOCIALPILOT_DEMO_EMAIL', 'Demo@Example.com')
    monkeypatch.setenv('SOCIALPILOT_DEMO_PASSWORD', 'demo-password-1')
    main.seed_demo_account()
    main.seed_demo_account()  # idempotent
    demo = new_client()
    me = demo.post('/api/auth/login', json={'email': 'demo@example.com', 'password': 'demo-password-1'}).json()
    assert me['business']['name'] == 'SocialPilot Demo' and me['business']['role'] == 'owner'
    assert {d['status'] for d in demo.get('/api/drafts').json()} == {'published', 'scheduled', 'pending_approval', 'draft'}
    assert len(demo.get('/api/connections').json()) == 2 and len(demo.get('/api/schedule').json()) == 1
    a = demo.get('/api/analytics').json()
    assert (a['published_posts'], a['generation_runs'], a['credits_consumed']) == (1, 2, 7)
    assert demo.get('/api/credits').json()['balance'] == 93
    assert client.get('/api/drafts').json() == []  # other businesses are unaffected
    with main.db() as c: assert c.execute('SELECT COUNT(*) FROM users WHERE email=?', ('demo@example.com',)).fetchone()[0] == 1


# ---- upgrading a database from before accounts existed
LEGACY_SCHEMA = '''
CREATE TABLE drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, content TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'LinkedIn', topic TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft', rejection_reason TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE credit_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE automation_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, trigger_type TEXT NOT NULL, action_type TEXT NOT NULL, topic_template TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'LinkedIn', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX ux_automation_name ON automation_rules(name COLLATE NOCASE);
INSERT INTO drafts(title,content,created_at,updated_at) VALUES('Old draft','c','x','x');
INSERT INTO credit_ledger(amount,reason,created_at) VALUES(100,'Initial demo credit balance','x'), (-6,'old generation','x');
'''


def test_legacy_data_is_adopted_by_first_account(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'legacy.db')
    con = main.sqlite3.connect(main.DB_PATH)
    con.executescript(LEGACY_SCHEMA)
    con.commit(); con.close()
    main.init_db()
    first = new_client(email='first@example.com', business='Original')
    assert [d['title'] for d in first.get('/api/drafts').json()] == ['Old draft']
    assert first.get('/api/credits').json()['balance'] == 94   # legacy ledger kept, no second top-up
    second = new_client(email='second@example.com', business='New')
    assert second.get('/api/drafts').json() == [] and second.get('/api/credits').json()['balance'] == 100
