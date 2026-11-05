# 毫米波雷达体感跑酷 · 雷达侧（点云程序 / 动作判定 / 结果传给游戏）

> 本目录是**薛雅心（[@chawaicha](https://github.com/chawaicha)，学号 3026445055）**的「熊二快跑」体感跑酷个人交付区。
> 每一项工作都能在本目录里找到**可运行的代码**和**可核对的证据**，证据与验收项的对应关系见
> [`05-证据与日志/证据索引.md`](05-证据与日志/证据索引.md)。

---

## 一、三项工作与可核查产出

| # | 工作 | 产出（本目录） | 怎么验证 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | **点云调试程序** | `01-点云程序/radar-pointcloud.html`（单文件网页）、`dev113p_boot.js`（启动宏）、`dev113p-prompter.html`（采集剧本提词器） | 双击网页即可跑；带 `?demo=1` 用内置数据，无需硬件 | `截图/dev202h-waterfall.png`（新板实时流）、`截图/grade113p-*.png`（RD 图读数） |
| 2 | **点云程序判定动作** | `radar-pointcloud.html` 内 `JCDet` 段 + `03-动作判定/radar-detect.js`（Node 版同源实现） | `node 03-动作判定/test_jcdet.js` 离线回放；或看网页面板闪判定 | `截图/heat-{still,mid,moving}.png`（判据来源）、`截图/grade113p-waterfall.png`（MTI 前后对比）、`截图/grade_az_waterfall.png`、`截图/grade_el_waterfall.png` |
| 3 | **点云程序把结果传给游戏** | `04-结果传给游戏/radar_fusion.js`（事件 → 20 Hz 帧 → UDP）、`_test_client.js`（无硬件自测） | `npm i` 后 `node radar_fusion.js --sim`，再开一个窗口跑 `node _test_client.js` | `运行日志/fusion.log`、`运行日志/gamebridge.log` |

配套工具（工作 1 的支撑）：`02-串口桥接/bridge.js` 解决"浏览器串口缓冲只有 8 KB、高波特率必丢字节"的问题，
把串口转成 WebSocket 后再喂给网页；`motion-bridge.js` 是更早期的"点云 → 运动能量"直出版本。

> 游戏本体与 UI（@gf199lin / @hwx-666）、双雷达后端判定与汇聚 `radar_bridge.py` / `radar_hub.py`（@jqz070803）**不在本目录**。

---

## 二、链路

```
                    ┌ 点云程序（01）─────────────────────────────────────┐
DEV113P/DEV202H ─串口─> bridge.js ─ws─> 解析 → 点云/瀑布图 → JCDet 判定 ─┤
 (COM7/COM8)      （02）                                                │ 事件 ws://127.0.0.1:8899/{jc,lr}
                                                                       └───────────────┬──────────────┘
                                                                                       v
                                                    radar_fusion.js（04）合成 20 Hz 帧
                                                                                       v
                                                                             UDP 127.0.0.1:8888
                                                                                       v
                                                                        游戏侧 tools/radar_bridge.py（组员）
                                                                                       v
                                                                    游戏页 ws://127.0.0.1:8766（组员）
```

---

## 三、目录结构

```
chawaicha/
├── 01-点云程序/          radar-pointcloud.html、dev113p_boot.js、dev113p-prompter.html
├── 02-串口桥接/          bridge.js、motion-bridge.js、启动与采集批处理、tools/
├── 03-动作判定/          radar-detect.js、extract_jc.js、test_jcdet.js、analyze_v2.js、diag_*.js、判定说明.md
├── 04-结果传给游戏/      radar_fusion.js、_test_client.js、start_fusion.bat、package.json
└── 05-证据与日志/        证据索引.md、截图/（11 张）、grade_game2_verdict.txt、运行日志/（3 份）
```

| 目录 | 说明 |
| --- | --- |
| `01-点云程序/` | 主程序约 137 KB 单文件，双芯片（ICL1240 / ICL1122）解析、启动宏、点云 / 瀑布图 / RD 图 / 参数面板 |
| `02-串口桥接/` | `bridge.js` 大缓冲读串口转 WebSocket；`motion-bridge.js` 原始点云转运动能量；`tools/` 放 PDF 提取与页面渲染两个小脚本 |
| `03-动作判定/` | Node 版判定器与全部诊断 / 分析脚本；`判定说明.md` 是判据推导过程 |
| `04-结果传给游戏/` | 融合器把两路事件合成 20 Hz 帧；`_test_client.js` 可无硬件验证整链 |
| `05-证据与日志/` | 截图、判决全文、真实运行日志；**先看 `证据索引.md`** |

`node_modules`、`*.log` 以外的临时产物按仓库根 `.gitignore` 规则不入库。

---

## 四、复现步骤

### 环境
- Chrome / Edge（Web Serial）
- Node.js >= 18
- Python 3.10+（仅游戏侧 `radar_bridge.py` 需要：`pip install pyserial websockets`）

### 1. 只看点云（无硬件，30 秒）
双击 `01-点云程序/radar-pointcloud.html`；地址后加 `?demo=1` 走内置演示数据。

### 2. 接真板（以 DEV202H 走左右为例）
```bat
cd 02-串口桥接 && npm i
node bridge.js --com=COM7 --baud=921600 --ws=8768
```
浏览器打开（预设键是 `icl1112`，不是 `icl1122`）：
```
01-点云程序\radar-pointcloud.html?board=icl1112&bridge=ws://127.0.0.1:8768&out=ws://127.0.0.1:8899/lr
```
再启融合器（事件 → 20 Hz 帧 → UDP 8888）：
```bat
cd 04-结果传给游戏 && npm i
node radar_fusion.js
```

### 3. 无硬件验证「判定 → 游戏」全链
```bat
node 04-结果传给游戏\_test_client.js
```
模拟两个点云网页分别发 `/jc`、`/lr` 事件；融合器应打印 `跳跃 → jump=1 / 300ms`、`下蹲 → crouch=1`，
并每 10 秒统计一次帧数与 x 值。`radar_fusion.js --sim` 则完全不接上游，每 12 秒自动跳 / 蹲 / 左 / 右。

---

## 五、判定原理（摘要，详见 `03-动作判定/判定说明.md`）

- **找人门**：静止的人会被运动残差当背景滤掉（见 `截图/grade113p-waterfall.png` 右侧 MTI），
  所以改用 `bin0`（零速）能量剖面在 0.70~1.58 m 内取峰值定"人门"；
- **特征**：垂直天线对（虚拟通道 5−2、2−3）相位差 = 俯仰角 proxy，取相对"安静帧基线"的偏移 `dev`（度）；
- **判别**：`|dev[1]| ≥ 18°` 且次帧回落 → 跳；连续 2 帧 `≥ 12°` 且回落到 `< 7°` → 蹲；
- **免标方向**：用"持续时长"而不是符号判别，板子倾斜 / 安装高度翻转符号也不影响；
- **基线慢漂**：静止帧上 `α=0.03/帧`（≈17 s 时间常数），吸收站位微移。

阈值全在 `radar-pointcloud.html` 的 `/*JCDET-BEGIN*/ … /*JCDET-END*/` 段内，
该段不碰 DOM / Bus，网页与 Node 回放测的是同一份代码。

---

## 六、验收结论与已知边界（如实记录）

一次完整标定复测（72.4 s / 139 完整帧 / 21 段剧本）的分段判决原文见
`05-证据与日志/grade_game2_verdict.txt`，可视化母图见 `截图/grade_game2_waterfall.png`。
参数取值、判据设计依据、失败项归因，以及给游戏侧的三个待定参数答复，整理在
[`docs/calibration-record.md`](../docs/calibration-record.md)。

| 结论 | 项目 |
| --- | --- |
| ✅ 通过 | 距离（1.23 m / 2.99 m 对期望 1.5 m / 3 m）、慢速靠近与远离（∓0.6 m/s 量级）、**跳**、**蹲**、**跳+蹲连招**、挥手识别 |
| ⚠️ 偏差 | 阶梯线性（门间距 0.0 / 3.0 / 7.0，期望 ≈2.84 门 / 0.5 m）；慢速段距离读数偏低（1.23 m 对期望 2 m） |
| ❌ 未通过 | 快速靠近 / 快速远离的速度估计无输出；左右对称性同号（左 22° / 右 78°，期望 ∓67°/±67°） |

**边界（必须一起看）**：
- **跳 / 蹲尚未定稿**：旧 DEV113P 硬件损坏、旧数据作废；新板 `dev` 近似白噪声（离线误报 85%~91%），
  因此代码里 `observeOnly = true`，只闪面板、暂不把跳蹲事件发给游戏。**左右移动那条已实测可用。**
- 上表 ✅ 的部分是**离线回放的判据命中**，不等于端到端可玩；端到端只有"左右"这一条真正跑通。
- https 页面（GitHub Pages）连不上 `ws://`，联调必须用本地 http 页面。
- `radar_fusion.js` / `_test_client.js` 查找 `ws` 依赖的顺序为：本目录 `node_modules` →
  `02-串口桥接/_work/node_modules` → `../radar-bridge/_work/node_modules`，三处都没有则回落到常规 `require`。

---

## 七、个人工作与分工（评分用）

> 姓名 / 学号：`薛雅心 3026445055`　时间：2026-10

| # | 工作 | 产出 | 可核查点 |
| --- | --- | --- | --- |
| 1 | 点云程序 | `radar-pointcloud.html` + 桥接 / 启动宏 | 双芯片解析（ICL1240 / ICL1122）、启动宏、点云 / 瀑布图 / RD 图、参数面板 |
| 2 | 动作判定 | `JCDet`（网页内）+ `radar-detect.js`（Node） | 判据设计、安静帧校准、阈值 v1→v2、离线误报率统计 |
| 3 | 传给游戏 | 事件输出 WS + `radar_fusion.js` | 20 Hz 补帧、跳蹲转电平、掉线不回零、置信度门限 |

**不属于我的部分**：游戏本体与 UI（组员）、`radar_bridge.py` / `radar_hub.py` 双雷达后端判定与汇聚（组员）、
双雷达双开方案的提出（组员；我按手册做了本机验证与调参）。
