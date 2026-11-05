# 雷达硬件停机方案（实验 · 连通性已实机验证，**停机本身未验证**）

> 状态：**计划 + 工具 + 计划文件就绪（dry-run 通过）**；**T1 命令模式连通性已于
> 2026-10-15 在 COM3 实机通过（9/9，见 §7.1，未写任何寄存器）**；
> **T2（写停机寄存器、真正降温）仍未执行**。
> 按 AGENTS.md 证据纪律，本文不得被引用为「已实现降温功能」。
> 对应代码：`radar-pointcloud/radar_hwstop.py`（默认 dry-run）。

---

## 1. 为什么要它

软件层的「结束采集」只释放串口、停止解算/渲染（见 `radar-control-and-actions.md` §2），
**雷达芯片仍在发射 FMCW，温度不降**。用户明确反馈「雷达一直开着发热严重」，
所以要真正降温，只能让**芯片停下来**，即写固件里的停机寄存器表。

## 2. 为什么「理论上」可行：固件里确实有这条路

依据实读固件源码（`DEV202H` SDK）：

| 事实 | 来源 |
|---|---|
| 开机流程本身就是 Stop → Start | `App/common/src/main.c` L35 / L37（`Radar_Init_Stop_BanyanE()` → `Radar_Init_Start_BanyanE()`） |
| 停机寄存器表是固件**明文数组** | `Middleware/common/src/banyan.c` L49 `InitChipRegListStop_BanyanE[9]`（8 条有效 + 1 条 `{0x00,0x0000}` 终止符；循环条件 `while(addr)`，故终止符**不写**） |
| **停机序列其实有两步** | 同文件 L403 `Radar_Init_Stop_BanyanE()`：先写 STOP 表 → **再写 `InitChipRegListConfig0`**（`Middleware/common/src/radar_para.c` L2，67 条）到同一设备 |
| 启动寄存器表 | `Middleware/common/src/radar_para.c` L73 `InitChipRegListStart0`（5 条，终止符不写）；`banyan.c` L437 `Radar_Init_Start_BanyanE()` **只写这一张** |
| 本构建是**单芯片** | `banyan.c` 里 `TWO_CHIP` 只出现在 `#ifdef` 中、**全树从未定义**；且 `InitChipRegListStart1` / `InitChipRegListConfig1` 只有引用、**没有任何定义**（若真定义 TWO_CHIP 反而编不过）→ `IIC_BANYAN_ADDR1(0x42)` 那半段不参与编译 |
| 下行命令帧格式/命令码 | `App/common/src/cmdprocess.c`、`inc/cmdprocess.h` |

**推论**：既然开机时固件自己就会写这套寄存器，那么从串口用同样的命令帧写同样的表，
就能让芯片进入与「开机前」相同的停止状态；要恢复，再写启动表即可。

## 3. 下行命令协议要点（工具已按此实现）

- 帧：`FD FC FB FA` + LEN(u16LE) + `[cmdType(u16LE) + cmdData]` + `04 03 02 01`。
- `START_CFG_CMD=0x00FF` 进入命令模式 → 写寄存器 → `FINISH_CFG_CMD=0x00FE` 退出。
- `WRITE_REG_CMD=0x0001`；`cmdData` = `设备地址(u16LE)` + N×`(addr u16LE, val u16LE)`。
- **设备地址：下行填 7 位 `0x20`**（已三处独立确认）：
  - `Middleware/common/inc/banyan.h` L61 `#define IIC_BANYAN_ADDR (0x40)`、L62 `IIC_BANYAN_ADDR1 (0x42)`；
  - `Middleware/platform/DEV202H/src/RadarConfigInterface.c` L12/L13 `chipNum = 1; chipAddr[4] = {0x20};`
    （`0x20` 就是 7 位形式；该文件 L105 把它当 8 位地址传给 `HAL_I2C_Mem_Write`，所以
    `IIC_BANYAN_ADDR = 0x40 = 0x20 << 1` 是同一个地址的两种写法）；
  - `cmdprocess.c` L235 `devAddress = cmd->cmdData[0] << 1;` 再与 `IIC_BANYAN_ADDR` 比较 → `cmdData[0]` 必须是 `0x20`。
