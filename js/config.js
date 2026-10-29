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
      invulnTime: 1.15,

      // 跳跃到最高点时回头一眼：默认机位在背后，整段腾空只看得到后脑勺，
      // 在顶点附近把脑袋拧回来一下，玩家就能看到瞪眼吐舌的正脸。
      //   angle  = 回头角度（弧度），Math.PI 就是彻底转过来
      //   center = 回头发生在跳跃进度（0＝刚起跳，1＝快落地）的哪一点
      //   window = center 前后各多宽的进度内回头（越大转得越早、回来得越晚）
      //   speed  = 转头 / 回头的平滑速度
      headTurn: { angle: Math.PI, center: 0.5, window: 0.2, speed: 17 }
    },

    // ---------- 速度与难度 ----------
    // 速度不是"一条直线爬到底"，而是两段：
    //   热身段 0 -> cruiseAt 米：只从 start 爬到 cruise。玩家要在这一段里学会
    //                            跳 / 滑铲 / 换道，必须给足低压力时间。
    //   爬升段 cruiseAt -> 约 1080 米：每米 +gainPerMeter，到 max 封顶。
    // 旧版是一条直线（gainPerMeter 0.011），前 180 米就吃掉了一半以上的速度余量，
    // 结果"前段太陡、后段太肉"：新手还没学会滑铲，速度已经上来了。
    SPEED: {
      start: 8.5,
      max: 25,
      cruise: 13.5,          // 热身段结束时的速度
      cruiseAt: 180,         // 热身段里程（与第 2 区解锁点对齐）
      gainPerMeter: 0.0128   // 热身段之后每米加速度
    },

    /**
     * 速度曲线（纯函数：只读 SPEED，方便离线断言与调参）。
     * 两段都是直线，在 cruiseAt 处连续，全程单调不减。
     */
    speedAt: function (distance) {
      var s = RR.CFG.SPEED;
      var d = (distance > 0) ? distance : 0;
      if (d <= s.cruiseAt) return s.start + (s.cruise - s.start) * (d / s.cruiseAt);
      var v = s.cruise + (d - s.cruiseAt) * s.gainPerMeter;
      return v > s.max ? s.max : v;
    },

    // 生成间隔：用"时间"定义，再乘当时速度换算成米。
    // 这是整条难度曲线里最关键的一处修正。旧版把间隔从 15.5 米收到 6.8 米，
    // 而速度同时在涨（8.5 -> 25）——两件事一起挤"两行之间的反应时间"：
    //     开局   15.5 / 8.5  = 1.82s
    //     第 4 区 6.8 / 25   = 0.27s   ← 连看清都做不到，实际是"随机送命"
    // 现在只收时间：1.82s -> 1.02s；米数反而是变大的（速度涨得比时间收得快）。
    // 于是"越跑越快"依然成立，而"永远来得及反应"也成立。
    SPAWN: {
      timeGapStart: 1.82,
      timeGapMin: 1.02,
      rampMeters: 1500,       // 时间间隔从 timeGapStart 收到 timeGapMin 所需里程
      minGap: 8.0,            // 米数硬下限（低速或特殊情形下的兜底）
      jitter: [0.86, 1.18],   // 间隔随机抖动：不给玩家"踩点"的节奏
      mercyMeters: 55         // 掉血后的喘息距离：这段里程内不生成双障碍与 combo
    },
    HP: { start: 3, max: 5 },
    SCORE: { perMeter: 1, perCoin: 20, boostMul: 2 },
    BOOST: { duration: 3.2, speedMul: 1.45 },

    // ---------- 连击（任务 2） ----------
    // "一口气吃一路 / 连着擦过去"必须换得到收益，否则玩家没有理由贴着障碍跑。
    //   window      : 多久没吃到东西 / 没擦到障碍就断连（秒）
    //   maxStep     : 倍率最多叠几档
    //   step        : 每档加多少（6 档 = 满档 x2.50）
    //   coinAdd     : 吃一颗蜂蜜加几连
    //   nearMissAdd : 擦过一次障碍加几连
    // 倍率不写死，一律由 comboLevel * step 算出来，离线断言能直接对表。
    COMBO: {
      window: 2.4,
      maxStep: 6,
      step: 0.25,
      coinAdd: 1,
      nearMissAdd: 1
    },

    // ---------- 险躲（任务 2） ----------
    // margin  : 竖向余量（米）—— 跳过矮栏时"脚底离栏顶"多高以内算擦过去
    // xMargin : 横向余量（米）—— 绕开实心堆时"车身离它"多近以内算擦过去
    // score   : 一次险躲的基础分（还要再乘连击倍率与加速倍率）
    // close   : 余量小到这个数以内，提示文案升级成"毫厘之间"
    // 说明：悬吊物（钻树干）没有对应的竖向险躲 —— 蹲下后头顶离洞底恒为 0.46 米，
    // 一旦收进 margin，就等于"每次滑铲白送分"，所以这里刻意不覆盖它。
    NEAR_MISS: { margin: 0.34, xMargin: 0.52, score: 30, close: 0.12 },

    // ---------- 每局随机挑战（任务 3） ----------
    // 每局开局从下表随机抽一条（上一条不会连着出现两次），达标一次性给 bonus。
    // bonus 还会再乘加速倍率，所以"压着加速带刚好冲线"是有额外回报的。
    //   id     : 稳定标识（纪录与调试参数都用它，改文案别改它）
    //   metric : 用哪个数据算进度 —— 由 Game._challengeProgress() 映射，必须是它认识的键
    //   target : 达标线
    //   bonus  : 完成奖励分
    //   label  : HUD 徽标上的名字（短）
    //   text   : 结算面板里的一句话
    // 设计口径（三条，难度大致递进：顺手 → 堆命 → 真难）：
    //   honey  —— 顺手的收益，鼓励走蜂蜜滑道（15 罐 ≈ 一局中等水平）
    //   long   —— 纯里程，新手也能靠命堆出来，属于"低难度保底"
    //   steady —— "连续不撞"最容易理解，也最容易翻车（撞一下就归零）
    // 注：险躲 / 连击 两条挑战（原 dodge / combo）已从每局目标里下线，
    //     但险躲与连击机制本身仍在计分，只是更适合留作长期成就。
    CHALLENGES: [
      { id: 'honey',  metric: 'coins',       target: 15,  bonus: 200,
        label: '蜂蜜猎人', text: '一局吃到 15 罐蜂蜜' },
      { id: 'long',   metric: 'distance',    target: 600, bonus: 300,
        label: '森林疾行', text: '一局跑满 600 米' },
      { id: 'steady', metric: 'cleanMeters', target: 250, bonus: 250,
        label: '无伤高手', text: '连续 250 米不撞障碍' }
    ],

    // ---------- 结算与个人纪录（任务 4） ----------
    // 纪录按**维度**分开存，而不是只看分数：得分高 != 距离远（险躲和连击也加分），
    // 分开存玩家才有多个可以逐个突破的目标，也才有"下一局追什么"可讲。
    //   historyMax : 本地最多留几局历史（结算面板只列 showHistory 条）
    RECORDS: {
      historyMax: 5,
      showHistory: 3
    },

    // 本局亮点：够门槛才上榜（按下面写的顺序取前 max 条）。
    // 门槛刻意定得比"随便打一局"高一点，否则每局都是同一串字，等于没亮点。
    HIGHLIGHT: {
      max: 3,
      noHitDistance: 250,   // 一次没撞 + 跑够这么远 -> "毫发无伤"
      nearMiss: 3,          // 险躲次数
      combo: 3,             // 最长连击
      coins: 10,            // 蜂蜜罐数
      distance: 300         // 里程（米）
    },

    // 分段解锁：跑到指定距离后出现新障碍。
    //   doubleChance = 这一行再堵第二条道的概率（永远留一条活路）
    //   comboChance  = 这一行直接堵死两条道、只留一条的概率（只有第 4 区才有）
    // 旧版这两个数是死写在 ObstacleManager 里的（0.24 / 0.2），第 1 区就在掷双障碍，
    // 与"第 1 区只教跳和滑铲"的设计意图相矛盾。挪进分区配置后，难度是看得见的表。
    // sub 只在"进入新区域"横幅上显示一句玩法提示，ObstacleManager 不读它。
    DIFFICULTY: [
      { at: 0,   name: '第 1 区 · 狗熊岭入口', sub: '跳过木栅栏，滑铲钻过横倒的树干',
        kinds: ['low', 'high'], doubleChance: 0, comboChance: 0 },
      { at: 180, name: '第 2 区 · 密林深处', sub: '树越来越密，石头木头堆要换道绕开',
        kinds: ['low', 'high', 'block'], doubleChance: 0.16, comboChance: 0 },
      { at: 450, name: '第 3 区 · 伐木场', sub: '原木顺着坡滚下来，看准空隙再走',
        kinds: ['low', 'high', 'block', 'mover'], doubleChance: 0.24, comboChance: 0 },
      { at: 800, name: '第 4 区 · 光头强工地', sub: '光头强的地盘，障碍连成一串，别硬撞',
        kinds: ['low', 'high', 'block', 'mover', 'combo'], doubleChance: 0.30, comboChance: 0.22 }
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
      defaultUrl: 'ws://127.0.0.1:8766',

      // ---- 双雷达（两台同时接，按维度分工）----
      // 只有 dual=true 且 udUrl 有值时才启用；否则仍然是上面的单雷达。
      //   lrUrl = 管左右的雷达：只取 x  -> 产生 left / right（换道）
      //   udUrl = 管上下的雷达：只取 jump / crouch / height -> 产生 jump / slide
      // 两台雷达可以发同一套完整帧格式：游戏侧在进解码器前会按通道裁剪维度，
      // 所以数据侧不需要为"我是哪一路"改协议，也不会互相抢动作。
      dual: false,
      lrUrl: null,            // null = 用 defaultUrl
      udUrl: null,            // 例：ws://192.168.1.7:8767

      // ---- 离线回放（?input=replay）：把真实采集文件解出的帧按原帧率喂进游戏 ----
      replayUrl: './_radarreplay/radar-action-1020.js',
      replaySeries: null,     // null = 用数据里的第一个站点
      replayK: 3.2,           // 占位判定层的 z 分数阈值（越大越不容易误触发）
      replaySpeed: 1,         // 倍速
      replayLoop: true,
      replayWindow: 40,       // 滑动窗口帧数（≈4s @10Hz）
      // 'energy'  = 真实雷达特征 + z 分数占位判定（= 现状识别能力）
      // 'truth'   = 采集时的动作真值直接驱动（= 识别 100% 正确的上限对照）
      // 'azimuth' = 幅度门控 + 双接收相位差(方位)驱动换道（需 v3 数据里的 az 数组）
      replayMode: 'energy',
      replayFrom: 0,          // 从第几帧开始回放（联调时直接跳到某个动作段）
      // ---- 方位(azimuth)解码参数：只在 replayMode='azimuth' 时生效 ----
      // cap-long-1020 实测：静息相位差中位 ≈ -1.13 rad；向左横移会稳定偏到 -1.5~-2.2，
      // 而向右、下蹲、起跳的相位差与静息互相重叠 -> 只给「左」设阈值，
      // 右向阈值留到拿到真实右移真值再开（现设 9.9 = 永不触发）。
      replayAzLeft: null,     // 窗内方位中位 <= 此值 => x = -1（左）；null = 用数据里 series 的标定值
      replayAzRight: null,    // 窗内方位中位 >= 此值 => x = +1（右）
      replayAmpGate: null,    // 幅度门控：只采信幅度高于此值的帧（口径 = series.energy_bin）
      replayAzNeed: null,     // 滑动窗内至少这么多高幅帧才判定，避免单帧抖动
      replayAzWin: null,      // 方位滑动窗长度（帧，10 帧 ≈ 1.3s @7.72Hz）

      // ---- 开局校准（新手教程，js/Calibration.js）：现场测符号 + 基线 ----
      // 为什么必须现场测：组员的 lr-move 数据里用 px_delta 符号判左右只有 53.3% 准确率，
      // 且静息基线已经偏到 +0.53~+0.97；符号/基线随站位与安装角度变化，没人能替玩家定。
      //
      // v2 增加「上游跳/蹲质量探针 + 有界自适应阈值」：
      //   DEV113P 那批跳/蹲数据（1.92Hz）里，上游判据在静息时的 FPR 高达 85~91%
      //   （等长 5 帧窗混淆矩阵，见 .deepworks/tmp/gtd/_work/），照抄这类判据会让玩家
      //   站着不动也在跳/蹲。所以教程第 1~3 步顺便统计上游误报，超限就直接停用雷达跳/蹲。
      calib: {
        autoOpen: true,        // 选了雷达且没有可用校准时，自动弹出向导（每页一次）
        baseFrames: 8,         // 第 1 步：连续多少帧算「站好了」
        baseStdMax: 0.18,      // 第 1 步：这 8 帧 x 的标准差上限
        moveDelta: 0.35,       // 第 2/3 步：相对基线的位移阈值（贴着进道阈值 0.36）
        holdFrames: 3,         // 第 2/3 步：位移要连续保持几帧（@7.75Hz ≈ 390ms）
        minSpread: 0.5,        // 左右位移差下限（默认参数下数学下界 0.70，仅作安全网）
        stepTimeoutMs: 25000,  // 单步超时（超时自动重试）

        // ---- v2 质量探针（第 1~3 步静站/走位期间统计上游跳/蹲误报）----
        probeFrames: 24,       // 探针参考帧数（不足 8 帧不下结论，避免掉线误判）
        probeSpuriousMax: 2,   // 误报「事件」数上限（左右各算：静站时上游跳/蹲各 <= 2 次）
        probeRateMax: 0.06,    // 或误报帧占比上限 6%
        // ---- v2 有界自适应阈值 ----
        weakPeak: 0.40,        // 玩家自身峰值低于它 => 信号太弱，判不可用（不硬凑阈值）
        attemptLevel: 0.30,    // 原始值冲破它 = "玩家确实试了一下"（给"太弱"提示用，只认上升沿）
        learnMargin: 0.6,      // 学到的阈值 = 峰值 × margin（只放松）
        thFloor: 0.30,         // 自适应下限（再低就是在噪声里触发）
        thCeil: 0.50,          // 自适应上限（= 上面 jump/crouchThreshold 的占位值）
        disableTh: 2           // 判不可信时写入的阈值（字段 0~1，>=1 不可达 => 彻底停用该动作）
      }
    },

    /* ---------------- 音频 ----------------
     * 所有声音都由 js/Audio.js 用 WebAudio 现场合成，工程里不放任何音频文件
     * —— 双击 index.html 直接跑（file://）时不会因为 .mp3 加载失败而报错或静音。
     * 注意：浏览器要求 AudioContext 必须由用户手势解锁，所以真正出声是
     * 从玩家第一次点击/按键开始（见 Audio.unlock）。 */
    AUDIO: {
      enabled: true,          // 关掉即彻底不初始化音频（排查性能问题时用）
      master: 0.55,           // 总音量
      sfx: 0.85,              // 音效音量（乘在总音量上）
      bgm: 0.28,              // 背景音乐音量：刻意压低，不能盖住音效
      bgmBpm: 116,            // 背景音乐速度
      duckRatio: 0.32,        // 处决演出期间背景音乐压到这个比例
      coinPitchStep: 0.6,     // 每多吃一个金币，音高上升的半音数（连吃会"叮叮叮"往上走）
      coinPitchMax: 9,        // 音高上升上限（半音）：避免连吃 40 个后尖得刺耳
      chainWindow: 700        // 连吃判定的最大间隔（毫秒），超过就重新从低音开始
    },

    /* ---------------- 粒子特效 ----------------
     * 全部由 js/Particles.js 用两个 THREE.Points 池现场算，不占额外 draw call 预算
     * （整局固定 +2 个 call，跟粒子数量无关）。
     * 池满就丢新粒子，所以容量给太小只会"少喷几个"，不会卡。 */
    PARTICLES: {
      enabled: true,          // 关掉即不创建粒子系统（排查性能问题时用）
      dustCap: 260,           // 尘土池容量（正常混合：扬尘、碎屑、尾焰的土）
      sparkCap: 200,          // 星火池容量（加色混合：金币闪光、回血、斧头星火）
      pixelScale: 1.0,        // 整体粒径缩放：嫌粒子太大/太小只改这一个数
      runDustEvery: 0.085     // 跑动扬尘的发射间隔（秒），调大就是跑起来更干净
    },

    STORAGE_KEY: 'bear-run:best-score',
    STORAGE_KEY_INPUT: 'bear-run:input-source',
    STORAGE_KEY_CHAR: 'bear-run:character',
    STORAGE_KEY_AUDIO: 'bear-run:audio',
    STORAGE_KEY_RECORDS: 'bear-run:records:v1',
    // 雷达地址记忆：由 radar-connect.html（联调台）写入，main.js 在没给 ?radar= 时读它，
    // 这样组员在联调台连过一次以后，主菜单点「雷达」直接就是那台机器。
    STORAGE_KEY_RADAR_URL: 'bear-run:radar:url',
    // 双雷达：第二路（上下）地址 + 是否启用。由联调台写入，?radar2= 也会写。
    STORAGE_KEY_RADAR_URL_UD: 'bear-run:radar:url2',
    STORAGE_KEY_RADAR_DUAL: 'bear-run:radar:dual'
  };

})(window);
