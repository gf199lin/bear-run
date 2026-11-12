# -*- coding: utf-8 -*-
"""第四层：三分类（静息/跳/蹲）留一轮交叉验证，排除"是不是要三分类"。

规则：nz 5 帧滑窗 > g → 有动作；动作期间谱宽滑窗 > thr → 跳，否则蹲。
g 与 thr 只用训练轮确定，再测留出的那一轮，避免自证。

结论（第一批 3 轮数据）：
    三轮平均总体准确率约 84%，跳/蹲阈值稳定在 0.25 附近；
    但总体准确率被静息帧"稀释"——真正要看的是动作事件级（见 05）。

用法：
    python tools/vguide_1t2r_study/04_three_class.py [数据目录]
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
idx_v = (np.arange(v.CHIRPS) - v.CHIRPS // 2).astype(float) * v.V_RES

W = 5
nz = np.zeros(len(fr)); width = np.zeros(len(fr)); ent = np.zeros(len(fr))
for i in range(len(fr)):
    sp0 = np.fft.fftshift(np.fft.fft(fr[i][0] * v.WIN[:, None], axis=0), axes=0)
    sp1 = np.fft.fftshift(np.fft.fft(fr[i][1] * v.WIN[:, None], axis=0), axes=0)
    P = (np.abs(sp0) ** 2 + np.abs(sp1) ** 2)[:, BAND[0]:BAND[1]].sum(axis=1)
    tot = P.sum() or 1e-9
    vb = (P * idx_v).sum() / tot
    width[i] = float(np.sqrt((P * (idx_v - vb) ** 2).sum() / tot))
    nz[i] = float(1.0 - P[np.abs(idx_v) < 0.15].sum() / tot)
    p = P / tot
    ent[i] = float(-(p * np.log(p + 1e-12)).sum() / np.log(len(p)))

ws = np.full(len(width), np.nan)
es = np.full(len(ent), np.nan)
for i in range(len(width)):
    lo = max(0, i - W + 1)
    ws[i] = float(np.mean(width[lo:i + 1]))
    es[i] = float(np.mean(ent[lo:i + 1]))

# 连续同名段编号；本批每轮 11 段（base1/jump/base2/duck/base3 + 自检段）
seg_id = np.zeros(len(lab), dtype=int)
prev, cnt = None, -1
for i, nm in enumerate(lab):
    if not (nm.startswith('base') or nm.startswith('jump')
            or nm.startswith('duck') or nm.startswith('idle')):
        seg_id[i] = -1
        continue
    if nm != prev:
        cnt += 1
        prev = nm
    seg_id[i] = cnt

rnd = np.full(len(lab), -1)
order = {}
for i in range(len(lab)):
    s = seg_id[i]
    if s < 0:
        continue
    order.setdefault(s, lab[i])
    rnd[i] = s // 11
print('段序列:', ['%d:%s' % (k, x) for k, x in sorted(order.items())])
print('轮次分布:', {int(r): int((rnd == r).sum()) for r in np.unique(rnd) if r >= 0})


def truth_of(nm):
    if nm.startswith('base') or nm == 'idle':
        return 'IDLE'
    if nm == 'jump':
        return 'JUMP'
    if nm == 'duck':
        return 'DUCK'
    return None


truth = np.array([truth_of(nm) or '' for nm in lab])
valid = (truth != '') & ~np.isnan(ws)
print('有效帧:', int(valid.sum()), '/', len(lab))

res_all = []
for test_r in (0, 1, 2):
    tr = valid & (rnd != test_r)
    te = valid & (rnd == test_r)
    wj = ws[tr & (truth == 'JUMP')]
    wd = ws[tr & (truth == 'DUCK')]
    if not len(wj) or not len(wd):
        continue
    thr = float((np.mean(wj) + np.mean(wd)) / 2)
    nb = nz[tr & np.isin(lab, ['base2', 'base3'])]
    g = float(np.percentile(nb, 99)) + 0.05 if len(nb) else 0.15
    pred = np.where(nz[te] < g, 'IDLE', np.where(ws[te] > thr, 'JUMP', 'DUCK'))
    t = truth[te]
    acc = float((pred == t).mean())
    print('\n--- 留第 %d 轮 ---' % (test_r + 1))
    print('  门控 g=%.3f  谱宽阈值 thr=%.3f' % (g, thr))
    print('  总体准确率 %.1f%%' % (100 * acc))
    print('  真值\\预测  %6s %6s %6s' % ('IDLE', 'JUMP', 'DUCK'))
    for a in ('IDLE', 'JUMP', 'DUCK'):
        row = [int(((t == a) & (pred == b)).sum()) for b in ('IDLE', 'JUMP', 'DUCK')]
        print('  %-10s %6d %6d %6d' % (a, row[0], row[1], row[2]))
    for a in ('JUMP', 'DUCK'):
        na = int((t == a).sum())
        hit = int(((t == a) & (pred == a)).sum())
        print('  %s 召回 %.1f%% (%d/%d)' % (a, 100 * hit / max(na, 1), hit, na))
    res_all.append((acc, thr, g))

if res_all:
    print('\n=== 三轮平均准确率 %.1f%% ===' % (100 * np.mean([r[0] for r in res_all])))
    print('阈值稳定在 thr≈%.3f, g≈%.3f' %
          (np.mean([r[1] for r in res_all]), np.mean([r[2] for r in res_all])))

print('\n=== 对照：用谱熵代替谱宽 ===')
for test_r in (0, 1, 2):
    tr = valid & (rnd != test_r)
    te = valid & (rnd == test_r)
    ej = es[tr & (truth == 'JUMP')]
    ed = es[tr & (truth == 'DUCK')]
    if not len(ej) or not len(ed):
        continue
    thr = float((np.mean(ej) + np.mean(ed)) / 2)
    nb = nz[tr & (truth == 'IDLE')]
    g = float(np.percentile(nb, 95)) + 0.05
    pred = np.where(nz[te] < g, 'IDLE', np.where(es[te] > thr, 'JUMP', 'DUCK'))
    print('  留第 %d 轮：准确率 %.1f%%' % (test_r + 1, 100 * (pred == truth[te]).mean()))
