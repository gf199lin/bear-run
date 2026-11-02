# -*- coding: utf-8 -*-
"""跳 / 蹲 离线可分性评估（只读 data/vertical，不碰串口）。

用途
----
回答一个问题：**现有 2T4R 跳蹲采集数据，能不能训练出可泛化的跳/蹲分类器？**

做法
----
每个会话的每一轮产生 2 个样本（jump 段、duck 段），特征全部相对本轮基线
（base1/base2/base3 拼接后的中位数）计算。评估用 **留一会话交叉验证**
（leave-one-session-out）：用其余会话选阈值/训模型，在没见过的会话上测。
这是唯一能暴露"跨安装高度泛化"的评估方式。

输出
----
- 终端表格
- outputs/jumpduck-offline-eval/report.md 与 report.json

用法
----
    python tools\\eval_jumpduck_offline.py
    python tools\\eval_jumpduck_offline.py --data data\\vertical

判读
----
基线：随机 = 50%，多数类 = 50%（本数据 JUMP/DUCK 各 17）。
若多特征 LOSO 不显著高于 50%，说明这批数据里没有可泛化的跳/蹲方向信息，
**不应**把它接入游戏，也不应写成"已验证"。
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEGS = ["base1", "jump", "base2", "duck", "base3"]
OUT_DIR = os.path.join(ROOT, "outputs", "jumpduck-offline-eval")


# ------------------------------------------------------------------ 数据

def features(seq, base):
    """一段俯仰角序列 → 相对基线的特征字典。"""
    v = np.asarray(seq, float)
    if v.size == 0:
        return None
    dev = v - base
    d = np.diff(v) if v.size > 1 else np.array([0.0])
    mad = float(np.median(np.abs(v - np.median(v)))) + 1e-6
    return {
        "hi": float(dev.max()),
        "lo": float(dev.min()),
        "mean": float(dev.mean()),
        "std": float(dev.std()),
        "hi_norm": float(dev.max()) / mad,
        "lo_norm": float(dev.min()) / mad,
        "skew": float(((dev / mad) ** 3).mean()),
        "maxdiff": float(np.abs(d).max()),
        "sum_absdiff": float(np.abs(d).sum()),
        "range": float(v.max() - v.min()),
        "frac_above": float((dev > 0).mean()),
    }


def load_rows(data_dir):
    rows = []
    for p in sorted(glob.glob(os.path.join(data_dir, "*", "result.json"))):
        doc = json.load(open(p, encoding="utf-8"))
        session = os.path.basename(os.path.dirname(p))
        verdict = doc.get("verdict")
        for i, rnd in enumerate(doc.get("rounds") or []):
            seqs = {k: (rnd.get(k) or {}).get("pitch_seq") for k in SEGS}
            if not seqs["jump"] or not seqs["duck"]:
                continue
            bases = [np.asarray(seqs[k], float) for k in ("base1", "base2", "base3")
                     if seqs[k]]
            if not bases:
                continue
            base = float(np.median(np.concatenate(bases)))
            for label, key in (("JUMP", "jump"), ("DUCK", "duck")):
                f = features(seqs[key], base)
                if f is None:
                    continue
                # 距离剖面类特征（如果没有就跳过这两个）
                prof = (rnd.get(key) or {}).get("prof")
                if prof:
                    pr = np.asarray(prof, float)
                    pr = pr / (pr.sum() + 1e-12)
                    f["peak_bin"] = float(np.argmax(pr))
                    f["prof_entropy"] = float(-(pr * np.log(pr + 1e-12)).sum())
                rows.append(dict(session=session, verdict=verdict, rnd=i,
                                 label=label, n=len(seqs[key]), feat=f))
    return rows


# ------------------------------------------------------------------ 模型

def fit_logreg(X, y, l2=3.0, iters=800, lr=0.35):
    n, k = X.shape
    w = np.zeros(k)
    b = 0.0
    for _ in range(iters):
        z = np.clip(X @ w + b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = p - y
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, b


def apply_logreg(X, w, b):
    z = np.clip(X @ w + b, -30, 30)
    return 1.0 / (1.0 + np.exp(-z)) >= 0.5


def loso_single(col, y, sess):
    pred = np.zeros(len(y), bool)
    for s in sorted(set(sess)):
        tr, te = sess != s, sess == s
        best = (-1.0, 0.0, True)
        for th in np.unique(col[tr]):
            for up in (True, False):
                p = (col[tr] >= th) if up else (col[tr] <= th)
                acc = float((p == (y[tr] == 1)).mean())
                if acc > best[0]:
                    best = (acc, th, up)
        _, th, up = best
        pred[te] = (col[te] >= th) if up else (col[te] <= th)
    return pred


def loso_multi(X, y, sess):
    pred = np.zeros(len(y), bool)
    for s in sorted(set(sess)):
        tr, te = sess != s, sess == s
        Xtr = X[tr]
        mu = Xtr.mean(axis=0)
        sd = Xtr.std(axis=0) + 1e-9
        w, b = fit_logreg((Xtr - mu) / sd, y[tr])
        pred[te] = apply_logreg((X[te] - mu) / sd, w, b)
    return pred


# ------------------------------------------------------------------ 报告

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "vertical"))
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    rows = load_rows(args.data)
    if len(rows) < 8:
        print("样本不足（%d），检查 --data 指向的目录" % len(rows))
        return 2

    names = list(rows[0]["feat"].keys())
    X = np.array([[r["feat"][k] for k in names] for r in rows], float)
    y = np.array([r["label"] == "JUMP" for r in rows])
    sess = np.array([r["session"] for r in rows])
    labels = np.array([r["label"] for r in rows])

    lines = []
    lines.append("# 跳 / 蹲 离线可分性评估\n")
    lines.append("数据：`%s`\n" % os.path.relpath(args.data, ROOT).replace("\\", "/"))
    lines.append("样本 %d 个（JUMP %d / DUCK %d），来自 %d 个会话、%d 轮。\n"
                 % (len(rows), int(y.sum()), int((~y).sum()),
                    len(set(sess)), len({(r["session"], r["rnd"]) for r in rows})))
    lines.append("评估：留一会话交叉验证（LOSO），阈值/权重只在训练会话上拟合。\n")
    lines.append("基线：随机 50.0%%，多数类 %.1f%%。\n\n" % (100 * max(y.mean(), 1 - y.mean())))

    # 单特征
    lines.append("## 单特征 LOSO\n\n| 特征 | LOSO 准确率 | JUMP 均值 | DUCK 均值 | 差值 |\n|---|---|---|---|---|\n")
    single = {}
    for j, nm in enumerate(names):
        pred = loso_single(X[:, j], y, sess)
        acc = float((pred == y).mean())
        single[nm] = acc
        jm, dm = float(X[y, j].mean()), float(X[~y, j].mean())
        lines.append("| %s | %.1f%% | %+.2f | %+.2f | %+.2f |\n"
                     % (nm, 100 * acc, jm, dm, jm - dm))

    # 多特征
    pred = loso_multi(X, y, sess)
    acc = float((pred == y).mean())
    jump_rec = float(pred[y].mean())
    duck_rec = float(pred[~y].mean())
    lines.append("\n## 多特征逻辑回归 LOSO\n\n")
    lines.append("- 总体准确率：**%.1f%%**（%d/%d）\n"
                 % (100 * acc, int((pred == y).sum()), len(y)))
    lines.append("- JUMP 召回：%.1f%%　DUCK 召回：%.1f%%\n" % (100 * jump_rec, 100 * duck_rec))
    lines.append("- 对照：随机 50.0%%，多数类 %.1f%%\n"
                 % (100 * max(y.mean(), 1 - y.mean())))

    # 逐会话
    lines.append("\n## 逐会话（多特征）\n\n| 会话 | 轮数 | 命中 | 原 verdict |\n|---|---|---|---|\n")
    per_session = {}
    for s in sorted(set(sess)):
        m = sess == s
        hit = int((pred[m] == y[m]).sum())
        n = int(m.sum())
        per_session[s] = dict(hit=hit, n=n, verdict=str(rows[int(np.argmax(m))]["verdict"]))
        lines.append("| %s | %d | %d/%d | %s |\n"
                     % (s, n // 2, hit, n, per_session[s]["verdict"]))

    # 结论：用 Wilson 95% 置信区间下界判断，避免小样本上"看着高一点"就下结论
    n = len(y)
    p = acc
    z = 1.96
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo, hi = max(0.0, center - half), min(1.0, center + half)

    lines.append("\n## 结论\n\n")
    lines.append("多特征 LOSO = **%.1f%%**（%d/%d），"
                 "Wilson 95%% 置信区间 **%.1f%% – %.1f%%**。\n\n"
                 % (100 * p, int((pred == y).sum()), n, 100 * lo, 100 * hi))
    lines.append("> 注意：单特征表里有 13 个特征，取最好的一个（%.1f%%）属于多重比较，"
                 "不能当作结论。\n\n" % (100 * max(single.values())))

    if lo >= 0.60:
        verdict = "有可分性"
        lines.append("置信区间下界 %.1f%% ≥ 60%%，可以进入接入阶段；"
                     "但样本仅 %d 个，必须用新会话复测后才可写入验收。\n" % (100 * lo, n))
    elif lo >= 0.50:
        verdict = "弱可分"
        lines.append("置信区间下界 %.1f%% 刚过随机线，最多算「弱信号」。"
                     "不足以支撑接入游戏（误触发会很明显），需补数据或换特征。\n" % (100 * lo))
    else:
        verdict = "不可分"
        lines.append("置信区间下界 %.1f%% **低于随机基线 50%%**，"
                     "即在这批数据上无法证明跳/蹲方向可分。"
                     "不能训练出可用分类器，不应接入游戏，也不应记为「已验证」。\n" % (100 * lo))
    lines.append("\n下一步（按优先级）：\n\n")
    lines.append("1. 采集时保存**原始帧**（4×RX × 32 bin 复数据），而不是只存 pitch 汇总量；\n")
    lines.append("2. 用 4 元竖排 RX 做 DBF 俯仰谱，替代 rx0-rx1 两元相位差（灵敏度差 3 倍）；\n")
    lines.append("3. 2T4R 的 527 ms 帧周期是硬限制，跳一次只能采到 1–2 帧；"
                 "要么改配置提帧率，要么改用 1T2R 多普勒路径；\n")
    lines.append("4. 重采后重跑本脚本，LOSO 置信下界 ≥ 60% 再接游戏。\n")

    text = "".join(lines)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(dict(samples=len(rows), sessions=len(set(sess)),
                       multi_feature_accuracy=acc,
                       single_feature=single,
                       per_session=per_session,
                       verdict=verdict), f, ensure_ascii=False, indent=2)

    print(text)
    print("\n已写入 %s" % os.path.relpath(os.path.join(args.out, "report.md"), ROOT))
    # "不可分"是有效结论，不是执行失败；只有数据不足/IO 错误才用非零码
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
