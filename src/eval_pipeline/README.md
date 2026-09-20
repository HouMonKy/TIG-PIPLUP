# eval_pipeline — 日志模板解析质量评估流水线

对同一批日志运行**两个版本的日志模板解析算法**，通过行级对齐 + LLM 裁决生成版本中立的
**Ground Truth（GT）工件包**，并据此输出各算法的解析质量指标（PA / GA / FTA / FGA）。

核心设计原则：

- **簇不参与对比**：两个算法的簇数量、边界、编号都是实现细节，一切对比在**行级**
  （每行日志 → 每行的模板）完成，天然规避"簇数不一致"与"同日志分到不同簇"的问题
- **GT 对两个贡献版本无偏**：一致行共同背书、不一致行 LLM 裁决、等价行双变体均值、
  两版同错行合成修正/人工兜底——每条 GT 行都有明确的来源与置信度
- **metrics 不可变**：指标计算（`_shared/metrics.py`）为冻结基准，所有修复与优化
  只发生在 GT 构建、运行器与工程层

---

## 模块与执行顺序

| 模块 | 子命令 | 输入 | 输出 | 说明 |
|---|---|---|---|---|
| **1 数据生成** | `data` | `.log` 文件/目录 | `data.jsonl`（`{rawLog, module, logId}`） | 统一数据格式；`logId` 是全链路行对齐键 |
| **2 性能对比** | `compare` | `data.jsonl` + 算法模块路径 | `algo_comparison_report.txt` / `_raw.json` | 吞吐/耗时/CPU/RSS/模板数/簇数；独立可选 |
| **3 GT 生成** | `gt` | `data.jsonl` + 两版算法模块 + LLM 配置 | GT 工件包 + `eval_report.json` | 核心模块：行级对齐 → LLM 裁决 → 双变体 GT → 自动评估 |
| **4 答案评估** | `answer` | `data.jsonl` + **冻结的 GT** + 任意算法模块 | `eval_report.json` | GT 冻结后评估第三方版本（对 GT 无投票权，无主场优势） |

> 模块编号即执行顺序。模块 2 独立可选；模块 3 结束时自动完成两版的质量评估，
> 模块 4 面向"GT 冻结后评估新版本"的场景。

## 快速开始（两条命令）

```bash
# 1. 数据准备（一次性）
python -m eval_pipeline data --input <日志目录> --output data.jsonl

# 2. 质量结论：GT 生成 + 两版评估（一条命令）
python -m eval_pipeline gt --input data.jsonl \
    --piplup-orig        src.data_process.template_extractor.parsing_algorithm.PIPLUP_original \
    --preprocessing-orig src.data_process.template_extractor.parsing_algorithm.preprocessing_original \
    --postprocessing-orig src.data_process.template_extractor.parsing_algorithm.postprocessing_original \
    --piplup-opt         src.data_process.template_extractor.parsing_algorithm.PIPLUP \
    --llm-config llm.yaml --output gt/ --live \
    --concurrency 3 --audit-consistent 50
```

gt 结束后 `gt/` 下即有全部结论：`eval_report.json`（两版 PA/GA/FTA/FGA）、
`gt_summary.json`（覆盖率/裁决分布/审计错误率）、`final_gt.jsonl`（GT 工件）。

## GT 构建逻辑

### 行级对齐

对齐键是 `logId`。对每行日志比较两版给出的模板（经 `normalize_template` 归一化）：

- 相同 → **一致行**，直接进 GT
- 不同 → 按（原版模板, 优化版模板）对聚合为**裁决任务**（TaskID = 内容哈希，跨运行稳定）

### 组装优先级（每个任务簇）

```
1. 人工标注（human）        → GT = 人工模板（confidence=1.0）      ← 最高优先级
2. 审计修正（audit）        → GT = LLM 合成并通过程序校验的模板
3. LLM 裁决 original        → GT = 原版模板
4. LLM 裁决 optimized       → GT = 优化版模板
5. LLM 裁决 equivalent      → 双变体（主=原版 / 备=优化版，评估取均值）
6. LLM 裁决 neither + 合成  → 程序校验（覆盖全部样本 + 固定词≥2）通过 → GT = 合成模板
7. 其余（pending/failed/insufficient/合成未通过）→ excluded_lines，不计入 GT
```

设计意图：

- **等价行双变体**：LLM 判"两者等价"意味着两个模板都是合法 GT，挑谁都有偏。
  生成主/备两个 GT 变体，评估各算一轮取均值——等价行上两版 PA 各得 0.5，对称无偏
