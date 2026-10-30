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

# 控制台容错：cmd.exe 默认 cp936，打印 '⇒' / '★' 这类字符会抛
# UnicodeEncodeError 把整个分析进程打成 exit 1（看起来像「分析失败」，实际只是
# 行首字符编不出来）。用 ? 替代即可 —— 数字和结论都在，别让日志炸掉流程。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

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
WINDOW_DEFAULT = 8             # 滑窗 ≈1.04 s（8 × 129.5 ms）；原 12 帧（1.55 s），
#   2026-10-20 实测：8 帧把「onset → 首个事件」中位从 0.78 s 压到 0.52 s（1.50 m 段），
#   静息 36 s 上误报 0（§7.45）。提案，待小组确认。
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

# ---------------- 「静息水平偏移」判据（2026-10-18 新增；提案，待小组确认） ----------------
# 背景与实测依据：docs/progress-radar-pointcloud.md §7.26 ~ §7.28。
#   原判据（crit='delta'，默认）量的是**窗内**相对位移 v_lat/dx，已在真值数据上实测
#   无法区分左右（`seg_01.25`/`seg_01.50` 召回 0/10，§7.15）。根因：真值 t_start 是
#   **提示音时刻**，而人起步到位需要 0.4~1.9 s（§7.26.3）——窗内口径把「人还没动」
#   当成了位移起点，系统性压缩真实位移。
#   新判据（crit='drift'）改用**时间基线**作参考系：在线跟踪静息水平 base
#   （近 rest_win 秒内检出帧 px 的中位数），再判「当前 px 相对 base 的偏移 d」。
#   ★ 它依然是**相对量**（判定只用到 `px - base`），不比较 px 的绝对数值 ——
#     与原判据「绝对 px 不参与判定」的原则一致，区别只是参考系由「窗内起点」
#     换成「时间基线」。实测（同一真值集）：1.25 m 命中 9/10、误报 1 次(0.5/min)，
#     优于绝对阈值变体；置换检验 p≤0.0033（详 §7.27/§7.28）。
#   ⚠ 边界：base 需要一段静息预热（约 10~20 s）才收敛；阈值属**样本内**选定。
#     LEFT 在 1.50 m 站位上仍检不到（根因是回波弱、目标丢失，不是方向判错）。
CRIT_DEFAULT = 'xbase'          # 2026-10-20 改为 'xbase'（原 'delta'）。理由见 §7.45.6：
#   1.50/2.00 m 实采上 'delta' 的**方向符号与人体左右相反**（「向自己右侧移动」被判成 LANE_LEFT，
#   0/11 方向正确）；同批数据 'xbase' 方向正确 10/11、延迟 0.84~1.04 s。'delta' 仍保留可选。
#   代价：'xbase' 需要 ~40 s 历史（near 0.8 s vs long 40 s 中位），冷启动前 40 s 不出动作事件。
REST_WIN_DEFAULT = 60.0        # 静息水平基线的滑动窗口 s（提案值；越大越稳、预热越久）
# ★ 2026-10-19 重标定 DRIFT_LEFT（10/19 外样本 3 段 + 样本内 2 段，4 站位 × 10 窗）
#   现象：外样本里「左移」几乎检不到，检出的事件全被标成 RIGHT。
#   机理**不是**极性反转，而是**幅度档位标定错**——证据（.deepworks/tmp/sign_probe.py、
#   scan_drift_left.py 的输出）：
#     1) 符号不携带方向信息：LEFT 20 窗中 19 个「负向占优」、**0 个正向占优**；
#        RIGHT 20 窗中 11 个负向占优、**0 个正向占优**。两种动作的 d 都是**负向**。
#        ⇒ 曾设想的「按符号判定（负→LEFT / 正→RIGHT）」会把两个方向都判成 LEFT，
#          已验证不成立，故**未采用**。
#     2) 方向只在 |d| 的**幅度**里，而幅度尺度**随站位变**：
#          LEFT  0.70 m ≈ 1.0~1.3 m；0.85 m ≈ 0.8~1.2 m；1.25 m ≈ 2.2 m
#          RIGHT 各站位 ≈ 0.25~0.35 m（稳定）
#     3) 原值 1.30 正好卡在近站位 LEFT 的峰值上（窗内 min 多为 −1.25~−1.32），
#        且触发需连续 3 帧 ⇒ LEFT 落进「小负偏移」档，被标成 RIGHT。
#   重标定结果（同一真值集，只改 drift_left）：LEFT 7/20 → 15/20，
#   误报 18 → 12，RIGHT 18/20 → 16/20。
#   ★ 结构**未改**：仍是「d ≤ −drift_left → LEFT；−drift_left < d ≤ −drift_right → RIGHT」。
#   ★ 0.80 本身是**样本内**扫出的值（提案，待小组确认），且未在未知站位上验证。
#   ★ crit 默认仍是 'delta'：本改动只影响 crit='drift' 的离线分析/实验路径。
DRIFT_LEFT_DEFAULT = 0.80      # d <= -DRIFT_LEFT → LEFT（大负偏移）※提案值，待小组确认
DRIFT_RIGHT_DEFAULT = 0.25     # -DRIFT_LEFT < d <= -DRIFT_RIGHT → RIGHT（小负偏移）
DRIFT_RELEASE_DEFAULT = 0.10   # 释放门：d 回到 -该值以内即释放回 CENTER（滞回）
                               # ★ 必须满足 0 <= DRIFT_RELEASE < DRIFT_RIGHT。
                               #   2026-10-19 修（devlog §7.33）：此前释放条件直接
                               #   复用了 DRIFT_RIGHT，且释放语句只写在 not moved
                               #   分支 ⇒ hold 恒为假、lane 永不回 CENTER，同方向
                               #   连续动作在没有 TARGET_LOST 间隔时被静默吞掉
                               #   （外样本 3/10；修后 6/10，误报 0）。
                               #   取值 0.10 是在 3 个文件上扫出的**样本内**最佳值。
