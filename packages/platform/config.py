from functools import lru_cache
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlatformSettings(BaseSettings):
    service_name: str = "gateway"
    internal_service_token: SecretStr | None = None
    identity_url: str = "http://identity:8000"
    chat_url: str = "http://chat:8000"
    knowledge_url: str = "http://knowledge:8000"
    inference_url: str = "http://inference:8000"
    service_timeout_seconds: float = Field(default=90, gt=0)
    identity_database_url: SecretStr | None = None
    chat_database_url: SecretStr | None = None
    knowledge_database_url: SecretStr | None = None
    broker_url: SecretStr = SecretStr("redis://queue:6379/0")
    rate_limit_url: SecretStr = SecretStr("redis://limits:6379/0")
    task_lease_seconds: int = Field(default=90, ge=15)
    task_max_attempts: int = Field(default=3, ge=1, le=10)
    inference_max_pending: int = Field(default=32, ge=1, le=256)
    object_endpoint: str = "http://objects:9000"
    object_bucket: str = "knowledge-uploads"
    object_access_key: SecretStr | None = None
    object_secret_key: SecretStr | None = None
    model_config = SettingsConfigDict(env_file=".env.microservices", extra="ignore")

    def token(self) -> str:
        value = self.internal_service_token.get_secret_value() if self.internal_service_token else ""
        if len(value) < 32:
            raise ValueError("INTERNAL_SERVICE_TOKEN must contain at least 32 characters")
        return value


@lru_cache
def settings() -> PlatformSettings:
    return PlatformSettings()
