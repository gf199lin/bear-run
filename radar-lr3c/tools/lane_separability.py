# -*- coding: utf-8 -*-
r"""左右移逐帧可分性诊断：LEFT / RIGHT / 静息 的 px 分布到底重不重叠。

动机：xbase 基线 LEFT 只有 25.5%、RIGHT 92.7%。在训练任何模型之前，
必须先分清两件事 ——
  (a) 特征本身可分，只是阈值/窗口写偏了 → 改规则就行；
  (b) 特征本身重叠 → 机器学习也救不了，得改几何或换特征。

做法（离线，允许用未来帧，仅用于诊断不可分性）：
  1. 逐帧取带内最强点的 px（与产线 top_k=1 同口径）。
  2. 基线 = 所有「非动作帧」px 的中位数（每文件一个）。
  3. 动作帧的 dev = px - 基线，按 LEFT / RIGHT 分组看分布。
  4. 用单一阈值判方向，扫阈值求最佳准确率（这是单特征上限）。

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

FRAME_PERIOD = 0.1295
CFAR = 2.6
SNR = 7.0


CHUNK = 65536  # 必须分块：StreamParser.feed 内部缓冲是 O(n²)，
              # 一次喂 9.5 MB 会跑十几分钟还出不来；64 KB 分块整文件 2.5 s。


def extract(path, band):
    data = open(path, 'rb').read()
    parser, asm = make_protocol('1t2r')
    proc = PointCloudProcessor(cfar_scale=CFAR, min_snr=SNR, min_range=0.681)
    lo, hi = band
    px = []
    n_in = []
    t = []
    for i in range(0, len(data), CHUNK):
        for fr in parser.feed(data[i:i + CHUNK]):
            z = asm.push(fr)
            if z is None:
                continue
            z0, z1 = z
            pts, _ = proc.process(z0, z1)
            sel = [p for p in pts if lo <= p['y'] <= hi]
            if sel:
                # 最强点（pts 已按能量降序）
                px.append(sel[0]['x'])
                n_in.append(len(sel))
            else:
                px.append(np.nan)
                n_in.append(0)
            t.append(len(t) * FRAME_PERIOD)
    return np.array(t), np.array(px, dtype=float), np.array(n_in)


def spans(labels):
    return [(l['action'], l['t_start'], l['t_end'])
            for l in labels if l.get('action') in ('LEFT', 'RIGHT')]


def tag_frames(t, sp, pad=0.0):
    """帧标签：'LEFT'/'RIGHT'/'NONE'。"""
    y = np.array(['NONE'] * len(t), dtype=object)
    for act, t0, t1 in sp:
        m = (t >= t0 - pad) & (t <= t1 + pad)
        y[m] = act
    return y


SETS = ['seg-lr2/1', 'seg-lr2/jqz', 'oncehot-oncehot/lr']


def read_meta(labp):
    """labels 的第一行是 _meta（load_labels 会丢掉它）。

    坑：band 是按站位标定的 —— 0.70 m 用 [0.70, 1.35]，1.00 m 用 [1.36, 2.04]。
    用错带会导致 90% 以上的帧检不到人（NaN），把"检不到"误判成"不可分"。
    """
    with open(labp, encoding='utf-8') as f:
        first = json.loads(f.readline())
    return first.get('_meta', {})


def main():
    rows = []
    allL, allR, allN = [], [], []
    for rel in SETS:
        base = os.path.join(ROOT, '.deepworks', 'tmp', rel)
        for p in sorted(glob.glob(os.path.join(base, '**', '*.bin'), recursive=True)):
            labp = os.path.splitext(p)[0] + '.labels.jsonl'
            if not os.path.exists(labp):
                continue
            labels = lt.load_labels(labp)
            meta = read_meta(labp)
            band = meta.get('band') or [1.36, 2.04]
            t, px, nin = extract(p, band)
            det = 1.0 - float(np.isnan(px).sum()) / max(1, len(px))
            if det < 0.5:
                print('   !! %s 检出率仅 %.0f%%，band=%s 可疑，结论不可用'
                      % (os.path.basename(p), 100 * det, band))
            sp = spans(labels)
            y = tag_frames(t, sp)
            base_mask = (y == 'NONE') & ~np.isnan(px)
            if base_mask.sum() < 30:
                continue
            base_px = float(np.median(px[base_mask]))
            dev = px - base_px
            L = dev[(y == 'LEFT') & ~np.isnan(dev)]
            R = dev[(y == 'RIGHT') & ~np.isnan(dev)]
            N = dev[(y == 'NONE') & ~np.isnan(dev)]
            allL.append(L); allR.append(R); allN.append(N)
            print('%-34s band=%.2f-%.2f 基线px=%+.3f  静息σ=%.3f'
                  % (os.path.basename(p), band[0], band[1], base_px, np.std(N)))
            print('   LEFT  n=%3d  dev中位%+.3f  p10%+.3f  p90%+.3f'
                  % (len(L), np.median(L), np.percentile(L, 10), np.percentile(L, 90)))
            print('   RIGHT n=%3d  dev中位%+.3f  p10%+.3f  p90%+.3f'
                  % (len(R), np.median(R), np.percentile(R, 10), np.percentile(R, 90)))
            rows.append(dict(file=os.path.relpath(p, ROOT), base_px=base_px,
                             nL=int(len(L)), nR=int(len(R)),
                             medL=float(np.median(L)), medR=float(np.median(R))))

    L = np.concatenate(allL) if allL else np.array([])
    R = np.concatenate(allR) if allR else np.array([])
    N = np.concatenate(allN) if allN else np.array([])
    print()
    print('=' * 74)
    print('合计  LEFT n=%d  RIGHT n=%d  静息 n=%d' % (len(L), len(R), len(N)))
    print('  LEFT  dev 中位 %+.3f  IQR [%+.3f, %+.3f]'
          % (np.median(L), np.percentile(L, 25), np.percentile(L, 75)))
    print('  RIGHT dev 中位 %+.3f  IQR [%+.3f, %+.3f]'
          % (np.median(R), np.percentile(R, 25), np.percentile(R, 75)))

    # 单阈值最优（正 → LEFT，负 → RIGHT），只统计动作帧
    best = (0, None)
    for thr in np.linspace(-0.6, 0.6, 241):
        acc = ((L > thr).sum() + (R < -thr).sum()) / (len(L) + len(R))
        if acc > best[0]:
            best = (acc, thr)
    print('  单特征最佳: thr=%.3f  方向准确 %.1f%%  (LEFT %.1f%% / RIGHT %.1f%%)'
          % (best[1], 100 * best[0],
             100 * (L > best[1]).mean(), 100 * (R < -best[1]).mean()))

    # 重叠面积（用静息分布做参照）
    print('  静息 |dev| p90 = %.3f  （动作幅度要明显大于它才有信噪比）'
          % np.percentile(np.abs(N), 90))
    print('=' * 74)

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'separability.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(rows=rows,
                       L=dict(n=len(L), med=float(np.median(L)),
                              p10=float(np.percentile(L, 10)),
                              p90=float(np.percentile(L, 90))),
                       R=dict(n=len(R), med=float(np.median(R)),
                              p10=float(np.percentile(R, 10)),
                              p90=float(np.percentile(R, 90))),
                       N=dict(n=len(N), p90_abs=float(np.percentile(np.abs(N), 90))),
                       best=dict(acc=float(best[0]), thr=float(best[1]))),
                  f, ensure_ascii=False, indent=1)
    print('已写 outputs/lane-eval/separability.json')


if __name__ == '__main__':
    main()
