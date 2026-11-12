"""保存首次解析证据和当前正文坐标下的参数，不创建模板解析器。"""

from functools import lru_cache

from .configuration.load_param_config import ParamInfo
from .position_mapper import (
    template_tokens_for_alignment, tokenize_for_params_with_spans)
from .slot_anchor import compile_slot_anchor

__all__ = ["attach_first_parse"]


@lru_cache(maxsize=512)
def _slot_anchors(template):
    anchors = []
    for i, token in enumerate(template_tokens_for_alignment(template)):
        if "<" in token and ">" in token:
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
    for anchor in _slot_anchors(template):
        slot = anchor.locate(tokens, spans)
        if slot is not None and not slot.sub_token:
            pos = slot.token_index
            params[str(pos)] = ParamInfo(
                template_id=getattr(relation, "template_id", None),
                template_pos=pos, template_param=tokens[pos])
    relation.param_list = list(params.values())
    return relation
