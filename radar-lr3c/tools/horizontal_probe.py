# -*- coding: utf-8 -*-
"""2T4R 横向（水平方位角）引导测试。

用途
----
把 2T4R 板子**横过来**（天线阵列沿水平方向排列）以后，
测它能不能分辨人站在 左 / 中 / 右 三个位置。

这是一个**独立实验脚本**，不修改 tools\\jump_duck_guide.py 等既有文件。
数据单独存到 data\\horizontal\\<时间戳>\\ ，与竖直实验完全分开。

流程（默认 2 轮）
----
站中间 -> 走到左边 -> 回中间 -> 走到右边 -> 回中间 -> 分析

用法
----
    python tools\\horizontal_probe.py                 # 默认 COM4 / 2T4R
    python tools\\horizontal_probe.py --rounds 1
    python tools\\horizontal_probe.py --no-voice
    python tools\\horizontal_probe.py --board 1t2r    # 想看 1T2R 横放的对照

安装要求
----
- 板子横放（长边水平），USB 线从侧面走，别挡天线正面。
- 板子高度约 1.2 m，正对你胸口高度。
- 你站在 1.5 m 处；左/右各横移约 0.6~0.8 m。
- 左右移动走的是**平行于板面**的直线，不要前后走。
"""

import sys
import os
import json
import time
import argparse
import subprocess

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "radar-pointcloud"))

from radar_source import SerialSource, make_protocol   # noqa: E402

try:
    import winsound
except ImportError:
    winsound = None

TTS_DIR = os.path.join(ROOT, ".deepworks", "tmp", "tts_cache")
OUT_DIR = os.path.join(ROOT, "data", "horizontal")

BIN_M = 0.15        # 2T4R 距离分辨率（米 / 门）
BAND_LO = 6         # 0.90 m
BAND_HI = 21        # 3.15 m

VOICE_NAME = "Microsoft Huihui Desktop"

# 与竖直实验的语音分开命名，互不覆盖
LINES = {
    "hz_ready": "请站到雷达正前方一米五的位置，站在中间，站直",
    "hz_center": "好，请保持在中间站好，我在记录你站在中间的位置",
    "hz_left": "请向左横移大约六十厘米，站定不要动",
    "hz_back1": "请回到中间站好",
    "hz_right": "请向右横移大约六十厘米，站定不要动",
    "hz_back2": "请回到中间站好，测试继续",
    "hz_end": "测试结束，正在分析数据",
    "hz_ok": "成功。左中右三个位置可以区分",
    "hz_weak": "有一点区分度，但重叠较多，可以加大横移距离再来一次",
    "hz_same": "失败。三个位置区分不开",
}


# ----------------------------------------------------------------- 语音

def _synth_one(text, out_path):
    jpath = out_path + ".json"
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({"text": text, "out": out_path}, f, ensure_ascii=False)
    script = (
        "$ErrorActionPreference='Stop';"
        "$j=Get-Content -Raw -Encoding UTF8 '%s'|ConvertFrom-Json;"
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try{$s.SelectVoice('%s')}catch{};"
        "$s.SetOutputToWaveFile($j.out);"
        "$s.Speak($j.text);$s.Dispose()"
    ) % (jpath.replace("'", "''"), VOICE_NAME)
    subprocess.run(["powershell", "-NoProfile", "-Command", script],
                   check=True, capture_output=True)
    try:
        os.remove(jpath)
    except OSError:
        pass


def ensure_voice(enabled=True):
    os.makedirs(TTS_DIR, exist_ok=True)
    if not enabled:
        return 0
    made = 0
    for key, text in LINES.items():
        path = os.path.join(TTS_DIR, key + ".wav")
        if os.path.exists(path):
            continue
        try:
            _synth_one(text, path)
            made += 1
        except Exception as exc:
            print("  [语音] 合成失败 %s: %s" % (key, exc))
    return made


