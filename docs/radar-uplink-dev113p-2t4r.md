# DEV113P / 2T4R 上行链路协议（实测破解）

> 状态：**已执行证据**（非计划），**已接入 `radar-pointcloud` 网页并跑通出图**（见 §7），
> 且 2T4R 的**距离/速度刻度已由厂商配置收口**（`range_res ≈0.150 m`、`v_max ±0.376 m/s`，
> 见 §7.3；证据等级：配置推导 + 1T2R 交叉验证，非卷尺实测）。
> 数据来源：COM4（FTDI FT232, `VID_0403&PID_6001`）@ 1500000 8-N-1，
> 抓包 `.deepworks/tmp/2t4r-COM4-b1500000-8s.bin`（531712 B / 8.105 s）与 `.deepworks/tmp/fast-1500000-0.5s.bin`。
> 波形参数来源：ONELAB 上位机 `Radar_Config.ini` 的 `[WAVE_INFO_0]`/`[MULTI_WAVE_INFO]`/`[OBJECTDETECT]`。
> 分析脚本：`.deepworks/tmp/verify_2t4r_contract.py`、`.deepworks/tmp/group_by_counter.py`、`.deepworks/tmp/parse_dev113p.py`、`.deepworks/tmp/verify_2t4r_calib.py`。
> 最后更新：M1 节点（10/19）当天；含接入验证、刻度收口，以及 §7.6 的**帧尾 3 字节完全解出**
> （136 字节全字段已定，无剩余未知字节）。

## 1. 结论摘要

| 项 | 值 | 证据强度 |
|---|---|---|
| 波特率 | **1500000**，8-N-1 | 实测：仅此波特率下能解出 `0xAA`/同步结构；源码 `icl_uart.c:44` 佐证 |
| 帧长 | **136 字节** | 3908/3908 个间隙 == 136（比例 1.0000） |
| 同步字 | **`0x55 0xAA`** | 帧首 100% 命中 |
| 距离 bin 数 | **32**（`off4 = 0x1F` = 31） | 2333+ 帧 100% 命中；与源码公式自洽 |
| 帧率 | **≈482 帧/s** | 3909 帧 / 8.105 s |
| **雷达帧率** | **≈1.73 雷达帧/s**（64 chirp / 帧） | 8.105 s 内组装出 14 个完整帧；482 ÷ 256（64 帧 × 4 RX）|
| 每 chirp 帧数 | **4**（= 4 个 RX） | 976/978 组恰为 4 帧 |
| RX 序号位置 | 帧尾 `off135` **bits[3:2]** | 976/978 组的 4 帧依次为 `0,1,2,3`（RX 无需靠到达次序猜） |
| 帧尾校验和 | `BE16[133:135] == sum(128 B 的 64 个 int16) mod 65536` | **3908/3908 命中（100%）** |
| 雷达帧序号 | 帧尾 `off135` **bits[7:4]**（mod 16） | 组内恒定；15/15 次变化都发生在 chirp 63→0 且都 +1 |
| chirp 步进 | 每 4 帧 counter **+0x10** | 步进分布：0×2931、+16×962、回绕(-1008)×15 |
| 字节率 | 65.6 kB/s ≈ 1.5 Mbps 的 **42%** | 帧间有间隔；**非连续发送**，故此前「丢数据」的猜测不成立 |

**为什么早前一直认不出**：项目里既有的 1T2R 解析（`ICLH`/`ICLT` 包裹 + `0xAA…0x55` 裸帧）与
DEV113P 的 `datatransfer_dma_uart` 固件**完全是两套格式**。后者编译的是
`components/radar_process/radar_data.c`（**不含** `radar_data_error_happen.c`），
因此**不做** header/footer 重写；线上是 DSP 硬件原始 HAS 包再做 32-bit 字内字节翻转。

## 2. 帧结构（136 字节）

| 偏移 | 长度 | 内容 | 实测 |
|---|---|---|---|
| 0 | 2 | 同步字 `55 AA` | 100% |
| 2 | 1 | `0x80 \| (chirp >> 4)` | 4 个值：`80/81/82/83` |
| 3 | 1 | `(chirp & 0x0F) << 4`（低 4 位恒 0） | 16 个值：`80,90,…,F0`（步长 0x10） |
| 4 | 1 | **点数-1 = `0x1F`**（→ 32 点） | 100% |
| 5 | 128 | **32 个距离 bin**，每 bin 2 个 **big-endian int16** | 97.7% 落在 ±2048 |
| 133 | 2 | **校验和**（大端）：payload 128 B 解读为 64 个 int16 之和 mod 65536 | **3908/3908** |
| 135 | 1 | **帧标签**：bits[7:4]=雷达帧序号 mod 16、bits[3:2]=RX 序号、bits[1:0] 恒 0 | 见 §7.6 |