- **neither 合成 + 程序校验**：两版都被否时，LLM 基于 SampleLogs 合成正确模板；
  程序校验（线性通配匹配覆盖全部样本 + 固定词 ≥2 防垃圾模板）通过才进 GT，
  否则排除——两版 PA 同为 0 是正确语义（都被否决了）
- **一致行抽样审计**（`--audit-consistent K`）：两版一致的行存在"一起错"的盲区
  （互相背书）。抽样让 LLM 独立复核，产出**共识错误率**（GT 可信度指标）；
  判 incorrect 时同步合成修正模板，程序校验通过则回填 GT
- **人工标注兜底**：所有自动机制都无法解决的行汇入 `human_review_tasks.jsonl`，
  人工填写后经 `--human-annotations` 回填，优先级高于一切自动裁决

### 双变体均值与指标口径

| 指标 | 逐行问题 | 对什么敏感 |
|---|---|---|
| PA | 该行模板文本（归一化后）== GT 模板文本？ | 模板措辞差异 |
| GA | 该行所属的行集合划分 == GT 的划分？（LineId 集合比较，与簇编号无关） | 分组边界：过合并/过分裂 |
| FTA | 去重模板集合与 GT 模板集合的查准/查全 | 多产/缺失的模板 |

- 全部指标从**行级表**计算，簇数从头到尾不出现——两个算法簇数不同不影响任何环节
- 双变体下每指标取 `(主变体 + 备变体) / 2`；GA 的划分在两变体间相同，均值不改变语义
- 指标实现见 `_shared/metrics.py`（冻结基准，复制自 `src/evaluation/ParserMetrics.py`）

## GT 工件清单（`--output gt/` 目录）

| 文件 | 内容 |
|---|---|
| `runs/piplup_{orig,opt}_result.jsonl` | 两版行级解析结果（`--reuse-runs` 复用） |
| `runs/run_meta.json` | 输入签名（input/module/scenarios），复用时校验 |
| `line_pairs.jsonl` | 行级对齐明细（logId → 模板对 → TaskID → 是否一致） |
| `tasks/all_gt_tasks.jsonl` | 裁决任务总账（含归一化模板对与样本行） |
| `results/resolved|failed|raw_*.jsonl` | 裁决留痕（append-only，含原始 LLM 响应） |
| `final_gt.jsonl` | 主 GT（每行：logId/template/template_id/adjudication/confidence/reason/TaskID） |
| `final_gt_alt.jsonl` | 备变体 GT（仅等价行与主变体不同） |
| `excluded_lines.jsonl` | 排除行及原因（pending / adjudication_failed / insufficient_evidence / neither_invalid_proposal / audit_synthesis_failed / no_counterpart） |
| `consistent_audit.json` | 一致行审计：抽样数/错误率/修正明细 |
| `human_review_tasks.jsonl` | 人工标注工作清单（排除行 + 审计未修复簇，附样本与候选） |
| `eval_report.json` | 两版 vs GT 的 PA/GA/FTA/FGA（双变体均值） |
| `gt_summary.json` | 总汇总（见下） |

`gt_summary.json` 关键字段：

```json
{
  "gt_lines": 17450, "gt_coverage": 0.94,        // GT 覆盖率：结论外推的边界
  "consistent": 16000, "inconsistent_lines": 1450,
  "adjudication_distribution": {"original": 300, "optimized": 500, "equivalent": 600, "neither": 50},
  "human_annotations_applied": 120,               // 人工标注生效数
  "audit_corrections_applied": 18,                // 审计修正回填数
  "consistent_audit": {"error_rate": 0.01, ...}   // 共识错误率（GT 可信度）
}
```

## 人工标注工作流

```bash
# 1. 运行 gt 后，若 gt_summary.gt_coverage < 1.0，
#    人工标注清单已自动生成：gt/human_review_tasks.jsonl
#    每项含 TaskID / 原因 / 样本日志 / 候选模板（original / optimized / 被拒合成）

# 2. 人工填写标注文件（jsonl，每行一条）：
#    {"TaskID": "GT_AB12...", "template": "正确的模板", "annotator": "姓名"}

# 3. 回填（复用解析结果，秒级完成，不再调用 LLM）：
python -m eval_pipeline gt --input data.jsonl \
    --piplup-orig ... --preprocessing-orig ... --postprocessing-orig ... \
    --piplup-opt ... --output gt/ \
    --human-annotations human_annotations.jsonl --reuse-runs --no-eval
```

