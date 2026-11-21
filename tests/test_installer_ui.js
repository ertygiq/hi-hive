const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('zero-agent onboarding copy names supported agents and next steps', () => {
  const html = fs.readFileSync('src/hihive/static/index.html', 'utf8');
  assert.match(html, /No coding agent is set up yet/);
  assert.match(html, /Claude Code 2\.1\.283\+/);
  assert.match(html, /Pi 0\.87\.1\+/);
  assert.match(html, /Settings &gt; All projects &gt; Harnesses/);
});

test('task entry points guard the zero-agent state', () => {
  const js = fs.readFileSync('src/hihive/static/app.js', 'utf8');
  assert.match(js, /function renderCodingAgentOnboarding/);
  assert.match(js, /async function openAddTask\(\)[\s\S]*No coding agent is set up yet/);
  assert.match(js, /async function openBatchAdd\(\)[\s\S]*No coding agent is set up yet/);
});
