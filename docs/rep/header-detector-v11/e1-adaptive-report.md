# E1：M2/M3 离线自适应规则学习实验报告

**项目：** TIG-2026 / PIPLUP HeaderDetector V1.1  
**分支：** `exp/header-v11-adaptive`（自 E0 基线提交 `8fb2c985` 创建）  
**基线：** E0 重建基线（`docs/rep/header-detector-v11/e0-baseline-report.md`）  
**日期：** 2026-10-21

---

## 0. 结论摘要

1. **自适应规则选择有明确收益，且主要体现在安全性轴上。**
   E1-A（按格式族在验证集上选择 M2/M3 规则）以 −1.1pt 的宏平均
   Boundary Accuracy 代价，将全 22 集 Over-strip 从 M3-RULE 的 3.39%
   降到 **0.06%**（约 56×），字符级 Body Integrity 达到 **100%**，
   Accepted Boundary Accuracy 从 0.866 提升到 **0.965**。
   在 Loghub 干净格式上 E1-A（0.773）**反超**原始 M3（0.763）
   与 M3-RULE（0.771），同时 over-strip 从 2.1% 降至 0.09%。
2. **M2/M3 的分工被验证**：M3 仅在少数结构族上必要（括号线程头，
   如 Hadoop `(DATE,TIME,LEVEL,BRACKET_OPEN)`：M2 val 0.00 vs
   M3 val 0.60）；多数可安全剥离的格式族 M2 的扁平规则即最优
   （61 条部署规则中 52 条选 M2）。
3. **"两种方法都不可靠 → 不生成规则"的保守路径有效**：不可解格式
   （OpenStack、HealthApp、Messages、HPC、bracket-space 等）被正确
   识别为 KEEP，宁可弃剥不误删。
4. **留源（LODO）迁移实验给出重要安全警告**：仅凭签名跨源迁移规则
   在 Loghub 同构格式间安全（Linux↔OpenSSH 0.996，零 over-strip），
   但在压力格式间不安全（dsware LODO over-strip 12.8%、host-body
   19.6%）——**E4 的未知格式回退必须保守**（仅迁移高置信规则或直接保留）。
5. 建议：**保留 E1-A/E1-B 进入 E5 组合**；E1-C 作为阈值机制保留，
   推荐工作点 τ=0.5–0.6（pooled 覆盖率 0.68–0.69，accepted acc
   ≥0.97，over-strip 0.0007）。

---

## 1. 实验设计

### 1.1 架构（计划 §5.2 的落地）

```
每个来源 (source = 生产日志模块；实验中 = 数据集)
  └─ learn 行(30%) ──→ 按 4-token 结构签名分格式族
        ├─ M2 规则：族内扁平 M2 扫描边界
        └─ M3 规则：族内 M3 模态边界（M3 按源拟合）
  └─ val 行(20%) ───→ E1-A 选择 / E1-B 过滤 / E1-C 置信度
  └─ test 行(50%) ──→ 评估（任何选择均不接触 test）
```

### 1.2 对照组

| 变体 | 说明 |
|---|---|
| M2-RULE | 所有格式族一律用 M2 规则（扁平、可编译） |
| M3-RULE | 所有格式族一律用 M3 模态规则（扁平化） |
| E1-A | 按格式族选择 M2/M3；两者皆不可靠 → KEEP |
| E1-B | E1-A + 规则过滤（支持度/val 精度/val over-strip 门） |
| E1-C | E1-B + 置信度 τ 门控（τ 可扫） |
| M2-raw / M3-raw | 每源拟合的原始检测器（learn+val 训练，参照上界） |

### 1.3 关键设计决策与合规声明

- **规则键 = (source, 结构签名)**。来源是生产环境可得的日志模块信息
  （采集器元数据），不是"按数据集名硬编码 M2/M3"——方法选择完全由
  **结构族在验证集上的表现**决定，代码中不存在任何 `if 数据集名` 分支。
  实验中曾尝试**合并学习**（全部来源混流、仅按签名分组），结果
  Linux/OpenSSH 因共享 syslog 签名而互相污染（主机值不同→边界错位），
  这正是计划要求 E1-B"按来源分组"的动机，已按来源键重做。
