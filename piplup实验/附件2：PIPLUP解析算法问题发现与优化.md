# PIPLUP 解析算法问题发现与优化
---
## 一、问题发现：半括号残留

### 1.1 现象

PIPLUP 合并模板时，产生 `<*>)` 这样的半括号残留，正确结果应为 `(<*>)` 或 `<*>`。

**典型簇 MP007915**（4,485 行，35 种 tracepoint 名称）：

| 版本 | 输出模板 | 正确性 |
|------|---------|--------|
| v1 PIPLUP | `Search <*>) failed, ret(<*>).` | 错误：`<*>)` 缺少匹配的 `(` |
| v2 PIPLUP | `Search tracepoint(<*>) failed, ret(<*>).` | 正确 |

**全量影响**：v1 输出中无匹配 `(` 的 `<*>)` 涉及 15,687 行；v2 降至 1,831 行（-88.3%）；经后处理修复后降至 0 行（-100%）。

### 1.2 根因：原始合并逻辑只保留连续分隔符的 head/tail

**原始代码**（`PIPLUP_original.py` line 86-104）：

```python
# head: 从前向后，只保留连续的分隔符
for i in range(min_length):
    char = t2[i]
    if char not in path_delimiters: break   # 遇到非分隔符即停
    if t1[i] != char: break
    head += char

# tail: 从后向前，只保留连续的分隔符
for i in range(min_length):
    char = t2[-i-1]
    if char not in path_delimiters: break   # 遇到非分隔符即停
    if t1[-i-1] != char: break
    tail = char + tail

var = head + '<*>' + tail
```

**逻辑本质**：head/tail 只保留两 token 首尾**连续且相同**的分隔符，遇到第一个非分隔符或首个不匹配字符即停。

**以 MP007915 为例**，两条日志经预处理后 position-1 的 token 分别为：

```
t1 = tracepoint(CM_PUT_BQUEUE_FAILED_392)
t2 = tracepoint(TP_IPC_SERVER_CTX_ALLOC_FAIL)
```

原始合并逻辑处理过程：

```
head 扫描：t1[0]='t', t2[0]='t'
  → 't' 不是分隔符（path_delimiters 中无 't'）
  → head 直接为空，扫描从未开始

tail 扫描：t1[-1]=')', t2[-1]=')'
  → ')' 是分隔符
  → t1[-1] == t2[-1]（都是 ')'）
  → tail = ')'
  → t1[-2]='2' 不是分隔符，停止

结果：head + '<*>' + tail = '' + '<*>' + ')' = '<*>)'  ← 半括号！
```

### 1.3 正确的因果链

```
原始 PIPLUP 合并逻辑
  ↓
head 只保留连续分隔符前缀（遇到非分隔符即停）
tail 只保留连续分隔符后缀（同理）
  ↓
对任何「非分隔符开头的 token 对」，head 恒为空
  ↓
若两 token 末尾恰好共享分隔符后缀（如 ')'），
tail 保留该后缀
  ↓
head='' + <*> + tail=')' → <*>)  ← 半括号
  ↓
<*>) 作为中间 token 参与后续合并
  ↓
<*>) vs tracepoint(XXX) → head='' tail=')' → <*>)  ← 半括号传播
```

### 1.4 Merge Trace 证据（MP007915）

通过在独立实验目录注入 trace 日志，记录 `updateSequence()` 每轮合并的 lhs/rhs/结果，获得完整证据链：

| 轮次 | lhs | rhs | merged | 半括号? |
|------|-----|-----|--------|---------|
| #1 | `tracepoint(CM_PUT_BQUEUE_FAILED_392)` | `tracepoint(TP_IPC_SERVER_CTX_ALLOC_FAIL)` | `<*>)` | YES |
| #2 | `<*>)` | `tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392)` | `<*>)` | YES |
| #3 | `<*>)` | `tracepoint(TP_IPC_SERVER_MAGIC_FAIL)` | `<*>)` | YES |
| #4-#19 | `<*>)` | `tracepoint(...)` | `<*>)` | YES |

- **v1 原始合并**：19 次合并，全部产生半括号，半括号一旦产生便持续传播
- **v2 结构感知合并**：1 次合并即收敛，结果为 `tracepoint(<*>)`，无半括号

