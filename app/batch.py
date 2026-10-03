"""复核批次封存引擎（纯标准库，无 Flask/SQLite 依赖，便于单测）。

把 2~8 条已保存的实测复核记录封存为一个**不可变批次**：

* 所有记录必须引用**同一份原排程快照**（按规范化 JSON 的 SHA-256
  指纹比对）；任何一条缺失快照即整批拒绝；
* 复核参数必须一致：允许偏差、累计偏差剂量上限、最大采样间隔；
* 样本集合必须相同（同一批 sample_id）；
* 按样本 ID 对齐各记录结论：
    - 任一记录该样本为「数据不足」→ 批次该样本「数据不足」；
    - 否则任一记录为「复核」→「复核」；
    - 全部「通过」才为「通过」；
* 每个样本保留跨记录的**最大剂量**与**最大偏差**，并保留每条输入记录的
  逐样本结论/剂量/最大偏差明细；批次整体结论对对齐后的样本结论再做一次
  同样的汇总。

任何校验失败都抛出 :class:`BatchError`。调用方（DB 层）在同一次事务中
读取记录、执行本校验并写入批次行：校验失败即异常退出，事务不提交，
整批拒绝且不落库。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .review import (CONCLUSION_PASS, CONCLUSION_REVIEW,
                     CONCLUSION_INSUFFICIENT, CONCLUSION_LABELS)

MIN_RECORDS = 2
MAX_RECORDS = 8

# 结论严重度：数据不足 > 复核 > 通过（对齐时取最严重者）
_SEVERITY = {CONCLUSION_PASS: 0, CONCLUSION_REVIEW: 1,
             CONCLUSION_INSUFFICIENT: 2}

_PARAM_KEYS = ('tolerance', 'dose_limit', 'max_gap')
_PARAM_LABELS = {'tolerance': '允许偏差', 'dose_limit': '累计偏差剂量上限',
                 'max_gap': '最大采样间隔'}


class BatchError(Exception):
    """批次封存校验失败（整批拒绝，不落库）。"""


def snapshot_fingerprint(source_schedule: dict) -> str:
    """原排程快照的规范化 JSON SHA-256 指纹（键排序、紧凑分隔符）。"""
    canon = json.dumps(source_schedule, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'))
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()


def _worst(conclusions: list[str]) -> str:
    return max(conclusions, key=lambda c: _SEVERITY[c])


def _record_basics(rec: dict) -> dict:
    """从 DB 行/字典中提取封存所需字段；payload 必须是已解析的 dict。"""
    payload = rec.get('payload')
    if not isinstance(payload, dict):
        raise BatchError(f"复核记录 #{rec.get('id')} 内容损坏（缺少结果数据），"
                         "整批拒绝")
    return payload


def seal_batch(records: list[dict], note: str = '') -> dict:
    """把 2~8 条复核记录封存为一个批次结果（不落库，由调用方持久化）。

    ``records`` 中每条需含 ``id``、``created_at``、``conclusion``、
    ``note``、``payload``（已保存复核的完整结果，含 ``source_schedule``）。
    校验失败抛 :class:`BatchError`。
    """
    if not isinstance(records, (list, tuple)):
        raise BatchError("复核记录列表格式非法")
    n = len(records)
    if n < MIN_RECORDS or n > MAX_RECORDS:
        raise BatchError(
            f"批次封存需要 {MIN_RECORDS}~{MAX_RECORDS} 条复核记录，"
            f"当前选择了 {n} 条，整批拒绝")

    ids = [r.get('id') for r in records]
    if any(i is None for i in ids):
        raise BatchError("复核记录缺少 ID，无法封存")
    if len(set(ids)) != n:
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise BatchError(
            f"重复选择复核记录 {'、'.join('#' + str(i) for i in dup)}，整批拒绝")

    payloads = [_record_basics(r) for r in records]

    # ---- 1. 同一份原排程快照（缺失快照 / 指纹不一致 → 整批拒绝） --------- #
    fps = []
    for r, p in zip(records, payloads):
        src = p.get('source_schedule')
        if not src:
            raise BatchError(
                f"复核记录 #{r['id']} 缺失原排程快照，无法确认同基线，整批拒绝")
        fps.append(snapshot_fingerprint(src))
    if len(set(fps)) != 1:
        raise BatchError(
            "所选复核记录引用的原排程快照不一致（基线不同），整批拒绝")

    # ---- 2. 复核参数一致 ------------------------------------------------- #
    params0 = payloads[0].get('parameters') or {}
    for r, p in zip(records[1:], payloads[1:]):
        params = p.get('parameters') or {}
        for key in _PARAM_KEYS:
            v0, v1 = params0.get(key), params.get(key)
            if v0 is None or v1 is None:
                raise BatchError(
                    f"复核记录 #{r['id']} 缺少复核参数"
                    f"（{_PARAM_LABELS[key]}），整批拒绝")
            if abs(float(v0) - float(v1)) > 1e-9:
                raise BatchError(
                    f"复核参数不一致：{_PARAM_LABELS[key]}"
                    f"（#{records[0]['id']} 为 {v0}，#{r['id']} 为 {v1}），"
                    "整批拒绝")

    # ---- 3. 样本集合相同 ------------------------------------------------- #
    def sample_ids(p: dict) -> list[Any]:
        return sorted(s.get('sample_id') for s in (p.get('samples') or []))

    ids0 = sample_ids(payloads[0])
    if not ids0:
        raise BatchError("复核记录不含任何样本结果，无法封存")
    for r, p in zip(records[1:], payloads[1:]):
        if sample_ids(p) != ids0:
            raise BatchError(
                f"复核记录 #{r['id']} 的样本集合与 #{records[0]['id']} 不一致，"
                "整批拒绝")

    # ---- 4. 按样本 ID 对齐结论 ------------------------------------------ #
    first_samples = {s['sample_id']: s for s in payloads[0]['samples']}
    per_record_samples = [
        {s['sample_id']: s for s in p['samples']} for p in payloads]

    samples_out = []
    for sid in ids0:
        base = first_samples[sid]
        per_record = []
        conclusions = []
        max_dose = 0.0
        max_dev = 0.0
        for r, smap in zip(records, per_record_samples):
            s = smap[sid]
            concl = s.get('conclusion')
            if concl not in _SEVERITY:
                raise BatchError(
                    f"复核记录 #{r['id']} 中样本 {base.get('code') or sid} "
                    f"的结论无法识别（{concl!r}），整批拒绝")
            dose = float(s.get('dose') or 0.0)
            dev = float(s.get('max_deviation') or 0.0)
            conclusions.append(concl)
            max_dose = max(max_dose, dose)
            max_dev = max(max_dev, dev)
            per_record.append({
                'review_id': r['id'],
                'conclusion': concl,
                'conclusion_label': CONCLUSION_LABELS[concl],
                'dose': round(dose, 4),
                'max_deviation': round(dev, 4),
            })
        aligned = _worst(conclusions)
        samples_out.append({
            'sample_id': sid,
            'code': base.get('code'),
            'board': base.get('board'),
            'unboard': base.get('unboard'),
            'window_minutes': base.get('window_minutes'),
            'conclusion': aligned,
            'conclusion_label': CONCLUSION_LABELS[aligned],
            'max_dose': round(max_dose, 4),
            'max_deviation': round(max_dev, 4),
            'per_record': per_record,
        })

    n_pass = sum(s['conclusion'] == CONCLUSION_PASS for s in samples_out)
    n_review = sum(s['conclusion'] == CONCLUSION_REVIEW for s in samples_out)
    n_insuf = sum(s['conclusion'] == CONCLUSION_INSUFFICIENT
                  for s in samples_out)
    if n_insuf:
        overall = CONCLUSION_INSUFFICIENT
    elif n_review:
        overall = CONCLUSION_REVIEW
    else:
        overall = CONCLUSION_PASS

    return {
        'kind': 'review_batch',
        'note': str(note or ''),
        'review_ids': [r['id'] for r in records],
        'record_count': n,
        'parameters': {k: params0.get(k) for k in _PARAM_KEYS},
        'schedule_fingerprint': fps[0],
        'schedule': (payloads[0].get('schedule') or {}),
        'records': [{
            'review_id': r['id'],
            'created_at': r.get('created_at'),
            'note': r.get('note') or '',
            'conclusion': r.get('conclusion'),
            'conclusion_label': CONCLUSION_LABELS.get(
                r.get('conclusion'), r.get('conclusion')),
            'summary': (p.get('summary') or {}),
        } for r, p in zip(records, payloads)],
        'samples': samples_out,
        'summary': {'pass': n_pass, 'review': n_review,
                    'insufficient': n_insuf, 'total': len(samples_out)},
        'overall_conclusion': overall,
        'overall_conclusion_label': CONCLUSION_LABELS[overall],
    }
