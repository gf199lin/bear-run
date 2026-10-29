# 护盾 + 磁铁 —— 设计方案（仅设计，不含实现）

- 状态：设计稿 v1 · **机制与投放范围已裁决**（见 §9）· 本轮**未改动任何 `radar-runner/js/*` 或外壳文件**
- 依据：全部结论都来自本轮对当前工作区的逐行取证（`file:line` 见 §2、§5）
- 目标：给出可直接施工的落点、数值与验收口径；实现等用户信号后进行
- **已裁决**（用户）：护盾 = **吸收一次**（非限时无敌）；投放范围 = **仅第 3/4 区**（见 §4.6）；**暂不动代码**，等用户信号后按 §10 施工

---

## 0. 为什么现在只出设计

1. 并发第二会话正在 `radar-runner\` 里做**首次 git 提交**（`.git`、`.gitignore`、`.gitattributes`、`.deepworks\tmp\commitmsg.txt` 均已就绪）→ 用户裁决：先只出设计方案，避免半成品被卷进"首个版本"提交。
2. 磁铁方案有一条**硬前置**：它只对"本就落在 `freeLanes` 上的金币"安全。本轮已修复滚轮箱占道记账（`ObstacleManager.js` 的 `_spawnRow` pair 前置 + 第二件剔除 mover + `occupied` 用 `lanes`），并做了 A/B 实测验证（修复前必死行/R4 全数 FAIL，修复后两趟两遍 0 违规）。**磁铁必须晚于该修复上线**，否则磁铁会把"本该在箱子正下方"的金币吸向玩家，制造无法解释的死亡。

---

## 1. TL;DR

| 能力 | 一句话机制 | 改动量 | 新增探针 | 风险 | 建议 |
|---|---|---|---|---|---|
| **护盾** | 拾取后**吸收一次**撞击；未消耗则 12s 作废 | 极小：受击判定链**只加 1 个 `if` 分支**（`Game.js:514`） | 复用现有断言风格 | 低 | 先做 |
| **磁铁** | 持续 7s，把半径内**金币**吸进玩家所在道，由既有采集链自动吃掉 | 小：**零新增碰撞代码**，只加牵引与白名单登记 | 需要 1 条新探针（牵引+白名单回归） | 中（依赖记账修复） | 后做 |

两者都不改变玩家移动能力、不改分数倍率、不引入新的物理。

---

## 2. 现有事实（取证结论）

### 2.1 受击 / 无敌链（护盾入口）

| 位置 | 事实 | 对本设计的含义 |
|---|---|---|
| `Game.js:514` | `if (r.hit && !this.player.isInvulnerable) { hp--; takeHit(); flashDamage(); addShake(1.0); particles.hit(); vick.onPlayerHurt(); }` | **护盾唯一入口**。在 `hp--` 之前插入吸收分支即可 |
| `Game.js:522-529` | `hp > 0` → `SFX('chase')`；否则 `_beginDeath()` | 吸收必须 `return`，不能走到这里 |
| `Player.js:813` | `get isInvulnerable() { return this.invulnTimer > 0; }` | 扩展它就能免费获得"吸收后短暂免判"，判定链一行不用改 |
| `Player.js:514` | `if (this.invulnTimer > 0) this.invulnTimer -= dt;` | 新计时器的递减放这里 |
| `Player.js:797-803` | `_applyFlash()`：`flashing = invulnTimer > 0 && Math.floor(invulnTimer*12)%2===0` → `emissive` 闪红 | **护盾吸收绝不能复用 `invulnTimer`**：整只熊会闪红，语义误导（玩家以为掉血） |
| `ObstacleManager.js:364-368` | 命中时 `it.resolved = true; result.hit = true;` | 同一次撞击**不会在后续帧重复扣盾**——护盾正确性的根基（实现时加断言） |

### 2.2 道具投放链（新增两种拾取物）

| 位置 | 事实 | 含义 |
|---|---|---|
| `ObstacleManager.js:24-26` | `OBSTACLE_SPEC = { coin:{build:A.makeCoin,halfDepth:0.5}, heal:{build:A.makeHealPack,halfDepth:0.5}, boost:{build:A.makeBoostPad,halfDepth:1.4} }` | 新道具 = 加 2 个条目 |
| `ObstacleManager.js:91-92` | `meterSinceHeal += delta; meterSinceBoost += delta;`（`delta = speed*dt`，即**按米累积**） | 照抄成 `meterSinceShield/Magnet` |
| `ObstacleManager.js:196-219` | 投放优先级 **boost(240m) > heal(300m) > coin(72% 概率，3–6 枚，间距 1.05m)**；每条命中即 `return` | **一行只出一种道具**的约束必须保持 |
| `ObstacleManager.js:194` | `if (!freeLanes.length) return;` | 道具永不出现在被占道（修复后含 mover 两条道） |
| `ObstacleManager.js:269-292` | `_addItem(kind,lane,z,x)`；`coin` baseY=1.0、`heal` 1.05、`boost` 0 | 新道具需要自己的 baseY |
| `ObstacleManager.js:296-310` | `_obtain()` 懒建、`_releaseItem()` 按 `it.kind` 回收进 `pools[kind]` | **新 kind 自动获得对象池**，无需额外池代码 |
| `ObstacleManager.js:100-111` | coin 自转、heal 浮动+慢转（`it.phase`） | 新道具的待机动画模板 |

### 2.3 采集 / 威胁链（磁铁的安全边界）

| 位置 | 事实 | 含义 |
|---|---|---|
| `ObstacleManager.js:316,323` | `laneTol = CFG.LANE_TOLERANCE`(0.92)；`if (Math.abs(it.x - player.x) > laneTol) continue;` | **磁铁不需要新碰撞代码**：把金币 `x` 拉进 0.92 内，既有代码自动结算 |
| `ObstacleManager.js:325-328` | z 扫掠：`prevZ <= halfDepth+0.45 && it.z >= -(halfDepth+0.45)` | 高速低帧率也不穿透；牵引只改 x，不影响此判定 |
| `ObstacleManager.js:375-393` | `getThreats()` 给雷达自动驾驶用；**:380 按 kind 白名单跳过 `coin/heal/boost`** | **隐形地雷**：新 kind 不登记 → AI 把道具当障碍躲 |
| `InputSource.js:426,462`（对方在改） | 自动驾驶读 `threats[i].lane` / 已有 `threats[i].lanes` | 本设计**不动**该文件；只在 ObstacleManager 侧登记白名单 |

### 2.4 主控 / 重置 / 计时器模板

| 位置 | 事实 | 含义 |
|---|---|---|
| `Game.js:413-420` | `boostTimer -= dt` → 归零时 `hud.setBoost(false)`；`speed = base * (boostTimer>0 ? BOOST.speedMul : 1)` | 计时器递减的**唯一正确位置**（只在 `playing` 分支） |
| `Game.js:429` | `this.obstacles.update(dt, this.speed, this.distance)` | 磁铁参数从这里注入 |
| `Game.js:434` | `_resolveCollisions()` 只在 `playing` 调用 | dying/paused/menu **不会**误触发吸收 ✓ |
| `Game.js:437` | `if (this.boostTimer > 0) this.particles.boost(...)` | 持续型反馈的节流模板 |
| `Game.js:352-371` | `toMenu()` 重置 `hp/distance/score/coins/boostTimer/deathTimer` + `hud.setBoost(false)` + `particles.clear()` | 新计时器**必须**加进这里（否则回菜单后护盾还在） |
| `Game.js:457,483` | dying 分支传 `distance`；paused/over 只呼吸相机 | dying 时磁铁应传 `null` |

### 2.5 表现层资源

| 位置 | 事实 |
|---|---|
| `Art.js:15` | `var C = RR.CFG.COLORS;` → 调色板真身在 `config.js:187` |
| `config.js:237-241` | `honey:0xf7c531, honeyDark:0xcf9612, honeyGlow:0xffe066, apple:0xe0432f, appleDark:0xa82c1c` |
| `Art.js:471 / 496 / 527` | `makeCoin`≈**4 mesh**（罐/颈/盖/蜜滴）、`makeHealPack`≈**5**（苹果/暗块/柄/叶/高光）、`makeBoostPad`≈**11**（蜜滩/2 木轨/4 气泡/4 箭头） |
| `Art.js:1272-1274` | 导出表：`{ makeCoin, makeHealPack, makeBoostPad }` |
| `Audio.js:152-259` | `var SFX = { jump, land, slide, lane, coin(t,k), heal, boost, hurt, axe, chase, death, gameover, best, region, ui }`；`sfx()` 查表（`:446`）+ `stats.byName` 计数（`:455`） |
| `index.html:47-48` | `#hearts`、`#boost-tag.boost-tag.hidden`（唯一状态标签） |
| `style.css:98` | `.boost-tag { ... }` |
| `Hud.js` | `setBoost(on)` 是状态标签模板；`setHearts(hp,max)` **忽略 `max`**（既有 bug，非本设计范围） |

