// ============================================================
// 跳/蹲判定器  radar-detect.js
// 链路: 串口/回放 -> 256包超块(64chirp×4RX) -> 每bin能量特征 -> 判定 -> UDP 8888
//
// 用法:
//   node radar-detect.js --replay A.bin [--csv feat.csv] [--quiet]
//   node radar-detect.js --serial COM8
//   node radar-detect.js --serial COM8 --no-udp
//
// 输出(UDP 8888, 给 game 那套桥):
//   {"ts":..,"x":0,"height":..,"jump":0/1,"crouch":0/1,"confidence":..}
// ============================================================
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const dgram = require('dgram');
const localRequire = require('module').createRequire(
  path.join(__dirname, '..', 'radar-bridge', '_work', 'node_modules', 'index.js')
);
const { SerialPort } = localRequire('serialport');

// ---------------- 参数 ----------------
function arg(name, def) {
  const av = process.argv.slice(2);
  const p = '--' + name + '=';
  for (let i = 0; i < av.length; i++) {
    if (av[i].indexOf(p) === 0) return av[i].slice(p.length);
    if (av[i] === '--' + name && av[i + 1] && av[i + 1].indexOf('--') !== 0) return av[i + 1];
  }
  return def;
}
const has = (f) => process.argv.includes('--' + f);

const BIN_MIN = 2, BIN_MAX = 13;
const M_PER_BIN = 0.176;

// 判定阈值(先给初值, 用数据标定后改这里)
const TH = {
  JUMP_RATIO: 6.0,      // 跳: 能量/噪底 的峰值门
  CROUCH_RATIO: 2.2,    // 蹲: 持续期的能量门
  QUIET_RATIO: 1.5,     // 低于此值算安静
  MIN_CROUCH_FRAMES: 2, // 蹲至少持续2帧
  MAX_CROUCH_FRAMES: 12,
  WARMUP: 8             // 前N帧只学噪底, 不判定
};

// ---------------- 复用网页里的解包器(单一事实源) ----------------
let _cfg = null;
function loadCfgScript() {
  if (_cfg) return _cfg;
  const html = fs.readFileSync(path.join(__dirname, '..', 'radar-pointcloud.html'), 'utf8');
  const m = html.match(/<script>([\s\S]*)<\/script>/);
  let fakeNow = 1000;
  const g = globalThis;
  g.window = { addEventListener: () => {}, devicePixelRatio: 1 };
  g.document = { getElementById: () => null, querySelectorAll: () => [], addEventListener: () => {}, createElement: () => ({ getContext: () => null, style: {} }) };
  g.performance = { now: () => fakeNow };
  g.navigator = {}; g.localStorage = { getItem: () => null, setItem: () => {} };
  vm.runInThisContext(m[1] + ';window.__T={StreamDemux,Bus,DEFAULT_CFG_SCRIPT};', { filename: 'app.js' });
  _cfg = { StreamDemux: g.window.__T.StreamDemux, Bus: g.window.__T.Bus, SCRIPT: g.window.__T.DEFAULT_CFG_SCRIPT };
  _cfg.Bus.on('chip-packet', (p) => { if (p.type === 'FFT1D' && _activeFramer) _activeFramer.push(p); });
  return _cfg;
}
let _activeFramer = null;

const MAGIC = Buffer.from([0xFD, 0xFC, 0xFB, 0xFA]), TAIL = [0x04, 0x03, 0x02, 0x01];
function buildFrame(cmd, data = []) {
  const len = 2 + data.length, out = Buffer.alloc(10 + len);
  out.set(MAGIC, 0); out[4] = len & 0xFF; out[5] = len >> 8;
  out[6] = cmd & 0xFF; out[7] = cmd >> 8; out.set(data, 8); out.set(TAIL, 6 + len);
  return out;
}

