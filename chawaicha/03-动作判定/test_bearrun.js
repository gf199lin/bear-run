// 端到端联调: 他们的 radar_bridge.py (UDP->WS) + 我们的 radar-feed.js (--sim)
// 验证: 游戏组那一侧真的能收到符合约定的帧, 且 跳/蹲/左右 能过桥
const path = require('path');
const { spawn } = require('child_process');
const localRequire = require('module').createRequire('C:/Users/叉歪叉/DeepWorks/outputs/radar-pointcloud/radar-bridge/_work/node_modules/index.js');
const WebSocket = localRequire('ws');

const RUNNER = 'C:/Users/叉歪叉/Documents/xwechat_files/wxid_3onj4akypcjx22_d38f/msg/file/2026-10/bear-run-radar-connect-20261022-share(1)/radar-runner';
const FEED = 'C:/Users/叉歪叉/DeepWorks/outputs/radar-pointcloud/bear-run/radar-feed.js';

const frames = [];
let bridgeOut = '';

const bridge = spawn('py', ['tools\\radar_bridge.py', '--from', 'udp', '--udp-port', '8888', '--parse', 'json'], {
  cwd: RUNNER, env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' }), windowsHide: true
});
bridge.stdout.on('data', d => { bridgeOut += d.toString(); });
bridge.stderr.on('data', d => { bridgeOut += d.toString(); });

setTimeout(() => {
  const feed = spawn(process.execPath, [FEED, '--sim'], { windowsHide: true });
  feed.stdout.on('data', d => process.stdout.write('[feed] ' + d.toString()));
  global.__feed = feed;
}, 2500);

function connect(attempt) {
  const ws = new WebSocket('ws://127.0.0.1:8766');
  ws.on('open', () => {
    console.log('WS 已连上 8766, 采集 14 秒...\n');
    ws.on('message', b => { try { frames.push(JSON.parse(b.toString())); } catch (e) {} });
    setTimeout(finish, 14000);
  });
  ws.on('error', () => {
    if (attempt < 12) setTimeout(() => connect(attempt + 1), 500);
    else finish();
  });
}

function finish() {
  if (global.__feed) global.__feed.kill();
  bridge.kill();

  const F = frames;
  const keys = F.length ? Object.keys(F[0]) : [];
  const has = k => keys.indexOf(k) >= 0;
  const maxOf = k => F.reduce((m, f) => Math.max(m, +f[k] || 0), 0);
  const minOf = k => F.reduce((m, f) => Math.min(m, +f[k] || 0), 0);

  const jumpOn = F.some(f => +f.jump === 1);
  const jumpOff = jumpOn && F.some(f => +f.jump === 0);
  const crouchOn = F.some(f => +f.crouch === 1);
  const crouchOff = crouchOn && F.some(f => +f.crouch === 0);
  const xs = F.map(f => +f.x || 0);

  const items = [
    ['桥接器启动并监听', /8766/.test(bridgeOut), firstLine(bridgeOut)],
    ['游戏端收到帧', F.length > 100, F.length + ' 帧'],
    ['字段齐全 (x/height/jump/crouch/confidence/ts)', ['x', 'height', 'jump', 'crouch', 'confidence', 'ts'].every(has), keys.join(',')],
    ['jump 有 0->1->0', jumpOn && jumpOff, 'max=' + maxOf('jump')],
    ['crouch 有 0->1->0', crouchOn && crouchOff, 'max=' + maxOf('crouch')],
    ['x 能到负值(左)', Math.min.apply(null, xs) < -0.5, 'min=' + Math.min.apply(null, xs)],
    ['x 能到正值(右)', Math.max.apply(null, xs) > 0.5, 'max=' + Math.max.apply(null, xs)],
    ['height 随跳抬起', maxOf('height') > 0.5, 'max=' + maxOf('height')]
  ];

  const bad = items.filter(i => !i[1]);
  console.log('\n================ 结果 ================');
  for (const i of items) console.log((i[1] ? '  PASS  ' : '  FAIL  ') + i[0] + '  (' + i[2] + ')');
  console.log('\n样例帧:');
  for (const f of [F[0], F[Math.floor(F.length / 2)], F[F.length - 1]]) {
    if (f) console.log('  ' + JSON.stringify(f));
  }
  if (bad.length) {
    console.log('\n桥接器输出(前 1200 字):\n' + bridgeOut.slice(0, 1200));
  }
  console.log('\n' + (bad.length ? '失败 ' + bad.length + ' 项' : '全部 ' + items.length + ' 项通过'));
  process.exit(bad.length ? 1 : 0);
}

function firstLine(s) {
  const l = s.split(/\r?\n/).filter(x => x.trim());
  return l.slice(0, 2).join(' | ');
}

connect(0);
