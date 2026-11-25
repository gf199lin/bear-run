# -*- coding: utf-8 -*-
r"""左右移的机器学习判定（**可选，默认关闭**）：有效性闸门 + 12 维因果特征 + 小型 MLP。

为什么要单独成模块
------------------
`tools/compare_lane_models.py` 只是为了"比一比"，它在**整段文件**上向量化算特征。
真正上线必须两件事同时成立，否则离线数字就是假的：

  1. **训练和实时用同一份特征代码**——所以本模块是唯一实现，
     离线训练（tools/train_lane_ml.py）和在线判定（radar_actions.crit='ml'）都调用它；
  2. **特征必须是因果的**——第 t 帧只用 t 及以前的数据，不许用未来的整段中位数。

设计
----
    每帧 → [有效性闸门] → 有效 ? [MLP 概率] : 无证据
                                     ↓
                           交给 ActionRecognizer 原状态机
                        （confirm 3 帧 + 最短间隔 0.5 s + 释放确认）

闸门是"先判断信息是否有效"，只做 3 件可解释的事（阈值由训练数据定，写进模型 JSON）:
    present  本帧在带内有检出（px 非 NaN）
    det32    近 4 s 检出率 >= det32_min  —— 人得真的在视场里，而不是一两个野点
    sat      |px| < abs_px_max           —— 饱和（撞满量程 1.08）时 px 不再表示位置

纯 numpy 前向，不依赖 sklearn / torch：模型就是一个 JSON（权重 + 标准化参数 + 闸门）。
"""
import json
import os

import numpy as np

FRAME_PERIOD = 0.1295          # 1T2R 帧周期（与 radar_actions 的 1t2r 一致）
TAG = 'lane-mlp-1t2r'
MODEL_VERSION = 1
CLASSES = ('NONE', 'LEFT', 'RIGHT')

# 特征的顺序 = 模型 JSON 里的 features 顺序，改动必须同步 version
FEATURE_NAMES = ('dev', 'dev_04_16', 'd4', 'slope8', 'slope3', 'det8', 'det32',
                 'en_ratio', 'py_dev', 'npts4', 'jit16', 'px_minus_long')

# 闸门默认值（真正生效的是模型 JSON 里的 gate 段；这里只是构造时的兜底）
# ★ 三项都是**实测出来能区分的**，不是拍脑袋：
#   - det32：近 4 s 检出率。坏数据（带锁错门 / 人不在带内）实测 p50 只有 0.06~0.19，
#     好数据 0.44~0.88 ⇒ 0.15 只砍死的时段，几乎不伤真动作（动作段 p25=0.22）。
#   - abs_px：px 满量程 = 1.08。多个远处站位实测 p99 已达 1.15~1.19（撞满），
#     饱和时 px 不再表示位置，必须判无效。
#   - det8 / jit16 **刻意不用**：实测动作段与非动作段的 jit16 分布几乎重合
#     （中位 0.0549 vs 0.0552），det8 在动作段内 p25 只有 0.12 —— 用它们等于砍掉真动作。
GATE_DEFAULT = {
    'det32_min': 0.15,       # 近 4 s 检出率下限
    'abs_px_max': 1.05,      # 饱和门（满量程 1.08）
    'npts4_min': 0.0,        # 默认关闭（与 det32 重复且更粗）
    'det8_min': 0.0,         # 默认关闭（保留字段，便于以后按场景收紧）
    'jit16_max': 1.0,        # 默认关闭（同上）
}


# ---------------------------------------------------------------- 因果特征
def _ffill(x):
    """前向填充（NaN 沿用上一个有效值；开头沿用 0.0，与离线脚本一致）。"""
    out = np.asarray(x, dtype=float).copy()
    last = 0.0
    for i in range(len(out)):
        if np.isnan(out[i]):
            out[i] = last
        else:
            last = out[i]
    return out


def _rolling_median(x, w):
    """因果滚动中位（只用过去 w 帧，忽略 NaN）。"""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    for t in range(len(x)):
        a = max(0, t - w + 1)
        seg = x[a:t + 1]
        seg = seg[~np.isnan(seg)]
        if len(seg):
            out[t] = float(np.median(seg))
    return out


