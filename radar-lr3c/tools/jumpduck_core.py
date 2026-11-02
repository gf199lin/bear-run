# -*- coding: utf-8 -*-
"""跳 / 蹲 识别的共享核心：特征、模型、实时状态机。

设计约束（为什么这样切）
------------------------
1. **训练与推理必须用同一个特征函数**。一旦离线用"整段统计"、在线用
   "单帧判断"，上线必然崩。所以这里只暴露 `window_features()`，
   训练脚本和 ws 桥都调它。
2. 特征是**相对基线**的偏离。基线由调用方维护（离线=该轮基线，
   实时=启动期中位数 + 慢跟踪）。
3. 模型是纯 numpy 的 softmax 回归，权重存 JSON。
   不引入 sklearn/torch —— 现场机器只要 numpy 就能跑。

类别
----
    NONE / JUMP / DUCK

模型文件（models/jumpduck.json）
--------------------------------
    {"version":1, "window":4, "features":[...], "classes":[...],
     "mu":[...], "sd":[...], "W":[[...]], "b":[...],
     "enter":0.60, "exit":0.40, "min_dev":8.0,
     "trained_on":{...}, "metrics":{...}}
"""
import json
import os

import numpy as np

FEATURE_NAMES = ["hi", "lo", "mean", "std", "maxdiff", "range"]
CLASSES = ["NONE", "JUMP", "DUCK"]
DEFAULT_WINDOW = 3
DEFAULT_ENTER = 0.60
DEFAULT_EXIT = 0.40
DEFAULT_MIN_DEV = 8.0


# ------------------------------------------------------------------ 特征

def window_features(seq, base):
    """一窗俯仰角（度）+ 基线 → 定长特征向量。

    参数
    ----
    seq  : 长度 window 的俯仰角序列（度）
    base : 该时刻的基线（度）

    返回
    ----
    list[float]，顺序与 FEATURE_NAMES 一致。
    """
    v = np.asarray(seq, dtype=float)
    if v.size == 0 or not np.isfinite(base):
        return None
    dev = v - base
    d = np.diff(v) if v.size > 1 else np.array([0.0])
    return [
        float(dev.max()),          # hi   向上最大偏离
        float(dev.min()),          # lo   向下最大偏离
        float(dev.mean()),         # mean 窗口平均偏离
        float(dev.std()),          # std  窗口内波动
        float(np.abs(d).max()),    # maxdiff 单帧最大跳变
        float(v.max() - v.min()),  # range 窗口峰谷差
    ]


# ------------------------------------------------------------------ 模型

class SoftmaxModel(object):
    """softmax 回归，权重来自训练脚本。

    classes 默认 ["JUMP","DUCK"]：**方向**由模型分，
    "有没有动作"由识别器的 min_dev 门限分。这样模型不会学成
    "全都判 NONE"（在弱数据上三类一起训时会退化）。
    """

    def __init__(self, W, b, mu, sd, window=DEFAULT_WINDOW,
                 enter=DEFAULT_ENTER, exit_=DEFAULT_EXIT,
                 min_dev=DEFAULT_MIN_DEV, metrics=None, trained_on=None,
                 classes=None, usable=False):
        self.W = np.asarray(W, dtype=float)          # (k, C)
        self.b = np.asarray(b, dtype=float)          # (C,)
        self.mu = np.asarray(mu, dtype=float)        # (k,)
        self.sd = np.asarray(sd, dtype=float)        # (k,)
        self.window = int(window)
        self.enter = float(enter)
        self.exit = float(exit_)
        self.min_dev = float(min_dev)
        self.metrics = metrics or {}
        self.trained_on = trained_on or {}
        self.classes = list(classes or ["JUMP", "DUCK"])
        # usable=False 表示训练指标没到门槛，只可用于跑通链路，不得接入演示
        self.usable = bool(usable)

    # -- 推理 ---------------------------------------------------------
    def predict_proba(self, feat):
        if feat is None:
            return None
        x = (np.asarray(feat, dtype=float) - self.mu) / self.sd
        z = x @ self.W + self.b
        z = z - z.max()
        e = np.exp(z)
        return e / e.sum()

    def predict(self, feat):
        p = self.predict_proba(feat)
        if p is None:
            return "NONE", 0.0
        i = int(np.argmax(p))
        if float(p[i]) < self.enter:
            return "NONE", float(p[i])
        return self.classes[i], float(p[i])

    # -- 持久化 -------------------------------------------------------
    def to_dict(self):
        return {
            "version": 1,
            "window": self.window,
            "features": list(FEATURE_NAMES),
            "classes": list(self.classes),
            "mu": self.mu.tolist(),
            "sd": self.sd.tolist(),
            "W": self.W.tolist(),
            "b": self.b.tolist(),
            "enter": self.enter,
            "exit": self.exit,
            "min_dev": self.min_dev,
            "usable": self.usable,
            "metrics": self.metrics,
            "trained_on": self.trained_on,
        }

    def save(self, path):
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("features") != FEATURE_NAMES:
            raise ValueError("模型特征顺序与当前代码不一致：%s" % d.get("features"))
        return cls(d["W"], d["b"], d["mu"], d["sd"],
                   window=d.get("window", DEFAULT_WINDOW),
                   enter=d.get("enter", DEFAULT_ENTER),
                   exit_=d.get("exit", DEFAULT_EXIT),
                   min_dev=d.get("min_dev", DEFAULT_MIN_DEV),
                   metrics=d.get("metrics"),
                   trained_on=d.get("trained_on"),
                   classes=d.get("classes"),
                   usable=d.get("usable", False))


