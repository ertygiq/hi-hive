const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');

function client(storage = new Map()) {
  const elements = new Map();
  const listeners = new Map();
  const element = () => ({ style: {}, value: '', textContent: '', innerHTML: '', scrollHeight: 34,
    offsetHeight: 36, clientHeight: 34,
    classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, focus() {}, querySelectorAll() { return []; } });
  const document = {
    visibilityState: 'visible',
    documentElement: { clientHeight: 800 },
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    querySelectorAll() { return []; }, addEventListener(name, callback) { listeners.set(name, callback); },
    createElement() {
      const e = element();
      Object.defineProperty(e, 'innerHTML', { get() { return e.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } });
      return e;
    }
  };
  const context = vm.createContext({ document, localStorage: {
    getItem(key) { return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, value); }
  }, addEventListener(name, callback) { listeners.set(name, callback); },
    setInterval() {}, setTimeout() {}, clearTimeout() {}, crypto: { randomUUID }, console, innerHeight: 800 });
  context.window = context;
  const source = fs.readFileSync('src/hihive/static/app.js', 'utf8').split('// --- Init ---')[0];
  vm.runInContext(source, context);
  vm.runInContext("selectedTaskId = 'task'; configCache = { harnesses: [] }; const originalPoll = poll; poll = () => {};", context);
  return { context, document, storage, listeners };
}

test('Done failures show a dialog, including non-JSON and network errors', async () => {
  for (const failure of ['json', 'text', 'network']) {
    const { context } = client();
    const dialogs = [];
    context.showMessage = (...args) => dialogs.push(args);
    context.fetch = async () => {
      if (failure === 'network') throw new Error('offline');
      return { ok: false, json: async () => {
        if (failure === 'text') throw new Error('not JSON');
        return { error: 'Task not found' };
      } };
    };
    await vm.runInContext("toggleDone('task')", context);
    assert.equal(dialogs.length, 1);
    assert.match(dialogs[0][0], failure === 'json' ? /Task not found/ : failure === 'text' ? /Could not update/ : /may or may not have been saved/);
  }
});

test('marking and unmarking Done refresh the board without an error', async () => {
  const { context } = client();
  let polls = 0;
  context.poll = () => polls++;
  context.showMessage = () => assert.fail('Unexpected error dialog');
  context.showConfirm = async () => true;
  context.fetch = async () => ({ ok: true });
  for (const done of [false, true]) {
    context.snapshot = { results: [{ id: 'task', done }] };
    vm.runInContext('state = snapshot', context);
    await vm.runInContext("toggleDone('task')", context);
  }
  assert.equal(polls, 2);
});

test('result cards show accessible outline message counts and plain reviewed text', () => {
  const { context } = client();
  for (const count of [1, 2]) {
    context.task = { id: 'task', label: 'Result', status: 'completed', chat_count: count, has_review: true };
    const html = vm.runInContext('renderResultCard(task, 0, new Map())', context);
    const label = `${count} ${count === 1 ? 'message' : 'messages'} after the initial prompt`;
    assert.ok(html.includes(`title="${label}" aria-label="${label}"`));
    assert.match(html, /<span>reviewed<\/span>/);
    assert.match(html, /<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" aria-hidden="true">/);
    assert.match(html, /<path d="M2 1\.75h8/);
    assert.ok(html.indexOf('<span>reviewed</span>') < html.indexOf('class="message-count"'));
    assert.doesNotMatch(html, /💬|📋/);
  }
  for (const count of [0, -1, null, '1" onmouseover="attack', 1.5]) {
    context.count = count;
    assert.equal(vm.runInContext('messageCountHtml(count)', context), '');
  }
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.message-count\s*\{[^}]*display:\s*inline-flex;[^}]*align-items:\s*center;[^}]*gap:\s*3px;[^}]*font-variant-numeric:\s*tabular-nums;/s);
  assert.match(css, /\.message-count svg\s*\{\s*flex-shrink:\s*0;/);
});

test('message timestamps use local date rules and include full title', () => {
  const { context } = client();
  const now = new Date(2026, 6, 10, 12);
  for (const [date, options] of [
    [new Date(2026, 6, 10, 9, 8, 7), { hour: 'numeric', minute: '2-digit' }],
    [new Date(2026, 6, 9, 9, 8, 7), { hour: 'numeric', minute: '2-digit', month: 'short', day: 'numeric' }],
    [new Date(2025, 6, 9, 9, 8, 7), { hour: 'numeric', minute: '2-digit', month: 'short', day: 'numeric', year: 'numeric' }]
  ]) {
    context.value = date.toISOString(); context.now = now;
    const html = vm.runInContext('messageTimestampHtml(value, now)', context);
    assert.ok(html.includes(new Intl.DateTimeFormat(undefined, options).format(date)));
    assert.ok(html.includes(new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'medium' }).format(date)));
    assert.match(html, /<time class="chat-timestamp" datetime="[^"]+" title="[^"]+">/);
  }
  assert.equal(vm.runInContext('messageTimestampHtml(null)', context), '');
  assert.equal(vm.runInContext("messageTimestampHtml('not a date')", context), '');
  context.Intl = { DateTimeFormat: function () { return { format: () => '<bad "quoted">' }; } };
  const escaped = vm.runInContext("messageTimestampHtml('2026-01-01T00:00:00Z')", context);
  assert.match(escaped, /title="&lt;bad &quot;quoted&quot;&gt;"/);
  assert.doesNotMatch(escaped, /<bad/);
});

