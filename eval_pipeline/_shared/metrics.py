"""解析准确率指标：normalize_template + calculate_metrics_normalized。

复制自 src/evaluation/ParserMetrics.py。
"""
import re
import pandas as pd


def normalize_template(tmpl: str) -> str:
    """
    清洗日志解析模板，消除格式与自定义实体的干扰差异（保留大小写与单词边界）。
    """
    if not isinstance(tmpl, str):
        return ""

    # 1. 实体标签同构化映射 (已包含 PIPLUP 的 Short Template)
    tmpl = re.sub(r'<(MAC|IP|MAC-HEX|HEX4|HEX0x|NUM|CMD|Short Template)>', '<*>', tmpl)

    # 2. 将易引发对齐冲突的标点符号替换为【空格】
    tmpl = re.sub(r'[:=,\[\]\(\)\.\-\'\"]', ' ', tmpl)

    # 3. 连续空格折叠：将多个空格压缩为一个标准空格
    tmpl = re.sub(r'\s+', ' ', tmpl)

    # 4. 连续通配符折叠
    tmpl = re.sub(r'(<\*>\s*)+', '<*> ', tmpl)

    # 5. 剥离首尾多余空格，保留原始大小写
    return tmpl.strip()


def calculate_metrics_normalized(df_gt: pd.DataFrame, df_pred: pd.DataFrame) -> dict:
    """
    携带预处理平滑逻辑的指标计算管线。
    """
    gt = df_gt.copy()
    pred = df_pred.copy()

    gt['EventTemplate'] = gt['EventTemplate'].astype(str).apply(normalize_template)
    pred['EventTemplate'] = pred['EventTemplate'].astype(str).str.replace('<Short Template>', '<*>')
    pred['EventTemplate'] = pred['EventTemplate'].apply(normalize_template)

    total_lines = len(gt)

    correct_pa = (gt['EventTemplate'] == pred['EventTemplate']).sum()
    PA = correct_pa / total_lines

    gt_templates_set = set(gt['EventTemplate'].unique())
    pred_templates_set = set(pred['EventTemplate'].unique())
    correct_templates = gt_templates_set.intersection(pred_templates_set)

    PTA = len(correct_templates) / len(pred_templates_set) if pred_templates_set else 0
    RTA = len(correct_templates) / len(gt_templates_set) if gt_templates_set else 0
    FTA = 2 * PTA * RTA / (PTA + RTA) if (PTA + RTA) > 0 else 0

    gt_clusters = gt.groupby('EventId')['LineId'].apply(frozenset)
    pred_clusters = pred.groupby('EventId')['LineId'].apply(frozenset)

    gt_clusters_set = set(gt_clusters)
    pred_clusters_set = set(pred_clusters)

    accurate_lines_ga = sum(len(cluster) for cluster in pred_clusters_set if cluster in gt_clusters_set)
    GA = accurate_lines_ga / total_lines

    correct_clusters = gt_clusters_set.intersection(pred_clusters_set)

    PGA = len(correct_clusters) / len(pred_clusters_set) if pred_clusters_set else 0
    RGA = len(correct_clusters) / len(gt_clusters_set) if gt_clusters_set else 0
    FGA = 2 * PGA * RGA / (PGA + RGA) if (PGA + RGA) > 0 else 0

    return {
        "GA": GA, "PA": PA,
        "FGA": FGA, "FTA": FTA
    }


