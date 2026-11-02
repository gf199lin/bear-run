# -*- coding: utf-8 -*-
r"""B：扫 CFAR / SNR 门限，看"检出率 ↔ 噪声"能不能两全。

判据不变（单帧最强点 + 相对静息的偏离），只换 DSP 前端的两个门限：

  cfar_scale：CFAR 门限倍数，越小越容易检出弱目标、也越多虚警
  min_snr   ：最低信噪比（dB）

关键看"信噪比"这一列：`|LEFT dev 中位| / 静息 σ`。
它 < 1 就说明规则法再怎么调都不成立。目标是把这一列翻过 1。

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
SETS = ['seg-lr2/1', 'seg-lr2/jqz', 'oncehot-oncehot/lr']
CONFIGS = [
    (2.6, 7.0, '基线'),
    (2.0, 5.0, '松'),
    (1.6, 4.0, '很松'),
    (3.0, 9.0, '严'),
]


def read_meta(labp):
    with open(labp, encoding='utf-8') as f:
        return json.loads(f.readline()).get('_meta', {})


def files():
    out = []
    for rel in SETS:
        base = os.path.join(ROOT, '.deepworks', 'tmp', rel)
        for p in sorted(glob.glob(os.path.join(base, '**', '*.bin'), recursive=True)):
            labp = os.path.splitext(p)[0] + '.labels.jsonl'
            if os.path.exists(labp):
                out.append((p, labp))
    return out


def run(path, band, cfar, snr):
    data = open(path, 'rb').read()
    parser, asm = make_protocol('1t2r')
    proc = PointCloudProcessor(cfar_scale=cfar, min_snr=snr, min_range=0.681)
    lo, hi = band
    px, npts = [], []
    for i in range(0, len(data), CHUNK):
        for fr in parser.feed(data[i:i + CHUNK]):
            z = asm.push(fr)
            if z is None:
                continue
            pts, _ = proc.process(*z)
            sel = [q for q in pts if lo <= q['y'] <= hi]
            px.append(sel[0]['x'] if sel else np.nan)
            npts.append(len(pts))
    return np.array(px, dtype=float), np.array(npts, dtype=float)


def stats(px, npts, spans):
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
    devN = px[still] - base
    sig = float(np.std(devN))
    dL = float(np.median(L - base))
    dR = float(np.median(R - base))
    return dict(det=1.0 - np.isnan(px).sum() / len(px),
                npts=float(np.mean(npts)),
                sigma=sig, dL=dL, dR=dR,
                snrL=abs(dL) / max(sig, 1e-6),
                snrR=abs(dR) / max(sig, 1e-6))


def main():
    fs = files()
    print('文件 %d 个' % len(fs))
    print()
    print('%-6s %-8s %-8s %-8s %-8s %-8s %-8s %-8s'
          % ('cfar', 'snr', '检出率', '点/帧', '静息σ', 'LEFT', 'RIGHT', 'SNR_L'))
    print('-' * 76)
    out = {}
    for cfar, snr, tag in CONFIGS:
        rows = []
        for p, labp in fs:
            meta = read_meta(labp)
            band = meta.get('band') or [1.36, 2.04]
            labels = lt.load_labels(labp)
            sp = [(l['action'], l['t_start'], l['t_end'])
                  for l in labels if l.get('action') in ('LEFT', 'RIGHT')]
            px, npts = run(p, band, cfar, snr)
            st = stats(px, npts, sp)
            if st:
                rows.append(st)
        det = np.mean([r['det'] for r in rows])
        npf = np.mean([r['npts'] for r in rows])
        sig = np.mean([r['sigma'] for r in rows])
        dL = np.mean([abs(r['dL']) for r in rows])
        dR = np.mean([abs(r['dR']) for r in rows])
        snrL = dL / max(sig, 1e-6)
        print('%-6.1f %-8.1f %-8s %-8.2f %-8.3f %-8.3f %-8.3f %-8.2f  %s'
              % (cfar, snr, '%.0f%%' % (100 * det), npf, sig, dL, dR, snrL, tag))
        out[tag] = dict(cfar=cfar, snr_db=snr, det=float(det), npts=float(npf),
                        sigma=float(sig), dL=float(dL), dR=float(dR),
                        snr_l=float(snrL))

    best = max(out.items(), key=lambda kv: kv[1]['snr'])
    print()
    print('最佳信噪比：%s（SNR_L %.2f）' % (best[0], best[1]['snr']))
    print('判读：SNR < 1 表示信号仍小于噪声，规则法不成立；要 > 1.5 才谈得上稳定分类。')

    od = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(od, exist_ok=True)
    with open(os.path.join(od, 'tune-dsp.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('已写 outputs/lane-eval/tune-dsp.json')


if __name__ == '__main__':
    main()
