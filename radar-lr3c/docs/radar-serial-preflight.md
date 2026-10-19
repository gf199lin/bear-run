# 换板串口预检（2T4R 到手前准备）

> 目的：**在拿到 2T4R 板之前**把「一插上就能判断链路状态」的工具与判定标准准备好。
> 本文只覆盖**链路层**（端口、字节流、封装形态、帧长、RX 数、帧率）。
> **不含**任何动作识别结论，也不替代 P0「2T4R 链路验证」——嗅探到数据 ≠ 协议语义已知。
>
> 现状：2T4R 板**尚未到手**（用户 10/19 告知「待会可以拿到」）⇒ 本文档中标 **【待实机确认】**
> 的内容全部**未执行**，不得当作结果引用。已执行的部分标 **【已执行 10/19】**。

## 1. 为什么不能直接用现有 probe.py / server.py 探新板

`radar-pointcloud/radar_protocol.py` 把 **1T2R 假设写死在代码里**：

| 位置 | 写死的假设 | 若 2T4R 不同会怎样 |
|---|---|---|
| `BINS_PER_CHIRP = 8` | 每 chirp 8 个距离单元 | 解析出错误的 bin 数 |
| `RX0_ID = 0x30` / `RX1_ID = 0xB0` | 只有 **2 个** RX 标识 | 不认识的包**直接返回 `None`** |
| `CHIRPS_PER_FRAME = 64` | 每帧 64 chirp | 组帧永远凑不齐 |
| `FrameAssembler` 固定 `(2, chirps, bins)` | 帧形状 2×64×8 | 形状不匹配 |

后果：换板后 `probe.py` / `server.py` 的表现是「**一个包都认不出**」，而且**不会告诉你为什么**。
所以预热阶段需要一个**零协议假设**的工具先把客观事实量出来，再回来改协议层（改之前必须有实测证据）。

## 2. 工具：`radar-pointcloud/serial_preflight.py`

| 子命令 | 作用 | 是否碰端口 |
|---|---|---|
| `scan` | 枚举串口 + 硬件指纹（VID/PID → 芯片）+ 与项目已知端口对照 | **不打开**，零风险 |
| `sniff --port COMx --baud N --seconds S --out f.bin` | 打开端口读 S 秒，落盘原始字节，并当场做同样统计 | 占用端口（独占） |
| `analyze <file.bin> [--duration S]` | 离线分析已有抓包，可反复跑 | 不碰端口 |
| `selftest` | 用合成流自检统计器本身 | 不碰端口 |

它**不假设**任何协议：只统计魔数出现次数、包长字段直方图、RX/ID 取值集合、帧步长、帧率，
并逐项与 1T2R 的实测事实**并列对照**（只对照，不下判定）。

```powershell
$env:PYTHONIOENCODING='utf-8'
python radar-pointcloud\serial_preflight.py scan
python radar-pointcloud\serial_preflight.py sniff --port COM4 --baud 921600 --seconds 6 --out .deepworks\tmp\2t4r-COM4.bin
python radar-pointcloud\serial_preflight.py analyze .deepworks\tmp\2t4r-COM4.bin
python radar-pointcloud\serial_preflight.py selftest
```

> ⚠ `sniff` 会**独占**端口：跑之前先关掉 `server.py` 与雷达厂商上位机，
> 否则表现为「打开失败」而不是「读不到数据」。首次实机使用前先跑一次 `selftest`。

## 3. 【已执行 10/19】本机预检结论（无需板子即可得到）