### 1.6 产生半括号的两类场景

**场景 1：非分隔符开头的 token 对（head 从未开始）**

```
tracepoint(X) vs tracepoint(Y)
→ head: ''（'t' 不是分隔符）
→ tail: ')'（')' 是分隔符，两 token 末尾都是 ')'）
→ <*>)  ← 半括号
```

```
working(X) vs creating(Y)
→ head: ''（'w' 不是分隔符）
→ tail: ')'（')' 是分隔符）
→ <*>)  ← 半括号
```

**场景 2：半括号传播（中间 token 参与后续合并）**

```
<*>) vs tracepoint(XXX)
→ head: ''（'<' 不是分隔符）
→ tail: ')'（')' 是分隔符）
→ <*>)  ← 半括号持续传播
```

两类场景的共同特征：**merge boundary 落在 `(` 和 `)` 之间，`(` 进入 wildcard 区间，`)` 被 tail 保留。**

---

## 二、优化措施

### 2.1 结构感知合并（_structure_aware_merge）—— 从根因消除半括号

**改动文件**：`src/data_process/template_extractor/parsing_algorithm/PIPLUP.py`（第 25-97 行）

**算法设计**：

```
输入：t1, t2（待合并的两个模板 token），delimiters（分隔符集合）
输出：合并后的模板 token

1. 计算 LCP（最长公共前缀）→ 回退到最近的分隔符边界 → head
2. 计算 LCS（最长公共后缀）→ 前进到最近的分隔符边界 → tail
3. 括号平衡检查：统计 head 中开括号 `([{`，tail 中闭括号 `)]}`
   若 闭括号 > 开括号 → 从 tail 前部逐字移除多余闭括号
4. 括号缺额修复：若 开括号 > 闭括号（head 中有未闭合的 `(`）
   → 沿 t1 从 head 末尾向后扫描，找到深度归零的位置（匹配的 `)`）
   → 将 tail 扩展到从该 `)` 开始
5. 重叠保护：若 len(head) + len(tail) >= min(len(t1), len(t2)) → 退化为 <*>
6. 返回 head + '<*>' + tail
```

**以 MP007915 为例**，同一对 token：

```
t1 = tracepoint(CM_PUT_BQUEUE_FAILED_392)
t2 = tracepoint(TP_IPC_SERVER_CTX_ALLOC_FAIL)

Step 1: LCP = 11（'tracepoint('），回退到分隔符边界
  → lcp_len=11, t1[10]='(' 是分隔符 → head_end=10 → head='tracepoint('

Step 2: LCS = 1（')'），前进到分隔符边界
  → lcs_len=1, t1[-1]=')' 是分隔符 → tail_start=len(t1)-1 → tail=')'

Step 3: 括号平衡
  → head 含 1 个 '('，tail 含 1 个 ')'，平衡，无需调整

Step 4: 括号缺额修复
  → 开括号=1 = 闭括号=1，无需修复

Step 5: 重叠保护
  → len(head)+len(tail)=12+1=13 < min(37,37)=37，不触发

结果：'tracepoint(' + '<*>' + ')' = 'tracepoint(<*>)'  ← 正确！
```

**括号缺额修复示例**（`_structure_aware_merge` Step 4 独有）：

```
t1 = (Lwp:4c70)N_DATA_0_0_0_0:query
t2 = (Lwp:8f32)N_DATA_0_1_0_0:query

Step 1: LCP = 5（'(Lwp:'），回退到分隔符边界
  → t1[4]=':' 是分隔符 → head='(Lwp:'

Step 2: LCS from end: ':query' + '_0' = 8 字符（在 '0' vs '1' 处分歧）
  → tail_start_from=22（落在 N_DATA_ 内部）
  → 从位置 22 向前找分隔符：'_'不是分隔符 → 跳过 → ':' 是分隔符 → tail=':query'

Step 3: 括号平衡
  → head 含 1 个 '('，tail 含 0 个 ')' → 无多余闭括号

Step 4: 括号缺额修复（开括号 1 > 闭括号 0）
  → head 之后的深度 depth=1（1 个未闭合的 '('）
  → 沿 t1 从位置 5 扫描：4c70 → depth 不变
  → 位置 9: ')' → depth=0！
  → tail 扩展为 t1[9:] = ')N_DATA_0_0_0_0:query'

Step 5: 重叠保护
  → len(head)+len(tail)=5+21=26 < 30，不触发

结果：'(Lwp:' + '<*>' + ')N_DATA_0_0_0_0:query' = '(Lwp:<*>)N_DATA_0_0_0_0:query'  ← 正确！
```