test('message timestamps preserve space and toggle only from role rows on hoverless devices', () => {
  const { context, document } = client();
  context.task = { id: 'task', status: 'completed', messages: [
    { role: 'assistant', kind: 'initial', content: 'answer', timestamp: '2026-01-01T00:00:00Z' },
    { role: 'user', kind: 'follow_up', content: 'no date' }
  ] };
  vm.runInContext('renderDetail(task)', context);
  const html = document.getElementById('detail-body').innerHTML;
  assert.match(html, /class="chat-role" onclick="toggleMessageTimestamp\(event\)"/);
  assert.equal((html.match(/class="chat-timestamp"/g) || []).length, 1);
  assert.doesNotMatch(html, /class="chat-content[^\"]*" onclick=/);
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.chat-timestamp\s*\{[^}]*opacity:\s*0;[^}]*transition:\s*opacity 0\.15s/s);
  assert.match(css, /\.chat-message:hover \.chat-timestamp/);
  assert.match(css, /\.chat-message:focus-within \.chat-timestamp/);
  assert.doesNotMatch(css.match(/\.chat-timestamp\s*\{[^}]*\}/s)[0], /display:\s*none|visibility:\s*hidden/);
  let toggles = 0;
  context.event = { currentTarget: { classList: { toggle(name) { assert.equal(name, 'show-timestamp'); toggles++; } } } };
  context.matchMedia = () => ({ matches: false });
  vm.runInContext('toggleMessageTimestamp(event)', context);
  assert.equal(toggles, 0);
  context.matchMedia = () => ({ matches: true });
  vm.runInContext('toggleMessageTimestamp(event); toggleMessageTimestamp(event)', context);
  assert.equal(toggles, 2);
});

test('reply Stop sends the visible input and state, preserving drafts and handling stale and failed requests', async () => {
  for (const response of [200, 409, 500]) {
    const { context, document } = client();
    context.task = { id: 'task', status: 'running', activity_kind: 'reply', runner_state: 'active', stop_token: 'token',
      messages: [{ id: 'input-id', role: 'user', kind: 'follow_up', content: 'hello', settled: false }] };
    vm.runInContext('renderDetail(task)', context);
    assert.equal(document.getElementById('chat-stop-btn').style.display, 'inline-flex');
    document.getElementById('chat-input').value = 'draft';
    const dialogs = []; context.showMessage = (...args) => dialogs.push(args);
    let request;
    context.fetch = async (url, options) => {
      if (!options) return { ok: true, json: async () => ({ ...context.task, runner_state: 'idle', messages: [] }) };
      request = { url, body: JSON.parse(options.body) };
      return { ok: response === 200, status: response, json: async () => ({ error: 'problem' }) };
    };
    await vm.runInContext("stopWork('reply')", context);
    assert.equal(request.url, '/api/tasks/task/stop-work');
    assert.equal(request.body.msg_id, 'input-id');
    assert.equal(request.body.token, 'token');
    assert.equal(document.getElementById('chat-input').value, 'draft');
    assert.equal(dialogs.length, response === 500 ? 1 : 0);
    assert.equal(document.getElementById('chat-send-btn').disabled, response === 200);
  }
});

test('Send has visible disabled styling during stop', () => {
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /#chat-send-btn:disabled\s*\{[^}]*opacity:\s*0\.5;[^}]*cursor:\s*default;/);
});

test('original work has no stop or typing line, idle background has only background stop', () => {
  const { context, document } = client();
  context.task = { id: 'task', status: 'running', activity_kind: 'original', runner_state: 'active', chat_pending: true,
    background_activity: { task_count: 2 }, messages: [{ id: 'id', role: 'user', settled: false }] };
  vm.runInContext('renderDetail(task)', context);
  assert.equal(document.getElementById('chat-stop-btn').style.display, 'none');
  assert.equal(document.getElementById('chat-typing').style.display, 'none');
  assert.equal(document.getElementById('detail-background-stop-btn').style.display, 'none');
  context.task = { ...context.task, activity_kind: 'reply', runner_state: 'idle', status: 'completed', messages: [] };
  vm.runInContext('renderDetail(task)', context);
  assert.equal(document.getElementById('detail-background-stop-btn').style.display, 'inline-flex');
});

test('active task cards do not show output byte counts', () => {
  const { context, document } = client();
  context.snapshot = { workers: [{ id: 'task', label: 'Task', status: 'running', output_length: 2048, output_tail: 'Hello' }], running_total: 1 };
  vm.runInContext('state = snapshot; renderWorkers()', context);
  const html = document.getElementById('workers-list').innerHTML;
  assert.match(html, /Hello/);
  assert.doesNotMatch(html, /2\.0KB|2048B| output<\/span>/);
});

