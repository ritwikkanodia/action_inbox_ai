const IMPORTANCE_OPTIONS = ['low', 'medium', 'high'];
const STATUS_OPTIONS  = ['open', 'ongoing', 'closed'];
const AI_SOURCES = ['gmail', 'fathom', 'pocket', 'browser_history', 'system'];
const DURABLE_MODE = window.__DURABLE_WORK === true;
const durableStates = new Map();

const todosById = {};
(JSON.parse(document.getElementById('todos-data').textContent) || []).forEach(t => {
  todosById[t.todo_id] = t;
});

marked.setOptions({ breaks: true });

// ---------------- Utilities ----------------
function fmtDue(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString('en-GB', {
    day: 'numeric', month: 'short', year: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function patch(id, field, value) {
  return fetch(`/todos/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [field]: value || null }),
  });
}

function sourceLabel(source) {
  if (source === 'browser_history') return 'browser';
  if (source === 'system') return 'system';
  return source || 'gmail';
}

function isPending(t) {
  return AI_SOURCES.includes(t.source) && !t.decision && t.status !== 'closed';
}

// ---------------- Sidebar state ----------------
const todoList = document.getElementById('todo-list');
const noMatchesEl = document.getElementById('noMatches');
const countEl     = document.getElementById('todoCount');
const appEl = document.getElementById('app');
const detailEmpty   = document.getElementById('detail-empty');
const detailContent = document.getElementById('detail-content');

let selectedId = null;
// The thread the composer on screen belongs to: a todo id in the detail pane,
// or CHAT_ID in the chat view. Separate from `selectedId` because the chat
// selects no todo, and the same run/poll/stop code serves both.
const CHAT_ID = '__chat__';
let activeThreadId = null;
const contextCache = {};
const threadCache  = {};

const STATUS_LABELS = { open: 'Open', ongoing: 'Ongoing', closed: 'Closed', rejected: 'Rejected' };
const SOURCE_LABELS = {
  gmail: 'Gmail', fathom: 'Fathom', pocket: 'Pocket', browser_history: 'Browser',
  system: 'System', user: 'User',
};
const FILTER_STORE_PREFIX = 'inbox.filter.';

// Private-mode browsers throw on both of these, so a failure to remember the
// filter must never take the list down with it.
function readStoredFilter(key) {
  try {
    const raw = localStorage.getItem(FILTER_STORE_PREFIX + key);
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? parsed : null;
  } catch { return null; }
}
function writeStoredFilter(key, values) {
  try {
    localStorage.setItem(FILTER_STORE_PREFIX + key, JSON.stringify([...values]));
  } catch { /* not worth surfacing */ }
}

// Wires up one checkbox dropdown and returns the live Set of chosen values.
function setupMultiFilter({ id, storageKey, labels, plural, onChange }) {
  const root  = document.getElementById(id);
  const btn   = root.querySelector('.filter-btn');
  const label = root.querySelector('.filter-label');
  const boxes = Array.from(root.querySelectorAll('input[type="checkbox"]'));

  // Drop remembered values whose option no longer exists, but keep a
  // deliberately empty selection — that is a choice, not missing data.
  const known  = new Set(boxes.map(b => b.value));
  const stored = readStoredFilter(storageKey);
  const initial = stored ? stored.filter(v => known.has(v))
                         : boxes.filter(b => b.checked).map(b => b.value);
  const selected = new Set(initial);
  boxes.forEach(b => { b.checked = selected.has(b.value); });

  function updateLabel() {
    const picked = [...selected];
    label.textContent =
      picked.length === 0 ? `No ${plural}`
      : picked.length === 1 ? labels[picked[0]]
      : picked.length === boxes.length ? `All ${plural}`
      : `${picked.length} ${plural}`;
  }
  function setOpen(open) {
    root.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  boxes.forEach(box => box.addEventListener('change', () => {
    if (box.checked) selected.add(box.value);
    else selected.delete(box.value);
    writeStoredFilter(storageKey, selected);
    updateLabel();
    onChange();
  }));
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(!root.classList.contains('open'));
  });
  document.addEventListener('click', (e) => { if (!root.contains(e.target)) setOpen(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setOpen(false); });

  updateLabel();
  return selected;
}

// Rejection is exclusive: a dismissed suggestion lives under Rejected only, so
// the default view stays clear of things you already said no to.
function statusBucket(r) {
  if (r.classList.contains('rejected-todo')) return 'rejected';
  if (r.classList.contains('closed'))  return 'closed';
  if (r.classList.contains('ongoing')) return 'ongoing';
  return 'open';
}

function matchesFilters(r) {
  return selectedStatuses.has(statusBucket(r))
      && selectedSources.has(r.dataset.source || 'user');
}

// pruneSelection only when a filter itself moved — a todo you close while
// reading it should leave the list without yanking the detail pane away.
function applyFilter({ pruneSelection = false } = {}) {
  const rows = Array.from(document.querySelectorAll('#todo-list .todo-row'));
  let shown = 0;
  rows.forEach(r => {
    const show = matchesFilters(r);
    r.classList.toggle('filtered-out', !show);
    if (show) shown++;
  });
  countEl.textContent = shown;
  if (noMatchesEl) noMatchesEl.hidden = !(rows.length && shown === 0);

  if (pruneSelection && selectedId) {
    const row = document.querySelector(`.todo-row[data-id="${selectedId}"]`);
    if (row && row.classList.contains('filtered-out')) clearSelection();
  }
}

const selectedStatuses = setupMultiFilter({
  id: 'statusFilter', storageKey: 'statuses', labels: STATUS_LABELS,
  plural: 'statuses', onChange: () => applyFilter({ pruneSelection: true }),
});
const selectedSources = setupMultiFilter({
  id: 'sourceFilter', storageKey: 'sources', labels: SOURCE_LABELS,
  plural: 'sources', onChange: () => applyFilter({ pruneSelection: true }),
});

applyFilter();

// ---------------- Selection / detail rendering ----------------
function clearSelection() {
  selectedId = null;
  activeThreadId = null;
  stopPolling();
  document.querySelectorAll('.todo-row.selected').forEach(r => r.classList.remove('selected'));
  detailEmpty.classList.remove('hidden');
  detailContent.classList.add('hidden');
  detailContent.innerHTML = '';
  appEl.classList.remove('detail-open');
  if (location.hash && location.pathname === '/') history.replaceState(null, '', location.pathname);
}

document.getElementById('backBtn').addEventListener('click', () => {
  appEl.classList.remove('detail-open');
  localStorage.setItem('viewMode', 'list');
  applyViewMode('list');
});

function selectTodo(id) {
  const t = todosById[id];
  if (!t) return;
  selectedId = id;
  document.querySelectorAll('.todo-row.selected').forEach(r => r.classList.remove('selected'));
  const row = document.querySelector(`.todo-row[data-id="${id}"]`);
  if (row) row.classList.add('selected');

  detailEmpty.classList.add('hidden');
  detailContent.classList.remove('hidden');
  renderDetail(t);
  appEl.classList.add('detail-open');

  if (location.pathname === '/' && location.hash !== `#todo/${id}`) {
    history.replaceState(null, '', `#todo/${id}`);
  }
}

function bindRowClick(row) {
  row.addEventListener('click', (e) => {
    if (e.target.closest('.row-inline-actions')) return;
    if (e.target.closest('.row-title-input')) return;

    if (e.target.closest('.row-edit-title-btn')) {
      startRowTitleEdit(row, todosById[row.dataset.id]);
      return;
    }

    if (e.target.closest('.row-title-text')) {
      const link = row.dataset.link;
      if (link) { window.open(link, '_blank', 'noopener'); return; }
    }

    if (appEl.classList.contains('list-only')) {
      localStorage.setItem('viewMode', 'split');
      applyViewMode('split');
    }
    selectTodo(row.dataset.id);
  });
}
document.querySelectorAll('.todo-row').forEach(bindRowClick);

// ---------------- View mode toggle ----------------
const viewToggleBtn = document.getElementById('viewToggleBtn');
function applyViewMode(mode) {
  if (mode === 'list') {
    appEl.classList.add('list-only');
    viewToggleBtn.textContent = 'Split';
    viewToggleBtn.title = 'Switch to split view';
  } else {
    appEl.classList.remove('list-only');
    viewToggleBtn.textContent = 'List';
    viewToggleBtn.title = 'Switch to list-only view';
  }
}
if (window.__FRESH_SIGNUP) {
  localStorage.setItem('viewMode', 'split');
  applyViewMode('split');
} else {
  applyViewMode(localStorage.getItem('viewMode') === 'list' ? 'list' : 'split');
}
viewToggleBtn.addEventListener('click', () => {
  const next = appEl.classList.contains('list-only') ? 'split' : 'list';
  localStorage.setItem('viewMode', next);
  applyViewMode(next);
});

// ---------------- Unified field-edit handling ----------------
// Apply a committed field change to all DOM that displays it: sidebar row,
// inline action pill, and detail-pane cells. No full re-render.
function applyFieldChange(t, field, newVal) {
  t[field] = newVal || null;

  const row = document.querySelector(`.todo-row[data-id="${t.todo_id}"]`);
  if (row) {
    if (field === 'status') {
      row.classList.remove('open', 'ongoing', 'closed');
      if (newVal) row.classList.add(newVal);
    } else if (field === 'importance') {
      row.dataset.importance = newVal || 'none';
    } else if (field === 'due_date') {
      let dueEl = row.querySelector('.row-due');
      if (newVal) {
        if (!dueEl) {
          dueEl = document.createElement('span');
          dueEl.className = 'row-due';
          row.querySelector('.row-meta').insertBefore(dueEl, row.querySelector('.row-meta').firstChild);
        }
        dueEl.textContent = fmtDue(newVal);
      } else if (dueEl) {
        dueEl.remove();
      }
    }

    const pill = row.querySelector(`.row-inline-actions .editable[data-field="${field}"]`);
    if (pill) {
      pill.dataset.value = newVal || '';
      pill.textContent = field === 'due_date'
        ? (newVal ? fmtDue(newVal) : 'set due')
        : (newVal || '—');
    }
  }

  if (selectedId === t.todo_id) {
    const dCell = detailContent.querySelector(`.editable[data-field="${field}"]`);
    if (dCell) {
      dCell.dataset.value = newVal || '';
      if (field === 'status') {
        dCell.innerHTML = `<span class="status ${newVal || ''}">${newVal || '—'}</span>`;
      } else if (field === 'importance') {
        dCell.className = `editable importance ${newVal || ''}`;
        dCell.textContent = newVal || '—';
      } else if (field === 'due_date') {
        dCell.textContent = fmtDue(newVal);
      }
    }
  }

  applyFilter();
}

function bindEditable(cell, todo) {
  cell.addEventListener('click', () => {
    if (cell.querySelector('select,input')) return;
    const field = cell.dataset.field;
    const value = cell.dataset.value;

    function commit(newVal) {
      patch(todo.todo_id, field, newVal).then(r => {
        if (!r.ok) { alert('Save failed'); return; }
        applyFieldChange(todo, field, newVal);
      });
    }

    let widget;
    if (field === 'importance')      widget = makeSelect(IMPORTANCE_OPTIONS, value, commit);
    else if (field === 'status')  widget = makeSelect(STATUS_OPTIONS, value, commit);
    else                          widget = makeDateInput(value, commit);

    cell.innerHTML = '';
    cell.appendChild(widget);
    widget.focus();
  });
}

function applyDecision(t, decision) {
  const wasRejected = t.decision === 'rejected';
  t.decision = decision;
  // The server closes a rejected todo and reopens one accepted out of
  // rejection (db.update_todo_fields); mirror that so the status pill and
  // the row class agree with what a reload would show.
  if (decision === 'rejected' && t.status !== 'closed') applyFieldChange(t, 'status', 'closed');
  else if (decision === 'accepted' && wasRejected && t.status === 'closed') applyFieldChange(t, 'status', 'open');
  const row = document.querySelector(`.todo-row[data-id="${t.todo_id}"]`);
  if (row) {
    row.classList.remove('pending-decision', 'rejected-todo');
    if (decision === 'rejected') row.classList.add('rejected-todo');
    const label = row.querySelector('.suggestion-label');
    if (label) label.remove();
    row.querySelectorAll('.row-inline-actions .accept, .row-inline-actions .reject').forEach(b => b.remove());
  }
  if (selectedId === t.todo_id) renderDetail(t); // decision changes structure; re-render is appropriate here
  applyFilter();
}

function sendDecision(t, decision) {
  fetch(`/todos/${t.todo_id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  }).then(r => { if (r.ok) applyDecision(t, decision); });
}

// Wire row-inline action strips
function bindInlineActions(strip) {
  const id = strip.dataset.id;
  strip.addEventListener('click', e => e.stopPropagation());
  strip.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = todosById[id];
      if (!t) return;
      sendDecision(t, btn.dataset.action === 'accept' ? 'accepted' : 'rejected');
    });
  });
  strip.querySelectorAll('.editable').forEach(cell => bindEditable(cell, todosById[id]));
}
document.querySelectorAll('.row-inline-actions').forEach(bindInlineActions);

// ---------------- Detail rendering ----------------
function renderDetail(t) {
  const pending = isPending(t);
  const rejected = t.decision === 'rejected';

  let decisionHtml = '';
  if (pending) {
    decisionHtml = `
      <button class="accept-btn" data-action="accept">Accept</button>
      <button class="reject-btn" data-action="reject">Reject</button>`;
  } else if (rejected) {
    decisionHtml = `<button class="accept-btn" data-action="accept">Accept</button>`;
  }

  detailContent.innerHTML = `
    <div class="detail-header">
      <div class="detail-title-row">
        <div class="detail-title" id="detailTitle">${escapeHtml(t.title || '(untitled)')}</div>
        <button class="detail-edit-title-btn" id="detailEditTitleBtn" title="Edit title">✎</button>
      </div>

      <dl class="detail-meta-grid">
        <dt>Source</dt>
        <dd><span class="source-badge ${t.source || 'user'}">${sourceLabel(t.source)}</span></dd>

        <dt>Status</dt>
        <dd><span class="editable" data-field="status" data-value="${t.status || ''}"><span class="status ${t.status || ''}">${t.status || '—'}</span></span></dd>

        <dt>Importance</dt>
        <dd><span class="editable importance ${t.importance || ''}" data-field="importance" data-value="${t.importance || ''}">${t.importance || '—'}</span></dd>

        <dt>Due</dt>
        <dd><span class="editable" data-field="due_date" data-value="${t.due_date || ''}">${fmtDue(t.due_date)}</span></dd>

        <dt>Created</dt>
        <dd>${fmtDue(t.created_at)}</dd>

        ${t.estimated_time_minutes ? `<dt>Estimate</dt><dd>${t.estimated_time_minutes} min</dd>` : ''}
        ${t.relevant_link ? `<dt>Link</dt><dd><a href="${escapeHtml(t.relevant_link)}" target="_blank" class="context-link">Open ↗</a></dd>` : ''}
      </dl>

      ${decisionHtml ? `<div class="detail-actions">${decisionHtml}</div>` : ''}
    </div>

    ${t.suggested_action ? `<div class="detail-section"><h3>Suggested action</h3><div class="body-text">${escapeHtml(t.suggested_action)}</div></div>` : ''}
    ${t.reasoning ? `<div class="detail-section is-reasoning"><h3>Why</h3><div class="body-text">${escapeHtml(t.reasoning)}</div></div>` : ''}

    <div class="detail-section hidden" id="context-section">
      <h3 id="context-heading">Context</h3>
      <div id="context-body"></div>
    </div>

    <div class="ai-section">
      <div class="ai-section-header">
        <h3>Ways to close this</h3>
        <button id="ai-regen-btn" title="Re-infer the options">Regenerate</button>
      </div>
      <div id="ai-actions"></div>

      <div class="ai-section-header ai-thread-header">
        <h3>Execution</h3>
        <button id="ai-new-thread-btn">New thread</button>
      </div>
      <div id="ai-thread"></div>
      <div id="ai-input-area">
        <textarea id="ai-followup" placeholder="Or type your own instruction…" rows="1"></textarea>
        <button id="ai-send-btn">Send</button>
        <button id="ai-stop-btn" class="hidden">Stop</button>
      </div>
    </div>
  `;

  wireDetailHandlers(t);
  loadContext(t);
  loadAiThread(t);
  loadActions(t);
}

function wireDetailHandlers(t) {
  detailContent.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      sendDecision(t, btn.dataset.action === 'accept' ? 'accepted' : 'rejected');
    });
  });

  detailContent.querySelectorAll('.editable').forEach(cell => bindEditable(cell, t));

  const editBtn = document.getElementById('detailEditTitleBtn');
  if (editBtn) editBtn.addEventListener('click', () => startTitleEdit(t));

  bindComposer(t.todo_id);
  document.getElementById('ai-regen-btn').addEventListener('click', () => loadActions(t, true));

  document.getElementById('ai-new-thread-btn').addEventListener('click', () => {
    if (DURABLE_MODE) { resetDurable(t.todo_id); return; }
    fetch(aiUrl(t.todo_id, '/reset-thread'), { method: 'POST' }).then(() => {
      delete threadCache[t.todo_id];
      stopPolling();
      setRunning(false);
      document.getElementById('ai-thread').innerHTML = '';
    });
  });
}

// The composer under a thread: Send, Stop, Enter-to-send, autosize. The same
// markup serves a todo's detail pane and the chat view, so it is wired once
// here, against whichever thread is on screen.
function bindComposer(threadId) {
  const sendBtn = document.getElementById('ai-send-btn');
  const followup = document.getElementById('ai-followup');
  const placeholder = followup.placeholder;
  sendBtn.addEventListener('click', () => {
    const msg = followup.value.trim();
    if (!msg) return;
    followup.value = '';
    followup.style.height = 'auto';
    // "Something else…" borrows the placeholder to show which question is
    // being answered; put it back once that answer is on its way.
    followup.placeholder = placeholder;
    callAI(threadId, msg);
  });
  document.getElementById('ai-stop-btn').addEventListener('click', () => stopRun(threadId));
  followup.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendBtn.click(); }
  });
  followup.addEventListener('input', () => {
    followup.style.height = 'auto';
    followup.style.height = Math.min(140, followup.scrollHeight) + 'px';
  });
}

function makeSelect(options, current, onCommit) {
  const sel = document.createElement('select');
  sel.className = 'edit-select';
  options.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o;
    if (o === current) opt.selected = true;
    sel.appendChild(opt);
  });
  let committed = false;
  function fire() { if (!committed) { committed = true; onCommit(sel.value); } }
  sel.addEventListener('change', fire);
  sel.addEventListener('blur', fire);
  return sel;
}

function makeDateInput(current, onCommit) {
  const inp = document.createElement('input');
  inp.type = 'datetime-local';
  inp.className = 'edit-date';
  if (current) inp.value = current.slice(0, 16);
  inp.addEventListener('blur', () => onCommit(inp.value ? new Date(inp.value).toISOString() : ''));
  inp.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === 'Escape') inp.blur(); });
  return inp;
}

function startTitleEdit(t) {
  const titleDiv = document.getElementById('detailTitle');
  if (!titleDiv || titleDiv.classList.contains('editing')) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'detail-title-input';
  input.value = t.title || '';
  titleDiv.classList.add('editing');
  titleDiv.parentNode.insertBefore(input, titleDiv);
  input.focus();
  input.select();

  let done = false;
  function finish(save) {
    if (done) return; done = true;
    const newTitle = input.value.trim();
    if (save && newTitle && newTitle !== t.title) {
      fetch(`/todos/${t.todo_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle }),
      }).then(r => {
        if (r.ok) {
          t.title = newTitle;
          titleDiv.textContent = newTitle;
          const titleSpan = document.querySelector(`.todo-row[data-id="${t.todo_id}"] .row-title-text`);
          if (titleSpan) titleSpan.textContent = newTitle;
        }
      });
    }
    input.remove();
    titleDiv.classList.remove('editing');
  }

  input.addEventListener('blur', () => finish(true));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') finish(false);
  });
}

