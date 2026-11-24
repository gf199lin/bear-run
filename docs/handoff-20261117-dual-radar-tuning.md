# 交接：双雷达接入跑酷游戏 + 左右/跳蹲调参（2026-11-17）

> 下次直接读这一份就能接着调。所有数字都是**离线回放实测**，除特别标注外。
> 参数改动尚未经过人站现场实测确认的地方，已在文末「已知问题」里列出。

---

## 0. 一句话状态

两块 1T2R 雷达已接入 `bear-run-new/` 跑酷游戏，左右换道按幅度分档（13 个用例全过），
跳/蹲拆成「蹲多深 + 跳多高」两个幅度门（离线跳 46/46、中蹲 22/22、全蹲 12/14、浅蹲误报 1/10）。
**待办：现场人站实测手感**。

---

## 1. 硬件与摆法（当天）

| 项 | 状态 |
|---|---|
| 水平板（左右） | 1T2R 横放，人站约 1 m，`--role lr` |
| 竖直板（跳蹲） | 1T2R 竖放，离地约 0.5 m，人站约 1.2 m，`--role ud` |
| USB-UART | CP2102，装 CP210x 驱动（**不是 FTDI**） |
| 帧周期 | 129.5 ms ≈ 7.7 Hz —— 全项目体验上限 |
| 左右中线 | `radar-pointcloud/center.json` = +9° |
| 左右比例 | `--scale-deg 20.0`（x=1.0 对应 20°） |

---

## 2. 服务端口与启停

| 用途 | 地址 |
|---|---|
| 分配台（管理页面） | `http://127.0.0.1:8778` → 302 到 `/radar-connect-dual.html` |
| 左右雷达 ws | `ws://127.0.0.1:8766` |
| 跳蹲雷达 ws | `ws://127.0.0.1:8767` |
| 游戏 | `http://127.0.0.1:8778/index.html` |

启动：双击根目录 `启动-双雷达游戏.cmd`。

管理 API（脚本用）：

```powershell
# 重启两路
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8778/api/stop -Body '{}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8778/api/start -Body '{}'
# 看状态（含心跳帧数）
Invoke-RestMethod http://127.0.0.1:8778/api/state
# 左右镜像翻转
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8778/api/flip-lr -Body '{}'
```

**改了游戏侧 JS 之后必须刷新浏览器（Ctrl+F5）**，否则加载的还是旧逻辑。

---

## 3. 当前全部参数（下次从这套开始）

### 3.1 左右换道（`bear-run-new/js/config.js` 的 `RADAR` 段）

| 参数 | 当前值 | 含义 |
|---|---|---|
| `enterThreshold` | **0.22** | 中间→边上要偏这么多（0.22 × 20° ≈ 4.4°，1 m 处约 8 cm）|
| `exitThreshold` | **0.12** | 一边→回中间的门（≈2.4°），比进道门低，形成滞回 |
| `bigStepThreshold` | **0.62** | 一边→对面 / 大幅跨步（≈12.4°）|
| `laneCooldownMs` | **120** | 一帧 129.5 ms，120 等于每帧都允许切换 |
| `maxLaneStepPerFrame` | 1 | 单帧最多走一格（跨两根要靠两帧）|
| `scaleDeg` | 20.0 | 归一化比例 |

**判定逻辑**（`js/InputSource.js` 的 `RadarLaneDecoder.update`）：

```js
if (this.lane === 1) {
  if (x >= bigEnter) lane = 2;              // 大幅跨步优先（单帧限档会先压到中间）
  else if (x <= -bigEnter) lane = 0;
  // 小幅：必须"从中间穿过去"或"还在继续同向移动"
  else if (x <= -R.enterThreshold && (prevX > -R.enterThreshold || x < prevX - 0.05)) lane = 0;
  else if (x >= R.enterThreshold && (prevX < R.enterThreshold || x > prevX + 0.05)) lane = 2;
} else if (this.lane === 0) {
  if (x >= bigEnter) lane = 2;              // 大幅：跨两根
  else if (x >= -R.exitThreshold) lane = 1; // 收回：回中间
} else {
  if (x <= -bigEnter) lane = 0;
  else if (x <= R.exitThreshold) lane = 1;
}
```

**「穿门」规则是这一轮的核心修复**，没有它会出问题：
人从左边拉到中间位置（x=0.3），第一帧回中间，第二帧同一个 0.3 又满足「从中间进右道」，
于是继续跨过去 —— **小移动变成跨两根**。

### 3.2 跳/蹲（`tools/jumpduck_fusion.py`）

