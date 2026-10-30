# 雷达接入版（radar-agent）—— 给组员

这份包做的事：**雷达串口只由 `radar-agent` 打开，游戏只连它的本地 WebSocket。**
不再让浏览器直连串口，所以不会再有「连上一会儿又不行」。

解析和判定直接用的是你 `radar-pointcloud.html` 里的那份内核
（`agent/radar-core.js` 是从该页面自动抽取的，没有改逻辑）：
解包 / 拼帧 / 点云 / 跟踪 / 手势，和联调页面一致。

---

## 一、先跑起来（2 个窗口）

### 1) 启动雷达代理

双击 `radar-agent\start_radar_agent.bat`

正常会看到：

```
✔ 游戏请连：ws://127.0.0.1:8766
✔ 串口已打开，开始握手
✔ 配置完成，等待雷达出帧
✔ 检测到目标，自动标定 1 秒（请站在中间别动）…
✔ 自动标定完成：中心 x=0.00m，身高代理 r=0.88m
状态 串口通 · 收 12.3MB · 芯片包 1024 · 帧 64 · 游戏 1 个
```

窗口保持开着。自带 Node，不用装环境。

多块板子/多串口时指定串口：

```bat
start_radar_agent.bat --serial COM8
start_radar_agent.bat --list          :: 先看机器上有哪些串口
```

### 2) 打开游戏页面

双击 `start_game_page.bat`，浏览器会打开
`http://127.0.0.1:8801/radar-connect.html`

- 地址填 `ws://127.0.0.1:8766`
- 点「连接」
- 按页面提示校准：站中间 → 左 → 右 → 跳 → 蹲

---

## 二、没有雷达时先自测

```bat
:: 演示动作（跳/蹲/左/右），验证「代理 → 游戏」这条链
start_radar_agent.bat --sim

:: 用录制的原始字节回放，验证解包与判定
start_radar_agent.bat --replay D:\record\xxx.bin --replay-speed 2
```

自测客户端（另开窗口）：

```bat
radar-agent\runtime\node.exe radar-agent\tools\test-client.js ws://127.0.0.1:8766 6
```

正常输出：`约 20.0 Hz`、`✔ 字段齐全`、`x 范围 -0.80 ~ 0.80`、`✔ ts 单调`。

---

## 三、和你原来的东西是什么关系

原来的链路：

```
radar-detect.js --UDP 8888--> tools/radar_bridge.py --ws 8766--> 游戏
```

现在合成一个进程，并且补了两块 `radar-detect.js` 里缺的：

- **横向 x**：旧版里 `x` 恒为 0，游戏换不了道；现在走点云 + 跟踪真实算出来。
- **自动标定**：`GestureEngine` 不标定就不出 x（页面里是手动按钮），
  代理检测到人站定后自动标定 1 秒。

旧的 `radar_bridge.py` 仍然能收代理发的 UDP 8888，**但不要和代理同时占 8766**。

---

## 四、已知限制（重点）

1. **跳/蹲已接入（默认开启），阈值是 v1 实验值。**
   DEV113P 上用你们的 JCDet 接管跳/蹲（旧几何判据在正挂竖板上不成立，已禁用）。
   你们 qc_report 的离线评测是 39 次跳检出 32、39 次蹲检出 32（82%）。
   代理默认接进游戏；**误报多就加 `--no-jc`** 回到观察模式。
   游戏侧 `CFG.RADAR.forceJC=true` 时，开局校准不会再因为"静站误报"把跳/蹲阈值写成 2
   （2 会让 0~1 的 jump/crouch 字段永远不可达）；旧存档里的 2 也会在装载时自动修复。
   帧率只有 1.92fps，跳只占 1~3 帧，游戏里会有 0.5~1.5s 滞后，需要提前起跳。
   站定在 0.7~1.6m 等约 3 秒，看到 `✔ 跳/蹲检测器就绪` 才开始判定。

2. **板型和波特率要对应。**
   默认 `--chip icl1240`（DEV113P，1500000 baud）。
   DEV202H 用 `--chip icl1112`（921600 baud）。

3. **本包只在 Windows x64 上验证过。**
   `runtime\node.exe` 和 `node_modules`（serialport 预编译）都是 win32-x64。
   换平台就删掉 `node_modules`，用 `npm install serialport ws` 重装。

4. **真机数据还没跑过。**
   本包发出去前验证的是：串口枚举、内核加载、`--sim` 全链路、
   以及合成 ICL1240 裸流的「解包 → 拼帧 → 点云 → 跟踪 → 左右」，
   结果 15360 包 / 0 坏包 / 59 帧 / x 在 ±0.59 之间。
   真雷达的握手与配置下发沿用 `radar-detect.js` 的逻辑（你们实测 263 帧 0 丢块）。

---

## 五、出问题怎么反馈

启动时加 `--log`，把日志存成文件发回来：

```bat
start_radar_agent.bat --log D:\radar-log.txt
```

日志里有：串口名、握手结果、收到多少字节、解出多少芯片包、拼了多少帧、
自动标定的中心值。基本能直接定位是「串口没通 / 握手没过 / 解包为 0 / 判定为 0」。

常见对照：

| 现象 | 处理 |
|---|---|
| `端口 8766 已被占用` | 旧的 bridge / 另一个 agent 还开着，三者只能留一个 |
| `串口被别的程序占着` | 关掉厂商上位机、`radar-pointcloud.html` 的「连接串口」、串口助手 |
| `只找到蓝牙串口` | 雷达没枚举成 USB 串口：换数据线（有些线只供电）、装 CH340/CP210x 驱动 |
| `握手没成功` | 拔插 USB 等 5 秒；确认波特率；程序会自己重试，不用重启 |
| `收到字节但解不出包` | 板型选错，试 `--chip icl1112` |
| 左右反了 | 加 `--invert-x` |
| 太灵敏 / 不够灵 | 调 `--x-span 1.0`（横向多少米算满偏） |

完整说明看 `radar-agent\README.md`。

---

## 六、目录

```
radar-runner\
  index.html / radar-connect.html / style.css / js / assets / vendor
  serve.js                  本机静态服务器（没有 python 也能开页面）
  start_game_page.bat
  radar-agent\
    agent.js                主程序：串口 + 判定 + WS + UDP
    radar-core.js           你的雷达内核（自动抽取，勿手改）
    tools\                  extract-core / gen-synth / test-client
    runtime\node.exe        免安装 Node（79MB）
    node_modules\           serialport + ws
    start_radar_agent.bat
```
