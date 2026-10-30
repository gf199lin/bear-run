// analyze_v2.js — 干净采集数据的特征判别力分析器
// 用法: node analyze_v2.js <radar_raw.bin> <radar_sync.json>
// 输出: 每类剧本段(跳/蹲/负样本/静站/连招)在三类特征上的表现 + 逐次一致性 + 可分性评分
// 特征族: ①活动量包络(残差和, 人门区) ②多普勒展布(>0.4的bin数) ③落地冲击(|d|>=6残差和)
// 假设检验: 跳=双峰夹"空中静默"(act 低谷); 蹲=两个中等峰间隔>=1.2s; 负样本/静站=无结构
const fs = require('fs');
const REC = 136, G = 32, ND = 32, FPS = 1 / 0.52084, GATE_M = 0.176;

// ---- 剧本(与 dev113p-prompter.html PHASES_JC 一致) ----
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

const [,, binPath, syncPath] = process.argv;
if (!binPath || !syncPath) { console.log('用法: node analyze_v2.js <radar_raw.bin> <radar_sync.json>'); process.exit(1); }
const b = fs.readFileSync(binPath);
const sync = JSON.parse(fs.readFileSync(syncPath, 'utf8'));
let offS = sync.offS;
console.log(`bin=${binPath}\nsync offS=${offS == null ? 'null(未对时!)' : offS.toFixed(2) + 's'}`);
if (offS == null) { console.log('同步文件缺 offS, 无法用精确对时。请用「跳蹲采集(带同步)」重录。'); process.exit(1); }

// ---- 解析与多普勒(与 diag 系列同源) ----
const cosT = [], sinT = [];
for (let d = 0; d < ND; d++) { cosT.push([]); sinT.push([]); for (let k = 0; k < ND; k++) { const a = -2 * Math.PI * d * k / ND; cosT[d].push(Math.cos(a)); sinT[d].push(Math.sin(a)); } }
const sgn = (d) => d <= 16 ? d : d - 32;
const med = (xs) => { if (!xs.length) return NaN; const s = [...xs].sort((x, y) => x - y); return s[s.length >> 1]; };
const iqr = (xs) => { if (xs.length < 4) return NaN; const s = [...xs].sort((x, y) => x - y); return s[Math.floor(s.length * 0.75)] - s[Math.floor(s.length * 0.25)]; };

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
const staticAmp = (fr, g) => { let s = 0; for (let rx = 0; rx < 4; rx++) { const off = fr.d.get(0)[rx]; s += Math.hypot(b.readInt16LE(off + g * 4), b.readInt16LE(off + g * 4 + 2)); } return s / 4; };

const full = parseFrames(b);
console.log(`帧数=${full.length} 时长=${(full.length / FPS).toFixed(1)}s`);
if (full.length / FPS < 100) console.log('⚠ 录制偏短, 剧本 67s+收尾, 应≥75s');

// ---- 找人: idle_calib 段(剧本 4~10s) bin0 静态峰, 人应在 0.7~1.4m(门4..8) ----
const calibF0 = Math.round((offS + 4) * FPS), calibF1 = Math.round((offS + 10) * FPS);
let personG = -1, personE = -1;
for (let g = 4; g <= 8; g++) {
  let e = 0; for (let f = calibF0; f < Math.min(calibF1, full.length); f++) e += staticAmp(full[f], g);
  if (e > personE) { personE = e; personG = g; }
}
const ZLO = Math.max(2, personG - 2), ZHI = Math.min(15, personG + 3);
console.log(`人门=${personG} (${(personG * GATE_M).toFixed(2)}m) 分析门区=${ZLO}..${ZHI}`);

// ---- 逐帧特征 ----
const maps = full.map(fr => ({ v0: doppler(fr, 0), v1: doppler(fr, 1) }));
const bg = [];
for (let d = 0; d < ND; d++) { bg.push([]); for (let g = 0; g < G; g++) bg[d].push(med(maps.map(m => Math.log(energy(m.v0, m.v1, d, g) + 1)))); }
const F = maps.map(({ v0, v1 }, f) => {
  let act = 0, impact = 0, sprd = 0, bestD = 0, bestR = -1;
  const pr = [];
  for (let d = 0; d < ND; d++) { pr.push([]); for (let g = ZLO; g <= ZHI; g++) { const r = Math.max(0, Math.log(energy(v0, v1, d, g) + 1) - bg[d][g]); pr[d].push(r); act += r; if (r > 0.4) sprd++; if (r > bestR) { bestR = r; bestD = d; } if (Math.abs(sgn(d)) >= 6) impact += r; } }
  return { t: f / FPS - offS, act, sprd, impact, db: sgn(bestD), br: bestR };
});
console.log(`特征完成: act基线(全片中位)=${med(F.map(r => r.act)).toFixed(1)}`);

