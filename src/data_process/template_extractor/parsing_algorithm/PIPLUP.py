import regex as re
import os
import zlib
# pandas 已移除：DataFrame+iterrows 替换为 range+list 索引（优化1）
import logging
from datetime import datetime
from functools import lru_cache
import pickle

from .preprocessing import preprocess
from .postprocessing import correct_single_template
from src.common.global_config import GlobalConfig

logger = logging.getLogger(__name__)

# 轻量缓存归一化：必须含数字；连字符转义，避免形成意外字符范围。
_light_digit_re = re.compile(r'(?=[\d\s.,;\-/+_]*\d)[\d\s.,;\-/+_]+')

path_delimiters = {  # reduced set of delimiters for tokenizing for checking the path-like strings
        ' ', ',', '!', ';', ':',
        '=', '|', '"', "'", '+', '.',
        '[', ']', '(', ')', '{', '}', '_'
    }


def _structure_aware_merge(t1, t2, delimiters):
    """保留分隔符边界的共同前缀、后缀及同位置中段。"""
    if t1 == t2:
        return t1

    def covers(general, specific):
        pattern = '.*?'.join(re.escape(p) for p in general.split('<*>'))
        return re.fullmatch(pattern, specific, re.DOTALL) is not None

    # 已有通配模板覆盖新 token 时保持不变，避免收窄历史匹配范围。
    # 例如 <*> + 0, 仍为 <*>；仅当两侧都有逗号时才保留逗号。
    for general, specific in ((t1, t2), (t2, t1)):
        if '<*>' in general and covers(general, specific):
            return general

    lcp_len = 0
    for c1, c2 in zip(t1, t2):
        if c1 != c2:
            break
        lcp_len += 1

    head_end = -1
    for i in range(lcp_len - 1, -1, -1):
        if t1[i] in delimiters and t1[i] != '_':
            head_end = i
            break
    if head_end >= 0:
        for i in range(head_end + 1, lcp_len):
            if t1[i] == '_' and '_' in delimiters:
                if i < lcp_len - 1:
                    head_end = i
                break
    else:
        for i in range(lcp_len):
            if t1[i] in delimiters:
                head_end = i
                break
    head = t1[:head_end + 1]

    lcs_len = 0
    for c1, c2 in zip(reversed(t1), reversed(t2)):
        if c1 != c2:
            break
        lcs_len += 1
    tail = ''
    if lcs_len:
        start = len(t1) - lcs_len
        tail = t1[start:]
        for i in range(start, len(t1)):
            if t1[i] in delimiters:
                tail = t1[i:]
                break

    middle = ''
    if not tail:
        i = lcp_len
        while i < min(len(t1), len(t2)):
            if t1[i] in delimiters and t1[i] != '_' and t1[i] == t2[i]:
                j = i + 1
                while j < min(len(t1), len(t2)) and t1[j] == t2[j]:
                    j += 1
                if j - i > max(1, len(middle)):
                    middle = t1[i:j]
                i = j
            else:
                i += 1

    # 不按括号计数删补字符：开括号可能在 <*> 中，或在相邻 token 中。
    # 保留双方共有的闭括号及完整嵌套后缀，交给覆盖校验保证结果有效。
    if len(head) + len(middle) + len(tail) >= min(len(t1), len(t2)):
        return '<*>'
    result = head + '<*>' + (middle + '<*>' if middle else '') + tail
    # 防止 LCP/LCS 切开已有 <*>，以及中段和前后缀重叠。
    if not all(covers(result, token) for token in (t1, t2)):
        return '<*>'
    return result


@lru_cache(maxsize=4096)
def _compiled_template(template):
    """按模板字符串复用正则；仅缓存编译结果，每条日志仍完整匹配。"""
    parts = re.split(r"<\*>", template)
    pattern = ".*?".join(re.escape(part) for part in parts)
    return re.compile(f"^{pattern}$", re.DOTALL)


