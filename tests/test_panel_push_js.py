"""The service worker shows every push it gets (iPhone requires it) and a tap
opens the conversation, in an open panel window when there is one."""
import json
import shutil
import subprocess

import pytest

from core.web import panel

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node.js not installed")

HARNESS = r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const data = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const handlers = {}, shown = [], opened = [], log = [];
function client(url) {
  return { url, focused: false,
           focus() { this.focused = true; log.push('focus ' + this.url); return Promise.resolve(this); },
           navigate(to) { log.push('navigate ' + to); this.url = to; return Promise.resolve(this); },
           postMessage(message) { log.push(message.type + ' ' + message.url); } };
}
const windows = data.windows.map(client);
const context = {
  URL, Response, console,
  caches: { open: async () => ({ addAll: async () => {} }), keys: async () => [] },
  self: {
    location: { origin: 'https://panel.test' },
    addEventListener: (name, fn) => handlers[name] = fn,
    registration: { showNotification: async (title, options) => shown.push({ title, options }) },
    clients: {
      matchAll: async () => windows,
      openWindow: async url => { opened.push(url); return client(url); },
      claim: async () => {}
    },
    skipWaiting: async () => {}
  }
};
vm.runInNewContext(data.source.replace('__APP_VERSION__', '"test"'), context);
async function dispatch(name, event) {
  let work = Promise.resolve();
  handlers[name]({ ...event, waitUntil: p => { work = p; } });
  await work;
}
"""


def _run(script: str, **extra) -> None:
    source = (panel._STATIC / "sw.js").read_text()
    result = subprocess.run([NODE or "node", "-e", HARNESS + script],
                            input=json.dumps({"source": source, "windows": [], **extra}),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_a_push_is_always_shown_with_its_conversation():
    _run(r"""
(async () => {
  const payload = { title: 'Nova mensagem de Ana.', url: '/painel/#inbox/7', tag: 'inbox-7' };
  await dispatch('push', { data: { json: () => payload, text: () => JSON.stringify(payload) } });
  assert.equal(shown.length, 1);
  assert.equal(shown[0].title, 'Nova mensagem de Ana.');
  assert.equal(shown[0].options.tag, 'inbox-7');
  assert.equal(shown[0].options.renotify, true);
  assert.equal(shown[0].options.data.url, '/painel/#inbox/7');
  await dispatch('push', { data: { json: () => { throw new Error('bad'); }, text: () => 'x' } });
  await dispatch('push', {});
  assert.equal(shown.length, 3);
  assert.equal(shown[1].title, 'Converge');
  assert.equal(shown[2].options.data.url, '/painel/');
})().catch(e => { console.error(e); process.exitCode = 1; });
""")


def test_a_tap_focuses_an_open_panel_and_switches_to_the_conversation():
    """No reload: a reply being written in another conversation stays."""
    _run(r"""
(async () => {
  let closed = false;
  await dispatch('notificationclick', { notification: { data: { url: '/painel/#inbox/7' },
                                                        close: () => { closed = true; } } });
  assert.ok(closed);
  assert.deepEqual(opened, []);
  assert.deepEqual(log, ['focus https://panel.test/painel/', 'OPEN_URL https://panel.test/painel/#inbox/7']);
})().catch(e => { console.error(e); process.exitCode = 1; });
""", windows=["https://panel.test/painel/"])


def test_a_tap_without_a_panel_window_opens_one():
    _run(r"""
(async () => {
  await dispatch('notificationclick', { notification: { data: { url: '/painel/#inbox/7' }, close() {} } });
  assert.deepEqual(opened, ['https://panel.test/painel/#inbox/7']);
})().catch(e => { console.error(e); process.exitCode = 1; });
""", windows=["https://panel.test/outra-pagina"])


def test_a_tap_never_leaves_the_panel():
    _run(r"""
(async () => {
  for (const url of ['https://evil.example/x', '//evil.example/x', 'javascript:alert(1)', '/outra']) {
    await dispatch('notificationclick', { notification: { data: { url }, close() {} } });
  }
  assert.deepEqual(opened, Array(4).fill('https://panel.test/painel/'));
})().catch(e => { console.error(e); process.exitCode = 1; });
""")


def test_the_worker_says_it_handles_push():
    _run(r"""
(async () => {
  const replies = [];
  await dispatch('message', { data: { type: 'CAPABILITIES' },
                              ports: [{ postMessage: m => replies.push(m) }] });
  assert.equal(JSON.stringify(replies), JSON.stringify([{ push: true }]));
})().catch(e => { console.error(e); process.exitCode = 1; });
""")


def test_an_administration_notice_keeps_its_target_in_an_open_app():
    _run(r"""
(async () => {
  const payload = {title: 'Bot fora do ar', url: '/painel/#admin', tag: 'availability:1'};
  await dispatch('push', {data: {json: () => payload}});
  assert.equal(shown[0].options.data.url, '/painel/#admin');
  await dispatch('notificationclick', {notification: {data: shown[0].options.data, close() {}}});
  assert.deepEqual(log, ['focus https://panel.test/painel/', 'OPEN_URL https://panel.test/painel/#admin']);
})().catch(e => { console.error(e); process.exitCode = 1; });
""", windows=['https://panel.test/painel/'])
