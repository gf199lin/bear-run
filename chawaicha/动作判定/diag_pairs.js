// diag_pairs.js — 决定性实验: 首录 28 个虚拟通道对, 哪对有跳/蹲判别力?
// 窗口(grade_game2 实证对时 off≈?): 跳=49.5-52.8 蹲=53.8-55.8 站=56.5-57.8+62.5-63.8 (录音秒)
const fs = require('fs'), path = require('path');
const UP = 'C:/Users/叉歪叉/DeepWorks/uploads/ses_ec02eb2aeffeNQVRmmapAn7kfo';
const REC = 136, G = 32, ND = 32, FPS = 1 / 0.52084;
const RLO = 7, RHI = 18;
const b = fs.readFileSync(path.join(UP, 'radar_raw_1792465947153.bin'));

const cosT = [], sinT = [];
for (let d = 0; d < ND; d++) { cosT.push([]); sinT.push([]); for (let k = 0; k < ND; k++) { const a = -2 * Math.PI * d * k / ND; cosT[d].push(Math.cos(a)); sinT[d].push(Math.sin(a)); } }
const sgn = (d) => d <= 16 ? d : d - 32;
const d2r = (x) => { let v = x * 180 / Math.PI; while (v > 180) v -= 360; while (v < -180) v += 360; return v; };
const med = (xs) => { if (!xs.length) return NaN; const s = [...xs].sort((x, y) => x - y); return s[s.length >> 1]; };

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

const full = parseFrames(b);
console.log(`首录 帧=${full.length}`);
const maps = full.map(fr => ({ v0: doppler(fr, 0), v1: doppler(fr, 1) }));
const bg = [];
for (let d = 0; d < ND; d++) { bg.push([]); for (let g = 0; g < G; g++) bg[d].push(med(maps.map(m => Math.log(energy(m.v0, m.v1, d, g) + 1)))); }

// 逐帧: 残差追踪 + 相干帧全 8 通道相位
const recs = maps.map(({ v0, v1 }, f) => {
  const posres = [];
  for (let d = 0; d < ND; d++) { posres.push([]); for (let g = RLO; g <= RHI; g++) posres[d].push(Math.max(0, Math.log(energy(v0, v1, d, g) + 1) - bg[d][g])); }
  let bestG = -1, bestS = -1;
  for (let g = RLO; g <= RHI; g++) { let s = 0; for (let d = 0; d < ND; d++) s += posres[d][g - RLO]; if (s > bestS) { bestS = s; bestG = g; } }
  let bd = 0, br = -1;
  for (let d = 0; d < ND; d++) if (posres[d][bestG - RLO] > br) { br = posres[d][bestG - RLO]; bd = d; }
  let ph = null;
  if (br > 0.7) {
    ph = [];
    for (let p2 = 0; p2 < 2; p2++) { const vv = p2 ? v1 : v0; for (let rx = 0; rx < 4; rx++) { const a = vv[rx][bd]; ph.push(Math.atan2(a[bestG * 2 + 1], a[bestG * 2])); } }
  }
  return { f, t: f / FPS, ph };
});

const inW = (t0, t1) => recs.filter(r => r.ph && r.t >= t0 && r.t < t1);
const jump = inW(49.5, 52.8), cro = inW(53.8, 55.8), stand = [...inW(56.5, 57.8), ...inW(62.5, 63.8)];
console.log(`相干帧: 跳=${jump.length} 蹲=${cro.length} 站=${stand.length}`);
if (!jump.length || !cro.length || stand.length < 2) { console.log('相干帧不足, 实验失败'); process.exit(1); }

console.log('\n对(i,j)排名: 基准=站立中位, 报告跳/蹲偏移中位 与 窗内离散IQR');
const rank = [];
for (let i = 0; i < 8; i++) for (let j = i + 1; j < 8; j++) {
  const dph = (r) => d2r((r.ph[i] - r.ph[j]) * 180 / Math.PI * Math.PI / 180); // 注意: ph已是弧度, d2r输入按度处理
  const dd = (r) => { let v = (r.ph[i] - r.ph[j]) * 180 / Math.PI; while (v > 180) v -= 360; while (v < -180) v += 360; return v; };
  const bs = med(stand.map(dd));
  const jd = jump.map(r => { let v = dd(r) - bs; while (v > 180) v -= 360; while (v < -180) v += 360; return v; });
  const cd = cro.map(r => { let v = dd(r) - bs; while (v > 180) v -= 360; while (v < -180) v += 360; return v; });
  const jm = med(jd), cm = med(cd);
  const iqr = (xs) => { const s = [...xs].sort((a, b2) => a - b2); return s[Math.floor(s.length * 0.75)] - s[Math.floor(s.length * 0.25)]; };
  const spread = Math.max(iqr(jd), iqr(cd));
  // 判别分: 跳蹲方向相反且都超出离散
  const sep = (Math.abs(jm) + Math.abs(cm)) / (spread + 1);
  rank.push({ p: `${i}-${j}`, jm, cm, spread, sep, jSign: Math.sign(jm) !== Math.sign(cm) });
}
rank.sort((a, b2) => b2.sep - a.sep);
for (const r of rank) console.log(`${r.p}  跳偏=${r.jm.toFixed(0).padStart(4)}° 蹲偏=${r.cm.toFixed(0).padStart(4)}° IQR=${r.spread.toFixed(0).padStart(3)}° 反向=${r.jSign ? '是' : '否'} 判别分=${r.sep.toFixed(2)}`);
