# -*- coding: utf-8 -*-
"""采集电池数据的可分性评估（可复用）。

用法：
  python tools/eval_battery.py data/jumpduck_battery/<目录>
  python tools/eval_battery.py data/jumpduck_battery/<目录> --csv out.csv

输出：逐块特征表、按动作分组统计、三分类混淆矩阵、静息误报率、Cohen's d。
判据阈值均为**提案**，不是验收结论。
"""
import argparse, glob, io, json, os, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
import numpy as np
import vguide_1t2r as vg

# 提案阈值（待小组确认）：横向 vs 纵向靠谱宽分开
T_W_LAT = 0.22      # w90 低于此值判为横向移动
T_NZ_ON = 0.30      # nz90 高于此值判为"有身体动作"
LABEL_GROUP = {
    'jump_m': 'vertical', 'jump_s': 'vertical', 'jump_l': 'vertical',
    'duck_m': 'vertical', 'duck_s': 'vertical', 'duck_l': 'vertical',
    'combo_j': 'vertical', 'combo_d': 'vertical', 'alt': 'vertical',
    'bend': 'vertical', 'turn': 'vertical', 'walk': 'vertical',
    'left': 'lateral', 'right': 'lateral',
}
TRUE_GROUP = {'jump_m': 'jump', 'jump_s': 'jump', 'jump_l': 'jump',
              'duck_m': 'duck', 'duck_s': 'duck', 'duck_l': 'duck',
              'combo_j': 'jump', 'combo_d': 'duck', 'alt': 'alt',
              'bend': 'bend#neg', 'turn': 'turn#neg', 'walk': 'walk#neg',
              'left': 'left', 'right': 'right', 'rest0': 'rest'}


def classify(nz90, w90):
    if nz90 < T_NZ_ON:
        return 'none'
    if w90 < T_W_LAT:
        return 'lateral'
    return 'vertical'


