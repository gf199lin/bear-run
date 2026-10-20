/**
 * 输入层抽象。
 *
 * 游戏主循环只认 4 个动作事件：left / right / jump / slide。
 * 键盘、触屏、雷达、雷达模拟器 都实现同一套接口 —— 第二阶段接真雷达时，
 * 只替换这里的数据源，游戏逻辑一行都不用改。
 *
 * 雷达接口约定（与硬件同学对齐）：
 *   { ts, x, height, jump, crouch, confidence, raw }
 *   x         : 横向位置，归一化到 [-1, 1]，左负右正
 *   height    : 起跳高度，0~1
 *   jump      : 起跳标志，0~1 或布尔
 *   crouch    : 下蹲标志，0~1 或布尔
 *   confidence: 本帧可信度 0~1
 *   ts        : 采集时间戳（毫秒）
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;
  var R = CFG.RADAR;

  function num(v, dflt) {
    var n = Number(v);
    return isFinite(n) ? n : (dflt === undefined ? 0 : dflt);
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : (v > hi ? hi : v);
  }

  // ==================== 基类 ====================

  class BaseInputSource {
    constructor(id, label) {
      this.id = id;
      this.label = label;
      this.onEvent = null;
      this.connected = true;
      this.frameRate = 0;
      this.latency = null;
      this.lastRaw = null;
      this.lastLane = 1;
      this.lastEvents = [];
      this.hint = '';
    }

    attach() {}
    detach() {}
    update() {}

    emit(type) {
      this.lastEvents.push(type);
      if (this.lastEvents.length > 5) this.lastEvents.shift();
      if (this.onEvent) this.onEvent(type);
    }

    getStatusText() {
      return this.connected ? '已就绪' : '未连接';
    }

    static laneName(lane) {
      return lane === 0 ? '左' : (lane === 1 ? '中' : '右');
    }
  }

  // ==================== 键盘 ====================

  class KeyboardInputSource extends BaseInputSource {
    constructor() {
      super('keyboard', '键盘');
      this._onKeyDown = this._onKeyDown.bind(this);
      this.hint = '键盘操作';
    }

    attach() {
      window.addEventListener('keydown', this._onKeyDown);
      this.connected = true;
    }

    detach() {
      window.removeEventListener('keydown', this._onKeyDown);
    }

    _onKeyDown(e) {
      if (e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
      var k = e.key;

      if (k === 'ArrowLeft' || k === 'a' || k === 'A') {
        this.emit('left'); e.preventDefault();
      } else if (k === 'ArrowRight' || k === 'd' || k === 'D') {
        this.emit('right'); e.preventDefault();
      } else if (k === 'ArrowUp' || k === 'w' || k === 'W' || k === ' ' || k === 'Spacebar') {
        this.emit('jump'); e.preventDefault();
      } else if (k === 'ArrowDown' || k === 's' || k === 'S') {
        this.emit('slide'); e.preventDefault();
      }
    }
  }

  // ==================== 触屏（全手势滑动） ====================

  class TouchInputSource extends BaseInputSource {
    constructor(canvas) {
      super('touch', '触屏');
      this.canvas = canvas || document.body;
      this.threshold = 26;      // 滑动判定阈值(px)
      this.active = false;
      this.triggered = false;
      this.pointerId = null;
      this.startX = 0;
      this.startY = 0;
      this.hint = '滑动换道 / 跳跃 / 滑铲';

      this._onDown = this._onDown.bind(this);
      this._onMove = this._onMove.bind(this);
      this._onUp = this._onUp.bind(this);
    }

    attach() {
      var t = this.canvas;
      t.addEventListener('pointerdown', this._onDown, { passive: false });
      t.addEventListener('pointermove', this._onMove, { passive: false });
      t.addEventListener('pointerup', this._onUp);
      t.addEventListener('pointercancel', this._onUp);
      this.connected = true;
    }

    detach() {
      var t = this.canvas;
      t.removeEventListener('pointerdown', this._onDown);
      t.removeEventListener('pointermove', this._onMove);
      t.removeEventListener('pointerup', this._onUp);
      t.removeEventListener('pointercancel', this._onUp);
    }

    _onDown(e) {
      e.preventDefault();
      this.active = true;
      this.triggered = false;
      this.pointerId = e.pointerId;
      this.startX = e.clientX;
      this.startY = e.clientY;
      if (e.target && e.target.setPointerCapture && e.pointerId !== undefined) {
        try { e.target.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
      }
    }

    _onMove(e) {
      if (!this.active || this.triggered) return;
      if (this.pointerId !== null && e.pointerId !== this.pointerId) return;
      e.preventDefault();

      var dx = e.clientX - this.startX;
      var dy = e.clientY - this.startY;
      var adx = Math.abs(dx);
      var ady = Math.abs(dy);

      if (adx < this.threshold && ady < this.threshold) return;

      if (adx > ady) {
        this.emit(dx > 0 ? 'right' : 'left');
      } else {
        this.emit(dy > 0 ? 'slide' : 'jump');
      }
      this.triggered = true;
    }

    _onUp() {
      this.active = false;
      this.triggered = false;
      this.pointerId = null;
    }
  }

  // ==================== 雷达帧解码器 ====================
  // 把连续的雷达帧翻译成离散动作事件。真雷达和模拟器共用同一套解码逻辑，
  // 保证"模拟器上能玩"等价于"接上雷达能玩"。

  class RadarDecoder {
    constructor() {
      this.reset();
      this.frameCount = 0;
      this.lastFrameAt = 0;
      this._intervals = [];
    }

    reset() {
      this.lane = 1;
      this.prevJump = false;
      this.prevCrouch = false;
      this.lastX = 0;
      this.lastHeight = 0;
      this.lastConfidence = 1;
      this.lastRaw = null;
    }

    /**
     * @returns {string[]} 本次帧触发的事件
     */
    push(frame, now) {
      if (!frame || typeof frame !== 'object') return [];

      var events = [];
      var conf = frame.confidence === undefined ? 1 : clamp(num(frame.confidence, 1), 0, 1);

      this.frameCount++;
      this.lastRaw = frame;
      this.lastConfidence = conf;

      if (this.lastFrameAt) {
        this._intervals.push(now - this.lastFrameAt);
        if (this._intervals.length > 24) this._intervals.shift();
      }
      this.lastFrameAt = now;

      if (conf < R.minConfidence) return events;   // 可信度不足，本帧丢弃

      var x = clamp(num(frame.x, 0), -1, 1);
      var height = clamp(num(frame.height, 0), 0, 1);
      this.lastX = x;
      this.lastHeight = height;

      // ---- 车道判定（带迟滞，避免在阈值附近来回抖）----
      var lane = this.lane;
      if (this.lane === 1) {
        if (x <= -R.enterThreshold) lane = 0;
        else if (x >= R.enterThreshold) lane = 2;
      } else if (this.lane === 0) {
        if (x >= R.enterThreshold) lane = 2;
        else if (x >= -R.exitThreshold) lane = 1;
      } else {
        if (x <= -R.enterThreshold) lane = 0;
        else if (x <= R.exitThreshold) lane = 1;
      }

      while (this.lane !== lane) {
        if (lane > this.lane) { events.push('right'); this.lane++; }
        else { events.push('left'); this.lane--; }
      }

      // ---- 跳跃 / 下蹲：只取上升沿 ----
      var jumping = num(frame.jump, 0) >= R.jumpThreshold || height >= R.jumpThreshold;
      if (jumping && !this.prevJump) events.push('jump');
      this.prevJump = jumping;

      var crouching = num(frame.crouch, 0) >= R.crouchThreshold;
      if (crouching && !this.prevCrouch) events.push('slide');
      this.prevCrouch = crouching;

      return events;
    }

    /** 平均帧间隔(ms)，用于调试面板 */
    get averageInterval() {
      if (!this._intervals.length) return 0;
      var sum = 0;
      for (var i = 0; i < this._intervals.length; i++) sum += this._intervals[i];
      return sum / this._intervals.length;
    }
  }

  // ==================== 真雷达（WebSocket 数据源） ====================

  class RadarInputSource extends BaseInputSource {
    constructor(url) {
      super('radar', '雷达');
      this.url = url || R.defaultUrl;
      this.ws = null;
      this.decoder = new RadarDecoder();
      this.connected = false;
      this.closedByUser = false;
      this.retryTimer = null;
      this.statusText = '未连接';
      this.hint = this.url;
    }

    attach() {
      this.closedByUser = false;
      this._connect();
    }

    detach() {
      this.closedByUser = true;
      if (this.retryTimer) { clearTimeout(this.retryTimer); this.retryTimer = null; }
      if (this.ws) {
        try { this.ws.close(); } catch (e) { /* 忽略 */ }
        this.ws = null;
      }
      this.connected = false;
    }

    _connect() {
      var self = this;
      if (typeof WebSocket === 'undefined') {
        this.statusText = '当前环境不支持 WebSocket';
        return;
      }
      this.statusText = '连接中…';
      try {
        this.ws = new WebSocket(this.url);
      } catch (e) {
        this.statusText = '地址无效';
        this._scheduleRetry();
        return;
      }

      this.ws.onopen = function () {
        self.connected = true;
        self.statusText = '已连接';
      };

      this.ws.onmessage = function (evt) {
        var payload;
        try { payload = JSON.parse(evt.data); } catch (e) { return; }
        if (Array.isArray(payload)) {
          for (var i = 0; i < payload.length; i++) self._handleFrame(payload[i]);
        } else {
          self._handleFrame(payload);
        }
      };

      this.ws.onerror = function () {
        self.statusText = '连接错误';
      };

      this.ws.onclose = function () {
        self.connected = false;
        self.statusText = '已断开';
        self.ws = null;
        self._scheduleRetry();
      };
    }

    _scheduleRetry() {
      var self = this;
      if (this.closedByUser || this.retryTimer) return;
      this.retryTimer = setTimeout(function () {
        self.retryTimer = null;
        if (!self.closedByUser) self._connect();
      }, 2000);
    }

    _handleFrame(frame) {
      var now = performance.now();
      this.lastRaw = frame;

      if (frame && frame.ts) {
        var ts = num(frame.ts, 0);
        // ts 看起来像 epoch 毫秒时才能算端到端延迟
        if (ts > 1e12) this.latency = Date.now() - ts;
      }

      var events = this.decoder.push(frame, now);
      this.lastLane = this.decoder.lane;
      for (var i = 0; i < events.length; i++) this.emit(events[i]);

      var avg = this.decoder.averageInterval;
      this.frameRate = avg > 0 ? 1000 / avg : 0;
    }

    update() {
      // 掉线检测：太久没收到帧
      if (this.connected && this.decoder.lastFrameAt) {
        var silent = performance.now() - this.decoder.lastFrameAt;
        if (silent > R.staleMs) this.connected = false;
      }
    }

    getStatusText() { return this.statusText; }
  }

  // ==================== 雷达模拟器（假数据源） ====================
  // 在没有硬件的情况下，用一个"虚拟人体"产生完全相同的帧格式，
  // 走完全相同的解码路径 —— 用来验证雷达链路和调阈值。

  class MockRadarInputSource extends BaseInputSource {
    constructor(getThreats) {
      super('mock', '雷达模拟器');
      this.getThreats = getThreats || function () { return []; };
      this.decoder = new RadarDecoder();
      this.connected = true;
      this.hint = '虚拟人体自动驾驶';

      // 虚拟人体状态
      this.targetLane = 1;
      this.vx = 0;              // 归一化横向位置
      this.vHeight = 0;         // 起跳高度 0~1
      this.crouchTimer = 0;
      this.jumpTimer = 0;
      this.jumpCooldown = 0;
      this.laneCooldown = 0;
      this.emitAccum = 0;
      this.emitInterval = 1 / 30;   // 模拟雷达 30Hz
      this.laneSpeed = 1.55;        // 归一化 x 的变化速度(/秒)
      this.jumpDuration = 0.78;
    }

    attach() { this.connected = true; }
    detach() { this.connected = false; this.decoder.reset(); }

    update(dt) {
      this.jumpCooldown = Math.max(0, this.jumpCooldown - dt);
      this.laneCooldown = Math.max(0, this.laneCooldown - dt);

      this._think(dt);
      this._moveBody(dt);

      // 按模拟帧率产出雷达帧
      this.emitAccum += dt;
      while (this.emitAccum >= this.emitInterval) {
        this.emitAccum -= this.emitInterval;
        this._pushFrame();
      }
    }

    // ---- 自动驾驶决策 ----
    _think() {
      var threats = this.getThreats() || [];
      var myLane = this.targetLane;
      var nearest = null;

      for (var i = 0; i < threats.length; i++) {
        var t = threats[i];
        if (t.lane !== myLane) continue;
        if (t.z < -1) continue;
        if (!nearest || t.z < nearest.z) nearest = t;
      }

      if (!nearest) {
        if (this.laneCooldown <= 0 && myLane !== 1) {
          this.targetLane = 1;
          this.laneCooldown = 0.4;
        }
        return;
      }

      if (nearest.kind === 'low' && nearest.z < 9.5 && this.jumpCooldown <= 0 && this.jumpTimer <= 0) {
        this.jumpTimer = 0.0001;
        this.jumpCooldown = 0.5;
        return;
      }

      if (nearest.kind === 'high' && nearest.z < 7.5 && this.crouchTimer <= 0) {
        this.crouchTimer = CFG.PLAYER.slideDuration + 0.1;
        return;
      }

      if (nearest.z < 15 && this.laneCooldown <= 0) {
        var safe = this._findSafeLane(threats);
        if (safe !== myLane) {
          this.targetLane = safe;
          this.laneCooldown = 0.5;
        }
      }
    }

    _findSafeLane(threats) {
      var blocked = {};
      for (var i = 0; i < threats.length; i++) {
        if (threats[i].z < 24 && threats[i].z > -2) blocked[threats[i].lane] = true;
      }
      var order = [1, 0, 2];
      for (var j = 0; j < order.length; j++) {
        if (!blocked[order[j]]) return order[j];
      }
      return this.targetLane;
    }

    // ---- 虚拟人体运动 ----
    _moveBody(dt) {
      var targetX = [-0.72, 0, 0.72][this.targetLane];
      var d = targetX - this.vx;
      var step = this.laneSpeed * dt;
      if (Math.abs(d) <= step) this.vx = targetX;
      else this.vx += (d > 0 ? 1 : -1) * step;

      if (this.jumpTimer > 0) {
        this.jumpTimer += dt;
        if (this.jumpTimer >= this.jumpDuration) {
          this.jumpTimer = 0;
          this.vHeight = 0;
        } else {
          this.vHeight = Math.sin((this.jumpTimer / this.jumpDuration) * Math.PI);
        }
      } else {
        this.vHeight = 0;
      }

      if (this.crouchTimer > 0) this.crouchTimer = Math.max(0, this.crouchTimer - dt);
    }

    // ---- 产出与真雷达同构的帧 ----
    _pushFrame() {
      var frame = {
        ts: Date.now(),
        x: Number(this.vx.toFixed(3)),
        height: Number(this.vHeight.toFixed(3)),
        jump: this.vHeight > R.jumpThreshold ? 1 : 0,
        crouch: this.crouchTimer > 0 ? 1 : 0,
        confidence: 0.94,
        raw: { sim: true }
      };

      this.lastRaw = frame;
      var events = this.decoder.push(frame, performance.now());
      this.lastLane = this.decoder.lane;
      for (var i = 0; i < events.length; i++) this.emit(events[i]);

      var avg = this.decoder.averageInterval;
      this.frameRate = avg > 0 ? 1000 / avg : 0;
      this.latency = 0;
    }

    getStatusText() { return '模拟运行中'; }
  }

  // ==================== 输入管理器 ====================

  class InputManager {
    constructor(options) {
      options = options || {};
      this.onEvent = options.onEvent || function () {};
      this.canvas = options.canvas;
      this.getThreats = options.getThreats || function () { return []; };
      this.radarUrl = options.radarUrl || R.defaultUrl;
      this.source = null;
    }

    setSource(id) {
      if (this.source) {
        this.source.detach();
        this.source = null;
      }

      var src;
      if (id === 'touch') src = new TouchInputSource(this.canvas);
      else if (id === 'radar') src = new RadarInputSource(this.radarUrl);
      else if (id === 'mock') src = new MockRadarInputSource(this.getThreats);
      else src = new KeyboardInputSource();

      var self = this;
      src.onEvent = function (type) { self.onEvent(type); };
      src.attach();
      this.source = src;
      return src;
    }

    update(dt) {
      if (this.source && this.source.update) this.source.update(dt);
    }

    detach() {
      if (this.source) {
        this.source.detach();
        this.source = null;
      }
    }

    get current() { return this.source; }
  }

  RR.BaseInputSource = BaseInputSource;
  RR.KeyboardInputSource = KeyboardInputSource;
  RR.TouchInputSource = TouchInputSource;
  RR.RadarDecoder = RadarDecoder;
  RR.RadarInputSource = RadarInputSource;
  RR.MockRadarInputSource = MockRadarInputSource;
  RR.InputManager = InputManager;

})(window);
