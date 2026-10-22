# 实验结果清单（E0–E4）

全部结果文件 SHA256 见同目录 `results-checksums.sha256`。
逐行预测/错误分类在运行时由运行器按需复算（输入=数据+种子+代码，
确定性）；已提交 JSON 含逐数据集汇总、四分类计数与每类前 10 个
错误样例（`examples` 字段）。

## E0 — 基线重建（分支 exp/header-v11-repro @ 8fb2c985）

| 产物 | 内容 |
|---|---|
| `e0-baseline-report.md` | 历史缺失记录、M1–M4 定义、22 数据集、指标、错误类别 C1–C7 |
| `e0-results/e0_results.json` | M1–M4 × 22 数据集全量指标（E0 协议 500/rest）；**注意：其 M3 数值含 by_value 缺陷，修正值见 E2 报告 §1.3** |

## E1 — 自适应规则学习（分支 exp/header-v11-adaptive @ c3d6d93b + 5b7533b7）

| 产物 | 内容 |
|---|---|
| `e1-adaptive-report.md` | （来源=分支文件，归档分支内为副本）per-source 规则、选择准则、LODO、附录 A 补充指标 |
| E1 分支 `e1-results/e1_results.json` | 22 数据集 × M2-RULE/M3-RULE/E1-A/B/C/M2-raw/M3-raw、τ 曲线、LODO、method_choice、rules_e1c（规则原型 JSON） |
| 本归档分支 `e1-results/e1_results.json` | E3 前置检查在**修复版 M3** 下的 E1 重跑（M3 参照 +7pp，E1-A/B/C 逐位不变），用于跨实验一致性追踪 |

## E2 — Span 分词（分支 exp/header-v11-span @ 811914c6）

| 产物 | 内容 |
|---|---|
| `e2-span-report.md` | 分词器设计、消融 A/B/C/D、挑战集 8 类、修复案例、性能、基线偏移声明 §1.3 |
| `e2-results/e2_results.json` | E2-A/B/C/D × 23 数据集、挑战集分类别、性能（avg/p50/p95/吞吐/RSS） |

## E3 — 置信度门控（分支 exp/header-v11-gate @ 63eb7063）

| 产物 | 内容 |
|---|---|
| `e3-gate-report.md` | (签名,组合) 规则键、风险表、转移矩阵、拦截/损失率、双工作点、LODO |
| `e3-results/e3_results.json` | A/B/C/D 消融、val/test 曲线、转移矩阵、content_over 分解、逐数据集、门控开销 |

## E4 — 规则编译（分支 exp/header-v11-runtime @ a2898a02）

| 产物 | 内容 |
|---|---|
| `e4-runtime-report.md` | 编译器/匹配器设计、逐行等价（双 τ）、8 场景性能、安全回退、优化记录 |
| `e4-results/e4_results.json` | 等价性明细、P0–P3 性能（avg/p50/p95/p99/CPU/吞吐/5 次重复）、成本五项、来源扩展、跨源误路由全量 |
| **`e4-results/rules_tau0.4.json`** | **可交付规则集**（58/96 放行，树 215，151.4 KB） |
| **`e4-results/rules_tau0.2.json`** | **可交付规则集**（72/96 放行，覆盖优先工作点） |

规则集与训练版本对应：`lineage` 字段记录 detector=SpanM3Prefix(E2-D)、
gate=E3-B(sig,combo)、协议 30/20/50 种子 42；τ 为 E3 验证集选定
工作点（未依测试集重选）。上游演化须走重拟合→重编译，禁止在线改规则。

## 指标复算口径

- 汇总宏平均 = 数据集间平均（分 loghub/stress/challenge 套件 + all）；
  微平均（pooled）在 JSON 中以 n/counts 可复算。
- E3 的 pooled 基数 = 18,798 测试行（E3 报告正文"19,700"为笔误，
  以提交数据 n 为准——E4 报告 §3 已对账）。
- 性能数字为 E4 口径（median-of-5、GC 关闭、预热 300 行）；
  E2/E3 的早期性能数字口径较宽松，引用时以 E4 为准。
