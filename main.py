import os, logging, sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Annotated, Literal
from pydantic import BaseModel, Field, StringConstraints, field_validator
from dotenv import load_dotenv
import httpx

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('socialpilot')
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('SOCIALPILOT_DB') or BASE_DIR / 'socialpilot.db')
app = FastAPI(title='SocialPilot AI')
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'static')), name='static')


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, content TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'LinkedIn', topic TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft', rejection_reason TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scheduled_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, title TEXT NOT NULL, content TEXT NOT NULL, platform TEXT NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, platform TEXT NOT NULL, variations INTEGER NOT NULL, credits INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS credit_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, trigger_type TEXT NOT NULL, action_type TEXT NOT NULL, topic_template TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'LinkedIn', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_executions (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER, rule_name TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS integration_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'not_connected', notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
        ''')
        if c.execute('SELECT COUNT(*) FROM credit_ledger').fetchone()[0] == 0:
            c.execute('INSERT INTO credit_ledger(amount,reason,created_at) VALUES(?,?,?)',(100,'Initial demo credit balance',now()))

def mock_mode(): return os.getenv('SOCIALPILOT_MOCK_MODE', '1') == '1' or not os.getenv('ANTHROPIC_API_KEY')
def now(): return datetime.now(timezone.utc).isoformat()
init_db()

Platform = Literal['LinkedIn', 'Instagram']
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

class GenerateRequest(BaseModel):
    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    brand: str = 'My Brand'
    platform: Platform = 'LinkedIn'
    tone: str = 'Professional'
    objective: str = 'Engagement'
    variations: int = Field(default=3, ge=1, le=5)
class DraftCreate(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)]
    content: Text
    platform: Platform = 'LinkedIn'
    topic: str = ''
class DraftUpdate(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)] | None = None
    content: Text | None = None
    platform: Platform | None = None
    topic: str | None = None
class RejectRequest(BaseModel):
    reason: str = ''
class ScheduleRequest(BaseModel):
    draft_id: int | None = None
    title: Text
    content: Text
    platform: Platform
    scheduled_at: str

class AutomationCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    trigger_type: Literal['manual'] = 'manual'  # only manual execution is implemented
    action_type: Literal['create_draft'] = 'create_draft'  # only draft creation is implemented
    topic_template: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    platform: Platform = 'LinkedIn'

class AutomationUpdate(BaseModel):
    enabled: bool | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    topic_template: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)] | None = None
    platform: Platform | None = None

@app.get('/')
def home(): return FileResponse(str(BASE_DIR / 'static' / 'index.html'))


@app.get('/api/health')
def health(): return {'status':'ok','mode':'demo-mock' if mock_mode() else 'ai-enabled','database':'sqlite'}

@app.get('/api/credits')
def credits():
    with db() as c:
        total=c.execute('SELECT COALESCE(SUM(amount),0) FROM credit_ledger').fetchone()[0]
        used=c.execute('SELECT COALESCE(SUM(-amount),0) FROM credit_ledger WHERE amount<0').fetchone()[0]
        last=c.execute('SELECT -amount AS cost FROM credit_ledger WHERE amount<0 ORDER BY id DESC LIMIT 1').fetchone()
        return {'balance':total,'used':used,'lastCost':last['cost'] if last else 0,'transactions':[dict(x) for x in c.execute('SELECT * FROM credit_ledger ORDER BY id DESC LIMIT 30')]}
@app.post('/api/credits/reset')
def reset_credits():
    with db() as c:
        c.execute('DELETE FROM credit_ledger')
        c.execute('INSERT INTO credit_ledger(amount,reason,created_at) VALUES(?,?,?)',(100,'Demo credit reset',now()))
    return credits()

@app.post('/api/generate')
async def generate(req: GenerateRequest):
    # Offline mock mode is the default. No provider/API key is used unless explicitly configured.
    key=os.getenv('ANTHROPIC_API_KEY')
    if mock_mode():
        variations=[]
        for i in range(req.variations):
            hooks=[f"A practical look at {req.topic}.",f"What if we approached {req.topic} differently?",f"Here's a lesson worth sharing about {req.topic}.",f"Small steps can make a difference with {req.topic}.",f"Let's rethink {req.topic}."]
            body=f"{hooks[i%len(hooks)]}\n\nAt {req.brand}, we believe useful ideas should be clear, practical, and easy to apply. Focus on one meaningful takeaway, put it into action, and learn from the results.\n\nWhat has your experience been?"
            tags=' '.join('#'+w for w in ''.join(req.topic+' '+req.brand).split() if w.isalnum())
            variations.append(f"{i+1}. Caption:\n{body}\n\nHashtags: {tags} #{req.platform.replace(' ','')} #ContentStrategy\nCTA: Share your perspective in the comments.")
        content='\n\n'.join(variations)
        cost=1+req.variations
        with db() as c:
            balance=c.execute('SELECT COALESCE(SUM(amount),0) FROM credit_ledger').fetchone()[0]
            if balance<cost: raise HTTPException(402,f'Not enough demo credits. Need {cost}, have {balance}.')
            c.execute('INSERT INTO credit_ledger(amount,reason,created_at) VALUES(?,?,?)',(-cost,f'Mock generation: {req.topic} ({req.variations} variations)',now()))
            c.execute('INSERT INTO workflow_runs(topic,platform,variations,credits,status,created_at) VALUES(?,?,?,?,?,?)',(req.topic,req.platform,req.variations,cost,'review',now()))
        return {'content':content,'mock':True,'credits_used':cost}
    prompt=(f"Create {req.variations} distinct social media post variations for brand {req.brand}. Platform: {req.platform}. Topic: {req.topic}. Tone: {req.tone}. Objective: {req.objective}. Respect platform conventions. Return each variation with a caption, 3-8 relevant hashtags, and a clear CTA. Plain text, numbered, no preamble.")
    payload={'model':os.getenv('ANTHROPIC_MODEL','claude-3-5-haiku-latest'),'max_tokens':1500,'messages':[{'role':'user','content':prompt}]}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response=await client.post('https://api.anthropic.com/v1/messages',headers={'x-api-key':key,'anthropic-version':'2023-06-01','content-type':'application/json'},json=payload)
    except httpx.TimeoutException: raise HTTPException(504,'Claude API timed out.')
    except httpx.RequestError: raise HTTPException(502,'Could not connect to Claude API.')
    if response.status_code>=400:
        logger.error('Provider error %s: %s',response.status_code,response.text)
        raise HTTPException(response.status_code,'AI provider request failed; check server logs.')
    content='\n'.join(b.get('text','') for b in response.json().get('content',[]) if b.get('type')=='text').strip()
    if not content: raise HTTPException(502,'Provider returned empty content.')
    cost=1+req.variations
    with db() as c:
        balance=c.execute('SELECT COALESCE(SUM(amount),0) FROM credit_ledger').fetchone()[0]
        if balance<cost: raise HTTPException(402,'Not enough demo credits.')
        c.execute('INSERT INTO credit_ledger(amount,reason,created_at) VALUES(?,?,?)',(-cost,f'AI generation: {req.topic}',now()))
        c.execute('INSERT INTO workflow_runs(topic,platform,variations,credits,status,created_at) VALUES(?,?,?,?,?,?)',(req.topic,req.platform,req.variations,cost,'review',now()))
    return {'content':content,'mock':False,'credits_used':cost}

@app.get('/api/workflows')
def workflows():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM workflow_runs ORDER BY id DESC LIMIT 100')]
@app.get('/api/drafts')
def list_drafts():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM drafts ORDER BY updated_at DESC')]
@app.post('/api/drafts')
def create_draft(d:DraftCreate):
    t=now()
    with db() as c:
        cur=c.execute('INSERT INTO drafts(title,content,platform,topic,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(d.title,d.content,d.platform,d.topic,'draft',t,t))
        return dict(c.execute('SELECT * FROM drafts WHERE id=?',(cur.lastrowid,)).fetchone())
@app.put('/api/drafts/{draft_id}')
def update_draft(draft_id:int,d:DraftUpdate):
    with db() as c:
        old=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
        if not old: raise HTTPException(404,'Draft not found')
        if old['status'] in ('pending_approval','scheduled'): raise HTTPException(409,'Draft cannot be edited while pending approval or scheduled')
        vals={k:v for k,v in d.model_dump().items() if v is not None}
        if old['status'] in ('approved','rejected'): vals['status']='draft'; vals['rejection_reason']=''  # edits require re-approval
        vals['updated_at']=now()
        c.execute('UPDATE drafts SET '+','.join(f'{k}=?' for k in vals)+' WHERE id=?',(*vals.values(),draft_id))
        return dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone())
@app.delete('/api/drafts/{draft_id}')
def delete_draft(draft_id:int):
    with db() as c:
        row=c.execute('SELECT status FROM drafts WHERE id=?',(draft_id,)).fetchone()
        if row and row['status']=='scheduled': raise HTTPException(409,'Cancel the scheduled post before deleting this draft')
        cur=c.execute('DELETE FROM drafts WHERE id=?',(draft_id,))
        if not cur.rowcount: raise HTTPException(404,'Draft not found')
    return {'deleted':True}
@app.post('/api/drafts/{draft_id}/submit')
def submit_draft(draft_id:int):
    with db() as c:
        row=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
        if not row: raise HTTPException(404,'Draft not found')
        if row['status'] not in ('draft','rejected'): raise HTTPException(409,'Draft cannot be submitted from its current state')
        c.execute("UPDATE drafts SET status='pending_approval',rejection_reason='',updated_at=? WHERE id=?",(now(),draft_id))
        return dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone())
@app.get('/api/approvals')
def approvals():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM drafts WHERE status IN ('pending_approval','approved','rejected') ORDER BY updated_at DESC")]
@app.post('/api/drafts/{draft_id}/approve')
def approve_draft(draft_id:int):
    with db() as c:
        row=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
        if not row: raise HTTPException(404,'Draft not found')
        if row['status']!='pending_approval': raise HTTPException(409,'Only pending drafts can be approved')
        c.execute("UPDATE drafts SET status='approved',updated_at=? WHERE id=?",(now(),draft_id))
        return dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone())
@app.post('/api/drafts/{draft_id}/reject')
def reject_draft(draft_id:int,r:RejectRequest):
    with db() as c:
        row=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
        if not row: raise HTTPException(404,'Draft not found')
        if row['status']!='pending_approval': raise HTTPException(409,'Only pending drafts can be rejected')
        c.execute("UPDATE drafts SET status='rejected',rejection_reason=?,updated_at=? WHERE id=?",(r.reason,now(),draft_id))
        return dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone())
@app.post('/api/schedule')
def schedule_post(p:ScheduleRequest):
    try: when=datetime.fromisoformat(p.scheduled_at.replace('Z','+00:00'))
    except ValueError: raise HTTPException(400,'Invalid schedule date/time')
    if when <= (datetime.now(when.tzinfo) if when.tzinfo else datetime.now()): raise HTTPException(400,'Schedule time must be in the future')
    title,content=p.title,p.content
    with db() as c:
        if p.draft_id:
            draft=c.execute('SELECT * FROM drafts WHERE id=?',(p.draft_id,)).fetchone()
            if not draft: raise HTTPException(404,'Draft not found')
            if draft['status']!='approved': raise HTTPException(409,'Only approved drafts can be scheduled')
            title,content=draft['title'],draft['content']  # trust stored approved content, not client payload
        cur=c.execute('INSERT INTO scheduled_posts(draft_id,title,content,platform,scheduled_at,status,created_at) VALUES(?,?,?,?,?,?,?)',(p.draft_id,title,content,p.platform,p.scheduled_at,'scheduled',now()))
        if p.draft_id: c.execute("UPDATE drafts SET status='scheduled',updated_at=? WHERE id=?",(now(),p.draft_id))
        return dict(c.execute('SELECT * FROM scheduled_posts WHERE id=?',(cur.lastrowid,)).fetchone())
@app.get('/api/schedule')
def list_scheduled():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM scheduled_posts WHERE status='scheduled' ORDER BY scheduled_at")]
@app.delete('/api/schedule/{post_id}')
def cancel_schedule(post_id:int):
    with db() as c:
        row=c.execute('SELECT * FROM scheduled_posts WHERE id=?',(post_id,)).fetchone()
        if not row: raise HTTPException(404,'Scheduled post not found')
        if row['status']!='scheduled': raise HTTPException(409,'Scheduled post is not active')
        c.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=?",(post_id,))
        if row['draft_id']: c.execute("UPDATE drafts SET status='approved',updated_at=? WHERE id=?",(now(),row['draft_id']))
    return {'cancelled':True}

# Phase 5: local automation rules (manual execution only; no external publishing)
@app.get('/api/automations')
def list_automations():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM automation_rules ORDER BY id DESC')]
@app.post('/api/automations')
def create_automation(a: AutomationCreate):
    t=now()
    with db() as c:
        if c.execute('SELECT 1 FROM automation_rules WHERE lower(name)=lower(?)',(a.name,)).fetchone(): raise HTTPException(409,'An automation rule with this name already exists')
        cur=c.execute('INSERT INTO automation_rules(name,trigger_type,action_type,topic_template,platform,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(a.name,a.trigger_type,a.action_type,a.topic_template,a.platform,1,t,t))
        return dict(c.execute('SELECT * FROM automation_rules WHERE id=?',(cur.lastrowid,)).fetchone())
@app.put('/api/automations/{rule_id}')
def update_automation(rule_id:int,a:AutomationUpdate):
    with db() as c:
        if not c.execute('SELECT id FROM automation_rules WHERE id=?',(rule_id,)).fetchone(): raise HTTPException(404,'Automation not found')
        vals={k:v for k,v in a.model_dump().items() if v is not None}
        if 'enabled' in vals: vals['enabled']=int(vals['enabled'])
        if not vals: return dict(c.execute('SELECT * FROM automation_rules WHERE id=?',(rule_id,)).fetchone())
        vals['updated_at']=now(); c.execute('UPDATE automation_rules SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),rule_id))
        return dict(c.execute('SELECT * FROM automation_rules WHERE id=?',(rule_id,)).fetchone())
@app.delete('/api/automations/{rule_id}')
def delete_automation(rule_id:int):
    with db() as c:
        cur=c.execute('DELETE FROM automation_rules WHERE id=?',(rule_id,))
        if not cur.rowcount: raise HTTPException(404,'Automation not found')
    return {'deleted':True}
@app.post('/api/automations/{rule_id}/run')
def run_automation(rule_id:int):
    with db() as c:
        rule=c.execute('SELECT * FROM automation_rules WHERE id=?',(rule_id,)).fetchone()
        if not rule: raise HTTPException(404,'Automation not found')
        if not rule['enabled']: raise HTTPException(409,'Automation is disabled')
        # No AI provider calls: execution creates a clearly marked review draft.
        topic=rule['topic_template']
        content=f"[AUTOMATION MOCK — review required]\n\n{topic}\n\nDraft created by automation rule: {rule['name']}. Edit before publishing."
        t=now(); cur=c.execute('INSERT INTO drafts(title,content,platform,topic,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(f"Automation: {rule['name']} · {t}",content,rule['platform'],topic,'draft',t,t))
        details=f"Created draft #{cur.lastrowid}; provider calls disabled."
        c.execute('INSERT INTO automation_executions(rule_id,rule_name,status,details,created_at) VALUES(?,?,?,?,?)',(rule_id,rule['name'],'success',details,t))
        return {'status':'success','draft_id':cur.lastrowid,'details':details}
@app.get('/api/automation-executions')
def automation_executions():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM automation_executions ORDER BY id DESC LIMIT 100')]

# Phase 6: integration registry, deliberately disconnected and credential-free.
@app.get('/api/integrations')
def integrations():
    providers=[('n8n','Automation webhooks','not_connected','Webhook integration scaffold; configure endpoint outside this demo.'),('LinkedIn','Social publishing','not_connected','OAuth publishing is not implemented.'),('Instagram','Social publishing','not_connected','OAuth publishing is not implemented.'),('Analytics provider','Performance analytics','not_connected','No external analytics data connected.') ]
    return [{'provider':p,'category':cat,'status':st,'notes':notes} for p,cat,st,notes in providers]

# Phase 7: analytics from local demo records only.
@app.get('/api/analytics')
def analytics():
    with db() as c:
        total_runs=c.execute('SELECT COUNT(*) FROM workflow_runs').fetchone()[0]
        total_credits=c.execute('SELECT COALESCE(SUM(-amount),0) FROM credit_ledger WHERE amount<0').fetchone()[0]
        drafts_by_status=[dict(r) for r in c.execute('SELECT status,COUNT(*) AS count FROM drafts GROUP BY status')]
        scheduled_count=c.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status='scheduled'").fetchone()[0]
        published_count=c.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status='published'").fetchone()[0]
        automation_count=c.execute('SELECT COUNT(*) FROM automation_rules').fetchone()[0]
        automation_runs=c.execute('SELECT COUNT(*) FROM automation_executions').fetchone()[0]
        by_platform=[dict(r) for r in c.execute('SELECT platform,COUNT(*) AS count FROM drafts GROUP BY platform')]
        recent_runs=[dict(r) for r in c.execute('SELECT topic,platform,variations,credits,status,created_at FROM workflow_runs ORDER BY id DESC LIMIT 10')]
    return {'generation_runs':total_runs,'credits_consumed':total_credits,'drafts_by_status':drafts_by_status,'scheduled_posts':scheduled_count,'published_posts':published_count,'automation_rules':automation_count,'automation_executions':automation_runs,'drafts_by_platform':by_platform,'recent_generations':recent_runs,'mode':'local demo data'}
