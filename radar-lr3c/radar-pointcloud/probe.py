# -*- coding: utf-8 -*-
r"""
命令行探针：不启动网页，直接把每帧点云摘要打到屏幕/文件。
用于离线抓包核查，也用于实机（带真值标注）的验收测试。

  python probe.py --replay ..\.deepworks\tmp\cap.bin --seconds 3
  python probe.py --port COM3 --seconds 8
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from radar_dsp import PointCloudProcessor
from radar_source import SerialSource, ReplaySource, build_pipeline

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM3')
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--replay', default=None)
    ap.add_argument('--seconds', type=float, default=5.0)
    ap.add_argument('--cfar', type=float, default=2.6)
    ap.add_argument('--min-snr', type=float, default=7.0)
    ap.add_argument('--min-power', type=float, default=0.0)
    ap.add_argument('--no-clutter', action='store_true')
    ap.add_argument('--out', default=None)
    ap.add_argument('--record', default=None)
    args = ap.parse_args()

    lines = []

    def w(s):
        print(s, flush=True)
        lines.append(s)

    proc = PointCloudProcessor(cfar_scale=args.cfar, min_power=args.min_power,
                               min_snr=args.min_snr,
                               remove_clutter=not args.no_clutter)
    if args.replay:
        src = ReplaySource(args.replay, loop=True)
    else:
        src = SerialSource(args.port, args.baud)

    w('数据源: %s   时长: %.1fs   杂波抑制: %s' % (src.name, args.seconds,
                                                  '关' if args.no_clutter else '开'))
    w('%-6s %-6s %-8s %-6s %s' % ('帧#', 'fps', '噪声底', '点数', '最强 4 点 (r m / theta deg / v m/s / 相对强度)'))
    stop = threading.Event()

    def on_frame(z0, z1, stats):
        if stats['uptime'] > args.seconds:
            stop.set()
            return
        pts, noise = proc.process(z0, z1)
        s = '; '.join('%.2f/%+.1f/%+.2f/%.2f' % (q['r'], q['deg'], q['v'], q['p'])
                      for q in pts[:4])
        w('%-6d %-6.2f %-8.0f %-6d %s' % (stats['frames'], stats['fps'], noise, len(pts), s or '—'))

    build_pipeline(src, on_frame, stop, args.record)
    w('结束: 共 %d 帧' % (len(lines) - 2))
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))


if __name__ == '__main__':
    main()
