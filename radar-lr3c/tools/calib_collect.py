# -*- coding: utf-8 -*-
"""双板标定数据采集（带真值标注 + 语音引导）

几何约定：
  1T2R 水平放置 —— 水平方位角 ≈ 现实左右
  2T4R 竖直放置 —— 俯仰角 ≈ 现实上下（跳/蹲）；
                   其"纵向空间"旋转后对应现实左右，可用于辅助 1T2R

站位循环（关键改进）：
  每一轮都是 左移 -> 回中 -> 右移 -> 回中(不记录)，
  这样每次"左移/右移"都从正中间出发，不会出现"人已经在左边还要再往左跨"。

语音：
  启动时把提示语一次性合成为 wav 缓存（.deepworks/tmp/tts_cache/），
  播放时零延迟，不会打乱录音时序。没有中文语音时自动退化为蜂鸣。

用法：
  python tools\\calib_collect.py                  # 4 轮，每轮 6 段
  python tools\\calib_collect.py --reps 3
  python tools\\calib_collect.py --no-voice
  python tools\\calib_collect.py --list
"""
import os
import sys
import json
import time
import base64
import argparse
import threading
import subprocess
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "radar-pointcloud"))

from serial.tools import list_ports
from radar_source import SerialSource, make_protocol

CREATE_NO_WINDOW = 0x08000000

try:
    import winsound
    _HAS_SOUND = True
except Exception:
    _HAS_SOUND = False


def beep(freq=800, ms=120, n=1):
    if not _HAS_SOUND:
        return
    for i in range(n):
        try:
            winsound.Beep(freq, ms)
        except Exception:
            pass
        if i + 1 < n:
            time.sleep(0.09)


# ---------------------------------------------------------------- 语音合成

TTS_CACHE = os.path.join(ROOT, ".deepworks", "tmp", "tts_cache")
TTS_VOICE = "Microsoft Huihui Desktop"


def _ps_run(script):
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                          capture_output=True, timeout=25)


def synth_one(text, tag):
    """把一句中文合成为 wav 并缓存。失败返回 None。"""
    path = os.path.join(TTS_CACHE, tag + ".wav")
    if os.path.isfile(path) and os.path.getsize(path) > 2000:
        return path
    os.makedirs(TTS_CACHE, exist_ok=True)
    safe_text = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { $s.SelectVoice('%s') } catch { }; "
        "$s.Rate = 0; "
        "$s.SetOutputToWaveFile('%s'); "
        "$s.Speak('%s'); "
        "$s.Dispose()" % (TTS_VOICE, path, safe_text)
    )
    try:
        _ps_run(script)
    except Exception:
        return None
    return path if os.path.isfile(path) and os.path.getsize(path) > 2000 else None


def say(tag):
    """播放已缓存的语音，异步、零延迟。"""
    if not _HAS_SOUND:
        return False
    path = os.path.join(TTS_CACHE, tag + ".wav")
    if not os.path.isfile(path):
        return False
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 动作定义

# (label, 名称, 提示语, 是否记录, 录音时长)
# 跳/蹲给 10 秒：2T4R 只有 1.9 Hz，5 秒仅 9 帧，动作根本采不到几个点。
PLAN = [
    ("IDLE", "静止站立",
     "站在正中间，双臂自然下垂，保持不动", True, 5.0),
    ("LANE_LEFT", "左移",
     "从中间向左横跨一步，然后站定不动", True, 5.0),
    ("LANE_CENTER", "回中",
     "回到正中间，站定不动", True, 5.0),
    ("LANE_RIGHT", "右移",
     "从中间向右横跨一步，然后站定不动", True, 5.0),
    ("LANE_RETURN", "回中（准备）",
     "回到正中间，准备下一轮", False, 2.0),
    ("JUMP", "跳",
     "原地起跳三次，动作放慢，每次落地站稳", True, 10.0),
    ("DUCK", "蹲",
     "下蹲三次，动作放慢，每次起身站稳", True, 10.0),
]

TAG = {
    "IDLE": "idle",
    "LANE_LEFT": "left",
    "LANE_CENTER": "center",
    "LANE_RIGHT": "right",
    "LANE_RETURN": "return",
    "JUMP": "jump",
    "DUCK": "duck",
}


def prepare_voice():
    """预先合成所有提示语 + 开始/结束。"""
    if not _HAS_SOUND:
        print("  （无 winsound，跳过语音）")
        return {}
    print("  正在准备语音（首次约 10 秒，之后走缓存）...")
    made = {}
    for label, name, hint, _, _sec in PLAN:
        tag = TAG[label]
        p = synth_one(hint, tag)
        made[tag] = p
    made["start"] = synth_one("开始", "start")
    made["finish"] = synth_one("这一段结束", "finish")
    ok = sum(1 for v in made.values() if v)
    print("  语音就绪：%d/%d 句" % (ok, len(made)))
    if ok == 0:
        print("  !! 语音合成失败，将只用蜂鸣 + 屏幕提示")
    return made


