// Predefined colors for templates/tasks
const TASK_COLORS = [
  { id: 'slate',   label: 'Slate',   swatch: '#58a6ff' },
  { id: 'emerald', label: 'Emerald', swatch: '#3fb950' },
  { id: 'amber',   label: 'Amber',   swatch: '#d29922' },
  { id: 'rose',    label: 'Rose',    swatch: '#f85149' },
  { id: 'purple',  label: 'Purple',  swatch: '#bc8cff' },
  { id: 'teal',    label: 'Teal',    swatch: '#39d3c3' },
  { id: 'pink',    label: 'Pink',    swatch: '#db61a2' },
  { id: 'orange',  label: 'Orange',  swatch: '#ed9144' },
  { id: 'indigo',  label: 'Indigo',  swatch: '#7978ff' },
  { id: 'cyan',    label: 'Cyan',    swatch: '#76d3ff' },
];

function randomColor() {
  return TASK_COLORS[Math.floor(Math.random() * TASK_COLORS.length)].id;
}

function colorDot(colorId) {
  const c = TASK_COLORS.find(c => c.id === colorId);
  return c ? `<span class="tmpl-color-dot" style="background:${c.swatch}"></span>` : '';
}

function harnessById(id) {
  return (configCache?.harnesses || []).find(h => h.id === id) || null;
}

// The harness name itself is the identifier on a card — shortened so a long
// id can't crowd the meta row.
function harnessTag(name) {
  const n = (name || '').trim();
  return n.length > 14 ? `${n.slice(0, 13)}…` : n;
}

// Harness chip for task cards: a tinted, accent-bordered tag carrying the
// harness name. Colour alone was not distinguishable at this size, so the name
// does the work and the colour only reinforces it. A custom icon is kept; the
// default diamond is dropped since it says nothing.
function harnessBadgeHtml(harnessId) {
  const h = harnessById(harnessId);
  if (!h) return '';
  const icon = (h.icon || '').trim();
  const glyph = icon && icon !== '◆' ? `<span class="harness-glyph">${esc(icon)}</span>` : '';
  return `<span class="harness-badge" style="--harness-accent:${harnessSwatch(h.color)}" title="Harness: ${esc(h.name)}">`
    + `${glyph}${esc(harnessTag(h.name))}</span>`;
}

// Money the harness reports for the task's session. Only rendered when the
// "Show task cost" setting is on and the harness actually reported a number,
// so harnesses without cost data (Claude Code) add nothing to the card.
function formatCost(cost) {
  return cost < 0.01 ? '<$0.01' : `$${cost.toFixed(2)}`;
}

function costHtml(t) {
  if (!state.show_task_cost || typeof t.cost !== 'number') return '';
  return `<span class="task-cost" title="Session cost (USD)">${formatCost(t.cost)}</span>`;
}

// Badge for a follow-up that's been sent but not yet answered. Disappears the
// moment the agent's reply lands, so an idle card means "replied".
function replyStateHtml(task) {
  let label, title, kind;
  if (unreadReplies.ids.has(task.id)) {
    [label, title, kind] = ['New reply', 'A reply arrived since you last opened this task', 'unread'];
  } else if (task.activity_kind === 'reply' && (task.chat_pending || task.background_activity?.preparing_reply)) {
    [label, title, kind] = ['Replying', 'The harness is replying to your message', 'active'];
  } else if (task.background_activity?.task_count > 0) {
    [label, title, kind] = ['Background work', 'Background tasks are still running', 'active'];
  } else return '';
  return `<span class="reply-state is-${kind}" title="${title}"><span class="state-dot" aria-hidden="true"></span>${label}</span>`;
}

// Messenger-style status line above the chat input. It sits outside the chat
// scroll area, so it never covers messages. A reader at the end of the chat stays
// there when the line appears and makes the chat area shorter.
function detailAtBottom(body) {
  return body.scrollHeight - body.scrollTop - body.clientHeight <= 32;
}

function showChatTyping(visible) {
  const typing = document.getElementById('chat-typing');
  const display = visible ? 'flex' : 'none';
  if (typing.style.display === display) return;
  const body = document.getElementById('detail-body');
  const atEnd = detailPinned;
  typing.style.display = display;
  if (atEnd) scrollDetailToBottom();
}

// Background work is separate from an unanswered user input and from an idle
// process kept alive for follow-ups. Do not infer work from runner_state.
function backgroundActivityHtml(t) {
  const activity = t.background_activity || {};
  const count = Number.isInteger(activity.task_count) && activity.task_count > 0 ? activity.task_count : 0;
  if (!count && !activity.preparing_reply) return '';
  const label = count ? 'Background work' : 'Preparing reply';
  const title = count ? 'Background tasks are still running' : 'Background work finished; Claude is preparing its reply';
  return `<span class="background-activity" title="${title}"><span class="pulse" aria-hidden="true"></span>${label}</span>`;
}

// State. The server sends every project's tasks (fullState); the board shows
// only the current project's (state), so switching projects needs no request.
let state = { queue: [], workers: [], results: [], stats: {}, paused: false, max_parallel: 4, version: 0 };
let fullState = state;

// --- Projects ---
const CURRENT_PROJECT_KEY = 'hihive.current_project';
let currentProjectId = null;
try { currentProjectId = localStorage.getItem(CURRENT_PROJECT_KEY); } catch (_) {}

function projectList() {
  return configCache?.projects || [];
}

function currentProject() {
  const list = projectList();
  return list.find(p => p.id === currentProjectId) || list[0] || null;
}

function projectSwatch(colorId) {
  return TASK_COLORS.find(c => c.id === colorId)?.swatch || 'var(--text-muted)';
}

function projectView(snapshot) {
  const id = currentProject()?.id;
  const mine = t => t.project_id === id;
  const queue = (snapshot.queue || []).filter(mine);
  const workers = (snapshot.workers || []).filter(mine);
  const results = (snapshot.results || []).filter(mine);
  const count = status => results.filter(t => t.status === status).length;
  return {
    ...snapshot, queue, workers, results,
    stats: { queued: queue.length, running: workers.length,
             completed: count('completed'), failed: count('failed'), cancelled: count('cancelled') }
  };
}

// The same rule as the server: a harness lists the project, or it is shared and
// the project uses shared harnesses.
function harnessAvailableIn(harness, project) {
  if (!project) return false;
  return harness.only == null ? project.use_shared_harnesses !== false : harness.only.includes(project.id);
}

function outputActionAvailableIn(action, project) {
  return action.only == null || (project && action.only.includes(project.id));
}

function switchProject(id) {
  if (!projectList().some(p => p.id === id)) return;
  currentProjectId = id;
  try { localStorage.setItem(CURRENT_PROJECT_KEY, id); } catch (_) {}
  closeProjectMenu();
  state = projectView(fullState);
  if (selectedTaskId && !findTask(selectedTaskId)) closeDetail();
  renderProjectSwitcher();
  render();
  scheduleDeepSearch();
}

function renderProjectSwitcher() {
  const project = currentProject();
  if (!project) return;
  document.getElementById('project-name').textContent = project.name;
  document.getElementById('project-dot').style.setProperty('--project-color', projectSwatch(project.color));
  document.getElementById('project-btn').title = `Project: ${project.name} (click to switch)`;
  document.title = projectList().length > 1 ? `${project.name} · hi-hive` : 'hi-hive';
}

function renderProjectMenu() {
  const current = currentProject()?.id;
  const tasks = boardTasks(fullState);
  const items = projectList().map(p => {
    const mine = tasks.filter(t => t.project_id === p.id);
    const running = (fullState.workers || []).filter(t => t.project_id === p.id).length;
    const unread = mine.some(t => unreadReplies.ids.has(t.id));
    return `
      <button role="menuitemradio" aria-checked="${p.id === current}" class="project-item${p.id === current ? ' current' : ''}"
              onclick="switchProject('${esc(p.id)}')">
        <span class="project-dot" style="--project-color:${projectSwatch(p.color)}"></span>
        <span class="project-item-name">${esc(p.name)}</span>
        ${p.use_shared_harnesses === false ? '<span class="own-harnesses-tag" title="Uses only harnesses that select this project">own harnesses</span>' : ''}
        <span class="project-item-meta">
          ${unread ? '<span class="unread-dot" title="New follow-up reply"></span>' : ''}
          ${running ? `<span title="Running tasks">${running} running</span>` : ''}
        </span>
      </button>`;
  }).join('');
  document.getElementById('project-menu').innerHTML = `${items}<hr>
    <button role="menuitem" onclick="closeProjectMenu();openNewProject()">+ New project…</button>
    <button role="menuitem" onclick="closeProjectMenu();openSettings()">Project settings…</button>`;
}

function toggleProjectMenu() {
  const menu = document.getElementById('project-menu');
  const opening = menu.classList.contains('hidden');
  if (opening) renderProjectMenu();
  menu.classList.toggle('hidden', !opening);
  document.getElementById('project-btn').setAttribute('aria-expanded', String(opening));
  if (opening) (menu.querySelector('.current') || menu.querySelector('button'))?.focus();
}

function closeProjectMenu() {
  document.getElementById('project-menu').classList.add('hidden');
  document.getElementById('project-btn').setAttribute('aria-expanded', 'false');
}

function projectMenuOpen() {
  return !document.getElementById('project-menu').classList.contains('hidden');
}

function sharedHarnessNames() {
  return (configCache?.harnesses || []).filter(h => h.only == null).map(h => h.name);
}

function openNewProject() {
  document.getElementById('np-name').value = '';
  document.getElementById('np-work-dir').value = '';
  const shared = sharedHarnessNames();
  document.getElementById('np-harness-hint').textContent = shared.length
    ? `The project starts with all shared harnesses: ${shared.join(', ')}. You can change this later in its settings.`
    : 'There are no shared harnesses yet. After creating the project, select it in a harness under Settings → All projects.';
  openModal('new-project-modal');
  document.getElementById('np-name').focus();
}

async function submitNewProject() {
  const nameEl = document.getElementById('np-name');
  const dirEl = document.getElementById('np-work-dir');
  const name = nameEl.value.trim();
  const work_dir = dirEl.value.trim();
  if (!name) return flagInvalid(nameEl, 'Enter a name.');
  if (projectList().some(p => p.name.toLowerCase() === name.toLowerCase())) {
    return flagInvalid(nameEl, `A project named '${name}' already exists.`);
  }
  if (!work_dir) return flagInvalid(dirEl, 'Enter the folder agents work in.');
  const used = new Set(projectList().map(p => p.color));
  const color = (TASK_COLORS.find(c => !used.has(c.id)) || TASK_COLORS[0]).id;
  let res;
  try {
    res = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, work_dir, color })
    });
  } catch (e) {
    return showMessage('Could not reach the server.', 'Project not created');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return showMessage(data.error || `Request failed (${res.status})`, 'Project not created');
  closeModal('new-project-modal');
  await loadConfig();
  switchProject(data.id);
}

// Only outstanding unread tasks are retained, never a per-task read history.
const UNREAD_REPLIES_KEY = 'hive-unread-follow-up-replies';
function parseUnreadReplies(raw) {
  try {
    const value = JSON.parse(raw);
    if (value && Array.isArray(value.ids) && (value.checkedThrough === null || typeof value.checkedThrough === 'string')) {
      return { ids: new Set(value.ids.filter(id => typeof id === 'string')), checkedThrough: value.checkedThrough };
    }
  } catch (_) {}
  return { ids: new Set(), checkedThrough: null };
}
let unreadReplies;
try { unreadReplies = parseUnreadReplies(localStorage.getItem(UNREAD_REPLIES_KEY)); }
catch (_) { unreadReplies = parseUnreadReplies(null); }
let displayedConversation = null;

function saveUnreadReplies() {
  try {
    localStorage.setItem(UNREAD_REPLIES_KEY, JSON.stringify({
      ids: [...unreadReplies.ids], checkedThrough: unreadReplies.checkedThrough
    }));
  } catch (_) {} // Unavailable storage must not prevent viewing conversations.
}

function boardTasks(snapshot) {
  return [...(snapshot.queue || []), ...(snapshot.workers || []), ...(snapshot.results || [])];
}

function trackUnreadReplies(snapshot) {
  const previous = new Map(boardTasks(fullState).map(task => [task.id, task.latest_follow_up_reply_at]));
  const tasks = boardTasks(snapshot);
  const present = new Set(tasks.map(task => task.id));
  let changed = false;
  for (const id of unreadReplies.ids) {
    if (!present.has(id)) { unreadReplies.ids.delete(id); changed = true; }
  }
  let newest = unreadReplies.checkedThrough || '';
  for (const task of tasks) {
    const replyAt = task.latest_follow_up_reply_at;
    if (!replyAt) continue;
    // First visit establishes a baseline instead of flagging all old replies.
    // The prior board snapshot also covers a reply viewed before the next poll.
    if (unreadReplies.checkedThrough !== null && replyAt > unreadReplies.checkedThrough &&
        (!previous.get(task.id) || replyAt > previous.get(task.id))) {
      if (!unreadReplies.ids.has(task.id)) { unreadReplies.ids.add(task.id); changed = true; }
    }
    if (replyAt > newest) newest = replyAt;
  }
  if (newest !== unreadReplies.checkedThrough) {
    unreadReplies.checkedThrough = newest;
    changed = true;
  }
  if (changed) saveUnreadReplies();
}

function markDisplayedRepliesRead(task) {
  if (document.visibilityState !== 'visible' || selectedTaskId !== task.id || currentDetailTab !== 'output') return;
  const replyAt = task.latest_follow_up_reply_at;
  const card = boardTasks(state).find(card => card.id === task.id);
  // Never acknowledge a newer response than the conversation actually rendered.
  if (!replyAt || !card || (card.latest_follow_up_reply_at && card.latest_follow_up_reply_at > replyAt)) return;
  card.latest_follow_up_reply_at = replyAt;
  if (unreadReplies.ids.delete(task.id)) {
    saveUnreadReplies();
    renderResults();
  }
}

function unreadReplyHtml(task) {
  return unreadReplies.ids.has(task.id)
    ? '<span class="unread-reply" title="Follow-up response not yet viewed">● New reply</span>' : '';
}

