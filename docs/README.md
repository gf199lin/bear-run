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

仓库不包含雷达原始回波 `.bin` 抓包、图像或音频；`_radarreplay/` 下仅为动作回放所需的逐帧特征序列。
如需复用这些材料，请注明来源并遵守课程的数据使用要求。
