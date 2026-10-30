/**
 * 雷达开局校准（"新手教程"）：现场测出方向符号 + 静息基线，再修正所有帧的 x。
 *
 * 为什么要现场校准（数据结论，见 docs/radar-calibration-plan.md）：
 *   组员采的 lr-move 数据集里，用 px_delta 符号判左右只有 53.3% 准确率
 *   （右移 15 次有 12 次被判成向左），而且人静息时 px_m 基线已经在 +0.53~+0.97 m。
 *   符号和基线随站位/被试/安装角度变化 —— 所以任何人都不能替玩家定，只能开局现场测。
 *
 * ============ v2：上游跳/蹲信号不可信时的处置（数据驱动） ============
 *   新数据集（DEV113P，跳/蹲 = 上下）的实测结论（.deepworks/tmp/gtd/_work/）：
 *     - 帧率只有 1.92 Hz（帧周期 0.521 s），jump 标注帧中位数仅 2 帧、crouch 4 帧；
 *     - 该数据里名为主角的 dev1 是白噪声：静息段 lag-1 自相关 -0.03/-0.12/-0.13，
 *       std 102~108 度，均值还随 take 漂移；
 *     - 用 QC 自己的判据（jump: min(dev1) < -18；crouch: count(dev1 >= +12) >= 2）在
 *       等长 5 帧窗上做混淆矩阵：跳跃规则 TPR 87.2% 而静息 FPR 89.3%（全部负样本 90.9%）；
 *       下蹲规则 TPR 87.2% 而 FPR 85.0%。跳规则有 79.5% 打在蹲窗上。
 *     => 上游那套阈值基本等于抛硬币，游戏侧 jumpThreshold/crouchThreshold 原来是 0.5 占位值，
 *        一旦照抄这类判据，玩家站着不动也会不停起跳/下蹲。
 *
 *   所以 v2 不做"更强的识别"（数据不支持，见结论），而是做三件事：
 *     1) 质量探针：第 1~3 步（玩家本不该跳/蹲）顺便统计上游跳/蹲**误报**次数与占比；
 *        超过 probeSpuriousMax / probeRateMax 就判定"上游不可信"。
 *     2) 有界自适应：可信时才把阈值学成 clamp(玩家自身峰值 x learnMargin, thFloor, thCeil)
 *        —— 只放松、不放宽到噪声里；峰值低于 weakPeak 直接判"信号太弱"而不放宽。
 *     3) 拒绝 + 回退：不可信一律写 disableTh(=2，字段 0~1 不可达) 彻底停用雷达跳/蹲，
 *        让玩家用键盘，而不是让游戏随机乱跳。
 *
 * 零侵入设计：
 *   RadarInputSource._handleFrame(frame) 是实例方法，它把 frame 交给 decoder.push()。
 *   本文件给「实例」打一层装饰器，在进解码器之前改写 x：
 *       x' = (x - x0) * sign
 *   因此 js/InputSource.js 一行都不用改（对方会话正在改那个文件，避免冲突）。
 *
 * 对外接口：
 *   RR.Calibration.load() / save(cal) / clear() / isUsable(cal)
 *   RR.Calibration.install(src, cal) / setEnabled(src, on) / isInstalled(src)
 *   RR.Calibration.applyThresholds(cal)          // 把学到的 jump/crouch 阈值写回 CFG.RADAR
 *   var wz = new RR.CalibrationWizard({ onDone: fn, onClose: fn });
 *   wz.open(inputSource); wz.update(dt); wz.close();
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};

  var KEY = 'bear-run:calib:v1';   // key 不变；v1 存档会被 isUsable 判为过期 -> 重新引导
  var VERSION = 2;

  function num(v, dflt) {
    var n = Number(v);
    return isFinite(n) ? n : (dflt === undefined ? 0 : dflt);
  }

  function clamp(v, lo, hi) {
    v = num(v, lo);
    return v < lo ? lo : (v > hi ? hi : v);
  }

  function median(arr) {
    if (!arr.length) return 0;
    var a = arr.slice().sort(function (p, q) { return p - q; });
    var m = a.length >> 1;
    return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
  }

  function stddev(arr) {
    if (arr.length < 2) return 0;
    var i, sum = 0;
    for (i = 0; i < arr.length; i++) sum += arr[i];
    var mean = sum / arr.length, acc = 0;
    for (i = 0; i < arr.length; i++) { var d = arr[i] - mean; acc += d * d; }
    return Math.sqrt(acc / (arr.length - 1));
  }

  /** 校准默认参数（可被 RR.CFG.RADAR.calib 覆盖） */
  var DEFAULTS = {
    baseFrames: 8,          // 第 1 步：连续多少帧算"站好了"
    baseStdMax: 0.18,       // 第 1 步：这 8 帧 x 的标准差上限
    moveDelta: 0.35,        // 第 2/3 步：相对基线的位移阈值（贴着游戏进道阈值 0.36）
    holdFrames: 3,          // 第 2/3 步：位移需要保持几帧（@7.75Hz ≈ 390ms）
    minSpread: 0.5,         // 左右位移差小于它 => 校准不可靠
    stepTimeoutMs: 25000,   // 单步超时
    maxAttempts: 2,         // 同一关键步连续失败几次后允许跳过
    staleMs: 500,           // 沿用 config.RADAR.staleMs 的掉线判据

    // ---- v2：上游跳/蹲质量探针（第 1~3 步静站期间统计误报） ----
    probeFrames: 24,        // 探针参考帧数（远证据；不足 8 帧不下结论）
    probeSpuriousMax: 2,    // 误报**事件**数上限（静站时上游跳/蹲各允许 <= 2 次）
    probeRateMax: 0.06,     // 或误报帧占比上限 6%
    // ---- v2：有界自适应阈值 ----
    weakPeak: 0.40,         // 玩家自身峰值低于它 => 信号太弱，判不可用（不放宽）
    attemptLevel: 0.30,     // 原始值冲破它 = "玩家确实试了一下"（用于给出"太弱"提示，只认上升）
    learnMargin: 0.6,       // 学到的阈值 = 峰值 × margin
    thFloor: 0.30,          // 自适应下限（再低就是在噪声里触发）
    thCeil: 0.50,           // 自适应上限（config 现值，属占位值）
    disableTh: 2            // 不可信时写入的阈值（jump/crouch 字段 0~1，>=1 不可达 => 彻底停用）
  };

  function opts() {
    var base = DEFAULTS, o = RR.CFG && RR.CFG.RADAR && RR.CFG.RADAR.calib;
    var out = {}, k;
    for (k in base) out[k] = base[k];
    if (o) for (k in o) if (o[k] !== undefined) out[k] = o[k];
    return out;
  }

  // ==================== 校准结果的存取 ====================

  var _thOrig = null;   // config 里 jump/crouch 阈值的原值（第一次写回前记下）

  var Calibration = {
    KEY: KEY,
    VERSION: VERSION,

    load: function () {
      try {
        var raw = global.localStorage && global.localStorage.getItem(KEY);
        if (!raw) return null;
        var cal = JSON.parse(raw);
        return Calibration.isUsable(cal) ? cal : null;
      } catch (e) {
        return null;
      }
    },

    save: function (cal) {
      try {
        if (global.localStorage) global.localStorage.setItem(KEY, JSON.stringify(cal));
      } catch (e) { /* 隐私模式等：忽略，本次会话内存里仍有 */ }
      return cal;
    },

    clear: function () {
      try {
        if (global.localStorage) global.localStorage.removeItem(KEY);
      } catch (e) { /* 忽略 */ }
    },

    isUsable: function (cal) {
      if (!cal || cal.v !== VERSION) return false;
      var lrOk = (cal.lrOk === true) && isFinite(Number(cal.x0)) &&
        (cal.sign === 1 || cal.sign === -1);
      return !!(lrOk || cal.jumpOk === true || cal.crouchOk === true ||
        cal.keyboardOnly === true || cal.upVerdict === 'noisy');
    },

    /**
     * 把学到的跳/蹲阈值写回 CFG.RADAR（InputSource.js 里 R === CFG.RADAR，所以是热的）。
     *   jumpOk  -> clamp(峰值 × learnMargin, thFloor, thCeil)
     *   不可信  -> disableTh（>=1，字段 0~1 不可达 => 雷达跳/蹲彻底停用，回退键盘）
     * 原值存在模块变量 _thOrig 里，不写进存档对象。
     */
    applyThresholds: function (cal) {
      var cfg = RR.CFG && RR.CFG.RADAR;
      if (!cfg || !cal) return false;
      if (_thOrig === null) {
        _thOrig = { jump: num(cfg.jumpThreshold, 0.5), crouch: num(cfg.crouchThreshold, 0.5) };
      }
      if (isFinite(Number(cal.jumpTh))) cfg.jumpThreshold = num(cal.jumpTh, _thOrig.jump);
      if (isFinite(Number(cal.crouchTh))) cfg.crouchThreshold = num(cal.crouchTh, _thOrig.crouch);
      return true;
    },

    /** 恢复 config 里的跳/蹲阈值原值（调试/重置用） */
    restoreThresholds: function () {
      var cfg = RR.CFG && RR.CFG.RADAR;
      if (!cfg || !_thOrig) return false;
      cfg.jumpThreshold = _thOrig.jump;
      cfg.crouchThreshold = _thOrig.crouch;
      return true;
    },

    savedThresholds: function () { return _thOrig ? { jump: _thOrig.jump, crouch: _thOrig.crouch } : null; },

    /** 把校准应用到某个输入源实例（在进解码器之前改写 x） */
    install: function (src, cal) {
      if (!src || typeof src._handleFrame !== 'function') return false;
      if (!src.__calInstalled) {
        var orig = src._handleFrame;
        src._handleFrame = function (frame) {
          var c = this.__calib;
          if (c && c.enabled && frame && typeof frame === 'object') {
            var rawX = num(frame.x, 0);
            var out = {}, k;
            for (k in frame) out[k] = frame[k];
            var g = num(c.gain, 1);
            out.x = (rawX - c.x0) * c.sign * (g > 0 ? g : 1);
            // 保留原始值，便于调试面板/log 查看"雷达原始读数"
            out.rawX = rawX;
            out.calibrated = true;
            frame = out;
          }
          return orig.call(this, frame);
        };
        src.__calInstalled = true;
      }
      src.__calib = src.__calib || { enabled: false, x0: 0, sign: 1 };
      Calibration.apply(src, cal);
      Calibration.applyThresholds(cal);
      return true;
    },

    /** 更新已安装的校准参数（cal 为 null 时停用） */
    apply: function (src, cal) {
      if (!src || !src.__calInstalled) return false;
      if (!cal) {
        src.__calib.enabled = false;
        return true;
      }
      src.__calib.x0 = num(cal.x0, 0);
      src.__calib.sign = cal.sign === -1 ? -1 : 1;
      // v3：横向增益（一步 = 一格）。旧存档没有 xGain，按 1 处理（不改变原来手感）。
      src.__calib.gain = num(cal.xGain, 1) || 1;
      // v2：左右没标定成功（lrOk === false）就不改写 x，避免用错的符号把玩家甩反
      src.__calib.enabled = (cal.lrOk === false) ? false : true;
      // 解码器读的是 CFG.RADAR（热引用），把增益同步过去，改完立即生效。
      // xGainLock = 本次页面用 ?gain= 手动指定过 —— 此时 URL 优先，不让存档覆盖，
      // 否则现场刚调好的手感一刷新就被旧存档打回去。
      var cfg = RR.CFG && RR.CFG.RADAR;
      if (cfg && cfg.xGainLock) {
        src.__calib.gain = num(cfg.xGain, 1) || 1;
      } else if (cfg && isFinite(Number(cal.xGain))) {
        cfg.xGain = num(cal.xGain, 1);
      }
      return true;
    },

    setEnabled: function (src, on) {
      if (!src || !src.__calib) return false;
      src.__calib.enabled = !!on;
      return true;
    },

    isInstalled: function (src) {
      return !!(src && src.__calInstalled);
    },

    /** 一句话描述校准结果，用于菜单/调试面板 */
    describe: function (cal) {
      if (!cal) return '未校准';
      var parts = [];
      if (cal.keyboardOnly) parts.push('键盘模式(未用雷达)');
      else if (cal.lrOk) {
        parts.push('基线 ' + num(cal.x0, 0).toFixed(2));
        parts.push('方向 ' + (cal.sign === 1 ? '正向' : '反向'));
        if (isFinite(Number(cal.xGain)) && Number(cal.xGain) !== 1) {
          parts.push('增益 ' + num(cal.xGain, 1).toFixed(2));
        }
      } else parts.push('左右未标定(键盘)');
      if (!cal.keyboardOnly) {
        parts.push(cal.jumpOk ? ('跳跃 ' + num(cal.jumpTh, 0).toFixed(2)) : '跳跃(键盘)');
        parts.push(cal.crouchOk ? ('下蹲 ' + num(cal.crouchTh, 0).toFixed(2)) : '下蹲(键盘)');
      }
      if (cal.upVerdict === 'noisy') parts.push('上游跳蹲不可信');
      if (cal.probe && cal.probe.frames) {
        parts.push('静站误报 ' + num(cal.probe.jumpEvents, 0) + '跳/' +
          num(cal.probe.crouchEvents, 0) + '蹲@' + num(cal.probe.frames, 0) + '帧');
      }
      return parts.join(' · ');
    }
  };

  // ==================== 校准向导（5 步状态机 + 自建覆盖层） ====================

  var STEPS = [
    {
      id: 'ready',
      title: '第 1 步 / 5 · 站好别动',
      text: '站到雷达正前方约 1.5 米，正对雷达，保持不动。',
      tip: '正在确认你在场…'
    },
    {
      id: 'left',
      title: '第 2 步 / 5 · 向左移一步',
      text: '向左平移约一步（半米左右），然后停住 1 秒，再回中间。',
      tip: '等待向左的有效位移…'
    },
    {
      id: 'right',
      title: '第 3 步 / 5 · 向右移一步',
      text: '向右平移约一步，停住 1 秒，再回中间。',
      tip: '等待向右的有效位移…'
    },
    {
      id: 'jump',
      title: '第 4 步 / 5 · 原地跳一下',
      text: '原地起跳一次，落地后站稳。',
      tip: '等待跳跃信号…'
    },
    {
      id: 'crouch',
      title: '第 5 步 / 5 · 蹲一下',
      text: '下蹲保持 1 秒，然后起立。',
      tip: '等待下蹲信号…'
    }
  ];

  var KEY_LEFT = { ArrowLeft: 1, a: 1, A: 1 };
  var KEY_RIGHT = { ArrowRight: 1, d: 1, D: 1 };
  var KEY_JUMP = { ' ': 1, w: 1, W: 1, ArrowUp: 1 };
  var KEY_CROUCH = { ArrowDown: 1, s: 1, S: 1 };

  class CalibrationWizard {
    constructor(config) {
      config = config || {};
      this.onDone = config.onDone || function () { };
      this.onClose = config.onClose || function () { };
      // 面板里改完雷达地址后，交给宿主去重建数据源（见 main.js 的装配）
      this.onReconnect = config.onReconnect || null;
      this.container = config.container || global.document && global.document.body;

      this.src = null;
      this.opts = opts();
      this.open_ = false;
      this.stepIndex = 0;
      this.attempts = 0;
      this.result = null;
      this.message = '';
      this.messageKind = '';       // '' | 'warn' | 'error' | 'ok'

      this.baseBuf = [];           // 第 1 步的采样缓冲
      this.holdBuf = [];           // 第 2/3 步的位移保持缓冲
      this.x0 = null;
      this.sideA = null;
      this.sideB = null;
      this.jumpOk = false;
      this.crouchOk = false;
      this.usedKeyboard = false;
      this.radarSeen = false;      // 是否收到过真实雷达帧
      this.stepStartedAt = 0;
      this.lastFrameRef = null;
      this.lastFrameAt = 0;
      this.now = 0;

      // ---- v2：上游跳/蹲质量探针 ----
      this.probe = { frames: 0, jumpEvents: 0, crouchEvents: 0, spuriousFrames: 0 };
      this._prevUpJump = false;
      this._prevUpCrouch = false;
      this._upJumpEdges = 0;       // 上游 jump 上升沿计数（只涨不落）
      this._upCrouchEdges = 0;
      this._jumpEdge0 = 0;         // 进入第 4/5 步时的沿计数基线
      this._crouchEdge0 = 0;
      this.jumpPeak = 0;           // 第 4 步玩家自身峰值 max(jump, height)
      this.crouchPeak = 0;         // 第 5 步玩家自身峰值 crouch
      this.upVerdict = null;       // 'clean' | 'noisy' | 'unknown'（结束时定格）
      this._jcTested = false;      // 是否真的走到过第 4 步（上下测过）
      // v2：原始值（未经解码阈值门限）抬头 => "玩家确实试了一下"，用来给"太弱"提示
      this._prevRawJump = 0;
      this._prevRawCrouch = 0;
      this._rawJumpRose = false;
      this._rawCrouchRose = false;

      this.keys = { left: false, right: false, jump: false, crouch: false };
      this._onKeyDown = this._onKeyDown.bind(this);
      this._onKeyUp = this._onKeyUp.bind(this);

      this.el = null;
      this._build();
    }

    get isOpen() { return this.open_; }

    get step() { return STEPS[this.stepIndex]; }

    /**
     * 校准期间把横向增益临时归 1。
     *
     * 为什么：第 2/3 步量的是"玩家走一步 = 多少个归一化单位"。如果上一次存档里的
     * xGain 还在生效（比如 0.4），同一段位移会被压成 0.4 倍，量出来的 d 偏小，
     * 新增益就会算错。所以测的时候必须按原始量程来，测完再把学到的增益接回去。
     */
    _setLiveGain(g) {
      var cfg = RR.CFG && RR.CFG.RADAR;
      // 用 ?gain= 手动指定过：本次页面以 URL 为准，向导也不许改
      if (cfg && cfg.xGainLock) g = num(cfg.xGain, 1) || 1;
      if (cfg) cfg.xGain = g;
      var s = this.src;
      if (s && s.__calib) s.__calib.gain = g;
    }

    // ---------- 打开 / 关闭 ----------

    open(src) {
      if (this.open_) return;
      this.open_ = true;
      this.src = src || null;
      // 记住进向导前的增益：中途关掉（没测完）要还原，不能把玩家的手感弄丢
      this.preGain = num((RR.CFG && RR.CFG.RADAR && RR.CFG.RADAR.xGain), 1) || 1;
      this._setLiveGain(1);
      this.stepIndex = 0;
      this.attempts = 0;
      this.result = null;
      this.x0 = null;
      this.sideA = null;          // 第 2 步测到的原始 x（玩家在左侧）
      this.sideB = null;          // 第 3 步测到的原始 x（玩家在右侧）
      this.sideKeyboard = false;  // 左右步是否被键盘兜底走过（则极性无法标定）
      var prior = Calibration.load();
      this.priorSign = prior ? prior.sign : null;
      this.jumpOk = this.crouchOk = false;
      this.usedKeyboard = false;
      this.radarSeen = false;
      this.baseBuf = [];
      this.holdBuf = [];
      // v2：探针/峰值清零
      this.probe = { frames: 0, jumpEvents: 0, crouchEvents: 0, spuriousFrames: 0 };
      this._prevUpJump = false;
      this._prevUpCrouch = false;
      this._upJumpEdges = 0;
      this._upCrouchEdges = 0;
      this._jumpEdge0 = 0;
      this._crouchEdge0 = 0;
      this.jumpPeak = 0;
      this.crouchPeak = 0;
      this.upVerdict = null;
      this._jcTested = false;
      this._prevRawJump = 0;
      this._prevRawCrouch = 0;
      this._rawJumpRose = false;
      this._rawCrouchRose = false;
      this.message = '';
      this.messageKind = '';
      this._syncLinkInputs();
      this._enterStep();
      this.el.classList.remove('hidden');
      this._syncUI();
      global.addEventListener('keydown', this._onKeyDown);
      global.addEventListener('keyup', this._onKeyUp);
    }

    close() {
      if (!this.open_) return;
      this.open_ = false;
      global.removeEventListener('keydown', this._onKeyDown);
      global.removeEventListener('keyup', this._onKeyUp);
      this.el.classList.add('hidden');
      // 中途关掉（没产出结果）：把进向导前的增益还原，别让玩家莫名变钝/变灵敏
      if (!this.result) this._setLiveGain(this.preGain);
      this.src = null;
      this.onClose();
    }

    /** 每帧调用（dt 秒） */
    update(dt) {
      if (!this.open_) return;
      this.now += num(dt, 0) * 1000;
      if (!this.stepStartedAt) this.stepStartedAt = this.now;

      var frame = this._sample();

      switch (this.step.id) {
        case 'ready': this._stepReady(frame); break;
        case 'left': this._stepSide(frame, 0); break;
        case 'right': this._stepSide(frame, 1); break;
        case 'jump': this._stepJump(frame); break;
        case 'crouch': this._stepCrouch(frame); break;
      }

      this._checkTimeout();
      this._syncUI();
    }

    // ---------- 采样 ----------

    /** 取当前帧（新鲜度自检）；顺带记录"是否见过雷达帧" */
    _sample() {
      var src = this.src;
      if (!src) return null;
      var f = src.lastRaw;
      if (!f) return null;
      if (f !== this.lastFrameRef) {
        this.lastFrameRef = f;
        this.lastFrameAt = this.now;
        if (src.id === 'radar' || src.id === 'replay') this.radarSeen = true;
      }
      var age = this.now - this.lastFrameAt;
      if (this.lastFrameAt && age > this.opts.staleMs * 3) return null;   // 数据太旧
      return f;
    }

    get radarOffline() {
      if (!this.src) return true;
      if (this.src.id !== 'radar' && this.src.id !== 'replay') return false;
      if (!this.radarSeen) return true;
      return !!(this.src.connected === false);
    }

    _validX(frame) {
      if (!frame) return null;
      var conf = frame.confidence === undefined ? 1 : num(frame.confidence, 1);
      var minConf = (RR.CFG && RR.CFG.RADAR && RR.CFG.RADAR.minConfidence) || 0.3;
      if (conf < minConf) return null;
      var x = num(frame.x, null);
      if (x === null) return null;
      return x;
    }

    /**
     * v2 探针结论：'noisy' | 'clean' | 'unknown'
     *
     * 依据（.deepworks/tmp/gtd/_work/final_eval.txt）：上游那套判据在静息时的 FPR
     * 高达 85~91%，而一个能用的设备在玩家站着不动的十来秒里应该几乎不误报。
     * 所以「玩家本不该跳/蹲的第 1~3 步」就是最直接的判据：误报事件/占比超限 => 不可信。
     * 证据不足（< 8 帧）不下结论，避免掉线时误判。
     */
    get probeVerdict() {
      var o = this.opts, p = this.probe;
      if (p.frames < 8) return 'unknown';
      if (p.jumpEvents > o.probeSpuriousMax || p.crouchEvents > o.probeSpuriousMax) return 'noisy';
      if (p.frames > 0 && (p.spuriousFrames / p.frames) > o.probeRateMax) return 'noisy';
      return 'clean';
    }

    // ---------- 各步逻辑 ----------

    _stepReady(frame) {
      if (this.keys.jump && this.radarOffline) return this._finishKeyboard();
      if (!this.radarSeen && this.keys.left) return this._finishKeyboard();

      var x = this._validX(frame);
      if (x === null) {
        if (this.now - (this._lastValidAt || 0) > 1200) this.baseBuf = [];
        this.message = this.radarOffline
          ? '没收到雷达数据…可以按 ← → 空格 ↓ 用键盘完成。'
          : '暂时没看到你，站到雷达正前方试试。';
        this.messageKind = 'warn';
        return;
      }
      this._lastValidAt = this.now;
      this.baseBuf.push(x);
      if (this.baseBuf.length > 40) this.baseBuf.shift();

      var n = this.opts.baseFrames;
      if (this.baseBuf.length >= n) {
        var win = this.baseBuf.slice(-n);
        if (stddev(win) <= this.opts.baseStdMax) {
          this.x0 = median(win);
          this.message = '已定位，基线 x₀ = ' + this.x0.toFixed(2);
          this.messageKind = 'ok';
          this._nextStep();
          return;
        }
        this.message = '你在动，先站定 1 秒。';
        this.messageKind = '';
      } else {
        this.message = '正在确认你在场…（' + this.baseBuf.length + '/' + n + '）';
        this.messageKind = '';
      }
    }

    /**
     * 第 2/3 步：等一个相对基线够大的位移，并保持 holdFrames 帧。
     *
     * 关键：**不假设雷达极性**。数据结论（docs/radar-calibration-plan.md）里
     * 右移 15 次有 12 次的 px_delta 是负的 —— "往右走 x 一定变大"这个前提本身
     * 就是错的，所以这里只要求：
     *   which=0（第 2 步）：离基线足够远，落在哪一侧都算 -> sideA
     *   which=1（第 3 步）：离基线足够远，且落在与 sideA 相反的一侧 -> sideB
     *   sign = (sideB - sideA) > 0 ? +1 : -1
     * 这样无论雷达装正还是装反，校准后"玩家往右走 => x' 变大"都成立。
     */
    _stepSide(frame, which) {
      var label = which === 0 ? '左' : '右';
      var keyed = which === 0 ? this.keys.left : this.keys.right;

      // 键盘兜底：键盘无法告诉我们雷达极性（退回上次校准的极性）
      if (keyed) {
        this.usedKeyboard = true;
        this.sideKeyboard = true;
        this.message = '用键盘走了这步 —— 雷达极性没法标定了。';
        this.messageKind = 'warn';
        this._nextStep();
        return;
      }

      var x = this._validX(frame);
      if (x === null) {
        this.message = this.radarOffline
          ? '雷达没数据（按 ' + (which === 0 ? '←' : '→') + ' 可用键盘代替）'
          : '没看到你，站回雷达前面。';
        this.messageKind = this.radarOffline ? 'warn' : '';
        return;
      }

      var dx = x - this.x0;
      var far = Math.abs(dx) >= this.opts.moveDelta;
      var sameSide = (which === 1 && this.sideA !== null) &&
        ((dx > 0) === ((this.sideA - this.x0) > 0));
      var ok = far && !sameSide;

      if (ok) {
        this.holdBuf.push(x);
        this.message = '检测到' + label + '移 ' + x.toFixed(2) +
          '（保持 ' + this.holdBuf.length + '/' + this.opts.holdFrames + '）';
        this.messageKind = 'ok';
        if (this.holdBuf.length >= this.opts.holdFrames) {
          var val = median(this.holdBuf);
          if (which === 0) this.sideA = val; else this.sideB = val;
          this.holdBuf = [];
          this._nextStep();
        }
        return;
      }

      this.holdBuf = [];
      if (sameSide) {
        this.message = '你现在还在同一侧，走到另一边去。';
      } else {
        this.message = '当前 x = ' + x.toFixed(2) + '，要离基线 ' +
          this.x0.toFixed(2) + ' 再远 ' + this.opts.moveDelta.toFixed(2) + '（幅度大一点）';
      }
      this.messageKind = '';
    }

    /**
     * 第 4 步（跳）。
     *
     * 与 v1 的区别：
     *   1) 先看探针结论：静站已经误报一片 => 直接判不可信，不再等 25 秒（省时间也更诚实）。
     *   2) 只认**上升沿**（沿计数超过进入本步时的基线），不再认"电平一直高" —— 否则上游卡在
     *      高电平就能白拿走这一步。
     *   3) 记下玩家自身峰值 jumpPeak；峰值 < weakPeak 判"信号太弱"，不放宽阈值硬凑。
     */
    _stepJump(frame) {
      var o = this.opts;
      var peak = Math.max(num(frame && frame.jump, 0), num(frame && frame.height, 0));
      if (peak > this.jumpPeak) this.jumpPeak = peak;

      if (this.keys.jump && !this.jumpOk) {
        this.jumpOk = false;
        this.usedKeyboard = true;
        this.mode_ = 'keyboard-jump';
        this.message = '用键盘记录了跳跃（雷达不给跳跃信号）。';
        this.messageKind = 'warn';
        this._nextStep();
        return;
      }
      // 双雷达：上下通道没连上时直接说明，别让玩家对着"跳一下…"干等
      if (this.src && this.src.udOnline === false) {
        this.message = '上下雷达没连上（' + (this.src.udUrl || '地址未配置') +
          '），跳跃信号来不了。修好地址后再跳，或按空格用键盘完成。';
        this.messageKind = 'warn';
        return;
      }
      if (this.upVerdict === 'noisy' || this.probeVerdict === 'noisy') {
        this.jumpOk = false;
        this.crouchOk = false;
        this.upVerdict = 'noisy';
        this.message = '雷达把"站着不动"也当成跳/蹲（静站误报 ' + this.probe.jumpEvents +
          ' 跳 / ' + this.probe.crouchEvents + ' 蹲）。雷达跳跃已停用，用键盘空格/↑ 跳。';
        this.messageKind = 'error';
        this._nextStep();
        return;
      }
      var effTh = num((RR.CFG && RR.CFG.RADAR) ? RR.CFG.RADAR.jumpThreshold : null, o.thCeil);
      var need = this.jumpPeak * o.learnMargin;      // 要让玩家的峰值被认出来，阈值得放到这么低
      // "玩家确实试了"的三种证据：上游给了事件 / 新上升沿 / 原始值抬过头
      var tried = this._sawJump || this._upJumpEdges > this._jumpEdge0 || this._rawJumpRose;
      if (tried) {
        if (this.jumpPeak < o.weakPeak) {
          // 有动静但太弱：不放宽阈值硬凑
          this.jumpOk = false;
          this.message = '检测到跳跃，但幅度只有 ' + this.jumpPeak.toFixed(2) +
            '（太弱，标不准）。跳高一点再试，或按「跳过」用键盘。';
          this.messageKind = 'warn';
          this._retryStep(this.message, 'warn');
          return;
        }
        if (this.jumpPeak < effTh && need < o.thFloor) {
          // 现有阈值认不出来，而"放宽到刚好够用"又低于安全下限（该区间实测全是静站误报）
          this.jumpOk = false;
          this.message = '跳跃幅度 ' + this.jumpPeak.toFixed(2) + ' 偏弱：要把阈值降到 ' +
            need.toFixed(2) + ' 才认得出，但安全下限是 ' + o.thFloor.toFixed(2) +
            '（再低就全是静站误报）。请跳高一点，或按「跳过」用键盘。';
          this.messageKind = 'warn';
          this._retryStep(this.message, 'warn');
          return;
        }
        this.jumpOk = true;
        this.message = '跳跃信号已确认（峰值 ' + this.jumpPeak.toFixed(2) + '）';
        this.messageKind = 'ok';
        this._nextStep();
        return;
      }
      this.message = '跳一下…';
      this.messageKind = '';
    }

    /** 第 5 步（蹲）：同 _stepJump，判据换成 crouch 字段 */
    _stepCrouch(frame) {
      var o = this.opts;
      var peak = num(frame && frame.crouch, 0);
      if (peak > this.crouchPeak) this.crouchPeak = peak;

      if (this.keys.crouch) {
        this.usedKeyboard = true;
        this.crouchOk = false;
        this.message = '用键盘记录了下蹲（雷达不给下蹲信号）。';
        this.messageKind = 'warn';
        this._nextStep();
        return;
      }
      // 双雷达：上下通道没连上时直接说明
      if (this.src && this.src.udOnline === false) {
        this.message = '上下雷达没连上（' + (this.src.udUrl || '地址未配置') +
          '），下蹲信号来不了。修好地址后再蹲，或按 ↓ 用键盘完成。';
        this.messageKind = 'warn';
        return;
      }
      if (this.upVerdict === 'noisy' || this.probeVerdict === 'noisy') {
        this.upVerdict = 'noisy';
        this.crouchOk = false;
        this.message = '雷达下蹲信号不可信（静站误报太多），已停用，用键盘 ↓ 蹲。';
        this.messageKind = 'error';
        this._nextStep();
        return;
      }
      var effTh = num((RR.CFG && RR.CFG.RADAR) ? RR.CFG.RADAR.crouchThreshold : null, o.thCeil);
      var need = this.crouchPeak * o.learnMargin;
      var tried = this._sawCrouch || this._upCrouchEdges > this._crouchEdge0 || this._rawCrouchRose;
      if (tried) {
        if (this.crouchPeak < o.weakPeak) {
          this.crouchOk = false;
          this.message = '检测到下蹲，但幅度只有 ' + this.crouchPeak.toFixed(2) +
            '（太弱，标不准）。蹲低一点再试，或按「跳过」用键盘。';
          this.messageKind = 'warn';
          this._retryStep(this.message, 'warn');
          return;
        }
        if (this.crouchPeak < effTh && need < o.thFloor) {
          this.crouchOk = false;
          this.message = '下蹲幅度 ' + this.crouchPeak.toFixed(2) + ' 偏弱：要把阈值降到 ' +
            need.toFixed(2) + ' 才认得出，但安全下限是 ' + o.thFloor.toFixed(2) +
            '（再低就全是静站误报）。请蹲低一点，或按「跳过」用键盘。';
          this.messageKind = 'warn';
          this._retryStep(this.message, 'warn');
          return;
        }
        this.crouchOk = true;
        this.message = '下蹲信号已确认（峰值 ' + this.crouchPeak.toFixed(2) + '）';
        this.messageKind = 'ok';
        this._nextStep();
        return;
      }
      this.message = '蹲一下…';
      this.messageKind = '';
    }

    // ---------- 帧事件钩子（由外部把 decoder 事件喂进来） ----------

    /**
     * 把输入源解出的事件通知给向导（jump / slide / left / right）。
     * 由 main.js 在 onEvent 里转发；不转发也能靠 lastRaw 的字段判定。
     */
    notifyEvent(type) {
      if (!this.open_) return;
      if (type === 'jump') this._sawJump = true;
      else if (type === 'slide') this._sawCrouch = true;
    }

    /**
     * 每条帧进来时问一次：这一帧本身能不能算跳跃/下蹲；顺便喂 v2 质量探针。
     *
     * v2 变更：
     *   - 不再用"电平高"直接判某步通过（_sawJump/_sawCrouch 只由 notifyEvent 置位），
     *     改为统计**上升沿**计数 _upJumpEdges/_upCrouchEdges；
     *   - 第 1~3 步（玩家本不该跳/蹲）统计误报事件数与误报帧占比。
     */
    _probeFrame(frame) {
      if (!frame) return;
      // 用**出厂默认阈值**做探针判据（不能用可能已被 disableTh 停用/被上次学习改过的现值，
      // 否则"上一轮判了不可信 -> 阈值=2 -> 本轮探针永远不触发 -> 误判可信"）
      var base = Calibration.savedThresholds() || { jump: 0.5, crouch: 0.5 };
      var jt = num(base.jump, 0.5), ct = num(base.crouch, 0.5);
      // 与 InputSource.js 的解码判据保持一致（jump 用 jump||height，crouch 用 crouch）
      var j = (num(frame.jump, 0) >= jt) || (num(frame.height, 0) >= jt);
      var c = num(frame.crouch, 0) >= ct;

      if (j && !this._prevUpJump) this._upJumpEdges++;
      if (c && !this._prevUpCrouch) this._upCrouchEdges++;

      // v2：再看**未经解码阈值门限的原始值**。玩家跳了但只有 0.4x（够不着 0.5 的出厂阈值）
      // 时解码器根本不会给事件，向导只会一直显示"跳一下…"，玩家不知道自己差在哪。
      // 这里记录"原始值抬头（跨过 attemptLevel）的瞬间"，供第 4/5 步给出"太弱"提示。
      // 必须是**上升**（不是"电平高"）—— 否则上游卡在高电平同样会被当成"你试过了"。
      var rawJ = Math.max(num(frame.jump, 0), num(frame.height, 0));
      var rawC = num(frame.crouch, 0);
      var lvl = num(this.opts.attemptLevel, 0.30);
      if (rawJ >= lvl && this._prevRawJump < lvl) this._rawJumpRose = true;
      if (rawC >= lvl && this._prevRawCrouch < lvl) this._rawCrouchRose = true;

      if (this.stepIndex <= 2) {                 // 第 1~3 步：静站/走位，不该有跳/蹲
        var p = this.probe;
        p.frames++;
        if (j && !this._prevUpJump) p.jumpEvents++;
        if (c && !this._prevUpCrouch) p.crouchEvents++;
        if (j || c) p.spuriousFrames++;
      }

      this._prevUpJump = j;
      this._prevUpCrouch = c;
      this._prevRawJump = rawJ;
      this._prevRawCrouch = rawC;
    }

    // ---------- 流程控制 ----------

    _enterStep() {
      this.stepStartedAt = this.now;
      this.holdBuf = [];
      this._sawJump = false;
      this._sawCrouch = false;
      // v2：只认进入本步之后的**新**上升沿；峰值保持累计（取窗口内最大值）
      this._jumpEdge0 = this._upJumpEdges;
      this._crouchEdge0 = this._upCrouchEdges;
      this._rawJumpRose = false;
      this._rawCrouchRose = false;
      if (this.step.id === 'jump') this._jcTested = true;   // 走到第 4 步 => 上下真的测过了
      if (this.step.id === 'ready') this.baseBuf = [];
    }

    _nextStep() {
      this.attempts = 0;
      if (this.stepIndex >= STEPS.length - 1) return this._finish();
      this.stepIndex++;
      this._enterStep();
    }

    /**
     * 重试当前步。msg 非空时保留该提示（用于"太弱/方向不对"这类**有信息量**的失败原因），
     * 否则退回通用的「再来一次。」。
     *
     * 为什么要这样：v2 里"跳得太弱"要告诉玩家原因，但如果先设 message 再调 _retryStep()，
     * 通用提示会把它立刻覆盖掉 —— 玩家永远看不到"太弱"，只会看到"再来一次"，
     * 于是不知道该跳高一点（自检里就是这条被抓出来的）。
     */
    _retryStep(msg, kind) {
      this.attempts++;
      if (this.attempts >= this.opts.maxAttempts && this.step.id !== 'ready' && !msg) {
        this.message = '连续没检测到。可以再试，或按「跳过」用键盘继续。';
        this.messageKind = 'error';
        if (this.el) this.el.classList.add('calib-allow-skip');
        return;
      }
      this._enterStep();
      this.message = msg || '再来一次。';
      this.messageKind = msg ? (kind || 'warn') : '';
    }

    _checkTimeout() {
      if (this.step.id === 'ready') return;
      if (this.now - this.stepStartedAt > this.opts.stepTimeoutMs) {
        this.stepStartedAt = this.now;
        this._retryStep();
      }
    }

    _finishKeyboard() {
      this.usedKeyboard = true;
      this.result = this._composeResult(0, 1, false, false, false, false);
      // 完全没用雷达：不碰 config 阈值（jumpTh/crouchTh 留空），也不假装上下已确认
      this.result.jumpOk = false;
      this.result.crouchOk = false;
      this.result.keyboardOnly = true;
      this.result.source = 'keyboard';
      this.result.upVerdict = 'keyboard';
      Calibration.save(this.result);
      this.message = '未使用雷达，已按键盘模式完成。';
      this.messageKind = 'warn';
      this._finish();
    }

    /**
     * v2 结果装配：左右（x0/sign/lrOk）+ 上下（jumpOk/crouchOk/阈值/探针）分开记。
     *
     * 关键设计：上下和左右**互不连坐**，而且"只有拿到证据才动 config"。
     *   - 左右没标定成功 => lrOk=false => install 时不改写 x（跟 v1 的 unreliable 等价）
     *   - 探针判噪    => 上下都判不可信 + 阈值写 disableTh(2)，解码器再也解不出 jump/slide
     *                    （回退键盘），而不是让游戏随机乱跳
     *   - 试过但没测到（弱信号/键盘代替）=> 该动作单独 disable
     *   - 根本没测（第 1~3 步就跳过）    => 阈值留空，不动 config，不乱猜
     *   - 左右上下都不行 => 不保存（下次还会自动引导）
     */
    _composeResult(x0, sign, lrOk, jumpOk, crouchOk, sourceRadar) {
      var o = this.opts;
      var verdict = this.upVerdict || this.probeVerdict;
      var noisy = verdict === 'noisy';
      var tested = !!this._jcTested;

      var jOk = !noisy && !!jumpOk;
      var cOk = !noisy && !!crouchOk;
      var jPeak = num(this.jumpPeak, 0), cPeak = num(this.crouchPeak, 0);

      var jTh, cTh;
      if (noisy || (tested && !jOk)) jTh = o.disableTh;
      else if (jOk) jTh = clamp(jPeak * o.learnMargin, o.thFloor, o.thCeil);
      else jTh = undefined;

      if (noisy || (tested && !cOk)) cTh = o.disableTh;
      else if (cOk) cTh = clamp(cPeak * o.learnMargin, o.thFloor, o.thCeil);
      else cTh = undefined;

      // ---- v3：量程（增益）校准 ----
      // sideA/sideB 是玩家"向左/向右走一步"时的 x（进向导时增益已归 1，所以是原始量程）。
      // spread = sideB - sideA ≈ 两步的总跨度，除以 2 就是"一步"对应的归一化位移。
      // 目标：把这一步映射到 stepTarget(默认 0.55) —— 稳过进道阈值 0.36，又远不到满量程，
      // 于是手小幅晃不会误触发、真走一步刚好换一格。
      var stepMeasured = 0, xGain = 1;
      if (lrOk) {
        stepMeasured = Math.abs(this._spread()) / 2;
        if (stepMeasured > 0.02) {
          xGain = clamp(o.stepTarget / stepMeasured, o.gainMin, o.gainMax);
          xGain = Math.round(xGain * 100) / 100;
        }
      }

      var cal = {
        v: VERSION,
        at: Date.now(),
        x0: num(x0, 0),
        sign: sign === -1 ? -1 : 1,
        lrOk: !!lrOk,
        xGain: xGain,
        stepMeasured: Math.round(stepMeasured * 1000) / 1000,
        jumpOk: jOk,
        crouchOk: cOk,
        jumpPeak: jPeak,
        crouchPeak: cPeak,
        probe: {
          frames: this.probe.frames,
          jumpEvents: this.probe.jumpEvents,
          crouchEvents: this.probe.crouchEvents,
          spuriousFrames: this.probe.spuriousFrames,
          rate: this.probe.frames ? this.probe.spuriousFrames / this.probe.frames : 0
        },
        upVerdict: verdict,
        jcTested: tested,
        source: sourceRadar ? (this.usedKeyboard ? 'mixed' : 'radar') : 'keyboard',
        unreliable: !lrOk,
        debug: { left: this.sideA, right: this.sideB, spread: this._spread() }
      };
      if (isFinite(jTh)) cal.jumpTh = jTh;
      if (isFinite(cTh)) cal.crouchTh = cTh;
      if (this.resultDisabledAll) { cal.jumpDisabled = true; cal.crouchDisabled = true; }
      return cal;
    }

    _spread() {
      return (this.sideA !== null && this.sideB !== null) ? (this.sideB - this.sideA) : 0;
    }

    /**
     * 左右标定结论（_finish 和 skip 共用）。
     *
     * 为什么 skip 也要用真实结论：老版本 skip() 一律判"左右不可靠"，
     * 于是玩家如果左右明明走通了、只是在第 4 步跳不动而按跳过，
     * 已经标好的基线/极性会被一起丢掉 —— 白测一场。
     */
    _lrOkResult() {
      var bothSides = (this.sideA !== null && this.sideB !== null);
      var spread = this._spread();
      var lrOk = bothSides && !this.sideKeyboard &&
        isFinite(spread) && Math.abs(spread) >= this.opts.minSpread;
      var sign = bothSides ? (spread >= 0 ? 1 : -1) : ((this.priorSign === -1) ? -1 : 1);
      return { lrOk: lrOk, sign: sign, bothSides: bothSides, spread: spread };
    }

    /** 跳过剩余步骤：左右标定过的部分**保留**，没确认的上下动作交给键盘兜底 */
    skip() {
      if (!this.open_) return;
      var lr = this._lrOkResult();
      var cal = this._composeResult(this.x0 === null ? 0 : this.x0, lr.sign, lr.lrOk,
        this.jumpOk, this.crouchOk, !!this.radarSeen);
      if (!cal.upVerdict || cal.upVerdict === 'unknown') cal.upVerdict = 'skipped';
      this.result = cal;
      this.message = lr.lrOk
        ? '已跳过剩余步骤（左右方向已标定，保留；没确认的动作改用键盘）。'
        : '已跳过（左右方向未标定，使用键盘兜底）。';
      this.messageKind = 'warn';
      this._finish();
    }

    _finish() {
      if (!this.result) {
        var lr = this._lrOkResult();
        this.result = this._composeResult(this.x0 === null ? 0 : this.x0, lr.sign, lr.lrOk,
          this.jumpOk, this.crouchOk, true);
      }
      // 让学到的阈值当场生效（inputSource 与 CFG.RADAR 是同一对象）
      Calibration.applyThresholds(this.result);
      // 横向增益也当场接管（向导期间是 1，这里换成量出来的值）
      if (isFinite(Number(this.result.xGain))) this._setLiveGain(num(this.result.xGain, 1));
      // 左右和上下都没拿到证据 => 没有可保存的内容（下次还会自动引导）
      var r = this.result;
      if (r.lrOk || r.jumpOk || r.crouchOk || r.keyboardOnly || r.upVerdict === 'noisy') {
        Calibration.save(r);
      } else {
        Calibration.clear();
      }
      this.onDone(this.result);
      this.close();
    }

    // ---------- 键盘兜底 ----------

    _onKeyDown(e) {
      if (!this.open_) return;
      // 地址输入框里打字时不要抢方向键/空格（否则会被当成"用键盘校准"）
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      var k = e.key;
      if (KEY_LEFT[k]) { this.keys.left = true; e.preventDefault(); }
      else if (KEY_RIGHT[k]) { this.keys.right = true; e.preventDefault(); }
      else if (KEY_JUMP[k]) { this.keys.jump = true; e.preventDefault(); }
      else if (KEY_CROUCH[k]) { this.keys.crouch = true; e.preventDefault(); }
      else if (k === 'Escape') { this.close(); }
    }

    _onKeyUp(e) {
      var k = e.key;
      if (KEY_LEFT[k]) this.keys.left = false;
      else if (KEY_RIGHT[k]) this.keys.right = false;
      else if (KEY_JUMP[k]) this.keys.jump = false;
      else if (KEY_CROUCH[k]) this.keys.crouch = false;
    }

    // ---------- 自建 DOM（不依赖 index.html 里的模板） ----------

    _build() {
      if (!global.document) return;
      var wrap = global.document.createElement('div');
      wrap.className = 'calib-overlay hidden';
      wrap.innerHTML = [
        '<div class="calib-card">',
        '  <div class="calib-title">雷达动作确认</div>',
        // ---- 雷达连接：双雷达就在这里填两路地址，不用先开联调台 ----
        '  <div class="calib-link">',
        '    <label class="calib-link-dual">',
        '      <input type="checkbox" class="calib-dual">',
        '      <span>两台雷达：一台管左右，一台管上下</span>',
        '    </label>',
        '    <div class="calib-link-row">',
        '      <span class="calib-link-tag">左右</span>',
        '      <input type="text" class="calib-url-lr" spellcheck="false" autocomplete="off" placeholder="ws://192.168.1.7:8766">',
        '    </div>',
        '    <div class="calib-link-row calib-ud-row">',
        '      <span class="calib-link-tag">上下</span>',
        '      <input type="text" class="calib-url-ud" spellcheck="false" autocomplete="off" placeholder="ws://192.168.1.7:8767">',
        '    </div>',
        '    <div class="calib-link-foot">',
        '      <button type="button" class="calib-btn calib-connect">连接 / 重连</button>',
        '      <span class="calib-link-state"></span>',
        '    </div>',
        '  </div>',
        '  <div class="calib-step"></div>',
        '  <div class="calib-text"></div>',
        '  <div class="calib-msg"></div>',
        '  <div class="calib-live"></div>',
        '  <div class="calib-dots"></div>',
        '  <div class="calib-actions">',
        '    <button type="button" class="calib-btn calib-retry">重试</button>',
        '    <button type="button" class="calib-btn calib-skip">跳过（用键盘）</button>',
        '    <button type="button" class="calib-btn calib-close">关闭</button>',
        '  </div>',
        '</div>'
      ].join('\n');

      this.el = wrap;
      this.f = {
        step: wrap.querySelector('.calib-step'),
        text: wrap.querySelector('.calib-text'),
        msg: wrap.querySelector('.calib-msg'),
        live: wrap.querySelector('.calib-live'),
        dots: wrap.querySelector('.calib-dots'),
        dual: wrap.querySelector('.calib-dual'),
        urlLr: wrap.querySelector('.calib-url-lr'),
        urlUd: wrap.querySelector('.calib-url-ud'),
        udRow: wrap.querySelector('.calib-ud-row'),
        linkState: wrap.querySelector('.calib-link-state')
      };

      var self = this;
      wrap.querySelector('.calib-retry').addEventListener('click', function () {
        self._enterStep();
        self.message = '';
      });
      wrap.querySelector('.calib-skip').addEventListener('click', function () { self.skip(); });
      wrap.querySelector('.calib-close').addEventListener('click', function () { self.close(); });
      wrap.querySelector('.calib-connect').addEventListener('click', function () {
        self._connectFromPanel();
      });
      this.f.dual.addEventListener('change', function () { self._syncLinkVisibility(); });
      this.f.urlLr.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') self._connectFromPanel();
      });
      this.f.urlUd.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') self._connectFromPanel();
      });

      for (var i = 0; i < STEPS.length; i++) {
        var d = global.document.createElement('span');
        d.className = 'calib-dot';
        this.f.dots.appendChild(d);
      }

      if (this.container) this.container.appendChild(wrap);
    }

    // ---------- 雷达连接（一台 / 两台） ----------

    /** 打开时把当前数据源的地址回填到输入框 */
    _syncLinkInputs() {
      if (!this.f) return;
      var R = (RR.CFG && RR.CFG.RADAR) || {};
      var src = this.src;
      var lr = (src && src.lrUrl) || (src && src.url) || R.lrUrl || R.defaultUrl || '';
      var ud = (src && src.udUrl) || R.udUrl || '';
      var dual = !!(src && src.udUrl) || !!(R.dual && R.udUrl);

      this.f.urlLr.value = lr;
      this.f.urlUd.value = ud;
      this.f.dual.checked = dual;
      this._syncLinkVisibility();
    }

    _syncLinkVisibility() {
      if (!this.f) return;
      var on = this.f.dual.checked;
      this.f.udRow.classList.toggle('calib-hidden', !on);
      // 勾上但还没填：默认猜「同一台主机的下一个端口」，现场少想一件事
      if (on && !this.f.urlUd.value.trim()) {
        var m = /^(wss?:\/\/[^\/:]+):(\d+)/.exec(this.f.urlLr.value.trim());
        if (m) this.f.urlUd.value = m[1] + ':' + (Number(m[2]) + 1);
      }
    }

    _linkWarn(text) {
      this.message = text;
      this.messageKind = 'error';
      if (this.f && this.f.linkState) {
        this.f.linkState.textContent = text;
        this.f.linkState.className = 'calib-link-state is-bad';
      }
    }

    /** 点「连接 / 重连」：校验后交给宿主重建数据源（换地址必须重新 new 一次） */
    _connectFromPanel() {
      if (!this.f) return;
      var lr = this.f.urlLr.value.trim();
      var dual = this.f.dual.checked;
      var ud = this.f.urlUd.value.trim();

      if (!lr) return this._linkWarn('先填左右雷达地址（ws:// 开头）');
      if (!/^wss?:\/\//i.test(lr)) return this._linkWarn('左右地址要以 ws:// 或 wss:// 开头');
      if (dual) {
        if (!ud) return this._linkWarn('勾了两台雷达，还得填上下雷达地址');
        if (!/^wss?:\/\//i.test(ud)) return this._linkWarn('上下地址要以 ws:// 或 wss:// 开头');
      }
      if (typeof this.onReconnect !== 'function') {
        return this._linkWarn('这个页面不支持重连，请刷新后重试');
      }

      this.message = dual
        ? '正在连两台雷达：左右 ' + lr + ' · 上下 ' + ud
        : '正在连雷达 ' + lr;
      this.messageKind = '';
      if (this.f.linkState) {
        this.f.linkState.textContent = '正在连接…';
        this.f.linkState.className = 'calib-link-state';
      }
      this.onReconnect({ lrUrl: lr, udUrl: dual ? ud : '', dual: dual });
    }

    /** 每帧刷新两路的连接状态（掉线要当场看见，而不是等超时） */
    _syncLinkState() {
      if (!this.f || !this.f.linkState) return;
      var src = this.src;
      var el = this.f.linkState;

      if (!src) {
        el.textContent = '未连接';
        el.className = 'calib-link-state is-bad';
        return;
      }
      if (src.id !== 'radar') {
        el.textContent = '当前是「' + (src.label || src.id) + '」输入，不是雷达';
        el.className = 'calib-link-state';
        return;
      }
      if (src.udUrl) {
        var ok = src.lrOnline && src.udOnline;
        el.textContent = '左右 ' + (src.lrOnline ? '已连接' : src.lr.statusText) +
          ' · 上下 ' + (src.udOnline ? '已连接' : src.ud.statusText);
        el.className = 'calib-link-state' + (ok ? ' is-ok' : ' is-bad');
        return;
      }
      el.textContent = src.connected ? '已连接（单台）' : ('未连接：' + (src.statusText || '—'));
      el.className = 'calib-link-state' + (src.connected ? ' is-ok' : ' is-bad');
    }

    // ---------- 刷新界面 ----------

    _syncUI() {
      if (!this.el) return;
      var f = this.f;
      if (f.step) f.step.textContent = this.step.title;
      if (f.text) f.text.textContent = this.step.text;
      if (f.msg) {
        f.msg.textContent = this.message || this.step.tip;
        f.msg.className = 'calib-msg' + (this.messageKind ? ' calib-' + this.messageKind : '');
      }
      if (f.live) {
        var x = this._validX(this._sample());
        var line = 'x = ' + (x === null ? '—' : x.toFixed(2)) +
          (this.x0 === null ? '' : '   x₀ = ' + this.x0.toFixed(2));
        // v2：把上游跳/蹲误报计数摊在台面上，别让玩家以为是自己在乱动
        if (this.probe.frames) {
          var v = this.probeVerdict;
          line += '   上游误报 ' + this.probe.jumpEvents + '跳/' + this.probe.crouchEvents + '蹲' +
            '(静站 ' + this.probe.frames + ' 帧' +
            (v === 'noisy' ? '，判为不可信' : (v === 'clean' ? '，正常' : '')) + ')';
        }
        if (this.radarOffline) line += '   ⚠ 雷达未连接';
        f.live.textContent = line;
      }
      var dots = this.el.querySelectorAll('.calib-dot');
      for (var i = 0; i < dots.length; i++) {
        dots[i].classList.toggle('calib-dot-done', i < this.stepIndex);
        dots[i].classList.toggle('calib-dot-now', i === this.stepIndex);
      }

      this._syncLinkState();
    }
  }

  // 每帧自动做 jump/crouch 字段探测：包一层，外面不用管
  var _update = CalibrationWizard.prototype.update;
  CalibrationWizard.prototype.update = function (dt) {
    if (this.open_ && this.src) this._probeFrame(this.src.lastRaw);
    _update.call(this, dt);
  };

  RR.Calibration = Calibration;
  RR.CalibrationWizard = CalibrationWizard;

})(window);
