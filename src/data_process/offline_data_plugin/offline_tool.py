import ast
import os
import json
import re

import regex
from src.anomaly_detection.anomaly_detection import AnomalyDetection
from src.data_process.log_context import LogContext
from src.data_process.data_basic_structs.basic_structs import LRUCache
from src.data_process.log_context import abs_path
from src.data_process.offline_data_plugin.mae_data_plugin.log_entry import EnhancedLogEntry, LogEntry
from src.data_process.template_extractor.log_template_repository import LogTemplateRepository, LogTemplateRelation
from src.data_process.template_extractor.log_formatter_parse import LogFormatParser
from src.data_process.template_extractor.parsing_algorithm import FilePersistence, TemplateMinerConfig, TemplateMiner

from src.anomaly_detection.anomaly_base.data_models import Tag, TemplateInfo, Log, AnomalyEvent
# from src.evaluation.metrics import ResultEvaluator
from src.common.constant import FILE_PERSISTENCE_FILE, TEMPLATE_MINER_CONFIG_PATH, MEM_DATA_DURATION_TIME, \
    DME_TEMPLATE_FORMAT_PATH, WHITE_LIST_PATH, MAE_TEMPLATE_FORMAT_PATH


class OffLineTool:
    def __init__(self):
        self.log_template_repository = None
        self.format_dict = None
        self.template_miner = None
        self.parse = None
        self.logs = []
        # initialize configs
        self.init_log_format()
        self.init_template_extractor()
        self.log_template_repository = LogTemplateRepository(MEM_DATA_DURATION_TIME)

    def init_template_extractor(self):
        persistence = FilePersistence(abs_path(FILE_PERSISTENCE_FILE))
        config = TemplateMinerConfig()
        extractor_ini_path = abs_path(TEMPLATE_MINER_CONFIG_PATH)
        config.load(extractor_ini_path)
        config.profiling_enabled = False
        self.template_miner = TemplateMiner(persistence, config)
        print("miner", self.template_miner)

    def init_log_format(self, product_name=None):
        try:
            if product_name == "MAE":
                target_path = abs_path(MAE_TEMPLATE_FORMAT_PATH)
            else:
                target_path = abs_path(DME_TEMPLATE_FORMAT_PATH)
            with open(target_path, 'r') as file:
                self.format_dict = json.load(file)
                self.parse = LogFormatParser(log_format=self.format_dict)
            print("format dict", self.format_dict)
            print("parse", self.parse)
        except FileNotFoundError:
            print("文件未找到")
        except json.JSONDecodeError:
            print("文件不是有效的 JSON 格式")
        except Exception as e:
            print(f"发生了错误: {e}")

    def detection_log(self, path, out_path, module, source, start, end, window_size):
        if source not in self.format_dict:
            return
        with open(path, 'r', encoding='utf-8') as file:
            self.logs = file.readlines()
        cache = LRUCache(10000)
        log_id = 1
        for log in self.logs:
            time, content = self.parse.format(log, source)
            if content == '' or time == 0:
                continue
            masked_log = self.template_miner.masker.mask(content)
            template_id = cache.get(masked_log)
            if template_id is None:
                split_result = regex.split(r'([:=,\[\]\(\)])', content)
                if len(split_result) > 1:
                    masked_log = ' '.join(split_result)
                cluster = self.template_miner.match(masked_log)
                if cluster is None:
                    result = self.template_miner.add_log_message(masked_log)
                    template = result['template_mined']
                    template_id = result['cluster_id']
                else:
                    template = cluster.get_template()
                    template_id = cluster.cluster_id
                cache.put(masked_log, template_id)
            else:
                template = self.template_miner.extractor.id_to_cluster.get(template_id).get_template()

            if template == '<*>':
                template = '<Short Template>'
            self.log_template_repository.add(LogTemplateRelation(source, time, log_id, template_id, module))
            log_id = log_id + 1
        white_list = []
        target_path = abs_path(WHITE_LIST_PATH)
        with open(target_path, 'r', encoding='utf-8') as file:
            white_list_dict = json.load(file)
            if module in white_list_dict:
                white_list = white_list_dict[module]

        data, event = AnomalyDetection.get_detector().anomaly_detection(source, module, start, end, window_size,
                                                         self.log_template_repository, self.template_miner, white_list)
        # data2, event2 = AnomalyDetection.semantic_anomaly_detection(source, module, start, end, window_size, self.log_template_repository, self.template_miner)
        # data2 = AnomalyDetection.bert_anomaly_detection(source, module, start, end, window_size, self.log_template_repository, self.template_miner)
        # data.extend(data2)
        # event.extend(event2)

        dic = {"data": data, "event": event}

        with open(out_path, 'a+', encoding='utf-8') as file:
            file.write(json.dumps(dic, ensure_ascii=False))

    def evaluate_dme_log(self, org_path, res_path, label_path, out_path, module, source, start, end, window_size):
        if source not in self.format_dict:
            return

        label_logs = []
        for filename in os.listdir(label_path):
            if filename.endswith('.json'):
                file_path = os.path.join(label_path, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as file:
                        data = json.load(file)
                        if data["fileName"] == os.path.basename(org_path) and data["module"] == module:
                            file_name = data["fileName"]
                            rule = data["rule"]
                            label_logs.append(data["line"])
                except json.JSONDecodeError:
                    print(f"警告: 文件 {file_path} 不是有效的 JSON 文件")

        labeled_templates = []
        cache = LRUCache(10000)
        log_id = 1
        for log in label_logs:
            time, content = self.parse.format(log, source)
            if content == '' or time == 0:
                continue
            masked_log = self.template_miner.masker.mask(content)
            template_id = cache.get(masked_log)
            if template_id is None:
                split_result = regex.split(r'([:=,\[\]\(\)])', content)
                if len(split_result) > 1:
                    masked_log = ' '.join(split_result)
                cluster = self.template_miner.match(masked_log)
                if cluster is None:
                    result = self.template_miner.add_log_message(masked_log)
                    template = result['template_mined']
                    template_id = result['cluster_id']
                else:
                    template = cluster.get_template()
                    template_id = cluster.cluster_id
                cache.put(masked_log, template_id)
            else:
                template = self.template_miner.extractor.id_to_cluster.get(template_id).get_template()

            if template == '<*>':
                template = '<Short Template>'

            labeled_templates.append(
                TemplateInfo(
                    templateId=template_id,
                    template=template,
                    log=Log(
                        source=source,
                        logId=log_id,
                        rawLog=log,
                        timestamp=time,
                    )
                )
            )

            log_id = log_id + 1

        labels = Tag(
            fileName=file_name,
            source=source,
            format="",
            lines=labeled_templates,
            rule="",
        )

        with open(res_path, 'r', encoding='utf-8') as file:
            predictions = json.load(file)
        preds = [AnomalyEvent.from_data(x) for x in predictions["event"]]
        eval = ResultEvaluator(start_time=start, end_time=end, window_size=window_size)
        res = eval.evaluate(preds, labels)

        with open(out_path, 'a+', encoding='utf-8') as file:
            file.write(json.dumps(res, ensure_ascii=False))
        return res


class LogTemplateProcessor:
    def __init__(self, root_dir, tool):
        self.root_dir = root_dir
        self.tool = tool
        self.case_template_results = {}

    def process_case(self, case, log_entries):
        if not log_entries:
            print(f"警告: CASE {case} 无日志条目")
            return False

        first_entry = log_entries[0]
        module = first_entry.module
        file_path = first_entry.file_path

        if module not in self.tool.format_dict:
            print(f"跳过未支持的模块：{module}")
            return False

        full_log_path = self._get_full_log_path(file_path)
        patterns = self._compile_patterns(log_entries)
        cache = LRUCache(10000)
        log_id = 1
        rows = []

        try:
            logs = self._read_log_file(full_log_path)
            for log_line in logs:
                processed = self._process_log_line(log_line, module, cache, log_id)
                if not processed:
                    continue

                time, content, template_id, template, param_list = processed
                rows.append({
                    'time': time,
                    'content': content,
                    'template': template,
                    'template_id': template_id,
                    'param_list': str(param_list)
                })

                self._update_log_entries(log_line, template_id, template, param_list, log_entries, patterns)
                log_id += 1

            self.case_template_results[case] = rows
            return True

        except Exception as e:
            print(f"处理日志文件时出错：{full_log_path} - {str(e)}")
            return False

    def process_case_mae(self, case, log_entries):
        if not log_entries:
            print(f"警告: CASE {case} 无日志条目")
            return False

        first_entry = log_entries[0]
        module = first_entry.module
        file_path = first_entry.file_path

        if module not in self.tool.format_dict:
            print(f"跳过未支持的模块：{module}")
            return False

        patterns = self._compile_patterns(log_entries)
        cache = LRUCache(10000)
        log_id = 1
        rows = []

        try:
            logs = self._read_log_file(file_path)
            for log_line in logs:
                processed = self._process_log_line(log_line, module, cache, log_id)
                if not processed:
                    continue

                rows.append({
                    'module': processed.get('module', ''),
                    'time': processed.get('time', ''),
                    'log_id': processed.get('log_id', 0),
                    'log_content': processed.get('log_content', ''),
                    'template_id': processed.get('template_id', 0),
                    'template': processed.get('template', ''),
                    'param_list': processed.get('param_list', [])
                })

                patterns = self._update_log_entries_mae(processed, patterns)
                if not patterns:  # 如果所有 patterns 都已匹配，则跳出循环
                    break

                log_id += 1

            self.case_template_results[case] = rows
            return True

        except Exception as e:
            print(f"处理日志文件时出错：{file_path} - {str(e)}")
            return False

    def _get_full_log_path(self, file_path):
        rel_path = file_path.split('\\', 4)[4]
        return os.path.normpath(os.path.join(self.root_dir, rel_path))

    @staticmethod
    def _compile_patterns(log_entries):
        patterns = []
        for entry in log_entries:
            pattern = re.compile(re.escape(entry.line_content))
            patterns.append((pattern, entry))
        return patterns

    @staticmethod
    def _read_log_file(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.readlines()
        except Exception as e:
            print(f"读取日志文件失败: {file_path} - {str(e)}")
            return []

    def _process_log_line(self, log_line, module, cache, log_id):
        time, content = self.tool.parse.format(log_line, module)
        if not content or time == 0:
            return None

        masked_log = self.tool.template_miner.masker.mask(content)
        template_id = cache.get(masked_log)

        if template_id is None:
            split_result = re.split(r'([:=,\[\]\(\)])', masked_log)
            masked_log = ' '.join(split_result) if len(split_result) > 1 else masked_log
            cluster = self.tool.template_miner.match(masked_log)
            if not cluster:
                result = self.tool.template_miner.add_log_message(masked_log)
                template = result['template_mined']
                template_id = result['cluster_id']
            else:
                template = cluster.get_template()
                template_id = cluster.cluster_id
            cache.put(masked_log, template_id)
        else:
            template = self.tool.template_miner.extractor.id_to_cluster[template_id].get_template()

        if template == '<*>':
            template = '<Short Template>'

        split_result = re.split(r'([:=,\[\]\(\)])', content)
        split_content = content.strip()
        if len(split_result) > 1:
            split_content = ' '.join(split_result)

        drain: TemplateMiner = LogContext('MAE').get_drain(module)
        parameters = drain.extract_parameters(template, split_content)
        param_list = [p.value for p in parameters] if parameters else []
        self.tool.log_template_repository.add(LogTemplateRelation(module, time, log_id, template_id, module, param_list, template))
        parse_result = {'module': module, 'time': time, 'log_id': log_id, 'log_content': content, 'template_id': template_id, 'template': template,  'param_list': param_list}
        return parse_result

    @staticmethod
    def _update_log_entries(processed_result, patterns):
        """更新匹配到的日志条目"""
        for pattern, entry in patterns:
            if pattern.search(processed_result.get('log_content', '')):
                entry.template_id = processed_result.get('template_id', 0)
                entry.template = processed_result.get('template', '')
                entry.param_list = processed_result.get('param_list', [])
                entry.timestamp = processed_result.get('time', 0)
                break

    @staticmethod
    def _update_log_entries_mae(processed_result, patterns):
        """更新匹配到的日志条目，并删除已匹配的 pattern-entry 组合"""
        i = 0
        while i < len(patterns):
            pattern, entry = patterns[i]
            if pattern.search(processed_result.get('log_content', '')):
                # 更新日志条目信息
                entry.template_id = processed_result.get('template_id', 0)
                entry.template = processed_result.get('template', '')
                entry.param_list = processed_result.get('param_list', [])
                entry.timestamp = processed_result.get('time', 0)
                # 删除已匹配的 pattern-entry 组合
                del patterns[i]
                break  # 匹配到后退出循环
            else:
                i += 1
        return patterns

def read_file_with_encoding(file_path):
    encodings = ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin-1']
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
return f.readlines()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(f"无法使用任何编码读取文件：{file_path}")


def optimize_csv(csv_data, csv_path):
    # 读取CSV文件
    df = csv_data
    file_path = csv_path

    # 过滤掉template_id为空的行
    df = df[df['template_id'].notna()]

    # 按CASE和template_id去重，保留第一次出现的记录
    df = df.drop_duplicates(subset=['CASE', 'template_id'], keep='first')

    # 保存优化后的结果到新文件
    output_path = file_path.replace('.csv', '_update.csv')
    df.to_csv(output_path, index=False, encoding='utf-8-sig')
    return


def extract_all_bracket_tokens(s: str) -> list:
    matches = re.findall(r"<([^>]+)>", s)
    if matches:
        return [f"<{match}>" for match in matches]
    return []


def save_enhanced_entries_to_json_mae(entries, output_path):
    try:
        special_tokens = {'<MAC>', '<IP>', '<MAC-HEX>', '<HEX4>', '<HEX0x>', '<NUM>', '<CMD>', '<*>'}
        deduped_entries = []
        seen_keys = set()

        # 反转列表，保证去重保留下来的是见过大量数据之后产生的模板
        entries_reversed = entries[::-1]
        for entry in entries_reversed:
            key = (
                entry.module,
                entry.template_id
            )

            # 逻辑是：同一模块同一case，只保留一条
            if entry.template_id is not None and entry.template is not None:
                if key not in seen_keys:
                    seen_keys.add(key)
                    deduped_entries.append(entry)
                else:
                    continue
            else:
                deduped_entries.append(entry)

        deduped_entries_reversed_back = deduped_entries[::-1]
        serializable_data = []
        for entry in deduped_entries_reversed_back:
            template = entry.template
            content_tokens = template.split(' ')
            # 找到所有特殊标识符的位置
            positions = []
            for idx, token in enumerate(content_tokens):
                cleaned_tokens = extract_all_bracket_tokens(token)
                for cleaned_token in cleaned_tokens:
                    if cleaned_token in special_tokens:
                        positions.append(idx)

            entry_dict = {
                "case": entry.case,
                "module": entry.module,
                "file_name": entry.file_name,
                "file_path": entry.file_path,
                "line_content": entry.line_content,
                "template_id": entry.template_id,
                "template": entry.template,
                "param_list": entry.param_list,
                "param_list_position": positions,
                "param_pos": []
            }
            serializable_data.append(entry_dict)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(serializable_data, f, ensure_ascii=False, indent=2)
        return True

    except Exception as e:
        print(f"保存日志条目时出错: {str(e)}")
        return False


def save_enhanced_entries_to_json_dme(entries, output_path):
    try:
        deduped_entries = []
        seen_keys = set()

        for entry in entries:
            key = (
                entry.case,
                entry.file_path,
                entry.template_id,
                entry.template
            )

            # 逻辑是：同一模块同一case，只保留一条
            if entry.template_id is not None and entry.template is not None:
                if key not in seen_keys:
                    seen_keys.add(key)
                    deduped_entries.append(entry)
                else:
                    continue
            else:
                deduped_entries.append(entry)

        serializable_data = []
        for entry in deduped_entries:
            entry_dict = {
                "case": entry.case,
                "module": entry.module,
                "file_name": entry.file_name,
                "file_path": entry.file_path,
                "line_content": entry.line_content,
                "template_id": entry.template_id,
                "template": entry.template,
                "param_list": entry.param_list,
                "param_pos": entry.param_pos
            }
            serializable_data.append(entry_dict)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(serializable_data, f, ensure_ascii=False, indent=2)
        return True

    except Exception as e:
        print(f"保存日志条目时出错: {str(e)}")
        return False


def load_enhanced_entries_from_json(input_path):
    try:
        if not os.path.exists(input_path):
            print(f"文件不存在: {input_path}")
            return []

        with open(input_path, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)

        entries = []
        for item in raw_data:
            entry = EnhancedLogEntry(
                case=item.get('case', ''),
                module=item.get('module', ''),
                file_name=item.get('file_name', ''),
                file_path=item.get('file_path', ''),
                line_content=item.get('line_content', '')
            )

            entry.template_id = item.get('template_id')
            entry.template = item.get('template')
            entry.param_list = item.get('param_list')
            entry.param_pos = item.get('param_pos')

            entries.append(entry)

        return entries

    except Exception as e:
        print(f"加载日志条目时出错: {str(e)}")
        return []


def recursive_search_dme(base_dir):
    tool = OffLineTool()
    le = LogEntry
    for folder_name in os.listdir(base_dir):
        folder_path = os.path.join(base_dir, folder_name)

        if os.path.isdir(folder_path):
            # 构造result_json目录路径
            result_json_dir = os.path.join(folder_path, 'result_json')

            # 检查目录是否存在
            if os.path.exists(result_json_dir):
                # 遍历目录中的所有JSON文件
                for json_filename in os.listdir(result_json_dir):
                    if json_filename.endswith('.json'):
                        json_path = os.path.join(result_json_dir, json_filename)

                        try:
                            # 读取JSON文件
                            with open(json_path, 'r', encoding='utf-8') as f:
                                data = json.load(f)

                            # 提取需要的字段
                            case = folder_name.strip() if folder_name else ''
                            module = str(data.get('module', '')).strip()
                            file_name = str(data.get('fileName', '')).strip()
                            file_path = str(data.get('filePath', '')).strip()
                            raw_content = str(data.get('line', '')).strip()

                            # 将line_content进行日志头和日志内容分离
                            if module not in tool.format_dict:
                                continue
                            time, line_content = tool.parse.format(raw_content, module)
                            if line_content == '' or time == 0:
                                continue

                            # 添加到LogEntry
                            le.add_entry(case, module, file_name, file_path, line_content)

                        except json.JSONDecodeError as e:
                            print(f"JSON解析错误：{json_path} - {str(e)}")
                        except Exception as e:
                            print(f"处理文件时发生错误：{json_path} - {str(e)}")
            else:
                print(f"未找到result_json目录：{result_json_dir}")
    return le


def get_mae_raw_content(file_path, anomaly_lines):
    raw_content_list = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        # 将行号转换为从0开始的索引
        line_indices = [line - 1 for line in anomaly_lines]
        for index in line_indices:
            if 0 <= index < len(lines):
                raw_content_list.append(lines[index].strip())
            else:
                raw_content_list.append('')  # 行号超出范围，添加空字符串
    except FileNotFoundError:
        print(f"文件未找到：{file_path}")
        return raw_content_list
    except Exception as e:
        print(f"读取文件时发生错误：{file_path} - {str(e)}")
        return raw_content_list
    return raw_content_list


def recursive_search_mae(base_dir):
    tool = OffLineTool()
    tool.init_log_format(product_name='MAE')
    le = LogEntry
    for folder_name in os.listdir(base_dir):
        result_json_dir = os.path.join(base_dir, folder_name)
        # 检查目录是否存在
        if os.path.exists(result_json_dir):
            # 遍历目录中的所有JSON文件
            for json_filename in os.listdir(result_json_dir):
                if json_filename.endswith('.jsonl'):
                    json_path = os.path.join(result_json_dir, json_filename)

                    with open(json_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            try:
                                data = json.loads(line.strip())
                                # 提取需要的字段
                                case = str(data.get("outputPath").split('/')[-1])
                                module = str((data.get("outputPath").split('/')[-1]).split('_')[0])
                                file_name = str(data.get("outputPath").split('/')[-1])
                                file_path = '\\'.join(data.get("outputPath", '').split('/'))
                                anomaly_lines = ast.literal_eval(data.get('anomalyLines', ''))

                                # 特殊模块处理：'eo' --> 'sel'
                                if module == 'eo':
                                    module = 'sel'

                                # 将line_content进行日志头和日志内容分离
                                if module not in tool.format_dict:
                                    continue

                                log_path = os.path.join(result_json_dir, file_path)
                                raw_content_list = get_mae_raw_content(log_path, anomaly_lines)
                                if not raw_content_list:
                                    continue
                                for raw_content in raw_content_list:
                                    time, line_content = tool.parse.format(raw_content, module)
                                    if line_content == '' or time == 0:
                                        continue

                                    # 添加到LogEntry
                                    le.add_entry(case, module, file_name, log_path, line_content)

                            except json.JSONDecodeError as e:
                                print(f"JSON解析错误：{json_path} - {str(e)}")
                            except Exception as e:
                                print(f"处理文件时发生错误：{json_path} - {str(e)}")
        else:
            print(f"未找到result_json目录：{result_json_dir}")
    return le


def generate_config_from_enhanced_json(input_json_path, output_json_path):
    """
    从增强日志JSON文件生成优化后的配置JSON

    参数:
        input_json_path: 增强日志JSON文件路径
        output_json_path: 输出的配置JSON文件路径
    """
    try:
        # 读取增强日志数据
        with open(input_json_path, 'r', encoding='utf-8') as f:
            enhanced_data = json.load(f)

        # 新的配置数据结构：以template_id为主键
        config_data = {}

        # 处理每个日志条目
        for entry in enhanced_data:
            template_id = entry.get('template_id')
            param_positions = entry.get('param_pos', [])  # 参数位置列表
            template = entry.get('template')  # 新增字段
            module = entry.get('module')  # 新增字段

            # 跳过无效条目（缺少关键字段）
            if not template or not module or not param_positions:
                continue

            # 获取参数列表（确保是列表类型）
            param_list = entry.get('param_list', [])
            if not isinstance(param_list, list):
                try:
                    # 尝试将字符串表示的列表转换为实际列表
                    param_list = ast.literal_eval(param_list)
                except:
                    param_list = []

            # 初始化当前template_id的配置
            if template_id not in config_data:
                config_data[template_id] = {
                    "template": template,
                    "module": module,
                    "params": []
                }

            # 为每个参数位置创建配置项
            for pos in param_positions:
                # 确保位置是整数且在有效范围内
                if not isinstance(pos, int) or pos < 0 or pos >= len(param_list):
                    continue

                # 获取参数值（位置索引从0开始）
                param_value = param_list[pos]

                # 检查是否已存在相同位置的配置
                existing_param = next(
                    (p for p in config_data[template_id]["params"] if p['param_pos'] == str(pos)),
                    None
                )

                if existing_param:
                    # 如果已存在，追加参数值（去重）
                    if param_value not in existing_param['detect_detail']:
                        existing_param['detect_detail'].append(param_value)
                else:
                    # 创建新的参数配置项
                    new_param = {
                        "param_pos": str(pos),
                        "detect_mode": "black_list",
                        "detect_detail": [param_value]
                    }
                    config_data[template_id]["params"].append(new_param)

        # 将每个template_id的配置转换为最终输出格式
        final_config = []
        for template_id, data in config_data.items():
            final_config.append({
                "template": data["template"],
                "module": data["module"],
                "params": data["params"]
            })

        # 创建输出目录（如果不存在）
        os.makedirs(os.path.dirname(output_json_path), exist_ok=True)

        # 保存为JSON文件
        with open(output_json_path, 'w', encoding='utf-8') as jsonfile:
            json.dump(final_config, jsonfile, ensure_ascii=False, indent=4)

        print(f"优化配置已成功生成并保存至: {output_json_path}")
        return True

    except Exception as e:
        print(f"生成配置时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def extract_mask_values(template_raw, param_raw):
    # 将掩码模式中的 <XXX> 替换为对应的正则表达式
    special_tokens = {'<MAC>', '<IP>', '<MAC-HEX>', '<HEX4>', '<HEX0x>', '<NUM>', '<CMD>', '<*>'}

    # 构建正则表达式，为每个匹配部分生成唯一的组名
    pattern = template_raw
    group_count = 0
    for token in special_tokens:
        while token in pattern:
            # 每次替换时，使用不同的组名
            group_name = f"value_{group_count}"
            pattern = pattern.replace(token, rf"(?P<{group_name}>.+?)", 1)
            group_count += 1

    # 匹配原始字符串
    match = re.fullmatch(pattern, param_raw)
    if not match:
        return []
    else:
        # 提取所有匹配的值（只提取我们替换的组）
        return [match.group(f"value_{i}") for i in range(group_count)]

def generate_config_from_enhanced_json_mae(input_json_path, output_json_path):
    """
    MAE: 从增强日志JSON文件生成优化后的配置JSON

    参数:
        input_json_path: 增强日志JSON文件路径
        output_json_path: 输出的配置JSON文件路径
    """
    try:
        # 读取增强日志数据
        with open(input_json_path, 'r', encoding='utf-8') as f:
            enhanced_data = json.load(f)

        # 新的配置数据结构：以template_id为主键
        config_data = {}

        # 处理每个日志条目
        for entry in enhanced_data:
            template_id = entry.get('template_id')
            param_positions = entry.get('param_pos', [])  # 参数位置列表
            template = entry.get('template')  # 新增字段
            module = entry.get('module')  # 新增字段
            line_content = entry.get('line_content') # 新增字段

            # 跳过无效条目（缺少关键字段）
            if not line_content or not template or not module or not param_positions:
                continue

            # 初始化当前template_id的配置
            if template_id not in config_data:
                config_data[template_id] = {
                    "template": template,
                    "module": module,
                    "params": []
                }

            # 为每个参数位置创建配置项
            for pos in param_positions:
                # 确保位置是整数且在有效范围内
                if not isinstance(pos, int) or pos < 0 or pos >= len(template.split(' ')):
                    continue

                # 获取参数值（位置索引从0开始, 索引为全局索引，并非参数列表索引）
                split_result = re.split(r'([:=,\[\]\(\)])', line_content)
                split_content = line_content.strip()
                if len(split_result) > 1:
                    split_content = ' '.join(split_result)

                split_content = re.sub(",", " ", split_content)
                split_result = split_content.split(' ')
                filtered_content = [item for item in split_result if item]

                param_raw = filtered_content[pos]
                template_raw = template.split(' ')[pos]

                param_value_list = extract_mask_values(template_raw, param_raw)
                # 当前默认同一个位置仅一个参数；多参数情况param_value_list仍支持，需修改代码逻辑，例如'<NUM>.<NUM>' '<NUM>/<NUM>'
                param_value = param_value_list[0] if param_value_list else ''

                if not param_value:
                    continue

                # 检查是否已存在相同位置的配置
                existing_param = next(
                    (p for p in config_data[template_id]["params"] if p['param_pos'] == str(pos)),
                    None
                )

                if existing_param:
                    # 如果已存在，追加参数值（去重）
                    if param_value not in existing_param['detect_detail']:
                        existing_param['detect_detail'].append(param_value)
                else:
                    # 创建新的参数配置项
                    new_param = {
                        "param_pos": str(pos),
                        "detect_mode": "black_list",
                        "detect_detail": [param_value]
                    }
                    config_data[template_id]["params"].append(new_param)

        # 将每个template_id的配置转换为最终输出格式
        final_config = []
        for template_id, data in config_data.items():
            final_config.append({
                "template": data["template"],
                "module": data["module"],
                "params": data["params"]
            })

        # 创建输出目录（如果不存在）
        os.makedirs(os.path.dirname(output_json_path), exist_ok=True)

        # 保存为JSON文件
        with open(output_json_path, 'w', encoding='utf-8') as jsonfile:
            json.dump(final_config, jsonfile, ensure_ascii=False, indent=4)

        print(f"优化配置已成功生成并保存至: {output_json_path}")
        return True

    except Exception as e:
        print(f"生成配置时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        return False