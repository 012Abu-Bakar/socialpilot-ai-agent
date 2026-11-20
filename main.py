"""SocialPilot AI — FastAPI backend (local demo).

Mock generation is the default; the Anthropic API is only called when
SOCIALPILOT_MOCK_MODE=0 and ANTHROPIC_API_KEY is set.
"""
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

if os.getenv('SOCIALPILOT_TESTING') != '1':  # tests must never pick up a real key from .env
    load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('socialpilot')

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('SOCIALPILOT_DB') or BASE_DIR / 'socialpilot.db')
DEMO_CREDITS = 100
DEFAULT_MODEL = 'claude-haiku-5-5'
ALLOWED_HOSTS = [h.strip() for h in os.getenv('SOCIALPILOT_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]

app = FastAPI(title='SocialPilot AI')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)  # blocks DNS-rebinding
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'static')), name='static')


def now(): return datetime.now(timezone.utc).isoformat()
def mock_mode(): return os.getenv('SOCIALPILOT_MOCK_MODE', '1') == '1' or not os.getenv('ANTHROPIC_API_KEY')


# ---------------------------------------------------------------- database
@contextmanager
def db(write=False):
    """One connection per unit of work. write=True takes the write lock up front (BEGIN IMMEDIATE)
    so check-then-write sequences (credits, status transitions) cannot interleave."""
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        yield conn
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction: conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, content TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'LinkedIn', topic TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft', rejection_reason TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scheduled_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, title TEXT NOT NULL, content TEXT NOT NULL, platform TEXT NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, platform TEXT NOT NULL, variations INTEGER NOT NULL, credits INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS credit_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, trigger_type TEXT NOT NULL, action_type TEXT NOT NULL, topic_template TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'LinkedIn', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_executions (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER, rule_name TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')
        # Database-level guarantees; skipped with a warning if legacy data already violates them.
        for sql in ("CREATE UNIQUE INDEX IF NOT EXISTS ux_automation_name ON automation_rules(name COLLATE NOCASE)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_active_schedule ON scheduled_posts(draft_id) WHERE status='scheduled'"):
            try: conn.execute(sql)
            except sqlite3.IntegrityError: logger.warning('Could not create index (existing duplicates): %s', sql)
        if conn.execute('SELECT COUNT(*) FROM credit_ledger').fetchone()[0] == 0:
            conn.execute('INSERT INTO credit_ledger(amount,reason,created_at) VALUES(?,?,?)', (DEMO_CREDITS, 'Initial demo credit balance', now()))
        conn.commit()
    finally:
        conn.close()


init_db()


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse({'detail': 'Request conflicts with existing data'}, status_code=409)


# ---------------------------------------------------------------- request guards
@app.middleware('http')
async def security_guard(request: Request, call_next):
    # CSRF: the API has no cookies, but a page on another site could still fire simple POSTs at a
    # locally running instance. Browsers always label those, so refuse cross-site writes.
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if request.headers.get('sec-fetch-site') in ('cross-site', 'same-site') or \
                (origin and urlsplit(origin).netloc != request.headers.get('host')):
            return JSONResponse({'detail': 'Cross-site request blocked'}, status_code=403)
    response = await call_next(request)
    response.headers.update({
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                                   "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                                   "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })
    return response


_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limit(request: Request):
    """Sliding one-minute window per client for endpoints that create data or spend credits."""
    limit = int(os.getenv('SOCIALPILOT_RATE_LIMIT', '30'))
    key = request.client.host if request.client else 'unknown'
    t = time.monotonic()
    with _hits_lock:
        q = _hits[key]
        while q and t - q[0] > 60: q.popleft()
        if len(q) >= limit: raise HTTPException(429, 'Too many requests; try again in a minute.')
        q.append(t)


# ---------------------------------------------------------------- models
Platform = Literal['LinkedIn', 'Instagram']
Tone = Literal['Professional', 'Friendly', 'Persuasive', 'Educational', 'Storytelling', 'Witty']
Objective = Literal['Engagement', 'Brand awareness', 'Lead generation', 'Education', 'Product promotion']


def Str(max_length, min_length=1):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


class GenerateRequest(BaseModel):
    topic: Str(2000)
    brand: Str(120) = 'My Brand'
    platform: Platform = 'LinkedIn'
    tone: Tone = 'Professional'
    objective: Objective = 'Engagement'
    variations: int = Field(default=3, ge=1, le=5)

