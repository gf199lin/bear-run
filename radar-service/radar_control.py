# -*- coding: utf-8 -*-
"""
采集会话控制：**开始 / 结束 / 重置**（软件层）。

为什么需要它
- 原来 `server.py` 一启动就永久采集，只能 Ctrl+C 结束整个服务：现场没法「用完就停」，
  串口被长期占用，第二个实例或厂商工具打开 COM3 会失败（对应进度文档里的问题 C）。

三层语义（**不要混为一谈**）
- 层 1「结束」= 本模块：停采集线程 → **释放串口** → 停止解算与推送。
  可离线验证、无风险。
- 层 2「硬件停机」= `radar_hwstop.py`：写雷达停机寄存器，芯片才停止发射、温度才降。
  **未在实机验证，默认关闭**。
- 层 3「重置」= 清零统计与画面，不改变运行状态。

**诚实边界（界面提示必须与本条一致）**
软件层「结束」**不会降低雷达芯片发热**：芯片仍在持续发射调频连续波，
主机停读只是不再接收数据。真正降温只能靠层 2。

线程模型
- 每次 `start()` 都用 `source_factory()` 新建数据源，并新建解析器/拼帧器
  （`build_pipeline` 内部创建），避免上一次会话的半包/半帧残留污染新会话。
- 一个常驻守护线程负责自愈：采集线程意外退出、或长时间收不到帧（停滞）时，
  在**同一会话内**重建管道（`restarts` 计数），保证长时间演示不中断。
"""
import threading
import time

from radar_source import build_pipeline

HEAT_NOTE = ('软件「结束」只释放串口并停止解算/渲染，雷达芯片仍在发射，温度不会下降；'
             '真正降温需硬件停机（实验，未在实机验证）。')