// ---------------- 组帧器: 256包 = 64chirp × 4RX ----------------
class Framer {
  constructor(onBlock) { this.buf = []; this.onBlock = onBlock; this.dropped = 0; }
  push(p) {
    const pkt = { idx: p.idx, rx: p.rx, payload: Buffer.isBuffer(p.payload) ? p.payload : Buffer.from(p.payload) };
    if (this.buf.length === 0) { if (pkt.idx === 0 && pkt.rx === 0) this.buf.push(pkt); return; }
    const want = this.buf.length;
    if (pkt.rx === want % 4 && pkt.idx === (want >> 2)) {
      this.buf.push(pkt);
      if (this.buf.length === 256) { this.onBlock(this.buf); this.buf = []; }
    } else {
      this.dropped++;
      this.buf = (pkt.idx === 0 && pkt.rx === 0) ? [pkt] : [];
    }
  }
}

// ---------------- 特征 ----------------
class FeatureExtractor {
  constructor() {
    this.dcEma = new Float64Array(32);
    this.mvEma = new Float64Array(32);
    this.ready = false;
  }
  extract(packets) {
    const dc = new Float64Array(32), mv = new Float64Array(32);
    for (let b = 0; b < 32; b++) {
      let dcAcc = 0, mvAcc = 0;
      for (let rx = 0; rx < 4; rx++) {
        let sr = 0, si = 0, srr = 0, sii = 0, n = 0;
        for (let c = 0; c < 64; c++) {
          const pl = packets[c * 4 + rx].payload;
          const I = pl.readInt16LE(b * 4), Q = pl.readInt16LE(b * 4 + 2);
          sr += I; si += Q; srr += I * I; sii += Q * Q; n++;
        }
        const mr = sr / n, mi = si / n;
        dcAcc += mr * mr + mi * mi;
        mvAcc += (srr / n - mr * mr) + (sii / n - mi * mi);
      }
      dc[b] = dcAcc / 4; mv[b] = mvAcc / 4;
    }
    // 平滑(2帧时间常数, 3.8fps下≈0.5s)
    if (!this.ready) { this.dcEma.set(dc); this.mvEma.set(mv); this.ready = true; }
    for (let b = 0; b < 32; b++) {
      this.dcEma[b] = this.dcEma[b] * 0.6 + dc[b] * 0.4;
      this.mvEma[b] = this.mvEma[b] * 0.6 + mv[b] * 0.4;
    }
    // 人门: 静态能量最大(观察窗内)
    let gate = BIN_MIN, best = -1;
    for (let b = BIN_MIN; b <= BIN_MAX; b++) {
      if (this.dcEma[b] > best) { best = this.dcEma[b]; gate = b; }
    }
    // 人门邻域的运动能量
    let E = 0, peakBin = gate;
    for (let b = Math.max(BIN_MIN, gate - 1); b <= Math.min(BIN_MAX, gate + 1); b++) {
      if (this.mvEma[b] > E) { E = this.mvEma[b]; peakBin = b; }
    }
    // 窗内噪底(运动中位数), 用于归一化
    const win = [];
    for (let b = BIN_MIN; b <= BIN_MAX; b++) win.push(this.mvEma[b]);
    win.sort((a, b) => a - b);
    return { gate, gateM: +(gate * M_PER_BIN).toFixed(2), E, peakBin, med: win[win.length >> 1], dc: Array.from(dc), mv: Array.from(mv) };
  }
}

