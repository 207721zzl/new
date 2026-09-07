import pytest

from app.config import Settings


def test_rag_defaults_are_independent_from_business_agent():
    settings = Settings(_env_file=None)
    assert settings.app_name == "EvidenceRAG"
    assert settings.milvus_collection == "evidence_rag_knowledge"
    assert settings.knowledge_child_chunk_size < settings.knowledge_parent_chunk_size
    assert not hasattr(settings, "forecast_model_path")
    assert not hasattr(settings, "sql_query_limit")
    assert settings.auth_registration_enabled is True
    assert settings.auth_session_idle_minutes < settings.auth_session_absolute_hours * 60
    assert settings.auth_cookie_secure is None
    assert settings.pilot_reconcile_interrupted_tasks is False
    assert settings.pilot_conversation_retention_days == 90
    assert settings.knowledge_upload_max_uncompressed_bytes >= settings.knowledge_upload_max_file_bytes


def test_chunk_overlap_must_be_smaller_than_chunk():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            knowledge_child_chunk_size=400,
            knowledge_child_chunk_overlap=400,
        )


def test_auth_cookie_names_and_session_windows_are_validated():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            auth_session_cookie_name="same-cookie",
            auth_csrf_cookie_name="same-cookie",
        )
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            auth_session_absolute_hours=1,
            auth_session_idle_minutes=61,
        )