- 标注按 **TaskID** 生效，优先级高于一切自动裁决（含 LLM 裁决与合成）
- 回填后如需更新评估报告：重跑 answer 或 gt（不带 --no-eval）即可

## CLI 参考

### `gt`（模块3）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | 必填 | JSONL 或 .log 目录 |
| `--piplup-orig` / `--piplup-opt` | 必填 | 两版 PIPLUP 模块路径（import 路径） |
| `--preprocessing-orig` / `--postprocessing-orig` | 可选 | 原版预处理/后处理模块路径——**三文件差异（pre/post/PIPLUP）都会被正确应用**（改绑机制不依赖 import 顺序；token_structure 仅优化版引用，无需处理） |
| `--llm-config` | 可选 | LLM YAML 配置（`base_url/model/api_key[_env]/temperature/max_output_tokens/no_proxy_hosts/verify_ssl`） |
| `--live` / `--dry-run` | mock | `--live` 调真实 API；`--dry-run` 仅生成 Prompt |
| `--concurrency` | 1 | 裁决线程数（>1 线程池；限流自动等待恢复） |
| `--limit` / `--retry-failed` | 0 / off | 裁决任务数上限 / 重试失败任务 |
| `--save-prompts` | off | live 模式也落盘 Prompt（dry-run 恒落盘） |
| `--audit-consistent K` | 0 | 一致行抽样审计数量（0=关闭） |
| `--no-eval` | off | 跳过自动评估 |
| `--human-annotations` | 可选 | 人工标注回填文件 |
| `--reuse-runs` | off | 复用 runs/ 跳过重复解析（标注回填场景） |
| `--warm-start` | off | 预热模式（默认冷启动清缓存） |
| `--scenarios` / `--data-root` / `--module` | - | 场景目录模式参数 |

### `answer`（模块4）

- `--parsers a,b,c`：多算法评估（名称取模块名末段）；或双版模式
  `--piplup-orig` + `--piplup-opt`（可配 `--reuse-runs`）
- `--gt` 主 GT；`--gt-alt` 备变体（默认自动探测同目录 `final_gt_alt.jsonl`）

## 设计保证与已知限制

### 保证

1. **行级坐标系**：GT 生成与指标计算全程以行对齐，簇数差异不影响任何结论
2. **GT 来源可追溯**：每条 GT 行携带 `adjudication`（consistent/human/original/
   optimized/equivalent/neither/audit_synthesized）与 `TaskID`（内容寻址），
   裁决原始响应全程留痕
3. **跨运行稳定**：TaskID 内容寻址，断点续跑/重复生成不会错位；`--reuse-runs`
   带输入签名校验
4. **GT 对贡献版本无偏**：等价行双变体均值、未裁决行排除而非默认原版
5. **结果确定性**：解析在同输入同配置下可复现；`--reuse-runs` 复用前校验签名

### 已知限制

1. **共识 GT 的天花板**：两版"一起错"的行，审计只能**度量**错误率（并通过合成
   修正部分修复），无法保证全部发现——错误率高说明需要人工标注或独立真值
2. **"原版 vs 优化版"存在主场效应**：两者都参与了 GT 投票（各自赢下的裁决行）。
   对新版本的评估（模块4）没有这个问题；对这两版的结论应结合审计错误率解读
3. **覆盖率 < 100% 时**，指标是"已裁决行"上的指标，外推全量需谨慎
4. `--reuse-runs` 模式下 `gt_summary` 的簇数统计为 0（未重新解析；可从
   上次 `runs/*_clusters.json` 读取）
5. 一致行审计为**抽样**（按模板簇取最大 K 个），未抽样簇的共识错误未被度量
6. LLM 裁决/合成质量依赖模型与 Prompt；`temperature=0` + 程序校验兜底，
   但建议保留 raw 响应抽查

## 测试

```bash
python -m pytest test/eval_pipeline_fix_test.py -v   # GT 组装/改绑/双变体/合成/人工标注
python -m pytest test/piplup_parsing_test.py -v      # PIPLUP 算法回归（含掩码台账）
```

- `test/eval_fixtures/`：三文件差异（pre/post/PIPLUP + token_structure）的桩模块，
  验证原版模块改绑机制（preprocessing 差异此前从未被处理，postprocessing 的
  monkey-patch 在 compare 模块 import 顺序下曾完全失效）
- `test/piplup_parser_compare.py`：GT 无关的两版本一致性对比工具
  （ARI / Pairwise F1 / 双向 GA / 重复行一致性 / 分歧块清单）