def calculate_metrics_gt_options(df_gt: pd.DataFrame, df_pred: pd.DataFrame) -> dict:
    """带可选项 GT 的指标计算：等价行的 template_alt 与 template 任一匹配即算对。

    df_gt 需含 EventTemplate 列，可选 EventTemplateAlt 列（equivalent 行的
    备选模板，其余行为空）。df_pred 需含 EventTemplate + EventId 列，
    两表均需已按 LineId 对齐排序。

    - PA：逐行 pred 归一化模板 ∈ {template, template_alt}
    - FTA：真值单元 = 按"可选项集合"分组（{main} 或 {main, alt}），
          单元被覆盖 = 任一选项出现在 pred 模板集合中
    - GA：pred 簇（LineId 集合）∈ 主变体簇族 ∪ 备变体簇族（备变体仅等价行换 alt）
    - FGA：簇真值单元按"去掉等价行后的核心行集合"归并主/备变体表示，
          单元被覆盖 = 该单元任一表示出现在 pred 簇中
    """
    gt = df_gt.copy()
    pred = df_pred.copy()

    gt['EventTemplate'] = gt['EventTemplate'].astype(str).apply(normalize_template)
    if 'EventTemplateAlt' in gt.columns:
        gt['EventTemplateAlt'] = gt['EventTemplateAlt'].fillna('').astype(str).apply(
            lambda t: normalize_template(t) if t.strip() else '')
    else:
        gt['EventTemplateAlt'] = ''
    pred['EventTemplate'] = pred['EventTemplate'].astype(str).apply(normalize_template)

    total_lines = len(gt)
    if total_lines == 0:
        return {"GA": 0.0, "PA": 0.0, "FGA": 0.0, "FTA": 0.0}

    # ===== PA：任一选项匹配即正确 =====
    pa_flags = [
        (p == m) or (bool(a) and p == a)
        for m, a, p in zip(gt['EventTemplate'], gt['EventTemplateAlt'], pred['EventTemplate'])
    ]
    PA = sum(pa_flags) / total_lines

    # ===== FTA：按可选项集合聚合真值单元 =====
    option_units = {}  # frozenset(options) -> 行数
    for m, a in zip(gt['EventTemplate'], gt['EventTemplateAlt']):
        opts = frozenset([m] + ([a] if a else []))
        option_units[opts] = option_units.get(opts, 0) + 1
    all_options = set()
    for opts in option_units:
        all_options |= opts

    pred_templates_set = set(pred['EventTemplate'])
    PTA = (sum(1 for t in pred_templates_set if t in all_options) / len(pred_templates_set)
           if pred_templates_set else 0.0)
    RTA = (sum(1 for opts in option_units if opts & pred_templates_set) / len(option_units)
           if option_units else 0.0)
    FTA = 2 * PTA * RTA / (PTA + RTA) if (PTA + RTA) > 0 else 0.0

    # ===== GA / FGA：主备两变体簇族 =====
    def _clusters(templates):
        d = {}
        for lid, t in zip(gt['LineId'], templates):
            d.setdefault(t, set()).add(lid)
        return set(frozenset(s) for s in d.values())

    clusters_main = _clusters(gt['EventTemplate'])
    # 备变体：等价行换 alt 模板，其余行保持主模板
    templates_alt = gt['EventTemplateAlt'].where(gt['EventTemplateAlt'] != '',
                                                 gt['EventTemplate'])
    clusters_alt = _clusters(templates_alt)
    acceptable_clusters = clusters_main | clusters_alt

    pred_clusters_set = set(frozenset(s) for s in pred.groupby('EventId')['LineId'].apply(frozenset))
    accurate_lines_ga = sum(len(c) for c in pred_clusters_set if c in acceptable_clusters)
    GA = accurate_lines_ga / total_lines
    PGA = (sum(1 for c in pred_clusters_set if c in acceptable_clusters) / len(pred_clusters_set)
           if pred_clusters_set else 0.0)

    # 簇真值单元：主/备变体中"去掉等价行后核心相同"的簇互为同一单元的不同表示
    equiv_ids = set(gt.loc[gt['EventTemplateAlt'] != '', 'LineId'])
    cluster_units = {}  # core(frozenset) -> set(簇表示集合)
    for c in acceptable_clusters:
        core = frozenset(lid for lid in c if lid not in equiv_ids)
        cluster_units.setdefault(core, set()).add(c)
    covered_units = sum(1 for reps in cluster_units.values()
                        if any(p in reps for p in pred_clusters_set))
    RGA = covered_units / len(cluster_units) if cluster_units else 0.0
    FGA = 2 * PGA * RGA / (PGA + RGA) if (PGA + RGA) > 0 else 0.0

    return {
        "GA": GA, "PA": PA,
        "FGA": FGA, "FTA": FTA
    }