function startRowTitleEdit(row, t) {
  if (!t) return;
  const titleSpan = row.querySelector('.row-title-text');
  if (!titleSpan || titleSpan.classList.contains('editing')) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'row-title-input';
  input.value = t.title || '';
  titleSpan.classList.add('editing');
  titleSpan.parentNode.insertBefore(input, titleSpan);
  input.focus();
  input.select();

  input.addEventListener('click', e => e.stopPropagation());

  let done = false;
  function finish(save) {
    if (done) return; done = true;
    const newTitle = input.value.trim();
    if (save && newTitle && newTitle !== t.title) {
      fetch(`/todos/${t.todo_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle }),
      }).then(r => {
        if (r.ok) {
          t.title = newTitle;
          titleSpan.textContent = newTitle;
          if (selectedId === t.todo_id) {
            const detailTitle = document.getElementById('detailTitle');
            if (detailTitle) detailTitle.textContent = newTitle;
          }
        }
      });
    }
    input.remove();
    titleSpan.classList.remove('editing');
  }

  input.addEventListener('blur', () => finish(true));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') finish(false);
  });
}

// ---------------- Context loading ----------------
function loadContext(t) {
  const section = document.getElementById('context-section');
  const heading = document.getElementById('context-heading');
  const body = document.getElementById('context-body');

  if (t.source === 'user') {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');

  if (contextCache[t.todo_id]) {
    renderContext(contextCache[t.todo_id], heading, body);
    return;
  }

  body.innerHTML = '<div class="context-loading">Loading…</div>';
  fetch(`/todos/${t.todo_id}/context`)
    .then(r => r.json())
    .then(data => {
      contextCache[t.todo_id] = data;
      if (selectedId !== t.todo_id) return;
      renderContext(data, heading, body);
    })
    .catch(() => {
      if (selectedId !== t.todo_id) return;
      body.innerHTML = '<div class="context-error">Couldn\'t load context.</div>';
    });
}

function renderContext(data, heading, body) {
  if (data.error) {
    heading.textContent = 'Context';
    body.innerHTML = `<div class="context-error">${escapeHtml(data.error)}</div>`;
    return;
  }
  if (data.source === 'gmail') {
    heading.textContent = 'Email thread';
    if (!data.thread || data.thread.length === 0) {
      body.innerHTML = '<div class="context-error">No messages in this thread.</div>';
      return;
    }
    const msgs = data.thread.map(m => {
      const when = fmtDue(m.received_at);
      const sender = m.is_user ? 'You' : escapeHtml(m.from_name || m.from_email || '(unknown)');
      const longBody = (m.body_text || '').length > 600;
      const bodyClass = longBody ? 'thread-msg-body collapsed' : 'thread-msg-body';
      const expandBtn = longBody ? '<button class="thread-msg-expand">Show more</button>' : '';
      return `
        <div class="thread-msg${m.is_user ? ' is-user' : ''}">
          <div class="thread-msg-hdr">
            <span class="thread-msg-from">${sender}</span>
            <span>${when}</span>
          </div>
          <div class="${bodyClass}">${escapeHtml(m.body_text || '')}</div>
          ${expandBtn}
        </div>`;
    }).join('');
    const link = data.thread_url ? `<a href="${escapeHtml(data.thread_url)}" target="_blank" class="context-link">Open in Gmail ↗</a>` : '';
    body.innerHTML = msgs + link;
    body.querySelectorAll('.thread-msg-expand').forEach(btn => {
      btn.addEventListener('click', () => {
        btn.previousElementSibling.classList.remove('collapsed');
        btn.remove();
      });
    });
    return;
  }
  if (data.source === 'fathom') {
    heading.textContent = 'Meeting';
    const assigneeName = data.assignee && data.assignee.name ? data.assignee.name : null;
    body.innerHTML = `
      <div class="body-text">${escapeHtml(data.meeting_title || '(no title)')}</div>
      ${assigneeName ? `<div class="body-text assignee-hint">Assigned to ${escapeHtml(assigneeName)}</div>` : ''}
      ${data.recording_url ? `<a href="${escapeHtml(data.recording_url)}" target="_blank" class="context-link">Open recording ↗</a>` : ''}
    `;
    return;
  }
  if (data.source === 'pocket') {
    heading.textContent = 'Recording';
    body.innerHTML = `
      <div class="body-text">${escapeHtml(data.recording_title || '(no title)')}</div>
      ${data.context ? `<div class="body-text">${escapeHtml(data.context)}</div>` : ''}
      ${data.assignee ? `<div class="body-text assignee-hint">Assigned to ${escapeHtml(data.assignee)}</div>` : ''}
    `;
    return;
  }
  if (data.source === 'browser_history') {
    heading.textContent = 'Page';
    body.innerHTML = `
      ${data.page_title ? `<div class="body-text">${escapeHtml(data.page_title)}</div>` : ''}
      ${data.page_url ? `<a href="${escapeHtml(data.page_url)}" target="_blank" class="context-link">Open page ↗</a>` : ''}
    `;
    return;
  }
  heading.textContent = 'Context';
  body.innerHTML = '<div class="context-error">No additional context.</div>';
}

// ---------------- Suggested actions ----------------
// The three inferred ways to close the item. Picking one hands its instruction
// to the executor as the user message — same path as typing it by hand.
const actionsCache = {};

function renderActions(todoId, actions, error) {
  const el = document.getElementById('ai-actions');
  if (!el || selectedId !== todoId) return;

  if (error) {
    el.innerHTML = `<div class="context-error">Couldn't work out the options — ${escapeHtml(error)}</div>`;
    return;
  }
  if (!actions || !actions.length) {
    el.innerHTML = '<div class="context-error">No options suggested for this item.</div>';
    return;
  }

  el.innerHTML = actions.map((a, i) => `
    <button class="action-option" data-idx="${i}">
      <span class="action-option-num">${i + 1}</span>
      <span class="action-option-text">
        <span class="action-option-label">${escapeHtml(a.label || '')}</span>
        ${a.detail ? `<span class="action-option-detail">${escapeHtml(a.detail)}</span>` : ''}
      </span>
      <span class="action-option-go">Run →</span>
    </button>`).join('');

  el.querySelectorAll('.action-option').forEach(btn => {
    btn.addEventListener('click', () => {
      if (runningTodoId) return;
      const action = actions[Number(btn.dataset.idx)];
      if (!action) return;
      btn.classList.add('is-chosen');
      callAI(todoId, action.instruction, true);
    });
  });
  applyRunningStateToActions();
}

function loadActions(t, refresh) {
  const el = document.getElementById('ai-actions');
  if (!el) return;

  if (!refresh && actionsCache[t.todo_id]) {
    renderActions(t.todo_id, actionsCache[t.todo_id]);
    return;
  }
  el.innerHTML = '<div class="context-loading">Working out how to close this…</div>';
  fetch(`/todos/${t.todo_id}/actions${refresh ? '?refresh=1' : ''}`)
    .then(r => r.json())
    .then(data => {
      if (!data.error) actionsCache[t.todo_id] = data.actions;
      renderActions(t.todo_id, data.actions, data.error);
    })
    .catch(() => renderActions(t.todo_id, null, 'request failed'));
}

// ---------------- Execution ----------------
// A turn runs on the server in the background so it can be stopped mid-flight;
// the page starts it, then polls until it reaches a terminal state.
let runningTodoId = null;
let pollTimer = null;
const POLL_MS = 1500;

function aiUrl(threadId, suffix) {
  return (threadId === CHAT_ID ? '/chat' : `/todos/${threadId}`) + suffix;
}

function renderThread(thread) {
  const threadEl = document.getElementById('ai-thread');
  if (!threadEl) return;
  threadEl.innerHTML = '';
  thread.forEach((msg, i) => {
    if (msg.content) {
      const div = document.createElement('div');
      div.className = `ai-bubble ${msg.role}`;
      if (msg.role === 'assistant') {
        div.innerHTML = marked.parse(msg.content);
        // A link in a reply is usually a handoff — "sign in here, then click
        // continue". It has to open beside the app, not in place of it: the
        // chips the user comes back to click live on this page.
        div.querySelectorAll('a[href]').forEach(a => {
          a.target = '_blank';
          a.rel = 'noopener';
        });
      } else {
        div.textContent = msg.content;
      }
      threadEl.appendChild(div);
    }
    // Only the newest question is still open; earlier ones were answered by
    // the messages below them, so re-offering their chips would invite the
    // user to answer the same question twice.
    if (msg.questions && i === thread.length - 1) {
      threadEl.appendChild(renderQuestions(msg.questions));
    }
  });
  threadEl.scrollTop = threadEl.scrollHeight;
}

// The agent asked for something only the user knows. Rendering it as choices
// rather than prose is the whole point: answering costs one click instead of
// composing a sentence, which is what makes stopping to ask cheap enough that
// the agent can prefer it over guessing.
function renderQuestions(questions) {
  const wrap = document.createElement('div');
  wrap.className = 'ai-questions';

  // Questions in one block are answered together, so a single Send covers them.
  const picked = new Map();
  const send = document.createElement('button');

  questions.forEach((q, qi) => {
    const block = document.createElement('div');
    block.className = 'ai-question';

    if (q.header) {
      const tag = document.createElement('span');
      tag.className = 'ai-question-header';
      tag.textContent = q.header;
      block.appendChild(tag);
    }

    const text = document.createElement('div');
    text.className = 'ai-question-text';
    text.textContent = q.question;
    block.appendChild(text);

    // Some answers have no menu — "paste the sentence you want posted". The
    // agent is allowed to ask those, so they get a field rather than chips,
    // and still travel with the rest under one Send.
    if (!q.options || !q.options.length) {
      const field = document.createElement('textarea');
      field.className = 'ai-question-field';
      field.rows = 2;
      field.placeholder = 'Type your answer…';
      field.addEventListener('input', () => {
        const value = field.value.trim();
        if (value) picked.set(qi, [value]);
        else picked.delete(qi);
        send.disabled = !picked.size;
      });
      block.appendChild(field);
      wrap.appendChild(block);
      return;
    }

    const list = document.createElement('div');
    list.className = 'ai-question-options';

    q.options.forEach(opt => {
      const btn = document.createElement('button');
      btn.className = 'ai-option';
      const label = document.createElement('span');
      label.className = 'ai-option-label';
      label.textContent = opt.label;
      btn.appendChild(label);
      if (opt.detail) {
        const detail = document.createElement('span');
        detail.className = 'ai-option-detail';
        detail.textContent = opt.detail;
        btn.appendChild(detail);
      }

      btn.addEventListener('click', () => {
        if (runningTodoId) return;
        const wasOn = btn.classList.contains('is-picked');
        if (!q.multiSelect) {
          list.querySelectorAll('.ai-option').forEach(b => b.classList.remove('is-picked'));
        }
        btn.classList.toggle('is-picked', !wasOn);

        const chosen = [...list.querySelectorAll('.ai-option.is-picked')]
          .map(b => b.querySelector('.ai-option-label').textContent);
        if (chosen.length) picked.set(qi, chosen);
        else picked.delete(qi);
        send.disabled = !picked.size;
      });
      list.appendChild(btn);
    });

    // The escape hatch, always last: none of the options fit, or the real
    // answer needs a sentence. It hands off to the composer that was already
    // there rather than introducing a second place to type.
    const other = document.createElement('button');
    other.className = 'ai-option ai-option-other';
    other.textContent = 'Something else…';
    other.addEventListener('click', () => {
      const followup = document.getElementById('ai-followup');
      if (!followup || followup.disabled) return;
      followup.placeholder = q.question;
      followup.focus();
    });
    list.appendChild(other);

    block.appendChild(list);
    wrap.appendChild(block);
  });

  send.className = 'ai-question-send';
  send.textContent = 'Send answer';
  send.disabled = true;
  send.addEventListener('click', () => {
    if (runningTodoId || !picked.size) return;
    // Phrased back as a sentence per question, so the agent receives the same
    // shape it would have had the user typed the answer themselves.
    const answer = questions
      .map((q, qi) => picked.has(qi) ? `${q.question} ${picked.get(qi).join('; ')}` : null)
      .filter(Boolean)
      .join('\n');
    wrap.classList.add('is-sent');
    callAI(activeThreadId, answer);
  });
  wrap.appendChild(send);

  return wrap;
}

function addLoadingBubble() {
  const threadEl = document.getElementById('ai-thread');
  if (!threadEl || document.getElementById('ai-loading-bubble')) return;
  const div = document.createElement('div');
  div.className = 'ai-bubble loading';
  div.id = 'ai-loading-bubble';
  const label = document.createElement('div');
  label.className = 'ai-activity-label';
  label.textContent = 'Working…';
  div.appendChild(label);
  // Filled by renderActivity on each poll. Empty until the agent's first tool
  // call, and permanently empty on an executor that reports nothing — so the
  // bubble has to read as "working" on its own, without any steps under it.
  const list = document.createElement('ol');
  list.className = 'ai-activity';
  list.id = 'ai-activity';
  div.appendChild(list);
  threadEl.appendChild(div);
  threadEl.scrollTop = threadEl.scrollHeight;
}

// The steps the agent has taken so far this turn. Re-rendered wholesale rather
// than appended to: a poll can miss a tick, or land after the thread was
// redrawn, and the server's list is always the whole truth.
function renderActivity(activity) {
  const listEl = document.getElementById('ai-activity');
  if (!listEl) return;
  const steps = activity || [];
  if (listEl.childElementCount === steps.length) return;

  const threadEl = document.getElementById('ai-thread');
  const pinned = !threadEl ||
    threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 40;

  listEl.innerHTML = '';
  steps.forEach((step, i) => {
    const li = document.createElement('li');
    // Only the newest step is still in progress; the rest have returned.
    li.className = i === steps.length - 1 ? 'activity-step current' : 'activity-step';
    const tool = document.createElement('span');
    tool.className = 'activity-tool';
    tool.textContent = step.tool || 'working';
    li.appendChild(tool);
    if (step.detail) {
      const detail = document.createElement('span');
      detail.className = 'activity-detail';
      // textContent, not innerHTML: these are tool arguments the agent built
      // out of email content, which is attacker-controlled text.
      detail.textContent = step.detail;
      li.appendChild(detail);
    }
    listEl.appendChild(li);
  });
  // Follow the trace down, unless the user has scrolled up to read something.
  if (pinned && threadEl) threadEl.scrollTop = threadEl.scrollHeight;
}

function removeLoadingBubble() {
  const el = document.getElementById('ai-loading-bubble');
  if (el) el.remove();
}

function applyRunningStateToActions() {
  const running = Boolean(runningTodoId);
  document.querySelectorAll('#ai-actions .action-option').forEach(btn => {
    btn.disabled = running;
    btn.classList.toggle('is-waiting', running && !btn.classList.contains('is-chosen'));
  });
  if (!running) {
    document.querySelectorAll('#ai-actions .is-chosen').forEach(b => b.classList.remove('is-chosen'));
  }
}

// While a run is in flight the composer's Send is swapped for Stop, so the one
// control the user reaches for is always the one that applies.
function setRunning(running, todoId) {
  runningTodoId = running ? todoId : null;
  const sendBtn = document.getElementById('ai-send-btn');
  const stopBtn = document.getElementById('ai-stop-btn');
  const followup = document.getElementById('ai-followup');
  const regen = document.getElementById('ai-regen-btn');
  if (sendBtn) sendBtn.classList.toggle('hidden', running);
  if (stopBtn) {
    stopBtn.classList.toggle('hidden', !running);
    stopBtn.disabled = false;
    stopBtn.textContent = 'Stop';
  }
  if (followup) followup.disabled = running;
  if (regen) regen.disabled = running;
  if (running) addLoadingBubble(); else removeLoadingBubble();
  applyRunningStateToActions();
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

function applyRunState(todoId, data) {
  if (data.thread) {
    // Only completed turns are cached, because only those are persisted: a
    // stop or a failure leaves nothing behind server-side, so keeping its
    // notice around would survive longer than the state it describes.
    if (data.status === 'done' || data.status === 'idle') threadCache[todoId] = data.thread;
    if (activeThreadId === todoId) renderThread(data.thread);
  }
  if (data.status === 'running') {
    if (activeThreadId === todoId) {
      // setRunning first: renderThread above wiped the loading bubble, and
      // renderActivity needs it back before it has anywhere to draw.
      setRunning(true, todoId);
      renderActivity(data.activity);
    }
    schedulePoll(todoId);
    return;
  }
  stopPolling();
  if (activeThreadId === todoId) setRunning(false);
}

function schedulePoll(todoId) {
  stopPolling();
  pollTimer = setTimeout(() => {
    fetch(aiUrl(todoId, '/run'))
      .then(r => r.json())
      .then(data => {
        // The run may have been reset, or the worker restarted, while we waited.
        if (data.status === 'idle') { stopPolling(); if (activeThreadId === todoId) setRunning(false); return; }
        applyRunState(todoId, data);
      })
      .catch(() => schedulePoll(todoId));
  }, POLL_MS);
}

function stopRun(todoId) {
  if (DURABLE_MODE) return;
  const stopBtn = document.getElementById('ai-stop-btn');
  if (stopBtn) { stopBtn.disabled = true; stopBtn.textContent = 'Stopping…'; }
  fetch(aiUrl(todoId, '/run/stop'), { method: 'POST' })
    .then(() => schedulePoll(todoId))
    .catch(() => schedulePoll(todoId));
}

// `fromSuggestion` marks a message that came from clicking one of the inferred
// options rather than being typed. The server frames those differently: the
// user picked a short label, not the generated sentence underneath it.
function callAI(todoId, message, fromSuggestion) {
  if (DURABLE_MODE) return sendDurable(todoId, message, Boolean(fromSuggestion));
  if (message) {
    const threadEl = document.getElementById('ai-thread');
    if (threadEl && activeThreadId === todoId) {
      const userBubble = document.createElement('div');
      userBubble.className = 'ai-bubble user';
      userBubble.textContent = message;
      threadEl.appendChild(userBubble);
    }
    setRunning(true, todoId);
  }
  const body = message ? { message, from_suggestion: Boolean(fromSuggestion) } : {};
  return fetch(aiUrl(todoId, '/ask-ai'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
    .then(r => r.json())
    .then(data => applyRunState(todoId, data))
    .catch(() => {
      stopPolling();
      if (activeThreadId === todoId) {
        setRunning(false);
        addErrorBubble("Couldn't reach the server.");
      }
    });
}

function addErrorBubble(text) {
  const threadEl = document.getElementById('ai-thread');
  if (!threadEl) return;
  const div = document.createElement('div');
  div.className = 'ai-bubble assistant';
  div.textContent = `⚠️ ${text}`;
  threadEl.appendChild(div);
}

function loadAiThread(t) {
  const threadEl = document.getElementById('ai-thread');
  activeThreadId = t.todo_id;
  stopPolling();
  setRunning(false);
  if (DURABLE_MODE) return loadDurable(t.todo_id);

  if (threadCache[t.todo_id] && threadCache[t.todo_id].length > 0) {
    renderThread(threadCache[t.todo_id]);
  } else if (!t.has_ai_thread) {
    threadEl.innerHTML = '<div class="ai-empty-cta">Pick an action above, or type your own instruction below.</div>';
  }

  // Ask the server what it has: either the persisted thread, or a run this
  // page never started — one left behind by a reload, or by another tab.
  fetch(aiUrl(t.todo_id, '/ask-ai'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
    .then(r => r.json())
    .then(data => {
      if (activeThreadId !== t.todo_id) return;
      if (data.thread && data.thread.length) applyRunState(t.todo_id, data);
      else if (data.status === 'running') applyRunState(t.todo_id, data);
    })
    .catch(() => {});
}

// ---------------- New-todo form ----------------
const openFormBtn  = document.getElementById('openFormBtn');
const newTodoForm  = document.getElementById('newTodoForm');
const cancelBtn    = document.getElementById('cancelNewTodo');
const saveBtnNew   = document.getElementById('saveNewTodo');

function resetForm() {
  document.getElementById('ntTitle').value = '';
  document.getElementById('ntImportance').value = 'medium';
  document.getElementById('ntDueDate').value = '';
  document.getElementById('ntAction').value = '';
  newTodoForm.classList.remove('open');
}

openFormBtn.addEventListener('click', () => {
  newTodoForm.classList.toggle('open');
  if (newTodoForm.classList.contains('open')) document.getElementById('ntTitle').focus();
});
cancelBtn.addEventListener('click', resetForm);

function buildRow(t) {
  const row = document.createElement('div');
  row.className = `todo-row ${t.status || 'open'}`;
  row.dataset.id = t.todo_id;
  row.dataset.importance = t.importance || 'none';
  row.dataset.source = t.source || 'user';
  if (t.relevant_link) row.dataset.link = t.relevant_link;
  row.innerHTML = `
    <div class="row-body">
      <div class="row-title">
        <span class="row-title-inner">
          <span class="row-title-text">${escapeHtml(t.title || '(untitled)')}</span>
          <button class="row-edit-title-btn" title="Edit title">✎</button>
        </span>
      </div>
      <div class="row-meta">
        ${t.due_date ? `<span class="row-due">${fmtDue(t.due_date)}</span>` : ''}
        <span class="source-badge ${t.source || 'user'}">${sourceLabel(t.source)}</span>
      </div>
    </div>
    <div class="row-inline-actions" data-id="${t.todo_id}">
      <span class="row-source" data-source="${t.source || 'user'}">${sourceLabel(t.source)}</span>
      <span class="row-act editable" data-field="importance" data-value="${t.importance || ''}" title="Importance">${t.importance || '—'}</span>
      <span class="row-act editable" data-field="status" data-value="${t.status || ''}" title="Status">${t.status || '—'}</span>
      <span class="row-act editable" data-field="due_date" data-value="${t.due_date || ''}" title="Due date">${t.due_date ? fmtDue(t.due_date) : 'set due'}</span>
    </div>
  `;
  return row;
}

saveBtnNew.addEventListener('click', () => {
  const title = document.getElementById('ntTitle').value.trim();
  if (!title) { document.getElementById('ntTitle').focus(); return; }
  const importance = document.getElementById('ntImportance').value;
  const dueDateRaw = document.getElementById('ntDueDate').value;
  const due_date = dueDateRaw ? new Date(dueDateRaw).toISOString() : null;
  const suggested_action = document.getElementById('ntAction').value.trim();
  fetch('/todos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, importance, due_date, suggested_action }),
  }).then(r => r.json().then(data => ({ ok: r.ok, data }))).then(({ ok, data }) => {
    if (!ok || !data.todo) { alert('Failed to save todo'); return; }
    const t = data.todo;
    todosById[t.todo_id] = t;
    const row = buildRow(t);
    bindRowClick(row);
    bindInlineActions(row.querySelector('.row-inline-actions'));
    const empty = todoList.querySelector('.empty');
    if (empty) empty.remove();
    todoList.insertBefore(row, todoList.firstChild);
    resetForm();
    applyFilter();
    selectTodo(t.todo_id);
  });
});

document.getElementById('ntTitle').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveBtnNew.click();
  if (e.key === 'Escape') resetForm();
});

// ---------------- Init ----------------
// The status filter applies itself once on load, where it is defined.
(function initSelection() {
  const m = location.hash.match(/^#todo\/([^/]+)$/);
  if (m && todosById[m[1]]) { selectTodo(m[1]); return; }
  const first = document.querySelector('#todo-list .todo-row:not(.closed):not(.rejected-todo)');
  if (first) selectTodo(first.dataset.id);
})();

// ---------------- Settings view ----------------
// Settings is a view at /settings inside the same shell, not an overlay. The
// server renders the same template for both paths and the frontend swaps
// views with pushState, so Back is instant and a reload lands where you were.
// The header link and the Back link are real anchors: without JS they still
// work as full navigations.
const settingsView = document.getElementById('settings-view');
const SETTINGS_PATH = '/settings';

function inSettingsView() {
  return document.body.classList.contains('view-settings');
}

function setSourceConnected(prefix, on, hint) {
  const status = document.getElementById(`${prefix}-status`);
  status.textContent = on ? 'Connected' : 'Not connected';
  status.className = `source-connected-badge ${on ? 'on' : 'off'}`;
  const inputRow = document.getElementById(`${prefix}-input-row`) || document.getElementById(`${prefix}-connect-row`);
  const connectedRow = document.getElementById(`${prefix}-connected-row`);
  if (inputRow) inputRow.style.display = on ? 'none' : (inputRow.id.endsWith('connect-row') ? 'block' : 'flex');
  if (connectedRow) connectedRow.style.display = on ? 'flex' : 'none';
  if (on && hint) {
    const hintEl = document.getElementById(`${prefix}-key-preview`) || document.getElementById(`${prefix}-email-hint`);
    if (hintEl) hintEl.textContent = hint;
  }
  if (!on && (prefix === 'fathom' || prefix === 'pocket')) {
    document.getElementById(`${prefix}-key-input`).value = '';
  }
}

function renderGmailAccounts(accounts) {
  const container = document.getElementById('gmail-accounts');
  const status = document.getElementById('gmail-status');
  const connectBtn = document.getElementById('gmail-connect-btn');
  const count = accounts.length;

  status.textContent = count
    ? `${count} account${count === 1 ? '' : 's'}`
    : 'Not connected';
  status.className = `source-connected-badge ${count ? 'on' : 'off'}`;
  connectBtn.textContent = count ? 'Connect another account' : 'Connect with Google';

  const grantTemplate = (window.__settings && window.__settings.gmailGrantUrlTemplate) || '/settings/sources/gmail/auth?login_hint=';
  container.innerHTML = accounts.map(a => `
    <div class="gmail-account-row" data-email="${escapeHtml(a.email)}">
      <span class="gmail-account-email">${escapeHtml(a.email)}</span>
      <span class="gmail-agent-access ${a.agent_access ? 'on' : 'off'}">
        ${a.agent_access
          ? 'Agent access: granted'
          : `Agent access not granted · <a href="${grantTemplate}${encodeURIComponent(a.email)}">Grant</a>`}
      </span>
      <button class="btn-disconnect gmail-account-disconnect">Disconnect</button>
    </div>`).join('');

  container.querySelectorAll('.gmail-account-disconnect').forEach(btn => {
    btn.addEventListener('click', () => {
      const email = btn.closest('.gmail-account-row').dataset.email;
      if (!confirm(`Disconnect ${email}? Existing todos from this account are kept.`)) return;
      btn.disabled = true;
      fetch('/settings/sources/gmail', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disconnect: true, account_id: email }),
      })
        .then(r => r.json())
        .then(data => { if (data.ok) renderGmailAccounts(data.accounts); })
        .finally(() => { btn.disabled = false; });
    });
  });
}

// Which executor resolves this user's todos. Options come from the server
// with a readiness verdict each — one that can't run here is shown disabled
// with the reason, never selectable, so a saved choice always works.
function renderExecutorCard(executor) {
  const container = document.getElementById('executor-options');
  const status = document.getElementById('executor-status');
  const errorEl = document.getElementById('executor-error');
  const options = executor.options || [];
  const selected = options.find(o => o.name === executor.selected);
  errorEl.hidden = true;
  status.textContent = selected ? selected.label : 'Not set';
  status.className = `source-connected-badge ${selected && selected.ready ? 'on' : 'off'}`;

  container.innerHTML = options.map(o => `
    <label class="executor-option ${o.ready ? '' : 'unavailable'} ${o.name === executor.selected ? 'selected' : ''}">
      <input type="radio" name="executor" value="${escapeHtml(o.name)}"
        ${o.name === executor.selected ? 'checked' : ''} ${o.ready ? '' : 'disabled'}>
      <span class="executor-option-text">
        <span class="executor-option-label">${escapeHtml(o.label)}${o.recommended ? ' <span class="executor-tag">Recommended</span>' : ''}</span>
        <span class="executor-option-desc">${escapeHtml(o.description)}</span>
        ${o.ready ? '' : `<span class="executor-option-reason">Not available: ${escapeHtml(o.reason || '')}</span>`}
      </span>
    </label>`).join('');

  container.querySelectorAll('input[name="executor"]').forEach(input => {
    input.addEventListener('change', () => {
      if (!input.checked) return;
      container.querySelectorAll('input[name="executor"]').forEach(i => { i.disabled = true; });
      fetch('/settings/executor', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ executor: input.value }),
      })
        .then(r => r.json())
        .then(data => {
          if (data.ok) { renderExecutorCard(data.executor); return; }
          renderExecutorCard(executor);
          errorEl.textContent = data.error || 'Could not switch agent';
          errorEl.hidden = false;
        })
        .catch(() => {
          renderExecutorCard(executor);
          errorEl.textContent = 'Could not switch agent';
          errorEl.hidden = false;
        });
    });
  });
}

