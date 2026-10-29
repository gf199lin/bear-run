/**
 * 世界：场景、光照、雾、天空、地面、路边景物。
 *
 * 采用"跑步机"模式：玩家永远待在 z≈0，所有景物向 +z 移动并循环回收，
 * 这样不会因为跑得远导致浮点精度问题。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;
  var A = RR.Art;
  var C = CFG.COLORS;

  class World {
    constructor(scene) {
      this.scene = scene;
      this.groundTiles = [];
      this.props = [];
      this.swayers = [];        // 会随风晃的景物（内层枢轴）
      this.clouds = [];
      this.birds = [];
      this.scroll = 0;
      this.time = 0;
      this.stageIndex = 0;

      this._buildLights();
      this._buildSky();
      this._buildGround();
      this._buildProps();
      this._buildClouds();
      this._buildBirds();
      this._buildRegionDecor();

      this.camera = new THREE.PerspectiveCamera(64, 1, 0.1, 600);
      this.camera.position.set(0, 3.1, 5.9);
      this.camTarget = new THREE.Vector3(0, 1.3, -9);
      this.camera.lookAt(this.camTarget);
      this.camShake = 0;
      this.camX = 0;
      this.cineMix = 0;         // 0 = 正常跟拍，1 = 处决侧机位

      // 配色从第 1 区出发，保证开局和以前一模一样
      this._palCur = this._resolvePalette(0);
      this._palGoal = this._resolvePalette(0);
      this._applyPalette(this._palCur);
    }

    // ---------- 光照：暖阳 + 苔绿反弹光 ----------
    _buildLights() {
      // 熊出没式的明快日光：天光偏蓝、地面反弹光偏草绿
      // 注意：Lambert 材质的亮度是各光源之和，卡通浅色调下总强度必须压在 1.2 左右，
      // 否则土路/草地会直接冲成一片白。
      var hemi = new THREE.HemisphereLight(0xd8ecff, 0x8d8459, 0.5);
      this.scene.add(hemi);
      this.hemi = hemi;

      var sun = new THREE.DirectionalLight(C.sun, 0.55);
      sun.position.set(-14, 22, -8);
      this.scene.add(sun);
      this.sun = sun;

      var fill = new THREE.DirectionalLight(0xcfe4ff, 0.12);
      fill.position.set(12, 9, 14);
      this.scene.add(fill);
      this.fill = fill;

      this.scene.add(new THREE.AmbientLight(0x9a9070, 0.26));
    }

    _buildSky() {
      this.sky = A.makeSky();
      this.scene.add(this.sky);
      // 森林能见度好，雾又远又淡
      this.scene.fog = new THREE.Fog(C.fog, 70, 205);
      this.scene.background = new THREE.Color(C.fog);
    }

    // ---------- 地面 ----------
    _buildGround() {
      var tileLen = CFG.WORLD.groundTileLength;
      var span = CFG.WORLD.visibleLength + CFG.WORLD.recycleBehind;
      this.tileCount = Math.ceil(span / tileLen) + 1;
      this.tileSpan = this.tileCount * tileLen;

      // 全套地砖共用一组材质：换区域时直接改材质颜色，整片地面一起变
      this.groundMats = A.groundMaterials(tileLen);

      for (var i = 0; i < this.tileCount; i++) {
        var tile = A.makeGroundTile(tileLen, this.groundMats);
        tile.position.z = -i * tileLen;
        this.scene.add(tile);
        this.groundTiles.push(tile);
      }
    }

    // ---------- 路边景物 ----------
    _buildProps() {
      var spacing = CFG.WORLD.propSpacing;
      var span = CFG.WORLD.visibleLength + CFG.WORLD.recycleBehind;
      var slots = Math.ceil(span / spacing);
      this.propSpan = slots * spacing;
      this.propSpacing = spacing;

      for (var i = 0; i < slots; i++) {
        for (var side = -1; side <= 1; side += 2) {
          var prop = A.makeRandomProp();
          var offset = A.rand(6.6, 13.5);
          prop.position.set(side * offset, 0, -i * spacing + A.rand(-1.8, 1.8));
          prop.rotation.y = A.rand(-0.5, 0.5);
          this.scene.add(prop);
          this.props.push(prop);
          if (prop.userData && prop.userData.sway) this.swayers.push(prop.userData.sway);
        }
      }
    }

    // ---------- 天上的云 ----------
    _buildClouds() {
      var W = CFG.WORLD;
      for (var i = 0; i < W.cloudCount; i++) {
        var cl = A.makeCloud();
        cl.position.set(A.rand(-W.cloudSpanX, W.cloudSpanX), A.rand(24, 40), A.rand(-225, -25));
        cl.scale.setScalar(A.rand(0.7, 1.5));
        cl.userData.baseY = cl.position.y;
        cl.userData.phase = A.rand(0, Math.PI * 2);
        cl.userData.vx = A.rand(-0.4, 0.4);
        this.scene.add(cl);
        this.clouds.push(cl);
      }
    }

    // ---------- 偶尔飞过的鸟 ----------
    _buildBirds() {
      this.birdTimer = A.rand(1.5, 5);
      for (var i = 0; i < CFG.WORLD.birdPool; i++) {
        var b = A.makeBird();
        b.visible = false;
        b.userData.active = false;
        b.userData.flap = A.rand(0, Math.PI * 2);
        b.userData.phase = A.rand(0, Math.PI * 2);
        b.userData.vx = 0;
        b.userData.baseY = 16;
        this.scene.add(b);
        this.birds.push(b);
      }
    }

    _spawnBirds() {
      var dir = Math.random() < 0.5 ? 1 : -1;
      var n = 2 + Math.floor(Math.random() * 2);
      var y0 = A.rand(12, 19);
      var z0 = A.rand(-35, -115);
      var v = A.rand(7, 12) * dir;
      var s = A.rand(0.9, 1.6);
      var spawned = 0;

      for (var i = 0; i < this.birds.length && spawned < n; i++) {
        var b = this.birds[i];
        if (b.userData.active) continue;

        b.userData.active = true;
        b.userData.flap = A.rand(0, Math.PI * 2);
        b.userData.phase = A.rand(0, Math.PI * 2);
        b.userData.vx = v;
        b.userData.baseY = y0 + spawned * A.rand(0.4, 1.1);

        var off = spawned * A.rand(1.2, 2.0);
        b.position.set(-dir * (CFG.WORLD.cloudSpanX + off), b.userData.baseY, z0 + off * 1.4);
        b.rotation.y = dir > 0 ? -Math.PI / 2 : Math.PI / 2;
        b.scale.setScalar(s);
        b.visible = true;
        spawned++;
      }
    }

    // ---------- 区域专属景物 ----------
    _buildRegionDecor() {
      var spacing = CFG.WORLD.decorSpacing;
      var span = CFG.WORLD.visibleLength + CFG.WORLD.recycleBehind;
      var slots = Math.ceil(span / spacing);
      this.decorSpan = slots * spacing;
      this.regionDecor = { 1: [], 2: [], 3: [] };

      for (var idx = 1; idx <= 3; idx++) {
        for (var i = 0; i < slots; i++) {
          for (var side = -1; side <= 1; side += 2) {
            var d = A.makeRegionDecor(idx);
            if (!d) continue;
            d.position.set(side * A.rand(8.5, 15.5), 0, -i * spacing + A.rand(-5, 5));
            d.rotation.y = A.rand(-0.6, 0.6);
            d.visible = false;                 // 开局是第 1 区，全部藏着
            this.scene.add(d);
            this.regionDecor[idx].push(d);
            if (d.userData && d.userData.sway) this.swayers.push(d.userData.sway);
          }
        }
      }
    }

    // ---------- 每帧 ----------
    update(dt, speed, playerX, slideMix) {
      var delta = speed * dt;
      this.scroll += delta;
      this.time += dt;

      var i, t;
      for (i = 0; i < this.groundTiles.length; i++) {
        t = this.groundTiles[i];
        t.position.z += delta;
        if (t.position.z - CFG.WORLD.groundTileLength / 2 > CFG.WORLD.recycleBehind) {
          t.position.z -= this.tileSpan;
        }
      }

      // 地面贴图往镜头方向推：1.0 = 颗粒完全钉在土上，跑动时看不到 12 米一格的重复
      var gm = this.groundMats;
      if (gm && gm.texes) {
        var step = (delta / gm.meters) * CFG.WORLD.groundTexScroll;
        for (i = 0; i < gm.texes.length; i++) {
          var tx = gm.texes[i];
          tx.offset.y += step;
          if (tx.offset.y > 1) tx.offset.y -= 1;
        }
      }

      for (i = 0; i < this.props.length; i++) {
        t = this.props[i];
        t.position.z += delta;
        if (t.position.z > CFG.WORLD.recycleBehind + 6) {
          t.position.z -= this.propSpan;
        }
      }

      // 风：树 / 草 / 藤蔓各自按自己的频率晃
      var tm = this.time;
      for (i = 0; i < this.swayers.length; i++) {
        var sw = this.swayers[i];
        sw.node.rotation.z = Math.sin(tm * sw.freq + sw.phase) * sw.amp;
        sw.node.rotation.x = Math.sin(tm * sw.freq * 0.77 + sw.phase * 1.7) * sw.amp * 0.5;
      }

      this._updateClouds(dt, delta);
      this._updateBirds(dt, tm);
      this._updateRegionDecor(delta);
      this._stepPalette(dt);
      this._updateCamera(dt, playerX, slideMix);
    }

    _updateClouds(dt, delta) {
      var W = CFG.WORLD;
      for (var i = 0; i < this.clouds.length; i++) {
        var cl = this.clouds[i];
        cl.position.z += delta * 0.05;                  // 视差：比地面慢得多
        cl.position.x += cl.userData.vx * dt;
        cl.position.y = cl.userData.baseY + Math.sin(this.time * 0.3 + cl.userData.phase) * 0.45;
        if (cl.position.z > 70) cl.position.z -= 300;
        if (cl.position.x > W.cloudSpanX) cl.position.x -= W.cloudSpanX * 2;
        else if (cl.position.x < -W.cloudSpanX) cl.position.x += W.cloudSpanX * 2;
      }
    }

    _updateBirds(dt, tm) {
      this.birdTimer -= dt;
      if (this.birdTimer <= 0) {
        this.birdTimer = A.rand(CFG.WORLD.birdMinGap, CFG.WORLD.birdMaxGap);
        this._spawnBirds();
      }

      for (var i = 0; i < this.birds.length; i++) {
        var b = this.birds[i];
        if (!b.userData.active) continue;

        b.position.x += b.userData.vx * dt;
        b.position.y = b.userData.baseY + Math.sin(tm * 1.6 + b.userData.phase) * 0.5;

        // 扑翅
        b.userData.flap += dt * 8;
        var fl = Math.sin(b.userData.flap) * 0.85;
        var wings = b.userData.wings || [];
        for (var w = 0; w < wings.length; w++) {
          wings[w].node.rotation.z = wings[w].side * fl;
        }

        if (Math.abs(b.position.x) > CFG.WORLD.cloudSpanX + 6) {
          b.visible = false;
          b.userData.active = false;
        }
      }
    }

    _updateRegionDecor(delta) {
      if (!this.regionDecor) return;
      // 远处才允许切换显隐：绝不让摆件在玩家眼前凭空冒出来
      var popZ = CFG.WORLD.decorPopZ;

      for (var idx = 1; idx <= 3; idx++) {
        var want = (idx === this.stageIndex);
        var list = this.regionDecor[idx];
        for (var i = 0; i < list.length; i++) {
          var o = list[i];
          o.position.z += delta;
          if (o.position.z > CFG.WORLD.recycleBehind + 8) {
            o.position.z -= this.decorSpan;
          }
          if (o.visible !== want && (o.position.z < popZ || o.position.z > 4)) {
            o.visible = want;
          }
        }
      }
    }

    _updateCamera(dt, playerX, slideMix) {
      // 相机横向跟随（只跟 30%，保留动感）
      var targetX = playerX * 0.34;
      this.camX += (targetX - this.camX) * Math.min(1, dt * 6);

      // 跑动颠簸
      this.camShake = Math.max(0, this.camShake - dt * 3.4);
      var bob = Math.sin(this.scroll * 1.9) * 0.035 + this.camShake * 0.28;

      // ---- 默认机位：跟在熊二背后 ----
      var camPX = this.camX;
      var camPY = 3.1 + bob;
      var camPZ = 5.9;
      var lookPX = this.camX * 0.55;
      var lookPY = 1.3;
      var lookPZ = -9;

      // ---- 滑铲时压低机位、贴着地面往前看 ----
      // 熊滑起来比站姿矮一截（熊大还是仰着的），把镜头压下去才看得到脸
      var s = slideMix || 0;
      if (s > 0.001) {
        camPY -= 0.8 * s;
        camPZ -= 0.35 * s;
        lookPY += 0.4 * s;
        lookPZ += 0.9 * s;
      }

      // ---- 处决机位：绕到熊二侧后方，把两个人一起框进画面 ----
      var m = this.cineMix;
      if (m > 0.001) {
        var CINE = CFG.CINE;
        camPX += (playerX + CINE.offsetX - camPX) * m;
        camPY += (CINE.offsetY + this.camShake * 0.2 - camPY) * m;
        camPZ += (CINE.offsetZ - camPZ) * m;
        lookPX += (playerX + CINE.lookX - lookPX) * m;
        lookPY += (CINE.lookY - lookPY) * m;
        lookPZ += (CINE.lookZ - lookPZ) * m;
      }

      this.camera.position.set(camPX, camPY, camPZ);
      this.camTarget.set(lookPX, lookPY, lookPZ);
      this.camera.lookAt(this.camTarget);
    }

    /** 处决镜头混合度：0 = 正常跟拍，1 = 侧机位（由 Game 在死亡演出期间推进） */
    setCine(mix) {
      this.cineMix = mix < 0 ? 0 : (mix > 1 ? 1 : mix);
    }

    /** 受击 / 高速时的镜头震动 */
    addShake(amount) {
      this.camShake = Math.min(1.6, this.camShake + amount);
    }

    // ---------- 区域配色 ----------

    _resolvePalette(idx) {
      var list = CFG.STAGE_PALETTE || [];
      var p = list[idx] || list[0];
      return {
        fog: new THREE.Color(p.fog),
        near: p.near,
        far: p.far,
        hemiSky: new THREE.Color(p.hemiSky),
        hemiGround: new THREE.Color(p.hemiGround),
        hemi: p.hemi === undefined ? 0.5 : p.hemi,
        sun: new THREE.Color(p.sun),
        sunIntensity: p.sunIntensity === undefined ? 0.55 : p.sunIntensity,
        road: new THREE.Color(p.road),
        grass: new THREE.Color(p.grass),
        grassDark: new THREE.Color(p.grassDark)
      };
    }

    /** 只换目标值，真正的过渡交给 _stepPalette 每帧平滑推进 */
    updateStagePalette(stageIndex) {
      if (this.stageIndex === stageIndex) return;
      this.stageIndex = stageIndex;
      this._palGoal = this._resolvePalette(stageIndex);
    }

    _stepPalette(dt) {
      var goal = this._palGoal;
      if (!goal) return;
      var cur = this._palCur;
      var k = 1 - Math.exp(-(CFG.WORLD.paletteLerp || 1.1) * dt);

      cur.fog.lerp(goal.fog, k);
      cur.near += (goal.near - cur.near) * k;
      cur.far += (goal.far - cur.far) * k;
      cur.hemiSky.lerp(goal.hemiSky, k);
      cur.hemiGround.lerp(goal.hemiGround, k);
      cur.hemi += (goal.hemi - cur.hemi) * k;
      cur.sun.lerp(goal.sun, k);
      cur.sunIntensity += (goal.sunIntensity - cur.sunIntensity) * k;
      cur.road.lerp(goal.road, k);
      cur.grass.lerp(goal.grass, k);
      cur.grassDark.lerp(goal.grassDark, k);

      this._applyPalette(cur);
    }

    _applyPalette(p) {
      if (this.scene.fog) {
        this.scene.fog.color.copy(p.fog);
        this.scene.fog.near = p.near;
        this.scene.fog.far = p.far;
      }
      if (this.scene.background && this.scene.background.isColor) {
        this.scene.background.copy(p.fog);
      }
      if (this.hemi) {
        this.hemi.color.copy(p.hemiSky);
        this.hemi.groundColor.copy(p.hemiGround);
        this.hemi.intensity = p.hemi;
      }
      if (this.sun) {
        this.sun.color.copy(p.sun);
        this.sun.intensity = p.sunIntensity;
      }
      if (this.groundMats) {
        this.groundMats.mats.road.color.copy(p.road);
        this.groundMats.mats.grass.color.copy(p.grass);
        this.groundMats.mats.grassDark.color.copy(p.grassDark);
      }
    }

    resize(width, height) {
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
    }
  }

  RR.World = World;

})(window);
