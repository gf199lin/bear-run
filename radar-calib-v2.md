# 雷达开局校准（新手教程）v2 设计与验收

> 文件：`radar-runner/js/Calibration.js`（约 1043 行）+ `radar-runner/js/config.js`（`RADAR.calib` 段）
> 设计/验收时间：2026-10-21　　数据依据：`docs/radar-dataset-evaluation-2.md`

---

## 1. 为什么改：数据告诉我们「不能靠识别」

用户诉求是「开局加个类似新手教程的东西，先测一下雷达」，最早的想法是「把判定做得更强」。

但两批新数据的评估结论（见评估报告）是：

- 上游给的跳/蹲字段在**静息状态**下误报率 **39.4% / 54.3%**（523 帧真实负样本实测）；
- 跳/蹲判据的等长窗混淆矩阵 TPR 87% 但 **FPR 89%**，单帧 AUC ≤ 0.63 —— 数据里**没有可提取的信号**；
- 左右方向符号只有 **53.3%** 准确率，且去基线后 RIGHT 位移**依旧是负的**（0.07~0.16 m）—— 不是符号装反，是没采到。

所以 v2 的设计目标从「识别得更准」改成 **「分清能不能用，能用就量体裁衣，不能用就干净地退回键盘」**：

| 机制 | 一句话 |
|---|---|
| ① 质量探针 | 第 1~3 步（玩家本不该跳/蹲）顺便数上游误报，超限 → 判「上游不可信」 |
| ② 有界自适应 | 可信时把阈值学成 `clamp(峰值×margin, 下限, 上限)`；峰值太弱直接判不可用 |
| ③ 拒绝 + 回退 | 不可信一律写 `disableTh(=2)` 彻底停用雷达跳/蹲，让玩家用键盘继续玩，而不是随机乱跳 |

**设计原则：宁可少给一个功能，也不给一个会自己乱动的角色。**

---

## 2. 五步流程与判定

| 步 | 玩家做什么 | 系统测什么 | 通过条件 |
|---|---|---|---|
| 1 | 站好别动 | 静息基线 `x0` + 基线稳定性 + **探针开始计数** | 连续 8 帧且 x 标准差 ≤ 0.18 |
| 2 | 向左走一步 | 相对基线位移 + **探针计数** | 位移 ≥ 0.35 且连续保持 3 帧 |
| 3 | 向右走一步 | 相对基线位移 + **探针结束** | 同上；第 2/3 步位移一正一负定出 `sign` |
| 4 | 跳一下 | 上游 `jump`/`height` **新上升沿** + 峰值 | 峰值 ≥ 学到阈值且 ≥ 0.40 |
| 5 | 蹲一下 | 上游 `crouch` 新上升沿 + 峰值 | 同上 |

- 第 2/3 步还要求 **左右位移之差** `|右位移 − 左位移| ≥ minSpread(0.5)`（两侧各 0.35 时下界 0.70），防「原地晃一下就过」。
- 第 4/5 步共用探针结论：第 1~3 步若已判「上游不可信」，第 4/5 步直接跳过（省 25 s），不浪费玩家时间。
- 单步 25 s 超时 → 自动重试一次，重试时**保留原提示语**（否则「太弱」的提示会被「再来一次。」瞬间覆盖，玩家永远看不到失败原因）。

---

## 3. 参数（`config.js` → `RADAR.calib`）

| 参数 | 值 | 说明 |
|---|---|---|
| `baseFrames` / `baseStdMax` | 8 / 0.18 | 第 1 步静息判定 |
| `moveDelta` / `holdFrames` | 0.35 / 3 | 第 2/3 步位移与保持帧数（0.35 贴着游戏进道阈值 0.36）|
| `minSpread` / `stepTimeoutMs` | 0.5 / 25000 | 位移差下限 / 单步超时 |
| `probeFrames` | 24 | 探针参考帧数（< 8 帧不下结论，防掉线误判）|
| `probeSpuriousMax` | 2 | 静站时上游跳/蹲误报**事件**数上限（各自）|
| `probeRateMax` | 0.06 | 或误报帧占比上限 6% |
| `weakPeak` | 0.40 | 玩家峰值低于它 → 信号太弱，判不可用（不硬凑阈值）|
| `attemptLevel` | 0.30 | 原始值冲破它 = 「玩家确实试了一下」（**只认上升沿**），用于给出「太弱」提示 |
| `learnMargin` / `thFloor` / `thCeil` | 0.6 / 0.30 / 0.50 | 学到的阈值 = `clamp(峰值×0.6, 0.30, 0.50)` —— **只放松，不放宽到噪声里** |
| `disableTh` | 2 | 判不可信时写入的阈值（字段范围 0~1，≥1 不可达 ⇒ 彻底停用该动作）|

> 阈值为什么必须有 `thFloor`：探针只能证明「第 1~3 步这 24 帧没有误报」，不能证明「0.20 的阈值在别的时候也安全」。低于 0.30 就是在拿玩家的游戏体验赌上游噪声。

---

## 4. 零侵入接线

```
RadarInputSource._handleFrame(frame)  ──►  [装饰器]  x' = (x - x0) * sign  ──►  decoder.push()
                                              ↑ 保留原始值到 frame.rawX
```

- `js/InputSource.js` **一行未改**（该文件当时正被另一会话改动，避免冲突）；只给「实例」打一层装饰器。
- 解码器读阈值是**热的**：`InputSource.js` 里 `var CFG = RR.CFG; var R = CFG.RADAR;` → 写 `RR.CFG.RADAR.jumpThreshold` 立即生效，无需重载。
- 对外接口：`RR.Calibration.load/save/clear/isUsable/install/setEnabled/isInstalled/applyThresholds/restoreThresholds/savedThresholds`、`RR.CalibrationWizard({onDone,onClose})`。