- 寄存器地址在 I2C 上是 **8 位**（`I2C_MEMADD_SIZE_8BIT`），值传输时转**大端**
  （`RadarConfigInterface.c` L102 `LE16_TO_BIG`）；工具按 u16LE 拼帧，与固件解析一致。
- 单帧寄存器条数上限 `(54-2-2)/4 = 12` → 大表自动分帧（工具已做）。
- ACK：`cmdType = 原命令 | 0x0100`，`status` u16LE（`0=ACK_OK`，`1=ACK_FAIL`）。
- 工具内部的 `FirmwareChecker` 是**固件解析状态机的离线复刻**，用来证明「拼出来的帧，
  固件那条状态机能原样解析出来」。`selftest.py` 测试 5 的 8 项就是这件事——
  **它只证明帧构造正确，不证明实机能停机。**

## 4. 工具用法

```bash
# (a) 默认：只 dry-run，打印每一帧的字节与解析结果，不碰串口
python radar_hwstop.py

# (b) 连通性试探：只发「进入/退出命令模式」，不写任何寄存器（最安全的第一步）
python radar_hwstop.py --both-directions --send --confirm

# (c) 简单模式：单设备直接写若干寄存器（addr/val 支持 0x 前缀）
#     注意 --dev 是 7 位地址，本板为 0x20，不是 9
python radar_hwstop.py --dev 0x20 --reg 0x40=0x0000 --reg 0x41=0 --send --confirm

# (d) 按计划文件（推荐：寄存器值放仓库外）
python radar_hwstop.py --plan "%USERPROFILE%\.deepworks\radar-hwstop-t2a-stop-only.json"
```

- **默认 dry-run**；`--send` 且必须再加 `--confirm`，否则拒绝执行（防误触）。
- 退出码：`0` 成功/仅 dry-run，`2` 未找到计划文件，`3` 缺 `--confirm`，`4` 有帧未收到 `ACK_OK`。

## 5. 计划文件格式（放仓库外，勿提交）

默认路径 `%USERPROFILE%\.deepworks\radar-hwstop-plan.json`：

```json
{
  "note": "寄存器值来自厂商资料，不要提交进 git",
  "steps": [
    { "dev": 32, "label": "STOP 停机表", "regs": [[64, 0], [65, 0]] }
  ]
}
```

- `dev`：**7 位**设备地址（本板 `0x20` = 十进制 `32`）；`regs`：`[寄存器地址, 值]`，
  超过 12 条自动分帧；工具会自动在最前加 `0x00FF`、最后加 `0x00FE`。

### 5.1 已生成的三份计划文件（数值来自固件源码抽取，**不在仓库内**）

生成方式：`.deepworks/tmp/gen_hwstop_plans.py`（从 `banyan.c` / `radar_para.c`
正则抽取寄存器表，并**剔除末条 `{0x00,0x0000}` 终止符**——否则会真的往寄存器 0 写值）。

