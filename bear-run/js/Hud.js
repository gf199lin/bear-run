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

        damageFlash: $('damage-flash'),
        touchHint: $('touch-hint'),
        vickShout: $('vick-shout'),

        regionBanner: $('region-banner'),
        faceCam: $('face-cam'),
        faceCanvas: $('face-canvas')
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

    setStage(index, label) {
      if (this._lastStage === index) return;
      this._lastStage = index;
      this.el.stage.textContent = label || ('第 ' + (index + 1) + ' 区');
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
      this.el.overlayOver.classList.remove('hidden');
    }

    hideOver() {
      this.el.overlayOver.classList.add('hidden');
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

    // ---------- 角色选择 ----------

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