class DraftCreate(BaseModel):
    title: Str(250)
    content: Str(10000)
    platform: Platform = 'LinkedIn'
    topic: Str(2000, min_length=0) = ''

class DraftUpdate(BaseModel):
    title: Str(250) | None = None
    content: Str(10000) | None = None
    platform: Platform | None = None
    topic: Str(2000, min_length=0) | None = None

class RejectRequest(BaseModel):
    reason: Str(1000, min_length=0) = ''

class ScheduleRequest(BaseModel):
    draft_id: int = Field(ge=1)  # only approved drafts can be scheduled; content comes from the draft
    scheduled_at: Str(64)

class AutomationCreate(BaseModel):
    name: Str(120)
    trigger_type: Literal['manual'] = 'manual'  # only manual execution is implemented
    action_type: Literal['create_draft'] = 'create_draft'  # only draft creation is implemented
    topic_template: Str(2000)
    platform: Platform = 'LinkedIn'

class AutomationUpdate(BaseModel):
    enabled: bool | None = None
    name: Str(120) | None = None
    topic_template: Str(2000) | None = None
    platform: Platform | None = None


# ---------------------------------------------------------------- pages / health
@app.get('/')
def home(): return FileResponse(str(BASE_DIR / 'static' / 'index.html'))


@app.get('/api/health')
def health(): return {'status': 'ok', 'mode': 'demo-mock' if mock_mode() else 'ai-enabled', 'database': 'sqlite'}


# ---------------------------------------------------------------- credits
def balance_of(c): return c.execute('SELECT COALESCE(SUM(amount),0) FROM credit_ledger').fetchone()[0]


def charge(c, cost, reason):
    """Deduct credits inside the caller's write transaction; returns the ledger row id."""
    balance = balance_of(c)
    if balance < cost: raise HTTPException(402, f'Not enough demo credits. Need {cost}, have {balance}.')
    return c.execute('INSERT INTO credit_ledger(amount,reason,created_at) VALUES(?,?,?)', (-cost, reason, now())).lastrowid


@app.get('/api/credits')
def credits():
    with db() as c:
        used = c.execute('SELECT COALESCE(SUM(-amount),0) FROM credit_ledger WHERE amount<0').fetchone()[0]
        last = c.execute('SELECT -amount AS cost FROM credit_ledger WHERE amount<0 ORDER BY id DESC LIMIT 1').fetchone()
        return {'balance': balance_of(c), 'used': used, 'lastCost': last['cost'] if last else 0,
                'transactions': [dict(x) for x in c.execute('SELECT * FROM credit_ledger ORDER BY id DESC LIMIT 30')]}


@app.post('/api/credits/reset')
def reset_credits():
    # Real provider calls cost money, so free top-ups only exist in demo mode.
    if not mock_mode(): raise HTTPException(403, 'Credit reset is only available in demo mode')
    with db(write=True) as c:
        # Append an adjustment instead of deleting history, so the ledger and analytics stay consistent.
        delta = DEMO_CREDITS - balance_of(c)
        if delta: c.execute('INSERT INTO credit_ledger(amount,reason,created_at) VALUES(?,?,?)', (delta, 'Demo credit reset', now()))
    return credits()


# ---------------------------------------------------------------- generation
def mock_content(req: GenerateRequest):
    hooks = [f"A practical look at {req.topic}.", f"What if we approached {req.topic} differently?",
             f"Here's a lesson worth sharing about {req.topic}.", f"Small steps can make a difference with {req.topic}.",
             f"Let's rethink {req.topic}."]
    tags = ' '.join('#' + w for w in f'{req.topic} {req.brand}'.split() if w.isalnum())
    variations = []
    for i in range(req.variations):
        body = (f"{hooks[i % len(hooks)]}\n\nAt {req.brand}, we believe useful ideas should be clear, practical, and easy to apply. "
                "Focus on one meaningful takeaway, put it into action, and learn from the results.\n\nWhat has your experience been?")
        variations.append(f"{i+1}. Caption:\n{body}\n\nHashtags: {tags} #{req.platform} #ContentStrategy\nCTA: Share your perspective in the comments.")
    return '\n\n'.join(variations)


