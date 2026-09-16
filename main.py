import json
import time

from flask import Flask, request, jsonify, Response

from src.anomaly_detection.anomaly_detection import AnomalyDetection
from src.common.distributed_lock import FairLock
from src.data_process.log_context import LogContext
from src.data_process.template_extractor.log_template_parsing_process import parse_logs
from src.data_process.template_extractor.log_template_parsing_process_piplup import parse_logs_piplup_adapter
from src.data_process.template_extractor.log_template_repository import LogTemplateRepository

# 创建一个 Flask 应用
app = Flask(__name__)
app.config["context"] = LogContext("DME")
fair_lock = FairLock()


# @app.before_first_request


@app.route('/hello', methods=['GET'])
def hello():
    return jsonify(message="Hello, World!")


@app.route('/lognet_parse', methods=['POST'])
def lognet_parse():
    begin = int(time.time()*1000)
    context: LogContext = app.config["context"]

    # 从请求中获取 JSON 数据
    data = request.get_json()
    if data is None:
        return jsonify({"error": "No JSON data provided"}), 400  # 如果没有 JSON 数据，返回错误
    # 逐行解析日志信息
    parse_results = parse_logs_piplup_adapter(data.get('logInfo'), context)
    # 更新白名单
    context.update_white_list(parse_results, fair_lock)
    # 持久化模板信息
    context.persistence_piplup()
    end = int(time.time()*1000)
    print(f"lognet_parse cost: {end - begin}ms")

    return jsonify({"parsed_result": []})


@app.route('/abnormal-check', methods=['POST'])
def abnormal_check():
    context: LogContext = app.config["context"]
    log_repository: LogTemplateRepository = context.log_repository
    request_data = request.get_json()
    # 请求体中的数据只包含需要检测的最后一个时间窗的数据
    start_time = request_data['startTime']
    end_time = request_data['endTime']
    window_size = request_data['windowSize']
    module = request_data['module']
    drain = context.get_drain(module)
    if window_size is None:
        window_size = 180000
    source = request_data['source']
    result = AnomalyDetection.get_detector(mode="all", product="DME").anomaly_detection(source=source,
                                                                            module=module,
                                                                            start_time=start_time,
                                                                            end_time=end_time,
                                                                            window_size=window_size,
                                                                            repository=log_repository,
                                                                            template_cache=drain,
                                                                            white_list=context.get_white_list(module))
    # DME 返回 (data, event, reports)，MAE 返回 (data, event)，统一取前两个
    data, event = result[0], result[1]
    response = {"code": 200, "message": "OK", "data": data, "event": event}
    return jsonify(response)

@app.route('/anomaly-check-all', methods=['POST'])
def anomaly_check_all():
    context: LogContext = app.config["context"]
    log_repository: LogTemplateRepository = context.log_repository
    request_data = request.get_json()
    start_time = request_data['startTime']
    end_time = request_data['endTime']
    window_size = request_data['windowSize']
    if window_size is None:
        window_size = 180000
    result = AnomalyDetection.get_detector(mode="all", product="DME").all_detect(start_time=start_time, end_time=end_time, window_size=window_size, repository=log_repository, context=context)
    response = {"code": 200, "message": "OK", "data": result}
    return jsonify(response)



# 启动 Flask 服务
if __name__ == '__main__':
    app.run(threaded=False,debug=False, host=app.config["context"].template_config.host,
            port=app.config["context"].template_config.port)
