# -*- coding: utf-8 -*-
r"""C：点云跟踪替代"单帧最强点"，看能不能降噪而不衰减信号。

为什么时间平均（窗均值/EMA）没用
--------------------------------
平滑同时衰减信号和噪声，SNR 不变 —— 这就是 lane_window 里窗越长越差的原因。
唯一能提 SNR 的是「换一个抖动更小的测量」：如果抖动来自"最强点在人的多个
散射中心之间来回跳"，那么**锁定同一散射中心**就能降噪，而动作幅度不衰减。

四种测量方式对比（同一批帧、同一 band）
  1. top1      单帧最强点（现有产线口径）
  2. centroid  带内所有点能量加权质心
  3. tracked   在候选点里选与上帧最近的（门限 0.35 m），丢帧沿用预测
  4. tracked+e 跟踪 + 轻度 EMA(0.6)

指标：静息 σ、LEFT/RIGHT 的 |dev| 中位、SNR = 幅度/σ、符号同号率。

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
GATE = 0.35
SETS = ['seg-lr2/1', 'seg-lr2/jqz', 'oncehot-oncehot/lr']


def read_meta(labp):
    with open(labp, encoding='utf-8') as f:
        return json.loads(f.readline()).get('_meta', {})


def frame_points(path, band, cfar=2.6, snr=7.0):
    """返回每帧带内候选点的 [(x, p), ...]。"""
    data = open(path, 'rb').read()
    parser, asm = make_protocol('1t2r')
    proc = PointCloudProcessor(cfar_scale=cfar, min_snr=snr, min_range=0.681)
    lo, hi = band
    out = []
    for i in range(0, len(data), CHUNK):
        for fr in parser.feed(data[i:i + CHUNK]):
            z = asm.push(fr)
            if z is None:
                continue
            pts, _ = proc.process(*z)
            out.append([(q['x'], q['p']) for q in pts if lo <= q['y'] <= hi])
    return out


def m_top1(frames):
    return np.array([c[0][0] if c else np.nan for c in frames], float)


def m_centroid(frames):
    out = []
    for c in frames:
        if not c:
            out.append(np.nan)
            continue
        w = np.array([p for _, p in c])
        x = np.array([v for v, _ in c])
        out.append(float((x * w).sum() / w.sum()) if w.sum() > 0 else float(x[0]))
    return np.array(out)


def m_tracked(frames, gate=GATE, alpha=0.0):
    """最近邻跟踪：优先选离预测位置最近的点，丢帧沿用上次值。"""
    out = []
    x = None
    for c in frames:
        if not c:
            out.append(x if x is not None else np.nan)
            continue
        xs = np.array([v for v, _ in c])
        if x is None:
            x = float(xs[0])
        else:
            j = int(np.argmin(np.abs(xs - x)))
            if abs(xs[j] - x) <= gate:
                nxt = float(xs[j])
            else:
                nxt = x          # 超出关联门限：认为本帧没看到人，沿用
            x = (1 - alpha) * nxt + alpha * x
        out.append(x)
    return np.array(out)


def metrics(px, spans):
    t = np.arange(len(px)) * FR
    y = np.array(['NONE'] * len(t), dtype=object)
    for act, t0, t1 in spans:
        y[(t >= t0) & (t <= t1)] = act
    still = (y == 'NONE') & ~np.isnan(px)
    L = px[(y == 'LEFT') & ~np.isnan(px)]
    R = px[(y == 'RIGHT') & ~np.isnan(px)]
    if still.sum() < 30 or len(L) < 5 or len(R) < 5:
        return None
    base = float(np.median(px[still]))
    sig = float(np.std(px[still] - base))
    dL = abs(float(np.median(L - base)))
    dR = abs(float(np.median(R - base)))
    return dict(sigma=sig, dL=dL, dR=dR,
                snr=(dL + dR) / 2 / max(sig, 1e-6),
                posL=float((L - base > 0).mean()),
                negR=float((R - base < 0).mean()),
                det=1.0 - np.isnan(px).sum() / len(px))


def main():
    fs = []
    for rel in SETS:
        base = os.path.join(ROOT, '.deepworks', 'tmp', rel)
        for p in sorted(glob.glob(os.path.join(base, '**', '*.bin'), recursive=True)):
            labp = os.path.splitext(p)[0] + '.labels.jsonl'
            if os.path.exists(labp):
                fs.append((rel, p, labp))

    methods = {
        'top1': lambda f: m_top1(f),
        'centroid': lambda f: m_centroid(f),
        'tracked': lambda f: m_tracked(f, alpha=0.0),
        'tracked+ema': lambda f: m_tracked(f, alpha=0.6),
    }
    acc = {k: [] for k in methods}
    acc_stable = {k: [] for k in methods}

    print('%-16s %-10s %-6s %-8s' % ('文件', 'band', '帧', 'top1 σ'))
    for rel, p, labp in fs:
        meta = read_meta(labp)
        band = meta.get('band') or [1.36, 2.04]
        labels = lt.load_labels(labp)
        sp = [(l['action'], l['t_start'], l['t_end'])
              for l in labels if l.get('action') in ('LEFT', 'RIGHT')]
        frames = frame_points(p, band)
        sigs = []
        for name, fn in methods.items():
            m = metrics(fn(frames), sp)
            if m:
                acc[name].append(m)
                # 只保留符号稳定的一组（seg-lr2/1），避免"标签本身反了"污染降噪评估
                if rel == 'seg-lr2/1':
                    acc_stable[name].append(m)
                sigs.append('%s %.3f' % (name, m['sigma']))
        print('%-16s %-10s %-6d %s' % (os.path.basename(p)[:14], str(band),
                                       len(frames), '  '.join(sigs)))

    def table(title, store):
        print()
        print(title)
        print('%-12s %-9s %-9s %-9s %-7s %-8s' %
              ('方法', '静息σ', 'LEFT', 'RIGHT', 'SNR', '同号率'))
        print('-' * 62)
        for name, rows in store.items():
            if not rows:
                continue
            s = np.mean([r['sigma'] for r in rows])
            l = np.mean([r['dL'] for r in rows])
            r_ = np.mean([r['dR'] for r in rows])
            snr = np.mean([r['snr'] for r in rows])
            same = np.mean([(r['posL'] + r['negR']) / 2 for r in rows])
            print('%-12s %-9.3f %-9.3f %-9.3f %-7.2f %-8.0f%%'
                  % (name, s, l, r_, snr, 100 * same))

    table('全部 7 个文件', acc)
    table('仅符号稳定组 seg-lr2/1（2 个文件）', acc_stable)

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'tracking.json'), 'w', encoding='utf-8') as f:
        json.dump({k: [{kk: float(vv) for kk, vv in r.items()} for r in v]
                   for k, v in acc.items()}, f, ensure_ascii=False, indent=1)
    print()
    print('已写 outputs/lane-eval/tracking.json')


if __name__ == '__main__':
    main()
