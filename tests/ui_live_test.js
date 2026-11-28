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
    classList: { add() {}, remove() {}, toggle() {} }, setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; }, focus() {}, querySelectorAll() { return []; } });
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

test('session import previews untrusted text and imports into selected project', async () => {
  const { context, document } = client();
  vm.runInContext("configCache = {projects:[{id:'p', available_harnesses:['h'], default_harness:'h'}], harnesses:[{id:'h', session_import_supported:true}]}; currentProjectId = 'p'; setImportMode(true)", context);
  document.getElementById('import-harness').value = 'h';
  document.getElementById('import-session-id').value = 'exact-id';
  const requests = [];
  context.fetch = async (url, options) => {
    requests.push([url, JSON.parse(options.body)]);
    return {ok:true, json:async () => url.endsWith('/preview')
      ? {prompt:'<script>text</script>', answer:'', message_count:3, last_activity:'2026-01-01', preview_token:'token'} : {id:'new-task'}};
  };
  await vm.runInContext('lookupImportSession()', context);
  assert.equal(document.getElementById('import-prompt').textContent, '<script>text</script>');
  assert.equal(document.getElementById('import-answer').textContent, '(No readable answer)');
  assert.equal(document.getElementById('import-submit').disabled, false);
  document.getElementById('import-label').value = 'My session';
  let selected;
  context.poll = async () => {};
  context.selectTask = async id => { selected = id; };
  await vm.runInContext('submitImportSession()', context);
  assert.equal(selected, 'new-task');
  assert.deepEqual(requests, [
    ['/api/session-import/preview', {project_id:'p', harness_id:'h', session_id:'exact-id'}],
    ['/api/session-import', {project_id:'p', harness_id:'h', session_id:'exact-id', preview_token:'token', label:'My session'}]
  ]);
});

test('session import rejects paths and unsupported harnesses and ignores stale lookup', async () => {
  const { context, document } = client();
  vm.runInContext("configCache = {projects:[{id:'p'}], harnesses:[{id:'h', session_import_supported:true}]}; currentProjectId = 'p'; setImportMode(true)", context);
  document.getElementById('import-harness').value = 'h';
  const input = document.getElementById('import-session-id');
  input.value = '/tmp/session.jsonl';
  let requests = 0;
  context.fetch = () => { requests++; };
  await vm.runInContext('lookupImportSession()', context);
  assert.equal(requests, 0);
  assert.match(document.getElementById('import-status').textContent, /exact session ID/);
  input.value = 'first';
  let resolve;
  context.fetch = () => new Promise(done => { resolve = done; });
  const pending = vm.runInContext('lookupImportSession()', context);
  input.value = 'second'; vm.runInContext('invalidateImportPreview()', context);
  resolve({ok:true, json:async () => ({prompt:'stale', answer:'stale'})});
  await pending;
  assert.equal(document.getElementById('import-preview').style.display, 'none');
  assert.equal(document.getElementById('import-submit').disabled, true);
  vm.runInContext('configCache.harnesses[0].session_import_supported = false', context);
  await vm.runInContext('lookupImportSession()', context);
  assert.match(document.getElementById('import-status').textContent, /does not support/);
});

test('session import uses a word boundary for labels and compacts only preview blank lines', async () => {
  const { context, document } = client();
  vm.runInContext("configCache = {projects:[{id:'p'}], harnesses:[{id:'h', session_import_supported:true}]}; currentProjectId = 'p'; setImportMode(true)", context);
  document.getElementById('import-harness').value = 'h';
  document.getElementById('import-session-id').value = 'exact-id';
  const prompt = 'A long sentence that reaches the limit before another extraordinary word\n\n\nNext line';
  context.fetch = async () => ({ok:true, json:async () => ({prompt, answer:'First\n\n\nLast', preview_token:'token'})});
  await vm.runInContext('lookupImportSession()', context);
  const label = document.getElementById('import-label').value;
  assert.ok(label.length <= 60);
  assert.ok(prompt.replace(/\s+/g, ' ').startsWith(label + ' '));
  assert.equal(document.getElementById('import-prompt').textContent, prompt.replace(/\n\s*\n+/g, '\n'));
  assert.equal(document.getElementById('import-answer').textContent, 'First\nLast');
});

test('session import entry and selector show only supported project harnesses', async () => {
  const { context, document } = client();
  context.loadConfig = async () => {};
  vm.runInContext("configCache = {projects:[{id:'p', available_harnesses:['native','wrapper'], default_harness:'wrapper'}], harnesses:[{id:'native',name:'Native',session_import_supported:true},{id:'wrapper',name:'Wrapper',session_import_supported:false}]}; currentProjectId = 'p'", context);
  await vm.runInContext('openAddTask()', context);
  assert.equal(document.getElementById('import-mode-toggle').style.display, '');
  assert.match(document.getElementById('import-harness').innerHTML, /Native/);
  assert.doesNotMatch(document.getElementById('import-harness').innerHTML, /Wrapper/);
  vm.runInContext('configCache.harnesses[0].session_import_supported = false', context);
  await vm.runInContext('openAddTask()', context);
  assert.equal(document.getElementById('import-mode-toggle').style.display, 'none');
});

test('session import blocks closing during submission and warns on uncertain delivery', async () => {
  const { context, document } = client();
  vm.runInContext("configCache = {projects:[{id:'p'}], harnesses:[{id:'h', session_import_supported:true}]}; currentProjectId = 'p'; setImportMode(true)", context);
  document.getElementById('import-harness').value = 'h';
  document.getElementById('import-session-id').value = 'exact-id';
  context.fetch = async () => ({ok:true, json:async () => ({prompt:'hello', answer:'world', preview_token:'token'})});
  await vm.runInContext('lookupImportSession()', context);
  let reject;
  context.fetch = () => new Promise((_, fail) => { reject = fail; });
  const pending = vm.runInContext('submitImportSession()', context);
  assert.equal(document.getElementById('import-session-id').disabled, true);
  assert.equal(document.getElementById('add-task-cancel').disabled, true);
  await vm.runInContext("requestCloseModal('add-task-modal')", context);
  assert.equal(document.getElementById('import-status').textContent, 'Importing…');
  reject(new Error('offline'));
  await pending;
  assert.match(document.getElementById('import-status').textContent, /may already be imported/);
  assert.equal(document.getElementById('add-task-cancel').disabled, false);
});

test('session import shows lookup errors inline and keeps the session ID', async () => {
  const { context, document } = client();
  vm.runInContext("configCache = {projects:[{id:'p'}], harnesses:[{id:'h', session_import_supported:true}]}; currentProjectId = 'p'; setImportMode(true)", context);
  document.getElementById('import-harness').value = 'h';
  const input = document.getElementById('import-session-id');
  input.value = 'exact-id';
  for (const message of ['Session not found for this project and harness.', 'This session already belongs to another task.']) {
    context.fetch = async () => ({ok:false, json:async () => ({error:message})});
    await vm.runInContext('lookupImportSession()', context);
    assert.equal(document.getElementById('import-status').textContent, message);
    assert.equal(document.getElementById('import-submit').disabled, true);
    assert.equal(input.value, 'exact-id');
  }
  context.fetch = async () => { throw new Error('offline'); };
  await vm.runInContext('lookupImportSession()', context);
  assert.match(document.getElementById('import-status').textContent, /Could not search/);
});

test('imported task omits duration and shows missing imported text in detail', () => {
  const { context, document } = client();
  context.task = {id:'task', label:'Imported', status:'completed', imported_session:true,
    started_at:'2020-01-01T00:00:00Z', completed_at:'2020-01-01T00:01:15Z',
    messages:[{role:'user',kind:'initial',content:''}]};
  const card = vm.runInContext('renderResultCard(task, 0, new Map())', context);
  assert.doesNotMatch(card, /1m 15s/);
  vm.runInContext("currentDetailTab = 'prompt'; renderDetail(task)", context);
  assert.match(document.getElementById('detail-body').innerHTML, /\(No readable prompt\)/);
  vm.runInContext("currentDetailTab = 'output'; renderDetail(task)", context);
  assert.match(document.getElementById('detail-body').innerHTML, /\(No readable answer\)/);
  assert.doesNotMatch(document.getElementById('detail-body').innerHTML, /No output yet/);
  context.task.messages.push({role:'assistant',kind:'follow_up',content:'Later answer'});
  vm.runInContext('renderDetail(task)', context);
  assert.match(document.getElementById('detail-body').innerHTML, /\(No readable answer\)[^]*Later answer/);
  context.task.messages.push({role:'assistant',kind:'initial',content:'Imported answer'});
  vm.runInContext('renderDetail(task)', context);
  assert.doesNotMatch(document.getElementById('detail-body').innerHTML, /\(No readable answer\)/);
  context.task.imported_session = false;
  assert.match(vm.runInContext('renderResultCard(task, 0, new Map())', context), /1m 15s/);
});

test('imported result hides retry and warns before deleting the Hive task', async () => {
  const { context } = client();
  context.task = {id:'imported', label:'Imported', status:'completed', imported_session:true};
  vm.runInContext('state.results = [task]', context);
  assert.doesNotMatch(vm.runInContext('renderResultCard(task, 0, new Map())', context), /title="Retry"/);
  let message;
  context.showConfirm = async text => { message = text; return false; };
  await vm.runInContext("deleteResultTask('imported')", context);
  assert.match(message, /original harness session stays/);
});

test('imported follow-up requires first ownership confirmation and preserves cancelled draft', async () => {
  const { context, document } = client();
  context.task = {id:'task', status:'completed', imported_session:true, imported_session_confirmed:false};
  vm.runInContext('state.results = [task]; displayedConversation = task', context);
  const input = document.getElementById('chat-input'); input.value = 'Hello';
  let requests = 0, posted, options;
  context.showConfirm = async (_message, opts) => { options = opts; return false; };
  context.fetch = async (_url, request) => { requests++; posted = JSON.parse(request.body); return {ok:true, status:202}; };
  await vm.runInContext('sendChat()', context);
  assert.equal(requests, 0); assert.equal(input.value, 'Hello');
  assert.equal(options.title, 'Let Hive use this session'); assert.equal(options.confirmLabel, 'Send');
  context.showConfirm = async () => true;
  await vm.runInContext('sendChat()', context);
  assert.equal(requests, 1); assert.equal(posted.imported_session_confirmed, true);
  context.task.imported_session_confirmed = true; input.value = 'Again';
  context.showConfirm = async () => assert.fail('confirmed task should not prompt');
  await vm.runInContext('sendChat()', context);
  assert.equal(requests, 2); assert.equal(posted.imported_session_confirmed, undefined);
});

