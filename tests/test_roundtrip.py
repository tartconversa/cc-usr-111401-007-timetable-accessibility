"""验证按出发时刻的往返可达性分析：配对语义、损失归因、抽样误差界与增量更新。"""
import copy
import json
from pathlib import Path
import unittest
from app.catalog import load
from app.service import SCHEMAS
from app import reach
from app.reach_service import inspect_roundtrip, update_roundtrip

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / 'roundtrip.json').read_text(encoding='utf-8'))
Q1 = {'depart_range': [420, 960], 'arrive_window': [470, 940], 'min_stay': 30, 'budget': 300}


def evaluate(payload, query):
    snapshot = load(payload, SCHEMAS)
    return reach.evaluate(reach.build_network(snapshot), query)


class ExactEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = evaluate(FIXTURE, Q1)

    def test_feasible_intervals_are_exact_union_not_best_single_trip(self):
        o1 = self.result['opportunities']['o1']
        self.assertEqual(o1['status'], 'feasible')
        # 两段互不相交的可行区间：单一"全天最短行程"无法表达
        self.assertEqual(o1['feasible_intervals'], [[420, 470], [640, 738]])
        self.assertEqual(o1['feasible_minutes'], 51 + 99)
        self.assertAlmostEqual(o1['feasible_fraction'], 150 / 541)

    def test_loss_attribution_per_minute(self):
        o1 = self.result['opportunities']['o1']
        # [471,639]与[739,830]有去程也有回程但预算装不下；[831,960]已无任何去程
        self.assertEqual(o1['lost_minutes'], {'no_outbound': 130, 'over_budget': 261})

    def test_status_taxonomy(self):
        opps = self.result['opportunities']
        self.assertEqual(opps['o2']['status'], 'closed')        # 停留窗口装不下最短停留
        self.assertEqual(opps['o3']['status'], 'unreachable')   # 无任何路径
        self.assertEqual(opps['o6']['status'], 'one_way')       # 能去不能回
        self.assertEqual(opps['o5']['status'], 'missed')        # 双向都有资源但配对失败
        self.assertEqual(opps['o2']['lost_minutes'], {'closed': 541})
        self.assertEqual(opps['o3']['lost_minutes'], {'unreachable': 541})
        self.assertEqual(opps['o6']['lost_minutes'], {'one_way': 541})

    def test_walk_only_round_trip(self):
        o4 = self.result['opportunities']['o4']
        self.assertEqual(o4['status'], 'feasible')
        self.assertEqual(o4['feasible_intervals'], [[420, 915]])
        self.assertEqual(o4['lost_minutes'], {'no_outbound': 45})

    def test_transfer_rule_and_walk_does_not_reset_boarding(self):
        arrivals = {opt['arrive'] for opt in self.result['opportunities']['o1']['options']['outbound']}
        # t6(860到hub)接t7(870发)满足min_transfer=5；t8(863发)间隔3分钟不可换乘
        self.assertIn(890, arrivals)
        self.assertNotIn(883, arrivals)
        transfer_opt = next(opt for opt in self.result['opportunities']['o1']['options']['outbound']
                            if opt['arrive'] == 890)
        self.assertEqual(transfer_opt['transfers'], 1)
        self.assertEqual(transfer_opt['trips'], ['t6', 't7'])

    def test_witness_composition(self):
        witness = self.result['opportunities']['o1']['witnesses'][0]
        self.assertEqual(witness['interval'], [420, 470])
        self.assertEqual(witness['depart_at'], 470)
        self.assertEqual([leg['kind'] for leg in witness['outbound']['legs']], ['walk', 'trip'])
        self.assertEqual(witness['outbound']['legs'][1]['trip'], 't1')
        self.assertEqual(witness['return']['legs'][1]['trip'], 't2')
        totals = witness['totals']
        self.assertEqual((totals['walk_minutes'], totals['wait_minutes'], totals['ride_minutes']),
                         (20, 60, 60))
        self.assertEqual(totals['transfers'], 0)
        self.assertEqual(totals['total_minutes'], 170)
        self.assertEqual(totals['budget_slack'], 130)

    def test_same_opportunity_counted_once_across_stations(self):
        summary = self.result['summary']
        # o1经stA与stB多条路径汇入，可行分钟取并集、机会只计一次
        self.assertEqual(summary['opportunities_feasible'], 2)
        self.assertEqual(summary['weight_feasible'], 15)
        self.assertEqual(summary['any_feasible_minutes'], 496)
        self.assertEqual(summary['status_counts'],
                         {'feasible': 2, 'closed': 1, 'unreachable': 1, 'missed': 1, 'one_way': 1})

    def test_provenance_binding(self):
        self.assertEqual(self.result['data_version'], 'rt-1')
        self.assertEqual(len(self.result['input_digest']), 64)
        self.assertEqual(len(self.result['query_digest']), 64)


