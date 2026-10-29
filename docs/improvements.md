# 熊出没跑酷 · 改进清单

评审对象：`bear-run`（当前交付包 `outputs/bear-run-20261020e.zip`）
方法：源码逐行阅读 + headless Chrome（CDP）实测（`?input=mock` 自动驾驶长跑、`?input=replay` 静息回放、分层隐藏归因 draw call）。
结论按"该不该现在改"排序，每条都带文件位置、证据和预估改动量。

---

## 结论速览

| 优先级 | 问题 | 位置 | 改动量 |
|---|---|---|---|
| **P0** | ~~滚轮箱占道记账与实际扫掠不符 → 金币/医疗箱可能把你引向它~~ **已修复** | `ObstacleManager.js`（`_spawnRow` / `_addObstacle` / `getThreats`） | 已改完，A/B 实测通过 |
| **P0** | `slideCooldown` 是死配置，滑铲无代价 | `config.js:45` | 1 行 |
| **P0** | HUD 血量格数用 `start` 而非 `max`，看不到血上限 | `Hud.js` `setHearts` | ~5 行 |
| **P1** | 地砖 18 块 = **504 个 mesh**（每块 28 个） | `Art.js` `makeGroundTile` | 中 |
| **P1** | 金币每个 **4 个 mesh**，37 个在场 = 148 次绘制 | `Art.js` `makeCoin` | 小～中 |
| **P1** | 全场景 **2002 个独立 geometry / 2010 个 mesh**，几何体零复用 | `Art.js` / `World.js:107` | 中 |
| **P2** | **完全没有音频** | 全局 | 中大 |
| **P2** | 首个医疗箱在 ~490m 才吃到，新手 21 秒就死 | `config.js:151`, `ObstacleManager.js:185` | 1 行 |
| **P2** | "得分"与"距离"开局数值完全相同 | `Hud.js` / `config.js:61` | ~6 行 |
| **P3** | 体感识别只验证了"不误触发"，**漏检率是空白** | `_radarreplay/radar-replay.js` | 需重采数据 |

---

## P0 · 真问题，改动小

### 1. 滚轮箱（mover）的占道记账与它实际扫掠的两条道不一致 · **已修复**

**证据**

`_spawnRow()` 用一个 `occupied` 表算"这行还剩哪条道是空的"，再把金币串/医疗箱/加速带放进空道：

```js
// ObstacleManager.js:162-168
var occupied = {};
for (var s = 0; s < spawns.length; s++) occupied[spawns[s].lane] = true;
var freeLanes = [];
for (var f = 0; f < lanes.length; f++) {
  if (!occupied[lanes[f]]) freeLanes.push(lanes[f]);
}
// 然后 freeLanes 里随机挑一条放 coin / heal / boost（:179-199）
```

但 `_addObstacle('mover', …)` **把传进来的 `lane` 丢掉了**，位置由随机车道对决定，并且左右扫掠 ±halfSpan：

```js
// ObstacleManager.js:203-208
if (kind === 'mover') {
  var pair = Math.random() < 0.5 ? [0, 1] : [1, 2];
  var baseX = (CFG.LANES[pair[0]] + CFG.LANES[pair[1]]) / 2;
  var span = Math.abs(CFG.LANES[pair[1]] - CFG.LANES[pair[0]]) / 2;
  ...
}
```

**后果 A（体验不公）**：记在 `lane` 上的那条道，往往**不是**滚轮箱真正盖住的那两条。
最坏情况：滚轮箱声明占 `lane 2`，但实际 pair 抽到 `[0,1]` → 它盖的是 0 和 1；这时 `freeLanes` 算出"1 是空的"，于是把**医疗箱/加速带放进 1**——而 1 正好是滚轮箱来回扫的道。玩家跟着补给走就会撞上去。医疗箱是玩家**主动去拿**的道具，这种误导比随机撞一下更伤。

**后果 B（自动驾驶误判）**：`getThreats()` 只按滚轮箱**当前** x 折算成一条道，而它在移动：

```js
// ObstacleManager.js:361-364
var lane = it.lane;
if (it.kind === 'mover') {
  var lx = it.x;
  lane = Math.abs(lx - CFG.LANES[0]) < 1.1 ? 0 : (Math.abs(lx - CFG.LANES[2]) < 1.1 ? 2 : 1);
}
```

实测印证：`?input=mock` 自动驾驶连跑到 1176m 才死，**4 次受击全部发生在滚轮箱登场之后**（749 / 938 / 1026 / 1176m；滚轮箱在第 3 区 450m 起出现）。受击时 x 折算出来的"威胁道"是过时的，自动驾驶按它躲就会撞。

