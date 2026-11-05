// 自测脚本: 模拟两个点云网页(事件输出)往融合器发事件, 验证 网页→融合器→游戏 全链
// 用法: node _test_client.js   (先启动 radar_fusion.js 和游戏的 radar_bridge.py)
const fs = require('fs');
const path = require('path');
// 同 radar_fusion.js: 依次找 本目录 node_modules / 02-串口桥接 的依赖 / 旧开发目录
const _wsBin = [
  path.join(__dirname, 'node_modules', 'ws'),
  path.join(__dirname, '..', '02-串口桥接', '_work', 'node_modules', 'ws'),
  path.join(__dirname, '..', '02-串口桥接', 'node_modules', 'ws'),
  path.join(__dirname, '..', 'radar-bridge', '_work', 'node_modules', 'ws'),
].find(p => fs.existsSync(p));
const localRequire = _wsBin
  ? require('module').createRequire(path.join(_wsBin, '..', 'index.js'))
  : require;
const WebSocket = localRequire('ws');
const jc = new WebSocket('ws://127.0.0.1:8899/jc');
const lr = new WebSocket('ws://127.0.0.1:8899/lr');
const t0 = Date.now();
jc.on('open', () => console.log('[selftest] /jc 已接入'));
lr.on('open', () => console.log('[selftest] /lr 已接入'));
jc.on('error', e => console.log('[selftest] /jc 错误: ' + e.message));
lr.on('error', e => console.log('[selftest] /lr 错误: ' + e.message));
setTimeout(() => {
  jc.send(JSON.stringify({ t: Date.now(), type: 'jump', x: 0, h: 0 }));
  setTimeout(() => lr.send(JSON.stringify({ t: Date.now(), type: 'left_start', x: -0.6, h: 0 })), 400);
  setTimeout(() => jc.send(JSON.stringify({ t: Date.now(), type: 'crouch_start', x: 0, h: 0 })), 900);
  setTimeout(() => lr.send(JSON.stringify({ t: Date.now(), type: 'right_start', x: 0.6, h: 0 })), 1400);
  setTimeout(() => jc.send(JSON.stringify({ t: Date.now(), type: 'crouch_end', x: 0, h: 0 })), 1900);
}, 1000);
// 保持连接 15 秒, 让融合器的 10 秒统计能输出
setTimeout(() => { jc.close(); lr.close(); console.log('[selftest] 结束'); process.exit(0); }, 15000);
