# -*- coding: utf-8 -*-
"""上线口径评估：直接跑**实时链路**（ActionRecognizer + analyze_file），
统计 16 个录制文件上的左右移命中率、延迟、误报。

为什么需要这个脚本：`train_lane_ml.py` 的 LOSO 是在**离线特征矩阵**上算的，
离线可以拿到"整段数据的因果窗口"，但拿不到实时链路的三个真实约束：
  1. 无检出点的帧 push() 直接早退 → 该帧没有证据；
  2. 野值帧（max_step 剔除）同样没有证据；
  3. TARGET_LOST 时 _reset_track() 把 lane 复位成 CENTER。

本脚本不模拟以上任何一条，而是**真的跑产线代码**，因此它的数字就是演示当天
会发生的数字。同一个脚本里同时跑 crit='ml' 与 crit='xbase'（规则），
两者走完全相同的取数与打分路径，可直接对比。

用法：
    python tools/eval_lane_ml_runtime.py                     # 默认 ml 0.60 + xbase
    python tools/eval_lane_ml_runtime.py --thr 0.55          # 换 ML 阈值
    python tools/eval_lane_ml_runtime.py --only ml           # 只跑一路
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, HERE)

import radar_source as rs                                      # noqa: E402
rs.sniff_board_from_file = lambda p, scan=262144: '1t2r'
import lane_model as lm                                        # noqa: E402
from radar_actions import ActionRecognizer, analyze_file        # noqa: E402
from radar_dsp import PointCloudProcessor                       # noqa: E402
from compare_lane_models import evaluate, labels_of, FILES      # noqa: E402

BAND = (1.0, 2.6)
TOPK = 1
MINPTS = 1
MODEL = os.path.join(ROOT, 'radar-pointcloud', 'models', 'lane_mlp.json')
OUT = os.path.join(ROOT, 'outputs', 'lane-eval', 'ml-runtime.json')


def run_one(path, crit, model=None, thr=None, release=None):
    kw = {}
    if crit == 'ml':
        kw = dict(lane_model=model, ml_thr=thr, ml_release=release)
    rec = ActionRecognizer(band=BAND, crit=crit, top_k=TOPK, min_pts=MINPTS, **kw)
    proc = PointCloudProcessor(cfar_scale=2.6, min_snr=7.0, min_range=0.681)
    t0 = time.time()
    recs, _ = analyze_file(path, processor=proc, recognizer=rec, board='1t2r')
    evs = [(float(r['t']), 'LEFT' if e['name'] == 'LANE_LEFT' else 'RIGHT')
           for r in recs for e in r['events']
           if e['name'] in ('LANE_LEFT', 'LANE_RIGHT')]
    span = float(recs[-1]['t']) if recs else 0.0
    return evs, span, time.time() - t0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--thr', type=float, default=None, help='ML 触发阈值，默认取模型里的值')
    ap.add_argument('--release', type=float, default=None)
    ap.add_argument('--only', choices=('ml', 'xbase', 'both'), default='both')
    ap.add_argument('--out', default=OUT)
    a = ap.parse_args(argv)

    model = lm.LaneModel.from_file(MODEL)
    thr = model.thr if a.thr is None else a.thr
    rls = model.release if a.release is None else a.release
    crits = []
    if a.only in ('ml', 'both'):
        crits.append(('ml', thr))
    if a.only in ('xbase', 'both'):
        crits.append(('xbase', None))

    print('模型 %s  thr=%.2f release=%.2f  band=%s top_k=%d min_pts=%d'
          % (os.path.basename(MODEL), thr, rls, BAND, TOPK, MINPTS))

    res = {}
    for crit, thr_c in crits:
        hit = {'LEFT': 0, 'RIGHT': 0}
        tot = {'LEFT': 0, 'RIGHT': 0}
        lat = []
        fp = 0
        span_all = 0.0
        per_file = []
        t_all = 0.0
        print('\n===== crit=%s =====' % crit)
        for tag, rel, _dist in FILES:
            path = os.path.join(ROOT, rel.replace('/', os.sep))
            if not os.path.exists(path):
                print('  跳过（无文件）%s' % tag)
                continue
            labs = labels_of(path)
            evs, span, dt = run_one(path, crit, model, thr_c, rls)
            r = evaluate(evs, labs, span)
            hit['LEFT'] += r['hit']['LEFT']
            hit['RIGHT'] += r['hit']['RIGHT']
            tot['LEFT'] += r['tot']['LEFT']
            tot['RIGHT'] += r['tot']['RIGHT']
            lat += r['lat']
            fp += r['fp']
            span_all += span
            t_all += dt
            n = r['hit']['LEFT'] + r['hit']['RIGHT']
            m = r['tot']['LEFT'] + r['tot']['RIGHT']
            per_file.append(dict(tag=tag, hit=n, tot=m, fp=r['fp'],
                                 fp_per_min=round(r['fp_per_min'], 2),
                                 min=round(span / 60.0, 2)))
            print('  %-14s %2d/%-2d = %5.1f%%  误报 %2d (%4.1f/min)  %.1fs'
                  % (tag, n, m, 100.0 * n / max(m, 1), r['fp'], r['fp_per_min'], dt))
        n = hit['LEFT'] + hit['RIGHT']
        m = tot['LEFT'] + tot['RIGHT']
        res[crit] = dict(
            thr=thr_c, hit=hit, tot=tot, n=n, m=m,
            acc=round(n / max(m, 1), 4),
            lat_median=round(float(np.median(lat)), 3) if lat else None,
            fp=fp, fp_per_min=round(fp / max(span_all / 60.0, 1e-6), 2),
            minutes=round(span_all / 60.0, 1), runtime_s=round(t_all, 1),
            per_file=per_file)
        print('  合计 %d/%d = %.1f%%  延迟中位 %s s  误报 %d (%.1f/min)'
              % (n, m, 100.0 * n / max(m, 1),
                 res[crit]['lat_median'], fp, res[crit]['fp_per_min']))

    if 'ml' in res and 'xbase' in res:
        d = (res['ml']['acc'] - res['xbase']['acc']) * 100
        print('\nML - 规则 = %+.1f 个百分点（上线口径，同一取数路径）' % d)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    meta = dict(band=list(BAND), top_k=TOPK, min_pts=MINPTS,
                model=os.path.relpath(MODEL, ROOT).replace(os.sep, '/'),
                note='实时链路实测（analyze_file 真跑产线代码），非离线重放')
    # 按判据合并：--only ml 重跑时不要冲掉同一文件里的 xbase 结果
    old = {}
    if os.path.exists(a.out):
        try:
            with open(a.out, encoding='utf-8') as f:
                old = json.load(f).get('results', {})
        except Exception:
            old = {}
    merged = dict(old)
    merged.update(res)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(dict(meta=meta, results=merged), f, ensure_ascii=False, indent=2)
    print('\n已写 %s' % os.path.relpath(a.out, ROOT))
    return res


if __name__ == '__main__':
    main()
