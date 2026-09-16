"""
test_log_parsing_engine.py

日志解析引擎自动化测试脚本 (基于 pytest)
执行命令: pytest test_log_parsing_engine.py -v
"""
import pytest
import datetime
import time
import types

from src.data_process.template_extractor.log_formatter_parse import LogFormatParser
from src.data_process.template_extractor.parsing_algorithm.tools.llm_client_all import llm_client_stream
from src.data_process.template_extractor.parsing_algorithm.LogAutoParsingEngine import LogExtractionEngine

# ==========================================
# 0. 基础设施健康检查 (Health Check & Latency)
# ==========================================
def test_llm_client_connectivity_and_latency():
    """
    测试 LLM 接口的连通性与响应延迟。
    作为前置基础测试，确保大模型服务可用且性能达标。
    """
    test_prompt = '你好！请仅回复“收到”两个字。'
    max_allowed_latency = 30.0  # 设定期望的最大延迟阈值（秒），可根据实际情况调整

    start_time = time.perf_counter()

    try:
        # 发起调用
        response = llm_client_stream(test_prompt)

    except Exception as e:
        pytest.fail(f"LLM 接口调用直接崩溃！抛出异常: {e}")

    # 计算精确耗时
    elapsed_time = time.perf_counter() - start_time

    # ==========================================
    # 断言层
    # ==========================================
    # 1. 连通性与内容断言
    assert response is not None, "LLM 返回了 None"
    assert isinstance(response, str), f"LLM 返回类型错误，期望 str，实际为 {type(response)}"
    assert len(response.strip()) > 0, "LLM 成功连通，但返回了空字符串"

    # 2. 性能延迟断言
    assert elapsed_time < max_allowed_latency, (
        f"LLM 响应严重超时！\n"
        f"设定阈值: {max_allowed_latency} 秒\n"
        f"实际耗时: {elapsed_time:.2f} 秒\n"
        f"网络可能波动，或者模型服务负载过高。"
    )

    # 如果在控制台使用 pytest -s 运行，可以看到这行输出
    print(f"\n[健康检查通过] LLM 耗时: {elapsed_time:.2f} 秒 | 响应内容: {response}")


# ==========================================
# 1. 核心资源初始化 (Fixture)
# ==========================================
@pytest.fixture(scope="module")
def parsing_engine():
    """
    初始化解析引擎。
    使用 module 级别作用域，使得多条测试用例复用同一个引擎实例。
    这不仅能加速测试，还能有效验证引擎的 "正则记忆与优先命中" 逻辑。
    """
    log_format = {'osd': {'log_format': []}}
    format_parse = LogFormatParser(log_format)
    engine = LogExtractionEngine(format_parse, llm_client_stream)
    return engine

# ==========================================
# 2. 测试用例数据集 (Data-Driven)
# ==========================================
TEST_CASES = [
    # 参数定义: (输入原始日志, 期望提取出的核心内容子串)
    (
        '2026-06-10 10:30:15.123 [INFO] System started successfully',
        'System started successfully'
    ),
    (
        '[2025-08-01 13:00:00.179][1500000670000][ERROR] Connection failed',
        'Connection failed'
    ),
    (
        'Jul 22 09:31:02 hostname process[12345]: Error occurred',
        'Error occurred'
    ),
    (
        '2026-06-10 10:30:15.123 [WARN] process[54321] Memory usage high',
        'Memory usage high'
    )
]

# ==========================================
# 3. 核心测试逻辑与断言层 (Assertions)
# ==========================================
@pytest.mark.parametrize("raw_log, expected_msg_substring", TEST_CASES)
def test_log_extraction(parsing_engine, raw_log, expected_msg_substring):
    """验证 LLM 驱动的日志特征提取功能"""

    # 执行目标函数
    ts, msg = parsing_engine.extract(raw_log)

    # 1. 基础状态断言 (非空验证)
    assert ts is not None, f"时间戳解析失败，引擎返回 None。原始日志: {raw_log}"
    assert msg is not None, f"内容解析失败，引擎返回 None。原始日志: {raw_log}"

    # 2. 数据类型断言 (向下游兼容性保证)
    assert isinstance(ts, (int, float)), f"时间戳数据类型异常。期望 int/float，实际为 {type(ts)}"
    assert isinstance(msg, str), f"内容数据类型异常。期望 str，实际为 {type(msg)}"

    # 3. 时间戳有效性断言
    assert ts > 0, f"时间戳数值异常，计算结果小于等于 0: {ts}"

    # 验证是否为毫秒级时间戳 (13位) 或进行转换验证
    try:
        # LLM 解析返回的是毫秒，需转换为秒进行 datetime 验证
        dt = datetime.datetime.fromtimestamp(ts / 1000)
        assert dt.year > 1970, f"时间戳转换后年份异常: {dt.year}"
    except Exception as e:
        pytest.fail(f"时间戳转换为 datetime 对象时发生崩溃: {e}")

    # 4. 业务逻辑断言 (内容纯净度验证)
    # 验证提取出的 msg 是否包含了真正有价值的日志实体，而非丢弃了核心信息
    assert expected_msg_substring in msg, (
        f"内容提取精度不足。\n"
        f"期望包含: '{expected_msg_substring}'\n"
        f"实际提取: '{msg}'"
    )