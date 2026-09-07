import asyncio
from sqlalchemy import select
from app.config import get_settings
from app.errors import RetrievalError
from app.rag.milvus_store import MilvusKnowledgeStore
from app.rag.models import RankedKnowledge, ParentKnowledgeContext
from packages.platform.client import ServiceClient
from services.knowledge.db import session_factory
from services.knowledge.models import KnowledgeState, DocumentHead, DocumentSnapshot


async def visible_evidence(hits, collection):
    if not hits:
        return []
    async with session_factory() as session:
        state = await session.get(KnowledgeState, 1)
        if state.collection != collection:
            return []
        rows = (await session.execute(select(DocumentHead, DocumentSnapshot).join(
            DocumentSnapshot, DocumentHead.active_snapshot == DocumentSnapshot.snapshot_id).where(
            DocumentHead.document_id.in_({hit.document_id for hit in hits}),
            DocumentHead.deleted_at.is_(None), DocumentSnapshot.collection == collection))).all()
    contexts = {}
    for head, snapshot in rows:
        parents = {p["parent_chunk_id"]: p for p in snapshot.payload["parents"]}
        for child in snapshot.payload["children"]:
            parent = parents.get(child["parent_chunk_id"])
            if parent is None:
                raise RetrievalError("published snapshot contains a missing parent")
            contexts[child["chunk_id"]] = ParentKnowledgeContext(**{key: parent.get(key)
                for key in ParentKnowledgeContext.__dataclass_fields__})
    return [(hit, contexts[hit.chunk_id]) for hit in hits if hit.chunk_id in contexts]


async def retrieve(query, top_k):
    config = get_settings()
    async with session_factory() as session:
        state = await session.get(KnowledgeState, 1)
        collection = state.collection
    vector = await ServiceClient("inference").post("/internal/v1/embed", {"texts": [query]})
    store = MilvusKnowledgeStore(config.model_copy(update={"milvus_collection": collection}))
    try:
        if not await asyncio.to_thread(store.client.has_collection, collection):
            return []
        # Refill when unpublished/obsolete versions consume the initial candidate budget.
        limit = max(top_k, config.hybrid_candidate_top_k)
        while True:
            hits = await asyncio.to_thread(store.hybrid_search, query, vector["vectors"][0], limit)
            visible = await visible_evidence(hits, collection)
            if len(visible) >= max(top_k, config.hybrid_candidate_top_k) or len(hits) < limit or limit >= 200:
                break
            limit = min(200, limit*2)
        if not visible:
            return []
        scores = await ServiceClient("inference").post("/internal/v1/rerank", {
            "query": query, "documents": [hit.content for hit, _ in visible], "top_k": len(visible)})
        candidates = []
        seen = set()
        for rank in scores["results"]:
            hit, parent = visible[rank["original_index"]]
            if rank["score"] < config.reranker_min_score or parent.parent_chunk_id in seen:
                continue
            seen.add(parent.parent_chunk_id)
            candidates.append(RankedKnowledge(hit, rank["score"], parent))
        # Deletion and generation cutover may happen while the GPU runs.
        still_visible = {hit.chunk_id for hit, _ in await visible_evidence([c.hit for c in candidates], collection)}
        return [c for c in candidates if c.hit.chunk_id in still_visible][:top_k]
    except Exception as exc:
        raise RetrievalError() from exc
    finally:
        store.close()
