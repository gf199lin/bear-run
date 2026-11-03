# -*- coding: utf-8 -*-
"""任务 2（连击与险躲）自检：CDP 驱动无头 Chrome，断言数值与判定，不靠肉眼。

要证明的五件事：
  1. 连击 -> 倍率的换算表正确（2 连 x1.25 起，7 连封顶 x2.50）。
  2. 一串蜂蜜是"逐颗"按当时倍率结算的（20+25+30=75，不是 60 也不是 90）。
  3. 险躲的记账本体（ObstacleManager.checkCollisions）：
     擦过去算一次且只算一次；撞上的判定口径一点没变（该撞的还是撞）。
  4. 险躲加分走连击倍率、弹提示，并且**不会被误判成撞上**（hp 不掉）。
  5. 实跑一局：吃真蜂蜜 -> 连击涨 -> 徽标出现；超时/撞车 -> 连击断 -> 徽标消失。

用法： python cdp_combo.py [file:///.../index.html]
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = r"<TEMP>\cdp-profile-combo"
PORT = 9356
_GAME = Path(HERE).joinpath('..', '..', 'radar-runner', 'index.html').resolve()
URL = sys.argv[1] if len(sys.argv) > 1 else _GAME.as_uri()

# ---------------------------------------------------------------- 页面内脚本

COLL_HELPER = """// 判定必须用临时数组：真件数组（__omt.items）要原样保留。否则预填充进真场景的
// 障碍/金币会被从台账里抹掉、网格却永远留在场景里（看起来像产品泄漏）
window.__scan = function (om, it, player) {
  var real = om.items;
  om.items = [it];
  try { return om.checkCollisions(player); }
  finally { om.items = real; }
};
window.__coll = function (spec, player) {
  var om = window.__omt;
  var it = {
    kind: spec.kind, blockKind: spec.kind, lane: 0,
    x: spec.x, z: spec.z, prevZ: spec.prevZ,
    halfDepth: spec.hd, mesh: null
  };
  if (spec.solid) it.solid = true;
  if (spec.ch !== undefined) it.clearHeight = spec.ch;
  if (spec.gb !== undefined) it.gapBottom = spec.gb;
  var r = window.__scan(om, it, player);
  return [r.hit ? 1 : 0, r.nearMiss, r.nearMissBest, it.resolved ? 1 : 0,
          it.nearMissed ? 1 : 0].join('/');
};
window.__collTwice = function (spec, player) {
  var om = window.__omt;
  var it = { kind: spec.kind, blockKind: spec.kind, lane: 0, x: spec.x, z: spec.z,
             prevZ: spec.prevZ, halfDepth: spec.hd, mesh: null };
  if (spec.solid) it.solid = true;
  if (spec.ch !== undefined) it.clearHeight = spec.ch;
  if (spec.gb !== undefined) it.gapBottom = spec.gb;
  var r1 = window.__scan(om, it, player);
  var a = [r1.hit ? 1 : 0, r1.nearMiss, r1.nearMissBest, it.resolved ? 1 : 0,
           it.nearMissed ? 1 : 0].join('/');
  // 第二次刻意保留 prevZ，模拟"同一件障碍在下一帧又被扫到"
  it.prevZ = spec.prevZ; it.z = spec.z;
  var r2 = window.__scan(om, it, player);
  return a + ' | ' + [r2.hit ? 1 : 0, r2.nearMiss, it.nearMissed ? 1 : 0].join('/');
}; 'ok'"""

COMBO_TABLE = """(function () {
  var g = RRGame, out = [];
  for (var n = 0; n <= 9; n++) {
    g.combo = n;
    out.push(n + ':' + g.comboLevel + ':' + g.comboMul.toFixed(2));
  }
  g.combo = 0; g.comboTimer = 0; g.hud.hideCombo();
  return out.join(' ');
})()"""

CFG_DUMP = """JSON.stringify({
  combo: RR.CFG.COMBO, nm: RR.CFG.NEAR_MISS,
  perCoin: RR.CFG.SCORE.perCoin, boostMul: RR.CFG.SCORE.boostMul,
  laneTol: RR.CFG.LANE_TOLERANCE, lanes: RR.CFG.LANES
})"""

# 注入一帧"吃了一串蜂蜜"
INJECT_COINS = """(function (n, boost) {
  var g = RRGame, om = g.obstacles, old = om.checkCollisions;
  g.score = 0; g.coins = 0; g.combo = 0; g.comboTimer = 0; g.nearMiss = 0;
  g.boostTimer = boost ? 1 : 0;
  om.checkCollisions = function () {
    return { hit: false, obstacle: null, coins: n, heal: 0, boost: false,
             nearMiss: 0, nearMissBest: 0 };
  };
  try { g._resolveCollisions(); } finally { om.checkCollisions = old; }
  g.boostTimer = 0;
  return [g.score, g.coins, g.combo, g.comboMul.toFixed(3), g.comboTimer.toFixed(2)].join('/');
})(%d, %s)"""

# 注入一帧"擦过去 n 次"，带一个余量
INJECT_NM = """(function (n, left, combo0) {
  var g = RRGame, om = g.obstacles, old = om.checkCollisions;
  g.score = 0; g.coins = 0; g.combo = combo0; g.comboTimer = 1; g.nearMiss = 0;
  g.boostTimer = 0;
  om.checkCollisions = function () {
    return { hit: false, obstacle: null, coins: 0, heal: 0, boost: false,
             nearMiss: n, nearMissBest: left };
  };
  try { g._resolveCollisions(); } finally { om.checkCollisions = old; }
  var toast = document.getElementById('nearmiss-toast');
  var close = document.getElementById('nearmiss-close');
  return [g.score, g.combo, g.nearMiss, g.comboMul.toFixed(3),
          toast.classList.contains('hidden') ? 'hidden' : 'shown',
          close.classList.contains('hidden') ? '-' : close.textContent].join('/');
})(%d, %s, %d)"""

# 注入一帧"撞上"：看 hp 掉、连击断
INJECT_HIT = """(function () {
  var g = RRGame, om = g.obstacles, old = om.checkCollisions;
  g.hp = 5; g.combo = 5; g.comboTimer = 2;
  g.hud.setCombo(5, 4, 1);
  var before = document.getElementById('combo-badge').classList.contains('hidden');
  om.checkCollisions = function () {
    return { hit: true, obstacle: null, coins: 0, heal: 0, boost: false,
             nearMiss: 0, nearMissBest: 0 };
  };
  g.player.invulnTimer = 0;
  try { g._resolveCollisions(); } finally { om.checkCollisions = old; }
  var after = document.getElementById('combo-badge').classList.contains('hidden');
  return [g.hp, g.combo, g.comboTimer, before ? 'hidden' : 'shown',
          after ? 'hidden' : 'shown'].join('/');
})()"""

HOOK_ADD = """(function () {
  var S = window.THREE.Scene.prototype, old = S.add;
  window.__adds = [];
  S.add = function (o) {
    try {
      var st = (new Error()).stack.split(String.fromCharCode(10));
      var frames = [];
      for (var i = 2; i < st.length && frames.length < 3; i++) {
        var f = st[i].trim();
        if (f.indexOf('at ') === 0) f = f.slice(3);
        var p = f.lastIndexOf('/');
        if (p >= 0) f = f.slice(p + 1);
        if (f) frames.push(f);
      }
      window.__adds.push(((o && (o.name || o.type)) || '?') + ' @ ' + (frames.join(' < ') || 'anon'));
    } catch (e) { window.__adds.push('hookerr'); }
    return old.apply(this, arguments);
  };
  var OM = window.RR.ObstacleManager.prototype;
  window.__omStats = { add: {}, rel: {}, ev: {} };
  if (OM._addItem) {
    var a = OM._addItem;
    OM._addItem = function (kind) {
      var k = kind || '?';
      window.__omStats.add[k] = (window.__omStats.add[k] || 0) + 1;
      return a.apply(this, arguments);
    };
  }
  if (OM._releaseItem) {
    var r = OM._releaseItem;
    OM._releaseItem = function (it) {
      var k = (it && it.kind) || '?';
      window.__omStats.rel[k] = (window.__omStats.rel[k] || 0) + 1;
      return r.apply(this, arguments);
    };
  }
  if (OM.reset) {
    var rs = OM.reset;
    OM.reset = function () {
      window.__omStats.ev.reset = (window.__omStats.ev.reset || 0) + 1;
      return rs.apply(this, arguments);
    };
  }
  return 'hooked';
})()"""

RESIDUE_CREATORS = """(function () {
  var agg = {};
  (window.__adds || []).forEach(function (s) {
    var k = s.split(' @ ')[1] || 'anon';
    var t = s.split(' @ ')[0];
    var key = k + ' | ' + t;
    agg[key] = (agg[key] || 0) + 1;
  });
  var rows = Object.keys(agg).map(function (k) { return [k, agg[k]]; });
  rows.sort(function (a, b) { return b[1] - a[1]; });
  return JSON.stringify({ total: (window.__adds || []).length,
                          top: rows.slice(0, 12) });
})()"""

NOSPAWN = """window.__om_orig = RRGame.obstacles.checkCollisions;
window.__om_quiet = function () {
  return { hit: false, obstacle: null, coins: 0, heal: 0, boost: false,
           nearMiss: 0, nearMissBest: 0 };
};
RRGame.obstacles.checkCollisions = window.__om_quiet;
'ok'"""

UNQUIET = """RRGame.obstacles.checkCollisions = window.__om_orig;
RRGame.player.invulnTimer = 60;
'ok'"""

# 在玩家所在车道前方摆一串真蜂蜜，然后交给真实主循环去吃
SPAWN_COINS = """(function () {
  var g = RRGame, om = g.obstacles;
  g.player.invulnTimer = 60;        // 这一节只验吃蜂蜜，别让障碍中途撞断连击
  g.player.x = 0;
  var n = 0;
  for (var k = 0; k < 4; k++) {
    om._addItem('coin', 1, -(7 + k * 1.05), 0);
    n++;
  }
  return n + '/' + g.coins + '/' + Math.floor(g.score) + '/' + Math.round(g.distance) + '/' + g.combo;
})()"""


def main():
    print('URL :', URL)
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--remote-debugging-port=%d' % PORT,
         '--window-size=1280,720', '--hide-scrollbars', '--no-first-run',
         '--no-default-browser-check', '--disable-extensions',
         '--enable-unsafe-swiftshader', '--remote-allow-origins=*',
         '--user-data-dir=' + PROFILE, URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fails = []
    try:
        ws_url = None
        t0 = time.time()
        while time.time() - t0 < 25 and not ws_url:
            try:
                with urllib.request.urlopen(
                        'http://127.0.0.1:%d/json/list' % PORT, timeout=2) as r:
                    for it in json.loads(r.read().decode('utf-8')):
                        if it.get('type') == 'page' and it.get('webSocketDebuggerUrl'):
                            ws_url = it['webSocketDebuggerUrl']
                            break
            except Exception:
                pass
            if not ws_url:
                time.sleep(0.4)
        if not ws_url:
            print('FAIL: devtools not reachable')
            return

        ws = websocket.create_connection(ws_url, timeout=60)
        counter = [0]
        events = []
        state = {'ws': ws}

        def find_ws():
            try:
                with urllib.request.urlopen(
                        'http://127.0.0.1:%d/json/list' % PORT, timeout=2) as r:
                    for it in json.loads(r.read().decode('utf-8')):
                        if it.get('type') == 'page' and it.get('webSocketDebuggerUrl'):
                            return it['webSocketDebuggerUrl']
            except Exception:
                pass
            return None

        def cmd(method, params=None):
            last = None
            for _ in range(5):
                counter[0] += 1
                mid = counter[0]
                try:
                    state['ws'].send(json.dumps(
                        {'id': mid, 'method': method, 'params': params or {}}))
                    while True:
                        msg = json.loads(state['ws'].recv())
                        if msg.get('id') == mid:
                            return msg
                        if 'method' in msg:
                            events.append(msg)
                except BaseException as e:
                    last = e
                    print('     [ws 重连] %s: %s' % (type(e).__name__, str(e)[:70]))
                    time.sleep(1.2)
                    u = find_ws() or (time.sleep(1.0) or find_ws())
                    if not u:
                        continue
                    try:
                        state['ws'].close()
                    except BaseException:
                        pass
                    try:
                        state['ws'] = websocket.create_connection(u, timeout=60)
                    except BaseException as e2:
                        print('     重连失败: %s' % str(e2)[:70])
                        continue
                    state['reconnects'] = state.get('reconnects', 0) + 1
            raise RuntimeError('CDP 连续 5 次失败: %s (%s)' % (method, type(last).__name__))

        def ev(expr, gesture=False):
            r = cmd('Runtime.evaluate', {'expression': expr, 'returnByValue': True,
                                         'userGesture': bool(gesture)})
            res = r.get('result', {})
            if res.get('exceptionDetails'):
                return 'JSERROR: ' + json.dumps(res['exceptionDetails'])[:300]
            return (res.get('result', {}) or {}).get('value')

        def check(label, ok, detail=''):
            print(('  PASS ' if ok else '  FAIL ') + label + ('  ' + str(detail) if detail else ''))
            if not ok:
                fails.append(label)

        cmd('Page.enable')
        cmd('Runtime.enable')
        time.sleep(3.5)

        print('\n[1] 冷启动与配置')
        h = str(ev("['three='+(window.THREE?THREE.REVISION:'MISSING'),"
                   "'errors='+((window.__bootErrors||[]).length),"
                   "'errtext='+((window.__bootErrors||[]).join(' ~ ')||'none')].join('|')"))
        print('    ', h)
        check('启动零报错', 'errors=0' in h, h.split('errtext=')[1][:60])
        cfg = json.loads(str(ev(CFG_DUMP)))
        print('     CFG.COMBO   =', cfg['combo'])
        print('     CFG.NEAR_MISS =', cfg['nm'])
        check('COMBO 表存在且数值正确',
              cfg['combo'] == {'window': 2.4, 'maxStep': 6, 'step': 0.25,
                               'coinAdd': 1, 'nearMissAdd': 1}, cfg['combo'])
        check('NEAR_MISS 表存在且数值正确',
              cfg['nm'] == {'margin': 0.34, 'xMargin': 0.52, 'score': 30, 'close': 0.12},
              cfg['nm'])
        dom = str(ev("(function(){var ids=['combo-badge','combo-mult','combo-count',"
                     "'combo-bar-fill','nearmiss-toast','nearmiss-close'];var miss=[];"
                     "for(var i=0;i<ids.length;i++)if(!document.getElementById(ids[i]))miss.push(ids[i]);"
                     "return miss.length?miss.join(','):'all-present';})()"))
        check('HUD 新节点齐了（徽标 + 险躲提示）', dom == 'all-present', dom)
        hidden = str(ev("document.getElementById('combo-badge').classList.contains('hidden')"
                        "+'/'+document.getElementById('nearmiss-toast').classList.contains('hidden')"))
        check('主菜单里两个新提示都是隐藏的', hidden == 'true/true', hidden)
        # 记下"纯菜单"的场景快照，任务尾声用它做残留物差分（比只数个数有信息量）
        ev("window.__menuKids = RRGame.scene.children.map(function (c) {"
           "return (c.name || c.type) + '#' + c.id; }).join(','); 'ok'")
        print('     纯菜单 scene.children =', ev("window.__menuKids.split(',').length"))
        print('     创建点钩子:', ev(HOOK_ADD))

        print('\n[2] 连击 -> 倍率换算表')
        tbl = str(ev(COMBO_TABLE))
        print('     连击:档位:倍率 ->', tbl)
        d = {}
        for tok in tbl.split(' '):
            n, lv, mul = tok.split(':')
            d[int(n)] = (int(lv), float(mul))
        check('0 连 = x1.00（没连不扣分）', d[0] == (0, 1.0), d[0])
        check('1 连 = x1.00（第一颗不涨价）', d[1] == (0, 1.0), d[1])
        check('2 连 = x1.25（从这里开始算连上）', d[2] == (1, 1.25), d[2])
        check('3 连 = x1.50', d[3] == (2, 1.5), d[3])
        check('7 连 = x2.50（满档）', d[7] == (6, 2.5), d[7])
        check('超过 maxStep 不再涨（8 连 / 9 连仍是 x2.50）',
              d[8] == (6, 2.5) and d[9] == (6, 2.5), (d[8], d[9]))
        check('全程单调不减', all(d[i][1] <= d[i + 1][1] for i in range(9)))

        print('\n[3] 一串蜂蜜逐颗结算（注入一帧 coins=3）')
        r3 = str(ev(INJECT_COINS % (3, 'false')))
        print('    ', r3, '（期望 score=20+25+30=75, coins=3, combo=3, mul=1.5）')
        sc, co, cb, ml, ct = r3.split('/')
        check('蜂蜜计数正确', co == '3', co)
        check('得分是逐颗按当时倍率算的（75，不是整串同倍率）',
              abs(float(sc) - 75.0) < 1e-6, sc)
        check('连击涨到 3', cb == '3', cb)
        check('倍率 = x1.500', abs(float(ml) - 1.5) < 1e-3, ml)
        check('连击窗口被刷新到满（2.4s）', abs(float(ct) - 2.4) < 1e-6, ct)

        r3b = str(ev(INJECT_COINS % (3, 'true')))
        scb = float(r3b.split('/')[0])
        check('加速带期间按 x2 结算（75 x 2 = 150）', abs(scb - 150.0) < 1e-6, r3b)

        r3c = str(ev(INJECT_COINS % (1, 'false')))
        check('只吃一颗 = 20 分（x1.00 起步）',
              abs(float(r3c.split('/')[0]) - 20.0) < 1e-6, r3c)

        print('\n[4] 险躲加分走连击倍率')
        r4 = str(ev(INJECT_NM % (1, '0.30', 0)))
        print('    ', r4, '（期望 score=30, combo=1, mul=1.00, 提示 shown）')
        sc, cb, nm, ml, sh, cl = r4.split('/')
        check('一次险躲 = 30 分', abs(float(sc) - 30.0) < 1e-6, sc)
        check('险躲也涨连击', cb == '1', cb)
        check('nearMiss 计数 +1', nm == '1', nm)
        check('弹出"险躲"提示', sh == 'shown', sh)
        check('余量 0.30 > close(0.12)：不写"毫厘之间"', cl == '-', cl)

        r4b = str(ev(INJECT_NM % (1, '0.05', 0)))
        sh, cl = r4b.split('/')[4], r4b.split('/')[5]
        check('余量 0.05 <= close：文案升级为"毫厘之间"', cl == '毫厘之间', r4b)

        r4c = str(ev(INJECT_NM % (1, '0.30', 4)))
        sc, cb, nm, ml = r4c.split('/')[0:4]
        check('已有 4 连时一次险躲 = 30 x 2.00 = 60', abs(float(sc) - 60.0) < 1e-6, r4c)
        check('倍率按"加成后的连击数"算（4->5 连 = x2.00）',
              abs(float(ml) - 2.0) < 1e-3 and cb == '5', r4c)

        r4d = str(ev(INJECT_NM % (2, '0.20', 0)))
        sc, cb, nm = r4d.split('/')[0:3]
        check('一帧擦过两次 = 30x2x1.25 = 75', abs(float(sc) - 75.0) < 1e-6, r4d)
        check('连击 +2', cb == '2' and nm == '2', r4d)

        print('\n[5] 险躲判定本体（真 checkCollisions，注入假 player）')
        print('    ', str(ev(COLL_HELPER)))
        ev("window.__omt = new RR.ObstacleManager(RRGame.scene); 'ok'")
        lane = float(cfg['lanes'][1])
        cases = [
            # label, spec, player, 期望 hit/nearMiss/best
            ('实心堆：还在道上 -> 撞', dict(kind='block', x=0.50, z=0.05, prevZ=-0.10,
                                            hd=0.72, solid=True), dict(bottom=0.0), (1, 0)),
            ('实心堆：擦身让开 0.30m -> 险躲', dict(kind='block', x=lane + 1.22, z=0.05,
                                                   prevZ=-0.10, hd=0.72, solid=True),
             dict(bottom=0.0), (0, 1, 0.300)),
            ('实心堆：换道换得远远的 -> 无事', dict(kind='block', x=lane + 2.15, z=0.05,
                                                   prevZ=-0.10, hd=0.72, solid=True),
             dict(bottom=0.0), (0, 0)),
            ('实心堆：dx 正好等于容差 -> 仍算撞（口径不变）',
             dict(kind='block', x=lane + 0.92, z=0.05, prevZ=-0.10, hd=0.72, solid=True),
             dict(bottom=0.0), (1, 0)),
            ('实心堆：dx 正好 = 容差+横余量 -> 险躲 0.52m',
             dict(kind='block', x=lane + 1.44, z=0.05, prevZ=-0.10, hd=0.72, solid=True),
             dict(bottom=0.0), (0, 1, 0.520)),
            ('实心堆：再远一点 1.50m -> 不算险躲',
             dict(kind='block', x=lane + 1.50, z=0.05, prevZ=-0.10, hd=0.72, solid=True),
             dict(bottom=0.0), (0, 0)),
            ('矮栏：跳得老高 1.45m -> 干净通过', dict(kind='low', x=lane, z=0.05, prevZ=-0.10,
                                                     hd=0.36, ch=0.92), dict(bottom=1.45), (0, 0)),
            ('矮栏：贴栏顶擦过 0.18m -> 险躲', dict(kind='low', x=lane, z=0.05, prevZ=-0.10,
                                                    hd=0.36, ch=0.92), dict(bottom=1.10),
             (0, 1, 0.180)),
            ('矮栏：余量正好 = 0.34 -> 险躲（边界含）',
             dict(kind='low', x=lane, z=0.05, prevZ=-0.10, hd=0.36, ch=0.92),
             dict(bottom=1.26), (0, 1, 0.340)),
            ('矮栏：跳晚了（脚底 0.50m）-> 撞', dict(kind='low', x=lane, z=0.05, prevZ=-0.10,
                                                     hd=0.36, ch=0.92), dict(bottom=0.50), (1, 0)),
            ('悬吊物：蹲下钻过 -> 不算险躲（否则滑铲白送分）',
             dict(kind='high', x=lane, z=0.05, prevZ=-0.10, hd=0.4, gb=1.18),
             dict(bottom=0.0, top=0.72), (0, 0)),
            ('悬吊物：站着没蹲 -> 撞', dict(kind='high', x=lane, z=0.05, prevZ=-0.10,
                                            hd=0.4, gb=1.18), dict(bottom=0.0, top=1.72), (1, 0)),
            ('还没走到平面（z 仍为负）-> 不记账',
             dict(kind='block', x=lane + 1.22, z=-0.30, prevZ=-0.50, hd=0.72, solid=True),
             dict(bottom=0.0), (0, 0)),
            ('已经过去了（prevZ > 0）-> 不补记',
             dict(kind='block', x=lane + 1.22, z=0.30, prevZ=0.10, hd=0.72, solid=True),
             dict(bottom=0.0), (0, 0)),
            ('矮栏擦身但不在自己道上 -> 不算险躲',
             dict(kind='low', x=lane + 1.30, z=0.05, prevZ=-0.10, hd=0.36, ch=0.92),
             dict(bottom=1.10), (0, 0)),
        ]
        for label, spec, pl, want in cases:
            pl = dict(pl, x=lane, top=pl.get('top', 1.72), isInvulnerable=False)
            got = str(ev('window.__coll(%s, %s)' % (json.dumps(spec), json.dumps(pl))))
            parts = [x for x in got.split('/')]
            hit, nm = int(parts[0]), int(parts[1])
            best = float(parts[2])
            ok = (hit == want[0] and nm == want[1]
                  and (len(want) < 3 or abs(best - want[2]) < 0.002))
            check(label, ok, 'got %s / 期望 %s' % (got, want))

        print('\n[6] 险躲只记账一次（同一件障碍反复扫到）')
        spec = dict(kind='block', x=lane + 1.22, z=0.05, prevZ=-0.10, hd=0.72, solid=True)
        pl = dict(x=lane, bottom=0.0, top=1.72, isInvulnerable=False)
        tw = str(ev('window.__collTwice(%s, %s)' % (json.dumps(spec), json.dumps(pl))))
        print('    ', tw)
        a, b = [x.strip() for x in tw.split('|')]
        check('第一帧记一次险躲', a.split('/')[1] == '1', a)
        check('第二帧不再重复记（nearMissed 生效）', b.split('/')[1] == '0', b)

        # 第 5/6 节那个独立 ObstacleManager 往真场景里预填充了一堆障碍/金币，
        # 它不归 RRGame 管，必须自己收，否则会一直挂在前景污染第 11 节的回归
        print('    ', '清理 __omt 预填充:',
              str(ev("window.__omt.reset(false); window.__omt.items.length")))

        print('\n[7] 撞上要断连（注入一帧 hit）')
        print('    ', str(ev(NOSPAWN)), '（装无事件桩：这几节只让被测逻辑改状态）')
        r7 = str(ev(INJECT_HIT))
        print('    ', r7, '（期望 hp 5->4, combo 5->0, 徽标 shown->hidden）')
        hp, cb, ct, before, after = r7.split('/')
        check('掉了 1 点血（碰撞口径没被改坏）', hp == '4', hp)
        check('连击归零', cb == '0' and ct == '0', r7)
        check('撞前徽标可见 -> 撞后隐藏', before == 'shown' and after == 'hidden', r7)

        print('\n[8] 实跑一局：真吃蜂蜜 -> 连击涨 -> 徽标出现')
        ev("document.getElementById('btn-start').click();'ok'", gesture=True)
        time.sleep(1.2)
        print('    ', str(ev(UNQUIET)), '（恢复真实碰撞）')
        sp = str(ev(SPAWN_COINS))
        print('     摆下 %s （摆串时的 coins/score/distance/combo）' % sp)
        time.sleep(2.2)
        run = str(ev("(function(){var g=RRGame;var b=document.getElementById('combo-badge');"
                     "return [g.state,g.coins,Math.floor(g.score),Math.round(g.distance),g.combo,"
                     "g.comboLevel,g.comboMul.toFixed(2),g.nearMiss,g.maxCombo,"
                     "b.classList.contains('hidden')?'hidden':'shown',"
                     "document.getElementById('combo-mult').textContent,"
                     "document.getElementById('combo-count').textContent].join('/');})()"))
        print('     跑完:', run)
        st, coins, score, dist, combo, lvl, mul, nmc, mx, badge, bmul, bcnt = run.split('/')
        check('采样时仍在跑', st == 'playing', st)
        check('真吃到了一串蜂蜜（>=4 颗）', int(coins) >= 4, coins)
        check('连击真的涨起来了（>=4）', int(combo) >= 4, combo)
        check('连击徽标已显示', badge == 'shown', badge)
        check('徽标倍率 = 1+档位x0.25', abs(float(bmul.lstrip('x')) - float(mul)) < 1e-6,
              '%s vs %s' % (bmul, mul))
        check('徽标连击数与状态一致', int(bcnt) == int(combo), bcnt + ' vs ' + combo)
        check('倍率与档位换算一致', abs(float(mul) - (1 + int(lvl) * 0.25)) < 1e-6, run)
        check('得分吃到蜂蜜的收益（>=4 颗 = 110 分 + 里程分）',
              float(score) >= 110 + int(dist) - 1,
              'score=%s dist=%s' % (score, dist))
        check('maxCombo 被记住（结算要用）', int(mx) >= 4, mx)

        print('\n[9] 连击超时断连（实跑）')
        ev(NOSPAWN)
        ev("RRGame.combo=3;RRGame.comboTimer=0.12;'ok'")
        time.sleep(0.9)
        to = str(ev("(function(){var g=RRGame;return [g.combo,g.comboTimer.toFixed(2),"
                    "document.getElementById('combo-badge').classList.contains('hidden')"
                    "?'hidden':'shown'].join('/');})()"))
        print('    ', to)
        cbc, cbt, bdg = to.split('/')
        check('窗口走完连击归零', cbc == '0' and float(cbt) == 0.0, to)
        check('徽标跟着隐藏', bdg == 'hidden', bdg)

        print('\n[10] 险躲不会被误判成撞上（实跑注入 nearMiss）')
        nh = str(ev("(function(){var g=RRGame,om=g.obstacles,old=om.checkCollisions;"
                    "g.hp=5;g.combo=0;g.comboTimer=0;g.player.invulnTimer=60;"
                    "var s0=g.score,hp0=g.hp,nm0=g.nearMiss;"
                    "om.checkCollisions=function(){return {hit:false,obstacle:null,coins:0,"
                    "heal:0,boost:false,nearMiss:1,nearMissBest:0.08};};"
                    "try{g._resolveCollisions();}finally{om.checkCollisions=old;}"
                    "return [g.hp-hp0,(g.score-s0).toFixed(1),g.nearMiss-nm0,"
                    "document.getElementById('nearmiss-close').textContent,"
                    "document.getElementById('nearmiss-toast').classList.contains('hidden')"
                    "?'hidden':'shown'].join('/');})()"))
        print('    ', nh)
        dhp, dsc, dnm, cltxt, tsh = nh.split('/')
        check('血量一点没掉（没被误判成撞上）', dhp == '0', dhp)
        check('加了正好 30 分（连击清零后 x1.00，不虚高）',
              abs(float(dsc) - 30.0) < 1e-6, dsc)
        check('nearMiss 计数 +1', dnm == '1', dnm)
        check('提示弹出且是"毫厘之间"', tsh == 'shown' and cltxt == '毫厘之间', nh)

        print('\n[11] 回归：回主菜单后一切复位')
        ev("RRGame.toMenu();'ok'")
        time.sleep(1.2)
        m = str(ev("(function(){var g=RRGame;return [g.combo,g.maxCombo,g.nearMiss,"
                   "document.getElementById('combo-badge').classList.contains('hidden')"
                   "?'hidden':'shown',"
                   "document.getElementById('nearmiss-toast').classList.contains('hidden')"
                   "?'hidden':'shown'].join('/');})()"))
        print('    ', m)
        cb, mx2, nm2, bdg, tst = m.split('/')
        check('toMenu() 清空连击/险躲计数', cb == '0' and mx2 == '0' and nm2 == '0', m)
        check('两个新提示都收回', bdg == 'hidden' and tst == 'hidden', m)
        # 菜单场景天然漂移（云/星在动），单次采样没有意义 -> 连采多次取"地板"。
        # 且残局物件要等排空（刚 toMenu 时上一局的道具还在往外漂，draw calls 会虚高）。
        samples = []
        kids = -1
        for _ in range(24):
            v = str(ev("RRGame.obstacles.items.length + '/' + RRGame.scene.children.length"
                       " + '/' + RRGame.renderer.info.render.calls"))
            a, b, c = v.split('/')
            kids = int(b)
            if int(a) == 0:
                samples.append(int(c))
            time.sleep(0.5)
        print('     draw calls 采样:', samples, '（末次 scene.children=%d）' % kids)
        floor = min(samples) if samples else -1
        diff = str(ev("(function () {"
                      "var W = RRGame.world || {};"
                      "var now = RRGame.scene.children;"
                      "var old = (window.__menuKids || '').split(',');"
                      "var hand = ['regionDecor','props','clouds','swayers','birds','groundTiles'];"
                      "var added = [];"
                      "for (var i = 0; i < now.length; i++) {"
                      "  var c = now[i], key = (c.name || c.type) + '#' + c.id;"
                      "  if (old.indexOf(key) >= 0) continue;"
                      "  var tags = [];"
                      "  for (var h = 0; h < hand.length; h++) {"
                      "    var arr = W[hand[h]];"
                      "    if (!arr) continue;"
                      "    if (arr.indexOf && arr.indexOf(c) >= 0) tags.push(hand[h]);"
                      "    else if (arr.children && arr.children.indexOf(c) >= 0)"
                      " tags.push(hand[h] + '.children');"
                      "  }"
                      "  added.push({ k: key, t: c.type, kids: c.children.length,"
                      " vis: !!c.visible, tags: tags.join('+') || '+',"
                      " parent: c.parent ? (c.parent.name || c.parent.type) + '#' + c.parent.id : 'none',"
                      " mats: c.material ? (c.material.type + '/' + (c.material.visible ? 'v' : 'x')) : '-' });"
                      "}"
                      "return JSON.stringify({ n: now.length, added: added.length,"
                      "vis: now.filter(function (c) { return c.visible; }).length,"
                      "sample: added.slice(0, 12) });"
                      "})()"))
        print('     残留物差分:', diff)
        print('     残留物是谁造的:', ev(RESIDUE_CREATORS))
        print('     造/收统计:', ev("JSON.stringify(window.__omStats)"))
        info = json.loads(diff)
        check('回菜单后障碍/道具列表清零',
              str(ev("RRGame.obstacles.items.length")) == '0')
        # cdp_calls.py 的 A/B 已证明：当前版与旧包 f 在"冷菜单"与"打一局回菜单"
        # 两种状态下 scene.children 都恒为 143，所以这里要求同样干净（差分严格为 0）
        check('回菜单后场景没有多余对象（差分=0，纯菜单基线 143）',
              info['added'] == 0, '新增=%s 可见=%s n=%s 样本=%s'
              % (info['added'], info['vis'], info['n'], info['sample'][:3]))
        check('主菜单 draw calls 地板与纯菜单基线同一量级（A/B 地板 807~845）',
              0 < floor <= 950, 'min=%s 残留新增=%s 可见=%s'
              % (floor, info['added'], info['vis']))

        errs = []
        for msg in events:
            meth = msg.get('method')
            p = msg.get('params', {}) or {}
            if meth == 'Runtime.exceptionThrown':
                errs.append('EXCEPTION: ' + str(p.get('exceptionDetails', {}).get('text')))
            elif meth == 'Log.entryAdded':
                en = p.get('entry', {}) or {}
                if en.get('level') == 'error':
                    errs.append('LOG: ' + str(en.get('text')))
            elif meth == 'Runtime.consoleAPICalled' and p.get('type') == 'error':
                errs.append('CONSOLE: ' + str(p.get('args'))[:200])
        print('\nconsole errors:', errs if errs else 'none')
        check('控制台零报错', not errs, errs[:2])

        print('\n===== 结果：%s =====' % (
            '全部通过' if not fails else '失败 %d 项: %s' % (len(fails), fails)))
        print('（调试连接重连次数: %d）' % state.get('reconnects', 0))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == '__main__':
    main()
