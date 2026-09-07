"""配置默认值、密钥脱敏和跨字段约束测试。"""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import PROJECT_ROOT, Settings


def test_default_settings():
    settings = Settings(_env_file=None)

    assert settings.app_name == "InsightAgent"
    assert settings.business_timezone == "Asia/Shanghai"
    assert settings.milvus_collection == "business_knowledge"
    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.reranker_model == "BAAI/bge-reranker-large"
    assert settings.embedding_device == "auto"
    assert settings.reranker_device == "auto"
    assert settings.reranker_batch_size == 8
    assert settings.reranker_min_score == 0.1
    assert settings.retrieval_top_k == 5
    assert settings.hybrid_candidate_top_k == 8
    assert settings.agent_node_max_attempts == 3
    assert settings.text2sql_max_repairs == 2
    assert settings.intent_classification_max_tokens == 500
    assert settings.intent_classification_temperature == 0
    assert settings.query_rewrite_enabled is True
    assert settings.query_rewrite_max_tokens == 300
    assert settings.query_rewrite_temperature == 0
    assert settings.conversation_recent_turns == 12
    assert settings.conversation_context_max_chars == 12_000
    assert settings.forecast_default_days == 7
    assert settings.forecast_max_days == 90
    assert settings.forecast_torch_device == "auto"
    assert settings.forecast_min_history_days == 28
    assert settings.forecast_explanation_history_days == 28
    assert settings.forecast_explanation_max_tokens == 1200
    assert settings.resolved_forecast_model_path == (
        PROJECT_ROOT / "models" / "forecast" / "global_torch_forecaster.pt"
    )
    assert settings.model_preload_enabled is True
    assert settings.model_warmup_enabled is True
    assert settings.database_pool_size == 5
    assert settings.database_connect_timeout_seconds == 5
    assert settings.database_read_expected_user == "insight_reader"
    assert settings.sql_query_limit == 1000
    assert settings.sql_query_timeout_seconds == 5
    assert settings.sql_evidence_row_limit == 20
    assert settings.knowledge_upload_max_files == 20
    assert settings.knowledge_upload_max_file_bytes == 20 * 1024 * 1024
    assert settings.knowledge_upload_max_batch_bytes == 100 * 1024 * 1024
    assert settings.knowledge_index_batch_size == 64
    assert settings.knowledge_parent_chunk_size == 1600
    assert settings.knowledge_parent_chunk_overlap == 0
    assert settings.knowledge_child_chunk_size == 400
    assert settings.knowledge_child_chunk_overlap == 80
    assert settings.resolved_log_dir == PROJECT_ROOT / "logs"


def test_api_key_is_masked():
    secret_value = "do-not-print-this-key"
    settings = Settings(_env_file=None, deepseek_api_key=secret_value)

    assert isinstance(settings.deepseek_api_key, SecretStr)
    assert secret_value not in repr(settings)


def test_candidate_count_cannot_be_smaller_than_result_count():
    with pytest.raises(ValidationError, match="hybrid_candidate_top_k"):
        Settings(
            _env_file=None,
            retrieval_top_k=5,
            hybrid_candidate_top_k=3,
        )


def test_agent_node_attempts_cannot_exceed_three():
    with pytest.raises(ValidationError, match="agent_node_max_attempts"):
        Settings(_env_file=None, agent_node_max_attempts=4)


def test_upload_batch_limit_cannot_be_smaller_than_single_file_limit():
    with pytest.raises(ValidationError, match="knowledge_upload_max_batch_bytes"):
        Settings(
            _env_file=None,
            knowledge_upload_max_file_bytes=2048,
            knowledge_upload_max_batch_bytes=1024,
        )


def test_child_chunk_cannot_be_larger_than_parent_chunk():
    with pytest.raises(ValidationError, match="knowledge_child_chunk_size"):
        Settings(
            _env_file=None,
            knowledge_parent_chunk_size=500,
            knowledge_child_chunk_size=600,
        )
