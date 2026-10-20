/**
 * 游戏主控：状态机 + 主循环 + 计分规则。
 *
 * 状态：menu / playing / paused / over
 * 主菜单时场景仍在慢慢滚动（背后是活的城市），但完全不生成障碍、不判定碰撞。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;

  class Game {
    constructor(canvas) {
      this.canvas = canvas;

      var coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
      this.isCoarse = !!coarse;

      this.renderer = new THREE.WebGLRenderer({
        canvas: canvas,
        antialias: !this.isCoarse,
        powerPreference: 'high-performance'
      });
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      if (typeof THREE.sRGBEncoding !== 'undefined') {
        this.renderer.outputEncoding = THREE.sRGBEncoding;
      }

      this.scene = new THREE.Scene();
      this.world = new RR.World(this.scene);
      this.charId = this.savedCharId;          // 上次选的角色，开局前还能在开始界面换
      this.player = new RR.Player(this.scene, this.charId);
      this.obstacles = new RR.ObstacleManager(this.scene);
      this.obstacles.reset(false);          // 主菜单不放障碍
      this.vick = new RR.Vick(this.scene);
      this.hud = new RR.Hud();
      this.debug = new RR.DebugPanel();

      var self = this;

      // 斧头落下的那一瞬间：熊二被拍成饼 + 镜头猛震 + 漫画字幕「臭狗熊！」
      this.vick.onImpact = function () {
        self.player.smash();
        self.world.addShake(1.6);
        self.hud.showVickShout();
      };

      this.input = new RR.InputManager({
        canvas: canvas,
        getThreats: function () { return self.obstacles.getThreats(); },
        onEvent: function (type) { self.handleAction(type); }
      });

      // ---- 运行时状态 ----
      this.state = 'menu';
      this.hp = CFG.HP.start;
      this.distance = 0;
      this.score = 0;
      this.coins = 0;
      this.speed = CFG.SPEED.start;
      this.boostTimer = 0;
      this.deathTimer = 0;
      this.best = this._loadBest();
      this.inputId = 'keyboard';

      this.lastFrame = 0;
      this.fps = 0;
      this._fpsAccum = 0;
      this._fpsFrames = 0;
      this._hudAccum = 0;

      this._onKey = this._onKey.bind(this);
      this._loop = this._loop.bind(this);
      this._onResize = this._onResize.bind(this);

      window.addEventListener('keydown', this._onKey);
      window.addEventListener('resize', this._onResize);
      window.addEventListener('orientationchange', this._onResize);
      window.addEventListener('blur', function () {
        if (self.state === 'playing') self.pause();
      });
      document.addEventListener('visibilitychange', function () {
        if (document.hidden && self.state === 'playing') self.pause();
      });

      this._onResize();
      this.hud.setHearts(this.hp, CFG.HP.max);
      this.hud.setBest(this.best);
      this.hud.showStart(this.best);

      this.running = false;
    }

    // ==================== 生命周期 ====================

    run() {
      if (this.running) return;
      this.running = true;
      this.lastFrame = 0;
      requestAnimationFrame(this._loop);
    }

    /** 等宽高生效后再调一次，避免 iOS 横屏首帧尺寸错误 */
    _onResize() {
      var w = window.innerWidth;
      var h = window.innerHeight;
      this.renderer.setSize(w, h, false);
      this.world.resize(w, h);
    }

    refreshLayout() {
      this._onResize();
    }

    // ==================== 输入源 ====================

    setInputSource(id) {
      var src = this.input.setSource(id);
      this.inputId = src.id;
      this.hud.setInput(src.label);
      this.hud.markInputChoice(src.id);
      if (src.id === 'radar') {
        this.hud.setRadarNote('雷达地址：' + (src.url || CFG.RADAR.defaultUrl));
      } else if (src.id === 'mock') {
        this.hud.setRadarNote('模拟器：虚拟人体以 30Hz 产出雷达帧，走与真雷达完全相同的解码链路。');
      } else if (src.id === 'touch') {
        this.hud.setRadarNote('触屏：全屏滑动操作。');
      } else {
        this.hud.setRadarNote('键盘：←→ 换道 · ↑/空格 跳 · ↓ 滑铲');
      }
      try { localStorage.setItem(CFG.STORAGE_KEY_INPUT, id); } catch (e) { /* 忽略 */ }
      return src;
    }

    get savedInputId() {
      try {
        var v = localStorage.getItem(CFG.STORAGE_KEY_INPUT);
        if (v === 'keyboard' || v === 'touch' || v === 'radar' || v === 'mock') return v;
      } catch (e) { /* 忽略 */ }
      return this.isCoarse ? 'touch' : 'keyboard';
    }

    // ==================== 角色选择 ====================

    get savedCharId() {
      try {
        var v = localStorage.getItem(CFG.STORAGE_KEY_CHAR);
        var spec = CFG.getCharacter(v);
        if (spec && v === spec.id) return v;
      } catch (e) { /* 忽略 */ }
      return CFG.DEFAULT_CHARACTER;
    }

    /**
     * 换角色：只重建"会动的那只熊"的模型，玩法数值一个都不动。
     * 速度曲线、血量、判定盒、计分、障碍生成全部共用，所以换角色不会影响难度与手感。
     */
    setCharacter(id) {
      var spec = CFG.getCharacter(id);
      if (!spec) return null;
      this.charId = spec.id;

      if (!this.player || this.player.charId !== spec.id) {
        var lane = 1, x = 0, y = 0, vy = 0;
        if (this.player) {
          lane = this.player.lane;
          x = this.player.x;
          y = this.player.y;
          vy = this.player.vy;
          this.player.dispose();
        }
        this.player = new RR.Player(this.scene, spec.id);
        // 保住换人前的位置，菜单里换角色不会让熊"闪"一下
        this.player.lane = lane;
        this.player.x = x;
        this.player.y = y;
        this.player.vy = vy;
        this.player.root.position.set(x, y, 0);

        // 还没开局时顺手把光头强摆回原位，免得他停在旧角色屁股后面
        if (this.state !== 'playing' && this.state !== 'dying') this.vick.reset();
      }

      try { localStorage.setItem(CFG.STORAGE_KEY_CHAR, spec.id); } catch (e) { /* 忽略 */ }
      this.hud.markCharChoice(spec.id);
      this.hud.setCharNote('当前角色：' + spec.name + ' · ' + spec.tagline);
      // 表情小窗跟着换脸：3D 模型是背对镜头的，这张脸才是玩家看到的"正脸"
      this.hud.setFaceCharacter(spec);
      return spec;
    }

    // ==================== 动作分发 ====================

    handleAction(type) {
      if (this.state !== 'playing') return;

      if (type === 'left') this.player.moveLeft();
      else if (type === 'right') this.player.moveRight();
      else if (type === 'jump') this.player.jump();
      else if (type === 'slide') this.player.slide();

      if (this.debug.visible) this.debug.logEvent(type);
    }

    _onKey(e) {
      if (e.repeat) { /* 允许长按，但下面几个键忽略重复 */ }
      var k = e.key;

      if (k === 'p' || k === 'P' || k === 'Escape') {
        if (this.state === 'playing') { this.pause(); e.preventDefault(); }
        else if (this.state === 'paused') { this.resume(); e.preventDefault(); }
        return;
      }

      if (this.state === 'menu' && (k === 'Enter' || k === ' ')) {
        this.start();
        e.preventDefault();
        return;
      }

      if (this.state === 'over' && k === 'Enter') {
        this.start();
        e.preventDefault();
        return;
      }
    }

    // ==================== 状态切换 ====================

    start() {
      this.hp = CFG.HP.start;
      this.distance = 0;
      this.score = 0;
      this.coins = 0;
      this.speed = CFG.SPEED.start;
      this.boostTimer = 0;

      this.player.reset();
      this.obstacles.reset(true);
      this.world.updateStagePalette(0);

      // 光头强：像《地铁跑酷》的警察一样，开局就咬在熊二屁股后面追
      this.vick.reset();
      this.vick.beginChase();
      this.world.setCine(0);
      this.deathTimer = 0;
      this.hud.hideVickShout();

      this.hud.setHearts(this.hp, CFG.HP.max);
      this.hud.setScore(0);
      this.hud.setDistance(0);
      this.hud.setCoins(0);
      this.hud.setSpeed(this.speed);
      this.hud.setBest(this.best);
      this.hud.setBoost(false);
      this.hud.setStage(0, CFG.DIFFICULTY[0].name);
      this.hud.hideAllOverlays();
      this.hud.el.overlayStart.classList.add('hidden');
      document.body.classList.add('playing');

      this.state = 'playing';
      this.lastFrame = 0;

      if (this.inputId === 'touch') this.hud.showTouchHint();
      if (this.debug.visible) this.debug.logEvent('START');
    }

    pause() {
      if (this.state !== 'playing') return;
      this.state = 'paused';
      this.hud.showPause();
    }

    resume() {
      if (this.state !== 'paused') return;
      this.hud.hidePause();
      this.state = 'playing';
      this.lastFrame = 0;
    }

    gameOver() {
      if (this.state === 'over') return;
      this.state = 'over';

      var finalScore = Math.floor(this.score);
      var isNewBest = finalScore > this.best;
      if (isNewBest) {
        this.best = finalScore;
        this._saveBest(this.best);
      }

      this.hud.setBest(this.best);
      this.hud.hideVickShout();
      this.hud.showOver({
        score: finalScore,
        distance: this.distance,
        coins: this.coins,
        best: this.best,
        isNewBest: isNewBest
      });
      document.body.classList.remove('playing');
    }

    toMenu() {
      this.state = 'menu';
      this.hp = CFG.HP.start;
      this.distance = 0;
      this.score = 0;
      this.coins = 0;
      this.boostTimer = 0;
      this.deathTimer = 0;
      this.player.reset();
      this.vick.reset();
      this.obstacles.reset(false);
      this.world.updateStagePalette(0);
      this.world.setCine(0);
      this.hud.hideVickShout();
      this.hud.setHearts(this.hp, CFG.HP.max);
      this.hud.setBoost(false);
      this.hud.showStart(this.best);
    }

    get isPlaying() { return this.state === 'playing'; }

    // ==================== 主循环 ====================

    _loop(ts) {
      if (!this.running) return;
      requestAnimationFrame(this._loop);

      var now = ts / 1000;
      if (!this.lastFrame) this.lastFrame = now;
      var dt = now - this.lastFrame;
      this.lastFrame = now;
      if (dt > 0.05) dt = 0.05;      // 掉帧时不让世界瞬移
      if (dt <= 0) dt = 1 / 60;

      this._fpsAccum += dt;
      this._fpsFrames++;
      if (this._fpsAccum >= 0.5) {
        this.fps = this._fpsFrames / this._fpsAccum;
        this._fpsAccum = 0;
        this._fpsFrames = 0;
      }

      this.tick(dt);
      this._updateFace(dt);
      this.renderer.render(this.scene, this.world.camera);
      this._updateDebug(dt);
    }

    tick(dt) {
      var s = this.state;

      if (s === 'playing') {
        this.input.update(dt);

        // ---- 速度：随距离线性爬升，加速带期间额外乘算 ----
        var base = Math.min(CFG.SPEED.max, CFG.SPEED.start + this.distance * CFG.SPEED.gainPerMeter);
        if (this.boostTimer > 0) {
          this.boostTimer -= dt;
          if (this.boostTimer <= 0) {
            this.boostTimer = 0;
            this.hud.setBoost(false);
          }
        }
        this.speed = base * (this.boostTimer > 0 ? CFG.BOOST.speedMul : 1);

        // ---- 距离与分数（加速期间双倍）----
        var advance = this.speed * dt;
        this.distance += advance;
        var mult = this.boostTimer > 0 ? CFG.SCORE.boostMul : 1;
        this.score += advance * CFG.SCORE.perMeter * mult;

        this.player.update(dt, this.speed);
        this.obstacles.update(dt, this.speed, this.distance);
        this.world.update(dt, this.speed, this.player.x, this.player.slideAmount);
        this._checkRegion();
        this.vick.update(dt, this.speed, this.player);

        this._resolveCollisions();

        this._hudAccum += dt;
        if (this._hudAccum >= 1 / 12) {
          this._hudAccum = 0;
          this._updateHud();
        }
        return;
      }

      if (s === 'dying') {
        // ---- 处决演出：世界迅速刹车，镜头从背后绕到侧面，光头强扑上来 ----
        this.deathTimer += dt;
        this.speed = Math.max(0, this.speed - CFG.VICK.brake * dt);

        var dyingAdvance = this.speed * dt;
        this.distance += dyingAdvance;
        this.score += dyingAdvance * CFG.SCORE.perMeter;

        this.player.update(dt, this.speed);
        this.obstacles.update(dt, this.speed, this.distance);
        this.world.update(dt, this.speed, this.player.x);
        this.vick.update(dt, this.speed, this.player);
        this.world.setCine(Math.min(1, this.deathTimer / CFG.CINE.switchTime));

        this._hudAccum += dt;
        if (this._hudAccum >= 1 / 12) {
          this._hudAccum = 0;
          this._updateHud();
        }

        if (this.vick.executionDone && this.deathTimer >= CFG.VICK.minDeathTime) {
          this.gameOver();
        }
        return;
      }

      if (s === 'menu') {
        // 背景活着，但没有障碍、没有碰撞
        var menuSpeed = 4.2;
        this.player.update(dt, menuSpeed);
        this.world.update(dt, menuSpeed, 0);
        return;
      }

      // paused / over：世界冻结，但让相机轻微呼吸
      this.world._updateCamera(dt, this.player.x);
    }

    _resolveCollisions() {
      var r = this.obstacles.checkCollisions(this.player);

      if (r.coins) {
        this.coins += r.coins;
        this.score += r.coins * CFG.SCORE.perCoin * (this.boostTimer > 0 ? CFG.SCORE.boostMul : 1);
      }

      if (r.heal) {
        this.hp = Math.min(CFG.HP.max, this.hp + r.heal);
        this.hud.setHearts(this.hp, CFG.HP.max);
      }

      if (r.boost) {
        this.boostTimer = CFG.BOOST.duration;
        this.hud.setBoost(true);
      }

      if (r.hit && !this.player.isInvulnerable) {
        this.hp--;
        this.player.takeHit();
        this.hud.setHearts(this.hp, CFG.HP.max);
        this.hud.flashDamage();
        this.world.addShake(1.0);
        if (this.debug.visible) this.debug.logEvent('HIT');
        if (this.hp > 0) {
          // 掉了血：光头强闻到味儿，立刻掉头猛扑回来
          this.vick.onPlayerHurt();
        } else {
          this.hp = 0;
          this._beginDeath();
        }
      }
    }

    /** 熊二倒下：切进处决演出，光头强冲上来把熊二拍成一张饼 */
    _beginDeath() {
      if (this.state === 'dying' || this.state === 'over') return;
      this.state = 'dying';
      this.deathTimer = 0;
      this.boostTimer = 0;
      this.hud.setBoost(false);
      this.player.die();
      this.vick.startExecution();
      this.world.addShake(1.4);
      if (this.debug.visible) this.debug.logEvent('DEAD');
    }

    /**
     * 区域切换 = 视觉上的"我跑进新地方了"。
     * 这里只负责弹横幅；地面配色与区域装饰由 World.updateStagePalette 平滑过渡。
     */
    _checkRegion() {
      var si = this.obstacles.stageIndex;
      if (si === this._lastStageBanner) return;
      this._lastStageBanner = si;

      var d = CFG.DIFFICULTY[si];
      if (d) this.hud.showRegionBanner(d.name, d.sub);
    }

    _updateHud() {
      this.hud.setScore(Math.floor(this.score));
      this.hud.setDistance(this.distance);
      this.hud.setCoins(this.coins);
      this.hud.setSpeed(this.speed);
      this.hud.setStage(this.obstacles.stageIndex, this.obstacles.stage.name);
    }

    // ==================== 表情小窗 ====================

    /**
     * 玩家在背后视角里只看得到后脑勺，所以把"脸"单独画在 HUD 上。
     * 状态优先级：死亡 > 受击 > 滞空 > 滑铲 > 跑动。
     */
    _updateFace(dt) {
      if (!this.hud) return;
      var p = this.player;
      var st = 'running';

      if (this.state === 'dying' || this.state === 'over') {
        st = (p.smashed >= 0.5) ? 'smashed' : 'dead';
      } else if (p.hurtFlash > 0.45) {
        st = 'hurt';
      } else if (p.jumping) {
        st = 'airborne';
      } else if (p.sliding) {
        st = 'sliding';
      }

      this.hud.setFaceState(st);
      this.hud.updateFaceCam(dt, this.speed);
    }

    _updateDebug(dt) {
      var src = this.input.current;
      this.debug.update({
        dt: dt,
        source: src ? src.label : '-',
        connection: src ? src.getStatusText() : '-',
        fps: this.fps,
        radarRate: src ? src.frameRate : 0,
        latency: src ? src.latency : null,
        frame: src ? src.lastRaw : null,
        lane: src ? src.lastLane : null,
        speed: this.speed,
        objects: this.obstacles.count
      });
    }

    // ==================== 最高分持久化 ====================

    _loadBest() {
      try {
        var v = parseInt(localStorage.getItem(CFG.STORAGE_KEY) || '0', 10);
        return isFinite(v) && v > 0 ? v : 0;
      } catch (e) {
        return 0;
      }
    }

    _saveBest(v) {
      try { localStorage.setItem(CFG.STORAGE_KEY, String(v)); } catch (e) { /* 忽略 */ }
    }
  }

  RR.Game = Game;

})(window);
