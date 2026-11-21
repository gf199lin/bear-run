# -*- coding: utf-8 -*-
"""对准检查：终端引导人做动作，后台逐帧读雷达并保存。

目的不是打分，而是回答三个问题：
  1. 人静止时雷达能看到几个点、在什么角度（还是只看到电脑？）
  2. 往左走一步，角度变大还是变小
  3. 往右走一步，角度变大还是变小

用法：
  py -3 tools/aim_check.py --port COM5
  py -3 tools/aim_check.py --port COM5 --rounds 4

采完会在 data/aimcheck/<时间戳>/ 下留 frames.npz 和 report.md。
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


def bar(frac, width=28):
    n = int(max(0.0, min(1.0, frac)) * width)
    return '[' + '#' * n + '-' * (width - n) + ']'


def build_segments(rounds):
    """rest 先打底，然后 左/回/右/回 循环 rounds 轮。"""
    segs = [('rest', 8.0, '站到操作位，正对雷达，双手自然下垂，完全不要动')]
    for r in range(rounds):
        segs.append(('left', 1.5, '往你的【左手边】横移一大步，然后停住不动'))
        segs.append(('back', 2.0, '回到正中间，站住别动'))
        segs.append(('right', 1.5, '往你的【右手边】横移一大步，然后停住不动'))
        segs.append(('back', 2.0, '回到正中间，站住别动'))
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM5', help='左右板串口，默认 COM5')
    ap.add_argument('--rounds', type=int, default=4, help='左/右各做几次，默认 4')
    ap.add_argument('--out', default=None, help='输出目录，默认 data/aimcheck/<时间戳>')
    ap.add_argument('--nearest', type=float, default=0.5, help='跟踪容差：只在最近点附近选，米')
    args = ap.parse_args()

    from radar_source import SerialSource, build_pipeline
    from radar_dsp import PointCloudProcessor

    out = args.out or os.path.join(
        ROOT, 'data', 'aimcheck', time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(out, exist_ok=True)

    proc = PointCloudProcessor(cfar_scale=2.6, min_snr=7.0,
                               remove_clutter=True, clutter_keep=1.0)

    stop = threading.Event()
    lock = threading.Lock()
    buf = []          # [(t, z0, z1)]
    latest = []       # 最近一帧的处理结果，供实时显示

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
        boot = len(buf)
    if boot == 0:
        print('\n  [X] %s 没有收到任何帧。' % args.port)
        print('    检查：1) 串口是否被推流占着  2) 板子是否插在这台电脑')
        diag = src.diag() if hasattr(src, 'diag') else {}
        print('    诊断：', diag.get('port_reason') or diag)
        stop.set()
        return 2

    segs = build_segments(args.rounds)
    records = []     # [(label, [每帧统计字典])]

    for idx, (label, dur, hint) in enumerate(segs, 1):
        with lock:
            buf.clear()
        t_end = time.time() + dur
        while True:
            now = time.time()
            left = t_end - now
            if left <= 0:
                break
            with lock:
                n = len(buf)
                snap = buf[-1] if buf else None
            stat_txt = ''
            if snap is not None:
                pts, _ = proc.process(snap[1], snap[2])
                cand = [p for p in pts if 0.5 <= p['r'] <= 2.5]
                if cand:
                    best = max(cand, key=lambda p: p['p'])
                    stat_txt = '点数 %d   最强 %+.0f°  r=%.2f  e=%.0f' % (
                        len(cand), best['deg'], best['r'], best['p'])
                else:
                    stat_txt = '点数 0   （这一帧没有目标）'
            clear()
            print()
            print('  ' + '=' * 62)
            print('    第 %d / %d 段   %s' % (idx, len(segs), label))
            print('  ' + '=' * 62)
            print()
            print('    >>  %s' % hint)
            print()
            print('    %s  剩余 %.1f 秒' % (bar(1.0 - left / dur), left))
            print()
            print('    已收 %d 帧    %s' % (n, stat_txt))
            print()
            sys.stdout.flush()
            time.sleep(0.25)

        with lock:
            got = list(buf)
        frames = []
        for t, z0, z1 in got:
            pts, _ = proc.process(z0, z1)
            cand = [p for p in pts if 0.5 <= p['r'] <= 2.5]
            if cand:
                best = max(cand, key=lambda p: p['p'])
                b_deg, b_r, b_e = best['deg'], best['r'], best['p']
            else:
                b_deg, b_r, b_e = float('nan'), float('nan'), 0.0
            frames.append({
                't': t,
                'n': len(cand),
                'best_deg': b_deg,
                'best_r': b_r,
                'best_e': b_e,
                'degs': [p['deg'] for p in cand],
                'rs': [p['r'] for p in cand],
                'es': [p['p'] for p in cand],
            })
        records.append((label, got, frames))

    stop.set()
    time.sleep(0.4)

    # ---------- 存盘 ----------
    z0s = np.concatenate([np.stack([g[1] for g in got]) for _, got, _ in records if got])
    z1s = np.concatenate([np.stack([g[2] for g in got]) for _, got, _ in records if got])
    ts = np.concatenate([np.array([g[0] for g in got]) for _, got, _ in records if got])
    labels = np.concatenate([np.array([lab] * len(got)) for lab, got, _ in records if got])
    np.savez_compressed(os.path.join(out, 'frames.npz'),
                        z0=z0s, z1=z1s, t=ts, label=labels)
    with open(os.path.join(out, 'port.txt'), 'w', encoding='utf-8') as f:
        f.write('%s\n' % args.port)

    # ---------- 分析 ----------
    def seg_stats(want):
        rows = []
        for lab, got, fr in records:
            if lab == want:
                rows.extend(fr)
        if not rows:
            return None
        n = np.array([r['n'] for r in rows], float)
        deg = np.array([r['best_deg'] for r in rows], float)
        e = np.array([r['best_e'] for r in rows], float)
        ok = ~np.isnan(deg)
        return {
            'frames': len(rows),
            'n_med': float(np.median(n)),
            'n_zero': int((n == 0).sum()),
            'deg_med': float(np.median(deg[ok])) if ok.any() else float('nan'),
            'deg_p5': float(np.percentile(deg[ok], 5)) if ok.any() else float('nan'),
            'deg_p95': float(np.percentile(deg[ok], 95)) if ok.any() else float('nan'),
            'e_med': float(np.median(e)),
        }

    rest = seg_stats('rest')
    left = seg_stats('left')
    right = seg_stats('right')
    back = seg_stats('back')

    L = []
    L.append('# 对准检查报告\n')
    L.append('生成：%s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
    L.append('端口：%s   轮数：%d   总帧：%d\n' % (args.port, args.rounds, len(z0s)))
    L.append('\n## 分段\n')
    L.append('```')
    L.append('%-8s %6s %8s %8s %10s %8s' % ('段', '帧数', '点数中位', '无点帧', '角度中位', '能量中位'))
    for name, st in (('rest 静止', rest), ('left 左移', left), ('back 回中', back), ('right 右移', right)):
        if st is None:
            continue
        L.append('%-8s %6d %8.1f %8d %10.1f %8.0f' % (
            name, st['frames'], st['n_med'], st['n_zero'], st['deg_med'], st['e_med']))
    L.append('```\n')

    L.append('\n## 结论\n')
    if rest is None or rest['deg_med'] != rest['deg_med']:
        L.append('- 静息段没有可用角度（全丢帧）。\n')
    else:
        L.append('- 静止时点数中位 %.1f，无点帧 %d/%d，说明%s。\n' % (
            rest['n_med'], rest['n_zero'], rest['frames'],
            '人基本看不见（被别的反射体压住）' if rest['n_med'] <= 1.2 else '人可见'))
    if left and right and L and rest:
        dl = left['deg_med'] - rest['deg_med']
        dr = right['deg_med'] - rest['deg_med']
        L.append('- 左移相对静止：%+.1f°\n' % dl)
        L.append('- 右移相对静止：%+.1f°\n' % dr)
        if abs(dl) < 3 and abs(dr) < 3:
            L.append('- **两个方向都没有明显角度变化** → 左右信号看不到。\n')
        elif dl * dr < 0:
            L.append('- 方向相反 [OK]（左移和右移落在静止两侧）\n')
        else:
            L.append('- **两个方向同号** → 左右方向分不开，或其中一侧出了视场。\n')
        if abs(dl) > 3 and abs(dr) > 3:
            ratio = abs(dl) / max(abs(dr), 0.1)
            if ratio > 2.5 or ratio < 0.4:
                L.append('- 两侧幅度不对称（左 %.1f° vs 右 %.1f°，差 %.1f 倍）→ 一侧接近视场边缘。\n'
                         % (dl, dr, ratio))
    L.append('\n原始数据：`frames.npz`（z0/z1/t/label）\n')

    with open(os.path.join(out, 'report.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))

    print('\n' * 2)
    print('  ' + '=' * 62)
    print('    采集完成')
    print('  ' + '=' * 62)
    print('    目录：%s' % out)
    print()
    print('\n'.join('    ' + x for x in L))
    print()
    print('  把上面这份结论发我就行。')
    print()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n  已中断。')
        sys.exit(1)
