# -*- coding: utf-8 -*-
"""集成自检：校准向导是否真的接进了 index.html（菜单入口 / 自动引导 / 帧改写）。

用法:  python cdp_calib.py <index.html 的 file URI> [port]
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROC = None
WS = None
MSG_ID = 0
PASS = 0
FAIL = 0


def ok(cond, name, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    line = ("PASS " if cond else "FAIL ") + name
    if extra is not None:
        line += "  |  " + str(extra)
    print(line)


def targets(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def ev(expr):
    """Runtime.evaluate -> python value (None on failure)."""
    global MSG_ID
    MSG_ID += 1
    WS.send(json.dumps({
        "id": MSG_ID, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
    }))
    while True:
        data = json.loads(WS.recv())
        if data.get("id") == MSG_ID:
            res = data.get("result", {}).get("result", {})
            return res.get("value")


def evj(expr):
    """Evaluate an expression that returns JSON text."""
    v = ev(expr)
    if not isinstance(v, str):
        return {}
    try:
        return json.loads(v)
    except Exception:
        return {"__raw": v}


def wait_until(expr, pred, timeout=8.0, every=0.2):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = ev(expr)
        try:
            if pred(last):
                return last
        except Exception:
            pass
        time.sleep(every)
    return last


def nav_doc(expr_nav, timeout=20.0):
    """执行导航/刷新，并等到"新文档"真的替换了旧文档。

    不做这一步的话，location.href=/reload() 之后的断言可能还在旧文档上求值 ——
    旧文档的输入源/向导状态往往恰好也满足条件，会给出"看起来对"的假结论
    （实测踩过：S5 断言跑在 S4 留下的、已连上 8766 的旧页面上）。
    判据：导航前在旧文档埋 window.__navMark，新文档里它必然不存在。
    """
    ev("window.__navMark = 1; " + expr_nav)
    return wait_until("typeof window.__navMark === 'undefined' && !!document.body",
                      lambda v: v is True, timeout)


def main():
    global PROC, WS
    url = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9370

    prof = Path(os.environ.get("TEMP", ".")) / "opencode" / ("calibprof-%d" % port)
    prof.mkdir(parents=True, exist_ok=True)

    PROC = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        "--user-data-dir=" + str(prof),
        "--allow-file-access-from-files", "--remote-allow-origins=*",
        "--remote-debugging-port=%d" % port, url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    end = time.time() + 25
    while time.time() < end:
        try:
            for t in targets(port):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
        except Exception:
            pass
        if ws_url:
            break
        time.sleep(0.3)
    if not ws_url:
        print("ERR: no page target")
        return 2

    WS = websocket.create_connection(ws_url, timeout=20)

    # ---------- S0 清空本地记录，保证从"首次开局"状态开始 ----------
    ev("localStorage.clear(); 1")
    nav_doc("location.reload(); 1")
    wait_until("!!(window.RRGame && window.RR)", lambda v: v is True, 15)

    # ---------- S1 装配检查 ----------
    s1 = evj("""(function(){
      return JSON.stringify({
        wizard: typeof RR.CalibrationWizard === 'function',
        calib: !!(RR.Calibration && RR.Calibration.load && RR.Calibration.install),
        autoOpen: !!(RR.CFG.RADAR.calib && RR.CFG.RADAR.calib.autoOpen),
        moveDelta: RR.CFG.RADAR.calib ? RR.CFG.RADAR.calib.moveDelta : null,
        btn: !!document.getElementById('btn-calib'),
        status: !!document.getElementById('calib-status'),
        tag: document.querySelectorAll('script[src*="Calibration.js"]').length,
        boot: (window.__bootErrors || []).join(' | '),
        input: RRGame.inputId
      });
    })()""")
    ok(s1.get("wizard") is True, "S1 Calibration.js 已加载（RR.CalibrationWizard 可用）")
    ok(s1.get("calib") is True, "S1 RR.Calibration 读写接口齐全")
    ok(s1.get("autoOpen") is True and s1.get("moveDelta") == 0.35, "S1 config.RADAR.calib 已生效",
       "autoOpen=%s moveDelta=%s" % (s1.get("autoOpen"), s1.get("moveDelta")))
    ok(s1.get("btn") is True and s1.get("status") is True, "S1 菜单里出现校准按钮与状态位")
    ok(s1.get("tag") == 1, "S1 index.html 只引入一次 Calibration.js", "tag=%s" % s1.get("tag"))
    ok(s1.get("boot") == "", "S1 启动无脚本错误", s1.get("boot"))
    ok(s1.get("input") == "keyboard", "S1 默认输入源=键盘（不会误连雷达）", s1.get("input"))

    # ---------- S2 手动入口：点按钮 -> 切雷达 + 打开向导 ----------
    ev("document.getElementById('btn-calib').click(); 1")
    opened = wait_until(
        """(function(){var el=document.querySelector('.calib-overlay');
           return (el && !el.classList.contains('hidden')) ? 'open' : 'closed';})()""",
        lambda v: v == "open", 8)
    ok(opened == "open", "S2 点「校准雷达动作」能打开向导")

    s2 = evj("""(function(){
      var el = document.querySelector('.calib-overlay');
      return JSON.stringify({
        input: RRGame.inputId,
        step: el ? el.querySelector('.calib-step').textContent : '',
        text: el ? el.querySelector('.calib-text').textContent : '',
        msg:  el ? el.querySelector('.calib-msg').textContent : '',
        dots: el ? el.querySelectorAll('.calib-dot').length : 0,
        btns: el ? el.querySelectorAll('.calib-btn').length : 0
      });
    })()""")
    ok(s2.get("input") == "radar", "S2 自动切到雷达输入源", s2.get("input"))
    ok("第 1 步" in (s2.get("step") or ""), "S2 停在第 1 步（站好）", s2.get("step"))
    ok(s2.get("dots") == 5 and s2.get("btns") == 3, "S2 5 个进度点 + 3 个按钮",
       "dots=%s btns=%s" % (s2.get("dots"), s2.get("btns")))

    # ---------- S3 生产路径：装饰器改写真实 RadarInputSource 的每一帧 ----------
    s3 = evj("""(function(){
      var src = RRGame.input.current;
      var cal = { v:1, at:Date.now(), x0:0.55, sign:-1, jumpOk:true, crouchOk:true,
                  source:'radar', unreliable:false };
      var installed = RR.Calibration.install(src, cal);
      src.connected = true;
      src._handleFrame({ ts:1, x:0.90, height:0, jump:0, crouch:0, confidence:1 });
      var a = src.lastRaw ? src.lastRaw.x : null;
      var ra = src.lastRaw ? src.lastRaw.rawX : null;
      src._handleFrame({ ts:2, x:0.10, height:0, jump:0, crouch:0, confidence:1 });
      var b = src.lastRaw ? src.lastRaw.x : null;
      RR.Calibration.setEnabled(src, false);
      src._handleFrame({ ts:3, x:0.90, height:0, jump:0, crouch:0, confidence:1 });
      var off = src.lastRaw ? src.lastRaw.x : null;
      RR.Calibration.setEnabled(src, true);
      src._handleFrame({ ts:4, x:0.90, height:0, jump:0, crouch:0, confidence:1 });
      var back = src.lastRaw ? src.lastRaw.x : null;
      return JSON.stringify({ id: src.id, installed: installed,
        x90: a, raw90: ra, x10: b, off: off, back: back,
        decorated: RR.Calibration.isInstalled(src) });
    })()""")
    ok(s3.get("installed") is True and s3.get("decorated") is True, "S3 装饰器已装到真实雷达源上",
       "src=%s" % s3.get("id"))
    ok(abs((s3.get("x90") or 0) - (-0.35)) < 1e-6, "S3 装反雷达：x=0.90 -> x'=-0.35（被纠偏）", s3.get("x90"))
    ok(abs((s3.get("x10") or 0) - 0.45) < 1e-6, "S3 装反雷达：x=0.10 -> x'=+0.45（够进右道）", s3.get("x10"))
    ok(abs((s3.get("raw90") or 0) - 0.90) < 1e-6, "S3 原始值仍保留在 rawX（调试面板可对照）", s3.get("raw90"))
    ok(abs((s3.get("off") or 0) - 0.90) < 1e-6, "S3 setEnabled(false) 能关掉改写", s3.get("off"))
    ok(abs((s3.get("back") or 0) - (-0.35)) < 1e-6, "S3 能重新开启", s3.get("back"))

    # ---------- S4 关闭向导 ----------
    ev("document.querySelector('.calib-close').click(); 1")
    shut = wait_until(
        """(function(){var el=document.querySelector('.calib-overlay');
           return (el && el.classList.contains('hidden')) ? 'hidden' : 'shown';})()""",
        lambda v: v == "hidden", 6)
    ok(shut == "hidden", "S4 关闭按钮能收起向导")
    ok(ev("RRGame.state") in ("menu", "playing", "paused"), "S4 关闭后游戏状态正常",
       ev("RRGame.state"))

    # ---------- S5 首启自动引导：?input=radar 直接开局 ----------
    # 注意：必须显式给一个连不上的地址（端口 9）来构造"没有雷达"的场景 ——
    # config 默认地址是 ws://127.0.0.1:8766，本机一旦跑着假雷达/真雷达就会真连上，
    # 那样第 1 步立刻通过、断言失效（这本身说明"默认地址能被自动接入"是通的）。
    ev("localStorage.clear(); 1")
    nav_doc("location.href = location.href.split('?')[0] + "
            "'?input=radar&radar=ws://127.0.0.1:9'; 1")
    wait_until("!!(window.RRGame && window.RRGame.inputId === 'radar')", lambda v: v is True, 15)
    auto = wait_until(
        """(function(){var el=document.querySelector('.calib-overlay');
           return (el && !el.classList.contains('hidden')) ? 'open' : 'closed';})()""",
        lambda v: v == "open", 10)
    ok(auto == "open", "S5 带 ?input=radar 开局会自动弹出向导（首启引导）")
    s5 = evj("""(function(){
      var el = document.querySelector('.calib-overlay');
      var src = RRGame.input.current;
      return JSON.stringify({
        input: RRGame.inputId,
        step: el ? el.querySelector('.calib-step').textContent : '',
        msg:  el ? el.querySelector('.calib-msg').textContent : '',
        live: el ? el.querySelector('.calib-live').textContent : '',
        url: location.search,
        eff: (RR.CFG.RADAR || {}).defaultUrl,
        conn: src ? !!src.connected : null,
        stext: src ? (src.statusText || '') : '',
        raw: (src && src.lastRaw) ? src.lastRaw.x : null,
        srcId: src ? src.id : null,
        srcUrl: src ? src.url : null,
        wsState: (src && src.ws) ? src.ws.readyState : null
      });
    })()""")
    ok(s5.get("input") == "radar", "S5 输入源保持雷达", s5.get("input"))
    ok("第 1 步" in (s5.get("step") or ""), "S5 自动停在第 1 步",
       "%s | url=%s eff=%s srcId=%s srcUrl=%s ws=%s conn=%s st=%s raw=%s" % (
           s5.get("step"), s5.get("url"), s5.get("eff"), s5.get("srcId"), s5.get("srcUrl"),
           s5.get("wsState"), s5.get("conn"), s5.get("stext"), s5.get("raw")))
    ok(("雷达未连接" in (s5.get("live") or "")) or ("没收到雷达数据" in (s5.get("msg") or "")),
       "S5 没有雷达时给出明确提示（不静默卡住）", "live=%s | msg=%s" % (s5.get("live"), s5.get("msg")))

    # ---------- S6 老玩家：本地已有可靠校准 -> 自动装上，不再打扰 ----------
    # v2 起存档格式换成 v2（多了 lrOk/探针/阈值）；v2 存档才算"可靠老玩家"。
    ev("""localStorage.setItem('bear-run:calib:v1', JSON.stringify({v:2, at:Date.now(),
        x0:0.12, sign:-1, lrOk:true, jumpOk:true, crouchOk:true, jumpTh:0.5, crouchTh:0.5,
        upVerdict:'clean', source:'radar', unreliable:false})); 1""")
    nav_doc("location.reload(); 1")
    wait_until("!!(window.RRGame && window.RRGame.inputId === 'radar')", lambda v: v is True, 15)
    time.sleep(1.6)                      # 给自动引导 600ms 的窗口
    s6 = evj("""(function(){
      var el = document.querySelector('.calib-overlay');
      var src = RRGame.input.current;
      src.connected = true;
      src._handleFrame({ ts:9, x:0.50, height:0, jump:0, crouch:0, confidence:1 });
      return JSON.stringify({
        hidden: !el || el.classList.contains('hidden'),
        status: document.getElementById('calib-status').textContent,
        usable: RR.Calibration.isUsable(RR.Calibration.load()),
        installed: RR.Calibration.isInstalled(src),
        x: src.lastRaw ? src.lastRaw.x : null
      });
    })()""")
    ok(s6.get("usable") is True and s6.get("installed") is True,
       "S6 有可用校准（v2 存档）时自动装载到雷达源")
    ok(s6.get("hidden") is True, "S6 不再重复弹向导打扰老玩家")
    ok("已校准" in (s6.get("status") or ""), "S6 菜单状态显示已校准", s6.get("status"))
    ok(abs((s6.get("x") or 0) - (-0.38)) < 1e-6,
       "S6 基线/符号按存档生效：x=0.50 -> x'=-0.38", s6.get("x"))

    # ---------- S7 旧版（v1）存档 -> 判过期，重新引导（v2 有意行为） ----------
    ev("""localStorage.setItem('bear-run:calib:v1', JSON.stringify({v:1, at:Date.now(),
        x0:0.12, sign:-1, jumpOk:true, crouchOk:true, source:'radar', unreliable:false})); 1""")
    nav_doc("location.reload(); 1")
    wait_until("!!(window.RRGame && window.RRGame.inputId === 'radar')", lambda v: v is True, 15)
    s7 = evj("""(function(){
      var el = document.querySelector('.calib-overlay');
      var raw = localStorage.getItem('bear-run:calib:v1');
      var stored = raw ? JSON.parse(raw) : null;
      return JSON.stringify({
        loadNull: RR.Calibration.load() === null,
        rawV: stored ? stored.v : null,
        open: !!(el && !el.classList.contains('hidden')),
        input: RRGame.inputId
      });
    })()""")
    s7auto = wait_until(
        """(function(){var el=document.querySelector('.calib-overlay');
           return (el && !el.classList.contains('hidden')) ? 'open' : 'closed';})()""",
        lambda v: v == "open", 10)
    ok(s7.get("loadNull") is True,
       "S7 v1 存档不被 load() 采用（不会拿旧结论硬用）", s7.get("loadNull"))
    ok(s7.get("rawV") == 1, "S7 不偷偷把 v1 存档升级成可信的 v2", s7.get("rawV"))
    ok(s7auto == "open", "S7 v1 存档 -> 重新引导新手教程（v2 有意行为）")

    # ---------- S8 雷达地址记忆（组员真机联调用）----------
    # 联调台 radar-connect.html 和 ?radar= 参数共用同一个 localStorage 键；
    # 用过一次以后，组员从主菜单进游戏就不需要再拼 URL。
    ev("localStorage.clear(); 1")
    nav_doc("location.href = location.href.split('?')[0] + "
            "'?input=radar&radar=ws://192.168.1.77:8766'; 1")
    wait_until("!!(window.RR && RR.CFG && RR.CFG.RADAR && RR.CFG.RADAR.defaultUrl)",
               lambda v: v is True, 15)
    s8a = evj("""(function(){return JSON.stringify({
        eff: RR.CFG.RADAR.defaultUrl,
        mem: localStorage.getItem(RR.CFG.STORAGE_KEY_RADAR_URL)});})()""")
    ok(s8a.get("eff") == "ws://192.168.1.77:8766", "S8 ?radar= 生效（默认地址被覆盖）", s8a.get("eff"))
    ok(s8a.get("mem") == "ws://192.168.1.77:8766",
       "S8 用过的雷达地址写入 localStorage 记忆", s8a.get("mem"))
    s8src = ev("(RRGame && RRGame.input && RRGame.input.current) ? RRGame.input.current.url : null")
    ok(s8src == "ws://192.168.1.77:8766",
       "S8 雷达源真的连到 ?radar= 给的地址（不是 config 默认值）", s8src)

    nav_doc("location.href = location.href.split('?')[0] + '?input=radar'; 1")
    wait_until("!!(window.RRGame && window.RRGame.inputId === 'radar')", lambda v: v is True, 15)
    s8b = ev("RR.CFG.RADAR.defaultUrl")
    ok(s8b == "ws://192.168.1.77:8766",
       "S8 不带参数时游戏用记忆地址（组员不用再拼 URL）", s8b)

    ev("localStorage.clear(); 1")
    print("")
    print("PASSED=%d FAILED=%d" % (PASS, FAIL))
    WS.close()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        try:
            if WS:
                WS.close()
        except Exception:
            pass
        try:
            if PROC:
                PROC.kill()
        except Exception:
            pass
    sys.exit(code)
