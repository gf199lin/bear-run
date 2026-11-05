// diag_static.js — 首录静态(bin0)距离剖面: 跳/蹲/站期间人所在门区形状是否变化
const fs = require('fs'), path = require('path');
const UP = 'C:/Users/叉歪叉/DeepWorks/uploads/ses_ec02eb2aeffeNQVRmmapAn7kfo';
const REC = 136, G = 32, FPS = 1 / 0.52084;
const b = fs.readFileSync(path.join(UP, 'radar_raw_1792465947153.bin'));

function parseFrames(b) {
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
  return frames.filter(fr => fr.n >= 240);
}
const full = parseFrames(b);
// bin0 静态能量剖面: 4rx 平均幅度 (chirp0)
function staticProfile(fr) {
  const prof = [];
  for (let g = 0; g < G; g++) {
    let s = 0;
    for (let rx = 0; rx < 4; rx++) {
      const off = fr.d.get(0)[rx];
      const I = b.readInt16LE(off + g * 4), Q = b.readInt16LE(off + g * 4 + 2);
      s += Math.hypot(I, Q);
    }
    prof.push(s / 4);
  }
  return prof;
}
const WIN = [[49.5, 52.8, '跳'], [53.8, 55.8, '蹲'], [56.5, 57.8, '站1'], [62.5, 63.8, '站2']];
console.log('帧  秒     窗   质心门  剖面(门7..18, 千)');
for (let f = 88; f <= 130 && f < full.length; f++) {
  const t = f / FPS;
  const w = WIN.find(w2 => t >= w2[0] && t < w2[1]);
  const prof = staticProfile(full[f]);
  // 人区质心(门8..16)
  let num = 0, den = 0;
  for (let g = 8; g <= 16; g++) { num += g * prof[g]; den += prof[g]; }
  const cen = den > 0 ? num / den : 0;
  const cells = [];
  for (let g = 7; g <= 18; g++) cells.push((prof[g] / 1000).toFixed(0).padStart(4));
  console.log(String(f).padStart(3), t.toFixed(1).padStart(6), (w ? w[2] : '·').padEnd(3), cen.toFixed(2).padStart(6), cells.join(''));
}
