"""BGE Cross-Encoder Reranker 的加载与批量推理封装。"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.rag.device import resolve_torch_device


logger = get_logger("rag.reranker")


@dataclass(frozen=True, slots=True)
class RerankResult:
    """单条重排结果及其原候选位置。"""

    content: str
    score: float
    original_index: int


class BgeReranker:
    """使用序列分类模型对检索候选进行相关性重排。"""

    def __init__(self, settings: Settings) -> None:
        """准备模型资源并加载 Tokenizer 与分类模型。"""
        model_path = Path(settings.reranker_model)
        if not model_path.is_dir():
            if model_path.is_absolute():
                raise FileNotFoundError(
                    f"Local reranker model directory does not exist: {model_path}"
                )
            if settings.hf_hub_disable_xet:
                os.environ["HF_HUB_DISABLE_XET"] = "1"
            if settings.hf_token:
                os.environ["HF_TOKEN"] = settings.hf_token.get_secret_value()
            os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = str(
                settings.hf_hub_download_timeout
            )
            from huggingface_hub import snapshot_download

            logger.info("downloading reranker model model=%s", settings.reranker_model)
            model_path = Path(snapshot_download(repo_id=settings.reranker_model))

        import torch
        from transformers import (
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )

        logger.info(
            "loading reranker model=%s device=%s",
            settings.reranker_model,
            settings.reranker_device,
        )
        self.settings = settings
        self.torch = torch
        resolved_device = resolve_torch_device(settings.reranker_device)
        self.device = torch.device(resolved_device)
        self.tokenizer = AutoTokenizer.from_pretrained(
            str(model_path),
            use_fast=settings.reranker_use_fast_tokenizer,
            local_files_only=True,
        )
        self.model = AutoModelForSequenceClassification.from_pretrained(
            str(model_path),
            local_files_only=True,
        )
        self.model.to(self.device)
        if resolved_device == "cuda":
            self.model.half()
        self.model.eval()
        logger.info("reranker model loaded resolved_device=%s", resolved_device)

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[RerankResult]:
        """计算 query-document 相关性，并稳定返回得分最高的候选。"""
        if not query.strip():
            raise ValueError("query must not be empty")
        if not documents:
            return []
        if any(not document.strip() for document in documents):
            raise ValueError("documents must not contain empty text")

        limit = top_k if top_k is not None else self.settings.retrieval_top_k
        if limit < 1:
            raise ValueError("top_k must be greater than zero")

        scores = self._compute_scores(query, documents)
        if len(scores) != len(documents):
            raise ValueError(
                "Reranker score count mismatch: "
                f"expected {len(documents)}, received {len(scores)}"
            )

        results = [
            RerankResult(
                content=document,
                score=score,
                original_index=index,
            )
            for index, (document, score) in enumerate(
                zip(documents, scores, strict=True)
            )
        ]
        results.sort(key=lambda result: (-result.score, result.original_index))
        return results[: min(limit, len(results))]

    def _compute_scores(self, query: str, documents: list[str]) -> list[float]:
        """分批执行交叉编码器推理，控制峰值显存或内存占用。"""
        scores: list[float] = []
        batch_size = self.settings.reranker_batch_size

        with self.torch.inference_mode():
            for start in range(0, len(documents), batch_size):
                batch = documents[start : start + batch_size]
                inputs = self.tokenizer(
                    [query] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.settings.reranker_max_length,
                    return_tensors="pt",
                )
                inputs = {
                    name: tensor.to(self.device) for name, tensor in inputs.items()
                }
                logits = self.model(**inputs, return_dict=True).logits.view(-1)
                logits = logits.float()
                if self.settings.reranker_normalize:
                    logits = self.torch.sigmoid(logits)
                scores.extend(logits.cpu().tolist())

        return scores


@lru_cache(maxsize=1)
def get_reranker() -> BgeReranker:
    """延迟创建并缓存进程级 Reranker。"""
    return BgeReranker(get_settings())
