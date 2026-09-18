import re
import os
import zlib
# pandas 已移除：DataFrame+iterrows 替换为 range+list 索引（优化1）
import logging
from datetime import datetime
import pickle
import weakref

from .preprocessing import preprocess
from .postprocessing import correct_single_template
from .token_structure import structure_similarity, merge_kv_tokens, merge_values
from src.common.global_config import GlobalConfig

logger = logging.getLogger(__name__)

# 注：regex 模块已替换为标准库 re。本模块用到的模式（re.escape、<\*>、
# .*?、lookbehind/lookahead）均为两者共有语法，且 escape 对空格/特殊字符
# 的处理经实测一致（' ' → '\ '，'<*>' → '<\*>'）；regex 模块的每次
# escape/_compile 都有可观的 enum 校验与初始化开销（cProfile 中合计
# >0.6s/2 万行），标准库 re 无此负担。

# 轻量归一化正则：替换连续的[数字+分隔符]序列，用于预处理前 CRC32 缓存预检
# 匹配至少含一个数字的连续字符序列，字符 ∈ {数字, 分隔符, 下划线}
# 下划线 _ 属于 \w，在日志中广泛用于连接复合数值（如 Arg:0_1_5、Cap:15749414426_16561907771）
# 冒号 : 不纳入——它是 key:value 分隔符（Cap:、Arg:），应保留
_light_digit_re = re.compile(r'(?=[\d\s.,;\-/+_]*\d)[\d\s.,;\-/+_]+')

path_delimiters = {  # reduced set of delimiters for tokenizing for checking the path-like strings
        ' ', ',', '!', ';', ':',
        '=', '|', '"', "'", '+', '.',
        '[', ']', '(', ')', '{', '}',
        '_'  # 下划线：日志标识符（如 xnet_m50_x0、conn_mode、down_count）用 _ 连接，
             # 不含 _ 时 _structure_aware_merge 回退到 ( 等粗粒度分隔符，导致过度泛化
    }


# 模块级缓存统一封顶：超限整表清空（只丢缓存可重建，不影响正确性）
_CACHE_LIMIT = 1 << 16
# jaccardSimilarity 的模板词集合缓存
_WORD_SET_CACHE = {}
# <*> 转义结果为模块级常量：模板固定段转义后按此切分
_ESCAPED_WILDCARD = re.escape('<*>')


def _wildcard_fullmatch(template, text):
    """线性通配符全匹配：判定 text 能否被 template（<*> 为通配）完整覆盖。

    等价于 re.fullmatch('.*?'.join(escape(段)), text, re.DOTALL)（按 <*>
    切分的字面量段间用非贪婪任意内容连接），但改为贪心最早匹配：
    首段必须为前缀，中间段依次取 >= 当前位置的最早出现，末段必须为
    后缀且起点不早于前段终点。O(n×段数) 纯字符串查找，彻底消除
    .*? 链在近似失配长行上的组合回溯。

    贪心正确性（交换论证）：中间段取最早出现位置为后续段保留最大
    空间，存在可行分解当且仅当贪心可行；全匹配下 .*? 的懒惰/贪婪
    不改变匹配存在性。
    """
    if template == text:
        return True
    if '<*>' not in template:
        return False
    segs = template.split('<*>')
    n = len(text)

    head = segs[0]
    if head:
        if not text.startswith(head):
            return False
        pos = len(head)
    else:
        pos = 0

    for seg in segs[1:-1]:
        if not seg:
            continue
        idx = text.find(seg, pos)
        if idx == -1:
            return False
        pos = idx + len(seg)

    tail = segs[-1]
    if tail:
        start = n - len(tail)
        if start < pos:
            return False
        return text.startswith(tail, start)
    return True


def _token_covers(template_token, text):
    """模板 token（可含 <*>）作为模式是否能覆盖 text 字符串。"""
    return _wildcard_fullmatch(template_token, text)


def _fixed_brackets_balanced(candidate):
    """检查候选 token 固定部分（去除 <*> 后）的三类括号是否配对平衡。"""
    stack = []
    pairs = {')': '(', ']': '[', '}': '{'}
    for ch in candidate.replace('<*>', ''):
        if ch in '([{':
            stack.append(ch)
        elif ch in ')]}':
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
    return not stack