> **更正（2026-10-20，被实测推翻）**：我原先在这里写"几何上不会出现三条道同时堵死的死局……并集最多 2 条，总还剩 1 条，所以这不是必死"，**这个推断是错的**。漏算了一种情况：`mover` 也会被选为**同一行里的第二件障碍**，此时它占的两条道 + 第一件障碍占的那条道 = 三条全盖。修复前的实测反例：`low` 在 `lane 0` + `mover` 抽到 `pair [1,2]` → `cover = 0,1,2`；纯 mover 趟 200 行里至少逮到 3 行，第 4 区真实配比 300 行里至少 3 行。**"三条道全堵"确实会出现**，所以它同时是引导错误、自动驾驶误判**和**不公平死亡。
> （修复后同口径复测：两趟 × 2 遍，`cover == 3` 的行数为 0。）

**已修复（2026-10-20）**。比原设想多修一处 —— 光把 pair 提前决定还不够：`mover` 一旦当上第二件障碍，它要占两条道而那时只剩两条，所以第二件障碍的候选里必须把 `mover` 剔掉。

改动（`ObstacleManager.js`）：

- `_spawnRow()`：`first` 条目在**计划阶段**就带上 `lanes`；kind 为 `mover` 时先掷定 `pair`（`[0,1]` 或 `[1,2]`），`lane` / `lanes` 都记成真实覆盖；
- 第二件障碍：`rest = lanes 去掉 first.lanes`，仍要求 `rest.length >= 2`，且候选 kind 剔除 `mover`（`combo` 退化成 `block`）；
- `occupied` 改为按 `spawns[s].lanes || [spawns[s].lane]` 统计 → `freeLanes` 从此与真实扫掠同源；
- `_addObstacle(kind, lane, z, pair)` 多一个 `pair` 参数（不传才自己掷），道具记录也带上 `pair`；
- `getThreats()` 对 mover 返回 `lanes: [左, 右]` 两条道（旧 `lane` 字段保留，不破坏既有调用方）。

**验证方式**：探针 `.deepworks/tmp/cdp_mover.py` 直接连调 `_spawnRow` 放 500 行，用几何法判定"某条道是否被某件障碍扫到/挡住"（`reach = halfSpan + LANE_TOLERANCE`），A/B 两棵冻结树各跑 2 遍：

| 树 | 趟 | 滚轮箱 | R1 必死行 | R2 道具压箱 | R3 记账自洽 | R4 威胁两条道 |
|---|---|---|---|---|---|---|
| A = 修复前（`bear-run-20261020c.zip`） | 纯 mover × 200 行 | 255 / 251 | **FAIL ≥3** | **FAIL ≥3** | **FAIL ≥3** | **FAIL 251 / 251** |
| A | 第 4 区真实配比 × 300 行 | 58 / 57 | **FAIL ≥3** | **FAIL ≥3** | **FAIL ≥3** | **FAIL 全数** |
| B = 修复后 | 纯 mover × 200 行 | 200 / 200 | PASS 0 | PASS 0 | PASS 0 | PASS 0 / 200 |
| B | 第 4 区真实配比 × 300 行 | 45 / 44 | PASS 0 | PASS 0 | PASS 0 | PASS 0 / 244 |

两处读数口径要说清楚：探针的违规清单**最多留 3 个样例**，所以"≥3"表示"至少逮到 3 行"；修复后 mover 出现率约降 22%（57 → 44 / 300 行），因为原先有一部分 mover 正是以"第二件障碍"的身份被塞进必死行。

另需说明：`mover` **单独**占两条道（没有第三件障碍）时依然打得过 —— 靠节奏从它扫过的那两条道之间钻过去，这是设计本意，不是 bug。修复守住的是"一行里至少有一条道**从头到尾**没被任何障碍扫到"这条不变量（也正是源码里 `仍然留一条活路` 那句注释的意图，以及 `?input=mock` 自动驾驶能安全通过的前提）。

---

### 2. `PLAYER.slideCooldown` 是死配置

```js
// config.js:45
slideCooldown: 0.15,
```

全仓 grep：**只在 config 里出现，`Player.js` 从未读取**。

**后果**：滑铲没有冷却、没有减速、没有代价 → "一直按住滑铲"是纯收益策略；而跳跃至少要付重力代价。两个动作在决策上不对等，玩法会退化。

**建议**：要么在 `Player.js` 的滑铲入口真的用上它，要么删掉这行——留着会误导后续维护者以为已有约束。是否额外给滑铲加 10~15% 减速是设计选择，但至少要把配置和实现对上。

