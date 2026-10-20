/**
 * 全局参数表 —— 调手感只需要改这里，不用动逻辑代码。
 * 经典脚本风格，挂在 window.RR.CFG 上。
 */
(function (global) {
  'use strict';

  var RR = global.RR = global.RR || {};

  // ---------- 角色可选字段的默认值 ----------
  // 角色表里只写要改的那几项，其余由 getCharacter 补齐。
  var DEFAULT_BUILD = { leg: 1, torso: 1, head: 1, limb: 1, tail: 1, ear: 1 };
  var DEFAULT_FACE = { eye: 'round', mouth: 'grin', tuft: 'floppy', brow: 'arch' };
  var DEFAULT_ANIM = { jump: 'tuck', slide: 'back', runSwing: 0.62 };

  /** 浅拷贝：把 over 里写了的字段盖到 base 上；over 为空则直接用 base 的默认值 */
  function shallowCopy(base, over) {
    var out = {};
    var k;
    for (k in base) {
      if (Object.prototype.hasOwnProperty.call(base, k)) out[k] = base[k];
    }
    if (over) {
      for (k in over) {
        if (Object.prototype.hasOwnProperty.call(over, k)) out[k] = over[k];
      }
    }
    return out;
  }

  RR.CFG = {
    // ---------- 赛道 ----------
    LANE_WIDTH: 2.15,
    LANES: [-2.15, 0, 2.15],
    LANE_TOLERANCE: 0.92,

    // ---------- 玩家 ----------
    PLAYER: {
      height: 1.72,
      slideHeight: 0.72,
      laneSwitchTime: 0.13,
      jumpVelocity: 8.6,
      gravity: 25.5,
      slideDuration: 0.62,
      slideCooldown: 0.15,
      invulnTime: 1.15
    },

    // ---------- 速度与难度 ----------
    SPEED: { start: 8.5, max: 25, gainPerMeter: 0.011 },
    SPAWN: { startGap: 15.5, minGap: 6.8, rampMeters: 1500 },
    HP: { start: 3, max: 5 },
    SCORE: { perMeter: 1, perCoin: 20, boostMul: 2 },
    BOOST: { duration: 3.2, speedMul: 1.45 },

    // 分段解锁：跑到指定距离后出现新障碍
    // 节奏：约 19s / 23s / 23s 进入下一区（速度 8.5→25 m/s，gainPerMeter 0.011）
    // sub 只在"进入新区域"横幅上显示一句玩法提示，ObstacleManager 不读它。
    DIFFICULTY: [
      { at: 0,   name: '第 1 区 · 狗熊岭入口', sub: '跳过木栅栏，滑铲钻过横倒的树干',       kinds: ['low', 'high'] },
      { at: 180, name: '第 2 区 · 密林深处',   sub: '树越来越密，石头木头堆要换道绕开',     kinds: ['low', 'high', 'block'] },
      { at: 450, name: '第 3 区 · 伐木场',     sub: '原木顺着坡滚下来，看准空隙再走',       kinds: ['low', 'high', 'block', 'mover'] },
      { at: 800, name: '第 4 区 · 光头强工地', sub: '光头强的地盘，障碍连成一串，别硬撞',   kinds: ['low', 'high', 'block', 'mover', 'combo'] }
    ],

    /**
     * 每个区域的主色调（雾 / 光 / 地面）。
     * 第 1 区就是原来那套"明亮森林"，改这里等于改全局默认观感；
     * 后面几区逐段变闷、变黄、变灰，让玩家一眼看出"换地方了"。
     * 只改颜色与雾距，不动任何玩法数值。
     */
    STAGE_PALETTE: [
      {
        fog: 0xcfe6c4, near: 70, far: 205,
        hemiSky: 0xd8ecff, hemiGround: 0x8d8459, hemi: 0.5,
        sun: 0xfff4c8, sunIntensity: 0.55,
        road: 0xa8834e, grass: 0x5faa34, grassDark: 0x468524
      },
      {
        // 密林深处：树冠更密，空气发绿发暗
        fog: 0xa9cd8c, near: 55, far: 165,
        hemiSky: 0xcbe6c0, hemiGround: 0x77814a, hemi: 0.48,
        sun: 0xfff0a8, sunIntensity: 0.5,
        road: 0x9a7846, grass: 0x4c9a2c, grassDark: 0x38701f
      },
      {
        // 伐木场：扬起的木屑尘土把空气染黄
        fog: 0xdbd0a8, near: 64, far: 188,
        hemiSky: 0xf0e6c2, hemiGround: 0x8b7f4a, hemi: 0.52,
        sun: 0xffe9a4, sunIntensity: 0.6,
        road: 0xb18d54, grass: 0x6a9c39, grassDark: 0x4c7c26
      },
      {
        // 光头强工地：水泥灰把绿色压下去
        fog: 0xc8c7b4, near: 58, far: 175,
        hemiSky: 0xd8e4ec, hemiGround: 0x86806a, hemi: 0.5,
        sun: 0xf4f0e2, sunIntensity: 0.52,
        road: 0x9a9482, grass: 0x6d9052, grassDark: 0x536f3c
      }
    ],

    // ---------- 光头强：追逐 + 处决 ----------
    // 本作是"跑步机"模式：熊二永远在 z=0，景物向 +z 移动。
    // 所以光头强的 z 不是滚动量，而是"落后熊二多少米"——他自己保持在这个落后量上。
    VICK: {
      zChase: 1.55,        // 追逐时落后熊二的米数（越小贴得越近，画面里越大只）
      zNear: 0.95,         // "重新追上来"时的最近距离
      zFar: 3.4,           // 追不上时退到的最远处（退到这儿基本已淡出画面）
      zExec: 0.92,         // 处决时停住的位置
      lateral: 1.45,       // 横向错开，避免挡住熊二
      lateralExec: 0.66,   // 处决时收拢到熊二身边
      followLag: 0.30,     // 横向跟随熊二轨迹的延迟（秒）—— 制造"追"的感觉
      followK: 3.4,        // 横向跟随的平滑速度
      sideSwapK: 2.4,      // 换边（跑到熊二另一侧）的平滑速度
      introChase: 2.9,     // 开场紧追时长（秒），之后开始落后
      fallK: 1.5,          // 落后的速度
      rushK: 9.0,          // 掉血后冲回来的速度
      reengageHold: 2.4,   // 掉血后重新追上并保持的时长
      surge: 0.42,         // 追逐中掉血的"猛扑"幅度（米）
      brake: 9.0,          // 死亡后熊二的减速（m/s²）
      execApproach: 0.42,  // 处决：扑到熊二身边
      execRaise: 0.42,     // 处决：举斧
      execSwing: 0.22,     // 处决：劈下
      execHold: 1.55,      // 处决：定住 + 字幕停留
      minDeathTime: 1.0    // 死亡演出至少持续这么久才结算
    },

    // ---------- 处决镜头：绕到侧面 3/4 视角，把熊二和光头强一起框进来 ----------
    // 相机站在熊二右后方：光头强从左侧（远离相机那一侧）扑上来，熊二在前景，
    // 两人在画面上左右分开约 12% 宽度，不会互相遮挡。
    CINE: {
      offsetX: 3.4,
      offsetY: 2.45,
      offsetZ: 3.1,
      lookX: 0,
      lookY: 0.95,
      lookZ: 0.5,
      switchTime: 0.85
    },

    // ---------- 道具投放 ----------
    PICKUP: {
      healEveryMeters: 300,
      boostEveryMeters: 240,
      coinRowChance: 0.72,
      coinRowMin: 3,
      coinRowMax: 6
    },

    // ---------- 世界回收 ----------
    WORLD: {
      visibleLength: 190,
      recycleBehind: 12,
      groundTileLength: 12,
      propSpacing: 8.0,

      // 地面贴图：一层噪声细节，让土路在跑动时有"颗粒在动"的质感。
      // groundTexScroll = 贴图偏移随世界滚动的比例（1 = 贴图在世界里钉住不动，
      // 这时 12 米的重复块就看不出来了；0 = 贴图跟着地面块一起走）。
      groundTexMeters: 2.2,
      groundTexScroll: 1.0,

      // 换区域时配色过渡的快慢（1.1 ≈ 一秒左右换完大半）
      paletteLerp: 1.1,

      // 天上/天上的活物
      cloudCount: 10,
      cloudSpanX: 78,        // 云在 ±X 上的分布范围（回收边界）
      birdPool: 5,
      birdMinGap: 6.5,       // 两群鸟之间的间隔（秒）
      birdMaxGap: 15,

      // 区域专属景物：比路边景物稀得多，只在对应区域显形
      decorSpacing: 26,
      decorPopZ: -70         // 远处 Z 之外才允许切换显隐，避免在眼前凭空冒出来
    },

    // ---------- 配色：熊出没式的明亮森林 + 蜂蜜金黄 ----------
    COLORS: {
      // 天空与空气
      fog: 0xcfe6c4,
      skyTop: 0x2f86d8,
      skyMid: 0x8dcaf0,
      skyLow: 0xf9eab4,
      sun: 0xfff4c8,

      // 地面：林间土路 + 鲜绿草地
      dirt: 0xa8834e,
      dirtDark: 0x8f6c3d,
      dirtSpot: 0x997544,
      grass: 0x5faa34,
      grassDark: 0x468524,
      laneEdge: 0x74bd45,
      pebble: 0xc4ad86,

      // 树木
      trunk: 0x8a5a32,
      trunkDark: 0x6b4425,
      leafLight: 0x7ed04f,
      leafMid: 0x59b338,
      leafDark: 0x3f8f2a,

      // 木头建筑
      wood: 0xb57c42,
      woodDark: 0x8a5a2b,
      plank: 0xd39f61,
      roof: 0xc0522f,

      // 天空里的活物（不吃雾，远看也清楚）
      cloud: 0xf7fcff,
      cloudShade: 0xdce9f5,
      bird: 0x40474f,

      // 伐木场 / 工地的工业色
      steel: 0x8e9aa4,
      steelDark: 0x6b7580,
      container: 0xc85a34,
      containerAlt: 0x3f7fb0,
      barrel: 0xd4762a,
      concrete: 0xbcb5a3,
      concreteDark: 0x9b9484,
      vine: 0x4f9c33,

      // 石头
      rock: 0xafae9f,
      rockDark: 0x83826f,

      // 道具
      honey: 0xf7c531,
      honeyDark: 0xcf9612,
      honeyGlow: 0xffe066,
      apple: 0xe0432f,
      appleDark: 0xa82c1c,
      hive: 0xe8b53a,

      // 功能性警示色（保留"一眼看懂"的识别度）
      hazard: 0xf0b52a,
      hazardDark: 0x2e2a22,

      // 熊二
      bearFur: 0xd9a45e,
      bearFurDark: 0xb8843f,
      bearBelly: 0xf5e3bd,
      bearNose: 0x4a3222,
      bearEyeWhite: 0xfdfbf5,
      bearEyeBlack: 0x241a12,
      bearBlush: 0xea9c88,
      bearTongue: 0xd9605a,

      // 光头强（狗熊岭伐木工：黄安全帽 + 蓝工装背带裤 + 大鼻子黑胡子）
      vickHat: 0xf2c230,
      vickHatDark: 0xc79212,
      vickSkin: 0xf3c9a2,
      vickSkinDark: 0xd9a778,
      vickNose: 0xe39a72,
      vickHair: 0x2a1c11,
      vickShirt: 0x4a86cc,
      vickShirtDark: 0x33619b,
      vickBib: 0x2f5c9c,
      vickPants: 0x6f5a3c,
      vickBoot: 0x4a3520,
      vickAxeWood: 0xb07e46,
      vickAxeSteel: 0xc3ccd2,
      vickAxeSteelDark: 0x8c979e,

      shadow: 0x2b2416
    },

    // ---------- 可选角色 ----------
    // 只描述"长什么样、怎么动"：速度、血量、判定盒、计分规则全部共用，换角色不影响玩法。
    //
    // 重要前提：本作是"跑步机"模式（见下方 VICK 注释），摄像机永远在熊的身后，
    // 玩家全程只看得到熊的背面。所以区分角色只能靠这三样，脸属于附带：
    //   1. build —— 骨架比例（腿长 / 腰围 / 头大小），决定背影轮廓
    //   2. 毛色 —— 背面是画面里最大的一块颜色
    //   3. anim  —— 跳跃与翻滚的动法，从背后看最显眼
    // 想让两个角色更像 / 更不像，改这三处就够了，不用动 Player.js。
    CHARACTERS: [
      {
        id: 'xiong2',
        name: '熊二',
        tagline: '憨头憨脑的狗熊岭第二胖',
        fur: 0xd9a45e,        // 浅棕黄皮毛
        furDark: 0xb8843f,
        belly: 0xf5e3bd,      // 米色肚皮与口鼻
        blush: 0xea9c88,      // 腮红
        hasBlush: true,
        scale: 1,             // 整体体型（纯外观，判定盒由 CFG.PLAYER 决定）
        browW: 0.12,          // 眉毛尺寸
        browH: 0.038,
        muzzleScale: 1,       // 吻部大小
        shadow: 0.48,
        build: { leg: 1, torso: 1, head: 1, limb: 1, tail: 1, ear: 1 },
        face: { eye: 'round', mouth: 'grin', tuft: 'floppy', brow: 'arch' },
        anim: { jump: 'tuck', slide: 'back', runSwing: 0.62 }
      },
      {
        id: 'xiong1',
        name: '熊大',
        tagline: '沉稳可靠的狗熊岭大哥',
        fur: 0x6b3f18,        // 深棕红皮毛：和熊二的浅棕黄在背影里直接拉开明暗
        furDark: 0x472a0c,
        belly: 0xd9c092,
        blush: 0xd98a6a,
        hasBlush: false,      // 熊大不画腮红
        scale: 1.06,          // 整体比熊二大一圈
        browW: 0.17,          // 浓黑一字眉
        browH: 0.056,
        muzzleScale: 1.04,
        shadow: 0.5,
        // 高瘦大长腿：腿长一大截、腰细、头小 —— 背影是根立起来的柱子
        build: { leg: 1.32, torso: 0.86, head: 0.88, limb: 0.9, tail: 0.78, ear: 0.88 },
        face: { eye: 'narrow', mouth: 'flat', tuft: 'spike', brow: 'flat' },
        // 举着双爪往后翻跟头；滑铲是超人式趴着滑
        anim: { jump: 'backflip', slide: 'superman', runSwing: 0.78 }
      }
    ],
    DEFAULT_CHARACTER: 'xiong2',

    /**
     * 按 id 取角色配置，未知 id 一律回退到默认角色。
     * 返回的是"补全过"的副本：角色表里没写的可选字段（build/face/anim）会填上默认值，
     * 这样以后往角色表里加新角色时，只写想改的那几项也不会把 Player.js 弄崩。
     */
    getCharacter: function (id) {
      var list = RR.CFG.CHARACTERS;
      var found = null;
      var i;
      for (i = 0; i < list.length; i++) {
        if (list[i].id === id) { found = list[i]; break; }
      }
      if (!found) {
        for (i = 0; i < list.length; i++) {
          if (list[i].id === RR.CFG.DEFAULT_CHARACTER) { found = list[i]; break; }
        }
      }
      if (!found) found = list[0];

      var out = shallowCopy(found);
      out.build = shallowCopy(DEFAULT_BUILD, found.build);
      out.face = shallowCopy(DEFAULT_FACE, found.face);
      out.anim = shallowCopy(DEFAULT_ANIM, found.anim);
      return out;
    },

    // ---------- 输入层 ----------
    RADAR: {
      enterThreshold: 0.36,   // 进入左右车道的阈值
      exitThreshold: 0.22,    // 回到中间车道的阈值（迟滞，避免抖动）
      jumpThreshold: 0.5,
      crouchThreshold: 0.5,
      minConfidence: 0.3,
      staleMs: 500,           // 超时未收到帧 => 判定雷达掉线
      defaultUrl: 'ws://127.0.0.1:8766'
    },

    STORAGE_KEY: 'bear-run:best-score',
    STORAGE_KEY_INPUT: 'bear-run:input-source',
    STORAGE_KEY_CHAR: 'bear-run:character'
  };

})(window);
