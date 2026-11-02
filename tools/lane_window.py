# -*- coding: utf-8 -*-
r"""左右移：时域积分（窗均值）能不能把弱信号从噪声里拉出来，代价是多少延迟。

背景
----
lane_separability 的结论：单帧 px 的静息抖动 p90 = 0.425 m，
而 LEFT 的 dev 中位只有 +0.064 m —— 单帧信噪比 < 0.2，规则法不可能判准。

做法（全部因果，只看过去）
--------------------------
1. 逐帧 px 序列缓存到 .deepworks/tmp/lane_cache/（带正确 band，见 read_meta）。
2. 对窗长 W ∈ {1,4,8,12,20,30}：`m[t] = mean(px[t-W+1 .. t])`。
3. 基线 `b` = 静息段 m 的中位；`dev[t] = m[t] - b`。
4. 阈值：取静息段 `|dev|` 的 p95（误报率钉在 5%）。
5. 指标：
   - 方向准确率：段起点后 W 帧到段末，首个越过阈值且方向正确的段占比；
   - 延迟：首个正确越阈帧的时刻 - 动作起点（秒）；
   - 误报：静息帧中越阈的比例（由阈值定义已知 ≈5%）。

只读。板型钉死 1t2r。
"""
import os
import sys
import glob
import json

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, os.path.join(ROOT, '.deepworks', 'tmp'))

import radar_source as rs  # noqa: E402

rs.sniff_board_from_file = lambda path, scan=262144: '1t2r'

import label_tool as lt  # noqa: E402
from radar_source import make_protocol  # noqa: E402
from radar_dsp import PointCloudProcessor  # noqa: E402

FR = 0.1295
CHUNK = 65536
CFAR = 2.6
SNR = 7.0
CACHE = os.path.join(ROOT, '.deepworks', 'tmp', 'lane_cache')
SETS = ['seg-lr2/1', 'seg-lr2/jqz', 'oncehot-oncehot/lr']
WINDOWS = [1, 4, 8, 12, 20, 30]


def read_meta(labp):
    with open(labp, encoding='utf-8') as f:
        return json.loads(f.readline()).get('_meta', {})


def extract(path, band):
    data = open(path, 'rb').read()
    parser, asm = make_protocol('1t2r')
    proc = PointCloudProcessor(cfar_scale=CFAR, min_snr=SNR, min_range=0.681)
    lo, hi = band
    px = []
    for i in range(0, len(data), CHUNK):
        for fr in parser.feed(data[i:i + CHUNK]):
            z = asm.push(fr)
            if z is None:
                continue
            pts, _ = proc.process(*z)
            sel = [q for q in pts if lo <= q['y'] <= hi]
            px.append(sel[0]['x'] if sel else np.nan)
    return np.array(px, dtype=float)


def load_all():
    os.makedirs(CACHE, exist_ok=True)
    items = []
    for rel in SETS:
        base = os.path.join(ROOT, '.deepworks', 'tmp', rel)
        for p in sorted(glob.glob(os.path.join(base, '**', '*.bin'), recursive=True)):
            labp = os.path.splitext(p)[0] + '.labels.jsonl'
            if not os.path.exists(labp):
                continue
            meta = read_meta(labp)
            band = meta.get('band') or [1.36, 2.04]
            key = os.path.splitext(os.path.relpath(p, os.path.join(ROOT, '.deepworks', 'tmp')))[0]
            cdir = os.path.join(CACHE, rel.replace('/', '_'))
            os.makedirs(cdir, exist_ok=True)
            cf = os.path.join(cdir, os.path.basename(key) + '.npz')
            if os.path.exists(cf):
                px = np.load(cf)['px']
            else:
                px = extract(p, band)
                np.savez_compressed(cf, px=px)
            labels = lt.load_labels(labp)
            sp = [(l['action'], l['t_start'], l['t_end'])
                  for l in labels if l.get('action') in ('LEFT', 'RIGHT')]
            items.append(dict(tag=rel, name=os.path.basename(p), px=px,
                              band=band, spans=sp))
    return items