# ---------------------------------------------------------------- 端口识别


def scan_ports():
    out = []
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        vp = ""
        if "VID:PID=" in hwid:
            vp = hwid.split("VID:PID=")[1][:9].upper()
        board = None
        if vp.startswith("10C4:EA60"):
            board = "1t2r"
        elif vp.startswith("0403:6001"):
            board = "2t4r"
        out.append((p.device, vp, board, (p.description or "")[:40]))
    return out


# ---------------------------------------------------------------- 后台录制


class Recorder:
    def __init__(self, port, board):
        self.port = port
        self.board = board
        self.frames = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.th = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.th.start()
        return self

    def _run(self):
        try:
            src = SerialSource(self.port, None)
            src.open()
            parser, asm = make_protocol(self.board)
            while not self.stop.is_set():
                try:
                    chunk = src.read(0.05)
                except Exception:
                    time.sleep(0.05)
                    continue
                if not chunk:
                    continue
                for rf in parser.feed(chunk):
                    zs = asm.push(rf)
                    if zs is None:
                        continue
                    t = time.monotonic()
                    with self.lock:
                        self.frames.append((t, zs))
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def window(self, t0, t1):
        with self.lock:
            return [x for x in self.frames if t0 <= x[0] <= t1]

    def close(self):
        self.stop.set()
        self.th.join(timeout=3)


def stack_frames(items, ch):
    """(n, chirps, bins, ch) complex64 + 时间数组。

    雷达给的是 IQ 复数，必须按复数保存，否则虚部丢失、角度信息全毁。
    """
    if not items:
        return None, None
    ts = np.array([t for t, _ in items], dtype=np.float64)
    arrs = []
    for _, zs in items:
        z = np.stack([np.asarray(c, dtype=np.complex64) for c in zs[:ch]], axis=-1)
        arrs.append(z)
    try:
        a = np.stack(arrs).astype(np.complex64)
    except ValueError:
        return None, None
    return a, ts


# ---------------------------------------------------------------- 主流程


