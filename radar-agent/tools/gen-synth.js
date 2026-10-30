#!/usr/bin/env node
/* 合成 ICL1240(DEV113P) 裸流，用于在没有雷达时验证整条链路：
 *   StreamDemux 解包 -> FrameAssembler 拼帧 -> Cloud 点云 -> Tracker 跟踪 -> 横向位置
 *
 * 用法: node tools/gen-synth.js out.bin [帧数] [横向摆动米]
 * 说明: 只模拟"一个会左右移动的点目标"，用于验证管道是否通；
 *       不代表真雷达的判定精度，跳/蹲需要真机验证。
 */
'use strict';
const fs = require('fs');

const OUT = process.argv[2] || 'synth.bin';
const NFRAME = parseInt(process.argv[3] || '60', 10);
const SWING = parseFloat(process.argv[4] || '0.6');

const CHIRPS = 64, RX = 4, BINS = 32;
const TARGET_BIN = 5;            // 0.88 m
const DOPPLER_K = 3;             // 速度约 0.57 m/s
const AMP = 15000;
const dAnt = 0.0059, lambda = 0.01224;

function pkt(chirp, payload) {
  const b = Buffer.alloc(4 + payload.length + 4);
  b[0] = 0xAA;
  b[1] = 0x80 | ((chirp >> 4) & 0x03);
  b[2] = (chirp & 0x0F) << 4;
  b[3] = BINS - 1;
  payload.copy(b, 4);
  b[4 + payload.length + 2] = (chirp * 4) & 0xFF;
  b[4 + payload.length + 3] = 0x55;
  return b;
}

function frameBytes(xMeters) {
  // 横向位置 -> 相邻 RX 相位差
  const rng = TARGET_BIN * 0.176;
  let sinT = Math.max(-0.95, Math.min(0.95, xMeters / rng));
  const dPhi = sinT * 2 * Math.PI * dAnt / lambda;
  const parts = [];
  for (let c = 0; c < CHIRPS; c++) {
    for (let rx = 0; rx < RX; rx++) {
      const payload = Buffer.alloc(BINS * 4);
      const phase = 2 * Math.PI * DOPPLER_K * c / CHIRPS + rx * dPhi;
      for (let b = 0; b < BINS; b++) {
        let i, q;
        if (b === TARGET_BIN) {
          i = Math.round(AMP * Math.cos(phase));
          q = Math.round(AMP * Math.sin(phase));
        } else {
          i = Math.round((Math.random() - 0.5) * 40);
          q = Math.round((Math.random() - 0.5) * 40);
        }
        payload.writeInt16LE(Math.max(-32767, Math.min(32767, i)), b * 4);
        payload.writeInt16LE(Math.max(-32767, Math.min(32767, q)), b * 4 + 2);
      }
      parts.push(pkt(c, payload));
    }
  }
  return Buffer.concat(parts);
}

const chunks = [];
// 手势脚本：静止 -> 左 -> 回中 -> 右 -> 回中
const script = [];
for (let f = 0; f < NFRAME; f++) {
  let x = 0;
  if (f >= 10 && f < 25) x = -SWING * Math.min(1, (f - 10) / 8);
  else if (f >= 25 && f < 33) x = -SWING * Math.max(0, 1 - (f - 25) / 8);
  else if (f >= 33 && f < 48) x = SWING * Math.min(1, (f - 33) / 8);
  else if (f >= 48) x = SWING * Math.max(0, 1 - (f - 48) / 8);
  script.push(x);
  chunks.push(frameBytes(x));
}

fs.writeFileSync(OUT, Buffer.concat(chunks));
console.log(`写入 ${OUT}  ${NFRAME} 帧 ≈ ${((NFRAME * 130) / 1000).toFixed(1)}s  ${(fs.statSync(OUT).size / 1048576).toFixed(2)}MB`);
console.log('横向脚本: ' + script.filter((v, i) => i % 5 === 0).map(v => v.toFixed(2)).join(' '));
