# -*- coding: utf-8 -*-
"""训练跳 / 蹲识别模型（NONE / JUMP / DUCK）。

数据来源：data/vertical/*/result.json 里的 pitch_seq（2T4R，rx0-rx1 相位差）。
算法：三分类 softmax 回归（纯 numpy），按类别加权处理样本不平衡。
评估：**留一会话交叉验证**（LOSO），阈值/权重只在训练会话上拟合。

⚠ 这批数据的已知结论：跳/蹲方向几乎不可分（见 docs/jump-duck-data-audit.md）。
   本脚本的作用是**把训练/导出/接入的程序跑通**，等重采出更好的数据后
   同一条命令重跑即可。不要把这里的指标当成验收结论。

用法
----
    python tools\\train_jumpduck.py
    python tools\\train_jumpduck.py --window 4 --min-dev 8 --l2 1.0
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from jumpduck_core import (CLASSES, DEFAULT_WINDOW, FEATURE_NAMES,  # noqa: E402
                           PriorJudge, SoftmaxModel, pitch_from_frame,
                           window_features)

SEGS = ["base1", "jump", "base2", "duck", "base3"]


# ------------------------------------------------------------------ 样本

def sliding(seq, w):
    v = np.asarray(seq, float)
    for i in range(0, max(0, len(v) - w + 1)):
        yield v[i:i + w]


def build_samples(data_dir, window, min_dev, include_none=False):
    """返回 samples=[{x, y, session, round, seg}], skipped={...}"""
    samples = []
    skipped = Counter()
    for p in sorted(glob.glob(os.path.join(data_dir, "*", "result.json"))):
        doc = json.load(open(p, encoding="utf-8"))
        session = os.path.basename(os.path.dirname(p))
        for ri, rnd in enumerate(doc.get("rounds") or []):
            seqs = {k: (rnd.get(k) or {}).get("pitch_seq") for k in SEGS}
            if not seqs["jump"] or not seqs["duck"]:
                continue
            bases = [np.asarray(seqs[k], float) for k in ("base1", "base2", "base3")
                     if seqs[k]]
            if not bases:
                continue
            base = float(np.median(np.concatenate(bases)))

            plan = [("JUMP", "jump"), ("DUCK", "duck")]
            if include_none:
                plan = [("NONE", "base1"), ("NONE", "base2"), ("NONE", "base3")] + plan
            for label, key in plan:
                seq = seqs.get(key)
                if not seq:
                    continue
                for win in sliding(seq, window):
                    feat = window_features(win, base)
                    if feat is None:
                        continue
                    # 动作类只保留"真的动了"的窗口；NONE 全保留
                    if label != "NONE" and max(abs(feat[0]), abs(feat[1])) < min_dev:
                        skipped[label] += 1
                        continue
                    samples.append(dict(x=feat, y=label, session=session,
                                        rnd=ri, seg=key))
    return samples, skipped


def build_samples_npz(data_dir, window, min_dev, include_none=False):
    """从 jumpduck_capture.py 的原始帧目录造样本。

    数据目录需含 labels.json + frames.npz（可直接给采集根目录，会递归找）。
    """
    samples = []
    skipped = Counter()
    roots = []
    if os.path.isfile(os.path.join(data_dir, "labels.json")):
        roots = [data_dir]
    else:
        for p in glob.glob(os.path.join(data_dir, "**", "labels.json"),
                           recursive=True):
            roots.append(os.path.dirname(p))
    if not roots:
        return None, skipped

    for d in roots:
        meta = json.load(open(os.path.join(d, "labels.json"), encoding="utf-8"))
        npz_path = os.path.join(d, "frames.npz")
        if not os.path.isfile(npz_path):
            continue
        z = np.load(npz_path)["z"]
        lo, hi = (meta.get("band") or [6, 21])[:2]
        session = os.path.basename(os.path.normpath(d))

        rounds = {}
        for s in meta.get("segments") or []:
            rounds.setdefault(s.get("round"), []).append(s)

        for rnd, segs in rounds.items():
            pitched = {}
            for s in segs:
                f0, f1 = int(s["frame_start"]), int(s["frame_end"]) + 1
                block = z[f0:max(f0 + 1, f1)]
                pitched[s["seg"]] = [pitch_from_frame(fr, lo, hi, 0, 1)
                                     for fr in block]
            bases = [np.asarray(pitched[k], float) for k in
                     ("base1", "base2", "base3")
                     if pitched.get(k)]
            bases = [b[np.isfinite(b)] for b in bases]
            bases = [b for b in bases if b.size]
            if not bases:
                continue
            base = float(np.median(np.concatenate(bases)))

            plan = [("JUMP", "jump"), ("DUCK", "duck")]
            if include_none:
                plan = [("NONE", "base1"), ("NONE", "base2"),
                        ("NONE", "base3")] + plan
            for label, key in plan:
                seq = [v for v in (pitched.get(key) or []) if np.isfinite(v)]
                if not seq:
                    continue
                for win in sliding(seq, window):
                    feat = window_features(win, base)
                    if feat is None:
                        continue
                    if label != "NONE" and max(abs(feat[0]), abs(feat[1])) < min_dev:
                        skipped[label] += 1
                        continue
                    samples.append(dict(x=feat, y=label, session=session,
                                        rnd=int(rnd or 0), seg=key))
    return samples, skipped


# ------------------------------------------------------------------ 先验判据校准

def build_dev_windows(data_dir, window):
    """返回 [(session, label, dev_array)]；dev 已减去本轮基线。"""
    rows = []

    # 原始帧优先
    roots = []
    if os.path.isfile(os.path.join(data_dir, "labels.json")):
        roots = [data_dir]
    else:
        roots = [os.path.dirname(p) for p in
                 glob.glob(os.path.join(data_dir, "**", "labels.json"),
                           recursive=True)]

    for d in roots:
        meta = json.load(open(os.path.join(d, "labels.json"), encoding="utf-8"))
        npz_path = os.path.join(d, "frames.npz")
        if not os.path.isfile(npz_path):
            continue
        z = np.load(npz_path)["z"]
        lo, hi = (meta.get("band") or [6, 21])[:2]
        session = os.path.basename(os.path.normpath(d))
        rounds = {}
        for s in meta.get("segments") or []:
            rounds.setdefault(s.get("round"), []).append(s)
        for rnd, segs in rounds.items():
            pitch = {}
            for s in segs:
                f0, f1 = int(s["frame_start"]), int(s["frame_end"]) + 1
                pitch[s["seg"]] = [pitch_from_frame(fr, lo, hi, 0, 1)
                                   for fr in z[f0:max(f0 + 1, f1)]]
            bases = [np.asarray(pitch[k], float) for k in
                     ("base1", "base2", "base3") if pitch.get(k)]
            bases = [b[np.isfinite(b)] for b in bases]
            bases = [b for b in bases if b.size]
            if not bases:
                continue
            base = float(np.median(np.concatenate(bases)))
            for label, key in (("JUMP", "jump"), ("DUCK", "duck")):
                seq = [v for v in (pitch.get(key) or []) if np.isfinite(v)]
                for win in sliding(seq, window):
                    rows.append((session, label, np.asarray(win, float) - base))

    if rows:
        return rows

    # 回退 result.json
    for p in sorted(glob.glob(os.path.join(data_dir, "*", "result.json"))):
        doc = json.load(open(p, encoding="utf-8"))
        session = os.path.basename(os.path.dirname(p))
        for rnd in doc.get("rounds") or []:
            seqs = {k: (rnd.get(k) or {}).get("pitch_seq") for k in SEGS}
            if not seqs["jump"] or not seqs["duck"]:
                continue
            bases = [np.asarray(seqs[k], float) for k in ("base1", "base2", "base3")
                     if seqs[k]]
            if not bases:
                continue
            base = float(np.median(np.concatenate(bases)))
            for label, key in (("JUMP", "jump"), ("DUCK", "duck")):
                for win in sliding(seqs[key], window):
                    rows.append((session, label, np.asarray(win, float) - base))
    return rows


def _prior_score(rows, params, act_thresh=8.0):
    """目标 = 动作窗口方向准确率 - 0.3 × 静止窗口误报率。"""
    j = PriorJudge()
    (j.min_dev, j.frame_t, j.max_up_frames, j.min_neg_frames,
     j.pos_over_neg, j.neg_over_pos) = params
    act_ok = act_n = still_fp = still_n = 0
    for _, label, dev in rows:
        moving = max(abs(float(dev.max())), abs(float(dev.min()))) >= act_thresh
        p, _, _ = j.judge(dev)
        if moving:
            act_n += 1
            act_ok += (p == label)
        else:
            still_n += 1
            still_fp += (p != "NONE")
    acc = act_ok / max(act_n, 1)
    fp = still_fp / max(still_n, 1)
    return acc - 0.3 * fp, acc, fp, act_n, still_n


def calibrate_prior(args):
    window = args.window
    rows = build_dev_windows(args.data, window)
    if not rows:
        print("没有可用数据")
        return 2
    sessions = sorted({r[0] for r in rows})
    print("校准先验判据：窗口 %d 帧，%d 个窗口，%d 个会话"
          % (window, len(rows), len(sessions)))

    grid = [(m, t, up, dn, pon, nop)
            for m in (7.0, 8.0, 9.0, 10.0, 11.0, 12.0)
            for t in (3.0, 4.0, 5.0)
            for up in (1, 2, 3)
            for dn in (1, 2, 3)
            for pon in (0.5, 0.6, 0.7, 0.8, 0.9)
            for nop in (1.0, 1.1, 1.2, 1.4)]

    print("搜索 %d 组参数……" % len(grid))

    # LOSO：参数只在训练会话上选
    hit = 0
    total = 0
    per = {}
    for s in sessions:
        tr = [r for r in rows if r[0] != s]
        te = [r for r in rows if r[0] == s]
        if not tr or not te:
            continue
        best = max(grid, key=lambda p: _prior_score(tr, p)[0])
        _, acc, fp, n_act, n_still = _prior_score(te, best)
        per[s] = dict(params=best, acc=acc, fp=fp,
                      n_act=n_act, n_still=n_still)
        hit += int(round(acc * n_act))
        total += n_act
        print("  %-18s 动作准确 %.1f%% (n=%d)  静止误报 %.1f%% (n=%d)  参数 %s"
              % (s, 100 * acc, n_act, 100 * fp, n_still,
                 "(min_dev=%.0f t=%.0f up=%d dn=%d pon=%.1f nop=%.1f)" % best))

    print("\nLOSO 动作窗口总体准确率 = %.1f%% (%d/%d)"
          % (100.0 * hit / max(total, 1), hit, total))

    # 全量最优 → 导出
    allbest = max(grid, key=lambda p: _prior_score(rows, p)[0])
    score, acc, fp, n_act, n_still = _prior_score(rows, allbest)
    print("全量最优：动作 %.1f%%  静止误报 %.1f%%  参数 %s"
          % (100 * acc, 100 * fp, allbest))

    j = PriorJudge(min_dev=allbest[0], frame_t=allbest[1],
                   max_up_frames=allbest[2], min_neg_frames=allbest[3],
                   pos_over_neg=allbest[4], neg_over_pos=allbest[5],
                   window=window)
    d = j.to_dict()
    d["metrics"] = dict(loso_action_accuracy=round(hit / max(total, 1), 4),
                        full_action_accuracy=round(acc, 4),
                        full_false_positive=round(fp, 4),
                        per_session={k: dict(acc=round(v["acc"], 4),
                                             params=list(v["params"]))
                                     for k, v in per.items()})
    d["trained_on"] = dict(data=os.path.relpath(args.data, ROOT),
                           windows=len(rows), sessions=sessions)
    d["usable"] = bool(hit / max(total, 1) >= 0.75)
    out = args.prior_out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print("已写入 %s（usable=%s）" % (out, d["usable"]))
    return 0


# ------------------------------------------------------------------ 模型

def softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_softmax(X, y_idx, n_class, l2=1.0, iters=1200, lr=0.4):
    n, k = X.shape
    W = np.zeros((k, n_class))
    b = np.zeros(n_class)

    # 类别权重：让三个类在损失里权重相近
    cnt = np.bincount(y_idx, minlength=n_class).astype(float)
    cw = np.where(cnt > 0, len(y_idx) / (n_class * np.maximum(cnt, 1)), 0.0)
    Y = np.zeros((n, n_class))
    Y[np.arange(n), y_idx] = 1.0

    for _ in range(iters):
        p = softmax(X @ W + b)
        g = (p - Y) * cw[None, :]
        W -= lr * (X.T @ g / n + l2 * W / n)
        b -= lr * g.mean(axis=0)
    return W, b


def fit_and_eval(train, test, labels, l2, iters):
    Xtr = np.array([s["x"] for s in train], float)
    ytr = np.array([labels.index(s["y"]) for s in train])
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0) + 1e-9
    W, b = fit_softmax((Xtr - mu) / sd, ytr, len(labels), l2=l2, iters=iters)

    pred = []
    for s in test:
        x = (np.array(s["x"], float) - mu) / sd
        p = softmax((x @ W + b)[None, :])[0]
        pred.append(labels[int(np.argmax(p))])
    return mu, sd, W, b, pred


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "vertical"))
    ap.add_argument("--out", default=os.path.join(ROOT, "models", "jumpduck.json"))
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help="滑窗帧数（2T4R 约 0.53 s/帧，默认 4 ≈ 2.1 s）")
    ap.add_argument("--min-dev", type=float, default=8.0,
                    help="动作窗口的最小偏离（度），低于它归为静止")
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--iters", type=int, default=1200)
    ap.add_argument("--enter", type=float, default=0.60)
    ap.add_argument("--three-class", action="store_true",
                    help="把静止(NONE)也作为一类训练；默认只训 JUMP vs DUCK，"
                         "静止由实时门限 min_dev 负责")
    ap.add_argument("--calibrate-prior", action="store_true",
                    help="不训模型，改为校准先验判据（EXPECTED_SPEC 那套阈值）")
    ap.add_argument("--prior-out", default=os.path.join(ROOT, "models",
                                                        "jumpduck-prior.json"))
    args = ap.parse_args()

    if args.calibrate_prior:
        return calibrate_prior(args)

    labels = list(CLASSES) if args.three_class else ["JUMP", "DUCK"]

    # 优先按「原始帧目录」读；没有就回退到旧的 result.json
    samples, skipped = build_samples_npz(args.data, args.window, args.min_dev,
                                         include_none=args.three_class)
    source = "原始帧 npz"
    if not samples:
        samples, skipped = build_samples(args.data, args.window, args.min_dev,
                                         include_none=args.three_class)
        source = "result.json (pitch_seq)"
    if len(samples) < 30:
        print("样本不足（%d）。--data 需指向 data/vertical 或含 frames.npz 的采集目录"
              % len(samples))
        return 2

    counts = Counter(s["y"] for s in samples)
    print("数据源：%s" % source)
    sessions = sorted({s["session"] for s in samples})
    print("样本 %d：%s" % (len(samples), dict(counts)))
    print("会话 %d 个：%s" % (len(sessions), ", ".join(sessions)))
    print("被丢弃的静止动作窗：%s" % dict(skipped))
    print("类别=%s  滑窗 %d 帧，动作门限 %.1f°，L2=%.2f\n"
          % (labels, args.window, args.min_dev, args.l2))

    # ---- 评估 ----
    all_true, all_pred = [], []
    per_session = {}
    if len(sessions) < 2:
        # 单次采集：无法做跨会话验证。退化为 5 折随机切分，
        # 只能证明"管线能学习"，不能证明"能泛化到新会话"。
        print("只有 1 个会话 → 用 5 折随机切分（⚠ 不是跨会话验证）")
        idx = np.arange(len(samples))
        np.random.default_rng(0).shuffle(idx)
        folds = np.array_split(idx, 5)
        for k, fold in enumerate(folds):
            test_set = set(int(i) for i in fold)
            test = [samples[i] for i in fold]
            train = [samples[i] for i in idx if int(i) not in test_set]
            if not train or not test:
                continue
            _, _, _, _, pred = fit_and_eval(train, test, labels, args.l2, args.iters)
            truth = [q["y"] for q in test]
            all_true += truth
            all_pred += pred
            hit = sum(a == b for a, b in zip(truth, pred))
            per_session["fold%d" % (k + 1)] = dict(n=len(test), hit=hit)
            print("  fold%d  %3d 样本  命中 %3d/%3d (%.0f%%)"
                  % (k + 1, len(test), hit, len(test), 100.0 * hit / len(test)))
    else:
        for s in sessions:
            train = [q for q in samples if q["session"] != s]
            test = [q for q in samples if q["session"] == s]
            if not train or not test:
                continue
            _, _, _, _, pred = fit_and_eval(train, test, labels, args.l2, args.iters)
            truth = [q["y"] for q in test]
            all_true += truth
            all_pred += pred
            hit = sum(a == b for a, b in zip(truth, pred))
            per_session[s] = dict(n=len(test), hit=hit)
            print("  %-18s %3d 样本  命中 %3d/%3d (%.0f%%)"
                  % (s, len(test), hit, len(test), 100.0 * hit / len(test)))

    n = len(all_true)
    acc = sum(a == b for a, b in zip(all_true, all_pred)) / n
    cm = np.zeros((len(labels), len(labels)), int)
    for t, p in zip(all_true, all_pred):
        cm[labels.index(t), labels.index(p)] += 1

    print("\n=== LOSO 混淆矩阵（行=真值，列=预测）===")
    print("        " + "".join("%8s" % c for c in labels))
    for i, c in enumerate(labels):
        print("%-8s" % c + "".join("%8d" % v for v in cm[i]))
    print("\n总体准确率 %.1f%%" % (100 * acc))
    for i, c in enumerate(labels):
        rec = cm[i, i] / max(cm[i].sum(), 1)
        prec = cm[i, i] / max(cm[:, i].sum(), 1)
        print("  %-5s 召回 %.1f%%  精确 %.1f%%  (n=%d)"
              % (c, 100 * rec, 100 * prec, cm[i].sum()))

    # 三类模式下，额外看"动作窗口里 JUMP/DUCK 是否分对"
    mask = np.array([t != "NONE" for t in all_true])
    act_acc = float(np.mean(np.array(all_pred)[mask] == np.array(all_true)[mask]))
    print("\n动作窗口（JUMP+DUCK）内的方向准确率 = %.1f%%" % (100 * act_acc))

    # ---- 用全部数据训最终模型 ----
    X = np.array([s["x"] for s in samples], float)
    y = np.array([labels.index(s["y"]) for s in samples])
    mu = X.mean(axis=0)
    sd = X.std(axis=0) + 1e-9
    W, b = fit_softmax((X - mu) / sd, y, len(labels), l2=args.l2, iters=args.iters)

    usable = bool(acc >= 0.75)
    model = SoftmaxModel(W, b, mu, sd, window=args.window,
                         enter=args.enter, exit_=1.0 - args.enter,
                         min_dev=args.min_dev, classes=labels, usable=usable,
                         metrics=dict(loso_accuracy=round(acc, 4),
                                      action_binary_accuracy=round(act_acc, 4),
                                      confusion={labels[i]: cm[i].tolist()
                                                 for i in range(len(labels))},
                                      per_session=per_session),
                         trained_on=dict(sessions=sessions, samples=len(samples),
                                         counts=dict(counts),
                                         data=os.path.relpath(args.data, ROOT)))
    out = model.save(args.out)
    print("\n模型已写入 %s" % out)

    if acc < 0.60:
        print("⚠ LOSO %.1f%%：模型不具备可用识别能力，只能用来跑通链路。" % (100 * acc))
        print("  模型已写入，但 usable=false；不要接演示（见 docs/jump-duck-recognizer.md §8）。")
    # 指标低是数据结论，不是执行失败
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