DRIFT_RELEASE_CONFIRM_DEFAULT = 3
"""释放端连续确认帧数 M：d 必须连续 M 帧回到 -DRIFT_RELEASE 以上才释放回 CENTER。

为什么需要（2026-10-19 新增，devlog §7.36 有逐帧证据）
----------------------------------------------------
释放门只判**单帧**时，一次**真实的单帧 px 野值**就能把 lane 打回 CENTER，接着
d 又落回触发门以下 ⇒ 3 帧确认**再触发一次** ⇒ 「一次动作被记两次」。
实测现场（`.deepworks/tmp/seg/seg_01.50.bin`，top_k=1）：
    第 554 帧触发 RIGHT（px=0.735，base=1.059 ⇒ d=-0.324）
    第 559 帧 px 单帧跳到 1.023（base=1.034 ⇒ d=-0.011）→ 单帧跨过释放门
    第 561~563 帧 d 又落回 -0.324/-0.276/-0.269 → 再次 3 帧确认
    ⇒ 多打一条 `RIGHT@72.91`（与 `RIGHT@71.74` 相隔 1.17 s），在窗级口径里
      因为真值窗已被用掉而计成**误报**（seg_01.50 误报 1 → 0）。
静息站位同理：`pos_0p70.bin` 的 px 在 0.644 ↔ -0.718 之间跳（单帧跳变 1.36 m），
也会诱发同样的跨门。

根因不是「动作前后穿越门槛抖动」，而是：top_k=1 的 px 是**单帧最强点的 x**，
站立时帧间噪声就有 ±0.15 m（0.70 m 站位更大），而实测站立偏移 |d| ≈ 0.27 m
**恰好压在触发门 0.25 上** ⇒ 每个 RIGHT 都是边缘事件，噪声一抖就跨门。

取值理由：与触发端已有的 `CONFIRM_DEFAULT = 3` **对称**（都是 0.39 s 确认，
帧周期 0.1295 s）。A/B 实测（`.deepworks/tmp/ab_dup_variants.py`，8 候选 × 7 文件）：
    M=1（旧行为）  seg_01.50 误报 1；M=2 与 M=3 **结果完全一致**：误报 0。
    ★ 且 7 个文件里**其它事件逐条完全不变**（只少了那条重复事件）：
      样本内 1.25 m 9/10、样本内 1.50 m 4/10、外样本 1.25 m 6/10、静息 4 站位 0 事件。
被否决的候选（同一 A/B 里实测更差，勿再试）：
    px 三帧中值（去野值）：1.50 m 多召回 1 条，但 1.25 m 新增 1 条误报、
      静息 0.70 m 由 2 条恶化到 4 条 ⇒ 否决。
    野值帧剔除（|Δpx| > 0.52 m/帧 ⇒ 本帧无方向证据）：1.25 m LEFT 由 4 条掉到 0 条
      ⇒ 反证 LEFT 的真实单帧位移本就 >0.52 m（这也是 MAX_STEP 默认 0 的原因）。
⚠ M=1 = 旧的「单帧即释放」行为（对照用，仍会「一次动作被记两次」）；
  M=0 非法（会在清零后又判 0>=0 ⇒ hold 恒真，比旧版更激进地释放）。
本参数只作用于 crit='drift'。
"""

# ---------------- 「段内自适应基线」判据 crit='xbase'（2026-10-20 新增；提案，待小组确认）
# 背景与实测依据：docs/progress-radar-pointcloud.md §7.40（本节新增）与
#   .deepworks/tmp/cap1020_selfbase.py / cap1020_adaptive.py 的输出
#   （_cap1020_selfbase.txt、_cap1020_adaptive.txt）。
#
# 为什么还要再加一个判据：crit='delta' 与 crit='drift' 都在真机真值上失败过——
#   delta（窗内位移）：15 动作段命中 21%、延迟中位 4.92 s（对照见下）；
#   drift（全程静息基线）：LEFT 检不到，根因是「静息段与动作段相隔数分钟，
#     人的站位/姿态已发生慢漂移」⇒ 用**早先采的静息水平**去比**现在**的 px，
#     参考系本身就是错的（实测 LEFT 窗 x 均值 0.442、RIGHT 窗 0.192，
#     而静息 base = 0.502 —— 两个方向相对 base 都是负偏，只是幅度不同）。
#
# xbase 的做法：把参考系从「早先采的静息段」换成**该段自身的近期水平**——
#     dev = median(近 xbase_win 秒的 px) − median(近 xbase_base_win 秒的 px)
#   两项都是同一段数据里现算的，因此**免疫站位/姿态慢漂移**（这是与 drift 的
#   唯一区别，也是它有效的原因）。判定仍然只用**相对量** dev，不比较 px 绝对数值，
#   与文件头的判定原则一致。
#
# 离线实测（cap-long-1020，1T2R，人站 0.7 m，真值 7 LEFT + 7 RIGHT 动作段
#   共 384 s；另有 366 s 纯静息段，其中任何 LANE_* 都算误报）：
#     产线 crit='delta' 现状        命中  7/30 ≈ 21%(旧数据)   延迟中位 4.92 s
#     crit='drift'   绝对静息基线    命中 6/14 = 43%           延迟中位 1.81 s   误报 0.7/min
#     crit='xbase'   段内自适应基线  命中 13/14 = 93%          延迟中位 1.81 s   误报 7.2/min
#     crit='xbase' + 左右独立阈值    命中 12/14 = 86%          延迟中位 1.94 s   误报 3.5/min
#   ⇒ 命中率提升主要来自「段内自适应基线」；误报控制主要来自「左右独立阈值」。
#
# ★ 左右阈值必须独立（这是本判据与 delta/drift 的结构差异）：
#   实测左右幅度**不对称**。阈值对称（都用 0.12）时 LEFT 7/7 全中、RIGHT 6/7；
#   阈值提到 0.18 时 RIGHT 7/7 全中而 LEFT 掉到 1/6 ⇒ 同一个门限不可能同时适配两侧。
#   物理原因：人横移时目标回波只剩约 2 点/帧（静息 3 点、跳 7 点、蹲 5 点），
#   且 LEFT 动作窗口里最多 20% 的帧**整帧无点**（RIGHT 为 0%）⇒ LEFT 的位移幅度
#   被系统性地压缩。这一点属于几何/站位问题，须重采数据确认（见 §7.40 待办）。
#
# ★ 符号约定（本判据已用真值标定，勿凭直觉改）：
#   在这套安装几何下实测 **人向自己的左侧移动 ⇒ dev > 0**（LEFT 7/7 命中于 dev>0）。
#   注意这与 crit='delta' 的约定**相反**（delta 把 v_lat>0 判成 RIGHT）——
#   delta 的该约定从未被真值验证过，且 delta 本身在本几何下不可用。
#   该符号依赖安装几何与 angle_sign；换站位/换角度后必须重新用真值标定。
#
# ⚠ 边界：本判据只在 1T2R + 单人 + 单一安装几何 + 0.7 m 站位上验证过；
#   2T4R、其它站位与房间均未验证。默认值 crit 仍是 'delta'（不改既有回归行为），
#   本判据须显式 `--crit xbase` 或 crit='xbase' 才启用。
CRIT_XBASE = 'xbase'
XBASE_WIN_DEFAULT = 0.80        # 近窗（秒）：dev 的「当前位置」项
XBASE_BASE_WIN_DEFAULT = 40.0   # 长窗（秒）：dev 的「近期常态」项；越大越稳
# ★ 2026-10-21 由 0.10 上调到 0.14（依据 docs/静止误报诊断-左门与噪声.md）。
#   原因：0.10 **恰好等于** 0.70 m 站位静息噪声的 p90（实测正侧 p90=0.102）
#   ⇒ 人站着不动时单帧越过左门的机会 5.1%（右门仅 1.0%），实测误报
#   **2.46 次/分且 80% 是 LEFT**（用户现场报告「静止却判向左」）。
#   提到 0.14 后：静息误报 **1.0 次/分（−60%）**，且**延迟不受影响**
#   （0.78 s 恒定——延迟由证据累积决定，不由门限决定）；代价是 LEFT 检出
#   由 2/2 降到 1/2（该站位 LEFT 证据峰值 p50=0.170 与静息噪声 p99=0.182
#   本就重叠，抬门限必然折损 LEFT 召回）。
#   ⚠ 仍属**样本内**结论（0.70 m / 单日单人 / 366 s 静息），待复采重新标定；
#     该值只影响 crit='xbase'，对 delta/drift 无作用。
XBASE_THR_LEFT_DEFAULT = 0.14   # dev >= +该值 → LEFT（LEFT 幅度小于右侧，门限须低于右门）
XBASE_THR_RIGHT_DEFAULT = 0.18  # dev <= -该值 → RIGHT（实测 RIGHT 幅度偏大，可用高门限）
XBASE_RELEASE_DEFAULT = 0.05    # 释放门：|dev| 回到该值以内即释放回 CENTER（滞回）
                                # ★ 必须满足 0 <= XBASE_RELEASE < min(两侧阈值)，
                                #   否则退化成「无滞回」，动作前后穿越门槛会重复触发。
