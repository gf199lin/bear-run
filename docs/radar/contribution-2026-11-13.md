# 雷达侧贡献索引（2026-11-13）

> 负责人：@jqz070803（雷达数据采集、帧解析、跳蹲判据与工具）
> 关联分支：`radar-lr3c`（雷达侧独立历史，与 `main` 无共同祖先）
> 本文件性质：**在 main 上新增的独立索引文件**，不修改 `docs/radar/` 下任何既有文件，
> 也不改动其他成员的提交内容。若与既有文档冲突，以既有文档为准。

---

## 一、本文件为什么存在

GitHub 的贡献图、Contributors 和主页计数**只统计默认分支 `main`**。
雷达侧 2026-11-13 的工作全部提交在 `radar-lr3c` 分支上（该分支与 main 是两条独立历史，
不能直接 merge），因此：

- `radar-lr3c` 分支页显示 **50 条本人提交**；
- GitHub 主页/Insights 仍只显示 main 上能关联到本账号的 **38 条**。

本文件在 main 上留下一个可检索入口，方便验收时按「分支 + 提交数 + 内容」核对，
而不是只看主页的单一数字。

---

## 二、2026-11-13 的 7 条提交（全部在 `radar-lr3c`）

| # | 提交 | 主题 |
|---|---|---|
| 1 | `499cff2` | 逐帧跳蹲状态机 + 判别规格；四批 96 样本 85.4% |
| 2 | `40ef0c3` | 跳蹲采集电池与四批分析（116 块有效、真值标注 + 自适应 QC） |
| 3 | `599f5e0` | 游戏侧 `radar_live_ws` 接入跳蹲状态机（`--jump-engine sm`） |
| 4 | `872c2a7` | 引导站距 1.5→1.2 m、预备时长可配、浏览器打开兜底 |
| 5 | `c684c87` | 竖直采集引导兼容 `radar-service/` 目录布局 |
| 6 | `6edc8d6` | 四批代表性数据 13 块 / 2.67 MB + 真值标注 |
| 7 | `9a7cab0` | 2026-11-13 贡献记录（判据、结论、失败边界、提交清单） |

---

## 三、提交数口径（实测）

| 位置 | 数量 | 说明 |
|---|---|---|
| `radar-lr3c` 分支 | **50** | 全部为 noreply 邮箱，可关联 @jqz070803 |
| `main` 默认分支 | **38** | 259 条提交中可关联本账号的部分；另有 10 条早期旧邮箱（`jiangqinzhao@local` 8 条、`jqz0803@qq.com` 2 条）无法关联 |
| 2026-11-13 新增 | **7** | 全部在 `radar-lr3c`，按 GitHub 规则不计入主页 |

核对命令（在雷达侧仓库根目录执行）：

```bat
git log master --oneline --author=jqz070803
git log master --oneline --since="2026-11-13 00:00:00"
git ls-remote origin refs/heads/radar-lr3c
```

---

## 四、2026-11-13 的结论摘要（详细版见第五节链接）

### 4.1 1T2R 方位角在本硬件上不可用

`rest0` 段（93 帧、无人动）方位角跨度 159.4°，`dphi` 在 ±3.14 满幅跳；
跳 146–165°、左/右 43–165°、弯腰 148–156°，各组无差异。
根因：带内九成以上能量挂在零多普勒（静态杂波）上。
因此全部判据只依赖多普勒轴，与板子正放/竖放无关。

### 4.2 谱宽分「径向 / 切向」，但不能分「跳 / 蹲」

- 静息 `nz_p90 = 0.037`，动作 0.52–0.94；
- 切向 `w90` 0.15–0.20，径向 0.29–0.54，门限 **0.22**；
- 大跳 vs 深蹲 `w90`：Cohen d = +0.06，完全重叠。

### 4.3 跳/蹲改用「径向速度同号段长度」

| 动作 | 最长负段 `nb` | 最长正段 `pb` |
|---|---|---|
| 轻跳 | 1–2 帧 | 0–2 帧 |
| 深蹲 | 4–6 帧 | 4–6 帧 |

分界 **3 帧 ≈ 0.39 s**。因果 EMA（`alpha=0.5`）解决浅蹲符号抖动。

### 4.4 状态机性能（四批 96 个跳/蹲样本）

| 指标 | 结果 |
|---|---|
| 总判对 | 82/96 = **85.4%** |
| 蹲 | 47/48 = **97.9%** |
| 跳 | 35/48 = 72.9% |
| 轻跳 + 深蹲专项 | **24/24** |
| 延迟中位 | 388 ms |
| 静息误触发 | 0 次 |

失败 14 例集中在自然大幅跳（9 例）与部分中跳（4 例），另 1 例浅蹲负段仅 2 帧；
**没有一例来自轻跳或深蹲**。

### 4.5 失败边界

自然大跳的蓄力下蹲与浅蹲在 `nb`、`negPk`、位移上全部重叠，是任务本身的边界，
不是调参能解决。全部阈值为**提案，待小组确认**，且为单人同日采集。

---

## 五、完整证据位置（均在 `radar-lr3c` 分支）

| 内容 | 链接 |
|---|---|
| 2026-11-13 贡献记录（含 7 条提交与判据参数表） | [docs/contribution-log-1113.md](https://github.com/gf199lin/bear-run/blob/radar-lr3c/docs/contribution-log-1113.md) |
| 跳蹲判别规格（规则 + 状态机 + 参数） | [docs/jumpduck-decision-spec.md](https://github.com/gf199lin/bear-run/blob/radar-lr3c/docs/jumpduck-decision-spec.md) |
| 四批数据分析（过程与失败样本） | [docs/jumpduck-battery-analysis-1113.md](https://github.com/gf199lin/bear-run/blob/radar-lr3c/docs/jumpduck-battery-analysis-1113.md) |
| 采集说明（摆法、参数、四组电池） | [docs/jumpduck-battery.md](https://github.com/gf199lin/bear-run/blob/radar-lr3c/docs/jumpduck-battery.md) |
| 代表性数据（13 块 / 2.67 MB） | [outputs/jumpduck-battery-1113-samples/](https://github.com/gf199lin/bear-run/tree/radar-lr3c/outputs/jumpduck-battery-1113-samples) |
| 块级全量特征 / 逐块判定 | [blockfeat.csv](https://github.com/gf199lin/bear-run/blob/radar-lr3c/outputs/jumpduck-battery-1113-blockfeat.csv)、[rule-eval.csv](https://github.com/gf199lin/bear-run/blob/radar-lr3c/outputs/jumpduck-battery-1113-rule-eval.csv) |
| 全部 50 条提交 | [commits?author=jqz070803](https://github.com/gf199lin/bear-run/commits/radar-lr3c?author=jqz070803) |

完整原始数据（6 个 session、约 46 MB）保留在本地 `data/jumpduck_battery/`，按项目约定不入库；
入库的是最小可复现集与全量逐块特征。

---

## 六、证据边界

- 本文件只陈述**已执行**的提交与实测结论，不把计划写成结果；
- 阈值均为提案，待小组确认；96 样本为单人同日采集，换人/换场地未验证；
- 采集数据为不含图像的人体雷达回波，仅课程内部使用；
- 本次改动**只新增本文件**，未修改 `docs/radar/` 下任何既有文档、代码或其他成员提交。
