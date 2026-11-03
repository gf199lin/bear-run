# -*- coding: utf-8 -*-
"""离线分析 lr-move 数据集：四方向数据是否够用？
只读 uploads 下的两个 CSV，不写任何项目文件。
"""
from pathlib import Path
from collections import Counter, defaultdict
import csv, math
import numpy as np

UP = Path(r"<PROJECT_ROOT>\uploads\ses_ea91660b3ffell1v8B4tGE7NhJ")
FR = UP / "lr-move-frames.csv"
AC = UP / "lr-move-actions.csv"


def fnum(s):
    if s is None:
        return None
    s = s.strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pct(a, b):
    return (100.0 * a / b) if b else float("nan")


def desc(name, vals):
    if not vals:
        print("  %-22s (no data)" % name)
        return
    a = np.asarray(vals, dtype=float)
    print("  %-22s n=%-4d mean=%+7.3f med=%+7.3f sd=%6.3f min=%+7.3f max=%+7.3f p05=%+7.3f p95=%+7.3f"
          % (name, len(a), a.mean(), np.median(a), a.std(), a.min(), a.max(),
             np.percentile(a, 5), np.percentile(a, 95)))


# ---------------- frames ----------------
rows = []
with FR.open(encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        rows.append(r)

print("=" * 78)
print("A. lr-move-frames.csv  (%d 行)" % len(rows))
print("=" * 78)

segs = Counter(r["segment"] for r in rows)
print("\n[A1] 每段帧数:", dict(segs))

det = defaultdict(lambda: [0, 0])
for r in rows:
    det[r["segment"]][1] += 1
    if fnum(r["n_pts"]) is not None:
        det[r["segment"]][0] += 1
print("\n[A2] 检出率（n_pts 非空 / 总帧）:")
tot_d = tot_n = 0
for s in sorted(det):
    d, n = det[s]
    tot_d += d; tot_n += n
    print("  %-8s %4d/%4d = %5.1f%%" % (s, d, n, pct(d, n)))
print("  %-8s %4d/%4d = %5.1f%%" % ("合计", tot_d, tot_n, pct(tot_d, tot_n)))

print("\n[A3] direction 标签分布:", dict(Counter(r["direction"] or "(空)" for r in rows)))

ev = Counter()
for r in rows:
    for e in (r["events"] or "").split(";"):
        e = e.strip()
        if e:
            ev[e] += 1
print("\n[A4] events 事件计数（帧级）:", dict(ev))

ts = sorted(fnum(r["t_s"]) for r in rows if fnum(r["t_s"]) is not None and r["segment"] == sorted(segs)[0])
dts = np.diff(np.asarray(ts))
print("\n[A5] 帧间隔: med=%.4f s  (=%.2f Hz)  min=%.4f max=%.4f  n=%d"
      % (np.median(dts), 1.0 / np.median(dts), dts.min(), dts.max(), len(dts)))

# 静息基线（IDLE 帧）
for s in sorted(segs):
    px = [fnum(r["px_m"]) for r in rows if r["segment"] == s and (r["direction"] == "IDLE")]
    px = [v for v in px if v is not None]
    desc("IDLE px_m @ " + s, px)

# 动作窗内
for lab in ("LEFT", "RIGHT"):
    px = [fnum(r["px_m"]) for r in rows if r["direction"] == lab]
    px = [v for v in px if v is not None]
    desc("%s 窗内 px_m" % lab, px)

print("\n[A6] 每帧点数 n_pts 分布:", dict(sorted(Counter(
    int(fnum(r["n_pts"])) for r in rows if fnum(r["n_pts"]) is not None).items())))

for s in sorted(segs):
    npts = [fnum(r["n_pts"]) for r in rows if r["segment"] == s and fnum(r["n_pts"]) is not None]
    if npts:
        print("  n_pts mean @%-8s = %.2f" % (s, float(np.mean(npts))))

# ---------------- actions ----------------
ar = []
with AC.open(encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        ar.append(r)

print()
print("=" * 78)
print("B. lr-move-actions.csv  (%d 行)" % len(ar))
print("=" * 78)
print("\n[B1] 每方向动作数:", dict(Counter(r["direction_label"] for r in ar)))
print("[B2] 数据方向与人工标注一致性:", dict(Counter(r["dir_matches_label"] for r in ar)))
print("[B3] 交叉表:")
for lab in ("LEFT", "RIGHT"):
    sub = [r for r in ar if r["direction_label"] == lab]
    yes = sum(1 for r in sub if r["dir_matches_label"] == "yes")
    print("  人工标注 %-5s : px 一致 %2d / %2d  (%.0f%%)" % (lab, yes, len(sub), pct(yes, len(sub))))

print("\n[B4] px_delta_m 分布（负=向左）:")
for lab in ("LEFT", "RIGHT"):
    d = [fnum(r["px_delta_m"]) for r in ar if r["direction_label"] == lab]
    desc(lab + " px_delta_m", [v for v in d if v is not None])

print("\n[B5] 按站位分组（px_delta_m 中位数）:")
for dist in sorted(set(r["distance_m"] for r in ar)):
    for lab in ("LEFT", "RIGHT"):
        d = [fnum(r["px_delta_m"]) for r in ar if r["distance_m"] == dist and r["direction_label"] == lab]
        d = [v for v in d if v is not None]
        if d:
            print("  %-5s m  人工=%-5s n=%d  med_delta=%+7.3f  符号为正(右)的个数=%d/%d"
                  % (dist, lab, len(d), float(np.median(d)), sum(1 for v in d if v > 0), len(d)))

print("\n[B6] 若仅用 px_delta 符号当分类器（把 <0 判为 LEFT）:")
tp = fp = tn = fn = 0
for r in ar:
    v = fnum(r["px_delta_m"])
    if v is None:
        continue
    pred = "LEFT" if v < 0 else "RIGHT"
    lab = r["direction_label"]
    if lab == "LEFT" and pred == "LEFT": tp += 1
    elif lab == "LEFT" and pred == "RIGHT": fn += 1
    elif lab == "RIGHT" and pred == "RIGHT": tn += 1
    else: fp += 1
n = tp + fp + tn + fn
print("   混淆矩阵  (n=%d)" % n)
print("               pred LEFT   pred RIGHT")
print("   真 LEFT        %3d         %3d" % (tp, fn))
print("   真 RIGHT       %3d         %3d" % (fp, tn))
if n:
    print("   准确率 = %.1f%%   左右召回: LEFT %.0f%% / RIGHT %.0f%%"
          % (pct(tp + tn, n), pct(tp, tp + fn), pct(tn, tn + fp)))

print("\n[B7] 窗口质量: frames_with_target / px_range_m / mean_deg_span")
for lab in ("LEFT", "RIGHT"):
    sub = [r for r in ar if r["direction_label"] == lab]
    desc(lab + " frames_with_target", [fnum(r["frames_with_target"]) for r in sub if fnum(r["frames_with_target"]) is not None])
    desc(lab + " px_range_m", [fnum(r["px_range_m"]) for r in sub if fnum(r["px_range_m"]) is not None])

print("\n[B8] 动作时间窗长度:")
dur = [(fnum(r["frame_end"]) - fnum(r["frame_start"])) * 0.1295 for r in ar
       if fnum(r["frame_end"]) is not None and fnum(r["frame_start"]) is not None]
desc("窗口时长 s", dur)

print("\n[B9] 动作间隔（同段相邻动作起点差，秒）:")
byseg = defaultdict(list)
for r in ar:
    byseg[r["segment"]].append(fnum(r["t_start_s"]))
gaps = []
for s, v in byseg.items():
    v = sorted(x for x in v if x is not None)
    gaps += list(np.diff(v))
desc("相邻动作间隔 s", gaps)
print("\n完成。")
