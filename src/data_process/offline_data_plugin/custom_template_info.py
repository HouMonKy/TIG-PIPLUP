import json
from typing import List, Dict

from src.common.atomic_op import abs_path
from src.data_process.log_context import LogContext


class CustomTemplateInfo:
    def __init__(self):
        self.context = LogContext()

    def add_custom_template(self, masked_info: List[str], module: str) -> Dict[str, int]:
        drain = self.context.get_drain(module)
        result = {}
        for info in masked_info:
            template_info = drain.add_log_message(info)
            result[info] = template_info['cluster_id']
        drain.save_state("saving")
        return result

    def update_param_config(self, path: str):
        path = abs_path(path)
        with open(path, "r") as f:
            param_list = json.load(f)
            mask_contents = {}
            for param in param_list:
                module = param['module']
                if module in mask_contents:
                    mask_contents[module].append(param['template'])
                else:
                    mask_contents[module] = [param['template']]
            for module, params in mask_contents.items():
                template_map = self.add_custom_template(params, module)
                for param in param_list:
                    if param['template'] in template_map:
                        param['template_id'] = template_map[param['template']]
            json.dump(param_list, open(path, "w"))
            
if __name__ == "__main__":
    CustomTemplateInfo().update_param_config("src/anomaly_detection/parameter_anomaly/configuration/config_build_dme.json")