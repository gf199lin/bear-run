#!/usr/bin/env node
/* ============================================================
 * radar-agent —— 雷达代理（游戏只连它，不直连雷达）
 *
 * 为什么需要它：
 *   浏览器直连串口时，Web Serial 的接收缓冲只有 8KB，而一帧雷达数据约 34KB
 *   并且是突发到达。JS 稍一卡顿就 overrun，读通道被打死后就表现为
 *   「连上一会儿又不行 / 一会成功一会失败」。
 *   本进程用 Node 独占串口（1MB 缓冲 + 自动重连），把结果整理成游戏要的
 *   JSON 帧，再以 WebSocket 推给游戏。串口永远只有一个进程在碰。
 *
 * 数据链：
 *   雷达 --串口--> radar-agent --ws://127.0.0.1:8766--> 游戏(radar-connect.html)
 *                              \--udp 8888--> 兼容旧链路
 *
 * 解析与判定：直接复用组员 radar-pointcloud.html 的内核（radar-core.js），
 *   解包 / 拼帧 / 点云 / 跟踪 / 手势判定与联调页面完全一致。
 *
 * 用法：
 *   node agent.js                 自动选串口
 *   node agent.js --serial COM8   指定串口
 *   node agent.js --list          只列出串口
 *   node agent.js --sim           不接雷达，发演示动作（验证游戏链路）
 *   node agent.js --replay x.bin  用录制的原始字节回放（验证解析链路）
 *   node agent.js --no-jc         关闭跳/蹲（默认已接入 JCDet v1 实验阈值）
 *   node agent.js --help
 * ============================================================ */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const dgram = require('dgram');

// ---------------------------------------------------------------- 参数
function arg(name, def) {
  const av = process.argv.slice(2);
  const p = '--' + name + '=';
  for (let i = 0; i < av.length; i++) {
    if (av[i] === '--' + name) {
      const n = av[i + 1];
      return (n && n.indexOf('--') !== 0) ? n : true;
    }
    if (av[i].indexOf(p) === 0) return av[i].slice(p.length);
  }
  return def;
}
const has = (f) => process.argv.includes('--' + f);

const CFG_OPTS = {
  serial: arg('serial', null),
  wsPort: parseInt(arg('ws-port', '8766'), 10),
  udpPort: parseInt(arg('udp-port', '8888'), 10),
  udp: !has('no-udp'),
  sim: has('sim'),
  replay: arg('replay', null),
  // 跳/蹲默认接入（组员 JCDet v1 实验阈值，离线检出 32/39）；
  // --jc 可显式开启（默认即开），现场误报多才加 --no-jc 退回观察模式
  jc: !has('no-jc'),
  chip: arg('chip', 'icl1240'),
  baud: parseInt(arg('baud', '0'), 10),
  xSpan: parseFloat(arg('x-span', '1.0')),   // 横向多少米算满偏(±1)
  invertX: has('invert-x'),
  rate: parseFloat(arg('rate', '20')),
  staleMs: parseInt(arg('stale', '1500'), 10),
  verbose: has('verbose'),
  list: has('list'),
};