document.addEventListener('visibilitychange', () => {
  if (displayedConversation) markDisplayedRepliesRead(displayedConversation);
});
globalThis.addEventListener?.('storage', event => {
  if (event.key !== UNREAD_REPLIES_KEY) return;
  unreadReplies = parseUnreadReplies(event.newValue);
  renderResults();
});
let selectedTaskId = null;
let currentDetailTab = 'output';
let configCache = null;
let reviewSectionsLocal = [];
let templatesLocal = [];
let editingReviewSectionId = null;
let pollTimer = null;
let hasLoadedState = false;

// Results pane controls. Search is in-memory (cleared on reload); sort, the
// hide-done toggle and the full-text toggle persist across reloads.
let resultsQuery = '';
const RESULTS_SORT_KEY = 'hihive.results_sort';
let resultsSort = localStorage.getItem(RESULTS_SORT_KEY) || 'completed';
const RESULTS_HIDE_DONE_KEY = 'hihive.results_hide_done';
let hideDone = localStorage.getItem(RESULTS_HIDE_DONE_KEY) === '1';
const RESULTS_DEEP_SEARCH_KEY = 'hihive.results_deep_search';
let deepSearch = localStorage.getItem(RESULTS_DEEP_SEARCH_KEY) === '1';

// Full-text search: prompt and label match client-side; model output and chat
// history are not in the state payload, so the server matches those and
// returns task IDs (/api/search). deepMatches caches the last answer, keyed by
// the query and state version it was computed for.
let deepMatches = { query: '', projectId: null, version: null, ids: new Set() };
let deepSearchTimer = null;
let deepSearchSeq = 0;

function onResultsSearchInput() {
  resultsQuery = document.getElementById('results-search').value.trim().toLowerCase();
  scheduleDeepSearch();
  renderResults();
}

function onDeepSearchToggle() {
  deepSearch = !deepSearch;
  try { localStorage.setItem(RESULTS_DEEP_SEARCH_KEY, deepSearch ? '1' : '0'); } catch (e) {}
  syncDeepSearchButton();
  scheduleDeepSearch();
  renderResults();
}

function syncDeepSearchButton() {
  const btn = document.getElementById('results-deep-search');
  btn.classList.toggle('is-active', deepSearch);
  btn.setAttribute('aria-pressed', deepSearch ? 'true' : 'false');
  document.getElementById('results-search').placeholder = deepSearch
    ? 'Search prompts, labels, responses & follow-ups…'
    : 'Search prompts & labels…';
}

function deepSearchActive() {
  return deepSearch && resultsQuery !== '';
}

// Debounced so typing does not fire a request per keystroke. A no-op when the
// cached answer already covers the current query and state version.
function scheduleDeepSearch() {
  clearTimeout(deepSearchTimer);
  if (!deepSearchActive()) return;
  if (deepMatches.query === resultsQuery && deepMatches.projectId === currentProject()?.id
      && deepMatches.version === state.version) return;
  deepSearchTimer = setTimeout(runDeepSearch, 150);
}

async function runDeepSearch() {
  const query = resultsQuery;
  const projectId = currentProject()?.id || '';
  const seq = ++deepSearchSeq;
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&project=${encodeURIComponent(projectId)}`);
    const data = await res.json();
    // Drop answers that a newer request or a changed query has overtaken.
    if (seq !== deepSearchSeq || query !== resultsQuery || projectId !== currentProject()?.id) return;
    deepMatches = { query, projectId, version: data.version, ids: new Set(data.ids || []) };
    renderResults();
  } catch (e) {
    console.error('Search error:', e);
  }
}

function deepMatchesTask(t) {
  return deepMatches.query === resultsQuery && deepMatches.projectId === currentProject()?.id
    && deepMatches.ids.has(t.id);
}

function onResultsSortChange() {
  resultsSort = document.getElementById('results-sort').value;
  try { localStorage.setItem(RESULTS_SORT_KEY, resultsSort); } catch (e) {}
  renderResults();
}

function onHideDoneToggle() {
  hideDone = !hideDone;
  try { localStorage.setItem(RESULTS_HIDE_DONE_KEY, hideDone ? '1' : '0'); } catch (e) {}
  syncHideDoneButton();
  renderResults();
}

function syncHideDoneButton() {
  const btn = document.getElementById('results-hide-done');
  btn.classList.toggle('is-active', hideDone);
  btn.setAttribute('aria-pressed', hideDone ? 'true' : 'false');
}

// Timestamp a result is sorted/grouped by. "updated" uses the latest chat message
// (last_chat_at, derived server-side from messages), falling back to completion.
function resultSortValue(t) {
  if (resultsSort === 'updated') {
    return t.last_chat_at || t.completed_at || t.created_at || '';
  }
  return t.completed_at || t.created_at || '';
}

// Collapsed fork tree state — persisted in localStorage. Default = expanded; we
// only store IDs of tasks whose forks are hidden.
const COLLAPSED_FORKS_KEY = 'hihive.collapsed_forks';
let collapsedForks = new Set();
try {
  collapsedForks = new Set(JSON.parse(localStorage.getItem(COLLAPSED_FORKS_KEY) || '[]'));
} catch (e) {
  collapsedForks = new Set();
}

function saveCollapsedForks() {
  try { localStorage.setItem(COLLAPSED_FORKS_KEY, JSON.stringify([...collapsedForks])); } catch (e) {}
}

function toggleForkCollapse(id) {
  if (collapsedForks.has(id)) collapsedForks.delete(id);
  else collapsedForks.add(id);
  saveCollapsedForks();
  renderResults();
}

// --- Polling ---
let loginNoticeShown = false;

function startPolling() {
  poll();
  pollTimer = setInterval(poll, 600);
}

async function poll() {
  let stateChanged = false;
  try {
    // The server answers 204 when the state named by `since` is still current.
    const since = hasLoadedState ? `?since=${encodeURIComponent(`${state.instance}:${state.version}`)}` : '';
    const res = await fetch(`/api/state${since}`);
    if (res.status === 204) return;
    if (res.status === 401) {
      // The browser has no valid login cookie. Tell the user once per page load.
      if (!loginNoticeShown) {
        loginNoticeShown = true;
        showMessage('This browser is not logged in to Hive. Run `hihive open` in a terminal to open Hive with a login link.', 'Login required');
      }
      return;
    }
    const data = await res.json();
    if (!hasLoadedState || data.version !== state.version || data.instance !== state.instance) {
      trackUnreadReplies(data);
      fullState = data;
      state = projectView(data);
      hasLoadedState = true;
      stateChanged = true;
    }
  } catch (e) {
    console.error('Poll error:', e);
    return;
  }
  if (!stateChanged) return;
  try {
    render();
  } catch (e) {
    console.error('Render error:', e);
  }
  // Output and chat history may have changed; refresh full-text matches.
  scheduleDeepSearch();
}

// --- Rendering ---
let openResultMenu = null;

// Lists are rebuilt from HTML on every state change. Put keyboard focus back on
// the same card (or the same action button on it) so polling cannot steal it.
function renderListPreservingFocus(list, html) {
  const active = document.activeElement;
  const card = active && list.contains?.(active) ? active.closest('[data-task-id]') : null;
  const actionLabel = card && active !== card ? active.getAttribute('aria-label') : null;
  list.innerHTML = html;
  if (openResultMenu && list.id === 'results-list') {
    const nextTrigger = list.querySelector(`[data-task-id="${openResultMenu.id}"] .card-menu-btn`);
    if (nextTrigger) {
      openResultMenu.trigger = nextTrigger;
      nextTrigger.setAttribute('aria-expanded', 'true');
      nextTrigger.closest('.result-card')?.classList.add('menu-open');
      positionResultCardMenu();
    } else closeResultCardMenu(false, true);
  }
  if (!card) return;
  const next = list.querySelector(`[data-task-id="${card.dataset.taskId}"]`);
  const action = actionLabel && next?.querySelector(`[aria-label="${actionLabel}"]`);
  (action || next)?.focus({ preventScroll: true });
}

// Enter or Space on a focused card opens it; keys on its buttons act on the button.
function positionResultCardMenu() {
  if (!openResultMenu) return;
  const { trigger, container } = openResultMenu;
  const rect = trigger.getBoundingClientRect();
  const menu = container.querySelector('[role="menu"]');
  const bounds = menu.getBoundingClientRect();
  container.style.left = `${Math.max(0, Math.min(window.innerWidth - bounds.width, rect.right - bounds.width))}px`;
  container.style.top = `${Math.max(0, rect.bottom + 4 + bounds.height > window.innerHeight ? rect.top - bounds.height - 4 : rect.bottom + 4)}px`;
}

function closeResultCardMenu(restoreFocus = false, fallback = false) {
  if (!openResultMenu) return;
  const { trigger, container } = openResultMenu;
  trigger.setAttribute('aria-expanded', 'false');
  trigger.closest('.result-card')?.classList.remove('menu-open');
  container.classList.add('hidden');
  openResultMenu = null;
  if (restoreFocus) trigger.focus();
  else if (fallback) document.getElementById('results-search')?.focus();
}

function openResultCardMenu(event, id, hasChildren, last = false) {
  event.stopPropagation();
  const trigger = event.currentTarget;
  if (openResultMenu?.trigger === trigger) { closeResultCardMenu(true); return; }
  closeResultCardMenu();
  const task = (state.results || []).find(item => item.id === id);
  if (!task) return;
  const container = document.getElementById('result-card-menu-container');
  const menu = container.querySelector('[role="menu"]');
  menu.id = `result-menu-${id}`;
  menu.innerHTML = '';
  const items = [
    [task.done ? 'Mark not done' : 'Mark done', () => toggleDone(id)],
    ['Fork…', () => openForkTask(id)],
    ...(['failed', 'cancelled'].includes(task.status) ? [['Continue', () => continueTask(id)]] : []),
    ...(hasChildren ? [] : [['Retry', () => retryTask(id)]]),
    ['Delete', () => deleteResultTask(id)]
  ];
  for (const [label, action] of items) {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = label; button.setAttribute('role', 'menuitem');
    button.addEventListener('click', e => { e.stopPropagation(); closeResultCardMenu(true); action(); });
    menu.appendChild(button);
  }
  openResultMenu = { id, trigger, container };
  trigger.setAttribute('aria-expanded', 'true');
  trigger.closest('.result-card')?.classList.add('menu-open');
  container.classList.remove('hidden');
  positionResultCardMenu();
  (last ? menu.lastElementChild : menu.firstElementChild)?.focus();
}

function resultMenuTriggerKey(event, id, hasChildren) {
  event.stopPropagation();
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    openResultCardMenu(event, id, hasChildren, event.key === 'ArrowUp');
  }
}

function resultMenuKey(event) {
  event.stopPropagation();
  if (!openResultMenu) return;
  if (event.key === 'Escape') { event.preventDefault(); closeResultCardMenu(true); return; }
  if (event.key === 'Tab') { closeResultCardMenu(true); return; }
  if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const items = [...openResultMenu.container.querySelectorAll('[role="menuitem"]')];
  const index = items.indexOf(document.activeElement);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 :
    (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
  items[next]?.focus();
}

function cardKeydown(event, id) {
  if (event.target !== event.currentTarget) return;
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    selectTask(id);
  }
}

function render() {
  renderStats();
  renderQueue();
  renderWorkers();
  renderResults();
  renderPausedBanner();
  renderHarnessOnboarding();

  if (selectedTaskId) {
    refreshDetail();
  }
}

function renderHarnessOnboarding() {
  const empty = (configCache?.harnesses || []).length === 0;
  document.getElementById('harness-onboarding').style.display = empty ? 'flex' : 'none';
  document.querySelectorAll('.header-controls button.primary, .header-controls button:nth-child(2)').forEach(button => {
    button.disabled = empty;
    button.title = empty ? 'Add a supported harness in Settings first' : '';
  });
}

function renderStats() {
  // The worker limit is shared by all projects, so the count is too.
  document.getElementById('worker-count').textContent = `${state.running_total ?? state.stats.running ?? 0} / ${state.max_parallel}`;
  document.getElementById('queue-count').textContent = state.stats.queued || 0;
  // Cancel all only has something to do while tasks are queued or running.
  const active = (state.queue?.length || 0) + (state.workers?.length || 0);
  document.getElementById('cancel-all-btn').style.display = active ? '' : 'none';
  document.getElementById('results-count').textContent = (state.stats.completed || 0) + (state.stats.failed || 0) + (state.stats.cancelled || 0);

  const btn = document.getElementById('pause-btn');
  if (state.paused) {
    btn.textContent = '▶ Resume';
    btn.classList.add('primary');
  } else {
    btn.textContent = '⏸ Pause';
    btn.classList.remove('primary');
  }
}

function renderPausedBanner() {
  const banner = document.getElementById('paused-banner');
  banner.classList.toggle('visible', state.paused);
  updateLayoutMetrics();
}

function renderQueue() {
  const list = document.getElementById('queue-list');
  document.getElementById('queue-panel').classList.toggle('is-empty', state.queue.length === 0);
  if (state.queue.length === 0) {
    list.innerHTML = '<div class="empty">No tasks in queue</div>';
    return;
  }
  renderListPreservingFocus(list, state.queue.map(t => `
    <div class="task-card ${t.id === selectedTaskId ? 'selected' : ''}"
         data-task-id="${t.id}" tabindex="0"
         onclick="selectTask('${t.id}')" onkeydown="cardKeydown(event, '${t.id}')"
         draggable="true"
         ondragstart="dragStart(event, '${t.id}')"
         ondragover="dragOver(event)"
         ondrop="drop(event, '${t.id}')">
      <div class="label">${colorDot(t.color)}${esc(t.label)}</div>
      <div class="meta">
        <span class="status-badge status-queued">queued</span>
        ${harnessBadgeHtml(t.harness)}
      </div>
      <div class="actions">
        <button class="icon-btn small" onclick="event.stopPropagation();removeTask('${t.id}')" title="Remove" aria-label="Remove">✕</button>
      </div>
    </div>
  `).join(''));
}

function renderWorkers() {
  const elsewhere = Math.max(0, (state.running_total || 0) - state.workers.length);
  const elsewhereEl = document.getElementById('workers-elsewhere');
  elsewhereEl.textContent = `+ ${elsewhere} running in other projects`;
  elsewhereEl.style.display = elsewhere ? '' : 'none';
  const list = document.getElementById('workers-list');
  document.getElementById('workers-panel').classList.toggle('is-empty', state.workers.length === 0);
  if (state.workers.length === 0) {
    list.innerHTML = '<div class="empty">No active workers</div>';
    return;
  }
  renderListPreservingFocus(list, state.workers.map(t => {
    const elapsed = t.started_at ? formatElapsed(new Date(t.started_at)) : '';
    const tail = t.output_tail || '';
    const lastLines = tail.split('\n').slice(-6).join('\n');
    const statusClass = t.status === 'reviewing' ? 'status-reviewing' : 'status-running';
    const statusText = t.status === 'reviewing' ? 'reviewing' : 'running';
    return `
      <div class="worker-card task-card ${t.id === selectedTaskId ? 'selected' : ''}"
           onclick="selectTask('${t.id}')" onkeydown="cardKeydown(event, '${t.id}')"
           data-task-id="${t.id}" tabindex="0"
           data-started-at="${t.started_at || ''}">
        <div class="worker-header">
          <span class="label">${colorDot(t.color)}${esc(t.label)}</span>
          <span class="elapsed"><span class="pulse"></span> ${elapsed}</span>
        </div>
        <div class="meta" style="margin-bottom:6px">
          <span class="status-badge ${statusClass}">${statusText}</span>
          ${harnessBadgeHtml(t.harness)}
          ${costHtml(t)}
          ${replyStateHtml(t)}
        </div>
        <div class="output-preview">${esc(lastLines) || '...'}</div>
        <div class="actions">
          <button class="icon-btn small" onclick="event.stopPropagation();cancelTask('${t.id}')" title="Cancel" aria-label="Cancel">✕</button>
        </div>
      </div>
    `;
  }).join(''));
}

function renderResults() {
  const list = document.getElementById('results-list');
  let results = state.results || [];
  if (results.length === 0) {
    if (openResultMenu) closeResultCardMenu(false, true);
    list.innerHTML = '<div class="empty">No results yet. Start an agent with + Add Task.</div>';
    return;
  }
  // Hide-done filter: show only tasks not marked as done. A hidden done parent
  // leaves its non-done children to surface as roots (parent no longer in byId).
  if (hideDone) {
    results = results.filter(t => !t.done);
    if (results.length === 0) {
      if (openResultMenu) closeResultCardMenu(false, true);
      list.innerHTML = '<div class="empty">No tasks left to do</div>';
      return;
    }
  }

  // Newest first by the selected sort key.
  const sortDesc = (a, b) => resultSortValue(b).localeCompare(resultSortValue(a));

  // Search mode: match prompt or label (plus server-side full-text matches
  // when the toggle is on), render a flat list (no fork tree).
  if (resultsQuery) {
    const matches = results
      .filter(t =>
        (t.prompt || '').toLowerCase().includes(resultsQuery) ||
        (t.label || '').toLowerCase().includes(resultsQuery) ||
        (deepSearch && deepMatchesTask(t)))
      .sort(sortDesc);
    if (matches.length === 0) {
      if (openResultMenu) closeResultCardMenu(false, true);
      list.innerHTML = '<div class="empty">No results match your search</div>';
      return;
    }
    const noChildren = new Map();
    renderListPreservingFocus(list, renderResultGroups(matches, noChildren));
    return;
  }

  const byId = new Map(results.map(t => [t.id, t]));
  const childrenByParent = new Map();
  const roots = [];
  for (const t of results) {
    if (t.parent_task_id && byId.has(t.parent_task_id)) {
      if (!childrenByParent.has(t.parent_task_id)) childrenByParent.set(t.parent_task_id, []);
      childrenByParent.get(t.parent_task_id).push(t);
    } else {
      roots.push(t);
    }
  }

  // Garbage-collect stale entries: drop collapsed IDs no longer present in any project.
  const allResultIds = new Set((fullState.results || []).map(t => t.id));
  let collapsedDirty = false;
  for (const id of collapsedForks) {
    if (!allResultIds.has(id)) { collapsedForks.delete(id); collapsedDirty = true; }
  }
  if (collapsedDirty) saveCollapsedForks();
  // Children sorted by created_at ascending — fork order, oldest fork first
  for (const arr of childrenByParent.values()) {
    arr.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  }
  // Roots sorted by the selected key, descending (newest first)
  roots.sort(sortDesc);

  renderListPreservingFocus(list, renderResultGroups(roots, childrenByParent));
}

// Render top-level results with date-group headers based on the active sort key.
function renderResultGroups(topLevel, childrenByParent) {
  let html = '';
  let lastDateLabel = '';
  for (const t of topLevel) {
    const dateLabel = resultDateLabel(resultSortValue(t));
    if (dateLabel !== lastDateLabel) {
      html += `<div class="results-date-header">${dateLabel}</div>`;
      lastDateLabel = dateLabel;
    }
    html += renderResultCard(t, 0, childrenByParent);
  }
  return html;
}

function resultHarnessHtml(harnessId) {
  const harness = harnessById(harnessId);
  if (!harness || !harness.name) return '';
  const name = String(harness.name);
  const title = esc(name).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const swatch = TASK_COLORS.find(color => color.id === harness.color)?.swatch;
  const icon = (harness.icon || '').trim();
  const custom = icon && icon !== '◆';
  const glyph = custom ? `<span class="harness-glyph" aria-hidden="true">${esc(icon)}</span>` : '';
  const dot = `<span class="harness-dot" style="--harness-accent:${swatch || 'var(--text-muted)'}" aria-hidden="true"></span>`;
  return `<span class="harness-name${custom ? ' has-glyph' : ''}" title="Harness: ${title}">${glyph}${dot}<span class="harness-name-text">${esc(name)}</span></span>`;
}

function messageCountHtml(count) {
  if (!Number.isSafeInteger(count) || count <= 0) return '';
  const label = `${count} ${count === 1 ? 'message' : 'messages'} after the initial prompt`;
  return `<span class="message-count" title="${label}" aria-label="${label}"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" aria-hidden="true"><path d="M2 1.75h8c.69 0 1.25.56 1.25 1.25v4.5c0 .69-.56 1.25-1.25 1.25H5.75L3.25 10.75V8.75H2c-.69 0-1.25-.56-1.25-1.25V3c0-.69.56-1.25 1.25-1.25z"/></svg>${count}</span>`;
}

