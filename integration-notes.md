# 整合说明：新手指引 × 雷达连接

本文记录 `bear-run-有新手指引版2.zip` 与 `bear-run-radar-connect-20261022-share(2).zip`
合并成一个交付包时做了什么、为什么这么做，以及后续怎么接着改。

- 合并日期：2026-10-23
- 合并结果目录：`bear-run-整合版/`
- 一句话结论：**以「雷达连接版」为底座，把「新手指引版」的 6 步引导接进来，并把两条引导串成一条流程。**

---

## 一、两个版本各有什么

| 能力 | 新手指引版（2026-10-21） | 雷达连接版（2026-10-22） |
|---|---|---|
| 6 步玩法引导 `js/Tutorial.js` | ✅ | ❌ |
| 雷达动作校准 `js/Calibration.js`（5 步 + 上游探针） | ❌ | ✅ |
| 雷达联调台 `radar-connect.html` | ❌ | ✅ |
| 联调工具 `tools/*.py` | ❌ | ✅ |
| 随机挑战 / 连击倍率 / 险躲避过 / 结算历史 | ❌ | ✅ |
| 雷达地址记忆（`?radar=` + localStorage） | ❌ | ✅ |
| 护盾磁铁设计稿 `docs/shield-magnet-design.md` | ✅ | ❌ |

关键判断：**雷达连接版是功能超集**——除 `Tutorial.js` 和 `shield-magnet-design.md` 外，
其余文件都是同一份代码的更新版（`World.js` / `Player.js` / `Art.js` / `Audio.js` /
`Particles.js` / `Vick.js` / `FaceCam.js` / `DebugPanel.js` 逐字节相同）。
所以合并方向不是"两边拼代码"，而是**把新手指引移植进新底座**。

---

## 二、改动了哪些文件

| 文件 | 改动 |
|---|---|
| `js/Tutorial.js` | **新增**（来自新手指引版，做了增强） |
| `index.html` | 加「新手指引」入口按钮、引导弹窗结构、`Tutorial.js` 引用；版本号统一 `20261023a` |
| `style.css` | 追加「新手引导弹窗」样式段（深色面板 + 蜂蜜金描边，复用现有 CSS 变量） |
| `js/main.js` | 教程接线：`startWithGuides()`、`btn-tutorial` 绑定、`?tutorial=` 参数、校准与开局的串联 |
| `README.md` | 新增第十节「新手引导」，原第十/十一节顺延为第十一/十二节，目录结构与操作表同步 |
| `docs/shield-magnet-design.md` | 从新手指引版补齐（雷达版没有） |
| `docs/integration-notes.md` | **新增**（本文） |

其余文件原样保留雷达版：`js/Calibration.js`、`js/Game.js`、`js/Hud.js`、`js/InputSource.js`、
`js/ObstacleManager.js`、`js/config.js`、`radar-connect.html`、`tools/`、`docs/radar-*`、`_*.html`。

---

## 三、新手指引做了什么增强

原版 `Tutorial.js` 只有「上一步 / 下一步 / 跳过」的最简弹窗，且**每次点开始都会弹**
（没有用 `hasSeen()` 判断）。整合版做了这些修正：

1. **只首次自动弹**：`hasSeen()` 判断 + `localStorage['bear-run:tutorial-seen']`，
   看过之后点「开始快跑」直接开局。
2. **主菜单常驻入口**：`btn-tutorial`，随时重看；不依赖"清缓存"才能重看。
3. **步骤进度可视化**：`1 / 6` 计数 + 可点击的蜂蜜金步骤条（点已走过的步骤可回看）。
4. **键盘可操作**：`→` / `Enter` / `Space` 下一步，`Esc` 跳过（原版只能点鼠标/触屏）。
5. **API 补齐**：新增 `maybeShow()` / `isOpen()` / `steps`，
   让 `main.js` 能判断"教程是否正开着"，从而编排与雷达校准的先后。
