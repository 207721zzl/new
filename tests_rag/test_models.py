from app.schemas import KnowledgeCitation, RunResult


def test_answer_contract_contains_traceable_location():
    result = RunResult(
        run_id="run-1",
        status="completed",
        answer="答案",
        citations=[
            KnowledgeCitation(
                index=1,
                chunk_id="child",
                parent_chunk_id="parent",
                document_id="doc",
                title="手册",
                source="manual.pdf",
                version="1",
                updated_at="2026-08-12",
                section_path=["部署", "配置"],
                page_start=8,
                page_end=9,
                retrieval_score=0.7,
                rerank_score=0.9,
            )
        ],
    )
    payload = result.model_dump()
    assert payload["citations"][0]["section_path"] == ["部署", "配置"]
    assert payload["citations"][0]["page_start"] == 8
