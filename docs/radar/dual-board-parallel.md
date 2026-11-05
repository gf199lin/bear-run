# 双板并行采集（1T2R + 2T4R）

> 状态：**已实机验证**（2026-10-30）。两路同时采集、SSE 按板隔离、分屏页面均通过。
> 相关：[boot-2t4r-pure-python.md](boot-2t4r-pure-python.md)（2T4R 自动唤醒原理）

---

## 1. 用法

```bash
python radar-pointcloud/server.py --ports COM3,COM4
```

然后浏览器打开：

| 地址 | 内容 |
|---|---|
| `http://127.0.0.1:8099/dual` | **双板分屏页**（推荐） |
| `http://127.0.0.1:8099/?board=2t4r` | 只看 2T4R |
| `http://127.0.0.1:8099/?board=1t2r` | 只看 1T2R |

**不传 `--ports` 时，单板行为与改造前逐字相同。**

## 2. 自动识别与自动唤醒

端口与板型的对应关系**不依赖 COM 号**，而是靠芯片 USB VID:PID：

| 板型 | 芯片 | VID:PID | 波特率 |
|---|---|---|---|
| 1T2R | CP2102 | `10C4:EA60` | 921600 |
| 2T4R | FTDI FT232 | `0403:6001` | 1500000 |

所以 COM 号因拔插而漂移（COM3→COM7）也不影响识别。

每种板各自的启动逻辑：

- **1T2R**：上电自跑，直接采集
- **2T4R**：上电待机 → 检测到 0 字节 → **自动重放 37 帧启动序列**唤醒（约 2.6 s）

## 3. 实测数据（2026-10-30）

```
[multi] COM3 -> 1t2r (DEV202H (ICL1122) 1T2R)
[multi] COM4 -> 2t4r (DEV113P 2T4R)

[COM3] frames=145 fps=7.65 skipped=30 bad=0
[COM4] frames=37  fps=1.87 skipped=49 bad=0
```

- 1T2R 7.65 fps ≈ 理论 7.72（129.5 ms 帧周期）
- 2T4R 1.87 fps ≈ 理论 1.92（520.8 ms 帧周期）

**SSE 隔离验证**：

```
订阅 board=1t2r -> frames=27 boards=['1t2r'] fps=7.65
订阅 board=2t4r -> frames=7  boards=['2t4r'] fps=1.90
```

两路数据在传输层完全分开，页面不会串数据。

## 4. 架构

```
server.py --ports a,b
        │
        └─ radar_multi.run()
              ├─ RadarSession(COM3, 1t2r)
              │     ├─ PointCloudProcessor  ← 独立杂波记忆
              │     ├─ ActionRecognizer     ← 独立滑窗
              │     └─ RadarController      ← 独立重连/停滞自愈
              └─ RadarSession(COM4, 2t4r)
                    └─ (同上，参数按板型)
                        ↓
                  broadcast(payload, board=)
                        ↓
              /stream?board=1t2r   /stream?board=2t4r
```

**每板一份状态**：`PointCloudProcessor` 有杂波记忆、`ActionRecognizer` 有滑窗，
这些**绝不能共用**，否则两路的门限与判据会互相污染。

**时间轴不混**：1T2R 与 2T4R 帧率差 4 倍，两条流各按自己的 `stats` 统计，
页面分开显示，**不做任何插值对齐**（对齐会让慢的一路看起来像丢了帧）。

**一板故障不连坐**：每板独立线程 + 独立 controller，拔掉一块不影响另一块。

## 5. 已知限制

1. **同型号双板会互相覆盖**：会话以板型为键（`sessions[board]`）。
   当前两块板型号不同，没有影响；若将来同时插两块 2T4R，
   需要改成按 USB 序列号 / 物理口做键。
2. **CP2102 序列号不唯一**：`SER=0001` 是出厂默认值。两块 CP2102 板
   无法靠序列号区分，只能靠 USB 物理口（`LOCATION`）。
   FTDI 的序列号是唯一的（当前 2T4R 为 `A02C56YMA`）。
3. **两个 iframe 各自全屏渲染**：分屏页是「一页两窗」，
   不是把两路点云画进同一坐标系。若要叠加显示，需要另外做合流渲染。
4. **控制按钮作用于全部板**：分屏页的「开始/结束」会同时控制两块板
   （`/control` 不带 board 参数时广播到所有会话）。

## 6. 相关文件

| 文件 | 作用 |
|---|---|
| `radar-pointcloud/radar_multi.py` | 多板会话与入口（新增） |
| `radar-pointcloud/dual.html` | 双板分屏页（新增） |
| `radar-pointcloud/server.py` | 新增 `--ports`、`/dual`、`/status?board=`、`/stream?board=` |
| `radar-pointcloud/radar_source.py` | 2T4R 自动唤醒（`auto_boot`） |
| `radar-pointcloud/radar_boot_2t4r.py` | 启动序列重放（`send_boot`） |