class CrossMidnightTests(unittest.TestCase):
    def test_trips_beyond_1440_and_day_offsets(self):
        query = {'depart_range': [1380, 1440], 'arrive_window': [1440, 1475],
                 'min_stay': 30, 'budget': 200, 'opportunities': ['o5']}
        o5 = evaluate(FIXTURE, query)['opportunities']['o5']
        self.assertEqual(o5['status'], 'feasible')
        self.assertEqual(o5['feasible_intervals'], [[1380, 1435]])
        self.assertEqual(o5['lost_minutes'], {'no_outbound': 5})
        witness = o5['witnesses'][0]
        # 去程t9超过1440（跨午夜），回程t10在d1服务日（偏移1440）
        self.assertEqual(witness['outbound']['legs'][1]['depart'], 1445)
        self.assertEqual(witness['return']['legs'][1]['trip'], 't10')
        self.assertEqual(witness['return']['legs'][1]['depart'], 1530)
        self.assertEqual(witness['totals']['total_minutes'], 135)


class ImpossiblePairingTests(unittest.TestCase):
    def test_best_outbound_and_best_return_cannot_be_stitched(self):
        # 唯一去程890到（ready 920），唯一可及回程t10（1530发，1570回城）：
        # 分别看都"存在"，但预算400内拼不成往返
        query = {'depart_range': [820, 960], 'arrive_window': [880, 900],
                 'min_stay': 30, 'budget': 400, 'opportunities': ['o1']}
        o1 = evaluate(FIXTURE, query)['opportunities']['o1']
        self.assertEqual(o1['status'], 'missed')
        self.assertEqual(o1['feasible_minutes'], 0)
        self.assertEqual(o1['lost_minutes'], {'no_outbound': 130, 'over_budget': 11})

    def test_tight_budget_turns_feasible_into_missed(self):
        query = {**Q1, 'budget': 120, 'opportunities': ['o1']}
        o1 = evaluate(FIXTURE, query)['opportunities']['o1']
        self.assertEqual(o1['status'], 'missed')
        self.assertEqual(o1['lost_minutes'], {'no_outbound': 130, 'over_budget': 411})

    def test_return_before_stay_ends_is_missed_return(self):
        payload = {
            'schema_version': 1,
            'metadata': {'data_version': 'mini', 'origin': 'home',
                         'day_offsets': {'d0': 0}, 'min_transfer': 5},
            'collections': {
                'walks': [{'id': 'w1', 'revision': 1, 'from': 'home', 'to': 'st', 'minutes': 5},
                          {'id': 'w2', 'revision': 1, 'from': 'st', 'to': 'home', 'minutes': 5}],
                'trips': [{'id': 'tb', 'revision': 1, 'service_day': 'd0',
                           'stops': [{'node': 'st', 'arrive': 100, 'depart': 100},
                                     {'node': 'site2', 'arrive': 130, 'depart': 130}]},
                          {'id': 'tc', 'revision': 1, 'service_day': 'd0',
                           'stops': [{'node': 'site2', 'arrive': 150, 'depart': 150},
                                     {'node': 'st', 'arrive': 180, 'depart': 180}]}],
                'opportunities': [{'id': 'ox', 'revision': 1, 'node': 'site2',
                                   'open': 0, 'close': 500, 'weight': 1}]}}
        query = {'depart_range': [0, 300], 'arrive_window': [100, 200],
                 'min_stay': 30, 'budget': 500}
        ox = evaluate(payload, query)['opportunities']['ox']
        self.assertEqual(ox['status'], 'missed')
        # 回程唯一班次在停留结束前已发车：前96分钟为回程漏车，之后连去程都没有
        self.assertEqual(ox['lost_minutes'], {'missed_return': 96, 'no_outbound': 205})


