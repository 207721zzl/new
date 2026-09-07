"""Milvus Schema、双路索引和 RRF 混合检索参数测试。"""

from app.config import Settings
from app.rag.milvus_store import (
    MilvusKnowledgeStore,
    build_collection_schema,
    build_index_params,
)


def test_milvus_schema_contains_dense_and_bm25_paths():
    schema = build_collection_schema(dimension=1024).to_dict()
    field_names = {field["name"] for field in schema["fields"]}
    function_names = {function["name"] for function in schema["functions"]}

    assert "dense_vector" in field_names
    assert "sparse_vector" in field_names
    assert "parent_chunk_id" in field_names
    assert "parent_index" in field_names
    assert "content_bm25" in function_names
    assert len(build_index_params()) == 2


class FakeMilvusClient:
    def __init__(self):
        self.call = None

    def hybrid_search(self, **kwargs):
        self.call = kwargs
        return [
            [
                {
                    "chunk_id": "a" * 64,
                    "distance": 0.75,
                    "entity": {
                        "parent_chunk_id": "b" * 64,
                        "document_id": "metric_refund_rate",
                        "title": "退款率指标定义",
                        "content": "退款率公式",
                        "chunk_index": 0,
                        "doc_type": "metric_definition",
                        "metric_name": "refund_rate",
                        "source": "指标手册",
                        "version": "1.0",
                        "updated_at": "2026-07-16",
                    },
                }
            ]
        ]


def test_hybrid_search_combines_dense_and_bm25_with_rrf():
    settings = Settings(
        _env_file=None,
        embedding_dimension=3,
        retrieval_top_k=4,
        hybrid_candidate_top_k=4,
        hybrid_rrf_k=30,
    )
    client = FakeMilvusClient()
    store = MilvusKnowledgeStore.__new__(MilvusKnowledgeStore)
    store.settings = settings
    store.client = client

    hits = store.hybrid_search("退款率", [0.1, 0.2, 0.3])

    assert len(hits) == 1
    assert hits[0].chunk_id == "a" * 64
    assert hits[0].parent_chunk_id == "b" * 64
    assert hits[0].document_id == "metric_refund_rate"
    assert hits[0].retrieval_score == 0.75
    assert [request.anns_field for request in client.call["reqs"]] == [
        "dense_vector",
        "sparse_vector",
    ]
    assert client.call["reqs"][1].data == ["退款率"]
    assert client.call["limit"] == 4
