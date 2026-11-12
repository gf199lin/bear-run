# -*- coding: utf-8 -*-
"""第二层：在单帧上找候选特征，看哪个能把跳和蹲分开。

候选四个：零多普勒外能量占比、多普勒谱宽（二阶矩）、归一化谱熵、正负不对称度。
每个都算 Cohen's d 和抽样 AUC，同时给出「动作 vs 站定」的检出能力。

结论（第一批数据，5 帧滑窗、仅活跃帧）：
    谱宽 d=1.88 / AUC 91.8%，熵 d=1.27 / AUC 81.7%，
    其余（nz_share、asym）在跳/蹲之间几乎无区分力。

用法：
    python tools/vguide_1t2r_study/02_single_frame_features.py [数据目录]
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import vguide_1t2r as v  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))
D = (sys.argv[1] if len(sys.argv) > 1 else
     os.path.join(ROOT, 'data', 'vguide_1t2r', '20261112-203419'))

z = np.load(os.path.join(D, 'frames.npz'))
fr = z['z']
lab = z['label'].astype(str)
print('数据：', os.path.basename(os.path.normpath(D)))

idx_v = (np.arange(v.CHIRPS) - v.CHIRPS // 2).astype(float) * v.V_RES
band = (1, 4)


def feats_of(f):
    sp0 = np.fft.fftshift(np.fft.fft(f[0] * v.WIN[:, None], axis=0), axes=0)
    sp1 = np.fft.fftshift(np.fft.fft(f[1] * v.WIN[:, None], axis=0), axes=0)
    P2 = np.abs(sp0) ** 2 + np.abs(sp1) ** 2
    P = P2[:, band[0]:band[1]].sum(axis=1)
    tot = float(P.sum())
    if tot <= 0:
        return None
    dc = float(P[np.abs(idx_v) < 0.15].sum())
    nz_share = 1.0 - dc / tot
    vbar = float((P * idx_v).sum() / tot)
    width = float(np.sqrt((P * (idx_v - vbar) ** 2).sum() / tot))
    p = P / tot
    ent = float(-(p * np.log(p + 1e-12)).sum() / np.log(len(p)))
    pos = float(P[idx_v > 0.15].sum())
    neg = float(P[idx_v < -0.15].sum())
    asym = (pos - neg) / max(pos + neg, 1e-9)
    return dict(nz_share=nz_share, width=width, energy=tot, ent=ent, asym=asym)


KEYS = ['nz_share', 'width', 'ent', 'asym']
rows = {k: [] for k in KEYS}
tags = []
for i in range(len(fr)):
    r = feats_of(fr[i])
    if r is None:
        continue
    for k in KEYS:
        rows[k].append(r[k])
    tags.append(lab[i])
tags = np.array(tags)
for k in KEYS:
    rows[k] = np.array(rows[k], dtype=float)


def stats(x):
    return float(np.mean(x)), float(np.std(x))


def auc_of(a, b, seed):
    rng = np.random.default_rng(seed)
    aa = rng.choice(a, 4000, replace=True)
    bb = rng.choice(b, 4000, replace=True)
    return float((aa > bb).mean() + 0.5 * (aa == bb).mean())


print('\n=== 单帧特征：jump vs duck ===')
print('%-10s %16s %16s %10s %10s' % ('特征', 'jump', 'duck', 'Cohen d', 'AUC'))
for k in KEYS:
    a = rows[k][tags == 'jump']
    b = rows[k][tags == 'duck']
    if not len(a) or not len(b):
        continue
    ma, sa = stats(a)
    mb, sb = stats(b)
    sp = np.sqrt((sa ** 2 + sb ** 2) / 2) or 1e-9
    print('%-10s %+7.4f±%-6.4f %+7.4f±%-6.4f %+9.2f %9.2f%%' %
          (k, ma, sa, mb, sb, (ma - mb) / sp, 100 * auc_of(a, b, 0)))

print('\n=== 动作 vs 站定（检出能力） ===')
act = np.isin(tags, ['jump', 'duck'])
base = np.isin(tags, ['base1', 'base2', 'base3', 'idle'])
for k in KEYS:
    a = rows[k][act]
    b = rows[k][base]
    if not len(a) or not len(b):
        continue
    ma, sa = stats(a)
    mb, sb = stats(b)
    sp = np.sqrt((sa ** 2 + sb ** 2) / 2) or 1e-9
    print('%-10s 动作 %+7.4f  站定 %+7.4f  d=%+6.2f  AUC=%.1f%%' %
          (k, ma, mb, (ma - mb) / sp, 100 * auc_of(a, b, 1)))

print('\n=== nz_share 时间形态（每段前 30 帧，3 帧滑动） ===')
for name in ('base1', 'jump', 'base2', 'duck'):
    sel = np.where(tags == name)[0][:30]
    if len(sel) < 3:
        continue
    x = rows['nz_share'][sel]
    sm = np.convolve(x, np.ones(3) / 3, mode='valid')
    print('%-6s ' % name + ' '.join('%.2f' % t for t in sm[:20]))
