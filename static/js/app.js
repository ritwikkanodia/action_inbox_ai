const IMPORTANCE_OPTIONS = ['low', 'medium', 'high'];
const STATUS_OPTIONS  = ['open', 'ongoing', 'closed'];
const AI_SOURCES = ['gmail', 'fathom', 'browser_history', 'system'];

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
const contextCache = {};
const threadCache  = {};

const STATUS_LABELS = { open: 'Open', ongoing: 'Ongoing', closed: 'Closed', rejected: 'Rejected' };
const SOURCE_LABELS = {
  gmail: 'Gmail', fathom: 'Fathom', browser_history: 'Browser',
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
  document.querySelectorAll('.todo-row.selected').forEach(r => r.classList.remove('selected'));
  detailEmpty.classList.remove('hidden');
  detailContent.classList.add('hidden');
  detailContent.innerHTML = '';
  appEl.classList.remove('detail-open');
  if (location.hash) history.replaceState(null, '', location.pathname);
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

  if (location.hash !== `#todo/${id}`) {
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
  t.decision = decision;
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

  const sendBtn = document.getElementById('ai-send-btn');
  const followup = document.getElementById('ai-followup');
  sendBtn.addEventListener('click', () => {
    const msg = followup.value.trim();
    if (!msg) return;
    followup.value = '';
    followup.style.height = 'auto';
    // "Something else…" borrows the placeholder to show which question is
    // being answered; put it back once that answer is on its way.
    followup.placeholder = 'Or type your own instruction…';
    callAI(t.todo_id, msg);
  });

  document.getElementById('ai-stop-btn').addEventListener('click', () => stopRun(t.todo_id));
  document.getElementById('ai-regen-btn').addEventListener('click', () => loadActions(t, true));
  followup.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendBtn.click(); }
  });
  followup.addEventListener('input', () => {
    followup.style.height = 'auto';
    followup.style.height = Math.min(140, followup.scrollHeight) + 'px';
  });

  document.getElementById('ai-new-thread-btn').addEventListener('click', () => {
    fetch(`/todos/${t.todo_id}/reset-thread`, { method: 'POST' }).then(() => {
      delete threadCache[t.todo_id];
      stopPolling();
      setRunning(false);
      document.getElementById('ai-thread').innerHTML = '';
    });
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
      callAI(todoId, action.instruction);
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

function renderThread(thread) {
  const threadEl = document.getElementById('ai-thread');
  if (!threadEl) return;
  threadEl.innerHTML = '';
  thread.forEach((msg, i) => {
    if (msg.content) {
      const div = document.createElement('div');
      div.className = `ai-bubble ${msg.role}`;
      if (msg.role === 'assistant') div.innerHTML = marked.parse(msg.content);
      else div.textContent = msg.content;
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
    callAI(selectedId, answer);
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
    if (selectedId === todoId) renderThread(data.thread);
  }
  if (data.status === 'running') {
    if (selectedId === todoId) {
      // setRunning first: renderThread above wiped the loading bubble, and
      // renderActivity needs it back before it has anywhere to draw.
      setRunning(true, todoId);
      renderActivity(data.activity);
    }
    schedulePoll(todoId);
    return;
  }
  stopPolling();
  if (selectedId === todoId) setRunning(false);
}

function schedulePoll(todoId) {
  stopPolling();
  pollTimer = setTimeout(() => {
    fetch(`/todos/${todoId}/run`)
      .then(r => r.json())
      .then(data => {
        // The run may have been reset, or the worker restarted, while we waited.
        if (data.status === 'idle') { stopPolling(); if (selectedId === todoId) setRunning(false); return; }
        applyRunState(todoId, data);
      })
      .catch(() => schedulePoll(todoId));
  }, POLL_MS);
}

function stopRun(todoId) {
  const stopBtn = document.getElementById('ai-stop-btn');
  if (stopBtn) { stopBtn.disabled = true; stopBtn.textContent = 'Stopping…'; }
  fetch(`/todos/${todoId}/run/stop`, { method: 'POST' })
    .then(() => schedulePoll(todoId))
    .catch(() => schedulePoll(todoId));
}

function callAI(todoId, message) {
  if (message) {
    const threadEl = document.getElementById('ai-thread');
    if (threadEl && selectedId === todoId) {
      const userBubble = document.createElement('div');
      userBubble.className = 'ai-bubble user';
      userBubble.textContent = message;
      threadEl.appendChild(userBubble);
    }
    setRunning(true, todoId);
  }
  const body = message ? { message } : {};
  return fetch(`/todos/${todoId}/ask-ai`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
    .then(r => r.json())
    .then(data => applyRunState(todoId, data))
    .catch(() => {
      stopPolling();
      if (selectedId === todoId) {
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
  stopPolling();
  setRunning(false);

  if (threadCache[t.todo_id] && threadCache[t.todo_id].length > 0) {
    renderThread(threadCache[t.todo_id]);
  } else if (!t.has_ai_thread) {
    threadEl.innerHTML = '<div class="ai-empty-cta">Pick an action above, or type your own instruction below.</div>';
  }

  // Ask the server what it has: either the persisted thread, or a run this
  // page never started — one left behind by a reload, or by another tab.
  fetch(`/todos/${t.todo_id}/ask-ai`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
    .then(r => r.json())
    .then(data => {
      if (selectedId !== t.todo_id) return;
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

// ---------------- Settings modal ----------------
const settingsModal = document.getElementById('settings-modal');

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
  if (!on && prefix === 'fathom') {
    document.getElementById('fathom-key-input').value = '';
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

  container.innerHTML = accounts.map(a => `
    <div class="gmail-account-row" data-email="${escapeHtml(a.email)}">
      <span class="gmail-account-email">${escapeHtml(a.email)}</span>
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

function openSettingsModal() {
  settingsModal.classList.add('open');
  fetch('/settings').then(r => r.json()).then(data => {
    const { fathom, gmail } = data.sources;
    setSourceConnected('fathom', fathom.connected, fathom.api_key_preview);
    renderGmailAccounts(gmail.accounts || []);
  });
}
document.getElementById('openSettingsBtn').addEventListener('click', openSettingsModal);
document.getElementById('settings-modal-close').addEventListener('click', () => settingsModal.classList.remove('open'));
settingsModal.addEventListener('click', e => { if (e.target === settingsModal) settingsModal.classList.remove('open'); });

document.getElementById('fathom-connect-btn').addEventListener('click', () => {
  const key = document.getElementById('fathom-key-input').value.trim();
  if (!key) { document.getElementById('fathom-key-input').focus(); return; }
  const btn = document.getElementById('fathom-connect-btn');
  btn.disabled = true;
  fetch('/settings/sources/fathom', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: key }),
  }).then(r => r.json()).then(data => {
    if (data.ok) setSourceConnected('fathom', true, data.api_key_preview);
    else alert(data.error || 'Failed to connect');
  }).finally(() => { btn.disabled = false; });
});
document.getElementById('fathom-disconnect-btn').addEventListener('click', () => {
  fetch('/settings/sources/fathom', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ disconnect: true }),
  }).then(r => r.json()).then(data => { if (data.ok) setSourceConnected('fathom', false); });
});

document.getElementById('gmail-connect-btn').addEventListener('click', () => {
  window.location.href = '/settings/sources/gmail/auth';
});
