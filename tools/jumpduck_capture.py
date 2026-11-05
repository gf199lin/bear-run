# -*- coding: utf-8 -*-
"""2T4R 跳 / 蹲 原始帧采集（带真值标注）。

为什么不能用 tools/vertical_probe.py 代替
----------------------------------------
它只存每段的 `pitch_mean / pitch_seq`，**原始帧丢了**。一旦发现两元相位差
不好用，就没法回头换 4 元 DBF、换距离带、换多普勒特征重算 ——
2026-10-29 那 17 轮数据就是这么废掉的。本脚本把每帧的
(4 RX × chirps × bins) 复数据原样存下来。

输出
----
    data/jumpduck/<时间戳>/
        frames.npz     z=(N,4,64,32) complex64, t=(N,), label=(N,)
        labels.json    每段的起止帧号 + 真值 + 采集参数
        pitch.csv      每帧 pitch（rx0-rx1 / rx0-rx3）+ 段标签（便于快速看）

用法
----
    python tools\\jumpduck_capture.py --port COM4 --rounds 3
    python tools\\jumpduck_capture.py --port COM4 --rounds 5 --sec-act 8
    python tools\\jumpduck_capture.py --selftest          # 不碰硬件，检查流程

先决条件
--------
2T4R 需先出流（`radar-pointcloud/radar_source.SerialSource` 会自动唤醒）。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "radar-pointcloud"))

from jumpduck_core import pitch_from_frame                     # noqa: E402
from radar_source import SerialSource, make_protocol, frame_period  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_OUT = os.path.join(ROOT, "data", "jumpduck")
SEGMENTS = [
    ("base1", "站好，不要动", 4.0),
    ("jump", "开始跳，连跳三次，动作放慢", 8.0),
    ("base2", "停，站好不要动", 4.0),
    ("duck", "开始蹲，连续蹲三次，动作放慢", 8.0),
    ("base3", "停，站好不要动", 4.0),
]


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def beep(ok=True):
    try:
        import winsound
        winsound.Beep(880 if ok else 440, 180)
    except Exception:
        pass


NO_SPEAK = False


def _ps_literal(s):
    """纯 ASCII 的 PowerShell 字符串表达式（按码点 join），避免中文编码损坏。"""
    return "(-join @(" + ",".join("[char]%d" % ord(c) for c in s) + "))"


def speak(text):
    """中文语音播报，后台异步，失败静默降级（采集节奏不能因播报中断）。"""
    if NO_SPEAK or not text:
        return False
    try:
        script = ("Add-Type -AssemblyName System.Speech;"
                  "$v=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  "$v.Rate=1;$v.Speak(%s)" % _ps_literal(text))
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive",
                          "-EncodedCommand", enc], **kw)
        return True
    except Exception:
        return False


def short_tick():
    try:
        import winsound
        winsound.Beep(1400, 70)
    except Exception:
        pass


# ------------------------------------------------------------------ 读帧

class FrameReader(object):
    """串口原始流 → 完整 (rx, chirps, bins) 帧。

    chirps 必须与 Radar_Config.ini 的 `ChirpsPerFrame` 一致：
    组帧要求 `4 RX × chirps` 全部填满才出一帧，写死 64 而芯片实际给 16，
    会一条帧都拼不出来（不是丢帧，是永远不完整）。所以这里必须显式传。
    """

    def __init__(self, port, board=None, chirps=None, auto_boot=True):
        # auto_boot=False：调用方已经下发过自己的配置（如 130 ms / 16 bin），
        # 不能让 SerialSource 再重放 64 chirp 的默认序列把它冲掉。
        self.src = SerialSource(port, baud=None, retry_delay=1.0,
                                auto_boot=auto_boot)
        self.src.open()
        self.board = board or getattr(self.src, "board", None) or "2t4r"
        self.chirps = int(chirps) if chirps else None
        self.parser, self.asm = make_protocol(self.board, chirps=self.chirps)
        self.chirps = int(getattr(self.asm, "chirps", 0) or 64)
        self.bad = 0

    def poll(self, timeout=0.05):
        out = []
        chunk = self.src.read(timeout)
        if not chunk:
            return out
        for rf in self.parser.feed(chunk):
            z = self.asm.push(rf)
            if z is None:
                continue
            if isinstance(z, tuple):
                try:
                    z = np.stack([np.asarray(v) for v in z if v is not None], axis=0)
                except ValueError:
                    continue
            z = np.asarray(z)
            if z.ndim == 3:
                out.append(z)
        self.bad = getattr(self.parser, "bad_packets", 0)
        return out

    def close(self):
        try:
            self.src.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 采集

def capture(reader, out_dir, rounds, sec_base, sec_act, band_lo, band_hi):
    os.makedirs(out_dir, exist_ok=True)
    zs, ts, labels = [], [], []
    marks = []
    t0 = time.time()

    for r in range(1, rounds + 1):
        print("\n" + "=" * 62)
        log("第 %d / %d 轮" % (r, rounds))
        print("=" * 62)
        speak("第 %d 轮，共 %d 轮。" % (r, rounds))
        for seg, hint, default_sec in SEGMENTS:
            sec = sec_base if seg.startswith("base") else sec_act
            if default_sec and sec <= 0:
                sec = default_sec
            is_base = seg.startswith("base")
            print("\n  >>> %s" % hint)
            # 先语音播报，等 1.6 s 让人准备；计时从「嘟」开始，
            # 这样语音不占动作窗口，真值窗口就是动作窗口。
            speak(hint)
            time.sleep(1.6)
            beep(is_base)
            time.sleep(0.4)
            f0 = len(zs)
            t_seg = time.time()
            shown = None
            while time.time() - t_seg < sec:
                remain = sec - (time.time() - t_seg)
                if not is_base and remain <= 3.2:
                    n = int(remain) + 1
                    if n != shown and n >= 1:
                        shown = n
                        short_tick()
                for z in reader.poll(0.05):
                    zs.append(z.astype(np.complex64))
                    ts.append(time.time() - t0)
                    labels.append(seg)
            f1 = len(zs)
            beep(not is_base)
            marks.append(dict(round=r, seg=seg, frame_start=f0,
                              frame_end=max(f0, f1 - 1), frames=f1 - f0,
                              t_start=round(t_seg - t0, 2),
                              t_end=round(time.time() - t0, 2)))
            print("      收到 %d 帧（累计 %d）" % (f1 - f0, len(zs)))
            if f1 == f0:
                print("      !! 这一段没有数据，检查串口/板型")

    if not zs:
        log("没有采到任何帧，放弃保存")
        return None

    shapes = {z.shape for z in zs}
    if len(shapes) > 1:
        log("警告：帧形状不一致 %s，按最小公共形状截断" % shapes)
        r = min(s[0] for s in shapes)
        c = min(s[1] for s in shapes)
        b = min(s[2] for s in shapes)
        zs = [z[:r, :c, :b] for z in zs]

    arr = np.stack(zs, axis=0)
    npz = os.path.join(out_dir, "frames.npz")
    np.savez_compressed(npz, z=arr, t=np.asarray(ts, float),
                        label=np.asarray(labels))

    meta = dict(
        created=time.strftime("%Y-%m-%d %H:%M:%S"),
        port=getattr(reader.src, "name", None),
        board=reader.board,
        chirps=getattr(reader, "chirps", None),
        frame_period=frame_period(reader.board, chirps=getattr(reader, "chirps", None)),
        rounds=rounds,
        sec_base=sec_base, sec_act=sec_act,
        band=[band_lo, band_hi],
        frames=int(arr.shape[0]),
        shape=list(arr.shape[1:]),
        bad_packets=getattr(reader, "bad", 0),
        segments=marks,
        note="真值由提示音/段边界给出；frame_start/frame_end 是 npz 里的行号",
    )
    with open(os.path.join(out_dir, "labels.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 顺手导出 pitch，方便肉眼检查
    rows = ["i,t,label,pitch_rx01,pitch_rx03"]
    for i, z in enumerate(zs):
        p1 = pitch_from_frame(z, band_lo, band_hi, 0, 1)
        p3 = pitch_from_frame(z, band_lo, band_hi, 0, min(3, z.shape[0] - 1))
        rows.append("%d,%.3f,%s,%s,%s"
                    % (i, ts[i], labels[i],
                       "" if p1 is None else "%.2f" % p1,
                       "" if p3 is None else "%.2f" % p3))
    with open(os.path.join(out_dir, "pitch.csv"), "w", encoding="utf-8") as f:
        f.write("\n".join(rows))

    log("已保存 %s（%d 帧，%s）"
        % (npz, arr.shape[0], " × ".join(str(v) for v in arr.shape[1:])))
    log("下一步：python tools\\train_jumpduck.py --data %s" % out_dir)
    return out_dir


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--board", default=None, help="1t2r/2t4r；默认跟随串口自动识别")
    ap.add_argument("--chirps", type=int, default=None,
                    help="每帧 chirp 数，必须与 Radar_Config.ini 的 ChirpsPerFrame 一致；"
                         "默认跟随 board profile（2T4R 为 64，可被 RADAR_2T4R_CHIRPS 覆盖）")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--sec-base", type=float, default=4.0)
    ap.add_argument("--sec-act", type=float, default=8.0)
    ap.add_argument("--band", default="6,21", help="俯仰角用的距离门范围")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出根目录，自动建 <时间戳> 子目录")
    ap.add_argument("--selftest", action="store_true", help="不碰硬件，检查流程")
    ap.add_argument("--auto", action="store_true",
                    help="无人值守：不按回车，语音+倒计时自动开始（人站在设备前够不到键盘）")
    ap.add_argument("--no-speak", action="store_true", help="关掉中文语音播报")
    args = ap.parse_args()

    global NO_SPEAK
    NO_SPEAK = bool(args.no_speak)

    try:
        lo, hi = [int(v) for v in args.band.split(",")]
    except Exception:
        log("--band 格式应为 lo,hi")
        return 2

    if args.selftest:
        print("selftest：检查输出目录与数据格式（不连串口）")
        print("  输出根目录：%s" % args.out)
        print("  段：%s" % [s[0] for s in SEGMENTS])
        n_chirp = int(args.chirps) if args.chirps else 64
        print("  chirps=%d（与 ini 的 ChirpsPerFrame 保持一致）" % n_chirp)
        z = np.zeros((4, n_chirp, 32), np.complex64)
        print("  pitch_from_frame 冒烟：%s"
              % pitch_from_frame(z, lo, hi, 0, 1))
        print("  目录可写：%s" % os.access(os.path.dirname(args.out) or ".", os.W_OK))
        return 0

    log("连接雷达 %s ..." % args.port)
    try:
        reader = FrameReader(args.port, args.board, chirps=args.chirps)
    except Exception as exc:
        log("打开串口失败：%s: %s" % (type(exc).__name__, exc))
        log("检查：USB 是否插紧 / 是否被其他程序占用（查 pythonw.exe）")
        return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(args.out, stamp)
    log("板型 %s → 输出 %s" % (reader.board, out_dir))

    print("\n请站到雷达正前方约 1.5–2 m，听提示音做动作。")
    print("每轮 5 段：站好 → 跳三次 → 站好 → 蹲三次 → 站好")
    if args.auto:
        # 人站在雷达前看不到屏幕：语音说明 + 8 秒准备，不用碰键盘。
        speak("跳蹲采集即将开始。请站到雷达正前方 1.5 到 2 米处，站好不要动。")
        for n in (3, 2, 1):
            print("  %d ..." % n, flush=True)
            short_tick()
            time.sleep(1.0)
        beep(True)
    else:
        input("按回车开始 ...")

    try:
        capture(reader, out_dir, args.rounds, args.sec_base, args.sec_act, lo, hi)
    except KeyboardInterrupt:
        log("已中断，未保存（避免存半截数据）")
        return 130
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