| 参数 | 当前值 | 含义 |
|---|---|---|
| `V_T` | 0.13 | 进负段门（先看见动作）|
| `NZ_T` | **0.20** | 单帧动作门（能量占比不到就整帧丢弃），原 0.28 |
| `V_JUMP` | **0.32** | 判跳：正速度峰值（**原始值，不经 EMA**）|
| `V_DUCK` | **0.35** | 判蹲：负段最深（EMA 后）|
| `NB_DUCK` | 3 | 负段连续几帧才考虑判蹲 |
| `DEFER_DUCK` | 2 | 凑够帧后压几帧看是否转正（转正=跳）|
| `ALPHA` | 0.5 | 速度 EMA |
| `QUIET` | 6 | 事件后静默帧数 |
| `COOLDOWN` | 8 | 事件冷却帧数 |
| `USE_WIDTH` | False | 谱宽**未**接入实时判据（理由见 4.3）|

**为什么跳用原始峰值**：`EMA(alpha=0.5)` 会把尖峰削掉一半，轻跳直接漏掉。

### 3.3 雷达进程参数（`tools/dual_radar_manager.py` 的 `LR_ARGS` / `UD_ARGS`）

| 参数 | 值 | 说明 |
|---|---|---|
| `--jump-engine` | `fusion` | `legacy` / `sm` / `fusion` 三选一 |
| `--defer-duck` | 2 | |
| `--jump-band` | `1,3` | **写死**，见已知问题 |
| `--auto-band` | lr 侧启用 | 5%~95% 分位定带，覆盖两个距离 bin |
| `--idle-exit` | 60 | 无人连接 60 s 自退 |
| `--lonely-exit` | 300 | 无客户端 300 s 自退 |
| `--quiet` | 开 | 逐帧日志关闭（**排障时务必加 `--no-quiet`**）|

---

## 4. 每一轮调参的实测数据（为什么定成上面这样）

### 4.1 左右：三次调整，前两次都踩坑

| 轮次 | 改动 | 结果 |
|---|---|---|
| 原始 | `enter 0.36`，无分档，冷却 120 | 站边上越过 0.36 直接跳对面 → 手一动跨两根 |
| 第 1 次 | 中间→边上也套 `bigStep 0.45`，冷却 300 | 现场**不灵敏**：挥到 45% 才换道 |
| 第 2 次 | 改回 `enter 0.36`，冷却 120 | 灵敏了，但小移动（<12 cm）不触发 |
| **第 3 次（当前）** | `enter 0.22` + 穿门规则 + 继续移动放行 | 13/13 用例通过 |

headless 实测（真实 `RR.RadarDecoder` + 合成 x 序列）：

| 起点 | x 序列 | 事件 | 落点 |
|---|---|---|---|
| 中间 | 0.4 ×4 | right | 2 |
| 中间 | 0.30 ×3 | right | 2 |
| 中间 | 0.24 ×3 | right | 2（刚过新门限）|
| 中间 | 0.20 ×3 | 无 | 1（防抖）|
| 中间静止→ | 0.3 ×3 | right | 2 |
| **左边** | **0.3 ×3** | right | **1（回中间就停，不跨两根）** |
| 左边 | 0.3 ×2 → 0.5 ×2 | right,right | 2 |
| 左边 | 0.7 ×5 | right,right | 2 |
| 右边 | 0.05 ×3 | left | 1 |

验证方式：`.deepworks/tmp/_jd/__lane_probe.html` + `check_lane.py`
（探针要复制到 `bear-run-new/` 下跑，iframe 才能同目录加载 `./index.html`）。

### 4.2 跳/蹲：从「只看方向」改成「看幅度」

现象：起跳前小蓄力被判成蹲；真跳要很大幅度才触发。

**根因**：原状态机只有速度方向 + 帧数，`v_t=0.13` 一个门限同时管跳和蹲。

改成三个门后（`data/jumpduck_battery`，10 浅蹲 / 22 中蹲 / 14 全蹲 / 46 跳）：

| 指标 | 改前 | 改后 |
|---|---|---|
| 小蹲误报成蹲 | 6/10 | **1/10** |
| 中蹲检出 | 21/22 | **22/22** |
| 全蹲检出 | 12/14 | 12/14 |
| 跳检出 | 45/46 | **46/46** |
| 跳延迟（中位） | 561 ms | **388 ms** |
| 静息误报 | 2/4 | **0/4** |

扫参记录：

- `V_JUMP`：0.35→43/46；**0.32→46/46**；0.30→46/46 但全蹲掉 11/14；0.28→浅蹲误报跳升到 6/10
- `NB_DUCK`：2 → 蹲延迟 517 ms 但跳掉到 44/46，**保持 3**
- `DEFER_DUCK`：0 → 跳掉到 34/46（12 个跳被误判成蹲），**必须保留 2**
- `NZ_T`：0.28 / 0.22 / 0.18 离线**指标完全一样** → 说明这批样本动作都够强，改它只影响现场弱动作

