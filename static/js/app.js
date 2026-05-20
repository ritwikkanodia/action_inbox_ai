const URGENCY_OPTIONS = ['low', 'medium', 'high'];
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
const tabOpen     = document.getElementById('tabOpen');
const tabClosed   = document.getElementById('tabClosed');
const tabRejected = document.getElementById('tabRejected');
const countEl     = document.getElementById('todoCount');
const appEl = document.getElementById('app');
const detailEmpty   = document.getElementById('detail-empty');
const detailContent = document.getElementById('detail-content');

let currentFilter = 'open';
let selectedId = null;
const contextCache = {};
const threadCache  = {};

function refreshCount() {
  const rows = Array.from(document.querySelectorAll('#todo-list .todo-row'));
  const n = rows.filter(r => {
    if (currentFilter === 'open')     return !r.classList.contains('closed') && !r.classList.contains('rejected-todo');
    if (currentFilter === 'closed')   return r.classList.contains('closed');
    if (currentFilter === 'rejected') return r.classList.contains('rejected-todo');
    return false;
  }).length;
  countEl.textContent = n;
}

function switchTab(tab) {
  currentFilter = tab;
  [tabOpen, tabClosed, tabRejected].forEach(b => b.classList.remove('active'));
  todoList.className = `show-${tab}`;
  ({ open: tabOpen, closed: tabClosed, rejected: tabRejected })[tab].classList.add('active');
  refreshCount();

  if (selectedId) {
    const row = document.querySelector(`.todo-row[data-id="${selectedId}"]`);
    if (row && getComputedStyle(row).display === 'none') clearSelection();
  }
}

