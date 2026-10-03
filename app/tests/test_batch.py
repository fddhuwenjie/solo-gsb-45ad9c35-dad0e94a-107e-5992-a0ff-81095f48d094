"""复核批次封存引擎与事务单元测试：
python3 -m unittest app.tests.test_batch -v
"""
import os
import tempfile
import unittest

from app.scheduler import schedule
from app.review import plan_series, review_run
from app.batch import (seal_batch, snapshot_fingerprint, BatchError,
                       MIN_RECORDS, MAX_RECORDS)
from app.db import DB


def two_sample_schedule():
    """容量 1：A、B 先后串行在箱（窗口不重叠），便于按样本制造不同结论。"""
    s = [
        {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
         'eq_duration': 10,
         'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
        {'id': 2, 'code': 'B', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
         'eq_duration': 10,
         'steps': [{'target': 62, 'dwell': 10, 'rate': 5}]},
    ]
    res = schedule(s, [], 1)
    assert res['feasible']
    return res


def lane_of(sched, sid):
    return next(l for l in sched['lanes'] if l['sample_id'] == sid)


def make_readings(sched, offset=None, drop=None):
    """按计划曲线生成每分钟读数；offset(t) 给偏差，drop(t) 为真则丢弃该点。"""
    rh = plan_series(sched['segments'], sched['end_minute'])
    out = []
    for t, v in enumerate(rh):
        if drop and drop(t):
            continue
        out.append((t, round(v + (offset(t) if offset else 0.0), 4)))
    return out


def make_record(rid, sched, readings, tol=2.0, dose=1.0, gap=2,
                note='', with_snapshot=True, created=None):
    res = review_run(sched, readings, tol, dose, gap)
    if with_snapshot:
        res['source_schedule'] = sched
    sm = res['summary']
    overall = ('insufficient' if sm['insufficient']
               else 'review' if sm['review'] else 'pass')
    return {'id': rid,
            'created_at': created or f'2026-10-0{rid} 08:00:00',
            'conclusion': overall, 'note': note, 'payload': res}


def gap_in_window(sched, sid, length=5):
    """丢弃某样本窗口内的读数点，制造超过 max_gap=2 的采样缺口。"""
    lane = lane_of(sched, sid)
    lo, hi = lane['board'] + 1, lane['board'] + 1 + length
    assert hi < lane['unboard']
    return lambda t: lo <= t <= hi


def offset_in_window(sched, sid, delta):
    lane = lane_of(sched, sid)
    return lambda t: delta if lane['board'] <= t <= lane['unboard'] else 0.0


class TestSealValidation(unittest.TestCase):
    def setUp(self):
        self.sched = two_sample_schedule()

    def test_count_bounds(self):
        rec = make_record(1, self.sched, make_readings(self.sched))
        with self.assertRaises(BatchError):
            seal_batch([rec])
        recs = [make_record(i, self.sched, make_readings(self.sched))
                for i in range(1, MAX_RECORDS + 2)]
        with self.assertRaises(BatchError):
            seal_batch(recs)
        # 边界：恰好 MIN/MAX 条可通过
        ok = [make_record(i, self.sched, make_readings(self.sched))
              for i in range(1, MIN_RECORDS + 1)]
        self.assertEqual(seal_batch(ok)['record_count'], MIN_RECORDS)
        ok = [make_record(i, self.sched, make_readings(self.sched))
              for i in range(1, MAX_RECORDS + 1)]
        self.assertEqual(seal_batch(ok)['record_count'], MAX_RECORDS)

    def test_duplicate_selection_rejected(self):
        rec = make_record(1, self.sched, make_readings(self.sched))
        rec2 = make_record(2, self.sched, make_readings(self.sched))
        with self.assertRaisesRegex(BatchError, '重复'):
            seal_batch([rec, rec2, rec])

    def test_missing_snapshot_rejected(self):
        rec1 = make_record(1, self.sched, make_readings(self.sched))
        rec2 = make_record(2, self.sched, make_readings(self.sched),
                           with_snapshot=False)
        with self.assertRaisesRegex(BatchError, '快照'):
            seal_batch([rec1, rec2])

    def test_different_snapshot_rejected(self):
        other = two_sample_schedule()
        other['end_minute'] += 1  # 任何差异都会改变指纹
        rec1 = make_record(1, self.sched, make_readings(self.sched))
        rec2 = make_record(2, self.sched, make_readings(self.sched))
        rec2['payload']['source_schedule'] = other
        with self.assertRaisesRegex(BatchError, '快照不一致'):
            seal_batch([rec1, rec2])

    def test_parameter_mismatch_rejected(self):
        base = make_readings(self.sched)
        for kw in ({'tol': 3.0}, {'dose': 5.0}, {'gap': 3}):
            rec1 = make_record(1, self.sched, base)
            rec2 = make_record(2, self.sched, base, **kw)
            with self.assertRaisesRegex(BatchError, '参数'):
                seal_batch([rec1, rec2])

    def test_sample_set_mismatch_rejected(self):
        rec1 = make_record(1, self.sched, make_readings(self.sched))
        rec2 = make_record(2, self.sched, make_readings(self.sched))
        # 同一快照，但人为删掉一个样本结果 → 样本集合不一致
        rec2['payload']['samples'] = rec2['payload']['samples'][:1]
        with self.assertRaisesRegex(BatchError, '样本集合'):
            seal_batch([rec1, rec2])

    def test_unknown_conclusion_rejected(self):
        rec1 = make_record(1, self.sched, make_readings(self.sched))
        rec2 = make_record(2, self.sched, make_readings(self.sched))
        rec2['payload']['samples'][0]['conclusion'] = 'bogus'
        with self.assertRaises(BatchError):
            seal_batch([rec1, rec2])

    def test_missing_record_payload_rejected(self):
        rec1 = make_record(1, self.sched, make_readings(self.sched))
        with self.assertRaises(BatchError):
            seal_batch([rec1, {'id': 2, 'payload': None}])


class TestSealAlignment(unittest.TestCase):
    def setUp(self):
        self.sched = two_sample_schedule()

    def test_all_pass(self):
        recs = [make_record(i, self.sched, make_readings(self.sched))
                for i in (1, 2)]
        out = seal_batch(recs, note='两次夜间实测')
        self.assertEqual(out['overall_conclusion'], 'pass')
        self.assertEqual(out['summary'],
                         {'pass': 2, 'review': 0, 'insufficient': 0,
                          'total': 2})
        self.assertEqual(out['review_ids'], [1, 2])
        self.assertEqual(out['note'], '两次夜间实测')
        self.assertEqual(out['parameters'],
                         {'tolerance': 2.0, 'dose_limit': 1.0, 'max_gap': 2})
        self.assertEqual(out['schedule_fingerprint'],
                         snapshot_fingerprint(self.sched))
        for s in out['samples']:
            self.assertEqual(s['conclusion'], 'pass')
            self.assertEqual(s['max_dose'], 0)
            self.assertEqual(len(s['per_record']), 2)

    def test_alignment_priority_and_maxima(self):
        # 记录1：A 数据不足（窗口内采样缺口），B 通过
        rec1 = make_record(
            1, self.sched,
            make_readings(self.sched, drop=gap_in_window(self.sched, 1)))
        # 记录2：A 通过，B 复核（窗口内 +5% 偏差，剂量上限 1.0）
        rec2 = make_record(
            2, self.sched,
            make_readings(self.sched,
                          offset=offset_in_window(self.sched, 2, 5.0)))
        # 记录3：全部通过
        rec3 = make_record(3, self.sched, make_readings(self.sched))
        self.assertEqual(rec1['payload']['samples'][0]['conclusion'],
                         'insufficient')
        self.assertEqual(rec2['payload']['samples'][1]['conclusion'],
                         'review')

        out = seal_batch([rec1, rec2, rec3])
        self.assertEqual(out['overall_conclusion'], 'insufficient')
        self.assertEqual(out['summary'],
                         {'pass': 0, 'review': 1, 'insufficient': 1,
                          'total': 2})
        sa, sb = out['samples']
        # A：任一数据不足 → 数据不足
        self.assertEqual(sa['conclusion'], 'insufficient')
        self.assertEqual([p['conclusion'] for p in sa['per_record']],
                         ['insufficient', 'pass', 'pass'])
        # B：无数据不足、任一复核 → 复核
        self.assertEqual(sb['conclusion'], 'review')
        self.assertEqual([p['conclusion'] for p in sb['per_record']],
                         ['pass', 'review', 'pass'])
        # 保留跨记录最大剂量与最大偏差（B 在记录2 中超限）
        dose2 = rec2['payload']['samples'][1]['dose']
        dev2 = rec2['payload']['samples'][1]['max_deviation']
        self.assertGreater(dose2, 1.0)
        self.assertEqual(sb['max_dose'], dose2)
        self.assertEqual(sb['max_deviation'], dev2)
        self.assertAlmostEqual(sb['max_deviation'], 5.0, places=3)
        # 每条输入记录的逐样本明细都保留
        self.assertEqual([p['review_id'] for p in sb['per_record']],
                         [1, 2, 3])
        # 输入记录元信息保留
        self.assertEqual([r['review_id'] for r in out['records']], [1, 2, 3])
        self.assertEqual(out['records'][0]['conclusion'], 'insufficient')

    def test_review_when_no_insufficient(self):
        rec1 = make_record(
            1, self.sched,
            make_readings(self.sched,
                          offset=offset_in_window(self.sched, 1, 5.0)))
        rec2 = make_record(2, self.sched, make_readings(self.sched))
        out = seal_batch([rec1, rec2])
        self.assertEqual(out['overall_conclusion'], 'review')
        self.assertEqual(out['samples'][0]['conclusion'], 'review')
        self.assertEqual(out['samples'][1]['conclusion'], 'pass')


class TestSealTransaction(unittest.TestCase):
    """DB 层：一次事务校验+落库；失败整批拒绝不留痕；批次独立于原复核。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        os.unlink(self.path)
        self.db = DB(self.path)
        self.sched = two_sample_schedule()

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def save_review(self, sched, readings, note='', **kw):
        res = review_run(sched, readings,
                         kw.get('tol', 2.0), kw.get('dose', 1.0),
                         kw.get('gap', 2))
        sm = res['summary']
        overall = ('insufficient' if sm['insufficient']
                   else 'review' if sm['review'] else 'pass')
        res['source_schedule'] = sched
        return self.db.save_review(res, overall, note)

    def test_seal_persists_immutable_batch(self):
        r1 = self.save_review(self.sched, make_readings(self.sched),
                              note='第一次')
        r2 = self.save_review(self.sched, make_readings(self.sched),
                              note='第二次')
        bid, payload = self.db.seal_review_batch([r1, r2], note='周汇总')
        self.assertEqual(payload['review_ids'], [r1, r2])
        rec = self.db.get_batch(bid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['conclusion'], 'pass')
        self.assertEqual(rec['note'], '周汇总')
        self.assertEqual(rec['review_ids'], [r1, r2])
        self.assertEqual(rec['payload']['record_count'], 2)
        listed = self.db.list_batches()
        self.assertEqual([b['id'] for b in listed], [bid])
        self.assertEqual(listed[0]['summary']['total'], 2)

    def test_rejection_leaves_no_trace(self):
        r1 = self.save_review(self.sched, make_readings(self.sched))
        r2 = self.save_review(self.sched, make_readings(self.sched), gap=5)
        before = self.db.list_batches()
        with self.assertRaisesRegex(BatchError, '参数'):
            self.db.seal_review_batch([r1, r2])
        self.assertEqual(self.db.list_batches(), before)

    def test_missing_record_rejected_atomically(self):
        r1 = self.save_review(self.sched, make_readings(self.sched))
        with self.assertRaisesRegex(BatchError, '不存在'):
            self.db.seal_review_batch([r1, 999])
        self.assertEqual(self.db.list_batches(), [])

    def test_delete_source_reviews_keeps_batch(self):
        r1 = self.save_review(self.sched, make_readings(self.sched))
        r2 = self.save_review(self.sched, make_readings(self.sched))
        bid, _ = self.db.seal_review_batch([r1, r2])
        # 删除原复核（会级联删补做）不影响已封存批次
        self.db.delete_review(r1)
        self.db.delete_review(r2)
        self.assertEqual(self.db.list_reviews(), [])
        rec = self.db.get_batch(bid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['payload']['review_ids'], [r1, r2])
        self.assertEqual(len(rec['payload']['records']), 2)
        self.assertEqual(len(rec['payload']['samples']), 2)

    def test_delete_batch_keeps_reviews(self):
        r1 = self.save_review(self.sched, make_readings(self.sched))
        r2 = self.save_review(self.sched, make_readings(self.sched))
        bid, _ = self.db.seal_review_batch([r1, r2])
        self.db.delete_batch(bid)
        self.assertEqual(self.db.list_batches(), [])
        self.assertIsNone(self.db.get_batch(bid))
        self.assertEqual(len(self.db.list_reviews()), 2)


if __name__ == '__main__':
    unittest.main()
