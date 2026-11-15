"""Execute the worker's privacy boundary and the offline retry in JavaScript.

No browser package is needed in CI. The full browser flow is checked separately.
"""
import json
import shutil
import subprocess

import pytest

from core.web import panel

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node.js not installed")


def _run(script: str, source: str, **extra):
    result = subprocess.run(
        [NODE or "node", "-e", script],
        input=json.dumps({"source": source, **extra}), text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_worker_never_intercepts_api_media_or_caches_authenticated_html():
    _run(r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const data = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const handlers={}, writes=[];
const context = {
  self: {location:{origin:'https://panel.test'},addEventListener:(n,f)=>handlers[n]=f},
  caches: {open:async()=>({put:async(req)=>writes.push(req.url)})}, URL, Response,
  fetch: async()=>new Response('private or public response')
};
vm.runInNewContext(data.source.replace('__APP_VERSION__','"test"'), context);
async function request(path,method='GET',mode='cors') {
  let answer;
  handlers.fetch({request:{url:'https://panel.test'+path,method,mode},respondWith:p=>answer=p});
  return answer ? await answer : undefined;
}
(async()=>{
  for (const path of ['/painel/api/me','/painel/api/inbox/1/media/2','/painel/api/devices']) {
    assert.equal(await request(path),undefined);
    assert.equal(await request(path,'GET','navigate'),undefined);
  }
  assert.equal(await request('/painel/api/auth/logout','POST'),undefined);
  assert.equal(await request('/painel/assets/pwa.js?v=secret'),undefined);
  assert.equal(await request('/painel/assets/not-on-the-list.js'),undefined);
  assert.ok(await request('/painel/','GET','navigate'));
  assert.deepEqual(writes,[],'Authenticated HTML must never enter Cache Storage');
  assert.ok(await request('/painel/assets/pwa.js'));
  assert.deepEqual(writes,['https://panel.test/painel/assets/pwa.js']);
})().catch(e=>{console.error(e);process.exitCode=1;});
""", (panel._STATIC / "sw.js").read_text())


@pytest.mark.parametrize("path,expected_reload", [("/painel/", True), ("/painel/offline", False)])
def test_offline_retry_loads_the_page_instead_of_navigating_to_the_same_fragment(path, expected_reload):
    _run(r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const data = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let click, reloaded=0, replaced=null;
const context={
  window:{matchMedia:()=>({matches:false}),addEventListener:()=>{}},
  navigator:{userAgent:'',onLine:true},
  document:{getElementById:id=>id==='retry-app'?{addEventListener:(n,f)=>click=f}:null,
            addEventListener:()=>{}},
  location:{pathname:data.path,reload:()=>reloaded++,replace:url=>replaced=url}
};
vm.runInNewContext(data.source,context);
assert.equal(typeof click,'function');
click();
assert.equal(reloaded,data.expected_reload?1:0);
assert.equal(replaced,data.expected_reload?null:'/painel/#inbox');
""", (panel._STATIC / "pwa.js").read_text(), path=path, expected_reload=expected_reload)