def _progressive_generalize(token, delimiters, max_candidates=6):
    """渐进泛化生成器：沿完整逻辑边界逐步将 token 尾部常量替换为 <*>。

    token 按 <*> 切分为常量段 p0..pn（n 个通配符）。每个候选保留
    p0..p(k-1) 与 pk 的前缀（在非下划线分隔符之后切齐），其余塌缩为
    单个尾部 <*>。按 (k, j) 从尾向头的字典序枚举，保证候选序列严格
    逐步泛化（后者覆盖前者），且每个候选都覆盖原 token。

    约束：
    - 候选固定部分括号必须平衡（避免括号截断）；
    - 去重，数量封顶 max_candidates（避免无限增长）；
    - 只在非下划线分隔符边界切分（下划线连接的子部分通常是变量，
      不作为保留边界）。

    例如 '(xnet_<*>:<*>)(num:<*>)server' 依次产生：
      '(xnet_<*>:<*>)(num:<*>)<*>'
      '(xnet_<*>:<*>)<*>'          （吸收 )(num: 段，仅保留配对的 ) ）
      '<*>'
    """
    parts = token.split('<*>')
    if len(parts) < 2:
        return
    last = len(parts) - 1
    seen = set()
    count = 0
    for k in range(last, -1, -1):
        seg = parts[k]
        # 段内切点：各非下划线分隔符之后（从后往前），最后补段首
        cut_points = [d + 1 for d in range(len(seg) - 1, -1, -1)
                      if seg[d] in delimiters and seg[d] != '_']
        cut_points.append(0)
        for j in cut_points:
            prefix = ''.join(p + '<*>' for p in parts[:k])
            cand = prefix + seg[:j] + '<*>'
            # 合并尾部连续通配符（如 <*>：<*> 形式的退化）
            while cand.endswith('<*><*>'):
                cand = cand[:-3]
            if cand in seen or cand == token:
                continue
            if not _fixed_brackets_balanced(cand):
                continue
            seen.add(cand)
            yield cand
            count += 1
            if cand == '<*>' or count >= max_candidates:
                return


def _structure_aware_merge(t1, t2, delimiters):
    """结构感知的 token 合并：保留共同前/后缀中到分隔符边界的稳定部分。

    核心不变量：合并结果必须同时覆盖 t1 和 t2（作为模板语言，二者都
    是合并结果的特例）。若一侧已含 <*> 且能覆盖另一侧，直接返回覆盖
    侧——不叠加仅单侧出现的分隔符（如 merge('<*>','0,') 不得产出
    '<*>,'，那会丢失原先可接受的 '7'）；双方共有的分隔符由 LCP/LCS
    路径自然保留（merge('0,','5,') → '<*>,'）。

    合并路径（按序尝试）：
    1. 完全相同 → 原样返回
    2. kv 结构（key:value / key=value，同字段名）→ 值合并（数字组边界）
    3. 裸复合标识符/数字链（双方含数字、无通配）→ merge_values
       （保留完整公共前/后缀，括号失衡时回退 LCP/LCS）
    4. 通配覆盖检查：一侧含 <*> 且能覆盖另一侧 → 返回覆盖侧
    5. 双方都含 <*>：渐进泛化；仅得 <*> 时优先 LCP/LCS 结果（可能
       保留中缀结构，如 (xnet_<*>:0)）
    6. LCP/LCS 字符级合并（head/middle/tail + 括号平衡 + 重叠保护）
    """

    # 完全相同则直接返回
    if t1 == t2:
        return t1

    # kv 结构合并：同字段名的复合值（key:value / key=value），不依赖业务掩码。
    # 仅在双方均无 <*> 时启用；含 <*> 的组合走下方通配覆盖与渐进泛化。
    kv_merged = merge_kv_tokens(t1, t2)
    if kv_merged is not None:
        return kv_merged

    # 裸复合标识符/数字链（无 kv 上下文）：保留完整公共前/后缀并做
    # 数字组边界修剪（fsi_snap_fs5041_1058676 → fsi_snap_fs<*>）。
    # 仅在双方均无 <*> 时启用；括号失衡时（结构在中段，如 cap:[a_b][c_d]）
    # 回退 LCP/LCS 路径。
    if ('<*>' not in t1 and '<*>' not in t2
            and any(c.isdigit() for c in t1) and any(c.isdigit() for c in t2)):
        bare = merge_values(t1, t2)
        if _fixed_brackets_balanced(bare):
            return bare

    def _covers(general, specific):
        """general（含 <*>）作为模板是否能覆盖 specific 字符串。"""
        return _token_covers(general, specific)

    has_wild_1 = '<*>' in t1
    has_wild_2 = '<*>' in t2
    if has_wild_1 and not has_wild_2:
        if _covers(t1, t2):
            return t1
    elif has_wild_2 and not has_wild_1:
        if _covers(t2, t1):
            return t2
    elif has_wild_1 and has_wild_2:
        # 双方都含 <*>，互相检查是否一方能覆盖另一方
        if _covers(t1, t2):
            return t1
        if _covers(t2, t1):
            return t2

        # 渐进泛化：当双方都含 <*> 但互不覆盖时（如结尾不同 server vs client），
        # 尝试将一侧尾部的常量段逐级替换为 <*>，直到覆盖另一侧。
        # 仅得到平凡解 <*> 时不立即采用：先记录，若下方 LCP/LCS 路径
        # 能给出更具体且同样覆盖双方的结果（如 (xnet_<*>:0) 保留中缀），
        # 则优先 LCP/LCS。
        trivial = None
        for gen_token in _progressive_generalize(t1, delimiters):
            if _covers(gen_token, t2) and _covers(gen_token, t1):
                if gen_token != '<*>':
                    return gen_token
                trivial = '<*>'
                break
        if trivial is None:
            for gen_token in _progressive_generalize(t2, delimiters):
                if _covers(gen_token, t1) and _covers(gen_token, t2):
                    if gen_token != '<*>':
                        return gen_token
                    trivial = '<*>'
                    break
        if trivial is not None:
            lcp_result = _lcp_lcs_merge(t1, t2, delimiters)
            if (lcp_result != '<*>'
                    and _covers(lcp_result, t1) and _covers(lcp_result, t2)):
                return lcp_result
            return trivial

    return _lcp_lcs_merge(t1, t2, delimiters)


