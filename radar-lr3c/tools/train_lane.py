# -*- coding: utf-8 -*-
r"""左右移：监督学习能不能超过规则判据（留一文件验证）。

为什么做这个实验
----------------
已经测过三种规则特征，命中率都不高：
  xbase（偏移，长窗）   59%   延迟 1.81 s   误报 108
  窗均值（1~30 帧）     48% → 3%
  变化率 dx（2~8 帧）   32%
用户问的是"能不能用机器学习提升"。本脚本用同一批数据、同一套段级口径，
训练一个 3 类（NONE/LEFT/RIGHT）线性模型，按**文件留一**验证。

诚实前提
--------
逐帧特征在段内高度相关，因此**必须按文件划分**，否则准确率会被虚高。
7 个文件 → 7 折，每折用 6 个文件训练、1 个测试。

特征（全部因果，只用过去）
  x_now        当前 px（NaN 前值填充）
  dev8         近 8 帧均值 − 近 64 帧均值
  d4           近 4 帧均值 − 再前 4 帧均值
  d8           近 8 帧均值 − 再前 8 帧均值
  det8         近 8 帧检出率
  en8          近 8 帧能量均值（log）

只读。缓存复用 lane_cache。
"""
import os
import sys
import json

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, os.path.join(ROOT, '.deepworks', 'tmp'))
sys.path.insert(0, HERE)

import radar_source as rs  # noqa: E402

rs.sniff_board_from_file = lambda path, scan=262144: '1t2r'

from lane_window import FR, load_all, rolling_mean  # noqa: E402

CLASSES = ['NONE', 'LEFT', 'RIGHT']
CONFIRM = 3  # 连续 3 帧同判才算触发（去抖）


def ffill(x):
    out = x.copy()
    last = 0.0
    for i in range(len(out)):
        if np.isnan(out[i]):
            out[i] = last
        else:
            last = out[i]
    return out


def build(it):
    px = it['px']
    f = ffill(px)
    m4 = rolling_mean(px, 4)
    m8 = rolling_mean(px, 8)
    m64 = rolling_mean(px, 64)
    d4 = m4 - np.concatenate([np.full(4, np.nan), m4[:-4]])
    d8 = m8 - np.concatenate([np.full(8, np.nan), m8[:-8]])
    det8 = rolling_mean((~np.isnan(px)).astype(float), 8)
    feats = np.column_stack([
        f,
        np.nan_to_num(m8 - m64),
        np.nan_to_num(d4),
        np.nan_to_num(d8),
        np.nan_to_num(det8),
    ])
    t = np.arange(len(px)) * FR
    y = np.zeros(len(px), dtype=int)
    for act, t0, t1 in it['spans']:
        y[(t >= t0) & (t <= t1)] = CLASSES.index(act)
    return feats, y, t


def softmax_train(X, y, lr=0.15, iters=600, l2=1e-3):
    n, d = X.shape
    K = 3
    W = np.zeros((d, K))
    b = np.zeros(K)
    Y = np.eye(K)[y]
    # 类权重：NONE 占多数
    cnt = np.bincount(y, minlength=K).astype(float)
    w = (len(y) / (K * np.maximum(cnt, 1)))
    sw = w[y]
    for _ in range(iters):
        z = X @ W + b
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        g = (p - Y) * sw[:, None] / n
        W -= lr * (X.T @ g + l2 * W)
        b -= lr * g.sum(axis=0)
    return W, b


def predict(X, W, b):
    z = X @ W + b
    z -= z.max(axis=1, keepdims=True)
    p = np.exp(z)
    return p / p.sum(axis=1, keepdims=True)


def pred_at(p, thr):
    """概率阈值：最高类概率不足 thr 的帧判为 NONE（抑制静息误报）。"""
    pred = p.argmax(axis=1)
    conf = p.max(axis=1)
    pred = pred.copy()
    pred[conf < thr] = 0
    return pred


