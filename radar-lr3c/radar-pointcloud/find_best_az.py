# -*- coding: utf-8 -*-
r"""找出 1T2R 的「最佳中线」：哪个方位角上点云最密。

为什么需要这个工具
------------------
2026-11-03 的现场观察：1T2R 斜着用时，最佳点不在板面法线（az=0），
而在法线一侧约 45 度。但 `radar_dsp.ANGLE_MAX = 45` 会把这个位置的点
砍掉一半，于是"最灵敏的角度"在程序里恰好是"被丢掉最多的角度"。

本工具用**放宽到 ±85 度**的口径统计真实角度分布，回答三个问题：

1. 点云峰值到底在几度（不是"大概 45"，要一个数）；
2. 峰值附近有多宽（决定左右移能走多远）；
3. 有多少点被旧的 ±45 门限丢掉（量化旧口径损失）。

用法
----
    python radar-pointcloud\find_best_az.py --port COM3
    python radar-pointcloud\find_best_az.py --port COM3 --save
    python radar-pointcloud\find_best_az.py --replay .deepworks\tmp\seg-lr2\jqz\d1.00\seg_01.00.bin

流程：人站到**自己觉得信号最好的位置**（不要刻意站法线），完全静止 20 秒，
看直方图稳定后读峰值。`--save` 会把峰值与建议的 angle_max 写进 center.json。

注意：这只是"人不动、雷达不动"时的能量分布，不能代替左右移真值采集。
退出：Ctrl+C 或 `--seconds` 到点。
"""
import argparse
import ctypes
import math
import os
import sys
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from radar_dsp import PointCloudProcessor          # noqa: E402
from radar_source import SerialSource, ReplaySource, build_pipeline  # noqa: E402
import radar_center                                # noqa: E402

ESC = '\033'
GREEN = ESC + '[92m'
YELLOW = ESC + '[93m'
RED = ESC + '[91m'
GREY = ESC + '[90m'
BOLD = ESC + '[1m'
REV = ESC + '[7m'
OFF = ESC + '[0m'
CLEAR = ESC + '[2J' + ESC + '[H'


