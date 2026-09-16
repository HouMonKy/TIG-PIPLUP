import json
import os
import uuid
from typing import List
import logging

from src.common.atomic_op import abs_path


class BusinessLog:
    logId: str
    source: str
    rawLog: str
    module: str

    def __init__(self, logId: str, source: str, rawLog: str, module: str) -> None:
        self.logId = logId
        self.source = source
        self.rawLog = rawLog
        self.module = module
    def to_dict(self) -> dict:
        return {"logId": self.logId, "source": self.source, "rawLog": self.rawLog, "module": self.module}


# 配置日志，替代 print
logger = logging.getLogger(__name__)

class LogFileConverter:

    """
    读入日志信息，并将日志信息转换为log_parse接口的入参信息
    @params: source source信息需要手动指定
    @params: module 日志类型信息也需要指定
    @return List[dict] 一个日志序列
    """
    @staticmethod
    def _parse_file_to_logs(file_path: str, source: str, module: str) -> List[dict]:
        """
        内部核心方法：读取单个文件并转换为结构化字典。
        """
        parsed_results = []

        # 预先提取文件名（如确有业务需要，可直接传入 BusinessLog）
        # file_name = "".join(file_path.split("temp_")[1:])

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                while True:
                    try:
                        line = f.readline()
                        if not line:
                            break
                        if not line.strip():  # 过滤空行，可根据实际日志规范决定是否保留
                            continue
                        # 构造业务日志对象并转为字典
                        log_item = BusinessLog(
                            logId=uuid.uuid4().hex,
                            source=source,
                            rawLog=line,
                            module=module
                        ).to_dict()
                        parsed_results.append(log_item)
                    except UnicodeDecodeError as ue:
                        logger.error(f"文件编码解析失败（非UTF-8）| 路径: {file_path} | 错误: {ue}")
                    except Exception as ce:
                        logger.error(f"解析日志行失败 | 文件: {file_path} | 错误: {ce}")
        except OSError as fe:
            logger.error(f"无法打开或读取文件 | 路径: {file_path} | 错误: {fe}")

        return parsed_results


    @staticmethod
    def read_and_convert_directory(directory: str, source: str, module: str) -> List[dict]:
        """
        对应原 read_and_convert_file：输入为文件夹路径，递归遍历内部所有文件。
        """
        result = []
        target_dir = abs_path(directory)

        if not os.path.exists(target_dir):
            logger.warning(f"目标目录不存在: {target_dir}")
            return result

        if not os.path.isdir(target_dir):
            logger.warning(f"传入路径并非标准目录: {target_dir}")
            return result

        # 递归遍历目录
        for root, _, files in os.walk(target_dir):
            for entry in files:
                file_path = os.path.join(root, entry)
                # 复用单文件解析逻辑
                result.extend(LogFileConverter._parse_file_to_logs(file_path, source, module))

        return result

    @staticmethod
    def read_and_convert_single_file(file_path: str, source: str, module: str) -> List[dict]:
        """
        对应原 read_and_convert_file_dme：输入严格为单文件路径。
        """
        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            logger.warning(f"目标文件不存在或非有效文件: {file_path}")
            return []

        return LogFileConverter._parse_file_to_logs(file_path, source, module)

if __name__ == "__main__":
    LogFileConverter.read_and_convert_directory(directory="../../data/CN/normal/eor", source="eor", module="eor")
