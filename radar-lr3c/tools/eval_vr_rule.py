# -*- coding: utf-8 -*-
"""跳/蹲 判别规则的离线评估（径向速度段持续时间法）。

判据来源（2026-11-13，两批数据共 40 个跳/蹲样本）：
  跳：最长连续负段 1-2 帧，最长连续正段 1-2 帧  （7.7 Hz 下 <0.3 s，采样抓不全）
  蹲：最长连续负段 3-4 帧，最长连续正段 3-5 帧  （受控慢动作，>0.4 s）
  幅值不可用：蹲的起身正峰（0.88）比跳的正峰（0.46）还大。

"下蹲后起立"的处理就是本规则的自然结果：
  一旦负段达到 3 帧即确认 DUCK，其后的正段属于起身后摇，不再判 JUMP。

用法：
  python tools/eval_vr_rule.py data/jumpduck_battery/<目录> [<目录2> ...]
  python tools/eval_vr_rule.py data/jumpduck_battery/* --csv outputs/rule.csv
"""
import argparse, glob, io, json, os, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
import numpy as np
import vguide_1t2r as vg

# 提案阈值（待小组确认）
V_T = 0.25        # 径向速度门限 m/s
NZ_T = 0.50       # 有动作门限（nz p90）
W_LAT = 0.22      # 谱宽 p90 低于此值 → 切向运动（垂直于视线）
DUR = 3           # 连续同号 >=3 帧 → 慢动作（蹲）；<=2 帧 → 快动作（跳）

TRUTH = {'jump_m': 'jump', 'jump_s': 'jump', 'jump_l': 'jump', 'jump_k': 'jump', 'combo_j': 'jump',
         'duck_m': 'duck', 'duck_s': 'duck', 'duck_l': 'duck', 'combo_d': 'duck',
         'alt': 'mix', 'bend': 'bend#neg', 'turn': 'turn#neg', 'walk': 'walk#neg',
         'left': 'left', 'right': 'right'}


def runs(v, sign):
    """连续同号超门限的段；允许段内 1 帧掉回门限（抗抖动）。"""
    s = 1 if sign > 0 else -1
    out, i, n = [], 0, len(v)
    while i < n:
        if s * v[i] > V_T:
            j, gap = i, 0
            while j + 1 < n:
                if s * v[j + 1] > V_T:
                    j, gap = j + 1, 0
                elif gap < 1:
                    j, gap = j + 1, gap + 1
                else:
                    break
            seg = v[i:j + 1]
            out.append((i, j, float(np.max(s * seg)), int((s * seg > V_T).sum())))
            i = j + 1
        else:
            i += 1
    return out


def judge(v, nz, w):
    """返回 (判定, 最长负段帧数, 最长正段帧数)。判定 ∈ none/tangential/jump/duck（tangential=垂直于视线的切向运动）。"""
    if np.percentile(nz, 90) < NZ_T:
        return 'none', 0, 0
    if np.percentile(w, 90) < W_LAT:
        return 'tangential', 0, 0
    neg, pos = runs(v, -1), runs(v, 1)
    nb = max((x[3] for x in neg), default=0)
    pb = max((x[3] for x in pos), default=0)
    if max(nb, pb) >= DUR:
        return 'duck', nb, pb
    if pb >= 1:
        return 'jump', nb, pb
    return 'none', nb, pb


def frame_feats_of(path, band):
    q = np.load(path, allow_pickle=False)
    z, lb = q['z'], q['label']
    return [vg.frame_feats(zz.astype('complex64'), band)
            for zz, l in zip(z, lb) if l != 'rest']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dirs', nargs='+')
    ap.add_argument('--csv')
    a = ap.parse_args()
    dirs = []
    for d in a.dirs:
        dirs += sorted(glob.glob(d)) if any(ch in d for ch in '*?') else [d]

    rows = []
    for D in dirs:
        D = D if os.path.isabs(D) else os.path.join(ROOT, D)
        lj = os.path.join(D, 'labels.json')
        if not os.path.exists(lj):
            continue
        lab = json.load(open(lj, encoding='utf-8'))
        band = tuple(lab['meta']['band'])
        tag = os.path.basename(D.rstrip('\\/'))
        for rec in lab['blocks']:
            if rec['id'] not in TRUTH:
                continue
            p = os.path.join(D, 'blocks', '%03d_%s.npz' % (rec['i'], rec['id']))
            if not os.path.exists(p):
                continue
            fs = [f for f in frame_feats_of(p, band) if f]
            if not fs:
                continue
            v = np.array([f['v_r'] for f in fs])
            nz = np.array([f['nz_share'] for f in fs])
            w = np.array([f['doppler_width'] for f in fs])
            pred, nb, pb = judge(v, nz, w)
            rows.append(dict(src=tag, i=rec['i'], id=rec['id'], truth=TRUTH[rec['id']],
                             pred=pred, nb=nb, pb=pb))

    print('%-20s %-3s %-8s %-9s %-7s %4s %4s %s'
          % ('run', '#', 'id', 'truth', 'pred', 'nb', 'pb', 'ok'))
    for r in rows:
        print('%-20s %-3d %-8s %-9s %-7s %4d %4d %s'
              % (r['src'][-4:], r['i'], r['id'], r['truth'], r['pred'],
                 r['nb'], r['pb'], '' if r['truth'] == r['pred'] else 'X'))

    print('\n=== 混淆（真值 -> 判定）===')
    cm = {}
    for r in rows:
        cm.setdefault(r['truth'], {}).setdefault(r['pred'], 0)
        cm[r['truth']][r['pred']] += 1
    for t in ['jump', 'duck', 'mix', 'bend#neg', 'turn#neg', 'walk#neg', 'left', 'right']:
        if t in cm:
            print('%-10s %s' % (t, cm[t]))

    jd = [r for r in rows if r['truth'] in ('jump', 'duck')]
    if jd:
        ok = sum(1 for r in jd if r['truth'] == r['pred'])
        print('\n跳/蹲 %d 样本，判对 %d = %.1f%%' % (len(jd), ok, 100.0 * ok / len(jd)))
        for src in sorted({r['src'] for r in jd}):
            s = [r for r in jd if r['src'] == src]
            print('  %s: %d/%d' % (src, sum(1 for r in s if r['truth'] == r['pred']), len(s)))
    lat = [r for r in rows if r['truth'] in ('left', 'right')]
    if lat:
        ok = sum(1 for r in lat if r['pred'] == 'tangential')
        print('切向 %d 样本，判对 %d = %.1f%%' % (len(lat), ok, 100.0 * ok / len(lat)))
    neg = [r for r in rows if r['truth'].endswith('#neg')]
    if neg:
        ok = sum(1 for r in neg if r['pred'] == 'none')
        print('负样本 %d 样本，正确静默 %d = %.1f%%' % (len(neg), ok, 100.0 * ok / len(neg)))

    if a.csv:
        with open(a.csv, 'w', encoding='utf-8') as f:
            f.write('src,i,id,truth,pred,nb,pb\n')
            for r in rows:
                f.write('%s,%d,%s,%s,%s,%d,%d\n' % (r['src'], r['i'], r['id'],
                                                    r['truth'], r['pred'], r['nb'], r['pb']))
        print('\nCSV -> %s' % a.csv)


if __name__ == '__main__':
    main()
