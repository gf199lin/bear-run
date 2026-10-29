# -*- coding: utf-8 -*-
"""双板标定数据分析

读取 tools/calib_collect.py 产出的会话目录，产出：
  1. 数据质量概况（每类帧数、丢失、饱和）
  2. 1T2R 左右特征（距离门 / 多普勒零 / 两通道相位差）
  3. 2T4R 上下特征（4 通道相邻相位差，竖直放置时对应现实上下）
  4. 类间可分性（Fisher 比），回答"这个特征能不能用来分动作"
  5. 初步阈值建议（提案，需用独立数据复核）

用法：
  python tools\\calib_analyze.py data\\calibration\\<session>
  python tools\\calib_analyze.py --selftest      # 无数据自检
"""
import os
import sys
import json
import glob
import argparse
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 特征提取


def dop_fft(z):
    """沿 chirp 轴做多普勒 FFT 并归零频到中心。

    z: (chirps, bins, rx) -> complex (chirps, bins, rx)
    """
    return np.fft.fftshift(np.fft.fft(z, axis=0), axes=0)


def frame_feat(z, rx, skip_bins=0):
    """单帧特征。

    以"多普勒零通道 + 能量最强的距离门"为静态回波主瓣，
    在那里取相邻接收通道的相位差，作为角度方向指标。

    返回 dict 或 None。
    """
    z = np.asarray(z).astype(np.complex64)      # 必须是 IQ 复数，不能按实数处理
    if z.ndim != 3 or z.shape[2] < 2:
        return None
    d = dop_fft(z)                     # (chirps, bins, rx)
    c = d.shape[0] // 2
    d0 = d[c]                          # 多普勒零：静态目标 (bins, rx)
    prof = (np.abs(d0) ** 2).sum(axis=1)
    if not np.isfinite(prof).all() or prof.sum() <= 0:
        return None
    # 2T4R 的近场门被直流/泄漏占掉 80%+ 能量，必须跳过，
    # 否则永远选中门 0，相位差恒定、看着"完美可分"其实什么也没测到。
    prof_use = prof.copy()
    if skip_bins > 0 and prof.size > skip_bins:
        prof_use[:skip_bins] = 0.0
    if prof_use.sum() <= 0:
        prof_use = prof
    rbin = int(np.argmax(prof_use))
    v = d0[rbin]                       # (rx,) 复数

    # 能量加权后仍是复数，直接取相邻相位差
    diffs = [float(np.angle(v[i + 1] * np.conj(v[i]))) for i in range(min(rx, v.size) - 1)]

    total = float((np.abs(d) ** 2).sum())
    p_static = float((np.abs(d0) ** 2).sum())
    # 主瓣占全帧能量的比例：衡量"是否有一个清晰主导目标"
    dom = float(prof_use[rbin] / max(prof_use.sum(), 1e-12))

    return {
        "rbin": rbin,
        "diffs": diffs,
        "total_db": 10 * np.log10(max(total, 1e-12)),
        "static_ratio": p_static / max(total, 1e-12),
        "dominance": dom,
    }


def circ_stats(angles):
    """圆统计：均值（弧度）与结果长度 R（0~1，越大越集中）。"""
    a = np.asarray(angles, dtype=np.float64)
    if a.size == 0:
        return None, 0.0
    s = np.exp(1j * a).mean()
    return float(np.angle(s)), float(abs(s))


def fisher_ratio(groups):
    """一维 Fisher 判别比：类间方差 / 类内方差。越大越可分。"""
    vals = [np.asarray(g, dtype=np.float64) for g in groups if len(g) > 0]
    if len(vals) < 2:
        return 0.0
    allv = np.concatenate(vals)
    mu = allv.mean()
    between = sum(len(v) * (v.mean() - mu) ** 2 for v in vals) / len(vals)
    within = sum(((v - v.mean()) ** 2).sum() for v in vals) / max(len(allv) - len(vals), 1)
    if within <= 0:
        return 0.0
    return float(between / within)


# ---------------------------------------------------------------- 分析主流程


