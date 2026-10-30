# 纯 Python 启动 2T4R（脱离 ONELAB）

> 状态：**实机验证通过**。2026-10-30 在本机 COM4 实测出流 66.7 kB/s、
> 1505 帧/3 s、坏包 0、坏校验 0，与历史成功样本（66.5 kB/s）一致。
> 日期：2026-10-30
> 相关：`docs/radar-uplink-dev113p-2t4r.md`（协议）、`docs/dual-board-interference.md` §10.1（待机特性）

---

## 1. 要解决的问题

2T4R（DEV113P）与 1T2R 有一个根本差异：

| | 1T2R (DEV202H) | 2T4R (DEV113P) |
|---|---|---|
| 上电行为 | **自跑**，插上就出数 | **待机等命令**，插上 0 字节 |
| 唤醒方式 | 不需要 | 需要一次完整的「握手 + 写段配置」 |

历史上唯一的唤醒方式是 **ONELAB 唤醒一次**，唤醒状态跨进程保持、直到断电为止
（实测见 `docs/dual-board-interference.md` §10.1）。

2026-10-30 起 ONELAB 被 Windows **Smart App Control** 拦截（未签名 DLL，
错误 `0xc0e90002`），软件打不开，这条路暂时断了。所以改为自己做启动。

## 2. 方案：原样重放厂商日志里的启动帧

不去重新构造寄存器帧，而是**把厂商那次成功唤醒的下行帧逐字节提取出来重放**。

理由：2T4R 用 **A32D32**（`addr u32 + val u32`，8 B/条），单帧最长 510 B，
远超 1T2R 工具里 `CMD_BUF_LEN = 54` 的假设。自己打包很容易在
「帧边界 / 长度字段 / 字节序」上出错，而重放天然没有这些风险。

## 3. 关键协议事实（均有实测出处）

| 事实 | 值 | 出处 |
|---|---|---|
| 帧封装 | `FD FC FB FA` + `u16LE len` + `u16LE cmd` + payload + `04 03 02 01` | `radar_hwstop.py` 帧构造，1T2R T1 实机 9/9 |
| 2T4R 写寄存器 | `cmd=0x0001`，payload = `chipAddr u16` + N×(`addr u32`,`val u32`) | `.deepworks/tmp/decode_registers.py` + 本次读回校验 |
| chipAddr | `0x0024` | 厂商日志 16 帧 WRITE_REG 全部一致 |
| 段声明 | `cmd=0x0012`，payload = `param u16` + `段号 u16` + `条数 u16` | 同上，30 次实测 |
| 五段条数 | Env 68 / ModCal 60 / Stop 6 / Config 155 / Start 9 = **298** | 厂商日志逐帧对账 |
| **回包格式** | **32 位字对齐**（每字段 4 B LE）；另有 8 位紧凑形态 | 本次实测 `READ_VERSION` 返回 `"VA1.0.0"` |
| **ACK 语义** | **不统一**：OPEN 返 1=OK，CLOSE 返 0=OK | 本次实测 |

**寄存器总量对账（离线 + 读回双重确认）**：

```
Env    68 = 62 + 6
ModCal 60 = 60
Stop    6 = 6
Config155 = 62 + 62 + 31
Start   9 = 9
------------------------
合计  298 = 8 帧 WRITE_REG
```

读回验证：写 `0x60000800 = 0x00000BA6`，READ_REG 读回 `0x00000BA6`，一致。

## 4. **关键发现：必须先握手，再配置**

这是本次最容易踩的坑，也是最初失败的原因。

一次完整启动其实是**两段**：

```
① 握手查询（6 帧）
   OPEN → 0x0016 → READ_VERSION → 0x0011 → READ_MODULE → CLOSE

② 写段配置（31 帧）
   OPEN → 清段/特殊段 → Env(62+6) → ModCal(60) → Stop(6)
        → Config(62+62+31) → 清 Env → Start(9) → CLOSE
```

**只重放②时**：寄存器读回正确、ACK 全部返回、但芯片**始终不出流**（0 字节）。
**补上①之后**：立刻出流 66.7 kB/s。

> 所以：**握手不是可选的"探测"，它是启动流程的一部分。**

## 5. 实现

| 文件 | 作用 | 是否入库 |
|---|---|---|
| `radar-pointcloud/tools/extract_vendor_boot.py` | 从 ONELAB 日志按协议语义定位「握手+配置」，输出原始帧 | 可入库（只读日志，不含寄存器数据） |
| `radar-pointcloud/radar_boot_2t4r.py` | 重放启动序列，可选验证出流 | 可入库 |
| `.deepworks/tmp/boot_seq_2t4r.json` | 提取出的 37 帧原始字节 | **不入库**（含厂商寄存器配置） |

识别规则不靠硬编码行号：

```
找到 SYS_PARAM(sec=0x0003, cnt=9)     ← Start 段声明
  → 向前找最近的 0x00FF               ← 配置段 OPEN
  → 再向前若紧邻 0x00FE               ← 说明前面还有握手块，纳入其 OPEN
  → 向后找最近的 0x00FE               ← 配置段 CLOSE
```

## 6. 验证结果（2026-10-30 实机）