def enable_vt():
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def med(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


class Hist(object):
    def __init__(self, args, color=True):
        self.args = args
        self.color = color
        self.rows = deque()          # (t, deg, p)
        self.frames = 0
        self.points = 0
        self.dropped45 = 0           # |deg| > 45 的点数（旧口径会丢掉）
        self.last_draw = -1e9
        self.peak = None

    def feed(self, t, pts):
        self.frames += 1
        self.points += len(pts)
        for q in pts:
            self.rows.append((t, q['deg'], q['pa']))
            if abs(q['deg']) > 45.0:
                self.dropped45 += 1
        cut = t - self.args.window
        while self.rows and self.rows[0][0] < cut:
            self.rows.popleft()

    def bins(self):
        step = self.args.bin
        nb = int(round(170.0 / step))
        cnt = [0] * nb
        pw = [0.0] * nb
        for _, deg, pa in self.rows:
            i = int((deg + 85.0) // step)
            i = max(0, min(nb - 1, i))
            cnt[i] += 1
            pw[i] += pa
        return cnt, pw

    def update_peak(self, cnt):
        if not cnt or sum(cnt) < 30:
            self.peak = None
            return
        step = self.args.bin
        i = max(range(len(cnt)), key=lambda k: cnt[k])
        # 峰值 bin 用 ±1 bin 的点做加权平均，避免整数度抖动
        num = den = 0.0
        for k in (i - 1, i, i + 1):
            if 0 <= k < len(cnt):
                c = cnt[k]
                num += c * (-85.0 + (k + 0.5) * step)
                den += c
        self.peak = (num / den if den else 0.0, i, cnt)

    def draw(self, force=False):
        now = time.time()
        if not force and now - self.last_draw < 0.4:
            return
        self.last_draw = now
        a = self.args
        cnt, _pw = self.bins()
        self.update_peak(cnt)
        total = sum(cnt)
        o = [CLEAR if self.color else '']
        add = o.append
        add(BOLD + '=' * 74 + OFF)
        add(BOLD + '   最佳中线扫描' + OFF + GREY
            + '  —— 人站到信号最好的位置，不要刻意站法线' + OFF)
        add('   窗口 %.0f s ｜ %d 帧 ｜ %d 点 ｜ 峰值 bin 宽 %.0f 度'
            % (a.window, self.frames, total, a.bin))
        add(BOLD + '=' * 74 + OFF)
        if total < 30:
            add(YELLOW + '   预热中：还没攒够点。让人站定别动，等 5 秒。' + OFF)
            add(GREY + '   如果一直不出点，说明这个角度已经超出可测范围。' + OFF)
            self._emit(o)
            return

        peak_az = self.peak[0] if self.peak else None
        scale = max(cnt) or 1
        step = a.bin
        for i, c in enumerate(cnt):
            lo = -85.0 + i * step
            if c == 0 and abs(lo) > 70:
                continue
            bar = '#' * int(round(30.0 * c / scale))
            share = 100.0 * c / max(1, total)
            hot = self.peak and i == self.peak[1]
            line = '  %+6.1f ~ %+6.1f  %5.1f%%  %s%s'
            txt = line % (lo, lo + step, share, bar, '  <= 峰值' if hot else '')
            if hot:
                add(GREEN + REV + txt + OFF)
            elif abs(lo + step / 2.0) <= 45.0:
                add(txt)
            else:
                add(GREY + txt + OFF)
        add(BOLD + '-' * 74 + OFF)
        if peak_az is not None:
            add('   峰值方位角   %s%+6.1f 度%s' % (BOLD, peak_az, OFF))
            fwhm = 0.0
            if self.peak:
                i = self.peak[1]
                j = i
                thr = max(cnt) * 0.5
                while i > 0 and cnt[i - 1] >= thr:
                    i -= 1
                while j < len(cnt) - 1 and cnt[j + 1] >= thr:
                    j += 1
                fwhm = (j - i + 1) * step
            add('   半高全宽 FWHM 约 %s%.0f 度%s（左右各约 %.0f 度）'
                % (BOLD, fwhm, OFF, fwhm / 2.0))
            if fwhm:
                add(GREY + '   1.0 m 站位下左右各约 %.2f m 是「点云主体」的宽度；'
                    '人横移超过它，质心才会明显离开' % (math.radians(fwhm / 2.0)) + OFF)
            add('   建议                              ')
            add('     --center-az %s%.1f%s   --angle-max %s%.0f%s'
                % (GREEN, peak_az, OFF, GREEN,
                   min(89.0, max(45.0, abs(peak_az) + 30.0)), OFF))
        if self.points:
            add(GREY + '   累计 %d 点中 %d 点（%.0f%%）落在 |az|>45，旧门限会直接丢掉'
                % (self.points, self.dropped45,
                   100.0 * self.dropped45 / max(1, self.points)) + OFF)
        add(BOLD + '=' * 74 + OFF)
        add(GREY + '   Ctrl+C 退出' + (' ｜ --save 会把峰值写进 center.json' if a.save else '')
            + OFF)
        self._emit(o)

    def _emit(self, lines):
        text = '\n'.join(lines) + '\n'
        if not self.color:
            import re
            text = re.sub(ESC + r'\[[0-9;]*m', '', text).replace(CLEAR, '')
        sys.stdout.write(text)
        sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description='扫描最佳方位角（只读，不写雷达）')
    ap.add_argument('--port', default='COM3')
    ap.add_argument('--baud', type=int, default=None, help='留空 = 按串口指纹自动认板型')
    ap.add_argument('--replay', default=None)
    ap.add_argument('--seconds', type=float, default=0.0, help='>0 到点自动退出')
    ap.add_argument('--window', type=float, default=6.0, help='统计窗口（秒）')
    ap.add_argument('--bin', type=float, default=5.0, help='直方图分箱宽度（度）')
    ap.add_argument('--angle-max', type=float, default=85.0,
                    help='扫描口径，默认 85（旧口径 45 会把贴着边界的峰值切掉）')
    ap.add_argument('--min-range', type=float, default=0.681, help='近距门，默认屏蔽 bin0')
    ap.add_argument('--save', action='store_true', help='退出时把峰值写进 center.json')
    ap.add_argument('--board', default='1t2r')
    ap.add_argument('--mono', action='store_true')
    args = ap.parse_args(argv)

    color = sys.stdout.isatty() and not args.mono
    if color:
        enable_vt()

    proc = PointCloudProcessor(min_range=args.min_range, angle_max=args.angle_max,
                               max_points=256)
    hist = Hist(args, color=color)
    stop = threading.Event()

    def on_frame(z0, z1, stats):
        if args.seconds and stats['uptime'] > args.seconds:
            stop.set()
            return
        pts, _ = proc.process(z0, z1)
        hist.feed(stats['uptime'], pts)
        hist.draw()

    src = ReplaySource(args.replay, loop=True) if args.replay else SerialSource(
        args.port, args.baud)
    print('打开数据源 ...' if not args.replay else '回放 %s ...' % os.path.basename(args.replay))
    try:
        build_pipeline(src, on_frame, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()

    if hist.peak is None:
        print('没攒到足够的点：这个人/这个角度可能完全不在可测范围。')
        return 1
    az = hist.peak[0]
    angle_max = min(89.0, max(45.0, abs(az) + 30.0))
    print('')
    print('峰值方位角 %+.1f 度，建议 angle_max %.0f 度' % (az, angle_max))
    if args.save:
        radar_center.save(args.board, az, angle_max,
                          note='find_best_az 扫描结果（%s）' % time.strftime('%Y-%m-%d %H:%M'))
        print('已写入 %s：%s' % (radar_center.PATH, os.path.basename(radar_center.PATH)))
    else:
        print('未保存。确认可靠后重跑加 --save，或手动传给 align_assist。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