async def call_provider(req: GenerateRequest) -> str:
    prompt = (f"Create {req.variations} distinct social media post variations for brand {req.brand}. Platform: {req.platform}. "
              f"Topic: {req.topic}. Tone: {req.tone}. Objective: {req.objective}. Respect platform conventions. "
              "Return each variation with a caption, 3-8 relevant hashtags, and a clear CTA. Plain text, numbered, no preamble.")
    try:
        async with anthropic.AsyncAnthropic(api_key=os.environ['ANTHROPIC_API_KEY'], timeout=60, max_retries=1) as client:
            message = await client.messages.create(model=os.getenv('ANTHROPIC_MODEL') or DEFAULT_MODEL, max_tokens=16000,
                                                   messages=[{'role': 'user', 'content': prompt}])
    except anthropic.APITimeoutError: raise HTTPException(504, 'AI provider timed out.')
    except anthropic.APIConnectionError: raise HTTPException(502, 'Could not connect to the AI provider.')
    except anthropic.APIStatusError as e:
        # Log status and request id only; the client gets a generic 502 rather than the provider's status.
        logger.error('AI provider error status=%s request_id=%s', e.status_code, e.request_id)
        raise HTTPException(502, 'AI provider request failed; check server logs.')
    if message.stop_reason == 'refusal': raise HTTPException(502, 'The AI provider declined this request.')
    content = '\n'.join(b.text for b in message.content if b.type == 'text').strip()
    if not content: raise HTTPException(502, 'AI provider returned empty content.')
    return content


def log_run(c, req, cost):
    c.execute('INSERT INTO workflow_runs(topic,platform,variations,credits,status,created_at) VALUES(?,?,?,?,?,?)',
              (req.topic, req.platform, req.variations, cost, 'review', now()))


@app.post('/api/generate', dependencies=[Depends(rate_limit)])
async def generate(req: GenerateRequest):
    cost = 1 + req.variations
    label = f'{req.topic[:80]} ({req.variations} variations)'
    if mock_mode():
        content = mock_content(req)
        with db(write=True) as c:
            charge(c, cost, f'Mock generation: {label}')
            log_run(c, req, cost)
        return {'content': content, 'mock': True, 'credits_used': cost}
    # Reserve credits before the paid call so an empty balance never reaches the provider;
    # the reservation is released if the call fails.
    with db(write=True) as c:
        reservation = charge(c, cost, f'AI generation: {label}')
    try:
        content = await call_provider(req)
    except BaseException:
        with db(write=True) as c: c.execute('DELETE FROM credit_ledger WHERE id=?', (reservation,))
        raise
    with db(write=True) as c: log_run(c, req, cost)
    return {'content': content, 'mock': False, 'credits_used': cost}


@app.get('/api/workflows')
def workflows():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM workflow_runs ORDER BY id DESC LIMIT 100')]


# ---------------------------------------------------------------- drafts + approval
def get_draft(c, draft_id):
    row = c.execute('SELECT * FROM drafts WHERE id=?', (draft_id,)).fetchone()
    if not row: raise HTTPException(404, 'Draft not found')
    return row


@app.get('/api/drafts')
def list_drafts():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM drafts ORDER BY updated_at DESC')]


