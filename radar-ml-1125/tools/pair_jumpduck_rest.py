# -*- coding: utf-8 -*-
"""跳/蹲的「前后对照」检验：把每个动作段与时间上最近的 rest 段配对。

复现：python tools/pair_jumpduck_rest.py
产物：outputs/lane-eval/jumpduck-1t2r-paired.json

为什么这样做：录制顺序里 jump 整块在前、duck 整块在后，
直接比较两者会把「时间/站位漂移」算作「动作差异」。
但每个动作段旁边都紧挨着一个 rest 段，用
    Δ = 动作段均值 - 最近 rest 段均值
做配对比较，慢漂移在相减时被抵消掉。

若 Δ_jump 与 Δ_duck 仍然显著不同，才说明「向上/向下」真的有信息。
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import train_jumpduck_1t2r as T  # noqa: E402

NAMES = ['n', 'r_med', 'r_top', 'v_w', 'v_top', 'v_abs', 'v_p90',
         'v_pos', 'v_neg', 'v_std', 'deg_med']


def auc(a, b):
    if len(a) == 0 or len(b) == 0:
        return float('nan')
    allv = np.concatenate([a, b])
    y = np.concatenate([np.ones(len(a)), np.zeros(len(b))])
    rk = allv.argsort().argsort() + 1.0
    return float((rk[y == 1].sum() - len(a) * (len(a) + 1) / 2.0) /
                 (len(a) * len(b)))


def main():
    F, L, S, B = T.load_arrays()
    segs = {}
    for i in range(len(L)):
        segs.setdefault((S[i], B[i]), []).append(i)

    rows = []
    for s in sorted(set(S.tolist())):
        ks = sorted([k for k in segs if k[0] == s], key=lambda k: min(segs[k]))
        lab = [L[segs[k][0]] for k in ks]
        pos = [float(np.mean([i for i in segs[k]])) for k in ks]
        means = [F[segs[k]].mean(0) for k in ks]
        rests = [p for p, l in zip(pos, lab) if l == 'rest']
        if not rests:
            continue
        for p, l, m in zip(pos, lab, means):
            if l not in ('jump', 'duck'):
                continue
            r = min(rests, key=lambda q: abs(q - p))
            ri = int(round(r))
            k = [kk for kk in ks if min(segs[kk]) <= ri <= max(segs[kk])][0]
            base = F[segs[k]].mean(0)
            rows.append(dict(session=s, label=l, delta=m - base,
                             gap=abs(p - r) / 7.73))

    print('配对数 %d（jump %d / duck %d），平均间隔 %.1f s' %
          (len(rows), sum(r['label'] == 'jump' for r in rows),
           sum(r['label'] == 'duck' for r in rows),
           float(np.mean([r['gap'] for r in rows]))))

    J = np.array([r['delta'] for r in rows if r['label'] == 'jump'])
    D = np.array([r['delta'] for r in rows if r['label'] == 'duck'])
    print('\n=== Δ(动作 - 最近 rest) 的 jump vs duck ===')
    print('%-9s %16s %16s %8s' % ('特征', 'jump Δ 中位', 'duck Δ 中位', 'AUC'))
    order = []
    for c in range(len(NAMES)):
        a, b = J[:, c], D[:, c]
        print('%-9s %+16.3f %+16.3f %8.3f'
              % (NAMES[c], np.median(a), np.median(b), auc(a, b)))
        order.append((abs(auc(a, b) - 0.5), NAMES[c], float(auc(a, b))))

    # 逐会话看方向是否一致（泛化性）
    print('\n=== 逐会话：Δ r_med 与 Δ v_std 的中位（jump / duck）===')
    for s in sorted(set(r['session'] for r in rows)):
        jj = [r['delta'] for r in rows if r['session'] == s and r['label'] == 'jump']
        dd = [r['delta'] for r in rows if r['session'] == s and r['label'] == 'duck']
        if not jj or not dd:
            continue
        jj, dd = np.array(jj), np.array(dd)
        print('  %-16s r_med %+.2f/%+.2f   v_std %+.2f/%+.2f   n %+.2f/%+.2f'
              % (s[-14:], np.median(jj[:, 1]), np.median(dd[:, 1]),
                 np.median(jj[:, 9]), np.median(dd[:, 9]),
                 np.median(jj[:, 0]), np.median(dd[:, 0])))

    order.sort(reverse=True)
    print('\n=== 最可分的 3 个 Δ 特征 ===')
    for _, nm, a in order[:3]:
        print('  %-9s AUC=%.3f' % (nm, a))

    json.dump(dict(n=len(rows), nj=len(J), nd=len(D),
                   auc={nm: round(a, 4) for nm, a in
                        [(o[1], o[2]) for o in order]},
                   pairs=[dict(session=r['session'], label=r['label'],
                               gap=round(r['gap'], 2),
                               delta={NAMES[c]: round(float(r['delta'][c]), 3)
                                      for c in range(len(NAMES))})
                          for r in rows]),
              open(os.path.join(T.ROOT, 'outputs', 'lane-eval',
                                'jumpduck-1t2r-paired.json'), 'w',
                   encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\nwritten outputs/lane-eval/jumpduck-1t2r-paired.json')


if __name__ == '__main__':
    main()
