# -*- coding: utf-8 -*-
"""跳 / 蹲 俯仰角引导测试（带语音）。

流程：站好 -> 跳三次 -> 站好 -> 蹲三次 -> 站好 -> 分析。

用法：
    python tools\\jump_duck_guide.py            # 默认 COM4 / 2T4R
    python tools\\jump_duck_guide.py --port COM4 --rounds 1
    python tools\\jump_duck_guide.py --no-voice  # 不出声，只看打印

注意：
- 2T4R 要装到约 1.2 m 高，水平正对你。
- 你站在离它约 1.5 m 处，背后留 0.7 m 以上到墙，
  这样你和墙落在不同距离门。
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
except ImportError:                                    # 非 Windows 兜底
    winsound = None

TTS_DIR = os.path.join(ROOT, ".deepworks", "tmp", "tts_cache")
OUT_DIR = os.path.join(ROOT, "data", "vertical")

BIN_M = 0.15        # 2T4R 距离分辨率（米 / 门）
BAND_LO = 6         # 0.90 m
BAND_HI = 21        # 3.15 m

VOICE_NAME = "Microsoft Huihui Desktop"

LINES = {
    "jd_ready": "请站到雷达正前方一米五的位置，站直，不要走动",
    "jd_hold":  "好，请保持站好，我在记录你站直的样子",
    "jd_jump":  "准备跳。连续跳三次，每次跳起来以后站稳",
    "jd_rest":  "很好。请重新站好，休息一下",
    "jd_duck":  "准备蹲。连续蹲三次，每次蹲到底再站起来",
    "jd_end":   "测试结束，正在分析数据",
    "jd_ok":    "成功。跳和蹲可以区分",
    "jd_weak":  "有一点效果，但差距偏小，建议加大动作再来一次",
    "jd_same":  "失败。跳和蹲区分不开",
}


# ----------------------------------------------------------------- 语音

def _synth_one(text, out_path):
    """用 PowerShell 的 System.Speech 合成一句 wav。"""
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


# ----------------------------------------------------------------- 分析

def pitch_series(frames, rx_a=0, rx_b=1):
    """每帧俯仰相位差（度）。

    rx_a/rx_b 选基线：
        0-1 单间距（灵敏度 1x，不会卷绕）
        0-3 三倍间距（灵敏度 3x，角度大时会卷绕，需 unwrap）
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


def pitch_unwrap(seq):
    """解卷绕：把相邻帧跳变超过 180 度的拉回连续。"""
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


def profile_mean(frames):
    acc = None
    n = 0
    for z in frames:
        p = (np.abs(z) ** 2).sum(axis=(0, 1))     # (rx, chirps) -> (bins,)
        t = p.sum()
        if t <= 0:
            continue
        p = p / t
        acc = p if acc is None else acc + p
        n += 1
    return acc / n if n else None


def summarize(frames):
    if not frames:
        return None
    ph = pitch_series(frames, 0, 1)
    pl = pitch_unwrap(pitch_series(frames, 0, 3))
    prof = profile_mean(frames)
    if ph.size == 0:
        return None
    d = {
        "n": int(len(frames)),
        "pitch_mean": round(float(ph.mean()), 2),
        "pitch_std": round(float(ph.std()), 2),
        "pitch_seq": [round(float(v), 2) for v in ph],
    }
    if pl.size:
        d["pitchL"] = [round(float(v), 2) for v in pl]
        d["pitchL_mean"] = round(float(pl.mean()), 2)
    if prof is not None:
        body = prof[BAND_LO:BAND_HI]
        tot = body.sum()
        d["peak_bin"] = int(np.argmax(body)) + BAND_LO
        d["prof"] = [round(float(v), 6) for v in prof]
        if tot > 0:
            bins = np.arange(BAND_LO, BAND_HI, dtype=float)
            d["dist_cen"] = round(float((body * bins).sum() / tot), 3)
            d["dist_cen_m"] = round(d["dist_cen"] * BIN_M, 3)
    return d


# ----------------------------------------------------------------- 主流程

