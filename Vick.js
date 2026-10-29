/**
 * 大反派：光头强（狗熊岭伐木工）。
 *
 * 造型配方：黄色安全帽 + 蓝色工装背带裤 + 棕色长靴，
 * 大鼻子、黑胡子、倒竖的怒眉，手里一把亮闪闪的斧头。
 *
 * 行为（四个状态，全部按需求实现）：
 *   1. 开场紧追 —— 像《地铁跑酷》的警察一样咬在熊二屁股后面跑一段；
 *   2. 追不上   —— 体力不支，逐渐落后、淡出画面（消失在林道尽头）；
 *   3. 重新追上 —— 熊二每次掉血，他立刻"闻到味儿"猛扑回来，追一段又落后；
 *   4. 处决     —— 熊二倒下时冲上来，高举斧头劈下，把熊二拍成一张饼，
 *                  屏幕打出大字「臭狗熊」。
 *
 * 坐标系注意：本作是"跑步机"模式，熊二永远待在 z=0，景物向 +z 移动。
 * 光头强的 z 不是世界滚动量，而是"落后熊二多少米"（gap），
 * 只在状态切换时改变这个落后量 —— 越小 = 贴得越近、画面里越大只。
 *
 * 材质同样是本文件独占的，因为"落后淡出"需要逐实例改 opacity。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;
  var C = CFG.COLORS;

  function ownMat(color) {
    return new THREE.MeshLambertMaterial({ color: color, flatShading: true });
  }

  function sph(r, seg) {
    seg = seg || 10;
    return new THREE.SphereGeometry(r, seg, Math.max(4, seg - 2));
  }

  function lerp(cur, target, k, dt) {
    return cur + (target - cur) * Math.min(1, dt * k);
  }

  var ST = {
    HIDDEN: 'hidden',
    CHASE: 'chase',
    FALL: 'fall',
    RUSH: 'rush',
    EXEC_APPROACH: 'execApproach',
    EXEC_RAISE: 'execRaise',
    EXEC_SWING: 'execSwing',
    EXEC_HOLD: 'execHold',
    EXEC_DONE: 'execDone'
  };

  class Vick {
    constructor(scene) {
      this.scene = scene;

      this.state = ST.HIDDEN;
      this.t = 0;                 // 当前状态已持续的时间
      this.gap = CFG.VICK.zFar;   // 落后熊二多少米
      this.opacity = 0;
      this.surge = 0;             // 掉血瞬间的"猛扑"冲量
      this.side = 1;              // 跑在熊二的哪一侧
      this.x = CFG.VICK.lateral;
      this.runCycle = 0;
      this.blend = 0;             // 处决姿势的混合度 0~1
      this.impactFired = false;

      // 横向跟随用的历史采样（延迟取样 => 看起来像在追熊二的走位）
      this.histT = 0;
      this.hist = [{ t: 0, x: 0 }];

      this.onImpact = null;       // 斧头落下的瞬间回调（由 Game 接管）
      this._transparent = null;

      this._build();
      this._buildBurst();
      this.reset();
    }

    // ==================== 建模 ====================

    _build() {
      var V = C;

      var hat = ownMat(V.vickHat);
      var hatDark = ownMat(V.vickHatDark);
      var skin = ownMat(V.vickSkin);
      var skinDark = ownMat(V.vickSkinDark);
      var noseM = ownMat(V.vickNose);
      var hair = ownMat(V.vickHair);
      var shirt = ownMat(V.vickShirt);
      var shirtDark = ownMat(V.vickShirtDark);
      var bib = ownMat(V.vickBib);
      var pants = ownMat(V.vickPants);
      var boot = ownMat(V.vickBoot);
      var eyeWhite = ownMat(0xfdfbf5);
      var eyeBlack = ownMat(0x241a12);
      var wood = ownMat(V.vickAxeWood);
      var steel = ownMat(V.vickAxeSteel);
      var steelDark = ownMat(V.vickAxeSteelDark);

      this.materials = [hat, hatDark, skin, skinDark, noseM, hair, shirt, shirtDark,
                        bib, pants, boot, eyeWhite, eyeBlack, wood, steel, steelDark];

      var root = new THREE.Group();
      this.root = root;

      var body = new THREE.Group();
      root.add(body);
      this.body = body;

      // ---------- 腿（脚底 y≈0）----------
      this.legL = new THREE.Group();
      this.legR = new THREE.Group();
      this.legL.position.set(-0.155, 0.86, 0);
      this.legR.position.set(0.155, 0.86, 0);
      body.add(this.legL);
      body.add(this.legR);

      var thighGeo = new THREE.BoxGeometry(0.23, 0.5, 0.25);
      var shinGeo = new THREE.BoxGeometry(0.21, 0.44, 0.23);
      var bootGeo = new THREE.BoxGeometry(0.25, 0.16, 0.38);
      [this.legL, this.legR].forEach(function (leg) {
        var thigh = new THREE.Mesh(thighGeo, pants);
        thigh.position.y = -0.25;
        leg.add(thigh);

        var shin = new THREE.Mesh(shinGeo, pants);
        shin.position.y = -0.62;
        leg.add(shin);

        var bt = new THREE.Mesh(bootGeo, boot);
        bt.position.set(0, -0.78, -0.05);
        leg.add(bt);
      });

      // ---------- 躯干：衬衫 + 工装背带裤 ----------
      var pantsHip = new THREE.Mesh(new THREE.BoxGeometry(0.56, 0.36, 0.34), bib);
      pantsHip.position.y = 0.98;
      body.add(pantsHip);

      var chest = new THREE.Mesh(new THREE.BoxGeometry(0.6, 0.46, 0.36), shirt);
      chest.position.y = 1.35;
      body.add(chest);

      // 胸兜
      var bibPatch = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.3, 0.07), bib);
      bibPatch.position.set(0, 1.33, -0.19);
      body.add(bibPatch);

      // 背带（前面 + 背面，背面才是镜头最常看到的）
      for (var s = -1; s <= 1; s += 2) {
        var strapF = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.44, 0.055), bib);
        strapF.position.set(s * 0.17, 1.42, -0.19);
        strapF.rotation.z = s * 0.06;
        body.add(strapF);

        var strapB = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.5, 0.055), bib);
        strapB.position.set(s * 0.15, 1.42, 0.185);
        strapB.rotation.z = s * 0.06;
        body.add(strapB);
      }

      // ---------- 手臂 ----------
      this.armL = new THREE.Group();
      this.armR = new THREE.Group();
      this.armL.position.set(-0.37, 1.5, 0);
      this.armR.position.set(0.37, 1.5, 0);
      body.add(this.armL);
      body.add(this.armR);

      var sleeveGeo = new THREE.BoxGeometry(0.2, 0.44, 0.21);
      var handGeo = sph(0.115, 8);
      [this.armL, this.armR].forEach(function (arm) {
        var sl = new THREE.Mesh(sleeveGeo, shirt);
        sl.position.y = -0.22;
        arm.add(sl);

        var hd = new THREE.Mesh(handGeo, skin);
        hd.position.y = -0.49;
        arm.add(hd);
      });

      // ---------- 斧头：握在右手，沿手臂方向向外延伸 ----------
      // 手臂自然下垂时斧头贴着腿，举起来时正好竖在头顶后方
      var axe = new THREE.Group();
      axe.position.set(0, -0.52, 0);
      this.armR.add(axe);
      this.axe = axe;

      var handle = new THREE.Mesh(new THREE.CylinderGeometry(0.032, 0.038, 0.98, 7), wood);
      handle.position.y = -0.3;
      axe.add(handle);

      // 斧头（刃朝前 = 熊二的方向）
      var headGroup = new THREE.Group();
      headGroup.position.y = -0.74;
      axe.add(headGroup);

      var poll = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.15, 0.12), steelDark);
      poll.position.set(0, 0, 0.06);
      headGroup.add(poll);

      var cheek = new THREE.Mesh(new THREE.BoxGeometry(0.085, 0.2, 0.16), steel);
      cheek.position.set(0, 0, -0.08);
      headGroup.add(cheek);

      var blade = new THREE.Mesh(new THREE.BoxGeometry(0.055, 0.34, 0.15), steel);
      blade.position.set(0, 0, -0.22);
      headGroup.add(blade);

      var edgeMat = ownMat(0xeef3f6);
      var edge = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.38, 0.05), edgeMat);
      edge.position.set(0, 0, -0.3);
      headGroup.add(edge);
      this.materials.push(edgeMat);

      // ---------- 头 ----------
      var headGroup2 = new THREE.Group();
      headGroup2.position.y = 1.56;
      body.add(headGroup2);
      this.headGroup = headGroup2;

      var neck = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.11, 0.16, 8), skinDark);
      neck.position.y = -0.2;
      headGroup2.add(neck);

      var head = new THREE.Mesh(sph(0.28, 12), skin);
      head.scale.set(1, 1.02, 0.98);
      headGroup2.add(head);

      // 大鼻子（光头强的招牌）
      var nose = new THREE.Mesh(sph(0.088, 9), noseM);
      nose.position.set(0, -0.06, -0.255);
      nose.scale.set(1.15, 0.95, 1.1);
      headGroup2.add(nose);

      // 黑胡子：贴在大鼻子正下方（往前提，免得被鼻子挡干净）
      var mustache = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.05, 0.06), hair);
      mustache.position.set(0, -0.13, -0.28);
      mustache.rotation.x = 0.2;
      headGroup2.add(mustache);
      for (var mu = -1; mu <= 1; mu += 2) {
        var mg = new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.045, 0.055), hair);
        mg.position.set(mu * 0.135, -0.115, -0.235);
        mg.rotation.z = mu * 0.35;
        mg.rotation.x = 0.2;
        headGroup2.add(mg);
      }

      // 嘴（一条下撇的线，看着就凶）
      var mouth = new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.03, 0.03), hair);
      mouth.position.set(0, -0.215, -0.175);
      headGroup2.add(mouth);

      // 眼睛 + 倒竖怒眉（y 必须压在帽檐之下，否则会被帽壳吞掉）
      for (var e = -1; e <= 1; e += 2) {
        var eg = new THREE.Group();
        eg.position.set(e * 0.105, 0.015, -0.235);
        headGroup2.add(eg);

        var w = new THREE.Mesh(sph(0.064, 9), eyeWhite);
        w.scale.set(1, 1.1, 0.75);
        eg.add(w);

        var p = new THREE.Mesh(sph(0.034, 7), eyeBlack);
        p.position.set(0, -0.004, -0.05);
        p.scale.set(1, 1.15, 0.7);
        eg.add(p);

        var brow = new THREE.Mesh(new THREE.BoxGeometry(0.115, 0.045, 0.05), hair);
        brow.position.set(e * 0.105, 0.088, -0.225);
        brow.rotation.z = e * 0.4;      // 内低外高 = 怒
        brow.rotation.x = 0.35;
        headGroup2.add(brow);
      }

      // 耳朵
      for (var es = -1; es <= 1; es += 2) {
        var ear = new THREE.Mesh(sph(0.062, 8), skin);
        ear.position.set(es * 0.27, -0.03, 0.01);
        ear.scale.set(0.65, 1, 0.9);
        headGroup2.add(ear);
      }

      // 光头：帽檐底下露出来的几根头发，向外下方翘（别扎进帽壳里）
      for (var hz = -1; hz <= 1; hz += 2) {
        var hairTuft = new THREE.Mesh(new THREE.ConeGeometry(0.032, 0.15, 5), hair);
        hairTuft.position.set(hz * 0.255, -0.14, 0.0);
        hairTuft.rotation.z = -hz * 2.15;
        headGroup2.add(hairTuft);
      }

      // ---------- 黄色安全帽 ----------
      // 浅圆顶 + 全圈帽檐，帽檐底边留在眉毛上方，避免压住五官
      var dome = new THREE.Mesh(new THREE.SphereGeometry(0.3, 14, 9, 0, Math.PI * 2, 0, Math.PI * 0.34), hat);
      dome.position.y = 0.1;
      dome.scale.set(0.95, 0.86, 0.95);
      headGroup2.add(dome);

      var domeBand = new THREE.Mesh(new THREE.CylinderGeometry(0.255, 0.255, 0.1, 14), hatDark);
      domeBand.position.y = 0.19;
      headGroup2.add(domeBand);

      var brim = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.305, 0.035, 14), hatDark);
      brim.position.y = 0.145;
      headGroup2.add(brim);

      var ridge = new THREE.Mesh(new THREE.BoxGeometry(0.045, 0.06, 0.3), hatDark);
      ridge.position.y = 0.315;
      headGroup2.add(ridge);

      // ---------- 脚下假阴影 ----------
      this.shadow = RR.Art.makeBlobShadow(0.44);
      this.shadowMat = this.shadow.material;
      root.add(this.shadow);

      this.scene.add(root);
    }

    /** 斧头落下时溅起的卡通星火（不随身体缩放，所以挂在场景上） */
    _buildBurst() {
      var g = new THREE.Group();
      this.burstMats = [];
      this.burstBits = [];
      for (var i = 0; i < 9; i++) {
        var col = (i % 3 === 0) ? 0xffffff : (i % 3 === 1 ? 0xffd85e : 0xffa63a);
        var m = new THREE.MeshBasicMaterial({
          color: col, transparent: true, opacity: 0, depthWrite: false
        });
        var b = new THREE.Mesh(new THREE.IcosahedronGeometry(0.1, 0), m);
        var a = (i / 9) * Math.PI * 2;
        b.userData.dx = Math.cos(a) * 1.5;
        b.userData.dy = 1.4 + (i % 3) * 0.5;
        b.userData.dz = Math.sin(a) * 1.0;
        g.add(b);
        this.burstMats.push(m);
        this.burstBits.push(b);
      }
      g.visible = false;
      this.burst = g;
      this.burstT = -1;
      this.scene.add(g);
    }

    // ==================== 生命周期 ====================

    reset() {
      this.state = ST.HIDDEN;
      this.t = 0;
      this.gap = CFG.VICK.zFar;
      this.surge = 0;
      this.side = 1;
      this.runCycle = 0;
      this.blend = 0;
      this.impactFired = false;
      this.histT = 0;
      this.hist = [{ t: 0, x: 0 }];
      this.x = CFG.VICK.lateral;
      this.burstT = -1;
      if (this.burst) this.burst.visible = false;
      this.body.rotation.set(0, 0, 0);
      this.body.position.set(0, 0, 0);
      this.body.scale.set(1, 1, 1);
      this.headGroup.rotation.set(0, 0, 0);
      this._setOpacity(0);
      this.root.position.set(this.x, 0, this.gap);
    }

    /** 开局紧追：直接从画面外冲进来 */
    beginChase() {
      this.t = 0;
      this.surge = 0;
      this.gap = CFG.VICK.zFar;
      this._setState(ST.CHASE);
      this._setOpacity(0);
    }

    /** 熊二掉血 —— 光头强立刻重新追上来 */
    onPlayerHurt() {
      if (this.state === ST.CHASE || this.state === ST.RUSH) {
        // 已经在他屁股后面：原地猛扑一下（同时把追逐时间续上）
        this.surge = 1;
        this.t = 0;
        return;
      }
      if (this.state === ST.HIDDEN || this.state === ST.FALL) {
        this.t = 0;
        this.surge = 0;
        this._setState(ST.RUSH);
      }
    }

    /** 熊二倒下 —— 开始处决演出 */
    startExecution() {
      this.t = 0;
      this.impactFired = false;
      this.surge = 0;
      this.side = -1;          // 直接扑到熊二左侧（远离侧机位那一边），不用平滑过渡
      this._setState(ST.EXEC_APPROACH);
      this._setOpacity(Math.max(this.opacity, 0.001));
    }

    get executionDone() { return this.state === ST.EXEC_DONE; }
    get isExecuting() { return this.state.indexOf('exec') === 0; }

    _setState(s) {
      this.state = s;
      this.t = 0;
    }

    // ==================== 每帧 ====================

    update(dt, speed, player) {
      if (this.state === ST.HIDDEN) {
        this._updateBurst(dt);
        return;
      }

      var V = CFG.VICK;
      this.t += dt;
      if (this.surge > 0) this.surge = Math.max(0, this.surge - dt * 2.2);

      // 横向：延迟跟随熊二的走位 + 左右错开，避免挡住熊二
      this._trackX(dt, player);
      var offset = this.isExecuting ? V.lateralExec : V.lateral;
      var baseX = this.isExecuting ? player.x : this.hist[0].x;
      // 处决时固定站在熊二左侧（远离侧机位的那一边），保证镜头里两人左右分开、不互相遮挡
      var wantSide = this.isExecuting ? -1
        : ((baseX > 0.05) ? -1 : (baseX < -0.05 ? 1 : this.side));
      this.side = lerp(this.side, wantSide, V.sideSwapK, dt);
      var targetX = baseX * 0.85 + this.side * offset;
      var k = this.isExecuting ? 7 : V.followK;
      this.x = lerp(this.x, targetX, k, dt);

      // 状态推进
      switch (this.state) {
        case ST.CHASE:
          this.gap = lerp(this.gap, V.zChase - this.surge * V.surge, 6, dt);
          this._fadeTo(1, 5, dt);
          if (this.t >= V.introChase) this._setState(ST.FALL);
          break;

        case ST.FALL:
          this.gap = lerp(this.gap, V.zFar, V.fallK, dt);
          this._fadeTo(0, 1.1, dt);
          if (this.opacity < 0.03) {
            this._setOpacity(0);
            this.state = ST.HIDDEN;
          }
          break;

        case ST.RUSH:
          this.gap = lerp(this.gap, V.zNear, V.rushK, dt);
          this._fadeTo(1, 6, dt);
          if (this.t >= V.reengageHold) this._setState(ST.FALL);
          break;

        case ST.EXEC_APPROACH:
          this.gap = lerp(this.gap, V.zExec, 9, dt);
          this.blend = lerp(this.blend, 0, 6, dt);
          this._fadeTo(1, 9, dt);
          if (this.t >= V.execApproach) this._setState(ST.EXEC_RAISE);
          break;

        case ST.EXEC_RAISE:
          this.gap = lerp(this.gap, V.zExec, 6, dt);
          this.blend = lerp(this.blend, 1, 8, dt);
          this._fadeTo(1, 8, dt);
          if (this.t >= V.execRaise) this._setState(ST.EXEC_SWING);
          break;

        case ST.EXEC_SWING:
          this.blend = lerp(this.blend, 1, 14, dt);
          this._fadeTo(1, 10, dt);
          if (!this.impactFired && this.t >= V.execSwing * 0.7) {
            this.impactFired = true;
            this._fireImpact(player);
          }
          if (this.t >= V.execSwing) this._setState(ST.EXEC_HOLD);
          break;

        case ST.EXEC_HOLD:
          this.blend = lerp(this.blend, 1, 10, dt);
          this._fadeTo(1, 10, dt);
          if (this.t >= V.execHold) this._setState(ST.EXEC_DONE);
          break;

        case ST.EXEC_DONE:
          this.blend = lerp(this.blend, 1, 6, dt);
          break;

        default:
          break;
      }

      // 摆位
      var bob = Math.sin(this.runCycle * 2) * 0.03;
      this.root.position.set(this.x, bob * (this.isExecuting ? 0 : 1), this.gap);

      // 影子：跑动时贴地，落后的同时变淡
      var sc = 1;
      this.shadow.scale.set(sc, sc, 1);
      this.shadowMat.opacity = 0.22 * this.opacity * (this.isExecuting ? 0.7 : 1);

      // 姿势
      if (this.isExecuting) this._animateExecution(dt);
      else this._animateRun(dt, speed);

      this._updateBurst(dt);
    }

    // ---------- 横向跟随（延迟取样）----------

    _trackX(dt, player) {
      this.histT += dt;
      this.hist.push({ t: this.histT, x: player.x });
      var lag = CFG.VICK.followLag;
      while (this.hist.length > 2 && (this.histT - this.hist[0].t) > lag) this.hist.shift();
      if (this.hist.length > 90) this.hist.shift();
    }

    // ---------- 淡入淡出 ----------

    _fadeTo(target, k, dt) {
      this._setOpacity(lerp(this.opacity, target, k, dt));
    }

    _setOpacity(v) {
      if (v > 1) v = 1;
      if (v < 0) v = 0;
      if (v > 0.999) v = 1;
      this.opacity = v;

      var wantTransparent = v < 0.999;
      if (this._transparent !== wantTransparent) {
        this._transparent = wantTransparent;
        for (var i = 0; i < this.materials.length; i++) {
          this.materials[i].transparent = wantTransparent;
          this.materials[i].needsUpdate = true;
        }
      }
      for (var j = 0; j < this.materials.length; j++) this.materials[j].opacity = v;

      this.root.visible = v > 0.02;
    }

    // ---------- 跑动 ----------

    _animateRun(dt, speed) {
      this.runCycle += dt * Math.max(3.2, speed * 0.78);
      var c = this.runCycle;
      var swing = Math.sin(c * 2);
      var fast = this.state === ST.RUSH || this.state === ST.CHASE;

      // 注意：角色面朝 -z，body 的原点在脚底，绕 x 轴正转 = 上半身朝 +z 倒（后仰）。
      // 所以"前倾"要用负值 —— 手臂/腿的原点在肩/髋，语义相反，不受影响。
      this.body.rotation.x = lerp(this.body.rotation.x, -0.18, 9, dt);      // 追人时身子前倾
      this.body.rotation.z = swing * 0.06;
      this.body.rotation.y = lerp(this.body.rotation.y, 0, 10, dt);

      // 腿
      this.legL.rotation.x = lerp(this.legL.rotation.x, swing * 0.8, 17, dt);
      this.legR.rotation.x = lerp(this.legR.rotation.x, -swing * 0.8, 17, dt);
      this.legL.rotation.z = lerp(this.legL.rotation.z, 0, 12, dt);
      this.legR.rotation.z = lerp(this.legR.rotation.z, 0, 12, dt);

      // 左臂正常摆，右臂拎着斧头，摆幅小一点
      var amp = fast ? 1 : 0.75;
      this.armL.rotation.x = lerp(this.armL.rotation.x, -swing * 0.8 * amp, 17, dt);
      this.armR.rotation.x = lerp(this.armR.rotation.x, swing * 0.55 * amp + 0.1, 17, dt);
      this.armL.rotation.z = lerp(this.armL.rotation.z, -0.2, 11, dt);
      this.armR.rotation.z = lerp(this.armR.rotation.z, 0.26, 11, dt);

      // 头部：盯着前方的熊二
      this.headGroup.rotation.x = lerp(this.headGroup.rotation.x, -0.1, 9, dt);
      this.headGroup.rotation.z = swing * 0.045;
      this.headGroup.position.y = 1.56;
    }

    // ---------- 处决 ----------

    _animateExecution(dt) {
      var V = CFG.VICK;
      var p = this.t;
      var st = this.state;

      // 手臂角度：负 = 举到头顶后方，正 = 抡到身前下方
      var armTarget = 0.1;
      var leanTarget = 0.05;
      var headTilt = 0;
      var legSpread = 0;

      if (st === ST.EXEC_APPROACH) {
        // 大步扑上来，斧头已经抬到一半
        var swing = Math.sin(p * 18);
        armTarget = -1.5 + Math.sin(p * 16) * 0.25;
        leanTarget = 0.34;
        legSpread = 0.34;
        this.legL.rotation.x = swing * 0.7;
        this.legR.rotation.x = -swing * 0.7;
        this.armL.rotation.x = -swing * 0.7;
      } else if (st === ST.EXEC_RAISE) {
        // 高举斧头，身子后仰蓄力
        var q = Math.min(1, p / V.execRaise);
        var ease = 1 - Math.pow(1 - q, 3);
        armTarget = -1.5 - 1.45 * ease;
        leanTarget = 0.34 - 0.6 * ease;
        headTilt = -0.22 * ease;
        legSpread = 0.3;
        this.legL.rotation.x = lerp(this.legL.rotation.x, 0.34, 10, dt);
        this.legR.rotation.x = lerp(this.legR.rotation.x, -0.34, 10, dt);
        this.armL.rotation.x = lerp(this.armL.rotation.x, -2.35, 12, dt);
      } else if (st === ST.EXEC_SWING) {
        // 抡下来
        var w = Math.min(1, p / V.execSwing);
        var e2 = w * w;
        armTarget = -2.95 + 3.85 * e2;
        leanTarget = -0.26 + 0.78 * e2;
        headTilt = 0.3 * e2;
        legSpread = 0.3;
        this.legL.rotation.x = lerp(this.legL.rotation.x, 0.44, 14, dt);
        this.legR.rotation.x = lerp(this.legR.rotation.x, -0.44, 14, dt);
        this.armL.rotation.x = lerp(this.armL.rotation.x, -0.6, 14, dt);
      } else {
        // 定住看结果 + 叉腰大笑
        var l = Math.min(1, (p - 0) / 0.3);
        // 注意：本文件的 lerp 是带 dt 的四参签名，这里要的是纯插值，别写成 lerp(0.9, 0.62, l)
        armTarget = 0.9 + (0.62 - 0.9) * l;
        leanTarget = 0.52;
        headTilt = 0.16;
        legSpread = 0.36;
        var laugh = Math.sin(p * 13) * 0.035;
        this.body.rotation.z = laugh;
        this.headGroup.rotation.z = -laugh * 1.6;
        this.legL.rotation.x = lerp(this.legL.rotation.x, 0.3, 8, dt);
        this.legR.rotation.x = lerp(this.legR.rotation.x, -0.3, 8, dt);
        // 左手叉腰
        this.armL.rotation.x = lerp(this.armL.rotation.x, 0.7, 9, dt);
        this.armL.rotation.z = lerp(this.armL.rotation.z, -1.15, 9, dt);
      }

      this.armR.rotation.x = lerp(this.armR.rotation.x, armTarget, 15, dt);
      this.armR.rotation.z = lerp(this.armR.rotation.z, 0.16, 10, dt);
      // 同 _animateRun：前倾为负、后仰为正，故 leanTarget 取反后使用
      this.body.rotation.x = lerp(this.body.rotation.x, -leanTarget, 13, dt);
      if (st === ST.EXEC_APPROACH || st === ST.EXEC_RAISE || st === ST.EXEC_SWING) {
        this.body.rotation.z = lerp(this.body.rotation.z, 0, 10, dt);
      }
      // headGroup 的原点在脖子，正转 = 抬头后仰；同样取反
      this.headGroup.rotation.x = lerp(this.headGroup.rotation.x, -headTilt, 12, dt);
      this.legL.rotation.z = lerp(this.legL.rotation.z, legSpread, 10, dt);
      this.legR.rotation.z = lerp(this.legR.rotation.z, -legSpread, 10, dt);
    }

    // ---------- 斧头落地的冲击 ----------

    _fireImpact(player) {
      this.burstT = 0;
      this.burst.visible = true;
      this.burst.position.set(player.x, 0.42, 0.1);
      for (var i = 0; i < this.burstBits.length; i++) {
        var b = this.burstBits[i];
        b.position.set(0, 0, 0);
        b.scale.set(1, 1, 1);
        this.burstMats[i].opacity = 1;
      }
      if (this.onImpact) this.onImpact();
    }

    _updateBurst(dt) {
      if (this.burstT < 0) return;
      this.burstT += dt;
      var life = 0.55;
      var t = this.burstT / life;
      if (t >= 1) {
        this.burstT = -1;
        this.burst.visible = false;
        return;
      }
      var ease = 1 - Math.pow(1 - t, 2.5);
      for (var i = 0; i < this.burstBits.length; i++) {
        var b = this.burstBits[i];
        b.position.set(
          b.userData.dx * ease * 0.62,
          0.1 + b.userData.dy * ease * 0.42 - 1.6 * t * t * 0.35,
          b.userData.dz * ease * 0.5
        );
        var s = 1 - t * 0.65;
        b.scale.set(s, s, s);
        this.burstMats[i].opacity = Math.max(0, 1 - t * 1.15);
      }
    }

    dispose() {
      this.scene.remove(this.root);
      this.scene.remove(this.burst);
    }
  }

  RR.Vick = Vick;

})(window);