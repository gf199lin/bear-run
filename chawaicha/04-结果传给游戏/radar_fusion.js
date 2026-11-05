// ============================================================
// 双板融合器  radar_fusion.js
//
// 背景:
//   我们有两块板, 各跑一个调试网页:
//     DEV113P (跳/蹲)  -> ws://127.0.0.1:8765 -> 网页A -> 事件 ws://127.0.0.1:8899/jc
//     DEV202H (左右)   -> ws://127.0.0.1:8768 -> 网页B -> 事件 ws://127.0.0.1:8899/lr
//   游戏只认一个 20Hz 的帧流: {"ts","x","height","jump","crouch","confidence"}
//   本脚本把两路事件合并、平滑、补齐 20Hz, 往 UDP 8888 发, 交给游戏组的
//   tools/radar_bridge.py --from udp --udp-port 8888 --parse json
//
// 用法:
//   node radar_fusion.js                    # 正式: 等两个网页接入
//   node radar_fusion.js --sim              # 演示: 不接雷达, 每12秒跳/蹲/左/右
//   node radar_fusion.js --udp-port=8889 --rate=20 --x-full=0.8
//
// 参数:
//   --ws-port=8899     等网页事件接入的端口(两个网页都连这个, 路径区分 /jc /lr)
//   --udp-host=127.0.0.1  --udp-port=8888
//   --rate=20          发帧频率
//   --x-full=0.8       横向走多远算 x=±1 (米); 车道阈值 0.36 → 约 0.29m
//   --jump-hold=300    jump=1 持续毫秒(保证 20Hz 下游戏能看到上升沿)
//   --crouch-hold=350  蹲下最短保持毫秒(防抖)
//   --x-ema=0.25       横向平滑系数(越小越稳、越慢)
//   --stale=1500       多久没事件算"上游掉线"(置信度降到0.2, 低于游戏的 minConfidence=0.3)
// ============================================================
const dgram = require('dgram');
const http = require('http');
const fs = require('fs');
const path = require('path');
// ws 依赖查找顺序(找到哪个用哪个), 都没有就回落到常规 node_modules 解析:
//   1) 本目录 npm i 装出来的 node_modules
//   2) 仓库标准布局: 02-串口桥接/_work/node_modules (桥接助手随包携带的那份)
//   3) 早期开发目录 ../radar-bridge/_work/node_modules
const _wsBin = [
  path.join(__dirname, 'node_modules', 'ws'),
  path.join(__dirname, '..', '02-串口桥接', '_work', 'node_modules', 'ws'),
  path.join(__dirname, '..', '02-串口桥接', 'node_modules', 'ws'),
  path.join(__dirname, '..', 'radar-bridge', '_work', 'node_modules', 'ws'),
].find(p => fs.existsSync(p));
const localRequire = _wsBin
  ? require('module').createRequire(path.join(_wsBin, '..', 'index.js'))
  : require;
const { WebSocketServer } = localRequire('ws');

function opt(name, def) {
  const p = '--' + name + '=';
  for (const a of process.argv.slice(2)) {
    if (a.indexOf(p) === 0) return a.slice(p.length);
  }
  return def;
}
const WS_PORT = parseInt(opt('ws-port', '8899'), 10);
const UDP_HOST = opt('udp-host', '127.0.0.1');
const UDP_PORT = parseInt(opt('udp-port', '8888'), 10);
const RATE = Math.max(2, parseInt(opt('rate', '20'), 10));
const X_FULL = Math.max(0.2, parseFloat(opt('x-full', '0.8')));
const JUMP_HOLD = parseInt(opt('jump-hold', '300'), 10);
const CROUCH_HOLD = parseInt(opt('crouch-hold', '350'), 10);
const X_EMA = Math.min(1, Math.max(0.02, parseFloat(opt('x-ema', '0.25'))));
const STALE = parseInt(opt('stale', '1500'), 10);
const X_INVERT = process.argv.includes('--x-invert');
const SIM = process.argv.includes('--sim');

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const now = () => Date.now();
const log = s => console.log('[fusion] ' + s);

// ---------------- 合并状态 ----------------
const S = {
  x: 0, xRaw: 0, xSeen: false,      // 横向: 归一化 -1~1, 左负右正
  jumpUntil: 0, heightUntil: 0,
  crouchActive: false, crouchMinUntil: 0,
  jcAt: 0, lrAt: 0,
  clients: { jc: 0, lr: 0 },        // 两个网页是否在线(链路活着, 与人有没有动作无关)
  nJump: 0, nCrouch: 0, nLeft: 0, nRight: 0, nEvt: 0,
};

function onJcEvent(ev) {
  S.jcAt = now(); S.nEvt++;
  const t = String(ev && ev.type || '');
  if (t === 'jump') {
    S.jumpUntil = S.jcAt + JUMP_HOLD;
    S.heightUntil = S.jumpUntil;
    S.nJump++;
    log('跳跃 → jump=1 / ' + JUMP_HOLD + 'ms');
  } else if (t === 'crouch_start') {
    S.crouchActive = true;
    S.crouchMinUntil = S.jcAt + CROUCH_HOLD;
    S.nCrouch++;
    log('下蹲 → crouch=1');
  } else if (t === 'crouch_end') {
    S.crouchActive = false;
    log('起身 → crouch=0');
  }
  // DEV113P 的 track 事件 x 恒为 0, 忽略
}

