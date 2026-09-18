#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import math
import numpy as np
import torch
from torch import nn as nn

MAX_PRED = 5
N_LAYERS = 1
N_HEADS = 1
D_MODEL = 64
D_FF = D_MODEL * 4
D_K = D_V = 32
N_SEGMENTS = 2


def gelu(x):
    """Gaussian Error Linear Unit激活函数"""
    return x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))


def get_attn_pad_mask(seq_q):
    """创建注意力掩码，忽略填充位置"""
    batch_size, seq_len = seq_q.size()
    pad_attn_mask = seq_q.data.eq(0).unsqueeze(1)  # [batch_size, 1, seq_len]
    return pad_attn_mask.expand(batch_size, seq_len, seq_len)  # [batch_size, seq_len, seq_len]


class Embedding(nn.Module):
    """词嵌入层（包含位置编码）"""

    def __init__(self, vocab_size, maxlength, device):
        super().__init__()
        self.device = device
        self.tok_embed = nn.Embedding(vocab_size, D_MODEL)
        self.pos_embed = nn.Embedding(maxlength, D_MODEL)
        self.norm = nn.LayerNorm(D_MODEL)

    def forward(self, x):
        seq_len = x.size(1)
        pos = torch.arange(seq_len, dtype=torch.long, device=self.device)
        pos = pos.unsqueeze(0).expand_as(x)
        embedding = self.tok_embed(x) + self.pos_embed(pos)
        return self.norm(embedding)


class ScaledDotProductAttention(nn.Module):
    """缩放点积注意力机制"""

    def __init__(self, stage, device):
        super().__init__()
        self.stage = stage
        self.device = device

    def forward(self, Q, K, V, attn_mask):
        scores = torch.matmul(Q, K.transpose(-1, -2)) / np.sqrt(D_K)
        scores.masked_fill_(attn_mask, -1e9)
        attn = nn.Softmax(dim=-1)(scores)

        if self.stage == "parse":
            return attn.squeeze(dim=0).squeeze(dim=0).detach().cpu().numpy()

        return torch.matmul(attn, V)


class MultiHeadAttention(nn.Module):
    """多头注意力机制"""

    def __init__(self, stage, weight, device):
        super().__init__()
        self.stage = stage
        self.weight = weight
        self.device = device
        self.W_Q = nn.Linear(D_MODEL, D_K * N_HEADS)
        self.W_K = nn.Linear(D_MODEL, D_K * N_HEADS)
        self.W_V = nn.Linear(D_MODEL, D_V * N_HEADS)

    def forward(self, Q, K, V, attn_mask):
        residual, batch_size = Q, Q.size(0)
        q_s = self.W_Q(Q).view(batch_size, -1, N_HEADS, D_K).transpose(1, 2)
        k_s = self.W_K(K).view(batch_size, -1, N_HEADS, D_K).transpose(1, 2)
        v_s = self.W_V(V).view(batch_size, -1, N_HEADS, D_V).transpose(1, 2)

        attn_mask = attn_mask.unsqueeze(1).repeat(1, N_HEADS, 1, 1)

        context = ScaledDotProductAttention(self.stage, self.device)(q_s, k_s, v_s, attn_mask)
        if self.stage == "parse":
            return context

        context = context.transpose(1, 2).contiguous().view(batch_size, -1, N_HEADS * D_V)
        output = nn.Linear(N_HEADS * D_V, D_MODEL).to(self.device)(context)
        return nn.LayerNorm(D_MODEL).to(self.device)(output + self.weight * residual)


class PoswiseFeedForwardNet(nn.Module):
    """位置前馈网络"""

    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(D_MODEL, D_FF)
        self.fc2 = nn.Linear(D_FF, D_MODEL)

    def forward(self, x):
        return self.fc2(gelu(self.fc1(x)))


class EncoderLayer(nn.Module):
    """Transformer编码层"""

    def __init__(self, stage, weight, device):
        super().__init__()
        self.stage = stage
        self.enc_self_attn = MultiHeadAttention(stage, weight, device)
        self.pos_ffn = PoswiseFeedForwardNet()

    def forward(self, enc_inputs, enc_self_attn_mask):
        enc_outputs = self.enc_self_attn(
            enc_inputs, enc_inputs, enc_inputs, enc_self_attn_mask
        )
        if self.stage == "parse":
            return enc_outputs
        return self.pos_ffn(enc_outputs)