const unreadKey = 'hive-unread-follow-up-replies';
const replyTime = n => `2026-09-26T12:00:0${n}.000000Z`;
function unreadSnapshot(c, replyAt, extra = {}) {
  c.snapshot = { results: [{ id: 'task', label: 'Task', status: 'completed',
    latest_follow_up_reply_at: replyAt, completed_at: '2020-01-01T00:00:00Z', ...extra }], workers: [], queue: [] };
  vm.runInContext('trackUnreadReplies(snapshot); state = fullState = snapshot;', c);
}
function unreadIds(c) { return Array.from(vm.runInContext('[...unreadReplies.ids]', c)); }

test('unread storage contains only outstanding task IDs and one checkpoint', () => {
  const { context, storage } = client();
  unreadSnapshot(context, replyTime(1));
  assert.deepEqual(unreadIds(context), []); // No flood of historical unread replies on first use.
  unreadSnapshot(context, replyTime(2));
  assert.deepEqual(unreadIds(context), ['task']);
  assert.deepEqual(JSON.parse(storage.get(unreadKey)), { ids: ['task'], checkedThrough: replyTime(2) });
  context.task = { ...context.snapshot.results[0], messages: [] };
  vm.runInContext('renderDetail(task)', context);
  assert.deepEqual(JSON.parse(storage.get(unreadKey)), { ids: [], checkedThrough: replyTime(2) });
  unreadSnapshot(context, replyTime(2));
  assert.deepEqual(unreadIds(context), []);
  unreadSnapshot(context, replyTime(3));
  assert.deepEqual(unreadIds(context), ['task']);
});

test('unread replies survive reload and replies arriving while closed are detected', () => {
  const first = client();
  unreadSnapshot(first.context, null);
  unreadSnapshot(first.context, replyTime(1));
  const second = client(first.storage);
  unreadSnapshot(second.context, replyTime(1));
  assert.deepEqual(unreadIds(second.context), ['task']);
  second.context.task = { ...second.context.snapshot.results[0], messages: [] };
  vm.runInContext('renderDetail(task)', second.context);
  const third = client(second.storage);
  unreadSnapshot(third.context, replyTime(2));
  assert.deepEqual(unreadIds(third.context), ['task']);
});

test('background tabs, prompt tabs and stale detail responses do not clear unread', () => {
  const { context, document, listeners } = client();
  unreadSnapshot(context, null);
  unreadSnapshot(context, replyTime(2));
  context.task = { ...context.snapshot.results[0], messages: [] };
  document.visibilityState = 'hidden';
  vm.runInContext('renderDetail(task)', context);
  assert.deepEqual(unreadIds(context), ['task']);
  document.visibilityState = 'visible';
  vm.runInContext("currentDetailTab = 'prompt'; renderDetail(task)", context);
  listeners.get('visibilitychange')();
  assert.deepEqual(unreadIds(context), ['task']);
  context.task.latest_follow_up_reply_at = replyTime(1);
  vm.runInContext("currentDetailTab = 'output'; renderDetail(task)", context);
  assert.deepEqual(unreadIds(context), ['task']);
  context.task.latest_follow_up_reply_at = replyTime(2);
  document.visibilityState = 'hidden';
  vm.runInContext('renderDetail(task)', context);
  document.visibilityState = 'visible';
  listeners.get('visibilitychange')();
  assert.deepEqual(unreadIds(context), []);
});

test('viewing a response ahead of the board poll does not reflag it', () => {
  const { context } = client();
  unreadSnapshot(context, replyTime(1));
  context.task = { id: 'task', status: 'completed', latest_follow_up_reply_at: replyTime(2), messages: [] };
  vm.runInContext('renderDetail(task)', context);
  unreadSnapshot(context, replyTime(2));
  assert.deepEqual(unreadIds(context), []);
});

test('failed or cancelled follow-ups do not invent replies and deleted tasks are removed', () => {
  const { context, storage } = client();
  unreadSnapshot(context, null, { chat_pending: true });
  unreadSnapshot(context, null, { chat_pending: false, status: 'failed' });
  assert.deepEqual(unreadIds(context), []);
  unreadSnapshot(context, replyTime(1));
  vm.runInContext('trackUnreadReplies({ results: [] })', context);
  assert.deepEqual(JSON.parse(storage.get(unreadKey)).ids, []);
});

test('existing poll detects and renders unread replies without additional requests', async () => {
  const { context, document } = client();
  vm.runInContext('selectedTaskId = null; updateLayoutMetrics = () => {};', context);
  let requests = 0;
  let version = 1;
  context.fetch = async url => {
    requests++;
    assert.match(url, /^\/api\/state(\?since=[^&]+)?$/);
    return { json: async () => ({ version, queue: [], workers: [], stats: {}, results: [{
      id: 'task', status: 'completed', label: 'Task', latest_follow_up_reply_at: replyTime(version)
    }] }) };
  };
  await vm.runInContext('originalPoll()', context);
  version++;
  await vm.runInContext('originalPoll()', context);
  assert.equal(requests, 2);
  assert.deepEqual(unreadIds(context), ['task']);
  assert.match(document.getElementById('results-list').innerHTML, /New reply/);
});

