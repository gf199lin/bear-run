/**
 * 入口：装配游戏、绑定 UI、处理 URL 参数。
 *
 * 支持的 URL 参数（方便真机联调，不用改代码）：
 *   ?input=keyboard|touch|radar|mock
 *   ?radar=ws://192.168.1.7:8766
 *   ?char=xiong2|xiong1   直接指定角色（熊二 / 熊大）
 *   ?debug=1        直接打开雷达调试面板
 *   ?startat=<米>   调试用：直接从该里程的难度区开局（验收 4 个区的配色与摆件）
 *   ?input=replay&replay=<js>&series=<站点>&replayk=<阈值>&replayspeed=<倍速>
 *                  离线回放雷达采集数据
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

    // 离线回放：?input=replay&replay=<js路径>&series=<站点>&replayk=<阈值>&replayspeed=<倍速>
    var rpUrl = queryParam('replay');
    if (rpUrl) RR.CFG.RADAR.replayUrl = rpUrl;
    var rpSeries = queryParam('series');
    if (rpSeries) RR.CFG.RADAR.replaySeries = rpSeries;
    var rpK = queryParam('replayk');
    if (rpK) RR.CFG.RADAR.replayK = Number(rpK);
    var rpSpeed = queryParam('replayspeed');
    if (rpSpeed) RR.CFG.RADAR.replaySpeed = Number(rpSpeed);
    if (queryParam('replayloop') === '0') RR.CFG.RADAR.replayLoop = false;
    var rpMode = queryParam('replaymode');       // energy | truth | azimuth
    if (rpMode) RR.CFG.RADAR.replayMode = rpMode;
    var rpFrom = queryParam('replayfrom');       // 从第几帧开始回放
    if (rpFrom) RR.CFG.RADAR.replayFrom = Number(rpFrom);
    // 方位解码参数：?replayazleft=-1.55&replayampgate=3600&replayazneed=3&replayazwin=10&replayazright=9.9
    var rpAzL = queryParam('replayazleft');
    if (rpAzL) RR.CFG.RADAR.replayAzLeft = Number(rpAzL);
    var rpAzR = queryParam('replayazright');
    if (rpAzR) RR.CFG.RADAR.replayAzRight = Number(rpAzR);
    var rpGate = queryParam('replayampgate');
    if (rpGate) RR.CFG.RADAR.replayAmpGate = Number(rpGate);
    var rpNeed = queryParam('replayazneed');
    if (rpNeed) RR.CFG.RADAR.replayAzNeed = Number(rpNeed);
    var rpAzWin = queryParam('replayazwin');
    if (rpAzWin) RR.CFG.RADAR.replayAzWin = Number(rpAzWin);
    var hp = queryParam('hp');                   // 联调：临时加命，方便长时间观察
    if (hp) { RR.CFG.HP.start = Number(hp); RR.CFG.HP.max = Math.max(RR.CFG.HP.max, Number(hp)); }

    var initialInput = queryParam('input') || game.savedInputId;
    game.setInputSource(initialInput);

    if (queryParam('debug') === '1') game.debug.setVisible(true);

    var startAt = Number(queryParam('startat'));
    if (isFinite(startAt) && startAt > 0) game.startAt = startAt;

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

    // ---------- 音频 ----------
    // 音效和背景音乐都是 WebAudio 现场合成的（js/Audio.js），工程里没有任何
    // 音频文件 —— 双击 index.html 用 file:// 打开也不会因为加载 .mp3 失败而静音。
    // 浏览器的自动播放策略要求 AudioContext 必须由"用户手势"解锁，install()
    // 挂的事件委托会在玩家第一次点击任意按钮时完成解锁。
    if (RR.Audio) {
      RR.Audio.install();

      var soundBtn = $('btn-sound');
      if (soundBtn) {
        var syncSoundBtn = function () {
          var m = RR.Audio.isMuted();
          soundBtn.classList.toggle('is-muted', m);
          soundBtn.textContent = m ? '静音' : '音效';
          soundBtn.setAttribute('aria-pressed', m ? 'true' : 'false');
        };
        RR.Audio.onMuteChange = syncSoundBtn;
        syncSoundBtn();                                   // 记住上次的选择
        bind('btn-sound', function () { RR.Audio.toggle(); });
      }
    }

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
