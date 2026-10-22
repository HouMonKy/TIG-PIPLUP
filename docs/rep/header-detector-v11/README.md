# HeaderDetector V1.1 研究归档（E0–E4）

**项目：** TIG-2026 / PIPLUP 自动日志头识别  
**归档分支：** `exp/header-v11-archive`（自 E4 提交 `a2898a02` 创建，仅追加归档文档，未合并任何实验分支）  
**归档日期：** 2026-10-22  
**状态：** E0–E4 全部完成，**未开始 E5，未合并任何稳定分支，未修改 PIPLUP 核心代码**

---

## 1. 项目背景与研究目标

PIPLUP 此前依赖人工配置的 `config/format_chaos.json` 清洗日志头。异构日志
头部格式难以穷举，未剥离的头部参与 token 相似度计算导致相同正文被错误
分簇。HeaderDetector 的目标：**自动学习日志头结构与结束边界**，以
"离线统计学习 + 在线轻量规则匹配"的架构替代人工配置。

V1.1 阶段（E0–E4）的研究问题：

1. 如何在失去历史 V1 实现的情况下重建可信基线（E0）；
2. M2/M3 两代算法能否按格式自适应选择并编译为规则（E1）；
3. 字符级 Span 分词能否解决多 token 括号字段的中 token 边界问题（E2）；
4. 置信度门控能否在不牺牲覆盖率的前提下把误删率压到 ≤0.5%（E3）；
5. 完整检测路径能否等价编译为在线轻量规则（E4）。

## 2. 版本索引

| 实验 | 分支 | 提交 | 父提交 | 主题 |
|---|---|---|---|---|
| 共同基线 | — | `419164df` | — | `codex/piplup-performance-fixes` 上的起点（历史 V1 `ac30623d`/`final` 分支经全面检索确认缺失） |
| E0 | `exp/header-v11-repro` | `8fb2c985` | `419164df` | V1 基线重建：M1–M4、22 数据集、字符级评估框架 |
| E1 | `exp/header-v11-adaptive` | `c3d6d93b` → `5b7533b7` | `8fb2c985` | M2/M3 离线自适应规则学习（独立分支，未合入 E2+ 谱系） |
| E2 | `exp/header-v11-span` | `811914c6` | `8fb2c985` | Span-aware 分词与字符级边界（含基线 M3 两处缺陷修复） |
| E3 | `exp/header-v11-gate` | `63eb7063` | `811914c6` | (签名,组合) 键控置信度门控 |
| E4 | `exp/header-v11-runtime` | `a2898a02` | `63eb7063` | 离线规则编译 + 在线匹配器（P1–P3） |
| **归档** | `exp/header-v11-archive` | 见 git log | `a2898a02` | 本归档（文档/清单/验收，不改变代码） |

谱系说明：E0→E2→E3→E4 为线性父子链；E1 自 E0 独立分叉（按实验计划
要求保持独立）。E3 分支中的 `bench/header_detector/adaptive.py`、
`e1_runner.py`、`bench/header_adaptive.py` 是 E3 前置一致性检查时从 E1
分支复制的运行器副本（用于修复版 M3 重跑对照），**不构成合并**；
E1 的原始结果与报告以 E1 分支为准。**E1 与 E4 均未合并到任何稳定分支。**

## 3. 目录索引

```
bench/
  header_ablation.py            E0 入口（M1–M4 消融）
  header_adaptive.py            E1 入口（自适应规则学习）*
  header_span.py                E2 入口（span 分词消融）
  header_gate.py                E3 入口（门控消融）
  header_e4.py                  E4 入口（编译/等价/性能）
  header_detector/
    tokentypes.py               token 类型分类（族级/强类型集合）
    spantok.py                  SpanAwareTokenizer（E2）
    methods.py                  M1–M4（分词器注入式；E2 修复 by_value 等）
    span_methods.py             E2-B/C/D（守卫/精化/前缀）
    adaptive.py, e1_runner.py   E1（副本，见版本索引说明）
    gating.py                   E3-B/C/D 门控
    e2_runner.py, e3_runner.py  实验运行器
    e4_compiler.py              E4-A 离线编译器
    e4_matcher.py               E4-B 在线匹配器（P1–P3）
    e4_runner.py                E4-C/D/E 实验运行器
    gt.py                       Loghub GT 正则（GT 版本 v1.1-rebuild）
    gen_stress.py               压力集生成（种子 42，源自 format_chaos）
    gen_challenge.py            挑战集生成（种子 4242，独立词表）
    metrics.py                  字符级四分类指标
  header_data/
    loghub/   14 个 Loghub 2k 数据集（公开基准）
    stress/   8 个压力集（生成，1000 行/集，GT 由构造生成）
    challenge/ 1 个括号挑战集（生成，1600 行）
docs/rep/header-detector-v11/
  e0-baseline-report.md ... e4-runtime-report.md   各实验报告（原始保留）
  e0-results/ ... e4-results/                       各实验结果 JSON/规则
  design/      architecture / algorithm / interfaces / decisions / limitations
  archive/     data-manifest / results-manifest / data-checksums / acceptance-report
  final-summary.md                                   阶段结论
  README.md                                          本文件
```

\* 入口脚本间存在分支差异：E0/E2 入口在其各自分支上；归档分支（=E4 谱系）
含全部五个入口。

## 4. 快速复现

统一环境：Python 3.13.5（纯标准库，无第三方依赖；Apple Silicon 实测）。
全部实验共用数据与划分（E1–E4：learn/val/test = 30/20/50，种子 42）。

```bash
git worktree add <dir> exp/header-v11-archive && cd <dir>

python3 bench/header_ablation.py    # E0：M1–M4 消融（约 3 分钟）
python3 bench/header_adaptive.py    # E1：自适应规则（约 4 分钟）
python3 bench/header_span.py        # E2：span 消融 + 性能（约 4 分钟）
python3 bench/header_gate.py        # E3：门控消融（约 4 分钟）
python3 bench/header_e4.py          # E4：编译/等价/性能（约 4 分钟）

# 数据重生成确定性验证（应与 archive/data-checksums.sha256 逐字节一致）
python3 -m bench.header_detector.gen_stress --out /tmp/rs
python3 -m bench.header_detector.gen_challenge --out /tmp/rc
```

复现验收（关键指标与已提交结果的逐位比对）见
`archive/acceptance-report.md`。

## 5. 结论可信度分级

- **已验证**（有对应实验数据，见各报告）：版本索引中全部量化结论、
  E4 的 18,798 行逐行等价、E3 拦截率/损失率、E2 挑战集修复、
  E1 自适应安全性收益、`by_value` 缺陷修复的量化偏移。
- **设计推断**（有证据但未单独消融）：E4 报告 §7 的 <5µs 优化方向、
  E3 组合特征对未知业务格式的泛化性、17/256 前缀预算的普适性
  （已按树深度验证为富余）。
- **未验证目标**：PIPLUP 端到端集成后"额外开销 <5%"（E5 目标，
  本阶段仅证明检测器自身 5–6 µs/行）；生产规模（>736 源）下的
  规则治理流程。
