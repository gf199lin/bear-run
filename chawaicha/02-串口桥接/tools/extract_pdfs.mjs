// PDF文本提取(一次性工具): pdfjs-dist 提取 -> .deepworks\tmp\manuals\*.txt
import * as pdfjs from 'pdfjs-dist/legacy/build/pdf.mjs';
import fs from 'fs';
import path from 'path';

const SRC = String.raw`C:\Users\叉歪叉\Documents\xwechat_files\wxid_3onj4akypcjx22_d38f\msg\file\2026-10\doc 毫米波雷达资料(1)\doc 毫米波雷达资料`;
const OUT = String.raw`C:\Users\叉歪叉\DeepWorks\.deepworks\tmp\manuals`;
fs.mkdirSync(OUT, { recursive: true });

const files = fs.readdirSync(SRC).filter(f => f.toLowerCase().endsWith('.pdf'));
for (const f of files) {
  const full = path.join(SRC, f);
  process.stdout.write('提取: ' + f + ' ... ');
  try {
    const data = new Uint8Array(fs.readFileSync(full));
    const doc = await pdfjs.getDocument({ data, isEvalSupported: false, disableFontFace: true, useSystemFonts: false }).promise;
    let text = '';
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const tc = await page.getTextContent();
      for (const it of tc.items) {
        text += it.str;
        if (it.hasEOL) text += '\n';
      }
      text += '\n=== 第' + i + '页完 ===\n';
    }
    const outFile = path.join(OUT, f.replace(/\.pdf$/i, '.txt'));
    fs.writeFileSync(outFile, text, 'utf8');
    console.log('OK ' + doc.numPages + '页, ' + (text.length / 1024).toFixed(0) + 'K字符 -> ' + outFile);
  } catch (e) {
    console.log('失败: ' + e.message);
  }
}
