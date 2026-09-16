from transformers.models.mpnet.modeling_mpnet import (
    MPNetEmbeddings,
    MPNetEncoder,
    MPNetPooler
)
from transformers import PreTrainedModel, PretrainedConfig, AutoConfig, MPNetModel, AutoModel
from torch import nn
from typing import Optional
from torch.nn import CrossEntropyLoss


class LogNetBigConfig(PretrainedConfig):
    model_type = "lognet_big"   # 必须要写！！！
    def __init__(
        self,
        vocab_size=30547,
        hidden_size=768,
        num_hidden_layers=12,
        num_attention_heads=12,
        intermediate_size=3072,
        hidden_act="gelu",
        hidden_dropout_prob=0.1,
        attention_probs_dropout_prob=0.1,
        max_position_embeddings=512,
        initializer_range=0.02,
        layer_norm_eps=1e-12,
        relative_attention_num_buckets=32,
        pad_token_id=1,
        bos_token_id=0,
        eos_token_id=2,
        **kwargs,
    ):
        super().__init__(pad_token_id=pad_token_id, bos_token_id=bos_token_id, eos_token_id=eos_token_id, **kwargs)

        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.hidden_act = hidden_act
        self.intermediate_size = intermediate_size
        self.hidden_dropout_prob = hidden_dropout_prob
        self.attention_probs_dropout_prob = attention_probs_dropout_prob
        self.max_position_embeddings = max_position_embeddings
        self.initializer_range = initializer_range
        self.layer_norm_eps = layer_norm_eps
        self.relative_attention_num_buckets = relative_attention_num_buckets


class LogNetBig(PreTrainedModel):
    config_class = LogNetBigConfig   # 必须要写！！！
    base_model_prefix = "lognet_big" # 推荐（否则名字混乱）

    def __init__(self, config):
        super().__init__(config)

        self.embeddings = MPNetEmbeddings(config)
        self.encoder     = MPNetEncoder(config)
        self.pooler = MPNetPooler(config) if getattr(config, "add_pooling_layer", True) else None

        self.post_init()  # 必须！让权重初始化正确挂载

    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None, position_ids=None, **kwargs):
        """
        Keep the same forward signature as the official model so it's a drop-in replacement.
        We'll implement a minimal forward that matches MPNetModel's behavior:
          embeddings -> encoder -> optionally pooler -> return BaseModelOutputLike
        For simplicity we call encoder(...) exactly as the official code expects.
        """
        # create embeddings
        embedding_output = self.embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
        )
        encoder_outputs = self.encoder(
            embedding_output,
            attention_mask=self.get_extended_attention_mask(attention_mask, input_ids.shape),
            head_mask=self.get_head_mask(kwargs.get("head_mask", None), self.config.num_hidden_layers),
            output_attentions=kwargs.get("output_attentions", False),
            output_hidden_states=kwargs.get("output_hidden_states", False),
            return_dict=kwargs.get("return_dict", True),
        )

        sequence_output = encoder_outputs[0]  # last_hidden_state

        pooled_output = None
        if self.pooler is not None:
            pooled_output = self.pooler(sequence_output)

        # follow transformers' return style
        if kwargs.get("return_dict", True):
            # lazy import for BaseModelOutputWithPooling
            from transformers.modeling_outputs import BaseModelOutputWithPooling
            return BaseModelOutputWithPooling(
                last_hidden_state=sequence_output,
                pooler_output=pooled_output,
                hidden_states=encoder_outputs.hidden_states if hasattr(encoder_outputs, "hidden_states") else None,
                attentions=encoder_outputs.attentions if hasattr(encoder_outputs, "attentions") else None,
            )
        else:
            # tuple: (last_hidden_state, hidden_states?, attentions?)
            out = (sequence_output,)
            if hasattr(encoder_outputs, "hidden_states"):
                out = out + (encoder_outputs.hidden_states,)
            if hasattr(encoder_outputs, "attentions"):
                out = out + (encoder_outputs.attentions,)
            if pooled_output is not None:
                out = out + (pooled_output,)
            return out

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value


def build_lognetbig(pretrained_name="lognet-base", device=None):

    model = LogNetBig.from_pretrained(pretrained_name)
    if device:
        model.to(device)

    return model


class LogNetForLogClassification(nn.Module):
    def __init__(self, model_name: str = "lognet-big", num_labels: int = 2,
                 dropout: float = 0.1, freeze_backbone: bool = True):
        super().__init__()
        # self.backbone = AutoModel.from_pretrained(model_name)  # returns PreTrainedModel (MPNet)
        self.backbone = LogNetBig.from_pretrained(model_name)
        hidden_size = self.backbone.config.hidden_size

        if freeze_backbone:
            for p in self.backbone.parameters():
                p.requires_grad = False
        else:
            for p in self.backbone.parameters():
                p.requires_grad = True

        self.classifier = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, num_labels)
        )

    def forward(self, input_ids, attention_mask):
        # MPNet returns last_hidden_state and pooler / or we can mean pool
        outputs = self.backbone(input_ids=input_ids, attention_mask=attention_mask, return_dict=True)
        last_hidden = outputs.last_hidden_state  # (B, L, H)

        # Simple pooling: mean pooling over valid tokens
        mask = attention_mask.unsqueeze(-1).expand(last_hidden.size()).float()
        summed = (last_hidden * mask).sum(1)
        counts = mask.sum(1).clamp(min=1e-9)
        pooled = summed / counts  # (B, H)

        logits = self.classifier(pooled)  # (B, num_labels)
        return logits

class LogNetUnified(nn.Module):
    def __init__(self, model_name: str = "lognet-big", num_labels: int = 2,
                 dropout: float = 0.1, freeze_backbone: bool = True):
        super().__init__()
        self.backbone = LogNetBig.from_pretrained(model_name)
        self.embedding_layer = self.backbone.get_input_embeddings()
        hidden_size = self.backbone.config.hidden_size

        if freeze_backbone:
            for p in self.backbone.parameters():
                p.requires_grad = False
        else:
            for p in self.backbone.parameters():
                p.requires_grad = True

        self.classifier = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, num_labels)
        )

    def forward(self, input_ids, attention_mask):
        raw_static_emb = self.embedding_layer(input_ids)  # [B, L, H]

        # 一次 backbone forward
        outputs = self.backbone(
            input_ids=input_ids,
            attention_mask=attention_mask,
            return_dict=True
        )
        hidden = outputs.last_hidden_state  # [B, L, H]

        # contextual branch 用 hidden
        contextual_embeddings = hidden

        # classification 用 pooled
        mask = attention_mask.unsqueeze(-1).float()
        summed = (hidden * mask).sum(1)
        pooled = summed / mask.sum(1).clamp(min=1e-9)
        logits = self.classifier(pooled)

        return dict(
            contextual=contextual_embeddings,
            static_raw=raw_static_emb,
            logits=logits
        )