---

## 3. 设计一：护盾（Honeycomb Shield）

### 3.1 手感定义

- 拾取**立即生效**，外观为一层蜂巢色半透明球壳裹住熊。
- **吸收一次撞击**（不是限时无敌）：撞上后球壳碎裂消失，本次伤害被吃掉。
- 未消耗则 **12s 到期作废**（防止一路屯着养老）。
- 吸收后给 **0.7s `graceTimer`**：免判但**不闪红**，避免同帧/相邻帧二次判定与视觉误导。
- 不改变移动能力、不影响分数倍率与其他道具。
- **仅在难度第 3/4 区可拾取**（已裁决，见 §4.6）。

### 3.2 数值

```js
// config.js
CFG.SHIELD = { absorbTime: 12, graceTime: 0.7 };
CFG.PICKUP.shieldEveryMeters = 420;
CFG.PICKUP.powerUpStage = 2;          // 第 3 区(450m)起才投放；计量也只在门后累积（§4.6）
```

### 3.3 关键实现选择：为什么新增 `graceTimer` 而不是复用 `invulnTimer`

`Player.js:797-803` 的闪红完全由 `invulnTimer` 驱动，且 `isInvulnerable`(`:813`) 只读它。复用会导致：吸收瞬间整只熊闪红（玩家以为掉血）。

