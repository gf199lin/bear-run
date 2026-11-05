// ============================================================
// 运动桥接助手 motion-bridge.js  (跑酷游戏专用版)
// 作用: 只输出"人体动作"——挥手/跑动/跳跃等主动运动;
//       呼吸、轻微摇摆、静止站立、家具杂物全部过滤。
// 原理三规则:
//   1) 运动能量 = 同一距离门上64个chirp的复数样本方差
//      (静止反射相位不变→方差≈0; 手0.5m/s每chirp转4圈→方差爆炸;
//       呼吸毫米级→方差埋在噪底里, 天然被滤)
//   2) 自适应能量门 = 噪底中位数×6, 噪底只在"无动作"时更新
//   3) 迟滞状态机: 连续2拍超门→报"有动作"; 连续2拍低于半门→报"安静"
// 输出: WebSocket ws://127.0.0.1:8766 每拍一条JSON:
//   {t, motion, strength, peakBin, distM, dir, zones:{near,mid,far}}
//   边缘事件: {event:"MOTION_START"/"MOTION_END", t}
// 用法:   node motion-bridge.js [COM8]        实时模式
//         node motion-bridge.js --replay 文件  离线回放验证
// 退出:   Ctrl+C / 关窗口, 退出前自动给板子熄火(0x0000)
// ============================================================
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const localRequire = require('module').createRequire(path.join(__dirname, '_work', 'node_modules', 'index.js'));
const { SerialPort } = localRequire('serialport');
const { WebSocketServer } = localRequire('ws');

const WS_PORT = 8766;          // 原始桥用8765, 运动桥用8766
const BIN_MIN = 2, BIN_MAX = 13;   // 观察窗: 0.35~2.3米 (bin0=DC, bin1=贴脸杂波, 永久忽略)
const ZONES = { near: [2, 4], mid: [5, 7], far: [8, 13] };  // 近0.35-0.7m 中0.88-1.23m 远1.4-2.3m
const GATE_MULT = 6;           // 门 = 噪底×6
const EMA_ALPHA = 0.4;         // 时间平滑 (~0.5秒窗)
const ON_NEED = 2, OFF_NEED = 2;   // 迟滞: 连续N拍才翻转
const M_PER_BIN = 0.176;

// ---- 从网页源码提取配置宏(单一事实源, 网页改了桥自动跟上); 只加载一次并缓存 ----
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
  // 只订阅一次数据包事件, 路由到"当前"组帧器(避免多次回放监听器堆积)
  _cfg.Bus.on('chip-packet', p => { if (p.type === 'FFT1D' && _activeFramer) _activeFramer.push(p); });
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