| 项目 | 结果 |
|---|---|
| 帧结构校验 | **37/37 通过** |
| 寄存器条数与厂商日志一致 | **298 条，逐段对账一致** |
| 寄存器写入是否生效 | **读回一致**（0x60000800 → 0x00000BA6） |
| **本机 2T4R 重放出流** | **✅ 通过：66.7 kB/s，1505 帧/3 s，坏包 0，坏校验 0** |
| 与历史成功样本对比 | 66.5 kB/s（10/19 的 `2t4r-COM4-b1500000-8s.bin`），**吻合** |
| 命令通道 | `READ_VERSION` 返回 `"VA1.0.0"`，`READ_MODULE` 返回 `0x0FA7`(DEV113P) |

复现命令：

```bash
python radar-pointcloud/radar_boot_2t4r.py --send --port COM4 --verify
```

## 7. 使用方法

```bash
# 1) 从 ONELAB 日志生成启动序列（换电脑后重跑一次）
python radar-pointcloud/tools/extract_vendor_boot.py --logdir "D:\...\ONELAB毫米波传感器开发工具\Log"

# 2) 离线校验（不碰串口）
python radar-pointcloud/radar_boot_2t4r.py --dry-run

# 3) 真机启动
python radar-pointcloud/radar_boot_2t4r.py --send --port COM4

# 4) 启动 + 验证出流
python radar-pointcloud/radar_boot_2t4r.py --send --port COM4 --verify

# 5) 只验证当前是否在出流（启动后可单独跑）
python radar-pointcloud/radar_boot_2t4r.py --verify --port COM4
```

## 8. 关于 ACK 的说明（别被它误导）

工具里 ACK 只做**中性显示**（只报「收到多少字节」），不做 OK/FAIL 判断，因为：

- 回包有 **8 位紧凑** 和 **32 位字对齐** 两种形态；
- 不同命令的 data 字段含义不同：`OPEN` 返 `1` 是 OK，`CLOSE` 返 `0` 也是 OK；
- `READ_MODULE` 返回的 `0x0FA7` 是**模组 ID**，早期按字节流解析时被误读成 `status=4007`。

**唯一可信的判据是 `--verify` 的出流结果。**

## 9. 风险与回退

- **风险 1**：寄存器序列若与本机固件版本不匹配，可能启动失败或行为异常。
  回退：拔插 USB 复位；或仍用 ONELAB（虚拟机）唤醒一次。
- **风险 2**：序列文件含厂商寄存器配置，**不得提交、不得外传**。
  `.deepworks/tmp/` 已在 `.gitignore` 内。
- **风险 3**：本工具只负责「让芯片开始出流」，不改变雷达参数设计；
  写寄存器不会写入任何本地配置文件。

## 10. 已知限制

- 序列来自 **1T2R/2T4R 同版本固件**（VA1.0.0）的厂商日志。固件升级后需重新提取。
- 波特率固定 `1500000`（与历史样本一致）。其它波特率实测均为 0 字节。
- 启动后若**断电或拔插 USB**，需要重新跑一次 `--send`。

## 11. 接入服务：插上就自动出数据

`radar_source.SerialSource` 已接入自动启动，`server.py` 无需任何额外操作。

### 11.1 工作方式

`SerialSource._try_open()` 成功后会调 `_maybe_boot()`，分三步：

```
1) 板型不是 2t4r        → 直接返回（1T2R 上电自跑，不需要干预）
2) _already_streaming() → 开 0.35 s，收到 ≥512 B 就认为"已在出流"，跳过启动
3) 否则 _boot_2t4r()    → 重放 37 帧启动序列（约 2.6 s）
```

「已在出流就不动」这个判断很关键：否则每次重连都会重置一次芯片，
把正常输出打断。 2T4R 满载约 66 kB/s，0.35 s 能收 2 万字节，
512 B 的门槛误判概率极低；而待机的芯片是严格 0 字节。

### 11.2 自动重连也覆盖了

`read()` 里的重连分支走的是**同一个** `_try_open()`，所以：

- 插拔 USB、换口、驱动瞬时失效 → 重连后自动重新判断并必要时唤醒
- 不需要重启 `server.py`

### 11.3 配置项

| 参数 | 默认 | 说明 |
|---|---|---|
| `--no-boot-2t4r` | 开（即自动启动生效） | 加上则不自动启动，交给人工/ONELAB |
| `--boot-seq` | `radar_boot_2t4r.DEFAULT_SEQ` | 自定义序列文件路径 |

### 11.4 实测证据（2026-10-30）

**场景 A：芯片已在出流时启动服务**

```
[serial] 识别到板型 2t4r（DEV113P 2T4R）→ 波特率 1500000
[boot] 2T4R 已在出流，跳过启动序列
[  3.22s] frames=5  fps=1.55 pts=23 ...
```

**场景 B：先把芯片停到待机，再启动服务**

制造待机：发 `SYS_PARAM Stop(0x0001)`，确认之后 1.2 s 内收到 **0 字节**。

```
[serial] 识别到板型 2t4r（DEV113P 2T4R）→ 波特率 1500000
[boot] 2T4R 未出流 → 重放启动序列（37 帧 / 2962 B）
[boot] 启动完成：发=37 回包=37 无回包=0 用时=2.62s
[  3.94s] frames=1  fps=0.25 ...
[ 18.55s] frames=29 fps=1.56 pts=15 ...
```

**结论：「插上 → 识别 → 自动唤醒 → 出流 → 推流」全链路无人工干预。**

## 12. 已知不足与后续

1. 启动序列依赖固件版本（VA1.0.0）；固件升级后需重新提取。
2. `_already_streaming()` 会丢弃开头的一小段数据（≤几 KB），
   对接入时的帧序影响可忽略。
3. 2T4R 的 jump/crouch 俯仰链路验证（T1/T2 决策门）仍按原计划推进。