最小改法（2 处，均为追加）：

```js
// Player.js
this.graceTimer = 0;                                   // 字段区，紧邻 invulnTimer 初始化
if (this.graceTimer > 0) this.graceTimer -= dt;        // 紧邻 :514
if (this.hurtFlash > 0) ...                            // 原样保留
get isInvulnerable() { return this.invulnTimer > 0 || this.graceTimer > 0; }  // :813
```

收益：`Game.js:514` 的判定链**一行都不用改**就获得了"吸收后短暂免判"，且不触发闪红。

### 3.4 吸收落点（`Game.js:514`，唯一入口）

```js
if (r.hit && !this.player.isInvulnerable) {
  if (this.shieldTimer > 0) {
    this.shieldTimer = 0;                       // 消耗
    this.player.graceTimer = CFG.SHIELD.graceTime;
    this.player.setShield(false);               // 球壳碎裂隐藏
    this.hud.setShield(false);
    SFX('shield');
    this.world.addShake(0.5);                   // 比真实受击(1.0)轻
    this.particles.hit(px, py + 0.7, 0);
    return;                                     // 必须在 hp-- 之前返回
  }
  this.hp--;                                    // ↓ 以下原逻辑完全不动
  this.player.takeHit();
  ...
}
```

正确性根基：`ObstacleManager.js:365` 已在 `checkCollisions` 内置 `it.resolved = true` → 同一次撞击只在**一帧**进入此分支，不会连续扣两次盾。

### 3.5 交互矩阵

| 对象 / 状态 | 行为 |
|---|---|
| 滚轮箱（占两条道） | 吸收一次后箱子**仍会继续扫过该道**，玩家仍需自己躲第二下 —— 设计意图：护盾是"免死一次"，不是"穿墙" |
| 矮栏（`clearHeight`） | 同上，按 `Player.bottom` 判定，吸收后免判 0.7s |
| 悬吊物（`gapBottom`） | 同上 |
| combo（两道全堵） | 与普通障碍一致：吸收一次 |
| 加速带 / 苹果 / 金币 | 无交互，可任意叠加 |
| `dying` / `paused` / `menu` | `_resolveCollisions` 不调用（`Game.js:434`）→ 不会误吸收；计时器递减也只在 `playing`（对齐 `boostTimer`） |
| 自动驾驶 | 护盾会被 AI 顺路吃；不改变 AI 逻辑；**新 kind 必须登记 `getThreats` 白名单**（见 §4.4） |

