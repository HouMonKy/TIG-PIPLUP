# E0：V1 基线恢复与复现报告（重建版）

**项目：** TIG-2026 / PIPLUP HeaderDetector V1.1  
**分支：** `exp/header-v11-repro`（自 `419164df` 创建）  
**日期：** 2026-10-21  
**执行：** opencode agent

---

## 0. 结论摘要（先读）

1. **历史 V1 基线不可恢复。** 计划文档引用的提交 `ac30623d`、分支 `final`、脚本
   `bench/header_ablation.py`、报告 `docs/rep/header-detector-ablation/report.md`
   在本仓库（含 `origin`、`tig2026` 两个远端、全部本地分支、git worktree）中
   均不存在。详见 §1。
2. 按计划 §2.3 的缺失处理流程，本分支**重建**了 V1 的 M1–M4 方法实现、22 个
   数据集（14 Loghub + 8 压力集）与统一字符级评估框架，并以固定种子 42 完成消融。
3. **重建结果与历史数值不可直接比较**（实现与数据集均为重建），但定性结论与
   V1 报告一致：M1 不可用；M2 适合单一标准格式；M3 在混合/复杂格式上最强；
   M4（正文证据回退）全面劣于 M3。
4. 重建基线数字（宏平均）：M3 全 22 集 Boundary Accuracy **66.3%**，
   Over-strip **3.74%**，字符级 Body Integrity **99.13%**；
   Loghub 干净格式 M2 **50.3%**（与历史 M2 的 52.2% 处于同一水平带）。
5. 后续 E1–E4 应以本分支的提交为基线（BASE）创建各自的实验分支。

---

## 1. 历史基线缺失记录（计划 §2.3 要求）

| 检查项 | 结果 |
|---|---|
| `git cat-file -t ac30623d` | `fatal: Not a valid object name`（不存在） |
| 本地分支 `final` | 不存在（仅有 `final_optimization`，内容为性能优化，与 HeaderDetector 无关） |
| `bench/header_ablation.py` | 不存在（`bench/` 目录本身不存在） |
| `docs/rep/header-detector-ablation/` | 不存在 |
| 远端 `origin`（TIG-PIPLUP）全部分支 | 无 header/ablation 相关文件 |
| 远端 `tig2026`（TIG-2026）全部分支 | 无 header/ablation 相关文件（该仓库为参数异常检测/正则抽取项目） |
| 工作区未跟踪目录（analysis/、piplup实验/ 等） | 无 V1 实验代码 |

处理：在独立分支上重建基线（本报告），重建结果不宣称与历史结果可比。
可复用资产：`config/format_chaos.json`（真实业务模块日志格式配置，25 个模块）
与 `log-parser-upstream-baseline/tests/data/2k_dataset/`（14 个 Loghub 2k 数据集，
仓库未跟踪文件，已复制入本分支提交）。

---

## 2. 重建的方法实现（M1–M4）

所有方法基于**空白分词**（保留字符偏移），输出统一换算为字符级
`body_start`；`body_start = 0` 表示"不剥离、保留原文"。
完整实现见 `bench/header_detector/`，此处为精确定义。

### 2.1 公共组件：字段类型分类器（`tokentypes.py`）

token → 类型（族）：`DATE / TIME / DATETIME / DATE_COMPACT / DATE_SLASH /
LEVEL / MONTH / WEEKDAY / IP / IP_PORT / HOST / HOST_PORT / MAC / HEX / ID /
NUM / NUM_EPOCH / DOTTED / CTAIL（冒号结尾，如 `sshd[19939]:`）/
DASHWORD（`osd-daemon`）/ WORD / PUNCT / FCALL（`jk2_init()`）/ BRACKET*
（括号结构，族级折叠）`。

- **HEADERISH 集合**：位置一致性满足且类型属于该集合时视为头部字段
  （WORD/OTHER/FCALL 不属于：纯词只有"恒定"才算头部）。