// ---------------- 判定状态机 ----------------
class Detector {
  constructor() {
    this.floor = null;
    this.n = 0;
    this.crouching = false;
    this.hot = 0;          // 连续超门帧数
    this.cool = 0;         // 连续安静帧数
    this.windowMax = 0;
    this.lastAction = null;
    this.lastT = 0;
  }
  // f: {E, med, gate, gateM}
  step(f) {
    this.n++;
    const out = { jump: 0, crouch: this.crouching ? 1 : 0, height: 0, conf: 0, action: null, gate: f.gateM };
    // 噪底: 取窗内中位数, 慢跟(只向下不向上太敏感)
    const base = Math.max(f.med, 100);
    if (this.floor === null) this.floor = base;
    const ratio = f.E / Math.max(this.floor, 100);
    out.ratio = +ratio.toFixed(2);
    out.floor = Math.round(this.floor);

    if (this.n <= TH.WARMUP) {
      this.floor = this.floor * 0.7 + base * 0.3;
      return out;
    }

    const hot = ratio >= TH.CROUCH_RATIO;
    if (hot) { this.hot++; this.cool = 0; this.windowMax = Math.max(this.windowMax, ratio); }
    else { this.cool++; }

    if (!this.crouching) {
      // 上升沿: 连续1帧就算"有事", 但要看后续时长决定是跳还是蹲
      if (this.hot === 1) { /* 起始 */ }
      if (this.hot >= TH.MIN_CROUCH_FRAMES) {
        this.crouching = true;
        this.hot = 0;
        out.action = 'crouch_start';
      } else if (this.hot === 1 && ratio >= TH.JUMP_RATIO) {
        // 单帧极强 -> 跳
        out.jump = 1; out.height = 0.8;
        out.action = 'jump';
        out.conf = Math.min(1, (ratio - TH.JUMP_RATIO) / 4 + 0.6);
        this.hot = 0;
      } else if (this.hot === 1 && ratio < TH.JUMP_RATIO) {
        // 弱动作, 等下一帧看是不是蹲
      }
      // 安静时噪底缓慢跟随
      if (!hot) {
        this.floor = this.floor * 0.9 + base * 0.1;
        if (this.hot > 0 && this.hot < TH.MIN_CROUCH_FRAMES) this.hot = 0;
      }
    } else {
      out.crouch = 1;
      if (this.cool >= 2) {
        this.crouching = false;
        out.crouch = 0;
        out.action = 'crouch_end';
        this.hot = 0; this.windowMax = 0;
      }
      if (hot) this.crouching = true;
    }

    if (out.action) {
      out.conf = Math.max(out.conf, Math.min(1, 0.5 + (this.windowMax - TH.CROUCH_RATIO) / 8));
      this.lastAction = out.action;
      this.windowMax = 0;
    }
    return out;
  }
}

// ---------------- UDP 发送 ----------------
const UDP_HOST = arg('udp-host', '127.0.0.1');
const UDP_PORT = parseInt(arg('udp-port', '8888'), 10);
const NO_UDP = has('no-udp');
const sock = dgram.createSocket('udp4');
const live = { x: 0, height: 0, jump: 0, crouch: 0, conf: 0 };
let lastSend = 0;

function sendState(force) {
  if (NO_UDP) return;
  const now = Date.now();
  if (!force && now - lastSend < 50) return;
  lastSend = now;
  const f = { ts: now, x: 0, height: +live.height.toFixed(2), jump: live.jump, crouch: live.crouch, confidence: +live.conf.toFixed(2) };
  sock.send(Buffer.from(JSON.stringify(f)), UDP_PORT, UDP_HOST, () => {});
}
setInterval(() => sendState(false), 200);

