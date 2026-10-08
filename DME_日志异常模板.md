# DME 日志异常模板

## 语义类

### Scan kvm: traverse time out(<\*>), storeID(<\*>) num(<\*>)

``
Scan kvm: traverse time out(54 ms), storeID(70032) num(269).
``

### PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:(<\*>)

``
PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:-1
``

### PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapUidToUnameInner failed for uId (<\*>)

``
PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapUidToUnameInner failed for uId 3004
``

### PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapGidToGnameInner failed for gId (<\*>)

``
PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapGidToGnameInner failed for gId 2000
``

### FS:(<\*>)Uuid length is invalid(len <\*>)

``
FS:(0:4c70)Uuid length is invalid(len 27).
``

### FS:(<\*>)check uuid para fail ret(<\*>)

``
FS:(0:4c70)check uuid para fail ret(-2147438954).
``

### FS:(<\*>)Ret is<\*>, decode uuid failed

``
FS:(0:4c70)Ret is -2147438954, decode uuid failed.
``

### Invalid mapped object (ID <\*>) for object type (ID <\*>), object (ID <\*>), the max mapped ID is (<\*>)

``
Invalid mapped object (ID 4294967294) for object type (ID 33), object (ID 4614219297510899712), the max mapped ID is (1023).
``

### FS:(<\*>)gfs modify mmtime check func is NULL

``
FS:(0:4c70)gfs modify mmtime check func is NULL
``

### Kv parameter check error. val(not null) val_len(<\*>)

``
Kv parameter check error. val(not null) val_len(0).
``

### Db_name(<\*>) check parameter error, ret=<\*>

``
Db_name(                ADM_ADM_ARRAY_DB) check parameter error, ret=-2.
``

### ADM_JAGUARDB_EVENT: get into db <\*> not success, ret:<\*>

``
ADM_JAGUARDB_EVENT: get into db ADM_ADM_ARRAY_DB not success, ret:-2.
``

### print: <\*> messages suppressed.dopCountData,line=<\*>

``
print: 1409 messages suppressed.dopCountData,line=582.
``

### Error type or threadId, count type : <\*>, threadId  <\*>

``
Error type or threadId, count type : 0, threadId  154 .
``

### (post-ddp)Memcpy keyBug memcpy failed, ret(<\*>)

``
(post-ddp)Memcpy keyBug memcpy failed, ret(162).
``

### Invalid mapped object (ID <\*>) for object type (ID <\*>), object (ID <\*>), the max mapped ID is (<\*>)

``
Invalid mapped object (ID 4294967294) for object type (ID 33), object (ID 4614219297510899712), the max mapped ID is (1023).
``

### idx(<\*>): val_type(<\*>) is invalid

``
idx(38): val_type(0) is invalid.
``

### Parse_and_trans_data_block failed, ret is <\*>

``
Parse_and_trans_data_block failed, ret is -9,822,4.
``

### Optable compress error, ErrCode:(<\*>), Line:(<\*>), ModuleID:(<\*>)

``
Optable compress error, ErrCode:(-9), Line:(822), ModuleID:(4).
``

### Get key for check timeout(<\*> ms), lpID(0) ret(<\*>)

``
Get key for check timeout(56 ms), lpID(0) ret(0x0)
``

### Search tracepoint(<\*> ) failed, ret(<\*> )

``
Search tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392) failed, ret(3).
``

## 参数类

### Xnet send msg(<\*>(mid:<\*>) -- <\*>(mid:<\*>), time use=<\*>(us), SNO=<\*>, reqSNO=<\*>, msg type=<\*>, plane=<\*>, xnetRet=<\*>