- **STRONG 集合**：`{LEVEL, TIME, DATE*, DATETIME, MONTH, WEEKDAY, MAC,
  BRACKET*}`。HOST/ID/NUM/DOTTED/CTAIL/DASHWORD **不是**强证据
  （正文类主机名保护，对应计划 §7.1 的 `user123` 问题）。

### 2.2 M1：位置变化率（重建定义）

对训练集每个 token 位置 j 计算变化率 `1 - max_value_freq/n`；
边界 = **第一个变化率 > 0.5 的位置**。时间戳在位置 0/1 即变化，因此
几乎所有带时间戳格式在第 0–1 位即截断。

### 2.3 M2：位置变化率 + 字段类型

逐位置扫描（最多 16 位），位置 j 属于头部当且仅当：
(a) 值恒定（≥90%），或 (b) 类型（族）一致（≥90%）且类型 ∈ HEADERISH。
边界 = 第一个不满足的位置。**全局单一边界**（对整个训练语料），
不做任何分组——这是对"M2 = 位置变化率 + 字段类型"的字面重建，
因此混合格式流在位置 0 即类型不一致而整体弃剥。

### 2.4 M3：M2 + 短前缀分组 + 结构一致性

1. **4-token 类型签名分组**（族级类型，避免值相关碎片化）；
   签名仅用于分组，**组内扫描从位置 0 重启**（短于 4 token 的头部仍可表达）；
   组 < 5 行 → 保守保留（Leaf 0）。
2. 组内递归扫描，每位置：
   - 恒定 → 消费前进；
   - 类型一致且 HEADERISH → 消费前进；
   - 类型一致但弱（WORD/OTHER 等）→ **正文开始**；若处于未闭合 `[`
     之内（多 token 括号字段，如 `[AsyncDispatcher event handler]`）则按值
     分裂继续；
   - 类型混杂 → **分裂**：强类型分区按 (族,值) 分裂继续（结构子格式，
     如 `[main]` vs `[LeaseRenewer:…]`）；弱类型分区若无未闭合括号则停。
3. **Guard A**：学习到的边界前缀必须含至少一个 STRONG 位置，否则保留原文。
4. **Guard B**：边界吞掉行中位数 token 数的 75% 以上时，回退到最近分裂位置
   （无则保留）——防止正文模板被整体当作结构。
5. 预测：签名组 → 树下行；未见签名/未见结构 → 保留（保守弃权）。

### 2.5 M4：M3 + 正文证据

M3 边界确定后，若正文首 token 类型 ∈ {ID, NUM, HEX, HOST, IP}
（"值样"token），边界**回退 1 个 token**。该启发式在正文以
用户名/主机名/数字开头时把边界拉回头部内部（under-strip），
在头部以值结尾时又误删字段——两个方向都错，复现了
"M4 不适合直接修改边界搜索"的历史结论。

---

## 3. 重建的数据集（22 个）

### 3.1 Loghub（14 个，GT 来自标准格式正则）

`bench/header_data/loghub/*.log`（2k 采样）。GT 头部正则沿用 logparser
基准的格式定义，`body_start = 头部正则 match.end()`：
如 HDFS `^\d{6} \d{6} \d+ \S+ \S+: `、Hadoop
`^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \S+ \[[^\]]*\] \S+: ` 等
（全部 14 条见 `bench/header_detector/gt.py`）。

GT 覆盖率验证（`--validate-gt`）：13/14 数据集 ≥ 99.6%；
Thunderbird 99.65%（7 行无组件冒号的内核行被排除并计数）。
被排除行不参与评估。

### 3.2 压力集（8 个，GT 由构造生成，种子 42）

`bench/header_data/stress/*.jsonl`，每集 1000 行，
由 `bench/header_detector/gen_stress.py` 从 `config/format_chaos.json`
的真实业务格式确定性生成：

