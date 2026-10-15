# PIPLUP 解析算法问题发现与优化

## 问题发现

### 半括号问题

该问题会导致日志模板信息缺失。

**示例：**

**原日志：**Search tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392) failed, ret(3).

**piplup-python（原版）：**Search <\*>) failed, <\*>

**piplup-python（已修复版）：**Search tracepoint(<\*>) failed, ret(<\*>).



**原日志：**Get item section(GLOBAL) item name(DDP_HANG_ALARM_SW), cmd(256), ret(-1).

**piplup-python（原版）：**Get item <\*>) item <\*>), cmd(<\*>), <\*>

**piplup-python（已修复版）：**Get item section(<\*>) item name(<\*>), cmd(<\*>), ret(<\*>).



**原日志：**Move delayQ ckg to multilist failed ret(0x3000001) zoneName(DTOBJ_DP0x0_RV0x2_EC_2_DIF512) ckgId(0x47f1).

**piplup-python（原版）：**Move delayQ ckg to multilist failed ret(<\*>) <\*>) ckgId(<\*>).

**piplup-python（已修复版）：**Move delayQ ckg to multilist failed ret(<\*>) zoneName(DTOBJ\_<\*>_EC_2_DIF512) ckgId(<\*>).



**原日志：**ZoneName(DTOBJ_DP0x0_RV0x3_EC_2_DIF512) ckId(0xfffffffe).

**piplup-python（原版）：**<\*>) ckId(<\*>).

**piplup-python（已修复版）：**ZoneName(DTOBJ\_<\*>_EC_2_DIF512) ckId(<\*>).



**原日志：**Send node alarm, id(0x100) type(0x1-516), alarm(IoCardErrRepairSuccess-0x100f00d10040) act(1) state(39-PCIE_ERR_REPAIR_SUCCESS), alarm count(0).

**piplup-python（原版）：**Send node alarm, id(<\*>) type(<\*>), <\*>) act(<\*>) <\*>), alarm count(<\*>).

**piplup-python（已修复版）：**Send node alarm, id(<\*>) type(<\*>), alarm(<\*>) act(<\*>) state(<\*>), alarm count(<\*>).

### 参数名被吞

该问题会导致参数名被错误泛化，影响后续参数异常检测。

**示例：**

**原日志：**(PELAGODB) Clear all tables begain, db_name(op_dpId_0_kvsId_22_STASH).

**piplup-python（原版）：**(PELAGODB) Clear all tables begain, <\*>

**piplup-python（已修复版）：**(PELAGODB) Clear all tables begain, db_name(<\*>).



**原日志：**(PELAGODB) Start to reset db:op_dpId_0_kvsId_2_STASH

**piplup-python（原版）：**(PELAGODB) Start to reset <\*>

**piplup-python（已修复版）：**(PELAGODB) Start to reset db:<*>

### 负号未泛化

该问题会导致数值错误，影响后续参数异常检测。

**示例：**

**原日志：**PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:-1

**piplup-python（原版）：**PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:-<\*>

**piplup-python（已修复版）：**PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:<*>



**原日志：**(Lwp:4c70)DsgingSubLog is null!, ret: -1.