def say(key, enabled=True, wait=False):
    if not enabled or winsound is None:
        return
    path = os.path.join(TTS_DIR, key + ".wav")
    if not os.path.exists(path):
        return
    flags = winsound.SND_FILENAME
    if not wait:
        flags |= winsound.SND_ASYNC
    try:
        winsound.PlaySound(path, flags)
    except Exception:
        pass


def beep(ok=True):
    if winsound is None:
        return
    try:
        winsound.Beep(880 if ok else 440, 180)
    except Exception:
        pass


# ----------------------------------------------------------------- 采集

class Recorder:
    def __init__(self, port, board):
        self.src = SerialSource(port, None)
        self.src.open()
        self.parser, self.asm = make_protocol(board)
        self.port = port
        self.board = board

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
                    frames.append(np.stack(z))   # (rx, chirps, bins)
                    if label:
                        bar = int((time.time() - t0) / seconds * 20)
                        print("\r  [%-5s] %s %d 帧" %
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


# ----------------------------------------------------------------- 角度

def azimuth_series(frames, rx_a=0, rx_b=1):
    """每帧水平方位角（度）。

    板子横放时天线沿水平排列，所以同一相位差公式测的就是水平方位角。
    rx_a/rx_b 选基线：
        0-1  单间距（灵敏度 1x，不会卷绕，±45° 视场）
        0-3  三倍间距（灵敏度 3x，但 ±15° 就开始卷绕）
    带内所有距离门按能量加权做圆平均。
    """
    bins = list(range(BAND_LO, BAND_HI))
    out = []
    for z in frames:
        w = (np.abs(z) ** 2).sum(axis=(0, 1))[BAND_LO:BAND_HI].astype(float)
        if w.sum() <= 0:
            continue
        vec = np.empty(len(bins), dtype=complex)
        for i, b in enumerate(bins):
            a = np.angle(z[rx_b, :, b] * np.conj(z[rx_a, :, b]))
            vec[i] = np.exp(1j * a).mean()
        out.append(float(np.degrees(np.angle((vec * w).sum()))))
    return np.array(out)


def unwrap(seq):
    if seq.size == 0:
        return seq
    out = seq.astype(float).copy()
    for i in range(1, out.size):
        d = out[i] - out[i - 1]
        while d > 180.0:
            out[i] -= 360.0
            d -= 360.0
        while d < -180.0:
            out[i] += 360.0
            d += 360.0
    return out


def circ_mean_deg(seq):
    """圆均值（度），避免 ±180 附近的算术均值陷阱。"""
    if seq.size == 0:
        return float("nan")
    return float(np.degrees(np.angle(np.exp(1j * np.radians(seq)).mean())))


def circ_std_deg(seq):
    """圆标准差（度）：sqrt(-2 ln R)，R 为合成向量长度。"""
    if seq.size < 2:
        return float("nan")
    R = float(np.abs(np.exp(1j * np.radians(seq)).mean()))
    R = min(max(R, 1e-9), 1.0)
    return float(np.degrees(np.sqrt(-2.0 * np.log(R))))


def summarize(frames):
    if not frames:
        return None
    s1 = azimuth_series(frames, 0, 1)
    s1 = s1[np.isfinite(s1)]
    if s1.size == 0:
        return None
    s3 = unwrap(azimuth_series(frames, 0, 3))
    s3 = s3[np.isfinite(s3)]

    d = {
        "n": int(len(frames)),
        "az_mean": round(circ_mean_deg(s1), 2),
        "az_std": round(circ_std_deg(s1), 2),
        "az_seq": [round(float(v), 2) for v in s1],
    }
    if s3.size:
        # 长基线是 3 倍间距，除以 3 折算回单间距角度便于对照
        d["az3_mean"] = round(circ_mean_deg(s3) / 3.0, 2)
        d["az3_std"] = round(circ_std_deg(s3) / 3.0, 2)
        d["az3_seq"] = [round(float(v), 2) for v in s3]
    return d


# ----------------------------------------------------------------- 主流程

def run(port, board, rounds, voice, sec_base, sec_act):
    print("=" * 66)
    print("  2T4R 横向（水平方位角）测试     板子=%s  端口=%s"
          % (board.upper(), port))
    print("=" * 66)
    print("  注意：板子要横放，天线阵列沿水平方向。")

    made = ensure_voice(voice)
    if voice:
        print("语音就绪（本次新合成 %d 句）" % made)

    print("\n连接雷达 ...")
    try:
        rec = Recorder(port, board)
    except Exception as exc:
        print("连接失败: %s" % exc)
        print("检查：USB 是否插紧 / 串口是否被其他程序占用。")
        return 3

    result = {"port": port, "board": board, "mount": "horizontal",
              "ts": time.strftime("%Y%m%d-%H%M%S"), "rounds": []}

    try:
        for r in range(1, rounds + 1):
            print("\n---------- 第 %d / %d 轮 ----------" % (r, rounds))
            one = {}

            if r == 1:
                say("hz_ready", voice)
                print("  语音：站到 1.5 m 处，站在中间")
                time.sleep(6)
            else:
                say("hz_ready", voice)
                time.sleep(4)

            say("hz_center", voice)
            one["center1"] = summarize(rec.grab(sec_base, "中1"))
            print("  中1   %s" % fmt(one["center1"]))

            say("hz_left", voice)
            beep(True)
            print("  语音：向左横移 60 cm")
            one["left"] = summarize(rec.grab(sec_act, "左"))
            print("  左    %s" % fmt(one["left"]))

            say("hz_back1", voice)
            one["center2"] = summarize(rec.grab(sec_base, "中2"))
            print("  中2   %s" % fmt(one["center2"]))

            say("hz_right", voice)
            beep(True)
            print("  语音：向右横移 60 cm")
            one["right"] = summarize(rec.grab(sec_act, "右"))
            print("  右    %s" % fmt(one["right"]))

            say("hz_back2", voice)
            one["center3"] = summarize(rec.grab(sec_base, "中3"))
            print("  中3   %s" % fmt(one["center3"]))

            result["rounds"].append(one)

    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        rec.close()

    say("hz_end", voice)
    print("\n" + "=" * 66)
    result["verdict"] = "pending"
    save(result)
    verdict = report(result)
    result["verdict"] = verdict
    save(result)

    if verdict == "ok":
        say("hz_ok", voice)
        beep(True)
    elif verdict == "weak":
        say("hz_weak", voice)
        beep(True)
    else:
        say("hz_same", voice)
        beep(False)

    return 0 if verdict == "ok" else 1


def fmt(s):
    if not s:
        return "无数据"
    extra = ""
    if "az3_mean" in s:
        extra = "  长基线折算 %+6.1f°" % s["az3_mean"]
    return "方位 %+7.1f° (波动 %.1f°)  %d帧%s" % (
        s["az_mean"], s["az_std"], s["n"], extra)


def report(result):
    """看三件事：段内稳不稳、三段之间分不分得开、顺序对不对。"""
    rows = []
    for i, one in enumerate(result["rounds"], 1):
        cs = [one.get(k) for k in ("center1", "center2", "center3")]
        cs = [c for c in cs if c]
        l = one.get("left")
        r = one.get("right")
        if not cs or not l or not r:
            continue
        c_mean = circ_mean_deg(np.array([c["az_mean"] for c in cs], dtype=float))
        c_std = float(np.mean([c["az_std"] for c in cs]))
        rows.append({
            "round": i,
            "L": l["az_mean"], "Lstd": l["az_std"], "L3": l.get("az3_mean"),
            "C": c_mean, "Cstd": c_std,
            "R": r["az_mean"], "Rstd": r["az_std"], "R3": r.get("az3_mean"),
            "Lseq": l.get("az_seq", []), "Cseq": cs[0].get("az_seq", []),
            "Rseq": r.get("az_seq", []),
        })

    if not rows:
        print("  数据不足，无法分析。")
        return "no_signal"

    print("  【左中右可分性】")
    print("  " + "-" * 62)
    print("    %-4s %-16s %-16s %-16s" % ("轮", "左", "中", "右"))
    for x in rows:
        print("    %-4d %+7.1f ±%-6.1f %+7.1f ±%-6.1f %+7.1f ±%-6.1f"
              % (x["round"], x["L"], x["Lstd"],
                 x["C"], x["Cstd"], x["R"], x["Rstd"]))

    # 每轮检查：顺序 + 跨度 + 重叠
    print("\n  【逐轮判定】")
    print("  " + "-" * 62)
    good_rows = 0
    for x in rows:
        L, C, R = x["L"], x["C"], x["R"]
        spread_lc = C - L
        spread_cr = R - C
        # 用段内波动做“重叠”粗判：相邻两段的均值差要大于两段各自波动的和
        sep_lc = abs(spread_lc) - (x["Lstd"] + x["Cstd"])
        sep_cr = abs(spread_cr) - (x["Cstd"] + x["Rstd"])
        mono = (L < C < R) or (L > C > R)
        ok = mono and sep_lc > 0 and sep_cr > 0
        if ok:
            good_rows += 1
        print("    第%d轮  左%+6.1f 中%+6.1f 右%+6.1f | 左中 %+6.1f (净分离%+5.1f) "
              "中右 %+6.1f (净分离%+5.1f) | %s"
              % (x["round"], L, C, R, spread_lc, sep_lc, spread_cr, sep_cr,
                 "顺序正确" if mono else "顺序错乱"))

    # 全局统计
    Ls = np.array([x["L"] for x in rows], dtype=float)
    Cs = np.array([x["C"] for x in rows], dtype=float)
    Rs = np.array([x["R"] for x in rows], dtype=float)
    mL, mC, mR = float(Ls.mean()), float(Cs.mean()), float(Rs.mean())
    span = max(mL, mC, mR) - min(mL, mC, mR)
    inner_std = float(np.mean([x["Lstd"] for x in rows]
                              + [x["Cstd"] for x in rows]
                              + [x["Rstd"] for x in rows]))

    print("  " + "-" * 62)
    print("    均值   左 %+6.1f   中 %+6.1f   右 %+6.1f" % (mL, mC, mR))
    print("    跨距 %.1f°   段内平均波动 %.1f°   跨度/波动 = %.2f"
          % (span, inner_std, span / inner_std if inner_std > 0 else 0))
    print("    合格轮次 %d / %d" % (good_rows, len(rows)))

    print("\n  " + "=" * 62)
    ratio = span / inner_std if inner_std > 0 else 0
    if good_rows == len(rows) and ratio >= 2.0 and span >= 12:
        print("  结论：可用。左中右分得开（跨距 %.1f°，跨度/波动 %.2f）。" % (span, ratio))
        return "ok"
    if ratio >= 1.2 and span >= 8:
        print("  结论：有区分度但不够稳（跨距 %.1f°，跨度/波动 %.2f）。"
              % (span, ratio))
        print("        建议：加大左右横移距离，或把板子再放远一点。")
        return "weak"
    print("  结论：分不开（跨距 %.1f°，跨度/波动 %.2f）。" % (span, ratio))
    print("        可能原因：横移距离太小、板子没横放、或视场角覆盖不足。")
    return "same"


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
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--base", type=float, default=6.0, help="每段中间位秒数")
    ap.add_argument("--act", type=float, default=10.0, help="每段左右位秒数")
    a = ap.parse_args()
    sys.exit(run(a.port, a.board, a.rounds, not a.no_voice, a.base, a.act))


if __name__ == "__main__":
    main()
