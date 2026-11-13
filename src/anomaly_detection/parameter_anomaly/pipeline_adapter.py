"""保存首次解析证据和当前正文坐标下的参数，不创建模板解析器。

共享字段保存在 repository 的内存对象上。公共 relation.to_dict()
仍只输出旧业务字段；需要传输完整检测输入时使用 LogEntry.as_dict()。
字段会被同一窗口的后续检测复用，不能在一次检测后自动删除。
"""

from functools import lru_cache
import logging

from .configuration.load_param_config import ParamInfo
from .lexical_matcher import _KNOWN_PH
from .position_mapper import (
    template_tokens_for_alignment, tokenize_for_params_with_spans)
from .slot_anchor import compile_slot_anchor

__all__ = ["attach_first_parse"]
logger = logging.getLogger(__name__)


@lru_cache(maxsize=512)
def _slot_anchors(template_tokens):
    # Cache by token structure: whitespace differences do not consume entries;
    # punctuation and literal differences remain distinct.
    template = " ".join(template_tokens)
    anchors = []
    for i, token in enumerate(template_tokens):
        if any(placeholder in token.lower() for placeholder in _KNOWN_PH):
            anchor = compile_slot_anchor(template, i)
            if anchor is not None:
                anchors.append(anchor)
    return tuple(anchors)


def attach_first_parse(relation, *, template, raw_content,
                       content_tokens=None, token_spans=None):
    """保留旧参数列表，补充能唯一定位的单 token 槽位及共享分词。

    param_list 的位置属于当前正文；多词/子 token 槽位不伪造位置，
    检测器仍可使用同一份正文和 span 按规则定位。
    """
    relation.template = template
    relation.raw_content = raw_content
    if content_tokens is None or token_spans is None:
        content_tokens, token_spans = tokenize_for_params_with_spans(raw_content)
    tokens, spans = content_tokens, token_spans
    relation.content_tokens = tokens
    relation.token_spans = spans
    relation.template_id_namespace = "parser"
    params = {str(p.template_pos): p for p in relation.param_list}
    for anchor in _slot_anchors(tuple(template_tokens_for_alignment(template))):
        slot = anchor.locate(tokens, spans)
        if slot is not None and not slot.sub_token:
            pos = slot.token_index
            existing = params.get(str(pos))
            if existing is not None and str(existing.template_param) != tokens[pos]:
                # Do not log raw log content or parameter values.
                logger.warning(
                    "Shared parameter coordinate conflict: module=%s template_id=%s position=%s",
                    getattr(relation, "module", ""),
                    getattr(relation, "template_id", None), pos)
            params[str(pos)] = ParamInfo(
                template_id=getattr(relation, "template_id", None),
                template_pos=pos, template_param=tokens[pos])
    relation.param_list = list(params.values())
    return relation