| 文件（`%USERPROFILE%\.deepworks\`） | 内容 | 寄存器/帧 | dry-run |
|---|---|---|---|
| `radar-hwstop-t2a-stop-only.json` | 仅 STOP 表 8 条 | 8 → 1 帧 | 通过 |
| `radar-hwstop-t2b-full-stop.json` | STOP 表 8 条 + Config0 67 条（固件 `Radar_Init_Stop_BanyanE` 全序列） | 75 → 7 帧 | 通过 |
| `radar-hwstop-start.json` | 恢复用 Start0 表 5 条 | 5 → 1 帧 | 通过 |

`sha256` 前缀：`4afc628b…`(t2a) / `7a642332…`(t2b) / `ec24fe2d…`(start)。

dry-run 帧长实测：`2 / 36 / 52 / 24` 字节，均在 `CMD_LEN_MAX=64` 上限内（数据域 ≤54）。

## 6. 保密要求（AGENTS.md L36–L40）

- **停机寄存器明文数值不写进本仓库**，只存在于仓库外的计划 JSON。
- 厂商工具配置（`Radar_Config.ini` 等）、SDK 源码、`.lic` 均为厂商资料，**不外传、不复制进公开仓库**。
- dry-run 的字节输出**不要整段贴进聊天/文档**（等于泄露寄存器值），需要核对时只贴
  `dev` / `regNum` / 帧长等结构性字段。

## 7. 实机验证步骤与门槛

**前置条件**：COM3 空闲（先停掉 `server.py`，确认没有第二个实例）、
**能重新插拔 USB 兜底**（一旦把芯片写成哑状态，插拔上电通常可恢复）、
**手上能测温**（否则「降温」无法证伪）。进入命令模式期间上行数据流会受影响，
所以 T1/T2 期间不要同时跑采集。

> **COM3 现已空闲**（此前被 PID 13680 的旧实例长期占用，已结束；`check_port.py` → `PORT_FREE_OK COM3 921600`）。
> `check_hw_control.py` 31/31 真机通过：开始/结束/重置在真实串口上可用，且「结束」确实释放串口。
> 因此本页的 T1/T2 现在随时可以做——但 T2 写寄存器前仍须再单独确认。

| 阶段 | 做什么 | 通过判据 |
|---|---|---|
| **T0 离线** | `python radar_hwstop.py --plan …`（dry-run）+ `selftest.py` 测试 5 | 帧字节与固件解析规则一致 ✅ 已通过（含 3 份计划文件） |
| **T1 连通性** | `run_t1_hwstop.py`（本项目实际用法；只发 0x00FF/0x00FE 两帧） | 收到 `ACK_OK`；随后正常采集仍能出帧（证明没搞坏设备）→ ✅ **2026-10-15 实机通过，见 §7.1** |
| **T2a 停机（最小）** | 只发 STOP 表（8 条） | ① 数据流停止（无新帧）；② **温度实测下降**（记录前后数值与时间） |
| **T2b 停机（对齐固件）** | STOP 表 + Config0（7 帧） | 同上；用于判断「只写 STOP 表」是否够——两者语义差异**尚未确证** |
| **T2 恢复** | 发 `radar-hwstop-start.json`（或重新上电） | 数据流恢复、帧率回到 **7.7 fps** |

**T2 未通过之前，页面上不出现「深度停机」按钮**（服务端 `cfg.hw_stop` 固定为 `False`，
即该功能对前端保持关闭，避免误用）。

### 7.1 T1 实测结果（2026-10-15，COM3，未写任何寄存器）

执行器：`.deepworks/tmp/run_t1_hwstop.py`（为什么不用 `radar_hwstop.py --both-directions
--send`：后者每帧只 `read(16)` 一次，回来多半只是普通上行数据 → 会给出**假阴性**）。
报告：`.deepworks/tmp/t1-hwstop-report.txt`。结果 **9 项全 PASS、FAIL=0**。

实测数据：

| 观测 | 值 |
|---|---|
| 发送前基线 | **40960 B/s、裸 40 B 帧 1023 条/s**（≈7.99 fps，与 129.5 ms 帧周期吻合） |
| 发 `0x00FF`（`fd fc fb fa 02 00 ff 00 04 03 02 01`）后 1.5 s | 窗口仅 **464 B**（≈停流）→ 命令模式**确实进去了** |
| `0x00FF` 应答 | `type=0x01FF status=1 len=4` → **ACK_OK**（见下「老约定」） |
| 发 `0x00FE`（`fd fc fb fa 02 00 fe 00 04 03 02 01`）后 1.5 s | 窗口 **65536 B**（流已回来） |
| `0x00FE` 应答 | `type=0x01FE status=1 len=4` → **ACK_OK** → 命令模式**出得来** |
| 收尾 2 s | **40960 B/s、裸 40 B 帧 1024 条/s**（= 基线）→ **数据流完全恢复，设备没被搞坏** |

**三个必须记住的发现：**

1. **应答状态是「老约定：1 = ACK_OK、0 = ACK_FAIL」**。固件
   `cmdprocess.c` L122-143 + `cmdprocess.h` L122-126：`masterProtocolVer == 0` 时
   `FillCmdAck` 会把状态**反转**成 `ACK_OK_OLD=1 / ACK_FAIL_OLD=0`；
   而 L75 `static uint16_t masterProtocolVer = 0;` 是启动默认值。
   我们第一次实测把 `status=1` 判成 FAIL，是**判读错**，不是设备拒绝。
   → `radar_hwstop.py` 的 `parse_ack()` 已修正（`ACK_OK_OLD/ACK_FAIL_OLD` 常量 +
   `master_protocol_ver` 参数，默认老约定），`selftest.py` 测试 5 加了
   「老约定 1=OK / 老约定 0=FAIL / 新约定可切换」三条断言。
2. **`masterProtocolVer` 由 `0x00FF` 帧自己的数据域首字节设定**（`cmdprocess.c` L653-662）：
   不带数据（我们现在的做法）→ 固件走 else 分支**显式置 0**（老约定）；
   若带 `data[0] = 非 0` → 切**新约定**（0=OK、1=FAIL），且该帧应答会**带回 boardInfomation**
   （`{RADAR_PROTOCOL_VERSION, CMD_LEN_MAX}`，len = 4+4 = 8）。
   → 这给了一个**可选的后续探针**（能顺带读出固件的 `RADAR_PROTOCOL_VERSION` 与
   `CMD_LEN_MAX=64`）；但只要带版本数据，**后续 ACK 就必须按新约定判读**，T2 提交前要留意。
3. **`DoWriteReg` 的应答是「写后回读比对」**（`cmdprocess.c` L233-256：写 → `HAL_Delay(10)`
   → `RadarConifg_Read` → 比对不等则 `ACK_FAIL`），所以 T2 拿到 `ACK_OK` 意味着
   **雷达真的吃下了该寄存器值**，比「命令收到了」强得多。
   同时该函数印证了我们的帧布局：`CMD_DEV_ADDR_LEN=2`、`sizeof(CMD_REG_T)=4`、
   `devAddress = cmd->cmdData[0] << 1`（L235）与 `regNum=(cmdLen-2-2)/4`（L236）都对得上。

附带确认：真机上行是**裸 40 B 帧、没有 ICLH 封装**（40 B × 2 rx ≈ 83 B/chirp × 64 chirp
≈ 5.3 KB/帧 × 7.7 fps ≈ 40960 B/s）——这正是 `radar_protocol.StreamParser`
双格式自适应在真机上的必要性证据。

## 8. 未确证项与风险

- ~~`IIC_BANYAN_ADDR` 7 位原值未确证~~ → **已确证为 `0x20`**（见 §3 的三处独立来源）。
  遗留小项：`InitChipRegListConfig0` 与厂商 `Radar_Config.ini` 的
  `[CHIPCONFIGREGISTER0]` 是否逐条一致，仍未核对。
- **~~应答状态约定未确证~~ → 已确证为「老约定」**（`masterProtocolVer == 0` → 1=OK、0=FAIL），
  2026-10-15 T1 实机验证；判读代码已修正（见 §7.1 发现 1）。**T2 提交前务必确认当时的约定**
  （只有 `0x00FF` 带非 0 版本数据时才是新约定）。
- **T2a 与 T2b 谁才等价于「固件意义的停机」未知**：固件序列是「STOP 表 → Config0」，
  只写 STOP 表会发生什么，没有依据可推断（既可能停得更彻底，也可能停不干净）。
  首次实机建议从 **T2a** 开始，用「数据流是否停止 + 温度」判断，再做 T2b 对比。
- **停机表未与厂商工具核对**：厂商 `Radar_Config.ini` 里的 `Encode`+base64 混淆未破解，
  无法把两者逐条对照，只能先信固件明文数组。
- **恢复风险**：若停机后 START 表写失败，需**重新插拔 USB / 断电上电**。
- **多实例抢串口**：`docs/progress-radar-pointcloud.md` 问题 C 所述端口争抢未彻底确认，
  实机验证时务必保证只有一个进程（本轮已实测：端口被占时打开会直接
  `PermissionError(13, '拒绝访问')`）。
