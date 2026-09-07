"""Milvus Collection、索引和 Dense+BM25 混合检索封装。"""

import json

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.rag.models import KnowledgeChunk, RetrievalHit


logger = get_logger("rag.milvus")


def build_collection_schema(dimension: int):
    """创建包含文档元数据、Dense 向量和 BM25 稀疏向量的 Schema。"""
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(
        field_name="chunk_id",
        datatype=DataType.VARCHAR,
        is_primary=True,
        auto_id=False,
        max_length=64,
    )
    schema.add_field(
        field_name="document_id",
        datatype=DataType.VARCHAR,
        max_length=128,
    )
    schema.add_field(
        field_name="parent_chunk_id",
        datatype=DataType.VARCHAR,
        max_length=64,
    )
    schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=256)
    schema.add_field(
        field_name="content",
        datatype=DataType.VARCHAR,
        max_length=8192,
        enable_analyzer=True,
        analyzer_params={"tokenizer": "jieba"},
    )
    schema.add_field(field_name="chunk_index", datatype=DataType.INT64)
    schema.add_field(field_name="parent_index", datatype=DataType.INT64)
    schema.add_field(field_name="doc_type", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(
        field_name="metric_name",
        datatype=DataType.VARCHAR,
        max_length=128,
    )
    schema.add_field(field_name="source", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="version", datatype=DataType.VARCHAR, max_length=32)
    schema.add_field(
        field_name="section_path",
        datatype=DataType.VARCHAR,
        max_length=1024,
    )
    schema.add_field(field_name="page_start", datatype=DataType.INT64)
    schema.add_field(field_name="page_end", datatype=DataType.INT64)
    schema.add_field(field_name="block_type", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="language", datatype=DataType.VARCHAR, max_length=16)
    schema.add_field(
        field_name="updated_at",
        datatype=DataType.VARCHAR,
        max_length=32,
    )
    schema.add_field(
        field_name="dense_vector",
        datatype=DataType.FLOAT_VECTOR,
        dim=dimension,
    )
    schema.add_field(
        field_name="sparse_vector",
        datatype=DataType.SPARSE_FLOAT_VECTOR,
    )
    schema.add_function(
        Function(
            name="content_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["content"],
            output_field_names=["sparse_vector"],
        )
    )
    return schema


def build_index_params():
    """定义 Dense HNSW/COSINE 与 Sparse BM25 两条索引路径。"""
    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(
        field_name="dense_vector",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )
    index_params.add_index(
        field_name="sparse_vector",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    return index_params


class MilvusKnowledgeStore:
    """管理知识 Collection 的建表、Upsert、检索和统计。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """创建轻量 Milvus 客户端；调用方负责关闭。"""
        self.settings = settings or get_settings()
        token = (
            self.settings.milvus_token.get_secret_value()
            if self.settings.milvus_token
            else ""
        )
        self.client = MilvusClient(uri=self.settings.milvus_uri, token=token)

    def ensure_collection(self, recreate: bool = False) -> bool:
        """确保 Collection 存在，并返回本次是否执行了创建。"""
        collection_name = self.settings.milvus_collection
        collection_exists = self.client.has_collection(collection_name)

        if collection_exists and recreate:
            logger.warning("dropping collection collection=%s", collection_name)
            self.client.drop_collection(collection_name)
            collection_exists = False

        if collection_exists:
            logger.info("collection already exists collection=%s", collection_name)
            return False

        self.client.create_collection(
            collection_name=collection_name,
            schema=build_collection_schema(self.settings.embedding_dimension),
            index_params=build_index_params(),
        )
        logger.info("collection created collection=%s", collection_name)
        return True

    def upsert_chunks(
        self,
        chunks: list[KnowledgeChunk],
        dense_vectors: list[list[float]],
    ) -> int:
        """按稳定 chunk_id 幂等写入知识块及其 Dense 向量。"""
        if len(chunks) != len(dense_vectors):
            raise ValueError("chunks and dense_vectors must have the same length")
        if not chunks:
            return 0

        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "parent_chunk_id": chunk.parent_chunk_id,
                "document_id": chunk.document_id,
                "title": chunk.title,
                "content": chunk.content,
                "chunk_index": chunk.chunk_index,
                "parent_index": chunk.parent_index,
                "doc_type": chunk.doc_type,
                "metric_name": chunk.metric_name,
                "source": chunk.source,
                "version": chunk.version,
                "section_path": " / ".join(chunk.section_path)[:1024],
                "page_start": chunk.page_start or 0,
                "page_end": chunk.page_end or 0,
                "block_type": chunk.block_type,
                "language": chunk.language,
                "updated_at": chunk.updated_at.isoformat(),
                "dense_vector": vector,
            }
            for chunk, vector in zip(chunks, dense_vectors, strict=True)
        ]

        self.client.upsert(
            collection_name=self.settings.milvus_collection,
            data=rows,
        )
        self.client.flush(self.settings.milvus_collection)
        logger.info(
            "knowledge chunks upserted collection=%s count=%s",
            self.settings.milvus_collection,
            len(rows),
        )
        return len(rows)

    def delete_documents(self, document_ids: list[str]) -> None:
        """增量更新前移除指定文档的旧子块，避免新旧版本同时被召回。"""
        unique_ids = list(dict.fromkeys(document_ids))
        if not unique_ids:
            return
        expression = f"document_id in {json.dumps(unique_ids, ensure_ascii=False)}"
        self.client.delete(
            collection_name=self.settings.milvus_collection,
            filter=expression,
        )
        self.client.flush(self.settings.milvus_collection)

    def hybrid_search(
        self,
        query: str,
        dense_vector: list[float],
        candidate_top_k: int | None = None,
    ) -> list[RetrievalHit]:
        """并行执行 Dense 与 BM25 检索，再使用 RRF 融合候选。"""
        if not query.strip():
            raise ValueError("query must not be empty")
        if len(dense_vector) != self.settings.embedding_dimension:
            raise ValueError(
                "Query embedding dimension mismatch: "
                f"expected {self.settings.embedding_dimension}, "
                f"received {len(dense_vector)}"
            )

        limit = candidate_top_k or self.settings.hybrid_candidate_top_k
        # 两条请求使用相同候选上限，确保 RRF 在可比的排名集合上融合。
        dense_request = AnnSearchRequest(
            data=[dense_vector],
            anns_field="dense_vector",
            param={
                "metric_type": "COSINE",
                "params": {"ef": self.settings.dense_search_ef},
            },
            limit=limit,
        )
        sparse_request = AnnSearchRequest(
            data=[query],
            anns_field="sparse_vector",
            param={"metric_type": "BM25", "params": {}},
            limit=limit,
        )
        output_fields = [
            "chunk_id",
            "parent_chunk_id",
            "document_id",
            "title",
            "content",
            "chunk_index",
            "parent_index",
            "doc_type",
            "metric_name",
            "source",
            "version",
            "updated_at",
            "section_path",
            "page_start",
            "page_end",
            "block_type",
            "language",
        ]

        results = self.client.hybrid_search(
            collection_name=self.settings.milvus_collection,
            reqs=[dense_request, sparse_request],
            ranker=RRFRanker(k=self.settings.hybrid_rrf_k),
            limit=limit,
            output_fields=output_fields,
            timeout=self.settings.milvus_search_timeout_seconds,
        )
        if not results:
            return []

        hits: list[RetrievalHit] = []
        for result in results[0]:
            entity = result.get("entity") or {}
            hits.append(
                RetrievalHit(
                    chunk_id=str(
                        result.get("id")
                        or result.get("chunk_id")
                        or entity.get("chunk_id")
                        or ""
                    ),
                    parent_chunk_id=str(entity.get("parent_chunk_id") or ""),
                    document_id=str(entity.get("document_id") or ""),
                    title=str(entity.get("title") or ""),
                    content=str(entity.get("content") or ""),
                    chunk_index=int(entity.get("chunk_index") or 0),
                    doc_type=str(entity.get("doc_type") or ""),
                    metric_name=str(entity.get("metric_name") or ""),
                    source=str(entity.get("source") or ""),
                    version=str(entity.get("version") or ""),
                    updated_at=str(entity.get("updated_at") or ""),
                    retrieval_score=float(
                        result.get("distance", result.get("score", 0.0))
                    ),
                )
            )

        logger.info(
            "hybrid retrieval completed collection=%s candidates=%s",
            self.settings.milvus_collection,
            len(hits),
        )
        return hits

    def collection_row_count(self) -> int:
        """返回当前知识 Collection 的实体数量。"""
        stats = self.client.get_collection_stats(self.settings.milvus_collection)
        return int(stats.get("row_count", 0))

    def close(self) -> None:
        """释放底层 Milvus 客户端连接。"""
        self.client.close()
