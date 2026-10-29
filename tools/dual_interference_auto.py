# -*- coding: utf-8 -*-
"""毫米波双板干扰 · 全自动检测

================================================================
这个程序解决什么问题
================================================================
两块雷达同向摆在一起时，2T4R 会不会把 1T2R "打瞎"？

难点在于：想证明"有干扰"，最干净的方法是"关掉 2T4R 对比"。
但 2T4R 一旦上电自启（RegisterAutoBootup=1），
禁用 USB 只断数据线、**不断射频**，它照样在扫。
所以"软件开关雷达"这条路走不通。

本程序的思路：不关任何雷达，改用**周期指纹**。

  2T4R 每 520.8 ms 扫一次 → 1.92 Hz 的方波式占空比。
  如果它真的在打 1T2R，1T2R 的逐帧总能量上会叠加一个
  1.92 Hz 的周期性起伏。

  于是：只录 1T2R 的逐帧能量 → 去慢趋势 → 做频谱
        → 看 1.92 Hz 附近有没有显著的峰。

  全程不需要拔插、不需要点按钮、不需要管理员权限。

================================================================
判据（提案，待小组确认）
================================================================
  峰/噪底 >= 10 dB  且  峰频率落在实测 2T4R 帧频 +-15%
      -> 判定：检测到周期干扰
  否则
      -> 判定：本次未检测到显著周期干扰

  注意：这能证明"有"，不能证明"绝对没有"。
        功率太弱时同样测不到。

================================================================
用法
================================================================
  python tools/dual_interference_auto.py                # 默认 120 秒
  python tools/dual_interference_auto.py --seconds 60
  python tools/dual_interference_auto.py --selftest     # 无硬件自检
  python tools/dual_interference_auto.py --keep-raw     # 保留原始字节便于复查

  两块板都插好后再跑。1T2R 必须有数据；2T4R 用来提供"它确实在工作"的证据。
"""
import os
import re
import sys
import json
import time
import argparse
import threading
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RADAR_DIR = os.path.join(ROOT, "radar-pointcloud")
sys.path.insert(0, RADAR_DIR)

try:
    from serial.tools import list_ports
    from radar_source import SerialSource, build_pipeline, BOARD_PROFILES
except ImportError as exc:                                   # pragma: no cover
    print("导入失败：%s" % exc)
    print("请在项目根目录运行，且确保 radar-pointcloud/radar_source.py 存在。")
    sys.exit(2)

DEFAULT_SECONDS = 120.0
FRAME_PERIOD_2T4R = 0.520840        # 秒，实测 520.84 ms → 1.9199 Hz
NOMINAL_F2 = 1.0 / FRAME_PERIOD_2T4R

# 阈值说明：纯噪声频谱的"最大值/中位数"本身就在 9~12 dB（极值统计，与 bin 数有关）。
# 所以 10 dB 必然误报 —— 必须抬到 15 dB 以上，再叠一层"分段一致性"。
SNR_DB_PASS = 15.0
FREQ_TOL = 0.15                     # 相对容差 ±15%
SEG_CONSIST_PASS = 0.75             # 至少 3/4 的时段都要看到这个峰
N_SEGMENTS = 4
SEG_PEAK_RATIO = 3.0                # 段内峰 / 段内噪底 的倍数下限（≈9.5 dB）
MIN_FRAMES_1T2R = 96                # 少于这个帧数，频谱分辨率不够

# ---------------------------------------------------------------- 端口发现


def scan_ports():
    """按 VID:PID 精确认板，不靠魔数（两板帧头都是 55AA）。"""
    found = []
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        m = re.search(r"VID:PID=([0-9A-F]{4}):([0-9A-F]{4})", hwid)
        vidpid = ("%s:%s" % (m.group(1), m.group(2))) if m else ""
        board = None
        for name, prof in BOARD_PROFILES.items():
            if vidpid.lower() in [v.lower() for v in prof.get("vidpid", ())]:
                board = name
                break
        found.append({
            "port": p.device,
            "desc": (p.description or "")[:48],
            "vidpid": vidpid,
            "board": board,
        })
    return found


def pick(ports, board):
    for p in ports:
        if p["board"] == board:
            return p["port"]
    return None


# ---------------------------------------------------------------- 录制


