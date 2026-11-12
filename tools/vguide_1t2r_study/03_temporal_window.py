# -*- coding: utf-8 -*-
"""第三层：从「单帧」转到「时间窗」，验证"跳抖、蹲平"这个观察。

现场观察：v_r 曲线上，跳的波峰仍在抖动，蹲的波峰相对平稳。
这里把 5 帧滑窗内的 std / 一阶差分 / 过零率 / 谱宽均值等作为特征，
再看跳与蹲的 Cohen's d 和 AUC，并过滤掉不活跃帧（nz<=0.3）。

结论：单帧谱宽本身已足够（d=1.88），时间域特征没有带来额外增益，
因此实时判定采用「5 帧滑窗 + 谱宽均值」而不是更复杂的时序模型。

用法：
    python tools/vguide_1t2r_study/03_temporal_window.py [数据目录]
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

BAND = (1, 4)
print('逐帧提特征…')
vr, width, nz, energy, ent = [], [], [], [], []
idx_v = (np.arange(v.CHIRPS) - v.CHIRPS // 2).astype(float) * v.V_RES
for i in range(len(fr)):
    f = v.frame_feats(fr[i], BAND)
    if f is None:
        vr.append(0.0); width.append(0.0); nz.append(0.0)
        energy.append(0.0); ent.append(0.0)
        continue
    vr.append(f['v_r'])
    energy.append(f['band_power'])
    sp0 = np.fft.fftshift(np.fft.fft(fr[i][0] * v.WIN[:, None], axis=0), axes=0)
    sp1 = np.fft.fftshift(np.fft.fft(fr[i][1] * v.WIN[:, None], axis=0), axes=0)
    P = (np.abs(sp0) ** 2 + np.abs(sp1) ** 2)[:, BAND[0]:BAND[1]].sum(axis=1)
    tot = P.sum() or 1e-9
    vb = (P * idx_v).sum() / tot
    width.append(float(np.sqrt((P * (idx_v - vb) ** 2).sum() / tot)))
    nz.append(float(1.0 - P[np.abs(idx_v) < 0.15].sum() / tot))
    p = P / tot
    ent.append(float(-(p * np.log(p + 1e-12)).sum() / np.log(len(p))))

vr = np.array(vr); width = np.array(width); nz = np.array(nz)
energy = np.array(energy); ent = np.array(ent)

print('\n=== 段级时序统计（跳应比蹲"抖"） ===')
print('%-8s %7s %9s %9s %9s %9s' %
      ('段', '帧', 'v_r std', '|Δv_r|', '过零率', '|Δ能量|%'))
for name in ('base1', 'jump', 'base2', 'duck', 'base3'):
    m = lab == name
    if not m.any():
        continue
    x = vr[m]
    d = np.diff(x)
    zc = float(np.mean(np.diff(np.sign(x)) != 0)) if len(x) > 1 else 0.0
    e = energy[m]
    de = float(np.mean(np.abs(np.diff(e) / (np.mean(e) + 1e-9)))) if len(e) > 1 else 0.0
    print('%-8s %7d %+9.3f %9.3f %9.3f %9.1f' %
          (name, m.sum(), np.std(x), np.mean(np.abs(d)) if len(d) else 0, zc, 100 * de))

W = 5
feats = {}


def roll(a, fn):
    out = np.full(len(a), np.nan)
    for i in range(len(a)):
        lo = max(0, i - W + 1)
        out[i] = fn(a[lo:i + 1])
    return out


feats['roll_std_vr'] = roll(vr, np.std)
feats['roll_diff_vr'] = roll(vr, lambda a: np.mean(np.abs(np.diff(a))) if len(a) > 1 else 0.0)
feats['roll_zc_vr'] = roll(vr, lambda a: np.mean(np.diff(np.sign(a)) != 0) if len(a) > 1 else 0.0)
feats['roll_std_width'] = roll(width, np.std)
feats['roll_mean_width'] = roll(width, np.mean)
feats['roll_std_nz'] = roll(nz, np.std)
feats['roll_mean_ent'] = roll(ent, np.mean)
feats['roll_diff_energy'] = roll(
    energy, lambda a: np.mean(np.abs(np.diff(a) / (np.mean(a) + 1e-9))) if len(a) > 1 else 0.0)


def auc(a, b, seed=0):
    if len(a) == 0 or len(b) == 0:
        return float('nan')
    rng = np.random.default_rng(seed)
    aa = rng.choice(a, 5000, replace=True)
    bb = rng.choice(b, 5000, replace=True)
    return float((aa > bb).mean() + 0.5 * (aa == bb).mean())


print('\n=== 滑窗(5帧) 特征：jump vs duck ===')
print('%-18s %9s %9s %9s %9s' % ('特征', 'jump均', 'duck均', 'Cohen d', 'AUC'))
jm = lab == 'jump'
dm = lab == 'duck'
for k, arr in feats.items():
    a = arr[jm & ~np.isnan(arr)]
    b = arr[dm & ~np.isnan(arr)]
    if not len(a) or not len(b):
        continue
    sp = np.sqrt((np.var(a) + np.var(b)) / 2) or 1e-9
    print('%-18s %+9.4f %+9.4f %+9.2f %8.1f%%' %
          (k, np.mean(a), np.mean(b), (np.mean(a) - np.mean(b)) / sp, 100 * auc(a, b)))

print('\n=== 仅动作活跃帧(nz>0.3) ===')
act = nz > 0.3
for k, arr in feats.items():
    a = arr[jm & act & ~np.isnan(arr)]
    b = arr[dm & act & ~np.isnan(arr)]
    if len(a) < 10 or len(b) < 10:
        continue
    sp = np.sqrt((np.var(a) + np.var(b)) / 2) or 1e-9
    print('%-18s %+9.4f %+9.4f %+9.2f %8.1f%%  (n=%d/%d)' %
          (k, np.mean(a), np.mean(b), (np.mean(a) - np.mean(b)) / sp,
           100 * auc(a, b, 3), len(a), len(b)))