test('imported fork requires first ownership confirmation', async () => {
  const { context, document } = client();
  context.task = {id:'task', status:'completed', imported_session:true, imported_session_confirmed:false};
  vm.runInContext("state.results = [task]; forkParentId = 'task'", context);
  document.getElementById('fork-prompt').value = 'Fork prompt';
  let sent, options;
  context.showConfirm = async (_message, opts) => { options = opts; return false; };
  context.postJson = async (path, body) => { sent = [path, body]; return null; };
  await vm.runInContext('submitForkTask()', context);
  assert.equal(sent, undefined); assert.equal(document.getElementById('fork-prompt').value, 'Fork prompt');
  assert.equal(options.confirmLabel, 'Fork');
  context.showConfirm = async () => true;
  await vm.runInContext('submitForkTask()', context);
  assert.equal(sent[0], '/api/tasks/task/fork'); assert.equal(sent[1].imported_session_confirmed, true);
});

test('paused banner Resume appears only for an open phone overlay without changing its height', () => {
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  assert.match(html, /Paused in all projects - queued tasks won't start\./);
  assert.match(html, /id="detail-title" tabindex="-1"/);
  assert.match(css, /@media \(max-width: 899px\)\s*\{\s*\.paused-banner\s*\{\s*min-height:\s*45px/);
  assert.doesNotMatch(css.match(/\.paused-banner\s*\{[^}]*\}/s)[0], /min-height/);
  assert.match(css, /#paused-banner button\s*\{\s*display:\s*none/);
  assert.match(css, /@media \(max-width: 899px\)\s*\{[^]*?body:has\(#detail-panel:not\(\.hidden\)\) #paused-banner button\s*\{\s*display:\s*inline-flex/);
});

test('banner Resume restores keyboard focus on success, close and external resume', async () => {
  const { context, document, listeners } = client();
  const bannerButton = document.getElementById('banner-resume-btn');
  const heading = document.getElementById('detail-title');
  const toolbar = document.getElementById('pause-btn');
  heading.focus = () => { document.activeElement = heading; };
  toolbar.focus = () => { document.activeElement = toolbar; };
  context.innerWidth = 390;
  vm.runInContext('selectedTaskId = "task"; state.paused = true; updateLayoutMetrics = () => {}; render = () => {};', context);
  context.postJson = async () => null;
  document.activeElement = bannerButton;
  await vm.runInContext('resumeFromBanner()', context);
  assert.equal(document.activeElement, heading);
  document.activeElement = bannerButton;
  context.postJson = async () => 'offline';
  const messages = []; context.showMessage = (...args) => messages.push(args);
  await vm.runInContext('resumeFromBanner()', context);
  assert.equal(document.activeElement, bannerButton);
  assert.deepEqual(messages[0], ['offline', 'Could not resume']);
  vm.runInContext('state.paused = false; renderPausedBanner()', context);
  assert.equal(document.activeElement, heading);
  document.activeElement = bannerButton;
  vm.runInContext('selectedTaskId = "task"; closeDetail()', context);
  assert.equal(document.activeElement, toolbar);
  document.activeElement = heading;
  vm.runInContext('selectedTaskId = "task"; closeDetail()', context);
  assert.equal(document.activeElement, heading);
  document.activeElement = bannerButton;
  context.innerWidth = 1000;
  // The production resize listener is installed in the initialization section;
  // the same focus decision is exercised directly here.
  vm.runInContext('focusAfterBannerResumeHidden()', context);
  assert.equal(document.activeElement, toolbar);
});

test('banner focus recovers when the browser moves focus to BODY before resize', () => {
  const { context, document } = client();
  const banner = document.getElementById('paused-banner');
  banner.classList = { contains: () => true };
  const toolbar = document.getElementById('pause-btn');
  let focused = false, scheduled;
  toolbar.focus = () => { focused = true; };
  document.body = {}; document.activeElement = document.body;
  context.innerWidth = 1000;
  context.getComputedStyle = () => ({ display: 'none' });
  context.setTimeout = callback => { scheduled = callback; };
  context.event = { relatedTarget: null };
  vm.runInContext('onBannerResumeBlur(event)', context);
  scheduled();
  assert.equal(focused, true);
  focused = false;
  context.getComputedStyle = () => ({ display: 'inline-flex' });
  vm.runInContext('onBannerResumeBlur(event)', context);
  scheduled();
  assert.equal(focused, false);
});

test('message dialog focuses OK and restores its connected opener', () => {
  const { context, document } = client();
  const opener = { isConnected: true, focus() { document.activeElement = this; } };
  document.activeElement = opener;
  const ok = document.getElementById('message-ok-btn');
  ok.focus = () => { document.activeElement = ok; };
  vm.runInContext("showMessage('offline', 'Could not resume')", context);
  assert.equal(document.activeElement, ok);
  vm.runInContext("closeModal('message-modal')", context);
  assert.equal(document.activeElement, opener);
});

test('button variants, sizes, toggles and disabled hover share consistent rules', () => {
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  assert.match(css, /button\s*\{[^}]*font-size:\s*13px;[^}]*line-height:\s*15px;[^}]*min-height:\s*29px/s);
  assert.match(css, /button\.small\s*\{[^}]*font-size:\s*12px;[^}]*line-height:\s*16px;[^}]*min-height:\s*24px/s);
  assert.match(css, /button\.icon-btn\.small\s*\{[^}]*width:\s*20px;[^}]*height:\s*20px/s);
  assert.match(css, /button\.primary:not\(:disabled\):not\(\[aria-disabled="true"\]\):hover/);
  assert.match(css, /button\.danger:not\(:disabled\):not\(\[aria-disabled="true"\]\):hover/);
  assert.match(css, /button:disabled, button\[aria-disabled="true"\], select:disabled\s*\{[^}]*opacity:\s*0\.5/s);
  assert.match(css, /button:focus-visible, select:focus-visible/);
  assert.match(css, /button\.toggle\[aria-pressed="true"\]/);
  assert.match(css, /\.results-sort\s*\{[^}]*height:\s*24px/s);
  assert.match(html, /id="results-hide-done" class="small toggle"[^]*?aria-pressed="false"/);
  assert.match(html, /id="results-deep-search" class="small toggle"[^]*?aria-pressed="false"/);
  assert.match(html, /class="small toggle" id="pause-btn"[^>]*>Pause/);
  assert.doesNotMatch(html.match(/<button[^>]*id="pause-btn"[^>]*>/)[0], /aria-pressed/);
  assert.match(html, /id="detail-cancel-btn"[^>]*>Cancel task/);
  assert.match(html, /id="header-run-template"[^>]*>Run template/);
  assert.match(html, /id="restart-server-btn"[^>]*>Restart server/);
});

test('Results toggles expose aria-pressed and Pause swaps labels without a pressed state', () => {
  const { context, document } = client();
  const pause = document.getElementById('pause-btn');
  const classes = new Set();
  pause.classList = { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); } };
  context.snapshot = { workers: [], queue: [], stats: {}, max_parallel: 4, paused: false };
  vm.runInContext('state = snapshot; renderStats(); hideDone = false; deepSearch = false; syncHideDoneButton(); syncDeepSearchButton()', context);
  assert.equal(pause.textContent, 'Pause'); assert.equal(classes.has('is-on'), false);
  assert.equal(document.getElementById('results-hide-done')['aria-pressed'], 'false');
  assert.equal(document.getElementById('results-deep-search')['aria-pressed'], 'false');
  context.snapshot.paused = true;
  vm.runInContext('renderStats(); hideDone = true; deepSearch = true; syncHideDoneButton(); syncDeepSearchButton()', context);
  assert.equal(pause.textContent, 'Resume'); assert.equal(classes.has('is-on'), true);
  assert.equal(pause['aria-pressed'], undefined);
  assert.equal(document.getElementById('results-hide-done')['aria-pressed'], 'true');
  assert.equal(document.getElementById('results-deep-search')['aria-pressed'], 'true');
});

test('confirm severity, labels and safe focus are set per invocation', async () => {
  const { context, document } = client();
  let focused = '';
  document.activeElement = { isConnected: true, focus() { focused = 'opener'; } };
  const accept = document.getElementById('confirm-accept-btn');
  accept.classList = { toggle(name, value) { assert.equal(name, 'danger'); accept.danger = value; } };
  document.getElementById('confirm-cancel-btn').focus = () => { focused = 'cancel'; };
  context.options = { confirmLabel: 'Delete', cancelLabel: 'Keep', danger: true };
  let pending = vm.runInContext("showConfirm('Delete?', options)", context);
  assert.equal(focused, 'cancel');
  assert.equal(accept.textContent, 'Delete'); assert.equal(accept.danger, true);
  assert.equal(document.getElementById('confirm-cancel-btn').textContent, 'Keep');
  vm.runInContext('resolveConfirm(true)', context);
  assert.equal(await pending, true); assert.equal(focused, 'opener');
  pending = vm.runInContext("showConfirm('Continue?')", context);
  assert.equal(accept.danger, false); assert.equal(accept.textContent, 'Yes');
  assert.equal(document.getElementById('confirm-cancel-btn').textContent, 'No');
  vm.runInContext('resolveConfirm(false)', context);
  assert.equal(await pending, false);
  const source = fs.readFileSync('src/hihive/static/app.js', 'utf8');
  for (const text of ['Cancel task', 'Cancel all', 'Delete', 'Restart', 'Discard', 'Discard changes']) {
    assert.match(source, new RegExp(`confirmLabel: '${text}', danger: true`));
  }
  assert.match(source, /confirmLabel: 'Mark not done'/);
  assert.match(source, /confirmLabel: 'Save anyway'/);
});

