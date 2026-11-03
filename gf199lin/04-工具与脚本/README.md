# 04 · 工具与脚本

本目录是**可复现的证据来源**：报告里的每个数字，都能用这里的工具重新跑出来。

> 重要：脚本是开发期在本机运行的一次性工具，路径、端口、Chrome 位置按当时环境写死。
> 为保护隐私，本目录已把绝对路径脱敏为 `<PROJECT_ROOT>`、`<TEMP>`、`<USER_HOME>`，
> 复现前请先替换成你自己的路径。它们的价值在**测试思路和断言**，不是开箱即用的产品代码。

---

## 1. 雷达数据体检台（最常用）

`radar-diagnose.html` —— 单文件、零依赖，浏览器直接打开。

用法：

1. 打开页面，导入逐帧 CSV（字段说明见 [`../03-雷达评估与校准/radar-diagnose-guide.md`](../03-雷达评估与校准/radar-diagnose-guide.md)）；
2. 依次看 G0 通路 → G1 坐标 → G2 时间 → G3 事件；
3. 任何一门 FAIL，先修数据，不要回去改游戏阈值。

四道门的作用：

| 门 | 判定 | 典型失败原因 |
|---|---|---|
| G0 | 帧率 ≥ 20 Hz、无 > 500 ms 空洞、字段齐全 | 串口配置错、固件限流 |
| G1 | 静息 σ 小、左右分离度 ≥ 4σ、位移单调 | 板子装反、俯仰角过大、量纲错 |
| G2 | 双路到达差 p95 < 50 ms | 两路上游时钟不同源 |
| G3 | 静息误报 ≤ 6%、命中 ≥ 80% | 判据阈值过低、信号本身缺失 |

---

## 2. 脚本清单

### 数据分析

| 脚本 | 作用 |
|---|---|
| `analyze.py` | 通用日志/CSV 提取与统计（早期探索用） |
| `analyze_lr.py` | 离线分析 `lr-move` 数据集：四个方向的数据是否够用 |
| `analyze_lr2.py` | 补充统计：只打印写文档要引用的关键数字，避免输出过长 |

### 假数据与回放（不依赖硬件）

| 脚本 | 作用 |
|---|---|
| `fake_radar_ws.py` | 起一个假雷达 WebSocket（默认 8766），把脚本数据喂进真游戏，用来演示校准向导 |
| `gen_lr_replay.py` | 把 `lr-move` 数据集转成游戏能直接吃的 `window.RR_RADAR_REPLAY`，含 truth 通道对照 |
| `capture_replay.py` | 用游戏离线回放通道 + 无头 Chrome 逐帧抓图，产出可合成视频的图片序列 |
| `e2e_test.py` | 端到端：假雷达 → 两个 bridge（后台判定）→ hub → 探针，验证左右/上下不串道 |

### 雷达链路自检

| 脚本 | 作用 |
|---|---|
| `verify_connect.py` | 验证 `radar-connect.html` 能否真的连上雷达、读数、自检、带地址进游戏 |
| `bridge_selftest.py` | `radar_bridge.py` 端到端自测：UDP / TCP / WS 三种上游 → WS 输出 |
| `wire_calibration.py` | 把 `Calibration.js` 接进游戏（script 标签、菜单入口、首启引导、样式、配置），锚点唯一命中才写 |
| `fix_calib_polarity.py` | 修复校准向导"按 x 符号判左右"的极性缺陷 |

### 端到端 UI / 玩法回归（CDP）

| 脚本 | 断言重点 |
|---|---|
| `cdp_calib.py` | 校准向导是否真的接进 `index.html`：菜单入口、自动引导、帧改写（27 条断言） |
| `cdp_combo.py` | 连击倍率换算、逐颗结算、险躲判定 |
| `cdp_difficulty.py` | 速度曲线连续单调封顶 25、反应时间随里程收敛、分区概率 |
| `cdp_particles.py` | 粒子触发计数、活跃数回收、draw call 增量恰为 2 |
| `cdp_records.py` | 结算面板节点、纪录容错（坏 JSON / 负数 / 脏历史） |
| `cdp_shot.py` | 真跑一局到第 2 区，在横幅可见瞬间截图并打印状态 |
| `smoke-index.html` | 页面冒烟：资源加载、无控制台报错 |

---

## 3. 运行环境

- Python 3.9+（部分脚本需要 `numpy`）
- Chrome / Edge（CDP 脚本需要可执行文件路径）
- 无需安装 Node（游戏本体零依赖，`vendor/three.min.js` 已内置）

典型跑法：

```bash
python fake_radar_ws.py 8766
python cdp_calib.py "file:///<项目路径>/radar-runner/index.html" 9373
python analyze_lr.py
```

---

## 4. 这些脚本证明过什么

| 结论 | 证据脚本 |
|---|---|
| 校准向导接入后 27 条断言全过 | `cdp_calib.py` |
| 分享包解压后仍能跑通校准与回归 | `cdp_calib.py` + `cdp_particles.py`（对 zip 解压目录复验） |
| 左右/上下两路不会互相串道 | `e2e_test.py` |
| 离线回放能复现真实数据的判定过程 | `gen_lr_replay.py` + `capture_replay.py` |

失败与未完成的部分同样记录在 [`../03-雷达评估与校准/`](../03-雷达评估与校准/)，没有只保留成功截图。
