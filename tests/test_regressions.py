import unittest
import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock
from datetime import datetime
from unittest.mock import patch

from tracker import ranking, widget, export, google_limits, sources, limits
from tracker.db import DB


class Regressions(unittest.TestCase):
    def test_local_ranking_config_uses_public_service(self):
        cfg = {'ranking': {'server': 'http://localhost:8787'}}
        self.assertEqual(ranking.server_url(cfg), ranking.DEFAULT_SERVER)
        cfg['ranking']['allow_local_server'] = True
        self.assertEqual(ranking.server_url(cfg), 'http://localhost:8787')

    def test_tooltip_uses_above_near_bottom(self):
        x, y, width, height = widget.tooltip_geometry((800, 940, 300, 36), (350, 500), (0, 0, 1920, 1040))
        self.assertEqual(y + height, 932)

    def test_tooltip_uses_below_near_top(self):
        x, y, width, height = widget.tooltip_geometry((800, 10, 300, 36), (350, 500), (0, 0, 1920, 1040))
        self.assertEqual(y, 54)

    def test_oversized_tooltip_stays_in_work_area(self):
        x, y, width, height = widget.tooltip_geometry((800, 940, 300, 36), (350, 1500), (0, 0, 1920, 1040))
        self.assertEqual(y, 0)
        self.assertEqual(height, 932)

    def test_disconnected_apps_are_hidden(self):
        self.assertFalse(sources.connected({'status': 'no_credentials', 'windows': [{'used_percent': 0}]}))
        self.assertFalse(sources.connected({'status': 'unsupported'}))
        self.assertTrue(sources.connected({'status': 'offline', 'windows': [{'used_percent': 10}]}))

    def test_taskbar_ignores_disconnected_cached_quotas(self):
        bar = widget.Widget.__new__(widget.Widget)
        bar.show = ['gemini']
        bar.live_icon = False
        from unittest.mock import Mock
        bar.root = Mock()
        bar._taskbar_status({'gemini': {'status': 'no_credentials',
                                      'windows': [{'label': 'Model quota', 'used_percent': 100}]}})
        bar.root.title.assert_called_once_with('Token Tracker')

    def test_explicit_calibri_is_preserved(self):
        with patch.object(widget.tkfont, 'families', return_value=('Calibri', 'Segoe UI Variable Text')):
            self.assertEqual(widget.pick_fonts(None, 'Calibri', 'Calibri'), ('Calibri', 'Calibri'))

    def test_monthly_subscription_is_full_price(self):
        db = DB(':memory:')
        with patch.object(ranking, 'windows', return_value={
            'month': ('2026-10', datetime(2026, 10, 1).date(), 11)
        }):
            result = ranking.report(db, {'claude': {'plan': 'Pro'}},
                                    {'subscription_usd_month': {}})
        self.assertEqual(result['periods']['month']['sub'], 20)

    def test_weekly_exhaustion_overrides_five_hour(self):
        limit = {'windows': [
            {'label': '5시간', 'used_percent': 14},
            {'label': '주간', 'used_percent': 100},
        ]}
        self.assertEqual(widget.Widget.__new__(widget.Widget)._five_hour(limit)['label'], '주간')

    def test_export_does_not_fetch_limits_when_snapshot_given(self):
        db = DB(':memory:')
        with patch.object(export, 'all_limits', side_effect=AssertionError('network fetch')):
            self.assertEqual(export.build(db, limits={})['daily_detail'], [])

    def test_google_quota_handles_zero_and_missing_amount(self):
        windows = google_limits.quota_windows({'buckets': [
            {'modelId': 'gemini-pro', 'remainingFraction': 0},
            {'modelId': 'gemini-flash', 'remainingFraction': 1},
        ]})
        self.assertEqual([w['used_percent'] for w in windows], [100, 0])

    def test_antigravity_quota_keeps_model_name(self):
        windows = google_limits.quota_windows({'userStatus': {'models': [
            {'label': 'Gemini Pro', 'quotaInfo': {'remainingFraction': .4, 'resetTime': '2026-10-12T00:00:00Z'}}
        ]}})
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]['label'], 'Gemini Pro')
        self.assertEqual(windows[0]['used_percent'], 60)

    def test_disabled_providers_are_not_called(self):
        with patch.object(sources, 'enabled', return_value=['chatgpt']), \
             patch.object(limits, 'fetch_claude', side_effect=AssertionError('disabled')):
            result = limits.all_limits(DB(':memory:'))
        self.assertEqual(list(result), ['chatgpt'])
        self.assertEqual(result['chatgpt']['status'], 'unsupported')

    def test_future_usage_is_excluded(self):
        db = DB(':memory:')
        db.upsert_records([{'uid':'future','source':'claude','ts':9999999999,
                            'model':'claude-sonnet','input':1000000}])
        self.assertEqual(ranking._spend(db, 0, 2000000000)['claude'], 0)

    def test_google_apps_share_one_subscription(self):
        cfg = {'sources': {key: key in ('gemini', 'antigravity') for key in sources.NAMES},
               'subscription_usd_month': {'gemini': 20}}
        result = ranking.report(DB(':memory:'), {}, cfg)
        self.assertEqual(result['periods']['month']['sub'], 20)

    def test_disabled_usage_is_excluded_with_explicit_config(self):
        db = DB(':memory:')
        db.upsert_records([{'uid': 'test', 'source': 'claude', 'ts': 1,
                            'model': 'claude-sonnet', 'input': 1000000}])
        cfg = {'sources': {key: False for key in sources.NAMES}}
        self.assertEqual(ranking._spend(db, 0, cfg=cfg)['claude'], 0)


