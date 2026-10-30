// extract_jc.js v2 — 跳蹲采集三组数据 → 训练用 JSONL
// v2 修正：静止人不靠残差追踪（静止人会被背景中位数吞掉），改用 bin0 静态能量定人门；
//         对时不靠挥手（近门被人体常驻残差淹没），改用剧本模板与俯仰相位信号互相关。
const fs = require('fs'), path = require('path');
const UP = 'C:/Users/叉歪叉/DeepWorks/uploads/ses_ec02eb2aeffeNQVRmmapAn7kfo';
const OUT = 'C:/Users/叉歪叉/DeepWorks/outputs/game-training-data';
fs.mkdirSync(OUT, { recursive: true });
const TAKES = [
  { id: 'A', file: 'radar_raw_1792576640965.bin' },
  { id: 'B', file: 'radar_raw_1792576891518.bin', note: '采集时雷达板有轻微倾斜(用户报备)' },
  { id: 'C', file: 'radar_raw_1792577686397.bin' },
];
const REC = 136, G = 32, ND = 32, FPS = 1 / 0.52084, GATE_M = 0.176;

// ---- #jc 剧本段定义（与提词器 PHASES_JC 一致）----
const SEGS = [];
const SEG = (dur, act, name) => SEGS.push({ dur, act, name });
SEG(4, 'sync_wave', '第 0 段 · 对时挥手'); SEG(6, 'idle_calib', '第 1 段 · 校准静止');
for (let i = 1; i <= 10; i++) { SEG(1, 'jump', `跳 ${i}/10`); SEG(2, 'idle', `跳 ${i}/10 · 站稳`); }
SEG(4, 'idle_rest', '休息 · 站立');
for (let i = 1; i <= 10; i++) { SEG(2, 'crouch', `蹲 ${i}/10`); SEG(2, 'idle', `蹲 ${i}/10 · 起`); }
SEG(4, 'idle_rest', '休息 · 站立');
SEG(6, 'neg_sway', '负样本 · 自然晃'); SEG(6, 'neg_step', '负样本 · 原地踏步');
SEG(4, 'neg_bend', '负样本 · 弯腰'); SEG(5, 'neg_arms', '负样本 · 挥臂扭腰');
['jump', 'crouch', 'jump', 'crouch', 'jump', 'crouch'].forEach((a, i) => SEG(3, a, `连招 ${i + 1}/6`));
SEG(4, 'idle_end', '收尾 · 静止');
let acc = 0; const jc = { jump: 0, crouch: 0 };
SEGS.forEach(s => { s.t0 = acc; s.t1 = acc + s.dur; acc += s.dur; s.sid = (s.act === 'jump' || s.act === 'crouch') ? `${s.act}_${++jc[s.act]}` : '-'; });
const SCRIPT_DUR = acc;

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

const meta = { script: 'jc', script_dur_s: SCRIPT_DUR, gate_m: GATE_M, fps: +FPS.toFixed(3), frame_period_s: 0.52084,
  segments: SEGS.map((s, i) => ({ seg: i, name: s.name, act: s.act, sid: s.sid, t0: +s.t0.toFixed(1), t1: +s.t1.toFixed(1) })), takes: {} };