def run(port, board, rounds, voice, sec_base, sec_act):
    print("=" * 62)
    print("  跳 / 蹲  俯仰角引导测试     板子=%s  端口=%s" % (board.upper(), port))
    print("=" * 62)

    made = ensure_voice(voice)
    if voice:
        print("语音就绪（本次新合成 %d 句）" % made)

    print("\n连接雷达 ...")
    try:
        rec = Recorder(port, board)
    except Exception as exc:
        print("连接失败: %s" % exc)
        print("检查：USB 是否插紧 / 串口是否被其他程序占用（查 pythonw.exe）。")
        return 3

    result = {"port": port, "board": board, "ts": time.strftime("%Y%m%d-%H%M%S"),
              "rounds": []}

    try:
        for r in range(1, rounds + 1):
            print("\n---------- 第 %d / %d 轮 ----------" % (r, rounds))
            one = {}

            if r == 1:
                say("jd_ready", voice)
                print("  语音：站到 1.5 m 处，站直")
                time.sleep(5)
            else:
                say("jd_ready", voice)
                time.sleep(4)

            say("jd_hold", voice)
            one["base1"] = summarize(rec.grab(sec_base, "基线1"))
            print("  基线1  %s" % _fmt(one["base1"]))

            say("jd_jump", voice)
            beep(True)
            print("  语音：连续跳三次")
            one["jump"] = summarize(rec.grab(sec_act, "跳"))
            print("  跳     %s" % _fmt(one["jump"]))

            say("jd_rest", voice)
            one["base2"] = summarize(rec.grab(sec_base, "基线2"))
            print("  基线2  %s" % _fmt(one["base2"]))

            say("jd_duck", voice)
            beep(True)
            print("  语音：连续蹲三次")
            one["duck"] = summarize(rec.grab(sec_act, "蹲"))
            print("  蹲     %s" % _fmt(one["duck"]))

            one["base3"] = summarize(rec.grab(sec_base, "基线3"))
            print("  基线3  %s" % _fmt(one["base3"]))

            result["rounds"].append(one)

    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        rec.close()

    say("jd_end", voice)
    print("\n" + "=" * 62)
    # 先把原始数据落盘，再打印结论：即使报告部分出错也不会丢数据
    result["verdict"] = "pending"
    save(result)
    verdict = report(result)
    result["verdict"] = verdict
    save(result)

    if verdict == "ok":
        say("jd_ok", voice)
        beep(True)
    elif verdict == "weak":
        say("jd_weak", voice)
        beep(True)
    else:
        say("jd_same", voice)
        beep(False)

    return 0 if verdict == "ok" else 1


def _fmt(s):
    if not s:
        return "无数据"
    extra = ""
    if "peak_bin" in s:
        extra = "  峰门%d(%.2fm)" % (s["peak_bin"], s["peak_bin"] * BIN_M)
    return "俯仰 %+7.1f° (波动 %.1f°)  %d帧%s" % (
        s["pitch_mean"], s["pitch_std"], s["n"], extra)


def report(result):
    b, j, d = [], [], []
    for one in result["rounds"]:
        for k in ("base1", "base2", "base3"):
            if one.get(k):
                b.append(one[k]["pitch_mean"])
        if one.get("jump"):
            j.append(one["jump"]["pitch_mean"])
        if one.get("duck"):
            d.append(one["duck"]["pitch_mean"])

    print("  【极值偏离判据】长基线 0-3（灵敏度 3x）")
    print("  " + "-" * 56)

    def pseq(o, k, key="pitchL"):
        s2 = o.get(k)
        if not s2 or key not in s2 or len(s2[key]) < 3:
            # 回退到短基线
            if key == "pitchL" and s2 and "pitch_seq" in s2 and len(s2["pitch_seq"]) >= 3:
                return np.array(s2["pitch_seq"], dtype=float)
            return None
        return np.array(s2[key], dtype=float)

    jdev, ddev = [], []
    for i, o in enumerate(result["rounds"], 1):
        bs = [pseq(o, k) for k in ("base1", "base2", "base3")]
        bs = [x for x in bs if x is not None]
        j, dk = pseq(o, "jump"), pseq(o, "duck")
        if not bs or j is None or dk is None:
            continue
        base = float(np.median(np.concatenate(bs)))
        jd = float(j.max() - base)
        dd = float(dk.min() - base)
        jdev.append(jd)
        ddev.append(dd)
        print("    第%d轮  跳偏离 %+7.1f  蹲偏离 %+7.1f  差 %+7.1f"
              % (i, jd, dd, jd - dd))

    if not jdev:
        print("    数据不足。")
        return "no_signal"

    mj, md = float(np.mean(jdev)), float(np.mean(ddev))
    print("  " + "-" * 56)
    print("    均值  跳 %+7.1f   蹲 %+7.1f   差 %+7.1f" % (mj, md, mj - md))

    print("\n  " + "=" * 56)
    if mj >= 20 and md <= -10:
        print("  结论：成功。跳上抬 %+.1f°，蹲下沉 %+.1f°。" % (mj, md))
        return "ok"
    if mj >= 10 and md <= -5:
        print("  结论：方向正确但幅度偏小（跳 %+.1f°，蹲 %+.1f°）。" % (mj, md))
        return "weak"
    if mj <= 5 and md >= -5:
        print("  结论：跳和蹲都没让仰角明显变化，区分不开。")
        return "same"
    print("  结论：方向不一致（跳 %+.1f°，蹲 %+.1f°），数据可疑。" % (mj, md))
    return "reversed"


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
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--base", type=float, default=6.0, help="每段基线秒数")
    ap.add_argument("--act", type=float, default=12.0, help="每段动作秒数")
    a = ap.parse_args()
    sys.exit(run(a.port, a.board, a.rounds, not a.no_voice, a.base, a.act))


if __name__ == "__main__":
    main()
