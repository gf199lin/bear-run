# -*- coding: utf-8 -*-
"""第五层：事件级回放——最终采用的评法。

为什么不再看帧级准确率：静息帧占绝大多数，帧级准确率会被"稀释"，
真正影响游戏体验的是「一次动作有没有被检成一个事件、这个事件判对没有」。

流程：
    nz 5 帧滑窗上穿门控 g=0.30 且持续 >=3 帧 = 一个动作事件
    → 事件内谱宽滑窗均值 > thr ? JUMP : DUCK

评估：
    留一轮：用另外两轮估 thr，再测留出轮；
    事件归属真值按「事件起始帧所在段」判定。

用法：
    python tools/vguide_1t2r_study/05_event_replay.py [数据目录]
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

n = len(fr)
nz = np.zeros(n); width = np.zeros(n)
for i in range(n):
    sp0 = np.fft.fftshift(np.fft.fft(fr[i][0] * v.WIN[:, None], axis=0), axes=0)
    sp1 = np.fft.fftshift(np.fft.fft(fr[i][1] * v.WIN[:, None], axis=0), axes=0)
    P = (np.abs(sp0) ** 2 + np.abs(sp1) ** 2)[:, BAND[0]:BAND[1]].sum(axis=1)
    tot = P.sum() or 1e-9
    vb = (P * idx_v).sum() / tot
    width[i] = float(np.sqrt((P * (idx_v - vb) ** 2).sum() / tot))
    nz[i] = float(1.0 - P[np.abs(idx_v) < 0.15].sum() / tot)


def roll_mean(a, w=5):
    out = np.empty(len(a))
    for i in range(len(a)):
        out[i] = np.mean(a[max(0, i - w + 1):i + 1])
    return out


nzs = roll_mean(nz, 5)
ws = roll_mean(width, 5)


def truth_of(nm):
    if nm in ('jump', 'check_jump'):
        return 'JUMP'
    if nm in ('duck', 'check_duck'):
        return 'DUCK'
    if nm.startswith('base') or nm in ('idle', 'check_stand1', 'check_stand2', 'check_idle'):
        return 'IDLE'
    return None


truth = np.array([truth_of(x) or '' for x in lab])

seg_id = np.full(n, -1)
prev, cnt = None, -1
for i, nm in enumerate(lab):
    if truth[i] == '':
        seg_id[i] = -1
        continue
    if nm != prev:
        cnt += 1
        prev = nm
    seg_id[i] = cnt
# 本批布局：自检段在前（<11），之后每 11 段为一轮正式采集
rnd = np.where(seg_id < 0, -1,
               np.where(seg_id < 11, 0, np.where(seg_id < 22, 1, 2)))

print('段真值分布:', {k: int((truth == k).sum()) for k in ('IDLE', 'JUMP', 'DUCK')})


def detect(g, use_rnd=None):
    events = []
    i = 0
    while i < n:
        ok = truth[i] != '' and (use_rnd is None or rnd[i] in use_rnd)
        if ok and nzs[i] > g and (i == 0 or nzs[i - 1] <= g):
            j = i
            while j < n and nzs[j] > g:
                j += 1
            if j - i >= 3:
                events.append((i, j))
            i = j
        else:
            i += 1
    return events


def classify(events, thr):
    return [(i, j, float(np.mean(ws[i:j])),
             'JUMP' if float(np.mean(ws[i:j])) > thr else 'DUCK')
            for i, j in events]


def estimate_thr(train_rnds):
    ev = classify(detect(0.30, train_rnds), 1e9)
    wj = [e[2] for e in ev if truth[e[0]] == 'JUMP']
    wd = [e[2] for e in ev if truth[e[0]] == 'DUCK']
    if not wj or not wd:
        return 0.25, np.nan, np.nan
    return float((np.mean(wj) + np.mean(wd)) / 2), float(np.mean(wj)), float(np.mean(wd))


print('\n=== 事件级留一验证 ===')
for test_r in (0, 1, 2):
    tr = [r for r in (0, 1, 2) if r != test_r]
    thr, mj, md = estimate_thr(tr)
    ce = classify(detect(0.30, [test_r]), thr)
    act = [e for e in ce if truth[e[0]] in ('JUMP', 'DUCK')]
    ok = sum(1 for e in act if e[3] == truth[e[0]])
    fa = sum(1 for e in ce if truth[e[0]] == 'IDLE')
    print('\n--- 留第 %d 轮 ---' % (test_r + 1))
    print('  thr=%.3f（训练: 跳宽 %.3f / 蹲宽 %.3f）' % (thr, mj, md))
    print('  动作段事件 %d 个，分类正确 %d 个 → %.1f%%' %
          (len(act), ok, 100 * ok / max(len(act), 1)))
    print('  静息段误报 %d 个事件（静息帧 %d）' % (fa, int((truth == 'IDLE').sum())))
    jj = [e for e in act if truth[e[0]] == 'JUMP']
    dd = [e for e in act if truth[e[0]] == 'DUCK']
    if jj:
        print('    跳: %d 个事件，判对 %d' % (len(jj), sum(1 for e in jj if e[3] == 'JUMP')))
    if dd:
        print('    蹲: %d 个事件，判对 %d' % (len(dd), sum(1 for e in dd if e[3] == 'DUCK')))

print('\n=== 全部数据（含自检段）事件清单 ===')
thr, mj, md = estimate_thr([0, 1, 2])
print('全局 thr=%.3f（跳宽 %.3f / 蹲宽 %.3f）' % (thr, mj, md))
for i, j, w, c in classify(detect(0.30), thr):
    print('  帧 %4d-%4d  真值 %-5s  谱宽 %.3f  → %s  %s' %
          (i, j - 1, truth[i] or '(其他)', w, c, '✓' if c == truth[i] else ''))
