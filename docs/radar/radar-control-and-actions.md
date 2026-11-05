# 采集控制（开始 / 结束 / 重置）与近距动作识别

> 版本：2026-10-15 晚。对应代码：`radar-pointcloud/radar_control.py`、`radar_actions.py`、
> `radar_hwstop.py`、`server.py`、`viewer.html`、`label_tool.py`。
> 所有阈值均为**提案，待小组确认**（AGENTS.md 证据纪律）。

---

## 0. 一句话状态

| 能力 | 状态 | 证据 |
|---|---|---|
| 开始 / 结束 / 重置 采集会话 | **已实现，并通过离线 + 端到端 + 真机（COM3）验证** | `selftest-report.txt` 测试 4；`test_control_e2e.py` 23 项；`.deepworks/tmp/check_hw_control.py` **真机 31 项全通过**（2026-10-15） |
| 结束=降温 | **不成立（诚实结论）** | 软件停止只释放串口，雷达仍在发射，见 §2 |
| 硬件停机（真正降温） | **命令模式连通性已实机验证；真正停机（写寄存器）仍未验证** | `radar_hwstop.py`（默认 dry-run）；T1 实机 9/9 通过（`.deepworks/tmp/t1-hwstop-report.txt`）；见 `radar-hardware-stop-plan.md` §7.1 |
| 左移 / 右移识别 | **规则法已实现；真机真值仍待标注** | 合成用例通过（测试 6）+ `wave1.bin` 实测事件，但**无真值** |
| 蹲 / 跳识别 | **仅候选，未过验证门，不得当验收结论** | 事件恒带 `gated=True, verified=False` |

---

## 1. 「开始 / 结束 / 重置」到底是什么语义

控制器把「一次采集会话」封装成状态机（`radar_control.py`）。

| 操作 | 做什么 | 不做什么 |
|---|---|---|
| **开始** | 新建数据源（串口/回放/合成）→ 起采集线程 → `sessions += 1`、`starts += 1`、帧计数归零 | 不改变雷达配置，不重置页面其它状态 |
| **结束** | 杀采集线程（join ≤2 s）→ `source.close()`**释放串口**，停止解算/推送/渲染 | **不停止雷达发射**（芯片仍在工作，温度不降，见 §2） |
| **重置** | 帧计数、`active_seconds`、`since_frame`、`restarts` 归零；运行中则把本会话计时清零（会话号不变） | 停止态重置会把 `sessions` 一并清零；不关闭页面 |
| **热切换**（`switch`，需 `--hotkey`，**默认关闭**） | 同一把锁内**原子**执行「停旧源（释放串口）→ 换模式 → 按原状态重启」：帧计数归零、`sessions`/`starts` 各 +1、`switches` +1，并回传 `perf_counter` 实测的 `switch_ms` | **不重启 HTTP 服务、不关页面**；不改变雷达配置；**不保证新源真的出帧**（见 §6 `switch`） |

补充状态机行为（测试 4 覆盖）：

- **重复开始**被拒（已在采集）；**空闲结束**被拒（当前未在采集）；**未知 action** 被拒（已知 action = `start` / `stop` / `reset` / `switch` 及其别名）。
- **停滞自愈**：帧号超过 `--stall-timeout`（默认 8 s）不增长 → 判为停滞，**同会话内重建管道**并 `restarts += 1`，页面会看到 `restarts` 增加而 `sessions` 不变。
- 网页端用 **1 Hz 控制心跳**区分「服务在、但已结束采集」与「服务不可达」：停止后心跳继续，帧流停止；页面显示「采集已停止」。

## 2. 关于发热：必须说清楚的一条

`HEAT_NOTE`（服务端下发，页面直接显示）：

> 软件「结束」只释放串口并停止解算/渲染，雷达芯片仍在发射，温度不会下降；
> 真正降温需硬件停机（实验：命令模式连通性已实机验证，写寄存器停机仍未验证）。

也就是说：**点「结束」能省的是主机侧 CPU/USB 带宽，省不了雷达自身功耗。**
真降温路径见 `radar-hardware-stop-plan.md`（写停机寄存器）：其前置的「进/出命令模式」
已于 2026-10-15 在实机验证（T1，9/9，未写寄存器），但**真正写停机寄存器那一步尚未跑通**。

---

## 3. 近距动作识别：为什么不能沿用原流水线

1. 距离分辨率仅 **0.6813 m**、只有 8 个距离门：1 m 附近的动作目标与近距强反射**落在同一距离门**，靠"哪个门有点"分不出动作。
2. 目标几乎在正前方时**径向速度≈0**：左右移动几乎不产生多普勒 → 左/右**不能靠 `v` 判**。
3. 近距门会被 TX/RX 泄漏与自身强反射占据 → 必须**限定距离带**并只取带内幅值加权质心。