``
Xnet send msg(0x25e0000000a0001(mid:606) -- 0x25e000000120000(mid:606), time use=10772(us), SNO=2283037522451277885, reqSNO=0, msg type=1, plane=1, xnetRet=0.
``

参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| src addr | Xnet发送端地址 | Hex ID | 依赖节点地址分配，通常为64bit/128bit地址 |
| src mid | 发送端模块ID | int | 正整数 |
| dst addr | Xnet接收端地址 | Hex ID | 依赖节点地址分配 |
| dst mid | 接收端模块ID | int | 正整数 |
| time use | 消息处理耗时 | int(us) | 通常 <100000us（100ms），需业务校准 |
| SNO | 请求序列号 | uint64 | >=0，单调递增 |
| reqSNO | 请求关联序列号 | uint64 | >=0 |
| msg type | 消息类型 | enum/int | 通常为有限枚举值 |
| plane | 网络平面编号 | enum/int | 0~若干业务平面 |
| xnetRet | Xnet返回码 | int/enum | 0表示成功，非0异常 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| time use | 0 ~ 100000 us |
| xnetRet | =0 |
| msg type | 已知枚举集合 |
| plane | 0 ~ 3 |
| SNO | >=0 且连续递增 |

### PROTOCOL:MOD_NAS:Invalid(<\*>) delay start(<\*>)-end(<\*>)

``
PROTOCOL:MOD_NAS:Invalid(764) delay start(0)-end(417074873669340).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| Invalid code | NAS模块异常码 | int | 依赖NAS协议定义 |
| delay start | 延迟开始时间/计数 | int | >=0 |
| delay end | 延迟结束时间/时间戳 | int64 | >=start |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| Invalid code | 正常日志不应出现该模板，建议黑名单检测 |
| delay start | >=0 |
| delay end | >=delay start |

### Update dpId(<\*>) taskType(<\*>) memDb startFlag(<\*>) failFlag(<\*>) stopFlag(<\*>) finishFlag(<\*>) diskId(<\*>) ret(<\*>)

``
Update dpId(<*>) taskType(<*>) memDb startFlag(<*>) failFlag(<*>) stopFlag(<*>) finishFlag(<*>) diskId(<*>) ret(<*>).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| dpId | 数据处理节点ID | int/hex | >=0 |
| taskType | 任务类型 | enum | 依赖任务定义 |
| memDb startFlag | 内存数据库任务启动标志 | bool | 0/1 |
| failFlag | 失败状态标志 | bool | 0/1 |
| stopFlag | 停止状态标志 | bool | 0/1 |
| finishFlag | 完成状态标志 | bool | 0/1 |
| diskId | 磁盘ID | int/hex | >=0 |
| ret | 返回码 | int | 0表示成功 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| failFlag | =0 |
| ret | =0 |
| memDb startFlag | 0~1 |
| stopFlag | 0~1 |
| finishFlag | 0~1 |
| taskType | 已知枚举集合 |

### (Lwp:<\*>)index:<\*>, dsgingChunkNum:<\*>, mergingChunkNum:<\*>, rewritingNum:<\*>, ckWaitDsg:<\*>, ckWaitMerge:<\*>, ckInRecy:<\*>, openRef:<\*>, state:<\*>, walSize:<\*>, fState:<\*>, lState:<\*>, metaStateCnt:<\*>, LsStartAddr:<\*>, LsEndAddr:<\*>, subviewHangTime:<\*>

``
(Lwp:4c70)index:718, dsgingChunkNum:0, mergingChunkNum:0, rewritingNum:0, ckWaitDsg:0, ckWaitMerge:0, ckInRecy:0, openRef:0, state:0, walSize:0, fState:0, lState:0, metaStateCnt:0, LsStartAddr:18446744073709551615, LsEndAddr:18446744073709551615, subviewHangTime:0
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| Lwp | 轻量线程ID | hex/int | 合法线程ID |
| index | Lwp索引 | int | >=0 |
| dsgingChunkNum | 正在设计/处理chunk数量 | int | >=0 |
| mergingChunkNum | 正在合并chunk数量 | int | >=0 |
| rewritingNum | 重写任务数量 | int | >=0 |
| ckWaitDsg | checkpoint等待设计数量 | int | >=0 |
| ckWaitMerge | checkpoint等待merge数量 | int | >=0 |
| ckInRecy | 回收中的checkpoint数量 | int | >=0 |
| openRef | 打开引用数量 | int | >=0 |
| state | 当前状态 | enum | 依赖状态机 |
| walSize | WAL大小 | int | >=0 |
| fState | fast状态 | enum | 依赖状态机 |
| lState | local状态 | enum | 依赖状态机 |
| metaStateCnt | metadata状态数量 | int | >=0 |
| LsStartAddr | 日志空间起始地址 | uint64 | >=0 |
| LsEndAddr | 日志空间结束地址 | uint64 | >=LsStartAddr |
| subviewHangTime | 子视图阻塞时间 | int | >=0 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| dsgingChunkNum | 0 ~ 100 |
| mergingChunkNum | 0 ~ 100 |
| rewritingNum | 0 ~ 100 |
| ckWaitDsg | 0 ~ 100 |
| ckWaitMerge | 0 ~ 100 |
| ckInRecy | 0 ~ 100 |
| walSize | 0 ~ 配置容量上限 |
| subviewHangTime | 0 ~ 60000 ms |
| state | 已知状态集合 |
| fState | 已知状态集合 |
| lState | 已知状态集合 |

### GMM: Get ddId(<\*>) totalWriteBackSeq(<\*>), totalDiffSeq(<\*>), statKvsNum(<\*>), sortType(<\*>)(<\*>: ratio, <\*>: seq), BalanceResult: <\*>(<\*>: not balance, <\*>: keep, <\*>: balance)

``
GMM: Get ddId(0) totalWriteBackSeq(0), totalDiffSeq(4), statKvsNum(4), sortType(0)(0: ratio, 1: seq), BalanceResult: 1(0: not balance, 1: keep, 2: balance).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| ddId | 数据域ID（Data Domain ID） | int | >=0 |
| totalWriteBackSeq | 写回序列总数 | int | >=0 |
| totalDiffSeq | 差异序列数量 | int | >=0 |
| statKvsNum | 统计KV数量 | int | >=0 |
| sortType | 排序类型 | enum | 0=ratio，1=seq |
| BalanceResult | 均衡结果 | enum | 0=not balance，1=keep，2=balance |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| totalWriteBackSeq | >=0 |
| totalDiffSeq | >=0 |
| statKvsNum | >=0 |
| sortType | 0~1 |
| BalanceResult | 0~2 |

### GMM: bak sequence to stashDB successdpId(<\*>), kvsId(<\*>), isSync(<\*>), seq(<\*>)(start, end, his, local seq,start,end)

``
GMM: bak sequence to stashDB successdpId(0), kvsId(42), isSync(0), seq(517, 517, 517, 517, 517, 517)(start, end, his, local seq,start,end)
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| dpId | 数据处理节点ID | int | >=0 |
| kvsId | KV存储实例ID | int | >=0 |
| isSync | 是否同步 | bool | 0/1 |
| seq start | 当前起始序列号 | uint64 | >=0 |
| seq end | 当前结束序列号 | uint64 | >=seq start |
| seq his | 历史序列号 | uint64 | >=0 |
| local seq start | 本地起始序列 | uint64 | >=0 |
| local seq end | 本地结束序列 | uint64 | >=local start |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| isSync | 0~1 |
| seq start | >=0 |
| seq end | >=seq start |
| his seq | >=0 |
| local seq start | >=0 |
| local seq end | >=local seq start |

### specifyRaid(<\*>) NormalDisk(<\*>) optimalColNum(<\*>) maxRaidLen(<\*>) maxHotDisk(<\*>) hotDiskNum(<\*>) excludeFlag(<\*>) dpId(<\*>) tierType(<\*>)

``
specifyRaid(1) NormalDisk(36) optimalColNum(25) maxRaidLen(25) maxHotDisk(1) hotDiskNum(1) excludeFlag(1, 0) dpId(0x0) tierType(0).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| specifyRaid | RAID配置选择标志 | enum/bool | 0/1 |
| NormalDisk | 普通磁盘数量 | int | >=0 |
| optimalColNum | 最优RAID列数量 | int | >0 |
| maxRaidLen | 最大RAID长度 | int | >0 |
| maxHotDisk | 最大热备盘数量 | int | >=0 |
| hotDiskNum | 当前热备盘数量 | int | >=0 |
| excludeFlag | 排除标志 | enum | 0/1 |
| dpId | 数据处理节点ID | hex/int | >=0 |
| tierType | 存储层类型 | enum | 0~N |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| specifyRaid | 0~1 |
| NormalDisk | >0 |
| optimalColNum | 1~128 |
| maxRaidLen | 1~128 |
| maxHotDisk | 0~10 |
| hotDiskNum | 0~maxHotDisk |
| excludeFlag | 0~1 |
| tierType | 已知枚举集合 |

### TierType(<\*>) oldTotalSize(<\*>) newTotalSize(<\*>) raidLostCap(<\*>) usedHotSpare(<\*>) oldUsedHotSpare(<\*>) outDiskUsedSize(<\*>) dpId(<\*>)

``
TierType(0) oldTotalSize(0x67b3a20000) newTotalSize(0x67b3a20000) raidLostCap(0x0) usedHotSpare(0x0) oldUsedHotSpare(0x0) outDiskUsedSize(0) dpId(0x0).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| TierType | 存储层类型 | enum | 0~N |
| oldTotalSize | 调整前总容量 | uint64/hex | >=0 |
| newTotalSize | 调整后总容量 | uint64/hex | >=0 |
| raidLostCap | RAID损耗容量 | uint64/hex | >=0 |
| usedHotSpare | 当前使用热备容量 | uint64/hex | >=0 |
| oldUsedHotSpare | 原热备使用容量 | uint64/hex | >=0 |
| outDiskUsedSize | 外置磁盘使用容量 | uint64 | >=0 |
| dpId | 数据节点ID | hex/int | >=0 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| TierType | 已知枚举 |
| newTotalSize | >=0 |
| oldTotalSize | >=0 |
| newTotalSize | 与oldTotalSize差异合理 |
| raidLostCap | 0 ~ oldTotalSize |
| usedHotSpare | >=0 |
| outDiskUsedSize | >=0 |

### FRU_HANDLE : Query clusterId(<\*>) nodeId(<\*>) fru info: NodeService(<\*>), lsidStatus(<\*>), bbustatus(<\*>), rootDiskStatus(<\*>), CofferMemStatus(<\*>), SingleNodeWrthroughState(<\*>), ForceWriteBack(<\*>), fruNormal(<\*>)

``
FRU_HANDLE : Query clusterId(0x0) nodeId(0) fru info: NodeService(3), lsidStatus(9), bbustatus(0), rootDiskStatus(1), CofferMemStatus(1), SingleNodeWrthroughState(0), ForceWriteBack(0), fruNormal(0).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| clusterId | 集群ID | hex/int | >=0 |
| nodeId | 节点ID | int | >=0 |
| NodeService | 节点服务状态 | enum | 依赖服务状态定义 |
| lsidStatus | LSID状态 | enum | 依赖LSID状态机 |
| bbustatus | BBU（电池备份单元）状态 | enum | 0通常表示正常 |
| rootDiskStatus | 根盘状态 | enum | 0/1或状态枚举 |
| CofferMemStatus | Coffer内存状态 | enum | 0/1或状态枚举 |
| SingleNodeWrthroughState | 单节点写透状态 | enum | 0/1 |
| ForceWriteBack | 强制写回状态 | enum | 0/1 |
| fruNormal | FRU整体健康状态 | enum/bool | 0/1 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| bbustatus | =0 |
| rootDiskStatus | =1 |
| CofferMemStatus | =1 |
| SingleNodeWrthroughState | 0~1 |
| ForceWriteBack | 0 |
| fruNormal | =1 |
| NodeService | 正常状态枚举 |
| lsidStatus | 正常状态集合 |

### Report alarm:csn <\*>, id <\*>, level <\*>, type <\*>, occur <\*>, recover <\*>, status <\*>, flag <\*>, objType <\*>, objId (), start <\*>, delay<\*>, fathers <\*>

``
Report alarm:csn 6087445, id 64462524423, level 3, type 1, occur 1779120133, recover 0, status 1, flag 0x51200008, objType 580, objId (), start 0, delay 0, fathers 0 0 0 0 0.
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| csn | 告警序列号 | uint64/int | >=0 |
| id | 告警ID | uint64 | >=0 |
| level | 告警等级 | enum | 通常1~4/5 |
| type | 告警类型 | enum | 依赖定义 |
| occur | 告警发生时间 | timestamp | Unix时间戳 |
| recover | 恢复时间 | timestamp | 0表示未恢复，否则>=occur |
| status | 当前告警状态 | enum | 0/1 |
| flag | 告警标志位 | bitmask/hex | 合法bit组合 |
| objType | 告警对象类型 | enum | 已定义集合 |
| objId | 告警对象ID | string/int | 可为空 |
| start | 告警开始位置 | int | >=0 |
| delay | 延迟时间 | int | >=0 |
| fathers | 父告警链 | int列表 | >=0 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| level | 1~5 |
| recover | 0 或 >=occur |
| status | 0~1 |
| delay | 0~600000 ms |
| occur | 合法Unix时间戳 |
| flag | 已知bit组合 |
| objType | 已知枚举集合 |

### Conver result to <\*>, StoreId <\*>, rNodeId <\*>, rdCrc <\*>, calCrc <\*>, crc check retCode(<\*>) offset(<\*>) nodSize(<\*>) level(<\*>)

``
Conver result to 0x80006b82, StoreId 910, rNodeId 172, rdCrc 48053, calCrc 3357, crc check retCode(0x80006b82) offset(b494) nodSize(508) level(1).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| Conver result | 转换结果码 | hex/int | 0表示成功 |
| StoreId | 存储实例ID | int | >=0 |
| rNodeId | 读取节点ID | int | >=0 |
| rdCrc | 读取CRC值 | int | >=0 |
| calCrc | 计算CRC值 | int | >=0 |
| crc check retCode | CRC校验返回码 | hex/int | 成功码 |
| offset | 数据偏移地址 | hex/int | >=0 |
| nodSize | 节点大小 | int | >0 |
| level | 数据层级 | enum | 0~N |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| Conver result | 成功返回码集合 |
| crc check retCode | 成功返回码 |
| rdCrc | >=0 |
| calCrc | >=0 |
| rdCrc-calCrc | 差异应接近0 |
| offset | >=0 |
| nodSize | 1KB~数MB |
| level | 已知层级集合 |

### (Lwp:<\*>)Seal stream(<\*>) offset(<\*>) mode(<\*>) durability(<\*>) waitTaskCnt(<\*>) dispatch(<\*>) tryFreePos(<\*>) preFreePos(<\*>) unmappedPos(<\*>) preFreeDataPos(<\*>) recordPos(<\*>) ret:<\*>

``
(Lwp:4c70)Seal stream(N_DATA_0_1_1_0) offset(453405336141823) mode(1) durability(2) waitTaskCnt(0) dispatch(0) tryFreePos(453405269032960) preFreePos(453405269032960) unmappedPos(453405269032960) preFreeDataPos(0) recordPos(453369365790720) ret:6.
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| Lwp | 工作线程ID | hex/int | 合法线程ID |
| stream | 数据流名称 | string | 固定格式 |
| offset | 当前流偏移 | uint64 | >=0 |
| mode | Seal模式 | enum | 依赖定义 |
| durability | 持久化级别 | enum | 0~N |
| waitTaskCnt | 等待任务数量 | int | >=0 |
| dispatch | 调度状态 | enum | 0/1 |
| tryFreePos | 尝试释放位置 | uint64 | >=0 |
| preFreePos | 上一次释放位置 | uint64 | >=0 |
| unmappedPos | 未映射位置 | uint64 | >=0 |
| preFreeDataPos | 上次释放数据位置 | uint64 | >=0 |
| recordPos | 记录位置 | uint64 | >=0 |
| ret | 返回码 | int | 成功码 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| waitTaskCnt | 0~1000 |
| ret | 成功返回码集合 |
| mode | 已知枚举 |
| durability | 已知枚举 |
| offset | >=0 |
| tryFreePos | >=preFreePos |
| recordPos | >=0 |

### (Lwp:<\*>)keynum(<\*>) prefix(<\*>)off(<\*>)len(<\*>)tp(<\*>)shardId(<\*>)opCode(<\*>)TierPlace(<\*>)policy(<\*>)vId(<\*>)prio(<\*>)deadLine(<\*>)flags(<\*>)setType(<\*>)setId(<\*>)append(<\*>)firstW(<\*>) curTime(<\*>)

``
(Lwp:4c70)keynum(0) prefix(2d8e)off(0)len(8192)tp(1)shardId(1729)opCode(3)TierPlace(0)policy(2)vId(1)prio(4)deadLine(0)flags(1)setType(0)setId(2d8e00000001)append(1)firstW(0) curTime(434048114273280)
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| Lwp | 工作线程ID | hex/int | 合法线程ID |
| keynum | 当前key数量 | int | >=0 |
| prefix | key前缀 | hex/string | 固定格式 |
| off | 数据偏移 | uint64 | >=0 |
| len | 数据长度 | int | >0 |
| tp | 类型标识 | enum | 依赖类型定义 |
| shardId | 分片ID | int | >=0 |
| opCode | 操作类型 | enum | 有限操作集合 |
| TierPlace | 数据所在层位置 | enum | 依赖tier定义 |
| policy | 调度策略 | enum | 有限集合 |
| vId | volume ID | int | >=0 |
| prio | 优先级 | enum/int | 业务定义 |
| deadLine | 截止时间 | uint64/int | >=0 |
| flags | 标志位 | bitmask | 合法bit组合 |
| setType | set类型 | enum | 有限集合 |
| setId | set标识 | hex/string | 固定格式 |
| append | 是否追加写 | bool | 0/1 |
| firstW | 是否首次写 | bool | 0/1 |
| curTime | 当前时间戳 | uint64 | 单调递增 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| keynum | >=0 |
| len | 512B ~ 16MB |
| shardId | >=0 |
| opCode | 已知操作枚举 |
| TierPlace | 已知枚举 |
| policy | 已知枚举 |
| prio | 0~10 |
| append | 0~1 |
| firstW | 0~1 |
| flags | 合法bit组合 |
| deadLine | >=0 |

### (post-ddp)BEST_MATCH: <\*> L <\*> Type <\*> UniqID <\*> vs R <\*> Type <\*> UniqID <\*> weight <\*>

``
(post-ddp)BEST_MATCH: 0x140021294f919a78 L 0 Type 1 UniqID da7ce7ab vs R 12 Type 4 UniqID 3104c75f weight 19
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| L ID | 左侧对象ID | hex | 合法对象地址 |
| L Type | 左侧对象类型 | enum | 已定义类型 |
| L UniqID | 左侧唯一ID | string | 固定格式 |
| R ID | 右侧对象ID | hex | 合法对象地址 |
| R Type | 右侧对象类型 | enum | 已定义类型 |
| R UniqID | 右侧唯一ID | string | 固定格式 |
| weight | 匹配权重 | int | >=0 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| L Type | 已知类型集合 |
| R Type | 已知类型集合 |
| weight | 0~100 |
| L/R ID | 合法hex格式 |

### (post-ddp)BEST_MATCH: leftNum <\*>(nrNormal <\*> + nrPreProc <\*>) rightNum <\*>(nrHybProc <\*> + nrProc <\*> + nrDedup <\*>)

``
(post-ddp)BEST_MATCH: leftNum 1(nrNormal 1 + nrPreProc 0) rightNum 10(nrHybProc 0 + nrProc 10 + nrDedup 0)
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| leftNum | 左侧匹配数量 | int | >=0 |
| nrNormal | 普通数量 | int | >=0 |
| nrPreProc | 预处理数量 | int | >=0 |
| rightNum | 右侧匹配数量 | int | >=0 |
| nrHybProc | Hybrid处理数量 | int | >=0 |
| nrProc | 处理数量 | int | >=0 |
| nrDedup | 去重数量 | int | >=0 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| leftNum | >=0 |
| rightNum | >=0 |
| nrNormal | >=0 |
| nrPreProc | >=0 |
| nrHybProc | >=0 |
| nrProc | >=0 |
| nrDedup | >=0 |
| rightNum/leftNum | 0~100 |

### FS:(<\*>)journal num is <\*>, fsId(<\*>), ret(<\*>)

``
FS:(0:4c70)journal num is 0, fsId(4803), ret(-2147483391).
``
参数含义表

| 参数 | 含义 | 类型 | 正常范围 |
|---|---|---|---|
| FS标识 | 文件系统线程/实例标识 | string | 固定格式 |
| journal num | Journal编号或数量 | int | >=0 |
| fsId | 文件系统ID | int | >=0 |
| ret | 返回码 | int | 0表示成功，负值通常异常 |

---

参数异常检测建议

| 参数 | 正常范围 |
|---|---|
| journal num | >=0 |
| fsId | >=0 |
| ret | =0 |
| ret | 非0进入异常检测 |
