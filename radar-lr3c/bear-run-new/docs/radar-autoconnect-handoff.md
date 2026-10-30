# 交接：雷达自动连接包（给同学用）

> 状态：**包已交付，1T2R 全链路实测通过；2T4R 当前不出流，待用户侧确认上电/复位**。
> 日期：2026-10-30
> 仓库位置：`radar-service/`（源码已并入主线；本文保留当时的打包与验证记录）

---

## 1. 为什么做这个包

原来的双雷达启动方式是 `dual-radar-kit/tools/start_dual_radar.cmd`，里面**写死 `--port COM3` / `--port COM4`**。换一台电脑 COM 号一变，脚本就永远打不开；而且 `radar_source.find_radar_port()` 只返回「第一个命中的口」，两块板同时插时只会打开一块。

所以不是雷达连不上，是**端口发现层不可移植**。

本包的做法：

- 端口清单 = **注册表扫描 ∪ pyserial 枚举**（互补；本机实测 pyserial 有时一个都枚举不到，注册表仍能给出两个口）；
- 板型不靠 COM 号，靠 **USB VID:PID**：`10c4:ea60` → 1T2R（CP210x）、`0403:6001` → 2T4R（FT232）；
- 再用**真实字节流形态**复核（1T2R：`ICLH` 封装或 40 B 裸帧；2T4R：136 B 裸帧 `55 AA`）；
- 两块板并发打开、各自独立，从不写寄存器、不发雷达命令。

---

## 2. 仓库里对应哪些文件

早期是打成一个可直接发给同学的 zip；现在源码已并入主线，位置如下：

```
radar-service/1-安装环境.cmd       检查 Python + 装 pyserial / numpy
radar-service/2-检查雷达.cmd       scan + check（先跑这个）
radar-service/3-采集数据.cmd       自动识别并录原始字节 → captures\
radar-service/4-持续监视.cmd       watch
radar-service/5-推流到游戏.cmd     radar_serve.py：自动分配 lr/ud 并起两路 ws
radar-service/6-停止推流.cmd       radar_serve.py stop
radar-service/radar_autoconnect.py 端口发现 / 判定 / 采集 / 监视
radar-service/radar_serve.py       推流编排
radar-service/radar_boot_2t4r.py   2T4R 纯 Python 启动（新增，见 docs/boot-2t4r-pure-python.md）
radar-service/setup_check.py       环境检查
radar-service/tools/radar_live_ws.py 推流实体
radar-service/tools/rx_pair_probe.py 2T4R 四通道对角探针
radar-service/tools/horizontal_guide.py 横向方位角标定引导
radar-service/tools/extract_vendor_boot.py 从厂商日志提取 2T4R 启动帧（输出不入库）
radar-service/radar-pointcloud/    解析/识别模块 + calib.json
```

发 zip 时保留的 `先读我.md`、`故障排查.md`、`Python安装说明.md` 仍在 `radar-service/` 根目录。

关键改动：`tools/radar_live_ws.py` 原来按 `HERE/../../radar-pointcloud` 找模块（适配 `dual-radar-kit/tools/` 布局）。仓库内已改为**两种布局都兼容**（先找 `../radar-pointcloud`，再找 `../../radar-pointcloud`）。

---

## 3. 本轮已执行的验证（本机，2026-10-30）

| 项 | 证据 |
|---|---|
| 枚举 | COM3 `10c4:ea60` CP210x → 1T2R；COM4 `0403:6001` FT232 → 2T4R |
| 判定 | 1T2R 7.72 帧/s（典型 7.74），形态「40 B 裸帧」@921600 |
| 单板推流 | `radar_serve.py` 起 `ws://127.0.0.1:8766`，自检收到 6 帧，8.1 帧/s |
| 停止 | `radar_serve.py stop` 正确结束 PID 并删 PID 文件 |
| 采集 | 4 秒 154.3 KB，`meta-*.json` 写入正常 |
| 采集可解析 | 采回的 bin 被 `sniff_board_from_file` 判为 `1t2r`，回放出 30 个雷达帧 |
| 角色分配 | 7 个离线用例全通过（双板/顺序颠倒/单板两种/同型号/手动指定/未检测端口） |
| 修掉的 bug | `capture --port COM3` 在未给 `--board` 时 `KeyError: None`（v1.0 真实崩溃点），已改为先现场判定板型再取波特率 |

**未验证**：2T4R 当前**端口能开、5 秒 0 字节**（6 档波特率全试过）→ 双板同时推流没跑通。这不属于本包缺陷，判定逻辑已正确区分 `busy` / `missing` / `no_data`。需要先确认那块板是否上电、是否要复位、或是否需要厂商工具触发才出流。

> 复验补充：当时打 zip 后再次启动推流时，COM3 被 `BearRunRadarBridge` 占用，因此推流复验用的是打包前的同一份源码目录；zip 内容为该目录的副本（排除 `__pycache__` 与 `logs/*.log`），已解压确认可正常 `scan` / 导入 / 报错。
> 现在源码已直接在 `radar-service/` 内维护，不再需要单独解压 zip。

---

## 4. 下一步

1. **2T4R 恢复出流**：拔插/复位那块板，用 ONELAB 确认它自己能出数据，再跑 `2-检查雷达.cmd`。
2. 两块都通过后跑 `5-推流到游戏.cmd`，确认 8766 / 8767 两路都收到帧。
3. jump / crouch 仍属候选项（1T2R 无俯仰角，2T4R 俯仰链路在验），**不得写进「已实现功能」**。