所以做法是：**限定距离带 → 幅值加权横向质心 `px` → 滑窗线性拟合得横向速度 `v_lat` → 阈值 + 滞回 + 最短切换间隔 + 连续确认 → 事件**。
事件名与项目「关键契约」一致：`LANE_LEFT` / `LANE_RIGHT` / `JUMP` / `DUCK` / `TARGET_LOST` / `TARGET_BACK`。

## 4. 距离带（`--band`）：用你自己的录制实测出来的

距离门索引由 `radar_dsp.py` 决定：`rng = (j + 0.5) × 0.6813`，即
**`j = round(r / 0.6813 - 0.5)`**，门中心 0.34 / 1.02 / 1.70 / 2.38 / 3.07 / 3.75 / 4.43 / 5.11 m。

对 `wave1.bin` 前 1500 帧（≈194 s，2026-10-15 21:05 起录）只读离线逐门统计：

| 距离门 | 中心 | 占用率 | 中位 `pa` | p90 `pa` | 中位 \|v\| | 判定 |
|---|---|---|---|---|---|---|
| **j=0** | 0.34 m | **98%** | 7.4e4 | 2.5e5 | **0.77 m/s** | TX/RX 泄漏近场门 → **排除** |
| **j=1** | 1.02 m | **73%** | **1.3e6** | 2.9e7 | 0.05 m/s | **就是人**（见下） |
| j=2 | 1.70 m | 1% | 4.4e4 | **4.3e7** | 0.82 | 偶发极强回波 → **排除** |
| j=3 | 2.38 m | 1% | 3.2e4 | 1.7e9 | 0.77 | 排除 |
| j=5–7 | 3.8–5.1 m | 12–16% | 1.8e5–3.1e5 | 3.4e6–4e6 | 0.05 | 远距静态背景 |

- 判定 j=1 是人的依据：带内质心实测 `px≈-0.52 m`、`py≈0.88 m` → **r≈1.02 m、方位角≈-30°**，即人站在 1 m 处、偏左约 30°；且大幅动作帧的带内能量暴涨到 **2.9e9 / 7.9e9**（相对中位 1.3e6 高出约 2000 倍），与人的动作时刻对应。
- 因旧带 `(0.85, 1.85)` 同时含 j=1 与 j=2（偶发 p90 4.3e7 的强回波），**默认带已改为 `0.70 1.35`**，即**只保留 j=1**。
- **局限**：人站到 **1.4 m 以上**会落进 j=2，必须改用 `--band 1.35 2.05` 另测——这正是「1~1.5 m」里 1.5 m 一档目前**未被默认带覆盖**的地方，需要专门验证。

## 5. 参数表（全部为提案，待小组确认）

| 常量 | 默认 | 含义 |
|---|---|---|
| `BAND_DEFAULT` | `(0.70, 1.35)` | 动作识别距离带（= j=1） |
| `WINDOW_DEFAULT` | `12` | 滑窗 ≈1.55 s（12 × 129.5 ms） |
| `V_ON_DEFAULT` | `0.35` m/s | 触发用横向速度门限 |
| `V_OFF_DEFAULT` | `0.18` m/s | 滞回释放门限 |
| `MIN_DX_DEFAULT` | `0.10` m | 窗内横向净位移门限（防抖） |
| `MIN_INTERVAL_DEFAULT` | `0.5` s | 最短切换间隔 |
| `CONFIRM_DEFAULT` | `3` 帧 | 连续确认帧数（**触发端**） |
| `DRIFT_LEFT_DEFAULT` / `DRIFT_RIGHT_DEFAULT` | `0.80` / `0.25` m | `crit='drift'` 专用：`d` 水平偏移 → `LEFT` / `RIGHT` 的触发门（`0.80` 为**提案值**，2026-10-19 由 `1.30` 重标定，见下） |
| `DRIFT_RELEASE_DEFAULT` | `0.10` m | `crit='drift'` 专用：释放门（`d` 回到 `-0.10` 以内 ⇒ 允许回 `CENTER`，滞回） |
| `DRIFT_RELEASE_CONFIRM_DEFAULT` | `3` 帧 | `crit='drift'` 专用：**释放端**连续确认帧数（2026-10-19 新增，见下）；`1` = 旧的单帧即释放（会出现「一次动作被记两次」） |
| `LOST_TIMEOUT_DEFAULT` | `0.6` s | 目标丢失判定 |
| `JUMP_RISE_DEFAULT` | `1.9` 倍 | 回波能量相对基线上升（**候选**） |
| `DUCK_DROP_DEFAULT` | `0.50` | 回波能量相对基线下降（**候选**） |
| `min_pts` | `2` | 带内至少几个点才算「有目标」 |

