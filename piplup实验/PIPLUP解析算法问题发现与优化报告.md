## 一、问题发现：半括号残留

### 1.1 现象

PIPLUP 合并模板时，产生 `<*>)` 这样的半括号残留，正确结果应为 `(<*>)` 或 `<*>`。

**典型簇 MP007915**（4,485 行，35 种 tracepoint 名称）：

| 输出模板 | 正确性 |
|---------|--------|
| `Search <*>) failed, ret(<*>).` | 错误：`<*>)` 缺少匹配的 `(` |
| `Search tracepoint(<*>) failed, ret(<*>).` | 正确 |

**优化效果**
```
原日志：Search tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392) failed, ret(3).
修复前：Search <*>) failed, ret(<*>).
修复后：Search tracepoint(<*>) failed, ret(<*>).
```

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

### 1.3 因果链

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

### 1.5 产生半括号的两类场景

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
4. 重叠保护：若 len(head) + len(tail) >= min(len(t1), len(t2)) → 退化为 <*>
5. 返回 head + '<*>' + tail
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

Step 4: 重叠保护
  → len(head)+len(tail)=12+1=13 < min(37,37)=37，不触发

结果：'tracepoint(' + '<*>' + ')' = 'tracepoint(<*>)'  ← 正确！
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

**核心 invariant**：不能让 merge boundary 落在一对结构分隔符之间，使 `(` 进入 wildcard、而 `)` 单独保留在 tail。

**修复机制总结**：

| 机制 | 解决的问题 | 示例 |
|------|-----------|------|
| LCP/LCS 回退到分隔符边界 | 非分隔符开头的 token 对，head 从未开始 | `tracepoint(X)` vs `tracepoint(Y)` → head=`tracepoint(` → `tracepoint(<*>)` |
| 括号平衡检查 | head 不含 `(` 但 tail 含 `)` | `working(X)` vs `creating(Y)` → 移除多余 `)` → `<*>` |
| 重叠保护 | head+tail 覆盖整个 token | 防止极端情况下的结构破坏 |

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