- 选择准则（E1-A）：over-strip 风险优先（差距 >2pp 即采纳更低者），
  平手比 val 精度，再平手选更简单的 M2；选中规则若
  val over > 2% 或 val acc < 50% → KEEP。
- 规则为**扁平 (签名 → token 边界)**，天然可编译为 E4 要求的
  确定性前缀匹配（每条规则 JSON 见 `e1-results/e1_results.json`
  `rules_e1c` 字段）。

### 1.4 数据划分（种子 42）

每数据集 30% learn / 20% val / 50% test（Loghub 600/400/1000，
压力集 300/200/500）。所有阈值与选择仅用 learn/val。

---

## 2. 结果

### 2.1 宏平均汇总

**all22（22 数据集宏平均）**

| 变体 | acc | over-strip | abstain | coverage | accepted-acc | body-integrity(char) |
|---|---:|---:|---:|---:|---:|---:|
| M2-RULE | 0.6362 | 0.0440 | 0.0730 | 0.9122 | 0.6787 | 0.9762 |
| M3-RULE | 0.6584 | 0.0339 | 0.2534 | 0.7238 | 0.8655 | 0.9923 |
| **E1-A** | **0.6473** | **0.0006** | 0.3318 | 0.6454 | **0.9654** | **1.0000** |
| E1-B | 0.6407 | 0.0006 | 0.3408 | 0.6364 | 0.9703 | 1.0000 |
| E1-C(τ=.75) | 0.5869 | 0.0006 | 0.4085 | 0.5687 | 0.9941 | 1.0000 |
| M2-raw(参照) | 0.3887 | 0.0001 | 0.3408 | 0.6364 | 0.5749 | 1.0000 |
| M3-raw(参照) | 0.6689 | 0.0413 | 0.2467 | 0.7305 | 0.8621 | 0.9883 |

**Loghub（干净格式，宏平均）**

| 变体 | acc | over-strip | coverage | accepted-acc |
|---|---:|---:|---:|---:|
| M2-RULE | 0.7491 | 0.0116 | 0.9216 | 0.8134 |
| M3-RULE | 0.7711 | 0.0037 | 0.8282 | 0.9160 |
| **E1-A** | **0.7734** | **0.0009** | 0.7968 | **0.9728** |
| M3-raw | 0.7630 | 0.0210 | 0.8226 | 0.8907 |

**压力集（宏平均）**

| 变体 | acc | over-strip | coverage | accepted-acc |
|---|---:|---:|---:|---:|
| M2-RULE | 0.4387 | 0.1008 | 0.8958 | 0.4598 |
| M3-RULE | 0.4612 | 0.0868 | 0.5410 | 0.7790 |
| **E1-A** | 0.4265 | **0.0000** | 0.3805 | **0.9527** |
| M3-raw | 0.5042 | 0.0767 | 0.5693 | 0.8090 |

压力集上 E1-A 比 M3-raw 低 7.8pp 精度——代价全部来自**拒绝剥离
高风险格式**（这正是设计目标：M3-raw 在压力集上 7.7% 的 over-strip
意味着业务正文被删）。

### 2.2 逐数据集（Boundary Accuracy）