class Logcluster:
    def __init__(self, logTemplate='', logIDL=None, br_thresh=2):
        self.logTemplate = logTemplate
        if logIDL is None:
            logIDL = []
        self.logIDL = logIDL
        self.pathList = [logTemplate]
        self.templateList = [correct_single_template(' '.join(logTemplate))]
        self.br_thresh = br_thresh
        self.hit_time = 1

    # Check whether template1 can describe template2 (template2 can be merged into template1)
    @staticmethod
    def templateMatches(template1, template2):
        return _compiled_template(template1).fullmatch(template2) is not None
    
    def seqSimilarity(self, seq1, seq2):
        sim = 0
        for t1, t2 in zip(seq1, seq2):
            if t1 == t2 or '<*>' in t1: sim += 1
        return sim/len(seq1)
    
    def updateSequence(self, seq):
        # --- 核心修复：废弃原生的破损启发式校验，改为严格正则覆盖校验 ---
        seq_str = ' '.join(seq)
        is_covered = False

        # 遍历当前簇内所有已生成的候选模板
        for template in self.templateList:
            # 如果新日志能被现有任一模板完美匹配，则说明无新特征，安全返回
            if self.templateMatches(template, seq_str):
                is_covered = True
                break

        if is_covered:
            return False

        # Is the path list full?
        if len(self.pathList) < self.br_thresh:
            # Add to the path list
            self.pathList.append(seq)
        # If it is full, merge with the closest path
        else:
            maxSim, target = 0, None
            for t in self.pathList:
                sim = self.seqSimilarity(t, seq)
                if sim > maxSim:
                    maxSim = sim
                    target = t
            # Start merging
            for pos, (t1, t2) in enumerate(zip(target, seq)):
                if t1 != t2:
                    var = _structure_aware_merge(t1, t2, path_delimiters)
                    # 下方会更新所有路径，保留的结构也必须覆盖其余历史路径。
                    for p in self.pathList:
                        var = _structure_aware_merge(var, p[pos], path_delimiters)

                    # Update all lists
                    for p in self.pathList: p[pos] = var
        
        # Clean the list (preserve sorted order equivalent to np.unique(axis=0))
        unique_tuples = sorted(set(tuple(p) for p in self.pathList))
        self.pathList = [list(t) for t in unique_tuples]
        # Update the template list
        self.templateList = [correct_single_template(' '.join(t)) for t in self.pathList]
        # Clean the templates
        newList = [True for _ in self.templateList]
        for i, t1 in enumerate(self.templateList):
            if newList[i] == False: continue
            for j in range(i+1, len(self.templateList)):
                t2 = self.templateList[j]
                if self.templateMatches(t2, t1): newList[i] = False; break
                if self.templateMatches(t1, t2): newList[j] = False
        self.templateList = [item for i, item in enumerate(self.templateList) if newList[i]]
        
        return True



