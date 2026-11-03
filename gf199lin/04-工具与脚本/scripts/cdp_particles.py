"""粒子特效自检：用 CDP 驱动无头 Chrome 真跑一局，逐项确认粒子接线正确。

无头环境看不见画面，所以验证方式是"触发计数 + draw call 增量"：
  RRGame.particles.stats.byName   每类特效各触发了多少次
  RRGame.particles.live()         当前活跃粒子数（池满/回收是否正常）
  renderer.info.render.calls      开关粒子池的差值必须正好 = 2（整局固定的代价）
另外：页面零报错（着色器编译失败会被 three 打到控制台）+ 截几张图给人眼看。

用法： python cdp_particles.py [file:///.../index.html]
"""

import base64
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
PROFILE = r"<TEMP>\cdp-profile-particles"
PORT = 9341
# 功能断言这趟不截图（加 --shots 才截）：截图会把 headless 浏览器搞挂，见 shot() 注释
SHOTS = '--shots' in sys.argv
# URL 在这里用 Path.as_uri() 生成，避免中文路径经过 shell 参数被 ANSI 编码吃掉
_GAME = Path(HERE).joinpath('..', '..', 'radar-runner', 'index.html').resolve()
URL = sys.argv[1] if len(sys.argv) > 1 else _GAME.as_uri()

HEALTH = (
    "(function(){var P=window.RR&&RR.Particles,g=window.RRGame;"
    "var p=g?g.particles:null;"
    "return ['three='+(window.THREE?THREE.REVISION:'MISSING'),"
    "'errors='+((window.__bootErrors||[]).length),"
    "'errtext='+((window.__bootErrors||[]).join(' ~ ')||'none'),"
    "'mod='+(P?'loaded':'MISSING'),"
    "'inst='+(p?(p.enabled?'on':'off'):'none'),"
    "'pools='+(p&&p.dust?(p.dust.cap+'/'+p.spark.cap):'-'),"
    "'calls='+(g?g.renderer.info.render.calls:-1),"
    "'canvas='+(document.getElementById('game')?document.getElementById('game').width+'x'+document.getElementById('game').height:'none')"
    "].join('|');})()"
)

STATS = (
    "(function(){var p=RRGame.particles,s=p.stats;"
    "var keys=Object.keys(s.byName).sort();"
    "var txt=keys.map(function(k){return k+':'+s.byName[k];}).join(',');"
    "var l=p.live();"
    "return ['state='+RRGame.state,'dist='+Math.round(RRGame.distance),"
    "'live='+l.dust+'/'+l.spark,'spawn='+s.dust+'/'+s.spark,"
    "'names='+(txt||'none'),'hp='+RRGame.hp].join('|');})()"
)


