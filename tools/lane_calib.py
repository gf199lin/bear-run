# -*- coding: utf-8 -*-
r"""左右移：如果每次会话开头做一次"左/右各动一次"的校准，规则法能到什么水平？

动机
----
lane_track 的分组结果：
  符号稳定组（seg-lr2/1）  SNR 1.23~1.32  ← 可用
  全部 7 文件              SNR 0.93       ← 不可用
差距几乎全部来自"符号在某些会话里是反的"（oncehot 的 LEFT 同号率仅 33%）。

所以问题不是算法，是**每个会话的坐标方向可能不同**。
如果开场用几个动作段学会「本次会话哪边是左」，后续就能用简单阈值。

协议（严格留出，避免用测试段调参）
--------------------------------
每个文件：
  1. 前 30% 的动作段只用于校准：取该组 dev 的多数符号、取静息 |dev| 的 p95 作阈值；
  2. 剩余 70% 的动作段才评估：段内首次"同号且越阈"即命中；
  3. 报告命中率与延迟。

这是"现场开场校准 10 秒"的离线等价物。
"""
import os
import sys
import json

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, os.path.join(ROOT, '.deepworks', 'tmp'))
sys.path.insert(0, HERE)

import radar_source as rs  # noqa: E402

rs.sniff_board_from_file = lambda path, scan=262144: '1t2r'

from lane_window import FR, load_all  # noqa: E402


def dev_of(px, base):
    return px - base


def main():
    items = load_all()
    total_hit = total_n = 0
    rows = []
    print('%-14s %-8s %-5s %-8s %-8s %-7s' %
          ('文件', '校准段', '测试', 'LEFT', 'RIGHT', '延迟中位'))
    print('-' * 62)
    for it in items:
        px = it['px']
        t = np.arange(len(px)) * FR
        spans = sorted(it['spans'], key=lambda s: s[1])
        if len(spans) < 4:
            continue
        # 采集顺序是「先 N 次左、后 N 次右」，不能按时间切 30%：
        # 那样校准集全是 LEFT，RIGHT 的符号永远学不到。每个方向各留前 20%。
        by_side = {'LEFT': [], 'RIGHT': []}
        for s in spans:
            by_side[s[0]].append(s)
        cal, test = [], []
        for side in ('LEFT', 'RIGHT'):
            g = by_side[side]
            k = max(1, int(round(len(g) * 0.2)))
            cal += g[:k]
            test += g[k:]
        ncal = len(cal)

        # 静息基线：所有非动作帧
        still = np.ones(len(px), bool)
        for _, t0, t1 in spans:
            still &= ~((t >= t0) & (t <= t1))
        still &= ~np.isnan(px)
        if still.sum() < 50:
            continue
        base = float(np.median(px[still]))
        thr = float(np.percentile(np.abs(px[still] - base), 95))

        # 校准符号：每个动作段取段内 dev 中位，按标签投票
        votes = {'LEFT': [], 'RIGHT': []}
        for act, t0, t1 in cal:
            sel = (t >= t0) & (t <= t1) & ~np.isnan(px)
            if sel.sum() >= 3:
                votes[act].append(float(np.median(px[sel] - base)))
        if not votes['LEFT'] or not votes['RIGHT']:
            continue
        signL = 1.0 if np.mean(votes['LEFT']) > 0 else -1.0
        signR = -signL  # 左右必须相反，否则物理上不成立

        hitL = totL = hitR = totR = 0
        lat = []
        for act, t0, t1 in test:
            sel = (t >= t0) & (t <= t1) & ~np.isnan(px)
            dev = px[sel] - base
            sign = signL if act == 'LEFT' else signR
            idx = np.where(sign * dev > thr)[0]
            ok = len(idx) > 0
            if act == 'LEFT':
                totL += 1; hitL += int(ok)
            else:
                totR += 1; hitR += int(ok)
            if ok:
                lat.append(float(t[sel][idx[0]] - t0))

        total_hit += hitL + hitR
        total_n += totL + totR
        med = sorted(lat)[len(lat) // 2] if lat else None
        rows.append(dict(tag=it['tag'], name=it['name'], signL=signL,
                         thr=thr, hitL=hitL, totL=totL,
                         hitR=hitR, totR=totR, lat_med=med))
        print('%-14s %-8d %-5d %-8s %-8s %-7s'
              % (it['name'][:13], ncal, len(test),
                 '%d/%d' % (hitL, totL), '%d/%d' % (hitR, totR),
                 ('%.2f s' % med) if med else '—'))

    print()
    print('=' * 62)
    print('校准后规则法：命中 %d/%d = %.0f%%'
          % (total_hit, total_n, 100.0 * total_hit / max(1, total_n)))
    lat = sorted(r['lat_med'] for r in rows if r['lat_med'])
    if lat:
        print('  延迟中位（段内首次越阈）约 %.2f s' % lat[len(lat) // 2])
    print('  对照：不校准 xbase 59%%（延迟 1.81 s，误报 108）')
    print('  对照：ML 无置信 67%%（延迟 0.39 s，误报 3725）')
    print('=' * 62)

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'calibrated-rule.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(rows=rows, hit=total_hit, n=total_n), f,
                  ensure_ascii=False, indent=1)
    print('已写 outputs/lane-eval/calibrated-rule.json')


if __name__ == '__main__':
    main()
