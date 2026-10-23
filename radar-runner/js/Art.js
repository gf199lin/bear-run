/**
 * 美术素材工厂 —— 熊出没风（狗熊岭森林跑酷）
 *
 * 视觉配方：
 *   · 明亮饱和的卡通色：鲜绿树冠、土黄林道、蜂蜜金黄
 *   · 圆润厚实的几何体，树冠用平滑球体堆出蓬松感
 *   · 森林元素：大树、松树、灌木、蘑菇、蜂巢、木栅栏、小木屋、原木堆
 *   · 功能性警示色（黄黑条）只出现在"要跳"的障碍上，保证一眼能读懂
 * 所有材质统一走缓存，减少状态切换。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};
  var C = RR.CFG.COLORS;

  var matCache = {};

  /** 带缓存的材质 */
  function mat(color, opts) {
    opts = opts || {};
    var flat = opts.flat !== false;
    var emissive = opts.emissive || 0;
    var key = color + '#' + (flat ? 'f' : 's') + '#' + emissive;
    if (matCache[key]) return matCache[key];

    var m = new THREE.MeshLambertMaterial({ color: color, flatShading: flat });
    if (emissive) m.emissive = new THREE.Color(emissive);
    matCache[key] = m;
    return m;
  }

  function box(w, h, d, color, opts) {
    return new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat(color, opts));
  }

  function cyl(rt, rb, h, seg, color, opts) {
    return new THREE.Mesh(new THREE.CylinderGeometry(rt, rb, h, seg), mat(color, opts));
  }

  /** 低面数球（石头、土块用） */
  function ball(r, color, opts) {
    return new THREE.Mesh(new THREE.IcosahedronGeometry(r, 0), mat(color, opts));
  }

  /** 平滑球（树冠、蘑菇、苹果用，圆润卡通感） */
  function ballS(r, color, opts) {
    var o = opts || {};
    var m = new THREE.Mesh(new THREE.SphereGeometry(r, 10, 8), mat(color, { flat: o.flat === true, emissive: o.emissive }));
    return m;
  }

  function cone(r, h, seg, color, opts) {
    return new THREE.Mesh(new THREE.ConeGeometry(r, h, seg), mat(color, opts));
  }

  function rand(a, b) { return a + Math.random() * (b - a); }
  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

  /**
   * 给路边景物套一层"会晃"的内层枢轴。
   *
   * 外层 g 负责摆放（World 会设 position / rotation.y），内层 pivot 只负责摇摆，
   * 两者互不干扰。枢轴原点就在地面（y=0），所以树是连根一起微微前俯后仰，
   * 草则是整丛甩动 —— 这正是风穿过林子的样子。
   */
  function swayed(g, amp, freq) {
    var pivot = new THREE.Group();
    g.add(pivot);
    g.userData.sway = {
      node: pivot,
      amp: amp,
      freq: freq || rand(0.6, 1.1),
      phase: rand(0, Math.PI * 2)
    };
    return pivot;
  }

  // ==================== 地面贴图 ====================

  var _dirtCanvas = null;
  var _grassCanvas = null;

  /** 浅色噪声底：乘到材质颜色上，所以整体画成接近白色，只留很淡的颗粒与颗粒阴影 */
  function _grainCanvas(base, dots) {
    var cv = document.createElement('canvas');
    cv.width = 128;
    cv.height = 128;
    var ctx = cv.getContext('2d');
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, 128, 128);

    var i, x, y;
    for (i = 0; i < dots; i++) {
      x = Math.random() * 128;
      y = Math.random() * 128;
      var r = rand(0.6, 2.4);
      var dark = Math.random() < 0.62;
      ctx.fillStyle = dark
        ? 'rgba(70,48,22,' + rand(0.05, 0.16).toFixed(3) + ')'
        : 'rgba(255,255,255,' + rand(0.15, 0.4).toFixed(3) + ')';
      ctx.beginPath();
      ctx.ellipse(x, y, r, r * rand(0.6, 1.2), Math.random() * 3, 0, Math.PI * 2);
      ctx.fill();
    }
    // 几道浅色车辙 / 草根纹路，让贴图有方向感
    for (i = 0; i < 5; i++) {
      ctx.strokeStyle = 'rgba(255,255,255,' + rand(0.1, 0.24).toFixed(3) + ')';
      ctx.lineWidth = rand(1, 3);
      ctx.beginPath();
      x = Math.random() * 128;
      y = Math.random() * 128;
      ctx.moveTo(x, y);
      ctx.lineTo(x + rand(-10, 10), y + rand(18, 46));
      ctx.stroke();
    }
    return cv;
  }

  function _tileTexture(canvas) {
    var tex = new THREE.CanvasTexture(canvas);
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    if (THREE.sRGBEncoding !== undefined) tex.encoding = THREE.sRGBEncoding;
    tex.anisotropy = 2;
    return tex;
  }

  var _groundMats = null;
  var _groundTexes = null;

  /**
   * 地面材质（全局共享，只建一次）。
   *
   * 每个材质一份 CanvasTexture 实例（共用同一张 canvas 当图源），因为 repeat 是
   * 记在 texture 上的：土路 8 米宽、草地 2.6 米宽，比例不一样，只能各记一份。
   * 返回的 texes 供 World 每帧推 offset —— 每块地砖各推各的就会相对滑移，必须共享。
   */
  function groundMaterials(length) {
    if (!_groundMats) {
      var meters = RR.CFG.WORLD.groundTexMeters;
      _dirtCanvas = _dirtCanvas || _grainCanvas('#f6efe2', 420);
      _grassCanvas = _grassCanvas || _grainCanvas('#eef7e6', 520);

      var roadW = RR.CFG.LANE_WIDTH * 3 + 1.6;
      var reps = [
        [roadW, C.dirt],
        [2.6, C.grass],
        [5.5, C.grassDark]
      ];
      var texes = [];
      var mats = [];
      for (var i = 0; i < reps.length; i++) {
        var tex = _tileTexture(i === 0 ? _dirtCanvas : _grassCanvas);
        tex.repeat.set(reps[i][0] / meters, length / meters);
        texes.push(tex);
        mats.push(new THREE.MeshLambertMaterial({
          color: reps[i][1],
          flatShading: true,
          map: tex
        }));
      }
      _groundMats = { road: mats[0], grass: mats[1], grassDark: mats[2] };
      _groundTexes = texes;
    }
    return { mats: _groundMats, texes: _groundTexes, meters: RR.CFG.WORLD.groundTexMeters };
  }

  // ==================== 天空 ====================

  function makeSky() {
    var canvas = document.createElement('canvas');
    canvas.width = 512;
    canvas.height = 256;
    var ctx = canvas.getContext('2d');

    // 狗熊岭的明亮蓝天
    var grad = ctx.createLinearGradient(0, 0, 0, 256);
    grad.addColorStop(0.00, '#2f86d8');
    grad.addColorStop(0.30, '#6fb4e8');
    grad.addColorStop(0.50, '#a8d6f2');
    grad.addColorStop(0.66, '#d8ecf5');
    grad.addColorStop(0.80, '#f7ecbf');
    grad.addColorStop(1.00, '#f4d69b');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 512, 256);

    // 白云（小小几朵，贴着地平线上方，不抢画面）
    var clouds = [
      [64, 116, 26, 11], [99, 110, 18, 8], [34, 122, 16, 7],
      [250, 113, 29, 12], [293, 107, 20, 9], [221, 120, 15, 7],
      [420, 121, 24, 10], [457, 115, 16, 7]
    ];
    for (var i = 0; i < clouds.length; i++) {
      var c = clouds[i];
      var rg = ctx.createRadialGradient(c[0], c[1], 1, c[0], c[1], c[2]);
      rg.addColorStop(0, 'rgba(255,255,255,0.82)');
      rg.addColorStop(0.55, 'rgba(255,255,255,0.5)');
      rg.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = rg;
      ctx.beginPath();
      ctx.ellipse(c[0], c[1], c[2], c[3], 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.ellipse(c[0] + c[2] * 0.5, c[1] + c[3] * 0.3, c[2] * 0.6, c[3] * 0.75, 0, 0, Math.PI * 2);
      ctx.fill();
    }

    var tex = new THREE.CanvasTexture(canvas);
    if (THREE.sRGBEncoding !== undefined) tex.encoding = THREE.sRGBEncoding;

    var skyMat = new THREE.MeshBasicMaterial({
      map: tex,
      side: THREE.BackSide,
      depthWrite: false,
      fog: false
    });

    var sky = new THREE.Mesh(new THREE.SphereGeometry(320, 24, 16), skyMat);
    sky.name = 'sky';
    return sky;
  }

  // ==================== 地面块（林间土路） ====================

  function makeGroundTile(length, gm) {
    var laneW = RR.CFG.LANE_WIDTH;
    var roadW = laneW * 3 + 1.6;
    var g = new THREE.Group();

    // 地面三大块（土路 / 近草 / 远草）走共享材质：World 换区域时直接改材质颜色就能整片换色
    var M = (gm && gm.mats) || groundMaterials(length).mats;

    function slab(w, h, d, m, x, y) {
      var mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m);
      mesh.position.set(x, y, 0);
      g.add(mesh);
      return mesh;
    }

    // 踩出来的土路
    slab(roadW, 0.32, length, M.road, 0, -0.16);

    // 两侧绿草地
    for (var s = -1; s <= 1; s += 2) {
      slab(2.6, 0.42, length, M.grass, s * (roadW / 2 + 1.3), -0.10);
      slab(5.5, 0.36, length, M.grassDark, s * (roadW / 2 + 4.6), -0.16);
    }

    // 车道之间的草缝：极扁、几乎完全趴在地上，只做"三条道"的视觉分界
    var edgeMat = { flat: true };
    var tufts = Math.max(2, Math.round(length / 6));
    for (var i = 0; i < 2; i++) {
      var x = (i === 0 ? -1 : 1) * (laneW / 2 + 0.02);
      for (var j = 0; j < tufts; j++) {
        var t = box(0.13, 0.02, rand(0.8, 1.5), C.laneEdge, edgeMat);
        t.position.set(x + rand(-0.03, 0.03), 0.006, -length / 2 + rand(1.2, length - 1.2));
        g.add(t);
      }
    }

    // 土路上踩出来的浅色车辙与深色土斑
    for (var k = 0; k < 4; k++) {
      var spot = new THREE.Mesh(
        new THREE.CircleGeometry(rand(0.6, 1.5), 8),
        mat(C.dirtSpot, { flat: true })
      );
      spot.rotation.x = -Math.PI / 2;
      spot.position.set(pick([-1, 1]) * rand(1.5, roadW / 2 - 0.3), 0.01, rand(-length / 2, length / 2));
      spot.rotation.z = rand(0, Math.PI);
      g.add(spot);
    }
    for (var p = 0; p < 3; p++) {
      var pb = new THREE.Mesh(new THREE.SphereGeometry(rand(0.07, 0.15), 6, 4), mat(C.pebble, { flat: true }));
      pb.position.set(rand(-roadW / 2 + 0.3, roadW / 2 - 0.3), 0.03, rand(-length / 2, length / 2));
      pb.rotation.set(rand(0, 3), rand(0, 3), rand(0, 3));
      g.add(pb);
    }

    // 路肩草丛
    for (var q = 0; q < 4; q++) {
      var side = q < 2 ? -1 : 1;
      var clump = new THREE.Group();
      for (var b = 0; b < 3; b++) {
        var blade = box(rand(0.07, 0.11), rand(0.32, 0.58), rand(0.07, 0.11), C.leafMid, { flat: true });
        blade.position.set(rand(-0.14, 0.14), 0.0 + blade.geometry.parameters.height / 2, rand(-0.14, 0.14));
        blade.rotation.z = rand(-0.35, 0.35);
        blade.rotation.x = rand(-0.25, 0.25);
        clump.add(blade);
      }
      clump.position.set(side * rand(roadW / 2 + 0.3, roadW / 2 + 2.0), 0.1, rand(-length / 2, length / 2));
      g.add(clump);
    }

    return g;
  }

  // ==================== 障碍物 ====================

  /** 矮木栅栏：必须跳过去（顶部黄黑条是唯一识别信号） */
  function makeLowBarrier() {
    var g = new THREE.Group();

    // 两根木桩
    for (var s = -1; s <= 1; s += 2) {
      var post = box(0.2, 0.96, 0.22, C.woodDark, { flat: true });
      post.position.set(s * 0.84, 0.48, 0);
      g.add(post);
    }

    // 两道横板
    for (var i = 0; i < 2; i++) {
      var plank = box(1.86, 0.2, 0.17, C.plank, { flat: true });
      plank.position.set(0, 0.44 + i * 0.36, 0);
      g.add(plank);
    }

    // 顶部的黄黑警示条 —— "这个要跳！"
    var warn = box(1.9, 0.17, 0.2, C.hazard, { flat: true, emissive: 0x3a2a00 });
    warn.position.y = 0.88;
    g.add(warn);
    var darkMat = mat(C.hazardDark, { flat: true });
    for (var d = -1; d <= 1; d++) {
      var tick = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.19, 0.22), darkMat);
      tick.position.set(d * 0.55, 0.88, 0);
      tick.rotation.z = 0.5;
      g.add(tick);
    }

    return g;
  }

  /** 横倒的大树干：底部离地 1.18m，必须滑铲钻过去 */
  function makeHighBarrier() {
    var g = new THREE.Group();

    // 两侧支撑树干
    for (var s = -1; s <= 1; s += 2) {
      var trunk = cyl(0.19, 0.24, 3.1, 7, C.trunkDark, { flat: true });
      trunk.position.set(s * 1.0, 1.55, 0.12);
      g.add(trunk);
    }

    // 横着的粗树干（最低点在 1.18m）
    var log = cyl(0.26, 0.28, 2.1, 9, C.trunk, { flat: true });
    log.rotation.z = Math.PI / 2;
    log.position.y = 1.46;
    g.add(log);

    // 树皮纹路
    for (var i = 0; i < 5; i++) {
      var rib = box(0.1, 0.5, 0.1, C.trunkDark, { flat: true });
      rib.position.set(-0.78 + i * 0.39, 1.46, 0.25);
      g.add(rib);
    }

    // 树杈上挂着的树叶
    for (var k = 0; k < 5; k++) {
      var leaf = ballS(rand(0.24, 0.36), k % 2 ? C.leafMid : C.leafLight);
      leaf.position.set(-0.85 + k * 0.43, 1.86 + rand(-0.06, 0.22), rand(-0.12, 0.12));
      leaf.scale.y = 0.7;
      g.add(leaf);
    }

    // 藤蔓垂下（危险提示）
    for (var v = 0; v < 4; v++) {
      var vine = box(0.07, rand(0.24, 0.42), 0.07, C.leafDark, { flat: true });
      vine.position.set(-0.7 + v * 0.47, 1.16 - vine.geometry.parameters.height / 2, rand(-0.05, 0.05));
      g.add(vine);
    }

    return g;
  }

  /** 石头 + 原木堆：必须换道绕开 */
  function makeRubbleBlock() {
    var g = new THREE.Group();

    // 底部横放的两根原木
    for (var i = 0; i < 2; i++) {
      var log = cyl(0.31, 0.31, 1.75, 8, i ? C.wood : C.woodDark, { flat: true });
      log.rotation.z = Math.PI / 2;
      log.position.set(0, 0.32, i ? 0.36 : -0.36);
      g.add(log);
      // 木头年轮端面
      for (var s = -1; s <= 1; s += 2) {
        var face = cyl(0.3, 0.3, 0.04, 8, C.plank, { flat: true });
        face.rotation.z = Math.PI / 2;
        face.position.set(s * 0.89, 0.32, i ? 0.36 : -0.36);
        g.add(face);
      }
    }

    // 上面压着一根原木
    var top = cyl(0.3, 0.3, 1.6, 8, C.wood, { flat: true });
    top.rotation.z = Math.PI / 2;
    top.position.set(0, 0.88, 0);
    g.add(top);

    // 顶部一块大石头（不让你跳过去）
    var rock = ball(0.78, C.rock, { flat: true });
    rock.position.set(0.06, 1.45, 0);
    rock.scale.set(1.05, 0.82, 0.95);
    rock.rotation.set(rand(0, 2), rand(0, 2), rand(0, 2));
    g.add(rock);

    var rock2 = ball(0.5, C.rockDark, { flat: true });
    rock2.position.set(-0.62, 1.2, 0.28);
    rock2.rotation.set(rand(0, 2), rand(0, 2), rand(0, 2));
    g.add(rock2);

    // 石头上的青苔
    var moss = ballS(0.42, C.leafMid);
    moss.position.set(-0.05, 1.9, 0.05);
    moss.scale.set(1.2, 0.4, 1.1);
    g.add(moss);

    return g;
  }

  /** 滚木桶：在两条车道之间来回滚动 */
  function makeMover() {
    var g = new THREE.Group();

    // 桶身（横躺的圆柱，滚动感）
    var body = cyl(0.62, 0.62, 1.62, 12, C.wood, { flat: true });
    body.rotation.z = Math.PI / 2;
    body.position.y = 0.66;
    g.add(body);

    // 桶板缝
    for (var i = 0; i < 6; i++) {
      var a = (i / 6) * Math.PI * 2;
      var seam = box(1.64, 0.07, 0.07, C.woodDark, { flat: true });
      seam.position.set(0, 0.66 + Math.sin(a) * 0.63, Math.cos(a) * 0.63);
      seam.rotation.x = -a;
      g.add(seam);
    }

    // 两道铁箍
    for (var r = -1; r <= 1; r += 2) {
      var hoop = cyl(0.66, 0.66, 0.1, 12, C.rockDark, { flat: true });
      hoop.rotation.z = Math.PI / 2;
      hoop.position.set(r * 0.5, 0.66, 0);
      g.add(hoop);
    }

    // 端面木纹
    for (var s = -1; s <= 1; s += 2) {
      var cap = cyl(0.6, 0.6, 0.05, 12, C.plank, { flat: true });
      cap.rotation.z = Math.PI / 2;
      cap.position.set(s * 0.82, 0.66, 0);
      g.add(cap);
      var core = cyl(0.2, 0.2, 0.07, 8, C.woodDark, { flat: true });
      core.rotation.z = Math.PI / 2;
      core.position.set(s * 0.84, 0.66, 0);
      g.add(core);
    }

    // 木头里冒出来的一片叶子（憨）
    var leaf = ballS(0.22, C.leafLight);
    leaf.position.set(0.2, 1.32, 0);
    leaf.scale.y = 0.75;
    g.add(leaf);

    return g;
  }

  // ==================== 拾取物 ====================

  /** 蜂蜜罐 —— 熊二的最爱，就是本作的金币 */
  function makeCoin() {
    var g = new THREE.Group();

    var jar = cyl(0.26, 0.29, 0.42, 10, C.honey, { flat: true, emissive: 0x8a5c00 });
    g.add(jar);

    // 罐口与木塞盖
    var neck = cyl(0.27, 0.26, 0.09, 10, C.honeyDark, { flat: true });
    neck.position.y = 0.25;
    g.add(neck);

    var lid = cyl(0.25, 0.25, 0.1, 10, C.plank, { flat: true });
    lid.position.y = 0.34;
    g.add(lid);

    // 溢出来的蜜，让它在远处也发光醒目
    var drip = ballS(0.13, C.honeyGlow, { emissive: 0x9a7000 });
    drip.position.set(0.2, -0.2, 0.08);
    drip.scale.set(1, 1.3, 1);
    g.add(drip);

    return g;
  }

  /** 大红苹果：回血 */
  function makeHealPack() {
    var g = new THREE.Group();

    var apple = ballS(0.42, C.apple, { emissive: 0x3a0c06 });
    g.add(apple);

    var apple2 = ballS(0.3, C.appleDark, { emissive: 0x2a0804 });
    apple2.position.set(0.2, 0.12, 0.06);
    g.add(apple2);

    // 果柄
    var stem = cyl(0.045, 0.05, 0.24, 5, C.trunkDark, { flat: true });
    stem.position.set(0, 0.48, 0);
    stem.rotation.z = 0.2;
    g.add(stem);

    // 叶子
    var leaf = box(0.3, 0.06, 0.16, C.leafMid, { flat: true });
    leaf.position.set(0.16, 0.5, 0);
    leaf.rotation.z = -0.45;
    g.add(leaf);

    // 高光
    var shine = ballS(0.09, 0xf6b9ac, { emissive: 0x5a1a12 });
    shine.position.set(-0.16, 0.2, 0.3);
    g.add(shine);

    return g;
  }

  /** 蜂蜜滑道：踩上去打滑加速（接住加速带逻辑） */
  function makeBoostPad() {
    var g = new THREE.Group();

    // 一滩蜂蜜
    var slick = box(1.95, 0.09, 2.7, C.honey, { flat: true, emissive: 0x6b4400 });
    slick.position.y = 0.045;
    g.add(slick);

    // 两侧的木条框
    for (var s = -1; s <= 1; s += 2) {
      var rail = box(0.16, 0.14, 2.74, C.woodDark, { flat: true });
      rail.position.set(s * 0.98, 0.08, 0);
      g.add(rail);
    }

    // 蜜里的气泡
    for (var i = 0; i < 4; i++) {
      var bub = ballS(rand(0.1, 0.19), C.honeyGlow, { emissive: 0x8a6400 });
      bub.position.set(rand(-0.7, 0.7), 0.11, rand(-1.1, 1.1));
      bub.scale.y = 0.5;
      g.add(bub);
    }

    // 前进方向的箭头
    var arrowMat = mat(0xfff3cf, { flat: true, emissive: 0x6a5a2a });
    for (var a = 0; a < 2; a++) {
      for (var d = -1; d <= 1; d += 2) {
        var bar = new THREE.Mesh(new THREE.BoxGeometry(0.17, 0.07, 0.8), arrowMat);
        bar.position.set(d * 0.25, 0.1, -0.55 + a * 1.05);
        bar.rotation.y = d * 0.6;
        g.add(bar);
      }
    }

    return g;
  }

  // ==================== 路边景物 ====================

  /** 阔叶大树：熊出没式的蓬松圆树冠 */
  function makePropTree() {
    var g = new THREE.Group();
    var p = swayed(g, 0.019, rand(0.45, 0.8));   // 大树晃得慢而小
    var h = rand(3.4, 6.2);

    var trunk = cyl(0.28, 0.42, h, 7, C.trunk, { flat: true });
    trunk.position.y = h / 2;
    p.add(trunk);

    // 树根鼓起
    var root = cyl(0.55, 0.7, 0.5, 7, C.trunkDark, { flat: true });
    root.position.y = 0.25;
    p.add(root);

    // 蓬松树冠：几团球错落堆叠
    var canopy = [
      [0, h * 0.94, 0, h * 0.42],
      [h * 0.22, h * 0.80, h * 0.08, h * 0.30],
      [-h * 0.24, h * 0.78, -h * 0.06, h * 0.28],
      [0.04, h * 1.12, -0.02, h * 0.26]
    ];
    for (var i = 0; i < canopy.length; i++) {
      var c = canopy[i];
      var leaf = ballS(c[3], i % 2 ? C.leafMid : C.leafLight);
      leaf.position.set(c[0], c[1], c[2]);
      leaf.scale.set(1, 0.86, 1);
      p.add(leaf);
    }

    // 底部一团深色叶（体积感）
    var low = ballS(h * 0.26, C.leafDark);
    low.position.set(-h * 0.1, h * 0.66, 0.12);
    low.scale.set(1, 0.72, 1);
    p.add(low);

    return g;
  }

  /** 松树 */
  function makePropPine() {
    var g = new THREE.Group();
    var p = swayed(g, 0.015, rand(0.5, 0.9));
    var h = rand(4.0, 6.8);

    var trunk = cyl(0.24, 0.36, h, 6, C.trunkDark, { flat: true });
    trunk.position.y = h / 2;
    p.add(trunk);

    var layers = 3;
    for (var i = 0; i < layers; i++) {
      var t = i / (layers - 1);
      var cr = h * (0.34 - t * 0.14);
      var ch = h * 0.44;
      var lc = cone(cr, ch, 8, i % 2 ? C.leafMid : C.leafDark, { flat: true });
      lc.position.y = h * 0.46 + i * h * 0.21;
      p.add(lc);
    }

    return g;
  }

  /** 灌木丛 */
  function makePropBush() {
    var g = new THREE.Group();
    var p = swayed(g, 0.055, rand(1.1, 1.8));
    var n = 3;
    for (var i = 0; i < n; i++) {
      var r = rand(0.42, 0.72);
      var b = ballS(r, i % 2 ? C.leafMid : C.leafLight);
      b.position.set(rand(-0.5, 0.5), r * 0.72, rand(-0.4, 0.4));
      b.scale.y = 0.84;
      p.add(b);
    }
    // 几点小浆果
    for (var k = 0; k < 3; k++) {
      var berry = ballS(0.08, C.apple, { emissive: 0x2a0804 });
      berry.position.set(rand(-0.5, 0.5), rand(0.4, 0.9), rand(-0.4, 0.4));
      p.add(berry);
    }
    return g;
  }

  /** 红伞白点蘑菇 */
  function makePropMushroom() {
    var g = new THREE.Group();
    var h = rand(0.34, 0.6);

    var stem = cyl(0.11, 0.15, h, 8, 0xeee0c4, { flat: true });
    stem.position.y = h / 2;
    g.add(stem);

    var cap = ballS(0.32, C.roof, { flat: true });
    cap.position.y = h + 0.04;
    cap.scale.set(1, 0.62, 1);
    g.add(cap);

    for (var i = 0; i < 4; i++) {
      var dot = ballS(0.07, 0xf7f0e0, { flat: true });
      var a = rand(0, Math.PI * 2);
      var rr = rand(0.06, 0.24);
      dot.position.set(Math.cos(a) * rr, h + 0.16, Math.sin(a) * rr);
      dot.scale.y = 0.55;
      g.add(dot);
    }

    return g;
  }

  /** 石头 */
  function makePropRock() {
    var g = new THREE.Group();
    var r = rand(0.5, 1.1);
    var rock = ball(r, C.rock, { flat: true });
    rock.position.y = r * 0.7;
    rock.scale.set(1, rand(0.7, 0.95), rand(0.85, 1.1));
    rock.rotation.set(rand(0, 3), rand(0, 3), rand(0, 3));
    g.add(rock);

    var small = ball(r * 0.5, C.rockDark, { flat: true });
    small.position.set(r * 0.8, r * 0.3, rand(-0.3, 0.3));
    small.rotation.set(rand(0, 3), rand(0, 3), rand(0, 3));
    g.add(small);

    var moss = ballS(r * 0.5, C.leafMid);
    moss.position.set(-r * 0.25, r * 1.15, 0);
    moss.scale.set(1.1, 0.36, 1.1);
    g.add(moss);
    return g;
  }

  /** 草丛 */
  function makePropGrass() {
    var g = new THREE.Group();
    var p = swayed(g, 0.15, rand(1.5, 2.4));    // 草甩得最欢
    for (var i = 0; i < 5; i++) {
      var blade = box(rand(0.07, 0.12), rand(0.4, 0.75), rand(0.07, 0.12), i % 2 ? C.leafLight : C.leafMid, { flat: true });
      blade.position.set(rand(-0.24, 0.24), blade.geometry.parameters.height / 2, rand(-0.24, 0.24));
      blade.rotation.z = rand(-0.4, 0.4);
      blade.rotation.x = rand(-0.3, 0.3);
      p.add(blade);
    }
    // 小黄花
    if (Math.random() > 0.5) {
      var f = ballS(0.09, C.honey, { flat: true });
      f.position.set(rand(-0.2, 0.2), rand(0.45, 0.7), rand(-0.2, 0.2));
      p.add(f);
    }
    return g;
  }

  /** 树桩 */
  function makePropStump() {
    var g = new THREE.Group();
    var h = rand(0.45, 0.85);
    var stump = cyl(0.38, 0.46, h, 8, C.trunk, { flat: true });
    stump.position.y = h / 2;
    g.add(stump);

    var face = cyl(0.36, 0.36, 0.06, 8, C.plank, { flat: true });
    face.position.y = h;
    g.add(face);

    var ring = cyl(0.18, 0.18, 0.07, 8, C.woodDark, { flat: true });
    ring.position.y = h + 0.01;
    g.add(ring);

    var moss = ballS(0.2, C.leafMid);
    moss.position.set(0.3, 0.12, 0.1);
    moss.scale.y = 0.5;
    g.add(moss);
    return g;
  }

  /** 木栅栏 */
  function makePropFence() {
    var g = new THREE.Group();
    for (var i = -1; i <= 1; i++) {
      var post = box(0.14, rand(0.85, 1.1), 0.14, C.woodDark, { flat: true });
      post.position.set(i * 0.7, post.geometry.parameters.height / 2, 0);
      g.add(post);
    }
    for (var j = 0; j < 2; j++) {
      var rail = box(1.6, 0.13, 0.1, C.plank, { flat: true });
      rail.position.set(0, 0.42 + j * 0.34, 0);
      g.add(rail);
    }
    return g;
  }

  /** 原木堆 */
  function makePropWoodpile() {
    var g = new THREE.Group();
    var rows = [3, 2, 1];
    var y = 0.26;
    for (var r = 0; r < rows.length; r++) {
      var n = rows[r];
      for (var i = 0; i < n; i++) {
        var log = cyl(0.24, 0.24, 1.7, 8, i % 2 ? C.wood : C.woodDark, { flat: true });
        log.rotation.z = Math.PI / 2;
        log.position.set(0, y, (i - (n - 1) / 2) * 0.5);
        g.add(log);
      }
      y += 0.44;
    }
    return g;
  }

  /** 小木屋（远景氛围担当） */
  function makePropCabin() {
    var g = new THREE.Group();
    var w = rand(2.6, 3.4);
    var h = rand(1.9, 2.4);
    var d = rand(2.2, 2.8);

    var body = box(w, h, d, C.wood, { flat: true });
    body.position.y = h / 2;
    g.add(body);

    // 横向木纹
    for (var i = 1; i < 4; i++) {
      var line = box(w + 0.04, 0.07, d + 0.04, C.woodDark, { flat: true });
      line.position.y = (h / 4) * i;
      g.add(line);
    }

    // 屋顶（两层方锥感）
    var roof = cone(Math.max(w, d) * 0.78, h * 0.78, 4, C.roof, { flat: true });
    roof.position.y = h + h * 0.39;
    roof.rotation.y = Math.PI / 4;
    g.add(roof);

    var rim = box(w + 0.3, 0.14, d + 0.3, C.woodDark, { flat: true });
    rim.position.y = h + 0.02;
    g.add(rim);

    // 门
    var door = box(0.62, 1.1, 0.08, C.woodDark, { flat: true });
    door.position.set(-w * 0.18, 0.55, d / 2 + 0.02);
    g.add(door);

    // 窗
    var win = box(0.5, 0.5, 0.08, 0xbfe4f2, { flat: true, emissive: 0x2a3d46 });
    win.position.set(w * 0.26, h * 0.62, d / 2 + 0.02);
    g.add(win);

    // 烟囱
    var chim = box(0.3, 0.7, 0.3, C.rockDark, { flat: true });
    chim.position.set(w * 0.28, h + h * 0.42, -d * 0.2);
    g.add(chim);

    return g;
  }

  /** 木指示牌 */
  function makePropSign() {
    var g = new THREE.Group();
    var h = rand(1.3, 1.7);

    var post = cyl(0.09, 0.11, h, 6, C.woodDark, { flat: true });
    post.position.y = h / 2;
    g.add(post);

    var board = box(rand(0.8, 1.1), 0.42, 0.09, C.plank, { flat: true });
    board.position.y = h * 0.86;
    board.rotation.y = rand(-0.3, 0.3);
    g.add(board);

    // 牌子上的"字"
    for (var i = 0; i < 3; i++) {
      var t = box(0.5, 0.05, 0.03, C.woodDark, { flat: true });
      t.position.set(0, h * 0.86 + 0.1 - i * 0.11, 0.06);
      t.rotation.y = board.rotation.y;
      g.add(t);
    }
    return g;
  }

  /** 树上的蜂巢 */
  function makePropHive() {
    var g = new THREE.Group();
    var p = swayed(g, 0.024, rand(0.5, 0.85));
    var h = rand(2.2, 3.0);

    var trunk = cyl(0.3, 0.4, h, 7, C.trunk, { flat: true });
    trunk.position.y = h / 2;
    p.add(trunk);

    var canopy = ballS(h * 0.36, C.leafMid);
    canopy.position.y = h * 0.94;
    canopy.scale.y = 0.85;
    p.add(canopy);

    // 蜂巢
    var hive = cyl(0.32, 0.38, 0.72, 8, C.hive, { flat: true, emissive: 0x4a3300 });
    hive.position.set(h * 0.34, h * 0.6, 0);
    p.add(hive);

    var lid = cyl(0.3, 0.24, 0.18, 8, C.wood, { flat: true });
    lid.position.set(h * 0.34, h * 0.6 + 0.44, 0);
    p.add(lid);

    // 几只小蜜蜂
    for (var i = 0; i < 3; i++) {
      var bee = ballS(0.07, C.hazardDark, { flat: true });
      bee.position.set(h * 0.34 + rand(-0.5, 0.5), h * 0.6 + rand(-0.3, 0.4), rand(-0.5, 0.5));
      p.add(bee);
    }
    return g;
  }

  // ==================== 天上的活物 ====================

  var _cloudMat = null;

  /** 云不吃雾：远处的云也得是白的，否则天上一片灰 */
  function cloudMaterial() {
    if (!_cloudMat) {
      _cloudMat = new THREE.MeshLambertMaterial({
        color: C.cloud,
        emissive: 0x92a8bd,
        flatShading: true,
        fog: false
      });
    }
    return _cloudMat;
  }

  /** 一朵云：几团压扁的球连成一串 */
  function makeCloud() {
    var g = new THREE.Group();
    var m = cloudMaterial();
    var n = 3 + Math.floor(Math.random() * 3);
    var radii = [];
    var span = 0;
    var i;
    for (i = 0; i < n; i++) { radii.push(rand(2.2, 4.4)); span += radii[i] * 1.15; }

    var x = -span / 2;
    for (i = 0; i < n; i++) {
      var b = new THREE.Mesh(new THREE.SphereGeometry(radii[i], 8, 6), m);
      b.position.set(x + radii[i] * 0.58, rand(-0.5, 0.7), rand(-1.8, 1.8));
      b.scale.set(1, rand(0.5, 0.68), 1);
      g.add(b);
      x += radii[i] * 1.15;
    }
    return g;
  }

  /** 一只鸟（朝着 -Z 建模，World 靠 rotation.y 让它朝着飞行方向） */
  function makeBird() {
    var g = new THREE.Group();
    var m = mat(C.bird, { flat: true });

    var body = new THREE.Mesh(new THREE.SphereGeometry(0.26, 7, 5), m);
    body.scale.set(0.85, 0.8, 1.9);
    g.add(body);

    var head = new THREE.Mesh(new THREE.SphereGeometry(0.17, 6, 5), m);
    head.position.set(0, 0.09, -0.42);
    g.add(head);

    var beak = new THREE.Mesh(new THREE.ConeGeometry(0.07, 0.2, 5), mat(0xe6a13c, { flat: true }));
    beak.rotation.x = -Math.PI / 2;
    beak.position.set(0, 0.06, -0.6);
    g.add(beak);

    var tail = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.06, 0.42), m);
    tail.position.set(0, 0.03, 0.48);
    g.add(tail);

    // 翅膀挂在独立枢轴上，扑翅就是转这个枢轴
    var wings = [];
    for (var s = -1; s <= 1; s += 2) {
      var pivot = new THREE.Group();
      pivot.position.set(s * 0.06, 0.06, 0.02);
      var wing = new THREE.Mesh(new THREE.BoxGeometry(1.05, 0.06, 0.42), m);
      wing.position.set(s * 0.55, 0, -0.06);
      pivot.add(wing);
      g.add(pivot);
      wings.push({ node: pivot, side: s });
    }
    g.userData.wings = wings;
    return g;
  }

  // ==================== 区域专属景物 ====================
  // 每个区域摆一批"只有这儿才有"的东西，进区时整批显形，一眼就知道换地方了。

  /** 第 2 区 · 密林深处：垂满藤蔓的老树 */
  function makeDecorVine() {
    var g = new THREE.Group();
    var p = swayed(g, 0.024, rand(0.4, 0.7));
    var h = rand(4.6, 6.4);

    var trunk = cyl(0.34, 0.5, h, 7, C.trunkDark, { flat: true });
    trunk.position.y = h / 2;
    p.add(trunk);

    for (var i = 0; i < 3; i++) {
      var leaf = ballS(h * rand(0.26, 0.34), i % 2 ? C.leafDark : C.leafMid);
      leaf.position.set(rand(-0.7, 0.7), h * rand(0.86, 1.0), rand(-0.7, 0.7));
      leaf.scale.y = 0.8;
      p.add(leaf);
    }

    // 从树冠垂下来的藤（带几片叶子）
    for (var k = 0; k < 5; k++) {
      var len = rand(1.0, 2.6);
      var vx = rand(-1.7, 1.7);
      var vz = rand(-1.5, 1.5);
      var top = h * 0.82;
      var vine = box(0.09, len, 0.09, C.vine, { flat: true });
      vine.position.set(vx, top - len / 2, vz);
      vine.rotation.z = rand(-0.12, 0.12);
      p.add(vine);
      for (var m = 0; m < 3; m++) {
        var lf = ballS(0.13, C.leafLight);
        lf.position.set(vx + rand(-0.12, 0.12), top - rand(0.1, len), vz + rand(-0.12, 0.12));
        lf.scale.set(1, 0.5, 0.8);
        p.add(lf);
      }
    }
    return g;
  }

  /** 第 2 区 · 密林深处：长满苔藓的巨石 */
  function makeDecorMossRock() {
    var g = new THREE.Group();
    var r = rand(1.3, 2.2);

    var rock = ball(r, C.rock, { flat: true });
    rock.position.y = r * 0.6;
    rock.scale.set(1, rand(0.72, 0.98), rand(0.9, 1.15));
    rock.rotation.set(rand(0, 3), rand(0, 3), rand(0, 3));
    g.add(rock);

    for (var i = 0; i < 4; i++) {
      var moss = ballS(r * rand(0.28, 0.46), i % 2 ? C.leafMid : C.leafDark);
      moss.position.set(rand(-r * 0.6, r * 0.6), r * rand(0.85, 1.25), rand(-r * 0.6, r * 0.6));
      moss.scale.set(1, 0.42, 1);
      g.add(moss);
    }
    return g;
  }

  /** 第 3 区 · 伐木场：码好的大原木堆 */
  function makeDecorLogPile() {
    var g = new THREE.Group();
    var rows = [4, 3, 2, 1];
    var y = 0.3;

    for (var r = 0; r < rows.length; r++) {
      var n = rows[r];
      for (var i = 0; i < n; i++) {
        var len = rand(3.4, 4.6);
        var z = (i - (n - 1) / 2) * 0.58;
        var log = cyl(0.28, 0.3, len, 8, i % 2 ? C.wood : C.trunk, { flat: true });
        log.rotation.z = Math.PI / 2;
        log.position.set(0, y, z);
        g.add(log);

        // 锯开的断面：一眼看出是"木头"不是石头
        for (var s = -1; s <= 1; s += 2) {
          var cap = cyl(0.27, 0.27, 0.07, 8, C.plank, { flat: true });
          cap.rotation.z = Math.PI / 2;
          cap.position.set(s * (len / 2 + 0.02), y, z);
          g.add(cap);
        }
      }
      y += 0.52;
    }
    return g;
  }

  /** 第 3 区 · 伐木场：锯木架（上面还靠着把大锯子） */
  function makeDecorSawHorse() {
    var g = new THREE.Group();

    for (var s = -1; s <= 1; s += 2) {
      for (var d = -1; d <= 1; d += 2) {
        var leg = box(0.12, 1.15, 0.12, C.woodDark, { flat: true });
        leg.position.set(s * 0.78, 0.5, d * 0.26);
        leg.rotation.z = s * 0.24;
        g.add(leg);
      }
    }

    var top = box(1.95, 0.16, 0.34, C.wood, { flat: true });
    top.position.y = 1.02;
    g.add(top);

    var blade = box(1.6, 0.06, 0.3, C.steel, { flat: true });
    blade.position.set(0.12, 1.24, 0.18);
    blade.rotation.z = 0.22;
    g.add(blade);

    var grip = box(0.24, 0.34, 0.12, C.roof, { flat: true });
    grip.position.set(-0.72, 1.42, 0.18);
    g.add(grip);
    return g;
  }

  /** 第 3 区 · 伐木场：新锯出来的木板垛 */
  function makeDecorPlankStack() {
    var g = new THREE.Group();
    for (var i = 0; i < 5; i++) {
      var l = box(rand(2.4, 3.2), 0.16, rand(1.2, 1.8), i % 2 ? C.plank : C.wood, { flat: true });
      l.position.set(rand(-0.12, 0.12), 0.12 + i * 0.19, rand(-0.1, 0.1));
      l.rotation.y = rand(-0.1, 0.1);
      g.add(l);
    }
    return g;
  }

  /** 第 4 区 · 光头强工地：塔吊 */
  function makeDecorCrane() {
    var g = new THREE.Group();
    var H = rand(9, 13);

    var mast = box(0.72, H, 0.72, C.hazard, { flat: true });
    mast.position.y = H / 2;
    g.add(mast);

    var rungs = Math.floor(H / 1.2);
    for (var i = 0; i < rungs; i++) {
      var a = box(0.86, 0.09, 0.09, C.hazardDark, { flat: true });
      a.position.set(0, 0.7 + i * 1.2, 0.34);
      g.add(a);
      var b = box(0.86, 0.09, 0.09, C.hazardDark, { flat: true });
      b.position.set(0, 0.7 + i * 1.2, -0.34);
      g.add(b);
    }

    // 转台上的大臂与配重
    var jib = box(0.52, 0.52, 7.6, C.steel, { flat: true });
    jib.position.set(0, H + 0.45, -3.5);
    g.add(jib);

    var counter = box(0.78, 0.78, 2.2, C.steelDark, { flat: true });
    counter.position.set(0, H + 0.45, 1.5);
    g.add(counter);

    // 吊索与吊钩
    var cable = box(0.07, 5.0, 0.07, C.hazardDark, { flat: true });
    cable.position.set(0, H - 2.0, -4.8);
    g.add(cable);

    var hook = box(0.56, 0.56, 0.56, C.steelDark, { flat: true });
    hook.position.set(0, H - 4.8, -4.8);
    g.add(hook);
    return g;
  }

  /** 第 4 区 · 光头强工地：集装箱 */
  function makeDecorContainer() {
    var g = new THREE.Group();
    var W = rand(2.4, 2.7);
    var H = 2.4;
    var D = rand(5.4, 6.2);
    var col = Math.random() < 0.5 ? C.container : C.containerAlt;

    var body = box(W, H, D, col, { flat: true });
    body.position.y = H / 2 + 0.06;
    g.add(body);

    var ribs = Math.floor(D / 0.55);
    for (var i = 0; i < ribs; i++) {
      var rib = box(W + 0.07, H * 0.84, 0.12, col, { flat: true });
      rib.position.set(0, H / 2 + 0.06, -D / 2 + 0.4 + i * 0.55);
      g.add(rib);
    }

    var door = box(W * 0.96, H * 0.9, 0.1, C.steelDark, { flat: true });
    door.position.set(0, H / 2 + 0.06, D / 2 + 0.03);
    g.add(door);

    var base = box(W * 1.06, 0.24, D * 1.02, C.steelDark, { flat: true });
    base.position.y = 0.12;
    g.add(base);
    return g;
  }

  /** 第 4 区 · 光头强工地：油桶堆 */
  function makeDecorBarrels() {
    var g = new THREE.Group();
    var spots = [
      [0, 0, 0], [0.64, 0, 0.22], [-0.6, 0, -0.26],
      [0.12, 0.9, 0.02], [-0.34, 0.9, -0.5], [0.36, 1.8, 0.08]
    ];
    for (var i = 0; i < spots.length; i++) {
      var sx = spots[i][0];
      var sy = 0.45 + spots[i][1];
      var sz = spots[i][2];
      var body = cyl(0.32, 0.32, 0.88, 10, i % 3 === 0 ? C.steelDark : C.barrel, { flat: true });
      body.position.set(sx, sy, sz);
      g.add(body);

      for (var r = -1; r <= 1; r += 2) {
        var ring = cyl(0.34, 0.34, 0.08, 10, C.steelDark, { flat: true });
        ring.position.set(sx, sy + r * 0.22, sz);
        g.add(ring);
      }
    }
    return g;
  }

  /** 第 4 区 · 光头强工地：水泥堆 + 水泥袋 */
  function makeDecorConcretePile() {
    var g = new THREE.Group();

    var pile = cone(1.9, 1.5, 8, C.concrete, { flat: true });
    pile.position.y = 0.74;
    pile.scale.z = 0.82;
    g.add(pile);

    var bags = [
      [-1.65, 0.24, 0.4], [-1.5, 0.24, -0.55], [-1.25, 0.72, 0.0],
      [1.7, 0.24, -0.2], [1.5, 0.72, 0.26]
    ];
    for (var i = 0; i < bags.length; i++) {
      var bag = box(1.05, 0.44, 0.68, C.concreteDark, { flat: true });
      bag.position.set(bags[i][0], bags[i][1], bags[i][2]);
      bag.rotation.y = rand(-0.32, 0.32);
      g.add(bag);

      var strap = box(1.1, 0.09, 0.72, C.steel, { flat: true });
      strap.position.copy(bag.position);
      strap.rotation.y = bag.rotation.y;
      g.add(strap);
    }
    return g;
  }

  // 区域号 -> 该区域专属景物的候选清单（第 1 区就是纯森林，不加摆件）
  var DECOR_TABLE = {
    1: [makeDecorVine, makeDecorMossRock],
    2: [makeDecorLogPile, makeDecorSawHorse, makeDecorPlankStack],
    3: [makeDecorCrane, makeDecorContainer, makeDecorBarrels, makeDecorConcretePile]
  };

  function makeRegionDecor(stageIndex) {
    var list = DECOR_TABLE[stageIndex];
    if (!list) return null;
    return pick(list)();
  }

  // 加权随机的路边景物表（森林感为主）
  var PROP_TABLE = [
    { w: 26, fn: makePropTree },
    { w: 14, fn: makePropPine },
    { w: 17, fn: makePropBush },
    { w: 8,  fn: makePropMushroom },
    { w: 12, fn: makePropGrass },
    { w: 8,  fn: makePropRock },
    { w: 5,  fn: makePropStump },
    { w: 4,  fn: makePropFence },
    { w: 4,  fn: makePropWoodpile },
    { w: 2,  fn: makePropSign },
    { w: 3,  fn: makePropHive },
    { w: 3,  fn: makePropCabin }
  ];
  var PROP_TOTAL = PROP_TABLE.reduce(function (s, e) { return s + e.w; }, 0);

  function makeRandomProp() {
    var roll = Math.random() * PROP_TOTAL;
    for (var i = 0; i < PROP_TABLE.length; i++) {
      roll -= PROP_TABLE[i].w;
      if (roll <= 0) return PROP_TABLE[i].fn();
    }
    return PROP_TABLE[0].fn();
  }

  /** 角色脚下的假阴影 */
  function makeBlobShadow(radius) {
    var m = new THREE.MeshBasicMaterial({
      color: 0x2b2416,
      transparent: true,
      opacity: 0.2,
      depthWrite: false
    });
    var shadow = new THREE.Mesh(new THREE.CircleGeometry(radius, 14), m);
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.02;
    return shadow;
  }

  RR.Art = {
    mat: mat,
    box: box,
    cyl: cyl,
    ball: ball,
    ballS: ballS,
    cone: cone,
    rand: rand,
    pick: pick,
    makeSky: makeSky,
    makeGroundTile: makeGroundTile,
    groundMaterials: groundMaterials,
    makeCloud: makeCloud,
    makeBird: makeBird,
    makeRegionDecor: makeRegionDecor,
    makeLowBarrier: makeLowBarrier,
    makeHighBarrier: makeHighBarrier,
    makeRubbleBlock: makeRubbleBlock,
    makeMover: makeMover,
    makeCoin: makeCoin,
    makeHealPack: makeHealPack,
    makeBoostPad: makeBoostPad,
    makeRandomProp: makeRandomProp,
    makeBlobShadow: makeBlobShadow
  };

})(window);