- `counter = (off2 << 8) | off3`；同一 chirp 的 4 帧 counter 相同，组间 +0x10。
- **`counter` 只是 chirp 的冗余编码**：`counter = 0x8000 | (chirp << 4)`（3908/3908 成立）
  → off2/off3 是 chirp 的副产品，**不是独立计数器**，不含可用于检测丢帧的序号。
- 每 4 帧 = 同一 chirp 的 **4 个 RX 通道**（RX 序号在 off135 bits[3:2]）。
- 数据区 128 B = 32 bin × 4 B（两个 int16）→ 与 `DSP_GetDataLength()` 的
  `DSP_1DFFT: len = points*4 + 8`（32×4+8 = **136**）**完全吻合**。
- **至此 136 字节全部字段已定，无剩余未知字节**（证据见 §7.6）。

## 3. 字节序成因（重要）

`HAS_DATA_Output`（`data_transfer.c`）在输出前对**每个 32-bit 字**做整体字节翻转：

```c
small = *((uint32_t*)buf + i);
big   = byteswap32(small);
*((uint32_t*)buf + i) = big;
```

后果：线上 4 字节组是内存字节的逆序，故 bin 的 int16 在线上表现为 **big-endian**。
**实测判据**：按 BE int16 解读数据区，97.7% 幅值落在 ±2048（中位 |v|=15，p95=892）——
合理的 ADC 幅度；按 LE 解读则得 ±32768 满幅假值。

## 4. 已确认 vs 待确认

### 已确认（实测）
- 物理层参数、136 字节帧、同步字、32 bin、帧率、4 帧/chirp。
- 数据区起点：对 off=4/5/6/7/8 做 BE int16 幅值合理性扫描，
  **off=5 得 0.977**、off=7 得 0.983（同一 4 字节组内的另一槽位），
  而 off=4/6/8 仅 0.30 且出现 ±32768 假值 → 起点锁定 off=5。
- 解析后能解出**合理 FMCW 距离谱**：`bin0` 虚部恒为 0（直流泄漏特征）、
  `bin1` 幅度最大（强近距目标）、`bin7+` 衰减至噪声底。
- 组内 RX 相位关系**稳定**：`bin1` 的 RX1-RX0 相位差 = 2.919 rad，std = **0.094 rad**；
  而噪声 bin（2/5/6）std 达 1.2–2.1 rad → 证实 `bin1` 是真实目标、4 帧确为 4 个 RX。
- **帧尾 3 字节完全解出（10/19 离线收敛，见 §7.6）**：`off133:135` = 16 bit 大端校验和，
  `off135` bits[3:2] = RX 序号、bits[7:4] = 雷达帧序号 mod 16。
- **抓包流完整性**：3908 帧 **100% 通过校验和**、100% 间距 136 B、RX 组 976/978 完整，
  解析器已把校验和与 RX 标签接入（`StreamParser.bad_checksum`、`FrameAssembler.gap_frames/gap_groups`）。

### 待确认（需真值实验，不得写入已实现功能）
1. **两套子阵列的 TX 映射**：已实测（`.deepworks/tmp/test_tdm_tx.py`）按 counter 奇偶分组后，
   两组各自都呈现**极稳定的阵列相位梯度**（bin1，std 仅 0.08 rad ≈ 4.6°）：

   | 组 | RX0..3 平均 \|bin1\| | RX1-RX0 | RX2-RX0 | RX3-RX0 | std |
   |---|---|---|---|---|---|
   | 奇数 chirp（疑 TX0） | 400 / 1227 / **5644** / **7157** | 2.865 | -1.418 | 1.184 | 0.080–0.081 |
   | 偶数 chirp（疑 TX1） | 407 / 1209 / 1246 / 808 | 2.976 | -0.185 | 0.139 | 0.079–0.081 |

   两组之间同 RX 的相位偏移：RX0 = -0.107、RX1 = 0.005、RX2 = **+1.126**、RX3 = **-1.152**
   （std 0.024–0.120）。

   **注意**：纯 TDM-MIMO 下，切换 TX 对所有 RX 产生的相位偏移应**相同**；实测 RX0/RX1 ≈ 0
   而 RX2/RX3 显著不同 → 帧序 ↔ 物理 RX 的映射、以及 TX 交替方式**尚未确定**。
   需真值实验（移动目标观察相位）确认。**不得据此推断 8 虚拟通道已可用。**
2. ~~**RX 物理间距与角度标定**：需要阵列几何（`d/λ`）才能把相位差换成方位角。~~
   **已由厂商配置收口（10/19）**：`Radar_Config.ini` 的 `[OBJECTDETECT]`
   `HorzAngleInfo = 5.900,1.000,0,2;0,1,2,7,` → **水平阵元间距 = 5.900 mm**
   （1T2R 的 `.bak` 里是 `6.250,...`，与 `radar_dsp.ANT_SPACING = 6.250e-3` 逐字吻合）。
   `d/λ = 5.900/12.237 = 0.482`（略小于 0.5，无栅瓣）→ 几何自洽。
   证据等级：**厂商配置来源**，非卷尺实测。
   注：`VertAngleInfo = 5.900,1.000,0,2;5,2,3,` **非空** → 2T4R 的俯仰角**有**通道组合
   （1T2R 的 `.bak` 里该段为空，这才对应 AGENTS「俯仰角不可用」）。**但虚拟通道索引
   `5,2,3` 到物理天线高度的映射仍未确定，不得据此宣布「2T4R 可测人体高度」。**
