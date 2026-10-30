# 毫米波雷达体感跑酷 · 雷达侧（点云程序 / 动作判定 / 结果传给游戏）

文件收录我负责的三项工作，对应「熊二快跑」体感跑酷项目的雷达侧：

1. **点云调试程序** —— 单文件网页，直接连雷达、解析帧、出点云/瀑布图/热图；
2. **点云程序判定动作** —— 在点云程序内实现跳/蹲判定器 `JCDet`（另有一版 Node 实现的 `radar-detect.js`）；
3. **点云程序把结果传给游戏** —— 事件输出（WebSocket）+ 融合器，把判定结果变成游戏能吃的 20 Hz 帧。

> 双雷达双开的后端判定与汇聚（`radar_bridge.py` / `radar_hub.py`）、游戏本体（`radar-runner`）由组员完成，不在本仓库。

---

## 一、链路

```
                    ┌ 点云程序（本仓库 01）──────────────────────────────┐
DEV113P/DEV202H ─串口─> bridge.js ─ws─> 解析 → 点云/瀑布图 → JCDet 判定 ─┤
 (COM8/COM7)      (本仓库 04)                                          │ 事件 ws://127.0.0.1:8899/{jc,lr}
                                                                       └───────────────┬──────────────┘
                                                                                       v
                                                        radar_fusion.js（本仓库 04）合成 20 Hz 帧
                                                                                       v
                                                                             UDP 127.0.0.1:8888
                                                                                       v
                                                                        游戏侧 radar_bridge.py（组员）
                                                                                       v
                                                                    游戏页 ws://127.0.0.1:8766（组员）
```

---

## 二、目录

| 目录 | 内容 | 对应工作 |
| --- | --- | --- |
| `01-点云程序/` | `radar-pointcloud.html`（137 KB 单文件，v0.8.1）、`dev113p_boot.js`（从厂商日志提取的启动宏）、`dev113p-prompter.html`（采集剧本语音提词器） | 工作 1 |
| `02-点云程序-开发过程(补丁)/` | `patch_v050` → `patch_v070` 共 14 个补丁脚本 | 工作 1（迭代过程证据） |
| `03-动作判定/` | `radar-detect.js`（Node 版跳蹲判定器）、`extract_jc.js`、`test_jcdet.js`、`analyze_v2.js`、`diag_jc*.js`、`判定说明.md` | 工作 2 |
| `04-结果传给游戏/` | `game-link/`（`radar_fusion.js`、`_test_client.js`、`start_fusion.bat`）、`radar-bridge/`（`bridge.js` 串口转 WebSocket）、`radar-request-v3.txt`（写给组员的接口需求）、`对接说明.md` | 工作 3 |
| `05-证据与日志/` | 13 张截图（瀑布图/热图/判定面板）+ 4 份真实运行日志/判定输出 | 全部 |

---

## 三、快速开始

### 环境
- Chrome / Edge（Web Serial）
- Node.js >= 18；Python 3.10+（`pip install pyserial websockets`）

### 只看点云（无硬件）
直接双击 `01-点云程序/radar-pointcloud.html`；带 `?demo=1` 可走内置演示数据。

### 接真板（DEV202H 为例）
```bat
:: 串口 → WebSocket：浏览器串口缓冲只有 8KB，1.5M 波特率必须中转
node 04-结果传给游戏\radar-bridge\bridge.js --com=COM7 --baud=921600 --ws=8768
```
浏览器打开（预设键是 `icl1112`，不是 `icl1122`）：
```
01-点云程序\radar-pointcloud.html?board=icl1112&bridge=ws://127.0.0.1:8768&out=ws://127.0.0.1:8899/lr
```
再启融合器（事件 → 20 Hz 帧 → UDP 8888）：
```bat
node 04-结果传给游戏\game-link\radar_fusion.js
:: 不接雷达想先验证链路：node radar_fusion.js --sim （每 12 秒自动跳/蹲/左/右）
```

### 无硬件验证「判定 → 游戏」这一段
```bat
node 04-结果传给游戏\game-link\_test_client.js
```
它模拟两个点云网页分别发 `/jc`、`/lr` 事件；融合器应打印 `跳跃 → jump=1 / 300ms`、`下蹲 → crouch=1`，
并每 10 秒统计一次帧数与 x 值。

---

## 四、判定原理（摘要，详见 `03-动作判定/判定说明.md`）

- **找人门**：静止的人被运动残差当背景滤掉，所以用 `bin0`（零速）能量剖面在 0.70~1.58 m 内取峰值定人门；
- **特征**：垂直天线对（虚拟通道 5−2、2−3）相位差 = 俯仰角 proxy，相对「安静帧基线」的偏移 `dev`（度）；
- **判别**：`|dev[1]| ≥ 18°` 且次帧回落 → 跳；连续 2 帧 `≥ 12°` 且回落到 `< 7°` → 蹲；
- **免标方向**：用「持续时长」而不是符号判别，板子倾斜/安装高度翻转符号也不影响；
- **基线慢漂**：静止帧上 `α=0.03/帧`（≈17 s 时间常数），吸收站位微移。

阈值全在 `radar-pointcloud.html` 的 `/*JCDET-BEGIN*/ … /*JCDET-END*/`（第 1425~1576 行），
该段不碰 DOM/Bus，网页与 Node 回放测的是同一份代码。

---

## 五、已知问题（如实记录）

- ⚠️ **跳/蹲尚未定稿**：旧 DEV113P 硬件损坏、旧数据作废；新板 `dev` 近似白噪声（离线误报 85%~91%），
  因此 `observeOnly = true`，只闪面板、暂不把跳蹲事件发给游戏（代码第 1568 行有注释）。左右移动那条已实测可用。
- ⚠️ `radar_fusion.js` 的 `require('ws')` 走原目录 `..\radar-bridge\_work\node_modules`，挪位置后需 `npm i ws`（见 `04-结果传给游戏/对接说明.md`）。
- ⚠️ https 页面（GitHub Pages）连不上 `ws://`，联调必须用本地 http 页面。

---

## 六、个人工作与分工（评分用）

> 姓名 / 学号：`薛雅心 3026445055`　时间：2026-10

| # | 工作 | 产出 | 可核查点 |
| --- | --- | --- | --- |
| 1 | 点云程序 | `radar-pointcloud.html` 及补丁链 | 双芯片解析（ICL1240/ICL1122）、启动宏、点云/瀑布图/RD 图、参数面板 |
| 2 | 动作判定 | `JCDet`（网页内）+ `radar-detect.js`（Node） | 判据设计、安静帧校准、阈值 v1→v2、离线误报率统计 |
| 3 | 传给游戏 | 事件输出 WS + `radar_fusion.js` | 20 Hz 补帧、跳蹲转电平、掉线不回零、置信度门限 |

**不属于我的部分**：游戏本体与 UI（组员）、`radar_bridge.py` / `radar_hub.py` 双雷达后端判定与汇聚（组员）、
双雷达双开方案的提出（组员；我按手册做了本机验证与调参）。