> **`DRIFT_RELEASE_CONFIRM_DEFAULT = 3` 的来源（2026-10-19，`docs/progress-radar-pointcloud.md` §7.36）**：
> `top_k=1` 时 `px` 是**单帧最强点的 x**，站立噪声 ±0.15 m（0.70 m 站位 ±0.6 m），
> 而站立偏移 `|d| ≈ 0.27 m` 恰好压在触发门 `0.25` 上 ⇒ 一次**单帧野值**就能跨过释放门
> （`seg_01.50` 第 559 帧 `d` 由 -0.324 变 -0.011）⇒ `lane` 回 `CENTER` ⇒ 随即**再次触发**
> （多打 `RIGHT@72.91`，即现场「挥一次手、角色连切两道」）。释放端改为需连续 3 帧确认后，
> 1.50 m 误报 1 → **0**，且**其余 7 个文件事件逐条不变**。
> 回退：`radar_actions.py --drift-release-confirm 1`。CLI：`--drift-release-confirm`（`server.py` 未暴露，用模块默认值）。

> **`DRIFT_LEFT_DEFAULT = 0.80` 的来源（2026-10-19，`docs/progress-radar-pointcloud.md` §7.39）**：
> 外样本 3 段上「左移」几乎检不到，且检出的事件**全被标成 `RIGHT`**。根因**不是极性反转、也不是判据结构错**，
> 而是**幅度档位标定错**：左右两类动作的 `d` **同为负向**（LEFT 20 窗中 19 个负向占优、**0 个正向占优**；
> RIGHT 20 窗中 11 个负向占优、**0 个正向占优**）⇒ 符号不携带方向信息，方向只在 `|d|` 的幅度里；
> 而 `|d|` 尺度**随站位变**（LEFT ≈ 1.0~1.3 m @0.70 m、≈2.2 m @1.25 m；RIGHT 各站位 ≈0.25~0.35 m）。
> 原值 `1.30` 卡在近站位 LEFT 峰值上（窗内 min 多为 −1.25~−1.32）⇒ LEFT 落进「小负偏移」档被判成 `RIGHT`。
> 下调到 `0.80`（**样本内**扫出的值）：LEFT 7/20 → 15/20、误报 18 → 12、RIGHT 18/20 → 16/20；
> 官方评估 LEFT 2/15 → 11/15、总命中 15/30 → 22/30、误报 13 → 7，且 `seg_01.25`（`L4 R5 fp0`）与
> `seg_01.50`（`L0 R4 fp0`）事件逐条不变。
> **结构未改**（仍是「大负偏移 → `LEFT`；小负偏移 → `RIGHT`」）；**曾设想的「按符号判定」已验证不成立，未采用**。
> 回退：`radar_actions.py --drift-left 1.30`。CLI：`--drift-left`。

### 5.1 判定原则（硬约束，不许改）

判定**只用移动过程的方向**：`v_lat` 与 `dx` 的**符号**（以及相对幅度），
`dx` 用「前后 1/3 中位数之差」或「端点差」估计；`lane` 是**方向 latch**，不是绝对位置。
**静止时不得产生 `LANE_*`**。注意范围：目前**只有恒定偏置**被回归测试钉住（px 恒为 -0.80 时零事件）；`dx`/`v_lat` 只抵消恒定分量，**随机游走分量不会被抵消**，带噪声静止序列的误报率**尚未量化**，须真机静息采集 + 真值标注后才可声称（见 `docs/progress-radar-pointcloud.md` §7.18 ⑩）。**不得写成「任何 px 都不误报」。**
不允许改成绝对位置映射（AGENTS.md 硬约束 1）。
`require_sign_agree=True`（默认）时，`v_lat` 与 `dx` 必须**同号**才动作。

> ⚠ **`crit='drift'` 不能靠符号分左右**（2026-10-19，见 §7.39）：drift 判据量的是「相对静息水平的横向偏移」，
> 而实测 LEFT / RIGHT 两类动作的偏移**同号（都偏负）**——LEFT 20 窗中 19 个负向占优、RIGHT 20 窗中 11 个负向占优，
> **两类都没有一个正向占优的窗**。⇒ drift 路径的左右只能由 `|d|` 的**幅度档位**决定（`drift_left` / `drift_right`），
> 且该幅度刻度**随站位变化**，因此 drift 阈值是**站位相关**的、未经未知站位验证。
> 同一结论在位移判据上更早就出现过（`disp_sign` 在 RIGHT 上 R 0/5，§7.26.4/§7.27.1）——两次独立复现。