def _lcp_lcs_merge(t1, t2, delimiters):
    """LCP/LCS 字符级合并：head/middle/tail + 括号平衡 + 重叠保护。"""
    # Step 1-2: LCP → 回退到分隔符边界
    lcp_len = 0
    for c1, c2 in zip(t1, t2):
        if c1 == c2:
            lcp_len += 1
        else:
            break

    head = ''
    if lcp_len > 0:
        # 从 lcp_len 向前找最近的非下划线分隔符
        # 下划线 _ 连接的标识符子部分（如 xnet_r0_0_14 中的 r0、0、14）通常是变量，
        # 不应作为 head 边界，否则 head 会过拟合到 (xnet_r0_0_ 而非 (xnet_
        head_end = -1
        for i in range(lcp_len - 1, -1, -1):
            if t1[i] in delimiters and t1[i] != '_':
                head_end = i
                break
        if head_end >= 0:
            # 在非下划线分隔符之后，找第一个下划线作为 head 边界
            # 但仅当该下划线不是 LCP 的最后一个字符时才保留标识符前缀：
            # - name(DMI_SysPowerCheckThread) vs name(DMI_CpuFreqOptThread)
            #   LCP = name(DMI_ → _ 在末尾 → DMI_ 后就分叉 → head = name( (泛化)
            # - (xnet_r0_0_14) vs (xnet_r0_0_50)
            #   LCP = (xnet_r0_0_1 → _ 后还有 r0_0_1 → head = (xnet_ (保留前缀)
            for i in range(head_end + 1, lcp_len):
                if t1[i] == '_':
                    if i < lcp_len - 1:
                        head_end = i
                    break
            head = t1[:head_end + 1]
        else:
            # LCP 中无非下划线分隔符，从前往后找第一个下划线作为 head 边界
            # xnet_r0_0_14 vs xnet_r0_0_50 → head = xnet_（保留标识符首段，泛化后续变量）
            for i in range(lcp_len):
                if t1[i] in delimiters:
                    head_end = i
                    break
            if head_end >= 0:
                head = t1[:head_end + 1]

    # Step 3-4: LCS → 前进到分隔符边界
    lcs_len = 0
    for c1, c2 in zip(reversed(t1), reversed(t2)):
        if c1 == c2:
            lcs_len += 1
        else:
            break

    tail = ''
    if lcs_len > 0:
        # 在 LCS 区域内从前往后找第一个分隔符，取从该分隔符到末尾的全部作为 tail
        # 原因：LCS 是尾部共同部分，应尽可能完整地保留
        # 例如 GlobalInfo;) vs FrameInfo;) → LCS=Info;) → 找第一个分隔符 ; → tail=;)'
        # 而不是从后往前找最后一个分隔符 ) → tail=)（丢失 Info;）
        lcs_start = len(t1) - lcs_len
        tail_start = -1
        for i in range(lcs_start, len(t1)):
            if t1[i] in delimiters:
                tail_start = i
                break
        if tail_start >= 0:
            tail = t1[tail_start:]
        else:
            # LCS 区域内无分隔符，直接取完整 LCS
            tail = t1[lcs_start:]

    # Step 4.5: 中段补偿 — tail 为空时，在 LCP 之后寻找以非下划线分隔符开头的最长共同子串
    # 下划线 _ 连接的子部分（如 _14）通常是变量，不应作为中段起始边界，
    # 否则会导致后续合并时字符位置错位（<*> 3字符 vs 原始值 N字符不对齐）
    middle = ''
    if not tail and lcp_len < min(len(t1), len(t2)):
        best_mid = ''
        i = lcp_len
        while i < min(len(t1), len(t2)):
            if t1[i] in delimiters and t1[i] != '_' and t1[i] == t2[i]:
                # 找到共同非下划线分隔符，计算从这里开始的最长共同子串
                j = i
                while j < min(len(t1), len(t2)) and t1[j] == t2[j]:
                    j += 1
                mid = t1[i:j]
                if len(mid) > len(best_mid):
                    best_mid = mid
                i = j + 1
            else:
                i += 1
        if best_mid and len(best_mid) > 1:
            middle = best_mid

    # Step 5: 括号平衡检查
    # 考虑 head 和 tail 中所有的括号，而非仅 head 的开括号 vs tail 的闭括号
    # tail 内部可能含有 ( )，如 :<*>)(num:<*>) 中 ) 和 ( 都是结构的一部分
    #
    # 关键修正：当 head 为空时，<*> 区域可能包含对应的开括号
    # 例如 GlobalInfo;) vs FrameInfo;) → head='', tail=;)' → ) 对应原始 token 中的 (
    # 此时 tail 中的闭括号属于 <*> 区域的结构，不应删除。
    # 只有当 head 非空（head 中有开括号可抵消）时才做括号平衡。
    if head:
        open_count = head.count('(') + head.count('[') + head.count('{') + \
                     tail.count('(') + tail.count('[') + tail.count('{')
        close_count = head.count(')') + head.count(']') + head.count('}') + \
                      tail.count(')') + tail.count(']') + tail.count('}')
        if close_count > open_count:
            # 从 tail 前端逐字符移除多余的关闭括号
            excess = close_count - open_count
            new_tail = ''
            removed = 0
            for ch in tail:
                if removed < excess and ch in (')', ']', '}'):
                    removed += 1
                else:
                    new_tail += ch
            tail = new_tail

    # Step 6: 重叠保护
    total_fixed = len(head) + len(middle) + len(tail)
    if total_fixed >= min(len(t1), len(t2)):
        return '<*>'

    # Step 7: 组装结果
    result = head + '<*>'
    if middle:
        result += middle + '<*>'
    result += tail
    return result


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
        # templateList 世代计数：每次模板列表变化时递增，
        # 供簇级已编译模式缓存做失效判断（见 _get_cluster_patterns）
        self._tpl_version = 0

    # Check whether template1 can describe template2 (template2 can be merged into template1)
    @staticmethod
    def templateMatches(template1, template2):
        # 与 _token_covers 共用已编译模式缓存：原实现每次调用重跑
        # split+escape+join 并触发编译，是解析热路径的最大热点。
        # fullmatch 已隐含首尾锚定（原实现的 ^ $ 对 fullmatch 是冗余的）。
        return _wildcard_fullmatch(template1, template2)
    
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
                    # 优先使用 logTemplate[pos]（getTemplate 已增量合并出的最通用形式），
                    # 但仅在它同时覆盖 t1 与 t2 时采用（防归纳失效时排除既有路径）；
                    # 否则用结构感知合并。
                    lt = self.logTemplate[pos] if pos < len(self.logTemplate) else None
                    if (lt is not None and lt != t1 and lt != t2
                            and _token_covers(lt, t1) and _token_covers(lt, t2)):
                        var = lt
                    else:
                        var = _structure_aware_merge(t1, t2, path_delimiters)

                    # Update all lists
                    for p in self.pathList: p[pos] = var
        
        # Clean the list (preserve sorted order equivalent to np.unique(axis=0))
        unique_tuples = sorted(set(tuple(p) for p in self.pathList))
        self.pathList = [list(t) for t in unique_tuples]
        # 注意：不在容量未满时把 logTemplate 的簇级通配符同步进 pathList。
        # pathList 保存具体路径差异，泛化只发生在上方容量满的合并分支
        # （那里经 logTemplate[pos]/_structure_aware_merge 得到的 var 同时
        # 覆盖所有路径值后再统一回写）。提前同步会抹平多路径信息，
        # 使 templateList 提前塌缩为簇级概括，接受未出现过的取值组合。
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
        self._tpl_version += 1

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
        # 精确模板索引：logTemplate 元组 → 簇。树搜索在桶内是线性扫描，
        # 模板数上千后每行都要做 O(桶) 次 seqDist（吞吐崩塌为百行/秒级，
        # 表现为"卡死"）；完全相同的行先查此索引 O(1) 直达簇。
        self._exact_index = {}
        # seqDist 记忆化：扫桶时相同 (模板, 序列) 对的评分直接复用
        self._seqdist_cache = {}
        # 已登记常量键集：桶路由的成员判断依据（'None' 恒在），
        # 仅由 _reindex_from_none 与 load_cache 重建登记
        self._constant_keys = {'None'}
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

                # 先完整读取到局部变量，通过必要字段校验后再一次性提交。
                # 否则损坏/旧版本缓存若缺少中间字段，可能只覆盖 storage_tree
                # 就抛异常，留下树与簇列表不一致的半加载状态。
                storage_tree = cache_data['storage_tree']
                shallow_tree = cache_data['shallow_tree']
                log_clusters = cache_data['logCluL']
                template_cache = cache_data.get('template_cache', {})
                light_cache = cache_data.get('light_cache', {})

                if not isinstance(storage_tree, dict) or not isinstance(shallow_tree, dict):
                    raise TypeError("cache trees must be dictionaries")
                if not isinstance(log_clusters, list):
                    raise TypeError("cache logCluL must be a list")
                if not isinstance(template_cache, dict) or not isinstance(light_cache, dict):
                    raise TypeError("cache indexes must be dictionaries")

                # 强清空历史行号记录，防止流数据游标错位
                for cluster in log_clusters:
                    cluster.br_thresh = self.br_thresh
                    cluster.logIDL = []

                # ── 去重 + 重建桶（不能只重建 _exact_index）──
                # 历史缓存可能包含：模板演化前遗留的同模板重复簇、
                # 陈旧桶路由（簇按旧模板常量入桶后模板已变化）。
                # 1) 按模板元组去重，命中计数累加，保留首个；
                # 2) 以当前模板按统一路由规则重建 storage/shallow 两树；
                # 3) _exact_index 同步重建。
                unique = {}
                deduped = []
                for cluster in log_clusters:
                    key = tuple(cluster.logTemplate)
                    first = unique.get(key)
                    if first is None:
                        unique[key] = cluster
                        deduped.append(cluster)
                    else:
                        first.hit_time += getattr(cluster, 'hit_time', 1)

                storage_tree = {'None': {}}
                shallow_tree = {'None': []}
                exact_index = {}
                constant_keys = {'None'}
                for cluster in deduped:
                    # 与 _reindex_from_none 登记规则一致：首个非 <*> token
                    # 作为常量键，簇按此路由重建
                    constant = 'None'
                    for token in cluster.logTemplate:
                        if '<*>' not in token:
                            constant = token
                            break
                    if constant != 'None':
                        constant_keys.add(constant)
                    storage_tree.setdefault(constant, {}).setdefault(
                        len(cluster.logTemplate), []).append(cluster)
                    shallow_tree.setdefault(constant, []).append(cluster)
                    exact_index.setdefault(tuple(cluster.logTemplate), cluster)
                self._constant_keys = constant_keys

                log_clusters = deduped

                self.storage_tree = storage_tree
                self.shallow_tree = shallow_tree
                self.logCluL = log_clusters
                self.template_cache = template_cache
                self.light_cache = light_cache
                self._exact_index = exact_index

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
        统一编译聚类簇的正则匹配模式，隔离分词器产生的特征畸变。

        返回已编译对象而不是字符串，避免热路径每次 ``fullmatch`` 都走
        regex 模块的全局缓存查询/潜在重编译。簇级缓存负责在模板变化时
        失效，因此这里的编译成本只发生在每个模板世代的第一次匹配。
        """
        template_patterns = []
        for template in templates:
            pattern = re.escape(template).replace(_ESCAPED_WILDCARD, ".*?")
            pattern = pattern.replace(r"\ ", r"\s*")
            pattern = f"^{pattern}$"
            template_patterns.append(re.compile(pattern, re.DOTALL | re.IGNORECASE))
        return template_patterns

    # 簇级已编译模式缓存：WeakKeyDictionary，随簇对象回收自动释放，
    # 不进入 pickle 持久化；以 templateList 世代计数（_tpl_version）失效。
    # 空间成本与存活簇数同阶（每簇一份模式串列表）。
    _PATTERN_CACHE = weakref.WeakKeyDictionary()

    def _get_cluster_patterns(self, cluster):
        """取簇模板的已编译正则列表（带世代失效的复用）。"""
        version = getattr(cluster, '_tpl_version', 0)
        entry = self._PATTERN_CACHE.get(cluster)
        if entry is not None and entry[0] == version:
            return entry[1]
        patterns = self._compile_regex_patterns(cluster.templateList)
        self._PATTERN_CACHE[cluster] = (version, patterns)
        return patterns

    @staticmethod
    def _regex_match_log(content: str, template_patterns: list) -> int:
        """
        统一执行单行日志的正则比对
        返回: 命中模板在列表中的索引 index。若无命中，返回 -1。
        """
        clean_content = str(content).strip()
        for j, pattern in enumerate(template_patterns):
            if pattern.fullmatch(clean_content) is not None:
                return j
        return -1

    def _bucket_constant(self, tokens):
        """桶路由常量：tokens 中第一个**已登记**的常量键，否则 'None'。

        只沿 tokens 自身顺序做集合成员判断（O(len(tokens))），不扫描
        全部常量键——原实现遍历 shallow_tree 所有键找第一个命中，键多时
        每行都是线性开销。语义要点：未登记的 token（通常是变量值）不
        参与路由，避免按高熵变量值分桶导致永不合桶；常量键只由
        _reindex_from_none 与缓存重建登记（模板稳定后的固定 token）。
        """
        for token in tokens:
            if token in self._constant_keys:
                return token
        return 'None'

    def treeSearch(self, seq):
        # ── O(1) 精确命中快路径 ──
        # 完全相同的模板序列直接定位簇，绕过桶内线性扫描（桶达数千簇时
        # 每行 O(桶) 次 seqDist 是吞吐崩塌的根源）。语义与 fastMatch 的
        # 精确匹配分支一致：maxSim=1，new_template 取等值新列表
        # （getTemplate 同样返回新列表——副本避免 logTemplate 与调用方
        # 列表对象别名，防止 updateSequence 原地改写污染模板）。
        exact = self._exact_index.get(tuple(seq))
        if exact is not None:
            return exact, self._bucket_constant(seq), seq[:]

        retLogClust = None

        # Find the frequent word
        constant = self._bucket_constant(seq)

        # Search on the second layer
        seqLen = len(seq)

        logClustL = self.storage_tree.get(constant, {}).get(seqLen)
        if not logClustL:
            return retLogClust, constant, seq

        retLogClust, new_template = self.fastMatch(logClustL, seq)

        return retLogClust, constant, new_template

    def addSeqToPrefixTree(self, logClust):
        seqLen = len(logClust.logTemplate)
        constant = self._bucket_constant(logClust.logTemplate)

        self.storage_tree.setdefault(constant, {}).setdefault(seqLen, []).append(logClust)
        self.shallow_tree.setdefault(constant, []).append(logClust)

        return constant

    def seqDist(self, seq1, seq2):
        """seqDist 记忆化包装：同 (模板, 序列) 对的评分结果直接复用。

        说明为什么用记忆化而不是更细的候选索引来避免扫桶：失配 token
        仍可通过通配覆盖/结构相似度拿到高达 0.9 的部分分，任何"先过滤
        再精算"的候选索引都无法同时保证不漏簇（admissible）——除非改变
        匹配语义。而相同形态的行会反复与同一批簇模板比较，记忆化把
        重复扫描的每步成本降为一次元组哈希，且零行为变化。
        """
        key = (tuple(seq1), tuple(seq2))
        cached = self._seqdist_cache.get(key)
        if cached is not None:
            return cached
        result = self._seqdist_compute(key[0], key[1])
        if len(self._seqdist_cache) >= _CACHE_LIMIT:
            self._seqdist_cache.clear()
        self._seqdist_cache[key] = result
        return result

    @staticmethod
    def _seqdist_compute(seq1, seq2):
        """簇模板与候选序列的 token 级相似度（原实现，被 seqDist 记忆化）。

        token 相等计 1 分；不等时用共享的结构识别给部分分：
        - 同字段名（key:value/key=value）：字段名固定证据 + 值相似度
          （复合标识符/数字链 → 高分；普通词状态值 → 仅字段名分）
        - 非 kv：括号/引号骨架对齐 + 固定文本证据（上限 0.5）
        - 已含 <*> 的混合形式（如 name:xxx_<*>）：覆盖新值 → 0.9，
          否则走结构相似度
        纯 <*> token 维持原语义：不计分母贡献、计入 numOfPara 决胜。
        """
        assert len(seq1) == len(seq2)
        simTokens = 0.0
        numOfPar = 0
        for token1, token2 in zip(seq1, seq2):
            if '<*>' in token1:
                if not any(ch.isalnum() for ch in token1):
                    numOfPar += 1
                    continue
                if token1 == token2:
                    simTokens += 1
                    continue
                if _token_covers(token1, token2):
                    simTokens += 0.9
                else:
                    simTokens += structure_similarity(token1, token2)
                continue
            if token1 == token2:
                simTokens += 1
            else:
                simTokens += structure_similarity(token1, token2)

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
        result = []
        for w1, w2 in zip(seq1, seq2):
            if w1 == w2:
                result.append(w1)
            else:
                result.append(_structure_aware_merge(w1, w2, path_delimiters))
        return result
    
    def jaccardSimilarity(self, template1, template2):
        set1 = self._get_word_set(template1)
        set2 = self._get_word_set(template2)

        if len(set1.union(set2)) == 0: return 0
        return len(set1.intersection(set2))/len(set1.union(set2))

    @staticmethod
    def _get_word_set(template):
        """模板 → 有效词集合（带缓存）。mergeTemplates 对同一模板反复
        计算 split+过滤+建集；模板字符串有限，按字符串缓存。"""
        word_set = _WORD_SET_CACHE.get(template)
        if word_set is None:
            word_set = frozenset(
                item for item in template.split() if any(ch.isalnum() for ch in item))
            if len(_WORD_SET_CACHE) >= _CACHE_LIMIT:
                _WORD_SET_CACHE.clear()
            _WORD_SET_CACHE[template] = word_set
        return word_set

    def mergeTemplates(self, cluster, constant, oldTemplateList=None):
        if oldTemplateList is None:
            oldTemplateList = ()
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
                            cluster._tpl_version = getattr(cluster, '_tpl_version', 0) + 1
                            break
                    # t2 described by t1
                    if len(t1.split()) <= len(t2.split()):
                        if Logcluster.templateMatches(t1, t2):
                            logClust.templateList[j] = t1
                            logClust._tpl_version = getattr(logClust, '_tpl_version', 0) + 1

    def _reindex_from_none(self, cluster):
        """把挂在 'None' 常量下的簇迁移到其首个常量 token 对应的桶。

        触发条件（update_population 阈值或模板变化）由调用方判断；
        迁移包含 shallow_tree['None'] 中含相同常量的其他簇。
        """
        constant = 'None'
        for token in cluster.logTemplate:
            if '<*>' not in token:
                constant = token
                break
        # 首个非 <*> token 登记为常量键（与原 shallow 键语义一致）：
        # 此后含该 token 的行/簇才路由到对应桶
        if constant != 'None':
            self._constant_keys.add(constant)

        # 1. Initialize the buckets（storage/shallow 两树键必须成对创建）
        self.storage_tree.setdefault(constant, {}).setdefault(len(cluster.logTemplate), [])
        self.shallow_tree.setdefault(constant, [])

        # 2. Remove the clusters with the new constant from 'None' in both trees
        # 一次性收集再重建列表（保持原有顺序），替换逐个 list.remove 的
        # O(桶²) 实现——'None' 桶巨大时每次重索引可达秒级~分钟级
        if constant != 'None':
            moved = [cl for cl in self.shallow_tree['None'] if constant in cl.logTemplate]
            if moved:
                moved_set = set(moved)
                for cl in moved:
                    self.shallow_tree[constant].append(cl)
                    self.storage_tree[constant].setdefault(
                        len(cl.logTemplate), []).append(cl)
                self.shallow_tree['None'] = [
                    cl for cl in self.shallow_tree['None'] if cl not in moved_set]
                for seq_len_key in list(self.storage_tree['None'].keys()):
                    bucket = self.storage_tree['None'][seq_len_key]
                    self.storage_tree['None'][seq_len_key] = [
                        cl for cl in bucket if cl not in moved_set]

    def _register_cache_hit(self, cluster, logID, hit_clusters):
        """缓存命中共用维护：hit_time 递增、行号登记、阈值触发常量重索引。

        缓存命中此前完全绕过这些维护，导致 update_population 阈值
        （常量提取重索引）在缓存开启时永不触发。模板分配统一延迟到
        批末输出循环，从最终 templateList 取值，结构性避免过期模板。
        """
        cluster.hit_time += 1
        cluster.logIDL.append(logID)
        hit_clusters.add(cluster)
        if (cluster.hit_time == self.update_population
                and cluster in self.storage_tree.get('None', {}).get(
                    len(cluster.logTemplate), [])):
            self._reindex_from_none(cluster)

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

        # 预初始化结果列表，支持 CRC32 缓存命中时直接写入
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
            # 命中必须做完整正则校验：all(part in raw_content) 的子串捷径
            # 不验证顺序和锚点，不能作为最终覆盖证明。
            cache_hit = False
            if self.use_template_cache:
                light = _light_digit_re.sub('0', content)
                light_crc = zlib.crc32(light.encode('utf-8')) & 0xffffffff
                cached_clusters = self.light_cache.get(light_crc)
                if cached_clusters:
                    raw_content = str(content).strip()
                    for cluster in cached_clusters:
                        templates_c = cluster.templateList
                        template_patterns = self._get_cluster_patterns(cluster)
                        if self._regex_match_log(raw_content, template_patterns) != -1:
                            self._register_cache_hit(cluster, logID, hit_clusters)
                            cache_hits += 1
                            cache_hit = True
                            break
                if cache_hit:
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
            # CRC32 相等不足以认定命中（真实碰撞样例 816545939），
            # 必须用模板对预处理后形式做完整覆盖校验；失败则安全回退完整解析。
            if self.use_template_cache:
                preprocessed_str = ' '.join(logmessageL)
                crc = zlib.crc32(preprocessed_str.encode('utf-8')) & 0xffffffff
                cached_cluster = self.template_cache.get(crc)
                if cached_cluster is not None:
                    if any(Logcluster.templateMatches(t, preprocessed_str)
                           for t in cached_cluster.templateList):
                        # 回填 light_cache：确保同一 light form 的后续行能跳过预处理
                        bucket = self.light_cache.setdefault(light_crc, [])
                        if cached_cluster not in bucket:
                            bucket.append(cached_cluster)
                        self._register_cache_hit(cached_cluster, logID, hit_clusters)
                        cache_hits += 1
                        continue
                    # 校验失败（碰撞或模板已演进）：视为未命中，走完整解析

            matchCluster, first_constant, newTemplate = self.treeSearch(logmessageL)

            # Match no existing log cluster
            if matchCluster is None:
                newCluster = Logcluster(logTemplate=logmessageL, logIDL=[logID], br_thresh=self.br_thresh)
                self.logCluL.append(newCluster)  # 追加至全局类变量
                hit_clusters.add(newCluster)  # 记录本批次命中
                constant = self.addSeqToPrefixTree(newCluster)
                self.mergeTemplates(newCluster, constant)
                self.unsaved_updates += 1  # 变更计数器累加
                # 登记精确模板索引（setdefault：若同键已存在则保留先登记者，
                # 与桶内扫描命中首个同模板簇的语义一致）
                self._exact_index.setdefault(tuple(logmessageL), newCluster)

            # Add the new log message to the existing cluster
            else:
                # Increase the hit time
                matchCluster.hit_time += 1
                oldTemplate = matchCluster.logTemplate
                matchCluster.logTemplate = newTemplate
                # 维护精确模板索引：旧键仅在仍指向本簇时删除，
                # 新键仅在未被占用时登记（保留先登记者）
                old_key = tuple(oldTemplate)
                if self._exact_index.get(old_key) is matchCluster:
                    del self._exact_index[old_key]
                new_key = tuple(newTemplate)
                if new_key not in self._exact_index:
                    self._exact_index[new_key] = matchCluster
                constant = first_constant
                # The template changed, update the list
                oldTemplateList = matchCluster.templateList.copy()
                matchCluster.logIDL.append(logID)
                hit_clusters.add(matchCluster)  # 记录本批次命中
                updated = matchCluster.updateSequence(logmessageL)

                if (
                        oldTemplate != newTemplate or matchCluster.hit_time == self.update_population) and first_constant == 'None':
                    self._reindex_from_none(matchCluster)

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
        # log_templates 已在循环前预初始化，缓存命中的行已填入模板

        for logClust in hit_clusters:
            templates = logClust.templateList

            if len(templates) == 1:
                for logID in logClust.logIDL:
                    if not log_templates[logID - 1]:
                        log_templates[logID - 1] = templates[0]
            else:
                # 调用公共方法生成正则
                template_patterns = self._get_cluster_patterns(logClust)

                for logID in logClust.logIDL:
                    index = logID - 1
                    if log_templates[index]:
                        continue
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

