# 跳 / 蹲识别程序：采集 → 训练 → 实时 → 游戏

> 状态（2026-11-02）：**默认判据 = 「期望形态」先验规则**（`--rule prior`），
> 已接入游戏链路并在联调台可见；训练模型降级为可选项（`--rule model`）。
>
> 指标：动作窗口方向准确率 **57.5%**（LOSO），**静止误报 0%**。
> 方向仍接近随机，原因见 [`jump-duck-data-audit.md`](jump-duck-data-audit.md)：
> 现有数据不满足先验判据要求的形态（帧率 1.9 Hz、跳是「深预蹲+浅上冲」）。
>
> **采样上限**：2T4R 帧周期 0.52084 s，腾空 0.4–0.5 s 只能采到 0–1 帧，
> 3 帧在物理上不可能。见 [`jump-duck-sampling-limit.md`](jump-duck-sampling-limit.md)。
>
> 在 T2 决策完成前，跳/蹲仍不得写进「已实现功能」或验收结论（AGENTS.md 硬约束 5）。

---

## 1. 结构：为什么分两层

```
2T4R 串口
  → 帧装配 (4 RX × 64 chirp × 32 bin)
  → pitch_from_frame()            俯仰相位差（rx0-rx1，带内能量加权圆平均）
  → JumpDuckRecognizer
        ├─ 层 1：有没有动作  ← 规则：窗内 |偏离| ≥ min_dev（默认 8°）
        └─ 层 2：跳还是蹲    ← PriorJudge（默认）/ SoftmaxModel（可选）
  → ws 帧 {jump, crouch, confidence, raw.reason, raw.pitch, ...}
  → 游戏 DualRadarInputSource（role=ud）
  → 联调台「上下 · 判据」实时显示 reason / pitch / 基线 / hi / lo
```

**为什么这么切**：数据证明「有没有动作」是可靠的（17/17 轮都有明显偏离），
「跳还是蹲」不可靠。把两者拆开，换判据时规则层不用动；
方向判不准时也不影响「有没有动」的调试。

### 1.1 层 2 的默认判据：期望形态（PriorJudge）

不训练，直接把我期望的数据形态写成可执行判据（`EXPECTED_SPEC`）：

| 动作 | 期望形态 | 判据 |
|---|---|---|
| 跳 | **短促上冲**：上冲 ≥8°，只持续 1–2 帧，幅度 ≥ 预蹲深度的 60% | `hi ≥ min_dev` 且 `上冲帧数 ≤ max_up_frames` 且 `pos ≥ pos_over_neg × neg` |
| 蹲 | **持续下沉**：下沉 ≥8°，低位 ≥2 帧，深度 ≥ 站起幅度的 120% | `lo ≤ -min_dev` 且 `下沉帧数 ≥ min_neg_frames` 且 `neg ≥ neg_over_pos × pos` |

**为什么必须用「比例」而不是「极值方向」**：跳和蹲都会先向下（预蹲 / 下蹲），
只看极值会把所有跳都判成蹲。跳的区别是「向上那一半够强、够短」。

判据输出可解释的 `reason`，直接出现在联调台：

| reason | 含义 |
|---|---|
| `up_peak_short_strong` | 判跳 |
| `down_trough_sustained` | 判蹲 |
| `ambiguous` | 动了但方向不明确 → 不出事件 |
| `below_min_dev` | 没动 |

## 2. 文件

| 文件 | 作用 |
|---|---|
| `tools/jumpduck_core.py` | 特征、softmax 模型、实时状态机、俯仰角提取（**训练/推理共用**） |
| `tools/jumpduck_capture.py` | 采集**原始帧** + 真值标注 → `frames.npz` / `labels.json` / `pitch.csv` |
| `tools/train_jumpduck.py` | 训练 + 留一会话验证 + 导出 `models/jumpduck.json` |
| `tools/jumpduck_ws.py` | 实时桥：串口 → 识别 → `ws://127.0.0.1:8767`（role=ud） |
| `tools/eval_jumpduck_offline.py` | 旧数据可分性审计（只读 `data/vertical`） |
| `models/jumpduck-prior.json` | **默认判据**的校准参数（`usable: false`） |
| `models/jumpduck.json` | 可选训练模型权重（`usable: false`） |

## 3. 三步用法

### 3.1 采集（需要 2T4R 插在 COM4）

```bash
python tools\jumpduck_capture.py --port COM4 --rounds 3
```

每轮 5 段：站好 → 跳三次 → 站好 → 蹲三次 → 站好。
输出到 `data/jumpduck/<时间戳>/`：

- `frames.npz`：`z=(N,4,64,32) complex64`，**原始复数据，这才是能反复换特征的东西**
- `labels.json`：每段起止帧号 + 真值 + 采集参数
- `pitch.csv`：每帧 `pitch_rx01` / `pitch_rx03`，方便肉眼检查

> 不要再用 `tools/vertical_probe.py` 采训练数据：它只存段均值，
> 原始帧丢了，等于把 10/29 那 17 轮的坑再踩一遍。

