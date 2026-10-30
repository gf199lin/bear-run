# 纯 Python 启动 2T4R（脱离 ONELAB）

> 状态：**代码完成、离线校验 31/31 通过；实机出流尚未验证**（板子未连接）。
> 日期：2026-10-30
> 相关：`radar-service/radar_protocol_dev113p.py`（协议实现）、`docs/radar-no-data-triage.md`（无数据排查）

---

## 1. 要解决的问题

2T4R（DEV113P）与 1T2R 有一个根本差异：

| | 1T2R (DEV202H) | 2T4R (DEV113P) |
|---|---|---|
| 上电行为 | **自跑**，插上就出数 | **待机等命令**，插上 0 字节 |
| 唤醒方式 | 不需要 | 需要写五段寄存器才出流 |

历史上唯一的唤醒方式是 **ONELAB 唤醒一次**，唤醒状态跨进程保持、直到断电为止
（2T4R 待机特性的实测记录见 `docs/radar-no-data-triage.md`）。

2026-10-30 起，ONELAB 被 Windows **Smart App Control** 拦截（未签名 DLL，
错误 `0xc0e90002`），软件打不开，这条路暂时断了。所以要自己做启动。

## 2. 方案：原样重放厂商日志里的启动帧

不去重新构造寄存器帧，而是**把厂商那次唤醒的下行帧逐字节提取出来重放**。

理由：2T4R 用 **A32D32**（`addr u32 + val u32`，8 B/条），单帧最长 510 B，
远超 1T2R 工具里 `CMD_BUF_LEN = 54` 的假设。自己打包很容易在
「帧边界 / 长度字段 / 字节序」上出错，而重放天然没有这些风险。

## 3. 关键协议事实（均有出处）

| 事实 | 值 | 出处 |
|---|---|---|
| 帧封装 | `FD FC FB FA` + `u16LE len` + `u16LE cmd` + payload + `04 03 02 01` | `radar_hwstop.py` 帧构造，T1 实机 9/9 |
| 2T4R 写寄存器 | `cmd=0x0001`，payload = `chipAddr u16` + N×(`addr u32`,`val u32`) | 厂商日志解析记录（本地留存，不入库）+ 实测解析 |
| chipAddr | `0x0024` | 厂商日志实测，16 帧 WRITE_REG 全部一致 |
| 段声明 | `cmd=0x0012`，payload = `param u16` + `段号 u16` + `条数 u16` | 同上，30 次实测 |
| 五段条数 | Env 68 / ModCal 60 / Stop 6 / Config 155 / Start 9 = **298** | 厂商日志逐帧对账 |

**寄存器总量对账（离线，已通过）**：

```
Env    68 = 62 + 6
ModCal 60 = 60
Stop    6 = 6
Config155 = 62 + 62 + 31
Start   9 = 9
------------------------
合计  298 = 8 帧 WRITE_REG
```

## 4. 实现

| 文件 | 作用 | 是否入库 |
|---|---|---|
| `radar-service/tools/extract_vendor_boot.py` | 从 ONELAB 日志按协议语义定位一次完整启动，输出原始帧 | 可入库（只读日志，不含寄存器数据） |
| `radar-service/radar_boot_2t4r.py` | 重放启动序列，逐帧等 ACK，可选验证出流 | 可入库 |
| `.deepworks/tmp/boot_seq_2t4r.json` | 提取出的 31 帧原始字节 | **不入库**（含厂商寄存器配置） |

识别启动块不靠硬编码行号，靠语义：

```
找到 SYS_PARAM(sec=0x0003, cnt=9)   ← Start 段声明
  → 向前找最近的 0x00FF              ← 打开命令模式
  → 向后找最近的 0x00FE              ← 关闭命令模式
取这一段 = 一次完整启动
```

## 5. 启动序列结构（31 帧 / 2882 B）

```
 0  OPEN_CMD_MODE
 1  清 Stop 段 (cnt=0)
 2  特殊段 0x2624 cnt=10021
 3  cmd=0x0016
 4  特殊段 0xE360 cnt=22
 5  cmd=0x0014
 6~11  清 Stop/Config/Env/Env/Config/Clear 段 (cnt=0)
12  cmd=0x0020
13  读版本 (0x0000)
14  cmd=0x0011
15  读模组 (0x0005)
16  声明 Env 68  → 17 (62 条) + 18 (6 条)
19  声明 ModCal 60 → 20 (60 条)
21  声明 Stop 6   → 22 (6 条)
23  声明 Config 155 → 24 (62) + 25 (62) + 26 (31)
27  清 Env (cnt=0)
28  声明 Start 9 → 29 (9 条)
30  CLOSE_CMD_MODE
```

## 6. 验证状态（证据纪律）

| 项目 | 状态 | 证据 |
|---|---|---|
| 帧结构 31/31 合格 | ✅ 已完成 | `--dry-run` 输出 |
| 寄存器条数与厂商日志一致（298） | ✅ 已完成 | 段声明 vs 写帧逐项对账 |
| 段顺序与厂商一致 | ✅ 已完成 | 日志两次相同配置（帧 71–101 / 102–132）互证 |
| **本机 2T4R 重放后真的出流** | ❌ **未验证** | 板子未连接 |
| ACK 是否逐帧返回 | ❌ **未验证** | 同上 |

> 上面两项未验证的，**不得**写进「已实现/可用」结论。

## 7. 使用方法

```bash
# 1) 从 ONELAB 日志生成启动序列（只需一次，换电脑后重跑）
python radar-service/tools/extract_vendor_boot.py --logdir "D:\...\ONELAB毫米波传感器开发工具\Log"

# 2) 离线校验（不碰串口）
python radar-service/radar_boot_2t4r.py --dry-run

# 3) 真机启动
python radar-service/radar_boot_2t4r.py --send --port COM4

# 4) 启动 + 验证出流
python radar-service/radar_boot_2t4r.py --send --port COM4 --verify
```

## 8. 风险与回退

- **风险 1**：寄存器序列若与本机固件版本不匹配，可能启动失败或行为异常。
  回退：拔插 USB 即可复位；或仍用 ONELAB（虚拟机）唤醒一次。
- **风险 2**：ACK 协议可能因固件版本不同而不同（老约定 1=OK / 新约定 0=OK）。
  工具用 `radar_hwstop.parse_ack`，默认老约定；若全部 `ACK_FAIL` 但实际出流，
  说明只是约定判断问题，用 `--no-ack` 跳过即可。
- **风险 3**：序列文件含厂商寄存器配置，**不得提交、不得外传**。
  `.deepworks/tmp/` 已在 `.gitignore` 内。

## 9. 下一步

1. 插上 2T4R（COM4），跑 `--send --verify`，记录真实结果。
2. 成功后把结论写回本文件第 6 节，并更新 `docs/radar-autoconnect-handoff.md`。
3. 若失败，优先怀疑：波特率、chipAddr、段序、固件版本。逐项对照厂商日志定位。

---

## 保密边界

- 本文件只记录**流程、协议结构与离线校验结论**，不含任何厂商寄存器地址/取值。
- 具体启动帧由 `extract_vendor_boot.py` 从**本机 ONELAB 日志**生成，落在 `.deepworks/tmp/`，**不随仓库分发**。
- 厂商安装包、授权文件（`.lic`）、ONELAB 日志原文均不纳入仓库。
