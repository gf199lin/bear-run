# -*- coding: utf-8 -*-
r"""左右移：规则判据 vs 机器学习，同一批数据、同一段级口径（留一文件验证）。

为什么要有这个脚本
------------------
`tools/train_lane.py` 已经在老的 7 个文件上试过 softmax，结果很差（阈值 0.7 以上 0 命中）。
但它有两点不足，不能拿来回答"ML 到底行不行"：
  1. 特征不是产线用的那个 px（它自己按"带内最强点"重算了一遍）；
  2. 只试了线性模型，没有非线性模型，也没和"同一批文件上的规则基线"逐文件对照。

本脚本把比较做干净：
  - **同一个 px 序列**：直接跑产线 `ActionRecognizer(band='auto', crit='xbase')`，
    从每帧 features 里取 px/py/energy/n，规则和 ML 用完全相同的输入。
  - **同一套打分**：命中窗口 [t_start-0.5, t_end+0.5]、延迟 = 段内首个同向事件
    − t_start、误报 = 不落在任何窗口内的事件（与 lane_eval.py 一致）。
  - **同一套状态机**：ML 的逐帧概率也过 confirm=3 + 最短间隔 0.5 s + 释放确认，
    再产生事件，所以两边比的是"整条链路"，不是逐帧分类准确率。
  - **留一文件验证**：16 个文件 → 16 折，每折用 15 个训练、1 个测试，
    阈值也只在训练折上选（不看测试文件）。

模型
----
  rule      产线 crit='xbase'（近 0.8 s 中位 − 近 40 s 中位，左右独立门限 + 滞回）
  logit     多项逻辑回归（线性，类权重）
  tree      CART 决策树（深度 5，类权重）——可读，能直接看出它学到的门限
  mlp       1 层隐层 16 单元的 tanh 网络（非线性）

只读，不写采集数据。产物：outputs/lane-eval/model-compare.json

用法
----
    python tools/compare_lane_models.py
    python tools/compare_lane_models.py --subset mvp     # 只看 0.7~1.0 m 站位
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, HERE)

import radar_source as rs  # noqa: E402

rs.sniff_board_from_file = lambda path, scan=262144: '1t2r'

import label_tool as lt                      # noqa: E402
from radar_actions import ActionRecognizer, analyze_file  # noqa: E402
from radar_dsp import PointCloudProcessor    # noqa: E402

FR = 0.1295
CFAR = 2.6
SNR = 7.0
PRE = POST = 0.5
CACHE = os.path.join(ROOT, '.deepworks', 'tmp', 'lane_models_cache')
CLASSES = ['NONE', 'LEFT', 'RIGHT']

# 参与比较的文件（已按 md5 去重：cap-long-1020 有 3 个字节级相同的副本，只留 1 个；
# _dx-smoke/t.bin 是 0 字节空文件，丢弃；seg-2t4r 是 2T4R 板，帧周期不同，另算）。
FILES = [
    ('lr3b',            'data/lr3b/seg_01.00.bin',                        1.0),
    ('lr3c',            'data/lr3c/seg_01.00.bin',                        1.0),
    ('lr2-p1-d070',     '.deepworks/tmp/seg-lr2/1/d0.70/seg_00.70.bin',   0.7),
    ('lr2-p1-d100',     '.deepworks/tmp/seg-lr2/1/d1.00/seg_01.00.bin',   1.0),
    ('lr2-jqz-d070',    '.deepworks/tmp/seg-lr2/jqz/d0.70/seg_00.70.bin', 0.7),
    ('lr2-jqz-d100',    '.deepworks/tmp/seg-lr2/jqz/d1.00/seg_01.00.bin', 1.0),
    ('once-070',        '.deepworks/tmp/oncehot-oncehot/lr/seg_00.70.bin', 0.7),
    ('once-085',        '.deepworks/tmp/oncehot-oncehot/lr/seg_00.85.bin', 0.85),
    ('once-100',        '.deepworks/tmp/oncehot-oncehot/lr/seg_01.00.bin', 1.0),
    ('long-070',        '.deepworks/tmp/cap-long-1020/action/seg_00.70.bin', 0.7),
    ('m2recap-150',     '.deepworks/tmp/m2-recap/seg_01.50.bin',          1.5),
    ('m2recap-200',     '.deepworks/tmp/m2-recap/seg_02.00.bin',          2.0),
    ('seg-125',         '.deepworks/tmp/seg/seg_01.25.bin',               1.25),
    ('seg-150',         '.deepworks/tmp/seg/seg_01.50.bin',               1.5),
    ('stride-150',      '.deepworks/tmp/seg-stride/seg_01.50.bin',        1.5),
    ('ext-125',         '.deepworks/tmp/seg-ext/ext/d1.25/seg_01.25.bin', 1.25),
]
MVP = set(['lr3b', 'lr3c', 'lr2-p1-d070', 'lr2-p1-d100', 'lr2-jqz-d070',
           'lr2-jqz-d100', 'once-070', 'once-085', 'once-100', 'long-070'])

CONFIRM = 3
MIN_INTERVAL = 0.5
RELEASE_CONFIRM = 3


# ---------------------------------------------------------------- 取数
def labels_of(path):
    lab = os.path.splitext(path)[0] + '.labels.jsonl'
    return lt.load_labels(lab)


def prod_series(path, tag, top_k, min_pts, band='auto', use_cache=True):
    """跑产线路径，返回逐帧 px/py/energy/n/t 与产线自身产生的 LANE 事件。

    ★ 缓存键必须带 tag：多个文件都叫 seg_01.00.bin，用 basename 会互相覆盖
      （2026-11-24 踩过，导致 10 个文件读到同一份缓存）。
    """
    os.makedirs(CACHE, exist_ok=True)
    bkey = band if isinstance(band, str) else '%.2f-%.2f' % band
    cf = os.path.join(CACHE, '%s-k%d-m%d-%s.npz' % (tag, top_k, min_pts, bkey))
    if use_cache and os.path.exists(cf):
        z = np.load(cf, allow_pickle=True)
        return dict(t=z['t'], px=z['px'], py=z['py'], en=z['en'], n=z['n'],
                    ev_t=z['ev_t'], ev_s=z['ev_s'], ev_c=z['ev_c'], span=float(z['span']))

    rec = ActionRecognizer(band=band, crit='xbase', top_k=top_k, min_pts=min_pts)
    proc = PointCloudProcessor(cfar_scale=CFAR, min_snr=SNR, min_range=0.681)
    records, info = analyze_file(path, processor=proc, recognizer=rec, board='1t2r')

    t, px, py, en, n = [], [], [], [], []
    ev_t, ev_s, ev_c = [], [], []
    for r in records:
        f = r['features'] or {}
        t.append(r['t'])
        px.append(np.nan if f.get('px') is None else float(f['px']))
        py.append(np.nan if f.get('py') is None else float(f['py']))
        en.append(np.nan if f.get('energy') is None else float(f['energy']))
        n.append(np.nan if f.get('n') is None else float(f['n']))
        for e in r['events']:
            if e['name'] in ('LANE_LEFT', 'LANE_RIGHT'):
                ev_t.append(e['t'])
                ev_s.append(1 if e['name'] == 'LANE_LEFT' else 2)
                ev_c.append(1)
    span = records[-1]['t'] if records else 1.0
    out = dict(t=np.asarray(t), px=np.asarray(px), py=np.asarray(py),
               en=np.asarray(en), n=np.asarray(n),
               ev_t=np.asarray(ev_t, dtype=float), ev_s=np.asarray(ev_s, dtype=int),
               ev_c=np.asarray(ev_c, dtype=int), span=float(span))
    np.savez_compressed(cf, **out)
    return out


# ---------------------------------------------------------------- 特征
def ffill(x):
    out = x.copy()
    last = 0.0
    for i in range(len(out)):
        if np.isnan(out[i]):
            out[i] = last
        else:
            last = out[i]
    return out


def rolling_median(x, w):
    v = ~np.isnan(x)
    out = np.full(len(x), np.nan)
    for t in range(len(x)):
        a = max(0, t - w + 1)
        seg = x[a:t + 1]
        seg = seg[~np.isnan(seg)]
        if len(seg):
            out[t] = float(np.median(seg))
    return out


def rolling_mean(x, w):
    v = ~np.isnan(x)
    xz = np.where(v, x, 0.0)
    cs = np.cumsum(xz)
    cn = np.cumsum(v.astype(float))
    out = np.full(len(x), np.nan)
    for t in range(len(x)):
        a = max(0, t - w + 1)
        nn = cn[t] - (cn[a - 1] if a > 0 else 0.0)
        if nn > 0:
            out[t] = (cs[t] - (cs[a - 1] if a > 0 else 0.0)) / nn
    return out


def rolling_slope(x, w):
    """因果最小二乘斜率（只用过去 w 帧）。"""
    out = np.full(len(x), np.nan)
    tt = np.arange(w, dtype=float)
    tt = tt - tt.mean()
    den = (tt ** 2).sum()
    for t in range(len(x)):
        if t < w - 1:
            continue
        seg = x[t - w + 1:t + 1]
        if np.isnan(seg).any():
            continue
        out[t] = float((tt * (seg - seg.mean())).sum() / den)
    return out


def lagged(x, k):
    out = np.full(len(x), np.nan)
    out[k:] = x[:-k]
    return out


def build_features(s):
    """全部因果（只用 t 及以前）。"""
    px = s['px']
    f = ffill(px)
    nfr = lambda sec: max(2, int(round(sec / FR)))
    med_s = rolling_median(px, nfr(0.8))
    med_l = rolling_median(px, nfr(40.0))
    ms = np.where(np.isnan(med_s), f, med_s)
    ml = np.where(np.isnan(med_l), f, med_l)
    dev = ms - ml                                            # 规则用的证据

    med_04 = np.where(np.isnan(rolling_median(px, nfr(0.4))), f,
                      rolling_median(px, nfr(0.4)))
    med_16 = np.where(np.isnan(rolling_median(px, nfr(1.6))), f,
                      rolling_median(px, nfr(1.6)))

    m4 = rolling_mean(px, 4)
    m4p = lagged(m4, 4)
    d4 = np.nan_to_num(m4 - m4p)
    slope8 = np.nan_to_num(rolling_slope(px, 8))
    slope3 = np.nan_to_num(rolling_slope(px, 3))
    det8 = np.nan_to_num(rolling_mean((~np.isnan(px)).astype(float), 8))
    det32 = np.nan_to_num(rolling_mean((~np.isnan(px)).astype(float), 32))

    en = ffill(np.log10(np.maximum(s['en'], 1e-6)))
    en4 = rolling_mean(en, 4)
    en128 = rolling_mean(en, 128)
    en_ratio = np.nan_to_num(en4 - en128)

    py = ffill(s['py'])
    py_dev = np.nan_to_num(py - np.where(np.isnan(rolling_median(s['py'], 64)), py,
                                         rolling_median(s['py'], 64)))
    npt = np.nan_to_num(rolling_mean(s['n'], 4))

    dp = np.diff(f, prepend=f[0])
    jit = np.nan_to_num(rolling_mean(np.abs(dp), 16))

    X = np.column_stack([dev, med_04 - med_16, d4, slope8, slope3, det8, det32,
                         en_ratio, py_dev, npt, jit, f - ml])
    names = ['dev', 'dev_04_16', 'd4', 'slope8', 'slope3', 'det8', 'det32',
             'en_ratio', 'py_dev', 'npts4', 'jit16', 'px_minus_long']
    return np.nan_to_num(X), names


def build_labels(s, spans):
    t = s['t']
    y = np.zeros(len(t), dtype=int)
    for act, t0, t1 in spans:
        y[(t >= t0) & (t <= t1)] = CLASSES.index(act)
    return y


# ---------------------------------------------------------------- 模型
def fit_logit(X, y, lr=0.3, iters=800, l2=1e-3):
    n, d = X.shape
    K = 3
    W = np.zeros((d, K))
    b = np.zeros(K)
    Y = np.eye(K)[y]
    cnt = np.bincount(y, minlength=K).astype(float)
    cw = len(y) / (K * np.maximum(cnt, 1))
    sw = cw[y]
    for _ in range(iters):
        z = X @ W + b
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        g = (p - Y) * sw[:, None] / n
        W -= lr * (X.T @ g + l2 * W)
        b -= lr * g.sum(axis=0)
    return lambda Z: _softmax(Z @ W + b)


def _softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    p = np.exp(z)
    return p / p.sum(axis=1, keepdims=True)


def fit_mlp(X, y, hidden=16, epochs=500, lr=0.08, l2=1e-4, seed=0):
    rng = np.random.default_rng(seed)
    n, d = X.shape
    K = 3
    Y = np.eye(K)[y]
    cnt = np.bincount(y, minlength=K).astype(float)
    cw = len(y) / (K * np.maximum(cnt, 1))
    sw = cw[y]
    W1 = rng.normal(0, np.sqrt(2.0 / d), (d, hidden))
    b1 = np.zeros(hidden)
    W2 = rng.normal(0, np.sqrt(2.0 / hidden), (hidden, K))
    b2 = np.zeros(K)
    m = [np.zeros_like(W1), np.zeros_like(b1), np.zeros_like(W2), np.zeros_like(b2)]
    v = [np.zeros_like(W1), np.zeros_like(b1), np.zeros_like(W2), np.zeros_like(b2)]
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    for it in range(1, epochs + 1):
        H = np.tanh(X @ W1 + b1)
        P = _softmax(H @ W2 + b2)
        G = (P - Y) * sw[:, None] / n
        gW2 = H.T @ G + l2 * W2
        gb2 = G.sum(axis=0)
        GH = (G @ W2.T) * (1 - H ** 2)
        gW1 = X.T @ GH + l2 * W1
        gb1 = GH.sum(axis=0)
        for i, g in enumerate([gW1, gb1, gW2, gb2]):
            m[i] = beta1 * m[i] + (1 - beta1) * g
            v[i] = beta2 * v[i] + (1 - beta2) * g * g
            mh = m[i] / (1 - beta1 ** it)
            vh = v[i] / (1 - beta2 ** it)
            step = lr * mh / (np.sqrt(vh) + eps)
            if i == 0:
                W1 -= step
            elif i == 1:
                b1 -= step
            elif i == 2:
                W2 -= step
            else:
                b2 -= step
    return lambda Z: _softmax(np.tanh(Z @ W1 + b1) @ W2 + b2)


class Tree:
    """最小实现的 CART 多分类树（gini + 类权重），只为"学到的门限可读"。"""

    def __init__(self, max_depth=5, min_leaf=60, seed=0):
        self.max_depth = max_depth
        self.min_leaf = min_leaf
        self.seed = seed

    def fit(self, X, y, w):
        self.K = 3
        self.root = self._grow(X, y, w, 0)
        return self

    def _grow(self, X, y, w, depth):
        n = len(y)
        cnt = np.bincount(y, weights=w, minlength=self.K)
        p = cnt / max(cnt.sum(), 1e-12)
        node = {'p': p, 'n': n, 'split': None}
        if depth >= self.max_depth or n < 2 * self.min_leaf or p.max() > 0.985:
            return node
        best = None
        base = 1.0 - (p ** 2).sum()
        for j in range(X.shape[1]):
            xj = X[:, j]
            qs = np.unique(np.quantile(xj, np.linspace(0.05, 0.95, 19)))
            for q in qs:
                m = xj <= q
                nl = int(m.sum())
                if nl < self.min_leaf or n - nl < self.min_leaf:
                    continue
                cl = np.bincount(y[m], weights=w[m], minlength=self.K)
                cr = np.bincount(y[~m], weights=w[~m], minlength=self.K)
                pl = cl / max(cl.sum(), 1e-12)
                pr = cr / max(cr.sum(), 1e-12)
                gl = (1.0 - (pl ** 2).sum()) * cl.sum()
                gr = (1.0 - (pr ** 2).sum()) * cr.sum()
                gain = base * cnt.sum() - gl - gr
                if best is None or gain > best[0]:
                    best = (gain, j, q)
        if best is None or best[0] <= 1e-9:
            return node
        _, j, q = best
        m = X[:, j] <= q
        node['split'] = (j, float(q))
        node['left'] = self._grow(X[m], y[m], w[m], depth + 1)
        node['right'] = self._grow(X[~m], y[~m], w[~m], depth + 1)
        return node

    def _leaf(self, x, node):
        while node.get('split') is not None:
            j, q = node['split']
            node = node['left'] if x[j] <= q else node['right']
        return node['p']

    def predict_proba(self, X):
        return np.vstack([self._leaf(x, self.root) for x in X])

    def rules(self, names, depth=0):
        out = []

        def walk(node, path, d):
            if node.get('split') is None:
                cls = CLASSES[int(np.argmax(node['p']))]
                out.append('  ' * d + '→ %s (p=%s, n=%d)'
                           % (cls, np.round(node['p'], 2).tolist(), node['n']))
                return
            j, q = node['split']
            out.append('  ' * d + 'if %s <= %.3f:' % (names[j], q))
            walk(node['left'], path + [1], d + 1)
            out.append('  ' * d + 'else:')
            walk(node['right'], path + [0], d + 1)
        walk(self.root, [], depth)
        return out


def class_weight_vec(y):
    cnt = np.bincount(y, minlength=3).astype(float)
    cw = len(y) / (3 * np.maximum(cnt, 1))
    return cw[y]


# ---------------------------------------------------------------- 打分
def evaluate(evs, labels, span):
    """与 lane_eval.evaluate 同口径 + 误报/分钟。"""
    acts = [l for l in labels if l.get('action') in ('LEFT', 'RIGHT')]
    used = set()
    hit = {'LEFT': 0, 'RIGHT': 0}
    tot = {'LEFT': 0, 'RIGHT': 0}
    lat = []
    for a in acts:
        side = a['action']
        tot[side] += 1
        t0, t1 = a['t_start'] - PRE, a['t_end'] + POST
        inner = [(i, t) for i, (t, s) in enumerate(evs) if s == side and t0 <= t <= t1]
        if inner:
            hit[side] += 1
            lat.append(inner[0][1] - a['t_start'])
            used.update(i for i, _ in inner)
    fp = sum(1 for i, _ in enumerate(evs) if i not in used)
    return dict(hit=hit, tot=tot, lat=lat, fp=fp,
                fp_per_min=fp / max(span / 60.0, 1e-6))


def emit_events(t, prob, thr):
    """逐帧概率 → 事件。状态机与产线一致：confirm 帧 + 最短间隔 + 释放确认。"""
    pred = prob.argmax(axis=1)
    conf = prob.max(axis=1)
    lane = 0
    want = 0
    run = 0
    last_flip = -1e9
    rel = 0
    evs = []
    for i in range(len(t)):
        cls = int(pred[i]) if conf[i] >= thr else 0
        if cls != 0 and cls != lane:
            if cls == want:
                run += 1
            else:
                want = cls
                run = 1
            if run >= CONFIRM and t[i] - last_flip >= MIN_INTERVAL:
                lane = cls
                last_flip = t[i]
                evs.append((float(t[i]), 'LEFT' if cls == 1 else 'RIGHT'))
                run = 0
        else:
            run = 0
        if lane != 0:
            rel = rel + 1 if cls == 0 else 0
            if rel >= RELEASE_CONFIRM:
                lane = 0
                rel = 0
    return evs


def agg_scores(rows):
    hitL = sum(r['hit']['LEFT'] for r in rows)
    totL = sum(r['tot']['LEFT'] for r in rows)
    hitR = sum(r['hit']['RIGHT'] for r in rows)
    totR = sum(r['tot']['RIGHT'] for r in rows)
    lat = sorted(x for r in rows for x in r['lat'])
    fp = sum(r['fp'] for r in rows)
    minutes = sum(r['minutes'] for r in rows)
    tot = totL + totR
    hit = hitL + hitR
    return dict(
        hit=hit, tot=tot, hitrate=hit / max(tot, 1),
        hitL=hitL, totL=totL, hitR=hitR, totR=totR,
        lat_med=(lat[len(lat) // 2] if lat else None),
        lat_p90=(lat[min(len(lat) - 1, int(len(lat) * 0.9))] if lat else None),
        fp=fp, fp_per_min=fp / max(minutes, 1e-6), minutes=minutes,
        n_files=len(rows))


def fmt(s):
    return ('%d/%d=%.0f%% (L %d/%d, R %d/%d)  延迟中位 %s s  误报 %d (%.1f/min)'
            % (s['hit'], s['tot'], 100 * s['hitrate'], s['hitL'], s['totL'],
               s['hitR'], s['totR'],
               ('%.2f' % s['lat_med']) if s['lat_med'] is not None else '—',
               s['fp'], s['fp_per_min']))


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    ap = argparse.ArgumentParser(description='左右移：规则 vs 机器学习（留一文件）')
    ap.add_argument('--subset', choices=['all', 'mvp'], default='all')
    ap.add_argument('--top-k', type=int, default=0,
                    help='0=产线默认（带内幅值加权质心），1=只留最强点')
    ap.add_argument('--min-pts', type=int, default=2)
    ap.add_argument('--depth', type=int, default=5)
    ap.add_argument('--out', default='model-compare.json')
    ap.add_argument('--nocache', action='store_true')
    ap.add_argument('--band', default='auto', help='auto 或 lo,hi')
    ap.add_argument('--min-det', type=float, default=0.0,
                    help='丢弃检出率低于该值的文件（数据有效性筛选）')
    ap.add_argument('--rule-only', action='store_true')
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8')

    band = ('auto' if args.band == 'auto'
            else tuple(float(x) for x in args.band.split(',')))
    files = [f for f in FILES if args.subset == 'all' or f[0] in MVP]
    data = []
    print('=== 取数（产线路径 band=%s crit=xbase top_k=%d min_pts=%d）==='
          % (band, args.top_k, args.min_pts))
    for tag, rel, dist in files:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print('  跳过（缺文件）%s' % rel)
            continue
        s = prod_series(path, tag, args.top_k, args.min_pts, band=band,
                        use_cache=not args.nocache)
        labs = labels_of(path)
        spans = [(l['action'], l['t_start'], l['t_end']) for l in labs
                 if l.get('action') in ('LEFT', 'RIGHT')]
        X, names = build_features(s)
        y = build_labels(s, spans)
        det = float(np.mean(~np.isnan(s['px'])))
        data.append(dict(tag=tag, dist=dist, s=s, X=X, y=y, names=names,
                         spans=spans, labs=labs, det=det,
                         minutes=s['span'] / 60.0))
        print('  %-14s %5.1f m  帧 %5d  检出率 %.0f%%  动作段 %2d  %.1f min'
              % (tag, dist, len(s['px']), 100 * det, len(spans), s['span'] / 60))

    if args.min_det > 0:
        keep = [d for d in data if d['det'] >= args.min_det]
        drop = [d['tag'] for d in data if d['det'] < args.min_det]
        print('  数据有效性筛选 det >= %.2f：保留 %d 个，丢弃 %d 个（%s）'
              % (args.min_det, len(keep), len(drop), ', '.join(drop)))
        data = keep
    if not data:
        print('没有文件通过筛选')
        return 1

    print()
    print('=== 规则基线（产线事件，逐文件）===')
    rule_rows = []
    for d in data:
        evs = list(zip(d['s']['ev_t'].tolist(), d['s']['ev_s'].tolist()))
        evs = [(t, 'LEFT' if c == 1 else 'RIGHT') for t, c in evs]
        r = evaluate(evs, d['labs'], d['s']['span'])
        r.update(tag=d['tag'], minutes=d['minutes'])
        rule_rows.append(r)
        print('  %-14s %s' % (d['tag'], fmt(agg_scores([r]))))
    base = agg_scores(rule_rows)
    print('  ' + '-' * 70)
    print('  合计          %s' % fmt(base))

    if args.rule_only:
        out = os.path.join(ROOT, 'outputs', 'lane-eval')
        os.makedirs(out, exist_ok=True)
        dump = dict(
            subset=args.subset, top_k=args.top_k, min_pts=args.min_pts,
            band=band if isinstance(band, str) else list(band),
            min_det=args.min_det, rule_only=True,
            files=[dict(tag=d['tag'], dist=d['dist'],
                        frames=int(len(d['s']['px'])),
                        det_rate=round(d['det'], 3), actions=len(d['spans']),
                        minutes=round(d['minutes'], 2)) for d in data],
            rule=dict(agg=base, rows=[{k: v for k, v in r.items() if k != 'lat'}
                                      for r in rule_rows]),
            models={}, matched_fp={}, feature_names=data[0]['names'],
            tree_full_rules=[])
        with open(os.path.join(out, args.out), 'w', encoding='utf-8') as f:
            json.dump(dump, f, ensure_ascii=False, indent=1, default=float)
        print('已写 outputs/lane-eval/%s' % args.out)
        return 0

    # ---- 留一文件：每折只训一次，预测存下来，阈值在后面统一扫 ----
    print()
    print('=== 留一文件验证（%d 折，阈值只在训练折选）===' % len(data))
    THRS = (0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95)
    folds = {}
    for mname in ('logit', 'tree', 'mlp'):
        fi = []
        for k, d in enumerate(data):
            tr = [j for j in range(len(data)) if j != k]
            Xtr = np.vstack([data[j]['X'] for j in tr])
            ytr = np.concatenate([data[j]['y'] for j in tr])
            mu = Xtr.mean(axis=0)
            sd = Xtr.std(axis=0) + 1e-9
            Xtr_n = (Xtr - mu) / sd
            if mname == 'logit':
                f = fit_logit(Xtr_n, ytr)
            elif mname == 'tree':
                f = Tree(max_depth=args.depth, min_leaf=60).fit(
                    Xtr_n, ytr, class_weight_vec(ytr)).predict_proba
            else:
                f = fit_mlp(Xtr_n, ytr)
            fi.append(dict(tr=tr,
                           Pte=f((d['X'] - mu) / sd),
                           Ptr={j: f((data[j]['X'] - mu) / sd) for j in tr}))
            print('    已训 %-6s 折 %2d/%d（留出 %s）'
                  % (mname, k + 1, len(data), d['tag']))
        folds[mname] = fi

    models = {}
    matched = {}
    for mname in ('logit', 'tree', 'mlp'):
        fi = folds[mname]
        rows = []
        for k, d in enumerate(data):
            tr = fi[k]['tr']
            best = None
            for thr in THRS:
                hit = tot = 0
                fpm = 0.0
                for j in tr:
                    dj = data[j]
                    ev = emit_events(dj['s']['t'], fi[k]['Ptr'][j], thr)
                    rr = evaluate(ev, dj['labs'], dj['s']['span'])
                    hit += rr['hit']['LEFT'] + rr['hit']['RIGHT']
                    tot += rr['tot']['LEFT'] + rr['tot']['RIGHT']
                    fpm += rr['fp_per_min']
                sc = hit / max(tot, 1) - (fpm / len(tr)) / 40.0
                if best is None or sc > best[0]:
                    best = (sc, thr)
            thr = best[1]
            ev = emit_events(d['s']['t'], fi[k]['Pte'], thr)
            r = evaluate(ev, d['labs'], d['s']['span'])
            r.update(tag=d['tag'], minutes=d['minutes'], thr=thr)
            rows.append(r)
        models[mname] = dict(rows=rows, agg=agg_scores(rows))
        print('  %-6s %s' % (mname, fmt(models[mname]['agg'])))
        for r in rows:
            print('      %-14s thr=%.2f %s' % (r['tag'], r['thr'],
                                               fmt(agg_scores([r]))))

        # 等误报扫描：同一批预测，只换阈值
        rows_all = {}
        for thr in THRS:
            rr_all = []
            for k, d in enumerate(data):
                ev = emit_events(d['s']['t'], fi[k]['Pte'], thr)
                r = evaluate(ev, d['labs'], d['s']['span'])
                r.update(tag=d['tag'], minutes=d['minutes'])
                rr_all.append(r)
            rows_all[str(thr)] = agg_scores(rr_all)
        ok = [(thr, s) for thr, s in rows_all.items()
              if s['fp_per_min'] <= base['fp_per_min']]
        pick = max(ok, key=lambda x: x[1]['hitrate']) if ok else None
        matched[mname] = dict(by_thr=rows_all,
                              pick_thr=(pick[0] if pick else None),
                              pick=(pick[1] if pick else None))

    print()
    print('=== 等误报对比（把误报压到 <= 规则的 %.1f/min，看还能命中多少）==='
          % base['fp_per_min'])
    for mname in ('logit', 'tree', 'mlp'):
        pick = matched[mname]['pick']
        if pick:
            print('  %-6s thr=%s %s' % (mname, matched[mname]['pick_thr'],
                                         fmt(pick)))
        else:
            print('  %-6s 没有任何阈值能把误报压到该水平' % mname)

    # ---- 学到的树规则（可读） ----
    tree = Tree(max_depth=args.depth, min_leaf=60)
    Xall = np.vstack([d['X'] for d in data])
    yall = np.concatenate([d['y'] for d in data])
    mu = Xall.mean(axis=0)
    sd = Xall.std(axis=0) + 1e-9
    tree.fit((Xall - mu) / sd, yall, class_weight_vec(yall))
    print()
    print('=== 全量训练的树（仅用于看它学到了什么门限，不代表泛化）===')
    print('  注：以下阈值是标准化后的，换算回原量纲 = 值 × sd + mu')
    for line in tree.rules(data[0]['names']):
        print('  ' + line)
    print('  sd = %s' % np.round(sd, 3).tolist())
    print('  mu = %s' % np.round(mu, 3).tolist())

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    dump = dict(
        subset=args.subset, top_k=args.top_k, min_pts=args.min_pts,
        band=band if isinstance(band, str) else list(band),
        min_det=args.min_det, rule_only=args.rule_only,
        files=[dict(tag=d['tag'], dist=d['dist'], frames=int(len(d['s']['px'])),
                    det_rate=round(d['det'], 3), actions=len(d['spans']),
                    minutes=round(d['minutes'], 2)) for d in data],
        rule=dict(agg=base, rows=[{k: v for k, v in r.items() if k != 'lat'}
                                  for r in rule_rows]),
        models={m: dict(agg=models[m]['agg'], rows=[
            {k: v for k, v in r.items() if k != 'lat'} for r in models[m]['rows']])
            for m in models},
        matched_fp={m: dict(pick_thr=matched[m]['pick_thr'],
                            pick=matched[m]['pick'],
                            by_thr=matched[m]['by_thr']) for m in matched},
        feature_names=data[0]['names'],
        tree_full_rules=tree.rules(data[0]['names']),
    )
    with open(os.path.join(out, args.out), 'w', encoding='utf-8') as f:
        json.dump(dump, f, ensure_ascii=False, indent=1, default=float)
    print()
    print('已写 outputs/lane-eval/%s' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
