# -*- coding: utf-8 -*-
r"""对准实时助手：把「人静息时的方位角 / 横向位置 / 检出率」打在屏幕上。

用途：2026-10-22 的 seg-lr2 采集显示静息方位角 +25.8°（要求 ≤10°），
人落在视场边缘，向左移动时目标大面积丢失（LEFT 0/10）。这个工具就是为了
把那 26 度调回来：一边转雷达（或挪人），一边看数字往 0 靠。

口径与 outputs/eval-lr2-jqz.md 一致：
    radar_actions.ActionRecognizer(band='auto', crit='drift', top_k=1, min_pts=1)
    方位角 az = atan2(px, r_mean)，px / r_mean 取自 recognizer 的 features。

判定（可用参数改）：|az| <= 10 度、|px| < 0.20 m、检出率 >= 80%。
读数都用滚动窗口（默认 5 s）的中位数与 10/90 分位，避免瞬时抖动误导。

用法：
    python radar-pointcloud\align_assist.py --port COM3
    python radar-pointcloud\align_assist.py --replay .deepworks\tmp\seg-lr2\jqz\d0.70\seg_00.70.bin

退出：Ctrl+C。工具只读串口，不写文件、不发指令。
"""
import argparse
import ctypes
import math
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from radar_dsp import PointCloudProcessor          # noqa: E402
from radar_source import SerialSource, ReplaySource, build_pipeline   # noqa: E402
import radar_actions as ra                          # noqa: E402

ESC = '\033'
GREEN = ESC + '[92m'
YELLOW = ESC + '[93m'
RED = ESC + '[91m'
GREY = ESC + '[90m'
BOLD = ESC + '[1m'
REV = ESC + '[7m'
OFF = ESC + '[0m'
ANSI_RE = re.compile(ESC + r'\[[0-9;]*m')
CLEAR = ESC + '[2J' + ESC + '[H'


def enable_vt():
    """让传统 conhost 也认 ANSI 转义（Windows Terminal 本来就认）。"""
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


def pct(xs, q):
    """分位数（线性取整索引，够用）。"""
    xs = sorted(xs)
    if not xs:
        return None
    i = int(round(q * (len(xs) - 1)))
    return xs[min(len(xs) - 1, max(0, i))]