test('unavailable or corrupt storage does not break reply tracking', () => {
  const { context } = client(new Map([[unreadKey, 'broken JSON']]));
  context.localStorage.setItem = () => { throw new Error('blocked'); };
  unreadSnapshot(context, null);
  unreadSnapshot(context, replyTime(1));
  assert.deepEqual(unreadIds(context), ['task']);
});

test('read markers synchronize between tabs without a request', () => {
  const { context, listeners } = client();
  unreadSnapshot(context, null);
  unreadSnapshot(context, replyTime(1));
  listeners.get('storage')({ key: unreadKey, newValue: JSON.stringify({ ids: [], checkedThrough: replyTime(1) }) });
  assert.deepEqual(unreadIds(context), []);
  unreadSnapshot(context, replyTime(1));
  assert.deepEqual(unreadIds(context), []);
});

test('follow-up indicator does not promote a result in completion order', () => {
  const { context, document } = client();
  context.cards = [
    { id: 'older', label: 'OLDER_CARD', status: 'completed', completed_at: '2020-01-01T00:00:00Z' },
    { id: 'newer', label: 'NEWER_CARD', status: 'completed', completed_at: '2020-01-02T00:00:00Z' }
  ];
  for (const pending of [false, true, false]) {
    context.cards[0].chat_pending = pending;
    context.cards[0].last_chat_at = '2026-09-26T00:00:00Z';
    vm.runInContext('state.results = cards; renderResults()', context);
    const html = document.getElementById('results-list').innerHTML;
    assert.ok(html.indexOf('NEWER_CARD') < html.indexOf('OLDER_CARD'));
    assert.equal(html.includes('awaiting reply'), pending);
  }
});

test('background activity badges update cards and details without promoting results', () => {
  const { context, document } = client();
  context.cards = [
    { id: 'task', label: 'OLDER_CARD', status: 'completed', completed_at: '2020-01-01T00:00:00Z', runner_state: 'idle', messages: [] },
    { id: 'newer', label: 'NEWER_CARD', status: 'completed', completed_at: '2020-01-02T00:00:00Z' }
  ];
  for (const [activity, label] of [
    [{ task_count: 2, preparing_reply: false }, 'Background work'],
    [{ task_count: 1, preparing_reply: false }, 'Background work'],
    [{ task_count: 0, preparing_reply: true }, 'Preparing reply'],
    [{ task_count: 0, preparing_reply: false }, null]
  ]) {
    context.cards[0].background_activity = activity;
    vm.runInContext('state.results = cards; renderResults(); renderDetail(cards[0]);', context);
    const html = document.getElementById('results-list').innerHTML;
    assert.ok(html.indexOf('NEWER_CARD') < html.indexOf('OLDER_CARD'));
    const detail = document.getElementById('detail-background-activity');
    if (label) {
      assert.ok(html.includes(label));
      assert.ok(detail.innerHTML.includes(label));
      assert.equal(detail.style.display, 'inline-flex');
      assert.ok(html.includes('awaiting-reply'));
    } else {
      assert.ok(!html.includes('background-activity'));
      assert.equal(detail.style.display, 'none');
    }
  }
  assert.equal(vm.runInContext("backgroundActivityHtml({runner_state: 'active'})", context), '');
  assert.equal(vm.runInContext("backgroundActivityHtml({runner_state: 'idle'})", context), '');
});

test('background activity uses the existing board poll without extra requests', async () => {
  const { context, document } = client();
  vm.runInContext('selectedTaskId = null; updateLayoutMetrics = () => {};', context);
  let requests = 0;
  let version = 1;
  context.fetch = async url => {
    requests++;
    assert.match(url, /^\/api\/state(\?since=[^&]+)?$/);
    return { json: async () => ({ version, queue: [], workers: [], stats: {}, results: [{
      id: 'task', status: 'completed', label: 'Task',
      background_activity: { task_count: version === 1 ? 2 : 0, preparing_reply: version === 2 }
    }] }) };
  };
  await vm.runInContext('originalPoll()', context);
  assert.match(document.getElementById('results-list').innerHTML, />Background work<\/span>/);
  assert.doesNotMatch(document.getElementById('results-list').innerHTML, /Background work ·/);
  version++;
  await vm.runInContext('originalPoll()', context);
  assert.match(document.getElementById('results-list').innerHTML, /Preparing reply/);
  version++;
  await vm.runInContext('originalPoll()', context);
  assert.doesNotMatch(document.getElementById('results-list').innerHTML, /background-activity/);
  assert.equal(requests, 3);
});

test('chat textarea grows with content and shrinks back to one line', () => {
  const { context, document } = client();
  const input = document.getElementById('chat-input');
  input.scrollHeight = 78;
  vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '80px');
  assert.equal(input.style.overflowY, 'hidden');

  input.scrollHeight = 34;
  vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '36px');
  assert.equal(input.style.overflowY, 'hidden');
});

test('chat textarea caps at the smaller of 240px and 35vh and scrolls only above it', () => {
  const { context, document } = client();
  const input = document.getElementById('chat-input');
  context.innerHeight = 400;
  input.scrollHeight = 200;
  vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '140px');
  assert.equal(input.style.overflowY, 'auto');

  input.scrollHeight = 138;
  vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '140px');
  assert.equal(input.style.overflowY, 'hidden');

  context.innerHeight = 1000;
  input.scrollHeight = 300;
  vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '240px');
  assert.equal(input.style.overflowY, 'auto');
});