tabOpen.addEventListener('click',     () => switchTab('open'));
tabClosed.addEventListener('click',   () => switchTab('closed'));
tabRejected.addEventListener('click', () => switchTab('rejected'));

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
applyViewMode(localStorage.getItem('viewMode') === 'list' ? 'list' : 'split');
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
    } else if (field === 'urgency') {
      row.dataset.urgency = newVal || 'none';
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
      } else if (field === 'urgency') {
        dCell.className = `editable urgency ${newVal || ''}`;
        dCell.textContent = newVal || '—';
      } else if (field === 'due_date') {
        dCell.textContent = fmtDue(newVal);
      }
    }
  }

  refreshCount();
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
    if (field === 'urgency')      widget = makeSelect(URGENCY_OPTIONS, value, commit);
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
  refreshCount();
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

        <dt>Urgency</dt>
        <dd><span class="editable urgency ${t.urgency || ''}" data-field="urgency" data-value="${t.urgency || ''}">${t.urgency || '—'}</span></dd>

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
        <h3>Ask AI</h3>
        <button id="ai-new-thread-btn">New thread</button>
      </div>
      <div id="ai-thread"></div>
      <div id="ai-input-area">
        <textarea id="ai-followup" placeholder="Ask a follow-up…" rows="1"></textarea>
        <button id="ai-send-btn">Send</button>
      </div>
    </div>
  `;

  wireDetailHandlers(t);
  loadContext(t);
  loadAiThread(t);
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
    const userBubble = document.createElement('div');
    userBubble.className = 'ai-bubble user';
    userBubble.textContent = msg;
    document.getElementById('ai-thread').appendChild(userBubble);
    followup.value = '';
    callAI(t.todo_id, msg);
  });
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
      document.getElementById('ai-thread').innerHTML = '';
      callAI(t.todo_id, null);
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

// ---------------- Ask AI ----------------
function renderThread(thread) {
  const threadEl = document.getElementById('ai-thread');
  if (!threadEl) return;
  threadEl.innerHTML = '';
  thread.forEach(msg => {
    const div = document.createElement('div');
    div.className = `ai-bubble ${msg.role}`;
    if (msg.role === 'assistant') div.innerHTML = marked.parse(msg.content);
    else div.textContent = msg.content;
    threadEl.appendChild(div);
  });
  threadEl.scrollTop = threadEl.scrollHeight;
}

function addLoadingBubble() {
  const threadEl = document.getElementById('ai-thread');
  if (!threadEl) return;
  const div = document.createElement('div');
  div.className = 'ai-bubble loading';
  div.id = 'ai-loading-bubble';
  div.textContent = 'Thinking…';
  threadEl.appendChild(div);
  threadEl.scrollTop = threadEl.scrollHeight;
}

function removeLoadingBubble() {
  const el = document.getElementById('ai-loading-bubble');
  if (el) el.remove();
}

function callAI(todoId, message) {
  const sendBtn = document.getElementById('ai-send-btn');
  const followup = document.getElementById('ai-followup');
  if (sendBtn) sendBtn.disabled = true;
  if (followup) followup.disabled = true;
  addLoadingBubble();
  const body = message ? { message } : {};
  return fetch(`/todos/${todoId}/ask-ai`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
    .then(r => r.json())
    .then(data => {
      removeLoadingBubble();
      threadCache[todoId] = data.thread;
      if (selectedId !== todoId) return;
      renderThread(data.thread);
      if (sendBtn) sendBtn.disabled = false;
      if (followup) { followup.disabled = false; followup.focus(); }
    })
    .catch(() => {
      removeLoadingBubble();
      if (sendBtn) sendBtn.disabled = false;
      if (followup) followup.disabled = false;
    });
}

function loadAiThread(t) {
  const threadEl = document.getElementById('ai-thread');
  if (threadCache[t.todo_id] && threadCache[t.todo_id].length > 0) {
    renderThread(threadCache[t.todo_id]);
    return;
  }
  if (t.has_ai_thread) {
    callAI(t.todo_id, null);
    return;
  }
  threadEl.innerHTML = `
    <div class="ai-empty-cta">
      Ask a question, or
      <button id="ai-kickoff-btn" class="ai-kickoff-btn">get AI suggestions</button>
      for this item.
    </div>`;
  const kickoff = document.getElementById('ai-kickoff-btn');
  if (kickoff) kickoff.addEventListener('click', () => {
    threadEl.innerHTML = '';
    callAI(t.todo_id, null);
  });
}

// ---------------- New-todo form ----------------
const openFormBtn  = document.getElementById('openFormBtn');
const newTodoForm  = document.getElementById('newTodoForm');
const cancelBtn    = document.getElementById('cancelNewTodo');
const saveBtnNew   = document.getElementById('saveNewTodo');

function resetForm() {
  document.getElementById('ntTitle').value = '';
  document.getElementById('ntUrgency').value = 'medium';
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
  row.dataset.urgency = t.urgency || 'none';
  row.innerHTML = `
    <div class="row-body">
      <div class="row-title">
        <span class="row-title-text">${escapeHtml(t.title || '(untitled)')}</span>
        <span class="source-badge inline-source ${t.source || 'user'}">${sourceLabel(t.source)}</span>
      </div>
      <div class="row-meta">
        ${t.due_date ? `<span class="row-due">${fmtDue(t.due_date)}</span>` : ''}
        <span class="source-badge ${t.source || 'user'}">${sourceLabel(t.source)}</span>
      </div>
    </div>
    <div class="row-inline-actions" data-id="${t.todo_id}">
      <span class="row-act editable" data-field="urgency" data-value="${t.urgency || ''}" title="Urgency">${t.urgency || '—'}</span>
      <span class="row-act editable" data-field="status" data-value="${t.status || ''}" title="Status">${t.status || '—'}</span>
      <span class="row-act editable" data-field="due_date" data-value="${t.due_date || ''}" title="Due date">${t.due_date ? fmtDue(t.due_date) : 'set due'}</span>
      <div class="row-decision-slot"></div>
    </div>
  `;
  return row;
}

saveBtnNew.addEventListener('click', () => {
  const title = document.getElementById('ntTitle').value.trim();
  if (!title) { document.getElementById('ntTitle').focus(); return; }
  const urgency = document.getElementById('ntUrgency').value;
  const dueDateRaw = document.getElementById('ntDueDate').value;
  const due_date = dueDateRaw ? new Date(dueDateRaw).toISOString() : null;
  const suggested_action = document.getElementById('ntAction').value.trim();
  fetch('/todos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, urgency, due_date, suggested_action }),
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
    refreshCount();
    selectTodo(t.todo_id);
  });
});

document.getElementById('ntTitle').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveBtnNew.click();
  if (e.key === 'Escape') resetForm();
});

// ---------------- Init ----------------
switchTab('open');
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

function openSettingsModal() {
  settingsModal.classList.add('open');
  fetch('/settings').then(r => r.json()).then(data => {
    const { fathom, gmail } = data.sources;
    setSourceConnected('fathom', fathom.connected, fathom.api_key_preview);
    setSourceConnected('gmail', gmail.connected, gmail.email || 'Authorized via OAuth');
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
document.getElementById('gmail-disconnect-btn').addEventListener('click', () => {
  fetch('/settings/sources/gmail', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ disconnect: true }),
  }).then(r => r.json()).then(data => { if (data.ok) setSourceConnected('gmail', false); });
});
