# -*- coding: utf-8 -*-
r"""训练左右移的 ML 模型（有效性闸门 + MLP），并导出可直接上线的 JSON。

与 `tools/compare_lane_models.py` 的分工
----------------------------------------
  比较脚本：只回答"ML 值不值得上"，用 3 个模型横比，产物是报告。
  本脚本  ：真的训一个模型出来，产物是 `radar-pointcloud/models/lane_mlp.json`，
            能被 `radar_actions.ActionRecognizer(crit='ml')` 直接加载。

口径（与比较脚本一致，防止"离线好、上线崩"）
--------------------------------------------
  * 特征：`radar-pointcloud/lane_model.build_feature_matrix`（因果，训练=实时同一份代码）
  * 证据：MLP 逐帧概率 → 同一套状态机（confirm 3 帧 + 最短间隔 0.5 s + 释放确认 3）
  * 打分：命中窗 [t_start-0.5, t_end+0.5]、延迟=段内首个同向事件、误报/分钟
  * 验证：16 折留一文件。**阈值与标准化参数都只在训练折上选**，测试文件全程不参与。

有效性闸门（"先判信息是否有效"）
--------------------------------
  闸门不吃特征、只做样本筛选：无效帧在推理时被强制判 NONE（= 本帧不产生证据）。
  本脚本会打印两件事作为验收依据：
    1) 闸门在**动作段内**的通过率（应接近 100%，否则会吃掉真动作）
    2) 闸门在**非动作段**的拒收率（越高越好，说明它在挡垃圾）

用法
----
    python tools/train_lane_ml.py                    # 全量 16 文件，导出模型
    python tools/train_lane_ml.py --subset mvp --no-export
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'radar-pointcloud'))
sys.path.insert(0, HERE)

import lane_model as lm  # noqa: E402
from compare_lane_models import (  # noqa: E402
    FILES, MVP, agg_scores, emit_events, evaluate, fmt, labels_of, prod_series,
)

MODEL_OUT = os.path.join(ROOT, 'radar-pointcloud', 'models', 'lane_mlp.json')
REPORT_OUT = os.path.join(ROOT, 'outputs', 'lane-eval', 'ml-lane.json')
CONFIRM = 3
MIN_INTERVAL = 0.5
RELEASE_CONFIRM = 3
RELEASE_THR = 0.35      # 释放门（max(p_left,p_right) <= 该值 连续 3 帧 → 回 CENTER）


# ---------------------------------------------------------------- 模型拟合
def fit_mlp(X, y, hidden=16, epochs=500, lr=0.08, l2=1e-4, seed=0):
    """1 隐层 tanh + softmax，Adam。返回参数字典（可直接写 JSON），不返回闭包。"""
    rng = np.random.default_rng(seed)
    n, d = X.shape
    K = 3
    Y = np.eye(K)[y]
    cnt = np.bincount(y, minlength=K).astype(float)
    sw = (len(y) / (K * np.maximum(cnt, 1)))[y]
    W1 = rng.normal(0, np.sqrt(2.0 / d), (d, hidden))
    b1 = np.zeros(hidden)
    W2 = rng.normal(0, np.sqrt(2.0 / hidden), (hidden, K))
    b2 = np.zeros(K)
    ps = [W1, b1, W2, b2]
    m = [np.zeros_like(p) for p in ps]
    v = [np.zeros_like(p) for p in ps]
    b1_, b2_, eps = 0.9, 0.999, 1e-8
    for it in range(1, epochs + 1):
        H = np.tanh(X @ W1 + b1)
        Z = H @ W2 + b2
        Z = Z - Z.max(axis=1, keepdims=True)
        P = np.exp(Z)
        P /= P.sum(axis=1, keepdims=True)
        G = (P - Y) * sw[:, None] / n
        gW2 = H.T @ G + l2 * W2
        gb2 = G.sum(axis=0)
        GH = (G @ W2.T) * (1 - H ** 2)
        gW1 = X.T @ GH + l2 * W1
        gb1 = GH.sum(axis=0)
        for i, g in enumerate([gW1, gb1, gW2, gb2]):
            m[i] = b1_ * m[i] + (1 - b1_) * g
            v[i] = b2_ * v[i] + (1 - b2_) * g * g
            upd = lr * (m[i] / (1 - b1_ ** it)) / (np.sqrt(v[i] / (1 - b2_ ** it)) + eps)
            ps[i] -= upd
    return dict(W1=ps[0], b1=ps[1], W2=ps[2], b2=ps[3])


def mlp_proba(params, X):
    """入参是**原始**特征（内部做标准化，与 LaneModel.proba 一致）。"""
    Z = (X - params['mu']) / params['sd']
    H = np.tanh(Z @ params['W1'] + params['b1'])
    L = H @ params['W2'] + params['b2']
    L = L - L.max(axis=1, keepdims=True)
    P = np.exp(L)
    return P / P.sum(axis=1, keepdims=True)


def apply_gate(P, valid):
    """无效帧 → 强制 NONE（conf=1.0），即"本帧不产生证据"。"""
    Q = P.copy()
    Q[~valid] = np.array([1.0, 0.0, 0.0])
    return Q


def emit_events_skip(t, P, valid, thr, release=0.35, resets=None,
                     reset_strict=False):
    """与 ActionRecognizer(crit='ml') 逐条等价的事件生成。

    ★ 这里必须和 radar_actions 里的实现**完全一致**，否则离线数字就不是上线的数字：
      - 无效帧（闸门判无效或该帧无检出点）：确认计数与释放计数**双双清零**，
        这一帧不产生也不释放事件
      - 释放条件用 max(p_left, p_right) <= release（**不是** argmax 判 NONE）
        —— 2026-11-24 踩过：训练侧用 argmax 判 NONE 会让释放更松，事件数比实时多
    """
    from compare_lane_models import CONFIRM, MIN_INTERVAL, RELEASE_CONFIRM
    pred = P.argmax(axis=1)
    conf = P.max(axis=1)
    lane = 0
    want = 0
    run = 0
    last_flip = -1e9
    rel = 0
    evs = []
    ri = 0
    for i in range(len(t)):
        # 复位（TARGET_LOST → _reset_track）：lane 回 CENTER、计数清零
        while (resets is not None and ri < len(resets)
               and (t[i] > resets[ri] if reset_strict else t[i] >= resets[ri])):
            lane = 0
            want = 0
            run = 0
            rel = 0
            ri += 1
        if not valid[i]:
            # ★ 实时侧对无效帧的处理是 **清零**（radar_actions L821-823：hold=False,
            #   _xrel_run=0；随后 L852-853：moved=False ⇒ _dir_frames=0），
            #   不是"跳过"。跳过会让确认/释放计数跨无效帧累积，等于偷偷放宽了门限。
            run = 0
            rel = 0
            continue
        cls = int(pred[i]) if conf[i] >= thr else 0
        if cls != 0 and cls != lane:
            if cls == want:
                run += 1
            else:
                want = cls
                run = 1
            if run >= CONFIRM and t[i] - last_flip >= MIN_INTERVAL:
                lane = cls
                last_flip = t[i]
                evs.append((float(t[i]), 'LEFT' if cls == 1 else 'RIGHT'))
                run = 0
        else:
            run = 0
        if lane != 0:
            rel = rel + 1 if float(max(P[i, 1], P[i, 2])) <= release else 0
            if rel >= RELEASE_CONFIRM:
                lane = 0
                rel = 0
    return evs


# ---------------------------------------------------------------- 取数
def load_dataset(top_k, min_pts, band, min_det, subset, nocache):
    keep = MVP if subset == 'mvp' else None
    data = []
    for tag, rel, dist in FILES:
        if keep is not None and tag not in keep:
            continue
        path = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.exists(path):
            print('  跳过（文件不存在）: %s' % rel)
            continue
        s = prod_series(path, tag, top_k, min_pts, band=band, use_cache=not nocache)
        det = float(np.mean(~np.isnan(s['px']))) if len(s['px']) else 0.0
        if det < min_det:
            print('  丢弃（检出率 %.0f%% < %.0f%%）: %s' % (100 * det, 100 * min_det, tag))
            continue
        labs = labels_of(path)
        spans = [(l['action'], float(l['t_start']), float(l['t_end'])) for l in labs
                 if l.get('action') in ('LEFT', 'RIGHT')]
        X = lm.build_feature_matrix(s['px'], s['py'], s['en'], s['n'])
        t = s['t']
        y = np.zeros(len(t), dtype=int)
        in_span = np.zeros(len(t), dtype=bool)
        for act, t0, t1 in spans:
            m = (t >= t0) & (t <= t1)
            y[m] = lm.CLASSES.index(act)
            in_span |= m
        data.append(dict(tag=tag, dist=dist, s=s, t=t, X=X, y=y, in_span=in_span,
                         spans=spans, labs=labs, det=det,
                         minutes=s['span'] / 60.0))
    return data


def gate_stats(data):
    """闸门的两项验收指标：动作段内通过率、非动作段拒收率。"""
    tot_in = tot_out = 0
    pass_in = pass_out = 0
    jit_in, jit_out = [], []
    for d in data:
        m = lm.gate_mask(d['s']['px'], d['X'])
        ins = d['in_span']
        tot_in += int(ins.sum())
        tot_out += int((~ins).sum())
        pass_in += int((m & ins).sum())
        pass_out += int((m & ~ins).sum())
        jit_in.append(d['X'][ins, 10])
        jit_out.append(d['X'][~ins, 10])
    ji = np.concatenate(jit_in) if jit_in else np.array([0.0])
    jo = np.concatenate(jit_out) if jit_out else np.array([0.0])
    return dict(
        span_frames=tot_in, span_pass=pass_in,
        span_pass_rate=pass_in / max(tot_in, 1),
        idle_frames=tot_out, idle_pass=pass_out,
        idle_reject_rate=1.0 - pass_out / max(tot_out, 1),
        jit_in_p50=float(np.median(ji)), jit_in_p90=float(np.percentile(ji, 90)),
        jit_idle_p50=float(np.median(jo)), jit_idle_p90=float(np.percentile(jo, 90)),
    )


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    ap = argparse.ArgumentParser(description='训练左右移 ML 模型（闸门 + MLP）并导出')
    ap.add_argument('--subset', choices=['all', 'mvp'], default='all')
    ap.add_argument('--top-k', type=int, default=0)
    ap.add_argument('--min-pts', type=int, default=2)
    ap.add_argument('--band', default='auto')
    ap.add_argument('--min-det', type=float, default=0.35)
    ap.add_argument('--epochs', type=int, default=500)
    ap.add_argument('--hidden', type=int, default=16)
    ap.add_argument('--nocache', action='store_true')
    ap.add_argument('--no-export', action='store_true')
    ap.add_argument('--seed', type=int, default=0)
    args = ap.parse_args(argv)
    band = args.band
    if band != 'auto':
        try:
            band = tuple(float(v) for v in band.split(','))
        except Exception:
            raise SystemExit("--band 只接受 'auto' 或 'lo,hi'")

    t0 = time.time()
    print('=== 取数（产线口径：band=%s, top_k=%d, min_pts=%d）===' % (band, args.top_k, args.min_pts))
    data = load_dataset(args.top_k, args.min_pts, band, args.min_det, args.subset, args.nocache)
    if not data:
        print('没有可用文件')
        return 1
    nfr = sum(len(d['t']) for d in data)
    nseg = sum(len(d['spans']) for d in data)
    print('  文件 %d 个，帧 %d，动作段 %d 段，共 %.1f min'
          % (len(data), nfr, nseg, sum(d['minutes'] for d in data)))

    print()
    print('=== 有效性闸门体检（阈值见 GATE_DEFAULT / 模型 JSON）===')
    g = gate_stats(data)
    print('  动作段内：%d/%d 帧通过 = %.1f%%   ← 必须接近 100，否则吃掉真动作'
          % (g['span_pass'], g['span_frames'], 100 * g['span_pass_rate']))
    print('  非动作段：拒收 %.1f%%（%d/%d 帧被挡）'
          % (100 * g['idle_reject_rate'], g['idle_frames'] - g['idle_pass'], g['idle_frames']))
    print('  jit16 中位：动作段 %.4f (p90 %.4f)  非动作段 %.4f (p90 %.4f)'
          % (g['jit_in_p50'], g['jit_in_p90'], g['jit_idle_p50'], g['jit_idle_p90']))

    # ---- 留一文件：三种用法都跑，闸门到底帮不帮忙由数据说话 ----
    print()
    print('=== 留一文件验证（%d 折）===' % len(data))
    THRS = (0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95)
    MODES = ('raw', 'skip', 'force', 'rt')
    res = {m: {'thr': [], 'rows': []} for m in MODES}
    Pte, Valid = {}, {}
    for k, d in enumerate(data):
        tr = [j for j in range(len(data)) if j != k]
        Xtr = np.vstack([data[j]['X'] for j in tr])
        ytr = np.concatenate([data[j]['y'] for j in tr])
        vtr = np.concatenate([lm.gate_mask(data[j]['s']['px'], data[j]['X']) for j in tr])
        # ★ 模型只在**有效帧**上训练：这就是"从有效信息学动作"
        Xtr_v, ytr_v = Xtr[vtr], ytr[vtr]
        mu = Xtr_v.mean(axis=0)
        sd = Xtr_v.std(axis=0) + 1e-9
        par = fit_mlp((Xtr_v - mu) / sd, ytr_v, hidden=args.hidden,
                      epochs=args.epochs, seed=args.seed)
        par.update(mu=mu, sd=sd)
        Pte[k] = mlp_proba(par, d['X'])
        Valid[k] = lm.gate_mask(d['s']['px'], d['X'])

        for mode in MODES:
            best = None
            for thr in THRS:
                hit = tot = 0
                fpm = 0.0
                for j in tr:
                    Pj = mlp_proba(par, data[j]['X'])
                    vj = lm.gate_mask(data[j]['s']['px'], data[j]['X'])
                    if mode == 'raw':
                        ev = emit_events(data[j]['t'], Pj, thr)
                    elif mode == 'skip':
                        ev = emit_events_skip(data[j]['t'], Pj, vj, thr)
                    elif mode == 'force':
                        ev = emit_events(data[j]['t'], apply_gate(Pj, vj), thr)
                    else:
                        # rt：**完全复现实时**——无检出点的那一帧 push() 直接早退，
                        # 不产生任何证据（见 radar_actions.push 的 len(sel) < min_pts 分支）
                        det_j = ~np.isnan(data[j]['s']['px'])
                        rst_j = data[j]['s'].get('rst_t')
                        ev = emit_events_skip(data[j]['t'], Pj, vj & det_j, thr,
                                              RELEASE_THR,
                                              resets=list(rst_j) if rst_j is not None else None,
                                              reset_strict=True)
                    rr = evaluate(ev, data[j]['labs'], data[j]['s']['span'])
                    hit += rr['hit']['LEFT'] + rr['hit']['RIGHT']
                    tot += rr['tot']['LEFT'] + rr['tot']['RIGHT']
                    fpm += rr['fp_per_min']
                sc = hit / max(tot, 1) - (fpm / len(tr)) / 40.0
                if best is None or sc > best[0]:
                    best = (sc, thr)
            res[mode]['thr'].append(best[1])
        print('    折 %2d/%d 留出 %-14s thr(raw/skip/force/rt)=%.2f/%.2f/%.2f/%.2f'
              % (k + 1, len(data), d['tag'], res['raw']['thr'][-1],
                 res['skip']['thr'][-1], res['force']['thr'][-1],
                 res['rt']['thr'][-1]))

    LABEL = {'raw': '纯 MLP（无闸门）', 'skip': '闸门(跳过)+MLP', 'force': '闸门(强制NONE)+MLP',
             'rt': '闸门+MLP（**上线口径**）'}
    for mode in MODES:
        rows = []
        for k, d in enumerate(data):
            thr = res[mode]['thr'][k]
            if mode == 'raw':
                ev = emit_events(d['t'], Pte[k], thr)
            elif mode == 'skip':
                ev = emit_events_skip(d['t'], Pte[k], Valid[k], thr)
            elif mode == 'force':
                ev = emit_events(d['t'], apply_gate(Pte[k], Valid[k]), thr)
            else:
                det = ~np.isnan(d['s']['px'])
                rst = d['s'].get('rst_t')
                ev = emit_events_skip(d['t'], Pte[k], Valid[k] & det, thr, RELEASE_THR,
                                      resets=list(rst) if rst is not None else None,
                                      reset_strict=True)
            r = evaluate(ev, d['labs'], d['s']['span'])
            r.update(tag=d['tag'], minutes=d['minutes'], thr=thr)
            rows.append(r)
        res[mode]['rows'] = rows
        res[mode]['agg'] = agg_scores(rows)
        print('  %-18s %s' % (LABEL[mode], fmt(res[mode]['agg'])))

    # 等误报视图（同一批测试预测，只换阈值；用"跳过"语义 = 拟上线用法）
    by_thr = {}
    for thr in THRS:
        rr = []
        for k, d in enumerate(data):
            det = ~np.isnan(d['s']['px'])
            rst = d['s'].get('rst_t')
            ev = emit_events_skip(d['t'], Pte[k], Valid[k] & det, thr, RELEASE_THR,
                                  resets=list(rst) if rst is not None else None,
                                  reset_strict=True)
            r = evaluate(ev, d['labs'], d['s']['span'])
            r.update(tag=d['tag'], minutes=d['minutes'])
            rr.append(r)
        by_thr[str(thr)] = agg_scores(rr)
    print('  等误报曲线（上线口径，只换阈值）：')
    for thr, s in by_thr.items():
        print('      thr=%s  %s' % (thr, fmt(s)))

    # 规则基线，便于同表对照
    rule_rows = []
    for d in data:
        evs = [(float(t), 'LEFT' if c == 1 else 'RIGHT')
               for t, c in zip(d['s']['ev_t'].tolist(), d['s']['ev_s'].tolist())]
        r = evaluate(evs, d['labs'], d['s']['span'])
        r.update(tag=d['tag'], minutes=d['minutes'])
        rule_rows.append(r)
    base = agg_scores(rule_rows)
    print('  %-16s %s' % ('规则基线（对照）', fmt(base)))

    # ---- 全量训练 + 导出 ----
    Xa = np.vstack([d['X'] for d in data])
    ya = np.concatenate([d['y'] for d in data])
    va = np.concatenate([lm.gate_mask(d['s']['px'], d['X']) for d in data])
    mu = Xa[va].mean(axis=0)
    sd = Xa[va].std(axis=0) + 1e-9
    par = fit_mlp((Xa[va] - mu) / sd, ya[va], hidden=args.hidden,
                  epochs=args.epochs, seed=args.seed)
    thr_on = float(np.median(res['rt']['thr']))
    thr_off = float(min(RELEASE_THR, thr_on))
    print()
    print('=== 导出 ===')
    print('  全量训练帧 %d（闸门后有效帧），thr_on=%.2f（各折中位），thr_off=%.2f'
          % (int(va.sum()), thr_on, thr_off))

    obj = dict(
        version=lm.MODEL_VERSION, tag=lm.TAG,
        created=time.strftime('%Y-%m-%d %H:%M'),
        purpose='左右移 ML 判定：有效性闸门 + 12 维因果特征 MLP（1T2R）',
        frame_period=lm.FRAME_PERIOD,
        features=list(lm.FEATURE_NAMES),
        classes=list(lm.CLASSES),
        norm=dict(mu=[float(x) for x in mu], sd=[float(x) for x in sd]),
        mlp=dict(W1=par['W1'].tolist(), b1=par['b1'].tolist(),
                 W2=par['W2'].tolist(), b2=par['b2'].tolist()),
        gate=dict(lm.GATE_DEFAULT),
        thr=dict(on=thr_on, off=thr_off),
        state_machine=dict(confirm=CONFIRM, min_interval=MIN_INTERVAL,
                           release_confirm=RELEASE_CONFIRM),
        loso=dict(n_files=len(data), n_frames=int(nfr), n_segments=nseg,
                  gate_skip=res['skip']['agg'], raw=res['raw']['agg'],
                  gate_force=res['force']['agg'], rt=res['rt']['agg'],
                  rule=base, by_thr=by_thr),
        train=dict(subset=args.subset, top_k=args.top_k, min_pts=args.min_pts,
                   band=(band if isinstance(band, str) else list(band)),
                   min_det=args.min_det, epochs=args.epochs,
                   hidden=args.hidden, frames_used=int(va.sum()),
                   files=[d['tag'] for d in data]),
        notes='离线留一验证；几何/站位与训练不一致时必须重新训练并重标 thr。',
    )
    if not args.no_export:
        lm.save_model(obj, MODEL_OUT)
        print('  模型 → %s（%.1f KB）'
              % (os.path.relpath(MODEL_OUT, ROOT), os.path.getsize(MODEL_OUT) / 1024.0))
        os.makedirs(os.path.dirname(REPORT_OUT), exist_ok=True)
        rep = dict(model=os.path.relpath(MODEL_OUT, ROOT), gate=g,
                   loso=dict(gate_skip=res['skip']['agg'], raw=res['raw']['agg'],
                             gate_force=res['force']['agg'], rt=res['rt']['agg'],
                             rule=base, by_thr=by_thr),
                   per_file={d['tag']: None for d in data})
        for r in res['skip']['rows']:
            rep['per_file'][r['tag']] = {k: v for k, v in r.items() if k != 'lat'}
        with open(REPORT_OUT, 'w', encoding='utf-8') as f:
            json.dump(rep, f, ensure_ascii=False, indent=1, default=float)
        print('  报告 → %s' % os.path.relpath(REPORT_OUT, ROOT))

        # 导出后自检：JSON 能被运行时类加载，且预测与训练侧一致
        m = lm.LaneModel.from_file(MODEL_OUT)
        k0 = 0
        chk = m.proba(data[k0]['X'][:64])
        ref = mlp_proba(dict(W1=par['W1'], b1=par['b1'], W2=par['W2'], b2=par['b2'],
                             mu=mu, sd=sd), data[k0]['X'][:64])
        print('  自检：加载后前向最大偏差 %.2e' % float(np.abs(chk - ref).max()))
        print('  自检：闸门参数 %s' % json.dumps(m.gate_params, ensure_ascii=False))
    print()
    print('用时 %.0f s' % (time.time() - t0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