---

### 3. HUD 血量格数用了 `start` 而不是 `max`

```js
// config.js:60
HP: { start: 3, max: 5 },
```

`Hud.setHearts(hp, max)` 收到的 `max` **参数没被使用**，格数由 `Math.max(CFG.HP.start, hp)` 决定。

**后果**：吃到医疗箱 3→4 时，只是"凭空多冒一个心"；玩家看不到血量上限是 5，也不知道"还能再补"。实测确实会到 4（长跑第 42 秒 hp 3→4）。

**建议**：按 `CFG.HP.max` 渲染 5 格，未满的用空/灰格。医疗箱的收益立刻可见，也让"血量"这个概念完整。

---

## P1 · 性能（收益最大的一块）

### 实测数据

| 场景 | draw calls / 帧 |
|---|---|
| 开局（场景里还没有任何障碍） | **864** |
| 正常进行中 | 1112 ~ 1273 |
| 779m（第 3 区） | **1510** |

> 补测（加入粒子特效之后复测，同一套测量口径）：主菜单 817~836、进行中 1145~1520，与上表同量级。
> 粒子特效本身实测只占 **+2** draw call（冻结同一画面、反复切换两个点云的 `visible` 求差，
> 连续 6 次重试均稳定为 2），且粒子数量与跑了多远无关（池容量开局固定 260 + 200）。

分解（同一次测量内逐层隐藏，近似值 ±10%）：

| 部件 | draw calls | 对象数 | mesh 数 | 独立 geometry |
|---|---|---|---|---|
| 路边景物（`world.props`） | ~257 | 52 | 289 | 289 |
| 地砖（`world.groundTiles`） | ~230 | 18 | **504** | **504** |
| 区域摆件（`world.regionDecor`，3 区合计） | ~200 | 48 | **818** | **818** |
| 云 10 + 鸟 5 | ~40 | 15 | 72 | 72 |
| 障碍 + 拾取物 | ~342 | ~55 | ~300 | — |
| 熊二 | 69 | 1 | 69 | — |
| 光头强 | 31 | 1 | 31 | — |

**关键事实：`meshes = 2010`，`unique geometries = 2002`，而材质只有 81 个。**
也就是说——**几乎每个 mesh 都独占一份几何体，完全没有复用**。这既白烧显存（GPU 侧 geometry 数已到 2115），又让 three.js 无法批处理。

顺带一个好消息：**没测到内存泄漏**。geometry 数随距离 1343→1673 是单调的，但增速递减（+176 / +113 / +33）并收敛，与场景内唯一几何体数一致；对象池工作正常（金币池稳定留 20 个备用，`items` 长期稳定在 51~71）。

> **复测复核（55 秒长跑 + 开关粒子的 A/B）**：粒子**开** geometry 1214 → 2018，
> 粒子**关** 1329 → 2011 —— 两条曲线基本重合，证明这段增长与粒子特效无关
> （粒子只在开局一次性贡献 2 个几何体）。同一窗口内 **JS 堆 24.6MB → 24.5MB**
> （全程只在 22~49MB 之间随 GC 抖动），**着色器程序数恒为 8**（没有反复重编译）。
> 结论与上面一致：增长是"跑到哪就懒建到哪"的区域摆件造成的，**有上限**（终点≈全场景
> `meshes` 总数 ~2000），不是无限泄漏。
>
> 但这个**上限本身偏高**：在软件渲染（headless + SwiftShader）下，长时间跑动会让
> 浏览器进程直接崩掉（实测 50~70 秒区间，`/json/list` 里整个 page 目标消失）；
> 同样的时长换真机 GPU 未必复现，但第 6 条的收益因此更值钱 —— 它同时压的是
> 长时间运行的稳定性。**复现命令见文末**（`cdp_leak.py`，可 `--off` 对照）。

### 4. 地砖 18 块 = 504 个 mesh（最离谱的一处）

`groundTileLength: 12`，`visibleLength: 190` + `recycleBehind: 12` → 18 块砖。
每块砖 **28 个 mesh**（504 / 18），全部各自独立 geometry。

一块 12m 的地面视觉上就是"土路 + 两侧草地"，却拆成 28 次绘制。整片地面约 **230 次 draw call**。

**建议**：同一块砖内的静态部件用 `BufferGeometryUtils.mergeGeometries()` 合成 1~3 个几何体（土路 / 草地 / 碎石），碎石与草簇用 `InstancedMesh`。地面是全场最大的单点开销，也最容易压。目标：504 → 20 以内。