3. ~~**bin → 距离标定**：需要 chirp 斜率与采样率。固件寄存器配置或 ONELAB 工具配置中查找。~~
   **已由厂商配置收口（10/19）**，见 §7.3 的完整推导：
   `[WAVE_INFO_0]` `IdleFreq = 2.400100e+10` → `TopFreq = 2.500000e+10`
   ⇒ **B = 999 MHz** ⇒ `range_res = c/(2B) ≈ 0.150 m`。证据等级：**配置推导**
   （同一组公式代回 1T2R 得 0.6813 m / 1.535 / 0.0480，与既有模块常量 5 项全中 ⇒ 模型可信）。
   建议补一次卷尺真值点检（方案见 `docs/hardware-baseline.md` §3 注）。
   ⚠️ 同文件的 `MTT_RangeResolution = 125`（mm）**不可用作 range_res**：1T2R 配置里它同样是
   125，而 1T2R 真实值是 0.6813 m ⇒ 该字段与真实带宽无关（疑似上位机算法遗留值）。
4. ~~**帧尾 3 字节语义**：`off133` distinct=17、`off134` distinct=256、`off135` distinct=64
   （每值 64 次，步长模式）→ 疑似计数器/CRC 组合，未定。~~
   **已完全解出（10/19 离线收敛，见 §7.6）**：`off133:135` = `sum(64 个 int16) mod 65536`
   的**大端**校验和；`off135` = `(雷达帧序号 mod 16) << 4 | (RX 序号 << 2)`。
   早前把 `off133`/`off134` 分开统计产生的「distinct=17 / 256」是把它当单字节看的假象。
5. ~~**counter 回绕周期**：counter 仅约 64 个不同值后回绕（`0x8000…0x83F0`），
   是否为「一个 radar frame = 64 chirp」需长时抓包确认。~~
   **已于 10/19 确认**（`.deepworks/tmp/verify_frames_dev113p.py`）：8 s 抓包内 counter 恰有
   64 个唯一值（`0x8000…0x83F0`，步长 0x10，奇数 chirp 在 `80/82`、偶数在 `81/83`），
   `chirp = (counter >> 4) & 0x3F` 覆盖 0..63 无缺口 → **一个 radar frame = 64 chirp × 4 RX × 32 bin**，
   组装出 14 个完整帧（1 个因抓包起点不完整而丢弃）。
6. **COM4 = DEV113P 的真值标注**：目前依据是「FTDI FT232 + 1.5 Mbps 下协议自洽」，
   尚无物理端口贴标确认。

## 5. 与 1T2R 的关键差异（集成时必须分支）

| 项 | 1T2R（DEV202H/ICL1122, COM3） | 2T4R（DEV113P, COM4） |
|---|---|---|
| 芯片 | CP210x `10c4:ea60` | FTDI FT232 `0403:6001` |
| 波特率 | 921600 | **1500000** |
| 外层包裹 | `ICLH` + len + seq … `ICLT` | 无 |
| 帧长 | 40 字节裸帧 + 包裹 | **136 字节** |
| 同步字 | `0xAA` … `0x55` | **`0x55 0xAA`** |
| 载荷 | 2 RX × 64 chirp × 8 bin | 32 距离 bin × 2 int16（BE），4 RX/chirp |
| 字节序 | 原样 | **32-bit 字内整体翻转** |

> 既有 `radar-pointcloud/radar_protocol.py` 写死 1T2R 假设（8 bins、`0x30/0xB0`、64 chirps、
> `FrameAssembler` 固定 2×64×8），换板会「一个包都认不出」→ 必须新增按硬件指纹分支的 DEV113P 解析器。

## 6. 复现方法

```powershell
# 抓包（先用 serial_preflight.py sniff，或直接）
python .deepworks\tmp\fastsniff.py COM4 1500000 0.5 .deepworks\tmp\fast-1500000-0.5s.bin
# 契约验证
python .deepworks\tmp\verify_2t4r_contract.py .deepworks\tmp\2t4r-COM4-b1500000-8s.bin
# 分组 / RX 结构
python .deepworks\tmp\group_by_counter.py .deepworks\tmp\2t4r-COM4-b1500000-8s.bin
# 帧尾 3 字节 + 全帧字段复核（离线，不需要新数据）
python .deepworks\tmp\decode_frame_tail.py .deepworks\tmp\2t4r-COM4-b1500000-8s.bin
python .deepworks\tmp\probe_counter_tag.py .deepworks\tmp\2t4r-COM4-b1500000-8s.bin
python .deepworks\tmp\verify_dev113p_tail.py .deepworks\tmp\2t4r-COM4-b1500000-8s.bin
# 距离谱
python .deepworks\tmp\parse_dev113p.py .deepworks\tmp\2t4r-COM4-b1500000-8s.bin
```

