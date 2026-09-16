#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.

from collections import defaultdict
from typing import List

from src.anomaly_detection.semantic_anomaly.bert_model.LogNetMini import build_lognetmini
from src.anomaly_detection.semantic_anomaly.data_models import AbnormalLog, AbnormalEvent, AnomalyType, \
    AbnormalLogPerSource, AbnormalEventPerSource
from src.anomaly_detection.semantic_anomaly.utils import format_input, format_output, format_input_one_module, \
    format_output_one_module
import os
# 避免 tokenizers 并行导致 fork 死锁，要加在import transformers之前
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import torch
try:
    import torch_npu
    a = torch.randn(3, 4).npu()
    support_device = 'npu:0'
    print(f'torch_npu version: {torch_npu.__version__}, support_device = {support_device}')
except (ImportError, AttributeError, RuntimeError) as e:
    support_device = "cpu"

import torch.nn.functional as F
from transformers import AutoTokenizer, BertTokenizerFast
import json
import yaml
import re
import numpy as np
from src.data_process.template_extractor.log_template_repository import MixedAggrWindow
from src.data_process.template_extractor.parsing_algorithm import TemplateMiner
from src.anomaly_detection.semantic_anomaly.bert_model.LogNetBig import LogNetBig, LogNetForLogClassification, LogNetUnified


