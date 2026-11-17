"""Exercise the real Worker with Node 24's in-memory SQLite D1 adapter, offline."""
import shutil
import subprocess
import unittest
from pathlib import Path


HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
(async () => {
const root = process.argv[1], sql = new DatabaseSync(':memory:');
sql.exec(fs.readFileSync(path.join(root, 'server/schema.sql'), 'utf8'));
for (const file of fs.readdirSync(path.join(root, 'server/migrations')).sort())
  sql.exec(fs.readFileSync(path.join(root, 'server/migrations', file), 'utf8'));
const calls = [];
const wrap = (statement, text) => ({ values: [], bind(...args) { this.values = args; return this; },
  async first(column) { calls.push({method:'first', text}); const row = statement.get(...this.values); return column ? row?.[column] ?? null : row || null; },
  async all() { return {results: statement.all(...this.values)}; },
  async run() {
    if (statement.columns().length) return this.all();
    const result = statement.run(...this.values); return {results: [], meta: {changes: Number(result.changes)}};
  } });
const env = {DB: {prepare: text => wrap(sql.prepare(text), text), async batch(stmts) {
  calls.push({method:'batch', count:stmts.length});
  sql.exec('BEGIN');
  try { const results = []; for (const stmt of stmts) results.push(await stmt.run()); sql.exec('COMMIT'); return results; }
  catch (error) { sql.exec('ROLLBACK'); throw error; }
}}};
const source = fs.readFileSync(path.join(root, 'server/worker.js'), 'utf8');
const worker = (await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))).default;
let now = Date.UTC(2026, 10, 15, 0, 10);
Date.now = () => now;
const HOUR = 3600000, DAY = 24 * HOUR;
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
const group = code => sql.prepare('INSERT INTO groups (code,name,created) VALUES (?,?,?)').run(code, 'Friends', now);
const seed = (id, code='GLOBAL', api=10, sub=1, updated=now, version=2, joined=now) => {
  sql.prepare('INSERT INTO members (id,group_code,token_hash,nick,joined,updated) VALUES (?,?,?,?,?,?)')
    .run(id, code, hash(id), id, joined, updated);
  sql.prepare('INSERT INTO scores (member_id,period,window,api,sub,updated,calculation_version,usage,tokens) VALUES (?,?,?,?,?,?,?,?,?)')
    .run(id, 'month', '2026-11', api, sub, updated, version, api, 123);
};
const request = (route, method='GET', body, token, ip='192.0.2.1', headers={}) => worker.fetch(
  new Request('https://example.invalid' + route, {method, headers: {
    'CF-Connecting-IP': ip, ...(token ? {authorization: 'Bearer ' + token} : {}), ...headers},
    ...(body === undefined ? {} : {body: typeof body === 'string' ? body : JSON.stringify(body)})}), env);
const ranking = token => request('/v1/global/ranking?period=month&window=2026-11', 'GET', undefined, token);
const score = (api=10, sub=1, period='month') => ({nick:'Updated', plans:'Pro', calculation_version:2,
  periods:{[period]:{window:period === 'day' ? '2026-11-15' : period === 'week' ? '2026-W46' : '2026-11', api, sub, usage:10, tokens:12.4}}});