固件源码位置：`.deepworks/tmp/dev113p/src/DEV113P/`（源自 `DEV datatransfer.zip` → 内层 `DEV113P_V1.0.0.zip`）。
关键文件：`drivers/uart/icl_uart.c`（波特率）、`drivers/dsp_4/icl_dsp.c`（`DSP_GetDataLength`）、
`components/data_process/data_transfer.c`（`HAS_DATA_Output` 字节翻转）、
`boards/datatransfer_dma_uart/ses/datatransfer_dma_uart.emProject`（编译宏与文件列表）。

## 7. 接入 `radar-pointcloud` 网页（10/19 已执行）

**目标**：让新插上的 2T4R 板直接可用于现有网页软件，且**不改动 1T2R 路径的行为**。

### 7.1 新增/改动文件

| 文件 | 改动 |
|---|---|
| `radar-pointcloud/radar_protocol_dev113p.py` | **新增**。`StreamParser`（`55 AA` 同步、136 B 帧、BE int16、32 bin）+ `FrameAssembler`（64 chirp × 4 RX → `(z0,z1,z2,z3)`）|
| `radar-pointcloud/radar_dsp.py` | `PointCloudProcessor` 参数化：新增 `chirps/bins/range_res/v_res/ant_spacing` 关键字参数，全部默认 `None` → 回退模块常量（**默认行为逐字不变**）|
| `radar-pointcloud/radar_source.py` | 新增 `BOARD_PROFILES` / `detect_board()` / `find_board_for()` / `probe_board()` / `make_protocol()`；`SerialSource.baud` 支持 `None`=按板型自动；`SerialSource.board` 在开串口时按指纹复核；`build_pipeline(..., board=None)` 在板型变化时重建解析器；`ReplaySource` 支持板型节奏（1T2R 991×52 B / 2T4R 482×136 B）|
| `radar-pointcloud/server.py` | 新增 `--board auto\|1t2r\|2t4r`；`--baud` 默认改为 `None`（按板型自动）；启动时按板型定 DSP 参数与 `LATEST['cfg']`；cfg 增加 `board` / `board_label` / `range_res_verified` |
| `radar-pointcloud/viewer.html` | 距离-多普勒图的行列数从 `cfg.chirps`/`cfg.bins` 取（原先写死 64×8）；速度轴标注用 `cfg.v_max`；顶栏副标题与参数表随板型显示；距离轴未标定时显式提示 |

### 7.2 自动识别链（两级）

1. **启动时**：`probe_board()` 按 USB VID:PID 判定 —— `10c4:ea60`(CP210x) → `1t2r`，`0403:6001`(FT232) → `2t4r`。
   认不出则回退 `1t2r`（与改动前行为一致）。这一步决定 DSP 的距离单元数与速度刻度。
2. **开串口时**：`SerialSource._try_open()` 用 `find_board_for()` 复核实际打开的端口，
   据此把波特率切到 921600 / 1500000，`build_pipeline` 再按 `source.board` 选解析器。
   —— 两级都用同一套指纹，避免「启动猜 A、实际开 B」导致参数错配。

### 7.3 2T4R 的物理换算（10/19 已收口，来源 = 厂商上位机配置）

**来源**：`Radar_Config.ini`（ONELAB 上位机写出的运行配置，`Ver=1.1.9`，
`ChipType=Sensor_PoplarD`、`ChipName=ICL1240`、`ModuleName=DEV113P`、`DataType=1dfft`）
的 `[WAVE_INFO_0]` / `[MULTI_WAVE_INFO]` 段。

| 量 | 1T2R（`.bak` 实测口径） | 2T4R（当前 `Radar_Config.ini`） | 公式 |
|---|---|---|---|
| `IdleFreq` | 2.4015e10 | 2.400100e10 | — |
| `TopFreq` | 2.4235e10 | 2.500000e10 | — |
| 带宽 B | **220 MHz** | **999 MHz** | `Top−Idle` |
| 中心频率 fc | **24.125 GHz** | **24.5005 GHz** | `(Top+Idle)/2` |
| 距离分辨率 | **0.6813 m** | **≈0.150 m** | `c/(2B)` |
| 帧周期 / chirp 数 | 129.5 ms / 64 | 520.84 ms / 64 | `FrameTimeUS` |
| chirp 周期 Tc | 2.0234 ms | 8.138 ms | `FrameTime/Chirps` |
| 速度量程 `v_max` | **±1.535 m/s** | **±0.376 m/s** | `λ/(4·Tc)` |
| 速度分辨率 `v_res` | **0.0480 m/s** | **0.01175 m/s** | `λ/(2·N·Tc)` |
| 水平阵元间距 | **6.250 mm** | **5.900 mm** | `HorzAngleInfo` |