### 5.2 降噪层 D1~D6（2026-10-16 新增）

| 开关 | 默认 | 作用 |
|---|---|---|
| `res_pa_frac` | `RES_PA_FRAC_DEFAULT` | 同帧相对功率门：低于「该帧最强点 × 该比例」的点先剔除（先于估位） |
| `top_k` | `TOP_K_DEFAULT` | 每帧最多用功率最强的 K 个点（0 = 全用） |
| `max_step` | **`0`（关闭）** | 单帧 px 跳变上限（米）：超过判野值，不入窗且不更新 `_last_px` |
| TARGET_LOST 清窗 | 固定行为 | 丢失目标时清空滑窗，避免旧窗跨丢失段污染再捕获后的估计 |
| `slope` | `'theilsen'` | px-时间趋势斜率：Theil–Sen（两两斜率中位数，抗野值）或 `'lstsq'` |
| `dx_mode` | `'median'` | 净位移估计：`'median'`（前后 1/3 中位数差）或 `'endpoints'`（端点差） |
| `require_sign_agree` | `True` | `v_lat` 与 `dx` 必须同号才判动作 |

**`max_step` 默认为何是 0（关闭）**：带内点只有 0.1~2.4 点/帧时，幅值加权质心**本身不是连续轨迹**，
帧间跳变经常 > 0.5 m，固定的 0.5 m 物理门会把大部分帧判成野值丢掉，可算出的动作组从 10 组掉到 3~8 组，
**反而引入偏差**。正确做法是用现场实测 `p99(|Δpx|)` 折算（`radar_calibrate.py` 已实现，上限裁到 0.80 m）。
能力仍保留，可显式开启（`--max-step`）。依据见 `docs/progress-radar-pointcloud.md` §7.18 ⑥。

**关于稳健估计器的说明（不得过度主张）**：`theilsen`+`median` 保留为默认，
理由是它们**对单点毛刺更保守**（可在原理上论证）。
**本轮离线 A/B 没有**得到「它们比 `lstsq`+`endpoints` 更准」的证据（信噪比 < 1，见 §7.18 ③），
因此**不得**用准确率来论证这个默认值；也不得据此声称识别精度有提升。

### 5.3 现场校准（`radar_calibrate.py`，2026-10-16 新增）

| 模式 | 用途 | 产出 |
|---|---|---|
| `--mode band` | 逐距离门统计占用率/点数/帧/功率占比，推荐实际承载人的距离带 | `band_stats` + 推荐 `band` |
| `--mode rest` | 用真人静息段实测噪声，反推阈值并做**闭环静息误报自检** | `v_on/v_off/min_dx/max_step` + `noise` |
| `--mode sign` | 判定 `angle_sign`（左右正负约定），带**信号 vs 噪声判别** | `sign_evidence` |

要点：

- 时间轴离线与实时**统一用「帧号 × 129.5 ms」**，两处数字可直接对照。
- `sign` 模式信噪比 `<1` 时**拒绝给结论**（EXIT=2），`<2` 时给警告——避免把噪声当方向证据。
- `rest` 模式有效窗过少时**拒绝**给阈值（EXIT=2），不硬报。
- 输出 `calib.json` 顶层带 `version/proposal:true/created/updated/notes/source/...`，
  并且**每个模式都会把本次实际生效的 DSP 参数写回 `processor` 段**（含 `cfar_scale`），
  避免多次运行留下陈旧值导致「按校准文件复现现场设置」失真。
  band/rest 各自的证据来源文件记录在 `band_source_file` / `threshold_source_file`。
- **所有阈值均为提案，`proposal: true`，待小组确认**；校准只调阈值，**不改变 §5.1 判定原则**。

**可信度随结论一起落盘**（2026-10-16）：`calib.json` 里 `angle_sign` 会带 `sign_confidence`（low/medium/high，由 `sign_evidence.snr_ratio` 推出）与 `sign_evidence.snr_ratio/noise_p95/n_null_windows`；rest 阈值会带 `threshold_confidence`（由有效估计帧数与闭环静息误报推出）。
**读 `calib.json` 时必须连可信度一起读**——只有 `angle_sign` 而没有 `snr_ratio` 的旧文件，不得当作硬证据引用。