class LogNetMini(nn.Module):
    """BERT模型主类"""

    def __init__(self, stage, vocab_size, weight, device, maxlen):
        super().__init__()
        self.stage = stage
        self.device = device
        self.embedding = Embedding(vocab_size, maxlen, device)
        self.layers = nn.ModuleList([
            EncoderLayer(stage, weight, device) for _ in range(N_LAYERS)
        ])

        self.fc = nn.Sequential(
            nn.Linear(D_MODEL, D_MODEL),
            nn.Dropout(0.5),
            nn.Tanh(),
        )
        self.classifier = nn.Linear(D_MODEL, 2)
        self.linear = nn.Linear(D_MODEL, D_MODEL)
        self.activ2 = gelu

        # 权重共享
        self.fc2 = nn.Linear(D_MODEL, vocab_size, bias=False)
        self.fc2.weight = self.embedding.tok_embed.weight

    def forward(self, input_ids, masked_pos=None):
        output = self.embedding(input_ids)
        enc_self_attn_mask = get_attn_pad_mask(input_ids).to(self.device)

        for layer in self.layers:
            output = layer(output, enc_self_attn_mask)
            if self.stage == "parse":
                return output

        if masked_pos is None:
            return output

        # 掩码语言模型任务
        masked_pos = masked_pos[:, :, None].expand(-1, -1, D_MODEL)
        h_masked = torch.gather(output, 1, masked_pos)
        h_masked = self.activ2(self.linear(h_masked))
        return self.fc2(h_masked)


    def resize_token_embeddings(self, new_vocab_size):
        """
        手动扩展 token embedding 层和输出层（fc2），保持原有 token 不变，新增 token 放在后面。

        Args:
            new_vocab_size (int): 新的词表大小
        """
        old_vocab_size = self.embedding.tok_embed.weight.size(0)

        if new_vocab_size == old_vocab_size:
            return  # 无需调整

        if new_vocab_size < old_vocab_size:
            raise ValueError("不支持缩小词表！当前仅支持扩展。")

        # === 1. 扩展 embedding 层 ===
        old_weight = self.embedding.tok_embed.weight.data  # (old_vocab, D_MODEL)
        D_MODEL = old_weight.size(1)

        # 创建新权重矩阵：(new_vocab, D_MODEL)
        new_weight = torch.randn((new_vocab_size, D_MODEL), device=old_weight.device, dtype=old_weight.dtype)
        new_weight[:old_vocab_size] = old_weight  # 保留原有 token

        # 可选：对新增 token 使用正态初始化（更利于训练）
        # torch.nn.init.normal_(new_weight[old_vocab_size:], mean=0.0, std=0.02)

        # 替换 embedding 的 weight
        self.embedding.tok_embed = nn.Embedding(new_vocab_size, D_MODEL, device=self.device)
        self.embedding.tok_embed.weight.data = new_weight

        # === 2. 更新 fc2（输出层）并重新绑定权重 ===
        # 删除旧的 fc2
        del self.fc2

        # 创建新的 fc2，不带 bias
        self.fc2 = nn.Linear(D_MODEL, new_vocab_size, bias=False)

        # 重新绑定：让 fc2.weight 指向 embedding 的 weight（实现权重共享）
        self.fc2.weight = self.embedding.tok_embed.weight

        # === 3. （可选）更新模型内部记录的 vocab_size ===
        # 如果你的 Embedding 类内部有 vocab_size 字段，也应更新
        if hasattr(self.embedding, 'vocab_size'):
            self.embedding.vocab_size = new_vocab_size

        print(f"Resized token embeddings from {old_vocab_size} to {new_vocab_size}.")


    def get_input_embeddings(self):
        return self.embedding.tok_embed


    def save_pretrained(self, path):
        torch.save(self.state_dict(), path + '/model.pth')


def build_lognetmini(stage, vocab_size, weight, device, maxlength):
    """创建BERT模型实例"""
    return LogNetMini(stage, vocab_size, weight, device, maxlength)