function renderResultCard(t, depth, childrenByParent) {
  const status = t.status === 'failed' ? ['✗', 'Failed'] : t.status === 'cancelled' ? ['⊘', 'Cancelled'] : null;
  const statusIcon = status ? `<span class="result-status status-${t.status}" title="${status[1]}" aria-label="${status[1]}">${status[0]}</span>` : '';
  const duration = t.started_at && t.completed_at
    ? formatDuration(new Date(t.started_at), new Date(t.completed_at))
    : '';
  const summary = (t.status === 'failed' || t.status === 'cancelled') && t.summary
    ? `<div class="result-summary">${esc(t.summary)}</div>`
    : '';
  const doneClass = t.done ? ' task-done' : '';
  const doneIcon = t.done ? '●' : '○';
  const doneTitle = t.done ? 'Mark not done' : 'Mark done';
  const children = childrenByParent.get(t.id) || [];
  const hasChildren = children.length > 0;
  const isCollapsed = hasChildren && collapsedForks.has(t.id);
  const forkPrefix = depth > 0 ? '<span class="fork-tree-prefix">└─</span>' : '';
  const actionCount = 3 + (hasChildren ? 0 : 1) + (['failed', 'cancelled'].includes(t.status) ? 1 : 0);
  const indentStyle = `style="--fork-indent:${depth * 16}px;--action-space:${actionCount * 24 + (actionCount - 1) * 2 + 4}px"`;
  const forkAction = isCollapsed ? 'Show forks' : 'Hide forks';
  const forksLabel = hasChildren ? `<button class="fork-count" aria-expanded="${!isCollapsed}" title="${forkAction}" aria-label="${forkAction}" onclick="event.stopPropagation();toggleForkCollapse('${t.id}')">⑂ ${children.length} <span class="fork-chevron" aria-hidden="true">${isCollapsed ? '▸' : '▾'}</span></button>` : '';
  const activityHtml = backgroundActivityHtml(t);
  const waitingClass = t.chat_pending || activityHtml ? ' awaiting-reply' : '';
  const unreadClass = unreadReplies.ids.has(t.id) ? ' unread-reply-card' : '';

  let html = `
    <div class="task-card result-card${doneClass}${waitingClass}${unreadClass} ${t.id === selectedTaskId ? 'selected' : ''}"
         ${indentStyle} data-task-id="${t.id}" tabindex="0"
         onclick="selectTask('${t.id}')" onkeydown="cardKeydown(event, '${t.id}')">
      <div class="label">
        ${forkPrefix}${colorDot(t.color)}${statusIcon}<span class="result-title">${esc(t.label)}</span>
      </div>
      <div class="meta">
        ${resultHarnessHtml(t.harness)}
        ${duration ? `<span>${duration}</span>` : ''}
        ${costHtml(t)}
        ${t.has_review ? '<span>reviewed</span>' : ''}
        ${messageCountHtml(t.chat_count)}
        ${replyStateHtml(t)}
        ${forksLabel}
      </div>
      ${summary}
      <div class="actions">
        <button class="icon-btn small card-action done-btn${t.done ? ' is-done' : ''}" onclick="event.stopPropagation();toggleDone('${t.id}')" title="${doneTitle}" aria-label="${doneTitle}">${doneIcon}</button>
        <button class="icon-btn small card-action" onclick="event.stopPropagation();openForkTask('${t.id}')" title="Fork" aria-label="Fork">⑂</button>
        ${['failed', 'cancelled'].includes(t.status) ? `<button class="icon-btn small card-action" onclick="event.stopPropagation();continueTask('${t.id}')" title="Continue" aria-label="Continue">▶</button>` : ''}
        ${hasChildren ? '' : `<button class="icon-btn small card-action" onclick="event.stopPropagation();retryTask('${t.id}')" title="Retry" aria-label="Retry">↻</button>`}
        <button class="icon-btn small card-action" onclick="event.stopPropagation();deleteResultTask('${t.id}')" title="Delete" aria-label="Delete">✕</button>
        <button class="icon-btn small card-menu-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="result-menu-${t.id}" title="Actions" aria-label="Actions" onclick="openResultCardMenu(event, '${t.id}', ${hasChildren})" onkeydown="resultMenuTriggerKey(event, '${t.id}', ${hasChildren})">⋯</button>
      </div>
    </div>
  `;

  if (!isCollapsed) {
    for (const child of children) {
      html += renderResultCard(child, depth + 1, childrenByParent);
    }
  }
  return html;
}

function resultDateLabel(iso) {
  if (!iso) return 'Unknown';
  const d = new Date(iso);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diff = Math.round((today - target) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  if (diff < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: now.getFullYear() !== d.getFullYear() ? 'numeric' : undefined });
}

// --- Detail Panel ---
// The detail panel sits to the right of the board. Its width is remembered.
const DETAIL_WIDTH_KEY = 'hihive.detail_width';
let detailWidth = 0;
try { detailWidth = Number(localStorage.getItem(DETAIL_WIDTH_KEY)) || 0; } catch (e) {}

// Preserve a 360px Results column and, on wide screens, the 300px side column.
function clampDetailWidth(width) {
  const viewport = globalThis.innerWidth || 1200;
  const preferred = width || Math.round(viewport * 0.45);
  const maximum = viewport >= 1080 ? viewport - 664 : viewport - 364;
  return Math.max(400, Math.min(Math.max(400, maximum), preferred));
}

function applyDetailWidth() {
  document.getElementById('detail-panel').style.width = `${clampDetailWidth(detailWidth)}px`;
}

// Scroll position per task and tab, kept in memory only: it survives switching
// between tasks but not a page reload. Saved when the panel shows other content.
const detailScrollPositions = new Map();
let detailRenderedKey = null;
let detailPinned = true;
let detailHasNewContent = false;
let detailExpectedScroll = null;
let detailResizeObserver = null;

function updateJumpToLatest() {
  const button = document.getElementById('chat-jump-latest');
  button.style.display = detailRenderedKey?.endsWith(':output') && !detailPinned && detailHasNewContent ? '' : 'none';
}

function setDetailScroll(top) {
  const body = document.getElementById('detail-body');
  body.scrollTop = top;
  detailExpectedScroll = body.scrollTop;
}

function scrollDetailToBottom() {
  setDetailScroll(document.getElementById('detail-body').scrollHeight);
}

function onDetailScroll() {
  const body = document.getElementById('detail-body');
  if (detailExpectedScroll !== null && Math.abs(body.scrollTop - detailExpectedScroll) < 1) {
    detailExpectedScroll = null;
    return;
  }
  detailExpectedScroll = null;
  if (!detailRenderedKey?.endsWith(':output')) return;
  detailPinned = detailAtBottom(body);
  if (detailPinned) detailHasNewContent = false;
  updateJumpToLatest();
}

function jumpToLatest() {
  detailPinned = true;
  detailHasNewContent = false;
  scrollDetailToBottom();
  updateJumpToLatest();
}

function onDetailResize() {
  if (detailRenderedKey?.endsWith(':output') && detailPinned) scrollDetailToBottom();
}

function observeDetailSize() {
  if (!detailResizeObserver) return;
  const body = document.getElementById('detail-body');
  detailResizeObserver.disconnect();
  detailResizeObserver.observe(body);
  if (body.firstElementChild) detailResizeObserver.observe(body.firstElementChild);
}

function rememberDetailScroll() {
  if (detailRenderedKey) {
    const body = document.getElementById('detail-body');
    detailScrollPositions.set(detailRenderedKey, {
      top: body.scrollTop, atBottom: detailPinned, hasNewContent: detailHasNewContent,
      html: body.innerHTML,
    });
  }
}

