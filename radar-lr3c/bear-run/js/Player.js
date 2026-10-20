/**
 * 玩家角色：熊二 / 熊大（可在开始界面切换）。
 *
 * 两个角色共用同一套骨架与判定盒，但**长相和动作都不一样**（都来自 CFG.CHARACTERS）：
 *   熊二 —— 浅棕黄皮毛、圆瞪大眼、斜挑细眉、腮红、三瓣呆毛、矮胖个儿，憨。
 *   熊大 —— 深棕红皮毛、细长眯眼、一字浓眉、无腮红、一根冲天呆毛、长腿细腰小头，稳。
 * build 里的比例系数全为 1 时算出来的就是"原始那套尺寸"（熊二 = 全 1，所以它一点没变）。
 *
 * 造型配方：熊出没式圆润卡通熊 —— 皮毛、米色大肚皮和口鼻、圆耳朵、头顶那撮呆毛。
 *
 * 搞笑动作设计（两人各玩各的，一眼就能看出区别）：
 *   · 熊二跳跃：抱膝前空翻 —— 半空中把四肢收进肚子、整个熊压成一颗球，
 *               眼睛瞪圆、吐舌头，落地"啪叽"一下拍扁再弹回来。
 *   · 熊大跳跃：后空翻 —— 双爪高举过头顶欢呼，腿屈起来外八岔开，一整个"我飞起来了！"
 *   · 熊二滑铲：仰面朝天躺平滑出去，四只爪子在空中乱蹬（像翻不过身的乌龟），
 *               脑袋后仰、舌头甩在外面。
 *   · 熊大滑铲：超人式趴滑 —— 脸朝下、肚子贴地、双爪笔直前伸，
 *               后腿高高翘起像打水一样乱蹬，脖子还使劲往上抬着看路。
 *
 * 角色材质是本文件独占的，不与其他物体共享 —— 这样受伤闪红时才不会污染全局。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;
  var C = CFG.COLORS;

  function ownMat(color, emissive) {
    var m = new THREE.MeshLambertMaterial({ color: color, flatShading: true });
    if (emissive) m.emissive = new THREE.Color(emissive);
    return m;
  }

  function sph(r, seg) {
    return new THREE.SphereGeometry(r, seg || 12, seg ? seg - 2 : 10);
  }

  class Player {
    constructor(scene, charId) {
      this.scene = scene;

      // 角色只决定"长什么样"：配色、体型、眉毛、腮红。
      // 速度、血量、判定盒、计分规则全部共用，换角色不影响任何玩法数值。
      this.spec = CFG.getCharacter(charId);
      this.charId = this.spec.id;

      this.lane = 1;
      this.x = 0;
      this.y = 0;
      this.vy = 0;
      this.jumping = false;
      this.sliding = false;
      this.slideTimer = 0;
      this.jumpCooldown = 0;
      this.invulnTimer = 0;
      this.runCycle = 0;
      this.alive = true;
      this.hurtFlash = 0;
      this.ballT = 0;      // 空中"缩成球"的程度 0~1
      this.squash = 0;     // 落地"拍扁"的程度 0~1
      this.jumpTime = 0;   // 本次跳跃已飞行的时间
      this.jumpDuration = 1;
      this.dead = false;   // 被光头强逮住
      this.deadT = 0;
      this.smashed = 0;    // 被斧头拍成饼的程度 0~1

      // 给外部看的动作进度：相机 / HUD 表情小窗靠它们决定"现在是什么状态"
      this.jumpProgress = 0;   // 0~1，起跳后翻到哪儿了
      this.slideAmount = 0;    // 0~1，平滑过的滑铲程度

      this._build();
    }

    // ==================== 建模 ====================

    _build() {
      var S = this.spec;
      var B = S.build;
      var F = S.face;

      // 动作风格与脸型：每帧要用，先存到实例上，免得动画里反复穿 spec
      this.jumpStyle = S.anim.jump;
      this.slideStyle = S.anim.slide;
      this.runSwing = S.anim.runSwing;
      this.eyeAspect = F.eye === 'narrow' ? 0.62 : 1.15;
      this.ballShape = this.jumpStyle === 'backflip' ? 0.55 : 1;

      var fur = ownMat(S.fur);
      var furDark = ownMat(S.furDark);
      var belly = ownMat(S.belly);
      var nose = ownMat(C.bearNose);
      var eyeWhite = ownMat(C.bearEyeWhite);
      var eyeBlack = ownMat(C.bearEyeBlack);
      var blush = ownMat(S.blush);
      var tongue = ownMat(C.bearTongue);
      var pad = ownMat(0x6b4f30);

      this.materials = [fur, furDark, belly, nose, eyeWhite, eyeBlack, blush, tongue, pad];

      var root = new THREE.Group();
      this.root = root;

      // 体型层：只负责把整套零件按角色整体放大/缩小。
      // 单独一层，避免和"滑铲倾倒 / 落地压扁"用的 body 变换互相覆盖。
      var bodyScale = new THREE.Group();
      bodyScale.scale.set(S.scale, S.scale, S.scale);
      root.add(bodyScale);
      this.bodyScale = bodyScale;

      // 身体组：滑铲时整体倾倒、落地时整体压扁
      var body = new THREE.Group();
      bodyScale.add(body);
      this.body = body;

      // ---- 骨架比例 ----
      // 下面所有零件的位置和大小都由这几个系数推出来。
      // 系数全为 1 时算出来的就是"原始那套尺寸"（熊二 = 全 1，所以它一点没变）。
      //   腿长 -> 决定髋高，进而决定整只熊站多高
      //   肚子 -> 决定腰围，也决定上半身各零件往上抬多少
      //   头   -> 头连同五官一起缩放
      //   四肢 -> 手脚粗细
      var legK = B.leg;
      var torK = B.torso;
      var headK = B.head;
      var limbK = B.limb;

      var hipY = 0.44 * legK;                 // 髋关节高度（脚底 y≈0）
      var torsoR = 0.46 * torK;               // 肚子半径
      var tummyY = hipY + torsoR * 0.957;     // 肚子中心（torsoR=0.46 时正好 0.88）
      var headY = tummyY + torsoR * 1.0435;   // 头中心（torsoR=0.46 时正好 1.36）
      var headR = 0.36 * headK;
      this.headRestY = headY;                 // 动画与 reset 都要用，不能写死 1.36

      // ---- 尾巴 ----
      var tail = new THREE.Mesh(sph(0.13 * B.tail, 8), furDark);
      tail.position.set(0, tummyY - torsoR * 0.2174, 0.42 * torK);
      body.add(tail);

      // ---- 短粗腿（脚底 y≈0）----
      this.legL = new THREE.Group();
      this.legR = new THREE.Group();
      var legX = 0.19 * limbK;
      this.legL.position.set(-legX, hipY, 0);
      this.legR.position.set(legX, hipY, 0);
      body.add(this.legL);
      body.add(this.legR);

      var legGeo = new THREE.BoxGeometry(0.25 * limbK, 0.34 * legK, 0.27 * limbK);
      var footGeo = new THREE.BoxGeometry(0.27 * limbK, 0.15 * legK, 0.36 * limbK);
      [this.legL, this.legR].forEach(function (leg) {
        var l = new THREE.Mesh(legGeo, furDark);
        l.position.y = -0.17 * legK;
        leg.add(l);

        var f = new THREE.Mesh(footGeo, fur);
        f.position.set(0, -0.36 * legK, -0.04 * limbK);
        leg.add(f);

        var fp = new THREE.Mesh(sph(0.07 * limbK, 6), pad);
        fp.position.set(0, -0.4 * legK, -0.2 * limbK);
        fp.scale.set(1, 0.5, 0.9);
        leg.add(fp);
      });

      // ---- 圆滚滚的大肚子 ----
      var tummy = new THREE.Mesh(sph(torsoR), fur);
      tummy.position.y = tummyY;
      tummy.scale.set(1.02, 1.0, 0.94);
      body.add(tummy);

      // 米色肚皮斑
      var bellyPatch = new THREE.Mesh(sph(0.34 * torK), belly);
      bellyPatch.position.set(0, tummyY - 0.06 * torK, -0.2 * torK);
      bellyPatch.scale.set(1.0, 1.08, 0.7);
      body.add(bellyPatch);

      // ---- 胖手臂 ----
      this.armL = new THREE.Group();
      this.armR = new THREE.Group();
      var armX = 0.43 * torK;
      var armY = tummyY + torsoR * 0.565;
      this.armL.position.set(-armX, armY, 0);
      this.armR.position.set(armX, armY, 0);
      body.add(this.armL);
      body.add(this.armR);

      var armGeo = new THREE.BoxGeometry(0.2 * limbK, 0.4 * limbK, 0.21 * limbK);
      [this.armL, this.armR].forEach(function (arm) {
        var a = new THREE.Mesh(armGeo, fur);
        a.position.y = -0.2 * limbK;
        arm.add(a);

        var hand = new THREE.Mesh(sph(0.15 * limbK, 8), furDark);
        hand.position.y = -0.44 * limbK;
        arm.add(hand);

        var hp = new THREE.Mesh(sph(0.08 * limbK, 6), pad);
        hp.position.set(0, -0.44 * limbK, -0.11 * limbK);
        hp.scale.set(1, 1, 0.6);
        arm.add(hp);
      });

      // ---- 头 ----
      var headGroup = new THREE.Group();
      headGroup.position.y = headY;
      body.add(headGroup);
      this.headGroup = headGroup;

      var head = new THREE.Mesh(sph(headR), fur);
      head.scale.set(1.05, 0.98, 1.0);
      headGroup.add(head);

      // 口鼻（米色吻部）
      var muzzle = new THREE.Mesh(sph(0.24 * headK), belly);
      muzzle.position.set(0, -0.08 * headK, -0.26 * headK);
      muzzle.scale.set(1.15 * S.muzzleScale, 0.82 * S.muzzleScale, 0.85);
      headGroup.add(muzzle);

      // 黑鼻头
      var noseM = new THREE.Mesh(sph(0.1 * headK, 8), nose);
      noseM.position.set(0, 0.01 * headK, -0.44 * headK);
      noseM.scale.set(1.25, 0.9, 0.9);
      headGroup.add(noseM);

      var noseHi = new THREE.Mesh(sph(0.035 * headK, 6), ownMat(0xffffff, 0x444444));
      noseHi.position.set(-0.04 * headK, 0.04 * headK, -0.5 * headK);
      headGroup.add(noseHi);

      if (F.mouth === 'flat') {
        // 熊大：嘴角抿成一条直线，一副"我看你能跑多远"的表情
        var mouthFlat = new THREE.Mesh(new THREE.BoxGeometry(0.15 * headK, 0.032 * headK, 0.03), nose);
        mouthFlat.position.set(0, -0.155 * headK, -0.4 * headK);
        headGroup.add(mouthFlat);
      } else {
        // 熊二：憨笑（一条弧形的嘴）
        var mouth = new THREE.Mesh(new THREE.BoxGeometry(0.14 * headK, 0.035 * headK, 0.03), nose);
        mouth.position.set(0, -0.17 * headK, -0.4 * headK);
        headGroup.add(mouth);
        for (var ms = -1; ms <= 1; ms += 2) {
          var mc = new THREE.Mesh(new THREE.BoxGeometry(0.06 * headK, 0.035 * headK, 0.03), nose);
          mc.position.set(ms * 0.085 * headK, -0.15 * headK, -0.39 * headK);
          mc.rotation.z = ms * 0.5;
          headGroup.add(mc);
        }
      }

      // 舌头（滑铲/下坠时甩出来）
      this.tongue = new THREE.Mesh(new THREE.BoxGeometry(0.13 * headK, 0.14 * headK, 0.05), tongue);
      this.tongue.position.set(0.02 * headK, -0.23 * headK, -0.38 * headK);
      this.tongue.rotation.x = 0.4;
      this.tongue.visible = false;
      headGroup.add(this.tongue);

      // 眼睛（白色眼球 + 黑瞳孔分开，方便做"瞪圆"）
      // 熊二是圆瞪大眼，熊大是细长眯眼（eyeAspect 在 _setEyeScale 里也要用）
      this.eyeWhites = [];
      this.pupils = [];
      var browTilt = F.brow === 'flat' ? 0.06 : 0.22;
      for (var e = -1; e <= 1; e += 2) {
        var eg = new THREE.Group();
        eg.position.set(e * 0.15 * headK, 0.1 * headK, -0.3 * headK);
        headGroup.add(eg);

        var w = new THREE.Mesh(sph(0.115 * headK, 10), eyeWhite);
        w.scale.set(1, this.eyeAspect, 0.8);
        eg.add(w);
        this.eyeWhites.push(w);

        var p = new THREE.Mesh(sph(0.064 * headK, 8), eyeBlack);
        p.position.set(e * 0.012 * headK, -0.01 * headK, -0.078 * headK);
        p.scale.set(1, this.eyeAspect, 0.7);
        eg.add(p);
        this.pupils.push(p);

        // 眉毛（贴着额头长，不要悬空成"眼镜框"）：熊二是斜挑细眉，熊大是一字浓眉
        var brow = new THREE.Mesh(
          new THREE.BoxGeometry(S.browW * headK, S.browH * headK, 0.04), furDark);
        brow.position.set(e * 0.15 * headK, 0.245 * headK, -0.245 * headK);
        brow.rotation.z = -e * browTilt;
        brow.rotation.x = 0.5;
        headGroup.add(brow);

        // 腮红（熊大不画）
        if (S.hasBlush) {
          var bl = new THREE.Mesh(sph(0.09 * headK, 8), blush);
          bl.position.set(e * 0.26 * headK, -0.1 * headK, -0.24 * headK);
          bl.scale.set(1, 0.72, 0.4);
          headGroup.add(bl);
        }
      }

      // 圆耳朵（熊大耳朵偏小，显得脑袋更"精气神"）
      for (var es = -1; es <= 1; es += 2) {
        var ear = new THREE.Mesh(sph(0.15 * headK * B.ear, 10), fur);
        ear.position.set(es * 0.27 * headK, 0.27 * headK, 0.02 * headK);
        ear.scale.set(1, 1, 0.65);
        headGroup.add(ear);

        var earIn = new THREE.Mesh(sph(0.085 * headK * B.ear, 8), belly);
        earIn.position.set(es * 0.27 * headK, 0.26 * headK, -0.07 * headK);
        earIn.scale.set(1, 1, 0.5);
        headGroup.add(earIn);
      }

      // 头顶那撮呆毛：熊二是三瓣软趴趴的，熊大是一根冲天硬刺
      var tuft = new THREE.Group();
      tuft.position.set(0.02 * headK, 0.35 * headK, 0.02 * headK);
      headGroup.add(tuft);
      this.tuft = tuft;

      if (F.tuft === 'spike') {
        var spikeBase = new THREE.Mesh(sph(0.085 * headK, 8), furDark);
        spikeBase.position.y = 0.03 * headK;
        spikeBase.scale.set(1, 0.75, 1);
        tuft.add(spikeBase);

        var spike = new THREE.Mesh(new THREE.ConeGeometry(0.048 * headK, 0.3 * headK, 6), furDark);
        spike.position.set(0.01 * headK, 0.19 * headK, 0);
        spike.rotation.z = 0.18;   // 微微歪着，别像根电线杆
        tuft.add(spike);
      } else {
        var tuftA = new THREE.Mesh(sph(0.09 * headK, 8), furDark);
        tuftA.position.y = 0.05 * headK;
        tuftA.scale.set(1, 0.8, 1);
        tuft.add(tuftA);
        var tuftB = new THREE.Mesh(new THREE.ConeGeometry(0.055 * headK, 0.2 * headK, 6), furDark);
        tuftB.position.set(-0.03 * headK, 0.16 * headK, 0);
        tuftB.rotation.z = 0.35;
        tuft.add(tuftB);
        var tuftC = new THREE.Mesh(new THREE.ConeGeometry(0.045 * headK, 0.16 * headK, 6), furDark);
        tuftC.position.set(0.06 * headK, 0.14 * headK, 0.02 * headK);
        tuftC.rotation.z = -0.4;
        tuft.add(tuftC);
      }

      // ---- 脚下软阴影 ----
      this.shadow = RR.Art.makeBlobShadow(S.shadow);
      root.add(this.shadow);

      this.root.position.set(0, 0, 0);
      this.scene.add(this.root);
    }

    // ==================== 控制 ====================

    moveLeft() {
      if (this.lane > 0) this.lane--;
    }

    moveRight() {
      if (this.lane < 2) this.lane++;
    }

    jump() {
      if (this.jumpCooldown > 0) return;
      if (this.jumping) return;
      if (this.sliding) this.sliding = false;   // 滑铲中起跳 => 立刻打断滑铲
      this.jumping = true;
      this.vy = CFG.PLAYER.jumpVelocity;
      this.jumpCooldown = 0.16;
      // 一次跳跃刚好翻一整圈：落地时角度正好回到 0，不会突然弹一下
      this.jumpTime = 0;
      this.jumpDuration = (2 * CFG.PLAYER.jumpVelocity) / CFG.PLAYER.gravity;
    }

    slide() {
      if (this.sliding) return;
      if (this.jumping) {
        // 空中下滑铲 => 快速砸地，落地后接滑铲
        this.vy = Math.min(this.vy, -6);
        this.jumping = false;
      }
      this.sliding = true;
      this.slideTimer = CFG.PLAYER.slideDuration;
    }

    takeHit() {
      this.invulnTimer = CFG.PLAYER.invulnTime;
      this.hurtFlash = 1;
    }

    /** 血掉光：熊二被光头强逮住，站在原地吓僵住等斧头落下 */
    die() {
      this.dead = true;
      this.deadT = 0;
      this.smashed = 0;
      this.jumping = false;
      this.sliding = false;
      this.slideTimer = 0;
      this.vy = 0;
      this.y = 0;
      this.invulnTimer = 0;
      this.hurtFlash = 0;
      this.ballT = 0;
      this.squash = 0;
    }

    /** 斧头落下：啪叽，拍成一张饼 */
    smash() {
      this.smashed = 1;
      this.invulnTimer = 0;
      this.hurtFlash = 0;
    }

    reset() {
      this.lane = 1;
      this.x = 0;
      this.y = 0;
      this.vy = 0;
      this.jumping = false;
      this.sliding = false;
      this.slideTimer = 0;
      this.jumpCooldown = 0;
      this.invulnTimer = 0;
      this.runCycle = 0;
      this.alive = true;
      this.hurtFlash = 0;
      this.ballT = 0;
      this.squash = 0;
      this.jumpTime = 0;
      this.dead = false;
      this.deadT = 0;
      this.smashed = 0;

      this.body.rotation.set(0, 0, 0);
      this.body.position.set(0, 0, 0);
      this.body.scale.set(1, 1, 1);
      this.headGroup.rotation.set(0, 0, 0);
      this.headGroup.position.y = this.headRestY;
      this.legL.rotation.set(0, 0, 0);
      this.legR.rotation.set(0, 0, 0);
      this.armL.rotation.set(0, 0, 0);
      this.armR.rotation.set(0, 0, 0);
      this.tongue.visible = false;
      this._setEyeScale(1, 1);

      this.root.position.set(0, 0, 0);
      this.root.visible = true;
      this._applyFlash(0);
    }

    // ==================== 每帧 ====================

    update(dt, speed) {
      var P = CFG.PLAYER;

      if (this.jumpCooldown > 0) this.jumpCooldown -= dt;

      // ---- 横向：平滑吸附到目标车道 ----
      var targetX = CFG.LANES[this.lane];
      var maxStep = (CFG.LANE_WIDTH / P.laneSwitchTime) * dt;
      var dx = targetX - this.x;
      if (Math.abs(dx) <= maxStep) this.x = targetX;
      else this.x += (dx > 0 ? 1 : -1) * maxStep;

      // ---- 被光头强逮住：不再接受任何操作，只演死亡姿势 ----
      if (this.dead) {
        this.deadT += dt;
        this.root.position.x = this.x;
        this.root.position.y = this.y;
        this._animateDead(dt);
        this._applyFlash(0);
        return;
      }

      // ---- 纵向：跳跃物理 ----
      if (this.jumping) {
        this.jumpTime += dt;
        this.vy -= P.gravity * dt;
        this.y += this.vy * dt;
        if (this.y <= 0) {
          this.y = 0;
          this.vy = 0;
          this.jumping = false;
          this.squash = 1;      // 落地：啪叽
        }
      }

      // ---- 滑铲计时 ----
      if (this.sliding) {
        this.slideTimer -= dt;
        if (this.slideTimer <= 0) this.sliding = false;
      }

      // ---- 无敌时间 ----
      if (this.invulnTimer > 0) this.invulnTimer -= dt;
      if (this.hurtFlash > 0) this.hurtFlash = Math.max(0, this.hurtFlash - dt * 2.4);

      this.root.position.x = this.x;
      this.root.position.y = this.y;

      this._animate(dt, speed);
      this._applyFlash(this.hurtFlash);
    }

    _approach(cur, target, k, dt) {
      return cur + (target - cur) * Math.min(1, dt * k);
    }

    _setEyeScale(whiteScale, pupilScale) {
      var a = this.eyeAspect;   // 熊二是圆眼（1.15），熊大是眯眼（0.62）
      for (var i = 0; i < this.eyeWhites.length; i++) {
        this.eyeWhites[i].scale.set(whiteScale, whiteScale * a, 0.8);
        this.pupils[i].scale.set(pupilScale, pupilScale * a, 0.7);
      }
    }

    _animate(dt, speed) {
      this.runCycle += dt * Math.max(2.4, speed * 0.62);

      var airborne = this.jumping;
      var sliding = this.sliding;
      var cycle = this.runCycle;

      // 动作进度：滑铲程度做平滑，否则相机和表情小窗会跟着状态位一起抖
      this.jumpProgress = airborne ? Math.min(1, this.jumpTime / this.jumpDuration) : 0;
      this.slideAmount = this._approach(this.slideAmount, sliding ? 1 : 0, sliding ? 12 : 8, dt);

      // 落地压扁回弹
      this.squash = Math.max(0, this.squash - dt * 3.8);
      // 空中缩成球的程度
      this.ballT = this._approach(this.ballT, airborne ? 1 : 0, airborne ? 10 : 8, dt);

      // ---- 身体整体：滑铲躺平 / 空中前倾 / 跑动 ----
      if (sliding) {
        if (this.slideStyle === 'superman') {
          // 熊大：超人式趴滑 —— 脸朝下、肚子贴地、后腿高高翘起
          this.body.rotation.x = this._approach(this.body.rotation.x, -1.5, 15, dt);
          this.body.position.y = this._approach(this.body.position.y, 0.34, 15, dt);
          this.body.position.z = this._approach(this.body.position.z, 0.72, 15, dt);
          this.body.rotation.z = Math.sin(cycle * 6) * 0.09;
          this.body.rotation.y = Math.sin(cycle * 3.6) * 0.08;
        } else {
          this.body.rotation.x = this._approach(this.body.rotation.x, 1.5, 15, dt);
          this.body.position.y = this._approach(this.body.position.y, 0.42, 15, dt);
          this.body.position.z = this._approach(this.body.position.z, -0.78, 15, dt);
          // 躺着扭来扭去
          this.body.rotation.z = Math.sin(cycle * 7) * 0.13;
          this.body.rotation.y = Math.sin(cycle * 4.5) * 0.1;
        }
      } else if (airborne) {
        // 翻一整圈（落地角度正好归零）
        //   熊二＝抱膝前空翻；熊大＝四肢摊开的后空翻
        var prog = Math.min(1, this.jumpTime / this.jumpDuration);
        var dir = this.jumpStyle === 'backflip' ? 1 : -1;
        this.body.rotation.x = dir * Math.PI * 2 * prog;
        this.body.position.y = this._approach(this.body.position.y, 0, 12, dt);
        this.body.position.z = this._approach(this.body.position.z, 0, 12, dt);
        this.body.rotation.z = Math.sin(cycle * 9) * 0.07 * this.ballT;
        this.body.rotation.y = this._approach(this.body.rotation.y, 0, 12, dt);
      } else {
        this.body.rotation.x = this._approach(this.body.rotation.x, 0, 14, dt);
        this.body.position.y = this._approach(this.body.position.y, 0, 14, dt);
        this.body.position.z = this._approach(this.body.position.z, 0, 14, dt);
        // 胖熊跑步：左右摇摆
        this.body.rotation.z = Math.sin(cycle * 2) * 0.07;
        this.body.rotation.y = this._approach(this.body.rotation.y, 0, 14, dt);
      }

      // ---- 压扁 / 绷圆 ----
      // 熊大做后空翻时摊手摊脚，就别缩成球了（ballShape 把它减弱）
      var sq = this.squash;
      var ball = this.ballT * this.ballShape;
      var sy = 1 - 0.16 * ball - 0.34 * sq + 0.10 * (airborne ? Math.max(0, this.vy) * 0.02 : 0);
      var sxz = 1 + 0.16 * ball + 0.26 * sq;
      this.body.scale.set(sxz, sy, sxz);

      // ---- 四肢 ----
      if (sliding) {
        if (this.slideStyle === 'superman') {
          // 熊大：双爪笔直前伸（像在天上飞），后腿翘起来打水
          this.armL.rotation.x = this._approach(this.armL.rotation.x, -2.9, 18, dt);
          this.armR.rotation.x = this._approach(this.armR.rotation.x, -2.9, 18, dt);
          this.armL.rotation.z = this._approach(this.armL.rotation.z, -0.16, 14, dt);
          this.armR.rotation.z = this._approach(this.armR.rotation.z, 0.16, 14, dt);

          var flutterL = Math.sin(cycle * 13) * 0.35;
          var flutterR = Math.sin(cycle * 13 + 2.2) * 0.35;
          this.legL.rotation.x = this._approach(this.legL.rotation.x, -0.45 + flutterL, 20, dt);
          this.legR.rotation.x = this._approach(this.legR.rotation.x, -0.45 + flutterR, 20, dt);
          this.legL.rotation.z = this._approach(this.legL.rotation.z, 0.45, 14, dt);
          this.legR.rotation.z = this._approach(this.legR.rotation.z, -0.45, 14, dt);
        } else {
          // 四只爪子在半空乱蹬（翻不过身的乌龟）
          var kickL = -0.7 + Math.sin(cycle * 11) * 0.85;
          var kickR = -0.7 + Math.sin(cycle * 11 + 2.4) * 0.85;
          this.legL.rotation.x = this._approach(this.legL.rotation.x, kickL, 18, dt);
          this.legR.rotation.x = this._approach(this.legR.rotation.x, kickR, 18, dt);
          this.legL.rotation.z = Math.sin(cycle * 11) * 0.3;
          this.legR.rotation.z = -Math.sin(cycle * 11 + 1.2) * 0.3;

          this.armL.rotation.x = this._approach(this.armL.rotation.x, 0.9 + Math.sin(cycle * 9) * 0.9, 16, dt);
          this.armR.rotation.x = this._approach(this.armR.rotation.x, 0.9 + Math.sin(cycle * 9 + 2.1) * 0.9, 16, dt);
          this.armL.rotation.z = -0.5 - Math.sin(cycle * 9) * 0.3;
          this.armR.rotation.z = 0.5 + Math.sin(cycle * 9 + 1.1) * 0.3;
        }
      } else if (airborne) {
        if (this.jumpStyle === 'backflip') {
          // 熊大：双爪高举过头顶（还左右晃），腿屈起来外八岔开 —— 一整个"我飞起来了！"
          var cheer = Math.sin(cycle * 8) * 0.16;
          this.armL.rotation.x = this._approach(this.armL.rotation.x, -2.95, 14, dt);
          this.armR.rotation.x = this._approach(this.armR.rotation.x, -2.95, 14, dt);
          this.armL.rotation.z = this._approach(this.armL.rotation.z, -0.42 + cheer, 12, dt);
          this.armR.rotation.z = this._approach(this.armR.rotation.z, 0.42 + cheer, 12, dt);

          var legCheer = Math.sin(cycle * 8 + 1.6) * 0.14;
          this.legL.rotation.x = this._approach(this.legL.rotation.x, -1.15, 14, dt);
          this.legR.rotation.x = this._approach(this.legR.rotation.x, -1.15, 14, dt);
          this.legL.rotation.z = this._approach(this.legL.rotation.z, 0.62 + legCheer, 12, dt);
          this.legR.rotation.z = this._approach(this.legR.rotation.z, -0.62 + legCheer, 12, dt);
        } else {
          // 抱住肚子缩成球
          this.legL.rotation.x = this._approach(this.legL.rotation.x, -2.0, 14, dt);
          this.legR.rotation.x = this._approach(this.legR.rotation.x, -2.0, 14, dt);
          this.legL.rotation.z = this._approach(this.legL.rotation.z, 0.28, 12, dt);
          this.legR.rotation.z = this._approach(this.legR.rotation.z, -0.28, 12, dt);

          this.armL.rotation.x = this._approach(this.armL.rotation.x, -2.7, 14, dt);
          this.armR.rotation.x = this._approach(this.armR.rotation.x, -2.7, 14, dt);
          this.armL.rotation.z = this._approach(this.armL.rotation.z, 0.6, 12, dt);
          this.armR.rotation.z = this._approach(this.armR.rotation.z, -0.6, 12, dt);
        }
      } else {
        // 跑动摆臂摆腿（熊大甩得更开，看着更"得瑟"）
        var swing = Math.sin(cycle * 2) * this.runSwing;
        this.legL.rotation.x = this._approach(this.legL.rotation.x, swing, 18, dt);
        this.legR.rotation.x = this._approach(this.legR.rotation.x, -swing, 18, dt);
        this.legL.rotation.z = this._approach(this.legL.rotation.z, 0, 12, dt);
        this.legR.rotation.z = this._approach(this.legR.rotation.z, 0, 12, dt);

        this.armL.rotation.x = this._approach(this.armL.rotation.x, -swing * 0.85, 18, dt);
        this.armR.rotation.x = this._approach(this.armR.rotation.x, swing * 0.85, 18, dt);
        this.armL.rotation.z = this._approach(this.armL.rotation.z, -0.12 + Math.sin(cycle * 2) * 0.12, 14, dt);
        this.armR.rotation.z = this._approach(this.armR.rotation.z, 0.12 + Math.sin(cycle * 2) * 0.12, 14, dt);
      }

      // ---- 头 ----
      if (sliding) {
        // 熊大趴滑时把脖子使劲往上支起来（像超人抬头往前飞），脸才朝着镜头；
        // 熊二仰面朝天，脑袋往后仰，从身后高机位正好看得到正脸。
        var slideHead = this.slideStyle === 'superman' ? 1.15 : 0.62;
        this.headGroup.rotation.x = this._approach(this.headGroup.rotation.x, slideHead, 14, dt);
        this.headGroup.position.y = this._approach(this.headGroup.position.y, this.headRestY, 12, dt);
      } else if (airborne) {
        var airHead = this.jumpStyle === 'backflip' ? -0.3 : -0.12;
        this.headGroup.rotation.x = this._approach(this.headGroup.rotation.x, airHead, 12, dt);
        this.headGroup.position.y = this._approach(
          this.headGroup.position.y, this.headRestY - 0.06, 12, dt);   // 头也往肚子里缩
      } else {
        this.headGroup.rotation.x = this._approach(this.headGroup.rotation.x, 0, 12, dt);
        this.headGroup.position.y = this._approach(
          this.headGroup.position.y, this.headRestY + Math.abs(Math.sin(cycle * 2)) * 0.035, 16, dt);
      }

      // ---- 呆毛甩动 ----
      this.tuft.rotation.z = Math.sin(cycle * 3.1) * 0.22 + (sliding ? 0.4 : 0) + (airborne ? -0.5 : 0);
      this.tuft.rotation.x = (airborne ? -0.6 : 0) * this.ballT + (sliding ? -0.5 : 0);

      // ---- 表情 ----
      var shock = sliding ? 1 : (airborne ? this.ballT : 0);
      this._setEyeScale(1 + shock * 0.42, 1 - shock * 0.32);
      this.tongue.visible = sliding || (airborne && this.vy < -1.2);

      // ---- 影子随高度缩小、变淡 ----
      var h = this.y;
      var scale = Math.max(0.42, 1 - h * 0.26);
      this.shadow.scale.set(scale, scale, 1);
      if (this.shadow.material) {
        this.shadow.material.opacity = Math.max(0.1, 0.34 - h * 0.1);
      }
    }

    /**
     * 死亡演出：
     *   阶段一 —— 被逮住吓僵：站直、举爪投降、全身高频发抖、眼睛瞪圆；
     *   阶段二 —— 斧头落下：整只熊啪叽拍成一张饼（头和四肢都是 body 的子节点，一起扁下去），
     *             四肢摊平、舌头全吐出来、眼睛缩成蚊香眼。
     */
    _animateDead(dt) {
      if (this.smashed > 0.5) {
        var k = Math.min(1, dt * 20);
    
        this.body.rotation.x += (0 - this.body.rotation.x) * k;
        this.body.rotation.y += (0 - this.body.rotation.y) * k;
        this.body.rotation.z += (0 - this.body.rotation.z) * k;
        this.body.position.y += (0 - this.body.position.y) * k;
        this.body.position.z += (0 - this.body.position.z) * k;
        this.body.scale.x += (1.62 - this.body.scale.x) * k;
        this.body.scale.y += (0.16 - this.body.scale.y) * k;
        this.body.scale.z += (1.5 - this.body.scale.z) * k;
    
        this.armL.rotation.x += (0 - this.armL.rotation.x) * k;
        this.armR.rotation.x += (0 - this.armR.rotation.x) * k;
        this.armL.rotation.z += (1.35 - this.armL.rotation.z) * k;
        this.armR.rotation.z += (-1.35 - this.armR.rotation.z) * k;
        this.legL.rotation.x += (0 - this.legL.rotation.x) * k;
        this.legR.rotation.x += (0 - this.legR.rotation.x) * k;
        this.legL.rotation.z += (1.25 - this.legL.rotation.z) * k;
        this.legR.rotation.z += (-1.25 - this.legR.rotation.z) * k;
    
        this.headGroup.rotation.x += (0 - this.headGroup.rotation.x) * k;
        this.headGroup.rotation.y += (0 - this.headGroup.rotation.y) * k;
        this.headGroup.rotation.z += (0 - this.headGroup.rotation.z) * k;
        this.headGroup.position.y += (this.headRestY - this.headGroup.position.y) * k;
    
        this._setEyeScale(0.45, 0.5);
        this.tongue.visible = true;
        this.tongue.scale.set(1.6, 1.5, 1);
        this.tuft.rotation.z += (0 - this.tuft.rotation.z) * k;
        this.tuft.rotation.x += (0 - this.tuft.rotation.x) * k;
        return;
      }
    
      var app = Math.min(1, dt * 15);
      var shake = Math.sin(this.deadT * 46) * 0.05;
    
      this.body.rotation.x += (-0.08 - this.body.rotation.x) * app;
      this.body.rotation.y += (0 - this.body.rotation.y) * app;
      this.body.rotation.z += (shake - this.body.rotation.z) * app;
      this.body.position.y += (0 - this.body.position.y) * app;
      this.body.position.z += (0 - this.body.position.z) * app;
      this.body.scale.x += (1 - this.body.scale.x) * app;
      this.body.scale.y += (1 - this.body.scale.y) * app;
      this.body.scale.z += (1 - this.body.scale.z) * app;
    
      this.armL.rotation.x += (-2.75 - this.armL.rotation.x) * app;
      this.armR.rotation.x += (-2.75 - this.armR.rotation.x) * app;
      this.armL.rotation.z += (0.6 - this.armL.rotation.z) * app;
      this.armR.rotation.z += (-0.6 - this.armR.rotation.z) * app;
    
      this.legL.rotation.x += (shake * 0.5 - this.legL.rotation.x) * app;
      this.legR.rotation.x += (-shake * 0.5 - this.legR.rotation.x) * app;
      this.legL.rotation.z += (0.12 - this.legL.rotation.z) * app;
      this.legR.rotation.z += (-0.12 - this.legR.rotation.z) * app;
    
      this.headGroup.rotation.x += (0.14 - this.headGroup.rotation.x) * app;
      this.headGroup.rotation.y += (0 - this.headGroup.rotation.y) * app;
      this.headGroup.position.y += ((this.headRestY + Math.abs(Math.sin(this.deadT * 40)) * 0.012)
        - this.headGroup.position.y) * app;
    
      this.tuft.rotation.z = Math.sin(this.deadT * 44) * 0.45;
      this.tuft.rotation.x = -0.2;
    
      // 吓得瞪圆眼睛
      this._setEyeScale(1.62, 0.72);
      this.tongue.visible = false;
      this.tongue.scale.set(1, 1, 1);
    }

    _applyFlash(intensity) {
      var flashing = this.invulnTimer > 0 && Math.floor(this.invulnTimer * 12) % 2 === 0;
      var target = Math.max(intensity, flashing ? 0.85 : 0);
      for (var i = 0; i < this.materials.length; i++) {
        this.materials[i].emissive.setRGB(target * 0.65, target * 0.06, target * 0.06);
      }
    }

    // ==================== 碰撞盒 ====================

    get bottom() { return this.y; }

    get top() {
      return this.y + (this.sliding ? CFG.PLAYER.slideHeight : CFG.PLAYER.height);
    }

    get isInvulnerable() { return this.invulnTimer > 0; }

    /**
     * 换角色时整只重建，这里把几何体与材质一并还给显卡，反复切换也不会越攒越多。
     * 玩家身上的资源全部是 _build() 现建的私有对象（材质说明见本文件顶部），
     * 不与场景里任何其它物体共享，所以可以安全释放。
     */
    dispose() {
      this.scene.remove(this.root);
      this.root.traverse(function (o) {
        if (o.geometry && o.geometry.dispose) o.geometry.dispose();
        var m = o.material;
        if (!m) return;
        if (m.length) {
          for (var i = 0; i < m.length; i++) {
            if (m[i] && m[i].dispose) m[i].dispose();
          }
        } else if (m.dispose) {
          m.dispose();
        }
      });
    }
  }

  RR.Player = Player;

})(window);