// Per-user pause for a discovery source. The poller re-reads the flag every
// cycle, so flipping it here takes effect on the next poll. Connections are
// untouched: a paused source keeps its accounts and keys.
function setSourceToggle(source, enabled) {
  const card = document.querySelector(`.source-card[data-source="${source}"], .source-subcard[data-source="${source}"]`);
  if (!card) return;
  const input = card.querySelector('.source-toggle-input');
  const label = card.querySelector('.source-toggle-label');
  if (input) input.checked = enabled;
  if (label) label.textContent = enabled ? 'On' : 'Paused';
  card.classList.toggle('paused', !enabled);
}

function bindSourceToggle(input) {
  input.addEventListener('change', () => {
    const source = input.dataset.source;
    const enabled = input.checked;
    input.disabled = true;
    fetch(`/settings/sources/${encodeURIComponent(source)}/enabled`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
      .then(r => r.json())
      .then(data => { setSourceToggle(source, data.ok ? data.enabled : !enabled); })
      .catch(() => setSourceToggle(source, !enabled))
      .finally(() => { input.disabled = false; });
  });
}

// The opt-in macOS sources have nothing to connect, so their cards are just
// a description and the pause toggle, and exist only when the server runs them.
function renderExtraSources(extra) {
  const container = document.getElementById('extra-sources');
  container.innerHTML = (extra || []).map(src => `
    <div class="source-card" data-source="${escapeHtml(src.name)}">
      <div class="source-card-header">
        <span class="source-card-name">${escapeHtml(src.label)}</span>
        <span class="source-connected-badge on">Local</span>
        <label class="source-toggle" title="Discover todos from ${escapeHtml(src.label)}">
          <input type="checkbox" class="source-toggle-input" data-source="${escapeHtml(src.name)}">
          <span class="source-toggle-track"></span>
          <span class="source-toggle-label">On</span>
        </label>
      </div>
      <div class="source-card-body">
        <span class="source-card-hint">${escapeHtml(src.description)}</span>
      </div>
    </div>`).join('');
  container.querySelectorAll('.source-toggle-input').forEach(bindSourceToggle);
  (extra || []).forEach(src => setSourceToggle(src.name, src.enabled));
}

function loadSettings() {
  fetch('/settings.json').then(r => r.json()).then(data => {
    const { fathom, pocket, gmail, extra } = data.sources;
    setSourceConnected('fathom', fathom.connected, fathom.api_key_preview);
    setSourceConnected('pocket', pocket.connected, pocket.api_key_preview);
    window.__settings = { gmailGrantUrlTemplate: gmail.grant_url_template };
    renderGmailAccounts(gmail.accounts || []);
    setSourceToggle('gmail', gmail.enabled !== false);
    setSourceToggle('fathom', fathom.enabled !== false);
    setSourceToggle('pocket', pocket.enabled !== false);
    renderExtraSources(extra || []);
    renderExecutorCard(data.executor || { selected: null, default: null, options: [] });
    renderPushCard(data.notifications || { configured: false, subscription_count: 0 });
    renderWhatsappCard(data.whatsapp || { configured: false });
  });
}

function showSettingsView({ push } = { push: true }) {
  hideChatView();
  document.body.classList.add('view-settings');
  settingsView.hidden = false;
  if (push && location.pathname !== SETTINGS_PATH) {
    history.pushState({ view: 'settings' }, '', SETTINGS_PATH);
  }
  window.scrollTo(0, 0);
  settingsView.scrollTop = 0;
  loadSettings();
}

function showInboxView({ push } = { push: true }) {
  hideChatView();
  document.body.classList.remove('view-settings');
  settingsView.hidden = true;
  if (push && location.pathname !== '/') {
    history.pushState({ view: 'inbox' }, '', selectedId ? `/#todo/${selectedId}` : '/');
  }
}

document.getElementById('openSettingsBtn').addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // let "open in new tab" through
  e.preventDefault();
  showSettingsView();
});
document.getElementById('settingsBackBtn').addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  showInboxView();
});
window.addEventListener('popstate', () => {
  if (location.pathname === SETTINGS_PATH) showSettingsView({ push: false });
  else if (location.pathname === CHAT_PATH) showChatView({ push: false });
  else showInboxView({ push: false });
});
document.querySelectorAll('#gmail-card .source-toggle-input, #fathom-card .source-toggle-input, #pocket-card .source-toggle-input')
  .forEach(bindSourceToggle);