// ==================== 运动引擎(实时/回放共用) ====================
class MotionEngine {
  constructor() {
    this.smooth = new Float64Array(32);   // EMA后的每bin运动能量
    this.floor = 1e5;                     // 自适应噪底
    this.on = false; this.onCnt = 0; this.offCnt = 0;
    this.lastCentroid = -1; this.frames = 0; this.drops = 0;
  }
  // 输入: 一个64chirp×4RX超块的包数组(已按序), 输出: 判决对象
  process(packets) {
    this.frames++;
    // 每bin: 4个RX各自的64chirp复数方差, 取平均
    const motionE = new Float64Array(32);
    for (let b = 0; b < 32; b++) {
      let acc = 0;
      for (let rx = 0; rx < 4; rx++) {
        let sr = 0, si = 0, srr = 0, sii = 0, n = 0;
        for (let c = 0; c < 64; c++) {
          const pl = packets[c * 4 + rx].payload;
          const I = pl.readInt16LE(b * 4), Q = pl.readInt16LE(b * 4 + 2);
          sr += I; si += Q; srr += I * I; sii += Q * Q; n++;
        }
        const mr = sr / n, mi = si / n;
        acc += (srr / n - mr * mr) + (sii / n - mi * mi);   // 方差 = 运动能量
      }
      motionE[b] = acc / 4;
    }
    for (let b = 0; b < 32; b++) this.smooth[b] = this.smooth[b] * (1 - EMA_ALPHA) + motionE[b] * EMA_ALPHA;
    // 观察窗内的最大值/峰值/分区
    let peakBin = BIN_MIN, peakV = 0;
    const z = { near: 0, mid: 0, far: 0 };
    for (let b = BIN_MIN; b <= BIN_MAX; b++) {
      const v = this.smooth[b];
      if (v > peakV) { peakV = v; peakBin = b; }
      if (b >= ZONES.near[0] && b <= ZONES.near[1]) z.near = Math.max(z.near, v);
      if (b >= ZONES.mid[0] && b <= ZONES.mid[1]) z.mid = Math.max(z.mid, v);
      if (b >= ZONES.far[0] && b <= ZONES.far[1]) z.far = Math.max(z.far, v);
    }
    // 自适应噪底: 窗内中位数, 只在"无动作"状态更新(防止动作把自己顶死)
    const win = [];
    for (let b = BIN_MIN; b <= BIN_MAX; b++) win.push(this.smooth[b]);
    win.sort((a, b) => a - b);
    const med = win[(win.length / 2) | 0];
    if (!this.on) this.floor = this.floor * 0.7 + Math.max(med, 100) * 0.3;
    const gate = this.floor * GATE_MULT;
    // 迟滞状态机
    let event = null;
    if (!this.on) {
      if (peakV >= gate) { if (++this.onCnt >= ON_NEED) { this.on = true; this.onCnt = 0; event = 'MOTION_START'; } }
      else this.onCnt = 0;
    } else {
      if (peakV < gate * 0.5) { if (++this.offCnt >= OFF_NEED) { this.on = false; this.offCnt = 0; event = 'MOTION_END'; } }
      else this.offCnt = 0;
    }
    // 方向: 质心漂移 (近→远=远离-1, 远→近=接近+1)
    let centroid = peakBin, dir = 0;
    if (this.lastCentroid >= 0) { const d = centroid - this.lastCentroid; if (d <= -1) dir = 1; else if (d >= 1) dir = -1; }
    this.lastCentroid = centroid;
    const strength = Math.max(0, Math.min(1, (peakV / gate - 1) / 4));
    return {
      t: Date.now(), motion: this.on, strength: +strength.toFixed(2),
      peakBin, distM: +(peakBin * M_PER_BIN).toFixed(2), dir,
      zones: { near: +(z.near / gate).toFixed(2), mid: +(z.mid / gate).toFixed(2), far: +(z.far / gate).toFixed(2) },
      floor: Math.round(this.floor), event, rawPeak: Math.round(peakV)
    };
  }
}

// ---- 超块组帧器: 从包流切出256包=64chirp×4RX, 错位自动重扫 ----
class Framer {
  constructor(onBlock) { this.buf = []; this.onBlock = onBlock; this.dropped = 0; }
  push(p) {
    const pkt = { idx: p.idx, rx: p.rx, payload: Buffer.isBuffer(p.payload) ? p.payload : Buffer.from(p.payload) };
    if (this.buf.length === 0) { if (pkt.idx === 0 && pkt.rx === 0) this.buf.push(pkt); return; }
    const want = this.buf.length;
    if (pkt.rx === want % 4 && pkt.idx === (want >> 2)) {
      this.buf.push(pkt);
      if (this.buf.length === 256) { this.onBlock(this.buf); this.buf = []; }
    } else {   // 错位: 丢一个超块, 重新找头
      this.dropped++;
      this.buf = (pkt.idx === 0 && pkt.rx === 0) ? [pkt] : [];
    }
  }
}

