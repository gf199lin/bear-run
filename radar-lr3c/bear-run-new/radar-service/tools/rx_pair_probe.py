# -*- coding: utf-8 -*-
"""2T4R 通道对诊断：一次采集，同时算 4 种通道对的角，看哪个能分辨左/中/右。

背景
----
从 Radar_Config.ini 推断 RX 是 L 形阵列：

        RX3
         |
   RX0---RX1---RX2

    HorzAngleInfo = 0,1,2   → 水平：RX0→RX1→RX2
    VertAngleInfo = 5,2,3   → 垂直：RX2→RX3（RX2 是拐角）

所以「哪一对通道对应哪个方向」取决于板子怎么摆。
板子横放 90° 之后，原本水平的 0-1 会变成垂直，原本垂直的 2-3 会变成水平。

本脚本不做判定，只把 4 种组合都打出来，让你自己看哪个方向对得上。

用法
----
    python tools\\rx_pair_probe.py
    python tools\\rx_pair_probe.py --rounds 1 --no-voice
"""

import sys
import os
import json
import time
import argparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.dirname(HERE)                  # radar-service/
ROOT = os.path.dirname(SERVICE)                  # 仓库根
# 兼容两种布局：仓库内（模块在 radar-service/ 根）与单独拷贝的包
for _cand in (SERVICE, os.path.join(SERVICE, "radar-pointcloud"),
              os.path.join(ROOT, "radar-pointcloud")):
    if os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.insert(0, _cand)

from radar_source import SerialSource, make_protocol   # noqa: E402

try:
    import winsound
except ImportError:
    winsound = None

# 采集结果不入库（.gitignore 已忽略 radar-service/captures/）
OUT_DIR = os.path.join(SERVICE, "captures", "rxpair")

BIN_M = 0.15
BAND_LO = 6
BAND_HI = 21

# 4 种通道对：名字 → (a, b)，角度 = angle(z[b] * conj(z[a]))
PAIRS = [
    ("z0-z1", 0, 1),
    ("z0-z2", 0, 2),
    ("z0-z3", 0, 3),
    ("z2-z3", 2, 3),
]


class Recorder:
    def __init__(self, port, board):
        self.src = SerialSource(port, None)
        self.src.open()
        self.parser, self.asm = make_protocol(board)

    def grab(self, seconds, label=""):
        frames = []
        t0 = time.time()
        while time.time() - t0 < seconds:
            chunk = self.src.read(0.05)
            if not chunk:
                continue
            for rf in self.parser.feed(chunk):
                z = self.asm.push(rf)
                if z is not None:
                    frames.append(np.stack(z))
                    if label:
                        bar = int((time.time() - t0) / seconds * 20)
                        print("\r  [%-4s] %s %d 帧" %
                              (label, "#" * bar + "." * (20 - bar), len(frames)),
                              end="", flush=True)
        if label:
            print()
        return frames

    def close(self):
        try:
            self.src.close()
        except Exception:
            pass


def angle_series(frames, a, b):
    """用通道对的相位差算角度序列（度），带内按能量加权圆平均。"""
    bins = list(range(BAND_LO, BAND_HI))
    out = []
    for z in frames:
        w = (np.abs(z) ** 2).sum(axis=(0, 1))[BAND_LO:BAND_HI].astype(float)
        if w.sum() <= 0:
            continue
        vec = np.empty(len(bins), dtype=complex)
        for i, k in enumerate(bins):
            ang = np.angle(z[b, :, k] * np.conj(z[a, :, k]))
            vec[i] = np.exp(1j * ang).mean()
        out.append(float(np.degrees(np.angle((vec * w).sum()))))
    return np.array(out)


def circ_mean_deg(seq):
    if seq.size == 0:
        return float("nan")
    return float(np.degrees(np.angle(np.exp(1j * np.radians(seq)).mean())))


def circ_std_deg(seq):
    if seq.size < 2:
        return float("nan")
    R = float(np.abs(np.exp(1j * np.radians(seq)).mean()))
    R = min(max(R, 1e-9), 1.0)
    return float(np.degrees(np.sqrt(-2.0 * np.log(R))))


def summarize(frames):
    if not frames:
        return None
    d = {"n": len(frames)}
    for name, a, b in PAIRS:
        s = angle_series(frames, a, b)
        s = s[np.isfinite(s)]
        if s.size == 0:
            continue
        d[name] = {
            "mean": round(circ_mean_deg(s), 2),
            "std": round(circ_std_deg(s), 2),
            "seq": [round(float(v), 2) for v in s],
        }
    return d


