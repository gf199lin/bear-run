// 一次性工具: 从组员的 radar-pointcloud.html 抽出内核脚本, 生成 radar-core.js
const fs = require('fs');
const src = process.argv[2];
const dst = process.argv[3];
const html = fs.readFileSync(src, 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) throw new Error('未找到 <script>');
const body = m[1];
const header = `
/* ============================================================
 * radar-core.js —— 雷达内核（从组员的 radar-pointcloud.html 抽取）
 * 来源: radar-pointcloud.html 的内联 <script>（未做逻辑改动）
 * 用途: 让 Node 端复用同一份解包/点云/跟踪/手势判定代码,
 *       避免"网页里一套、桥接里又一套"导致行为不一致。
 * 重新生成: node tools/extract-core.js <radar-pointcloud.html> <radar-core.js>
 * ============================================================ */
`;
const footer = `
;window.__RADAR_CORE__ = {
  StreamDemux, FrameAssembler, Cloud, Tracker, GestureEngine, JCDet, CFG,
  Bus, U, DEFAULT_CFG_SCRIPT, CFG_SCRIPTS, ChipProfiles, APP_VER
};
`;
fs.writeFileSync(dst, header + body + footer);
console.log('写入 ' + dst + '  ' + fs.statSync(dst).size + ' 字节');
