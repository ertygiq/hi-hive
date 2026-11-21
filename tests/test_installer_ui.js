const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('zero-harness onboarding copy names supported tools and next steps', () => {
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  assert.match(html, /No harness is set up yet/);
  assert.match(html, /Claude Code 2\.1\.283\+/);
  assert.match(html, /Pi 0\.87\.1\+/);
  assert.match(html, /Settings &gt; All projects &gt; Harnesses/);
  assert.match(html, /Add harness/);
  assert.match(html, /Save harness/);
});

test('task entry points guard the zero-harness state', () => {
  const js = fs.readFileSync('src/hihive/static/app.js', 'utf8');
  assert.match(js, /function renderHarnessOnboarding/);
  assert.match(js, /async function openAddTask\(\)[\s\S]*No harness is set up yet/);
  assert.match(js, /async function openBatchAdd\(\)[\s\S]*No harness is set up yet/);
  assert.match(js, /Add harness/);
  assert.match(js, /Edit harness/);
});
