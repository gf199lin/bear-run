# -*- coding: utf-8 -*-
"""
离线自检（无需硬件）:
  1) 解析实机抓包 cap.bin → 帧同步、包数、帧率、坏包位置、真实帧上的点云输出
  2) 编码-解码闭环：合成已知目标 → 编码为真实字节流 → 解析 → 反算目标位置
  3) 方位角符号与线性度
输出 UTF-8 文本 (selftest-report.txt)，作为可复查证据。
"""
import io
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import radar_dsp as dsp
from radar_dsp import PointCloudProcessor
from radar_protocol import StreamParser, FrameAssembler
from radar_source import LoopbackSource

OUT = io.StringIO()


def p(*a):
    print(*a)
    print(*a, file=OUT)


def test_capture(path):
    p('=' * 74)
    p('测试 1  实机抓包解析: %s' % path)
    p('=' * 74)
    if not os.path.exists(path):
        p('!! 文件不存在，跳过')
        return
    data = open(path, 'rb').read()
    parser = StreamParser()
    asm = FrameAssembler()
    frames = []
    first_rf = None
    for i in range(0, len(data), 4096):
        for rf in parser.feed(data[i:i + 4096]):
            if first_rf is None:
                first_rf = rf
            pair = asm.push(rf)
            if pair is not None:
                frames.append(pair)
    dt = len(data) / (52 * 991.0)     # 按实测速率折算时长
    p('字节数              : %d' % len(data))
    p('解析包数            : %d' % parser.packets)
    p('跳过字节 / 坏包     : %d / %d   重同步位置: %s' %
      (parser.skipped_bytes, parser.bad_packets, parser.resync_at[:8]))
    p('拼装完整雷达帧      : %d   丢弃残缺帧: %d' % (len(frames), asm.dropped))
    p('估算帧率            : %.2f fps   (期望 ≈7.72 fps)' % (len(frames) / dt))
    p('首包 chirp/rx       : chirp=%d rx=%d' % (first_rf.chirp, first_rf.rx))
    p('首包 8 距离单元 (大端 int16 → 复数):')
    p('  ' + '  '.join('%.0f%+.0fj' % (z.real, z.imag) for z in first_rf.bins))
    if frames:
        z0, z1 = frames[len(frames) // 2]
        p('中位帧矩阵          : z0=%s z1=%s' % (z0.shape, z1.shape))
        p('幅度 均值/峰值      : %.1f / %.1f' % (np.abs(z0).mean(), np.abs(z0).max()))
        proc = PointCloudProcessor(cfar_scale=2.0, min_power=50.0, remove_clutter=False)
        p('真实数据点云（末帧, 静态杂波保留）:')
        for k, (a, b) in enumerate(frames[-3:]):
            pts, noise = proc.process(a, b)
            s = '; '.join('r=%.2fm θ=%+.1f° v=%+.2f p=%.2f' %
                          (q['r'], q['deg'], q['v'], q['p']) for q in pts[:4])
            p('  帧#%d 噪声底=%.0f 点数=%d   %s' % (len(frames) - 3 + k, noise, len(pts), s or '—'))
    return frames


def test_loopback(targets):
    p('')
    p('=' * 74)
    p('测试 2  编码-解码闭环（合成已知目标，走真实字节流）')
    p('=' * 74)
    proc = PointCloudProcessor(cfar_scale=2.0, min_power=200.0, remove_clutter=True)
    src = LoopbackSource(targets=targets, frames=48)
    src.open()
    parser = StreamParser()
    asm = FrameAssembler()
    ok = tested = 0
    t0 = time.time()
    while tested < 40 and time.time() - t0 < 60:
        chunk = src.read(0.05)
        if not chunk:
            continue
        for rf in parser.feed(chunk):
            pair = asm.push(rf)
            if pair is None:
                continue
            tested += 1
            tx, ty, tv = src.frame_targets[tested - 1]
            tr = math.hypot(tx, ty)
            ta = math.degrees(math.atan2(tx, ty))
            pts, _ = proc.process(*pair)
            if not pts:
                p('  帧#%-3d 未检出' % tested)
                continue
            q = pts[0]
            good = (abs(q['r'] - tr) <= 0.7 and abs(q['deg'] - ta) <= 6.0
                    and abs(q['v'] - tv) <= 0.12)
            ok += good
            if tested <= 6 or tested % 12 == 0 or not good:
                p('  帧#%-3d 真值 r=%5.2f θ=%+6.1f° v=%+.2f  →  测得 r=%5.2f θ=%+6.1f° v=%+.2f '
                  '点数=%d %s' % (tested, tr, ta, tv, q['r'], q['deg'], q['v'], len(pts),
                                  'OK' if good else 'FAIL'))
    p('闭环一致            : %d / %d' % (ok, tested))
    return ok, tested


def test_angle():
    p('')
    p('=' * 74)
    p('测试 3  方位角符号与线性度（静态目标，保留 DC）')
    p('=' * 74)
    proc = PointCloudProcessor(cfar_scale=2.0, min_power=200.0, remove_clutter=False)
    errs = []
    for x in (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
        z0, z1 = dsp.synth_frame(target=(x, 3.0, 0.0), seed=1)
        pts, _ = proc.process(z0, z1)
        got = pts[0]['deg'] if pts else float('nan')
        want = math.degrees(math.atan2(x, 3.0))
        errs.append(abs(got - want))
        p('  真实 x=%+4.1f m → 期望方位 %+6.1f°   测得 %+6.1f°   (r=%.2f m)'
          % (x, want, got, pts[0]['r'] if pts else float('nan')))
    p('方位角最大误差      : %.2f°' % max(errs))
    return max(errs)


if __name__ == '__main__':
    cap = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(HERE, '..', '.deepworks', 'tmp', 'cap.bin')
    test_capture(cap)
    ok, tested = test_loopback([(1.2, 3.0, -0.5), (0.0, 2.0, 0.4), (-1.6, 4.0, 0.45)])
    e = test_angle()
    p('')
    p('=' * 74)
    p('结论: 解析链路 %s；闭环 %d/%d；方位角误差 %.2f°' %
      ('通过' if tested and ok == tested else '需复核', ok, tested, e))
    p('=' * 74)
    out = os.path.join(HERE, 'selftest-report.txt')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(OUT.getvalue())
    print('\n报告已写入: %s' % out)