// ---------------------------------------------------------------- 日志
const T0 = Date.now();
const LOGFILE = arg('log', null);
if (LOGFILE) { try { fs.writeFileSync(LOGFILE, ''); } catch (e) {} }
function writeLog(line) {
  if (!LOGFILE) return;
  try { fs.appendFileSync(LOGFILE, line + '\n'); } catch (e) {}
}
function ts() {
  const d = new Date();
  const p = (n, w = 2) => String(n).padStart(w, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}
function log(...a) {
  const line = '[' + ts() + '] ' + a.join(' ');
  console.log(line); writeLog(line);
}
function warn(...a) {
  const line = '[' + ts() + '] ⚠ ' + a.join(' ');
  console.log(line); writeLog(line);
}
function ok(...a) {
  const line = '[' + ts() + '] ✔ ' + a.join(' ');
  console.log(line); writeLog(line);
}
function die(msg) {
  const line = '[' + ts() + '] ✘ ' + msg;
  console.error(line); writeLog(line);
  process.exit(1);
}

if (has('help') || has('h')) {
  console.log(`
radar-agent —— 雷达代理（游戏只连它）

  node agent.js                 自动选串口并开始
  node agent.js --serial COM8   指定串口
  node agent.js --list          只列出串口后退出
  node agent.js --sim           不接雷达，发演示动作
  node agent.js --replay x.bin  回放录制的原始字节

  --ws-port 8766    游戏连接的端口（游戏里填 ws://127.0.0.1:8766）
  --udp-port 8888   同时向旧链路发一份 JSON（--no-udp 关闭）
  --chip icl1240    icl1240=DEV113P(默认)  icl1112=DEV202H
  --x-span 1.0      横向 1 米算满偏 ±1
  --invert-x        左右反了时打开
  --jc              强制开启跳/蹲（默认即开启；显式写上便于确认）
  --no-jc           关闭跳/蹲，退回观察模式
  --stale 1500      超过多少毫秒没有新帧就停止推送（游戏会自动判掉线）
  --log <file>      同时把日志写进文件（现场排查用）
  --verbose         打印每一帧的判定细节
`);
  process.exit(0);
}

// ---------------------------------------------------------------- 加载雷达内核
function makeDomStub() {
  const noop = () => {};
  const el = () => ({
    style: {}, classList: { add: noop, remove: noop, toggle: noop },
    addEventListener: noop, removeEventListener: noop, appendChild: noop,
    getContext: () => null, querySelector: () => null,
    setAttribute: noop, removeAttribute: noop,
    textContent: '', innerHTML: '', value: '', checked: false, width: 0, height: 0,
  });
  globalThis.window = {
    addEventListener: noop, removeEventListener: noop, devicePixelRatio: 1,
    matchMedia: () => ({ matches: false, addEventListener: noop }),
    requestAnimationFrame: () => 0, cancelAnimationFrame: noop,
    location: { hostname: '127.0.0.1', protocol: 'http:', search: '', href: 'http://127.0.0.1/' },
    open: noop, alert: noop, URLSearchParams,
  };
  globalThis.document = {
    getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
    addEventListener: noop, removeEventListener: noop, createElement: el,
    body: { appendChild: noop }, documentElement: el(), head: el(),
  };
  // Node 22 自带只读的 performance / navigator，直接赋值会抛
  // "Cannot set property navigator of #<Object> which has only a getter"
  const def = (k, v) => {
    try { Object.defineProperty(globalThis, k, { value: v, writable: true, configurable: true }); }
    catch (e) { try { globalThis[k] = v; } catch (_) {} }
  };
  def('window', globalThis.window);
  def('document', globalThis.document);
  def('performance', { now: () => Date.now(), timeOrigin: 0 });
  def('navigator', { userAgent: 'node-radar-agent' });
  def('localStorage', { getItem: () => null, setItem: noop, removeItem: noop });
  def('requestAnimationFrame', () => 0);
  def('cancelAnimationFrame', noop);
  def('alert', noop);
  def('URLSearchParams', URLSearchParams);
}

function loadCore() {
  makeDomStub();
  const file = path.join(__dirname, 'radar-core.js');
  if (!fs.existsSync(file)) die('缺少 radar-core.js，请先运行 tools/extract-core.js 生成');
  const src = fs.readFileSync(file, 'utf8');
  try {
    vm.runInThisContext(src, { filename: 'radar-core.js' });
  } catch (e) {
    die('雷达内核加载失败：' + (e && e.message));
  }
  const core = globalThis.window.__RADAR_CORE__;
  if (!core) die('雷达内核未导出（radar-core.js 末尾的 __RADAR_CORE__ 丢失？）');
  return core;
}

const CORE = loadCore();
const { StreamDemux, FrameAssembler, Cloud, Tracker, GestureEngine, JCDet, CFG, Bus, DEFAULT_CFG_SCRIPT, CFG_SCRIPTS, APP_VER } = CORE;

// 板型
const profile = CFG_SCRIPTS[CFG_OPTS.chip] ? CFG_OPTS.chip : 'icl1240';
const chipCfg = {
  icl1240: { chip: 'icl1240', baud: 1500000, rangeRes: 0.176, rangeBins: 32, rxCount: 4, chirpsPerFrame: 64 },
  icl1112: { chip: 'icl1122', baud: 921600, rangeRes: 0.704, rangeBins: 8, rxCount: 2, chirpsPerFrame: 64 },
}[profile];
Object.assign(CFG, chipCfg);
if (CFG_OPTS.baud > 0) CFG.baud = CFG_OPTS.baud;
CFG.downRate = 1;

const SCRIPT = CFG_SCRIPTS[profile] || DEFAULT_CFG_SCRIPT;

// ---------------------------------------------------------------- 游戏帧状态
const NPULSE = 260;              // 跳的脉冲宽度（跟组员/游戏约定一致）
const state = {
  x: 0, height: 0, jump: 0, crouch: 0, confidence: 0.0,
  action: null, actionAt: 0,
  dxM: 0, hM: 0,
};
let jumpTimer = null;
let lastFrameAt = 0;             // 最近一次有效 rd-map 时间
let lastEmitAt = 0;
let rdMaps = 0, chipPackets = 0, dropped = 0;
let autoCalStarted = false, autoCalDone = false, lostFrames = 0;
let wsClients = new Set();
let wsSent = 0;

function pulseJump() {
  state.jump = 1;
  state.height = Math.max(state.height, 0.85);
  if (jumpTimer) clearTimeout(jumpTimer);
  jumpTimer = setTimeout(() => {
    state.jump = 0;
    state.height = 0;
    jumpTimer = null;
  }, NPULSE);
}

function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

function noteAction(type) {
  state.action = type;
  state.actionAt = Date.now();
}

// ---------------------------------------------------------------- 管线
const demux = new StreamDemux();
const assembler = new FrameAssembler();
const tracker = new Tracker();
const gesture = new GestureEngine();

JCDet.en = true;
JCDet.swapIQ = !!CFG.swapIQ;
JCDet.observeOnly = !CFG_OPTS.jc;   // --no-jc 时只观察，不把未定稿阈值灌进游戏
JCDet.onState = (s) => {
  if (s.state === 'ready' && !JCDet._saidReady) {
    JCDet._saidReady = true;
    ok(`跳/蹲检测器就绪：人门 ${s.personM} m，基线俯仰 ${s.dev1}°${CFG_OPTS.jc ? ' · 已接入游戏' : ' · 观察模式'}`);
  }
  if (s.state === 'wait' && s.msg) {
    const now = Date.now();
    if (!JCDet._lastCalLog || now - JCDet._lastCalLog > 3000) {
      JCDet._lastCalLog = now;
      log('跳/蹲校准：' + s.msg);
    }
  }
  if (s.msg && /离开/.test(s.msg)) warn('检测器：' + s.msg);
};
JCDet.onEvent = (ev) => {
  if (ev.observe) {
    if (CFG_OPTS.verbose) log(`[JCDet 观察] ${ev.type} peak=${ev.peak || '-'}°`);
    return;
  }
  if (ev.type === 'jump') { pulseJump(); noteAction('jump'); }
  else if (ev.type === 'crouch_start') { state.crouch = 1; noteAction('crouch_start'); }
  else if (ev.type === 'crouch_end') { state.crouch = 0; state.height = 0; noteAction('crouch_end'); }
};

Bus.on('chip-packet', (p) => {
  chipPackets++;
  assembler.feed(p);
  // JCDet 自己解交织，与显示管线并行（仅 4RX+32门 的 DEV113P）
  if (p.type === 'FFT1D' && CFG.rxCount === 4 && CFG.rangeBins === 32) JCDet.push(p);
});

Bus.on('rd-map', (map) => {
  rdMaps++;
  lastFrameAt = Date.now();
  Cloud.fromRDMap(map);
});

Bus.on('cloud', () => {
  const trk = tracker.update(Cloud);
  // GestureEngine 必须先标定（页面里是手动按钮），否则永远不出 track 事件、
  // 横向位置恒为 0。这里检测到人站定后自动标定 1 秒。
  if (!autoCalDone) {
    if (trk && trk.active) {
      if (!autoCalStarted) {
        autoCalStarted = true;
        log('检测到目标，自动标定 1 秒（请站在中间别动）…');
        gesture.calibrate();
      }
    }
    if (gesture.calibrated) {
      autoCalDone = true;
      ok(`自动标定完成：中心 x=${gesture.x0.toFixed(2)}m，身高代理 r=${gesture.r0.toFixed(2)}m`);
    }
  }
  // 人离开：把横向位置缓慢收回中间，避免游戏卡在某条车道
  if (!trk || !trk.active) {
    lostFrames++;
    if (lostFrames > 8) {
      state.x *= 0.85;
      if (Math.abs(state.x) < 0.02) state.x = 0;
      if (!state.jump) state.height = 0;
      if (lostFrames > 40) { state.crouch = 0; state.confidence = 0; }
    }
  } else {
    lostFrames = 0;
  }
  gesture.update(trk);
});

Bus.on('gesture', (e) => {
  if (e.type === 'track') {
    // 连续横向位置：米 → 归一化。游戏侧还会做一次基线校准。
    const dx = (CFG_OPTS.invertX ? -e.x : e.x) / (CFG_OPTS.xSpan || 1);
    state.dxM = e.x;
    state.x = clamp(dx, -1, 1);
    state.hM = e.h;
    // height 只在跳的时候抬，避免误触跳跃（游戏里 height>=0.5 也算跳）
    if (!state.jump) state.height = 0;
    if (e.h > 0.04 && !state.crouch) state.confidence = 0.7;
    else state.confidence = 0.85;
    return;
  }
  if (e.type === 'jump') { pulseJump(); noteAction('jump'); }
  else if (e.type === 'crouch_start') { state.crouch = 1; noteAction('crouch_start'); }
  else if (e.type === 'crouch_end') { state.crouch = 0; noteAction('crouch_end'); }
  else if (e.type === 'left_start') noteAction('left_start');
  else if (e.type === 'right_start') noteAction('right_start');
  else if (e.type === 'move_stop') noteAction('move_stop');
  if (CFG_OPTS.verbose) log(`[动作] ${e.type} dx=${e.x} h=${e.h}`);
});

// ---------------------------------------------------------------- WebSocket 输出
const WebSocket = require('ws');
let wss = null;

function startWs() {
  try {
    wss = new WebSocket.Server({ port: CFG_OPTS.wsPort });
  } catch (e) {
    die(`WebSocket 端口 ${CFG_OPTS.wsPort} 启动失败：${e.message}`);
  }
  wss.on('error', (e) => {
    if (e.code === 'EADDRINUSE') {
      die(`端口 ${CFG_OPTS.wsPort} 已被占用。\n  多半是旧的 radar_bridge.py / 另一个 agent 还开着。\n  关掉它，或换个端口：node agent.js --ws-port 8770（游戏里填对应端口）`);
    }
    die('WebSocket 错误：' + e.message);
  });
  wss.on('listening', () => ok(`游戏请连：ws://127.0.0.1:${CFG_OPTS.wsPort}`));
  wss.on('connection', (ws, req) => {
    wsClients.add(ws);
    const who = (req.socket && req.socket.remoteAddress) || '?';
    log(`游戏已连接（${who}，当前 ${wsClients.size} 个）`);
    ws.on('close', () => { wsClients.delete(ws); log(`游戏断开（剩 ${wsClients.size} 个）`); });
    ws.on('error', () => {});
    // 连上立刻给一帧，避免游戏先显示"无数据"
    if (Date.now() - lastFrameAt < CFG_OPTS.staleMs) ws.send(JSON.stringify(buildFrame()));
  });
}

// ---------------------------------------------------------------- UDP 兼容
const udpSock = dgram.createSocket('udp4');

function buildFrame() {
  const now = Date.now();
  return {
    ts: now,
    x: +state.x.toFixed(3),
    height: +state.height.toFixed(3),
    jump: state.jump ? 1 : 0,
    crouch: state.crouch ? 1 : 0,
    confidence: +state.confidence.toFixed(2),
    src: 'radar-agent',
    // 诊断字段（游戏不用，联调时可以看）
    dx: +state.dxM.toFixed(3),
    fps: +frameRate.toFixed(1),
    action: state.action || '',
  };
}

let frameRate = 0;
let _fpsMark = 0, _fpsCount = 0;

/* Windows 的 setInterval 实际会漂到 ~62ms，游戏看到的就是 16Hz。
 * 这里用累积计时：每 25ms 检查一次，欠了多少帧就补多少，长期稳定在目标帧率。 */
function startTicker() {
  const period = 1000 / Math.max(1, CFG_OPTS.rate);
  let acc = 0, last = Date.now();
  setInterval(() => {
    const now = Date.now();
    acc += now - last;
    last = now;
    let guard = 0;
    while (acc >= period && guard++ < 5) { acc -= period; tick(); }
    if (acc > period * 5) acc = 0;   // 卡顿后不要暴发补帧
  }, Math.max(4, Math.round(period / 2)));
}

function tick() {
  const now = Date.now();
  // 新鲜度看门狗：雷达停了就停推，让游戏自己判掉线回键盘
  if (!lastFrameAt) return;
  if (now - lastFrameAt > CFG_OPTS.staleMs) return;
  const frame = buildFrame();
  const text = JSON.stringify(frame);
  for (const c of wsClients) {
    if (c.readyState === 1) { c.send(text); wsSent++; }
  }
  if (CFG_OPTS.udp && udpSock) {
    const buf = Buffer.from(text);
    udpSock.send(buf, CFG_OPTS.udpPort, '127.0.0.1', () => {});
  }
  lastEmitAt = now;
  _fpsCount++;
  if (now - _fpsMark >= 1000) {
    frameRate = _fpsCount * 1000 / (now - _fpsMark);
    _fpsMark = now; _fpsCount = 0;
  }
}

// ---------------------------------------------------------------- 串口
let SerialPort = null;
try {
  ({ SerialPort } = require('serialport'));
} catch (e) {
  warn('没找到 serialport 模块（node_modules 缺失）——只能跑 --sim / --replay');
}

const BAUD = CFG.baud;

const MAGIC = Buffer.from([0xFD, 0xFC, 0xFB, 0xFA]);
const TAIL = [0x04, 0x03, 0x02, 0x01];
function buildFrameCmd(cmd, data = []) {
  const len = 2 + data.length, out = Buffer.alloc(10 + len);
  out.set(MAGIC, 0); out[4] = len & 0xFF; out[5] = len >> 8;
  out[6] = cmd & 0xFF; out[7] = cmd >> 8; out.set(data, 8); out.set(TAIL, 6 + len);
  return out;
}

class Link {
  constructor() {
    this.port = null;
    this.opening = false;
    this.closing = false;
    this.retry = 0;
    this.rxBytes = 0;
    this.lastRx = 0;
    this._waiters = [];
    this._resp = Buffer.alloc(0);
    this.ready = false;
    this._lastWarn = 0;
  }

  async list() {
    if (!SerialPort) die('serialport 不可用，无法列串口');
    return SerialPort.list();
  }

  async pick() {
    if (CFG_OPTS.serial && CFG_OPTS.serial !== true) return CFG_OPTS.serial;
    const ports = await SerialPort.list();
    if (!ports.length) throw new Error('一个串口都没有（雷达 USB 插好了吗？驱动装了吗？）');
    const isBt = (p) => /蓝牙|bluetooth|BTHENUM/i.test((p.friendlyName || '') + ' ' + (p.manufacturer || '') + ' ' + (p.pnpId || ''));
    const noBt = ports.filter(p => !isBt(p));
    if (!noBt.length) {
      throw new Error('只找到蓝牙串口（' + ports.map(p => p.path).join(', ') +
        '）——雷达没被识别成 USB 串口，先查 USB 线和驱动');
    }
    const hit = noBt.find(p => /usb|stm|serial|ch340|cp210|ftdi|silicon|uart/i.test((p.friendlyName || '') + ' ' + (p.manufacturer || '') + ' ' + (p.pnpId || '')));
    return (hit || noBt[0]).path;
  }

  open_() {
    if (this.opening || this.closing) return;
    this.opening = true;
    (async () => {
      const p = await this.pick();
      if (!this._saidPort) { this._saidPort = true; log(`打开串口 ${p} @${BAUD}`); }
      const port = new SerialPort({
        path: p, baudRate: BAUD, dataBits: 8, stopBits: 1, parity: 'none',
        highWaterMark: 1 << 20,
        autoOpen: false,
      });
      this.port = port;

      port.on('data', (chunk) => {
        this.rxBytes += chunk.length;
        this.lastRx = Date.now();
        this._sniffAck(chunk);
        try { demux.push(chunk); } catch (e) {
          if (CFG_OPTS.verbose) warn('解包异常（已隔离）: ' + e.message);
        }
      });
      port.on('error', (e) => {
        const now = Date.now();
        if (now - this._lastWarn > 3000) { this._lastWarn = now; warn('串口错误：' + e.message); }
      });
      port.on('close', () => {
        this.ready = false;
        if (this.closing) return;
        warn('串口断开，2 秒后自动重连（拔插 USB 也不用手动重启）');
        this.port = null;
        this._scheduleReopen(2000);
      });

      await new Promise((res, rej) => port.open(e => e ? rej(e) : res()));
      try { await new Promise(r => port.set({ dtr: true, rts: true }, () => r())); } catch (e) {}
      await new Promise(r => setTimeout(r, 150));
      ok(`串口已打开，开始握手`);
      const hs = await this.handshake();
      if (!hs) {
        warn('握手没成功（雷达不应答）。3 秒后重试；若一直这样：拔插 USB / 关掉占用的厂商工具');
        this.closePort();
        this._scheduleReopen(3000);
        return;
      }
      await this.sendCmd(buildFrameCmd(0x0000), 600);
      await new Promise(r => setTimeout(r, 600));
      log('下发标准配置…');
      const scriptOk = await this.runScript(SCRIPT);
      if (!scriptOk) {
        warn('配置脚本有命令没应答。仍然继续（页面里同一脚本也偶发漏应答）');
      } else {
        ok('配置完成，等待雷达出帧');
      }
      this.ready = true;
      this.retry = 0;
    })().catch((e) => {
      const msg = e && e.message || String(e);
      if (!this._saidErr || Date.now() - this._saidErr > 5000) {
        this._saidErr = Date.now();
        warn('打开串口失败：' + msg);
        if (/Access denied|busy|cannot open/i.test(msg)) {
          warn('串口被别的程序占着：关掉厂商上位机 / 浏览器调试页 / 旧的桥接进程，再等自动重试');
        }
      }
      this.closePort();
      this._scheduleReopen(3000);
    }).finally(() => { this.opening = false; });
  }

  _scheduleReopen(ms) {
    if (this.closing) return;
    this.retry++;
    const wait = Math.min(ms * Math.min(this.retry, 3), 8000);
    setTimeout(() => { if (!this.closing && !this.port) this.open_(); }, wait);
  }

  closePort() {
    if (this.port) {
      try { this.port.close(() => {}); } catch (e) {}
      this.port = null;
    }
    this.ready = false;
  }

  _sniffAck(chunk) {
    if (!this._waiters.length) { if (this._resp.length) this._resp = Buffer.alloc(0); return; }
    this._resp = Buffer.concat([this._resp, chunk]);
    if (this._resp.length > 8192) this._resp = this._resp.subarray(this._resp.length - 4096);
    for (;;) {
      let m = -1;
      for (let i = 0; i + 4 <= this._resp.length; i++) {
        if (this._resp[i] === 0xFD && this._resp[i + 1] === 0xFC && this._resp[i + 2] === 0xFB && this._resp[i + 3] === 0xFA) { m = i; break; }
      }
      if (m < 0) { this._resp = Buffer.alloc(0); break; }
      if (m > 0) this._resp = this._resp.subarray(m);
      if (this._resp.length < 10) break;
      const len = this._resp[4] | (this._resp[5] << 8);
      if (len > 1024) { this._resp = this._resp.subarray(4); continue; }
      const total = 10 + len;
      if (this._resp.length < total) break;
      const t = 6 + len;
      if (this._resp[t] === 0x04 && this._resp[t + 1] === 0x03 && this._resp[t + 2] === 0x02 && this._resp[t + 3] === 0x01) {
        const cmd = this._resp[6] | (this._resp[7] << 8);
        this._dispatch(cmd);
        this._resp = this._resp.subarray(total);
      } else {
        this._resp = this._resp.subarray(4);
      }
    }
  }

  _dispatch(retCmd) {
    const i = this._waiters.findIndex(w => w.retCmd === retCmd);
    if (i < 0) return;
    const w = this._waiters.splice(i, 1)[0];
    clearTimeout(w.timer);
    w.resolve(true);
  }

  sendCmd(bytes, timeoutMs = 700) {
    if (!this.port) return Promise.resolve(null);
    const retCmd = (bytes[6] | (bytes[7] << 8)) | 0x0100;
    return new Promise((resolve) => {
      const w = { retCmd, resolve, timer: null };
      w.timer = setTimeout(() => {
        const i = this._waiters.indexOf(w);
        if (i >= 0) this._waiters.splice(i, 1);
        resolve(null);
      }, timeoutMs);
      this._waiters.push(w);
      this.port.write(bytes, (e) => {
        if (e) {
          clearTimeout(w.timer);
          const i = this._waiters.indexOf(w);
          if (i >= 0) this._waiters.splice(i, 1);
          resolve(null);
        }
      });
    });
  }

  async handshake() {
    for (let t = 0; t < 4; t++) {
      this.port.write(buildFrameCmd(0x00FF, [0x02, 0x00]));
      await new Promise(r => setTimeout(r, 150));
      if (await this.sendCmd(buildFrameCmd(0x00FF, [0x02, 0x00]), 800)) return true;
    }
    return false;
  }

  async runScript(text) {
    const lines = (Array.isArray(text) ? text.join('\n') : String(text))
      .split('\n').map(s => s.trim()).filter(s => s && !s.startsWith('#'));
    const TOL = { 0x16: 1, 0x20: 1, 0x11: 1, 0x05: 1 };
    for (const line of lines) {
      const bytes = Buffer.from(line.split(/\s+/).map(h => parseInt(h, 16)));
      if (bytes.length < 8) continue;
      const cmd = bytes[6] | (bytes[7] << 8);
      if (cmd === 0xFE) {
        this.port.write(bytes);
        await new Promise(r => setTimeout(r, 400));
        continue;
      }
      let resp = null;
      for (let t = 0; t < 8; t++) {
        resp = await this.sendCmd(bytes, 700);
        if (resp || TOL[cmd]) break;
        await new Promise(r => setTimeout(r, 60));
      }
      if (!resp && !TOL[cmd]) return false;
      await new Promise(r => setTimeout(r, 15));
    }
    return true;
  }

  async stopStream() {
    // 给板子停流，避免它继续全速喷数据
    if (!this.port) return;
    try {
      await this.sendCmd(buildFrameCmd(0x0000), 500);
      await new Promise(r => setTimeout(r, 300));
      await this.sendCmd(buildFrameCmd(0x0000), 500);
    } catch (e) {}
  }

  async shutdown() {
    this.closing = true;
    await this.stopStream();
    this.closePort();
  }
}

// ---------------------------------------------------------------- 回放
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function replay(file) {
  if (!fs.existsSync(file)) die('回放文件不存在：' + file);
  const raw = fs.readFileSync(file);
  const speed = Math.max(0.25, parseFloat(arg('replay-speed', '1')) || 1);
  // 实测雷达约 65KB/s；按此速率回放，判定/节流逻辑才与真机一致
  const bytesPerSec = 65536 * speed;
  const CH = 4096;
  log(`回放 ${path.basename(file)}（${(raw.length / 1048576).toFixed(1)} MB，${speed}× 速率）`);
  const st = demux.stats;
  const t = Date.now();
  for (let off = 0; off < raw.length; off += CH) {
    demux.push(raw.subarray(off, Math.min(off + CH, raw.length)));
    if (off + CH < raw.length) await sleep(CH / bytesPerSec * 1000);
  }
  ok(`回放完成 ${((Date.now() - t) / 1000).toFixed(1)}s：芯片包 ${st.chip}，坏包 ${st.bad}，拼帧 ${rdMaps}`);
  if (!st.chip) {
    warn('一个芯片包都没解出来：确认这份 bin 是本板型的裸流/封装流（不是别的采集格式）');
  }
  log(`游戏可连 ws://127.0.0.1:${CFG_OPTS.wsPort}，这里会以 ${CFG_OPTS.rate}Hz 重推最后一帧`);
  // 让最后一帧保持"新鲜"，方便游戏端继续观察
  setInterval(() => { lastFrameAt = Date.now(); }, 1000);
}

// ---------------------------------------------------------------- 模拟
function sim() {
  log('演示模式：不接雷达，每 12 秒一轮（跳 / 蹲 / 左 / 右）');
  state.confidence = 0.9;
  lastFrameAt = Date.now();
  setInterval(() => { lastFrameAt = Date.now(); }, 500);
  const loop = () => {
    setTimeout(() => { noteAction('jump'); pulseJump(); log('→ 跳'); }, 2000);
    setTimeout(() => { state.crouch = 1; noteAction('crouch_start'); log('→ 蹲'); }, 4000);
    setTimeout(() => { state.crouch = 0; noteAction('crouch_end'); }, 5400);
    setTimeout(() => { state.x = -0.8; state.dxM = -0.8; noteAction('left'); log('→ 左'); }, 7000);
    setTimeout(() => { state.x = 0; state.dxM = 0; }, 8000);
    setTimeout(() => { state.x = 0.8; state.dxM = 0.8; noteAction('right'); log('→ 右'); }, 9000);
    setTimeout(() => { state.x = 0; state.dxM = 0; }, 10000);
  };
  loop();
  setInterval(loop, 12000);
}

// ---------------------------------------------------------------- 启动
async function main() {
  console.log('============================================');
  console.log('  radar-agent  ·  雷达代理');
  console.log('  内核版本 v' + APP_VER + '  ·  板型 ' + profile + ' @' + BAUD);
  console.log('  跳/蹲：' + (CFG_OPTS.jc
    ? '已接入（JCDet v1 实验阈值 · 需站定 0.7~1.6m 等约 3 秒校准）'
    : '观察模式（--no-jc）'));
  console.log('============================================');

  if (CFG_OPTS.list) {
    if (!SerialPort) die('serialport 不可用');
    const ports = await SerialPort.list();
    if (!ports.length) { log('没有发现串口'); return; }
    ports.forEach(p => log(`${p.path}  ${p.friendlyName || p.manufacturer || ''}`));
    return;
  }

  startWs();
  if (CFG_OPTS.udp) {
    log(`同时向 UDP 127.0.0.1:${CFG_OPTS.udpPort} 发一份（兼容旧链路，--no-udp 关闭）`);
  }
  startTicker();
  // 状态行
  setInterval(() => {
    const now = Date.now();
    const fresh = lastFrameAt ? ((now - lastFrameAt) / 1000).toFixed(1) : '—';
    const st = demux.stats;
    log(`状态 串口${link.ready ? '通' : '断'} · 收 ${(link.rxBytes / 1048576).toFixed(1)}MB · 芯片包 ${st.chip} · 帧 ${rdMaps} · 游戏 ${wsClients.size} 个 · 已推送 ${wsSent} · 最近一帧 ${fresh}s 前`);
    if (st.chip === 0 && link.ready && link.rxBytes > 200000) {
      warn('收到字节但解不出包：确认 --chip 与板型匹配（DEV113P=icl1240），或雷达是否已被别处重新配置');
    }
  }, 10000);

  const link = new Link();

  const bye = async () => {
    log('退出：给雷达停流并关闭串口…');
    try { await link.shutdown(); } catch (e) {}
    try { if (wss) wss.close(); } catch (e) {}
    try { udpSock.close(); } catch (e) {}
    process.exit(0);
  };
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);
  process.on('uncaughtException', (e) => {
    warn('未捕获异常（进程继续）：' + (e && e.stack || e));
  });

  if (CFG_OPTS.replay && CFG_OPTS.replay !== true) {
    await replay(CFG_OPTS.replay);
  } else if (CFG_OPTS.sim) {
    sim();
  } else {
    if (!SerialPort) die('没有 serialport 模块，无法接串口。可先用 --sim 验证游戏链路');
    link.open_();
  }
}

main();