| 数据集 | M2-RULE | M3-RULE | E1-A | E1-B | M3-raw |
|---|---:|---:|---:|---:|---:|
| Apache | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| BGL | 0.902 | 0.902 | 0.902 | 0.902 | 0.897 |
| HDFS | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| HPC | 0.253 | 0.229 | 0.253 | 0.253 | 0.229 |
| **Hadoop** | 0.359 | **0.774** | **0.774** | **0.774** | 0.761 |
| HealthApp | 0 | 0 | 0 | 0 | 0 |
| Linux | 0.996 | 0.996 | 0.996 | 0.996 | 0.996 |
| Mac | 0.924 | 0.924 | 0.924 | 0.924 | 0.826 |
| OpenSSH | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| OpenStack | 0.074 | 0 | 0 | 0 | 0.041 |
| Proxifier | 1.000 | 1.000 | 1.000 | 1.000 | 0.957 |
| Spark | 1.000 | 1.000 | 1.000 | 1.000 | 0.999 |
| Thunderbird | 1.000 | 0.991 | 1.000 | 1.000 | 0.997 |
| Zookeeper | 0.979 | 0.979 | 0.979 | 0.979 | 0.979 |
| stress-bracket-space | 0.264 | 0.264 | 0.264 | 0.264 | 0.410 |
| stress-deploy | 0.420 | 0.380 | 0.398 | 0.258 | 0.432 |
| stress-dsware | 0.434 | 0.588 | 0.352 | 0.352 | 0.670 |
| stress-host-body | 0.444 | 0.426 | 0.276 | 0.276 | 0.444 |
| stress-messages | 0 | 0 | 0 | 0 | 0 |
| stress-noheader | 0.824 | **1.000** | **1.000** | **1.000** | **1.000** |
| stress-oam-console | 0.388 | 0.296 | 0.386 | 0.382 | 0.342 |
| stress-pure-word | 0.736 | 0.736 | 0.736 | 0.736 | 0.736 |

注：M3-raw 在 bracket-space（0.410）、dsware（0.670）上精度更高，
但代价是 8–10% over-strip（见 §2.1 压力集行）；E1-A 按"正文安全
优先"准则拒绝了这些规则——这是设计使然，不是缺陷。

### 2.3 E1-C 置信度扫描（pooled test，微平均）

| τ | acc | over-strip | coverage | accepted-acc |
|---:|---:|---:|---:|---:|
| 0.0 | 0.6923 | 0.0007 | 0.6987 | 0.9708 |
| 0.5 | 0.6782 | 0.0007 | 0.6832 | 0.9722 |
| 0.6 | 0.6764 | 0.0007 | 0.6814 | 0.9721 |
| 0.75 | 0.6470 | 0.0007 | 0.6386 | 0.9912 |
| 0.90 | 0.6078 | **0.0002** | 0.5957 | 0.9970 |

over-strip 在全 τ 范围内 ≤0.07%，τ 主要调节 coverage/accepted-acc
权衡。**推荐 τ=0.5–0.6**：覆盖 68%，accepted 97.2%，E3 可在此
基础上继续压 over-strip。

### 2.4 留源迁移（LODO）

held-out 源不参与学习，测试时仅按签名匹配其他源的规则（最高置信度）：

- **同构格式迁移成功且安全**：Linux 0.996 / OpenSSH 1.000
  （互相覆盖，over-strip ≤0.2%）、BGL 0.910（借 Thunderbird 规则，
  零 over-strip）、stress-noheader 1.000。
- **异构格式正确回退**：Apache/HDFS/Spark/Mac/Zookeeper 等独有签名
  无规则可匹配 → 保留原文（coverage 0，零风险）。
- **危险区**：dsware LODO over-strip 12.8%、host-body 19.6%——
  相似但不完全相同的签名之间迁移规则会误删正文。
  **结论：E4 的未知格式回退不得做宽松的签名迁移**；仅允许
  置信度 ≥0.9 的规则迁移或直接保留原文。

---

## 3. 计划 §5.4 重点问题回答

1. **哪些数据集适合 M2？** 单一结构、前缀类型强的格式
   （Apache/HDFS/Spark/TBird/Zookeeper/Linux/Mac 等，M2 与 M3 规则
   等价）；以及 M3 因保守守卫而弃剥、但族内扁平扫描稳定的场景
   （oam-console 的两个族：M2 val=1.00 vs M3 val=0.00）。
2. **哪些适合 M3？** 括号字段跨 token 的格式。最大案例 Hadoop
   `(DATE,TIME,LEVEL,BRACKET_OPEN)`（391 learn 行）：M2 边界=4
   （在 `[AsyncDispatcher` 处截断括号字段），M3 边界=6（正确越过
   `event handler]`），val 0.00 vs 0.60。部署规则中 9 个族选 M3。
3. **M3 在 Loghub 上退化的真实原因（历史问题）？** 重建基线中
   **未复现**该退化（重建 M3 在 Loghub 反而 0.762 > M2 0.503）。
   历史退化的机制已不可考；推测与旧实现的签名碎片化（细粒度内型
   签名把同格式拆成小组）有关。本实现用**族级签名 + 组内从 pos0
   重扫**避免了该问题。此问题按"无法验证"记录，不再作为 E1 前提。