def _rolling_mean(x, w):
    x = np.asarray(x, dtype=float)
    v = ~np.isnan(x)
    xz = np.where(v, x, 0.0)
    cs = np.cumsum(xz)
    cn = np.cumsum(v.astype(float))
    out = np.full(len(x), np.nan)
    for t in range(len(x)):
        a = max(0, t - w + 1)
        nn = cn[t] - (cn[a - 1] if a > 0 else 0.0)
        if nn > 0:
            out[t] = (cs[t] - (cs[a - 1] if a > 0 else 0.0)) / nn
    return out


def _rolling_slope(x, w):
    """因果最小二乘斜率（只用过去 w 帧；窗内有 NaN 则本帧无斜率）。"""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    tt = np.arange(w, dtype=float)
    tt = tt - tt.mean()
    den = float((tt ** 2).sum())
    for t in range(len(x)):
        if t < w - 1:
            continue
        seg = x[t - w + 1:t + 1]
        if np.isnan(seg).any():
            continue
        out[t] = float((tt * (seg - seg.mean())).sum() / den)
    return out


def _lagged(x, k):
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:-k]
    return out


def build_feature_matrix(px, py, en, n, frame_period=FRAME_PERIOD):
    """由逐帧 px/py/energy/n 序列算 12 维因果特征，返回 (T, 12)。

    ★ 这是训练与实时**唯一**的特征实现。逐帧口径与
      `tools/compare_lane_models.py::build_features` 完全一致（该脚本是历史比对工具，
      新代码一律走本函数）。
    """
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    en = np.asarray(en, dtype=float)
    n = np.asarray(n, dtype=float)
    if px.size == 0:
        return np.zeros((0, len(FEATURE_NAMES)))

    nfr = lambda sec: max(2, int(round(sec / frame_period)))
    f = _ffill(px)
    med_s = _rolling_median(px, nfr(0.8))
    med_l = _rolling_median(px, nfr(40.0))
    ms = np.where(np.isnan(med_s), f, med_s)
    ml = np.where(np.isnan(med_l), f, med_l)
    dev = ms - ml

    med_04 = _rolling_median(px, nfr(0.4))
    med_16 = _rolling_median(px, nfr(1.6))
    med_04 = np.where(np.isnan(med_04), f, med_04)
    med_16 = np.where(np.isnan(med_16), f, med_16)

    m4 = _rolling_mean(px, 4)
    d4 = np.nan_to_num(m4 - _lagged(m4, 4))
    slope8 = np.nan_to_num(_rolling_slope(px, 8))
    slope3 = np.nan_to_num(_rolling_slope(px, 3))
    det8 = np.nan_to_num(_rolling_mean((~np.isnan(px)).astype(float), 8))
    det32 = np.nan_to_num(_rolling_mean((~np.isnan(px)).astype(float), 32))

    enl = _ffill(np.log10(np.maximum(en, 1e-6)))
    en_ratio = np.nan_to_num(_rolling_mean(enl, 4) - _rolling_mean(enl, 128))

    pyl = _ffill(py)
    py_med = _rolling_median(py, 64)
    py_med = np.where(np.isnan(py_med), pyl, py_med)
    py_dev = np.nan_to_num(pyl - py_med)

    npt = np.nan_to_num(_rolling_mean(n, 4))

    dp = np.diff(f, prepend=f[0])
    jit = np.nan_to_num(_rolling_mean(np.abs(dp), 16))

    X = np.column_stack([dev, med_04 - med_16, d4, slope8, slope3, det8, det32,
                         en_ratio, py_dev, npt, jit, f - ml])
    return np.nan_to_num(X)


# ---------------------------------------------------------------- 实时用：只算当前帧
def _tail(x, k):
    return x[-k:] if k < len(x) else x


def _med_ignore_nan(seg, fallback):
    seg = np.asarray(seg, dtype=float)
    seg = seg[~np.isnan(seg)]
    return float(np.median(seg)) if len(seg) else float(fallback)


