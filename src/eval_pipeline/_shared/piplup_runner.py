"""PIPLUP 运行器：子进程隔离执行一版 PIPLUP 并落盘 result/clusters。

统一 gt_generation / answer_generation / runner(compare) 三处重复的
worker 实现，并修复以下问题：

1. 原版模块改绑（关键）：原版与优化版在 preprocessing.py /
   postprocessing.py / PIPLUP.py 三个文件都有差异（优化版另有
   token_structure.py，原版算法不引用它，无需处理）。旧实现仅
   monkey-patch postprocessing，且依赖 import 顺序——先 import
   解析器模块再 patch 时，`from .postprocessing import ...` 的
   旧绑定使 patch 完全失效（compare 模块即此情形）；且
   preprocessing 的差异从未被处理，"原版"一直混用优化版预处理。
   现改为：import 解析器模块后，直接把原版 preprocess /
   correct_single_template 改绑到解析器模块命名空间，不依赖
   import 顺序。函数对象自带原模块 globals，原版内部辅助
   （sequence/_apply_cleanup 等）自动随原版模块解析。

2. 子进程直写文件：结果由子进程直接落盘，父进程从磁盘加载，
   避免 Pipe 全量序列化 + 父进程重复写盘的双份开销。

3. 健壮性：缓存删除容忍并发竞态（FileNotFoundError/PermissionError）；
   子进程异常以 {'ok': False, 'error': traceback} 回传而非让
   parent.recv() 裸抛 EOFError。
"""
import importlib
import json
import os
import time
import traceback
from multiprocessing import Process, Pipe
from pathlib import Path

from eval_pipeline._shared.data_loader import load_jsonl, write_json


def _remove_cache_file(cache_file):
    """删除持久化缓存文件，容忍并发竞态与文件不存在。"""
    for attempt in range(5):
        try:
            os.remove(cache_file)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
        except OSError:
            return


def _install_original_modules(parser_mod, preprocessing_orig_path, postprocessing_orig_path):
    """把原版 preprocess / correct_single_template 改绑到解析器模块命名空间。

    直接改绑模块属性，不依赖 import 顺序（旧 monkey-patch 方式在
    '先 import 后 patch' 时是无效的）。token_structure 仅优化版
    PIPLUP 引用，原版算法不涉及，无需处理。
    """
    if preprocessing_orig_path:
        orig_pp = importlib.import_module(preprocessing_orig_path)
        if not hasattr(orig_pp, 'preprocess'):
            raise AttributeError(
                f"{preprocessing_orig_path} 缺少 preprocess 函数，请确认原版预处理模块路径")
        parser_mod.preprocess = orig_pp.preprocess
    if postprocessing_orig_path:
        orig_post = importlib.import_module(postprocessing_orig_path)
        if not hasattr(orig_post, 'correct_single_template'):
            raise AttributeError(
                f"{postprocessing_orig_path} 缺少 correct_single_template 函数，"
                "请确认原版后处理模块路径")
        parser_mod.correct_single_template = orig_post.correct_single_template


def _piplup_worker(conn, input_path, parser_module_path, output_dir, label,
                   module_name, cold_start, scenarios, data_root,
                   preprocessing_orig_path, postprocessing_orig_path):
    """子进程 worker：运行一版 PIPLUP，直接落盘，回传路径与统计。"""
    try:
        import gc
        import sys
        from pathlib import Path as _Path

        project_root = _Path(__file__).resolve().parent.parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from eval_pipeline._shared.data_loader import (
            load_jsonl, load_raw_lines, load_raw_lines_by_scenarios, build_log_info,
        )
        from src.data_process.log_context import LogContext
        from src.common.global_config import GlobalConfig
        from src.data_process.template_extractor.log_template_parsing_process_piplup import (
            parse_logs_piplup_adapter,
        )

        # 加载数据（与 worker 内构建 logId 的顺序决定 log_id，须稳定）
        if input_path and str(input_path).endswith('.jsonl'):
            log_info = load_jsonl(input_path)
        elif scenarios:
            raw_lines = load_raw_lines_by_scenarios(data_root, scenarios)
            log_info = build_log_info(raw_lines, module_name)
            del raw_lines
        else:
            raw_lines = load_raw_lines(input_path)
            log_info = build_log_info(raw_lines, module_name)
            del raw_lines

        # 清缓存（容忍竞态：并行运行时另一子进程可能已删除）
        cache_file = os.path.join(
            GlobalConfig.get_project_root(), "config", "persistence",
            f"{module_name}_piplup_cache.pkl")
        if cold_start:
            _remove_cache_file(cache_file)
        gc.collect()

        # import 解析器模块后，把原版 preprocess / correct_single_template
        # 改绑到解析器模块命名空间（不依赖 import 顺序）
        parser_mod = importlib.import_module(parser_module_path)
        _install_original_modules(
            parser_mod, preprocessing_orig_path, postprocessing_orig_path)
        parser_class = parser_mod.LogParser

        context = LogContext(module_name=module_name)
        context.parser_piplup = parser_class(
            log_format='<Content>', module_name=module_name,
            use_cache=(not cold_start),
        )

        results = parse_logs_piplup_adapter(log_info, context)

        # 子进程直接落盘
        out = _Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        result_path = out / f"{label}_result.jsonl"
        with open(result_path, 'w', encoding='utf-8') as f:
            for r in results:
                f.write(json.dumps({
                    'log_id': r['log_id'],
                    'template': r['template'],
                    'template_id': r['template_id'],
                }, ensure_ascii=False) + '\n')

        clusters = []
        for idx, cluster in enumerate(context.parser_piplup.logCluL):
            clusters.append({
                'cluster_id': idx,
                'template': cluster.templateList[0] if cluster.templateList else '',
                'hit_count': cluster.hit_time,
                'num_templates': len(cluster.templateList),
            })
        cluster_path = out / f"{label}_clusters.json"
        with open(cluster_path, 'w', encoding='utf-8') as f:
            json.dump(clusters, f, ensure_ascii=False, indent=2)

        conn.send({
            'ok': True,
            'label': label,
            'result_path': str(result_path),
            'cluster_path': str(cluster_path),
            'n_results': len(results),
            'n_clusters': len(clusters),
        })
    except Exception:
        conn.send({'ok': False, 'label': label, 'error': traceback.format_exc()})
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_run_outputs(payload):
    """从子进程落盘的文件加载 results / clusters（父进程免序列化传输）。"""
    results = load_jsonl(payload['result_path'])
    clusters = json.load(open(payload['cluster_path'], encoding='utf-8'))
    return results, clusters


