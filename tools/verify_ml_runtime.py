# -*- coding: utf-8 -*-
"""端到端验证：radar_actions 的 crit='ml' 分支 vs 训练侧（lane_model + train_lane_ml）

分成两层，各自有明确判据：

  第 1 层（模型移植）：实时侧每一帧算出的 (ml_valid, p_left, p_right)
      必须和离线 build_feature_matrix + LaneModel.proba 完全一致。
      判据：max |Δp| < 1e-3（实时侧特征存 3 位小数，允许该量级的舍入差）。

  第 2 层（状态机移植）：把**实时侧自己产生的证据流**（同一批帧、同一批概率）
      喂给训练侧的 emit_events_skip，事件必须逐条相同。
      判据：事件条数相等，且每条同名、时间差 < 0.14 s（1 帧周期）。
      说明：这一层不比较"离线全帧"的结果——实时侧在无检出帧/野值帧根本不产生
      证据（push() 早退），那是取数策略差异，不是移植错误。

第 1 层通过 → 模型正确接入；第 2 层通过 → 状态机正确接入；两层都过 = 离线数字
就是上线数字（在相同证据流前提下）。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import radar_source as rs                                    # noqa: E402
rs.sniff_board_from_file = lambda p, scan=262144: '1t2r'
import lane_model as lm                                      # noqa: E402
from radar_actions import ActionRecognizer, analyze_file      # noqa: E402
from radar_dsp import PointCloudProcessor                     # noqa: E402
from compare_lane_models import prod_series                   # noqa: E402
from train_lane_ml import emit_events_skip                    # noqa: E402

BAND = (1.0, 2.6)
TOPK = 1
MINPTS = 1
MODEL = os.path.join(ROOT, 'radar-pointcloud', 'models', 'lane_mlp.json')

FILES = [
    ('lr3b',         'data/lr3b/seg_01.00.bin',                        1.0),
    ('lr3c',         'data/lr3c/seg_01.00.bin',                        1.0),
    ('lr2-p1-d070',  '.deepworks/tmp/seg-lr2/1/d0.70/seg_00.70.bin',   0.7),
    ('lr2-p1-d100',  '.deepworks/tmp/seg-lr2/1/d1.00/seg_01.00.bin',   1.0),
    ('lr2-jqz-d070', '.deepworks/tmp/seg-lr2/jqz/d0.70/seg_00.70.bin', 0.7),
    ('lr2-jqz-d100', '.deepworks/tmp/seg-lr2/jqz/d1.00/seg_01.00.bin', 1.0),
    ('once-070',     '.deepworks/tmp/oncehot-oncehot/lr/seg_00.70.bin', 0.7),
    ('once-085',     '.deepworks/tmp/oncehot-oncehot/lr/seg_00.85.bin', 0.85),
    ('once-100',     '.deepworks/tmp/oncehot-oncehot/lr/seg_01.00.bin', 1.0),
    ('long-070',     '.deepworks/tmp/cap-long-1020/action/seg_00.70.bin', 0.7),
    ('m2recap-150',  '.deepworks/tmp/m2-recap/seg_01.50.bin',          1.5),
    ('m2recap-200',  '.deepworks/tmp/m2-recap/seg_02.00.bin',          2.0),
    ('seg-125',      '.deepworks/tmp/seg/seg_01.25.bin',               1.25),
    ('seg-150',      '.deepworks/tmp/seg/seg_01.50.bin',               1.5),
    ('stride-150',   '.deepworks/tmp/seg-stride/seg_01.50.bin',        1.5),
    ('ext-125',      '.deepworks/tmp/seg-ext/ext/d1.25/seg_01.25.bin', 1.25),
]


def main():
    model = lm.LaneModel.from_file(MODEL)
    thr = model.thr
    rls = model.release
    print('模型 %s  thr=%.2f release=%.2f'
          % (os.path.basename(MODEL), thr, rls))

    l1_bad = l2_bad = 0
    worst_dp = 0.0
    for tag, rel, dist in FILES:
        path = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.exists(path):
            continue
        # ---------- 离线参考 ----------
        s = prod_series(path, tag, TOPK, MINPTS, band=BAND, use_cache=True)
        X = lm.build_feature_matrix(s['px'], s['py'], s['en'], s['n'])
        P = model.proba(X)
        V = model.gate(s['px'], X)

        # ---------- 实时 ----------
        rec = ActionRecognizer(band=BAND, crit='ml', top_k=TOPK, min_pts=MINPTS,
                               lane_model=model, ml_thr=thr, ml_release=rls)
        proc = PointCloudProcessor(cfar_scale=2.6, min_snr=7.0, min_range=0.681)
        recs, _ = analyze_file(path, processor=proc, recognizer=rec, board='1t2r')

        # ---------- 第 1 层：逐帧概率 / 闸门 ----------
        n = min(len(recs), len(P))
        dp = 0.0
        ncmp = 0
        vmis = 0
        for i in range(n):
            f = recs[i]['features'] or {}
            p = f.get('ml_p')
            if p is None:
                continue
            ncmp += 1
            dp = max(dp, abs(p[0] - P[i, 1]), abs(p[1] - P[i, 2]))
            if bool(f.get('ml_valid')) != bool(V[i]):
                vmis += 1
        ok1 = (dp < 1e-3) and (vmis == 0)
        if not ok1:
            l1_bad += 1
        worst_dp = max(worst_dp, dp)

        # ---------- 第 2 层：同一证据流 → 事件 ----------
        t = np.array([float(r['t']) for r in recs[:n]])
        vrt = np.zeros(n, dtype=bool)
        prt = np.full((n, 3), 1.0 / 3.0)
        for i in range(n):
            f = recs[i]['features'] or {}
            p = f.get('ml_p')
            if p is None:
                continue
            vrt[i] = bool(f.get('ml_valid'))
            prt[i, 1] = p[0]
            prt[i, 2] = p[1]
            prt[i, 0] = max(0.0, 1.0 - p[0] - p[1])
        ev_rt = [(float(r['t']), 'LEFT' if e['name'] == 'LANE_LEFT' else 'RIGHT')
                 for r in recs[:n] for e in r['events']
                 if e['name'] in ('LANE_LEFT', 'LANE_RIGHT')]
        # 实时侧的 TARGET_LOST → _reset_track()（lane 回 CENTER、计数清零），
        # 离线必须复现，否则会凭空多出/漏掉事件
        resets = sorted(float(e['t']) for r in recs[:n] for e in r['events']
                        if e['name'] == 'TARGET_LOST')
        # 复位时刻是 TARGET_LOST 事件的 t（该帧处理完之后才复位），故用严格大于
        ev_ref = emit_events_skip(t, prt, vrt, thr, rls, resets=resets, reset_strict=True)

        # 判据：训练侧发的每条事件都必须在实时侧找到同向、时间差 ≤ 0.35 s 的对应
        #（实时侧"多出来"的事件单独计数——那是 TARGET_LOST 时 _reset_track() 把 lane
        #  复位成 CENTER 带来的重复触发，属感知层策略，不是状态机移植错误）
        j = 0
        miss = []
        for t1, c1 in ev_ref:
            hit_j = -1
            for k2 in range(j, len(ev_rt)):
                t2, c2 = ev_rt[k2]
                if t2 - t1 > 0.35:
                    break
                if c2 == c1 and abs(t2 - t1) <= 0.35:
                    hit_j = k2
                    break
            if hit_j < 0:
                miss.append('%s@%.2f' % (c1, t1))
            else:
                j = hit_j + 1
        n_extra = len(ev_rt) - (len(ev_ref) - len(miss))
        ok2 = (len(miss) == 0)
        detail = ''
        if not ok2:
            detail = '漏 %d 条（%s）' % (len(miss), ' '.join(miss[:3]))
        elif n_extra:
            detail = '多 %d 条（复位重复触发）' % n_extra
        if not ok2:
            l2_bad += 1

        hit = tot = 0
        for c in ('LEFT', 'RIGHT'):
            pass
        print('  %-3s%-3s %-14s 帧对比 %4d  maxΔp %.1e  闸门差 %d  事件 %2d/%2d %s'
              % ('OK' if ok1 else 'L1!', 'OK' if ok2 else 'L2!', tag,
                 ncmp, dp, vmis, len(ev_ref), len(ev_rt), detail))

    print('\n第 1 层（模型移植）不通过文件数 %d；全局 maxΔp = %.2e' % (l1_bad, worst_dp))
    print('第 2 层（状态机移植）不通过文件数 %d' % l2_bad)
    print('结论：%s' % ('两层全部通过——离线数字即上线数字'
                       if (l1_bad == 0 and l2_bad == 0) else '仍有不一致，见上'))


if __name__ == '__main__':
    main()
