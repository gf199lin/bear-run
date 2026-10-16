# -*- coding: utf-8 -*-
"""
近距（1~1.5 m）人体动作识别 —— **规则法**，不引入深度学习（AGENTS.md 硬约束 4）。

════════════════════════════════════════════════════════════════════════
★ 判定原则（用户 2026-10-16 明确强调；改本文件前必须先读这一段）
────────────────────────────────────────────────────────────────────────
我们识别的**不是**「人此刻在左边还是右边」（绝对位置），而是
**「人这一段时间往哪个方向移动了」** —— 即 **相对位移的方向**。

因此：
  * 判定的全部输入只有两个**运动/差分**量：
      `v_lat` = 滑窗内 `px(t)` 的稳健斜率（m/s，带符号）
      `dx`    = 滑窗内首尾锚点的稳健差值（m，带符号）
  * **绝对 `px` 数值不参与任何判定**：本文件不存在 `px > 0 → 右边` 这类比较。
    质心系统性偏在轴线一侧（实测可偏 +0.5 ~ +1.0 m，见
    `docs/progress-radar-pointcloud.md` §8 站位自检）**不影响结论**，
    只要「移动方向」正确即可。站位偏差只会压缩/放大位移幅度，不改变符号。
  * `self.lane` 是**上一次已确认的漂移方向**（latch 锁存），**不是人的绝对位置**；
    `CENTER` 的语义是「最近没有已确认的方向性漂移」，**不是**「人站在中间」。
  * 静止时**不得**产生 `LANE_*` 事件。已被回归测试钉住的**仅限恒定偏置**：
    `selftest.py` 测试 7 用「质心整体偏 −0.8 m 的静止序列」断言零事件。
    注意：**带噪声的静止序列尚未量化**——`dx`/`v_lat` 只抵消恒定分量，
    随机游走分量不会被抵消，其误报率需要真机静息采集 + 真值标注后才能标定
    （见 `docs/progress-radar-pointcloud.md` §7.18 ⑩）。在此之前不得声称
    「静止时任何 px 都不产生 LANE_*」。
  * 跳/蹲同理：判的是**向上的过程**还是**向下的过程**（能量轨迹的上升/下降走向，
    且需要连续保持），不是「现在高还是矮」这个静态量。

`selftest.py` 测试 7 用「质心整体偏 −0.8 m 的静止序列」断言零 `LANE_*` 事件、
用「偏置 +0.8 m 上仍然左移」断言仍能正确判左 —— 把这条原则钉成回归测试。
════════════════════════════════════════════════════════════════════════

为什么不能直接沿用现有流水线
1. 距离分辨率只有 **0.6813 m**、只有 8 个距离门：1~1.5 m 的目标落在
   **bin1(1.02 m)** 与 bin2(1.70 m) 之间，单靠「哪个距离门有点」分不出动作。
2. 目标几乎在正前方（方位角 ≈ 0）时，**径向速度几乎为零** —— 左右移动几乎不产生
   多普勒。所以「左/右」不能靠 `p['v']` 判，必须靠 **横向坐标随时间的漂移**判。
3. bin0/bin1 被雷达自身/外壳/线缆的近距强反射占据（实测 ≤1.4 m 占 95% 点数、
   90% 功率）→ 必须**限定距离带**，并只取带内的幅值加权质心，才不被杂波带跑。

因此本模块的做法：
  限定距离带 → 幅值加权横向质心 px → **降噪层** → 稳健斜率 v_lat / 稳健锚点位移 dx
  → 阈值 + 滞回 + 最短切换间隔 + 连续确认 → 事件

★ 降噪层（2026-10-16 新增；针对实测根因「位置估计噪声 > 真实位移」，见
  `docs/progress-radar-pointcloud.md` §7.15/§7.16）
  D1 点位级门控：带内相对功率门控 / 只取最强 K 点（默认关闭，需 `--res-pa-frac`/`--top-k`）
  D2 帧级野值剔除：|Δpx| 超过门限（`max_step`）即判野值，不入滑窗。
     **默认 0 = 关闭**（2026-10-16 改；原默认 0.50 m/帧 ≈ 3.9 m/s × 0.1295 s）。
     关闭原因：带内仅 0.1~2.4 点/帧时幅值加权质心本身不是连续轨迹，
     固定 0.5 m 门会把可算动作组从 10 组砍到 3~8 组并引入偏差；
     正确值应由 `radar_calibrate.py` 用现场实测 p99(|Δpx|) 折算。依据见
     `docs/progress-radar-pointcloud.md` §7.18 ⑥
  D3 失锁清窗：`TARGET_LOST` 时清空滑窗，避免丢失前的旧 px 污染重新捕获后的估计
  D4 稳健斜率：Theil–Sen（两两斜率取中位数）替代最小二乘，抗单点野值
  D5 稳健锚点：`dx` = 后半段 px 中位数 − 前半段 px 中位数，替代「末点 − 首点」
  D6 方向一致性：`v_lat` 与 `dx` 必须**同号**才允许动作（两个独立估计互相印证）
  这 6 条都只是**估计器层面的降噪**，不改变「按相对位移方向判定」这条原则。

★ 校准（配套工具 `radar_calibrate.py`，同一晚新增）
  实测噪声随站位/灵敏度差异极大（静息 |Δ| 中位 0.14~0.40 m、p95 0.84~2.32 m），
  固定阈值不可能通用。校准器用**与识别器完全相同的估计器**在同一套数据上量出噪声
  分布，再回填阈值 → 输出 `calib.json`，识别器用 `--calib` 加载。
  这样阈值从「拍脑袋的提案数字」变成「当场测出来的数字」。
  **注意**：校准只调阈值与距离带，不改变判定原则（仍是相对位移方向）。

输出事件名与项目「关键契约」一致（AGENTS.md）：
  `LANE_LEFT` / `LANE_RIGHT` / `JUMP` / `DUCK` / `TARGET_LOST` / `TARGET_BACK`

验证状态（证据纪律，禁止把计划写成结果）
- 左/右：判定逻辑有 `selftest.py` 测试 6/7 的合成用例（合成横向运动序列 → 断言事件），
  **真机真值仍待测**（`.deepworks/tmp/seg/` 与 `seg-stride/` 的 40 条真值可复算，
  但实测结论是「本硬件在 1~1.5 m 站位下幅移不可判」，见 progress §7.15/§7.16）。
- 蹲/跳：1T2R 无俯仰角、2T4R 链路未验证（AGENTS.md 事实基线），本模块只输出**候选**，且永远带
  `gated=True`；P0→T0→T1→T2 验证门未过前不得算作已实现功能。
- 全部阈值均为**提案，待小组确认**（见 `docs/radar-control-and-actions.md` §5 参数表）。
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from collections import deque

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

FRAME_PERIOD = 0.1295          # 实测帧周期 129.5 ms（≈7.7 Hz）

LANE_LEFT = 'LANE_LEFT'
LANE_RIGHT = 'LANE_RIGHT'
JUMP = 'JUMP'
DUCK = 'DUCK'
TARGET_LOST = 'TARGET_LOST'
TARGET_BACK = 'TARGET_BACK'

# 提案阈值（待小组确认）——改这里等于改算法行为，必须同步改文档
# BAND_DEFAULT 依据 2026-10-15 的一次实机录制（1500 帧，只读离线逐门实测；
# 该录制文件已按用户要求删除，逐帧特征留存于 outputs/wave1-actions.csv）。
# 距离门索引 j = round(r/0.6813 - 0.5)（radar_dsp: rng=(j+0.5)*0.6813）：
#   j=0 (0.34 m) 占用 98%、8 点/帧、中位 |v|=0.77 m/s —— TX/RX 泄漏近场门，
#                必须排除（否则被它拖满整条横向轨迹）
#   j=1 (1.02 m) 占用 73%、中位 pa 1.34e6、中位 |v|=0.05 m/s —— **就是人**。
#                带内质心实测 px≈-0.52、py≈0.88 → r≈1.02 m、∠≈-30°
#   j=2 (1.70 m) 占用仅 1%，但 p90 pa 4.25e7（偶发极强回波）—— 排除，避免偶发拖偏
# 故取 0.70~1.35 = 只保留 j=1。代价：人站到 1.4 m 以上会落进 j=2，
# 那种场景必须用 --band 1.35 2.05 另测（见 docs/radar-control-and-actions.md「局限」）。
#
# [!] 2026-10-16 复测与此处注释不一致（尚未解决，勿当结论）：在 seg/seg_01.00.bin
#     （同一站位 1.0 m，918 帧）上重测得到 j=1 占用 42.5%、0.65 点/帧、功率占比 9.6%，
#     而 j=2 占用 39.9%、功率占比 90.2%。即「标称站位距离」与「主回波所在距离门」
#     可能不是同一回事。两种可能（距离标定偏差 / 身后侧方强静态反射主导）**未区分**，
#     须先做站位自检 + 真值标注。详见 docs/progress-radar-pointcloud.md §7.18 ⑤。
# ⚠ 2026-10-15 实测修正：j=1 占用率并非 73%，两次独立真机采集实测仅 42.5%/20.7%/15.0%
#   （带内 0.6/0.2/0.2 点每帧）。**以「带内约 0.2~1.3 点/帧」为基线**。
BAND_DEFAULT = (0.70, 1.35)    # 只取 j=1(1.02 m)：实测人所在距离门
WINDOW_DEFAULT = 12            # 滑窗 ≈1.55 s（12 × 129.5 ms）
V_ON_DEFAULT = 0.35            # 触发用横向速度门限 m/s
V_OFF_DEFAULT = 0.18           # 滞回释放门限 m/s
MIN_DX_DEFAULT = 0.10          # 窗内横向净位移门限 m（防抖）
MIN_INTERVAL_DEFAULT = 0.5     # 最短切换间隔 s
CONFIRM_DEFAULT = 3            # 连续确认帧数
LOST_TIMEOUT_DEFAULT = 0.6     # 目标丢失判定 s
JUMP_RISE_DEFAULT = 1.9        # 回波能量相对基线上升倍数（候选）
DUCK_DROP_DEFAULT = 0.50       # 回波能量相对基线下降比例（候选）

# ---------------- 降噪层参数（2026-10-16 新增；提案，待小组确认） ----------------
# 人体横向移动的物理上限：即便大跨步/急停，躯干横向速度也很难超过 ~4 m/s；
# 4 m/s × 0.1295 s ≈ 0.52 m。超过这个单帧跳变的 px，只可能是杂波野值或重新捕获，
# 不可能是人的连续运动 → 判野值、不入滑窗。设 0 可关闭该门。
#
# ⚠ 默认值改为 0（关闭），依据 2026-10-16 的离线 A/B 实测（.deepworks/tmp/ab_denoise.py）：
#   带内点只有 0.1~2.4 点/帧 时，幅值加权质心**本身就不是连续轨迹** ——
#   每帧的 px 更接近「这一帧哪个点在门限之上」的随机抽样，帧间跳变经常 > 0.5 m。
#   此时 0.5 m 的物理门会把大部分帧判成野值并丢掉，可算出的动作组数从 10 组掉到
#   3~8 组，反而引入偏差。正确做法是**用现场实测的 p99(|Δpx|) 折算**
#   （见 radar_calibrate.py 的 rest 模式），而不是拍一个物理常数。
#   点云变密（带内 ≥ 数点/帧）之后，这个门才会重新变得有意义。
MAX_STEP_DEFAULT = 0.0         # m/帧；0 = 关闭（默认）。建议由校准器实测给出
RES_PA_FRAC_DEFAULT = 0.0      # 点位相对功率门控：只留 pa >= frac × 带内最大 pa（0=关闭）
TOP_K_DEFAULT = 0              # 每帧只保留带内最强的 K 个点（0=全保留）
SLOPE_DEFAULT = 'theilsen'     # 稳健斜率：'theilsen'（默认）| 'lstsq'（旧的最小二乘）
DX_MODE_DEFAULT = 'median'     # 位移锚点：'median'（稳健前后段中位数）| 'endpoints'（旧行为）
REQUIRE_SIGN_AGREE_DEFAULT = True   # v_lat 与 dx 必须同号才动作（D6 方向一致性）
# 跳/蹲候选：要求能量变化**连续保持** N 帧（判「过程」而不是判单帧毛刺）
JUMP_DUCK_CONFIRM_DEFAULT = 3


def load_calib(src):
    """载入校准结果：接受 dict、JSON 路径、或 None。

    返回 dict（至少含 version）；文件不存在时抛 FileNotFoundError，
    内容不是 JSON 对象时抛 ValueError —— 这两条都由调用方决定是否降级为警告。
    """
    if src is None:
        return None
    if isinstance(src, dict):
        return dict(src)
    with open(src, 'r', encoding='utf-8') as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError('校准文件顶层必须是 JSON 对象: %r' % (src,))
    return obj


class ActionRecognizer:
    """有状态的近距动作识别器：每帧 push 一次，返回特征与事件。

    ★ 判定只用「相对位移方向」（见文件头）：`v_lat`（稳健斜率）与 `dx`（稳健锚点差）。
      绝对 px 数值在本类里不做任何阈值比较。
    """

    def __init__(self, band=None, window=WINDOW_DEFAULT, min_pts=2,
                 confirm=CONFIRM_DEFAULT, v_on=None, v_off=None,
                 min_dx=None, min_interval=MIN_INTERVAL_DEFAULT,
                 lost_timeout=LOST_TIMEOUT_DEFAULT, jump_rise=JUMP_RISE_DEFAULT,
                 duck_drop=DUCK_DROP_DEFAULT, calib=None,
                 max_step=None, res_pa_frac=RES_PA_FRAC_DEFAULT,
                 top_k=TOP_K_DEFAULT, slope=SLOPE_DEFAULT, dx_mode=DX_MODE_DEFAULT,
                 require_sign_agree=REQUIRE_SIGN_AGREE_DEFAULT,
                 jump_duck_confirm=JUMP_DUCK_CONFIRM_DEFAULT):
        # ---- 先取校准值，再用显式参数覆盖（显式参数优先级最高） ----
        cal = load_calib(calib) or {}
        if band is None:
            band = cal.get('band', BAND_DEFAULT)
        if v_on is None:
            v_on = cal.get('v_on', V_ON_DEFAULT)
        if v_off is None:
            v_off = cal.get('v_off', V_OFF_DEFAULT)
        if min_dx is None:
            min_dx = cal.get('min_dx', MIN_DX_DEFAULT)
        if max_step is None:
            max_step = cal.get('max_step', MAX_STEP_DEFAULT)
        self.calib = cal or None

        if not (0 < band[0] < band[1]):
            raise ValueError('距离带非法: %r' % (band,))
        if slope not in ('theilsen', 'lstsq'):
            raise ValueError("slope 只能是 'theilsen' 或 'lstsq': %r" % (slope,))
        if dx_mode not in ('median', 'endpoints'):
            raise ValueError("dx_mode 只能是 'median' 或 'endpoints': %r" % (dx_mode,))
        self.band = (float(band[0]), float(band[1]))
        self.window = int(window)
        self.min_pts = int(min_pts)
        self.confirm = int(confirm)
        self.v_on = float(v_on)
        self.v_off = float(v_off)
        self.min_dx = float(min_dx)
        self.min_interval = float(min_interval)
        self.lost_timeout = float(lost_timeout)
        self.jump_rise = float(jump_rise)
        self.duck_drop = float(duck_drop)
        self.max_step = float(max_step)          # 0 = 关闭帧级野值剔除
        self.res_pa_frac = float(res_pa_frac)    # 0 = 关闭点位相对功率门控
        self.top_k = int(top_k)                  # 0 = 全保留
        self.slope = slope
        self.dx_mode = dx_mode
        self.require_sign_agree = bool(require_sign_agree)
        self.jump_duck_confirm = int(jump_duck_confirm)

        self.hist = deque(maxlen=self.window)     # (t, px|None, n, energy, rejected)
        self.lane = 'CENTER'
        self.events = 0
        self._dir_frames = 0
        self._last_flip = -1e9
        self._last_gated = -1e9
        self._target = False
        self._t_last_seen = None
        self._t_first = None
        self._last_px = None
        self._gated_run = 0        # 跳/蹲连续确认计数
        self._gated_side = 0       # +1 上升 / -1 下降 / 0 无
        self._gated_fired = 0      # 本次持续变化是否已报过候选（一次变化只报一次）

    # ------------------------------------------------------------ 校准便利构造
    @classmethod
    def from_calib(cls, calib, **overrides):
        """用校准文件构造识别器；overrides 里的显式参数优先。"""
        return cls(calib=calib, **overrides)

    # ------------------------------------------------------------------ 主入口
    def push(self, pts, t=None):
        """推入一帧点云，返回 {'t','lane','target','features','events'}。

        pts 元素须含 r/x/y/pa（或 p）；带外的点会被忽略。
        """
        now = time.time() if t is None else float(t)
        if self._t_first is None:
            self._t_first = now
        sel = [p for p in pts if self.band[0] <= p['r'] <= self.band[1]]
        sel = self._gate_points(sel)                 # D1 点位级门控
        out = []

        # ---------------- 目标存在性 ----------------
        if len(sel) < self.min_pts:
            lost = False
            if (self._target and self._t_last_seen is not None
                    and now - self._t_last_seen > self.lost_timeout):
                self._target = False
                lost = True
                out.append(self._evt(TARGET_LOST, now))
            if lost:
                # D3 失锁清窗：旧 px 与重新捕获后的 px 没有可比性，
                #    留着会让「相对位移」里混进一次假跳变。
                self._reset_track()
            self.hist.append((now, None, len(sel), 0.0, 0))
            return self._result(now, None, out)
        self._t_last_seen = now
        if not self._target:
            self._target = True
            out.append(self._evt(TARGET_BACK, now))

        # ---------------- 带内幅值加权特征 ----------------
        w = np.array([max(float(p.get('pa', p.get('p', 0.0))), 1e-9) for p in sel])
        x = np.array([float(p['x']) for p in sel])
        y = np.array([float(p['y']) for p in sel])
        r = np.array([float(p['r']) for p in sel])
        v = np.array([float(p['v']) for p in sel])
        deg = np.array([float(p['deg']) for p in sel])
        e_sum = float(w.sum())
        px = float((w * x).sum() / e_sum)
        py = float((w * y).sum() / e_sum)

        # ---------------- D2 帧级野值剔除 ----------------
        # 物理约束：人不可能在 129.5 ms 内横移 > max_step。超限只可能是杂波，
        # 或（更重要的）目标刚丢失又冷启动 —— 后者也不该拿来做「相对位移」。
        rejected = 0
        if self.max_step > 0 and self._last_px is not None:
            if abs(px - self._last_px) > self.max_step:
                rejected = 1
                self.hist.append((now, None, len(sel), e_sum, rejected))
                feat = {
                    'n': len(sel), 'px': round(px, 3), 'py': round(py, 3),
                    'rejected': rejected, 'step': round(px - self._last_px, 3),
                }
                return self._result(now, feat, out)
        self._last_px = px

        feat = {
            'n': len(sel), 'px': round(px, 3), 'py': round(py, 3),
            'r_mean': round(float((w * r).sum() / e_sum), 3),
            'v_rad': round(float((w * v).sum() / e_sum), 3),
            'deg_span': round(float(deg.max() - deg.min()), 1),
            'energy': round(e_sum, 1),
            'rejected': rejected,
        }
        self.hist.append((now, px, len(sel), e_sum, rejected))

        # ---------------- 相对位移估计（稳健） ----------------
        ts = [h[0] for h in self.hist if h[1] is not None]
        xs = [h[1] for h in self.hist if h[1] is not None]
        v_lat = None
        dx = None
        if len(ts) >= self.confirm and (ts[-1] - ts[0]) >= 0.25:
            tm = np.asarray(ts) - ts[-1]
            v_lat = self._slope(tm, np.asarray(xs))
            dx = self._delta_x(np.asarray(ts), np.asarray(xs))
            feat['v_lat'] = round(v_lat, 3)
            feat['dx'] = round(dx, 3)
            feat['n_win'] = len(ts)
            feat['px_mad'] = round(self._mad_step(np.asarray(xs)), 3)

        # ---------------- 左/右判定（相对位移方向 + 阈值 + 滞回 + 确认） ----
        # ★ 只用 v_lat / dx 的**符号与幅度**；从不比较 px 的绝对数值。
        moved = (v_lat is not None and abs(v_lat) >= self.v_on
                 and dx is not None and abs(dx) >= self.min_dx)
        drift = None
        if moved:
            s_v = 1 if v_lat > 0 else -1
            s_d = 1 if dx > 0 else -1
            if (not self.require_sign_agree) or (s_v == s_d):
                drift = s_v           # +1 = 向右漂移，-1 = 向左漂移（相对！）
                feat['drift'] = 'RIGHT' if drift > 0 else 'LEFT'
            else:
                # 两个独立估计互相矛盾 → 本帧不作为方向证据（降噪 D6）
                feat['drift'] = 'REJECT_CONFLICT'
        if moved and drift is not None:
            want = 'RIGHT' if drift > 0 else 'LEFT'
            if self.lane != want:
                self._dir_frames += 1
                if (self._dir_frames >= self.confirm
                        and now - self._last_flip >= self.min_interval):
                    self.lane = want
                    self._last_flip = now
                    self._dir_frames = 0
                    out.append(self._evt(LANE_LEFT if want == 'LEFT' else LANE_RIGHT,
                                         now, v_lat=v_lat, dx=dx))
            else:
                self._dir_frames = 0
        else:
            self._dir_frames = 0
            if self.lane != 'CENTER' and (v_lat is None or abs(v_lat) <= self.v_off):
                self.lane = 'CENTER'

        # ---------------- 跳/蹲：仅候选（受控能力，见文件头） ----------------
        # 用户强调「判过程/方向」：这里要求能量轨迹**连续 N 帧**朝同一方向变化
        # （升=起跳过程，降=下蹲过程），单帧毛刺不再产生候选。
        es = [h[3] for h in self.hist if h[2] > 0]
        if len(es) >= max(4, self.confirm):
            base = float(np.median(es))
            cand = 0
            ratio = None
            if base > 0:
                ratio = e_sum / base
                if ratio >= self.jump_rise:
                    cand = 1
                elif ratio <= self.duck_drop:
                    cand = -1
            if cand != self._gated_side:
                self._gated_side = cand
                self._gated_run = 1 if cand != 0 else 0
                if cand == 0:
                    self._gated_fired = 0        # 变化结束 → 允许下一次触发
            elif cand != 0:
                self._gated_run += 1
            if (cand != 0 and self._gated_run >= self.jump_duck_confirm
                    and self._gated_fired != cand
                    and now - self._last_gated >= self.min_interval):
                self._last_gated = now
                self._gated_fired = cand             # 一次持续变化只报一次
                name = JUMP if cand > 0 else DUCK
                out.append(self._evt(name, now, gated=True,
                                     ratio=round(float(ratio), 2),
                                     run=self._gated_run))

        return self._result(now, feat, out)

    # ------------------------------------------------------------------ 内部
    def _gate_points(self, sel):
        """D1 点位级门控：相对功率门控 + 只取最强 K 点（默认都关闭）。"""
        if not sel:
            return sel
        if self.res_pa_frac > 0:
            pa = [max(float(p.get('pa', p.get('p', 0.0))), 0.0) for p in sel]
            mx = max(pa)
            if mx > 0:
                sel = [p for p, a in zip(sel, pa) if a >= self.res_pa_frac * mx]
        if self.top_k > 0 and len(sel) > self.top_k:
            sel = sorted(sel, key=lambda q: -float(q.get('pa', q.get('p', 0.0))))[:self.top_k]
        return sel

    def _slope(self, tm, xs):
        """相对位移的稳健斜率（m/s）。tm 为「以末帧为 0」的负时间轴。"""
        if self.slope == 'lstsq':
            return float(np.polyfit(tm, xs, 1)[0])
        # Theil–Sen：所有点对的斜率取中位数。对单点野值极不敏感。
        n = len(tm)
        slopes = []
        for a in range(n - 1):
            for b in range(a + 1, n):
                dt = tm[b] - tm[a]
                if dt > 1e-9:
                    slopes.append((xs[b] - xs[a]) / dt)
        if not slopes:
            return 0.0
        return float(np.median(slopes))

    def _delta_x(self, ts, xs):
        """相对位移 dx（m）：窗尾若干样本中位数 − 窗首若干样本中位数。"""
        if self.dx_mode == 'endpoints':
            return float(xs[-1] - xs[0])
        k = max(1, len(xs) // 3)
        head = float(np.median(xs[:k]))
        tail = float(np.median(xs[-k:]))
        return tail - head

    @staticmethod
    def _mad_step(xs):
        """窗内逐帧 |Δpx| 的中位数（m）：噪声尺度的稳健度量，供校准/诊断用。"""
        if len(xs) < 2:
            return 0.0
        return float(np.median(np.abs(np.diff(xs))))

    def _reset_track(self):
        self.hist.clear()
        self._last_px = None
        self._dir_frames = 0
        self._gated_run = 0
        self._gated_side = 0
        self._gated_fired = 0
        self.lane = 'CENTER'

    def _evt(self, name, t, gated=False, **kw):
        self.events += 1
        d = {'name': name, 't': round(float(t), 3), 'seq': self.events}
        if gated:
            d['gated'] = True
            d['verified'] = False
            d['gate_note'] = ('跳/蹲未过 P0→T0→T1→T2 验证门（1T2R 无俯仰角、2T4R 链路未验证），'
                              '此处只是候选项，不得当验收结论')
        for k, val in kw.items():
            d[k] = round(float(val), 3) if isinstance(val, float) else val
        return d

    def _result(self, t, feat, out):
        return {'t': round(t, 3), 'lane': self.lane, 'target': self._target,
                'features': feat, 'events': out}


# ---------------------------------------------------------------- 离线分析
def analyze_file(path, processor=None, recognizer=None, band=BAND_DEFAULT,
                 limit=None, log=None):
    """对录制文件做离线动作分析。

    time 轴用 **帧号 × 0.1295 s**（等效真实节拍），不用墙钟 —— 离线跑得比实机快，
    墙钟时间会让「最短切换间隔」等时间门限失真。
    返回 (records, info)。
    """
    from radar_protocol import StreamParser, FrameAssembler
    from radar_dsp import PointCloudProcessor

    proc = processor or PointCloudProcessor()
    rec = recognizer or ActionRecognizer(band=band)
    parser = StreamParser()
    asm = FrameAssembler()
    records = []
    log = log or (lambda *a: None)
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            for rf in parser.feed(chunk):
                pair = asm.push(rf)
                if pair is None:
                    continue
                pts, noise = proc.process(*pair)
                t = asm.frames * FRAME_PERIOD
                r = rec.push(pts, t)
                r['frame'] = asm.frames
                r['noise'] = round(float(noise), 1)
                records.append(r)
                if limit is not None and len(records) >= limit:
                    break
            if limit is not None and len(records) >= limit:
                break
    info = {'frames': len(records), 'packets': parser.packets,
            'skipped': parser.skipped_bytes, 'bad': parser.bad_packets,
            'dropped': asm.dropped, 'file': os.path.basename(path)}
    log('[actions] %s → %d 帧' % (info['file'], info['frames']))
    return records, info


def summarize(records):
    counts = {}
    gated_counts = {}
    for r in records:
        for e in r['events']:
            key = e['name']
            if e.get('gated'):
                gated_counts[key] = gated_counts.get(key, 0) + 1
            else:
                counts[key] = counts.get(key, 0) + 1
    return {'events': counts, 'gated_candidates': gated_counts,
            'frames': len(records)}


# CSV 列定义：前 4 列固定，其余取自 features；新增列一律**追加在末尾**，
# 以便既有产物（outputs/wave1-actions.csv，16959 行）仍可按列名复算。
CSV_COLS = ['n', 'px', 'py', 'r_mean', 'v_rad', 'deg_span', 'energy',
            'v_lat', 'dx', 'noise', 'events',
            'rejected', 'n_win', 'px_mad', 'drift']


def write_csv(records, path):
    cols = ['frame', 't', 'lane', 'target'] + CSV_COLS
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for r in records:
            ft = r['features'] or {}
            row = ['', '', '', '']
            row[0] = r.get('frame')
            row[1] = r['t']
            row[2] = r['lane']
            row[3] = int(bool(r['target']))
            for k in CSV_COLS:
                if k == 'noise':
                    row.append(r.get('noise'))
                elif k == 'events':
                    row.append(';'.join(e['name'] for e in r['events']))
                else:
                    row.append(ft.get(k, ''))
            wr.writerow(row)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description='近距（1~1.5 m）动作识别 · 离线分析')
    ap.add_argument('--file', required=True, help='录制文件（.bin）')
    ap.add_argument('--band', type=float, nargs=2, default=None,
                    metavar=('RMIN', 'RMAX'), help='距离带（米），默认 0.70 1.35')
    ap.add_argument('--calib', default=None,
                    help='校准文件（radar_calibrate.py 产出的 calib.json）；'
                         '其中的 band/v_on/v_off/min_dx/max_step 会被采用，'
                         '显式命令行参数优先')
    ap.add_argument('--min-range', type=float, default=None,
                    help='点云级近距屏蔽（米），0=关闭（内置默认 0.0）')
    ap.add_argument('--min-snr', type=float, default=None,
                    help='相对噪声底 SNR 门限 dB（内置默认 7.0）')
    ap.add_argument('--cfar', type=float, default=None,
                    help='CFAR 门限倍数（内置默认 2.6）')
    ap.add_argument('--max-step', type=float, default=None,
                    help='帧级野值门限（米/帧），0=关闭（内置默认 0=关闭；'
                         '建议由 radar_calibrate.py 实测 p99(|Δpx|) 给出）')
    ap.add_argument('--res-pa-frac', type=float, default=None,
                    help='点位相对功率门控（0~1），0=关闭')
    ap.add_argument('--top-k', type=int, default=None, help='每帧只取带内最强 K 点，0=全取')
    ap.add_argument('--slope', choices=['theilsen', 'lstsq'], default=None,
                    help='横向速度估计器，默认 theilsen（稳健）')
    ap.add_argument('--dx-mode', choices=['median', 'endpoints'], default=None,
                    help='位移锚点，默认 median（稳健）')
    ap.add_argument('--no-sign-agree', action='store_true',
                    help='关闭「v_lat 与 dx 必须同号」的一致性门（不建议）')
    ap.add_argument('--limit', type=int, default=None, help='只分析前 N 帧（调试用）')
    ap.add_argument('--csv', default=None, help='逐帧特征写出到 CSV')
    ap.add_argument('--json', default=None, help='事件与摘要写出到 JSON')
    ap.add_argument('--events-only', action='store_true', help='只打印事件行')
    args = ap.parse_args(argv)

    from radar_dsp import PointCloudProcessor
    cal = load_calib(args.calib) if args.calib else {}
    psec = (cal or {}).get('processor', {}) or {}
    # 优先级统一为：显式命令行参数 > calib.json > 内置默认。
    # 因此处理器相关参数用 None 作 argparse 默认值（None = 用户没传），
    # 不能用 2.6/7.0 当默认，否则无从区分「用户显式传了 7.0」与「没传」，
    # 会把 calib.json 的值悄悄盖掉（或反过来让 calib 盖掉显式参数）。
    proc = PointCloudProcessor(
        cfar_scale=(args.cfar if args.cfar is not None
                    else psec.get('cfar_scale', 2.6)),
        min_snr=(args.min_snr if args.min_snr is not None
                 else psec.get('min_snr', 7.0)),
        min_range=(args.min_range if args.min_range is not None
                   else psec.get('min_range', 0.0)),
        # 注意：本 CLI 没有 --angle-sign/--angle-max 参数（符号归 radar_calibrate.py 的
        # sign 模式管），所以这里必须用 getattr 取默认 None，不能写 args.angle_sign，
        # 否则会 AttributeError。
        angle_sign=(getattr(args, 'angle_sign', None)
                    if getattr(args, 'angle_sign', None) is not None
                    else psec.get('angle_sign', 1.0)),
        angle_max=(getattr(args, 'angle_max', None) or psec.get('angle_max', 45.0)),
    )
    over = {}
    if args.max_step is not None:
        over['max_step'] = args.max_step
    if args.res_pa_frac is not None:
        over['res_pa_frac'] = args.res_pa_frac
    if args.top_k is not None:
        over['top_k'] = args.top_k
    if args.slope is not None:
        over['slope'] = args.slope
    if args.dx_mode is not None:
        over['dx_mode'] = args.dx_mode
    if args.no_sign_agree:
        over['require_sign_agree'] = False
    band = tuple(args.band) if args.band else None
    rec = ActionRecognizer(band=band, calib=cal or None, **over)
    print('[actions] 估计器: slope=%s dx_mode=%s max_step=%.2f sign_agree=%s '
          'res_pa_frac=%.2f top_k=%d'
          % (rec.slope, rec.dx_mode, rec.max_step, rec.require_sign_agree,
             rec.res_pa_frac, rec.top_k))
    print('[actions] 距离带 %s  阈值 v_on=%.2f v_off=%.2f min_dx=%.2f%s'
          % (rec.band, rec.v_on, rec.v_off, rec.min_dx,
             '  （来自 %s）' % args.calib if args.calib else '  （内置提案值）'))

    records, info = analyze_file(args.file, processor=proc, recognizer=rec,
                                 limit=args.limit, log=lambda m: print(m))

    if not args.events_only:
        print('文件: %s  帧数: %d  坏包: %d  丢弃帧: %d  跳过字节: %d'
              % (info['file'], info['frames'], info['bad'], info['dropped'],
                 info['skipped']))
    for r in records:
        for e in r['events']:
            tag = '  [候选·未验证]' if e.get('gated') else ''
            extra = ''
            if 'v_lat' in e:
                extra = '  v_lat=%.2f m/s  dx=%.2f m' % (e['v_lat'], e['dx'])
            elif 'ratio' in e:
                extra = '  能量比=%.2f' % e['ratio']
            print('帧 %5d  t=%7.2fs  %-12s%s%s' % (r.get('frame', 0), e['t'],
                                                   e['name'], tag, extra))
    s = summarize(records)
    print('摘要: %s' % json.dumps(s, ensure_ascii=False))
    if args.csv:
        print('逐帧特征已写出: %s' % write_csv(records, args.csv))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump({'info': info, 'summary': s,
                       'events': [dict(e, frame=r.get('frame')) for r in records
                                  for e in r['events']]}, f, ensure_ascii=False,
                      indent=2)
        print('事件 JSON 已写出: %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