### 5. 金币：每个 4 个 mesh，材质却只有 4 种

实测：`coin: items=37 meshes/item=4.00 groups=37 uniqMat=4`
→ 场上 37 个金币 = **148 个 mesh = 约 148 次 draw call**，而它们**共用同样的 4 个材质**。

这是教科书级的 `InstancedMesh` 场景：4 个部件各建一个 InstancedMesh，37 个金币 → **4 次绘制**。

同类：`low` 8 mesh/个、`high` 17 mesh/个、`block` 10、`mover` 14、`boost` 11、`heal` 5。
把每个障碍 kind 的部件合并成 1 个几何体（multi-material groups），或按部件实例化。

**目标：障碍+拾取物 342 → 30 左右。**

> 另一个放大器：`coinRowChance: 0.72` × 每行 3~6 枚（`coinRowMin/Max: 3/6`），单行沿 z 铺 `z - c*1.05` → 一行最多 6 个 mesh。金币数量是"行概率 × 行内枚数"双乘出来的，改 5 的收益会再乘一层。

### 6. 全场景几何体零复用

`World._buildProps()` 每个槽位直接新建：

```js
// World.js:105-115
for (var i = 0; i < slots; i++) {
  for (var side = -1; side <= 1; side += 2) {
    var prop = A.makeRandomProp();   // 每次都新建 geometry
```

同理 `_buildRegionDecor()` 一次性把 3 个区域的摆件全建出来（`World.js:185-198`），48 个对象、818 个 mesh。

**建议**（按性价比排序）：
1. `A.makeRandomProp()` / `A.makeRegionDecor()` 内部对**每种景物类型缓存几何体**，实例化时只随机 `position / rotation.y / scale`，几何体共享。改动集中在 `Art.js`，收益是 2002 → 几十个 geometry。
2. 或者保持现有构造函数，但改成 `prototype.clone()`（`clone()` 天然共享 geometry），再对 clone 做随机化。
3. 进一步用 `InstancedMesh` 批量同类型景物。

`World.js:107` 的 `prop.position.set(…)` 已经支持"只随机变换"，所以改成原型复用的阻力很小。

---

## P2 · 体验打磨

### 7. 完全没有音频

全仓 grep `AudioContext|new Audio|\.play\(|createOscillator|Howl|sound|volume` → **零命中**。

一个跑酷游戏没有跳跃、落地、受伤、吃到金币、区域切换、BGM 的音效，是当前**最大的单点体验缺口**。体感游戏尤其需要"动作→声音"的即时反馈来建立手感。

**建议**：优先做 4 个——跳跃、受伤、金币、区域切换横幅同时来一声音效。用 WebAudio 合成即可，不需要音频文件（也更省包体）。**注意**：`file://` 双击直玩的场景下，WebAudio 必须由首次点击（开始按钮）解锁，要在 `#btn-start` 的 handler 里 `resume()`。

### 8. 首个医疗箱太远，新手大概 21 秒就死

- `healEveryMeters: 300`，且医疗箱生成在前方 `z = -visibleLength`（190m 处），所以**第一次真正吃到回血在 498m**（长跑实测：第 42 秒 hp 3→4）。
- 也就是说 **0~490m 全程只有 3 点血，没有任何回复**。
- 实测最坏情况：`?input=replay`（静息数据 = 完全不操作）时玩家在 **204m / 21 秒** 就 hp 0 死亡。

对"第一次上手、还在摸索体感动作"的玩家，21 秒阵亡偏苛刻。

**建议**（一行改动）：`reset()` 里让 `meterSinceHeal` 的初值设为 `CFG.PICKUP.healEveryMeters - 120`，使首个医疗箱在 ~180m 内出现。或者第 1 区受击只掉半血。

### 9. "得分"与"距离"开局数值完全相同

`SCORE: { perMeter: 1, perCoin: 20, boostMul: 2 }` → 分数 = 距离 + 20×金币数。
开局没吃金币时两者就是一个数（截图实测：「得分 23 / 距离 23m」）。

**建议**：把"距离"改成 **"距下一区 xxx 米"**（用 `CFG.DIFFICULTY[i+1].at - distance`）。既去掉了信息重复，又强化了刚做好的区域横幅节奏感——玩家会更在意"还有多久换区"。

### 10. 受伤红闪会染色整屏

`#damage-flash` 目前是全屏覆盖，实测截图中整帧明显偏红。3 次受伤就是 3 次全屏红。建议缩成边缘渐晕（vignette）或降低不透明度，保留"疼"的感觉但不影响看清障碍。

