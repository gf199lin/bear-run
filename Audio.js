/**
 * 音频引擎：所有声音都用 WebAudio 现场合成，工程里没有任何音频文件。
 *
 * 为什么不用 mp3/ogg：
 *   这个游戏是"双击 index.html 就能玩"的（file:// 也能跑）。file:// 下加载外部
 *   音频要么被 CORS 拦、要么在部分浏览器里静默失败，为了不让音效变成"有时有
 *   有时没有"的玄学问题，干脆全部用振荡器 + 噪声合成 8-bit 音色。
 *   顺带好处：包体积 0 增长，改音色只改数字。
 *
 * 音量三层：master（总）→ sfx / bgm（分轨）。静音 = 直接把 AudioContext 挂起，
 * 既绝对安静又省 CPU。静音状态记在 localStorage（bear-run:audio）。
 *
 * 浏览器自动播放策略：AudioContext 必须由"用户手势"解锁，所以真实发声从玩家
 * 第一次点击/按键开始。网页里的首次点击会走 Audio.install() 装的事件委托，
 * 在那里完成 unlock()。
 *
 * 任何环节失败都必须静默降级：音频坏了顶多没声音，绝不能让游戏报错或停住。
 * 所以所有对外方法内部都包了 try/catch。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG = RR.CFG || {};
  var A = CFG.AUDIO || {};

  // ---------------------------------------------------------------- 合成工具

  var ctx = null;         // AudioContext
  var master = null;      // 总音量
  var sfxBus = null;      // 音效分轨
  var bgmBus = null;      // 背景音乐分轨
  var noiseBuf = null;    // 复用的白噪声缓冲（打击乐/风声都用它）

  function mtof(m) { return 440 * Math.pow(2, (m - 69) / 12); }

  function num(v, d) { return (typeof v === 'number' && isFinite(v)) ? v : d; }

  /**
   * 固定种子的伪随机噪声。
   * 用固定种子而不是 Math.random()：每次运行听到的噪声完全一致，
   * 便于"改了之后有没有变"的对比。
   */
  function makeNoiseBuffer(ac) {
    var len = Math.floor(ac.sampleRate * 1.0);
    var buf = ac.createBuffer(1, len, ac.sampleRate);
    var d = buf.getChannelData(0);
    var seed = 20130118;                       // 熊出没开播日，纯粹图个吉利
    for (var i = 0; i < len; i++) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      d[i] = (seed / 0x3fffffff) - 1;
    }
    return buf;
  }

  /**
   * 一个带包络的振荡器音符。
   * opt: { type, freq, freq2, sweep:'exp'|'lin', detune, t, dur, gain,
   *        attack, hold, filter, filterFreq, filterFreq2, q, bus }
   */
  function tone(opt) {
    if (!ctx) return;
    var t0 = num(opt.t, ctx.currentTime);
    var dur = num(opt.dur, 0.15);
    var f0 = num(opt.freq, 440);
    var f2 = opt.freq2 == null ? null : num(opt.freq2, f0);

    var osc = ctx.createOscillator();
    osc.type = opt.type || 'square';
    osc.frequency.setValueAtTime(Math.max(1, f0), t0);

    // 只在结束时刻设目标值，中间留给浏览器选曲线（人耳听感够用，代码也短）
    if (f2 != null) {
      var target = Math.max(1, f2);
      if (opt.sweep === 'lin') osc.frequency.linearRampToValueAtTime(target, t0 + dur);
      else osc.frequency.exponentialRampToValueAtTime(target, t0 + dur);
    }
    if (opt.detune) osc.detune.setValueAtTime(opt.detune, t0);

    var node = osc;

    if (opt.filter) {
      var bq = ctx.createBiquadFilter();
      bq.type = opt.filter;
      var ff = num(opt.filterFreq, 1200);
      bq.frequency.setValueAtTime(Math.max(1, ff), t0);
      if (opt.filterFreq2) {
        bq.frequency.exponentialRampToValueAtTime(Math.max(1, num(opt.filterFreq2, ff)), t0 + dur);
      }
      bq.Q.value = num(opt.q, 1);
      node.connect(bq);
      node = bq;
    }

    var peak = Math.max(0.0001, num(opt.gain, 0.25));
    var atk = Math.max(0.0005, num(opt.attack, 0.004));
    var g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.linearRampToValueAtTime(peak, t0 + Math.min(atk, dur));
    if (opt.hold) g.gain.setValueAtTime(peak, t0 + atk + num(opt.hold, 0));
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

    node.connect(g);
    g.connect(opt.bus || sfxBus);

    osc.start(t0);
    osc.stop(t0 + dur + 0.03);
  }

  /** 一段被滤波器塑形的噪声（脚步声、滑铲摩擦、斧头劈风都用它） */
  function noise(opt) {
    if (!ctx || !noiseBuf) return;
    var t0 = num(opt.t, ctx.currentTime);
    var dur = num(opt.dur, 0.15);

    var src = ctx.createBufferSource();
    src.buffer = noiseBuf;
    src.loop = true;
    if (opt.rate) src.playbackRate.value = num(opt.rate, 1);

    var bq = ctx.createBiquadFilter();
    bq.type = opt.filter || 'bandpass';
    var ff = num(opt.freq, 1200);
    bq.frequency.setValueAtTime(Math.max(1, ff), t0);
    if (opt.freq2) {
      bq.frequency.exponentialRampToValueAtTime(Math.max(1, num(opt.freq2, ff)), t0 + dur);
    }
    bq.Q.value = num(opt.q, 1.2);

    var peak = Math.max(0.0001, num(opt.gain, 0.2));
    var atk = Math.max(0.0005, num(opt.attack, 0.004));
    var g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.linearRampToValueAtTime(peak, t0 + Math.min(atk, dur));
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

    src.connect(bq);
    bq.connect(g);
    g.connect(opt.bus || sfxBus);

    src.start(t0);
    src.stop(t0 + dur + 0.03);
  }

  // ---------------------------------------------------------------- 音效表

  /**
   * 每个音效是一个 (t, k) => void 的小函数：
   *   t = 起始时刻（AudioContext 时间轴）
   *   k = 上下文参数（目前只有金币连吃用得到：第几个）
   */
  var SFX = {
    /* 跳跃：一声上滑的"啾"，方形波才够 8-bit */
    jump: function (t) {
      tone({ type: 'square', freq: 330, freq2: 780, t: t, dur: 0.13, gain: 0.26, attack: 0.003 });
      tone({ type: 'triangle', freq: 165, freq2: 390, t: t, dur: 0.13, gain: 0.14 });
    },

    /* 落地：闷响 + 一点土味噪声，配合"压扁"那一下 */
    land: function (t) {
      tone({ type: 'sine', freq: 150, freq2: 58, t: t, dur: 0.14, gain: 0.30 });
      noise({ t: t, dur: 0.11, filter: 'lowpass', freq: 900, freq2: 220, gain: 0.22 });
    },

    /* 滑铲：一段往下拉长的摩擦声 */
    slide: function (t) {
      noise({ t: t, dur: 0.34, filter: 'bandpass', freq: 2600, freq2: 380, q: 1.1, gain: 0.20 });
    },

    /* 换道：极短的一声"呼"，纯提示用，必须很轻（换道太频繁，响了就烦） */
    lane: function (t) {
      noise({ t: t, dur: 0.09, filter: 'bandpass', freq: 1500, freq2: 620, q: 1.8, gain: 0.09 });
    },

    /* 吃蜂蜜罐：经典两段"叮叮"，连吃会一路升key（k = 连吃序号） */
    coin: function (t, k) {
      var semi = Math.min(k | 0, num(A.coinPitchMax, 9)) * num(A.coinPitchStep, 0.6);
      var base = 83 + semi;                    // B5 起
      tone({ type: 'square', freq: mtof(base), t: t, dur: 0.055, gain: 0.22, attack: 0.002 });
      tone({ type: 'square', freq: mtof(base + 7), t: t + 0.045, dur: 0.10, gain: 0.18, attack: 0.002 });
    },

    /* 回血：C 大三和弦往上滚，听感"舒服/被治好" */
    heal: function (t) {
      var notes = [72, 76, 79, 84];
      for (var i = 0; i < notes.length; i++) {
        tone({ type: 'triangle', freq: mtof(notes[i]), t: t + i * 0.055, dur: 0.24, gain: 0.20 });
      }
    },

    /* 蜂蜜滑道：带滤波打开的上冲，速度感 */
    boost: function (t) {
      tone({
        type: 'sawtooth', freq: 220, freq2: 1320, t: t, dur: 0.38, gain: 0.20,
        filter: 'lowpass', filterFreq: 700, filterFreq2: 4200, q: 0.9
      });
      tone({ type: 'square', freq: 660, freq2: 1760, t: t, dur: 0.30, gain: 0.13, attack: 0.01 });
    },

    /* 撞到障碍：下滑的刺耳声 + 一记撞击噪声 */
    hurt: function (t) {
      tone({ type: 'sawtooth', freq: 420, freq2: 110, t: t, dur: 0.26, gain: 0.26 });
      noise({ t: t, dur: 0.16, filter: 'lowpass', freq: 1800, freq2: 300, gain: 0.18 });
    },

    /* 斧头落地（处决）：劈风声 + 低频轰 + 一点金属余音 = 全片最重的音效 */
    axe: function (t) {
      noise({ t: t, dur: 0.30, filter: 'highpass', freq: 2600, freq2: 900, gain: 0.30 });
      tone({ type: 'sine', freq: 130, freq2: 40, t: t, dur: 0.45, gain: 0.42 });
      tone({ type: 'square', freq: 1180, freq2: 620, t: t + 0.005, dur: 0.34, gain: 0.10 });
      tone({ type: 'square', freq: 1760, freq2: 900, t: t + 0.012, dur: 0.26, gain: 0.065 });
    },

    /* 光头强追上来：两下低沉警告音，压迫感 */
    chase: function (t) {
      tone({ type: 'sawtooth', freq: 110, t: t, dur: 0.16, gain: 0.26, filter: 'lowpass', filterFreq: 700 });
      tone({ type: 'sawtooth', freq: 92, t: t + 0.17, dur: 0.26, gain: 0.28, filter: 'lowpass', filterFreq: 600 });
      noise({ t: t, dur: 0.40, filter: 'bandpass', freq: 420, freq2: 180, q: 0.8, gain: 0.09 });
    },

    /* 血掉光、被逮住：一整条下行音阶，"完了"的预感 */
    death: function (t) {
      var notes = [60, 56, 51, 44];
      for (var i = 0; i < notes.length; i++) {
        tone({
          type: 'square', freq: mtof(notes[i]), t: t + i * 0.13, dur: 0.30, gain: 0.24,
          filter: 'lowpass', filterFreq: 1500
        });
      }
    },

    /* 结算界面弹出 */
    gameover: function (t) {
      var notes = [64, 60, 57, 52];
      for (var i = 0; i < notes.length; i++) {
        tone({ type: 'triangle', freq: mtof(notes[i]), t: t + i * 0.16, dur: 0.42, gain: 0.26 });
      }
    },

    /* 破纪录：五音上行小号角 */
    best: function (t) {
      var notes = [72, 76, 79, 84, 88];
      for (var i = 0; i < notes.length; i++) {
        tone({ type: 'square', freq: mtof(notes[i]), t: t + i * 0.09, dur: 0.28, gain: 0.22 });
      }
    },

    /* 进入新区域：明亮的四音上行 + 一点空气感 */
    region: function (t) {
      var notes = [67, 71, 74, 79];
      for (var i = 0; i < notes.length; i++) {
        tone({ type: 'square', freq: mtof(notes[i]), t: t + i * 0.11, dur: 0.30, gain: 0.20 });
      }
      noise({ t: t, dur: 0.50, filter: 'highpass', freq: 5000, gain: 0.06 });
    },

    /* UI 点击：一记几乎听不见的"嗒" */
    ui: function (t) {
      tone({ type: 'square', freq: 880, t: t, dur: 0.045, gain: 0.14, attack: 0.002 });
    }
  };

  // ---------------------------------------------------------------- 背景音乐

  /**
   * 4 小节循环：C - Am - F - G（最不容易听腻的流行走向）。
   * 每行 32 个 8 分音符 = 4 小节 × 8 个；0 表示该拍休止。
   * 速度由 CFG.AUDIO.bgmBpm 决定，改数字即可。
   */
  var BGM_BASS = [
    48, 0, 55, 0, 48, 0, 55, 0,
    45, 0, 52, 0, 45, 0, 52, 0,
    41, 0, 48, 0, 41, 0, 48, 0,
    43, 0, 50, 0, 43, 0, 50, 0
  ];
  var BGM_LEAD = [
    72, 76, 79, 76, 72, 79, 84, 79,
    69, 72, 76, 72, 69, 76, 81, 76,
    65, 69, 72, 69, 65, 72, 77, 72,
    67, 71, 74, 71, 67, 74, 79, 74
  ];
  var BGM_HAT = [
    1, 0, 1, 1, 1, 0, 1, 1,
    1, 0, 1, 1, 1, 0, 1, 1,
    1, 0, 1, 1, 1, 0, 1, 1,
    1, 0, 1, 1, 1, 0, 1, 1
  ];

  var bgmTimer = null;
  var bgmPlaying = false;
  var bgmStep = 0;
  var bgmNextT = 0;
  var ducked = false;
  var LOOKAHEAD = 0.35;      // 每次调度往未来排 0.35 秒的音符
  var TICK_MS = 60;          // 定时器间隔

  /** 把未来 LOOKAHEAD 窗口内的音符排出去（WebAudio 的经典调度写法） */
  function bgmSchedule() {
    if (!ctx || !bgmPlaying) return;
    var eighth = 60 / Math.max(40, num(A.bgmBpm, 116)) / 2;

    while (bgmNextT < ctx.currentTime + LOOKAHEAD) {
      var i = bgmStep % 32;
      var t = bgmNextT;
      var bass = BGM_BASS[i];
      var lead = BGM_LEAD[i];

      if (bass) {
        tone({
          type: 'triangle', freq: mtof(bass), t: t, dur: eighth * 1.6,
          gain: 0.30, bus: bgmBus, attack: 0.01
        });
      }
      if (lead) {
        tone({
          type: 'square', freq: mtof(lead), t: t, dur: eighth * 1.4,
          gain: 0.085, bus: bgmBus, attack: 0.006
        });
      }
      if (BGM_HAT[i]) {
        noise({ t: t, dur: 0.045, filter: 'highpass', freq: 7000, gain: 0.045, bus: bgmBus });
      }
      if (i % 8 === 0) {                    // 每小节第一拍：底鼓
        tone({ type: 'sine', freq: 120, freq2: 45, t: t, dur: 0.16, gain: 0.34, bus: bgmBus });
      }
      if (i % 8 === 4) {                    // 第三拍：军鼓
        noise({ t: t, dur: 0.09, filter: 'bandpass', freq: 1900, q: 0.9, gain: 0.12, bus: bgmBus });
      }

      bgmNextT += eighth;
      bgmStep++;
      if (bgmStep % 32 === 0) Audio.stats.bgmLoops++;
    }
  }

  function bgmStart() {
    if (!ctx || bgmPlaying) return;
    bgmPlaying = true;
    bgmStep = 0;
    bgmNextT = ctx.currentTime + 0.08;
    bgmSchedule();
    bgmTimer = global.setInterval(bgmSchedule, TICK_MS);
  }

  function bgmStop() {
    bgmPlaying = false;
    if (bgmTimer) {
      global.clearInterval(bgmTimer);
      bgmTimer = null;
    }
  }

  // ---------------------------------------------------------------- 对外接口

  var Audio = {
    ready: false,      // AudioContext 是否建好
    failed: false,     // 环境不支持 / 建失败：之后所有调用直接空转
    muted: false,
    // 自检用计数：自动化测试靠它确认"音效真的触发了"（无头环境听不到声音）
    stats: { played: 0, byName: {}, bgmLoops: 0 },

    /** 建 AudioContext。惰性调用，第一次真正需要发声时才建。 */
    init: function () {
      if (Audio.ready || Audio.failed) return Audio.ready;
      if (A.enabled === false) { Audio.failed = true; return false; }
      try {
        var AC = global.AudioContext || global.webkitAudioContext;
        if (!AC) { Audio.failed = true; return false; }

        ctx = new AC();
        master = ctx.createGain();
        master.gain.value = Audio.muted ? 0 : num(A.master, 0.55);
        master.connect(ctx.destination);

        sfxBus = ctx.createGain();
        sfxBus.gain.value = num(A.sfx, 0.85);
        sfxBus.connect(master);

        bgmBus = ctx.createGain();
        bgmBus.gain.value = num(A.bgm, 0.28);
        bgmBus.connect(master);

        noiseBuf = makeNoiseBuffer(ctx);
        Audio.ready = true;
      } catch (e) {
        ctx = null;
        Audio.ready = false;
        Audio.failed = true;
      }
      return Audio.ready;
    },

    /**
     * 用户手势解锁。必须在真实的 click / keydown / touch 回调里调用。
     * 返回 AudioContext 当前状态，方便自动化测试断言（'running' 才算真解锁）。
     */
    unlock: function () {
      Audio.init();
      if (!ctx) return 'unavailable';
      try {
        if (ctx.state === 'suspended') {
          var p = ctx.resume();
          if (p && p.catch) p.catch(function () { /* 忽略 */ });
        }
      } catch (e) { /* 忽略 */ }
      // iOS Safari 的老毛病：不真的播放过一次，state 就一直是 suspended。
      // 播一个 1 帧的静音 buffer 骗它一下，听感上完全无声。
      try {
        if (!Audio._primed) {
          var s = ctx.createBufferSource();
          s.buffer = ctx.createBuffer(1, 1, ctx.sampleRate);
          s.connect(master);
          s.start(0);
          Audio._primed = true;
        }
      } catch (e) { /* 忽略 */ }
      return ctx.state;
    },

    /* 静音状态：'running' / 'suspended' / 'unavailable' */
    state: function () {
      if (!ctx) return Audio.failed ? 'unavailable' : 'idle';
      return ctx.state;
    },

    /**
     * 播一个音效。
     *   name = SFX 表的键
     *   k    = 上下文参数（金币连吃序号由内部自己算，调用方不用管）
     * 静音 / 不支持 / 单个音效出错，一律直接返回，绝不影响游戏。
     */
    sfx: function (name, k) {
      if (A.enabled === false || Audio.muted) return;
      if (!Audio.ready && !Audio.init()) return;

      if (name === 'coin') {
        var nowMs = (global.performance && global.performance.now)
          ? global.performance.now() : Date.now();
        var win = num(A.chainWindow, 700);
        if (Audio._lastCoinAt && (nowMs - Audio._lastCoinAt) < win) Audio._coinChain++;
        else Audio._coinChain = 0;
        Audio._lastCoinAt = nowMs;
        k = Audio._coinChain;
      }

      var fn = SFX[name];
      if (!fn) return;
      try {
        if (ctx.state === 'suspended') {
          var p = ctx.resume();
          if (p && p.catch) p.catch(function () { /* 忽略 */ });
        }
        fn(ctx.currentTime + 0.001, k || 0);
        Audio.stats.played++;
        Audio.stats.byName[name] = (Audio.stats.byName[name] || 0) + 1;
      } catch (e) { /* 忽略 */ }
    },

    /* ---- 背景音乐 ---- */

    startBgm: function () {
      // 先记住"游戏希望有音乐"：静音期间也会被记下来，取消静音后自动接上
      Audio._wantBgm = true;
      if (A.enabled === false || Audio.muted) return;
      if (!Audio.ready && !Audio.init()) return;
      try { bgmStart(); } catch (e) { /* 忽略 */ }
    },

    stopBgm: function () {
      Audio._wantBgm = false;
      try { bgmStop(); } catch (e) { /* 忽略 */ }
    },

    /** 处决演出时把音乐压下去，别盖过斧头声 */
    setDuck: function (on) {
      if (!ctx || !bgmBus) return;
      var base = num(A.bgm, 0.28);
      var target = on ? base * num(A.duckRatio, 0.32) : base;
      ducked = !!on;
      try {
        bgmBus.gain.cancelScheduledValues(ctx.currentTime);
        bgmBus.gain.setValueAtTime(bgmBus.gain.value, ctx.currentTime);
        bgmBus.gain.linearRampToValueAtTime(target, ctx.currentTime + 0.35);
      } catch (e) { /* 忽略 */ }
    },

    isMuted: function () { return Audio.muted; },

    /** 静音开关（HUD 右下角按钮 / 测试都会走这里）。返回切换后的状态。 */
    toggle: function () {
      Audio.muted = !Audio.muted;
      Audio._save();
      try {
        if (Audio.muted) {
          bgmStop();
          if (master) master.gain.value = 0;
          if (ctx && ctx.suspend) {
            var p = ctx.suspend();
            if (p && p.catch) p.catch(function () { /* 忽略 */ });
          }
        } else {
          Audio.init();
          if (ctx) {
            if (ctx.resume) {
              var q = ctx.resume();
              if (q && q.catch) q.catch(function () { /* 忽略 */ });
            }
            if (master) master.gain.value = num(A.master, 0.55);
          }
          if (ducked) Audio.setDuck(true);
          if (Audio._wantBgm) Audio.startBgm();
        }
      } catch (e) { /* 忽略 */ }
      if (Audio.onMuteChange) {
        try { Audio.onMuteChange(Audio.muted); } catch (e2) { /* 忽略 */ }
      }
      return Audio.muted;
    },

    /* ---- 持久化 ---- */

    _load: function () {
      try {
        var v = global.localStorage.getItem(CFG.STORAGE_KEY_AUDIO || 'bear-run:audio');
        Audio.muted = (v === '1');
      } catch (e) { Audio.muted = false; }
    },

    _save: function () {
      try {
        global.localStorage.setItem(CFG.STORAGE_KEY_AUDIO || 'bear-run:audio', Audio.muted ? '1' : '0');
      } catch (e) { /* 隐私模式下写不了，忽略 */ }
    },

    /**
     * 装配全局钩子（在 main.js 里调一次）：
     *   1) 事件委托：页面里任何按钮被点 -> 解锁音频 + 一记 UI 音
     *   2) 首次手势解锁兜底：玩家要是用空格/回车开局，也能正常出声
     */
    install: function () {
      if (Audio._installed) return;
      Audio._installed = true;

      document.addEventListener('click', function (e) {
        var el = e.target;
        while (el && el.nodeType === 1 && el !== document.body) {
          var cls = el.className ? String(el.className) : '';
          if (el.tagName === 'BUTTON' || cls.indexOf('chip') >= 0) {
            Audio.unlock();
            Audio.sfx('ui');
            return;
          }
          el = el.parentNode;
        }
      }, true);   // 捕获阶段：比业务 handler 先响，点击手感更跟手

      var once = function () {
        Audio.unlock();
        document.removeEventListener('pointerdown', once, true);
        document.removeEventListener('touchstart', once, true);
        document.removeEventListener('keydown', once, true);
      };
      document.addEventListener('pointerdown', once, true);
      document.addEventListener('touchstart', once, true);
      document.addEventListener('keydown', once, true);
    }
  };

  Audio._load();

  RR.Audio = Audio;

})(window);