class SemanticAnomalyDetector:

    def __init__(self, case='DME', device='CPU', use_sen_embedd_sim=True, use_sen_embedd_class=False, print_abn_logs=False):
        self.case = case  # 定制化目标场景（目前支持DME、CN）
        self.use_sen_embedd_sim = use_sen_embedd_sim
        self.use_sen_embedd_class = use_sen_embedd_class
        self.print_abn_logs = print_abn_logs
        self.supported_cases = ['DME', 'MAE']
        self.supported_devices = ['CPU', 'NPU']
        if not device.upper() in self.supported_devices:
            raise ValueError(f'Wrong value of device. It must be in {self.supported_devices}')
        else:
            self.device = device.upper()
        if not self.case.upper() in self.supported_cases:
            raise ValueError(f'Wrong value of case. It must be in {self.supported_cases}')
        else:
            self.abn_baseline_path = f'./configurations/abn_baseline_{self.case}_{self.device}.json'
        # 获取当前文件所在路径
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if self.device == 'CPU':  # 仅CPU场景
            self.model_name = 'lognet-mini'
            model_path = os.path.join(base_dir, self.model_name, 'model.pth')  # 本地模型路径
            tokenizer_path = os.path.join(base_dir, self.model_name)
            self.static_tokenizer = BertTokenizerFast.from_pretrained(tokenizer_path)
            device = torch.device('cpu')
            max_len = 512
            self.model = build_lognetmini("embed", len(self.static_tokenizer.vocab), 0.01, device, max_len).to(
                device)  # 实例化模型
            self.model.load_state_dict(torch.load(model_path, map_location=torch.device('cpu')))
            self.static_embedding = self.model.embedding.tok_embed
            self.detect_func = self.detect_abn_logs

        elif self.device == 'NPU':
            self.model_name = 'lognet-big'
            model_path = os.path.join(base_dir, self.model_name)  # 本地模型路径
            self.static_tokenizer = AutoTokenizer.from_pretrained(model_path)
            target_device = torch.device(support_device)
            is_npu_node = bool(re.match(r'^npu:[0-7]$', support_device.lower()))

            # 封装通用加载逻辑
            def setup_model(model_instance, load_classifier=False):
                model_instance.to(target_device)
                if load_classifier:
                    ckpt_path = os.path.join(model_path, 'classifier.pth')
                    model_instance.classifier.load_state_dict(torch.load(ckpt_path, map_location=target_device))
                return model_instance.eval()

            if is_npu_node:
                self.unified_model = setup_model(LogNetUnified(model_path), load_classifier=True)
                self.detect_func = self.detect_abn_logs_npu
            else:
                self.model = LogNetBig.from_pretrained(model_path).to(target_device).eval()
                self.static_embedding = self.model.get_input_embeddings()
                if self.use_sen_embedd_sim:
                    self.contextual_tokenizer = self.static_tokenizer
                    self.contextual_embedding = self.model
                if self.use_sen_embedd_class:
                    self.log_classifier = setup_model(LogNetForLogClassification(model_path), load_classifier=True)
                self.detect_func = self.detect_abn_logs

        # 语义基线
        self.static_threshold = 0.9
        self.contextual_threshold_sim = 0.99
        self.contextual_threshold_class = 0.99
        # 异常语义基线
        with open(os.path.join(base_dir, self.abn_baseline_path), "r", encoding="utf-8") as f:
            baseline = json.load(f)
        self.static_baseline_norm = torch.tensor(np.array(baseline["static_baseline_norm"]), dtype=torch.float32).to(support_device)
        if self.device.upper() == 'NPU' and self.use_sen_embedd_sim:
            self.contextual_baseline_norm = torch.tensor(np.array(baseline["contextual_baseline_norm"]), dtype=torch.float32).to(support_device)


    def detect_one_module(self, windows: dict[int, List[MixedAggrWindow]], module: str, template_cache: TemplateMiner,
                          white_list: List[int]) -> dict[str, []]:
        """
        入参包含单个module在指定时间内的所有的source的数据，具体source数据在window的每条数据中
        Args:
            module: 本次检测的module
            windows: 当前module下所有的日志数据汇聚成的时间窗
            template_cache: template缓存，可以使用template_cache.extractor.id_to_cluster[templateId].get_template()获取模板信息
            white_list: 当前module 对应的白名单信息
        Returns:
             按照source -> data组织的map,包含了module下面的所有source的检测结果
        """
        # 将window_map中白名单部分去除，剩余部分转换成结构化信息（格式转换）
        input_templates = format_input_one_module(windows=windows, template_cache=template_cache, module=module,
                                                  white_list=white_list)
        templates_to_detect = [t.template for t in input_templates]
        # abn_templates = self.detect_abn_log_fuzzy(templates_to_detect)
        abn_templates = self.detect_func(templates_to_detect)

        # 后处理
        full_anomaly_results = defaultdict(list)

        res = defaultdict(list)

        for template_info in input_templates:
            if template_info.template in abn_templates:  # 属于异常模板
                full_anomaly_results[template_info.timestamp].append(
                    AbnormalLog(
                        anomaly_type=AnomalyType.SEMANTIC,
                        template_info=template_info,
                    )
                )
        abnormal_events = [
            AbnormalEvent(abnormal_logs=value, window_start_time=key, score=100)
            for key, value in full_anomaly_results.items()
        ]
        # abnormal_logs
        abnormal_logs = []
        for key, abn_log in full_anomaly_results.items():
            abnormal_logs.extend(abn_log)

        # 按照source返回异常事件
        data = format_output_one_module(abnormal_logs)

        for d in data:
            for source in d['sources']:
                if not source:
                    continue
                res[source].append(d)

        return res

    def detect(self, source, module, k_sigma, window_map, template_cache, white_list_dict, first_only=False):
        """
        轻量级语义异常检测
        """
        # 将window_map中白名单部分去除，剩余部分转换成结构化信息（格式转换）
        input_templates = format_input(window_map=window_map, template_cache=template_cache, source=source,
                                       module=module, white_list=white_list_dict)
        templates_to_detect = [t.template for t in input_templates]
        # abn_templates = self.detect_abn_log_fuzzy(templates_to_detect)
        abn_templates = self.detect_func(templates_to_detect)

        # 后处理
        full_anomaly_results = defaultdict(list)
        abnormal_events = []
        if first_only:
            # 记录需要上template id的首次出现时间及其信息
            template_first_reported = {}
            if abn_templates:
                for template_info in input_templates:
                    if template_info.template in abn_templates:
                        if template_info.template_id not in template_first_reported:
                            template_first_reported[template_info.template_id] = template_info
                        else:
                            if template_info.timestamp < template_first_reported[template_info.template_id].timestamp:
                                template_first_reported[template_info.template_id] = template_info
            if template_first_reported:
                for id, template_info in template_first_reported.items():
                    full_anomaly_results[template_info.timestamp].append(
                        AbnormalLogPerSource(
                            anomaly_type=AnomalyType.SEMANTIC,
                            template_info=template_info,
                        )
                    )
                abnormal_events = [
                    AbnormalEvent(abnormal_logs=value, window_start_time=key, score=100)
                    for key, value in full_anomaly_results.items()
                ]
        else:
            for template_info in input_templates:
                if template_info.template in abn_templates:
                    full_anomaly_results[template_info.timestamp].append(
                        AbnormalLogPerSource(
                            anomaly_type=AnomalyType.SEMANTIC,
                            template_info=template_info,
                        )
                    )
            abnormal_events = [
                AbnormalEventPerSource(abnormal_logs=value, window_start_time=key, score=100)
                for key, value in full_anomaly_results.items()
            ]
        # abnormal_logs
        abnormal_logs = []
        for key, abn_log in full_anomaly_results.items():
            abnormal_logs.extend(abn_log)

        return format_output(abnormal_logs, abnormal_events)

    def detect_abn_logs(self, logs: list):
        """
        对输入的log列表，检测每一行日志（模板）是否存在异常，基于log-net模型
        返回异常日志（模板）
        """
        # logs = list(set(logs))  # 去重，会打乱list顺序
        logs = list(dict.fromkeys(logs))  # 保留顺序
        abn_logs = []
        for log in logs:
            log_en = self.filter_english(log)  # 去除无意义字符
            if not log_en:
                continue
            inputs = self.static_tokenizer(log_en, return_tensors="pt", padding=True)
            if self.model_name == 'lognet-mini':
                static_inputs = inputs["input_ids"][:, :-1]
            else:
                static_inputs = inputs["input_ids"]
            # # token级语义相似度
            static_embeddings = self.static_embed(static_inputs)  # 得到各个token的embedding
            # 归一化
            static_embeddings_norm = F.normalize(static_embeddings, p=2, dim=1)
            # 相似度矩阵计算
            similarity_matrix = torch.matmul(static_embeddings_norm, self.static_baseline_norm.T)
            # 获取最大值相似度值
            max_val, flat_idx = torch.max(similarity_matrix.view(-1), dim=0)  # 1. 获取整个矩阵的最大值和扁平化索引
            if self._is_anomaly(log, max_val, self.static_threshold, "与异常基线相似度"):
                abn_logs.append(log)
                continue

            if self.device.upper() == 'NPU' and self.use_sen_embedd_sim:
                # sentence级语义相似度匹配
                inputs = self.contextual_tokenizer(log_en, return_tensors="pt", padding=True)
                contextual_embeddings = self.contextual_embedding(**inputs).last_hidden_state.squeeze(0)
                # 归一化
                contextual_embedding_norm = F.normalize(contextual_embeddings, p=2, dim=1)
                # 相似度矩阵计算
                similarity_matrix_phrases = torch.matmul(contextual_embedding_norm, self.contextual_baseline_norm.T)
                # 获取最大值相似度值
                max_val_phrases, flat_idx_phrases = torch.max(similarity_matrix_phrases.view(-1),
                                                              dim=0)  # 1. 获取整个矩阵的最大值和扁平化索引
                if self._is_anomaly(log, max_val_phrases, self.contextual_threshold_sim, "contextual相似度"):
                    abn_logs.append(log)
                    continue

            if self.device.upper() == 'NPU' and self.use_sen_embedd_class:
                inputs = self.contextual_tokenizer(log_en, return_tensors="pt", padding=True)
                logits = self.log_classifier(**inputs)
                probs = torch.softmax(logits, dim=-1)[:, 1].detach().cpu().numpy()
                if self._is_anomaly(log, probs[0], self.contextual_threshold_class, "异常置信度"):
                    abn_logs.append(log)
                    continue

        return abn_logs

    def detect_abn_logs_npu(self, logs: list, use_dynamic_buckets=False):
        logs = list(dict.fromkeys(logs))
        if not logs:
            return []

        if not use_dynamic_buckets:
            # print(f'detect {len(logs)} templates into one batch')
            return self.detect_abn_logs_batch(logs)

        # 动态分桶：根据长度自动划分 buckets
        logs_en = [self.filter_english(log) for log in logs]
        if not logs_en:
            return []
        buckets = self.bucketize_logs(logs_en) # 每个桶里面是idx
        # print(f'detect {len(logs)} templates in {len(buckets)} batchs')
        all_results = [None] * len(logs)
        for bucket in buckets:
            sub_logs = [logs[i] for i in bucket]
            # 使用原有的批处理逻辑
            results = self.detect_abn_logs_batch(sub_logs)
            for i, r in zip(bucket, results):
                all_results[i] = r
        return all_results

    def bucketize_logs(self, logs, max_bucket_diff=20):
        # 先 tokenize 一次得到真实token长度（不 padding）
        tokens = self.static_tokenizer(logs, padding=False)
        lengths = [len(x) for x in tokens['input_ids']]

        # 按长度排序索引
        sorted_idx = sorted(range(len(logs)), key=lambda i: lengths[i])

        buckets = []
        current_bucket = []
        last_len = None

        for idx in sorted_idx:
            l = lengths[idx]
            if last_len is None:
                current_bucket = [idx]
                last_len = l
            elif l - last_len <= max_bucket_diff:
                current_bucket.append(idx)
            else:
                buckets.append(current_bucket)
                current_bucket = [idx]
                last_len = l

        if current_bucket:
            buckets.append(current_bucket)

        return buckets


    def detect_abn_logs_batch(self, logs: list):
        """
        对输入的log列表，检测每条日志是否存在异常，基于log-net模型
        支持 batch 推理优化版本
        返回异常日志（模板）
        """
        logs_en = [self.filter_english(log) for log in logs]
        if not logs_en:
            return []

        # 根据当前batch中最长log填充
        inputs_static = self.static_tokenizer(logs_en, return_tensors="pt", padding=True).to(support_device)

        # 填充到固定长度128
        # inputs_static = self.static_tokenizer(logs_en, max_length=128, padding="max_length", truncation=True, return_tensors="pt").to(support_device)

        input_ids_static = inputs_static["input_ids"]
        pad_id = self.static_tokenizer.pad_token_id
        mask = (input_ids_static != pad_id).float() # 有效 token mask

        with torch.no_grad():
            unified_out = self.unified_model(
                input_ids=inputs_static["input_ids"],
                attention_mask=inputs_static["attention_mask"]
            )
            static_raw = unified_out["static_raw"]
            contextual_embeddings = unified_out["contextual"]
            logits = unified_out["logits"]

            # 扩展 embeddings，计算滑动窗口
            static_embeddings, windows_mask = self.static_embed_batch(static_raw, mask)
            # 按 embedding 维度归一化（dim=2），因为 shape 是 [batch, total_windows, emb]
            static_embeddings_norm = F.normalize(static_embeddings, p=2, dim=2)

        # 计算相似度similarity: [batch, total_windows, num_baseline]
        similarity = torch.matmul(static_embeddings_norm, self.static_baseline_norm.T)
        # [batch, total_windows] -> [batch, total_windows, 1]
        mask_exp = windows_mask.unsqueeze(-1)
        # 把无效位置设为极小值
        similarity = similarity * mask_exp + (1.0 - mask_exp) * (-1e9)

        # 取最大相似度
        max_per_window, _ = similarity.max(dim=2)  # [batch, total_windows]
        max_vals_static, _ = max_per_window.max(dim=1)  # [batch]

        # Sentence-level similarity
        max_vals_phrases = []
        if self.use_sen_embedd_sim:
            contextual_embeddings_norm = F.normalize(contextual_embeddings, p=2, dim=-1)
            similarity_matrix_phrases = torch.matmul(contextual_embeddings_norm, self.contextual_baseline_norm.T)
            mask_exp = inputs_static['attention_mask'].unsqueeze(-1)
            similarity_matrix_phrases = similarity_matrix_phrases * mask_exp + (1 - mask_exp) * (-1e9)
            max_vals_phrases, _ = torch.max(similarity_matrix_phrases.view(contextual_embeddings_norm.size(0), -1), dim=1)

        # Sentence-level classifier
        if self.use_sen_embedd_class:
            probs = torch.softmax(logits, dim=-1)[:, 1]

        # 结果判定
        final_mask = torch.zeros(len(logs), dtype=torch.bool, device=support_device)
        final_mask |= (max_vals_static > self.static_threshold)
        if self.use_sen_embedd_sim:
            final_mask |= (max_vals_phrases > self.contextual_threshold_sim)
        if self.use_sen_embedd_class:
            final_mask |= (probs > self.contextual_threshold_class)
        abnormal_indices = final_mask.nonzero().flatten().cpu().tolist()

        if self.print_abn_logs:
            for i in abnormal_indices:
                log = logs[i]
                # 依次检查，超过阈值的都会打印出来
                self._is_anomaly(log, max_vals_static[i], self.static_threshold, "与异常基线相似度")
                if self.use_sen_embedd_sim:
                    self._is_anomaly(log, max_vals_phrases[i], self.contextual_threshold_sim, "contextual相似度")
                if self.use_sen_embedd_class:
                    self._is_anomaly(log, probs[i], self.contextual_threshold_class, "异常置信度")

        return [logs[i] for i in abnormal_indices]

    def get_static_embedding(self, words: str):
        """获取 token 的静态 embedding"""
        ids = self.static_tokenizer(words).data['input_ids'][1:-1]  # 去掉CLS 和 SEQ
        static_embedding = np.mean([self.static_embedding.weight[tid].detach().cpu().numpy() for tid in ids], axis=0)
        return static_embedding

    def get_contextual_embedding(self, text: str):
        """获取文本的上下文 embedding（[CLS]位置输出）"""
        text = self.filter_english(text)
        inputs = self.contextual_tokenizer(text, return_tensors="pt", truncation=False)  # truncation=True
        with torch.no_grad():
            outputs = self.contextual_embedding(**inputs)
            # return outputs.last_hidden_state.mean(dim=1).cpu().numpy()[0]  # 取所有 token embedding 的平均值
            return outputs.last_hidden_state[:, 0, :].cpu().numpy()[0]  # 取[CLS]位置输出

    def static_embed(self, static_inputs):
        static_embeddings = self.static_embedding(static_inputs).squeeze(0).detach()[1:]
        max_key_words_num = 5 if self.device.upper()=='NPU' else 3
        extend_embeddings = []
        for l in range(2, max_key_words_num+1):
            extend_embeddings.extend([torch.mean(static_embeddings[i:i+l], dim=0) for i in range(len(static_embeddings)-l+1)])
        if len(extend_embeddings):
            static_embeddings = torch.cat((static_embeddings, torch.stack(extend_embeddings, dim=0)), dim=0)
        return static_embeddings

    def static_embed_batch(self, static_embeddings, mask):
        """
        将静态嵌入按不同长度的滑动窗口池化后拼接，返回扩展后的嵌入及对应 mask。

        Args:
            static_embeddings (Tensor): shape [batch, seq_len, emb_dim]
            mask (Tensor): shape [batch, seq_len]

        Returns:
            Tensor: shape [batch, total_windows, emb_dim]
            Tensor: shape [batch, total_windows]
        """
        static_embeddings = static_embeddings.detach()
        static_embeddings = static_embeddings[:, 1:, :] # 去掉 CLS token
        mask = mask[:, 1:] # 去掉 CLS token 后的 mask
        batch, seq_len, emb_dim = static_embeddings.shape

        max_key_words_num = 5
        extend_embeddings = []
        extend_masks = []

        emb_t = static_embeddings.transpose(1, 2)  # [batch, emb, seq]
        mask_t = mask.unsqueeze(1)  # [batch, 1, seq]

        for l in range(2, max_key_words_num + 1):
            if seq_len < l:
                # 如果序列比窗口短，跳过（没有产生窗口）
                continue

            sum_pool = F.avg_pool1d(input=emb_t * mask_t, kernel_size=l, stride=1) * l  # [batch, emb, seq']
            counts = F.avg_pool1d(input=mask_t, kernel_size=l, stride=1) * l  # [batch, 1, seq']
            # 防止除以0
            counts = counts.clamp(min=1e-6)

            window_avg = (sum_pool / counts).transpose(1, 2)  # [batch, seq', emb]
            window_mask = (counts.squeeze(1) > 0).float()  # [batch, seq']

            extend_embeddings.append(window_avg)
            extend_masks.append(window_mask)

        # 原始单 token 也作为候选
        single_token_mask = mask  # [batch, seq]
        extend_embeddings = [static_embeddings] + extend_embeddings  # list of [batch, L_i, emb]
        extend_masks = [single_token_mask] + extend_masks  # list of [batch, L_i]

        # 拼接所有扩展特征
        if extend_embeddings:
            static_embeddings_out = torch.cat(extend_embeddings, dim=1)  # [batch, total_windows, emb]
            windows_mask = torch.cat(extend_masks, dim=1)  # [batch, total_windows]
        else:
            static_embeddings_out = static_embeddings
            windows_mask = single_token_mask

        return static_embeddings_out, windows_mask  # mask 用于后续屏蔽

    def _is_anomaly(self, log, score, threshold, label):
        """
        检查是否异常，如果是则打印并返回 True
        """
        val = score.item() if hasattr(score, 'item') else score
        if val > threshold:
            if self.print_abn_logs:
                print(f"异常日志({label}={val:.4f})：{log}")
            return True
        return False

    @staticmethod
    def filter_english_tokens(log: str):
        """
        去掉日志中所有非英文单词，包括数字、标点符号
        返回一个只包含英文单词的列表
        """
        # \b[a-zA-Z]+\b 匹配完整英文单词
        log = log.replace('_', ' ')  # 下划线当做空格，切分子词
        log = log.lower()  # 全部统一为小写
        tokens = re.findall(r'\b[a-zA-Z]+\b', log)
        return tokens

    @staticmethod
    def filter_english(log: str):
        """
        去掉日志中所有非英文单词，包括数字、标点符号
        返回一个只包含英文单词的列表
        """
        # \b[a-zA-Z]+\b 匹配完整英文单词
        log = log.replace('_', ' ')  # 下划线当做空格，切分子词
        log = log.lower()  # 全部统一为小写
        tokens = re.findall(r'\b[a-zA-Z]+\b', log)
        log_en = ' '.join(tokens)
        return log_en

