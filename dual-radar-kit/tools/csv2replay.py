# -*- coding: utf-8 -*-
"""把「动作级 CSV（actions-draft 这类）」转成游戏可直接回放的 RR_RADAR_REPLAY 数据。

为什么需要它：
  游戏的输入协议是【逐帧】的 —— 每个 WebSocket 帧要带 {x, jump, crouch, height, confidence}，
  其中 x 是归一化到 [-1,1] 的左/中/右位置。而 actions-draft CSV 只有【动作级】的
  时间段 + 动作标签，没有任何逐帧值。所以它不能直接喂给游戏，必须先"展开成逐帧"。

两种展开口径：
  --mode truth  (默认) 用动作标签直接当逐帧真值 -> 游戏里 100% 按标注动作跑。
                        它验证的是「关卡 / 通路 / 演示」，不代表雷达识别能力。
  --mode energy        需要真实逐帧 px 序列，动作级 CSV 给不出来，会直接报错退出。

车道模型的重要提醒（决定了为什么要 --with-return）：
  游戏是【三车道 + 迟滞】模型（CFG.RADAR.enterThreshold / exitThreshold），
  不是"每次动作触发一次事件"。如果左移连续做 10 次而中间不回中，
  车道只会从"中"变到"左"一次 => 全程只产生 1 次换道。
  所以要把每个动作段切成「去(--go 秒) + 回中(--ret 秒)」，10 次动作才会产生 10 次换道。

用法:
  python csv2replay.py <输入.csv> <输出.js> [--rate 7.72] [--go 5] [--ret 8]
                       [--series move2_d0p70] [--no-return]
"""
import argparse
import csv
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def load_rows(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def kind_of(row):
    aid = (row.get('action_id') or '').upper()
    if aid.startswith('REST') or aid.startswith('IDLE'):
        return 'rest'
    d = (row.get('direction_label') or '').strip().lower()
    if d.startswith('left'):
        return 'left'
    if d.startswith('right'):
        return 'right'
    if d.startswith('up') or d.startswith('jump'):
        return 'jump'
    if d.startswith('down') or d.startswith('crouch'):
        return 'crouch'
    return 'rest'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--rate', type=float, default=7.72, help='回放帧率（CSV 无帧率信息，默认沿用上批左右的 7.72Hz）')
    ap.add_argument('--go', type=float, default=5.0, help='每个动作段里"做动作"的秒数')
    ap.add_argument('--ret', type=float, default=8.0, help='每个动作段里"回中性位"的秒数')
    ap.add_argument('--series', default='move2_d0p70', help='回放站点名')
    ap.add_argument('--no-return', action='store_true', help='不插入回中段（车道模型下只会各触发 1 次）')
    a = ap.parse_args()

    rows = load_rows(a.src)
    if not rows:
        print('CSV 为空'); return 2

    secs = [(int(r['rel_start_s']), int(r['rel_end_s']), kind_of(r), r['action_id']) for r in rows]
    t0 = min(s for s, _, _, _ in secs)
    t1 = max(e for _, e, _, _ in secs)
    span = t1 - t0
    n = int(round(span * a.rate)) + 1

    x = [0] * n
    jump = [0] * n
    crouch = [0] * n

    def idx(t):
        return max(0, min(n - 1, int(round((t - t0) * a.rate))))

    for rs, re, kind, aid in secs:
        if kind in ('rest',):
            continue
        if kind in ('left', 'right'):
            sign = -1 if kind == 'left' else 1
            if a.no_return:
                segs = [(rs, re, sign)]
            else:
                go_end = min(re, rs + a.go)
                segs = [(rs, go_end, sign), (go_end, re, 0)]
            for s, e, v in segs:
                for i in range(idx(s), idx(e) + 1):
                    x[i] = v
        elif kind == 'jump':
            for i in range(idx(rs), idx(re) + 1):
                jump[i] = 1
        elif kind == 'crouch':
            for i in range(idx(rs), idx(re) + 1):
                crouch[i] = 1

    kinds = {}
    for _, _, k, _ in secs:
        kinds[k] = kinds.get(k, 0) + 1

    meta = {
        'source': os.path.basename(a.src),
        'board': 'unknown',
        'rate': a.rate,
        'framePeriodMs': round(1000.0 / a.rate, 2),
        'totalFrames': n,
        'series': a.series,
        'withReturn': not a.no_return,
        'goSec': a.go, 'retSec': a.ret,
        'actionKinds': kinds,
        'note': '由动作级 CSV 展开的动作真值回放（truth），用于演示/联调；不代表雷达识别能力',
        'warning': '本数据没有任何逐帧雷达测量值；x 全部来自动作标签，jump/crouch 无来源故恒为 0',
    }

    # 换道次数预估（按 CFG.RADAR 的 enter/exit 阈值做三车道迟滞）
    enter, exit_ = 0.36, 0.22

    def lane_events(xs):
        lane, ev, prev = 1, {'left': 0, 'right': 0}, 0
        for v in xs:
            nl = lane
            if lane == 1:
                if v <= -enter: nl = 0
                elif v >= enter: nl = 2
            elif lane == 0:
                if v >= enter: nl = 2
                elif v >= -exit_: nl = 1
            else:
                if v <= -enter: nl = 0
                elif v <= exit_: nl = 1
            while lane != nl:
                if nl > lane:
                    ev['right'] += 1; lane += 1
                else:
                    ev['left'] += 1; lane -= 1
        return ev

    est = lane_events(x)
    est['jump'] = sum(1 for i in range(1, n) if jump[i] and not jump[i - 1])
    est['slide'] = sum(1 for i in range(1, n) if crouch[i] and not crouch[i - 1])
    meta['estimatedEvents'] = est

    doc = {'meta': meta, 'series': {a.series: {
        'frames': n, 'rate': a.rate,
        'energy': [0.0] * n,
        'truth': {'x': x, 'jump': jump, 'crouch': crouch},
    }}}

    os.makedirs(os.path.dirname(os.path.abspath(a.dst)), exist_ok=True)
    with open(a.dst, 'w', encoding='utf-8') as f:
        f.write('// 由 radar-runner/tools/csv2replay.py 生成，勿手改\n')
        f.write('window.RR_RADAR_REPLAY = ')
        json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
        f.write(';\n')

    print('== csv2replay ==')
    print('src      : %s' % a.src)
    print('rows     : %d   kinds=%s' % (len(rows), json.dumps(kinds)))
    print('span     : %s..%s s (%.0f s)' % (t0, t1, span))
    print('rate     : %s Hz   frames=%d' % (a.rate, n))
    print('withReturn: %s   go=%ss ret=%ss' % (not a.no_return, a.go, a.ret))
    print('estEvents: %s' % json.dumps(est))
    print('dst      : %s' % a.dst)
    return 0


if __name__ == '__main__':
    sys.exit(main())