async function selectTask(id) {
  selectedTaskId = id;
  document.getElementById('detail-panel').classList.remove('hidden');
  document.getElementById('resize-handle').style.display = 'block';
  applyDetailWidth();
  await refreshDetail();
  render();
}

async function refreshDetail() {
  if (!selectedTaskId) return;
  try {
    const requestedId = selectedTaskId;
    const res = await fetch(`/api/tasks/${requestedId}`);
    if (selectedTaskId !== requestedId) return;
    if (!res.ok) { closeDetail(); return; }
    const task = await res.json();
    if (selectedTaskId !== requestedId) return;
    renderDetail(task);
  } catch (e) {
    // silent
  }
}

const CHAT_ROLE_LABELS = { user: 'User', assistant: 'Assistant', system: 'System' };

function chatRoleLabel(m) {
  if (m.kind === 'review') return m.role === 'user' ? 'Self-review prompt' : 'Self-review response';
  return CHAT_ROLE_LABELS[m.role] || esc(m.role);
}

function messageTimestampHtml(value, now = new Date()) {
  if (typeof value !== 'string' || !value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const sameYear = date.getFullYear() === now.getFullYear();
  const today = sameYear && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
  const options = { hour: 'numeric', minute: '2-digit' };
  if (!today) Object.assign(options, { month: 'short', day: 'numeric' });
  if (!sameYear) options.year = 'numeric';
  const text = new Intl.DateTimeFormat(undefined, options).format(date);
  const title = new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'medium' }).format(date);
  const attr = text => esc(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  return `<time class="chat-timestamp" datetime="${attr(value)}" title="${attr(title)}">${esc(text)}</time>`;
}

function toggleMessageTimestamp(event) {
  if (window.matchMedia?.('(hover: none)').matches) event.currentTarget.classList.toggle('show-timestamp');
}

function renderDetail(task) {
  // The Session button needs a session viewer (claude-code-lens); hide it until one is configured.
  document.getElementById('detail-session-btn').style.display = configCache?.session_viewer_url ? '' : 'none';
  const label = task.label || '';
  const titleEl = document.getElementById('detail-title');
  if (task.parent_task_id) {
    const parent = findTask(task.parent_task_id);
    const parentLabel = parent ? parent.label : task.parent_task_id.slice(0, 8);
    const shortLabel = label.length > 60 ? label.slice(0, 57) + '...' : label;
    titleEl.innerHTML = `<a href="#" class="fork-parent-link" onclick="event.preventDefault();selectTask('${task.parent_task_id}')" title="Open parent">⑂ ${esc(parentLabel)}</a> <span class="fork-arrow">→</span> ${esc(shortLabel)}`;
  } else {
    titleEl.textContent = label.length > 80 ? label.slice(0, 77) + '...' : label;
  }
  titleEl.title = label;
  const tmpl = task.template_id ? (configCache?.templates || []).find(t => t.id === task.template_id) : null;
  const tmplEl = document.getElementById('detail-template');
  if (tmpl) {
    tmplEl.textContent = tmpl.name;
    tmplEl.style.display = 'inline-block';
  } else {
    tmplEl.style.display = 'none';
  }
  const harnessEl = document.getElementById('detail-harness');
  const harnessHtml = harnessBadgeHtml(task.harness);
  harnessEl.innerHTML = harnessHtml;
  harnessEl.style.display = harnessHtml ? 'inline-flex' : 'none';
  const costEl = document.getElementById('detail-cost');
  const costHtmlStr = costHtml(task);
  costEl.innerHTML = costHtmlStr;
  costEl.style.display = costHtmlStr ? 'inline-flex' : 'none';
  const statusEl = document.getElementById('detail-status');
  statusEl.textContent = task.status;
  statusEl.className = `status-badge status-pill status-${task.status}`;
  const activityEl = document.getElementById('detail-background-activity');
  const activityHtml = backgroundActivityHtml(task);
  activityEl.innerHTML = activityHtml;
  activityEl.style.display = activityHtml ? 'inline-flex' : 'none';

  const replyActive = task.activity_kind === 'reply' && ['starting', 'active'].includes(task.runner_state);
  const outstanding = (task.messages || []).find(m => m.role === 'user' && m.settled === false);
  const showReplyStop = replyActive && Boolean(outstanding);
  const cancelBtn = document.getElementById('detail-cancel-btn');
  cancelBtn.style.display = !showReplyStop && ['queued', 'running', 'reviewing'].includes(task.status) ? 'inline-block' : 'none';
  if (!['starting', 'active', 'idle', 'closing'].includes(task.runner_state)) stoppingWork.delete(task.id);
  const stopping = stoppingWork.get(task.id) || null;
  const closing = task.runner_state === 'closing';
  const backgroundStop = task.runner_state === 'idle' && (task.background_activity?.task_count || 0) > 0;
  const replyBtn = document.getElementById('chat-stop-btn');
  const backgroundBtn = document.getElementById('detail-background-stop-btn');
  const hideWithFocus = (button, visible) => {
    if (!visible && document.activeElement === button) document.getElementById('chat-input').focus();
    button.style.display = visible ? 'inline-flex' : 'none';
    button.disabled = Boolean(stopping);
  };
  hideWithFocus(replyBtn, showReplyStop || stopping === 'reply');
  hideWithFocus(backgroundBtn, backgroundStop || stopping === 'background');
  const count = task.background_activity?.task_count || 0;
  const replyTitle = count ? `Stop reply and ${count} background task${count === 1 ? '' : 's'}` : 'Stop reply';
  replyBtn.title = replyTitle;
  replyBtn.setAttribute('aria-label', replyTitle);
  document.getElementById('chat-typing-label').textContent = stopping === 'reply' ? 'Stopping…' : 'Agent is replying';
  const sendBtn = document.getElementById('chat-send-btn');
  sendBtn.disabled = Boolean(stopping || closing);
  sendBtn.title = stopping || closing ? `Wait until ${stopping === 'background' ? 'background work' : 'the reply'} stops` : '';

  const continueBtn = document.getElementById('detail-continue-btn');
  continueBtn.style.display = ['failed', 'cancelled'].includes(task.status) ? 'inline-block' : 'none';

  // Done and Fork live in the "⋯" menu; only finished tasks offer them.
  const isResult = ['completed', 'failed', 'cancelled'].includes(task.status);
  document.getElementById('detail-fork-btn').style.display = isResult ? '' : 'none';
  const doneBtn = document.getElementById('detail-done-btn');
  doneBtn.style.display = isResult ? '' : 'none';
  doneBtn.textContent = task.done ? 'Mark not done' : 'Mark done';
  document.getElementById('detail-done-tag').style.display = task.done ? 'inline' : 'none';

  const body = document.getElementById('detail-body');
  const renderedKey = `${task.id}:${currentDetailTab}`;
  const switched = renderedKey !== detailRenderedKey;
  if (switched) rememberDetailScroll();
  const previousTop = body.scrollTop;
  const followBottom = !switched && currentDetailTab === 'output' && detailPinned;
  const oldHtml = switched ? null : body.innerHTML;

  const canChat = ['running', 'reviewing', 'completed', 'failed'].includes(task.status);
  document.getElementById('chat-area').style.display =
    (currentDetailTab === 'output' && canChat) ? 'flex' : 'none';

  document.querySelectorAll('.detail-tab').forEach(tab => {
    const active = tab.textContent.toLowerCase() === currentDetailTab;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });

  showChatTyping(currentDetailTab === 'output' && (stopping === 'reply' ||
    (task.activity_kind === 'reply' && replyActive) ||
    (task.activity_kind === 'review' && Boolean(task.chat_pending || task.background_activity?.preparing_reply))));

  switch (currentDetailTab) {
    case 'output': {
      let html = '';
      const failureDetails = (task.failure_details || '').trim();

      // The initial prompt has its own tab. All answers and subsequent inputs
      // stay in conversation order, including steering before the first answer.
      const messages = (task.messages || []).filter(m =>
        !(m.role === 'user' && m.kind === 'initial') && m.delivery !== 'scheduled');

      // Restored tasks can use their last answer as the failure text; showing
      // it again in the Failure box would only repeat it.
      const failureShownAsAnswer = messages.some(m =>
        m.role === 'assistant' && (m.content || '').trim() === failureDetails);
      if ((task.status === 'failed' || task.status === 'cancelled') && failureDetails && !failureShownAsAnswer) {
        html += `<div class="error-block">
          <div class="section-label">Failure</div>
          <pre class="error-pre">${esc(failureDetails)}</pre>
        </div>`;
        html += `<hr class="section-divider">`;
      }
      if (!messages.some(m => m.role === 'assistant')) html += '<pre>No output yet...</pre>';
      if (messages.length > 0) {
        html += messages.map(m => {
          const isPending = m.delivery === 'pending';
          const isCancelled = m.delivery === 'cancelled';
          const isMarkdown = m.role === 'assistant' && typeof marked !== 'undefined';
          const content = isMarkdown ? renderMarkdownSafely(m.content) : esc(m.content);
          const statusSuffix = isPending ? ' (sending...)' : (isCancelled ? ' (cancelled)' : '');
          return `
            <div class="chat-message ${m.role}${isCancelled ? ' cancelled' : ''}">
              <div class="chat-role" onclick="toggleMessageTimestamp(event)">${chatRoleLabel(m)}${messageTimestampHtml(m.timestamp)}</div>
              <div class="chat-content${isMarkdown ? ' rendered-output' : ''}${isPending ? ' pending' : ''}">${content}${statusSuffix}</div>
              ${m.error ? `<div class="error-block" role="alert">${esc(m.error)}</div>` : ''}
            </div>
          `;
        }).join('');
      }

      const wrappedHtml = `<div class="chat-history">${html}</div>`;
      if (!switched && !detailPinned && oldHtml !== wrappedHtml) detailHasNewContent = true;
      body.innerHTML = wrappedHtml;
      displayedConversation = task;
      markDisplayedRepliesRead(task);
      break;
    }

    case 'prompt':
      body.innerHTML = `<pre>${esc((task.messages || []).find(m => m.role === 'user' && m.kind === 'initial')?.content || '')}</pre>`;
      break;

  }

  if (switched) {
    const saved = detailScrollPositions.get(renderedKey);
    detailPinned = currentDetailTab === 'output' && (saved ? saved.atBottom : true);
    detailHasNewContent = saved?.hasNewContent || Boolean(saved && currentDetailTab === 'output'
      && !detailPinned && saved.html !== body.innerHTML);
    setDetailScroll(saved ? (detailPinned ? body.scrollHeight : saved.top)
      : (detailPinned ? body.scrollHeight : 0));
    detailRenderedKey = renderedKey;
  } else {
    setDetailScroll(followBottom ? body.scrollHeight : previousTop);
  }
  observeDetailSize();
  updateJumpToLatest();
}

function switchDetailTab(tab) {
  currentDetailTab = tab;
  refreshDetail();
}

function toggleDetailMenu() {
  const menu = document.getElementById('detail-menu');
  const opening = menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !opening);
  document.getElementById('detail-menu-btn').setAttribute('aria-expanded', String(opening));
  if (opening) [...menu.querySelectorAll('button')].find(b => b.offsetParent)?.focus();
}

function closeDetailMenu() {
  document.getElementById('detail-menu').classList.add('hidden');
  document.getElementById('detail-menu-btn').setAttribute('aria-expanded', 'false');
}

function detailMenuOpen() {
  return !document.getElementById('detail-menu').classList.contains('hidden');
}

function closeDetail() {
  rememberDetailScroll();
  detailRenderedKey = null;
  closeDetailMenu();
  selectedTaskId = null;
  displayedConversation = null;
  currentDetailTab = 'output';
  document.getElementById('detail-panel').classList.add('hidden');
  document.getElementById('resize-handle').style.display = 'none';
  updateLayoutMetrics();
  render();
}

// --- Session fullscreen ---
let sessionTaskId = null;
let activeSessionId = null;
const sessionFrames = new Map();

function copySessionId() {
  if (!selectedTaskId) return;
  const task = findTask(selectedTaskId);
  if (!task) return;
  navigator.clipboard.writeText(task.session_id).then(() => {
    const btn = document.getElementById('copy-session-id-btn');
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = 'Copy session ID'; }, 1500);
  });
}

function copyTaskId() {
  if (!selectedTaskId) return;
  navigator.clipboard.writeText(selectedTaskId).then(() => {
    const btn = document.getElementById('copy-task-id-btn');
    btn.textContent = 'Copied';
    setTimeout(() => { closeDetailMenu(); btn.textContent = 'Copy task ID'; }, 800);
  });
}

function openSession() {
  if (!selectedTaskId) return;
  const task = findTask(selectedTaskId);
  if (!task) return;
  sessionTaskId = selectedTaskId;
  mountSessionFrame(task.session_id);
  // Show chat area pinned at bottom only if task supports chat
  const canChat = ['running', 'reviewing', 'completed', 'failed'].includes(task.status);
  const chatArea = document.getElementById('chat-area');
  chatArea.style.display = canChat ? 'flex' : 'none';
  chatArea.style.position = 'fixed';
  chatArea.style.bottom = '0';
  chatArea.style.left = '0';
  chatArea.style.right = '0';
  chatArea.style.zIndex = '201';
  chatArea.style.background = 'var(--surface)';
  updateLayoutMetrics();
  document.getElementById('session-overlay').classList.remove('hidden');
}

function closeSession() {
  document.getElementById('session-overlay').classList.add('hidden');
  // Restore chat area to normal flow
  const chatArea = document.getElementById('chat-area');
  chatArea.style.position = '';
  chatArea.style.bottom = '';
  chatArea.style.left = '';
  chatArea.style.right = '';
  chatArea.style.zIndex = '';
  chatArea.style.background = '';
  sessionTaskId = null;
  updateLayoutMetrics();
}