6. **完成后回调带状态**：`show(cb)` 的回调收到 `{ skipped: true|false }`，便于埋点。
7. **文案补齐雷达分支**：最后一步明确写"选了雷达 → 关掉引导后做 5 步校准"。
8. **样式统一**：原版是米黄色卡片，与游戏深色 UI 割裂；整合版改用 `--panel` / `--gold` /
   `--ink` 变量，和暂停、结算、校准三个面板同一套视觉语言。

---

## 四、两条引导怎么串

`js/main.js` 的入口链路：

```
点「开始快跑」
   │
   ├─ ?tutorial=0 或没有 Tutorial 模块 ──────────────┐
   ├─ 已看过教程 ───────────────────────────────────┤
   │                                                ▼
   └─ 未看过（或 ?tutorial=1）→ 6 步玩法引导 ──→ 当前输入源是「雷达」且未校准？
                                                     │            │
                                                    否            是
                                                     │            ▼
                                                     │      5 步雷达校准（可跳过/关闭）
                                                     ▼            │
                                                   game.start() ◀──┘
```

配套改动：

- 原来「选雷达 → 600ms 后自动弹校准」的逻辑，在**教程未看过时不弹**，
  交给上面的链路在教程结束后接管，避免两层引导叠在一起。
- 教程打开前若校准向导已经开着（例如用户先点了「校准雷达动作」），
  `closeWizardIfOpen()` 会先把它关掉，避免两层引导抢键盘输入。
- 校准向导的 `onClose` 里检查 `pendingStart`，所以"校准 → 关闭/跳过 → 自动开局"是闭环的，
  不会出现校准完停在菜单、还要再点一次开始的情况。

---

## 五、验收清单（手动）

用 `python -m http.server` 打开 `index.html`（推荐，localStorage 更可靠）：

1. 清掉 localStorage → 刷新 → 点「开始快跑」→ 应弹 6 步引导，第 1 步标题是「🐻 欢迎来到狗熊岭」。
2. 用 `→` 键走完 6 步 → 点「🚀 开始快跑」→ 应直接进入游戏（输入源为键盘时不再弹校准）。
3. 返回主菜单 → 再点「开始快跑」→ **不应再弹引导**，直接开局。
4. 主菜单点「新手指引」→ 引导重新打开，可随时关闭。
5. 主菜单切到「雷达」（未校准状态）→ 点「开始快跑」→ 先弹 6 步引导 → 结束后弹 5 步雷达校准 →
   校准完成/跳过 → 自动开局。
6. 地址栏加 `?tutorial=1` → 即使已看过也会弹；加 `?tutorial=0` → 直接开局。
7. 控制台 `RR.Tutorial.reset()` → 下次点开始重新引导。
8. 横屏手机宽度（<560px）下弹窗不溢出，按钮可点。

---

## 六、后续可以接着做

- 教程内容按当前输入源动态排序（键盘/触屏/雷达只显示对应的那一列操作），而不是三种全列。
- 教程里放一段 3 秒的静默动图/GIF 说明"跳"和"滑铲"的时机。
- 把"跳过引导"记成 `skipped` 埋点，统计多少人是硬着头皮点完的。
- 首次进入时先弹一个"选择你的输入方式"（键盘 / 触屏 / 雷达），教程和校准都跟着这个选择走。
- 教程与 `touch-hint`（触屏首次提示）去重：看过教程后不必再弹触屏提示。

---

## 七、文件校验

```bash
# 语法（有 node 的环境）
node --check js/Tutorial.js
node --check js/main.js

# 引用完整性：index.html 里所有 js/css 都应存在
python3 - <<'PY'
import re, os
html = open('index.html').read()
missing = [f for f in re.findall(r'(?:src|href)="\./([^"?]+)', html) if not os.path.isfile(f)]
print('缺失文件:', missing or '无')
PY
```
