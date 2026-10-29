"""2T4R 竖直动作验证：跳 / 蹲 能否通过"目标距离门移动"区分。

前提：2T4R 已移到离地约 30 cm。雷达越低，人跳起时身体与雷达的距离
      变化越大，跳（变远）与蹲（变近）会朝相反方向移动距离门。

流程（3 轮，语音引导，全程不用碰键盘）：
    站好 -> 跳三次 -> 站好 -> 蹲三次 -> 站好

后台只采 2T4R，不吃多普勒（只看距离剖面），所以 1.9 Hz 帧率够用。
跑完自动判断跳/蹲是否反向移动，并给出结论。

用法：
    python tools\\vertical_probe.py
    python tools\\vertical_probe.py --port COM4 --rounds 3 --no-voice
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "radar-pointcloud"))

from radar_source import SerialSource, make_protocol  # noqa: E402

try:
    import winsound
    _HAS_SOUND = True
except Exception:
    _HAS_SOUND = False

TTS_CACHE = os.path.join(ROOT, ".deepworks", "tmp", "tts_cache")
TTS_VOICE = "Microsoft Huihui Desktop"

# 距离门参数（来自 Radar_Config.ini：2T4R 分辨率 0.15 m/门，32 门）
BIN_M = 0.15
BAND_LO, BAND_HI = 6, 21          # 0.90 m ~ 3.15 m，人体活动带，避开近场泄漏

PHRASES = {
    "stand": ("v_stand", "请站到雷达正前方两米处，保持站立不动"),
    "jump":  ("v_jump",  "现在开始跳，连跳三次，动作放慢"),
    "duck":  ("v_duck",  "现在开始蹲，连续蹲三次，动作放慢"),
    "stop":  ("v_stop",  "停，站好不要动"),
    "ready": ("v_ready", "准备开始，请站好"),
}


def _ps_run(script):
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                          capture_output=True, timeout=25)


def synth(text, tag):
    path = os.path.join(TTS_CACHE, tag + ".wav")
    if os.path.isfile(path) and os.path.getsize(path) > 2000:
        return path
    os.makedirs(TTS_CACHE, exist_ok=True)
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { $s.SelectVoice('%s') } catch { }; "
        "$s.Rate = 0; "
        "$s.SetOutputToWaveFile('%s'); "
        "$s.Speak('%s'); "
        "$s.Dispose()" % (TTS_VOICE, path, text.replace("'", "''"))
    )
    try:
        _ps_run(script)
    except Exception:
        return None
    return path if os.path.isfile(path) and os.path.getsize(path) > 2000 else None


def prepare_voice():
    ok = 0
    for tag, (cache_tag, text) in PHRASES.items():
        if synth(text, cache_tag):
            ok += 1
    return ok, len(PHRASES)


def say(key):
    if not _HAS_SOUND:
        return
    cache_tag = PHRASES[key][0]
    path = os.path.join(TTS_CACHE, cache_tag + ".wav")
    if os.path.isfile(path):
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            pass


# ------------------------------------------------------------------ 采集

class Recorder:
    def __init__(self, port):
        self.port = port
        self.frames = []
        self._stop = False
        self.src = None
        self.parser = None
        self.asm = None

    def start(self):
        self.src = SerialSource(self.port, None)
        self.src.open()
        self.parser, self.asm = make_protocol("2t4r")
        import threading
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self._stop:
            chunk = self.src.read(0.05)
            if not chunk:
                continue
            for rf in self.parser.feed(chunk):
                c = self.asm.push(rf)
                if c is not None:
                    self.frames.append((time.time(), c))

    def window(self, t0, t1):
        return [z for t, z in self.frames if t0 <= t <= t1]

    def close(self):
        self._stop = True
        try:
            self.src.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 分析

def centroids(frames):
    """每帧的能量加权距离质心（单位：门）。不碰多普勒，只看距离剖面。

    注意：assembler 产出的单帧形状是 (rx, chirps, bins)，
    距离门在最后一维。早期版本误按 (chirps, bins, rx) 处理，
    等于在 chirp 轴上求质心，结果恒为中间值、看起来"纹丝不动"。
    """
    out = []
    for z in frames:
        prof = (np.abs(z) ** 2).sum(axis=(0, 1))[BAND_LO:BAND_HI]   # (bins,) -> 人体带
        if prof.sum() <= 0:
            out.append(np.nan)
            continue
        bins = np.arange(BAND_LO, BAND_HI, dtype=float)
        out.append(float((prof * bins).sum() / prof.sum()))
    return np.array(out)


def profiles(frames):
    """每帧的归一化距离剖面 (32,)，只保留形状信息，不受整体强弱影响。"""
    out = []
    for z in frames:
        prof = (np.abs(z) ** 2).sum(axis=(0, 1))          # (32,)
        t = prof.sum()
        if t <= 0:
            continue
        out.append(prof / t)
    return np.array(out) if out else None


def pitch_series(frames, lo=6, hi=21):
    """每帧俯仰相位差（度）。对带内所有距离门做能量加权圆平均，
    这样即使目标门在跳/蹲时移动，读数也不会因为门切换而跳变。"""
    out = []
    bins = list(range(lo, hi))
    for z in frames:
        w = (np.abs(z) ** 2).sum(axis=(0, 1))[lo:hi].astype(float)
        if w.sum() <= 0:
            out.append(np.nan)
            continue
        vec = np.empty(len(bins), dtype=complex)
        for i, b in enumerate(bins):
            a = np.angle(z[1, :, b] * np.conj(z[0, :, b]))
            vec[i] = np.exp(1j * a).mean()
        out.append(float(np.degrees(np.angle((vec * w).sum()))))
    return np.array(out)


def stats(frames):
    c = centroids(frames)
    c = c[np.isfinite(c)]
    if c.size == 0:
        return None
    pr = profiles(frames)
    ps = pitch_series(frames)
    ps = ps[np.isfinite(ps)]
    d = {"n": int(c.size), "mean": float(c.mean()),
         "min": float(c.min()), "max": float(c.max()),
         "seq": [round(float(v), 3) for v in c],
         "prof": ([round(float(v), 6) for v in pr.mean(axis=0)]
                  if pr is not None else None)}
    if ps.size:
        d["pitch"] = [round(float(v), 2) for v in ps]
        d["pitch_mean"] = round(float(ps.mean()), 2)
        d["pitch_std"] = round(float(ps.std()), 2)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--base-sec", type=float, default=4.0)
    ap.add_argument("--act-sec", type=float, default=6.0)
    ap.add_argument("--no-voice", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("  2T4R 竖直动作验证：跳 / 蹲 能否靠距离门反向移动区分")
    print("=" * 70)
    print("  距离门宽 %.2f m，只看 %.2f~%.2f m 的人体活动带" %
          (BIN_M, BAND_LO * BIN_M, BAND_HI * BIN_M))
    print("  请确认 2T4R 已放在离地约 30 cm 处，且人站在正前方约 2 m")

    if not args.no_voice:
        ok, tot = prepare_voice()
        print("  语音：%d/%d 句就绪" % (ok, tot))

    input("\n  按回车开始（之后全程听语音提示）...")

    rec = Recorder(args.port).start()
    time.sleep(2.5)
    if not rec.frames:
        print("\n  !! %s 2.5 秒内没有数据。端口可能被占用（start_dual_radar.cmd 之类）。" % args.port)
        rec.close()
        return 3

    rounds = []
    for rnd in range(1, args.rounds + 1):
        print("\n" + "-" * 70)
        print("  第 %d / %d 轮" % (rnd, args.rounds))
        print("-" * 70)

        segs = {}
        # 基线 1
        print("  [站好] 采 %.0fs" % args.base_sec)
        say("stand")
        t0 = time.time(); time.sleep(args.base_sec); t1 = time.time()
        segs["base1"] = rec.window(t0, t1)

        # 跳
        print("  [跳]   采 %.0fs —— 听提示" % args.act_sec)
        say("jump")
        t0 = time.time(); time.sleep(args.act_sec); t1 = time.time()
        segs["jump"] = rec.window(t0, t1)

        # 基线 2
        print("  [站好] 采 %.0fs" % args.base_sec)
        say("stop")
        t0 = time.time(); time.sleep(args.base_sec); t1 = time.time()
        segs["base2"] = rec.window(t0, t1)

        # 蹲
        print("  [蹲]   采 %.0fs —— 听提示" % args.act_sec)
        say("duck")
        t0 = time.time(); time.sleep(args.act_sec); t1 = time.time()
        segs["duck"] = rec.window(t0, t1)

        # 基线 3
        print("  [站好] 采 %.0fs" % args.base_sec)
        say("stop")
        t0 = time.time(); time.sleep(args.base_sec); t1 = time.time()
        segs["base3"] = rec.window(t0, t1)

        r = {}
        for k, v in segs.items():
            s = stats(v)
            r[k] = s
            if s:
                print("    %-6s 帧=%2d 质心均=%.2f 门 (范围 %.2f~%.2f)"
                      % (k, s["n"], s["mean"], s["min"], s["max"]))
            else:
                print("    %-6s 无数据" % k)
        rounds.append(r)

    rec.close()

    # ---------------- 汇总
    print("\n" + "=" * 70)
    print("  结果汇总（质心单位：门，1 门 = %.2f m）" % BIN_M)
    print("=" * 70)

    base_all = []
    for r in rounds:
        for k in ("base1", "base2", "base3"):
            if r.get(k):
                base_all.append(r[k]["mean"])
    if not base_all:
        print("  基线数据缺失，无法判断。")
        return 2
    base_mu = float(np.mean(base_all))

    jumps, ducks = [], []
    for i, r in enumerate(rounds, 1):
        bn = [r[k]["mean"] for k in ("base1", "base2", "base3") if r.get(k)]
        b = float(np.mean(bn)) if bn else base_mu
        j = r["jump"]["mean"] - b if r.get("jump") else None
        d = r["duck"]["mean"] - b if r.get("duck") else None
        jumps.append(j)
        ducks.append(d)
        print("  第%d轮  基线=%.2f  跳偏移=%s  蹲偏移=%s"
              % (i, b,
                 ("%+.2f 门" % j) if j is not None else "无数据",
                 ("%+.2f 门" % d) if d is not None else "无数据"))

    jv = [x for x in jumps if x is not None]
    dv = [x for x in ducks if x is not None]
    if not jv or not dv:
        print("\n  数据不足，无法判断。")
        return 2

    jm, dm = float(np.mean(jv)), float(np.mean(dv))
    print("\n  【质心法】跳 %+.2f 门  蹲 %+.2f 门  （仅供参考，噪声大）" % (jm, dm))

    # ---- 背景差分：减掉静止背景，只看动作引起的剖面形状变化 ----
    def peak_of(d):
        pos = np.clip(d, 0, None)
        if pos.sum() <= 0:
            return None
        return int(np.argmax(pos)), float(pos.max() / max(np.abs(d).sum(), 1e-12))

    j_pk, d_pk = [], []
    for i, r in enumerate(rounds, 1):
        bp = [r[k]["prof"] for k in ("base1", "base2", "base3")
              if r.get(k) and r[k].get("prof")]
        if not bp:
            continue
        base = np.mean(np.array(bp), axis=0)
        row = "  第%d轮差分峰值  " % i
        if r.get("jump") and r["jump"].get("prof"):
            rp = peak_of(np.array(r["jump"]["prof"]) - base)
            if rp:
                j_pk.append(rp[0]); row += "跳=门%d(%.2fm) " % (rp[0], rp[0]*BIN_M)
        if r.get("duck") and r["duck"].get("prof"):
            rp = peak_of(np.array(r["duck"]["prof"]) - base)
            if rp:
                d_pk.append(rp[0]); row += "蹲=门%d(%.2fm)" % (rp[0], rp[0]*BIN_M)
        print(row + "   基线峰=门%d" % int(np.argmax(base)))

    print("\n  【俯仰角判读】")
    b_all = []
    for r in rounds:
        for k in ("base1", "base2", "base3"):
            v = r.get(k)
            if v and "pitch_mean" in v:
                b_all.append(v["pitch_mean"])
    j_all = [r["jump"]["pitch_mean"] for r in rounds
             if r.get("jump") and "pitch_mean" in r["jump"]]
    d_all = [r["duck"]["pitch_mean"] for r in rounds
             if r.get("duck") and "pitch_mean" in r["duck"]]

    verdict = "no_signal"
    if b_all and j_all and d_all:
        bm = float(np.mean(b_all))
        jm2 = float(np.mean(j_all))
        dm2 = float(np.mean(d_all))
        print("    基线  %+7.1f deg" % bm)
        print("    跳    %+7.1f deg   (相对基线 %+.1f)" % (jm2, jm2 - bm))
        print("    蹲    %+7.1f deg   (相对基线 %+.1f)" % (dm2, dm2 - bm))
        sep = jm2 - dm2
        print("    跳-蹲 %+7.1f deg" % sep)
        js = [r["jump"]["pitch_std"] for r in rounds
              if r.get("jump") and "pitch_std" in r["jump"]]
        ds = [r["duck"]["pitch_std"] for r in rounds
              if r.get("duck") and "pitch_std" in r["duck"]]
        if js:
            print("    跳内波动 %.1f deg" % float(np.mean(js)))
        if ds:
            print("    蹲内波动 %.1f deg" % float(np.mean(ds)))
        if sep >= 10:
            print("    >> 成功：跳的仰角明显高于蹲，方向正确。")
            verdict = "ok"
        elif sep >= 5:
            print("    >> 方向对，分离度中等(5~10 deg)。建议加大动作再测。")
            verdict = "weak"
        elif sep <= -5:
            print("    >> 方向相反，检查动作顺序或相位符号。")
            verdict = "reversed"
        else:
            print("    >> 跳和蹲俯仰角接近，区分不开。")
            verdict = "same"
    else:
        print("    数据不足。")

    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
