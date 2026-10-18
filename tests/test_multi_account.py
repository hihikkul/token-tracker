import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from tracker import collectors, limits, ranking
from tracker.db import DB


class MultiAccount(unittest.TestCase):
    def setUp(self):
        self.db=DB(':memory:')
        self.cfg={'sources':{'codex':True},'subscription_plans':{'codex':'auto'}}

    def tearDown(self):
        self.db.conn.close()

    def test_same_plan_accounts_remain_distinct(self):
        snaps={}
        for account in ('a','b'):
            snapshot={'ts':time.time(),'plan':'plus','limit_id':'codex',
                      'windows':[{'key':'secondary','window_minutes':10080,'resets_at':time.time()+1000,'used_percent':20}]}
            collectors._keep_limits(snaps,snapshot,account,{},True)
        self.db.set_kv('codex_limits_by_account',snaps)
        with patch('tracker.ripple.chatgpt_snapshots', return_value=[]):
            accounts=limits.codex_limits(self.db)['accounts']
        self.assertEqual({a['account'] for a in accounts},{'a','b'})

    def test_monthly_fees_and_usage_are_per_account(self):
        for account in ('a','b',None):
            self.db.upsert_records([{'uid':str(account),'source':'codex','ts':int(time.time()),
                                     'model':'gpt-5','input':1000000,'account':account}])
        data={'codex':{'accounts':[{'account':'a','plan':'Plus'},{'account':'b','plan':'Plus'}]}}
        period=ranking.report(self.db,data,self.cfg)['periods']['month']
        self.assertEqual(period['sub'],40)
        self.assertEqual(len(period['tools']),3)
        unknown=period['tools']['codex:unknown']
        self.assertGreater(unknown['api'],0)
        self.assertIsNone(unknown['ratio'])
        self.assertIn('codex:unknown',period['excluded'])
        known=sum(row['api'] for row in period['tools'].values() if row['sub'])
        self.assertAlmostEqual(period['api'],known,places=4)

    def test_planless_quota_keeps_the_matching_account_label(self):
        snaps={}
        for account,plan,reset in [('a','plus',10000),('b','self_serve_business_prolite',20000)]:
            collectors._keep_limits(snaps,{'ts':1,'plan':plan,'limit_id':'codex',
                'windows':[{'window_minutes':10080,'resets_at':reset,'used_percent':5}]},account,{},True)
        newest={'ts':2,'limit_id':'codex','windows':[{'window_minutes':10080,'resets_at':20000,'used_percent':21}]}
        collectors._keep_limits(snaps,newest,'a',{'a':'plus'},False)
        self.assertEqual(newest['account'],'b')
        self.assertEqual(newest['plan_guess'],'self_serve_business_prolite')
        self.assertEqual(snaps['a:codex']['plan'],'plus')

    def test_unknown_price_does_not_exclude_other_account(self):
        data={'codex':{'accounts':[{'account':'a','plan':'Business Lite'},{'account':'b','plan':'Plus'}]}}
        self.db.upsert_records([{'uid':'b','source':'codex','ts':int(time.time()),'model':'gpt-5','input':1000000,'account':'b'}])
        result=ranking.report(self.db,data,self.cfg)['periods']['month']
        self.assertEqual(result['sub'],20)
        self.assertGreater(result['api'],0)
        self.cfg['account_plans']={'codex':{ranking.account_key('a'):'pro_100'}}
        self.assertEqual(ranking.report(self.db,data,self.cfg)['periods']['month']['sub'],120)

    def test_backfill_adds_identity_without_duplicate_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rollout-test.jsonl'
            rows=[{'type':'session_meta','payload':{'id':'session-a','creator_account_id':'a'}},
                  {'type':'event_msg','timestamp':int(time.time()),'payload':{'type':'token_count',
                   'rate_limits':{'plan_type':'plus','secondary':{'used_percent':20,'window_minutes':10080}},
                   'info':{'total_token_usage':{'input_tokens':100,'output_tokens':10,'total_tokens':110}}}}]
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
            self.db.upsert_records([{'uid':'x:session-a:110','source':'codex','ts':int(time.time()),'input':100,'output':10}])
            with patch.object(collectors.paths,'codex_session_dirs',return_value=[Path(tmp)]):
                collectors.scan_codex(self.db,{})
                collectors.scan_codex(self.db,{})
            records=self.db.query('SELECT account,input,output FROM records')
            self.assertEqual(records,[{'account':'a','input':100,'output':10}])
