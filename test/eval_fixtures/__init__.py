"""eval_pipeline 原版/优化版模块改绑机制的测试桩。

模拟真实场景的结构：原版 PIPLUP 通过 `from .preprocessing import preprocess`
和 `from .postprocessing import correct_single_template` 引用【当前版】模块
（与真实 PIPLUP_original 相同的引用方式），pipeline 需要把原版模块的
函数改绑到解析器命名空间，原版解析才能用上原版的预处理/后处理。
优化版多出的 token_structure 仅优化版引用，无需处理。
"""