---

## 4. 设计二：磁铁（Lodestone）

### 4.1 手感定义

- 持续 **7s**，把半径内的**金币**横向吸向玩家所在道；进入 0.92 容差后被既有采集链自动吃掉。
- **只吸金币**：不吸苹果、不吸加速带、不吸护盾/磁铁（不改变回血与加速经济）。
- 不移动玩家（`player.x` 全程不变），因此不会把玩家拽进危险。
- **仅在难度第 3/4 区可拾取**（已裁决，见 §4.6）。

### 4.2 数值

```js
CFG.MAGNET = { duration: 7, radius: 6.5, pullK: 9.0 };
CFG.PICKUP.magnetEveryMeters = 360;
```

`radius 6.5` 在最高速 25 m/s（`SPEED.max`）下约等于 **0.26s** 牵引窗口；若观感偏弱，可提到 9（代价：金币"隔两条道飞过来"会变夸张）。

### 4.3 实现落点：为什么几乎零风险

- 采集判定是纯几何的：`Math.abs(it.x - player.x) <= 0.92` + z 扫掠（`:323-328`）。**只要把金币的 `x` 拉进容差，既有代码自己结算**，不需要任何新碰撞分支、不需要改 `result` 结构。
- 牵引放在道具更新循环内（`:95-118`），由 `update()` 多接一个轻量参数：

```js
// ObstacleManager.js
update(dt, speed, distance, magnet) {          // :86 签名 + 1 参
  ...
  for (i = this.items.length - 1; i >= 0; i--) {
    it = this.items[i];
    ...
    if (magnet && magnet.on && it.kind === 'coin' && !it.collected) {
      if (it.z > -1.5 && it.z < magnet.radius && Math.abs(it.x - magnet.x) < magnet.radius) {
        it.x += (magnet.x - it.x) * Math.min(1, dt * CFG.MAGNET.pullK);
        if (it.mesh) it.mesh.position.x = it.x;
      }
    }
  }
}
```

```js
// Game.js
this.obstacles.update(dt, this.speed, this.distance, this._magnet());   // :429
this.obstacles.update(dt, this.speed, this.distance, null);            // :457 dying 不吸

_magnet() {
  if (this.magnetTimer <= 0) return null;
  return { on: true, x: this.player.x, radius: CFG.MAGNET.radius };
}
```

- **硬前置**：磁铁只对"本就落在 `freeLanes` 上的金币"安全。本轮之前，`_spawnRow` 按名义单道记账、而 mover 实际扫两条道 → 金币会生成在箱子扫掠道上。此时磁铁会把它们横向拉过箱子路径 = 玩家无法解释的死亡。本轮修复已守住"一行里至少一条道从头到尾没被扫到"的不变量 → **磁铁必须晚于该修复上线**。

### 4.4 必须改的一行"隐形地雷"

```js
// ObstacleManager.js:380（现状）
if (it.kind === 'coin' || it.kind === 'heal' || it.kind === 'boost') continue;
```

新 kind `shield` / `magnet` 若不登记，雷达自动驾驶（`InputSource.js:426/462`）会把道具当障碍去躲 —— 行为诡异且极难排查。建议改为道具白名单常量集中维护：

```js
var PICKUPS = { coin: 1, heal: 1, boost: 1, shield: 1, magnet: 1 };
if (PICKUPS[it.kind]) continue;
```

### 4.5 交互矩阵

| 对象 / 状态 | 行为 |
|---|---|
| 金币 | 半径内横向牵引 → 自动采集；z 速度仍由世界速度决定（高速时窗口变短） |
| 苹果 / 加速带 / 护盾 / 磁铁 | **不吸**（经济不变） |
| 障碍（含 mover） | 不吸、不改 x → 玩家位置不由磁铁改变，安全 |
| 加速带同时生效 | 可以叠加；加速期间金币相对位移更快，采集率提升有限 |
| `dying` / `paused` / `menu` | 传 `null` / 不调用 → 不牵引 |
| 自动驾驶 | 金币采集率上升 → **基线分数会变化**，验收比对应取"修复后基线"重测 |