class Board(object):
    """滚动窗口状态 + 刷屏。"""

    MIN_SAMPLES_FOR_BEST = 15

    def __init__(self, args, color=True):
        self.args = args
        self.color = color
        self.rows = []          # (t, frame, az, px, r_mean, npts)
        self.frames = 0
        self.det_frames = 0
        self.t0 = None
        self.last_draw = -1e9
        self.best = None        # (abs_az, az, t)
        self.notes = []

    def feed(self, t, frame, az, px, r_mean, npts):
        self.frames += 1
        if self.t0 is None:
            self.t0 = t
        if px is None:
            return
        self.det_frames += 1
        self.rows.append((t, frame, az, px, r_mean, npts))
        cut = t - self.args.window
        while self.rows and self.rows[0][0] < cut:
            self.rows.pop(0)

    def verdict(self):
        win = self.rows
        if len(win) < 3:
            return None
        azs = [r[2] for r in win if r[2] is not None]
        pxs = [r[3] for r in win if r[3] is not None]
        if not azs or not pxs:
            return None
        f0, f1 = win[0][1], win[-1][1]
        n_frames = max(1, f1 - f0 + 1)
        return dict(
            az=med(azs), px=med(pxs),
            az_lo=pct(azs, 0.10), az_hi=pct(azs, 0.90),
            n=len(win), total=n_frames,
            rate=min(1.0, len(win) / float(n_frames)),
            r_mean=med([r[4] for r in win if r[4] is not None]),
            npts=med([r[5] for r in win if r[5] is not None]),
        )

    def update_best(self, v):
        """只有窗口样本足够多时才算「最佳」，避免用瞬时抖动骗自己。"""
        if v is None or v['n'] < self.MIN_SAMPLES_FOR_BEST:
            return
        if v['az'] is None:
            return
        if self.best is None or abs(v['az']) < self.best[0]:
            self.best = (abs(v['az']), v['az'], time.time() - (self.t0 or 0))

    def _emit(self, lines):
        text = '\n'.join(lines) + '\n'
        if not self.color:
            text = ANSI_RE.sub('', text)
            text = text.replace(CLEAR, '')
        sys.stdout.write(text)
        sys.stdout.flush()

    def draw(self, force=False, stats=None):
        now = time.time()
        if not force and now - self.last_draw < 0.4:
            return
        self.last_draw = now
        a = self.args
        v = self.verdict()
        self.update_best(v)

        o = []
        add = o.append
        add(CLEAR if self.color else '')
        add(BOLD + '=' * 68 + OFF)
        add(BOLD + '   对准实时助手' + OFF + GREY + '  —— 调雷达朝向，让静息方位角回到 0 附近' + OFF)
        src = ('回放 ' + os.path.basename(a.replay)) if a.replay else ('实机 ' + a.port)
        add('   %s   %s帧 %d' % (src, (stats or {}).get('board', '') and
                                  '[%s] ' % stats['board'] or '', self.frames))
        add(BOLD + '=' * 68 + OFF)
        add('')

        if v is None:
            add(YELLOW + BOLD + '   预热中：目标还没站稳，或自动选带还没锁定（约需 30 帧 ≈ 4 s）' + OFF)
            add('')
            add(GREY + '   让一个人站在雷达正前方的主站位（0.70 m），站稳不要动。' + OFF)
            add(GREY + '   这一步只调朝向，不要一边看屏幕一边晃身体。' + OFF)
            add('')
            add(BOLD + '=' * 68 + OFF)
            add(GREY + '   Ctrl+C 退出' + OFF)
            self._emit(o)
            return

        def mark(ok):
            return (GREEN + '  [ 达标 ]' + OFF) if ok else (RED + '  [ 不达标 ]' + OFF)

        ok_az = abs(v['az']) <= a.target_az
        ok_px = abs(v['px']) <= a.target_px
        ok_rt = v['rate'] >= 0.80

        add('  静息方位角 az   %s%+7.1f 度%s   目标 |az| <= %-3.0f 度  %s'
            % (BOLD, v['az'], OFF, a.target_az, mark(ok_az)))
        add('  静息横向  px   %s%+7.2f m %s   目标 |px| <  %-.2f m %s'
            % (BOLD, v['px'], OFF, a.target_px, mark(ok_px)))
        add('  目标检出率      %s%6.0f %%  %s   目标 >= 80 %%     %s'
            % (BOLD, v['rate'] * 100, OFF, mark(ok_rt)))
        add('')
        add('  距离 r_mean     %s     点数 %s'
            % ('%.2f m' % v['r_mean'] if v['r_mean'] is not None else '  —  ',
               '%.0f' % v['npts'] if v['npts'] is not None else '—'))
        add('  最近 %.0f s 的 az 区间  %+.1f ~ %+.1f 度（10~90 分位，%d/%d 帧有目标）'
            % (a.window, v['az_lo'], v['az_hi'], v['n'], v['total']))
        if self.best is not None:
            add(GREY + '  本次最稳的 |az| = %.1f 度（该窗口 az=%+.1f 度）'
                % (self.best[0], self.best[1]) + OFF)
        add('')
        add(BOLD + '-' * 68 + OFF)

        if ok_az and ok_px:
            add(GREEN + REV + BOLD + '  >>>>>>>>>>   对准 OK，去跑采集   <<<<<<<<<<  ' + OFF)
        else:
            add(RED + REV + BOLD + '  >>>>>>>>>>   还不够，继续调    <<<<<<<<<<  ' + OFF)
            if not ok_az:
                add('   方位角 %+.0f 度 → 把雷达绕垂直轴朝「人体所在的那一侧」转约 %.0f 度，'
                    % (v['az'], abs(v['az'])))
                add(GREY + '   转向不确定就先转 5 度试：数字变小 = 对了，变大 = 反过来转。' + OFF)
                add(GREY + '   或者反过来把人横向挪 %.2f m，效果一样。' % abs(v['px']) + OFF)
            if not ok_rt:
                add('   检出率 %.0f%%：人可能还在视场边缘/杂波里；先把 az 调进去再看这一项。'
                    % (v['rate'] * 100))
        if self.notes:
            for n in self.notes[-2:]:
                add(GREY + '   ' + n + OFF)
        add(BOLD + '=' * 68 + OFF)
        add(GREY + '   Ctrl+C 退出 ｜ 可调：--target-az %.0f --target-px %.2f --window %.1f'
            % (a.target_az, a.target_px, a.window) + OFF)
        self._emit(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description='对准实时助手（只读，不写文件）')
    ap.add_argument('--port', default='COM3')
    ap.add_argument('--baud', type=int, default=None, help='默认留空 = 自动认板型')
    ap.add_argument('--replay', default=None, help='离线回放一个 .bin（验证/演练）')
    ap.add_argument('--seconds', type=float, default=0.0, help='>0 时到点自动退出')
    ap.add_argument('--target-az', type=float, default=10.0)
    ap.add_argument('--target-px', type=float, default=0.20)
    ap.add_argument('--window', type=float, default=5.0, help='判定用的滚动窗口（秒）')
    ap.add_argument('--mono', action='store_true', help='不开颜色/不清屏（写日志用）')
    args = ap.parse_args(argv)

    color = sys.stdout.isatty() and not args.mono
    if color:
        enable_vt()

    proc = PointCloudProcessor()
    rec = ra.ActionRecognizer(band='auto', crit='drift', top_k=1, min_pts=1)
    board = Board(args, color=color)

    if args.replay:
        src = ReplaySource(args.replay, loop=True)
    else:
        src = SerialSource(args.port, args.baud)

    stop = threading.Event()

    def on_frame(z0, z1, stats):
        if args.seconds and stats['uptime'] > args.seconds:
            stop.set()
            return
        pts, noise = proc.process(z0, z1)
        t = stats['uptime']
        r = rec.push(pts, t)
        ft = r['features'] or {}
        px, rmean = ft.get('px'), ft.get('r_mean')
        az = math.degrees(math.atan2(px, rmean)) if (px is not None and rmean) else None
        board.feed(t, stats['frames'], az, px, rmean, ft.get('n'))
        board.draw(stats=stats)

    print('打开数据源 ...' if not args.replay else '回放 %s ...' % os.path.basename(args.replay))
    try:
        build_pipeline(src, on_frame, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()

    v = board.verdict()
    print('')
    if v is None:
        print('没拿到可用窗口（目标太少）。换个站位/角度再来一次。')
    else:
        print('结束：窗口内 az=%+.1f 度  px=%+.2f m  检出率 %.0f%%'
              % (v['az'], v['px'], v['rate'] * 100))
        if board.best is not None:
            print('本次最稳的 |az| = %.1f 度' % board.best[0])
    print('判定标准：|az| <= %.0f 度、|px| < %.2f m、检出率 >= 80%%'
          % (args.target_az, args.target_px))
    return 0


if __name__ == '__main__':
    sys.exit(main())
