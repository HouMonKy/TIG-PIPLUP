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
