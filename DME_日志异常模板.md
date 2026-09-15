# DME 日志异常模板

## 语义类

### 1. Scan kvm: traverse time out(<\*>), storeID(<\*>) num(<\*>)

``
Scan kvm: traverse time out(54 ms), storeID(70032) num(269).
``

### 2. PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:(<\*>)

``
PROTOCOL:MOD_BASE:IDNAME: Nfs4CreateUpdateUidNamehash failed:-1
``

### 3. PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapUidToUnameInner failed for (<\*>)

``
PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapUidToUnameInner failed for uId 3004
``

### 4. PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapGidToGnameInner failed for (<\*>)

``
PROTOCOL:MOD_BASE:IDNAME: Nfs4IdMapGidToGnameInner failed for gId 2000
``

### 5. FS:(<\*>)Uuid length is invalid(len <\*>)

``
FS:(0:4c70)Uuid length is invalid(len 27).
``

### 6. FS:(<\*>)check uuid para fail ret(<\*>)

``
FS:(0:4c70)check uuid para fail ret(-2147438954).
``

### 7. FS:(<\*>)Ret is<\*>, decode uuid failed

``
FS:(0:4c70)Ret is -2147438954, decode uuid failed.
``

### 8. Invalid mapped object (ID <\*>) for object type (ID <\*>), object (ID <\*>), the max mapped ID is (<\*>)

``
Invalid mapped object (ID 4294967294) for object type (ID 33), object (ID 4614219297510899712), the max mapped ID is (1023).
``

### 9. FS:(<\*>)gfs modify mmtime check func is NULL

``
FS:(0:4c70)gfs modify mmtime check func is NULL
``

### 10. Kv parameter check error. val(not null) val_len(<\*>)

``
Kv parameter check error. val(not null) val_len(0).
``

### 11. Db_name(<\*>) check parameter error, ret=<\*>

``
Db_name(                ADM_ADM_ARRAY_DB) check parameter error, ret=-2.
``

### 12. ADM_JAGUARDB_EVENT: get into db <\*> not success, ret:<\*>

``
ADM_JAGUARDB_EVENT: get into db ADM_ADM_ARRAY_DB not success, ret:-2.
``

### 13. print: <\*> messages suppressed.dopCountData,line=<\*>

``
print: 1409 messages suppressed.dopCountData,line=582.
``

### 14. Error type or threadId, count type : <\*>, threadId  <\*>

``
Error type or threadId, count type : 0, threadId  154 .
``

### 15. (post-ddp)Memcpy keyBug memcpy failed, ret(<\*>)

``
(post-ddp)Memcpy keyBug memcpy failed, ret(162).
``

### 16. Invalid mapped object (ID <\*>) for object type (ID <\*>), object (ID <\*>), the max mapped ID is (<\*>)

``
Invalid mapped object (ID 4294967294) for object type (ID 33), object (ID 4614219297510899712), the max mapped ID is (1023).
``

### 17. idx(<\*>): val_type(<\*>) is invalid

``
idx(38): val_type(0) is invalid.
``

### 18. Parse_and_trans_data_block failed, ret is <\*>

``
Parse_and_trans_data_block failed, ret is -9,822,4.
``

### 19. Optable compress error, ErrCode:(<\*>), Line:(<\*>), ModuleID:(<\*>)

``
Optable compress error, ErrCode:(-9), Line:(822), ModuleID:(4).
``

### 20. Get key for check timeout(<\*> ms), lpID(0) ret(<\*>)

``
Get key for check timeout(56 ms), lpID(0) ret(0x0)
``

### 21. Search tracepoint(<\*> ) failed, ret(<\*> )

``
Search tracepoint(CM_PUT_MSGGAGE_TO_BLOCKING_QUEUE_FAILED_392) failed, ret(3).
``

## 参数类

### 1. Xnet send msg(<\*>(mid:<\*>) -- <\*>(mid:<\*>), time use=<\*>(us), SNO=<\*>, reqSNO=<\*>, msg type=<\*>, plane=<\*>, xnetRet=<\*>