// ---------------- Chat view ----------------
// The executor with no todo in front of it. One conversation per user, kept
// server-side; "New chat" clears it. The thread, composer, live trace and
// clarifying-question chips are the detail pane's code, pointed at /chat by
// `aiUrl`, so the view only has to build the markup and load the thread.
const chatView = document.getElementById('chat-view');
const chatBody = document.getElementById('chat-body');
const CHAT_PATH = '/chat';

function renderChatShell() {
  chatBody.innerHTML = `
    <div id="ai-thread"></div>
    <div id="ai-input-area">
      <textarea id="ai-followup" placeholder="Ask your agent to do something…" rows="1"></textarea>
      <button id="ai-send-btn">Send</button>
      <button id="ai-stop-btn" class="hidden">Stop</button>
    </div>`;
  bindComposer(CHAT_ID);
}

function loadChatThread() {
  activeThreadId = CHAT_ID;
  stopPolling();
  setRunning(false);
  if (DURABLE_MODE) return loadDurable(CHAT_ID);
  const threadEl = document.getElementById('ai-thread');
  if (threadCache[CHAT_ID] && threadCache[CHAT_ID].length) {
    renderThread(threadCache[CHAT_ID]);
  } else {
    threadEl.innerHTML = '<div class="ai-empty-cta">Ask for anything — the agent has the same tools it uses to resolve todos.</div>';
  }
  // Same as a todo: the server has either the saved thread or a run this page
  // never started (a reload mid-turn, another tab).
  fetch('/chat/ask-ai', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
    .then(r => r.json())
    .then(data => {
      if (activeThreadId !== CHAT_ID) return;
      if ((data.thread && data.thread.length) || data.status === 'running') applyRunState(CHAT_ID, data);
    })
    .catch(() => {});
}

function showChatView({ push } = { push: true }) {
  // The detail pane's composer uses the same ids; only one may exist at a time.
  clearSelection();
  document.body.classList.remove('view-settings');
  settingsView.hidden = true;
  document.body.classList.add('view-chat');
  chatView.hidden = false;
  if (push && location.pathname !== CHAT_PATH) {
    history.pushState({ view: 'chat' }, '', CHAT_PATH);
  }
  window.scrollTo(0, 0);
  renderChatShell();
  loadChatThread();
  document.getElementById('ai-followup').focus();
}

function hideChatView() {
  if (!document.body.classList.contains('view-chat')) return;
  document.body.classList.remove('view-chat');
  chatView.hidden = true;
  // A run in flight keeps going server-side; reopening the view picks it up.
  stopPolling();
  activeThreadId = null;
  chatBody.innerHTML = '';
}

document.getElementById('openChatBtn').addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  showChatView();
});
document.getElementById('chatBackBtn').addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  showInboxView();
});
document.getElementById('chat-new-btn').addEventListener('click', () => {
  if (DURABLE_MODE) { resetDurable(CHAT_ID); return; }
  const btn = document.getElementById('chat-new-btn');
  btn.disabled = true;
  fetch('/chat/reset-thread', { method: 'POST' })
    .then(() => {
      delete threadCache[CHAT_ID];
      renderChatShell();
      loadChatThread();
    })
    .finally(() => { btn.disabled = false; });
});