XBASE_RELEASE_CONFIRM_DEFAULT = 3   # 释放端连续确认帧数（与触发端 confirm 对称）
XBASE_MIN_SAMPLES = 8           # 长窗最少样本数；不足时**不产生任何方向证据**
XBASE_MIN_SHORT = 3             # 近窗最少样本数；不足时本帧无证据

# ---------------- 距离带自动选择（2026-10-19 新增；提案，待小组确认） --------
# 为什么需要它：出厂默认带 `BAND_DEFAULT = (0.70, 1.35)` 圈在 j=1（门中心
#   1.02 m），而实测真实站位**根本不落在该门**：1.25 m 站位 48% 的检出在 j=2、
#   1.50 m 站位 65% 在 j=2。用默认带 ⇒ 0/10 命中且**主动误报 3 次**；
#   换成 j=2 的带（1.36~2.04 m）⇒ 命中 8/10、误报 0。详 §7.28.3。
# 做法：开机后先只做「数点数」——按距离门统计检出点数，把 j>=1 中点数最多的
#   那一门当作距离带；预热期**不发任何车道事件、也不算 px**（没有带就没有
#   横向坐标，宁可不判也不猜）。
# 离线实测（`.deepworks/tmp/probe_autoband.py`，两个真值文件）：
#   两站位都从第 20 帧起 j=2 即居首；按下面的规则锁定在第 40 / 30 帧
#   （5.2 s / 3.9 s），**早于**首条真值窗（第 53 帧 = 6.86 s），因此不吞动作。
AUTO_BAND = 'auto'                    # 传给 band= 的字符串，启用自动选带
AUTO_BAND_MIN_FRAMES_DEFAULT = 30     # 最早允许锁定的帧数（≈3.9 s）
AUTO_BAND_MAX_FRAMES_DEFAULT = 120    # 兜底：到这一帧必须锁定（≈15.5 s）
AUTO_BAND_MIN_POINTS_DEFAULT = 20     # 允许锁定所需的最少累计检出点数
AUTO_BAND_DOMINANCE_DEFAULT = 1.5     # 首选门须 >= 该倍数 × 次选门才提前锁定
AUTO_BAND_MIN_GATE = 1                # 排除 j=0 近场泄漏门（同 min_range=0.681）
RANGE_RES_MIRROR = 0.6813             # 镜像 radar_dsp.RANGE_RES（import 失败时用）
RANGE_BINS_MIRROR = 8                 # 镜像 radar_dsp.BINS
BAND_READY = 'BAND_READY'             # 信息事件：距离带已选定（**不是游戏动作**）


def _is_auto_band(band):
    """band 是否为「自动选带」标记（字符串 'auto'，大小写与空白不敏感）。"""
    return isinstance(band, str) and band.strip().lower() == AUTO_BAND