| 项 | 结果 | 含义 |
|---|---|---|
| `scan` 注册表枚举 | **COM3** = `USB\VID_10C4&PID_EA60` → Silicon Labs CP210x（**1T2R，与 `radar-uplink-protocol.md` §1 一致**） | 1T2R 板当前在位 |
| `scan` 注册表枚举 | **COM4** = `FTDIBUS\VID_0403+PID_6001+A02C56YMA` → **FTDI FT232**，驱动服务 `FTSER2K` 已安装 | **本机已装好 FTDI VCP 驱动，且已有一个 FTDI 设备挂着**。它**是不是 2T4R 未验证**；注意 AGENTS.md 写的「FTDI 驱动是错的」只对 **1T2R（CP2102）** 成立 |
| `analyze .deepworks/tmp/cap.bin` | 2975 包，`52 字节×2974 + 16 字节×1`，载荷长 `40×2974 + 4×1`，包尾校验 2975/2975 通过，ID 仅 `0x30×1487 / 0xB0×1487` | 工具在**真实 1T2R wrapper 数据**上与 `radar-uplink-protocol.md` 逐项吻合，统计器可信 |
| `analyze .deepworks/tmp/after-replug2.bin` | `ICLH = 0` ⇒ **裸帧模式**；自适应帧步长 **40 字节**，自洽帧 9918 个 | 独立复现了协议文档记录的「抖动后 wrapper 消失、退化为裸帧」现象 |
| `selftest` | **25 项全部通过**（含伪 4RX、裸帧、噪声、异常帧长用例） | 统计器对「多 RX」「非 40 帧长」确实会**报出差异**，不是只会点头 |

### 3.1 三个必须记住的坑（都在本机实测到了）

> 下表引用的 `.deepworks/tmp/**`（`cap.bin`、`after-replug2.bin`、`diag_comports.py`）是
> **临时目录、不进仓库**（已在 `.gitignore` 内）。它们的作用是「本条结论当时怎么来的」，
> 待复采后请把关键 `.bin` 归档到仓库外的手动备份里。

1. **pyserial 枚举在本机失效。** `serial.tools.list_ports.comports()` 恒返回 `0` 个
   （Python 3.14.3 + pyserial 3.5；同时 PowerShell 能看到 COM3/COM4）。
   根因诊断脚本：`.deepworks/tmp/diag_comports.py`（`SetupDiClassGuidsFromName("Ports")` 成功、
   `SetupDiGetClassDevs` 句柄正常，但 Ports 类 `SetupDiEnumDeviceInfo` 枚举到 0 个设备；
   而 `DIGCF_ALLCLASSES|DIGCF_PRESENT` 能枚举到 243 个设备，`Win32_SerialPort` WMI 也是 0 个）。
   ⇒ 影响：`radar_source.list_serial_ports()` / `find_radar_port()` 在本机**不可信**，
   1T2R 至今只靠硬编码 `DEFAULT_PORT='COM3'` 兜底。
   ⇒ 结论：**一律显式 `--port`**，不要依赖自动选口；换板时尤其如此。
   ⇒ 工具已内置绕行：`scan` 直接读注册表
   `HKLM\SYSTEM\CurrentControlSet\Enum\...\Device Parameters\PortName`，本机验证可靠。

2. **载荷里的伪 `0xAA` 会伪造出「更多 RX」。** IQ 数据中出现 `0xAA` 字节是常事。
   对 `after-replug2.bin`，若直接拿「所有 `0xAA` 位置的下一个字节」统计，会得到
   `0x00×105, 0xF0×104, 0x60×4, 0x10×3, 0x20×3, 0xD0×3` 等假值 ——
   只看这堆数字会**误判成 4RX 甚至 8RX**。工具的做法是先按「等间隔 + 首 `0xAA` 尾 `0x55`」
   筛出自洽帧，再统计，结果干净收敛为 `0x30 / 0xB0`。
   ⇒ 这正是本项目在 2T4R 上最容易踩的坑：**必须用筛噪后的 `★ 自洽帧 ID 高半字节` 那一行判 RX 数。**

3. **2T4R 的一切都不得沿用 1T2R 的值。** 端口（可能不是 COM3）、波特率（未必 921600）、
   封装（可能没有 ICLH/ICLT）、帧长（未必 40）、RX 数（未必 2）、帧周期（未必 129.5 ms）——
   全部**未验证**。

## 4. 【待实机确认】拿到 2T4R 后的操作顺序

> 每一步都给了**判定标准**。任何一步「不通过」都不要改代码，先记录原始字节。

