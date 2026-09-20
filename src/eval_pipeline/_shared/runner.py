"""子进程隔离执行器，每算法独立进程。

Windows 使用 spawn 模式，worker 必须是顶层函数。
通过 Pipe 传递结果。复制自 benchmark/runner.py 并改造为支持动态 PIPLUP 模块路径。

注意：compare 模块需要公平采集资源指标，三算法必须串行执行；
原版/优化版 PIPLUP 的 preprocessing / postprocessing 差异通过
_shared/piplup_runner._install_original_modules 直接改绑到解析器
模块命名空间（不依赖 import 顺序——旧的 monkey-patch 方式在
'先 import 解析器模块、后 patch' 的顺序下完全失效）。
"""
import importlib
import traceback
from multiprocessing import Process, Pipe

from eval_pipeline._shared.piplup_runner import (
    _install_original_modules, _remove_cache_file,
)


def _worker(conn, algo_key, input_path, cold_start, module_name,
            piplup_orig_path, piplup_opt_path, postprocessing_orig_path,
            preprocessing_orig_path, data_root, scenario_names):
    """顶层 worker 函数，可被 pickle。

    在子进程内加载数据 + 运行算法 + 返回结果。
    支持两种输入模式：
    - input_path 为 JSONL 文件 → 直接加载
    - input_path 为文件夹 + scenario_names → 按场景加载 .log
    """
    import os, sys, gc, time
    from pathlib import Path

    # 确保 PROJECT_ROOT 在 sys.path
    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from eval_pipeline._shared.data_loader import load_jsonl, load_raw_lines, load_raw_lines_by_scenarios, build_log_info
    from eval_pipeline._shared.resource_monitor import ResourceMonitor

    # 加载数据
    if input_path and str(input_path).endswith('.jsonl'):
        log_info = load_jsonl(input_path)
    elif scenario_names:
        raw_lines = load_raw_lines_by_scenarios(data_root, scenario_names)
        log_info = build_log_info(raw_lines, module_name)
        del raw_lines
    else:
        raw_lines = load_raw_lines(input_path)
        log_info = build_log_info(raw_lines, module_name)
        del raw_lines

    # 运行算法
    if algo_key == "drain":
        r = _run_drain(log_info, cold_start, module_name)
    elif algo_key == "piplup_orig":
        # 先 import 解析器模块，再把原版 preprocess / correct_single_template
        # 改绑到其命名空间（与 gt/answer 模块同一机制，保证跨模块行为一致）
        parser_mod = importlib.import_module(piplup_orig_path)
        _install_original_modules(parser_mod, preprocessing_orig_path, postprocessing_orig_path)
        r = _run_piplup(log_info, cold_start, module_name, parser_mod.LogParser)
    elif algo_key == "piplup_opt":
        parser_class = importlib.import_module(piplup_opt_path).LogParser
        r = _run_piplup(log_info, cold_start, module_name, parser_class)
    else:
        raise ValueError(f"未知算法: {algo_key}")

    del log_info
    conn.send(r)
    conn.close()


def _run_drain(log_info, cold_start, module_name):
    """Drain3 — 通过 parse_logs_dem 端到端入口。"""
    import os, time
    from src.data_process.log_context import LogContext
    from src.data_process.template_extractor.log_template_parsing_process import parse_logs_dem
    from src.common.global_config import GlobalConfig
    from eval_pipeline._shared.resource_monitor import ResourceMonitor
    from eval_pipeline.config import WARMUP_LINES

    context = LogContext(module_name=module_name)

    if cold_start:
        if module_name in context.template_miner_map:
            del context.template_miner_map[module_name]
        bin_file = os.path.join(GlobalConfig.get_project_root(), "config", "persistence", f"{module_name}_extractor_state.bin")
        if os.path.exists(bin_file):
            os.remove(bin_file)

    if not cold_start:
        parse_logs_dem(log_info[:WARMUP_LINES], context)
        context.log_repository.clear()

    monitor = ResourceMonitor()
    monitor.start()
    t0 = time.perf_counter()
    results = parse_logs_dem(log_info, context)
    elapsed = time.perf_counter() - t0
    monitor.stop()

    drain = context.get_drain(module_name)
    skip_count = len(log_info) - len(results)
    unique_templates = len(set(r['template'] for r in results))
    total_clusters = len(drain.extractor.id_to_cluster) if hasattr(drain.extractor, 'id_to_cluster') else 0

    return {
        'throughput': len(log_info) / elapsed if elapsed > 0 else 0,
        'elapsed': elapsed, 'nlines': len(log_info),
        'peak_rss': monitor.peak_rss_mb, 'steady_rss': monitor.steady_rss_mb,
        'rss_delta': monitor.rss_delta_mb,
        'avg_cpu': monitor.avg_cpu_pct, 'peak_cpu': monitor.peak_cpu_pct,
        'cpu_time': monitor.cpu_time_s,
        'skip_count': skip_count,
        'unique_templates': unique_templates,
        'total_clusters': total_clusters,
    }


