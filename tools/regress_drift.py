# -*- coding: utf-8 -*-
"""crit='drift' 的**产线路径**回归锁定（只读，不写任何录制文件）。

为什么必须单独有这一个脚本：
    `level_recog.py` 的 9/10 是拿「带内**最强点**的 x」算出来的，而产线
    `radar_actions.ActionRecognizer` 用的是「带内**幅值加权质心** px」——
    **两者是不同的统计量**。所以那个 9/10 不能直接当成产线的结果，
    本脚本走 `analyze_file` + `ActionRecognizer` 真实路径重测一遍。

用法：
    python .deepworks/tmp/regress_drift.py            # 只报告
    python .deepworks/tmp/regress_drift.py --strict   # 断言（回归锁定）
"""
import argparse
import io
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
RADAR = os.path.join(ROOT, 'radar-pointcloud')
SEG = os.path.join(HERE, 'seg')
sys.path.insert(0, RADAR)

from radar_actions import ActionRecognizer, analyze_file      # noqa: E402
from radar_dsp import PointCloudProcessor                     # noqa: E402
import label_tool as lt                                       # noqa: E402

BAND = (1.36, 2.04)          # B3：站位 1.25/1.50 m 时人所在距离门（见 devlog §7.26）
CFAR = 2.6
SNR = 7.0
TOL = 0.5

# ---- 回归锁定的期望值（严格模式下必须命中；数字来源见 devlog §7.28 / §7.34）----
# 记录的是**产线路径**实测值，不是 level_recog.py 的 9/10。
# 注意：能跑通的配置是 band=(1.36,2.04) + crit='drift' + top_k=1 + min_pts=1。
# ★ 2026-10-19 更新（devlog §7.34）：drift 分支的 hold 死条件已修（释放门加滞回
#   DRIFT_RELEASE_DEFAULT=0.10）⇒ seg_01.25 由 (4,4,0) 升为 (4,5,0)。
# ★ 2026-10-19 二次更新（devlog §7.36）：释放端改为需连续 DRIFT_RELEASE_CONFIRM_DEFAULT=3
#   帧确认（与触发端 confirm 对称）⇒ seg_01.50 的窗边缘重复触发 RIGHT@72.91 消失，
#   误报上界由 1 **收紧回 0**。依据：A/B 8 候选 × 7 文件，M=2/M=3 结果一致，且 7 个
#   文件里其余事件逐条不变（.deepworks/tmp/ab-dup-variants-after.log）。
EXPECT = {
    'seg_01.25': {'drift/最强点': (4, 5, 0)},
    'seg_01.50': {'drift/最强点': (0, 4, 0)},
}


def read_labels(stem):
    return lt.load_labels(os.path.join(SEG, stem + '.labels.jsonl'))


def run_events(path, crit, left=None, right=None, rest_win=None,
               top_k=0, min_pts=2, band=None, board=None, extra=None):
    """跑产线路径取 LANE_* 事件。

    top_k=0, min_pts=2 → 产线默认：带内**幅值加权质心**
    top_k=1, min_pts=1 → 只留最强点：`px` 退化为「最强点的 x」，
                         即 level_recog.py 用的那个统计量
    board=None → analyze_file 会嗅探文件板型。**不要依赖它**：1T2R 的裸帧录制
    会被误判成 2T4R（见 docs/progress-radar-pointcloud.md「板型嗅探回归」），
    调用方应显式传 board='1t2r' / '2t4r'。
    """
    kw = dict(band=band or BAND, crit=crit, top_k=top_k, min_pts=min_pts)
    if extra:
        kw.update(extra)
    if left is not None:
        kw['drift_left'] = left
    if right is not None:
        kw['drift_right'] = right
    if rest_win is not None:
        kw['rest_win'] = rest_win
    rec = ActionRecognizer(**kw)
    proc = PointCloudProcessor(cfar_scale=CFAR, min_snr=SNR, min_range=0.681)
    records, info = analyze_file(path, processor=proc, recognizer=rec, board=board)
    evs = []
    for r in records:
        for e in r['events']:
            if e['name'] in ('LANE_LEFT', 'LANE_RIGHT'):
                evs.append((e['t'], 'LEFT' if e['name'] == 'LANE_LEFT' else 'RIGHT'))
    span = records[-1]['t'] if records else 1.0
    return evs, span, info