### 4.3 谱宽（`doppler_width`）为什么没进实时判据

| 类别 | 整段峰值−基线 | **事件后 1~2 帧** |
|---|---|---|
| 跳（小/中/大/缺上冲） | 0.305 / 0.661 / 0.903 / 0.472 | 0.234 / **0.249** / 0.289 / 0.261 |
| 蹲（浅/中/全） | 0.171 / 0.283 / 0.281 | 0.107 / **0.252** / 0.265 |

整段可分（跳 ≥0.30、蹲 ≤0.28），但**动作后头两帧完全重叠**（跳 0.249 vs 蹲 0.252），
而谱宽峰值要到动作后 7~15 帧（0.9~1.9 s）才出现 —— 那时角色已经蹲下或跳完了。
**结论：只做离线诊断，不做实时改判。**

### 4.4 可观测性：「显示连接成功但没有数据」

真因：ws 端口在听 + 串口枚举到设备 ≠ 有帧。当天是启动瞬间串口被上一轮残留进程占用，
加上 `--quiet` 关掉了逐帧日志，所以完全没有痕迹。

已加：

- `radar_live_ws.py` 每 2 s 写 `<pidfile>.hb`（`{pid, frames, t}`，`os.replace` 原子写）
- 8/25/60 s 无帧时写告警日志（**不受 `--quiet` 影响**）
- 分配台 / 联调台显示 `运行中 · N 帧` / `运行中 · 无数据` / `运行中 · 无心跳`

排障时先看 `/api/state` 里 `bridges.<role>.hb.frames` 有没有在涨。

---

## 5. 已知问题 / 下次先做什么

| # | 问题 | 建议动作 |
|---|---|---|
| 1 | **所有跳蹲改动未经人站现场实测** | 先试跳（`NZ_T` 0.20 是否足够灵）、再试蹲（`V_DUCK` 0.35 是否太严）|
| 2 | `--jump-band 1,3` 写死 | 若现场站距不在 1.0~3.5 m 的 bin 内，速度特征会失效；改 `UD_ARGS` 的 `--jump-band`（**`radar_live_ws.py` 没有 `--auto-band`**，原表述有误，见 `docs/贡献-20261124.md` §2）|
| 3 | `enterThreshold 0.22` 可能对站立抖动敏感 | 若静息误换道，回 0.26；或把 `NZ_T` 回 0.24（跳蹲侧）|
| 4 | 小幅移动 8 cm 才触发 | 若还嫌钝，降到 0.18（≈6.5 cm）|
| 5 | 无 node 环境 | 游戏侧 JS 只能靠 Edge headless 间接验证 |
| 6 | 穿门规则的 `0.05` 增量是写死的 | 若现场"继续移动"被判太慢/太快，调这个值 |

---

## 6. 相关文件

**游戏侧**

- `bear-run-new/js/config.js` —— 左右阈值
- `bear-run-new/js/InputSource.js` —— `RadarLaneDecoder` 判定逻辑（**改这里必须同步 `index.html`**）
- `bear-run-new/index.html` —— 打包产物（1.65 MB，四处需同步：config 参数、判定逻辑、兜底值 0.62）
- `bear-run-new/radar-connect-dual.html` —— 联调台托管副本

**雷达侧**

- `tools/jumpduck_sm.py` —— 速度状态机（`NEG` 分支追踪 `vneg` / `vpos`）
- `tools/jumpduck_fusion.py` —— 融合层 + 全部默认参数 + `eval_dir(factory=)` 支持扫参
- `dual-radar-kit/tools/radar_live_ws.py` —— ws 桥接、心跳、无数据告警
- `tools/dual_radar_manager.py` —— 8778 分配台、启停、端口复用
- `dual-radar-kit/radar-connect.html` —— 联调台源文件（与 `bear-run-new/` 下副本同步）

**文档**

- `docs/dual-radar-game-integration.md` —— 第 7 节：左右分档 13 用例表、跳蹲幅度门、谱宽否决理由

**离线评估**

```powershell
# 跳蹲：跑全部电池目录
python tools\jumpduck_fusion.py data\jumpduck_battery\2026*
# 扫参（在 .deepworks/tmp/_jd/sweep8.py 里有现成模板）
```

**临时脚本**（不提交，`\.deepworks\tmp\_jd\`）

- `__lane_probe.html` / `check_lane.py` —— 左右 13 用例探针
- `sweep4~9.py` —— 跳蹲扫参
- `width_test.py` / `width_early.py` —— 谱宽可分性

---

## 7. 当天提交记录

见 `git log`。主题划分：雷达进程可观测性 → 跳蹲幅度门 → 分配台与联调台 → 游戏侧左右分档 → 文档。
