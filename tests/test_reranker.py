"""Reranker 排序、Top-K、输入校验和模型目录测试。"""

import pytest

from app.config import PROJECT_ROOT, Settings
from app.rag.reranker import BgeReranker


@pytest.fixture
def reranker(monkeypatch):
    settings = Settings(
        _env_file=None,
        reranker_device="cpu",
        reranker_batch_size=4,
        reranker_max_length=256,
        reranker_normalize=True,
        retrieval_top_k=2,
    )
    instance = BgeReranker.__new__(BgeReranker)
    instance.settings = settings
    scores_by_document = {
        "low relevance": 0.1,
        "highest relevance": 0.9,
        "medium relevance": 0.5,
    }
    monkeypatch.setattr(
        instance,
        "_compute_scores",
        lambda _query, documents: [
            scores_by_document[document] for document in documents
        ],
    )
    return instance


def test_reranker_orders_documents_and_applies_default_top_k(reranker):
    results = reranker.rerank(
        "test query",
        ["low relevance", "highest relevance", "medium relevance"],
    )

    assert [result.content for result in results] == [
        "highest relevance",
        "medium relevance",
    ]
    assert [result.score for result in results] == [0.9, 0.5]
    assert [result.original_index for result in results] == [1, 2]


def test_reranker_allows_top_k_override(reranker):
    results = reranker.rerank(
        "test query",
        ["low relevance", "highest relevance", "medium relevance"],
        top_k=1,
    )

    assert len(results) == 1
    assert results[0].content == "highest relevance"


def test_reranker_rejects_empty_query(reranker):
    with pytest.raises(ValueError, match="query"):
        reranker.rerank("   ", ["highest relevance"])


def test_reranker_requires_local_model_directory():
    settings = Settings(
        _env_file=None,
        reranker_model=str(PROJECT_ROOT / "missing-reranker-model"),
        reranker_device="cpu",
    )

    with pytest.raises(FileNotFoundError, match="does not exist"):
        BgeReranker(settings)
