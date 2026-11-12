# -*- coding: utf-8 -*-
"""第一层证伪：为什么「逐帧平均径向速度」不可用。

把每段的平均多普勒谱摊开，看能量落在哪：
- 静息段 DC（|v|<0.15 m/s）占比 97%~100%，几乎全在零多普勒；
- 动作段 DC 占比掉到 42%~44%，但正负两侧同时升高、峰值接近对称。

结论：一次跳/蹲在 129.5 ms 的 CPI 内走完加减速，正负多普勒互相抵消，
用 Σ(P·v)/ΣP 得到的 v_r 必然≈0，符号没有物理意义。

用法：
    python tools/vguide_1t2r_study/01_dc_dilution.py [数据目录]
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
print('labels:', sorted(set(lab.tolist())))

idx_v = (np.arange(v.CHIRPS) - v.CHIRPS // 2).astype(float) * v.V_RES
band = (1, 4)


def seg_spectrum(mask):
    acc = None
    n = 0
    for f in fr[mask]:
        sp0 = np.fft.fftshift(np.fft.fft(f[0] * v.WIN[:, None], axis=0), axes=0)
        sp1 = np.fft.fftshift(np.fft.fft(f[1] * v.WIN[:, None], axis=0), axes=0)
        P = (np.abs(sp0) ** 2 + np.abs(sp1) ** 2)[:, band[0]:band[1]].sum(axis=1)
        acc = P if acc is None else acc + P
        n += 1
    return acc / max(n, 1), n


print('\n=== 段内平均多普勒谱（bin %d-%d 能量求和） ===' % band)
print('%-8s %5s %8s %9s %9s %9s %9s' %
      ('段', '帧', 'DC占比', '正峰v', '正峰占比', '负峰v', '负峰占比'))
for name in ('base1', 'base2', 'base3', 'jump', 'duck'):
    mask = lab == name
    if not mask.any():
        continue
    sp, n = seg_spectrum(mask)
    tot = sp.sum()
    dc = np.abs(idx_v) < 0.15
    pos = np.where(idx_v > 0.15, sp, 0.0)
    neg = np.where(idx_v < -0.15, sp, 0.0)
    ip, ineg = int(np.argmax(pos)), int(np.argmax(neg))
    print('%-8s %5d %7.1f%% %9.2f %8.1f%% %9.2f %8.1f%%' %
          (name, n, 100 * sp[dc].sum() / tot, idx_v[ip], 100 * pos[ip] / tot,
           idx_v[ineg], 100 * neg[ineg] / tot))

print('\n=== 两种估计方式对比（每段平均） ===')
print('%-8s %14s %14s %14s' % ('段', '线性加权v_r', '剔除DC后v_r', '非零能量占比'))


def two_estimates(mask):
    lin, clean, share = [], [], []
    for f in fr[mask]:
        sp0 = np.fft.fftshift(np.fft.fft(f[0] * v.WIN[:, None], axis=0), axes=0)
        sp1 = np.fft.fftshift(np.fft.fft(f[1] * v.WIN[:, None], axis=0), axes=0)
        P = (np.abs(sp0) ** 2 + np.abs(sp1) ** 2)[:, band[0]:band[1]].sum(axis=1)
        tot = P.sum()
        if tot <= 0:
            continue
        lin.append(float((P * idx_v).sum() / tot))
        nz = np.abs(idx_v) > 0.15
        if P[nz].sum() > 0:
            clean.append(float((P[nz] * idx_v[nz]).sum() / P[nz].sum()))
            share.append(float(P[nz].sum() / tot))
    return (np.mean(lin), np.mean(clean), np.mean(share)) if lin else (0, 0, 0)


for name in ('base1', 'base2', 'base3', 'jump', 'duck'):
    mask = lab == name
    if not mask.any():
        continue
    a, b, c = two_estimates(mask)
    print('%-8s %+13.3f %+13.3f %13.1f%%' % (name, a, b, 100 * c))