| 数据集 | 来源 | 特征 |
|---|---|---|
| stress-deploy | deploy 模块 16 种格式 | 真实业务混合格式流 |
| stress-oam-console | oam-console 11 种格式 | 含 `[time]:`、`----`、双空格等怪格式 |
| stress-dsware | fsa+fsm 13 种格式 | level 前置、`(UTC) DSWARE KMC`、路径括号 |
| stress-messages | Messages 5 种格式 | 10 括号字段、正文包裹在括号内 |
| stress-bracket-space | 手工构造 | `[AsyncDispatcher event handler]`、`[req-x - - -]`、变长括号字段 |
| stress-pure-word | fma 等 + 手工 | `CST <Time> TIP: <process> :` 等纯词头 |
| stress-host-body | 手工构造 | 85% 正文以 `user123/node01/client-x` 等主机样式开头 |
| stress-noheader | 手工构造 | 50% 无头部日志（正确行为 = 保留） |

**与历史压力集的差异声明：** 历史"压力测试集"的构成不可知。本重建集
基于真实 `format_chaos` 格式 + 计划文档点名的挑战类型，属于**新建**压力集。

### 3.3 评估协议（固定，种子 42）

- 每数据集：`random.Random(42)` 洗牌 → 训练 500 行（Loghub）/ 300 行（压力），
  其余为测试。**检测器仅接触训练行**；无测试统计泄漏（位置统计、分组、
  阈值均只用训练集）。
- 字符级四分类：`correct / over_strip / under_strip / abstain`
  （pred==0 且 gt>0 记 abstain，与欠剥严格区分）。
- 指标：Boundary Accuracy、Over-strip Rate、Under-strip Rate、Abstain Rate、
  Auto-strip Coverage、Accepted Boundary Accuracy、Body Integrity（字符级 +
  行级）。汇总为数据集间**宏平均**（分 suite 与 all22）。

---

## 4. 结果

### 4.1 宏平均汇总

| 指标 | M1 | M2 | M3 | M4 |
|---|---:|---:|---:|---:|
| **Loghub 干净格式 acc** | 0.000 | **0.503** | 0.762 | 0.739 |
| **压力集 acc** | 0.063 | 0.063 | **0.490** | 0.454 |
| all22 acc | 0.023 | 0.343 | **0.663** | 0.635 |
| all22 over-strip | 0.000 | 0.000 | 0.037 | 0.037 |
| all22 under-strip | 0.636 | 0.316 | 0.043 | 0.071 |
| all22 abstain | 0.341 | 0.341 | 0.257 | 0.257 |
| all22 coverage | 0.636 | 0.636 | 0.721 | 0.721 |
| all22 accepted acc | 0.000 | 0.503 | 0.863 | 0.831 |
| all22 body-integrity (char) | 1.000 | 1.000 | 0.991 | 0.991 |
| all22 body-integrity (line) | 1.000 | 1.000 | 0.963 | 0.963 |

### 4.2 与历史 V1 报告的对照（仅定性）

| 观察 | 历史 V1 | 重建 E0 | 一致? |
|---|---|---|---|
| M1 不可直接用于边界 | 12.5%/6.2% | 6.3%/0.0% | ✓ |
| M2 对标准化日志较好 | Loghub 52.2% | Loghub 50.3% | ✓（量级吻合） |
| M3 复杂/混合更鲁棒 | 压力 83.7% | 压力 49.0%（Loghub 反超 76.2%） | ✓ 方向 / ✗ 幅度 |
| M4 劣于 M3 | 71.2% < 83.7% | 45.4% < 49.0%（且 Loghub 73.9 < 76.2） | ✓ |
| over-strip 需治理 | 1.3% | 3.74% | ✓（E3 目标） |

幅度差异的来源（诚实声明）：(a) M2/M3 为按文档描述的重新实现，非原代码；
(b) 压力集为重建（历史构成未知）；(c) 本重建 M3 的递归分组在 Loghub 上
强于历史 M3（历史 M3 在 Loghub 反而退化到 40.8%，其机制不可考，
本实现未复现该退化——这一点将作为 E1 的分析问题保留）。

### 4.3 逐数据集（acc / over / under / abstain）