def run(port, board, rounds, sec):
    print("=" * 70)
    print("  2T4R 通道对诊断     板子=%s  端口=%s" % (board.upper(), port))
    print("=" * 70)
    print("  一次采 3 段：中间 -> 左边 -> 右边，每段 %.0f 秒" % sec)
    print("  同时算 4 种通道对的角，看哪一对的方向最对得上你的移动。\n")

    try:
        rec = Recorder(port, board)
    except Exception as exc:
        print("连接失败: %s" % exc)
        return 3

    result = {"port": port, "board": board, "ts": time.strftime("%Y%m%d-%H%M%S"),
              "seg_sec": sec, "rounds": []}

    try:
        for r in range(1, rounds + 1):
            print("\n---------- 第 %d / %d 轮 ----------" % (r, rounds))
            one = {}
            for key, label_cn, hint in (
                ("center", "中", "站在中间"),
                ("left", "左", "走到左边"),
                ("right", "右", "走到右边"),
            ):
                print("  请%s，%d 秒" % (hint, int(sec)))
                if winsound:
                    try:
                        winsound.Beep(660 if key == "center" else
                                      (520 if key == "left" else 880), 200)
                    except Exception:
                        pass
                one[key] = summarize(rec.grab(sec, label_cn))
                if one[key]:
                    parts = []
                    for name, _, _ in PAIRS:
                        v = one[key].get(name)
                        if v:
                            parts.append("%s %+6.1f±%4.1f" %
                                         (name, v["mean"], v["std"]))
                    print("  %s  %s" % (label_cn, "   ".join(parts)))
                else:
                    print("  %s  无数据" % label_cn)
                time.sleep(1.0)
            result["rounds"].append(one)
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        rec.close()

    save(result)
    analyze(result)
    return 0


def analyze(result):
    rows = [r for r in result["rounds"]
            if r.get("center") and r.get("left") and r.get("right")]
    if not rows:
        print("\n数据不足。")
        return

    print("\n" + "=" * 70)
    print("  各通道对的「左 / 中 / 右」均值")
    print("=" * 70)
    hdr = "  %-8s" % "通道对"
    for x in rows:
        hdr += "  轮%d 左/中/右" % rows.index(x)
    print(hdr)

    print("  " + "-" * 66)
    scores = {}
    for name, _, _ in PAIRS:
        line = "  %-8s" % name
        spread_list = []
        for x in rows:
            L = x["left"].get(name)
            C = x["center"].get(name)
            R = x["right"].get(name)
            if not (L and C and R):
                line += "  (缺)"
                continue
            line += "  %+5.0f/%+5.0f/%+5.0f" % (L["mean"], C["mean"], R["mean"])
            spread_list.append(max(L["mean"], C["mean"], R["mean"])
                               - min(L["mean"], C["mean"], R["mean"]))
        if spread_list:
            scores[name] = float(np.mean(spread_list))
            line += "   跨距 %.0f°" % scores[name]
        print(line)

    print("\n" + "=" * 70)
    print("  判定：跨距越大，这一对越可能是你要测的方向")
    print("=" * 70)
    if not scores:
        print("  没有完整数据。")
        return
    best = max(scores, key=scores.get)
    for name, v in sorted(scores.items(), key=lambda kv: -kv[1]):
        mark = "  ← 最大" if name == best else ""
        print("  %-8s 跨距 %5.1f°%s" % (name, v, mark))

    print("\n  提示：")
    print("    板子竖放时水平方向应看 z0-z1；板子横放时应看 z2-z3。")
    print("    如果横放后 z2-z3 跨距最大，说明 L 形阵列的判断成立，")
    print("    以后测水平就用 z2-z3，测垂直用 z0-z1。")


def save(result):
    os.makedirs(OUT_DIR, exist_ok=True)
    d = os.path.join(OUT_DIR, result["ts"])
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "result.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("\n结果已保存: %s" % os.path.relpath(p, ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--board", default="2t4r", choices=["1t2r", "2t4r"])
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--sec", type=float, default=6.0)
    a = ap.parse_args()
    sys.exit(run(a.port, a.board, a.rounds, a.sec))


if __name__ == "__main__":
    main()
