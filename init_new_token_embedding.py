# -*- coding: utf-8 -*-

"""
领域 Token Embedding 初始化

输入：
    old_model
    old_tokenizer
    new_model
    new_tokenizer
    new_tokens

流程：

    new_token
        │
        ▼
    old_tokenizer
        │
        ▼
    原始 sub-token
        │
        ▼
    old embedding
        │
        ▼
    semantic initialization
        │
        ▼
    PCA dominant subspace
        │
        ▼
    partial orthogonalization
        │
        ▼
    norm matching
        │
        ▼
    new_model embedding


注意：

1. new_tokens 已经由外部模块提供，本函数不负责发现。
2. old_tokenizer 必须是扩词之前的 tokenizer。
3. new_tokenizer 必须已经加入 new_tokens。
4. new_model 必须已经 resize_token_embeddings。
"""

from typing import List, Dict, Tuple, Optional
import os

import torch
import torch.nn.functional as F
import torch.distributed as dist

# NPU 检测
_NPU_AVAILABLE = False
try:
    import torch_npu  # noqa: F401
    _NPU_AVAILABLE = True
except ImportError:
    pass


# ============================================================
# 1. PCA 主子空间
# ============================================================

@torch.no_grad()
def build_pca_basis(
    old_embedding: torch.Tensor,
    pca_rank: int = 128,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    从旧词表 Embedding 中提取 PCA 主方向。

    Parameters
    ----------
    old_embedding:
        [vocab_size, hidden_size]

    pca_rank:
        PCA 主方向数量。

    Returns
    -------
    basis:
        [hidden_size, pca_rank]

    explained_variance:
        [pca_rank]
    """

    X = old_embedding.float()

    # 中心化
    X = X - X.mean(
        dim=0,
        keepdim=True
    )

    # SVD
    #
    # X = U S V^T
    #
    # V^T 的每一行是一个 PCA direction

    _, S, Vh = torch.linalg.svd(
        X,
        full_matrices=False
    )

    rank = min(
        pca_rank,
        Vh.shape[0]
    )

    # [hidden_size, rank]

    basis = (
        Vh[:rank]
        .T
        .contiguous()
    )

    # PCA explained variance

    variance = S.pow(2)

    explained_variance = (
        variance[:rank]
        /
        variance.sum()
    )

    return (
        basis,
        explained_variance
    )


# ============================================================
# 2. Semantic Embedding 初始化
# ============================================================

@torch.no_grad()
def build_semantic_embedding(
    token: str,
    old_tokenizer,
    old_embedding: torch.Tensor,
    pooling: str = "mean",
) -> Tuple[
    torch.Tensor,
    List[int],
    List[str]
]:
    """
    使用旧 tokenizer 将新 Token 切分为原始 sub-token，
    然后使用旧 embedding 构造 semantic embedding。

    例如：

        TP_IPC_SERVER_MAGIC_FAIL

    可能被切成：

        TP
        _
        IPC
        _
        SERVER
        _
        MAGIC
        _
        FAIL

    注意：

        必须使用 old_tokenizer。

    因为 new_tokenizer 已经认识：

        TP_IPC_SERVER_MAGIC_FAIL

    此时无法再得到原始切分。
    """

    # --------------------------------------------------------
    # 原始 tokenizer 切分
    # --------------------------------------------------------

    ids = old_tokenizer.encode(
        token,
        add_special_tokens=False
    )

    if len(ids) == 0:

        raise ValueError(
            f"Token cannot be encoded: {token}"
        )

    pieces = (
        old_tokenizer
        .convert_ids_to_tokens(ids)
    )

    # --------------------------------------------------------
    # 获取 sub-token embedding
    # --------------------------------------------------------

    sub_embeddings = (
        old_embedding[ids]
        .float()
    )

    # --------------------------------------------------------
    # Mean pooling
    # --------------------------------------------------------

    if pooling == "mean":

        semantic = (
            sub_embeddings
            .mean(dim=0)
        )

    # --------------------------------------------------------
    # Weighted Mean
    # --------------------------------------------------------

    elif pooling == "weighted_mean":

        weights = torch.ones(
            len(pieces),
            device=old_embedding.device,
            dtype=torch.float32
        )

        for i, piece in enumerate(
            pieces
        ):

            # 结构符号降低权重

            if piece in {
                "_",
                "▁",
                "##",
            }:

                weights[i] = 0.25

        weights = (
            weights /
            weights.sum()
        )

        semantic = (
            sub_embeddings *
            weights.unsqueeze(-1)
        ).sum(dim=0)

    else:

        raise ValueError(
            f"Unsupported pooling: {pooling}"
        )

    return (
        semantic,
        ids,
        pieces
    )


# ============================================================
# 3. Partial Orthogonalization
# ============================================================

@torch.no_grad()
def partial_orthogonalize(
    semantic: torch.Tensor,
    basis: torch.Tensor,
    alpha: float = 0.3,
) -> Tuple[
    torch.Tensor,
    torch.Tensor,
    torch.Tensor
]:
    """
    对 semantic embedding 进行部分正交化。

    projection：

        Q Q^T semantic

    orthogonal：

        semantic - projection

    result：

        (1-alpha) * semantic
        +
        alpha * orthogonal

    Parameters
    ----------
    alpha:

        0:
            完全不正交化

        0.3:
            推荐起点

        1:
            完全去除 PCA 主方向
    """

    # --------------------------------------------------------
    # Projection
    # --------------------------------------------------------

    projection = (
        basis @
        (
            basis.T @
            semantic
        )
    )

    # --------------------------------------------------------
    # 正交分量
    # --------------------------------------------------------

    orthogonal = (
        semantic -
        projection
    )

    # --------------------------------------------------------
    # 部分正交化
    # --------------------------------------------------------

    result = (
        (1.0 - alpha) * semantic
        +
        alpha * orthogonal
    )

    return (
        result,
        projection,
        orthogonal
    )


# ============================================================
# 4. Norm Matching
# ============================================================

@torch.no_grad()
def match_embedding_norm(
    vector: torch.Tensor,
    old_embedding: torch.Tensor,
    statistic: str = "median",
) -> torch.Tensor:
    """
    将新 embedding 的 norm 调整到旧 embedding 的合理范围。

    不建议直接：

        F.normalize(vector)

    因为 BERT 原始 embedding 的 norm 通常不是 1。

    推荐让：

        ||new_embedding||

    接近旧 embedding 的 median norm。
    """

    old_norms = (
        old_embedding
        .float()
        .norm(dim=1)
    )

    if statistic == "mean":

        target_norm = (
            old_norms.mean()
        )

    elif statistic == "p75":

        target_norm = torch.quantile(
            old_norms,
            0.75
        )

    elif statistic == "p50":

        target_norm = torch.quantile(
            old_norms,
            0.50
        )

    else:

        target_norm = (
            old_norms.median()
        )

    vector_norm = vector.norm()

    if vector_norm < 1e-8:

        # 理论上基本不会发生
        # 仅作为保护

        vector = torch.randn_like(
            vector
        )

        vector = F.normalize(
            vector,
            dim=0
        )

    else:

        vector = (
            vector /
            vector_norm
        )

    vector = (
        vector *
        target_norm
    )

    return vector


# ============================================================
# 5. Cosine Similarity 分析
# ============================================================

@torch.no_grad()
def analyze_similarity(
    vector: torch.Tensor,
    old_embedding: torch.Tensor,
    top_k: int = 10,
) -> Dict:

    """
    分析新 Token 与旧词表的 embedding 相似度。

    用于验证：

        新 token 是否和某个旧 token 过度重合。
    """

    new_vector = F.normalize(
        vector.float(),
        dim=0
    )

    old_vectors = F.normalize(
        old_embedding.float(),
        dim=1
    )

    cosine = (
        old_vectors @
        new_vector
    )

    k = min(
        top_k,
        cosine.shape[0]
    )

    values, indices = torch.topk(
        cosine,
        k=k
    )

    return {

        "max_cosine":
            float(
                cosine.max()
            ),

        "mean_cosine":
            float(
                cosine.mean()
            ),

        "top_indices":
            [
                int(x)
                for x in indices
            ],

        "top_cosines":
            [
                float(x)
                for x in values
            ],
    }


# ============================================================
# 6. 核心函数
# ============================================================

@torch.no_grad()
def init_new_embeddings(
    old_model,
    old_tokenizer,
    new_model,
    new_tokenizer,
    new_tokens: List[str],
    pca_rank: int = 128,
    alpha: float = 0.3,
    pooling: str = "weighted_mean",
    norm_statistic: str = "median",
    device: Optional[str] = None,
    return_report: bool = True,
):
    """
    初始化指定领域 Token 的 Embedding。

    ==========================================================
    输入
    ==========================================================

    old_model
        扩词前模型。

    old_tokenizer
        扩词前 tokenizer。

    new_model
        扩词后模型。

    new_tokenizer
        扩词后 tokenizer。

    new_tokens
        已经确定好的新增领域 Token 列表。

        例如：

            [
                "TP_IPC_SERVER_MAGIC_FAIL",
                "ADM_JAGUARDB_EVENT",
                "CableEndTimer",
                "ADM_ADM_LINK_DB"
            ]

    ==========================================================
    输出
    ==========================================================

    new_model

    report

    ==========================================================
    核心算法
    ==========================================================

        semantic
            =
        mean(
            old_embedding(
                old_tokenizer(token)
            )
        )

        projection
            =
        QQ^T semantic

        orthogonal
            =
        semantic - projection

        result
            =
        (1-alpha) semantic
        +
        alpha orthogonal

        result
            =
        norm_matching(result)
    """

    # ========================================================
    # 0. 参数检查
    # ========================================================

    if not new_tokens:

        raise ValueError(
            "new_tokens is empty."
        )

    if not 0.0 <= alpha <= 1.0:

        raise ValueError(
            "alpha must be in [0, 1]."
        )

    if pca_rank <= 0:

        raise ValueError(
            "pca_rank must be > 0."
        )

    # ========================================================
    # 1. Device
    # ========================================================

    if device is None:

        device = (
            "cuda"
            if torch.cuda.is_available()
            else "cpu"
        )

    old_model = old_model.to(
        device
    )

    new_model = new_model.to(
        device
    )

    old_model.eval()
    new_model.eval()

    # ========================================================
    # 2. 获取 Embedding
    # ========================================================

    old_embedding_layer = (
        old_model.get_input_embeddings()
    )

    new_embedding_layer = (
        new_model.get_input_embeddings()
    )

    old_embedding = (
        old_embedding_layer
        .weight
        .detach()
        .clone()
        .float()
        .to(device)
    )

    new_embedding = (
        new_embedding_layer
        .weight
    )

    old_vocab_size = (
        old_embedding.shape[0]
    )

    new_vocab_size = (
        new_embedding.shape[0]
    )

    hidden_size = (
        old_embedding.shape[1]
    )

    print("=" * 70)
    print("Initializing Domain Token Embeddings")
    print("=" * 70)

    print(
        f"Old vocab size : {old_vocab_size}"
    )

    print(
        f"New vocab size : {new_vocab_size}"
    )

    print(
        f"Hidden size    : {hidden_size}"
    )

    print(
        f"Token count    : {len(new_tokens)}"
    )

    print(
        f"PCA rank       : {pca_rank}"
    )

    print(
        f"Alpha          : {alpha}"
    )

    print(
        f"Pooling        : {pooling}"
    )

    # ========================================================
    # 3. 检查 Token
    # ========================================================

    new_vocab = (
        new_tokenizer.get_vocab()
    )

    for token in new_tokens:

        if token not in new_vocab:

            raise ValueError(
                f"Token '{token}' "
                f"is not found in new_tokenizer."
            )

    # ========================================================
    # 4. PCA
    # ========================================================

    print()
    print(
        "Building PCA basis..."
    )

    basis, explained_variance = (
        build_pca_basis(
            old_embedding=old_embedding,
            pca_rank=pca_rank,
        )
    )

    explained_ratio = (
        explained_variance.sum()
    )

    print(
        f"PCA explained variance "
        f"({pca_rank}): "
        f"{explained_ratio.item():.4f}"
    )

    # ========================================================
    # 5. Norm statistic
    # ========================================================

    old_norms = (
        old_embedding
        .norm(dim=1)
    )

    if norm_statistic == "mean":

        target_norm = (
            old_norms.mean()
        )

    elif norm_statistic == "p75":

        target_norm = torch.quantile(
            old_norms,
            0.75
        )

    else:

        target_norm = (
            old_norms.median()
        )

    print(
        f"Target embedding norm: "
        f"{target_norm.item():.6f}"
    )

    # ========================================================
    # 6. 初始化
    # ========================================================

    report = []

    for index, token in enumerate(
        new_tokens
    ):

        print()
        print(
            f"[{index + 1}/{len(new_tokens)}] "
            f"{token}"
        )

        # ====================================================
        # 6.1 获取新 token ID
        # ====================================================

        new_id = (
            new_tokenizer
            .convert_tokens_to_ids(
                token
            )
        )

        if new_id == (
            new_tokenizer.unk_token_id
        ):

            raise ValueError(
                f"Token '{token}' "
                f"maps to UNK."
            )

        # ====================================================
        # 6.2 old tokenizer 切分
        # ====================================================

        (
            semantic,
            old_ids,
            old_pieces,
        ) = build_semantic_embedding(
            token=token,
            old_tokenizer=old_tokenizer,
            old_embedding=old_embedding,
            pooling=pooling,
        )

        print(
            f"Old pieces: "
            f"{old_pieces}"
        )

        # ====================================================
        # 6.3 Partial Orthogonalization
        # ====================================================

        (
            result,
            projection,
            orthogonal,
        ) = partial_orthogonalize(
            semantic=semantic,
            basis=basis,
            alpha=alpha,
        )

        # ====================================================
        # 6.4 Norm Matching
        # ====================================================

        result = match_embedding_norm(
            vector=result,
            old_embedding=old_embedding,
            statistic=norm_statistic,
        )

        # ====================================================
        # 6.5 写入 new model
        # ====================================================

        new_embedding[
            new_id
        ].copy_(
            result.to(
                dtype=new_embedding.dtype,
                device=new_embedding.device
            )
        )

        # ====================================================
        # 6.6 分析
        # ====================================================

        similarity = analyze_similarity(
            vector=result,
            old_embedding=old_embedding,
        )

        semantic_norm = (
            semantic.norm()
        )

        projection_norm = (
            projection.norm()
        )

        orthogonal_norm = (
            orthogonal.norm()
        )

        print(
            f"Semantic norm   : "
            f"{semantic_norm.item():.6f}"
        )

        print(
            f"Projection norm : "
            f"{projection_norm.item():.6f}"
        )

        print(
            f"Orthogonal norm : "
            f"{orthogonal_norm.item():.6f}"
        )

        print(
            f"Final norm      : "
            f"{result.norm().item():.6f}"
        )

        print(
            f"Max cosine      : "
            f"{similarity['max_cosine']:.6f}"
        )

        # ====================================================
        # 6.7 Report
        # ====================================================

        report.append({

            "token":
                token,

            "new_id":
                int(new_id),

            "old_ids":
                [
                    int(x)
                    for x in old_ids
                ],

            "old_pieces":
                old_pieces,

            "semantic_norm":
                float(
                    semantic_norm
                ),

            "projection_norm":
                float(
                    projection_norm
                ),

            "orthogonal_norm":
                float(
                    orthogonal_norm
                ),

            "final_norm":
                float(
                    result.norm()
                ),

            "max_cosine":
                similarity[
                    "max_cosine"
                ],

            "mean_cosine":
                similarity[
                    "mean_cosine"
                ],

            "top_old_token_ids":
                similarity[
                    "top_indices"
                ],

            "top_old_token_cosines":
                similarity[
                    "top_cosines"
                ],
        })

    # ========================================================
    # 7. 返回
    # ========================================================

    print()
    print("=" * 70)
    print(
        f"Successfully initialized "
        f"{len(report)} domain tokens."
    )
    print("=" * 70)

    if return_report:

        return (
            new_model,
            report
        )

    return new_model


# ============================================================
# 7. 分布式版本（简化：仅 rank 0 计算，然后 broadcast 模型权重）
# ============================================================

@torch.no_grad()
def init_new_embeddings_distributed(
    old_model,
    old_tokenizer,
    new_model,
    new_tokenizer,
    new_tokens: List[str],
    pca_rank: int = 128,
    alpha: float = 0.3,
    pooling: str = "weighted_mean",
    norm_statistic: str = "median",
    device: Optional[str] = None,
    return_report: bool = True,
):
    """
    分布式版本的 init_new_embeddings。

    简化策略：
        - 仅 rank 0 执行 init_new_embeddings 计算
        - 其他 rank 等待
        - 计算完成后，通过 broadcast 将 new_model 的 embedding 权重
          同步到所有 rank

    这样逻辑与单卡版本完全一致，不会引入额外复杂性。
    当未初始化分布式时自动退化为单卡版本。
    """

    # ========================================================
    # 0. 分布式环境检测
    # ========================================================

    is_distributed = (
        dist.is_available()
        and dist.is_initialized()
    )

    if is_distributed:
        rank = dist.get_rank()
        world_size = dist.get_world_size()
    else:
        rank = 0
        world_size = 1

    is_main = (rank == 0)

    # ========================================================
    # 1. 仅 rank 0 执行 embedding 初始化
    # ========================================================

    report = []

    if is_main:
        print("=" * 70)
        print("Initializing Domain Token Embeddings (Rank 0)")
        print("=" * 70)

        new_model, report = init_new_embeddings(
            old_model=old_model,
            old_tokenizer=old_tokenizer,
            new_model=new_model,
            new_tokenizer=new_tokenizer,
            new_tokens=new_tokens,
            pca_rank=pca_rank,
            alpha=alpha,
            pooling=pooling,
            norm_statistic=norm_statistic,
            device=device,
            return_report=True,
        )

        print(f"Rank 0: embedding init complete, {len(report)} tokens.")

    # ========================================================
    # 2. 同步 embedding 权重到所有 rank
    # ========================================================

    if is_distributed:
        # 确定通信设备
        local_rank = int(os.environ.get("LOCAL_RANK", 0))
        if _NPU_AVAILABLE:
            comm_device = torch.device(f"npu:{local_rank}")
        elif torch.cuda.is_available():
            comm_device = torch.device(f"cuda:{local_rank}")
        else:
            comm_device = torch.device("cpu")

        if is_main:
            print(f"Broadcasting embedding weights from rank 0 (device: {comm_device})...")

        # 将 new_model 的 input embedding 权重 broadcast 到所有 rank
        embedding_layer = new_model.get_input_embeddings()
        embedding_weight = embedding_layer.weight

        # broadcast 需要 tensor 在通信设备上
        embedding_weight_on_device = embedding_weight.data.to(comm_device)
        dist.broadcast(embedding_weight_on_device, src=0)
        # 非 rank 0 的进程接收数据
        if not is_main:
            embedding_weight.data.copy_(
                embedding_weight_on_device.to(
                    dtype=embedding_weight.dtype,
                    device=embedding_weight.device,
                )
            )

        # 同样 broadcast output embedding (decoder) 权重
        # LogNetBigForMaskedLM 的 lm_head.decoder 与 input embedding 共享权重
        # 所以不需要额外 broadcast，但为通用性也处理一下
        output_emb = new_model.get_output_embeddings()
        if output_emb is not None and output_emb is not embedding_layer:
            output_weight = output_emb.weight
            output_weight_on_device = output_weight.data.to(comm_device)
            dist.broadcast(output_weight_on_device, src=0)
            if not is_main:
                output_weight.data.copy_(
                    output_weight_on_device.to(
                        dtype=output_weight.dtype,
                        device=output_weight.device,
                    )
                )

        # 确保所有 rank 同步完成
        dist.barrier()

        if is_main:
            print("Broadcast complete. All ranks synced.")

    # ========================================================
    # 3. 返回
    # ========================================================

    if return_report:
        return new_model, report

    return new_model