test('composer one-line, focus, disabled and growth retain aligned sizing', () => {
  const { context, document } = client();
  const input = document.getElementById('chat-input');
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.chat-input-area\s*\{[^}]*align-items:\s*flex-end/s);
  assert.match(css, /\.chat-input-area textarea\s*\{[^}]*min-height:\s*36px/s);
  assert.match(css, /#chat-send-btn\s*\{[^}]*min-height:\s*36px;[^}]*padding:\s*0 14px;[^}]*align-items:\s*center;[^}]*border-radius:\s*var\(--radius\)/s);
  assert.match(css, /\.chat-input-area textarea:focus\s*\{[^}]*border-color:\s*var\(--primary\)/s);
  assert.match(css, /button:disabled, button\[aria-disabled="true"\]/);
  input.scrollHeight = 20; vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '36px');
  input.scrollHeight = 100; vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '102px');
  input.scrollHeight = 1000; vm.runInContext('resizeChatInput()', context);
  assert.equal(input.style.height, '240px'); assert.equal(input.style.overflowY, 'auto');
});

test('worker and Results counts follow project lists and active filters', () => {
  const { context, document } = client();
  context.snapshot = { stats: {}, queue: [], workers: [], results: [], running_total: 2, max_parallel: 4 };
  vm.runInContext('state = snapshot; renderStats(); renderWorkers(); renderResults()', context);
  assert.equal(document.getElementById('worker-count').textContent, 0);
  assert.equal(document.getElementById('workers-list').innerHTML, '');
  context.snapshot.running_total = 4;
  context.snapshot.workers = [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }, { id: 'c', label: 'C' }, { id: 'd', label: 'D' }];
  vm.runInContext('renderStats(); renderWorkers()', context);
  assert.equal(document.getElementById('worker-count').textContent, 4);
  assert.doesNotMatch(document.getElementById('workers-list').innerHTML, /other projects|slots in use/);
  context.snapshot.results = [
    { id: 'parent', label: 'alpha parent', status: 'completed', done: false },
    { id: 'child', parent_task_id: 'parent', label: 'alpha child', status: 'completed', done: false },
    { id: 'done', label: 'beta', status: 'completed', done: true }
  ];
  vm.runInContext("hideDone = false; resultsQuery = ''; collapsedForks.add('parent'); renderResults()", context);
  assert.equal(document.getElementById('results-count').textContent, 3);
  vm.runInContext('hideDone = true; renderResults()', context);
  assert.equal(document.getElementById('results-count').textContent, 2);
  assert.equal(document.getElementById('results-count').title, '2 shown of 3');
  assert.equal(document.getElementById('results-count')['aria-label'], '2 shown of 3');
  vm.runInContext("resultsQuery = 'child'; renderResults()", context);
  assert.equal(document.getElementById('results-count').textContent, 1);
  assert.equal(document.getElementById('results-count').title, '1 shown of 3');
  vm.runInContext("hideDone = false; resultsQuery = ''; renderResults()", context);
  assert.equal(document.getElementById('results-count').title, undefined);
  assert.equal(document.getElementById('results-count')['aria-label'], undefined);
});

test('queue appears after one second or immediately when blocked', () => {
  const { context, document } = client();
  context.now = 1000;
  context.snapshot = { queue: [{id:'q', label:'Q'}], workers: [], results: [], stats: {}, running_total: 0, max_parallel: 2 };
  vm.runInContext("Date.now = () => now; state = snapshot; configCache = {projects:[{id:'p'}]}; currentProjectId = 'p'; renderQueue()", context);
  const panel = document.getElementById('queue-panel');
  assert.equal(panel.style.display, 'none');
  context.now = 1999; vm.runInContext('renderQueue()', context);
  assert.equal(panel.style.display, 'none');
  context.now = 2000; vm.runInContext('renderQueue()', context);
  assert.equal(panel.style.display, '');
  context.snapshot.queue = []; vm.runInContext('renderQueue()', context);
  assert.equal(panel.style.display, 'none');
  context.snapshot.queue = [{id:'new', label:'New'}]; context.snapshot.running_total = 3;
  context.snapshot.running_by_project = {p:{workers:1, results:1}};
  vm.runInContext('renderQueue()', context);
  assert.equal(panel.style.display, '');
  const reason = document.getElementById('queue-waiting');
  assert.match(reason.innerHTML, /^Waiting for a free slot: 3 in use, limit 2\.<span class="visually-hidden"> 1 in Workers, 1 replying in Results, 1 in other projects<\/span>$/);
  assert.equal(reason.title, '1 in Workers, 1 replying in Results, 1 in other projects');
  context.snapshot.paused = true; vm.runInContext('renderQueue()', context);
  assert.equal(reason.style.display, 'none');
});

test('an unchanged poll advances the queue visibility timer', async () => {
  const { context, document } = client();
  context.now = 1000;
  context.fetch = async () => ({ status:204 });
  vm.runInContext("Date.now = () => now; state.queue = [{id:'q', label:'Q'}]; state.max_parallel = 4; state.running_total = 0; renderQueue()", context);
  assert.equal(document.getElementById('queue-panel').style.display, 'none');
  context.now = 2001;
  await vm.runInContext('originalPoll()', context);
  assert.equal(document.getElementById('queue-panel').style.display, '');
});

test('queue focus moves only after a removed card loses focus', () => {
  const { context, document } = client();
  const queue = document.getElementById('queue-panel');
  const heading = document.getElementById('workers-heading');
  const worker = { dataset:{taskId:'q'}, focus() { document.activeElement = this; } };
  document.body = {};
  context.focused = { closest() { return { dataset:{taskId:'q'} }; } };
  queue.contains = value => value === context.focused;
  document.querySelectorAll = selector => selector === '#workers-list [data-task-id]' ? [worker] : [];
  heading.focus = () => { document.activeElement = heading; };
  context.queuePanel = queue;
  vm.runInContext("state.queue = []; selectedTaskId = null; renderStats = () => {}; renderQueue = () => { document.activeElement = document.body; queuePanel.style.display = 'none'; }; renderWorkers = () => {}; renderResults = () => {}; renderPausedBanner = () => {}; renderHarnessOnboarding = () => {}", context);
  document.activeElement = context.focused;
  vm.runInContext('render()', context);
  assert.equal(document.activeElement, worker);
  document.querySelectorAll = () => [];
  document.activeElement = context.focused;
  vm.runInContext('render()', context);
  assert.equal(document.activeElement, heading);
});

test('collapsed forks expose descendant states and Hide done retains their ancestors', () => {
  const { context, document } = client();
  context.cards = [
    { id:'parent', label:'PARENT', status:'completed', done:true, activity_kind:'reply', chat_pending:true },
    { id:'middle', label:'MIDDLE', parent_task_id:'parent', status:'completed', done:true },
    { id:'child', label:'CHILD', parent_task_id:'middle', status:'completed', done:true,
      background_activity:{task_count:1} },
  ];
  vm.runInContext("state.results = cards; fullState.results = cards; hideDone = true; resultsQuery = ''; collapsedForks.add('parent'); unreadReplies.ids.add('child'); renderResults()", context);
  const html = document.getElementById('results-list').innerHTML;
  assert.match(html, /PARENT/);
  assert.doesNotMatch(html, />CHILD</);
  assert.match(html, />Replying<\/span>[^]*>New reply in fork<\/span>[^]*>Background work in fork<\/span>/);
  assert.match(html, /title="In a fork: A reply arrived since you last opened this task"/);
  assert.match(html, /title="In a fork: Background tasks are still running"/);
  vm.runInContext("collapsedForks.delete('parent'); renderResults()", context);
  assert.match(document.getElementById('results-list').innerHTML, />CHILD</);
  assert.doesNotMatch(document.getElementById('results-list').innerHTML, /in fork<\/span>/);
});

test('restart confirmation uses latest distinct-work count across projects', async () => {
  const { context } = client();
  const messages = [];
  context.showConfirm = async message => { messages.push(message); return false; };
  for (const count of [0, 1, 3]) {
    context.nextCount = count;
    context.poll = async () => { vm.runInContext('fullState.restart_work_total = nextCount', context); return true; };
    await vm.runInContext('restartServer()', context);
  }
  assert.deepEqual(messages, [
    'Restart the server? Running tasks will be interrupted.',
    'Restart the server? 1 task in all projects has work in progress and may be interrupted.',
    'Restart the server? 3 tasks in all projects have work in progress and may be interrupted.'
  ]);
  context.poll = async () => false;
  await vm.runInContext('restartServer()', context);
  assert.equal(messages.at(-1), 'Restart the server? Running tasks will be interrupted.');
});

test('header task actions keep no-harness handling and template setup fallback', async () => {
  const { context, document } = client();
  const add = {}, batch = {};
  document.querySelectorAll = selector => selector === '#header-add-task, #header-run-template' ? [add, batch] : [];
  vm.runInContext('configCache = {harnesses: []}; renderHarnessOnboarding()', context);
  assert.equal(add.disabled, true); assert.equal(batch.disabled, true);
  context.loadConfig = async () => {};
  let target;
  context.openSettings = id => { target = id; };
  vm.runInContext("configCache = {harnesses:[{id:'h'}], projects:[{id:'p'}], templates:[]}; renderHarnessOnboarding()", context);
  assert.equal(add.disabled, false); assert.equal(batch.disabled, false);
  await vm.runInContext('openBatchAdd()', context);
  assert.equal(target, 'templates-heading');
});

test('prompt tab renders escaped prose and preserves whitespace', () => {
  const { context, document } = client();
  context.task = { id: 'task', status: 'completed', messages: [{ role: 'user', kind: 'initial', content: 'Hello\n<script>bad</script>' }] };
  vm.runInContext("currentDetailTab = 'prompt'; renderDetail(task)", context);
  const html = document.getElementById('detail-body').innerHTML;
  assert.match(html, /<div class="prompt-text">Hello\n&lt;script&gt;bad&lt;\/script&gt;<\/div>/);
  assert.doesNotMatch(html, /<pre>|<script>/);
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.prompt-text\s*\{[^}]*white-space:\s*pre-wrap;[^}]*overflow-wrap:\s*anywhere;[^}]*font-family:\s*inherit/s);
});

