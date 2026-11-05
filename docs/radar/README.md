# 雷达侧工作档案（索引）

> 归属：雷达数据采集与帧解析（@jqz070803）
> 时间跨度：2026-10-15 → 2026-11-05
> 说明：本目录归档雷达侧的过程记录与结论。代码在 `radar-service/`，
> 通用工具在 `tools/`，两者不在此目录重复收录。

本索引按主题说明**每个文件解决什么问题、留下什么结论**，便于验收时逐份回溯。
标「提案，待小组确认」的数字仍属提案，不是已验收结果。

---

## 一、事实基线与链路协议

| 文件 | 作用 | 关键结论 |
|---|---|---|
| [hardware-baseline.md](hardware-baseline.md) | 硬件与工具链的已核实事实 | CP2102 + CP210x 驱动（不是 FTDI）；1T2R/2T4R 双板并存；垂直角不可用 |
| [radar-uplink-protocol.md](radar-uplink-protocol.md) | DEV202H（1T2R）上行协议逆向 | 帧头/校验/字段的实际布局，用于脱离厂商工具解析 |
| [radar-uplink-dev113p-2t4r.md](radar-uplink-dev113p-2t4r.md) | DEV113P（2T4R）上行协议实测破解 | 裸帧 136 B、`55 AA` 帧头、4 RX 拼帧口径 |
| [radar-control-and-actions.md](radar-control-and-actions.md) | 采集控制（开始/结束/重置）与近距动作识别 | 命令序列与 ACK 语义；近距动作的可行性边界 |
| [radar-serial-preflight.md](radar-serial-preflight.md) | 换板前的串口预检 | 换板前先确认端口/驱动/出流，避免把「没连上」误判成「算法不行」 |

## 二、左右移动识别

| 文件 | 作用 | 关键结论 |
|---|---|---|
| [lane-recognition-ceiling.md](lane-recognition-ceiling.md) | 机器学习路线为什么在这批数据上不成立 | 数据量与时序分辨率决定上限，规则法更稳 |
| [lane-latency-and-thresholds.md](lane-latency-and-thresholds.md) | 门限与延迟实测（2026-11-03） | 右门 0.18→0.12；lr3c 实测 93.8%（RIGHT 4/8→7/8） |
| [lr3b-dataset-review.md](lr3b-dataset-review.md) | lr3b 数据集复核 | 电脑一侧反射更强，取「最强点」会锁到电脑；必须排除该扇区 |
| [handoff-leftright-radar.md](handoff-leftright-radar.md) | 左右移攻关全过程交接 | hold 误触发修复、`crit='xbase'` 落码、93% 宽口径/71% 严口径的差别 |
| [handoff-lr2-1022.md](handoff-lr2-1022.md) | seg-lr2 两轮采集与判据诊断 | 检出率受损的根因是固定距离带只覆盖一个 bin |
| [验证报告-左右移动识别.md](验证报告-左右移动识别.md) | 左右移识别的验证报告 | 逐条给出通过项、失败项与证据位置 |
| [静止误报诊断-左门与噪声.md](静止误报诊断-左门与噪声.md) | 「站着不动被判成向左」的根因分析 | 静止泄漏与噪声结构；单纯压阈值会杀 LEFT |

## 三、跳 / 蹲验证

| 文件 | 作用 | 关键结论 |
|---|---|---|
| [jump-duck-verification-plan.md](jump-duck-verification-plan.md) | P0→T0→T1→T2 验证门 | 决策走哪条路之前，不把跳蹲写成「已实现」 |
| [jump-duck-sampling-limit.md](jump-duck-sampling-limit.md) | 2T4R 采样上限 | 7.7 Hz 下跳这个动作的采样边界；chirps 必须与 ini 一致 |
| [jump-duck-recognizer.md](jump-duck-recognizer.md) | 采集→训练→实时→游戏的识别程序 | 离线可训练不等于实时可用 |
| [jump-duck-data-audit.md](jump-duck-data-audit.md) | 17 轮 2T4R 数据为什么训不出识别 | 只存汇总值、丢原始帧，导致无法换特征重算 |

## 四、采集、标定与实验

| 文件 | 作用 | 关键结论 |
|---|---|---|
| [capture-checklist.md](capture-checklist.md) | 采集清单 | 一次采集要同时解掉的悬案，避免反复重采 |
| [calibration-results.md](calibration-results.md) | 双板标定实验 | 1T2R 测左右、2T4R 测上下的分工依据 |
| [bg-baseline-verification.md](bg-baseline-verification.md) | 空场背景基线的离线验证 | 背景噪声校准的有效性与边界 |
| [center-line-45.md](center-line-45.md) | 1T2R 最佳中线 | 实测 +8°（后统一为 +9°），「45°」假设被推翻 |
| [dual-board-interference.md](dual-board-interference.md) | 双板共存干扰 | 1T2R 与 2T4R 同时工作是否互相影响 |
| [dual-board-parallel.md](dual-board-parallel.md) | 双板并行采集 | 并行采集的端口与节奏约定 |
| [横穿扫描实验-20261022-交接.md](横穿扫描实验-20261022-交接.md) | 横穿扫描实验交接 | 实验条件、原始结果位置与待分析项 |
| [radar-hardware-stop-plan.md](radar-hardware-stop-plan.md) | 雷达硬件停机方案 | 连通性已实机验证；**停机本身未验证** |

## 五、对接与验收

| 文件 | 作用 | 关键结论 |
|---|---|---|
| [radar-to-game.md](radar-to-game.md) | 雷达 → Bear Run 的接入路径 | 插上就能玩所需的完整链条 |
| [acceptance-test-table.md](acceptance-test-table.md) | 验收测试表 | 逐条列出验收项、判定与证据位置 |

---

## 与主仓库的关系

- 本文档目录是**过程与结论档案**；可运行代码以 `radar-service/` 为准。
- 部分文档写作时引用的是当时的分支内路径，若链接失效，以文件名为准在
  本目录内检索。
- 采集原始数据（`data/`、`*.bin`、`*.log`）按保密与体积要求不入库。
