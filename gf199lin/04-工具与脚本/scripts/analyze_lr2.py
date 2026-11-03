# -*- coding: utf-8 -*-
"""补充统计：只打印写文档要引用的关键数字（避免输出过长被截断）。"""
from pathlib import Path
from collections import defaultdict
import csv
import numpy as np

UP = Path(r"<PROJECT_ROOT>\uploads\ses_ea91660b3ffell1v8B4tGE7NhJ")


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


def line(name, vals):
    if not vals:
        print("%-28s (empty)" % name)
        return
    a = np.asarray(vals, float)
    print("%-28s n=%-3d med=%+7.3f mean=%+7.3f sd=%6.3f min=%+7.3f max=%+7.3f"
          % (name, len(a), np.median(a), a.mean(), a.std(), a.min(), a.max()))


ar = []
with (UP / "lr-move-actions.csv").open(encoding="utf-8-sig", newline="") as f:
    ar = list(csv.DictReader(f))

print("== B4 px_delta_m by label ==")
for lab in ("LEFT", "RIGHT"):
    line(lab + " px_delta", [fnum(r["px_delta_m"]) for r in ar
                             if r["direction_label"] == lab and fnum(r["px_delta_m"]) is not None])

print("\n== B5 per-station table ==")
for dist in sorted(set(r["distance_m"] for r in ar)):
    for lab in ("LEFT", "RIGHT"):
        d = [fnum(r["px_delta_m"]) for r in ar
             if r["distance_m"] == dist and r["direction_label"] == lab]
        d = [v for v in d if v is not None]
        if d:
            pos = sum(1 for v in d if v > 0)
            print("  %-5s m %-5s n=%d med=%+7.3f  delta>0(向正) %d/%d"
                  % (dist, lab, len(d), float(np.median(d)), pos, len(d)))

print("\n== B6 classifier: pred LEFT if px_delta<0 ==")
tp = fp = tn = fn = unk = 0
for r in ar:
    v = fnum(r["px_delta_m"])
    lab = r["direction_label"]
    if v is None:
        unk += 1
        continue
    pred = "LEFT" if v < 0 else "RIGHT"
    if lab == "LEFT":
        tp += pred == "LEFT"
        fn += pred == "RIGHT"
    else:
        tn += pred == "RIGHT"
        fp += pred == "LEFT"
n = tp + fp + tn + fn
print("  真LEFT  预测LEFT=%d  预测RIGHT=%d" % (tp, fn))
print("  真RIGHT 预测RIGHT=%d 预测LEFT=%d" % (tn, fp))
print("  n=%d unk=%d  acc=%.1f%%  recallL=%.0f%% recallR=%.0f%%"
      % (n, unk, 100.0 * (tp + tn) / n, 100.0 * tp / (tp + fn), 100.0 * tn / (tn + fp)))

print("\n== B7 window quality ==")
for lab in ("LEFT", "RIGHT"):
    sub = [r for r in ar if r["direction_label"] == lab]
    line(lab + " frames_with_target",
         [fnum(r["frames_with_target"]) for r in sub if fnum(r["frames_with_target"]) is not None])
    line(lab + " px_range_m",
         [fnum(r["px_range_m"]) for r in sub if fnum(r["px_range_m"]) is not None])
    line(lab + " mean_range_m",
         [fnum(r["mean_range_m"]) for r in sub if fnum(r["mean_range_m"]) is not None])
    line(lab + " mean_v_lat",
         [fnum(r["mean_v_lat_mps"]) for r in sub if fnum(r["mean_v_lat_mps"]) is not None])

print("\n== window frames vs frames_with_target (检出密度) ==")
for r in ar[:4]:
    print("  %s %s f(%s..%s)=%s with_target=%s rng=%s"
          % (r["action_id"], r["direction_label"], r["frame_start"], r["frame_end"],
             fnum(r["frame_end"]) - fnum(r["frame_start"]), r["frames_with_target"],
             r["mean_range_m"]))
