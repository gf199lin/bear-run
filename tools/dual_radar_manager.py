# -*- coding: utf-8 -*-
"""双雷达角色分配服务：谁管左右、谁管上下，由"插入先后"决定，可手动改。

为什么需要它
------------
两块板现在都是 **1T2R**（VID:PID 都是 10C4:EA60，描述都是
"Silicon Labs CP210x USB to UART Bridge"）。USB 指纹、板型探测、
`find_radar_port()` 的 vidpid 匹配都分不出谁是谁，所以：

* `--port auto` 只会抢到第一块；
* 靠型号/描述判断"这块是左右还是上下"必然出错。

本服务只做一件事：**把"哪块串口 = 哪个角色"这件事说清楚，并按这个分配
去启动项目里已验证的两个判据程序**：

  角色 lr（水平 / 左右换道）
      → dual-radar-kit/tools/radar_live_ws.py --role lr --mode pos
        判据：radar-pointcloud/radar_actions.py 的 xbase（项目已验收）
        ws://127.0.0.1:8766

  角色 ud（竖直 / 跳蹲）
      → 同一个桥接器 --role ud --jump-engine sm --jump-band 1,3
        判据：tools/jumpduck_sm.py 径向速度状态机（四批 96 样本 85.4%）
        ws://127.0.0.1:8767

默认分配（就是"插入先后"）
--------------------------
1. 服务启动后**第一次出现的串口**记为第 1 块 → 水平 / 左右；
2. 第二个出现的记为第 2 块 → 竖直 / 跳蹲；
3. 序列持久化在 `.deepworks/tmp/dual-radar-assign.json`，重启服务不会乱；
4. 服务启动时两块都已经插着（拿不到真实插入时刻）→ 按 COM 号升序兜底，
   页面上会明说"顺序是推定的"，可以手动改；
5. 页面上随时可以把某块改成"水平 / 竖直 / 不参与"，改动立即生效。

用法
----
    python tools/dual_radar_manager.py            # 起服务并打开联调台
    python tools/dual_radar_manager.py --no-open  # 只起服务
    python tools/dual_radar_manager.py --port 8778

打开 http://127.0.0.1:8778/radar-connect-dual.html ，第 0 张卡片就是分配面板。
（本服务同时托管 **bear-run-new/** —— 最新版游戏：index.html 带新手指引 + 雷达测试，
以及双雷达联调台 radar-connect-dual.html。页面的 fetch 与 API 同源。）
"""
import argparse
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

# ------------------------------------------------------------------ 路径
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KIT = os.path.join(ROOT, 'dual-radar-kit')
BRIDGE = os.path.join(KIT, 'tools', 'radar_live_ws.py')
# 托管的网页是「最新版游戏」目录：里面有 index.html（新手指引 + 雷达测试）
# 和 radar-connect-dual.html（双雷达联调台）。
GAME = os.path.join(ROOT, 'bear-run-new')
PAGE = 'radar-connect-dual.html'
TMP = os.path.join(ROOT, '.deepworks', 'tmp')
STATE_PATH = os.path.join(TMP, 'dual-radar-assign.json')

DEFAULT_HTTP_PORT = 8778

# 两个角色固定各占一个 ws 端口：游戏的 URL 参数约定就是 radar / radar2
ROLE_WS_PORT = {'lr': 8766, 'ud': 8767}
ROLE_LABEL = {
    'lr': '水平 · 左右换道',
    'ud': '竖直 · 跳 / 蹲',
    'none': '不参与',
}
ROLE_SHORT = {'lr': '水平', 'ud': '竖直', 'none': '—'}

# 水平：验收过的 xbase 判据，瞬时角度出位置电平（低延迟）
# 2026-11-19：回到原始（clutter-keep 1.0、cfar 2.6、min-snr 7.0），
# 只保留 --band 放宽。原因：
#   calib.json 的 band=[0.681,1.363] 只覆盖 1.02m 那一个 bin，
#   1T2R 实际有 1.02 和 1.70 两个 bin，写死会丢掉约 48% 的帧（AGENTS.md 记过）。
# 不校准。让 zero 在启动时锁到人，游戏用 config.js 默认的 x0=0/xGain=1 即可。
LR_ARGS = ['--mode', 'pos', '--band', '0.5,1.9']


def setup_console():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


