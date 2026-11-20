"""Local API tests. Uses a temporary SQLite DB and never calls an AI provider."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'test.db')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv('SOCIALPILOT_MOCK_MODE', '1')

    async def _no_network(*a, **k):  # safety net: any provider call fails the test
        raise AssertionError('external provider call attempted')
    monkeypatch.setattr(main.httpx.AsyncClient, 'post', _no_network)
    main.init_db()
    return TestClient(main.app)


def future(days=3):
    return (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%dT09:00:00')


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


def approved_draft(client):
    d = make_draft(client)
    assert client.post(f"/api/drafts/{d['id']}/submit").status_code == 200
    assert client.post(f"/api/drafts/{d['id']}/approve").status_code == 200
    return d


# ---- health / startup
def test_health_and_ui(client):
    assert client.get('/api/health').json()['mode'] == 'demo-mock'
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/missing').status_code == 404


def test_health_reports_mock_even_if_key_present(client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'dummy')
    assert client.get('/api/health').json()['mode'] == 'demo-mock'


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
                                 {'topic': '   '}, {'topic': 'a' * 2001}, {'platform': 'TikTok'}])
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


def test_credit_reset(client):
    gen(client)
    assert client.post('/api/credits/reset').json()['balance'] == 100


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


def test_cannot_edit_pending_draft(client):
    d = make_draft(client)
    client.post(f"/api/drafts/{d['id']}/submit")
    assert client.put(f"/api/drafts/{d['id']}", json={'content': 'sneaky'}).status_code == 409


# ---- scheduling
def test_schedule_flow(client):
    d = approved_draft(client)
    body = {'draft_id': d['id'], 'title': 'T', 'content': 'Some content', 'platform': 'LinkedIn', 'scheduled_at': future()}
    r = client.post('/api/schedule', json=body)
    assert r.status_code == 200 and r.json()['status'] == 'scheduled'
    assert client.post('/api/schedule', json=body).status_code == 409      # duplicate
    assert client.get('/api/drafts').json()[0]['status'] == 'scheduled'
    assert len(client.get('/api/schedule').json()) == 1
    pid = r.json()['id']
    assert client.delete(f'/api/schedule/{pid}').status_code == 200
    assert client.get('/api/schedule').json() == []
    assert client.get('/api/drafts').json()[0]['status'] == 'approved'
    assert client.delete(f'/api/schedule/{pid}').status_code == 409        # already cancelled
    assert client.delete('/api/schedule/9999').status_code == 404


def test_schedule_validation(client):
    d = approved_draft(client)
    base = {'draft_id': d['id'], 'title': 'T', 'content': 'C', 'platform': 'LinkedIn'}
    assert client.post('/api/schedule', json={**base, 'scheduled_at': 'garbage'}).status_code == 400
    assert client.post('/api/schedule', json={**base, 'scheduled_at': '2020-01-01T09:00:00'}).status_code == 400
    assert client.post('/api/schedule', json={**base, 'scheduled_at': future(), 'title': ''}).status_code == 422
    unapproved = make_draft(client)
    r = client.post('/api/schedule', json={**base, 'draft_id': unapproved['id'], 'scheduled_at': future()})
    assert r.status_code == 409
    r = client.post('/api/schedule', json={**base, 'draft_id': 9999, 'scheduled_at': future()})
    assert r.status_code == 404


def test_schedule_uses_stored_draft_content(client):
    d = approved_draft(client)
    r = client.post('/api/schedule', json={'draft_id': d['id'], 'title': 'T', 'content': 'TAMPERED',
                                           'platform': 'LinkedIn', 'scheduled_at': future()})
    assert r.json()['content'] == 'Some content'


def test_cannot_delete_scheduled_draft(client):
    d = approved_draft(client)
    client.post('/api/schedule', json={'draft_id': d['id'], 'title': 'T', 'content': 'C', 'platform': 'LinkedIn', 'scheduled_at': future()})
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
    assert len(client.get('/api/automation-executions').json()) == 1
    assert client.put(f'/api/automations/{rid}', json={'enabled': False}).json()['enabled'] == 0
    assert client.post(f'/api/automations/{rid}/run').status_code == 409
    assert len(client.get('/api/automation-executions').json()) == 1
    assert client.delete(f'/api/automations/{rid}').status_code == 200
    assert client.post(f'/api/automations/{rid}/run').status_code == 404
    assert client.get('/api/credits').json()['balance'] == 100  # no credits used


@pytest.mark.parametrize('bad', [{'name': ''}, {'name': '   '}, {'topic_template': ''}, {'topic_template': '  '},
                                 {'platform': 'Fax'}, {'trigger_type': 'cron'}, {'action_type': 'publish'}])
def test_invalid_automation(client, bad):
    assert client.post('/api/automations', json=rule(**bad)).status_code == 422


def test_duplicate_automation_name(client):
    assert client.post('/api/automations', json=rule()).status_code == 200
    assert client.post('/api/automations', json=rule(name='weekly')).status_code == 409


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
    client.post('/api/schedule', json={'draft_id': d['id'], 'title': 'T', 'content': 'C', 'platform': 'LinkedIn', 'scheduled_at': future()})
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


# ---- real-provider code path, with the HTTP layer faked (no network, no cost)
class FakeResp:
    def __init__(self, status=200, data=None, text=''):
        self.status_code, self._d, self.text = status, data or {}, text
    def json(self): return self._d


@pytest.fixture()
def ai_client(client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key-not-real')
    monkeypatch.setenv('SOCIALPILOT_MOCK_MODE', '0')
    calls = []

    def install(resp=None, exc=None):
        async def fake_post(self, url, **kw):
            calls.append((url, kw))
            if exc: raise exc
            return resp
        monkeypatch.setattr(main.httpx.AsyncClient, 'post', fake_post)
    client.install, client.calls = install, calls
    return client


def test_ai_path_success_charges_and_logs(ai_client):
    ai_client.install(FakeResp(200, {'content': [{'type': 'text', 'text': '1. Hello AI'}]}))
    assert ai_client.get('/api/health').json()['mode'] == 'ai-enabled'
    r = gen(ai_client, variations=2)
    assert r.status_code == 200 and r.json() == {'content': '1. Hello AI', 'mock': False, 'credits_used': 3}
    url, kw = ai_client.calls[0]
    assert url.startswith('https://api.anthropic.com/') and kw['json']['messages'][0]['content'].count('Acme') == 1
    assert ai_client.get('/api/credits').json()['balance'] == 97
    assert len(ai_client.get('/api/workflows').json()) == 1


@pytest.mark.parametrize('resp,exc,status', [
    (FakeResp(500, text='secret upstream body'), None, 500),
    (FakeResp(401, text='invalid x-api-key'), None, 401),
    (FakeResp(200, {'content': []}), None, 502),
    (None, main.httpx.TimeoutException('t'), 504),
    (None, main.httpx.ConnectError('c'), 502),
])
def test_ai_failures_do_not_charge_or_leak(ai_client, resp, exc, status):
    ai_client.install(resp, exc)
    r = gen(ai_client)
    assert r.status_code == status
    assert 'secret upstream body' not in r.text and 'test-key' not in r.text
    assert ai_client.get('/api/credits').json()['balance'] == 100
    assert ai_client.get('/api/workflows').json() == []