// ---------------- 回放 ----------------
function replay(file, csvPath) {
  const { StreamDemux } = loadCfgScript();
  const raw = fs.readFileSync(file);
  const fx = new FeatureExtractor();
  const det = new Detector();
  const rows = [];
  const quiet = has('quiet');
  const t0 = Date.now();

  const framer = new Framer((pkts) => {
    const f = fx.extract(pkts);
    const d = det.step(f);
    rows.push({
      i: rows.length, gate: f.gate, gateM: f.gateM, E: Math.round(f.E),
      med: Math.round(f.med), ratio: d.ratio, floor: d.floor,
      jump: d.jump, crouch: d.crouch, action: d.action || '', conf: +(d.conf || 0).toFixed(2)
    });
    if (!quiet) {
      const tag = d.action ? '◆' + d.action : (d.crouch ? '蹲中' : '');
      console.log(
        `#${String(rows.length).padStart(3)} 门${f.gate}(${f.gateM}m) E=${String(Math.round(f.E)).padStart(9)} ` +
        `噪=${String(d.floor).padStart(8)} 比=${String(d.ratio).padStart(6)} ${tag}`
      );
    }
  });

  _activeFramer = framer;
  const demux = new StreamDemux();
  const CH = 4096;
  for (let off = 0; off < raw.length; off += CH) demux.push(raw.subarray(off, Math.min(off + CH, raw.length)));
  _activeFramer = null;

  if (csvPath) {
    const head = 'i,gate,gateM,E,med,ratio,floor,jump,crouch,action,conf';
    fs.writeFileSync(csvPath, head + '\n' + rows.map(r => [r.i, r.gate, r.gateM, r.E, r.med, r.ratio, r.floor, r.jump, r.crouch, r.action, r.conf].join(',')).join('\n'));
  }
  const acts = rows.filter(r => r.action);
  console.log(`\n[回放] ${path.basename(file)}  ${rows.length} 帧  丢块 ${framer.dropped}  用时 ${Date.now() - t0}ms`);
  const counts = {};
  for (const a of acts) counts[a.action] = (counts[a.action] || 0) + 1;
  console.log('[判定] ' + JSON.stringify(counts));
  return rows;
}

