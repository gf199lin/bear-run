// test_jcdet.js — 从 radar-pointcloud.html 抽取 JCDet 同一份代码, 回放三组实采+首录, 验证跳/蹲状态机
// 用法: node test_jcdet.js [--thresh on,off,spike]
const fs = require('fs'), path = require('path');
const ROOT = 'C:/Users/叉歪叉/DeepWorks';
const UP = path.join(ROOT, 'uploads/ses_ec02eb2aeffeNQVRmmapAn7kfo');
const HTML = path.join(ROOT, 'outputs/radar-pointcloud/radar-pointcloud.html');
const META = path.join(ROOT, 'outputs/game-training-data/meta.json');

// ---- 1. 抽取 JCDet 代码(测的=发布的) ----
const src = fs.readFileSync(HTML, 'utf8');
const m = src.match(/\/\*JCDET-BEGIN\*\/([\s\S]*?)\/\*JCDET-END\*\//);
if (!m) { console.error('FAIL: html 中找不到 JCDET 标记'); process.exit(1); }
const JCDet = eval(m[1] + '\n;JCDet;');

// 阈值覆盖(调参用)
const arg = process.argv.find(a => a.startsWith('--thresh='));
if (arg) { const [a, b, c] = arg.slice(9).split(',').map(Number); JCDet.TH_ON = a; JCDet.TH_OFF = b; JCDet.TH_SPIKE = c; console.log(`阈值覆盖: ON=${a} OFF=${b} SPIKE=${c}`); }

// ---- 2. #jc 剧本(与 extract_jc.js 一致) ----
const SEGS = [];
const SEG = (dur, act, name) => SEGS.push({ dur, act, name });
SEG(4, 'sync_wave', '对时挥手'); SEG(6, 'idle_calib', '校准静止');
for (let i = 1; i <= 10; i++) { SEG(1, 'jump', `跳${i}`); SEG(2, 'idle', `站稳`); }
SEG(4, 'idle_rest', '休息');
for (let i = 1; i <= 10; i++) { SEG(2, 'crouch', `蹲${i}`); SEG(2, 'idle', `起`); }
SEG(4, 'idle_rest', '休息');
SEG(6, 'neg_sway', '负·晃'); SEG(6, 'neg_step', '负·踏步'); SEG(4, 'neg_bend', '负·弯腰'); SEG(5, 'neg_arms', '负·挥臂');
['jump', 'crouch', 'jump', 'crouch', 'jump', 'crouch'].forEach((a, i) => SEG(3, a, `连招${i + 1}`));
SEG(4, 'idle_end', '收尾');
let acc = 0; const cnt = { jump: 0, crouch: 0 };
SEGS.forEach(s => { s.t0 = acc; s.t1 = acc + s.dur; acc += s.dur; s.sid = (s.act === 'jump' || s.act === 'crouch') ? `${s.act}_${++cnt[s.act]}` : '-'; });

// ---- 3. 解析 136B 记录流 → 逐包喂 JCDet(与页面 chip-packet 同构) ----
const REC = 136;
function feedFile(file) {
  const b = fs.readFileSync(file);
  const sig = (p) => p + REC <= b.length && b[p] === 0xAA && (b[p + 1] & 0xF0) === 0x80 && b[p + 3] === 0x1F;
  let pos = 0, lastCh = -1, grp = 0, nRec = 0, nDrop = 0;
  const devSeries = [];
  JCDet.reset(); JCDet.nJump = 0; JCDet.nCrouch = 0; JCDet.frames = 0;
  const events = [];
  JCDet.onEvent = (ev) => events.push(ev);
  JCDet.onState = (s) => { if (s.state === 'ready') devSeries.push({ f: s.frames, dev1: s.dev1 }); };
  while (pos + REC <= b.length) {
    if (!sig(pos)) { let q = pos + 1; while (q + REC <= b.length && !sig(q)) q++; if (q + REC > b.length) break; pos = q; continue; }
    const chirp = ((b[pos + 1] & 3) << 4) | (b[pos + 2] >>> 4);
    if (chirp !== lastCh) { lastCh = chirp; grp = 0; }
    const rx = grp++; nRec++;
    if (rx > 3) { nDrop++; pos += REC; continue; }
    const I = [], Q = [];
    for (let g = 0; g < 32; g++) { I.push(b.readInt16LE(pos + 4 + g * 4)); Q.push(b.readInt16LE(pos + 4 + g * 4 + 2)); }
    JCDet.push({ idx: chirp, rx, payload: { I, Q, n: 32 } });
    pos += REC;
  }
  JCDet.flush();
  return { events, devSeries, nRec, nDrop, frames: JCDet.frames, personG: JCDet.personG, personM: JCDet.personM, elBase: JCDet.elBase.map(x => +x.toFixed(1)) };
}

// ---- 4. 计分 ----
function score(takeId, r, off) {
  const ts = (f) => f * 0.52084 - off;
  const winOf = (t) => SEGS.find(s => t >= s.t0 - 0.4 && t < s.t1 + 1.6);
  const segAt = (t) => SEGS.find(s => t >= s.t0 && t < s.t1);
  const got = { jump: new Map(), crouch: new Map() }; const fa = [];
  for (const ev of r.events) {
    const t = ts(ev.f), w = winOf(t), sg = segAt(t);
    const tag = `${ev.type}@录音${(ev.f * 0.52084).toFixed(1)}s/剧本${t.toFixed(1)}s[${sg ? sg.name : (t < 0 ? '片头' : '片尾')}]peak=${ev.peak ?? ''}`;
    const kind = ev.type === 'jump' ? 'jump' : (ev.type === 'crouch_start' ? 'crouch' : null);
    if (kind && w && w.act === kind) {
      if (got[kind].has(w.sid)) fa.push('重复! ' + tag);
      else got[kind].set(w.sid, tag);
    } else if (ev.type === 'crouch_end') { /* 结束事件不计分 */ }
    else fa.push('误报! ' + tag);
  }
  const jm = SEGS.filter(s => s.sid.startsWith('jump_') && !got.jump.has(s.sid)).map(s => s.sid);
  const cm = SEGS.filter(s => s.sid.startsWith('crouch_') && !got.crouch.has(s.sid)).map(s => s.sid);
  console.log(`\n=== take ${takeId} 帧=${r.frames} 人门=${r.personG}(${r.personM}m) 基准=[${r.elBase}] ===`);
  console.log(`跳 命中 ${got.jump.size}/13${jm.length ? ' 漏:' + jm.join(',') : ''}`);
  console.log(`蹲 命中 ${got.crouch.size}/13${cm.length ? ' 漏:' + cm.join(',') : ''}`);
  console.log(`误报 ${fa.length} 次${fa.length ? ':\n  ' + fa.join('\n  ') : ''}`);
  return { j: got.jump.size, c: got.crouch.size, fa: fa.length };
}

const meta = JSON.parse(fs.readFileSync(META, 'utf8'));

// ---- 4.5 波形打印: --trace=A:0,140 逐帧 dev1 + 剧本段 ----
const trArg = process.argv.find(a => a.startsWith('--trace='));
if (trArg) {
  const [tid, range] = trArg.slice(8).split(':');
  const [f0, f1] = range.split(',').map(Number);
  const tk = meta.takes[tid];
  const r = feedFile(path.join(UP, tk.file));
  console.log(`take ${tid} off=${tk.offset_s} 人门=${r.personG}`);
  for (const d of r.devSeries) {
    if (d.f < f0 || d.f > f1) continue;
    const t = d.f * 0.52084 - tk.offset_s;
    const sg = SEGS.find(s => t >= s.t0 && t < s.t1);
    const mark = sg && (sg.act === 'jump' || sg.act === 'crouch') ? ' <<<' + sg.name : (sg ? ' ' + sg.name : '');
    console.log(`f${String(d.f).padStart(3)} 剧本${t.toFixed(2).padStart(7)}s dev1=${String(d.dev1).padStart(4)}°${mark}`);
  }
  process.exit(0);
}

// ---- 5. 跑三组(用 meta.json 的对时偏移) + 首录(无标签只看计数) ----
let J = 0, C = 0, FA = 0;
for (const id of ['A', 'B', 'C']) {
  const tk = meta.takes[id];
  const r = feedFile(path.join(UP, tk.file));
  const s = score(id, r, tk.offset_s);
  J += s.j; C += s.c; FA += s.fa;
}
console.log(`\n==== 总计: 跳 ${J}/39 蹲 ${C}/39 误报 ${FA} ====`);

const r0 = feedFile(path.join(UP, 'radar_raw_1792465947153.bin'));
console.log(`\n=== 首录(无标签): 帧=${r0.frames} 人门=${r0.personG}(${r0.personM}m) 事件=${r0.events.length} ====`);
console.log(r0.events.map(e => `  ${e.type}@${e.t}s peak=${e.peak ?? ''}`).join('\n'));
