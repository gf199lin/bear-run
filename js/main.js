/**
 * 入口：装配游戏、绑定 UI、处理 URL 参数。
 *
 * 支持的 URL 参数（方便真机联调，不用改代码）：
 *   ?input=keyboard|touch|radar|mock
 *   ?challenge=honey|dodge|steady|combo|long   调试用：钉死这一局的随机挑战
 *   ?radar=ws://192.168.1.7:8766
 *   ?radar2=ws://192.168.1.7:8767   双雷达：radar 管左右、radar2 管上下
 *                   （?radar2=off 可临时关掉双雷达）
 *   ?char=xiong2|xiong1   直接指定角色（熊二 / 熊大）
 *   ?debug=1        直接打开雷达调试面板
 *   ?test=1         直接进测试模式（空跑道：没有障碍物，熊二一直跑，专供雷达联调）
 *   ?mode=test      同上（两者等价）
 *   ?testspeed=<m/s> 测试模式的固定速度（默认 10，见 config.js 的 TEST.speed）
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

    // ---------- 本地账号：登录 / 注册 / 登出 ----------
    // 登录页是盖在主菜单上的全屏层。未登录时它压住整个游戏；
    // 登录/注册成功会整页刷新，让存档读取天然跟随当前账号
    // （Game 里的最高分 / 纪录 / 输入源 / 角色都走 RR.Auth.key() 加账号前缀）。
    if (RR.Auth) {
      var authScreenEl = $('auth-screen');
      var acctNameEl = $('acct-name');

      function syncAcctName() {
        var u = RR.Auth.current();
        if (RR.Auth.isGuest && RR.Auth.isGuest()) u = '游客';
        if (acctNameEl) acctNameEl.textContent = u || '-';
      }

      var canPlay = RR.Auth.canPlay ? RR.Auth.canPlay() : RR.Auth.loggedIn();
      if (canPlay) {
        if (authScreenEl) authScreenEl.classList.add('hidden');
        syncAcctName();
      } else {
        // 未登录也非游客：保持登录页显示，盖住主菜单
        if (authScreenEl) authScreenEl.classList.remove('hidden');
      }

      var authForm = $('auth-form');
      if (authForm) {
        var isRegister = false;
        var authUser = $('auth-user');
        var authPass = $('auth-pass');
        var authConfirmWrap = $('auth-confirm-wrap');
        var authConfirm = $('auth-confirm');
        var authMsg = $('auth-msg');
        var authSubmit = $('auth-submit');
        var authToggle = $('auth-toggle');

        function setAuthMode(reg) {
          isRegister = reg;
          if (authSubmit) authSubmit.textContent = reg ? '注册账号' : '登 录';
          if (authConfirmWrap) authConfirmWrap.style.display = reg ? '' : 'none';
          if (authToggle) authToggle.textContent = reg ? '已有账号？去登录' : '没有账号？去注册';
          if (authMsg) authMsg.textContent = '';
        }
        if (authToggle) authToggle.addEventListener('click', function () { setAuthMode(!isRegister); });

        authForm.addEventListener('submit', function (e) {
          e.preventDefault();
          var u = authUser ? authUser.value.trim() : '';
          var p = authPass ? authPass.value : '';
          var c = authConfirm ? authConfirm.value : p;
          var handle = function (res) {
            if (res && res.ok) { global.location.reload(); return; }
            if (authMsg) authMsg.textContent = (res && res.msg) ? res.msg : '操作失败，请重试';
          };
          if (isRegister) { if (RR.Auth.register) RR.Auth.register(u, p, c).then(handle); }
          else if (RR.Auth.login) RR.Auth.login(u, p).then(handle);
        });
        setAuthMode(false);
      }

      // 游客模式：免注册免登录直接进游戏；存档只留在内存里，关掉页面就没有历史
      var authGuest = $('auth-guest');
      if (authGuest && RR.Auth.enterGuest) authGuest.addEventListener('click', function () {
        RR.Auth.enterGuest();
        global.location.reload();
      });

      // 退出账号：清会话 -> 刷新回到登录页（游客也一样退回收拾登录页）
      var logoutBtn = $('btn-logout');
      if (logoutBtn) logoutBtn.addEventListener('click', function () {
        RR.Auth.logout();
        global.location.reload();
      });
    }

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

    // ---------- 双雷达：第二路（上下） ----------
    // ?radar2=<ws地址> 一出现就启用双雷达：?radar=（或记住的地址）管左右，radar2 管上下。
    // 只用 ?radar2= 时，左右仍然走记忆地址 / config 默认值。
    // ?radar2=off 用来临时关掉双雷达，不必清 localStorage。
    var radarUrl2 = queryParam('radar2') || queryParam('radarud');
    if (radarUrl2 === 'off' || radarUrl2 === '0') {
      RR.CFG.RADAR.dual = false;
      RR.CFG.RADAR.udUrl = null;
      try { localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_DUAL, '0'); } catch (e) { /* 忽略 */ }
    } else if (radarUrl2) {
      RR.CFG.RADAR.dual = true;
      RR.CFG.RADAR.udUrl = radarUrl2;
      try {
        localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_URL_UD, radarUrl2);
        localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_DUAL, '1');
      } catch (e) { /* 忽略 */ }
    } else {
      try {
        var dualSaved = localStorage.getItem(RR.CFG.STORAGE_KEY_RADAR_DUAL) === '1';
        var udSaved = localStorage.getItem(RR.CFG.STORAGE_KEY_RADAR_URL_UD);
        if (dualSaved && udSaved) {
          RR.CFG.RADAR.dual = true;
          RR.CFG.RADAR.udUrl = udSaved;
        }
      } catch (e) { /* 忽略 */ }
    }

    // ---------- 横向手感现场微调（不改文件，刷新即可试） ----------
    // 现场联调用：?gain=0.6&enter=0.5&exit=0.35&step=1&cooldown=120
    //   gain     <1 变钝（雷达太灵敏），>1 变灵敏（读数偏小）
    //   enter    进左右车道的阈值（默认 0.36，调高 = 必须移动更大才换道）
    //   exit     回中间车道的阈值（默认 0.22，一般设成 enter 的 0.6 倍左右）
    //   step     单帧最多换几档（默认 1 = 手一晃最多走一格）
    //   cooldown 两次换道的最小间隔 ms（默认 120）
    var gainP = queryParam('gain');
    if (gainP !== null && isFinite(Number(gainP)) && Number(gainP) > 0) {
      RR.CFG.RADAR.xGain = Number(gainP);
      RR.CFG.RADAR.xGainLock = true;   // URL 优先：已有校准存档也不会把它覆盖回去
    }
    var enterP = queryParam('enter');
    if (enterP !== null && isFinite(Number(enterP)) && Number(enterP) > 0) {
      RR.CFG.RADAR.enterThreshold = Number(enterP);
    }
    var exitP = queryParam('exit');
    if (exitP !== null && isFinite(Number(exitP)) && Number(exitP) >= 0) {
      RR.CFG.RADAR.exitThreshold = Number(exitP);
    }
    var stepP = queryParam('step');
    if (stepP !== null && isFinite(Number(stepP))) {
      RR.CFG.RADAR.maxLaneStepPerFrame = Math.max(1, Math.floor(Number(stepP)));
    }
    var cdP = queryParam('cooldown');
    if (cdP !== null && isFinite(Number(cdP))) {
      RR.CFG.RADAR.laneCooldownMs = Math.max(0, Number(cdP));
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
    var testSpeed = queryParam('testspeed');     // 测试模式：临时改固定速度（m/s）
    if (testSpeed) RR.CFG.TEST.speed = Number(testSpeed);

    // ---------- 雷达开局校准（新手教程，js/Calibration.js） ----------
    // 现场测出方向符号 + 静息基线，修正后续所有帧的 x（x' = (x - x0) * sign）。
    // 装配全在这里，Game.js / InputSource.js 都不用改。
    var wizard = null;
    var wrappedSrc = null;
    var autoAsked = false;
    var pendingStart = false;      // 校准结束后要不要自动开局
    var pendingStartTest = false;  // 上面这次开局是不是"测试模式"

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
          // 从「开始快跑」/「雷达测试」进来的：校准完（或跳过/关闭）就接着开局
          if (pendingStart) {
            var asTest = pendingStartTest;
            pendingStart = false;
            pendingStartTest = false;
            setTimeout(function () { game.start(asTest ? { test: true } : undefined); }, 120);
          }
        },
        // 校准面板里填了地址 / 勾了双雷达后走这里：写回配置并重建数据源。
        // 换地址必须重新 new 一次 WebSocket，所以旧 src 直接 detach 掉。
        onReconnect: function (next) {
          RR.CFG.RADAR.defaultUrl = next.lrUrl;
          RR.CFG.RADAR.lrUrl = next.lrUrl;
          RR.CFG.RADAR.dual = !!next.dual;
          RR.CFG.RADAR.udUrl = next.dual ? next.udUrl : null;
          try {
            localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_URL, next.lrUrl);
            if (next.dual) {
              localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_URL_UD, next.udUrl);
              localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_DUAL, '1');
            } else {
              localStorage.setItem(RR.CFG.STORAGE_KEY_RADAR_DUAL, '0');
            }
          } catch (e) { /* 忽略 */ }

          game.setInputSource('radar');
          var src = currentSource();
          if (!src) return;
          syncCalibration();
          // 用新的 src 重走向导（close 会把 src 置空，所以要再 open 一次）
          wizard.close();
          wizard.open(src);
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
        pendingStartTest = false;
        ensureWizard().open(src);
      };
      if (!RR.Tutorial || tutorialParam === '0') { afterTutorial(); return; }
      if (tutorialParam === '1' || !RR.Tutorial.hasSeen()) {
        closeWizardIfOpen();
        RR.Tutorial.show(afterTutorial);
      } else afterTutorial();
    }

    /**
     * 测试模式：没有障碍物、光头强不上场、熊二按固定速度一直跑。
     * 专给现场联调雷达用 —— 不弹新手教程、不抽挑战、不记纪录、不会死。
     * 选的是雷达且还没校准，就先走校准，校准结束后自动进测试场。
     */
    function startTestMode() {
      requestFullscreenIfMobile();
      if (RR.CFG.TEST && RR.CFG.TEST.showDebug) game.debug.setVisible(true);

      var src = currentSource();
      var cal = calibSaved();
      var needCalib = src && src.id === 'radar' && RR.Calibration && RR.CalibrationWizard &&
        !(cal && RR.Calibration.isUsable(cal));
      if (!needCalib) { game.start({ test: true }); return; }

      closeWizardIfOpen();
      pendingStart = true;
      pendingStartTest = true;
      ensureWizard().open(src);
    }

    // ---------- 按钮绑定 ----------
    bind('btn-start', function () { requestFullscreenIfMobile(); startWithGuides(); });
    bind('btn-tutorial', function () { if (RR.Tutorial) { closeWizardIfOpen(); RR.Tutorial.show(); } });
    bind('btn-test', function () { startTestMode(); });
    bind('btn-pause', function () {
      if (game.state === 'playing') game.pause();
      else if (game.state === 'paused') game.resume();
    });
    bind('btn-resume', function () { game.resume(); });
    bind('btn-restart-pause', function () { game.restart(); });
    bind('btn-home-pause', function () { game.toMenu(); });
    bind('btn-restart', function () { game.restart(); });
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
    // 联调用：?mode=test（或 ?test=1）直接进测试模式，省掉点菜单那一步。
    // 常和雷达地址一起用：?test=1&input=radar&radar=ws://192.168.1.7:8766
    // 还没登录（且不是游客）时不自动开局：登录成功会整页刷新，先跑的这局等于白跑。
    var authLocked = !!(RR.Auth && !(RR.Auth.canPlay ? RR.Auth.canPlay() : RR.Auth.loggedIn()));
    var wantTest = queryParam('mode') === 'test' || queryParam('test') === '1';
    if (authLocked) {
      // 登录层就是这一屏，什么都不用做
    } else if (wantTest) {
      setTimeout(function () { startTestMode(); }, 250);
    } else if (queryParam('autostart') === '1') {
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
