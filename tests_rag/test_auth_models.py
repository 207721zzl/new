"""用户、服务端会话、审计和资源归属模型测试。"""

from app.db.base import Base


def _foreign_key_target(table_name: str, column_name: str) -> tuple[str, str | None]:
    column = Base.metadata.tables[table_name].c[column_name]
    foreign_key = next(iter(column.foreign_keys))
    return foreign_key.target_fullname, foreign_key.ondelete


def test_authentication_tables_are_registered() -> None:
    assert {"users", "auth_sessions", "admin_audit_logs"} <= set(
        Base.metadata.tables
    )

    users = Base.metadata.tables["users"]
    assert users.c.normalized_username.nullable is False
    assert users.c.password_hash.nullable is False
    assert users.c.failed_login_count.server_default.arg == "0"

    sessions = Base.metadata.tables["auth_sessions"]
    assert sessions.c.token_hash.unique is True
    assert sessions.c.csrf_token_hash.nullable is False
    assert _foreign_key_target("auth_sessions", "user_id") == (
        "users.user_id",
        "CASCADE",
    )


def test_business_resources_require_an_explicit_non_null_owner() -> None:
    conversations = Base.metadata.tables["conversations"]
    indexing_jobs = Base.metadata.tables["indexing_jobs"]

    assert conversations.c.user_id.nullable is False
    assert conversations.c.user_id.server_default is None
    assert _foreign_key_target("conversations", "user_id") == (
        "users.user_id",
        "RESTRICT",
    )

    assert indexing_jobs.c.created_by_user_id.nullable is False
    assert indexing_jobs.c.created_by_user_id.server_default is None
    assert _foreign_key_target("indexing_jobs", "created_by_user_id") == (
        "users.user_id",
        "RESTRICT",
    )