// ---------------- 实时 ----------------
async function liveRun(portName) {
  const { StreamDemux, SCRIPT } = loadCfgScript();
  const port = new SerialPort({ path: portName, baudRate: 1500000, dataBits: 8, stopBits: 1, parity: 'none', highWaterMark: 1 << 20, autoOpen: false });
  const err = await new Promise(res => port.open(e => res(e)));
  if (err) { console.log('串口打不开: ' + err.message); process.exit(1); }
  console.log(`[串口] ${portName} @1500000`);
  const demux = new StreamDemux();
  port.on('data', c => { sniff(c); if (_activeFramer) demux.push(c); });

  let waiters = [];
  let respBuf = Buffer.alloc(0);
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ACK 解析: 固件对每条命令回一帧, 命令码 |0x0100
  function dispatch(retCmd) {
    const i = waiters.findIndex(w => w.retCmd === retCmd);
    if (i < 0) return;
    const w = waiters.splice(i, 1)[0];
    clearTimeout(w.timer); w.resolve(true);
  }
  function sniff(chunk) {
    if (!waiters.length) { respBuf = Buffer.alloc(0); return; }
    respBuf = Buffer.concat([respBuf, chunk]);
    if (respBuf.length > 4096) respBuf = respBuf.subarray(respBuf.length - 2048);
    for (;;) {
      let m = -1;
      for (let i = 0; i + 4 <= respBuf.length; i++) {
        if (respBuf[i] === 0xFD && respBuf[i + 1] === 0xFC && respBuf[i + 2] === 0xFB && respBuf[i + 3] === 0xFA) { m = i; break; }
      }
      if (m < 0) { respBuf = Buffer.alloc(0); break; }
      if (m > 0) respBuf = respBuf.subarray(m);
      if (respBuf.length < 10) break;
      const len = respBuf[4] | (respBuf[5] << 8);
      if (len > 1024) { respBuf = respBuf.subarray(4); continue; }
      const total = 10 + len;
      if (respBuf.length < total) break;
      const t = 6 + len;
      if (respBuf[t] === 0x04 && respBuf[t + 1] === 0x03 && respBuf[t + 2] === 0x02 && respBuf[t + 3] === 0x01) {
        dispatch(respBuf[6] | (respBuf[7] << 8));
        respBuf = respBuf.subarray(total);
      } else {
        respBuf = respBuf.subarray(4);
      }
    }
  }
  const sendCmd = (bytes, timeoutMs = 700) => new Promise(res => {
    const retCmd = (bytes[6] | (bytes[7] << 8)) | 0x0100;
    const w = { retCmd, resolve: res, timer: setTimeout(() => { const i = waiters.indexOf(w); if (i >= 0) waiters.splice(i, 1); res(null); }, timeoutMs) };
    waiters.push(w);
    port.write(bytes, e => { if (e) { clearTimeout(w.timer); const i = waiters.indexOf(w); if (i >= 0) waiters.splice(i, 1); res(null); } });
  });
  // 进命令模式: 发一发 -> 等 150ms -> 再发并等 ACK
  async function handshake() {
    for (let t = 0; t < 4; t++) {
      await new Promise(r => port.write(buildFrame(0x00FF, [0x02, 0x00]), () => r()));
      await sleep(150);
      if (await sendCmd(buildFrame(0x00FF, [0x02, 0x00]), 800)) return true;
    }
    return false;
  }
  async function runScript(text) {
    const lines = text.split('\n').map(s => s.trim()).filter(s => s && !s.startsWith('#'));
    const TOL = { 0x16: 1, 0x20: 1, 0x11: 1, 0x05: 1 };
    for (const line of lines) {
      const bytes = Buffer.from(line.split(/\s+/).map(h => parseInt(h, 16)));
      const cmd = bytes[6] | (bytes[7] << 8);
      if (cmd === 0xFE) { port.write(bytes); await sleep(400); continue; }
      let resp = null;
      for (let t = 0; t < 8; t++) { resp = await sendCmd(bytes, 700); if (resp || TOL[cmd]) break; await sleep(60); }
      if (!resp && !TOL[cmd]) return false;
    }
    return true;
  }

  if (!(await handshake())) { console.log('握手失败, 拔插USB重试'); process.exit(1); }
  await sendCmd(buildFrame(0x0000), 600); await sleep(600);
  console.log('[配置] 写入标准配置…');
  if (!(await runScript(SCRIPT))) { console.log('配置写入失败'); process.exit(1); }
  console.log('[配置] 完成, 开始判定\n');

  const fx = new FeatureExtractor();
  const det = new Detector();
  const framer = new Framer((pkts) => {
    const f = fx.extract(pkts);
    const d = det.step(f);
    live.crouch = d.crouch; live.jump = d.jump; live.height = d.height; live.conf = d.conf || 0;
    if (d.action) console.log(`\n◆ ${d.action}  比=${d.ratio} 门=${f.gateM}m 置信=${d.conf || 0}`);
    else process.stdout.write(`\r门${f.gateM}m 比=${String(d.ratio).padStart(6)} ${d.crouch ? '蹲中' : '      '}   `);
    sendState(true);
    setTimeout(() => { if (d.jump) { live.jump = 0; live.height = 0; sendState(true); } }, 260);
  });
  _activeFramer = framer;
  port.on('error', e => console.log('[串口错误] ' + e.message));

  let stopping = false;
  const shutdown = async () => {
    if (stopping) return; stopping = true;
    console.log('\n[退出] 给板子熄火…');
    try { await handshake(); await sendCmd(buildFrame(0x0000), 500); await sleep(400); await sendCmd(buildFrame(0x0000), 500); } catch (e) {}
    try { await new Promise(r => port.close(() => r())); } catch (e) {}
    process.exit(0);
  };
  process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown);
}

// ---------------- 入口 ----------------
const binArg = arg('replay', null);
const csvArg = arg('csv', null);
const serialArg = arg('serial', null);

if (binArg) {
  replay(binArg, csvArg);
  setTimeout(() => process.exit(0), 50);
} else if (serialArg) {
  liveRun(serialArg);
} else {
  console.log('用法:');
  console.log('  node radar-detect.js --replay A.bin [--csv out.csv]');
  console.log('  node radar-detect.js --serial COM8');
  process.exit(0);
}
