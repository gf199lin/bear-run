/**
 * 雷达调试面板：把输入层的原始数据直接摊开给标定用。
 * 真机联调时，这个面板是判断"角度粗"还是"人真的没动"的唯一依据。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};

  function $(id) { return document.getElementById(id); }

  class DebugPanel {
    constructor() {
      this.panel = $('debug-panel');
      this.visible = false;

      this.f = {
        source: $('dbg-source'),
        conn: $('dbg-conn'),
        fps: $('dbg-fps'),
        rate: $('dbg-rate'),
        latency: $('dbg-latency'),
        x: $('dbg-x'),
        height: $('dbg-height'),
        conf: $('dbg-conf'),
        jump: $('dbg-jump'),
        crouch: $('dbg-crouch'),
        lane: $('dbg-lane'),
        events: $('dbg-events'),
        speed: $('dbg-speed'),
        count: $('dbg-count')
      };

      this.eventLog = [];
      this.acc = 0;
      this.hz = 30;
    }

    toggle() { this.setVisible(!this.visible); }

    setVisible(v) {
      this.visible = !!v;
      this.panel.classList.toggle('hidden', !this.visible);
    }

    logEvent(text) {
      this.eventLog.push(text);
      if (this.eventLog.length > 3) this.eventLog.shift();
      if (this.visible) this.f.events.textContent = this.eventLog.join(' / ');
    }

    update(info) {
      this.acc += info.dt;
      if (this.acc < 1 / this.hz) return;
      var acc = this.acc;
      this.acc = 0;

      var f = this.f;
      f.source.textContent = info.source || '-';
      f.conn.textContent = info.connection || '-';
      f.fps.textContent = info.fps ? info.fps.toFixed(0) + ' fps' : '-';
      f.rate.textContent = info.radarRate ? info.radarRate.toFixed(1) + ' Hz' : '-';
      f.latency.textContent = (info.latency !== null && info.latency !== undefined)
        ? info.latency.toFixed(0) + ' ms' : '-';

      var fr = info.frame;
      if (fr) {
        f.x.textContent = num(fr.x, 2);
        f.height.textContent = num(fr.height, 2);
        f.conf.textContent = num(fr.confidence, 2);
        f.jump.textContent = fr.jump ? 'YES' : 'no';
        f.crouch.textContent = fr.crouch ? 'YES' : 'no';
      } else {
        f.x.textContent = '-';
        f.height.textContent = '-';
        f.conf.textContent = '-';
        f.jump.textContent = '-';
        f.crouch.textContent = '-';
      }

      f.lane.textContent = (info.lane === null || info.lane === undefined) ? '-' : ('L' + info.lane);
      f.events.textContent = this.eventLog.length ? this.eventLog.join(' / ') : '-';
      f.speed.textContent = info.speed ? info.speed.toFixed(1) + ' m/s' : '-';
      f.count.textContent = String(info.objects || 0);

      void acc;
    }
  }

  function num(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '-';
    return Number(v).toFixed(digits);
  }

  RR.DebugPanel = DebugPanel;

})(window);
