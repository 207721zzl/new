"""BGE-M3 Dense Embedding 模型的加载与推理封装。"""

import os
from functools import lru_cache
from pathlib import Path

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.rag.device import resolve_torch_device


logger = get_logger("rag.embeddings")


class BgeM3Embedder:
    """在进程内复用的 BGE-M3 向量编码器。"""

    def __init__(self, settings: Settings) -> None:
        """解析模型目录、下载缺失文件并加载到目标设备。"""
        if settings.hf_hub_disable_xet:
            os.environ["HF_HUB_DISABLE_XET"] = "1"
        if settings.hf_token:
            os.environ["HF_TOKEN"] = settings.hf_token.get_secret_value()
        os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = str(settings.hf_hub_download_timeout)

        from huggingface_hub import snapshot_download
        from FlagEmbedding import BGEM3FlagModel

        model_path = settings.embedding_model
        # 支持本地模型目录；配置为 Hugging Face 仓库名时按需下载必要文件。
        if not Path(model_path).exists():
            logger.info("downloading minimal embedding model assets")
            model_path = snapshot_download(
                repo_id=settings.embedding_model,
                allow_patterns=[
                    "config.json",
                    "pytorch_model.bin",
                    "model.safetensors",
                    "colbert_linear.pt",
                    "sparse_linear.pt",
                    "sentencepiece.bpe.model",
                    "tokenizer.json",
                    "tokenizer_config.json",
                    "special_tokens_map.json",
                ],
            )

        logger.info(
            "loading embedding model model=%s device=%s",
            settings.embedding_model,
            settings.embedding_device,
        )
        self.settings = settings
        self.device = resolve_torch_device(settings.embedding_device)
        self.model = BGEM3FlagModel(
            model_path,
            devices=self.device,
            use_fp16=self.device == "cuda",
            batch_size=settings.embedding_batch_size,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        logger.info("embedding model loaded resolved_device=%s", self.device)

    def encode(self, texts: list[str]) -> list[list[float]]:
        """批量生成 Dense 向量并验证每个向量的维度。"""
        if not texts:
            return []

        output = self.model.encode(
            texts,
            batch_size=self.settings.embedding_batch_size,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        dense_vectors = output["dense_vecs"]
        vectors = dense_vectors.tolist()

        invalid_dimensions = {
            len(vector)
            for vector in vectors
            if len(vector) != self.settings.embedding_dimension
        }
        if invalid_dimensions:
            raise ValueError(
                "Embedding dimension mismatch: "
                f"expected {self.settings.embedding_dimension}, "
                f"received {sorted(invalid_dimensions)}"
            )
        return vectors


@lru_cache(maxsize=1)
def get_embedder() -> BgeM3Embedder:
    """延迟创建并缓存进程级 Embedding 模型。"""
    return BgeM3Embedder(get_settings())