// Durable mode is explicitly opt-in; none of these functions touch legacy runs.
function durableState(threadId) {
  if (!durableStates.has(threadId)) durableStates.set(threadId, { snapshot: null, pending: new Map(), serial: 0 });
  return durableStates.get(threadId);
}

function durableUrl(threadId, suffix = '') {
  const cid = durableConversationId(threadId);
  if (!cid) throw new Error('Conversation is not configured.');
  return `/api/work/conversations/${encodeURIComponent(cid)}${suffix}`;
}

function durableConversationId(threadId) {
  return window.__DURABLE_CONVERSATIONS[threadId === CHAT_ID ? 'chat' : threadId];
}

async function durableRequest(url, body) {
  const response = await fetch(url, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || 'Request failed.');
    error.status = response.status;
    error.code = result.error;
    throw error;
  }
  return result;
}

function renderDurable(threadId) {
  if (activeThreadId !== threadId) return;
  const state = durableState(threadId), target = document.getElementById('ai-thread');
  if (!target || !state.snapshot) return;
  target.replaceChildren();
  const data = state.snapshot;
  const bubble = (message) => {
    const element = document.createElement('div');
    element.className = `ai-bubble ${message.role}`;
    element.textContent = message.content; // Durable output is text, never executable HTML.
    if (message.job_id) element.dataset.jobId = message.job_id;
    target.appendChild(element);
    return element;
  };
  for (const message of data.messages) {
    if (message.role === 'assistant' && message.job_id) continue;
    const element = bubble(message);
    if (message.role === 'user') {
      const job = data.jobs.find(j => j.job_id === message.job_id);
      if (job) {
        const status = document.createElement('div');
        status.dataset.workState = job.state;
        const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(job.created_at)) / 1000));
        status.textContent = `${job.state.replaceAll('_', ' ')} · ${seconds}s${job.reason ? ' · ' + job.reason.replaceAll('_', ' ') : ''}`;
        element.appendChild(status);
        if (DurableWork.shouldPoll(job.state)) {
          const cancel = document.createElement('button');
          cancel.type = 'button'; cancel.textContent = 'Stop this request'; cancel.dataset.cancelJob = job.job_id;
          cancel.onclick = () => cancelDurable(threadId, job.job_id);
          element.appendChild(cancel);
        }
      }
      for (const reply of data.messages.filter(m => m.role === 'assistant' && m.job_id === message.job_id)) bubble(reply);
    }
  }
  if (data.reconciliation_hold) bubble({ role: 'notice', content: 'An earlier action needs review. Later requests will wait; stopping does not undo an action.' });
  for (const [key, pending] of state.pending) {
    const element = bubble({ role: 'notice', content: pending.failed ? 'Delivery is uncertain. Retry the same request safely.' : 'Saving request…' });
    if (pending.failed) {
      const retry = document.createElement('button');
      retry.type = 'button'; retry.textContent = 'Retry same request'; retry.dataset.retrySubmission = key;
      retry.onclick = () => postDurable(threadId, pending.submission);
      element.appendChild(retry);
    }
  }
  document.getElementById('ai-send-btn')?.classList.remove('hidden');
  document.getElementById('ai-stop-btn')?.classList.add('hidden');
  const input = document.getElementById('ai-followup');
  if (input) input.disabled = false;
}

