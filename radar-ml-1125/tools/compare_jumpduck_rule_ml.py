# -*- coding: utf-8 -*-
"""规则法 vs 机器学习：跳/蹲第 3 层判据对照。

回答一个具体问题：`docs/jumpduck-decision-spec.md` 里「因果 EMA 后同号段长度」
（nb<=2 跳 / nb>=3 蹲）这条规律，机器学习能不能自己看出来？

同一批 96 段（48 跳 + 48 蹲），四个口径：

  A  纯规则        nb_max >= 3 -> DUCK
  B  ML 55 维      帧统计 + 4 帧窗口（不含任何符号时序）
  C  ML 64 维      B + nb/pb/negPk/posPk/EMA速度/差分
  D  负样本检验     walk/bend/turn/alt/rest/left/right 会不会被误报

用法
----
    python tools/compare_jumpduck_rule_ml.py
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import train_jumpduck_1t2r as T  # noqa: E402

ALPHA = 0.5
THR = 0.15
NEG_LABELS = ('walk', 'bend', 'turn', 'alt', 'rest', 'left', 'right')


def ema(v, a=ALPHA):
    out = np.empty_like(v)
    s = 0.0
    for i, x in enumerate(v):
        s = a * x + (1 - a) * s
        out[i] = s
    return out


def runs(vs, thr=THR):
    """逐帧返回 (nb, pb, negPk, posPk)，允许段内 1 帧回落。"""
    n = len(vs)
    nb = np.zeros(n)
    pb = np.zeros(n)
    nk = np.zeros(n)
    pk = np.zeros(n)
    cn = cp = 0
    nPk = pPk = 0.0
    mn = mp = 0
    for i, x in enumerate(vs):
        if x < -thr:
            if cn == 0:
                nPk = x
            cn += 1
            nPk = min(nPk, x)
            cp, mp = 0, 0
        elif x > thr:
            if cp == 0:
                pPk = x
            cp += 1
            pPk = max(pPk, x)
            cn, mn = 0, 0
        else:
            mn += 1
            mp += 1
            if mn > 1:
                cn = 0
            if mp > 1:
                cp = 0
        nb[i], pb[i] = cn, cp
        nk[i] = nPk if cn else 0.0
        pk[i] = pPk if cp else 0.0
    return nb, pb, nk, pk


def frame_runs(F, L, S):
    """每帧的 (nb, pb, negPk, posPk, vs, dv)。"""
    out = np.zeros((len(L), 6), np.float64)
    for s in sorted(set(S.tolist())):
        idx = np.where(S == s)[0]
        ve = ema(F[idx, 3].astype(np.float64))
        nb, pb, nk, pk = runs(ve)
        dv = np.zeros(len(idx))
        dv[1:] = ve[1:] - ve[:-1]
        out[idx] = np.stack([nb, pb, nk, pk, ve, dv], axis=1)
    return out


def seg_max(SEG, L, S, B, col):
    o = {}
    for i in range(len(L)):
        o.setdefault((S[i], B[i]), []).append(SEG[i, col])
    return {k: float(np.max(v)) for k, v in o.items()}


def seg_min(SEG, L, S, B, col):
    o = {}
    for i in range(len(L)):
        o.setdefault((S[i], B[i]), []).append(SEG[i, col])
    return {k: float(np.min(v)) for k, v in o.items()}


def seg_eval(p, y, sess, seg):
    o = {}
    for i in range(len(p)):
        o.setdefault((sess[i], seg[i]), []).append((p[i], y[i]))
    hit = sum(int((np.mean([a for a, _ in v]) >= .5) == (v[0][1] == 1))
              for v in o.values())
    per = {}
    for k, v in o.items():
        per.setdefault(k[0], []).append(
            int((np.mean([a for a, _ in v]) >= .5) == (v[0][1] == 1)))
    return hit, len(o), per


def loso(X, y, sess):
    p = np.full(len(y), np.nan)
    for s in sorted(set(sess.tolist())):
        tr, te = sess != s, sess == s
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        m = T.fit_mlp((X[tr] - mu) / sd, y[tr].astype(float))
        p[te] = T.proba(m, (X[te] - mu) / sd)
    return p


def main():
    F, L, S, B = T.load_arrays()
    SEG = frame_runs(F, L, S)
    res = {}

    # ---------- A 纯规则 ----------
    nbm = seg_max(SEG, L, S, B, 0)
    rows = [k for k in nbm if L[np.where((S == k[0]) & (B == k[1]))[0][0]]
            in ('jump', 'duck')]
    lab = {k: L[np.where((S == k[0]) & (B == k[1]))[0][0]] for k in rows}
    hit = sum(int((nbm[k] >= 3) == (lab[k] == 'duck')) for k in rows)
    print('=== A 纯规则 nb_max >= 3 -> DUCK ===')
    print('  跳 nb_max: %s' % sorted(int(nbm[k]) for k in rows if lab[k] == 'jump'))
    print('  蹲 nb_max: %s' % sorted(int(nbm[k]) for k in rows if lab[k] == 'duck'))
    print('  段级 %d/%d = %.1f%%' % (hit, len(rows), 100 * hit / len(rows)))
    res['rule'] = dict(hit=hit, n=len(rows), acc=round(hit / len(rows), 4))

    # ---------- B / C ----------
    X, y, sess, seg = T.windowize(F, L, S, B)
    m = np.isin(y, ['jump', 'duck'])
    X, y, sess, seg = X[m], (y[m] == 'jump').astype(int), sess[m], seg[m]
    pB = loso(X, y, sess)
    hb, nb_, _ = seg_eval(pB, y, sess, seg)
    print('\n=== B ML 55 维（无符号时序）===')
    print('  段级 %d/%d = %.1f%%   窗口 %.1f%%'
          % (hb, nb_, 100 * hb / nb_, 100 * ((pB >= .5) == (y == 1)).mean()))
    res['ml_base'] = dict(seg_hit=hb, seg_n=nb_, seg_acc=round(hb / nb_, 4),
                          win_acc=round(float(((pB >= .5) == (y == 1)).mean()), 4))

    rows2 = []
    for i in range(len(L)):
        j0 = i - T.WIN + 1
        if j0 < 0 or S[i] != S[j0] or B[i] != B[j0]:
            continue
        blk = F[j0:i + 1]
        f = (list(blk.mean(0)) + list(blk.std(0)) + list(blk[-1] - blk[0]) +
             list(blk.max(0)) + list(blk.min(0)))
        t = SEG[j0:i + 1]
        f += [t[-1, 0], t[-1, 1], t[-1, 2], t[-1, 3], t[-1, 4], t[-1, 5],
              t[:, 0].max(), t[:, 1].max(), float(np.mean(t[:, 4]))]
        rows2.append(f)
    X2 = np.array(rows2, np.float64)[m]
    pC = loso(X2, y, sess)
    hc, nc, _ = seg_eval(pC, y, sess, seg)
    print('\n=== C ML %d 维（+nb/pb/negPk/posPk/EMA/差分）===' % X2.shape[1])
    print('  段级 %d/%d = %.1f%%   窗口 %.1f%%'
          % (hc, nc, 100 * hc / nc, 100 * ((pC >= .5) == (y == 1)).mean()))
    res['ml_time'] = dict(seg_hit=hc, seg_n=nc, seg_acc=round(hc / nc, 4),
                          win_acc=round(float(((pC >= .5) == (y == 1)).mean()), 4),
                          dim=int(X2.shape[1]))

    # ---------- D 负样本 ----------
    Xall, yall, sall, gall = T.windowize(F, L, S, B)
    mo = np.isin(yall, ['jump', 'duck'])
    mu, sd = Xall[mo].mean(0), Xall[mo].std(0) + 1e-9
    mdl = T.fit_mlp((Xall[mo] - mu) / sd,
                    (yall[mo] == 'jump').astype(float), epochs=600)
    print('\n=== D 负样本误报（全量模型）===')
    print('%-8s %6s %8s %8s' % ('label', '段', '判jump', '判duck'))
    neg = {}
    for lb in NEG_LABELS:
        sel = yall == lb
        if not sel.any():
            continue
        p = T.proba(mdl, (Xall[sel] - mu) / sd)
        o = {}
        for i in range(int(sel.sum())):
            o.setdefault((sall[sel][i], gall[sel][i]), []).append(p[i])
        sp = np.array([np.mean(v) for v in o.values()])
        nj, nd = int((sp >= .5).sum()), int((sp < .5).sum())
        print('%-8s %6d %8d %8d' % (lb, len(sp), nj, nd))
        neg[lb] = dict(seg=len(sp), jump=nj, duck=nd)
    res['negative'] = neg
    print('\n结论：第 3 层判据（规则或 ML）都不含"拒绝"能力，'
          '必须靠第 1 层 nz_share 与第 2 层 doppler_width 两道门。')

    out = os.path.join(ROOT, 'outputs', 'lane-eval',
                       'jumpduck-1t2r-vs-rule.json')
    json.dump(res, open(out, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('\nwritten outputs/lane-eval/jumpduck-1t2r-vs-rule.json')


if __name__ == '__main__':
    main()
