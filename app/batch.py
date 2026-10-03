"""复核批次封存引擎（纯标准库，无 Flask 依赖，便于单测）。

操作员选择 2～8 条**已保存**复核记录封存为一个不可变批次。封存在一次
事务内完成以下确认，任一不满足即整批拒绝、不落库：

1. 每条记录都嵌入了同一份**原排程快照**（``source_schedule``，按规范化
   JSON 指纹比对，而不是按排程 id——快照本身不带 id）；
2. 允许偏差、累计偏差剂量上限、最大采样间隔三项参数逐条一致；
3. 各记录复核的**样本集合相同**（按 ``sample_id`` 多重集合比对，与顺序无关）。

确认通过后按样本 ID 对齐各条记录的结论：

* 任一条记录对该样本为「数据不足」→ 批次样本结论为「数据不足」；
* 否则任一条为「复核」→「复核」；
* 全部「通过」→「通过」。

批次总体结论取全部样本结论中最严重者（数据不足 > 复核 > 通过）。
每个样本保留其跨记录的**最大剂量**与**最大偏差**及其来源记录；每条输入
记录的读数、缺口、逐样本明细均原样冻结进批次，因此删除原复核记录不影响
批次回看与导出。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .review import (CONCLUSION_INSUFFICIENT, CONCLUSION_LABELS,
                     CONCLUSION_PASS, CONCLUSION_REVIEW)

MIN_BATCH = 2
MAX_BATCH = 8

# 结论严重度：数据不足 > 复核 > 通过
_RANK = {CONCLUSION_PASS: 0, CONCLUSION_REVIEW: 1,
         CONCLUSION_INSUFFICIENT: 2}


class BatchError(Exception):
    """批次封存输入不满足一致性约束；携带机器可读 code。"""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# --------------------------------------------------------------------------- #
# 原排程快照指纹
# --------------------------------------------------------------------------- #
def schedule_fingerprint(schedule: Any) -> str:
    """原排程快照的规范化 SHA-256 指纹。

    快照是确定性引擎的 JSON 输出（不含排程 id / 时间戳），规范化键序后
    内容一致即视为同一份原排程（拖拽后的手工排程与自动排程时间轴不同，
    指纹自然不同）。
    """
    blob = json.dumps(schedule or {}, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


def _same_number(a: Any, b: Any, eps: float = 1e-9) -> bool:
    try:
        return abs(float(a) - float(b)) <= eps
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def seal_batch(entries: list[dict], note: str = '') -> dict:
    """把 2～8 条已保存复核记录封存为一个不可变批次。

    ``entries`` 按操作员选择顺序给出，每项形如::

        {'review_id': int, 'created_at': str, 'note': str, 'payload': dict}

    一致性检查失败时抛 :class:`BatchError`（调用方应整批回滚、不落库）。
    """
    n = len(entries)
    if not (MIN_BATCH <= n <= MAX_BATCH):
        raise BatchError(
            'BATCH_SIZE',
            f'封存批次必须选择 {MIN_BATCH}～{MAX_BATCH} 条复核记录'
            f'（当前 {n} 条）',
            {'count': n, 'min': MIN_BATCH, 'max': MAX_BATCH})

    # ---- 选择列表自身合法性：重复选择 ----------------------------------- #
    seen_ids: set[int] = set()
    order: list[int] = []
    for e in entries:
        rid = int(e['review_id'])
        if rid in seen_ids:
            raise BatchError(
                'DUPLICATE_SELECTION',
                f'选择列表中复核记录 #{rid} 出现了多次，请勿重复选择',
                {'review_id': rid})
        seen_ids.add(rid)
        order.append(rid)

    prepared: list[dict] = []
    for e in entries:
        rid = int(e['review_id'])
        payload = e.get('payload') or {}
        params = payload.get('parameters')
        samples = payload.get('samples')
        if not isinstance(params, dict) or not isinstance(samples, list) \
                or not samples:
            raise BatchError(
                'BAD_REVIEW_PAYLOAD',
                f'复核记录 #{rid} 的内容不完整（缺少复核参数或样本结果），'
                '无法封存',
                {'review_id': rid})
        src = payload.get('source_schedule')
        if not isinstance(src, dict) or not src.get('segments'):
            raise BatchError(
                'MISSING_SNAPSHOT',
                f'复核记录 #{rid} 未嵌入原排程快照（可能是旧版记录），'
                '无法确认批次引用同一份原排程',
                {'review_id': rid})
        prepared.append({'review_id': rid,
                         'created_at': e.get('created_at'),
                         'note': str(e.get('note') or ''),
                         'payload': payload,
                         'params': params,
                         'source': src,
                         'fingerprint': schedule_fingerprint(src),
                         'sample_ids': _sample_ids(payload)})

    first = prepared[0]

    # ---- 原排程快照一致 -------------------------------------------------- #
    for other in prepared[1:]:
        if other['fingerprint'] != first['fingerprint']:
            raise BatchError(
                'SNAPSHOT_MISMATCH',
                f"复核记录 #{first['review_id']} 与 #"
                f"{other['review_id']} 引用的原排程快照不一致"
                '（时间轴/泳道/占用不同），不能封存为同一批次',
                {'review_ids': [first['review_id'], other['review_id']]})

    # ---- 复核基线参数一致：允许偏差 / 剂量上限 / 最大采样间隔 ------------ #
    for other in prepared[1:]:
        diffs = _parameter_diffs(first['params'], other['params'])
        if diffs:
            raise BatchError(
                'PARAMETER_MISMATCH',
                f"复核记录 #{first['review_id']} 与 #"
                f"{other['review_id']} 的复核基线参数不一致（"
                + '、'.join(d['label'] for d in diffs) + '），不能封存为同一批次',
                {'review_ids': [first['review_id'], other['review_id']],
                 'fields': [d['field'] for d in diffs]})

    # ---- 样本集合相同（按 sample_id，与记录内顺序无关） ----------------- #
    base_ids = first['sample_ids']
    for other in prepared[1:]:
        if other['sample_ids'] != base_ids:
            missing = sorted(base_ids - other['sample_ids'])
            extra = sorted(other['sample_ids'] - base_ids)
            parts = [f"复核记录 #{first['review_id']} 与 #"
                     f"{other['review_id']} 的样本集合不一致"]
            if missing:
                parts.append(f"后者缺少样本 {missing}")
            if extra:
                parts.append(f"后者多出样本 {extra}")
            raise BatchError(
                'SAMPLE_SET_MISMATCH',
                '；'.join(parts) + '，不能封存为同一批次',
                {'review_ids': [first['review_id'], other['review_id']],
                 'missing': missing, 'extra': extra})

    return _build_batch(prepared, note, order)


# --------------------------------------------------------------------------- #
# 批次结果构建
# --------------------------------------------------------------------------- #
def _sample_ids(payload: dict) -> set[int]:
    ids: set[int] = set()
    for s in payload.get('samples') or []:
        sid = s.get('sample_id')
        if sid is None:
            raise BatchError(
                'BAD_REVIEW_PAYLOAD',
                '复核记录中存在缺少样本 ID 的结果，无法按样本对齐', {})
        ids.add(int(sid))
    return ids


_PARAM_SPECS = [
    ('tolerance', '允许偏差'),
    ('dose_limit', '累计偏差剂量上限'),
    ('max_gap', '最大采样间隔'),
]


def _parameter_diffs(a: dict, b: dict) -> list[dict]:
    out = []
    for field, label in _PARAM_SPECS:
        if not _same_number(a.get(field), b.get(field)):
            out.append({'field': field, 'label': label,
                        'a': a.get(field), 'b': b.get(field)})
    return out


def _worst(conclusions: list[str]) -> str:
    return max(conclusions, key=lambda c: _RANK.get(c, -1))


def _r4(x: Any) -> float:
    return round(float(x or 0.0), 4)


def _build_batch(prepared: list[dict], note: str,
                 order: list[int]) -> dict:
    first_params = prepared[0]['params']
    tolerance = _r4(first_params['tolerance'])
    dose_limit = _r4(first_params['dose_limit'])
    max_gap = int(round(float(first_params['max_gap'])))
    source = prepared[0]['source']

    # 每条输入记录的完整冻结副本（共享的原排程快照只在批次顶层保留一份）
    frozen_reviews: list[dict] = []
    by_record: dict[int, dict[int, dict]] = {}
    for p in prepared:
        rid = p['review_id']
        payload = p['payload']
        samples_by_id: dict[int, dict] = {}
        for s in payload.get('samples') or []:
            sid = int(s['sample_id'])
            samples_by_id[sid] = {
                'sample_id': sid,
                'code': s.get('code'),
                'conclusion': s.get('conclusion'),
                'conclusion_label': s.get('conclusion_label')
                or CONCLUSION_LABELS.get(s.get('conclusion'), ''),
                'dose': _r4(s.get('dose')),
                'max_deviation': _r4(s.get('max_deviation')),
                'data_complete': bool(s.get('data_complete')),
                'board': s.get('board'),
                'unboard': s.get('unboard'),
                'window_minutes': s.get('window_minutes'),
                'covered_minutes': _r4(s.get('covered_minutes')),
                'uncovered': s.get('uncovered') or [],
                'excess_intervals': s.get('excess_intervals') or [],
                'tolerance': _r4(s.get('tolerance', tolerance)),
                'dose_limit': _r4(s.get('dose_limit', dose_limit)),
                'max_gap': s.get('max_gap', max_gap),
            }
        by_record[rid] = samples_by_id
        frozen_reviews.append({
            'review_id': rid,
            'created_at': p['created_at'],
            'note': p['note'],
            'overall_conclusion': payload.get('overall_conclusion'),
            'overall_conclusion_label': payload.get('overall_conclusion_label')
            or CONCLUSION_LABELS.get(payload.get('overall_conclusion'), ''),
            'summary': payload.get('summary') or {},
            'readings': payload.get('readings') or [],
            'gaps': payload.get('gaps') or [],
            'samples_by_id': samples_by_id,
        })

    # ---- 按样本 ID 对齐（升序，确定性输出），跨记录取最严重结论与极值 --- #
    sample_ids = sorted(prepared[0]['sample_ids'])
    first_by_id = by_record[order[0]]
    samples_out: list[dict] = []
    n_pass = n_review = n_insuf = 0
    for sid in sample_ids:
        per_review = []
        conclusions = []
        max_dose = -1.0
        max_dose_rid = None
        max_dev = -1.0
        max_dev_rid = None
        for rid in order:
            s = by_record[rid][sid]
            conclusions.append(s['conclusion'])
            if s['dose'] > max_dose:
                max_dose, max_dose_rid = s['dose'], rid
            if s['max_deviation'] > max_dev:
                max_dev, max_dev_rid = s['max_deviation'], rid
            per_review.append({
                'review_id': rid,
                'conclusion': s['conclusion'],
                'conclusion_label': s['conclusion_label'],
                'dose': s['dose'],
                'max_deviation': s['max_deviation'],
                'data_complete': s['data_complete'],
            })
        conclusion = _worst(conclusions)
        if conclusion == CONCLUSION_PASS:
            n_pass += 1
        elif conclusion == CONCLUSION_REVIEW:
            n_review += 1
        else:
            n_insuf += 1
        anchor = first_by_id[sid]
        samples_out.append({
            'sample_id': sid,
            'code': anchor.get('code')
            or _code_from_lane(source, sid),
            'board': anchor.get('board'),
            'unboard': anchor.get('unboard'),
            'window_minutes': anchor.get('window_minutes'),
            'conclusion': conclusion,
            'conclusion_label': CONCLUSION_LABELS[conclusion],
            'dose_limit': dose_limit,
            'tolerance': tolerance,
            'max_gap': max_gap,
            'max_dose': _r4(max(max_dose, 0.0)),
            'max_dose_review_id': max_dose_rid,
            'max_deviation': _r4(max(max_dev, 0.0)),
            'max_deviation_review_id': max_dev_rid,
            'per_review': per_review,
        })

    overall = _worst([s['conclusion'] for s in samples_out])
    return {
        'schema': 'review-batch/v1',
        'note': str(note or ''),
        'parameters': {
            'tolerance': tolerance,
            'dose_limit': dose_limit,
            'max_gap': max_gap,
        },
        'schedule_fingerprint': prepared[0]['fingerprint'],
        'source_schedule': source,
        'schedule': {
            'end_minute': source.get('end_minute'),
            'sample_count': len(source.get('lanes') or sample_ids),
        },
        'review_count': len(prepared),
        'review_ids': order,
        'reviews': frozen_reviews,
        'samples': samples_out,
        'summary': {'pass': n_pass, 'review': n_review,
                    'insufficient': n_insuf, 'total': len(samples_out)},
        'overall_conclusion': overall,
        'overall_conclusion_label': CONCLUSION_LABELS[overall],
    }


def _code_from_lane(source: dict, sample_id: int) -> Optional[str]:
    for lane in source.get('lanes') or []:
        if int(lane.get('sample_id')) == sample_id:
            return lane.get('code')
    return None