async function loadDurable(threadId) {
  const state = durableState(threadId), serial = ++state.serial;
  const input = document.getElementById('ai-followup');
  if (!state.snapshot && activeThreadId === threadId && input) input.disabled = true;
  try {
    const snapshot = await durableRequest(durableUrl(threadId));
    if (serial !== state.serial || (state.snapshot && snapshot.generation < state.snapshot.generation)) return;
    if (state.snapshot && snapshot.generation !== state.snapshot.generation) {
      state.pending.clear();
      if (activeThreadId === threadId && input) input.value = '';
    }
    state.snapshot = snapshot;
    renderDurable(threadId);
    if (activeThreadId === threadId) {
      stopPolling();
      if (snapshot.jobs.some(j => DurableWork.shouldPoll(j.state))) pollTimer = setTimeout(() => loadDurable(threadId), POLL_MS);
    }
  } catch (_) {
    if (activeThreadId === threadId && serial === state.serial) {
      renderDurable(threadId);
      const active = state.snapshot?.jobs.some(j => DurableWork.shouldPoll(j.state));
      addErrorBubble(active ? 'Connection interrupted. Checking saved work again…' : 'Could not load saved work. Reload to reconnect.');
      if (active) {
        stopPolling();
        pollTimer = setTimeout(() => loadDurable(threadId), POLL_MS * 2);
      }
    }
  }
}