def _range_grid():
    """距离门的几何 (门宽 m, 门数)。

    优先取 `radar_dsp` 的真实常量，保证与 DSP 层永远一致；只有在
    import 不可用时才退到镜像值（这样本模块仍可被单独 import 做自检）。
    """
    try:
        from radar_dsp import BINS, RANGE_RES
        return float(RANGE_RES), int(BINS)
    except Exception:
        return RANGE_RES_MIRROR, RANGE_BINS_MIRROR


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
                 jump_duck_confirm=JUMP_DUCK_CONFIRM_DEFAULT,
                 crit=CRIT_DEFAULT, rest_win=REST_WIN_DEFAULT,
                 drift_left=DRIFT_LEFT_DEFAULT, drift_right=DRIFT_RIGHT_DEFAULT,
                 drift_release=DRIFT_RELEASE_DEFAULT,
                 drift_release_confirm=DRIFT_RELEASE_CONFIRM_DEFAULT,
                 xbase_win=XBASE_WIN_DEFAULT,
                 xbase_base_win=XBASE_BASE_WIN_DEFAULT,
                 xbase_thr_left=XBASE_THR_LEFT_DEFAULT,
                 xbase_thr_right=XBASE_THR_RIGHT_DEFAULT,
                 xbase_release=XBASE_RELEASE_DEFAULT,
                 xbase_release_confirm=XBASE_RELEASE_CONFIRM_DEFAULT,
                 auto_band_min_frames=AUTO_BAND_MIN_FRAMES_DEFAULT,
                 auto_band_max_frames=AUTO_BAND_MAX_FRAMES_DEFAULT,
                 auto_band_min_points=AUTO_BAND_MIN_POINTS_DEFAULT,
                 auto_band_dominance=AUTO_BAND_DOMINANCE_DEFAULT):
        # ---- 先取校准值，再用显式参数覆盖（显式参数优先级最高） ----
        cal = load_calib(calib) or {}
        # band='auto' 既可以显式传，也可以写在 calib.json 的 band 字段里
        # （写成字符串 "auto"）。两者都表示「预热期自己数点数选带」。
        auto = _is_auto_band(band) or _is_auto_band(cal.get('band'))
        if not auto and band is None:
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

        if not auto and not (0 < band[0] < band[1]):
            raise ValueError('距离带非法: %r' % (band,))
        if slope not in ('theilsen', 'lstsq'):
            raise ValueError("slope 只能是 'theilsen' 或 'lstsq': %r" % (slope,))
        if dx_mode not in ('median', 'endpoints'):
            raise ValueError("dx_mode 只能是 'median' 或 'endpoints': %r" % (dx_mode,))
        if crit not in ('delta', 'drift', CRIT_XBASE):
            raise ValueError("crit 只能是 'delta'、'drift' 或 'xbase': %r" % (crit,))
        if not rest_win > 0:
            raise ValueError('rest_win 必须 > 0: %r' % (rest_win,))
        # ---- crit='xbase' 的几何约束（无条件校验，手误当场报错）----
        if not float(xbase_win) > 0:
            raise ValueError('xbase_win 必须 > 0: %r' % (xbase_win,))
        if not float(xbase_base_win) > float(xbase_win):
            raise ValueError('须满足 xbase_base_win > xbase_win（长窗必须严长于近窗）: %r, %r'
                             % (xbase_base_win, xbase_win))
        if not float(xbase_thr_left) > 0 or not float(xbase_thr_right) > 0:
            raise ValueError('xbase 两侧触发门限都必须 > 0: %r, %r'
                             % (xbase_thr_left, xbase_thr_right))
        # 滞回：释放门必须严格小于**较小**的一侧触发门，否则退化成「无滞回」。
        if not 0.0 <= float(xbase_release) < min(float(xbase_thr_left),
                                                 float(xbase_thr_right)):
            raise ValueError('须满足 0 <= xbase_release < min(thr_left, thr_right): '
                             '%r, %r, %r' % (xbase_release, xbase_thr_left,
                                             xbase_thr_right))
        if int(xbase_release_confirm) < 1:
            raise ValueError('xbase_release_confirm 必须 >= 1（1 = 单帧即释放）: %r'
                             % (xbase_release_confirm,))
        if not drift_right < drift_left:
            raise ValueError('须满足 drift_right < drift_left: %r, %r'
                             % (drift_right, drift_left))
        # 释放门必须严格小于触发门才构成滞回（>= 触发门就退化成「无滞回」，
        # 动作前后 d 穿越阈值会额外打出重复事件，devlog §7.33.5）。
        if not 0.0 <= drift_release < drift_right:
            raise ValueError('须满足 0 <= drift_release < drift_right: %r, %r'
                             % (drift_release, drift_right))
        if int(drift_release_confirm) < 1:
            raise ValueError('drift_release_confirm 必须 >= 1（1 = 旧的单帧即释放；'
                             '0 会让 hold 恒真）: %r' % (drift_release_confirm,))
        # 这四个参数**无条件校验**（不管是否启用自动选带）：这样
        # `auto_band_min_frames=0` 这类手误会当场报错，而不是被
        # 「反正没启用」静默吞掉。
        if int(auto_band_min_frames) < 1:
            raise ValueError('auto_band_min_frames 必须 >= 1: %r'
                             % (auto_band_min_frames,))
        if int(auto_band_max_frames) < int(auto_band_min_frames):
            raise ValueError('须满足 auto_band_max_frames >= '
                             'auto_band_min_frames: %r, %r'
                             % (auto_band_max_frames, auto_band_min_frames))
        if int(auto_band_min_points) < 1:
            raise ValueError('auto_band_min_points 必须 >= 1: %r'
                             % (auto_band_min_points,))
        if not float(auto_band_dominance) >= 1.0:
            raise ValueError('auto_band_dominance 必须 >= 1.0: %r'
                             % (auto_band_dominance,))
        # ★ band='auto' 时 self.band 在锁定前是 None：此刻还没有「带」这个
        #   概念，任何用到 self.band 的代码都必须先看 self._band_locked。
        self.band = None if auto else (float(band[0]), float(band[1]))
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

        # ---- crit='drift' 用：静息水平基线（时间参考系）与阈值 ----
        # ⚠ `_rest` 刻意**不随 _reset_track()/TARGET_LOST 清空**：静息水平描述的是
        #    「站位 + 安装几何」这一场景常量，不是「目标轨迹」。清掉会让每次重新
        #    捕获后都要重新预热，反而制造假位移。
        self.crit = crit
        self.rest_win = float(rest_win)
        self.drift_left = float(drift_left)
        self.drift_right = float(drift_right)
        self.drift_release = float(drift_release)
        # 释放端连续确认帧数（0 = 单帧即释放）。计数器见 push 的 drift 分支。
        self.drift_release_confirm = int(drift_release_confirm)
        self._rel_run = 0

        # ---- crit='xbase' 用：段内自适应基线（近窗 vs 长窗，同为时间参考系）----
        # dev = median(近 xbase_win 秒 px) − median(近 xbase_base_win 秒 px)
        # 两个窗口都在**同一段数据内现算**，故免疫站位/姿态慢漂移（与 drift 的区别）。
        self.xbase_win = float(xbase_win)
        self.xbase_base_win = float(xbase_base_win)
        self.xbase_thr_left = float(xbase_thr_left)
        self.xbase_thr_right = float(xbase_thr_right)
        self.xbase_release = float(xbase_release)
        self.xbase_release_confirm = int(xbase_release_confirm)
        self._xrel_run = 0
        # px 缓冲区需要同时满足 drift 的 rest_win 与 xbase 的长窗：取两者较大值。
        self._buf_win = max(float(rest_win), float(xbase_base_win))

        # ---- band='auto' 用：按门累计检出，预热期结束后锁定距离带 ----
        self.band_mode = AUTO_BAND if auto else 'fixed'
        self.auto_band_min_frames = int(auto_band_min_frames)
        self.auto_band_max_frames = int(auto_band_max_frames)
        self.auto_band_min_points = int(auto_band_min_points)
        self.auto_band_dominance = float(auto_band_dominance)
        self._range_res, self._range_bins = _range_grid()
        self._band_counts = {}          # 门号 -> 累计检出点数
        self._band_frames = 0           # 预热期已过的帧数
        self._band_locked = not auto    # 固定带从一开始就是「已锁定」
        self.band_gate = None           # 选中的门号（j=0..7）
        self.band_count = 0             # 选中门累计检出点数
        self.band_total = 0             # 全部门的累计检出点数
        self.band_locked_frame = None   # 在第几帧锁定
        self.band_source = 'fixed' if not auto else None
        self.band_counts_final = None   # 锁定时的逐门计数快照（诊断用）

        self.hist = deque(maxlen=self.window)     # (t, px|None, n, energy, rejected)
        self._rest = deque()                      # (t, px)：近 rest_win 秒的检出帧
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
        # band='auto'：带还没选定 → 本帧只用于数点数，不判动作、不算 px。
        if not self._band_locked:
            return self._auto_band_step(pts, now)
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
        # 静息水平基线：只在「帧级门控通过」的帧上喂数据（与判据本身同源）。
        # 只保留近 _buf_win 秒，内存 O(窗口秒数 × 帧率)。
        # ★ crit='xbase' 也用这同一个缓冲区：它的「长窗」就是这里的中位数，
        #   「近窗」则取窗口尾部——两者同源，保证比较的是同一套 px 口径。
        # ★ 读取端（_win_median / _rest_level）都按 `t >= now - win` 现过滤，
        #   所以目标长时间丢失后重新捕获时，旧数据不会被当成「近期常态」。
        if self.crit in ('drift', CRIT_XBASE):
            self._rest.append((now, px))
            cut = now - self._buf_win
            while self._rest and self._rest[0][0] < cut:
                self._rest.popleft()

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
        # 两种判据共用下面的「滞回 + 确认 + 最短间隔」状态机，只有「本帧证据」不同：
        #   crit='delta'（默认）：证据 = 窗内相对位移 v_lat/dx —— 原行为，逐位不变
        #   crit='drift'        ：证据 = 当前 px 相对静息水平 base 的偏移 d（时间基线）
        # ★ 两者都只用**相对量**：delta 用「窗内首尾之差」，drift 用「px − base」；
        #   没有任何一处比较 px 的绝对数值。
        drift = None
        ev_kw = {}
        hold = False                     # 是否释放已有方向（回到 CENTER）
        if self.crit == 'drift':
            base = self._rest_level(now)
            d = None if base is None else float(px - base)
            feat['base'] = None if base is None else round(base, 3)
            feat['d_rest'] = None if d is None else round(d, 3)
            feat['n_rest'] = len(self._rest)
            moved = d is not None and d <= -self.drift_right
            if moved:
                # 大负偏移 = 人已左移到位；小负偏移 = 人已右移到位
                drift = -1.0 if d <= -self.drift_left else 1.0
                feat['drift'] = 'RIGHT' if drift > 0 else 'LEFT'
                ev_kw = {'d_rest': d, 'base': base, 'n_rest': len(self._rest)}
            # ★ 释放条件必须与触发条件**不同**（滞回）：d 回到静息附近即释放。
            #   2026-10-19 修 1：原实现与 moved 同式且只在 not moved 分支执行，
            #   hold 恒假 ⇒ lane 永不回 CENTER（devlog §7.33）。
            #   2026-10-19 修 2（devlog §7.36）：释放**也要连续确认 M 帧**。
            #   只判单帧时，一次真实的单帧 px 野值就能把 lane 打回 CENTER、随即
            #   再触发 ⇒「一次动作被记两次」（seg_01.50 RIGHT@71.74 与 @72.91）。
            #   ★ M=1 时与旧版逐帧等价（含「无证据帧即释放」）；无证据帧
            #   （d is None，仅出现在 base 未收敛时）不计不清零，仍判释放。
            if self.lane != 'CENTER':
                if d is not None:
                    if d > -self.drift_release:
                        self._rel_run += 1
                    else:
                        self._rel_run = 0
                hold = d is None or self._rel_run >= self.drift_release_confirm
            else:
                self._rel_run = 0
                hold = False
        elif self.crit == CRIT_XBASE:
            # dev = 近窗中位 − 长窗中位。两个窗口都在这**同一段数据内现算**，
            # 所以站位/姿态的慢漂移会同时进入两项并被相减抵消——这正是
            # 它比 drift（拿早先静息段当参考系）有效的原因。
            short = self._win_median(now, self.xbase_win)
            lng = self._win_median(now, self.xbase_base_win)
            n_short = self._win_count(now, self.xbase_win)
            n_long = self._win_count(now, self.xbase_base_win)
            dev = None
            if (short is not None and lng is not None
                    and n_short >= XBASE_MIN_SHORT and n_long >= XBASE_MIN_SAMPLES):
                dev = float(short - lng)
            feat['xshort'] = None if short is None else round(short, 3)
            feat['xbase'] = None if lng is None else round(lng, 3)
            feat['xdev'] = None if dev is None else round(dev, 3)
            feat['n_xbase'] = n_long
            moved = dev is not None and (dev >= self.xbase_thr_left
                                         or dev <= -self.xbase_thr_right)
            if moved:
                # ★ 已用真值标定，勿凭直觉改：本安装几何下
                #   「人向**自己的左侧**移动 ⇒ dev > 0」（LEFT 7/7 命中于 dev>0）。
                #   下方状态机的约定是 want='RIGHT' if drift>0 else 'LEFT'，
                #   故 dev>0 必须映射为 drift=-1。
                #   ⚠ 这与 crit='delta' 的符号约定相反（delta 把 v_lat>0 判成 RIGHT）。
                drift = -1.0 if dev > 0 else 1.0
                feat['xdir'] = 'LEFT' if drift < 0 else 'RIGHT'
                ev_kw = {'xdev': dev, 'xbase': lng, 'n_xbase': n_long}
            # 释放端：与触发端构成滞回，且同样需要连续确认（理由同 drift 的
            # drift_release_confirm：单帧 px 野值会诱发「一次动作被记两次」）。
            if self.lane != 'CENTER':
                if dev is not None:
                    if abs(dev) <= self.xbase_release:
                        self._xrel_run += 1
                    else:
                        self._xrel_run = 0
                hold = dev is None or self._xrel_run >= self.xbase_release_confirm
            else:
                self._xrel_run = 0
                hold = False
        else:
            moved = (v_lat is not None and abs(v_lat) >= self.v_on
                     and dx is not None and abs(dx) >= self.min_dx)
            if moved:
                s_v = 1 if v_lat > 0 else -1
                s_d = 1 if dx > 0 else -1
                if (not self.require_sign_agree) or (s_v == s_d):
                    drift = s_v       # +1 = 向右漂移，-1 = 向左漂移（相对！）
                    feat['drift'] = 'RIGHT' if drift > 0 else 'LEFT'
                else:
                    # 两个独立估计互相矛盾 → 本帧不作为方向证据（降噪 D6）
                    feat['drift'] = 'REJECT_CONFLICT'
            ev_kw = {'v_lat': v_lat, 'dx': dx}
            hold = (self.lane != 'CENTER'
                    and (v_lat is None or abs(v_lat) <= self.v_off))
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
                                         now, **ev_kw))
            else:
                self._dir_frames = 0
        else:
            self._dir_frames = 0
            if hold:
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
    def _gate_of(self, r):
        """距离门号：与 `radar_dsp.synth_frame` 的 `int(r / RANGE_RES)` 同规则。"""
        j = int(float(r) // self._range_res)
        return min(max(j, 0), self._range_bins - 1)

    def _auto_band_step(self, pts, now):
        """预热期的一帧：累计各门检出点数，够条件就锁定距离带。

        锁定规则（两道门，取先到者）：
          A. 提前锁定：`frames >= auto_band_min_frames`
             且 首选门 >= auto_band_dominance × 次选门
             且 累计检出 >= auto_band_min_points
          B. 到点锁定：`frames >= auto_band_max_frames`（无论分布如何）
        只有在 A/B 之后才可能出现「无候选门」→ 此时退回 `BAND_DEFAULT`
        并把 `band_source` 标成 'auto-fallback'（不装作选对了）。
        j=0（近场泄漏门）不参与候选：它已被 DSP 的 min_range 排除，此处是护栏。
        """
        self._band_frames += 1
        for p in pts:
            r = p.get('r')
            if r is None:
                continue
            j = self._gate_of(r)
            self._band_counts[j] = self._band_counts.get(j, 0) + 1

        total = sum(self._band_counts.values())
        cand = sorted(((c, j) for j, c in self._band_counts.items()
                       if j >= AUTO_BAND_MIN_GATE), reverse=True)
        top = cand[0] if cand else (0, None)
        second = cand[1][0] if len(cand) > 1 else 0
        early = (self._band_frames >= self.auto_band_min_frames
                 and total >= self.auto_band_min_points
                 and top[1] is not None
                 and top[0] >= self.auto_band_dominance * max(second, 1))
        if early or self._band_frames >= self.auto_band_max_frames:
            self._lock_band(top, total, now)

        feat = {'band_mode': AUTO_BAND, 'band_frames': self._band_frames,
                'band_locked': self._band_locked, 'band_progress':
                round(self._band_frames / float(self.auto_band_max_frames), 3)}
        out = []
        if self._band_locked:
            feat['band_gate'] = self.band_gate
            feat['band_lo'] = None if self.band is None else round(self.band[0], 3)
            feat['band_hi'] = None if self.band is None else round(self.band[1], 3)
            feat['band_source'] = self.band_source
            feat['band_count'] = self.band_count
            out.append(self._evt(
                BAND_READY, now, info=True,
                band_gate=self.band_gate,
                band_lo=None if self.band is None else self.band[0],
                band_hi=None if self.band is None else self.band[1],
                band_count=self.band_count, band_total=self.band_total,
                band_frames=self._band_frames, band_source=self.band_source))
        # 预热期一帧都不进 hist：没有带就没有 px，不能拿半成品当轨迹。
        return self._result(now, feat, out)

    def _lock_band(self, top, total, now):
        """锁定距离带（含「无候选门 → 退回 BAND_DEFAULT」的诚实分支）。"""
        if top[1] is None:
            self.band = (float(BAND_DEFAULT[0]), float(BAND_DEFAULT[1]))
            self.band_gate = None
            self.band_count = 0
            self.band_source = 'auto-fallback'
        else:
            j = top[1]
            self.band = (j * self._range_res, (j + 1) * self._range_res)
            self.band_gate = j
            self.band_count = top[0]
            self.band_source = 'auto'
        self.band_total = int(total)
        self.band_locked_frame = self._band_frames
        self.band_counts_final = dict(self._band_counts)
        self._band_locked = True
        # base 是「某个带内的 px 中位数」——换带即换参考系，必须清空重算，
        # 否则会拿旧带的水平去判新带的偏移（假位移）。
        self._rest.clear()

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

    def _rest_level(self, now):
        """静息水平基线 base（m）：近 rest_win 秒内检出帧 px 的中位数。

        这是 crit='drift' 的**时间参考系**。用中位数而非均值：动作帧是少数、
        静息帧是多数，中位数天然落在静息水平上，不会因为人短暂停在某一侧被拖走。
        样本 < 8 帧（刚开机 / 刚捕获）时返回 None —— 此时**不产生任何方向证据**，
        宁可不出事件也不猜。（离线实测：真值窗最早在 6.9 s，届时样本已远超 8 帧。）
        """
        cut = now - self.rest_win
        xs = [p for (t, p) in self._rest if t >= cut]
        if len(xs) < 8:
            xs = [p for (_, p) in self._rest]
        if len(xs) < 8:
            return None
        return float(np.median(xs))

    def _win_px(self, now, win):
        """近 win 秒内的 px 列表（crit='xbase' 的近窗/长窗共用）。

        ★ 按**读取时刻**现过滤，不是只靠 append 时裁剪：目标长时间丢失期间
        不会有 append，缓冲区里会留下跨越丢失期的旧样本；读取端过滤保证
        「近期常态」里不会混进几分钟前的姿势。
        """
        cut = now - float(win)
        return [p for (t, p) in self._rest if t >= cut]

    def _win_median(self, now, win):
        xs = self._win_px(now, win)
        return None if not xs else float(np.median(xs))

    def _win_count(self, now, win):
        return len(self._win_px(now, win))

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
        self._rel_run = 0
        self._xrel_run = 0
        self._gated_run = 0
        self._gated_side = 0
        self._gated_fired = 0
        self.lane = 'CENTER'

    def _evt(self, name, t, gated=False, info=False, **kw):
        self.events += 1
        d = {'name': name, 't': round(float(t), 3), 'seq': self.events}
        if gated:
            d['gated'] = True
            d['verified'] = False
            d['gate_note'] = ('跳/蹲未过 P0→T0→T1→T2 验证门（1T2R 无俯仰角、2T4R 链路未验证），'
                              '此处只是候选项，不得当验收结论')
        if info:
            # 信息事件（如 BAND_READY）：**不是游戏动作**，不计入动作统计。
            d['info'] = True
        for k, val in kw.items():
            d[k] = round(float(val), 3) if isinstance(val, float) else val
        return d

    def _result(self, t, feat, out):
        return {'t': round(t, 3), 'lane': self.lane, 'target': self._target,
                'features': feat, 'events': out}


# ---------------------------------------------------------------- 离线分析
def analyze_file(path, processor=None, recognizer=None, band=BAND_DEFAULT,
                 limit=None, log=None, board=None):
    """对录制文件做离线动作分析。

    time 轴用 **帧号 × 帧周期**（等效真实节拍），不用墙钟 —— 离线跑得比实机快，
    墙钟时间会让「最短切换间隔」等时间门限失真。
    帧周期与解析器都按**板型**走（board=None → 从文件字节形态自动嗅探：
    1T2R 0.1295 s / 2T4R 0.52084 s），所以同一个函数能读两块板的录制文件。
    返回 (records, info)。
    """
    import radar_source as rs
    from radar_dsp import PointCloudProcessor

    log = log or (lambda *a: None)
    b = board or rs.sniff_board_from_file(path) or '1t2r'
    frame_period = rs.frame_period(b)
    parser, asm = rs.make_protocol(b)
    if board is None:
        log('[actions] 板型 = %s（帧周期 %.4f s，门距 %.4f m，解析器已切换）'
            % (b, frame_period, rs.range_res(b)))

    proc = processor or PointCloudProcessor()
    rec = recognizer or ActionRecognizer(band=band)
    records = []
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            for rf in parser.feed(chunk):
                pair = asm.push(rf)
                if pair is None:
                    continue
                # 只用前两个 RX：与雷达实时管道 radar_source.build_pipeline 的
                # on_frame(pair[0], pair[1], stats) 同口径
                # （2T4R 的 assembler 返回 4 个 RX，*pair 展开会 TypeError）
                pts, noise = proc.process(pair[0], pair[1])
                t = asm.frames * frame_period
                r = rec.push(pts, t)
                r['frame'] = asm.frames
                r['noise'] = round(float(noise), 1)
                records.append(r)
                if limit is not None and len(records) >= limit:
                    break
            if limit is not None and len(records) >= limit:
                break
    info = {'frames': len(records), 'packets': getattr(parser, 'packets', 0),
            'skipped': parser.skipped_bytes, 'bad': parser.bad_packets,
            'dropped': asm.dropped, 'file': os.path.basename(path),
            'board': b}
    log('[actions] %s → %d 帧' % (info['file'], info['frames']))
    if not records:
        # 静默返回空记录会让人把「解析器判错板型」当成「算法不出事件」，
        # 10/20 实采就踩过这个坑（裸帧 1T2R 被嗅成 2T4R → 0 帧但 EXIT=0）。
        log('[actions] ⚠ 解析到 0 帧：板型判成了 %s（帧周期 %.4f s）。'
            '若这是 1T2R 的裸帧录制（无 ICLH、40 B 一包），请显式指定 board="1t2r"。'
            % (b, frame_period))
    return records, info


def summarize(records):
    counts = {}
    gated_counts = {}
    info_counts = {}
    for r in records:
        for e in r['events']:
            key = e['name']
            if e.get('gated'):
                gated_counts[key] = gated_counts.get(key, 0) + 1
            elif e.get('info'):
                info_counts[key] = info_counts.get(key, 0) + 1
            else:
                counts[key] = counts.get(key, 0) + 1
    return {'events': counts, 'gated_candidates': gated_counts,
            'info_events': info_counts, 'frames': len(records)}


# CSV 列定义：前 4 列固定，其余取自 features；新增列一律**追加在末尾**，
# 以便既有产物（outputs/wave1-actions.csv，16959 行）仍可按列名复算。
CSV_COLS = ['n', 'px', 'py', 'r_mean', 'v_rad', 'deg_span', 'energy',
            'v_lat', 'dx', 'noise', 'events',
            'rejected', 'n_win', 'px_mad', 'drift',
            'base', 'd_rest',        # 仅 crit='drift' 有值
            'xshort', 'xbase', 'xdev', 'n_xbase', 'xdir']   # 仅 crit='xbase' 有值


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
    ap.add_argument('--board', default=None, choices=['1t2r', '2t4r'],
                    help='板型；默认 None = 从文件字节形态嗅探。'
                         '裸帧 1T2R（无 ICLH、40 B 一包）现在也能嗅出，'
                         '但显式指定最稳')
    ap.add_argument('--calib', default=None,
                    help='校准文件（radar_calibrate.py 产出的 calib.json）；'
                         '其中的 band/v_on/v_off/min_dx/max_step 会被采用，'
                         '显式命令行参数优先')
    ap.add_argument('--min-range', type=float, default=None,
                    help='点云级近距屏蔽（米）。内置默认 0.681 = 整门排除泄漏门 '
                         'bin0(r≈0.34 m，实测占 ~73%% 点数但仅 ~0.1%% 功率)；'
                         '0 = 关闭（原样输出近场泄漏云）')
    ap.add_argument('--min-snr', type=float, default=None,
                    help='相对噪声底 SNR 门限 dB（内置默认 7.0）')
    ap.add_argument('--cfar', type=float, default=None,
                    help='CFAR 门限倍数（内置默认 2.6）')
    ap.add_argument('--max-step', type=float, default=None,
                    help='帧级野值门限（米/帧），0=关闭（内置默认 0=关闭；'
                         '建议由 radar_calibrate.py 实测 p99(|Δpx|) 给出）')
    ap.add_argument('--res-pa-frac', type=float, default=None,
                    help='点位相对功率门控（0~1），0=关闭')
    ap.add_argument('--top-k', type=int, default=None,
                    help='每帧只取带内最强 K 点，0=全取')
    ap.add_argument('--min-pts', type=int, default=None,
                    help='目标存在性门限：带内点数 < 该值即视为丢失，默认 2。'
                         '★ top_k=1 时必须同时给 --min-pts 1，否则每帧都被判成丢失')
    ap.add_argument('--slope', choices=['theilsen', 'lstsq'], default=None,
                    help='横向速度估计器，默认 theilsen（稳健）')
    ap.add_argument('--dx-mode', choices=['median', 'endpoints'], default=None,
                    help='位移锚点，默认 median（稳健）')
    ap.add_argument('--no-sign-agree', action='store_true',
                    help='关闭「v_lat 与 dx 必须同号」的一致性门（不建议）')
    ap.add_argument('--crit', choices=['delta', 'drift', 'xbase'], default=None,
                    help="左右判据：delta=窗内相对位移（默认，原行为）；"
                         "drift=静息水平偏移（时间基线，参考系是早先采的静息段）；"
                         "xbase=段内自适应基线（近窗中位 vs 长窗中位，免疫站位慢漂移，"
                         "实测最优，见 radar_actions.XBASE_* 注释）")
    ap.add_argument('--rest-win', type=float, default=None,
                    help='crit=drift 的静息水平基线窗口（秒），默认 60')
    ap.add_argument('--drift-left', type=float, default=None,
                    help='crit=drift：d <= -该值 → LEFT，默认 0.80')
    ap.add_argument('--drift-right', type=float, default=None,
                    help='crit=drift：-drift-left < d <= -该值 → RIGHT，默认 0.25')
    ap.add_argument('--drift-release', type=float, default=None,
                    help='crit=drift 的滞回释放门：d 回到 -该值以内 → CENTER，'
                         '默认 0.10（必须 < --drift-right）')
    ap.add_argument('--drift-release-confirm', type=int, default=None,
                    help='crit=drift 释放端的连续确认帧数，默认 3（与触发端 confirm '
                         '对称）。设为 1 = 旧的「单帧即释放」行为（会出现'
                         '「一次动作被记两次」）')
    ap.add_argument('--window', type=int, default=None,
                    help='滑窗帧数，默认 12（≈1.55 s）。crit=delta 直接决定位移尺度；'
                         'crit=drift/xbase 下只影响 v_lat/dx 诊断量')
    ap.add_argument('--confirm', type=int, default=None,
                    help='触发端连续确认帧数，默认 3（≈0.39 s）。'
                         '★ 这是延迟的主要来源之一：降到 2 可省约 0.13 s，但误报上升')
    ap.add_argument('--min-interval', type=float, default=None,
                    help='最短切换间隔（秒），默认 0.5。★ 游戏里换道本身就有冷却，'
                         '调到 0.8~1.0 可在几乎不影响真动作的前提下显著压静息误报')
    ap.add_argument('--xbase-win', type=float, default=None,
                    help="crit=xbase 的近窗（秒），默认 0.80。越小延迟越低、噪声越大")
    ap.add_argument('--xbase-base-win', type=float, default=None,
                    help="crit=xbase 的长窗（秒），默认 40（必须 > --xbase-win）。"
                         "这是「近期常态」的估计窗口，越大越稳、跟随越慢")
    ap.add_argument('--xbase-thr-left', type=float, default=None,
                    help="crit=xbase：dev >= +该值 → LEFT，默认 0.14"
                         "（2026-10-21 由 0.10 上调以压静止误报，见"
                         " docs/静止误报诊断-左门与噪声.md）")
    ap.add_argument('--xbase-thr-right', type=float, default=None,
                    help="crit=xbase：dev <= -该值 → RIGHT，默认 0.18（实测 RIGHT 幅度偏大）")
    ap.add_argument('--xbase-release', type=float, default=None,
                    help="crit=xbase 的滞回释放门：|dev| 回到该值以内 → CENTER，"
                         "默认 0.05（必须 < 两侧阈值）")
    ap.add_argument('--xbase-release-confirm', type=int, default=None,
                    help='crit=xbase 释放端的连续确认帧数，默认 3')
    ap.add_argument('--auto-band', action='store_true',
                    help='自动选带：预热期按距离门统计检出，取 j>=1 中最多的一门当带'
                         '（预热期不发任何事件）。与 --band 互斥，本开关优先；'
                         '详 radar_actions.AUTO_BAND 注释与 §7.28.3')
    ap.add_argument('--auto-band-min-frames', type=int, default=None,
                    help='自动选带：最早允许锁定的帧数，默认 30（约 3.9 s）')
    ap.add_argument('--auto-band-max-frames', type=int, default=None,
                    help='自动选带：兜底锁定帧数，默认 120（约 15.5 s）')
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
        # 内置默认 0.681 = 整门排除泄漏门 bin0（依据见 radar_dsp.PointCloudProcessor
        # 的 min_range 注释与 calib.json 的 leak_bins）。优先级仍是
        # 显式 --min-range > calib.json 的 processor.min_range > 本内置默认。
        min_range=(args.min_range if args.min_range is not None
                   else psec.get('min_range', 0.681)),
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
    if args.min_pts is not None:
        over['min_pts'] = args.min_pts
    if args.slope is not None:
        over['slope'] = args.slope
    if args.dx_mode is not None:
        over['dx_mode'] = args.dx_mode
    if args.no_sign_agree:
        over['require_sign_agree'] = False
    if args.crit is not None:
        over['crit'] = args.crit
    if args.rest_win is not None:
        over['rest_win'] = args.rest_win
    if args.drift_left is not None:
        over['drift_left'] = args.drift_left
    if args.drift_right is not None:
        over['drift_right'] = args.drift_right
    if args.drift_release is not None:
        over['drift_release'] = args.drift_release
    if args.drift_release_confirm is not None:
        over['drift_release_confirm'] = args.drift_release_confirm
    if args.window is not None:
        over['window'] = args.window
    if args.confirm is not None:
        over['confirm'] = args.confirm
    if args.min_interval is not None:
        over['min_interval'] = args.min_interval
    if args.xbase_win is not None:
        over['xbase_win'] = args.xbase_win
    if args.xbase_base_win is not None:
        over['xbase_base_win'] = args.xbase_base_win
    if args.xbase_thr_left is not None:
        over['xbase_thr_left'] = args.xbase_thr_left
    if args.xbase_thr_right is not None:
        over['xbase_thr_right'] = args.xbase_thr_right
    if args.xbase_release is not None:
        over['xbase_release'] = args.xbase_release
    if args.xbase_release_confirm is not None:
        over['xbase_release_confirm'] = args.xbase_release_confirm
    if args.auto_band_min_frames is not None:
        over['auto_band_min_frames'] = args.auto_band_min_frames
    if args.auto_band_max_frames is not None:
        over['auto_band_max_frames'] = args.auto_band_max_frames
    if args.auto_band and args.band:
        print('[actions] 注意：--auto-band 优先，--band 本次被忽略')
    band = AUTO_BAND if args.auto_band else (
        tuple(args.band) if args.band else None)
    rec = ActionRecognizer(band=band, calib=cal or None, **over)
    if rec.crit == 'drift':
        crit_txt = ('  base=近 %.0f s 内 px 中位数  drift_left=%.2f drift_right=%.2f '
                    'drift_release=%.2f rel_confirm=%d'
                    % (rec.rest_win, rec.drift_left, rec.drift_right,
                       rec.drift_release, rec.drift_release_confirm))
    elif rec.crit == CRIT_XBASE:
        crit_txt = ('  近窗=%.2f s 长窗=%.0f s  thr_left=+%.2f thr_right=-%.2f '
                    'release=%.2f rel_confirm=%d  （符号已真值标定：dev>0 ⇒ LEFT）'
                    % (rec.xbase_win, rec.xbase_base_win, rec.xbase_thr_left,
                       rec.xbase_thr_right, rec.xbase_release,
                       rec.xbase_release_confirm))
    else:
        crit_txt = ''
    print('[actions] 判据: crit=%s%s' % (rec.crit, crit_txt))
    print('[actions] 时序(按 1T2R 帧周期 %.4f s 折算): 窗=%d 帧(%.2f s)  '
          'confirm=%d(%.2f s)  min_interval=%.2f s  lost_timeout=%.2f s'
          % (FRAME_PERIOD, rec.window, rec.window * FRAME_PERIOD, rec.confirm,
             rec.confirm * FRAME_PERIOD, rec.min_interval, rec.lost_timeout))
    print('[actions] 估计器: slope=%s dx_mode=%s max_step=%.2f sign_agree=%s '
          'res_pa_frac=%.2f top_k=%d min_pts=%d'
          % (rec.slope, rec.dx_mode, rec.max_step, rec.require_sign_agree,
             rec.res_pa_frac, rec.top_k, rec.min_pts))
    band_txt = ('自动（预热 %d~%d 帧后锁定，期间不发事件）'
                % (rec.auto_band_min_frames, rec.auto_band_max_frames)
                if rec.band is None else str(rec.band))
    print('[actions] 距离带 %s  阈值 v_on=%.2f v_off=%.2f min_dx=%.2f%s'
          % (band_txt, rec.v_on, rec.v_off, rec.min_dx,
             '  （来自 %s）' % args.calib if args.calib else '  （内置提案值）'))

    records, info = analyze_file(args.file, processor=proc, recognizer=rec,
                                 limit=args.limit, board=args.board,
                                 log=lambda m: print(m))
    if rec.band_mode == AUTO_BAND:
        print('[actions] 自动选带结果: %s  门 j=%s  检出 %d/%d  第 %s 帧锁定'
              % (rec.band, rec.band_gate, rec.band_count, rec.band_total,
                 rec.band_locked_frame))

    if not args.events_only:
        print('文件: %s  帧数: %d  坏包: %d  丢弃帧: %d  跳过字节: %d'
              % (info['file'], info['frames'], info['bad'], info['dropped'],
                 info['skipped']))
    for r in records:
        for e in r['events']:
            if e.get('gated'):
                tag = '  [候选·未验证]'
            elif e.get('info'):
                tag = '  [信息]'
            else:
                tag = ''
            extra = ''
            if 'v_lat' in e:
                extra = '  v_lat=%.2f m/s  dx=%.2f m' % (e['v_lat'], e['dx'])
            elif 'd_rest' in e:
                extra = '  d_rest=%.2f m  base=%.2f m' % (e['d_rest'], e.get('base', 0.0))
            elif 'xdev' in e:
                extra = '  dev=%+.2f m  长窗中位=%.2f m  n=%d' % (
                    e['xdev'], e.get('xbase', 0.0), e.get('n_xbase', 0))
            elif 'band_gate' in e:
                extra = '  带=%.2f~%.2f 门j=%s 检出=%d/%d' % (
                    e.get('band_lo') or 0.0, e.get('band_hi') or 0.0,
                    e.get('band_gate'), e.get('band_count', 0),
                    e.get('band_total', 0))
            elif 'ratio' in e:
                extra = '  能量比=%.2f' % e['ratio']
            print('帧 %5d  t=%7.2fs  %-12s%s%s' % (r.get('frame', 0), e['t'],
                                                   e['name'], tag, extra))
    s = summarize(records)
    print('摘要: %s' % json.dumps(s, ensure_ascii=False))
    if not records:
        sys.stderr.write('[actions] ✗ 0 帧 —— 原因通常是板型判错（裸帧 1T2R 无 ICLH）。'
                         '请加 --board 1t2r 重跑；此退出码 2 表示「没读到数据」，'
                         '不是「没检出动作」。\n')
        return 2
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
