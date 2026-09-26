/* Cloud controller: account data lives only in this page's memory. */
(function () {
  'use strict';
  const S = window.CloudState, D = window.DurableWork;
  const state = S.createState(null);
  const el = id => document.getElementById(id);
  const controllers = new Set();
  let auth = null, cid = null, generation = 1, nextCursor = null;
  let pollTimer = null, pollIndex = 0, bootstrapSerial = 0, snapshotSerial = 0;
  let selectedTodo = null, validating = null;
  let channel = null;
  try { if ('BroadcastChannel' in window) channel = new BroadcastChannel('athena-session'); } catch (_) {}
  const status = text => { el('cloud-status').textContent = text; };
  const stale = () => Object.assign(new Error('stale_response'), { stale: true });
  function clearAccount(message) {
    S.suspend(state);
    bootstrapSerial++; snapshotSerial++;
    for (const controller of controllers) controller.abort();
    controllers.clear();
    clearTimeout(pollTimer);
    auth = null; cid = null; selectedTodo = null; nextCursor = null;
    el('cloud-account').hidden = true;
    for (const id of ['cloud-profile','cloud-thread','cloud-jobs','cloud-todos']) el(id).replaceChildren();
    el('cloud-draft').value = '';
    el('cloud-draft').disabled = true; el('cloud-send').disabled = true;
    el('cloud-retry').hidden = true;
    status(message || 'Checking your session…');
  }
  function signal() { if (channel) channel.postMessage('session-changed'); }
  async function raw(path, options = {}) {
    const controller = new AbortController();
    controllers.add(controller);
    try {
      const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store',
        ...options, signal: controller.signal });
      let data = {};
      try { data = await response.json(); } catch (_) {}
      return { response, data };
    } finally { controllers.delete(controller); }
  }
  function invalidSession(result) {
    return result.response.status === 401 || result.data.error === 'session_context_changed';
  }
  async function requestJSON(path, body) {
    const ticket = S.beginRequest(state);
    if (!auth || state.suspended) throw stale();
    const options = body === undefined ? {} : { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': auth.csrf,
        'X-Athena-Context': auth.context_id }, body: JSON.stringify(body) };
    const result = await raw(path, options);
    if (!S.acceptResponse(state, ticket)) throw stale();
    if (invalidSession(result)) {
      clearAccount('Your session changed. Please sign in again.');
      signal(); window.location.assign('/login'); throw stale();
    }
    if (!result.response.ok) throw Object.assign(new Error('request_failed'), { status: result.response.status });
    return result.data;
  }
  async function validateTicket(ticket) {
    if (!S.acceptResponse(state, ticket)) return false;
    if (channel) return true;
    const result = await raw('/api/cloud/bootstrap');
    if (!S.acceptResponse(state, ticket)) return false;
    if (!result.response.ok || result.data.context_id !== state.contextId) {
      clearAccount('Your session changed. Checking access…');
      void revalidate();
      return false;
    }
    auth = result.data;
    return true;
  }
  function showTodos(todos, append = false) {
    if (!append) el('cloud-todos').replaceChildren();
    for (const todo of todos) {
      const li = document.createElement('li'), button = document.createElement('button');
      button.type = 'button'; button.textContent = todo.title;
      button.addEventListener('click', () => openConversation(todo));
      li.append(button); el('cloud-todos').append(li);
    }
    el('cloud-more').hidden = !nextCursor;
  }
  async function revalidate() {
    if (document.hidden) return;
    const serial = ++bootstrapSerial, version = state.generation;
    try {
      const result = await raw('/api/cloud/bootstrap');
      if (serial !== bootstrapSerial || version !== state.generation || document.hidden) return;
      if (!result.response.ok) {
        clearAccount(result.response.status === 401 ? 'Please sign in again.' : 'Service unavailable. Retrying…');
        if (result.response.status === 401) window.location.assign('/login');
        return;
      }
      const data = result.data, first = state.contextId === null;
      const changed = !first && state.contextId !== data.context_id;
      if (changed) { clearAccount('Session changed.'); signal(); }
      if (changed || state.suspended || state.contextId === null) S.resume(state, data.context_id);
      auth = data; nextCursor = data.next_cursor;
      // A fresh document after OAuth has no previous context, but other tabs do.
      if (first) signal();
      el('cloud-profile').textContent = data.profile.name + ' · ' + data.profile.email;
      showTodos(data.todos);
      el('cloud-account').hidden = false;
      status('');
      const route = window.location.pathname;
      el('cloud-settings').hidden = route !== '/settings';
      el('cloud-inbox').hidden = route !== '/' || selectedTodo !== null;
      el('cloud-chat').hidden = route !== '/chat' && selectedTodo === null;
      el('cloud-execution').textContent = data.capabilities.chat_execute ? 'Synthetic test execution' : 'Task execution is not available yet.';
      el('cloud-draft').disabled = !data.capabilities.chat_execute || !!state.pending || !cid;
      el('cloud-send').disabled = !data.capabilities.chat_execute || !!state.pending || !cid;
      if (!el('cloud-chat').hidden && !cid) await openConversation(selectedTodo);
    } catch (error) {
      if (serial === bootstrapSerial && error.name !== 'AbortError' && !error.stale)
        clearAccount('Service unavailable. Retrying…');
    }
  }
  async function openConversation(todo = null) {
    if (!auth || state.suspended) return;
    const ticket = S.beginRequest(state);
    try {
      const data = await requestJSON('/api/cloud/conversations/ensure', todo ? { kind: 'todo', todo_id: todo.id } : { kind: 'chat' });
      if (!await validateTicket(ticket)) return;
      selectedTodo = todo; cid = data.id; pollIndex = 0;
      state.pending = null; el('cloud-draft').value = ''; el('cloud-retry').hidden = true;
      el('cloud-title').textContent = todo ? todo.title : 'Chat';
      el('cloud-chat').hidden = false; el('cloud-inbox').hidden = true;
      await snapshot();
    } catch (error) { if (!error.stale && error.name !== 'AbortError') status('Could not open conversation. Reload to retry.'); }
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!auth || state.suspended || document.hidden) return;
    const delay = [1000, 2000, 5000, 10000][Math.min(pollIndex++, 3)];
    pollTimer = setTimeout(snapshot, delay);
  }
  async function snapshot() {
    if (!cid || !auth || state.suspended) return;
    clearTimeout(pollTimer);
    const conversation = cid, serial = ++snapshotSerial, ticket = S.beginRequest(state);
    try {
      const data = await requestJSON('/api/work/conversations/' + conversation);
      if (!await validateTicket(ticket) || serial !== snapshotSerial || conversation !== cid) return;
      generation = data.generation;
      el('cloud-draft').disabled = !auth.capabilities.chat_execute || !!state.pending;
      el('cloud-send').disabled = !auth.capabilities.chat_execute || !!state.pending;
      el('cloud-thread').replaceChildren();
      for (const message of data.messages) {
        const p = document.createElement('p'); p.textContent = message.role + ': ' + message.content;
        el('cloud-thread').append(p);
      }
      el('cloud-jobs').replaceChildren();
      for (const job of data.jobs) {
        const li = document.createElement('li'); li.dataset.workState = job.state;
        li.textContent = job.state;
        if (D.shouldPoll(job.state)) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Cancel';
          button.dataset.cancelJob = job.job_id;
          button.addEventListener('click', async () => {
            try { await requestJSON('/api/work/jobs/' + job.job_id + '/cancel', {}); await snapshot(); }
            catch (error) { if (!error.stale) status('Cancellation could not be confirmed. Refresh status before trying again.'); }
          });
          li.append(button);
        }
        el('cloud-jobs').append(li);
      }
      if (data.jobs.some(job => D.shouldPoll(job.state))) schedulePoll();
      else pollIndex = 0;
    } catch (error) {
      if (!error.stale && error.name !== 'AbortError' && S.acceptResponse(state, ticket)) {
        status('Status temporarily unavailable. Retrying…'); schedulePoll();
      }
    }
  }
  async function send(retry = false) {
    if (!auth || !cid || state.suspended || !auth.capabilities.chat_execute) return;
    const ticket = S.beginRequest(state);
    try {
      if (!await validateTicket(ticket)) return;
      if (!retry) {
        const text = el('cloud-draft').value;
        if (!text.trim() || state.pending) return;
        state.pending = D.newSubmission(cid, generation, text, crypto.randomUUID());
      }
      if (!state.pending) return;
      const pending = D.retrySubmission(state.pending);
      el('cloud-send').disabled = true; el('cloud-draft').disabled = true; el('cloud-retry').hidden = true;
      await requestJSON('/api/work/conversations/' + pending.conversation_id + '/messages', {
        text: pending.text, generation: pending.generation, request_key: pending.request_key });
      if (!S.acceptResponse(state, ticket)) return;
      state.pending = null; el('cloud-draft').value = ''; status('Message saved.');
      await snapshot();
    } catch (error) {
      if (!S.acceptResponse(state, ticket) || error.stale || error.name === 'AbortError') return;
      if (error.status && error.status < 500) {
        el('cloud-draft').value = state.pending ? state.pending.text : '';
        state.pending = null; status('Message was not saved. Review your draft before sending again.');
        if (error.status === 409) void snapshot();
      } else {
        status('Delivery is uncertain. Retry the same message to check safely.');
        el('cloud-retry').hidden = false;
      }
    } finally {
      if (S.acceptResponse(state, ticket) && auth) {
        el('cloud-send').disabled = !auth.capabilities.chat_execute || !!state.pending;
        el('cloud-draft').disabled = !auth.capabilities.chat_execute || !!state.pending;
      }
    }
  }
  el('cloud-compose').addEventListener('submit', event => { event.preventDefault(); void send(); });
  el('cloud-retry').addEventListener('click', () => void send(true));
  el('cloud-reset').addEventListener('click', async () => {
    if (!cid) return;
    try {
      await requestJSON('/api/work/conversations/' + cid + '/reset', { generation });
      state.pending = null; el('cloud-draft').value = ''; el('cloud-retry').hidden = true;
      el('cloud-draft').disabled = !auth.capabilities.chat_execute; el('cloud-send').disabled = !auth.capabilities.chat_execute;
      await snapshot();
    } catch (error) { if (!error.stale) status('Reset could not be confirmed. Refresh status before retrying.'); }
  });
  el('cloud-more').addEventListener('click', async () => {
    if (!nextCursor) return;
    const ticket = S.beginRequest(state);
    try {
      const data = await requestJSON('/api/cloud/bootstrap?cursor=' + encodeURIComponent(nextCursor));
      if (!await validateTicket(ticket)) return;
      nextCursor = data.next_cursor; showTodos(data.todos, true);
    } catch (error) { if (!error.stale) status('Could not load more tasks. Try again.'); }
  });
  async function logout(all) {
    if (!auth) return;
    const saved = auth;
    clearAccount('Signing out…'); signal();
    try {
      const response = await fetch(all ? '/api/auth/logout-all' : '/logout', { method: 'POST',
        credentials: 'same-origin', cache: 'no-store', redirect: 'manual',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': saved.csrf,
          'X-Athena-Context': saved.context_id }, body: '{}' });
      // A same-origin manual 303 is opaque; server errors retain their status.
      if (response.type === 'opaqueredirect' || response.ok || response.status === 401) {
        signal(); window.location.assign('/login');
      } else status('Screen cleared, but the server could not confirm sign-out. Close this tab and try again when service returns.');
    } catch (_) { status('Screen cleared, but sign-out could not be confirmed. Close this tab and try again when service returns.'); }
  }
  el('cloud-logout').addEventListener('click', () => void logout(false));
  el('cloud-logout-all').addEventListener('click', () => void logout(true));
  if (channel) channel.onmessage = event => {
    if (event.data === 'session-changed') { clearAccount(); void revalidate(); }
  };
  window.addEventListener('pagehide', () => clearAccount());
  window.addEventListener('pageshow', () => { clearAccount(); void revalidate(); });
  document.addEventListener('visibilitychange', () => {
    clearAccount(); if (!document.hidden) void revalidate();
  });
  setInterval(() => { if (!document.hidden) void revalidate(); }, 30000);
  clearAccount(); void revalidate();
})();
