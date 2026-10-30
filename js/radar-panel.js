/*!
 * radar-panel.js —— 游戏页面里的雷达控制面板
 *
 * 它只是「遥控器」：真正的动作由本机 webcontrol.py 执行。
 * 控制服务没启动时，面板会自己隐藏，不影响游戏。
 * 不依赖任何游戏模块，可独立加载。
 */
(function () {
  'use strict';

  var API = (location.protocol === 'file:')
    ? 'http://127.0.0.1:8801'
    : location.origin;

  var POLL_MS = 2500;
  var alive = false;
  var busy = false;
  var collapsed = false;
  var last = { stream: false, viewer: false, lr: false, ud: false };

  // ------------------------------------------------------------ DOM

  var root = document.createElement('div');
  root.id = 'radar-panel';
  root.setAttribute('aria-live', 'polite');
  root.style.cssText = [
    'position:fixed', 'right:14px', 'bottom:14px', 'z-index:99999',
    'font:12px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif',
    'color:#e8eef7', 'background:rgba(16,20,28,.92)',
    'border:1px solid rgba(120,170,255,.28)', 'border-radius:10px',
    'box-shadow:0 6px 24px rgba(0,0,0,.45)',
    'padding:8px 10px', 'min-width:196px', 'max-width:230px',
    'display:none', 'user-select:none', 'backdrop-filter:blur(6px)'
  ].join(';');

  var head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;gap:6px;cursor:pointer';

  var dot = document.createElement('span');
  dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#5a6577;flex:0 0 auto;transition:background .2s';

  var title = document.createElement('span');
  title.textContent = '雷达';
  title.style.cssText = 'font-weight:600;letter-spacing:.3px;flex:1';

  var caret = document.createElement('span');
  caret.textContent = '—';
  caret.style.cssText = 'opacity:.55;font-size:11px';

  head.appendChild(dot); head.appendChild(title); head.appendChild(caret);

  var body = document.createElement('div');
  body.style.cssText = 'margin-top:7px';

  var statusLine = document.createElement('div');
  statusLine.textContent = '未连接';
  statusLine.style.cssText = 'opacity:.8;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';

  var btnWrap = document.createElement('div');
  btnWrap.style.cssText = 'display:flex;gap:6px;align-items:stretch';

  function makeBtn(text) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = text;
    b.style.cssText = [
      'flex:1', 'padding:5px 8px', 'border-radius:6px', 'cursor:pointer',
      'border:1px solid rgba(120,170,255,.35)', 'background:rgba(70,110,190,.30)',
      'color:#dfe9ff', 'font:inherit', 'white-space:nowrap', 'transition:background .15s'
    ].join(';');
    b.addEventListener('mouseenter', function () { if (!b.disabled) b.style.background = 'rgba(80,125,215,.45)'; });
    b.addEventListener('mouseleave', function () { if (!b.disabled) b.style.background = 'rgba(70,110,190,.30)'; });
    b.addEventListener('keydown', function (e) { e.stopPropagation(); });
    return b;
  }

  var btnStream = makeBtn('连接雷达');
  var btnViewer = makeBtn('点云');

  var logBox = document.createElement('pre');
  logBox.style.cssText = [
    'display:none', 'margin:7px 0 0', 'padding:6px', 'max-height:132px',
    'overflow:auto', 'background:rgba(0,0,0,.35)', 'border-radius:6px',
    'font:10px/1.45 Consolas,monospace', 'white-space:pre-wrap',
    'word-break:break-all', 'color:#a9b8cc'
  ].join(';');

  btnWrap.appendChild(btnStream);
  btnWrap.appendChild(btnViewer);
  body.appendChild(statusLine);
  body.appendChild(btnWrap);
  body.appendChild(logBox);
  root.appendChild(head);
  root.appendChild(body);
  document.body.appendChild(root);

  head.addEventListener('click', function () {
    collapsed = !collapsed;
    body.style.display = collapsed ? 'none' : '';
    caret.textContent = collapsed ? '+' : '—';
  });

  // 防止面板上的空格/方向键触发游戏动作
  root.addEventListener('keydown', function (e) { e.stopPropagation(); });
  root.addEventListener('keyup', function (e) { e.stopPropagation(); });

  // ------------------------------------------------------------ api

  function api(path, method, timeoutMs) {
    var ctl = ('AbortController' in window) ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, timeoutMs || 25000);
    var opt = { method: method || 'GET', cache: 'no-store' };
    if (ctl) opt.signal = ctl.signal;
    return fetch(API + path, opt)
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .finally(function () { clearTimeout(timer); });
  }

  function render(s) {
    var streaming = !!(s && s.stream);
    var viewer = !!(s && s.viewer);
    var foreignLr = !!(s && s.foreign && s.foreign.lr);
    var foreignUd = !!(s && s.foreign && s.foreign.ud);
    var foreign = foreignLr || foreignUd;

    last = { stream: streaming, viewer: viewer, foreign: foreign };

    if (foreign) {
      var bad = foreignLr ? '8766' : '8767';
      statusLine.textContent = bad + ' 被其他程序占用';
      statusLine.title = '多半是旧的 bridge、串口助手，或另开的推流窗口';
      dot.style.background = '#e5534b';
      btnStream.textContent = '重试';
      btnStream.disabled = false;
      btnViewer.textContent = '点云';
      btnViewer.disabled = true;
      btnViewer.style.opacity = '.45';
      return;
    }

    btnViewer.style.opacity = '1';
    btnViewer.disabled = false;

    if (streaming) {
      var parts = [];
      if (s.lr) { parts.push('左右'); }
      if (s.ud) { parts.push('上下'); }
      statusLine.textContent = '已连接 · ' + (parts.join(' + ') || '推流中');
      dot.style.background = '#41c463';
      btnStream.textContent = '断开';
      btnViewer.textContent = viewer ? '关点云' : '点云';
    } else if (viewer) {
      statusLine.textContent = '点云查看器运行中';
      dot.style.background = '#e5b454';
      btnStream.textContent = '连接雷达';
      btnViewer.textContent = '关点云';
    } else {
      statusLine.textContent = '未连接';
      dot.style.background = '#5a6577';
      btnStream.textContent = '连接雷达';
      btnViewer.textContent = '点云';
    }
  }

  function poll() {
    api('/api/status', 'GET', 4000).then(function (s) {
      if (!alive) {
        alive = true;
        root.style.display = '';
      }
      if (!busy) { render(s); }
    }).catch(function () {
      // 控制服务没开：藏起来，别打扰游戏
      if (alive) { alive = false; root.style.display = 'none'; }
    });
  }

  function notify(msg) {
    statusLine.textContent = msg;
    statusLine.style.opacity = '1';
    setTimeout(function () { statusLine.style.opacity = '.8'; }, 1800);
  }

  function toggleStream() {
    if (busy) { return; }
    busy = true;
    var stopping = last.stream;
    btnStream.disabled = true;
    btnStream.textContent = stopping ? '断开中…' : '连接中…';
    dot.style.background = '#7f8ba1';
    api(stopping ? '/api/stop-stream' : '/api/start-stream', 'POST')
      .then(function (r) { notify(r.msg || (r.ok ? '完成' : '失败')); })
      .catch(function () { notify('控制服务没有响应'); })
      .finally(function () {
        busy = false;
        btnStream.disabled = false;
        poll();
      });
  }

  function toggleViewer() {
    if (busy) { return; }
    busy = true;
    var stopping = last.viewer;
    btnViewer.disabled = true;
    btnViewer.textContent = stopping ? '关闭中…' : '启动中…';
    api(stopping ? '/api/stop-viewer' : '/api/start-viewer', 'POST')
      .then(function (r) {
        notify(r.msg || '完成');
        if (r.ok && r.url) { window.open(r.url, '_blank', 'noopener'); }
      })
      .catch(function () { notify('控制服务没有响应'); })
      .finally(function () {
        busy = false;
        btnViewer.disabled = false;
        poll();
      });
  }

  function showLog() {
    if (logBox.style.display === 'block') {
      logBox.style.display = 'none';
      return;
    }
    api('/api/logs', 'GET', 6000).then(function (r) {
      logBox.textContent = r.tail || '(没有日志)';
      logBox.style.display = 'block';
      logBox.scrollTop = logBox.scrollHeight;
    }).catch(function () { notify('读不到日志'); });
  }

  btnStream.addEventListener('click', toggleStream);
  btnViewer.addEventListener('click', toggleViewer);
  title.addEventListener('dblclick', showLog);
  title.title = '双击查看最近日志';

  poll();
  setInterval(poll, POLL_MS);
})();
