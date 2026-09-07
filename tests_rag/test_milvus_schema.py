from app.rag.milvus_store import build_collection_schema, build_index_params


def test_milvus_schema_contains_parent_link_structure_dense_and_bm25():
    schema = build_collection_schema(dimension=1024).to_dict()
    fields = {field["name"] for field in schema["fields"]}
    functions = {function["name"] for function in schema["functions"]}

    assert {
        "chunk_id",
        "parent_chunk_id",
        "section_path",
        "page_start",
        "page_end",
        "dense_vector",
        "sparse_vector",
    } <= fields
    assert "content_bm25" in functions
    assert len(build_index_params()) == 2


def test_document_update_delete_filter_is_bounded_to_explicit_ids():
    from app.rag.milvus_store import MilvusKnowledgeStore

    class FakeClient:
        def __init__(self):
            self.delete_call = None

        def delete(self, **kwargs):
            self.delete_call = kwargs

        def flush(self, collection_name):
            assert collection_name == "knowledge"

    store = MilvusKnowledgeStore.__new__(MilvusKnowledgeStore)
    store.settings = type("Settings", (), {"milvus_collection": "knowledge"})()
    store.client = FakeClient()
    store.delete_documents(["policy", "policy", "manual"])

    assert store.client.delete_call == {
        "collection_name": "knowledge",
        "filter": 'document_id in ["policy", "manual"]',
    }