for (const tk of TAKES) {
  const b = fs.readFileSync(path.join(UP, tk.file));
  const full = parseFrames(b);
  console.log(`\n=== take ${tk.id} 完整帧=${full.length} 时长=${(full.length / FPS).toFixed(1)}s ===`);
  const maps = full.map(fr => ({ v0: doppler(b, fr, 0), v1: doppler(b, fr, 1) }));

  // ---- 人门：bin0(零速)静态能量中位数剖面，0.7~1.6m(门4..9) 内取峰 ----
  const statProf = [];
  for (let g = 0; g < G; g++) statProf.push(med(maps.map(m => Math.log(energy(m.v0, m.v1, 0, g) + 1))));
  let personG = 4; for (let g = 4; g <= 9; g++) if (statProf[g] > statProf[personG]) personG = g;
  console.log(`静态剖面峰: 门${personG}=${(personG * GATE_M).toFixed(2)}m  剖面[门3..12]=${statProf.slice(3, 13).map((x, i) => `${i + 3}:${x.toFixed(1)}`).join(' ')}`);

  // ---- 逐帧：在人门上取最大能量 bin 的相位 + 剖面特征 ----
  const recs = maps.map(({ v0, v1 }, f) => {
    let bd = 0, be = -1;
    for (let d = 0; d < ND; d++) { const e = energy(v0, v1, d, personG); if (e > be) { be = e; bd = d; } }
    const ph = [];
    for (let p2 = 0; p2 < 2; p2++) { const vv = p2 ? v1 : v0; for (let rx = 0; rx < 4; rx++) { const a = vv[rx][bd]; ph.push(Math.atan2(a[personG * 2 + 1], a[personG * 2])); } }
    const el = [d2r(ph[5] - ph[2]), d2r(ph[2] - ph[3])];
    const az = [d2r(ph[1] - ph[0]), d2r(ph[2] - ph[1]), d2r(ph[7] - ph[2])];
    // 人门邻域(±1门)非零速能量 = 动作活跃度
    let act = 0, tot = 0;
    for (let g = Math.max(0, personG - 1); g <= Math.min(G - 1, personG + 1); g++) {
      let e0 = 0; for (let d = 0; d < ND; d++) { const e = energy(v0, v1, d, g); tot += e; if (d !== 0 && sgn(d) !== 0) e0 += e; }
      act += e0;
    }
    const rhi = []; for (let g = 0; g < G; g++) { let e = 0; for (let d = 0; d < ND; d++) e += energy(v0, v1, d, g); rhi.push(+Math.log(e + 1).toFixed(2)); }
    const dop = []; for (let d = 0; d < ND; d++) dop.push(+Math.log(energy(v0, v1, d, personG) + 1).toFixed(2));
    return { f, t: f / FPS, el, az, db: sgn(bd), actR: +(act / (tot + 1)).toFixed(3), rhi, dop };
  });

  // ---- 俯仰偏差序列（基准=全文件中位数，动作稀疏中位数稳健）----
  const elBase = [med(recs.map(r => r.el[0])), med(recs.map(r => r.el[1]))];
  for (const r of recs) { r.dev = [d2r((r.el[0] - elBase[0]) * Math.PI / 180), d2r((r.el[1] - elBase[1]) * Math.PI / 180)]; }

  // ---- 模板互相关找对时 off 与蹲方向符号 ----
  // 模板：跳段(t0..t0+1.5)=-1，蹲段(t0+0.3..t1+0.3)=+1，其余=0；与 clip(dev1) 相关
  const tmpl = (ts) => { let v = 0; for (const s of SEGS) { if (s.act === 'jump' && ts >= s.t0 - 0.2 && ts < s.t0 + 1.5) v = -1; if (s.act === 'crouch' && ts >= s.t0 + 0.3 && ts < s.t1 + 0.3) v = 1; } return v; };
  let best = { sc: -1e9, off: 0, sign: 1 };
  for (let off = 0; off <= 7.4; off += 0.26) {
    for (const sign of [1, -1]) {
      let sc = 0, n = 0;
      for (const r of recs) { const v = tmpl(r.t - off); if (!v) continue; n++; sc += v * sign * Math.max(-45, Math.min(45, r.dev[1])) / 45; }
      sc /= (n || 1);
      if (sc > best.sc) best = { sc, off, sign };
    }
  }
  const off = best.off, downSign = best.sign;
  console.log(`对时: off=${off.toFixed(2)}s 相关分=${best.sc.toFixed(3)} 蹲方向符号=${downSign > 0 ? '+' : '-'} 剧本t=录音t-off`);
  const st = (r) => r.t - off;

  // ---- 质检：跳/蹲逐次 ----
  let jumpOK = 0, jumpTot = 0, croOK = 0, croTot = 0; const repDetail = [];
  for (const s of SEGS) {
    if (s.act === 'jump') {
      jumpTot++;
      const w = recs.filter(r => st(r) >= s.t0 - 0.3 && st(r) < s.t1 + 1.0);
      const mn = w.length ? Math.min(...w.map(r => r.dev[1] * downSign)) : NaN;
      const ok = !isNaN(mn) && mn < -18; if (ok) jumpOK++;
      repDetail.push(`${s.sid}:${ok ? '✅' : '❌'}(${isNaN(mn) ? '-' : mn.toFixed(0)}°)`);
    } else if (s.act === 'crouch') {
      croTot++;
      const w = recs.filter(r => st(r) >= s.t0 + 0.3 && st(r) < s.t1 + 0.3);
      const md = w.length ? med(w.map(r => r.dev[1] * downSign)) : NaN;
      const ok = !isNaN(md) && md > 12; if (ok) croOK++;
      repDetail.push(`${s.sid}:${ok ? '✅' : '❌'}(${isNaN(md) ? '-' : md.toFixed(0)}°)`);
    }
  }
  const cover = recs.some(r => st(r) >= SCRIPT_DUR - 1);
  console.log(`质检: 跳=${jumpOK}/${jumpTot} 蹲=${croOK}/${croTot} 剧本覆盖=${cover ? '✅' : '⚠️不足'}`);
  console.log(repDetail.join(' '));

  // ---- 写 JSONL ----
  const lines = recs.map(r => {
    const ts = st(r);
    const si = SEGS.findIndex(s => ts >= s.t0 && ts < s.t1);
    const seg = si >= 0 ? SEGS[si] : null;
    return JSON.stringify({
      take: tk.id, f: r.f, t: +r.t.toFixed(2), ts: +ts.toFixed(2),
      seg: si, label: seg ? seg.act : (ts < 0 ? 'pre' : 'post'), sid: seg ? seg.sid : '-',
      g: personG, r_m: +(personG * GATE_M).toFixed(2), db: r.db, act: r.actR,
      el: r.el.map(x => +x.toFixed(1)), az: r.az.map(x => +x.toFixed(1)),
      dev: [+(r.dev[0] * downSign).toFixed(1), +(r.dev[1] * downSign).toFixed(1)],
      rhi: r.rhi, dop: r.dop,
    });
  });
  fs.writeFileSync(path.join(OUT, `features_take${tk.id}.jsonl`), lines.join('\n') + '\n');
  meta.takes[tk.id] = { file: tk.file, frames: full.length, dur_s: +(full.length / FPS).toFixed(1), offset_s: +off.toFixed(2), offset_method: 'template_correlation', offset_score: +best.sc.toFixed(3), person_gate: personG, person_m: +(personG * GATE_M).toFixed(2), el_base: elBase.map(x => +x.toFixed(1)), dev_sign: 'dev已符号归一化：蹲≈正，跳≈负', qc: { jump: `${jumpOK}/${jumpTot}`, crouch: `${croOK}/${croTot}`, cover }, note: tk.note || null };
}

let csv = 'seg,name,action,sample_id,t_start_s,t_end_s\n';
SEGS.forEach((s, i) => { csv += `${i},"${s.name}",${s.act},${s.sid},${s.t0.toFixed(1)},${s.t1.toFixed(1)}\n`; });
fs.writeFileSync(path.join(OUT, 'script_labels.csv'), '﻿' + csv);
fs.writeFileSync(path.join(OUT, 'meta.json'), JSON.stringify(meta, null, 1));
console.log('\n已写: features_take{A,B,C}.jsonl, script_labels.csv, meta.json');