---

### 4.6 投放分区：仅第 3/4 区（已裁决，护盾 + 磁铁共用）

现状：难度分区是 `CFG.DIFFICULTY`（`config.js:67-72`，`at` = 0 / 180 / 450 / 800），`ObstacleManager` 通过 `get stage()`（`:66-73`）与 **`get stageIndex()`（`:75-82`）** 读取 —— 两者都是**只读 getter，无 setter**，外部赋值会静默失败。第 3 区 = `stageIndex 2`（450m），与 `mover` 首次出现的分区一致。

推荐实现（复用既有 getter，不动 `DIFFICULTY` 结构）：

```js
// config.js
CFG.PICKUP.powerUpStage = 2;        // 第 3 区（450m）起

// ObstacleManager.js —— 计量只在门后累积，避免一进第 3 区就连续掉两个道具
if (this.stageIndex >= CFG.PICKUP.powerUpStage) {
  this.meterSinceShield += delta;
  this.meterSinceMagnet += delta;
}

// 投放分支（插在 heal 之后、coin 之前，保持一行一 return 的既有约束）
if (this.meterSinceShield >= CFG.PICKUP.shieldEveryMeters) { this.meterSinceShield = 0; /* ... */ }
if (this.meterSinceMagnet >= CFG.PICKUP.magnetEveryMeters) { this.meterSinceMagnet = 0; /* ... */ }
```

两个必须注意的后果：

1. **计量若不设门**：`meterSince*` 从 0m 起就在累积，进第 3 区（450m）时早已超过阈值 → 护盾与磁铁会在**同一个分区边界**连续冒出，节奏突兀。故采用门后累积。
2. **既有 `meterSinceHeal/Boost` 不要跟着加门**：苹果与加速带从开局就该出，加门会改变现有难度曲线与回血经济。

备选实现（更贴合既有设计语言，但要动 4 条分区数据）：给 `DIFFICULTY` 每区加 `pickups: ['shield', 'magnet']` 之类的数组，像读 `kinds` 一样读。代价是要同步维护 4 个条目、且 `stage` 只读需新增读取逻辑 —— 本轮不选。

---

## 5. 逐文件改动点

核心 6 文件 + 外壳 3 文件（外壳可一次性做完，互不耦合）。

| # | 文件 | 锚点 | 改动 | 估计 |
|---|---|---|---|---|
| 1 | `js/config.js` | `COLORS`(`:187` 区块)、`PICKUP`、新块 | 加色值（护盾可**纯复用** `honey/honeyGlow/honeyDark`，磁铁复用 `apple` + 需 1 个钢灰新键）、`PICKUP` +2 字段、新增 `SHIELD{}` / `MAGNET{}` 两块 | ~12 行 |
| 2 | `js/Player.js` | `:514`、`:813`、`root` 子节点、`dispose()` | `graceTimer` 字段+递减、getter 扩展、蜂巢球壳 mesh（挂在 **`root`** 下，**不挂 `body`**——`body` 会随压扁/翻滚变形）、`setShield(on)` | ~25 行 |
| 3 | `js/Game.js` | `:352`(重置)、`:413-420`(递减)、`:429`(注入)、`:486-531`(分支) | 2 个计时器字段、`start()`/`toMenu()` 重置、playing 分支递减+到期关 HUD、`_resolveCollisions` 2 个新分支、`_magnet()` 助手 | ~35 行 |
| 4 | `js/ObstacleManager.js` | `:24-26`、`:75-82`、`:91-92`、`:196-219`、`:273-275`、`:86`、`:380` | `OBSTACLE_SPEC` +2 条目、`meterSinceShield/Magnet`、`meterSinceShield/Magnet` **仅门后累积**（§4.6）、投放优先级（boost > heal > **shield > magnet** > coin，保持"一行一 return"）、baseY、`update()` 第 4 参 + 牵引、`getThreats` 白名单 | ~34 行 |
| 5 | `js/Art.js` | `:468-562` 区域、`:1272-1274` | `makeShieldPack()`（蜂巢盾，~5 mesh）、`makeMagnet()`（马蹄磁铁，~6 mesh）、导出表 +2 键 | ~70 行 |
| 6 | `js/Hud.js` | `el` 表、`setBoost` 旁 | `el.shieldTag` / `el.magnetTag` + `setShield/setMagnet`（照抄 `setBoost`） | ~14 行 |
| 7 | `index.html` | `:48` 旁 | 新增 `#shield-tag` / `#magnet-tag`（带 `hidden`） | 2 行 |
| 8 | `style.css` | `:98` 旁 | 2 个标签样式（建议把 `.boost-tag` 泛化为 `.power-tag` + 修饰类，避免三份重复） | ~12 行 |
| 9 | `js/Audio.js` | `SFX` 表 `:152-259` | `SFX.shield`（碎裂：噪声短促下扫）、`SFX.magnet`（吸附：上扬双音） | ~16 行 |

