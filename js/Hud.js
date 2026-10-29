/**
 * HUD：只负责读写 DOM，不含任何玩法逻辑。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;

  function $(id) { return document.getElementById(id); }

  class Hud {
    constructor() {
      this.el = {
        hearts: $('hearts'),
        boostTag: $('boost-tag'),
        score: $('stat-score'),
        best: $('stat-best'),
        distance: $('stat-distance'),
        coins: $('stat-coins'),
        speed: $('stat-speed'),
        stage: $('stat-stage'),
        input: $('stat-input'),

        overlayStart: $('overlay-start'),
        overlayPause: $('overlay-pause'),
        overlayOver: $('overlay-over'),

        startBest: $('start-best'),
        radarNote: $('radar-url-note'),
        inputPicker: $('input-picker'),
        charPicker: $('char-picker'),
        charNote: $('char-note'),

        overScore: $('over-score'),
        overDistance: $('over-distance'),
        overCoins: $('over-coins'),
        overBest: $('over-best'),
        overNewBest: $('over-newbest'),
        overCombo: $('over-combo'),
        overBestDistance: $('over-best-distance'),
        overBadges: $('over-badges'),
        overChallenge: $('over-challenge'),
        overChallengeState: $('over-challenge-state'),
        overChallengeName: $('over-challenge-name'),
        overChallengeText: $('over-challenge-text'),
        overHighlights: $('over-highlights'),
        overNext: $('over-next'),
        overHistory: $('over-history'),
        overHistoryList: $('over-history-list'),

        comboBadge: $('combo-badge'),
        comboMult: $('combo-mult'),
        comboCount: $('combo-count'),
        comboBarFill: $('combo-bar-fill'),
        nearMissToast: $('nearmiss-toast'),
        nearMissClose: $('nearmiss-close'),

        challengeTag: $('challenge-tag'),
        challengeName: $('challenge-name'),
        challengeProg: $('challenge-progress'),
        challengeBarFill: $('challenge-bar-fill'),
        challengeToast: $('challenge-toast'),

        damageFlash: $('damage-flash'),
        touchHint: $('touch-hint'),
        vickShout: $('vick-shout'),

        regionBanner: $('region-banner'),
        faceCam: $('face-cam'),
        faceCanvas: $('face-canvas'),

        testBadge: $('test-badge'),
        testHint: $('test-hint')
      };

      this._heartEls = [];
      this._buildHearts(CFG.HP.max);

      this._flashTimer = 0;
      this._hintTimer = 0;
      this._boostOn = false;
      this._lastStage = -1;
      this._regionTimer = 0;

      // 表情小窗（画布可能不存在 —— 老页面 / 极端降级时不要让 HUD 崩掉）
      this.faceCam = null;
      if (this.el.faceCanvas && RR.FaceCam) {
        this.faceCam = new RR.FaceCam(this.el.faceCanvas, 112);
      }
    }

    _buildHearts(n) {
      this.el.hearts.innerHTML = '';
      this._heartEls = [];
      for (var i = 0; i < n; i++) {
        var d = document.createElement('div');
        d.className = 'heart';
        this.el.hearts.appendChild(d);
        this._heartEls.push(d);
      }
    }

    setHearts(hp, max) {
      // 只显示"玩家当前拥有的上限"：开局 3 格满血，吃到医疗箱才长出第 4、5 格
      var shown = Math.max(CFG.HP.start, hp);
      if (this._heartEls.length !== shown) this._buildHearts(shown);
      for (var i = 0; i < this._heartEls.length; i++) {
        var cls = 'heart' + (i < hp ? '' : ' lost');
        this._heartEls[i].className = cls;
      }
    }

    setScore(v) { this.el.score.textContent = String(v); }
    setBest(v) { this.el.best.textContent = String(v); }
    setDistance(v) { this.el.distance.textContent = String(Math.floor(v)); }
    setCoins(v) { this.el.coins.textContent = String(v); }
    setSpeed(v) { this.el.speed.textContent = v.toFixed(1); }

    setStage(index, label, force) {
      if (!force && this._lastStage === index) return;
      this._lastStage = index;
      this.el.stage.textContent = label || ('第 ' + (index + 1) + ' 区');
    }

    // ---------- 测试模式（雷达联调） ----------

    /** 顶部徽标：进测试局就亮，回主菜单灭 */
    setTestMode(on) {
      this._testMode = !!on;
      if (this.el.testBadge) this.el.testBadge.classList.toggle('hidden', !on);
      if (!on && this.el.testHint) this.el.testHint.classList.add('hidden');
    }

    /** 测试局进场提示：说清楚"不会死"，几秒后自动收，不挡视线 */
    showTestHint() {
      var el = this.el.testHint;
      if (!el || !this._testMode) return;
      el.classList.add('hidden');
      void el.offsetWidth;              // 重排 -> 重播淡入淡出
      el.classList.remove('hidden');
      var self = this;
      clearTimeout(this._testHintTimer);
      this._testHintTimer = setTimeout(function () {
        el.classList.add('hidden');
      }, 5000);
    }

    setInput(name) { this.el.input.textContent = name; }

    setBoost(on) {
      if (this._boostOn === on) return;
      this._boostOn = on;
      this.el.boostTag.classList.toggle('hidden', !on);
    }

    setStartBest(v) { this.el.startBest.textContent = String(v); }
    setRadarNote(text) { this.el.radarNote.textContent = text || ''; }

    flashDamage() {
      this.el.damageFlash.classList.remove('on');
      // 强制重排，保证连续受伤也能重播动画
      void this.el.damageFlash.offsetWidth;
      this.el.damageFlash.classList.add('on');
      var self = this;
      clearTimeout(this._flashTimer);
      this._flashTimer = setTimeout(function () {
        self.el.damageFlash.classList.remove('on');
      }, 200);
    }

    showTouchHint(ms) {
      var el = this.el.touchHint;
      el.classList.add('hidden');
      void el.offsetWidth;              // 重排 -> 重播淡入淡出动画
      el.classList.remove('hidden');
      var self = this;
      clearTimeout(this._hintTimer);
      this._hintTimer = setTimeout(function () {
        el.classList.add('hidden');
      }, ms || 3400);
    }

    // ---------- 光头强的处决字幕 ----------

    showVickShout() {
      var el = this.el.vickShout;
      if (!el) return;
      el.classList.add('hidden');
      void el.offsetWidth;              // 重排 -> 重播缩放入场动画
      el.classList.remove('hidden');
    }

    hideVickShout() {
      if (this.el.vickShout) this.el.vickShout.classList.add('hidden');
    }

    // ---------- 进入新区域的横幅 ----------

    /**
     * @param {string} name 区域名，如「第 2 区 · 密林深处」
     * @param {string} sub  一句话提示（可为空）
     */
    showRegionBanner(name, sub) {
      var el = this.el.regionBanner;
      if (!el) return;

      el.querySelector('.region-banner-name').textContent = name || '';
      var subEl = el.querySelector('.region-banner-sub');
      subEl.textContent = sub || '';
      subEl.classList.toggle('hidden', !sub);

      el.classList.add('hidden');
      void el.offsetWidth;              // 重排 -> 保证每次都能重播动画
      el.classList.remove('hidden');

      var self = this;
      clearTimeout(this._regionTimer);
      this._regionTimer = setTimeout(function () {
        el.classList.add('hidden');
      }, 3200);
    }

    // ---------- 表情小窗 ----------

    setFaceCharacter(spec) {
      if (this.faceCam) this.faceCam.setCharacter(spec);
    }

    /** @param {string} state running | airborne | sliding | hurt | dead | smashed */
    setFaceState(state) {
      if (this.faceCam) this.faceCam.setState(state);
    }

    updateFaceCam(dt, speed) {
      if (this.faceCam) this.faceCam.update(dt, speed);
    }

    // ---------- 覆盖层 ----------

    showStart(best) {
      this.setStartBest(best);
      this.hideCombo();
      this.hideChallenge();
      this.el.overlayStart.classList.remove('hidden');
      this.el.overlayPause.classList.add('hidden');
      this.el.overlayOver.classList.add('hidden');
      document.body.classList.remove('playing');
    }

    hideStart() {
      this.el.overlayStart.classList.add('hidden');
      document.body.classList.add('playing');
    }

    showPause() {
      this.el.overlayPause.classList.remove('hidden');
    }

    hidePause() {
      this.el.overlayPause.classList.add('hidden');
    }

    showOver(data) {
      this.el.overScore.textContent = String(data.score);
      this.el.overDistance.textContent = Math.floor(data.distance) + ' m';
      this.el.overCoins.textContent = String(data.coins);
      this.el.overBest.textContent = String(data.best);
      this.el.overNewBest.classList.toggle('hidden', !data.isNewBest);

      // 任务 4：最长连击 / 最佳距离 / 破纪录徽标 / 挑战小结 / 亮点 / 下一局目标 / 历史
      var rec = data.records || {};
      if (this.el.overCombo) this.el.overCombo.textContent = String(data.maxCombo || 0);
      if (this.el.overBestDistance) {
        this.el.overBestDistance.textContent = Math.floor(rec.bestDistance || 0) + ' m';
      }
      this._showOverBadges(data);
      this._showOverChallenge(data.challenge);
      this._showOverHighlights(data.highlights);
      this._showOverNext(data.nextGoal);
      this._showOverHistory(data.history);

      this.el.overlayOver.classList.remove('hidden');
    }

    hideOver() {
      this.el.overlayOver.classList.add('hidden');
    }

    // ---------- 结算页：亮点 / 挑战 / 目标 / 历史（任务 4） ----------

    /** 破纪录徽标：只列本局真破了的项 */
    _showOverBadges(data) {
      var box = this.el.overBadges;
      if (!box) return;

      var beats = data.beats || {};
      var names = [
        ['score', '最高分'],
        ['distance', '最佳距离'],
        ['combo', '最长连击'],
        ['nearMiss', '最多险躲']
      ];
      var html = '';
      for (var i = 0; i < names.length; i++) {
        if (!beats[names[i][0]]) continue;
        html += '<span class="record-badge">' + names[i][1] + '破纪录</span>';
      }
      box.innerHTML = html;
      box.classList.toggle('hidden', !html);
    }

    _showOverChallenge(c) {
      var box = this.el.overChallenge;
      if (!box) return;
      if (!c) { box.classList.add('hidden'); return; }

      box.classList.remove('hidden');
      box.classList.toggle('done', !!c.done);
      if (this.el.overChallengeState) {
        this.el.overChallengeState.textContent = c.done ? '已完成' : '未完成';
      }
      if (this.el.overChallengeName) this.el.overChallengeName.textContent = c.label;
      if (this.el.overChallengeText) {
        var tail = c.done ? ' · +' + c.bonus + ' 分' : '（' + c.progress + ' / ' + c.target + '）';
        this.el.overChallengeText.textContent = (c.text || c.label) + tail;
      }
    }

    _showOverHighlights(list) {
      var box = this.el.overHighlights;
      if (!box) return;

      var items = list || [];
      box.textContent = '';
      for (var i = 0; i < items.length; i++) {
        var chip = document.createElement('span');
        chip.className = 'hl-chip';
        chip.textContent = items[i];
        box.appendChild(chip);
      }
      box.classList.toggle('hidden', !items.length);
    }

    _showOverNext(goal) {
      var box = this.el.overNext;
      if (!box) return;
      if (!goal || !goal.text) { box.classList.add('hidden'); return; }

      box.classList.remove('hidden');
      box.classList.toggle('all', goal.metric === 'all');
      box.textContent = goal.text;
    }

    /** 历史成绩：数值全部走 textContent 落进去（本地存档被改过也不会变成注入点） */
    _showOverHistory(list) {
      var box = this.el.overHistory;
      var host = this.el.overHistoryList;
      if (!box || !host) return;

      var rows = list || [];
      host.textContent = '';
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        var row = document.createElement('div');
        row.className = 'history-row';

        var score = document.createElement('b');
        score.textContent = r.score + ' 分';
        var info = document.createElement('span');
        info.textContent = r.distance + ' 米 · ' + r.combo + ' 连击 · 险躲 ' + r.nearMiss;

        row.appendChild(score);
        row.appendChild(info);
        if (r.won) {
          var won = document.createElement('i');
          won.className = 'history-won';
          won.textContent = '挑战达成';
          row.appendChild(won);
        }
        host.appendChild(row);
      }
      box.classList.toggle('hidden', !rows.length);
    }

    hideAllOverlays() {
      this.el.overlayStart.classList.add('hidden');
      this.el.overlayPause.classList.add('hidden');
      this.el.overlayOver.classList.add('hidden');
    }

    // ---------- 输入源选择 ----------

    markInputChoice(id) {
      var chips = this.el.inputPicker.querySelectorAll('.chip');
      for (var i = 0; i < chips.length; i++) {
        chips[i].classList.toggle('active', chips[i].getAttribute('data-input') === id);
      }
    }

    // ---------- 连击与险躲 ----------

    /**
     * @param {number} count 当前连击数
     * @param {number} level 倍率档位（0 = x1.00）
     * @param {number} ratio 连击剩余时间比例 0~1
     */
    setCombo(count, level, ratio) {
      var el = this.el.comboBadge;
      if (!el) return;

      // 1 连不显示：吃第一颗就弹徽标太吵，够 2 连才算"连上了"
      if (count < 2) { this.hideCombo(); return; }
      el.classList.remove('hidden');

      var mul = 1 + level * CFG.COMBO.step;
      if (this._comboMul !== mul) {
        this._comboMul = mul;
        this.el.comboMult.textContent = 'x' + mul.toFixed(2);
        el.classList.toggle('is-max', level >= CFG.COMBO.maxStep);
        el.classList.remove('pop');
        void el.offsetWidth;        // 重排 -> 每次升档都能重播那一下弹跳
        el.classList.add('pop');
      }

      if (this._comboCount !== count) {
        this._comboCount = count;
        this.el.comboCount.textContent = String(count);
      }

      var r = ratio < 0 ? 0 : (ratio > 1 ? 1 : ratio);
      this.el.comboBarFill.style.width = Math.round(r * 100) + '%';
    }

    hideCombo() {
      if (!this.el.comboBadge) return;
      this.el.comboBadge.classList.add('hidden');
      this._comboMul = null;        // 下次再出现时，倍率一定重写一遍
      this._comboCount = null;
    }

    /** 险躲避过：close = 余量极小，文案升级成"毫厘之间" */
    flashNearMiss(close) {
      var el = this.el.nearMissToast;
      if (!el) return;
      this.el.nearMissClose.textContent = close ? '毫厘之间' : '';
      this.el.nearMissClose.classList.toggle('hidden', !close);

      el.classList.add('hidden');
      void el.offsetWidth;          // 重排 -> 连续险躲也能次次重播
      el.classList.remove('hidden');

      var self = this;
      clearTimeout(this._nearMissTimer);
      this._nearMissTimer = setTimeout(function () {
        el.classList.add('hidden');
      }, 900);                      // 与 CSS 的 900ms 动画对齐
    }

    // ---------- 角色选择 ----------

    // ---------- 每局随机挑战（任务 3） ----------

    /**
     * @param {object} c    CFG.CHALLENGES 里的一条（null = 收起）
     * @param {number} now  当前进度值
     */
    setChallenge(c, now) {
      var el = this.el.challengeTag;
      if (!el || !c) { this.hideChallenge(); return; }

      if (this._challengeId !== c.id) {
        this._challengeId = c.id;
        this._challengeTxt = null;
        el.classList.remove('hidden');
        el.classList.remove('done');
        if (this.el.challengeName) this.el.challengeName.textContent = c.label;
        el.classList.remove('pop');
        void el.offsetWidth;            // 重排 -> 换挑战时重播一次入场弹跳
        el.classList.add('pop');
      }
      el.classList.remove('hidden');

      // 已经完成：别把"完成 +300"覆写回进度条
      if (el.classList.contains('done')) return;

      var p = now > c.target ? c.target : (now < 0 ? 0 : now);
      var txt = Math.floor(p) + ' / ' + c.target;
      if (this._challengeTxt !== txt) {
        this._challengeTxt = txt;
        if (this.el.challengeProg) this.el.challengeProg.textContent = txt;
        if (this.el.challengeBarFill) {
          this.el.challengeBarFill.style.width = Math.round((p / c.target) * 100) + '%';
        }
      }
    }

    /** 挑战达标：徽标切成完成态 + 画面中间弹一条 1.8s 的提示 */
    completeChallenge(c) {
      var el = this.el.challengeTag;
      if (el) {
        el.classList.remove('hidden');
        el.classList.add('done');
        // 把当前 id 记上：否则同一次刷新里 setChallenge() 会以为"换挑战了"，
        // 先把 done 抹掉、再把进度按当前值写回，刚显示的"完成 +N"就没了
        this._challengeId = c.id;
        if (this.el.challengeProg) this.el.challengeProg.textContent = '完成 +' + c.bonus;
        if (this.el.challengeBarFill) this.el.challengeBarFill.style.width = '100%';
      }

      var toast = this.el.challengeToast;
      if (!toast) return;
      var nameEl = toast.querySelector('.challenge-toast-name');
      var bonusEl = toast.querySelector('.challenge-toast-bonus');
      if (nameEl) nameEl.textContent = c.label;
      if (bonusEl) bonusEl.textContent = '+' + c.bonus;

      toast.classList.add('hidden');
      void toast.offsetWidth;           // 重排 -> 每次达标都能重播动画
      toast.classList.remove('hidden');

      var self = this;
      clearTimeout(this._challengeToastTimer);
      this._challengeToastTimer = setTimeout(function () {
        toast.classList.add('hidden');
      }, 1800);                         // 与 CSS 的 1800ms 动画对齐
    }

    hideChallenge() {
      this._challengeId = null;
      this._challengeTxt = null;
      if (!this.el.challengeTag) return;
      this.el.challengeTag.classList.add('hidden');
      this.el.challengeTag.classList.remove('done');
      if (this.el.challengeToast) this.el.challengeToast.classList.add('hidden');
    }

    markCharChoice(id) {
      if (!this.el.charPicker) return;
      var chips = this.el.charPicker.querySelectorAll('.chip');
      for (var i = 0; i < chips.length; i++) {
        chips[i].classList.toggle('active', chips[i].getAttribute('data-char') === id);
      }
    }

    setCharNote(text) {
      if (this.el.charNote) this.el.charNote.textContent = text || '';
    }
  }

  RR.Hud = Hud;

})(window);
