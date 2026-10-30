# -*- coding: utf-8 -*-
"""左右横移检测：全程语音引导 + 4 种通道对同时记录。

为什么重做
----------
上一版 tools\\rx_pair_probe.py 只有蜂鸣提示，人分不清该往哪边走。
这一版每段都用中文语音明确报「你的左手边 / 你的右手边」，
并且语音播完才开始计时采集，避免把走过去的过程算进站立段。

同时保留 4 种通道对的对照（z0-z1 / z0-z2 / z0-z3 / z2-z3），
因为 L 形阵列下哪一对对应水平方向还没有最终确定。

流程（默认 2 轮）
----------------
站中间 -> 语音「向你的左边横移」-> 走到左边站定
       -> 语音「回到中间」      -> 回中间站定
       -> 语音「向你的右边横移」-> 走到右边站定
       -> 语音「回到中间」      -> 回中间站定

用法
----
    python tools\\horizontal_guide.py
    python tools\\horizontal_guide.py --rounds 1
    python tools\\horizontal_guide.py --no-voice     # 无声，只看打印

站位
----
- 板子横放，天线正面朝你，高度约 1.2 m。
- 站在正前方 1.5 m。
- 「左边」指你自己的左手边，与板面平行横移 60~80 cm。
- 不要前后走，不要转身。
"""

import sys
import os
import json
import time
import argparse
import subprocess

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

TTS_DIR = os.path.join(ROOT, ".deepworks", "tmp", "tts_cache")
# 采集结果不入库（.gitignore 已忽略 radar-service/captures/）
OUT_DIR = os.path.join(SERVICE, "captures", "horizontal_guide")

BIN_M = 0.15
BAND_LO = 6
BAND_HI = 21

VOICE_NAME = "Microsoft Huihui Desktop"

PAIRS = [
    ("z0-z1", 0, 1),
    ("z0-z2", 0, 2),
    ("z0-z3", 0, 3),
    ("z2-z3", 2, 3),
]

LINES = {
    "hg_start":
        "测试开始。请面向雷达，站在正中间，双脚并拢站直，不要动。",
    "hg_center_ok":
        "好，就站在这个位置。现在记录你站在中间的角度。",
    "hg_to_left":
        "现在请向你的左手边横移，大约走六十厘米，和雷达保持平行，不要前后走。走好以后站定，双手自然下垂。",
    "hg_left_ok":
        "好，站住别动。现在记录你站在左边的角度。",
    "hg_back":
        "请走回正中间，回到刚才那个位置站好。",
    "hg_to_right":
        "现在请向你的右手边横移，大约走六十厘米，同样和雷达保持平行，不要前后走。走好以后站定，双手自然下垂。",
    "hg_right_ok":
        "好，站住别动。现在记录你站在右边的角度。",
    "hg_round":
        "这一轮结束。",
    "hg_end":
        "全部测试结束，正在分析数据。",
    "hg_ok":
        "成功。左中右三个位置分得开。",
    "hg_weak":
        "有区分度，但还不够稳定。可以加大横移距离再来一次。",
    "hg_same":
        "三个位置区分不开。",
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
        "$s.Rate=0;"
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
            print("  [语音] 新合成 %s" % key)
        except Exception as exc:
            print("  [语音] 合成失败 %s: %s" % (key, exc))
    return made


def speak(key, enabled=True):
    """同步播放：必须等这句话说完再开始采集。"""
    if not enabled or winsound is None:
        return
    path = os.path.join(TTS_DIR, key + ".wav")
    if not os.path.exists(path):
        return
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME)   # 阻塞直到播完
    except Exception:
        pass


def beep(freq=880, ms=180):
    if winsound is None:
        return
    try:
        winsound.Beep(freq, ms)
    except Exception:
        pass


