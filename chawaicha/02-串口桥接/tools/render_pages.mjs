// PDF指定页渲染为PNG(一次性工具): node render_pages.mjs <pdf路径> <起始页> <结束页> <输出目录> [缩放=2]
import * as pdfjs from 'pdfjs-dist/legacy/build/pdf.mjs';
import { createCanvas } from '@napi-rs/canvas';
import fs from 'fs';
import path from 'path';

const [pdfPath, p0, p1, outDir, scaleArg] = process.argv.slice(2);
const scale = parseFloat(scaleArg || '2');
fs.mkdirSync(outDir, { recursive: true });

const data = new Uint8Array(fs.readFileSync(pdfPath));
const doc = await pdfjs.getDocument({ data, isEvalSupported: false, disableFontFace: true }).promise;
for (let i = parseInt(p0); i <= Math.min(parseInt(p1), doc.numPages); i++) {
  const page = await doc.getPage(i);
  const vp = page.getViewport({ scale });
  const canvas = createCanvas(Math.ceil(vp.width), Math.ceil(vp.height));
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  await page.render({ canvasContext: ctx, viewport: vp }).promise;
  const out = path.join(outDir, 'page_' + String(i).padStart(3, '0') + '.png');
  fs.writeFileSync(out, canvas.toBuffer('image/png'));
  console.log('OK 第' + i + '页 -> ' + out);
}
