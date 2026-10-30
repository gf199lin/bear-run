# -*- coding: utf-8 -*-
"""在本地 http 托管 bear-run 游戏页（多版本可切换）。

为什么需要它：
  官方站是 https://gf199lin.github.io/bear-run/ ，而雷达桥是 ws://127.0.0.1:8766。
  HTTPS 页面连接不加密 ws:// 会被浏览器按混合内容拦截（code=1006）。
  用本机 http 托管同一份源码，页面与 ws:// 同为 http 源，不再拦截。

用法：
  python tools/serve_bear_run.py                 # 默认托管 bear-run-new（新版，含联调台）
  GAME_DIR=bear-run python tools/serve_bear_run.py   # 托管旧版
  GAME_PORT=8771 python tools/serve_bear_run.py

  游戏页   http://127.0.0.1:8771/?input=radar&radar=ws://127.0.0.1:8766&debug=1
  联调台   http://127.0.0.1:8771/radar-connect.html
  体检台   http://127.0.0.1:8771/radar-diagnose.html
"""
import http.server
import os
import socketserver
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_BASE = os.path.abspath(os.path.join(HERE, '..'))

GAME_DIR = os.environ.get('GAME_DIR', 'bear-run-new')
PORT = int(os.environ.get('GAME_PORT', '8771'))
ROOT = os.path.join(ROOT_BASE, GAME_DIR)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # 本地调试，禁用缓存，避免改了脚本还看到旧的
        self.send_header('Cache-Control', 'no-store')
        http.server.SimpleHTTPRequestHandler.end_headers(self)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    if not os.path.isfile(os.path.join(ROOT, 'index.html')):
        print('找不到游戏目录: %s' % ROOT, flush=True)
        return 1
    os.chdir(ROOT)
    print('托管 %s' % ROOT, flush=True)
    print('游戏页 http://127.0.0.1:%d/?input=radar&radar=ws://127.0.0.1:8766&debug=1' % PORT, flush=True)
    print('联调台 http://127.0.0.1:%d/radar-connect.html' % PORT, flush=True)
    with Server(('127.0.0.1', PORT), Handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