# ----------------------------------------------------------------- 采集

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
                        print("\r  [%-3s] %s %d 帧" %
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

def angle_series(frames, a, b):
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


# ----------------------------------------------------------------- 主流程

def run(port, board, rounds, voice, sec_stand):
    print("=" * 70)
    print("  左右横移语音引导测试     板子=%s  端口=%s" % (board.upper(), port))
    print("=" * 70)

    made = ensure_voice(voice)
    if voice:
        print("语音就绪（新合成 %d 句）\n" % made)

    print("连接雷达 ...")
    try:
        rec = Recorder(port, board)
    except Exception as exc:
        print("连接失败: %s" % exc)
        print("检查：ONELAB 是否已关闭并让出 COM4。")
        return 3

    result = {"port": port, "board": board, "mount": "horizontal",
              "ts": time.strftime("%Y%m%d-%H%M%S"), "rounds": []}

    try:
        for r in range(1, rounds + 1):
            print("\n---------- 第 %d / %d 轮 ----------" % (r, rounds))
            one = {}

            speak("hg_start", voice)
            time.sleep(1.0)
            speak("hg_center_ok", voice)
            print("  采集：中间")
            one["center1"] = summarize(rec.grab(sec_stand, "中"))
            show("中", one["center1"])

            speak("hg_to_left", voice)
            beep(520, 250)
            time.sleep(2.0)          # 给你走的缓冲，不计入采集
            speak("hg_left_ok", voice)
            print("  采集：左边")
            one["left"] = summarize(rec.grab(sec_stand, "左"))
            show("左", one["left"])

            speak("hg_back", voice)
            time.sleep(2.0)
            speak("hg_center_ok", voice)
            print("  采集：中间")
            one["center2"] = summarize(rec.grab(sec_stand, "中"))
            show("中", one["center2"])

            speak("hg_to_right", voice)
            beep(880, 250)
            time.sleep(2.0)
            speak("hg_right_ok", voice)
            print("  采集：右边")
            one["right"] = summarize(rec.grab(sec_stand, "右"))
            show("右", one["right"])

            speak("hg_back", voice)
            time.sleep(2.0)
            speak("hg_center_ok", voice)
            print("  采集：中间")
            one["center3"] = summarize(rec.grab(sec_stand, "中"))
            show("中", one["center3"])

            result["rounds"].append(one)
            if r < rounds:
                speak("hg_round", voice)
                time.sleep(1.5)

    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        rec.close()

    speak("hg_end", voice)
    print("\n" + "=" * 70)
    save(result)
    verdict = report(result)
    result["verdict"] = verdict
    save(result)

    if verdict == "ok":
        speak("hg_ok", voice)
        beep(1200, 300)
    elif verdict == "weak":
        speak("hg_weak", voice)
        beep(880, 300)
    else:
        speak("hg_same", voice)
        beep(440, 400)
    return 0


def show(tag, s):
    if not s:
        print("  %s  无数据" % tag)
        return
    parts = []
    for name, _, _ in PAIRS:
        v = s.get(name)
        if v:
            parts.append("%s %+6.1f±%4.1f" % (name, v["mean"], v["std"]))
    print("  %s  %s" % (tag, "   ".join(parts)))


def report(result):
    rows = []
    for i, one in enumerate(result["rounds"], 1):
        if not (one.get("center1") and one.get("left")
                and one.get("right") and one.get("center3")):
            continue
        rows.append((i, one))

    print("  【各通道对：左 / 中 / 右】")
    print("  " + "-" * 68)
    print("    %-8s %-24s %-24s" % ("通道对", "第1轮 左/中/右", "第2轮 左/中/右"))

    scores = {}
    for name, _, _ in PAIRS:
        line = "    %-8s" % name
        spreads = []
        for i, one in rows:
            L = one["left"].get(name)
            C = one["center2"].get(name)
            R = one["right"].get(name)
            if not (L and C and R):
                line += " %-24s" % "(缺)"
                continue
            line += " %+5.0f/%+5.0f/%+5.0f      " % (L["mean"], C["mean"], R["mean"])
            spreads.append(max(L["mean"], C["mean"], R["mean"])
                           - min(L["mean"], C["mean"], R["mean"]))
        if spreads:
            scores[name] = float(np.mean(spreads))
        print(line)

    # 顺序检查：左、中、右是否单调
    print("\n  【是否单调：左 < 中 < 右 或 左 > 中 > 右】")
    print("  " + "-" * 68)
    mono_ok = {}
    for name, _, _ in PAIRS:
        flags = []
        for i, one in rows:
            L = one["left"].get(name)
            C = one["center2"].get(name)
            R = one["right"].get(name)
            if not (L and C and R):
                flags.append("?")
                continue
            v = (L["mean"], C["mean"], R["mean"])
            if v[0] < v[1] < v[2]:
                flags.append("↑单调")
            elif v[0] > v[1] > v[2]:
                flags.append("↓单调")
            else:
                flags.append("乱")
        mono_ok[name] = flags
        print("    %-8s %s" % (name, "   ".join(flags)))

    print("\n  " + "=" * 68)
    if not scores:
        print("  数据不足。")
        return "no_signal"

    best = max(scores, key=scores.get)
    print("    跨距排名：")
    for name, v in sorted(scores.items(), key=lambda kv: -kv[1]):
        mono = "、".join(mono_ok.get(name, []))
        mark = "  ← 最大" if name == best else ""
        print("      %-8s 跨距 %5.1f°   单调性 %s%s" % (name, v, mono, mark))

    mono_all = all(f in ("↑单调", "↓单调") for f in mono_ok.get(best, []))
    span = scores[best]
    if mono_all and span >= 15:
        print("\n  结论：成功。%s 跨距 %.1f°，两轮都是单调的。" % (best, span))
        return "ok"
    if span >= 10:
        print("\n  结论：%s 跨距 %.1f°，但单调性不完全稳定。" % (best, span))
        print("        可能是横移距离不够，或左右两边不对称。")
        return "weak"
    print("\n  结论：跨距只有 %.1f°，分不开。" % span)
    return "same"


def save(result):
    os.makedirs(OUT_DIR, exist_ok=True)
    d = os.path.join(OUT_DIR, result["ts"])
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "result.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("结果已保存: %s" % os.path.relpath(p, ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--board", default="2t4r", choices=["1t2r", "2t4r"])
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--stand", type=float, default=8.0, help="每段站立采集秒数")
    a = ap.parse_args()
    sys.exit(run(a.port, a.board, a.rounds, not a.no_voice, a.stand))


if __name__ == "__main__":
    main()