class RankingGroups(unittest.TestCase):
    def setUp(self):
        self.cfg = {'ranking': {'server': 'https://example.invalid'}, 'ranking_groups': [
            dict(code='AAAA-BBBB', token='a', nick='하나', group_name='첫 그룹'),
            dict(code='CCCC-DDDD', token='b', nick='둘', group_name='둘째 그룹')],
            'ranking_global': {'token': 'global-token', 'nick': '전체'}}
        self.load = patch.object(ranking.paths, 'load_config', side_effect=lambda: copy.deepcopy(self.cfg))
        self.save = patch.object(ranking.paths, 'save_config', side_effect=self.save_cfg)
        self.load.start(); self.save.start()
        self.addCleanup(self.load.stop); self.addCleanup(self.save.stop)
        self.old_sync = ranking._last_sync['t']; ranking._last_sync['t'] = 0
        self.addCleanup(lambda: ranking._last_sync.update(t=self.old_sync))

    def save_cfg(self, cfg):
        self.cfg = copy.deepcopy(cfg)

    def test_migrate_legacy_preserves_server_and_credentials_once(self):
        cfg = {'ranking': dict(server='https://override.invalid', code='EEEE-FFFF', token='secret',
                               member_id='id', nick='이전', group_name='기존'), 'ranking_groups': []}
        with patch.object(ranking.paths, 'save_config'):
            first = ranking.state(cfg); second = ranking.state(cfg)
        self.assertEqual(len(first['groups']), 1)
        self.assertEqual(first, second)
        self.assertEqual(cfg['ranking'], {'server': 'https://override.invalid'})
        self.assertEqual(cfg['ranking_groups'][0]['token'], 'secret')
        self.assertNotIn('token', first['groups'][0])
        self.assertNotIn('member_id', first['groups'][0])

    def test_config_load_persists_legacy_migration(self):
        self.load.stop(); self.save.stop()
        cfg = {'ranking': dict(server='https://override.invalid', code='EEEE-FFFF', token='secret')}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.json'
            path.write_text(json.dumps(cfg), encoding='utf-8')
            with patch.object(ranking.paths, 'config_path', return_value=path):
                loaded = ranking.paths.load_config()
                self.assertEqual(loaded['ranking_groups'][0]['token'], 'secret')
                self.assertEqual(ranking.paths.load_config()['ranking_groups'], loaded['ranking_groups'])
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertNotIn('token', saved['ranking'])
            self.assertEqual(saved['ranking']['server'], 'https://override.invalid')

    def test_migration_does_not_duplicate_existing_code(self):
        self.cfg['ranking'].update(code='AAAA-BBBB', token='obsolete')
        ranking.state()
        self.assertEqual(len(self.cfg['ranking_groups']), 2)
        self.assertEqual(self.cfg['ranking_groups'][0]['token'], 'a')
        self.assertNotIn('token', self.cfg['ranking'])

    def test_create_appends_and_join_existing_updates_without_new_membership(self):
        response = dict(code='EEEE-FFFF', name='추가', member_id='new', token='new-token')
        with patch.object(ranking, '_call', return_value=response) as call:
            ranking.create('추가', '새 이름')
            ranking.join(' aaaa-bbbb ', '바꿈')
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.args[:2], ('PUT', '/v1/groups/AAAA-BBBB/score'))
        self.assertEqual(len(self.cfg['ranking_groups']), 3)
        self.assertEqual(self.cfg['ranking_groups'][0]['nick'], '바꿈')
        self.assertEqual(self.cfg['ranking_groups'][0]['token'], 'a')
        self.assertEqual(self.cfg['ranking']['server'], 'https://example.invalid')

    def test_rejoin_replaces_only_expired_membership(self):
        response = dict(code='AAAA-BBBB', name='첫 그룹', member_id='renewed', token='new-token')
        with patch.object(ranking, '_call', side_effect=[ranking.RankingError('bad token'), response]) as call:
            ranking.join('AAAA-BBBB', '다시')
        self.assertEqual(call.call_args.args[:2], ('POST', '/v1/groups/AAAA-BBBB/join'))
        self.assertEqual(len(self.cfg['ranking_groups']), 2)
        self.assertEqual(self.cfg['ranking_groups'][0]['token'], 'new-token')
        self.assertEqual(self.cfg['ranking_groups'][1]['token'], 'b')

    def test_leave_one_and_nickname_are_group_scoped(self):
        ranking.set_nick('CCCC-DDDD', '새 닉네임')
        self.assertEqual([g['nick'] for g in self.cfg['ranking_groups']], ['하나', '새 닉네임'])
        ranking.set_nick('첫 그룹 닉네임')  # legacy signature
        with patch.object(ranking, '_call', side_effect=ranking.RankingError('offline')):
            ranking.leave('CCCC-DDDD')
        self.assertEqual([g['code'] for g in self.cfg['ranking_groups']], ['AAAA-BBBB'])
        self.assertEqual(self.cfg['ranking_groups'][0]['nick'], '첫 그룹 닉네임')
        self.assertEqual(self.cfg['ranking_global']['token'], 'global-token')

    def test_global_join_does_not_duplicate_existing_participant(self):
        with patch.object(ranking, '_call') as call:
            ranking.global_join('새 전체 이름')
        call.assert_not_called()
        self.assertEqual(self.cfg['ranking_global']['token'], 'global-token')
        self.assertEqual(self.cfg['ranking_global']['nick'], '새 전체 이름')

    def test_global_leave_keeps_token_until_server_deletes_records(self):
        with patch.object(ranking, '_call', side_effect=ranking.RankingError('offline')):
            with self.assertRaises(ranking.RankingError):
                ranking.global_leave()
        self.assertEqual(self.cfg['ranking_global']['token'], 'global-token')
        with patch.object(ranking, '_call', return_value={'ok': True}) as call:
            ranking.global_leave()
        self.assertEqual(call.call_args.args[:2], ('DELETE', '/v1/global/me'))
        self.assertEqual(self.cfg['ranking_global'], {})
        self.assertEqual(len(self.cfg['ranking_groups']), 2)

    def test_sync_continues_after_one_bad_token(self):
        def call(method, path, *args, **kwargs):
            if 'AAAA-BBBB' in path:
                raise ranking.RankingError('bad token')
            return {}
        with patch.object(ranking, '_call', side_effect=call) as remote, \
                patch.object(ranking, '_score_body', return_value={'periods': {}}) as score:
            with self.assertRaises(ranking.RankingError):
                ranking.sync(None, {}, force=True)
        self.assertEqual([c.args[1] for c in remote.call_args_list],
                         ['/v1/groups/AAAA-BBBB/score', '/v1/groups/CCCC-DDDD/score', '/v1/global/score'])
        self.assertEqual([c.args[2]['nick'] for c in remote.call_args_list], ['하나', '둘', '전체'])
        score.assert_called_once()

    def test_detected_plan_score_has_price_without_manual_config_and_no_account_suffix(self):
        db = DB(':memory:')
        self.addCleanup(db.conn.close)
        db.upsert_records([{'uid': 'priced', 'source': 'codex', 'ts': ranking.time.time(),
                            'model': 'gpt-5', 'input': 1000000, 'account': 'account-id'}])
        body = ranking._score_body(db, {'codex': {'accounts': [
            {'account': 'account-id', 'plan': 'Pro 100 ·private-suffix'}]}},
            {'sources': {'codex': True}}, '나')
        self.assertEqual(body['periods']['month']['sub'], 100)
        self.assertGreater(body['periods']['month']['api'], 0)
        self.assertNotIn('private-suffix', body['plans'])
        self.assertNotIn('account-id', json.dumps(body))
        unknown = ranking._score_body(db, {'codex': {'plan': 'Go'}}, {'sources': {'codex': True}}, '나')
        self.assertEqual(unknown['periods']['month']['sub'], 0)

    def test_ranking_without_code_uses_first_group(self):
        with patch.object(ranking, 'sync'), patch.object(ranking, '_call', return_value={'members': []}) as call:
            ranking.ranking(None, {}, 'month')
            self.assertIn('/v1/groups/AAAA-BBBB/ranking?', call.call_args.args[1])
            ranking.ranking(None, {}, 'week', 'CCCC-DDDD')
            self.assertIn('/v1/groups/CCCC-DDDD/ranking?', call.call_args.args[1])
            with self.assertRaises(ranking.RankingError):
                ranking.ranking(None, {}, 'week', 'EEEE-FFFF')

    def test_routes_pass_code_and_return_state_without_secrets(self):
        from tracker import server
        engine = Mock(); engine.snapshot = {'limits': {'codex': {'plan': 'Pro 100'}}}
        cls = server.make_handler(engine); handler = cls.__new__(cls)
        handler.path = '/api/ranking?code=CCCC-DDDD&period=month'; handler._send = Mock()
        with patch.object(ranking, 'ranking', return_value={}) as get:
            handler.do_GET()
        get.assert_called_once_with(engine.db, engine.snapshot['limits'], 'month', 'CCCC-DDDD')
        handler.path = '/api/ranking/leave'; handler.headers = {}
        handler._body = Mock(return_value={'code': 'CCCC-DDDD'})
        with patch.object(ranking, 'leave') as leave:
            handler.do_POST()
        leave.assert_called_once_with('CCCC-DDDD')
        self.assertNotIn('global-token', json.dumps(handler._send.call_args.args[1]))


