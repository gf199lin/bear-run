# -*- coding: utf-8 -*-
"""
把 lr-move 数据集转成游戏能直接吃的 window.RR_RADAR_REPLAY。

产出两组并列通道，都用游戏的 truth 驱动口（series.truth.x/jump/crouch）：
  gt_d00p70 / gt_d00p85 / gt_d01p00   —— 用人工 cue 标注（LEFT/RIGHT/IDLE）当 x，上限对照
  px_d00p70 / px_d00p85 / px_d01p00   —— 用雷达实测横向质心 px_m 减该站位静息基线当 x，反例

x 归一化到游戏阈值域：enterThreshold=0.36 / exitThreshold=0.22（config.RADAR）
px 通道：x = clamp((px_m - base) / SCALE, -1, 1)，SCALE=0.8 m ≈ 一次完整换道的横向位移
无检出帧（n_pts 为空）保持上一帧 x，等价于"雷达这帧没给帧"，而不是"回到中道"。
"""

import csv
import io
import json
import os
import statistics
import sys

BASE = r'<PROJECT_ROOT>'
SRC = os.path.join(BASE, 'uploads', 'ses_ea91660b3ffell1v8B4tGE7NhJ', 'lr-move-frames.csv')
OUT = os.path.join(BASE, 'radar-runner', '_radarreplay', 'radar-lr-move.js')

FPS = 7.72
PERIOD_MS = 129.5
SCALE_M = 0.8          # px 归一化尺度：0.8 m 横向位移 = 满偏
ENTER_T = 0.36
EXIT_T = 0.22


