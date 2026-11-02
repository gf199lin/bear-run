# -*- coding: utf-8 -*-
"""2T4R 跳 / 蹲识别 → 游戏 WebSocket 桥（role=ud，默认端口 8767）。

它和 `radar_live_ws.py --role ud` 的区别
----------------------------------------
`radar_live_ws.py` 走 `build_pipeline`，而 2T4R 的帧装配器返回的是
(4 RX × chirps × bins)，`build_pipeline` 只把前两个 RX 传进回调 ——
拿不到完整俯仰阵列。本脚本自己读帧，保留完整 4 元数据，
所以以后可以平滑升级到 4 元 DBF 俯仰谱。

输出帧（与游戏端 InputSource.js 的约定一致）
-------------------------------------------
    {"ts":..., "x":0, "height":0, "jump":0|1, "crouch":0|1,
     "confidence":0..1, "raw":{...}}

判定分两层（这是刻意的）：
  1. **有没有动作** ← 窗内偏离 >= min_dev（规则，可解释）
  2. **跳还是蹲**   ← models/jumpduck.json（模型，可替换）

用法
----
    # 真机：2T4R 管跳蹲，游戏另开 8766 管左右
    python tools\\jumpduck_ws.py --port COM4 --ws-port 8767

    # 没有板子：回放历史原始流，验证管线是否通
    python tools\\jumpduck_ws.py --replay .deepworks\\tmp\\seg-jd2\\seg_01.00.bin --board 1t2r

    # 只做状态机自测（合成波形，不碰串口）
    python tools\\jumpduck_ws.py --selftest

    # 不用模型，只跑规则兜底（链路自测）
    python tools\\jumpduck_ws.py --port COM4 --no-model

⚠ 模型指标见 models/jumpduck.json 的 metrics 与 docs/jump-duck-data-audit.md。
   当前数据训出的方向准确率 ≈ 随机，**只能用于跑通链路，不能作为验收证据**。
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJECT = ROOT
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PROJECT, "radar-pointcloud"))
sys.path.insert(0, os.path.join(PROJECT, "dual-radar-kit", "tools"))

from jumpduck_core import (DEFAULT_MIN_DEV, EXPECTED_SPEC,  # noqa: E402
                           JumpDuckRecognizer, PriorJudge, SoftmaxModel,
                           pitch_from_frame)
from radar_source import SerialSource, make_protocol           # noqa: E402

# ws 服务端复用左右桥的实现，避免两份握手/分帧代码漂移
from radar_live_ws import Hub, start_server                    # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ------------------------------------------------------------------ 单帧

class Recognizer(object):
    """把一帧原始复数据变成 ws 帧。"""

    def __init__(self, det, rx_a, rx_b, band_lo, band_hi, on_frame=None):
        self.det = det
        self.rx_a = rx_a
        self.rx_b = rx_b
        self.band_lo = band_lo
        self.band_hi = band_hi
        self.count = 0
        self.last_pitch = None
        self.on_frame = on_frame
        self.events = {"JUMP": 0, "DUCK": 0, "NONE": 0}

    def feed(self, z):
        z = asarray_rx(z)
        if z is None:
            return None
        pitch = pitch_from_frame(z, self.band_lo, self.band_hi,
                                 self.rx_a, self.rx_b)
        if pitch is None or not np.isfinite(pitch):
            return None
        self.last_pitch = float(pitch)
        self.count += 1
        ev = self.det.update(pitch)
        if ev is None:
            return None

        state = ev["state"]
        self.events[state] = self.events.get(state, 0) + 1

        # 置信度：模型给出的最大类概率；NONE 帧压到 0.3 以下，
        # 游戏端 R.minConfidence=0.3 会直接丢掉（避免抖动污染上升沿）
        conf = 0.35
        prob = ev.get("prob")
        if prob:
            conf = float(max(prob.values()))
        if state == "NONE":
            conf = min(conf, 0.29)
        conf = max(0.0, min(1.0, conf))

        frame = {
            "ts": int(time.time() * 1000),
            "x": 0.0,
            "height": 0.0,
            "jump": 1.0 if state == "JUMP" else 0.0,
            "crouch": 1.0 if state == "DUCK" else 0.0,
            "confidence": round(conf, 3),
            "raw": {
                "src": "radar",
                "board": "2t4r",
                "role": "ud",
                "pitch": round(self.last_pitch, 2),
                "baseline": (round(ev["baseline"], 2)
                             if ev.get("baseline") is not None else None),
                "hi": round(ev["features"]["hi"], 2),
                "lo": round(ev["features"]["lo"], 2),
                "moving": bool(ev.get("moving")),
                "reason": ev.get("reason"),
                "prob": ({k: round(v, 3) for k, v in prob.items()}
                         if prob and isinstance(prob, dict) and
                         all(isinstance(x, (int, float)) for x in prob.values())
                         else None),
                "frames": self.count,
            },
        }
        if self.on_frame:
            self.on_frame(frame)
        return frame


def asarray_rx(z):
    """把装配器输出统一成 (rx, chirps, bins)。"""
    if z is None:
        return None
    if isinstance(z, tuple):
        arrs = [np.asarray(v) for v in z if v is not None]
        if not arrs:
            return None
        try:
            z = np.stack(arrs, axis=0)
        except ValueError:
            return None
    else:
        z = np.asarray(z)
    if z.ndim != 3:
        return None
    return z


# ------------------------------------------------------------------ 输入源

def run_serial(args, rec, hub):
    src = SerialSource(args.port, baud=None, retry_delay=1.0)
    src.open()
    log("串口已打开：%s（板型 %s）" % (getattr(src, "name", args.port),
                                    getattr(src, "board", "?")))
    parser = asm = cur = None
    while True:
        want = args.board or getattr(src, "board", None) or "2t4r"
        if want != cur:
            parser, asm = make_protocol(want, chirps=args.chirps)
            cur = want
            log("协议解析器 → %s（chirps=%s）" % (want, args.chirps or "按 profile"))
        chunk = src.read(0.05)
        if not chunk:
            continue
        for rf in parser.feed(chunk):
            z = asm.push(rf)
            if z is None:
                continue
            frame = rec.feed(z)
            if frame is not None:
                hub.broadcast(json.dumps(frame, ensure_ascii=False))
                if not args.quiet:
                    r = frame["raw"]
                    log("帧%d pitch=%s base=%s hi=%+.1f lo=%+.1f → %s %s"
                        % (r["frames"], r["pitch"], r["baseline"], r["hi"], r["lo"],
                           "JUMP" if frame["jump"] else
                           ("DUCK" if frame["crouch"] else "-"),
                           ("p=%s" % r["prob"]) if r["prob"] else ""))


def run_replay(args, rec, hub):
    path = args.replay
    if not os.path.isfile(path):
        log("回放文件不存在：%s" % path)
        return 2
    board = args.board or "2t4r"
    parser, asm = make_protocol(board, chirps=args.chirps)
    data = open(path, "rb").read()
    log("回放 %s（%.2f MB，板型 %s，chirps=%s）"
        % (path, len(data) / 1048576.0, board, args.chirps or "按 profile"))

    step = 65536
    for i in range(0, len(data), step):
        for rf in parser.feed(data[i:i + step]):
            z = asm.push(rf)
            if z is None:
                continue
            frame = rec.feed(z)
            if frame is not None:
                hub.broadcast(json.dumps(frame, ensure_ascii=False))
                if not args.quiet and (frame["jump"] or frame["crouch"]):
                    r = frame["raw"]
                    log("帧%d pitch=%s base=%s → %s %s"
                        % (r["frames"], r["pitch"], r["baseline"],
                           "JUMP" if frame["jump"] else "DUCK",
                           ("p=%s" % r["prob"]) if r["prob"] else ""))

    log("回放结束：共 %d 帧，事件 %s（坏包 %s）"
        % (rec.count, rec.events, getattr(parser, "bad_packets", "?")))
    return 0


def run_selftest(args):
    """合成波形自测：站立 → 上冲 → 站立 → 下冲。

    分三遍跑，各验证一件事：
      A. 无判据（兜底极值）→ 只验证**状态机**（基线/门限/锁存）
      B. 先验判据        → 验证**期望形态**判据本身能否识别标准波形
      C. 训练模型        → 验证模型推理这条代码路径（方向准确性另说）
    """
    seq = ([0.0] * 10 +
           [12.0, 18.0] + [0.0] * 5 +
           [-12.0, -18.0] + [0.0] * 5)
    print("合成序列：站立 → 上冲(+12,+18) → 站立 → 下冲(-12,-18) → 站立")
    print("（这就是 EXPECTED_SPEC 描述的形态：短促上冲 / 持续下沉）")
    print("base_frames=%d  cooldown=%d\n" % (args.base_frames, args.cooldown))

    prior = load_prior(args) if args.rule != "model" else None
    soft = load_softmax(args, quiet=True)

    cases = [("A. 无判据（验证状态机）", None)]
    if prior is not None:
        cases.append(("B. 先验判据（EXPECTED_SPEC）", prior))
    if soft is not None:
        cases.append(("C. 训练模型", soft))

    for title, judge in cases:
        det = JumpDuckRecognizer(judge, base_frames=args.base_frames,
                                 cooldown=args.cooldown,
                                 use_model=(judge is not None))
        if judge is not None:
            judge.min_dev = float(args.min_dev or DEFAULT_MIN_DEV)
        fired = []
        reasons = []
        print("--- %s ---" % title)
        for i, p in enumerate(seq):
            ev = det.update(p)
            if ev and ev["state"] != "NONE":
                fired.append((i, ev["state"]))
                reasons.append("%s:%s" % (ev["state"], ev.get("reason")))
        print("  触发：%s" % (", ".join("第%d帧 %s" % f for f in fired) or "无"))
        if reasons:
            print("  理由：%s" % " / ".join(reasons))
        print("  基线：%s\n" % ("%.1f" % det.baseline if det.baseline is not None else "未建立"))

    print("判读：A 必须同时出现 JUMP 和 DUCK，否则是状态机/门限问题；")
    print("      B 必须同时出现，且 reason 为 up_peak_short_strong / down_trough_sustained；")
    print("      C 只要不报错即可——当前模型方向准确率约随机，见 models/jumpduck.json。")
    return 0


def load_softmax(args, quiet=False):
    path = args.model
    if not os.path.isfile(path):
        if not quiet:
            log("未找到模型 %s" % path)
        return None
    m = SoftmaxModel.load(path)
    if not quiet:
        log("模型已加载：%s（类别 %s，窗 %d，LOSO %s）"
            % (os.path.relpath(path, ROOT), m.classes, m.window,
               m.metrics.get("loso_accuracy", "?")))
    if not m.usable and not quiet:
        log("!! 该模型未达可用门槛（usable=false）：只能验证链路，"
            "不要用于现场演示或验收；重新采集后再训练。")
    return m


def load_prior(args):
    path = args.prior
    if path and os.path.isfile(path):
        j = PriorJudge.load(path)
        log("先验判据已加载：%s（min_dev=%.1f 窗=%d 上冲≤%d帧 下沉≥%d帧）"
            % (os.path.relpath(path, ROOT), j.min_dev, j.window,
               j.max_up_frames, j.min_neg_frames))
        return j
    j = PriorJudge(min_dev=args.min_dev or 8.0, window=args.window)
    log("先验判据（内置默认，未用数据校准）：min_dev=%.1f 窗=%d"
        % (j.min_dev, j.window))
    spec = EXPECTED_SPEC
    log("期望形态：帧率≥%.0f Hz、站距 %.1f–%.1f m、跳上冲≥%.0f°且≤%d帧、"
        "蹲下沉≥%.0f°且≥%d帧"
        % (spec["frame_rate_hz"], spec["stand_range_m"][0], spec["stand_range_m"][1],
           spec["jump"]["up_peak_deg"], spec["jump"]["up_frames"][1],
           spec["duck"]["down_peak_deg"], spec["duck"]["down_frames"][0]))
    return j


def build_rule(args):
    """prior=先验判据（默认）  model=训练模型  auto=可用则模型、否则先验"""
    if args.no_model:
        log("--no-model：只用兜底极值规则（仅链路自测）")
        return None
    if args.rule == "model":
        return load_softmax(args)
    if args.rule == "prior":
        j = load_prior(args)
        if not args.min_dev:
            args.min_dev = j.min_dev
        return j
    m = load_softmax(args, quiet=True)
    if m is not None and getattr(m, "usable", False):
        log("auto：模型 usable=true → 采用训练模型")
        if not args.min_dev:
            args.min_dev = m.min_dev
        return m
    log("auto：模型缺失或 usable=false → 采用先验判据")
    j = load_prior(args)
    if not args.min_dev:
        args.min_dev = j.min_dev
    return j


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="2T4R 跳/蹲 → 游戏 ws 桥（role=ud）")
    ap.add_argument("--port", default="auto", help="串口；auto=按 USB 指纹自动识别")
    ap.add_argument("--ws-port", type=int, default=8767)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=os.path.join(ROOT, "models", "jumpduck.json"))
    ap.add_argument("--prior", default=os.path.join(ROOT, "models", "jumpduck-prior.json"),
                    help="先验判据参数文件（由 train_jumpduck.py --calibrate-prior 生成）")
    ap.add_argument("--rule", choices=["prior", "model", "auto"], default="prior",
                    help="prior=期望形态判据（默认） model=训练模型 auto=模型可用才用模型")
    ap.add_argument("--window", type=int, default=3, help="先验判据的滑窗帧数")
    ap.add_argument("--no-model", action="store_true", help="不用判据，只跑兜底极值规则")
    ap.add_argument("--rx-pair", default="0,1",
                    help="俯仰相位差用的两个 RX，如 0,1（默认）或 0,3（长基线）")
    ap.add_argument("--band", default="6,21", help="俯仰角加权用的距离门范围")
    ap.add_argument("--min-dev", type=float, default=None,
                    help="动作门限（度）；默认取模型里的值或 %.1f" % DEFAULT_MIN_DEV)
    ap.add_argument("--base-frames", type=int, default=8,
                    help="启动基线帧数；2T4R 约 0.53 s/帧，8 帧≈4.2 s")
    ap.add_argument("--cooldown", type=int, default=2)
    ap.add_argument("--board", default=None, help="强制板型 1t2r/2t4r（默认自动）")
    ap.add_argument("--chirps", type=int, default=None,
                    help="2T4R 每帧 chirp 数，必须等于 Radar_Config.ini 的 "
                         "ChirpsPerFrame；不填则按 profile（64）。改成 16 可把帧率提到 7.7Hz")
    ap.add_argument("--replay", default=None, help="回放原始串口流 .bin")
    ap.add_argument("--selftest", action="store_true", help="合成波形自测，不碰硬件")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--log", default=None, help="输出追加写入文件")
    args = ap.parse_args()

    if args.log:
        fp = open(args.log, "a", encoding="utf-8", buffering=1)
        sys.stdout = fp
        sys.stderr = fp

    if args.selftest:
        return run_selftest(args)

    try:
        rx_a, rx_b = [int(v) for v in args.rx_pair.split(",")]
    except Exception:
        log("--rx-pair 格式应为 a,b（如 0,1）")
        return 2
    try:
        lo, hi = [int(v) for v in args.band.split(",")]
    except Exception:
        log("--band 格式应为 lo,hi（如 6,21）")
        return 2

    model = build_rule(args)
    min_dev = args.min_dev if args.min_dev is not None else DEFAULT_MIN_DEV
    det = JumpDuckRecognizer(model, base_frames=args.base_frames,
                             cooldown=args.cooldown, use_model=not args.no_model)
    det.model = model

    hub = Hub()
    if args.replay is None:
        if start_server(args.host, args.ws_port, hub) is None:
            return 2
        log("ws://%s:%d 已监听（role=ud，跳蹲）" % (args.host, args.ws_port))
    log("识别参数：rx=%d-%d band=%d-%d min_dev=%.1f° base=%d 帧"
        % (rx_a, rx_b, lo, hi, min_dev, args.base_frames))
    # 采样能力自检：跳是一个 ~0.5 s 的动作，帧周期直接决定能采到几帧
    from radar_source import frame_period as _fp
    _T = _fp(args.board or "2t4r", chirps=args.chirps)
    _n = max(0, int(0.50 / _T))
    log("帧周期 %.3f s（%.2f Hz）→ 0.5s 腾空约采 %d 帧%s"
        % (_T, 1.0 / _T, _n,
           "  ← 不足 3 帧，跳的判断不可信" if _n < 3 else ""))

    # 把 min_dev 注入模型（识别器从这个属性读）
    if model is not None:
        model.min_dev = float(min_dev)
    else:
        class _RuleModel(object):
            window = 4
            min_dev = float(min_dev)
            enter = 0.5
            classes = ["JUMP", "DUCK"]
        det.model = _RuleModel()

    rec = Recognizer(det, rx_a, rx_b, lo, hi)

    if args.replay:
        return run_replay(args, rec, hub)

    try:
        run_serial(args, rec, hub)
    except KeyboardInterrupt:
        log("退出。共 %d 帧，事件 %s" % (rec.count, rec.events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
