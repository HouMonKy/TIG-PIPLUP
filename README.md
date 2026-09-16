# TIG_LogAnalysis  
  
TIG205技术项目代码仓。  
  
### 1.1 数据规范  
  
数据样本分为日志数据Data和数据标签Tag。  
  
● Tag：标签文件，包含对应日志名和对应路径，异常行标注，以及日志格式。  
  
● Data：原始日志文件，每一行代表一条日志。  
  
● Data样例：  
  
```auto  
[2022-08-02 05:07:24.902][15000033e0000][INFO][4146349][dus_server_entity.cc,577,void dus::DuSServerEntity::EndRemapTaskSession(int32_t)][INDEX][DUS]End session PoolId=0 Instid=11 Level=0  [2022-08-02 05:07:24.968][15000011d0000][WARN][4146336][1787,DSGIO_overTimePrint][T:364740048ad96a]log_cache:DSGIO: apptype(0) objid(0) dsg kv over 2.0s elapsedTime:2187494559 ns, kvnum(8) ret(0).  [2022-08-02 05:07:25.335][15000033e0000][INFO][4146352][dus_server_entity.cc,577,void dus::DuSServerEntity::EndRemapTaskSession(int32_t)][INDEX][DUS]End session PoolId=0 Instid=8 Level=0  ```  
  
● Tag格式：  
  
```auto  
"fileName":string,  // 本标签文件对应的是哪个日志文件  "filePath":string,  // 日志文件的相对/绝对路径  "line":List<int>    // 给出异常日志行数；DME的数据这里给出的是具体行内容，需要反查  "format": string    // 正则，如何提取content  ```  
  
● Tag样例：  
  
```auto  
"fileName":"log-dsware_java_11.log.2022-07-06_13-59-02.097",  "filePath":"E:\\perf_risk\\data\\01_01_03\\ST60202109010063\\decompress\\log\\log\\dsware\\eds\\bak\\log-dsware_java_11.log.2022-07-06_13-59-02.097",  "line":[  "[2022-07-06 13:53:35.884][1500001880000][INFO][3720950][cm_client_interface_impl.cpp,2158,CM_getLSViewAsyn]DPMMAlloc memory fail, client_id=5, dsName=qos_center_F_6.",  "[2022-07-06 13:53:35.884][1500001880000][INFO][3720950][cm_client_interface_impl.cpp,2158,CM_getLSViewAsyn]DPMMAlloc memory fail, client_id=5, dsName=qos_center_F_6."  
]   "format": "<Id>,<Time>,<Level>,<Event_code>,<Asserted_or_deasserted>,<Entity_type>:<Content>"  # 正则，如何提取content和time格式信息  
```  

### 2. 代码仓结构

```auto
TIG_LogAnalaysis[master]
	| -- bin                             // python环境配置等启动类脚本
	| -- config                          // 服务、日志格式、持久化的配置信息等
	| -- dataset                         // 数据集
	|	| -- CN                          // CN 数据集
	|	| -- DME                         // DME数据集
	|	| -- raw_data                    // 参数异常配置构建临时数据，待调整
	|	└─── result                      // 参数异常配置构建临时数据，待调整
	| -- design_doc                      // 设计文档，数据集说明文档
	| -- src
	|	| -- data_process                // 数据处理，包含基础数据结构，离线数据对接插件，模板提取和处理过程
	|	| -- anomaly_detection           // 检测算法
	|	|	| -- anomaly_base            // 基类和基础数据结构
	|	|	| -- parameter_anomaly       // 参数检测，参数黑白名单配置和检测算法
	|	|	| -- quantitive_anomaly      // 数量检测
	|	|	| -- semantic_anomaly        // 语义检测： cpu + npu方案
	|	|	└─── anomaly_detection.py    // 检测主入口
	|	└─── evalutation                 // 数据评估：指标计算
	| -- test                            // 测试方法
	| -- main.py                         // 在线app入口，拉起服务，提供模板提取和异常检测功能
	| -- README.md
	└─── requirement.txt

TIG_LogAnalaysis[TIG_tech]
	| -- dataset                         // 数据集
	| -- tech_dme                        // 存放dme历史代码和临时代码；
	| -- tech_mae                        // 存放mae历史代码和临时代码；
	└─── tech_evo                        // 各类技术预研，演进和demo代码；
```
- 数据集较大，服务器归档 /opt/TIG_LogAnalysis
- 后续调整，需保证服务器与代码同目录结构


### 3. 接口测试  
  
- 拉起服务  
```  
python main.py
```
- 正确显示参考：
```
 * Serving Flask app 'main'
 * Debug mode: on
WARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.
 * Running on http://127.0.0.1:5001
Press CTRL+C to quit
 * Restarting with stat
 * Debugger is active!
 * Debugger PIN: 370-979-238
```
- 接口测试
```
# 拉起服务后执行
python test_api.py
```
- 正确显示参考：
```
请求成功！响应内容：
{'parsed_result': [{'log_id': '148492c9-5418-43cb-8367-04c08e955e16', 'module': 'eds', 'template': 'io detect send res error code : <NUM> request id : <MAC-HEX> cmd type ( <NUM> ) start time : <NUM> end time : <NUM>.fc wait time : <NUM> iops ok : <NUM> not ok : 0m sendtime : <NUM> aio block time : <NUM> <NUM>.', 'template_id': 1}, {'log_id': '148492c9-5418-43cb-8367-04c08e955e17', 'module': 'eds', 'template': 'Get ns async failed ret ( <NUM> ) .', 'template_id': 2}, {'log_id': '148492c9-5418-43cb-8367-04c08e955e18', 'module': 'message', 'template': 'Jun <NUM> <NUM> : <NUM> : <NUM> allinonefm0 su [ <NUM> ] : ( to dbadmin ) root on none"', 'template_id': 1}, {'log_id': '148492c9-5418-43cb-8367-04c08e955e19', 'module': 'message', 'template': 'Jun <NUM> <NUM> : <NUM> : <NUM> allinonefm0 su [ <NUM> ] : ( to dbadmin ) root on none"', 'template_id': 1}, {'log_id': '148492c9-5418-43cb-8367-04c08e955e20', 'module': 'message', 'template': 'Jun <NUM> <NUM> : <NUM> : <NUM> allinonefm0 su [ <NUM> ] : ( to dbadmin ) root on none"', 'template_id': 1}]}
```

### 4. 离线测试

- DME数据测试：根据实际数据数据调整输入路径
```
python offline_dme_detection_tool.py
```

- MAE数据测试：根据实际数据数据调整输入路径
```
python offline_mae_detection_tool.py
```