class WalkOutTransitBackTests(unittest.TestCase):
    def test_walk_out_pairs_with_transit_return(self):
        payload = {
            'schema_version': 1,
            'metadata': {'data_version': 'mini2', 'origin': 'home',
                         'day_offsets': {'d0': 0}, 'min_transfer': 5},
            'collections': {
                'walks': [{'id': 'w1', 'revision': 1, 'from': 'home', 'to': 'st', 'minutes': 5},
                          {'id': 'w2', 'revision': 1, 'from': 'st', 'to': 'home', 'minutes': 5},
                          {'id': 'w3', 'revision': 1, 'from': 'home', 'to': 'site', 'minutes': 50}],
                'trips': [{'id': 'ta', 'revision': 1, 'service_day': 'd0',
                           'stops': [{'node': 'site', 'arrive': 600, 'depart': 600},
                                     {'node': 'st', 'arrive': 630, 'depart': 630}]}],
                'opportunities': [{'id': 'ox', 'revision': 1, 'node': 'site',
                                   'open': 100, 'close': 800, 'weight': 1}]}}
        query = {'depart_range': [0, 600], 'arrive_window': [90, 500],
                 'min_stay': 30, 'budget': 400}
        ox = evaluate(payload, query)['opportunities']['ox']
        self.assertEqual(ox['status'], 'feasible')
        # 步行去程t∈[40,450]；回程班车600发、635回城，预算400要求t≥235
        self.assertEqual(ox['feasible_intervals'], [[235, 450]])
        self.assertEqual(ox['lost_minutes'], {'no_outbound': 190, 'over_budget': 195})
        witness = ox['witnesses'][0]
        self.assertEqual(witness['outbound']['legs'][0]['kind'], 'walk')
        self.assertIsNone(witness['outbound']['latest_depart'])
        self.assertEqual(witness['return']['legs'][1]['trip'], 'ta')


class SamplingTests(unittest.TestCase):
    def test_sampled_fraction_carries_error_bounds(self):
        snapshot = load(FIXTURE, SCHEMAS)
        net = reach.build_network(snapshot)
        sampled = reach.evaluate_sample(net, Q1, 60)
        exact = reach.evaluate(net, Q1)
        self.assertEqual(sampled['method'], 'sampled_approximation')
        o1 = sampled['opportunities']['o1']
        self.assertEqual(o1['sample_count'], 10)
        lo, hi = o1['fraction_bounds']
        truth = exact['opportunities']['o1']['feasible_fraction']
        self.assertLessEqual(lo, truth)
        self.assertLessEqual(truth, hi)
        self.assertLess(lo, hi)  # 抽样不能冒充精确覆盖率
        self.assertGreater(o1['unresolved_minutes'], 0)
        self.assertLessEqual(lo, o1['estimate_fraction'])
        self.assertLessEqual(o1['estimate_fraction'], hi)


class UpdateTests(unittest.TestCase):
    def test_cancelled_trip_revalidates_only_affected_opportunities(self):
        old_result = inspect_roundtrip(FIXTURE, Q1)
        before = copy.deepcopy(old_result)
        cancelled = copy.deepcopy(FIXTURE)
        cancelled['collections']['trips'].append({'id': 't2', 'revision': 2, 'deleted': True})
        cancelled['metadata']['data_version'] = 'rt-2'
        outcome = update_roundtrip(FIXTURE, cancelled, Q1)
        scope = outcome['update_scope']
        self.assertFalse(scope['full_recompute'])
        self.assertEqual(outcome['diff']['trips']['removed'], ['t2'])
        # 缓存行程引用t2的机会被圈定；o3无路径、o6不引用t2，不在范围内
        self.assertTrue({'o1', 'o2', 'o4', 'o5'} <= set(scope['opportunities']))
        self.assertNotIn('o3', scope['opportunities'])
        self.assertNotIn('o6', scope['opportunities'])
        merged = outcome['result']
        # 取消中午回程后，o1只剩[640,738]；o4失去绕环行程退回纯步行
        self.assertEqual(merged['opportunities']['o1']['feasible_intervals'], [[640, 738]])
        self.assertEqual(merged['opportunities']['o4']['feasible_minutes'], 471)
        self.assertNotEqual(merged['input_digest'], old_result['input_digest'])
        # 固定资料版本的旧结果不被后来的停运记录改变
        self.assertEqual(old_result, before)
        self.assertEqual(inspect_roundtrip(FIXTURE, Q1), before)

    def test_station_closure_scope_and_reevaluation(self):
        closed = copy.deepcopy(FIXTURE)
        closed['collections']['walks'].append({'id': 'w1', 'revision': 2, 'deleted': True})
        outcome = update_roundtrip(FIXTURE, closed, Q1)
        scope = outcome['update_scope']['opportunities']
        # 步行边w1(city→stA)被撤回：引用它的机会全部圈定，o3不受影响
        self.assertNotIn('o3', scope)
        self.assertIn('o1', scope)
        # 封站后o1只能经stB乘t5：可行区间收敛为[640,738]
        self.assertEqual(outcome['result']['opportunities']['o1']['feasible_intervals'],
                         [[640, 738]])

    def test_metadata_change_forces_full_recompute(self):
        changed = copy.deepcopy(FIXTURE)
        changed['metadata']['min_transfer'] = 8
        snapshot_old = load(FIXTURE, SCHEMAS)
        diff = reach.diff_snapshots(snapshot_old, load(changed, SCHEMAS))
        net = reach.build_network(snapshot_old)
        scope = reach.plan_update(net, diff, old_result=evaluate(FIXTURE, Q1))
        self.assertTrue(scope['full_recompute'])

    def test_added_service_only_revisits_not_fully_feasible(self):
        query = {**Q1, 'depart_range': [445, 915]}
        old_result = evaluate(FIXTURE, query)
        self.assertEqual(old_result['opportunities']['o4']['feasible_minutes'],
                         old_result['summary']['range_minutes'])  # o4已完全可行
        extended = copy.deepcopy(FIXTURE)
        new_trip = {'id': 't12', 'revision': 1, 'service_day': 'd0',
                    'stops': [{'node': 'stA', 'arrive': 500, 'depart': 500},
                              {'node': 'vill', 'arrive': 530, 'depart': 530}]}
        extended['collections']['trips'].append(new_trip)
        snapshot_old = load(FIXTURE, SCHEMAS)
        diff = reach.diff_snapshots(snapshot_old, load(extended, SCHEMAS))
        net = reach.build_network(snapshot_old)
        scope = reach.plan_update(net, diff, old_result=old_result)
        # 新增班次只可能改善未完全可行的机会；o4已完全可行，不在重估范围
        self.assertNotIn('o4', scope['opportunities'])
        self.assertIn('o1', scope['opportunities'])

    def test_removed_opportunity_dropped_not_reevaluated(self):
        removed = copy.deepcopy(FIXTURE)
        removed['collections']['opportunities'].append(
            {'id': 'o6', 'revision': 2, 'deleted': True})
        removed['collections']['trips'].append({'id': 't2', 'revision': 2, 'deleted': True})
        outcome = update_roundtrip(FIXTURE, removed, Q1)
        scope = outcome['update_scope']
        self.assertEqual(scope['removed_opportunities'], ['o6'])
        self.assertNotIn('o6', scope['opportunities'])
        self.assertNotIn('o6', outcome['result']['opportunities'])
        self.assertEqual(outcome['result']['summary']['opportunities_total'], 5)

    def test_merge_rejects_mismatched_query(self):
        old_result = evaluate(FIXTURE, Q1)
        other = evaluate(FIXTURE, {**Q1, 'budget': 120})
        with self.assertRaisesRegex(ValueError, '查询不一致'):
            reach.merge_results(old_result, other)


