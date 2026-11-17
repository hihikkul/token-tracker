"""Collector regressions: replay, rotation, malformed input and provider attribution."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tracker import collectors, sources
from tracker.db import DB


class CollectorFixes(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.claude = self.root / 'claude'
        self.projects = self.claude / 'projects' / 'work'
        self.sessions = self.root / 'sessions'
        self.archive = self.root / 'archived_sessions'
        self.gemini = self.root / 'gemini'
        for directory in (self.projects, self.sessions, self.archive, self.gemini):
            directory.mkdir(parents=True)
        self.db = DB(':memory:')
        self.addCleanup(self.db.conn.close)
        patches = [
            patch.object(collectors.paths, 'claude_roots', return_value=[self.claude]),
            patch.object(collectors.paths, 'claude_project_dirs', return_value=[self.projects]),
            patch.object(collectors.paths, 'codex_session_dirs', return_value=[self.sessions, self.archive]),
            patch.object(collectors.paths, 'gemini_root', return_value=self.gemini),
            patch.object(collectors.paths, 'load_config', return_value={'extra_paths': {}}),
            patch('tracker.ripple.chatgpt_snapshots', return_value=[]),
            patch('tracker.ripple.active_account', return_value='today'),
            patch('tracker.ripple.account_timeline', return_value=[]),
            patch.dict(os.environ, {'ANTHROPIC_BASE_URL': '', 'TOKENTRACKER_HOME': str(self.root / 'data'),
                                    'CLAUDE_CONFIG_DIR': str(self.claude), 'CODEX_HOME': str(self.root / 'codex'),
                                    'GEMINI_HOME': str(self.gemini), 'CLAUDERIPPLE_HOME': str(self.root / 'ripple')}),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def write_lines(self, path, objects, append=False):
        with path.open('a' if append else 'w', encoding='utf-8') as stream:
            for obj in objects:
                stream.write(json.dumps(obj) + '\n')
        return path

    def claude_turn(self, mid='m', model='claude-sonnet-4', tokens=10, ts=100, **extra):
        return {'type': 'assistant', 'timestamp': ts, 'requestId': f'req_{mid}',
                'message': {'id': mid, 'model': model, 'usage': {'input_tokens': tokens}}, **extra}

    def codex_meta(self, sid='s', model='gpt-5', **extra):
        return {'type': 'session_meta', 'payload': {'id': sid, 'model': model, 'creator_account_id': 'a', **extra}}

    def codex_turn(self, total, ts=None, usage=None, **extra):
        return {'type': 'event_msg', 'timestamp': total if ts is None else ts,
                'payload': {'type': 'token_count', 'info': {'total_token_usage': {
                    'input_tokens': total, 'total_tokens': total, **(usage or {})}}, **extra}}

    def gemini_turn(self, tokens, ts=100, **extra):
        return {'timestamp': ts, 'model': 'gemini-3-pro', 'role': 'model',
                'tokens': {'input': tokens}, **extra}

    def total(self):
        return self.db.query('SELECT COALESCE(SUM(input + output + cache_read + cache_write),0) AS n FROM records')[0]['n']

    def test_replaced_jsonl_larger_than_old_offset_restarts(self):
        path = self.write_lines(self.projects / 's.jsonl', [self.claude_turn('old')])
        collectors.scan_claude(self.db, {})
        replacement = self.write_lines(self.root / 'replacement', [self.claude_turn('new'), self.claude_turn('last')])
        os.replace(replacement, path)
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.total(), 30)
        self.assertEqual(self.db.get_file(path)['state']['_lines'], 2)
        self.assertIn('_fingerprint', self.db.get_file(path)['state'])

    def test_truncated_and_regrown_same_inode_restarts(self):
        path = self.write_lines(self.projects / 's.jsonl', [self.claude_turn('old')])
        collectors.scan_claude(self.db, {})
        self.write_lines(path, [self.claude_turn('new'), self.claude_turn('last')])
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.total(), 30)

    def test_modified_window_before_offset_restarts_even_with_same_prefix(self):
        path = self.write_lines(self.projects / 's.jsonl', [self.claude_turn('same', padding='x' * 5000), self.claude_turn('old')])
        collectors.scan_claude(self.db, {})
        self.write_lines(path, [self.claude_turn('same', padding='x' * 5000), self.claude_turn('new'), self.claude_turn('last')])
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.total(), 40)

    def test_same_size_and_mtime_replacement_is_not_unchanged(self):
        path = self.write_lines(self.projects / 's.jsonl', [self.claude_turn('old')])
        collectors.scan_claude(self.db, {})
        st = path.stat()
        self.write_lines(path, [self.claude_turn('new')])
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.total(), 20)

    def test_codex_counter_reset_and_archive_have_stable_generation(self):
        path = self.write_lines(self.sessions / 'rollout.jsonl', [self.codex_meta(),
            self.codex_turn(100, 1), self.codex_turn(200, 2)])
        collectors.scan_codex(self.db, {})
        self.write_lines(path, [self.codex_turn(100, 3), self.codex_turn(200, 4)], append=True)
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.total(), 400)
        path.rename(self.archive / path.name)
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.total(), 400)
        self.assertEqual({r['uid'] for r in self.db.query('SELECT uid FROM records')},
                         {'x:s:100', 'x:s:200', 'x:s:100:g1', 'x:s:200:g1'})

    def test_codex_fork_and_parent_keep_per_file_deltas_in_both_scan_orders(self):
        counts = ('input', 'output', 'cache_write', 'cache_read', 'reasoning')
        parent_usage = [
            (30979, 30000, 10000, 1000, 979, 100),
            (67056, 65000, 40000, 1500, 2056, 200),
            (200000, 197000, 155520, 2000, 3000, 300),
            (500000, 490000, 350000, 3000, 10000, 1000),
            (20000951, 19800951, 18000000, 4000, 200000, 20000),
        ]
        fork_usage = [
            (237709, 237408, 234624, 0, 301, 101),
            (482704, 481904, 470000, 100, 800, 301),
            (736862, 735362, 700000, 200, 1500, 501),
        ]
        expected = {f'x:parent:{total}': values for total, values in (
            (30979, (19000, 979, 1000, 10000, 100)),
            (67056, (4500, 1077, 500, 30000, 100)),
            (200000, (15980, 944, 500, 115520, 100)),
            (500000, (97520, 7000, 1000, 194480, 700)),
            (20000951, (1659951, 190000, 1000, 17650000, 19000)),
            (237709, (2784, 301, 0, 234624, 101)),
            (482704, (9020, 499, 100, 235376, 200)),
            (736862, (23358, 700, 100, 230000, 200)),
        )}
        for directory, metadata, usage in (
                (self.sessions, [self.codex_meta('parent')], parent_usage),
                (self.archive, [self.codex_meta('fork', forked_from_id='parent', source='parent'),
                               self.codex_meta('parent')], fork_usage)):
            turns = [self.codex_turn(total, ts=i + 1, usage={
                'input_tokens': inp, 'cached_input_tokens': cached, 'cache_write_input_tokens': cwrite,
                'output_tokens': out, 'reasoning_output_tokens': reasoning})
                for i, (total, inp, cached, cwrite, out, reasoning) in enumerate(usage)]
            self.write_lines(directory / 'rollout.jsonl', metadata + turns)
        for order in ((self.sessions, self.archive), (self.archive, self.sessions)):
            for separate_scans in (False, True):
                with self.subTest(first=order[0].name, separate_scans=separate_scans):
                    db = DB(':memory:')
                    self.addCleanup(db.conn.close)
                    if separate_scans:
                        with patch.object(collectors.paths, 'codex_session_dirs', return_value=[order[0]]):
                            collectors.scan_codex(db, {})
                        first_rows = db.query('SELECT * FROM records ORDER BY uid')
                        self.assertEqual({r['uid']: tuple(r[key] for key in counts) for r in first_rows},
                                         {r['uid']: expected[r['uid']] for r in first_rows})
                    with patch.object(collectors.paths, 'codex_session_dirs', return_value=list(order)):
                        collectors.scan_codex(db, {})
                        rows = db.query('SELECT * FROM records ORDER BY uid')
                        self.assertEqual({r['uid']: tuple(r[key] for key in counts) for r in rows}, expected)
                        self.assertEqual({r['session'] for r in rows}, {'parent'})
                        db.set_kv('codex_record_accounts_version', 1)
                        collectors.scan_codex(db, {})
                        self.assertEqual(db.query('SELECT * FROM records ORDER BY uid'), rows)

    def test_partial_codex_copy_preserves_known_counts_across_scans(self):
        self.write_lines(self.sessions / 'original.jsonl', [self.codex_meta(),
            self.codex_turn(100), self.codex_turn(200)])
        collectors.scan_codex(self.db, {})
        # An unseen first total cannot distinguish a partial copy from a fork counter.
        copy = self.write_lines(self.archive / 'copy.jsonl', [self.codex_meta(),
            self.codex_turn(200), self.codex_turn(300)])
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.total(), 300)
        self.write_lines(self.archive / 'another.jsonl', [self.codex_meta(), self.codex_turn(300)])
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.total(), 300)
        self.assertEqual(self.db.query('SELECT input FROM records ORDER BY uid'), [{'input': 100}] * 3)
        self.assertEqual(self.db.get_file(copy)['state']['prev']['total_tokens'], 300)

    def test_partial_codex_copy_preserves_counts_produced_earlier_in_same_scan(self):
        self.write_lines(self.sessions / 'original.jsonl', [self.codex_meta(),
            self.codex_turn(100), self.codex_turn(200)])
        self.write_lines(self.archive / 'copy.jsonl', [self.codex_meta(),
            self.codex_turn(200), self.codex_turn(300)])
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.total(), 300)
        self.assertEqual(self.db.query('SELECT input FROM records ORDER BY uid'), [{'input': 100}] * 3)

    def test_partial_codex_archive_matches_reset_generation_by_timestamp(self):
        self.write_lines(self.sessions / 'original.jsonl', [self.codex_meta(),
            self.codex_turn(100, 1), self.codex_turn(200, 2),
            self.codex_turn(100, 3), self.codex_turn(200, 4)])
        collectors.scan_codex(self.db, {})
        copy = self.write_lines(self.archive / 'copy.jsonl', [self.codex_meta(),
            self.codex_turn(200, 4), self.codex_turn(300, 5)])
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.total(), 500)
        rows = self.db.query('SELECT uid,input FROM records ORDER BY uid')
        self.assertEqual(rows, [{'uid': uid, 'input': 100} for uid in
            ('x:s:100', 'x:s:100:g1', 'x:s:200', 'x:s:200:g1', 'x:s:300:g1')])
        self.assertEqual(self.db.get_file(copy)['state']['generation'], 1)
        self.db.set_kv('codex_record_accounts_version', 1)
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.db.query('SELECT uid,input FROM records ORDER BY uid'), rows)

    def test_gemini_insertion_does_not_shift_anonymous_uids(self):
        path = self.gemini / 'messages.json'
        first = [self.gemini_turn(10, 10), self.gemini_turn(20, 20)]
        with patch.object(collectors, '_gemini_files', return_value=[path]):
            path.write_text(json.dumps(first), encoding='utf-8')
            collectors.scan_gemini(self.db, {})
            path.write_text(json.dumps([self.gemini_turn(99, 99)] + first), encoding='utf-8')
            collectors.scan_gemini(self.db, {})
        self.assertEqual(self.total(), 129)
        self.assertEqual(len(self.db.query('SELECT uid FROM records')), 3)

    def test_identical_anonymous_messages_have_occurrence_ids_across_jsonl_scans(self):
        path = self.write_lines(self.gemini / 'messages.jsonl', [self.gemini_turn(10)])
        with patch.object(collectors, '_gemini_files', return_value=[path]):
            collectors.scan_gemini(self.db, {})
            self.write_lines(path, [self.gemini_turn(10)], append=True)
            collectors.scan_gemini(self.db, {})
            collectors.scan_gemini(self.db, {})
        self.assertEqual(self.total(), 20)
        self.assertEqual(len(self.db.query('SELECT uid FROM records')), 2)

    def test_proxy_turns_use_scan_time_account_at_any_age_but_vendors_never_do(self):
        now = int(time.time())
        self.db.upsert_records([{'uid': 'c:old:r', 'source': 'codex', 'model': 'gpt-5', 'ts': 1, 'output': 1}])
        turns = [self.claude_turn('past', 'gpt-5', ts=now - 901),
                 self.claude_turn('recent', 'gpt-5', ts=now - 10),
                 self.claude_turn('future', 'gpt-5', ts=now + 3600),
                 self.claude_turn('vendor', 'openai/gpt-5', ts=now)]
        for turn in turns:
            turn['message']['usage']['output_tokens'] = 1
        self.write_lines(self.projects / 's.jsonl', turns)
        collectors.scan_claude(self.db, {})
        accounts = {r['uid']: r['account'] for r in self.db.query('SELECT uid, account FROM records')}
        self.assertEqual(accounts, {'c:old:r': None, 'c:past:req_past': 'today',
            'c:recent:req_recent': 'today', 'c:future:req_future': 'today', 'c:vendor:req_vendor': None})

    def test_active_ripple_account_is_used_instead_of_list_order(self):
        with patch('tracker.ripple.active_account', return_value='active'):
            self.assertEqual(collectors.proxy_chatgpt_account(), 'active')

    def test_malformed_claude_record_does_not_block_later_records_or_files(self):
        malformed = self.claude_turn('bad', tokens='N/A')
        nested = self.claude_turn('nested')
        nested['message'] = ['unexpected']
        self.write_lines(self.projects / 'bad.jsonl', [malformed, nested, self.claude_turn('valid', tokens='10')])
        self.write_lines(self.projects / 'later.jsonl', [self.claude_turn('later')])
        report = {}
        collectors.scan_claude(self.db, report)
        self.assertEqual(self.total(), 20)
        self.assertEqual(report['claude']['skipped'], 2)
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.total(), 20)

    def test_malformed_codex_usage_and_nested_payload_do_not_block_provider(self):
        bad = self.codex_turn(100)
        bad['payload']['info']['total_token_usage']['input_tokens'] = 'N/A'
        self.write_lines(self.sessions / 'bad.jsonl', [self.codex_meta(), bad,
            {'type': 'event_msg', 'payload': ['unexpected']}, self.codex_turn(100)])
        self.write_lines(self.archive / 'later.jsonl', [self.codex_meta('another'), self.codex_turn(50)])
        report = {}
        collectors.scan_codex(self.db, report)
        self.assertEqual(self.total(), 150)
        self.assertEqual(report['codex']['skipped'], 2)

    def test_malformed_gemini_usage_does_not_block_nested_siblings_or_files(self):
        path = self.gemini / 'bad.json'
        path.write_text(json.dumps([self.gemini_turn('N/A'), self.gemini_turn(10)]), encoding='utf-8')
        later = self.write_lines(self.gemini / 'later.jsonl', [self.gemini_turn(20)])
        report = {}
        with patch.object(collectors, '_gemini_files', return_value=[path, later]):
            collectors.scan_gemini(self.db, report)
        self.assertEqual(self.total(), 30)
        self.assertEqual(report['gemini']['skipped'], 1)

    def test_upsert_corrects_source_and_fallback_model_but_keeps_original_timestamp(self):
        self.db.upsert_records([{'uid': 'g', 'source': 'claude', 'model': 'gemini', 'ts': 1, 'input': 10}])
        self.db.upsert_records([{'uid': 'g', 'source': 'gemini', 'model': 'gemini-3-pro', 'ts': 2, 'input': 20}])
        self.db.upsert_records([{'uid': 'g', 'source': 'gemini', 'model': 'gemini', 'ts': 3, 'input': 5}])
        self.assertEqual(self.db.query('SELECT source,model,ts,input FROM records'),
                         [{'source': 'gemini', 'model': 'gemini-3-pro', 'ts': 1, 'input': 20}])

    def test_large_jsonl_is_incremental_and_large_json_is_reported(self):
        path = self.write_lines(self.gemini / 'antigravity.jsonl', [self.gemini_turn(10)])
        large = self.gemini / 'large.json'
        large.write_text('[]', encoding='utf-8')
        original_stat = Path.stat
        def fake_stat(p, *args, **kwargs):
            st = original_stat(p, *args, **kwargs)
            if p in (path, large):
                # Preserve size changes across appends even when the filesystem mtime is unchanged.
                return SimpleNamespace(st_size=81 * 1024 * 1024 + st.st_size, st_mtime=st.st_mtime)
            return st
        report = {}
        with patch.object(collectors, '_gemini_files', return_value=[path, large]), patch.object(Path, 'stat', fake_stat), \
                patch.object(Path, 'read_text', side_effect=AssertionError('JSONL must not load whole file')):
            collectors.scan_gemini(self.db, report)
            self.assertEqual(self.total(), 10)
            self.assertEqual(report['gemini']['skipped_large'], 1)
            self.write_lines(path, [self.gemini_turn(20, 200)], append=True)
            collectors.scan_gemini(self.db, {})
        self.assertEqual(self.total(), 30)
        self.assertEqual(self.db.query('SELECT DISTINCT source FROM records'), [{'source': 'antigravity'}])

    def test_failed_executemany_rolls_back_before_next_unrelated_commit(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.upsert_records([{'uid': 'ok', 'source': 'claude', 'ts': 1},
                                    {'uid': 'bad', 'source': None, 'ts': 2}])
        self.assertFalse(self.db.conn.in_transaction)
        self.db.set_kv('unrelated', True)
        self.assertEqual(self.db.query('SELECT uid FROM records'), [])

    def test_vendor_migration_runs_after_old_marker_and_clears_non_codex_accounts(self):
        self.db.set_kv('claude_vendor_split', 1)
        models = [('other', 'deepseek-v4'), ('claude', 'openai/gpt-oss'),
                  ('codex', 'qwen3-coder'), ('gemini', 'google/gemini-3-pro'), ('claude', 'gpt-5')]
        self.db.upsert_records([{'uid': str(i), 'source': src, 'model': model, 'ts': 1, 'account': 'a'}
                                for i, (src, model) in enumerate(models)])
        collectors.scan_claude(self.db, {})
        got = self.db.query('SELECT model,source,account FROM records ORDER BY uid')
        self.assertEqual([(r['source'], r['account']) for r in got],
                         [('deepseek', None), ('openai_api', None), ('qwen', None), ('google', None), ('codex', 'a')])
        self.assertEqual(self.db.get_kv('vendor_split_v2'), 1)

    def test_masked_deepseek_needs_config_and_lacks_both_anthropic_markers(self):
        turns = [self.claude_turn('opus', 'claude-opus-4-8', requestId='deepseek-response'),
                 self.claude_turn('sonnet', 'claude-sonnet-4', requestId='deepseek-response'),
                 self.claude_turn('real-request', 'claude-opus-4-8'),
                 self.claude_turn('real-cache', 'claude-opus-4-8', requestId='other')]
        turns[-1]['message']['usage']['cache_creation'] = {}
        for turn in turns:
            turn['message']['usage']['output_tokens'] = 1
        self.write_lines(self.projects / 's.jsonl', turns)
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}):
            collectors.scan_claude(self.db, {})
        got = {r['uid']: (r['source'], r['model']) for r in self.db.query('SELECT * FROM records')}
        self.assertEqual(got['c:opus:deepseek-response'], ('deepseek', 'deepseek-v4-pro'))
        self.assertEqual(got['c:sonnet:deepseek-response'], ('deepseek', 'deepseek-flash'))
        self.assertEqual(got['c:real-request:req_real-request'][0], 'claude')
        self.assertEqual(got['c:real-cache:other'][0], 'claude')

    def test_masked_deepseek_settings_and_bedrock_vertex_are_not_guessed(self):
        self.write_lines(self.projects / 's.jsonl', [self.claude_turn('bedrock', requestId='aws-id')])
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.db.query('SELECT source FROM records'), [{'source': 'claude'}])
        (self.claude / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}}))
        masked = self.claude_turn('masked', requestId='deepseek-id')
        masked['message']['usage']['output_tokens'] = 1
        self.write_lines(self.projects / 'later.jsonl', [masked])
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.db.query("SELECT model FROM records WHERE source='deepseek'"), [{'model': 'deepseek-flash'}])

    def test_codex_vendor_models_and_provider_never_get_codex_account_or_limits(self):
        rate_limits = {'plan_type': 'plus', 'primary': {'used_percent': 20, 'window_minutes': 300}}
        for i, (model, provider) in enumerate([('deepseek-v4', 'deepseek'), ('qwen3-coder', 'ollama'),
                                               ('openai/gpt-5', 'openrouter'), ('gpt-5', 'deepseek')]):
            self.write_lines(self.sessions / f'{i}.jsonl', [self.codex_meta(str(i), model, model_provider=provider),
                                                          self.codex_turn(100, rate_limits=rate_limits)])
        report = {}
        collectors.scan_codex(self.db, report)
        rows = self.db.query('SELECT source,account FROM records ORDER BY session')
        self.assertEqual(rows, [{'source': key, 'account': None} for key in ('deepseek', 'qwen', 'openai_api', 'deepseek')])
        self.assertEqual(self.db.get_kv('codex_limits_by_account'), {})
        self.assertEqual(report['codex']['accounts'], 0)

    def test_vendor_migration_does_not_guess_from_fallback_models(self):
        self.db.upsert_records([{'uid': 'g:old', 'source': 'gemini', 'model': 'gemini', 'ts': 1},
                                {'uid': 'x:old:1', 'source': 'codex', 'model': None, 'ts': 1}])
        collectors.scan_claude(self.db, {})
        self.assertEqual(self.db.query('SELECT source FROM records ORDER BY uid'),
                         [{'source': 'gemini'}, {'source': 'codex'}])

    def test_malformed_codex_metadata_does_not_poison_following_events(self):
        self.write_lines(self.sessions / 's.jsonl', [self.codex_meta(),
            {'type': 'turn_context', 'payload': {'model': {'unexpected': 'nested'}}}, self.codex_turn(100)])
        report = {}
        collectors.scan_codex(self.db, report)
        self.assertEqual(self.total(), 100)
        self.assertEqual(report['codex']['skipped'], 1)
        self.assertEqual(self.db.query('SELECT model FROM records'), [{'model': 'gpt-5'}])

    def test_incomplete_jsonl_tail_waits_for_newline(self):
        path = self.gemini / 'messages.jsonl'
        path.write_text(json.dumps(self.gemini_turn(10)), encoding='utf-8')
        with patch.object(collectors, '_gemini_files', return_value=[path]):
            collectors.scan_gemini(self.db, {})
            self.assertEqual(self.total(), 0)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n')
            collectors.scan_gemini(self.db, {})
        self.assertEqual(self.total(), 10)

    def test_turn_context_provider_override_is_used(self):
        self.write_lines(self.sessions / 's.jsonl', [self.codex_meta(),
            {'type': 'turn_context', 'payload': {'model': 'deepseek-v4', 'model_provider': 'deepseek'}}, self.codex_turn(100)])
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.db.query('SELECT source,model,account FROM records'),
                         [{'source': sources.vendor_source('deepseek-v4'), 'model': 'deepseek-v4', 'account': None}])

    def test_estimate_skip_includes_vendors_gemini_and_masked_deepseek(self):
        turns = []
        for mid, model, request in (('vendor', 'deepseek-v4', 'r'), ('gemini', 'gemini-3-pro', 'r'),
                                    ('masked', 'claude-opus-4-8', 'deepseek-id')):
            estimate = self.claude_turn(mid, model, tokens=9700000, requestId=request)
            estimate['message']['usage']['output_tokens'] = '0'
            turns.append(estimate)
            final = self.claude_turn(mid, model, tokens='7', requestId=request)
            final['message']['usage'].update(output_tokens='2', cache_read_input_tokens='90')
            turns.append(final)
        turns.append(self.claude_turn('estimate-only', 'deepseek-v4', tokens=9700000))
        self.write_lines(self.projects / 's.jsonl', turns)
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}):
            collectors.scan_claude(self.db, {})
            collectors.scan_claude(self.db, {})
        rows = self.db.query('SELECT source,input,output,cache_read,account FROM records ORDER BY uid')
        self.assertEqual(rows, [{'source': src, 'input': 7, 'output': 2, 'cache_read': 90, 'account': None}
                                for src in ('gemini', 'deepseek', 'deepseek')])

    def test_vendor_repair_upgrades_upstream_marker_and_handles_bad_lines(self):
        self.db.set_kv('claude_vendor_split', 2)
        self.db.set_kv('claude_proxy_usage_repaired', 1)
        self.db.upsert_records([
            {'uid': 'c:vendor:r', 'source': 'other', 'model': 'deepseek-v4', 'ts': 1, 'input': 9700000, 'output': 2, 'account': 'a'},
            {'uid': 'c:masked:deepseek-id', 'source': 'claude', 'model': 'claude-opus-4-8', 'ts': 1, 'input': 9700000, 'output': 2},
            {'uid': 'c:estimate:r', 'source': 'other', 'model': 'deepseek-v4', 'ts': 1, 'input': 9700000, 'account': 'a'}])
        vendor = self.claude_turn('vendor', 'deepseek-v4', tokens='7', requestId='r')
        vendor['message']['usage'].update(output_tokens='2', cache_read_input_tokens='90')
        masked = self.claude_turn('masked', 'claude-opus-4-8', tokens=3, requestId='deepseek-id')
        masked['message']['usage']['output_tokens'] = 2
        path = self.write_lines(self.projects / 's.jsonl', [[], {'type': 'assistant', 'message': []},
            self.claude_turn('invalid', tokens='N/A'), vendor, masked,
            self.claude_turn('estimate', 'deepseek-v4', tokens=9700000, requestId='r')])
        res = collectors.read_new_lines(self.db, path)
        list(res['lines'])
        res['commit']({})  # Repair must work even when the incremental scan sees an unchanged file.
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}):
            collectors.scan_claude(self.db, {})
            rows = self.db.query('SELECT uid,source,model,input,output,cache_read,account FROM records ORDER BY uid')
            self.assertEqual(rows, [
                {'uid': 'c:masked:deepseek-id', 'source': 'deepseek', 'model': 'deepseek-v4-pro',
                 'input': 3, 'output': 2, 'cache_read': 0, 'account': None},
                {'uid': 'c:vendor:r', 'source': 'deepseek', 'model': 'deepseek-v4',
                 'input': 7, 'output': 2, 'cache_read': 90, 'account': None}])
            self.db.conn.execute("UPDATE records SET input = 1 WHERE uid = 'c:vendor:r'")
            collectors.scan_claude(self.db, {})
        self.assertEqual(self.db.query("SELECT input FROM records WHERE uid = 'c:vendor:r'"), [{'input': 1}])
        self.assertEqual(self.db.get_kv('claude_proxy_usage_repaired'), 2)
        self.assertEqual(self.db.get_kv('vendor_split_v2'), 1)

    def test_vendor_migration_markers_compose_and_each_runs_once(self):
        for split, vendors in ((1, 1), (2, None), (1, None)):
            with self.subTest(split=split, vendors=vendors):
                db = DB(':memory:')
                self.addCleanup(db.conn.close)
                db.set_kv('claude_vendor_split', split)
                if vendors is not None:
                    db.set_kv('vendor_split_v2', vendors)
                db.upsert_records([{'uid': 'c:gpt:r', 'source': 'claude', 'model': 'gpt-5', 'ts': 1},
                                   {'uid': 'x:vendor:1', 'source': 'codex', 'model': 'deepseek-v4', 'ts': 1, 'account': 'a'}])
                collectors._split_claude_vendors(db)
                self.assertEqual(db.get_kv('claude_vendor_split'), 2)
                self.assertEqual(db.get_kv('vendor_split_v2'), 1)
                self.assertEqual(db.query("SELECT source FROM records WHERE uid = 'c:gpt:r'"), [{'source': 'codex'}])
                if vendors is None:
                    self.assertEqual(db.query("SELECT source,account FROM records WHERE uid = 'x:vendor:1'"),
                                     [{'source': 'deepseek', 'account': None}])
                db.conn.execute("UPDATE records SET source = 'claude' WHERE uid = 'c:gpt:r'")
                collectors._split_claude_vendors(db)
                self.assertEqual(db.query("SELECT source FROM records WHERE uid = 'c:gpt:r'"), [{'source': 'claude'}])

    def test_served_account_survives_incremental_scan_but_never_leaks_to_vendor(self):
        reset = int(time.time()) + 86400
        quota = {'secondary': {'used_percent': 20, 'window_minutes': 10080, 'resets_at': reset}}
        live = [{'account': 'served', 'windows': [{'window_minutes': 10080, 'resets_at': reset}]}]
        path = self.write_lines(self.sessions / 's.jsonl', [self.codex_meta(), self.codex_turn(100, rate_limits=quota)])
        with patch('tracker.ripple.chatgpt_snapshots', return_value=live):
            collectors.scan_codex(self.db, {})
        self.assertEqual(self.db.get_file(path)['state']['served'], 'served')
        self.write_lines(path, [
            {'type': 'turn_context', 'payload': {'model': 'deepseek-v4', 'model_provider': 'deepseek'}},
            self.codex_turn(200, rate_limits=quota),
            {'type': 'turn_context', 'payload': {'model': 'gpt-5', 'model_provider': 'openai'}},
            self.codex_turn(300)], append=True)
        collectors.scan_codex(self.db, {})
        self.assertEqual(self.db.query('SELECT uid,source,account,input FROM records ORDER BY uid'), [
            {'uid': 'x:s:100', 'source': 'codex', 'account': 'served', 'input': 100},
            {'uid': 'x:s:200', 'source': 'deepseek', 'account': None, 'input': 100},
            {'uid': 'x:s:300', 'source': 'codex', 'account': 'served', 'input': 100}])


if __name__ == '__main__':
    unittest.main()
