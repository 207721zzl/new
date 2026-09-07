"""应用配置定义与环境变量加载。

所有可部署参数集中在 :class:`Settings` 中，并通过缓存的 ``get_settings``
在进程内共享，避免各模块重复解析 ``.env``。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """EvidenceRAG 的强类型运行配置。"""

    app_name: str = "EvidenceRAG"
    app_version: str = "1.0.0"
    app_environment: Literal["development", "test", "production"] = "development"
    app_timezone: str = "Asia/Shanghai"

    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    model_preload_enabled: bool = True
    model_warmup_enabled: bool = True
    model_preload_fail_fast: bool = False
    inference_queue_timeout_seconds: float = Field(default=30.0, gt=0, le=600)

    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    deepseek_max_retries: int = Field(default=2, ge=0, le=5)
    deepseek_max_tokens: int = Field(default=1200, ge=64, le=32768)
    deepseek_temperature: float = Field(default=0.1, ge=0, le=2)
    deepseek_thinking_enabled: bool = False
    query_rewrite_enabled: bool = True
    query_rewrite_max_tokens: int = Field(default=300, ge=64, le=2048)
    query_rewrite_temperature: float = Field(default=0, ge=0, le=1)

    conversation_recent_turns: int = Field(default=12, ge=1, le=50)
    conversation_context_max_chars: int = Field(default=12_000, ge=500, le=100_000)

    auth_registration_enabled: bool = True
    auth_session_absolute_hours: int = Field(default=12, ge=1, le=24 * 30)
    auth_session_idle_minutes: int = Field(default=60, ge=5, le=24 * 60)
    auth_session_touch_interval_seconds: int = Field(default=300, ge=30, le=3600)
    auth_login_max_failures: int = Field(default=5, ge=1, le=20)
    auth_login_lock_minutes: int = Field(default=15, ge=1, le=24 * 60)
    # None 表示按当前请求是否为 HTTPS 自动决定，本机 HTTP 与公网隧道可并存。
    auth_cookie_secure: bool | None = None
    auth_cookie_samesite: Literal["lax", "strict"] = "lax"
    auth_session_cookie_name: str = Field(
        default="evidence_rag_session",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    auth_csrf_cookie_name: str = Field(
        default="evidence_rag_csrf",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    # 单机试点的运行治理。正式多实例部署应改用集中式指标、任务队列和调度器。
    pilot_metrics_window_hours: int = Field(default=24, ge=1, le=24 * 30)
    pilot_stale_task_minutes: int = Field(default=30, ge=5, le=24 * 60)
    pilot_reconcile_interrupted_tasks: bool = False
    pilot_conversation_retention_days: int = Field(default=90, ge=1, le=3650)
    pilot_session_retention_days: int = Field(default=30, ge=1, le=3650)
    pilot_audit_retention_days: int = Field(default=365, ge=30, le=3650)
    pilot_rate_limit_enabled: bool = True
    pilot_login_requests_per_5_minutes: int = Field(default=20, ge=1, le=1000)
    pilot_registration_requests_per_hour: int = Field(default=10, ge=1, le=1000)
    pilot_query_requests_per_minute: int = Field(default=30, ge=1, le=1000)
    pilot_upload_requests_per_hour: int = Field(default=20, ge=1, le=1000)

    database_url: SecretStr = SecretStr(
        "mysql+asyncmy://insight_agent:insight_agent@127.0.0.1:13306/"
        "insight_agent?charset=utf8mb4"
    )
    database_read_url: SecretStr = SecretStr(
        "mysql+asyncmy://insight_reader:insight_reader@127.0.0.1:13306/"
        "insight_agent?charset=utf8mb4"
    )
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    database_echo: bool = False
    database_read_expected_user: str = Field(default="insight_reader", min_length=1)

    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: SecretStr | None = None
    milvus_collection: str = "evidence_rag_knowledge"

    embedding_model: str = "BAAI/bge-m3"
    embedding_device: Literal["auto", "cpu", "cuda"] = "auto"
    embedding_dimension: int = Field(default=1024, ge=1)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    retrieval_top_k: int = Field(default=5, ge=1, le=100)
    hybrid_candidate_top_k: int = Field(default=8, ge=1, le=200)
    hybrid_rrf_k: int = Field(default=60, ge=1, le=1000)
    milvus_search_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    dense_search_ef: int = Field(default=64, ge=1, le=4096)
    reranker_model: str = "BAAI/bge-reranker-large"
    reranker_device: Literal["auto", "cpu", "cuda"] = "auto"
    reranker_batch_size: int = Field(default=8, ge=1, le=256)
    reranker_max_length: int = Field(default=512, ge=64, le=8192)
    reranker_use_fast_tokenizer: bool = True
    reranker_normalize: bool = True
    reranker_min_score: float = Field(default=0.1, ge=0, le=1)
    hf_token: SecretStr | None = None
    hf_hub_disable_xet: bool = True
    hf_hub_download_timeout: int = Field(default=300, ge=10, le=3600)

    knowledge_source_file: Path = Path("data/knowledge/example.json")
    knowledge_parent_chunk_size: int = Field(default=1600, ge=200, le=16000)
    knowledge_parent_chunk_overlap: int = Field(default=0, ge=0, le=4000)
    knowledge_child_chunk_size: int = Field(default=400, ge=100, le=4000)
    knowledge_child_chunk_overlap: int = Field(default=80, ge=0, le=1000)
    knowledge_upload_max_files: int = Field(default=20, ge=1, le=100)
    knowledge_upload_max_file_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1024,
        le=1024 * 1024 * 1024,
    )
    knowledge_upload_max_batch_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1024,
        le=5 * 1024 * 1024 * 1024,
    )
    knowledge_upload_parse_timeout_seconds: float = Field(
        default=60.0,
        ge=1,
        le=600,
    )
    knowledge_upload_max_archive_entries: int = Field(
        default=5000,
        ge=10,
        le=100_000,
    )
    knowledge_upload_max_uncompressed_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        le=10 * 1024 * 1024 * 1024,
    )
    knowledge_upload_max_compression_ratio: float = Field(
        default=100.0,
        ge=1,
        le=10_000,
    )
    knowledge_index_batch_size: int = Field(default=64, ge=1, le=1024)
    pdf_ocr_enabled: bool = False
    pdf_ocr_language: str = Field(default="chi_sim+eng", min_length=3, max_length=64)
    pdf_ocr_dpi: int = Field(default=200, ge=72, le=600)
    excel_rows_per_block: int = Field(default=25, ge=1, le=500)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_retrieval_limits(self) -> Self:
        """校验检索候选数和文档切分参数之间的约束。"""
        if self.hybrid_candidate_top_k < self.retrieval_top_k:
            raise ValueError(
                "hybrid_candidate_top_k must be greater than or equal to "
                "retrieval_top_k"
            )
        if self.knowledge_parent_chunk_overlap >= self.knowledge_parent_chunk_size:
            raise ValueError(
                "knowledge_parent_chunk_overlap must be less than "
                "knowledge_parent_chunk_size"
            )
        if self.knowledge_child_chunk_overlap >= self.knowledge_child_chunk_size:
            raise ValueError(
                "knowledge_child_chunk_overlap must be less than "
                "knowledge_child_chunk_size"
            )
        if self.knowledge_child_chunk_size > self.knowledge_parent_chunk_size:
            raise ValueError(
                "knowledge_child_chunk_size must not exceed knowledge_parent_chunk_size"
            )
        if self.knowledge_upload_max_batch_bytes < self.knowledge_upload_max_file_bytes:
            raise ValueError(
                "knowledge_upload_max_batch_bytes must be greater than or equal to "
                "knowledge_upload_max_file_bytes"
            )
        if (
            self.knowledge_upload_max_uncompressed_bytes
            < self.knowledge_upload_max_file_bytes
        ):
            raise ValueError(
                "knowledge_upload_max_uncompressed_bytes must be greater than or "
                "equal to knowledge_upload_max_file_bytes"
            )
        if self.auth_session_idle_minutes > self.auth_session_absolute_hours * 60:
            raise ValueError(
                "auth_session_idle_minutes must not exceed the absolute session limit"
            )
        if (
            self.auth_session_touch_interval_seconds
            >= self.auth_session_idle_minutes * 60
        ):
            raise ValueError(
                "auth_session_touch_interval_seconds must be shorter than the idle limit"
            )
        if self.auth_session_cookie_name == self.auth_csrf_cookie_name:
            raise ValueError("authentication cookie names must be different")
        return self

    @property
    def resolved_log_dir(self) -> Path:
        """返回相对于项目根目录解析后的日志目录。"""
        if self.log_dir.is_absolute():
            return self.log_dir
        return PROJECT_ROOT / self.log_dir

    @property
    def resolved_knowledge_source_file(self) -> Path:
        """返回相对于项目根目录解析后的知识文件路径。"""
        if self.knowledge_source_file.is_absolute():
            return self.knowledge_source_file
        return PROJECT_ROOT / self.knowledge_source_file

    @property
    def database_url_value(self) -> str:
        """返回迁移和灌数使用的管理连接 URL。"""
        return self.database_url.get_secret_value()

    @property
    def database_read_url_value(self) -> str:
        """返回在线查询使用的只读连接 URL。"""
        return self.database_read_url.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    """创建并缓存进程级配置对象。"""
    return Settings()