class RadarController:
    """把「一次采集会话」封装成可反复 开始/结束/重置 的控制器。"""

    def __init__(self, source_factory, on_frame, record_path=None, log=None,
                 stall_timeout=8.0, auto_restart=True):
        """
        source_factory: 无参函数，返回一个新的数据源实例（每次 start 都会调用）
        on_frame:       回调 on_frame(z0, z1, stats)，由采集线程调用
        stall_timeout:  超过该秒数收不到帧即判定停滞并重建管道；<=0 关闭停滞判定
        """
        self._make_source = source_factory
        self._on_frame_user = on_frame
        self._record_path = record_path
        self._log = log if log is not None else (lambda *a: None)
        self._stall_timeout = float(stall_timeout)
        self._auto_restart = bool(auto_restart)

        self._lock = threading.RLock()        # 生命周期
        self._stat_lock = threading.Lock()    # 计数（采集线程高频进入，必须短）

        self._stop = None
        self._thread = None
        self._source = None
        self._active = False
        self._t_session = None
        self._t_last_frame = None
        self._frames = 0
        self._starts = 0
        self._stops = 0
        self._active_seconds = 0.0
        self._last_stop_reason = None
        self._record_opened_once = False
        self._on_change = None

        self.sessions = 0
        self.restarts = 0

        self._supervisor = threading.Thread(target=self._supervise, daemon=True,
                                            name='radar-supervisor')
        self._supervisor.start()

    # ------------------------------------------------------------------ 对外
    def set_on_change(self, fn):
        """状态变化回调，用于让 HTTP 层广播控制状态。"""
        self._on_change = fn

    def start(self, record_path=None):
        with self._lock:
            if self._active:
                return dict(ok=False, msg='已在采集中', **self.status())
            if record_path is not None:
                self._record_path = record_path
            self._active = True
            self._t_session = time.time()
            self._starts += 1
            self.sessions += 1
            self._frames = 0
            self._last_stop_reason = None
            self._spawn_locked(append=self._record_opened_once)
            self._record_opened_once = True
            st = self.status()
            self._log('[control] 开始采集 #%d · 源 %s' % (self.sessions, st['source']))
        self._notify()
        return dict(ok=True, **st)

    def stop(self, reason='user'):
        with self._lock:
            if not self._active:
                return dict(ok=False, msg='当前未在采集', **self.status())
            self._finish_locked(reason)
            st = self.status()
        self._notify()
        return dict(ok=True, **st)

    def reset(self):
        """清零统计与画面。运行中重置＝重新开始计时（会话编号不变）。"""
        with self._lock:
            with self._stat_lock:
                self._frames = 0
                self._t_last_frame = time.time()
            self._active_seconds = 0.0
            if self._active:
                self._t_session = time.time()
            else:
                self._t_session = None
                self.sessions = 0
            self.restarts = 0
            st = self.status()
        self._notify()
        return dict(ok=True, **st)

    def control(self, action, **kw):
        action = (action or '').strip().lower()
        if action in ('start', 'begin', 'on'):
            return self.start(**kw)
        if action in ('stop', 'end', 'off'):
            return self.stop(reason=kw.get('reason', 'user'))
        if action in ('reset', 'clear'):
            return self.reset()
        if action == 'status':
            return dict(ok=True, **self.status())
        return dict(ok=False, msg='未知动作: %r（支持 start/stop/reset）' % action,
                    **self.status())

    def switch_source(self, apply_fn=None, reason='switch'):
        """运行中热切换数据源：停当前源（释放串口）→ apply_fn() 改模式 → 按原状态重启。

        用途：现场演示不重启进程、不刷新页面，就能在「真雷达 / 回放 / 合成」之间
        切换（AGENTS.md 硬约束 2：一个热键 1 秒内切换）。耗时用单调钟实测并回传，
        供「1 秒内切换」验收判定，而非靠日志观感。

        apply_fn 传 None 表示调用方已先自行改好源模式（本仓库 server.py 即此型）；
        未在采集时只切模式、不启停，便于「开采集之前先选好源」。
        """
        t0 = time.perf_counter()
        with self._lock:
            was_active = self._active
            ok, msg = True, None
            try:
                if was_active:
                    self._finish_locked(reason)
                if apply_fn is not None:
                    apply_fn()
                if was_active:
                    # 与 start() 内部等价，但在同一把锁内完成，避免出现「已停未起」的
                    # 中间态被 /status 或 supervisor 看到。
                    self._active = True
                    self._t_session = time.time()
                    self._starts += 1
                    self.sessions += 1
                    self._frames = 0
                    self._last_stop_reason = None
                    self._spawn_locked(append=self._record_opened_once)
                    self._record_opened_once = True
            except (Exception, SystemExit) as e:
                # 新源建不起来（如串口没了/回放文件缺失）时不要留下「半开」状态：
                # 回到已停止，把原因如实回传给调用方。
                self._active = False
                self._t_session = None
                self._kill_locked()
                ok, msg = False, '%s: %s' % (type(e).__name__, e)
            elapsed = round((time.perf_counter() - t0) * 1000.0, 1)
            st = self.status()
        self._log('[control] 热切换数据源（%.0f ms）%s%s'
                  % (elapsed,
                     '' if was_active else ' · 未在采集，仅切模式',
                     '' if ok else ' · 失败: ' + msg))
        self._notify()
        return dict(ok=ok, msg=msg, switch_ms=elapsed,
                    was_active=was_active, **st)

    def status(self):
        now = time.time()
        with self._stat_lock:
            frames = self._frames
            last = self._t_last_frame
        elapsed = (now - self._t_session) if (self._active and self._t_session) else 0.0
        total = self._active_seconds + (elapsed if self._active else 0.0)
        stall = round(now - last, 1) if last is not None else None
        return {
            'active': bool(self._active),
            'sessions': self.sessions,
            'starts': self._starts,
            'stops': self._stops,
            'frames': frames,
            'elapsed': round(elapsed, 1),
            'active_seconds': round(total, 1),
            'since_frame': stall,
            'restarts': self.restarts,
            'source': self._source.name if self._source is not None else None,
            'src_diag': (self._source.diag()
                         if (self._source is not None and hasattr(self._source, 'diag'))
                         else None),
            'stop_reason': self._last_stop_reason,
            'heat_note': HEAT_NOTE,
        }

    def shutdown(self):
        self.stop(reason='shutdown')

    # ------------------------------------------------------------- 内部实现
    def _spawn_locked(self, append=True):
        """（重新）建立采集管道；调用方须持有 _lock。"""
        self._kill_locked()
        stop = threading.Event()
        src = self._make_source()
        self._stop = stop
        self._source = src
        self._t_last_frame = time.time()
        t = threading.Thread(target=self._run, args=(src, stop, self._record_path, append),
                             daemon=True, name='radar-pipeline')
        self._thread = t
        t.start()

    def _kill_locked(self):
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._stop = None

    def _run(self, src, stop, rec, append):
        def cb(z0, z1, stats):
            with self._stat_lock:
                self._frames += 1
                self._t_last_frame = time.time()
            self._on_frame_user(z0, z1, stats)

        try:
            build_pipeline(src, cb, stop, rec, record_append=append)
        except Exception as e:                      # 管道自身异常不应带崩服务
            self._log('[control] 管道异常退出: %s: %s' % (type(e).__name__, e))

    def _finish_locked(self, reason):
        self._active = False
        self._stops += 1
        self._last_stop_reason = reason
        if self._t_session is not None:
            self._active_seconds += time.time() - self._t_session
            self._t_session = None
        self._kill_locked()
        if self._source is not None:
            try:
                self._source.close()                # 关键：释放串口，COM3 立即可用
            except Exception:
                pass
        self._log('[control] 结束采集（%s）· 串口已释放 · %s' % (reason, HEAT_NOTE))

    def _supervise(self):
        while True:
            time.sleep(0.5)
            try:
                with self._lock:
                    if not self._active:
                        continue
                    dead = self._thread is None or not self._thread.is_alive()
                    with self._stat_lock:
                        last = self._t_last_frame
                    stalled = (self._stall_timeout > 0 and last is not None
                               and (time.time() - last) > self._stall_timeout)
                    if not dead and not stalled:
                        continue
                    if not self._auto_restart:
                        if dead:
                            self._finish_locked('thread-died')
                        continue
                    why = '采集线程退出' if dead else '停滞 %.0fs 无帧' % (time.time() - last)
                    self.restarts += 1
                    self._log('[control] 管道异常（%s），重建中（第 %d 次）…'
                              % (why, self.restarts))
                    self._spawn_locked(append=True)
            except Exception as e:                  # 自愈线程绝不允许退出
                print('[control] supervisor 异常: %s: %s' % (type(e).__name__, e),
                      flush=True)

    def _notify(self):
        fn = self._on_change
        if fn is None:
            return
        try:
            fn(self.status())
        except Exception:
            pass