**三个入口的 `--calib` 优先级一致：显式命令行参数 > `calib.json` > 内置默认。**
不带 `--calib` 时行为与之前完全一致（已实测：`band=(0.7, 1.35)`、`min_range=0.0`、`calib=None`）。

改这些值等于改算法行为，**必须同步改本文件**。

## 6. 接口

### `POST /control`（也接受 `/api/control`、`?action=`）
请求：`{"action": "start" | "stop" | "reset" | "switch"}`
响应：`{"ok": true/false, "error"?: "...", ...status()}`，字段含
`active / sessions / starts / stops / frames / elapsed / active_seconds / since_frame / restarts / source / source_mode / stop_reason / heat_note / hotkey`。

**`switch`（运行中热切换数据源 —— `AGENTS.md` 硬约束 2）**
仅在启动时给了 `--hotkey` 时可用；**未启用时服务端明确拒绝**（`ok=false`，并提示「启动服务时加 `--hotkey`」）。
- 别名：`switch-source` / `switch_source` / `next-source`。
- 模式循环：**启动模式排最前**，其余按 `serial → replay → loopback` 补齐；可用 `--hotkey-modes` 显式指定（顺序即轮转顺序）。
- 额外响应字段：`source_mode`（新源）、`switch_ms`（服务端实测切换耗时）、`switches`（累计次数）、`note`。
- ⚠ **`ok=true` 只表示「已按新模式重启」**，**不代表新源能出数据**（串口被占用／回放文件缺失都会静默无帧）⇒ 判断依据仍是
  `active` / 帧率 / `src_diag`。响应里的 `note` 就是这个提醒，页面也会把它显示出来。
- 实测（2026-10-19，37 项离线端到端检查）：服务端 `switch_ms` **1.4~3.6 ms**、HTTP 往返 **17.9~19.8 ms**，远低于 1 秒。

> 2026-10-19 修正：`/status` 原先**只在 `control` 为空时**才回填，于是**切到坏源 / 帧停之后会永久回放过期快照**
> （`active`、`source_mode`、`switches` 全是旧值）。现在**一律取实时 `control_status()`**，无帧时也能反映真实状态（含 `hotkey`）。
> 该 bug 是「切到坏源」的用例暴露的，不是代码审查发现的。

### `/status`
同上字段 + 链路统计（`connected / reconnects / read_errors / dropped / cb_errors`）。

> 2026-10-15 修正：原来 `/status` 只在**收到第一帧之后**才有内容（`LATEST['status']` 由帧回调写入），
> 于是用 `--no-autostart` 启动、还没点「开始」时读 `/status` 得到的是空对象 `{}`。
> 现在无帧时回退到已知的 `cfg` 与 `control`（1 Hz 心跳已经写入），监控端在「未开始」状态下也能读到
> `active / sessions / heat_note` 等字段。改动在 `server.py` 的 `do_GET` 分支，浏览器端逻辑未受影响。

### `/stream`（SSE）
- `hello`：含 `cfg`（`band / actions / autostart / hw_stop / min_range ...`）与初始 `control`。
- `control`：**1 Hz 心跳**，页面据此判断在线与采集状态；含 `hotkey`（`enabled / key / modes / mode / next / switches / last_ms`，未启用时 `enabled=false` 且 `modes=[]`）。
- `frame`：含 `control`（当前会话状态）与 `act`（`lane / target / features`）+ `events`。

### 命令行（`server.py`）
```
python server.py --port COM3 --http-port 8099 --band 0.70 1.35 --record wave1.bin
# 省电/省发热：启动后不自动采集，等页面点「开始」
python server.py --port COM3 --no-autostart
# 热切换（硬约束 2）：运行中按 F2（或页面「切换数据源」按钮）轮转 真雷达/回放/合成
python server.py --port COM3 --hotkey
python server.py --source replay --replay cap.bin --hotkey --hotkey-key F3 --hotkey-modes replay,loopback
# 其它：--stall-timeout 8.0  --no-stall-restart  --no-actions  --quiet
```
注意：**同时只能有一个进程占 COM3**；跑离线分析/回放不要碰串口（用 `--source replay`）。
热切换**默认关闭**：不给 `--hotkey` 时行为与改动前完全一致（`switch` 被拒、页面不出现按钮）。

## 7. 怎么用（完整闭环）

**推荐路径（省掉手工打标）**：用 `segment_capture.py` 一键做「分段定距 + 提示音驱动 + 自动真值 + 自动对照」：

