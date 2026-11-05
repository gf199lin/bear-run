// diag_jc.js — 诊断 takeA：near 曲线（找对时挥手）+ 人门轨迹
const fs = require('fs'), path = require('path');
const b = fs.readFileSync('C:/Users/叉歪叉/DeepWorks/uploads/ses_ec02eb2aeffeNQVRmmapAn7kfo/radar_raw_1792576640965.bin');
const REC = 136, G = 32, ND = 32, FPS = 1 / 0.52084;
const sig = (p) => p + REC <= b.length && b[p] === 0xAA && (b[p + 1] & 0xF0) === 0x80 && b[p + 3] === 0x1F;
const recOff = []; let pos = 0;
while (pos + REC <= b.length) {
  if (sig(pos)) { recOff.push(pos); pos += REC; }
  else { let q = pos + 1; while (q + REC <= b.length && !sig(q)) q++; if (q + REC > b.length) break; recOff.push(q); pos = q + REC; }
}
const frames = []; let cur = null, lastCh = -1, grp = 0;
for (const p of recOff) {
  const chirp = ((b[p + 1] & 3) << 4) | (b[p + 2] >>> 4);
  if (chirp !== lastCh) { lastCh = chirp; grp = 0; }
  const rx = grp++; if (rx > 3) continue;
  if (chirp === 0 && cur && cur.n > 100) { frames.push(cur); cur = null; }
  if (!cur) cur = { n: 0, d: new Map() };
  if (!cur.d.has(chirp)) cur.d.set(chirp, [null, null, null, null]);
  cur.d.get(chirp)[rx] = p + 4; cur.n++;
}
if (cur && cur.n > 100) frames.push(cur);
const full = frames.filter(fr => fr.n >= 240);
const cosT = [], sinT = [];
for (let d = 0; d < ND; d++) { cosT.push([]); sinT.push([]); for (let k = 0; k < ND; k++) { const a = -2 * Math.PI * d * k / ND; cosT[d].push(Math.cos(a)); sinT[d].push(Math.sin(a)); } }
function doppler(fr, parity) {
  const iq = (ch, rx, g) => { const off = fr.d.get(ch)?.[rx]; if (off == null) return [0, 0]; return [b.readInt16LE(off + g * 4), b.readInt16LE(off + g * 4 + 2)]; };
  const out = [];
  for (let rx = 0; rx < 4; rx++) {
    const rows = []; for (let d = 0; d < ND; d++) rows.push(new Float64Array(G * 2));
    for (let g = 0; g < G; g++) {
      const xr = [], xi = [];
      for (let k = 0; k < ND; k++) { const [I, Q] = iq(parity + 2 * k, rx, g); xr.push(I); xi.push(Q); }
      for (let d = 0; d < ND; d++) { let re = 0, im = 0;
        for (let k = 0; k < ND; k++) { re += xr[k] * cosT[d][k] - xi[k] * sinT[d][k]; im += xr[k] * sinT[d][k] + xi[k] * cosT[d][k]; }
        rows[d][g * 2] = re; rows[d][g * 2 + 1] = im; }
    }
    out.push(rows);
  }
  return out;
}
const energy = (v0, v1, d, g) => { let e = 0; for (let rx = 0; rx < 4; rx++) { const a = v0[rx][d], c = v1[rx][d]; e += a[g * 2] ** 2 + a[g * 2 + 1] ** 2 + c[g * 2] ** 2 + c[g * 2 + 1] ** 2; } return e; };
const maps = full.map(fr => ({ v0: doppler(fr, 0), v1: doppler(fr, 1) }));
const bg = [];
for (let d = 0; d < ND; d++) { bg.push([]); for (let g = 0; g < G; g++) { const arr = maps.map(m => Math.log(energy(m.v0, m.v1, d, g) + 1)).sort((x, y) => x - y); bg[d].push(arr[arr.length >> 1]); } }
// 每帧：近门残差(1..4) / 各候选门残差和 / 人门
console.log('  f    t   near  g1..4  g5..8  g9..12 g13..18 最大门');
for (let f = 0; f < maps.length; f++) {
  const { v0, v1 } = maps[f];
  const band = [0, 0, 0, 0];
  let bestG = 0, bestS = -1;
  for (let g = 1; g <= 18; g++) {
    let s = 0; for (let d = 1; d < ND; d++) s += Math.max(0, Math.log(energy(v0, v1, d, g) + 1) - bg[d][g]);
    if (g <= 4) band[0] += s; else if (g <= 8) band[1] += s; else if (g <= 12) band[2] += s; else band[3] += s;
    if (s > bestS) { bestS = s; bestG = g; }
  }
  console.log(String(f).padStart(3), (f / FPS).toFixed(1).padStart(6), band.map(x => x.toFixed(0).padStart(5)).join(' '), String(bestG).padStart(3), (bestG * 0.176).toFixed(2) + 'm');
}