# ------------------------------------------------------------------ 串口枚举
def com_number(device):
    """COM12 → 12；解析不出来给一个大数，排到最后。"""
    m = re.search(r'(\d+)', device or '')
    return int(m.group(1)) if m else 10 ** 6


def list_ports():
    """枚举串口 → [{device, description, hwid, location, serial, vid, pid}]。

    pyserial 的 comports() 顺序是注册表 SERIALCOMM 顺序（近似 COM 号序），
    不是真实插入时刻；所以这里只把它当**首轮兜底**，真实先后由
    Registry 在运行期观察"哪个串口是新出现的"来决定。
    """
    try:
        from serial.tools import list_ports as lp
    except Exception as e:  # 缺 pyserial 时不要把整个服务带崩
        return [], 'pyserial 不可用：%r' % (e,)
    out = []
    for p in lp.comports():
        hwid = p.hwid or ''
        loc = ''
        m = re.search(r'LOCATION=([^\s]+)', hwid)
        if m:
            loc = m.group(1)
        out.append({
            'device': p.device,
            'description': p.description or '',
            'hwid': hwid,
            'location': loc,
            'serial': p.serial_number or '',
            'vid': p.vid,
            'pid': p.pid,
        })
    return out, None


def is_radar_like(p):
    """是不是本项目会用的板：CP210x(10c4:ea60) / FTDI(0403:6001) / 描述线索。"""
    blob = ('%s %s' % (p.get('description', ''), p.get('hwid', ''))).lower()
    if '10c4:ea60' in blob or '0403:6001' in blob:
        return True
    return any(h in blob for h in ('cp210', 'silicon labs', 'ftdi', 'sihab'))