test('sending clears and resets the chat textarea before the request completes', async () => {
  const { context, document } = client();
  const input = document.getElementById('chat-input');
  Object.defineProperty(input, 'scrollHeight', { get() { return input.value ? 98 : 34; } });
  input.value = 'two\nlines';
  vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '100px');

  let finishRequest;
  context.fetch = () => new Promise(resolve => { finishRequest = resolve; });
  const sending = vm.runInContext('sendChat()', context);
  assert.equal(input.value, '');
  assert.equal(input.style.height, '36px');
  assert.equal(input.style.overflowY, 'hidden');
  finishRequest({ ok: true, status: 202 });
  await sending;
  assert.equal(input.style.height, '36px');
});

test('chat textarea keeps Enter to send and Shift+Enter for a newline', () => {
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  assert.match(html, /id="chat-input"[^>]*rows="1"[^>]*oninput="resizeChatInput\(this\)"[^>]*onkeydown="if\(event\.key==='Enter'&&!event\.shiftKey\)\{event\.preventDefault\(\);sendChat\(\)\}"/s);
});

test('request errors are shown with their server-provided reason and draft restored', async () => {
  const { context, document } = client();
  context.fetch = async () => ({ ok: false, json: async () => ({ error: 'Hive owns protocol/session flags' }) });
  const input = document.getElementById('chat-input');
  Object.defineProperty(input, 'scrollHeight', { get() { return input.value ? 98 : 34; } });
  input.value = 'steer';
  await vm.runInContext('sendChat()', context);
  assert.equal(document.getElementById('message-text').textContent, 'Hive owns protocol/session flags');
  assert.equal(input.value, 'steer');
  assert.equal(input.style.height, '100px');
  assert.equal(input.style.overflowY, 'hidden');
});

test('transport errors are visible and retry reuses the original Hive message ID', async () => {
  const { context, document } = client();
  const ids = [];
  context.fetch = async (_url, options) => { ids.push(JSON.parse(options.body).message_id); throw new Error('offline'); };
  const input = document.getElementById('chat-input');
  Object.defineProperty(input, 'scrollHeight', { get() { return input.value ? 98 : 34; } });
  input.value = 'steer';
  await vm.runInContext('sendChat()', context);
  assert.match(document.getElementById('message-text').textContent, /Connection lost/);
  assert.equal(input.style.height, '100px');
  await vm.runInContext('sendChat()', context);
  assert.equal(ids[0], ids[1]);
});

test('server failures remain visible and preserve the retry message ID', async () => {
  const { context, document } = client();
  const ids = [];
  context.fetch = async (_url, options) => {
    ids.push(JSON.parse(options.body).message_id);
    return { ok: false, status: 500, json: async () => ({ error: 'Cannot persist input safely' }) };
  };
  document.getElementById('chat-input').value = 'steer';
  await vm.runInContext('sendChat()', context);
  await vm.runInContext('sendChat()', context);
  assert.equal(document.getElementById('message-text').textContent, 'Cannot persist input safely');
  assert.equal(ids[0], ids[1]);
});

test('background runner errors and message rejection reasons appear escaped in detail', () => {
  const { context, document } = client();
  context.task = { id: 'task', status: 'failed', label: 'Task', output: '',
    failure_details: 'Claude lacks <msg_lifecycle_v1>',
    messages: [{ role: 'user', kind: 'follow_up', content: 'hello', delivery: 'rejected', error: 'rejected <reason>' }] };
  vm.runInContext('renderDetail(task)', context);
  const html = document.getElementById('detail-body').innerHTML;
  assert.match(html, /Claude lacks &lt;msg_lifecycle_v1&gt;/);
  assert.match(html, /rejected &lt;reason&gt;/);
  assert.match(html, /role="alert"/);
});

test('canonical messages render one answer after steering, with reviews labelled', () => {
  const { context, document } = client();
  context.task = { id: 'task', status: 'completed', label: 'Task', messages: [
    { role: 'user', kind: 'initial', content: 'INITIAL_PROMPT' },
    { role: 'user', kind: 'steering', content: 'STEERING' },
    { role: 'assistant', kind: 'initial', content: 'UNIQUE_FINAL' },
    { role: 'user', kind: 'review', content: 'CHECK', review_id: 'r1' },
    { role: 'assistant', kind: 'review', content: 'REVIEW_ANSWER', review_id: 'r1' },
    { role: 'user', kind: 'review', content: 'NOT_SENT', delivery: 'scheduled' }
  ] };
  vm.runInContext('renderDetail(task)', context);
  const html = document.getElementById('detail-body').innerHTML;
  assert.equal(html.match(/UNIQUE_FINAL/g).length, 1);
  assert.ok(html.indexOf('STEERING') < html.indexOf('UNIQUE_FINAL'));
  assert.match(html, /Self-review prompt/);
  assert.match(html, /Self-review response/);
  assert.doesNotMatch(html, /NOT_SENT|INITIAL_PROMPT/);
  vm.runInContext("currentDetailTab = 'prompt'; renderDetail(task)", context);
  assert.match(document.getElementById('detail-body').innerHTML, /INITIAL_PROMPT/);
});