def main():
    ap = argparse.ArgumentParser(description="双板标定数据采集（语音引导）")
    ap.add_argument("--reps", type=int, default=4, help="重复轮数")
    ap.add_argument("--seconds", type=float, default=5.0, help="每段录音时长")
    ap.add_argument("--prepare", type=float, default=3.0, help="就位时间")
    ap.add_argument("--no-voice", action="store_true", help="关闭语音")
    ap.add_argument("--list", action="store_true", help="只列出串口")
    args = ap.parse_args()

    ports = scan_ports()
    print("=" * 70)
    print("  双板标定数据采集")
    print("=" * 70)
    print("  串口：")
    for d, vp, b, desc in ports:
        print("    %-6s %-10s %-6s %s" % (d, vp or "?", b or "未知", desc))

    if args.list:
        return 0

    p1 = next((d for d, vp, b, _ in ports if b == "1t2r"), None)
    p2 = next((d for d, vp, b, _ in ports if b == "2t4r"), None)
    if not p1:
        print("\n  !! 没找到 1T2R（CP2102）")
        return 2
    if not p2:
        print("\n  !! 没找到 2T4R（FTDI）—— 跳/蹲数据会缺失")

    n_rec = sum(1 for x in PLAN if x[3])
    total_sec = args.reps * (len(PLAN) * args.prepare
                            + sum(x[4] for x in PLAN) + len(PLAN) * 0.6)
    print("\n  几何：1T2R 水平（测左右） / 2T4R 竖直（测上下 + 辅助左右）")
    print("  站位：雷达正前方约 2 m，横向跨步 0.5~0.8 m")
    print("  循环：每轮 %d 段（含 1 段不记录的「回中」），共 %d 轮"
          % (len(PLAN), args.reps))
    print("  预计：约 %.1f 分钟" % (total_sec / 60))

    if not args.no_voice:
        prepare_voice()
    else:
        print("  语音：已关闭")

    print("\n  按回车开始（之后全程不用碰键盘）...")
    try:
        input()
    except EOFError:
        pass

    rec1 = Recorder(p1, "1t2r").start()
    rec2 = Recorder(p2, "2t4r").start() if p2 else None
    time.sleep(2.2)

    # 关键自检：串口被占用时底层是"静默重试"，不会抛异常，
    # 必须靠"2 秒内到底有没有帧"来判断，否则会录出一份全 0 的数据还显示成功。
    problems = []
    if len(rec1.frames) == 0:
        problems.append("1T2R(%s)" % p1)
    if rec2 is not None and len(rec2.frames) == 0:
        problems.append("2T4R(%s)" % p2)
    if problems:
        print("\n  !! 以下串口 2 秒内没有任何数据：%s" % "、".join(problems))
        print("     最常见原因：该串口被其它程序占用（厂商工具 / radar_live_ws.py 等）。")
        print("     Windows 串口是独占的，两个程序不能同时读同一个口。")
        print("     请先关闭占用程序，再重新运行。")
        rec1.close()
        if rec2 is not None:
            rec2.close()
        return 3

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(ROOT, "data", "calibration", stamp)
    os.makedirs(out_dir, exist_ok=True)

    samples = []
    idx = 0
    try:
        for rnd in range(1, args.reps + 1):
            print("\n" + "=" * 70)
            print("  第 %d / %d 轮" % (rnd, args.reps))
            print("=" * 70)
            for label, name, hint, save, sec in PLAN:
                tag = TAG[label]
                print("\n" + "-" * 70)
                print("  %s — %s" % (name, label))
                print("  %s" % hint)
                say(tag) if not args.no_voice else None
                beep(600, 130, 1)

                for left in range(int(round(args.prepare)), 0, -1):
                    print("\r    准备 %d " % left, end="", flush=True)
                    time.sleep(1)
                print("\r" + " " * 18, end="")

                if not save:
                    print("    （本段不记录，只调整站位）")
                    continue

                idx += 1
                beep(1000, 110, 2)
                if not args.no_voice:
                    say("start")
                t0 = time.monotonic()
                print("  >>> 录音 %.0f 秒" % sec, end="", flush=True)
                for _ in range(int(sec * 10)):
                    time.sleep(0.1)
                t1 = time.monotonic()
                beep(400, 380, 1)

                a1, ts1 = stack_frames(rec1.window(t0, t1), 2)
                a2, ts2 = (stack_frames(rec2.window(t0, t1), 4)
                           if rec2 is not None else (None, None))

                n1 = 0 if a1 is None else a1.shape[0]
                n2 = 0 if a2 is None else a2.shape[0]
                warn = "" if (n2 >= 3 or rec2 is None) else "  << 2T4R 偏少"
                print("\r    已录：1T2R %d 帧 / 2T4R %d 帧%s" % (n1, n2, warn))

                npz = os.path.join(out_dir, "s%02d_%s_r%d.npz" % (idx, label, rnd))
                payload = {"label": label, "rep": rnd, "seconds": sec}
                if a1 is not None:
                    payload["z1"] = a1
                    payload["t1"] = ts1 - t0
                if a2 is not None:
                    payload["z2"] = a2
                    payload["t2"] = ts2 - t0
                np.savez_compressed(npz, **payload)

                samples.append({
                    "idx": idx, "label": label, "name": name, "rep": rnd,
                    "file": os.path.basename(npz),
                    "n_1t2r": n1, "n_2t4r": n2,
                    "t_start": round(t0, 3), "t_end": round(t1, 3),
                })
                time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n\n  手动中断，已保存的部分仍然有效。")
    finally:
        rec1.close()
        if rec2 is not None:
            rec2.close()

    meta = {
        "session": stamp,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "geometry": {"1t2r": "horizontal(左右)", "2t4r": "vertical(上下+辅助左右)"},
        "ports": {"1t2r": p1, "2t4r": p2},
        "reps": args.reps,
        "seconds_per_sample": args.seconds,
        "plan": [{"label": l, "name": n, "hint": h, "recorded": r, "seconds": sec}
                 for l, n, h, r, sec in PLAN],
        "samples": samples,
        "notes": [
            "z1: (n,64,8,2) complex64, 1T2R 两通道 IQ",
            "z2: (n,64,32,4) complex64, 2T4R 四通道 IQ",
            "bins 轴是距离门，chirps 轴含多普勒",
            "t1/t2: 相对本次录音开始的秒数",
            "站位循环：左移 -> 回中 -> 右移 -> 回中(不记录)，保证每次动作从中性位出发",
        ],
        "errors": {"1t2r": rec1.error,
                   "2t4r": None if rec2 is None else rec2.error},
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    print("  采集完成")
    print("=" * 70)
    print("  会话目录：%s" % os.path.relpath(out_dir, ROOT))
    print("  样本数  ：%d（每轮 %d 段，共 %d 轮）" % (len(samples), n_rec, args.reps))
    print("  总帧数  ：1T2R %d / 2T4R %d"
          % (sum(s["n_1t2r"] for s in samples), sum(s["n_2t4r"] for s in samples)))
    if rec1.error or (rec2 is not None and rec2.error):
        print("  !! 异常  ：1T2R=%s  2T4R=%s"
              % (rec1.error, None if rec2 is None else rec2.error))
    print("\n  下一步：说一声，我来分析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
