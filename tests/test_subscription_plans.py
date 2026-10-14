import unittest
from unittest.mock import Mock, patch
from tracker import ranking, server


class SubscriptionPlans(unittest.TestCase):
    def cfg(self, plan):
        return {'sources':{'claude':True,'codex':True,'gemini':True,'antigravity':True},
                'subscription_plans':plan, 'subscription_usd_month':{'claude':999}}

    def test_selection_overrides_detected_plan_and_old_price(self):
        result=ranking.subscriptions({'claude':{'plan':'Pro'},'codex':{'plan':'Business Lite'}},
                                     self.cfg({'claude':'max_20x','codex':'pro_100','gemini':'pro'}))
        self.assertEqual(result['claude'], {'usd':200,'plan':'Max 20x','from':'selection'})
        self.assertEqual(result['codex']['usd'],100)
        self.assertEqual(result['gemini']['usd'],19.99)
        self.assertEqual(len(result),3)

    def test_free_and_unknown_do_not_fall_back_to_paid_detection(self):
        result=ranking.subscriptions({'claude':{'plan':'Pro'},'codex':{'plan':'Plus'}},
                                     self.cfg({'claude':'free','codex':'business_lite'}))
        self.assertEqual(result['claude']['usd'],0)
        self.assertIsNone(result['codex']['usd'])

    def test_auto_uses_detection(self):
        cfg=self.cfg({'codex':'auto'}); cfg['subscription_usd_month']={}
        self.assertEqual(ranking.subscriptions({'codex':{'plan':'Plus'}},cfg)['codex']['usd'],20)

    def test_save_validates_plan_and_clears_previous_override(self):
        engine=Mock(); cls=server.make_handler(engine); handler=cls.__new__(cls)
        handler.path='/api/settings';handler.headers={};handler._send=Mock()
        handler._body=Mock(return_value={'subscription_plans':{'claude':'auto','codex':'pro_500','gemini':'invalid'}})
        cfg=self.cfg({})
        with patch.object(server.paths,'load_config',return_value=cfg),patch.object(server.paths,'save_config'):
            handler.do_POST()
        self.assertEqual(cfg['subscription_plans'],{'claude':'auto','codex':'pro_500'})
        self.assertEqual(cfg['subscription_usd_month']['claude'],0)
        self.assertNotIn('gemini',cfg['subscription_plans'])


class BusinessPremiumDetection(unittest.TestCase):
    def test_prolite_workspace_reads_as_business_premium(self):
        from tracker.limits import PLAN_NAMES
        self.assertEqual(PLAN_NAMES['self_serve_business_prolite'], 'Business Premium')
        cfg = {'sources': {'codex': True}, 'subscription_plans': {}, 'subscription_usd_month': {}}
        self.assertEqual(ranking.subscriptions({'codex': {'plan': 'Business Premium'}}, cfg)['codex']['usd'], 125)