test('settings labels, native switches and edit-only delete controls remain accessible', () => {
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.ok(html.indexOf('id="header-add-task"') < html.indexOf('id="header-run-template"'));
  assert.match(html, /class="primary" id="header-add-task"/);
  assert.match(html, /id="cfg-prevent-sleep" aria-describedby="cfg-prevent-sleep-help"/);
  assert.match(html, /id="cfg-show-task-cost" aria-describedby="cfg-show-task-cost-help"/);
  assert.match(css, /\.switch input:focus-visible \+ span/);
  assert.match(css, /\.muted\s*\{\s*color:\s*var\(--text-muted\)/);
  for (const id of ['harness-delete-btn', 'template-delete-btn', 'review-section-delete-btn']) {
    assert.match(html, new RegExp(`id="${id}"[^>]*style="display:none"`));
  }
  for (const label of ['Running task slots', 'Session viewer URL', 'Review sections', 'Attached review sections', 'Default args (space-separated)', 'Sessions directory (optional)']) assert.ok(html.includes(label));
});

test('delete controls are shown for edit but hidden for add', () => {
  const { context, document } = client();
  context.openModal = () => {};
  for (const name of ['renderColorPicker', 'renderTemplateHarnessOptions', 'renderTemplateReviewSections',
    'renderHarnessTypeOptions', 'renderHarnessColorPicker', 'renderHarnessAvailability']) context[name] = () => {};
  vm.runInContext("harnessesLocal = [{id:'h',name:'H',args:[]}]; templatesLocal = [{id:'t',name:'T',template:'x'}]; reviewSectionsLocal = [{id:'r',name:'R',prompt:'p'}]", context);
  for (const [add, edit, button] of [['addHarnessForm()', "editHarness('h')", 'harness-delete-btn'],
    ['addTemplateForm()', "editTemplate('t')", 'template-delete-btn'],
    ['addReviewSectionForm()', "editReviewSection('r')", 'review-section-delete-btn']]) {
    vm.runInContext(add, context);
    assert.equal(document.getElementById(button).style.display, 'none');
    vm.runInContext(edit, context);
    assert.equal(document.getElementById(button).style.display, '');
  }
});

test('destructive Settings actions are only in edit modals and remain staged until Save', async () => {
  const { context, document } = client();
  context.showConfirm = async () => false;
  context.openModal = () => {};
  context.closeModal = id => { context.closed = id; };
  context.renderHarnessesList = () => {};
  context.renderTemplatesList = () => {};
  context.renderReviewSectionsList = () => {};
  vm.runInContext("harnessesLocal = [{id:'h',name:'Harness'}]; templatesLocal = [{id:'t', review_section_ids:['r']}]; reviewSectionsLocal = [{id:'r',name:'Review'}]", context);
  await vm.runInContext("deleteHarness('h');", context);
  assert.equal(vm.runInContext('harnessesLocal.length', context), 1);
  assert.equal(context.closed, undefined);
  context.showConfirm = async () => true;
  await vm.runInContext("deleteHarness('h');", context);
  assert.equal(context.closed, 'harness-modal');
  assert.equal(vm.runInContext('harnessesLocal.length', context), 0);
  await vm.runInContext("removeReviewSection('r');", context);
  assert.equal(context.closed, 'review-section-modal');
  assert.equal(vm.runInContext('reviewSectionsLocal.length', context), 0);
  assert.equal(vm.runInContext('templatesLocal[0].review_section_ids.length', context), 0);
  await vm.runInContext("deleteTemplate('t');", context);
  assert.equal(context.closed, 'template-modal');
  assert.equal(vm.runInContext('templatesLocal.length', context), 0);
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  for (const id of ['harness-delete-btn', 'template-delete-btn', 'review-section-delete-btn']) assert.ok(html.includes(id));
  assert.ok(document.getElementById('settings-modal'));
});

test('confirmed Settings deletions restore focus to the next Edit or section Add', async () => {
  const { context, document } = client();
  context.showConfirm = async () => true;
  context.closeModal = () => {};
  context.renderHarnessesList = () => {};
  context.renderTemplatesList = () => {};
  context.renderReviewSectionsList = () => {};
  vm.runInContext("configCache.projects = [{id:'p'}]; currentProjectId = 'p'", context);
  for (const [list, add, setup, call] of [
    ['harnesses-list', 'add-harness-btn', "harnessesLocal = [{id:'first'}, {id:'remove'}, {id:'next'}]", "deleteHarness('remove')"],
    ['templates-list', 'add-template-btn', "templatesLocal = [{id:'first',project_id:'p'}, {id:'remove',project_id:'p'}, {id:'next',project_id:'p'}]", "deleteTemplate('remove')"],
    ['review-sections-list', 'add-review-section-btn', "reviewSectionsLocal = [{id:'first',project_id:'p'}, {id:'remove',project_id:'p'}, {id:'next',project_id:'p'}]; templatesLocal = []", "removeReviewSection('remove')"]
  ]) {
    let focused = '';
    const buttons = [{ focus() { focused = 'first'; } }, { focus() { focused = 'next'; } }];
    document.getElementById(list).querySelectorAll = () => buttons;
    document.getElementById(add).focus = () => { focused = 'add'; };
    vm.runInContext(setup, context);
    await vm.runInContext(call, context);
    assert.equal(focused, 'next', list);
    document.getElementById(list).querySelectorAll = () => [];
    vm.runInContext(setup, context);
    await vm.runInContext(call, context);
    assert.equal(focused, 'add', list);
  }
});

test('settings harness markers use dots or custom icons and selects omit default diamond', () => {
  const { context } = client();
  context.h = { name: '<Pi>', icon: '◆', color: 'rose' };
  assert.match(vm.runInContext('harnessMarkerHtml(h)', context), /harness-dot/);
  assert.equal(vm.runInContext('harnessOptionLabel(h)', context), '&lt;Pi&gt;');
  context.h.icon = '★';
  assert.match(vm.runInContext('harnessMarkerHtml(h)', context), /harness-glyph/);
  assert.equal(vm.runInContext('harnessOptionLabel(h)', context), '★ &lt;Pi&gt;');
});

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

test('detail toolbar shows plain configured metadata and only available session actions', () => {
  const { context, document } = client();
  context.config = { harnesses: [{ id: 'h', name: 'Long Harness', color: 'rose' }],
    templates: [{ id: 't', name: 'Long Template' }], session_viewer_url: 'https://example.test' };
  context.task = { id: 'task', status: 'completed', harness: 'h', template_id: 't', session_id: 'session',
    background_activity: { task_count: 2 }, messages: [] };
  vm.runInContext('configCache = config; renderDetail(task)', context);
  assert.match(document.getElementById('detail-harness').innerHTML, /Long Harness/);
  assert.doesNotMatch(document.getElementById('detail-harness').innerHTML, /harness-badge/);
  assert.equal(document.getElementById('detail-template').title, 'Template: Long Template');
  assert.equal(document.getElementById('detail-status').style.display, 'none');
  assert.equal(document.getElementById('detail-session-btn').style.display, '');
  assert.equal(document.getElementById('copy-session-id-btn').style.display, '');
  assert.match(document.getElementById('detail-background-activity').innerHTML, /reply-state is-active/);
  context.task = { ...context.task, status: 'failed', session_id: null, background_activity: { preparing_reply: true } };
  vm.runInContext('renderDetail(task)', context);
  assert.equal(document.getElementById('detail-status').textContent, 'Failed');
  assert.equal(document.getElementById('detail-status').className, 'status-failed');
  assert.equal(document.getElementById('detail-session-btn').style.display, 'none');
  assert.equal(document.getElementById('copy-session-id-btn').style.display, 'none');
  assert.equal(document.getElementById('detail-background-activity').style.display, 'none');
  context.task.session_id = 'session'; context.config.session_viewer_url = '';
  vm.runInContext('renderDetail(task)', context);
  assert.equal(document.getElementById('detail-session-btn').style.display, 'none');
  assert.equal(document.getElementById('copy-session-id-btn').style.display, '');
  for (const status of ['queued', 'running', 'reviewing', 'failed', 'cancelled']) {
    context.task.status = status;
    vm.runInContext('renderDetail(task)', context);
    assert.equal(document.getElementById('detail-status').textContent, status[0].toUpperCase() + status.slice(1));
    assert.equal(document.getElementById('detail-status').style.display, '');
  }
});

test('copy session ID closes its menu after confirmation', async () => {
  const { context, document } = client();
  context.task = { id: 'task', status: 'completed', session_id: 'session-id', messages: [] };
  vm.runInContext('renderDetail(task)', context);
  let copied, delay, callback, closed = false;
  context.navigator = { clipboard: { writeText(value) { copied = value; return Promise.resolve(); } } };
  context.setTimeout = (fn, ms) => { callback = fn; delay = ms; };
  context.closeDetailMenu = () => { closed = true; };
  vm.runInContext('copySessionId()', context);
  await Promise.resolve();
  assert.equal(copied, 'session-id');
  assert.equal(document.getElementById('copy-session-id-btn').textContent, 'Copied');
  assert.equal(delay, 800);
  callback();
  assert.equal(closed, true);
  assert.equal(document.getElementById('copy-session-id-btn').textContent, 'Copy session ID');
});

test('detail tabs use roving focus and menu navigation follows keyboard conventions', () => {
  const { context, document } = client();
  const tabs = ['Output', 'Prompt'].map(textContent => ({ textContent, focus() { document.activeElement = this; } }));
  document.querySelectorAll = selector => selector === '.detail-tab' ? tabs : [];
  context.refreshDetail = () => {};
  context.event = { key: 'ArrowRight', currentTarget: tabs[0], preventDefault() {} };
  vm.runInContext('detailTabKey(event)', context);
  assert.equal(document.activeElement, tabs[1]);
  assert.equal(vm.runInContext('currentDetailTab', context), 'prompt');
  context.event = { key: 'Home', currentTarget: tabs[1], preventDefault() {} };
  vm.runInContext('detailTabKey(event)', context);
  assert.equal(document.activeElement, tabs[0]);
  assert.equal(vm.runInContext('currentDetailTab', context), 'output');
  const items = [0, 1, 2].map(() => ({ offsetParent: {}, focus() { document.activeElement = this; } }));
  const menu = document.getElementById('detail-menu');
  let hidden = true;
  menu.classList = { contains() { return hidden; }, toggle(_name, value) { hidden = value; }, add() { hidden = true; } };
  menu.querySelectorAll = () => items;
  const trigger = document.getElementById('detail-menu-btn');
  trigger.focus = () => { document.activeElement = trigger; };
  vm.runInContext('toggleDetailMenu()', context);
  assert.equal(document.activeElement, items[0]);
  context.event = { key: 'ArrowUp', preventDefault() {}, stopPropagation() {} };
  vm.runInContext('detailMenuKey(event)', context);
  assert.equal(document.activeElement, items[2]);
  context.event.key = 'Home'; vm.runInContext('detailMenuKey(event)', context);
  assert.equal(document.activeElement, items[0]);
  context.event.key = 'End'; vm.runInContext('detailMenuKey(event)', context);
  assert.equal(document.activeElement, items[2]);
  context.event.key = 'Escape'; vm.runInContext('detailMenuKey(event)', context);
  assert.equal(document.activeElement, trigger);
  assert.equal(hidden, true);
});

test('detail markup and CSS cover narrow layouts, tabs, and message styling', () => {
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(html, /id="detail-menu-btn"[^>]*aria-haspopup="menu"/);
  assert.match(html, /role="tab" aria-selected="true" aria-controls="detail-body" tabindex="0"/);
  assert.match(html, /role="tab" aria-selected="false" aria-controls="detail-body" tabindex="-1"/);
  assert.match(html, /id="detail-body" role="tabpanel"/);
  assert.ok(html.indexOf('id="copy-task-id-btn"') < html.indexOf('id="copy-session-id-btn"'));
  assert.match(html, /class="text-btn" id="detail-session-btn"[^>]*>Open session/);
  assert.match(css, /@container \(max-width: 560px\)/);
  assert.match(css, /\.detail-toolbar-right\s*\{\s*flex-basis:\s*100%;\s*margin-left:\s*0/);
  assert.match(css, /#detail-template\s*\{\s*display:\s*none !important/);
  assert.match(css, /\.chat-message\.user\s*\{[^}]*color-mix\(in srgb, var\(--primary\) 6%, transparent\)[^}]*border:\s*none/s);
  assert.match(css, /\.chat-message\s*\{[^}]*margin:\s*12px 0/s);
});

test('rendered inline code can wrap and the phone overlay can shrink below 400px', () => {
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.detail-body \.rendered-output code\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(css, /\.chat-message \.chat-content\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(css, /\.detail-body pre\s*\{[^}]*white-space:\s*pre-wrap/s);
  assert.match(css, /\.detail-body \.rendered-output pre code\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(css, /\.detail-body \.rendered-output table\s*\{[^}]*display:\s*block;[^}]*max-width:\s*100%;[^}]*overflow-x:\s*auto/s);
  assert.match(css, /\.detail-body \.rendered-output img\s*\{\s*max-width:\s*100%;\s*height:\s*auto/);
  assert.match(css, /\.detail-panel\s*\{[^}]*min-width:\s*400px/s);
  assert.match(css, /@media \(max-width: 899px\)[\s\S]*?\.detail-panel\s*\{[^}]*left:\s*0;[^}]*right:\s*0;[^}]*width:\s*auto !important;[^}]*min-width:\s*0;[^}]*max-width:\s*100%/);
  const { context } = client();
  for (const [viewport, maximum] of [[900, 536], [1079, 715], [1080, 416], [1440, 776]]) {
    context.innerWidth = viewport;
    assert.equal(vm.runInContext('clampDetailWidth(9999)', context), maximum);
    assert.ok(vm.runInContext('clampDetailWidth(1)', context) >= 400);
  }
});