```bash
# 0) 先停掉 server.py（COM3 同时只能一个进程占）
# 1) 3 段 × 2 动作 × 5 次，约 6 分钟，人在设备旁按提示做动作（提前停 server.py）
python segment_capture.py --port COM3
#    输出：.deepworks/tmp/seg/seg_<距离>.bin + .labels.jsonl + compare-all.txt（逐段表 + 合计表）
#    自检/无硬件演练：--source loopback  或  --source replay --replay-file sample-synthetic.bin
```

注意：1.0 m 与 1.25 m 落**同一距离门 j=1**（带内不可分），1.5 m 才是 j=2（要 `(1.35,2.05)` 带）。

手工路径（想看回放、或只想补标几段时用）：

```bash
# 1) 采集（人在 1 m 左右，动作做完再来一轮）
python server.py --port COM3 --http-port 8099 --record wave1.bin

# 2) 离线分析（不占串口）
python radar_actions.py --file wave1.bin --csv actions.csv

# 3) 标真值（回放式打标：1=左移 2=右移 3=蹲 4=跳，0/空格=结束，u=撤销，q=保存退出）
python label_tool.py --file wave1.bin --out wave1.labels.jsonl

# 4) 预测 vs 真值对照（精确率/召回率/漏报/误报）
python label_tool.py --file wave1.bin --labels wave1.labels.jsonl --compare
```

**第 3 步是必需的**：没有真值，第 2 步的事件只能算「跑出来了」，不能算「准」。
真值区间的时间轴是 `帧号 × 0.1295`（`radar_actions.FRAME_PERIOD`），不是墙钟。

## 8. 验证证据（可复查）

| 证据 | 内容 | 位置 |
|---|---|---|
| `selftest.py` 测试 4 | 开始/结束/重置 + 停滞自愈（`restarts=2`）全通过 | `selftest-report.txt` |
| `selftest.py` 测试 6 | 右移/左移/静止不误报/带外忽略/丢失恢复/跳蹲候选 + 单帧毛刺不算跳/跳=候选且未验证/蹲=候选且未验证/偏置静止不误报/偏置下右移仍判对/野值帧被剔除/校准阈值生效，**共 12 项全过**（`OVERALL True`） | 同上（2026-10-16 更新） |
| 降噪层离线 A/B | `.deepworks/tmp/ab_denoise.py`（临时，只读）：4 数据集 × 4 距离带 × 2 灵敏度 × 5 估计器。**结论：所有配置信噪比 < 1（0.10×~0.84×），换估计器换不动**；表内 60%~80% 命中率经左右分解证实为「恒答同一边」假象或多重比较假象 | `docs/progress-radar-pointcloud.md` §7.18 |
| 现场校准工具 | `radar_calibrate.py` 三模式离线跑通：`band` 查出在用带只吃到 ~9.6% 功率（j=2 占 **90.2%**）并给出不一致警告；`sign` 以信噪比 **0.05×** 拒绝给结论（EXIT=2）；`rest` 在有效窗仅 12 个时拒绝给阈值（EXIT=2） | 同上 §7.18 ⑤⑧ |
| `server.py --calib` | 三级回退实测：① 带 `--calib` → `band=[0.68,2.04]` 生效；② 带 `--calib` + 显式 `--band 1.0 1.6` → 显式压过校准；③ 不带 `--calib` → `band=[0.7,1.35]`、`calib=None`，**与改动前一致（无回归）** | 同上 §7.18 ① |
| `test_control_e2e.py` | 真起 HTTP 服务（端口 8124、回放源）：空闲心跳、空闲 stop 被拒、未知 action 被拒、start、frame 带 control/act、运行中 reset、重复 start 被拒、stop 统计与热提示、停止后仍有心跳无帧、可再 start、停止态 reset 清零 —— **23 项全通过** | `.deepworks/tmp/test_control_e2e.py` + `e2e-server.log` |
| **真机验收（COM3）** | `check_hw_control.py`（真实串口，HTTP 8125，`--no-autostart`）**31 项全通过**。关键项：未采集时 COM3 可被外部进程打开；采集中外部打开得到 `PermissionError(13,'拒绝访问。')`＝确实独占；真机 **fps=7.43**（≈129.5 ms/帧，与规格一致）、`read_errors=0 reconnects=0`；`stop` 之后**外部进程能重新打开 COM3**（用户最初的痛点，已解决）、HTTP 心跳仍在但帧计数冻结；可再次 `start` 并恢复出帧；进程退出后 COM3 空闲 | `.deepworks/tmp/hw-acc-report.txt` + `hw-acc-server.log` |
| **T1 命令模式连通性（COM3，不写寄存器）** | `run_t1_hwstop.py` **9 项全 PASS**：0x00FF 进命令模式后上行由 **40960 B/s → 464 B/1.5 s**（≈停流），0x00FE 后回到 **65536 B→40960 B/s（裸 40 B 帧 1024 条/s = 基线）**；两个应答都是 `type=命令\|0x0100、len=4、status=1`，按固件老约定 **= ACK_OK**（起初误判为 FAIL，已纠正） | `.deepworks/tmp/t1-hwstop-report.txt`；`docs/radar-hardware-stop-plan.md` §7.1 |
| `wave1.bin` 实测 | 前 1200 帧（≈155 s）事件：`LANE_RIGHT 2 / LANE_LEFT 3 / TARGET_BACK 41 / TARGET_LOST 40`，候选 `JUMP 97 / DUCK 27`；显著动作帧 t≈93.2 s（px=+0.53, v_lat=+1.05, pa=2.9e9 → `LANE_RIGHT`）、t≈104.9 s（px=-0.57, v_lat=-1.12, pa=7.9e9 → `LANE_LEFT`） | 见 §4 与 `.deepworks/tmp/probe_wave1.py` |

