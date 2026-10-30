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
      this.lastLaneAt = 0;
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

      // 横向增益：雷达量程和游戏车道宽度不一定匹配（手一晃就打满 ±1），
      // 现场校准学到的 xGain 在这里生效。热读 CFG.RADAR，改参数不用重载页面。
      var gain = num(R.xGain, 1);
      var x = num(frame.x, 0);
      if (gain > 0 && gain !== 1) x = x * gain;
      x = clamp(x, -1, 1);
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

      // ---- 单帧限档 + 换道冷却 ----
      // 上面的判定只回答"按当前 x 我应该在哪条道"。一次大幅挥手时 x 可能从 -1 直接打到 +1，
      // 不限制的话下面的 while 会立刻发两次 right/left —— 玩家看到的就是"横跨两个跑道"。
      // 所以：单帧最多走 maxStep 格；两次换道之间至少隔 laneCooldownMs。
      var maxStep = Math.max(1, Math.floor(num(R.maxLaneStepPerFrame, 1)));
      if (lane > this.lane + maxStep) lane = this.lane + maxStep;
      else if (lane < this.lane - maxStep) lane = this.lane - maxStep;

      if (lane !== this.lane) {
        var cd = num(R.laneCooldownMs, 0);
        if (cd > 0 && this.lastLaneAt && now - this.lastLaneAt < cd) {
          lane = this.lane;          // 冷却中：这一帧先不换，下一帧继续判（不会丢动作）
        } else {
          this.lastLaneAt = now;
        }
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
        self.openedAt = performance.now();
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
      // 数据到达即视为在线：掉线/无数据之后上游恢复推流，不用刷新页面
      if (!this.connected) {
        this.connected = true;
        this.statusText = '已连接';
      }
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
      // 掉线 / 空流检测。
      // 关键点：ws.onopen 只代表握手成功，不代表有人在推帧。
      // 之前只在 lastFrameAt 非 0 时才判超时，于是「连上但一帧都没来」会永远显示已连接
      // —— 直播现场最容易被这个假绿灯骗到。现在用 openedAt 兜底判「无数据」。
      if (!this.connected) return;
      var now = performance.now();
      // 自适应掉线判定：上下雷达实测只有 1.92Hz（521ms/帧），固定 500ms 会让它
      // 每一帧到达前都被判「掉线」、下一帧又恢复，状态栏反复闪，看起来像「时连时断」。
      // 取「实测帧间隔 × 3」和 staleMs 的较大者：高帧率雷达仍保持 500ms 的灵敏判定。
      var avg = this.decoder.averageInterval;
      var stale = Math.max(R.staleMs, avg > 0 ? avg * 3 : R.staleMs * 3);
      if (this.decoder.lastFrameAt) {
        if (now - this.decoder.lastFrameAt > stale) {
          this.connected = false;
          this.statusText = '掉线';
        }
      } else if (this.openedAt && now - this.openedAt > R.staleMs * 3) {
        this.connected = false;
        this.statusText = '无数据';
      }
    }

    getStatusText() { return this.statusText; }
  }

  // ==================== 双雷达（一路管左右、一路管上下） ====================
  // 两台雷达同时接入：LR 只负责 x（换道），UD 只负责 jump / crouch / height（跳 / 滑铲）。
  //
  // 关键做法是「按角色裁剪帧」而不是「按事件过滤」：
  //   每台雷达各跑一个独立的 RadarDecoder，但在进解码器之前把不属于它的维度清零
  //   —— LR 帧的 jump/crouch/height 一律置 0，UD 帧的 x 一律置 0。
  // 这样两台雷达可以直接发同一套完整帧格式（数据侧不用为「我是哪一路」改协议），
  // 游戏侧也不依赖上游字段守规矩：LR 永远产生不出 jump，UD 永远换不了道。
  //
  // 两路连接、重连、掉线检测完全独立：一台掉线时另一台照常出动作，
  // 状态栏会写清楚哪一路断了，而不是整个雷达源判死。

  /** 把不属于本通道的维度清零（原地不改原帧，复制一份） */
  function pickRoleFrame(frame, role) {
    if (!frame || typeof frame !== 'object') return null;
    var out = {}, k;
    for (k in frame) {
      if (Object.prototype.hasOwnProperty.call(frame, k)) out[k] = frame[k];
    }
    if (role === 'lr') {
      out.height = 0;
      out.jump = 0;
      out.crouch = 0;
    } else {
      out.x = 0;
    }
    return out;
  }

  /** 双雷达里的一路连接：自己连、自己重连、自己解码 */
  class RadarChannel {
    constructor(role, url, owner) {
      this.role = role;                 // 'lr' = 左右, 'ud' = 上下
      this.url = url;
      this.owner = owner;
      this.ws = null;
      this.decoder = new RadarDecoder();
      this.connected = false;
      this.statusText = '未连接';
      this.closedByUser = false;
      this.retryTimer = null;
      this.openedAt = 0;
      this.lastRaw = null;
      this.latency = null;
      this.frames = 0;
    }

    get label() { return this.role === 'lr' ? '左右雷达' : '上下雷达'; }

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
      this.statusText = '已断开';
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
        self.openedAt = performance.now();
        self.statusText = '已连接';
      };

      this.ws.onmessage = function (evt) {
        var payload;
        try { payload = JSON.parse(evt.data); } catch (e) { return; }
        if (Array.isArray(payload)) {
          for (var i = 0; i < payload.length; i++) self._dispatch(payload[i]);
        } else {
          self._dispatch(payload);
        }
      };

      this.ws.onerror = function () { self.statusText = '连接错误'; };

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

    /**
     * 帧的总入口。
     * 左右通道必须先经过 owner._handleFrame：校准（Calibration.install）就是装饰这一层，
     * 学到的 x0 / sign 才会作用到横向读数上；上下通道不经过校准，直接进自己的解码器。
     */
    _dispatch(frame) {
      if (this.role === 'lr' && this.owner && typeof this.owner._handleFrame === 'function') {
        this.owner._handleFrame(frame);
      } else {
        this.ingest(frame);
      }
    }

    /** 收到一帧：按角色裁剪 -> 解码 -> 交给双雷达聚合 */
    ingest(frame) {
      if (!frame || typeof frame !== 'object') return;

      // 有帧就恢复在线：网络抖动造成的「掉线」不该逼用户刷新页面
      if (!this.connected) {
        this.connected = true;
        this.statusText = '已连接';
      }

      var now = performance.now();
      var f = pickRoleFrame(frame, this.role);

      if (f.ts) {
        var ts = num(f.ts, 0);
        if (ts > 1e12) this.latency = Date.now() - ts;
      }

      this.frames++;
      var events = this.decoder.push(f, now);

      // 存裁剪后的帧：调试面板看到的是"这一路真正喂给解码器的值"
      f.channel = this.role;
      this.lastRaw = f;

      if (this.owner) this.owner._onChannelFrame(this, events);
    }

    update() {
      // 与单雷达同一条判据：握手成功 ≠ 有数据。
      // 从没收到过帧时用 openedAt 兜底（3×staleMs），否则会一直显示「已连接」。
      if (!this.connected) return;
      var now = performance.now();
      // 自适应掉线判定：上下雷达实测只有 1.92Hz（521ms/帧），固定 500ms 会让它
      // 每一帧到达前都被判「掉线」、下一帧又恢复，状态栏反复闪，看起来像「时连时断」。
      // 取「实测帧间隔 × 3」和 staleMs 的较大者：高帧率雷达仍保持 500ms 的灵敏判定。
      var avg = this.decoder.averageInterval;
      var stale = Math.max(R.staleMs, avg > 0 ? avg * 3 : R.staleMs * 3);
      if (this.decoder.lastFrameAt) {
        if (now - this.decoder.lastFrameAt > stale) {
          this.connected = false;
          this.statusText = '掉线';
        }
      } else if (this.openedAt && now - this.openedAt > R.staleMs * 3) {
        this.connected = false;
        this.statusText = '无数据';
      }
    }

    get frameRate() {
      var avg = this.decoder.averageInterval;
      return avg > 0 ? 1000 / avg : 0;
    }
  }

  class DualRadarInputSource extends BaseInputSource {
    constructor(lrUrl, udUrl) {
      super('radar', '双雷达');
      this.lrUrl = lrUrl || R.defaultUrl;
      this.udUrl = udUrl || '';
      this.lr = new RadarChannel('lr', this.lrUrl, this);
      this.ud = new RadarChannel('ud', this.udUrl, this);
      this.url = this.lrUrl;                       // 兼容旧代码读 src.url
      this.urlText = '左右 ' + this.lrUrl + ' · 上下 ' + this.udUrl;
      this.hint = this.urlText;
      this.statusText = '连接中…';
      this.connected = false;
      this.lastRaw = null;                         // 合并帧（供校准 / 调试面板）
      this.lastLane = 1;
      this.frameRate = 0;
      this.latency = null;
    }

    attach() { this.lr.attach(); this.ud.attach(); }

    detach() {
      this.lr.detach();
      this.ud.detach();
      this.lastRaw = null;
      this.connected = false;
    }

    /**
     * 校准（Calibration.install）装饰的入口。
     * 只把帧转给左右通道 => 校准学到的 x0/sign 只作用在横向上，
     * 上下通道不受影响（它的 x 本来就会被清零）。
     */
    _handleFrame(frame) { this.lr.ingest(frame); }

    /** 一路解出了事件：只放行本路负责的动作，再重建合并帧 */
    _onChannelFrame(channel, events) {
      var allow = channel.role === 'lr'
        ? { left: 1, right: 1 }
        : { jump: 1, slide: 1 };
      for (var i = 0; i < events.length; i++) {
        if (allow[events[i]]) this.emit(events[i]);
      }
      this._rebuildMerged();
    }

    /**
     * 合并帧：x 取左右雷达，height/jump/crouch 取上下雷达。
     * 这是「玩家实际生效的输入」——调试面板与校准向导都读它。
     * 引用要稳定（每次收帧才换新对象），否则校准的新鲜度判断会一直当成新帧。
     */
    _rebuildMerged() {
      var lr = this.lr.lastRaw, ud = this.ud.lastRaw;
      if (!lr && !ud) return;

      var out = {
        ts: Date.now(),
        // 左右雷达还没来过帧时给 NaN：解码器里会当成 0（不换道），
        // 而校准向导的 _validX 会判成"无效" —— 不会把"没数据"学成基线 0。
        x: lr ? num(lr.x, 0) : NaN,
        height: ud ? num(ud.height, 0) : 0,
        jump: ud ? num(ud.jump, 0) : 0,
        crouch: ud ? num(ud.crouch, 0) : 0,
        confidence: lr ? num(lr.confidence, 1) : (ud ? num(ud.confidence, 1) : 1),
        source: 'dual',
        lr: lr,
        ud: ud
      };
      if (lr && lr.calibrated) {
        out.calibrated = true;
        out.rawX = lr.rawX;
      }
      this.lastRaw = out;
    }

    update() {
      this.lr.update();
      this.ud.update();

      // connected 只跟左右通道走：校准向导与"雷达掉线"提示都以横向可用性为准，
      // 上下通道的健康状况单独用 udOnline / statusText 暴露。
      // 两路都断才真的没输入，此时 connected=false 同时满足旧逻辑。
      var lrOk = this.lr.connected;
      var udOk = this.ud.connected;
      this.connected = lrOk;
      this._bothOnline = lrOk && udOk;
      this.statusText = '左右 ' + (lrOk ? '已连接' : this.lr.statusText)
        + ' · 上下 ' + (udOk ? '已连接' : this.ud.statusText);

      var a = this.lr.frameRate, b = this.ud.frameRate;
      this.frameRate = (a > 0 && b > 0) ? Math.min(a, b) : (a || b);

      if (this.lr.latency !== null && this.ud.latency !== null) {
        this.latency = Math.max(this.lr.latency, this.ud.latency);
      } else {
        this.latency = this.lr.latency !== null ? this.lr.latency : this.ud.latency;
      }

      this.lastLane = this.lr.decoder.lane;
    }

    getStatusText() { return this.statusText; }

    /** 两路是否都在线（界面提示用；游戏逻辑不依赖它） */
    get bothOnline() { return !!this._bothOnline; }
    get lrOnline() { return this.lr.connected; }
    get udOnline() { return this.ud.connected; }
  }

  // ==================== 后台判定结果（游戏只收 action） ====================
  // 方案 B：判定（阈值 / 车道迟滞 / 换道冷却 / 上升沿 / 防抖）全部在后台
  // radar_bridge.py --judge 里完成。本类**不做任何判定**，只做三件事：
  //   1. 连上 radar_hub.py，把 {type:'action'} 转成游戏事件
  //   2. 把 {type:'state'} 存进 lastRaw，供调试面板显示 x / 高度 / 在线状态
  //   3. 掉线检测 + 重连 + 重复动作去重
  //
  // 关键约定：收到没有 action 字段的旧式连续帧时，只更新面板，**绝不**自己判动作。
  // 这样「游戏只接受结果」在代码层面是可验证的 —— 前端没有第二个判定点。

  var ACTION_NAMES = { left: 1, right: 1, jump: 1, slide: 1 };

  class RadarActionInputSource extends BaseInputSource {
    constructor(url) {
      super('radar', '雷达（后台判定）');
      this.url = url || R.actionUrl || R.defaultUrl;
      this.ws = null;
      this.connected = false;
      this.closedByUser = false;
      this.retryTimer = null;
      this.statusText = '未连接';
      this.hint = this.url;
      this.supportsCalibration = false;     // 阈值在后台，前端校准没有意义
      this.lastFrameAt = 0;
      this.lastActionAt = 0;
      this.lastSeq = null;
      this._dedup = {};
      this._frameTimes = [];
      this.frameRate = 0;
      this.latency = null;
      this.lastRaw = null;
      this.lrOk = null;                     // 来自后台 state 帧
      this.udOk = null;
      this.actionCounts = { left: 0, right: 0, jump: 0, slide: 0 };
      this.unjudgedFrames = 0;              // 收到却没有 action 的帧（上游没开 --judge）
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
      this.statusText = '已断开';
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
        self.openedAt = performance.now();
        self.statusText = '已连接';
      };

      this.ws.onmessage = function (evt) {
        var payload;
        try { payload = JSON.parse(evt.data); } catch (e) { return; }
        if (Array.isArray(payload)) {
          for (var i = 0; i < payload.length; i++) self._ingest(payload[i]);
        } else {
          self._ingest(payload);
        }
      };

      this.ws.onerror = function () { self.statusText = '连接错误'; };

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

    /** 单帧总入口：只认 type，不认识就当状态显示 */
    _ingest(obj) {
      if (!obj || typeof obj !== 'object') return;

      var now = performance.now();
      if (!this.connected) {
        this.connected = true;
        this.statusText = '已连接';
      }
      this.lastFrameAt = now;
      this._tickRate(now);

      if (obj.ts) {
        var ts = num(obj.ts, 0);
        if (ts > 1e12) this.latency = Date.now() - ts;
      }

      if (obj.type === 'state') {
        this.lastRaw = obj;
        this.lrOk = obj.lr_ok === undefined ? this.lrOk : !!obj.lr_ok;
        this.udOk = obj.ud_ok === undefined ? this.udOk : !!obj.ud_ok;
        if (obj.lane !== undefined) this.lastLane = obj.lane;
        return;
      }

      var act = obj.action;
      if (!act) {
        // 上游还在发连续帧（bridge 没开 --judge）：只显示，不判定。
        this.unjudgedFrames++;
        this.lastRaw = obj;
        if (this.unjudgedFrames === 30) {
          this.statusText = '上游未开 --judge（收不到动作）';
        }
        return;
      }
      if (!ACTION_NAMES[act]) return;
      if (!this._accept(act, obj, now)) return;

      this.actionCounts[act]++;
      this.lastActionAt = now;
      if (obj.lane !== undefined) this.lastLane = obj.lane;
      this.emit(act);
    }

    /** 去重：后台 seq 相同直接丢；同一动作在 actionDedupMs 内重复也丢 */
    _accept(act, obj, now) {
      var seq = obj.seq;
      if (seq !== undefined && seq !== null) {
        if (seq === this.lastSeq) return false;
        this.lastSeq = seq;
      }
      var ms = num(R.actionDedupMs, 70);
      if (ms > 0) {
        var last = this._dedup[act] || 0;
        if (now - last < ms) return false;
        this._dedup[act] = now;
      }
      return true;
    }

    _tickRate(now) {
      this._frameTimes.push(now);
      while (this._frameTimes.length > 25) this._frameTimes.shift();
      if (this._frameTimes.length > 2) {
        var span = this._frameTimes[this._frameTimes.length - 1] - this._frameTimes[0];
        if (span > 0) this.frameRate = (this._frameTimes.length - 1) * 1000 / span;
      }
    }

    update() {
      if (!this.connected) return;
      var now = performance.now();
      var stale = num(R.actionStaleMs, 1200);
      if (this.lastFrameAt && now - this.lastFrameAt > stale) {
        this.connected = false;
        this.statusText = '后台没数据';
      }
    }

    /** 兼容 Calibration.install / 外部手工喂帧：只记录，不产生任何动作 */
    _handleFrame(frame) {
      if (frame && typeof frame === 'object') this.lastRaw = frame;
    }

    getStatusText() {
      var extra = '';
      if (this.lrOk === false) extra += ' · 左右断';
      if (this.udOk === false) extra += ' · 上下断';
      return this.statusText + extra;
    }

    /** 与双雷达源保持同名字段，避免面板/向导读不到时崩 */
    get bothOnline() { return this.lrOk !== false && this.udOk !== false; }
    get lrOnline() { return this.lrOk !== false; }
    get udOnline() { return this.udOk !== false; }
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

  // ==================== 雷达数据回放（离线 .bin 的解码结果） ====================

  /**
   * 把「真实雷达采集文件」解出的每帧特征，按原始帧率回放成与真雷达同构的帧，
   * 走完全相同的解码链路（车道迟滞 / 跳 / 蹲 / 掉线统计）。
   *
   * 数据由 .deepworks/tmp/.../_work/decode_to_replay.py 生成，形状：
   *   window.RR_RADAR_REPLAY = {
   *     meta:{...},
   *     series:{ 'pos_0p70':{ frames, frameBytes, rate, energy:[...], rms:[...] } }
   *   }
   *
   * ⚠ 判定层是「占位」的，别把它当识别结果：
   *   这批数据只有静息段、没有逐帧动作真值，也没解出角度维，
   *   因此 x 恒为 0；jump/crouch 只用「本帧总能量相对滑动中位数的 z 分数」近似。
   *   它验证的是 数据 → 解码 → 游戏 的通路，以及静息状态下的误触发基线。
   *
   * URL 参数：?input=replay&replay=<js路径>&series=<站点>&replayk=<z阈值>&replayspeed=<倍速>
   */
  class ReplayRadarInputSource extends BaseInputSource {
    constructor(opts) {
      super('replay', '雷达回放');
      opts = opts || {};

      this.decoder = new RadarDecoder();
      this.url = opts.url || './_radarreplay/radar-replay.js';
      this.wantSeries = opts.series || null;
      this.k = num(opts.k, 3.2);                       // z 分数阈值
      this.speed = Math.max(0.25, num(opts.speed, 1)); // 回放倍速
      this.loop = opts.loop !== false;
      this.windowSize = Math.max(8, Math.round(num(opts.window, 40)));
      // 'energy'  = 用真实雷达特征 + z 分数占位判定（v2 现状）
      // 'truth'   = 用采集时的动作真值直接驱动（识别 100% 正确的上限对照）
      // 'azimuth' = 幅度门控 + 双接收相位差(方位)驱动换道（需数据里有 az 数组）
      this.mode = opts.mode === 'truth' ? 'truth'
                : (opts.mode === 'azimuth' ? 'azimuth' : 'energy');
      // 方位解码参数：null = 未指定，留给数据侧(series)的标定值；URL 显式给出才覆盖
      this.azLeft = opts.azLeft === undefined || opts.azLeft === null ? null : num(opts.azLeft, -1.55);
      this.azRight = opts.azRight === undefined || opts.azRight === null ? null : num(opts.azRight, 9.9);
      this.ampGate = opts.ampGate === undefined || opts.ampGate === null ? null : num(opts.ampGate, 3600);
      this.azNeed = opts.azNeed === undefined || opts.azNeed === null ? null
        : Math.max(1, Math.round(num(opts.azNeed, 3)));
      this.azWin = opts.azWin === undefined || opts.azWin === null ? null
        : Math.max(2, Math.round(num(opts.azWin, 10)));
      this._azBuf = [];
      this.lastAz = NaN;
      this.from = num(opts.from, 0);

      this.series = null;
      this.seriesName = '';
      this.hint = '回放 ' + this.url;
      this.statusText = '回放数据加载中…';
      this.connected = false;

      this._clock = 0;    // 虚拟时钟(ms)
      this._idx = 0;
      this._win = [];
      this._period = 100;
      this.stats = { frames: 0, jump: 0, slide: 0, lane: 0, end: 0, zmax: 0, rate: 0 };
      this._el = null;
    }

    attach() {
      var self = this;
      if (global.RR_RADAR_REPLAY) { this._ready(); return; }

      var s = document.createElement('script');
      s.src = this.url;
      s.onload = function () {
        if (global.RR_RADAR_REPLAY) self._ready();
        else { self.statusText = '回放数据格式不对'; }
        self._publish(true);
      };
      s.onerror = function () {
        self.statusText = '回放数据没找到：' + self.url;
        self._publish(true);
      };
      document.head.appendChild(s);
    }

    detach() { this.connected = false; }

    _ready() {
      var data = global.RR_RADAR_REPLAY;
      var names = Object.keys(data.series || {});
      if (!names.length) { this.statusText = '回放数据为空'; return; }
      var pick = (this.wantSeries && data.series[this.wantSeries]) ? this.wantSeries : names[0];

      this.seriesName = pick;
      this.series = data.series[pick];
      // 方位解码参数：URL 显式给出 > 数据侧(series)标定 > 内置默认
      if (this.mode === 'azimuth') {
        var s = this.series;
        if (this.azLeft === null) this.azLeft = num(s.azLeft, -1.55);
        if (this.azRight === null) this.azRight = num(s.azRight, 9.9);
        if (this.ampGate === null) this.ampGate = num(s.ampGate, 3600);
        if (this.azNeed === null) this.azNeed = Math.max(1, Math.round(num(s.azNeed, 3)));
        if (this.azWin === null) this.azWin = Math.max(2, Math.round(num(s.azWin, 10)));
        this._azBuf = [];
      }
      this._period = 1000 / Math.max(num(this.series.rate, 10), 1);
      // 联调用的起播偏移：直接跳到某个动作段，省得等 3 分钟
      var from = Math.max(0, Math.round(num(this.from, 0)));
      if (from > 0 && from < this.series.frames) {
        this._idx = from;
        this._clock = from * this._period;
      }
      this.connected = true;
      this.frameRate = num(this.series.rate, 0) * this.speed;
      this.latency = 0;
      this.statusText = '回放中 · ' + pick + ' · ' + this.series.frames + '帧 @'
        + num(this.series.rate, 0).toFixed(2) + 'Hz · ' + this.mode;
      this.hint = this.statusText;
      this._publish(true);
    }

    /** 滑动窗 + MAD 稳健 z 分数（跳/蹲的占位判定层，不依赖角度） */
    _zscore(energy) {
      var win = this._win;
      win.push(energy);
      if (win.length > this.windowSize) win.shift();

      var med = 0, z = 0;
      if (win.length >= 8) {
        var sorted = win.slice().sort(function (a, b) { return a - b; });
        med = sorted[sorted.length >> 1];
        var dev = sorted.map(function (v) { return Math.abs(v - med); })
                        .sort(function (a, b) { return a - b; });
        var mad = dev[dev.length >> 1] * 1.4826;
        if (mad < med * 0.01) mad = med * 0.01;      // 静息时 MAD 会趋近 0
        z = (energy - med) / Math.max(mad, 1e-6);
      }
      if (z > this.stats.zmax) this.stats.zmax = z;
      return z;
    }

    /**
     * 方位 → 横向位置。
     * 只用「幅度足够高」的帧采信相位差：静息时幅度也会偶尔冲高（p99≈3200），
     * 所以还要求滑动窗内高幅帧的数量达标，再取它们相位差的中位数。
     * @returns {number} -1 = 左, +1 = 右, 0 = 不判定
     */
    _azimuthX(energy, i) {
      var buf = this._azBuf;
      buf.push({ e: energy, a: num(this.series.az[i], NaN) });
      while (buf.length > this.azWin) buf.shift();

      var hi = [];
      for (var b = 0; b < buf.length; b++) {
        if (buf[b].e > this.ampGate && isFinite(buf[b].a)) hi.push(buf[b].a);
      }
      if (hi.length < this.azNeed) { this.lastAz = NaN; return 0; }

      hi.sort(function (p, q) { return p - q; });
      var azMed = hi[hi.length >> 1];
      this.lastAz = azMed;
      if (azMed <= this.azLeft) return -1;
      if (azMed >= this.azRight) return 1;
      return 0;
    }

    /** z 分数 / 方位 → 判定层 */
    _decide(energy, i) {
      // 真值模式：直接回放采集时的动作真值（不是识别结果，是上限对照）
      var tr = this.series && this.series.truth;
      if (this.mode === 'truth' && tr) {
        return {
          x: num(tr.x && tr.x[i], 0),
          height: 0,
          jump: tr.jump && tr.jump[i] ? 1 : 0,
          crouch: tr.crouch && tr.crouch[i] ? 1 : 0
        };
      }

      var z = this._zscore(energy);

      // 方位模式：换道由相位差驱动；跳/蹲仍只有 z 分数占位（本批数据无俯仰维）
      if (this.mode === 'azimuth' && this.series.az) {
        // 门控必须用与 az 同源的能量尺度（energy_bin），不是整体的 absmean
        var gateE = this.series.energy_bin ? num(this.series.energy_bin[i], energy) : energy;
        return {
          x: this._azimuthX(gateE, i),
          height: 0,
          jump: z >= this.k ? 1 : 0,
          crouch: z <= -this.k ? 1 : 0
        };
      }

      return {
        x: 0,                                        // 无角度维：不猜左右
        height: 0,
        jump: z >= this.k ? 1 : 0,
        crouch: z <= -this.k ? 1 : 0
      };
    }

    update(dt) {
      if (!this.series) return;

      var n = this.series.frames;
      this._clock += dt * 1000 * this.speed;

      while (this._idx < n && this._idx * this._period <= this._clock) {
        var d = this._decide(num(this.series.energy[this._idx], 0), this._idx);
        var frame = {
          ts: Date.now(),
          x: d.x,
          height: d.height,
          jump: d.jump,
          crouch: d.crouch,
          confidence: 0.9,
          raw: { replay: this.seriesName, i: this._idx, e: num(this.series.energy[this._idx], 0) }
        };

        this.lastRaw = frame;
        var events = this.decoder.push(frame, performance.now());
        this.lastLane = this.decoder.lane;
        for (var i = 0; i < events.length; i++) {
          if (events[i] === 'jump') this.stats.jump++;
          else if (events[i] === 'slide') this.stats.slide++;
          else this.stats.lane++;
          this.emit(events[i]);
        }
        this.stats.frames++;
        this._idx++;
      }

      // 注意：回放是一帧 update 里连续补多帧，decoder 量到的「帧间隔」会接近 0，
      // 所以帧率要按数据本身的速率算，不能用 decoder.averageInterval。
      this.frameRate = num(this.series.rate, 0) * this.speed;
      this.stats.rate = this.frameRate;
      this.latency = 0;

      if (this._idx >= n && this.stats.end === 0) {
        if (this.loop) {
          this._idx = 0;
          this._clock = 0;
        } else {
          this.stats.end = 1;
          this.statusText = '回放结束';
        }
      }
      this._publish();
    }

    getStatusText() { return this.statusText; }

    /** 把统计写进一个隐藏节点：无头 Chrome --dump-dom 时可直接抓 */
    _publish(force) {
      if (!document.body) return;
      // 每帧都写会把这块浮层变成每帧一次布局 + 一次 JSON.stringify，
      // 长回放（2969 帧）下既费性能又闪。节流到 5Hz，关键时机用 force 立即刷。
      var t = (global.performance && global.performance.now) ? global.performance.now() : Date.now();
      if (!force && this._pubAt && t - this._pubAt < 200) return;
      this._pubAt = t;
      if (!this._el) {
        this._el = document.createElement('div');
        this._el.id = 'replay-stat';
        // 回放联调时直接显示在屏幕上：既能肉眼确认通路，也方便无头截图取证
        this._el.style.cssText =
          'position:fixed;left:12px;top:92px;z-index:60;pointer-events:none;'
          + 'font:12px/1.5 ui-monospace,Consolas,monospace;color:#f4ecd8;'
          + 'background:rgba(20,14,10,.62);border:1px solid rgba(244,236,216,.22);'
          + 'border-radius:8px;padding:6px 9px;max-width:46vw;white-space:pre-wrap;';
        document.body.appendChild(this._el);
      }
      this._el.textContent = JSON.stringify({
        series: this.seriesName, mode: this.mode, k: this.k, speed: this.speed,
        az: isFinite(this.lastAz) ? Number(this.lastAz.toFixed(3)) : null,
        azCfg: this.mode === 'azimuth'
          ? { left: this.azLeft, right: this.azRight, gate: this.ampGate, need: this.azNeed, win: this.azWin }
          : undefined,
        status: this.statusText, stats: this.stats
      });
    }
  }

  // ==================== 输入管理器 ====================

  class InputManager {
    constructor(options) {
      options = options || {};
      this.onEvent = options.onEvent || function () {};
      this.canvas = options.canvas;
      this.getThreats = options.getThreats || function () { return []; };
      // 这里**不要**快照 R.defaultUrl：main.js 是先 new RR.Game()（本类随之构造）、
      // 之后才解析 ?radar= 参数和联调台记下的地址。快照会导致雷达源永远连 config 里的默认地址，
      // ?radar=/radar-connect.html 设的地址根本不生效。改成 setSource 时再取当下的值。
      this.radarUrl = options.radarUrl || null;
      this.source = null;
    }

    setSource(id) {
      if (this.source) {
        this.source.detach();
        this.source = null;
      }

      var src;
      if (id === 'touch') src = new TouchInputSource(this.canvas);
      else if (id === 'radar') {
        // 后台判定模式（方案 B）：判定全在两个 bridge 进程里，游戏只收 action 结果
        if (R.judge === 'backend') {
          src = new RadarActionInputSource(this.radarUrl || R.actionUrl || R.defaultUrl);
        }
        // 双雷达：config 里 dual 打开且给了上下雷达地址才启用；否则退回原来的单雷达
        else if (R.dual && R.udUrl) {
          src = new DualRadarInputSource(this.radarUrl || R.lrUrl || R.defaultUrl, R.udUrl);
        } else {
          src = new RadarInputSource(this.radarUrl || R.lrUrl || R.defaultUrl);
        }
      }
      else if (id === 'mock') src = new MockRadarInputSource(this.getThreats);
      else if (id === 'replay') {
        src = new ReplayRadarInputSource({
          url: R.replayUrl, series: R.replaySeries, k: R.replayK,
          speed: R.replaySpeed, loop: R.replayLoop, window: R.replayWindow,
          mode: R.replayMode, from: R.replayFrom,
          azLeft: R.replayAzLeft, azRight: R.replayAzRight, ampGate: R.replayAmpGate,
          azNeed: R.replayAzNeed, azWin: R.replayAzWin
        });
      }
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
  RR.RadarChannel = RadarChannel;
  RR.DualRadarInputSource = DualRadarInputSource;
  RR.RadarActionInputSource = RadarActionInputSource;
  RR.MockRadarInputSource = MockRadarInputSource;
  RR.ReplayRadarInputSource = ReplayRadarInputSource;
  RR.InputManager = InputManager;

})(window);
