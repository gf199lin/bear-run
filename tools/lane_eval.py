# -*- coding: utf-8 -*-
r"""左右移动识别：现有规则判据的准确率 / 延迟 / 误报基线。

为什么先做这个：docs/handoff-lr2-1022.md §2.4 明确写了「延迟还没量」。
没有延迟数字，就无法判断机器学习到底有没有变快，只能比准确率。

口径
----
- 事件：产线路径 `run_events_auto(crit='xbase')` 输出的 LANE_LEFT / LANE_RIGHT。
- 动作段：labels.jsonl 的 `frame_start` / `frame_end`（cue 驱动，真值）。
- 命中窗口：[t_start - PRE, t_end + POST]，`PRE=POST=0.5 s`（与 score.TOL 一致）。
- 延迟：段内第一个同向事件的 `t_event - t_start`（秒）。负数 = 提前触发。
- 误报：不落在任何动作段命中窗口内的事件。
- 同一段内重复触发只记一次命中，但重复次数单独统计（抖动）。

只读。板型钉死 1t2r（裸帧会被嗅探误判成 2t4r）。
"""
import os
import sys
import json
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, os.path.join(ROOT, '.deepworks', 'tmp'))

import radar_source as rs  # noqa: E402

rs.sniff_board_from_file = lambda path, scan=262144: '1t2r'

import label_tool as lt  # noqa: E402
from regress_drift import run_events_auto  # noqa: E402

FRAME_PERIOD = 0.1295
PRE = POST = 0.5
SETS = [
    ('seg-lr2/1', '对准后 · 人1'),
    ('seg-lr2/jqz', '未对准 · 人2'),
    ('oncehot-oncehot/lr', '早期 · oncehot'),
]


def find_pairs():
    out = []
    for rel, tag in SETS:
        base = os.path.join(ROOT, '.deepworks', 'tmp', rel)
        for p in sorted(glob.glob(os.path.join(base, '**', '*.bin'), recursive=True)):
            lab = os.path.splitext(p)[0] + '.labels.jsonl'
            if os.path.exists(lab):
                out.append((tag, p, lab))
    return out


def evaluate(path, labels, evs):
    """返回 (hit, total, latencies, fps_inside, dup)。"""
    acts = [l for l in labels if l.get('action') in ('LEFT', 'RIGHT')]
    used = set()
    hit = {'LEFT': 0, 'RIGHT': 0}
    tot = {'LEFT': 0, 'RIGHT': 0}
    lat = []
    dup = 0
    for a in acts:
        side = a['action']
        tot[side] += 1
        t0, t1 = a['t_start'] - PRE, a['t_end'] + POST
        inner = [(i, t, s) for i, (t, s) in enumerate(evs)
                 if s == side and t0 <= t <= t1]
        if inner:
            hit[side] += 1
            lat.append(inner[0][1] - a['t_start'])
            used.update(i for i, _, _ in inner)
            dup += max(0, len(inner) - 1)
    fp = sum(1 for i, _ in enumerate(evs) if i not in used)
    return hit, tot, lat, fp, dup


def main():
    rows = []
    agg = dict(hitL=0, totL=0, hitR=0, totR=0, fp=0, dup=0, lat=[])
    for tag, path, labp in find_pairs():
        stem = os.path.splitext(os.path.basename(path))[0]
        d = os.path.dirname(path)
        labels = lt.load_labels(labp)
        try:
            evs, span, rec = run_events_auto(path, crit='xbase',
                                             top_k=1, min_pts=1, board='1t2r')
        except Exception as e:  # noqa: BLE001
            print('!! %s: %s' % (stem, e))
            continue
        hit, tot, lat, fp, dup = evaluate(path, labels, evs)
        row = dict(set=tag, file=os.path.relpath(path, ROOT),
                   hitL=hit['LEFT'], totL=tot['LEFT'],
                   hitR=hit['RIGHT'], totR=tot['RIGHT'],
                   fp=fp, dup=dup,
                   lat=[round(x, 3) for x in lat],
                   lat_med=round(sorted(lat)[len(lat) // 2], 3) if lat else None,
                   minutes=round(span / 60.0, 2))
        rows.append(row)
        agg['hitL'] += hit['LEFT']; agg['totL'] += tot['LEFT']
        agg['hitR'] += hit['RIGHT']; agg['totR'] += tot['RIGHT']
        agg['fp'] += fp; agg['dup'] += dup
        agg['lat'] += lat
        print('%-22s %-10s  L %2d/%2d  R %2d/%2d  误报 %2d  重复 %2d  '
              '延迟中位 %s s'
              % (tag, stem, hit['LEFT'], tot['LEFT'], hit['RIGHT'],
                 tot['RIGHT'], fp, dup, row['lat_med']))

    lat = sorted(agg['lat'])
    print()
    print('=' * 74)
    print('合计 %d 段动作' % (agg['totL'] + agg['totR']))
    print('  LEFT  %d/%d = %.1f%%' % (agg['hitL'], agg['totL'],
                                       100.0 * agg['hitL'] / max(1, agg['totL'])))
    print('  RIGHT %d/%d = %.1f%%' % (agg['hitR'], agg['totR'],
                                       100.0 * agg['hitR'] / max(1, agg['totR'])))
    print('  方向正确合计 %d/%d = %.1f%%'
          % (agg['hitL'] + agg['hitR'], agg['totL'] + agg['totR'],
             100.0 * (agg['hitL'] + agg['hitR']) / max(1, agg['totL'] + agg['totR'])))
    if lat:
        p50 = lat[len(lat) // 2]
        p90 = lat[int(len(lat) * 0.9)]
        print('  延迟 中位 %.2f s  P90 %.2f s  最小 %.2f  最大 %.2f'
              % (p50, p90, lat[0], lat[-1]))
    print('  误报 %d 次   重复触发 %d 次' % (agg['fp'], agg['dup']))
    print('=' * 74)

    out = os.path.join(ROOT, 'outputs', 'lane-eval')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'baseline-xbase.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(rows=rows, agg=agg, latencies=agg['lat']), f,
                  ensure_ascii=False, indent=1)
    print('已写 outputs/lane-eval/baseline-xbase.json')


if __name__ == '__main__':
    main()