def _mean_ignore_nan(seg):
    seg = np.asarray(seg, dtype=float)
    seg = seg[~np.isnan(seg)]
    return float(seg.mean()) if len(seg) else np.nan


def build_feature_row(px, py, en, n, frame_period=FRAME_PERIOD):
    """只算**最后一帧**的 12 维特征（实时路径用）。

    与 `build_feature_matrix(...)[-1]` 逐位等价（tools/ 里有一致性自检），
    但复杂度从 O(T·W) 降到 O(W)：7.7 Hz 下每帧约 0.2 ms，而整段重算是 ~0.3 s。
    传入的序列至少要覆盖长窗（40 s ≈ 309 帧），否则长窗统计会偏。
    """
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    en = np.asarray(en, dtype=float)
    n = np.asarray(n, dtype=float)
    T = px.size
    if T == 0:
        raise ValueError('空序列')
    nfr = lambda sec: max(2, int(round(sec / frame_period)))
    f = _ffill(px)
    ft = f[-1]

    med_s = _med_ignore_nan(_tail(px, nfr(0.8)), ft)
    med_l = _med_ignore_nan(_tail(px, nfr(40.0)), ft)
    dev = med_s - med_l

    med_04 = _med_ignore_nan(_tail(px, nfr(0.4)), ft)
    med_16 = _med_ignore_nan(_tail(px, nfr(1.6)), ft)

    m4 = _mean_ignore_nan(_tail(px, 4))
    m4p = _mean_ignore_nan(px[max(0, T - 8):T - 4]) if T > 4 else np.nan
    d4 = 0.0 if (np.isnan(m4) or np.isnan(m4p)) else float(m4 - m4p)

    def slope(w):
        if T < w:
            return 0.0
        seg = px[T - w:]
        if np.isnan(seg).any():
            return 0.0
        tt = np.arange(w, dtype=float)
        tt -= tt.mean()
        return float((tt * (seg - seg.mean())).sum() / (tt ** 2).sum())

    det8 = float(np.mean(~np.isnan(_tail(px, 8))))
    det32 = float(np.mean(~np.isnan(_tail(px, 32))))

    enl = _ffill(np.log10(np.maximum(en, 1e-6)))
    en4 = float(_tail(enl, 4).mean())
    en128 = float(_tail(enl, 128).mean())
    en_ratio = en4 - en128

    pyl = _ffill(py)
    py_med = _med_ignore_nan(_tail(py, 64), pyl[-1])
    py_dev = pyl[-1] - py_med

    npts4 = _mean_ignore_nan(_tail(n, 4))
    npts4 = 0.0 if np.isnan(npts4) else float(npts4)

    dp = np.abs(np.diff(_tail(f, 17)))
    jit = float(dp.mean()) if dp.size else 0.0

    row = np.array([dev, med_04 - med_16, d4, slope(8), slope(3), det8, det32,
                    en_ratio, py_dev, npts4, jit, ft - med_l], dtype=float)
    return np.nan_to_num(row)


# ---------------------------------------------------------------- 有效性闸门
def gate_mask(px, X, gate=None):
    """标记"这一帧的雷达信息能不能用"。

    px 为逐帧横向坐标原始序列（无检出为 NaN）；X 为 build_feature_matrix 的输出。
    返回 bool 数组：True = 有效，可作为证据送进分类器。

    为什么必须传 px：饱和度要看**当前** px，而它无法从 12 维特征里无损还原
    （特征里只有 px 与长窗中位之差）。
    """
    px = np.asarray(px, dtype=float)
    X = np.atleast_2d(np.asarray(X, dtype=float))
    return _gate_mask(px, X, dict(GATE_DEFAULT, **(gate or {})))


