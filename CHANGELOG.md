# 改动记录

> 记录雷达实时链路与现场可用性的关键改动，按实验日期分组。
> 每条给出「现象 / 根因 / 改法 / 验证」，只写**已实测通过**的内容；
> 未验证的推测会明确标注。

## 2026-11-03 现场联调

### 1. 插上即自动识别双板，2T4R 无流时自动唤醒（`678b619`）

- **现象**：换 USB 口或换板后，`START-RADAR.cmd [1]` 检测不到数据，被当成“设备没插”。
- **根因**：两块板的上电行为不同。
  - 1T2R（`10c4:ea60`，CP210x）上电自跑，直接出流。
  - 2T4R（`0403:6001`，FT232）上电只把 DTR/RTS 拉高、**不发帧**，3 秒内 0 字节，必须重放启动序列。
  - 仓库版 `radar-service/` 缺少自动唤醒，检测 2 秒无数据就跳过；旧入口
    `radar-pointcloud/radar_source.py` 里早就有 `_maybe_boot()`。
- **改法**：
  - `radar_serve.py`：新增 `wake_2t4r()`、`find_streaming(auto_boot=True)`；识别到 2T4R 且状态为
    `no_data` / `unknown` 时，先重放启动序列，再 `check_one(seconds=5.0)`。
  - `radar_source.py`：新增 `auto_boot` / `boot_seq` / `boot_gap` 参数，`_try_open()` 末尾调用
    `_maybe_boot()`。
  - `radar_boot_2t4r.py`：`do_send()` 打开端口后显式 `dtr=True` / `rts=True` + `sleep(0.15)`。
- **验证**：
  - `--dry-run` 自动唤醒后：COM3(1T2R) **7.71 帧/s**、COM4(2T4R) **1.93 帧/s**。
  - 正式推流：8766 收到 **8.1 帧/s**、8767 收到 **2.0 帧/s**。
  - `radar_boot_2t4r.py --send --port COM4 --verify`：**68.0 kB/s、1533 帧/3s**。
  - 旧入口回归：`radar-pointcloud.SerialSource(port='COM4').open()` 自动唤醒，5 秒 **329,344 字节**。
- **开关**：`--no-boot-2t4r` 关闭自动唤醒。

### 2. 合并单文件游戏构建（`cfd3d6e`）

- **现象**：本地双击即玩的单文件 `index.html`（1.65 MB，内联 three.js / 样式 / 校准向导）不在仓库里。
- **改法**：合并进仓库根目录，保留 `ws://127.0.0.1:8766` / `8767` 与
  `?input=radar&radar=...` 入口。
- **验证**：本地静态服务返回 200，游戏可进入。

### 3. 雷达左右方向统一翻转（`5099c1e`）

- **现象**：现场人向左走，游戏角色向右。
- **根因**：1T2R 实测中线 **≈ +9°**，人向自己左侧移动时方位角 **变大**（左移 az +14~+28、
  右移 az -8~0）；而游戏画面按 `x > 0` 向右，两者符号相反。
- **改法**：反向统一放在**游戏层**，`index.html` 增加 `CFG.MIRROR_LR: true`，雷达出口按该开关
  翻转左右。识别层的符号映射有真值标定（`radar_actions.py` 的 `xbase` 判据），不动它。
- **验证**：实机重跑 `START-RADAR.cmd [1]`，人左右移动与角色方向一致。

### 4. 清理上次残留的推流进程（`10a736d`）

- **现象**：`PermissionError(13, '拒绝访问。', None, 5)`，被误判为设备没插。
- **根因**：上一次 `[1]` 留下的 detached 子进程仍占着 COM3/COM4（`radar_live_ws.py`）。
- **改法**：`radar_serve.py` 新增 `_scan_stray_procs()`，用 `Get-CimInstance Win32_Process`
  按命令行匹配本项目进程，与 pidfile 合并后统一清理。
- **验证**：清理后 COM3/COM4 均可重新打开，推流正常。

### 5. `stop` 不再误杀 webcontrol（`a4a55b0`）

- **现象**：雷达检测全部通过，但浏览器打开 `127.0.0.1:8801` 报**拒绝连接**，游戏进不去。
- **根因**：上面第 4 条的残留扫描把 `webcontrol.py` 也当成残留推流。它是常驻的页面服务
  （8801），不占串口，被一起杀掉后页面就没了。
- **改法**：清理名单只保留 `radar_live_ws.py`，`webcontrol.py` 不再进入。
- **验证**：`stop` 后 8801 仍监听，`radar_live_ws.py` 正常退出；重启推流后页面进程未被误杀。

### 6. 推流层 `--flip-lr` 兜底开关（`ffadd54`，默认关闭）

- **背景**：反向已由游戏层负责（第 3 条），推流层若再翻一次会翻回反的。
- **改法**：
  - `tools/radar_live_ws.py` 新增 `--flip-lr`：`pos` 取反 + `LANE_LEFT` / `LANE_RIGHT`
    事件名互换，仅 `role=lr` 生效，`role=ud` 的 `height` 不受影响。
  - `radar_serve.py` 新增同名参数，只在**显式指定**时才传给左右板子进程。
- **用途**：现场临时换用旧版游戏页面（没有 `MIRROR_LR`）时的兜底。
- **默认行为**：与 `5099c1e` 完全一致，不改变任何现有输出。

## 保密与不入库

- `radar-service/boot_seq_2t4r.json` 含厂商寄存器配置，**不入库、不外传**，只在本地放置。
- `.gitignore` 已覆盖 `boot_seq_*.json`、`.deepworks/`、`uploads/`、`*.zip`。

## 回退方式

| 想要 | 操作 |
|---|---|
| 恢复“左右方向与直觉相反”的旧行为 | 把 `index.html` 的 `CFG.MIRROR_LR` 改为 `false` |
| 关闭 2T4R 自动唤醒 | `radar_serve.py --no-boot-2t4r` |
| 临时用旧版游戏页面 | 启动时给左右板加 `--flip-lr` |
| 停推流但保留页面服务 | `radar_serve.py stop`（已不会误杀 `webcontrol.py`）|