| `wave1.bin` 全量实测（16959 帧 / 36.6 min） | `LANE_LEFT 17 / LANE_RIGHT 21`；`TARGET_BACK 608 / TARGET_LOST 608`；候选 `JUMP 556 / DUCK 103` | `outputs/wave1-actions.csv` + `outputs/wave1-actions.json` |
| `--compare` 通道 | 用 4 条手造真值跑通：命中 2、漏报 2、误报 127，且控制台汇总 / 报告文件全量明细分工正常 | `.deepworks/tmp/diag_flicker.py`、`wave1.labels.report.txt` |

> **数据来源注记（2026-10-15）**：上表引用的原始录制 `wave1.bin`（120,675,244 B = 115.08 MB，
> sha256 `6e06ec8d9a6b128a7dc03ed6a9f01a3365bbf6686ad82426009383192015ec14`）**已按用户要求删除**
> ——它本身就是「软件不停机 → 雷达一直录」的产物，无真值、只作对照。
> 它的逐帧特征与事件序列**完整保留**在 `outputs/wave1-actions.csv` / `outputs/wave1-actions.json`
> （16959 行，816 KB），本页所有统计都可由这两个文件复算；
> 分析脚本 `.deepworks/tmp/probe_wave1.py`、`.deepworks/tmp/diag_flicker.py` 也仍在。

## 8.1 「目标断续」的实测结论（修正 §8 表格下方那句旧话）

对 `outputs/wave1-actions.csv` 的逐帧统计（`.deepworks/tmp/diag_flicker.py`，只读）：

| 统计 | 结果 | 说明 |
|---|---|---|
| 带内 0 点的帧 | **9562 / 16959 = 56.4%** | 不是「门限附近起伏」，是**带内真的一个点都没有** |
| `target=0` 的帧里 `n=0` 的比例 | **100%** | 丢失判定 100% 由「0 点」触发，没有一帧是「有点但被判不合格」 |
| `target=1` 且 `n=0` 的帧 | 4487（占 target=1 的 60.7%） | 这是 `LOST_TIMEOUT=0.6 s` 的**滞回保持**（median 有目标段 0.65 s ≈ 5 帧），不是 bug |
| LOST 段 | 609 段，中位 1.17 s，中90 4.79 s，**最长 23.4 s** | 段长呈「长平台」而非逐帧抖动 |
| 有目标段 | 608 段，中位 0.65 s，中90 3.24 s，最长 15.4 s | 同上 |

**结论**：断续的主因是**窄带**——默认带只留 j=1（0.70~1.35 m），人一旦走出这 0.65 m 宽的扇环就立刻「消失」。
所以 §8 表格下方原写的「门限附近的正常起伏」说法**不准确**，已按实测更正。
这也解释了为什么长录制不适合做真值：36.6 min 里站位在变，没有统一真值。

## 9. 局限与未验证（不得当作已完成）

- **左/右的准确率未知**：`wave1.bin` 没有真值，无法给精确率/召回率。必须先打标
  （`label_tool.py` 已跑通，`--compare` 通道用 4 条手造真值验证过命中/漏报/误报与报告文件；
  新增的 `segment_capture.py` 把「采集 + 自动真值 + 对照」合成一步，**离线自检通过、真机采集待执行**）。
