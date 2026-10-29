/**
 * 粒子特效：整局只占 2 个 draw call（尘土一池、星火一池），不随粒子数增长。
 *
 * 为什么是两个池，而不是一个：
 *   加色混合下深色粒子等于看不见（亮度被"加"没了），正常混合下发光的星火又会糊成一团灰。
 *   混合方式是固定管线的渲染状态，没法逐粒子切换，所以按混合模式分池。
 *
 * 为什么自己写 ShaderMaterial，而不是直接用 THREE.PointsMaterial：
 *   1) PointsMaterial 只有一个全局 size，做不到"大土块"和"小火星"混在同一池里；
 *   2) 不给贴图时 Points 画出来是硬边方块，自己写片元着色器能白送一个柔和圆点，
 *      省掉一张贴图资源，也不用管贴图的采样过滤。
 *
 * 池满就丢新粒子，绝不扩容 —— 扩容要重新分配 Float32Array，那一帧必然卡一下。
 * 粒子位置每帧都在变，包围球算不准，所以两个 Points 都关掉视锥剔除（否则整池会被误剔除）。
 *
 * 关于雾：粒子全部发射在玩家身边（离相机 3~10 米），远小于雾的 near（70），
 *         所以这里不接雾是安全的。以后如果要在远处放粒子，再补 fog 支持。
 *
 * 关于颜色：因为用的是裸 ShaderMaterial，three 不会替我做 linear -> sRGB 的输出转换
 *         （内置材质才走 <encodings_fragment>）。这里自己补一个和 three 完全一致的转换，
 *         保证粒子颜色和场景里其他物体的颜色是同一套亮度标准。
 *         前提是 Game 里 renderer.outputEncoding = sRGBEncoding；哪天改了那行，粒子会整体偏亮。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var CFG = RR.CFG || {};
  var PC = CFG.PARTICLES || {};

  // ---------- 一个粒子的全部状态挤在一段定长浮点里 ----------
  // 这样"用队尾元素填坑"只需要 copyWithin 拷 20 个数，不用逐个数组手动搬。
  var STRIDE = 20;
  var P_X = 0, P_Y = 1, P_Z = 2;                  // 位置
  var V_X = 3, V_Y = 4, V_Z = 5;                  // 速度
  var S_A = 6, S_B = 7;                           // 尺寸：出生 -> 死亡
  var C0R = 8, C0G = 9, C0B = 10;                 // 颜色：出生
  var C1R = 11, C1G = 12, C1B = 13;               // 颜色：死亡
  var A_A = 14, A_B = 15;                         // 透明度：出生 -> 死亡
  var T_LIFE = 16, T_TOTAL = 17;                  // 已活时长、总寿命
  var D_DRAG = 18, D_GRAV = 19;                   // 阻尼、重力（正数往下拽，负数往上飘）

  // gl_PointSize = 世界尺寸 * uPixel / 到相机的距离
  // uPixel = 视口高度 / 2 * (1 / tan(fov/2))，也就是把"世界单位"换算成"像素"的系数
  var VERT = [
    'attribute float aSize;',
    'attribute vec4 aColor;',
    'uniform float uPixel;',
    'varying vec4 vColor;',
    'void main() {',
    '  vColor = aColor;',
    '  vec4 mv = modelViewMatrix * vec4(position, 1.0);',
    '  float dist = max(0.05, -mv.z);',
    '  gl_PointSize = clamp(aSize * uPixel / dist, 1.0, 220.0);',
    '  gl_Position = projectionMatrix * mv;',
    '}'
  ].join('\n');

  var FRAG = [
    'varying vec4 vColor;',
    // 和 three 的 LinearTosRGB 完全一致，保证和内置材质同一个亮度标准
    'vec3 toSRGB(vec3 c) {',
    '  vec3 lo = c * 12.92;',
    '  vec3 hi = 1.055 * pow(max(c, vec3(0.0)), vec3(0.41666)) - 0.055;',
    '  return mix(hi, lo, step(c, vec3(0.0031308)));',
    '}',
    'void main() {',
    '  vec2 d = gl_PointCoord - vec2(0.5);',
    '  float r2 = dot(d, d) * 4.0;',        // 0 = 圆心，1 = 圆边
    '  if (r2 > 1.0) discard;',             // 方形点裁成圆点
    '  float k = 1.0 - r2;',
    '  gl_FragColor = vec4(toSRGB(vColor.rgb), vColor.a * k * k);',  // 二次衰减，边缘更柔
    '}'
  ].join('\n');

  function rnd(a, b) { return a + Math.random() * (b - a); }

  // 扬尘配色：路面本身是浅土黄的，尘土必须比路面再亮一档才分得出来
  // （同亮度就会糊在路面上，等于白喷）。数值是线性空间，最终显示由着色器转 sRGB。
  var DUST_A = [0.66, 0.47, 0.28];   // 刚冒出来
  var DUST_B = [0.42, 0.28, 0.15];   // 快散掉

  // ==================== 粒子池 ====================
  function Pool(scene, cap, blending, renderOrder) {
    this.cap = cap;
    this.count = 0;
    this.data = new Float32Array(cap * STRIDE);

    // GPU 侧缓冲：紧凑排列，活跃粒子永远占 [0, count)
    this.pos = new Float32Array(cap * 3);
    this.col = new Float32Array(cap * 4);   // 4 分量 => 逐粒子 alpha（three r149 的 USE_COLOR_ALPHA）
    this.siz = new Float32Array(cap);

    var geo = new THREE.BufferGeometry();
    this.aPos = new THREE.BufferAttribute(this.pos, 3);
    this.aCol = new THREE.BufferAttribute(this.col, 4);
    this.aSize = new THREE.BufferAttribute(this.siz, 1);
    geo.setAttribute('position', this.aPos);
    geo.setAttribute('aColor', this.aCol);
    geo.setAttribute('aSize', this.aSize);
    geo.setDrawRange(0, 0);
    this.geo = geo;

    this.mat = new THREE.ShaderMaterial({
      uniforms: { uPixel: { value: 60 } },
      vertexShader: VERT,
      fragmentShader: FRAG,
      transparent: true,
      depthWrite: false,     // 粒子之间不互相遮挡，避免半透明排序闪烁
      depthTest: true,       // 但会被场景里的实物挡住，这是对的
      blending: blending
    });

    this.points = new THREE.Points(geo, this.mat);
    this.points.frustumCulled = false;   // 位置每帧都在变，包围球不准，禁止剔除
    this.points.renderOrder = renderOrder;
    scene.add(this.points);
  }

  Pool.prototype.spawn = function (o) {
    if (this.count >= this.cap) return false;    // 池满直接丢，不扩容
    var d = this.data, i = this.count * STRIDE;
    d[i + P_X] = o.x; d[i + P_Y] = o.y; d[i + P_Z] = o.z;
    d[i + V_X] = o.vx || 0; d[i + V_Y] = o.vy || 0; d[i + V_Z] = o.vz || 0;
    d[i + S_A] = o.size; d[i + S_B] = (o.size2 === undefined ? o.size : o.size2);
    d[i + C0R] = o.r0; d[i + C0G] = o.g0; d[i + C0B] = o.b0;
    d[i + C1R] = o.r1; d[i + C1G] = o.g1; d[i + C1B] = o.b1;
    d[i + A_A] = o.a0; d[i + A_B] = o.a1;
    d[i + T_LIFE] = 0; d[i + T_TOTAL] = o.life;
    d[i + D_DRAG] = o.drag || 0; d[i + D_GRAV] = o.gravity || 0;
    this.count++;
    return true;
  };

  Pool.prototype.clear = function () {
    this.count = 0;
    this.geo.setDrawRange(0, 0);    // 立刻不画，避免清场后还残留一帧旧粒子
  };

  Pool.prototype.update = function (dt) {
    var d = this.data, n = this.count;

    for (var i = 0; i < n; i++) {
      var o = i * STRIDE;

      d[o + T_LIFE] += dt;
      if (d[o + T_LIFE] >= d[o + T_TOTAL]) {
        // 死透了：把队尾那个搬过来填坑，再把队尾砍掉（i 不动，这一帧重算新搬来的）
        var last = (n - 1) * STRIDE;
        if (o !== last) d.copyWithin(o, last, last + STRIDE);
        n--;
        i--;
        continue;
      }

      var k = 1 - d[o + D_DRAG] * dt;
      if (k < 0) k = 0;
      d[o + V_X] *= k; d[o + V_Y] *= k; d[o + V_Z] *= k;
      d[o + V_Y] -= d[o + D_GRAV] * dt;

      d[o + P_X] += d[o + V_X] * dt;
      d[o + P_Y] += d[o + V_Y] * dt;
      d[o + P_Z] += d[o + V_Z] * dt;
    }

    this.count = n;

    // 把模拟结果刷进 GPU 缓冲（插值出这一帧的尺寸/颜色/透明度）
    var pos = this.pos, col = this.col, siz = this.siz;
    for (var j = 0; j < n; j++) {
      var p = j * STRIDE, p3 = j * 3, p4 = j * 4;
      var u = d[p + T_LIFE] / d[p + T_TOTAL];

      pos[p3] = d[p + P_X]; pos[p3 + 1] = d[p + P_Y]; pos[p3 + 2] = d[p + P_Z];

      siz[j] = d[p + S_A] + (d[p + S_B] - d[p + S_A]) * u;

      col[p4] = d[p + C0R] + (d[p + C1R] - d[p + C0R]) * u;
      col[p4 + 1] = d[p + C0G] + (d[p + C1G] - d[p + C0G]) * u;
      col[p4 + 2] = d[p + C0B] + (d[p + C1B] - d[p + C0B]) * u;
      col[p4 + 3] = d[p + A_A] + (d[p + A_B] - d[p + A_A]) * u;
    }

    this.aPos.needsUpdate = true;
    this.aCol.needsUpdate = true;
    this.aSize.needsUpdate = true;
    this.geo.setDrawRange(0, n);
  };

  // ==================== 粒子系统 ====================
  function Particles(scene, renderer) {
    this.enabled = PC.enabled !== false;
    this.renderer = renderer || null;
    this._v2 = new THREE.Vector2();
    this._t = {};                      // 连续发射的节流计时器
    this.stats = { dust: 0, spark: 0, byName: {} };

    if (!this.enabled) return;

    this.dust = new Pool(scene, PC.dustCap || 260, THREE.NormalBlending, 1);
    this.spark = new Pool(scene, PC.sparkCap || 200, THREE.AdditiveBlending, 2);
  }

  Particles.prototype._hit = function (name) {
    var b = this.stats.byName;
    b[name] = (b[name] || 0) + 1;
  };

  /** 节流：连续型特效（跑动扬尘、尾焰）按固定间隔发射，避免每帧都喷 */
  Particles.prototype._tick = function (name, every, dt) {
    var t = (this._t[name] || 0) - dt;
    if (t > 0) { this._t[name] = t; return false; }
    this._t[name] = every;
    return true;
  };

  // ---------- 尘土（正常混合）----------

  /** 落地：一圈向两侧散开的土黄扬尘，power 越大炸得越开 */
  Particles.prototype.land = function (x, y, z, power) {
    if (!this.enabled) return;
    power = power || 1;
    var n = Math.round(8 + power * 7);
    for (var i = 0; i < n; i++) {
      var a = rnd(0, Math.PI * 2);
      var sp = rnd(1.0, 3.2) * power;
      var sh = rnd(0.9, 1.1);
      this.dust.spawn({
        x: x + rnd(-0.22, 0.22), y: y + rnd(0.02, 0.14), z: z + rnd(-0.25, 0.35),
        vx: Math.cos(a) * sp, vy: rnd(0.35, 1.7) * power, vz: Math.sin(a) * sp * 0.45 + 0.7,
        size: rnd(0.34, 0.56), size2: rnd(0.78, 1.22),
        r0: DUST_A[0] * sh, g0: DUST_A[1] * sh, b0: DUST_A[2] * sh,
        r1: DUST_B[0] * sh, g1: DUST_B[1] * sh, b1: DUST_B[2] * sh,
        a0: 0.68, a1: 0,
        life: rnd(0.34, 0.60), drag: 3.2, gravity: 2.4
      });
    }
    this.stats.dust += n;
    this._hit('land');
  };

  /** 起跳：脚下一点点土，比落地小得多 */
  Particles.prototype.takeoff = function (x, y, z) {
    if (!this.enabled) return;
    for (var i = 0; i < 6; i++) {
      var a = rnd(0, Math.PI * 2);
      this.dust.spawn({
        x: x + rnd(-0.18, 0.18), y: y + rnd(0, 0.1), z: z + rnd(-0.15, 0.25),
        vx: Math.cos(a) * rnd(0.5, 1.5), vy: rnd(0.25, 0.85), vz: Math.sin(a) * rnd(0.3, 0.9) + 0.5,
        size: rnd(0.22, 0.36), size2: rnd(0.55, 0.85),
        r0: DUST_A[0], g0: DUST_A[1], b0: DUST_A[2],
        r1: DUST_B[0], g1: DUST_B[1], b1: DUST_B[2],
        a0: 0.52, a1: 0,
        life: rnd(0.24, 0.40), drag: 3.4, gravity: 2.2
      });
    }
    this.stats.dust += 6;
    this._hit('takeoff');
  };

  /** 跑动扬尘：每帧调，内部按间隔节流 */
  Particles.prototype.run = function (x, y, z, dt) {
    if (!this.enabled) return;
    if (!this._tick('run', PC.runDustEvery || 0.085, dt)) return;
    for (var i = 0; i < 2; i++) {
      this.dust.spawn({
        x: x + rnd(-0.35, 0.35), y: y + rnd(0.01, 0.10), z: z + rnd(-0.15, 0.35),
        vx: rnd(-0.5, 0.5), vy: rnd(0.30, 0.90), vz: rnd(0.6, 1.6),
        size: rnd(0.24, 0.38), size2: rnd(0.52, 0.80),
        r0: DUST_A[0], g0: DUST_A[1], b0: DUST_A[2],
        r1: DUST_B[0], g1: DUST_B[1], b1: DUST_B[2],
        a0: 0.46, a1: 0,
        life: rnd(0.22, 0.36), drag: 3.0, gravity: 1.6
      });
    }
    this.stats.dust += 2;
    this._hit('run');
  };

  /** 滑铲：贴地往后拖的一道更宽的尘 */
  Particles.prototype.slide = function (x, y, z, dt) {
    if (!this.enabled) return;
    if (!this._tick('slide', 0.05, dt)) return;
    for (var i = 0; i < 3; i++) {
      this.dust.spawn({
        x: x + rnd(-0.60, 0.60), y: y + rnd(0.01, 0.16), z: z + rnd(-0.30, 0.60),
        vx: rnd(-1.1, 1.1), vy: rnd(0.20, 0.80), vz: rnd(1.2, 2.8),
        size: rnd(0.30, 0.50), size2: rnd(0.72, 1.08),
        r0: DUST_A[0], g0: DUST_A[1], b0: DUST_A[2],
        r1: DUST_B[0], g1: DUST_B[1], b1: DUST_B[2],
        a0: 0.60, a1: 0,
        life: rnd(0.30, 0.50), drag: 2.6, gravity: 2.0
      });
    }
    this.stats.dust += 3;
    this._hit('slide');
  };

  // ---------- 星火（加色混合）----------

  /** 撞到障碍：土块碎屑 + 几粒短促白火星 */
  Particles.prototype.hit = function (x, y, z) {
    if (!this.enabled) return;
    var i, a, sp;
    for (i = 0; i < 12; i++) {
      a = rnd(0, Math.PI * 2);
      sp = rnd(1.6, 4.2);
      this.dust.spawn({
        x: x + rnd(-0.2, 0.2), y: y + rnd(-0.3, 0.4), z: z + rnd(-0.2, 0.2),
        vx: Math.cos(a) * sp, vy: rnd(0.8, 3.2), vz: Math.sin(a) * sp * 0.5 + 1.0,
        size: rnd(0.16, 0.30), size2: rnd(0.36, 0.60),
        r0: 0.30, g0: 0.20, b0: 0.12, r1: 0.18, g1: 0.12, b1: 0.07,
        a0: 0.75, a1: 0,
        life: rnd(0.30, 0.55), drag: 1.6, gravity: 7.0
      });
    }
    for (i = 0; i < 10; i++) {
      a = rnd(0, Math.PI * 2);
      sp = rnd(2.5, 6.0);
      this.spark.spawn({
        x: x + rnd(-0.15, 0.15), y: y + rnd(-0.2, 0.3), z: z + rnd(-0.15, 0.15),
        vx: Math.cos(a) * sp, vy: rnd(1.0, 3.6), vz: Math.sin(a) * sp * 0.5 + 1.0,
        size: rnd(0.10, 0.20), size2: 0.02,
        r0: 1.0, g0: 0.75, b0: 0.35, r1: 0.50, g1: 0.12, b1: 0.02,
        a0: 0.90, a1: 0,
        life: rnd(0.16, 0.30), drag: 1.2, gravity: 8.0
      });
    }
    this.stats.dust += 12;
    this.stats.spark += 10;
    this._hit('hit');
  };

  /** 吃到蜂蜜罐：一小撮金色闪光 */
  Particles.prototype.coin = function (x, y, z) {
    if (!this.enabled) return;
    for (var i = 0; i < 7; i++) {
      this.spark.spawn({
        x: x + rnd(-0.25, 0.25), y: y + rnd(-0.1, 0.3), z: z + rnd(-0.15, 0.15),
        vx: rnd(-1.4, 1.4), vy: rnd(1.6, 3.4), vz: rnd(-1.0, 1.0),
        size: rnd(0.12, 0.20), size2: rnd(0.02, 0.06),
        r0: 1.0, g0: 0.68, b0: 0.07, r1: 1.0, g1: 0.95, b1: 0.55,
        a0: 1.0, a1: 0,
        life: rnd(0.30, 0.50), drag: 2.0, gravity: 3.0
      });
    }
    this.stats.spark += 7;
    this._hit('coin');
  };

  /** 回血：绿色光点往上飘，慢一点显得"治愈" */
  Particles.prototype.heal = function (x, y, z) {
    if (!this.enabled) return;
    for (var i = 0; i < 14; i++) {
      this.spark.spawn({
        x: x + rnd(-0.30, 0.30), y: y + rnd(-0.3, 0.5), z: z + rnd(-0.25, 0.25),
        vx: rnd(-0.8, 0.8), vy: rnd(0.9, 2.2), vz: rnd(-0.8, 0.8),
        size: rnd(0.14, 0.26), size2: rnd(0.30, 0.50),
        r0: 0.28, g0: 0.87, b0: 0.15, r1: 0.75, g1: 1.0, b1: 0.60,
        a0: 0.85, a1: 0,
        life: rnd(0.60, 1.00), drag: 1.2, gravity: -0.6
      });
    }
    this.stats.spark += 14;
    this._hit('heal');
  };

  /** 加速带：脚下连续往外喷的橙黄尾焰，每帧调，内部节流 */
  Particles.prototype.boost = function (x, y, z, dt) {
    if (!this.enabled) return;
    if (!this._tick('boost', 0.03, dt)) return;
    var i;
    for (i = 0; i < 2; i++) {
      this.dust.spawn({
        x: x + rnd(-0.40, 0.40), y: y + rnd(0.02, 0.20), z: z + rnd(-0.2, 0.3),
        vx: rnd(-0.8, 0.8), vy: rnd(0.40, 1.40), vz: rnd(2.0, 4.0),
        size: rnd(0.20, 0.36), size2: rnd(0.55, 0.90),
        r0: 0.55, g0: 0.34, b0: 0.16, r1: 0.30, g1: 0.18, b1: 0.09,
        a0: 0.50, a1: 0,
        life: rnd(0.24, 0.42), drag: 2.4, gravity: 1.4
      });
    }
    for (i = 0; i < 3; i++) {
      this.spark.spawn({
        x: x + rnd(-0.32, 0.32), y: y + rnd(0.02, 0.22), z: z + rnd(-0.15, 0.25),
        vx: rnd(-0.7, 0.7), vy: rnd(0.60, 2.00), vz: rnd(2.5, 5.0),
        size: rnd(0.14, 0.24), size2: 0.03,
        r0: 1.0, g0: 0.45, b0: 0.06, r1: 0.45, g1: 0.08, b1: 0.02,
        a0: 0.90, a1: 0,
        life: rnd(0.18, 0.32), drag: 2.0, gravity: -1.0
      });
    }
    this.stats.dust += 2;
    this.stats.spark += 3;
    this._hit('boost');
  };

  /** 处决斧头砸下来：星火四溅 + 一蓬土（README 里一直承诺的那一下）*/
  Particles.prototype.axe = function (x, y, z) {
    if (!this.enabled) return;
    var i, a, sp;
    for (i = 0; i < 30; i++) {
      a = rnd(0, Math.PI * 2);
      sp = rnd(3.0, 9.0);
      this.spark.spawn({
        x: x + rnd(-0.15, 0.15), y: y + rnd(-0.15, 0.25), z: z + rnd(-0.15, 0.15),
        vx: Math.cos(a) * sp, vy: rnd(1.0, 5.5), vz: Math.sin(a) * sp * 0.6 + 0.5,
        size: rnd(0.10, 0.22), size2: 0.02,
        r0: 1.0, g0: 0.92, b0: 0.60, r1: 0.55, g1: 0.12, b1: 0.02,
        a0: 1.0, a1: 0,
        life: rnd(0.25, 0.60), drag: 0.9, gravity: 11.0
      });
    }
    for (i = 0; i < 16; i++) {
      a = rnd(0, Math.PI * 2);
      sp = rnd(1.4, 3.6);
      this.dust.spawn({
        x: x + rnd(-0.3, 0.3), y: y * 0.2 + rnd(0.02, 0.2), z: z + rnd(-0.3, 0.3),
        vx: Math.cos(a) * sp, vy: rnd(0.6, 2.6), vz: Math.sin(a) * sp * 0.6,
        size: rnd(0.30, 0.60), size2: rnd(0.90, 1.40),
        r0: DUST_A[0], g0: DUST_A[1], b0: DUST_A[2],
        r1: DUST_B[0], g1: DUST_B[1], b1: DUST_B[2],
        a0: 0.55, a1: 0,
        life: rnd(0.40, 0.70), drag: 2.6, gravity: 3.0
      });
    }
    this.stats.spark += 30;
    this.stats.dust += 16;
    this._hit('axe');
  };

  // ---------- 生命周期 ----------

  /** 回主菜单/重开时清场：别让上一局的尘土飘进新一局 */
  Particles.prototype.clear = function () {
    if (!this.enabled) return;
    this.dust.clear();
    this.spark.clear();
    this._t = {};
  };

  /**
   * 每帧调一次：先推进模拟，再按当前视口把"世界尺寸"换算成像素。
   * dt 再夹一次上限：切后台回来时 dt 可能是好几秒，粒子会瞬移一大段。
   */
  Particles.prototype.update = function (dt, camera) {
    if (!this.enabled) return;

    var h = 720;
    if (this.renderer) {
      this.renderer.getDrawingBufferSize(this._v2);
      if (this._v2.y > 0) h = this._v2.y;
    }
    var pm = (camera && camera.projectionMatrix) ? camera.projectionMatrix.elements[5] : 1.15;
    var px = (h * 0.5) * pm * (PC.pixelScale || 1);
    this.dust.mat.uniforms.uPixel.value = px;
    this.spark.mat.uniforms.uPixel.value = px;

    var d = Math.min(dt, 0.05);
    this.dust.update(d);
    this.spark.update(d);
  };

  /** 当前活跃粒子数（自检/调优用） */
  Particles.prototype.live = function () {
    if (!this.enabled) return { dust: 0, spark: 0 };
    return { dust: this.dust.count, spark: this.spark.count };
  };

  RR.Particles = Particles;

})(window);