## 5. 存档与兼容

- `VERSION = 2`，KEY 仍是 `bear-run:calib:v1`（不新增 key，避免残留旧档被误读）。
- `isUsable(cal)` 要求 `cal.v === VERSION`，且 `lrOk || jumpOk || crouchOk || keyboardOnly || upVerdict==='noisy'`。
  - 于是**旧版 v1 存档被自动判过期 → 老玩家也会被重新引导一次**（这是有意行为：v2 的口径和 v1 不同，不能拿旧结论硬用）。
  - `load()` 对不可用存档返回 `null`（不静默迁移、不偷偷把 v1「升级」成可信 v2）。
- `keyboardOnly`（玩家选键盘）与 `upVerdict='noisy'`（上游不可信）都要**落盘**，否则每次进游戏都会重新弹向导。
- 仅当「雷达+跳/蹲都测成功了」才把学到阈值写回 config 并保存；否则 `clear()`，保持默认占位值。

---

## 6. 验收（headless 双轨，全部通过）

### 6.1 v2 夹具轨：**68 PASS / 0 FAIL**

```powershell
python .deepworks\tmp\calib_v2\cdp_calib_v2.py      # 本地 http(8767) + headless Chrome(9333) + CDP
```

7 个场景 + 断言（夹具由 `gen_fixtures.py` 用**真实**上游负样本生成）：

| 场景 | 桩数据 | 期望 | 实测 |
|---|---|---|---|
| A_jcNoisy (94 帧) | 真实上游噪声（跳/蹲乱报）+ 配合玩家 | 探针判 noisy、跳/蹲停用、左右仍测出 | probe `{frames:75, jumpEvents:16, crouchEvents:16, spuriousFrames:60, verdict:noisy}`；`lrOk=true, sign=1, jumpOk=false, crouchOk=false`；写入 `jumpTh=crouchTh=2` |
| B_cleanStrong (44) | 干净上游 + 幅度充足 | 全绿 + 学到阈值 | `jumpPeak 0.92 / crouchPeak 0.88` → `jumpTh/crouchTh = 0.50` |
| C_cleanWeak (44) | 干净上游 + 幅度偏小 | 只放松不放宽到噪声 | 学到 `0.33 / 0.31`（> thFloor 0.30）|
| D_tooWeak (101) | 干净上游 + 幅度太弱 | 判「太弱」不解锁 | 提示「检测到跳跃，但幅度只有 0.36（太弱…）」；跳过第 4/5 步后**保留左右标定** |
| E_inverted (44) | 极性装反 + 基线 +0.62 | 现场学到 `sign=-1` | `sign=-1, x0≈0.62`；`0.98→-0.36`、`-0.05→+0.67` |
| F_lrImpossible (120) | 左右走不动（真实 lr 处境）| 卡在第 2 步、不写存档、不动 config、可退出 | `stillOpen=true`，存档为 `null` |
| G_stuckHigh (95) | 上游**卡高电平** | 只认上升沿，不白送通过 | `stepAfter=3`（卡在第 4 步），`jumpTh` 未改 |

另含：v1 存档被 `isUsable` 拒用、`install()` 改写 x 且保留 `rawX`、`localStorage` 往返、以及 `__errs.length === 0`（全程无脚本异常）。

### 6.2 集成回归轨：**30 PASS / 0 FAIL**

```powershell
# 真页面（radar-runner/index.html）里跑，覆盖菜单/输入源切换/装饰器/存档/自动引导
python .deepworks\tmp\cdp_calib.py "file:///C:/Users/BayMax/Desktop/%E4%BD%93%E6%84%9F%E5%B0%8F%E6%B8%B8%E6%88%8F/radar-runner/index.html" 9373
```

S1~S7 覆盖：脚本加载与接口完整性、菜单按钮/状态位、向导开合与输入源切换、装饰器对真实雷达源生效（`x=0.90 → -0.35`）、`setEnabled` 开关、`?input=radar` 首启自动引导、老玩家 v2 存档自动装载不打扰、**v1 存档判过期并重新引导**。

### 6.3 本轮自检抓出的 3 个真实缺陷（已修）

| # | 缺陷 | 影响 | 修法 |
|---|---|---|---|
| ① | 重试时消息被覆盖 | 玩家永远看不到「太弱」的真实原因 | `_retryStep(msg, kind)` 支持传入 `this.message` |
| ② | `skip()` 一律把 `lrOk` 置 false | 玩家已测好的左右标定被白丢 | 抽出 `_lrOkResult()` 供 `_finish`/`skip` 共用，跳过时保留左右结果 |
| ③ | 峰值判定只看「当前电平 ≥ 阈值」 | 上游卡高电平时会**白送通过**（第 4 步直接判过） | 改判**新上升沿**；并新增「原始值上升沿」通道（`attemptLevel`，只认上升），使 0.36 这类「够不着 0.5 解码阈值」的尝试也能被识别并给出「太弱」提示 |

第 ③ 条是真 bug：修复前场景 G 会误判通过；现在 `stepAfter=3`（正确卡住）。

---

## 7. 已知边界

- 教程只解决「符号/基线/上游可用性」，**不能让不可用的雷达变可用**；左右方向仍依赖后续重采（`outputs/雷达数据补采方案.md`）。
- 探针判定基于「第 1~3 步这 24+ 帧没有误报」，属于**必要条件**而非充分条件；阈值下限 0.30 是防止把噪声当动作的兜底。
- 未做：多人/多站位自动适配（当前是每人一次现场标定）；雷达板被移动后需重新校准（提示文案已给出）。
