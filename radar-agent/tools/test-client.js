#!/usr/bin/env node
/* 自测客户端：连 ws://127.0.0.1:8766，检查帧率与字段齐全
 * 用法: node tools/test-client.js [url] [秒数]
 */
'use strict';
const WebSocket = require('ws');
const url = process.argv[2] || 'ws://127.0.0.1:8766';
const secs = parseFloat(process.argv[3] || '6');
const frames = [];
const t0 = Date.now();
let err = null;

const ws = new WebSocket(url);
ws.on('open', () => console.log('已连接 ' + url + '，采样 ' + secs + 's'));
ws.on('error', (e) => { err = e; });
ws.on('message', (buf) => {
  try { frames.push(JSON.parse(buf.toString())); } catch (e) { err = e; }
});

setTimeout(() => {
  console.log(`\n收到 ${frames.length} 帧，用时 ${((Date.now() - t0) / 1000).toFixed(1)}s，约 ${(frames.length / ((Date.now() - t0) / 1000)).toFixed(1)} Hz`);
  if (err) console.log('错误: ' + err.message);
  if (!frames.length) { console.log('✘ 一帧都没收到'); process.exit(2); }
  const f = frames[frames.length - 1];
  console.log('最后一帧: ' + JSON.stringify(f));
  const need = ['ts', 'x', 'height', 'jump', 'crouch', 'confidence'];
  const miss = need.filter(k => !(k in f));
  console.log(miss.length ? ('✘ 缺字段: ' + miss.join(', ')) : '✔ 字段齐全');
  const xs = frames.map(v => v.x);
  const jumps = frames.filter(v => v.jump).length;
  const crouches = frames.filter(v => v.crouch).length;
  console.log(`x 范围 ${Math.min(...xs).toFixed(2)} ~ ${Math.max(...xs).toFixed(2)} · jump 帧 ${jumps} · crouch 帧 ${crouches}`);
  const actions = [...new Set(frames.map(v => v.action).filter(Boolean))];
  console.log('action 出现过: ' + (actions.join(', ') || '（无）'));
  const ts = frames.map(v => v.ts);
  const mono = ts.every((t, i) => i === 0 || t >= ts[i - 1]);
  console.log(mono ? '✔ ts 单调' : '✘ ts 不单调');
  ws.close();
  process.exit(miss.length ? 3 : 0);
}, secs * 1000);