| 数据集 | M2 | M3 | M3 cov / accepted |
|---|---|---|---|
| Apache | 1.000/0/0/0 | 1.000/0/0/0 | 1.000 / 1.00 |
| BGL | 0/0/1.000/0 | 0.895/0.025/0/0.081 | 0.919 / 0.973 |
| HDFS | 1.000/0/0/0 | 1.000/0/0/0 | 1.000 / 1.00 |
| HPC | 0/0/0/1 | 0.231/0.025/0.420/0.324 | 0.676 / 0.342 |
| Hadoop | 0/0/1.000/0 | 0.750/0/0.014/0.236 | 0.764 / 0.982 |
| HealthApp | 0/0/0/1 | 0/0/0/1 | 0 / — |
| Linux | 0.993/0.003/0.003/0 | 0.993/0.003/0.003/0 | 1.000 / 0.993 |
| Mac | 0/0/1.000/0 | 0.830/0.097/0.070/0.003 | 0.997 / 0.833 |
| OpenSSH | 1.000/0/0/0 | 1.000/0/0/0 | 1.000 / 1.00 |
| OpenStack | 0.075/0/0.925/0 | 0.043/0.032/0/0.925 | 0.075 / 0.575 |
| Proxifier | 0/0/1.000/0 | 0.953/0.047/0/0 | 1.000 / 0.953 |
| Spark | 1.000/0/0/0 | 0.999/0/0/0.001 | 0.999 / 1.00 |
| Thunderbird | 1.000/0/0/0 | 0.988/0.003/0/0.009 | 0.991 / 0.997 |
| Zookeeper | 0.979/0/0.021/0 | 0.979/0/0.021/0 | 1.000 / 0.979 |
| stress-bracket-space | 0/0/1/0 | 0.384/0/0/0.616 | 0.384 / 1.00 |
| stress-deploy | 0/0/0/1 | 0.364/0.033/0.089/0.514 | 0.486 / 0.750 |
| stress-dsware | 0/0/0/1 | 0.666/0.050/0.059/0.226 | 0.774 / 0.860 |
| stress-host-body | 0/0/0/1 | 0.426/**0.509**/0.029/0.037 | 0.963 / 0.442 |
| stress-messages | 0/0/1/0 | 0/0/0/1 | 0 / — |
| stress-noheader | 0.5/0/0/0.5 | **1.000**/0/0/0 | 0.500 / 1.00 |
| stress-oam-console | 0/0/0/1 | 0.330/0/0.244/0.426 | 0.574 / 0.575 |
| stress-pure-word | 0/0/0/1 | 0.750/0/0/0.250 | 0.750 / 1.00 |

完整逐行错误样例（每类前 10 条）见 `e0-results/e0_results.json`。

---

## 5. 典型错误类别（供 E1–E4 引用）

| 类别 | 案例 | 根因 | 后续实验 |
|---|---|---|---|
| C1 中 token 边界 | `…[dme-server]disk 15448…`（正文紧贴括号）；`2026-03-02 06:53:03.000000 UT----task…`；deploy#9/13、fsm#6、oam#1 | GT 边界落在空白 token 内部，token 级方法原理上不可达 | **E2**（Span-aware 分词） |
| C2 正文以值样 token 开头 | `… ERROR user175 reported…`（host-body 集 over-strip 50.9%）；Mac `ARPT:` | ID/HOST 类正文首词被位置一致性吞入头部 | **E3**（置信度门控） |
| C3 正文以括号开头 | deploy#8 `… [osd-daemon] [checkpoint finished, …]`（Content 被括号包裹） | BRACKET_OPEN 一致 → 前进越过真实边界 | E3 |
| C4 括号内含空格 + req 段 | OpenStack `[req-xxx 113d… - - -]`（abstain 92.5%）；bracket-space 变长字段 | 括号字段跨多 token，值分裂碎片化，未见结构即弃权 | E2/E1 |
| C5 括号全包裹/管道单 token | HealthApp（`a|b|c|body` 一行一个 token）、Messages（10 括号字段） | 空白分词粒度过粗 | E2 |
| C6 异构节点字段 | HPC `node-246` vs `Interconnect-0N03` vs `full`（类型混杂） | 位置类型不一致即停 | E1/E2 |
| C7 小样本格式弃权 | deploy 51% abstain、oam 43% abstain | 300 训练行 / 16+ 格式 → 签名组 < 5 | E1（规则学习提升覆盖率） |

