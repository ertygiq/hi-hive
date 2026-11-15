const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');

function client(storage = new Map()) {
  const elements = new Map();
  const listeners = new Map();
  const element = () => ({ style: {}, value: '', textContent: '', innerHTML: '', classList: { add() {}, remove() {}, toggle() {} }, querySelectorAll() { return []; } });
  const document = {
    visibilityState: 'visible',
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
    setInterval() {}, setTimeout() {}, clearTimeout() {}, crypto: { randomUUID }, console });
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

test('request errors are shown with their server-provided reason and draft restored', async () => {
  const { context, document } = client();
  context.fetch = async () => ({ ok: false, json: async () => ({ error: 'Hive owns protocol/session flags' }) });
  document.getElementById('chat-input').value = 'steer';
  await vm.runInContext('sendChat()', context);
  assert.equal(document.getElementById('message-text').textContent, 'Hive owns protocol/session flags');
  assert.equal(document.getElementById('chat-input').value, 'steer');
});

test('transport errors are visible and retry reuses the original Hive message ID', async () => {
  const { context, document } = client();
  const ids = [];
  context.fetch = async (_url, options) => { ids.push(JSON.parse(options.body).message_id); throw new Error('offline'); };
  document.getElementById('chat-input').value = 'steer';
  await vm.runInContext('sendChat()', context);
  assert.match(document.getElementById('message-text').textContent, /Connection lost/);
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
    [{ chat_pending: true }, 'flex'],
    [{ background_activity: { task_count: 0, preparing_reply: true } }, 'flex'],
    [{ background_activity: { task_count: 1, preparing_reply: false } }, 'none'],
    [{ chat_pending: false }, 'none']
  ]) {
    context.task = { id: 'task', status: 'completed', label: 'Task', messages, ...fields };
    vm.runInContext("currentDetailTab = 'output'; renderDetail(task)", context);
    assert.equal(typing.style.display, display);
  }
  context.task = { id: 'task', status: 'completed', label: 'Task', messages, chat_pending: true };
  vm.runInContext("currentDetailTab = 'prompt'; renderDetail(task)", context);
  assert.equal(typing.style.display, 'none');

  // A reader at the end of the chat stays there; a reader higher up is not moved.
  for (const [scrollTop, expected] of [[500, 900], [100, 100]]) {
    Object.assign(body, { scrollHeight: 900, clientHeight: 400, scrollTop });
    typing.style.display = 'none';
    vm.runInContext('showChatTyping(true)', context);
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

test('detail scroll position is kept per task and tab while switching', () => {
  const { context, document } = client();
  const body = document.getElementById('detail-body');
  const show = (id, tab) => {
    context.shownTask = { id, status: 'completed', messages: [] };
    vm.runInContext(`currentDetailTab = '${tab}'; renderDetail(shownTask)`, context);
  };
  show('a', 'output');
  body.scrollTop = 300;
  show('a', 'output');
  assert.equal(body.scrollTop, 300, 'a refresh of the same task keeps the position');
  show('b', 'output');
  assert.equal(body.scrollTop, 0, 'a task opened for the first time starts at the top');
  body.scrollTop = 120;
  show('a', 'prompt');
  assert.equal(body.scrollTop, 0, 'each tab has its own position');
  show('a', 'output');
  assert.equal(body.scrollTop, 300);
  show('b', 'output');
  assert.equal(body.scrollTop, 120);
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