class RankingWorker(unittest.TestCase):
    """Run the real Worker and its SQL against Node's in-memory SQLite, no deploy."""
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for Worker integration tests')
    def test_ratio_order_exclusions_ties_and_outside_top_hundred(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');
const crypto = require('node:crypto');
(async () => {
const root = process.argv[1], sql = new DatabaseSync(':memory:');
sql.exec(fs.readFileSync(path.join(root, 'server/schema.sql'), 'utf8'));
for (const file of fs.readdirSync(path.join(root, 'server/migrations')).sort()) sql.exec(fs.readFileSync(path.join(root, 'server/migrations', file), 'utf8'));
const wrap = statement => ({ values: [], bind(...args) { this.values = args; return this; },
  async first(column) { const row = statement.get(...this.values); return column ? row?.[column] ?? null : row || null; },
  async all() { return {results: statement.all(...this.values)}; }, async run() { return statement.run(...this.values); } });
const env = {DB:{prepare: text => wrap(sql.prepare(text)), batch: stmts => Promise.all(stmts.map(s => s.run()))}};
const source = fs.readFileSync(path.join(root, 'server/worker.js'), 'utf8');
const worker = (await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))).default;
const member = (id, api, sub, usage=api, version=2, group='GLOBAL', period='month', win='2026-10') => {
  sql.prepare('INSERT INTO members (id,group_code,token_hash,nick,plans,joined) VALUES (?,?,?,?,?,0)').run(id,group,crypto.createHash('sha256').update(id).digest('hex'),id,'자동 감지');
  sql.prepare('INSERT INTO scores (member_id,period,window,api,sub,updated,calculation_version,usage,tokens) VALUES (?,?,?,?,?,0,?,?,?)').run(id,period,win,api,sub,version,usage,1234);
};
const get = async id => {
  const response = await worker.fetch(new Request('https://example.invalid/v1/global/ranking?period=month&window=2026-10',{headers:{authorization:'Bearer '+id}}),env);
  assert.equal(response.status,200); return response.json();
};
member('high-usage', 1000, 1000, 9000); member('ratio-winner', 200, 20);
member('tie-high-api', 100, 20); member('tie-low-api', 50, 10);
member('tie-a', 25, 5); member('tie-b', 25, 5);
member('no-sub', 900, 0); member('zero-api', 0, 20, 5000);
member('legacy', 500, 1, 500, 1); member('other-group', 10000, 1, 10000, 2, 'FRIENDS');
member('old-window', 999, 1, 999, 2, 'GLOBAL', 'month', '2026-09');
let data = await get('ratio-winner');
assert.deepEqual(data.members.map(m=>m.nick), ['ratio-winner','tie-high-api','tie-low-api','tie-a','tie-b','high-usage']);
assert.equal(data.total,6); assert.equal(data.place,1); assert.equal(data.ratio,10); assert.equal(data.no_sub,false);
assert.deepEqual(Object.keys(data.members[0]).sort(), ['rank','nick','plans','api','sub','ratio','usage','tokens','me'].sort());
assert.equal((await get('tie-b')).place,5); assert.equal((await get('no-sub')).place,null); assert.equal((await get('no-sub')).no_sub,true);
assert.equal((await get('zero-api')).place,null); assert.equal((await get('legacy')).place,null);
for(let i=0;i<105;i++) member('filler-'+String(i).padStart(3,'0'), 3, 1);
data = await get('high-usage'); assert.equal(data.members.length,100); assert.equal(data.total,111); assert.equal(data.place,111);
assert.equal(data.api,1000); assert.equal(data.sub,1000); assert.equal(data.ratio,1); assert.equal(data.usage,9000); assert.equal(data.tokens,1234);
assert(!data.members.some(m=>m.me));
// Existing group endpoint and cap remain intact.
sql.prepare('INSERT INTO groups (code,name,created) VALUES (?,?,0)').run('ABCD-EFGH','친구');
for(let i=0;i<50;i++) member('friend-'+i,1,1,1,2,'ABCD-EFGH');
const full = await worker.fetch(new Request('https://example.invalid/v1/groups/ABCD-EFGH/join',{method:'POST',body:JSON.stringify({nick:'새 친구'})}),env);
assert.equal(full.status,409);
sql.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '--disable-warning=ExperimentalWarning', '-e', script, str(root)],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class RankingDashboard(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for dashboard script tests')
    def test_tab_defaults_deep_links_and_script_syntax(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1]+'/web/dashboard.html','utf8');