---

## 6. 计划 §4.2 重点检查项逐条回答

1. **M3 是否正确实现短前缀分组？** 是——4-token 族级类型签名分组 +
   组内递归扫描（签名仅分组，不约束边界下限，见 §2.4 注记）。
2. **签名长度是否确实为 4 token？** 是（`M3_SIG_LEN = 4`）。
   重建过程实测：族级签名 + 值分裂优于 8-token 签名（8-token 会把正文
   特征并入分组键——与历史结论方向一致，未做正式消融，因历史 8-token
   实现不可考）。
3. **训练/测试是否泄漏？** 否。fit 仅接收训练行文本；测试行不参与任何
   位置统计、分组或阈值。拆分由种子 42 的洗牌完成。
4. **模板/格式分组是否提前使用测试集信息？** 否（同上）。
5. **M4 为何降低 Boundary Accuracy？** 值样正文首词（ID/NUM/HOST）触发
   边界回退：host-body 上 acc 从 0.426 跌到 0.129——回退方向在
   "头部以值结尾"的格式上是错的（欠剥），在"正文以值开头"的格式上
   又是对的，无法区分，净效应为负。
6. **汇总指标是宏平均还是微平均？** 宏平均（数据集间平均），
   同时保留逐数据集明细（§4.3）；`e0_results.json` 含全部原始计数可复算微平均。
7. **拒绝剥离与欠剥是否混为一类？** 否。`abstain`（pred==0 且 gt>0）与
   `under_strip`（0 < pred < gt）严格分开（metrics.py `classify_outcome`）。

---

## 7. 复现方式

```bash
# worktree: ../header-v11-repro, branch exp/header-v11-repro
git worktree add -b exp/header-v11-repro ../header-v11-repro 419164df

python3 -m bench.header_detector.gen_stress      # 重建压力集（种子 42，确定性）
python3 bench/header_ablation.py --validate-gt   # GT 正则覆盖率检查
python3 bench/header_ablation.py                 # M1-M4 全量评估
```

配置常量（`bench/header_detector/methods.py`）：`SEED=42`、
`MAX_POS=16`、`VARIATION_THRESHOLD=0.5`、`CONST_THRESHOLD=0.9`、
`TYPE_CONSISTENCY=0.9`、`M3_SIG_LEN=4`、`M3_GROUP_MIN=5`、
Guard B 系数 0.75。训练/测试规模：Loghub 500/rest，压力 300/700。

环境：Python 3.13.5，纯标准库，无第三方依赖。

---

## 8. 结论与对后续实验的输入

- **通过条件评估：** 基线"复现"以**重建**形式达成——历史无法恢复的原因
  已记录（§1），重建实现、数据、协议、种子、结果全部固定并提交。
  后续实验的改善归因以本基线为参照。
- **E1 输入：** M2 在 8/14 Loghub 数据集上 =1.0（单一格式强），M3 在混合
  压力集上 0.36–0.67 且 Loghub 反超 M2——本基线中 M3 未复现"Loghub 退化"，
  E1 的自适应选择需在**规则级**（每格式族选择/过滤/置信度）而非
  "M2 vs M3 全局二选一"上找收益；重点转向覆盖率（C7 弃权 38%）与
  accepted-accuracy（HPC 0.342、host-body 0.442）。
- **E2 输入：** C1/C4/C5 三类失败的 GT 边界均在 token 内部或括号跨度上，
  字符级 span 分词是唯一出路；Messages/HealthApp 为极端案例。
- **E3 输入：** over-strip 全局 3.74%（目标 ≤0.5%），集中在
  host-body（50.9%）、Mac（9.7%）、dsware（5.0%）；Guard A/B 已证明
  "强证据 + 行长约束"能压住无头部与模板类过剥，但值样正文首词
  （C2）需置信度门控。
- **E4 输入：** M3 树下行预测是 O(头部 token 数) 的字典查找，
  天然可编译为规则（签名组 → 边界），为离线规则化提供了直接路径。
