"""The attach and record buttons of the Inbox and the Administração room are
icons — a paperclip and a microphone — that still say what they do to a
screen reader and on hover."""
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
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {};
    this.listeners = {}; this.className = ''; this.hidden = false; this._text = ''; this.innerHTML = '';
    const self = this;
    this.classList = { add(c) { self.className += ' ' + c; }, remove() {}, toggle(c, on) {
      self.className = self.className.split(' ').filter(x => x && x !== c).join(' ') + (on ? ' ' + c : ''); },
      contains(c) { return self.className.split(' ').includes(c); } };
    this.style = {}; }
  append(...n) { this.children.push(...n); }
  prepend(...n) { this.children.unshift(...n); }
  replaceChildren(...n) { this.children = n; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { this.listeners[t] = f; }
  closest() { return null; }
  set textContent(v) { this._text = v == null ? '' : String(v); this.innerHTML = ''; }
  get textContent() { return this._text; }
}
const document = { createElement: t => new El(t), addEventListener() {} };
const window = { addEventListener() {} };
vm.runInNewContext(data.source, { window, document, navigator: {}, URL, console,
  setInterval, clearInterval, crypto: { randomUUID: () => 'n' } });
const host = new El('div');
window.PanelAttachments.create(host, { caption: () => '', url: () => '/x', sent() {}, refresh() {} });
const controls = host.children.find(c => c.className.includes('attachment-controls'));
const buttons = controls.children.filter(c => c.tagName === 'BUTTON');
const [pick, mic] = buttons;
for (const [b, label] of [[pick, 'Anexar foto ou arquivo'], [mic, 'Gravar áudio']]) {
  assert.equal(b.textContent, '', label + ': no visible text, only the icon');
  assert.ok(b.innerHTML.includes('<svg'), label + ': an icon');
  assert.ok(b.innerHTML.includes('aria-hidden="true"'), label + ': the icon is not read aloud');
  assert.equal(b.getAttribute('aria-label'), label);
  assert.equal(b.title, label, label + ': the name shows on hover');
}
assert.notEqual(pick.innerHTML, mic.innerHTML, 'a clip and a microphone, not the same icon');
"""


def test_attach_and_record_are_named_icons():
    source = (panel._STATIC / "attachments.js").read_text()
    result = subprocess.run([NODE or "node", "-e", HARNESS], text=True, capture_output=True,
                            input=json.dumps({"source": source}), timeout=15)
    assert result.returncode == 0, result.stderr
