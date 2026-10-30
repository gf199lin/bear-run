# radar-agent —— 让游戏不再自己连雷达

一句话：**雷达串口只由这个程序打开，游戏只连它的 WebSocket。**
这样「一会儿能连、一会儿连不上」的问题从根上消失，因为串口永远只有一个进程在碰。

```
雷达 --USB串口--> radar-agent --ws://127.0.0.1:8766--> 游戏(radar-connect.html)
                            \--UDP 8888--> 兼容旧的 radar_bridge.py 链路
```

解析和判定**不是重写的**：`radar-core.js` 直接从组员的 `radar-pointcloud.html`
里抽出同一份代码（解包 / 拼帧 / 点云 / 跟踪 / 手势），所以行为和联调页面一致。

---

## 为什么原来会时好时坏

浏览器直连串口时：

- Web Serial 的接收缓冲默认只有 **8KB**；
- 一帧雷达数据约 **34KB**，而且是突发到达；
- JS 只要卡顿几十毫秒，驱动缓冲就 overrun，读通道被打死 → 页面看起来"连上了又不行"。

`radar-pointcloud.html` 里为此写了 1MB 缓冲、reader 重建、串口整体重开三层自救，
仍然只能缓解。本程序把这一步搬到 Node：1MB 高水位 + 内核缓冲 + 断线自动重连，
游戏侧只面对一条稳定的本地 WebSocket。

---

## 三步跑起来

### 1. 启动雷达代理

双击 `radar-agent\start_radar_agent.bat`

看到这几行就算通了：

```
✔ 游戏请连：ws://127.0.0.1:8766
✔ 串口已打开，开始握手
✔ 配置完成，等待雷达出帧
✔ 自动标定完成：中心 x=0.00m，身高代理 r=0.88m
```

窗口**保持开着**。它自带 Node 运行时（`runtime\node.exe`），不用装环境。

> 想指定串口：`start_radar_agent.bat --serial COM8`
> 想先看有哪些串口：`start_radar_agent.bat --list`

### 2. 打开游戏页面

双击 `radar-runner\start_game_page.bat`
（等价于 `python -m http.server 8801` 后打开 `http://127.0.0.1:8801/radar-connect.html`）

### 3. 在页面里连本地代理

- 地址填 `ws://127.0.0.1:8766`（页面上有「本机 8766」按钮）
- 点「连接」
- 按页面提示走校准：**站中间 → 左 → 右 → 跳 → 蹲**

校准数据存在浏览器里，同一台机器下次不用重做。

---

## 没有雷达也能先验证

```bat
:: 演示动作（跳/蹲/左/右），验证游戏链路
start_radar_agent.bat --sim

:: 用录制的原始字节回放，验证解包与判定
start_radar_agent.bat --replay D:\record\xxx.bin --replay-speed 2
```

自测客户端（另开一个窗口）：

```bat
radar-agent\runtime\node.exe radar-agent\tools\test-client.js ws://127.0.0.1:8766 6
```

它会把帧率、字段、x 范围、jump/crouch 帧数打出来。正常应看到：

```
约 20.0 Hz
✔ 字段齐全
x 范围 -0.80 ~ 0.80
✔ ts 单调
```

---

## 常用参数

| 参数 | 作用 |
|---|---|
| `--serial COM8` | 指定串口；默认自动挑 USB 串口，自动跳过蓝牙口 |
| `--ws-port 8766` | 游戏连接的端口（改了游戏里也要填一样的） |
| `--udp-port 8888` / `--no-udp` | 是否同时向旧 UDP 链路发一份 |
| `--chip icl1240` | `icl1240`=DEV113P（默认），`icl1112`=DEV202H |
| `--x-span 1.0` | 横向多少米算满偏 ±1；觉得太灵敏就调大 |
| `--invert-x` | 左右反了就加上 |
| `--no-jc` | 关闭跳/蹲，退回观察模式（默认已接入 JCDet v1） |
| `--stale 1500` | 多久没有新帧就停推（游戏会自动判掉线回键盘） |
| `--verbose` | 打印每帧判定细节，排查用 |

---

## 现场排障