function onLrEvent(ev) {
  S.lrAt = now(); S.nEvt++;
  const t = String(ev && ev.type || '');
  const m = Number(ev && ev.x);
  if (Number.isFinite(m)) {
    S.xRaw = clamp((X_INVERT ? -m : m) / X_FULL, -1, 1);
    S.xSeen = true;
  }
  if (t === 'left_start') S.nLeft++;
  else if (t === 'right_start') S.nRight++;
}

// ---------------- 演示模式(不接雷达, 先验证游戏链路) ----------------
function runSim() {
  log('演示模式: 每 12 秒一轮(跳2s / 蹲4-5.2s / 左7s / 右9s)');
  const loop = () => {
    setTimeout(() => onJcEvent({ type: 'jump' }), 2000);
    setTimeout(() => onJcEvent({ type: 'crouch_start' }), 4000);
    setTimeout(() => onJcEvent({ type: 'crouch_end' }), 5200);
    setTimeout(() => onLrEvent({ type: 'left_start', x: -0.6 }), 7000);
    setTimeout(() => onLrEvent({ type: 'move_stop', x: 0 }), 7800);
    setTimeout(() => onLrEvent({ type: 'right_start', x: 0.6 }), 9000);
    setTimeout(() => onLrEvent({ type: 'move_stop', x: 0 }), 9800);
  };
  loop();
  setInterval(loop, 12000);
}

// ---------------- 20Hz 输出 ----------------
const sock = dgram.createSocket('udp4');
let sent = 0, lastLog = now();

function tick() {
  const t = now();
  // 横向: 只有左右板给过数据才平滑; 掉线时"冻结"而不是回零(回零会误触发换道)
  if (S.xSeen) S.x = S.x + (S.xRaw - S.x) * X_EMA;

  const jump = t < S.jumpUntil ? 1 : 0;
  const height = t < S.heightUntil ? 0.8 : 0;
  const crouch = (S.crouchActive || t < S.crouchMinUntil) ? 1 : 0;

  // 置信度看"链路是否在线", 不看"有没有动作": 站定不动时没有事件, 但雷达是好的
  const anyClient = S.clients.jc > 0 || S.clients.lr > 0;
  const conf = (SIM || anyClient) ? 0.92 : 0.2;

  const frame = {
    ts: t,
    x: Math.round(S.x * 1000) / 1000,
    height,
    jump,
    crouch,
    confidence: conf,
  };
  sock.send(Buffer.from(JSON.stringify(frame)), UDP_PORT, UDP_HOST, () => {});
  sent++;

  if (t - lastLog >= 10000) {
    lastLog = t;
    log(`已发 ${sent} 帧 → ${UDP_HOST}:${UDP_PORT} | x=${frame.x} jump=${jump} crouch=${crouch} conf=${conf}` +
        ` | 在线 跳蹲板:${S.clients.jc} 左右板:${S.clients.lr}` +
        ` | 事件 跳${S.nJump}/蹲${S.nCrouch}/左${S.nLeft}/右${S.nRight}`);
  }
}

// ---------------- 网页事件入口 ----------------
function startWsServer() {
  const server = http.createServer((req, res) => { res.writeHead(200); res.end('radar-fusion ok'); });
  const wss = new WebSocketServer({ server });
  wss.on('connection', (ws, req) => {
    const url = (req && req.url) || '/';
    const role = url.indexOf('lr') >= 0 ? 'lr' : 'jc';
    S.clients[role]++;
    log(`网页接入: ${role}  (${url})  当前在线 跳蹲板:${S.clients.jc} 左右板:${S.clients.lr}`);
    ws.on('message', raw => {
      let ev;
      try { ev = JSON.parse(String(raw)); } catch (e) { return; }
      if (!ev || typeof ev !== 'object') return;
      if (role === 'lr') onLrEvent(ev); else onJcEvent(ev);
    });
    ws.on('close', () => {
      S.clients[role] = Math.max(0, S.clients[role] - 1);
      log(`网页断开: ${role}  当前在线 跳蹲板:${S.clients.jc} 左右板:${S.clients.lr}`);
    });
    ws.on('error', () => {});
  });
  server.listen(WS_PORT, '127.0.0.1', () => {
    log(`等网页事件: ws://127.0.0.1:${WS_PORT}/jc (跳蹲板) 和 ws://127.0.0.1:${WS_PORT}/lr (左右板)`);
  });
}

startWsServer();
if (SIM) runSim();
setInterval(tick, 1000 / RATE);
tick();
log(`输出 UDP ${UDP_HOST}:${UDP_PORT} @${RATE}Hz  x满量程 ${X_FULL}m  (Ctrl+C 退出)`);
