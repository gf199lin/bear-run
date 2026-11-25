# -*- coding: utf-8 -*-
"""1T2R 跳/蹲训练与诚实评估（单条命令复现全部结论）。

数据
----
data/jumpduck_battery/<会话>/frames.npz
    z     (N, 2, 64, 8) complex64  原始立方体（1T2R，两 RX × 距离 × chirp）
    label (N,) str                 逐帧真值：rest/jump/duck/walk/bend/turn/alt/left/right
逐帧真值来自同一目录的 labels.json（按块顺序展开）。

特征
----
每帧过产线 DSP（radar_dsp.PointCloudProcessor）取点云，聚合成 11 维物理量
（点数、距离、加权径向速度、正负多普勒占比、速度离散度、方位角），
再取 4 帧因果窗口的 均值/标准差/首尾差/最大/最小 = 55 维。

评估（留一会话，4 折）
----
1. 窗口级：每个约 0.52 s 窗口判一次
2. 段级：一个动作段内所有窗口概率取均值，判一次（贴近「一次动作一次事件」）
3. 配对检验：Δ = 动作段均值 − 时间最近的 rest 段均值，抵消慢漂移
4. 置换检验：会话内打乱段标签 200 次，看真实准确率是否在零分布之外

⚠ 结论边界（写进 docs/jump-duck-1t2r-findings.md）
   - 逐帧单变量几乎不可分（AUC 0.43–0.57）
   - 段级 8–9 成，但每个会话里 jump 整块在前、duck 整块在后，
     置换检验无法排除「时间漂移」这一混淆；配对检验是目前最强的证据（AUC 0.738）
   - **未接入实时链路，不作为已实现功能**；MVP 跳/蹲仍走 2T4R 俯仰角

用法
----
    python tools/train_jumpduck_1t2r.py               # 全部
    python tools/train_jumpduck_1t2r.py --perm 0      # 跳过置换检验（快）
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, HERE)

from radar_dsp import PointCloudProcessor  # noqa: E402

BATTERY = os.path.join(ROOT, 'data', 'jumpduck_battery')
CACHE = os.path.join(ROOT, '.deepworks', 'tmp', 'jd_feat_cache.npz')
OUT = os.path.join(ROOT, 'outputs', 'lane-eval')
MODEL = os.path.join(ROOT, 'radar-pointcloud', 'models', 'jumpduck_1t2r.json')
WIN = 4
FEAT_NAMES = ['n', 'r_med', 'r_top', 'v_w', 'v_top', 'v_abs', 'v_p90',
              'v_pos', 'v_neg', 'v_std', 'deg_med']
BLOCKS = ['mean', 'std', 'diff', 'max', 'min']


def col_names():
    return ['%s.%s' % (b, f) for b in BLOCKS for f in FEAT_NAMES]


# ------------------------------------------------------------------ 特征

def frame_feats(pts):
    if not pts:
        return None
    r = np.array([p['r'] for p in pts], float)
    v = np.array([p['v'] for p in pts], float)
    w = np.array([p['pa'] for p in pts], float)
    w = w / (w.sum() + 1e-12)
    vw = float((v * w).sum())
    top = np.argsort(-w)[:max(1, len(pts) // 3)]
    return [float(len(pts)), float(np.median(r)), float(np.median(r[top])),
            vw, float(np.median(v[top])), float((np.abs(v) * w).sum()),
            float(np.quantile(np.abs(v), 0.9)),
            float(w[v > 0.05].sum()), float(w[v < -0.05].sum()),
            float(np.sqrt((w * (v - vw) ** 2).sum())),
            float(np.median([p['deg'] for p in pts]))]


def load_arrays():
    if os.path.isfile(CACHE):
        z = np.load(CACHE, allow_pickle=True)
        return z['F'], z['L'], z['S'], z['B']
    proc = PointCloudProcessor(min_range=0.0, remove_clutter=True,
                               exclude_az=None, max_points=64)
    F, L, S, B = [], [], [], []
    for d in sorted(glob.glob(os.path.join(BATTERY, '*'))):
        npz = os.path.join(d, 'frames.npz')
        if not os.path.isfile(npz):
            continue
        z = np.load(npz, allow_pickle=True)
        Z, lab, name = z['z'], z['label'], os.path.basename(d)
        seg, prev = 0, None
        for i in range(len(Z)):
            lb = str(lab[i])
            if lb != prev:
                seg += 1
                prev = lb
            f = frame_feats(proc.process(Z[i, 0], Z[i, 1])[0])
            if f is None:
                continue
            F.append(f)
            L.append(lb)
            S.append(name)
            B.append(seg)
    F = np.array(F, np.float32)
    L, S, B = np.array(L), np.array(S), np.array(B, np.int32)
    np.savez_compressed(CACHE, F=F, L=L, S=S, B=B)
    return F, L, S, B


def windowize(F, L, S, B, w=WIN):
    rows, ly, sy, by = [], [], [], []
    for i in range(len(F)):
        j = i - w + 1
        if j < 0 or S[i] != S[j] or B[i] != B[j]:
            continue
        blk = F[j:i + 1]
        rows.append(list(blk.mean(0)) + list(blk.std(0)) +
                    list(blk[-1] - blk[0]) + list(blk.max(0)) + list(blk.min(0)))
        ly.append(L[i])
        sy.append(S[i])
        by.append(B[i])
    return np.array(rows, np.float64), np.array(ly), np.array(sy), np.array(by)


# ------------------------------------------------------------------ 模型

def fit_mlp(X, y, hidden=16, epochs=400, lr=0.08, l2=1e-3, seed=0):
    rng = np.random.default_rng(seed)
    n, k = X.shape
    W1 = rng.normal(0, np.sqrt(2.0 / k), (k, hidden))
    b1 = np.zeros(hidden)
    W2 = rng.normal(0, np.sqrt(2.0 / hidden), (hidden,))
    b2 = 0.0
    for _ in range(epochs):
        H = np.maximum(0.0, X @ W1 + b1)
        z = np.clip(H @ W2 + b2, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = (p - y) / n
        gH = np.outer(g, W2) * (H > 0)
        W2 -= lr * (H.T @ g + l2 * W2)
        b2 -= lr * g.sum()
        W1 -= lr * (X.T @ gH + l2 * W1)
        b1 -= lr * gH.sum(axis=0)
    return W1, b1, W2, b2


def proba(model, X):
    W1, b1, W2, b2 = model
    H = np.maximum(0.0, X @ W1 + b1)
    return 1.0 / (1.0 + np.exp(-np.clip(H @ W2 + b2, -30, 30)))


def seg_acc(p, y, sess, seg):
    order = {}
    for i in range(len(p)):
        order.setdefault((sess[i], seg[i]), []).append((p[i], y[i]))
    hit = sum(int((np.mean([a for a, _ in v]) >= .5) == (v[0][1] == 1))
              for v in order.values())
    per = {}
    for k, v in order.items():
        per.setdefault(k[0], []).append(
            int((np.mean([a for a, _ in v]) >= .5) == (v[0][1] == 1)))
    return hit / max(1, len(order)), len(order), per


def loso(X, y, sess, epoch=400):
    p = np.full(len(y), np.nan)
    for s in sorted(set(sess.tolist())):
        tr, te = sess != s, sess == s
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        mdl = fit_mlp((X[tr] - mu) / sd, y[tr].astype(float), epochs=epoch)
        p[te] = proba(mdl, (X[te] - mu) / sd)
    return p


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--perm', type=int, default=200)
    ap.add_argument('--delta', action='store_true',
                    help='用因果基线差特征（另存一份结果）')
    args = ap.parse_args()

    F, L, S, B = load_arrays()
    print('帧 %d  会话 %d  标签 %s' %
          (len(L), len(set(S.tolist())), sorted(set(L.tolist()))))

    if args.delta:
        D = np.empty_like(F, dtype=np.float64)
        for s in sorted(set(S.tolist())):
            idx = np.where(S == s)[0]
            sub = F[idx].astype(np.float64)
            for i in range(len(sub)):
                D[idx[i]] = (sub[i] - np.median(sub[max(0, i - 155):i + 1], 0)
                             if i >= 8 else 0.0)
        F = D

    X, y, sess, seg = windowize(F, L, S, B)
    m = np.isin(y, ['jump', 'duck'])
    X, y, sess, seg = X[m], (y[m] == 'jump').astype(int), sess[m], seg[m]
    print('窗口 %d  维度 %d  段 %d' % (len(y), X.shape[1],
                                     len(set(zip(sess, seg)))))

    p = loso(X, y, sess)
    acc_win = float(((p >= .5) == (y == 1)).mean())
    acc_seg, n_seg, per = seg_acc(p, y, sess, seg)
    print('\n=== 留一会话 ===')
    print('  窗口级 %5.1f%%   段级 %5.1f%% (%d 段)'
          % (100 * acc_win, 100 * acc_seg, n_seg))
    for k in sorted(per):
        print('    %-16s %d/%d' % (k[-14:], sum(per[k]), len(per[k])))
    res = dict(win_acc=round(acc_win, 4), win_n=int(len(y)),
               seg_acc=round(acc_seg, 4), seg_n=n_seg,
               per_session={str(k): [sum(v), len(v)] for k, v in per.items()})

    # ---- 置换检验
    perm = None
    if args.perm:
        rng = np.random.default_rng(7)
        segs_of = {}
        for i in range(len(y)):
            segs_of.setdefault((sess[i], seg[i]), []).append(i)
        keys = sorted(segs_of)
        null = []
        for _ in range(args.perm):
            lab = {}
            for s in sorted(set(sess.tolist())):
                ks = [k for k in keys if k[0] == s]
                v = [int(y[segs_of[k][0]]) for k in ks]
                rng.shuffle(v)
                lab.update(dict(zip(ks, v)))
            yy = np.array([lab[(sess[i], seg[i])] for i in range(len(y))])
            pp = loso(X, yy, sess, epoch=200)
            a, _, _ = seg_acc(pp, yy, sess, seg)
            null.append(a)
        null = np.array(null)
        pv = float((null >= acc_seg).mean())
        perm = dict(iters=args.perm, median=round(float(np.median(null)), 4),
                    p95=round(float(np.quantile(null, .95)), 4),
                    max=round(float(null.max()), 4), p_value=round(pv, 4))
        print('\n置换检验 %d 次：零分布 中位 %.3f p95 %.3f max %.3f | p≈%.3f'
              % (args.perm, perm['median'], perm['p95'], perm['max'], pv))
        print('  → %s（注意：置换仍不能排除「块顺序」混杂）'
              % ('显著' if pv < 0.05 else '不显著'))

    # ---- 导出模型（全量训练）
    mu, sd = X.mean(0), X.std(0) + 1e-9
    W1, b1, W2, b2 = fit_mlp((X - mu) / sd, y.astype(float), epochs=600)
    mdl_path = MODEL.replace('.json', '_delta.json') if args.delta else MODEL
    os.makedirs(os.path.dirname(mdl_path), exist_ok=True)
    json.dump(dict(
        kind='jumpduck_1t2r', board='1t2r', classes=['duck', 'jump'],
        feature_names=col_names(), window=WIN,
        frame_feature_names=FEAT_NAMES,
        norm=dict(mu=[round(float(v), 6) for v in mu],
                  sd=[round(float(v), 6) for v in sd]),
        layers=dict(W1=[[round(float(v), 6) for v in row] for row in W1],
                    b1=[round(float(v), 6) for v in b1],
                    W2=[round(float(v), 6) for v in W2], b2=round(float(b2), 6)),
        metrics=dict(window_acc=res['win_acc'], seg_acc=res['seg_acc'],
                     seg_n=res['seg_n'], perm=perm),
        caveats=['单受试者 4 会话', 'jump/duck 块顺序固定，未完全排除时间漂移',
                 '段级依赖完美段切分', '未接入实时链路'],
    ), open(mdl_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\nwritten', os.path.relpath(mdl_path, ROOT))

    os.makedirs(OUT, exist_ok=True)
    name = 'jumpduck-1t2r-delta.json' if args.delta else 'jumpduck-1t2r.json'
    json.dump(dict(loso=res, perm=perm),
              open(os.path.join(OUT, name), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('written outputs/lane-eval/%s' % name)


if __name__ == '__main__':
    main()
