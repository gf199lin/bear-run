#!/usr/bin/env node
/**
 * 把 js/cloud.js 和 js/auth.js 同步进 index.html 的内联脚本块。
 *
 * 为什么需要：index.html 是「所有 JS 内联成一个文件」的产物（双击即玩、
 * GitHub Pages 也不用管 MIME / 路径）。改了 js/ 下的源文件后，必须重新内联，
 * 否则线上跑的还是旧代码。
 *
 * 用法：
 *   node tools/sync-inline.js            # 同步
 *   node tools/sync-inline.js --check    # 只检查是否已同步，不写文件
 *
 * 只处理这两个文件；Game.js / config.js 等其它模块请按同样方式扩展。
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, 'index.html');
const CHECK_ONLY = process.argv.includes('--check');

const TARGETS = [
  { src: path.join(ROOT, 'js', 'cloud.js'), marker: 'RR.Cloud = Cloud', name: 'cloud' },
  { src: path.join(ROOT, 'js', 'auth.js'),  marker: 'RR.Auth = Auth',   name: 'auth'  },
  { src: path.join(ROOT, 'js', 'Game.js'),  marker: 'RR.Game = Game',   name: 'game'  }
];

const CONFIG_BLOCK = [
  '<script>',
  '/* === RR_CLOUD_CONFIG =====================================================',
  ' * 部署时只需要改这三行。留空 = 纯本地模式（双击 index.html 玩的时候就是这样）。',
  ' * url / key 来自 Supabase 控制台 -> Project Settings -> API。',
  ' * key 用 anon public key，可以公开；service_role key 绝对不能放这里。',
  ' * ======================================================================= */',
  'window.RR_CLOUD_CONFIG = {',
  "  url: '',",
  "  key: '',",
  "  domain: 'bearrun.local'",
  '};',
  '</script>'
].join('\n');

function blockRegex(marker) {
  return new RegExp(
    '<script>(?:(?!<\\/script>)[\\s\\S])*?' +
    marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') +
    '(?:(?!<\\/script>)[\\s\\S])*?<\\/script>'
  );
}

function read(p) {
  return fs.readFileSync(p, 'utf8');
}

function main() {
  let html = read(HTML);
  const original = html;
  let changed = false;

  for (const t of TARGETS) {
    const src = read(t.src);
    if (src.indexOf('</script>') !== -1) {
      console.error('ERROR: ' + t.name + ' contains "</script>" and cannot be inlined safely.');
      process.exit(1);
    }

    const re = blockRegex(t.marker);
    if (!re.test(html)) {
      // cloud.js 是新增模块：首次部署到别的分支时，index.html 里还没有它，
      // 插到 auth 块之前即可（cloud 必须先于 auth 启动，才能同步恢复登录态）
      if (t.name === 'cloud') {
        const authRe = blockRegex('RR.Auth = Auth');
        const anchor = html.match(authRe);
        if (!anchor) {
          console.error('ERROR: cannot find anchor block (RR.Auth = Auth) to insert cloud.js');
          process.exit(1);
        }
        html = html.replace(authRe, function () {
          return '<script>\n' + src.replace(/\s+$/, '') + '\n</script>\n' + anchor[0];
        });
        changed = true;
        continue;
      }
      console.error('ERROR: inline block for "' + t.marker + '" not found in index.html');
      process.exit(1);
    }

    const next = '<script>\n' + src.replace(/\s+$/, '') + '\n</script>';
    // 用函数形式替换：源码里含 "$'"、"$&" 这类字符，直接当 replacement 会被展开
    const replaced = html.replace(re, function () { return next; });
    // 只有行尾差异（CRLF vs LF）不算改动，否则 --check 会永远报 out of sync
    if (replaced !== html &&
        replaced.replace(/\r\n/g, '\n') !== html.replace(/\r\n/g, '\n')) {
      html = replaced;
      changed = true;
    }
  }

  // 确保 RR_CLOUD_CONFIG 块存在，并且位于 cloud 块之前
  if (html.indexOf('window.RR_CLOUD_CONFIG = {') === -1) {
    const cloudRe = blockRegex('RR.Cloud = Cloud');
    const m = html.match(cloudRe);
    if (m) {
      html = html.replace(cloudRe, function () { return CONFIG_BLOCK + '\n' + m[0]; });
      changed = true;
    }
  }

  if (CHECK_ONLY) {
    if (changed) {
      console.error('OUT OF SYNC: index.html differs from js/ sources.');
      process.exit(2);
    }
    console.log('OK: index.html is in sync.');
    return;
  }

  if (!changed) {
    console.log('Nothing to do: already in sync.');
    return;
  }

  fs.writeFileSync(HTML, html, 'utf8');
  console.log('Synced -> index.html (' + (html.length - original.length) + ' bytes delta)');
}

main();