function mountSessionFrame(sessionId) {
  const host = document.getElementById('session-frame-host');

  if (activeSessionId && sessionFrames.has(activeSessionId)) {
    sessionFrames.get(activeSessionId).style.display = 'none';
  }

  let frame = sessionFrames.get(sessionId);
  if (!frame) {
    frame = document.createElement('iframe');
    frame.className = 'session-fullscreen';
    const viewerUrl = configCache?.session_viewer_url;
    frame.src = `${viewerUrl}/#${encodeURIComponent(sessionId)}`;
    frame.dataset.sessionId = sessionId;
    sessionFrames.set(sessionId, frame);
    host.appendChild(frame);
  }

  frame.style.display = 'block';
  activeSessionId = sessionId;
}

function findTask(id) {
  return state.queue.find(t => t.id === id)
    || state.workers.find(t => t.id === id)
    || state.results.find(t => t.id === id);
}

// --- Actions ---
// POST JSON. Returns null on success, or a message to show the user.
async function postJson(url, body = {}) {
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (res.ok) return null;
    const err = await res.json().catch(() => ({}));
    return err.error || `Request failed (${res.status})`;
  } catch (e) {
    return 'Could not reach the server.';
  }
}

async function togglePause() {
  const endpoint = state.paused ? '/api/control/resume' : '/api/control/pause';
  await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  });
  poll();
}

async function removeTask(id) {
  const res = await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || 'Delete failed', 'Task not deleted');
    return;
  }
  if (selectedTaskId === id) closeDetail();
  poll();
}

async function cancelTask(id) {
  if (!id) return;
  if (!await showConfirm('Cancel this task?')) return;
  const error = await postJson(`/api/tasks/${id}/cancel`);
  if (error) showMessage(error, 'Task not cancelled');
  poll();
}

async function cancelAllTasks() {
  const activeCount = (state.queue?.length || 0) + (state.workers?.length || 0);
  if (activeCount === 0) return;
  if (!await showConfirm(`Cancel all queued and running tasks in ${currentProject()?.name || 'this project'}?`)) return;
  await fetch('/api/tasks/cancel_all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: currentProject()?.id })
  });
  poll();
}

let retryTaskId = null;

async function retryTask(id) {
  if (countDescendants(id) > 0) {
    showMessage('A task with forks cannot be retried. Delete its forks first.', 'Cannot retry');
    return;
  }
  await loadConfig();
  const task = findTask(id);
  retryTaskId = id;
  populateHarnessSelect(document.getElementById('retry-harness'), task?.harness);
  openModal('retry-modal');
}

async function submitRetry() {
  if (!retryTaskId) return;
  const harness = document.getElementById('retry-harness').value || null;
  const error = await postJson(`/api/tasks/${retryTaskId}/retry`, { harness });
  if (error) {
    showMessage(error, 'Retry failed');
    return;
  }
  closeModal('retry-modal');
  retryTaskId = null;
  poll();
}

async function deleteResultTask(id) {
  const childCount = countDescendants(id);
  const message = childCount > 0
    ? `This task has ${childCount} fork${childCount === 1 ? '' : 's'}. Deleting will delete the entire subtree. Continue?`
    : 'Delete this task result?';
  if (!await showConfirm(message)) return;
  await removeTask(id);
}

function countDescendants(id) {
  const all = [...(state.queue || []), ...(state.workers || []), ...(state.results || [])];
  let count = 0;
  const queue = [id];
  while (queue.length) {
    const cur = queue.shift();
    for (const t of all) {
      if (t.parent_task_id === cur) {
        count++;
        queue.push(t.id);
      }
    }
  }
  return count;
}

async function continueTask(id) {
  const error = await postJson(`/api/tasks/${id}/continue`);
  if (error) showMessage(error, 'Could not continue');
  poll();
}

async function toggleDone(id) {
  const task = (state.results || []).find(t => t.id === id);
  if (task && task.done) {
    if (!await showConfirm('Mark this task as not done?')) return;
  }
  try {
    const res = await fetch(`/api/tasks/${id}/done`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showMessage(err.error || 'Could not update the Done mark.', 'Could not update Done');
      return;
    }
  } catch (error) {
    showMessage('Connection lost. The Done mark may or may not have been saved. Check the task before trying again.', 'Could not update Done');
  }
  poll();
}

const stoppingWork = new Map();

async function stopWork(kind) {
  const task = displayedConversation;
  const message = (task?.messages || []).find(m => m.role === 'user' && m.settled === false);
  if (!task || !task.stop_token || stoppingWork.has(task.id) || (kind === 'reply' && !message)) return;
  stoppingWork.set(task.id, kind);
  renderDetail(task);
  try {
    const res = await fetch(`/api/tasks/${task.id}/stop-work`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, token: task.stop_token, msg_id: message?.id || null })
    });
    if (!res.ok) {
      stoppingWork.delete(task.id);
      if (res.status === 409) {
        const latest = await fetch(`/api/tasks/${task.id}`);
        if (!latest.ok) throw new Error('Could not refresh the current activity.');
        const current = await latest.json();
        if (selectedTaskId === task.id) renderDetail(current);
      } else {
        const error = await res.json().catch(() => ({}));
        showMessage(error.error || 'The stop request failed.', kind === 'reply' ? 'Could not stop the reply' : 'Could not stop background work');
        if (selectedTaskId === task.id) renderDetail(task);
      }
    }
  } catch (error) {
    stoppingWork.delete(task.id);
    showMessage('Could not refresh or contact Hive. Check the task before trying again.', kind === 'reply' ? 'Could not stop the reply' : 'Could not stop background work');
    if (selectedTaskId === task.id) renderDetail(task);
  }
  poll();
}

let uncertainChatRequest = null;

