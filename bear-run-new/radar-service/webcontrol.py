#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""radar-runner 本地控制服务

作用：
  1. 提供游戏页面静态文件（替代 `python -m http.server 8801`）
  2. 提供 /api/*，让游戏页面上的雷达面板按钮能真正起停本地进程

为什么需要它：
  浏览器沙箱不允许网页直接启动本地程序。所以页面按钮只能向这个
  本机服务发 HTTP 请求，由它去调 Python 子进程。

只监听 127.0.0.1，不对外网开放。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # radar-runner/
LOGDIR = os.path.join(HERE, 'logs')
SERVE_PY = os.path.join(HERE, 'radar_serve.py')
VIEWER_PY = os.path.join(HERE, 'server.py')
VIEWER_PID = os.path.join(LOGDIR, 'viewer.pid')
SERVE_PIDS = os.path.join(LOGDIR, 'serve-pids.json')

HTTP_PORT = int(os.environ.get('RC_HTTP_PORT', '8801'))
WS_LR = int(os.environ.get('RC_WS_LR', '8766'))
WS_UD = int(os.environ.get('RC_WS_UD', '8767'))
VIEWER_PORT = int(os.environ.get('RC_VIEWER_PORT', '8099'))

_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers

def port_open(port, host='127.0.0.1', timeout=0.25):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _popen_detached(args, cwd=HERE):
    """起一个不占当前控制台的子进程。"""
    flags = 0
    if os.name == 'nt':
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def _read_serve_pids():
    try:
        with open(SERVE_PIDS, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get('procs') or data.get('pids') or []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _read_pid_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        out = subprocess.run(
            ['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
            capture_output=True, text=True, timeout=8,
        ).stdout
        return str(pid) in out and 'python' in out.lower()
    except Exception:
        return False


def _kill(pid):
    try:
        subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                       capture_output=True, timeout=15)
        return True
    except Exception:
        return False


def _latest_log_tail(lines=40):
    try:
        names = [os.path.join(LOGDIR, n) for n in os.listdir(LOGDIR)
                 if n.lower().endswith('.log')]
        if not names:
            return ''
        newest = max(names, key=os.path.getmtime)
        with open(newest, 'r', encoding='utf-8', errors='replace') as f:
            buf = f.readlines()
        return ''.join(buf[-lines:])
    except Exception as e:
        return '(读取日志失败: %s)' % e


def snapshot():
    lr = port_open(WS_LR)
    ud = port_open(WS_UD)
    viewer = port_open(VIEWER_PORT)
    pids = _read_serve_pids()

    # 端口通不等于「是我们起的」：可能被旧的 bridge / 串口助手占着。
    # 只有 serve-pids.json 里登记过、且进程还活着，才算本工具的推流。
    def owned(ws_port):
        for p in pids:
            if str(p.get('ws_port')) == str(ws_port):
                return _pid_alive(p.get('pid'))
        return False

    own_lr = owned(WS_LR)
    own_ud = owned(WS_UD)

    return {
        'ok': True,
        'stream': (lr and own_lr) or (ud and own_ud),
        'lr': lr,
        'ud': ud,
        'owned': {'lr': own_lr, 'ud': own_ud},
        'foreign': {'lr': lr and not own_lr, 'ud': ud and not own_ud},
        'viewer': viewer,
        'ports': {'lr': WS_LR, 'ud': WS_UD, 'viewer': VIEWER_PORT, 'http': HTTP_PORT},
        'roles': [{'role': p.get('role'), 'port': p.get('port'),
                   'ws': p.get('ws_port')} for p in pids],
        'time': time.strftime('%H:%M:%S'),
    }


# ---------------------------------------------------------------- actions

def start_stream():
    if port_open(WS_LR) or port_open(WS_UD):
        snap = snapshot()
        if snap['stream']:
            return {'ok': True, 'already': True, 'msg': '推流已经在跑了'}
        bad = WS_LR if snap['foreign']['lr'] else WS_UD
        return {
            'ok': False,
            'msg': '端口 %d 被别的程序占着（旧的 bridge / 串口助手 / 另一个窗口）。'
                   '先把它们关掉，再用菜单 [5] 停掉残留推流。' % bad,
        }
    if port_open(VIEWER_PORT):
        return {'ok': False, 'msg': '点云查看器正占着串口，先把它关掉再连雷达'}
    with _LOCK:
        _popen_detached([sys.executable, SERVE_PY])
        deadline = time.time() + 20
        while time.time() < deadline:
            if port_open(WS_LR) or port_open(WS_UD):
                return {'ok': True, 'msg': '雷达已连接'}
            time.sleep(0.3)
    return {
        'ok': False,
        'msg': '20 秒内没起来。多半是没找到雷达板，或串口被别的程序占用。',
        'hint': '看下面日志的 [serial] 行，或运行 radar-service 里的检查脚本。',
    }


def stop_stream():
    if not (port_open(WS_LR) or port_open(WS_UD)):
        return {'ok': True, 'msg': '推流本来就没在跑'}
    snap = snapshot()
    if snap['foreign']['lr'] or snap['foreign']['ud']:
        return {'ok': False,
                'msg': '这个端口是别的程序占的，本工具停不掉，请手动关掉那个窗口'}
    with _LOCK:
        try:
            subprocess.run([sys.executable, SERVE_PY, 'stop'],
                           capture_output=True, timeout=60)
        except Exception as e:
            return {'ok': False, 'msg': '停止失败: %s' % e}
        for _ in range(20):
            if not (port_open(WS_LR) or port_open(WS_UD)):
                return {'ok': True, 'msg': '雷达已断开'}
            time.sleep(0.3)
    return {'ok': False, 'msg': '端口还占着，可能不是本工具起的进程'}


def start_viewer():
    if port_open(VIEWER_PORT):
        return {'ok': True, 'already': True, 'msg': '点云查看器已经在跑了'}
    if port_open(WS_LR) or port_open(WS_UD):
        return {'ok': False, 'msg': '雷达推流正占着串口，先断开雷达再开点云'}
    os.makedirs(LOGDIR, exist_ok=True)
    with _LOCK:
        p = _popen_detached([sys.executable, VIEWER_PY, '--http-port', str(VIEWER_PORT)])
        deadline = time.time() + 15
        while time.time() < deadline:
            if port_open(VIEWER_PORT):
                with open(VIEWER_PID, 'w', encoding='utf-8') as f:
                    f.write(str(p.pid))
                return {'ok': True, 'msg': '点云已启动', 'url': 'http://127.0.0.1:%d' % VIEWER_PORT}
            time.sleep(0.3)
    _kill(p.pid)
    return {'ok': False, 'msg': '点云没起来，可能是串口打不开或依赖缺失'}


def stop_viewer():
    pid = _read_pid_file(VIEWER_PID)
    if _pid_alive(pid):
        _kill(pid)
    try:
        os.remove(VIEWER_PID)
    except OSError:
        pass
    for _ in range(20):
        if not port_open(VIEWER_PORT):
            return {'ok': True, 'msg': '点云已关闭'}
        time.sleep(0.3)
    return {'ok': False, 'msg': '8099 端口仍被占用，可能是别的程序（如另开的查看器）'}


ACTIONS = {
    'start-stream': start_stream,
    'stop-stream': stop_stream,
    'start-viewer': start_viewer,
    'stop-viewer': stop_viewer,
}


# ---------------------------------------------------------------- http

class Handler(SimpleHTTPRequestHandler):
    server_version = 'radar-runner/1.0'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # -- 基础设施 ---------------------------------------------------

    def _cors(self):
        origin = self.headers.get('Origin', '')
        # 允许：同源、file://（Origin: null）、本机页面
        if origin in ('null', '') or '127.0.0.1' in origin or 'localhost' in origin:
            self.send_header('Access-Control-Allow-Origin', origin or '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass   # 别把每帧静态请求刷到控制台

    # -- 路由 -------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/status':
            return self._json(snapshot())
        if path == '/api/logs':
            return self._json({'ok': True, 'tail': _latest_log_tail(40)})
        if path in ('/', '/index.html'):
            self.path = '/index.html'
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        origin = self.headers.get('Origin', '')
        # 简单 CSRF 防护：只接受本机页面或 file:// 页面
        if not (origin in ('null', '') or '127.0.0.1' in origin or 'localhost' in origin):
            return self._json({'ok': False, 'msg': '拒绝来自外部页面的请求'}, 403)
        name = path.rsplit('/', 1)[-1]
        fn = ACTIONS.get(name)
        if not fn:
            return self._json({'ok': False, 'msg': '未知接口: %s' % name}, 404)
        try:
            result = fn()
        except Exception as e:
            result = {'ok': False, 'msg': '执行出错: %s' % e}
        return self._json(result)


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    if port_open(HTTP_PORT):
        print('[WARN] 端口 %d 已被占用：可能已经有一个控制服务在跑，或别的程序占了它。'
              % HTTP_PORT)
    print('=' * 52)
    print('  radar-runner 控制台')
    print('  游戏页  http://127.0.0.1:%d/index.html' % HTTP_PORT)
    print('  接口    /api/status  /api/start-stream  /api/start-viewer')
    print('=' * 52)
    srv = ThreadingHTTPServer(('127.0.0.1', HTTP_PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出。')
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
