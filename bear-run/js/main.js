/**
 * 入口：装配游戏、绑定 UI、处理 URL 参数。
 *
 * 支持的 URL 参数（方便真机联调，不用改代码）：
 *   ?input=keyboard|touch|radar|mock
 *   ?radar=ws://192.168.1.7:8766
 *   ?char=xiong2|xiong1   直接指定角色（熊二 / 熊大）
 *   ?debug=1        直接打开雷达调试面板
 */
(function (global) {
  'use strict';

  var RR = global.RR;

  function $(id) { return document.getElementById(id); }

  function bind(id, handler) {
    var el = $(id);
    if (el) el.addEventListener('click', handler);
  }

  function onReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function queryParam(name) {
    var m = new RegExp('[?&]' + name + '=([^&]*)').exec(global.location.search);
    return m ? decodeURIComponent(m[1]) : null;
  }

  onReady(function () {
    var canvas = $('game');
    if (!canvas) return;

    if (!global.THREE) {
      canvas.parentNode.innerHTML =
        '<div style="color:#f0e6d2;font:16px/1.7 system-ui;padding:40px;text-align:center">' +
        'three.js 没有加载成功。<br>请确认 <code>vendor/three.min.js</code> 与 <code>index.html</code> 在同一目录下。' +
        '</div>';
      return;
    }

    var game = new RR.Game(canvas);
    global.RRGame = game;              // 方便在浏览器控制台里调试

    // ---------- URL 参数 ----------
    var radarUrl = queryParam('radar');
    if (radarUrl) RR.CFG.RADAR.defaultUrl = radarUrl;

    var initialInput = queryParam('input') || game.savedInputId;
    game.setInputSource(initialInput);

    if (queryParam('debug') === '1') game.debug.setVisible(true);

    // ---------- 按钮绑定 ----------
    bind('btn-start', function () { requestFullscreenIfMobile(); game.start(); });
    bind('btn-pause', function () {
      if (game.state === 'playing') game.pause();
      else if (game.state === 'paused') game.resume();
    });
    bind('btn-resume', function () { game.resume(); });
    bind('btn-restart-pause', function () { game.start(); });
    bind('btn-home-pause', function () { game.toMenu(); });
    bind('btn-restart', function () { game.start(); });
    bind('btn-home', function () { game.toMenu(); });

    bind('btn-debug', function () { game.debug.toggle(); });
    bind('debug-close', function () { game.debug.setVisible(false); });

    // 输入源选择
    var picker = $('input-picker');
    if (picker) {
      picker.addEventListener('click', function (e) {
        var btn = e.target.closest ? e.target.closest('.chip') : null;
        if (!btn) return;
        var id = btn.getAttribute('data-input');
        if (!id) return;
        game.setInputSource(id);
      });
    }

    // ---------- 角色选择 ----------
    // 按钮直接由 CFG.CHARACTERS 生成：颜色以配置为唯一来源，不在这里另写一份。
    var charPicker = $('char-picker');
    if (charPicker) {
      var chars = RR.CFG.CHARACTERS || [];
      for (var ci = 0; ci < chars.length; ci++) {
        var cspec = chars[ci];
        var cbtn = document.createElement('button');
        cbtn.type = 'button';
        cbtn.className = 'chip';
        cbtn.setAttribute('data-char', cspec.id);

        var cdot = document.createElement('span');
        cdot.className = 'chip-dot';
        cdot.style.background = '#' + ('000000' + (cspec.fur >>> 0).toString(16)).slice(-6);
        cbtn.appendChild(cdot);
        cbtn.appendChild(document.createTextNode(cspec.name));

        charPicker.appendChild(cbtn);
      }

      charPicker.addEventListener('click', function (e) {
        var b = e.target.closest ? e.target.closest('.chip') : null;
        if (!b) return;
        var id = b.getAttribute('data-char');
        if (id) game.setCharacter(id);
      });
    }

    var initialChar = queryParam('char') || game.savedCharId;
    game.setCharacter(initialChar);

    // ---------- 移动端横屏适配 ----------
    global.addEventListener('orientationchange', function () {
      setTimeout(function () { game.refreshLayout(); }, 260);
    });
    global.addEventListener('resize', function () {
      setTimeout(function () { game.refreshLayout(); }, 60);
    });

    // ---------- 启动 ----------
    game.run();

    // 自动化测试用：?autostart=1 直接开局
    if (queryParam('autostart') === '1') {
      setTimeout(function () { game.start(); }, 250);
    }

    function requestFullscreenIfMobile() {
      if (!game.isCoarse) return;
      try {
        var el = document.documentElement;
        if (el.requestFullscreen) {
          var p = el.requestFullscreen();
          if (p && p.catch) p.catch(function () { /* 用户拒绝或不支持，忽略 */ });
        } else if (el.webkitRequestFullscreen) {
          el.webkitRequestFullscreen();
        }
      } catch (e) { /* 忽略 */ }

      try {
        if (screen.orientation && screen.orientation.lock) {
          var q = screen.orientation.lock('landscape');
          if (q && q.catch) q.catch(function () { /* 忽略 */ });
        }
      } catch (e) { /* 忽略 */ }
    }
  });

})(window);