# ------------------------------------------------------------------ 先验判据

# 这是"期望的数据形态"——判据按它写，验收也按它查。
# 之所以先写判据再看数据：现有采集不满足第 1、2 条，所以方向必然判不准。
EXPECTED_SPEC = {
    "frame_rate_hz": 5.0,        # 跳一次 0.6~0.9 s，>=5 Hz 才有 3~4 个有效帧
    "stand_range_m": (1.5, 2.0),
    "jump": {
        "up_peak_deg": 8.0,      # 相对基线的上冲幅度
        "up_frames": (1, 2),     # 上冲只持续 1~2 帧（短促）
        "peak_ratio": 0.6,       # 上冲幅度 / 预蹲深度，跳应 >= 0.6
    },
    "duck": {
        "down_peak_deg": 8.0,
        "down_frames": (2, 5),   # 下沉需 >= 2 帧（持续）
        "trough_ratio": 1.2,     # 下沉幅度 / 站起幅度，对应 >= 1.2
    },
    # 现实板卡能否满足上面的 frame_rate_hz —— 不满足时 jump 分支整体不可信
    "measured": {
        "2t4r_frame_period_s": 0.52084,   # 实测（seg_01.00.labels.jsonl _meta）
        "2t4r_frame_rate_hz": 1.92,
        "1t2r_frame_period_s": 0.1295,
        "1t2r_frame_rate_hz": 7.72,
    },
    "viability": (
        "EXPECTED_SPEC 要求 >=5 Hz；2T4R 实测 1.92 Hz，腾空 0.4~0.5 s 只能采到 0~1 帧，"
        "跳的 up_frames(1,2) 在 2T4R 上退化为 1 帧，max_up_frames=3 等于不限。"
        "要采满 3 帧需腾空 >=1.04 s（跳高 >1.3 m），人类做不到。"
        "结论：2T4R 可测「蹲」（持续姿势），测不了「跳」（瞬时动作）；"
        "详见 docs/jump-duck-sampling-limit.md。"
    ),
}


