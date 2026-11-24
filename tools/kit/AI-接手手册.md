# AI 接手手册 — 从零到能采数据

> 照抄命令即可。每一步都有**通过判据**，没通过就别进下一步。
> 所有命令都在**包根目录**下执行（也就是本文件所在目录）。
> 版本：2026-11-24。

---

## 步骤 0：环境自检（不需要插雷达）

```powershell
py -3 --version
py -3 -m pip install pyserial numpy
py -3 -c "import serial, numpy; print('ok', serial.__version__, numpy.__version__)"
```

**通过判据**：打印 `ok 3.x.x 2.x.x`。

`py` 不存在就试 `python`；两个都没有，先装 Python 3（勾选 Add to PATH）。
或者直接双击包里的 `安装依赖.cmd` / `环境检查.cmd`。

---

## 步骤 1：离线能力自检（不需要插雷达）

这一步验证「判据代码 + 数据 + 依赖」三者都对，是最省时间的排错手段。

```powershell
# 跳蹲判据：单个电池目录应该接近 100%
py -3 tools\jumpduck_fusion.py data\jumpduck_battery\20261113-133307_S01

# 左右判据：lr3b 应落在 50~75%；lr3c 应 >= 85%
# --out 只写文件名，结果落在 outputs\lane-eval\ 下
py -3 tools\lane_eval.py --dir data\lr3b --out selfcheck-lr3b.json
py -3 tools\lane_eval.py --dir data\lr3c --out selfcheck-lr3c.json
```

> 输出中文乱码时，先在窗口里执行 `chcp 936` 再跑；这是控制台代码页的问题，不是脚本坏了。

**通过判据**：

- 跳蹲输出里 `jump` 接近 100%、`crouch_mid`（中蹲）全对
- 左右：`lr3b` 的 LEFT+RIGHT 合计 16 次动作，正确率落在 50~75%
- 左右：`lr3c` 正确率 >= 85%（RIGHT 应 >= 7/8）

数字差很多 → 先怀疑依赖装错或数据被改过，**不要先改判据**。

---

## 步骤 2：确认包内路径关系没断

```powershell
py -3 -c "import sys; sys.path.insert(0,'radar-pointcloud'); import radar_source, radar_dsp, radar_actions; print('radar modules OK')"
Test-Path radar-bridge\tools\radar_live_ws.py
Test-Path bear-run-new\index.html
```

**通过判据**：打印 `radar modules OK`，两个 `True`。

`radar_live_ws.py` 会自己把 `radar-pointcloud` 加进 `sys.path`，
所以这两个目录**必须同级**，不要拆开放。

---

## 步骤 3：插板 + 启动（唯一需要硬件的步骤）

```
1) 先插水平板（横放），等 2 秒
2) 再插竖直板（竖放）
3) 双击「一键-自动启动.cmd」
```

**通过判据**：命令行里出现两行 `启动 水平：COMx → ws://127.0.0.1:8766`
和 `启动 竖直：COMy → ws://127.0.0.1:8767`，浏览器停在游戏页面。

启动不了就用 `启动-双雷达游戏.cmd`，页面上能看见每个串口的角色，
手动改完点「启动并回游戏主页面」。

### 连上了但没数据？

```powershell
py -3 tools\radar_selftest.py --watch --port 8766
```

- `frames` 在涨、`n` 长期为 0 → 波束没打到人（做步骤 4）
- `frames` 不涨 → 串口没真正读到数据，检查驱动/波特率

---

## 步骤 4：确认波束打得到人（现场第一件事）

```powershell
# 1) 停正式服务
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8778/api/stop -Body '{}'

# 2) 诊断实例（保留静止点）
py -3 radar-bridge\tools\radar_live_ws.py --port COM3 --role lr --ws-port 8781 --clutter-keep 0.0

# 3) 空场 + 在场各扫 12 秒
py -3 tools\sector_scan.py --secs 12 --port 8781
```

**通过判据**：人站进去后直方图峰比空场移动 >= 15°。

不移动 → 转板，或把人站到峰所在角度（并更新 `radar-pointcloud/center.json`）。
**不要靠调阈值硬救**。

---

## 步骤 5：锁 zero（顺序不能反）

```powershell
# 人先站到位，再启动
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8778/api/start -Body '{}'
py -3 tools\radar_selftest.py --watch --port 8766
```

**通过判据**：`x` 在 ±0.6 内往返、不贴 ±1；`zero` 与人实际角度差 < 10°。

---

## 步骤 6：采一轮带真值的数据

```powershell
# 左右移（默认自动定距离带，别写死）
py -3 radar-pointcloud\segment_capture.py --port COM3 --out data\lr-1124 --auto-band

# 跳蹲
py -3 tools\jumpduck_capture.py --port COM6 --out data\jumpduck-1124
```

**通过判据**：`data\<批次>\meta.json` 里有真值标签，样本数 >= 计划数。

采集时必须**先确认波束打得到人**（步骤 4），否则采回来的全是噪底。

---

## 步骤 7：离线评估

```powershell
py -3 tools\lane_eval.py --dir data\lr-1124 --out _work-lr1124.json
py -3 tools\jumpduck_fusion.py data\jumpduck-1124\*
```

把结果写进 `docs/acceptance-test-table.md` 的「执行结果 / 证据」两栏，
**按实际达标情况**标 `[通过]` / `[未通过]`。

---

## 常见坑（都踩过）

| 坑 | 真相 |
|---|---|
| 装 FTDI 驱动 | 错。芯片是 **CP2102**，装 CP210x |
| 写死 `--band 1.36,2.04` | 会丢 48% 的帧。用 `--auto-band`（采集）/ `--band 0.5,1.9`（实时桥接） |
| 给 `radar_live_ws.py` 传 `--auto-band` | **没有这个参数**，只存在于采集脚本 |
| 用「最强点」找人的位置 | 会锁到显示器（实测差 38°） |
| 先启动再让人站好 | `zero` 会锁到墙，整档 `x` 贴 −1 |
| 期望 30 fps | 帧周期 129.5 ms ≈ **7.7 Hz**，这是硬件上限 |