test('responsive Results layout clamps widths and caps nested cards', () => {
  const { context } = client();
  for (const [width, expected] of [[899, 535], [900, 536], [1079, 715], [1080, 416], [1440, 776]]) {
    context.innerWidth = width;
    assert.equal(vm.runInContext('clampDetailWidth(2000)', context), expected);
  }
  context.innerWidth = 1080;
  assert.equal(vm.runInContext('clampDetailWidth(1)', context), 400);
  context.task = { id: 'task', label: 'Result', status: 'completed' };
  assert.match(vm.runInContext('renderResultCard(task, 2, new Map())', context), /--fork-indent:32px;--action-space:90px/);
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  assert.match(css, /grid-template-columns:\s*300px minmax\(360px, 1fr\)/);
  assert.match(css, /@media \(min-width: 900px\) and \(max-width: 1079px\)/);
  assert.match(css, /\.workspace:has\(> \.detail-panel:not\(\.hidden\)\) main\s*\{[^}]*grid-template-rows:\s*auto minmax\(0, 1fr\)/s);
  assert.match(css, /\.workspace:has\(> \.detail-panel:not\(\.hidden\)\) \.side-column\s*\{[^}]*max-height:\s*35vh;[^}]*overflow:\s*hidden/s);
  assert.match(css, /\.side-column \.panel\.is-empty \.task-list\s*\{\s*display:\s*none/);
  assert.match(css, /\.side-column \.panel \.task-list\s*\{[^}]*min-height:\s*0;[^}]*overflow-y:\s*auto/s);
  assert.match(css, /@media \(max-width: 899px\)/);
  assert.match(css, /max-width:\s*calc\(880px - var\(--fork-indent, 0px\)\)/);
  assert.match(css, /\.results-header-top, \.results-search-row, \.results-date-header\s*\{\s*max-width:\s*880px/);
  assert.match(css, /@container \(max-width: 460px\)/);
  assert.match(css, /\.result-card \.actions \.card-action\s*\{\s*display:\s*none/);
  assert.match(css, /\.result-card \.harness-name-text, \.result-card \.harness-glyph\s*\{\s*display:\s*none/);
  assert.match(css, /\.results-sort\s*\{[^}]*max-width:\s*140px;[^}]*text-overflow:\s*ellipsis/s);
  assert.match(html, /placeholder="Search…"/);
});

test('narrow action menu supports keyboard, focus return, positioning, and conditional actions', () => {
  const { context, document } = client();
  const children = [];
  const menu = { id: '', getBoundingClientRect: () => ({ width: 170, height: 120 }),
    set innerHTML(_value) { children.length = 0; }, appendChild(button) { children.push(button); },
    get firstElementChild() { return children[0]; }, get lastElementChild() { return children.at(-1); } };
  const container = { style: {}, classList: { add() {}, remove() {} },
    querySelector() { return menu; }, querySelectorAll() { return children; } };
  const originalGet = document.getElementById;
  document.getElementById = id => id === 'result-card-menu-container' ? container : originalGet(id);
  document.createElement = () => ({ setAttribute() {}, addEventListener(_event, handler) { this.click = handler; },
    focus() { document.activeElement = this; } });
  const attributes = {};
  context.trigger = { setAttribute(name, value) { attributes[name] = value; },
    closest() { return { classList: { add() {}, remove() {} } }; },
    getBoundingClientRect() { return { right: 600, top: 740, bottom: 760 }; },
    focus() { document.activeElement = this; } };
  context.innerWidth = 1000; context.innerHeight = 800;
  context.snapshot = { results: [{ id: 'task', status: 'failed', done: false }] };
  vm.runInContext('state = snapshot', context);
  context.event = { currentTarget: context.trigger, key: 'ArrowDown', stopPropagation() {}, preventDefault() {} };
  vm.runInContext("resultMenuTriggerKey(event, 'task', false)", context);
  assert.deepEqual(children.map(item => item.textContent), ['Mark done', 'Fork…', 'Continue', 'Retry', 'Delete']);
  assert.equal(menu.id, 'result-menu-task');
  assert.equal(attributes['aria-expanded'], 'true');
  assert.equal(document.activeElement, children[0]);
  assert.equal(container.style.top, '616px');
  assert.equal(container.style.left, '430px');
  context.event.key = 'ArrowUp'; vm.runInContext('resultMenuKey(event)', context);
  assert.equal(document.activeElement, children.at(-1));
  context.event.key = 'Home'; vm.runInContext('resultMenuKey(event)', context);
  assert.equal(document.activeElement, children[0]);
  context.event.key = 'End'; vm.runInContext('resultMenuKey(event)', context);
  assert.equal(document.activeElement, children.at(-1));
  context.event.key = 'Escape'; vm.runInContext('resultMenuKey(event)', context);
  assert.equal(document.activeElement, context.trigger);
  assert.equal(attributes['aria-expanded'], 'false');
  context.event.key = 'ArrowUp'; vm.runInContext("resultMenuTriggerKey(event, 'task', true)", context);
  assert.deepEqual(children.map(item => item.textContent), ['Mark done', 'Fork…', 'Continue', 'Delete']);
  assert.equal(document.activeElement, children.at(-1));
  context.event.key = 'Tab'; vm.runInContext('resultMenuKey(event)', context);
  assert.equal(document.activeElement, context.trigger);
});

test('unread, replying and background states are independent and ordered', () => {
  const { context } = client();
  for (const unread of [false, true]) for (const replying of [false, true]) for (const background of [false, true]) {
    context.task = { id: 'task', activity_kind: 'reply', chat_pending: replying,
      background_activity: { task_count: background ? 2 : 0 } };
    vm.runInContext(`unreadReplies.ids.${unread ? 'add' : 'delete'}('task')`, context);
    const html = vm.runInContext('replyStateHtml(task)', context);
    const labels = [...html.matchAll(/<\/span>(New reply|Replying|Background work)<\/span>/g)].map(m => m[1]);
    assert.deepEqual(labels, [...(unread ? ['New reply'] : []), ...(replying ? ['Replying'] : []), ...(background ? ['Background work'] : [])]);
  }
  vm.runInContext("unreadReplies.ids.delete('task')", context);
  context.task = { id: 'task', activity_kind: 'review', background_activity: { preparing_reply: true, task_count: 2 } };
  assert.match(vm.runInContext('replyStateHtml(task)', context), /Replying[^]*Background work/);
  context.task.activity_kind = 'original'; context.task.background_activity.task_count = 0;
  assert.equal(vm.runInContext('replyStateHtml(task)', context), '');
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.reply-state\.is-active \.state-dot\s*\{\s*animation:\s*none/);
});

test('attention and activity badges appear in priority order', () => {
  const { context } = client();
  context.task = { id: 'task', activity_kind: 'reply', chat_pending: true, compacting: true,
    background_activity: { task_count: 1 } };
  vm.runInContext("unreadReplies.ids.add('task')", context);
  const html = vm.runInContext('replyStateHtml(task)', context);
  const labels = [...html.matchAll(/<\/span>(New reply|Replying|Compacting|Background work)<\/span>/g)].map(m => m[1]);
  assert.deepEqual(labels, ['New reply', 'Replying', 'Compacting', 'Background work']);
});

test('compaction has a transient visible activity badge', () => {
  const { context } = client();
  context.task = { id: 'task', activity_kind: 'original', compacting: true };
  assert.match(vm.runInContext('replyStateHtml(task)', context), /Compacting/);
  context.task.compacting = false;
  assert.doesNotMatch(vm.runInContext('replyStateHtml(task)', context), /Compacting/);
});

test('result harness names are full, escaped, and use only configured colors or icons', () => {
  const { context } = client();
  context.harnesses = [
    { id: 'h', name: 'Long <name> "quoted" and more', color: 'rose' },
    { id: 'custom', name: 'Custom', icon: '★' },
    { id: 'plain', name: 'No color' }
  ];
  vm.runInContext('configCache.harnesses = harnesses', context);
  let html = vm.runInContext("resultHarnessHtml('h')", context);
  assert.match(html, /title="Harness: Long &lt;name&gt; &quot;quoted&quot; and more"/);
  assert.match(html, /Long &lt;name&gt; "quoted" and more/);
  assert.match(html, /class="harness-dot" style="--harness-accent:#f85149"/);
  html = vm.runInContext("resultHarnessHtml('custom')", context);
  assert.match(html, /class="harness-glyph"[^>]*>★/);
  assert.match(html, /harness-dot/);
  assert.match(vm.runInContext("resultHarnessHtml('plain')", context), /harness-dot/);
  assert.equal(vm.runInContext("resultHarnessHtml('missing')", context), '');
});

test('result cards hide completed status and put accessible fork controls in meta', () => {
  const { context } = client();
  context.task = { id: 'task', label: 'Result', status: 'completed' };
  context.child = { id: 'child', label: 'Child', status: 'completed' };
  let html = vm.runInContext('renderResultCard(task, 0, new Map())', context);
  assert.doesNotMatch(html, /result-status|fork-toggle|fork-count/);
  html = vm.runInContext('renderResultCard(task, 0, new Map([[task.id, [child]]]))', context);
  assert.match(html, /class="fork-count" aria-expanded="true" title="Hide forks" aria-label="Hide forks"/);
  assert.match(html, /⑂ 1 <span class="fork-chevron" aria-hidden="true">▾/);
  assert.match(html, /fork-tree-prefix/);
  vm.runInContext("collapsedForks.add('task')", context);
  html = vm.runInContext('renderResultCard(task, 0, new Map([[task.id, [child]]]))', context);
  assert.match(html, /aria-expanded="false" title="Show forks" aria-label="Show forks"/);
  assert.match(html, />▸<\/span>/);
  assert.doesNotMatch(html, /fork-tree-prefix| hidden/);
  for (const [status, label, icon] of [['failed', 'Failed', '✗'], ['cancelled', 'Cancelled', '⊘']]) {
    context.task.status = status;
    html = vm.runInContext('renderResultCard(task, 0, new Map())', context);
    assert.ok(html.includes(`class="result-status status-${status}" title="${label}" aria-label="${label}">${icon}`));
  }
});

test('quiet result styles preserve focus, unread, layout and action visibility', () => {
  const { context } = client();
  context.task = { id: 'task', label: 'Result', status: 'completed' };
  vm.runInContext("unreadReplies.ids.add('task')", context);
  const html = vm.runInContext('renderResultCard(task, 0, new Map())', context);
  assert.match(html, /unread-reply-card selected/);
  assert.match(html, /New reply/);
  assert.match(html, /class="actions"/);
  assert.match(html, /aria-label="Mark done"/);
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.task-card\.result-card\s*\{[^}]*border-color:\s*transparent;[^}]*padding:\s*8px 12px;[^}]*margin-bottom:\s*2px/s);
  assert.match(css, /\.task-card\.result-card\.unread-reply-card\s*\{\s*border-color:\s*transparent/);
  assert.match(css, /\.task-card\.result-card\.selected\s*\{\s*border-color:\s*var\(--primary\)/);
  assert.match(css, /\.result-card:focus-visible \.actions/);
  assert.match(css, /\.result-card \.actions:focus-within/);
  assert.match(css, /\.task-card\.result-card \.actions\s*\{\s*display:\s*flex;\s*opacity:\s*0;\s*pointer-events:\s*none/);
  assert.match(css, /\.result-card\.menu-open \.actions\s*\{\s*opacity:\s*1;\s*pointer-events:\s*auto/);
  assert.match(css, /\.result-card:hover \.label,[\s\S]*?\.result-card\.menu-open \.label\s*\{\s*padding-right:\s*var\(--action-space\)/);
  assert.match(css, /\.result-card \.meta > \*\s*\{\s*flex-shrink:\s*0/);
  assert.match(css, /\.result-card \.meta > \.harness-name\s*\{[^}]*flex-shrink:\s*1/s);
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
  assert.match(css, /button:disabled, button\[aria-disabled="true"\], select:disabled\s*\{[^}]*opacity:\s*0\.5;[^}]*cursor:\s*default;/);
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

test('quiet Workers show harness, elapsed time and only exception labels', () => {
  const { context, document } = client();
  context.snapshot = { workers: [{ id: 'task', label: 'Long <title> "quoted"', harness: 'h', status: 'reviewing',
    activity_kind: 'review', chat_pending: true, background_activity: { task_count: 1 },
    started_at: '2026-01-01T00:00:00Z', output_tail: 'PRIVATE PREVIEW' }], show_task_cost: false };
  vm.runInContext("Date.now = () => new Date('2026-01-01T01:01:02Z').getTime(); configCache.harnesses = [{id:'h',name:'Harness',color:'rose'}]; state = snapshot; unreadReplies.ids.add('task'); renderWorkers()", context);
  const html = document.getElementById('workers-list').innerHTML;
  assert.match(html, /title="Long &lt;title&gt; &quot;quoted&quot;"/);
  assert.match(html, /class="harness-name"[^>]*>[^]*Harness/);
  assert.match(html, /class="worker-row"[^]*class="meta"[^]*class="elapsed" title="Running time"/);
  assert.match(html, /class="visually-hidden">Running time /);
  assert.match(html, /class="elapsed-time">1:01:02/);
  assert.match(html, />Reviewing<\/span>/);
  assert.match(html, />New reply<\/span>/);
  assert.match(html, />Background work<\/span>/);
  assert.doesNotMatch(html, />Replying<\/span>|status-running|output-preview|PRIVATE PREVIEW|class="pulse"/);
  assert.match(html, /title="Cancel task" aria-label="Cancel task"/);
  context.snapshot.workers[0].status = 'running';
  context.snapshot.workers[0].background_activity = {};
  vm.runInContext("unreadReplies.ids.delete('task'); renderWorkers()", context);
  assert.doesNotMatch(document.getElementById('workers-list').innerHTML, />Reviewing<\/span>|>Background work<\/span>|>New reply<\/span>/);
});

test('worker elapsed clock formats hours, clamps future times and updates text only', () => {
  const { context, document } = client();
  vm.runInContext("Date.now = () => new Date('2026-01-01T01:01:02Z').getTime()", context);
  assert.equal(vm.runInContext("formatElapsed(new Date('2026-01-01T01:00:03Z'))", context), '0:59');
  assert.equal(vm.runInContext("formatElapsed(new Date('2026-01-01T00:01:02Z'))", context), '1:00:00');
  assert.equal(vm.runInContext("formatElapsed(new Date('2026-01-01T02:00:00Z'))", context), '0:00');
  assert.equal(vm.runInContext("formatElapsed(new Date('invalid'))", context), '');
  const clock = { textContent: '', innerHTML: 'unchanged' };
  document.querySelectorAll = selector => selector === '.worker-card[data-started-at]' ? [{ dataset: { startedAt: '2026-01-01T00:01:02Z' },
    querySelector(name) { assert.equal(name, '.elapsed-time'); return clock; } }] : [];
  vm.runInContext('updateWorkerElapsedTimes()', context);
  assert.equal(clock.textContent, '1:00:00'); assert.equal(clock.innerHTML, 'unchanged');
  const source = fs.readFileSync('src/hihive/static/app.js', 'utf8');
  assert.match(source, /updateWorkerElapsedTimes\(\);\s*\}, 1000\)/);
  assert.match(source, /refreshDetail\(\);\s*\}, 2000\)/);
});

test('quiet Queue retains drag reorder and keyboard-accessible removal', async () => {
  const { context, document } = client();
  context.snapshot = { queue: [{ id: 'first', label: 'First <item>', harness: 'h' }, { id: 'second', label: 'Second' }],
    workers: [], results: [], paused: true, stats: {}, max_parallel: 2 };
  vm.runInContext("state = snapshot; configCache = {harnesses:[{id:'h',name:'Harness',color:'rose'}], projects:[{id:'p'}]}; currentProjectId = 'p'; renderQueue()", context);
  const html = document.getElementById('queue-list').innerHTML;
  assert.match(html, /class="task-card queue-card/);
  assert.match(html, /title="First &lt;item&gt;"/);
  assert.match(html, /draggable="true"[^]*ondragstart="dragStart\(event, 'first'\)"[^]*ondragover="dragOver\(event\)"[^]*ondrop="drop\(event, 'first'\)"/);
  assert.match(html, /class="harness-name"[^]*Harness/);
  assert.match(html, /title="Remove" aria-label="Remove"/);
  assert.doesNotMatch(html, /status-queued|harness-badge/);
  const actions = [];
  context.fetch = async (url, options) => { actions.push([url, JSON.parse(options.body)]); return { ok: true }; };
  const classes = new Set();
  context.event = { dataTransfer: {}, target: { dataset: { taskId: 'second' }, classList: { add(value) { classes.add(value); }, remove(value) { classes.delete(value); } }, closest() { return this; } }, preventDefault() {} };
  vm.runInContext("dragStart(event, 'first'); dragOver(event); drop(event, 'second')", context);
  assert.equal(context.event.dataTransfer.effectAllowed, 'move');
  assert.equal(context.event.dataTransfer.dropEffect, 'move');
  assert.ok(classes.has('drag-over-after'));
  assert.equal(actions[0][0], '/api/queue/reorder');
  assert.deepEqual(actions[0][1], { order: ['second', 'first'], project_id: 'p' });
});

test('unchanged queue polls preserve card identity, hover, click and active drag', () => {
  const { context, document } = client();
  const list = document.getElementById('queue-list');
  let html = '', writes = 0, node;
  Object.defineProperty(list, 'innerHTML', { get() { return html; }, set(value) {
    writes++; html = value;
    node = { dataset: { taskId: 'first' }, hovered: false, clicked: false,
      classList: { toggle() {} }, click() { this.clicked = true; } };
  } });
  list.querySelectorAll = () => node ? [node] : [];
  context.snapshot = { queue: [{ id: 'first', label: 'First' }, { id: 'second', label: 'Second' }],
    workers: [], results: [], paused: true, stats: {}, max_parallel: 2 };
  vm.runInContext('state = snapshot; renderQueue()', context);
  const original = node; original.hovered = true;
  vm.runInContext('renderQueue(); renderQueue()', context);
  assert.equal(writes, 1); assert.equal(node, original); assert.equal(node.hovered, true);
  node.click(); assert.equal(original.clicked, true);
  context.event = { target: { classList: { add() {} } }, dataTransfer: {} };
  vm.runInContext("dragStart(event, 'first')", context);
  context.snapshot.queue[0].label = 'Changed during drag';
  vm.runInContext('renderQueue()', context);
  assert.equal(writes, 1); assert.equal(node, original);
  assert.equal(vm.runInContext('draggedId', context), 'first');
  vm.runInContext('dragEnd()', context);
  assert.equal(writes, 2); assert.notEqual(node, original);
  assert.match(html, /Changed during drag/);
});

test('queue drag indicators match reorder direction', () => {
  const { context, document } = client();
  context.snapshot = { queue: [{ id: 'first' }, { id: 'middle' }, { id: 'last' }], workers: [] };
  vm.runInContext('state = snapshot', context);
  const marks = new Set();
  const target = { dataset: { taskId: 'last' }, classList: { add(name) { marks.add(name); },
    remove(...names) { names.forEach(name => marks.delete(name)); } }, closest() { return this; } };
  document.querySelectorAll = selector => selector.includes('.drag-over') ? [target] : [];
  context.event = { target, dataTransfer: {}, preventDefault() {} };
  vm.runInContext("draggedId = 'first'; dragOver(event)", context);
  assert.deepEqual([...marks], ['drag-over-after']);
  target.dataset.taskId = 'first';
  vm.runInContext("draggedId = 'last'; dragOver(event)", context);
  assert.deepEqual([...marks], ['drag-over']);
});

test('keyboard Worker cancellation focuses the next card or Workers heading', async () => {
  const { context, document } = client();
  const worker = { dataset: { taskId: 'first' } };
  const opener = { matches: () => true, closest: () => worker };
  const heading = document.getElementById('workers-heading');
  const list = document.getElementById('workers-list');
  let focused = '';
  heading.focus = () => { focused = 'heading'; };
  list.querySelectorAll = () => [{ focus() { focused = 'next'; } }];
  document.body = {}; document.activeElement = opener;
  context.snapshot = { workers: [{ id: 'first' }, { id: 'second' }] };
  context.showConfirm = async () => true;
  context.postJson = async () => null;
  context.poll = async () => { document.activeElement = document.body; return true; };
  vm.runInContext('state = snapshot', context);
  await vm.runInContext("cancelTask('first')", context);
  assert.equal(focused, 'next');
  document.activeElement = opener;
  list.querySelectorAll = () => [];
  await vm.runInContext("cancelTask('first')", context);
  assert.equal(focused, 'heading');
  focused = '';
  document.activeElement = opener;
  context.postJson = async () => 'failed'; context.showMessage = () => {};
  await vm.runInContext("cancelTask('first')", context);
  assert.equal(focused, '');
});

test('stacked Queue and Workers use bounded shares and preserve room at short heights', () => {
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.side-column:has\(#queue-panel\.is-visible\) \.panel:not\(\.is-empty\)\s*\{\s*max-height:\s*50%/);
  assert.match(css, /#queue-panel\.is-visible\s*\{[^}]*min-height:\s*180px;[^}]*max-height:\s*max\(50%, 180px\)/s);
  assert.match(css, /#workers-panel:not\(\.is-empty\)\s*\{[^}]*min-height:\s*130px;[^}]*max-height:\s*max\(50%, 130px\)/s);
  assert.match(css, /height:\s*max\(35vh, 310px\);\s*max-height:\s*max\(35vh, 310px\)/);
  assert.match(css, /@media \(min-width: 900px\) and \(max-width: 1079px\)[^]*?\.workspace:has\(> \.detail-panel:not\(\.hidden\)\) #queue-panel\.is-visible/);
  assert.match(css, /@media \(max-width: 899px\)[^]*?#queue-panel\.is-visible/);
  assert.doesNotMatch(css, /#queue-panel\.is-visible\s*\{[^}]*min-height:\s*min-content/s);
});

test('quiet Queue and Workers retain focus reveal, drop feedback and stacked alignment', () => {
  const css = fs.readFileSync('src/hihive/static/style.css', 'utf8');
  assert.match(css, /\.task-card\.worker-card, \.task-card\.queue-card\s*\{[^}]*border-color:\s*transparent;[^}]*padding:\s*8px 12px;[^}]*margin-bottom:\s*2px/s);
  assert.match(css, /\.worker-card \.worker-row\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto/);
  assert.match(css, /\.worker-card \.elapsed\s*\{[^}]*color:\s*var\(--text\);[^}]*font-variant-numeric:\s*tabular-nums/s);
  assert.match(css, /\.task-card\.worker-card \.actions, \.task-card\.queue-card \.actions\s*\{[^}]*display:\s*flex;[^}]*opacity:\s*0;[^}]*pointer-events:\s*none/);
  assert.match(css, /\.task-card\.worker-card:focus-visible \.actions/);
  assert.match(css, /\.task-card\.queue-card \.actions:focus-within/);
  assert.match(css, /\.task-card\.drag-over, \.task-card\.queue-card\.drag-over\s*\{\s*border-top:\s*2px solid var\(--primary\)/);
  assert.match(css, /#workers-list, #queue-list\s*\{\s*padding-left:\s*16px;\s*padding-right:\s*16px/);
  assert.match(css, /\.reply-state\.is-active \.state-dot\s*\{\s*animation:\s*none/);
});

test('active task cards do not show output byte counts', () => {
  const { context, document } = client();
  context.snapshot = { workers: [{ id: 'task', label: 'Task', status: 'running', output_length: 2048, output_tail: 'Hello' }], running_total: 1 };
  vm.runInContext('state = snapshot; renderWorkers()', context);
  const html = document.getElementById('workers-list').innerHTML;
  assert.match(html, /Task/);
  assert.doesNotMatch(html, /Hello|output-preview|2\.0KB|2048B| output<\/span>/);
});

const unreadKey = 'hive-unread-follow-up-replies';
const replyTime = n => `2026-09-26T12:00:0${n}.000000Z`;
function unreadSnapshot(c, replyAt, extra = {}) {
  c.snapshot = { results: [{ id: 'task', label: 'Task', status: 'completed',
    latest_follow_up_reply_at: replyAt, completed_at: '2020-01-01T00:00:00Z', ...extra }], workers: [], queue: [] };
  vm.runInContext('trackUnreadReplies(snapshot); state = fullState = snapshot;', c);
}
function unreadIds(c) { return Array.from(vm.runInContext('[...unreadReplies.ids]', c)); }

test('unread and replying remain independent throughout the reply timeline', () => {
  const { context } = client();
  const labels = () => [...vm.runInContext('replyStateHtml(snapshot.results[0])', context)
    .matchAll(/<\/span>(New reply|Replying|Background work)<\/span>/g)].map(m => m[1]);
  unreadSnapshot(context, replyTime(1));
  unreadSnapshot(context, replyTime(2), {activity_kind:'reply', chat_pending:false});
  assert.deepEqual(labels(), ['New reply']);
  unreadSnapshot(context, replyTime(2), {activity_kind:'reply', chat_pending:true});
  assert.deepEqual(labels(), ['New reply', 'Replying']);
  context.task = { ...context.snapshot.results[0], messages:[] };
  vm.runInContext("selectedTaskId = 'task'; currentDetailTab = 'output'; renderDetail(task)", context);
  assert.deepEqual(labels(), ['Replying']);
  unreadSnapshot(context, replyTime(3), {activity_kind:'reply', chat_pending:false});
  context.task = { ...context.snapshot.results[0], messages:[] };
  vm.runInContext('renderDetail(task)', context);
  assert.deepEqual(labels(), []);
  vm.runInContext("selectedTaskId = 'elsewhere'", context);
  unreadSnapshot(context, replyTime(4), {activity_kind:'reply', chat_pending:true});
  assert.deepEqual(labels(), ['New reply', 'Replying']);
  unreadSnapshot(context, replyTime(4), {activity_kind:'reply', chat_pending:false});
  assert.deepEqual(labels(), ['New reply']);
});

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
    { id: 'older', label: 'OLDER_CARD', status: 'completed', activity_kind: 'reply', completed_at: '2020-01-01T00:00:00Z' },
    { id: 'newer', label: 'NEWER_CARD', status: 'completed', completed_at: '2020-01-02T00:00:00Z' }
  ];
  for (const pending of [false, true, false]) {
    context.cards[0].chat_pending = pending;
    context.cards[0].last_chat_at = '2026-09-26T00:00:00Z';
    vm.runInContext('state.results = cards; renderResults()', context);
    const html = document.getElementById('results-list').innerHTML;
    assert.ok(html.indexOf('NEWER_CARD') < html.indexOf('OLDER_CARD'));
    assert.equal(html.includes('Replying'), pending);
  }
});

test('background activity badges update cards and details without promoting results', () => {
  const { context, document } = client();
  context.cards = [
    { id: 'task', label: 'OLDER_CARD', status: 'completed', activity_kind: 'reply', completed_at: '2020-01-01T00:00:00Z', runner_state: 'idle', messages: [] },
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
      assert.ok(html.includes(label === 'Preparing reply' ? 'Replying' : label));
      if (activity.task_count > 0) {
        assert.match(detail.innerHTML, /Background work/);
        assert.equal(detail.style.display, 'inline-flex');
      } else {
        assert.equal(detail.style.display, 'none');
      }
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
      id: 'task', status: 'completed', label: 'Task', activity_kind: 'reply',
      background_activity: { task_count: version === 1 ? 2 : 0, preparing_reply: version === 2 }
    }] }) };
  };
  await vm.runInContext('originalPoll()', context);
  assert.match(document.getElementById('results-list').innerHTML, />Background work<\/span>/);
  assert.doesNotMatch(document.getElementById('results-list').innerHTML, /Background work ·/);
  version++;
  await vm.runInContext('originalPoll()', context);
  assert.match(document.getElementById('results-list').innerHTML, /Replying/);
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

