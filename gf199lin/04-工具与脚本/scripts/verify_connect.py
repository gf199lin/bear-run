# -*- coding: utf-8 -*-
"""自检：radar-connect.html（雷达联调台）能不能真的连上雷达、读数、自检、带地址进游戏。

前置：radar-connect.html 经 http 打开（file:// 下 localStorage 受限），假雷达在 8766。
用法: python verify_connect.py <radar-connect.html 的 http URL> [port]

分档验证：
  still  -> 纯静息 + 干净上游：静息自检应判"干净"
  noisy  -> 纯静息 + 真实上游噪声：静息自检应判"噪"（与向导探针同口径）
  good   -> 配合的玩家：x 真的在动、方向自检给结论、记住地址、一键进游戏
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
PROC = None
WS = None
MSG_ID = 0
PASS = 0
FAIL = 0
ERRORS = []


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
    """Runtime.evaluate -> python value；顺带收集页面异常（favicon 404 不算）。"""
    global MSG_ID
    MSG_ID += 1
    WS.send(json.dumps({
        "id": MSG_ID, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
    }))
    while True:
        data = json.loads(WS.recv())
        if data.get("method") == "Runtime.exceptionThrown":
            d = data.get("params", {}).get("exceptionDetails", {})
            ERRORS.append(str(d.get("text")) + " " + str((d.get("exception") or {}).get("description", ""))[:200])
            continue
        if data.get("method") == "Log.entryAdded":
            e = data.get("params", {}).get("entry", {})
            url = str(e.get("url", ""))
            if e.get("level") == "error" and "favicon" not in url:
                ERRORS.append("log: " + str(e.get("text"))[:160] + " @ " + url[:80])
            continue
        if data.get("id") == MSG_ID:
            return data.get("result", {}).get("result", {}).get("value")


def evj(expr):
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


def num_of(text):
    try:
        return float(str(text).split()[0])
    except Exception:
        return None


def connect_to(url):
    ev("document.getElementById('url').value = %s; 1" % json.dumps(url))
    ev("document.getElementById('btn-connect').click(); 1")
    return wait_until("document.getElementById('status').textContent",
                      lambda v: isinstance(v, str) and "已连接" in v, 8)


def still_check(label, expect_clean, wait=12.0):
    ev("document.getElementById('btn-still').click(); 1")
    time.sleep(1.2)
    so = ev("document.getElementById('still-out').textContent")
    ok(isinstance(so, str) and "倒计时" in so, label + " 静息自检在跑（倒计时/计数刷新）", so)
    time.sleep(wait)
    sv = ev("document.getElementById('still-verdict').textContent")
    kind = ev("document.getElementById('still-verdict').className")
    rate = num_of(str(sv).split("误报帧 ")[-1]) if "误报帧" in str(sv) else None
    if expect_clean:
        ok(isinstance(kind, str) and "ok" in kind, label + " 干净上游判为干净（不误报）", str(sv)[:70])
    else:
        ok(isinstance(kind, str) and "err" in kind, label + " 真实噪声判为噪（与向导探针同口径）", str(sv)[:70])
    return sv


def main():
    global PROC, WS
    url = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9378
    prof = Path(os.environ.get("TEMP", ".")) / "opencode" / ("connprof-%d-%d" % (port, int(time.time())))
    prof.mkdir(parents=True, exist_ok=True)

    PROC = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        "--user-data-dir=" + str(prof), "--remote-allow-origins=*",
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
    WS = websocket.create_connection(ws_url, timeout=25)
    WS.send(json.dumps({"id": -1, "method": "Runtime.enable"}))
    WS.send(json.dumps({"id": -2, "method": "Log.enable"}))

    # ---------- C0 装配 ----------
    ev("localStorage.clear(); 1")
    ev("location.reload(); 1")
    wait_until("!!(window.RR && RR.CFG)", lambda v: v is True, 12)
    c0 = evj("""(function(){return JSON.stringify({
      calib: !!(window.RR && RR.Calibration && RR.Calibration.load),
      key: (RR.CFG && RR.CFG.STORAGE_KEY_RADAR_URL) || null,
      urlVal: (document.getElementById('url')||{}).value || ''});})()""")
    ok(c0.get("calib") is True, "C0 js/Calibration.js 已加载（联调台能读/清校准存档）")
    ok(c0.get("key") == "bear-run:radar:url", "C0 config.js 提供 STORAGE_KEY_RADAR_URL", c0.get("key"))
    ok("127.0.0.1:8766" in str(c0.get("urlVal")), "C0 ?radar= 参数已自动填入地址框", c0.get("urlVal"))

    # ---------- C1 连接 still 档：状态/帧率/延迟/波形 ----------
    st = connect_to("ws://127.0.0.1:8766/still")
    ok(isinstance(st, str) and "已连接" in st, "C1 连接成功", st)
    ok("ok" in str(ev("document.getElementById('dot').className")), "C1 状态灯变绿")
    time.sleep(3.0)
    c1 = evj("""(function(){return JSON.stringify({
      frames: document.getElementById('v-frames').textContent,
      rate: document.getElementById('v-rate').textContent,
      x: document.getElementById('v-x').textContent,
      lane: document.getElementById('lane').textContent,
      lat: document.getElementById('v-lat').textContent,
      cal: document.getElementById('v-x-cal').textContent});})()""")
    n = num_of(c1.get("frames"))
    rate = num_of(c1.get("rate"))
    ok(n is not None and n > 30, "C1 3 秒收到足够帧（>30）", c1.get("frames"))
    ok(rate is not None and 18 <= rate <= 22, "C1 帧率稳定在 20Hz 附近", c1.get("rate"))
    ok(c1.get("x") not in (None, "—"), "C1 横向 x 有数值", c1.get("x"))
    ok(c1.get("lane") in ("左边", "中间", "右边"), "C1 车道判定显示正常", c1.get("lane"))
    ok(c1.get("lat") not in (None, "—"), "C1 端到端延迟算出来了（ts 按毫秒解析）", c1.get("lat"))
    ok("未校准" in str(c1.get("cal")), "C1 未校准时明确提示", c1.get("cal"))
    wave = ev("(function(){var c=document.getElementById('wave');var g=c.getContext('2d');"
              "var d=g.getImageData(0,0,c.width,c.height).data;var n=0;"
              "for(var i=0;i<d.length;i+=4) if(d[i+2]>120) n++; return n;})()")
    ok(isinstance(wave, (int, float)) and wave > 200, "C1 x 波形画出来了（蓝像素数）", wave)

    # ---------- C2 静息自检：干净档应判干净 ----------
    still_check("C2", True)

    # ---------- C3 静息自检：真实噪声档应判噪 ----------
    ok("已连接" in str(connect_to("ws://127.0.0.1:8766/noisy")), "C3 切到 noisy 档重连成功")
    time.sleep(1.0)
    sv = still_check("C3", False)
    ok("雷达把" in str(sv) or "误报" in str(sv), "C3 判噪时说清了后果（会停用雷达跳/蹲）", str(sv)[:80])

    # ---------- C4 good 档：数据在动 + 方向自检 ----------
    ok("已连接" in str(connect_to("ws://127.0.0.1:8766/good")), "C4 切到 good 档重连成功")
    moved = wait_until("document.getElementById('v-x').textContent",
                       lambda v: (num_of(v) is not None and abs(num_of(v)) > 0.3), 12)
    ok(moved is not None and abs(num_of(moved)) > 0.3, "C4 x 真的在动（|x| > 0.3）", moved)
    ev("document.getElementById('btn-left').click(); 1")
    time.sleep(2.4)
    ev("document.getElementById('btn-right').click(); 1")
    time.sleep(2.4)
    dinfo = evj("""(function(){return JSON.stringify({
      out: document.getElementById('dir-out').textContent,
      cls: document.getElementById('dir-verdict').className,
      txt: document.getElementById('dir-verdict').textContent});})()""")
    ok("左：" in str(dinfo.get("out")) and "右：" in str(dinfo.get("out"))
       and "—" not in str(dinfo.get("out")).split("左：")[1][:8],
       "C4 左右两侧都采到了数值", dinfo.get("out"))
    ok("verdict show" in str(dinfo.get("cls")), "C4 方向自检给出结论", dinfo.get("cls"))
    ok(len(str(dinfo.get("txt"))) > 12, "C4 结论有可读的解释文案", str(dinfo.get("txt"))[:70])

    # ---------- C5 记住地址 + 一键进游戏 ----------
    mem = ev("localStorage.getItem('bear-run:radar:url')")
    ok(isinstance(mem, str) and "8766/good" in mem, "C5 连接成功后地址写入 localStorage", mem)
    ok(ev("document.getElementById('btn-play').disabled") is False, "C5 「进入游戏（带校准）」按钮可用")
    ev("document.getElementById('btn-play').click(); 1")
    got = wait_until("location.pathname.indexOf('index.html') >= 0 && "
                     "!!(window.RRGame && RRGame.input && RRGame.input.current)",
                     lambda v: v is True, 15)
    ok(got is True, "C5 点击后跳进 index.html（带 input=radar & radar= 参数）", ev("location.search"))
    eff = ev("RR.CFG.RADAR.defaultUrl")
    ok(isinstance(eff, str) and "127.0.0.1:8766" in eff,
       "C5 游戏侧读到了联调台记下的地址（组员不用手拼 URL）", eff)
    src_url = ev("(RRGame && RRGame.input && RRGame.input.current) ? RRGame.input.current.url : null")
    ok(src_url == "ws://127.0.0.1:8766/good",
       "C5 雷达源真的连到联调台给的地址（不是 config 默认值）", src_url)

    # ---------- C6 校准存档区（回联调台） ----------
    ev("location.href = %s; 1" % json.dumps(url))
    wait_until("!!(window.RR && RR.Calibration)", lambda v: v is True, 12)
    cal = ev("document.getElementById('cal-out').textContent")
    ok(isinstance(cal, str) and ("没有校准存档" in cal or "v=" in cal), "C6 校准存档区可读", str(cal)[:40])
    ev("document.getElementById('btn-cal-clear').click(); 1")
    cal2 = ev("document.getElementById('cal-out').textContent")
    ok(isinstance(cal2, str) and "没有校准存档" in cal2, "C6 清除校准存档按钮生效", str(cal2)[:40])

    ok(len(ERRORS) == 0, "C7 全流程没有 JS 异常 / 资源 404", "; ".join(ERRORS[:3]))

    print("\n== %d PASS / %d FAIL ==" % (PASS, FAIL))
    try:
        WS.close()
    except Exception:
        pass
    PROC.terminate()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
