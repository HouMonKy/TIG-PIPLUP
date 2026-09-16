import copy
import json
import os
import threading
import time
from typing import List

from src.anomaly_detection.parameter_anomaly.configuration.load_param_config import load_param_config, ParamInfo
from src.common.constant import TEMPLATE_CONFIG_PATH, TEMPLATE_MINER_CONFIG_PATH, LEARNING_TIME_FILE, WHITE_LIST_PATH, \
    DETECT_INTERVAL, CONFIG_STATE_FILE, DME_TEMPLATE_FORMAT_PATH, MAE_TEMPLATE_FORMAT_PATH
from src.common.global_config import GlobalConfig
from src.data_process.data_basic_structs.basic_structs import LRUCache
from src.data_process.template_extractor.log_formatter_parse import LogFormatParser
from src.data_process.template_extractor.log_template_repository import LogTemplateRepository
from src.data_process.template_extractor.parsing_algorithm import TemplateMiner, FilePersistence, TemplateMinerConfig
from src.data_process.template_extractor.parsing_algorithm.template_config import TemplateConfig
from src.data_process.template_extractor.parsing_algorithm.PIPLUP import LogParser

class LogContext:
    template_miner_map: dict  # drain树提取的容器

    log_repository: LogTemplateRepository  # 内存数据存储

    white_list: dict  # 白名单列表

    template_config: TemplateConfig  # 稳态学习开关，学习时长，内存数据存储时长，在线服务器的ip和端口号配置信息

    log_format: dict  # 日志格式配置

    first_learning: bool  # 是否是首次开始学习

    lru_cache: dict  # 模板缓存，不同的module使用不同的缓存，懒加载，只有module的日志初次出现时初始化对应的缓存,原lru_cache

    learning_time: float  # 稳态学习的开始时间

    detect_config_time: int  # template_config可以在运行期间修改，记录上次的检测template_config内容的时间

    saving_time: float  # template_miner需要定时持久化模板信息，记录上次存储时间

    white_list_update_time: float  # 白名单white_list需要定时持久化，记录上次持久化的时间

    parser: LogFormatParser

    parser_piplup: LogParser

    template_param: dict

    def __init__(self, product_name=None, module_name=None):
        self.detect_config_time = 0
        self.saving_time = time.time()
        self.white_list_update_time = time.time()
        self.template_miner_map = {}
        self.lru_cache = {}
        self.template_config = TemplateConfig.load(TEMPLATE_CONFIG_PATH)
        self.log_repository = LogTemplateRepository(self.template_config.persistence_duration)
        self.init_log_format(product_name)
        self.init_learning_time()
        self.init_white_list()
        self.template_param = load_param_config()
        module_name = module_name if module_name is not None else product_name
        self.parser_piplup = LogParser(log_format='<Content>', module_name=module_name, use_cache=True)

    def init_learning_time(self):
        self.first_learning = False
        if not self.is_switch_on():
            return
        if os.path.exists(abs_path(LEARNING_TIME_FILE)):
            with open(abs_path(LEARNING_TIME_FILE), 'r') as file:
                learning_time = json.load(file)
                time_struct = time.strptime(learning_time.get('learning_time', 0), '%Y-%m-%d %H:%M:%S')
                start_time = time.mktime(time_struct)
                self.learning_time = start_time
        else:
            self.first_learning = True
            with open(abs_path(LEARNING_TIME_FILE), 'w') as file:
                learning_time_dict = {}
                start_time = time.time()
                local_time = time.localtime(start_time)
                start_time = time.strftime('%Y-%m-%d %H:%M:%S', local_time)
                learning_time_dict['learning_time'] = start_time
                json.dump(learning_time_dict, file, indent=4)

    def init_white_list(self):
        if self.first_learning and self.template_config.learning_mode == 'NEW':
            self.white_list = {}
            self.first_learning = False
            return
        # 继续学习的场景读取文件中的白名单
        abs_white_list_path = abs_path(WHITE_LIST_PATH)
        if os.path.exists(abs_white_list_path):
            with open(abs_white_list_path, 'r') as file:
                content = file.read().strip()
                if not content:
                    white_list = {}
                else:
                    white_list = json.loads(content, object_hook=lambda d: {
                        key: list_to_set(value) if isinstance(value, list) else value for key, value in d.items()})
        else:
            white_list = {}
        self.white_list = white_list

    def init_log_format(self, product_name=None):
        """
        从 config/format.json中加载日志的格式信息
        """
        if product_name == "MAE":
            path = abs_path(MAE_TEMPLATE_FORMAT_PATH)
        else:
            path = abs_path(DME_TEMPLATE_FORMAT_PATH)
        try:
            with open(path, 'r') as file:
                format_dict = json.load(file)
                self.log_format = format_dict
                self.parser = LogFormatParser(self.log_format)
        except FileNotFoundError:
            print("文件未找到")
        except json.JSONDecodeError:
            print("文件不是有效的 JSON 格式")
        except Exception as e:
            print(f"发生了错误: {e}")

    def get_drain(self, module) -> TemplateMiner:
        """
        获取入参module对应的drain树信息
        @param module 日志类型
        @return Template_miner 实际是一个drain树容器
        """
        if module in self.template_miner_map:
            return self.template_miner_map[module]
        root_dir = GlobalConfig.get_project_root()
        if not os.path.exists(os.path.join(root_dir, 'config', 'persistence')):
            os.mkdir(os.path.join(root_dir, 'config', 'persistence'))
        file_name = os.path.join(root_dir, 'config', 'persistence', f"{module}_extractor_state.bin")
        persistence = FilePersistence(file_name)
        template_miner_config = TemplateMinerConfig()
        template_miner_config.load(abs_path(TEMPLATE_MINER_CONFIG_PATH))
        template_miner_config.profiling_enabled = False
        template_miner = TemplateMiner(persistence, template_miner_config)
        self.template_miner_map[module] = template_miner
        return template_miner

    def get_white_list(self, module: str) -> List[int]:
        if module in self.white_list:
            white_list = self.white_list[module]
        else:
            white_list = []
        result = []
        for template_id in white_list:
            result.append(int(template_id))
        return result

    def get_lru_cache(self, module: str) -> LRUCache:
        """
        获取指定模块的模板缓存，如果已存在则直接返回，不存在创建一个大小为10000的新缓存
        @param module 日志模块
        @return 模板缓存
        """
        if module in self.lru_cache:
            return self.lru_cache[module]
        lru_cache = LRUCache(10000)
        self.lru_cache[module] = lru_cache
        return lru_cache

    def reload_template_config_and_learn_status(self):
        """
        定时加载学习配置，并判断是否应该结束学习
        """
        cur_time: float = time.time()
        if time.time() - self.detect_config_time > DETECT_INTERVAL:
            # 固定间隔后重新加载配置数据
            self.reload_template_config(cur_time)
            # 判断学习是否结束，如果结束更新config.json文件和内存
            self.try_change_learn_status(cur_time)

    def try_change_learn_status(self, cur_time):
        if not self.is_switch_on():
            return
        learn_duration = self.template_config.learning_time_hours * 60 * 60
        if cur_time - self.learning_time > learn_duration:
            self.template_config.learning_switch = 'off'
            config_dic = self.template_config.dict()
            with open('config/config.json', 'w') as file:
                json.dump(config_dic, file, indent=4)
            if os.path.exists(LEARNING_TIME_FILE):
                os.remove(LEARNING_TIME_FILE)

    def reload_template_config(self, cur_time):
        local_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cur_time))
        config_state_file = CONFIG_STATE_FILE
        if os.path.exists(config_state_file):
            with open(config_state_file, 'r') as file:
                config_state_dict = json.load(file)
        else:
            config_state_dict = {}
        if not self.template_config.is_learn_config_valid():
            config_state_dict['state'] = 'Configuration Parameter Error'
            config_state_dict['last_modify_time'] = local_time
        else:
            config_state_dict['state'] = 'Success'
            config_state_dict['last_modify_time'] = local_time
        with open(config_state_file, 'w') as file:
            json.dump(config_state_dict, file, indent=4)
        self.detect_config_time = cur_time

    def update_white_list(self, parse_results, fair_lock):
        """
        更新内存白名单信息，如果距离上次持久化时间超过self.template_config.persistence_time_minutes分钟则持久化
        @param parse_results 本次模板抽取的结果列表
        """
        if self.is_switch_on():
            white_list = self.white_list
            for parse_result in parse_results:
                white_list.setdefault(parse_result['module'], set()).add(str(parse_result['template_id']))
            self.white_list = white_list
            cur_time: float = time.time()
            if (cur_time - self.white_list_update_time > self.template_config.
                    persistence_time_minutes * 60):
                white_list = copy.deepcopy(white_list)
                thread = threading.Thread(target=write_to_file, args=(white_list, fair_lock))
                thread.start()
                self.white_list_update_time = cur_time

    def persistence_drain(self):
        cur_time: float = time.time()
        if cur_time - self.saving_time > self.template_config.persistence_time_minutes * 60:
            for template_miner in self.template_miner_map.values():
                template_miner.save_state('saving')
            self.saving_time = cur_time

    def persistence_piplup(self):
        cur_time: float = time.time()
        if cur_time - self.saving_time > self.template_config.persistence_time_minutes * 60:
            self.parser_piplup.save_cache()
            self.saving_time = cur_time

    def is_switch_on(self):
        return self.template_config.learning_switch == 'on'

    def get_format_parse(self) -> LogFormatParser:
        return self.parser

    def get_template_param(self):
        return self.template_param


def abs_path(path):
    config = GlobalConfig()
    root_dir = config.get_project_root()
    return os.path.join(root_dir, path)


def list_to_set(obj):
    if isinstance(obj, list):
        return set(obj)
    return obj


def write_to_file(white_list, fair_lock):
    with fair_lock:
        with open(abs_path(WHITE_LIST_PATH), 'w') as file:
            json.dump(white_list, file, default=set_to_list, indent=4)


def set_to_list(obj):
    if isinstance(obj, set):
        return list(obj)
    raise TypeError(f"Type {type(obj)} not serializable")