test('active task keeps chat available and acceptance never creates an assistant bubble', () => {
  const { context, document } = client();
  context.task = { id: 'task', status: 'running', label: 'Task', output: '',
    messages: [{ role: 'user', kind: 'steering', content: 'steer', delivery: 'accepted', harness_accepted: true }] };
  vm.runInContext('renderDetail(task)', context);
  assert.equal(document.getElementById('chat-area').style.display, 'flex');
  const html = document.getElementById('detail-body').innerHTML;
  assert.match(html, /class="chat-content">steer<\/div>/);
  assert.doesNotMatch(html, /chat-message assistant/);
});

test('typing line shows outside the chat only while a reply is on its way', () => {
  const { context, document } = client();
  const typing = document.getElementById('chat-typing');
  const body = document.getElementById('detail-body');
  const messages = [{ role: 'user', kind: 'follow_up', content: 'FOLLOW_UP', delivery: 'accepted' }];
  for (const [fields, display] of [
    [{ activity_kind: 'original', runner_state: 'active', chat_pending: true }, 'none'],
    [{ activity_kind: 'reply', runner_state: 'active', chat_pending: true }, 'flex'],
    [{ activity_kind: 'review', chat_pending: true }, 'flex'],
    [{ activity_kind: 'reply', runner_state: 'idle', background_activity: { task_count: 1 } }, 'none']
  ]) {
    context.task = { id: 'task', status: 'completed', label: 'Task', messages, ...fields };
    vm.runInContext("currentDetailTab = 'output'; renderDetail(task)", context);
    assert.equal(typing.style.display, display);
  }
  context.task = { id: 'task', status: 'completed', label: 'Task', messages, activity_kind: 'reply', runner_state: 'active', chat_pending: true };
  vm.runInContext("currentDetailTab = 'prompt'; renderDetail(task)", context);
  assert.equal(typing.style.display, 'none');

  // A reader at the end of the chat stays there; a reader higher up is not moved.
  for (const [scrollTop, expected] of [[500, 900], [100, 100]]) {
    Object.assign(body, { scrollHeight: 900, clientHeight: 400, scrollTop });
    typing.style.display = 'none';
    context.pinned = scrollTop === 500;
    vm.runInContext('detailPinned = pinned; showChatTyping(true)', context);
    assert.equal(body.scrollTop, expected);
  }
});

test('delivery metadata and diagnostic records do not clutter chat', () => {
  const { context, document } = client();
  for (const status of ['accepted', 'written', 'delivered', 'included']) {
    context.task = { id: 'task', status: 'completed', label: 'Task', output: '',
      diagnostics: [{ kind: 'stdin_queued', hive_message_id: 'INTERNAL_DEBUG_ID' }],
      messages: [{ role: 'user', kind: 'follow_up', content: '1', delivery: status, harness_accepted: true, settled: true }] };
    vm.runInContext('renderDetail(task)', context);
    const html = document.getElementById('detail-body').innerHTML;
    assert.match(html, /class="chat-content">1<\/div>/);
    assert.doesNotMatch(html, /Runner diagnostics|INTERNAL_DEBUG_ID|stdin_queued/);
  }
});

test('an unchanged-state reply keeps the board and asks with the current state next time', async () => {
  const { context, document } = client();
  vm.runInContext('selectedTaskId = null; updateLayoutMetrics = () => {};', context);
  const urls = [];
  let unchanged = false;
  context.fetch = async url => {
    urls.push(url);
    if (unchanged) return { status: 204, json: async () => { throw new Error('204 has no body'); } };
    return { status: 200, json: async () => ({ version: 7, instance: 'run-a', queue: [], workers: [], stats: {}, results: [{
      id: 'task', status: 'completed', label: 'Kept'
    }] }) };
  };
  await vm.runInContext('originalPoll()', context);
  unchanged = true;
  await vm.runInContext('originalPoll()', context);
  assert.deepEqual(urls, ['/api/state', '/api/state?since=run-a%3A7']);
  assert.match(document.getElementById('results-list').innerHTML, /Kept/);
});

test('deep search is project-aware and project switches schedule a new search', async () => {
  const { context } = client();
  context.urls = [];
  context.fetch = async url => {
    context.urls.push(url);
    return { json: async () => ({ version: 4, ids: ['match'] }) };
  };
  vm.runInContext(`
    configCache = { projects: [{id:'a', name:'A'}, {id:'b', name:'B'}], harnesses: [] };
    currentProjectId = 'a'; selectedTaskId = null; resultsQuery = 'needle'; deepSearch = true;
    state = fullState = {version: 4, queue: [], workers: [], results: [], stats: {}};
  `, context);
  await vm.runInContext('runDeepSearch()', context);
  assert.match(context.urls[0], /project=a/);
  assert.equal(vm.runInContext('deepMatches.projectId', context), 'a');

  let scheduled = 0;
  context.scheduleDeepSearch = () => scheduled++;
  context.render = () => {};
  context.renderProjectSwitcher = () => {};
  context.closeProjectMenu = () => {};
  vm.runInContext("switchProject('b')", context);
  assert.equal(scheduled, 1);
  assert.equal(vm.runInContext("deepMatchesTask({id:'match'})", context), false);
  await vm.runInContext('runDeepSearch()', context);
  assert.match(context.urls[1], /project=b/);
  assert.equal(vm.runInContext('deepMatches.projectId', context), 'b');
});

