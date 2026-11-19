# -*- coding: utf-8 -*-
"""左右换道采数引导：终端分段提示动作，后台逐帧记录并保存。

2026-11-19 调参依据（空场扫描 + 上一轮 aim_check）：
  - 空场 0.38 点/帧，无固定干扰；人在场 3.0 点/帧，信噪比 8 倍
  - 一大步（约 35 度）会走出视场或撞上边缘，改半步（约 15 度）
  - 开场静息容易锁在杂波上，所以零点改用「回中」段，不再用开场 rest

用法：
  py -3 tools/lane_guide.py --port COM5
  py -3 tools/lane_guide.py --port COM5 --rounds 5

产物：data/laneguide/<时间戳>/{frames.npz, report.md}
"""
import argparse
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, os.path.join(ROOT, 'bear-run-new', 'radar-service'))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='gbk', errors='replace')

import numpy as np  # noqa: E402

CLEAR = '\033[2J\033[H'


def clear():
    sys.stdout.write(CLEAR)
    sys.stdout.flush()


def bar(frac, width=30):
    n = int(max(0.0, min(1.0, frac)) * width)
    return '[' + '#' * n + '-' * (width - n) + ']'


def build_segments(rounds):
    segs = [('rest', 5.0, '站到操作位，正对雷达，双手下垂，完全不要动')]
    for r in range(rounds):
        segs.append(('left', 1.3, '往【左】迈半步（约一个脚的距离），立刻停住'))
        segs.append(('back', 1.8, '回到正中间，站住别动'))
        segs.append(('right', 1.3, '往【右】迈半步（约一个脚的距离），立刻停住'))
        segs.append(('back', 1.8, '回到正中间，站住别动'))
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM5', help='左右板串口')
    ap.add_argument('--rounds', type=int, default=4)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    from radar_source import SerialSource, build_pipeline
    from radar_dsp import PointCloudProcessor

    out = args.out or os.path.join(ROOT, 'data', 'laneguide', time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(out, exist_ok=True)

    proc = PointCloudProcessor(cfar_scale=2.6, min_snr=7.0,
                               remove_clutter=True, clutter_keep=1.0)
    stop = threading.Event()
    lock = threading.Lock()
    buf = []

    def on_frame(z0, z1, stats):
        with lock:
            buf.append((time.time(), z0.copy(), z1.copy()))

    src = SerialSource(port=args.port)
    th = threading.Thread(target=build_pipeline, args=(src, on_frame, stop),
                          kwargs={'verbose': False}, daemon=True)

    clear()
    print('\n  正在打开 %s ...' % args.port)
    th.start()
    time.sleep(2.5)
    with lock:
        if not buf:
            print('\n  [X] %s 没有收到帧。检查串口是否被推流占用。' % args.port)
            stop.set()
            return 2

    segs = build_segments(args.rounds)
    records = []

    for idx, (label, dur, hint) in enumerate(segs, 1):
        with lock:
            buf.clear()
        t_end = time.time() + dur
        while True:
            left = t_end - time.time()
            if left <= 0:
                break
            with lock:
                n = len(buf)
                snap = buf[-1] if buf else None
            info = ''
            if snap is not None:
                pts, _ = proc.process(snap[1], snap[2])
                cand = [p for p in pts if 0.5 <= p['r'] <= 2.5]
                if cand:
                    best = max(cand, key=lambda p: p['p'])
                    info = '点数 %d   现看到 %+.0f deg  r=%.2f' % (len(cand), best['deg'], best['r'])
                else:
                    info = '点数 0'
            clear()
            print()
            print('  ' + '=' * 64)
            print('    第 %d / %d 段' % (idx, len(segs)))
            print('  ' + '=' * 64)
            print()
            print('    >>  %s' % hint)
            print()
            print('    %s  剩 %.1f 秒' % (bar(1.0 - left / dur), left))
            print()
            print('    %s' % info)
            print()
            print('  ' + '-' * 64)
            print('    一共 %d 轮：左边半步 - 回中 - 右边半步 - 回中' % args.rounds)
            sys.stdout.flush()
            time.sleep(0.22)

        with lock:
            got = list(buf)
        frames = []
        for t, z0, z1 in got:
            pts, _ = proc.process(z0, z1)
            cand = [p for p in pts if 0.5 <= p['r'] <= 2.5]
            if cand:
                best = max(cand, key=lambda p: p['p'])
                b = (best['deg'], best['r'], best['p'])
            else:
                b = (float('nan'), float('nan'), 0.0)
            frames.append({'t': t, 'n': len(cand), 'deg': b[0], 'r': b[1], 'e': b[2]})
        records.append((label, got, frames))

    stop.set()
    time.sleep(0.4)

    # ---------- 存盘 ----------
    keep = [(lab, got) for lab, got, _ in records if got]
    if not keep:
        print('\n  [X] 没有采到任何数据。')
        return 3
    z0s = np.concatenate([np.stack([g[1] for g in got]) for _, got in keep])
    z1s = np.concatenate([np.stack([g[2] for g in got]) for _, got in keep])
    ts = np.concatenate([np.array([g[0] for g in got]) for _, got in keep])
    labels = np.concatenate([np.array([lab] * len(got)) for lab, got in keep])
    np.savez_compressed(os.path.join(out, 'frames.npz'),
                        z0=z0s, z1=z1s, t=ts, label=labels)

    # ---------- 统计 ----------
    def gather(want):
        rows = []
        for lab, got, fr in records:
            if lab == want:
                rows.extend(fr)
        return rows

    def summarize(rows):
        if not rows:
            return None
        degs = np.array([r['deg'] for r in rows], float)
        ns = np.array([r['n'] for r in rows], float)
        ok = ~np.isnan(degs)
        return {
            'n': len(rows),
            'deg': float(np.median(degs[ok])) if ok.any() else float('nan'),
            'deg_p10': float(np.percentile(degs[ok], 10)) if ok.any() else float('nan'),
            'deg_p90': float(np.percentile(degs[ok], 90)) if ok.any() else float('nan'),
            'pts': float(np.median(ns)),
            'zero': int((ns == 0).sum()),
            'valid': int(ok.sum()),
        }

    rest = summarize(gather('rest'))
    backs = summarize(gather('back'))
    left = summarize(gather('left'))
    right = summarize(gather('right'))

    # 零点优先用回中段
    zero_src = 'back' if (backs and backs['valid'] >= 10) else 'rest'
    zero = (backs if zero_src == 'back' else rest)

    L = []
    L.append('# 左右采数报告\n')
    L.append('生成：%s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
    L.append('端口：%s   轮数：%d   总帧：%d\n' % (args.port, args.rounds, len(z0s)))
    L.append('\n## 分段\n')
    L.append('```')
    L.append('%-10s %5s %7s %7s %10s %8s' % ('段', '帧数', '点数', '无点帧', '角度中位', '有效帧'))
    for name, st in (('rest 开场', rest), ('back 回中', backs), ('left 左移', left), ('right 右移', right)):
        if st is None:
            continue
        L.append('%-10s %5d %7.1f %7d %10.1f %8d' % (
            name, st['n'], st['pts'], st['zero'], st['deg'], st['valid']))
    L.append('```\n')

    L.append('\n## 结论\n')
    if zero is None or zero['valid'] == 0:
        L.append('- 回中段没有可用角度，无法定零点。\n')
    else:
        L.append('- 零点取「%s」段：%+.1f deg（点数中位 %.1f，有效 %d/%d 帧）\n' % (
            '回中' if zero_src == 'back' else '开场', zero['deg'], zero['pts'],
            zero['valid'], zero['n']))
        if left and left['valid']:
            dl = left['deg'] - zero['deg']
            L.append('- 左移相对零点：%+.1f deg（游戏 x = %+.2f，门限 0.22）\n' % (dl, dl / 20.0))
        else:
            L.append('- 左移段没有可用角度。\n')
        if right and right['valid']:
            dr = right['deg'] - zero['deg']
            L.append('- 右移相对零点：%+.1f deg（游戏 x = %+.2f）\n' % (dr, dr / 20.0))
        else:
            L.append('- 右移段没有可用角度。\n')
        if left and right and left['valid'] and right['valid']:
            dl = left['deg'] - zero['deg']
            dr = right['deg'] - zero['deg']
            if abs(dl) < 2.5 and abs(dr) < 2.5:
                L.append('\n**两个方向都没到 2.5 deg，左右信号基本看不到。**\n')
            elif dl * dr < 0:
                L.append('\n方向相反 [OK]，左右可分。\n')
                if abs(dl) >= 4.4 and abs(dr) >= 4.4:
                    L.append('两侧都超过游戏门限 4.4 deg（0.22 x 20）。\n')
                else:
                    L.append('有一侧不足门限 4.4 deg，注意幅度。\n')
                r_ratio = abs(dl) / max(abs(dr), 0.1)
                if r_ratio > 2.0 or r_ratio < 0.5:
                    L.append('幅度不对称：左 %.1f / 右 %.1f（差 %.1f 倍）。\n' % (dl, dr, r_ratio))
            else:
                L.append('\n**两个方向同号，左右分不开。**\n')

    with open(os.path.join(out, 'report.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))

    print('\n' * 2)
    print('  ' + '=' * 64)
    print('    采集完成')
    print('  ' + '=' * 64)
    print('    目录：%s' % out)
    print()
    print('\n'.join('    ' + x for x in L))
    print()
    print('  这份结论发我就行。')
    print()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n  已中断。')
        sys.exit(1)
