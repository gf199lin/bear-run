// diag_jc2.js — 残差域+质量门复刻 grade_game2, 看 take A/B/C 相干帧上的俯仰信号是否干净
const fs = require('fs'), path = require('path');
const UP = 'C:/Users/叉歪叉/DeepWorks/uploads/ses_ec02eb2aeffeNQVRmmapAn7kfo';
const META = JSON.parse(fs.readFileSync('C:/Users/叉歪叉/DeepWorks/outputs/game-training-data/meta.json', 'utf8'));
const REC = 136, G = 32, ND = 32, FPS = 1 / 0.52084, GATE_M = 0.176;
const P_LO = 3, P_HI = 10;   // 找人区 0.53~1.76m

const SEGS = [];
const SEG = (dur, act, name) => SEGS.push({ dur, act, name });
SEG(4, 'sync_wave', '挥手'); SEG(6, 'idle_calib', '静站');
for (let i = 1; i <= 10; i++) { SEG(1, 'jump', `跳${i}`); SEG(2, 'idle', ''); }
SEG(4, 'idle_rest', '休息');
for (let i = 1; i <= 10; i++) { SEG(2, 'crouch', `蹲${i}`); SEG(2, 'idle', ''); }
SEG(4, 'idle_rest', '休息');
SEG(6, 'neg_sway', '负·晃'); SEG(6, 'neg_step', '负·踏步'); SEG(4, 'neg_bend', '负·弯腰'); SEG(5, 'neg_arms', '负·挥臂');
['jump', 'crouch', 'jump', 'crouch', 'jump', 'crouch'].forEach((a, i) => SEG(3, a, `连招${i + 1}·${a === 'jump' ? '跳' : '蹲'}`));
SEG(4, 'idle_end', '收尾');
let acc = 0; SEGS.forEach(s => { s.t0 = acc; s.t1 = acc + s.dur; acc += s.dur; });

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
function doppler(b, fr, parity) {
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

const takeId = process.argv[2] || 'A';
let tk, b;
if (takeId === '0') {   // 首录: 无剧本, off=0, 只打印
  tk = { file: 'radar_raw_1792465947153.bin', offset_s: 0 };
} else tk = META.takes[takeId];
b = fs.readFileSync(path.join(UP, tk.file));
const full = parseFrames(b);
const maps = full.map(fr => ({ v0: doppler(b, fr, 0), v1: doppler(b, fr, 1) }));
console.log(`take ${takeId} 帧=${full.length} off=${tk.offset_s}`);

// 背景=全文件中位数(离线诊断用; 实时版改滚动中位)
const logE = maps.map(({ v0, v1 }) => { const m2 = []; for (let d = 0; d < ND; d++) { m2.push([]); for (let g = 0; g < G; g++) m2[d].push(Math.log(energy(v0, v1, d, g) + 1)); } return m2; });
// 帧增益估计: 全图(d≥1, 排除bin0静态大头)中位数
const frameMed = logE.map(m2 => { const all = []; for (let d = 1; d < ND; d++) for (let g = 0; g < G; g++) all.push(m2[d][g]); return med(all); });
const normE = logE.map((m2, f) => m2.map(row => row.map(v => v - frameMed[f])));
const bg = [], bgN = [];
for (let d = 0; d < ND; d++) { bg.push([]); bgN.push([]); for (let g = 0; g < G; g++) {
  bg[d].push(med(logE.map(m2 => m2[d][g])));
  bgN[d].push(med(normE.map(m2 => m2[d][g])));
} }

const rows = maps.map(({ v0, v1 }, f) => {
  const posres = [];
  for (let d = 0; d < ND; d++) { posres.push([]); for (let g = P_LO; g <= P_HI; g++) posres[d].push(Math.max(0, Math.log(energy(v0, v1, d, g) + 1) - bg[d][g])); }
  let bestG = -1, bestS = -1;
  for (let g = P_LO; g <= P_HI; g++) { let s = 0; for (let d = 0; d < ND; d++) s += posres[d][g - P_LO]; if (s > bestS) { bestS = s; bestG = g; } }
  let bd = 0, br = -1;
  for (let d = 0; d < ND; d++) if (posres[d][bestG - P_LO] > br) { br = posres[d][bestG - P_LO]; bd = d; }
  let el = null;
  if (br > 0.7) {
    const ph = [];
    for (let p2 = 0; p2 < 2; p2++) { const vv = p2 ? v1 : v0; for (let rx = 0; rx < 4; rx++) { const a = vv[rx][bd]; ph.push(Math.atan2(a[bestG * 2 + 1], a[bestG * 2])); } }
    el = [d2r(ph[5] - ph[2]), d2r(ph[2] - ph[3])];
  }
  // 活动量=人区全部正残差和; 展布=bestG 上残差>0.4 的 bin 数; 落地冲击=高速bin(|sgn|>=6)残差和
  let act = 0;
  for (let d = 0; d < ND; d++) for (let g = P_LO; g <= P_HI; g++) act += posres[d][g - P_LO];
  const sprd = []; for (let d = 0; d < ND; d++) if (posres[d][bestG - P_LO] > 0.4) sprd.push(sgn(d));
  let impact = 0;
  for (let d = 0; d < ND; d++) if (Math.abs(sgn(d)) >= 6) for (let g = P_LO; g <= P_HI; g++) impact += posres[d][g - P_LO];
  // 增益归一版活动量
  let actN = 0;
  for (let d = 1; d < ND; d++) for (let g = P_LO; g <= P_HI; g++) actN += Math.max(0, normE[f][d][g] - bgN[d][g]);
  return { f, t: f / FPS, g: bestG, d: sgn(bd), br, el, sprd: sprd.length, act, impact, actN, gm: frameMed[f] };
});

// 打印相干帧
console.log('\n  f  剧本t   段      门    db   残差  展布  活动量  归一活动  俯仰52/23');
for (const r of rows) {
  const t = r.t - tk.offset_s;
  const sg = SEGS.find(s => t >= s.t0 && t < s.t1);
  const nm = sg ? (sg.name || sg.act) : '片外';
  console.log(String(r.f).padStart(3), t.toFixed(1).padStart(6), nm.padEnd(6), String(r.g).padStart(2), String(r.d).padStart(3), r.br.toFixed(2).padStart(5), String(r.sprd).padStart(3), r.act.toFixed(1).padStart(6), r.actN.toFixed(1).padStart(6), (r.el ? r.el.map(x => String(x.toFixed(0)).padStart(4)).join('/') : '-'));
}
// 汇总: 动作窗内相干帧 el1 统计
console.log('\n动作窗汇总(el1 = dPhi(2-3)):');
const base = med(rows.filter(r => { const t = r.t - tk.offset_s; return r.el && t >= 6 && t < 10; }).map(r => r.el[1]));
console.log(`基准(静站段相干帧中位) = ${isNaN(base) ? 'NaN(静站无相干帧!)' : base.toFixed(0) + '°'}`);
for (const s of SEGS) {
  if (s.act !== 'jump' && s.act !== 'crouch') continue;
  const w = rows.filter(r => { const t = r.t - tk.offset_s; return r.el && t >= s.t0 - 0.3 && t < s.t1 + 1.2; });
  if (!w.length) { console.log(`${s.name.padEnd(6)} 无相干帧`); continue; }
  const devs = w.map(r => d2r((r.el[1] - base) * Math.PI / 180));
  console.log(`${s.name.padEnd(6)} n=${w.length} dev1: ${devs.map(x => String(x.toFixed(0)).padStart(4)).join(' ')}`);
}