**对比**：

```
working(X) vs creating(Y)

v1 原始合并：
  head='' → tail=')' → <*>)  ← 半括号

v2 结构感知合并：
  LCP=0 → head=''
  LCS=1(')') → tail=')'
  括号平衡：head 含 0 个 '('，tail 含 1 个 ')'，excess=1
  → 从 tail 移除 1 个 ')' → tail=''
  结果：'<*>'  ← 正确！（不保留无匹配的 ')'）
```

**修复机制总结**：

| 机制 | 解决的问题 | 示例 |
|------|-----------|------|
| LCP/LCS 回退到分隔符边界 | 非分隔符开头的 token 对，head 从未开始 | `tracepoint(X)` vs `tracepoint(Y)` → head=`tracepoint(` → `tracepoint(<*>)` |
| 括号平衡检查 | head 不含 `(` 但 tail 含 `)` | `working(X)` vs `creating(Y)` → 移除多余 `)` → `<*>` |
| 括号缺额修复 | head 含 `(` 但 tail 不含匹配的 `)`（LCS 过短导致 tail 前进跳跃越过 `)`） | `(Lwp:4c70)N_DATA...:query` vs `(Lwp:8f32)N_DATA...:query` → `(Lwp:<*>)N_DATA_0_0_0_0:query` |
| 重叠保护 | head+tail 覆盖整个 token | 防止极端情况下的结构破坏 |

**括号缺额修复的根因**：当两个 token 的公共后缀 LCS 较短（因中间段内容不同）时，tail 的起点落在非分隔符区域内部（如 `N_DATA_0_0_0_0`），前进跳跃到下一个分隔符时越过了 `)` 到了 `:`，导致 `)N_DATA_...` 被 `<*>` 吞掉。括号缺额修复通过检测 head 中未闭合的 `(`，沿 t1 找到匹配的 `)` 并将 tail 扩展到从该 `)` 开始，确保括号结构完整。

### 2.2 后处理半括号修复 —— 兜底修复所有半括号模式

**改动文件**：`src/data_process/template_extractor/parsing_algorithm/postprocessing.py`

`_structure_aware_merge` 已从根因上消除合并阶段的半括号，但作为防御编程，在 `correct_single_template()` 中新增两层后置修复，对任何其他代码路径可能产生的 `<*>)` 模式做兜底。

#### 原版代码的问题

原版 `correct_single_template()` 完全没有半括号处理逻辑，CV 规则（合并连续 `<*><*>` 为 `<*>`）和 LUNAR 格式清理之后直接 `return template`，半括号 `<*>)` 原样输出。

#### 修复后代码

在 `correct_single_template()` 的 CV 规则之后、return 之前，新增两层修复：

**1. 新增正则修复**（第 144 行）：处理 `identifier<*>)` 模式

```python
# 兜底：修复 identifier<*>) → identifier(<*>) （半括号修正）
template = re.sub(r'([a-zA-Z_]\w*)<\*>\)', r'\1(<*>)', template)
```

修复 `func<*>)` → `func(<*>)` 这类 identifier 后直接跟 `<*>)` 的模式。

**2. 新增模板级括号平衡修复** `_fix_bracket_balance()`（第 11-64 行），在 `correct_single_template()` 末尾调用（第 184 行）：

```
输入：template（可能含半括号的模板字符串）
输出：修复后的模板

1. 将 <*> 等占位符替换为 \x00（单字符），避免占位符内部括号干扰
2. 逐字符扫描，维护三种括号的深度计数器
3. 遇到未匹配的关闭括号时，向前找最近的占位符，在其前面插入对应的开启括号
4. 还原占位符并返回
```

**示例**：