test('harness availability warning counts queued and completed affected tasks', () => {
  const { context } = client();
  vm.runInContext(`
    configCache = {
      projects: [{id:'p', use_shared_harnesses:true}],
      harnesses: [{id:'h', only:null}]
    };
    projectsLocal = [{id:'p', use_shared_harnesses:false}];
    harnessesLocal = [{id:'h', only:null}];
    fullState = {
      queue: [{id:'q', project_id:'p', harness:'h'}], workers: [],
      results: [{id:'r', project_id:'p', harness:'h'}]
    };
  `, context);
  const warning = vm.runInContext('harnessAvailabilityWarning()', context);
  assert.match(warning, /1 queued task/);
  assert.match(warning, /1 completed task/);
  vm.runInContext(`
    projectsLocal = [{id:'p', use_shared_harnesses:true}];
    harnessesLocal = [{id:'h', only:null}, {id:'new', only:['p']}];
  `, context);
  assert.equal(vm.runInContext('harnessAvailabilityWarning()', context), null);
});

test('project deletion requires the exact case-sensitive project name', () => {
  const { context, document } = client();
  vm.runInContext("configCache = { projects: [{id:'p', name:'Mobile app'}] }; currentProjectId = deletingProjectId = 'p';", context);
  const input = document.getElementById('delete-project-name');
  const button = document.getElementById('delete-project-confirm');
  for (const [value, disabled] of [['mobile app', true], [' Mobile app ', false]]) {
    input.value = value;
    vm.runInContext('updateDeleteProjectButton()', context);
    assert.equal(button.disabled, disabled);
  }
});

test('detail scroll position is kept per task and tab while switching', () => {
  const { context, document } = client();
  const body = document.getElementById('detail-body');
  Object.assign(body, { scrollHeight: 1000, clientHeight: 400, scrollTop: 0 });
  const show = (id, tab) => {
    context.shownTask = { id, status: 'completed', messages: [] };
    vm.runInContext(`currentDetailTab = '${tab}'; renderDetail(shownTask)`, context);
  };
  show('a', 'output');
  body.scrollTop = 300;
  vm.runInContext('onDetailScroll()', context);
  show('a', 'output');
  assert.equal(body.scrollTop, 300, 'a refresh of the same task keeps the position');
  show('b', 'output');
  assert.equal(body.scrollTop, 1000, 'a task opened for the first time follows the bottom');
  body.scrollTop = 120;
  vm.runInContext('onDetailScroll()', context);
  show('a', 'prompt');
  assert.equal(body.scrollTop, 0, 'each tab has its own position');
  show('a', 'output');
  assert.equal(body.scrollTop, 300);
  show('b', 'output');
  assert.equal(body.scrollTop, 120);
});

test('chat follows new output only when already at the bottom', () => {
  const { context, document } = client();
  const body = document.getElementById('detail-body');
  let html = '';
  Object.assign(body, { scrollHeight: 1000, clientHeight: 400, scrollTop: 0 });
  Object.defineProperty(body, 'innerHTML', {
    get() { return html; },
    set(value) { html = value; body.scrollHeight += 200; body.scrollTop = 0; }
  });
  context.task = { id: 'task', status: 'completed', messages: [] };
  vm.runInContext('renderDetail(task)', context);
  assert.equal(body.scrollTop, 1200, 'first open goes to the bottom');
  body.scrollTop = 800; // browser-clamped bottom
  vm.runInContext('renderDetail(task)', context);
  assert.equal(body.scrollTop, 1400, 'new output follows the bottom');
  body.scrollTop = 200;
  vm.runInContext('onDetailScroll()', context);
  context.task.messages = [{ role: 'assistant', content: 'new reply' }];
  vm.runInContext('renderDetail(task)', context);
  assert.equal(body.scrollTop, 200, 'reading older output keeps its position');
  assert.equal(document.getElementById('chat-jump-latest').style.display, '');
  vm.runInContext('jumpToLatest()', context);
  assert.equal(body.scrollTop, 1600);
  assert.equal(document.getElementById('chat-jump-latest').style.display, 'none');
});

test('delayed content growth follows only while pinned', () => {
  const { context, document } = client();
  const body = document.getElementById('detail-body');
  Object.assign(body, { scrollHeight: 1000, clientHeight: 400, scrollTop: 600 });
  vm.runInContext("detailRenderedKey = 'task:output'; detailPinned = true", context);
  body.scrollHeight = 1200;
  vm.runInContext('onDetailResize()', context);
  assert.equal(body.scrollTop, 1200);
  body.scrollTop = 100;
  vm.runInContext('onDetailScroll()', context);
  body.scrollHeight = 1400;
  vm.runInContext('onDetailResize()', context);
  assert.equal(body.scrollTop, 100);
});