- **建议的采集方式改为「分段定距」**：既然窄带使带内点数对站位敏感（实测 56.4% 帧为 0 点），
  应在 **1.0 / 1.25 / 1.5 m 各录一段**（每段 60~90 s、每段只做一种动作若干次），
  分段打标、分段 `--compare`；一条 36 min 的长录制无法给出可解释的真值。
  已实现为一键脚本：`python segment_capture.py --port COM3`（提示音驱动、按帧号自动打真值，
  约 6 分钟；⚠ 1.0 m 与 1.25 m 落**同一距离门 j=1**，两段只能分开看，不能声称按距离带区分）。
- **1.4 m 以上未覆盖**：默认带只含 j=1；1.5 m 档需要 `--band 1.35 2.05` 单独验证。
- **蹲/跳**：**1T2R 无俯仰角**（AGENTS.md 事实基线）；2T4R 或可测俯仰角，但**链路正在验证中**（P0，10/16 起实验中，结论未出）。当前只是能量突变候选，**未过 P0→T0→T1→T2 验证门**。
  更进一步：实测 36.6 min 内产生 `JUMP` 候选 **556 次（约每 3.9 s 一次）**、`DUCK` 103 次，
  而这段录制里人并没有在反复跳；能量比分布（中位 5.05、最大 70739，仅 15% 落在 2.0~2.1 的刚过阈值区）
  说明这不是阈值卡边，而是**「窗口能量中位数」这个基线本身在带内稀疏（56.4% 帧为 0 点）时不稳定**，
  以它做比值无法区分跳与日常动作。**在通过真值测试之前，候选门不应被视为有效信号**；
  改进方向（绝对能量下限 + 连续 N 帧确认 + 用宽带能量而非单纯比值）需纳入 P0→T0→T1→T2 流程后再定。
- **硬件停机**：方案与工具已就绪；**「进/出命令模式」已实机验证（T1，9/9，未写寄存器，
  见 `radar-hardware-stop-plan.md` §7.1）**，但**写停机寄存器那一步未验证**（操作前仍需确认能重新插拔 USB 兜底）。
  顺带纠正一处判读：本固件默认「老约定 status=1 即 ACK_OK」（`cmdprocess.c` L122-143），
  `radar_hwstop.py::parse_ack()` 与 `selftest.py` 测试 5 已按此修正。
- **真机验收本身的覆盖边界（诚实说明）**：
  - 脚本用 `CTRL_BREAK` 结束服务进程（Windows 上**不等价于**控制台 `Ctrl+C`），因此
    `finally: ctrl.shutdown()` 这条「优雅退出」路径**没被真机覆盖**（软件层由 `selftest.py` 测试 4 与 E2E 覆盖）。
    真机上验证到的是「stop 释放串口」与「进程结束后 OS 回收串口」这两条实用性质。
  - 「结束 → 再开始」时**往同一个 `--record` 文件追加**（`record_append`）本轮未启用 `--record`，
    未做真机验证。
  - 「外部能否打开 COM3」这个判定是**真的去 open 一次**：串口打开瞬间会拉 DTR/RTS，
    可能让 DEV202H 复位重新出帧（脚本已尽量在打开后立刻将 DTR/RTS 置低），
    所以该检查只用于证明占用/释放，不用于判定设备状态。
- **方位角符号**：合成闭环只证明自洽，真机左右符号仍建议用一次明确标注的挥手确认（必要时 `--angle-sign` 翻转）。
- `p['p']`（帧内归一化）与 `p['pa']`（绝对功率）用途不同：动作识别用 `pa` 做幅值加权；展示可用 `p`。

## 10. 相关文件

| 文件 | 作用 |
|---|---|
| `radar-pointcloud/radar_control.py` | 会话状态机、停滞自愈、`HEAT_NOTE` |
| `radar-pointcloud/radar_actions.py` | 规则法动作识别 + 离线分析 CLI |
| `radar-pointcloud/label_tool.py` | 真值标注（回放打标）+ 预测 vs 真值对照 |
| `radar-pointcloud/segment_capture.py` | **分段定距采集器**：提示音驱动 + 按真实帧号自动打真值 + 自动逐段/合计对照（离线自检通过，真机采集待执行） |
| `radar-pointcloud/radar_hwstop.py` | 硬件停机帧构造（默认 dry-run，实验） |
| `radar-pointcloud/server.py` | HTTP/SSE 服务、`/control`、1 Hz 控制心跳 |
| `radar-pointcloud/viewer.html` | 开始/结束/重置按钮、采集会话与近距动作面板 |
| `docs/radar-hardware-stop-plan.md` | 硬件停机方案（命令模式连通性已实机验证，写寄存器停机未验证） |
| `docs/radar-uplink-protocol.md` | 上行协议（注意：其「不依赖下行命令」的表述已被固件源码修正） |