---

## P3 · 未知量：体感识别的漏检率是空白

**已验证的好消息**：静息状态下基本不误触发。
`?input=replay` 走真实解码链路，214 帧（21 秒）内只触发 1 次跳跃，而且 `zmax` 从 2.5 飙到 **74.11**——明显是采集分段边界（能量 750→7800 跳变）造成的伪触发，**不是静息噪声**。折算约 2.8 次/分钟，且可归因于数据本身。`k = 3.2` 的阈值标定是稳的。

**但**：同一份数据**无法测漏检率**（真实"跳/蹲/左右移"是否被识别），因为 `_radarreplay/radar-replay.js` 的 meta 里明写了：

```
"kind":"rest-only, 无动作真值"
"note":"energy/rms 与帧内布局无关; 未解出角度维 -> x 恒为 0"
```

4 个 series（pos_0p70 / 1p00 / 1p35 / 1p70）都是**人站着不动**的采集，且**角度维没解出来，x 恒为 0**——意味着左右换道这个动作目前从数据上就无从验证。

**这是体感功能最大的未知量，比任何代码问题都值得优先解决。**
**建议**：补一次**带动作标注**的采集——站定 → 跳 3 次 → 蹲 3 次 → 左右各移 3 次，全程留时间戳。有了真值才能标定灵敏度/特异性，进而调 `k`、判定窗口和滞回。同时需要解出角度维，否则"换道"形同虚设。

---

## 已验证、不需要改的（避免误伤）

- **无内存泄漏**：GPU geometry 数随距离收敛；对象池正常复用（`pools` 有 20 个金币备用）。
- **受击有无敌帧**：`CFG.PLAYER.invulnTime: 1.15`，`Player.isInvulnerable` 有真实消费者（`Player.js:772` 的闪烁、`Game.js` 的判定）→ 不会出现连续秒杀。
- **区域节奏正确**：按 0 / 180 / 450 / 800m 切换，实测 stage 0→1 发生在 187m，与配置 180 吻合；横幅与 `#stat-stage` 同步。
- **代码卫生良好**：全仓 `TODO|FIXME|HACK|XXX|console.log|debugger` 零命中。
- **相机 `far` 无浪费**：`far = 600` 而景物最远只到 -210、`fog far = 205`，目前没有多余绘制。但**如果以后把 `visibleLength` 拉长，记得同步收紧 `far`**，否则雾里那些看不见的东西会照画不误。

---

## 附：复现命令

```powershell
# 无缓存预览（8802）
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\serve_nocache.py

# 自动驾驶长跑（公平性 + 性能）
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_soak.py

# 静息回放 → 误触发基线
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_replay.py 45

# 场景构成 / draw call 归因
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_world.py
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_items.py

# 粒子特效专项自检（含 draw call 增量 +2、池回收、处决星火）
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_particles.py

# 滚轮箱占道几何探针（修复前的树应 FAIL、修复后应 PASS；不传 URL 会报用法）
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_mover.py "file:///C:/<path>/index.html" "--port=9350"

# 长时间跑动压测 + 粒子开关 A/B（看几何体与 JS 堆的趋势）
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_leak.py --secs 50
& "C:\Users\BayMax\AppData\Local\Python\bin\python.exe" .deepworks\tmp\cdp_leak.py --off --secs 50
```

> 注意：`cdp_world.py` 的逐层隐藏归因要求世界静止。若中途发生受击/死亡，镜头会切到处决机位，读数会失真——所以读那组数时要挑"稳定运行中"的时刻，别在死亡演出附近采。

> 这些自检脚本都接受"第一个参数 = `file:///…/index.html`、第二个 = `--port=`"，所以能直接对**解压后的交付包**复验：把 zip 解到临时目录再跑，验的就是玩家真正拿到的那一份。例如
> `cdp_particles.py "file:///C:/Users/BayMax/AppData/Local/Temp/opencode/verify-e/bear-run/index.html" --port=9341`
> （每次换一个 `--port`，脚本按端口建独立 profile，避免 Chrome 复用旧缓存。）

> 另一个坑：在 headless + SwiftShader 下**用 `Page.captureScreenshot` 抓正在渲染的重场景，会把整个浏览器进程搞挂**（几十秒后 `/json/list` 里连 page 目标都没了，报 `ConnectionResetError / WinError 10054`）。这与游戏无关，是截图路径 + 软件渲染的问题。所以 `cdp_particles.py` 默认**不截图**（功能断言一律不看画面佐证）；需要画面时加 `--shots`，或者单独跑一趟短脚本，拍完就退出。