def fnum(s):
    s = (s or '').strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def main():
    # 上传的 CSV 带 BOM，用 utf-8-sig 否则首列名会变成 '\ufeffsegment'
    with io.open(SRC, 'r', encoding='utf-8-sig', newline='') as fh:
        rows = list(csv.DictReader(fh))
    if rows and 'segment' not in rows[0]:
        raise SystemExit('列名异常：%r' % (list(rows[0].keys())[:3],))

    segs = {}
    for r in rows:
        segs.setdefault(r['segment'], []).append(r)

    series = {}
    report = []

    for seg in sorted(segs):
        rs = sorted(segs[seg], key=lambda r: int(r['frame']))
        f0 = int(rs[0]['frame'])
        # 帧号必须能直接当数组下标用（回放器用帧号索引 energy/truth）
        ids = [int(r['frame']) for r in rs]
        dense = sorted(set(ids))
        span = dense[-1] - dense[0] + 1
        if span != len(dense) or f0 != 1:
            print('  ! %s 帧号不连续或非 1 起：first=%d last=%d n=%d' % (seg, f0, dense[-1], len(dense)))
        n = dense[-1]   # 下标 = 帧号；0 号位补一个空帧
        byframe = {int(r['frame']): r for r in rs}

        energy = [0.0] * (n + 1)
        px = [None] * (n + 1)
        dirn = ['IDLE'] * (n + 1)
        npts = [0] * (n + 1)
        for i in range(1, n + 1):
            r = byframe.get(i)
            if r is None:
                continue
            e = fnum(r['energy'])
            energy[i] = e if e is not None else 0.0
            p = fnum(r['px_m'])
            px[i] = p
            np_ = fnum(r['n_pts'])
            npts[i] = int(np_) if np_ is not None else 0
            d = (r['direction'] or '').strip().upper()
            dirn[i] = d if d in ('LEFT', 'RIGHT') else 'IDLE'

        # 该站位静息基线：IDLE 且有检出的帧的 px 中位数
        idle = [px[i] for i in range(1, n + 1) if dirn[i] == 'IDLE' and px[i] is not None]
        base = statistics.median(idle) if idle else 0.0

        # ---- 通道 1：人工 cue 标注 ----
        gtx = [0.0] * (n + 1)
        for i in range(1, n + 1):
            gtx[i] = -1.0 if dirn[i] == 'LEFT' else (1.0 if dirn[i] == 'RIGHT' else 0.0)

        # ---- 通道 2：雷达实测 px ----
        pxx = [0.0] * (n + 1)
        last = 0.0
        for i in range(1, n + 1):
            if px[i] is None:
                pxx[i] = last            # 无检出 = 这一帧雷达没给数据，保持
            else:
                v = (px[i] - base) / SCALE_M
                v = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
                v = round(v, 4)
                pxx[i] = v
                last = v

        zeros = [0.0] * (n + 1)
        tag = seg.replace('.', 'p')
        for name, x in (('gt_' + tag, gtx), ('px_' + tag, pxx)):
            series[name] = {
                'frames': n + 1,
                'rate': FPS,
                'energy': [round(energy[i], 1) for i in range(n + 1)],
                'truth': {'x': x, 'jump': zeros, 'crouch': zeros},
                'basePx': round(base, 4),
                'scaleM': SCALE_M,
                'detectRate': round(sum(1 for i in range(1, n + 1) if npts[i] > 0) / float(n), 4),
            }

        # 统计：两条通道各会触发多少次换道（按游戏阈值 + 迟滞复算）
        def lane_changes(xs):
            lane, cnt = 1, 0
            for i in range(1, n + 1):
                x = xs[i]
                nl = lane
                if lane == 1:
                    if x <= -ENTER_T:
                        nl = 0
                    elif x >= ENTER_T:
                        nl = 2
                elif lane == 0:
                    if x >= ENTER_T:
                        nl = 2
                    elif x >= -EXIT_T:
                        nl = 1
                else:
                    if x <= -ENTER_T:
                        nl = 0
                    elif x <= EXIT_T:
                        nl = 1
                if nl != lane:
                    cnt += abs(nl - lane)
                    lane = nl
            return cnt

        nleft = sum(1 for i in range(1, n + 1) if dirn[i] == 'LEFT' and (i == 1 or dirn[i - 1] != 'LEFT'))
        nright = sum(1 for i in range(1, n + 1) if dirn[i] == 'RIGHT' and (i == 1 or dirn[i - 1] != 'RIGHT'))
        report.append({
            'segment': seg, 'frames': n + 1, 'basePx': round(base, 4),
            'idleN': len(idle), 'detect': series['gt_' + tag]['detectRate'],
            'cueLeft': nleft, 'cueRight': nright,
            'gtLaneChanges': lane_changes(gtx), 'pxLaneChanges': lane_changes(pxx),
        })

    doc = {
        'meta': {
            'source': 'uploads/ses_ea91660b3ffell1v8B4tGE7NhJ/lr-move-frames.csv',
            'board': '1t2r', 'distance_m': [0.70, 0.85, 1.00],
            'rate': FPS, 'framePeriodMs': PERIOD_MS, 'totalFrames': len(rows),
            'channels': {
                'gt_*': '人工 cue 标注 LEFT/RIGHT/IDLE 当真值驱动（上限对照，不是识别结果）',
                'px_*': '雷达实测 px_m 减站位静息基线后归一化驱动（反例：符号不可用）',
            },
            'scaleM': SCALE_M, 'enterThreshold': ENTER_T, 'exitThreshold': EXIT_T,
            'noJumpData': '本数据集没有跳/蹲动作，jump/crouch 恒为 0',
            'note': '生成脚本 .deepworks/tmp/gen_lr_replay.py',
        },
        'series': series,
    }

    d = os.path.dirname(OUT)
    if not os.path.isdir(d):
        os.makedirs(d)
    with io.open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('// 由 .deepworks/tmp/gen_lr_replay.py 生成，勿手改\n')
        fh.write('window.RR_RADAR_REPLAY = ')
        fh.write(json.dumps(doc, ensure_ascii=False, separators=(',', ':')))
        fh.write(';\n')

    print('写出：%s  (%.0f KB)' % (OUT, os.path.getsize(OUT) / 1024.0))
    print()
    print('%-10s %8s %8s %8s %8s %8s %10s %12s' % (
        'segment', 'frames', 'basePx', 'idleN', 'detect', 'cueL/R', 'gt换道', 'px换道'))
    for r in report:
        print('%-10s %8d %8.3f %8d %7.1f%% %5d/%-2d %10d %12d' % (
            r['segment'], r['frames'], r['basePx'], r['idleN'],
            r['detect'] * 100, r['cueLeft'], r['cueRight'],
            r['gtLaneChanges'], r['pxLaneChanges']))
    print()
    print('gt换道 = 人工标注真值驱动的换道次数（应≈ cue 动作数）')
    print('px换道 = 雷达实测 px 驱动的换道次数（反例，越多越说明符号/噪声不可用）')


if __name__ == '__main__':
    sys.exit(main())