```
修复前：Search <*>) failed
  → flat = Search \x00) failed
  → 扫描：')' 无匹配 '('，在 \x00 前插入 '('
  → 还原：Search (<*>) failed  ← 正确

修复前：Port:<*>) link
  → 插入 '(' → Port:(<*>) link  ← 正确

已平衡不变：FS:(<*>) success
  → '(' 和 ')' 配对，无需修复  ← 正确

方括号：data<*>]
  → 插入 '[' → data[<*>]  ← 正确

花括号：obj<*>}
  → 插入 '{' → obj{<*>}  ← 正确
```

#### 两层修复的覆盖范围

| 模式 | 行数 | 正则覆盖? | 括号平衡覆盖? |
|------|------|-----------|--------------|
| `identifier<*>)` | 少量 | YES | YES |
| `key:<*>)` | 337 | NO | YES |
| bare `<*>)` | 1,387 | NO | YES |
| `key=<*>)` | 少量 | NO | YES |

正则修复专门处理 `identifier<*>)` 这种高频且结构清晰的模式（精确替换为 `identifier(<*>)`）；`_fix_bracket_balance()` 则通用覆盖所有半括号模式，无论 `<*>)` 前面是什么字符。

---

## 三、问题发现：预处理遗漏下划线连接复合数值导致簇碎片化

### 3.1 现象

PIPLUP 输出中存在大量仅因数值参数不同而分裂的模板簇。典型示例：

| 簇 ID | 模板 | 行数 |
|--------|------|------|
| 182 | `ZONE_CAP:TYPE:<*> Arg:0_0_5 WL:<*> Cap:<*> {<*>:<*>}.` | 少量 |
| 183 | `ZONE_CAP:TYPE:<*> Arg:0_1_5 WL:<*> Cap:<*> {<*>:<*>}.` | 少量 |
| 619 | `ZONE_CAP:TYPE:<*> Arg:0_2_5 WL:<*> Cap:<*> {<*>:<*>}.` | 少量 |
| 620 | `ZONE_CAP:TYPE:<*> Arg:0_3_5 WL:<*> Cap:<*> {<*>:<*>}.` | 少量 |

这4个簇语义完全相同，唯一差异是 `Arg:` 后的参数值（`0_0_5` / `0_1_5` / `0_2_5` / `0_3_5`）。

**全量统计**：
- `Cap:` 后的复合数值有 34 种不同值 → 至少 34 个碎片簇
- `Arg:` 后的复合数值有 8 种不同值 → 至少 8 个碎片簇
- 大括号内 `{hex:hex_N_N}` 有 99 种不同值 → 额外碎片簇

### 3.2 根因：`\b` word boundary 与下划线的交互

PIPLUP 预处理中 `numerical` 正则负责替换数字：

```python
'numerical': r'\b(\-?\+?\d+\.?\d*)\b|\b0[Xx][a-fA-F\d]+\b|\b[a-fA-F\d]{4,}\b',
```

关键约束：`\b` 要求匹配位置两侧分别是 `\w`（word character）和 `\W`（non-word character）。

**但 `_`（下划线）属于 `\w`！** 因此：

```
Cap:15749414426_16561907771_414286279520
    └── 整段是一个 \w word ──┘

\b\d+\b 需要匹配纯数字段，但数字段之间的 _ 阻止了 \b 形成：
  '6' 和 '_' 之间：6 ∈ \w，_ ∈ \w → 无 \b → \b\d+\b 无法匹配 '15749414426'
```

实际验证：

```python
>>> re.findall(r'\b\d+\b', 'Cap:15749414426_16561907771_414286279520')
[]  # 完全匹配不到！

>>> re.findall(r'\d+', 'Cap:15749414426_16561907771_414286279520')
['15749414426', '16561907771', '414286279520']  # 不带 \b 可以，但拆成了3段
```

**后果**：这些复合值作为整体 token 进入 PIPLUP 聚类，不同的 Cap/Arg 值无法归一，产生碎片簇。

### 3.3 同步问题：`_light_digit_re` 缓存归一化不充分

PIPLUP.py 中的轻量归一化正则也存在同样问题：

```python
# 修改前
_light_digit_re = re.compile(r'\d+')  # 只替换单独的数字段
```

对 `Cap:15749414426_16561907771_414286279520`：

```
light = _light_digit_re.sub('0', content)
→ Cap:0_0_0  （3段数字各自替换为 '0'，但整体结构不同导致 CRC32 仍不同）
```

