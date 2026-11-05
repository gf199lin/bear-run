# 跳 / 蹲采集引导程序（jumpduck_guide）

> 2026-11-05。用 `tools/jumpduck_guide.py` 在浏览器里引导人做 站好 / 跳 / 蹲，
> 后台线程采雷达原始帧并打真值段标签。产出可直接喂 `tools/train_jumpduck.py`。

## 用法

```powershell
# 默认：2T4R、130 ms 配置（7.6 Hz / 32 chirp / 16 bin），2 轮，自动开浏览器
python tools\jumpduck_guide.py --port COM4

# 520 ms 对照采集（1.92 Hz / 64 chirp / 32 bin）
python tools\jumpduck_guide.py --port COM4 --profile base

# 不碰设备配置 / 不开浏览器（排障用）
python tools\jumpduck_guide.py --no-boot --no-open
```

页面每次启动时会自动下发配置，**不需要提前设 `RADAR_2T4R_*` 环境变量**。
关掉终端窗口 = 结束采集。

## 页面

- 环形倒计时 + 动作大字（站好/跳/蹲）+ 进度 `N/5`
- 底部 16 根距离 bin 能量柱（当前峰值 bin 高亮）
- 实时帧率（全局平均，非瞬时值）

每段流程：播报 1.6 s 准备（不计入真值）→ 蜂鸣 → 采集 `--sec-base` / `--sec-act` 秒。

## 产出

```
data/jumpduck_guide/<YYYYmmdd-HHMMSS>/
    frames.npz    z=(N,4,chirps,bins) complex64, t=(N,), label=(N,)
    labels.json   段名 base1/jump/base2/duck/base3 + frame_start/end + band
    level.csv     每帧峰值 bin / 距离
```

段名与 `train_jumpduck.py` 完全一致；`band` 按实际 bin 数写（16 bin → `[6,16]`，
32 bin → `[6,21]`），避免下游用默认 `[6,21]` 越界。

## 两个必须记住的坑

### 1. 下发配置和采集必须用同一个串口句柄

FT232 的 DTR 一掉（关串口再重开），2T4R 会退到**不完整出流**：实测
70 kB/s → 45 kB/s，组帧永远凑不齐，表现为「前面几段正常、后面越采越少」。

所以 `apply_profile()` 返回保持打开的 `serial.Serial`，采集用 `DirectReader`
直接读这个句柄，不经过 `SerialSource`（它会关串口重开）。

代价：一次采集结束后设备停在退化状态，下次启动会重新下发恢复。
不要用「上个进程刚采过所以这次不用下发」的思路。

### 2. 不能 `sys.path.insert(bear-run-new/radar-service)`

那个目录有**旧版** `radar_protocol_dev113p.py`（硬编码 136 B 帧长 / `buf[4]==0x1F`）。
它一旦抢先 import，16 bin 的帧（`buf[4]==0x0F`）会被 100% 判成坏包：
实测 `bad=3029/3029`、`frames=0`，但同一批字节用 `radar-pointcloud` 的解析器
`frames=2036 / bad=1`。

因此 `radar_boot_2t4r` 用 `importlib.util.spec_from_file_location` 按文件路径加载，
只在函数作用域内使用，不改 `sys.path`。

## 实测（2026-11-05）

| 场景 | 帧数 | 帧率 | 说明 |
|---|---|---|---|
| 130 ms / 1 轮 / 2+3 s | 193 | 6.8–6.9 Hz / 段 | 5 段均正常 |
| 130 ms / 3 轮 / 4+4 s | 670 | 7.5 Hz | 长跑不衰减 |
| 520 ms / 1 轮 / 3+3 s | 47 | 1.92 Hz | `--profile base` |

下游：`train_jumpduck.build_samples_npz()` 可直接造出 NONE / JUMP / DUCK 三类样本
（window=5 时 69 条，window=7 时 59 条）。

## 未验证

- 真实跳 / 蹲的可分性（本次只验证链路与数据格式，动作未真做）。
- 130 ms 配置下俯仰相位差对跳跃高度的敏感度。