### 3.2 训练

```bash
# 重采后的原始帧（推荐）
python tools\train_jumpduck.py --data data\jumpduck\<时间戳>

# 旧数据（result.json 里的 pitch_seq，只用于复现审计结论）
python tools\train_jumpduck.py
```

输出 `models/jumpduck.json`，其中：

- `usable: true/false` —— 是否达到接入门槛
- `metrics.loso_accuracy` —— 留一会话准确率
- `metrics.per_session` —— 逐会话命中，最容易被平均值掩盖的地方

当前模型：`usable: false`，`loso_accuracy: 0.5579`。

### 3.2.1 校准先验判据（默认走这条）

```bash
python tools\train_jumpduck.py --calibrate-prior
```

它保持判据形式不变，只搜索 6 个阈值（`min_dev / frame_t /
max_up_frames / min_neg_frames / pos_over_neg / neg_over_pos`），
并且**照样按会话留一验证**。输出 `models/jumpduck-prior.json`。

当前结果：动作窗口 **57.5%**，静止误报 **0%**；
全量最优参数被推到 `up=3, dn=1, pon=0.9` —— 这本身就说明数据里
没有「短促上冲」的形态，判据只能被迫放宽。

### 3.3 推流

```bash
# 真机（2T4R 管跳蹲）—— 默认用期望形态判据
python tools\jumpduck_ws.py --port COM4 --ws-port 8767

# 判据来源三选一（默认 prior）
python tools\jumpduck_ws.py --port COM4 --rule prior   # 期望形态（默认，可解释）
python tools\jumpduck_ws.py --port COM4 --rule model   # 训练模型
python tools\jumpduck_ws.py --port COM4 --rule auto    # 模型 usable=true 才用模型，否则 prior

# 没有板子：回放历史原始流，验证解析/推理链路
python tools\jumpduck_ws.py --replay .deepworks\tmp\seg-2t4r\2t4r\d1.00\seg_01.00.bin --board 2t4r

# 状态机自测（合成波形，不碰硬件）
python tools\jumpduck_ws.py --selftest
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--rule` | `prior` | `prior` 期望形态 / `model` 训练模型 / `auto` 模型可用才用 |
| `--prior` | `models/jumpduck-prior.json` | 判据参数文件（`--calibrate-prior` 生成） |
| `--rx-pair` | `0,1` | 俯仰相位差的 RX 对；`0,3` 是 3 倍基线（会卷绕，需谨慎） |
| `--band` | `6,21` | 俯仰角加权用的距离门（2T4R 0.15 m/门 → 0.90–3.15 m） |
| `--min-dev` | 8.0 | 动作门限（度）；低于它判静止 |
| `--base-frames` | 8 | 启动基线帧数（1.9 Hz ≈ 4.2 s），期间请站好别动 |
| `--no-model` | 关 | 只跑规则兜底，验证链路 |

## 4. 接进游戏（双通道）

游戏端**已经支持**两条独立 WS，不需要改游戏代码：

| 通道 | 端口 | 板子 | role |
|---|---|---|---|
| 左右换道 | 8766 | 1T2R | `lr` |
| 跳 / 蹲 | 8767 | 2T4R | `ud` |

启动顺序：

```bash
# 1) 左右（已验收链路，保持不变）
python tools\bear_run_radar_launcher.py --no-open

# 2) 上下（本程序）
python tools\jumpduck_ws.py --port COM4 --ws-port 8767

# 3) 本地托管游戏页
python tools\serve_bear_run.py
```

浏览器：

1. 打开 <http://127.0.0.1:8771/radar-connect.html>
2. 勾选「双雷达」，第一路填 `ws://127.0.0.1:8766`，第二路填 `ws://127.0.0.1:8767`
3. 点「进游戏」（它会把两路地址写进本地配置，再跳转）

> 游戏页本身只解析 `?radar=`；第二路来自联调台写入的 `udUrl` / `dual` 配置，
> 所以**必须先进联调台点一次「进游戏」**，直接手拼 URL 不会生效。

## 5. 当前指标（务必按这个口径引用）

| 项目 | 数值 | 口径 |
|---|---|---|
| **先验判据（默认）动作窗口** | **57.5%（298/518）** | 3 帧窗，LOSO，静止窗口不计入 |
| **先验判据 静止误报** | **0.0%** | 167 个静止窗口无一误报 |
| 先验判据 默认阈值（未校准） | 34.0% | 说明校准确实有用 |
| 训练模型 LOSO | 55.8% | 同类数据、同窗口口径 |
| 段级多特征 LOSO | 58.8%（20/34），CI 42.2–73.6% | 17 轮 × 跳/蹲 两样本 |
| 逐会话范围（判据） | 19.7% ~ 76.9% | 215347 只有 19.7% |
| 合成标准波形 | 100%（JUMP + DUCK 都触发，reason 正确） | 证明判据实现无 bug |
| **回放「左右动作」数据（无跳蹲）** | **238 帧出 22 次事件（9.2%）** | `.deepworks/tmp/seg-2t4r/.../seg_01.00.bin` |
| 同上，`--min-dev 14` | 18 次（7.6%） | 收紧门限也压不下去 |