class LogParser:
    def __init__(self, log_format, module_name, indir='./', outdir='./result/',
                 br_thresh=5, update_population=385, sim_thresh='default', keep_para=False, print_info=False,
                 use_cache=False, cache_path=None, use_template_cache=True, debug=False):

        """
        Attributes
        ----------
            rex : regular expressions used in preprocessing (step1)
            path : the input path stores the input log file name
            st : similarity threshold
            maxChild : max number of children of an internal node
            logName : the name of the input file containing raw log messages
            savePath : the output path stores the file containing structured logs
        """
        self.path = indir
        if sim_thresh == 'default':
            self.st_dict = {
                1: 1,
                2: (2-1)/2,
                3: (3-1)/3,
                4: (4-1)/4,
                5: (5-2)/5, 
                6: (6-2)/6, 
                7: (7-3)/7,
                8: (8-3)/8,
                9: (9-3)/9,
                'others': 0.5
            }
        else:
            try:
                self.st_dict = {'others': float(sim_thresh)}
            except (ValueError, TypeError):
                raise ValueError("sim_thresh must be 'default' or a float value.")

        self.module_name = module_name
        self.use_cache = use_cache

        if cache_path is None:
            project_root = GlobalConfig().get_project_root()
            persistence_dir = os.path.join(project_root, 'config', 'persistence')
            # 按照 {module}_piplup_cache.pkl 规范组装文件名
            self.cache_path = os.path.join(persistence_dir, f"{self.module_name}_piplup_cache.pkl")
        else:
            self.cache_path = cache_path

        # 如果启用了缓存但目录不存在，则在初始化阶段建立物理目录树
        if self.use_cache:
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)

        self.logName = None
        self.savePath = outdir
        self.df_log = None
        self.update_population = update_population
        self.log_format = log_format
        self.keep_para = keep_para
        self.commonword_list = None
        self.storage_tree = {'None':{}}
        self.shallow_tree = {'None':[]}
        self.br_thresh = br_thresh
        self.print_info = print_info

        # 新增缓存属性
        self.logCluL = []  # 提升为全局实例变量，维护全生命周期聚类簇
        self.unsaved_updates = 0  # 状态变更计数器，用于限流写盘
        # CRC32 模板缓存：预处理后日志消息 → 模板字符串的 O(1) 快速路径
        self.use_template_cache = use_template_cache
        self.template_cache = {}
        # 轻量归一化缓存：预处理前用数字替换做 CRC32，跳过全量预处理
        self.light_cache = {}
        # 初始化时执行且仅执行一次加载
        if self.use_cache:
            self.load_cache()
        self.debug = debug

    def load_cache(self):
        """反序列化：加载本地解析树，并清洗历史行号记录"""
        logger.debug("Loading previous parsing tree...")
        if os.path.exists(self.cache_path) and os.path.getsize(self.cache_path) > 0:
            try:
                with open(self.cache_path, 'rb') as f:
                    cache_data = pickle.load(f)
                    self.storage_tree = cache_data['storage_tree']
                    self.shallow_tree = cache_data['shallow_tree']
                    self.logCluL = cache_data['logCluL']
                    if 'template_cache' in cache_data:
                        self.template_cache = cache_data['template_cache']
                    if 'light_cache' in cache_data:
                        self.light_cache = cache_data['light_cache']

                    # 强清空历史行号记录，防止流数据游标错位
                    for cluster in self.logCluL:
                        cluster.br_thresh = self.br_thresh
                        cluster.logIDL = []

                    logger.debug(
                        f"[PIPLUP State] IO Load Triggered. Successfully loaded {len(self.logCluL)} clusters from cache.")
            except Exception as e:
                logger.debug(f"Cache load failed: {e}. Degrading to empty cache.")
        else:
            logger.debug(f"Cache file {self.cache_path} is empty. Degrading to empty cache.")

    def save_cache(self):
        """序列化：将当前解析树状态落盘"""
        logger.debug("Saving parsing tree...")
        if self.use_cache:
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
            temp_path = self.cache_path + '.tmp'
            with open(temp_path, 'wb') as f:
                pickle.dump({
                    'storage_tree': self.storage_tree,
                    'shallow_tree': self.shallow_tree,
                    'logCluL': self.logCluL,
                    'template_cache': self.template_cache,
                    'light_cache': self.light_cache
                }, f)
            os.replace(temp_path, self.cache_path)
            self.unsaved_updates = 0  # 重置计数器

    @staticmethod
    def _compile_regex_patterns(templates: list) -> list:
        """
        统一编译聚类簇的正则匹配模式，隔离分词器产生的特征畸变
        """
        template_patterns = []
        for template in templates:
            pattern = re.escape(template).replace(re.escape("<*>"), ".*?")
            pattern = pattern.replace(r"\ ", r"\s*")
            pattern = f"^{pattern}$"
            template_patterns.append(pattern)
        return template_patterns

    @staticmethod
    def _regex_match_log(content: str, template_patterns: list) -> int:
        """
        统一执行单行日志的正则比对
        返回: 命中模板在列表中的索引 index。若无命中，返回 -1。
        """
        clean_content = str(content).strip()
        for j, pattern in enumerate(template_patterns):
            if bool(re.fullmatch(pattern, clean_content, re.DOTALL | re.IGNORECASE)):
                return j
        return -1

    def treeSearch(self, seq):
        retLogClust = None
        
        # Find the frequent word
        constant = 'None'
        for token in self.shallow_tree.keys():
            if token in seq: 
                constant = token
                break
        
        # Search on the second layer
        seqLen = len(seq)
        
        if seqLen not in self.storage_tree[constant].keys():
            return retLogClust, constant, seq
        else:
            logClustL = self.storage_tree[constant][seqLen]
        
        retLogClust, new_template = self.fastMatch(logClustL, seq)

        return retLogClust, constant, new_template

    def addSeqToPrefixTree(self, logClust):
        seqLen = len(logClust.logTemplate)
        # Default first token is None
        constant = 'None'
        
        for token in self.shallow_tree.keys(): 
            if token in logClust.logTemplate: 
                constant = token
                break

        if seqLen not in self.storage_tree[constant].keys():
            self.storage_tree[constant][seqLen] = [logClust]
        else:
            self.storage_tree[constant][seqLen].append(logClust)

        self.shallow_tree[constant].append(logClust)
        
        return constant

    #seq1 is template
    def seqDist(self, seq1, seq2):
        assert len(seq1) == len(seq2)
        simTokens = 0
        numOfPar = 0
        for token1, token2 in zip(seq1, seq2):
            if '<*>' in token1:
                if not any(ch.isalnum() for ch in token1): 
                    numOfPar += 1
                    continue
            if token1 == token2:
                simTokens += 1 

        retVal = float(simTokens) / len(seq1)
        return retVal, numOfPar


    def fastMatch(self, logClustL, seq):
        retLogClust = None

        maxSim = -1
        maxNumOfPara = -1
        maxClust = None

        for logClust in logClustL:
            # Directly take the same one
            if logClust.logTemplate == seq:
                maxSim = 1
                maxClust = logClust
                break
            curSim, curNumOfPara = self.seqDist(logClust.logTemplate, seq)
            if curSim>maxSim or (curSim==maxSim and curNumOfPara>maxNumOfPara):
                maxSim = curSim
                maxNumOfPara = curNumOfPara
                maxClust = logClust

        if len(seq) in self.st_dict.keys(): st = self.st_dict[len(seq)]
        else: st = self.st_dict['others']

        new_template = seq
        if maxSim >= st:
            retLogClust = maxClust  
            new_template = self.getTemplate(seq, retLogClust.logTemplate)
            
        return retLogClust, new_template

    def getTemplate(self, seq1, seq2):
        assert len(seq1) == len(seq2)
        return [w1 if w1 == w2 else '<*>' for w1, w2 in zip(seq1, seq2)]
    
    def jaccardSimilarity(self, template1, template2):
        set1 = set([item for item in template1.split() if any(ch.isalnum() for ch in item)])
        set2 = set([item for item in template2.split() if any(ch.isalnum() for ch in item)])

        if len(set1.union(set2)) == 0: return 0
        return len(set1.intersection(set2))/len(set1.union(set2))

    def mergeTemplates(self, cluster, constant, oldTemplateList=[]):
        # Find the cluster in the shallow tree
        cluster_list = self.shallow_tree[constant]
        # Iterate through the clusters with the same frequent word
        for i, t1 in enumerate(cluster.templateList):
            # Skip if it is not updated
            if t1 in oldTemplateList: continue
            # Skip if it is too general
            # Iterate through the clusters
            for logClust in cluster_list:
                # Skip if it is too general
                for j, t2 in enumerate(logClust.templateList):
                    if self.jaccardSimilarity(t1, t2) < 0.6: continue
                    # t1 described by t2
                    if len(t1.split()) >= len(t2.split()):
                        if Logcluster.templateMatches(t2, t1): 
                            cluster.templateList[i] = t2
                            break
                    # t2 described by t1
                    if len(t1.split()) <= len(t2.split()):
                        if Logcluster.templateMatches(t1, t2): 
                            logClust.templateList[j] = t1

    def parse_list(self, log_list: list) -> list:
        """
        自定义接口：输入日志字符串列表，输出对应的日志模板列表

        优化1：去掉 DataFrame + iterrows，改为 range + list 索引直接访问
        """
        # 优化1：不再构建 DataFrame，直接使用 list
        total_lines = len(log_list)

        # 赋一个虚拟的 logName，防止原本的类属性在后续逻辑中引发 NoneType 异常
        self.logName = "memory_logs"
        if self.print_info:
            logger.debug(f"Total lines loaded from memory list: {total_lines}")

        # 记录解析时间（可选，便于算法性能评估）
        start_time = datetime.now()

        # 修改点 1：
        hit_clusters = set()  # 记录本批次涉及的聚类簇，缩小遍历域

        # 预初始化结果列表；所有行在批末使用最终模板统一生成输出
        log_templates = [""] * total_lines
        cache_hits = 0

        # 第二步，执行解析循环...(无改动)
        count = 0
        matched_types = []
        use_sequence = []
        for idx in range(total_lines):
            logID = idx + 1
            content = log_list[idx]

            # ── Level 1: 轻量归一化 CRC32 缓存（预处理前） ──
            if self.use_template_cache:
                light = _light_digit_re.sub('0', content)
                light_crc = zlib.crc32(light.encode('utf-8')) & 0xffffffff
                cached_hit = None
                cached_clusters = self.light_cache.get(light_crc)
                if cached_clusters:
                    raw_content = str(content).strip()
                    for cluster in cached_clusters:
                        templates_c = cluster.templateList
                        if len(templates_c) == 1:
                            # 快速子串校验：模板中非 <*> 部分必须都出现在原始行中
                            parts = [p for p in templates_c[0].split('<*>') if p]
                            if all(p in raw_content for p in parts):
                                cached_hit = cluster
                                break
                        else:
                            template_patterns = self._compile_regex_patterns(templates_c)
                            match_idx = self._regex_match_log(raw_content, template_patterns)
                            if match_idx != -1:
                                cached_hit = cluster
                                break
                if cached_hit is not None:
                    cached_hit.hit_time += 1
                    cached_hit.logIDL.append(logID)
                    hit_clusters.add(cached_hit)
                    cache_hits += 1
                    continue

            # Add preprocess
            if idx < 2000:
                logmessageL, sequence, matched = preprocess(content, estimation_stage=True)
                # print(matched_types)
                logmessageL = logmessageL.strip().split()
                matched_types.extend(matched)
                use_sequence = sequence
            else:
                if idx == 2000:
                    matched_types = set(matched_types)
                    # print(matched_types)
                    use_sequence = [i for i in use_sequence if i in matched_types]
                logmessageL = preprocess(content, estimation_stage=False,
                                         use_sequence=use_sequence).strip().split()

            # ── Level 2: 预处理形式 CRC32 缓存 ──
            # 对预处理后的日志消息计算 CRC32，O(1) 查表命中簇引用后直接取当前模板
            if self.use_template_cache:
                crc = zlib.crc32(' '.join(logmessageL).encode('utf-8')) & 0xffffffff
                cached_cluster = self.template_cache.get(crc)
                if cached_cluster is not None:
                    # 回填 light_cache：确保同一 light form 的后续行能跳过预处理
                    bucket = self.light_cache.setdefault(light_crc, [])
                    if cached_cluster not in bucket:
                        bucket.append(cached_cluster)
                    cached_cluster.hit_time += 1
                    cached_cluster.logIDL.append(logID)
                    hit_clusters.add(cached_cluster)
                    cache_hits += 1
                    continue

            matchCluster, first_constant, newTemplate = self.treeSearch(logmessageL)

            # Match no existing log cluster
            if matchCluster is None:
                newCluster = Logcluster(logTemplate=logmessageL, logIDL=[logID], br_thresh=self.br_thresh)
                self.logCluL.append(newCluster)  # 追加至全局类变量
                hit_clusters.add(newCluster)  # 记录本批次命中
                constant = self.addSeqToPrefixTree(newCluster)
                self.mergeTemplates(newCluster, constant)
                self.unsaved_updates += 1  # 变更计数器累加

            # Add the new log message to the existing cluster
            else:
                # Increase the hit time
                matchCluster.hit_time += 1
                oldTemplate = matchCluster.logTemplate
                matchCluster.logTemplate = newTemplate
                constant = first_constant
                # The template changed, update the list
                oldTemplateList = matchCluster.templateList.copy()
                matchCluster.logIDL.append(logID)
                hit_clusters.add(matchCluster)  # 记录本批次命中
                updated = matchCluster.updateSequence(logmessageL)

                if (
                        oldTemplate != newTemplate or matchCluster.hit_time == self.update_population) and first_constant == 'None':
                    # Find the first constant
                    for token in newTemplate:
                        if '<*>' not in token:
                            constant = token
                            break

                    # Update the tree
                    # 1. Initialize the clusters
                    if constant in self.storage_tree.keys():
                        if len(logmessageL) not in self.storage_tree[constant].keys():
                            self.storage_tree[constant][len(logmessageL)] = []
                    else:
                        self.storage_tree[constant] = {len(logmessageL): []}
                        self.shallow_tree[constant] = []

                    # 2. remove the cluster from 'None' in both trees and update the other clusters
                    if constant != 'None':
                        # Find the clusters with the new constant
                        shallowTreeCopy = self.shallow_tree['None'].copy()
                        for cluster in self.shallow_tree['None']:
                            if constant in cluster.logTemplate:
                                self.shallow_tree[constant].append(cluster)
                                if len(cluster.logTemplate) in self.storage_tree[constant].keys():
                                    self.storage_tree[constant][len(cluster.logTemplate)].append(cluster)
                                else:
                                    self.storage_tree[constant][len(cluster.logTemplate)] = [cluster]

                                index = self.storage_tree['None'][len(cluster.logTemplate)].index(cluster)
                                self.storage_tree['None'][len(cluster.logTemplate)].pop(index)
                                index = shallowTreeCopy.index(cluster)
                                shallowTreeCopy.pop(index)
                        self.shallow_tree['None'] = shallowTreeCopy

                if updated:
                    self.mergeTemplates(matchCluster, constant, oldTemplateList)
                    self.unsaved_updates += 1  # 变更计数器累加

            # 缓存簇引用供后续重复日志 O(1) 快速命中
            if self.use_template_cache:
                cluster_ref = newCluster if matchCluster is None else matchCluster
                self.template_cache[crc] = cluster_ref
                # 同时存入轻量缓存（预处理前查表用）
                bucket = self.light_cache.setdefault(light_crc, [])
                if cluster_ref not in bucket:
                    bucket.append(cluster_ref)

            count += 1
            if self.print_info and (count % 1000 == 0 or count == total_lines):
                logger.debug('Processed {0:.1f}% of log lines.'.format(count * 100.0 / total_lines))


        # --- 第三步：重构输出侧，生成并返回模板列表 ---
        # 缓存命中行也已登记到簇中，统一使用批末 templateList 输出

        for logClust in hit_clusters:
            templates = logClust.templateList

            if len(templates) == 1:
                for logID in logClust.logIDL:
                    log_templates[logID - 1] = templates[0]
            else:
                # 调用公共方法生成正则
                template_patterns = self._compile_regex_patterns(templates)

                for logID in logClust.logIDL:
                    index = logID - 1
                    content = str(log_list[index])

                    # 调用公共方法执行比对
                    match_idx = self._regex_match_log(content, template_patterns)
                    if match_idx != -1:
                        log_templates[index] = templates[match_idx]
                    else:
                        # 兜底容错：如果极端情况下正则均未命中，默认赋予簇内第一个模板
                        log_templates[index] = templates[0]
                        if self.debug:
                            global_idx = self.logCluL.index(logClust)
                            logger.debug(f"No template matched for log: {content}")
                            logger.debug(f"          -> Assigned Cluster Index: E{global_idx:04d}")
                            logger.debug(f"          -> Candidate Templates in this cluster: {templates}")
                            logger.debug(f"          -> pathList length: {len(logClust.pathList)}")

            # 关键：本批次处理完毕后，强制清空该簇内的 LineId 挂载，释放游标，防止下一次流计算内存泄漏或错位
            logClust.logIDL = []

        if self.print_info:
            cache_info = f", cache_hits={cache_hits}/{total_lines}" if self.use_template_cache else ""
            logger.debug(f"Parsing done. [Time taken: {datetime.now() - start_time}{cache_info}]")

        # 策略化异步落盘：如果解析树发生了结构变动超过10000次，自动静默触发落盘一次
        if self.use_cache and self.unsaved_updates > 10000:
            self.save_cache()

        return log_templates


