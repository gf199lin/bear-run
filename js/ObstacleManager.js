/**
 * 障碍与拾取物管理。
 *
 * 设计原则：任何一行都必须至少留一条可通行车道（绝不出现三条道全堵的死局）。
 * 障碍用颜色做功能编码，和马里奥一样"看一眼就知道该怎么办"：
 *   红色警示矮栏  → 跳
 *   绿色悬吊管道  → 滑铲
 *   灰色混凝土堆  → 换道
 *   带条纹的滚轮箱 → 横向滑动，需预判
 * 金币、医疗箱、加速带都放在空车道上，天然引导玩家走安全路线。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;
  var A = RR.Art;

  var OBSTACLE_SPEC = {
    low:   { build: A.makeLowBarrier,  halfDepth: 0.36, clearHeight: 0.92 },
    high:  { build: A.makeHighBarrier, halfDepth: 0.4,  gapBottom: 1.18 },
    block: { build: A.makeRubbleBlock, halfDepth: 0.72, solid: true },
    mover: { build: A.makeMover,       halfDepth: 0.72, solid: true },
    coin:  { build: A.makeCoin,        halfDepth: 0.5 },
    heal:  { build: A.makeHealPack,    halfDepth: 0.5 },
    boost: { build: A.makeBoostPad,    halfDepth: 1.4 }
  };

  class ObstacleManager {
    constructor(scene) {
      this.scene = scene;
      this.items = [];
      this.pools = {};
      // 生成开关：测试模式会关掉它，之后既不出障碍也不出拾取物
      this.spawning = true;
      this.reset();
    }

    // ==================== 生命周期 ====================

    /**
     * 测试模式用：开 / 关"生成"。
     * 关掉时顺手清空场上已有对象，保证进去就是一片干净的空地；
     * 再打开（回主菜单 / 正常开局）时恢复原本的生成逻辑。
     */
    setEnabled(on) {
      this.spawning = !!on;
      if (!this.spawning) {
        for (var i = this.items.length - 1; i >= 0; i--) this._releaseItem(this.items[i]);
        this.items.length = 0;
      }
    }

    reset(prefill) {
      if (prefill === undefined) prefill = true;
      for (var i = this.items.length - 1; i >= 0; i--) this._releaseItem(this.items[i]);
      this.items.length = 0;

      this.distance = 0;
      this.speed = CFG.SPEED.start;   // gap 需要用当前速度换算（见 get gap）
      this.mercyMetersLeft = 0;       // 掉血后的喘息里程（grantMercy 会点亮它）
      this.moverPhase = 0;
      this.meterSinceHeal = 0;
      this.meterSinceBoost = 0;
      this.lastKind = null;

      if (prefill && this.spawning) {
        // 预填充前方，避免开局长时间空跑（约 3.5 秒后遇到第一个障碍）
        var z = -32;
        while (z > -CFG.WORLD.visibleLength) {
          this._spawnRow(z);
          z -= this.gap;
        }
      }
      this.spawnCursor = this.gap * 0.6;
    }

    /**
     * 两行之间隔多少米 = 当前速度 x 当前"时间间隔"。
     * 时间间隔随里程从 timeGapStart 收到 timeGapMin，所以随着速度上升，
     * 米数反而越拉越开，但真正决定难度的"反应时间"在稳步变短。
     */
    get gap() {
      var t = Math.min(1, this.distance / CFG.SPAWN.rampMeters);
      var tg = CFG.SPAWN.timeGapStart + (CFG.SPAWN.timeGapMin - CFG.SPAWN.timeGapStart) * t;
      var v = this.speed || CFG.SPEED.start;
      return Math.max(CFG.SPAWN.minGap, v * tg);
    }

    /** 掉血后给一段喘息：这段时间内不生成双障碍 / combo，节奏能重新找回来 */
    grantMercy() {
      this.mercyMetersLeft = CFG.SPAWN.mercyMeters;
    }

    get stage() {
      var stages = CFG.DIFFICULTY;
      var s = stages[0];
      for (var i = 0; i < stages.length; i++) {
        if (this.distance >= stages[i].at) s = stages[i];
      }
      return s;
    }

    get stageIndex() {
      var stages = CFG.DIFFICULTY;
      var idx = 0;
      for (var i = 0; i < stages.length; i++) {
        if (this.distance >= stages[i].at) idx = i;
      }
      return idx;
    }

    // ==================== 每帧 ====================

    update(dt, speed, distance) {
      this.distance = distance;
      this.speed = speed;
      this.moverPhase += dt * 1.75;

      var delta = speed * dt;
      this.meterSinceHeal += delta;
      this.meterSinceBoost += delta;
      if (this.mercyMetersLeft > 0) {
        this.mercyMetersLeft = Math.max(0, this.mercyMetersLeft - delta);
      }

      var i, it;
      for (i = this.items.length - 1; i >= 0; i--) {
        it = this.items[i];
        it.prevZ = it.z;          // 扫掠碰撞检测需要上一帧位置
        it.z += delta;

        if (it.mesh) {
          it.mesh.position.z = it.z;

          if (it.kind === 'mover') {
            it.x = it.baseX + Math.sin(this.moverPhase + it.phase) * it.halfSpan;
            it.mesh.position.x = it.x;
          } else if (it.kind === 'coin') {
            it.mesh.rotation.y += dt * 3.4;
          } else if (it.kind === 'heal') {
            it.mesh.position.y = it.baseY + Math.sin(this.moverPhase * 2.2 + it.phase) * 0.11;
            it.mesh.rotation.y += dt * 1.1;
          }
        }

        if (it.z > CFG.WORLD.recycleBehind + 8) {
          this._releaseItem(it);
          this.items.splice(i, 1);
        }
      }

      // 生成新的一行（测试模式关掉生成：场上永远是空跑道）
      if (!this.spawning) return;

      this.spawnCursor -= delta;
      var guard = 0;
      while (this.spawnCursor <= 0 && guard++ < 8) {
        this._spawnRow(-CFG.WORLD.visibleLength);
        this.spawnCursor += this.gap * A.rand(CFG.SPAWN.jitter[0], CFG.SPAWN.jitter[1]);
      }
    }

    // ==================== 生成 ====================

    _spawnRow(z) {
      var kinds = this.stage.kinds;
      var lanes = [0, 1, 2];
      var spawns = [];

      // 掉血后的喘息段：这一行最多只堵一条道，让玩家把节奏找回来
      var mercy = this.mercyMetersLeft > 0;
      var comboChance = mercy ? 0 : (this.stage.comboChance || 0);
      var doubleChance = mercy ? 0 : (this.stage.doubleChance || 0);

      // combo：两条道堵死，只留一条
      if (kinds.indexOf('combo') >= 0 && Math.random() < comboChance) {
        var free = A.pick(lanes);
        for (var l = 0; l < 3; l++) {
          if (l !== free) spawns.push({ lane: l, kind: 'block', lanes: [l] });
        }
      } else {
        var kind = A.pick(kinds);
        if (kind === 'combo') kind = 'block';
        var lane = A.pick(lanes);
        var first = { lane: lane, kind: kind, lanes: [lane] };

        // 滚轮箱在相邻两条道之间来回滑，真实占道是两条。必须在这里定下是哪两条，
        // 否则后面的 occupied / freeLanes 都会按"只堵一条"来算，金币会掉在箱子上。
        if (kind === 'mover') {
          first.pair = Math.random() < 0.5 ? [0, 1] : [1, 2];
          first.lane = first.pair[0];
          first.lanes = first.pair.slice();
        }
        spawns.push(first);

        // 有一定概率再来一个不同车道的障碍，仍然留一条活路
        var covered = first.lanes;
        var rest = [];
        for (var r = 0; r < lanes.length; r++) {
          if (covered.indexOf(lanes[r]) < 0) rest.push(lanes[r]);
        }
        // 第一件是滚轮箱时只剩一条道 -- 再堵就是三线全封的死路，必须收手
        if (rest.length >= 2 && Math.random() < doubleChance) {
          var lane2 = A.pick(rest);
          // 滚轮箱一个人就要占两条道，而此刻只剩两条 -- 它一进来就把活路吃光，
          // 所以第二件只从"占一条道"的障碍里挑（combo 会退化成 block）
          var kinds2 = [];
          for (var k2 = 0; k2 < kinds.length; k2++) {
            var kk = kinds[k2] === 'combo' ? 'block' : kinds[k2];
            if (kk !== 'mover' && kinds2.indexOf(kk) < 0) kinds2.push(kk);
          }
          var kind2 = A.pick(kinds2);
          // 不要在同一条道上放 "跳过" 和 "滑铲" 之外的冲突组合
          spawns.push({ lane: lane2, kind: kind2, lanes: [lane2] });
        }
      }

      var occupied = {};
      for (var s = 0; s < spawns.length; s++) {
        var spLanes = spawns[s].lanes || [spawns[s].lane];
        for (var q = 0; q < spLanes.length; q++) occupied[spLanes[q]] = true;
      }

      var freeLanes = [];
      for (var f = 0; f < lanes.length; f++) {
        if (!occupied[lanes[f]]) freeLanes.push(lanes[f]);
      }

      for (var k = 0; k < spawns.length; k++) {
        this._addObstacle(spawns[k].kind, spawns[k].lane, z, spawns[k].pair);
      }

      if (!freeLanes.length) return;

      // ---- 加速带（优先，因为它占地面 2.7m 长）----
      if (this.meterSinceBoost >= CFG.PICKUP.boostEveryMeters) {
        this.meterSinceBoost = 0;
        var boostLane = A.pick(freeLanes);
        this._addItem('boost', boostLane, z - 1.5, CFG.LANES[boostLane]);
        return;
      }

      // ---- 医疗箱 ----
      if (this.meterSinceHeal >= CFG.PICKUP.healEveryMeters) {
        this.meterSinceHeal = 0;
        var healLane = A.pick(freeLanes);
        this._addItem('heal', healLane, z, CFG.LANES[healLane]);
        return;
      }

      // ---- 金币串 ----
      if (Math.random() < CFG.PICKUP.coinRowChance) {
        var coinLane = A.pick(freeLanes);
        var n = Math.floor(A.rand(CFG.PICKUP.coinRowMin, CFG.PICKUP.coinRowMax + 0.999));
        for (var c = 0; c < n; c++) {
          this._addItem('coin', coinLane, z - c * 1.05, CFG.LANES[coinLane]);
        }
      }
    }

    _addObstacle(kind, lane, z, pair) {
      if (kind === 'mover') {
        // 在两条相邻车道之间来回滑动。pair 由 _spawnRow 定好，占道记账必须同源
        if (!pair) pair = Math.random() < 0.5 ? [0, 1] : [1, 2];
        var baseX = (CFG.LANES[pair[0]] + CFG.LANES[pair[1]]) / 2;
        var span = Math.abs(CFG.LANES[pair[1]] - CFG.LANES[pair[0]]) / 2;

        var mesh = this._obtain('mover');
        mesh.position.set(baseX, 0, z);
        this.scene.add(mesh);
        this.items.push({
          kind: 'mover',
          blockKind: 'mover',
          lane: pair[0],
          lanes: pair.slice(),
          pair: pair,
          x: baseX,
          baseX: baseX,
          halfSpan: span,
          phase: A.rand(0, Math.PI * 2),
          z: z,
          halfDepth: OBSTACLE_SPEC.mover.halfDepth,
          mesh: mesh,
          solid: true
        });
        return;
      }

      var spec = OBSTACLE_SPEC[kind];
      var m = this._obtain(kind);
      m.position.set(CFG.LANES[lane], 0, z);
      this.scene.add(m);

      this.items.push({
        kind: kind,
        blockKind: kind,
        lane: lane,
        x: CFG.LANES[lane],
        z: z,
        halfDepth: spec.halfDepth,
        clearHeight: spec.clearHeight,
        gapBottom: spec.gapBottom,
        solid: !!spec.solid,
        mesh: m
      });
    }

    _addItem(kind, lane, z, x) {
      var spec = OBSTACLE_SPEC[kind];
      var mesh = this._obtain(kind);

      var baseY = 0;
      if (kind === 'coin') baseY = 1.0;
      else if (kind === 'heal') baseY = 1.05;

      mesh.position.set(x, baseY, z);
      if (kind === 'coin') mesh.rotation.y = A.rand(0, Math.PI * 2);
      this.scene.add(mesh);

      this.items.push({
        kind: kind,
        lane: lane,
        x: x,
        z: z,
        baseY: baseY,
        halfDepth: spec.halfDepth,
        phase: A.rand(0, Math.PI * 2),
        mesh: mesh,
        collected: false
      });
    }

    // ==================== 对象池 ====================

    _obtain(kind) {
      var pool = this.pools[kind];
      if (pool && pool.length) return pool.pop();
      var builder = OBSTACLE_SPEC[kind].build;
      return builder();
    }

    _releaseItem(it) {
      if (it.mesh) {
        this.scene.remove(it.mesh);
        var pool = this.pools[it.kind] || (this.pools[it.kind] = []);
        pool.push(it.mesh);
        it.mesh = null;
      }
    }

    // ==================== 碰撞 ====================

    checkCollisions(player) {
      var result = { hit: false, obstacle: null, coins: 0, heal: 0, boost: false,
                     nearMiss: 0, nearMissBest: 0 };
      var laneTol = CFG.LANE_TOLERANCE;
      var playerReach = 0.45;
      var margin = CFG.NEAR_MISS.margin;
      var xMargin = CFG.NEAR_MISS.xMargin;

      for (var i = this.items.length - 1; i >= 0; i--) {
        var it = this.items[i];
        if (it.collected || it.resolved) continue;

        var dx = Math.abs(it.x - player.x);
        var inLane = dx <= laneTol;

        // 扫掠检测：高速低帧率下也不能穿透
        var reach = it.halfDepth + playerReach;
        var prevZ = (it.prevZ === undefined) ? it.z : it.prevZ;
        var inSweep = (prevZ <= reach && it.z >= -reach);

        // ---- 拾取物：必须正好落在车道上 ----
        if (it.kind === 'coin' || it.kind === 'heal' || it.kind === 'boost') {
          if (!inLane || !inSweep) continue;

          it.collected = true;
          if (it.kind === 'coin') result.coins++;
          else if (it.kind === 'heal') result.heal++;
          else result.boost = true;

          this._releaseItem(it);
          this.items.splice(i, 1);
          continue;
        }

        // ---- 障碍：撞上 ----
        // 判定口径与旧版逐字一致，只是把两个前置条件换成了 inLane / inSweep。
        var blocked = false;
        if (inLane && inSweep) {
          if (it.solid) {
            blocked = true;
          } else if (it.clearHeight !== undefined) {
            blocked = player.bottom < it.clearHeight;      // 矮栏：跳得不够高就撞
          } else if (it.gapBottom !== undefined) {
            blocked = player.top > it.gapBottom;           // 悬吊物：没蹲下去就撞
          }
        }

        if (blocked) {
          it.resolved = true;
          result.hit = true;
          result.obstacle = it;
          continue;
        }

        // ---- 障碍：擦过去（险躲）----
        // 只认"障碍中心穿过玩家所在平面"的那一帧（prevZ < 0 <= z）：
        // 同一件障碍最多记一次，也不会和扫掠窗口里的多帧判定互相干扰。
        // 记账用独立标志 nearMissed —— resolved 的含义（"撞上了"）一点都不动。
        if (it.nearMissed || !(prevZ < 0 && it.z >= 0)) continue;

        var left;
        if (it.solid) {
          // 实心堆：只能换道绕，所以看"横向让出了多少"（还在道上就是撞，走不到这里）
          if (dx > laneTol + xMargin) continue;
          left = dx - laneTol;
        } else if (it.clearHeight !== undefined) {
          // 矮栏：看"脚底离栏顶还剩多少"
          if (!inLane) continue;
          left = player.bottom - it.clearHeight;
        } else {
          // 悬吊物：蹲下后余量恒为 0.46 米，收不进 margin，不参与险躲
          continue;
        }

        it.nearMissed = true;
        if (left >= 0 && left <= (it.solid ? xMargin : margin)) {
          result.nearMiss++;
          if (result.nearMissBest === 0 || left < result.nearMissBest) {
            result.nearMissBest = left;
          }
        }
      }

      return result;
    }

    /** 给雷达模拟器（自动驾驶）用：列出前方的威胁 */
    getThreats() {
      var out = [];
      for (var i = 0; i < this.items.length; i++) {
        var it = this.items[i];
        if (it.collected) continue;
        if (it.kind === 'coin' || it.kind === 'heal' || it.kind === 'boost') continue;

        var lane = it.lane;
        var covers = it.lanes || [it.lane];
        if (it.kind === 'mover') {
          // 此刻真正压在哪条道上；整条循环里它会扫过 pair 两条（covers 里是全集）
          var lx = it.x;
          lane = Math.abs(lx - CFG.LANES[0]) < 1.1 ? 0 : (Math.abs(lx - CFG.LANES[2]) < 1.1 ? 2 : 1);
        }

        out.push({ kind: it.blockKind, lane: lane, lanes: covers, z: -it.z });
      }
      return out;
    }

    get count() { return this.items.length; }
  }

  RR.ObstacleManager = ObstacleManager;

})(window);
