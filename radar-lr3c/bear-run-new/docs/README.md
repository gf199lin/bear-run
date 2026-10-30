# 项目文档索引

熊二快跑（Bear Run）· 雷达体感跑酷 MVP —— 设计、需求与评审文档。

## 正式交付物

| 文档 | 说明 |
|---|---|
| [PRD-BearRun-MVP-v1.0.md](PRD-BearRun-MVP-v1.0.md) | 产品需求文档 v1.0（提交版），含用户场景、功能条款 BR/EC/NFR、验收口径 |
| [PROPOSAL-BearRun-v1.0.md](PROPOSAL-BearRun-v1.0.md) | 项目提案 v1.0（提交版），含选题依据、方案对比、分工与排期 |

## 设计方案

| 文档 | 说明 |
|---|---|
| [shield-magnet-design.md](shield-magnet-design.md) | 护盾与磁铁道具的设计稿（仅设计，是否施工待定） |

## 截图

`images/` 下为 PRD 引用的界面截图。

| 文件 | 内容 |
|---|---|
| `01-menu.png` | 主菜单 |
| `02-play-settlement.png` | 局内画面与结算 |
| `03-action-jump.png` | 跳跃动作 |
| `04-action-slide.png` | 下蹲滑行动作 |

---

## 雷达数据相关文档

以下三份文档涉及上游设备参数与采集数据集，**已确认可用于本次课程作业的公开提交**，仅用于教学与项目演示：

| 文档 | 说明 |
|---|---|
| [radar-dataset-evaluation-2.md](radar-dataset-evaluation-2.md) | 两批雷达数据的可用性评估（跳/蹲 + 左右） |
| [radar-calibration-plan.md](radar-calibration-plan.md) | 雷达数据评估、采集方案与开局校准设计 |
| [radar-data-request-v3.md](radar-data-request-v3.md) | 数据采集需求与字段约定 |

## 雷达服务与工具

雷达侧代码在 `radar-service/`，日常启动入口见仓库根 `RADAR-SERVICE.md`。

| 文档 | 说明 |
|---|---|
| [radar-autoconnect-handoff.md](radar-autoconnect-handoff.md) | 双雷达自动识别、板型判定、推流与采集的实现和验证记录 |
| [boot-2t4r-pure-python.md](boot-2t4r-pure-python.md) | 2T4R 脱离 ONELAB 的纯 Python 启动（离线校验通过，实机未验证） |
| [双雷达双开-组员操作手册.md](双雷达双开-组员操作手册.md) | 两块板各跑一个后台判定进程 + hub 合并的操作手册 |

工具入口：

| 位置 | 用途 |
|---|---|
| `radar-service/2-检查雷达.cmd` | 列出串口、判定板型、看帧率 |
| `radar-service/3-采集数据.cmd` | 采集原始字节（结果在 `captures/`，不入库） |
| `radar-service/5-推流到游戏.cmd` | 自动分配左右/上下两路 WebSocket |
| `radar-service/radar_boot_2t4r.py` | 2T4R 启动序列重放 |
| `radar-service/tools/rx_pair_probe.py` | 2T4R 四通道对角探针 |
| `radar-service/tools/horizontal_guide.py` | 横向方位角标定引导 |
| `tools/run-dual.bat` | 双雷达双开（后台判定 + hub 合并） |
| `tools/run-dual-demo.bat` | 无硬件自测同一条链路 |

仓库不包含雷达原始回波 `.bin` 抓包、图像或音频；`_radarreplay/` 下仅为动作回放所需的逐帧特征序列。
如需复用这些材料，请注明来源并遵守课程的数据使用要求。