| 现象 | 原因 / 处理 |
|---|---|
| `端口 8766 已被占用` | 旧的 `radar_bridge.py` 或另一个 agent 还开着。**两者只能留一个**，关掉旧的再启动 |
| `串口被别的程序占着` | 厂商上位机、`radar-pointcloud.html` 的「连接串口」、串口助手没关 |
| `只找到蓝牙串口` | 雷达没被识别成 USB 串口：换数据线（有些线只供电）、装 CH340/CP210x 驱动、换 USB 口 |
| `握手没成功` | 拔插 USB 等 5 秒；确认波特率 1500000（DEV113P）。程序会自己重试，不用重启 |
| `收到字节但解不出包` | 板型选错了：DEV202H 试 `--chip icl1112` |
| 游戏里左右反了 | 加 `--invert-x` |
| 人站中间却一直压着左/右 | 重新校准（把页面校准重做一遍）；或先确认 `--x-span` 不是太小 |
| 人离开后卡在边上 | 程序会在约 1 秒后把 x 收回中间；若仍卡住，看窗口是否在刷 `帧 0`（雷达没出帧） |

---

## 和旧链路的关系

原来的链路是：

```
radar-detect.js --UDP 8888--> tools/radar_bridge.py --ws 8766--> 游戏
```

本程序**把这三段合成一个进程**，并且补上了 `radar-detect.js` 缺的两块：

- **横向 x**：组员旧版 `radar-detect.js` 里 `x` 恒为 0，所以游戏换不了道。
  现在走点云 + 跟踪，横向位置是真实算出来的。
- **自动标定**：`GestureEngine` 必须先标定才输出 x（页面里是手动按钮），
  程序检测到人站定后自动标定 1 秒。

旧的 `radar_bridge.py` 仍然可以用（它也能收本程序的 UDP 8888），
但**不要和本程序同时占 8766**。

---

## 已知限制（现场务必知道）

1. **跳/蹲已接入，但阈值是 v1 实验值。**
   DEV113P 上组员用 JCDet 接管跳/蹲（旧的几何判据在正挂竖板上不成立，已禁用），
   离线评测 39 次跳检出 32、39 次蹲检出 32（82%，见 `qc_report.md`）。
   本程序默认把它接进游戏；**现场误报多就加 `--no-jc` 回到观察模式**。
   注意帧率只有 1.92fps，跳只占 1~3 帧，游戏里的跳跃会有 0.5~1.5s 滞后，需要提前起跳。
   站定在 0.7~1.6m 内等约 3 秒，日志出现 `✔ 跳/蹲检测器就绪` 才开始判定。

2. **真机未在本机验证。**
   本机没有插雷达，已验证的是：串口枚举、内核加载、`--sim` 全链路、
   以及用合成 ICL1240 裸流跑通的「解包 → 拼帧 → 点云 → 跟踪 → 左右」，
   结果是 15360 包 / 0 坏包 / 59 帧 / x 在 ±0.59 之间。
   握手与配置下发逻辑沿用组员 `radar-detect.js`（他们实测 263 帧 0 丢块）。

3. **帧率上限受雷达限制。** 雷达本身约 3.8~7.7 帧/秒，程序只是以 20Hz
   重复推最新一帧，让游戏的掉线判定不闪。真正的动作延迟还是雷达帧率决定的。

4. **跳/蹲依赖站位。** JCDet 的校准门区是 0.70~1.58 m，
   站位偏离太多会一直「校准中」。

---

## 目录说明

```
radar-agent/
  agent.js            主程序（串口 + 判定 + WS + UDP）
  radar-core.js       组员的雷达内核（自动抽取，勿手改）
  tools/
    extract-core.js   重新从 radar-pointcloud.html 抽取核心
    gen-synth.js      生成合成雷达裸流（无硬件自测）
    test-client.js    WS 自测客户端
  runtime/node.exe    免安装 Node（79MB）
  node_modules/       serialport + ws
  start_radar_agent.bat
```

`radar-core.js` 的生成方式（组员更新页面后重跑一次即可，路径指向组员那份页面）：

```bat
radar-agent\runtime\node.exe radar-agent\tools\extract-core.js ^
  <组员目录>\radar-pointcloud.html radar-agent\radar-core.js
```
