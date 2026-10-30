/**
 * 新手引导 —— 熊二快跑（整合版）
 *
 * 6 步把"怎么跑、怎么躲、道具怎么吃"讲完，最后一步会提示雷达动作校准。
 * 依赖 index.html 里的 #tutorial-overlay 结构，样式在 style.css 的「新手引导弹窗」段。
 *
 * 对外接口（main.js / 控制台都能用）：
 *   RR.Tutorial.show(onComplete)   打开引导；完成或跳过后回调 onComplete
 *   RR.Tutorial.maybeShow(cb)      没看过才打开，看过直接执行 cb
 *   RR.Tutorial.hide()             直接关掉（不触发回调）
 *   RR.Tutorial.isOpen()           是否正在显示
 *   RR.Tutorial.hasSeen()          本地是否已看过
 *   RR.Tutorial.markAsSeen()       标记已看过
 *   RR.Tutorial.reset()            清掉记录，下次重新引导
 *
 * 存档：localStorage 键名 bear-run:tutorial-seen
 */
window.RR = window.RR || {};

RR.Tutorial = (function () {
  'use strict';

  var STORAGE_KEY = 'bear-run:tutorial-seen';

  var steps = [
    {
      title: '🐻 欢迎来到狗熊岭',
      content:
        '<p>帮熊二躲开路上的障碍，甩掉后面追你的光头强！</p>' +
        '<p>跑得越远、蜂蜜吃得越多，分数就越高～</p>',
      btnText: '下一步'
    },
    {
      title: '⬅️ ➡️ 切换三条跑道',
      content:
        '<p><strong>键盘：</strong>按 ← → 或 A D</p>' +
        '<p><strong>触屏：</strong>左右滑动屏幕</p>' +
        '<p><strong>体感：</strong>身体向左 / 向右平移</p>' +
        '<p>⚠️ <strong>原木石头堆、滚动木桶</strong>只能换道绕开</p>',
      btnText: '下一步'
    },
    {
      title: '⬆️ 跳过低木栅栏',
      content:
        '<p><strong>键盘：</strong>按 ↑ / 空格 / W</p>' +
        '<p><strong>触屏：</strong>向上滑动</p>' +
        '<p><strong>体感：</strong>向上跳起</p>' +
        '<p>看到带黄黑警示条的木栅栏，跳过去就对了</p>',
      btnText: '下一步'
    },
    {
      title: '⬇️ 滑铲钻树干',
      content:
        '<p><strong>键盘：</strong>按 ↓ / S</p>' +
        '<p><strong>触屏：</strong>向下滑动</p>' +
        '<p><strong>体感：</strong>蹲下身子</p>' +
        '<p>横倒的粗树干挡路，滑铲仰面钻过去</p>',
      btnText: '下一步'
    },
    {
      title: '🍯 吃道具涨状态',
      content:
        '<p>🍯 <strong>蜂蜜罐</strong>：加分，接上蜂蜜滑道分数翻倍</p>' +
        '<p>🍎 <strong>红苹果</strong>：回复血量（最多 5 格）</p>' +
        '<p>❤️ 初始 3 条命，撞一次掉一格，受伤后短暂无敌</p>',
      btnText: '下一步'
    },
    {
      title: '准备好了吗？',
      content:
        '<p>掉血时光头强会冲上来贴身追，稳住别乱撞</p>' +
        '<p>📡 选了「雷达」输入源？关掉引导后会带你做 5 步动作校准</p>' +
        '<p>💡 体感时站在雷达正前方 1–2 米，动作舒展一点</p>' +
        '<p>按 P 或 Esc 可以随时暂停</p>',
      btnText: '🚀 开始快跑'
    }
  ];

  var currentStep = 0;
  var completeCallback = null;
  var open_ = false;
  var el = {};
  var ready = false;

  function $(id) { return document.getElementById(id); }

  function init() {
    if (ready) return;
    el.overlay = $('tutorial-overlay');
    if (!el.overlay) return;                       // 页面里没有引导结构就静默跳过

    el.title = $('tutorial-title');
    el.content = $('tutorial-content');
    el.dots = $('tutorial-dots');
    el.stepNo = $('tutorial-step-no');
    el.next = $('tutorial-next');
    el.skip = $('tutorial-skip');

    if (el.next) el.next.addEventListener('click', handleNext);
    if (el.skip) el.skip.addEventListener('click', handleSkip);
    if (el.dots) el.dots.addEventListener('click', onDotsClick);

    document.addEventListener('keydown', onKeyDown);

    buildDots();
    ready = true;
  }

  /** 打开时禁掉背景滚动/误触，结束再放开 */
  function lockBackground(on) {
    var app = document.getElementById('app');
    if (app) app.classList.toggle('tutorial-lock', !!on);
  }

  function buildDots() {
    if (!el.dots) return;
    var html = '';
    for (var i = 0; i < steps.length; i++) {
      html += '<button class="tutorial-dot" type="button" data-step="' + i +
        '" aria-label="第 ' + (i + 1) + ' 步"></button>';
    }
    el.dots.innerHTML = html;
  }

  function renderDots() {
    if (!el.dots) return;
    var dots = el.dots.querySelectorAll('.tutorial-dot');
    for (var i = 0; i < dots.length; i++) {
      var done = i < currentStep;
      dots[i].classList.toggle('is-done', done);
      dots[i].classList.toggle('is-now', i === currentStep);
      dots[i].setAttribute('aria-current', i === currentStep ? 'step' : 'false');
    }
    if (el.stepNo) el.stepNo.textContent = (currentStep + 1) + ' / ' + steps.length;
  }

  function renderStep() {
    var step = steps[currentStep];
    if (!step) return;

    if (el.title) el.title.textContent = step.title;
    if (el.content) el.content.innerHTML = step.content;
    if (el.next) el.next.textContent = step.btnText;
    if (el.skip) {
      el.skip.style.display = currentStep === steps.length - 1 ? 'none' : '';
      el.skip.textContent = '跳过引导';
    }
    renderDots();

    if (el.overlay) {
      el.overlay.setAttribute('aria-label', '新手指引：' + step.title);
      // 内容高度每步不同，回到顶部避免小屏溢出后停在中段
      var box = el.overlay.querySelector('.tutorial-box');
      if (box) box.scrollTop = 0;
    }
  }

  function handleNext() {
    if (currentStep < steps.length - 1) {
      currentStep++;
      renderStep();
    } else {
      finish();
    }
  }

  function handleSkip() {
    finish(true);
  }

  /** 完成 / 跳过：关掉弹窗并把控制权交还给调用方 */
  function finish(skipped) {
    open_ = false;
    hide();
    markAsSeen();
    var cb = completeCallback;
    completeCallback = null;
    if (typeof cb === 'function') cb({ skipped: !!skipped });
  }

  function onDotsClick(e) {
    var btn = e.target && e.target.closest ? e.target.closest('.tutorial-dot') : null;
    if (!btn) return;
    var idx = Number(btn.getAttribute('data-step'));
    if (!isFinite(idx) || idx < 0 || idx >= steps.length) return;
    currentStep = idx;
    renderStep();
  }

  function onKeyDown(e) {
    if (!open_) return;
    var k = e.key;
    if (k === 'Escape') {
      e.preventDefault();
      handleSkip();
    } else if (k === 'ArrowRight' || k === 'Enter' || k === ' ') {
      e.preventDefault();
      handleNext();
    }
  }

  function show(onComplete) {
    init();
    if (!el.overlay) return false;

    currentStep = 0;
    completeCallback = onComplete || null;
    open_ = true;
    renderStep();

    el.overlay.classList.remove('hidden');
    // 强制一次 reflow，保证从 hidden 到 show 的过渡真的播出来
    void el.overlay.offsetWidth;
    el.overlay.classList.add('tutorial-show');
    lockBackground(true);

    if (el.next) {
      try { el.next.focus({ preventScroll: true }); } catch (err) { el.next.focus(); }
    }
    return true;
  }

  function hide() {
    open_ = false;
    lockBackground(false);
    if (!el.overlay) return;
    el.overlay.classList.remove('tutorial-show');
    el.overlay.classList.add('hidden');
  }

  /** 没看过才打开；看过（或没有引导结构）直接把控制权交给 onComplete */
  function maybeShow(onComplete) {
    init();
    if (!el.overlay || hasSeen()) {
      if (typeof onComplete === 'function') onComplete({ skipped: true, alreadySeen: true });
      return false;
    }
    return show(onComplete);
  }

  function isOpen() { return open_; }

  function hasSeen() {
    try { return localStorage.getItem(STORAGE_KEY) === '1'; } catch (e) { return false; }
  }

  function markAsSeen() {
    try { localStorage.setItem(STORAGE_KEY, '1'); } catch (e) { /* file:// 下可能被限制 */ }
  }

  function reset() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) { /* 忽略 */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /** 跳到指定步（0 起）；越界忽略。外部调试 / 演示用 */
  function goto(index) {
    var i = Number(index);
    if (!open_ || !isFinite(i)) return false;
    i = Math.max(0, Math.min(steps.length - 1, Math.round(i)));
    currentStep = i;
    renderStep();
    return true;
  }

  function next() { if (open_) handleNext(); }

  function prev() {
    if (!open_ || currentStep <= 0) return;
    currentStep--;
    renderStep();
  }

  return {
    show: show,
    maybeShow: maybeShow,
    hide: hide,
    isOpen: isOpen,
    hasSeen: hasSeen,
    markAsSeen: markAsSeen,
    reset: reset,
    goto: goto,
    next: next,
    prev: prev,
    steps: steps
  };
})();