| # | 动作 | 通过标准 | 不通过时 |
|---|---|---|---|
| 1 | 插上板子，`scan` | 新出现一个 COM 口，VID/PID 能映射到芯片名 | 没有新口 ⇒ 查 USB 线/驱动（FTDI 需 `FTSER2K`；本机已装） |
| 2 | `sniff --port <新口> --baud 921600 --seconds 6 --out ...` | 收到**非零**字节且有稳定速率 | 0 字节 ⇒ 板子未上电/未出流，或波特率错 ⇒ 见第 3 步 |
| 3 | 看统计的**形态** | 落到下面判定表里的任一行 | 收到大量字节但**毫无结构**（无 ICLH 也无 0xAA 步长）⇒ **波特率可能错**，换 `115200 / 230400 / 460800 / 921600 / 1500000 / 2000000` 重试（启发式，不是结论） |
| 4 | 记录 `★ 自洽帧 ID 高半字节` 与 `自适应帧步长` | 得到干净、稳定的取值集合 | 集合里出现**多个非 0x30/0xB0** 的值 ⇒ 可能的 4RX 证据 ⇒ **保留原始 .bin**，不要凭一次运行下结论 |
| 5 | 重复 `sniff` **≥3 次**（不同时刻/不同站位） | 帧长、ID 集合、帧率在多次之间**稳定** | 不稳定 ⇒ 说明是噪声或模式切换，不能作为参数依据 |
| 6 | 把结果写进 `docs/radar-uplink-protocol.md`（新开 2T4R 一节） | 每条都有 .bin 来源 + 运行次数 | —— |

### 4.1 形态判定表

| 观察到的形态 | 最可能是什么 | 下一步 |
|---|---|---|
| 有 `ICLH/ICLT`，包长 52/载荷 40，ID 仅 `0x30/0xB0` | **与 1T2R 完全同封装同形状** | 仍要核对帧率与 RX 数；可能只多了 TX 通道不影响上行 |
| 有 `ICLH/ICLT`，但包长/载荷 ≠ 52/40 | 同封装、帧结构不同（chirp 数或 bin 数变了） | 用 `载荷长直方图` 反推每 chirp 字节数 |
| 无 `ICLH/ICLT`，但 `0xAA` 可自适应出稳定步长 | **裸帧模式**（1T2R 抖动后也见过） | 用步长当帧长，再按自洽帧统计帧率 |
| `★ 自洽帧 ID 高半字节` 有 4 个值（如 `0x30/0x70/0xB0/0xF0`） | **4RX 的直接证据** | 保留 .bin，考虑把协议层 RX 表参数化 |
| 包速率 ⇒ 推算帧周期明显偏离 129.5 ms | 每帧包数 ≠ 128（RX 数或 chirp 数不同） | 用「包速率 / 帧率」反推每帧包数 |
| 收到字节但完全没有结构 | 波特率错 / 非本项目雷达的数据 | 换波特率；确认插的是雷达 |

## 5. 【待实机确认】为什么这些结论直接影响跳/蹲路径（T2 决策）

跳/蹲的候选路径之一是 **2T4R 俯仰角测高**。它的前提是**链路能拿到 4 通道原始数据**。
所以本文第 4 步的产出（`★ 自洽帧 ID 高半字节` 到底几个值）是 **T2 决策的第一手输入**，
不是可选项。若第 4 步只看到 2 个值，说明上行根本没带 4 通道 ⇒ 俯仰路径需要另想办法
（或该板固件未开放），必须在 T2 决策记录里写明。

## 6. 未验证 / 不要当结论

- 2T4R 的端口、波特率、封装、帧长、RX 数、帧周期：**全部未知**。
- COM4 那个 FTDI 设备**是不是 2T4R**：**未知**（只能确认它是 FT232 且驱动已装）。
- 本工具的 `sniff` 子命令**从未在真实端口上跑过**（只在合成流与已有 .bin 上验证过），
  首次实机运行属于「首次使用」，需记录实际表现。
- `sniff` 的波特率候选清单是**启发式**，不是枚举验证。
- 嗅探到数据**不等于**协议语义已知：字段含义仍需按 `radar-uplink-protocol.md` 的流程逐项验证。
- 本文**不含**任何动作识别（左/中/右、跳/蹲）结论。