def run_events_auto(path, crit='drift', top_k=1, min_pts=1, board=None, extra=None):
    """走**产线自动选带**（band='auto'）：预热若干帧后自行锁定距离门。

    存在的意义：手工带 B3 是「已知答案」，而现场站位是未知的。若自动选带
    不能复现同一批数字，它就只是纸面功能。返回第三个值是识别器本身，
    以便断言它到底选了哪个门、第几帧锁定。

    board 同 run_events：不要靠嗅探，显式传板型。
    """
    rec = ActionRecognizer(band='auto', crit=crit, top_k=top_k, min_pts=min_pts,
                           **(extra or {}))
    proc = PointCloudProcessor(cfar_scale=CFAR, min_snr=SNR, min_range=0.681)
    records, info = analyze_file(path, processor=proc, recognizer=rec, board=board)
    evs = []
    for r in records:
        for e in r['events']:
            if e['name'] in ('LANE_LEFT', 'LANE_RIGHT'):
                evs.append((e['t'], 'LEFT' if e['name'] == 'LANE_LEFT' else 'RIGHT'))
    span = records[-1]['t'] if records else 1.0
    return evs, span, rec


def score(evs, labels, tol=TOL):
    """窗级召回 + 误报。每条事件最多匹配一个真值窗（与 level_recog 同口径）。"""
    used = set()
    hit = {'LEFT': 0, 'RIGHT': 0}
    tot = {'LEFT': 0, 'RIGHT': 0}
    for lb in labels:
        k = str(lb.get('action', '')).upper()
        if k in tot:
            tot[k] += 1
    fp = 0
    for (t, kind) in sorted(evs):
        m = None
        for j, lb in enumerate(labels):
            k = str(lb.get('action', '')).upper()
            if j in used or k != kind:
                continue
            if lb['t_start'] - tol <= t <= lb['t_end'] + tol:
                m = j
                break
        if m is None:
            fp += 1
        else:
            used.add(m)
            hit[kind] += 1
    return hit, tot, fp