def run_piplup_and_save(input_path, parser_module_path, output_dir, label,
                        module_name="Messages", cold_start=True, scenarios=None,
                        data_root=None, preprocessing_orig_path=None,
                        postprocessing_orig_path=None):
    """运行一版 PIPLUP（子进程隔离），落盘并返回 (results, clusters)。"""
    parent_conn, child_conn = Pipe()
    p = Process(
        target=_piplup_worker,
        args=(child_conn, input_path, parser_module_path, output_dir, label,
              module_name, cold_start, scenarios, data_root,
              preprocessing_orig_path, postprocessing_orig_path),
    )
    p.start()
    payload = parent_conn.recv()
    p.join()
    parent_conn.close()

    if not payload.get('ok'):
        raise RuntimeError(
            f"PIPLUP[{label}] 子进程执行失败:\n{payload.get('error', 'unknown')}")

    print(f"  {label}: {payload['n_results']} 行, {payload['n_clusters']} 簇 "
          f"→ {Path(payload['result_path']).name}")
    return _load_run_outputs(payload)


def run_piplup_pair(input_path, output_dir, module_name, cold_start,
                    scenarios, data_root,
                    piplup_orig_path, preprocessing_orig_path, postprocessing_orig_path,
                    piplup_opt_path, parallel=None):
    """运行原版+优化版两版 PIPLUP，返回 (results_orig, clusters_orig, results_opt, clusters_opt)。

    parallel=None（默认）: 冷启动时并行执行（两版互不依赖，gt/answer 无资源
    测量需求）；warm 模式存在共享缓存文件读取，保持串行。
    compare 模块因需公平采集资源指标，不应使用本函数。
    """
    common = dict(input_path=input_path, output_dir=output_dir, module_name=module_name,
                  cold_start=cold_start, scenarios=scenarios, data_root=data_root)
    if parallel is None:
        parallel = bool(cold_start)

    if not parallel:
        results_orig, clusters_orig = run_piplup_and_save(
            parser_module_path=piplup_orig_path, label="piplup_orig",
            preprocessing_orig_path=preprocessing_orig_path,
            postprocessing_orig_path=postprocessing_orig_path, **common)
        results_opt, clusters_opt = run_piplup_and_save(
            parser_module_path=piplup_opt_path, label="piplup_opt",
            **common)
        return results_orig, clusters_orig, results_opt, clusters_opt

    # 并行：两个子进程同时启动（冷启动下仅删除同一缓存文件，已做竞态容忍）
    conns, procs = [], []
    variants = [
        ("piplup_orig", piplup_orig_path,
         dict(preprocessing_orig_path=preprocessing_orig_path,
              postprocessing_orig_path=postprocessing_orig_path)),
        ("piplup_opt", piplup_opt_path, {}),
    ]
    try:
        for label, parser_path, extra in variants:
            parent_conn, child_conn = Pipe()
            p = Process(
                target=_piplup_worker,
                args=(child_conn, input_path, parser_path, output_dir, label,
                      module_name, cold_start, scenarios, data_root,
                      extra.get('preprocessing_orig_path'),
                      extra.get('postprocessing_orig_path')),
            )
            p.start()
            conns.append(parent_conn)
            procs.append(p)
        payloads = [conn.recv() for conn in conns]
    finally:
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        for p in procs:
            p.join()

    results = []
    for payload in payloads:
        if not payload.get('ok'):
            raise RuntimeError(
                f"PIPLUP[{payload.get('label')}] 子进程执行失败:\n{payload.get('error', 'unknown')}")
        results.append(_load_run_outputs(payload))

    (results_orig, clusters_orig), (results_opt, clusters_opt) = results
    print(f"  piplup_orig: {len(results_orig)} 行, {len(clusters_orig)} 簇（并行）")
    print(f"  piplup_opt:  {len(results_opt)} 行, {len(clusters_opt)} 簇（并行）")
    return results_orig, clusters_orig, results_opt, clusters_opt