def main():
    print('URL :', URL)
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--remote-debugging-port=%d' % PORT,
         '--window-size=1280,720', '--hide-scrollbars', '--no-first-run',
         '--no-default-browser-check', '--disable-extensions',
         '--enable-unsafe-swiftshader', '--remote-allow-origins=*',
         '--user-data-dir=' + PROFILE, URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        fails = []
        failed_shots = []
        state = {'ws': ws}

        def find_ws():
            """重新从 /json/list 捞一次目标：截图偶尔会把 GPU 进程搞挂，
            连接被重置是 harness 的问题，不该当成游戏失败。"""
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
            for attempt in range(5):
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
                except BaseException as e:      # 连 KeyboardInterrupt 之外的一切都接住
                    last = e
                    print('     [ws 重连] %s: %s' % (type(e).__name__, str(e)[:70]))
                    time.sleep(1.2)
                    u = find_ws()
                    if not u:
                        time.sleep(1.0)
                        u = find_ws()
                    if not u:
                        print('     调试目标没了（页面可能被整个重建）')
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
                    events.append({'method': 'HARNESS.reconnect', 'params': {}})
                    state['reconnects'] = state.get('reconnects', 0) + 1
                    # 重连后确认页面本身还活着：这是区分"harness 抖动"和"页面真崩"的判据
                    try:
                        state['ws'].send(json.dumps({
                            'id': counter[0] + 50000, 'method': 'Runtime.evaluate',
                            'params': {'expression':
                                       "(typeof RRGame!=='undefined')"
                                       "?RRGame.stats().state+'/err'"
                                       "+((window.__bootErrors||[]).length)"
                                       ":'RRGame 没了'", 'returnByValue': True}}))
                        while True:
                            m = json.loads(state['ws'].recv())
                            if m.get('id') == counter[0] + 50000:
                                print('     重连后页面状态 => %s'
                                      % m.get('result', {}).get('result', {}).get('value'))
                                break
                    except BaseException as e3:
                        print('     重连后探测失败: %s' % str(e3)[:70])
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

        def shot(name):
            # 实测：对"正在渲染的重场景"连续 captureScreenshot 会把整个 headless
            # 浏览器搞挂（/json/list 里连 page 目标都没了）。这是环境/截图路径的
            # 问题，不是游戏的问题。所以功能断言这条线一律不截图（SHOTS=False），
            # 画面佐证交给 cdp_shots.py 单独一趟——那趟就算拍完就崩也不影响结论。
            if not SHOTS:
                return True
            try:
                data = cmd('Page.captureScreenshot', {'format': 'png'})['result']['data']
            except BaseException as e:
                print('     截图失败（不影响断言）: %s: %s' % (type(e).__name__, str(e)[:60]))
                failed_shots.append(name)
                return False
            with open(os.path.join(HERE, name), 'wb') as f:
                f.write(base64.b64decode(data))
            print('     截图 ->', name)
            return True

        def names_now():
            return str(ev(STATS)).split('names=')[1].split('|')[0]

        cmd('Page.enable')
        cmd('Runtime.enable')
        time.sleep(3.5)

        print('\n[1] 冷启动')
        h = str(ev(HEALTH))
        print('    ', h)
        check('Particles.js 已加载', 'mod=loaded' in h)
        check('粒子系统已建好并启用', 'inst=on' in h)
        check('两个池容量正确（260/200）', 'pools=260/200' in h, h.split('pools=')[1][:10])
        check('启动零报错（着色器编译失败会在这里露出来）', 'errors=0' in h,
              h.split('errtext=')[1][:60])
        base_calls = int(h.split('calls=')[1].split('|')[0])
        print('     主菜单 draw calls =', base_calls)

        print('\n[2] 点开始 -> 跑动扬尘自动冒出来')
        ev("document.getElementById('btn-start').click();'ok'", gesture=True)
        time.sleep(1.5)
        st = str(ev(STATS))
        print('    ', st)
        check('进入 playing', 'state=playing' in st)
        check('跑动扬尘在发射（run）', 'run:' in names_now(), names_now()[:80])
        lv = str(ev("RRGame.particles.live().dust"))
        check('跑动时尘土池里有活跃粒子', int(lv) > 0, 'live dust=%s' % lv)

        print('\n[3] 跳跃 -> 蹬地(takeoff) 与 落地(land)')
        n0 = names_now()
        ev("RRGame.handleAction('jump');'ok'")
        time.sleep(0.25)
        ev("RRGame.handleAction('jump');'ok'")     # 空中再按一次无效果，确认不会重复喷
        check('有 takeoff', 'takeoff:' in names_now())
        shot('pt-jump.png')
        time.sleep(1.2)
        check('有 land（落地扬尘）', 'land:' in names_now(), names_now()[:80])

        print('\n[4] 滑铲 -> 贴地拖尘')
        ev("RRGame.handleAction('slide');'ok'")
        time.sleep(0.2)
        shot('pt-slide.png')
        time.sleep(0.5)
        st = str(ev(STATS))
        check('有 slide', 'slide:' in names_now(), names_now()[:80])
        print('    ', st)

        print('\n[5] 拾取/受击分支（走 Game 真实结算代码，stub 完立刻还原）')
        r = ev(
            "(function(){var q=[{coins:3},{heal:1},{boost:true},{coins:1}];"
            "var om=RRGame.obstacles,old=om.checkCollisions;"
            "om.checkCollisions=function(){var x=q.shift()||{};x.hit=false;return x;};"
            "var out=[];"
            "while(q.length){RRGame._resolveCollisions();out.push(RRGame.coins+'/'+RRGame.hp);}"
            "om.checkCollisions=old;"
            "return out.join(' ')+' | box='+(om.checkCollisions===old);})()")
        print('     结算轨迹:', r)
        time.sleep(0.3)
        nm = names_now()
        print('    ', str(ev(STATS)))
        for name in ('coin', 'heal', 'boost'):
            check('有 %s 粒子' % name, (name + ':') in nm)
        r = ev(
            "(function(){var om=RRGame.obstacles,old=om.checkCollisions;"
            "RRGame.player.invulnTimer=0;"
            "om.checkCollisions=function(){return {hit:true,coins:0,heal:0,boost:false};};"
            "RRGame._resolveCollisions();om.checkCollisions=old;"
            "return RRGame.hp;})()")
        time.sleep(0.3)
        check('受击有 hit 粒子', 'hit:' in names_now(), 'hp=%s' % r)

        print('\n[6] 加速带尾焰：踩上之后持续喷')
        ev("RRGame.boostTimer=RR.CFG.BOOST.duration;RRGame.player.x=0;'ok'")
        time.sleep(0.5)
        st = str(ev(STATS))
        print('    ', st)
        check('加速时星火池有活跃粒子', int(st.split('live=')[1].split('/')[1].split('|')[0]) > 0,
              st.split('live=')[1][:9])
        ev("RRGame.boostTimer=0;'ok'")

        print('\n[7] 代价核算：粒子整局固定占多少 draw call')
        ev("RRGame.state='paused';RRGame.particles.clear();'ok'")
        time.sleep(0.6)
        delta = None
        for attempt in range(6):
            ev("(function(){var p=RRGame.particles;"
               "p.dust.points.visible=false;p.spark.points.visible=false;return 'ok';})()")
            time.sleep(0.35)
            a = int(ev("RRGame.renderer.info.render.calls"))
            ev("(function(){var p=RRGame.particles;"
               "p.dust.points.visible=true;p.spark.points.visible=true;return 'ok';})()")
            time.sleep(0.35)
            b = int(ev("RRGame.renderer.info.render.calls"))
            print('     第%d次: 关=%d 开=%d 差=%d' % (attempt + 1, a, b, b - a))
            if b - a == 2:
                delta = 2
                break
        check('两个粒子池合计恰好 +2 draw call', delta == 2,
              '实测差值=%s（场景在动时偶尔会 ±1，重试 6 次仍不稳定才算失败）' % (b - a))

        print('\n[8] 池回收：清场后不该有任何残留')
        st = str(ev(STATS))
        check('clear() 之后活跃粒子归零', st.split('live=')[1].startswith('0/0'),
              st.split('live=')[1][:9])

        print('\n[9] 掉血 -> 处决：斧头砸地那一下的星火')
        ev("RRGame.state='playing';RRGame.best=0;RRGame.hp=1;"
           "RRGame.player.invulnTimer=0;'ok'")
        r = ev(
            "(function(){var om=RRGame.obstacles,old=om.checkCollisions;"
            "om.checkCollisions=function(){return {hit:true,coins:0,heal:0,boost:false};};"
            "RRGame._resolveCollisions();om.checkCollisions=old;"
            "return RRGame.state+' hp='+RRGame.hp;})()")
        print('    致命一击:', r)
        t0 = time.time()
        axe_ok = False
        while time.time() - t0 < 12:
            if 'axe:' in names_now():
                axe_ok = True
                break
            time.sleep(0.2)
        check('斧头落地有星火（README 承诺的那一条）', axe_ok, names_now()[:90])
        st = str(ev(STATS))
        print('    ', st)
        check('处决瞬间星火池确实在喷', int(st.split('live=')[1].split('/')[1].split('|')[0]) > 0,
              st.split('live=')[1][:9])
        shot('pt-axe.png')

        t0 = time.time()
        while time.time() - t0 < 10 and 'state=over' not in str(ev(STATS)):
            time.sleep(0.25)
        check('结算完成（over）', 'state=over' in str(ev(STATS)))

        print('\n[10] 回主菜单 -> 清场，粒子不残留到下一局')
        # 关键点：必须和 toMenu() 在同一个 JS 任务里读，中间不能让 rAF 插队，
        # 否则读到的是"菜单里熊二刚跑出来的新尘"，看起来像没清干净
        r = ev("(function(){var p=RRGame.particles;RRGame.toMenu();"
               "var l=p.live();return l.dust+'/'+l.spark;})()")
        check('toMenu() 那一瞬间活跃粒子归零（上一局的云被清掉）', str(r) == '0/0', str(r))
        time.sleep(1.2)
        st = str(ev(STATS))
        print('    ', st)
        check('回到 menu', 'state=menu' in st)
        lv = st.split('live=')[1].split('|')[0]
        n_d, n_s = [int(v) for v in lv.split('/')]
        check('菜单里只有熊二自己跑出的新尘，没有残留云（<20）',
              0 <= n_d < 20 and n_s == 0, lv)

        print('\n[11] 收尾：长跑一遍看会不会漏、会不会报错')
        ev("document.getElementById('btn-start').click();'ok'", gesture=True)
        t0 = time.time()
        peak = 0
        while time.time() - t0 < 14:
            ev("RRGame.hp=5;'ok'")          # 别在长跑里被撞死
            st = str(ev(STATS))
            try:
                peak = max(peak, int(st.split('live=')[1].split('/')[0]))
            except Exception:
                pass
            time.sleep(0.4)
        st = str(ev(STATS))
        print('    ', st)
        print('     实测距离 %s 米，尘土池峰值 %d 个活跃粒子' % (st.split('dist=')[1].split('|')[0], peak))
        check('长跑没把池用满也不会崩（峰值 < 容量）', 0 < peak < 260, 'peak=%d' % peak)
        check('长跑零中断（仍在 playing）', 'state=playing' in st)
        shot('pt-run.png')
        h = str(ev(HEALTH))
        print('    ', h)
        check('全程零启动错误', 'errors=0' in h)

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
        check('控制台零报错（含着色器编译错误）', not errs, errs[:2])

        if failed_shots:
            print('\n[12] 补拍失败的截图: %s' % failed_shots)
            for name in list(failed_shots):
                if name == 'pt-run.png':
                    time.sleep(0.5)
                elif name in ('pt-jump.png', 'pt-slide.png'):
                    ev("RRGame.handleAction('jump' if '%s'=='pt-jump.png' "
                       "else 'slide');'ok'" % name)
                    time.sleep(0.35)
                elif name == 'pt-axe.png':
                    ev("RRGame.hp=1;RRGame.invuln=0;'ok'")
                    time.sleep(1.6)
                if shot(name):
                    failed_shots.remove(name)
            print('     补拍结果: %s' % (failed_shots if failed_shots else '全部拍到'))

        print('\n===== 结果：%s =====' % (
            '全部通过' if not fails else '失败 %d 项: %s' % (len(fails), fails)))
        if failed_shots:
            print('（截图未拍到，不影响上面的断言）: %s' % failed_shots)
        print('（调试连接重连次数: %d）' % state.get('reconnects', 0))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == '__main__':
    main()
