/**
 * 表情小窗：HUD 左上角那只"The camera on your face"。
 *
 * 为什么要有它？
 *   游戏是第三人称背后视角，玩家 99% 的时间只能看到熊的后脑勺 —— 花在脸上的
 *   呆毛、腮红、眉毛、瞪眼、吐舌全白做了。所以这里用一块 2D 小画布，把角色的
 *   脸实时重画一遍，并且永远正对玩家：跑动眨眼、滞空瞪眼、滑铲眯眼吐舌、
 *   受击 X 眼、被拍扁成饼。
 *
 * 配色与五官全部读角色 spec（fur / belly / blush / face.eye / face.mouth / ...），
 * 所以换角色时这张脸会跟着换，和 3D 模型是同一套设定。
 *
 * 绘制坐标用 100×100 的设计空间，再按画布尺寸缩放，改尺寸不用重算任何数字。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG;
  var C = CFG.COLORS;

  var INK = '#2b2118';        // 眼珠 / 描边
  var WHITE = '#fdf8ee';      // 眼白
  var PINK = '#d9605a';       // 舌头

  function hex(n) {
    var s = (n === undefined || n === null ? 0 : n).toString(16);
    while (s.length < 6) s = '0' + s;
    return '#' + s;
  }

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  class FaceCam {
    /**
     * @param {HTMLCanvasElement} canvas
     * @param {number} size 设计尺寸（CSS 像素），默认 112
     */
    constructor(canvas, size) {
      this.cv = canvas;
      this.size = size || 112;
      this.dpr = Math.min(2, global.devicePixelRatio || 1);
      this.cv.width = Math.round(this.size * this.dpr);
      this.cv.height = Math.round(this.size * this.dpr);
      this.ctx = this.cv.getContext('2d');

      this.spec = CFG.getCharacter(CFG.DEFAULT_CHARACTER);
      this.state = 'running';
      this.speed = 0;

      this.t = 0;
      this.blink = 0;                        // 0 = 睁眼，1 = 闭眼
      this.blinkTimer = 1.6 + Math.random() * 2;
      this.hurtPulse = 0;                    // 受击红光的余韵
      this.deadT = 0;
      this.flatten = 0;                      // 被拍扁的程度 0~1
    }

    setCharacter(spec) {
      if (spec) this.spec = spec;
    }

    /** 'running' | 'airborne' | 'sliding' | 'hurt' | 'dead' | 'smashed' */
    setState(s) {
      if (this.state !== s) {
        this.state = s;
        if (s === 'dead' || s === 'smashed') this.deadT = 0;
      }
    }

    update(dt, speed) {
      this.t += dt;
      this.speed = speed || 0;

      // 眨眼：跑动时偶尔闭一下，滞空时死瞪着不眨
      var open = this.state !== 'airborne';
      if (open) {
        this.blinkTimer -= dt;
        if (this.blinkTimer <= 0) {
          this.blinkTimer = 1.8 + Math.random() * 2.6;
          this.blink = 1;
        }
      }
      if (this.blink > 0) this.blink = Math.max(0, this.blink - dt * 9);

      if (this.state === 'hurt') this.hurtPulse = 1;
      else if (this.hurtPulse > 0) this.hurtPulse = Math.max(0, this.hurtPulse - dt * 2.2);

      if (this.state === 'dead' || this.state === 'smashed') this.deadT += dt;
      this.flatten = clamp(this.flatten + (this.state === 'smashed' ? dt * 9 : -dt * 9), 0, 1);

      this.draw();
    }

    // ==================== 绘制 ====================

    draw() {
      var s = this.spec;
      var ctx = this.ctx;
      var k = (this.size * this.dpr) / 100;

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, this.cv.width, this.cv.height);
      ctx.save();
      ctx.scale(k, k);

      // 被拍扁：从底部压下去
      if (this.flatten > 0) {
        ctx.translate(0, 96 * this.flatten);
        ctx.scale(1 + 0.22 * this.flatten, 1 - 0.62 * this.flatten);
      }

      var fur = hex(s.fur);
      var furDark = hex(s.furDark);
      var belly = hex(s.belly);

      var running = this.state === 'running';
      var air = this.state === 'airborne';
      var slide = this.state === 'sliding';
      var dead = this.state === 'dead' || this.state === 'smashed';
      var hurt = this.state === 'hurt';

      // 跑动时的上下起伏 + 滑铲时歪头
      var bob = running ? Math.sin(this.t * 7.5) * 1.6 : (air ? -1.5 : 0);
      var tilt = slide ? -0.16 : (running ? Math.sin(this.t * 3.8) * 0.02 : 0);

      ctx.translate(50, 54 + bob);
      ctx.rotate(tilt);

      // ---- 耳朵 ----
      var earK = (s.build && s.build.ear ? s.build.ear : 1);
      var earR = 13.5 * (0.9 + earK * 0.1);
      [-1, 1].forEach(function (sg) {
        ctx.beginPath();
        ctx.arc(sg * 25, -29, earR, 0, Math.PI * 2);
        ctx.fillStyle = fur;
        ctx.fill();
        ctx.lineWidth = 3;
        ctx.strokeStyle = furDark;
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(sg * 25, -29, earR * 0.52, 0, Math.PI * 2);
        ctx.fillStyle = belly;
        ctx.fill();
      });

      // ---- 呆毛（跑动时甩） ----
      var tuft = (s.face && s.face.tuft) || 'floppy';
      var tuftWag = Math.sin(this.t * (running ? 5.2 : 2.4)) * 6;
      ctx.strokeStyle = furDark;
      ctx.lineWidth = 3.4;
      ctx.lineCap = 'round';
      if (tuft === 'spike') {
        for (var i = -1; i <= 1; i++) {
          ctx.beginPath();
          ctx.moveTo(i * 5, -30);
          ctx.lineTo(i * 5 + tuftWag * 0.5, -42 - Math.abs(i) * -2);
          ctx.stroke();
        }
      } else {
        ctx.beginPath();
        ctx.moveTo(-3, -30);
        ctx.quadraticCurveTo(tuftWag, -48, 8 + tuftWag * 1.4, -42);
        ctx.stroke();
      }

      // ---- 头 ----
      ctx.beginPath();
      ctx.ellipse(0, 0, 35, 33, 0, 0, Math.PI * 2);
      ctx.fillStyle = fur;
      ctx.fill();
      ctx.lineWidth = 3;
      ctx.strokeStyle = furDark;
      ctx.stroke();

      // ---- 腮红 ----
      if (s.hasBlush) {
        ctx.globalAlpha = slide || air ? 0.72 : 0.5;
        [-1, 1].forEach(function (sg) {
          ctx.beginPath();
          ctx.ellipse(sg * 24, 10, 7.5, 4.6, 0, 0, Math.PI * 2);
          ctx.fillStyle = hex(s.blush);
          ctx.fill();
        });
        ctx.globalAlpha = 1;
      }

      // ---- 吻部 ----
      ctx.beginPath();
      ctx.ellipse(0, 13, 21, 15.5, 0, 0, Math.PI * 2);
      ctx.fillStyle = belly;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = furDark;
      ctx.stroke();

      // ---- 鼻子 ----
      ctx.beginPath();
      ctx.ellipse(0, 5.5, 7.2, 5.2, 0, 0, Math.PI * 2);
      ctx.fillStyle = furDark;
      ctx.fill();

      // ---- 嘴 / 舌 ----
      this._drawMouth(ctx, furDark, { running: running, air: air, slide: slide, dead: dead, hurt: hurt });

      // ---- 眼睛 ----
      this._drawEyes(ctx, s, { running: running, air: air, slide: slide, dead: dead, hurt: hurt });

      // ---- 眉毛 ----
      this._drawBrows(ctx, s, { air: air, slide: slide, dead: dead, hurt: hurt });

      // ---- 受击红光 ----
      if (this.hurtPulse > 0.02) {
        ctx.globalAlpha = this.hurtPulse * 0.4;
        ctx.beginPath();
        ctx.ellipse(0, 0, 35, 33, 0, 0, Math.PI * 2);
        ctx.fillStyle = '#e33a2c';
        ctx.fill();
        ctx.globalAlpha = 1;
      }

      ctx.restore();
    }

    _drawMouth(ctx, furDark, st) {
      ctx.strokeStyle = furDark;
      ctx.lineWidth = 3;
      ctx.lineCap = 'round';

      if (st.dead) {
        // 被拍扁：张大嘴，舌头整个吐出来
        ctx.beginPath();
        ctx.ellipse(0, 16, 9, 6.5, 0, 0, Math.PI * 2);
        ctx.fillStyle = '#7a2b22';
        ctx.fill();
        ctx.beginPath();
        ctx.ellipse(0, 21, 6.5, 7.5, 0, 0, Math.PI * 2);
        ctx.fillStyle = PINK;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#a84a41';
        ctx.stroke();
        return;
      }

      if (st.slide) {
        // 滑铲：咧到大开的笑，舌头甩在旁边
        ctx.beginPath();
        ctx.moveTo(-11, 12);
        ctx.quadraticCurveTo(0, 24, 11, 12);
        ctx.stroke();
        ctx.beginPath();
        ctx.ellipse(4, 20, 7, 8, 0.25, 0, Math.PI * 2);
        ctx.fillStyle = PINK;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#a84a41';
        ctx.stroke();
        return;
      }

      if (st.air) {
        // 滞空：喔——张嘴 + 舌头
        ctx.beginPath();
        ctx.ellipse(0, 15, 7.5, 8.5, 0, 0, Math.PI * 2);
        ctx.fillStyle = '#7a2b22';
        ctx.fill();
        ctx.beginPath();
        ctx.ellipse(0, 20, 5.4, 5.6, 0, 0, Math.PI * 2);
        ctx.fillStyle = PINK;
        ctx.fill();
        return;
      }

      if (st.hurt) {
        // 受击：波浪嘴
        ctx.beginPath();
        ctx.moveTo(-9, 16);
        ctx.quadraticCurveTo(-4.5, 12.5, 0, 16);
        ctx.quadraticCurveTo(4.5, 19.5, 9, 16);
        ctx.stroke();
        return;
      }

      // 跑动：小小的咧嘴
      ctx.beginPath();
      ctx.moveTo(-8, 14);
      ctx.quadraticCurveTo(0, 20, 8, 14);
      ctx.stroke();
    }

    _drawEyes(ctx, s, st) {
      var narrow = s.face && s.face.eye === 'narrow';
      var ex = 14;
      var ey = -4;
      var rx = 8.2;
      var ry = narrow ? 5.1 : 9.2;

      if (st.dead) {
        // 蚊香眼
        [-1, 1].forEach(function (sg) {
          ctx.strokeStyle = INK;
          ctx.lineWidth = 2.4;
          ctx.beginPath();
          for (var a = 0; a < Math.PI * 5; a += 0.16) {
            var r = 1.2 + a * 0.72;
            var px = sg * ex + Math.cos(a) * r;
            var py = ey + Math.sin(a) * r;
            if (a === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
          }
          ctx.stroke();
        });
        return;
      }

      if (st.hurt) {
        // X_X
        [-1, 1].forEach(function (sg) {
          ctx.strokeStyle = INK;
          ctx.lineWidth = 3.2;
          ctx.lineCap = 'round';
          ctx.beginPath();
          ctx.moveTo(sg * ex - 6, ey - 6);
          ctx.lineTo(sg * ex + 6, ey + 6);
          ctx.moveTo(sg * ex + 6, ey - 6);
          ctx.lineTo(sg * ex - 6, ey + 6);
          ctx.stroke();
        });
        return;
      }

      var openY = ry;
      if (this.blink > 0.02) openY = ry * (1 - this.blink * 0.92);
      if (st.slide) openY = ry * 0.34;         // 眯眼爽飞
      if (st.air) { rx *= 1.16; openY = ry * 1.2; }

      [-1, 1].forEach(function (sg) {
        // 眼白
        ctx.beginPath();
        ctx.ellipse(sg * ex, ey, rx, openY, 0, 0, Math.PI * 2);
        ctx.fillStyle = WHITE;
        ctx.fill();
        ctx.lineWidth = 2.2;
        ctx.strokeStyle = INK;
        ctx.stroke();

        // 眼珠（往镜头方向偏一点点，看着更有神）
        if (openY > 1.4) {
          var pr = st.air ? 2.5 : 3.4;
          ctx.beginPath();
          ctx.arc(sg * ex + sg * 1.1, ey + (st.air ? -1 : 0.6), pr, 0, Math.PI * 2);
          ctx.fillStyle = INK;
          ctx.fill();
          ctx.beginPath();
          ctx.arc(sg * ex + sg * 1.1 - 1, ey - 1.1, pr * 0.34, 0, Math.PI * 2);
          ctx.fillStyle = WHITE;
          ctx.fill();
        }
      });
    }

    _drawBrows(ctx, s, st) {
      var bw = (s.browW || 0.12) * 62;
      var bh = Math.max(2.6, (s.browH || 0.04) * 62);
      var flat = s.face && s.face.brow === 'flat';
      var y = st.air ? -17 : (st.slide ? -19 : -16);
      var lift = st.air ? -1.5 : 0;

      ctx.fillStyle = hex(s.furDark);
      [-1, 1].forEach(function (sg) {
        ctx.save();
        ctx.translate(sg * 14, y + lift);
        // 受击 / 睁大眼时眉毛往上挑
        var rot = flat ? 0 : sg * (st.air ? -0.16 : -0.08);
        if (st.slide) rot = sg * 0.14;
        ctx.rotate(rot);
        ctx.beginPath();
        this._roundRect(ctx, -bw / 2, -bh / 2, bw, bh, bh / 2);
        ctx.fill();
        ctx.restore();
      }, this);
    }

    _roundRect(ctx, x, y, w, h, r) {
      var rr = Math.min(r, h / 2, w / 2);
      ctx.moveTo(x + rr, y);
      ctx.lineTo(x + w - rr, y);
      ctx.arcTo(x + w, y, x + w, y + rr, rr);
      ctx.lineTo(x + w, y + h - rr);
      ctx.arcTo(x + w, y + h, x + w - rr, y + h, rr);
      ctx.lineTo(x + rr, y + h);
      ctx.arcTo(x, y + h, x, y + h - rr, rr);
      ctx.lineTo(x, y + rr);
      ctx.arcTo(x, y, x + rr, y, rr);
    }
  }

  RR.FaceCam = FaceCam;

})(window);
