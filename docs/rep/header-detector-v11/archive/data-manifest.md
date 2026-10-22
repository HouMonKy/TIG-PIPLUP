# 数据清单（来源、权限、种子、划分、GT 版本、校验）

全部数据文件 SHA256 见同目录 `data-checksums.sha256`（23 个文件）。

## 1. Loghub 2k 数据集（14 个）

| 项 | 说明 |
|---|---|
| 文件 | `bench/header_data/loghub/<Name>_2k.log`（各 ~2000 行） |
| 来源 | Loghub 公开日志基准的 2k 采样（logparser benchmark 标准子集）；本地副本取自仓库未跟踪目录 `log-parser-upstream-baseline/tests/data/2k_dataset/`，E0 时复制入实验分支 |
| 使用权限 | Loghub 基准数据供研究用途；BGL/Thunderbird 等源自 IBM/SDSC 公开trace，沿用其原始许可；**未做再分发限制去除，公开引用时需注明 Loghub 出处** |
| GT | `bench/header_detector/gt.py::LOGHUB_GT` 正则（GT 版本 **v1.1-rebuild**），`body_start = match.end()`；覆盖率 13/14 ≥99.6%，Thunderbird 99.65%（7 行无组件冒号内核行排除并计数） |
| 内部敏感信息 | 无（公开基准数据） |
| 关联实验 | E0–E4 全部（各分支 `bench/header_data/loghub/` 内容一致，校验和通用） |

## 2. 压力集（8 个，生成式）

| 项 | 说明 |
|---|---|
| 文件 | `bench/header_data/stress/stress-*.jsonl`（各 1000 行，JSONL 含逐行 `body_start`） |
| 生成脚本 | `bench/header_detector/gen_stress.py`，种子 **42**，输入 `config/format_chaos.json`（仓库既有真实业务模块格式模板：deploy/oam-console/fsa+fsm/Messages 等 25 模块）+ 手工挑战类型（bracket-space/pure-word/host-body/noheader） |
| 使用权限 | 合成数据；格式模板来自仓库既有配置（含模块名与格式结构，**不含任何原始业务日志行**）；模板本身已在仓库公开范围，未引入新的敏感信息 |
| 确定性 | 归档验收已验证：脚本重生成与提交文件**逐字节一致**（9/9 文件 SHA256 相同） |
| GT | 构造时生成（渲染格式串时记录 Content 偏移），GT 版本 v1.1-rebuild |
| 关联实验 | E0–E4 全部 |

## 3. 括号挑战集（1 个，生成式）

| 项 | 说明 |
|---|---|
| 文件 | `bench/header_data/challenge/challenge-brackets.jsonl`（1600 行，8 类×200） |
| 生成脚本 | `bench/header_detector/gen_challenge.py`，种子 **4242**，词表与压力集完全独立（避免复用调参样本作为唯一评估依据，实验计划 §4 要求） |
| 8 类 | 正常括号 / 括号内多空格 / 嵌套 / 未闭合 / 正文含括号与附着正文 / 无头部 / 混合格式 / 头部-正文相似结构 |
| 已知 GT 怪癖 | 正文模板前导空格导致 39 行"空白级 over"（零内容损失，见 decisions.md D12） |
| 关联实验 | E2–E4（E1 及 E0 不含挑战集） |

## 4. 划分与种子（协议版本）

| 协议 | 使用实验 | 定义 |
|---|---|---|
| E0 协议 | E0 | 种子 42 洗牌；Loghub train 500/rest，压力 train 300/rest |
| E1–E4 协议 | E1/E2/E3/E4 | 种子 42 洗牌；learn/val/test = 30%/20%/50%（全部 23 源；E1 为 22 源） |

两协议的划分函数：`e1_runner/e2_runner/e3_runner.three_way_split /
split_dataset`；`random.Random(42).shuffle` 确定性划分。

## 5. 大型数据与内部日志处理声明

- 本项目**未使用任何内部真实业务日志行**：压力/挑战集为合成数据，
  仅复用仓库既有的 `format_chaos.json` 格式模板（结构元数据）。
- Loghub 2k 合计约 4.6 MB，为公开基准，直接随实验分支保存（校验和
  见 `data-checksums.sha256`）。
- 若后续 E5 需要内部真实日志：不得直接入公开仓库，应使用独立存储
  并只提交清单+校验和（本清单格式可直接扩展）。
