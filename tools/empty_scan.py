# -*- coding: utf-8 -*-
"""空场扫描：不引导动作，持续记录雷达看到的所有点，用来定位干扰源。

用法：
  py -3 tools/empty_scan.py --port COM5 --sec 60

每 5 秒打印一次"最近窗口"的概况，结束时给完整直方图。
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM5')
    ap.add_argument('--sec', type=float, default=60.0)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    from radar_source import SerialSource, build_pipeline
    from radar_dsp import PointCloudProcessor

    out = args.out or os.path.join(ROOT, 'data', 'aimcheck', 'empty-' + time.strftime('%Y%m%d-%H%M%S'))
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
    th.start()
    time.sleep(2.5)
    with lock:
        if not buf:
            print('[X] %s 没有收到帧' % args.port)
            stop.set()
            return 2

    all_points = []      # [(t, deg, r, p)]
    all_frames = []      # 每帧点数
    t0 = time.time()
    last_report = t0
    win_points = []

    print('\n  空场扫描 %s，共 %.0f 秒。现在把雷达前方的东西移开。\n' % (args.port, args.sec))

    while time.time() - t0 < args.sec:
        with lock:
            got = list(buf)
            buf.clear()
        for t, z0, z1 in got:
            pts, _ = proc.process(z0, z1)
            cand = [p for p in pts if 0.4 <= p['r'] <= 3.0]
            all_frames.append(len(cand))
            for p in cand:
                rec = (t, p['deg'], p['r'], p['p'])
                all_points.append(rec)
                win_points.append(rec)

        now = time.time()
        if now - last_report >= 5.0:
            n = len(all_frames)
            med = np.median(all_frames[-40:]) if all_frames else 0
            line = '  %5.1fs  点数中位 %.1f  累计 %d 点' % (now - t0, med, len(all_points))
            if win_points:
                degs = [w[1] for w in win_points]
                hist, edges = np.histogram(degs, bins=range(-50, 55, 10))
                top = int(np.argmax(hist))
                line += '   本窗口最密 %d~%d° (%d 点)' % (edges[top], edges[top + 1], hist[top])
            print(line, flush=True)
            win_points = []
            last_report = now
        time.sleep(0.15)

    stop.set()

    # ---------- 汇总 ----------
    print('\n  ' + '=' * 66)
    print('  扫描结束')
    print('  ' + '=' * 66)

    degs = np.array([p[1] for p in all_points], float)
    rs = np.array([p[2] for p in all_points], float)
    ps = np.array([p[3] for p in all_points], float)

    print('\n  总帧 %d，有点的帧 %d，点总数 %d' % (
        len(all_frames), sum(1 for x in all_frames if x > 0), len(all_points)))
    if len(all_points):
        print('\n  角度直方图（5° 一桶，按点数）')
        hist, edges = np.histogram(degs, bins=range(-50, 55, 5))
        mx = max(hist) if len(hist) else 1
        for h, e in zip(hist, edges):
            if h == 0:
                continue
            bar = '#' * int(round(50.0 * h / mx))
            print('    %+4d ~ %+4d  %5d  %s' % (e, e + 5, h, bar))

        print('\n  距离分布（0.25 m 一桶，按点数）')
        hist2, edges2 = np.histogram(rs, bins=np.arange(0.0, 3.25, 0.25))
        mx2 = max(hist2) if len(hist2) else 1
        for h, e in zip(hist2, edges2):
            if h == 0:
                continue
            bar = '#' * int(round(40.0 * h / mx2))
            print('    %.2f ~ %.2f m  %5d  %s' % (e, e + 0.25, h, bar))

        print('\n  每个 5° 桶的稳定度（出现在多少比例的帧里）')
        nf = max(1, len(all_frames))
        for lo in range(-50, 50, 10):
            sel = (degs >= lo) & (degs < lo + 10)
            cnt = int(sel.sum())
            if cnt == 0:
                continue
            # 该桶在多少帧里至少出现一次
            frames_with = 0
            per_frame = {}
            for t, dg, r, p in all_points:
                if lo <= dg < lo + 10:
                    per_frame[t] = 1
            frames_with = len(per_frame)
            print('    %+4d ~ %+4d  点 %4d   出现在 %3d/%3d 帧 (%.0f%%)   中位能量 %.2f' % (
                lo, lo + 10, cnt, frames_with, nf, 100.0 * frames_with / nf,
                float(np.median(ps[sel])) if cnt else 0.0))

    with open(os.path.join(out, 'report.md'), 'w', encoding='utf-8') as f:
        f.write('# 空场扫描\n\n')
        f.write('- 端口：%s\n- 时长：%.0f 秒\n- 总帧：%d\n- 点总数：%d\n\n'
                % (args.port, args.sec, len(all_frames), len(all_points)))
        if len(all_points):
            f.write('## 角度直方图（5° 桶）\n\n```\n')
            hist, edges = np.histogram(degs, bins=range(-50, 55, 5))
            for h, e in zip(hist, edges):
                if h:
                    f.write('%+4d ~ %+4d : %d\n' % (e, e + 5, h))
            f.write('```\n\n## 距离直方图（0.25m 桶）\n\n```\n')
            hist2, edges2 = np.histogram(rs, bins=np.arange(0.0, 3.25, 0.25))
            for h, e in zip(hist2, edges2):
                if h:
                    f.write('%.2f ~ %.2f m : %d\n' % (e, e + 0.25, h))
            f.write('```\n')
    np.savez_compressed(os.path.join(out, 'points.npz'),
                        deg=degs, r=rs, p=ps, n=len(all_frames),
                        t=np.array([q[0] for q in all_points], float))

    print('\n  数据目录：%s' % out)
    print('  把上面直方图贴给 DeepWorks 就能定位。\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n  已中断。')
        sys.exit(1)
