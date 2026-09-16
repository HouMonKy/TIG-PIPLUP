import json

from src.common.atomic_op import abs_path


class TemplateConfig:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.persistence_time_minutes = 5 ## 持久化周期-分钟计数
        self.learning_switch = 'on'      ## 学习开关是否开启
        self.learning_mode = "new"        ## 学习模式，new 重新学习 continue 继续学习
        self.learning_time_hours = 24     ## 学习持续时-小时计数
        self.persistence_duration = 86400000 ## 日志数据留存时间-毫秒计数
        self.print_invalid_log = False

    @staticmethod
    def load(path):
        config = TemplateConfig("127.0.0.1", "5001")
        with open(abs_path(path), 'r') as file:
            config_map = json.load(file)
            config.host = config_map['host']
            config.port = config_map['port']
            config.persistence_time_minutes = config_map['persistence_time_minutes']
            config.learning_switch = config_map['learning_switch']
            config.learning_mode = config_map['learning_mode']
            config.learning_time_hours = config_map['learning_time_hours']
            config.persistence_duration = config_map['persistence_duration']
            config.print_invalid_log = bool(config_map['print_invalid_log'])
        return config

    def is_learn_config_valid(self):
        return self.learning_switch in ['on', 'off'] and self.learning_mode in ['new', 'goon'] and (10 <= self.learning_time_hours <= 7 * 24)

    def dict(self):
        return {
            'host': self.host,
            'port': self.port,
            'persistence_time_minutes': self.persistence_time_minutes,
            'learning_switch': self.learning_switch,
            'learning_mode': self.learning_mode,
            'learning_time_hours': self.learning_time_hours,
            'persistence_duration': self.persistence_duration
        }