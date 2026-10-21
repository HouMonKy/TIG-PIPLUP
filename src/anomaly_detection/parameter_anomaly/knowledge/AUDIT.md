# 知识表审核结论

- 来源：`final1100-1790178817`（冻结库 sha256 `f8b66b054e39ef31…`，111 条候选变体）
- 审核：110 条通过 / 1 条删除
- 审核库：`variants_audited.db`（110 条，sha256 `c397a52e3633cef0…`）
- 回放（同形值注入：正常值 + 黑名单值，结构不变）：189 通过 / 0 失败 / 0 跳过（黑名单值与原值异形，注入即改结构，属指纹保守边界）
- 哈希勘误（2026-10-08）：交付入库的 `initial_variants.db` 自
  c960030 起哈希即为 `df423388d4a99…`，与 `.sha256` 记录的
  `c397a52e…`（审核库 `variants_audited.db`，非交付副本）不一致。
  交付副本逻辑核验：110 条 / 全部 executable / 已删除条目
  bf7cbe60 不存在。`.sha256` 已更新为交付副本实际哈希，审核库
  原哈希以本条目为准。

## 删除条目（已知坏）

- `eds|10305` bf7cbe60 [FAIL:attribution_ok,pairing_ok,value_ok] 样例 `Get ns async failed, "s:[13,"ret(3).`（gold=eds|10304 target=s=15）

## 失败项分布

- attribution_ok: 1
- pairing_ok: 1
- value_ok: 1

## 规则库修正记录

- 2026-10-08：`vbs|4029` param_pos 修正 16→17（所有者授权；16 在当前
  分词坐标下指向 `[`，值在 17）。库内 4029 变体的 rule_id 内嵌旧
  param_pos（`vbs|4029|16|sig`），启动时按 target_definition_revision
  失效语义自动作废并经冷路径重学——**本库文件不变，sha256 持续有效**。
