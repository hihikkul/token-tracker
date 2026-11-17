"""Exercise the dashboard's actual script with a small DOM, without a server or network."""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[1] + '/web/dashboard.html', 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(js);
class Node {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.style = {}; this.attrs = {}; this.hidden = false; this.className = ''; }
  append(...values) { for (const value of values) { this.children.push(value); if (value instanceof Node) value.parentNode = this; } }
  replaceChildren(...values) { this.children = []; this.append(...values); }
  set textContent(value) { this.children = [String(value)]; }
  get textContent() { return this.children.map(value => value instanceof Node ? value.textContent : String(value)).join(''); }
  set innerHTML(value) { throw new Error('HTML sink: ' + value); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  addEventListener() {}
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children.at(-1); }
  after() {}
  closest() { return this.parentNode; }
  querySelectorAll(selector) {
    const matches = node => selector.startsWith('.') ? node.className.split(' ').includes(selector.slice(1)) :
      selector === 'select[name="codex"]' ? node.tagName === 'select' && node.name === 'codex' : node.tagName === selector;
    const found = [];
    const walk = node => { for (const child of node.children) if (child instanceof Node) { if (matches(child)) found.push(child); walk(child); } };
    walk(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get elements() { return new Proxy({}, { get: (_, key) => this.querySelectorAll('input').concat(this.querySelectorAll('select')).find(node => node.name === key) }); }
}
const nodes = new Map(), get = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
const storage = new Map();
const context = {
  console, URLSearchParams, Intl, Date, Set, Map, Object, Number, String, Math,
  location: {hash:''}, history: {replaceState(a,b,hash) { context.location.hash = hash; }},
  document: {documentElement: {dataset:{}}, getElementById:get, createElement:tag => new Node(tag), createElementNS:(_,tag) => new Node(tag), querySelectorAll:() => [], querySelector:() => new Node()},
  localStorage: {getItem:key => storage.get(key), setItem:(key,value) => storage.set(key,value)},
  Option: function(text,value) { const node = new Node('option'); node.textContent = text; node.value = value; return node; },
  addEventListener() {}, scrollTo() {}, setTimeout, clearTimeout,
  d3: {timeFormat:() => () => '11/17 12:00', max:(rows,fn = x => x) => Math.max(...rows.map(fn)), sum:(rows,fn = x => x) => rows.reduce((sum,row) => sum + fn(row),0)},
};
vm.createContext(context);
vm.runInContext(js.slice(0, js.lastIndexOf("$('empty-all').textContent='사용량 기록을 읽는 중…';")), context);
const run = code => vm.runInContext(code,context);
'''


@unittest.skipUnless(shutil.which('node'), 'Node is needed for dashboard script tests')
class DashboardFixes(unittest.TestCase):
    def node(self, body):
        result = subprocess.run(['node', '-e', HARNESS + '\n' + body, str(ROOT)],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_detected_tool_hash_round_trip_and_no_codex_fallback(self):
        self.node(r'''
run(`SOURCE_SETTINGS = {sources:{},source_names:{codex:'Codex',deepseek:'DeepSeek'},source_meta:{}};
location.hash = '#t=deepseek'; readHash(); refreshToolButtons(); writeHash(); st.tool = 'all'; readHash();`);
assert.equal(run('st.tool'),'deepseek');
assert(context.location.hash.includes('t=deepseek'));
assert.equal(run("passTool({tool:'DeepSeek'})"),true);
assert.equal(run("passTool({tool:'Codex'})"),false);
run("location.hash='#t=DeepSeek'; readHash(); refreshToolButtons();");
assert.equal(run('st.tool'),'deepseek');
run("st.tool='codex'"); assert.equal(run('passTool({})'),false);
run("SOURCE_SETTINGS={}; DATA.daily=[{tool:'DeepSeek'}]; refreshToolButtons();");
assert(get('tool').textContent.includes('DeepSeek'));
''')

    def test_unknown_colour_is_stable_and_known_css_is_preserved(self):
        self.node(r'''
const before = run("sourceColor('acme')");
assert.match(before,/^var\(--provider[1-8]\)$/);
run("SOURCE_SETTINGS={source_names:{newcomer:'New',acme:'Renamed'}}");
assert.equal(run("sourceColor('acme')"),before);
assert.equal(run("sourceColor('claude')"),'var(--claude)');
assert.equal(run("sourceColor('deepseek')"),'var(--deepseek)');
assert.match(run("sourceColor('x);background:url(evil)')"),/^var\(--provider[1-8]\)$/);
assert(html.includes('--deepseek: #6250d6')); assert(html.includes('--deepseek: #9085e9'));
''')

    def test_payg_is_separate_for_each_period_and_older_backend_works(self):
        self.node(r'''
run(`SOURCE_SETTINGS={source_names:{deepseek:'DeepSeek',qwen:'Qwen'},source_meta:{deepseek:{kind:'payg'}}};
RATIO={periods:{month:{api:20,sub:10,ratio:2,tools:{claude:{label:'Claude',api:20,sub:10,ratio:2}},payg:{deepseek:{label:'DeepSeek',api:1.23},qwen:{label:'Qwen',api:.4}}},day:{tools:{},payg:{deepseek:{label:'DeepSeek',api:.2}}}}}; ratioView();`);
assert.equal(get('ratio-payg').textContent,'종량제 API(실제 지출, 배율 제외): DeepSeek $1.23 · Qwen $0.40');
assert(!get('r-rows').textContent.includes('DeepSeek')); assert.equal(get('r-rows').children.length,1);
run("st.rp='day'; ratioView()");
assert.equal(get('ratio-card').hidden,false); assert.equal(get('r-rows').children.length,0);
assert(get('ratio-payg').textContent.endsWith('DeepSeek $0.20'));
run("RATIO={periods:{day:{tools:{claude:{api:0,sub:0}}}}}; ratioView()");
assert.equal(get('ratio-payg').hidden,true);
run("RATIO={periods:{day:{}}}; ratioView()"); assert.equal(get('ratio-card').hidden,true);
''')

    def test_unpriced_note_and_dynamic_names_are_text_not_html(self):
        self.node(r'''
assert(!/\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|\.html\s*\(/.test(js));
context.bad = '<img src=x onerror=alert(1)>';
run(`SOURCE_SETTINGS={source_names:{deepseek:bad},source_meta:{deepseek:{kind:'payg',detected:true,first_seen:Date.now()/1000}}};
refreshToolButtons(); sourceBanners(); DATA.unpriced=[{model:bad,tool:bad,tokens:12300000}]; unpricedView();
RATIO={periods:{month:{tools:{},payg:{deepseek:{label:bad,api:1.23}}}}}; ratioView();`);
assert(get('tool').textContent.includes(context.bad));
assert(get('unpriced-note').textContent.includes(context.bad+' 12.3M 토큰'));
assert(get('unpriced-note').textContent.startsWith('가격 미확인 모델(비용 제외):'));
assert(get('ratio-payg').textContent.includes(context.bad));
assert.equal(get('source-banners').children.length,1);
run('sourceBanners()'); assert.equal(get('source-banners').children.length,1);
assert(get('tool').textContent.includes('새로 감지됨'));
get('source-banners').children[0].children[1].onclick(); assert.equal(get('source-banners').children.length,0);
run('ANNOUNCED_SOURCES.clear(); sourceBanners()'); assert.equal(get('source-banners').children.length,0);
context.localStorage.getItem = () => { throw new Error('blocked'); };
run("SOURCE_SETTINGS.source_meta.qwen={detected:true,first_seen:1}; sourceBanners(); sourceBanners()");
assert.equal(get('source-banners').children.length,1);
''')

    def test_balance_currency_availability_and_absent_quota(self):
        self.node(r'''
run(`SOURCE_SETTINGS={source_names:{deepseek:'DeepSeek',qwen:'Qwen'},sources:{}};
LIMITS={deepseek:{status:'ok',available:true,checked_at:123,balance:[{currency:'USD',total:12.3},{currency:'CNY',total:40}]}}; limitsView();`);
assert.equal(get('limits').children.length,1);
assert(get('limits').textContent.includes('잔액 $12.30 · USD'));
assert(get('limits').textContent.includes('잔액 ¥40.00 · CNY'));
assert(get('limits').textContent.includes('사용 가능'));
assert.equal(get('limits').querySelectorAll('.track').length,0);
run("LIMITS.deepseek={status:'auth',message:'bad key'}; limitsView()");
assert(get('limits').textContent.includes('인증 확인 필요'));
run('LIMITS={}; limitsView()'); assert.equal(get('limits').hidden,true);
''')

    def test_settings_have_subscription_plans_only_for_builtins(self):
        self.node(r'''
// Install the fixed widget controls from the surrounding HTML form.
for (const name of ['font','number_font','opacity','topmost','taskbar','auto_update']) {
  const input = new Node('input'); input.name = name; get('settingsForm').append(input);
}
get('settingsForm').append(get('subscriptionPlans'),get('codexAccountPlans'));
run(`SOURCE_SETTINGS={source_names:{claude:'Claude Code',deepseek:'DeepSeek',acme:'Acme'},
source_meta:{deepseek:{kind:'payg'},acme:{kind:'subscription'}},
plan_options:{claude:{pro:['Pro',20]},deepseek:{fake:['Fake',1]},acme:{fake:['Fake',1]}}}; settingsView()`);
assert.deepEqual(get('subscriptionPlans').querySelectorAll('select').map(node => node.name),['claude']);
run('SOURCE_SETTINGS={}; settingsView()'); assert.equal(get('subscriptionPlans').children.length,0);
''')

    def test_mix_uses_peak_rates_and_legacy_price_fallback(self):
        self.node(r'''
// Execute the actual mix calculations and DOM rows, stopping before its unrelated chart.
const mixStart = js.indexOf('function mix() {'), chartStart = js.indexOf('  // daily hit rate', mixStart);
run(js.slice(mixStart, chartStart) + '\n}');
run(`st.range='all'; st.unit='cost'; FIRST='2026-11-17';
DATA={prices:{deepseek:{input:1,output:2,cache_read:.1,cache_write:1.2,
peak:{input:3,output:6,cache_read:.3,cache_write:3.6}},legacy:{input:2,output:4,cache_read:.2,cache_write:2.4}},
daily:[{model:'deepseek',date:FIRST,peak:0,input:1e6,output:1e6,cache_read:1e6,cache_write:1e6},
{model:'deepseek',date:FIRST,peak:1,input:1e6,output:1e6,cache_read:1e6,cache_write:1e6},
{model:'legacy',date:FIRST,peak:1,input:1e6,output:1e6,cache_read:1e6,cache_write:1e6}]}; mix();`);
assert.equal(get('x-save').textContent,'$5.40');
assert.deepEqual(get('x-rows').children.slice(1).map(row=>row.lastChild.textContent),['$6.00','$0.60','$7.20','$12.00']);
// Missing row flag, missing nested rates, and missing model prices retain old behavior.
run(`DATA.daily=[{model:'deepseek',date:FIRST,input:1e6,output:1e6,cache_read:1e6,cache_write:1e6},
{model:'unpriced',date:FIRST,peak:1,input:1e6,cache_read:1e6}]; mix();`);
assert.equal(get('x-save').textContent,'$0.90');
assert.deepEqual(get('x-rows').children.slice(1).map(row=>row.lastChild.textContent),['$1.00','$0.10','$1.20','$2.00']);
''')

    def test_ranking_badge_reads_are_sequential(self):
        self.node(r'''
(async () => {
let active=0,peak=0,reads=0;
const state={server:true,groups:[{code:'AAAA-BBBB',joined:true},{code:'CCCC-DDDD',joined:true}],global:{joined:true}};
context.fetch=async url => {
  if (url==='/api/ranking/state') return {ok:true,json:async()=>state};
  active++; peak=Math.max(peak,active); reads++;
  await new Promise(resolve=>setTimeout(resolve,2));
  return {json:async()=>{ active--; return {members:[],state:{joined:true}}; }};
};
run("st.view='rank'; st.rankTab='AAAA-BBBB'"); await run('loadRanking()');
assert.equal(reads,3); assert.equal(peak,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


if __name__ == '__main__':
    unittest.main()