def _run_piplup(log_info, cold_start, module_name, parser_class):
    """PIPLUP — 通过 parse_logs_piplup_adapter 端到端入口。

    parser_class: LogParser 类（原版或优化版）。原版模块的
    preprocess / correct_single_template 已由调用方改绑完成。
    """
    import os, gc, time
    from src.data_process.log_context import LogContext
    from src.data_process.template_extractor.log_template_parsing_process_piplup import parse_logs_piplup_adapter
    from src.common.global_config import GlobalConfig
    from eval_pipeline._shared.resource_monitor import ResourceMonitor
    from eval_pipeline.config import WARMUP_LINES

    cache_file = os.path.join(GlobalConfig.get_project_root(), "config", "persistence", f"{module_name}_piplup_cache.pkl")

    if cold_start:
        _remove_cache_file(cache_file)
    gc.collect()

    context = LogContext(module_name=module_name)
    context.parser_piplup = parser_class(
        log_format='<Content>', module_name=module_name,
        use_cache=(not cold_start)
    )

    if not cold_start:
        parse_logs_piplup_adapter(log_info[:WARMUP_LINES], context)
        context.log_repository.clear()

    monitor = ResourceMonitor()
    monitor.start()
    t0 = time.perf_counter()
    results = parse_logs_piplup_adapter(log_info, context)
    elapsed = time.perf_counter() - t0
    monitor.stop()

    unique_templates = len(set(r['template'] for r in results))
    total_clusters = len(context.parser_piplup.logCluL)

    return {
        'throughput': len(log_info) / elapsed if elapsed > 0 else 0,
        'elapsed': elapsed, 'nlines': len(log_info),
        'peak_rss': monitor.peak_rss_mb, 'steady_rss': monitor.steady_rss_mb,
        'rss_delta': monitor.rss_delta_mb,
        'avg_cpu': monitor.avg_cpu_pct, 'peak_cpu': monitor.peak_cpu_pct,
        'cpu_time': monitor.cpu_time_s,
        'skip_count': 0,
        'unique_templates': unique_templates,
        'total_clusters': total_clusters,
    }


def run_in_subprocess(algo_key, input_path=None, cold_start=True, module_name="Messages",
                      piplup_orig_path=None, piplup_opt_path=None, postprocessing_orig_path=None,
                      preprocessing_orig_path=None,
                      data_root=None, scenario_names=None):
    """子进程隔离运行。返回结果 dict。

    Args:
        algo_key: "drain" / "piplup_orig" / "piplup_opt"
        input_path: JSONL 文件路径（优先使用）
        cold_start: True=清缓存
        module_name: 日志模块名
        piplup_orig_path: PIPLUP 原版模块路径（Python import 路径）
        piplup_opt_path: PIPLUP 优化版模块路径
        postprocessing_orig_path: 原版后处理模块路径
        preprocessing_orig_path: 原版预处理模块路径
        data_root: 数据根目录（scenario_names 模式时使用）
        scenario_names: 场景名列表（可选）
    """
    parent_conn, child_conn = Pipe()
    p = Process(
        target=_worker,
        args=(child_conn, algo_key, input_path, cold_start, module_name,
              piplup_orig_path, piplup_opt_path, postprocessing_orig_path,
              preprocessing_orig_path, data_root, scenario_names),
    )
    p.start()
    try:
        result = parent_conn.recv()
    except EOFError:
        p.join(timeout=10)
        raise RuntimeError(
            f"算法[{algo_key}] 子进程崩溃（无返回结果），请检查子进程堆栈") from None
    finally:
        parent_conn.close()
    p.join()
    return result
