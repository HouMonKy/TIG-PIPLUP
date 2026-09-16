# -*- coding: utf-8 -*-
# 白名单文件路径
WHITE_LIST_PATH = 'config/white_list_a4.json'

# 学习时间文件路径
LEARNING_TIME_FILE = 'config/learning_time.json'

# 检测间隔（秒）
DETECT_INTERVAL = 5

# 默认模板配置文件路径
TEMPLATE_CONFIG_PATH = 'config/config.json'

DME_TEMPLATE_FORMAT_PATH = 'config/format_chaos.json'

MAE_TEMPLATE_FORMAT_PATH = 'config/format_mae.json'

# 日志模板仓库持久化时间（分钟）
PERSISTENCE_DURATION = 10

# 默认学习时间（小时）
DEFAULT_LEARNING_HOURS = 24

# 默认持久化时间（分钟）
DEFAULT_PERSISTENCE_TIME = 10

# 默认主机地址
DEFAULT_HOST = '0.0.0.0'

# 默认端口号
DEFAULT_PORT = 5000

# 模板缓存大小
TEMPLATE_CACHE_SIZE = 10000

# 白名单更新时间间隔（分钟）
WHITE_LIST_UPDATE_INTERVAL = 10

# 模板持久化时间间隔（分钟）
TEMPLATE_PERSISTENCE_INTERVAL = 10

# 模板挖掘状态保存文件路径
TEMPLATE_MINER_STATE_PATH = 'config/persistence/{module}_extractor_state.bin'

TEMPLATE_MINER_CONFIG_PATH = "config/extractor.ini"

CONFIG_STATE_FILE = "config/config_state.json"

MEM_DATA_DURATION_TIME = 24 * 60 * 60 * 1000

FILE_PERSISTENCE_FILE = "extractor_state.bin"