async function sendDurable(threadId, text, fromSuggestion) {
  const state = durableState(threadId);
  if (!state.snapshot) await loadDurable(threadId);
  if (!state.snapshot || !text) return;
  const pending = Object.freeze({ ...DurableWork.newSubmission(durableConversationId(threadId), state.snapshot.generation, text, crypto.randomUUID()), from_suggestion: fromSuggestion });
  return postDurable(threadId, pending);
}

async function postDurable(threadId, submission) {
  const state = durableState(threadId);
  if (state.snapshot.generation !== submission.generation) { state.pending.delete(submission.request_key); renderDurable(threadId); return; }
  state.pending.set(submission.request_key, { submission, failed: false });
  renderDurable(threadId);
  const { conversation_id, ...body } = DurableWork.retrySubmission(submission);
  try {
    await durableRequest(durableUrl(threadId, '/messages'), body);
    state.pending.delete(submission.request_key);
  } catch (error) {
    if ([400,401,404,409].includes(error.status)) state.pending.delete(submission.request_key);
    else state.pending.set(submission.request_key, { submission, failed: true });
  }
  await loadDurable(threadId);
  renderDurable(threadId);
}

async function cancelDurable(threadId, jobId) {
  try { await durableRequest(`/api/work/jobs/${encodeURIComponent(jobId)}/cancel`, {}); }
  catch (_) { addErrorBubble('Could not confirm cancellation.'); }
  await loadDurable(threadId);
}