test('one-time login link copies the current origin and clears the success state', async () => {
  const { context, document } = client();
  const ticket = 'A'.repeat(43), calls = [], timers = [];
  const button = document.getElementById('copy-login-link-btn');
  button.disabled = false;
  document.activeElement = button;
  const fallback = document.getElementById('login-link-fallback-area');
  fallback.replaceChildren = () => {};
  context.location = { origin: 'http://127.0.0.1:4567' };
  context.navigator = { clipboard: { writeText: async value => calls.push(value) } };
  context.fetch = async (url, options) => { assert.equal(url, '/api/login-ticket'); assert.equal(options.method, 'POST'); return { ok: true, json: async () => ({ ticket }) }; };
  context.setTimeout = (fn, ms) => timers.push([fn, ms]);
  await vm.runInContext('copyLoginLink()', context);
  assert.deepEqual(calls, [`http://127.0.0.1:4567/login#ticket=${ticket}`]);
  assert.equal(document.getElementById('copy-login-link-btn').textContent, 'Copied');
  assert.equal(button['aria-disabled'], 'true');
  assert.equal(button.disabled, false);
  assert.equal(document.activeElement, button);
  assert.equal(document.getElementById('copy-login-link-status').textContent, 'Login link copied');
  assert.equal(timers[0][1], 2000); timers[0][0]();
  assert.equal(button['aria-disabled'], 'false');
  assert.equal(button.disabled, false);
  assert.equal(document.activeElement, button);
  assert.equal(document.getElementById('copy-login-link-status').textContent, '');
});