不同 `Cap:` 值的 light form 不同 → CRC32 不同 → 缓存未命中 → 重复走预处理+解析路径。

### 3.4 修复

#### 修复 1：预处理新增 `underscore_composite_num` 和 `brace_hex_value_pair`

**改动文件**：`src/data_process/template_extractor/parsing_algorithm/preprocessing.py`

**新增正则**：

```python
# 15. 下划线连接的复合数值：Cap:NUM_NUM、Arg:D_D_D 等 Key:Value 结构
# 作用目标：Cap:15749414426_16561907771_414286279520、Arg:0_2_5
# 工程逻辑：下划线 _ 属于 \w，导致 \b 无法在数字段之间形成 word boundary，
#          numerical 正则的 \b\d+\b 对这些复合值完全失效。
#          用 (?<=[A-Za-z]:) 后行断言限定 Key:Value 结构，避免误替换独立数字。
'underscore_composite_num': r'(?<=[A-Za-z]:)\d+(?:_\d+)+',

# 16. 大括号内十六进制:数值对：{hex:hex_N_N}
# 作用目标：{165879a000:165879a000_0_0}{133fe97aee:133fe97aee_0_0}
# 工程逻辑：numerical 的 \b[a-fA-F\d]{4,}\b 只替换冒号前的 hex 部分，
#          冒号后的 hex_N_N 因下划线连接而残留。整对替换避免碎片化。
'brace_hex_value_pair': r'\{[0-9a-fA-F]+:[0-9a-fA-F]+(?:_\d+)+\}',
```

**插入位置**：`sequence` 中 `numerical` 之前（先匹配更具体的模式）：

```python
sequence = [..., 'underscore_composite_num', 'brace_hex_value_pair', 'url', ..., 'numerical', ...]
```

**替换效果**：

```
原: ZONE_CAP:TYPE:2 Arg:0_3_5 WL:6 Cap:10549177244_10799201990_414286279520 {113ea70000:113ea70000_0_0}{e996679df:e996679df_0_0}{3078da85:3078da85_0_0}.
后: ZONE_CAP:TYPE:<*> Arg:<*> WL:<*> Cap:<*> {<*>:...} 残留 → brace_hex_value_pair 整体替换 → <*>

最终: ZONE_CAP:TYPE:<*> Arg:<*> WL:<*> Cap:<*>. 
```

| 模式 | 修复前 | 修复后 |
|------|--------|--------|
| `Cap:15749414426_16561907771_414286279520` | 原样保留（34种值→34碎片簇） | `Cap:<*>` |
| `Arg:0_2_5` | 原样保留（8种值→8碎片簇） | `Arg:<*>` |
| `{113ea70000:113ea70000_0_0}` | `{<*>:113ea70000_0_0}` 残留 | `<*>` 整体替换 |

**不误触验证**：

| 输入 | 结果 | 说明 |
|------|------|------|
| `foo 123_456 bar` | 原样保留 | 无 `Key:` 前缀，不匹配 |
| `id:42` | `id:<*>`（由 numerical 处理） | 单数字不受影响 |
| `aa_user_manage.c:AA_Add` | 原样保留 | `AA_Add` 不匹配 `\d+` |

#### 修复 2：`_light_digit_re` 扩展

**改动文件**：`src/data_process/template_extractor/parsing_algorithm/PIPLUP.py`

```python
# 修改前
_light_digit_re = re.compile(r'\d+')

# 修改后
_light_digit_re = re.compile(r'(?=[\d\s.,;\-/+_]*\d)[\d\s.,;\-/+_]+')
```

**设计要点**：

- 匹配连续的「数字+分隔符+下划线」序列，要求至少含一个数字（前瞻断言 `(?=...\d)`）
- 冒号 `:` 不纳入——它是 `Key:Value` 分隔符（`Cap:`、`Arg:`），应保留作为 CRC32 区分键
- 使不同 `Cap:` 值的 light form 相同 → CRC32 相同 → 缓存命中

```
修复前：Cap:15749414426_16561907771_414286279520 → Cap:0_0_0（3个0，不同值仍不同）
修复后：Cap:15749414426_16561907771_414286279520 → Cap:0（整体归一化，所有值相同）
```