def analyze_session(sess_dir):
    meta_path = os.path.join(sess_dir, "meta.json")
    if not os.path.isfile(meta_path):
        print("找不到 meta.json：", meta_path)
        return 2
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    print("=" * 72)
    print("  标定数据分析 · 会话 %s" % meta.get("session"))
    print("=" * 72)
    print("  几何：%s" % meta.get("geometry"))
    print("  样本：%d 个（%s 次重复 x %s 秒）"
          % (len(meta["samples"]), meta.get("reps"), meta.get("seconds_per_sample")))

    # ---- 逐样本提特征
    rows = []
    for s in meta["samples"]:
        path = os.path.join(sess_dir, s["file"])
        if not os.path.isfile(path):
            continue
        try:
            npz = np.load(path)
        except Exception as exc:
            print("  跳过 %s: %s" % (s["file"], exc))
            continue

        f1 = f1_diffs = None
        feats1, feats2 = [], []
        if "z1" in npz:
            for z in npz["z1"]:
                ft = frame_feat(z, 2)
                if ft:
                    feats1.append(ft)
        if "z2" in npz:
            for z in npz["z2"]:
                ft = frame_feat(z, 4, skip_bins=3)
                if ft:
                    feats2.append(ft)

        def agg(feats, k):
            if not feats:
                return None, 0.0, 0.0
            m, r = circ_stats([f["diffs"][k] for f in feats if len(f["diffs"]) > k])
            return m, r, float(np.mean([f["dominance"] for f in feats]))

        d1_0, r1_0, dom1 = agg(feats1, 0)
        d2_0, r2_0, dom2 = agg(feats2, 0)
        d2_1, r2_1, _ = agg(feats2, 1)

        rows.append({
            "label": s["label"], "rep": s["rep"],
            "n1": len(feats1), "n2": len(feats2),
            "d1": d1_0, "d1_R": r1_0, "dom1": dom1,
            "d2": d2_0, "d2_R": r2_0, "dom2": dom2,
            "d2b": d2_1,
            "e1": float(np.mean([f["total_db"] for f in feats1])) if feats1 else None,
            "e2": float(np.mean([f["total_db"] for f in feats2])) if feats2 else None,
        })

    if not rows:
        print("\n  没有任何可用样本。")
        return 1

    # ---- 数据质量
    print("\n" + "-" * 72)
    print("  数据质量")
    print("-" * 72)
    print("  %-12s %5s %6s %6s   %s" % ("label", "样本", "1T2R帧", "2T4R帧", "备注"))
    labels = []
    plan_order = [p["label"] if isinstance(p, dict) else p[0]
                  for p in meta.get("plan", [])]

    def _order(x):
        return plan_order.index(x) if x in plan_order else 99

    labels = []
    for lab in sorted(set(r["label"] for r in rows), key=_order):
        labels.append(lab)
        sub = [r for r in rows if r["label"] == lab]
        n1 = sum(r["n1"] for r in sub)
        n2 = sum(r["n2"] for r in sub)
        note = ""
        if n2 == 0:
            note = "2T4R 无数据"
        elif n2 < len(sub) * 3:
            note = "2T4R 帧数偏少"
        print("  %-12s %5d %6d %6d   %s" % (lab, len(sub), n1, n2, note))

    # ---- 特征表
    def feat_table(key, title, unit="rad"):
        print("\n" + "-" * 72)
        print("  %s" % title)
        print("-" * 72)
        print("  %-12s %9s %8s %8s" % ("label", "均值(%s)" % unit, "集中度R", "样本数"))
        for lab in labels:
            vals = [r[key] for r in rows if r["label"] == lab and r[key] is not None]
            if not vals:
                print("  %-12s %9s %8s %8d" % (lab, "-", "-", 0))
                continue
            m, R = circ_stats(vals)
            print("  %-12s %9.3f %8.2f %8d" % (lab, m, R, len(vals)))
        return {lab: [r[key] for r in rows
                      if r["label"] == lab and r[key] is not None] for lab in labels}

    g1 = feat_table("d1", "1T2R 左右指标（两通道相位差，水平放置）")
    g2 = feat_table("d2", "2T4R 上下指标（通道 0-1 相位差，竖直放置）")
    g2b = feat_table("d2b", "2T4R 纵向指标（通道 1-2 相位差）")

    # ---- 可分性
    print("\n" + "-" * 72)
    print("  可分性（Fisher 比，越大越好；>1 值得用，>3 很好）")
    print("-" * 72)
    scores = [("1T2R 相位差", fisher_ratio(g1.values())),
              ("2T4R 通道0-1", fisher_ratio(g2.values())),
              ("2T4R 通道1-2", fisher_ratio(g2b.values()))]
    for name, v in sorted(scores, key=lambda x: -x[1]):
        flag = "OK" if v >= 3 else ("可用" if v >= 1 else "不足")
        print("  %-16s %8.2f   %s" % (name, v, flag))

    # ---- 左右阈值（只用 LANE_* 三类）
    print("\n" + "-" * 72)
    print("  左右阈值建议（提案，需独立数据复核）")
    print("-" * 72)
    lane = {}
    for lab in ("LANE_LEFT", "LANE_CENTER", "LANE_RIGHT"):
        vals = [r["d1"] for r in rows if r["label"] == lab and r["d1"] is not None]
        if vals:
            lane[lab] = circ_stats(vals)[0]
    if len(lane) == 3:
        order = sorted(lane.items(), key=lambda kv: kv[1])
        print("  角度指标排序（小 -> 大）：%s"
              % " < ".join("%s(%.3f)" % (k, v) for k, v in order))
        b1 = (order[0][1] + order[1][1]) / 2
        b2 = (order[1][1] + order[2][1]) / 2
        print("  建议分界：%.3f 与 %.3f rad" % (b1, b2))
        span = abs(order[2][1] - order[0][1])
        print("  左右跨距：%.3f rad（若过小，说明跨步幅度不够或阵列间距不匹配）" % span)
    else:
        print("  LANE_* 三类样本不全，无法给阈值")

    # ---- 结论
    print("\n" + "-" * 72)
    print("  结论")
    print("-" * 72)
    best = max(scores, key=lambda x: x[1])
    if best[1] >= 3:
        print("  %s 可分性 %.2f，足以支撑动作判定。" % (best[0], best[1]))
    elif best[1] >= 1:
        print("  %s 可分性 %.2f，偏弱，建议加大动作幅度或增加特征维度。"
              % (best[0], best[1]))
    else:
        print("  所有特征可分性都 < 1，当前几何/幅度下难以区分动作。")
    print("  注意：这是同一批数据上算的可分性，属于训练集表现；")
    print("        真实准确率必须用独立采样复核。")

    # ---- 存结果（自检产物不写进 outputs，避免污染交付目录）
    if meta.get("session") == "selftest":
        out_dir = os.path.join(ROOT, ".deepworks", "tmp")
    else:
        out_dir = os.path.join(ROOT, "outputs", "calibration-analysis")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "analysis-%s.json" % meta.get("session"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "session": meta.get("session"),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "fisher": {n: round(v, 3) for n, v in scores},
            "per_label": {lab: {
                "n": len([r for r in rows if r["label"] == lab]),
                "d1_mean": circ_stats([r["d1"] for r in rows
                                       if r["label"] == lab and r["d1"] is not None])[0],
                "d2_mean": circ_stats([r["d2"] for r in rows
                                       if r["label"] == lab and r["d2"] is not None])[0],
            } for lab in labels},
            "samples": rows,
        }, f, ensure_ascii=False, indent=2, default=str)
    print("\n  结果已保存：%s" % os.path.relpath(out, ROOT))
    return 0


