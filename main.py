"""SocialPilot AI — FastAPI backend (local demo).

Mock generation is the default; the Anthropic API is only called when
SOCIALPILOT_MOCK_MODE=0 and ANTHROPIC_API_KEY is set.

Every record belongs to a business account. People sign in, can belong to several
businesses with a role in each (owner / approver / editor), and only ever see the
data of the business their session is switched to.

Publishing goes through connected channels. Demo channels validate and record a post
locally; nothing is sent to LinkedIn or Instagram until a live publisher is added.
"""
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, NamedTuple
from urllib.parse import urlsplit

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
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
if os.getenv('RENDER_EXTERNAL_HOSTNAME'): ALLOWED_HOSTS.append(os.environ['RENDER_EXTERNAL_HOSTNAME'])  # set by Render for the public URL
SESSION_COOKIE = 'sp_session'
SESSION_DAYS = 7


# ---------------------------------------------------------------- background publisher
_stop_publisher = threading.Event()


def publish_interval(): return float(os.getenv('SOCIALPILOT_PUBLISH_INTERVAL', '30'))


def publisher_loop():
    while not _stop_publisher.wait(publish_interval()):
        try: publish_due()
        except Exception: logger.exception('Background publishing failed')


@asynccontextmanager
async def lifespan(app):
    # Publishes scheduled posts when they fall due. SOCIALPILOT_PUBLISH_INTERVAL=0 turns it off.
    if publish_interval() > 0:
        _stop_publisher.clear()
        threading.Thread(target=publisher_loop, name='publisher', daemon=True).start()
    yield
    _stop_publisher.set()


app = FastAPI(title='SocialPilot AI', lifespan=lifespan)
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