class PriorJudge(object):
    """按"期望的数据形态"判跳/蹲，不依赖训练。

    判据（全部可解释，`reason` 会带进 ws 帧）：

      JUMP ← 上冲明显（hi ≥ min_dev）
             且上冲帧数 ≤ max_up_frames（短促）
             且上冲幅度 ≥ pos_over_neg × 预蹲深度
      DUCK ← 下沉明显（|lo| ≥ min_dev）
             且低位帧数 ≥ min_neg_frames（持续）
             且下沉深度 ≥ neg_over_pos × 站起幅度

    两个"比例"条件是关键：跳和蹲都会先向下（预蹲 / 下蹲），
    只看极值方向会把跳全判成蹲。跳的区别是**向上那一半够强且够短**。
    """

    def __init__(self, min_dev=8.0, frame_t=4.0, max_up_frames=2,
                 min_neg_frames=2, pos_over_neg=0.6, neg_over_pos=1.2,
                 window=DEFAULT_WINDOW, cooldown=2):
        self.min_dev = float(min_dev)
        self.frame_t = float(frame_t)
        self.max_up_frames = int(max_up_frames)
        self.min_neg_frames = int(min_neg_frames)
        self.pos_over_neg = float(pos_over_neg)
        self.neg_over_pos = float(neg_over_pos)
        self.window = int(window)
        self.cooldown = int(cooldown)

    def judge(self, dev):
        """dev = 窗口内俯仰角相对基线的偏离（度）。返回 (label, reason, metrics)。"""
        dev = np.asarray(dev, float)
        if dev.size == 0:
            return "NONE", "empty", {}
        hi, lo = float(dev.max()), float(dev.min())
        pos = float(dev[dev > 0].sum())
        neg = float(-dev[dev < 0].sum())
        pf = int((dev > self.frame_t).sum())
        nf = int((dev < -self.frame_t).sum())
        m = dict(hi=hi, lo=lo, pos=pos, neg=neg, up_frames=pf, down_frames=nf)

        if max(abs(hi), abs(lo)) < self.min_dev:
            return "NONE", "below_min_dev", m

        # JUMP：上冲够强、够短，且明显强于预蹲
        if (hi >= self.min_dev and pf <= self.max_up_frames
                and pos >= self.pos_over_neg * neg):
            return "JUMP", "up_peak_short_strong", m

        # DUCK：下沉够深、够久，且明显重于站起
        if (lo <= -self.min_dev and nf >= self.min_neg_frames
                and neg >= self.neg_over_pos * pos):
            return "DUCK", "down_trough_sustained", m

        return "NONE", "ambiguous", m

    def to_dict(self):
        return dict(min_dev=self.min_dev, frame_t=self.frame_t,
                    max_up_frames=self.max_up_frames,
                    min_neg_frames=self.min_neg_frames,
                    pos_over_neg=self.pos_over_neg,
                    neg_over_pos=self.neg_over_pos,
                    window=self.window, cooldown=self.cooldown,
                    expected=EXPECTED_SPEC)

    @classmethod
    def load(cls, path):
        d = json.load(open(path, encoding="utf-8"))
        keys = ["min_dev", "frame_t", "max_up_frames", "min_neg_frames",
                "pos_over_neg", "neg_over_pos", "window", "cooldown"]
        return cls(**{k: d[k] for k in keys if k in d})


# ------------------------------------------------------------------ 实时状态机