**交叉验证（关键）**：上表公式代回 **1T2R** 一行，得出 0.6813 m / 24.125 GHz / ±1.535 /
0.0480 / 6.250 mm —— 与 `radar_dsp.py` 既有模块常量（`RANGE_RES` / `F0` / `V_MAX` /
`V_RES` / `ANT_SPACING`）**5 项逐字吻合**，证明「读 ini 波形段 → 套公式」这条链可信，
故 2T4R 一列同样可信。2T4R 的 `v_max`/`v_res` 还与真机实测（≈0.374 / 0.0117）吻合。

**证据等级声明**：以上为**厂商配置推导**，**不是卷尺实测**。已据此把
`cfg.range_res` 从「借用 1T2R 的 0.6813」改为 **0.150**，`cfg.range_res_verified`
由 `false` 改为 **`true`**（网页「距离轴未标定」提示随之消失）。
仍建议按 `docs/hardware-baseline.md` §3 注做一次卷尺点检做最终确认。

**其余保留项**：
- 近距门 `min_range` 随板型改为 **0.15 m**（= 本板型 1 个距离单元，整门排除 bin0）。
  原先 hardcode 1T2R 的 0.6813 m，对 2T4R 会**误屏蔽 bin0–bin3**
  （0.075/0.225/0.375/0.525 m 全部 < 0.6813）。
- 波长 λ 也随板型传入 DSP（`lam = c/fc`）：测角 `sinθ = Δφ·λ/(2π·d)` 同时依赖 λ 与 d，
  两块板两者都不同；只改 d 不改 λ 会留下 ~1.5% 固定角误差（24.125 vs 24.5005 GHz）。
- DSP 仍只用前两个 RX（`z0`/`z1`）。角度由这两通道相位差得出 → **2T4R 的 4 通道能力尚未利用**，
  8 虚拟通道需等 §4-1 的 TX/RX 映射确认。

### 7.4 验证证据（全部已执行）

| 检查 | 命令 | 结果 |
|---|---|---|
| 语法 | `python -m py_compile` × 5 文件 | OK |
| 指纹识别 | `detect_board()` 单测 | FT232→`2t4r`、CP210x→`1t2r`、未知→`None` |
| 离线端到端 | `.deepworks/tmp/verify_dev113p_e2e.py` | 3908 帧解析/0 坏包 → 14 完整帧 → 每帧 11–23 点 |
| 集成链路 | `.deepworks/tmp/test_pipeline_integration.py` | `ReplaySource(2t4r) → build_pipeline → DSP` 12 帧，fps **1.72** |
| 真 server（回放） | `.deepworks/tmp/test_server_smoke.py` | `cfg.bins=32`、`cfg.board=2t4r`、`rd` 数组长度 **2048 = 64×32** |
| 真 server（真机） | `.deepworks/tmp/test_serial_2t4r.py` | COM4 自动识别 2T4R → 自动切 1500000 → **1.75 雷达帧/s**，bad=0，每帧 16–20 点 |
| 1T2R 回归 | `.deepworks/tmp/test_regress_1t2r.py` | 模块常量、默认 DSP、默认波特率、loopback 端到端 cfg（bins=8/v_max=1.535）**全部一致** |
| **2T4R 标定三源比对** | `.deepworks/tmp/verify_2t4r_calib.py` | ini 原始数字独立重算 ↔ `BOARD_PROFILES` ↔ 真 server `/status` cfg 三者一致；1T2R 反向 5 项全中 → **ALL CALIB CHECKS PASS** |
| **帧尾/全帧复核** | `.deepworks/tmp/verify_dev113p_tail.py` | 33 项 **ALL PASS**（校验和 3908/3908、RX 标签、帧序号、组装器、负例） |
| 1T2R 回归（帧尾改造后重跑） | 上述 6 脚本串跑 | 全绿；`test_serial_2t4r.py` 因 COM4 被 8099 服务占用只报「NO-FRAMES 排他」，离线回放链路不受影响 |

**标定后的真机 `/status` cfg 回显**（`--source serial --port auto`）：

```json
{ "range_res": 0.15, "max_range": 4.8, "range_res_verified": true,
  "v_max": 0.376, "v_res": 0.0118, "chirps": 64, "bins": 32,
  "board": "2t4r", "board_label": "DEV113P 2T4R", "min_range": 0.15 }
```

点云自洽性抽检：首点 `[x=0.099, y=0.202, deg=26.0, v=-0.38, p=0.919]`
→ `r = √(0.099²+0.202²) = 0.225 m = bin1 中心`（`1.5×0.15`）✓，
bin0（0.075 m）已被 `min_range=0.15` 排除 ✓。

**真机运行日志片段**（`python server.py --source serial --port auto`）：