async function resetDurable(threadId) {
  const state = durableState(threadId);
  if (!state.snapshot) return;
  ++state.serial; // Fence status responses already in flight.
  try {
    const result = await durableRequest(durableUrl(threadId, '/reset'), { generation: state.snapshot.generation });
    state.pending.clear();
    state.snapshot = { ...state.snapshot, generation: result.generation, messages: [], jobs: [] };
    const input = document.getElementById('ai-followup');
    if (input) input.value = '';
    renderDurable(threadId);
    await loadDurable(threadId);
  } catch (_) { addErrorBubble('Could not confirm reset.'); }
}

// A direct load of /settings or /chat opens on that view. Last, because the
// view code above has to exist before either can be shown.
if (window.__INITIAL_VIEW === 'settings' || location.pathname === SETTINGS_PATH) {
  showSettingsView({ push: false });
} else if (window.__INITIAL_VIEW === 'chat' || location.pathname === CHAT_PATH) {
  showChatView({ push: false });
}

// Fathom and Pocket are both a pasted API key behind the same card layout.
['fathom', 'pocket'].forEach(source => {
  document.getElementById(`${source}-connect-btn`).addEventListener('click', () => {
    const input = document.getElementById(`${source}-key-input`);
    const key = input.value.trim();
    if (!key) { input.focus(); return; }
    const btn = document.getElementById(`${source}-connect-btn`);
    btn.disabled = true;
    fetch(`/settings/sources/${source}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: key }),
    }).then(r => r.json()).then(data => {
      if (data.ok) setSourceConnected(source, true, data.api_key_preview);
      else alert(data.error || 'Failed to connect');
    }).finally(() => { btn.disabled = false; });
  });
  document.getElementById(`${source}-disconnect-btn`).addEventListener('click', () => {
    fetch(`/settings/sources/${source}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disconnect: true }),
    }).then(r => r.json()).then(data => { if (data.ok) setSourceConnected(source, false); });
  });
});

// ---------------- WhatsApp linking ----------------
// One number per user, proven by a code the user sends from their phone. The
// webhook completes the link, not this page, so while a code is pending the
// card polls settings to notice when it lands.
let whatsappPollTimer = null;

function renderWhatsappCard(info) {
  info = info || {};
  const status = document.getElementById('whatsapp-status');
  const hint = document.getElementById('whatsapp-hint');
  const inputRow = document.getElementById('whatsapp-input-row');
  const input = document.getElementById('whatsapp-number-input');
  const linkBtn = document.getElementById('whatsapp-link-btn');
  const steps = document.getElementById('whatsapp-steps');
  const linkedRow = document.getElementById('whatsapp-linked-row');
  const numberEl = document.getElementById('whatsapp-number');
  const errorEl = document.getElementById('whatsapp-error');
  if (whatsappPollTimer) { clearTimeout(whatsappPollTimer); whatsappPollTimer = null; }
  errorEl.hidden = true;

  if (!info.configured) {
    status.textContent = 'Not configured';
    status.className = 'source-connected-badge off';
    hint.textContent = 'Set META_WA_PHONE_NUMBER_ID, META_WA_ACCESS_TOKEN, META_WA_APP_SECRET and META_WA_VERIFY_TOKEN on the server to enable this.';
    inputRow.style.display = 'none';
    steps.hidden = true;
    linkedRow.style.display = 'none';
    return;
  }
  hint.textContent = 'Message your agent from WhatsApp. Same conversation as Chat. Enter your number with its country code.';

  if (info.number) {
    status.textContent = 'Linked';
    status.className = 'source-connected-badge on';
    numberEl.textContent = info.number;
    linkedRow.style.display = 'flex';
    inputRow.style.display = 'none';
    steps.hidden = true;
    return;
  }

  status.textContent = 'Not linked';
  status.className = 'source-connected-badge off';
  linkedRow.style.display = 'none';
  inputRow.style.display = 'flex';
  const pending = info.pending;
  if (!pending) {
    steps.hidden = true;
    linkBtn.textContent = 'Get code';
    return;
  }
  input.value = pending.number;
  linkBtn.textContent = 'New code';
  const from = info.business_number || "the app's WhatsApp number";
  const minutesLeft = Math.max(1, Math.round((new Date(pending.expires_at) - Date.now()) / 60000));
  const items = [];
  if (info.test_number) {
    items.push('This server is on a Meta <em>test</em> number, which only answers numbers on its recipient list — ask whoever runs it to add yours first.');
  }
  items.push(`Save <strong>${escapeHtml(from)}</strong> in your contacts.`);
  items.push(`From ${escapeHtml(pending.number)}, send the code <strong class="whatsapp-code">${escapeHtml(pending.code)}</strong> to that number.`);
  items.push(`The code expires in ${minutesLeft} min. This card updates by itself once the code arrives.`);
  steps.innerHTML = items.map(t => `<li>${t}</li>`).join('');
  steps.hidden = false;
  whatsappPollTimer = setTimeout(() => {
    whatsappPollTimer = null;
    if (!inSettingsView()) return;
    fetch('/settings.json').then(r => r.json()).then(d => renderWhatsappCard(d.whatsapp)).catch(() => {});
  }, 3000);
}

document.getElementById('whatsapp-link-btn').addEventListener('click', () => {
  const input = document.getElementById('whatsapp-number-input');
  const btn = document.getElementById('whatsapp-link-btn');
  const errorEl = document.getElementById('whatsapp-error');
  const number = input.value.trim();
  if (!number) { input.focus(); return; }
  btn.disabled = true;
  fetch('/settings/whatsapp/link', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ number }),
  })
    .then(r => r.json())
    .then(data => {
      if (data.ok) { renderWhatsappCard(data.whatsapp); return; }
      errorEl.textContent = data.error || 'Could not start linking';
      errorEl.hidden = false;
    })
    .catch(() => { errorEl.textContent = 'Could not start linking'; errorEl.hidden = false; })
    .finally(() => { btn.disabled = false; });
});
document.getElementById('whatsapp-unlink-btn').addEventListener('click', () => {
  fetch('/settings/whatsapp/unlink', { method: 'POST' })
    .then(r => r.json())
    .then(data => { if (data.ok) renderWhatsappCard(data.whatsapp); });
});

// ---------------- Browser notifications ----------------
// Enrolment is per browser: the subscription lives in this browser's push
// manager and a copy on the server. State is read from the browser, not the
// server, so a subscription revoked elsewhere never shows as "on" here.

function urlBase64ToUint8Array(base64) {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function currentPushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

function setPushState(on, hint) {
  const status = document.getElementById('push-status');
  status.textContent = on ? 'On' : 'Off';
  status.className = `source-connected-badge ${on ? 'on' : 'off'}`;
  document.getElementById('push-connect-row').style.display = on ? 'none' : 'flex';
  document.getElementById('push-connected-row').style.display = on ? 'flex' : 'none';
  if (hint !== undefined) document.getElementById('push-hint').textContent = hint;
}

async function renderPushCard(info) {
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  if (!info.configured || !supported) {
    setPushState(false, !info.configured
      ? 'Not configured on this server (VAPID keys missing).'
      : 'This browser does not support push notifications.');
    document.getElementById('push-connect-row').style.display = 'none';
    return;
  }
  if (Notification.permission === 'denied') {
    setPushState(false, 'Blocked in browser settings — allow notifications for this site to enable.');
    document.getElementById('push-connect-row').style.display = 'none';
    return;
  }
  const sub = await currentPushSubscription();
  setPushState(!!sub, sub
    ? 'This browser is enrolled. New todos arrive as notifications with their suggested actions.'
    : 'Get a notification for every new todo, with its suggested actions as buttons.');
}

document.getElementById('push-enable-btn').addEventListener('click', async () => {
  const btn = document.getElementById('push-enable-btn');
  btn.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      renderPushCard({ configured: true });
      return;
    }
    const { key } = await fetch('/push/vapid-public-key').then(r => r.json());
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
    const r = await fetch('/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    });
    if (!r.ok) throw new Error((await r.json()).error || 'subscribe failed');
    await renderPushCard({ configured: true });
  } catch (err) {
    alert(`Couldn't enable notifications: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('push-disable-btn').addEventListener('click', async () => {
  const sub = await currentPushSubscription();
  if (sub) {
    const endpoint = sub.endpoint;
    await sub.unsubscribe().catch(() => null);
    await fetch('/push/subscribe', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint }),
    }).catch(() => null);
  }
  await renderPushCard({ configured: true });
});

document.getElementById('push-test-btn').addEventListener('click', async () => {
  const btn = document.getElementById('push-test-btn');
  btn.disabled = true;
  try {
    const { sent } = await fetch('/push/test', { method: 'POST' }).then(r => r.json());
    document.getElementById('push-hint').textContent = sent
      ? 'Test sent — it should appear in a moment.'
      : 'Nothing sent — this browser may have unsubscribed. Disable and enable again.';
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('gmail-connect-btn').addEventListener('click', () => {
  window.location.href = '/settings/sources/gmail/auth';
});