``
Xnet send msg(0x25e0000000a0001(mid:606) -- 0x25e000000120000(mid:606), time use=10772(us), SNO=2283037522451277885, reqSNO=0, msg type=1, plane=1, xnetRet=0.
``

### 2. PROTOCOL:MOD_NAS:Invalid(<\*>) delay start(<\*>)-end(<\*>)

``
PROTOCOL:MOD_NAS:Invalid(764) delay start(0)-end(417074873669340).
``

### 3. Update dpId(<\*>) taskType(<\*>) memDb startFlag(<\*>) failFlag(<\*>) stopFlag(<\*>) finishFlag(<\*>) diskId(<\*>) ret(<\*>).

``
Update dpId(<*>) taskType(<*>) memDb startFlag(<*>) failFlag(<*>) stopFlag(<*>) finishFlag(<*>) diskId(<*>) ret(<*>).
``

### 4. (Lwp:<\*>)index:<\*>, dsgingChunkNum:<\*>, mergingChunkNum:<\*>, rewritingNum:<\*>, ckWaitDsg:<\*>, ckWaitMerge:<\*>, ckInRecy:<\*>, openRef:<\*>, state:<\*>, walSize:<\*>, fState:<\*>, lState:<\*>, metaStateCnt:<\*>, LsStartAddr:<\*>, LsEndAddr:<\*>, subviewHangTime:<\*>

``
(Lwp:4c70)index:718, dsgingChunkNum:0, mergingChunkNum:0, rewritingNum:0, ckWaitDsg:0, ckWaitMerge:0, ckInRecy:0, openRef:0, state:0, walSize:0, fState:0, lState:0, metaStateCnt:0, LsStartAddr:18446744073709551615, LsEndAddr:18446744073709551615, subviewHangTime:0
``

### 5. GMM: Get ddId(<\*>) totalWriteBackSeq(<\*>), totalDiffSeq(<\*>), statKvsNum(<\*>), sortType(<\*>)(<\*>: ratio, <\*>: seq), BalanceResult: <\*>(<\*>: not balance, <\*>: keep, <\*>: balance).

``
GMM: Get ddId(0) totalWriteBackSeq(0), totalDiffSeq(4), statKvsNum(4), sortType(0)(0: ratio, 1: seq), BalanceResult: 1(0: not balance, 1: keep, 2: balance).
``

### 6. GMM: bak sequence to stashDB successdpId(<\*>), kvsId(<\*>), isSync(<\*>), seq(<\*>)(start, end, his, local seq,start,end)

``
GMM: bak sequence to stashDB successdpId(0), kvsId(42), isSync(0), seq(517, 517, 517, 517, 517, 517)(start, end, his, local seq,start,end)
``

### 7. specifyRaid(<\*>) NormalDisk(<\*>) optimalColNum(<\*>) maxRaidLen(<\*>) maxHotDisk(<\*>) hotDiskNum(<\*>) excludeFlag(<\*>) dpId(<\*>) tierType(<\*>).

``
specifyRaid(1) NormalDisk(36) optimalColNum(25) maxRaidLen(25) maxHotDisk(1) hotDiskNum(1) excludeFlag(1, 0) dpId(0x0) tierType(0).
``

### 8. TierType(<\*>) oldTotalSize(<\*>) newTotalSize(<\*>) raidLostCap(<\*>) usedHotSpare(<\*>) oldUsedHotSpare(<\*>) outDiskUsedSize(<\*>) dpId(<\*>).

``
TierType(0) oldTotalSize(0x67b3a20000) newTotalSize(0x67b3a20000) raidLostCap(0x0) usedHotSpare(0x0) oldUsedHotSpare(0x0) outDiskUsedSize(0) dpId(0x0).
``

### 9. FRU_HANDLE : Query clusterId(<\*>) nodeId(<\*>) fru info: NodeService(<\*>), lsidStatus(<\*>), bbustatus(<\*>), rootDiskStatus(<\*>), CofferMemStatus(<\*>), SingleNodeWrthroughState(<\*>), ForceWriteBack(<\*>), fruNormal(<\*>).

``
FRU_HANDLE : Query clusterId(0x0) nodeId(0) fru info: NodeService(3), lsidStatus(9), bbustatus(0), rootDiskStatus(1), CofferMemStatus(1), SingleNodeWrthroughState(0), ForceWriteBack(0), fruNormal(0).
``

### 10. Report alarm:csn <\*>, id <\*>, level <\*>, type <\*>, occur <\*>, recover <\*>, status <\*>, flag <\*>, objType <\*>, objId (), start <\*>, delay<\*>, fathers <\*>.

``
Report alarm:csn 6087445, id 64462524423, level 3, type 1, occur 1779120133, recover 0, status 1, flag 0x51200008, objType 580, objId (), start 0, delay 0, fathers 0 0 0 0 0.
``

### 11. Conver result to <\*>, StoreId <\*>, rNodeId <\*>, rdCrc <\*>, calCrc <\*>, crc check retCode(<\*>) offset(<\*>) nodSize(<\*>) level(<\*>).

``
Conver result to 0x80006b82, StoreId 910, rNodeId 172, rdCrc 48053, calCrc 3357, crc check retCode(0x80006b82) offset(b494) nodSize(508) level(1).
``

### 12. (Lwp:<\*>)Seal stream(<\*>) offset(<\*>) mode(<\*>) durability(<\*>) waitTaskCnt(<\*>) dispatch(<\*>) tryFreePos(<\*>) preFreePos(<\*>) unmappedPos(<\*>) preFreeDataPos(<\*>) recordPos(<\*>) ret:<\*>.

``
(Lwp:4c70)Seal stream(N_DATA_0_1_1_0) offset(453405336141823) mode(1) durability(2) waitTaskCnt(0) dispatch(0) tryFreePos(453405269032960) preFreePos(453405269032960) unmappedPos(453405269032960) preFreeDataPos(0) recordPos(453369365790720) ret:6.
``

### 13. (Lwp:<\*>)keynum(<\*>) prefix(<\*>)off(<\*>)len(<\*>)tp(<\*>)shardId(<\*>)opCode(<\*>)TierPlace(<\*>)policy(<\*>)vId(<\*>)prio(<\*>)deadLine(<\*>)flags(<\*>)setType(<\*>)setId(<\*>)append(<\*>)firstW(<\*>) curTime(<\*>)

``
(Lwp:4c70)keynum(0) prefix(2d8e)off(0)len(8192)tp(1)shardId(1729)opCode(3)TierPlace(0)policy(2)vId(1)prio(4)deadLine(0)flags(1)setType(0)setId(2d8e00000001)append(1)firstW(0) curTime(434048114273280)
``

### 14. (post-ddp)BEST_MATCH: <\*> L <\*> Type <\*> UniqID <\*> vs R <\*> Type <\*> UniqID <\*> weight <\*>

``
(post-ddp)BEST_MATCH: 0x140021294f919a78 L 0 Type 1 UniqID da7ce7ab vs R 12 Type 4 UniqID 3104c75f weight 19
``

### 15. (post-ddp)BEST_MATCH: leftNum <\*>(nrNormal <\*> + nrPreProc <\*>) rightNum <\*>(nrHybProc <\*> + nrProc <\*> + nrDedup <\*>)

``
(post-ddp)BEST_MATCH: leftNum 1(nrNormal 1 + nrPreProc 0) rightNum 10(nrHybProc 0 + nrProc 10 + nrDedup 0)
``

### 16. FS:(<\*>)journal num is <\*>, fsId(<\*>), ret(<\*>).

``
FS:(0:4c70)journal num is 0, fsId(4803), ret(-2147483391).
``

