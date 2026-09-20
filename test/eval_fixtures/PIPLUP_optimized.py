"""模拟【优化版】PIPLUP：多一个 token_structure 引用（原版没有）。"""
from .preprocessing import preprocess
from .postprocessing import correct_single_template
from . import token_structure_stub  # 优化版独有的模块引用


class Logcluster:
    def __init__(self, logTemplate='', logIDL=None):
        self.logTemplate = logTemplate
        self.templateList = [correct_single_template(' '.join(logTemplate))]
        self.hit_time = 1


class LogParser:
    def __init__(self, log_format='<Content>', module_name='test', **kwargs):
        self.logCluL = []
        self.used_token_structure = token_structure_stub.MARK

    def parse_list(self, log_list):
        out = []
        for log in log_list:
            tmpl = f"{preprocess(log)}|{correct_single_template(log)}"
            self.logCluL.append(Logcluster(logTemplate=[tmpl]))
            out.append(tmpl)
        return out
