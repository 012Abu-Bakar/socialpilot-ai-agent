// SocialPilot AI — dashboard client. All state lives in the backend; this file only renders it.
const $ = id => document.getElementById(id);

const state = {
  mode: 'demo-mock',          // from /api/health: 'demo-mock' | 'ai-enabled'
  me: null,                   // from /api/me: signed-in user, current business + role, all their businesses
  current: '', currentTopic: '', currentPlatform: 'LinkedIn',
  drafts: [], runs: [], scheduled: [], members: [],
  credits: { balance: 100, used: 0, lastCost: 0 },
  calendarDate: (() => { const d = new Date(); d.setDate(1); return d; })(),
};
const isMock = () => state.mode !== 'ai-enabled';
const myRole = () => state.me?.business.role;
const isOwner = () => myRole() === 'owner';
const canApprove = () => ['owner', 'approver'].includes(myRole());

// ------------------------------------------------------------------ helpers
function toast(message) {
  const t = $('toast');
  t.textContent = message;
  t.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove('show'), 2600);
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(url, opts = {}) {
  const r = await fetch(url, { ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  let data = {};
  try { data = await r.json(); } catch { /* empty or non-JSON body */ }
  if (r.status === 401 && !url.startsWith('/api/auth/')) showAuth();  // session ended (signed out or removed from the business)
  if (!r.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(e => `${e.loc?.at(-1) ?? 'field'}: ${e.msg}`).join('; ') : data.detail;
    throw new Error(detail || `Request failed (${r.status})`);
  }
  return data;
}
const post = (url, body) => api(url, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
const put = (url, body) => api(url, { method: 'PUT', body: JSON.stringify(body) });
const del = url => api(url, { method: 'DELETE' });

const fmtDateTime = iso => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const fmtTime = iso => new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
const localDayKey = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const truncate = (s, n) => esc(s.slice(0, n)) + (s.length > n ? '…' : '');
const STATUS_LABEL = { draft: 'Draft', pending_approval: 'Pending approval', approved: 'Approved', rejected: 'Rejected', scheduled: 'Scheduled',
                       publishing: 'Publishing', published: 'Published', failed: 'Failed' };
const ROLE_LABEL = { owner: 'Owner', approver: 'Approver', editor: 'Editor' };

// ------------------------------------------------------------------ modal dialog (replaces prompt/confirm)
function openModal({ title, body = '', confirmLabel = 'Confirm', validate }) {
  const dialog = $('modal'), form = $('modalForm');
  $('modalTitle').textContent = title;
  $('modalBody').innerHTML = body;
  $('modalError').textContent = '';
  $('modalConfirm').textContent = confirmLabel;
  return new Promise(resolve => {
    const finish = value => {
      form.removeEventListener('submit', onSubmit);
      $('modalCancel').removeEventListener('click', onCancel);
      dialog.removeEventListener('cancel', onCancel);
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const onSubmit = e => {
      e.preventDefault();
      const values = Object.fromEntries(new FormData(form));
      const error = validate?.(values);
      if (error) { $('modalError').textContent = error; return; }
      finish(values);
    };
    const onCancel = e => { e.preventDefault(); finish(null); };
    form.addEventListener('submit', onSubmit);
    $('modalCancel').addEventListener('click', onCancel);
    dialog.addEventListener('cancel', onCancel);  // Escape key
    dialog.showModal();
    form.querySelector('input,textarea,select,#modalConfirm')?.focus();
  });
}
const confirmModal = (title, confirmLabel) => openModal({ title, confirmLabel }).then(Boolean);
const platformOptions = selected => ['LinkedIn', 'Instagram'].map(p => `<option ${p === selected ? 'selected' : ''}>${p}</option>`).join('');

// ------------------------------------------------------------------ mode + credits
function renderMode() {
  const mock = isMock();
  $('modeBadge').textContent = mock ? '● Demo mode (mock generation)' : '● AI mode (provider enabled)';
  $('modePill').textContent = mock ? '✦  Mock generation (demo)' : '✦  AI generation';
  $('securityNote').textContent = mock ? '🔒 Demo mode: no AI provider is called. Any API key stays server-side.'
                                       : '🔒 AI mode: generation calls the AI provider from the server. Your API key never reaches the browser.';
  $('resetCredits').hidden = !mock || !isOwner();  // the backend only allows owners to reset, and only in demo mode
}

// ------------------------------------------------------------------ account: sign-in, business switcher, user panel
function showAuth() {
  state.me = null;
  $('app').hidden = true;
  $('authScreen').hidden = false;
}

function setAuthMode(register) {
  $('loginForm').hidden = register;
  $('registerForm').hidden = !register;
  $('authHeading').textContent = register ? 'Create your account' : 'Sign in';
  $('authIntro').textContent = register ? 'Your account comes with its own business workspace and 100 demo credits.'
                                        : 'Sign in to your business workspace.';
  $('authToggle').textContent = register ? 'Already have an account? Sign in' : 'New here? Create an account';
  $('authError').textContent = '';
  (register ? $('registerForm') : $('loginForm')).querySelector('input').focus();
}

function renderAccount() {
  const { user, business, businesses } = state.me;
  $('businessSelect').innerHTML = businesses.map(b =>
    `<option value="${b.id}" ${b.id === business.id ? 'selected' : ''}>${esc(b.name)} · ${esc(ROLE_LABEL[b.role])}</option>`).join('');
  $('userName').textContent = user.name;
  $('userRole').textContent = `${ROLE_LABEL[business.role]} · ${business.name}`;
  $('avatar').textContent = user.name.split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
  $('headerBusiness').textContent = business.name;
  $('memberForm').hidden = !isOwner();
}

async function enterApp(me) {
  state.me = me;
  $('authScreen').hidden = true;
  $('app').hidden = false;
  renderAccount();
  await loadAll();
}

async function submitAuth(e, url) {
  e.preventDefault();
  const button = e.target.querySelector('button[type=submit]');
  button.disabled = true;
  $('authError').textContent = '';
  try {
    const me = await post(url, Object.fromEntries(new FormData(e.target)));
    e.target.reset();
    await enterApp(me);
  } catch (err) { $('authError').textContent = err.message; }
  finally { button.disabled = false; }
}

$('loginForm').addEventListener('submit', e => submitAuth(e, '/api/auth/login'));
$('registerForm').addEventListener('submit', e => submitAuth(e, '/api/auth/register'));
$('authToggle').addEventListener('click', () => setAuthMode($('registerForm').hidden));

$('signOut').addEventListener('click', async () => {
  try { await post('/api/auth/logout'); } catch { /* signed out either way */ }
  showAuth();
  setAuthMode(false);
});

// Switching business swaps every panel to that business's data.
function resetStudio() {
  Object.assign(state, { current: '', currentTopic: '' });
  $('output').innerHTML = '<div class="emptyicon" aria-hidden="true">✦</div><h3>Your content will appear here</h3><p>Enter a topic and generate your first content variations.</p>';
  $('output').className = 'empty';
  $('resultTag').textContent = 'Waiting';
  ['copy', 'save', 'saveSubmit'].forEach(id => { $(id).disabled = true; });
}

$('businessSelect').addEventListener('change', async e => {
  try {
    resetStudio();
    await enterApp(await post('/api/session/business', { business_id: Number(e.target.value) }));
    toast(`Switched to ${state.me.business.name}`);
  } catch (err) { toast(err.message); }
});

$('newBusiness').addEventListener('click', async () => {
  const values = await openModal({
    title: 'New business account', confirmLabel: 'Create',
    body: '<p class="modal-note">Each business has its own drafts, schedule, channels, team and demo credits. You will be its owner.</p>' +
          '<label>Business name<input name="name" maxlength="120"></label>',
    validate: v => (!v.name.trim() ? 'Enter a business name.' : null),
  });
  if (!values) return;
  try {
    resetStudio();
    await enterApp(await post('/api/businesses', { name: values.name }));
    toast(`Created ${state.me.business.name}`);
  } catch (err) { toast(err.message); }
});

function renderCredits() {
  const { balance, used, lastCost } = state.credits;
  $('creditBalance').textContent = balance;
  $('creditUsed').textContent = used;
  $('lastCost').textContent = lastCost ? `${lastCost} credits` : '—';
  const pct = Math.max(0, Math.min(100, balance / Math.max(100, balance) * 100));
  $('creditBar').style.width = `${pct}%`;
  $('creditTrack').setAttribute('aria-valuenow', Math.round(pct));
}

async function loadCredits() { state.credits = await api('/api/credits'); renderCredits(); }

// ------------------------------------------------------------------ workflow runs
function renderRuns() {
  $('workflowRuns').innerHTML = state.runs.length ? state.runs.slice(0, 10).map(r => `
    <div class="run">
      <div class="run-top"><strong>${esc(r.topic)}</strong><span class="tag ready">${esc(r.status)}</span></div>
      <div class="run-meta">${esc(r.platform)} · ${Number(r.variations)} variation(s) · ${Number(r.credits)} credits · ${esc(fmtDateTime(r.created_at))}</div>
      <div class="run-steps"><span class="done">✓ Brief validated</span><span class="done">✓ Generation completed</span><span class="done">✓ Review workflow logged</span><span class="pending">○ Publish after approval</span></div>
    </div>`).join('') : '<p class="muted">No workflow runs yet. Generate content to create your first run.</p>';
}

async function loadRuns() { state.runs = await api('/api/workflows'); renderRuns(); }

// ------------------------------------------------------------------ drafts + approvals
function draftButtons(d) {
  const b = (action, label) => `<button class="secondary" data-action="${action}" data-id="${d.id}">${label}</button>`;
  return [
    ['draft', 'rejected', 'approved'].includes(d.status) && b('edit', 'Edit'),
    ['draft', 'rejected'].includes(d.status) && b('submit', 'Submit for approval'),
    d.status === 'approved' && b('schedule', 'Schedule approved'),
    d.status !== 'scheduled' && b('delete', 'Delete'),
  ].filter(Boolean).join('');
}

function renderDrafts() {
  $('draftCount').textContent = state.drafts.length;
  $('drafts').innerHTML = state.drafts.length ? state.drafts.map(d => `
    <div class="draft" data-id="${d.id}">
      <strong>${esc(d.title)} <span class="tag">${esc(STATUS_LABEL[d.status] ?? d.status)}</span></strong>
      <p>${esc(d.platform)} · updated ${esc(fmtDateTime(d.updated_at))}</p>
      <p>${truncate(d.content, 280)}</p>
      <div class="draft-actions">${draftButtons(d)}</div>
      ${d.status === 'pending_approval' ? '<small>Waiting in the approval queue.</small>' : ''}
      ${d.status === 'scheduled' ? '<small>Cancel the schedule in the calendar to edit or delete.</small>' : ''}
      ${d.status === 'published' ? '<small>Published content is locked. See the publishing history in the calendar.</small>' : ''}
      ${d.rejection_reason ? `<small>Reason: ${esc(d.rejection_reason)}</small>` : ''}
    </div>`).join('') : '<p class="muted">No drafts saved yet.</p>';
}

async function renderApprovals() {
  try {
    const pending = (await api('/api/approvals')).filter(x => x.status === 'pending_approval');
    $('approvalQueue').innerHTML = pending.length ? pending.map(d => `
      <div class="draft">
        <strong>${esc(d.title)}</strong>
        <p>${truncate(d.content, 300)}</p>
        ${canApprove() ? `<div class="draft-actions">
          <button class="secondary" data-action="approve" data-id="${d.id}">Approve</button>
          <button class="secondary" data-action="reject" data-id="${d.id}">Reject</button>
        </div>` : '<small>Waiting for an approver or owner to review.</small>'}
      </div>`).join('') : '<p class="muted">No items waiting for approval.</p>';
  } catch (e) { $('approvalQueue').innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

async function refreshDrafts() {
  state.drafts = await api('/api/drafts');
  renderDrafts();
  await renderApprovals();
  loadAutomationPanels();
}

async function refreshSchedule() {
  const [scheduled, history] = await Promise.all([api('/api/schedule'), api('/api/publishing')]);
  state.scheduled = scheduled;
  renderCalendar();
  renderHistory(history);
}

// After anything that changes publishing state, every panel that shows it is refreshed.
async function refreshPublishing() {
  await Promise.all([refreshSchedule(), refreshDrafts()]);
}

const draftActions = {
  async edit(id) {
    const d = state.drafts.find(x => x.id === id);
    if (!d) return;
    const values = await openModal({
      title: 'Edit draft',
      confirmLabel: 'Save changes',
      body: `
        ${d.status === 'approved' ? '<p class="modal-note">This draft is approved. Saving changes sends it back to draft and it must be approved again.</p>' : ''}
        <label>Title<input name="title" maxlength="250" value="${esc(d.title)}"></label>
        <label>Platform<select name="platform">${platformOptions(d.platform)}</select></label>
        <label>Content<textarea name="content" rows="10" maxlength="10000">${esc(d.content)}</textarea></label>`,
      validate: v => (!v.title.trim() ? 'Title is required.' : !v.content.trim() ? 'Content is required.' : null),
    });
    if (!values) return;
    await put(`/api/drafts/${id}`, values);
    await refreshDrafts();
    toast('Draft updated');
  },
  async submit(id) {
    await post(`/api/drafts/${id}/submit`);
    await refreshDrafts();
    toast('Submitted for approval');
  },
  async delete(id) {
    if (!await confirmModal('Delete this draft? This cannot be undone.', 'Delete')) return;
    await del(`/api/drafts/${id}`);
    await refreshDrafts();
    toast('Draft deleted');
  },
  async approve(id) {
    await post(`/api/drafts/${id}/approve`);
    await refreshDrafts();
    toast('Draft approved');
  },
  async reject(id) {
    const values = await openModal({
      title: 'Reject draft', confirmLabel: 'Reject',
      body: '<label>Reason (optional)<textarea name="reason" rows="3" maxlength="1000"></textarea></label>',
    });
    if (!values) return;
    await post(`/api/drafts/${id}/reject`, { reason: values.reason });
    await refreshDrafts();
    toast('Draft rejected');
  },
  async schedule(id) {
    const d = state.drafts.find(x => x.id === id);
    if (!d || d.status !== 'approved') { toast('Only approved drafts can be scheduled'); return; }
    const values = await openModal({
      title: 'Schedule approved draft', confirmLabel: 'Schedule',
      body: `<p class="modal-note">${esc(d.title)} · ${esc(d.platform)}. Times are in your local timezone. The post publishes through this business's ${esc(d.platform)} channel when it falls due.</p>
             <label>Date and time<input type="datetime-local" name="when" required></label>`,
      validate: v => {
        const when = new Date(v.when);
        if (!v.when || isNaN(when)) return 'Choose a date and time.';
        if (when <= new Date()) return 'Schedule time must be in the future.';
        return null;
      },
    });
    if (!values) return;
    await post('/api/schedule', { draft_id: id, scheduled_at: new Date(values.when).toISOString() });
    await refreshPublishing();
    toast('Approved draft scheduled');
  },
  async cancelSchedule(id) {
    await del(`/api/schedule/${id}`);
    await refreshPublishing();
    toast('Schedule cancelled');
  },
  async publishNow(id) {
    const p = await post(`/api/schedule/${id}/publish`);
    await refreshPublishing();
    toast(p.status === 'published' ? `Published to ${p.platform} (demo channel)` : `Publishing failed: ${p.last_error}`);
  },
  async connectChannel(_, button) {
    const platform = button.dataset.platform;
    const values = await openModal({
      title: `Connect ${platform} demo channel`, confirmLabel: 'Connect',
      body: `<p class="modal-note">A demo channel checks posts the way ${esc(platform)} would and records them here. Nothing is sent to ${esc(platform)}.</p>
             <label>Account or page name<input name="account_name" maxlength="120" value="${esc(state.me.business.name)}"></label>`,
      validate: v => (!v.account_name.trim() ? 'Enter the account name.' : null),
    });
    if (!values) return;
    await post('/api/connections', { platform, account_name: values.account_name });
    await loadIntegrations();
    toast(`${platform} demo channel connected`);
  },
  async disconnectChannel(id) {
    if (!await confirmModal('Disconnect this channel? Scheduled posts for it will fail until a channel is connected again.', 'Disconnect')) return;
    await del(`/api/connections/${id}`);
    await loadIntegrations();
    toast('Channel disconnected');
  },
  async changeRole(id) {
    const m = state.members.find(x => x.user_id === id);
    const values = await openModal({
      title: `Change role for ${m.name}`, confirmLabel: 'Save',
      body: `<label>Role<select name="role">${Object.entries(ROLE_LABEL).map(([v, l]) => `<option value="${v}" ${v === m.role ? 'selected' : ''}>${l}</option>`).join('')}</select></label>`,
    });
    if (!values) return;
    await put(`/api/members/${id}`, { role: values.role });
    await loadTeam();
    toast('Role updated');
  },
  async removeMember(id) {
    const m = state.members.find(x => x.user_id === id);
    if (!await confirmModal(`Remove ${m.name} from this business?`, 'Remove')) return;
    await del(`/api/members/${id}`);
    await loadTeam();
    toast('Member removed');
  },
  async runAutomation(id) {
    const r = await post(`/api/automations/${id}/run`);
    await refreshDrafts();
    toast(`Automation ran · Draft #${r.draft_id} created`);
  },
  async toggleAutomation(id, button) {
    const enable = button.dataset.enable === '1';
    await put(`/api/automations/${id}`, { enabled: enable });
    await loadAutomationPanels();
    toast(enable ? 'Automation enabled' : 'Automation disabled');
  },
  async deleteAutomation(id) {
    if (!await confirmModal('Delete this automation rule?', 'Delete')) return;
    await del(`/api/automations/${id}`);
    await loadAutomationPanels();
    toast('Automation deleted');
  },
};

// One delegated handler for every data-action button; disables the button while the request runs.
document.addEventListener('click', async e => {
  const button = e.target.closest('button[data-action]');
  if (!button || button.disabled) return;
  const action = draftActions[button.dataset.action];
  if (!action) return;
  button.disabled = true;
  try { await action(Number(button.dataset.id), button); }
  catch (err) { toast(err.message); }
  finally { if (button.isConnected) button.disabled = false; }
});

// ------------------------------------------------------------------ generation
$('variations').addEventListener('input', e => { $('varLabel').textContent = e.target.value; });

$('resetCredits').addEventListener('click', async () => {
  try { state.credits = await post('/api/credits/reset'); renderCredits(); loadAutomationPanels(); toast('Demo credits reset'); }
  catch (e) { toast(e.message); }
});

$('generate').addEventListener('click', async () => {
  const topic = $('topic').value.trim();
  if (!topic) { toast('Enter a topic first.'); $('topic').focus(); return; }
  const btn = $('generate');
  btn.disabled = true;
  btn.textContent = 'Generating…';
  $('resultTag').textContent = 'Working';
  $('output').innerHTML = isMock()
    ? '<div class="empty"><h3>Generating demo content…</h3><p>No AI provider is being called.</p></div>'
    : '<div class="empty"><h3>Generating content…</h3><p>Waiting for the AI provider.</p></div>';
  try {
    const data = await post('/api/generate', {
      topic, brand: $('brand').value.trim() || 'My Brand', platform: $('platform').value,
      tone: $('tone').value, objective: $('objective').value, variations: Number($('variations').value),
    });
    Object.assign(state, { current: data.content, currentTopic: topic, currentPlatform: $('platform').value });
    $('output').innerHTML = '<div class="generated" id="generated"></div>';
    $('generated').textContent = state.current;
    $('resultTag').textContent = data.mock ? 'Generated (mock)' : 'Generated';
    ['copy', 'save', 'saveSubmit'].forEach(id => { $(id).disabled = false; });
    toast(`${data.mock ? 'Generated mock content' : 'Generated content'} · ${data.credits_used} credits used`);
    await Promise.all([loadCredits(), loadRuns()]);
    loadAutomationPanels();
  } catch (e) {
    $('output').innerHTML = `<div class="empty"><h3>Could not generate content</h3><p>${esc(e.message)}</p></div>`;
    $('resultTag').textContent = 'Error';
    toast(e.message);
    loadCredits().catch(() => {});
  } finally {
    btn.disabled = false;
    btn.textContent = '✦ Generate content';
  }
});

$('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(state.current); toast('Copied to clipboard'); }
  catch { toast('Clipboard is unavailable here; select the text and copy it manually.'); }
});

async function saveCurrentDraft() {
  if (!state.current) return null;
  const title = `${$('brand').value.trim() || 'My Brand'} · ${state.currentPlatform} · ${new Date().toLocaleString()}`;
  return post('/api/drafts', { title, content: state.current, platform: state.currentPlatform, topic: state.currentTopic });
}

$('save').addEventListener('click', async () => {
  try { if (await saveCurrentDraft()) { await refreshDrafts(); toast('Draft saved to database'); } }
  catch (e) { toast(e.message); }
});

$('saveSubmit').addEventListener('click', async () => {
  try {
    const d = await saveCurrentDraft();
    if (!d) return;
    await post(`/api/drafts/${d.id}/submit`);
    await refreshDrafts();
    toast('Draft saved and submitted for approval');
  } catch (e) { toast(e.message); }
});

// ------------------------------------------------------------------ calendar
function renderCalendar() {
  const cal = state.calendarDate, y = cal.getFullYear(), m = cal.getMonth();
  $('calendarMonth').textContent = cal.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const byDay = {};
  for (const p of state.scheduled) (byDay[localDayKey(new Date(p.scheduled_at))] ??= []).push(p);
  const start = new Date(y, m, 1).getDay(), days = new Date(y, m + 1, 0).getDate();
  let html = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map(n => `<div class="calendar-weekday">${n}</div>`).join('');
  html += '<div class="calendar-day outside"></div>'.repeat(start);
  for (let day = 1; day <= days; day++) {
    const posts = byDay[localDayKey(new Date(y, m, day))] ?? [];
    html += `<div class="calendar-day"><span class="day-number">${day}</span>${posts.map(p =>
      `<div class="calendar-event" title="${esc(p.title)}">${esc(fmtTime(p.scheduled_at))} · ${esc(p.platform)}</div>`).join('')}</div>`;
  }
  $('calendarGrid').innerHTML = html;
  $('scheduledPosts').innerHTML = state.scheduled.length ? state.scheduled.map(p => `
    <div class="scheduled-item">
      <div>
        <strong>${esc(p.title)}</strong>
        <p>${esc(fmtDateTime(p.scheduled_at))} · ${esc(p.platform)}</p>
        <div class="scheduled-text">${truncate(p.content, 220)}</div>
      </div>
      <div class="automation-actions">
        <button class="secondary" data-action="publishNow" data-id="${p.id}">Publish now</button>
        <button class="secondary" data-action="cancelSchedule" data-id="${p.id}">Remove</button>
      </div>
    </div>`).join('') : '<p class="muted">No scheduled posts yet.</p>';
}

function renderHistory(posts) {
  $('publishHistory').innerHTML = posts.length ? posts.map(p => `
    <div class="scheduled-item" data-id="${p.id}">
      <div>
        <strong>${esc(p.title)} <span class="status-pill ${esc(p.status)}">${esc(STATUS_LABEL[p.status] ?? p.status)}</span></strong>
        <p>${esc(p.platform)} · ${p.published_at ? `published ${esc(fmtDateTime(p.published_at))} · post id ${esc(p.external_id)}`
                                                 : `scheduled for ${esc(fmtDateTime(p.scheduled_at))} · ${Number(p.attempts)} attempt(s)`}</p>
        ${p.last_error ? `<p class="error">${esc(p.last_error)}</p>` : ''}
      </div>
      ${p.status === 'failed' ? `<div class="automation-actions">
        <button class="secondary" data-action="publishNow" data-id="${p.id}">Retry</button>
        <button class="secondary" data-action="cancelSchedule" data-id="${p.id}">Remove</button>
      </div>` : ''}
    </div>`).join('') : '<p class="muted">Nothing published yet.</p>';
}

$('runDue').addEventListener('click', async () => {
  try {
    const r = await post('/api/publishing/run-due');
    await refreshPublishing();
    toast(r.processed ? `${r.published} published, ${r.failed} failed` : 'No posts are due yet');
  } catch (e) { toast(e.message); }
});

$('prevMonth').addEventListener('click', () => { state.calendarDate.setMonth(state.calendarDate.getMonth() - 1); renderCalendar(); });
$('nextMonth').addEventListener('click', () => { state.calendarDate.setMonth(state.calendarDate.getMonth() + 1); renderCalendar(); });

// ------------------------------------------------------------------ automations, integrations, analytics
async function loadAutomationPanels() {
  try {
    const [rules, execs] = await Promise.all([api('/api/automations'), api('/api/automation-executions')]);
    $('automationList').innerHTML = rules.length ? rules.map(r => `
      <div class="automation-rule">
        <div>
          <strong>${esc(r.name)}</strong>
          <div class="rule-meta">${esc(r.platform)} · manual trigger · creates a draft · ${r.enabled ? 'Enabled' : 'Disabled'}</div>
          <div class="rule-meta">${esc(r.topic_template)}</div>
        </div>
        <div class="automation-actions">
          <button class="secondary" data-action="runAutomation" data-id="${r.id}" ${r.enabled ? '' : 'disabled title="Enable the rule to run it"'}>Run now</button>
          <button class="secondary" data-action="toggleAutomation" data-id="${r.id}" data-enable="${r.enabled ? 0 : 1}">${r.enabled ? 'Disable' : 'Enable'}</button>
          <button class="secondary" data-action="deleteAutomation" data-id="${r.id}">Delete</button>
        </div>
      </div>`).join('') : '<p class="muted">No automation rules created.</p>';
    $('automationExecutions').innerHTML = execs.length ? execs.map(x => `
      <div class="scheduled-item"><div>
        <strong>${esc(x.rule_name)}</strong>
        <p>${esc(x.status)} · ${esc(fmtDateTime(x.created_at))}</p>
        <div class="scheduled-text">${esc(x.details)}</div>
      </div></div>`).join('') : '<p class="muted">No executions yet.</p>';
  } catch (e) { $('automationList').innerHTML = `<p class="muted">${esc(e.message)}</p>`; }

  try {
    const a = await api('/api/analytics');
    const cards = [['Generation runs', a.generation_runs], ['Credits consumed', a.credits_consumed], ['Scheduled posts', a.scheduled_posts],
                   ['Automation rules', a.automation_rules], ['Automation executions', a.automation_executions], ['Published posts', a.published_posts],
                   ['Failed posts', a.failed_posts]];
    $('analyticsCards').innerHTML = cards.map(([label, val]) => `<div class="metric-card"><strong>${Number(val)}</strong><small>${label}</small></div>`).join('');
    const rows = (items, key) => items.map(x => `<tr><td>${esc(STATUS_LABEL[x[key]] ?? x[key])}</td><td>${Number(x.count)}</td></tr>`).join('') || '<tr><td colspan="2">No data</td></tr>';
    $('analyticsDetails').innerHTML = `
      <h3>Draft status</h3><table class="analytics-table"><thead><tr><th scope="col">Status</th><th scope="col">Count</th></tr></thead><tbody>${rows(a.drafts_by_status, 'status')}</tbody></table>
      <h3>Drafts by platform</h3><table class="analytics-table"><thead><tr><th scope="col">Platform</th><th scope="col">Count</th></tr></thead><tbody>${rows(a.drafts_by_platform, 'platform')}</tbody></table>
      <h3>Published by platform</h3><table class="analytics-table"><thead><tr><th scope="col">Platform</th><th scope="col">Count</th></tr></thead><tbody>${rows(a.published_by_platform, 'platform')}</tbody></table>
      <p class="muted">Counts come from this business's records. Demo channels record posts locally; engagement metrics need a live platform connection.</p>`;
  } catch (e) { $('analyticsCards').innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

function channelButton(i) {
  if (!isOwner() || i.category !== 'Social publishing') return '';
  return i.connection_id ? `<button class="secondary" data-action="disconnectChannel" data-id="${i.connection_id}">Disconnect</button>`
                         : `<button class="secondary" data-action="connectChannel" data-platform="${esc(i.provider)}">Connect demo channel</button>`;
}

async function loadIntegrations() {
  try {
    const items = await api('/api/integrations');
    $('integrationList').innerHTML = items.map(i => `
      <div class="integration-item">
        <div><strong>${esc(i.provider)}</strong><div class="integration-meta">${esc(i.category)} · ${esc(i.notes)}</div></div>
        <div class="automation-actions">${channelButton(i)}<span class="status-pill ${esc(i.status)}">${esc(i.status.replace('_', ' '))}</span></div>
      </div>`).join('');
    const icon = { LinkedIn: '<b class="li" aria-hidden="true">in</b>', Instagram: '<b class="ig" aria-hidden="true">◎</b>' };
    $('channelList').innerHTML = items.filter(i => icon[i.provider]).map(i =>
      `<div class="channel">${icon[i.provider]} ${esc(i.provider)} <small>${i.connection_id ? 'demo connected' : 'not connected'}</small></div>`).join('');
  } catch (e) { $('integrationList').innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

// ------------------------------------------------------------------ team
async function loadTeam() {
  try {
    state.members = await api('/api/members');
    const me = state.me.user.id;
    $('memberList').innerHTML = state.members.map(m => `
      <div class="automation-rule">
        <div>
          <strong>${esc(m.name)}${m.user_id === me ? ' (you)' : ''}</strong>
          <div class="rule-meta">${esc(m.email)} · ${esc(ROLE_LABEL[m.role] ?? m.role)}</div>
        </div>
        ${isOwner() ? `<div class="automation-actions">
          <button class="secondary" data-action="changeRole" data-id="${m.user_id}">Change role</button>
          <button class="secondary" data-action="removeMember" data-id="${m.user_id}">Remove</button>
        </div>` : ''}
      </div>`).join('');
  } catch (e) { $('memberList').innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

$('addMember').addEventListener('click', async () => {
  const email = $('memberEmail').value.trim();
  if (!email) { toast('Enter the email of a registered user'); return; }
  try {
    await post('/api/members', { email, role: $('memberRole').value });
    $('memberEmail').value = '';
    await loadTeam();
    toast('Member added');
  } catch (e) { toast(e.message); }
});

$('createAutomation').addEventListener('click', async () => {
  const name = $('automationName').value.trim(), topic = $('automationTopic').value.trim();
  if (!name || !topic) { toast('Enter a rule name and topic'); return; }
  try {
    await post('/api/automations', { name, topic_template: topic, platform: $('automationPlatform').value });
    $('automationName').value = '';
    $('automationTopic').value = '';
    await loadAutomationPanels();
    toast('Automation rule created');
  } catch (e) { toast(e.message); }
});

// ------------------------------------------------------------------ navigation highlight
const navLinks = [...document.querySelectorAll('.nav[href^="#"]')];
const setActive = hash => navLinks.forEach(a => a.classList.toggle('active', a.getAttribute('href') === hash));
navLinks.forEach(a => a.addEventListener('click', () => setActive(a.getAttribute('href'))));
if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver(entries => {
    const visible = entries.filter(e => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (visible) setActive(`#${visible.target.id}`);
  }, { rootMargin: '-20% 0px -70% 0px' });
  navLinks.forEach(a => { const target = document.querySelector(a.getAttribute('href')); if (target) observer.observe(target); });
}

// ------------------------------------------------------------------ boot: each panel loads independently
async function loadAll() {
  const results = await Promise.allSettled([
    api('/api/health').then(h => { state.mode = h.mode; renderMode(); }),
    loadCredits(),
    loadRuns(),
    refreshSchedule(),
    refreshDrafts(),
    loadIntegrations(),
    loadTeam(),
  ]);
  const failed = results.find(r => r.status === 'rejected');
  if (failed && state.me) toast('Some data failed to load: ' + failed.reason.message);
}

// Keep the schedule current while the page is open: the server publishes due posts in the background.
setInterval(() => { if (state.me && !document.hidden) refreshPublishing().catch(() => {}); }, 30000);

renderCalendar();
api('/api/me').then(enterApp).catch(() => { showAuth(); setAuthMode(false); });