**piplup-python（原版）：**(Lwp:<\*>)DsgingSubLog is null!, ret: -<\*>.][cache][:WCK_parseForDsg,<*>][app_data,pid:<\*>,tid:<\*>

**piplup-python（已修复版）：**(Lwp:<\*>)DsgingSubLog is null!, ret: <\*>.



**原日志：**GMM: ###check db ret(-1).dpId(0), kvsId(25).

**piplup-python（原版）：**GMM: ###check db ret(-<\*>).dpId(<\*>), kvsId(<\*>).

**piplup-python（已修复版）：**GMM: ###check db ret(<\*>).dpId(<\*>), kvsId(<\*>).



**原日志：**[combine debug]hzdb encode fail:-2130172161, num(4 5),8192,40960,40862,oldValidBlkNum 4,negCmprs

**piplup-python（原版）：**[combine debug]hzdb encode fail:-<\*>, num(<\*>),<\*>,oldValidBlkNum <\*>,negCmprs

**piplup-python（已修复版）：**[combine debug]hzdb encode fail:<\*>, num(<\*>),<\*>,oldValidBlkNum <\*>,negCmprs

## 问题定位

### 问题概述

| 问题类型   | 原始日志（节选）                                          | 错误结果      | 预期结果          |
| ---------- | --------------------------------------------------------- | ------------- | ----------------- |
| 半括号问题 | `tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392)` | `<*>)`        | `tracepoint(<*>)` |
| 参数名被吞 | `db_name(op_dpId_0_kvsId_22_STASH)`                       | `<*>`         | `db_name(<*>)`    |
| 负号未泛化 | `failed:-1`                                               | `failed:-<*>` | `failed:<*>`      |

通过源码分析，三个问题可以归结为两个独立根因：

- **根因 A：路径合并时仅保留分隔符证据，未保留稳定的字母数字前后缀。** 导致半括号和参数名被吞。
- **根因 B：numerical 正则中的** `\b` **词边界与可选负号位置不合理。** 导致负号无法完整进入通配符。

### 问题一：半括号与参数名被吞

#### 源码定位

| 源码位置                                  | 作用                                        |
| ----------------------------------------- | ------------------------------------------- |
| `算法代码/PIPLUP_原版.py:84-106`          | `updateSequence()` 内联 head/tail 合并逻辑，问题根源 |
| `算法代码/PIPLUP_原版.py:53`              | `Logcluster.updateSequence()`，路径容量满后触发合并 |
| `算法代码/PIPLUP_原版.py:18-22`           | `path_delimiters` 分隔符集合，分隔符判定     |

#### 问题触发过程

PIPLUP 首先对日志进行正则预处理，然后按空白分词，利用完整 token 的相似度将日志归入已有簇或创建新簇。

进入同一簇后，若新日志不能被已有候选模板覆盖：

1. `pathList` 未达到 `br_thresh` 时，保留新的独立路径。
2. `pathList` 达到容量上限后，选择相似度最高的路径进行合并。
3. 对存在差异的 token 位执行 head/tail 合并，生成通配 token。
4. 合并结果写回 `pathList` 中所有路径的对应位置（`for p in self.pathList: p[pos] = var`）。

原版逻辑（`PIPLU.py:84-106`）：

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
    if char not in path_delimiters: break
    if t1[-i-1] != char: break
    tail = char + tail

var = head + '<*>' + tail
```

该逻辑要求公共前缀中的每个字符都必须是分隔符。遇到第一个字母或数字时立即停止，导致稳定字段名被整体丢弃。

#### 案例一：半括号问题

原始日志：

```text
Search tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392) failed, ret(3)
```

预处理后：

```text
Search tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392) failed, ret(<*>)
```

其中：

- `ret(3)` 中的数字 `3` 能够被 numerical 正则识别并替换。
- `CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392` 属于下划线连接的混合标识符，末尾 `392` 前面是下划线，不满足数字起始处的 `\b`，因此不会被该数值规则直接泛化。

当同族日志中出现多个不同的 tracepoint 值，并触发路径合并时：

```text
tracepoint(abc)
tracepoint(defgh)
```

原版合并过程：

- `head` 从首字符 `t` 开始检查。
- `t` 不是分隔符，因此 `head` 为空。
- `tail` 从末尾开始检查。
- `)` 是共同分隔符，因此得到 `tail = ")"`。

最终合并为：

```text
<*>)
```

可能产生的错误模板：

```text
Search <*>) failed, ret(<*>)
```

预期模板：

```text
Search tracepoint(<*>) failed, ret(<*>)
```

**问题本质：** `tracepoint(` 是稳定的字段结构，但原版算法没有保留字母数字构成的公共前缀，只保留了末尾右括号。

#### 参数名被吞

原始日志：

```text
(PELAGODB) Clear all tables begain, db_name(op_dpId_0_kvsId_22_STASH).
```

预处理后，`db_name` 内部的下划线混合标识符不能被 numerical 规则完整识别，仍然作为一个 token 保留。

假设后续出现同结构日志：

```text
(PELAGODB) Clear all tables begain, db_name(abc).
(PELAGODB) Clear all tables begain, db_name(defgh).
```

当路径容量达到上限并触发合并时，对差异 token 执行 head/tail 合并（`updateSequence()` 内联逻辑）：

```text
db_name(abc).
db_name(defgh).
```

原版合并过程：

- `head` 第一个字符为 `d`，不是分隔符，因此直接停止。
- `tail` 保留共同的末尾结构 `).`。
- 最终得到 `<*>).`。

可能产生的错误模板：

```text
(PELAGODB) Clear all tables begain, <*>).
```

预期模板：

```text
(PELAGODB) Clear all tables begain, db_name(<*>).
```

**问题本质：** `db_name(` 本应作为稳定的字段结构保留，但原版算法将其错误泛化，导致参数名丢失。

####  为什么后处理无法解决

PIPLUP 的后处理模块 `correct_single_template` 作用于路径合并后生成的模板字符串。

一旦 `tracepoint(` 或 `db_name(` 在 token 合并阶段被删除，后处理阶段就无法知道原始字段名，因此无法恢复。

所以该问题需要在 **token 路径合并层** 修复，而不是依赖后处理进行格式补偿。

### 问题二：负号未被泛化

#### 源码定位

| 源码位置                                | 作用                                  |
| ---------------------------------------- | ------------------------------------- |
| `算法代码/preprocessing_原版.py:22`      | `regex_match['numerical']`，Numerical 正则定义 |
| `算法代码/preprocessing_原版.py:104`     | `sequence`，规则应用顺序              |
| `算法代码/preprocessing_原版.py:106`     | `preprocess()`，逐条日志按 `sequence` 顺序执行 `re.sub` |

原始 numerical 正则（`preprocessing.py:22`）：

```python
'numerical': r'\b(\-?\+?\d+\.?\d*)\b|\b0[Xx][a-fA-F\d]+\b|\b[a-fA-F\d]{4,}\b',
```

#### 案例三：负号未泛化

原始日志：

```text
PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:-1
```

原版预处理结果：

```text
PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:-<*>
```

预期结果：

```text
PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:<*>
```

原因在于原始表达式中，数值分支以 `\b` 作为匹配起点的前置条件：

```text
\b(\-?\+?\d+\.?\d*)\b
```

要求匹配开始位置先满足词边界。

对于 `failed:-1`：

- 负号 `-` 前面是冒号 `:`，二者均为非词字符，不满足 `\b`。
- 数字 `1` 前面是负号 `-`，属于非词字符到词字符的转换，满足 `\b`。
- 正则从数字 `1` 开始匹配，导致 `-` 被保留。

因此，负号能否进入通配符，取决于其前一个字符的词性，而不是数值本身是否为负数。

该问题发生在预处理阶段，单条日志即可触发，与路径合并和 `br_thresh` 无关。

## 问题解决

### 半括号和参数名被吞问题

#### 修复目标

将原版仅保留分隔符的策略，调整为**基于公共前后缀和分隔符边界的结构感知合并**。

修复需要满足：

- 保留稳定字段名，如 `tracepoint`、`db_name`。
- 保留括号、冒号等结构性分隔符。
- 将真正变化的参数值替换为 `<*>`。
- 确保合并后的模板能够覆盖参与合并的历史 token。
- 随后出现更多变化时，允许模板继续合理泛化。

#### 修复方案

主要修改 `PIPLUP.py`：新增 `_structure_aware_merge()`（第 25-100 行），替换 `updateSequence()` 中差异 token 的内联合并逻辑（原版实现见 `PIPLUP.py:84-106`）。

**步骤一：计算最长公共前缀（LCP）**

不再要求公共前缀的每个字符都是分隔符，而是先找到两个 token 真正的最长公共前缀，再回退到合适的结构边界。

例如：

```text
tracepoint(abc)
tracepoint(defg)
```

保留结构前缀：

```text
tracepoint(
```

对于：

```text
db_name(abc).
db_name(defg).
```

保留：

```text
db_name(
```

**步骤二：计算最长公共后缀（LCS）**

从末尾向前扫描，保留稳定的后缀结构。

两个示例分别需要保留：

```text
)
```

以及：

```text
).
```

**步骤三：泛化中间变化部分**

最终结果：

```text
tracepoint(<*>)
db_name(<*>).
```

**步骤四：覆盖校验**

对于合并得到的模板，需要验证其是否可以完整覆盖参与合并的原始路径。

例如：

```text
db_name(<*>).
```

必须能够匹配所有参与合并的 `db_name(...)` token。

如果生成结果不能覆盖原始路径，则直接退化为 `<*>`，避免产生不完整或错误的候选模板。

核心实现节选（同位置中段保留与下划线边界特判从略，完整实现见 `PIPLUP.py:25-100`）：

```python
def _structure_aware_merge(t1, t2, delimiters):
    """保留分隔符边界的共同前缀、后缀及同位置中段。"""
    if t1 == t2:
        return t1

    def covers(general, specific):
        pattern = '.*?'.join(re.escape(p) for p in general.split('<*>'))
        return re.fullmatch(pattern, specific, re.DOTALL) is not None

    # 已有通配模板覆盖新 token 时保持不变，避免收窄历史匹配范围
    for general, specific in ((t1, t2), (t2, t1)):
        if '<*>' in general and covers(general, specific):
            return general

    # 步骤一：最长公共前缀 → 回退到最近的分隔符边界 → head
    lcp_len = 0
    for c1, c2 in zip(t1, t2):
        if c1 != c2:
            break
        lcp_len += 1
    head_end = -1
    for i in range(lcp_len - 1, -1, -1):
        if t1[i] in delimiters and t1[i] != '_':
            head_end = i
            break
    if head_end < 0:                        # 回退：取前缀内首个分隔符
        for i in range(lcp_len):
            if t1[i] in delimiters:
                head_end = i
                break
    head = t1[:head_end + 1]

    # 步骤二：最长公共后缀 → 前进到最近的分隔符边界 → tail
    lcs_len = 0
    for c1, c2 in zip(reversed(t1), reversed(t2)):
        if c1 != c2:
            break
        lcs_len += 1
    tail = ''
    if lcs_len:
        start = len(t1) - lcs_len
        tail = t1[start:]
        for i in range(start, len(t1)):
            if t1[i] in delimiters:
                tail = t1[i:]
                break

    # （同位置中段保留逻辑此处从略）

    # 步骤四：重叠保护 + 覆盖校验
    if len(head) + len(tail) >= min(len(t1), len(t2)):
        return '<*>'
    result = head + '<*>' + tail
    if not all(covers(result, token) for token in (t1, t2)):
        return '<*>'
    return result
```

### 负号未泛化问题

#### 修复目标

确保负数作为一个完整的动态参数被识别，而不是保留符号、只泛化数字。

原始日志：

```text
PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:-1
```

修复后：

```text
PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:<*>
```

#### 修复方案

主要修改 `preprocessing.py` 中的 `regex_match['numerical']`（第 22 行）。有两种修改方式（rust版本使用版本2，python版本使用版本1，实测python版本在两个正则下解析效果一致，后续可按需切换）：

原始 numerical 正则（`preprocessing.py:22`）：

```regex
r'\b(\-?\+?\d+\.?\d*)\b|\b0[Xx][a-fA-F\d]+\b|\b[a-fA-F\d]{4,}\b'
```

修复后的正则（`preprocessing.py:22`）：

```regex
# 版本1：
r'(?:\b|(?<=\W)|^)(\-?\+?\d+(?:\.\d+)?|0[Xx][a-fA-F\d]+)(?:\b|(?=\W)|$)|\b[a-fA-F\d]{4,}\b'

# 版本2：
r"[+-]?\b\d+(?:\.\d+)?\b|\b0[Xx][a-fA-F\d]+\b|\b[a-fA-F\d]{4,}\b"
```

**版本1：核心修改是将匹配起点/终点的边界条件由单一 `\b` 扩展为「三选一」的或语义。**

原规则把 `\b` 放在可选负号 `\-?` 之前，要求在负号位置首先满足词边界（位置两侧一端为词字符、一端为非词字符）。而冒号、空格、括号与负号均属于非词字符，负号位置无法形成 `\b`，匹配只能回退到数字起始处，最终只替换数字部分。

修复后，起点条件扩展为 `\b|(?<=\W)|^`：

- `\b`：常规词边界。
- `(?<=\W)`：前一字符为非词字符（负号前的冒号、空格、括号即属此类）。
- `^`：行首。

终点条件对称地扩展为 `\b|(?=\W)|$`；同时小数部分由 `\.?\d*`（允许尾部悬空点，如 `17500.` 被整体吞掉）收紧为 `(?:\.\d+)?`（点后必须跟数字），修复数字后紧跟标点时标点被吞入通配符的问题。

例如，对于 `failed:-1`：

1. 匹配从负号 `-` 位置尝试：`-` 前是冒号 `:`（非词字符），`(?<=\W)` 成立，起点条件满足。
2. `\-?\+?` 匹配负号，`\d+` 匹配数字 `1`，整体匹配 `-1`。
3. 终点条件 `(?=\W)`（或 `$`）成立。
4. 将完整匹配结果替换为 `<*>`，得到 `failed:<*>`。

**版本2：核心修改是调整可选符号与词边界的匹配顺序。**

原规则将 `\b` 放在可选负号 `-?` 之前，要求在负号位置首先满足词边界。而空格、冒号、括号与负号均属于非词字符，因此无法从负号位置开始匹配，最终只替换数字部分。

修复后，将 `[+-]?` 放到 `\b` 之前：

- `[+-]?`：优先匹配可选的正号或负号。
- `\b`：在符号与数字之间，或无符号数值的起始位置，验证词边界。
- `\d+(?:\.\d+)?`：匹配整数或小数部分。
- 末尾 `\b`：限定数字结束位置。

例如，对于 `failed:-1`：

1. 正则从负号 `-` 位置开始，`[+-]?` 匹配负号。
2. 此时匹配位置位于 `-` 与 `1` 之间，满足非词字符到词字符的边界条件。
3. `\d+` 匹配数字 `1`，完成整个 `-1` 的匹配。
4. 将完整匹配结果替换为 `<*>`，得到 `failed:<*>`。

修复前后对比（Python 实现实测）：

| 原始输入        | 修复前         | 修复后         |
| --------------- | -------------- | -------------- |
| `failed:-1`     | `failed:-<*>`  | `failed:<*>`   |
| `ret(-5)`       | `ret(-<*>)`    | `ret(<*>)`     |
| `gid -5`        | `gid -<*>`     | `gid <*>`      |
| `ret:+10`       | `ret:+<*>`     | `ret:<*>`      |
| `duration:3.14` | `duration:<*>` | `duration:<*>` |

### 总结

本次三个问题归结为两个独立根因：

| 根因                       | 对应问题           | 修复位置                                  | 解决方向                                                     |
| -------------------------- | ------------------ | ----------------------------------------- | ------------------------------------------------------------ |
| 路径合并仅保留分隔符证据   | 半括号、参数名被吞 | `PIPLUP.py`（`_structure_aware_merge()`） | 公共前后缀 + 结构感知泛化 + 覆盖校验                         |
| Numerical 词边界设计不合理 | 负号未泛化         | `preprocessing.py`（`numerical` 正则）    | 起止边界扩展为 `\b` / `(?<=\W)` / `^` 或语义或调整正则匹配顺序 |
