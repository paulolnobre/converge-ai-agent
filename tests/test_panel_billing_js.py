"""The value field of the charge form, in JavaScript: what a person types as
reais becomes whole cents, and anything ambiguous is refused, not guessed."""
import json
import shutil
import subprocess

import pytest

from core.web import panel

NODE = shutil.which("node")


def _assets_listed(name: str) -> bool:
    return name in panel._ASSETS and f"/painel/assets/{name}" in (panel._STATIC / "sw.js").read_text()


def test_billing_script_is_served_and_cached():
    assert _assets_listed("billing.js")
    assert '<script src="/painel/assets/billing.js" defer></script>' in (
        panel._STATIC / "index.html").read_text()


CASES = [
    ["1.234,56", 123456], ["1234,56", 123456], ["1234.56", 123456], ["12,5", 1250],
    ["19", 1900], ["R$ 19,90", 1990], [" 5,00 ", 500], ["100.000,00", 10000000],
    ["", None], ["0", None], ["0,00", None], ["abc", None], ["1,234", None],
    ["12,345", None], ["100.000,01", None], ["-5", None], ["1.23.4", None],
]


@pytest.mark.skipif(not NODE, reason="Node.js not installed")
def test_parse_reais():
    script = r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const data = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const window = {};
vm.runInNewContext(data.source, { window, document: undefined });
for (const [text, cents] of data.cases) {
  assert.equal(window.PanelBilling.parseReais(text), cents, JSON.stringify(text));
}
assert.equal(window.PanelBilling.formatBrl(123456), 'R$ 1.234,56');
assert.equal(window.PanelBilling.formatBrl(5), 'R$ 0,05');
"""
    source = (panel._STATIC / "billing.js").read_text()
    result = subprocess.run([NODE or "node", "-e", script], text=True, capture_output=True,
                            input=json.dumps({"source": source, "cases": CASES}), timeout=15)
    assert result.returncode == 0, result.stderr