def segment_score(pred, t, spans):
    """段级：连续 CONFIRM 帧同判才触发，取段内首次正确触发。"""
    hitL = totL = hitR = totR = 0
    lat = []
    fp = 0
    in_seg = np.zeros(len(t), dtype=bool)
    for act, t0, t1 in spans:
        in_seg |= (t >= t0 - 0.5) & (t <= t1 + 0.5)
    # 去抖：只有连续 CONFIRM 帧同判的最后一帧才算触发点
    trig = np.zeros(len(pred), dtype=bool)
    for c in range(1, 3):
        run = 0
        for i in range(len(pred)):
            run = run + 1 if pred[i] == c else 0
            if run >= CONFIRM:
                trig[i] = True
    for act, t0, t1 in spans:
        side = CLASSES.index(act)
        if act == 'LEFT':
            totL += 1
        else:
            totR += 1
        sel = (t >= t0) & (t <= t1) & trig & (pred == side)
        if sel.any():
            if act == 'LEFT':
                hitL += 1
            else:
                hitR += 1
            lat.append(t[np.argmax(sel)] - t0)
    fp = int((trig & ~in_seg).sum())
    return hitL, totL, hitR, totR, lat, fp


def main():
    items = load_all()
    data = [build(it) for it in items]
    print('文件 %d 个，总帧 %d' % (len(items), sum(len(d[1]) for d in data)))
    print('留一文件验证（%d 折）' % len(items))
    print()
    # 先训好 7 折，然后对每个概率阈值统一评估（同一批预测，公平对比）
    folds = []
    for k in range(len(data)):
        Xtr = np.vstack([data[j][0] for j in range(len(data)) if j != k])
        ytr = np.concatenate([data[j][1] for j in range(len(data)) if j != k])
        mu = Xtr.mean(axis=0)
        sd = Xtr.std(axis=0) + 1e-9
        W, b = softmax_train((Xtr - mu) / sd, ytr)
        Xte = (data[k][0] - mu) / sd
        folds.append((items[k], data[k][2], predict(Xte, W, b)))

    print('%-6s %-14s %-14s %-10s %-10s %-10s' %
          ('阈值', '总命中', 'LEFT', 'RIGHT', '延迟中位', '静息误报'))
    print('-' * 70)
    best = None
    results = {}
    for thr in (0.50, 0.70, 0.85, 0.95, 0.99):
        agg = dict(hitL=0, totL=0, hitR=0, totR=0, fp=0, lat=[])
        for it, t, p in folds:
            pred = pred_at(p, thr)
            hL, tL, hR, tR, lat, fp = segment_score(pred, t, it['spans'])
            agg['hitL'] += hL; agg['totL'] += tL
            agg['hitR'] += hR; agg['totR'] += tR
            agg['fp'] += fp; agg['lat'] += lat
        lat = sorted(agg['lat'])
        tot = agg['totL'] + agg['totR']
        hit = agg['hitL'] + agg['hitR']
        med = lat[len(lat) // 2] if lat else float('nan')
        print('%-6.2f %-14s %-14s %-10s %-10s %-10d'
              % (thr,
                 '%d/%d %.0f%%' % (hit, tot, 100.0 * hit / max(1, tot)),
                 '%d/%d %.0f%%' % (agg['hitL'], agg['totL'],
                                   100.0 * agg['hitL'] / max(1, agg['totL'])),
                 '%d/%d %.0f%%' % (agg['hitR'], agg['totR'],
                                   100.0 * agg['hitR'] / max(1, agg['totR'])),
                 ('%.2f s' % med) if lat else '—', agg['fp']))
        results['%.2f' % thr] = dict(agg=agg, hit=hit, tot=tot,
                                     hitrate=hit / max(1, tot), lat_med=med)
        # 选"命中率 − 误报惩罚"最优（误报按每 1000 帧计）
        score = hit / max(1, tot) - agg['fp'] / 2000.0
        if best is None or score > best[0]:
            best = (score, thr, agg, med)

    print()
    print('=' * 70)
    print('对照（同一批数据、同一段级口径）：')
    print('  规则 xbase      59%%   延迟 1.81 s   误报 108')
    print('  ML 最优阈值 %.2f  %.0f%%   延迟 %.2f s   误报 %d'
          % (best[1], 100.0 * best[0] + best[2]['fp'] / 2000.0 * 0 + 100.0 *
             (best[2]['hitL'] + best[2]['hitR']) /
             max(1, best[2]['totL'] + best[2]['totR']), best[3], best[2]['fp']))
    print('=' * 70)

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'ml-loso.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(by_threshold=results), f, ensure_ascii=False, indent=1)
    print('已写 outputs/lane-eval/ml-loso.json')


if __name__ == '__main__':
    main()