**资源释放**：`Player.dispose()`（`:820-831`）已遍历 `root` 释放 geometry/material → 只要球壳挂在 `root` 下就自动被回收，不必额外代码。

---

## 6. 性能代价

| 项 | 代价 | 依据 |
|---|---|---|
| 新增 2 种拾取物 | shield ~5 mesh / magnet ~6 mesh，与 `heal`(5) / `coin`(4) 同量级；**池化后稳态零分配**（`_obtain`/`_releaseItem` 自动按 kind 建池） | `Art.js:471/496/527`、`ObstacleManager.js:296-310` |
| 蜂巢球壳 | **+1 draw call**（`IcosahedronGeometry(r,1)` + `MeshBasicMaterial{transparent, opacity, depthWrite:false, blending:Additive}`）。**不用 ShaderMaterial**，省一次 shader 编译与 uniform 维护 | 与粒子"固定管线差值=2"同法可测 |
| 磁铁持续期 | 无新增 mesh；牵引只改既有金币 `x`；反馈粒子走既有 `coin` 池并节流（建议 0.12s），**池上限不扩容** | `PARTICLES` 容量 dust 260 / spark 200 |
| 主菜单 | 不生成道具 → 主菜单 draw call **不变**（基线 829） | 实测 |
| 预期进行中 draw call | 1145–1520 → 护盾激活期 **+1~2**；磁铁激活期 **+0~1** | 用 `cdp_particles.py` 的静止差值法复测 |

---

## 7. 验收口径（探针设计）

新建 `.deepworks/tmp/cdp_powerups.py`，沿用 `cdp_mover.py` 的架构（**功能断言不截图**、`--port=` 换端口避缓存、**样本数 0 判 INVALID 硬闸**）。

| # | 用例 | 断言 |
|---|---|---|
| 1 | 投放节律 | 模拟 3000m：`shield` 出现 ∈ [5,10]、`magnet` ∈ [6,12]；**样本 0 → INVALID** |
| 2 | 护盾吸收 | 强制 `shieldTimer=6` 后撞 solid 障碍：`hp` 不变、`shieldTimer==0`、`graceTimer≈0.7`、`particles.byName.hit>0`、**同一次撞击 hp 只被判定一次** |
| 3 | 吸收不闪红 | 吸收瞬间 `player.materials[0].emissive.r < 0.1`（真实受击对照 > 0.4） |
| 4 | 到期失效 | 推进 `absorbTime` 后 `shieldTimer==0`，再撞 → `hp--` |
| 5 | 磁铁牵引 | 三道塞满金币 + `magnetTimer>0`：2s 内 `coins` 增长、被吸金币 `\|x-player.x\|→<0.92`、**`player.x` 不变** |
| 6 | 不吸非金币 | 半径内的 `heal`/`boost` 的 `x` 不变 |
| 7 | 白名单回归 | 强制生成两类道具后 `getThreats()` 中**不含** `shield`/`magnet`（防 AI 误判） |
| 8 | 重置 | `start()` / `toMenu()` 后 `shieldTimer==magnetTimer==0`、HUD 标签隐藏、球壳 `visible===false` |
| 9 | 状态隔离 | `paused` / `dying` / `menu` 下计时器**不递减**（暂停不吃护盾时间） |
| 10 | 零报错 | `console errors: none` + 重连 0（沿用既有口径） |
| 11 | **分区门**（§4.6） | 450m 之前：`shield`/`magnet` 投放计数 **= 0** 且 `meterSinceShield`/`meterSinceMagnet` 保持 0；过 450m 后开始出现 |

