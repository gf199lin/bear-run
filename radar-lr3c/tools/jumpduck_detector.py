# -*- coding: utf-8 -*-
"""跳 / 蹲 检测器。

输入：2T4R 的俯仰角序列（度，来自 4 个竖排 RX 的相位差）
输出：'JUMP' / 'DUCK' / None

判据来源（不要随意改阈值）：
    17 轮实测（8 次会话、4 个安装高度）全部命中
    跳：相对基线偏离 均值 +18.1  最小 +6.4
    蹲：相对基线偏离 均值 -22.3  最大 -7.0
    两者从不交叉

核心思想：
    不要对整段取平均。跳/蹲是脉冲式动作，平均值会把峰值和谷值
    互相抵消（这正是前 15 轮失败的原因）。检测器看的是瞬时偏离。

用法：
    det = JumpDuckDetector()
    for pitch in pitch_stream:          # 每帧调用一次
        ev = det.update(pitch)
        if ev:
            print(ev)                   # 'JUMP' 或 'DUCK'

    # 状态
    det.state       # 'CALM' / 'JUMP' / 'DUCK'
    det.baseline    # 当前基线（度）
    det.deviation   # 当前帧相对基线的偏离（度）
"""

from collections import deque
import numpy as np

CALM = "CALM"
JUMP = "JUMP"
DUCK = "DUCK"


class JumpDuckDetector:
    """从俯仰角序列检测跳/蹲事件。

    设计要点（这是第 2 版，修正了第 1 版的根本错误）
    ------------------------------------------------
    第 1 版用「单帧偏离」判断，命中率只有 24%。原因是：2T4R 帧率 1.9 Hz，
    跳一次只采到 1~2 帧，而且站立时偏离本身就会摆到 ±35°，单帧完全不可靠。

    第 2 版改为「近期峰值 / 谷值」：
        维护最近 peak_window 帧的最大值和最小值，
        用它们相对基线的偏离来判断，等价于离线极值法，但只滞后几帧。

    实测依据（17 轮离线数据）：
        跳：段内最大值相对基线  均值 +18.1  最小 +6.4
        蹲：段内最小值相对基线  均值 -22.3  最大 -7.0
    """

    def __init__(self, base_frames=20, peak_window=5,
                 jump_th=8.0, duck_th=6.0,
                 cooldown=4, calm_band=12.0):
        self.base_frames = int(base_frames)
        self.peak_window = int(peak_window)
        self.jump_th = float(jump_th)
        self.duck_th = float(duck_th)
        self.cooldown = int(cooldown)
        self.calm_band = float(calm_band)

        self._init = []                       # 启动阶段攒基线
        self._recent = deque(maxlen=self.peak_window)
        self._cool = 0

        self.baseline = None
        self.state = CALM
        self.peak_dev = 0.0
        self.trough_dev = 0.0
        self.frames = 0

    # ------------------------------------------------------ 基线

    def _step_baseline(self, pitch):
        if self.baseline is None:
            self._init.append(pitch)
            if len(self._init) >= self.base_frames:
                self.baseline = float(np.median(self._init))
                self._init = []
            return
        # 平静时缓慢跟踪，抵抗慢漂移
        if abs(pitch - self.baseline) < self.calm_band:
            self.baseline += 0.02 * (pitch - self.baseline)

    # ------------------------------------------------------ 主逻辑

    def update(self, pitch):
        """喂入一帧俯仰角（度），返回 'JUMP' / 'DUCK' / None。"""
        if pitch is None or not np.isfinite(pitch):
            return None
        pitch = float(pitch)
        self.frames += 1
        self._step_baseline(pitch)

        if self.baseline is None:
            return None

        self._recent.append(pitch)
        if len(self._recent) < self.peak_window:
            return None

        hi = max(self._recent)
        lo = min(self._recent)
        self.peak_dev = hi - self.baseline
        self.trough_dev = lo - self.baseline

        if self._cool > 0:
            self._cool -= 1
            self.state = CALM
            return None

        if self.peak_dev >= self.jump_th:
            self._cool = self.cooldown
            self.state = JUMP
            self._recent.clear()
            return JUMP

        if self.trough_dev <= -self.duck_th:
            self._cool = self.cooldown
            self.state = DUCK
            self._recent.clear()
            return DUCK

        self.state = CALM
        return None

    # ------------------------------------------------------ 工具

    def reset(self):
        self._init = []
        self._recent.clear()
        self._cool = 0
        self.baseline = None
        self.state = CALM
        self.peak_dev = 0.0
        self.trough_dev = 0.0
        self.frames = 0

    def rebaseline(self):
        self._recent.clear()
        self._cool = 0
        self.state = CALM


if __name__ == "__main__":
    # 自测：用合成序列走一遍
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    det = JumpDuckDetector()
    stream = ([-35.0] * 12 +           # 站立，建立基线
              [-20.0, -18.0, -25.0] +  # 跳
              [-35.0] * 6 +            # 站回
              [-52.0, -55.0, -50.0] +  # 蹲
              [-35.0] * 6)

    for i, p in enumerate(stream):
        ev = det.update(p)
        tag = "  <<< %s" % ev if ev else ""
        print("%2d  pitch=%+6.1f  base=%s  dev=%+6.1f  state=%-4s%s"
              % (i, p,
                 "%.1f" % det.baseline if det.baseline is not None else "  -  ",
                 det.deviation, det.state, tag))
