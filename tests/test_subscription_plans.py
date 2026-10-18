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


class DetectedPlanPrices(unittest.TestCase):
    def test_every_option_name_and_id_shares_its_price(self):
        for source, options in ranking.PLAN_OPTIONS.items():
            for plan_id, (name, price) in options.items():
                for detected in (plan_id, name, '  ' + name.swapcase() + '  ·abcd',
                                 name.replace(' ', '  ')):
                    with self.subTest(source=source, detected=detected):
                        cfg = {'sources': {source: True}, 'subscription_plans': {source: 'auto'}}
                        result = ranking.subscriptions({source: {'plan': detected}}, cfg)[source]
                        self.assertEqual(result['usd'], price)

    def test_common_detected_aliases(self):
        cases = [('claude', 'Max 5x', 100), ('claude', 'MAX 20X·a123', 200),
                 ('claude', 'Team Standard', 25), ('codex', 'Pro 100', 100),
                 ('codex', 'Business Standard', 25), ('codex', 'Team', 25),
                 ('codex', 'Business Premium', 125), ('codex', 'Pro', 200),
                 ('gemini', 'Google AI Pro', 19.99), ('gemini', 'g1-pro-tier', 19.99),
                 ('gemini', 'Google AI Ultra', 249.99), ('gemini', 'Ultra 5x', 99.99),
                 ('gemini', 'Pro (annual)', 199.99 / 12), ('gemini', 'AI Pro', 19.99),
                 ('claude', 'Claude Team Standard', 25)]
        for source, plan, price in cases:
            with self.subTest(source=source, plan=plan):
                cfg = {'sources': {source: True}}
                result = ranking.subscriptions({source: {'plan': plan}}, cfg)[source]
                self.assertEqual(result['usd'], price)
                self.assertEqual(result['from'], 'plan')

    def test_gemini_can_use_antigravity_detected_plan(self):
        result = ranking.subscriptions({'antigravity': {'plan': 'Google AI Pro'}},
                                       {'sources': {'antigravity': True}})
        self.assertEqual(result['gemini']['usd'], 19.99)

    def test_unknown_and_free_detection_do_not_guess_prices(self):
        for source, plan in [('claude', 'Enterprise'), ('codex', 'Go'),
                             ('codex', 'Business Lite'), ('gemini', 'standard-tier'),
                             ('gemini', None), ('claude', 'Pro enterprise custom')]:
            with self.subTest(source=source, plan=plan):
                result = ranking.subscriptions({source: {'plan': plan}}, {'sources': {source: True}})
                self.assertIsNone(result[source]['usd'])
        self.assertEqual(ranking.subscriptions({'codex': {'plan': 'Free'}},
                                              {'sources': {'codex': True}})['codex']['usd'], 0)

    def test_multi_account_detection_keeps_individual_prices(self):
        result = ranking.codex_subscriptions({'codex': {'accounts': [
            {'account': 'a', 'plan': 'Pro 100 ·aaaa'},
            {'account': 'b', 'plan': 'Business Standard·bbbb'}]}}, {'sources': {'codex': True}})
        self.assertEqual([r['usd'] for r in result], [100, 25])


class BusinessPremiumDetection(unittest.TestCase):
    def test_prolite_workspace_reads_as_business_premium(self):
        from tracker.limits import PLAN_NAMES
        self.assertEqual(PLAN_NAMES['self_serve_business_prolite'], 'Business Premium')
        cfg = {'sources': {'codex': True}, 'subscription_plans': {}, 'subscription_usd_month': {}}
        self.assertEqual(ranking.subscriptions({'codex': {'plan': 'Business Premium'}}, cfg)['codex']['usd'], 125)