# Tables whose rows belong to one business account.
SCOPED_TABLES = ('drafts', 'scheduled_posts', 'workflow_runs', 'credit_ledger', 'automation_rules', 'automation_executions')
ADDED_COLUMNS = {**{t: [('business_id', 'INTEGER')] for t in SCOPED_TABLES},
                 'scheduled_posts': [('business_id', 'INTEGER'), ('connection_id', 'INTEGER'), ('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('last_error', "TEXT NOT NULL DEFAULT ''"), ('external_id', "TEXT NOT NULL DEFAULT ''"), ('published_at', 'TEXT')]}


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
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS businesses (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memberships (user_id INTEGER NOT NULL, business_id INTEGER NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (user_id, business_id));
        CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, business_id INTEGER NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS social_connections (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id INTEGER NOT NULL, platform TEXT NOT NULL, account_name TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'demo', status TEXT NOT NULL DEFAULT 'connected', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        ''')
        # Upgrade databases created before accounts and publishing existed.
        for table, columns in ADDED_COLUMNS.items():
            existing = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            for name, decl in columns:
                if name not in existing: conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
        conn.execute('DROP INDEX IF EXISTS ux_automation_name')  # names are now unique per business
        # Database-level guarantees; skipped with a warning if legacy data already violates them.
        for sql in ("CREATE UNIQUE INDEX IF NOT EXISTS ux_automation_name_biz ON automation_rules(business_id, name COLLATE NOCASE)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_active_schedule ON scheduled_posts(draft_id) WHERE status='scheduled'",
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_active_connection ON social_connections(business_id, platform) WHERE status='connected'"):
            try: conn.execute(sql)
            except sqlite3.IntegrityError: logger.warning('Could not create index (existing duplicates): %s', sql)
        for table in SCOPED_TABLES: conn.execute(f'CREATE INDEX IF NOT EXISTS ix_{table}_business ON {table}(business_id)')
        # A post that was mid-publish when the server stopped is surfaced as failed, never silently re-sent.
        conn.execute("UPDATE scheduled_posts SET status='failed', last_error='Interrupted while publishing; retry to publish again' WHERE status='publishing'")
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
    # CSRF: the session cookie is SameSite=Strict, and as a second layer cross-site writes are
    # refused outright (browsers always label them with Sec-Fetch-Site / Origin).
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
    """Sliding one-minute window per client for endpoints that create data, spend credits or check passwords."""
    limit = int(os.getenv('SOCIALPILOT_RATE_LIMIT', '30'))
    key = request.client.host if request.client else 'unknown'
    t = time.monotonic()
    with _hits_lock:
        q = _hits[key]
        while q and t - q[0] > 60: q.popleft()
        if len(q) >= limit: raise HTTPException(429, 'Too many requests; try again in a minute.')
        q.append(t)


# ---------------------------------------------------------------- accounts + sessions
def hash_password(password):
    salt = secrets.token_bytes(16)
    return 'scrypt$' + salt.hex() + '$' + hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def verify_password(password, stored):
    try: _, salt, digest = stored.split('$')
    except ValueError: return False
    return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), digest)


DUMMY_HASH = hash_password(secrets.token_hex(8))  # unknown emails cost the same time as wrong passwords


def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()


class Ctx(NamedTuple):
    user_id: int
    business_id: int
    role: str


def current(request: Request) -> Ctx:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db() as c:
            row = c.execute('SELECT s.user_id, s.business_id, m.role FROM sessions s JOIN memberships m '
                            'ON m.user_id=s.user_id AND m.business_id=s.business_id WHERE s.token_hash=? AND s.expires_at>?',
                            (token_hash(token), now())).fetchone()
        if row: return Ctx(*row)
    raise HTTPException(401, 'Sign in required')


def role(*allowed):
    def check(ctx: Ctx = Depends(current)):
        if ctx.role not in allowed: raise HTTPException(403, f'This action needs the {" or ".join(allowed)} role')
        return ctx
    return check


Member = Annotated[Ctx, Depends(current)]
Approver = Annotated[Ctx, Depends(role('owner', 'approver'))]
Owner = Annotated[Ctx, Depends(role('owner'))]


def start_session(c, response: Response, user_id, business_id):
    token = secrets.token_urlsafe(32)
    c.execute('DELETE FROM sessions WHERE expires_at<=?', (now(),))
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    c.execute('INSERT INTO sessions(token_hash,user_id,business_id,expires_at,created_at) VALUES(?,?,?,?,?)',
              (token_hash(token), user_id, business_id, expires.isoformat(), now()))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite='strict', path='/',
                        secure=os.getenv('SOCIALPILOT_SECURE_COOKIES') == '1')


def create_business(c, user_id, name, adopt_legacy=False):
    t = now()
    biz = c.execute('INSERT INTO businesses(name,created_at) VALUES(?,?)', (name, t)).lastrowid
    c.execute('INSERT INTO memberships(user_id,business_id,role,created_at) VALUES(?,?,?,?)', (user_id, biz, 'owner', t))
    if adopt_legacy:  # data created before accounts existed belongs to the first account
        for table in SCOPED_TABLES: c.execute(f'UPDATE {table} SET business_id=? WHERE business_id IS NULL', (biz,))
    if not c.execute('SELECT 1 FROM credit_ledger WHERE business_id=?', (biz,)).fetchone():
        c.execute('INSERT INTO credit_ledger(business_id,amount,reason,created_at) VALUES(?,?,?,?)', (biz, DEMO_CREDITS, 'Initial demo credit balance', t))
    return biz


def profile(user_id, business_id):
    with db() as c:
        user = c.execute('SELECT id,name,email FROM users WHERE id=?', (user_id,)).fetchone()
        businesses = [dict(r) for r in c.execute('SELECT b.id,b.name,m.role FROM memberships m JOIN businesses b ON b.id=m.business_id '
                                                 'WHERE m.user_id=? ORDER BY b.name COLLATE NOCASE, b.id', (user_id,))]
    return {'user': dict(user), 'business': next(b for b in businesses if b['id'] == business_id), 'businesses': businesses}


# ---------------------------------------------------------------- models
Platform = Literal['LinkedIn', 'Instagram']
Tone = Literal['Professional', 'Friendly', 'Persuasive', 'Educational', 'Storytelling', 'Witty']
Objective = Literal['Engagement', 'Brand awareness', 'Lead generation', 'Education', 'Product promotion']
Role = Literal['owner', 'approver', 'editor']


def Str(max_length, min_length=1):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')]


class RegisterRequest(BaseModel):
    name: Str(120)
    email: Email
    password: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    business_name: Str(120)

class LoginRequest(BaseModel):
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]

class BusinessCreate(BaseModel):
    name: Str(120)

class BusinessSwitch(BaseModel):
    business_id: int = Field(ge=1)

class MemberAdd(BaseModel):
    email: Email
    role: Role = 'editor'

class MemberUpdate(BaseModel):
    role: Role

class ConnectionCreate(BaseModel):
    platform: Platform
    account_name: Str(120)

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


# ---------------------------------------------------------------- auth endpoints
@app.post('/api/auth/register', dependencies=[Depends(rate_limit)])
def register(r: RegisterRequest, response: Response):
    password_hash = hash_password(r.password)  # outside the write lock: scrypt is deliberately slow
    with db(write=True) as c:
        if c.execute('SELECT 1 FROM users WHERE email=?', (r.email,)).fetchone(): raise HTTPException(409, 'An account with this email already exists')
        first_user = c.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
        user_id = c.execute('INSERT INTO users(email,name,password_hash,created_at) VALUES(?,?,?,?)', (r.email, r.name, password_hash, now())).lastrowid
        biz = create_business(c, user_id, r.business_name, adopt_legacy=first_user)
        start_session(c, response, user_id, biz)
    return profile(user_id, biz)


@app.post('/api/auth/login', dependencies=[Depends(rate_limit)])
def login(r: LoginRequest, response: Response):
    with db() as c: user = c.execute('SELECT id,password_hash FROM users WHERE email=?', (r.email,)).fetchone()
    if not verify_password(r.password, user['password_hash'] if user else DUMMY_HASH) or not user:
        raise HTTPException(401, 'Invalid email or password')
    with db(write=True) as c:
        first = c.execute('SELECT business_id FROM memberships WHERE user_id=? ORDER BY created_at, business_id LIMIT 1', (user['id'],)).fetchone()
        if not first: raise HTTPException(403, 'This account is not a member of any business')
        start_session(c, response, user['id'], first['business_id'])
    return profile(user['id'], first['business_id'])


@app.post('/api/auth/logout')
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db(write=True) as c: c.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(token),))
    response.delete_cookie(SESSION_COOKIE, path='/')
    return {'signed_out': True}


@app.get('/api/me')
def me(ctx: Member): return profile(ctx.user_id, ctx.business_id)


# ---------------------------------------------------------------- business accounts + team
def switch_session(c, request, business_id):
    c.execute('UPDATE sessions SET business_id=? WHERE token_hash=?', (business_id, token_hash(request.cookies[SESSION_COOKIE])))


@app.post('/api/businesses')
def create_business_endpoint(b: BusinessCreate, ctx: Member, request: Request):
    with db(write=True) as c:
        biz = create_business(c, ctx.user_id, b.name)
        switch_session(c, request, biz)
    return profile(ctx.user_id, biz)


@app.post('/api/session/business')
def switch_business(s: BusinessSwitch, ctx: Member, request: Request):
    with db(write=True) as c:
        if not c.execute('SELECT 1 FROM memberships WHERE user_id=? AND business_id=?', (ctx.user_id, s.business_id)).fetchone():
            raise HTTPException(404, 'Business not found')
        switch_session(c, request, s.business_id)
    return profile(ctx.user_id, s.business_id)


def owner_count(c, business_id): return c.execute("SELECT COUNT(*) FROM memberships WHERE business_id=? AND role='owner'", (business_id,)).fetchone()[0]


def get_member(c, business_id, user_id):
    row = c.execute('SELECT u.id AS user_id,u.name,u.email,m.role,m.created_at FROM memberships m JOIN users u ON u.id=m.user_id '
                    'WHERE m.business_id=? AND m.user_id=?', (business_id, user_id)).fetchone()
    if not row: raise HTTPException(404, 'Member not found')
    return row


@app.get('/api/members')
def list_members(ctx: Member):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT u.id AS user_id,u.name,u.email,m.role,m.created_at FROM memberships m JOIN users u ON u.id=m.user_id '
                                           'WHERE m.business_id=? ORDER BY m.created_at, u.id', (ctx.business_id,))]


@app.post('/api/members')
def add_member(m: MemberAdd, ctx: Owner):
    with db(write=True) as c:
        user = c.execute('SELECT id FROM users WHERE email=?', (m.email,)).fetchone()
        if not user: raise HTTPException(404, 'No account uses this email yet; ask them to sign up first')
        if c.execute('SELECT 1 FROM memberships WHERE user_id=? AND business_id=?', (user['id'], ctx.business_id)).fetchone():
            raise HTTPException(409, 'This person is already a member')
        c.execute('INSERT INTO memberships(user_id,business_id,role,created_at) VALUES(?,?,?,?)', (user['id'], ctx.business_id, m.role, now()))
        return dict(get_member(c, ctx.business_id, user['id']))


@app.put('/api/members/{user_id}')
def update_member(user_id: int, m: MemberUpdate, ctx: Owner):
    with db(write=True) as c:
        old = get_member(c, ctx.business_id, user_id)
        if old['role'] == 'owner' and m.role != 'owner' and owner_count(c, ctx.business_id) == 1:
            raise HTTPException(409, 'A business needs at least one owner')
        c.execute('UPDATE memberships SET role=? WHERE user_id=? AND business_id=?', (m.role, user_id, ctx.business_id))
        return dict(get_member(c, ctx.business_id, user_id))


@app.delete('/api/members/{user_id}')
def remove_member(user_id: int, ctx: Owner):
    with db(write=True) as c:
        if get_member(c, ctx.business_id, user_id)['role'] == 'owner' and owner_count(c, ctx.business_id) == 1:
            raise HTTPException(409, 'A business needs at least one owner')
        c.execute('DELETE FROM memberships WHERE user_id=? AND business_id=?', (user_id, ctx.business_id))
        c.execute('DELETE FROM sessions WHERE user_id=? AND business_id=?', (user_id, ctx.business_id))
    return {'removed': True}


# ---------------------------------------------------------------- credits
def balance_of(c, business_id): return c.execute('SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE business_id=?', (business_id,)).fetchone()[0]


def charge(c, business_id, cost, reason):
    """Deduct credits inside the caller's write transaction; returns the ledger row id."""
    balance = balance_of(c, business_id)
    if balance < cost: raise HTTPException(402, f'Not enough demo credits. Need {cost}, have {balance}.')
    return c.execute('INSERT INTO credit_ledger(business_id,amount,reason,created_at) VALUES(?,?,?,?)', (business_id, -cost, reason, now())).lastrowid


@app.get('/api/credits')
def credits(ctx: Member):
    biz = ctx.business_id
    with db() as c:
        used = c.execute('SELECT COALESCE(SUM(-amount),0) FROM credit_ledger WHERE amount<0 AND business_id=?', (biz,)).fetchone()[0]
        last = c.execute('SELECT -amount AS cost FROM credit_ledger WHERE amount<0 AND business_id=? ORDER BY id DESC LIMIT 1', (biz,)).fetchone()
        return {'balance': balance_of(c, biz), 'used': used, 'lastCost': last['cost'] if last else 0,
                'transactions': [dict(x) for x in c.execute('SELECT * FROM credit_ledger WHERE business_id=? ORDER BY id DESC LIMIT 30', (biz,))]}


@app.post('/api/credits/reset')
def reset_credits(ctx: Owner):
    # Real provider calls cost money, so free top-ups only exist in demo mode.
    if not mock_mode(): raise HTTPException(403, 'Credit reset is only available in demo mode')
    with db(write=True) as c:
        # Append an adjustment instead of deleting history, so the ledger and analytics stay consistent.
        delta = DEMO_CREDITS - balance_of(c, ctx.business_id)
        if delta: c.execute('INSERT INTO credit_ledger(business_id,amount,reason,created_at) VALUES(?,?,?,?)', (ctx.business_id, delta, 'Demo credit reset', now()))
    return credits(ctx)


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


def log_run(c, business_id, req, cost):
    c.execute('INSERT INTO workflow_runs(business_id,topic,platform,variations,credits,status,created_at) VALUES(?,?,?,?,?,?,?)',
              (business_id, req.topic, req.platform, req.variations, cost, 'review', now()))


@app.post('/api/generate', dependencies=[Depends(rate_limit)])
async def generate(req: GenerateRequest, ctx: Member):
    biz = ctx.business_id
    cost = 1 + req.variations
    label = f'{req.topic[:80]} ({req.variations} variations)'
    if mock_mode():
        content = mock_content(req)
        with db(write=True) as c:
            charge(c, biz, cost, f'Mock generation: {label}')
            log_run(c, biz, req, cost)
        return {'content': content, 'mock': True, 'credits_used': cost}
    # Reserve credits before the paid call so an empty balance never reaches the provider;
    # the reservation is released if the call fails.
    with db(write=True) as c:
        reservation = charge(c, biz, cost, f'AI generation: {label}')
    try:
        content = await call_provider(req)
    except BaseException:
        with db(write=True) as c: c.execute('DELETE FROM credit_ledger WHERE id=?', (reservation,))
        raise
    with db(write=True) as c: log_run(c, biz, req, cost)
    return {'content': content, 'mock': False, 'credits_used': cost}


@app.get('/api/workflows')
def workflows(ctx: Member):
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM workflow_runs WHERE business_id=? ORDER BY id DESC LIMIT 100', (ctx.business_id,))]


# ---------------------------------------------------------------- drafts + approval
def get_draft(c, business_id, draft_id):
    row = c.execute('SELECT * FROM drafts WHERE id=? AND business_id=?', (draft_id, business_id)).fetchone()
    if not row: raise HTTPException(404, 'Draft not found')
    return row


@app.get('/api/drafts')
def list_drafts(ctx: Member):
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM drafts WHERE business_id=? ORDER BY updated_at DESC', (ctx.business_id,))]


@app.post('/api/drafts')
def create_draft(d: DraftCreate, ctx: Member):
    t = now()
    with db(write=True) as c:
        cur = c.execute('INSERT INTO drafts(business_id,title,content,platform,topic,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (ctx.business_id, d.title, d.content, d.platform, d.topic, 'draft', t, t))
        return dict(get_draft(c, ctx.business_id, cur.lastrowid))


@app.put('/api/drafts/{draft_id}')
def update_draft(draft_id: int, d: DraftUpdate, ctx: Member):
    with db(write=True) as c:
        old = get_draft(c, ctx.business_id, draft_id)
        if old['status'] in ('pending_approval', 'scheduled', 'published'):
            raise HTTPException(409, 'Draft cannot be edited while pending approval, scheduled or published')
        vals = {k: v for k, v in d.model_dump().items() if v is not None and v != old[k]}
        if not vals: return dict(old)  # no-op edits keep the current approval
        if old['status'] in ('approved', 'rejected'): vals.update(status='draft', rejection_reason='')  # edits require re-approval
        vals['updated_at'] = now()
        c.execute('UPDATE drafts SET ' + ','.join(f'{k}=?' for k in vals) + ' WHERE id=?', (*vals.values(), draft_id))
        return dict(get_draft(c, ctx.business_id, draft_id))


@app.delete('/api/drafts/{draft_id}')
def delete_draft(draft_id: int, ctx: Member):
    with db(write=True) as c:
        if get_draft(c, ctx.business_id, draft_id)['status'] == 'scheduled': raise HTTPException(409, 'Cancel the scheduled post before deleting this draft')
        c.execute('DELETE FROM drafts WHERE id=?', (draft_id,))
    return {'deleted': True}


def transition(ctx, draft_id, allowed_from, error, **fields):
    with db(write=True) as c:
        if get_draft(c, ctx.business_id, draft_id)['status'] not in allowed_from: raise HTTPException(409, error)
        fields['updated_at'] = now()
        c.execute('UPDATE drafts SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', (*fields.values(), draft_id))
        return dict(get_draft(c, ctx.business_id, draft_id))


@app.post('/api/drafts/{draft_id}/submit')
def submit_draft(draft_id: int, ctx: Member):
    return transition(ctx, draft_id, ('draft', 'rejected'), 'Draft cannot be submitted from its current state', status='pending_approval', rejection_reason='')


@app.get('/api/approvals')
def approvals(ctx: Member):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM drafts WHERE business_id=? AND status IN ('pending_approval','approved','rejected') "
                                           "ORDER BY updated_at DESC", (ctx.business_id,))]


@app.post('/api/drafts/{draft_id}/approve')
def approve_draft(draft_id: int, ctx: Approver):
    return transition(ctx, draft_id, ('pending_approval',), 'Only pending drafts can be approved', status='approved')


@app.post('/api/drafts/{draft_id}/reject')
def reject_draft(draft_id: int, r: RejectRequest, ctx: Approver):
    return transition(ctx, draft_id, ('pending_approval',), 'Only pending drafts can be rejected', status='rejected', rejection_reason=r.reason)


# ---------------------------------------------------------------- scheduling
def parse_schedule_time(value):
    try: when = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError: raise HTTPException(400, 'Invalid schedule date/time')
    if when.tzinfo is None: raise HTTPException(400, 'Schedule time must include a timezone offset, e.g. 2026-12-01T09:00:00Z')
    when = when.astimezone(timezone.utc)
    if when <= datetime.now(timezone.utc): raise HTTPException(400, 'Schedule time must be in the future')
    return when


def get_post(c, business_id, post_id):
    row = c.execute('SELECT * FROM scheduled_posts WHERE id=? AND business_id=?', (post_id, business_id)).fetchone()
    if not row: raise HTTPException(404, 'Scheduled post not found')
    return row


@app.post('/api/schedule')
def schedule_post(p: ScheduleRequest, ctx: Member):
    when = parse_schedule_time(p.scheduled_at)
    with db(write=True) as c:
        draft = get_draft(c, ctx.business_id, p.draft_id)
        if draft['status'] != 'approved': raise HTTPException(409, 'Only approved drafts can be scheduled')
        # Everything published comes from the stored, approved draft — never from the client payload.
        cur = c.execute('INSERT INTO scheduled_posts(business_id,draft_id,title,content,platform,scheduled_at,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                        (ctx.business_id, draft['id'], draft['title'], draft['content'], draft['platform'], when.isoformat(), 'scheduled', now()))
        c.execute("UPDATE drafts SET status='scheduled',updated_at=? WHERE id=?", (now(), draft['id']))
        return dict(get_post(c, ctx.business_id, cur.lastrowid))


@app.get('/api/schedule')
def list_scheduled(ctx: Member):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM scheduled_posts WHERE business_id=? AND status='scheduled' ORDER BY scheduled_at", (ctx.business_id,))]


@app.delete('/api/schedule/{post_id}')
def cancel_schedule(post_id: int, ctx: Member):
    with db(write=True) as c:
        row = get_post(c, ctx.business_id, post_id)
        if row['status'] not in ('scheduled', 'failed'): raise HTTPException(409, 'Scheduled post is not active')
        c.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=?", (post_id,))
        if row['draft_id']: c.execute("UPDATE drafts SET status='approved',updated_at=? WHERE id=? AND status='scheduled'", (now(), row['draft_id']))
    return {'cancelled': True}


# ---------------------------------------------------------------- publishing
PLATFORM_LIMITS = {'LinkedIn': 3000, 'Instagram': 2200}  # caption lengths the platforms accept


class PublishError(Exception):
    """A publish attempt the platform (or demo publisher) refused; the message is shown to the user."""


def demo_publisher(post, connection):
    """Checks the post the way the platform would, then records a simulated post id. Nothing leaves this machine."""
    limit = PLATFORM_LIMITS[post['platform']]
    if len(post['content']) > limit:
        raise PublishError(f"{post['platform']} posts are limited to {limit} characters; this one has {len(post['content'])}")
    return f"demo-{post['platform'].lower()}-{post['id']}-{secrets.token_hex(4)}"


# Publisher per connection mode. A live LinkedIn or Instagram publisher (OAuth token + platform API call)
# is added here as another mode; until then only demo channels can be connected.
PUBLISHERS = {'demo': demo_publisher}


def publish(post_id, business_id):
    with db(write=True) as c:
        post = get_post(c, business_id, post_id)
        if post['status'] not in ('scheduled', 'failed'): raise HTTPException(409, 'Post is not waiting to be published')
        channel = c.execute("SELECT * FROM social_connections WHERE business_id=? AND platform=? AND status='connected'",
                            (business_id, post['platform'])).fetchone()
        # Claim the post first, so the publish button and the background publisher can never both post it.
        c.execute("UPDATE scheduled_posts SET status='publishing', attempts=attempts+1, connection_id=? WHERE id=?",
                  (channel['id'] if channel else None, post_id))
    error = external_id = None
    try:
        if not channel: raise PublishError(f"No {post['platform']} channel is connected for this business")
        publisher = PUBLISHERS.get(channel['mode'])
        if not publisher: raise PublishError(f"{channel['mode']} publishing is not available")
        external_id = publisher(dict(post), dict(channel))
    except PublishError as e:
        error = str(e)
    except Exception:
        logger.exception('Publishing post %s failed', post_id)
        error = 'Publisher error; check server logs'
    with db(write=True) as c:
        if error:
            c.execute("UPDATE scheduled_posts SET status='failed', last_error=? WHERE id=?", (error, post_id))
        else:
            c.execute("UPDATE scheduled_posts SET status='published', external_id=?, published_at=?, last_error='' WHERE id=?", (external_id, now(), post_id))
            if post['draft_id']: c.execute("UPDATE drafts SET status='published', updated_at=? WHERE id=? AND status='scheduled'", (now(), post['draft_id']))
        return dict(get_post(c, business_id, post_id))


def publish_due(business_id=None):
    """Publish every scheduled post whose time has come (one business, or all of them)."""
    with db() as c:
        sql = "SELECT id,business_id FROM scheduled_posts WHERE status='scheduled' AND business_id IS NOT NULL AND scheduled_at<=?"
        args = (now(),)
        if business_id: sql, args = sql + ' AND business_id=?', args + (business_id,)
        due = c.execute(sql + ' ORDER BY scheduled_at', args).fetchall()
    results = []
    for row in due:
        try: results.append(publish(row['id'], row['business_id']))
        except HTTPException: pass  # another publisher claimed or cancelled it first
    return results


@app.post('/api/schedule/{post_id}/publish', dependencies=[Depends(rate_limit)])
def publish_now(post_id: int, ctx: Member):
    return publish(post_id, ctx.business_id)


@app.post('/api/publishing/run-due', dependencies=[Depends(rate_limit)])
def run_due(ctx: Member):
    results = publish_due(ctx.business_id)
    return {'processed': len(results), 'published': sum(r['status'] == 'published' for r in results),
            'failed': sum(r['status'] == 'failed' for r in results), 'posts': results}


@app.get('/api/publishing')
def publishing_history(ctx: Member):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM scheduled_posts WHERE business_id=? AND status IN ('publishing','published','failed') "
                                           "ORDER BY COALESCE(published_at, scheduled_at) DESC, id DESC LIMIT 100", (ctx.business_id,))]


# ---------------------------------------------------------------- channels (social connections)
@app.get('/api/connections')
def list_connections(ctx: Member):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM social_connections WHERE business_id=? AND status='connected' ORDER BY platform", (ctx.business_id,))]


@app.post('/api/connections')
def connect_channel(conn: ConnectionCreate, ctx: Owner):
    t = now()
    with db(write=True) as c:
        if c.execute("SELECT 1 FROM social_connections WHERE business_id=? AND platform=? AND status='connected'", (ctx.business_id, conn.platform)).fetchone():
            raise HTTPException(409, f'A {conn.platform} channel is already connected')
        cur = c.execute('INSERT INTO social_connections(business_id,platform,account_name,mode,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (ctx.business_id, conn.platform, conn.account_name, 'demo', 'connected', t, t))
        return dict(c.execute('SELECT * FROM social_connections WHERE id=?', (cur.lastrowid,)).fetchone())


@app.delete('/api/connections/{connection_id}')
def disconnect_channel(connection_id: int, ctx: Owner):
    with db(write=True) as c:
        if not c.execute("SELECT 1 FROM social_connections WHERE id=? AND business_id=? AND status='connected'", (connection_id, ctx.business_id)).fetchone():
            raise HTTPException(404, 'Channel not found')
        c.execute("UPDATE social_connections SET status='disconnected', updated_at=? WHERE id=?", (now(), connection_id))
    return {'disconnected': True}


# ---------------------------------------------------------------- automation (manual execution only)
def get_rule(c, business_id, rule_id):
    row = c.execute('SELECT * FROM automation_rules WHERE id=? AND business_id=?', (rule_id, business_id)).fetchone()
    if not row: raise HTTPException(404, 'Automation not found')
    return row


def ensure_unique_name(c, business_id, name, exclude_id=None):
    if c.execute('SELECT 1 FROM automation_rules WHERE business_id=? AND lower(name)=lower(?) AND id IS NOT ?', (business_id, name, exclude_id)).fetchone():
        raise HTTPException(409, 'An automation rule with this name already exists')


@app.get('/api/automations')
def list_automations(ctx: Member):
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM automation_rules WHERE business_id=? ORDER BY id DESC', (ctx.business_id,))]


@app.post('/api/automations')
def create_automation(a: AutomationCreate, ctx: Member):
    t = now()
    with db(write=True) as c:
        ensure_unique_name(c, ctx.business_id, a.name)
        cur = c.execute('INSERT INTO automation_rules(business_id,name,trigger_type,action_type,topic_template,platform,enabled,created_at,updated_at) '
                        'VALUES(?,?,?,?,?,?,?,?,?)', (ctx.business_id, a.name, a.trigger_type, a.action_type, a.topic_template, a.platform, 1, t, t))
        return dict(get_rule(c, ctx.business_id, cur.lastrowid))


@app.put('/api/automations/{rule_id}')
def update_automation(rule_id: int, a: AutomationUpdate, ctx: Member):
    with db(write=True) as c:
        get_rule(c, ctx.business_id, rule_id)
        vals = {k: v for k, v in a.model_dump().items() if v is not None}
        if 'name' in vals: ensure_unique_name(c, ctx.business_id, vals['name'], exclude_id=rule_id)
        if 'enabled' in vals: vals['enabled'] = int(vals['enabled'])
        if vals:
            vals['updated_at'] = now()
            c.execute('UPDATE automation_rules SET ' + ','.join(k + '=?' for k in vals) + ' WHERE id=?', (*vals.values(), rule_id))
        return dict(get_rule(c, ctx.business_id, rule_id))


@app.delete('/api/automations/{rule_id}')
def delete_automation(rule_id: int, ctx: Member):
    with db(write=True) as c:
        get_rule(c, ctx.business_id, rule_id)
        c.execute('DELETE FROM automation_rules WHERE id=?', (rule_id,))
    return {'deleted': True}


@app.post('/api/automations/{rule_id}/run', dependencies=[Depends(rate_limit)])
def run_automation(rule_id: int, ctx: Member):
    biz = ctx.business_id
    with db(write=True) as c:
        rule = get_rule(c, biz, rule_id)
        if not rule['enabled']: raise HTTPException(409, 'Automation is disabled')
        # No AI provider calls: execution creates a clearly marked draft that still needs approval.
        topic = rule['topic_template']
        content = f"[AUTOMATION MOCK — review required]\n\n{topic}\n\nDraft created by automation rule: {rule['name']}. Edit before publishing."
        t = now()
        cur = c.execute('INSERT INTO drafts(business_id,title,content,platform,topic,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (biz, f"Automation: {rule['name']} · {t[:16].replace('T', ' ')} UTC", content, rule['platform'], topic, 'draft', t, t))
        details = f"Created draft #{cur.lastrowid}; provider calls disabled."
        c.execute('INSERT INTO automation_executions(business_id,rule_id,rule_name,status,details,created_at) VALUES(?,?,?,?,?,?)',
                  (biz, rule_id, rule['name'], 'success', details, t))
        return {'status': 'success', 'draft_id': cur.lastrowid, 'details': details}


@app.get('/api/automation-executions')
def automation_executions(ctx: Member):
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM automation_executions WHERE business_id=? ORDER BY id DESC LIMIT 100', (ctx.business_id,))]


# ---------------------------------------------------------------- integrations
@app.get('/api/integrations')
def integrations(ctx: Member):
    with db() as c:
        channels = {r['platform']: r for r in c.execute("SELECT * FROM social_connections WHERE business_id=? AND status='connected'", (ctx.business_id,))}
    items = [{'provider': 'n8n', 'category': 'Automation webhooks', 'status': 'not_connected',
              'notes': 'Webhook integration scaffold; configure endpoint outside this demo.'}]
    for platform in ('LinkedIn', 'Instagram'):
        ch = channels.get(platform)
        items.append({'provider': platform, 'category': 'Social publishing', 'status': 'demo_connected' if ch else 'not_connected',
                      'connection_id': ch['id'] if ch else None,
                      'notes': f"Demo channel “{ch['account_name']}”: posts are checked and recorded locally, nothing is sent to {platform}." if ch
                               else f'Connect a demo channel to publish. Live {platform} publishing needs a {platform} developer app.'})
    items.append({'provider': 'Analytics provider', 'category': 'Performance analytics', 'status': 'not_connected', 'notes': 'No external analytics data connected.'})
    return items


# ---------------------------------------------------------------- analytics (local records only)
@app.get('/api/analytics')
def analytics(ctx: Member):
    biz = ctx.business_id
    with db() as c:
        count = lambda sql: c.execute(sql, (biz,)).fetchone()[0]
        rows = lambda sql: [dict(r) for r in c.execute(sql, (biz,))]
        return {
            'generation_runs': count('SELECT COUNT(*) FROM workflow_runs WHERE business_id=?'),
            'credits_consumed': count('SELECT COALESCE(SUM(credits),0) FROM workflow_runs WHERE business_id=?'),
            'drafts_by_status': rows('SELECT status,COUNT(*) AS count FROM drafts WHERE business_id=? GROUP BY status'),
            'scheduled_posts': count("SELECT COUNT(*) FROM scheduled_posts WHERE business_id=? AND status='scheduled'"),
            'published_posts': count("SELECT COUNT(*) FROM scheduled_posts WHERE business_id=? AND status='published'"),
            'failed_posts': count("SELECT COUNT(*) FROM scheduled_posts WHERE business_id=? AND status='failed'"),
            'automation_rules': count('SELECT COUNT(*) FROM automation_rules WHERE business_id=?'),
            'automation_executions': count('SELECT COUNT(*) FROM automation_executions WHERE business_id=?'),
            'drafts_by_platform': rows('SELECT platform,COUNT(*) AS count FROM drafts WHERE business_id=? GROUP BY platform'),
            'published_by_platform': rows("SELECT platform,COUNT(*) AS count FROM scheduled_posts WHERE business_id=? AND status='published' GROUP BY platform"),
            'recent_generations': rows('SELECT topic,platform,variations,credits,status,created_at FROM workflow_runs WHERE business_id=? ORDER BY id DESC LIMIT 10'),
            'mode': 'local demo data',
        }


# ---------------------------------------------------------------- shared demo login
DEMO_POSTS = [  # (title, platform, status, content)
    ('5 ways small teams save hours with AI', 'LinkedIn', 'published',
     "Small teams don't need more tools, they need fewer repetitive tasks.\n\nHere are 5 places AI saves us hours every week: "
     "first drafts, repurposing posts, scheduling, reporting and approvals.\n\nWhich one would help your team most?\n\n#AI #Productivity #SmallBusiness"),
    ('Behind the scenes: our content week', 'Instagram', 'scheduled',
     "Monday planning, Wednesday creating, Friday reviewing. ☕\n\nThis is how we plan a full week of content in one hour.\n\n#ContentPlanning #BehindTheScenes"),
    ('Customer story: from idea to post in 10 minutes', 'LinkedIn', 'pending_approval',
     "One of our customers used to spend a full afternoon on a single post.\n\nNow the brief goes in, three variations come out, "
     "and the team approves the best one before lunch.\n\n#CaseStudy #Marketing"),
    ('Weekly tip: repurpose one post three ways', 'Instagram', 'draft',
     "[AUTOMATION MOCK — review required]\n\nTurn one long post into a carousel, a short caption and a quote card.\n\n#ContentTips"),
]


def seed_demo_account():
    """Recreates the shared demo login on every start, because a free host's disk is wiped on restart.
    Only runs when SOCIALPILOT_DEMO_EMAIL and SOCIALPILOT_DEMO_PASSWORD are set on the server."""
    email = os.getenv('SOCIALPILOT_DEMO_EMAIL', '').strip().lower()
    password = os.getenv('SOCIALPILOT_DEMO_PASSWORD', '')
    if not email or not password: return
    with db() as c:
        if c.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone(): return
    password_hash = hash_password(password)
    t = datetime.now(timezone.utc)
    with db(write=True) as c:
        if c.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone(): return
        user_id = c.execute('INSERT INTO users(email,name,password_hash,created_at) VALUES(?,?,?,?)', (email, 'Demo User', password_hash, now())).lastrowid
        biz = create_business(c, user_id, os.getenv('SOCIALPILOT_DEMO_BUSINESS') or 'SocialPilot Demo')
        channels = {}
        for platform, account in (('LinkedIn', 'SocialPilot Demo Page'), ('Instagram', '@socialpilot.demo')):
            channels[platform] = c.execute('INSERT INTO social_connections(business_id,platform,account_name,mode,status,created_at,updated_at) '
                                           'VALUES(?,?,?,?,?,?,?)', (biz, platform, account, 'demo', 'connected', now(), now())).lastrowid
        for topic, platform, variations in (('AI for small business teams', 'LinkedIn', 3), ('Planning a content week', 'Instagram', 2)):
            charge(c, biz, 1 + variations, f'Mock generation: {topic} ({variations} variations)')
            c.execute('INSERT INTO workflow_runs(business_id,topic,platform,variations,credits,status,created_at) VALUES(?,?,?,?,?,?,?)',
                      (biz, topic, platform, variations, 1 + variations, 'review', now()))
        for title, platform, status, content in DEMO_POSTS:
            draft_id = c.execute('INSERT INTO drafts(business_id,title,content,platform,topic,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                                 (biz, title, content, platform, title, status, now(), now())).lastrowid
            if status == 'published':
                when = (t - timedelta(days=1)).isoformat()
                c.execute('INSERT INTO scheduled_posts(business_id,draft_id,title,content,platform,scheduled_at,status,created_at,connection_id,attempts,external_id,published_at) '
                          'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (biz, draft_id, title, content, platform, when, 'published', when, channels[platform], 1,
                                                              f'demo-{platform.lower()}-{draft_id}-seed', when))
            elif status == 'scheduled':
                when = (t + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
                c.execute('INSERT INTO scheduled_posts(business_id,draft_id,title,content,platform,scheduled_at,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                          (biz, draft_id, title, content, platform, when, 'scheduled', now()))
        rule_id = c.execute('INSERT INTO automation_rules(business_id,name,trigger_type,action_type,topic_template,platform,enabled,created_at,updated_at) '
                            'VALUES(?,?,?,?,?,?,?,?,?)', (biz, 'Weekly content tip', 'manual', 'create_draft',
                                                         'Repurpose one post three ways', 'Instagram', 1, now(), now())).lastrowid
        c.execute('INSERT INTO automation_executions(business_id,rule_id,rule_name,status,details,created_at) VALUES(?,?,?,?,?,?)',
                  (biz, rule_id, 'Weekly content tip', 'success', 'Created a draft; provider calls disabled.', now()))
    logger.info('Created the shared demo account')


seed_demo_account()