test('copy link ignores repeated activation while working or showing Copied', async () => {
  const { context, document } = client();
  document.getElementById('login-link-fallback-area').replaceChildren = () => {};
  const button = document.getElementById('copy-login-link-btn');
  button.disabled = false; document.activeElement = button;
  context.location = { origin: 'http://localhost' };
  context.navigator = { clipboard: { writeText: async () => {} } };
  let resolveRequest, requests = 0, timer;
  context.fetch = () => { requests++; return new Promise(resolve => { resolveRequest = resolve; }); };
  context.setTimeout = fn => { timer = fn; };
  const first = vm.runInContext('copyLoginLink()', context);
  await vm.runInContext('copyLoginLink()', context);
  assert.equal(requests, 1);
  assert.equal(button['aria-disabled'], 'true');
  assert.equal(button.disabled, false);
  assert.equal(document.activeElement, button);
  resolveRequest({ ok: true, json: async () => ({ ticket: 'D'.repeat(43) }) });
  await first;
  await vm.runInContext('copyLoginLink()', context);
  assert.equal(requests, 1);
  assert.equal(document.activeElement, button);
  timer();
  assert.equal(button['aria-disabled'], 'false');
});

test('one-time login clipboard failure provides a transient selected field, removed on close', async () => {
  const { context, document } = client();
  const ticket = 'B'.repeat(43), children = [];
  const fallback = document.getElementById('login-link-fallback-area');
  fallback.replaceChildren = () => { children.length = 0; };
  fallback.append = (...elements) => children.push(...elements);
  document.createElement = () => ({ dataset: {}, style: {}, setAttribute(name, value) { this[name] = value; },
    focus() { this.focused = true; }, select() { this.selected = true; } });
  context.location = { origin: 'http://localhost:4567' };
  context.navigator = { clipboard: { writeText: async () => { throw new Error('denied'); } } };
  context.fetch = async () => ({ ok: true, json: async () => ({ ticket }) });
  await vm.runInContext('copyLoginLink()', context);
  assert.equal(children[0].value, `http://localhost:4567/login#ticket=${ticket}`);
  assert.equal(children[0].readOnly, true);
  assert.equal(children[0]['aria-label'], 'One-time login link');
  assert.equal(children[0].focused, true); assert.equal(children[0].selected, true);
  assert.equal(children[0].dataset.transient, 'true');
  assert.match(children[1].textContent, /full access/);
  vm.runInContext("closeModal('settings-modal')", context);
  assert.equal(children.length, 0);
});