```
板型: 2t4r (DEV113P 2T4R)  [按 USB VID:PID 自动识别]
[serial] 识别到板型 2t4r（DEV113P 2T4R）→ 波特率 1500000
[serial] 使用串口 COM4（自动选择理由: single）
[  2.74s] frames=5 fps=1.83 pts=15 skipped=73 bad=0 conn=True rc=0 err=0
[  6.90s] frames=13 fps=1.88 pts=18 skipped=73 bad=0 conn=True rc=0 err=0
```

### 7.5 仍未完成 / 未验证

- **2T4R 的左右换道识别未做**：网页现在能出点云，但「挥手换道」的门限仍是 1T2R 的标定值
  （日志里 `band=(0.7, 1.35)`），**必须在 2T4R 上用真值实验重新标定**，否则不能写进验收结论。
  → 与 `docs/handoff-leftright-radar.md`、`docs/验证报告-左右移动识别.md` 衔接。
- 跳/蹲（`JUMP`/`DUCK`）仍受 `docs/jump-duck-verification-plan.md` 的验证门约束，本次**未触碰**。
- `range_res` 的**卷尺真值点检**未见执行（现结论为配置推导 + 交叉验证 + 1T2R 站位自检佐证，
  见 `hardware-baseline.md` §3）；**用户 10/19 告知当前无法采集数据 ⇒ 该项挂起**，
  待能到现场时按 `hardware-baseline.md` §3 的点检方案执行。
  TX/RX 物理映射（§4-1）未完成。
- 本方案依赖 `radar_source.py` 里新增的 `BOARD_PROFILES` 指纹表；若厂商换 USB-UART 芯片，
  需同步更新该表（并更新本文档）。

### 7.6 帧尾 3 字节完全解出（10/19 离线收敛）

**背景**：用户当时无法采集新数据，故只在既有 8 s 抓包
（`.deepworks/tmp/2t4r-COM4-b1500000-8s.bin`，3908 帧）上做**离线再分析**与解析器加固。

#### 已解的字段（证据：`.deepworks/tmp/verify_dev113p_tail.py`，33 项 ALL PASS）

| 字段 | 结论 | 实测命中 |
|---|---|---|
| `off0:2` | `55 AA` 同步字 | 3908/3908 |
| `off2`/`off3` | `counter = 0x8000 \| (chirp << 4)` → `off2 = 0x80\|(chirp>>4)`、`off3 = (chirp&0xF)<<4` | 3908/3908（三项分别命中） |
| `off4` | `0x1F` = 点数-1 | 3908/3908 |
| `off5:133` | 32 bin × (I,Q) big-endian int16 | — |
| `off133:135` | **校验和**：`sum(64 个 int16) mod 65536`，**大端** | **3908/3908** |
| `off135[3:2]` | **RX 序号**（0..3） | 976/978 组依次 `0,1,2,3`；`[1:0]` 恒 0 |
| `off135[7:4]` | **雷达帧序号 mod 16** | 组内恒定；15/15 次变化都在 chirp 63→0 且都 **+1**，并回绕 15→0 |

**关键纠正**：`counter` **不是**独立计数器，只是 chirp 的冗余编码
（早前假设「8 bit 滚动组计数、回绕于 256」是错的 —— 实测 `off2` 每 16 组才变一次、
`chirp` 每 64 组回绕，故 `n` 并不逐组 +1）。**真正的帧序号在帧尾标签高 4 位**，
因此可检出「整条雷达帧丢失」（`gap_frames`），但也只能是 mod 16、无法给出绝对帧号。

#### 抓包流完整性（可写进报告的量化结论）

| 量 | 值 |
|---|---|
| 总字节 / 帧数 | 531712 B / **3908 帧** |
| 头部非帧字节 | **118 B**（抓包时串口已在流中，起点落在帧中段） |
| 尾部残帧 | **106 B**（不够一帧即停） |
| 帧间距 | 3908/3908 恰为 136 B |
| 校验和失败 | **0** |
| 组装结果 | `frames=14`、`dropped=1`、`gap_frames=0`、`gap_groups=0`、`overrun=0` |
| 解释 | 8.1 s 内共 15 条雷达帧，**仅第 1 条因抓包起点不完整而丢弃** → 稳态**无任何确认丢帧** |

（两个 RX 顺序「例外」组 `0x81A0`=[2,3]、`0x82B0`=[0,1] 也都在第 1 条不完整帧内，
不是丢帧，只是该帧被截断后剩余的尾巴；这也解释了早前把它们当成「乱序」的误判。）

#### 解析器加固（`radar_protocol_dev113p.py`）

- `parse_frame(..., verify=True)`：**校验和不符直接丢弃**（不再放过被截断/串扰的帧）；
  同时从 `off135` 读出 `rx` 与 `frame_seq`。
- `StreamParser(verify=True)`：新增 `bad_checksum` 计数，校验和失败时按 **skip 1 字节**保守重同步
  （而不是跳 136 B，避免错过真实帧边界）。
