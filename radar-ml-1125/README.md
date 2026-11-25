# 2026-11-25 雷达侧成果快照（左右移 ML + 跳蹲 1T2R）

> 本目录是 `radar-lr3c` 分支 2026-11-25 的成果快照，**只做追加，不改动仓库里任何已有文件**。
> 对应的开发分支：`radar-lr3c`（代码的完整历史在那里）。

## 为什么要有这一份

仓库根目录 `docs/` 下的 `lane-model-comparison.md` 是 2026-11-24 的版本
（PR #7 合入），当时的结论是「MLP 84% vs 规则 67%」。
**那个 84% 是离线宽松口径**——离线重放允许「没有检出点的帧」用上一帧的值凑出证据，
而实时链路在无检出时会直接早退。用真跑产线代码的口径重测后是 **70.5%**。

本目录放的是修正后的完整结论，不与旧文件冲突；旧文件保留原样，作为当时的记录。

## 关键数字（全部真跑产线代码，不是离线重放）

### 左右移（1T2R，16 文件 224 段，约 49.6 min）

| 判据 | 命中 | 延迟中位 | 误报 |
|---|---|---|---|
| ML `crit='ml'` | **158/224 = 70.5%** | **1.295 s** | **2.9 /min** |
| 规则 `crit='xbase'` | 150/224 = 67.0% | 1.684 s | 7.9 /min |

优势是「同准确率下误报少 63%、快 0.39 s」，不是「+17 点」。
默认仍走规则，`--crit ml` 一秒切换。

### 跳/蹲（1T2R，4 会话 96 段）

| 层 | 结果 |
|---|---|
| 逐帧单变量 | AUC **0.43–0.57**（不可分，1T2R 无垂直角） |
| 4 帧窗口 + MLP 留一会话 | 窗口级 **66.2%**、段级 **88.5%** |
| 配对 Δ（动作 − 最近 rest） | `v_std` **AUC 0.738**，4 会话方向一致 |
| 负样本（弯腰/转身/踏步/静止） | **100% 误报** |

**未接入实时链路，不作为已实现功能。** 原因与翻案条件见
`docs/jump-duck-1t2r-findings.md` 第 5、6 节：每段录制里 jump 整块在前、
duck 整块在后，置换检验排除不了时间漂移，必须先交替重采（A/B/A/B）。

## 目录

```
radar-ml-1125/
├── README.md                          # 本文件
├── docs/
│   ├── lane-model-comparison.md       # 左右移结论主文档（含上线口径第 8 节）
│   ├── lane-model-comparison-deck.md  # PPT 素材（第 10-11 页）
│   ├── 贡献-20261125.md               # 当日完整流水（代码/数字/验证/限制）
│   └── jump-duck-1t2r-findings.md     # 跳蹲证据边界（含与规则法对照）
├── radar-pointcloud/
│   ├── lane_model.py                  # 闸门 + 12 维因果特征 + 纯 numpy 前向
│   └── models/
│       ├── lane_mlp.json              # 左右移模型（thr 0.60 / release 0.35）
│       └── jumpduck_1t2r.json         # 跳蹲模型（未接入）
├── tools/
│   ├── train_lane_ml.py               # 训练 + 四口径留一 + 等误报曲线 + 导出
│   ├── eval_lane_ml_runtime.py        # 上线口径评估（真跑 analyze_file）
│   ├── verify_ml_runtime.py           # 两层移植一致性自检
│   ├── check_lane_feature_parity.py   # 实时/离线特征逐位一致
│   ├── compare_lane_models.py         # 离线四路对比 + prod_series 缓存
│   ├── train_jumpduck_1t2r.py         # 跳蹲训练 + 置换检验
│   ├── pair_jumpduck_rest.py          # 跳蹲配对检验（Δ 相对最近 rest）
│   └── compare_jumpduck_rule_ml.py    # 规则 vs ML + 负样本误报
└── outputs/
    ├── ml-runtime.json                # 70.5% vs 67.0%（对外引用的数字来源）
    ├── ml-lane.json                   # 闸门体检 + 四模式留一 + 等误报
    ├── jumpduck-1t2r.json             # 跳蹲留一 + 置换检验
    ├── jumpduck-1t2r-delta.json       # 因果基线差口径
    ├── jumpduck-1t2r-paired.json      # 96 对配对明细
    └── jumpduck-1t2r-vs-rule.json     # 规则/ML/负样本对照
```

## 复现说明

脚本默认从**仓库根目录**运行（`radar-pointcloud/` 与 `tools/` 同级）。
本快照里的路径是按该布局复制的；若在本目录内直接运行，需相应调整 `ROOT` 推导。

原始录制数据（`data/`）按项目规定**不入库**，因此下列命令在没有数据时只能复现
代码逻辑，不能复现数字：

```powershell
python tools\train_lane_ml.py --top-k 1 --min-pts 1 --band 1.0,2.6
python tools\eval_lane_ml_runtime.py
python tools\verify_ml_runtime.py
python tools\train_jumpduck_1t2r.py
python tools\pair_jumpduck_rest.py
python tools\compare_jumpduck_rule_ml.py
```
