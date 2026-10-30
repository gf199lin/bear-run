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

  /** 播个音效。音频模块没加载或被关掉时静默跳过，绝不影响玩法。 */
  function SFX(name) { if (RR.Audio) RR.Audio.sfx(name); }

  /** 背景音乐 / 压音控制，同理做空转兜底 */
  function BGM(op, arg) {
    if (!RR.Audio) return;
    if (op === 'start') RR.Audio.startBgm();
    else if (op === 'stop') RR.Audio.stopBgm();
    else if (op === 'duck') RR.Audio.setDuck(arg);
  }

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

      // 粒子：整局固定 +2 个 draw call，跟粒子数量无关
      this.particles = new RR.Particles(this.scene, this.renderer);
      this.player.particles = this.particles;   // 玩家自己负责起跳/落地/跑动的脚下扬尘

      var self = this;

      // 斧头落下的那一瞬间：熊二被拍成饼 + 镜头猛震 + 漫画字幕「臭狗熊！」
      this.vick.onImpact = function () {
        self.player.smash();
        self.world.addShake(1.6);
        self.hud.showVickShout();
        SFX('axe');            // 处决最重的一记音效，压在字幕弹出的同一帧
        // 斧刃砸地的位置在光头强脚下，不是熊二脚下 —— 星火溅在那儿才对
        var vp = self.vick.root.position;
        self.particles.axe(vp.x, 0.35, vp.z);
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
      // 连击：combo 涨到 2 才有意义（第一颗不算连），comboTimer 归零即断
      this.combo = 0;
      this.comboTimer = 0;
      this.maxCombo = 0;
      this.nearMiss = 0;
      // 每局随机挑战：开局抽一条（见 CFG.CHALLENGES），一局内只会完成一次
      this.challenge = null;
      this.challengeDone = false;
      this.cleanMeters = 0;          // 距离上次撞障碍跑了多少米（steady 挑战用）
      this._forcedChallenge = null;  // ?challenge=<id> / 测试脚本用来钉死某一条
      this._lastChallengeId = null;  // 抽签去重：同一条不连着出现
      this.records = null;           // 个人纪录（结算时读，游戏内不改它）
      this.best = this._loadBest();
      this.inputId = 'keyboard';
      this.startAt = 0;                        // ?startat=<米>：调试用，直接从某区域开局

      this.lastFrame = 0;
      this.fps = 0;
      this._fpsAccum = 0;
      this._fpsFrames = 0;
      this._hudAccum = 0;
      // 已经弹过横幅的区域号；-1 = 还没进过任何区（start() 会置 0）
      this._lastStageBanner = -1;

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
      try { RR.Auth.set(CFG.STORAGE_KEY_INPUT, id); } catch (e) { /* 忽略 */ }
      return src;
    }

    get savedInputId() {
      try {
        var v = RR.Auth.get(CFG.STORAGE_KEY_INPUT);
        if (v === 'keyboard' || v === 'touch' || v === 'radar' || v === 'mock') return v;
      } catch (e) { /* 忽略 */ }
      return this.isCoarse ? 'touch' : 'keyboard';
    }

    // ==================== 角色选择 ====================

    get savedCharId() {
      try {
        var v = RR.Auth.get(CFG.STORAGE_KEY_CHAR);
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

      try { RR.Auth.set(CFG.STORAGE_KEY_CHAR, spec.id); } catch (e) { /* 忽略 */ }
      this.hud.markCharChoice(spec.id);
      this.hud.setCharNote('当前角色：' + spec.name + ' · ' + spec.tagline);
      // 表情小窗跟着换脸：3D 模型是背对镜头的，这张脸才是玩家看到的"正脸"
      this.hud.setFaceCharacter(spec);
      return spec;
    }

    // ==================== 动作分发 ====================

    handleAction(type) {
      if (this.state !== 'playing') return;

      var beforeLane = this.player.lane;
      if (type === 'left') this.player.moveLeft();
      else if (type === 'right') this.player.moveRight();
      else if (type === 'jump') this.player.jump();
      else if (type === 'slide') this.player.slide();

      // 只在真的换了道时才出声（贴着最左道再按左键不算动作）
      if (this.player.lane !== beforeLane) SFX('lane');

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
      this.combo = 0;
      this.comboTimer = 0;
      this.maxCombo = 0;
      this.nearMiss = 0;

      // 调试用（?startat=<米>）：直接从指定区域的里程开局，
      // 好一次性验收 4 个区的配色 / 雾 / 专属摆件，不用真跑 800 米。
      if (this.startAt > 0) this.distance = this.startAt;

      // 随机挑战：抽一条 + 进度清零（要在第一次 _updateHud 之前定下来）
      this.challenge = this._pickChallenge();
      this.challengeDone = false;
      this.cleanMeters = 0;

      this.player.reset();
      this.obstacles.reset(true);
      this.world.updateStagePalette(0);

      // 开局本来就在第 1 区，别一进来就弹"进入新区域"（等真的跑进下一区再弹）；
      // 跳区开局时反而要弹，好确认横幅 + 换天气是一起发生的。
      this._lastStageBanner = this.startAt > 0 ? -1 : 0;

      // 光头强：像《地铁跑酷》的警察一样，开局就咬在熊二屁股后面追
      this.vick.reset();
      this.vick.beginChase();
      SFX('chase');
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
      this.hud.hideCombo();
      this.hud.setChallenge(this.challenge, 0);
      this.hud.el.overlayStart.classList.add('hidden');
      document.body.classList.add('playing');

      this.state = 'playing';
      this.lastFrame = 0;

      if (this.inputId === 'touch') this.hud.showTouchHint();
      if (this.debug.visible) this.debug.logEvent('START');

      // 音乐在这一刻起。start() 必然发生在玩家点击/按键之后，
      // 所以这里也是 AudioContext 的解锁时机（浏览器自动播放策略要求）。
      BGM('duck', false);
      BGM('start');
    }

    pause() {
      if (this.state !== 'playing') return;
      this.state = 'paused';
      this.hud.showPause();
      BGM('duck', true);          // 暂停时把音乐压下去，别在暂停画面上还蹦跶
    }

    resume() {
      if (this.state !== 'paused') return;
      this.hud.hidePause();
      this.state = 'playing';
      this.lastFrame = 0;
      BGM('duck', false);
    }

    gameOver() {
      if (this.state === 'over') return;
      this.state = 'over';

      // 结算前再判一次：最后那一帧刚好达标的挑战不能被吞掉
      this._updateChallenge();

      var finalScore = Math.floor(this.score);

      // 个人纪录：顺序要紧 —— 先读**本局之前**的纪录，再写回最高分。
      // 反过来的话 _loadRecords() 会把刚写进去的最高分并进 records.best，
      // beats.score 就恒为 false：刚破纪录的那一局反而拿不到"最高分"徽标。
      var records = this._loadRecords();
      var distNow = Math.floor(this.distance);
      var beats = {
        score: finalScore > records.best,
        distance: distNow > records.bestDistance,
        combo: this.maxCombo > records.bestCombo,
        nearMiss: this.nearMiss > records.bestNearMiss
      };

      var isNewBest = finalScore > this.best;
      if (isNewBest) {
        this.best = finalScore;
        this._saveBest(this.best);
      }

      var challenge = this._challengeSummary();
      var highlights = this._buildHighlights();
      var nextGoal = this._buildNextGoal(records, finalScore, beats);
      this._pushRecord(records, finalScore, challenge);
      this.records = records;

      this.hud.setBest(this.best);
      this.hud.hideVickShout();
      this.hud.showOver({
        score: finalScore,
        distance: this.distance,
        coins: this.coins,
        nearMiss: this.nearMiss,
        maxCombo: this.maxCombo,
        challenge: challenge,
        best: this.best,
        isNewBest: isNewBest,
        beats: beats,
        records: records,
        highlights: highlights,
        nextGoal: nextGoal,
        history: records.history.slice(1, 1 + CFG.RECORDS.showHistory)
      });
      document.body.classList.remove('playing');

      // 破纪录的号角要压过普通的结算音
      BGM('duck', false);
      if (isNewBest) SFX('best');
      else SFX('gameover');
    }

    toMenu() {
      this.state = 'menu';
      this.hp = CFG.HP.start;
      this.distance = 0;
      this.score = 0;
      this.coins = 0;
      this.boostTimer = 0;
      this.deathTimer = 0;
      this.combo = 0;
      this.comboTimer = 0;
      this.maxCombo = 0;
      this.nearMiss = 0;
      this.challenge = null;
      this.challengeDone = false;
      this.cleanMeters = 0;
      this.player.reset();
      this.vick.reset();
      this.obstacles.reset(false);
      this.world.updateStagePalette(0);
      this.world.setCine(0);
      this.hud.hideVickShout();
      this.hud.setHearts(this.hp, CFG.HP.max);
      this.hud.setBoost(false);
      this.hud.hideCombo();
      this.hud.hideChallenge();
      this.particles.clear();    // 别让上一局的尘土飘进新一局
      this.hud.showStart(this.best);
      BGM('stop');               // 回主菜单：音乐停，别在主菜单里还放着跑步的曲子
    }

    get isPlaying() { return this.state === 'playing'; }

    // ==================== 连击 ====================

    /** 倍率档位：连到 2 才是第 1 档，封顶 maxStep 档 */
    get comboLevel() {
      return Math.min(CFG.COMBO.maxStep, Math.max(0, this.combo - 1));
    }

    /** 当前分数倍率（x1.00 / x1.25 / ... / x2.50） */
    get comboMul() {
      return 1 + this.comboLevel * CFG.COMBO.step;
    }

    /**
     * 加连击并重置计时窗口。
     * 调用顺序很重要：一律"先 _addCombo 再计分"，
     * 所以一串蜂蜜里第一颗永远按 x1.00 算，收益逐颗递增。
     */
    _addCombo(n) {
      if (!n) return;
      this.combo += n;
      this.comboTimer = CFG.COMBO.window;
      if (this.combo > this.maxCombo) this.maxCombo = this.combo;
    }

    /** 断连：撞上、超时、死亡都会走到这里 */
    _breakCombo() {
      this.combo = 0;
      this.comboTimer = 0;
      this.hud.hideCombo();
    }

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
      // 粒子放在渲染前推进：暂停/结算/主菜单里也要继续跑完手上的寿命，
      // 否则死亡那一下的星火会被冻结在半空
      this.particles.update(dt, this.world.camera);
      this.renderer.render(this.scene, this.world.camera);
      this._updateDebug(dt);
    }

    tick(dt) {
      var s = this.state;

      if (s === 'playing') {
        this.input.update(dt);

        // ---- 速度：随距离线性爬升，加速带期间额外乘算 ----
        // 曲线是 config 里的纯函数（热身段 + 爬升段），这里只读数
        var base = CFG.speedAt(this.distance);
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
        this.cleanMeters += advance;      // 只在真正跑动时累计：暂停/处决都不算
        var mult = this.boostTimer > 0 ? CFG.SCORE.boostMul : 1;
        this.score += advance * CFG.SCORE.perMeter * mult;

        this.player.update(dt, this.speed);
        this.obstacles.update(dt, this.speed, this.distance);
        this.world.update(dt, this.speed, this.player.x, this.player.slideAmount);
        this._checkRegion();
        this.vick.update(dt, this.speed, this.player);

        // ---- 连击窗口走表：只在真正跑动时递减，暂停/死亡都不掉连击 ----
        if (this.comboTimer > 0) {
          this.comboTimer -= dt;
          if (this.comboTimer <= 0) this._breakCombo();
        }

        this._resolveCollisions();

        // 加速带尾焰：跟着计时器自己断，不用在别处再关一次
        if (this.boostTimer > 0) this.particles.boost(this.player.x, 0, 0, dt);

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
      var px = this.player.x, py = this.player.y;

      var boostMul = this.boostTimer > 0 ? CFG.SCORE.boostMul : 1;

      if (r.coins) {
        this.coins += r.coins;
        // 一颗一颗结算：倍率在"每颗到手"的瞬间生效，
        // 所以一整串的收益是递增的（x1.00 -> x1.25 -> ...），
        // 而不是"整串都按最后那个倍率算"。
        for (var ci = 0; ci < r.coins; ci++) {
          this._addCombo(CFG.COMBO.coinAdd);
          this.score += CFG.SCORE.perCoin * this.comboMul * boostMul;
        }
        // 一帧里可能吃到一整串蜂蜜：最多叠 4 声，多了会糊成噪音
        var cn = Math.min(r.coins, 4);
        for (var si = 0; si < cn; si++) SFX('coin');
        // 一帧吃一串也只闪一次，闪太多反而看不清
        this.particles.coin(px, py + 0.7, 0);
      }

      if (r.heal) {
        this.hp = Math.min(CFG.HP.max, this.hp + r.heal);
        this.hud.setHearts(this.hp, CFG.HP.max);
        SFX('heal');
        this.particles.heal(px, py + 0.7, 0);
      }

      if (r.boost) {
        this.boostTimer = CFG.BOOST.duration;
        this.hud.setBoost(true);
        SFX('boost');
        this.particles.boost(px, 0, 0, 0.05);   // 踩上去就先喷一簇
      }

      if (r.nearMiss) {
        // 擦过去：和吃蜂蜜同一套口径 —— 先加连击，再按(新)倍率计分
        this.nearMiss += r.nearMiss;
        this._addCombo(CFG.COMBO.nearMissAdd * r.nearMiss);
        this.score += CFG.NEAR_MISS.score * r.nearMiss * this.comboMul * boostMul;
        // 贴边贴出来的分，必须让玩家当场知道"刚才那下是赚的"
        this.particles.coin(px, py + 0.6, 0);
        SFX('slide');
        this.hud.flashNearMiss(r.nearMissBest <= CFG.NEAR_MISS.close);
        if (this.debug.visible) this.debug.logEvent('NEAR MISS');
      }

      if (r.hit && !this.player.isInvulnerable) {
        this.hp--;
        this.cleanMeters = 0;            // 撞了一下：连续干净里程从头算
        this.player.takeHit();
        this.obstacles.grantMercy();     // 掉血后一段里程内只堵一条道
        this._breakCombo();              // 撞了就重头攒：连击是"不犯错"的奖励
        this.hud.setHearts(this.hp, CFG.HP.max);
        this.hud.flashDamage();
        this.world.addShake(1.0);
        this.particles.hit(px, py + 0.6, 0);
        if (this.debug.visible) this.debug.logEvent('HIT');
        if (this.hp > 0) {
          // 掉了血：光头强闻到味儿，立刻掉头猛扑回来
          this.vick.onPlayerHurt();
          SFX('chase');
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
      this._breakCombo();                // 死了不留连击徽标
      this.player.die();
      this.vick.startExecution();
      this.world.addShake(1.4);
      SFX('death');
      BGM('duck', true);          // 处决演出：把音乐让给斧头
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
      if (d) {
        this.hud.showRegionBanner(d.name, d.sub);
        SFX('region');
      }

      // 换区同时"换天气"：雾 / 光照 / 地面颜色平滑过渡到该区调色板，
      // 区域专属摆件（伐木场的原木堆、工地的塔吊集装箱）也靠这个索引显形。
      this.world.updateStagePalette(si);
    }

    _updateHud() {
      this.hud.setScore(Math.floor(this.score));
      this.hud.setDistance(this.distance);
      this.hud.setCoins(this.coins);
      this.hud.setSpeed(this.speed);
      this.hud.setStage(this.obstacles.stageIndex, this.obstacles.stage.name);
      this.hud.setCombo(this.combo, this.comboLevel, this.comboTimer / CFG.COMBO.window);
      this._updateChallenge();
    }

    // ==================== 表情小窗 ====================

    /**
     * 玩家在背后视角里只看得到后脑勺，所以把"脸"单独画在 HUD 上。
     * 状态优先级：死亡 > 受击 > 滞空 > 滑铲 > 跑动。
     */
    // ==================== 每局随机挑战 ====================

    /**
     * 抽一条挑战。同一条不会连着出现两次（除非表里只有一条）；
     * _forcedChallenge 由 ?challenge=<id> 或测试脚本写入，用来钉死某一条方便验收。
     */
    _pickChallenge() {
      var list = CFG.CHALLENGES || [];
      if (!list.length) return null;

      if (this._forcedChallenge) {
        for (var i = 0; i < list.length; i++) {
          if (list[i].id === this._forcedChallenge) {
            this._lastChallengeId = list[i].id;
            return list[i];
          }
        }
      }

      var idx = Math.floor(Math.random() * list.length);
      if (list.length > 1 && list[idx].id === this._lastChallengeId) {
        // 换一条：在剩下的 n-1 条里均匀重抽
        idx = (idx + 1 + Math.floor(Math.random() * (list.length - 1))) % list.length;
      }
      this._lastChallengeId = list[idx].id;
      return list[idx];
    }

    /**
     * 当前进度值，口径与 challenge.target 一致。
     * 加新挑战：这里补一个 case，再往 CFG.CHALLENGES 里写同一套 metric 名就行。
     */
    _challengeProgress() {
      var c = this.challenge;
      if (!c) return 0;
      switch (c.metric) {
        case 'coins': return this.coins;
        case 'nearMiss': return this.nearMiss;
        case 'maxCombo': return this.maxCombo;
        case 'cleanMeters': return this.cleanMeters;
        case 'distance': return this.distance;
        default: return 0;
      }
    }

    /**
     * 挑战推进：达标那一刻一次性发奖（分数 + 粒子 + 提示）。
     * 挂在 _updateHud() 上以 12Hz 跑，另外在 gameOver() 里再补判一次，所以不会漏。
     */
    _updateChallenge() {
      var c = this.challenge;
      if (!c) return;

      var now = this._challengeProgress();
      if (!this.challengeDone && now >= c.target) {
        this.challengeDone = true;
        // 奖励吃加速倍率：踩着加速带冲线更划算
        var boostMul = this.boostTimer > 0 ? CFG.SCORE.boostMul : 1;
        this.score += c.bonus * boostMul;
        this.particles.heal(this.player.x, this.player.y + 0.8, 0);
        SFX('region');
        this.hud.completeChallenge(c);
        if (this.debug.visible) this.debug.logEvent('CHALLENGE');
      }

      // 只在跑动中刷新徽标：死亡/结算画面留给结算面板去讲
      if (this.state === 'playing') this.hud.setChallenge(c, now);
    }

    /** 结算面板用的挑战小结（任务 4 的结算页直接读它） */
    _challengeSummary() {
      var c = this.challenge;
      if (!c) return null;
      var p = this._challengeProgress();
      return {
        id: c.id,
        label: c.label,
        target: c.target,
        bonus: c.bonus,
        progress: p > c.target ? c.target : p,
        text: c.text,               // 结算面板里那句人话（任务 4 用）
        done: !!this.challengeDone
      };
    }

    _updateFace(dt) {
      if (!this.hud) return;
      var p = this.player;
      var st = 'running';

      if (this.state === 'dying' || this.state === 'over') {
        st = (p.smashed >= 0.5) ? 'smashed' : 'dead';
      } else if (p.hurtFlash > 0.45) {
        st = 'hurt';
      } else if (p.jumping) {
        // 回头那一小段换成 peek：小窗里也递给玩家一张正脸
        st = (p.headTurn > 0.5) ? 'peek' : 'airborne';
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

    // ==================== 结算：个人纪录（任务 4） ====================
    // 存的是"逐维度最好成绩"而不是一条总分：
    //   { best, bestDistance, bestCombo, bestNearMiss, plays, history[ historyMax ] }
    // 老版本只有 STORAGE_KEY 里的一个最高分，_emptyRecords() 里做一次迁移，
    // 老玩家打开还是原来的最高分，不会因为换存档格式白丢纪录。

    _emptyRecords() {
      return {
        best: this._loadBest(),   // 迁移：老存档的最高分直接接过来
        bestDistance: 0,
        bestCombo: 0,
        bestNearMiss: 0,
        plays: 0,
        history: []
      };
    }

    /** 读纪录：JSON 坏了 / 字段缺了 / 类型不对，一律降级成能用的值，绝不让结算崩掉 */
    _loadRecords() {
      var out = this._emptyRecords();
      var raw = null;
      try { raw = RR.Auth.get(CFG.STORAGE_KEY_RECORDS); } catch (e) { raw = null; }
      if (!raw) return out;

      var d = null;
      try { d = JSON.parse(raw); } catch (e) { return out; }
      if (!d || typeof d !== 'object') return out;

      // 负数和 NaN 一律当 0：本地存档被改过也不该让纪录变成负数
      var num = function (v) {
        v = Number(v);
        return (isFinite(v) && v > 0) ? v : 0;
      };

      if (num(d.best) > out.best) out.best = num(d.best);
      out.bestDistance = num(d.bestDistance);
      out.bestCombo = num(d.bestCombo);
      out.bestNearMiss = num(d.bestNearMiss);
      out.plays = num(d.plays);

      if (Object.prototype.toString.call(d.history) === '[object Array]') {
        for (var i = 0; i < d.history.length; i++) {
          if (out.history.length >= CFG.RECORDS.historyMax) break;
          var h = d.history[i];
          if (!h || typeof h !== 'object') continue;
          out.history.push({
            score: Math.floor(num(h.score)),
            distance: Math.floor(num(h.distance)),
            coins: Math.floor(num(h.coins)),
            combo: Math.floor(num(h.combo)),
            nearMiss: Math.floor(num(h.nearMiss)),
            won: !!h.won,
            at: num(h.at)
          });
        }
      }
      return out;
    }

    _saveRecords(r) {
      try { RR.Auth.set(CFG.STORAGE_KEY_RECORDS, JSON.stringify(r)); } catch (e) { /* 忽略 */ }
    }

    /** 把本局写进纪录：刷新各维度最高值 + 历史前插（超长直接截断，只留最近几局） */
    _pushRecord(r, score, challenge) {
      r.best = Math.max(r.best, score);
      r.bestDistance = Math.max(r.bestDistance, Math.floor(this.distance));
      r.bestCombo = Math.max(r.bestCombo, this.maxCombo);
      r.bestNearMiss = Math.max(r.bestNearMiss, this.nearMiss);
      r.plays += 1;
      r.history.unshift({
        score: score,
        distance: Math.floor(this.distance),
        coins: this.coins,
        combo: this.maxCombo,
        nearMiss: this.nearMiss,
        won: !!(challenge && challenge.done),
        at: Date.now()
      });
      if (r.history.length > CFG.RECORDS.historyMax) {
        r.history.length = CFG.RECORDS.historyMax;
      }
      this._saveRecords(r);
    }

    /** 本局亮点：够门槛才上榜；一条都没有时也给一句话，不留空白 */
    _buildHighlights() {
      var h = CFG.HIGHLIGHT;
      var list = [];
      if (this.hp >= CFG.HP.max && this.distance >= h.noHitDistance) list.push('毫发无伤');
      if (this.nearMiss >= h.nearMiss) list.push('险躲 ' + this.nearMiss + ' 次');
      if (this.maxCombo >= h.combo) list.push(this.maxCombo + ' 连击');
      if (this.coins >= h.coins) list.push('蜂蜜 ' + this.coins + ' 罐');
      if (this.distance >= h.distance) list.push('跑出 ' + Math.floor(this.distance) + ' 米');
      if (!list.length) {
        list.push(this.distance >= 100 ? '跑了 ' + Math.floor(this.distance) + ' 米' : '刚起步，下一局见');
      }
      return list.slice(0, h.max);
    }

    /**
     * "下一局可以突破什么"：在四个维度里挑差距最小的一项说出来。
     * 注意传入的是**本局之前**的纪录（_pushRecord 还没写），否则差距恒为 0。
     * @param {object} r     本局开始时的纪录
     * @param {number} score 本局得分
     * @param {object} beats 本局破了哪些纪录
     */
    _buildNextGoal(r, score, beats) {
      var me = [
        { metric: 'score',    label: '得分',     best: r.best,         cur: score,                     unit: '' },
        { metric: 'distance', label: '距离',     best: r.bestDistance, cur: Math.floor(this.distance), unit: ' 米' },
        { metric: 'combo',    label: '最长连击', best: r.bestCombo,    cur: this.maxCombo,             unit: '' },
        { metric: 'nearMiss', label: '险躲',     best: r.bestNearMiss, cur: this.nearMiss,             unit: ' 次' }
      ];

      var open = [];
      var hasRecord = false;
      for (var i = 0; i < me.length; i++) {
        var m = me[i];
        if (m.best > 0) hasRecord = true;
        // 本局刚破的、还没有纪录的、已经打平的，都不催
        if (beats[m.metric] || m.best <= 0 || m.cur >= m.best) continue;
        m.gap = m.best - m.cur;
        open.push(m);
      }

      if (!open.length) {
        return {
          metric: hasRecord ? 'all' : 'none',
          remain: 0,
          text: hasRecord
            ? '四项纪录全拿下了 —— 下一局怎么打都是新纪录'
            : '第一局就开张了 —— 下一局开始就有纪录可以追'
        };
      }

      open.sort(function (a, b) { return a.gap - b.gap; });
      var pick = open[0];
      return {
        metric: pick.metric,
        label: pick.label,
        remain: pick.gap,
        best: pick.best,
        text: '还差 ' + pick.gap + pick.unit + ' 就能破' + pick.label
              + '纪录（最好 ' + pick.best + pick.unit + '）'
      };
    }

    // ==================== 最高分持久化 ====================

    _loadBest() {
      try {
        var v = parseInt(RR.Auth.get(CFG.STORAGE_KEY) || '0', 10);
        return isFinite(v) && v > 0 ? v : 0;
      } catch (e) {
        return 0;
      }
    }

    _saveBest(v) {
      try { RR.Auth.set(CFG.STORAGE_KEY, String(v)); } catch (e) { /* 忽略 */ }
    }
  }

  RR.Game = Game;

})(window);