- `FrameAssembler`：
  - RX 槽位取 `fr.rx`（帧尾标签），**不再依赖到达次序** —— 修掉「漏掉某 chirp 的第 1 帧
    就把 3 路数据错填进 4 个槽位」的隐患；
  - **帧边界判据改为 `frame_seq` 变化**（比「chirp 号回绕」更可靠，不假设 chirp 单调）；
  - `diag()` 现在给出 `gap_frames`（整帧丢失）/ `gap_groups`（帧内丢 chirp 组）/ `overrun`/`dropped`。
- 该模块 docstring 已同步更新；`counter_to_n`（基于错误模型的函数）已删除，改为
  `chirp_to_counter()`（仅供构造合成帧）。

#### 影响

- 对**功能无行为变化**（正常流下校验和 100% 通过）→ 只是把「静默容忍坏帧」改成「计数并丢弃」，
  并把丢帧诊断量化，便于现场演示时判断链路质量。
- 1T2R 路径完全未受影响（改动仅在新模块内，回归脚本重跑全绿）。

### 7.7 命令模式实测证据（10/19 离线；来源 = 厂商上位机日志里的原始字节）

**背景**：用户 10/19 明确「无法采集数据」⇒ 本轮只做**不需要新数据**的取证。
唯一还能推进的方向是**厂商工具自己留下的通信日志**：工具配置 `appConfig.xml` 里
`EnableDumpProtocolBuffer="1"` ⇒ 每次 `_Send:` / `_Recv:` 的**原始 hex 同时写进** `Log\LogFile_*.txt`。
⇒ 于是拿到了**两块板真机下达命令的字节原文**（此前只有厂商文档里的通用示例）。

脚本（均可复跑）：`.deepworks/tmp/{extract_protocol_bytes.py, dump_log_transcripts.py,
decode_registers2.py, regs_dump_search.py}`；输出见同名 `.out`。

#### 板型指纹（命令模式下实测，两块板都拿到）

| 项 | 1T2R（DEV202H） | 2T4R（本板） |
|---|---|---|
| 打开命令模式 `0x00FF` 返回 | 协议版本 **3**、缓冲 **64**（`03 00 40 00`） | 协议版本 **3**、缓冲 **512**（`03 00 00 02`） |
| 读模组名 `0x0105` | 成功 → **"DEV202H"** | **失败 `0x0FA7`**（本板不返回模组名） |
| 读版本 `0x0100` | 结果=0 + 5×u16 `[0x8000,0,1,1,1]`（工具打印 `v1.1.1_32768.0`，**数值结构体**） | 结果=0 + 长度 7 的**字符串** `"VA1.0.0"` |
| 读序列号 `0x0011` / 设接口类型 `0x0016` | 失败 `0x0001` | 失败 `0x0FA8` / `0x0FA7` |
| SetRxChannelBit `0x0020` | `03`（2 RX） | `0F`（4 RX） |
| SetChipAddress `0x0006` | `20 22 21 23` | `24 26 25 27` |
| SetChipCount `0x0005` | 1 | 1 |
| 地址数据格式 `0x0007` | `0`（A8D16）⇒ 回落 A16D16（4 B/条），chipAddr `0x0020` | `2`（**A32D32**，8 B/条），chipAddr `0x0024` |
| 串口波特率 | 工具未下发，用 ini 的 `Baudrate=921600` | 工具**显式**下发 `0x0008 = 1500000` |

⇒ **对项目有用的结论**：命令模式用「缓冲大小 + 读模组名是否成功 + 通道位」即可**区分两块板**，
**不依赖 USB-UART 芯片的 VID:PID**。若厂商换代工串口芯片，`radar_source.py` 的指纹表会失效，
但这条旁路仍然可用（`serial_preflight.py probe` 已内置该指纹，**默认干跑、`--send` 才真发**）。

**收发细节（实测，与文档示例有出入）**：工具打开命令模式发的是 `02 00`，
而厂商文档示例写 `01 00`；工具的做法是**三步**——先发一次不解析返回 → 延时 ~100–150 ms → 再发一次并解析。
读帧顺序为**先读 6 B**（magic 4 + `u16LE` 数据长），**再读 `dataLen + 4` B**。

#### 实测结果码（**已落地到** `serial_preflight.py`，10/19）

| 码 | 含义（本机实测语境） |
|---|---|
| `0x0000` | 成功 |
| `0x0001` | 失败 / 不支持（1T2R 对接口类型、序列号、A8D16 等返回它） |
| `0x0FA7`(4007) / `0x0FA8`(4008) | 2T4R 固件专用失败码（读模组名 / 读序列号 / 接口类型 / 通道位） |
| `0xA001`(40961) | 打开命令模式失败 |

**工具侧落地（10/19 已完成，`selftest` 69 项全过）**：
- `serial_preflight.py:describe_result()` 带上述码表，`decode_cmd_reply()` 输出形如
  `失败 0x0FA7(4007) 不支持该命令`；未知码**不吞掉**，仍打印原始十六进制。
