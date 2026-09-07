from dataclasses import asdict
from app.rag.models import RankedKnowledge, RetrievalHit, ParentKnowledgeContext
from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=64)
    priority: int = Field(default=0, ge=0, le=1)


class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    documents: list[str] = Field(min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=200)


def encode_evidence(items):
    return [asdict(item) for item in items]


def decode_evidence(items):
    return [RankedKnowledge(hit=RetrievalHit(**item["hit"]), rerank_score=item["rerank_score"],
                            parent=ParentKnowledgeContext(**item["parent"]) if item.get("parent") else None)
            for item in items]
