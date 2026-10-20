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
      this.reset();
    }

    // ==================== 生命周期 ====================

    reset(prefill) {
      if (prefill === undefined) prefill = true;
      for (var i = this.items.length - 1; i >= 0; i--) this._releaseItem(this.items[i]);
      this.items.length = 0;

      this.distance = 0;
      this.moverPhase = 0;
      this.meterSinceHeal = 0;
      this.meterSinceBoost = 0;
      this.lastKind = null;

      if (prefill) {
        // 预填充前方，避免开局长时间空跑（约 3.5 秒后遇到第一个障碍）
        var z = -32;
        while (z > -CFG.WORLD.visibleLength) {
          this._spawnRow(z);
          z -= this.gap;
        }
      }
      this.spawnCursor = this.gap * 0.6;
    }

    get gap() {
      var t = Math.min(1, this.distance / CFG.SPAWN.rampMeters);
      return CFG.SPAWN.startGap + (CFG.SPAWN.minGap - CFG.SPAWN.startGap) * t;
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
      this.moverPhase += dt * 1.75;

      var delta = speed * dt;
      this.meterSinceHeal += delta;
      this.meterSinceBoost += delta;

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

      // 生成新的一行
      this.spawnCursor -= delta;
      var guard = 0;
      while (this.spawnCursor <= 0 && guard++ < 8) {
        this._spawnRow(-CFG.WORLD.visibleLength);
        this.spawnCursor += this.gap * A.rand(0.86, 1.18);
      }
    }

    // ==================== 生成 ====================

    _spawnRow(z) {
      var kinds = this.stage.kinds;
      var lanes = [0, 1, 2];
      var spawns = [];

      // combo：两条道堵死，只留一条
      if (kinds.indexOf('combo') >= 0 && Math.random() < 0.2) {
        var free = A.pick(lanes);
        for (var l = 0; l < 3; l++) {
          if (l !== free) spawns.push({ lane: l, kind: 'block' });
        }
      } else {
        var kind = A.pick(kinds);
        if (kind === 'combo') kind = 'block';
        var lane = A.pick(lanes);
        spawns.push({ lane: lane, kind: kind });

        // 有一定概率再来一个不同车道的障碍，仍然留一条活路
        if (Math.random() < 0.24) {
          var rest = [];
          for (var r = 0; r < lanes.length; r++) {
            if (lanes[r] !== lane) rest.push(lanes[r]);
          }
          var lane2 = A.pick(rest);
          var kind2 = A.pick(kinds);
          if (kind2 === 'combo') kind2 = 'block';
          // 不要在同一条道上放 "跳过" 和 "滑铲" 之外的冲突组合
          spawns.push({ lane: lane2, kind: kind2 });
        }
      }

      var occupied = {};
      for (var s = 0; s < spawns.length; s++) occupied[spawns[s].lane] = true;

      var freeLanes = [];
      for (var f = 0; f < lanes.length; f++) {
        if (!occupied[lanes[f]]) freeLanes.push(lanes[f]);
      }

      for (var k = 0; k < spawns.length; k++) {
        this._addObstacle(spawns[k].kind, spawns[k].lane, z);
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

    _addObstacle(kind, lane, z) {
      if (kind === 'mover') {
        // 在两条相邻车道之间来回滑动
        var pair = Math.random() < 0.5 ? [0, 1] : [1, 2];
        var baseX = (CFG.LANES[pair[0]] + CFG.LANES[pair[1]]) / 2;
        var span = Math.abs(CFG.LANES[pair[1]] - CFG.LANES[pair[0]]) / 2;

        var mesh = this._obtain('mover');
        mesh.position.set(baseX, 0, z);
        this.scene.add(mesh);
        this.items.push({
          kind: 'mover',
          blockKind: 'mover',
          lane: lane,
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
      var result = { hit: false, obstacle: null, coins: 0, heal: 0, boost: false };
      var laneTol = CFG.LANE_TOLERANCE;
      var playerReach = 0.45;

      for (var i = this.items.length - 1; i >= 0; i--) {
        var it = this.items[i];
        if (it.collected || it.resolved) continue;

        if (Math.abs(it.x - player.x) > laneTol) continue;

        // 扫掠检测：高速低帧率下也不能穿透
        var reach = it.halfDepth + playerReach;
        var prevZ = (it.prevZ === undefined) ? it.z : it.prevZ;
        if (!(prevZ <= reach && it.z >= -reach)) continue;

        if (it.kind === 'coin') {
          it.collected = true;
          result.coins++;
          this._releaseItem(it);
          this.items.splice(i, 1);
          continue;
        }

        if (it.kind === 'heal') {
          it.collected = true;
          result.heal++;
          this._releaseItem(it);
          this.items.splice(i, 1);
          continue;
        }

        if (it.kind === 'boost') {
          it.collected = true;
          result.boost = true;
          this._releaseItem(it);
          this.items.splice(i, 1);
          continue;
        }

        // ---- 障碍判定 ----
        var blocked = false;
        if (it.solid) {
          blocked = true;
        } else if (it.clearHeight !== undefined) {
          blocked = player.bottom < it.clearHeight;      // 矮栏：跳得不够高就撞
        } else if (it.gapBottom !== undefined) {
          blocked = player.top > it.gapBottom;           // 悬吊物：没蹲下去就撞
        }

        if (blocked) {
          it.resolved = true;
          result.hit = true;
          result.obstacle = it;
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
        if (it.kind === 'mover') {
          var lx = it.x;
          lane = Math.abs(lx - CFG.LANES[0]) < 1.1 ? 0 : (Math.abs(lx - CFG.LANES[2]) < 1.1 ? 2 : 1);
        }

        out.push({ kind: it.blockKind, lane: lane, z: -it.z });
      }
      return out;
    }

    get count() { return this.items.length; }
  }

  RR.ObstacleManager = ObstacleManager;

})(window);