const js=html.match(/<script>([\s\S]*?)<\/script>/)[1]; new vm.Script(js);
const choose=js.match(/function chooseRankTab\([\s\S]*?\n}/)[0];
const read=js.match(/function readHash\([\s\S]*?\n}/)[0];
const write=js.match(/function writeHash\([\s\S]*?\n}/)[0];
const context={URLSearchParams,location:{hash:''},st:{range:'30',unit:'tokens',tool:'all'},history:{replaceState(a,b,hash){context.location.hash=hash;}}};
vm.createContext(context);vm.runInContext(choose+'\n'+read+'\n'+write,context);
const group={code:'AAAA-BBBB'},state={groups:[group],global:{joined:true}};
assert.equal(context.chooseRankTab({groups:[]},null,null),'global');
assert.equal(context.chooseRankTab({groups:[],global:{joined:true}},null,null),'global');
assert.equal(context.chooseRankTab({groups:[group]},null,null),'AAAA-BBBB');
assert.equal(context.chooseRankTab(state,null,'global'),'global');
assert.equal(context.chooseRankTab(state,'AAAA-BBBB','global'),'AAAA-BBBB');
assert.equal(context.chooseRankTab(state,'gone','gone'),'AAAA-BBBB');
assert.equal(context.chooseRankTab({groups:[]},'add','gone'),'global');
context.location.hash='#v=rank&r=global';context.readHash();assert.equal(context.st.rankTab,'global');assert.equal(context.st.range,'30');
context.st.rankTab='AAAA-BBBB';context.writeHash();assert(context.location.hash.includes('r=AAAA-BBBB'));context.readHash();assert.equal(context.st.rankTab,'AAAA-BBBB');
assert(!html.includes('public_ranking'));
const globalCode=js.slice(js.indexOf('function globalView()'),js.indexOf('async function loadRanking()'));
assert(!globalCode.includes('쌀숭'));assert(!globalCode.includes('tierOf'));
'''
        result = subprocess.run(['node', '-e', script, str(root)], capture_output=True,
                                text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
