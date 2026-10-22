# 接口规范：编译器、匹配器、数据结构、字符边界

## 1. Span 与字符边界（E2）

```python
@dataclass
class Span:
    text: str                 # 不变量: raw[start:end] == text（半开区间）
    start: int
    end: int
    last_close_end: int | None  # 最后一个深度0闭合括号组之后的位置

span_tokenize(line) -> list[Span]        # 完整行
SpanAwareTokenizer(max_spans=17, max_chars=256).tokenize(line)  # 前缀限定
```

字符级边界输出（E2-C，检测器层）：

```python
body_start, info = detector.predict_offset(raw_log)
# info = {"body_start_offset": int, "method": "span_aware",
#         "confidence": None, "span_boundary": b, "refined": bool}
body = raw_log[body_start_offset:]       # 直接切片，禁止重组 token
```

## 2. 在线匹配器（E4，生产接口）

```python
from bench.header_detector.e4_matcher import load_matcher

matcher = load_matcher("docs/rep/header-detector-v11/e4-results/rules_tau0.4.json")
result = matcher.match(raw_log, source)
# {
#   "action": "ACCEPT" | "KEEP",
#   "body_start_offset": int,     # KEEP 时为 0（保留原文）
#   "rule_id": "source#sig#combo" | None,
#   "reason": None | "unknown_source" | "unknown_signature"
#             | "tree_no_match" | "no_val_evidence" | "below_tau",
# }
if result["action"] == "ACCEPT":
    body = raw_log[result["body_start_offset"]:]
```

约定：`source` 为日志模块标识（生产=采集器元数据；实验=数据集名）；
来源名是安全边界的一部分（跨源误路由可发生精确结构碰撞剥离，
E4 报告 §6）。

## 3. 规则文件格式（header-rules/v1）

顶层：

```json
{
  "version": "v1.1-rules-v1",
  "tau": 0.4,
  "protocol": {"learn_frac": 0.3, "val_frac": 0.2, "seed": 42},
  "lineage": {"detector": "SpanM3Prefix(E2-D)", "gate": "E3-B(sig,combo)"},
  "sources": {"<source>": {"trees": [...], "flat_rules": [...],
                            "gate": {...}, "meta": {...}}}
}
```

- **trees[i]**：`{"signature": [4 个族类型], "root": 节点}`；
  节点 = `{"leaf": span下标}` 或 `{"split": {"pos": j, "kind":
  "family"|"value"|"kind", "children": {键: 节点}, "fallback": {...}}}`。
- **flat_rules[k]**：根→叶路径的有序条件序列（`len_le` / `value` /
  `kind` / `fam` / `strong` / `not_value` / `not_kind` /
  `fam_not_child` / `fam_other`）+ 叶边界（P1 顺序匹配用）。
- **gate**：键 `"sig|combo"`（combo = 4 布尔元组的字符串）→
  `{n, val_acc, val_over, stability, reliability, accept, modal_boundary}`。
- 边界不含固定字符偏移：叶 span 下标经运行时 span 解析 +
  `last_close_end` 精化得到字符偏移。

规则文件实例：`e4-results/rules_tau0.4.json`（58/96 放行）、
`rules_tau0.2.json`（72/96 放行）；215 树 / 151.4 KB 每套。

## 4. 编译器入口（E4-A）

```python
from bench.header_detector.e4_compiler import compile_ruleset
ruleset = compile_ruleset(tau=0.4, data_root=..., out_path=...)
# 输入固定：learn 拟合 E2-D（种子 42）+ val 门控统计 + E3 选定 τ
# τ 不可依测试集重选；上游演化走 重拟合→重编译 流水线
```

## 5. 评估数据结构与指标（E0 冻结口径）

逐行四分类：`correct / over_strip / under_strip / abstain`
（pred==0 且 gt>0 记 abstain，与欠剥严格区分）。

```python
evaluate(items, preds) -> {
  "boundary_accuracy", "over_strip_rate", "under_strip_rate",
  "abstain_rate", "auto_strip_coverage", "accepted_boundary_accuracy",
  "body_integrity_char", "body_integrity_line", "counts", "examples"}
```

E3 增补：`content_over_rate`（删除非空白正文字符的 over 行占比）、
`whitespace_only_over`；门控转移矩阵与拦截率/损失率见
`e3-results/e3_results.json` 的 `transitions` 字段。

## 6. GT 数据结构

- Loghub：`gt.py::LOGHUB_GT` 正则表（GT 版本 v1.1-rebuild），
  `body_start = 正则 match.end()`；覆盖率 13/14 ≥99.6%（TBird 99.65%）。
- 压力/挑战集：JSONL 每行 `{"line", "body_start", "format",
  "has_header"}`，GT 由 `gen_stress.py`（种子 42）/
  `gen_challenge.py`（种子 4242）构造时生成，可确定性重生成
  （归档验收已验证逐字节一致）。
