#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
LogNetBig MLM 继续预训练（增训）
支持 Accelerate 多卡分布式训练，兼容 Ascend NPU。

用法:
    # 单卡
    python train_mlm_accelerate.py \
        --model_path ../lognet-big-bge-new \
        --data_dir /path/to/log/data \
        --output_dir ../lognet-big-bge-finetuned

    # 多卡 (torch.distributed.launch)
    python -m torch.distributed.launch \
        --nnodes=1 --node_rank=0 --nproc_per_node=8 \
        --master_addr=localhost --master_port=29500 \
        train_mlm_accelerate.py \
        --model_path ../lognet-big-bge-new \
        --data_dir /path/to/log/data \
        --output_dir ../lognet-big-bge-finetuned \
        --per_device_train_batch_size 8
"""
import copy
import json
import os
import sys
import argparse
import glob
import random
import logging
import math
import time

import torch
from torch.utils.data import Dataset, DataLoader
from init_new_token_embedding import init_new_embeddings, init_new_embeddings_distributed

# ============================================================
# NPU 兼容层：必须在 import transformers/accelerate 之前执行
# ============================================================
NPU_AVAILABLE = False
try:
    import torch_npu  # noqa: F401
    from torch_npu.contrib import transfer_to_npu  # 将 torch.cuda 调用重定向到 npu
    NPU_AVAILABLE = True
    logging.info("torch_npu detected, NPU mode enabled.")
except ImportError:
    pass

from transformers import (
    AutoTokenizer,
    AutoModel,
    AutoModelForMaskedLM,
    DataCollatorForLanguageModeling,
    get_linear_schedule_with_warmup,
)

try:
    from accelerate import Accelerator
except ImportError:
    raise ImportError("请安装 accelerate: pip install accelerate")

# ============================================================
# 路径常量
# ============================================================
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)

logger = logging.getLogger(__name__)


# ============================================================
# LogDataset — 日志数据集（延迟 tokenize + 批量预 tokenize）
# ============================================================
class LogDataset(Dataset):
    """
    从目录加载所有 .log/.txt 文件，每行一条日志。

    两种模式:
      - lazy (默认): 仅存储原始文本，__getitem__ 时 tokenize。
        初始化极快，但训练时每个 step 有 tokenize 开销。
        DataCollatorForLanguageModeling 会动态做 MLM mask + padding，
        所以 __getitem__ 只需返回单条文本即可。
      - eager: 初始化时批量 tokenize 并缓存结果。
        初始化较慢（但比逐行 tokenize 快 20-50x），
        训练时无 tokenize 开销，整体更快。

    推荐策略: 数据量 < 500 万行用 eager，否则用 lazy。
    """

    def __init__(self, lines, tokenizer, max_length, mode="lazy"):
        """
        Args:
            lines: 文本行列表（已过滤非空行）。
            tokenizer: 分词器。
            max_length: 最大序列长度。
            mode: "lazy" 延迟 tokenize, "eager" 批量预 tokenize。
        """
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.mode = mode
        self.samples = []   # lazy 模式存 str，eager 模式存 dict

        # 直接使用传入的行列表
        raw_lines = lines

        total_lines = len(raw_lines)
        logger.info(f"LogDataset: received {total_lines} non-empty lines.")

        if mode == "eager":
            # ---- 批量 tokenize (每批 4096 条，比逐行快 20-50x) ----
            batch_size = 4096
            skipped = 0
            for start in range(0, len(raw_lines), batch_size):
                batch = raw_lines[start : start + batch_size]
                encoded_batch = tokenizer(
                    batch,
                    truncation=True,
                    max_length=max_length,
                    padding=False,
                    return_attention_mask=True,
                )
                for i in range(len(batch)):
                    ids = encoded_batch["input_ids"][i]
                    if len(ids) <= 2:  # 过滤只有 special tokens 的
                        skipped += 1
                        continue
                    self.samples.append({
                        "input_ids": ids,
                        "attention_mask": encoded_batch["attention_mask"][i],
                    })

            logger.info(
                f"LogDataset (eager): {len(self.samples)} samples tokenized, "
                f"{total_lines} total lines, {skipped} skipped (too short)."
            )

        else:
            # ---- lazy: 直接存原始文本 ----
            self.samples = raw_lines
            logger.info(
                f"LogDataset (lazy): {len(self.samples)} lines stored as raw text, "
                f"tokenization deferred to __getitem__."
            )

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        if self.mode == "eager":
            return self.samples[idx]
        else:
            # lazy: 运行时 tokenize 单条
            text = self.samples[idx]
            encoded = self.tokenizer(
                text,
                truncation=True,
                max_length=self.max_length,
                padding=False,
                return_attention_mask=True,
            )
            return {
                "input_ids": encoded["input_ids"],
                "attention_mask": encoded["attention_mask"],
            }


# ============================================================
# 工具函数
# ============================================================
def find_log_files(data_dir):
    """递归搜索目录下所有 .log 和 .txt 文件。"""
    log_files = glob.glob(os.path.join(data_dir, "**", "*.log"), recursive=True)
    txt_files = glob.glob(os.path.join(data_dir, "**", "*.txt"), recursive=True)
    all_files = sorted(set(log_files + txt_files))
    # 过滤空文件
    non_empty = []
    for f in all_files:
        try:
            if os.path.getsize(f) > 0:
                non_empty.append(f)
        except OSError:
            continue
    return non_empty


def read_all_lines(file_paths):
    """读取所有文件中的非空行，返回列表。"""
    raw_lines = []
    for fpath in file_paths:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    raw_lines.append(line)
    return raw_lines


def split_lines(all_lines, val_split, seed):
    """行级别划分训练/验证集。固定 seed 保证各 rank 一致。"""
    rng = random.Random(seed)
    shuffled = list(all_lines)
    rng.shuffle(shuffled)
    split_idx = max(1, int(len(shuffled) * (1 - val_split)))
    train_lines = shuffled[:split_idx]
    eval_lines = shuffled[split_idx:]
    return train_lines, eval_lines


@torch.no_grad()
def evaluate(model, eval_dataloader, accelerator, args):
    """评估验证集 loss。"""
    model.eval()
    losses = []
    for batch in eval_dataloader:
        outputs = model(**batch)
        loss = outputs.loss
        # gather 所有 rank 的 loss
        gathered = accelerator.gather(loss.repeat(args.per_device_eval_batch_size))
        losses.append(gathered.mean().item())

    model.train()
    if not losses:
        return float("inf")
    return sum(losses) / len(losses)


def save_checkpoint(model, tokenizer, output_dir, accelerator, step=None, is_best=False, save_dpc=True):
    """保存检查点。仅主进程保存。"""
    if not accelerator.is_main_process:
        return
    dpc_output_dir = r"/dpc/liangzhengdong/lognet_output"
    if is_best:
        save_dir = os.path.join(output_dir, "best_model")
        dpc_save_dir = os.path.join(dpc_output_dir, "best_model")
    elif step is not None:
        save_dir = os.path.join(output_dir, f"checkpoint-{step}")
        dpc_save_dir = os.path.join(dpc_output_dir, f"checkpoint-{step}")
    else:
        save_dir = os.path.join(output_dir, "final_model")
        dpc_save_dir = os.path.join(dpc_output_dir, f"final_model")

    os.makedirs(save_dir, exist_ok=True)

    # 解开 DDP 包装
    unwrapped_model = accelerator.unwrap_model(model)
    unwrapped_model.save_pretrained(save_dir, safe_serialization=True)
    tokenizer.save_pretrained(save_dir)

    accelerator.print(f"[Save] Model saved to {save_dir}")
    if save_dpc:
        unwrapped_model.save_pretrained(dpc_save_dir, safe_serialization=True)
        tokenizer.save_pretrained(dpc_save_dir)
        accelerator.print(f"[Save] Model saved to DPC {dpc_save_dir}")

    # # ---- 保存后验证：确认关键文件已落盘 ----
    # expected_files = ["config.json", "tokenizer_config.json"]
    # safetensors_file = "model.safetensors"
    # bin_file = "pytorch_model.bin"
    # # 模型权重文件至少存在一种
    # has_weight = False
    # for wf in [safetensors_file, bin_file]:
    #     wf_path = os.path.join(save_dir, wf)
    #     if os.path.exists(wf_path):
    #         has_weight = True
    #         expected_files.append(wf)
    #         break
    #
    # if has_weight:
    #     expected_files.append(safetensors_file if os.path.exists(os.path.join(save_dir, safetensors_file)) else bin_file)
    #
    # all_exist = True
    # for fname in expected_files:
    #     fpath = os.path.join(save_dir, fname)
    #     if os.path.exists(fpath):
    #         fsize = os.path.getsize(fpath)
    #         if fsize >= 1024 * 1024:
    #             size_str = f"{fsize / (1024 * 1024):.2f} MB"
    #         else:
    #             size_str = f"{fsize / 1024:.2f} KB"
    #         accelerator.print(f"  [Verify] ✅ {fname} ({size_str})")
    #     else:
    #         accelerator.print(f"  [Verify] ❌ {fname} MISSING!")
    #         all_exist = False
    #
    # if all_exist:
    #     # 列出目录下所有文件作为最终确认
    #     saved_files = os.listdir(save_dir)
    #     accelerator.print(f"  [Verify] 目录 {save_dir} 共 {len(saved_files)} 个文件: {saved_files}")
    # else:
    #     accelerator.print(f"  [Verify] ⚠️ 检查点保存不完整，请检查磁盘空间和权限！")


# ============================================================
# 参数解析
# ============================================================
def parse_args():
    parser = argparse.ArgumentParser(description="LogNetBig MLM Continued Pretraining with Accelerate")

    # 数据与模型路径
    parser.add_argument("--model_path", type=str, required=True,
                        help="预训练模型目录 (如 ../lognet-big-bge-new)")
    parser.add_argument("--data_dir", type=str, required=True,
                        help="日志数据目录，自动搜索 .log/.txt 文件")
    parser.add_argument("--output_dir", type=str, required=True,
                        help="模型输出目录")
    parser.add_argument("--base_model_folder_name", type=str, required=True,
                        help="模型输出目录")

    # 训练超参
    parser.add_argument("--per_device_train_batch_size", type=int, default=8,
                        help="每卡训练 batch size")
    parser.add_argument("--per_device_eval_batch_size", type=int, default=16,
                        help="每卡评估 batch size")
    parser.add_argument("--epochs", type=int, default=3,
                        help="训练轮数")
    parser.add_argument("--learning_rate", type=float, default=5e-6,
                        help="学习率（增训默认5e-6，远小于从头训练的2e-5~5e-5）")
    parser.add_argument("--weight_decay", type=float, default=0.1,
                        help="权重衰减（增训默认0.1，比从头训练更大以约束参数变化）")
    parser.add_argument("--warmup_ratio", type=float, default=0.1,
                        help="学习率预热比例")
    parser.add_argument("--max_seq_length", type=int, default=128,
                        help="最大序列长度")
    parser.add_argument("--mlm_probability", type=float, default=0.15,
                        help="MLM 掩码概率")

    # 梯度与显存优化
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1,
                        help="梯度累积步数")
    parser.add_argument("--gradient_checkpointing", action="store_true",
                        help="启用梯度检查点（省显存，增加计算）")
    parser.add_argument("--max_grad_norm", type=float, default=1.0,
                        help="梯度裁剪阈值")
    parser.add_argument("--freeze_base_model", action="store_false", default=True,
                        help="冻结base model，只训练lm_head（含embedding新token部分）")
    parser.add_argument("--skip_training", action="store_true",
                        help="跳过增训，直接保存扩展词表后的模型并退出")

    # 保存与评估
    parser.add_argument("--save_steps", type=int, default=200000000,
                        help="保存检查点间隔步数")
    parser.add_argument("--eval_steps", type=int, default=500,
                        help="评估间隔步数")
    parser.add_argument("--logging_steps", type=int, default=100,
                        help="日志打印间隔步数")
    parser.add_argument("--save_total_limit", type=int, default=3,
                        help="最多保存的检查点数量（不含 best_model）")

    # 数据划分
    parser.add_argument("--validation_split", type=float, default=0.02,
                        help="验证集比例（行级别划分）")
    parser.add_argument("--data_sample_ratio", type=float, default=0.002,
                        help="使用数据量的比例(0~1)，用于调试流程时快速跑通，1.0=使用全部数据")

    # 恢复训练
    parser.add_argument("--resume_from_checkpoint", type=str, default=None,
                        help="从检查点恢复训练的路径")

    # 杂项
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--dataloader_num_workers", type=int, default=0,
                        help="DataLoader worker 数量")
    parser.add_argument("--dataloader_pin_memory", action="store_true",
                        help="DataLoader pin_memory (NPU 下不要开启)")
    parser.add_argument("--dataset_mode", type=str, default="lazy",
                        choices=["lazy", "eager"],
                        help="数据集加载模式: lazy=延迟tokenize(初始化快), eager=批量预tokenize(训练快)")
    parser.add_argument("--overwrite_output_dir", action="store_true",
                        help="覆盖输出目录")

    # 兼容用户 shell 脚本的参数（实际不使用，仅做接口兼容）
    parser.add_argument("--use_common", type=int, default=0,
                        help="兼容参数: 是否使用通用数据 (1/0)")
    parser.add_argument("--random_mask", type=int, default=0,
                        help="兼容参数: 随机重建掩码 (1/0)")
    parser.add_argument("--former", type=str, default="encoder",
                        help="兼容参数: encoder/decoder 架构")
    parser.add_argument("--platform", type=str, default="TIGE",
                        help="兼容参数: 训练平台标识")

    return parser.parse_args()


# ============================================================
# 主函数
# ============================================================
def main():
    args = parse_args()
    base_model_path = args.model_path
    base_model_folder_name = args.base_model_folder_name

    # --------------------------------------------------------
    # 1. 初始化 Accelerator（必须先于 init_new_embeddings_distributed，
    #    因为分布式 embedding 初始化依赖 dist 进程组）
    # --------------------------------------------------------
    mixed_precision = "no"
    if NPU_AVAILABLE:
        mixed_precision = "bf16"  # Ascend 910 原生支持 bf16
        logger.info("NPU detected, using bf16 mixed precision.")

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=mixed_precision,
        log_with=None,  # 可改为 "tensorboard" 或 "wandb"
        project_dir=args.output_dir,
    )

    # 配置日志
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO if accelerator.is_main_process else logging.WARNING,
    )

    accelerator.print("=" * 60)
    accelerator.print("LogNetBig MLM Continued Pretraining")
    accelerator.print("=" * 60)
    accelerator.print(f"Model path    : {args.model_path}")
    accelerator.print(f"Data dir      : {args.data_dir}")
    accelerator.print(f"Output dir    : {args.output_dir}")
    accelerator.print(f"NPU available : {NPU_AVAILABLE}")
    accelerator.print(f"Mixed precision: {mixed_precision}")
    accelerator.print(f"Num processes : {accelerator.num_processes}")
    accelerator.print(f"Device        : {accelerator.device}")

    # --------------------------------------------------------
    # 2. 词表扩展（所有 rank 执行相同操作，保证 tokenizer 一致）
    # --------------------------------------------------------
    tokenizer = AutoTokenizer.from_pretrained(base_model_path)
    accelerator.print(f"原始词表大小: {len(tokenizer)}")

    # 保留原有模型和分词器状态，用以后续初始化 Embedding
    old_tokenizer = copy.deepcopy(tokenizer)

    add_tokens_path = r'domain_tokens.json'
    with open(add_tokens_path, 'r', encoding='utf-8') as f:
        add_tokens = json.load(f)
    domain_tokens = []
    for token in add_tokens.keys():
        domain_tokens.append(token)

    # 过滤出不在原词表的"新词"
    existing_vocab = set(tokenizer._tokenizer.get_vocab().keys())
    new_tokens = [t for t in domain_tokens if t not in existing_vocab]
    new_tokens = [t.lower() for t in new_tokens]
    old_vocab_size = len(tokenizer)
    tokenizer.add_tokens(new_tokens)
    new_vocab_size = len(tokenizer)
    accelerator.print(f"成功新增 {new_vocab_size - old_vocab_size} 个新token({old_vocab_size}-->{new_vocab_size})")

    # 词表扩增结果校验
    for word in new_tokens:
        if not tokenizer.tokenize(word)[0] == word.lower():
            raise ValueError(f"新增词{word}校验失败，原因为：{tokenizer.tokenize(word)}")

    # --------------------------------------------------------
    # 3. 加载模型 & 分布式初始化新 token Embedding
    # 直接使用 AutoModel 加载原始模型，保持权重命名与 base_model 一致
    # --------------------------------------------------------
    accelerator.print(f"使用 AutoModel 加载原始模型 (from {base_model_path})...")
    model = AutoModel.from_pretrained(base_model_path)
    accelerator.print(
        f"原始模型加载完成: {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M parameters"
    )

    old_model = copy.deepcopy(model)

    model.resize_token_embeddings(len(tokenizer))
    accelerator.print("Embedding矩阵扩展完成 ✅")

    # 使用分布式版本初始化新 token embedding
    # 当 world_size=1 时自动退化为单卡版本
    accelerator.print(
        f"开始分布式 Embedding 初始化 "
        f"(world_size={accelerator.num_processes})..."
    )

    new_model, report = init_new_embeddings_distributed(
        old_model=old_model,
        old_tokenizer=old_tokenizer,
        new_model=model,
        new_tokenizer=tokenizer,
        new_tokens=new_tokens,
        pca_rank=128,
        alpha=0.3,
        pooling="weighted_mean",
        norm_statistic="median",
        device="npu",
    )

    # 保存新的分词器与模型（仅主进程，仅作备份/记录）
    # 多机场景下，只有 rank 0 所在节点会保存此文件，其他节点无需访问。
    if accelerator.is_main_process:
        save_dir = os.path.join(os.path.dirname(current_dir), base_model_folder_name + '-new')
        os.makedirs(save_dir, exist_ok=True)
        tokenizer.save_pretrained(save_dir)
        new_model.save_pretrained(save_dir)
        print(f"已保存到: {save_dir}")

    # --------------------------------------------------------
    # 3.5 跳过增训模式：只做词表扩展 + embedding 初始化，不训练
    # --------------------------------------------------------
    if args.skip_training:
        accelerator.print("\n" + "=" * 60)
        accelerator.print("skip_training=True: 跳过增训，直接输出扩展词表后的模型")
        accelerator.print("=" * 60)

        # 同时保存为 MLM 模型格式（含 lm_head），方便后续直接加载训练
        if accelerator.is_main_process:
            mlm_save_dir = "/dpc/liangzhengdong/lognet_output/tiny_model"
            os.makedirs(mlm_save_dir, exist_ok=True)

            # 构建 MLM 模型并保存
            try:
                mlm_model = AutoModelForMaskedLM.from_pretrained(base_model_path)
            except Exception:
                mlm_model = AutoModelForMaskedLM.from_config(new_model.config)

            base_attr = mlm_model.base_model_prefix
            base_sub = getattr(mlm_model, base_attr)
            if mlm_model.get_input_embeddings().weight.shape[0] != len(tokenizer):
                mlm_model.resize_token_embeddings(len(tokenizer))
            base_sub.load_state_dict(new_model.state_dict(), strict=False)

            mlm_model.save_pretrained(mlm_save_dir, safe_serialization=True)
            tokenizer.save_pretrained(mlm_save_dir)
            accelerator.print(f"MLM 模型已保存到: {mlm_save_dir}")

        accelerator.wait_for_everyone()
        if torch.distributed.is_initialized():
            torch.distributed.destroy_process_group()
        return

    # --------------------------------------------------------
    # 4. 设置随机种子
    # --------------------------------------------------------
    seed = args.seed + accelerator.process_index  # 不同 rank 不同种子，避免数据重复
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    # --------------------------------------------------------
    # 5. 检查输出目录
    # --------------------------------------------------------
    if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
        if not args.overwrite_output_dir:
            accelerator.print(
                f"WARNING: Output directory {args.output_dir} is not empty. "
                f"Use --overwrite_output_dir to overwrite."
            )

    os.makedirs(args.output_dir, exist_ok=True)

    # --------------------------------------------------------
    # 6. 发现文件、读取所有行、行级别划分训练/验证集
    # --------------------------------------------------------
    all_files = find_log_files(args.data_dir)
    if not all_files:
        raise FileNotFoundError(
            f"在 {args.data_dir} 中未找到 .log 或 .txt 文件"
        )

    accelerator.print(f"发现 {len(all_files)} 个日志文件，开始读取所有行...")
    all_lines = read_all_lines(all_files)
    accelerator.print(f"共读取 {len(all_lines)} 条非空日志行")

    # 按 data_sample_ratio 采样（调试时可用少量数据快速跑通流程）
    if args.data_sample_ratio < 1.0:
        rng = random.Random(args.seed)
        sampled_count = max(1, int(len(all_lines) * args.data_sample_ratio))
        all_lines = rng.sample(all_lines, sampled_count)
        accelerator.print(f"data_sample_ratio={args.data_sample_ratio}, 采样后: {len(all_lines)} 条")

    # 行级别划分（使用固定 seed 保证各 rank 一致）
    train_lines, eval_lines = split_lines(all_lines, args.validation_split, args.seed)
    accelerator.print(f"训练集行数: {len(train_lines)}, 验证集行数: {len(eval_lines)}")

    # --------------------------------------------------------
    # 7. 构建训练模型（直接从内存构建，兼容多机多卡）
    # 使用 AutoModelForMaskedLM，保持权重命名与 base_model 一致
    # --------------------------------------------------------
    if args.resume_from_checkpoint:
        accelerator.print(f"从检查点恢复: {args.resume_from_checkpoint}")
        tokenizer = AutoTokenizer.from_pretrained(args.resume_from_checkpoint)
        model = AutoModelForMaskedLM.from_pretrained(args.resume_from_checkpoint)
    else:
        accelerator.print("从内存中构建 MLM 训练模型...")

        # 策略：先用 from_pretrained 加载 base_model_path 中所有能加载的权重
        # （包括 lm_head，如果 checkpoint 里有的话），然后再用 new_model 的
        # 扩展后 embedding 覆盖。这样 lm_head.dense/layer_norm/bias 也来自
        # 原模型，而不是随机初始化，收敛更快。
        try:
            accelerator.print(f"  尝试 AutoModelForMaskedLM.from_pretrained({base_model_path})...")
            model = AutoModelForMaskedLM.from_pretrained(base_model_path)
            accelerator.print("  ✅ 成功加载完整 MLM 模型（含 lm_head）")
        except Exception as e:
            accelerator.print(f"  ⚠️ from_pretrained 失败: {e}")
            accelerator.print("  退回 from_config + 手动加载 base model 权重")
            config = new_model.config
            model = AutoModelForMaskedLM.from_config(config)

        # 获取 base model 属性名（如 model.roberta, model.bert 等）
        base_model_attr = model.base_model_prefix  # e.g. "roberta", "bert"
        base_submodel = getattr(model, base_model_attr)

        # 先 resize embedding，使词表大小与 new_model 一致
        # 否则 load_state_dict 会因 embedding 形状不匹配报错
        # （from_pretrained 加载的是原始词表大小，new_model 已扩展）
        if model.get_input_embeddings().weight.shape[0] != len(tokenizer):
            model.resize_token_embeddings(len(tokenizer))
            accelerator.print(
                f"Embedding resize: {model.get_input_embeddings().weight.shape[0]} → {len(tokenizer)}"
            )

        # 将 new_model 的扩展后 embedding + encoder 权重复制到训练模型
        # 这会覆盖 from_pretrained 加载的旧 embedding（词表更小），
        # 替换为 resize 后的扩展 embedding（词表更大 + 新 token 已初始化）
        new_state_dict = new_model.state_dict()
        missing, unexpected = base_submodel.load_state_dict(
            new_state_dict, strict=False
        )

        # ---- 权重加载诊断 ----
        total_keys = len(new_state_dict)
        loaded_keys = total_keys - len(unexpected)
        accelerator.print(
            f"权重加载诊断: base_model_prefix={base_model_attr}, "
            f"源模型{total_keys}个key, 成功加载{loaded_keys}个, "
            f"跳过{len(unexpected)}个, 缺失{len(missing)}个"
        )
        if unexpected:
            accelerator.print(f"  跳过的key (如pooler, 非训练必需): {unexpected[:5]}")
        if missing:
            accelerator.print(f"  ⚠️ 缺失的key (本应加载但未匹配): {missing[:5]}")

        # 验证关键权重确实来自原模型：对比第一层 attention 的 weight
        sample_key = [k for k in new_state_dict if "encoder.layer.0" in k and "weight" in k]
        if sample_key:
            k = sample_key[0]
            src_val = new_state_dict[k]
            dst_val = base_submodel.state_dict()[k]
            max_diff = (src_val.cpu() - dst_val.cpu()).abs().max().item()
            accelerator.print(
                f"  权重校验 ({k}): max_diff={max_diff:.2e} "
                f"{'✅ 一致' if max_diff < 1e-6 else '❌ 不一致！'}"
            )

        # 检查 lm_head 权重来源
        lm_head_keys = [n for n, _ in model.named_parameters() if "lm_head" in n]
        lm_head_random = [n for n in lm_head_keys
                          if "decoder" not in n]  # decoder.weight 与 embedding 共享，不算随机
        accelerator.print(
            f"  lm_head 参数: {len(lm_head_keys)} 个, "
            f"其中随机初始化: {len(lm_head_random)} 个 ({lm_head_random[:3]})"
        )

        accelerator.print(
            f"MLM 模型构建完成 (base: {base_model_attr}): "
            f"{sum(p.numel() for p in model.parameters()) / 1e6:.1f}M parameters"
        )

    # 梯度检查点
    if args.gradient_checkpointing:
        base_submodel = getattr(model, model.base_model_prefix)
        base_submodel.gradient_checkpointing_enable()
        accelerator.print("Gradient checkpointing enabled.")

    # 冻结 base model，只训练 lm_head + embedding
    if args.freeze_base_model:
        base_submodel = getattr(model, model.base_model_prefix)
        # 冻结 encoder 所有层
        for param in base_submodel.encoder.parameters():
            param.requires_grad = False
        # 冻结 position embedding 和 token type embedding（不属于新 token 部分）
        for name, param in base_submodel.embeddings.named_parameters():
            if "word_embeddings" not in name:
                param.requires_grad = False
        # word_embeddings 保持可训练（含新增 token），让新 token embedding 在训练中适配
        # 由于 weight tying，lm_head.decoder.weight 也保持可训练

        trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        total_params = sum(p.numel() for p in model.parameters())
        accelerator.print(
            f"freeze_base_model=True: encoder + position/token_type embedding 已冻结, "
            f"训练 lm_head + word_embeddings (含新旧token)"
        )
        accelerator.print(
            f"可训练参数: {trainable_params / 1e6:.2f}M / {total_params / 1e6:.1f}M "
            f"({trainable_params / total_params * 100:.2f}%)"
        )
    else:
        accelerator.print("freeze_base_model=False: 全部参数参与训练")

    accelerator.print(
        f"Model params: {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M total, "
        f"{sum(p.numel() for p in model.parameters() if p.requires_grad) / 1e6:.1f}M trainable"
    )

    # --------------------------------------------------------
    # 8. 构建数据集
    # --------------------------------------------------------
    accelerator.print("构建训练数据集...")
    train_dataset = LogDataset(train_lines, tokenizer, args.max_seq_length, mode=args.dataset_mode)
    accelerator.print(f"训练集样本数: {len(train_dataset)}")

    eval_dataset = None
    if eval_lines:
        accelerator.print("构建验证数据集...")
        eval_dataset = LogDataset(eval_lines, tokenizer, args.max_seq_length, mode="eager")
        accelerator.print(f"验证集样本数: {len(eval_dataset)}")

    # --------------------------------------------------------
    # 9. DataLoader + DataCollator
    # --------------------------------------------------------
    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer,
        mlm=True,
        mlm_probability=args.mlm_probability,
    )

    pin_memory = args.dataloader_pin_memory and not NPU_AVAILABLE

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=args.per_device_train_batch_size,
        shuffle=True,
        collate_fn=data_collator,
        num_workers=args.dataloader_num_workers,
        pin_memory=pin_memory,
        drop_last=True,
    )

    eval_dataloader = None
    if eval_dataset is not None:
        eval_dataloader = DataLoader(
            eval_dataset,
            batch_size=args.per_device_eval_batch_size,
            shuffle=False,
            collate_fn=data_collator,
            num_workers=args.dataloader_num_workers,
            pin_memory=pin_memory,
        )

    # --------------------------------------------------------
    # 10. Optimizer + Scheduler
    # --------------------------------------------------------
    # 区分需要 weight decay 和不需要的参数
    no_decay = ["bias", "LayerNorm.weight", "layer_norm.weight"]
    optimizer_grouped_params = [
        {
            "params": [
                p for n, p in model.named_parameters()
                if not any(nd in n for nd in no_decay) and p.requires_grad
            ],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [
                p for n, p in model.named_parameters()
                if any(nd in n for nd in no_decay) and p.requires_grad
            ],
            "weight_decay": 0.0,
        },
    ]

    optimizer = torch.optim.AdamW(
        optimizer_grouped_params,
        lr=args.learning_rate,
        betas=(0.9, 0.999),
        eps=1e-8,
    )

    # 注意: scheduler 不在此处创建，而是在 accelerator.prepare() 之后创建。
    # 原因: get_linear_schedule_with_warmup 内部的 lr_lambda 是闭包，
    #       创建时就捕获了 num_training_steps 的值，事后修改属性无法生效。
    #       必须在 prepare() 之后拿到精确的 dataloader 长度，才能创建正确的 scheduler。

    # --------------------------------------------------------
    # 11. Accelerate prepare（不包含 scheduler，scheduler 在之后重建）
    # --------------------------------------------------------
    if eval_dataloader is not None:
        model, optimizer, train_dataloader, eval_dataloader = accelerator.prepare(
            model, optimizer, train_dataloader, eval_dataloader
        )
    else:
        model, optimizer, train_dataloader = accelerator.prepare(
            model, optimizer, train_dataloader
        )

    # --------------------------------------------------------
    # 计算总步数（必须在 prepare 之后，此时 DataLoader 已替换为 DistributedSampler，
    # len() 才是每个 rank 实际看到的 batch 数）
    # --------------------------------------------------------
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    max_train_steps = num_update_steps_per_epoch * args.epochs
    warmup_steps = int(max_train_steps * args.warmup_ratio)

    # 在 prepare 之后重建 scheduler，使用精确的总步数
    # 这样 lr_lambda 闭包捕获的是正确的 num_training_steps 值
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=warmup_steps,
        num_training_steps=max_train_steps,
    )

    accelerator.print(f"每 epoch 更新步数: {num_update_steps_per_epoch}")
    accelerator.print(f"总训练步数: {max_train_steps}, warmup 步数: {warmup_steps}")
    accelerator.print(
        f"有效全局 batch size: "
        f"{args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps}"
    )

    # --------------------------------------------------------
    # 12. 训练循环
    # --------------------------------------------------------
    accelerator.print("\n" + "=" * 60)
    accelerator.print("开始训练")
    accelerator.print("=" * 60)

    total_step = 0
    best_eval_loss = float("inf")
    completed_epochs = 0
    checkpoints_saved = []  # 记录保存的检查点路径
    start_time = time.time()

    for epoch in range(args.epochs):
        model.train()
        epoch_loss = 0.0
        epoch_steps = 0

        for step, batch in enumerate(train_dataloader):
            with accelerator.accumulate(model):
                outputs = model(**batch)
                loss = outputs.loss

                # 记录 loss（gather 前的值用于日志）
                avg_loss = accelerator.gather(loss.repeat(args.per_device_train_batch_size)).mean().item()
                epoch_loss += avg_loss
                epoch_steps += 1

                accelerator.backward(loss)

                # 梯度裁剪 + 参数更新（仅在梯度同步时执行，即一个累积周期结束）
                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()

            # total_step 计数 update step（与 scheduler 步数对齐）
            if accelerator.sync_gradients:
                total_step += 1

            # --- 日志打印 ---
            if total_step % args.logging_steps == 0:
                elapsed = time.time() - start_time
                steps_per_sec = total_step / elapsed if elapsed > 0 else 0
                current_lr = scheduler.get_last_lr()[0]
                accelerator.print(
                    f"Epoch {epoch + 1}/{args.epochs} | "
                    f"Step {total_step}/{max_train_steps} | "
                    f"Loss: {avg_loss:.4f} | "
                    f"LR: {current_lr:.2e} | "
                    f"Speed: {steps_per_sec:.2f} steps/s"
                )

            # --- 评估 ---
            if eval_dataloader is not None and total_step % args.eval_steps == 0:
                eval_loss = evaluate(model, eval_dataloader, accelerator, args)
                accelerator.print(
                    f"  [Eval] Step {total_step} | Eval Loss: {eval_loss:.4f}"
                    f"{' (best!)' if eval_loss < best_eval_loss else ''}"
                )

                if eval_loss < best_eval_loss:
                    best_eval_loss = eval_loss
                    save_checkpoint(model, tokenizer, args.output_dir, accelerator, is_best=True)
                    accelerator.wait_for_everyone()  # 等待 rank 0 保存完成

            # --- 定期保存检查点 ---
            if total_step % args.save_steps == 0:
                ckpt_dir = os.path.join(args.output_dir, f"checkpoint-{total_step}")
                save_checkpoint(model, tokenizer, args.output_dir, accelerator, step=total_step)
                checkpoints_saved.append(ckpt_dir)

                # 清理旧检查点（保留最近的 N 个）
                if args.save_total_limit > 0 and len(checkpoints_saved) > args.save_total_limit:
                    old_ckpt = checkpoints_saved.pop(0)
                    if os.path.exists(old_ckpt) and "best_model" not in old_ckpt:
                        import shutil
                        try:
                            shutil.rmtree(old_ckpt)
                            accelerator.print(f"  [Cleanup] Removed old checkpoint: {old_ckpt}")
                        except OSError:
                            pass

            # --- 达到最大步数则停止 ---
            if total_step >= max_train_steps:
                break

        # Epoch 结束
        completed_epochs += 1
        if epoch_steps > 0:
            avg_epoch_loss = epoch_loss / epoch_steps
            accelerator.print(
                f"\nEpoch {epoch + 1} 完成 | "
                f"平均 Loss: {avg_epoch_loss:.4f} | "
                f"总步数: {total_step}"
            )

        if total_step >= max_train_steps:
            break

    # --------------------------------------------------------
    # 13. 最终保存
    # --------------------------------------------------------
    accelerator.print("\n" + "=" * 60)
    accelerator.print("训练完成！")
    accelerator.print("=" * 60)

    # 最终模型保存（仅主进程保存，其他 rank 等待保存完成）
    save_checkpoint(model, tokenizer, args.output_dir, accelerator)
    accelerator.wait_for_everyone()  # 确保所有 rank 同步，避免 rank 0 还在保存时其他 rank 已退出

    # 如果有验证集，打印最佳 eval loss
    if best_eval_loss < float("inf"):
        accelerator.print(f"最佳 Eval Loss: {best_eval_loss:.4f}")

    total_time = time.time() - start_time
    accelerator.print(f"总训练时间: {total_time / 3600:.2f} 小时")
    accelerator.print(f"总步数: {total_step}, 完成轮数: {completed_epochs}")

    # 显式销毁进程组，确保 torch.distributed.launch 的所有子进程退出
    if torch.distributed.is_initialized():
        torch.distributed.destroy_process_group()
    accelerator.print("进程组已销毁，训练流程结束。")


if __name__ == "__main__":
    main()