@app.post('/api/drafts')
def create_draft(d: DraftCreate):
    t = now()
    with db(write=True) as c:
        cur = c.execute('INSERT INTO drafts(title,content,platform,topic,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (d.title, d.content, d.platform, d.topic, 'draft', t, t))
        return dict(get_draft(c, cur.lastrowid))


@app.put('/api/drafts/{draft_id}')
def update_draft(draft_id: int, d: DraftUpdate):
    with db(write=True) as c:
        old = get_draft(c, draft_id)
        if old['status'] in ('pending_approval', 'scheduled'): raise HTTPException(409, 'Draft cannot be edited while pending approval or scheduled')
        vals = {k: v for k, v in d.model_dump().items() if v is not None and v != old[k]}
        if not vals: return dict(old)  # no-op edits keep the current approval
        if old['status'] in ('approved', 'rejected'): vals.update(status='draft', rejection_reason='')  # edits require re-approval
        vals['updated_at'] = now()
        c.execute('UPDATE drafts SET ' + ','.join(f'{k}=?' for k in vals) + ' WHERE id=?', (*vals.values(), draft_id))
        return dict(get_draft(c, draft_id))


@app.delete('/api/drafts/{draft_id}')
def delete_draft(draft_id: int):
    with db(write=True) as c:
        if get_draft(c, draft_id)['status'] == 'scheduled': raise HTTPException(409, 'Cancel the scheduled post before deleting this draft')
        c.execute('DELETE FROM drafts WHERE id=?', (draft_id,))
    return {'deleted': True}


def transition(draft_id, allowed_from, error, **fields):
    with db(write=True) as c:
        if get_draft(c, draft_id)['status'] not in allowed_from: raise HTTPException(409, error)
        fields['updated_at'] = now()
        c.execute('UPDATE drafts SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', (*fields.values(), draft_id))
        return dict(get_draft(c, draft_id))


@app.post('/api/drafts/{draft_id}/submit')
def submit_draft(draft_id: int):
    return transition(draft_id, ('draft', 'rejected'), 'Draft cannot be submitted from its current state', status='pending_approval', rejection_reason='')


@app.get('/api/approvals')
def approvals():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM drafts WHERE status IN ('pending_approval','approved','rejected') ORDER BY updated_at DESC")]


@app.post('/api/drafts/{draft_id}/approve')
def approve_draft(draft_id: int):
    return transition(draft_id, ('pending_approval',), 'Only pending drafts can be approved', status='approved')


@app.post('/api/drafts/{draft_id}/reject')
def reject_draft(draft_id: int, r: RejectRequest):
    return transition(draft_id, ('pending_approval',), 'Only pending drafts can be rejected', status='rejected', rejection_reason=r.reason)


# ---------------------------------------------------------------- scheduling
def parse_schedule_time(value):
    try: when = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError: raise HTTPException(400, 'Invalid schedule date/time')
    if when.tzinfo is None: raise HTTPException(400, 'Schedule time must include a timezone offset, e.g. 2026-12-01T09:00:00Z')
    when = when.astimezone(timezone.utc)
    if when <= datetime.now(timezone.utc): raise HTTPException(400, 'Schedule time must be in the future')
    return when


@app.post('/api/schedule')
def schedule_post(p: ScheduleRequest):
    when = parse_schedule_time(p.scheduled_at)
    with db(write=True) as c:
        draft = get_draft(c, p.draft_id)
        if draft['status'] != 'approved': raise HTTPException(409, 'Only approved drafts can be scheduled')
        # Everything published comes from the stored, approved draft — never from the client payload.
        cur = c.execute('INSERT INTO scheduled_posts(draft_id,title,content,platform,scheduled_at,status,created_at) VALUES(?,?,?,?,?,?,?)',
                        (draft['id'], draft['title'], draft['content'], draft['platform'], when.isoformat(), 'scheduled', now()))
        c.execute("UPDATE drafts SET status='scheduled',updated_at=? WHERE id=?", (now(), draft['id']))
        return dict(c.execute('SELECT * FROM scheduled_posts WHERE id=?', (cur.lastrowid,)).fetchone())


@app.get('/api/schedule')
def list_scheduled():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM scheduled_posts WHERE status='scheduled' ORDER BY scheduled_at")]


@app.delete('/api/schedule/{post_id}')
def cancel_schedule(post_id: int):
    with db(write=True) as c:
        row = c.execute('SELECT * FROM scheduled_posts WHERE id=?', (post_id,)).fetchone()
        if not row: raise HTTPException(404, 'Scheduled post not found')
        if row['status'] != 'scheduled': raise HTTPException(409, 'Scheduled post is not active')
        c.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=?", (post_id,))
        if row['draft_id']: c.execute("UPDATE drafts SET status='approved',updated_at=? WHERE id=? AND status='scheduled'", (now(), row['draft_id']))
    return {'cancelled': True}


# ---------------------------------------------------------------- automation (manual execution only; no external publishing)
def get_rule(c, rule_id):
    row = c.execute('SELECT * FROM automation_rules WHERE id=?', (rule_id,)).fetchone()
    if not row: raise HTTPException(404, 'Automation not found')
    return row


def ensure_unique_name(c, name, exclude_id=None):
    if c.execute('SELECT 1 FROM automation_rules WHERE lower(name)=lower(?) AND id IS NOT ?', (name, exclude_id)).fetchone():
        raise HTTPException(409, 'An automation rule with this name already exists')


@app.get('/api/automations')
def list_automations():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM automation_rules ORDER BY id DESC')]


@app.post('/api/automations')
def create_automation(a: AutomationCreate):
    t = now()
    with db(write=True) as c:
        ensure_unique_name(c, a.name)
        cur = c.execute('INSERT INTO automation_rules(name,trigger_type,action_type,topic_template,platform,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (a.name, a.trigger_type, a.action_type, a.topic_template, a.platform, 1, t, t))
        return dict(get_rule(c, cur.lastrowid))


@app.put('/api/automations/{rule_id}')
def update_automation(rule_id: int, a: AutomationUpdate):
    with db(write=True) as c:
        get_rule(c, rule_id)
        vals = {k: v for k, v in a.model_dump().items() if v is not None}
        if 'name' in vals: ensure_unique_name(c, vals['name'], exclude_id=rule_id)
        if 'enabled' in vals: vals['enabled'] = int(vals['enabled'])
        if vals:
            vals['updated_at'] = now()
            c.execute('UPDATE automation_rules SET ' + ','.join(k + '=?' for k in vals) + ' WHERE id=?', (*vals.values(), rule_id))
        return dict(get_rule(c, rule_id))


@app.delete('/api/automations/{rule_id}')
def delete_automation(rule_id: int):
    with db(write=True) as c:
        get_rule(c, rule_id)
        c.execute('DELETE FROM automation_rules WHERE id=?', (rule_id,))
    return {'deleted': True}


@app.post('/api/automations/{rule_id}/run', dependencies=[Depends(rate_limit)])
def run_automation(rule_id: int):
    with db(write=True) as c:
        rule = get_rule(c, rule_id)
        if not rule['enabled']: raise HTTPException(409, 'Automation is disabled')
        # No AI provider calls: execution creates a clearly marked draft that still needs approval.
        topic = rule['topic_template']
        content = f"[AUTOMATION MOCK — review required]\n\n{topic}\n\nDraft created by automation rule: {rule['name']}. Edit before publishing."
        t = now()
        cur = c.execute('INSERT INTO drafts(title,content,platform,topic,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (f"Automation: {rule['name']} · {t[:16].replace('T', ' ')} UTC", content, rule['platform'], topic, 'draft', t, t))
        details = f"Created draft #{cur.lastrowid}; provider calls disabled."
        c.execute('INSERT INTO automation_executions(rule_id,rule_name,status,details,created_at) VALUES(?,?,?,?,?)', (rule_id, rule['name'], 'success', details, t))
        return {'status': 'success', 'draft_id': cur.lastrowid, 'details': details}


@app.get('/api/automation-executions')
def automation_executions():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM automation_executions ORDER BY id DESC LIMIT 100')]


# ---------------------------------------------------------------- integrations (deliberately disconnected and credential-free)
@app.get('/api/integrations')
def integrations():
    providers = [('n8n', 'Automation webhooks', 'Webhook integration scaffold; configure endpoint outside this demo.'),
                 ('LinkedIn', 'Social publishing', 'OAuth publishing is not implemented.'),
                 ('Instagram', 'Social publishing', 'OAuth publishing is not implemented.'),
                 ('Analytics provider', 'Performance analytics', 'No external analytics data connected.')]
    return [{'provider': p, 'category': cat, 'status': 'not_connected', 'notes': notes} for p, cat, notes in providers]


# ---------------------------------------------------------------- analytics (local demo records only)
@app.get('/api/analytics')
def analytics():
    with db() as c:
        count = lambda sql: c.execute(sql).fetchone()[0]
        return {
            'generation_runs': count('SELECT COUNT(*) FROM workflow_runs'),
            'credits_consumed': count('SELECT COALESCE(SUM(credits),0) FROM workflow_runs'),
            'drafts_by_status': [dict(r) for r in c.execute('SELECT status,COUNT(*) AS count FROM drafts GROUP BY status')],
            'scheduled_posts': count("SELECT COUNT(*) FROM scheduled_posts WHERE status='scheduled'"),
            'published_posts': count("SELECT COUNT(*) FROM scheduled_posts WHERE status='published'"),
            'automation_rules': count('SELECT COUNT(*) FROM automation_rules'),
            'automation_executions': count('SELECT COUNT(*) FROM automation_executions'),
            'drafts_by_platform': [dict(r) for r in c.execute('SELECT platform,COUNT(*) AS count FROM drafts GROUP BY platform')],
            'recent_generations': [dict(r) for r in c.execute('SELECT topic,platform,variations,credits,status,created_at FROM workflow_runs ORDER BY id DESC LIMIT 10')],
            'mode': 'local demo data',
        }
