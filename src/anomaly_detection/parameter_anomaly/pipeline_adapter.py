"""首次解析结果直通（业务解析管线 → 参数检测共享输入）。

业务适配器（log_template_parsing_process_piplup.py）在组装日志
relation 时调用 attach_first_parse，把首次 PIPLUP 解析得到的模板
文本与送入解析器的去格式正文挂到 relation 实例上——公共 relation
类不扩展，兼容处理收敛在本包内；ParamAnomalyDetector._to_log_entry
优先读取 raw_content 完成取值。

规则身份用配置人工编号，解析模板 ID（crc32）与之分属两套命名
空间：路由按模板文本、取值按模板坐标在检测器内完成，两阶段
不共享位置坐标。
"""

__all__ = ["attach_first_parse"]


def attach_first_parse(relation, *, template, raw_content):
    """把首次解析结果挂到业务日志 relation 实例上，返回该实例。

    template 写入 relation.template（供模板回填与规则路由）；
    raw_content 写入 relation.raw_content（供参数取值）。
    """
    relation.template = template
    relation.raw_content = raw_content
    return relation