test('resizing the composer preserves the chat position or follows its bottom', () => {
  const { context, document } = client();
  const body = document.getElementById('detail-body');
  const input = document.getElementById('chat-input');
  Object.assign(body, { scrollHeight: 1000, clientHeight: 400, scrollTop: 600 });
  Object.defineProperty(input.style, 'height', {
    set() { body.scrollTop = 50; }, configurable: true
  });
  vm.runInContext('resizeChatInput()', context);
  assert.equal(body.scrollTop, 1000, 'bottom remains visible when composer changes size');
  body.scrollTop = 200;
  vm.runInContext("detailRenderedKey = 'task:output'; onDetailScroll(); resizeChatInput()", context);
  assert.equal(body.scrollTop, 200, 'older output stays in place');
});

test('dropdown changes ask before closing editors but not action dialogs', () => {
  const { context, document } = client();
  const fields = { text: { type: 'text', value: '' }, select: { type: 'select-one', value: 'template-a' } };
  for (const id of ['batch-modal', 'settings-modal']) {
    document.getElementById(id).querySelectorAll = selector =>
      [fields.text, ...(selector.includes('select') ? [fields.select] : [])];
  }
  const changedBySelect = id => {
    fields.select.value = 'template-a';
    const before = vm.runInContext(`modalState('${id}')`, context);
    fields.select.value = 'template-b';
    return before !== vm.runInContext(`modalState('${id}')`, context);
  };
  assert.equal(changedBySelect('batch-modal'), false);
  assert.equal(changedBySelect('settings-modal'), true);

  const before = vm.runInContext("modalState('batch-modal')", context);
  fields.text.value = 'typed input';
  assert.notEqual(vm.runInContext("modalState('batch-modal')", context), before);
});

test('output action tags split into buttons only for configured tags', () => {
  const { context } = client();
  context.text = 'Run <run-id>ab12</run-id> and <other>zz</other> then <run-id>bad value</run-id>.';
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("splitOutputActions(text, ['run-id'])", context))), [
    'Run ', { tag: 'run-id', value: 'ab12' }, ' and <other>zz</other> then <run-id>bad value</run-id>.'
  ]);
  assert.deepEqual(Array.from(vm.runInContext("splitOutputActions('plain', ['run-id'])", context)), ['plain']);
  assert.deepEqual(Array.from(vm.runInContext("splitOutputActions('plain', [])", context)), ['plain']);
});

test('output action failures are shown and successes are silent', async () => {
  for (const ok of [true, false]) {
    const { context } = client();
    const dialogs = [];
    const requests = [];
    context.showMessage = (...args) => dialogs.push(args);
    context.fetch = async (url, options) => {
      requests.push([url, JSON.parse(options.body)]);
      return ok ? { ok: true } : { ok: false, json: async () => ({ error: "'tool' exited with status 1." }) };
    };
    context.button = { disabled: false, dataset: { outputAction: 'run-id', value: 'ab12' } };
    await vm.runInContext('runOutputAction(button)', context);
    assert.deepEqual(requests, [['/api/output_actions/run-id/run', { value: 'ab12' }]]);
    assert.equal(context.button.disabled, false);
    assert.deepEqual(dialogs.map(d => d[1]), ok ? [] : ['Action failed']);
  }
});

test('a missing login asks once to run hihive open and keeps the board', async () => {
  const { context } = client();
  const dialogs = [];
  context.showMessage = (...args) => dialogs.push(args);
  context.fetch = async () => ({ status: 401, ok: false, json: async () => ({ error: 'Missing or wrong Hive token.' }) });
  vm.runInContext('state = { results: [], workers: [], queue: [] }', context);
  await vm.runInContext('originalPoll()', context);
  await vm.runInContext('originalPoll()', context);
  assert.equal(dialogs.length, 1);
  assert.match(dialogs[0][0], /hihive open/);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(state)', context)), { results: [], workers: [], queue: [] });
});

async function runLogin(hash, response) {
  const steps = [];
  const status = { textContent: '' };
  const context = vm.createContext({
    URLSearchParams,
    location: { hash, replace(url) { steps.push(`go ${url}`); } },
    history: { replaceState(_s, _t, url) { steps.push(`address ${url}`); } },
    document: { getElementById: () => status },
    fetch: async (url, options) => { steps.push(`post ${options.body}`); return response; }
  });
  vm.runInContext(fs.readFileSync('src/hihive/static/login.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return { steps, status: status.textContent };
}

test('login page clears the secret from the address bar before sending it, then opens Hive', async () => {
  const { steps } = await runLogin('#token=s3cret', { ok: true });
  assert.deepEqual(steps, ['address /login', 'post {"token":"s3cret"}', 'go /']);
});

test('login page explains a missing or wrong secret', async () => {
  assert.match((await runLogin('', { ok: true })).status, /no Hive token/);
  const wrong = await runLogin('#token=bad', { ok: false });
  assert.match(wrong.status, /wrong/);
  assert.ok(!wrong.steps.includes('go /'));
});