test('one-time link fallback does not dirty Settings and late requests cannot restore it', async () => {
  const { context, document } = client();
  const fallback = document.getElementById('login-link-fallback-area');
  fallback.replaceChildren = () => {};
  const settings = document.getElementById('settings-modal');
  const saved = { type: 'text', value: 'unchanged', dataset: {} };
  settings.querySelectorAll = () => [saved];
  const before = vm.runInContext("modalState('settings-modal')", context);
  settings.querySelectorAll = () => [saved, { type: 'text', value: 'ticket-url', dataset: { transient: 'true' } }];
  assert.equal(vm.runInContext("modalState('settings-modal')", context), before);
  let resolveRequest, copied = false;
  context.location = { origin: 'http://localhost' };
  context.navigator = { clipboard: { writeText: async () => { copied = true; } } };
  context.fetch = () => new Promise(resolve => { resolveRequest = resolve; });
  const pending = vm.runInContext('copyLoginLink()', context);
  vm.runInContext("closeModal('settings-modal')", context);
  resolveRequest({ ok: true, json: async () => ({ ticket: 'C'.repeat(43) }) });
  await pending;
  assert.equal(copied, false);
  assert.equal(document.getElementById('copy-login-link-btn')['aria-disabled'], 'false');
});

test('one-time login issuance failure is visible and restores the button', async () => {
  const { context, document } = client();
  document.getElementById('login-link-fallback-area').replaceChildren = () => {};
  const dialogs = []; context.showMessage = (...args) => dialogs.push(args);
  context.fetch = async () => ({ ok: false, json: async () => ({ error: 'Not logged in' }) });
  await vm.runInContext('copyLoginLink()', context);
  assert.equal(document.getElementById('copy-login-link-btn')['aria-disabled'], 'false');
  assert.deepEqual(dialogs[0], ['Not logged in', 'Could not create a login link']);
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
    fetch: async (url, options) => { steps.push(`post ${options.body}`); if (response instanceof Error) throw response; return response; }
  });
  vm.runInContext(fs.readFileSync('src/hihive/static/login.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return { steps, status: status.textContent };
}

test('login page clears the secret from the address bar before sending it, then opens Hive', async () => {
  const { steps } = await runLogin('#token=s3cret', { ok: true });
  assert.deepEqual(steps, ['address /login', 'post {"token":"s3cret"}', 'go /']);
});

test('one-time login page removes the ticket before exchange and explains expired links', async () => {
  const valid = await runLogin('#ticket=oneuse', { ok: true });
  assert.deepEqual(valid.steps, ['address /login', 'post {"ticket":"oneuse"}', 'go /']);
  const expired = await runLogin('#ticket=oneuse', { ok: false, status: 403, json: async () => ({ code: 'invalid_login_ticket' }) });
  assert.match(expired.status, /expired or was already used/);
  assert.match(expired.status, /Copy one-time login link/);
  assert.ok(!expired.steps.includes('go /'));
  for (const response of [
    { ok: false, status: 403, json: async () => ({ error: 'untrusted Origin oneuse' }) },
    { ok: false, status: 500, json: async () => ({ code: 'invalid_login_ticket', error: 'oneuse' }) },
    { ok: false, status: 415, json: async () => { throw new Error('oneuse'); } },
    new Error('network oneuse')
  ]) {
    const failure = await runLogin('#ticket=oneuse', response);
    assert.doesNotMatch(failure.status, /expired or was already used|oneuse/);
    assert.match(failure.status, /Could not/);
    assert.ok(!failure.steps.includes('go /'));
  }
});

test('login page explains a missing or wrong secret', async () => {
  assert.match((await runLogin('', { ok: true })).status, /This page needs a login link/);
  assert.match((await runLogin('#token=abc', new Error('secret'))).status, /run `hihive open` again/);
  const wrong = await runLogin('#token=bad', { ok: false });
  assert.match(wrong.status, /wrong/);
  assert.ok(!wrong.steps.includes('go /'));
});
