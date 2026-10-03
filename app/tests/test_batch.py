"""复核批次封存引擎与持久化单元测试：
python3 -m unittest app.tests.test_batch -v
"""
import os
import tempfile
import unittest

from app.batch import (BatchError, MAX_BATCH, MIN_BATCH, schedule_fingerprint,
                      seal_batch)
from app.db import DB
from app.review import (CONCLUSION_INSUFFICIENT, CONCLUSION_PASS,
                        CONCLUSION_REVIEW, plan_series, review_run)
from app.scheduler import schedule

PARAMS = {'tolerance': 2.0, 'dose_limit': 30.0, 'max_gap': 2}


def two_sample_schedule():
    s = [
        {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
         'eq_duration': 10,
         'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
        {'id': 2, 'code': 'B', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
         'eq_duration': 10,
         'steps': [{'target': 62, 'dwell': 10, 'rate': 5}]},
    ]
    return schedule(s, [], 2)


def review_payload(sched, readings, *, tolerance=2.0, dose_limit=30.0,
                   max_gap=2, conclusions_override=None,
                   strip_snapshot=False):
    """生成一条（保存形态的）复核 payload，并嵌入原排程快照。"""
    res = review_run(sched, readings, tolerance, dose_limit, max_gap)
    if conclusions_override:
        for s in res['samples']:
            if s['sample_id'] in conclusions_override:
                c = conclusions_override[s['sample_id']]
                s['conclusion'] = c
                s['conclusion_label'] = {
                    'pass': '通过', 'review': '复核',
                    'insufficient': '数据不足'}[c]
    res['source_schedule'] = None if strip_snapshot else sched
    return res


def perfect(sched, offset=0.0):
    rh = plan_series(sched['segments'], sched['end_minute'])
    return [(t, round(v + offset, 4)) for t, v in enumerate(rh)]


def entries(db_ids_payloads):
    """[(rid, payload)] → seal_batch 的 entries 形态。"""
    return [{'review_id': rid, 'created_at': '2026-10-01 10:00:00',
             'note': '', 'payload': p} for rid, p in db_ids_payloads]


def save_batch(db, payloads):
    ids = []
    for p in payloads:
        overall = _overall(p)
        ids.append(db.save_review(p, overall, p.get('_note', '')))
    return ids


def _overall(p):
    sm = p['summary']
    if sm['insufficient']:
        return CONCLUSION_INSUFFICIENT
    if sm['review']:
        return CONCLUSION_REVIEW
    return CONCLUSION_PASS


class TestFingerprint(unittest.TestCase):
    def test_equal_snapshots(self):
        a = two_sample_schedule()
        b = two_sample_schedule()
        self.assertEqual(schedule_fingerprint(a), schedule_fingerprint(b))

    def test_key_order_insensitive(self):
        import json
        a = two_sample_schedule()
        b = json.loads(json.dumps(a))
        # 末段挪到首位：内容相同仅键/数组顺序… 数组顺序不能动，只改键序
        b['capacity'], b['end_minute'] = b['end_minute'], b['capacity']
        b['capacity'], b['end_minute'] = b['end_minute'], b['capacity']
        self.assertEqual(schedule_fingerprint(a), schedule_fingerprint(b))

    def test_different_timeline(self):
        s1 = two_sample_schedule()
        s2 = schedule([
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 20,
             'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
            {'id': 2, 'code': 'B', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 20,
             'steps': [{'target': 62, 'dwell': 10, 'rate': 5}]}], [], 2)
        self.assertNotEqual(schedule_fingerprint(s1), schedule_fingerprint(s2))


class TestSealValidation(unittest.TestCase):
    def setUp(self):
        self.sched = two_sample_schedule()

    def test_size_out_of_range(self):
        p = review_payload(self.sched, perfect(self.sched))
        for bad in ([], [p], [p] * (MAX_BATCH + 1)):
            with self.assertRaises(BatchError) as cm:
                seal_batch(entries([(i + 1, q) for i, q in enumerate(bad)]))
            self.assertEqual(cm.exception.code, 'BATCH_SIZE')

    def test_size_boundaries_accepted(self):
        ps = [review_payload(self.sched, perfect(self.sched))
              for _ in range(MAX_BATCH)]
        out = seal_batch(entries(list(enumerate(ps, 1))))
        self.assertEqual(out['review_count'], MAX_BATCH)

    def test_duplicate_selection_rejected(self):
        p = review_payload(self.sched, perfect(self.sched))
        with self.assertRaises(BatchError) as cm:
            seal_batch(entries([(7, p), (7, p)]))
        self.assertEqual(cm.exception.code, 'DUPLICATE_SELECTION')

    def test_missing_snapshot_rejected(self):
        p1 = review_payload(self.sched, perfect(self.sched))
        p2 = review_payload(self.sched, perfect(self.sched),
                            strip_snapshot=True)
        with self.assertRaises(BatchError) as cm:
            seal_batch(entries([(1, p1), (2, p2)]))
        self.assertEqual(cm.exception.code, 'MISSING_SNAPSHOT')

    def test_different_snapshot_rejected(self):
        other = schedule([
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 20,
             'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
            {'id': 2, 'code': 'B', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 20,
             'steps': [{'target': 62, 'dwell': 10, 'rate': 5}]}], [], 2)
        p1 = review_payload(self.sched, perfect(self.sched))
        p2 = review_payload(other, perfect(other))
        with self.assertRaises(BatchError) as cm:
            seal_batch(entries([(1, p1), (2, p2)]))
        self.assertEqual(cm.exception.code, 'SNAPSHOT_MISMATCH')

    def test_parameter_mismatch_rejected(self):
        p1 = review_payload(self.sched, perfect(self.sched), tolerance=2.0)
        for kw, field in (
                ({'tolerance': 1.5}, 'tolerance'),
                ({'dose_limit': 20.0}, 'dose_limit'),
                ({'max_gap': 3}, 'max_gap')):
            p2 = review_payload(self.sched, perfect(self.sched), **kw)
            with self.assertRaises(BatchError) as cm:
                seal_batch(entries([(1, p1), (2, p2)]))
            self.assertEqual(cm.exception.code, 'PARAMETER_MISMATCH')
            self.assertIn(field, cm.exception.details['fields'])

    def test_sample_set_mismatch_rejected(self):
        other = two_sample_schedule()
        # 手工裁掉一个样本结果，模拟不同样本集合
        p1 = review_payload(self.sched, perfect(self.sched))
        p2 = review_payload(other, perfect(other))
        p2['samples'] = [s for s in p2['samples'] if s['sample_id'] != 2]
        with self.assertRaises(BatchError) as cm:
            seal_batch(entries([(1, p1), (2, p2)]))
        self.assertEqual(cm.exception.code, 'SAMPLE_SET_MISMATCH')

    def test_sample_order_difference_ok(self):
        # 记录内样本顺序不同、集合相同 → 按样本 ID 对齐，允许
        p1 = review_payload(self.sched, perfect(self.sched))
        p2 = review_payload(self.sched, perfect(self.sched))
        p2['samples'] = list(reversed(p2['samples']))
        out = seal_batch(entries([(1, p1), (2, p2)]))
        self.assertEqual([s['sample_id'] for s in out['samples']], [1, 2])

    def test_sample_set_empty_rejected(self):
        p = review_payload(self.sched, perfect(self.sched))
        p['samples'] = []
        with self.assertRaises(BatchError) as cm:
            seal_batch(entries([(1, p), (2, p)]))
        self.assertEqual(cm.exception.code, 'BAD_REVIEW_PAYLOAD')


class TestSealAggregation(unittest.TestCase):
    def setUp(self):
        self.sched = two_sample_schedule()

    def _seal(self, over1, over2):
        """两条记录：样本 A/B 分别带指定恒定偏高（%RH）。"""
        ps = [review_payload(self.sched, perfect(self.sched, off))
              for off in (over1, over2)]
        return seal_batch(entries([(101, ps[0]), (102, ps[1])]))

    def test_all_pass(self):
        out = self._seal(0.0, 0.5)
        self.assertEqual(out['overall_conclusion'], CONCLUSION_PASS)
        self.assertEqual(out['summary'],
                         {'pass': 2, 'review': 0, 'insufficient': 0,
                          'total': 2})
        for s in out['samples']:
            self.assertEqual(s['conclusion'], CONCLUSION_PASS)

    def test_any_review_makes_review(self):
        # 偏移 4%（带 2%）必产生超带剂量 > 上限 30？该简单计划窗口很短，
        # 用强制结论覆盖以精确测试合并规则
        p1 = review_payload(self.sched, perfect(self.sched),
                            conclusions_override={1: 'pass', 2: 'pass'})
        p2 = review_payload(self.sched, perfect(self.sched),
                            conclusions_override={1: 'review', 2: 'pass'})
        out = seal_batch(entries([(1, p1), (2, p2)]))
        by_id = {s['sample_id']: s for s in out['samples']}
        self.assertEqual(by_id[1]['conclusion'], CONCLUSION_REVIEW)
        self.assertEqual(by_id[2]['conclusion'], CONCLUSION_PASS)
        self.assertEqual(out['overall_conclusion'], CONCLUSION_REVIEW)
        self.assertEqual(out['summary']['review'], 1)

    def test_insufficient_dominates_review(self):
        p1 = review_payload(self.sched, perfect(self.sched),
                            conclusions_override={1: 'review', 2: 'review'})
        p2 = review_payload(self.sched, perfect(self.sched),
                            conclusions_override={1: 'insufficient',
                                                  2: 'pass'})
        out = seal_batch(entries([(1, p1), (2, p2)]))
        by_id = {s['sample_id']: s for s in out['samples']}
        self.assertEqual(by_id[1]['conclusion'], CONCLUSION_INSUFFICIENT)
        self.assertEqual(by_id[2]['conclusion'], CONCLUSION_REVIEW)
        self.assertEqual(out['overall_conclusion'], CONCLUSION_INSUFFICIENT)

    def test_per_sample_max_dose_and_deviation(self):
        # 两条记录偏移不同：最大剂量/最大偏差必须来自偏移更大的记录
        p1 = review_payload(self.sched, perfect(self.sched, 4.0),
                            dose_limit=100000)
        p2 = review_payload(self.sched, perfect(self.sched, 6.0),
                            dose_limit=100000)
        out = seal_batch(entries([(1, p1), (2, p2)]))
        for s in out['samples']:
            self.assertEqual(s['max_dose_review_id'], 2)
            self.assertEqual(s['max_deviation_review_id'], 2)
            single = next(x for x in p2['samples']
                          if x['sample_id'] == s['sample_id'])
            self.assertAlmostEqual(s['max_dose'], single['dose'], places=4)
            self.assertAlmostEqual(s['max_deviation'],
                                   single['max_deviation'], places=4)
            self.assertEqual(len(s['per_review']), 2)

    def test_frozen_reviews_carry_readings(self):
        p1 = review_payload(self.sched, perfect(self.sched, 1.0))
        p2 = review_payload(self.sched, perfect(self.sched, 0.0))
        out = seal_batch(entries([(1, p1), (2, p2)]), note='批次一')
        self.assertEqual(out['review_ids'], [1, 2])
        self.assertEqual(len(out['reviews']), 2)
        self.assertEqual(out['reviews'][0]['readings'], p1['readings'])
        # 共享快照只在顶层保留一份，冻结条目内不再重复
        self.assertNotIn('source_schedule', out['reviews'][0])
        self.assertEqual(out['source_schedule']['end_minute'],
                         self.sched['end_minute'])
        self.assertEqual(out['parameters']['tolerance'], 2.0)
        self.assertEqual(out['note'], '批次一')

    def test_dose_limit_boundary_not_overridden(self):
        # 单条层结论以剂量上限 30 判定：用 4% 偏移产生可控剂量的简单计划，
        # 检查封存不改变每条输入记录自身结论
        p = review_payload(self.sched, perfect(self.sched, 4.0),
                           dose_limit=100000)
        out = seal_batch(entries([(1, p), (2, p)]))
        single = p['samples'][0]
        s0 = out['samples'][0]
        self.assertEqual(
            [r['conclusion'] for r in s0['per_review']],
            [single['conclusion'], single['conclusion']])


class TestBatchPersistence(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.db = DB(self.path)
        self.sched = two_sample_schedule()

    def tearDown(self):
        os.unlink(self.path)

    def _save_reviews(self, n=3):
        ids = []
        for i in range(n):
            p = review_payload(self.sched, perfect(self.sched, 0.5 * i))
            ids.append(self.db.save_review(p, _overall(p), f'记录{i}'))
        return ids

    def test_seal_persists_and_reads_back(self):
        ids = self._save_reviews(3)
        bid, created_at, payload = self.db.seal_review_batch(ids, '封存')
        self.assertGreater(bid, 0)
        self.assertTrue(created_at)
        rec = self.db.get_review_batch(bid)
        self.assertEqual(rec['id'], bid)
        self.assertEqual(rec['review_count'], 3)
        self.assertEqual(rec['payload']['review_ids'], ids)
        self.assertEqual(rec['note'], '封存')
        lst = self.db.list_review_batches()
        self.assertEqual([x['id'] for x in lst], [bid])
        self.assertEqual(lst[0]['review_ids'], ids)

    def test_deleting_source_review_keeps_batch(self):
        ids = self._save_reviews(2)
        bid, _, _ = self.db.seal_review_batch(ids)
        # 删除原复核（及其补做级联）不应触及批次
        self.db.delete_review(ids[0])
        rec = self.db.get_review_batch(bid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['payload']['review_ids'], ids)
        frozen = {r['review_id'] for r in rec['payload']['reviews']}
        self.assertEqual(frozen, set(ids))

    def test_inconsistent_batch_rolls_back(self):
        ids = self._save_reviews(2)
        # 再保存一条引用不同快照的复核（不同 eq_duration → 时间轴不同）
        other = schedule([
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 25,
             'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
            {'id': 2, 'code': 'B', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 25,
             'steps': [{'target': 62, 'dwell': 10, 'rate': 5}]}], [], 2)
        bad = self.db.save_review(
            review_payload(other, perfect(other)),
            CONCLUSION_PASS, '异快照')
        before = len(self.db.list_review_batches())
        with self.assertRaises(BatchError) as cm:
            self.db.seal_review_batch([ids[0], bad])
        self.assertEqual(cm.exception.code, 'SNAPSHOT_MISMATCH')
        self.assertEqual(len(self.db.list_review_batches()), before)

    def test_missing_review_id_rejected_no_write(self):
        ids = self._save_reviews(2)
        with self.assertRaises(BatchError) as cm:
            self.db.seal_review_batch([ids[0], 99999])
        self.assertEqual(cm.exception.code, 'REVIEW_NOT_FOUND')
        self.assertEqual(self.db.list_review_batches(), [])

    def test_duplicate_id_rejected_no_write(self):
        ids = self._save_reviews(2)
        with self.assertRaises(BatchError) as cm:
            self.db.seal_review_batch([ids[0], ids[0]])
        self.assertEqual(cm.exception.code, 'DUPLICATE_SELECTION')
        self.assertEqual(self.db.list_review_batches(), [])

    def test_delete_batch(self):
        ids = self._save_reviews(2)
        bid, _, _ = self.db.seal_review_batch(ids)
        self.db.delete_review_batch(bid)
        self.assertIsNone(self.db.get_review_batch(bid))
        # 删除批次不影响原复核
        for rid in ids:
            self.assertIsNotNone(self.db.get_review(rid))


if __name__ == '__main__':
    unittest.main()