- 「打开命令模式」的 payload **默认改为真机实测的 `02 00`**（`MEASURED_OPEN_VER = 0x0002`），
  文档示例口径 `01 00` 保留为 `DOC_OPEN_VER` 并可用 `probe --open-payload 1` 复现。

#### 写寄存器序列：结构已解出，且**条数与声明逐项吻合**

- `0x0012` + param **`0x0100`** = 「写寄存器段信息」= `(段号 u16, 条数 u16)`；
- `0x0001` payload = `chipAddr u16` + N × `(addr, val)`，条目宽度由地址数据格式决定
  （1T2R = 4 B：`addr u16 + val u16`；2T4R = 8 B：`addr u32 + val u32`）；
- 实测（`decode_registers2.py`，按会话切分并以「地址等步长」自校验宽度假设）：

| 板 | 段号 | 段名 | 声明条数 | 实测条数 |
|---|---|---|---|---|
| 1T2R | `0x0001` | Stop | 9 | **9** ✔ |
| 1T2R | `0x0002` | Config | 67 | **67** ✔ |
| 1T2R | `0x0003` | Start | 5 | **5** ✔ |
| 2T4R | `0x0000` | Env Setup | 68 | **68** ✔ |
| 2T4R | `0x0001` | Stop | 6 | **6** ✔ |
| 2T4R | `0x0002` | Config | 155 | **155** ✔ |
| 2T4R | `0x0003` | Start | 9 | **9** ✔ |
| 2T4R | `0x0010` | Mod Calibration | 60 | **60** ✔ |

段号与厂商协议表逐项对应。**这同时回答了「§7.3 的配置数字从哪来」**：上位机就是按这张表
把 `Radar_Config.ini` 翻译成寄存器写的（2T4R 段 `0x0002` 的 155 条里含 `0x6000F0xx` 波形表，
段 `0x0000` 的 68 条含 `0x6000E00xx` 等）。**注意**：1T2R 的 `Config` 是 67 条，
而早前手工按「每帧 12 条 × 6 帧」数会得 69~72 —— 以本次按段累计的 **67** 为准（已与声明数对上）。

#### 地址语义澄清（10/19 顺带复核，bounded，已结案）

固件 `components/config/config.c` 的 `Config_ApplyConfig()`（L2946 起）从 **`0x60008000`** 读一个
**「配置下载块」头**（`REG32(0x60008000)` 的低 16 位 = 条目数 `regNum`），随后从 **`0x60008008`** 起
按 **(addr, value) 对**逐条执行 `REG32(addr) = value`（`0xfffffd` 表示延时）。

⇒ **`0x60008000` 不是寄存器写基址，而是 blob 头**；**被写入的目标地址**才是芯片寄存器，
与厂商工具实测下发的 `0x60000800` / `0x60000804` 一致（`0x60000804` 也确实出现在固件大表的
「被写目标」一列里）。
早前「`0x60008000` 疑为 `0x60000800` 的误读」这一猜测 **不成立**：两者是**语义不同的两个地址**
（后者 = 芯片寄存器窗口，前者 = 固件侧配置 blob 头），**该疑点已作废、从待确认项中移除**。

#### 常量搜索：**负结果，该线索关闭**

在**干净分板**寄存器池（1T2R 648 条 / 唯一 61；2T4R 1192 条 / 唯一 135）里搜索
`299792458`、24.015 / 24.235 / 24.125 / 24.001 / 25.000 / 24.5005 GHz、220 / 999 MHz、
0.6813 / 0.150 m、129500 / 520840 / 8138 µs 的 u16 / u32 / float32（含 10^k 缩比）：
**2T4R 0 命中；1T2R 仅 1 个歧义命中**（段 `0x0002`、`addr 0x43 = 25000`）。
判为**巧合、不作为证据**：25 GHz 是 **2T4R** 的 TopFreq 却出现在 1T2R 表里，
且相邻 `addr 0x42 = 50000` 恰为其 2 倍 ⇒ 更像分频/计数而非绝对频率。
⇒ **`range_res` 仍是「配置推导 + 1T2R 交叉验证」，不是设备寄存器确认**；
卷尺点检仍待做（见 §7.5）。**本线索就此关闭，不再投入**（与该芯片无公开寄存器手册一致）。

#### 证据位置与保密

- 原始字节：厂商工具 `Log\LogFile_2026_10_{15_17_04_50, 18_15_46_58, 19_14_11_10, 19_14_21_57}.txt`
  （`utf-8-sig`、纯英文；`_Send:` / `_Recv:` 行的**下一行**即 hex 转储）；
- 解析产物：`.deepworks/tmp/{extract_protocol_bytes.out, transcripts.out, decode_registers2.out,
  regs_dump_search.out}`，以及完整寄存器表 `regs_1t2r.txt` / `regs_2t4r.txt`
  —— **全部位于 `.gitignore` 覆盖的临时目录，不入库、不外传**（含厂商寄存器明文）。
  要复核请按 §6 复现方法重跑脚本，不要把这些文件复制进仓库。
