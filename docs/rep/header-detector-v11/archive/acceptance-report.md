# 归档复现验收报告

**归档分支：** `exp/header-v11-archive`（自 E4 `a2898a02`）  
**验收日期：** 2026-10-22  
**验收环境：** macOS / Apple Silicon / Python 3.13.5（纯标准库），
干净 worktree（`/Users/hmonky/Downloads/header-v11-archive`，全新检出）

---

## 1. 验收项与结果

| # | 验收项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 所有提交可在远端定位 | `git push` 5 个实验分支至 origin | ✅ 全部推送成功（见 §3） |
| 2 | 归档文件校验值 | `data-checksums.sha256`（23 数据文件）、`results-checksums.sha256`（5 报告 + 5 结果 JSON + 2 规则集） | ✅ 已生成并随归档提交 |
| 3 | 数据来源与 GT 可追溯 | `archive/data-manifest.md` | ✅ 来源/权限/种子/划分/GT 版本/关联实验齐备 |
| 4 | 生成数据可复现 | 干净目录重跑 `gen_stress`（种子 42）+ `gen_challenge`（种子 4242） | ✅ 9/9 文件 SHA256 逐字节一致 |
| 5 | E0 关键指标可复现 | 重跑 `bench/header_ablation.py`，比对 `e0_results.json` | ✅ **逐字节一致**（git diff 为空） |
| 6 | E1 关键指标可复现 | 重跑 `bench/header_adaptive.py`，比对 `e1_results.json` | ✅ **逐字节一致** |
| 7 | E2 关键指标可复现 | 重跑 `bench/header_span.py` | ✅ 质量/逐数据集指标**零差异**；差异仅为计时字段（µs/吞吐/RSS，随机器波动，属预期） |
| 8 | E3 关键指标可复现 | 重跑 `bench/header_gate.py` | ✅ 全部消融/转移矩阵/曲线**零差异**；仅门控开销计时 3 个数值波动 |
| 9 | E4 可复现 + 规则确定性 | 重跑 `bench/header_e4.py` | ✅ 等价性（18798/18798 双 τ）与全部质量字段**零差异**；`rules_tau0.4/0.2.json` 剥离 `_compile_meta` 计时元数据后**逐字节一致**；性能字段（µs/吞吐/CPU/RSS）随运行波动，量级与 E4 报告一致 |
| 10 | 编译规则与训练版本对应 | 规则文件 `lineage` 字段（detector=E2-D, gate=E3-B, 协议 30/20/50 种子 42, τ∈{0.4,0.2}） | ✅ |
| 11 | 文档未将 E1/E4 描述为已合并 | README 版本索引 + decisions D2/谱系说明 | ✅ 明确标注"E1 与 E4 均未合并到任何稳定分支"；E3 分支中的 E1 运行器为前置检查副本，非合并 |
| 12 | 稳定分支与 PIPLUP 核心未变 | `git diff 419164df..各分支 -- src/ config/ main.py test/ eval_pipeline/` | ✅ 五个实验分支全部为空 diff（仅改动 bench/ 与 docs/rep/header-detector-v11/） |

## 2. 复现执行摘要

```
worktree: /Users/hmonky/Downloads/header-v11-archive  (exp/header-v11-archive @ a2898a02)
python3 bench/header_ablation.py    → exit 0, 结果与提交版本逐字节一致
python3 bench/header_adaptive.py    → exit 0, 结果与提交版本逐字节一致
python3 bench/header_span.py        → exit 0, 质量指标零差异（仅计时波动）
python3 bench/header_gate.py        → exit 0, 质量指标零差异（仅计时波动）
python3 bench/header_e4.py          → exit 0, 等价性与规则内容一致（仅计时/RSS 波动）
```

"逐字节一致"判定依据：`git diff` 对结果 JSON 为空。
"仅计时波动"判定依据：diff 行全部为 `*_ns_*` / `lines_per_s` /
`cpu_ns_per_line` / `wall_s` / `*_seconds` / `*_rss_*` 类字段
（程序化字段类型统计，见验收过程记录）。

## 3. 远端分支定位（origin = github.com/HouMonKy/TIG-PIPLUP）

| 分支 | 提交 | 推送状态 |
|---|---|---|
| `exp/header-v11-repro` | `8fb2c985` | ✅ 已推送 |
| `exp/header-v11-adaptive` | `5b7533b7` | ✅ 已推送 |
| `exp/header-v11-span` | `811914c6` | ✅ 已推送 |
| `exp/header-v11-gate` | `63eb7063` | ✅ 已推送 |
| `exp/header-v11-runtime` | `a2898a02` | ✅ 已推送 |
| `exp/header-v11-archive` | （本提交） | ✅ 归档提交后推送 |

## 4. 未提交内容核查

- 五个实验 worktree 的已跟踪文件均干净（无未提交修改）。
- 主工作目录（TIG-PIPLUP）的未跟踪文件（`TIG-RUST/`、`analysis/`、
  `piplup实验/` 等）为**归档任务前即存在的用户文件**，与
  HeaderDetector 实验无关，未纳入归档、未做改动。
- E4 在主工作目录的 `exp/header-v11-runtime` 分支上完成且已全部
  提交（`a2898a02`），无遗留。

## 5. 剩余风险与说明

1. **性能数字的机器相关性**：E2/E3/E4 的性能指标随硬件/负载波动
   （验收运行即有 ±5–10% 漂移）；跨机器引用性能结论需重测，
   质量结论与机器无关（已证逐字节/零差异）。
2. **Loghub 数据许可**：公开研究基准，随分支保存约 3.7 MB；
   公开分发时需注明 Loghub 出处并遵循其原始许可（BGL/Thunderbird
   等 IBM/SDSC trace 条款）。
3. **E1 结果的参照系差异**：E1 分支的原始结果基于修复前 M3 与
   22 数据集；修复版重跑结果在归档分支 `e1-results/`。两者均在，
   引用时按 `results-manifest.md` §E1 说明区分。
4. **挑战集 GT 前导空格怪癖**（39 行空白级 over，零内容损失）：
   已在指标中分离（content_over_rate），GT 修正留待 E5 前
   （decisions.md D12）。
5. **E4 `_compile_meta` 计时字段**嵌入规则文件——规则内容确定性
   已验证，但未来规则文件 diff 会包含计时噪声；建议（不强制）
   后续版本把计时移出规则文件。