class JumpDuckRecognizer(object):
    """把逐帧俯仰角变成 NONE / JUMP / DUCK。

    行为
    ----
    - 启动后前 base_frames 帧只攒基线，不出事件。
    - 窗满才推理；窗内 |偏离| 都小于 min_dev 时直接判 NONE（防静止误触发）。
    - 触发后进入 cooldown 帧冷却，避免同一次动作连发。
    - 基线只在"平静"时慢跟踪（|dev| < calm_band），否则不动。
    """

    def __init__(self, model, base_frames=20, cooldown=3, calm_band=12.0,
                 use_model=True):
        self.model = model
        self.base_frames = int(base_frames)
        self.cooldown = int(cooldown)
        self.calm_band = float(calm_band)
        self.use_model = bool(use_model)

        self._init = []
        self._win = []
        self._cool = 0
        self._latched = False     # 已触发、还没平静：禁止同一动作重复出事件
        self.baseline = None
        self.state = "NONE"
        self.last_prob = None
        self.frames = 0

    # -- 基线 ---------------------------------------------------------
    def _step_baseline(self, pitch):
        if self.baseline is None:
            self._init.append(pitch)
            if len(self._init) >= self.base_frames:
                self.baseline = float(np.median(self._init))
                self._init = []
            return
        if abs(pitch - self.baseline) < self.calm_band:
            self.baseline += 0.02 * (pitch - self.baseline)

    # -- 主入口 -------------------------------------------------------
    def update(self, pitch):
        """喂一帧俯仰角（度）。返回 dict 或 None（还没准备好）。"""
        if pitch is None or not np.isfinite(pitch):
            return None
        pitch = float(pitch)
        self.frames += 1
        self._step_baseline(pitch)
        if self.baseline is None:
            return None

        win = int(getattr(self.model, "window", DEFAULT_WINDOW))
        self._win.append(pitch)
        if len(self._win) > win:
            self._win = self._win[-win:]
        if len(self._win) < win:
            return None

        feat = window_features(self._win, self.baseline)
        if feat is None:
            return None

        model = self.model
        min_dev = float(getattr(model, "min_dev", DEFAULT_MIN_DEV))
        # 动作门限：窗口里没有任何像样的偏离 → 直接 NONE
        moving = max(abs(feat[0]), abs(feat[1])) >= min_dev
        dev = np.asarray(self._win, float) - self.baseline

        prob = None
        reason = None
        label = "NONE"
        if moving and isinstance(model, PriorJudge):
            # 先验判据：不依赖训练，输出可解释的 reason
            label, reason, prob = model.judge(dev)
            self.last_prob = prob
        elif moving and self.use_model and model is not None:
            prob = model.predict_proba(feat)
            if prob is not None:
                # 模型只负责方向（JUMP/DUCK）；有无动作由上面的 min_dev 门限决定
                self.last_prob = {c: float(v) for c, v in zip(model.classes, prob)}
                i = int(np.argmax(prob))
                if float(prob[i]) >= model.enter and model.classes[i] in ("JUMP", "DUCK"):
                    label = model.classes[i]
                else:
                    label = "NONE"
        elif moving:
            # 无模型时的兜底规则（仅用于链路自测，不作为识别结论）
            label = "JUMP" if feat[0] >= -feat[1] else "DUCK"
            reason = "fallback_extremum"

        # 边沿锁存：一次动作只出一次事件。
        # 触发后必须等到窗口彻底平静（moving=False）才解锁，
        # 否则跳一次会在动作持续期间连发 2~3 次，游戏里就是"跳三下"。
        if self._latched:
            if not moving:
                self._latched = False
            label = "NONE"

        # 冷却：兜底，抑制刚解锁就立刻再触发
        if self._cool > 0:
            self._cool -= 1
            if label != "NONE":
                label = "NONE"

        if label != "NONE":
            self._latched = True
            self._cool = self.cooldown
        self.state = label
        return {
            "state": label,
            "reason": reason,
            "features": dict(zip(FEATURE_NAMES, feat)),
            "baseline": self.baseline,
            "prob": self.last_prob,
            "moving": bool(moving),
        }

    def reset(self):
        self._init = []
        self._win = []
        self._cool = 0
        self._latched = False
        self.baseline = None
        self.state = "NONE"
        self.last_prob = None
        self.frames = 0


# ------------------------------------------------------------------ 俯仰角

def pitch_from_frame(z, band_lo=6, band_hi=21, rx_a=0, rx_b=1):
    """从一帧原始复数据算俯仰相位差（度）。

    z 形状 (rx, chirps, bins)。与 tools/jump_duck_guide.py 的
    pitch_series() 完全同口径：带内每个距离门做圆平均，再按能量加权。

    只用两元相位差的已知代价：灵敏度低、±180° 外会卷绕。
    换成 4 元 DBF 需要原始帧，见 docs/jump-duck-recognizer.md。
    """
    z = np.asarray(z)
    if z.ndim != 3 or z.shape[0] <= max(rx_a, rx_b):
        return None
    # 距离门数由板型/配置决定（2T4R=32，1T2R 的某些配置更少），
    # 不能假设 band_hi 一定在范围内，否则换板就 IndexError。
    n_bins = int(z.shape[2])
    lo = max(0, min(int(band_lo), n_bins - 1))
    hi = max(lo + 1, min(int(band_hi), n_bins))
    band_lo, band_hi = lo, hi

    bins = list(range(band_lo, band_hi))
    w = (np.abs(z) ** 2).sum(axis=(0, 1))[band_lo:band_hi].astype(float)
    if w.sum() <= 0:
        return None
    vec = np.empty(len(bins), dtype=complex)
    for i, b in enumerate(bins):
        a = np.angle(z[rx_b, :, b] * np.conj(z[rx_a, :, b]))
        vec[i] = np.exp(1j * a).mean()
    return float(np.degrees(np.angle((vec * w).sum())))
