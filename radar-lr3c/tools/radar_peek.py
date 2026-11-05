"""实时查看每个距离 bin 的能量，用来确认人站在哪、有没有进波束。

用法：
    python tools\\radar_peek.py --port COM4
    python tools\\radar_peek.py --profile base     # 520 ms 对照

看 chg（变化量）那一列：你挥手/下蹲时哪根柱子跳，人就在那个 bin。
abs 列被近距泄漏（bin 0）压平是正常的，别拿它判断。
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))

import jumpduck_guide as G


def bar(v, vmax, width, fill='#'):
    if vmax <= 0:
        return '.' * width
    n = int(round(width * max(0.0, min(1.0, v / vmax))))
    return fill * n + '.' * (width - n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM4')
    ap.add_argument('--profile', default='130ms')
    ap.add_argument('--board', default='2t4r', choices=('2t4r', '1t2r'))
    ap.add_argument('--seconds', type=float, default=120.0)
    ap.add_argument('--skip', type=int, default=2,
                    help='从第几个 bin 开始画相对柱（跳过近距泄漏）')
    a = ap.parse_args()

    ser, msg = G.apply_profile(a.profile, a.port, True, a.board)
    print(msg)
    reader = G.DirectReader(ser, a.board)
    bins = int(reader.asm.bins)
    bw = 2.82 / bins
    print('%d bin，分辨率 %.2f m。' % (bins, bw))
    print('先静止 3 秒看基线，再挥手/下蹲，盯 chg 列哪根柱子跳。Ctrl+C 退出。\n')

    t0 = time.time()
    acc = np.zeros(bins)
    n = 0
    last = 0.0
    slow = None
    sq = np.zeros(bins)
    cnt = 0
    try:
        while time.time() - t0 < a.seconds:
            for z in reader.poll(0.05):
                acc += np.abs(z).mean(axis=(0, 1))      # (bins,)
                n += 1
            now = time.time() - t0
            if now - last < 0.6 or n == 0:
                continue
            last = now
            prof = acc / n
            acc = np.zeros(bins)
            n = 0
            if slow is None or now < 2.5:
                # 刚上电/刚下发时近距 bin 有个衰减瞬态，直接用它会造出假的高 chg
                slow = prof.copy() if slow is None else 0.82 * slow + 0.18 * prof
                continue
            slow = 0.82 * slow + 0.18 * prof
            chg = prof - slow
            sq += chg ** 2
            cnt += 1
            tail = slice(a.skip, None)
            vmax = float(prof[tail].max()) or 1.0
            dmax = max(float(np.abs(chg[tail]).max()), 3.0)   # 噪声底别画成大柱子
            peak = int(prof[tail].argmax()) + a.skip
            print('--- %5.1fs  abs峰值 bin %d (%.2f m)  |  chg峰值 bin %d '
                  '(%+.1f) ---' % (now, peak, (peak + 0.5) * bw,
                                   int(np.abs(chg[tail]).argmax()) + a.skip,
                                   float(chg[tail][int(np.abs(chg[tail]).argmax())])))
            for i in range(bins):
                print('  bin %2d %5.2fm  abs [%s] %7.1f   chg [%s] %+7.1f'
                      % (i, (i + 0.5) * bw,
                         bar(prof[i], vmax, 24), prof[i],
                         bar(max(chg[i], 0), dmax, 24), chg[i]))
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
        if cnt:
            std = np.sqrt(sq / cnt)
            order = np.argsort(std)[::-1]
            best = next((j for j in order if j >= a.skip), None)
            print('\n===== 变化强度排名（越靠前越像"有东西在动"）=====')
            for i in order:
                if i < a.skip:
                    continue
                flag = '  <== 变化最大' if i == best else ''
                print('  bin %2d  %5.2f m   std %6.2f%s'
                      % (i, (i + 0.5) * bw, std[i], flag))
            print('提示：人挥手/下蹲应让某个 bin 的 std 到 10 以上；'
                  '全都是 2~4 说明人没进波束。')
        print('\n已关闭。')


if __name__ == '__main__':
    main()