// ---- 逐段统计 ----
const inSeg = (s, pad0 = 0.3, pad1 = 0.8) => F.filter(r => r.t >= s.t0 - pad0 && r.t < s.t1 + pad1);
const idleFrames = F.filter(r => { const s = SEGS.find(x => r.t >= x.t0 && r.t < x.t1); return s && (s.act.startsWith('idle')); });
const idleAct = idleFrames.map(r => r.act), idleSprd = idleFrames.map(r => r.sprd), idleImp = idleFrames.map(r => r.impact);
console.log(`静站基线: act=${med(idleAct).toFixed(1)}±${iqr(idleAct).toFixed(1)} sprd=${med(idleSprd).toFixed(0)}±${iqr(idleSprd).toFixed(0)} impact=${med(idleImp).toFixed(1)}±${iqr(idleImp).toFixed(1)} (n=${idleFrames.length})`);

const segStat = (act, pad0, pad1) => SEGS.filter(s => s.act === act).map(s => {
  const rows = inSeg(s, pad0, pad1);
  const acts = rows.map(r => r.act), sprds = rows.map(r => r.sprd), imps = rows.map(r => r.impact);
  // 结构签名: 最大act, 最大impact, act最大值前是否有低谷(空中静默检测)
  const pk = Math.max(...acts); const pkI = acts.indexOf(pk);
  const quietBefore = pkI > 0 ? Math.min(...acts.slice(0, pkI)) : NaN;
  const quietAfter = pkI < acts.length - 1 ? Math.min(...acts.slice(pkI + 1)) : NaN;
  return { name: s.name, n: rows.length, actPk: pk, actMed: med(acts), sprdPk: Math.max(...sprds), impPk: Math.max(...imps), quiet: Math.min(quietBefore ?? 1e9, quietAfter ?? 1e9) };
});

console.log('\n=== 跳(10次, 窗-0.3~+0.8s) ===');
const J = segStat('jump');
for (const r of J) console.log(`${r.name.padEnd(4)} n=${r.n} act峰值=${r.actPk.toFixed(1)} act中位=${r.actMed.toFixed(1)} sprd峰=${r.sprdPk} impact峰=${r.impPk.toFixed(1)} 峰旁低谷=${r.quiet.toFixed(1)}`);
console.log(`跳汇总: actPk中位=${med(J.map(r => r.actPk)).toFixed(1)} impPk中位=${med(J.map(r => r.impPk)).toFixed(1)} quiet中位=${med(J.map(r => r.quiet)).toFixed(1)}`);

console.log('\n=== 蹲(10次, 窗-0.3~+0.8s) ===');
const C = segStat('crouch');
for (const r of C) console.log(`${r.name.padEnd(4)} n=${r.n} act峰值=${r.actPk.toFixed(1)} act中位=${r.actMed.toFixed(1)} sprd峰=${r.sprdPk} impact峰=${r.impPk.toFixed(1)} 峰旁低谷=${r.quiet.toFixed(1)}`);
console.log(`蹲汇总: actPk中位=${med(C.map(r => r.actPk)).toFixed(1)} impPk中位=${med(C.map(r => r.impPk)).toFixed(1)} quiet中位=${med(C.map(r => r.quiet)).toFixed(1)}`);

console.log('\n=== 负样本(窗全程) ===');
for (const a of ['neg_sway', 'neg_step', 'neg_bend', 'neg_arms']) {
  const s = SEGS.find(x => x.act === a);
  const rows = inSeg(s, 0, 0);
  console.log(`${s.name.padEnd(6)} n=${rows.length} act峰=${Math.max(...rows.map(r => r.act)).toFixed(1)} act中位=${med(rows.map(r => r.act)).toFixed(1)} imp峰=${Math.max(...rows.map(r => r.impact)).toFixed(1)}`);
}

// ---- 可分性评分: (动作中位 - 静站中位) / 静站IQR ----
console.log('\n=== 可分性(>2 可用, >4 优秀) ===');
const sep = (name, actionVals, idleVals) => console.log(`${name.padEnd(12)} 跳=${((med(J.map(actionVals)) - med(idleVals)) / (iqr(idleVals) + 0.01)).toFixed(2).padStart(6)}  蹲=${((med(C.map(actionVals)) - med(idleVals)) / (iqr(idleVals) + 0.01)).toFixed(2).padStart(6)}`);
sep('actPk', r => r.actPk, idleAct);
sep('impPk', r => r.impPk, idleImp);
sep('sprdPk', r => r.sprdPk, idleSprd);
console.log('\n跳vs蹲互分(同特征中位差/合并IQR, >1.5 可用):');
const jSep = (vals) => { const jv = J.map(vals), cv = C.map(vals); const pool = [...jv, ...cv]; return ((med(jv) - med(cv)) / (iqr(pool) + 0.01)).toFixed(2); };
console.log(`actPk: ${jSep(r => r.actPk)}  impPk: ${jSep(r => r.impPk)}  sprdPk: ${jSep(r => r.sprdPk)}  quiet: ${jSep(r => r.quiet)}`);
