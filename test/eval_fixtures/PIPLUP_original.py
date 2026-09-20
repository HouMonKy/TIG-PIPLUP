"""模拟【原版】PIPLUP：与真实 PIPLUP_original 相同的模块级引用方式
（from .preprocessing import preprocess）——引用的是当前版模块，
pipeline 必须改绑才能让原版解析用上原版预处理/后处理。"""
from .preprocessing import preprocess
from .postprocessing import correct_single_template


class Logcluster:
    def __init__(self, logTemplate='', logIDL=None):
        self.logTemplate = logTemplate
        self.templateList = [correct_single_template(' '.join(logTemplate))]
        self.hit_time = 1


class LogParser:
    def __init__(self, log_format='<Content>', module_name='test', **kwargs):
        self.logCluL = []

    def parse_list(self, log_list):
        """每行模板 = 预处理产物 + 后处理产物（可验证改绑是否生效）。"""
        out = []
        for log in log_list:
            tmpl = f"{preprocess(log)}|{correct_single_template(log)}"
            self.logCluL.append(Logcluster(logTemplate=[tmpl]))
            out.append(tmpl)
        return out
