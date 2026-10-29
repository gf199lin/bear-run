"""2T4R 直流/近场泄漏来源诊断。

目的：判断门 0~1 上那股压倒性的能量，是设备固有泄漏，
      还是来自环境反射 / 站立的人。

方法：同一块板、同一位置、同一配置，采两段——
      段A 现场清空（无人在雷达前）
      段B 人站到正常位置保持不动
      对比总能量、近场门占比、多普勒谱分布。

用法：
    python tools\\dc_probe.py
    python tools\\dc_probe.py --port COM4 --seconds 8

判读要点：
    两段总能量接近、且近场门都占 80%+  → 设备固有泄漏，改几何/增益才有用
    仅有人的那段飙升                        → 人体过近或反射过强，调安装距离
    两段都低                                → 当前不是DC问题，是增益/量程配置
"""

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "radar-pointcloud"))

from radar_source import SerialSource, make_protocol  # noqa: E402


def capture(port, seconds, baud=None):
    src = SerialSource(port, baud)
    src.open()
    parser, asm = make_protocol("2t4r")
    out = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        chunk = src.read(0.05)
        if not chunk:
            continue
        for rf in parser.feed(chunk):
            c = asm.push(rf)
            if c is not None:
                out.append(c)
    src.close()
    return out


def summarize(name, frames):
    if not frames:
        print("  %s: 没有收到任何帧" % name)
        return None
    z = np.stack(frames)                      # (n, chirps, bins, rx)
    dop = np.fft.fftshift(np.fft.fft(z, axis=1), axes=1)
    c = dop.shape[1] // 2
    d0 = dop[:, c]                            # 零多普勒 (n, bins, rx)

    prof = (np.abs(d0) ** 2).sum(axis=(0, 2))  # (bins,)
    total = prof.sum()
    near = prof[:3].sum() / max(total, 1e-12)

    spec = (np.abs(dop) ** 2).sum(axis=(2, 3)).mean(axis=0)   # (chirps,)
    pos = spec[c + 2:].sum()
    neg = spec[:c - 1].sum()

    print("  %s" % name)
    print("    帧数        : %d" % z.shape[0])
    print("    总能量      : %.3e" % total)
    print("    近场门0~2   : %.1f%%" % (near * 100))
    print("    峰值门      : %d (占 %.1f%%)" % (int(np.argmax(prof)), 100 * prof.max() / total))
    print("    每门能量    : %s" % np.array2string(
        (prof / total * 100)[:10], precision=1, floatmode="fixed"))
    print("    多普勒 正/负: %.3e / %.3e  (比 %.3f)" % (pos, neg, pos / max(neg, 1e-12)))
    return {"total": total, "near": near, "prof": prof, "pos": pos, "neg": neg}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=None)
    ap.add_argument("--seconds", type=float, default=8.0)
    args = ap.parse_args()

    print("=" * 66)
    print("  2T4R 直流/近场泄漏来源诊断   port=%s  %.0fs/段" % (args.port, args.seconds))
    print("=" * 66)
    print("\n注意：运行前请关闭占着串口的其它程序（radar_live_ws.py / 厂商工具）。")

    input("\n[段A] 请把雷达前方清空（人退到 3 米外、避开波束），然后按回车...")

    print("\n  采集 段A（空场）...")
    a = capture(args.port, args.seconds, args.baud)
    sa = summarize("段A 空场", a)

    print("\n[段B] 请站到雷达正前方约 2 米处，保持站立不动。")
    for i in (5, 4, 3, 2, 1):
        print("    %d..." % i)
        time.sleep(1.0)

    print("\n  采集 段B（有人静止）...")
    b = capture(args.port, args.seconds, args.baud)
    sb = summarize("段B 有人静止", b)

    print("\n" + "=" * 66)
    print("  判读")
    print("=" * 66)
    if not sa or not sb:
        print("  数据不足，无法判读。")
        return 2

    ratio = sb["total"] / max(sa["total"], 1e-12)
    print("  总能量比 (有人/空场)      : %.2fx" % ratio)
    print("  近场门占比 空场/有人      : %.1f%% / %.1f%%"
          % (sa["near"] * 100, sb["near"] * 100))

    if ratio < 1.5:
        print("\n  >> 有人与空场能量几乎相同：近场能量是**设备固有泄漏/环境固定反射**。")
        print("     人体回波被压在下面，调算法没用。要动几何（抬高、改俯仰角）")
        print("     或改配置（降低增益、推远距离门起始）。")
    elif sb["near"] > 0.8:
        print("\n  >> 有人的时候能量上升，但 80%+ 仍挤在近场门：")
        print("     人离得太近或正对强反射面，回波没进有效距离门。")
        print("     把人往后移，或调整板子俯仰角让波束打到躯干。")
    else:
        print("\n  >> 有人的时候能量上升且分布到远端门：")
        print("     器件是好的，问题在当前的采集几何/距离。优先调摆放。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