def perm_p(evs, labels, span, tol=TOL, n=2000, seed=20261018):
    """置换检验：真值窗随机重摆 n 次 → 命中总数的零分布（打散时间信息）。"""
    rnd = random.Random(seed)
    dur = [lb['t_end'] - lb['t_start'] for lb in labels]
    kinds = [str(lb.get('action', '')).upper() for lb in labels]
    h, _, _ = score(evs, labels, tol)
    obs = h['LEFT'] + h['RIGHT']
    null = []
    for _ in range(n):
        sh = []
        for d, k in zip(dur, kinds):
            t0 = rnd.uniform(0.0, max(0.0, span - d))
            sh.append({'action': k, 't_start': t0, 't_end': t0 + d})
        hh, _, _ = score(evs, sh, tol)
        null.append(hh['LEFT'] + hh['RIGHT'])
    null.sort()
    ge = sum(1 for v in null if v >= obs)
    return obs, null[len(null) // 2], null[-1], (ge + 1.0) / (n + 1.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description='crit=drift 产线路径回归（只读）')
    ap.add_argument('--strict', action='store_true', help='启用断言（回归锁定）')
    ap.add_argument('--perm', type=int, default=2000)
    ap.add_argument('--sweep', action='store_true', help='扫 drift_left/right 稳健性')
    ap.add_argument('--matrix', action='store_true', help='带选择 + rest_win 敏感度矩阵')
    ap.add_argument('--auto-band', action='store_true',
                    help='测「自动选带」能否免手工指定 band')
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8')

    print('=== crit=drift 产线路径回归  带 %.2f~%.2f m  cfar=%.1f snr=%.1f tol=%.1f ==='
          % (BAND[0], BAND[1], CFAR, SNR, TOL))
    # 四个组合：判据(delta/drift) × 点选择(质心/最强点)
    VARIANTS = (
        ('delta/质心', dict(crit='delta')),
        ('drift/质心', dict(crit='drift')),
        ('delta/最强点', dict(crit='delta', top_k=1, min_pts=1)),
        ('drift/最强点', dict(crit='drift', top_k=1, min_pts=1)),
    )
    fails = []
    for stem in ('seg_01.25', 'seg_01.50'):
        path = os.path.join(SEG, stem + '.bin')
        labels = read_labels(stem)
        print('\n--- %s  真值 %d 条 ---' % (stem, len(labels)))
        res = {}
        for tag, kw in VARIANTS:
            evs, span, info = run_events(path, **kw)
            hit, tot, fp = score(evs, labels)
            res[tag] = (evs, span, hit, tot, fp)
            print('  %-12s 事件 %2d | 召回 L %d/%d  R %d/%d | 误报 %d (%.1f/min)'
                  % (tag, len(evs), hit['LEFT'], tot['LEFT'], hit['RIGHT'],
                     tot['RIGHT'], fp, fp / (span / 60.0)))
            print('    事件: %s' % ', '.join('%s@%.2f' % (k, t) for (t, k) in evs[:16]))
        # 对「drift/最强点」也做置换检验（它才是 level_recog 的等价物）
        for tag in ('drift/质心', 'drift/最强点'):
            evs, span, hit, tot, fp = res[tag]
            if args.perm:
                obs, med, mx, p = perm_p(evs, labels, span, n=args.perm)
                print('  [置换检验 %s] 观测 %d | 零分布 中位 %d 最大 %d | p=%.4f'
                      % (tag, obs, med, mx, p))
        if args.sweep:
            print('  [稳健性] drift 参数扫描（最强点口径）')
            for L, R in ((1.10, 0.25), (1.30, 0.25), (1.50, 0.25), (1.30, 0.35),
                         (1.30, 0.45), (0.80, 0.25)):
                # 末尾 (0.80, 0.25) = 2026-10-19 重标定后的现行默认值（§7.39.5）
                e2, s2, _ = run_events(path, 'drift', left=L, right=R,
                                       top_k=1, min_pts=1)
                h2, t2, f2 = score(e2, labels)
                print('    L=%.2f R=%.2f → 召回 L %d/%d R %d/%d  误报 %d'
                      % (L, R, h2['LEFT'], t2['LEFT'], h2['RIGHT'], t2['RIGHT'], f2))
        if args.strict:
            # (1) 旧判据必须**仍然**是 0 命中（两种点选择都算）→ 钉住「没悄悄改判据」
            for dtag in ('delta/质心', 'delta/最强点'):
                e_d, _s, h_d, t_d, _f = res[dtag]
                n_d = h_d['LEFT'] + h_d['RIGHT']
                ok_d = (n_d == 0)
                print('  [strict] %-10s 命中 %d（期望 0）→ %s'
                      % (dtag, n_d, '通过' if ok_d else '失败'))
                if not ok_d:
                    fails.append('%s %s' % (stem, dtag))
            # (2) 新配置必须达到锁定值
            key = 'drift/最强点'
            want = EXPECT[stem][key]
            e_w, _s, h_w, t_w, f_w = res[key]
            got = (h_w['LEFT'], h_w['RIGHT'], f_w)
            ok = (h_w['LEFT'] >= want[0] and h_w['RIGHT'] >= want[1] and f_w <= want[2])
            print('  [strict] %-10s 期望 L>=%d R>=%d 误报<=%d，实得 L=%d R=%d 误报=%d → %s'
                  % (key, want[0], want[1], want[2], got[0], got[1], got[2],
                     '通过' if ok else '失败'))
            if not ok:
                fails.append('%s %s' % (stem, key))
            # (3) 生产「自动选带」路径必须复现同一批数字
            #     （否则 band='auto' 只是纸面功能，现场仍然要手工填带）
            e_a, _sa, rec_a = run_events_auto(path)
            h_a, t_a, f_a = score(e_a, labels)
            band_ok = (rec_a.band_source == 'auto' and rec_a.band_gate == 2)
            ok_a = (h_a['LEFT'] >= want[0] and h_a['RIGHT'] >= want[1]
                    and f_a <= want[2] and band_ok)
            print('  [strict] %-10s 期望 L>=%d R>=%d 误报<=%d，实得 L=%d R=%d 误报=%d'
                  ' | 自选门 j=%s 第 %s 帧锁定 → %s'
                  % ('自动带', want[0], want[1], want[2], h_a['LEFT'],
                     h_a['RIGHT'], f_a, rec_a.band_gate,
                     rec_a.band_locked_frame, '通过' if ok_a else '失败'))
            if not ok_a:
                fails.append('%s 自动带(产线)' % stem)

    print('')
    if args.matrix:
        print('=== 敏感度矩阵（drift/最强点）===') 
        bands = (('B3 1.36-2.04', (1.36, 2.04)),
                 ('默认 0.70-1.35', (0.70, 1.35)),
                 ('B4 0.68-2.72', (0.68, 2.72)))
        print('[带选择]')
        for bname, b in bands:
            line = []
            for stem in ('seg_01.25', 'seg_01.50'):
                path = os.path.join(SEG, stem + '.bin')
                labels = read_labels(stem)
                evs, span, _ = run_events(path, 'drift', top_k=1, min_pts=1, band=b)
                hit, tot, fp = score(evs, labels)
                line.append('%s L%d/%d R%d/%d FP%d' % (stem[-4:], hit['LEFT'],
                            tot['LEFT'], hit['RIGHT'], tot['RIGHT'], fp))
            print('  %-14s %s' % (bname, ' | '.join(line)))
        print('[rest_win（基线长度）]  B3，drift/最强点')
        for rw in (20.0, 30.0, 60.0, 120.0, 1e9):
            line = []
            for stem in ('seg_01.25', 'seg_01.50'):
                path = os.path.join(SEG, stem + '.bin')
                labels = read_labels(stem)
                evs, span, _ = run_events(path, 'drift', top_k=1, min_pts=1,
                                          rest_win=rw)
                hit, tot, fp = score(evs, labels)
                line.append('%s L%d/%d R%d/%d FP%d' % (stem[-4:], hit['LEFT'],
                            tot['LEFT'], hit['RIGHT'], tot['RIGHT'], fp))
            tag = 'global(全时长)' if rw > 1e8 else '%.0f s' % rw
            print('  %-14s %s' % (tag, ' | '.join(line)))
        print('')
    if args.auto_band:
        print('=== 自动选带（免手工指定 band）：按「整段检出数最多的距离门」定带 ===')
        from sens_search import dsp_pass
        from radar_dsp import RANGE_RES
        for stem in ('seg_01.25', 'seg_01.50'):
            path = os.path.join(SEG, stem + '.bin')
            labels = read_labels(stem)
            frames, info = dsp_pass(path, CFAR, SNR, True)
            tally = {}
            for (_t, _fr, pts, _n) in frames:
                for pt in pts:
                    j = int(round(float(pt['r']) / RANGE_RES - 0.5))
                    tally[j] = tally.get(j, 0) + 1
            tot = sum(tally.values()) or 1
            desc = ', '.join('j=%d[%.2f~%.2f] %d(%.0f%%)'
                             % (j, j * RANGE_RES, (j + 1) * RANGE_RES,
                                c, 100.0 * c / tot)
                             for j, c in sorted(tally.items()))
            print('  %s 帧 %d | 各门检出: %s' % (stem, info['frames'], desc))
            cand = {j: c for j, c in tally.items() if j >= 1}
            if not cand:
                print('    无 j>=1 检出，跳过')
                continue
            jbest = max(cand, key=lambda j: cand[j])
            auto = (jbest * RANGE_RES, (jbest + 1) * RANGE_RES)
            print('    自动带 = j=%d → (%.2f, %.2f)' % (jbest, auto[0], auto[1]))
            for tag, b in (('自动带', auto), ('B3 1.36-2.04', BAND)):
                evs, span, _ = run_events(path, 'drift', top_k=1, min_pts=1, band=b)
                hit, tots, fp = score(evs, labels)
                print('      %-14s L %d/%d  R %d/%d  误报 %d'
                      % (tag, hit['LEFT'], tots['LEFT'], hit['RIGHT'],
                         tots['RIGHT'], fp))
        print('')
    print('=' * 74)
    if args.strict:
        print('回归结论: %s' % ('全部通过' if not fails else '失败 → %s' % fails))
    else:
        print('回归结论: 报告模式（未断言）')
    print('=' * 74)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