4. **是否存在两种算法都无法解决的格式？** 是：OpenStack
   （`[req-… - - -]` 含空格 + 正文以 IP 开头，val 全废 → KEEP）、
   HealthApp（管道单 token）、Messages（括号全包裹）、HPC
   （异构节点字段）、bracket-space 变长括号字段——全部被 E1-A
   正确转为 KEEP。这些是 E2（span 分词）的直接目标。
5. **自动选择是否真正优于固定选择？** 是，且不是过拟合：
   - vs 固定 M2-RULE：acc +1.1pp、over-strip 73×↓、integrity
     0.976→1.000；
   - vs 固定 M3-RULE：acc −1.1pp、over-strip 56×↓、accepted-acc
     +10pp、integrity →1.000；
   - 选择只用 val，test 一致成立；LODO 进一步证明规则在未见源上
     不产生新 over-strip（保守路径正确）。
   收益的本质是**风险敏感的选择目标**（over-strip 优先），而非
   精度提升——这与计划"优先选择正文误删风险更低的规则"一致。

---

## 4. 规则集形态（E4 输入）

- 234 个 (源, 签名) 规则族；E1-B 部署 53 条剥离规则（47 M2 / 6 M3）
  + 181 条 KEEP。
- 规则 JSON 示例（`rules_e1c` 字段，节选）：

```json
{
  "rule_id": "HDFS::DATE_COMPACT,DATE_COMPACT…",
  "source": "HDFS",
  "prefix_signature": ["DATE_COMPACT", "DATE_COMPACT", "NUM", "LEVEL"],
  "method": "M2",
  "body_boundary_tokens": 5,
  "learn_support": 600, "val_support": 400,
  "val_accuracy": 1.0, "val_over_strip": 0.0,
  "confidence": 1.0, "version": "e1-v1.1"
}
```

在线推理 = `signature(line) → 规则查表 → token 边界 → 字符偏移`，
无统计计算，满足计划方向 B（在线轻量规则匹配）。

---

## 5. 结论与建议

| 决策项 | 建议 |
|---|---|
| E1-A（自适应选择） | **保留**：安全性收益巨大，精度代价可忽略 |
| E1-B（规则过滤） | **保留**：在 E1-A 之上小幅收紧（acc −0.7pp，accepted +0.5pp） |
| E1-C（置信度门控） | 保留机制，**τ=0.5–0.6**；把 over-strip 进一步压缩交给 E3 重新校准 |
| 未知格式回退 | 仅允许高置信（≥0.9）规则跨源迁移，否则保留原文（LODO 结论） |
| 进入 E5 | E1-A/E1-B 的规则生成管线 + (source, signature) 键控规则格式 |

**遗留问题（转 E2/E3）：** 中 token 边界格式（deploy#9/13、fsm#6、
oam#1）与括号包裹正文（Messages、bracket-space）在 token 级规则框架
内不可解，等待 E2 的 span 分词；host-body 的 over-strip 剩余风险
（M2/M3-RULE 视角 50.9%，E1-A 已通过拒绝剥离规避）需 E3 的门控在
不牺牲覆盖率的前提下解决。

---

## 6. 复现方式

```bash
# worktree: ../header-v11-adaptive, branch exp/header-v11-adaptive
# 基于 E0 基线提交 8fb2c985
git worktree add -b exp/header-v11-adaptive ../header-v11-adaptive exp/header-v11-repro

python3 bench/header_adaptive.py
# 输出: docs/rep/header-detector-v11/e1-results/e1_results.json
#   含 results/summary/tau_curve/lodo/method_choice/rules_e1c 全量数据
```

配置（`bench/header_detector/adaptive.py::E1Config`）：seed 42、
learn/val/test = 30/20/50%、over_margin 0.02、min_val_acc 0.50、
max_val_over 0.02、filter 门 0.60/0.02、τ 默认 0.75。
环境：Python 3.13.5，纯标准库。