function resizeChatInput(input = document.getElementById('chat-input')) {
  if (!input) return;
  const body = document.getElementById('detail-body');
  const previousTop = body.scrollTop;
  const atBottom = detailPinned;
  const borderHeight = Math.max(0, (input.offsetHeight || 0) - (input.clientHeight || 0));
  input.style.height = '0px';
  const naturalHeight = input.scrollHeight + borderHeight;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
  const maxHeight = Math.min(240, viewportHeight * 0.35);
  input.style.height = `${Math.min(naturalHeight, maxHeight)}px`;
  input.style.overflowY = naturalHeight > maxHeight ? 'auto' : 'hidden';
  setDetailScroll(atBottom ? body.scrollHeight : previousTop);
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message || !selectedTaskId || stoppingWork.has(selectedTaskId) || displayedConversation?.runner_state === 'closing') return;

  const taskId = selectedTaskId;
  const request = uncertainChatRequest?.taskId === taskId && uncertainChatRequest.message === message
    ? uncertainChatRequest : { taskId, message, message_id: crypto.randomUUID() };
  input.value = '';
  resizeChatInput(input);
  jumpToLatest();
  let res;
  try {
    res = await fetch(`/api/tasks/${taskId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request)
    });
  } catch (error) {
    uncertainChatRequest = request;
    input.value = message;
    resizeChatInput(input);
    showMessage('Connection lost. Retrying this text will reuse its message ID to avoid duplicate delivery.', 'Delivery unknown');
    return;
  }
  uncertainChatRequest = res.status >= 500 ? request : null;
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || 'Follow-up failed', 'Follow-up failed');
    input.value = message;
    resizeChatInput(input);
    return;
  }
  resizeChatInput(input);
  poll();
  setTimeout(poll, 1000);
  setTimeout(poll, 3000);
  setTimeout(poll, 6000);
}

// --- Add Task ---
async function openAddTask() {
  await loadConfig();
  if ((configCache?.harnesses || []).length === 0) {
    showMessage('No harness is set up yet. Install Claude Code 2.1.283+ or Pi 0.87.1+, then run the installer again, or add a harness in Settings > All projects > Harnesses.', 'No harness');
    return;
  }
  document.getElementById('add-label').value = '';
  document.getElementById('add-prompt').value = '';
  populateHarnessSelect(document.getElementById('add-harness'));
  openModal('add-task-modal');
  document.getElementById('add-prompt').focus();
}

async function submitAddTask() {
  const promptEl = document.getElementById('add-prompt');
  const prompt = promptEl.value.trim();
  if (!prompt) return flagInvalid(promptEl, 'Enter a prompt.');
  const label = document.getElementById('add-label').value.trim();
  const harness = document.getElementById('add-harness').value || null;

  const error = await postJson('/api/tasks', { prompt, label: label || null, harness, project_id: currentProject()?.id });
  if (error) return showMessage(error, 'Task not added');
  closeModal('add-task-modal');
  poll();
}

// --- Fork Task ---
let forkParentId = null;

function openForkTask(parentId) {
  if (!parentId) return;
  const parent = findTask(parentId);
  if (!parent) return;
  forkParentId = parentId;
  document.getElementById('fork-parent-label').textContent = parent.label || '(no label)';
  document.getElementById('fork-label').value = '';
  document.getElementById('fork-prompt').value = '';
  openModal('fork-task-modal');
  document.getElementById('fork-prompt').focus();
}

async function submitForkTask() {
  if (!forkParentId) return;
  const promptEl = document.getElementById('fork-prompt');
  const prompt = promptEl.value.trim();
  if (!prompt) return flagInvalid(promptEl, 'Enter a prompt for the fork.');
  const label = document.getElementById('fork-label').value.trim();
  const error = await postJson(`/api/tasks/${forkParentId}/fork`, { prompt, label: label || null });
  if (error) return showMessage(error, 'Fork failed');
  closeModal('fork-task-modal');
  forkParentId = null;
  poll();
}

// --- Run Template (one task per input line, or one task without input) ---
function templateHasInputPlaceholder(template) {
  return /%\{input\}/.test(template?.template || '');
}

function updateBatchModal() {
  const id = document.getElementById('batch-template').value;
  const tmpl = (configCache?.templates || []).find(t => t.id === id);
  const hasInput = templateHasInputPlaceholder(tmpl);

  document.getElementById('batch-inputs-group').style.display = hasInput ? '' : 'none';
  document.getElementById('batch-no-input-info').style.display = hasInput ? 'none' : '';
  const preview = document.getElementById('batch-template-preview');
  preview.textContent = tmpl?.template || '';
  preview.parentElement.style.display = tmpl ? '' : 'none';
  updateBatchSubmitLabel();

  // Preselect the template's harness if it pins one, else the project's default.
  populateHarnessSelect(document.getElementById('batch-harness'), tmpl?.harness);
}

function batchInputLines() {
  return document.getElementById('batch-inputs').value.split('\n').map(s => s.trim()).filter(Boolean);
}

// The button says how many tasks it will create.
function updateBatchSubmitLabel() {
  const id = document.getElementById('batch-template').value;
  const tmpl = (configCache?.templates || []).find(t => t.id === id);
  const count = batchInputLines().length;
  const tasks = templateHasInputPlaceholder(tmpl) ? count : 1;
  document.getElementById('batch-submit-btn').textContent = tasks === 0 ? 'Run tasks' : `Run ${tasks} task${tasks === 1 ? '' : 's'}`;
}

async function openBatchAdd() {
  await loadConfig();
  if ((configCache?.harnesses || []).length === 0) {
    showMessage('No harness is set up yet. Install Claude Code 2.1.283+ or Pi 0.87.1+, then run the installer again, or add a harness in Settings > All projects > Harnesses.', 'No harness');
    return;
  }
  const select = document.getElementById('batch-template');
  const templates = projectTemplates();
  // Without templates there is nothing to run: go straight to where they are added.
  if (templates.length === 0) return openSettings('templates-heading');
  select.innerHTML = templates.map(t =>
    `<option value="${t.id}">${esc(t.name)}${templateHasInputPlaceholder(t) ? ' (input)' : ''}</option>`
  ).join('');
  document.getElementById('batch-inputs').value = '';
  updateBatchModal();
  openModal('batch-modal');
}

async function submitBatch() {
  const templateId = document.getElementById('batch-template').value;
  if (!templateId) return showMessage('Add a template in Settings first.', 'No template');
  const tmpl = (configCache?.templates || []).find(t => t.id === templateId);

  let inputs = [''];
  if (templateHasInputPlaceholder(tmpl)) {
    inputs = batchInputLines();
    if (inputs.length === 0) return flagInvalid(document.getElementById('batch-inputs'), 'Enter at least one input.');
  }

  const harness = document.getElementById('batch-harness').value || null;
  const error = await postJson('/api/tasks/batch', { template_id: templateId, inputs, harness });
  if (error) return showMessage(error, 'Tasks not added');
  closeModal('batch-modal');
  poll();
}

// --- Settings ---
// Settings has two tabs: the current project, and what all projects share.
// Edits stay local until Save sends everything in one request.
let projectsLocal = [];
let outputActionsLocal = [];
let settingsTab = 'project';
let selectedProjectColor = null;
let editingOutputActionTag = null;
const PROJECT_FIELDS = ['id', 'name', 'color', 'work_dir', 'use_shared_harnesses', 'default_harness'];

async function loadConfig() {
  const res = await fetch('/api/config');
  configCache = await res.json();
  reviewSectionsLocal = (configCache.review_sections || []).map(s => ({ ...s }));
  templatesLocal = (configCache.templates || []).map(t => ({ ...t }));
  harnessesLocal = (configCache.harnesses || []).map(h => ({ ...h, only: h.only ? [...h.only] : null }));
  projectsLocal = (configCache.projects || []).map(p => Object.fromEntries(PROJECT_FIELDS.map(k => [k, p[k]])));
  outputActionsLocal = (configCache.output_actions || []).map(a => ({ ...a, only: a.only ? [...a.only] : null }));
  if (!projectList().some(p => p.id === currentProjectId)) currentProjectId = projectList()[0]?.id || null;
  state = projectView(fullState);
  renderProjectSwitcher();
}

function projectTemplates() {
  const id = currentProject()?.id;
  return (configCache?.templates || []).filter(t => t.project_id === id);
}

// The project being edited in Settings, with the form's unsaved switch value.
function editedProject() {
  const project = projectsLocal.find(p => p.id === currentProject()?.id);
  if (!project) return null;
  return { ...project, use_shared_harnesses: document.getElementById('prj-use-shared').checked };
}

function editedProjectHarnesses() {
  const project = editedProject();
  return harnessesLocal.filter(h => harnessAvailableIn(h, project));
}

async function openSettings(scrollToId, tab = 'project') {
  await loadConfig();
  const project = projectsLocal.find(p => p.id === currentProject()?.id);
  document.getElementById('prj-name').value = project?.name || '';
  document.getElementById('prj-work-dir').value = project?.work_dir || '';
  document.getElementById('prj-use-shared').checked = project?.use_shared_harnesses !== false;
  renderProjectColorPicker(project?.color || 'slate');
  editingOutputActionTag = null;

  document.getElementById('cfg-max-parallel').value = configCache.max_parallel;
  document.getElementById('cfg-session-viewer-url').value = configCache.session_viewer_url || '';
  const preventSleepEl = document.getElementById('cfg-prevent-sleep');
  preventSleepEl.checked = !!configCache.prevent_sleep_when_running;
  document.getElementById('cfg-prevent-sleep-help').textContent = configCache.sleep_prevention_requirement
    + ' Keeps awake for 5 minutes after activity stops.';
  preventSleepEl.onchange = () => {
    if (preventSleepEl.checked && !configCache.sleep_prevention_available) {
      preventSleepEl.checked = false;
      showMessage(configCache.sleep_prevention_requirement, 'Not available');
    }
  };
  document.getElementById('cfg-show-task-cost').checked = !!configCache.show_task_cost;
  document.getElementById('restart-server-btn').style.display = configCache.restart_available ? '' : 'none';

  renderProjectHarnessSection(project?.default_harness);
  renderHarnessesList();
  renderTemplatesList();
  renderReviewSectionsList();
  renderProjectOutputActions();
  renderOutputActionsList();
  switchSettingsTab(tab);

  openModal('settings-modal');
  if (scrollToId) document.getElementById(scrollToId).scrollIntoView({ block: 'start' });
}

function switchSettingsTab(tab) {
  settingsTab = tab;
  for (const name of ['project', 'global']) {
    document.getElementById(`settings-tab-${name}`).setAttribute('aria-selected', String(name === tab));
    document.getElementById(`settings-pane-${name}`).classList.toggle('hidden', name !== tab);
  }
  const name = document.getElementById('prj-name').value.trim() || currentProject()?.name || '';
  document.getElementById('settings-project-tab-label').textContent = `Project: ${name}`;
  document.getElementById('settings-project-dot').style.setProperty('--project-color', projectSwatch(selectedProjectColor));
  // Harness availability may have changed in the other tab.
  if (tab === 'project') {
    renderProjectHarnessSection(document.getElementById('prj-default-harness').value);
    renderProjectOutputActions();
  }
}

function renderProjectColorPicker(selectedId) {
  selectedProjectColor = selectedId;
  document.getElementById('prj-color-picker').innerHTML = TASK_COLORS.map(c => `
    <button type="button" class="color-swatch ${c.id === selectedId ? 'selected' : ''}"
            style="background:${c.swatch}" title="${c.label}" aria-label="${c.label}"
            onclick="renderProjectColorPicker('${c.id}');switchSettingsTab('project')"></button>
  `).join('');
}

function onUseSharedChange() {
  renderProjectHarnessSection(document.getElementById('prj-default-harness').value);
}

function renderProjectHarnessSection(selectedDefault) {
  const project = editedProject();
  if (!project) return;
  const available = editedProjectHarnesses();
  document.getElementById('prj-harness-chips').innerHTML = available.length
    ? available.map(h => `<span class="chip" style="--harness-accent:${harnessSwatch(h.color)}"><span class="harness-glyph">${esc(h.icon || '◆')}</span>${esc(h.name)}</span>`).join('')
    : '<span class="muted">No harnesses. Turn on shared harnesses, or select this project in a harness under "All projects".</span>';
  const select = document.getElementById('prj-default-harness');
  const chosen = available.some(h => h.id === selectedDefault) ? selectedDefault : available[0]?.id;
  select.innerHTML = available.map(h =>
    `<option value="${esc(h.id)}" ${h.id === chosen ? 'selected' : ''}>${esc(h.icon || '◆')} ${esc(h.name)}</option>`).join('');
  select.disabled = available.length === 0;
  const own = harnessesLocal.filter(h => h.only && h.only.includes(project.id)).map(h => h.name);
  document.getElementById('prj-use-shared-help').textContent =
    'Shared harnesses are the ones set to "All projects". Turn this off to keep this project on its own harnesses'
    + (own.length ? ` (${own.join(', ')})` : '')
    + '. Agents started in this project can then only start tasks with those harnesses.';
}

function renderProjectOutputActions() {
  const project = editedProject();
  const actions = outputActionsLocal.filter(a => outputActionAvailableIn(a, project));
  document.getElementById('prj-output-actions').innerHTML = actions.length
    ? actions.map(a => `<span class="chip">${esc(a.label || a.tag)}</span>`).join('')
    : '<span class="muted">None</span>';
}

// Copy the project form into projectsLocal before saving.
function applyProjectForm() {
  const id = currentProject()?.id;
  projectsLocal = projectsLocal.map(p => p.id !== id ? p : {
    ...p,
    name: document.getElementById('prj-name').value.trim(),
    work_dir: document.getElementById('prj-work-dir').value.trim(),
    color: selectedProjectColor,
    use_shared_harnesses: document.getElementById('prj-use-shared').checked,
    default_harness: document.getElementById('prj-default-harness').value || null
  });
}

let deletingProjectId = null;

async function deleteCurrentProject() {
  const project = currentProject();
  if (!project) return;
  if (projectList().length <= 1) {
    return showMessage('Hive needs at least one project. Create another project before deleting this one.', 'Cannot delete');
  }
  const active = [...(fullState.queue || []), ...(fullState.workers || [])].filter(t => t.project_id === project.id);
  if (active.length) {
    return showMessage(`Cancel the ${active.length} queued or running task${active.length === 1 ? '' : 's'} before deleting this project.`, 'Cannot delete');
  }
  const snapshot = modalSnapshots.get('settings-modal');
  if (snapshot !== undefined && snapshot !== modalState('settings-modal')
      && !await showConfirm('Discard unsaved Settings changes before deleting this project?')) return;
  closeModal('settings-modal');
  deletingProjectId = project.id;
  const tasks = boardTasks(fullState).filter(t => t.project_id === project.id).length;
  const templates = (configCache?.templates || []).filter(t => t.project_id === project.id).length;
  const sections = (configCache?.review_sections || []).filter(s => s.project_id === project.id).length;
  document.getElementById('delete-project-title').textContent = `Delete project "${project.name}"?`;
  document.getElementById('delete-project-summary').textContent =
    `This permanently deletes from hi-hive: ${tasks} task${tasks === 1 ? '' : 's'} and their conversations, `
    + `${templates} template${templates === 1 ? '' : 's'}, and ${sections} review section${sections === 1 ? '' : 's'}. You cannot undo this.`;
  document.getElementById('delete-project-files').textContent =
    `Files in ${project.work_dir} are not changed. Harnesses are kept; this project is removed from their availability lists.`;
  document.getElementById('delete-project-home').textContent =
    `You can also copy the hi-hive data folder yourself: ${configCache?.hive_home || ''}`;
  document.getElementById('delete-project-name-label').textContent = `To confirm, type the project name: ${project.name}`;
  document.getElementById('delete-project-name').value = '';
  updateDeleteProjectButton();
  openModal('delete-project-modal');
  document.getElementById('delete-project-name').focus();
}

function closeDeleteProjectModal() {
  deletingProjectId = null;
  closeModal('delete-project-modal');
}

function updateDeleteProjectButton() {
  const project = projectList().find(p => p.id === deletingProjectId);
  document.getElementById('delete-project-confirm').disabled =
    !project || document.getElementById('delete-project-name').value.trim() !== project.name;
}

async function downloadBackup() {
  const button = document.getElementById('delete-project-backup');
  button.disabled = true;
  try {
    const res = await fetch('/api/backup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `Request failed (${res.status})`);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const filename = disposition.match(/filename="([^"]+)"/)?.[1] || 'hi-hive-backup.zip';
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    showMessage(error.message || 'Backup download failed.', 'Backup not downloaded');
  } finally {
    button.disabled = false;
  }
}

async function confirmDeleteProject() {
  const project = projectList().find(p => p.id === deletingProjectId);
  if (!project || document.getElementById('delete-project-name').value.trim() !== project.name) return;
  let res;
  try {
    res = await fetch(`/api/projects/${encodeURIComponent(project.id)}`, { method: 'DELETE' });
  } catch (e) {
    return showMessage('Could not reach the server.', 'Project not deleted');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return showMessage(err.error || `Request failed (${res.status})`, 'Project not deleted');
  }
  closeDeleteProjectModal();
  await loadConfig();
  switchProject(projectList()[0].id);
  poll();
  showMessage(`Project "${project.name}" deleted.`);
}

function renderTemplatesList() {
  const list = document.getElementById('templates-list');
  const id = currentProject()?.id;
  const templates = templatesLocal.filter(t => t.project_id === id);
  if (templates.length === 0) {
    list.innerHTML = '<div class="empty">No templates in this project</div>';
    return;
  }
  list.innerHTML = templates.map(t => {
    const colorDot = t.color ? `<span class="tmpl-color-dot" style="background:${TASK_COLORS.find(c => c.id === t.color)?.swatch || 'transparent'}"></span>` : '';
    return `
    <div class="template-item">
      <span class="template-name">${colorDot}${esc(t.name)}</span>
      <div>
        <button class="small" onclick="editTemplate('${t.id}')">Edit</button>
        <button class="small danger" onclick="deleteTemplate('${t.id}')">Delete</button>
      </div>
    </div>
  `;
  }).join('');
}

function projectReviewSections() {
  const id = currentProject()?.id;
  return reviewSectionsLocal.filter(s => s.project_id === id);
}

function renderReviewSectionsList() {
  const list = document.getElementById('review-sections-list');
  const sections = projectReviewSections();
  if (sections.length === 0) {
    list.innerHTML = '<div class="empty">No review sections in this project</div>';
    return;
  }
  list.innerHTML = sections.map(section => `
    <div class="template-item">
      <span class="template-name">${esc(section.name || '(no name)')}</span>
      <div>
        <button class="small" onclick="editReviewSection('${section.id}')">Edit</button>
        <button class="small danger" onclick="removeReviewSection('${section.id}')">Delete</button>
      </div>
    </div>
  `).join('');
}

// Review sections are edited in their own dialog, like templates, and saved
// with the rest of Settings.
function addReviewSectionForm() {
  editingReviewSectionId = null;
  document.getElementById('review-section-modal-title').textContent = 'Add Review Section';
  document.getElementById('review-section-name').value = '';
  document.getElementById('review-section-prompt').value = '';
  openModal('review-section-modal');
  document.getElementById('review-section-name').focus();
}

function editReviewSection(id) {
  const section = reviewSectionsLocal.find(s => s.id === id);
  if (!section) return;
  editingReviewSectionId = id;
  document.getElementById('review-section-modal-title').textContent = 'Edit Review Section';
  document.getElementById('review-section-name').value = section.name || '';
  document.getElementById('review-section-prompt').value = section.prompt || '';
  openModal('review-section-modal');
}

function saveReviewSection() {
  const nameEl = document.getElementById('review-section-name');
  const promptEl = document.getElementById('review-section-prompt');
  const name = nameEl.value.trim();
  const prompt = promptEl.value.trim();
  if (!name) return flagInvalid(nameEl, 'Enter a name.');
  if (projectReviewSections().some(s => s.id !== editingReviewSectionId && (s.name || '').toLowerCase() === name.toLowerCase())) {
    return flagInvalid(nameEl, `A review section named '${name}' already exists.`);
  }
  if (!prompt) return flagInvalid(promptEl, 'Enter the review prompt.');

  reviewSectionsLocal = editingReviewSectionId
    ? reviewSectionsLocal.map(s => s.id === editingReviewSectionId ? { ...s, name, prompt } : s)
    : [...reviewSectionsLocal, { id: crypto.randomUUID(), project_id: currentProject()?.id, name, prompt }];
  closeModal('review-section-modal');
  renderReviewSectionsList();
}

async function removeReviewSection(id) {
  if (!await showConfirm('Delete this review section?')) return;
  reviewSectionsLocal = reviewSectionsLocal.filter(s => s.id !== id);
  templatesLocal = templatesLocal.map(template => ({
    ...template,
    review_section_ids: (template.review_section_ids || []).filter(sectionId => sectionId !== id)
  }));
  renderReviewSectionsList();
}

function proposedHarnessAvailable(harness, project) {
  return harness && project && (harness.only == null
    ? project.use_shared_harnesses !== false : harness.only.includes(project.id));
}

function harnessAvailabilityWarning() {
  const oldProjects = new Map((configCache?.projects || []).map(p => [p.id, p]));
  const newProjects = new Map(projectsLocal.map(p => [p.id, p]));
  const oldHarnesses = new Map((configCache?.harnesses || []).map(h => [h.id, h]));
  const newHarnesses = new Map(harnessesLocal.map(h => [h.id, h]));
  const affected = task => proposedHarnessAvailable(oldHarnesses.get(task.harness), oldProjects.get(task.project_id))
    && !proposedHarnessAvailable(newHarnesses.get(task.harness), newProjects.get(task.project_id));
  const queued = (fullState.queue || []).filter(affected).length;
  const completed = (fullState.results || []).filter(affected).length;
  if (queued + completed === 0) return null;
  return `Harness availability is changing. ${queued} queued task${queued === 1 ? '' : 's'} and `
    + `${completed} completed task${completed === 1 ? '' : 's'} use harnesses that would no longer be available. Save anyway?`;
}

async function persistSettingsConfig() {
  const res = await fetch('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      max_parallel: parseInt(document.getElementById('cfg-max-parallel').value, 10),
      session_viewer_url: document.getElementById('cfg-session-viewer-url').value.trim(),
      prevent_sleep_when_running: document.getElementById('cfg-prevent-sleep').checked,
      show_task_cost: document.getElementById('cfg-show-task-cost').checked,
      projects: projectsLocal,
      review_sections: reviewSectionsLocal,
      templates: templatesLocal,
      harnesses: harnessesLocal,
      output_actions: outputActionsLocal
    })
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || 'Failed to save settings');
  }
}

async function restartServer() {
  if (!await showConfirm('Restart the server? Running tasks will be interrupted.')) return;
  try {
    await fetch('/api/control/restart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
  } catch (e) {
    // Server shuts down before responding; that's fine.
  }
  closeModal('settings-modal');
  // Wait for the installed service manager to respawn the server, then resume polling.
  setTimeout(() => poll(), 2000);
}

async function saveSettings() {
  const nameEl = document.getElementById('prj-name');
  const dirEl = document.getElementById('prj-work-dir');
  if (!nameEl.value.trim() || !dirEl.value.trim()) {
    switchSettingsTab('project');
    return flagInvalid(nameEl.value.trim() ? dirEl : nameEl, nameEl.value.trim() ? 'Enter the folder agents work in.' : 'Enter a name.');
  }
  if (harnessesLocal.length > 0 && editedProjectHarnesses().length === 0) {
    switchSettingsTab('project');
    showMessage('This project would have no harness. Turn on shared harnesses, or select this project for one under "All projects".', 'Settings not saved');
    return;
  }
  applyProjectForm();
  const availabilityWarning = harnessAvailabilityWarning();
  if (availabilityWarning && !await showConfirm(availabilityWarning)) return;
  try {
    await persistSettingsConfig();
  } catch (e) {
    showMessage(e.message, 'Settings not saved');
    return;
  }
  closeModal('settings-modal');
  await loadConfig();
  render();
  poll();
}

// --- Harnesses ---
// Defined once for all projects; each says where it is available.
let harnessesLocal = [];
let editingHarnessId = null;
let selectedHarnessColor = null;

function harnessSwatch(colorId) {
  return TASK_COLORS.find(c => c.id === colorId)?.swatch || 'var(--text-muted)';
}

function harnessTypeLabel(typeId) {
  return (configCache?.harness_types || []).find(t => t.id === typeId)?.label || typeId || '';
}

function availabilityLabel(item) {
  if (item.only == null) return 'All projects';
  const names = item.only.map(id => projectsLocal.find(p => p.id === id)?.name).filter(Boolean);
  return names.length ? `Only ${names.join(', ')}` : 'No projects';
}

function availabilityTagHtml(item) {
  return `<span class="availability-tag${item.only == null ? '' : ' only'}">${esc(availabilityLabel(item))}</span>`;
}

function renderHarnessesList() {
  const list = document.getElementById('harnesses-list');
  if (harnessesLocal.length === 0) {
    list.innerHTML = '<div class="empty">No harnesses configured. Add a harness for Claude Code or Pi after installing it on this computer.</div>';
    return;
  }
  list.innerHTML = harnessesLocal.map(h => `
    <div class="template-item">
      <span class="template-name" style="--harness-accent:${harnessSwatch(h.color)}">
        <span class="harness-glyph">${esc(h.icon || '◆')}</span>${esc(h.name)}
        <span class="harness-type-tag">${esc(harnessTypeLabel(h.type))}</span>
        ${availabilityTagHtml(h)}
      </span>
      <div>
        <button class="small" onclick="editHarness('${h.id}')">Edit</button>
        <button class="small danger" onclick="deleteHarness('${h.id}')">Delete</button>
      </div>
    </div>
  `).join('');
}

function renderHarnessTypeOptions(selectedId) {
  const sel = document.getElementById('harness-type');
  const types = configCache?.harness_types || [];
  sel.innerHTML = types.map(t =>
    `<option value="${esc(t.id)}" ${t.id === selectedId ? 'selected' : ''}>${esc(t.label)}</option>`
  ).join('');
  sel.onchange = updateHarnessTypeFields;
  updateHarnessTypeFields();
}

// Fields that only make sense for one harness type. Sessions dir is Pi-only:
// it is where Pi's transcripts are read from for task cost.
function updateHarnessTypeFields() {
  const type = document.getElementById('harness-type').value;
  document.getElementById('harness-sessions-dir-group').style.display = type === 'pi' ? '' : 'none';
}

function renderHarnessColorPicker(selectedId) {
  const container = document.getElementById('harness-color-picker');
  container.innerHTML = TASK_COLORS.map(c => `
    <button type="button" class="color-swatch ${c.id === selectedId ? 'selected' : ''}"
            style="background:${c.swatch}" title="${c.label}"
            onclick="pickHarnessColor('${c.id}')"></button>
  `).join('');
  selectedHarnessColor = selectedId;
}

function pickHarnessColor(id) {
  selectedHarnessColor = id;
  document.querySelectorAll('#harness-color-picker .color-swatch').forEach(el => {
    el.classList.toggle('selected', el.title === TASK_COLORS.find(c => c.id === id)?.label);
  });
}

// "Available in" for a harness: all projects, or the ticked projects only.
function renderHarnessAvailability(only) {
  document.getElementById(only == null ? 'harness-avail-all' : 'harness-avail-only').checked = true;
  const selected = only || [currentProject()?.id];
  document.getElementById('harness-avail-projects').innerHTML = projectsLocal.map(p => `
    <label class="checkbox-item"><input type="checkbox" value="${esc(p.id)}" ${selected.includes(p.id) ? 'checked' : ''}>
      <span class="project-dot" style="--project-color:${projectSwatch(p.color)}"></span>${esc(p.name)}</label>
  `).join('');
  updateHarnessAvailabilityFields();
}

function updateHarnessAvailabilityFields() {
  const only = document.getElementById('harness-avail-only').checked;
  document.getElementById('harness-avail-projects').style.display = only ? '' : 'none';
}

function readHarnessAvailability() {
  if (!document.getElementById('harness-avail-only').checked) return null;
  return [...document.querySelectorAll('#harness-avail-projects input:checked')].map(input => input.value);
}

function addHarnessForm() {
  editingHarnessId = null;
  document.getElementById('harness-modal-title').textContent = 'Add harness';
  document.getElementById('harness-name').value = '';
  document.getElementById('harness-icon').value = '◆';
  document.getElementById('harness-command').value = '';
  document.getElementById('harness-args').value = '';
  document.getElementById('harness-sessions-dir').value = '';
  renderHarnessTypeOptions(configCache?.harness_types?.[0]?.id || 'claude-code');
  renderHarnessColorPicker(randomColor());
  renderHarnessAvailability(null);
  openModal('harness-modal');
}

function editHarness(id) {
  const h = harnessesLocal.find(x => x.id === id);
  if (!h) return;
  editingHarnessId = id;
  document.getElementById('harness-modal-title').textContent = 'Edit harness';
  document.getElementById('harness-name').value = h.name || '';
  document.getElementById('harness-icon').value = h.icon || '◆';
  document.getElementById('harness-command').value = h.command || '';
  document.getElementById('harness-args').value = (h.args || []).join(' ');
  document.getElementById('harness-sessions-dir').value = h.sessions_dir || '';
  renderHarnessTypeOptions(h.type || 'claude-code');
  renderHarnessColorPicker(h.color || randomColor());
  renderHarnessAvailability(h.only);
  openModal('harness-modal');
}

function saveHarness() {
  const name = document.getElementById('harness-name').value.trim();
  const command = document.getElementById('harness-command').value.trim();
  if (!name) return flagInvalid(document.getElementById('harness-name'), 'Enter a name.');
  if (!command) return flagInvalid(document.getElementById('harness-command'), 'Enter a command.');
  const only = readHarnessAvailability();
  if (only && only.length === 0) {
    return flagInvalid(document.getElementById('harness-avail-projects'), 'Select at least one project, or choose "All projects".');
  }
  const icon = document.getElementById('harness-icon').value.trim() || '◆';
  const type = document.getElementById('harness-type').value;
  const argsRaw = document.getElementById('harness-args').value.trim();
  const args = argsRaw ? argsRaw.split(/\s+/) : [];
  const sessionsDirRaw = document.getElementById('harness-sessions-dir').value.trim();
  const sessions_dir = type === 'pi' && sessionsDirRaw ? sessionsDirRaw : null;

  const fields = { name, icon, type, command, args, sessions_dir, color: selectedHarnessColor, only };
  if (editingHarnessId) {
    harnessesLocal = harnessesLocal.map(h => h.id === editingHarnessId ? { ...h, ...fields } : h);
  } else {
    harnessesLocal.push({ id: crypto.randomUUID(), ...fields });
  }

  closeModal('harness-modal');
  renderHarnessesList();
}

async function deleteHarness(id) {
  if (!await showConfirm('Delete this harness?')) return;
  harnessesLocal = harnessesLocal.filter(h => h.id !== id);
  renderHarnessesList();
}

// --- Output actions (availability only; the rest is set in config.json) ---
function renderOutputActionsList() {
  const list = document.getElementById('output-actions-list');
  if (outputActionsLocal.length === 0) {
    list.innerHTML = '<div class="empty">No output actions</div>';
    return;
  }
  list.innerHTML = outputActionsLocal.map(a => {
    const open = editingOutputActionTag === a.tag;
    const selected = a.only || [];
    return `
    <div class="template-item output-action-item">
      <span class="template-name">${esc(a.label || a.tag)} <span class="harness-type-tag">&lt;${esc(a.tag)}&gt;</span> ${availabilityTagHtml(a)}</span>
      <button class="small" aria-expanded="${open}" onclick="toggleOutputActionEditor('${esc(a.tag)}')">${open ? 'Done' : 'Edit'}</button>
      ${open ? `<fieldset class="availability availability-editor">
        <legend>Available in</legend>
        <label class="radio-item"><input type="radio" name="oa-${esc(a.tag)}" ${a.only == null ? 'checked' : ''}
               onchange="setOutputActionOnly('${esc(a.tag)}', null)"> All projects</label>
        <label class="radio-item"><input type="radio" name="oa-${esc(a.tag)}" ${a.only != null ? 'checked' : ''}
               onchange="setOutputActionOnly('${esc(a.tag)}', [currentProject()?.id])"> Only selected projects</label>
        ${a.only != null ? `<div class="availability-projects">${projectsLocal.map(p => `
          <label class="checkbox-item"><input type="checkbox" ${selected.includes(p.id) ? 'checked' : ''}
                 onchange="toggleOutputActionProject('${esc(a.tag)}', '${esc(p.id)}', this.checked)">
            <span class="project-dot" style="--project-color:${projectSwatch(p.color)}"></span>${esc(p.name)}</label>`).join('')}</div>` : ''}
      </fieldset>` : ''}
    </div>`;
  }).join('');
}

function toggleOutputActionEditor(tag) {
  editingOutputActionTag = editingOutputActionTag === tag ? null : tag;
  renderOutputActionsList();
}

function setOutputActionOnly(tag, only) {
  outputActionsLocal = outputActionsLocal.map(a => a.tag === tag ? { ...a, only } : a);
  renderOutputActionsList();
}

function toggleOutputActionProject(tag, projectId, checked) {
  outputActionsLocal = outputActionsLocal.map(a => {
    if (a.tag !== tag) return a;
    const only = new Set(a.only || []);
    if (checked) only.add(projectId); else only.delete(projectId);
    return { ...a, only: [...only] };
  });
  renderOutputActionsList();
}

// Populate a template's harness <select> with "Use project default" + the
// harnesses available in the project.
function renderTemplateHarnessOptions(selectedId) {
  const sel = document.getElementById('tmpl-harness');
  sel.innerHTML = ['<option value="">Use project default</option>']
    .concat(editedProjectHarnesses().map(h =>
      `<option value="${esc(h.id)}" ${h.id === selectedId ? 'selected' : ''}>${esc(h.icon || '◆')} ${esc(h.name)}</option>`))
    .join('');
}

// Action modals (Add Task / Batch / Retry) send an explicit harness id from the
// harnesses available in the current project. The project default is preselected and tagged.
function populateHarnessSelect(sel, selectedId) {
  const project = currentProject();
  const available = new Set(project?.available_harnesses || []);
  const harnesses = (configCache?.harnesses || []).filter(h => available.has(h.id));
  const def = project?.default_harness;
  const chosen = available.has(selectedId) ? selectedId : def;
  sel.innerHTML = harnesses.map(h =>
    `<option value="${esc(h.id)}" ${h.id === chosen ? 'selected' : ''}>${esc(h.icon || '◆')} ${esc(h.name)}${h.id === def ? ' (default)' : ''}</option>`
  ).join('');
}

// --- Templates ---
let editingTemplateId = null;

let selectedTemplateColor = null;

function renderColorPicker(selectedId) {
  const container = document.getElementById('tmpl-color-picker');
  container.innerHTML = TASK_COLORS.map(c => `
    <button type="button" class="color-swatch ${c.id === selectedId ? 'selected' : ''}"
            style="background:${c.swatch}"
            title="${c.label}"
            onclick="pickColor('${c.id}')"></button>
  `).join('');
  selectedTemplateColor = selectedId;
}

function pickColor(id) {
  selectedTemplateColor = id;
  document.querySelectorAll('#tmpl-color-picker .color-swatch').forEach(el => {
    el.classList.toggle('selected', el.title === TASK_COLORS.find(c => c.id === id)?.label);
  });
}

function addTemplateForm() {
  editingTemplateId = null;
  document.getElementById('template-modal-title').textContent = 'Add Template';
  document.getElementById('tmpl-name').value = '';
  document.getElementById('tmpl-body').value = '';
  document.getElementById('tmpl-work-dir').value = '';
  document.getElementById('tmpl-agent-args').value = '';
  renderColorPicker(randomColor());
  renderTemplateHarnessOptions('');
  renderTemplateReviewSections([]);
  openModal('template-modal');
}

function editTemplate(id) {
  const tmpl = templatesLocal.find(t => t.id === id);
  if (!tmpl) return;
  editingTemplateId = id;
  document.getElementById('template-modal-title').textContent = 'Edit Template';
  document.getElementById('tmpl-name').value = tmpl.name;
  document.getElementById('tmpl-body').value = tmpl.template;
  document.getElementById('tmpl-work-dir').value = tmpl.work_dir || '';
  const args = tmpl.agent_args || [];
  document.getElementById('tmpl-agent-args').value = Array.isArray(args) ? args.join(' ') : args;
  renderColorPicker(tmpl.color || randomColor());
  renderTemplateHarnessOptions(tmpl.harness || '');
  renderTemplateReviewSections(tmpl.review_section_ids || []);
  openModal('template-modal');
}

function renderTemplateReviewSections(selectedIds) {
  const container = document.getElementById('tmpl-review-sections');
  const sections = projectReviewSections();
  if (sections.length === 0) {
    container.innerHTML = '<div class="empty">No review sections in this project</div>';
    return;
  }

  container.innerHTML = sections.map(section => `
    <label class="checkbox-item">
      <input type="checkbox" value="${esc(section.id)}" ${selectedIds.includes(section.id) ? 'checked' : ''}>
      <span>${esc(section.name || section.prompt.substring(0, 80))}</span>
    </label>
  `).join('');
}

// Template edits stay local, like harnesses and review sections, until the
// Settings Save button sends them all in one request.
function saveTemplate() {
  const nameEl = document.getElementById('tmpl-name');
  const bodyEl = document.getElementById('tmpl-body');
  const name = nameEl.value.trim();
  const template = bodyEl.value;
  const projectId = editingTemplateId
    ? templatesLocal.find(t => t.id === editingTemplateId)?.project_id : currentProject()?.id;
  if (!name) return flagInvalid(nameEl, 'Enter a name.');
  if (templatesLocal.some(t => t.id !== editingTemplateId && t.project_id === projectId && t.name.toLowerCase() === name.toLowerCase())) {
    return flagInvalid(nameEl, `A template named '${name}' already exists in this project.`);
  }
  if (!template.trim()) return flagInvalid(bodyEl, 'Enter the template text.');

  const record = {
    id: editingTemplateId || crypto.randomUUID(),
    project_id: projectId,
    name,
    template,
    review_section_ids: Array.from(document.querySelectorAll('#tmpl-review-sections input:checked')).map(input => input.value),
    color: selectedTemplateColor,
    work_dir: document.getElementById('tmpl-work-dir').value.trim() || null,
    agent_args: document.getElementById('tmpl-agent-args').value.trim(),
    harness: document.getElementById('tmpl-harness').value || null
  };
  templatesLocal = editingTemplateId
    ? templatesLocal.map(t => t.id === editingTemplateId ? record : t)
    : [...templatesLocal, record];
  closeModal('template-modal');
  renderTemplatesList();
}

async function deleteTemplate(id) {
  if (!await showConfirm('Delete this template?')) return;
  templatesLocal = templatesLocal.filter(t => t.id !== id);
  renderTemplatesList();
}

// --- Drag & Drop ---
let draggedId = null;

function dragStart(e, id) {
  draggedId = id;
  e.target.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
}

function dragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  const card = e.target.closest('.task-card');
  if (card) card.classList.add('drag-over');
}

function drop(e, targetId) {
  e.preventDefault();
  document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
  document.querySelectorAll('.dragging').forEach(el => el.classList.remove('dragging'));

  if (!draggedId || draggedId === targetId) return;

  const queueIds = state.queue.map(t => t.id);
  const fromIdx = queueIds.indexOf(draggedId);
  const toIdx = queueIds.indexOf(targetId);
  if (fromIdx === -1 || toIdx === -1) return;

  queueIds.splice(fromIdx, 1);
  queueIds.splice(toIdx, 0, draggedId);

  fetch('/api/queue/reorder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order: queueIds, project_id: currentProject()?.id })
  }).then(() => poll());

  draggedId = null;
}

// --- Resize ---
function initResize() {
  const handle = document.getElementById('resize-handle');
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = document.getElementById('detail-panel').offsetWidth;
    document.body.style.userSelect = 'none';
    const onMove = (event) => {
      detailWidth = clampDetailWidth(startWidth + (startX - event.clientX));
      applyDetailWidth();
    };
    const onUp = () => {
      document.body.style.userSelect = '';
      try { localStorage.setItem(DETAIL_WIDTH_KEY, String(detailWidth)); } catch (err) {}
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function updateLayoutMetrics() {
  const root = document.documentElement;
  const header = document.querySelector('header');
  const banner = document.getElementById('paused-banner');
  const chatArea = document.getElementById('chat-area');
  const headerHeight = (header ? header.offsetHeight : 0)
    + (banner && banner.classList.contains('visible') ? banner.offsetHeight : 0);
  const chatHeight = sessionTaskId && chatArea && chatArea.style.display !== 'none'
    ? chatArea.offsetHeight
    : 0;

  root.style.setProperty('--header-height', `${headerHeight}px`);
  root.style.setProperty('--chat-area-height', `${chatHeight}px`);
}

// --- Message / confirm modals ---
function showMessage(message, title = 'Notice') {
  document.getElementById('message-title').textContent = title;
  document.getElementById('message-text').textContent = message;
  openModal('message-modal');
}

let confirmResolve = null;

function showConfirm(message) {
  return new Promise(resolve => {
    confirmResolve = resolve;
    document.getElementById('confirm-message').textContent = message;
    document.getElementById('confirm-modal').classList.remove('hidden');
  });
}

function resolveConfirm(value) {
  document.getElementById('confirm-modal').classList.add('hidden');
  if (confirmResolve) {
    confirmResolve(value);
    confirmResolve = null;
  }
}

// --- Modal helpers ---
// Each open modal remembers its form state when it opens. Closing it without
// submitting (✕, Cancel, Escape, a click outside) asks before discarding edits.
const modalSnapshots = new Map();

// In action dialogs only typed text is worth protecting: a dropdown choice is
// one click to redo. In editors every field, dropdowns included, is an edit.
const TEXT_ONLY_MODALS = new Set(['add-task-modal', 'fork-task-modal', 'batch-modal', 'retry-modal', 'new-project-modal']);

function modalState(id) {
  const selector = TEXT_ONLY_MODALS.has(id) ? 'input[type="text"], textarea' : 'input, textarea, select';
  const fields = [...document.getElementById(id).querySelectorAll(selector)]
    .map(el => (el.type === 'checkbox' ? el.checked : el.value));
  const local = {
    'settings-modal': () => [harnessesLocal, projectsLocal, reviewSectionsLocal, templatesLocal, outputActionsLocal,
                             selectedProjectColor],
    'template-modal': () => [selectedTemplateColor],
    'harness-modal': () => [selectedHarnessColor]
  }[id];
  return JSON.stringify([fields, local ? local() : []]);
}

function openModal(id) {
  const modal = document.getElementById(id);
  modal.querySelectorAll('.field-error').forEach(el => el.remove());
  modal.querySelectorAll('.invalid').forEach(el => el.classList.remove('invalid'));
  modal.classList.remove('hidden');
  modalSnapshots.set(id, modalState(id));
}

// Close after the modal's work is done (saved or submitted).
function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
  modalSnapshots.delete(id);
}

async function requestCloseModal(id) {
  if (id === 'delete-project-modal') return closeDeleteProjectModal();
  const snapshot = modalSnapshots.get(id);
  if (snapshot !== undefined && snapshot !== modalState(id) &&
      !await showConfirm('Discard unsaved changes?')) return;
  closeModal(id);
}

// Mark a required field, say what is missing, and clear the mark on edit.
function flagInvalid(el, message) {
  el.classList.add('invalid');
  let note = el.nextElementSibling;
  if (!note || !note.classList.contains('field-error')) {
    note = document.createElement('div');
    note.className = 'field-error';
    el.after(note);
  }
  note.textContent = message;
  el.focus();
  el.addEventListener('input', () => { el.classList.remove('invalid'); note.remove(); }, { once: true });
}

document.addEventListener('click', (e) => {
  if (openResultMenu) closeResultCardMenu();
  if (detailMenuOpen() && !e.target.closest('.menu-wrap')) closeDetailMenu();
  if (projectMenuOpen() && !e.target.closest('.project-switcher')) closeProjectMenu();
  if (!e.target.classList.contains('modal-overlay')) return;
  if (e.target.id === 'confirm-modal') resolveConfirm(false);
  else requestCloseModal(e.target.id);
});

// Escape closes only the topmost layer: confirm, menu, modal, session, detail.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (confirmResolve) return resolveConfirm(false);
  if (openResultMenu) { closeResultCardMenu(true); return; }
  if (projectMenuOpen()) {
    closeProjectMenu();
    document.getElementById('project-btn').focus();
    return;
  }
  if (detailMenuOpen()) {
    closeDetailMenu();
    document.getElementById('detail-menu-btn').focus();
    return;
  }
  const openModals = document.querySelectorAll('.modal-overlay:not(.hidden)');
  if (openModals.length) return requestCloseModal(openModals[openModals.length - 1].id);
  if (sessionTaskId) return closeSession();
  if (selectedTaskId) closeDetail();
});

// --- Utility ---
function esc(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Task output is untrusted model/log text. Escape raw HTML before parsing so tags
// like "<main>" stay literal text. Entity references are decoded only in the
// resulting text nodes, never as HTML, so "&lt;script&gt;" remains harmless text.
function renderMarkdownSafely(source) {
  return postProcessMarkdown(marked.parse(
    source
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
  ));
}

function postProcessMarkdown(html) {
  const template = document.createElement('template');
  template.innerHTML = html;

  // Strip bare-URL autolinks that Marked adds for plain hostnames while keeping
  // normal markdown links intact.
  template.content.querySelectorAll('a[href]').forEach(link => {
    const text = (link.textContent || '').trim();
    const href = (link.getAttribute('href') || '').trim();
    const normalizedHref = href.replace(/^https?:\/\//, '').replace(/\/$/, '');
    const normalizedText = text.replace(/\/$/, '');

    if (normalizedText === normalizedHref) {
      link.replaceWith(document.createTextNode(text));
      return;
    }
    // Open links outside Hive so the board stays where it was.
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  });

  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  let textNode;
  while ((textNode = walker.nextNode())) {
    textNode.data = decodeHtmlEntities(textNode.data);
    textNodes.push(textNode);
  }
  textNodes.forEach(insertOutputActionButtons);

  return template.innerHTML;
}

// Output actions: a configured tag in agent output, such as
// <run-id>ab12</run-id>, becomes a button that runs the tag's command. Tags in
// code are examples, not actions, so they stay as text.
function insertOutputActionButtons(textNode) {
  const project = currentProject();
  const actions = ((configCache && configCache.output_actions) || []).filter(a => outputActionAvailableIn(a, project));
  if (!actions.length || textNode.parentElement?.closest('code, pre, a')) return;
  const parts = splitOutputActions(textNode.data, actions.map(a => a.tag));
  if (parts.length === 1 && typeof parts[0] === 'string') return;

  const fragment = document.createDocumentFragment();
  parts.forEach(part => {
    if (typeof part === 'string') {
      fragment.appendChild(document.createTextNode(part));
      return;
    }
    const action = actions.find(a => a.tag === part.tag);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'output-action';
    button.dataset.outputAction = part.tag;
    button.dataset.value = part.value;
    button.title = action.label || `Run ${part.tag}`;
    button.textContent = part.value;
    fragment.appendChild(button);
  });
  textNode.replaceWith(fragment);
}

// Splits text into plain strings and {tag, value} parts for the given tags.
// Tags are validated on the server as lowercase letters, digits and dashes.
function splitOutputActions(text, tags) {
  if (!tags.length) return [text];
  const pattern = new RegExp(`<(${tags.join('|')})>([^<>\\s]{1,200})</\\1>`, 'g');
  const parts = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push({ tag: match[1], value: match[2] });
    last = match.index + match[0].length;
  }
  if (last < text.length || !parts.length) parts.push(text.slice(last));
  return parts;
}

// Success needs no notice; only a failure is shown.
async function runOutputAction(button) {
  button.disabled = true;
  const error = await postJson(`/api/output_actions/${encodeURIComponent(button.dataset.outputAction)}/run`,
    { value: button.dataset.value });
  button.disabled = false;
  if (error) showMessage(error, 'Action failed');
}

// Decode one layer of named/numeric entities without ever parsing surrounding
// output as markup. Unknown entity names are left unchanged.
function decodeHtmlEntities(str) {
  const textarea = document.createElement('textarea');
  return String(str).replace(/&(?:#(?:x[0-9a-f]+|\d+)|[0-9a-z]+);/gi, entity => {
    textarea.innerHTML = entity;
    return textarea.value;
  });
}

function escDecodedEntities(str) {
  return esc(decodeHtmlEntities(str));
}

function formatElapsed(start) {
  const secs = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatDuration(start, end) {
  const secs = Math.floor((end.getTime() - start.getTime()) / 1000);
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}


function updateWorkerElapsedTimes() {
  document.querySelectorAll('.worker-card[data-started-at]').forEach(card => {
    const startedAt = card.dataset.startedAt;
    if (!startedAt) return;
    const elapsedEl = card.querySelector('.elapsed');
    if (!elapsedEl) return;
    elapsedEl.innerHTML = `<span class="pulse"></span> ${formatElapsed(new Date(startedAt))}`;
  });
}

setInterval(() => {
  if (state.workers.length > 0) updateWorkerElapsedTimes();
  if (selectedTaskId && state.workers.some(w => w.id === selectedTaskId)) refreshDetail();
}, 2000);

// --- Init ---
initResize();
const detailBody = document.getElementById('detail-body');
detailBody.addEventListener('scroll', onDetailScroll);
if (typeof ResizeObserver !== 'undefined') {
  detailResizeObserver = new ResizeObserver(onDetailResize);
  observeDetailSize();
}
detailBody.addEventListener('click', event => {
  const button = event.target.closest('button.output-action');
  if (button) runOutputAction(button);
});
document.getElementById('results-sort').value = resultsSort;
syncHideDoneButton();
syncDeepSearchButton();
resizeChatInput();
updateLayoutMetrics();
window.addEventListener('resize', () => {
  resizeChatInput();
  updateLayoutMetrics();
  if (selectedTaskId) applyDetailWidth();
});
// Harness badges and template names need the config, so load it before the
// first render. A failed load still starts polling.
loadConfig().catch(() => {}).finally(startPolling);
