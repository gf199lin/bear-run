# 贡献记录与身份说明（雷达侧）

> 负责人：@jqz070803（雷达数据采集与帧解析）
> 整理日期：2026-11-05
> 用途：说明本人在仓库中的提交分布与作者身份，便于验收时核对。

---

## 一、作者身份的说明（重要）

本项目早期提交使用过两个不同的 Git 作者邮箱：

| 邮箱 | 能否被 GitHub 关联到账号 | 说明 |
|---|---|---|
| `302337358+jqz070803@users.noreply.github.com` | ✅ 能 | GitHub 为该账号生成的 noreply 地址 |
| `jiangqinzhao@local` | ❌ 不能 | 本地临时邮箱，不是有效域名，GitHub 无法验证与关联 |

**影响**：使用 `jiangqinzhao@local` 提交的 commit，内容真实存在，但在 GitHub 的
贡献图与作者页上不会被统计到 `@jqz070803` 名下。

**已处理**：

- `radar-lr3c` 分支上的 38 个 commit 已全部修正为 noreply 邮箱（改写该分支历史后重推）。
- 默认分支 `main` 上 2026-11-03 的 8 个 commit 仍保留旧邮箱——**不改写共享分支历史**，
  这是有意为之，避免影响其他成员的提交与已有 PR。

因此核对贡献时，请以**提交内容与 PR 记录**为准，并参考下表。

---

## 二、提交分布

| 分支 / 位置 | 数量 | 邮箱 | 状态 |
|---|---|---|---|
| `main`（更早） | 1 | noreply（可关联） | 已合并 |
| `main`（2026-11-03） | 8 | `jiangqinzhao@local`（不可关联） | 已合并，内容为自动识别、串口自占用修复、方向翻转、单文件构建 |
| `main`（2026-11-05，第一批） | 9 | noreply（可关联） | 已合并，帧率上限、跳蹲工具、AI 决策记录、雷达档案 |
| `main`（2026-11-05，第二批） | 9 | noreply（可关联） | 已合并，8 份采集/分析记录与评估结果集 |
| `radar-lr3c`（雷达侧独立历史） | 38 | noreply（可关联） | 已推送 |

**main 上合计 27 个本人提交**：19 个可被 GitHub 统计到 `@jqz070803`，8 个因旧邮箱无法关联。

`radar-lr3c` 是雷达侧的独立历史分支（与 main 无共同祖先），因为两边的目录结构不同：
本分支雷达代码在根 `radar-pointcloud/`，main 在 `radar-service/`。两者不能直接合并，
只能按主题把成果移植到 main——前两批合并就是这么做的。

---

## 三、`radar-lr3c` 上的工作主线（38 个 commit）

按时间顺序的主要节点：

1. **初始化与协议**：点云软件入库、双格式解析（兼容裸帧）修复卡死、近距门屏蔽与合成回放样本。
2. **事实基线**：2T4R 板纳入基线、跳/蹲改为 P0→T0→T1→T2 验证门、立项与工具链文档。
3. **链路打通**：串口自动识别、一键启动、页面链路诊断、真机 COM3 实测。
4. **左右移动识别**：
   - 修 hold 死条件、按实测泄漏门定 `min_range`；
   - 默认判据改 `xbase` + 滑窗 8 + 裸帧板型识别；
   - 左门 0.10→0.14 压静止误报、MTI 系数可调、在线参数开关；
   - 右门 0.18→0.12，lr3c 实测 93.8%（RIGHT 4/8→7/8）；
   - 自动距离带按 5%~95% 分位覆盖双 bin（修复检出率 47% 的根因）；
   - 中线实测 +9°、电脑扇区排除、判据诊断与重采集引导。
5. **2T4R 与跳蹲**：仰角标定链、跳蹲检测器、横向方位角测试、通道对诊断、
   原始帧采集（带真值标注）、采样上限量化。
6. **帧率上限**：130 ms 配置实测 7.6 Hz、3 Mbps 证伪、32 chirp 真相。
7. **自动连接**：插上即识别双板、2T4R 无流时自动唤醒、DTR/RTS 修复、
   启动序列路径可移植。
8. **游戏侧联调**：雷达输入源与 ws 桥、热切换、单文件合并构建。

完整清单可在本地执行：

```bat
git log radar-lr3c --oneline
```

---

## 四、2026-11-05 合并的两批内容

### 第一批（9 个 commit）：结论、工具与档案

| 主题 | 交付物 |
|---|---|
| 2T4R 帧率上限 | `docs/2t4r-framerate-limit.md`、`tools/uart_baud_probe.py` |
| 跳蹲采集 | `tools/jumpduck_capture.py`、`tools/radar_peek.py` |
| 跳蹲引导界面 | `tools/jumpduck_guide.py`、`tools/jumpduck_guide.html`、`docs/jumpduck-guide.md` |
| AI 决策记录 | `docs/ai-decision-log.md`（64 条） |
| 雷达侧档案 | `docs/radar/`（26 篇 + 索引） |

### 第二批（9 个 commit）：8 份实测记录与评估结果集

| 主题 | 交付物 |
|---|---|
| lr3 系列固定带评估 | `docs/radar/captures/lr3-series-20261022.md` + 2 份对比表 + 8 份评估 json |
| 2T4R 俯仰标定 11 轮 | `captures/vertical-20261029.md` + 11 份 result.json |
| 通道对诊断 | `captures/rxpair-20261029.md` + 2 份 result.json |
| 水平方位角 | `captures/horizontal-20261029.md` + 2 份 result.json |
| Fisher 可分性 | `captures/calibration-fisher-20261029.md` + 2 份分析 json |
| 双板干扰 4 次 | `captures/dual-interference-20261029.md` + 4 份 report json |
| 跳蹲离线评估 | `captures/jumpduck-offline-eval.md` + report.json/md |
| 左右移判据结果集 | `captures/lane-eval-summary.md` + 15 份评估 json |

两批合计 50 个新增文件、11048 行，**0 行删除**，未改动 main 上任何现有文件。

目录与文件说明见 `docs/radar/README.md` 与 `docs/radar/captures/README.md`。

---

## 五、证据边界（与项目证据纪律一致）

- 本文只陈述**已执行**的提交与实测结论；计划中的检查不在此列。
- 阈值类数字若来自单次实验，已在对应文档标注样本量与口径（宽/严）。
- 涉及厂商寄存器配置的文件（`boot_seq_2t4r.json` 等）按保密要求不入库。
- 采集原始数据（`data/`、`*.bin`、`*.log`）不入库，只在文档中记录结论与复现方法。
