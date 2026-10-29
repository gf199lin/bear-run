/**
 * 入口：装配游戏、绑定 UI、处理 URL 参数。
 *
 * 支持的 URL 参数（方便真机联调，不用改代码）：
 *   ?input=keyboard|touch|radar|mock
 *   ?challenge=honey|dodge|steady|combo|long   调试用：钉死这一局的随机挑战
 *   ?radar=ws://192.168.1.7:8766
 *   ?char=xiong2|xiong1   直接指定角色（熊二 / 熊大）
 *   ?debug=1        直接打开雷达调试面板
 *   ?startat=<米>   调试用：直接从该里程的难度区开局（验收 4 个区的配色与摆件）
 *   ?input=replay&replay=<js>&series=<站点>&replayk=<阈值>&replayspeed=<倍速>
 *                  离线回放雷达采集数据
 *   ?tutorial=1     强制先走一遍新手指引（即使已经看过）
 *   ?tutorial=0     跳过新手指引，直接开局
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
    // 雷达地址三处优先级：?radar= > localStorage（radar-connect.html 联调台记下的）> config.js 默认值。
    // 用 URL 参数指定过一次，也会被记下来，下次主菜单点「雷达」直接用同一台机器。
    var radarUrl = queryParam('radar');
    if (radarUrl) {
      RR.CFG.RADAR.defaultUrl = radarUrl;
      try { localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_URL, radarUrl); } catch (e) { /* 忽略 */ }
    } else {
      try {
        var savedRadar = localStorage.getItem(RR.CFG.STORAGE_KEY_RADAR_URL);
        if (savedRadar) RR.CFG.RADAR.defaultUrl = savedRadar;
      } catch (e) { /* 忽略 */ }
    }

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

    // ---------- 雷达开局校准（新手教程，js/Calibration.js） ----------
    // 现场测出方向符号 + 静息基线，修正后续所有帧的 x（x' = (x - x0) * sign）。
    // 装配全在这里，Game.js / InputSource.js 都不用改。
    var wizard = null;
    var wrappedSrc = null;
    var autoAsked = false;
    var pendingStart = false;      // 校准结束后要不要自动开局

    function currentSource() {
      return (game.input && game.input.current) ? game.input.current : null;
    }

    function calibSaved() {
      return RR.Calibration ? RR.Calibration.load() : null;
    }

    function updateCalibNote() {
      var el = $('calib-status');
      if (!el) return;
      var cal = calibSaved();
      if (cal && RR.Calibration.isUsable(cal)) {
        el.textContent = '已校准 · ' + RR.Calibration.describe(cal);
        el.className = 'calib-status is-ok';
      } else {
        el.textContent = '未校准：选雷达后会自动引导';
        el.className = 'calib-status';
      }
    }

    function ensureWizard() {
      if (wizard) return wizard;
      wizard = new RR.CalibrationWizard({
        onDone: function (cal) {
          var s = currentSource();
          if (s && s.id === 'radar' && cal && RR.Calibration.isUsable(cal)) {
            RR.Calibration.install(s, cal);
          }
          updateCalibNote();
        },
        onClose: function () {
          updateCalibNote();
          // 从「开始快跑」进来的：校准完（或跳过/关闭）就接着开局
          if (pendingStart) {
            pendingStart = false;
            setTimeout(function () { game.start(); }, 120);
          }
        }
      });
      return wizard;
    }

    /** 切换输入源后：装校准 + 转发事件 + 首启自动引导 */
    function syncCalibration() {
      if (!RR.Calibration || !RR.CalibrationWizard) return;
      updateCalibNote();
      var src = currentSource();
      if (!src) return;

      if (src !== wrappedSrc) {                       // 事件转发只挂一次
        wrappedSrc = src;
        var prev = src.onEvent;
        src.onEvent = function (type) {
          if (wizard && wizard.isOpen) wizard.notifyEvent(type);
          if (prev) prev.call(this, type);
        };
      }

      if (src.id !== 'radar') return;                 // 只有真实雷达需要校准
      var cal = calibSaved();
      if (cal && RR.Calibration.isUsable(cal)) {
        RR.Calibration.install(src, cal);
        return;
      }
      var cfg = (RR.CFG.RADAR && RR.CFG.RADAR.calib) || {};
      if (cfg.autoOpen !== false && !autoAsked) {      // 每次打开页面最多自动弹一次
        autoAsked = true;
        setTimeout(function () {
          if (calibSaved()) return;
          // 新手指引还没看过：先让玩家学玩法，校准推迟到教程结束后由 startWithGuides 接管
          if (RR.Tutorial && !RR.Tutorial.hasSeen()) return;
          ensureWizard().open(currentSource());
        }, 600);
      }
    }

    function openCalibration() {
      if (!RR.Calibration || !RR.CalibrationWizard) return;
      if (!currentSource() || currentSource().id !== 'radar') game.setInputSource('radar');
      var src = currentSource();
      if (!src || src.id !== 'radar') return;
      ensureWizard().open(src);
    }

    // 向导自己每帧驱动，不依赖游戏循环 —— 在开始界面里也能校准
    var calibLast = 0;
    function driveWizard(ts) {
      requestAnimationFrame(driveWizard);
      var now = ts || (global.performance ? global.performance.now() : Date.now());
      var dt = calibLast ? (now - calibLast) / 1000 : 1 / 60;
      calibLast = now;
      if (!(dt > 0) || dt > 0.5) dt = 1 / 60;
      if (wizard && wizard.isOpen) wizard.update(dt);
    }

    var initialInput = queryParam('input') || game.savedInputId;
    game.setInputSource(initialInput);
    syncCalibration();
    requestAnimationFrame(driveWizard);

    if (queryParam('debug') === '1') game.debug.setVisible(true);

    var startAt = Number(queryParam('startat'));
    if (isFinite(startAt) && startAt > 0) game.startAt = startAt;

    // 调试用（?challenge=<id>）：跳过随机，直接钉死这一局的挑战
    var forcedChallenge = queryParam('challenge');
    if (forcedChallenge) game._forcedChallenge = forcedChallenge;

    // ---------- 新手引导（js/Tutorial.js） ----------
    // 首次点「开始快跑」走 6 步玩法引导 → 雷达未校准则接着校准 → 开局。
    // 教程已看过、或 ?tutorial=0，则直接进入下一环节。
    var tutorialParam = queryParam('tutorial');

    /** 教程要盖在最上层，先把可能已经开着的校准向导收起来，避免两层引导抢输入 */
    function closeWizardIfOpen() {
      if (wizard && wizard.isOpen) wizard.close();
    }

    function startWithGuides() {
      var afterTutorial = function () {
        var src = currentSource();
        var cal = calibSaved();
        var needCalib = src && src.id === 'radar' && RR.Calibration && RR.CalibrationWizard &&
          !(cal && RR.Calibration.isUsable(cal));
        if (!needCalib) { game.start(); return; }
        pendingStart = true;
        ensureWizard().open(src);
      };
      if (!RR.Tutorial || tutorialParam === '0') { afterTutorial(); return; }
      if (tutorialParam === '1' || !RR.Tutorial.hasSeen()) {
        closeWizardIfOpen();
        RR.Tutorial.show(afterTutorial);
      }
      else afterTutorial();
    }

    // ---------- 按钮绑定 ----------
    bind('btn-start', function () { requestFullscreenIfMobile(); startWithGuides(); });
    bind('btn-tutorial', function () { if (RR.Tutorial) { closeWizardIfOpen(); RR.Tutorial.show(); } });
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

    // 雷达校准（新手教程）入口
    bind('btn-calib', function () { openCalibration(); });

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
        syncCalibration();
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

    // 自动化测试用：?autostart=1 直接开局（旧行为，跳过引导）
    // 想连引导一起测：?autostart=1&tutorial=1
    if (queryParam('autostart') === '1') {
      if (tutorialParam === '1') setTimeout(function () { startWithGuides(); }, 250);
      else setTimeout(function () { game.start(); }, 250);
    }

    // 演示 / 验收用：?tutorial=preview[&step=N] 只弹教程，不进游戏
    if (tutorialParam === 'preview' && RR.Tutorial) {
      var previewStep = Number(queryParam('step') || 1);
      setTimeout(function () {
        RR.Tutorial.show();
        if (isFinite(previewStep) && previewStep > 1) RR.Tutorial.goto(previewStep - 1);
      }, 300);
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