class Side:
    """一路串口的录制状态。"""

    def __init__(self, port, board, raw_path=None):
        self.port = port
        self.board = board
        self.raw_path = raw_path
        self.ts = []
        self.energy = []
        self.errors = []
        self.stop = threading.Event()
        self.done = False

    def on_frame(self, z0, z1, stats):
        self.ts.append(time.monotonic())
        # 逐帧 RD 功率非相干相加，再取总和（dB 在外面算）
        acc = None
        for z in (z0, z1):
            zz = np.asarray(z)
            dop = np.fft.fftshift(np.fft.fft(zz, axis=0), axes=0)
            rng = np.fft.fft(dop, axis=1)
            p = (np.abs(rng) ** 2).astype(np.float64)
            acc = p if acc is None else acc + p
        self.energy.append(float(acc.sum()))

    def run(self):
        try:
            src = SerialSource(self.port, None)
            build_pipeline(src, self.on_frame, self.stop,
                           record_path=self.raw_path, board=self.board)
        except Exception as exc:
            self.errors.append("%s: %s" % (type(exc).__name__, exc))
        finally:
            self.done = True


def record_both(sec, p1, p2, keep_raw):
    tmp = os.path.join(ROOT, ".deepworks", "tmp")
    stamps = datetime.now().strftime("%Y%m%d-%H%M%S")
    s1 = Side(p1, "1t2r",
              os.path.join(tmp, "raw-1t2r-%s.bin" % stamps) if keep_raw else None)
    s2 = Side(p2, "2t4r",
              os.path.join(tmp, "raw-2t4r-%s.bin" % stamps) if keep_raw else None)

    workers = []
    if s1.port:
        workers.append(threading.Thread(target=s1.run, daemon=True))
    if s2.port:
        workers.append(threading.Thread(target=s2.run, daemon=True))
    for w in workers:
        w.start()

    t0 = time.time()
    try:
        while time.time() - t0 < sec:
            left = sec - (time.time() - t0)
            if left <= 0:
                break
            bar = int(30 * (1 - left / sec))
            print("\r  录制中 %5.1fs 剩余 |%s%s|" %
                  (left, "#" * bar, "." * (30 - bar)), end="", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n  手动中断，用已录部分分析 ...")
    finally:
        s1.stop.set()
        s2.stop.set()
        for w in workers:
            w.join(timeout=5)
        print("\r" + " " * 60)

    return s1, s2


# ---------------------------------------------------------------- 分析


def analyze_series(ts, energy, f_target):
    """在逐帧能量里找 f_target 附近的周期峰。

    返回 dict：帧率、峰频率、峰底比、是否命中。
    """
    e = np.asarray(energy, dtype=np.float64)
    t = np.asarray(ts, dtype=np.float64)
    n = len(e)
    if n < 16:
        return {"ok": False, "reason": "帧数太少 (%d)" % n}

    dur = float(t[-1] - t[0])
    if dur <= 0:
        return {"ok": False, "reason": "时长无效"}
    fs = (n - 1) / dur

    # 能量转 dB，压缩动态范围，避免个别强帧主导频谱
    e_db = 10.0 * np.log10(np.maximum(e, 1e-30))

    # 去慢趋势：减去 2 秒滑动均值（人体走动、温度漂移都在 0.5Hz 以下）
    win = max(3, int(round(fs * 2.0)))
    if win % 2 == 0:
        win += 1
    pad = win // 2
    padded = np.pad(e_db, pad, mode="edge")
    trend = np.convolve(padded, np.ones(win) / win, mode="valid")[:n]
    d = e_db - trend

    # 加窗后做频谱
    w = np.hanning(n)
    spec = np.abs(np.fft.rfft(d * w))
    freqs = np.fft.rfftfreq(n, 1.0 / fs)

    band = (freqs >= 0.5) & (freqs <= 3.5)
    if not band.any():
        return {"ok": False, "reason": "频率轴无效（帧率 %.2f Hz）" % fs}

    fb, sb = freqs[band], spec[band]
    k = int(np.argmax(sb))
    f_peak, a_peak = float(fb[k]), float(sb[k])
    floor = float(np.median(sb))
    snr = 20.0 * np.log10(a_peak / max(floor, 1e-30))

    # 半高宽，用于描述峰的锐度
    half = a_peak / 2.0
    lo = k
    while lo > 0 and sb[lo] > half:
        lo -= 1
    hi = k
    while hi < len(sb) - 1 and sb[hi] > half:
        hi += 1
    width = float(fb[hi] - fb[lo]) if hi > lo else 0.0

    ferr = abs(f_peak - f_target) / f_target

    # ---- 分段一致性：真干扰在每个时段都在同一频率上，噪声假峰位置随机飘 ----
    consist, seg_used, seg_hits = _segment_consistency(d, fs, f_target)

    hit = (snr >= SNR_DB_PASS) and (ferr <= FREQ_TOL) and (consist >= SEG_CONSIST_PASS)

    return {
        "ok": True,
        "frames": n,
        "duration_s": round(dur, 2),
        "fs_hz": round(fs, 3),
        "f_peak_hz": round(f_peak, 4),
        "snr_db": round(snr, 2),
        "peak_width_hz": round(width, 4),
        "f_target_hz": round(f_target, 4),
        "f_err_pct": round(ferr * 100, 1),
        "seg_consistency": round(consist, 2),
        "seg_used": seg_used,
        "seg_hits": seg_hits,
        "energy_med_db": round(float(np.median(e_db)), 2),
        "energy_p95_db": round(float(np.percentile(e_db, 95)), 2),
        "energy_max_db": round(float(np.max(e_db)), 2),
        "hit": bool(hit),
    }


def _segment_consistency(d, fs, f_target):
    """把去趋势后的信号切成 N 段，看每段在目标频率附近是否都有明显峰。

    返回 (命中比例, 实际用到的段数, 命中段数)。
    """
    n = len(d)
    size = n // N_SEGMENTS
    if size < 48:
        return 0.0, 0, 0
    lo = f_target * (1.0 - FREQ_TOL)
    hi = f_target * (1.0 + FREQ_TOL)
    used = hits = 0
    for i in range(N_SEGMENTS):
        seg = d[i * size:(i + 1) * size]
        w = np.hanning(len(seg))
        sp = np.abs(np.fft.rfft(seg * w))
        fr = np.fft.rfftfreq(len(seg), 1.0 / fs)
        band = (fr >= 0.5) & (fr <= 3.5)
        if not band.any():
            continue
        sb, fb = sp[band], fr[band]
        win = (fb >= lo) & (fb <= hi)
        if not win.any():
            continue
        used += 1
        if sb[win].max() > np.median(sb) * SEG_PEAK_RATIO:
            hits += 1
    return (hits / used if used else 0.0), used, hits


# ---------------------------------------------------------------- 报告


def print_report(r1, r2_rate, f_target, sec, assumed=False):
    print("\n" + "=" * 68)
    print("  检测报告")
    print("=" * 68)

    if not r1.get("ok"):
        print("  [1T2R] 数据不足：%s" % r1.get("reason"))
        print("  -> 检查 1T2R 是否插好、是否在出数（约 38 KB/s）")
        return None

    print("  [1T2R] 帧数 %d · 时长 %.1fs · 帧率 %.2f Hz"
          % (r1["frames"], r1["duration_s"], r1["fs_hz"]))
    print("         能量 中位 %.1f / p95 %.1f / max %.1f dB"
          % (r1["energy_med_db"], r1["energy_p95_db"], r1["energy_max_db"]))

    if r2_rate:
        print("  [2T4R] 帧率 %.2f Hz（≈%.1f ms/帧）-> 目标频率 %.3f Hz"
              % (r2_rate, 1000.0 / r2_rate, f_target))
    elif assumed:
        print("  [2T4R] 已声明在发射（其串口可能被厂商工具占用）-> 目标频率取标称 %.3f Hz"
              % f_target)
    else:
        print("  [2T4R] 没读到数据 —— 无法确认它正在发射")
        print("         目标频率退回标称值 %.3f Hz" % f_target)

    print("\n  频谱：峰 %.3f Hz · 峰底比 %.1f dB · 半高宽 %.3f Hz · 偏离目标 %.1f%%"
          % (r1["f_peak_hz"], r1["snr_db"], r1["peak_width_hz"], r1["f_err_pct"]))
    print("  时段：%d/%d 个时段在目标频率附近都看得到峰（一致性 %.0f%%）"
          % (r1["seg_hits"], r1["seg_used"], r1["seg_consistency"] * 100))

    print("-" * 68)
    if r1["hit"]:
        print("  结论：检测到 2T4R 的周期干扰（峰 %.3f Hz ≈ 扫频频率）" % r1["f_peak_hz"])
        print("        峰底比 %.1f dB > %.0f dB，且各时段稳定复现。" % (r1["snr_db"], SNR_DB_PASS))
        print("\n  建议：把 2T4R 往后挪 30~50 cm 或加大方位错开角，重跑一次；")
        print("        峰高应明显下降。若降不下来，说明需要从物理布局上做隔离。")
    else:
        if r1["snr_db"] < SNR_DB_PASS:
            print("  结论：本次未检测到显著周期干扰（峰底比 %.1f dB < %.0f dB）"
                  % (r1["snr_db"], SNR_DB_PASS))
        elif r1["f_err_pct"] > FREQ_TOL * 100:
            print("  结论：有峰（%.1f dB）但频率 %.3f Hz 不在 2T4R 的 %.3f Hz 附近，"
                  % (r1["snr_db"], r1["f_peak_hz"], f_target))
            print("        多半是环境里的其它周期性来源（风扇/空调/人走动），不是雷达互扰。")
        else:
            print("  结论：有峰但复现性不足（仅 %d/%d 个时段），不判定为雷达互扰。"
                  % (r1["seg_hits"], r1["seg_used"]))
        if not r2_rate and not assumed:
            print("\n  !! 本次结论无效：2T4R 全程 0 字节，它很可能压根没在发射。")
            print("     '没检测到干扰' 不能当成 '没有干扰' —— 请先确认 2T4R 正在工作。")
    print("=" * 68)
    return r1["hit"]


def save_report(payload):
    out_dir = os.path.join(ROOT, "outputs", "dual-interference")
    os.makedirs(out_dir, exist_ok=True)
    name = "report-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- 自检


def selftest():
    """无硬件下验证判据：纯噪声应判否，叠加 1.92Hz 调制应判是。"""
    print("=" * 68)
    print("  自检（合成数据，不需要雷达）")
    print("=" * 68)

    fs = 7.72
    n = 900                        # ≈116 秒
    rng = np.random.default_rng(20261029)
    t = np.arange(n) / fs
    base = 41.0 + 0.6 * np.sin(2 * np.pi * 0.07 * t)        # 慢漂移

    cases = []

    # 1) 纯噪声
    e1 = 10 ** ((base + rng.normal(0, 1.0, n)) / 10.0)
    cases.append(("纯噪声", e1, False))

    # 2) 叠加 1.92 Hz 方波调制（模拟干扰），幅度 2 dB
    sq = np.sign(np.sin(2 * np.pi * NOMINAL_F2 * t))
    e2 = 10 ** ((base + 2.0 * sq + rng.normal(0, 1.0, n)) / 10.0)
    cases.append(("1.92Hz 方波调制 +2dB", e2, True))

    # 3) 别的频率（0.9 Hz），不该被判成 2T4R
    slow = np.sin(2 * np.pi * 0.9 * t)
    e3 = 10 ** ((base + 2.0 * slow + rng.normal(0, 1.0, n)) / 10.0)
    cases.append(("0.9Hz 正弦（应排除）", e3, False))

    ok_all = True
    for label, e, expect in cases:
        ts = t.copy()
        r = analyze_series(ts, e, NOMINAL_F2)
        got = bool(r.get("hit"))
        mark = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok_all = False
        print("  [%s] %-24s 峰 %.3f Hz  峰底比 %5.1f dB  一致性 %d/%d  判定=%s  预期=%s"
              % (mark, label, r["f_peak_hz"], r["snr_db"],
                 r["seg_hits"], r["seg_used"],
                 "有" if got else "无", "有" if expect else "无"))

    # 假阳性压力测试：30 组不同种子的纯噪声，看会不会被误判成"有干扰"
    n_trials = 30
    false_pos = 0
    for k in range(n_trials):
        rng_k = np.random.default_rng(1000 + k)
        e = 10 ** ((base + rng_k.normal(0, 1.0, n)) / 10.0)
        if analyze_series(t.copy(), e, NOMINAL_F2).get("hit"):
            false_pos += 1

    rate = false_pos / n_trials * 100
    fp_ok = rate <= 3.0            # 允许极少量，超过 3% 说明判据还是松
    if not fp_ok:
        ok_all = False
    print("  [%s] %-24s 误报 %d/%d（%.0f%%）" %
          ("PASS" if fp_ok else "FAIL", "纯噪声假阳性压力测试", false_pos, n_trials, rate))

    print("-" * 68)
    print("  自检结果：%s" % ("全部通过" if ok_all else "有失败项，判据需要调整"))
    print("=" * 68)
    return 0 if ok_all else 1


# ---------------------------------------------------------------- 主流程


def main():
    ap = argparse.ArgumentParser(description="毫米波双板干扰 · 全自动检测（周期指纹法）")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                    help="录制时长，默认 %.0f 秒" % DEFAULT_SECONDS)
    ap.add_argument("--port3", default=None, help="手动指定 1T2R 串口，如 COM3")
    ap.add_argument("--port4", default=None, help="手动指定 2T4R 串口，如 COM4")
    ap.add_argument("--keep-raw", action="store_true", help="保留原始字节流")
    ap.add_argument("--assume-2t4r-on", action="store_true",
                    help="声明 2T4R 已在发射（例如被 ONELAB 占用 COM4 时），"
                         "此时用标称 1.92 Hz 作目标频率")
    ap.add_argument("--selftest", action="store_true", help="无硬件自检判据")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    ports = scan_ports()
    print("=" * 68)
    print("  毫米波双板干扰 · 全自动检测")
    print("=" * 68)
    print("  发现的串口：")
    if not ports:
        print("    （无）")
    for p in ports:
        print("    %-6s %-14s %-10s %s"
              % (p["port"], p["vidpid"] or "?", p["board"] or "未知", p["desc"]))

    p1 = args.port3 or pick(ports, "1t2r")
    p2 = args.port4 or pick(ports, "2t4r")

    if not p1:
        print("\n  !! 没找到 1T2R（CP2102 / 10C4:EA60）")
        print("     它是必须的 —— 没有它就没有判据来源。插好后重跑。")
        return 2

    print("\n  1T2R : %s" % p1)
    print("  2T4R : %s" % (p2 or "（未找到，结果可信度下降）"))
    print("\n  摆放要求：两块板按实际工作位置同向摆好，之后全程别动。")
    print("  人离开雷达视场（走动会带来 0.x Hz 的强干扰峰）。")
    print("  开始后 %.0f 秒内无人值守，随时 Ctrl+C 可提前结束分析。\n" % args.seconds)

    s1, s2 = record_both(args.seconds, p1, p2, args.keep_raw)

    for s in (s1, s2):
        if s.errors:
            print("  [%s] 异常：%s" % (s.board, "; ".join(s.errors[:2])))

    # 1T2R 分析
    r1 = analyze_series(s1.ts, s1.energy, NOMINAL_F2)

    # 2T4R 帧率（作为"它在发射"的证据 + 修正目标频率）
    r2_rate = None
    if len(s2.ts) > 8:
        d = s2.ts[-1] - s2.ts[0]
        if d > 0:
            r2_rate = (len(s2.ts) - 1) / d

    f_target = r2_rate if (r2_rate and 0.5 < r2_rate < 5.0) else NOMINAL_F2
    assumed = bool(args.assume_2t4r_on)
    has_evidence = bool(r2_rate) or assumed

    if r1.get("ok"):
        # 用实测帧率重算一次，让判据对准真实扫频周期
        r1 = analyze_series(s1.ts, s1.energy, f_target)

    hit = print_report(r1, r2_rate, f_target, args.seconds, assumed)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seconds_requested": args.seconds,
        "ports": {"1t2r": p1, "2t4r": p2},
        "errors": {"1t2r": s1.errors, "2t4r": s2.errors},
        "frame_rate_2t4r_hz": round(r2_rate, 4) if r2_rate else None,
        "target_freq_hz": round(f_target, 4),
        "criterion": {"snr_db_pass": SNR_DB_PASS, "freq_tol_rel": FREQ_TOL,
                      "segment_consistency": SEG_CONSIST_PASS,
                      "note": "提案，待小组确认"},
        "analysis_1t2r": r1,
        "verdict": (
            "interference_detected" if hit else
            # 2T4R 没数据 -> 它可能压根没发射，此时"没检测到"不等于"无干扰"
            "inconclusive_2t4r_silent" if not has_evidence else
            "no_significant_periodic_interference"
        ),
        "verdict_note": (
            "2T4R 全程无数据，无法确认其是否发射；本次结论无效，需先唤醒 2T4R"
            if not has_evidence else
            ("检测到与 2T4R 扫频周期一致的周期调制" if hit
             else "未检测到与 2T4R 扫频周期一致的周期调制")
        ),
        "caveat": ("周期指纹法只能证明'有干扰'，不能证明'绝对没有'；"
                   "干扰功率过低时同样测不到。"),
    }
    path = save_report(payload)
    print("\n  报告已保存：%s" % os.path.relpath(path, ROOT))
    return 0 if (r1.get("ok") and not hit) else (1 if hit else 2)


if __name__ == "__main__":
    sys.exit(main())