class RobustnessTests(unittest.TestCase):
    def test_network_reused_across_batch_queries(self):
        snapshot = load(FIXTURE, SCHEMAS)
        net = reach.build_network(snapshot)
        first = reach.evaluate(net, Q1)
        stats = net.cache_stats()
        self.assertGreater(stats['outbound_options'], 0)
        second = reach.evaluate(net, {**Q1, 'budget': 120})
        self.assertEqual(net.cache_stats(), stats)  # 缓存复用，未重复枚举
        self.assertEqual(first['opportunities']['o1']['feasible_minutes'], 150)
        self.assertEqual(second['opportunities']['o1']['feasible_minutes'], 0)

    def test_evaluation_is_deterministic(self):
        self.assertEqual(evaluate(FIXTURE, Q1), evaluate(FIXTURE, Q1))

    def test_query_validation(self):
        snapshot = load(FIXTURE, SCHEMAS)
        net = reach.build_network(snapshot)
        with self.assertRaisesRegex(ValueError, '起点不应晚于终点'):
            reach.evaluate(net, {**Q1, 'depart_range': [500, 400]})
        with self.assertRaisesRegex(ValueError, 'min_stay'):
            reach.evaluate(net, {'depart_range': [0, 10], 'arrive_window': [0, 10], 'budget': 60})
        with self.assertRaisesRegex(ValueError, '未知机会'):
            reach.evaluate(net, {**Q1, 'opportunities': ['nope']})
        with self.assertRaisesRegex(ValueError, 'budget'):
            reach.evaluate(net, {**Q1, 'budget': 0})

    def test_data_validation(self):
        bad = copy.deepcopy(FIXTURE)
        bad['collections']['trips'][0]['service_day'] = 'dX'
        with self.assertRaisesRegex(ValueError, 'day_offsets'):
            reach.build_network(load(bad, SCHEMAS))
        bad2 = copy.deepcopy(FIXTURE)
        bad2['collections']['trips'][0]['stops'][1]['arrive'] = 400
        with self.assertRaisesRegex(ValueError, '不减'):
            reach.build_network(load(bad2, SCHEMAS))


if __name__ == '__main__':
    unittest.main()