**接入门槛**：动作窗口 LOSO ≥ 75%，且新会话复测通过。
当前 57.5%，不满足。

> ⚠ **左右动作会误触发跳蹲**：这段 2T4R 数据是左右挥手采集的，本没有跳蹲，
> 判据仍出了 22 次事件。说明单靠俯仰角无法区分「跳蹲」与「水平大动作」。
> 现场若同时开两路，挥手换道时可能伴随误跳。
> 缓解手段：`--min-dev 14`、或演示时只开左右、或等重采后加水平角互斥。

## 6. 已知限制

1. **帧率**：2T4R 527 ms/帧，3 帧窗 = 1.6 s 延迟。跳一次只采 1–2 帧，
   这是方向判不出来的首要原因，改代码解决不了（要改雷达配置）。
2. **只用了两元相位差**：`rx0-rx1` 灵敏度低。`jumpduck_capture.py` 已保存
   全部 4 个 RX，`--rx-pair 0,3` 可以先试长基线，但会卷绕，正式方案应是 4 元 DBF。
3. **启动基线**：前 8 帧只标定不出事件。标定期请站在正前方别动。
4. **边沿锁存**：一次动作只出一次事件，必须等窗口彻底平静才允许下一次；
   连续快速跳两次会漏掉第二次。
5. **1T2R 没有垂直角通道**，不能像 2T4R 那样直接测俯仰（AGENTS.md 事实基线）；
   但 2026-11-12 起改用「降低雷达高度 + 多普勒谱宽」路线：在
   「雷达离地约 0.5 m + 人站约 1.2 m」摆法下，四批实测事件级 **46/49 = 93.9%**
   （见 [`vguide-1t2r-analysis-1112.md`](vguide-1t2r-analysis-1112.md)）。
   该结果**尚未通过 T2 决策、阈值属提案**，因此仍不得写进「已实现功能」或验收结论。
6. **先验判据依赖形态**：它假设「跳 = 短促上冲、蹲 = 持续下沉」。
   如果实际动作是「深预蹲 + 浅上冲」，判据会把跳判成蹲或 `ambiguous`。
   校准结果把 `up` 放宽到 3、`dn` 压到 1，就是数据不满足假设的证据。
7. **判据仍是逐窗口判定**：一个人动作慢、或站在 3 m 外，幅度会整体变小，
   需要重新校准 `min_dev`，不能一套阈值走天下。
8. **分不清跳蹲与水平大动作**：2T4R 只有俯仰角可用，左右挥手同样会
   造成大幅俯仰波动。回放实测 238 帧误报 22 次（9.2%）。
   两路同开时，建议把 `--min-dev` 提到 14，或只开左右做演示。
9. **2T4R 采不到跳**：帧周期 0.52084 s，腾空 0.4–0.5 s 只能采到 0–1 帧，
   `up_frames` 实际退化为 1，"短促上冲"根本采不出来。
   详见 [`jump-duck-sampling-limit.md`](jump-duck-sampling-limit.md) —— 这是硬件上限，不是判据问题。

## 7. 自测证据（2026-11-02）

```
python tools\jumpduck_ws.py --selftest

合成序列：站立 → 上冲(+12,+18) → 站立 → 下冲(-12,-18) → 站立
--- A. 无判据（验证状态机） ---
  触发：第10帧 JUMP, 第17帧 DUCK
--- B. 先验判据（EXPECTED_SPEC） ---
  触发：第10帧 JUMP, 第17帧 DUCK
  理由：JUMP:up_peak_short_strong / DUCK:down_trough_sustained
--- C. 训练模型 ---
  触发：第18帧 DUCK
```

A 证明状态机（基线 / 门限 / 边沿锁存）正确；B 证明**期望形态判据在标准波形上
两个方向都对**；C 说明训练模型在这条合成序列上只出 DUCK —— 与它 55.8% 的
真实水平一致，不是接线错误。

回放验证：

```
2T4R  .deepworks\tmp\seg-2t4r\...\seg_01.00.bin  238 帧，坏包 60
1T2R  .deepworks\tmp\seg-jd2\seg_01.00.bin       1739 帧，坏包 0
```

## 8. 什么时候可以把 `usable` 打开

1. 用 `jumpduck_capture.py` 重采 ≥ 5 个会话（不同人 / 不同安装高度）；
2. 确认数据真的满足 `EXPECTED_SPEC`：帧率 ≥5 Hz、跳能采到 3 帧以上；
3. `train_jumpduck.py --calibrate-prior --data ...` 的动作窗口 LOSO ≥ 75%；
4. 留一个**完全没参与校准**的会话来复测，确认不掉；
5. 现场用 `--quiet` 跑 10 分钟，统计误报次数，再决定是否上演示。

在此之前，`jumpduck_ws.py` 只能作为实验通道，演示仍以 1T2R 左右为主。