def rolling_mean(x, w):
    """因果滑动均值：只用 t-w+1..t。NaN 跳过（nan-aware）。"""
    if w <= 1:
        return x.copy()
    v = ~np.isnan(x)
    xz = np.where(v, x, 0.0)
    cs = np.cumsum(xz)
    cn = np.cumsum(v.astype(float))
    out = np.full(len(x), np.nan)
    for t in range(len(x)):
        a = max(0, t - w + 1)
        n = cn[t] - (cn[a - 1] if a > 0 else 0.0)
        if n > 0:
            out[t] = (cs[t] - (cs[a - 1] if a > 0 else 0.0)) / n
    return out


def analyse(items, w):
    res = []
    for it in items:
        px = it['px']
        m = rolling_mean(px, w)
        t = np.arange(len(px)) * FR
        y = np.array(['NONE'] * len(t), dtype=object)
        for act, t0, t1 in it['spans']:
            y[(t >= t0) & (t <= t1)] = act
        still = (y == 'NONE') & ~np.isnan(m)
        if still.sum() < 50:
            continue
        base = np.median(m[still])
        dev = m - base
        thr = float(np.nanpercentile(np.abs(dev[still]), 95))
        for side, sgn in (('LEFT', 1), ('RIGHT', -1)):
            for act, t0, t1 in it['spans']:
                if act != side:
                    continue
                sel = (t >= t0) & (t <= t1)
                hit_idx = np.where(sel & ~np.isnan(dev) & (sgn * dev > thr))[0]
                if len(hit_idx):
                    # 段内第一次越阈；若越阈发生在段内，延迟就是它的相对时刻
                    dt = t[hit_idx[0]] - t0
                    res.append(dict(side=side, hit=1, lat=dt, thr=thr,
                                    file=it['name'], tag=it['tag']))
                else:
                    res.append(dict(side=side, hit=0, lat=None, thr=thr,
                                    file=it['name'], tag=it['tag']))
    return res


def main():
    items = load_all()
    print('文件 %d 个，总帧 %d' % (len(items), sum(len(i['px']) for i in items)))
    print()
    hdr = '%-5s %-7s %-9s %-9s %-9s' % ('窗', '帧', '总命中', 'LEFT', 'RIGHT')
    print(hdr)
    print('-' * len(hdr))
    summary = {}
    for w in WINDOWS:
        r = analyse(items, w)
        tot = len(r)
        hit = sum(x['hit'] for x in r)
        L = [x for x in r if x['side'] == 'LEFT']
        R = [x for x in r if x['side'] == 'RIGHT']
        hl = sum(x['hit'] for x in L)
        hr = sum(x['hit'] for x in R)
        lat = sorted(x['lat'] for x in r if x['hit'])
        med = lat[len(lat) // 2] if lat else float('nan')
        print('%-5d %-7.1f %-9s %-9s %-9s'
              % (w, w * FR,
                 '%d/%d %.0f%%' % (hit, tot, 100.0 * hit / max(1, tot)),
                 '%d/%d %.0f%%' % (hl, len(L), 100.0 * hl / max(1, len(L))),
                 '%d/%d %.0f%%' % (hr, len(R), 100.0 * hr / max(1, len(R)))))
        summary[w] = dict(hit=hit, tot=tot, hitL=hl, totL=len(L),
                          hitR=hr, totR=len(R), lat_med=med,
                          lat_p90=(lat[int(len(lat) * 0.9)] if lat else None))
        if lat:
            print('        延迟 中位 %.2f s  P90 %.2f s'
                  % (med, lat[min(len(lat) - 1, int(len(lat) * 0.9))]))

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'window-sweep.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print()
    print('已写 outputs/lane-eval/window-sweep.json')


if __name__ == '__main__':
    main()
