# -*- coding: utf-8 -*-
"""把 js/Calibration.js 接进游戏：script 标签 + 菜单入口 + 首启自动引导 + 样式 + 配置。
所有锚点都要求"唯一命中"，任何一处对不上就整体不改（原子写入）。"""
from pathlib import Path

ROOT = Path(r"<PROJECT_ROOT>\radar-runner")
OLD = "?v=20261020e"
NEW = "?v=20261020f"


def patch(rel, pairs, append=None):
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    orig = s
    log = []
    for i, (old, new, want) in enumerate(pairs):
        n = s.count(old)
        assert n == want, "%s 锚点%d 期望 %d 处，实际 %d 处: %r" % (rel, i, want, n, old[:70])
        s = s.replace(old, new)
        log.append("anchor%d x%d" % (i, n))
    if append:
        if not s.endswith("\n"):
            s += "\n"
        s += append
        log.append("append %d chars" % len(append))
    assert s != orig
    p.write_text(s, encoding="utf-8", newline="\n")
    print("%-12s %6d -> %6d chars  [%s]" % (rel, len(orig), len(s), ", ".join(log)))


# ============================================================ index.html
MENU_ROW = (
    '\n\n            <div class="calib-menu-row">\n'
    '              <button id="btn-calib" class="ghost-btn calib-open-btn" type="button">校准雷达动作</button>\n'
    '              <span id="calib-status" class="calib-status"></span>\n'
    '            </div>'
)

patch("index.html", [
    (OLD, NEW, 16),
    ('<script src="./js/DebugPanel.js?v=20261020f"></script>',
     '<script src="./js/DebugPanel.js?v=20261020f"></script>\n'
     '<script src="./js/Calibration.js?v=20261020f"></script>', 1),
    ('            <div id="radar-url-note" class="radar-note"></div>',
     '            <div id="radar-url-note" class="radar-note"></div>' + MENU_ROW, 1),
])

# ============================================================ main.js
BLOCK = r"""    // ---------- 雷达开局校准（新手教程，js/Calibration.js） ----------
    // 现场测出方向符号 + 静息基线，修正后续所有帧的 x（x' = (x - x0) * sign）。
    // 装配全在这里，Game.js / InputSource.js 都不用改。
    var wizard = null;
    var wrappedSrc = null;
    var autoAsked = false;

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
        onClose: function () { updateCalibNote(); }
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
          if (!calibSaved()) ensureWizard().open(currentSource());
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
"""

patch("main.js", [
    ("    var initialInput = queryParam('input') || game.savedInputId;\n"
     "    game.setInputSource(initialInput);",
     BLOCK.rstrip("\n"), 1),
    ("        var id = btn.getAttribute('data-input');\n"
     "        if (!id) return;\n"
     "        game.setInputSource(id);",
     "        var id = btn.getAttribute('data-input');\n"
     "        if (!id) return;\n"
     "        game.setInputSource(id);\n"
     "        syncCalibration();", 1),
    ("    bind('debug-close', function () { game.debug.setVisible(false); });",
     "    bind('debug-close', function () { game.debug.setVisible(false); });\n\n"
     "    // 雷达校准（新手教程）入口\n"
     "    bind('btn-calib', function () { openCalibration(); });", 1),
])

# ============================================================ config.js
patch("js/config.js", [
    ("      replayAzWin: null       // 方位滑动窗长度（帧，10 帧 ≈ 1.3s @7.72Hz）\n    },",
     "      replayAzWin: null,      // 方位滑动窗长度（帧，10 帧 ≈ 1.3s @7.72Hz）\n"
     "\n"
     "      // ---- 开局校准（新手教程，js/Calibration.js）：现场测符号 + 基线 ----\n"
     "      // 为什么必须现场测：组员的 lr-move 数据里用 px_delta 符号判左右只有 53.3% 准确率，\n"
     "      // 且静息基线已经偏到 +0.53~+0.97；符号/基线随站位与安装角度变化，没人能替玩家定。\n"
     "      calib: {\n"
     "        autoOpen: true,        // 选了雷达且没有可用校准时，自动弹出向导（每页一次）\n"
     "        baseFrames: 8,         // 第 1 步：连续多少帧算「站好了」\n"
     "        baseStdMax: 0.18,      // 第 1 步：这 8 帧 x 的标准差上限\n"
     "        moveDelta: 0.35,       // 第 2/3 步：相对基线的位移阈值（贴着进道阈值 0.36）\n"
     "        holdFrames: 3,         // 第 2/3 步：位移要连续保持几帧（@7.75Hz ≈ 390ms）\n"
     "        minSpread: 0.5,        // 左右位移差下限（默认参数下数学下界 0.70，仅作安全网）\n"
     "        stepTimeoutMs: 25000   // 单步超时（超时自动重试）\n"
     "      }\n"
     "    },", 1),
])

# ============================================================ style.css
CSS = """
/* ================= 雷达开局校准（新手教程，js/Calibration.js） ================= */

.calib-menu-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.calib-open-btn {
  padding: 8px 14px;
  font-size: 13px;
}

.calib-status { font-size: 12px; color: var(--ink-dim); }
.calib-status.is-ok { color: var(--moss); }

.calib-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: calc(var(--safe-t) + 16px) calc(var(--safe-r) + 16px) calc(var(--safe-b) + 16px) calc(var(--safe-l) + 16px);
  background: radial-gradient(circle at 50% 40%, rgba(28, 52, 22, 0.9), rgba(6, 12, 5, 0.97));
  z-index: 40;
  overflow-y: auto;
}

.calib-overlay.hidden { display: none; }

.calib-card {
  width: 100%;
  max-width: 460px;
  padding: 22px 22px 18px;
  border: 1px solid var(--panel-line);
  border-radius: 18px;
  background: var(--panel);
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.6);
  text-align: center;
}

.calib-title { font-size: 13px; letter-spacing: 4px; color: var(--ink-dim); }

.calib-step {
  margin-top: 8px;
  font-size: 22px;
  font-weight: 800;
  color: var(--gold);
  text-shadow: 0 2px 0 #8a6110;
}

.calib-text { margin-top: 10px; font-size: 15px; line-height: 1.6; color: var(--ink); }

.calib-msg { margin-top: 12px; min-height: 20px; font-size: 14px; color: var(--ink-dim); }
.calib-msg.calib-ok { color: var(--moss); }
.calib-msg.calib-warn { color: var(--boost); }
.calib-msg.calib-error { color: var(--blood); }

.calib-live {
  margin-top: 10px;
  padding: 7px 10px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.28);
  font: 12px/1.5 Consolas, "Courier New", monospace;
  color: var(--ink-dim);
}

.calib-dots { display: flex; justify-content: center; gap: 8px; margin: 14px 0 4px; }
.calib-dot { width: 10px; height: 10px; border-radius: 50%; background: rgba(255, 255, 255, 0.18); }
.calib-dot-done { background: var(--moss); }
.calib-dot-now { background: var(--gold); transform: scale(1.25); }

.calib-actions { display: flex; gap: 10px; justify-content: center; margin-top: 16px; flex-wrap: wrap; }

.calib-btn {
  font: inherit;
  font-size: 13px;
  padding: 9px 14px;
  border: 1px solid var(--panel-line);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--ink);
  cursor: pointer;
}

.calib-btn:active { transform: translateY(1px); }

@media (max-width: 560px) {
  .calib-card { padding: 18px 16px 14px; }
  .calib-step { font-size: 19px; }
  .calib-text { font-size: 14px; }
}
"""

patch("style.css", [], append=CSS)

print("wired")