def _gate_mask(px, X, gate):
    """两项条件，都是实测能区分的（见 GATE_DEFAULT 注释）。

    ★ 刻意**不把"本帧有检出"当条件**：7.7 Hz 下检出率本来就只有 40~60%，
      要求"每帧都有点"会把一半的动作段判成无效（实测动作段内 present 只有 51.7%）。
      判定用的是 ~1 s 量级的证据，不是单帧。
    """
    det32 = X[:, 6]
    px_ff = _ffill(np.asarray(px, dtype=float))
    # px 序列可能比 X 长（实时只算最后一帧，却传了整段缓冲）：取对齐的尾部
    px_ff = px_ff[-len(X):] if len(px_ff) > len(X) else px_ff
    ok = det32 >= float(gate['det32_min'])
    ok &= np.abs(px_ff) < float(gate['abs_px_max'])
    if float(gate.get('det8_min', 0.0)) > 0:
        ok &= X[:, 5] >= float(gate['det8_min'])
    if float(gate.get('jit16_max', 1.0)) < 1.0:
        ok &= X[:, 10] <= float(gate['jit16_max'])
    if float(gate.get('npts4_min', 0.0)) > 0:
        ok &= X[:, 9] >= float(gate['npts4_min'])
    return ok


# ---------------------------------------------------------------- 模型
class LaneModel:
    """加载并前向一个"闸门 + MLP"模型（纯 numpy）。

    典型用法（radar_actions 内部）::

        m = LaneModel.from_file('models/lane_mlp.json')
        X = build_feature_matrix(px_buf, py_buf, en_buf, n_buf)
        valid = m.gate(px_buf, X)[-1]
        p = m.proba(X[-1:])   # (1, 3) → [NONE, LEFT, RIGHT]
    """

    def __init__(self, obj):
        if not isinstance(obj, dict):
            raise ValueError('模型必须是 JSON 对象')
        if int(obj.get('version', -1)) != MODEL_VERSION:
            raise ValueError('模型 version 不匹配（本代码只认 %d）: %r'
                             % (MODEL_VERSION, obj.get('version')))
        feats = tuple(obj.get('features') or ())
        if feats != FEATURE_NAMES:
            raise ValueError('模型特征与本代码不一致，拒绝加载:\n  模型 %r\n  代码 %r'
                             % (feats, FEATURE_NAMES))
        self.raw = obj
        self.tag = obj.get('tag', TAG)
        self.frame_period = float(obj.get('frame_period', FRAME_PERIOD))
        norm = obj['norm']
        self.mu = np.asarray(norm['mu'], dtype=float)
        self.sd = np.asarray(norm['sd'], dtype=float)
        mlp = obj['mlp']
        self.W1 = np.asarray(mlp['W1'], dtype=float)
        self.b1 = np.asarray(mlp['b1'], dtype=float)
        self.W2 = np.asarray(mlp['W2'], dtype=float)
        self.b2 = np.asarray(mlp['b2'], dtype=float)
        if self.W1.shape[0] != len(FEATURE_NAMES):
            raise ValueError('W1 形状与特征数不符: %r' % (self.W1.shape,))
        if self.W1.shape[1] != self.b1.size or self.W2.shape[1] != len(CLASSES):
            raise ValueError('W2/b1 形状不符: %r %r' % (self.W1.shape, self.W2.shape))
        self.gate_params = dict(GATE_DEFAULT, **(obj.get('gate') or {}))
        self.thr = float((obj.get('thr') or {}).get('on', 0.70))
        self.release = float((obj.get('thr') or {}).get('off', 0.45))

    @classmethod
    def from_file(cls, path):
        with open(path, 'r', encoding='utf-8') as f:
            return cls(json.load(f))

    def gate(self, px, X):
        return _gate_mask(np.asarray(px, dtype=float), np.atleast_2d(X), self.gate_params)

    def proba(self, X):
        X = np.atleast_2d(np.asarray(X, dtype=float))
        Z = (X - self.mu) / self.sd
        H = np.tanh(Z @ self.W1 + self.b1)
        logits = H @ self.W2 + self.b2
        logits = logits - logits.max(axis=1, keepdims=True)
        P = np.exp(logits)
        return P / P.sum(axis=1, keepdims=True)

    def predict(self, X):
        return self.proba(X).argmax(axis=1)

    def to_json(self):
        return self.raw


def save_model(obj, path):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=float)
    return path
