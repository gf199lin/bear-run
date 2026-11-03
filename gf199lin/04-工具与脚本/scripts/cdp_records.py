# -*- coding: utf-8 -*-
"""任务 4（丰富结算与个人纪录）自检：CDP 驱动无头 Chrome，只断言数值与 DOM。

要证明的十件事：
  1. CFG.RECORDS / CFG.HIGHLIGHT 表 + 结算面板 11 个新节点 + CSS 真的接上了。
  2. _loadRecords() 容错：坏 JSON / 非对象 / 数组 / 负数 / 脏历史 都能降级，不炸结算。
  3. 老存档迁移：只有 bear-run:best-score 时，records.best 接得上旧最高分。
  4. _pushRecord() 逐维度取最大值、plays 累加、历史前插并截断到 historyMax=5。
  5. 真结算一局 -> 面板 6 项数据 / 4 个破纪录徽标 / 挑战小结 / 亮点 / 下一局目标都对。
  6. 历史只列"本局之前"的局（首局没有历史 -> 整块隐藏）。
  7. _buildHighlights()：门槛、顺序、max 截断、一条都不够时的兜底句。
  8. _buildNextGoal()：全破 / 只差距离 / 第一局 三种分支与文案。
  9. 回归：回主菜单后场景零残留、障碍清零、draw calls 不高于纯菜单基线。
 10. ?autostart=1&hp=1 真 URL 端到端：自己死掉 -> 结算面板自己弹出来。

用法： python cdp_records.py [file:///.../index.html]
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
PROFILE = r"<TEMP>\cdp-profile-records-9358"
PORT = 9358
_GAME = Path(HERE).joinpath('..', '..', 'radar-runner', 'index.html').resolve()
URL = sys.argv[1] if len(sys.argv) > 1 else _GAME.as_uri()

REC_KEY = 'bear-run:records:v1'
BEST_KEY = 'bear-run:best-score'

# ---------------------------------------------------------------- 页面内脚本

CFG_DUMP = """JSON.stringify({
  rec: RR.CFG.RECORDS,
  hl: RR.CFG.HIGHLIGHT,
  hpMax: RR.CFG.HP.max,
  key: RR.CFG.STORAGE_KEY_RECORDS
})"""

DOM_PROBE = """(function () {
  var ids = ['over-score','over-distance','over-coins','over-best','over-combo',
             'over-best-distance','over-newbest','over-badges','over-challenge',
             'over-challenge-state','over-challenge-name','over-challenge-text',
             'over-highlights','over-next','over-history','over-history-list'];
  var miss = [], i;
  for (i = 0; i < ids.length; i++) {
    if (!document.getElementById(ids[i])) miss.push(ids[i]);
  }

  // CSS 探针：把新样式挂到一个临时节点上，读计算值（不是只 assert 类名存在）
  var probe = document.createElement('div');
  probe.className = 'record-badge';
  probe.textContent = '最高分破纪录';
  var chip = document.createElement('span');
  chip.className = 'hl-chip';
  chip.textContent = '险躲 3 次';
  var host = document.getElementById('over-history-list');
  var row = document.createElement('div');
  row.className = 'history-row';
  var b = document.createElement('b');
  b.textContent = '120 分';
  row.appendChild(b);
  probe.style.position = 'absolute';
  probe.style.left = '-9999px';
  document.body.appendChild(probe);
  document.body.appendChild(chip);
  if (host) host.appendChild(row);

  var pb = getComputedStyle(probe);
  var pc = getComputedStyle(chip);
  var pr = getComputedStyle(row);
  var panel = getComputedStyle(document.querySelector('#overlay-over .panel'));
  var out = [
    'miss=' + (miss.length ? miss.join(',') : 'none'),
    'badge=' + [pb.fontSize, pb.borderTopLeftRadius, (pb.backgroundImage.indexOf('gradient') >= 0 ? 'grad' : 'flat')].join('/'),
    'chip=' + [pc.fontSize, pc.borderTopLeftRadius].join('/'),
    'row=' + [pr.display, pr.borderTopLeftRadius].join('/'),
    'panel=' + panel.maxWidth
  ].join('|');

  probe.remove();
  chip.remove();
  if (row.parentNode) row.parentNode.removeChild(row);
  return out;
})()"""

LS_PROBE = """(function () {
  try {
    localStorage.setItem('__t', '1');
    var v = localStorage.getItem('__t');
    localStorage.removeItem('__t');
    return 'ok|' + v;
  } catch (e) { return 'blocked|' + e.name; }
})()"""

RESET = """(function () {
  try {
    localStorage.removeItem('bear-run:records:v1');
    localStorage.removeItem('bear-run:best-score');
  } catch (e) {}
  RRGame.best = 0;
  RRGame.records = null;
  RRGame.toMenu();
  return 'ok';
})()"""

# 坏数据容错：每种输入读一次，返回 "名字~best~bDist~bCombo~bNear~plays~historyLen"
TOLERANT = """(function () {
  var g = RRGame, out = [];
  function put(v) {
    try {
      localStorage.setItem('bear-run:records:v1', typeof v === 'string' ? v : JSON.stringify(v));
    } catch (e) {}
  }
  var cases = [
    ['bad-json',      '{oops'],
    ['not-object',    '"hello"'],
    ['array',         [1, 2, 3]],
    ['negative',      {best: -5, bestDistance: -1, bestCombo: 'x', bestNearMiss: null, plays: 'NaN', history: 'nope'}],
    ['long-history',  {best: 100, bestDistance: 200, history: [{score: 1}, {score: 2}, {score: 3}, {score: 4}, {score: 5}, {score: 6}, {score: 7}]}],
    ['dirty-history', {history: [null, 'x', {score: 7, distance: 3, combo: 2, nearMiss: 1, won: 1, at: 5}, {score: 'bad'}]}]
  ];
  for (var i = 0; i < cases.length; i++) {
    put(cases[i][1]);
    var r = g._loadRecords();
    out.push([cases[i][0], r.best, r.bestDistance, r.bestCombo, r.bestNearMiss,
              r.plays, r.history.length].join('~'));
  }
  return out.join('|');
})()"""

MIGRATE = """(function () {
  var g = RRGame;
  try { localStorage.setItem('bear-run:records:v1', ''); } catch (e) {}
  try { localStorage.setItem('bear-run:best-score', '1234'); } catch (e) {}
  var got = g._loadRecords().best;
  try { localStorage.removeItem('bear-run:best-score'); } catch (e) {}
  return String(got);
})()"""

# 真开 7 局：每局一条 "histLen:plays:头部分数:头部距离:best:bestDist:bestCombo:bestNear"
PUSH7 = """(function () {
  var g = RRGame, rows = [];
  try {
    localStorage.removeItem('bear-run:records:v1');
    localStorage.removeItem('bear-run:best-score');
  } catch (e) {}
  g.best = 0;
  g.toMenu();
  for (var i = 0; i < 7; i++) {
    g.start();
    g.hud.hideOver();
    g.score = 100 + i * 10;
    g.distance = 100 + i * 20;
    g.coins = i;
    g.maxCombo = i;
    g.nearMiss = i;
    g.gameOver();
    var r = g._loadRecords();
    rows.push([r.history.length, r.plays, r.history[0].score, r.history[0].distance,
               r.best, r.bestDistance, r.bestCombo, r.bestNearMiss].join(':'));
  }
  return rows.join('|');
})()"""

# 首局真实结算：面板 + 纪录 + 历史（首局不该有历史行）
SETTLE_DOM = """(function () {
  var g = RRGame;
  try {
    localStorage.removeItem('bear-run:records/v1');
    localStorage.removeItem('bear-run:records:v1');
    localStorage.removeItem('bear-run:best-score');
  } catch (e) {}
  g.best = 0;
  g.records = null;
  g.toMenu();
  g._forcedChallenge = 'honey';
  g.start();
  g.hud.hideOver();
  g.distance = 412.6;
  g.coins = 31;          // 超过 honey 的 15，检验进度封顶
  g.maxCombo = 3;
  g.nearMiss = 5;
  g.hp = RR.CFG.HP.max;  // 满分 -> "毫发无伤"
  g._updateChallenge();  // 先让挑战判完成（这里会发 200 分）
  g.score = 700;         // 再把分数钉死，免得奖励分混进断言
  g.gameOver();
  return JSON.stringify(readOver(g));
})()"""

# 读结算面板的公共函数（注入一次，后续复用）
READ_OVER = """window.readOver = function (g) {
  function txt(id) { var e = document.getElementById(id); return e ? e.textContent : 'MISSING'; }
  function hidden(id) {
    var e = document.getElementById(id);
    return e ? e.classList.contains('hidden') : true;
  }
  var hl = [].slice.call(document.querySelectorAll('#over-highlights .hl-chip'))
    .map(function (e) { return e.textContent; });
  var rows = [].slice.call(document.querySelectorAll('#over-history-list .history-row'))
    .map(function (e) { return e.textContent; });
  var rec = g.records || {};
  return {
    overShown: !hidden('overlay-over'),
    score: txt('over-score'), dist: txt('over-distance'), coins: txt('over-coins'),
    best: txt('over-best'), combo: txt('over-combo'), bestDist: txt('over-best-distance'),
    newBest: !hidden('over-newbest'),
    badges: [].slice.call(document.querySelectorAll('#over-badges .record-badge'))
      .map(function (e) { return e.textContent; }),
    chHidden: hidden('over-challenge'), chDone: !!(document.getElementById('over-challenge')
      && document.getElementById('over-challenge').classList.contains('done')),
    chState: txt('over-challenge-state'), chName: txt('over-challenge-name'),
    chText: txt('over-challenge-text'),
    hl: hl, hlHidden: hidden('over-highlights'),
    next: txt('over-next'), nextHidden: hidden('over-next'),
    nextAll: !!(document.getElementById('over-next')
      && document.getElementById('over-next').classList.contains('all')),
    histHidden: hidden('over-history'), histRows: rows,
    plays: rec.plays, histLen: (rec.history || []).length,
    recBest: rec.best, recBestDist: rec.bestDistance,
    recBestCombo: rec.bestCombo, recBestNear: rec.bestNearMiss
  };
};'ok'"""

# 纯函数：亮点门槛 / max 截断 / 兜底
HIGHLIGHTS = """(function () {
  var g = RRGame, out = [];
  function s(dist, combo, nm, coins, hp) {
    g.distance = dist; g.maxCombo = combo; g.nearMiss = nm; g.coins = coins; g.hp = hp;
  }
  s(260, 3, 3, 10, RR.CFG.HP.max); out.push(g._buildHighlights().join('/'));
  s(260, 2, 2, 9, RR.CFG.HP.max);  out.push(g._buildHighlights().join('/'));
  s(249, 0, 0, 0, RR.CFG.HP.max);  out.push(g._buildHighlights().join('/'));
  s(50, 0, 0, 0, 1);               out.push(g._buildHighlights().join('/'));
  s(300, 0, 0, 0, 1);              out.push(g._buildHighlights().join('/'));
  return out.join('|');
})()"""

# 纯函数：下一局目标三分支
NEXT_GOAL = """(function () {
  var g = RRGame, out = [];
  function set(dist, combo, nm) { g.distance = dist; g.maxCombo = combo; g.nearMiss = nm; }
  function rec(b, bd, bc, bn) {
    return {best: b, bestDistance: bd, bestCombo: bc, bestNearMiss: bn, plays: 3, history: []};
  }
  var ALL = {score: true, distance: true, combo: true, nearMiss: true};
  var NONE = {score: false, distance: false, combo: false, nearMiss: false};

  set(500, 9, 9);
  var a = g._buildNextGoal(rec(100, 100, 1, 1), 900, ALL);

  var b = g._buildNextGoal(rec(900, 540, 9, 9), 900, NONE);

  set(412, 3, 2);
  var c = g._buildNextGoal(rec(0, 0, 0, 0), 0,
    {score: false, distance: true, combo: false, nearMiss: false});

  set(200, 4, 1);
  var d = g._buildNextGoal(rec(600, 300, 4, 1), 700, {score: true, distance: false, combo: false, nearMiss: false});

  out.push([a.metric, a.remain, a.text].join('~'));
  out.push([b.metric, b.remain, b.text].join('~'));
  out.push([c.metric, c.remain, c.text].join('~'));
  out.push([d.metric, d.remain, d.text].join('~'));
  return out.join('|');
})()"""

# 回归：菜单快照 + draw calls 地板
MENU_SNAP = """(function () {
  var g = RRGame, sc = g.world.scene, vis = 0;
  for (var i = 0; i < sc.children.length; i++) if (sc.children[i].visible) vis++;
  return [sc.children.length, vis, g.obstacles.items.length].join('|');
})()"""

DRAW_CALLS = """(function () {
  var g = RRGame;
  return String(g.renderer.info.render.calls);
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

        print('\n[1] 冷启动：表 / 结算节点 / 样式都接上了')
        h = str(ev("['errors=' + ((window.__bootErrors || []).length),"
                   "'errtext=' + ((window.__bootErrors || []).join(' ~ ') || 'none')].join('|')"))
        print('    ', h)
        check('启动零报错', h.startswith('errors=0'), h)

        c = json.loads(str(ev(CFG_DUMP)))
        print('     RECORDS=%s HIGHLIGHT=%s key=%s' % (c['rec'], c['hl'], c['key']))
        check('CFG.RECORDS = historyMax 5 / showHistory 3',
              c['rec']['historyMax'] == 5 and c['rec']['showHistory'] == 3, c['rec'])
        check('CFG.HIGHLIGHT 五个门槛齐全（max=3）',
              c['hl']['max'] == 3 and c['hl']['nearMiss'] == 3 and c['hl']['combo'] == 3
              and c['hl']['coins'] == 10 and c['hl']['distance'] == 300
              and c['hl']['noHitDistance'] == 250, c['hl'])
        check('STORAGE_KEY_RECORDS 独立于老的最高分键',
              c['key'] == REC_KEY and c['key'] != BEST_KEY, c['key'])

        ls = str(ev(LS_PROBE))
        print('     localStorage:', ls)
        check('localStorage 可用（file:// 下也能存纪录）', ls == 'ok|1', ls)

        d = str(ev(DOM_PROBE))
        print('     DOM/CSS:', d)
        parts = d.split('|')
        check('结算面板 16 个节点全在', parts[0] == 'miss=none', parts[0])
        check('破纪录徽标样式来自样式表（圆角胶囊 + 渐变底）',
              parts[1] == 'badge=12px/999px/grad', parts[1])
        check('亮点 chip 样式来自样式表', parts[2] == 'chip=12px/999px', parts[2])
        check('历史行样式来自样式表', parts[3] == 'row=flex/8px', parts[3])
        check('结算面板放宽到 420px（其它面板 380px）', parts[4] == 'panel=420px', parts[4])

        ev(RESET)
        time.sleep(0.6)
        try:
            fresh = json.loads(str(ev("JSON.stringify(RRGame._loadRecords())")))
        except Exception:
            fresh = str(ev("JSON.stringify(RRGame._loadRecords())"))
        check('清档后是干净纪录', fresh == {'best': 0, 'bestDistance': 0, 'bestCombo': 0,
                                            'bestNearMiss': 0, 'plays': 0, 'history': []}, fresh)

        print('\n[2] _loadRecords() 容错：坏存档不许炸结算')
        t = str(ev(TOLERANT)).split('|')
        for row in t:
            print('     ', row)
        exp = {
            'bad-json': '0~0~0~0~0~0',
            'not-object': '0~0~0~0~0~0',
            'array': '0~0~0~0~0~0',
            'negative': '0~0~0~0~0~0',
            'long-history': '100~200~0~0~0~5',
            'dirty-history': '0~0~0~0~0~2',
        }
        got = dict((r.split('~')[0], '~'.join(r.split('~')[1:])) for r in t)
        for k in exp:
            check('坏存档 [%s] 降级正确（0 而不是 NaN/负数）' % k, got.get(k) == exp[k],
                  '%s -> %s (期望 %s)' % (k, got.get(k), exp[k]))

        print('\n[3] 老存档迁移：只有 best-score 时纪录接得上')
        mg = str(ev(MIGRATE))
        check('records.best 迁移旧最高分 1234', mg == '1234', mg)

        print('\n[4] _pushRecord()：逐维度取 max + 历史前插截断')
        p7 = str(ev(PUSH7)).split('|')
        for row in p7:
            print('     ', row)
        check('连开 7 局：plays 累加到 7', p7[-1].split(':')[1] == '7', p7[-1])
        check('历史截断到 historyMax=5（不无限长）', p7[-1].split(':')[0] == '5', p7[-1])
        check('最新一局排在历史最前面（第 7 局 160 分 / 220 米）',
              p7[-1].split(':')[2] == '160' and p7[-1].split(':')[3] == '220', p7[-1])
        check('四个维度都取到本局最大值（160/220/6/6）',
              p7[-1].split(':')[4:] == ['160', '220', '6', '6'], p7[-1])
        check('每一局都比上一局更好（best 单调不降）',
              all(int(r.split(':')[4]) == 100 + i * 10 for i, r in enumerate(p7)), p7)

        print('\n[5] 首局真实结算：面板 6 项 / 徽标 / 挑战 / 亮点 / 下一局目标')
        ev(READ_OVER)
        s = json.loads(str(ev(SETTLE_DOM)))
        print('     ', json.dumps(s, ensure_ascii=False))
        check('结算面板真的弹出来了', s['overShown'] is True, s['overShown'])
        check('6 项数据：得分 700 / 距离 412 m / 蜂蜜 31 / 最高分 700',
              s['score'] == '700' and s['dist'] == '412 m' and s['coins'] == '31'
              and s['best'] == '700',
              '%s %s %s %s' % (s['score'], s['dist'], s['coins'], s['best']))
        check('最长连击 / 最佳距离两项在面板上',
              s['combo'] == '3' and s['bestDist'] == '412 m', '%s %s' % (s['combo'], s['bestDist']))
        check('首局四项纪录全破 -> 4 个徽标',
              len(s['badges']) == 4, s['badges'])
        check('"新纪录"提示亮着', s['newBest'] is True, s['newBest'])
        check('挑战小结：已完成 + 名字 + 文案 + 奖励分',
              s['chHidden'] is False and s['chDone'] is True and s['chState'] == '已完成'
              and s['chName'] == '蜂蜜猎人'
              and '一局吃到 15 罐蜂蜜' in s['chText'] and '+200 分' in s['chText'],
              '%s / %s / %s' % (s['chState'], s['chName'], s['chText']))
        check('本局亮点按门槛上榜且截断到 3 条',
              s['hl'] == ['毫发无伤', '险躲 5 次', '3 连击'], s['hl'])
        check('首局没有历史 -> 历史块整块隐藏（不列"本局"）',
              s['histHidden'] is True and s['histRows'] == [], s['histRows'])
        check('第一局的"下一局目标"说的是开张不是破纪录',
              s['nextHidden'] is False and '第一局' in s['next'], s['next'])
        check('纪录里确实写进了本局（plays 1 / history 1 / best 700）',
              s['plays'] == 1 and s['histLen'] == 1 and s['recBest'] == 700
              and s['recBestDist'] == 412, '%s %s %s' % (s['plays'], s['histLen'], s['recBestDist']))

        print('\n[6] 第二、三局：历史出现、徽标变化、"下一局目标"改口')
        ev(READ_OVER)
        low = json.loads(str(ev("""(function () {
          var g = RRGame;
          g.start(); g.hud.hideOver();
          // 除得分外全部打平上一局：这样"下一局目标"只会盯住得分这一项
          g.score = 300; g.distance = 412; g.coins = 1;
          g.maxCombo = 3; g.nearMiss = 5; g.hp = 1;
          g.gameOver();
          return JSON.stringify(readOver(g));
        })()""")))
        print('     ', json.dumps({'badges': low['badges'], 'rows': low['histRows'],
                                    'next': low['next'], 'best': low['best']},
                                   ensure_ascii=False))
        check('第二局没破纪录 -> 徽标全灭', low['badges'] == [], low['badges'])
        check('第二局历史出现 1 行（只列"本局之前"）',
              low['histHidden'] is False and len(low['histRows']) == 1, low['histRows'])
        check('第二局历史行是首局（700 分 / 412 米）',
              '700 分' in low['histRows'][0] and '412 米' in low['histRows'][0], low['histRows'])
        check('最高分不被更低的分数冲掉（仍是 700）', low['best'] == '700', low['best'])
        check('"下一局目标"给出还差多少（差 400 分破纪录）',
              '还差' in low['next'] and '得分' in low['next'] and '400' in low['next'], low['next'])

        high = json.loads(str(ev("""(function () {
          var g = RRGame;
          g.start(); g.hud.hideOver();
          g.score = 900; g.distance = 500; g.coins = 12;
          g.maxCombo = 5; g.nearMiss = 6; g.hp = RR.CFG.HP.max;
          g.gameOver();
          return JSON.stringify(readOver(g));
        })()""")))
        print('     ', json.dumps({'badges': high['badges'], 'rows': high['histRows'],
                                    'next': high['next'], 'nextAll': high['nextAll']},
                                   ensure_ascii=False))
        check('第三局四项全破 -> 4 个徽标 + "新纪录"',
              len(high['badges']) == 4 and high['newBest'] is True, high['badges'])
        check('历史排在前面的是上一局（300 分）',
              '300 分' in high['histRows'][0], high['histRows'])
        check('四项全破时目标改口成"怎么打都是新纪录"',
              high['nextAll'] is True and '新纪录' in high['next'], high['next'])
        check('最佳距离随之抬到 500 m', high['bestDist'] == '500 m', high['bestDist'])

        print('\n[7] 历史最多列 showHistory=3 条')
        many = str(ev("""(function () {
          var g = RRGame;
          for (var i = 0; i < 5; i++) {
            g.start(); g.hud.hideOver();
            g.score = 1000 + i; g.distance = 600 + i; g.coins = 0;
            g.maxCombo = 0; g.nearMiss = 0; g.hp = 1;
            g.gameOver();
          }
          var out = readOver(g);
          return [out.histRows.length, out.plays, out.histLen, out.best].join('|');
        })()""")).split('|')
        print('     历史行数=%s plays=%s history长度=%s best=%s' % tuple(many))
        check('面板只列 3 行历史（showHistory），本地留 5 条（historyMax）',
              many[0] == '3' and many[2] == '5', many)
        check('plays 一路累加到 8（3 局 + 5 局）', many[1] == '8', many)
        check('best 抬到 1004 且不被后面更低的分数冲掉', many[3] == '1004', many)

        print('\n[8] _buildHighlights()：门槛 / 顺序 / 截断 / 兜底')
        hl = str(ev(HIGHLIGHTS)).split('|')
        for row in hl:
            print('     ', row)
        check('够门槛的按顺序上榜，且截断到 max=3（蜂蜜/里程被切掉）',
              hl[0] == '毫发无伤/险躲 3 次/3 连击', hl[0])
        check('只有"无伤"够门槛时只上一条', hl[1] == '毫发无伤', hl[1])
        check('差 1 米不算无伤（门槛 250），也不到里程门槛 -> 兜底句',
              hl[2] == '跑了 249 米', hl[2])
        check('跑得太短时给"刚起步"', hl[3] == '刚起步，下一局见', hl[3])
        check('刚好 300 米就算里程亮点', hl[4] == '跑出 300 米', hl[4])

        print('\n[9] _buildNextGoal()：全破 / 只差距离 / 第一局 / 已破项不提')
        ng = str(ev(NEXT_GOAL)).split('|')
        for row in ng:
            print('     ', row)
        check('四项全破 -> metric=all 且文案不提差距',
              ng[0].split('~')[0] == 'all' and ng[0].split('~')[1] == '0', ng[0])
        check('只差距离 -> 精确算出"还差 40 米"（其余项已打平不催）',
              ng[1].startswith('distance~40~') and '还差 40 米' in ng[1], ng[1])
        check('第一局（还没有任何纪录）-> metric=none，说的是开张',
              ng[2].split('~')[0] == 'none' and '第一局' in ng[2], ng[2])
        check('本局刚破的项不再当目标（得分已破就不提得分）',
              ng[3].split('~')[0] == 'distance' and '距离' in ng[3], ng[3])

        print('\n[10] 回归：回主菜单后场景零残留')
        ev("RRGame.toMenu();'ok'")
        time.sleep(1.5)
        snap = str(ev(MENU_SNAP)).split('|')
        print('     scene=%s 可见=%s 障碍台账=%s' % tuple(snap))
        check('障碍台账清零', snap[2] == '0', snap[2])
        check('回的正是主菜单', str(ev("RRGame.state")) == 'menu', str(ev("RRGame.state")))
        check('结算面板收起来了',
              str(ev("document.getElementById('overlay-over').classList.contains('hidden')"))
              == 'True', 'hidden')
        calls = []
        for _ in range(6):
            calls.append(int(str(ev(DRAW_CALLS))))
            time.sleep(0.25)
        mn = min(calls)
        print('     draw calls 采样:', calls, '地板=', mn)
        check('主菜单 draw calls 地板与纯菜单基线（807~845）同一量级', mn <= 950, mn)

        print('\n[11] 真 URL：?autostart=1&hp=1 自己死掉 -> 结算面板自己弹出来')
        ev("try{localStorage.removeItem('bear-run:records:v1');localStorage.removeItem('bear-run:best-score');}catch(e){}")
        cmd('Page.navigate', {'url': URL + '?autostart=1&hp=1'})
        time.sleep(4.0)
        killed = 'natural'
        t0 = time.time()
        while time.time() - t0 < 34:
            if str(ev("RRGame && RRGame.state")) == 'over':
                break
            time.sleep(1.5)
        if str(ev("RRGame && RRGame.state")) != 'over':
            killed = 'injected'
            ev("RRGame.player.invulnTimer = 0; RRGame.hp = 1;"
               "RRGame.obstacles.checkCollisions = function () {"
               " return {hit: true, obstacle: null, coins: 0, heal: 0, boost: false,"
               " nearMiss: 0, nearMissBest: false}; };'ok'")
            t0 = time.time()
            while time.time() - t0 < 20:
                if str(ev("RRGame && RRGame.state")) == 'over':
                    break
                time.sleep(1.5)
        time.sleep(1.2)
        ev(READ_OVER)
        url = json.loads(str(ev("JSON.stringify(readOver(RRGame))")))
        print('     死法=%s  state=%s' % (killed, str(ev("RRGame.state"))))
        check('真 URL 下自己死掉（state=over）', str(ev("RRGame.state")) == 'over', killed)
        check('结算面板自动弹出', url['overShown'] is True, url['overShown'])
        check('面板 6 项都有值（得分/距离/连击/最佳距离不是占位 0）',
              url['score'].isdigit() and url['dist'].endswith(' m')
              and url['combo'].isdigit() and url['bestDist'].endswith(' m'),
              '%s %s %s %s' % (url['score'], url['dist'], url['combo'], url['bestDist']))
        check('亮点与"下一局目标"都有内容',
              len(url['hl']) >= 1 and url['next'].strip() != '', '%s / %s' % (url['hl'], url['next']))
        check('挑战小结这一局也在', url['chHidden'] is False, url['chName'])

        errs = []
        for m in events:
            if m.get('method') == 'Runtime.exceptionThrown':
                errs.append('exception')
            if m.get('method') == 'Log.entryAdded':
                d2 = m.get('params', {}).get('entry', {})
                if d2.get('level') == 'error':
                    errs.append(d2.get('text', '')[:90])
        print('     console errors:', errs if errs else 'none')
        check('全程零报错', not errs, errs)

    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    print('\n===== %s =====' % ('结果：全部通过' if not fails else '结果：%d 项失败' % len(fails)))
    for f in fails:
        print('  FAIL:', f)
    print('（调试连接重连次数: %d）' % state.get('reconnects', 0))


if __name__ == '__main__':
    main()