补充：交付前按既有流程对**解压后的交付包**跑一遍 `file://` 复验（换新端口）。

---

## 8. 风险与回滚

| 风险 | 触发 | 缓解 | 回滚 |
|---|---|---|---|
| 难度下降（护盾过强） | 护盾堆积 | 吸收一次 + 12s 期限 + 420m 节律 | `PICKUP.shieldEveryMeters = Infinity` |
| 磁铁收益过高 | 金币采集率暴涨 | 只吸金币 + 7s + 360m | `MAGNET.duration = 0` / 投放关掉 |
| 吸收分支 `return` 位置写错 | 实现失误 | 用例 #2 覆盖"hp 只判定一次" | 单点还原 `Game.js` 该 if |
| `getThreats` 白名单漏登记 | 新增 kind | 用例 #7 回归 | 加一行 |
| 磁铁踩到旧记账 bug | 先上磁铁后修 mover | §4.3 硬前置：**修复已先行** | 关掉磁铁投放 |
| 暂停中护盾偷偷到期 | 递减放错位置 | 用例 #9 | 递减移回 playing 分支 |

**回滚一致性**：回滚后与 `outputs\bear-run-20261020e.zip`（SHA256 `10D95F82…EE2F3F`）逐文件 diff 即可确认无残留。

---

## 9. 明确不做 / 待裁决

1. **不做**"限时无敌 + 平移穿墙"式护盾（会破坏躲闪玩法与自动驾驶基线）。
2. **不吸**障碍、苹果、加速带（保持经济与难度曲线）。
3. **不改** `InputSource.js`（并发会话正在修改）；只在其依赖的 `ObstacleManager.getThreats()` 侧登记白名单。
4. ~~待裁决 A~~ **已裁决 = 吸收一次**；原候选：护盾取"**吸收一次**"（推荐，改动小、语义清晰）还是"限时无敌 N 秒"？
5. ~~待裁决 B~~ **已裁决 = 仅第 3/4 区投放**（`powerUpStage = 2`，见 §4.6）；原候选：两种道具**全程投放**，还是与 mover 同步只在第 3/4 区（`DIFFICULTY` `:70-71`）出现？后者更克制。
6. **待裁决 C（仍开放）**：护盾外观采"蜂巢盾"（纯复用现有 honey 色系）还是另设钢蓝配色（需在 `COLORS` 新增键）？
7. 附带项（非本设计）：`PLAYER.slideCooldown:0.15`（`config.js:45`）死配置；`Hud.setHearts(hp,max)` 忽略 `max`。

---

## 10. 建议实施顺序

1. 等用户信号（对方首次提交落地）。
2. **护盾**：改 2/3/4/5/6 + 外壳 7/8/9 → 跑用例 #1–#4、#8、#9。
3. **磁铁**：改 1/3/4 → 跑用例 #5–#7。
4. 一起跑 `cdp_particles.py` 回归（draw call 增量断言）+ `pack.ps1` 重打包（包名自动按 `?v=` stamp 推导）+ 对解压包复验。
5. 之后：角色数值差异。

---

## 附：取证命令（可复现）

```powershell
# 受击/无敌链
rg -n "isInvulnerable|invuln|_applyFlash|hurtFlash" radar-runner\js\Player.js
# 受击结算与道具结算
rg -n "checkCollisions|r\.hit|r\.heal|r\.boost|boostTimer" radar-runner\js\Game.js
# 投放链与威胁白名单
rg -n "OBSTACLE_SPEC|meterSince|_addItem|PICKUP|getThreats|kind === 'coin'" radar-runner\js\ObstacleManager.js
# 拾取物 mesh 与导出表
rg -n "makeCoin|makeHealPack|makeBoostPad|var C = " radar-runner\js\Art.js
# 音效表
rg -n "var SFX|^    \w+: function \(t" radar-runner\js\Audio.js
# 色值真身
rg -n "COLORS|honey:|apple:" radar-runner\js\config.js
# HUD 标签锚点
rg -n "boost-tag|hearts" radar-runner\index.html; rg -n "boost-tag" radar-runner\style.css
```