// ==================== 离线回放模式 ====================
async function replay(file) {
  const { StreamDemux } = loadCfgScript();
  const raw = fs.readFileSync(file);
  const eng = new MotionEngine();
  const framer = new Framer(pkts => {
    const r = eng.process(pkts);
    const tag = r.motion ? '有动作' : '安静  ';
    console.log(`块${String(eng.frames).padStart(3)} [${tag}] 强度${(r.strength * 100).toFixed(0).padStart(3)}% 峰bin${r.peakBin}(${r.distM}m) 能量/门=${(r.rawPeak / r.floor / GATE_MULT).toFixed(2)} 近${r.zones.near} 中${r.zones.mid} 远${r.zones.far}${r.event ? '  ◆◆ ' + r.event : ''}`);
    results.push(r);
  });
  const results = [];
  _activeFramer = framer;
  const demux = new StreamDemux();
  const CH = 4096;
  for (let off = 0; off < raw.length; off += CH) demux.push(raw.subarray(off, Math.min(off + CH, raw.length)));
  _activeFramer = null;
  const on = results.filter(r => r.motion).length;
  const maxS = results.reduce((m, r) => Math.max(m, r.strength), 0);
  console.log('\n========== 回放成绩单 ==========');
  console.log(`${path.basename(file)}: ${eng.frames}个超块, 判"有动作"${on}个 (${(on / Math.max(1, eng.frames) * 100).toFixed(0)}%), 最大强度${(maxS * 100).toFixed(0)}%, 丢块${framer.dropped}个`);
  return { frames: eng.frames, on, maxS };
}

