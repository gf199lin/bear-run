# -*- coding: utf-8 -*-
"""生成合成字节流样本 sample-synthetic.bin —— 不含任何人体数据，可提交入库。

用途：无硬件、无真人采集时也能跑通「解析 → 点云 → 网页显示」全链路，
作为课程演示的兜底方案（对应 AGENTS.md 的「模拟器兜底是 P1 需求」）。

用法：
    python make_sample.py [输出文件]
默认输出到本目录的 sample-synthetic.bin。

场景：人在 y = 3.0 m 处，先静立，再向左走、向右走、走回中间（每段 30 帧）。
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from radar_source import LoopbackSource  # noqa: E402

FPS = 7.72
SEG = 30
N = SEG * 4


def scenario():
    """返回每帧的目标 (x, y, v)，单位 m 与 m/s。"""
    t = []
    for _ in range(SEG):                       # ① 静立
        t.append((0.0, 3.0, 0.0))
    for k in range(SEG):                       # ② 向左走 x: 0 → -1.2
        t.append((-1.2 * (k + 1) / SEG, 3.0, -0.31))
    for k in range(SEG):                       # ③ 向右走 x: -1.2 → +1.2
        t.append((-1.2 + 2.4 * (k + 1) / SEG, 3.0, +0.62))
    for k in range(SEG):                       # ④ 走回中间 x: +1.2 → 0
        t.append((1.2 - 1.2 * (k + 1) / SEG, 3.0, -0.31))
    return t


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'sample-synthetic.bin')
    src = LoopbackSource(targets=scenario(), frames=N)
    src.open()
    data = bytes(src.data)
    with open(out, 'wb') as f:
        f.write(data)
    print('写出 %s' % out)
    print('  字节 %d  帧 %d  时长 %.1f s  速率 %.2f 帧/s' % (len(data), N, N / FPS, FPS))
    print('  提示：python server.py --source replay --replay "%s"' % os.path.basename(out))


if __name__ == '__main__':
    main()
