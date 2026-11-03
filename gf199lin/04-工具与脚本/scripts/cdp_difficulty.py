# -*- coding: utf-8 -*-
"""任务 1（难度曲线）自检：CDP 驱动无头 Chrome，断言速度曲线 / 反应时间 / 分区概率。

核心要证明的三件事：
  1. CFG.speedAt() 是一条连续、单调、封顶 25 的曲线（热身段 8.5->13.5）。
  2. 两行之间的"反应时间" = gap / speed，随里程从 1.82s 收到 1.02s，
     并且**全程都 >= 1.0s** —— 旧版在第 800 米只有 0.63s，这是要修的真 bug。
  3. 双障碍 / combo 的概率来自分区配置：第 1 区恒为 0，掉血后有 55 米喘息。

用法： python cdp_difficulty.py [file:///.../index.html]
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
PROFILE = r"<TEMP>\cdp-profile-diff"
PORT = 9355
_GAME = Path(HERE).joinpath('..', '..', 'radar-runner', 'index.html').resolve()
URL = sys.argv[1] if len(sys.argv) > 1 else _GAME.as_uri()

ROW_STATS = """window.__rowStats = function (om) {
  var blocked = {}, kinds = [];
  for (var i = 0; i < om.items.length; i++) {
    var it = om.items[i];
    if (it.kind === 'coin' || it.kind === 'heal' || it.kind === 'boost') continue;
    var ls = it.lanes || [it.lane];
    for (var j = 0; j < ls.length; j++) blocked[ls[j]] = true;
    kinds.push(it.kind);
  }
  return { lanes: Object.keys(blocked).length, kinds: kinds };
}; 'ok'"""

REACTION = """(function () {
  var S = RR.CFG.SPEED, P = RR.CFG.SPAWN;
  var out = [];
  [0, 100, 180, 300, 450, 800, 1080, 1200, 1500, 2000].forEach(function (d) {
    var s = RR.CFG.speedAt(d);
    var t = Math.min(1, d / P.rampMeters);
    var tg = P.timeGapStart + (P.timeGapMin - P.timeGapStart) * t;
    var g = Math.max(P.minGap, s * tg);
    var oS = Math.min(S.max, 8.5 + d * 0.011);          // 旧版直线速度
    var oG = 15.5 + (6.8 - 15.5) * t;                    // 旧版米数间隔
    out.push(d + ':' + s.toFixed(2) + '/' + g.toFixed(1) + '/' + (g / s).toFixed(2)
             + '|' + (oG / oS).toFixed(2));
  });
  return out.join(' ');
})()"""

SPEED_CURVE = """(function () {
  var S = RR.CFG.SPEED, bad = 0, prev = -1, cap = null;
  for (var d = 0; d <= 3000; d += 2) {
    var v = RR.CFG.speedAt(d);
    if (v < prev - 1e-9) bad++;
    if (v > S.max + 1e-9) bad++;
    prev = v;
    if (cap === null && v >= S.max) cap = d;
  }
  var cont = RR.CFG.speedAt(S.cruiseAt - 0.01) + '|' + RR.CFG.speedAt(S.cruiseAt + 0.01);
  return ['at0=' + RR.CFG.speedAt(0), 'at180=' + RR.CFG.speedAt(180),
          'at2000=' + RR.CFG.speedAt(2000), 'cap=' + cap, 'bad=' + bad,
          'cont=' + cont, 'neg=' + RR.CFG.speedAt(-50)].join(' ');
})()"""

STAGE_ROLL = """(function () {
  var om = new RR.ObstacleManager(RRGame.scene);
  var res = [], N = 600;
  for (var si = 0; si < RR.CFG.DIFFICULTY.length; si++) {
    var d = RR.CFG.DIFFICULTY[si].at + 60;
    var multi = 0, maxLanes = 0;
    for (var i = 0; i < N; i++) {
      om.reset(false);
      om.distance = d;
      om.speed = RR.CFG.speedAt(d);
      om._spawnRow(-100);
      var s = window.__rowStats(om);
      if (s.lanes >= 2) multi++;
      if (s.lanes > maxLanes) maxLanes = s.lanes;
    }
    res.push(si + ':' + (multi / N).toFixed(3) + '/' + maxLanes);
  }
  om.reset(false);
  window.__omtest = om;
  return res.join(' ');
})()"""

FORCED = """(function () {
  var om = window.__omtest, out = [], old = Math.random;
  function run(d, rnd, mercy) {
    om.reset(false);
    om.distance = d;
    om.speed = RR.CFG.speedAt(d);
    if (mercy) om.grantMercy();
    Math.random = function () { return rnd; };
    try { om._spawnRow(-100); } finally { Math.random = old; }
    var s = window.__rowStats(om);
    return s.lanes + ':' + s.kinds.join('+');
  }
  out.push('s1@.05=' + run(60, 0.05, false));      // 第 1 区：旧版这里会摆出双障碍
  out.push('s4@.10=' + run(900, 0.10, false));     // 第 4 区 combo 分支
  out.push('s4@.28=' + run(900, 0.28, false));     // 只走 double 分支（非 combo）
  out.push('s4@.35=' + run(900, 0.35, false));     // 两个概率都不触发
  out.push('s4mercy@0=' + run(900, 0.0, true));    // 喘息段：任何概率都不摆双障碍
  return out.join(' | ');
})()"""

MERCY_TICK = """(function () {
  var om = window.__omtest;
  om.reset(false);
  om.grantMercy();
  var a = om.mercyMetersLeft;
  om.update(1 / 60, 10, 0);
  om.update(1 / 60, 10, 0);
  var b = om.mercyMetersLeft;
  om.update(1.0, 60, 0);
  var c = om.mercyMetersLeft;
  return a + ' ' + b.toFixed(3) + ' ' + c;
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

        print('\n[1] 冷启动')
        h = str(ev("['three='+(window.THREE?THREE.REVISION:'MISSING'),"
                   "'errors='+((window.__bootErrors||[]).length),"
                   "'errtext='+((window.__bootErrors||[]).join(' ~ ')||'none'),"
                   "'calls='+(RRGame?RRGame.renderer.info.render.calls:-1),"
                   "'spawn='+JSON.stringify(RR.CFG.SPAWN),"
                   "'speed='+JSON.stringify(RR.CFG.SPEED)].join('|')"))
        print('    ', h)
        check('启动零报错', 'errors=0' in h, h.split('errtext=')[1][:60])
        base_calls = int(h.split('calls=')[1].split('|')[0])
        print('     主菜单 draw calls =', base_calls)
        check('SPEED/SPAWN 已是新配置（含 speedAt 曲线）',
              'timeGapStart' in h and 'cruiseAt' in h, h.split('speed=')[1][:70])

        print('\n[2] 速度曲线 CFG.speedAt')
        sc = str(ev(SPEED_CURVE))
        print('    ', sc)
        check('speedAt(0) = 8.5', sc.split('at0=')[1].split(' ')[0] == '8.5', sc.split('at0=')[1][:10])
        check('speedAt(180) = 13.5（热身段末尾正好是 cruise）',
              sc.split('at180=')[1].split(' ')[0] == '13.5', sc.split('at180=')[1][:10])
        check('speedAt(2000) 封顶 25', sc.split('at2000=')[1].split(' ')[0] == '25')
        cap = int(sc.split('cap=')[1].split(' ')[0])
        check('到顶里程在 1040~1120 米之间（旧版要 1500）', 1040 <= cap <= 1120, 'cap=%d' % cap)
        check('0~3000 米逐点单调、不超上限', sc.split('bad=')[1].split(' ')[0] == '0')
        lo, hi = [float(v) for v in sc.split('cont=')[1].split(' ')[0].split('|')]
        check('cruiseAt 处连续（左右极限只差 <0.01）', abs(hi - lo) < 0.01, '%.4f vs %.4f' % (lo, hi))
        check('负里程兜底为 8.5', sc.split('neg=')[1] == '8.5', sc.split('neg=')[1])

        print('\n[3] 反应时间 = gap / speed（这是本条最重要的断言）')
        rt = str(ev(REACTION))
        print('     里程:新速度/新间隔/新反应|旧反应')
        print('    ', rt)
        cells = []
        for tok in rt.split(' '):
            d, rest = tok.split(':')
            new_s, gap, react_and_old = rest.split('/')
            new_react, old_react = react_and_old.split('|')
            cells.append((int(d), float(new_s), float(gap), float(new_react), float(old_react)))
        check('反应时间随里程单调不增', all(cells[i][3] >= cells[i + 1][3] - 1e-9
                                            for i in range(len(cells) - 1)),
              ' -> '.join('%.2f' % c[3] for c in cells))
        worst = min(cells, key=lambda c: c[3])
        check('全程反应时间 >= 1.0s（旧版第 800 米只有 0.63s）',
              worst[3] >= 1.0, '最紧 %.2fs @ %d 米' % (worst[3], worst[0]))
        d800 = [c for c in cells if c[0] == 800][0]
        check('第 800 米反应时间至少是旧版的 2 倍', d800[3] >= d800[4] * 2.0 - 1e-9,
              '%.2fs vs 旧版 %.2fs' % (d800[3], d800[4]))
        check('开局间隔仍是 1.82s（前段手感不变）', abs(cells[0][3] - 1.82) < 0.01,
              '%.2f' % cells[0][3])

        print('\n[4] 分区摆障碍概率（每区 600 次，纯随机）')
        print('    ', str(ev(ROW_STATS)))
        CFG_DIFF = json.loads(str(ev("JSON.stringify(RR.CFG.DIFFICULTY)")))
        rolls = str(ev(STAGE_ROLL))
        print('    ', rolls)
        rate, maxl = {}, {}
        for tok in rolls.split(' '):
            si, rest = tok.split(':')
            r, m = rest.split('/')
            rate[int(si)] = float(r)
            maxl[int(si)] = int(m)
        check('第 1 区永不摆双障碍（旧版 0.24 概率会摆）', rate.get(0) == 0.0,
              'rate=%.3f' % rate.get(0, -1))
        # 期望值由配置直接推出来（不写死数字，改了配置这行会跟着变）：
        # P(堵>=2 道) = combo + (1-combo) x [P(先挑到 mover) + (1-...)*double]
        expect = {}
        for si, st in enumerate(CFG_DIFF):
            kinds = st['kinds']
            pm = kinds.count('mover') / float(len(kinds))
            c = st['comboChance']
            d = st['doubleChance']
            expect[si] = c + (1 - c) * (pm + (1 - pm) * d)
        for si in sorted(rate):
            check('第 %d 区堵两道及以上的比例符合配置（期望 %.3f）' % (si + 1, expect[si]),
                  abs(rate[si] - expect[si]) < 0.07,
                  '实测 %.3f vs 期望 %.3f（%d 区 kinds=%s double=%.2f combo=%.2f）'
                  % (rate[si], expect[si], si + 1, ','.join(CFG_DIFF[si]['kinds']),
                     CFG_DIFF[si]['doubleChance'], CFG_DIFF[si]['comboChance']))
        check('任何一行都留活路（最多堵两条道）',
              all(v <= 2 for v in maxl.values()), str(maxl))

        print('\n[5] 概率分支的确定性验证（注入 Math.random）')
        forced = str(ev(FORCED))
        print('    ', forced)
        parts = dict(p.split('=', 1) for p in forced.split(' | '))
        check('第 1 区强制触发概率也只堵一条道', parts.get('s1@.05', '').startswith('1:'),
              parts.get('s1@.05'))
        check('第 4 区 combo 分支：两件都是 block（堵两道留一条）',
              parts.get('s4@.10', '') == '2:block+block', parts.get('s4@.10'))
        check('第 4 区 0.28 走 double 分支而非 combo（含跳/滑障碍）',
              parts.get('s4@.28', '').startswith('2:') and 'block+block' != parts.get('s4@.28', ''),
              parts.get('s4@.28'))
        check('第 4 区 0.35 两个概率都不触发 -> 单障碍',
              parts.get('s4@.35', '').startswith('1:'), parts.get('s4@.35'))
        check('喘息段内 combo 概率失效 -> 单障碍',
              parts.get('s4mercy@0', '').startswith('1:'), parts.get('s4mercy@0'))

        print('\n[6] 掉血喘息（mercy）计时')
        mt = str(ev(MERCY_TICK))
        print('    ', mt)
        a, b, c = mt.split(' ')
        check('grantMercy() 给满 55 米', a == '55', a)
        check('按里程递减（0.333 米后剩 ~54.67）', abs(float(b) - 54.667) < 0.01, b)
        check('走完就归零、不会变负', c == '0', c)

        print('\n[7] 真跑一局：Game 用的就是 CFG.speedAt')
        ev("document.getElementById('btn-start').click();'ok'", gesture=True)
        seen = []
        bad = 0
        t0 = time.time()
        while time.time() - t0 < 6.5:
            ev("RRGame.hp=5;'ok'")
            s = str(ev("(function(){var g=RRGame,d=g.distance;return g.speed.toFixed(3)+'/'"
                       "+RR.CFG.speedAt(d).toFixed(3)+'/'+Math.round(d)"
                       "+'/'+(g.obstacles.gap/g.speed).toFixed(2)+'/'+g.boostTimer.toFixed(2)"
                       "+'/'+g.state"
                       "+'/'+RR.CFG.speedAt(d-g.speed*0.05).toFixed(3);})()"))
            if s.startswith('JSERROR'):
                bad += 1
                continue
            v, want, d, react, boost, st_, low = s.split('/')
            seen.append((float(v), float(want), int(d), float(react), float(boost), st_, float(low)))
            time.sleep(0.6)
        for x in seen:
            print('     %6.3f / 曲线 %6.3f  @%4dm  反应 %.2fs  boost=%s  %s'
                  % (x[0], x[1], x[2], x[3], x[4], x[5]))
        # 加速带会让实测速度 = 曲线 x BOOST.speedMul，这是设计如此，要按倍率还原再比
        mul = json.loads(str(ev("JSON.stringify([RR.CFG.BOOST.speedMul,RR.CFG.BOOST.duration])")))
        boost_mul = float(mul[0])
        # 速度是用**上一帧**的里程算出来的（tick 里先定速度、再积分里程），
        # 所以它必须落在 [speedAt(d - v*maxDt), speedAt(d)] 这个一帧窗口里 —— 精确到浮点。
        slack = 2e-3
        offs = [(x[0] / (boost_mul if x[4] > 0 else 1.0), x[1], x[6]) for x in seen]
        check('每帧速度都等于 CFG.speedAt(该帧里程)（允许一帧滞后，加速带按倍率还原）',
              all(low - slack <= v <= want + slack for v, want, low in offs),
              '样本 %d，一帧窗口外 %d 个' % (
                  len(offs), sum(1 for v, want, low in offs
                                 if not (low - slack <= v <= want + slack))))
        check('速度确实在涨', len(seen) >= 5 and seen[-1][0] > seen[0][0],
              '%.2f -> %.2f' % (seen[0][0], seen[-1][0]))
        check('实战反应时间也 >= 1.0s', all(r >= 1.0 for _, _, _, r, _, _, _ in seen),
              '最紧 %.2fs' % min(r for _, _, _, r, _, _, _ in seen))
        check('采样期间一直在 playing', all(x[5] == 'playing' for x in seen),
              str(sorted(set(x[5] for x in seen))))

        r = ev("(function(){var om=RRGame.obstacles,old=om.checkCollisions;"
               "om.checkCollisions=function(){return {hit:true,coins:0,heal:0,boost:false,obstacle:null};};"
               "RRGame.player.invulnTimer=0;RRGame._resolveCollisions();om.checkCollisions=old;"
               "return RRGame.hp+'/'+om.mercyMetersLeft;})()")
        print('     受击后 hp/喘息剩余:', r)
        check('真实受击后喘息被点亮（55 米）', str(r).endswith('/55'), str(r))
        time.sleep(1.0)
        m2 = float(str(ev("RRGame.obstacles.mercyMetersLeft")))
        check('喘息随真实跑动消耗', 0 <= m2 < 55, '%.1f' % m2)

        print('\n[8] 回归：主菜单 draw calls 未受影响')
        # 菜单里世界还在滚动、扬尘还在喷，单次读数在 810~890 之间跳；
        # 取多次的**最小值**当稳定地板再比，才有意义。
        def menu_floor():
            vals = []
            for _ in range(6):
                vals.append(int(ev("RRGame.renderer.info.render.calls")))
                time.sleep(0.25)
            return min(vals), vals
        ev("RRGame.toMenu();'ok'")
        time.sleep(1.5)
        fl_before, raw_before = menu_floor()
        # 再进一局建立几个障碍，然后回菜单，比"回菜单后"的绘制量和"进局前"一样
        ev("document.getElementById('btn-start').click();'ok'", gesture=True)
        time.sleep(2.2)
        ev("RRGame.toMenu();'ok'")
        time.sleep(1.5)
        r2 = str(ev("(function(){var g=RRGame.obstacles;"
                    "return g.mercyMetersLeft+'/'+g.distance+'/'+g.speed;})()"))
        print('     回菜单后 obstacles:', r2)
        check('toMenu() 复位了喘息与速度', r2 == '0/0/8.5', r2)
        fl_after, raw_after = menu_floor()
        print('     菜单 draw calls 地板 %d（基线 %d）' % (fl_after, fl_before))
        print('     基线采样 %s / 回菜单采样 %s' % (raw_before, raw_after))
        check('主菜单 draw calls 地板与基线同量级', abs(fl_after - fl_before) <= 40,
              '%d vs %d' % (fl_after, fl_before))
        check('没有失控的绘制量（菜单 < 1000）', fl_after < 1000, fl_after)

        errs = []
        for m in events:
            meth = m.get('method')
            p = m.get('params', {}) or {}
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