// ==================== 实时模式 ====================
async function live() {
  const { StreamDemux, SCRIPT } = loadCfgScript();
  const ports = await SerialPort.list();
  const arg = process.argv[2] && !process.argv[2].startsWith('-') ? process.argv[2] : null;
  // 排除蓝牙虚拟串口(名字带"串行"会抢匹配), 再优先 USB/STM/CH340/FTDI/Serial
  const noBt = ports.filter(p => !/蓝牙|bluetooth/i.test((p.friendlyName || '') + ' ' + (p.manufacturer || '')));
  const pool = noBt.length ? noBt : ports;
  const target = arg ? { path: arg } : (pool.find(p => /usb|stm|serial|ch340|ftdi|串行/i.test((p.friendlyName || '') + ' ' + (p.manufacturer || ''))) || pool[0]);
  if (!target) { console.log('没发现串口——请插好雷达板'); process.exit(1); }
  const port = new SerialPort({ path: target.path, baudRate: 1500000, dataBits: 8, stopBits: 1, parity: 'none', highWaterMark: 1 << 20, autoOpen: false });
  for (let t = 0; t < 5; t++) {
    const err = await new Promise(res => port.open(e => res(e)));
    if (!err) break;
    console.log('串口被占用, 5秒后重试… (关掉厂商软件/Edge雷达页/旧桥接)');
    await new Promise(r => setTimeout(r, 5000));
    if (t === 4) { console.log('打不开, 放弃'); process.exit(1); }
  }
  console.log(`[串口] ${target.path} 已打开 @1500000`);
  await new Promise(r => port.set({ dtr: true, rts: true }, () => r()));

  // ---- 命令/应答(配置阶段) ----
  let respBuf = Buffer.alloc(0); const waiters = [];
  const sendCmd = (bytes, timeoutMs = 700) => new Promise(res => {
    const retCmd = (bytes[6] | (bytes[7] << 8)) | 0x0100;
    const w = { retCmd, resolve: res, timer: setTimeout(() => { const i = waiters.indexOf(w); if (i >= 0) waiters.splice(i, 1); res(null); }, timeoutMs) };
    waiters.push(w);
    port.write(bytes, e => { if (e) { clearTimeout(w.timer); res(null); } });
  });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  async function handshake() {
    for (let t = 0; t < 5; t++) { port.write(buildFrame(0x00FF, [0x02, 0x00])); await sleep(150); const resp = await sendCmd(buildFrame(0x00FF, [0x02, 0x00]), 700); if (resp) return true; }
    return false;
  }
  async function runScript(text) {
    const lines = text.split('\n').map(s => s.trim()).filter(s => s && !s.startsWith('#'));
    const TOLERATE = { 0x16: 1, 0x20: 1, 0x11: 1, 0x05: 1 };
    for (const line of lines) {
      const bytes = Buffer.from(line.split(/\s+/).map(h => parseInt(h, 16)));
      const cmd = bytes[6] | (bytes[7] << 8);
      if (cmd === 0xFE) { port.write(bytes); await sleep(400); continue; }
      let resp = null;
      for (let t = 0; t < 8; t++) { resp = await sendCmd(bytes, 700); if (resp || TOLERATE[cmd]) break; await sleep(60); }
      if (!resp && !TOLERATE[cmd]) return false;
    }
    return true;
  }

  // ---- 给板子一个干净起点: 熄火→配置→开流 ----
  if (!(await handshake())) { console.log('握手失败, 请拔插USB重试'); process.exit(1); }
  await sendCmd(buildFrame(0x0000), 600); await sleep(600);
  console.log('[配置] 推送标准配置…');
  if (!(await runScript(SCRIPT))) { console.log('配置写入失败, 请拔插USB重试'); process.exit(1); }
  console.log('[配置] 完成 ✓ 开始监听运动…\n');

  // ---- 数据阶段: 包→超块→运动引擎→广播 ----
  const eng = new MotionEngine();
  const wss = new WebSocketServer({ port: WS_PORT });
  const clients = new Set();
  wss.on('connection', ws => { clients.add(ws); console.log(`[游戏] 有客户端连入 (${clients.size}个)`); ws.on('close', () => clients.delete(ws)); ws.on('error', () => {}); });
  const cast = obj => { const s = JSON.stringify(obj); for (const c of clients) if (c.readyState === 1) c.send(s); };
  const framer = new Framer(pkts => {
    const r = eng.process(pkts);
    cast(r.event ? { event: r.event, t: r.t } : r);
    if (r.event === 'MOTION_START') console.log(`\n◆◆ 有动作！ 强度${(r.strength * 100) | 0}% 距离${r.distM}m`);
    else if (r.event === 'MOTION_END') console.log(`◇◇ 安静了 (共运动${eng.frames}拍)`);
    else if (r.motion) process.stdout.write(`\r[运动中] 强度${(r.strength * 100).toFixed(0).padStart(3)}% 峰bin${r.peakBin}(${r.distM}m) 方向${r.dir > 0 ? '接近' : r.dir < 0 ? '远离' : '—'} 近${r.zones.near}/中${r.zones.mid}/远${r.zones.far}   `);
    else process.stdout.write(`\r[安静中] 噪底${r.floor} · 等待动作… (峰bin${r.peakBin} 能量/门=${(r.rawPeak / r.floor / GATE_MULT).toFixed(2)})   `);
  });
  _activeFramer = framer;
  const demux = new StreamDemux();
  port.on('data', c => demux.push(c));

  console.log(`[桥接] 游戏通道就绪: ws://127.0.0.1:${WS_PORT}  (JSON事件流)`);
  console.log('[提示] 在板前0.3~1.2米挥手/走动试试; 静坐呼吸应该毫无反应\n');

  // ---- 退出必熄火(A001教训) ----
  let stopping = false;
  const shutdown = async () => {
    if (stopping) return; stopping = true;
    console.log('\n[退出] 给板子熄火…');
    try { await handshake(); await sendCmd(buildFrame(0x0000), 500); await sleep(400); await sendCmd(buildFrame(0x0000), 500); } catch (e) {}
    try { await new Promise(r => port.close(() => r())); } catch (e) {}
    console.log('[退出] 板子已安静, 再见');
    process.exit(0);
  };
  process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown); process.on('SIGHUP', shutdown);
}

// ==================== 入口 ====================
(async () => {
  const args = process.argv.slice(2);
  const ri = args.indexOf('--replay');
  console.log('============================================');
  console.log('  运动桥接助手 v2.0  (跑酷游戏·迟钝模式)');
  console.log('============================================');
  if (ri >= 0) {
    const files = args.slice(ri + 1);
    if (!files.length) { console.log('用法: node motion-bridge.js --replay 文件1.bin [文件2.bin …]'); process.exit(1); }
    for (const f of files) await replay(f);
  } else {
    await live();
  }
})().catch(e => { console.log('出错:', e.message); process.exit(1); });