# ------------------------------------------------------------------ 状态文件
class Registry(object):
    """串口 → 插入序 → 角色，以及两个桥接进程。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {'seen': {}, 'roles': {}, 'flags': {'lr_flip': False}}
        self.ports = []
        self.warnings = []
        self.bridges = {'lr': None, 'ud': None}
        self.load()

    # ---- 持久化
    def load(self):
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                d = json.load(f)
            seen = d.get('seen') or {}
            roles = d.get('roles') or {}
            flags = d.get('flags') or {}
            self.data = {
                'seen': {str(k): v for k, v in seen.items() if isinstance(v, dict)},
                'roles': {str(k): v for k, v in roles.items()
                          if v in ('lr', 'ud', 'none')},
                'flags': {'lr_flip': bool(flags.get('lr_flip'))},
            }
        except Exception:
            self.data = {'seen': {}, 'roles': {}, 'flags': {'lr_flip': False}}

    def save(self):
        try:
            os.makedirs(TMP, exist_ok=True)
            tmp = STATE_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            log('!! 分配状态写入失败：%r' % (e,))

    # ---- 发现顺序
    def absorb(self, ports):
        """把本次枚举结果并进 seen；新出现的串口按出现顺序拿下一个序号。

        "新出现"的判定只依赖设备名（COMx），不依赖 USB 口位置：
        换 USB 口时 Windows 可能给新 COM 号，那就算一块新板，序号往后排 ——
        这正是"插入先后"的语义。
        """
        seen = self.data['seen']
        known = [int(v.get('order') or 0) for v in seen.values()]
        nxt = (max(known) if known else 0) + 1
        fresh = []
        now = time.time()
        for p in sorted(ports, key=lambda q: com_number(q['device'])):
            dev = p['device']
            if dev in seen:
                continue
            seen[dev] = {
                'order': nxt,
                'first_seen': now,
                'first_seen_human': datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S'),
                'origin': 'first-scan' if not known else 'hot-plug',
            }
            fresh.append((dev, nxt))
            nxt += 1
        if fresh:
            self.save()
            for dev, n in fresh:
                log('发现串口 %s → 插入序 %d' % (dev, n))
        return fresh

    def reset_order(self, ports):
        """清空序号与手动分配，把**当前在线**的串口按 COM 号重排 1..N。

        用于"刚才分错了 / 想严格按先插水平再插竖直走一遍"：
        先点这个按钮（此刻在线的会被当作已插好按 COM 号排），
        或者先拔掉两块再点，然后按你要的顺序插。
        """
        with self.lock:
            now = time.time()
            flags = dict(self.data.get('flags') or {'lr_flip': False})
            self.data = {'seen': {}, 'roles': {}, 'flags': flags}
            ordered = sorted(ports, key=lambda q: com_number(q['device']))
            for i, p in enumerate(ordered, 1):
                self.data['seen'][p['device']] = {
                    'order': i,
                    'first_seen': now,
                    'first_seen_human': datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S'),
                    'origin': 'reset',
                }
            self.save()

    # ---- 角色
    def effective_roles(self):
        """手动分配优先；没指定的按插入序补空位。返回 ({dev: role}, warnings)。"""
        seen = self.data['seen']
        manual = self.data['roles']
        online = [p['device'] for p in self.ports]
        warns = []

        roles = {}
        for dev in online:
            r = manual.get(dev)
            if r in ('lr', 'ud'):
                roles[dev] = r

        # 同一角色被手动指给了两块：保留插入序靠前的那块，另一块置空
        for role in ('lr', 'ud'):
            holders = [d for d, r in roles.items() if r == role]
            if len(holders) > 1:
                holders.sort(key=lambda d: self.order_of(d))
                keep = holders[0]
                for d in holders[1:]:
                    roles.pop(d, None)
                    self.data['roles'][d] = 'none'
                warns.append('%s 被指给了 %d 块板（%s），只保留插入序最靠前的 %s'
                             % (ROLE_LABEL[role], len(holders),
                                '、'.join(holders), keep))
                self.save()

        free = [r for r in ('lr', 'ud') if r not in roles.values()]
        # 明确选了"不参与"的板不占名额，也不参与补位
        rest = [d for d in online
                if d not in roles and manual.get(d) != 'none']
        rest.sort(key=lambda d: self.order_of(d))
        for dev in rest:
            if free:
                roles[dev] = free.pop(0)
        return roles, warns

    def order_of(self, dev):
        return int((self.data['seen'].get(dev) or {}).get('order') or 10 ** 6)

    # ---- 快照
    def snapshot(self):
        with self.lock:
            ports, err = list_ports()
            self.ports = ports
            self.absorb(ports)
            roles, warns = self.effective_roles()
            self.warnings = ([err] if err else []) + warns

            items = []
            for p in sorted(ports, key=lambda q: self.order_of(q['device'])):
                dev = p['device']
                info = self.data['seen'].get(dev) or {}
                role = roles.get(dev, 'none')
                b = self.bridges.get(role)
                items.append({
                    'device': dev,
                    'description': p['description'],
                    'location': p['location'],
                    'serial': p['serial'],
                    'hwid': p['hwid'],
                    'radar_like': is_radar_like(p),
                    'order': info.get('order'),
                    'first_seen': info.get('first_seen_human'),
                    'origin': info.get('origin'),
                    'role': role,
                    'manual': self.data['roles'].get(dev),
                    'bridge': ({
                        'alive': b.alive(),
                        'pid': b.pid(),
                        'ws_port': b.ws_port,
                        'log': os.path.relpath(b.log, ROOT),
                        'log_tail': b.tail(),
                    } if b else None),
                })

            offline = []
            for dev, info in self.data['seen'].items():
                if dev not in {p['device'] for p in ports}:
                    offline.append({'device': dev, 'order': info.get('order'),
                                    'first_seen': info.get('first_seen_human')})

            bridges = {}
            for role in ('lr', 'ud'):
                b = self.bridges.get(role)
                item = {'role': role, 'label': ROLE_LABEL[role],
                        'ws_port': ROLE_WS_PORT[role], 'running': False}
                if b is not None:
                    item.update({
                        'running': b.alive(),
                        'pid': b.pid(),
                        'device': b.device,
                        'alive': b.alive(),
                        'ws_port': b.ws_port,
                        'ws': 'ws://127.0.0.1:%d' % b.ws_port,
                        'listening': port_open(b.ws_port, 0.15),
                        'log': os.path.relpath(b.log, ROOT),
                        'log_tail': b.tail(),
                        'hb': b.heartbeat(),
                    })
                bridges[role] = item

            return {
                'ok': True,
                'root': ROOT,
                'ports': items,
                'offline': sorted(offline, key=lambda q: q.get('order') or 0),
                'roles': roles,
                'bridges': bridges,
                'warnings': self.warnings,
                'flags': dict(self.data.get('flags') or {'lr_flip': False}),
                'defaults_rule': '插入序 1 → 水平（左右），插入序 2 → 竖直（跳蹲）',
                'board_note': '两块都是 1T2R（VID:PID 相同），只能按插入顺序或手动指定区分',
            }

    # ---- 桥接进程
    def start_all(self):
        with self.lock:
            ports, err = list_ports()
            self.ports = ports
            self.absorb(ports)
            roles, _warns = self.effective_roles()

            # 先停旧的，避免端口/串口占用
            self.stop_all()

            problems = []
            started = []
            lr_flip = bool((self.data.get('flags') or {}).get('lr_flip'))
            for role in ('lr', 'ud'):
                devs = [d for d, r in roles.items() if r == role]
                if not devs:
                    continue
                dev = devs[0]
                bus = [p['device'] for p in ports]
                if dev not in bus:
                    problems.append('%s 已分配但当前不在线（%s）' % (ROLE_LABEL[role], dev))
                    continue
                ok, note = reclaim_port(role)
                if not ok:
                    problems.append(note)
                    continue
                b = Bridge(role, dev, lr_flip=lr_flip)
                try:
                    b.start()
                    self.bridges[role] = b
                    started.append(b)
                    log('启动 %s：%s → ws://127.0.0.1:%d（pid %s）'
                        % (ROLE_LABEL[role], dev, b.ws_port, b.proc.pid))
                except Exception as e:
                    problems.append('%s 启动失败：%r' % (ROLE_LABEL[role], e))

            # 等 ws 端口真正在听（桥接器先起 server 再开串口，通常 0.3 s 内）
            deadline = time.time() + 8.0
            while time.time() < deadline:
                if all(port_open(b.ws_port, 0.2) for b in started):
                    break
                time.sleep(0.15)
            notup = [b for b in started if not port_open(b.ws_port, 0.2)]
            if not started and not problems:
                problems.append('当前没有可分到角色的雷达：先插上两块板（至少一块分给「水平」），'
                                '页面每 3 秒会自动刷新串口列表')

            return {
                'ok': not problems and not notup,
                'started': [{'role': b.role, 'device': b.device,
                             'ws_port': b.ws_port, 'pid': b.pid()} for b in started],
                'problems': problems + ['%s 端口 %d 8 秒内没起来，看日志'
                                        % (ROLE_LABEL[b.role], b.ws_port) for b in notup],
                'state': self.snapshot(),
            }

    def stop_all(self):
        with self.lock:
            stopped = []
            for role in ('lr', 'ud'):
                b = self.bridges.get(role)
                if b is not None:
                    if b.alive():
                        stopped.append(role)
                    b.stop()
                    self.bridges[role] = None
            # 分配台被强关过时，上一轮起的桥接会变成孤儿挂在 8766/8767 上，
            # 这里顺手回收，免得下次点「启动」报端口被占用。
            for role in ('lr', 'ud'):
                if not port_open(ROLE_WS_PORT[role], 0.2):
                    continue
                ok, note = reclaim_port(role)
                if ok:
                    stopped.append('%s(旧进程)' % role)
                else:
                    log('!! %s' % note)
            return stopped

    def flip_lr(self, value=None):
        """左右镜像开关。翻转后立刻重启水平桥接，新参数才生效。

        value=None → 切换（点一次翻一次）；True/False → 设为指定值。
        """
        with self.lock:
            flags = self.data.setdefault('flags', {'lr_flip': False})
            on = (not flags.get('lr_flip')) if value is None else bool(value)
            flags['lr_flip'] = on
            self.save()

            restarted = False
            note = ''
            b = self.bridges.get('lr')
            if b is not None and b.alive():
                dev = b.device
                b.stop()
                self.bridges['lr'] = None
                ok, why = reclaim_port('lr', quiet=True)
                if not ok:
                    note = why
                else:
                    try:
                        nb = Bridge('lr', dev, lr_flip=on)
                        nb.start()
                        self.bridges['lr'] = nb
                        restarted = True
                    except Exception as e:
                        note = '重启水平桥接失败：%r' % (e,)

            log('左右镜像 → %s%s' % (
                '开（雷达左 = 游戏右）' if on else '关（雷达左 = 游戏左）',
                '，已重启水平桥接' if restarted else '，下次启动生效'))
            return {'ok': True, 'lr_flip': on, 'restarted': restarted,
                    'note': note, 'state': self.snapshot()}


class Bridge(object):
    """一个 radar_live_ws.py 子进程。"""

    def __init__(self, role, device, lr_flip=False):
        self.role = role
        self.device = device
        self.lr_flip = bool(lr_flip)
        self.ws_port = ROLE_WS_PORT[role]
        self.log = os.path.join(TMP, 'radar-%s.log' % role)
        self.pidfile = bridge_pidfile(role)
        self.proc = None
        self.started_at = None

    def pid(self):
        return self.proc.pid if self.proc is not None else None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def command(self):
        args = [sys.executable, '-u', BRIDGE,
                '--port', self.device,
                '--role', self.role,
                '--ws-port', str(self.ws_port),
                '--pidfile', self.pidfile,
                '--idle-exit', '60',
                '--lonely-exit', '300',
                '--quiet']
        args += LR_ARGS if self.role == 'lr' else UD_ARGS
        if self.role == 'lr' and self.lr_flip:
            args.append('--lr-flip')
        return args

    def start(self):
        os.makedirs(TMP, exist_ok=True)
        # 每次启动截断日志：只保留本次运行，现场排错看的就是这一次
        self.fp = open(self.log, 'wb')
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self.proc = subprocess.Popen(self.command(), cwd=ROOT,
                                     stdout=self.fp, stderr=subprocess.STDOUT, **kw)
        self.started_at = time.time()
        try:
            self.fp.close()      # 子进程已继承句柄；父端不留
        except Exception:
            pass

    def stop(self):
        p = self.proc
        self.proc = None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        try:
            os.remove(self.pidfile)
        except Exception:
            pass

    def tail(self, lines=8):
        try:
            with open(self.log, 'rb') as f:
                data = f.read()[-4096:]
            text = data.decode('utf-8', 'replace')
            rows = [r for r in text.splitlines() if r.strip()]
            return '\n'.join(rows[-lines:])
        except Exception:
            return ''

    def heartbeat(self):
        """读桥接写的心跳（每 2 s）：帧数 + 多久没更新。

        「ws 连上了但一帧都没有」在日志里几乎没有痕迹（--quiet 关了逐帧日志），
        所以让桥接把帧数写进文件，分配台据此判断是真在出数据还是空转。
        """
        path = (self.pidfile + '.hb') if self.pidfile else None
        if not path:
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                hb = json.load(f)
        except Exception:
            return None
        try:
            hb['age'] = round(time.time() - float(hb.get('t') or 0), 1)
        except Exception:
            hb['age'] = None
        return hb


def port_open(port, timeout=0.2):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------ 旧桥接回收
def bridge_pidfile(role):
    """桥接自己写的 pid 文件（和双雷达分配台约定好，别改路径）。"""
    return os.path.join(TMP, 'radar-%s.pid' % role)


def read_pidfile(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return int(f.read().strip())
    except Exception:
        return None


def pid_alive(pid):
    """进程还在不在（Windows 用 OpenProcess + STILL_ACTIVE，不依赖 psutil）。"""
    if not pid:
        return False
    if os.name != 'nt':
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259                       # STILL_ACTIVE
    except Exception:
        return False


def kill_pid(pid, wait=4.0):
    if not pid:
        return False
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(int(pid), 15)
    except Exception:
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def listener_pid(port):
    """谁在 LISTENING 这个端口（解析 netstat -ano）。"""
    if os.name != 'nt':
        return None
    try:
        raw = subprocess.run(['netstat', '-ano', '-p', 'TCP'],
                            capture_output=True, timeout=12).stdout
    except Exception:
        return None
    want = ':%d' % port
    for line in raw.decode('ascii', 'replace').splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == 'LISTENING' and parts[1].endswith(want):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def process_cmdline(pid):
    """取进程命令行：用来确认占用端口的到底是不是我们自己的雷达桥接。"""
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command',
             '(Get-CimInstance Win32_Process -Filter "ProcessId=%d").CommandLine' % int(pid)],
            capture_output=True, timeout=20).stdout
    except Exception:
        return ''
    for enc in ('utf-8', 'gbk'):
        try:
            return out.decode(enc).strip()
        except Exception:
            continue
    return out.decode('utf-8', 'replace').strip()


def reclaim_port(role, quiet=False):
    """端口被占时先判断占用者是不是本项目上一次留下的桥接，是就结束它。

    返回 (ok, note)：ok=True 表示端口现在可用（本来就空闲，或已回收）。
    只杀「pid 文件记录的一致」或「命令行里是 radar_live_ws.py」的进程，
    别的程序占用照实报错，不误杀。
    """
    port = ROLE_WS_PORT[role]
    pidfile = bridge_pidfile(role)
    if not port_open(port, 0.2):
        return True, ''
    owner = listener_pid(port)
    recorded = read_pidfile(pidfile)
    target, why = None, ''
    if recorded and owner and recorded == owner and pid_alive(recorded):
        target, why = owner, 'pid 文件对上'
    elif owner:
        cmd = process_cmdline(owner)
        if 'radar_live_ws' in cmd:
            target, why = owner, '命令行含 radar_live_ws'
    if target is None:
        return False, ('端口 %d 被 pid %s 占用，不是本项目的桥接；'
                       '关掉它（taskkill /F /PID %s）或换 --ws-port'
                       % (port, owner or '?', owner or '?'))
    if not quiet:
        log('端口 %d 被旧桥接占用（%s，pid %d）→ 自动结束并回收' % (port, why, target))
    kill_pid(target)
    for _ in range(40):
        if not port_open(port, 0.2):
            break
        time.sleep(0.1)
    try:
        os.remove(pidfile)
    except Exception:
        pass
    if port_open(port, 0.2):
        return False, '端口 %d 结束 pid %d 后仍未释放（可能还有别的监听者）' % (port, target)
    return True, ''


def is_our_manager(port):
    """8778 上跑的是不是另一个本服务的实例。"""
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:%d/api/ping' % port, timeout=1.5) as r:
            data = json.loads(r.read().decode('utf-8'))
        return data.get('service') == 'dual-radar-manager'
    except Exception:
        return False


# ------------------------------------------------------------------ HTTP
def make_handler(reg, http_port):
    import http.server

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=GAME, **kw)

        def log_message(self, fmt, *args):
            pass

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            super().end_headers()

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            try:
                if path == '/' or path == '':
                    # 直接访问根地址先看联调台（游戏是 /index.html）
                    self.send_response(302)
                    self.send_header('Location', '/' + PAGE)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if path in ('/api/state', '/api/radars'):
                    return self._json(reg.snapshot())
                if path == '/api/ping':
                    return self._json({'ok': True, 'service': 'dual-radar-manager',
                                       'port': http_port,
                                       'page': 'http://127.0.0.1:%d/%s' % (http_port, PAGE)})
                if path in ('/api/log'):
                    # /api/log?role=lr&lines=40
                    from urllib.parse import urlparse, parse_qs
                    q = parse_qs(urlparse(self.path).query)
                    role = (q.get('role') or ['lr'])[0]
                    n = int((q.get('lines') or ['40'])[0])
                    b = reg.bridges.get(role)
                    return self._json({'ok': True, 'role': role,
                                       'log': (b.tail(n) if b else '')})
            except Exception as e:
                return self._json({'ok': False, 'error': repr(e)}, 500)
            return super().do_GET()

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b'{}'
            try:
                body = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                return self._json({'ok': False, 'error': 'body 不是 JSON'}, 400)
            try:
                if path == '/api/assign':
                    return self._json(api_assign(reg, body))
                if path == '/api/reset':
                    ports, err = list_ports()
                    reg.ports = ports
                    reg.reset_order(ports)
                    if err:
                        reg.warnings = [err]
                    log('已按当前 COM 号重排插入序')
                    return self._json({'ok': True, 'state': reg.snapshot()})
                if path == '/api/start':
                    res = reg.start_all()
                    return self._json(res, 200 if res.get('ok') else 409)
                if path == '/api/stop':
                    stopped = reg.stop_all()
                    log('已停止桥接：%s' % (', '.join(stopped) or '无'))
                    return self._json({'ok': True, 'stopped': stopped, 'state': reg.snapshot()})
                if path == '/api/flip-lr':
                    # 不传 lr_flip 就是「翻一次」；传 true/false 是直接设定
                    val = body.get('lr_flip') if 'lr_flip' in body else None
                    res = reg.flip_lr(val)
                    return self._json(res)
            except Exception as e:
                return self._json({'ok': False, 'error': repr(e)}, 500)
            return self._json({'ok': False, 'error': '未知接口 %s' % path}, 404)

    return Handler


def api_assign(reg, body):
    with reg.lock:
        pairs = {}
        dev = body.get('device')
        if dev:
            pairs[dev] = body.get('role')
        for d, r in (body.get('roles') or {}).items():
            pairs[d] = r
        if not pairs:
            return {'ok': False, 'error': '没给 device/role'}
        for d, r in pairs.items():
            if r not in ('lr', 'ud', 'none'):
                return {'ok': False, 'error': '%s 的角色只能是 lr / ud / none' % d}
            reg.data['roles'][d] = r
        reg.save()
        log('手动分配：%s' % '，'.join('%s=%s' % (d, ROLE_SHORT.get(r, r))
                                      for d, r in pairs.items()))
        return {'ok': True, 'state': reg.snapshot()}


def serve(reg, http_port):
    import http.server
    import socketserver

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    handler = make_handler(reg, http_port)
    try:
        httpd = Server(('127.0.0.1', http_port), handler)
    except OSError as e:
        log('!! 端口 %d 起不来：%s' % (http_port, e))
        log('   换个端口：python tools/dual_radar_manager.py --port 8779')
        return None
    log('雷达分配台：http://127.0.0.1:%d/%s' % (http_port, PAGE))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reg.stop_all()
        httpd.server_close()
    return httpd


def main():
    setup_console()
    ap = argparse.ArgumentParser(description='双雷达角色分配服务（插入顺序 + 手动指定）')
    ap.add_argument('--port', type=int, default=DEFAULT_HTTP_PORT, help='HTTP 端口，默认 8778')
    ap.add_argument('--no-open', action='store_true', help='不自动打开联调台')
    args = ap.parse_args()

    if not os.path.isfile(BRIDGE):
        log('!! 找不到桥接器 %s' % BRIDGE)
        return 2

    reg = Registry()
    ports, err = list_ports()
    reg.ports = ports
    reg.absorb(ports)
    if err:
        log('!! %s' % err)

    page = 'http://127.0.0.1:%d/%s' % (args.port, PAGE)
    if port_open(args.port, 0.4):
        if is_our_manager(args.port):
            log('分配台已经在本机 %d 上跑着 → 直接打开页面，不重复启动' % args.port)
            if not args.no_open:
                try:
                    webbrowser.open(page)
                except Exception:
                    pass
            return 0
        log('!! 端口 %d 被别的程序占用；换个端口：'
            'python tools/dual_radar_manager.py --port 8779' % args.port)
        return 3
    print('=' * 62, flush=True)
    print(' 双雷达角色分配（两块 1T2R 只能按插入顺序区分）', flush=True)
    print(' 默认：先插入 = 水平/左右，后插入 = 竖直/跳蹲', flush=True)
    print(' 页面：%s' % page, flush=True)
    print(' 分配状态：%s' % os.path.relpath(STATE_PATH, ROOT), flush=True)
    print('-' * 62, flush=True)
    print(' 现场摆法（跳蹲判据来自 2026-11-13 四批 96 样本，85.4%）：', flush=True)
    print('   竖直板用 1T2R（判据是在 1T2R 竖放上采的，2T4R 距离带不同）', flush=True)
    print('   水平板  横放，人站正前方约 1 m', flush=True)
    print('   竖直板  绕光轴转 90 度竖放，离地约 0.5 m，人站约 1.2 m', flush=True)
    print('   两块都别正对显示器/机箱（静止强反射会抢走目标）', flush=True)
    print('-' * 62, flush=True)
    print(' 页面上点「启动并回游戏主页面」= 起两路桥接 + 回到主菜单', flush=True)
    print(' 停止：Ctrl+C；关闭页面 60 秒后桥接自动退出并释放端口', flush=True)
    print('=' * 62, flush=True)

    if not args.no_open:
        def open_later():
            for _ in range(40):
                if port_open(args.port, 0.2):
                    break
                time.sleep(0.15)
            try:
                webbrowser.open(page)
            except Exception:
                pass
        threading.Thread(target=open_later, daemon=True).start()

    serve(reg, args.port)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