# ---------------------------------------------------------------- 自检


def selftest():
    """用合成数据验证分析流程能跑通并给出合理方向。"""
    import tempfile
    print("=" * 72)
    print("  自检（合成数据）")
    print("=" * 72)
    tmp = tempfile.mkdtemp(prefix="calib_selftest_")
    rng = np.random.default_rng(7)

    plan = [("IDLE", "静止", ""), ("LANE_LEFT", "左", ""),
            ("LANE_CENTER", "中", ""), ("LANE_RIGHT", "右", ""),
            ("JUMP", "跳", ""), ("DUCK", "蹲", "")]
    # 人为设定：LEFT 相位差 -0.8，CENTER 0，RIGHT +0.8，JUMP/DUCK 用另一维
    phi = {"IDLE": 0.05, "LANE_LEFT": -0.8, "LANE_CENTER": 0.0,
           "LANE_RIGHT": 0.8, "JUMP": 0.0, "DUCK": 0.0}
    psi = {"IDLE": 0.0, "LANE_LEFT": 0.0, "LANE_CENTER": 0.0,
           "LANE_RIGHT": 0.0, "JUMP": 0.7, "DUCK": -0.7}

    samples = []
    idx = 0
    for lab, _, _ in plan:
        for rep in range(1, 3):
            idx += 1
            n1, n2 = 14, 6
            z1 = np.zeros((n1, 64, 8, 2), dtype=np.complex64)
            z2 = np.zeros((n2, 64, 32, 4), dtype=np.complex64)
            base = (rng.normal(0, 60, (64, 8, 2))
                    + 1j * rng.normal(0, 60, (64, 8, 2)))
            for i in range(n1):
                ph = phi[lab] + rng.normal(0, 0.12)
                sig = np.zeros((8, 2), dtype=np.complex128)
                sig[:, 0] = 2000
                sig[:, 1] = 2000 * np.exp(1j * ph)
                # 静态目标 = chirp 方向恒定（多普勒零），保留完整复数相位
                z1[i] = (base + np.repeat(sig[None, :, :], 64, axis=0)).astype(np.complex64)

            base2 = (rng.normal(0, 60, (64, 32, 4))
                     + 1j * rng.normal(0, 60, (64, 32, 4)))
            for i in range(n2):
                p0 = psi[lab] + rng.normal(0, 0.1)
                sig2 = np.zeros((32, 4), dtype=np.complex128)
                for k, ph in enumerate([0, p0, 2 * p0, 3 * p0]):
                    sig2[:, k] = 2500 * np.exp(1j * ph)
                z2[i] = (base2 + np.repeat(sig2[None, :, :], 64, axis=0)).astype(np.complex64)

            fn = "s%02d_%s_r%d.npz" % (idx, lab, rep)
            np.savez_compressed(os.path.join(tmp, fn), z1=z1, z2=z2,
                                t1=np.arange(n1) * 0.13, t2=np.arange(n2) * 0.52)
            samples.append({"idx": idx, "label": lab, "name": lab, "rep": rep,
                            "file": fn, "n_1t2r": n1, "n_2t4r": n2})

    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"session": "selftest", "geometry": {"1t2r": "h", "2t4r": "v"},
                   "reps": 3, "seconds_per_sample": 5,
                   "plan": [{"label": l, "name": n, "hint": h} for l, n, h in plan],
                   "samples": samples}, f, ensure_ascii=False)

    rc = analyze_session(tmp)
    print("\n  [自检] 合成数据方向设置：LEFT=-0.8 / CENTER=0 / RIGHT=+0.8，")
    print("         预期 1T2R 相位差可分性高、排序为 LEFT < CENTER < RIGHT。")
    return rc


def main():
    ap = argparse.ArgumentParser(description="双板标定数据分析")
    ap.add_argument("session", nargs="?", help="会话目录，如 data/calibration/20261029-1830")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.session:
        base = os.path.join(ROOT, "data", "calibration")
        dirs = sorted(glob.glob(os.path.join(base, "*")))
        print("用法：python tools\\calib_analyze.py <会话目录>")
        if dirs:
            print("已有会话：")
            for d in dirs[-5:]:
                print("  ", os.path.relpath(d, ROOT))
        return 2
    return analyze_session(args.session)


if __name__ == "__main__":
    sys.exit(main())