def cohen_d(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return float('nan')
    s = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) /
                max(1, len(a) + len(b) - 2))
    return (a.mean() - b.mean()) / s if s > 0 else float('nan')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    ap.add_argument('--csv')
    a = ap.parse_args()
    D = a.dir if os.path.isabs(a.dir) else os.path.join(ROOT, a.dir)
    lab = json.load(open(os.path.join(D, 'labels.json'), encoding='utf-8'))
    band = tuple(lab['meta']['band'])
    print('目录 %s' % D)
    print('受试者 %s  身高 %s  band=%s  fps=%.2f  块数=%d'
          % (lab['meta']['subject'], lab['meta'].get('height') or '-', band,
             lab['meta']['fps'], len(lab['blocks'])))

    rows = []
    rest_frames = []      # 全部 rest 标签帧的 nz
    for rec in lab['blocks']:
        p = os.path.join(D, 'blocks', '%03d_%s.npz' % (rec['i'], rec['id']))
        if not os.path.exists(p):
            continue
        q = np.load(p, allow_pickle=False)
        z, lb = q['z'], q['label']
        feats = [vg.frame_feats(zz.astype('complex64'), band) for zz in z]
        body = [f for f, l in zip(feats, lb) if l != 'rest' and f]
        rest = [f for f, l in zip(feats, lb) if l == 'rest' and f]
        rest_frames += [f['nz_share'] for f in rest]
        if not body:
            # 纯静息块（rest0）没有动作帧，单独记为 none 类样本
            if rest and rec['id'] == 'rest0':
                nz = np.array([f['nz_share'] for f in rest])
                w = np.array([f['doppler_width'] for f in rest])
                vr = np.array([f['v_r'] for f in rest])
                rows.append(dict(i=rec['i'], id='rest0', truth='rest', kind='cal',
                                 n=len(rest),
                                 nz50=np.median(nz), nz90=np.percentile(nz, 90),
                                 w50=np.median(w), w90=np.percentile(w, 90),
                                 wmax=w.max(), pos=float((vr > 0.05).mean()),
                                 neg=float((vr < -0.05).mean()),
                                 vr90=float(np.percentile(np.abs(vr), 90))))
            continue
        nz = np.array([f['nz_share'] for f in body])
        w = np.array([f['doppler_width'] for f in body])
        vr = np.array([f['v_r'] for f in body])
        rows.append(dict(i=rec['i'], id=rec['id'], truth=rec['truth'], kind=rec['kind'],
                         n=len(body),
                         nz50=np.median(nz), nz90=np.percentile(nz, 90),
                         w50=np.median(w), w90=np.percentile(w, 90),
                         wmax=w.max(), pos=float((vr > 0.05).mean()),
                         neg=float((vr < -0.05).mean()),
                         vr90=float(np.percentile(np.abs(vr), 90))))

    hdr = '%-3s %-9s %-6s %4s %6s %6s %6s %6s %6s %6s'
    print('\n' + hdr % ('#', 'id', 'truth', 'n', 'nz50', 'nz90', 'w50', 'w90', 'wmax', 'vr90'))
    for r in rows:
        print('%-3d %-9s %-6s %4d %6.3f %6.3f %6.3f %6.3f %6.3f %6.3f'
              % (r['i'], r['id'], r['truth'], r['n'], r['nz50'], r['nz90'],
                 r['w50'], r['w90'], r['wmax'], r['vr90']))

    print('\n=== 按动作分组 ===')
    order = ['jump_s', 'jump_m', 'jump_l', 'duck_s', 'duck_m', 'duck_l',
             'combo_j', 'combo_d', 'alt', 'bend', 'turn', 'walk', 'left', 'right']
    g = {}
    for r in rows:
        g.setdefault(r['id'], []).append(r)
    print('%-9s %3s  %-28s %-28s' % ('id', 'n', 'w90', 'nz90'))
    for k in order:
        if k not in g:
            continue
        print('%-9s %3d  %-28s %-28s'
              % (k, len(g[k]),
                 ' '.join('%.2f' % x['w90'] for x in g[k]),
                 ' '.join('%.2f' % x['nz90'] for x in g[k])))

    print('\n=== 三分类混淆矩阵（块级，提案阈值）===')
    print('判据: nz90<%.2f→none ; w90<%.2f→lateral ; else→vertical' % (T_NZ_ON, T_W_LAT))
    cm = {}
    for r in rows:
        pred = classify(r['nz90'], r['w90'])
        true = 'none' if r['id'] == 'rest0' else LABEL_GROUP.get(r['id'], '?')
        cm.setdefault(true, {}).setdefault(pred, []).append(r['id'])
    print('%-10s %-34s %-34s %s' % ('真值', 'none', 'lateral', 'vertical'))
    for true in ['none', 'lateral', 'vertical']:
        cells = cm.get(true, {})
        def fmt(k):
            v = cells.get(k, [])
            return ('%d [%s]' % (len(v), ','.join(v))) if v else '0'
        print('%-10s %-34s %-34s %s' % (true, fmt('none'), fmt('lateral'), fmt('vertical')))

    print('\n=== 逐帧误报（静息）===')
    rf = np.array(rest_frames)
    print('动作后 rest 段共 %d 帧：nz 中位 %.3f，>0.30 的帧占 %.1f%%'
          % (len(rf), np.median(rf), 100.0 * (rf > T_NZ_ON).mean()))
    r0 = [r for r in rows if r['id'] == 'rest0']
    if r0:
        print('采集前纯静息 rest0：nz90=%.3f（即 90%% 的帧低于此值）' % r0[0]['nz90'])

    print('\n=== 类间可分性（Cohen d，块级 w90）===')
    def w_of(k):
        return [x['w90'] for x in g.get(k, [])]
    pairs = [('jump_m', 'duck_m'), ('jump_l', 'duck_l'), ('jump_s', 'duck_s'),
             ('jump_m', 'bend'), ('duck_m', 'bend'), ('left', 'jump_m'),
             ('left', 'duck_m')]
    for x, y in pairs:
        d = cohen_d(w_of(x), w_of(y))
        print('%-9s vs %-9s  d=%+.2f  %s'
              % (x, y, d, '可分' if abs(d) >= 0.8 else ('中等' if abs(d) >= 0.5 else '不可分')))

    if a.csv:
        with open(a.csv, 'w', encoding='utf-8') as f:
            f.write('i,id,truth,n,nz50,nz90,w50,w90,wmax,vr90\n')
            for r in rows:
                f.write('%d,%s,%s,%d,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f\n'
                        % (r['i'], r['id'], r['truth'], r['n'], r['nz50'], r['nz90'],
                           r['w50'], r['w90'], r['wmax'], r['vr90']))
        print('\nCSV -> %s' % a.csv)


if __name__ == '__main__':
    main()
