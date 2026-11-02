# -*- coding: utf-8 -*-
r"""左右移：用「变化率 dx」而不是「相对基线的偏移 dev」来判方向。

为什么
------
lane_window 的结果：窗长 1→30 帧，命中率 48%→3%，越平均越差。
说明动作是一个**短促位移脉冲**，不是持续偏移；窗均值把它抹平了。
xbase 用的正是偏移（近窗中位 − 远窗中位），所以延迟被拖到 1.8 s。

本脚本改用 `dx = mean(px[t-w+1..t]) − mean(px[t-2w+1..t-w])`（两个相邻窗的位移），
它直接测"最近 w 帧朝哪边动了多少"，对脉冲敏感、对慢漂移不敏感。

符号：数据里 LEFT 的 dev 为正、RIGHT 为负（雷达 x 轴方向与标签相反），
所以 LEFT ← dx > +thr，RIGHT ← dx < −thr。

只读。缓存复用 lane_window 的 lane_cache。
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

from lane_window import FR, load_all, rolling_mean  # noqa: E402

WINDOWS = [2, 4, 8]
SIGN = {'LEFT': +1, 'RIGHT': -1}


def dx_series(px, w):
    fast = rolling_mean(px, w)
    slow = np.concatenate([np.full(w, np.nan), fast[:-w]])
    d = fast - slow
    return fast, d


def analyse(items, w):
    res = []
    for it in items:
        fast, d = dx_series(it['px'], w)
        t = np.arange(len(fast)) * FR
        y = np.array(['NONE'] * len(t), dtype=object)
        for act, t0, t1 in it['spans']:
            y[(t >= t0) & (t <= t1)] = act
        still = (y == 'NONE') & ~np.isnan(d)
        if still.sum() < 50:
            continue
        thr = float(np.nanpercentile(np.abs(d[still]), 95))
        for side, sgn in SIGN.items():
            for act, t0, t1 in it['spans']:
                if act != side:
                    continue
                sel = (t >= t0) & (t <= t1) & ~np.isnan(d)
                idx = np.where(sel & (sgn * d > thr))[0]
                if len(idx):
                    res.append(dict(side=side, hit=1, lat=t[idx[0]] - t0,
                                    thr=thr, file=it['name']))
                else:
                    res.append(dict(side=side, hit=0, lat=None, thr=thr,
                                    file=it['name']))
    return res


def main():
    items = load_all()
    print('文件 %d 个，总帧 %d' % (len(items), sum(len(i['px']) for i in items)))
    print()
    hdr = '%-6s %-8s %-16s %-16s %-16s' % ('半窗', '两窗', '总命中', 'LEFT', 'RIGHT')
    print(hdr)
    print('-' * len(hdr))
    summary = {}
    for w in WINDOWS:
        r = analyse(items, w)
        tot, hit = len(r), sum(x['hit'] for x in r)
        L = [x for x in r if x['side'] == 'LEFT']
        R = [x for x in r if x['side'] == 'RIGHT']
        hl = sum(x['hit'] for x in L)
        hr = sum(x['hit'] for x in R)
        lat = sorted(x['lat'] for x in r if x['hit'])
        med = lat[len(lat) // 2] if lat else float('nan')
        print('%-6d %-8.1f %-16s %-16s %-16s'
              % (w, 2 * w * FR,
                 '%d/%d %.0f%%' % (hit, tot, 100.0 * hit / max(1, tot)),
                 '%d/%d %.0f%%' % (hl, len(L), 100.0 * hl / max(1, len(L))),
                 '%d/%d %.0f%%' % (hr, len(R), 100.0 * hr / max(1, len(R)))))
        print('        延迟 中位 %.2f s  P90 %.2f s'
              % (med, (lat[min(len(lat) - 1, int(len(lat) * 0.9))] if lat else float('nan'))))
        summary[w] = dict(hit=hit, tot=tot, hitL=hl, totL=len(L),
                          hitR=hr, totR=len(R), lat_med=med)

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'dx-sweep.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print()
    print('已写 outputs/lane-eval/dx-sweep.json')


if __name__ == '__main__':
    main()