try {
'''
FOOTER = r'''
} finally { sql.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
'''


@unittest.skipUnless(shutil.which('node'), 'Node 24 is needed for Worker integration tests')
class WorkerFixes(unittest.TestCase):
    def run_worker(self, script):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', '--disable-warning=ExperimentalWarning', '-e', HARNESS + script + FOOTER, str(root)],
            capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tiny_subscription_and_nonfinite_scores_are_rejected_atomically(self):
        self.run_worker(r'''
seed('me');
for (const period of ['day','week','month']) {
  for (const sub of [1e-310, 0.009]) {
    const body = score(10, sub, period);
    body.periods.day = {window:'2026-11-15', api:10, sub:1};
    body.periods[period].sub = sub;
    assert.equal((await request('/v1/global/score','PUT',body,'me')).status,400);
  }
}
for (const body of [score(-1,1), score(1e6,1), score(1,1e6), score(1,null), score(1,1).periods])
  assert.equal((await request('/v1/global/score','PUT',body,'me')).status,400);
assert.equal((await request('/v1/global/score','PUT','{"periods":{"month":{"api":1e309,"sub":1,"window":"2026-11"}}}','me')).status,400);
assert.equal(sql.prepare('SELECT nick FROM members WHERE id=?').get('me').nick,'me');
assert.equal(sql.prepare('SELECT COUNT(*) AS n FROM scores').get().n,1);
assert.equal((await request('/v1/global/score','PUT',score(10,.01),'me')).status,200);
let data = await (await ranking('me')).json();
assert.equal(data.ratio,1000); assert.equal(data.place,1);
assert.equal(sql.prepare('SELECT tokens FROM scores WHERE member_id=?').get('me').tokens,12);
// Unknown subscriptions and zero usage still sync, but are not ranked.
assert.equal((await request('/v1/global/score','PUT',score(10,0),'me')).status,200);
data = await (await ranking('me')).json(); assert.equal(data.no_sub,true); assert.equal(data.place,null);
assert.equal((await request('/v1/global/score','PUT',score(0,1e-310),'me')).status,200);
const legacy = score(); delete legacy.calculation_version;
assert.equal((await request('/v1/global/score','PUT',legacy,'me')).status,200);
assert.equal(sql.prepare('SELECT calculation_version FROM scores WHERE member_id=?').get('me').calculation_version,1);
assert.equal((await (await ranking('me')).json()).place,null);
''')

    def test_stored_invalid_ratios_are_excluded_from_global_and_group_ranking(self):
        self.run_worker(r'''
seed('valid'); seed('tiny', 'GLOBAL', 10, 1e-310); seed('small','GLOBAL',10,.009);
seed('infinite','GLOBAL',Infinity,1); seed('infinite-sub','GLOBAL',1,Infinity);
seed('negative','GLOBAL',-1,1); seed('free','GLOBAL',10,0);
let data = await (await ranking('tiny')).json();
assert.deepEqual(data.members.map(m=>m.nick),['valid']); assert.equal(data.total,1);
assert.equal(data.place,null); assert.equal(data.ratio,null);
assert.equal((await (await ranking('valid')).json()).place,1);
group('ABCD-EFGH'); seed('friend','ABCD-EFGH'); seed('tiny-friend','ABCD-EFGH',10,1e-310);
data = await (await request('/v1/groups/ABCD-EFGH/ranking?period=month&window=2026-11','GET',undefined,'friend')).json();
assert.equal(data.members[0].nick,'friend');
const invalid = data.members.find(m=>m.nick==='tiny-friend');
assert.equal(invalid.current,false); assert.equal(invalid.ratio,null); assert.equal(invalid.api,0);
''')

    def test_ranking_keeps_combined_auth_and_one_read_batch(self):
        self.run_worker(r'''
seed('me'); seed('tiny','GLOBAL',100,1e-310); seed('stale','GLOBAL',100,1,now-31*DAY);
let data = await (await ranking('me')).json();
assert.equal(data.total,1); assert.equal(data.place,1);
assert.equal(calls.length,2);
assert.equal(calls[0].method,'first'); assert(calls[0].text.includes('LEFT JOIN members'));
assert.deepEqual(calls[1],{method:'batch',count:4});
calls.length = 0;
assert.equal((await ranking('wrong-token')).status,401);
assert.equal(calls.length,1);
assert.equal((await request('/v1/groups/ABCD-EFGH/ranking','GET',undefined,'me')).status,404);
group('ABCD-EFGH'); seed('friend','ABCD-EFGH'); calls.length = 0;
assert.equal((await request('/v1/groups/ABCD-EFGH/ranking?period=month&window=2026-11','GET',undefined,'friend')).status,200);
assert.equal(calls.length,2); assert.deepEqual(calls[1],{method:'batch',count:1});
// Legacy timestamps are unknown, not stale; a zero score timestamp falls back to joined.
seed('unknown','GLOBAL',20,1,0,2,0);
seed('boundary','GLOBAL',30,1,now-30*DAY);
seed('old-join','GLOBAL',100,1,0,2,now-31*DAY);
data = await (await ranking('unknown')).json();
assert.deepEqual(data.members.map(m=>m.nick),['boundary','unknown','me']);
assert.equal(data.total,3); assert.equal(data.place,2);
data = await (await ranking('old-join')).json(); assert.equal(data.place,null); assert.equal(data.ratio,null);
''')

    def test_hourly_enrollment_quotas_are_hashed_and_client_scoped(self):
        self.run_worker(r'''
group('ABCD-EFGH');
for (const [route,limit,action] of [['/v1/groups',5,'create'],['/v1/groups/ABCD-EFGH/join',30,'join'],['/v1/global/join',3,'global_join']]) {
  for (let i=0;i<limit;i++) assert.equal((await request(route,'POST',{nick:'Member'})).status,201);
  const response = await request(route,'POST',{nick:'Member'});
  assert.equal(response.status,429); assert.equal(response.headers.get('retry-after'),'3600');
  assert.equal((await request(route,'POST',{nick:'Other'},undefined,'192.0.2.2')).status,201);
  const row = sql.prepare('SELECT * FROM enrollment_limits WHERE client_hash=? AND action=?').get(hash('ranking-client:192.0.2.1'),action);
  assert.equal(row.hour_count,limit); assert.equal(row.day_count,limit);
}
const rows = sql.prepare('SELECT * FROM enrollment_limits').all();
assert(rows.every(row=>/^[a-f0-9]{64}$/.test(row.client_hash)));
assert(!JSON.stringify(rows).includes('192.0.2.'));
now += HOUR;
assert.equal((await request('/v1/global/join','POST',{nick:'New hour'})).status,201);
// Missing CF headers cannot bypass limits.
for (let i=0;i<3;i++) assert.equal((await worker.fetch(new Request('https://example.invalid/v1/global/join',{method:'POST',body:'{"nick":"No IP"}'}),env)).status,201);
assert.equal((await worker.fetch(new Request('https://example.invalid/v1/global/join',{method:'POST',body:'{"nick":"No IP"}'}),env)).status,429);
''')

    def test_daily_quotas_survive_hour_rollover_and_expire_next_day(self):
        self.run_worker(r'''
for (const code of ['AAAA-BBBB','CCCC-DDDD','EEEE-FFFF','GGGG-HHHH','JJJJ-KKKK']) group(code);
seed('keep-group','JJJJ-KKKK');
const routes = ['/v1/groups','/v1/groups/AAAA-BBBB/join','/v1/global/join'];
const hourly = [5,30,3], daily = [20,100,10], used = [0,0,0];
for (let hour=0;hour<4;hour++) {
  for (let action=0;action<3;action++) {
    const route = action===1 ? '/v1/groups/' + ['AAAA-BBBB','CCCC-DDDD','EEEE-FFFF','GGGG-HHHH'][hour] + '/join' : routes[action];
    const count = Math.min(hourly[action],daily[action]-used[action]);
    for(let i=0;i<count;i++) assert.equal((await request(route,'POST',{nick:'Member'})).status,201);
    used[action] += count;
  }
  now += HOUR;
}
for (const route of ['/v1/groups','/v1/groups/JJJJ-KKKK/join','/v1/global/join'])
  assert.equal((await request(route,'POST',{nick:'Over daily quota'})).status,429);
now += DAY;
for (const route of ['/v1/groups','/v1/groups/JJJJ-KKKK/join','/v1/global/join'])
  assert.equal((await request(route,'POST',{nick:'Next day'})).status,201);
''')

    def test_parallel_quota_and_group_capacity_writes_are_atomic(self):
        self.run_worker(r'''
let responses = await Promise.all(Array.from({length:30},()=>request('/v1/global/join','POST',{nick:'Concurrent'})));
assert.equal(responses.filter(r=>r.status===201).length,3);
assert.equal(responses.filter(r=>r.status===429).length,27);
group('ABCD-EFGH'); seed('creator','ABCD-EFGH');
responses = await Promise.all(Array.from({length:60},(_,i)=>request('/v1/groups/ABCD-EFGH/join','POST',{nick:'Concurrent'},undefined,'198.51.100.'+(i+1))));
assert.equal(responses.filter(r=>r.status===201).length,49);
assert.equal(responses.filter(r=>r.status===409).length,11);
assert.equal(sql.prepare('SELECT COUNT(*) AS n FROM members WHERE group_code=?').get('ABCD-EFGH').n,50);
// The global group intentionally has no 50-member cap.
responses = await Promise.all(Array.from({length:60},(_,i)=>request('/v1/global/join','POST',{nick:'Global'},undefined,'203.0.113.'+(i+1))));
assert(responses.every(r=>r.status===201));
''')

    def test_abandoned_data_is_hidden_on_reads_and_lazily_removed_on_writes(self):
        self.run_worker(r'''
seed('active'); seed('stale','GLOBAL',100,1,now-31*DAY);
seed('friend','FRIENDS',100,1,now-31*DAY);
// Nick-only updates do not keep a stale score alive.
sql.prepare('UPDATE members SET updated=? WHERE id=?').run(now,'stale');
sql.prepare('INSERT INTO members (id,group_code,token_hash,nick,joined) VALUES (?,?,?,?,?)')
  .run('abandoned','GLOBAL',hash('abandoned'),'abandoned',now-31*DAY);
group('EMPTY-OLD'); group('EMPTY-NEW');
sql.prepare('UPDATE groups SET created=? WHERE code=?').run(now-2*DAY,'EMPTY-OLD');
sql.prepare('INSERT INTO enrollment_limits VALUES (?,?,?,?,?,?,?)').run(hash('expired'),'join',0,0,1,1,now-1);
let data = await (await ranking('stale')).json();
assert.equal(data.total,1); assert.equal(data.place,null); assert.equal(data.ratio,null);
assert.deepEqual(data.members.map(m=>m.nick),['active']);
assert(sql.prepare('SELECT 1 FROM members WHERE id=?').get('stale'));
assert.equal((await request('/v1/groups','POST',{nick:'Cleanup'})).status,201);
assert(!sql.prepare('SELECT 1 FROM members WHERE id=?').get('stale'));
assert(!sql.prepare('SELECT 1 FROM members WHERE id=?').get('abandoned'));
assert(!sql.prepare('SELECT 1 FROM scores WHERE member_id=?').get('stale'));
assert(!sql.prepare('SELECT 1 FROM groups WHERE code=?').get('EMPTY-OLD'));
assert(sql.prepare('SELECT 1 FROM groups WHERE code=?').get('EMPTY-NEW'));
assert(sql.prepare('SELECT 1 FROM groups WHERE code=?').get('GLOBAL'));
assert(sql.prepare('SELECT 1 FROM members WHERE id=?').get('friend'));
assert(!sql.prepare('SELECT 1 FROM enrollment_limits WHERE client_hash=?').get(hash('expired')));
// Score and delete routes also run cleanup, not just enrollments.
group('EMPTY-SCORE'); sql.prepare('UPDATE groups SET created=? WHERE code=?').run(now-2*DAY,'EMPTY-SCORE');
assert.equal((await request('/v1/global/score','PUT',score(),'active')).status,200);
assert(!sql.prepare('SELECT 1 FROM groups WHERE code=?').get('EMPTY-SCORE'));
group('EMPTY-DELETE'); sql.prepare('UPDATE groups SET created=? WHERE code=?').run(now-2*DAY,'EMPTY-DELETE');
assert.equal((await request('/v1/global/me','DELETE',undefined,'active')).status,200);
assert(!sql.prepare('SELECT 1 FROM groups WHERE code=?').get('EMPTY-DELETE'));
''')

    def test_content_length_is_rejected_before_stream_access(self):
        self.run_worker(r'''
for (const route of ['/v1/groups','/v1/global/join']) {
  const response = await worker.fetch({url:'https://example.invalid'+route,method:'POST',
    headers:new Headers({'Content-Length':'8193'}),get body(){throw Error('body must not be read');}},env);
  assert.equal(response.status,413);
}
assert.equal(sql.prepare('SELECT COUNT(*) AS n FROM enrollment_limits').get().n,0);
''')

    def test_stream_limit_counts_bytes_and_stops_before_full_body(self):
        self.run_worker(r'''
let read=0, cancelled=false;
const stream = new ReadableStream({pull(controller){read++;controller.enqueue(new Uint8Array(1024).fill(32));},
  cancel(){cancelled=true;}},{highWaterMark:0});
let response = await worker.fetch(new Request('https://example.invalid/v1/groups',
  {method:'POST',body:stream,duplex:'half'}),env);
assert.equal(response.status,413); assert.equal(read,9); assert.equal(cancelled,true);
const multibyte = JSON.stringify({nick:'한'.repeat(3000)});
assert(multibyte.length<8192); assert(Buffer.byteLength(multibyte)>8192);
response = await request('/v1/groups','POST',multibyte,undefined,undefined,{'Content-Length':'1'});
assert.equal(response.status,413);
const exact = JSON.stringify({nick:'Exact'});
assert.equal((await request('/v1/groups','POST',exact+' '.repeat(8192-Buffer.byteLength(exact)))).status,201);
// A decoder must preserve UTF-8 characters split across byte chunks.
const bytes = new TextEncoder().encode(JSON.stringify({nick:'한글'})); let offset=0;
const split = new ReadableStream({pull(controller){if(offset===bytes.length)controller.close();else controller.enqueue(bytes.slice(offset,++offset));}});
response = await worker.fetch(new Request('https://example.invalid/v1/groups', {method:'POST',body:split,duplex:'half'}),env);
assert.equal(response.status,201);
const created = await response.json();
assert.equal(sql.prepare('SELECT nick FROM members WHERE id=?').get(created.member_id).nick,'한글');
assert.equal((await request('/v1/groups','POST','{broken')).status,400);
seed('scorer');
assert.equal((await request('/v1/global/score','PUT',multibyte,'scorer')).status,413);
''')

    def test_pages_uses_shared_migrations_before_both_deployments(self):
        root = Path(__file__).resolve().parents[1]
        worker_config = (root / 'server/wrangler.toml').read_text(encoding='utf-8')
        pages_dir = root / 'server/pages'
        pages_config = (pages_dir / 'wrangler.toml').read_text(encoding='utf-8')
        self.assertIn('migrations_dir = "../migrations"', pages_config)
        self.assertEqual((pages_dir / '../migrations').resolve(), root / 'server/migrations')
        self.assertIn('mode = "smart"', worker_config)
        deploy = (root / 'server/deploy.py').read_text(encoding='utf-8')
        migrate = deploy.index("wr('d1', 'migrations', 'apply', NAME, '--remote')")
        self.assertLess(migrate, deploy.index('wr("deploy", capture=True)'))
        self.assertLess(migrate, deploy.index('wr("pages", "deploy"'))
        self.assertEqual((pages_dir / 'dist/_worker.js').read_text(encoding='utf-8').strip(),
                         "export { default } from '../../worker.js';")

    def test_migration_upgrades_existing_schema_idempotently(self):
        self.run_worker(r'''
const old = new DatabaseSync(':memory:');
try {
  old.exec('CREATE TABLE groups(code TEXT PRIMARY KEY,name TEXT NOT NULL,created INTEGER NOT NULL)');
  const migration = fs.readFileSync(path.join(root,'server/migrations/0004_enrollment_limits.sql'),'utf8');
  old.exec(migration); old.exec(migration);
  const columns = old.prepare('PRAGMA table_info(enrollment_limits)').all().map(row=>row.name);
  assert.deepEqual(columns,['client_hash','action','hour','day','hour_count','day_count','expires']);
  assert(!columns.some(name=>name==='ip'||name==='client_ip'));
} finally { old.close(); }
''')


if __name__ == '__main__':
    unittest.main()
