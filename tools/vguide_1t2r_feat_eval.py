# -*- coding: utf-8 -*-
"""1T2R 竖直方向跳/蹲的特征评估（离线）。

用法：
    python tools/vguide_1t2r_feat_eval.py data/vguide_1t2r/20261112-205706
    python tools/vguide_1t2r_feat_eval.py data/vguide_1t2r          # 该目录下全部批次

回答三个问题：
1. 动作发生时，能量是否真的偏离零多普勒（nz_share）？
2. 跳与蹲在「多普勒谱宽」上是否可分（双峰空隙多大）？
3. 用固定阈值回放，事件级判对多少？

背景：帧周期 129.5 ms（64 chirp × 1.96 ms），一次跳/蹲在单个 CPI 内完成加减速，
正负多普勒互相抵消，逐帧平均径向速度 v_r 的符号不可用；改用谱宽。
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vguide_1t2r as vg  # noqa: E402


def list_batches(path):
    if os.path.exists(os.path.join(path, 'frames.npz')):
        return [path]
    out = []
    for name in sorted(os.listdir(path)):
        d = os.path.join(path, name)
        if os.path.exists(os.path.join(d, 'frames.npz')):
            out.append(d)
    return out


def truth_of(i0, i1, marks):
    """事件区间与哪一段重叠帧数最多，就采信那段真值。

    动作通常比提示晚 0.3~1 s，事件会横跨段边界；用中心帧会在边界上判错，
    用重叠帧数最多者更稳。
    """
    best, best_ov = '', 0
    for m in marks:
        ov = max(0, min(i1, m['frame_end']) - max(i0, m['frame_start']) + 1)
        if ov > best_ov:
            best_ov, best = ov, m['seg']
    return best or 'idle'


def evaluate(d):
    z = np.load(os.path.join(d, 'frames.npz'))
    frames = z['z']
    labels = z['label'].astype(str)
    with open(os.path.join(d, 'labels.json'), encoding='utf-8') as f:
        meta = json.load(f)
    band = tuple(meta.get('band') or (1, 4))
    fp = meta.get('frame_period', 0.1295)
    marks = meta.get('segments') or []

    feats = [vg.frame_feats(frames[i], band) for i in range(len(frames))]

    # 1) 零多普勒外能量占比：静息 vs 动作
    print('\n[1] 零多普勒外能量占比 nz_share（>0.30 视为有人在动）')
    for cls in ('base', 'jump', 'duck'):
        vals = [f['nz_share'] for f, lb in zip(feats, labels)
                if f and lb.startswith(cls)]
        if vals:
            print('    %-6s n=%4d  中位 %.3f  均值 %.3f' %
                  (cls, len(vals), float(np.median(vals)), float(np.mean(vals))))

    # 2) 事件判定
    judge = vg.ActionJudge()
    events = []
    for i, f in enumerate(feats):
        if not f:
            continue
        ev = judge.push(f['nz_share'], f['doppler_width'])
        if ev:
            events.append((i, ev))

    print('\n[2] 事件判定（gate %.2f / 阈值 %.3f / 下限 %.2f / 最短 %d 帧）' %
          (judge.gate, judge.thr, judge.lo, judge.min_frames))
    print('    %-6s %-6s %-6s %-7s %-5s %s' %
          ('结束帧', '时刻s', '判定', '谱宽', '持续', '真值'))
    hit = tot = 0
    for i, (lb, w, nf) in events:
        i0 = max(0, i - nf + 1)
        truth = truth_of(i0, i, marks) if marks else labels[max(0, i - nf // 2)]
        ok = truth.startswith(lb.lower())
        hit += ok
        tot += 1
        print('    %-6d %-6.2f %-6s %-7.3f %-5d %s' %
              (i, i * fp, lb, w, nf, truth))

    jumps = [w for _, (lb, w, _) in events if lb == 'JUMP']
    ducks = [w for _, (lb, w, _) in events if lb == 'DUCK']
    print('\n[3] 谱宽分布')
    if jumps:
        print('    JUMP  n=%d  范围 %.3f ~ %.3f' %
              (len(jumps), min(jumps), max(jumps)))
    if ducks:
        print('    DUCK  n=%d  范围 %.3f ~ %.3f' %
              (len(ducks), min(ducks), max(ducks)))
    if jumps and ducks:
        gap = min(jumps) - max(ducks)
        print('    两簇空隙 %.3f （>0 表示完全分开）' % gap)

    if tot:
        print('\n[4] 动作段事件命中 %d/%d = %.1f%%' % (hit, tot, 100.0 * hit / tot))
    print('    （阈值属本批提案，待更多样本与小组确认）')


def main():
    ap = argparse.ArgumentParser(description='1T2R 竖直跳/蹲特征评估')
    ap.add_argument('path', help='数据目录，或包含多个批次的父目录')
    a = ap.parse_args()
    batches = list_batches(a.path)
    if not batches:
        print('未找到 frames.npz')
        return 1
    for d in batches:
        print('=' * 62)
        print(os.path.basename(os.path.normpath(d)))
        print('=' * 62)
        evaluate(d)
    return 0


if __name__ == '__main__':
    sys.exit(main())
